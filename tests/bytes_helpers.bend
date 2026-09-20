import Base
import ../scrapanium.bend as S
import ../http.bend as H

def bad_list(result: Result<&1, &1, U32 & String, S.Bytes>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("error:" ++ U32.show(code))
    case Done{bytes}:
      S.Bytes.discard(bytes)

def show_byte(value: Maybe<U32>) -> IO(Unit):
  match value:
    case None{}:
      IO.print("none")
    case Some{n}:
      IO.print(U32.show(n))

def text(pair: S.Bytes & String) -> IO(Unit):
  (bytes, value) = pair
  do IO<Unit>:
    IO.print(value)
    S.Bytes.discard(bytes)

def second(pair: S.Bytes & Maybe<U32>) -> IO(Unit):
  (bytes, value) = pair
  do IO<Unit>:
    show_byte(value)
    pair : S.Bytes & String <- S.Bytes.text(bytes)
    text(pair)

def first(pair: S.Bytes & Maybe<U32>) -> IO(Unit):
  (bytes, value) = pair
  do IO<Unit>:
    show_byte(value)
    pair : S.Bytes & Maybe<U32> <- S.Bytes.at(bytes, 999)
    second(pair)

def show_body(pair: S.Response & String) -> IO(Unit):
  (response, body) = pair
  do IO<Unit>:
    IO.print(body)
    S.discard(response)

def reupload(url: String, bytes: S.Bytes) -> IO(Unit):
  match bytes:
    case S.Bytes{handle, size}:
      do IO<Unit>:
        response : S.Response <- IO.try(S.Response, S.fetch(S.defaults(),
          S.Request.with_query(S.Request.binary("POST", url ++ "/echo", S.Bytes{handle, 0}), [H.Param{"binary", "yes"}])))
        pair : S.Response & String <- S.text(response)
        show_body(pair)

def transfer(+url: String) -> IO(Unit):
  do IO<Unit>:
    response : S.Response <- IO.try(S.Response, S.get(url ++ "/binary"))
    bytes : S.Bytes <- S.into_bytes(response)
    reupload(url, bytes)

def main() -> IO(Unit):
  do IO<Unit>:
    invalid : Result<&1, &1, U32 & String, S.Bytes> <- S.Bytes.from_list([0, 256, 255])
    bad_list(invalid)
    bytes : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_text("héllo" ++ SCon{Chr{0}, "🌍"}))
    pair : S.Bytes & Maybe<U32> <- S.Bytes.at(bytes, 0)
    first(pair)
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    transfer(url)
