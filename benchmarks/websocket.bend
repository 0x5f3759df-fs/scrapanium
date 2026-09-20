import Base
import ../scrapanium.bend as S

def discard_message(message: S.WsMessage) -> IO(Unit):
  match message:
    case S.WsMessage{kind, body}:
      S.Bytes.discard(body)

def receive_done(pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(S.WebSocket):
  (socket, result) = pair
  do IO<S.WebSocket>:
    message : S.WsMessage <- IO.pass(S.WsMessage, result)
    discard_message(message)
    return socket

def send_done(pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(S.WebSocket):
  (socket, result) = pair
  do IO<S.WebSocket>:
    IO.pass(Unit, result)
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    receive_done(pair)

def repeat(n: Nat, socket: S.WebSocket) -> IO(S.WebSocket):
  match n:
    case 0n:
      IO.pure(S.WebSocket, socket)
    case 1n+rest:
      do IO<S.WebSocket>:
        data : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text("scrapanium-websocket-benchmark"))
        pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 2, data, 5000)
        socket : S.WebSocket <- send_done(pair)
        repeat(rest, socket)

def timed(n: Nat, socket: S.WebSocket) -> IO(Unit):
  do IO<Unit>:
    socket : S.WebSocket <- repeat(1n, socket)
    start : Nat <- IO.now()
    socket : S.WebSocket <- repeat(n, socket)
    end : Nat <- IO.now()
    S.Ws.discard(socket)
    IO.print(Nat.show(Nat.sub(end, start)))

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_CA"))
    count : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_COUNT"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.browser("chrome146"), ca), url))
    timed(U32.to_nat(Maybe.default(&2, U32, U32.read(count), 1000)), socket)
