"""Explicit opt-in tests requiring the experimental patched libcurl backend.

Run this module only with the owned-buffer and canonical-length backend prefix.
The stock dependency does not have curl_ws_recv_owned and retains the upstream
noncanonical-length parser behavior.
"""
import pytest
from test_websocket import WebSocket, ws

@pytest.mark.parametrize("path", ["noncanonical16", "noncanonical64"])
def test_reject_noncanonical_frame_lengths(ws, path):
    _, url, ca = ws
    with WebSocket(url + "/" + path, ca_bundle=ca) as w:
        assert w.error == 0
        assert w.recv(timeout=2000)[0] == 4
        assert w.send(b"after-invalid-frame") == 16


@pytest.fixture(scope="module")
def bend_ws_noncanonical():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_noncanonical.bend", root / "build/ws-noncanonical", True)


@pytest.mark.parametrize("path", ["noncanonical16", "noncanonical64"])
@pytest.mark.parametrize("threads", [1, 4])
def test_bend_reject_noncanonical_frame_lengths(bend_ws_noncanonical, ws, threads, path):
    import os
    import subprocess
    _, url, ca = ws
    out = subprocess.run([str(bend_ws_noncanonical), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url + "/" + path, "SCRAPANIUM_TEST_CA": ca}, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["4", "transport failed", "noncanonical-rejected"]


@pytest.fixture(scope="module")
def bend_ws_owned_segments():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_owned_segments.bend", root / "build/ws-owned-segments", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_owned_segments_survive_next_receive_and_socket_disposal(
        bend_ws_owned_segments, ws, tmp_path, threads):
    import os
    import subprocess
    _, url, ca = ws
    expected = bytes((i * 37 + 11) & 255 for i in range(300000))
    payload = tmp_path / "owned-segments.bin"
    payload.write_bytes(expected)
    out = subprocess.run([str(bend_ws_owned_segments), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca,
        "SCRAPANIUM_TEST_EXPECTED": str(payload)}, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["owned-message-ok"]



@pytest.fixture(scope="module")
def bend_ws_owned_send():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_owned_send.bend", root / "build/ws-owned-send", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_owned_segments_flatten_for_ws_send(bend_ws_owned_send, ws, tmp_path, threads):
    import os
    import subprocess
    server, url, ca = ws
    expected = bytes((i * 37 + 11) & 255 for i in range(300000))
    payload = tmp_path / "owned-send.bin"
    payload.write_bytes(expected)
    before = len(server.frames)
    out = subprocess.run([str(bend_ws_owned_send), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca,
        "SCRAPANIUM_TEST_EXPECTED": str(payload)}, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["owned-send-ok"]
    assert (2, expected, True) in server.frames[before:]



@pytest.fixture(scope="module")
def bend_ws_owned_request():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_owned_request.bend", root / "build/ws-owned-request", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_owned_segments_flatten_for_binary_request(
        bend_ws_owned_request, ws, http, tmp_path, threads):
    import os
    import subprocess
    _, url, ca = ws
    expected = bytes((i * 37 + 11) & 255 for i in range(300000))
    payload = tmp_path / "owned-request.bin"
    payload.write_bytes(expected)
    out = subprocess.run([str(bend_ws_owned_request), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_HTTP_URL": http,
        "SCRAPANIUM_TEST_CA": ca, "SCRAPANIUM_TEST_EXPECTED": str(payload)},
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["owned-binary-request-ok"]



@pytest.fixture(scope="module")
def bend_ws_owned_cancel():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_owned_cancel.bend", root / "build/ws-owned-cancel", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_owned_receive_cancel_releases_partial_segments(
        bend_ws_owned_cancel, ws, threads):
    import os
    import subprocess
    _, url, ca = ws
    out = subprocess.run([str(bend_ws_owned_cancel), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca},
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["owned-cancel-ok"]



@pytest.fixture(scope="module")
def bend_ws_owned_empty():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_owned_empty.bend", root / "build/ws-owned-empty", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bend_owned_empty_message(bend_ws_owned_empty, ws, threads):
    import os
    import subprocess
    _, url, ca = ws
    out = subprocess.run([str(bend_ws_owned_empty), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca},
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["owned-empty-ok"]



def test_owned_chunks_survive_easy_cleanup_and_cross_thread_release(ws, tmp_path):
    import os
    import subprocess
    from pathlib import Path
    _, url, ca = ws
    root = Path(__file__).resolve().parents[1]
    prefix = Path(os.environ["SCRAPANIUM_CURL_DIR"]).resolve()
    library = prefix / "lib" if (prefix / "lib/libcurl-impersonate.so").exists() else prefix
    binary = tmp_path / "websocket-owned-release"
    compile_command = ["/usr/bin/clang-21", "-std=c11", "-O1", "-g", "-fno-omit-frame-pointer",
        "-fsanitize=address,undefined", "-I" + str(prefix / "include"),
        "-I" + str(root / "native"), str(root / "tests/websocket_owned_release.c"),
        str(root / "native/scrapanium.c"), "-lpthread", "-lm", "-ldl", "-L" + str(library),
        "-Wl,-rpath," + str(library), "-lcurl-impersonate", "-o", str(binary)]
    subprocess.run(compile_command, check=True, capture_output=True, text=True)
    env = {**os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1",
           "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1",
           "LD_LIBRARY_PATH": str(library) + (":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else "")}
    out = subprocess.run([str(binary), url + "/owned-segments", ca],
        env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["owned-payload-worker-release-ok"]




@pytest.fixture(scope="module")
def bend_ws_length_boundaries():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from build import build
    return build(root / "tests/websocket_length_boundaries.bend",
                 root / "build/ws-length-boundaries", True)


@pytest.mark.parametrize("case,path,valid,size", [
    ("valid-126", "canonical126-segmented", True, 126),
    ("valid-65536", "canonical65536-segmented", True, 65536),
    ("short-16", "noncanonical16-125", False, 125),
    ("short-64", "noncanonical64-65535", False, 65535),
    ("high-bit", "highbit-length", False, 0),
])
def test_native_segmented_rfc6455_length_boundaries(ws, tmp_path, case, path, valid, size):
    _, url, ca = ws
    expected = bytes((i * 29 + 17) & 255 for i in range(size))
    with WebSocket(url + "/" + path, ca_bundle=ca) as socket:
        assert socket.error == 0
        result = socket.recv(timeout=5000)
        if valid:
            assert result == (0, 2, expected), case
        else:
            assert result[0] == 4, (case, result)


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("case,path,valid,size", [
    ("valid-126", "canonical126-segmented", True, 126),
    ("valid-65536", "canonical65536-segmented", True, 65536),
    ("short-16", "noncanonical16-125", False, 125),
    ("short-64", "noncanonical64-65535", False, 65535),
    ("high-bit", "highbit-length", False, 0),
])
def test_bend_segmented_rfc6455_length_boundaries(
        bend_ws_length_boundaries, ws, tmp_path, threads, case, path, valid, size):
    import os
    import subprocess
    _, url, ca = ws
    expected = tmp_path / (case + ".bin")
    expected.write_bytes(bytes((i * 29 + 17) & 255 for i in range(size)))
    out = subprocess.run([str(bend_ws_length_boundaries), "--threads", str(threads)], env={**os.environ,
        "SCRAPANIUM_TEST_URL": url + "/" + path, "SCRAPANIUM_TEST_CA": ca,
        "SCRAPANIUM_TEST_PATH": str(expected), "SCRAPANIUM_TEST_VALID": "yes" if valid else "no"},
        capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["accepted-exact" if valid else "rejected-noncanonical"]
