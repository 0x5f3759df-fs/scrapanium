# Memcpy guard correctness evidence

This archive records correctness-only checks for an isolated memcpy link probe. It contains no production curl backend and provides no performance evidence. The checked candidate is the tiny `libmemcpy-candidate.so` from link-probe run 04; these results do not validate curl-impersonate or either WebSocket client.

The harness resolves the requested symbol from the specified DSO, confirms its mapped DSO, and invokes it through a function pointer. It uses read-only source pages, checks destination canaries outside the copied range, and tests guard pages at both ends. It excludes overlapping ranges because overlap is undefined for `memcpy`.

The ordinary glibc reference passed 83,135 size/alignment cases and 64 guarded-edge cases. Six deliberate provider faults were rejected: a changed copied byte, wrong return pointer, write beyond the checked destination region, source write, read before the source guard, and write past the destination-end guard. Protected-page faults were SIGSEGV. The candidate export `sp_wss_memcpy_test` passed the same 83,135 size/alignment and 64 guarded-edge cases. This tests the exported wrapper body directly.

The separate run-04 ELF evidence checks that the ordinary caller entry jumps to the hidden alias and the alias reaches the versioned libc import. The callsite claim uses only the 10-byte function-symbol extent. The link-probe candidate DSO SHA256 is `837c7118dc0a58396ff5037e7800e9e18993654a919a87c98b3ec38a65fff225`; the external run-04 manifest SHA256 is `b371c3a035c7062ce3a49ca77cf2736e24f426f21d8789a5316d9ecc0266dd47`. The candidate DSO and generated binaries are not copied here. The self-test summary records harness/source hashes; `SHA256SUMS.txt` covers files in this archive.

A separate report-only run 05 used the same route object bytes (`a5fb141075277aa556c968c3aa88de0d73d7c93dff67874c43907067ed3f2835`) but a different compiler-runtime object hash and therefore a different whole DSO hash. Inspection found the `.text` section hashes identical across run 04 and run 05 for both the compiler-runtime object (`e2150622091c4e49bbfe6b33e559da87c958041b7534b9b77d31383b5ba96fa0`) and candidate DSO (`edc8e7466a43b5ba879ae63af874c46e7587d3a239a0a5101a51b04ee662e702`). The decoded line tables also match; the raw DWARF line-table difference is the embedded Zig cache directory (`/home/baidu/.cache/zig/b/825ec3800566796db027b14ff84b6dd4` versus `/home/baidu/.cache/zig/b/54ed969d922e70f43a0d7c05bb327c4f`). Exact build commands are retained in the external run-04 `commands.log` (SHA256 `a317d18115279feeb57b1a38825c5efe795a22298196f86c31002d5b67c338a2`) and run-05 `commands.log` (SHA256 `05c6ebcdc6914da08ead00fef0ca47cc143ad7a6534377af0e855942d7ce4ae8`); the differing `-femit-bin` output paths produce different Zig cache directory names. The run-05 manifest SHA256 is `77da286ad3b7be59a4ae28d2547886427ea4d8a7fbbf5a02e58d5c171db8da76`, and its source hashes were stable before and after build. Its corrected callsite excerpt is bounded to the function size. The guard harness independently passed against that exact DSO too; `candidate-run05/` retains its output. Run 04 and run 05 are distinct whole-file binaries and are not described as byte-identical.

## Reproduction

From the repository root, run the libc reference and fault-provider self-test into a fresh ignored directory:

```sh
.deps/venv-matched/bin/python benchmarks/websocket_memcpy/tests/run_memcpy_guard_controls.py \
  --cc /usr/bin/gcc \
  --output-dir build/wss-memcpy-guard-selftest-FRESH
```

After obtaining and hash-checking the exact run-04 candidate DSO, run the compiled driver against its exported test symbol:

```sh
build/wss-memcpy-guard-selftest-FRESH/memcpy_guard_test \
  /absolute/path/to/libmemcpy-candidate.so sp_wss_memcpy_test all
```

The retained local invocation used `/home/baidu/scrapanium-experiments/wss-memcpy-link-probe/run-20260924-04/libmemcpy-candidate.so`, checked that its SHA256 matched before execution, and stored the result in `candidate-run04/`. The driver was compiled with GCC 15.2.0 using `-fno-builtin`, `-fno-lto`, and `-fno-tree-loop-distribute-patterns`; its SHA256 is `61093c5f271241cf3faaa47e22b32672ed4ef7016f440a0f285dfe345d76ecb4`.

`selftest/` contains the summary and raw reference/fault/build logs, excluding generated executables and fault-provider DSOs. `candidate-run04/` and `candidate-run05/` contain separate direct-candidate summaries and stdout/stderr. No timings, backend build, private keys, dependency packages, or compiled binaries are included.
