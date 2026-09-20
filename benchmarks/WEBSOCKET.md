# WebSocket benchmark

2026-09-20T01:35:22.977773+00:00 · Linux/WSL2 · 5 shuffled runs × 1,000 warm WSS round trips.

Chrome 146, same curl-impersonate 2.2.3, verified loopback TLS, 30-byte binary payload. Startup, TLS/HTTP upgrade and close excluded.

| Client | Median ms | Round trips/s |
| --- | ---: | ---: |
| scrapanium-bend | 167.00 | 5988 |
| curl_cffi-matched | 165.43 | 6045 |

These results cover one small-message loopback workload. They do not establish WAN throughput or concurrent-connection scaling. Bend uses millisecond timing; Python additionally compares each returned payload. Correctness is tested separately with exact bytes, fragmentation and large frames.

[Raw runs, source hashes and pins](results/websocket.json). Reproduce with `.deps/venv-matched/bin/python benchmarks/websocket.py`.
