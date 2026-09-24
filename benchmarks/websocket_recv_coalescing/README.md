# WSS receive-coalescing experiment

This is a rejected, reproducible experiment. It tested whether curl could drain
already-buffered TLS input into a larger caller buffer, reducing WebSocket
receive calls. The candidate preserved the existing initial read, then allowed
at most four additional one-reader `Curl_bufq_sipn` attempts on an incomplete
data frame when the pending-data hint was true. It did not wait, cross a frame
boundary, or coalesce control frames. The native bridge kept its first 16 KiB
scratch read and widened only reads into already-owned message capacity.

The fixed throughput run did not meet the predeclared Bend adoption gate. Its
64 KiB baseline/candidate median ratios were about 0.95 for flush 1 (paired
95% CI 0.893–1.017) and 1.01 for flush 64 (0.969–1.052). Both intervals
include 1, and neither cell met the required median of 1.05. The backend and
bridge changes therefore remain an experiment; no product source or existing
benchmark chart was changed. The [retained results report](../results/websocket-recv-coalescing-sipn-20260924/REPORT.md)
contains the full analysis and raw rows.

`stream_four_condition.py` and `recv_count_probe.py` are the exact frozen
drivers used for the throughput and eight-row receive-call runs. They import
the existing `benchmarks/websocket_streaming.py` and related clients from the
base source revision recorded below; the raw manifest stores those source
hashes. The direct receiver and wrapper fixtures, pytest logs, and a
correctness-only 32-row stream smoke are retained under `evidence/`.

## Reconstruct the isolated pair

Use a Linux x86-64 environment with the repository's pinned Bun, Bend source,
matched curl_cffi virtual environment, Clang, CMake, Ninja, Go, and normal
backend build dependencies installed. The documented cold setup is not
claimed to have been exercised end-to-end on a clean machine. It performs a
full optional-backend build and can take several minutes.

From a checkout containing this experiment directory, create a clean detached
worktree at the source revision used for the measurements, then copy these
ignored-build inputs into it. Keeping the drivers under `build/` lets their
recorded repository-root and clean-worktree checks match the original run.

```sh
git worktree add --detach ../scrapanium-wss-recv-base 7b412fbaae907f4419b94085d0ae5bf81db94341
mkdir -p ../scrapanium-wss-recv-base/build/wss-recv-coalescing
cp -a benchmarks/websocket_recv_coalescing/. ../scrapanium-wss-recv-base/build/wss-recv-coalescing/
cd ../scrapanium-wss-recv-base
python3 scripts/bootstrap.py --matched
python3 build/wss-recv-coalescing/prepare_pair.py
```

The helper refuses a dirty or wrong-revision checkout and refuses to reuse
existing backend/client output directories. It builds the baseline with
`scripts/build_backend.py`, snapshots that install with symlinks materialized,
applies `patches/curl-ws-recv-coalescing.patch` to the same fresh curl source,
rebuilds and snapshots the candidate, then creates two `git archive` client
roots. It applies `patches/native-bridge-coalescing.patch` only to the candidate
root and builds each root's native bridge and stream client with its matching
backend RPATH. Fresh local manifests and minimal runner provenance are written
under the ignored build directory. The helper does not consume the captured
machine-specific manifests in `benchmarks/results/`.

Before attempting a full run, execute the correctness-only smoke and inspect
its retained manifest:

```sh
python3 build/wss-recv-coalescing/stream_four_condition.py --smoke \
  --output-dir build/wss-recv-coalescing/stream-smoke-reproduction
```

The fixed 20-repeat run is available for independent reproduction; it makes
480 positive samples and eight corruption/swap controls. Use a new output path
on a durable Linux filesystem and do not overwrite the published results:

```sh
python3 build/wss-recv-coalescing/stream_four_condition.py --run-full \
  --output-dir "$HOME/scrapanium-experiments/stream-full-reproduction"
```

The separately frozen receive-call probe builds its instrumented clients and
retains eight exact-check runs. Its timings are diagnostic only:

```sh
python3 build/wss-recv-coalescing/recv_count_probe.py \
  --output-dir build/wss-recv-coalescing/recv-count-reproduction
```

## Recorded implementation and evidence

- Base repository: `7b412fbaae907f4419b94085d0ae5bf81db94341`.
- Upstream curl: `6e8f87760a4dd96771e96fc9d55440dcd8845243`; archive SHA256
  `222c6b5c1f368ac63aed59bce2774eb5def9e8e67e46e800be182e684d2845a3`.
- Baseline/candidate backend library SHA256 values:
  `59460540f8fd6eab8eff22244e071c3132d7073f5be5a6dd9702b7c991315527` /
  `242162132dde4cc83a9fb081bbaf2d9f4c12a15873bfa4b5c80f13f8c022e287`.
- Candidate curl source SHA256:
  `9e67cfc98bd2e08de253fd9a09f57b06f1d6c5f5ab97780ac7e1da135946c058`.
- Candidate native bridge source SHA256:
  `f051b39d89808f9db5a918471574db97a2e60dd4d601b256cc2714779e2de544`.
- Backend and bridge patches are in [`patches/`](patches/). Correctness fixtures
  are in [`tests/`](tests/), with captured logs and hashes under [`evidence/`](evidence/).

The baseline and candidate isolated WebSocket pytest groups each passed 134
tests. The 32-row smoke passed all positive and corruption/swap checks. The
full fixed run completed 488/488 rows with verified final hashes. The separate
call-count probe completed eight rows; the report records those counts without
treating them as speedup evidence. A previous setup attempt omitted the pinned
Bun executable and logged setup errors; the corrected suite supplied the
pinned Bun, Bend source, Clang, and backend prefix. That setup-only failure is
retained separately from the passing run.
