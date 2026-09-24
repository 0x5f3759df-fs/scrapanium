import Base
import ./http.bend as H

# The File field is an opaque host handle, as required by Bend 2.0.27.
# Only Scrapanium effects may operate on it. Close sessions/responses explicitly.
type Session is Type:
  Session{handle: File}

type Response is Type:
  Response{handle: File, status: U32, size: U32, version: U32}

# Native bytes are affine, binary-safe, and use generation-checked handles.
type Bytes is Type:
  Bytes{handle: File, size: U32}

# Copy tokens to share cancellation between IO tasks. Release once after use;
# releasing invalidates every copy, while in-flight operations remain safe.
type CancelToken is Data:
  CancelToken{slot: U32, generation: U32}

type DownloadInfo is Data:
  DownloadInfo{status: U32, bytes: U32, url: String, headers: String}

type Limits is Data:
  Limits{batch_bytes: U32, batch_requests: U32}

def Limits.defaults() -> Limits:
  Limits{134217728, 4096}

# Toggle values: 0 inherits the profile, 1 disables, 2 enables.
# Empty strings and a zero window inherit the profile.
type Fingerprint is Data:
  Fingerprint{ciphers: String, curves: String, signature_algorithms: String,
    extension_order: String, http2_settings: String, pseudo_header_order: String,
    cert_compression: String, grease: U32, permute_extensions: U32, window_update: U32}

def Fingerprint.inherit() -> Fingerprint:
  Fingerprint{"", "", "", "", "", "", "", 0, 0, 0}

type Config is Data:
  Config{profile: String, proxy: String, ca_bundle: String,
    timeout_ms: U32, connect_timeout_ms: U32, max_redirects: U32,
    concurrency: U32, max_body_bytes: U32, max_header_bytes: U32,
    verify: Bool, follow_redirects: Bool, default_headers: Bool, fingerprint: Fingerprint, limits: Limits}

type Request is Type:
  Request{method: String, url: String, headers: List<String>, body: String}
  BinaryRequest{method: String, url: String, headers: List<String>, body: Bytes}

def defaults() -> Config:
  Config{"chrome150", "", "", 30000, 10000, 10, 16,
    67108864, 262144, True{}, True{}, True{}, Fingerprint.inherit(), Limits.defaults()}

def browser(profile: String) -> Config:
  Config{profile, "", "", 30000, 10000, 10, 16,
    67108864, 262144, True{}, True{}, True{}, Fingerprint.inherit(), Limits.defaults()}

def Config.with_ca(config: Config, ca: String) -> Config:
  match config:
    case Config{profile, proxy, old_ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}:
      Config{profile, proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}

def Config.with_proxy(config: Config, proxy: String) -> Config:
  match config:
    case Config{profile, old_proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}:
      Config{profile, proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}

def Config.with_timeouts(config: Config, timeout: U32, connect: U32) -> Config:
  match config:
    case Config{profile, proxy, ca, old_timeout, old_connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}:
      Config{profile, proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}

def Config.with_concurrency(config: Config, concurrency: U32) -> Config:
  match config:
    case Config{profile, proxy, ca, timeout, connect, redirects, old_concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}:
      Config{profile, proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}

def Config.with_limits(config: Config, limits: Limits) -> Config:
  match config:
    case Config{profile, proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, old_limits}:
      Config{profile, proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}

def Config.with_fingerprint(config: Config, fingerprint: Fingerprint) -> Config:
  match config:
    case Config{profile, proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, old_fingerprint, limits}:
      Config{profile, proxy, ca, timeout, connect, redirects, concurrency, body, headers, verify, follow, default_fields, fingerprint, limits}

def Request.get(url: String) -> Request:
  Request{"GET", url, [], ""}

def Request.post(url: String, body: String) -> Request:
  Request{"POST", url, [], body}

def Request.binary(method: String, url: String, body: Bytes) -> Request:
  BinaryRequest{method, url, [], body}

