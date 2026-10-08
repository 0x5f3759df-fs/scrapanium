# Owned-segment WSS comparison preregistration - campaign 02

**Campaign state:** no smoke or timing campaign has run. The preflight-only integration test and deterministic 20-test unit suite below passed; no WebSocket workload attempt or performance claim has been made. The smoke and timing commands below still require root release. This plan and the runner/source-pins are immutable inputs once released. Every attempt and failed attempt must remain in its fresh output directory; no retries, parameter sweeps, workload-count changes, or backend substitutions are allowed.

## Question and scope

This comparison tests whether the isolated owned-segment receive implementation improves the existing public Bend WebSocket APIs while preserving exact application behavior. The production baseline is Scrapanium commit `7b52d7f1cbbfe4c21b1abf1ad71e8e3061c47c17`; candidate and baseline use the **same pinned optional source-built curl/BoringSSL backend DSO**, and matched `curl_cffi` uses that DSO as well. This is a same-backend uplift comparison, not a performance comparison against the stock installed DSO. The stock default backend remains unchanged and does not have the owned-receive symbols or the optional backend's noncanonical-length rejection patch.

The candidate uses the ordinary public `S.Ws.receive`, `S.Bytes.equal`, `S.Bytes.discard`, and `S.Ws.send` calls. Streaming compares every returned byte and opcode inside timing. Round-trip reuses the received `Bytes` value in the ordinary `S.Ws.send` path, so required flatten-on-send remains part of timed work. No borrowed-span API, test-only shortcut, reduced checker, or omitted payload validation is allowed. The backend patch removes the curl receive-buffer-queue-to-Scrapanium-message copy when owned symbols are available; the BoringSSL `SSL_read` to curl buffer copy remains. This is not transport-level zero-copy.

All three clients use the fixed `chrome146` profile and certificate verification enabled by the original benchmark sources. The Go peer requires TLS 1.3 code 772 and cipher 4865; Python peer negotiation is recorded and must remain a single stable version/cipher across all variants. Each client process must map the exact pinned DSO path and SHA-256. Do not substitute recent browser-profile capture settings or imply that stock-default behavior is represented.

## Fixed matrix and acceptance

Run the original 13 workloads unchanged: six streaming workloads (30 B x 65,536 messages, 1,024 B x 16,384, and 65,536 B x 1,024, each with peer flush batch 1 and 64) and seven round-trip workloads (original Python 30 B x 1,000; Go 30 B, 1,024 B, and 65,536 B at connection counts 1 and 4, with their original counts 5,000, 2,000, and 300 respectively). The checked-in runner validates these matrix definitions against the original benchmark scripts before it builds.

Use 12 repeats per workload and variant, seed `20261008`, and the runner's fixed balanced ordering: each of the six baseline/candidate/`curl_cffi` execution permutations appears once in each six-repeat block for every workload. Stream and round-trip workload order is position-balanced by the runner. This yields 468 positive observations (13 x 12 x 3) plus six fixed negative controls (corrupt and swap sequence/payload faults, one for each of three clients), 474 attempts total.

Keep the runner's original full message counts, payload generators, sequence tags, binary-opcode and complete-byte comparisons, frame observations, TLS checks, timer bounds, and exact checker failures. A failed check, missing attempt, unexpected DSO, invalid TLS identity, or changed source aborts the campaign; preserve the error artifact and do not rerun it under the same output path.

A result qualifies only if both 65,536-byte streaming workloads have candidate-versus-`curl_cffi` paired-throughput bootstrap 95% CI lower bounds **at least 2.0**, and candidate-versus-baseline paired-throughput bootstrap 95% CI lower bounds are **at least 0.95** for all six streaming and all seven round-trip workloads. All gates must pass together. Any smoke is correctness-only and must not produce or support a performance claim.

## Immutable inputs

