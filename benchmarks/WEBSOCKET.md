# TLS WebSocket benchmarks

## Round trips

Seven shuffled repetitions of seven workloads, **98 timed samples**, on an
i5-13420H running Ubuntu 26.04 under WSL2. Both clients check the complete binary
payload and opcode inside timing, with verified TLS, Chrome 146 and the identical
curl-impersonate 2.2.3 shared library. Rates are warm round trips per second.

| Peer | Payload | Connections | Scrapanium / Bend | curl_cffi / Python | Ratio of medians | Paired ratio 95% interval |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Python | 30 B | 1 | 19,608 | 15,800 | 1.24× | 1.19–1.30× |
| Go | 30 B | 1 | 22,124 | 18,581 | 1.19× | 1.12–1.35× |
| Go | 1 KiB | 1 | 20,833 | 20,206 | 1.03× | 1.02–1.33× |
| Go | 64 KiB | 1 | 5,769 | 4,445 | 1.30× | 0.95–1.75× |
| Go | 30 B | 4 | 38,685 | 31,078 | 1.24× | 1.23–1.28× |
| Go | 1 KiB | 4 | 38,647 | 28,979 | 1.33× | 1.29–1.36× |
| Go | 64 KiB | 4 | 15,385 | 10,382 | 1.48× | 1.30–1.58× |

**The single-connection Go / 64 KiB case is inconclusive.** Its paired interval
crosses 1×. Ratios of the client medians and medians of the paired ratios are
different statistics; confidence intervals here describe the latter. Round-trip
advantages in the other measured cases are smaller than the strongest streaming
result below. These figures do not establish a large win for every workload.

## Method

- The original Python peer, 30-byte payload and 1,000 exchanges remain in the
  first row. Go adds an independent standard-library TLS peer. Go runs use
  5,000 / 2,000 / 300 exchanges per connection for 30 B / 1 KiB / 64 KiB.
- Startup, payload preparation, TLS/HTTP upgrade, one checked warmup and close
  are excluded. There is no compression or reconnect in the timed section.
- Both clients preload payloads. Bend reuses the validated echo as its next
  outgoing buffer and retains the expected buffer; Python reuses its bytes.
  Both fail on any length, byte or opcode mismatch. The peers additionally
  check frame counts, masking and complete binary frames.
- Four connections use four independent client processes with a common start
  gate. Aggregate duration is the latest completion minus the earliest start.
  This measures multiple processes, not one application's event-loop scaling.
- Bend uses millisecond monotonic timing; Python uses nanosecond monotonic
  timing. Client order is shuffled per repetition. All samples and min/max
  values are retained. Confidence intervals use 10,000 repeat-level bootstrap
  resamples of the median paired speed ratio; they are not packet-latency
  percentiles or guarantees on other machines.
- The harness records the actual mapped backend hash for every process,
  binding/compiler provenance, source and executable hashes, plus client CPU
  time and context switches. CPU accounting covers complete client processes,
  including setup and shutdown. Source and binary changes during a run abort
  the report.

The old 5,988 / 6,045 chart used asymmetric validation and per-round Bend payload
construction. It must not be used for a direct before/after speedup claim. The
current harness makes validation and payload preparation comparable.

## Reproduce

```sh
python3 scripts/bootstrap.py --matched
.deps/venv-matched/bin/python benchmarks/websocket.py
```

Run on an otherwise idle machine. The harness builds both executables before
timing and writes [all measurements and provenance](results/websocket.json).
The [first complete readiness matrix](results/websocket-readiness-baseline.json)
is retained separately. Protocol and ownership tests are documented in
[validation](../docs/VALIDATION.md); the state machine's
[invariants](../docs/WEBSOCKET_INTERNALS.md) describe what optimizations preserve.

## Inbound streaming

Rates below are inbound messages per second. The strongest measured gap is
**30 B with 64 frames per server write: 1.63M versus 694k messages/s**.
The ratio of client medians is **2.35×**; the median paired ratio is **2.32×**
with a 95% bootstrap interval of **2.30–2.38×**.

