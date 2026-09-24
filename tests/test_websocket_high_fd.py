"""WSS readiness and cancellation with the client socket above FD_SETSIZE."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from lab import certificate
from ws_lab import Handler, Server, frame, read_frame

ROOT = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).with_name("high_fd_exec.py")
sys.path.insert(0, str(ROOT / "scripts"))
from build import build


def _skip_unavailable_high_fds():
    if os.name != "posix" or not Path("/proc/self/fd").is_dir():
        pytest.skip("high-FD verification requires Linux /proc")
    import resource
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and soft < 1200:
        pytest.skip(f"RLIMIT_NOFILE soft limit {soft} is below the required 1200")


class SlowFragmentHandler(Handler):
    def do_GET(self):
        self.server.request_event.set()
        if not self.server.upgrade_gate.wait(30):
            return
        if self.path != "/slow-fragmented":
            return super().do_GET()

        import base64
        import hashlib
        import socket

        self.server.handshakes.append(list(self.headers.items()))
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        self.send_response(101)
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", "websocket")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(4)
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        for index in range(30):
            wire = frame(2 if index == 0 else 0, bytes([index]), index == 29)
            self.wfile.write(wire)
            self.wfile.flush()
            time.sleep(0.02)
        try:
            kind, data, final = read_frame(self.rfile)
            self.server.frames.append((kind, data, final))
            if kind == 8:
                self.wfile.write(frame(8, data))
                self.wfile.flush()
        except (OSError, EOFError):
            pass


@pytest.fixture(scope="module")
def high_fd_binaries():
    _skip_unavailable_high_fds()
    (ROOT / "build/high-fd").mkdir(parents=True, exist_ok=True)
    return {
        "receive": build(ROOT / "tests/websocket_receive.bend", ROOT / "build/high-fd/ws-receive", True),
        "async": build(ROOT / "tests/websocket_async.bend", ROOT / "build/high-fd/ws-async", True),
        "blocked": build(ROOT / "tests/websocket_blocked_send.bend", ROOT / "build/high-fd/ws-blocked-send", True),
    }


@pytest.fixture(scope="module")
def high_fd_wss(tmp_path_factory):
    context, ca = certificate(tmp_path_factory.mktemp("high-fd-wss"))
    context.set_alpn_protocols(["http/1.1"])
    server = Server(("127.0.0.1", 0), SlowFragmentHandler)
    server.handshakes, server.frames = [], []
    server.request_event = threading.Event()
    server.upgrade_gate = threading.Event()
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"wss://127.0.0.1:{server.server_port}"
    yield server, url, ca
    server.upgrade_gate.set()
    server.shutdown()
    server.server_close()


def _scan_client_fds(pid):
    filler = False
    sockets = []
    directory = Path(f"/proc/{pid}/fd")
    try:
        entries = list(directory.iterdir())
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return filler, sockets
    for entry in entries:
        try:
            fd = int(entry.name)
            target = os.readlink(entry)
        except (ValueError, FileNotFoundError, PermissionError, OSError):
            continue
        if fd >= 1100 and target == "/dev/null":
            filler = True
        if fd >= 1024 and target.startswith("socket:["):
            sockets.append(fd)
    return filler, sockets


def _run_high_fd(binary, threads, env, server, expected_lines,
                 timeout=10, unordered=False):
    command = [
        sys.executable, str(HELPER), "--fd-through", "1100", "--",
        str(binary), "--threads", str(threads),
    ]
    proc = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    inherited_filler_seen = False
    high_socket_fds = set()
    deadline = time.monotonic() + timeout
    stdout = stderr = ""
    timed_out = False
    drained = False
    try:
        while proc.poll() is None and time.monotonic() < deadline:
            filler, sockets = _scan_client_fds(proc.pid)
            inherited_filler_seen |= filler
            if server.request_event.is_set() and inherited_filler_seen:
                high_socket_fds.update(sockets)
                if high_socket_fds:
                    server.upgrade_gate.set()
            time.sleep(0.005)

        timed_out = proc.poll() is None
        if timed_out:
            proc.kill()
        stdout, stderr = proc.communicate(timeout=2)
        drained = True
    finally:
        server.upgrade_gate.set()
        if proc.poll() is None:
            proc.kill()
        if not drained:
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=2)

    assert not timed_out, f"high-FD client exceeded {timeout}s\\n{stdout}{stderr}"
    assert inherited_filler_seen, "exec'd Bend process did not retain a high /dev/null filler FD"
    assert high_socket_fds, "no actual client socket was observed at FD >= 1024 before HTTP 101"
    assert proc.returncode == 0, stdout + stderr
    actual_lines = stdout.splitlines()
    if unordered:
        actual_lines.sort()
        expected_lines = sorted(expected_lines)
    assert actual_lines == expected_lines, stdout + stderr
    assert len(server.handshakes) > 0, "WSS fixture did not observe a completed client handshake"
    return sorted(high_socket_fds)


def _env(url, ca, **values):
    return {
        **os.environ,
        "SCRAPANIUM_TEST_URL": url,
        "SCRAPANIUM_TEST_CA": ca,
        **values,
        "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1",
        "UBSAN_OPTIONS": "halt_on_error=1",
    }


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_wss_receive_with_socket_above_fd_setsize(
        high_fd_binaries, high_fd_wss, tmp_path, threads, request):
    server, url, ca = high_fd_wss
    server.request_event.clear()
    server.upgrade_gate.clear()
    before_frames = len(server.frames)
    payload = tmp_path / "expected.bin"
    payload.write_bytes(bytes(range(30)))
    socket_fds = _run_high_fd(
        high_fd_binaries["receive"], threads,
        _env(url + "/slow-fragmented", ca, SCRAPANIUM_TEST_MODE="receive",
             SCRAPANIUM_TEST_PAYLOAD=str(payload), SCRAPANIUM_TEST_KIND="2"),
        server, ["received"],
    )
    request.node.user_properties.append(("observed_client_socket_fds_ge_1024", ",".join(map(str, socket_fds))))
    assert (8, b"\x03\xe8done", True) in server.frames[before_frames:]


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_wss_async_cancel_timeout_with_socket_above_fd_setsize(
        high_fd_binaries, high_fd_wss, threads, request):
    server, url, ca = high_fd_wss
    server.request_event.clear()
    server.upgrade_gate.clear()
    before_handshakes = len(server.handshakes)
    socket_fds = _run_high_fd(
        high_fd_binaries["async"], threads,
        _env(url + "/echo", ca),
        server, ["cancelled", "timeout", "timer"], unordered=True,
    )
    request.node.user_properties.append(("observed_client_socket_fds_ge_1024", ",".join(map(str, socket_fds))))
    assert len(server.handshakes) >= before_handshakes + 2


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_wss_blocked_send_with_socket_above_fd_setsize(
        high_fd_binaries, high_fd_wss, tmp_path, threads, request):
    server, url, ca = high_fd_wss
    server.request_event.clear()
    server.upgrade_gate.clear()
    before_handshakes = len(server.handshakes)
    payload = tmp_path / "blocked.bin"
    payload.write_bytes(b"z" * (8 * 1024 * 1024))
    socket_fds = _run_high_fd(
        high_fd_binaries["blocked"], threads,
        _env(url + "/stall-read", ca, SCRAPANIUM_TEST_PAYLOAD=str(payload)),
        server, ["cancelled-send", "timeout-send"], timeout=8, unordered=True,
    )
    request.node.user_properties.append(("observed_client_socket_fds_ge_1024", ",".join(map(str, socket_fds))))
    assert len(server.handshakes) >= before_handshakes + 2
