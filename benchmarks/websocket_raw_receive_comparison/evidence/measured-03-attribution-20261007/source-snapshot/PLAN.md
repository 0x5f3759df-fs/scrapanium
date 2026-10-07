# WSS receive attribution diagnostic plan

Status: the isolated 3-variant runner and FD-backed perf controller are authored. The final source freeze, one full-workload WSS boundary proof, and one refreshed 36-row sanitizer smoke are pending. The 144-row primary matrix and 72 client-profile captures have not run. Root release and independent review are required before either campaign.

The experiment compares candidate Bend raw receive, clean baseline Bend framed receive, and matched curl_cffi framed receive against the same Go TLS peer and stock curl DSO. Each primary positive retains the original checker and corpus: 1,024 ordered binary messages of exactly 65,536 bytes, an eight-digit sequence prefix, full byte-for-byte response comparison, binary opcode validation, and immediate release of actual and expected buffers. The peer flushes after one or 64 messages.

The primary matrix crosses three clients, two instrumentation modes, and two flush sizes: 12 cells and 12 fixed repeats per cell, 144 positives. Each repeat contains all cells. Circular rotations make each cell occupy each of 12 positions once; this balances position, not first-order carryover. The schedule unit checks these properties. There are 24 eight-message fault controls, one corrupt and one swapped sequence per cell, with expected original rejection text.

All clients use the same attribution shim and interval. Control mode counts raw/framed receive and wait calls and records interval wall/thread/process clocks without per-call clocks. Attribution mode adds clocks around receive and poll/ppoll/select/epoll calls plus per-message receive/assembly and validation/release. These phases overlap and are not additive. The interval starts before the start control message and ends after full comparison and releases, before socket close. The peer records frames/bytes, TLS Write wall sum, send interval, process user/system CPU, and pprof labels.

The Go pprof floor is 80 samples aggregated per client variant across its four mode/flush labels (two modes x two batch sizes), not per cell. Under-floor aggregate results remain inconclusive. Peer stacks do not establish client-exclusive cost.

Candidate Bend uses raw curl_easy_recv; baseline Bend and curl_cffi use framed curl_ws_recv. The raw API includes TLS decryption and frame bytes; framed receive also parses WebSocket framing. No CPU residual will be labeled parser CPU. Attribute client samples only where symbols and DSOs identify them; retain unresolved frames as unknown. cpu-clock:u produces user-space stacks only.

The private official Ubuntu linux-perf package 7.0.0-38.38 (binary 7.0.14) is extracted under this experiment root, not installed. Synthetic own-process cpu-clock:u at 99 Hz resolved frame-pointer stacks with zero lost; cycles:u also recorded. This only proves tool availability.

The separate client profile campaign has six cells: three variants crossed with flush 1 and 64. It uses 12 fixed full-workload captures per cell, 72 total, each 1,024 x 65,536 bytes with the exact checker/release path in control mode. A deterministic set of 12 circular rotations places every cell twice in each schedule position. Captures are excluded from the 144-row observer-effect matrix. Perf uses cpu-clock:u at 499 Hz, --clockid mono, and frame-pointer callchains. Both Bend variants use -O3 -g -fno-omit-frame-pointer. The aggregate floor is 100 retained samples per variant/flush cell. Retain under-floor cells; do not extend or retry.

The fixed driver builds the Bend clients, attribution shim, perf-control shim, and peer once on the first scheduled capture. It verifies the shared build manifest and every binary before and after reuse. Each capture has its own output directory, certificate, key, and perf data. A failed capture is retained and never retried.

For each profile process, TLS setup and warmup finish before client ready. perf record -D -1 attaches while the client waits at the gate. A parent disable command must be acknowledged before gate release. The profile shim captures interval start, sends enable, and waits for perf's ack. At interval end it stops counters, sends disable, waits for ack, then captures the end. Markers use CLOCK_MONOTONIC and perf uses --clockid mono. Validate command/ack ordering, every retained sample timestamp within the saved interval (one microsecond print-resolution tolerance), and perf report's total lost-sample count. perf gets private LD_LIBRARY_PATH, empty DEBUGINFOD_URLS, and -N to prevent build-ID cache writes; the client receives its own stock-curl environment and its mapped DSO SHA is checked. Do not fetch symbols remotely; unresolved or stripped symbols remain unknown.

