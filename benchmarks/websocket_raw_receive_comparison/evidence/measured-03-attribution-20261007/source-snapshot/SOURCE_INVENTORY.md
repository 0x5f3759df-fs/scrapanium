# WSS receive diagnostic source and tool inventory

## Frozen inputs

Candidate worktree: /home/baidu/scrapanium-experiments/wss-raw-recv-probe-20260924/worktree, HEAD c4927171753a3cdcb12986f06c598219bba6e5b4, deliberately dirty and read-only. The frozen changes were in benchmarks/websocket_attribution.py, benchmarks/websocket_attribution_shim.c, docs/WEBSOCKETS.md, docs/WEBSOCKET_INTERNALS.md, native/websocket.inc.c, tests/test_websocket_operation_reuse.py, tests/websocket_operation_reuse.c, and diagnostic/test artifacts. Candidate native/websocket.inc.c SHA-256: 0f3c4e2c2844975f4bfd63adb1ba0bfb901f6b97deff640c6a80498653ad82f8. Candidate native/scrapanium.c SHA-256: d6dbad0e7671596b1c918f0c7f66936e396ecd3cc7da95e2786ac089796675ae.

Baseline worktree: /home/baidu/scrapanium-experiments/wss-raw-recv-comparison-20261005/baseline-c492717, same HEAD, clean and read-only. Baseline native/websocket.inc.c SHA-256: 0bc6766607ecfad0594d4a2df85b8247da678f4ab1cc0c9fca09504307082fb5. Baseline native/scrapanium.c has the same SHA-256 as candidate.

## Toolchain and external runtime inputs

- Private Bend 2.0.27 checkout: /home/baidu/scrapanium-experiments/bend2-027-compat/compiler-2.0.27, commit 63bee70b55a71024d6bdcb49a745111bc54b114e; compiler script, Bun runtime executable, revision, and clean tracked status are recorded.
- Bun 1.3.11, Clang Ubuntu 21.1.8 (6ubuntu1), Go 1.26.0 linux/amd64, and /usr/bin/python3.14 Python 3.14.4. Resolved executable paths and SHA-256 values are recorded before build and rechecked afterward.
- Clang's resolved -print-resource-dir/include tree is recursively hashed. The Go peer's actual package closure is enumerated with go list -deps -json under the same explicit offline environment used by go build; imported Go source files and the Go compile, link, and assembler tools are hashed. Build uses GO111MODULE=off, GOTOOLCHAIN=local, GOPROXY=off, CGO_ENABLED=0, GOENV=off, empty GOFLAGS, GOOS=linux, GOARCH=amd64, GOAMD64=v1, empty GOEXPERIMENT, a private diagnostic GOCACHE, and go build -trimpath.
- Matched Python environment is candidate build/local-site-packages-matched-20261005: curl_cffi 0.16.4b1, matched source checkout pin 8cd226f24a06f81d66c42fbdb2a7c49305b1b7b7. Provenance initializes and closes the matched Session before hashing, uses a fresh per-build PYTHONPYCACHEPREFIX with bytecode writes disabled, and asserts every loaded matched-site __cached__ path is inside that empty prefix and absent. This prevents reading existing site .pyc files. It hashes curl_cffi source/package files and loaded nonstdlib modules and records versions for all 24 distributions.
- Stock curl DSO: /mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl/libcurl-impersonate.so, SHA-256 bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3. All files under its external include directory are hashed before build and rechecked.
- Main published checkout was not built or edited. The diagnostic read the pinned stock curl DSO, its curl include headers, and the pinned Bun runtime through the existing dependency paths.

## Perf tool capability

- Kernel: 6.18.33.2-microsoft-standard-WSL2; Ubuntu 26.04 Resolute; kernel.perf_event_paranoid=2.
- Official Ubuntu linux-perf package 7.0.0-38.38 was downloaded and extracted only under this experiment root, not installed. Package SHA-256: c09e0183f04bab0a74015b0eb5055879ffa8c7b778ad5ab02224954bea2dfe46. Extracted perf 7.0.14 SHA-256: 1371ae5cb9fa59632d1d0e34b877d7251df4029810ba67dc56e946545e87b531. Its private shared-library dependencies are separately hashed.
- A synthetic own-process probe produced 395 cpu-clock:u samples over four seconds at 99 Hz with frame-pointer callchains and zero lost samples; cycles:u produced 296 samples over three seconds. This establishes tool availability only. No WSS boundary proof or client profile campaign has run.

## Diagnostic sources and scope

Root: /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007.

- runner.py compiles the same staged Bend diagnostic client against candidate and baseline native trees, plus the matched curl_cffi client path.
- src/benchmarks/wss_diag.bend and benchmarks/wss_diag_python.py preserve the 64 KiB byte-for-byte comparison and release path while reporting observer phases.
- benchmarks/websocket_attribution_shim.c is the dual raw/framed receive and wait-counter shim. benchmarks/websocket_perf_shim.c is the separate FD-controlled sampling shim. benchmarks/websocket_attribution_peer.go is the TLS peer with three client labels and a pprof floor aggregated across each client's four mode/flush labels.
- The source manifest hashes all staged .bend and .c files, src/native/bend_bridge.c, build/lab helpers, both clock bridges, Python helper, both shims, Go peer, schedule unit, fixed 72-capture profile driver, runner, controller, plan/inventory, and native source files in both frozen worktrees.
- The primary plan is 12 client/mode/flush cells x 12 fixed repeats = 144 positive rows plus 24 eight-message fault controls. Build flags are recorded by artifact type: Bend/native clients O3 plus debug/frame-pointer flags, attribution shim O2/debug without an explicit frame-pointer flag, and perf shim O3/debug/frame-pointer. Position is balanced by 12 circular rotations; carryover is not claimed. It is not run.
- Separate client perf profiles are planned as six variant/flush cells x 12 fixed captures = 72 full-workload rows. The perf_campaign.py driver creates a deterministic 12-rotation schedule that places each cell twice in every position, builds once on the first row, and verifies one shared build manifest plus all binary hashes before and after each reused capture. TLS files and profile artifacts remain under each row output. Failed captures are retained without retry or extension, and all 72 rows remain excluded from primary timings. A single 1,024-message candidate/flush-1 WSS window proof and a refreshed 36-row sanitizer smoke are prerequisites to root release. Neither has run.

## Existing outputs

- artifacts/asan-smoke-20261007-c is a historical completed 36-row preflight from before the expanded provenance closure: 12 positives, 24 negatives, no sanitizer markers, and one TLS version/cipher pair. It is not the final smoke.
- Failed preflights artifacts/asan-smoke-20261007-a and -b are retained unchanged.
- Failed proof preflight artifacts perf-window-proof-20261007-01 through -04 are retained. The first stopped before build on the Go version-probe flag. The second was interrupted after compilation while bytecode-cache provenance was being corrected; the third stopped before build because the fresh Python cache path was not allowlisted; the fourth was interrupted during compilation to pin GOAMD64 explicitly. No WSS client interval or perf.data capture was produced by these attempts.
- Earlier measured campaigns, frozen candidate/baseline inputs, and the published checkout remain untouched.
