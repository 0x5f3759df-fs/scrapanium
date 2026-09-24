"""Fresh, uninstrumented matched curl_cffi client for the perf diagnostic."""
import os
from collections import deque
from pathlib import Path
import sys
import time

from curl_cffi import CurlWsFlag, requests


count_text = os.environ["SCRAPANIUM_BENCH_COUNT"]
if not count_text.isdecimal() or not count_text:
    raise RuntimeError("invalid WebSocket message count")
count = int(count_text)
if count < 1 or count > 1024:
    raise RuntimeError("WebSocket message count is outside the diagnostic bound")
body = Path(os.environ["SCRAPANIUM_BENCH_BODY"]).read_bytes()
if len(body) > 65528:
    raise RuntimeError("WebSocket message body is too large")
messages = deque(f"{index:08d}".encode("ascii") + body for index in range(count))
del body

with requests.Session(impersonate="chrome146", verify=os.environ["SCRAPANIUM_BENCH_CA"],
                      trust_env=False) as session:
    socket = session.ws_connect(os.environ["SCRAPANIUM_BENCH_URL"])
    socket.send(b"warmup", CurlWsFlag.BINARY, timeout=5)
    warm_actual, warm_kind = socket.recv(timeout=5)
    warm_valid = warm_actual == b"warmup" and bool(warm_kind & CurlWsFlag.BINARY)
    del warm_actual
    if not warm_valid:
        raise RuntimeError("WebSocket warmup mismatch")

    print("ready", flush=True)
    if sys.stdin.buffer.read(1) != b"x":
        raise RuntimeError("missing diagnostic start signal")

    start_ns = time.monotonic_ns()
    socket.send(b"start", CurlWsFlag.BINARY, timeout=5)
    while messages:
        expected = messages.popleft()
        actual, kind = socket.recv(timeout=5)
        valid_kind = bool(kind & CurlWsFlag.BINARY)
        valid_payload = actual == expected
        del actual, expected
        if not (valid_kind and valid_payload):
            raise RuntimeError("WebSocket opcode, sequence, or payload mismatch")
    end_ns = time.monotonic_ns()
    if end_ns <= start_ns:
        raise RuntimeError("invalid monotonic WebSocket interval")
    socket.terminate()

print(f"WSS_PERF_INTERVAL_NS={start_ns},{end_ns}")
print(f"client_interval_elapsed_us={(end_ns - start_ns) // 1000}")
