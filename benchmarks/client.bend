import Base
import ../scrapanium.bend as S

def request(url: String) -> S.Request:
  S.Request{"GET", url, ["User-Agent: scrapanium-benchmark", "Accept: */*"], ""}

def assert_status(ok: Bool) -> IO(Unit):
  match ok:
    case True{}:
      IO.pure(Unit, Unit{})
    case False{}:
      IO.die(Unit, 1, "benchmark received a non-200 response")

def discard_checked(response: S.Response) -> IO(Unit):
  S.Response{handle, +status, size, version} = response
  do IO<Unit>:
    S.discard(S.Response{handle, status, size, version})
    assert_status(U32.is_eq(status, 200))

def discard_results(xs: List<Result<&1, &1, U32 & String, S.Response>>) -> IO(Unit):
  match xs:
    case Nil{}:
      IO.pure(Unit, Unit{})
    case Con{x, rest}:
      do IO<Unit>:
        r : S.Response <- IO.pass(S.Response, x)
        discard_checked(r)
        discard_results(rest)

def finish_batch(pair: S.Session & List<Result<&1, &1, U32 & String, S.Response>>) -> IO(S.Session):
  (session, results) = pair
  do IO<S.Session>:
    discard_results(results)
    return session

def requests(n: Nat, +url: String) -> List<S.Request>:
  match n:
    case 0n:
      Nil{}
    case 1n+p:
      request(url) <> requests(p, url)

def finish_one(pair: S.Session & Result<&1, &1, U32 & String, S.Response>) -> IO(S.Session):
  (session, result) = pair
  do IO<S.Session>:
    response : S.Response <- IO.pass(S.Response, result)
    discard_checked(response)
    return session

def sequential(n: Nat, +url: String, session: S.Session) -> IO(S.Session):
  match n:
    case 0n:
      IO.pure(S.Session, session)
    case 1n+p:
      do IO<S.Session>:
        pair : S.Session & Result<&1, &1, U32 & String, S.Response> <- S.send(session, request(url))
        next : S.Session <- finish_one(pair)
        sequential(p, url, next)

def choose(mode: Bool, n: Nat, url: String, session: S.Session) -> IO(S.Session):
  match mode:
    case True{}:
      do IO<S.Session>:
        pair : S.Session & List<Result<&1, &1, U32 & String, S.Response>> <- S.batch(session, requests(n, url))
        finish_batch(pair)
    case False{}:
      sequential(n, url, session)

def run(mode: String, n: Nat, url: String, session: S.Session) -> IO(S.Session):
  choose(String.eq(mode, "batch"), n, url, session)

def timed(+url: String, mode: String, n: Nat, session: S.Session) -> IO(Unit):
  do IO<Unit>:
    pair : S.Session & Result<&1, &1, U32 & String, S.Response> <- S.send(session, request(url))
    session : S.Session <- finish_one(pair)
    start : Nat <- IO.now()
    session : S.Session <- run(mode, n, url, session)
    end : Nat <- IO.now()
    S.close(session)
    IO.print(Nat.show(Nat.sub(end, start)))

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_URL"))
    mode : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_MODE"))
    count : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_COUNT"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_BENCH_CA"))
    session : S.Session <- IO.try(S.Session, S.open(S.Config{"chrome146", "", ca,
      30000, 10000, 10, 16, 67108864, 262144, True{}, True{}, False{}, S.Fingerprint.inherit(), S.Limits.defaults()}))
    timed(url, mode, U32.to_nat(Maybe.default(&2, U32, U32.read(count), 1000)), session)
