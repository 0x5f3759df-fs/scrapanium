import Base

# Ordered pairs preserve duplicate names and empty values.
type Param is Data:
  Param{name: String, value: String}

type Header is Data:
  Header{name: String, value: String}

type Headers is Data:
  Headers{items: List<&2, Header>}

def hex_digit(+x: U32) -> Char:
  Chr{Bool.pick(U32, (x < 10 : U32), (48 + x : U32), (55 + x : U32))}

def escaped_byte(+x: U32) -> String:
  SCon{Chr{37}, SCon{hex_digit(U32.shrn(x, 4n)), SCon{hex_digit(U32.and(x, 15)), SNil{}}}}

def encoded_percent(x: U32, space: Bool) -> String:
  match space:
    case True{}:
      "+"
    case False{}:
      escaped_byte(x)

def encoded_byte(x: U32, form: Bool, safe: Bool) -> String:
  match safe:
    case True{}:
      SCon{Chr{x}, SNil{}}
    case False{}:
      encoded_percent(x, form)

def ascii_byte(+x: U32, +form: Bool) -> String:
  encoded_byte(x, form && U32.is_eq(x, 32),
    ((x >= 48 : U32) && (x <= 57 : U32)) ||
    ((x >= 65 : U32) && (x <= 90 : U32)) ||
    ((x >= 97 : U32) && (x <= 122 : U32)) ||
    U32.is_eq(x, 45) || U32.is_eq(x, 46) || U32.is_eq(x, 95) ||
    (form && U32.is_eq(x, 42)) || (Bool.not(form) && U32.is_eq(x, 126)))

def utf8_four(+x: U32, small: Bool) -> String:
  match small:
    case True{}:
      escaped_byte((224 + U32.shrn(x, 12n) : U32)) ++
      escaped_byte((128 + U32.and(U32.shrn(x, 6n), 63) : U32)) ++
      escaped_byte((128 + U32.and(x, 63) : U32))
    case False{}:
      escaped_byte((240 + U32.shrn(x, 18n) : U32)) ++
      escaped_byte((128 + U32.and(U32.shrn(x, 12n), 63) : U32)) ++
      escaped_byte((128 + U32.and(U32.shrn(x, 6n), 63) : U32)) ++
      escaped_byte((128 + U32.and(x, 63) : U32))

def utf8_three(+x: U32, small: Bool) -> String:
  match small:
    case True{}:
      escaped_byte((192 + U32.shrn(x, 6n) : U32)) ++ escaped_byte((128 + U32.and(x, 63) : U32))
    case False{}:
      utf8_four(x, (x < 65536 : U32))

def utf8_two(+x: U32, form: Bool, ascii: Bool) -> String:
  match ascii:
    case True{}:
      ascii_byte(x, form)
    case False{}:
      utf8_three(x, (x < 2048 : U32))

def scalar_byte(+x: U32, form: Bool, invalid: Bool) -> String:
  match invalid:
    case True{}:
      "%EF%BF%BD"
    case False{}:
      utf8_two(x, form, (x < 128 : U32))

# Defined separately to make the scalar available at unrestricted quantity.
def encode_code(+code: U32, form: Bool) -> String:
  scalar_byte(code, form, (code > 1114111 : U32) || ((code >= 55296 : U32) && (code <= 57343 : U32)))

def encode_char(c: Char, form: Bool) -> String:
  match c:
    case Chr{code}:
      encode_code(code, form)

def encode_text(value: String, +form: Bool) -> String:
  match value:
    case SNil{}:
      ""
    case SCon{head, tail}:
      encode_char(head, form) ++ encode_text(tail, form)

def Query.escape(value: String) -> String:
  encode_text(value, False{})

def Form.escape(value: String) -> String:
  encode_text(value, True{})

def encode_param(param: Param, +form: Bool) -> String:
  match param:
    case Param{name, value}:
      encode_text(name, form) ++ "=" ++ encode_text(value, form)

def encode_params(params: List<&2, Param>, +form: Bool) -> List<&2, String>:
  match params:
    case Nil{}:
      []
    case Con{head, tail}:
      Con{encode_param(head, form), encode_params(tail, form)}

def Query.encode(params: List<&2, Param>) -> String:
  String.join(encode_params(params, False{}), "&")

def Form.encode(params: List<&2, Param>) -> String:
  String.join(encode_params(params, True{}), "&")

def query_base(+base: String, encoded: String) -> String:
  base ++ Bool.pick(String, String.contains(base, "?"),
    Bool.pick(String, String.ends_with(base, "?") || String.ends_with(base, "&"), "", "&"), "?") ++ encoded

def query_fragment(parts: List<&2, String>) -> String:
  match parts:
    case Nil{}:
      ""
    case Con{head, tail}:
      "#" ++ String.join(Con{head, tail}, "#")

