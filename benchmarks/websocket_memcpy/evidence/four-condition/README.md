# Same-source WSS memcpy comparison

The candidate backend redirects the pinned BoringSSL `SSL_read`/`SSL_peek`
memcpy path through the verified `memcpy@GLIBC_2.14` wrapper. The backend pair
passed its route, guard, full-suite, and sanitizer gates, but the candidate is
not adopted: both Bend 64 KiB workload medians missed the predeclared 5% gain
threshold, and both paired 95% intervals include 1.0. The four smaller Bend
cells met their no-regression lower-bound gate. Product sources, dependency
defaults, and published stock-performance claims remain unchanged.

The fixed full run completed all 488 planned attempts: 8 corrupt/swap controls
followed by 480 positive measurements, with zero failed rows and final source,
binary, backend, and binding hashes verified. The correctness smoke separately
completed all 32 attempts. Both clients retained binary-opcode, unique sequence,
full-payload, ordering, and immediate-release checks inside each timed interval.
One Bend executable and bridge were compiled once against baseline headers and
reused byte-for-byte for both backend conditions; only `LD_LIBRARY_PATH`
selected the runtime DSO, whose mapped path and hash were checked. TLS setup,
warmup, corpus preparation, and close were outside the client timer.

Ratios are `baseline elapsed / candidate elapsed` for backend comparisons, so
values above 1 favor the candidate. Matched-client ratios are
`curl_cffi elapsed / Bend elapsed`, so values above 1 favor Bend. Intervals are
paired repeat-level bootstrap 95% CIs over 20 repeats (10,000 resamples); raw
attempts are retained. The candidate-gain interval is not a claim about other
hosts. These measurements use a local source-built upstream curl-impersonate
2.2.3 pair, not released stock binaries or a production configuration.

| Workload | Bend candidate ratio (95% CI) | `curl_cffi` candidate ratio (95% CI) | `curl_cffi` / Bend, baseline (95% CI) | `curl_cffi` / Bend, candidate (95% CI) |
|---|---:|---:|---:|---:|
| 30 B, flush 1 | 1.004 (0.986–1.013) | 0.995 (0.973–1.003) | 1.482 (1.469–1.519) | 1.501 (1.491–1.531) |
| 30 B, flush 64 | 0.999 (0.982–1.016) | 1.002 (0.987–1.019) | 2.343 (2.294–2.397) | 2.336 (2.293–2.358) |
| 1 KiB, flush 1 | 0.999 (0.970–1.022) | 1.015 (0.980–1.035) | 1.440 (1.419–1.481) | 1.432 (1.396–1.448) |
| 1 KiB, flush 64 | 1.007 (0.988–1.052) | 1.012 (0.992–1.026) | 1.967 (1.944–2.017) | 1.974 (1.944–2.018) |
| 64 KiB, flush 1 | 1.013 (0.983–1.075) | 1.043 (1.007–1.084) | 1.254 (1.180–1.312) | 1.226 (1.155–1.263) |
| 64 KiB, flush 64 | 1.033 (0.974–1.120) | 1.030 (0.995–1.064) | 1.266 (1.206–1.297) | 1.268 (1.230–1.327) |

The frozen promotion rule required both 64 KiB Bend medians to be at least
1.05 with paired-CI lower bounds above 1.0. Observed Bend ratios were
1.0126 (0.9829–1.0746) at flush 1 and 1.0330 (0.9739–1.1204) at flush 64;
neither passed. All four 30 B/1 KiB Bend lower bounds were at least 0.95. The
`curl_cffi` 64 KiB ratios were 1.0427 (1.0073–1.0837) and 1.0304
(0.9954–1.0641), retained as separate client results rather than used to claim
a Bend gain.

## Retained files

- [`full/samples-full/manifest.json`](full/samples-full/manifest.json) records
  the complete fixed plan, environment, pair/client provenance, and final hash
  checks; [`samples.jsonl`](full/samples-full/samples.jsonl) contains every
  full-run attempt.
- [`smoke/samples-smoke/manifest.json`](smoke/samples-smoke/manifest.json) and
  [`samples.jsonl`](smoke/samples-smoke/samples.jsonl) retain the correctness
  smoke.
- `client-build-logs/` and each run's `peer-build.*.txt` retain command outputs.
- [`verdict.json`](verdict.json) and [`audit_results.py`](audit_results.py)
  contain the independent summary/verifier. [`SHA256SUMS.txt`](SHA256SUMS.txt)
  inventories the archived bytes.

Recompute the verdict from the retained full-run rows, then verify every
packaged file from the repository root:

```sh
python benchmarks/websocket_memcpy/evidence/four-condition/audit_results.py \
  --samples-dir benchmarks/websocket_memcpy/evidence/four-condition/full/samples-full \
  --output benchmarks/websocket_memcpy/evidence/four-condition/verdict.json
(cd benchmarks/websocket_memcpy/evidence/four-condition && sha256sum -c SHA256SUMS.txt)
```

The generated client archive, executables, TLS private keys, and dependency
packages are excluded. The complete original run directories remain at the
ext4 paths recorded in the manifests.
