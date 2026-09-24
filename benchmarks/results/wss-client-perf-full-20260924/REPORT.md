# WSS client CPU profile diagnostic

This fixed run profiles user-mode client CPU during exact-checked 64 KiB WSS
receives. It is an attribution diagnostic, not a throughput comparison or a
measurement of peer CPU, kernel networking, or socket wait time. No production
performance claim follows from the sample counts below.

## What the retained profiles show

All four profiled client/flush cells passed the predeclared floor of 80
selected samples across 96 sessions. The sample unit is one 99 Hz `cpu-clock:u`
event sample filtered to the client PID/TIDs and its half-open monotonic client
interval. The count floor includes unresolved and unknown frames.

| Client | Flush | Selected samples | Fully resolved stacks | Unresolved stacks | Missing stacks | Unknown leaf symbols |
|---|---:|---:|---:|---:|---:|---:|
| Bend | 1 | 134 | 78 | 56 | 0 | 48 |
| Bend | 64 | 130 | 88 | 42 | 0 | 36 |
| Matched `curl_cffi` | 1 | 215 | 2 | 213 | 0 | 71 |
| Matched `curl_cffi` | 64 | 220 | 0 | 220 | 0 | 91 |

The largest flat leaf groups retain DSO identity. `[inline DSO omitted]` means
the perf text identifies the inline symbol but emits no DSO on that frame;
`[unknown]` is an unresolved symbol, not a function name. `libc.so.6` below is
`/usr/lib/x86_64-linux-gnu/libc.so.6`; the Python executable is
`/usr/bin/python3.14`.

| Client / flush | Largest flat leaves by `(DSO, symbol)` |
|---|---|
| Bend / 1 | `libc.so.6 / [unknown]` 48; stock curl DSO / `aes_gcm_dec_update_vaes_avx2` 29; `[inline DSO omitted] / copyBlocks` 26 |
| Bend / 64 | `libc.so.6 / [unknown]` 36; stock curl DSO / `aes_gcm_dec_update_vaes_avx2` 30; `[inline DSO omitted] / copyBlocks` 27 |
| `curl_cffi` / 1 | `libc.so.6 / [unknown]` 62; stock curl DSO / `aes_gcm_dec_update_vaes_avx2` 50; Python executable / `_PyEval_EvalFrameDefault` 24 |
| `curl_cffi` / 64 | `libc.so.6 / [unknown]` 67; stock curl DSO / `aes_gcm_dec_update_vaes_avx2` 41; `[inline DSO omitted] / copyBlocks` 20 |

The stock curl DSO is the pinned
`.deps/curl/libcurl-impersonate.so.4.8.0` (SHA-256
`bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3`). In the
Bend profiles, 84 samples have an unresolved libc leaf; 64 of those have
`sp_bend_bytes_equal` as their immediately preceding named frame (34 at flush 1,
30 at flush 64). This places many unresolved leaves in the required full-byte
comparison path, but does not identify the libc routine or its exclusive CPU
cost.

The 53 Bend `copyBlocks` leaf samples have `DSO=null` on the inline frame. A
retained example, `positive-0010-flush1-repeat003-position2`, shows
`copyBlocks → copyBlocksAlignedSource → copyForwards → memcpyFast →
OPENSSL_memcpy → SSL_peek → SSL_read → ossl_recv → ssl_cf_recv → Curl_easy_recv
→ Curl_bufq_sipn → Curl_bufq_slurp → curl_ws_recv → sp_ws_io_advance`.
The following non-inline SSL/curl frames map to the pinned curl DSO; the raw
inline DSO remains unspecified. This is observed copy activity in the TLS read
path, not proof of its exact implementation or the latency it causes.

The following counts are observed inclusive stack memberships. A sample may
appear in several columns; incomplete stacks make these lower bounds on the
named paths, and the columns must not be added or read as CPU percentages.
The Bend equality-helper column counts only observed `sp_bend_bytes_equal`
frames. Python uses a different exact-byte comparison path, so it is marked
not applicable rather than treating generic `bcmp` frames as proof of the
payload check. Both clients retain full-payload validation.

| Client / flush | TLS AEAD/crypto | TLS record read | curl WebSocket receive | Copy/memcpy path | Bend equality helper |
|---|---:|---:|---:|---:|---:|
| Bend / 1 | 29 | 59 | 84 | 26 | 34 |
| Bend / 64 | 34 | 72 | 90 | 28 | 30 |
| `curl_cffi` / 1 | 50 | 84 | 113 | 8 | N/A |
| `curl_cffi` / 64 | 46 | 71 | 100 | 20 | N/A |

