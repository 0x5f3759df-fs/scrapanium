# Scrapanium goal and release gates

Goal: a production-quality requests library for Bend2, competitive with
curl_cffi on performance and stronger in measured profile fidelity/maintenance.
The goal remains active; this repository is an initial working implementation.

## Implemented foundation

- Native C integration with pinned Bend2; persistent curl multi/session transport.
- Ordered concurrent batches; cookies, redirects, proxies, TLS verification.
- Bounded binary buffers, explicit UTF-8 conversion, exact file save.
- Query/form encoding, structured request/response headers and separate trailers,
  bounded binary uploads and response-buffer transfer.
- WS/WSS with binary/text messages, fragmentation, ping/pong, closing handshakes,
  acceptance verification, certificate checks, limits, timeouts and cancellation.
- Shared retained-response allocation/count limits, thread-safe cancellation,
  native push sinks and atomic streaming file downloads from Bend.
- Browser TLS/HTTP/2 profile selection and explicit custom fingerprint overrides.
- 41 stock profiles plus optional Chrome 153, Chrome for Testing 153 headless,
  Google Chrome 154, Chrome for Testing 154 headless, and Firefox 156 profiles
  with retained Linux browser captures; source-built TLS controls and HTTP/2
  stream-window/stream-ID controls.
- Firefox 148 reference comparison and an explicitly limited Chrome 152 preview.
- Affine wrappers with generation-checked native handles and exit cleanup.
- Pure API laws, loopback tests, fingerprint parity tests, sanitizer integration.
- Reproducible comparisons with curl_cffi release, the same-backend source build,
  and pinned Go tls-client; whole-process memory and CPU measurements.

## Before a production release

1. Complete the everyday request API: JSON conveniences, cookies import/export,
   streaming uploads and multipart. Add examples showing error paths and cleanup.
2. Build general Bend chunk iterators on the current native push sinks and file
   downloads. Define retry semantics for idempotent methods. Extend fairness and
   cancellation tests to slow DNS/proxy, paused consumers and HTTP/2 multiplexing;
   test memory scaling beyond the retained-response accounting already enforced.
3. Capture actual versioned browsers on multiple OSes. Track provenance and
   retirement dates per profile. Test resumed TLS, extension permutation, GREASE,
   ECH, HTTP/2 stream behavior; evaluate HTTP/3 without claiming untested fidelity.
   The optional backend now exposes signature GREASE; Chrome 152 remains a
   separately limited mixed-source preview. Validate current Safari/Android captures and WS-specific
   handshakes. Add WebSocket compression and broader RFC conformance/fuzz coverage.
   Chrome 154 and Firefox 156.0.1 now have scoped Linux evidence. Safari 27 and
   current browsers on other platforms remain immediate targets;
   see the dated primary sources and coverage gap in `docs/PROFILES.md`.
4. Expand the initial comparison with curl_cffi and tls-client: latency distributions,
   throughput/concurrency curves, memory/CPU scaling, cold TLS, large bodies,
   text/JSON decoding, WAN-like latency, and server-independent checks.
5. Run long-duration stress, sanitizer fuzzing with fault injection, reproducible
   dependency audits, ABI compatibility checks, and Linux/macOS/ARM CI.
6. Ship relocatable dependency packaging and release documentation. Review the
   backend's bundled licenses, artifact hashes and upgrade process before release.

## Claims policy

- "As fast" requires matching semantics and workload, raw repeated measurements,
  machine/compiler/backend versions, and discussion of any slower cases.
- "Better profiles" requires real-browser evidence, not User-Agent changes or
  equivalence to a reference binding alone.
- Never silently map an unsupported profile to another browser.
- Never declare the goal complete solely because an alpha compiles or tests pass.
