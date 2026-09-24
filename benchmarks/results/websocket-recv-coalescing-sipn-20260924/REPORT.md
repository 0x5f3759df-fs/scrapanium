# WebSocket receive-coalescing experiment

## Decision

The candidate reduced `curl_ws_recv` calls sharply, but it did not meet the predeclared Bend throughput gate for either 64 KiB workload. Keep this source-built candidate as an experiment; do not adopt it into the product or use it for a benchmark headline.

The candidate is a pair of changes: curl's incomplete-data-frame receive path makes up to four bounded, nonblocking extra reads when curl's pending-data hint is true, and the native bridge allows later reads into already-owned message capacity. Both changes preserve the existing exact-payload checks and ownership lifetime. The tested source-built curl-impersonate 8.22.0 backend uses the optional browser-controls build configuration; it is not the stock-release backend used in the earlier benchmark report.

## Method

The fixed run retained 488/488 attempts: eight corruption/swap controls followed by 480 positive samples. The positive matrix has six workloads, 20 repeats per workload, and four client/backend conditions. Each workload uses five instances of every planned four-condition Williams order. Bend and matched `curl_cffi` both receive and check the ordered binary opcode, sequence, and full payload, then release each actual and expected buffer. The peer reports exact frame and byte totals; every positive negotiated TLS version 772 and cipher 4865.

The measured client interval includes the start signal and message loop/checks. Corpus creation, process startup, TLS upgrade, warmup, and close are outside it. Speed ratios are baseline elapsed/candidate elapsed, so values above 1 favor the candidate. Matched-client ratios are `curl_cffi` elapsed/Bend elapsed, so values above 1 favor Bend. Times shown are median elapsed times by condition; each ratio is the median of 20 paired repeat-level ratios, not the ratio of those displayed medians. Bracketed intervals are 95% repeat-level bootstrap intervals for the median, 10,000 resamples; backend-pair ratios use seed 20260924 and matched-client ratios seed 20260925. These per-cell intervals are not adjusted for multiple comparisons.

## Throughput results

| Workload | Bend median ms, baseline → candidate | Bend speed ratio [95% CI] | `curl_cffi` median ms, baseline → candidate | `curl_cffi` speed ratio [95% CI] |
| --- | ---: | ---: | ---: | ---: |
| 30 B × 65,536, flush 1 | 114.625 → 114.290 | 1.000 [0.970, 1.070] | 168.991 → 170.504 | 0.997 [0.980, 1.018] |
| 30 B × 65,536, flush 64 | 38.331 → 38.537 | 1.001 [0.975, 1.020] | 92.489 → 92.993 | 0.984 [0.966, 1.022] |
| 1 KiB × 16,384, flush 1 | 36.560 → 35.605 | 1.000 [0.988, 1.046] | 52.352 → 51.680 | 1.020 [0.999, 1.031] |
| 1 KiB × 16,384, flush 64 | 16.915 → 16.689 | 1.014 [1.001, 1.049] | 33.163 → 32.039 | 1.031 [1.009, 1.055] |
| 64 KiB × 1,024, flush 1 | 25.777 → 26.775 | 0.954 [0.893, 1.017] | 32.186 → 27.014 | 1.175 [1.156, 1.247] |
| 64 KiB × 1,024, flush 64 | 25.250 → 24.938 | 1.011 [0.969, 1.052] | 31.362 → 26.328 | 1.213 [1.180, 1.263] |

The four 30 B and 1 KiB Bend cells pass the predeclared small/medium guard: each paired interval's lower bound is at least 0.95. Both primary 64 KiB Bend cells fail: the required median ratio was at least 1.05 with a lower interval bound above 1.0; the observed medians are 0.954 and 1.011, and both intervals include 1. This does not establish equivalence or zero cost. It does show this candidate has not demonstrated the requested large Bend gain.

The same candidate pair materially improved matched `curl_cffi` on the two large workloads. The Bend-versus-`curl_cffi` median ratio fell from 1.275 to 1.010 at flush 1 and from 1.272 to 1.035 at flush 64. The candidate intervals include 1, so these comparisons do not establish equal throughput; the measured shared-backend benefit mostly accrued to `curl_cffi`, not Bend.

