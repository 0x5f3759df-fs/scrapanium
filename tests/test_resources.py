"""Response allocation accounting, cancellation, push sinks and atomic downloads."""
import ctypes as C
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import pytest
from binding import Cancel, Session, Response, Request, lib, request


@pytest.mark.parametrize("path", ["/bytes/65536", "/bomb", "/headers"])
def test_aggregate_budget_counts_retained_capacity(http, path):
    budget = 96 * 1024
    with Session(max_batch_bytes=budget) as s:
        results = s.batch([request(http + path) for _ in range(20)])
        try:
            assert sum(r.memory_bytes for r in results) <= budget
            assert any(r.error == 9 for r in results)
            assert all(r.error in (0, 9) for r in results)
        finally:
            for r in results: r.close()
        with s.send(http) as r:
            assert r.error == 0 and r.body == b"ok"  # Budget resets each call.


@pytest.mark.parametrize("limit", ["count", "metadata"])
def test_preflight_leaves_outputs_untouched(http, limit):
    with Session() as s, s.send("file:///invalid") as r: metadata = r.memory_bytes
    options = {"max_batch_requests": 1} if limit == "count" else {"max_batch_bytes": metadata}
    with Session(**options) as s:
        qs = (Request * 2)(request(http), request(http))
        # A recognizable sentinel verifies no output traversal on preflight.
        out = (C.c_void_p * 2)(123, 456)
        assert lib.sp_session_batch(s.ptr, qs, 2, out) == 9
        assert list(out) == [123, 456]


@pytest.mark.parametrize("option", ["max_batch_bytes", "max_batch_requests"])
def test_zero_resource_limit_rejected(option):
    with pytest.raises(ValueError): Session(**{option: 0})


def test_budget_must_fit_one_response():
    with pytest.raises(ValueError): Session(max_batch_bytes=1)


def test_pre_cancelled_is_sticky_and_prevents_invalid_network_request(http):
    with Cancel() as token, Session() as s:
        token.trigger(); token.trigger()
        assert lib.sp_cancel_is_triggered(token.ptr) == 1
        with s.send("http://127.0.0.1:1/", cancel=token) as r:
            assert r.error == 10 and r.new_connections == 0
        with s.send(http, cancel=token) as r: assert r.error == 10
        with s.send(http) as r: assert r.error == 0


def test_cancel_wakes_poll_and_aborts_queued_batch(http):
    with Cancel() as token, Session(concurrency=2) as s, ThreadPoolExecutor() as pool:
        future = pool.submit(s.batch, [request(http + "/slow/3000") for _ in range(30)], token)
        time.sleep(0.1)
        start = time.monotonic(); token.trigger()
        results = future.result(timeout=1)
        try:
            assert time.monotonic() - start < 0.8
            assert len(results) == 30 and all(r.error == 10 for r in results)
        finally:
            for r in results: r.close()
        with s.send(http) as r: assert r.error == 0


def test_shared_token_cancels_multiple_sessions(http):
    with Cancel() as token, Session() as a, Session() as b, ThreadPoolExecutor() as pool:
        jobs = [pool.submit(s.send, http + "/slow/3000", cancel=token) for s in (a, b)]
        time.sleep(0.1); token.trigger()
        for job in jobs:
            with job.result(timeout=1) as r: assert r.error == 10
        for s in (a, b):
            with s.send(http) as r: assert r.error == 0


def test_cancel_in_body_callback_and_reuse(http):
    chunks = []
    with Session() as s, Cancel() as token:
        def sink(data):
            chunks.append(data); token.trigger(); return 0
        with s.stream(http + "/paced", sink, cancel=token) as r:
            assert r.error == 10 and 0 < r.downloaded < 4096 * 64
            assert r.size == 0
        with s.send(http) as r: assert r.error == 0


@pytest.mark.parametrize("path,expected", [
    ("/binary", b"\x00\xff\xc0\x80hello\x00"), ("/chunked", b"abcde"),
    ("/bomb", b"x" * 65536), ("/bytes/1048576", bytes(range(256)) * 4096)])
