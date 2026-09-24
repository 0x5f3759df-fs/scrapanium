# WSS receive attribution diagnostic

This diagnostic separates client receive work from Go peer activity for one
large-message workload. It is profiling evidence, not a throughput comparison.
The fixed run's raw output is retained in
[`results/websocket-attribution-diagnostic-20260924/`](results/websocket-attribution-diagnostic-20260924/).

## Measured findings

The retained run has 384 positive sessions (48 per client/mode/batch cell),
16 negative controls, and no validation errors. All 25 source hashes and the
profile/trace hashes match the manifest. The CPU profile contains 3,532 samples;
891 carry send-loop labels, with 105–118 samples in each of the eight cells,
above the predeclared floor of 80. Go's official `pprof -tags` marginals agree
with the manifest: 457 Bend and 434 matched-Python samples; 446 control and
445 attribution; 455 flush-1 and 436 flush-64. Profile samples are 10 ms each.

The table uses medians across 48 attribution-mode samples per row. The receive
share is receive-call thread CPU divided by caller-thread CPU. Wait wall is the
sum of the intercepted `poll`, `ppoll`, `select`, and `epoll_wait` intervals;
those intervals can overlap receive calls and are not subtracted from either
CPU or wall time. Peer CPU is process-wide user plus system CPU, not CPU specific
to the writer goroutine. Receive and wait shares are ratios of medians; peer
CPU/send-wall is the median of per-run ratios.

| Client | Frames per peer write | Caller CPU (ms) | `curl_ws_recv` CPU (ms) | Receive share | Supported wait wall / client wall | Receive calls per message | Peer CPU / send wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Bend | 1 | 29.39 | 21.65 | 73.7% | 2.7% | 5.02 | 0.84 |
| Bend | 64 | 28.40 | 20.74 | 73.0% | 3.2% | 5.04 | 0.83 |
| Matched `curl_cffi` | 1 | 37.42 | 21.75 | 58.1% | 0.5% | 5.03 | 0.69 |
| Matched `curl_cffi` | 64 | 36.54 | 20.81 | 56.9% | 0.6% | 5.03 | 0.67 |

The caller-CPU remainder after `curl_ws_recv` includes validation, assembly,
release, runtime, wrapper, and shim work; it is not a measurement of Bend
wrapper cost alone. The similar receive-call counts across clients and batches
make a narrowly scoped receive-coalescing experiment testable, but do not show
that call overhead is the cause. Test any changed backend with both Bend and
matched `curl_cffi`; this diagnostic does not predict whether it helps either
client. A candidate should drain only TLS plaintext
already available, stop at a would-block or frame boundary, and preserve the
same byte/opcode/sequence checks and buffer lifetimes. Keep the peer fixed and
accept the candidate only if it reduces calls and improves paired end-to-end
client intervals in a separate unprofiled confirmation run. These profiled
numbers do not predict the gain.

The paired `attribution/control` ratios below estimate the added per-call clock
reads in attribution mode. Each row has 48 pairs matched by repeat; intervals
are percentile 95% bootstrap intervals from 20,000 resamples of those repeat
pairs, seed 20260924. They are observer-effect ratios, not backend speed ratios.
The profiling peer and common shim instrumentation are active in both modes.

| Client | Frames per peer write | Client wall ratio [95% interval] | Caller CPU ratio [95% interval] |
| --- | ---: | ---: | ---: |
| Bend | 1 | 1.063 [1.037, 1.079] | 1.062 [1.050, 1.082] |
| Bend | 64 | 1.063 [0.997, 1.101] | 1.084 [1.047, 1.094] |
| Matched `curl_cffi` | 1 | 1.099 [1.081, 1.115] | 1.097 [1.079, 1.113] |
| Matched `curl_cffi` | 64 | 1.080 [1.045, 1.116] | 1.083 [1.054, 1.110] |

Some intervals exclude 1 and one wall interval includes it. This shows a
nonzero incremental clock-read cost in several cells; an interval containing 1
does not establish equivalence or zero observer cost.

The CPU profile labels only the peer's send loop. Of 4.57 seconds of Bend-
labeled samples, `main.writeFrames` accounts for 4.55 seconds cumulatively;
the matched-Python values are 4.34 and 4.31 seconds. In flat samples, the top
costs for Bend are Linux `Syscall6` (1.78 s), AES-GCM (1.47 s), and `memmove`
(0.83 s); for matched Python they are 1.63 s, 1.44 s, and 0.74 s. These are
profile costs inside the peer send path, not a decomposition of client time.
Across the 384 positive rows, peer process CPU sums to 9.39 s while send-loop
wall intervals sum to 12.04 s. That pairing is consistent with substantial
peer CPU work, but does not by itself establish a CPU ceiling or a cause for
end-to-end time.

The official Go trace `net` delay export attributes 3.11 s to `main.writeFrames`
across the full run. The export is not filtered by trace task/region; its
512.39 s total is dominated by listener `Accept` (421.25 s) and reads
(88.03 s), including control and idle periods. Treat it as aggregate stack
evidence only, not per-cell writer backpressure. Recreate the aggregate views
with `go tool pprof -sample_index=1 -tagfocus='client=scrapanium-bend' -top`
on `cpu.prof`, and `go tool trace -pprof=net trace.out`.

