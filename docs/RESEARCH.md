# Architecture evidence

Research date: 2026-09-24. Source commits are recorded below because default
branches and documentation change. The Bend row was rechecked for the current
compatibility pin; other rows preserve their inspected revisions. Scrapanium does
not claim that any upstream library's marketing claims have been independently
verified.

| Source | Inspected revision | Relevant finding |
| --- | --- | --- |
| [Bend2](https://github.com/bendlang/bend) | `63bee70b55a71024d6bdcb49a745111bc54b114e` (2.0.27) | Native C effects, one IO event loop, helper workers; Linux `io_wait` uses dynamically sized `select` descriptor sets; no stable foreign ABI. |
| [Bend effects guide](https://github.com/bendlang/bend/blob/63bee70b55a71024d6bdcb49a745111bc54b114e/guide/EFFECTS.md) | same | User-defined opaque handles unavailable; custom effects must wrap a Base handle. |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | `8cd226f24a06f81d66c42fbdb2a7c49305b1b7b7` | Direct curl-impersonate binding; sessions, async multi transport, browser profiles and detailed overrides. |
| [curl-impersonate](https://github.com/lexiforest/curl-impersonate) | `6e8f87760a4dd96771e96fc9d55440dcd8845243` | BoringSSL and HTTP/2 fingerprint patches; prebuilt v2.2.3 is pinned by archive digest. |
| [tls-client](https://github.com/bogdanfinn/tls-client) | `34718e1b514b446b95bc68dc4f096247e69c7939` | Go/uTLS and fhttp approach, explicit HTTP/2 settings/order, Chrome 152 and Firefox 148 entries present in source. |

## Decisions

Use the maintained curl-impersonate engine, not a fresh TLS implementation. It
provides a direct C interface, HTTP/2 multiplexing, connection reuse, mature URL
handling and browser profiles. A Go backend remains an interesting alternative
for custom ClientHello work, but adds runtime and packaging costs. The local
benchmark includes a pinned Go tls-client comparison; see `benchmarks/RESULTS.md`.
Its profile catalog is independently useful even when a benchmark favors curl.

The persistent multi handle owns connection pooling. A session serializes calls;
`batch` schedules up to its configured concurrency and preserves input order.
Cookie, DNS and TLS-session state are shared within that session. Every request
resets easy options before applying the immutable session profile. According to
[curl_easy_reset](https://curl.se/libcurl/c/curl_easy_reset.html), reset preserves
connections and several caches; integration tests verify reuse and header isolation.

Bend effects marshal request metadata and text, or transfer native byte buffers. Network IO runs on
Bend helper workers, not its event-loop thread. A batch uses libcurl multi on one
worker. This avoids a process or JSON bridge per request, but the worker wakeup
still adds measurable cost to sequential calls. This is not yet a direct
multi-socket integration into Bend's IO readiness loop.

Response bodies remain native byte buffers until `text`, `byte_at`, or `save`.
Bend String is a linked list of Unicode characters: returning every body as
String would add allocations and destroy arbitrary binary data. `text` decodes
UTF-8 with replacement; `save` and `byte_at` preserve exact bytes.
`download` streams decoded bytes to a temporary file on a worker, then commits
it by rename after successful HTTP 2xx completion. Push streaming in the native
API consumes each chunk synchronously. Shared response budgets and cancellation
semantics are specified in [RESOURCES.md](RESOURCES.md).

## Profile evidence and limits

Wire tests capture real ClientHello messages and compare cipher ordering,
extension sets, signature algorithms, supported groups/versions, key-share
groups, ALPN and certificate-compression settings with curl_cffi linked to the
same backend. Random bytes, GREASE values, Chrome's extension permutation, and
conditional RFC 7685 padding are normalized. Unnormalized extension order stays
in the capture. Safari/Firefox/Tor order is compared directly.

HTTP/2 tests independently record SETTINGS order/values, connection window
updates, pseudo-header order and regular header order after a verified local TLS
handshake. HTTP/1 success alone is not a fingerprint validation.

These tests establish binding parity with a reference implementation. They do
not establish exact current-browser fidelity, JA4 equivalence across every path,
HTTP/3 behavior, ECH negotiation, resumed-handshake fidelity, or a guarantee of
access to any website. Real-browser capture fixtures and profile lifecycle
policy remain release criteria.

The 2026-09-20 update adds Firefox 148 (independent Go ClientHello comparison),
a Chrome 152 trust-anchor preview with an explicit signature GREASE gap, and
connection tests for all 41 catalog targets. See [PROFILES.md](PROFILES.md).
WS/WSS reuses the same TLS engine with HTTP/1.1 upgrade. Our independent peer
caught missing acceptance-challenge validation in libcurl; the wrapper now
checks it, rejects unsolicited extensions/subprotocols and bounds complete
messages and operation deadlines. See [WEBSOCKETS.md](WEBSOCKETS.md).

## Security behavior

Certificate chain and hostname verification default on. Only HTTP/HTTPS URLs
and redirects are enabled. Request header control characters are rejected.
Bodies and headers have independently enforced limits, including decompressed
body size. Ambient proxy variables are ignored; proxies are explicit per session.
Authorization is stripped across origins by curl and exercised in tests.
Other custom headers follow curl's redirect policy and may reach a redirected
origin: callers must decide whether following redirects is appropriate.

The foreign C code is a trusted boundary outside Bend's pure proofs. The laws
check pure defaults/status behavior only; sanitizers and adversarial tests cover
the native boundary. No claim of a formally verified network stack is made.

The Bend bridge uses a slot/generation registry rather than embedding host
pointers in user-visible handles. An incorrect resource kind, stale generation
or a Base File mistakenly wrapped as a Session fails before native dereference.
Ordinary duplication is rejected by Bend's affine checker. Live, dropped handles
are reclaimed at process exit; long-running code must still close them promptly.
Configuration correctness is now tested through compiled Bend HTTPS requests,
not only the C transport. The pinned compiler's nonrecursive records are
flattened and booleans use numeric arm indices at this boundary. Constructor
arity assertions and native TLS override captures guard the marshalling contract.
