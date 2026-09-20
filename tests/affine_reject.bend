import Base
import ../scrapanium.bend as S

# This is an intentional negative compile test.
def duplicate(session: S.Session) -> S.Session & S.Session:
  (session, session)
