# WSS client profiler feasibility

This is a profiler/symbol-resolution feasibility record, not a throughput benchmark or a client CPU attribution result. The profile is deliberately retained with its harness failures and limits.

One existing Bend streaming client ran the exact-check workload of 1,024 binary messages × 65,536 payload bytes (64 MiB total), Chrome 146 profile, flush batch 1, over local WSS. The client returned zero after checking each message's binary opcode, ordered sequence tag, complete bytes, and buffer release. The backend mapped in the process was stock `libcurl-impersonate.so.4.8.0`, SHA-256 `bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3`.

`perf record` used user-only `cpu-clock:u`, 999 Hz, DWARF call chains with a 16 KiB stack, and `CLOCK_MONOTONIC`. Events were enabled and acknowledged immediately before the existing start gate. One session was captured; it is not a repeated profile or a performance comparison. The raw file contains 72 samples and `perf report` reports zero lost samples. The decoded stacks resolve `curl_ws_recv`, `Curl_easy_recv`, `SSL_read`, `ssl_read_impl`, TLS record opening, AES-GCM, `sp_ws_io_advance`, and `sp_ws_append`. This confirms the user-local tool can resolve the actual curl/TLS/client path.

The evidence has material limits. The existing Bend executable contains ASan symbols and the exact build recipe that produced that binary was not recorded, so the retrospective source inventory and hashes in manifest.json do not establish its build provenance. The profile includes samples after the client timer through close/print; it was not filtered to the exact client interval. The profile process exited naturally with the client before the requested disable acknowledgment. The checked client returned zero, but the harness did not reach its subsequent peer-stats query, so the Go peer's frame totals are not retained. No CPU shares, bottleneck percentages, or performance conclusions should be drawn from this small ASan profile.

The first harness attempt stopped after readiness because it imported `mapped_backend` from the wrong module; it captured no samples. The corrected attempt produced the retained profile, then raised on the post-exit disable acknowledgment. `capture_attempt.py` is the historical one-off script with those limitations and an output-cleanup step; it is retained for provenance only and must not be run as a reproduction command.

The local-only perf package was extracted under `/home/baidu`; no package, client, backend binary, or TLS private key is included here. The package SHA-256 is `a32aa4f85e70546a1cc7b8ecb50aacc78c2bfb83b9589a2909d43f87326371ce`; the perf executable SHA-256 is `94b3ed36eeedf585d88fedd326fb616f662223193dddbf6d48d2df1cced69009`, version 7.0.14. The one-off capture command was:

```text
perf record --delay=-1 --event=cpu-clock:u --freq=999 --call-graph=dwarf,16384 --clockid=monotonic --control=fifo:/home/baidu/wss-perf-feasibility-20260923/wss-actual/run/perf.ctl,/home/baidu/wss-perf-feasibility-20260923/wss-actual/run/perf.ack -p 758 -o /home/baidu/wss-perf-feasibility-20260923/wss-actual/run/perf.data
```

## Evidence files

- [`perf.data`](perf.data), SHA-256 `11198d43f58dbe000a4a65705278432a4f8cb713c5abc62a5bb6bfa61017f037`.
- [`perf.report.txt`](perf.report.txt), decoded call-chain report, SHA-256 `331e3f6a409df113fb2c08df0ab22a721e845160e19291a95a1ad47c9202440f`.
- [`perf.script-ns.txt`](perf.script-ns.txt), 72 monotonic-timestamped samples, SHA-256 `7094023421d2fe9b6c63f98aa13cf84cd14d85b548419468c5fd58b04f5010f1`.
- [`capture_attempt.py`](capture_attempt.py), historical one-off source, SHA-256 `493cc8f6efbeaabd137990aecf93ec027a98b0e136a0a0d08a7156a7dd488db2`.
- [`capture_failures.txt`](capture_failures.txt) retains the setup and shutdown errors.
- [`manifest.json`](manifest.json) records event settings, hashes, and limitations.

## Bounded next diagnostic proposal — not run

Use freshly rebuilt, verified **unsanitized** Bend and matched `curl_cffi` clients against one frozen stock backend and the same exact-check Go peer. Test four cells: the two clients at flush batches 1 and 64, each with 1,024 × 65,536-byte ordered binary messages, Chrome 146, complete opcode/byte/sequence checks, and immediate release. Preplan 96 repeats per cell with sampling enabled and 96 matched repeats with the perf event disabled (768 positive sessions total), plus 16 corrupt/swap negative controls (four client × batch cells, two profiler modes, and two faults). Use 99 Hz `cpu-clock:u`, the same 16 KiB DWARF stack, and monotonic timestamps; attach only after readiness, bracket the start gate, then filter by client PID/TID to the recorded client `[start,end)` timer interval. Do not include Go peer, setup, TLS upgrade, warmup, or close samples in client attribution.

Set the minimum at 80 valid user samples per cell before collecting data. The 96-repeat count is fixed before inspection; a cell below the floor remains inconclusive and is not extended. At `perf_event_paranoid=2`, user-only CPU samples omit kernel CPU and all blocked time, so this floor has underpower risk. Report flat leaf samples separately from overlapping inclusive call chains; never sum inclusive percentages. The enabled-versus-disabled comparison estimates the incremental sampling/unwind observer effect relative to an attached but disabled recorder, not relative to no recorder. Do not use this attribution profile as a throughput claim.