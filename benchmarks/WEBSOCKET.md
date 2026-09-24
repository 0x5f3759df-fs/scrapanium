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

## Receive-capacity experiment: rejected

A candidate increased direct reads from 16 KiB to at most 128 KiB of already
owned capacity, retaining the 64-step and 1 MiB dispatch limits. The
[complete three-client experiment](results/websocket-receive-capacity.json)
retains all 216 samples: 12 repetitions of six workloads for published
Scrapanium (`0704d51`), the candidate, and matched-backend curl_cffi. Each
client ordering occurs twice per workload; workload order is also shuffled.
All three clients rejected deliberate corruption and reordering before timing.

| Payload | Frames / server write | Candidate / published, paired median | 95% bootstrap interval |
| --- | ---: | ---: | ---: |
| 30 B | 1 | 1.019× | 0.978–1.201× |
| 1 KiB | 1 | 0.993× | 0.892–1.055× |
| 64 KiB | 1 | 1.087× | 0.971–1.287× |
| 30 B | 64 | 1.009× | 0.924–1.075× |
| 1 KiB | 64 | 0.973× | 0.761–1.021× |
| 64 KiB | 64 | 0.947× | 0.761–1.344× |

Every before/after interval includes 1×, so the patch was rejected as an
unproven throughput optimization. These nominal intervals describe this run;
they do not establish equivalence or rule out smaller effects. Duration
coefficients of variation were 17–44% across client/workload combinations,
which limits the experiment's ability to distinguish small changes.

The separate [instrumented mechanism pass](results/websocket-receive-capacity-probe.json)
explains why larger caller buffers did not reduce receive calls here. On each
64 KiB workload, all three clients made exactly **5,125 successful reads** for
1,024 messages plus the six-byte warmup. Every successful return was at most
16 KiB, even though curl_cffi offered 128 KiB and the candidate offered up to
61,988 bytes. Calls returning `CURLE_AGAIN` varied and are retained separately.
Instrumented timings are not used for speed claims. The proposed diff, source,
binary, compiler, and actual mapped-backend hashes remain in the reports.

The portable [experiment helper](websocket_receive_capacity.py) reproduces the
comparison from isolated baseline and candidate checkouts. This diagnostic
does not replace the README's independently published streaming matrix.

After the normal matched-backend bootstrap, run from the repository root:

```sh
ws_base=$(mktemp -d)
ws_candidate=$(mktemp -d)
git worktree add --detach "$ws_base" 0704d51c2275f26cd2580c95d7bb67f0655b1554
git worktree add --detach "$ws_candidate" 0704d51c2275f26cd2580c95d7bb67f0655b1554
ln -s "$PWD/.deps" "$ws_base/.deps"
ln -s "$PWD/.deps" "$ws_candidate/.deps"
for phase in prepare measure probe; do
  .deps/venv-matched/bin/python benchmarks/websocket_receive_capacity.py "$phase" \
    --baseline-root "$ws_base" --candidate-root "$ws_candidate" \
    --artifact-dir build/receive-capacity-reproduction
done
```

`prepare` reconstructs the rejected patch from the retained report and checks
every candidate source hash before compiling. A pre-existing manifest or output
is rejected to preserve earlier samples; use a fresh artifact directory to rerun.

## Per-operation state reuse comparison

This separate comparison measures the candidate that embeds WebSocket operation
state in its socket, removing the per-operation state-object `calloc` and
`free`. The measured candidate changes only `native/websocket.inc.c`; its SHA-256
is `0bc6766607ecfad0594d4a2df85b8247da678f4ab1cc0c9fca09504307082fb5`. The
baseline is `0704d51c2275f26cd2580c95d7bb67f0655b1554`. The matched `curl_cffi`
client is version `0.16.4b1`; every timed process mapped the same
`libcurl-impersonate` backend with SHA-256
`bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3`.

The [216-sample measurement](results/websocket-operation-reuse.json) contains
12 balanced repeats for each of six payload and frame-batching workloads across
baseline, candidate and `curl_cffi`; the separate
[18-sample instrumented probe](results/websocket-operation-reuse-probe.json)
retains one sample per client and workload. Both reference the same frozen
[provenance manifest](results/websocket-operation-reuse-manifest.json), SHA-256
`0522652351871bc9984c5bce9726782dfaf65c767c25baa5c6ad5c970a79f561`. The
measure and probe file hashes are `4518c543f45339607a5868d94585ff62236031e76eb40ec540c7b492b5f62ce4`
and `cfcbcf953fab1668ae5dd60bdfcca064635b459ba5e6025deb86702c62e07834`.

Each client compares every sequence-tagged message's bytes and binary opcode
inside the timed interval, then releases expected and actual buffers. A Go TLS
peer independently verifies frame and payload byte totals. The six corruption
and frame-swap controls failed in all three clients before measurement. Setup,
payload preparation, TLS upgrade, warmup and close are excluded from the timed
interval; source and binary hashes, mapped backend, clock bounds and peer totals
are retained in the reports. The probe report is instrumented and its timings
are not used in the table below.