def Request.with_query(request: Request, params: List<&2, H.Param>) -> Request:
  match request:
    case Request{method, url, headers, body}:
      Request{method, H.Query.append(url, params), headers, body}
    case BinaryRequest{method, url, headers, body}:
      BinaryRequest{method, H.Query.append(url, params), headers, body}

def Request.with_headers(request: Request, headers: H.Headers) -> Request:
  match request:
    case Request{method, url, old_headers, body}:
      Request{method, url, H.Headers.lines(headers), body}
    case BinaryRequest{method, url, old_headers, body}:
      BinaryRequest{method, url, H.Headers.lines(headers), body}

def Request.form(url: String, params: List<&2, H.Param>) -> Request:
  Request{"POST", url, ["Content-Type: application/x-www-form-urlencoded"], H.Form.encode(params)}

def Scrapanium.bytes_from_list(values: List<U32>) -> IO(Result<&1, &1, U32 & String, Bytes>):
  import "./native/bend_bridge.c"

def Bytes.from_list(values: List<U32>) -> IO(Result<&1, &1, U32 & String, Bytes>):
  Scrapanium.bytes_from_list(values)

def Scrapanium.bytes_from_text(value: String) -> IO(Result<&1, &1, U32 & String, Bytes>):
  import "./native/bend_bridge.c"

def Bytes.from_text(value: String) -> IO(Result<&1, &1, U32 & String, Bytes>):
  Scrapanium.bytes_from_text(value)

def Scrapanium.bytes_read_file(path: String, max_bytes: U32) -> IO(Result<&1, &1, U32 & String, Bytes>):
  import "./native/bend_bridge.c"

def Bytes.read_file(path: String, max_bytes: U32) -> IO(Result<&1, &1, U32 & String, Bytes>):
  Scrapanium.bytes_read_file(path, max_bytes)

def Scrapanium.bytes_at(bytes: Bytes, index: U32) -> IO(Bytes & Maybe<U32>):
  import "./native/bend_bridge.c"

def Bytes.at(bytes: Bytes, index: U32) -> IO(Bytes & Maybe<U32>):
  Scrapanium.bytes_at(bytes, index)

def Scrapanium.bytes_text(bytes: Bytes) -> IO(Bytes & String):
  import "./native/bend_bridge.c"

def Bytes.text(bytes: Bytes) -> IO(Bytes & String):
  Scrapanium.bytes_text(bytes)

# Compares length and every byte without text decoding; returns both buffers.
def Scrapanium.bytes_equal(left: Bytes, right: Bytes) -> IO((Bytes & Bytes) & Bool):
  import "./native/bend_bridge.c"

def Bytes.equal(left: Bytes, right: Bytes) -> IO((Bytes & Bytes) & Bool):
  Scrapanium.bytes_equal(left, right)

def Scrapanium.bytes_discard(bytes: Bytes) -> IO(Unit):
  import "./native/bend_bridge.c"

def Bytes.discard(bytes: Bytes) -> IO(Unit):
  Scrapanium.bytes_discard(bytes)

# Moves the existing body allocation out of a response without a body copy.
# The response is consumed; read its status/headers before transferring it.
def Scrapanium.into_bytes(response: Response) -> IO(Bytes):
  import "./native/bend_bridge.c"

def into_bytes(response: Response) -> IO(Bytes):
  Scrapanium.into_bytes(response)

def is_success(+status: U32) -> Bool:
  (status >= 200 : U32) && (status < 300 : U32)

def Scrapanium.open(config: Config) -> IO(Result<&1, &1, U32 & String, Session>):
  import "./native/bend_bridge.c"

def open(config: Config) -> IO(Result<&1, &1, U32 & String, Session>):
  Scrapanium.open(config)

def Scrapanium.close(session: Session) -> IO(Unit):
  import "./native/bend_bridge.c"

def close(session: Session) -> IO(Unit):
  Scrapanium.close(session)

def Scrapanium.send(session: Session, request: Request) -> IO(Session & Result<&1, &1, U32 & String, Response>):
  import "./native/bend_bridge.c"

