import asyncio
import sys
import time
from curl_cffi import requests

url, mode, count, ca = sys.argv[1:]
count = int(count)
HEADERS = {"User-Agent": "scrapanium-benchmark", "Accept": "*/*", "Accept-Encoding": "gzip, deflate, br, zstd"}

def check(r):
    assert r.status_code == 200
    _ = r.content

async def concurrent():
    async with requests.AsyncSession(impersonate="chrome146", verify=ca or True,
                                     trust_env=False, max_clients=16, default_headers=False, headers=HEADERS) as session:
        check(await session.get(url))
        start = time.perf_counter()
        responses = await asyncio.gather(*(session.get(url) for _ in range(count)))
        for r in responses: check(r)
        return (time.perf_counter() - start) * 1000

if mode == "batch":
    print(asyncio.run(concurrent()))
else:
    with requests.Session(impersonate="chrome146", verify=ca or True, trust_env=False, default_headers=False, headers=HEADERS) as session:
        check(session.get(url))
        start = time.perf_counter()
        for _ in range(count): check(session.get(url))
        print((time.perf_counter() - start) * 1000)
