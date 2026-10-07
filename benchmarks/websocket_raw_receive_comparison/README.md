# WSS receive-path comparison

This is a predeclared comparison harness, not a result. Do not run it until the candidate protocol review, network/full test suite, and compiler attribution checks have passed. It never changes the retained baseline reports or the frozen benchmarks/websocket_raw_receive/ evidence package.

The comparison has three clients:

- baseline: a clean checkout of Scrapanium commit c4927171753a3cdcb12986f06c598219bba6e5b4, built with Bend 2.0.27.
- candidate: the receive-parser source under review, built with that same Bend checkout, C compiler, build flags, and backend.
- curl_cffi: the matched curl_cffi checkout 8cd226f24a06f81d66c42fbdb2a7c49305b1b7b7 (0.16.4b1), using the same stock libcurl-impersonate.so DSO.

The runner first reserves a fresh output directory and fsyncs status.json with the command, selected runtime paths, and zero build/attempt flags before any external preflight command. A preflight failure updates that status; smoke mode records sanitized failure facts without a raw traceback or client timing lines. Binding-inspection failures also retain child stdout and stderr, with smoke redaction applied. The runner then builds new clients and peers under that directory and keeps each build command, stdout, and stderr in build-logs/. It writes a frozen manifest.json after the builds and before the first control or sample. Each client attempt is marked successful only after its full-message checker, exact DSO mapping, and linked peer/TLS checks pass; errors are appended to attempts.jsonl with fsync and stop the run. Use a new output directory for every invocation; it intentionally has no resume or overwrite mode. The exact benchmark checker/workload files must be byte-identical between the baseline and candidate checkouts. The manifest records both repositories’ commit, tracked-diff hash, status, source hashes, binary hashes, toolchain, package files, DSO path/hash, run seed, and order. The runner rechecks the relevant source, binary, toolchain, compiler checkout, installed binding, runner, and plan hashes after samples.

If a round-trip client sample raises, the runner retains its buffered monotonic readiness, DSO map/hash, gate and exit stages, client return code and separated output, plus available peer frame/TLS/socket-timeout/close observations. Readiness instrumentation wraps the existing line read, preserves its decoded value and file descriptor, and reads only the first stdout line bytewise so later timing output stays available to the original `communicate()` call. This adds a few startup syscalls and stage clock reads before the timed interval. Peer frame observation also adds a clock read per read. No file writes, fsyncs, or payload hashing occur during the sample. Failure-only cleanup waits briefly for a child to exit before killing it if necessary, then uses the original `communicate()` path to collect remaining output. These diagnostics can perturb timing slightly and do not establish the cause of the earlier 296-row failure.

## Predeclared workload and checks

The streaming run keeps the original six workload definitions and checker code unchanged:

| Server write grouping | Message size | Count |
| --- | ---: | ---: |
| 1 frame per TLS write | 30 B | 65,536 |
| 1 frame per TLS write | 1 KiB | 16,384 |
| 1 frame per TLS write | 64 KiB | 1,024 |
| 64 frames per TLS write | 30 B | 65,536 |
| 64 frames per TLS write | 1 KiB | 16,384 |
| 64 frames per TLS write | 64 KiB | 1,024 |

For each stream sample, all clients use one verified WSS connection and the same unique, ordered corpus: an eight-digit sequence prefix plus the existing deterministic body bytes. Corpus construction, TLS upgrade, and warmup are outside the timed region. The timed interval includes the start signal and receiving every complete message in order. Clients compare full payload bytes and binary kind and release each received/expected buffer inside timing, including the last message. The Go peer independently verifies warmup/start counts, frame count, payload-byte count, and canonical frame-byte totals for the identical server batching.

Each of the three variants is measured in every one of the six baseline/candidate/curl_cffi execution-order permutations once per six repeats for each workload. The six stream workloads rotate through starting positions across each six-repeat block. The default is 12 repeats per workload and variant, with a fixed seed. Every performance sample retains its elapsed time, throughput, setup/CPU/RSS data from the existing helper, DSO mapping, run ID, and peer observation.

The curl_cffi checker’s kind assertion tests the CurlWsFlag.BINARY bit in libcurl’s aggregate flags; it does not interpret that value as the literal wire opcode. Its received payload equality and sequence checks cover the full returned message. Peer-side frame accounting supplies wire-frame evidence.

The unchanged seven-workload round-trip matrix also runs with baseline, candidate, and matched curl_cffi as a regression check. Each client verifies complete binary payloads and kinds; the Python or Go peer confirms the expected frame and payload totals. Round-trip evidence prevents an inbound-only improvement from being presented as broad WebSocket superiority.

