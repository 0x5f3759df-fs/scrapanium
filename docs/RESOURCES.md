# Resource control and streaming

These APIs are implemented for the pinned native Linux target. Each operation
owns its session until it returns; use separate sessions for independent tasks.
Network and streaming-file work runs on Bend's IO workers. Configuration and
result marshalling run on its event loop.

## Shared response budget

`Config.limits = Limits{batch_bytes, batch_requests}` defaults to 128 MiB and
4096 requests. Native fields are `max_batch_bytes` and `max_batch_requests`.
The byte limit must fit at least one native response structure; smaller values
are invalid configuration. Both limits must be positive.

At the start of a batch, metadata capacity is reserved for every response.
Every body/header growth is charged by its allocation capacity, including the
terminating NUL, rather than only bytes received. Effective URL copies are also
charged. Completed responses stay charged until the operation returns because
the batch retains them. Each call starts a new budget. Input order is preserved;
which requests exhaust a shared budget depends on completion order.

This bounds retained native response allocations. It does not bound total RSS:
caller input, native request marshalling, Bend strings/result lists, earlier
responses still owned by the caller, allocator overhead/transient realloc copies,
session pools and libcurl/TLS/decompression allocations are outside it.

Error 9 means aggregate allocation/count exhaustion. Per-response decoded body
and raw header limits still apply independently (errors 5 and 6). In C, an invalid
argument or count/metadata preflight failure leaves the output array untouched;
initialize it to NULL before calling. After an accepted batch, every non-NULL
response, including errors, belongs to the caller and must be freed. Bend returns
an error for each affected item and releases failed native responses itself.

## Cancellation

`cancel_new()` returns a copyable `CancelToken`. `cancel(token)` is idempotent,
sticky and safe while one or more operations use that token. The cancellable
operations are `send_cancel`, `batch_cancel` and `download_cancel`. A pre-triggered
token prevents a network transfer. Cancellation of a batch aborts its active and
queued work; responses that already completed retain their original result.

`cancel_release(token)` invalidates every Bend copy and releases the registry's
reference. In-flight jobs retain their own reference before worker dispatch, so
immediate release after triggering is safe. A released token cannot be reused;
stale token operations return error 1. Token fields are implementation details,
not an isolation/security boundary between mutually untrusted Bend code.

The C token uses explicit `sp_cancel_retain`/`sp_cancel_free`; each thread must
keep an owned reference while using the pointer. Operations also hold a reference
while attached. Triggering sets an atomic flag and wakes every attached multi
handle using libcurl's [thread-safe wakeup API](https://curl.se/libcurl/c/curl_multi_wakeup.html).
Cancellation is cooperative: it cannot interrupt a currently executing sink,
blocking filesystem call or synchronous backend computation. A completion can
win a race with cancellation. No fixed cancellation latency SLA is promised.

## Streaming and file replacement

`download`/`download_cancel` stream decoded bytes to a newly created private
temporary file next to the destination. No body buffer accumulates in Scrapanium.
The returned `DownloadInfo{status, bytes, url, headers}` contains final HTTP
status, decoded byte count, final URL and raw header blocks. The total decoded
body limit remains in effect, even for compressed downloads.

Only transport success, HTTP 2xx and successful file close permit an atomic
rename over the destination. Failures remove the temporary file and preserve
the destination. An HTTP 4xx/5xx download is error 12; ordinary `send` still
returns such statuses as responses. Disk/sink failures use error 11. Cancellation
uses error 10. Rename is the commit point: a cancellation racing after the final
check may lose to a successful rename. The new file has the temporary file's
private permissions; existing destination permissions are not preserved.

This guarantees replacement atomicity, not durability across power loss.
There is no file/directory fsync, resume/append, or automatic retry. A process
crash can leave a temporary file; normal returned failures clean it up. Parents
must already exist, and the caller chooses a trusted destination directory.

For native consumers, `sp_session_stream` accepts a synchronous push callback.
Return zero after consuming the entire chunk; a nonzero return aborts with error
11. The pointer is valid only during the callback. Do not throw across C or
destroy the active session. Slow callbacks provide backpressure on the calling
worker. `sp_response_size` is zero for a stream; use `sp_response_downloaded`
for accepted decoded bytes. Failed streams may already have delivered a prefix.

The implementation does not use `CURLPAUSE` to claim bounded streaming memory:
libcurl documents additional buffering for paused HTTP/2 and compressed
transfers in its [pause API](https://curl.se/libcurl/c/curl_easy_pause.html).
General pull-style Bend chunk iterators remain future work.

## Bend compiler boundary

Binary uploads move a `Bytes` allocation into the native worker. Inputs are
outside the retained-response budget. `Bytes.read_file` has a separate explicit
input limit and rejects directories/FIFOs. `into_bytes` transfers a response
buffer without a body copy. Structured-header conversion and text decoding
allocate Bend values outside the native response budget.

WebSocket receive buffers one complete message, bounded by `max_body_bytes`,
plus at most one 125-byte control payload and a 16 KiB stack receive chunk.
Backend/socket buffers and Bend result values are additional memory. One socket
owns its own session; there is no aggregate budget across multiple sockets.
See [WebSocket ownership and cancellation](WEBSOCKETS.md).

The pinned Bend compiler flattens nonrecursive record fields at the foreign
boundary. `Config` therefore occupies 24 runtime words: 12 scalar fields, 10
fingerprint fields and two limit fields. Booleans are arm indices, not boxed
constructor IDs. The bridge checks constructor arity before reading/writing
records and is compiled with the pinned toolchain. A compiler upgrade requires
rechecking layouts and running the compiled HTTPS/configuration tests.
