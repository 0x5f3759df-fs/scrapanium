import Base
import ../scrapanium.bend as S

def checked(pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        IO.print(U32.show(code))
        IO.print(message)
        S.Ws.discard(socket)
        IO.print("noncanonical-rejected")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            S.Bytes.discard(body)
            S.Ws.discard(socket)
            IO.die(Unit, 1, "noncanonical frame was accepted")

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    checked(pair)
