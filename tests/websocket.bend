import Base
import ../scrapanium.bend as S

def print_body(pair: S.Bytes & String) -> IO(Unit):
  (bytes, value) = pair
  do IO<Unit>:
    IO.print(value)
    S.Bytes.discard(bytes)

def show_message(message: S.WsMessage) -> IO(Unit):
  match message:
    case S.WsMessage{kind, body}:
      do IO<Unit>:
        IO.print(U32.show(kind))
        pair : S.Bytes & String <- S.Bytes.text(body)
        print_body(pair)

def received(pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    message : S.WsMessage <- IO.try(S.WsMessage, IO.pure(Result<&1, &1, U32 & String, S.WsMessage>, result))
    show_message(message)
    IO.try(Unit, S.Ws.close(socket, 1000, "done", 2000))

def sent(pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    IO.try(Unit, IO.pure(Result<&1, &1, U32 & String, Unit>, result))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 2000)
    received(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    bytes : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text("Bend 🌍\0"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 1, bytes, 2000)
    sent(pair)
