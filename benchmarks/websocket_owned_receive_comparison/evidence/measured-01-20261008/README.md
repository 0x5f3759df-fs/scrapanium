# Owned-segment receive comparison (2026-10-08)

The owned-segment receive prototype remains experimental and was not adopted. In this matched-backend Bend 2.0.27 campaign, all six stream non-regression guards failed, only two of seven round-trip guards passed, and neither 64 KiB stream reached the 2x throughput target against matched `curl_cffi`.

The frozen candidate was measured against the clean 7b52d7f production baseline and `curl_cffi` using the same optional source-built curl-impersonate 2.2.3/BoringSSL DSO. This is not a comparison against the stock installed backend. The TLS peer records differ by peer: Go reports TLS version 772 and cipher 4865; the Python peer reports TLS 1.3 with `TLS_AES_256_GCM_SHA384` (256-bit).

Each workload variant has 12 paired repeats. Ratios below are candidate throughput divided by the named reference; intervals are paired bootstrap 95% confidence intervals. The non-regression guard passes only when the interval lower bound is at least 0.95. A failed guard alone means the guard was not established; an interval wholly below 1.0 supports slower throughput for that measured workload.

## Streaming

| Workload | Candidate / baseline [95% CI] | 0.95 guard | Candidate / matched `curl_cffi` [95% CI] | 2x target |
|---|---:|:---:|---:|:---:|
| 30 B, flush 1 | 0.4909 [0.4514, 0.7135] | Fail | 0.6904 [0.6773, 0.7024] | — |
| 1 KiB, flush 1 | 0.6825 [0.5806, 0.7117] | Fail | 0.7771 [0.7676, 0.7890] | — |
| 64 KiB, flush 1 | 0.7529 [0.6936, 0.8127] | Fail | 0.9075 [0.8562, 0.9786] | Fail |
| 30 B, flush 64 | 0.8489 [0.8353, 0.8651] | Fail | 2.0302 [1.9546, 2.1139] | — |
| 1 KiB, flush 64 | 0.8146 [0.7827, 0.8565] | Fail | 1.5937 [1.5173, 1.6700] | — |
| 64 KiB, flush 64 | 0.7460 [0.6455, 0.8478] | Fail | 0.9232 [0.8281, 1.0058] | Fail |

The two 64 KiB stream comparisons both miss the 2x lower-bound target. For flush 1, the candidate/CFFI interval is wholly below parity; for flush 64 it crosses parity.

## Round-trip

| Workload | Candidate / baseline [95% CI] | 0.95 guard |
|---|---:|:---:|
| Python, 30 B, 1 connection | 0.9708 [0.9289, 1.0000] | Fail |
| Go, 30 B, 1 connection | 0.9952 [0.9722, 1.0143] | Pass |
| Go, 1 KiB, 1 connection | 1.0054 [0.9766, 1.0178] | Pass |
| Go, 64 KiB, 1 connection | 0.5333 [0.5128, 0.5472] | Fail |
| Go, 30 B, 4 connections | 0.9779 [0.9073, 1.0300] | Fail |
| Go, 1 KiB, 4 connections | 0.9522 [0.9407, 0.9709] | Fail |
| Go, 64 KiB, 4 connections | 0.5785 [0.5455, 0.5995] | Fail |

Both Go 64 KiB round-trip intervals are below parity; the 1-connection result is 0.533 [0.513, 0.547]. The full JSON report retains all 39 pairwise comparison intervals; `attempts.jsonl` retains every row.

## Correctness and provenance

The terminal campaign completed 474/474 successful attempts: six corrupt/swap controls, 216 stream attempts, and 252 round-trip attempts. The six controls were detected by the exact checker. The fixed schedule has 12 repeats per workload/variant and uses every variant order once in each six-repeat block. Peer frame and payload counters, binary-message/order checks, TLS observations, and runtime mappings to the pinned DSO were recorded for every positive row. Ten build steps exited successfully.

A preceding correctness-only smoke completed 45/45 attempts using 32 streamed messages and 20 round trips per workload/variant; its report contains no performance or acceptance claim. Focused sanitizer, frame-boundary, owned-lifecycle, stock-fallback, segmented-text, WSS send-flattening, and BinaryRequest-flattening allocation-failure logs are retained under `validation/`. The history ledger distinguishes the earlier compiler-version preflight failure and the unreleased second source snapshot from this final run.

`candidate-source/` contains byte-exact snapshots of the candidate's changed source inputs. The frozen runner, plan, source pins, run manifests and checksums are included for review. Binaries, DSOs, dependency trees, generated certificates and private keys are not included. These pins describe the recorded source/tool inputs; they do not claim complete operating-system or system-library hermeticity.

From the repository root, verify this saved package without building or running the campaign:

```sh
cd benchmarks/websocket_owned_receive_comparison/evidence/measured-01-20261008 && python3.14 audit/verify_saved_run.py --self-test
```

The verifier checks the package checksum list, frozen input/source hashes, both saved run records, schedule, counts, controls, peer counters, all 39 paired ratio samples and bootstrap intervals, and gate decisions. Its self-test uses temporary copies to confirm missing or modified evidence is rejected.
