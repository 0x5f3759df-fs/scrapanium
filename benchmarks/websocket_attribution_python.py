"""Matched curl_cffi client for the WSS attribution diagnostic."""
import ctypes
import os
from collections import deque
from pathlib import Path
import sys

from curl_cffi import CurlWsFlag, requests


count = int(os.environ["SCRAPANIUM_BENCH_COUNT"])
body = Path(os.environ["SCRAPANIUM_BENCH_BODY"]).read_bytes()
messages = deque(f"{index:08d}".encode() + body for index in range(count))
del body
mode = os.environ["SCRAPANIUM_ATTRIBUTION_MODE"]
if mode not in ("control", "attribution"):
    raise RuntimeError("invalid diagnostic instrumentation mode")
attribution = mode == "attribution"

# Resolve shim calls before connecting and warming up, outside the measured
# client interval. The same total-boundary calls are used in both modes.
shim = ctypes.CDLL(None)
begin = shim.wsattr_begin
begin.argtypes = (ctypes.c_int,)
begin.restype = ctypes.c_int
end = shim.wsattr_end
end.argtypes = ()
end.restype = ctypes.c_uint64

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

    if begin(int(attribution)) != 1:
        raise RuntimeError("cannot start diagnostic attribution interval")
    socket.send(b"start", CurlWsFlag.BINARY, timeout=5)
    while messages:
        expected = messages.popleft()
        actual, kind = socket.recv(timeout=5)
        valid_kind = bool(kind & CurlWsFlag.BINARY)
        valid_payload = actual == expected
        del actual, expected
        if not (valid_kind and valid_payload):
            raise RuntimeError("WebSocket opcode, sequence, or payload mismatch")

    elapsed = end()
    if not elapsed:
        raise RuntimeError("cannot stop diagnostic attribution interval")
    socket.terminate()

print(f"client_interval_wall_ns={elapsed}")
