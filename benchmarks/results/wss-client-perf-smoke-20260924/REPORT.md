# WSS client perf correctness smoke

The fixed smoke completed all 48 planned attempts: 16 corruption/swap controls
and 32 positive sessions. Every row passed. This archive is a correctness and
artifact-format result only; it contains no throughput result and does not meet
the full diagnostic's sample floor.

Each positive session checked eight ordered binary messages with exact sequence
and payload bytes, then released the received and expected buffers. All 32
positive peer records show eight frames, 524,288 payload bytes, no peer error,
TLS version 772 and cipher 4865. The 16 negative controls all returned the
expected client rejection for corruption or swapped sequence. The schedule has
four rows for each client × sampling mode × flush batch positive cell, with all
controls executed before positives.

All 48 clients mapped the pinned stock RELEASE backend at
.deps/curl/libcurl-impersonate.so.4.8.0, SHA-256
bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3. The
peer negotiated the same TLS identity in every row. All actual perf header
checks passed, including cpu-clock:u, 99 Hz, monotonic timestamps, inherited
events, user-only sampling, and a 16 KiB DWARF stack. All rows recorded the
expected two control commands: ping followed by enable or disable; there were
no unparsed acknowledgement tokens. Source, binary, binding, and tool input
hashes were unchanged across the run.

The smoke's 16 profiled positive sessions each sent only eight messages, and
the retained profile summaries show zero selected in-window samples. The
predeclared 80-sample floor applies to the 96-repeat full schedule, not this
short smoke. No full profile is included in this archive. The raw data, headers,
decoded scripts, stdout/stderr, row records, plan, and manifest are retained
for inspection.

## Retained acknowledgement failure

The first smoke attempt, negative-01-flush1-scrapanium-bend-disabled-corrupt,
is preserved under first-framing-failure/. Perf's
acknowledgement was framed as ack followed by newline and NUL. The original
runner accepted the ping response but treated the next response as an
unsolicited NUL-prefixed token, so the disable acknowledgement timed out. The
exact runner source from that attempt and its profiler/control outputs are
retained beside it. The corrected runner splits acknowledgements on both
terminators; the sequential, fragmented-read regression is in
tests/test_websocket_client_perf_lifecycle.py.

For the complete fixed schedule and limits, see
[the WSS client CPU profile diagnostic](../../WSS_CLIENT_PERF.md). The
archive's byte hashes are listed in [SHA256SUMS.txt](SHA256SUMS.txt).
