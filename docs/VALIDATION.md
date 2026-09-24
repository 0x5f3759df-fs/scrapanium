# Implementation validation

## Bend 2.0.27 compatibility check

Bend 2.0.27 (`63bee70b55a71024d6bdcb49a745111bc54b114e`) passed an isolated
stock-backend compatibility check on Ubuntu 26.04 under WSL2. The full local
pytest invocation reported **469 passed, 32 skipped** (501 collected, including
eight benchmark-plan unit tests) after the native library was built. The optional
browser/source-build cases account for skips. The detailed report records the
isolated worktree, compiler, build commands and artifact hashes.

Additional correctness-only gates were run separately: two post-pin streaming
sanitizer smokes used one and four threads, one repeat, six workloads and four
corruption/reordering controls per run; the phase and attribution sanitizer smokes
completed 24 rows each (16 negative controls and eight positive cases); and six
high-FD WSS sanitizer cases passed for exact fragmented receive, async
cancel/timeout and blocked-send cancel/timeout, each at one and four threads.
The high-FD test observed actual client sockets at FDs 1104–1106 before releasing
the test server's HTTP 101 gate. These smokes are correctness checks, not
performance measurements. Detailed compiler/build provenance and retained logs
are in the [Bend 2.0.27 compatibility report](compatibility/bend-2.0.27/README.md).

WebSocket operation-state reuse targeted validation on Ubuntu 26.04 under WSL2
(working tree based on local `48f4602`; this is not a full-suite run): the
optional browser backend passed 134 tests in 111.79 seconds across the
operation-state reuse sanitizer harness, native WebSocket API tests, generated
WS/WSS sequences and Bend readiness tests.
The run includes AddressSanitizer/UndefinedBehaviorSanitizer lifecycle checks
and compiled Bend checks with one and four threads.

