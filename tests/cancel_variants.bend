import Base
import ../scrapanium.bend as S

def item(result: Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("batch:" ++ U32.show(code))
    case Done{response}:
      S.discard(response)

def drain(results: List<Result<&1, &1, U32 & String, S.Response>>) -> IO(Unit):
  match results:
    case Nil{}:
      IO.pure(Unit, Unit{})
    case Con{head, tail}:
      do IO<Unit>:
        item(head)
        drain(tail)

def info(result: Result<&1, &1, U32 & String, S.DownloadInfo>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("download:" ++ U32.show(code))
    case Done{download}:
      IO.print("unexpected-success")

def finish(token: S.CancelToken, pair: S.Session & Result<&1, &1, U32 & String, S.DownloadInfo>) -> IO(Unit):
  (session, result) = pair
  do IO<Unit>:
    info(result)
    S.close(session)
    IO.try(Unit, S.cancel_release(token))

def batched(url: String, path: String, +token: S.CancelToken, pair: S.Session & List<Result<&1, &1, U32 & String, S.Response>>) -> IO(Unit):
  (session, results) = pair
  do IO<Unit>:
    drain(results)
    pair : S.Session & Result<&1, &1, U32 & String, S.DownloadInfo> <- S.download_cancel(session, S.Request.get(url), path, token)
    finish(token, pair)

def start(url: String, path: String, +token: S.CancelToken) -> IO(Unit):
  do IO<Unit>:
    IO.try(Unit, S.cancel(token))
    body : S.Bytes <- IO.try(S.Bytes, S.Bytes.from_list([0, 255, 192, 128]))
    session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
    pair : S.Session & List<Result<&1, &1, U32 & String, S.Response>> <- S.batch_cancel(session,
      [S.Request.binary("POST", "http://127.0.0.1:1/", body), S.Request.get("file:///invalid")], token)
    batched(url, path, token, pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_OUTPUT"))
    token : S.CancelToken <- IO.try(S.CancelToken, S.cancel_new())
    start(url, path, token)