- Runner: `run_owned_comparison.py`, SHA-256 `e98c43c765e63d86fc55d071561ab6a16b6cfcac307f198ec6f61cb96e6755d0`.
- Source/toolchain pins: `source-pins.json`, SHA-256 `5f891e15575c3390e71f9e84ced837697ee46453570858d74afdfffc80f557f7`. The runner constant is pinned to this digest.
- Baseline worktree: `/home/baidu/scrapanium-experiments/bend2-027-compat/worktree`, clean at `7b52d7f1cbbfe4c21b1abf1ad71e8e3061c47c17`.
- Candidate worktree: `/home/baidu/scrapanium-experiments/wss-owned-segments-20261007/worktree`, same HEAD with the exact status, diff, and file hashes in source-pins.
- Optional backend: `/home/baidu/scrapanium-experiments/wss-owned-segments-20261007/backend-prefix-01`, DSO SHA-256 `2a0be84b6dc33d023b2b748163bc49a32352502e189d23012defcdb29167a439`, BoringSSL, upstream curl commit `6e8f87760a4dd96771e96fc9d55440dcd8845243`; backend manifest and both backend patches are pinned.
- Matched curl_cffi checkout: `/mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl_cffi`; exact source revision and package-tree hashes are in source-pins; version 0.16.4b1.
- Bend 2.0.27 commit `63bee70b55a71024d6bdcb49a745111bc54b114e`; Bun 1.3.11; Clang 21.1.8; Go 1.26.0; Python 3.14.4. Executable hashes, actual compiler helpers, curl headers, Clang builtin headers, loaded matched package files, and build environment are pinned. Runtime uses isolated Python bytecode and Go build caches. Final client binaries, generated C, and mapped DSO are hashed and rechecked.

The source-pins snapshot records 32 candidate changed paths, all candidate source/native inputs, full checker/workload helper equality with the baseline, the external optional backend, and the toolchain closure. The separately retained pre-final source-pins snapshot is historical only and must not be used for execution.



## Campaign 01 failure and campaign 02 version correction

Campaign 01 was authorized for one no-timing smoke. Its exact attempt is retained at `../owned-campaign-01/smoke-20261008-01`; status SHA-256 was `223268be5b8eb1548ddc84e5851c27e64ee5d7fcccc2dc85559100796c93caed`. It failed before build/client creation: `builds_started=false`, `attempts_started=false`, and no build directory or attempts were created. The failure was in the pinned Clang version check. The pin stores the version string without a final LF, while raw `clang --version` output ends with one. The original runner compared these byte-for-byte and raised `RuntimeError: Clang version differs from frozen source pins`.

The command's PowerShell `Tee-Object` target used a WSL `/home/...` path, which PowerShell interpreted as `C:\home\...`; this prevented a full outer terminal capture, although the runner recorded its exact argv in status. The capture issue and reproduced exception are retained outside campaign 01's frozen inputs and failed output:
- `../owned-campaign-01/smoke-20261008-01-terminal-note.txt`, SHA-256 `d927b8614de94f95d3533ddc2f9f80df1824b3268abefca611a506da60c91106`.
- `../owned-campaign-01/smoke-preflight-version-diagnostic-20261008-01.log`, SHA-256 `ef81d410a49e18bdfd854fdefbad09ac80fd8a0d2ab7721f5411eefa14af32de`.
- Read-only preceding-source/tool/DSO/CFFI/helper checks passed in `../owned-campaign-01/smoke-preflight-diagnostic-20261008-01.log`; no runner, plan, source pin, backend, workload, or product input was changed after that failure.

Campaign 02 changes only its isolated runner's Clang comparison to remove exactly one trailing LF before comparison. The raw output remains recorded in `bend.cc_version` and in `compiler_version_check` with the normalized comparison text and raw-output SHA-256. Its `--preflight-only` mode uses the same main preflight path and returns before creating build directories, clients, peers, or attempts. The integration regression invokes this actual CLI using the pinned compiler and asserts the raw LF, normalized pinned value, and explicit no-build/no-attempt/no-performance status. It is a validation-only path; no WSS smoke or timing campaign has run in campaign 02.

The candidate source pins, backend, profile, workload matrices, all checkers, balanced repeat order, seed, and acceptance gates are byte-identical to campaign 01. Campaign 02 has a distinct runner and output namespace; campaign 01's failed smoke and files remain immutable.

## Commands proposed for review

From the frozen campaign directory, this correctness smoke uses reduced counts (32 stream messages and 20 round-trip operations per workload), one repeat, and six corruption controls. It should produce 45 correctness attempts, retain full checkers, and omit timing fields. It has **not** been run.

```sh
wsl.exe --cd /home/baidu/scrapanium-experiments/wss-owned-segments-20261007/artifacts/owned-campaign-02 --exec /usr/bin/python3.14 run_owned_comparison.py --baseline /home/baidu/scrapanium-experiments/bend2-027-compat/worktree --candidate /home/baidu/scrapanium-experiments/wss-owned-segments-20261007/worktree --curl-cffi-checkout /mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl_cffi --bend-source /home/baidu/scrapanium-experiments/bend2-027-compat/compiler-2.0.27 --bun /mnt/c/Users/Baidu/Projetos/scrapanium/.deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun --curl-prefix /home/baidu/scrapanium-experiments/wss-owned-segments-20261007/backend-prefix-01 --cc /usr/lib/llvm-21/bin/clang --smoke --runs 1 --seed 20261008 --output /home/baidu/scrapanium-experiments/wss-owned-segments-20261007/artifacts/owned-campaign-02/smoke-20261008-02
```

