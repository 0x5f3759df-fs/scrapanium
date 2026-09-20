# Implementation validation

Latest full local run: **324 tests passed**, 287.73 seconds, Ubuntu 26.04 under WSL2.
The compiled implementation uses the Bend/source and backend pins in
`dependencies.json`. CI is configured but has not run on a hosted runner.

```sh
python3 scripts/bootstrap.py --matched
python3 scripts/build.py
.deps/venv-matched/bin/pytest -q tests --junitxml=build/test-results.xml
```

Bootstrap completed locally using verified curl archive bytes, exact Git commits,
Bun 1.3.11 and a curl_cffi source build linked to the same shared backend. Local
JUnit detail is generated at `build/test-results.xml` (ignored as a build artifact).

| Group | Tests | Evidence |
| --- | ---: | --- |
| Transport | 98 | Methods/binary uploads, pooling, cookie persistence, HTTP status handling, UTF-8/binary downloads, compression, chunking, duplicate headers, redirects, secret-header stripping, proxies, invalid inputs, size limits, timeout/truncation recovery, certificate trust/hostname/expiry, HTTP/2. |
| Fingerprints | 19 | Seven ClientHello reference comparisons, seven versioned capture regressions, three HTTP/2 wire comparisons, two custom-override wire tests. |
| Resource control | 30 | Retained allocation/count limits, decoded gzip limits, concurrent cancellation, wakeup while waiting for headers, session reuse, synchronous chunk sinks, atomic downloads and destination preservation on TLS/status/timeout/truncation/cancellation/filesystem failure. |
| Bend/native integration | 16 | Five pure laws, compiled batches with/without sanitizers, exact binary IO, affine/invalid-handle rejection, two native sanitizer stress programs, compiled TLS trust/hostname/expiry checks, nested config/boolean conversion, custom TLS wire capture, timer-driven cancellation, token lifetime/release, batch/download cancellation and a download-only effect subset. |
| Request builders | 44 | Differential UTF-8 query/form encoding, invalid scalars, duplicate/query/fragment handling, header validation and compiled wire requests. |
| Binary buffers | 12 | Exact file uploads, input limits, regular-file checks, byte access, zero-copy response transfer/reupload and affine rejection. |
| Structured response headers | 7 | Final blocks after redirects/CONNECT/103, case/order/duplicates/empty values, OWS trimming, unfolded fields and separate HTTP/1 and HTTP/2 trailers. |
| Profile catalog | 45 | Runtime catalog agreement, all 41 targets against verified HTTPS, Firefox 148/Chrome 152 preview comparisons with independent Go captures, explicit unsupported-name failure. |
| WS/WSS | 53 | Text/binary/empty/large frames, client masking, fragmented UTF-8 with interleaved pings, message limits, close, timeouts, cancellation/busy ownership, malformed handshakes, protocol errors, certificate trust/hostname/expiry, proxy tunneling and compiled Bend under sanitizers. |

The native stress harness makes 3,828 request attempts across repeated sessions,
including malformed header bytes and mixed-success batches, then 100 unsupported
profile constructions to exercise partial initialization cleanup. A second stress
program repeatedly cancels shared tokens across two worker threads, releases
ownership while operations finish, cancels inside body sinks, reuses sessions,
and exhausts batch budgets. It runs with
AddressSanitizer, UndefinedBehaviorSanitizer and leak detection enabled. Bend
batch/save/download/cancellation/configuration programs also execute under the sanitizers. The prebuilt transport
itself is not instrumented by those builds.

Actual external HTTPS was exercised once through the compiled Bend example at
`https://example.com`. Automated tests use only loopback fixtures and generated
local certificates. They do not depend on third-party fingerprint services.

## Findings resolved during development

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

The current five-client run measured 1.10–1.78x Bend throughput relative to
same-backend curl_cffi across six warmed loopback workloads. A separate three-run
memory probe measured 4.2 MiB median whole-process peak RSS for a 64 MiB streamed
file download, versus 68.0 MiB for buffering the same body in Bend. The download
path stayed around 4.1–4.2 MiB across the four tested body sizes. These are observed
measurements on this machine, not fixed resource guarantees; see
`benchmarks/MEMORY.md` and the raw sample/source-hash files.

Five shuffled WSS runs measured 5,988 Bend round trips/second versus 6,045 for
matched curl_cffi with a 30-byte binary payload. That is approximately equal
throughput for this loopback workload, not a general WebSocket speed claim.
Handshake/close are excluded; detailed caveats and raw samples are in
`benchmarks/WEBSOCKET.md`.
