<p align="center"><img src="docs/assets/banner.svg" alt="Scrapanium — native requests, browser TLS, Bend2" width="1120"></p>

<p align="center">
  <a href="https://github.com/0x5f3759df-fs/scrapanium/actions/workflows/ci.yml"><img src="https://github.com/0x5f3759df-fs/scrapanium/actions/workflows/ci.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/Bend-2.0.17-9f87dd" alt="Bend 2.0.17">
  <img src="https://img.shields.io/badge/profiles-41-efaa75" alt="41 profiles">
  <img src="https://img.shields.io/badge/transport-HTTP%20%2B%20WSS-78bfa6" alt="HTTP and WSS">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-9f87dd" alt="MIT"></a>
</p>

<p align="center"><a href="#quick-start">Quick start</a> · <a href="docs/PROFILES.md">Profiles</a> · <a href="docs/WEBSOCKETS.md">WebSockets</a> · <a href="benchmarks/RESULTS.md">Benchmarks</a> · <a href="ROADMAP.md">Roadmap</a></p>

# Scrapanium

Native browser-profile HTTP requests for **Bend2**. Early development release.

Scrapanium connects Bend directly to pinned curl-impersonate/BoringSSL. It offers
pooled sessions, concurrent batches, browser TLS/HTTP/2 profiles, explicit profile
overrides, cookies, redirects, proxies, certificate verification and binary-safe
response buffers, shared batch budgets, cancellation and atomic streaming file
downloads. **TLS WebSockets**, binary uploads, query/form encoding, and structured
multi-value headers are included. No Python runtime or per-request subprocess is
used by Bend.

The ambition is a dependable requests library for Bend. Production readiness and
superior real-browser fidelity are goals, not claims of this initial version.

## Quick start

Currently validated on Linux x86_64 (Ubuntu/WSL2), Bend 2.0.17 at the commit in
`dependencies.json`, and curl-impersonate 2.2.3. Native C output only; the Bend JS
target is not implemented. Bootstrap needs Python 3.12+, npm, git and clang 21;
the full test suite also needs Go 1.26. Ubuntu 26.04 supplies the tested clang.

```sh
# Ubuntu prerequisites, once:
sudo apt-get install clang build-essential python3-venv python3-dev libffi-dev time

git clone https://github.com/0x5f3759df-fs/scrapanium.git
cd scrapanium
python3 scripts/bootstrap.py --matched
python3 scripts/build.py examples/get.bend -o build/get
./build/get --threads 1
```

The build helper emits Bend C and links the transport. Plain `bend example.bend`
does not supply those linker options. Downloads remain inside `.deps/`; the
transport archive is checked against its pinned SHA-256. Bend and the comparison
curl_cffi source are checked out at exact commits.

```python
import Base
import ./scrapanium.bend as S

def print_body(pair: S.Response & String) -> IO(Unit):
  (response, body) = pair
  do IO<Unit>:
    IO.print(body)
    S.discard(response)

def main() -> IO(Unit):
  do IO<Unit>:
    response : S.Response <- IO.try(S.Response, S.get("https://example.com"))
    pair : S.Response & String <- S.text(response)
    print_body(pair)
```

For repeated requests, use `open` once, pass the returned session through `send`
or `batch`, then `close`. The session is returned even when a request fails.
`get`/`post` are one-shot conveniences; `Request.get`/`Request.post` build requests
for pooled sessions. See [the pooled example](examples/get.bend) and
[concurrent batch example](tests/integration.bend).

## API

See [requests, headers, bytes and error handling](docs/REQUESTS.md) and the
[WebSocket guide](docs/WEBSOCKETS.md) for ownership examples and precise semantics.

