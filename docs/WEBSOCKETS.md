# WebSockets over TLS

`Ws.connect(config, "wss://…")` creates a dedicated connection using the same
browser profile, custom fingerprint settings, proxy, CA and certificate checks
as HTTP. The upgrade uses HTTP/1.1. HTTP/2 extended CONNECT and permessage-deflate
are not implemented. WebSocket-specific backend TLS settings are retained;
an HTTPS fingerprint is not assumed to be identical to a WSS fingerprint.

```bend
socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.defaults(), url))
data : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text("hello"))
pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 1, data, 5000)
```

Destructure `pair` in a helper, handle its result, and pass the returned socket to
`Ws.receive(socket, 5000)`. It returns the socket and `Result<…, WsMessage>`.
The [complete compiled example](../tests/websocket.bend) demonstrates ownership,
UTF-8 conversion and graceful close.

| Operation | Behavior |
| --- | --- |
| `Ws.connect_with(config, url, headers, token)` | Structured handshake headers and cancellable upgrade; set Origin/subprotocol here. |
| `Ws.send(socket, kind, bytes, timeout_ms)` | Consumes bytes; sends text (1), binary (2), ping (9), or pong (10). |
| `Ws.receive(socket, timeout_ms)` | Complete reassembled text (1), binary (2), or close (8) message. |
| `Ws.send_cancel`, `Ws.receive_cancel` | Same ownership with an additional shared cancellation token. |
| `Ws.close(socket, code, reason, timeout_ms)` | Sends close, waits for the peer, releases the socket on success or failure. Invalid Unicode scalars in the reason return an error before sending. |
| `Ws.close_cancel` | Cancellable close with the same cleanup guarantee. |
| `Ws.discard(socket)` | Immediate release without a closing handshake. |

`Config.max_body_bytes` bounds each incoming and outgoing message. Receive
buffers one complete message; it is not a chunk stream. The header limit applies
to the upgrade. Timeouts cover the entire operation, including fragmented
messages and interleaved controls. Receive answers pings and discards pongs;
there is no background heartbeat while the application is idle. Cancellation
is checked between chunks and at most every 50 ms during socket waits.

Send, receive and close wait for socket readiness on Bend's I/O loop. Pending
operations retain partial writes, message fragments and control replies until
completion, cancellation or timeout. The upgrade runs on an I/O helper;
individual messages do not require a helper-thread handoff. Other Bend tasks
can progress while a WebSocket waits or handles continuously available messages.

Text messages must be valid UTF-8; fragmentation may split a code point. Control
frames have the RFC 6455 125-byte limit. Close codes and reasons are checked.
The acceptance challenge is verified independently of libcurl; duplicate/missing
acceptance, unsolicited protocols, and extension negotiation are rejected.
Redirects are rejected. Caller headers cannot replace the generated WebSocket
key, version, upgrade, or connection fields.

I/O errors invalidate the connection because some bytes may already have been
transferred. Discard it and reconnect explicitly. Validation errors before I/O
leave it usable. A socket is affine in Bend; native C callers must serialize
operations, and may never free it while another call is active.

Close message bodies contain the network-order two-byte status followed by its
UTF-8 reason (or an empty payload). `Ws.close` accepts these as separate arguments.

See [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455),
[libcurl's frame API](https://curl.se/libcurl/c/curl_ws_recv.html), and the
[repeatable WSS benchmark](../benchmarks/WEBSOCKET.md).
Implementation review criteria are recorded in [operation invariants](WEBSOCKET_INTERNALS.md).