def send(session: Session, request: Request) -> IO(Session & Result<&1, &1, U32 & String, Response>):
  Scrapanium.send(session, request)

def Scrapanium.batch(session: Session, requests: List<Request>) -> IO(Session & List<Result<&1, &1, U32 & String, Response>>):
  import "./native/bend_bridge.c"

def batch(session: Session, requests: List<Request>) -> IO(Session & List<Result<&1, &1, U32 & String, Response>>):
  Scrapanium.batch(session, requests)

def Scrapanium.cancel_new() -> IO(Result<&1, &1, U32 & String, CancelToken>):
  import "./native/bend_bridge.c"

def cancel_new() -> IO(Result<&1, &1, U32 & String, CancelToken>):
  Scrapanium.cancel_new()

def Scrapanium.cancel(token: CancelToken) -> IO(Result<&1, &1, U32 & String, Unit>):
  import "./native/bend_bridge.c"

def cancel(token: CancelToken) -> IO(Result<&1, &1, U32 & String, Unit>):
  Scrapanium.cancel(token)

def Scrapanium.cancel_release(token: CancelToken) -> IO(Result<&1, &1, U32 & String, Unit>):
  import "./native/bend_bridge.c"

def cancel_release(token: CancelToken) -> IO(Result<&1, &1, U32 & String, Unit>):
  Scrapanium.cancel_release(token)

def Scrapanium.send_cancel(session: Session, request: Request, token: CancelToken) -> IO(Session & Result<&1, &1, U32 & String, Response>):
  import "./native/bend_bridge.c"

def send_cancel(session: Session, request: Request, token: CancelToken) -> IO(Session & Result<&1, &1, U32 & String, Response>):
  Scrapanium.send_cancel(session, request, token)

def Scrapanium.batch_cancel(session: Session, requests: List<Request>, token: CancelToken) -> IO(Session & List<Result<&1, &1, U32 & String, Response>>):
  import "./native/bend_bridge.c"

def batch_cancel(session: Session, requests: List<Request>, token: CancelToken) -> IO(Session & List<Result<&1, &1, U32 & String, Response>>):
  Scrapanium.batch_cancel(session, requests, token)

# Streams decoded bytes on an IO worker. Replaces path only on successful 2xx;
# failures leave an existing destination intact. No response body is buffered.
def Scrapanium.download(session: Session, request: Request, path: String) -> IO(Session & Result<&1, &1, U32 & String, DownloadInfo>):
  import "./native/bend_bridge.c"

def download(session: Session, request: Request, path: String) -> IO(Session & Result<&1, &1, U32 & String, DownloadInfo>):
  Scrapanium.download(session, request, path)

def Scrapanium.download_cancel(session: Session, request: Request, path: String, token: CancelToken) -> IO(Session & Result<&1, &1, U32 & String, DownloadInfo>):
  import "./native/bend_bridge.c"

def download_cancel(session: Session, request: Request, path: String, token: CancelToken) -> IO(Session & Result<&1, &1, U32 & String, DownloadInfo>):
  Scrapanium.download_cancel(session, request, path, token)

# UTF-8 decoding is explicit and keeps the response alive. Invalid UTF-8 uses
# Bend's replacement-character semantics; use byte_at/save for exact bytes.
def Scrapanium.text(response: Response) -> IO(Response & String):
  import "./native/bend_bridge.c"

def text(response: Response) -> IO(Response & String):
  Scrapanium.text(response)

# Raw headers include intermediate responses, preserving duplicates and order.
def Scrapanium.headers(response: Response) -> IO(Response & String):
  import "./native/bend_bridge.c"

def headers(response: Response) -> IO(Response & String):
  Scrapanium.headers(response)

def Scrapanium.header_pairs(response: Response, trailers: Bool) -> IO(Response & List<&2, Sigma<&2, &2, String, _ => String>>):
  import "./native/bend_bridge.c"