def append_parts(parts: List<&2, String>, encoded: String) -> String:
  match parts:
    case Nil{}:
      "?" ++ encoded
    case Con{base, fragments}:
      query_base(base, encoded) ++ query_fragment(fragments)

def append_nonempty(url: String, encoded: String, empty: Bool) -> String:
  match empty:
    case True{}:
      url
    case False{}:
      append_parts(String.split(url, Chr{35}), encoded)

def Query.append_encoded(url: String, +encoded: String) -> String:
  append_nonempty(url, encoded, String.is_empty(encoded))

def Query.append(url: String, params: List<&2, Param>) -> String:
  Query.append_encoded(url, Query.encode(params))

def Headers.empty() -> Headers:
  Headers{[]}

def Headers.add(headers: Headers, name: String, value: String) -> Headers:
  match headers:
    case Headers{items}:
      Headers{List.append(&2, Header, items, [Header{name, value}])}

def header_name(header: Header) -> String:
  match header:
    case Header{name, value}:
      String.to_lower(name)

def header_value(header: Header) -> String:
  match header:
    case Header{name, value}:
      value

def filter_header(header: Header, +rest: List<&2, Header>, remove: Bool) -> List<&2, Header>:
  Bool.pick(List<&2, Header>, remove, rest, Con{header, rest})

def remove_header(+header: Header, rest: List<&2, Header>, name: String) -> List<&2, Header>:
  filter_header(header, rest, String.eq(header_name(header), name))

def remove_headers(items: List<&2, Header>, +name: String) -> List<&2, Header>:
  match items:
    case Nil{}:
      []
    case Con{head, tail}:
      remove_header(head, remove_headers(tail, name), name)

def Headers.remove(headers: Headers, name: String) -> Headers:
  match headers:
    case Headers{items}:
      Headers{remove_headers(items, String.to_lower(name))}

def Headers.set(headers: Headers, +name: String, value: String) -> Headers:
  Headers.add(Headers.remove(headers, name), name, value)

def keep_value(value: String, +rest: List<&2, String>, keep: Bool) -> List<&2, String>:
  Bool.pick(List<&2, String>, keep, Con{value, rest}, rest)

def find_header(+header: Header, rest: List<&2, String>, name: String) -> List<&2, String>:
  keep_value(header_value(header), rest, String.eq(header_name(header), name))

def find_headers(items: List<&2, Header>, +name: String) -> List<&2, String>:
  match items:
    case Nil{}:
      []
    case Con{head, tail}:
      find_header(head, find_headers(tail, name), name)

def Headers.get_all(headers: Headers, name: String) -> List<&2, String>:
  match headers:
    case Headers{items}:
      find_headers(items, String.to_lower(name))

def Headers.get(headers: Headers, name: String) -> Maybe<&2, String>:
  List.head(&2, String, Headers.get_all(headers, name))

def token_char(+c: Char) -> Bool:
  Char.is_alpha(c) || Char.is_digit(c) || String.contains("!#$%&'*+-.^_`|~", SCon{c, SNil{}})

def value_code(+code: U32) -> Bool:
  U32.is_eq(code, 9) || ((code >= 32 : U32) && U32.is_ne(code, 127))

def value_char(c: Char) -> Bool:
  match c:
    case Chr{code}:
      value_code(code)

def valid_fields(+name: String, value: String) -> Bool:
  Bool.not(String.is_empty(name)) && List.all(&2, Char, c => token_char(c), String.to_list(name)) &&
    List.all(&2, Char, value_char, String.to_list(value))

def valid_header(header: Header) -> Bool:
  match header:
    case Header{name, value}:
      valid_fields(name, value)

def Headers.valid(headers: Headers) -> Bool:
  match headers:
    case Headers{items}:
      List.all(&2, Header, valid_header, items)

def header_line(name: String, value: String, empty: Bool) -> String:
  match empty:
    case True{}:
      name ++ ";"
    case False{}:
      name ++ ": " ++ value

def make_line(name: String, +value: String) -> String:
  header_line(name, value, String.is_empty(value))

def header_lines(items: List<&2, Header>) -> List<String>:
  match items:
    case Nil{}:
      []
    case Con{head, tail}:
      match head:
        case Header{name, value}:
          Con{make_line(name, value), header_lines(tail)}

def unchecked_lines(headers: Headers) -> List<String>:
  match headers:
    case Headers{items}:
      header_lines(items)

def checked_lines(headers: Headers, valid: Bool) -> List<String>:
  match valid:
    case False{}:
      ["\n"]
    case True{}:
      unchecked_lines(headers)

# Invalid structured fields become a rejected raw header, never a different name.
def Headers.lines(+headers: Headers) -> List<String>:
  checked_lines(headers, Headers.valid(headers))
