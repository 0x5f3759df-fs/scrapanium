import Base
import ../scrapanium.bend as S
import ../http.bend as H

def print_body(pair: S.Response & String) -> IO(Unit):
  (response, value) = pair
  do IO<Unit>:
    IO.print(value)
    S.discard(response)

def show(result: Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("error:" ++ U32.show(code))
    case Done{response}:
      do IO<Unit>:
        body : S.Response & String <- S.text(response)
        print_body(body)

def drain(results: List<Result<&1, &1, U32 & String, S.Response>>) -> IO(Unit):
  match results:
    case Nil{}:
      IO.pure(Unit, Unit{})
    case Con{head, tail}:
      do IO<Unit>:
        show(head)
        drain(tail)

def finish(pair: S.Session & List<Result<&1, &1, U32 & String, S.Response>>) -> IO(Unit):
  (session, results) = pair
  do IO<Unit>:
    S.close(session)
    drain(results)

def start(+url: String) -> IO(Unit):
  do IO<Unit>:
    body : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_list([0, 255, 192, 128, 65, 0]))
    session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
    results : S.Session & List<Result<&1, &1, U32 & String, S.Response>> <- S.batch(session, [
      S.Request.with_query(S.Request.get(url ++ "/echo?old=1#fragment"), [H.Param{"q", "a &🌍"}, H.Param{"q", ""}]),
      S.Request.form(url ++ "/echo", [H.Param{"q", "a +~*"}, H.Param{"q", "héllo"}]),
      S.Request.with_headers(S.Request.binary("POST", url ++ "/echo", body),
        H.Headers{[H.Header{"Content-Type", "application/octet-stream"}, H.Header{"X-Dup", "one"}, H.Header{"x-dup", "two"}, H.Header{"X-Empty", ""}]}),
      S.Request.with_headers(S.Request.get(url ++ "/echo"), H.Headers{[H.Header{"Bad: name", "value"}]}),
      S.Request.with_headers(S.Request.get(url ++ "/echo"), H.Headers{[H.Header{"X-Good", "bad\r\nInjected: yes"}]})])
    finish(results)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    start(url)