The categories intentionally overlap. These small sample sets are descriptive:
the profile does not quantify an exclusive cost split between crypto, copying,
parsing, comparison, and runtime work.

### Recorder observer check

Each cell has 96 paired repeats. The table reports the median of per-repeat
`profiled interval / disabled interval` ratios, with a paired repeat bootstrap
(20,000 draws, seed 20260924; percentile 95% intervals, no multiplicity
adjustment). The adjacent medians are marginal medians of intervals and need
not form the paired median ratio.

| Client / flush | Disabled median (ms) | Profiled median (ms) | Median paired ratio, profiled / disabled (nominal 95% interval) |
|---|---:|---:|---:|
| Bend / 1 | 28.307 | 28.378 | 0.9996 (0.9709–1.0233) |
| Bend / 64 | 27.409 | 26.875 | 1.0030 (0.9744–1.0226) |
| `curl_cffi` / 1 | 34.260 | 34.531 | 1.0063 (0.9734–1.0237) |
| `curl_cffi` / 64 | 33.340 | 33.524 | 1.0088 (0.9956–1.0250) |

These intervals all include 1. They leave the incremental sampling observer
effect unresolved; they do not establish equivalence or zero profiler cost.
Both conditions attach the same recorder, so shared recorder/harness overhead
is not measured by this comparison.

## Integrity and limits

The retained result has 784/784 scheduled attempts: 768 positives and 16
corruption/swap controls, with no failed rows. In each positive session, the
peer sent 1,024 binary frames and 67,108,864 payload bytes; peer counters
reported 1,024 frames, 67,119,104 framed plaintext bytes, and TLS identity
version/cipher 772/4865. Both clients retained exact opcode, sequence, and
full-payload checks and immediate release. All four sample floors passed. The
source inputs match Git blobs at
`061950a8041e6fdf9cbb9602b07bbab5291b6d24`; the manifest also records matching
before/after source, binary, binding, and tool hashes. The build uses the
repository's pinned stock RELEASE curl backend and Chrome 146 profile, not the
optional browser-controls backend used by the separate receive-coalescing
experiment.

The raw perf-script text yielded no in-window PID/TID/event mismatches. For
`curl_cffi`, 182 flush-1 and 188 flush-64 samples were outside the interval at
or after its end and were excluded; all other reported exclusion categories
were zero. Bend had no outside-window samples. The parser found no malformed
sample lines. It cannot observe every truncated DWARF unwind, so truncation is
unknown rather than zero. The run did not retain a complete raw perf-record
diagnostic stream: raw lost-record and throttle/unthrottle counts are
unavailable, not zero. The decoded perf report's total-lost-sample field was
zero where reported and unavailable in other rows.

`cpu-clock:u` excludes kernel and hypervisor samples. It cannot measure time
blocked in socket reads or peer work. The 99 Hz frequency and unresolved stack
frames also limit fine-grained attribution, especially for the Python client.
The full-byte equality checks are part of the benchmark contract and are not
removed by this diagnostic. These results do not validate a throughput gain or
support a production optimization claim.

## Reproduction and retained evidence

From the repository root, rerun the deterministic audit without rebuilding or
recollecting profiles:

```sh
python3 benchmarks/results/wss-client-perf-full-20260924/audit.py
```

The audit loads the schedule builder and parser from immutable Git blobs at the
recorded source commit, checks the exact 784-row plan and control order, hashes
all archive members, and re-parses each raw perf-script file to recompute
PID/TID/window selections, flat leaves, stack memberships, and paired observer
intervals. It writes `summary.json`.

Evidence files: [manifest](manifest.json), [fixed plan](plan.json), [raw row
JSONL](samples.jsonl), [recomputed summary](summary.json), [audit
script](audit.py), [lossless attempt archive](attempts.tar.gz), and the
[per-member SHA-256 inventory](attempt-members.sha256). The archive contains
all 12 raw files for each attempt (including `perf.data`, decoded text,
control/peer records, client logs, and the row sidecar); it excludes generated
executables, dependency packages, the message corpus, and private keys. Check
top-level files from the repository root with:

```sh
(cd benchmarks/results/wss-client-perf-full-20260924 && sha256sum -c SHA256SUMS.txt)
```

The audit script verifies archive members.

The retained ext4 collection was at
`/home/baidu/scrapanium-experiments/wss-client-perf-full-20260924` on the
collection host. That path is provenance only; the audit command operates on
the published result directory.