Before either campaign, run one separate profile-only WSS proof on candidate Bend, full count 1,024, flush 1. Exclude it from both campaigns. It checks the actual WSS perf boundary and event-loss reporting, not performance.

The runner pins and rechecks all staged Bend/C sources, including src/native/bend_bridge.c; diagnostic Bend/Python sources, clocks, shims, Go peer, schedule unit, fixed 72-capture profile driver, runner/controller, plan/inventory, and every native file in both frozen checkouts. It also pins the matched curl_cffi source package without .pyc files, loaded nonstdlib Python modules, all 24 matched distribution versions, and uses a fresh private Python bytecode-cache prefix with writes disabled; the provenance check confirms no loaded module cache exists there, all external curl headers, Clang's builtin headers, the Go source files returned by go list -deps for this peer, Go compile/link/assembler tools, and the Bun/Clang/Go/Python executables. Go list and go build share an explicit offline environment and private build cache. The Go peer uses go build -trimpath. Bend/native clients use Clang -O3 -g -fno-omit-frame-pointer in the ordinary runs; sanitizer clients use -O2 with ASan/UBSan. The ordinary attribution shim uses -O2 -g -Wall -Wextra -Werror -fPIC -shared without an explicit frame-pointer flag. The separate perf-control shim uses -O3 -g -Wall -Wextra -Werror -fno-omit-frame-pointer -fPIC -shared. Each flag set is recorded separately in the manifests. Bend revision/script, stock curl DSO, generated C, and final binaries are recorded. Inputs are checked before and after the sampled schedule. Output directories are exclusive; failed preflights stay preserved. Frozen source checkouts remain read-only.

Schedule unit:
  python3 /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/schedule_unit.py

Historical sanitizer smoke (completed before the expanded provenance closure; not the final smoke):
  env -u CC -u BEND_SOURCE -u BUN -u LD_PRELOAD -u SCRAPANIUM_CURL_DIR python3 /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/runner.py --smoke --sanitize --output-dir /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/artifacts/asan-smoke-20261007-c

It completed 36 rows (12 positive, 24 negative), all valid, no sanitizer markers, common TLS version/cipher. Failed preflights asan-smoke-20261007-a and -b remain preserved. Run one refreshed final 36-row smoke after source freeze:
  env -u CC -u BEND_SOURCE -u BUN -u LD_PRELOAD -u SCRAPANIUM_CURL_DIR python3 /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/runner.py --smoke --sanitize --output-dir /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/artifacts/asan-smoke-20261007-final-01

The failed proof preflight at perf-window-proof-20261007-01 is preserved; it stopped before build because the Go version probe used an unsupported flag. Attempt -02 was interrupted after compilation while the Python bytecode input closure was reviewed; no WSS window or perf capture was made. Attempt -03 failed before build because the fresh private Python cache directory was not included in the build-directory allowlist. Attempt -04 was stopped during compilation when GOAMD64 needed an explicit pin. These output/build directories remain preserved.

Fixed WSS proof command, pending:
  env -u CC -u BEND_SOURCE -u BUN -u LD_PRELOAD -u SCRAPANIUM_CURL_DIR python3 /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/perf_window_smoke.py --output-dir /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/artifacts/perf-window-proof-20261007-05

Fixed 72-capture client-profile command, pending root release:
  env -u CC -u BEND_SOURCE -u BUN -u LD_PRELOAD -u SCRAPANIUM_CURL_DIR -u LD_LIBRARY_PATH python3 /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/perf_campaign.py --seed 20261007 --output-dir /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/artifacts/client-profiles-20261007-01

Primary command, not run:
  env -u CC -u BEND_SOURCE -u BUN -u LD_PRELOAD -u SCRAPANIUM_CURL_DIR python3 /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/runner.py --seed 20261007 --output-dir /home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007/artifacts/primary-20261007-01

Do not start the 144-row matrix or 72 client-profile captures until root approves the frozen sources and independent review. Profiles do not create throughput or adoption claims.
