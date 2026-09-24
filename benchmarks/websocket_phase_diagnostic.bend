import Base
import ../scrapanium.bend as S
import ./websocket.bend as B

def WsPhase.event(event: Nat) -> IO(Nat):
  import "./websocket_phase_clock.c"

def tag(+sequence: String) -> String:
  String.take("00000000", Nat.sub(8n, String.length(sequence))) ++ sequence

def prepare(n: Nat, +index: Nat, +body: String) -> IO(List<S.Bytes>):
  match n:
    case 0n:
      IO.pure(List<S.Bytes>, [])
    case 1n+rest:
      do IO<List<S.Bytes>>:
        message : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text(tag(Nat.show(index)) ++ body))
        tail : List<S.Bytes> <- prepare(rest, Nat.add(index, 1n), body)
        return Con{message, tail}

def prepared_body(count: Nat, pair: S.Bytes & String) -> IO(List<S.Bytes>):
  (bytes, body) = pair
  do IO<List<S.Bytes>>:
    S.Bytes.discard(bytes)
    prepare(count, 0n, body)

def require(valid: Bool) -> IO(Unit):
  match valid:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "WebSocket opcode, sequence, or payload mismatch")

def mark(phase: Bool, event: Nat) -> IO(Unit):
  match phase:
    case True{}:
      do IO<Unit>:
        ignored : Nat <- WsPhase.event(event)
        return Unit{}
    case False{}:
      IO.pure(Unit, Unit{})

def check_equality(phase: Bool, kind: U32,
    pair: (S.Bytes & S.Bytes) & Bool) -> IO(Unit):
  (buffers, equal) = pair
  (actual, wanted) = buffers
  do IO<Unit>:
    S.Bytes.discard(actual)
    S.Bytes.discard(wanted)
    require(U32.is_eq(kind, 2) && equal)
    timing_stop : Unit <- mark(phase, 5n)
    return Unit{}

def check_message(expected: S.Bytes, +phase: Bool, message: S.WsMessage) -> IO(Unit):
  match message:
    case S.WsMessage{kind, body}:
      do IO<Unit>:
        timing_start : Unit <- mark(phase, 4n)
        pair : (S.Bytes & S.Bytes) & Bool <- S.Bytes.equal(body, expected)
        check_equality(phase, kind, pair)

def received(expected: S.Bytes, +phase: Bool,
    pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(S.WebSocket):
  (socket, result) = pair
  do IO<S.WebSocket>:
    message : S.WsMessage <- IO.pass(S.WsMessage, result)
    receive_stop : Unit <- mark(phase, 3n)
    check_message(expected, phase, message)
    return socket

def receive_one(expected: S.Bytes, +phase: Bool, socket: S.WebSocket) -> IO(S.WebSocket):
  do IO<S.WebSocket>:
    receive_start : Unit <- mark(phase, 2n)
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    received(expected, phase, pair)

def receive_all(expected: List<S.Bytes>, +phase: Bool, socket: S.WebSocket) -> IO(S.WebSocket):
  match expected:
    case Nil{}:
      IO.pure(S.WebSocket, socket)
    case Con{head, tail}:
      do IO<S.WebSocket>:
        socket : S.WebSocket <- receive_one(head, phase, socket)
        receive_all(tail, phase, socket)

def sent(pair: S.WebSocket & Result<&1, &1, U32 & String, Unit>) -> IO(S.WebSocket):
  (socket, result) = pair
  do IO<S.WebSocket>:
    IO.pass(Unit, result)
    return socket

def mode_result(phase: Bool, control: Bool) -> IO(Bool):
  match phase:
    case True{}:
      IO.pure(Bool, True{})
    case False{}:
      match control:
        case True{}:
          IO.pure(Bool, False{})
        case False{}:
          IO.die(Bool, 1, "invalid diagnostic phase mode")

def mode_enabled(+mode: String) -> IO(Bool):
  mode_result(String.eq(mode, "phase"), String.eq(mode, "control"))

def timed(socket: S.WebSocket, expected: List<S.Bytes>, phase: Bool) -> IO(Unit):
  do IO<Unit>:
    signal : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text("start"))
    B.gate()
    ignored : Nat <- WsPhase.event(0n)
    pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 2, signal, 5000)
    socket : S.WebSocket <- sent(pair)
    socket : S.WebSocket <- receive_all(expected, phase, socket)
    total : Nat <- WsPhase.event(1n)
    receive : Nat <- WsPhase.event(6n)
    validation : Nat <- WsPhase.event(7n)
    start_us : Nat <- WsPhase.event(8n)
    end_us : Nat <- WsPhase.event(9n)
    S.Ws.discard(socket)
    IO.print("total_elapsed_ns=" ++ Nat.show(total))
    IO.print("receive_assembly_ns=" ++ Nat.show(receive))
    IO.print("validation_release_ns=" ++ Nat.show(validation))
    IO.print("start_monotonic_us=" ++ Nat.show(start_us))
    IO.print("end_monotonic_us=" ++ Nat.show(end_us))

def warmed_received(expected: List<S.Bytes>, warmup: S.Bytes, phase: Bool,
    pair: S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage>) -> IO(Unit):
  (socket, result) = pair
  do IO<Unit>:
    message : S.WsMessage <- IO.pass(S.WsMessage, result)
    check_message(warmup, False{}, message)
    timed(socket, expected, phase)

def warmed(expected: List<S.Bytes>, phase: Bool, socket: S.WebSocket) -> IO(Unit):
  do IO<Unit>:
    warmup : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text("warmup"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, S.WsMessage> <- S.Ws.receive(socket, 5000)
    warmed_received(expected, warmup, phase, pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_CA"))
    count : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_COUNT"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_BODY"))
    mode : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_PHASE_MODE"))
    phase : Bool <- mode_enabled(mode)
    body : S.Bytes <- IO.try(S.Bytes, S.Bytes.read_file(path, 1048576))
    pair : S.Bytes & String <- S.Bytes.text(body)
    expected : List<S.Bytes> <- prepared_body(U32.to_nat(Maybe.default(&2, U32, U32.read(count), 1)), pair)
    socket : S.WebSocket <- IO.try(S.WebSocket, S.Ws.connect(S.Config.with_ca(S.browser("chrome146"), ca), url))
    warmup : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text("warmup"))
    pair : S.WebSocket & Result<&1, &1, U32 & String, Unit> <- S.Ws.send(socket, 2, warmup, 5000)
    socket : S.WebSocket <- sent(pair)
    warmed(expected, phase, socket)
