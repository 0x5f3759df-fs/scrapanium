import Base
import ../scrapanium.bend as S
import ../http.bend as H

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "BinaryRequest flatten fault did not match SP_NOMEM")

def request_result(pair: S.Session & Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  (session, result) = pair
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        require(U32.is_eq(code, 2))
        require(String.eq(message, "out of memory"))
        S.close(session)
        IO.print("binary-request-flatten-oom-propagated")
    case Done{response}:
      do IO<Unit>:
        body : S.Bytes <- S.into_bytes(response)
        S.Bytes.discard(body)
        S.close(session)
        IO.die(Unit, 1, "BinaryRequest unexpectedly reached HTTP")

def received(http: String, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Ws.discard(socket)
        IO.die(Unit, 1, "WebSocket receive failed before BinaryRequest flatten fault")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            S.Ws.discard(socket)
            session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
            pair : S.Session & Result<&1, &1, U32 & String, S.Response> <-
              S.send(session, S.Request.with_headers(
                S.Request.binary("POST", http ++ "/must-not-run", body),
                H.Headers{[H.Header{"X-Owned-OOM", "header-must-be-consumed"},
                          H.Header{"Content-Type", "application/octet-stream"}]}))
            request_result(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    http : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_HTTP_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url ++ "/owned-segments"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    received(http, pair)
