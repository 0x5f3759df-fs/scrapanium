import Base
import ../scrapanium.bend as S

def show_body(pair: S.Response & String) -> IO(Unit):
  (response, body) = pair
  do IO<Unit>:
    IO.print(body)
    S.discard(response)

def show_response(result: Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("error:" ++ U32.show(code))
    case Done{response}:
      do IO<Unit>:
        pair : S.Response & String <- S.text(response)
        show_body(pair)

def upload(url: String, result: Result<&1, &1, U32 & String, S.Bytes>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("error:" ++ U32.show(code))
    case Done{bytes}:
      do IO<Unit>:
        result : Result<&1, &1, U32 & String, S.Response> <- S.fetch(S.defaults(), S.Request.binary("POST", url, bytes))
        show_response(result)

def limit(value: Maybe<&2, U32>) -> U32:
  match value:
    case None{}:
      0
    case Some{n}:
      n

def main() -> IO(Unit):
  do IO<Unit>:
    path : String <- IO.try(String, IO.get_env("PROBE_FILE"))
    max_bytes : String <- IO.try(String, IO.get_env("PROBE_LIMIT"))
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    bytes : Result<&1, &1, U32 & String, S.Bytes> <- S.Bytes.read_file(path, limit(U32.read(max_bytes)))
    upload(url, bytes)
