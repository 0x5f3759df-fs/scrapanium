# Receive coalescing correctness evidence

This folder retains the final correctness-only direct-curl run for the frozen
candidate. It contains no throughput measurements. `commands.txt` records the
compiler/link and reproduction commands; `direct-correctness.log` and
`wrapped-faults.log` retain complete stdout/stderr, including the peer's decoded
frame captures.

The direct TLS suite passed for both baseline and candidate: varying receive
capacities and canaries, zero-capacity metadata, empty and queued frames,
fragmented data with interleaved PINGs, gated partial reads followed by fresh
input, and handler-close truncation. The peer observations matched byte for
byte between the two builds. The wrapped candidate tests passed for a false
pending hint, a stale hint followed by `CURLE_AGAIN`, zero-byte EOF, a fatal
receive status, and the bounded-drain case. Partial payload was returned before
EOF/fatal status, those deferred statuses were one-shot, and the connection
then resumed with the exact remaining frame and close handshake.

The deterministic bound test retained a full 2,048-byte frame at the peer. The
first wrapped receive obtained 68 wire bytes (the four-byte frame header plus
64 payload bytes), followed by four successful 16-byte reads while the pending
hook continued returning true. The first API call returned 128 payload bytes;
the next returned the exact remaining 1,920 bytes. The logged extra-read count
is four, independent of an initial pre-data `CURLE_AGAIN` attempt. This
exercises the hard bound introduced by using one `Curl_bufq_sipn` reader call
for each additional drain.

The split-PING observation is recorded as a known backend limitation, not a
conformance pass: both builds returned 6 of the 10 PING payload bytes to the
caller and sent the tail `ping` as their auto-PONG. Baseline and candidate
behavior matched; this coalescing check does not fix that separate behavior.

The source under test is `ws.c` SHA256
`9e67cfc98bd2e08de253fd9a09f57b06f1d6c5f5ab97780ac7e1da135946c058`. The
baseline and candidate shared-library hashes are respectively
`59460540f8fd6eab8eff22244e071c3132d7073f5be5a6dd9702b7c991315527` and
`242162132dde4cc83a9fb081bbaf2d9f4c12a15873bfa4b5c80f13f8c022e287`.

The full logs are hashed in `commands.txt`. The standalone fixture sources and
executables are also hashed there so the retained observations can be tied to
the precise direct tests and linker wrappers that produced them.