| Workload | Baseline `curl_cffi` / Bend [95% CI] | Candidate `curl_cffi` / Bend [95% CI] |
| --- | ---: | ---: |
| 30 B × 65,536, flush 1 | 1.482 [1.408, 1.507] | 1.499 [1.407, 1.529] |
| 30 B × 65,536, flush 64 | 2.418 [2.337, 2.446] | 2.466 [2.354, 2.501] |
| 1 KiB × 16,384, flush 1 | 1.438 [1.403, 1.504] | 1.445 [1.428, 1.465] |
| 1 KiB × 16,384, flush 64 | 1.930 [1.859, 2.013] | 1.958 [1.911, 2.022] |
| 64 KiB × 1,024, flush 1 | 1.275 [1.209, 1.309] | 1.010 [0.955, 1.047] |
| 64 KiB × 1,024, flush 64 | 1.272 [1.170, 1.345] | 1.035 [0.970, 1.065] |

## Receive-call diagnostic

A separate fixed eight-row probe measured one 64 MiB stream for each client/backend/flush cell, with exact byte checks and 1,024 peer frames. It is call-count evidence, not a timing comparison.

| Client | Flush 1, baseline → candidate | Reduction | Flush 64, baseline → candidate | Reduction |
| --- | ---: | ---: | ---: | ---: |
| Bend | 5,130 → 2,166 | 57.8% | 5,128 → 2,116 | 58.7% |
| Matched `curl_cffi` | 5,188 → 1,031 | 80.1% | 5,163 → 1,027 | 80.1% |

The reduction passed the call-count check, but it did not translate into the required Bend throughput improvement. All call-count rows transferred exactly 67,108,864 payload bytes and negotiated the same TLS identity.

## Run history and limitations

The authoritative data is the persistent-filesystem run copied here. An earlier `/mnt/c` attempt retained 319 passed rows (eight controls and 311 positives) before atomic replacement of its manifest failed with `PermissionError`; it is incomplete and is not included in any ratio. A later `/tmp` attempt completed, but its raw files disappeared when the WSL temporary filesystem reset before they could be retained. That attempt is not used as quantitative evidence.

This report applies only to the optional browser-controls backend pair and measured workloads. The receive-call probe is one sample per cell, and timing intervals are per-cell nominal intervals without multiplicity adjustment. The results do not support broad throughput or no-regression claims. The full manifest and raw records retain the schedule, per-attempt validation, toolchain, client/backend mappings, and frozen hashes.

## Reproduce the report arithmetic

From this directory, run `python3 audit.py`. It verifies the retained row schedule and evidence hashes, then recomputes the medians, paired ratios, bootstrap intervals, and gate outcomes from `manifest.json` and `samples.jsonl`. The exact stream runner is in the [reproducible experiment sources](../../websocket_recv_coalescing/stream_four_condition.py), SHA256 `0e13fef8779896d41f0249641a1cbfe1c2c7ce46952534fbd05a8d626fff9b6b`; its build and reproduction details are in the [experiment inventory](../../websocket_recv_coalescing/README.md). The [direct fixture report](../../websocket_recv_coalescing/evidence/direct/REPORT.md), [wrapped-fault log](../../websocket_recv_coalescing/evidence/direct/wrapped-faults.log), both [134-test logs](../../websocket_recv_coalescing/evidence/pytest-baseline.log) and [candidate log](../../websocket_recv_coalescing/evidence/pytest-candidate-sipn.log), and the [32-row smoke manifest](../../websocket_recv_coalescing/evidence/smoke/manifest.json) retain the correctness evidence.

## Evidence files

The [full manifest](manifest.json) records the fixed plan, validation summary, environment, backend/client mappings, and source/binary hashes; [samples.jsonl](samples.jsonl) retains all 488 attempts. The separate call-count probe retains its [plan](probe/plan.json), [manifest](probe/manifest.json), [eight raw rows](probe/samples.jsonl), and [summary](probe/summary.json). The partial `/mnt/c` attempt retains its [manifest](interrupted-mntc/manifest.json) and [raw rows](interrupted-mntc/samples.jsonl). Backend build details and file hashes are indexed in the [evidence inventory](EVIDENCE.md). Run the local [audit script](audit.py) to validate the retained data and recompute the tables above.
