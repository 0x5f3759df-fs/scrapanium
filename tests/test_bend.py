import os
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build import build, compiler

def test_proofs():
    out = subprocess.run(compiler() + [str(ROOT / "PROOF.bend"), "--check-only"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "All terms check" in out.stdout + out.stderr

@pytest.mark.parametrize("sanitize", [False, True])
def test_bend_batch_end_to_end(http, sanitize):
    binary = build(ROOT / "tests/integration.bend", ROOT / f"build/integration-{sanitize}", sanitize)
    out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ, "SCRAPANIUM_TEST_URL": http}, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    assert lines[:4] == ["Bend: héllo 🌍", "error:1", "abcde", "ok"]
    assert '"method": "POST"' in lines[4] and '"X-Bend", "works"' in lines[4]
    assert "ERROR: AddressSanitizer" not in out.stderr

def test_bend_exact_binary_save(http, tmp_path):
    binary = build(ROOT / "tests/binary.bend", ROOT / "build/binary-test", True)
    output = tmp_path / "data.bin"
    out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ,
        "SCRAPANIUM_TEST_URL": http, "SCRAPANIUM_TEST_OUTPUT": str(output)}, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip() == "255"
    assert output.read_bytes() == b"\x00\xff\xc0\x80hello\x00"

def test_affine_session_cannot_be_duplicated():
    out = subprocess.run(compiler() + [str(ROOT / "tests/affine_reject.bend"), "--check-only"], capture_output=True, text=True)
    assert out.returncode != 0
    assert "session" in out.stdout + out.stderr

def test_invalid_host_handle_is_rejected():
    binary = build(ROOT / "tests/invalid_handle.bend", ROOT / "build/invalid-handle", True)
    out = subprocess.run([str(binary), "--threads", "1"], capture_output=True, text=True, timeout=10)
    assert out.returncode != 0
    assert "invalid Scrapanium handle" in out.stdout + out.stderr
    assert "AddressSanitizer" not in out.stderr

@pytest.mark.parametrize("source,message", [("stress", "stress: passed"), ("resource_stress", "resources: passed")])
def test_native_sanitizer_stress(http, source, message):
    curl = Path(os.environ.get('SCRAPANIUM_CURL_DIR', ROOT / '.deps/curl')).resolve()
    curl_lib = curl / 'lib' if (curl / 'lib/libcurl-impersonate.so').exists() else curl
    binary = ROOT / "build" / source
    subprocess.run(["clang", "-std=c11", "-O2", "-g", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I" + str(ROOT / "native"),
        "-I" + str(curl / "include"), str(ROOT / "tests" / (source + ".c")), str(ROOT / "native/scrapanium.c"),
        "-L" + str(curl_lib), "-Wl,-rpath," + str(curl_lib), "-lcurl-impersonate", "-lpthread", "-o", str(binary)], check=True)
    out = subprocess.run([str(binary), http], capture_output=True, text=True, timeout=30,
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1", "UBSAN_OPTIONS": "halt_on_error=1"})
    assert out.returncode == 0, out.stdout + out.stderr
    assert message in out.stdout


def test_bend_cancellation_and_atomic_download(http, tmp_path):
    binary = build(ROOT / "tests/resources.bend", ROOT / "build/resources-test", True)
    output = tmp_path / "data.bin"
    out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ,
        "SCRAPANIUM_TEST_URL": http, "SCRAPANIUM_TEST_OUTPUT": str(output)}, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["timer", "request-error:10", "token:1", "token:1", "download:200:1048576", "download-error:4"]
    assert output.read_bytes() == bytes(range(256)) * 4096
    assert list(tmp_path.iterdir()) == [output]


@pytest.fixture(scope="module")
def config_probe():
    binary = build(ROOT / "tests/config_probe.bend", ROOT / "build/config-probe", True)
    def run(url, ca="", mode="default"):
        out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ,
            "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca, "SCRAPANIUM_TEST_MODE": mode},
            capture_output=True, text=True, timeout=10)
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout.strip()
    return run


def test_bend_tls_verification_and_ca(config_probe, https):
    url, ca = https
    assert config_probe(url) == "error:4"
    assert config_probe(url, ca) == "ok"
    assert config_probe(url, mode="verify-off") == "ok"


def test_bend_config_flags_and_flattened_records(config_probe, http):
    assert config_probe(http + "/redirect", mode="redirect-off") == "redirect"
    assert json.loads(config_probe(http + "/redirect"))["method"] == "GET"
    with_headers = dict(json.loads(config_probe(http + "/echo"))["headers"])
    without_headers = dict(json.loads(config_probe(http + "/echo", mode="headers-off"))["headers"])
    assert "User-Agent" in with_headers and "Chrome/150" in with_headers["User-Agent"]
    assert "User-Agent" not in without_headers
    assert config_probe(http + "/bytes/65536", mode="budget") == "error:9"
    assert config_probe(http, mode="bad-fingerprint") == "error:1"


def test_bend_fingerprint_overrides_reach_wire(config_probe):
    from fingerprint import capture
    def run(url): assert config_probe(url, mode="custom") == "error:4"
    hello = capture(run)
    assert hello["details"]["10"] == [23, 24]
    assert hello["grease_ciphers"] == hello["grease_extensions"] == 0


@pytest.mark.parametrize("mismatch,expired", [(True, False), (False, True)])
def test_bend_rejects_trusted_wrong_identity_or_expiry(config_probe, tmp_path, mismatch, expired):
    from lab import certificate, start_http
    context, ca = certificate(tmp_path, mismatch=mismatch, expired=expired)
    server, url = start_http(context)
    try: assert config_probe(url, ca) == "error:4"
    finally: server.shutdown(); server.server_close()


def test_bend_cancelled_batch_and_download(http, tmp_path):
    binary = build(ROOT / "tests/cancel_variants.bend", ROOT / "build/cancel-variants", True)
    output = tmp_path / "data.bin"; output.write_bytes(b"previous")
    out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ,
        "SCRAPANIUM_TEST_URL": http, "SCRAPANIUM_TEST_OUTPUT": str(output)}, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["batch:10", "batch:10", "download:10"]
    assert output.read_bytes() == b"previous" and list(tmp_path.iterdir()) == [output]


def test_download_example_effect_subset(http, tmp_path):
    binary = build(ROOT / "examples/download.bend", ROOT / "build/download-example", True)
    output = tmp_path / "data.bin"
    out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ,
        "SCRAPANIUM_URL": http + "/binary", "SCRAPANIUM_OUTPUT": str(output)}, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip() == "Saved 10 bytes from " + http + "/binary"
    assert output.read_bytes() == b"\x00\xff\xc0\x80hello\x00"
