import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "segmented Bytes.text mismatch")

def codepoints(text: String) -> String:
  match text:
    case SNil{}:
      ""
    case SCon{head, tail}:
      U32.show(Char.to_u32(head)) ++ " " ++ codepoints(tail)

def checked(socket: S.WebSocket, pair: S.Bytes & String) -> IO(Unit):
  (bytes, text) = pair
  do IO<Unit>:
    IO.print(codepoints(text))
    S.Bytes.discard(bytes)
    S.Ws.discard(socket)

def received(pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Ws.discard(socket)
        IO.die(Unit, 1, "fragmented binary UTF-8 edge payload missing")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            pair : S.Bytes & String <- S.Bytes.text(body)
            checked(socket, pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    received(pair)
