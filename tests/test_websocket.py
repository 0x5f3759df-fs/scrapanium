import ctypes as C
import threading
import time
import pytest
from binding import lib, declare, request, Session, Cancel
from lab import certificate
from ws_lab import start_ws

declare("sp_ws_upgrade", C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_int))
declare("sp_ws_free", None, C.c_void_p)
declare("sp_ws_send", C.c_int, C.c_void_p, C.c_uint, C.c_void_p, C.c_size_t, C.c_uint32, C.c_void_p)
declare("sp_ws_receive", C.c_int, C.c_void_p, C.POINTER(C.c_uint), C.POINTER(C.c_void_p), C.POINTER(C.c_size_t), C.c_uint32, C.c_void_p)
declare("sp_ws_close", C.c_int, C.c_void_p, C.c_uint, C.c_void_p, C.c_size_t, C.c_uint32, C.c_void_p)
libc = C.CDLL(None); libc.free.argtypes = [C.c_void_p]


class WebSocket:
    def __init__(self, url, cancel=None, headers=(), **options):
        session = Session(concurrency=1, **options)
        q = request(url, headers=headers); error = C.c_int()
        self.ptr = lib.sp_ws_upgrade(session.ptr, C.byref(q), cancel.ptr if cancel else None, C.byref(error))
        session.ptr = None
        self.error = error.value
    def send(self, data, kind=2, timeout=2000, cancel=None):
        return lib.sp_ws_send(self.ptr, kind, data, len(data), timeout, cancel.ptr if cancel else None)
    def recv(self, timeout=2000, cancel=None):
        kind, data, size = C.c_uint(), C.c_void_p(), C.c_size_t()
        err = lib.sp_ws_receive(self.ptr, C.byref(kind), C.byref(data), C.byref(size), timeout, cancel.ptr if cancel else None)
        try: return err, kind.value, C.string_at(data, size.value) if data else b""
        finally: libc.free(data)
    def close(self, code=1000, reason=b"", timeout=2000):
        return lib.sp_ws_close(self.ptr, code, reason, len(reason), timeout, None)
    def __enter__(self): return self
    def __exit__(self, *_): lib.sp_ws_free(self.ptr)


@pytest.fixture(scope="module", params=[False, True], ids=["ws", "wss"])
def ws(request, tmp_path_factory):
    context, ca = certificate(tmp_path_factory.mktemp("ws")) if request.param else (None, "")
    server, url = start_ws(context)
    yield server, url, ca
    server.shutdown(); server.server_close()


@pytest.mark.parametrize("kind,data", [(1, "hello 🌍\0".encode()), (2, bytes(range(256))), (1, b""), (2, b""), (2, b"x" * 65536)])
def test_roundtrip_masking_and_close(ws, kind, data):
    server, url, ca = ws
    with WebSocket(url + "/echo", ca_bundle=ca, headers=["Origin: https://example.test", "Sec-WebSocket-Protocol: chat"]) as w:
        assert w.error == 0
        assert w.send(data, kind) == 0
        assert w.recv() == (0, kind, data)
        assert w.close(reason=b"done") == 0
        assert w.send(b"after-close") == 16
    assert (kind, data, True) in server.frames
    assert dict(server.handshakes[-1])["Origin"] == "https://example.test"


def test_fragmented_utf8_and_interleaved_ping(ws):
    server, url, ca = ws
    with WebSocket(url + "/fragments", ca_bundle=ca) as w:
        assert w.error == 0
        assert w.recv() == (0, 1, "hello 🌍".encode())
        assert w.close() == 0
    assert (10, b"heartbeat", True) in server.frames


def test_large_receive_and_partial_send(ws):
    _, url, ca = ws
    data = bytes(range(256)) * 4096
    with WebSocket(url + "/large", ca_bundle=ca) as w:
        assert w.recv() == (0, 2, data)
    with WebSocket(url + "/slow-send", ca_bundle=ca) as w:
        assert w.send(data * 4, timeout=5000) == 0
        assert w.recv(timeout=5000) == (0, 2, data * 4)


