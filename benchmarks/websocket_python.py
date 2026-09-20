import os
import time
from curl_cffi import requests

with requests.Session(impersonate="chrome146", verify=os.environ["SCRAPANIUM_BENCH_CA"], trust_env=False) as session:
    socket = session.ws_connect(os.environ["SCRAPANIUM_BENCH_URL"])
    data = b"scrapanium-websocket-benchmark"
    def exchange():
        socket.send(data)
        assert socket.recv(timeout=5)[0] == data
    exchange()
    start = time.perf_counter()
    for _ in range(int(os.environ["SCRAPANIUM_BENCH_COUNT"])): exchange()
    end = time.perf_counter()
    socket.terminate()
    print((end - start) * 1000)