The published hosted full-suite run at [`0f5176c`](https://github.com/0x5f3759df-fs/scrapanium/actions/runs/35492009948)
completed successfully for both stock and browser jobs. Its stock job reported
414 passed and 22 skipped. The browser job and its test-results artifact both
completed successfully, but its exact JUnit count has not been verified here.
Those hosted results predate WebSocket operation-state reuse. For the new
commit's full-matrix result, see the
[current CI workflow](https://github.com/0x5f3759df-fs/scrapanium/actions/workflows/ci.yml).

Previous complete local validation on Ubuntu 26.04 under WSL2 (historical run
before WebSocket operation-state reuse):

- **Stock backend: 406 passed, 22 skipped**, in 336.17 seconds. The skipped
  cases require the optional backend.
- **Browser backend: 430 distinct passing cases**: a 426-case full run in
  367.09 seconds, plus four installation and mixed-window checks added afterward.
  Both native sanitizer stress cases were also rerun with the optional headers.
- **WSS streaming sanitizers:** all six positive workloads and all four deliberate
  corruption/reordering controls passed with both one and four Bend threads on
  the source-built backend. These are correctness runs, not performance results.

The compiler and default backend pins are in `dependencies.json`; the optional
source build uses [backend/lock.json](../backend/lock.json). Hosted CI runs both
backend variants on the same Ubuntu 26.04/clang 21 target;
the [current workflow results](https://github.com/0x5f3759df-fs/scrapanium/actions/workflows/ci.yml)
report hosted validation separately.

```sh
python3 scripts/bootstrap.py --matched
python3 scripts/build.py
.deps/venv-matched/bin/pytest -q tests --junitxml=build/test-results.xml
```

Bootstrap completed locally using verified curl archive bytes, exact Git commits,
Bun 1.3.11 and a curl_cffi source build linked to the same shared backend. Local
JUnit detail is generated at `build/test-results.xml` (ignored as a build artifact).

Counts shown as stock/browser differ by backend; a single count applies to both.
The optional build skips the one stock-only unsupported-profile check. The
full-suite results above and unchanged group counts below are historical and
predate the Chrome 154 profiles. The Profile catalog and Actual browser captures
rows report the focused current run (47/51 and 4/25, stock/browser); the current
WebSocket collection has
138 cases: 134 from the four targeted files above plus four close-reason scalar
cases. This count includes eight generated WS/WSS sequences and the WebSocket
operation-state reuse lifecycle regression.

| Group | Tests | Evidence |
| --- | ---: | --- |
| Transport | 98 | Methods/binary uploads, pooling, cookie persistence, HTTP status handling, UTF-8/binary downloads, compression, chunking, duplicate headers, redirects, secret-header stripping, proxies, invalid inputs, size limits, timeout/truncation recovery, certificate trust/hostname/expiry, HTTP/2. |
| Fingerprints | 19 | Seven ClientHello reference comparisons, seven versioned capture regressions, three HTTP/2 wire comparisons, two custom-override wire tests. |
| Resource control | 30 | Retained allocation/count limits, decoded gzip limits, concurrent cancellation, wakeup while waiting for headers, session reuse, synchronous chunk sinks, atomic downloads and destination preservation on TLS/status/timeout/truncation/cancellation/filesystem failure. |
| Bend/native integration | 16 | Five pure laws, compiled batches with/without sanitizers, exact binary IO, affine/invalid-handle rejection, two native sanitizer stress programs, compiled TLS trust/hostname/expiry checks, nested config/boolean conversion, custom TLS wire capture, timer-driven cancellation, token lifetime/release, batch/download cancellation and a download-only effect subset. |
| Request builders | 44 | Differential UTF-8 query/form encoding, invalid scalars, duplicate/query/fragment handling, header validation and compiled wire requests. |
| Binary buffers | 15 | Exact file uploads, input limits, regular-file checks, byte access, zero-copy response transfer/reupload, affine rejection, exact length/byte equality with retained ownership, and checked Unicode conversion. |
| Structured response headers | 7 | Final blocks after redirects/CONNECT/103, case/order/duplicates/empty values, OWS trimming, unfolded fields and separate HTTP/1 and HTTP/2 trailers. |
| Profile catalog | 47 / 51 | Runtime catalog agreement, all 41 / 46 targets against verified HTTPS, Firefox 148/Chrome 152 preview comparisons with independent Go captures, explicit unsupported-name failure and rejection of optional profiles on stock builds. |
| Actual browser captures | 4 / 25 | Reparse historical and dated browser TLS/HTTP/2 artifacts; compare Chrome 153, CfT headless 153, Google Chrome 154, CfT headless 154, and Firefox 156 against both 156.0 and 156.0.1 captures; verify binary/UTF-8 WSS round trips and certificate rejection for all five profiles. |
| Backend controls | 0 / 7 | Option bounds, TLS handle duplication/reset, cache-safe connection reuse, and rejection of a stream window below the advertised initial setting before request headers. |
| Browser flow control | 0 / 2 | Three simultaneous 13 MiB+17-byte streams on one connection, exact contents and stream IDs, repeated with different per-request windows to check stream ownership. |
| Backend installation | 0 / 2 | Input/output manifest hashes and retained notices/source; load the moved installation in a fresh process with its original search path removed. |
| WS/WSS | 138 | Text/binary/empty/large frames, masking, ordered changing payloads and frame-length boundaries, fragmented UTF-8/binary with interleaved pings, bytewise network input, message limits, close, timeouts, cancellation/busy ownership, malformed handshakes, protocol errors, TLS trust/hostname/expiry and proxies. Eight generated sequences (four deterministic seeds over each of WS and WSS) check 18 messages apiece, arbitrary frame/write cuts and ping/pong ordering. Compiled Bend runs under sanitizers with 1/4 threads, including idle cancellation, 8 MiB blocked sends, exact payloads through 1 MiB, direct receive followed by allocation growth, checked close-reason scalars and fairness to timers. |

The change adds a native lifecycle sanitizer regression for the embedded state.
It checks parked-state stability, busy-gate preservation, transferred-buffer
ownership, and timeout/cancellation cleanup. This separate test passed in the
targeted 134-case run above; it is not included in the historical full-suite
totals.

The native stress harness makes 3,828 request attempts across repeated sessions,
including malformed header bytes and mixed-success batches, then 100 unsupported
profile constructions to exercise partial initialization cleanup. A second stress
program repeatedly cancels shared tokens across two worker threads, releases
ownership while operations finish, cancels inside body sinks, reuses sessions,
and exhausts batch budgets. It runs with
AddressSanitizer, UndefinedBehaviorSanitizer and leak detection enabled. Bend
batch/save/download/cancellation/configuration programs also execute under the sanitizers. The transport
backends themselves are not instrumented by those builds.

The WebSocket readiness checks exercise compiled Bend callbacks as well as the
native API. A negative control with the completion yield removed makes the
always-ready send loop finish before its forked timer; the production path
dispatches the timer first with both one and four worker threads. Review the
[operation invariants](WEBSOCKET_INTERNALS.md) when changing that path. This is
test and review evidence, not a formal proof of every possible network state.

Separate streaming gates run all six workloads under ASan/UBSan with one and
four Bend threads. Both clients must reject deliberate byte corruption and
reordered messages before positive cases run. Timing uses independently checked
monotonic clocks, and peer frame/byte totals are checked outside the timed loop.
These smoke runs are correctness evidence and never supply chart measurements.

Actual external HTTPS was exercised once through the compiled Bend example at
`https://example.com`. Automated tests use only loopback fixtures and generated
local certificates. They do not depend on third-party fingerprint services.

## Findings resolved during development

- The pinned Bend runtime can truncate an invalid Unicode scalar into different,
  valid UTF-8. `Bytes.from_text` and WebSocket close reasons now validate scalar
  ranges before encoding, including surrogates and high values whose bits would
  be lost. Sanitizer tests check exact valid bytes, embedded NUL and rejected
  invalid values through both APIs with one and four threads. Other foreign
  String conversions still use the upstream runtime; this fix is limited to
  these payload paths.

- A fresh hosted container passed 322 cases but could not build the Go reference
  probe because VCS stamping failed under checkout ownership. Comparison builds
  now use `-buildvcs=false`; profile source pins and benchmark source hashes remain
  explicit. This changes build metadata, not network behavior.

- libcurl accepted an incorrect WebSocket acceptance challenge. Scrapanium now
  validates it independently, requires exactly one acceptance field and rejects
  unsolicited subprotocols/extensions. The loopback peer computes challenges
  independently with Python hashlib and checks every client frame is masked.
- CONNECT_ONLY did not enforce the requested total upgrade deadline in our
  delayed-handshake test. The wrapper now applies its own monotonic deadline.
- A fingerprint probe's one-second connection budget intermittently expired
  before ClientHello. A 100-process diagnostic reproduced the timeout; the probe
  now allows five seconds to connect. Deliberate timeout tests retain short limits.
- Foreign effect names are prefixed with `Scrapanium` to avoid collisions with
  ordinary user functions such as `text`. A compiled byte-buffer test covers it.
- A generic Bool selection within an OR expression produced incorrect C for the
  encoder's `~` branch. Explicit boolean expressions avoid the construct; seeded
  Unicode differential tests verify query/form output against Python's encoders.

- **The initial Bend config bridge misread flattened record fields and booleans,
  including TLS verification.** C-only transport tests did not catch this.
  The corrected bridge reads all 24 physical fields and treats booleans as arm
  indices. Compiled Bend tests now reject untrusted certificates, trusted wrong
  hostnames and expired certificates, accept an explicit CA, and validate flags,
  limits and profile overrides. Constructor arity checks reject changed field counts.
  Benchmark measurements were rerun after this fix; older alpha measurements
  must not be treated as verified equivalent TLS behavior.
- Bend's `io_hand_v` macro evaluates its argument twice. Passing a handle-creation
  call directly duplicated the token registry entry; ASan caught the resulting
  cleanup use-after-free. The bridge now evaluates creation into a local first,
  and tests release shared cancellation tokens while workers finish.
- Bend's do blocks require helper functions for tuple destructuring.
- A foreign C file must guard helpers for effect/type subsets: a program using
  only `close` does not emit response constructor IDs.
- LLVM 21 can fail register allocation for Bend's `preserve_none` ABI with ASan
  at `-O1`; the verified sanitizer configuration uses `-O2`.
- ClientHello padding extension 21 varies with randomized ECH payload length,
  even within a single reference backend/profile. Normalization excludes this
  conditional padding while retaining raw sample order and GREASE counts.
- POSTFIELDS-backed custom methods need explicit 301/302 preservation; PUT,
  PATCH, DELETE and GET bodies now retain their method/body while 303 becomes GET.
- Local servers need a sufficient listen backlog for concurrency measurements;
  the lab uses 128 to avoid measuring SYN retransmission from a backlog of five.
- Benchmark certificates must use separate paths for separate server identities;
  each local TLS fixture now retains its own CA.

## What this does not prove

This is a tested alpha, not an exhaustive verification of all possible input,
all network behaviors or all upstream dependencies. Real-browser fidelity,
HTTP/3, session resumption, general Bend stream iterators, process-wide memory
bounds, cross-platform execution and long-duration reliability remain on the
roadmap. Current streaming/cancellation/resource guarantees and exclusions are
documented in [RESOURCES.md](RESOURCES.md).
Benchmark throughput, whole-process CPU and peak RSS evidence is documented
separately in `benchmarks/RESULTS.md`; it is not a general Internet speed claim.

The historical five-client run measured 1.10–1.78x Bend throughput relative to
same-backend curl_cffi across six warmed loopback workloads using Bend 2.0.17.
The Bend 2.0.27 compatibility check did not rerun performance measurements. A separate three-run
memory probe measured 4.2 MiB median whole-process peak RSS for a 64 MiB streamed
file download, versus 68.0 MiB for buffering the same body in Bend. The download
path stayed around 4.1–4.2 MiB across the four tested body sizes. These are observed
measurements on this machine, not fixed resource guarantees; see
`benchmarks/MEMORY.md` and the raw sample/source-hash files.

Seven shuffled WSS repetitions retain all 98 round-trip and 84 streaming
samples. Both clients check every byte and opcode inside timing. Independent
audits recomputed the rates and confidence intervals, checked source/binary and
mapped backend hashes, and verified 2,847,656 frames containing 3,939,058,200
payload bytes. Batched 30 B streaming measured 1.63M Bend messages/second versus
694k for matched curl_cffi: 2.35× by client medians, with a median paired ratio of
2.32× and a 95% interval of 2.30–2.38×. The single-connection Go / 64 KiB round-trip
case and both 64 KiB streaming cases remain inconclusive. All cases, uncertainty
and reproduction details appear in
[`benchmarks/WEBSOCKET.md`](../benchmarks/WEBSOCKET.md).