@pytest.mark.parametrize("path,error", [("large", 5), ("fragment-limit", 5), ("bad-utf8", 15), ("bad-close", 15), ("truncated", 16)])
def test_receive_limits_and_protocol_failure(ws, path, error):
    _, url, ca = ws
    with WebSocket(url + "/" + path, ca_bundle=ca, max_body_bytes=100) as w:
        assert w.error == 0
        actual = w.recv()[0]
        # TLS truncation is a transport error; plain TCP EOF is closed.
        assert actual in (4, 16) if path == "truncated" else actual == error
        assert w.send(b"no-retry") == 16


def test_remote_close(ws):
    server, url, ca = ws
    with WebSocket(url + "/close", ca_bundle=ca) as w:
        assert w.recv() == (0, 8, b"\x03\xe8bye")
        assert w.close() == 0


def test_timeout_and_ping_flood_deadline(ws):
    _, url, ca = ws
    for path in ("echo", "flood-ping"):
        with WebSocket(url + "/" + path, ca_bundle=ca) as w:
            start = time.monotonic()
            assert w.recv(timeout=100)[0] == 14
            assert time.monotonic() - start < .5


def test_cancellation_and_busy(ws):
    _, url, ca = ws
    with Cancel() as token, WebSocket(url + "/echo", ca_bundle=ca) as w:
        result = []
        worker = threading.Thread(target=lambda: result.append(w.recv(cancel=token)))
        worker.start(); time.sleep(.05)
        assert w.send(b"busy") == 7
        start = time.monotonic(); token.trigger(); worker.join(1)
        assert result == [(10, 0, b"")] and time.monotonic() - start < .3


@pytest.mark.parametrize("path", ["reject", "redirect", "bad-accept", "missing-accept", "duplicate-accept", "extensions", "unsolicited-protocol"])
def test_bad_handshakes(ws, path):
    _, url, ca = ws
    with WebSocket(url + "/" + path, ca_bundle=ca) as w:
        assert w.error != 0 and not w.ptr


def test_upgrade_cancellation_and_timeout(ws):
    _, url, ca = ws
    with Cancel() as token:
        token.trigger()
        with WebSocket(url, cancel=token, ca_bundle=ca) as w: assert w.error == 10
    with WebSocket(url + "/stall-upgrade", ca_bundle=ca, timeout_ms=100) as w: assert w.error == 14


def test_send_validation_is_recoverable(ws):
    _, url, ca = ws
    with WebSocket(url + "/echo", ca_bundle=ca, max_body_bytes=100) as w:
        assert w.send(b"\xc0\x80", 1) == 1
        assert w.send(b"x", 8) == 1
        assert w.send(b"x" * 126, 9) == 1
        assert w.send(b"x" * 101) == 13
        assert w.close(code=1005) == 1
        assert w.send(b"valid") == 0 and w.recv() == (0, 2, b"valid")


def test_wss_certificate_validation(tmp_path):
    for mode in ("untrusted", "mismatch", "expired"):
        context, ca = certificate(tmp_path / mode, mismatch=mode == "mismatch", expired=mode == "expired")
        server, url = start_ws(context)
        try:
            with WebSocket(url, ca_bundle="" if mode == "untrusted" else ca) as w:
                assert not w.ptr and w.error == 4
        finally: server.shutdown(); server.server_close()


def test_wss_proxy(ws, proxy):
    _, url, ca = ws
    if not ca: return
    server, proxy_url = proxy
    with WebSocket(url + "/echo", ca_bundle=ca, proxy=proxy_url) as w:
        assert w.error == 0 and w.send(b"proxy") == 0 and w.recv() == (0, 2, b"proxy")
    assert server.requests[0].startswith("CONNECT ")


