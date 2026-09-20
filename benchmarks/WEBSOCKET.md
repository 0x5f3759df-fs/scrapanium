# TLS WebSocket round trips

Seven shuffled repetitions of seven workloads, **98 timed samples**, on an
i5-13420H running Ubuntu 26.04 under WSL2. Both clients check the complete binary
payload and opcode inside timing, with verified TLS, Chrome 146 and the identical
curl-impersonate 2.2.3 shared library. Rates are warm round trips per second.

| Peer | Payload | Connections | Scrapanium / Bend | curl_cffi / Python | Ratio of medians | Paired ratio 95% interval |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Python | 30 B | 1 | 5,952 | 3,918 | 1.52× | 1.26–1.54× |
| Go | 30 B | 1 | 10,638 | 9,427 | 1.13× | 0.96–1.15× |
| Go | 1 KiB | 1 | 10,152 | 8,134 | 1.25× | 1.15–1.42× |
| Go | 64 KiB | 1 | 2,381 | 2,012 | 1.18× | 1.05–1.41× |
| Go | 30 B | 4 | 24,010 | 18,932 | 1.27× | 1.21–1.27× |
| Go | 1 KiB | 4 | 23,392 | 17,809 | 1.31× | 1.27–1.33× |
| Go | 64 KiB | 4 | 8,571 | 7,050 | 1.22× | 1.14–1.27× |

**The single-connection Go / 30 B case is inconclusive.** Its median paired ratio
is 0.99× and its interval crosses 1×. The ratio of the two client medians is a
different statistic and does not establish a win in that case. These results
show modest round-trip improvements in the other tested cases; they do not
establish a large advantage for every WebSocket workload.

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
