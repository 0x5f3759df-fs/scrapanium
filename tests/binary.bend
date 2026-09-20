import Base
import ../scrapanium.bend as S

def finish_save(pair: S.Response & Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  (response, saved) = pair
  do IO<Unit>:
    S.discard(response)
    IO.pass(Unit, saved)

def save_response(pair: S.Response & Maybe<U32>) -> IO(Unit):
  (response, byte) = pair
  do IO<Unit>:
    IO.print(U32.show(Maybe.default(&1, U32, byte, 999)))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_OUTPUT"))
    saved : S.Response & Result<&1, &1, U32 & String, Unit> <- S.save(response, path)
    finish_save(saved)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    response : S.Response <- IO.try(S.Response, S.get(url ++ "/binary"))
    pair : S.Response & Maybe<U32> <- S.byte_at(response, 1)
    save_response(pair)
