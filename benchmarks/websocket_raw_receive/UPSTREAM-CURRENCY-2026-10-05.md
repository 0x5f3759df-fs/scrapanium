# Upstream currency audit (2026-10-05)

Checked against primary release pages and project documentation on 2026-10-05. This is a dated audit note; it does not rewrite the frozen captures or authorize changing their dependency pins.

## Bend

Bend 2.0.35 was the latest listed release on 2026-10-05, published 2026-10-03 at commit 79df8d9c40722ee9507a1e253f283b51025f9d6c. The retained experiment and repository dependency manifest use Bend 2.0.27 at 63bee70b55a71024d6bdcb49a745111bc54b114e. Keep this receive comparison pinned to 2.0.27; test 2.0.35 in a separate compiler compatibility campaign before considering any migration.

Sources: [Bend releases](https://github.com/bendlang/bend/releases), [2.0.35 commit patch](https://github.com/bendlang/bend/commit/79df8d9c40722ee9507a1e253f283b51025f9d6c.patch).

## curl_cffi

The latest listed curl_cffi release was the beta 0.16.4b1, published 2026-09-20 at commit 4e20fcd9adba3fc7bef29f8976d953640f001f7e. The latest stable release listed was 0.16.3, published 2026-09-02 at d7302948771801955bcef0d5288c137d7b74f49d. The Scrapanium matched-checkout pin remains 8cd226f24a06f81d66c42fbdb2a7c49305b1b7b7 (recorded as 0.16.4b1 in dependencies.json); preserve that exact comparison input unless new compatibility evidence calls for a deliberate change.

The project's current supported-target documentation lists chrome150 and firefox147 as its newest Chrome and Firefox impersonation targets. Those are profile labels, not current browser release versions.

Sources: [curl_cffi releases](https://github.com/lexiforest/curl_cffi/releases), [0.16.3 release](https://github.com/lexiforest/curl_cffi/releases/tag/v0.16.3), [supported impersonation targets](https://github.com/lexiforest/curl_cffi/blob/main/docs/impersonate/targets.rst).

## curl-impersonate

The latest release listed for lexiforest/curl-impersonate was v2.2.3, published 2026-09-16 at commit 6e8f87760a4dd96771e96fc9d55440dcd8845243, based on curl 8.22.0. This matches the transport version used in the retained evidence, whose loaded DSO hash is recorded in each capture. No transport pin change is indicated by this audit.

Sources: [curl-impersonate releases](https://github.com/lexiforest/curl-impersonate/releases), [v2.2.3 release](https://github.com/lexiforest/curl-impersonate/releases/tag/v2.2.3), [v2.2.3 profile table](https://github.com/lexiforest/curl-impersonate/blob/v2.2.3/README.md).

## Browser release context

Google's 2026-10-01 desktop Stable update listed Chrome 154.0.8037.97 for Linux and 154.0.8037.97/.98 for Windows and macOS. Firefox 157.0 was first offered to Release channel users on 2026-09-29. Therefore the evidence profiles chrome150 (adversarial capture) and chrome146 (standalone capability probe) are dated profile snapshots, not the newest stable browser versions on this audit date. The Firefox 156.0.1 capture referenced by earlier profile records is likewise historical relative to Firefox 157.0.

Sources: [Chrome Stable update, 2026-10-01](https://chromereleases.googleblog.com/2026/10/stable-channel-update-for-desktop.html), [Firefox 157.0 release notes](https://www.firefox.com/en-US/firefox/157.0/releasenotes/).

## Decision

Keep the historical WS/WSS evidence byte-preserved and retain its original source, compiler, transport, and profile identities. The upstream changes above are recorded for later review only. They do not update dependencies.json, the retained captures, or the planned same-compiler receive comparison.
