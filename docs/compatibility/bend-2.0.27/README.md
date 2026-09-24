# Bend 2.0.27 compatibility check

## Scope and result

An isolated Scrapanium worktree based on application revision d234c88c1c3500223b838b90c34b784d1a041f1d passed the compatibility checks below using Bend 2.0.27, upstream tag v2.0.27 at commit 63bee70b55a71024d6bdcb49a745111bc54b114e, and the pinned stock curl-impersonate 2.2.3 backend. The tested candidate lock SHA-256 is a8e4134a63f0fa60fcd78d63fd9c500cfa0f42c0e6aa0ad4d444096d59d5b56e.

This is a Linux x86_64/glibc correctness check under Ubuntu 26.04 on WSL2. It is not a performance rerun. Existing README charts and historical WebSocket reports remain attributed to Bend 2.0.17 and their recorded source revisions.

## Runtime and ownership review

The Bend 2.0.27 IoWork layout remains hand, made, word, size, data, text, code, call, and pack, matching pinned 2.0.17. In both revisions, io_loop drives io_step; io_wait parks and resumes activations on the loop; io_work sends only its call function to a helper and packs the result on the loop. Scrapanium's WSS callbacks and socket-mutex owner therefore remain on the same loop thread. The opaque File handle contract and transferred byte-buffer ownership did not change.

The material Linux readiness change is from poll to select. Bend 2.0.27 builds read/write bitsets sized through the highest watched descriptor rather than using fixed-size fd_set macros. The WSS bridge supplies read, write, or timer-only waits. Six sanitizer cases exercised actual WSS client sockets at FDs 1104, 1105, and 1106. This covers those tested paths, not arbitrary descriptor limits or other operating systems. Compiler upgrades must recheck IoWork/effect ABI, callback thread affinity, readiness behavior, and opaque-handle/buffer ownership.

The 2.0.27 standalone executable's config-loading hardening is not inherited by Scrapanium's source compiler invocation: scripts/build.py invokes Bun with bend2/main.ts. This check makes no claim about that separate standalone path.

## Test evidence

- The full local pytest collection had 501 tests: 469 passed, 32 skipped, and none failed. All skips were optional browser-backend/source-build cases. This run happened before the isolated dependencies.json metadata changed from 2.0.17 to 2.0.27. At that time the worktree's .deps/bend symlink already targeted the clean 2.0.27 compiler clone, but pytest XML does not record a compiler command or binary hash. Treat it as full functional-suite evidence from that isolated environment, not as compiler-binding evidence. The pre-run 2.0.17 lock snapshot is retained alongside it.
- The phase and attribution sanitizer smokes each retained 24 rows: 16 negative controls and eight positive correctness cases. Their manifests independently record Bun, the clean compiler commit, compiler source hash, built client/source hashes, and post-run source checks. These are the direct compiler-provenance records.
- The one-thread and four-thread streaming sanitizer smokes each ran one repeat over six workloads with four corruption/reordering controls. Their manifests bind Scrapanium sources, dependency lock, backend, built Bend executable and Go peer. Their compiler field records Clang but not the Bend compiler commit, and the JSON binary hash list does not include generated C, so those manifests alone are not compiler provenance. A supplemental post-run SHA-256 for the generated-C file is recorded separately in provenance.json.
- The high-FD sanitizer regression passed six cases in 20.26 seconds: fragmented binary receive, async cancellation/timeout, and blocked-send cancellation/timeout, each with Bend thread counts one and four. A test-only gate held HTTP 101 until /proc confirmed the actual client socket. The receive case observed FD 1104; async and blocked-send observed FDs 1105 and 1106. Test/helper hashes are recorded in provenance.json.

The high-FD regression exercises io_wait after its Linux readiness implementation changed to dynamic select fdsets. It does not establish behavior on macOS, other libc implementations, non-Linux systems, or arbitrary descriptor limits. None of these compatibility checks is performance evidence.

## Reproduction

Use a fresh checkout/worktree with its own .deps directory when changing the compiler pin. Do not replace the main checkout's 2.0.17 compiler in place: bootstrap refuses to overwrite a different pinned Bend commit. Keep the old compiler tree for historical results.

From a fresh Linux checkout, bootstrap the matched environment first:

    python3 scripts/bootstrap.py --matched
    python3 scripts/build.py
    env -u BEND_SOURCE -u SCRAPANIUM_CURL_DIR -u BUN -u CC -u LD_LIBRARY_PATH -u LD_PRELOAD .deps/venv-matched/bin/pytest -q -ra --junitxml=logs/full-suite-after-native-build.xml
    env -u BEND_SOURCE -u SCRAPANIUM_CURL_DIR -u BUN -u CC -u LD_LIBRARY_PATH -u LD_PRELOAD .deps/venv-matched/bin/pytest -q tests/test_websocket_high_fd.py --junitxml=logs/high-fd-tests.xml

The full-suite argv is a retrospective invocation record; pytest XML does not contain its compiler-selection environment. The archived JUnit output was written to /home/baidu/scrapanium-experiments/bend2-027-compat/logs/full-suite-after-native-build.xml; the relative logs path above is for a new checkout. The high-FD cases skip with an explicit reason if Linux /proc or the existing RLIMIT_NOFILE is insufficient; they never raise the limit.

Equivalent correctness-only smoke reproduction commands are shown below. Use fresh output paths when repeating them:

    .deps/venv-matched/bin/python benchmarks/websocket_streaming.py --smoke --sanitize --runs 1 --threads 1 --output logs/stream-smoke-pin-027-1.json
    .deps/venv-matched/bin/python benchmarks/websocket_streaming.py --smoke --sanitize --runs 1 --threads 4 --no-build --output logs/stream-smoke-pin-027-4.json
    .deps/venv-matched/bin/python benchmarks/websocket_phase_diagnostic.py --smoke --sanitize --output-dir logs/phase-smoke-sanitized-02
    .deps/venv-matched/bin/python benchmarks/websocket_attribution.py --smoke --sanitize --output-dir logs/attribution-smoke-sanitized-02

Phase and attribution output directories must be fresh. The stream four-thread run intentionally reuses the client produced by the one-thread run. For the stream smokes, websocket_streaming.py calls scripts/build.py's build function on benchmarks/websocket_stream.bend. With BEND_SOURCE unset, the source compiler resolves through the worktree .deps/bend symlink; Bun is selected from the worktree .deps/bun directory. The first run built the client and generated C; the second used --no-build. Both stream manifests retained the same Bend and Go-peer binary hashes. The generated-C hash and a normalized build recipe reconstructed from scripts/build.py are detailed separately in provenance.json; neither was recorded in the stream JSON as a generated-C field or exact compiler argv.

## Evidence

evidence/ retains the full-suite and focused high-FD logs/XML, post-pin stream manifests/logs, and phase/attribution manifests/samples/logs. The full-suite XML is gzip-compressed; its uncompressed SHA-256 is recorded in provenance.json. The initial missing-native-library collection failure and the initial phase smoke rejected by old lock metadata are labeled as setup attempts.

The phase/attribution manifests name additional local files intentionally not packaged: compiled executables, cpu.prof, Go trace, corpus-body.bin, generated TLS keys/certificates, and dependency/toolchain trees. No Bend 2.0.27 performance data were collected.

provenance.json records compiler-source, generated-C, executable, backend, test/helper, and evidence hashes. SHA256SUMS covers the report, provenance, and all retained evidence.
