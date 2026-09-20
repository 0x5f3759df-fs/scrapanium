# Browser profile coverage

The versioned [catalog](../profiles/catalog.json) records **41 named profiles**:
39 upstream curl-impersonate targets plus Firefox 148 and a Chrome 152 preview.
Use `S.profiles()` to enumerate them. Unknown names fail; `chrome152` does not
silently select the preview. The default remains the pinned `chrome150`.

| Family | Newest included target | Evidence / limitation |
| --- | --- | --- |
| Chrome desktop | `chrome150` | Stock backend, curl_cffi ClientHello and HTTP/2 parity. |
| Chrome 152 preview | `chrome152_preview` | Chrome 150 plus 28 source-derived trust anchors and versioned headers. Known signature GREASE gap. |
| Chrome Android | `chrome131_android` | Newest Android-specific stock target in this backend. |
| Firefox | `firefox148` | Cipher updates over 147; normalized ClientHello matches independent Go tls-client 148. |
| Safari macOS | `safari2601` | Stock Safari 26.0.1; curl_cffi ClientHello parity. |
| Safari iOS | `safari260_ios` | Stock Safari 26.0; curl_cffi ClientHello parity. |
| Tor | `tor145` | Stock Tor 14.5; curl_cffi ClientHello parity. |

Older profiles and Edge/OkHttp targets remain available for reproducible clients.
The catalog is a transport inventory, not a list of currently released browsers.
**No profile has yet passed our own real-browser capture gate.** Reference-library
parity and a successful TLS connection do not prove indistinguishability from a
browser across platforms, resumption, HTTP/3, cookies or application behavior.

## Freshness gap, checked 2026-09-20

The browser releases have advanced beyond these reference libraries:
[Chrome 153](https://chromereleases.googleblog.com/2026/09/stable-channel-update-for-desktop_0808145027.html),
[Firefox 156](https://www.firefox.com/en-US/firefox/156.0/releasenotes/) and
[Safari 27](https://webkit.org/blog/18325/webkit-features-for-safari-27-0/) are
released. Their matching profiles are still required. The current catalog does
**not** satisfy the project's latest-browser coverage goal. Next work is actual
browser captures and backend changes, followed by reviewed regression evidence;
changing a User-Agent or copying an older profile name would not close this gap.

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