Each cell is a paired throughput ratio: `A / B` is `B elapsed / A elapsed`, so
values above 1 mean A completed more messages per second. Intervals are the
2.5th and 97.5th percentiles of 10,000 repeat-level bootstrap medians (seed
`20260923`). The `baseline / curl_cffi` column is computed from the same retained
repeat-paired raw samples, separately from both candidate comparisons.

| Payload | Frames / server write | Candidate / baseline | Candidate / curl_cffi | Baseline / curl_cffi |
| --- | ---: | ---: | ---: | ---: |
| 30 B | 1 | 1.017× (0.979–1.170×) | 1.495× (1.415–1.625×) | 1.499× (1.383–1.592×) |
| 1 KiB | 1 | 0.981× (0.952–1.068×) | 1.425× (1.328–1.564×) | 1.402× (1.325–1.584×) |
| 64 KiB | 1 | 0.926× (0.832–1.038×) | 1.222× (0.959–1.524×) | 1.175× (1.125–1.501×) |
| 30 B | 64 | 1.003× (0.983–1.059×) | 2.350× (2.252–2.453×) | 2.351× (2.216–2.531×) |
| 1 KiB | 64 | 1.018× (0.982–1.100×) | 2.011× (1.832–2.196×) | 1.972× (1.569–2.088×) |
| 64 KiB | 64 | 1.115× (1.021–1.257×) | 1.267× (1.247–1.623×) | 1.136× (1.031–1.316×) |

Five of the six candidate/baseline intervals include 1×; the 64 KiB / one-frame
case has a median of 0.926× and interval 0.832–1.038×. Only the 64 KiB / 64-frame
candidate/baseline interval excludes 1× in this matrix. These are nominal
per-workload intervals without a multiple-comparison adjustment, so this single
result does not establish a broad throughput change. Intervals that include 1×
are inconclusive and do not establish equivalence. The existing README streaming
headline is based on its separate report, not this comparison.

To audit the retained reports without rebuilding or timing clients, run from
the repository root in WSL/Linux:

```sh
.deps/venv-matched/bin/python benchmarks/audit_websocket_operation_reuse.py
```

This checks report-internal schedule completeness and balance, paired sample
counts, finite timing/rate values, peer totals and errors, backend identities,
negative controls, receive histograms, source-hash differences and saved
bootstrap math. It does not reopen absolute source or binary paths from the
manifest; use the recorded hashes when those original local artifacts are
available.

To reconstruct and rerun the experiment, first bootstrap the matched backend as
for the other streaming benchmark, then use a new artifact directory. The
portable helper reconstructs the candidate from the published candidate
report, rather than from the current checkout:

```sh
unset CC BEND_SOURCE BUN
ws_base=$(mktemp -d)
ws_candidate=$(mktemp -d)
git worktree add --detach "$ws_base" 0704d51c2275f26cd2580c95d7bb67f0655b1554
git worktree add --detach "$ws_candidate" 0704d51c2275f26cd2580c95d7bb67f0655b1554
ln -s "$PWD/.deps" "$ws_base/.deps"
ln -s "$PWD/.deps" "$ws_candidate/.deps"
artifact_dir="build/wss-operation-reuse-$(date -u +%Y%m%dT%H%M%SZ)"
python=./.deps/venv-matched/bin/python

$python benchmarks/websocket_receive_capacity.py prepare \
  --baseline-root "$ws_base" --candidate-root "$ws_candidate" \
  --candidate-report benchmarks/results/websocket-operation-reuse.json \
  --artifact-dir "$artifact_dir"
$python benchmarks/websocket_receive_capacity.py measure \
  --baseline-root "$ws_base" --candidate-root "$ws_candidate" \
  --artifact-dir "$artifact_dir"
$python benchmarks/websocket_receive_capacity.py probe \
  --baseline-root "$ws_base" --candidate-root "$ws_candidate" \
  --artifact-dir "$artifact_dir"
$python benchmarks/audit_websocket_operation_reuse.py \
  --measure "$artifact_dir/measure.json" --probe "$artifact_dir/probe.json" \
  --manifest "$artifact_dir/manifest.json"
```

The audit helper validates report semantics and arithmetic; it does not rerun
the timed benchmark or treat the instrumented probe as performance evidence.

The separate [WSS phase diagnostic](WSS_PHASE_DIAGNOSTIC.md) reports receive,
validation and peer timing measurements. The [WSS receive attribution diagnostic](WSS_ATTRIBUTION_DIAGNOSTIC.md)
adds a labeled Go CPU profile and runtime trace for the 64 KiB workload. Both
retain their correctness controls and limits separately from the throughput
tables above.
