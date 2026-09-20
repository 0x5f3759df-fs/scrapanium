import Base
import ../scrapanium.bend as S

def print_text(pair: S.Response & String) -> IO(Unit):
  (response, value) = pair
  do IO<Unit>:
    IO.print(value)
    S.discard(response)

def show_result(result: Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("error:" ++ U32.show(code))
    case Done{response}:
      do IO<Unit>:
        pair : S.Response & String <- S.text(response)
        print_text(pair)

def drain(results: List<Result<&1, &1, U32 & String, S.Response>>) -> IO(Unit):
  match results:
    case Nil{}:
      IO.pure(Unit, Unit{})
    case Con{head, tail}:
      do IO<Unit>:
        show_result(head)
        drain(tail)

def finish(pair: S.Session & List<Result<&1, &1, U32 & String, S.Response>>) -> IO(Unit):
  (session, results) = pair
  do IO<Unit>:
    S.close(session)
    drain(results)

def start(+url: String) -> IO(Unit):
  do IO<Unit>:
    session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
    pair : S.Session & List<Result<&1, &1, U32 & String, S.Response>> <- S.batch(session,
      [S.Request.get(url ++ "/unicode"), S.Request.get("file:///etc/passwd"),
       S.Request.get(url ++ "/chunked"), S.Request.get(url ++ "/"),
       S.Request{"POST", url ++ "/echo", ["X-Bend: works"], "payload"}])
    finish(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    start(url)