def header_items(pairs: List<&2, Sigma<&2, &2, String, _ => String>>) -> List<&2, H.Header>:
  match pairs:
    case Nil{}:
      []
    case Con{head, tail}:
      match head:
        case Tuple{name, value}:
          Con{H.Header{name, value}, header_items(tail)}

def header_result(pair: Response & List<&2, Sigma<&2, &2, String, _ => String>>) -> IO(Response & H.Headers):
  (response, pairs) = pair
  IO.pure(Response & H.Headers, (response, H.Headers{header_items(pairs)}))

# Structured fields from the final HTTP response. Trailers remain separate.
def response_headers(response: Response) -> IO(Response & H.Headers):
  do IO<Response & H.Headers>:
    pair : Response & List<&2, Sigma<&2, &2, String, _ => String>> <- Scrapanium.header_pairs(response, False{})
    header_result(pair)

def response_trailers(response: Response) -> IO(Response & H.Headers):
  do IO<Response & H.Headers>:
    pair : Response & List<&2, Sigma<&2, &2, String, _ => String>> <- Scrapanium.header_pairs(response, True{})
    header_result(pair)

def Scrapanium.effective_url(response: Response) -> IO(Response & String):
  import "./native/bend_bridge.c"

def effective_url(response: Response) -> IO(Response & String):
  Scrapanium.effective_url(response)

def Scrapanium.byte_at(response: Response, index: U32) -> IO(Response & Maybe<U32>):
  import "./native/bend_bridge.c"

def byte_at(response: Response, index: U32) -> IO(Response & Maybe<U32>):
  Scrapanium.byte_at(response, index)

def Scrapanium.save(response: Response, path: String) -> IO(Response & Result<&1, &1, U32 & String, Unit>):
  import "./native/bend_bridge.c"

def save(response: Response, path: String) -> IO(Response & Result<&1, &1, U32 & String, Unit>):
  Scrapanium.save(response, path)

def Scrapanium.discard(response: Response) -> IO(Unit):
  import "./native/bend_bridge.c"

def discard(response: Response) -> IO(Unit):
  Scrapanium.discard(response)

def Scrapanium.backend() -> IO(String):
  import "./native/bend_bridge.c"

def backend() -> IO(String):
  Scrapanium.backend()

def Scrapanium.profiles() -> IO(List<String>):
  import "./native/bend_bridge.c"

def profiles() -> IO(List<String>):
  Scrapanium.profiles()

def finish_fetch(pair: Session & Result<&1, &1, U32 & String, Response>) -> IO(Result<&1, &1, U32 & String, Response>):
  (session, result) = pair
  do IO<Result<&1, &1, U32 & String, Response>>:
    close(session)
    return result

def start_fetch(request: Request, opened: Result<&1, &1, U32 & String, Session>) -> IO(Result<&1, &1, U32 & String, Response>):
  match opened:
    case Fail{error}:
      IO.pure(Result<&1, &1, U32 & String, Response>, Fail{error})
    case Done{session}:
      do IO<Result<&1, &1, U32 & String, Response>>:
        pair : Session & Result<&1, &1, U32 & String, Response> <- send(session, request)
        finish_fetch(pair)

# One-shot convenience; use open/send/batch for pooling across requests.
def fetch(config: Config, request: Request) -> IO(Result<&1, &1, U32 & String, Response>):
  do IO<Result<&1, &1, U32 & String, Response>>:
    opened : Result<&1, &1, U32 & String, Session> <- open(config)
    start_fetch(request, opened)

def get(url: String) -> IO(Result<&1, &1, U32 & String, Response>):
  fetch(defaults(), Request.get(url))

def post(url: String, body: String) -> IO(Result<&1, &1, U32 & String, Response>):
  fetch(defaults(), Request.post(url, body))

# WebSockets have a dedicated connection. Their handles and message bytes are
# affine. Each receive returns a complete text (1), binary (2), or close (8)
# message; control pings are answered while receiving. Limits come from Config.
type WebSocket is Type:
  WebSocket{handle: File}

