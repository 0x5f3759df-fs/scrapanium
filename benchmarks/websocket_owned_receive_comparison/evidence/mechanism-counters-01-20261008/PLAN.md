# WSS owned-receive mechanism counters-only preparation

Status: preparation only. The fixed 16 positive attempts are not released by this plan; do not execute them until the root reviewer sends a separate release.

## Purpose and limits

This captures mechanism counters for the existing Bend 2.0.27 WSS receive implementation and the isolated owned-segment candidate at the same 7b52d7f benchmark/checker baseline. It does not measure elapsed time, throughput, CPU time, or performance gates. It cannot establish adoption or a performance benefit. The already reviewed private Curl/BoringSSL diagnostic DSO is shared by both arms.

The existing websocket_streaming.sample and websocket.sample helpers launch the unchanged benchmark clients and enforce the existing full byte/opcode checks. They internally return timing values because that is their existing interface; the runner must discard those values, save no timing/rate/duration fields, and record only the counter snapshots, client return/mapping, exact peer observations, workload identity, and correctness checks. This is a fixed mechanism census, not a re-run or replacement of the published performance campaign.

The only client profile is Chrome 146 over verified local TLS. The unchanged Go peers negotiate TLS 1.3 (wire version 772) with cipher 4865. Both arms use the same isolated Release/O3 Curl diagnostic DSO. There is no curl_cffi arm, stock-backend arm, corruption control, sweep, retry, or adaptive count.

## Fixed cells and execution order

Each cell is run once by each arm, for exactly 16 client processes if every attempt succeeds. Stream cells are the six existing websocket_streaming.WORKLOADS, in their frozen order:

| Cell | Payload bytes per binary message | Data messages | Peer flush batch |
|---|---:|---:|---:|
| stream-30b-flush1 | 30 | 65,536 | 1 |
| stream-1024b-flush1 | 1,024 | 16,384 | 1 |
| stream-65536b-flush1 | 65,536 | 1,024 | 1 |
| stream-30b-flush64 | 30 | 65,536 | 64 |
| stream-1024b-flush64 | 1,024 | 16,384 | 64 |
| stream-65536b-flush64 | 65,536 | 1,024 | 64 |

The two round-trip cells are the existing Go single-connection cases:

| Cell | Payload bytes per binary message | Measured echoes |
|---|---:|---:|
| go-30b-c1 | 30 | 5,000 |
| go-65536b-c1 | 65,536 | 300 |

For workload index 0 through 7, run baseline then owned for even indices and owned then baseline for odd indices. Do not reorder cells, add warmup attempts, retry failed attempts, or continue after a failed attempt. The runner writes the intended order to planned_schedule.json before peer/client startup and checks the actual attempt sequence.

The stream payload length includes its 8-byte decimal sequence tag. Per arm, the measured six-cell stream workload contains 165,888 exact comparisons and 171,704,320 compared payload bytes; each cell also has one six-byte warmup comparison before the gate. The Go round-trip cells contain 5,300 measured exact comparisons and 19,810,800 compared payload bytes per arm; each cell performs one full-payload warmup comparison before the gate. These totals are validation expectations, not timing measurements.

## Counter boundaries and checks

Every client opens one exclusive counter JSONL file and writes four cumulative snapshots in this exact order: cold after the WSS connection is established and before warmup, gate after the warmup and immediately before measured work, work_end after measured messages and exact checks but before release, and released after the client releases its WebSocket and retained message buffers. The JSON object contains the fixed native and Curl counter names plus layout values. Each unique client process has its own DSO instance and counter file.

The saved-artifact validator requires all four rows, stable counter names/layout, nonnegative integer values, the fixed attempt order and counts, exact checker success, exact peer frame/payload/frame-byte totals, TLS version/cipher, the mapped canonical DSO path/hash, and successful peer exit records with hashed stdout/stderr captures. It verifies the per-cell equality deltas: stream warmup is one call/6 bytes and measured work is count calls / count * bytes bytes; round-trip warmup is one full-size call and measured work is count calls / count * bytes bytes. Round-trip peer payload accounting therefore includes the full-size warmup frame.

For the measured interval, native framed/owned receive and WebSocket send status classes must reconcile to their call totals with zero errors, and the receive byte totals must equal the exact checked payload. The baseline must receive through framed recv; the owned arm must receive the complete measured payload through the owned API. Both arms have zero generic receive-flatten counters on these fixed step paths. Baseline Bend flatten slots remain zero because its pinned source has no such helper; owned stream uses one flat start-send call without copying, while owned round trips require N flatten calls, N allocations, and N * message-size copied bytes per cell. The owned round-trip warmup body is retained at both gate and work_end, and the final body is released by the released snapshot. The stream checker consumes each actual/expected message before work_end, so native segment and payload/pin gauges must already be zero there.

The validator checks that native segments, Curl chunks and their requested-byte charges, pins, and payloads are all released by the released boundary. It reconciles cumulative allocation/free counters with live gauges without assuming one network read or Curl chunk per frame. This WebSocket receive queue is unpooled; queue-reference release and physical chunk free remain separate counters, and the final released snapshot must have all tagged live gauges at zero. Curl TLS-read counters cover the instrumented client process and are broader than WSS payload reads. The native readiness-query counter does not count kernel waits. No inclusive call-chain, CPU-time, Amdahl, or causal cost claim is in scope.

## Inputs and build

The baseline and owned source checkouts are separate worktrees at exact HEAD 7b52d7f1cbbfe4c21b1abf1ad71e8e3061c47c17. The shared benchmark, checker, and peer sources are hash-compared. Every modified/untracked worktree path and complete tracked diff is pinned. The compiler/runtime, compiler resource headers, Curl overlay/patch sources, Curl public headers, build manifest, private DSO, and built client binaries are separately SHA-256 pinned. Preflight creates a fresh run-local Python bytecode prefix before importing the frozen Python helpers and records source/cache origins.

The diagnostic Curl DSO is /home/baidu/scrapanium-experiments/wss-owned-mechanism-20261008/artifacts/owned-mechanism-02/curl-diagnostic-build-01/prefix/lib/libcurl-impersonate.so.4.8.0, SHA-256 e48e14edacc7d8ed0abab6f5f77e5d9cebca1a87c0153ce7e939b407252e9ab9. Both arms link the same public headers and library path. The program records the actual /proc/<pid>/maps backend mapping for every client.

Build-01 is retained as a preflight failure with zero commands started. The successful build-02 outputs are written only under client-build-02; the checkouts and shared/published build trees are inputs. The exact argv arrays, working directories, environment removals/overrides, and same-flags link commands for all four Bend clients and both Go peers are recorded in build-commands-02.json. All ten generation/link commands exited successfully and their stdout, stderr, exit code, generated C, and binary hashes are pinned in build-session.json. No client or peer process was invoked during compilation. The runner stores each successful attempt only after checker, peer, DSO-map, and counter validation. If an attempt fails, status records its planned index, stage, error type, and any available counter-file hash; the run stops without retry.
## Release gate

Preparation is not permission to run the 16 processes. Before execution, the root reviewer must approve the final runner, test, plan, source pins, built client hashes, preflight log, and exact fresh output path. Execution is one command, one pass, 16 positive attempts maximum, no retries, no timings, no acceptance/performance claims. Any failure is preserved in the unique output directory and ends the run.
