import Base
import ../http.bend as H

def code_value(code: Maybe<&2, U32>) -> String:
  match code:
    case None{}:
      "invalid-number"
    case Some{value}:
      H.Query.escape(SCon{Chr{value}, SNil{}})

def probe(+value: String, base: String, name: String, code: String) -> IO(Unit):
  do IO<Unit>:
    IO.print(H.Query.escape(value))
    IO.print(H.Form.escape(value))
    IO.print(H.Query.append(base, [H.Param{"key", value}, H.Param{"key", ""}, H.Param{"", "v"}]))
    IO.print(Bool.show(H.Headers.valid(H.Headers{[H.Header{name, value}]})))
    IO.print(String.join(H.Headers.get_all(H.Headers{[H.Header{"X-A", "one"}, H.Header{"x-a", "two"}, H.Header{"X-B", "else"}]}, "X-a"), "|"))
    IO.print(String.join(H.Headers.get_all(H.Headers.set(H.Headers{[H.Header{"X-A", "one"}, H.Header{"x-a", "two"}]}, "x-A", "new"), "X-A"), "|"))
    IO.print(code_value(U32.read(code)))
    IO.print(H.Query.append("/unchanged?#tail#two", []))

def main() -> IO(Unit):
  do IO<Unit>:
    value : String <- IO.try(String, IO.get_env("PROBE_VALUE"))
    base : String <- IO.try(String, IO.get_env("PROBE_BASE"))
    name : String <- IO.try(String, IO.get_env("PROBE_NAME"))
    code : String <- IO.try(String, IO.get_env("PROBE_CODE"))
    probe(value, base, name, code)
