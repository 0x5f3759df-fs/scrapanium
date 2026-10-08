# Owned-receive mechanism counters (2026-10-08)

This counters-only follow-up records one baseline and one experimental-owned run for each of six streaming cells and two Go single-connection round-trip cells. It used the frozen Bend 2.0.27 diagnostic clients, the chrome146 profile, and one shared optional curl-impersonate/BoringSSL diagnostic DSO. The Go peer negotiated TLS 1.3 (version code 772, cipher 4865).

All 16 client attempts passed the exact byte, opcode, ordering, peer-frame, and mapped-DSO checks. Each arm completed 165,888 streaming equality checks over 171,704,320 bytes and 5,300 round-trip equality checks over 19,810,800 bytes. The table reports accumulated measured-interval counters across the six streaming and two round-trip cells per arm.

| Counter | Baseline | Owned |
| --- | ---: | ---: |
| New tagged receive chunks allocated (chunks) | 0 | 99,968 |
| Spare receive-chunk reuse events (count) | 99,968 | 0 |
| Curl receive-queue collect-copy (bytes) | 191,515,120 | 0 |
| Bend Bytes.flatten copied bytes | 0 | 19,810,800 |
| Tagged chunk, pin, payload, and native-segment live gauges after release | all zero | all zero |

The owned stream start signal is sent inside the measured interval. Its six Bytes.flatten calls allocated and copied zero bytes. Owned round-trip bodies were retained at work_end and released afterward; all tracked live gauges were zero at the released boundary. Read-call counts are not message counts.

The saved report is complete and all client return codes are zero. The wrapper's outer shell returned 1 while trying to save a terminal log, so the runner process exit code was not independently captured. The [recovery note](census/wrapper-capture-recovery.txt) preserves the available tool transcript without claiming a runner exit code. No attempt was repeated.

The preceding 474-attempt performance comparison remains the source of performance findings; its gates failed and its candidate remains unadopted. This census contains no elapsed time, throughput, latency, acceptance result, or adoption claim. See the [earlier comparison](../measured-01-20261008/README.md).

The archive includes the saved report, attempt rows, counter snapshots, peer logs, diagnostic source snapshots, the original shared Bend checkers, Go peers, Python client/lab helpers, baseline WebSocket include, and build/preflight records. SHA256SUMS lists every archive file except itself. It omits compiled executables, the Curl DSO, compiler resource headers, dependency caches, certificates, and private keys. Curl source snapshots are accompanied by its original [COPYING notice](backend-overlay/COPYING). Absolute paths in frozen records refer to the original WSL experiment environment; [snapshot-map.json](provenance/snapshot-map.json) maps archived source files to their pinned paths and hashes.

From the repository root, verify the saved data without compiling or running the campaign:

    python3 benchmarks/websocket_owned_receive_comparison/evidence/mechanism-counters-01-20261008/audit/verify_saved_census.py --self-test

The verifier checks the package SHA-256 list, frozen source and overlay snapshots, fixed schedule, every saved counter and peer-log hash, and counter/status invariants. Its self-test verifies that reordered attempts and a missing source snapshot are rejected from temporary copies.
