import Base
import ../scrapanium.bend as S

def buffered_fields(handle: File, status: U32, +size: U32, version: U32) -> IO(Unit):
  do IO<Unit>:
    IO.print(U32.show(size))
    S.discard(S.Response{handle, status, size, version})

def buffered(response: S.Response) -> IO(Unit):
  match response:
    case S.Response{handle, status, size, version}:
      buffered_fields(handle, status, size, version)

def info(download: S.DownloadInfo) -> IO(Unit):
  match download:
    case S.DownloadInfo{status, bytes, url, headers}:
      IO.print(U32.show(bytes))

def streamed(pair: S.Session & Result<&1, &1, U32 & String, S.DownloadInfo>) -> IO(Unit):
  (session, result) = pair
  do IO<Unit>:
    S.close(session)
    download : S.DownloadInfo <- IO.try(S.DownloadInfo, IO.pure(Result<&1, &1, U32 & String, S.DownloadInfo>, result))
    info(download)

def run(url: String, path: String, memory: Bool) -> IO(Unit):
  match memory:
    case True{}:
      do IO<Unit>:
        response : S.Response <- IO.try(S.Response, S.get(url))
        buffered(response)
    case False{}:
      do IO<Unit>:
        session : S.Session <- IO.try(S.Session, S.open(S.defaults()))
        pair : S.Session & Result<&1, &1, U32 & String, S.DownloadInfo> <- S.download(session, S.Request.get(url), path)
        streamed(pair)

def main() -> IO(Unit):
  do IO<Unit>:
    url : String <- IO.try(String, IO.get_env("SCRAPANIUM_URL"))
    path : String <- IO.try(String, IO.get_env("SCRAPANIUM_OUTPUT"))
    mode : String <- IO.try(String, IO.get_env("SCRAPANIUM_MODE"))
    run(url, path, String.eq(mode, "buffer"))
