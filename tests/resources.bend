import Base
import ../scrapanium.bend as S

def report(result: Result<&1, &1, U32 & String, Unit>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("token:" ++ U32.show(code))
    case Done{value}:
      IO.print("token:ok")

def trigger_later(+token: S.CancelToken) -> IO(Unit):
  do IO<Unit>:
    IO.sleep(60)
    IO.print("timer")
    IO.try(Unit, S.cancel(token))
    IO.try(Unit, S.cancel_release(token))

def show_download(result: Result<&1, &1, U32 & String, S.DownloadInfo>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("download-error:" ++ U32.show(code))
    case Done{info}:
      match info:
        case S.DownloadInfo{status, bytes, url, headers}:
          IO.print("download:" ++ U32.show(status) ++ ":" ++ U32.show(bytes))

def final(pair: S.Session & Result<&1, &1, U32 & String, S.DownloadInfo>) -> IO(Unit):
  (session, result) = pair
  do IO<Unit>:
    show_download(result)
    S.close(session)

def downloaded(+url: String, path: String, pair: S.Session & Result<&1, &1, U32 & String, S.DownloadInfo>) -> IO(Unit):
  (session, result) = pair
  do IO<Unit>:
    show_download(result)
    pair : S.Session & Result<&1, &1, U32 & String, S.DownloadInfo> <- S.download(session, S.Request.get(url ++ "/truncated"), path)
    final(pair)

def show_response(result: Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("request-error:" ++ U32.show(code))
    case Done{response}:
      S.discard(response)

def cancelled(+url: String, +path: String, +token: S.CancelToken, child: Chan(Unit), pair: S.Session & Result<&1, &1, U32 & String, S.Response>) -> IO(Unit):
  (session, result) = pair
  do IO<Unit>:
    show_response(result)
    IO.join(Unit, child)
    released : Result<&1, &1, U32 & String, Unit> <- S.cancel_release(token)
    report(released)
    stale : Result<&1, &1, U32 & String, Unit> <- S.cancel(token)
    report(stale)
    pair : S.Session & Result<&1, &1, U32 & String, S.DownloadInfo> <- S.download(session, S.Request.get(url ++ "/bytes/1048576"), path)
    downloaded(url, path, pair)

def start_token(+url: String, path: String, +token: S.CancelToken) -> IO(Unit):
  do IO<Unit>:
    child : Chan(Unit) <- IO.fork(Unit, trigger_later(token))
    session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
    pair : S.Session & Result<&1, &1, U32 & String, S.Response> <- S.send_cancel(session, S.Request.get(url ++ "/slow/3000"), token)
    cancelled(url, path, token, child, pair)

def start(url: String, path: String) -> IO(Unit):
  do IO<Unit>:
    token : S.CancelToken <- IO.try(S.CancelToken, S.cancel_new())
    start_token(url, path, token)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_URL"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_TEST_OUTPUT"))
    start(url, path)
