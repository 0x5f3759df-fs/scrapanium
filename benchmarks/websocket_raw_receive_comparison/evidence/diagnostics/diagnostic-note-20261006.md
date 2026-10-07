# WSS receive failure diagnosis - 2026-10-06

**Diagnostic experiment only.** This is not a release, product change, or performance result. No speed claim is made.

The original campaign at `/home/baidu/scrapanium-experiments/wss-raw-recv-comparison-20261005/candidate-c492-wss-recv-measured-01-20261006` retained 296 attempt rows: 295 succeeded and one failed (`original-python-30b-c1-r003-candidate`, `RuntimeError: transport failed`). It produced no final comparison report. Its manifest SHA-256 is `74f31eaef8295e097bdb6f45310d1a1115c21e21e00d719c4515ff359209c3a7`; attempts SHA-256 is `57c6f93828db09192ac998254337799543900dbd26ad7a39c3b4e9e2ac2f12a6`.

## Retained diagnostics

- Natural reproduction: `/home/baidu/scrapanium-experiments/wss-raw-recv-comparison-20261005/diag-python-peer-candidate-20261006/`
- Delayed-gate control: `/home/baidu/scrapanium-experiments/wss-raw-recv-comparison-20261005/diag-python-peer-delay-control-20261006/`
- Both use the same reviewed diagnostic helper, SHA-256 `e2c601b3ec54236116aaac4d429ede4ce0d9619b33d54695183fed7edc849949`.

The natural run passed with 1,001 exact masked binary frames (including warmup), TLS 1.3 / AES-256-GCM, and the peer's applied 4-second socket timeout. The 31,375,208-byte DSO hash took 273.216 ms; readiness-to-gate was about 274 ms. The peer later saw EOF after all frames, not a timeout. Postchecks passed.

One approved control inserted a deliberate 6-second pause after DSO hashing. The peer timed out after 4.004632 seconds while waiting for the gated frame, then closed. It captured only the warmup frame. The client returned code 4; stdout was `ready\n` and stderr was `transport failed\n`. Postchecks passed. This demonstrates that the four-second peer timeout can produce the generic transport error under an intentional delay; it does not establish that this caused the original campaign failure. The original cause remains unknown.

## Pinned inputs

- Candidate source: `/home/baidu/scrapanium-experiments/wss-raw-recv-probe-20260924/worktree`, HEAD `c4927171753a3cdcb12986f06c598219bba6e5b4`; `native/websocket.inc.c` SHA-256 `0f3c4e2c2844975f4bfd63adb1ba0bfb901f6b97deff640c6a80498653ad82f8`.
- Baseline source: `/home/baidu/scrapanium-experiments/wss-raw-recv-comparison-20261005/baseline-c492717`, HEAD `c4927171753a3cdcb12986f06c598219bba6e5b4`; `native/websocket.inc.c` SHA-256 `0bc6766607ecfad0594d4a2df85b8247da678f4ab1cc0c9fca09504307082fb5`.
- Executed candidate binary: `/home/baidu/scrapanium-experiments/wss-raw-recv-comparison-20261005/candidate-c492-wss-recv-measured-01-20261006/build/candidate-roundtrip`, SHA-256 `c18e28a46f79dfc9628bf0b64042a9a8a11eecb151509c2056b950c2c3ed641e`.
- Bend compiler source: `/home/baidu/scrapanium-experiments/bend2-027-compat/compiler-2.0.27`, commit `63bee70b55a71024d6bdcb49a745111bc54b114e`; clang `/usr/lib/llvm-21/bin/clang`, SHA-256 `412bbe8c60571a1eb06f48fde89635033621caeb01a9b4ee76d46711bae8e932`.
- Python: `/usr/bin/python3.14`, SHA-256 `52e0a13e60a981d8c4b6478be2ba5176f69da07948a056bf49cf6f077e30cb41`.
- Mapped backend: `/mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl/libcurl-impersonate.so.4.8.0`, SHA-256 `bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3`.
