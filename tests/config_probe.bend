import Base
import ../scrapanium.bend as S

def enabled(off: Bool) -> Bool:
  match off:
    case True{}:
      False{}
    case False{}:
      True{}

def stock_fingerprint(bad: Bool) -> S.Fingerprint:
  match bad:
    case True{}:
      S.Fingerprint{"", "", "", "", "", "", "", 99, 0, 0}
    case False{}:
      S.Fingerprint.inherit()

def fingerprint(custom: Bool, bad: Bool) -> S.Fingerprint:
  match custom:
    case True{}:
      S.Fingerprint{"", "P-256:P-384", "", "", "", "", "", 1, 1, 0}
    case False{}:
      stock_fingerprint(bad)

def limits(small: Bool) -> S.Limits:
  match small:
    case True{}:
      S.Limits{1024, 1}
    case False{}:
      S.Limits.defaults()

def config(ca: String, +mode: String) -> S.Config:
  S.Config{"chrome150", "", ca, 10000, 5000, 10, 2, 65536, 16384,
    enabled(String.eq(mode, "verify-off")), enabled(String.eq(mode, "redirect-off")), enabled(String.eq(mode, "headers-off")),
    fingerprint(String.eq(mode, "custom"), String.eq(mode, "bad-fingerprint")), limits(String.eq(mode, "budget"))}

def print_text(pair: S.Response & String) -> IO(Unit):
  (response, text) = pair
  do IO<Unit>:
    IO.print(text)
    S.discard(response)

def show(result: Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("error:" ++ U32.show(code))
    case Done{response}:
      do IO<Unit>:
        pair : S.Response & String <- S.text(response)
        print_text(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    ca : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_CA"))
    mode : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_MODE"))
    result : Result<&1, &1, U32 & String, S.Response> <- S.fetch(config(ca, mode), S.Request.get(url))
    show(result)
