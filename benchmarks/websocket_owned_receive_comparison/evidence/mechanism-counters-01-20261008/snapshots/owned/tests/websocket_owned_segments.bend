import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "owned WebSocket byte check failed")

def compare_held(pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (held, expected) = buffers
  do IO<Unit>:
    require(equal)
    S.Bytes.discard(held)
    S.Bytes.discard(expected)
    IO.print("owned-message-ok")

def last_index(expected: S.Bytes, pair: S.Bytes & Maybe<U32>) -> IO(Unit):
  (held, value) = pair
  do IO<Unit>:
    require(U32.is_eq(Maybe.default(&1, U32, value, 999), 70))
    pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(held, expected)
    compare_held(pair)

def middle_index(expected: S.Bytes, pair: S.Bytes & Maybe<U32>) -> IO(Unit):
  (held, value) = pair
  do IO<Unit>:
    require(U32.is_eq(Maybe.default(&1, U32, value, 999), 230))
    pair : S.Bytes & Maybe<U32> <- S.Bytes.at(held, 299999)
    last_index(expected, pair)

def first_index(expected: S.Bytes, pair: S.Bytes & Maybe<U32>) -> IO(Unit):
  (held, value) = pair
  do IO<Unit>:
    require(U32.is_eq(Maybe.default(&1, U32, value, 999), 11))
    pair : S.Bytes & Maybe<U32> <- S.Bytes.at(held, 65535)
    middle_index(expected, pair)

def check_held(expected: S.Bytes, held: S.Bytes) -> IO(Unit):
  do IO<Unit>:
    pair : S.Bytes & Maybe<U32> <- S.Bytes.at(held, 0)
    first_index(expected, pair)

def text_checked(held: S.Bytes, expected: S.Bytes, socket: S.WebSocket, pair: S.Bytes & String) -> IO(Unit):
  (text_bytes, text) = pair
  do IO<Unit>:
    require(String.eq(text, "Segmented 🌍é"))
    S.Bytes.discard(text_bytes)
    S.Ws.discard(socket)
    check_held(expected, held)

def second_message(held: S.Bytes, expected: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Bytes.discard(held)
        S.Bytes.discard(expected)
        S.Ws.discard(socket)
        IO.die(Unit, 1, "second WebSocket receive failed")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 1))
            pair : S.Bytes & String <- S.Bytes.text(body)
            text_checked(held, expected, socket, pair)

def first_message(expected: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Bytes.discard(expected)
        S.Ws.discard(socket)
        IO.die(Unit, 1, "first WebSocket receive failed")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
            second_message(body, expected, pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_EXPECTED"))
    expected : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 400000))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url ++ "/owned-segments"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    first_message(expected, pair)
