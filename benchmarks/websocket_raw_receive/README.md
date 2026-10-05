# WebSocket receive evidence at c492717

The adversarial corpus exercises Scrapanium's public native WebSocket receive API at commit c4927171753a3cdcb12986f06c598219bba6e5b4 and records a baseline parser gap. The separate WSS capability probe is an independent C client of libcurl; it does not call Scrapanium's native API. The probe shows that the tested curl easy handle can receive raw inbound bytes in connect-only mode while retaining the existing masked send path. Neither capture measures performance or tests the Bend application API.

## Adversarial WS/WSS baseline

The frozen harness sends 18 cases over plain WS and verified TLS WSS: 36 independent connections total. It used the chrome150 profile, a 256-byte response-body limit, and stock curl-impersonate 2.2.3 / libcurl 8.22.0. The recorded backend DSO SHA-256 is bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3.

Results were 32 passes and 4 failures. On each transport, the implementation accepted a 125-byte payload encoded with the 126 length marker and a 126-byte payload encoded with the 127 marker. It returned each as a successful binary message and accepted a follow-up client binary send that the peer recorded as must-not-send. The remaining malformed/truncated cases were rejected, and the valid canonical control/data case passed. The four failures are two cases repeated over WS and WSS, not four distinct malformed encodings.

RFC 6455 section 5.2 assigns marker 126 to a following 16-bit length and marker 127 to a following 64-bit length; it also requires the minimal number of bytes to encode the length. In particular, the 126 form must represent at least 126 bytes and the 127 form at least 65,536 bytes. Thus the two accepted cases above are non-minimal encodings. See [RFC 6455 section 5.2](https://www.rfc-editor.org/rfc/rfc6455#section-5.2).

This is a narrow parser baseline, not a general conformance result, sanitizer or fuzz evidence, a Bend client test, or a performance result. The captured run exits nonzero because those four assertions fail. The full summary and all 36 rows are retained in [results.json](evidence/adversarial/results.json) and [results.jsonl](evidence/adversarial/results.jsonl).

## Raw receive capability probe

The separate 2026-09-24 probe used curl_easy_recv for inbound bytes and the existing curl_ws_send API for masked outbound frames, with CURLOPT_CONNECT_ONLY=2, raw WebSocket mode disabled, and automatic PONG disabled. libcurl documents that connect-only mode 2 completes the WebSocket upgrade before handing control to the application; curl_easy_recv reads raw connection data and may return CURLE_AGAIN, and curl_ws_send sends WebSocket frames. See the [connect-only option](https://curl.se/libcurl/c/CURLOPT_CONNECT_ONLY.html), [curl_easy_recv](https://curl.se/libcurl/c/curl_easy_recv.html), [WebSocket options](https://curl.se/libcurl/c/CURLOPT_WS_OPTIONS.html), and [curl_ws_send](https://curl.se/libcurl/c/curl_ws_send.html).

The probe verified TLS peer and hostname certificates, observed an HTTP 101 response coalesced with the first frame octet, exercised a gated empty read, fragmentation with an interleaved PING/PONG, a 64 KiB binary message, and close handling. The peer negotiated TLS 1.3 with TLS_AES_256_GCM_SHA384. The adjacent-byte check covers the immediate post-upgrade receive scenario described in [curl issue 22107](https://github.com/curl/curl/issues/22107). That report concerns CURLWS_RAW_MODE; this probe has raw mode disabled, so its result applies only to the recorded configuration.

This establishes capability for the tested standalone curl handle and inputs. It does not establish that the application parser is correct, that the change is integrated in Bend, or that it is faster. See the retained [manifest](evidence/capability-01/manifest.json), logs, and peer record.

## Provenance and retained files

The source revision for both captures is c4927171753a3cdcb12986f06c598219bba6e5b4. The adversarial summary records the native source hashes: native/scrapanium.c d6dbad0e7671596b1c918f0c7f66936e396ecd3cc7da95e2786ac089796675ae and native/websocket.inc.c 0bc6766607ecfad0594d4a2df85b8247da678f4ab1cc0c9fca09504307082fb5. The harness and probe-source hashes are covered by [SHA256SUMS](SHA256SUMS).

The public package retains source inputs and text/JSON evidence only. It does not include the generated bridge or probe executable, Python bytecode, private keys, or temporary certificates. The capability manifest records private_key_retained: false. Its runner creates the probe executable and logs in the selected output directory, but creates the certificate and key under a system temporary directory. The key is unlinked before the client starts, and the temporary directory (including the certificate) is removed after the probe. The retained manifest records only the public certificate hash. The capture files include absolute paths from the original machine layout; those paths are historical metadata, not current reproduction paths. The recorded DSO hash, source revision, source hashes, and run results identify the experiment across that move.

## Reproduction

The evidence package was added after commit c492717, so it is absent from that historical checkout. Start from a published revision that contains this directory, copy the package to a fresh temporary directory, then restore it after checking out the frozen source revision. Use a fresh Linux x86_64 or WSL2 checkout. Install prerequisites and bootstrap the pinned compiler and stock curl-impersonate backend as described in [Getting Started](../../docs/GETTING_STARTED.md). Keep this checkout's .deps isolated.

    git clone https://github.com/0x5f3759df-fs/scrapanium.git scrapanium-c492717
    cd scrapanium-c492717
    stash_dir="$(mktemp -d /tmp/scrapanium-websocket-receive.XXXXXX)"
    cp -a benchmarks/websocket_raw_receive "$stash_dir/"
    git checkout c4927171753a3cdcb12986f06c598219bba6e5b4
    mkdir -p benchmarks
    cp -a "$stash_dir/websocket_raw_receive" benchmarks/websocket_raw_receive
    python3 scripts/bootstrap.py
    .deps/venv/bin/python -c 'import sys; print(sys.executable)'
    sha256sum .deps/curl/libcurl-impersonate.so.4.8.0
    mkdir -p build/wss-adversarial
    SCRAPANIUM_CURL_DIR="$PWD/.deps/curl" .deps/venv/bin/python scripts/build.py -o build/wss-adversarial/libscrapanium.so
    cp benchmarks/websocket_raw_receive/run_adversarial_ws.py build/wss-adversarial/
    .deps/venv/bin/python build/wss-adversarial/run_adversarial_ws.py --output build/wss-adversarial/reproduction.json

Confirm that the DSO hash printed by sha256sum matches the value above before interpreting a reproduction. Use the bootstrapped virtual environment because the harness imports the project's lab.certificate helper. The harness writes a new summary and per-attempt JSONL alongside the requested output and refuses to overwrite an existing output. Fresh certificate paths and generated material make the resulting JSON differ byte-for-byte from the historical capture; compare case outcomes and recorded source/backend hashes. Do not copy generated private keys or certificates into this evidence directory.

To rerun the standalone capability probe from the restored package, choose a new output directory and use the same virtual environment plus clang:

    .deps/venv/bin/python benchmarks/websocket_raw_receive/raw_recv_probe.py \
      --backend .deps/curl \
      --output-dir build/wss-raw-recv-capability/reproduction-01 \
      --cc clang

The probe writes its executable and logs to that output directory. It creates TLS certificate material in a system temporary directory, unlinks the private key before the child client starts, and removes the temporary directory after the probe. Preserve the original evidence/ files; store any fresh run separately.
