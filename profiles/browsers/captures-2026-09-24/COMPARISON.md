# Current browser capture comparison

This is a separate current-release observation set. The 2026-09-20 capture files
and profile catalog remain unchanged. Each current browser/mode group has three
fresh-process, fresh-profile ClientHello captures and three local HTTP/2 captures.
The release lock, upstream metadata, executable hashes, raw records, and
verification checks are retained beside this report in this directory and
[`provenance.json`](provenance.json).

## Comparison method

ClientHello comparisons use the stored `normalized_clienthello` and
`detailed_clienthello` structures and require exact equality. The capture
normalization removes random/session/key-share bytes, SNI, ECH payload bytes,
GREASE values, extension permutation, and conditional padding extension 21;
it retains GREASE counts/vector positions, stable extension payloads, and key
share group/size information. Raw TLS records and parsed extension orders remain
in the capture JSON. Because some excluded data can change wire length, a
normalized match does not establish raw-byte equivalence.

HTTP/2 fingerprints use the same comparison as
`tests/test_browser_profiles.py`: decoded headers retain their order and values
except that the ephemeral `:authority` is replaced with `<origin>`. ACK frames
are excluded because their timing depends on scheduling. ALPN, TLS version,
SETTINGS, window updates, frame order/stream IDs/flags, non-HEADERS payloads,
and decoded header values remain compared. Raw HTTP/2 bytes are retained in each
sample.

## ClientHello results

All five comparable browser/mode groups have zero differences in both stored
normalized and detailed ClientHello structures between the 2026-09-20 and
2026-09-24 samples. GREASE counts also match. The raw wire observations include
these ClientHello lengths and ECH GREASE extension (type 65037) payload lengths:

| Browser and mode | ClientHello bytes, 2026-09-20 -> 2026-09-24 | ECH payload bytes, 2026-09-20 -> 2026-09-24 |
| --- | --- | --- |
| Chrome for Testing, headed | 1918, 1918, 1982 -> 2014, 2014, 2014 | 186, 186, 250 -> 282, 282, 282 |
| Google Chrome, headed | 1912, 1976, 1944 -> 1912, 1944, 1976 | 186, 250, 218 -> 186, 218, 250 |
| Firefox, headed | 1882, 1882, 1882 -> 1882, 1882, 1882 | 282, 282, 282 -> 282, 282, 282 |
| Chrome for Testing, headless | 1982, 1982, 2014 -> 1982, 1950, 1982 | 250, 250, 282 -> 250, 218, 250 |
| Firefox, headless | 1882, 1882, 1882 -> 1882, 1882, 1882 | 282, 282, 282 -> 282, 282, 282 |

Chrome's non-GREASE extension order varied across all three samples in both
capture dates (three distinct orders per group); Firefox used one order across
all three samples, unchanged between captures. The per-sample orders and raw
extension lengths are listed in [`comparison.json`](comparison.json). In
particular, the headed Chrome for Testing ECH payload lengths differ in these
three samples even though the normalized structures compare equal. These small
samples do not establish the distribution or cause of ECH payload sizing, so
the difference must remain visible in any current-profile fidelity decision.

Source inspection provides useful context without turning the three captures
into a distribution estimate. The official Chromium 153 and 154 DEPS files pin
BoringSSL revisions `defe5810ee8be430bcdeccf46a199bec0e93abdb` and
`ac39ea6853833c1f18fd23614091d11855e71752`. In the latter revision,
`setup_ech_grease` selects an inner length of 128, 160, 192, or 224 bytes; the
AEAD overhead and extension framing produce the observed 186, 218, 250, or 282
byte payload lengths. The corresponding source diff does not change that size
selection, so the sampled length changes fit the existing random choices and do
not establish a version-induced ECH behavior change. See the [153 DEPS](https://chromium.googlesource.com/chromium/src/+/refs/tags/153.0.8010.52/DEPS),
[154 DEPS](https://chromium.googlesource.com/chromium/src/+/refs/tags/154.0.8037.57/DEPS),
[BoringSSL revision diff](https://boringssl.googlesource.com/boringssl/+/defe5810ee8be430bcdeccf46a199bec0e93abdb..ac39ea6853833c1f18fd23614091d11855e71752/ssl/encrypted_client_hello.cc),
and [154 ECH GREASE source](https://boringssl.googlesource.com/boringssl/+/ac39ea6853833c1f18fd23614091d11855e71752/ssl/encrypted_client_hello.cc).

## HTTP/2 results

The normalized HTTP/2 frame sequence, SETTINGS, window updates, ALPN, and TLS
version are unchanged for all five groups. Firefox's decoded request headers are
also unchanged (including its observed `rv:156.0` user-agent string).

The only normalized HTTP/2 header-value differences are Chromium version/client
hints:

| Browser and mode | Changed decoded request headers |
| --- | --- |
| Chrome for Testing, headed | `sec-ch-ua`: `"Chromium";v="153", "Not_A Brand";v="8"` -> `"Not A(Brand";v="99", "Chromium";v="154"`; `user-agent` changes `Chrome/153.0.0.0` -> `Chrome/154.0.0.0` |
| Google Chrome, headed | `sec-ch-ua`: `"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"` -> `"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"`; `user-agent` changes `Chrome/153.0.0.0` -> `Chrome/154.0.0.0` |
| Chrome for Testing, headless | `sec-ch-ua`: `"Chromium";v="153", "Not_A Brand";v="8"` -> `"Not A(Brand";v="99", "Chromium";v="154"`; `user-agent` changes `HeadlessChrome/153.0.0.0` -> `HeadlessChrome/154.0.0.0` |
| Firefox, headed and headless | No normalized header-value differences |

For a current-version HTTP/2 identity, update the expected Chromium
`sec-ch-ua` and user-agent values as shown. The normalized TLS evidence does not
indicate a stable cipher, extension-set, or static-payload change. It does not
settle whether the observed ECH payload-size variation should be reproduced by
the native profile. Keep the 153 and Firefox 156.0 catalog evidence intact
until that decision is made separately; these captures do not update native
profile data.

## Release identity

The current binaries report Google Chrome and Chrome for Testing
`154.0.8037.57`, and Firefox `156.0.1`. Firefox's executable launcher hash is
unchanged from the earlier capture, while its `libxul.so` hash changed. The
release manifest and provenance record contain archive hashes and upstream
checksum evidence; raw upstream metadata is in [`upstream/`](upstream/). The
[official Google Chrome stable-channel notice](https://chromereleases.googleblog.com/2026/09/stable-channel-update-for-desktop_0856730748.html)
is retained as a URL citation; the package version and checksum come from the
official Debian package index.