The full fixed campaign, if separately released after smoke and artifact review, differs only in mode, repeat count, and output path:

```sh
wsl.exe --cd /home/baidu/scrapanium-experiments/wss-owned-segments-20261007/artifacts/owned-campaign-02 --exec /usr/bin/python3.14 run_owned_comparison.py --baseline /home/baidu/scrapanium-experiments/bend2-027-compat/worktree --candidate /home/baidu/scrapanium-experiments/wss-owned-segments-20261007/worktree --curl-cffi-checkout /mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl_cffi --bend-source /home/baidu/scrapanium-experiments/bend2-027-compat/compiler-2.0.27 --bun /mnt/c/Users/Baidu/Projetos/scrapanium/.deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun --curl-prefix /home/baidu/scrapanium-experiments/wss-owned-segments-20261007/backend-prefix-01 --cc /usr/lib/llvm-21/bin/clang --runs 12 --seed 20261008 --output /home/baidu/scrapanium-experiments/wss-owned-segments-20261007/artifacts/owned-campaign-02/timing-20261008-02
```

Both output paths must be absent before execution. Run only after independent source/plan review and root release, on an otherwise idle host, and run timing only after successful smoke review. No browser preview/profile capture or sanitizer build should overlap these timings.

## Focused correctness records

These are separate correctness checks, not benchmark attempts:

- Owned lifecycle, canonical/segmented boundaries, and byte equality checks: retained 22/22, 30/30, and 3/3 logs under `/home/baidu/scrapanium-experiments/wss-owned-segments-20261007/artifacts/`.
- Segmented malformed UTF-8 WSS checks at threads 1 and 4: `owned-segment-text-edges-final-20261008.log`; exact byte oracle and source hashes are retained adjacent to the log.
- Patched real `bufq.c` pooled and nonpooled pin/release ASan+UBSan harness: `backend-bufq-pin-asan-ubsan-02.log`; it compiles the patched source file itself. The built optional backend DSO is not sanitizer-instrumented.
- Real WSS `Ws.send` and `BinaryRequest` flatten-allocation fault cases: `/home/baidu/scrapanium-experiments/wss-owned-segments-20261007/artifacts/binaryrequest-flatten-oom-final-20261008-02/run.log` reports 2 passed (the two WSS cases; two WS cases deselected). The caller-PC `malloc` wrapper targets the retained `sp_bend_bytes_flatten` symbol at its 300,000-byte allocation. Both require native error code 2 and exact message `out of memory`; the request case supplies two nonempty headers and confirms the loopback HTTP server received no request. Generated Bend/native clients and wrapper are ASan/UBSan/LSan instrumented; custom curl DSO is not.
- The failed initial BinaryRequest compile is retained at `binaryrequest-flatten-oom-final-20261008-01/run.log`; it predates the corrected entry syntax and started no client/peer request.

## Pre-run tests and retention

The deterministic campaign unit suite passed all 20 tests in 5.467 seconds. It exercises schedule balancing, durable failed-attempt handling, exact source/preflight snapshot consumption, rejection of mutated checker/native files or backend DSO before builds/attempts, and the real --preflight-only CLI path with the pinned Clang raw newline. The command is in `unit-suite-final-20261008-01.command.txt` (SHA-256 `33b6d8f2c769cf35d80be6fe5481862b8d3a13cc32755f1f3953e56b6f5bc1f7`); output is in `unit-suite-final-20261008-01.log` (SHA-256 `35d8d7fd32786eadb1688e5d79d12a29463368c9d90f3fb41481dcf37de287d6`). The actual CLI regression also passed separately; its readable command is `preflight-integration-final-20261008-01.readable-command.txt`, and its output is `preflight-integration-final-20261008-01.log` (SHA-256 `9f3142e75ca3ee1da98eb6ff781abdd9bea8ba0a1ae88734b5111eefa14af32de`). A smoke or timing directory is always new; no previous attempt or failed artifact is overwritten. Before a released run, record the final PLAN, runner and source-pins SHA-256 values. After any run, independently audit attempt counts, full-check results, TLS/profile/DSO identities, all build and in-run pin checks, and acceptance bounds before describing evidence.
