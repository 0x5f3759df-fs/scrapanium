import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "owned receive cancellation check failed")

def timer(+token: S.CancelToken) -> IO(Unit):
  do IO<Unit>:
    IO.sleep(500)
    IO.try(Unit, S.cancel(token))

def cancelled(token: S.CancelToken, child: Chan(Unit), pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  match result:
    case Fail{error}:
      (code, message) = error
      do IO<Unit>:
        require(U32.is_eq(code, 10))
        S.Ws.discard(socket)
        IO.join(Unit, child)
        IO.try(Unit, S.cancel_release(token))
        IO.print("owned-cancel-ok")
    case Done{message}:
      IO.die(Unit, 1, "incomplete WebSocket frame unexpectedly completed")

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    +token : S.CancelToken <- IO.try(S.CancelToken, S.cancel_new())
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url ++ "/owned-cancel"))
    child : Chan(Unit) <- IO.fork(Unit, timer(token))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive_cancel(socket, 5000, token)
    cancelled(token, child, pair)
