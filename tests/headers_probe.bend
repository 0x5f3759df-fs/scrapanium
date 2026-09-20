import Base
import ../scrapanium.bend as S
import ../http.bend as H

def print_items(items: List<&2, H.Header>) -> IO(Unit):
  match items:
    case Nil{}:
      IO.pure(Unit, Unit{})
    case Con{head, tail}:
      match head:
        case H.Header{name, value}:
          do IO<Unit>:
            IO.print(name ++ "=" ++ value)
            print_items(tail)

def print_headers(headers: H.Headers) -> IO(Unit):
  match headers:
    case H.Headers{items}:
      print_items(items)

def trailers(pair: S.Response & H.Headers) -> IO(Unit):
  (response, headers) = pair
  do IO<Unit>:
    IO.print("--trailers--")
    print_headers(headers)
    S.discard(response)

def regular(pair: S.Response & H.Headers) -> IO(Unit):
  (response, headers) = pair
  do IO<Unit>:
    print_headers(headers)
    pair : S.Response & H.Headers <- S.response_trailers(response)
    trailers(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    proxy : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_PROXY"))
    response : S.Response <- IO.try(S.Response, S.fetch(S.Config.with_proxy(S.Config.with_ca(S.defaults(), ca), proxy), S.Request.get(url)))
    pair : S.Response & H.Headers <- S.response_headers(response)
    regular(pair)
