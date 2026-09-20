import Base
import ../scrapanium.bend as S

def duplicate(bytes: S.Bytes) -> S.Request & S.Request:
  (S.Request.binary("POST", "https://example.com", bytes), S.Request.binary("POST", "https://example.com", bytes))
