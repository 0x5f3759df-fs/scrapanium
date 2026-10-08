import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "owned WebSocket send check failed")

def closed(result: Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  do IO<Unit>:
    IO.try(Unit, IO.pure(Result<&1, &1, U32 & String, Unit>, result))
    IO.print("owned-send-ok")

def compare_echo_bytes(socket: S.WebSocket, pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (received, wanted) = buffers
  do IO<Unit>:
    require(equal)
    S.Bytes.discard(received)
    S.Bytes.discard(wanted)
    result : Result<&1, &1, U32 & String, Unit> <- S.Ws.close(socket, 1000, "done", 5000)
    closed(result)

def compare_echo(expected: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Bytes.discard(expected)
        S.Ws.discard(socket)
        IO.die(Unit, 1, "owned WebSocket echo receive failed")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(body, expected)
            compare_echo_bytes(socket, pair)

def sent(expected: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    IO.try(Unit, IO.pure(Result<&1, &1, U32 & String, Unit>, result))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    compare_echo(expected, pair)

def received(expected: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
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
            pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 2, body, 5000)
            sent(expected, pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_EXPECTED"))
    expected : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 400000))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url ++ "/owned-send"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    received(expected, pair)
