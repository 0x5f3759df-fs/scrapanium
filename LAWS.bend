import Base
import ./scrapanium.bend as S

# Scope: these are pure API laws, not claims about the native TLS implementation.
law verification_default:
  {S.defaults() == S.Config{"chrome150", "", "", 30000, 10000, 10, 16,
    67108864, 262144, True{}, True{}, True{}, S.Fingerprint.inherit(), S.Limits.defaults()} : S.Config}

law status_200_success:
  {S.is_success(200) == True{} : Bool}

law status_299_success:
  {S.is_success(299) == True{} : Bool}

law status_300_not_success:
  {S.is_success(300) == False{} : Bool}

law status_199_not_success:
  {S.is_success(199) == False{} : Bool}
