# Getting started

Scrapanium currently supports **Linux x86_64**, including WSL2. It uses Bend
2.0.17's native C target. The JavaScript target is not supported.

## Install

You need Python 3.12+, npm, git, clang 21, and the C build dependencies below.
Ubuntu 26.04 supplies the tested compiler version.

```sh
sudo apt-get update
sudo apt-get install clang build-essential git nodejs npm python3-venv python3-dev libffi-dev ca-certificates time

git clone https://github.com/0x5f3759df-fs/scrapanium.git
cd scrapanium
python3 scripts/bootstrap.py
```

Bootstrap downloads the pinned compiler and transport into `.deps/`. It verifies
the transport archive's SHA-256 and checks out Bend at an exact commit.
The versions and hashes are in [dependencies.json](../dependencies.json).

For the additional Chrome 153 and Firefox 156 profiles, follow the optional
[browser-backend build](../backend/README.md). It compiles a separate transport
from pinned sources; the normal install and benchmark defaults remain stock.

## Run the example

```sh
python3 scripts/build.py examples/get.bend -o build/get
./build/get --threads 1
```

This prints the response from `https://example.com`. The build helper compiles
Bend to C and links the native transport. Use this helper instead of plain
`bend example.bend`, which does not supply the required linker options.

## Write your own request

Save the following as `hello.bend` in the repository root:

```bend
import Base
import ./scrapanium.bend as S

def print_body(pair: S.Response & String) -> IO(Unit):
  (response, body) = pair
  do IO<Unit>:
    IO.print(body)
    S.discard(response)

def main() -> IO(Unit):
  do IO<Unit>:
    response : S.Response <- IO.try(S.Response, S.get("https://example.com"))
    pair : S.Response & String <- S.text(response)
    print_body(pair)
```

```sh
python3 scripts/build.py hello.bend -o build/hello
./build/hello --threads 1
```

`get` closes its one-shot session automatically. `text` returns both the response
and its decoded body; `discard` releases the response's native memory.
`IO.try` propagates a transport failure. HTTP 4xx/5xx remain ordinary responses;
check `Response.status` when your application needs a successful HTTP status.

## Keep a connection open

For repeated requests, `open` a session once, pass it through `send` or `batch`,
then `close` it. A send returns the session even when the request fails. Sessions
and response buffers have one owner; always close or discard them after use.

- [Pooled request example](../examples/get.bend)
- [Requests, headers and binary bodies](REQUESTS.md)
- [Browser profiles and custom settings](PROFILES.md)
- [WebSocket guide](WEBSOCKETS.md)
- [API reference](API.md)

## Run tests

The full suite additionally needs Go 1.26 for the independent profile comparison.

```sh
python3 scripts/bootstrap.py --matched
python3 scripts/build.py
.deps/venv-matched/bin/pytest -q tests
```

The `--matched` environment builds curl_cffi against the same transport library
used by Scrapanium. See [validation](VALIDATION.md) for coverage and
[benchmarks](../benchmarks/RESULTS.md) for reproduction commands.