Raw evidence hashes: `manifest.json`
`f3dd331da36fdbbbc192e479f5e0a33531d99beece675d57c3965a261760a6a8`,
`samples.jsonl`
`023889311b117e50a7758ace64f002b07b051a34f7a7e4b64cd27ed94c92e128`,
`cpu.prof` `d4ec09fa00e4d23b7cf7cc159b3533eb27278a493c97e4463ec0beb2b8d08515`,
and `trace.out`
`774aab4efeac0d849879195353058f60b5e8d00695550f236533750a9136def8`.


## Fixed workload

The plan has 48 repeat blocks of eight cells: Bend and matched `curl_cffi`,
each in `control` and `attribution` mode, with one or 64 frames per peer write.
Every positive cell sends and checks 1,024 binary messages of 65,536 bytes.
Sixteen eight-message corruption and frame-swap controls cover every
client/mode/batch combination and run before profiling. The schedule balances
cell position within eight-repeat rotation blocks; it does not balance
carryover. The TLS peer, browser profile, mapped stock backend, corpus,
validation and buffer-release work are shared across the four client/mode cells.

The `control` client still uses the preload shim, common interval clocks and
receive/wait counters. `attribution` adds per-call wall and calling-thread CPU
clock reads around `curl_ws_recv` and the supported wait wrappers. Both modes
run while the same peer CPU profile and runtime trace are active, so their
paired difference estimates only the incremental client clock-read cost; it
does not isolate profiling or tracing overhead.

## What the counters mean

The client interval begins just before it sends the start control and ends
after every response's binary opcode, sequence and full payload have been
checked and both buffers released. Corpus preparation, TLS setup, warmup and
close are outside that interval. Client process CPU and caller-thread CPU are
separate; process CPU may include helper threads. The difference between
caller-thread CPU and measured `curl_ws_recv` CPU includes validation,
assembly, release, wrapper/runtime work and shim bookkeeping, so it is not
pure Bend wrapper CPU.

The shim counts every intercepted `curl_ws_recv` call and successful byte.
That function may return only the next available part of a frame, bounded by
the caller's buffer; a successful call does not imply a complete frame. The
shim also times `poll`, `ppoll`, `select` and `epoll_wait` in attribution mode.
These wait intervals may be nested within `curl_ws_recv`, and other wait APIs
are not covered. Do not add the receive and wait durations. Wall time minus
thread CPU is an off-CPU gap, not proof of socket blocking.

The peer's `send_interval_wall_ns` brackets the send loop after the start
control. `tls_write_wall_ns_sum` is the sum of the sequential wall intervals
around `tls.Conn.Write`; it includes TLS work and any blocking. The plaintext
byte counter is data accepted by `tls.Conn.Write`, not encrypted TCP bytes.
Peer user/system CPU comes from process-wide accounting and includes all peer
threads and clock-call overhead around the send interval; it is not writer
goroutine CPU. Neither process CPU nor write wall time alone identifies why a
send took that long.

Go CPU samples are labeled by client, mode and batch only inside the peer's
send-loop call. A cell below the predeclared 80-sample floor is inconclusive;
the run is not extended or repeated to chase that floor. The runtime trace
marks each send loop as a task/region. Trace-wide blocking includes other peer
activity, so only events associated with the send task/region can inform writer
waits. Profiling and tracing add observer cost; no instrumented duration is a
performance claim.

The raw [manifest](results/websocket-attribution-diagnostic-20260924/manifest.json)
records planned and retained IDs, source/compiler/binding/backend hashes,
profile and trace hashes, timing definitions and the fixed schedule. The
[sample log](results/websocket-attribution-diagnostic-20260924/samples.jsonl)
keeps each client and peer record, including failures. The raw [CPU profile](results/websocket-attribution-diagnostic-20260924/cpu.prof)
and [runtime trace](results/websocket-attribution-diagnostic-20260924/trace.out)
are retained for independent inspection.

## Reproduce

Bootstrap the pinned matched environment first, then choose a new, nonexistent
output directory. The runner refuses to overwrite retained evidence. This
fixed full diagnostic sends 24 GiB of positive payload in 384 checked sessions;
do not increase the schedule or rerun it based on observed sample counts.

```sh
unset CC BEND_SOURCE BUN LD_PRELOAD SCRAPANIUM_CURL_DIR LD_LIBRARY_PATH
artifact_dir="benchmarks/results/websocket-attribution-diagnostic-$(date -u +%Y%m%dT%H%M%SZ)"
.deps/venv-matched/bin/python benchmarks/websocket_attribution.py \
  --seed 20260924 --output-dir "$artifact_dir"
```

The correctness-only sanitizer smoke is the smaller CI check:

```sh
.deps/venv-matched/bin/python benchmarks/websocket_attribution.py \
  --smoke --sanitize --output-dir build/ws-attribution-smoke
```

It runs 16 negative controls and eight positive eight-message cells. Bend and
its embedded clock bridge are ASan/UBSan-instrumented; the preloaded Bend shim
is AddressSanitizer-instrumented. The matched Python executable and its shim
are not instrumented. Sanitizer smoke timings are not profiling or performance
evidence. See the official [Go 1.26 `runtime/pprof` API](https://pkg.go.dev/runtime/pprof@go1.26.0),
[Go 1.26 `runtime/trace` API](https://pkg.go.dev/runtime/trace@go1.26.0),
[libcurl `curl_ws_recv` reference](https://curl.se/libcurl/c/curl_ws_recv.html),
and [Clang AddressSanitizer documentation](https://clang.llvm.org/docs/AddressSanitizer.html).
