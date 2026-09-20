"""Ordered streaming check using real curl_cffi and a prebuilt payload corpus."""
import os
from collections import deque
from pathlib import Path
import sys
import time
from curl_cffi import CurlWsFlag, requests

body = Path(os.environ["SCRAPANIUM_BENCH_BODY"]).read_bytes()
messages = deque(f"{i:08d}".encode() + body for i in range(int(os.environ["SCRAPANIUM_BENCH_COUNT"])))
del body
with requests.Session(impersonate="chrome146", verify=os.environ["SCRAPANIUM_BENCH_CA"], trust_env=False) as session:
    socket = session.ws_connect(os.environ["SCRAPANIUM_BENCH_URL"])
    socket.send(b"warmup", CurlWsFlag.BINARY, timeout=5)
    if socket.recv(timeout=5)[0] != b"warmup":
        raise RuntimeError("warmup mismatch")
    print("ready", flush=True)
    if sys.stdin.buffer.read(1) != b"x":
        raise RuntimeError("missing benchmark start signal")
    start = time.monotonic_ns()
    socket.send(b"start", CurlWsFlag.BINARY, timeout=5)
    while messages:
        expected = messages.popleft()
        actual, kind = socket.recv(timeout=5)
        if actual != expected or not kind & CurlWsFlag.BINARY:
            raise RuntimeError("stream sequence or payload mismatch")
        # Match Bend's affine-buffer lifetime, including the final iteration.
        del actual, expected
    end = time.monotonic_ns()
    socket.terminate()
    print(start / 1_000_000)
    print(end / 1_000_000)
