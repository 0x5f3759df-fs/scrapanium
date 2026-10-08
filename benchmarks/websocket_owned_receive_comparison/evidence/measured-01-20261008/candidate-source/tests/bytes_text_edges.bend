import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "UTF-8 byte decoding mismatch")

def checked(pair: S.Bytes & String, expected: String) -> IO(Unit):
  (returned, text) = pair
  do IO<Unit>:
    require(String.eq(text, expected))
    S.Bytes.discard(returned)

def decoded(result: Result<&1, &1, U32 & String, S.Bytes>, expected: String) -> IO(Unit):
  match result:
    case Fail{error}:
      IO.die(Unit, 1, "byte construction failed")
    case Done{bytes}:
      do IO<Unit>:
        pair : S.Bytes & String <- S.Bytes.text(bytes)
        checked(pair, expected)

def check(values: List<U32>, expected: String) -> IO(Unit):
  do IO<Unit>:
    result : Result<&1, &1, U32 & String, S.Bytes> <- S.Bytes.from_list(values)
    decoded(result, expected)

def main() -> IO(Unit):
  do IO<Unit>:
    check([0, 226, 40, 161, 240, 159], SCon{Chr{0}, "�(��"})
    check([237, 160, 128], "���")
    check([244, 144, 128, 128], "����")
    check([224, 128, 128], "���")
    check([240, 159, 140, 141], "🌍")
    check([226, 130], "�")
    IO.print("byte-text-edges-ok")
