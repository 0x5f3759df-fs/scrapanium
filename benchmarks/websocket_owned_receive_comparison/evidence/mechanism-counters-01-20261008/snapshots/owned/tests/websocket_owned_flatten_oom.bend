import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "expected binary WebSocket response")

def failed_send(pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        require(U32.is_eq(code, 2))
        require(String.eq(message, "out of memory"))
        S.Ws.discard(socket)
        IO.print("flatten-oom-propagated")
    case Done{value}:
      do IO<Unit>:
        S.Ws.discard(socket)
        IO.die(Unit, 1, "flatten allocation unexpectedly succeeded")

def send_received(pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      do IO<Unit>:
        S.Ws.discard(socket)
        IO.die(Unit, 1, "WebSocket receive failed before flatten fault")
    case Done{message}:
      match message:
        case S.WsMessage{kind, body}:
          do IO<Unit>:
            require(U32.is_eq(kind, 2))
            pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 2, body, 5000)
            failed_send(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url ++ "/owned-send"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    send_received(pair)
