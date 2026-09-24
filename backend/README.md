# Browser backend

An optional source build adds five capture-tested Linux profiles:
`chrome153`, `chrome153_headless`, `chrome154`, `chrome154_headless`, and
`firefox156`.
The normal install keeps the stock transport and its 41 profiles. The browser
build exposes 46 profiles; unknown names fail on either build.

## Build and use

On Ubuntu 26.04 / Linux x86_64, install the normal Scrapanium prerequisites plus
CMake, Ninja, make, patch, Perl, and Go. Then:

```sh
python3 scripts/bootstrap.py --matched
python3 scripts/build_backend.py
export SCRAPANIUM_CURL_DIR="$PWD/.deps/curl-browser"
export LD_LIBRARY_PATH="$SCRAPANIUM_CURL_DIR/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python3 scripts/build.py
.deps/venv-matched/bin/pytest -q tests
```

Use the same environment when compiling Bend examples. `build.py` embeds the
selected library directory in the executable's runtime search path. The
`LD_LIBRARY_PATH` setting also makes the comparison binding load that backend.
An application is not yet a portable standalone package.

Choose a profile inside your Bend `do IO` block with
`session : S.Session <- IO.try(S.Session, S.open(S.browser("chrome153")))`.
The same configuration works with the WebSocket API.

The builder uses an independent directory under `~/.cache/`, downloads pinned
sources, and installs into `.deps/curl-browser`. It does not install system files.
`--work`, `--prefix`, and `--jobs` override those locations and parallelism.
Changed source inputs require a fresh work and installation directory. This is
a repeatable source recipe, not a claim of bit-identical builds across toolchains.

## What changes

| Control | Purpose |
| --- | --- |
| TLS ClientHello controls | Captured Chrome 153 and 154 include signature GREASE in headed and headless modes. CfT headless sends a two-byte server-padding request with value 0; extension 4832 appears in CfT captures and is absent in Google Chrome captures. |
| HTTP/2 stream receive window | Firefox 156 grows each stream from 128 KiB to 12 MiB after HEADERS. |
| HTTP/2 initial stream ID | Firefox 156 starts navigation on stream 3. |

The TLS controls participate in connection and session-cache keys and survive
handle duplication. They apply to TCP TLS. The stream-window callback looks up
the stream's owning request, which matters when multiple requests share a
connection. The patches keep the upstream behavior when no control is selected.

[lock.json](lock.json) pins curl-impersonate and libidn2. The pinned upstream
CMake file supplies SHA-256 checks for the other nine source archives. The build
isolates BoringSSL and the other transport dependencies from system OpenSSL.

## Evidence and scope

[Browser captures](../profiles/browsers/README.md) retain raw TLS records and
HTTP/2 frames, binary hashes, launch settings, and three samples per mode.
Tests compare normalized ClientHello fields and ordered HTTP/2 navigation;
they also check option ranges, handle reset/duplication, connection reuse,
and byte-exact transfers beyond Firefox's initial stream window.

This does not establish browser equivalence for session resumption, accepted
ECH, HTTP/3, other operating systems, browser field trials, or WebSocket request
headers. WS/WSS protocol tests still run against both backends.

The published performance charts use the **stock** backend. They are not
measurements of these new profiles or this source build.

## Build artifacts

The installation includes `manifest.json` with input and output hashes,
compiler details and ELF dependencies, plus dependency licenses and the libidn2
source archive. `--archive build/curl-browser.tar.gz` additionally packages the
installation for local use. Keep the manifest, source recipe and dependency
notices together; Scrapanium's MIT license does not relicense its dependencies.
Public binary releases and their full distribution review remain pending.
