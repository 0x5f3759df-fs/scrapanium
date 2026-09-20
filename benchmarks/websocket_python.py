"""Real curl_cffi client, with exact byte and binary-opcode checks in timing."""
import os
from pathlib import Path
import sys
import time
from curl_cffi import CurlWsFlag, requests

with requests.Session(impersonate="chrome146", verify=os.environ["SCRAPANIUM_BENCH_CA"], trust_env=False) as session:
    socket = session.ws_connect(os.environ["SCRAPANIUM_BENCH_URL"])
    data = Path(os.environ["SCRAPANIUM_BENCH_PAYLOAD"]).read_bytes()

    def exchange():
        socket.send(data, CurlWsFlag.BINARY, timeout=5)
        actual, kind = socket.recv(timeout=5)
        if actual != data or not kind & CurlWsFlag.BINARY:
            raise RuntimeError("WebSocket payload or opcode mismatch")

    exchange()
    print("ready", flush=True)
    if sys.stdin.buffer.read(1) != b"x":
        raise RuntimeError("missing benchmark start signal")
    start = time.monotonic_ns()
    for _ in range(int(os.environ["SCRAPANIUM_BENCH_COUNT"])):
        exchange()
    end = time.monotonic_ns()
    socket.terminate()
    print(start / 1_000_000)
    print(end / 1_000_000)
