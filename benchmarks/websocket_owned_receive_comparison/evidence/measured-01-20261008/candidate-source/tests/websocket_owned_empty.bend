import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "empty owned WebSocket check failed")

def done(socket: S.WebSocket, pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (body, empty) = buffers
  do IO<Unit>:
    require(equal)
    S.Bytes.discard(body)
    S.Bytes.discard(empty)
    S.Ws.discard(socket)
    IO.print("owned-empty-ok")

def text_checked(socket: S.WebSocket, expected: S.Bytes, pair: S.Bytes & String) -> IO(Unit):
  (body, text) = pair
  do IO<Unit>:
    require(String.eq(text, ""))
    pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(body, expected)
    done(socket, pair)

def empty_text(socket: S.WebSocket, expected: S.Bytes, pair: S.Bytes & Maybe<U32>) -> IO(Unit):
  (body, value) = pair
  match value:
    case None{}:
      do IO<Unit>:
        pair : S.Bytes & String <- S.Bytes.text(body)
        text_checked(socket, expected, pair)
    case Some{value}:
      do IO<Unit>:
        S.Bytes.discard(body)
        S.Bytes.discard(expected)
        S.Ws.discard(socket)
        IO.die(Unit, 1, "empty message had a byte at index zero")

def received(expected: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Bytes.discard(expected)
        S.Ws.discard(socket)
        IO.die(Unit, 1, "empty WebSocket receive failed")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            pair : S.Bytes & Maybe<U32> <- S.Bytes.at(body, 0)
            empty_text(socket, expected, pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    expected : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_list([]))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url ++ "/owned-empty"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    received(expected, pair)