type WsMessage is Type:
  WsMessage{kind: U32, body: Bytes}

def Scrapanium.ws_upgrade(session: Session, url: String, headers: List<String>, token: CancelToken) -> IO(Result<&1, &1, U32 & String, WebSocket>):
  import "./native/bend_bridge.c"

def Ws.start(url: String, headers: H.Headers, token: CancelToken, opened: Result<&1, &1, U32 & String, Session>) -> IO(Result<&1, &1, U32 & String, WebSocket>):
  match opened:
    case Fail{error}:
      IO.pure(Result<&1, &1, U32 & String, WebSocket>, Fail{error})
    case Done{session}:
      Scrapanium.ws_upgrade(session, url, H.Headers.lines(headers), token)

def Ws.connect_with(config: Config, url: String, headers: H.Headers, token: CancelToken) -> IO(Result<&1, &1, U32 & String, WebSocket>):
  do IO<Result<&1, &1, U32 & String, WebSocket>>:
    opened : Result<&1, &1, U32 & String, Session> <- open(Config.with_concurrency(config, 1))
    Ws.start(url, headers, token, opened)

def Ws.connect(config: Config, url: String) -> IO(Result<&1, &1, U32 & String, WebSocket>):
  Ws.connect_with(config, url, H.Headers.empty(), CancelToken{0, 0})

def Scrapanium.ws_send(socket: WebSocket, kind: U32, data: Bytes, timeout_ms: U32, token: CancelToken) -> IO(WebSocket & Result<&1, &1, U32 & String, Unit>):
  import "./native/bend_bridge.c"

def Ws.send_cancel(socket: WebSocket, kind: U32, data: Bytes, timeout_ms: U32, token: CancelToken) -> IO(WebSocket & Result<&1, &1, U32 & String, Unit>):
  Scrapanium.ws_send(socket, kind, data, timeout_ms, token)

def Ws.send(socket: WebSocket, kind: U32, data: Bytes, timeout_ms: U32) -> IO(WebSocket & Result<&1, &1, U32 & String, Unit>):
  Scrapanium.ws_send(socket, kind, data, timeout_ms, CancelToken{0, 0})

def Scrapanium.ws_receive(socket: WebSocket, timeout_ms: U32, token: CancelToken) -> IO(WebSocket & Result<&1, &1, U32 & String, WsMessage>):
  import "./native/bend_bridge.c"

def Ws.receive_cancel(socket: WebSocket, timeout_ms: U32, token: CancelToken) -> IO(WebSocket & Result<&1, &1, U32 & String, WsMessage>):
  Scrapanium.ws_receive(socket, timeout_ms, token)

def Ws.receive(socket: WebSocket, timeout_ms: U32) -> IO(WebSocket & Result<&1, &1, U32 & String, WsMessage>):
  Scrapanium.ws_receive(socket, timeout_ms, CancelToken{0, 0})

# Close sends a close frame and waits for the peer, then releases the handle
# on success or failure. Discard releases immediately without a handshake.
def Scrapanium.ws_close(socket: WebSocket, code: U32, reason: String, timeout_ms: U32, token: CancelToken) -> IO(Result<&1, &1, U32 & String, Unit>):
  import "./native/bend_bridge.c"

def Ws.close_cancel(socket: WebSocket, code: U32, reason: String, timeout_ms: U32, token: CancelToken) -> IO(Result<&1, &1, U32 & String, Unit>):
  Scrapanium.ws_close(socket, code, reason, timeout_ms, token)

def Ws.close(socket: WebSocket, code: U32, reason: String, timeout_ms: U32) -> IO(Result<&1, &1, U32 & String, Unit>):
  Scrapanium.ws_close(socket, code, reason, timeout_ms, CancelToken{0, 0})

def Scrapanium.ws_discard(socket: WebSocket) -> IO(Unit):
  import "./native/bend_bridge.c"

def Ws.discard(socket: WebSocket) -> IO(Unit):
  Scrapanium.ws_discard(socket)
