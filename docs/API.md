# API reference


See [requests, headers, bytes and error handling](REQUESTS.md) and the
[WebSocket guide](WEBSOCKETS.md) for ownership examples and precise semantics.

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
See [the cancellation integration example](../tests/resources.bend).

Defaults allow 16 simultaneous transfers, at most 4096 requests per batch,
64 MiB decoded body per response, 256 KiB headers per response and 128 MiB
shared response allocations per call. The shared budget counts response
structures, retained buffer capacities and final URLs. It excludes request input,
Bend result/text copies, previously returned responses, allocator overhead,
temporary realloc copies and backend internals; it is not a process RSS cap.
Streaming downloads keep no body buffer but retain bounded headers. See
[resource semantics](RESOURCES.md) and [the download example](../examples/download.bend).

