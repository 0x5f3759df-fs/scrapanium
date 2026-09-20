"""Reject malformed Unicode scalar inputs before UTF-8 conversion loses bits."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lab import certificate
from ws_lab import start_ws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build import build

SCALARS = [0, 0x7f, 0x80, 0x7ff, 0x800, 0xd7ff, 0xe000, 0x10ffff,
           0xd800, 0xdfff, 0x110000, 0x04010000, 0xffffffff]


@pytest.fixture(scope="module")
def scalar_binary():
    return build(ROOT / "tests/text_scalars.bend", ROOT / "build/text-scalars", True)


def invoke(binary, mode, scalar, path, threads, url="", ca=""):
    result = subprocess.run([str(binary), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_MODE": mode, "SCRAPANIUM_TEST_SCALAR": str(scalar),
        "SCRAPANIUM_TEST_PAYLOAD": str(path), "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca,
        "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1", "UBSAN_OPTIONS": "halt_on_error=1"},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.splitlines()


@pytest.mark.parametrize("threads", [1, 4])
def test_bytes_from_text_scalar_boundaries(scalar_binary, tmp_path, threads):
    expected = tmp_path / "expected.bin"
    for scalar in SCALARS:
        valid = scalar <= 0x10ffff and not 0xd800 <= scalar <= 0xdfff
        expected.write_bytes(chr(scalar).encode() + b"tail" if valid else b"")
        assert invoke(scalar_binary, "bytes", scalar, expected, threads) == ["exact" if valid else "invalid"], hex(scalar)


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("tls", [False, True], ids=["ws", "wss"])
def test_close_scalar_boundaries(scalar_binary, tmp_path, threads, tls):
    context, ca = certificate(tmp_path) if tls else (None, "")
    server, url = start_ws(context)
    try:
        for scalar in SCALARS:
            valid = scalar <= 0x10ffff and not 0xd800 <= scalar <= 0xdfff
            before = len(server.frames)
            assert invoke(scalar_binary, "close", scalar, tmp_path / "unused", threads, url, ca) == ["closed" if valid else "invalid"], hex(scalar)
            expected_frames = [(8, b"\x03\xe8" + chr(scalar).encode() + b"tail", True)] if valid else []
            assert server.frames[before:] == expected_frames
    finally:
        server.shutdown(); server.server_close()
