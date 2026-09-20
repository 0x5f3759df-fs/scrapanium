"""Exercise suspended state and always-ready fairness through compiled Bend IO."""
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


@pytest.fixture(scope="module")
def readiness_binary():
    return build(ROOT / "tests/websocket_receive.bend", ROOT / "build/ws-readiness-bend", True)


@pytest.fixture(scope="module", params=[False, True], ids=["ws", "wss"])
def readiness_server(request, tmp_path_factory):
    context, ca = certificate(tmp_path_factory.mktemp("ws-readiness")) if request.param else (None, "")
    server, url = start_ws(context)
    yield server, url, ca
    server.shutdown(); server.server_close()


def invoke(binary, url, ca, mode, payload, kind, threads):
    result = subprocess.run([str(binary), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca,
        "SCRAPANIUM_TEST_MODE": mode, "SCRAPANIUM_TEST_PAYLOAD": str(payload),
        "SCRAPANIUM_TEST_KIND": str(kind),
        "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1", "UBSAN_OPTIONS": "halt_on_error=1"},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.splitlines()


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("path,kind,payload", [
    ("fragments", 1, "hello 🌍".encode()),
    ("fragmented-binary", 2, bytes(range(256)) * 257),
    ("bytewise-frame", 2, b"\x00\xff\x80\xc0boundary\x00"),
    ("close", 8, b"\x03\xe8bye"),
], ids=["fragmented-text", "fragmented-binary", "bytewise", "remote-close"])
def test_bend_fragment_control_and_close(readiness_binary, readiness_server, tmp_path, threads, path, kind, payload):
    server, url, ca = readiness_server
    expected = tmp_path / "expected.bin"
    expected.write_bytes(payload)
    before = len(server.frames)
    assert invoke(readiness_binary, url + "/" + path, ca, "receive", expected, kind, threads) == ["received"]
    frames = server.frames[before:]
    if path == "fragments":
        assert (10, b"heartbeat", True) in frames
    elif path == "fragmented-binary":
        assert [frame for frame in frames if frame[0] == 10] == [(10, bytes([i, 0, 255]), True) for i in range(6)]
    assert any(frame[0] == 8 for frame in frames)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_always_ready_sends_yield_to_timer(readiness_binary, readiness_server, tmp_path, threads):
    _, url, ca = readiness_server
    # Empty frames fit well inside the socket buffer. Without completion yields,
    # an immediate send loop can finish before Bend dispatches its forked timer.
    assert invoke(readiness_binary, url + "/echo", ca, "fairness", tmp_path / "unused", 2, threads) == ["timer", "sent"]
