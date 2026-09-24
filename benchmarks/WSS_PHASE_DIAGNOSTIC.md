# WSS phase diagnostic

This diagnostic is intended to explain where time is spent in the existing
inbound WebSocket workload. It complements the throughput benchmark; it does
not replace or revise historical measurements.

Each positive sample uses one verified WSS connection and 1,024 ordered binary
messages of 64 KiB each (64 MiB total). The Bend client and matched
`curl_cffi` client use the same `chrome146` profile and mapped curl backend.
The peer sends either one frame or 64 frames per TLS `Write` call. Both clients
check the binary opcode, sequence tag and every payload byte, then release the
expected and received buffers immediately inside the timed loop. Corpus
preparation, connection setup, TLS upgrade, warmup and close are outside the
client interval.

The diagnostic has a total-only control mode and a phase mode. Both retain the
same total interval and receive/check/release work. Phase mode adds per-message
clock reads; comparing paired totals estimates the effect of those client-side
phase clocks within this diagnostic loop.

`total_elapsed_ns` starts immediately before the client sends the `start`
control and ends after the final message has passed validation and both buffers
have been released. `start_monotonic_us` and `end_monotonic_us` expose the
client boundaries for independent parent-clock checks; Bend reports
microsecond-resolution boundaries and the curl client truncates nanoseconds to
microseconds. `receive_assembly_ns` is the sum of wall intervals around
the WebSocket receive API calls. It includes time blocked waiting for data,
TLS delivery, parsing, frame assembly and receive-side allocation. It does not
separate those costs. `validation_release_ns` covers opcode and sequence checks,
full byte equality, and immediate release of both buffers. `residual_ns` is the
total minus those two sums; it includes control sending, loop work, timer
boundaries and gaps, so it is not a pure overhead measurement.

The peer record reports `send_interval_wall_ns` from after it decodes the
`start` control through the final TLS `Write`, plus the sum of wall durations
around each `tls.Conn.Write` in `tls_write_wall_ns_sum`. The peer interval does
not exactly match the client interval: the client timer starts before sending
the control, and peer `Write` completion can precede the client's final check.
`data_plaintext_bytes_written` counts plaintext frame bytes accepted by
`tls.Conn.Write`, before TLS encryption; it is not encrypted network traffic.
Frame and payload counters describe only complete successful frames. The peer
process user/system CPU deltas use `RUSAGE_SELF` calls around the send interval.
They include all activity in the peer process and
small sampling overhead, not just the handler goroutine.

Interpret peer CPU and `Write` wall time together. Long `Write` wall time can
reflect TLS work, blocking, scheduling or backpressure; process CPU is
process-wide. Neither counter alone establishes peer saturation or proves its
cause. The peer-side instrumentation is common to phase and control client
samples, so its observer effect is not separately estimated. A record marked
`completed` has finalized metrics; successful positive samples also require no
peer error and exact expected frame/write/byte counts. Negative controls retain
their observed partial counts and errors where a client aborts early.

Run the sanitizer-backed correctness smoke from the repository root:

```sh
.deps/venv-matched/bin/python benchmarks/websocket_phase_diagnostic.py \
  --smoke --sanitize --output-dir build/ws-phase-smoke
```

The smoke uses eight messages per positive condition and is correctness-only.
Each full positive sample uses 1,024 messages (64 MiB). The retained run below
has 96 positive samples and 16 negative controls.

## Retained 64 KiB run

The run was captured on 2026-09-24 under WSL2 on an i5-13420H. All 112 planned
records are present in order. The 96 positive records cover 12 repeats of four
client/mode conditions at each flush size; the 16 corruption/swap controls all
failed on the expected mismatch. Every client process mapped the same
`libcurl-impersonate` backend (`bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3`),
and all positive peer records negotiated TLS 1.3 with cipher `TLS_AES_128_GCM_SHA256`.
The hashed source files, built binaries, Bun/compiler script, mapped backend
and matched `curl_cffi` files were rechecked against available local artifacts;
Clang and Go versions are recorded in the manifest. The retained `samples.jsonl`
SHA-256 is
`e0d84cfc3575cf0146bd16989c79e58545f9d394aa045108c0631e7bb195a7de`.

