import Base
import ../scrapanium.bend as S

def report(valid: Bool, label: String) -> IO(Unit):
  match valid:
    case True{}:
      IO.print(label)
    case False{}:
      IO.die(Unit, 1, "blocked send returned wrong error")

def error_is(expected: U32, label: String, result: Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      report(U32.is_eq(code, expected), label)
    case Done{value}:
      IO.die(Unit, 1, "8MiB send unexpectedly completed while peer was not reading")

def timer(token: S.CancelToken) -> IO(Unit):
  do IO<Unit>:
    IO.sleep(60)
    IO.try(Unit, S.cancel(token))

def cancelled(token: S.CancelToken, child: Chan(Unit), pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    error_is(10, "cancelled-send", result)
    S.Ws.discard(socket)
    IO.join(Unit, child)
    IO.try(Unit, S.cancel_release(token))

def run_cancel(url: String, ca: String, path: String) -> IO(Unit):
  do IO<Unit>:
    +token : S.CancelToken <- IO.try(S.CancelToken, S.cancel_new())
    data : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 16777216))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    child : Chan(Unit) <- IO.fork(Unit, timer(token))
    pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send_cancel(socket, 2, data, 2000, token)
    cancelled(token, child, pair)

def timed_out(pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    error_is(14, "timeout-send", result)
    S.Ws.discard(socket)

def run_timeout(url: String, ca: String, path: String) -> IO(Unit):
  do IO<Unit>:
    data : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 16777216))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 2, data, 120)
    timed_out(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    +url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    +ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    +path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_PAYLOAD"))
    child : Chan(Unit) <- IO.fork(Unit, run_cancel(url, ca, path))
    run_timeout(url, ca, path)
    IO.join(Unit, child)
