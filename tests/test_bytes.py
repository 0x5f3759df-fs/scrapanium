import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build import build, compiler


@pytest.fixture(scope="module")
def upload():
    binary = build(ROOT / "tests/bytes_file.bend", ROOT / "build/bytes-file", True)
    def run(path, limit, url):
        out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ,
            "PROBE_FILE": str(path), "PROBE_LIMIT": str(limit), "SCRAPANIUM_TEST_URL": url},
            capture_output=True, text=True, timeout=10)
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout.strip()
    return run


@pytest.mark.parametrize("size", [0, 1, 255, 256, 4096, 32768])
def test_file_bytes_upload_exact(upload, http, tmp_path, size):
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    path = tmp_path / "bytes.bin"; path.write_bytes(data)
    response = json.loads(upload(path, size, http + "/echo"))
    assert bytes(response["body"]) == data and response["method"] == "POST"


def test_read_limit_no_partial_upload(upload, http, tmp_path):
    path = tmp_path / "bytes.bin"; path.write_bytes(b"x" * 1025)
    assert upload(path, 1024, http + "/echo") == "error:13"
    assert upload(path, 0, http + "/echo") == "error:13"


@pytest.mark.parametrize("kind,error", [("missing", 11), ("directory", 1), ("fifo", 1)])
def test_file_errors_and_nonregular_files_do_not_block(upload, http, tmp_path, kind, error):
    path = tmp_path / "input"
    if kind == "directory": path.mkdir()
    if kind == "fifo": os.mkfifo(path)
    assert upload(path, 100, http + "/echo") == f"error:{error}"


def test_byte_access_text_and_response_move(http):
    binary = build(ROOT / "tests/bytes_helpers.bend", ROOT / "build/bytes-helpers", True)
    out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ, "SCRAPANIUM_TEST_URL": http},
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    assert lines[:4] == ["error:1", "104", "none", "héllo\x00🌍"]
    response = json.loads(lines[4])
    assert bytes(response["body"]) == b"\x00\xff\xc0\x80hello\x00"
    assert response["path"] == "/echo?binary=yes"


def test_bytes_cannot_be_duplicated():
    out = subprocess.run(compiler() + [str(ROOT / "tests/bytes_affine_reject.bend"), "--check-only"], capture_output=True, text=True)
    assert out.returncode != 0 and "consumed more than once" in out.stdout + out.stderr
