import Base
import ../scrapanium.bend as S

def report(result: Result<&1, &1, U32 & String, S.DownloadInfo>) -> IO(Unit):
  match result:
    case Fail{error}:
      (code, message) = error
      IO.print("Download failed (" ++ U32.show(code) ++ "): " ++ message)
    case Done{info}:
      match info:
        case S.DownloadInfo{status, bytes, url, headers}:
          IO.print("Saved " ++ U32.show(bytes) ++ " bytes from " ++ url)

def finish(pair: S.Session & Result<&1, &1, U32 & String, S.DownloadInfo>) -> IO(Unit):
  (session, result) = pair
  do IO<Unit>:
    S.close(session)
    report(result)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_URL"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_OUTPUT"))
    session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
    pair : S.Session & Result<&1, &1, U32 & String, S.DownloadInfo> <- S.download(session, S.Request.get(url), path)
    finish(pair)