The Go peer records negotiated TLS version and cipher for each stream attempt and Go round-trip connection; those records must report TLS 1.3 (version 772) with cipher ID 4865. The runner also records the Python round-trip peer's negotiated version, cipher name, protocol and bit strength per connection. Its TLS identity must stay constant across baseline, candidate and curl_cffi; the Python peer is allowed to negotiate a different cipher from the Go peer.

## Decision rules

The working target for a “huge” receive-path lead is a candidate/curl_cffi paired throughput-ratio 95% bootstrap interval whose lower bound is at least 2.0 on both 64 KiB streaming workloads. All six stream workloads report candidate/baseline, baseline/curl_cffi, and candidate/curl_cffi paired ratios with full samples and intervals, whatever the result. Round-trip candidate/baseline ratios are reported for all seven workloads.

The working non-regression guard is a candidate/baseline paired throughput-ratio 95% interval lower bound of 0.95 or higher on every stream workload and every round-trip workload. A low count or an interval that crosses the guard is inconclusive; it is not a pass. These are predeclared decision aids, not promises about outcome or claims beyond this loopback Linux/WSL setup. Report durations and uncertainty and avoid claims based on close point estimates.

No sample may be silently retried, removed, or cherry-picked. Any failed protocol/checker/peer/DSO check stops the run. A failed or partial run remains preserved under its unique output directory and is not a performance result.

For a WSL run using the local Linux package snapshot, set PYTHONPATH to a byte-for-byte copy of the matched Python environment's site-packages; the copy must match the Python ABI used by the candidate. The harness and curl_cffi child clients inherit that path, and the runner verifies that curl_cffi/__init__.py, curl.py, requests/websockets.py and the native wrapper resolve from the local copy and match the pinned checkout. The harness Python must provide `cryptography` for the certificate helper. With a normal provisioned local matched venv, use a Python 3.14 interpreter with that helper dependency and leave `PYTHONPATH` unset. In either case the manifest records inherited PYTHONPATH and LD_LIBRARY_PATH, the selected build variables, the child interpreter and its sys.path.

## Reproduction

Prepare a clean detached baseline at the pinned commit, the candidate checkout, a clean matched curl_cffi checkout, the pinned Bend 2.0.27 checkout and Bun executable, and the already reviewed stock DSO prefix. Run the harness with Python 3.14 and `cryptography` available for the existing certificate helper. The clients themselves are launched through the candidate’s `.deps/venv-matched/bin/python`. In the current WSL setup, `/usr/bin/python3.14` uses the local CPython 3.14 package snapshot through `PYTHONPATH`; a normal provisioned matched environment can leave `PYTHONPATH` unset.

After the candidate review and all correctness gates are complete, the exact invocation shape is:

    CANDIDATE=/path/to/candidate
    HARNESS_PYTHON=/path/to/python3.14-with-cryptography
    # Optional for WSL runs using a local CPython 3.14 package snapshot.
    export PYTHONPATH=/path/to/local/python3.14/site-packages
    "$HARNESS_PYTHON" \
      "$CANDIDATE/benchmarks/websocket_raw_receive_comparison/run_comparison.py" \
      --baseline /path/to/clean-c492-checkout \
      --candidate "$CANDIDATE" \
      --curl-cffi-checkout /path/to/clean-matched-curl_cffi-checkout \
      --bend-source /path/to/clean-bend-2.0.27-checkout \
      --bun /path/to/pinned/bun \
      --curl-prefix /path/to/reviewed/curl-impersonate-prefix \
      --runs 12 \
      --seed 20261005 \
      --output /path/to/new-unique-run-directory

The runs argument must be at least six and divisible by six. The seven round-trip workloads are mandatory in measured mode so the report cannot imply a broad WebSocket lead from receive-only data. For a reduced-count correctness check, add --smoke (optionally --runs N); this runs 32 messages per stream workload and 20 round trips per round-trip workload, writes no timing fields, and makes no performance or acceptance claims. The temporary TLS key and certificate are created at run time and deleted with the temporary directory; they are not copied into the evidence package. Retain the complete output directory for review before making any performance claim.

## Harness checks

Run the small, non-performance tests before a comparison:

    cd "$CANDIDATE/benchmarks/websocket_raw_receive_comparison"
    python -m unittest -v test_run_comparison.py

These verify six-order balancing, smoke result redaction and durable error rows, including retention of client/peer evidence when a post-client validation fails.