| Function/type | Behavior |
| --- | --- |
| `profiles()` | Lists 41 versioned targets; see the evidence and preview limitations in the profile catalog. |
| `Ws.connect`, `Ws.send`, `Ws.receive`, `Ws.close` | Verified WSS, binary/text messages, fragments, control frames, deadlines and cancellation. |
| `Request.with_query`, `Request.form` | Ordered UTF-8 URL/form encoding; duplicates and fragments preserved. |
| `http.bend: Headers` | Case-insensitive lookup, duplicate-preserving fields, validation and explicit empty values. |
| `response_headers`, `response_trailers` | Final-response structured headers and separate HTTP/1 or HTTP/2 trailers. |
| `Bytes.from_list/from_text/read_file`, `Request.binary` | Binary-safe uploads; bounded regular-file reads. |
| `into_bytes(response)` | Moves the native body buffer for reuse without copying it. |
| `defaults()`, `browser(name)` | Secure configuration; explicit pinned `chrome150` default. |
| `open(config)` | Returns `Result<..., Session>`. Unknown profile fails explicitly. |
| `send(session, request)` | Returns session plus response/error; does not block the Bend event loop. |
| `batch(session, requests)` | Bounded concurrency, ordered per-request results, connection reuse. |
| `Limits{batch_bytes, batch_requests}` | Shared retained-response allocation/count bounds per operation; stored in `Config.limits`. |
| `cancel_new()`, `cancel(token)`, `cancel_release(token)` | Create, trigger and release a shared one-shot cancellation token. Each returns a result. |
| `send_cancel`, `batch_cancel` | Corresponding request operation with a final token argument. |
| `download(session, request, path)` | Streams to disk; returns session plus `Result<..., DownloadInfo>`. Atomically replaces path only on HTTP 2xx and successful transfer/close. |
| `download_cancel(session, request, path, token)` | Cancellable streaming download with the same file guarantees. |
| `fetch(config, request)`, `get(url)`, `post(url, text)` | One-shot calls with automatic session close. |
| `Request{method, url, headers, body}` | Ordered header lines; UTF-8 upload text, including embedded NUL in the body. |
| `Response{handle, status, size, version}` | Owned native buffer plus metadata. Treat handle as opaque. |
| `text(response)` | Returns response plus UTF-8 text; invalid sequences use replacement characters. |
| `headers(response)` | Returns response plus raw header blocks, including redirects and duplicate headers. |
| `effective_url(response)` | Returns response plus final URL. |
| `byte_at(response, index)` | Returns response plus optional exact byte. |
| `save(response, path)` | Writes exact bytes and returns response plus result. Replaces an existing file. |
| `discard(response)`, `close(session)` | Release native resources promptly. |
| `backend()` | Actual linked transport/TLS version string. |
| `is_success(status)` | HTTP status in 200–299; HTTP 4xx/5xx are ordinary responses. |

`Result` expands to `Result<&1, &1, U32 & String, T>` in Bend. Numeric HTTP version
values come from curl: 2 = HTTP/1.1, 3 = HTTP/2. `Response.size` is decoded body
bytes. Do not infer body text length from it.

Both sessions and responses are affine. Bend rejects ordinary duplication.
The bridge additionally validates resource kind and generation before accessing
native memory. Explicitly close/discard resources; forgotten handles are reclaimed
at process exit, but a long-running program should never rely on that fallback.
Do not unwrap a handle to use with Base's File effects.

`CancelToken` is copyable so separate IO tasks can share it. Pass it through a
`+token: S.CancelToken` parameter when reusing it in Bend. Triggering is sticky;
create a fresh token for another operation. Release exactly once after use.
Releasing invalidates every copy; an already-dispatched operation keeps its own
reference. Releasing alone does not cancel. A stale token returns error 1.
See [the cancellation integration example](tests/resources.bend).

Defaults allow 16 simultaneous transfers, at most 4096 requests per batch,
64 MiB decoded body per response, 256 KiB headers per response and 128 MiB
shared response allocations per call. The shared budget counts response
structures, retained buffer capacities and final URLs. It excludes request input,
Bend result/text copies, previously returned responses, allocator overhead,
temporary realloc copies and backend internals; it is not a process RSS cap.
Streaming downloads keep no body buffer but retain bounded headers. See
[resource semantics](docs/RESOURCES.md) and [the download example](examples/download.bend).

## TLS and HTTP/2 profiles

All **41 catalog profiles** are tested against verified HTTPS. Selected Chrome,
Safari, Firefox and Tor profiles also have ClientHello/HTTP/2 comparisons against
curl_cffi. The added `firefox148` matches an independent tls-client ClientHello;
`chrome152_preview` adds trust anchors but has a known signature GREASE gap.
`chrome152` remains rejected to keep that distinction explicit. See the
[profile coverage and update policy](docs/PROFILES.md).
Current Chrome 153, Firefox 156 and Safari 27 captures remain active work; this
alpha does not yet provide complete latest-browser coverage.

`Config.fingerprint` accepts `Fingerprint{ciphers, curves, signature_algorithms,
extension_order, http2_settings, pseudo_header_order, cert_compression, grease,
permute_extensions, window_update}`. Empty strings and window 0 inherit the
named profile. Bend toggle values are 0 = inherit, 1 = disabled, 2 = enabled;
the C API uses -1/0/1. Use `Fingerprint.inherit()` for stock profiles.

The string formats are those of curl-impersonate: colon-separated cipher/curve
names, dash-separated TLS extension IDs, semicolon-separated HTTP/2 `id:value`
settings, and pseudo-header order such as `masp`. Overrides create a custom
fingerprint; matching a named browser is no longer implied.