For each client and flush size, phase mode has 12 samples. Each cell below is
the median of those per-repeat fractions; brackets are unadjusted 95% percentile
bootstrap intervals from 20,000 repeat-level resamples (seed `20260923`). These
are shares of the client interval, not throughput comparisons.

| Flush | Client | Receive/assembly | Validation/release | Residual |
| --- | --- | ---: | ---: | ---: |
| 1 | Bend | 83.3% (82.6–83.6%) | 15.2% (14.9–15.9%) | 1.5% (1.4–1.5%) |
| 64 | Bend | 81.8% (81.3–82.8%) | 16.8% (15.8–17.2%) | 1.6% (1.4–1.7%) |
| 1 | curl_cffi | 87.4% (87.1–88.0%) | 11.0% (10.5–11.4%) | 1.6% (1.5–1.6%) |
| 64 | curl_cffi | 87.2% (86.7–87.7%) | 11.2% (10.5–11.6%) | 1.7% (1.6–1.8%) |

The observer-effect table compares phase-mode and control-mode totals for the
same client and repeat. Each ratio is phase/control; it is not a Bend/curl_cffi
speedup. Entries are medians of 12 paired repeat ratios with unadjusted 95%
percentile bootstrap intervals from 20,000 repeat-level resamples (seed
`20260923`). Every interval includes 1, so this run does not resolve the effect
of the added client phase-clock calls; it also does not establish equivalence or
zero observer cost.

| Flush | Client | Phase/control total ratio (median, 95% interval) | Median paired total difference |
| --- | --- | ---: | ---: |
| 1 | Bend | 1.005 (0.970–1.127) | +0.156 ms |
| 64 | Bend | 0.973 (0.898–1.078) | −0.853 ms |
| 1 | curl_cffi | 1.072 (0.954–1.110) | +2.791 ms |
| 64 | curl_cffi | 1.003 (0.897–1.067) | +0.115 ms |

The peer summary pools all four client/mode conditions for each flush size
(48 positive records per row group), so the following are descriptive medians,
not independent-repeat confidence intervals. Each peer sent exactly 64 MiB of
payload: flush 1 used 1,024 TLS writes per sample and flush 64 used 16.

| Flush | Peer send interval median | TLS `Write` wall-sum median | `Write` sum / interval median | Process user/system CPU medians | Process CPU / interval median |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 36.59 ms | 36.53 ms | 99.85% | 18.54 / 10.04 ms | 0.772 |
| 64 | 36.91 ms | 36.90 ms | 99.99% | 17.44 / 11.89 ms | 0.820 |

The client receive/assembly bucket is largest, at about 82–87% of phase-mode
client time. It combines blocking, TLS/socket delivery, parsing, frame assembly
and library allocation, so it does not isolate native transport work. The peer
`Write` wall sum nearly spans its send interval, while process CPU is measured
across the entire peer process; those counters cannot separate TLS computation,
blocking, scheduling or backpressure, and neither proves saturation or its
cause. Client and peer intervals also have different start/end boundaries. A
useful next diagnostic would separate time waiting for receive data from native
parsing/assembly. These measurements alone do not justify optimizing byte
comparison, allocation or a specific transport subpath.

Raw evidence: [manifest](results/websocket-phase-diagnostic-20260924/manifest.json)
and [all samples](results/websocket-phase-diagnostic-20260924/samples.jsonl).

## Reproduce

The full run uses the default 12 repeats and 1,024 messages per positive sample.
Use a new output directory; the runner refuses to overwrite one that exists.

```sh
artifact_dir="benchmarks/results/websocket-phase-diagnostic-$(date -u +%Y%m%dT%H%M%SZ)"
.deps/venv-matched/bin/python benchmarks/websocket_phase_diagnostic.py \
  --output-dir "$artifact_dir"
```

The eight-message correctness smoke is not performance evidence. The CI
sanitizer invocation is listed above.
