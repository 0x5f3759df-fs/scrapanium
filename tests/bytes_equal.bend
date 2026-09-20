import Base
import ../scrapanium.bend as S

def show(equal: Bool) -> IO(Unit):
  match equal:
    case True{}:
      IO.print("equal")
    case False{}:
      IO.print("different")

def compared(pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (left, right) = buffers
  do IO<Unit>:
    S.Bytes.discard(left)
    S.Bytes.discard(right)
    show(equal)

def compare(left: List<U32>, right: List<U32>) -> IO(Unit):
  do IO<Unit>:
    left : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_list(left))
    right : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_list(right))
    pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(left, right)
    compared(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    compare([], [])
    compare([], [0])
    compare([0, 255, 128, 192, 0], [0, 255, 128, 192, 0])
    compare([0, 255, 128, 192, 0], [1, 255, 128, 192, 0])
    compare([0, 255, 128, 192, 0], [0, 255, 127, 192, 0])
    compare([0, 255, 128, 192, 0], [0, 255, 128, 192, 1])
    compare([0, 255, 128, 192, 0], [0, 255, 128, 192])
