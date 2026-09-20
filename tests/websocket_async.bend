import Base
import ../scrapanium.bend as S

def require(valid: Bool, label: String) -> IO(Unit):
  match valid:
    case True{}:
      IO.print(label)
    case False{}:
      IO.die(Unit, 1, "unexpected WebSocket completion")

def error_is(expected: U32, label: String, result: Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      require(U32.is_eq(code, expected), label)
    case Done{message}:
      IO.die(Unit, 1, "unexpected successful receive")

def timer(+token: S.CancelToken) -> IO(Unit):
  do IO<Unit>:
    IO.sleep(60)
    IO.print("timer")
    IO.try(Unit, S.cancel(token))

def cancelled(+token: S.CancelToken, child: Chan(Unit), pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    error_is(10, "cancelled", result)
    S.Ws.discard(socket)
    IO.join(Unit, child)
    IO.try(Unit, S.cancel_release(token))

def run_cancel(url: String, ca: String) -> IO(Unit):
  do IO<Unit>:
    +token : S.CancelToken <- IO.try(S.CancelToken, S.cancel_new())
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    child : Chan(Unit) <- IO.fork(Unit, timer(token))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive_cancel(socket, 2000, token)
    cancelled(token, child, pair)

def timed_out(pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    error_is(14, "timeout", result)
    S.Ws.discard(socket)

def run_timeout(url: String, ca: String) -> IO(Unit):
  do IO<Unit>:
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 120)
    timed_out(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    +url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    +ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    child : Chan(Unit) <- IO.fork(Unit, run_cancel(url, ca))
    run_timeout(url, ca)
    IO.join(Unit, child)
