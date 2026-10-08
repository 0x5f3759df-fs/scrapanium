# Browser capture audit, 2026-10-07

The browser distribution lock pins Chrome Stable 155.0.8059.39 (revision 1697595) and Firefox 157.0.1 to official download metadata. Three fresh Linux samples per browser and mode are retained in the dated HTTP/2 reports. The optional navigation profiles are scoped to the observed initial TCP ClientHello structure and HTTP/2 navigation.

The WSS reports contain 15 samples: headed Chrome for Testing, Google Chrome and Firefox, plus headless Chrome for Testing and Firefox, with three samples in each cell. Offline validation reconstructs every ClientHello from bounded TLS records, checks the recorded byte length and SHA-256, reparses its fields, and confirms within-cell parser output is consistent. Every browser page reports an opened WebSocket, close code 1000 and a clean close. The local RFC 6455 peer observed the close payload 03e863617074757265, which is code 1000 followed by capture.

The status 101 field in a WSS record is the response configured by the loopback peer; the helper does not claim to retain the raw HTTP response. Browser onopen is the evidence that the upgrade succeeded. The peer did not negotiate a compression extension. Temporary CA, leaf certificate and key files were removed with each sample's temporary directory.

The capture-time source hashes in the HTTP/2 and WSS reports are preserved in [source-map.json](source-map.json) with byte-exact source snapshots under [source-snapshot/](source-snapshot/). The four unchanged Python helpers (scripts/capture_browsers.py, tests/fingerprint.py, tests/browser_lab.py, and tests/ws_lab.py) match source commit 7b52d7f1; tests/lab.py is also snapshotted because the HTTP/2 report pins it, although a post-capture import audit found it was not imported by the WSS helper. The dated WSS helper and release lock were introduced for this capture and are stored as exact dated-input snapshots. The WSS reports retain their original source hashes. The validator reads the snapshots rather than requiring mutable current helper files to stay unchanged.
A separate capture_browser_wss_v2.py now refuses existing output paths and requires the exact browser close result and peer-observed close payload. Its syntax, help path and overwrite refusal were checked; it did not produce or replace any saved sample.

Chrome's headed HTTP/2 captures show the 155 client-hint brands and Accept value including image/jxl; Chrome for Testing has its separately captured brand order and headless user agent. Firefox 157's observed navigation user agent contains rv:157.0. These values were added as dated profile headers. For three samples, extension 65037 (ECH GREASE) had these payload lengths:

| Capture | Samples, bytes |
| --- | --- |
| HTTP/2 headed Chrome for Testing | 218, 250, 186 |
| HTTP/2 headed Google Chrome | 218, 250, 250 |
| HTTP/2 headed Firefox | 282, 282, 282 |
| HTTP/2 headless Chrome for Testing | 282, 282, 250 |
| HTTP/2 headless Firefox | 282, 282, 282 |
| WSS headed Chrome for Testing | 282, 282, 186 |
| WSS headed Google Chrome | 186, 218, 250 |
| WSS headed Firefox | 282, 282, 282 |
| WSS headless Chrome for Testing | 186, 186, 186 |
| WSS headless Firefox | 282, 282, 282 |

These three observations per cell do not establish an ECH GREASE length distribution or raw-byte parity.

## Native WSS control

A separate native control was built from the same 7b52d7f1 source before the new profile data was added. Its build record is [native-wss-build-20261007.json](native-wss-build-20261007.json), and the captured ClientHello and decrypted Upgrade headers are in [native-wss-current.json](native-wss-current.json). The native library SHA-256 is 4cbc3c6e2523b981e7fe180b487d4dbb477e3608670f987588abfe226b4325f5; the loaded pinned curl-impersonate 2.2.3 DSO SHA-256 is c09acf80854192341a3b46f8c25ea051a570ee47290a76f3cfe880fb316ff7ab. The loaded file path was resolved from the process map, and the custom browser-control header was confirmed before compilation.
The updated navigation-profile native build is recorded in [native-profile-build-20261007.json](native-profile-build-20261007.json); its library SHA-256 is e055371a7bccecf1fd90093ea2213bd87cf3e65c87bd8c5f12db587771790637. The post-build audit in [native-profile-build-provenance-20261008.json](native-profile-build-provenance-20261008.json) pins the resolved Clang executable and all 134 translation-unit dependencies, including project and curl headers. This is a post-hoc input record, not a bit-reproducible link claim.

The native controls here are the older chrome154, chrome154_headless, and firefox156 profiles; no Chrome 155 or Firefox 157 native WSS capture was made. The chrome154 native control and a Chrome 155 WSS sample have equal detailed-parser fields, while their raw ClientHello lengths differ; this is not a byte-equality claim. Chrome 155 WSS advertises only http/1.1 in ALPN and omits ALPS extension 17613, while its HTTP/2 navigation captures advertise h2,http/1.1 and include 17613. Native Chrome WebSocket requests also include navigation headers (client hints, document Accept, navigation fetch fields and Upgrade-Insecure-Requests) that the browser WSS request does not send. The captured browser request instead includes Pragma and Cache-Control: no-cache and offers permessage-deflate; the loopback peer did not negotiate compression, which Scrapanium does not implement.

The native firefox156 control offers only http/1.1 in ALPN. Firefox 157's WSS ClientHello offers h2,http/1.1 and includes extensions 27 and 35 that this native capture lacks. The native Firefox WebSocket request also uses its navigation header set, while the browser WSS request has WebSocket-specific fetch metadata and offers permessage-deflate; the loopback peer did not negotiate compression. The server selected HTTP/1.1 in these captures. These differences are separate from the navigation profiles; no WebSocket parity is claimed for the new profiles.

The dated checksum manifest covers every regular file in this capture directory and the release lock, excluding the manifest itself. Verify it from the repository root with sha256sum -c profiles/browsers/captures-2026-10-07/SHA256SUMS-2026-10-07.txt.
