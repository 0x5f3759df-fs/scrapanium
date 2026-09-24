# WSS client CPU profile diagnostic

This fixed diagnostic samples user CPU stacks during 64 KiB WebSocket receive
streams. It is intended to locate client-side costs for a later falsifiable
optimization. It does not measure an unprofiled throughput baseline or prove
that a sampled stack is the exclusive cause of latency.

## Fixed plan

Both clients use the same mapped, pinned stock RELEASE curl backend from
`.deps/curl` and the Chrome 146 profile. This is not the optional
browser-controls backend used by the rejected receive-coalescing experiment.
The peer is a local TLS server with a temporary certificate; its private key is
deleted when the run ends.

Each positive session receives 1,024 ordered binary messages of 65,536 bytes.
Both clients check each binary opcode, eight-digit sequence tag, and every
payload byte, then release the actual and expected buffers. The peer retains
frame, payload-byte, TLS-version, and cipher counters. The four conditions are
Bend and matched `curl_cffi`, each with the perf event enabled or disabled;
flush batches 1 and 64 are separate schedule cells.

The fixed full plan runs 16 eight-message corruption/swap controls first, then
96 balanced four-condition Williams repeats per flush batch: 768 positive
sessions total. It never extends the plan in response to sample counts. The
predeclared floor is 80 selected user-CPU samples for each profiled client ×
flush cell, pooled over its 96 sessions. All selected samples remain in the
floor and flat-symbol denominator, including unresolved frames. Missing and
unresolved stacks are reported separately; perf text does not reliably expose
all truncated unwinds, so truncation remains unknown. Inclusive stack groups
can count only frames that were observed and are therefore lower bounds.

The client interval uses integer `CLOCK_MONOTONIC` nanoseconds. It begins just
before sending the start control frame and ends after the final opcode, sequence
and full-byte check and immediate buffer release. Corpus creation, process
startup, TLS setup, warmup, compilation, and socket close are outside the
interval. Perf samples are filtered to the half-open interval `[start,end)` and
the client PID/TIDs. The event is `cpu-clock:u` at 99 Hz with a 16 KiB DWARF
user stack; kernel and hypervisor samples are excluded.

The disabled condition attaches the same perf recorder and uses the same
control channel with sampling disabled. It controls for that harness path; it
is not a no-profiler comparison. Any enabled/disabled difference includes
sampling observer cost. Unknown/lost or throttling diagnostics remain
unavailable when the retained perf output does not expose them; unavailable is
not interpreted as zero. A below-floor cell is inconclusive and is not
extended.

The correctness smoke passed all 48 rows: 16 expected corruption/swap
rejections and 32 exact-check positives. Its eight-message profiled rows yielded
zero selected in-window samples, as expected for a short correctness gate; the
80-sample floor applies only to the fixed full schedule. This is not performance
evidence, and no full CPU profile is included. See the
[retained smoke report](results/wss-client-perf-smoke-20260924/REPORT.md).
Neither this diagnostic nor the older phase/profile reports support a
throughput claim without a separate matched, unprofiled benchmark. Results from
a different backend build must not be compared as if only the client changed.

## Reproduction

Run from the repository root in WSL/Linux with the matched Python environment,
Clang, Go, pinned Bend/Bun runtime, and the extracted Linux `perf` executable
and libraries available. `SCRAPANIUM_PERF_LIB` must point to the directory
containing the matching `perf` shared libraries. The runner rejects inherited
sanitizer or preload settings. Each output path must be new.

```sh
export SCRAPANIUM_PERF=/path/to/perf
export SCRAPANIUM_PERF_LIB=/path/to/perf/shared-libraries

smoke_dir="build/wss-client-perf-smoke-$(date -u +%Y%m%dT%H%M%SZ)"
.deps/venv-matched/bin/python benchmarks/websocket_client_perf.py \
  --smoke --output-dir "$smoke_dir" --perf "$SCRAPANIUM_PERF"

full_root="/path/on/persistent/storage"
full_dir="$full_root/wss-client-perf-$(date -u +%Y%m%dT%H%M%SZ)"
.deps/venv-matched/bin/python benchmarks/websocket_client_perf.py \
  --full --output-dir "$full_dir" --perf "$SCRAPANIUM_PERF"
```

Full output must be on persistent `ext4`, `xfs`, or `btrfs` storage. Keep the
manifest, plan, per-attempt JSON/JSONL, perf data and decoded text together;
they record the actual tool/header/source/backend hashes, client boundaries,
exclusions, lost-sample availability, and failures. The smoke command runs 16
negative controls and 32 positives of eight messages each. It must pass before
the fixed full schedule is considered ready.

For the retained smoke, the machine-specific perf executable and output root
were /home/baidu/wss-perf-feasibility-20260923/extracted/usr/bin/perf and
/home/baidu/scrapanium-experiments/wss-client-perf-smoke-20260924-nul-ack-final.
