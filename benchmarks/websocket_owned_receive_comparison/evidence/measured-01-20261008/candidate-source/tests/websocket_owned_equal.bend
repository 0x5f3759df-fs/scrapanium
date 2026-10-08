import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "owned segmented byte check failed")

def keep_equal(pair: (S.Bytes & S.Bytes) & Bool) -> IO(S.Bytes):
  (buffers, equal) = pair
  (held, other) = buffers
  match equal:
    case True{}:
      do IO<S.Bytes>:
        S.Bytes.discard(other)
        IO.pure(S.Bytes, held)
    case False{}:
      IO.die(S.Bytes, 1, "expected equal byte segments")

def keep_different(pair: (S.Bytes & S.Bytes) & Bool) -> IO(S.Bytes):
  (buffers, equal) = pair
  (held, other) = buffers
  match equal:
    case True{}:
      IO.die(S.Bytes, 1, "expected byte mismatch")
    case False{}:
      do IO<S.Bytes>:
        S.Bytes.discard(other)
        IO.pure(S.Bytes, held)

def compare_different(held: S.Bytes, path: String) -> IO(S.Bytes):
  do IO<S.Bytes>:
    other : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 200000))
    pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(held, other)
    keep_different(pair)

def compare_equal(held: S.Bytes, path: String) -> IO(S.Bytes):
  do IO<S.Bytes>:
    other : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 200000))
    pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(held, other)
    keep_equal(pair)

def verify_files(held: S.Bytes) -> IO(Unit):
  do IO<Unit>:
    first : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_FIRST"))
    middle : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_MIDDLE"))
    last : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_LAST"))
    expected : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_EXPECTED"))
    after_first : S.Bytes <- compare_different(held, first)
    after_middle : S.Bytes <- compare_different(after_first, middle)
    after_last : S.Bytes <- compare_different(after_middle, last)
    exact : S.Bytes <- compare_equal(after_last, expected)
    S.Bytes.discard(exact)
    IO.print("owned-equality-ok")

def second(held: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        S.Bytes.discard(held)
        S.Ws.discard(socket)
        IO.die(Unit, 1, "second equality frame missing")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            pair2 : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(held, body)
            same : S.Bytes <- keep_equal(pair2)
            S.Ws.discard(socket)
            verify_files(same)

def first(pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        S.Ws.discard(socket)
        IO.die(Unit, 1, "first equality frame missing")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            pair2 : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
            second(body, pair2)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url ++ "/owned-equality"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    first(pair)
