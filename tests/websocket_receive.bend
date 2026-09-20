import Base
import ../scrapanium.bend as S

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "WebSocket readiness regression")

def checked(pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (body, expected) = buffers
  do IO<Unit>:
    require(equal)
    S.Bytes.discard(body)
    S.Bytes.discard(expected)

def check_message(expected: S.Bytes, expected_kind: U32, message: S.WsMessage) -> IO(Unit):
  match message:
    case S.WsMessage{kind, body}:
      do IO<Unit>:
        require(U32.is_eq(kind, expected_kind))
        pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(body, expected)
        checked(pair)

def received(expected: S.Bytes, expected_kind: U32, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    message : S.WsMessage <- IO.pass(S.WsMessage, result)
    check_message(expected, expected_kind, message)
    IO.try(Unit, S.Ws.close(socket, 1000, "done", 2000))
    IO.print("received")

def timer() -> IO(Unit):
  do IO<Unit>:
    IO.sleep(0)
    IO.print("timer")

def sent(pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(S.WebSocket):
  (socket, result) = pair
  do IO<S.WebSocket>:
    IO.pass(Unit, result)
    return socket

def sends(n: Nat, socket: S.WebSocket) -> IO(S.WebSocket):
  match n:
    case 0n:
      IO.pure(S.WebSocket, socket)
    case 1n+rest:
      do IO<S.WebSocket>:
        body : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text(""))
        pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 2, body, 2000)
        socket : S.WebSocket <- sent(pair)
        sends(rest, socket)

def run(fairness: Bool, socket: S.WebSocket, path: String, kind: U32) -> IO(Unit):
  match fairness:
    case True{}:
      do IO<Unit>:
        child : Chan(Unit) <- IO.fork(Unit, timer())
        socket : S.WebSocket <- sends(512n, socket)
        IO.print("sent")
        S.Ws.discard(socket)
        IO.join(Unit, child)
    case False{}:
      do IO<Unit>:
        expected : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 1048576))
        pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 2000)
        received(expected, kind, pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    mode : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_MODE"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_PAYLOAD"))
    kind : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_KIND"))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.defaults(), ca), url))
    run(String.eq(mode, "fairness"), socket, path, Maybe.default(&2, U32, U32.read(kind), 2))
