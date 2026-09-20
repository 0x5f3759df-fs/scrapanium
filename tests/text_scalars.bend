import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "scalar conversion regression")

def compared(pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (actual, expected) = buffers
  do IO<Unit>:
    require(equal)
    S.Bytes.discard(actual)
    S.Bytes.discard(expected)
    IO.print("exact")

def converted(path: String, result: Result<&1, &1, U32 & String, S.Bytes>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        require(U32.is_eq(code, 1))
        IO.print("invalid")
    case Done{actual}:
      do IO<Unit>:
        expected : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 128))
        pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(actual, expected)
        compared(pair)

def closed(result: Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        require(U32.is_eq(code, 1))
        IO.print("invalid")
    case Done{unit}:
      IO.print("closed")

def run(close: Bool, text: String, path: String) -> IO(Unit):
  match close:
    case True{}:
      do IO<Unit>:
        url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
        ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
        socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
        result : Result<&1, &1, U32 & String, Unit> <- S.Ws.close(socket, 1000, text, 2000)
        closed(result)
    case False{}:
      do IO<Unit>:
        result : Result<&1, &1, U32 & String, S.Bytes> <- S.Bytes.from_text(text)
        converted(path, result)

def main() -> IO(Unit):
  do IO<Unit>:
    scalar : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_SCALAR"))
    mode : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_MODE"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_PAYLOAD"))
    run(String.eq(mode, "close"), SCon{Chr{Maybe.default(&2, U32, U32.read(scalar), 0)}, "tail"}, path)
