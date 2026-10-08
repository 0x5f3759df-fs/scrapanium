import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "owned BinaryRequest check failed")

def compare_http(session: S.Session, pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (received, expected) = buffers
  do IO<Unit>:
    require(equal)
    S.Bytes.discard(received)
    S.Bytes.discard(expected)
    S.close(session)
    IO.print("owned-binary-request-ok")

def response(expected: S.Bytes, pair: S.Session & Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  (session, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Bytes.discard(expected)
        S.close(session)
        IO.die(Unit, 1, "owned BinaryRequest failed")
    case Done{response}:
      do IO<Unit>:
        received : S.Bytes <- S.into_bytes(response)
        pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(received, expected)
        compare_http(session, pair)

def receive_owned(expected: S.Bytes, http: String, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Bytes.discard(expected)
        S.Ws.discard(socket)
        IO.die(Unit, 1, "owned WebSocket receive failed")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            S.Ws.discard(socket)
            session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
            pair : S.Session & Result<&1, &1, U32 & String, S.Response> <-
              S.send(session, S.Request.binary("POST", http ++ "/owned-body-echo", body))
            response(expected, pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    http : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_HTTP_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_EXPECTED"))
    expected : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 400000))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url ++ "/owned-segments"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    receive_owned(expected, http, pair)
