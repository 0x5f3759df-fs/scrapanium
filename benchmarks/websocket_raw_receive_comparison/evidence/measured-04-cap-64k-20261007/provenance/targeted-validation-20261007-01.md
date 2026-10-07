# Targeted validation record for the 64 KiB receive-cap trial

Correctness and sanitizer evidence only. No timing campaign has started.

## Pins

- Trial HEAD: c4927171753a3cdcb12986f06c598219bba6e5b4; branch codex/wss-recv-cap-64k-20261007.
- Trial native/websocket.inc.c SHA-256: 94cb08c07c1db119c43028a9b0c3682e214e205ab8550b3ba991879980cbc446.
- Test C SHA-256: 7356aa3d0fd46a614f913ee75fb49c4625e91c1f0b925e5406bb993c4304f4a2.
- Test wrapper SHA-256: fa2b2133a160c5a88a672f95f70beb62f7c4bff88ea5ea587d757034393162d3.
- Historical candidate native SHA-256: 0f3c4e2c2844975f4bfd63adb1ba0bfb901f6b97deff640c6a80498653ad82f8.
- Production baseline native SHA-256: 0bc6766607ecfad0594d4a2df85b8247da678f4ab1cc0c9fca09504307082fb5.
- Non-sanitized library built from trial source SHA-256: 571446ba542ec340b77f7b9bfaad6013ff6bd0d6c41cda333a69951c6fe75239.
- Stock DSO SHA-256: bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3.

## Validation sequence

1. The initial focused pytest collection returned 133 passed and one failure in the then-stale lifecycle harness. The raw stdout/stderr was not redirected and could not be recovered. `initial-focused-pytest-output.partial-20261007-01.txt` retains only the known outcome; it is explicitly not a full or verbatim log. Do not combine its count with the later lifecycle result.
2. A later single-test attempt using system Python without the matched-package PYTHONPATH failed before pytest collection with `No module named pytest`. Its complete one-line log is `operation-reuse-final-sanitized.log`, SHA-256 a91714972f6c29b2a7931c4b6edc23682e5e7b31ba7af21cb4793989584b8473.
3. The final expanded lifecycle test passed ASan/UBSan: `1 passed in 2.08s`. Log: `operation-reuse-final-sanitized-02.log`, SHA-256 bdc4a033a16622e249556c78c42b2f18fdbdb2bfa4c14d4977c44133effefef7. Sanitizer harness binary SHA-256 cd8936ed5b6885f742aa58466ee78e2f0d9bd436ac988d54c5c5aae8d17268d6.

   ```sh
   PYTHONPATH=/home/baidu/scrapanium-experiments/wss-raw-recv-probe-20260924/worktree/build/local-site-packages-matched-20261005 \
     CC=/usr/bin/clang \
     SCRAPANIUM_CURL_DIR=/mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl \
     LD_LIBRARY_PATH=/mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl \
     /usr/bin/python3.14 -m pytest -q tests/test_websocket_operation_reuse.py
   ```

4. The existing real WSS streaming sanitizer runner passed with Bend threads 1 and 4. Both runs cover six workloads x 32 messages x two clients (12 positive rows), plus corrupt and sequence-swap controls for both clients (four negative controls). Both reports validate full frame counts, payload byte totals, and TLS 1.3. The t4 invocation uses --no-build and confirms the t1 instrumented binary hash.
   - t1 report: stream-san-t1.json, SHA-256 7d377ff95f6484c3f7af1ca98ad1feabd68bcc10dd57e73f52ecc2814e97daf9; log SHA-256 a7e816ea9bc42f681a4247cc9cd85a9d7f15574618313f386a177cae631eed89.
   - t4 report: stream-san-t4.json, SHA-256 dfbffa3aaca7825dc68c98f03c81d72b2a268c1c5e17e266f2fd161de920a734; log SHA-256 2b9cf0ac54cae377bbd1b975954d56778bf5a7dacb9a724306c7af8e5c287e9d.
   - Shared Bend binary SHA-256: 893ee1ed535521547f197fcebd1798c9d6be9573ab44e0cd6debfe6dbbf1177d. Shared peer SHA-256: afd14fa369167ebb53598af27e0982d0f74bba339d17baa9dce5d2c34d8fb861.
   - Full output logs are retained in stream-san-t1.log and stream-san-t4.log.

The real stream checks use Bend 2.0.27 at commit 63bee70b55a71024d6bdcb49a745111bc54b114e, Bun 1.3.11, Clang 21.1.8, Python 3.14.4, and the pinned stock DSO. Reports pin the trial native source hash. These are correctness checks, not performance measurements. No broad suite was repeated.
