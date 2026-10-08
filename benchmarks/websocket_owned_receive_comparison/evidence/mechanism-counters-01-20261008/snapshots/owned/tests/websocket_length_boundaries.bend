import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "WebSocket frame length boundary mismatch")

def compared(socket: S.WebSocket, pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (body, expected) = buffers
  do IO<Unit>:
    require(equal)
    S.Bytes.discard(body)
    S.Bytes.discard(expected)
    S.Ws.discard(socket)
    IO.print("accepted-exact")

def valid_result(path: String, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        S.Ws.discard(socket)
        IO.die(Unit, 1, "valid frame length was rejected")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            expected : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 70000))
            pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(body, expected)
            compared(socket, pair)

def invalid_result(pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        require(U32.is_eq(code, 4))
        S.Ws.discard(socket)
        IO.print("rejected-noncanonical")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            S.Bytes.discard(body)
            S.Ws.discard(socket)
            IO.die(Unit, 1, "invalid frame length was accepted")

def run(is_valid: Bool, path: String, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  match is_valid:
    case True{}:
      valid_result(path, pair)
    case False{}:
      invalid_result(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_PATH"))
    validity : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_VALID"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    run(String.eq(validity, "yes"), path, pair)