@pytest.fixture(scope="module")
def bend_ws():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket.bend", root / "build/ws-bend", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_wss_with_sanitizers(bend_ws, ws, threads):
    import os
    import subprocess
    _, url, ca = ws
    out = subprocess.run([str(bend_ws), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url + "/echo", "SCRAPANIUM_TEST_CA": ca}, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["1", "Bend 🌍\0"]


def test_sequence_and_frame_length_boundaries(ws):
    """Every returned byte and ordering at both RFC6455 length transitions."""
    import random
    server, url, ca = ws
    rng = random.Random(20260920)
    expected = [i.to_bytes(4, "big") + rng.randbytes(size) for i, size in enumerate(
        [0, 1, 121, 122, 123, 65531, 65532, 65533, 262140] * 3)]
    before = len(server.frames)
    with WebSocket(url + "/echo", ca_bundle=ca) as w:
        for payload in expected:
            assert w.send(payload, timeout=5000) == 0
            assert w.recv(timeout=5000) == (0, 2, payload)
    assert server.frames[before:] == [(2, payload, True) for payload in expected]


def test_fragmented_binary_and_partial_network_reads(ws):
    server, url, ca = ws
    before = len(server.frames)
    with WebSocket(url + "/fragmented-binary", ca_bundle=ca) as w:
        assert w.recv() == (0, 2, bytes(range(256)) * 257)
        assert w.send(b"next\x00\xff") == 0
        assert w.recv() == (0, 2, b"next\x00\xff")
    assert server.frames[before:before + 6] == [(10, bytes([i, 0, 255]), True) for i in range(6)]
    with WebSocket(url + "/bytewise-frame", ca_bundle=ca) as w:
        assert w.recv() == (0, 2, b"\x00\xff\x80\xc0boundary\x00")


def test_deadline_spans_fragment_progress(ws):
    _, url, ca = ws
    with WebSocket(url + "/slow-fragments", ca_bundle=ca) as w:
        start = time.monotonic()
        assert w.recv(timeout=100)[0] == 14
        assert time.monotonic() - start < .5
        assert w.send(b"closed-after-incomplete-frame") == 16


@pytest.fixture(scope="module")
def bend_ws_async():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_async.bend", root / "build/ws-async-bend", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_pending_cancel_and_timeout_with_sanitizers(bend_ws_async, ws, threads):
    import os
    import subprocess
    _, url, ca = ws
    out = subprocess.run([str(bend_ws_async), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url + "/echo", "SCRAPANIUM_TEST_CA": ca},
        capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    assert sorted(out.stdout.splitlines()) == ["cancelled", "timeout", "timer"]


@pytest.fixture(scope="module")
def bend_ws_checked():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "benchmarks/websocket.bend", root / "build/ws-checked-bend", True)


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("size", [0, 125, 126, 65535, 65536, 1048576])
def test_bend_exact_binary_roundtrips_with_sanitizers(bend_ws_checked, ws, tmp_path, threads, size):
    import os
    import subprocess
    _, url, ca = ws
    payload = tmp_path / "payload.bin"
    payload.write_bytes(bytes((i * 131 + 17) % 256 for i in range(size)))
    out = subprocess.run([str(bend_ws_checked), "--threads", str(threads)], input="x", env={**os.environ,
        "SCRAPANIUM_BENCH_URL": url + "/slow-send", "SCRAPANIUM_BENCH_CA": ca,
        "SCRAPANIUM_BENCH_COUNT": "8", "SCRAPANIUM_BENCH_PAYLOAD": str(payload)},
        capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines()[0] == "ready"


@pytest.fixture(scope="module")
def bend_ws_blocked():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_blocked_send.bend", root / "build/ws-blocked-send", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_partial_send_cancellation_and_timeout(bend_ws_blocked, ws, tmp_path, threads):
    import os
    import subprocess
    _, url, ca = ws
    payload = tmp_path / "blocked.bin"
    payload.write_bytes(bytes(range(256)) * 32768)
    out = subprocess.run([str(bend_ws_blocked), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url + "/stall-read", "SCRAPANIUM_TEST_CA": ca,
        "SCRAPANIUM_TEST_PAYLOAD": str(payload)}, capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stdout + out.stderr
    assert sorted(out.stdout.splitlines()) == ["cancelled-send", "timeout-send"]
