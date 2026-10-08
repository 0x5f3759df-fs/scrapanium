"""Focused real-allocation OOM checks for owned Bytes flatten paths.

Run explicitly with the experimental source-built optional curl backend. These
checks are correctness/fault-injection evidence, not benchmark timing results.
"""
import http.server
import os
from pathlib import Path
import re
import subprocess
import threading

import pytest

from test_websocket import ws


def _build_fault_client(root, entry, output, tmp_path):
    import importlib.util

    prefix = Path(os.environ["SCRAPANIUM_CURL_DIR"]).resolve()
    bend = Path(os.environ["BEND_SOURCE"]).resolve()
    bun = Path(os.environ["BUN"]).resolve()
    compiler = Path(os.environ.get("SCRAPANIUM_PINNED_CLANG", "/usr/lib/llvm-21/bin/clang")).resolve()
    wrapper_source = root / "tests/flatten_oom_malloc_wrap.c"
    cc_wrapper = tmp_path / ("clang-" + output.name)
    cc_wrapper.write_text(
        "#!/bin/sh\nexec " + str(compiler) +
        " \"$@\" -O2 -fno-inline -fno-omit-frame-pointer -no-pie "
        "-fsanitize=address,undefined -Wl,--wrap=malloc " + str(wrapper_source) + "\n",
        encoding="utf-8")
    cc_wrapper.chmod(0o755)

    script = root / "scripts/build.py"
    spec = importlib.util.spec_from_file_location("owned_flatten_oom_build", script)
    build_module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(build_module)
    old = {key: os.environ.get(key) for key in
           ("BEND_SOURCE", "BUN", "SCRAPANIUM_CURL_DIR", "CC")}
    try:
        os.environ["BEND_SOURCE"] = str(bend)
        os.environ["BUN"] = str(bun)
        os.environ["SCRAPANIUM_CURL_DIR"] = str(prefix)
        os.environ["CC"] = str(cc_wrapper)
        binary = build_module.build(entry, output, sanitize=False)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    nm = subprocess.run(["nm", "-S", "--defined-only", str(binary)],
                        check=True, capture_output=True, text=True)
    match = re.search(r"^([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+[tT]\s+sp_bend_bytes_flatten$",
                      nm.stdout, re.MULTILINE)
    assert match, "the fault-injection build must retain the local flatten symbol"
    begin = int(match.group(1), 16)
    end = begin + int(match.group(2), 16)
    return binary, prefix, begin, end


def _fault_environment(url, ca, prefix, begin, end):
    library = prefix / "lib" if (prefix / "lib/libcurl-impersonate.so").exists() else prefix
    return {
        **os.environ,
        "LD_LIBRARY_PATH": str(library),
        "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1",
        "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1",
        "SCRAPANIUM_TEST_URL": url,
        "SCRAPANIUM_TEST_CA": ca,
        "SCRAPANIUM_FAIL_FLATTEN_BEGIN": hex(begin),
        "SCRAPANIUM_FAIL_FLATTEN_END": hex(end),
        "SCRAPANIUM_FAIL_FLATTEN_SIZE": "300000",
    }


def _assert_flatten_failure(binary, env, marker):
    out = subprocess.run([str(binary), "--threads", "1"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == [marker]
    assert out.stderr.splitlines() == ["flatten-oom-injected-at-owned-bytes-allocation"]
    return out


@pytest.mark.parametrize("threads", [1])
def test_owned_flatten_allocation_failure_is_reported_and_releases_message(ws, tmp_path, threads):
    server, url, ca = ws
    root = Path(__file__).resolve().parents[1]
    binary, prefix, begin, end = _build_fault_client(
        root, root / "tests/websocket_owned_flatten_oom.bend",
        tmp_path / "websocket-owned-flatten-oom", tmp_path)
    before = len(server.frames)
    env = _fault_environment(url, ca, prefix, begin, end)
    _assert_flatten_failure(binary, env, "flatten-oom-propagated")
    expected = bytes((index * 37 + 11) & 255 for index in range(300000))
    assert not any(frame[0] == 2 and frame[1] == expected
                   for frame in server.frames[before:])


@pytest.mark.parametrize("threads", [1])
def test_binary_request_flatten_oom_keeps_error_and_does_not_issue_http(ws, tmp_path, threads):
    class CountingHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _record(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self.server.requests.append(
                (self.command, self.path, self.headers.get("X-Owned-OOM")))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _record
        do_POST = _record
        do_PUT = _record
        do_PATCH = _record
        do_DELETE = _record
        do_OPTIONS = _record
        do_HEAD = _record

    http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CountingHandler)
    http_server.daemon_threads = True
    http_server.requests = []
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        root = Path(__file__).resolve().parents[1]
        binary, prefix, begin, end = _build_fault_client(
            root, root / "tests/websocket_owned_request_oom.bend",
            tmp_path / "websocket-owned-request-oom", tmp_path)
        _, websocket_url, ca = ws
        http_url = f"http://127.0.0.1:{http_server.server_port}"
        env = _fault_environment(websocket_url, ca, prefix, begin, end)
        env["SCRAPANIUM_TEST_HTTP_URL"] = http_url
        _assert_flatten_failure(binary, env, "binary-request-flatten-oom-propagated")
        assert http_server.requests == [], (
            "BinaryRequest flatten failure must happen before any HTTP request; "
            f"observed {http_server.requests!r}")
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)
