# Actual browser captures

These historical fixtures capture **Google Chrome 153.0.8010.52**, **Chrome for
Testing 153.0.8010.52**, and **Firefox 156.0** on Linux x86_64. Each browser
runs in a new temporary profile for every sample. Headed samples use Xvfb.

| Profile | Browser build | Mode | Compared behavior |
| --- | --- | --- | --- |
| `chrome153` | Google Chrome | Headed | Initial TCP ClientHello and HTTP/2 navigation. |
| `chrome153_headless` | Chrome for Testing | Headless | Initial TCP ClientHello and HTTP/2 navigation. |
| `firefox156` | Mozilla Firefox | Headed and headless | Initial TCP ClientHello and HTTP/2 navigation. |

The current optional profiles also have separate 2026-09-24 capture evidence:

| Profile | Browser build | Mode | Compared behavior |
| --- | --- | --- | --- |
| `chrome154` | Google Chrome 154.0.8037.57 | Headed | Initial TCP ClientHello and HTTP/2 navigation. |
| `chrome154_headless` | Chrome for Testing 154.0.8037.57 | Headless | Initial TCP ClientHello and HTTP/2 navigation. |
| `firefox156` | Mozilla Firefox 156.0.1 | Headed and headless | Current release confirmation for the existing profile. |

Three samples per browser/mode are retained in each dated set. The historical
headed fixture additionally records Chrome for Testing to expose the difference
between testing and branded Chrome. Google Chrome and Chrome for Testing have
different client-hint brands, header order, and extension behavior in these
captures.

## Reproduce

The capture tools need the test environment, the browser runtime libraries,
`xvfb`, `xauth`, and `libnss3-tools` (`certutil`). Downloads stay in `.deps/`;
the Debian Chrome package is extracted without installing it.

```sh
python3 scripts/fetch_browsers.py
xvfb-run -a .deps/venv-matched/bin/python scripts/capture_browsers.py \
  --google-chrome .deps/browsers/google-chrome/opt/google/chrome/chrome \
  --chrome .deps/browsers/chrome-linux64/chrome \
  --firefox .deps/browsers/firefox/firefox \
  --headed --http2 --samples 3 --output build/browser-headed.json
.deps/venv-matched/bin/python scripts/capture_browsers.py \
  --chrome .deps/browsers/chrome-linux64/chrome \
  --firefox .deps/browsers/firefox/firefox \
  --http2 --samples 3 --output build/browser-headless.json
```

[releases.json](releases.json) pins download URLs and SHA-256 hashes. The reports
record executable hashes, Firefox's `libxul.so` hash, launch flags, preferences,
capture-source hashes, raw TLS records and raw HTTP/2 frames. Chrome runs without
its sandbox for this isolated test harness. The TLS listener closes immediately
after the first ClientHello. HTTP/2 uses a local test CA, imported into Firefox's
temporary NSS database; Chrome trusts only the test certificate's public key.

## Normalization and limits

Tests normalize GREASE values while retaining their counts and vector positions,
random/session/key-share bytes, shuffled extension order, conditional padding
extension 21, SNI, ECH payload bytes and trust-anchor order. Stable extension
payloads, key-share sizes, algorithms and ordered HTTP/2 headers are compared.
The only HTTP/2 header-value substitution is the ephemeral localhost authority;
ACK timing is excluded from frame-order comparison.

The HTTP/2 peer waits for an acknowledgement of a PING sent after request headers
and before response completion. This includes Firefox's per-stream WINDOW_UPDATE
without assuming it arrived in the same socket read as HEADERS.

These fixtures cover the supplied builds, flags, platform, and fresh local
navigation. They do not prove full browser indistinguishability, session
resumption, accepted ECH, HTTP/3, WebSocket handshake parity, field-trial
configurations, macOS, Android or iOS behavior. Safari 27 still needs captures.

Official release metadata: [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json),
[Google Chrome packages](https://dl.google.com/linux/chrome/deb/dists/stable/main/binary-amd64/Packages.gz),
[Firefox versions](https://product-details.mozilla.org/1.0/firefox_versions.json).

## Current release observations (2026-09-24)

Separate three-sample headed and headless captures of Chrome for Testing
154.0.8037.57, headed Google Chrome 154.0.8037.57, and headed/headless Firefox
156.0.1 are retained under
[`captures-2026-09-24/`](captures-2026-09-24/). The dated release lock is
[`releases-2026-09-24.json`](releases-2026-09-24.json). The comparison report
lists every normalized TLS and HTTP/2 difference against the 2026-09-20 captures
and calls out raw ECH payload-length variation. These observations do not
replace the historical capture inputs. The native `chrome154` and
`chrome154_headless` profiles use these captures' exact HTTP/2 headers and the
established Chrome TLS controls: signature GREASE for both modes and the
headless server-padding setting for Chrome for Testing. Their normalized TLS
structures match the 153 captures, but this does not establish raw-byte parity.
The `firefox156` TLS and HTTP/2 structures and observed `rv:156.0` headers also
match the earlier capture, so Firefox 156.0.1 confirms the existing profile
rather than requiring a new ID. Three samples do not estimate an ECH payload
length distribution or prove version-wide equivalence.
