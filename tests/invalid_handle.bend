import Base
import ../scrapanium.bend as S

# Attempt to misuse a File as a Session must be rejected before pointer access.
def main() -> IO(Unit):
  do IO<Unit>:
    file : File <- IO.try(File, File.open("/dev/null", "r"))
    S.close(S.Session{file})
