"""Explicit checks for the optional patched libcurl owned-buffer backend."""
import os
from pathlib import Path
import subprocess
import sys

from test_websocket import ws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_bytes_text_matches_pinned_bend_utf8_decoder():
    from build import build

    binary = build(ROOT / "tests/bytes_text_edges.bend", ROOT / "build/bytes-text-edges", True)
    out = subprocess.run([str(binary), "--threads", "1"], env=os.environ,
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["byte-text-edges-ok"]



def test_segmented_bytes_equal_uses_full_mismatch_and_partition_checks(ws, tmp_path):
    from build import build

    _, url, ca = ws
    payload = bytes((i * 37 + 11) & 255 for i in range(131073))
    expected = tmp_path / "expected.bin"
    first = tmp_path / "first.bin"
    middle = tmp_path / "middle.bin"
    last = tmp_path / "last.bin"
    expected.write_bytes(payload)
    for path, index in ((first, 0), (middle, len(payload) // 2), (last, len(payload) - 1)):
        variant = bytearray(payload)
        variant[index] ^= 0x80
        path.write_bytes(variant)

    binary = build(ROOT / "tests/websocket_owned_equal.bend", ROOT / "build/ws-owned-equal", True)
    out = subprocess.run([str(binary), "--threads", "4"], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca,
        "SCRAPANIUM_TEST_EXPECTED": str(expected), "SCRAPANIUM_TEST_FIRST": str(first),
        "SCRAPANIUM_TEST_MIDDLE": str(middle), "SCRAPANIUM_TEST_LAST": str(last)},
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["owned-equality-ok"]