| Payload | Frames / server write | Scrapanium / Bend | curl_cffi / Python | Ratio of medians | Paired ratio 95% interval |
| --- | ---: | ---: | ---: | ---: | ---: |
| 30 B | 1 | 559,248 | 375,532 | 1.49× | 1.33–1.49× |
| 1 KiB | 1 | 458,063 | 308,289 | 1.49× | 1.40–1.85× |
| 64 KiB | 1 | 38,374 | 31,946 | 1.20× | 0.99–1.29× |
| 30 B | 64 | 1,632,808 | 693,963 | 2.35× | 2.30–2.38× |
| 1 KiB | 64 | 973,789 | 462,635 | 2.10× | 1.90–2.15× |
| 64 KiB | 64 | 39,219 | 32,018 | 1.22× | 0.85–1.36× |

Both **64 KiB streaming comparisons are inconclusive**: their paired intervals
include 1×. The 64 KiB / one-frame interval is 0.9936–1.2898× before rounding.
All seven samples per client are retained, including slower runs. Larger frames
have short measured durations and substantial run-to-run variation.

The separate [streaming report](results/websocket-streaming.json) measures
**messages received per second**, not echo round trips. An independent Go peer
sends unique, ordered binary messages over one verified TLS connection. Both
clients compare every byte and opcode, then release both received and expected
buffers inside timing. The last message receives the same treatment.

The six workloads use 30 B, 1 KiB and 64 KiB payloads, with either one or 64
distinct WebSocket frames per server TLS write. Both clients receive identical
framing and batching. Sequence tags detect missing, duplicate or reordered
messages; deliberate byte corruption and frame swaps must fail before positive
measurements begin. The peer independently checks frame and byte counts.

Expected payloads are prepared before timing, with at most 64 MiB of payload
per connection. Preparation time and process RSS at readiness are recorded.
Client CPU and context-switch accounting includes preparation and shutdown;
it is not receive-loop-only CPU. The benchmark uses monotonic microseconds in
Bend and nanoseconds in Python, with both intervals checked against independent
parent-process timestamps. Seven shuffled repeats retain every sample.

```sh
.deps/venv-matched/bin/python benchmarks/websocket_streaming.py
# Correctness gates; sanitizer results are never used as performance figures.
.deps/venv-matched/bin/python benchmarks/websocket_streaming.py --smoke --sanitize --runs 1 --threads 1
.deps/venv-matched/bin/python benchmarks/websocket_streaming.py --smoke --sanitize --runs 1 --threads 4
```

The [first streaming baseline](results/websocket-streaming-readiness-baseline.json)
is preserved, including its 64 KiB losses. That historical harness used
millisecond Bend timing and kept Python's expected corpus alive after timing,
while Bend freed each expected buffer during timing. Those differences make it
unsuitable for attributing a speedup solely to a product change. Later reports
use equivalent buffer lifetimes and finer timing. Absolute round-trip rates
also rose for curl_cffi between the historical and final matrices. Comparisons
across those runs cannot isolate a Scrapanium code change; use within-matrix
client comparisons or the controlled revision comparison below.

## Isolating the dispatch change

A separate [old/new Scrapanium comparison](results/websocket-dispatch-attribution.json)
holds the checked Bend workload, compiler and backend fixed while replacing
the WebSocket implementation and bridge. Five paired repetitions measured a
1.86× median speed ratio on the Go / 30 B case (95% bootstrap interval
1.48–2.01×), and 1.34× on the retained Python-peer case (1.10–1.75×).
This compares Scrapanium revisions, not Scrapanium against curl_cffi.

```sh
.deps/venv-matched/bin/python benchmarks/websocket_dispatch.py
```

The reproducer pins its before and after revisions, so subsequent receive-buffer
optimizations do not silently change that historical comparison.
