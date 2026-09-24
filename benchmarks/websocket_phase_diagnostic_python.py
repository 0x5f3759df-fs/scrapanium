"""Matched curl_cffi client for the isolated WebSocket phase diagnostic."""
import os
from collections import deque
from pathlib import Path
import sys
import time

from curl_cffi import CurlWsFlag, requests


count = int(os.environ["SCRAPANIUM_BENCH_COUNT"])
body = Path(os.environ["SCRAPANIUM_BENCH_BODY"]).read_bytes()
messages = deque(f"{index:08d}".encode() + body for index in range(count))
del body
mode = os.environ["SCRAPANIUM_BENCH_PHASE_MODE"]
if mode not in ("control", "phase"):
    raise RuntimeError("invalid diagnostic phase mode")
phase = mode == "phase"

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

    receive_assembly_ns = 0
    validation_release_ns = 0
    start_ns = time.monotonic_ns()
    socket.send(b"start", CurlWsFlag.BINARY, timeout=5)
    while messages:
        expected = messages.popleft()
        if phase:
            receive_start_ns = time.monotonic_ns()
        actual, kind = socket.recv(timeout=5)
        if phase:
            receive_assembly_ns += time.monotonic_ns() - receive_start_ns

        if phase:
            validation_start_ns = time.monotonic_ns()
        valid_kind = bool(kind & CurlWsFlag.BINARY)
        valid_payload = actual == expected
        del actual, expected
        if phase:
            validation_release_ns += time.monotonic_ns() - validation_start_ns
        if not (valid_kind and valid_payload):
            raise RuntimeError("WebSocket opcode, sequence, or payload mismatch")

    end_ns = time.monotonic_ns()
    socket.terminate()

print(f"total_elapsed_ns={end_ns - start_ns}")
print(f"receive_assembly_ns={receive_assembly_ns}")
print(f"validation_release_ns={validation_release_ns}")
print(f"start_monotonic_us={start_ns // 1000}")
print(f"end_monotonic_us={end_ns // 1000}")