def test_push_stream_is_decoded_and_has_no_body_buffer(http, path, expected):
    chunks = []
    with Session(max_batch_bytes=8192) as s:
        def sink(data): chunks.append(data); return 0
        with s.stream(http + path, sink) as r:
            assert r.error == 0 and r.body == b""
            assert r.downloaded == len(expected) and r.memory_bytes <= 8192
            assert b"".join(chunks) == expected
            assert max(map(len, chunks)) <= 16384


def test_sink_failure_and_decoded_limit(http):
    with Session(max_body_bytes=32768) as s:
        with s.stream(http + "/bytes/65536", lambda _: 1) as r: assert r.error == 11
        sizes = []
        def sink(data): sizes.append(len(data)); return 0
        with s.stream(http + "/bomb", sink) as r:
            assert r.error == 5 and r.downloaded == sum(sizes) <= 32768
        with s.send(http) as r: assert r.error == 0


def test_slow_sink_is_synchronous_and_preserves_chunk_order(http):
    chunks = []
    owner = threading.get_ident()
    def sink(data):
        assert threading.get_ident() == owner
        time.sleep(0.002); chunks.append(data); return 0
    with Session() as s:
        with s.stream(http + "/bytes/262144", sink) as r:
            assert r.error == 0 and b"".join(chunks) == bytes(range(256)) * 1024


@pytest.mark.parametrize("path,expected", [
    ("/binary", b"\x00\xff\xc0\x80hello\x00"), ("/bomb", b"x" * 65536),
    ("/bytes/1048576", bytes(range(256)) * 4096), ("/chunked", b"abcde")])
def test_atomic_download_success(http, tmp_path, path, expected):
    target = tmp_path / "data.bin"
    target.write_bytes(b"previous")
    with Session(max_batch_bytes=8192) as s, s.download(http + path, target) as r:
        assert r.error == 0 and r.size == 0 and r.downloaded == len(expected)
        assert r.memory_bytes <= 8192 and target.read_bytes() == expected
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("path,options,error", [
    ("/status/404", {}, 12), ("/truncated", {}, 4),
    ("/slow/1000", {"timeout_ms": 40}, 4), ("/bomb", {"max_body_bytes": 1024}, 5),
    ("/bytes/65536", {"max_batch_bytes": 1000}, 9)])
def test_failed_download_preserves_destination(http, tmp_path, path, options, error):
    target = tmp_path / "data.bin"; target.write_bytes(b"previous")
    with Session(**options) as s, s.download(http + path, target) as r:
        assert r.error == error
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]


def test_cancelled_download_removes_temporary(http, tmp_path):
    target = tmp_path / "data.bin"; target.write_bytes(b"previous")
    with Session() as s, Cancel() as token, ThreadPoolExecutor() as pool:
        future = pool.submit(s.download, http + "/paced", target, token)
        deadline = time.monotonic() + 3
        while not list(tmp_path.glob("*.scrapanium-*")):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        time.sleep(0.04); token.trigger()
        with future.result(timeout=1) as r: assert r.error == 10
        with s.send(http) as r: assert r.error == 0
    assert target.read_bytes() == b"previous" and list(tmp_path.iterdir()) == [target]


def test_download_verifies_tls(https, tmp_path):
    url, ca = https; target = tmp_path / "data.bin"
    with Session() as s, s.download(url, target) as r: assert r.error == 4
    assert not target.exists() and not list(tmp_path.iterdir())
    with Session(ca_bundle=ca) as s, s.download(url, target) as r: assert r.error == 0


def test_download_destination_errors(http, tmp_path):
    with Session() as s:
        with s.download(http, tmp_path / "missing/child") as r: assert r.error == 11
        # Rename failure must not remove the directory or leave a temp file.
        directory = tmp_path / "directory"; directory.mkdir()
        with s.download(http, directory) as r: assert r.error == 11
        assert directory.is_dir() and list(tmp_path.iterdir()) == [directory]
        with s.download(http, "") as r: assert r.error == 1
