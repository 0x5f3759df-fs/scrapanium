# Browser profile coverage

The versioned [catalog](../profiles/catalog.json) records **46 named profiles**.
The normal install exposes 41. The optional [browser backend](../backend/README.md)
adds five capture-tested profiles. Use `S.profiles()` to enumerate the profiles
in your build.
Unknown names fail; `chrome152` does not silently select the preview. The default
remains the pinned `chrome150`.

| Family | Newest included target | Evidence / limitation |
| --- | --- | --- |
| Chrome desktop | `chrome154` (optional), `chrome153` | Google Chrome 154.0.8037.57 and 153.0.8010.52, Linux headed captures: initial TCP ClientHello and HTTP/2 navigation. Stock default remains `chrome150`. |
| Chrome for Testing | `chrome154_headless` (optional), `chrome153_headless` | CfT 154.0.8037.57 and 153.0.8010.52, Linux headless captures. Its TLS padding control and headers differ from Google Chrome. |
| Chrome 152 preview | `chrome152_preview` | Chrome 150 plus 28 source-derived trust anchors and versioned headers. Known signature GREASE gap. |
| Chrome Android | `chrome131_android` | Newest Android-specific stock target in this backend. |
| Firefox | `firefox156` (optional) | Firefox 156.0 and 156.0.1, Linux headed/headless captures: initial TCP ClientHello and HTTP/2 navigation. Stock newest: `firefox148`. |
| Safari macOS | `safari2601` | Stock Safari 26.0.1; curl_cffi ClientHello parity. |
| Safari iOS | `safari260_ios` | Stock Safari 26.0; curl_cffi ClientHello parity. |
| Tor | `tor145` | Stock Tor 14.5; curl_cffi ClientHello parity. |

Older profiles and Edge/OkHttp targets remain available for reproducible clients.
The catalog is a transport inventory, not a list of currently released browsers.
All five optional profiles pass our scoped [real-browser capture gate](../profiles/browsers/README.md).
Each capture set retains three raw samples per tested mode. Coverage is fresh
TCP TLS and HTTP/2 navigation on the specified Linux builds. Reference-library parity
and capture agreement do not prove indistinguishability across platforms,
resumption, HTTP/3, WebSocket handshakes, field trials or application behavior.

## Freshness gap, checked 2026-09-24

The browser releases have advanced beyond these reference libraries:
[Chrome 154](https://chromereleases.googleblog.com/2026/09/stable-channel-update-for-desktop_0856730748.html),
[Firefox 156](https://www.firefox.com/en-US/firefox/156.0/releasenotes/) and
[Safari 27](https://webkit.org/blog/18325/webkit-features-for-safari-27-0/) are
released. Chrome and Firefox now have capture-tested Linux targets in the
optional backend. Safari 27, current Android/iOS and macOS captures remain
outstanding. The catalog does **not** yet satisfy the complete latest-browser
coverage goal. The current three-sample captures do not estimate the distribution
of ECH GREASE payload lengths or establish raw-byte browser parity; see the
[dated comparison](../profiles/browsers/captures-2026-09-24/COMPARISON.md).

## Chrome 152 preview

The pinned tls-client definition includes GREASE at the start of the TLS signature
algorithm list and the `trust_anchors` extension. curl-impersonate 2.2.3 exposes
trust anchors but not the corresponding BoringSSL signature GREASE switch.
Scrapanium includes the supported extension and preserves shuffled anchor order.
Its test compares the complete normalized capture and explicitly asserts the
remaining GREASE difference, rather than excluding that difference from evidence.

The 28 IDs come from tls-client's documented Chrome 152.0.7977.64 Android capture,
Chrome Root Store 39. Root-store/platform variation still needs desktop captures.
The preview uses desktop request headers. It is an experimental mixed-source
profile and must not be represented as an exact desktop or Android browser.

## Custom TLS and HTTP/2 settings

`Config.fingerprint` accepts `Fingerprint{ciphers, curves, signature_algorithms,
extension_order, http2_settings, pseudo_header_order, cert_compression, grease,
permute_extensions, window_update}`. Empty strings and window 0 inherit the
named profile. Bend toggle values are 0 = inherit, 1 = disabled, 2 = enabled;
the C API uses -1/0/1. Use `Fingerprint.inherit()` for stock profiles.

The string formats follow curl-impersonate: colon-separated cipher/curve names,
dash-separated TLS extension IDs, semicolon-separated HTTP/2 `id:value`
settings, and pseudo-header order such as `masp`. Overrides create a custom
fingerprint; matching a named browser is no longer implied.

## Updating profiles

1. Inspect primary upstream sources, browser release data and captures. Record
   exact source commits and platform/build metadata in the catalog.
2. Update the backend lock/hash or explicit profile data. Never rename an older
   profile to imply newer wire behavior.
3. Run every catalog target against verified TLS, capture ClientHello structure,
   compare independent references and review any differences, then test HTTP/2
   settings, header order and WSS separately.
4. Preserve known gaps, archive raw measurements and only advance defaults when
   their evidence warrants it. Capture resumption and HTTP/3 before claiming them.

Existing regression fixtures can be deliberately refreshed with
`scripts/capture_profiles.py`. They record backend behavior, not real browsers.

Sources: [curl-impersonate 2.2.3](https://github.com/lexiforest/curl-impersonate/releases/tag/v2.2.3),
[tls-client Chrome 152](https://github.com/bogdanfinn/tls-client/blob/34718e1b514b446b95bc68dc4f096247e69c7939/profiles/internal_browser_profiles.go),
[Firefox 148](https://github.com/bogdanfinn/tls-client/blob/34718e1b514b446b95bc68dc4f096247e69c7939/profiles/contributed_browser_profiles.go),
[trust-anchor capture provenance](https://github.com/bogdanfinn/tls-client/blob/34718e1b514b446b95bc68dc4f096247e69c7939/profiles/extension_data.go).

Attribution and license acknowledgements: [THIRD_PARTY.md](../THIRD_PARTY.md).
