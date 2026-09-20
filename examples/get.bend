import Base
import ../scrapanium.bend as S

def print_body(pair: S.Response & String) -> IO(Unit):
  (response, body) = pair
  do IO<Unit>:
    IO.print(body)
    S.discard(response)

def finish(pair: S.Session & Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  (session, result) = pair
  do IO<Unit>:
    S.close(session)
    response : S.Response <- IO.pass(S.Response, result)
    pair : S.Response & String <- S.text(response)
    print_body(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
    pair : S.Session & Result<&1, &1, U32 & String, S.Response> <- S.send(session, S.Request.get("https://example.com"))
    finish(pair)
