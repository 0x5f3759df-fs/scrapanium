# Requests, headers and bytes

Import `scrapanium.bend` for IO and `http.bend` for pure builders.

```python
import ./scrapanium.bend as S
import ./http.bend as H

def search() -> S.Request:
  S.Request.with_query(S.Request.get("https://example.com/search"),
    [H.Param{"q", "Bend & TLS"}, H.Param{"tag", "one"}, H.Param{"tag", "two"}])

def login_form() -> S.Request:
  S.Request.form("https://example.com/session", [H.Param{"name", "héllo"}])
```

Query parameters preserve order and duplicates, encode UTF-8 with `%20` for
spaces, and are inserted before an existing fragment. Existing query text is
preserved. Form encoding uses `+` for spaces and sends the appropriate form
content type. Invalid Unicode scalar values become U+FFFD before encoding.
These are encoders, not a complete URL parser, resolver or IDNA library.

`Request.with_headers(request, H.Headers{[…]})` replaces its explicit header
list. `H.Header{name, value}` fields preserve case, order, duplicates and empty
values. Names use ASCII HTTP token syntax; values reject control characters
except horizontal tabs. Invalid fields cause send to fail with error 1.

`Headers.add` appends; `Headers.set` removes all case-insensitive matches then
appends; `Headers.remove` removes all matches. `Headers.get` returns the first
match, `Headers.get_all` returns all matches. This matters for `Set-Cookie`, which
must not be collapsed into a comma-joined value. Empty structured values send
empty fields; the raw request interface retains libcurl's `Name:` suppression
and `Name;` empty-field syntax.

`response_headers` returns only the final response block, excluding redirects,
informational responses and CONNECT. `response_trailers` returns final-message
trailers separately. Both preserve duplicates and trim surrounding HTTP
whitespace. `headers` returns the complete raw history for diagnostics.

## Binary data

`Bytes` owns native memory. `Bytes.from_list` accepts integers 0–255, and
`Bytes.from_text` encodes UTF-8 (including NUL). `Bytes.read_file(path, max_bytes)`
loads only regular files and rejects inputs larger than the limit; it never
uploads a partial prefix. It is a buffered file read, not streaming upload.

`Request.binary(method, url, bytes)` consumes that buffer into the request.
It also works in batches and cancelled operations. `into_bytes(response)` moves
the existing native allocation out of a response, consuming the response and its
metadata. Read status/headers first if needed. The public `size` field is
informational: native operations use the actual owned buffer length.

`Bytes.at` and `Bytes.text` return the buffer with the requested value; discard it
with `Bytes.discard`. Decode only when needed: Bend strings allocate Unicode
character lists, whereas raw bytes can remain in native memory.

Examples tested through compiled Bend: [request builders](../tests/builders.bend),
[binary file upload](../tests/bytes_file.bend), [response-buffer reuse](../tests/bytes_helpers.bend).

## Error codes

All effect failures carry `(U32, String)`; handle the code, keep the message for
diagnostics. HTTP statuses are successful transport results unless an operation
explicitly requires HTTP 2xx, as atomic file downloads do.

| Code | Meaning |
| ---: | --- |
| 1 | Invalid request/configuration/token |
| 2 | Allocation failure |
| 3 | Unsupported profile |
| 4 | Transport/TLS failure |
| 5 / 6 | Response body / header limit |
| 7 | Concurrent native operation on an owned session/socket |
| 8 | Backend failure |
| 9 | Aggregate response memory / batch count limit |
| 10 | Cancellation |
| 11 | File or stream sink failure |
| 12 | Download response was not HTTP 2xx |
| 13 | Input byte limit |
| 14 | WebSocket operation deadline |
| 15 | Invalid WebSocket handshake/message |
| 16 | WebSocket closed or unusable after I/O failure |

Generation-invalid native resource handles fail fast, rather than becoming a
recoverable request error. Never unwrap Scrapanium handles for Base File IO.
