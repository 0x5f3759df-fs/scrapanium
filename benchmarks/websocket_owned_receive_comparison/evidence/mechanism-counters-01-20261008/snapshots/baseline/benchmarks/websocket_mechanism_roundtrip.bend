import Base
import ../scrapanium.bend as S

def WsBenchDiag.mark(phase: String) -> IO(Unit):
  IO.try(Unit, WsBenchDiag.checked_mark(phase))

def WsBenchDiag.checked_mark(phase: String) -> IO(Result<&1, &1, U32 & String, Unit>):
  import "./websocket_mechanism_diag.c"

# Exact byte and opcode checks are inside timing. Returned echo bytes are reused.
def checked(pair: (S.Bytes & S.Bytes) & Bool) -> IO(S.Bytes & S.Bytes):
  (buffers, equal) = pair
  match equal:
    case True{}:
      IO.pure(S.Bytes & S.Bytes, buffers)
    case False{}:
      IO.die(S.Bytes & S.Bytes, 1, "WebSocket payload mismatch")

def check_kind(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "WebSocket opcode mismatch")

def check_message(socket: S.WebSocket, expected: S.Bytes, message: S.WsMessage) -> IO(S.WebSocket & (S.Bytes & S.Bytes)):
  match message:
    case S.WsMessage{kind, body}:
      do IO<S.WebSocket & (S.Bytes & S.Bytes)>:
        check_kind(U32.is_eq(kind, 2))
        equality : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(body, expected)
        buffers : S.Bytes & S.Bytes <- checked(equality)
        return (socket, buffers)

def receive_done(expected: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(S.WebSocket & (S.Bytes & S.Bytes)):
  (socket, result) = pair
  do IO<S.WebSocket & (S.Bytes & S.Bytes)>:
    message : S.WsMessage <- IO.pass(S.WsMessage, result)
    check_message(socket, expected, message)

def send_done(expected: S.Bytes, pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(S.WebSocket & (S.Bytes & S.Bytes)):
  (socket, result) = pair
  do IO<S.WebSocket & (S.Bytes & S.Bytes)>:
    IO.pass(Unit, result)
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    receive_done(expected, pair)

def repeat(n: Nat, state: S.WebSocket & (S.Bytes & S.Bytes)) -> IO(S.WebSocket & (S.Bytes & S.Bytes)):
  match n:
    case 0n:
      IO.pure(S.WebSocket & (S.Bytes & S.Bytes), state)
    case 1n+rest:
      (socket, buffers) = state
      (data, expected) = buffers
      do IO<S.WebSocket & (S.Bytes & S.Bytes)>:
        pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 2, data, 5000)
        state : S.WebSocket & (S.Bytes & S.Bytes) <- send_done(expected, pair)
        repeat(rest, state)

def release(state: S.WebSocket & (S.Bytes & S.Bytes)) -> IO(Unit):
  (socket, buffers) = state
  (data, expected) = buffers
  do IO<Unit>:
    S.Bytes.discard(data)
    S.Bytes.discard(expected)
    S.Ws.discard(socket)

def gate_done(pair: File & Result<&1, &1, U32 & String, String>) -> IO(Unit):
  (file, result) = pair
  do IO<Unit>:
    signal : String <- IO.pass(String, result)
    File.close(file)

def gate() -> IO(Unit):
  do IO<Unit>:
    file : File <- IO.try(File, File.open("/dev/stdin", "r"))
    IO.print("ready")
    pair : File & Result<&1, &1, U32 & String, String> <- File.read(file, 1)
    gate_done(pair)

def timed(n: Nat, state: S.WebSocket & (S.Bytes & S.Bytes)) -> IO(Unit):
  do IO<Unit>:
    state : S.WebSocket & (S.Bytes & S.Bytes) <- repeat(1n, state)
    gate()
    WsBenchDiag.mark("gate")
    start : Nat <- IO.now()
    state : S.WebSocket & (S.Bytes & S.Bytes) <- repeat(n, state)
    end : Nat <- IO.now()
    WsBenchDiag.mark("work_end")
    release(state)
    WsBenchDiag.mark("released")
    IO.print(Nat.show(start))
    IO.print(Nat.show(end))

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_CA"))
    count : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_COUNT"))
    +payload : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_PAYLOAD"))
    data : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(payload, 1048576))
    expected : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(payload, 1048576))
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.browser("chrome146"), ca), url))
    WsBenchDiag.mark("cold")
    timed(U32.to_nat(Maybe.default(&2, U32, U32.read(count), 1000)), (socket, (data, expected)))
