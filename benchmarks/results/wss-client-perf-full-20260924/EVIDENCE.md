# Evidence inventory

This archive contains the fixed WSS client CPU-profile run collected on the
WSL ext4 path `/home/baidu/scrapanium-experiments/wss-client-perf-full-20260924`.
The run is tied to source commit
`061950a8041e6fdf9cbb9602b07bbab5291b6d24`; the audit verifies recorded source
hashes against Git blobs at that commit rather than requiring future
working-tree sources to remain unchanged.

The copied `manifest.json`, `plan.json`, and `samples.jsonl` are byte-identical
to the collection-host originals. The manifest records 784/784 completed rows,
768 positive sessions, 16 corruption/swap controls, zero failed rows, all four
sample floors met, stable source/binary/binding/tool inputs, and TLS identity
772/4865. The positive condition used the pinned stock RELEASE backend with
SHA-256
`bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3`.
Recorded profiling used perf 7.0.14 and the fixed `cpu-clock:u` 99 Hz,
16 KiB DWARF user-stack, monotonic-clock configuration. The private TLS key was
not retained.

`attempts.tar.gz` is a lossless archive of all 784 attempt directories. It
contains 9,408 regular files (12 per attempt; 58,314,577 bytes uncompressed),
including each `perf.data`, header/report/script text and stderr, control
transcript, peer record, client logs, and attempt-sidecar JSON. It excludes the
generated Bend/Go executables, dependency packages, input message corpus, and
private key. `attempt-members.sha256` inventories every archive file and is
verified against the uncompressed members by the audit script.

The checked-in `audit.py` reconstructs the exact seeded plan and control order
using the runner/parser Git blobs, validates all row and peer totals, verifies
the complete archive against its per-member inventory, and re-parses every raw
perf-script file to recompute sample filtering and profile summaries. It also
recomputes paired enabled/disabled interval ratios from retained nanosecond
boundaries. It writes `summary.json`; no profile collection or build is run.

From the repository root, verify the archive and regenerate the summary with:

```sh
python3 benchmarks/results/wss-client-perf-full-20260924/audit.py
(cd benchmarks/results/wss-client-perf-full-20260924 && sha256sum -c SHA256SUMS.txt)
```

`SHA256SUMS.txt` covers every top-level evidence file except itself. Scoped
`.gitattributes` rules preserve the copied collection bytes and keep authored
text as LF across checkout platforms.