See [research and evidence](docs/RESEARCH.md) for the backend choice, reference
sources, wire test coverage, and the boundary between Bend proofs and native IO.

## Tests and benchmarks

**324 tests pass locally**, including 53 WS/WSS cases and sanitizer coverage.
Recorded on 2026-09-20 with one Bend compute thread on Linux/WSL2:

| Measurement | Observed result | Evidence |
| --- | --- | --- |
| Six warm HTTP workloads | 1.10–1.78× throughput of matched curl_cffi | [HTTP results](benchmarks/RESULTS.md) |
| Small-message WSS | 5,988 vs 6,045 round trips/s for matched curl_cffi | [WSS results](benchmarks/WEBSOCKET.md) |
| 64 MiB response | 4.2 MiB peak RSS streaming to file; 68.0 MiB buffered | [Memory probe](benchmarks/MEMORY.md) |

These are repeated loopback measurements with explicit limits, not Internet
performance guarantees. Raw timings, source hashes and dependency pins are included.

```sh
python3 scripts/build.py
.deps/venv-matched/bin/pytest -q tests
.deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun .deps/bend/bend2/main.ts PROOF.bend --check-only
.deps/venv-matched/bin/python benchmarks/run.py --runs 5 --count 1000
.deps/venv-matched/bin/python benchmarks/websocket.py
```

To include the Go performance comparison, add `--tls-client`. The Go
module and transitive checksums are pinned under `benchmarks/tls-client/`.

The matched test environment links curl_cffi against the **same shared backend**.
Running parity tests against the older release wheel is not equivalent: it lacks
some profiles and uses an older curl. Benchmarks include both baselines and the
native C core. Raw timings, versions and limitations are saved in
`benchmarks/results/local.json`. See the [generated results report](benchmarks/RESULTS.md).
Run benchmarks without simultaneous builds/tests.

The separate [memory scaling probe](benchmarks/MEMORY.md) compares buffered and
file-streamed Bend responses from 64 KiB to 64 MiB. Reproduce it with
`.deps/venv-matched/bin/python benchmarks/resources.py --runs 3`.

The [WSS benchmark](benchmarks/WEBSOCKET.md) compares repeated verified-TLS
round trips against matched curl_cffi, with raw runs and source hashes.

The test lab uses loopback HTTP/1, HTTPS, HTTP/2, WS/WSS and a CONNECT proxy. It verifies
actual ClientHello and HTTP/2 behavior, malformed requests, decompression limits,
timeouts, redirects, pooling, cookies, exact binary IO, ownership, and compiled
Bend execution, certificate checks through the compiled bridge, cancellation,
aggregate allocation limits and atomic downloads. Sanitizer tests cover our C core/bridge; the downloaded transport
is not rebuilt with sanitizers. LLVM 21's `preserve_none` ABI fails on one ASan
build at `-O1`; sanitizer builds use `-O2`, which passes.

## Current limits

- Batch buffers all results until it returns, subject to the shared budget.
  General Bend chunk iterators are pending; current streaming is to disk from Bend
  or to a synchronous push callback from C.
- HTTP/3 is present in the backend build but is not an exposed, verified feature.
- Automatic retries, multipart, JSON conveniences, cookie import/export and
  streaming uploads are pending. Binary file uploads currently use bounded buffers.
- WebSockets use HTTP/1.1 upgrade and complete-message buffering; permessage-deflate,
  HTTP/2 extended CONNECT and background heartbeat scheduling are pending.
- Session configuration is immutable; use separate sessions for different proxies
  or profiles. One owner may call a session at a time. `batch` provides concurrency.
- Native resources and network behavior are not mathematically proven. Initial
  laws cover defaults/status behavior; see `LAWS.bend` and `PROOF.bend`.
- Relocatable packaging, macOS/ARM validation, real-browser golden captures,
  long-duration stress and broader benchmarks remain release work.

See [ROADMAP.md](ROADMAP.md). Dependency notices are in [THIRD_PARTY.md](THIRD_PARTY.md).

Built on [Bend](https://github.com/bendlang/bend),
[curl-impersonate](https://github.com/lexiforest/curl-impersonate) and BoringSSL;
tested against [curl_cffi](https://github.com/lexiforest/curl_cffi) and
[tls-client](https://github.com/bogdanfinn/tls-client). Profile-data attribution:
Bogdan Finn / tls-client. Upstream licence acknowledgement: “This product includes
software developed by the &lt;organization&gt;.” See the retained notices above.
