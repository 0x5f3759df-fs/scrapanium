# Third-party dependencies

Scrapanium's original code is MIT licensed. Downloaded dependencies are not
relicensed by this project. Dependencies are stored under ignored `.deps/`.

- Bend compiler/runtime: Apache-2.0; source and license in `.deps/bend`.
- Bun: MIT; distribution includes its own notices.
- README artwork uses Archivo, copyright The Archivo Project Authors, under the
  SIL Open Font License. Its notice is retained in
  [notices/Archivo.OFL.txt](notices/Archivo.OFL.txt). The rendered artwork embeds
  outlined lettering; the font is not a runtime dependency.
- curl-impersonate and curl: upstream MIT/curl licenses plus bundled libraries.
- BoringSSL, zlib, Brotli, zstd, c-ares, libidn2, nghttp2, nghttp3 and ngtcp2:
  consult every `LICENSE*` in `.deps/curl`. Keep these notices when distributing
  a binary or the shared transport. This release archive includes libidn2
  license alternatives and Unicode data notices; do not assume the complete
  dependency graph is covered by Scrapanium's MIT license.
- curl_cffi: MIT; used as a testing/benchmarking reference, not at runtime.
- Profile header data is adapted from curl-impersonate. Its MIT notice is retained
  in [notices/curl-impersonate.LICENSE](notices/curl-impersonate.LICENSE).
- Firefox 148 wire parameters and Chrome 152 trust-anchor data are derived from
  tls-client, copyright (c) 2023 Bogdan Finn. Its BSD-style license, including the
  advertising clause and upstream placeholder wording, is retained verbatim in
  [notices/tls-client.LICENSE](notices/tls-client.LICENSE). This product includes
  software developed by the &lt;organization&gt;. The upstream project is
  [bogdanfinn/tls-client](https://github.com/bogdanfinn/tls-client).

The Bend compiler emits its runtime into compiled programs. Distribution must
retain applicable upstream notices. Release packaging/license review remains
part of the production release checklist.
