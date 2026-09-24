# Source-built curl memcpy pair: provenance and correctness gate

This archive records a correctness-only comparison of two source-built `curl-impersonate` 2.2.3 shared libraries. The baseline is a natural release-recipe build. The candidate reuses the same compiled curl/BoringSSL objects and link arguments, adding only the reviewed `memcpy_route.o`. The candidate routes the BoringSSL `SSL_read`/`SSL_peek` copy callsites through a hidden alias to `memcpy@GLIBC_2.14`. The pair preserves the GLIBC 2.17 ceiling and the same dynamic dependency set. This is a readiness gate, not throughput evidence.

## Frozen pair

| Item | SHA256 |
| --- | --- |
| Upstream source | `6e8f87760a4dd96771e96fc9d55440dcd8845243` (`v2.2.3`) |
| Baseline DSO | `19a3732717119c67002204579881379c0d89beb0ae080cce38a4cc52fd9bbd8b` |
| Candidate DSO | `c15ed19b2510ea89e94df68e64146612bda86733414706b367f186250ecd0621` |
| Route source | `79f1816530383e34c36df60d46bfd1b672a7f2635cf3cbfebafb6bf617e0aea1` |
| Route object | `a5fb141075277aa556c968c3aa88de0d73d7c93dff67874c43907067ed3f2835` |

The pinned release source is the clean upstream commit above. The baseline was built with the official Linux x86-64 release workflow, Zig 0.15.2, and target `x86_64-linux-gnu.2.17`. `backend/baseline-provenance.json` retains the complete configure argument vector, toolchain and dependency hashes, and build provenance. The exact shared-library link command is in `backend/baseline-link-command.txt`.

The initial configure attempt picked up host pkg-config metadata and retained a failed nested curl cache. Its cache is `build-history/failed-curl-CMakeCache.host-pkg-config.txt`; its failed configure transcript is included in `build-history/baseline-build.log.gz`. They are not the passing baseline. The exact outer recipe and compiler shims are copied under `build-history/recipe/`: `run-baseline.sh`, `continue-baseline.sh`, and the `cc`, `cxx`, and `ar` wrappers. These historical files contain absolute paths into the original experiment and Zig tool roots, so they document the commands and must be path-adjusted before reuse elsewhere. Recovery reran the generated curl configure invocation in a fresh nested build directory, clearing ambient include/library/pkg-config search variables and setting `PKG_CONFIG_LIBDIR` to the pinned dependency prefix. The exact recovery environment and full argv are in `build-history/curl-clean-reconfigure.json`, and its output is in `build-history/curl-clean-reconfigure.log`. The recovery record and hashes are also reflected in `backend/baseline-provenance.json`. This keeps the workaround visible and confines discovery to the pinned dependency metadata.

The candidate relink did not rebuild upstream objects or change the baseline prefix. The command and input map are preserved in `backend/candidate-link-command.txt`, `backend/candidate-link-command.json`, and `backend/baseline-candidate-object-map.json`: all 209 baseline link-input hashes match, and the only candidate addition is `memcpy_route.o`. ELF evidence records `SSL_read`/`SSL_peek` routing, the `memcpy@GLIBC_2.14` relocation, unchanged `DT_NEEDED`, and the GLIBC ceiling. The build artifacts themselves are omitted. The route source and the Zig link/runtime feasibility evidence are in the neighboring [link-probe package](../link-probe/README.md).

Only the candidate shared DSO was relinked. The candidate prefix's static curl archive and command-line executable remain baseline artifacts; neither was used as candidate code in these checks. Both isolated client worktrees use the same Scrapanium source revision, each with its own test build; runtime backend selection is through that worktree's private `.deps/curl` mapping.

## Correctness results

Both isolated source worktrees were at Scrapanium commit `411981c05167fa54065fa3d8757bac3c734b19f6`; each had its own separately built test binaries. The complete suite passed on both libraries: **461 passed, 32 skipped**. All skips were existing optional source-built browser-backend/source-build requirements. Both libraries also passed the streaming sanitizer checks at one and four threads, plus the phase and attribution sanitizer smoke checks. Positive phase/attribution rows passed exact payload checks; all 16 deliberately corrupted/swapped controls per smoke were detected. ASan/UBSan covered the Bend/native bridge smoke clients, and the attribution smoke also used its ASan-instrumented preload shim. The release backend dependencies and Python client were not sanitizer-instrumented. The candidate DSO passed 83,135 size/alignment cases and 64 guarded-edge cases. The gate links and hashes every retained log, manifest, and sample file.

The diagnostic runners require a private `.deps/curl` mapping in each isolated checkout. For the successful attribution runs, each test checkout had a private `.deps` directory, with `.deps/curl` pointing to its selected source-built prefix; the other pinned dependency entries pointed to the main checkout's read-only dependency tree. One initial attribution command passed `SCRAPANIUM_CURL_DIR` and was rejected before launching a client or peer; the retained note explains that its original tee log was overwritten and is a retrospective console record. Historical attribution manifests call their selected path the “pinned .deps/curl stock backend”; the retained `backend.path` and SHA identify the source-built baseline or candidate DSO, not the prebuilt release DSO.

The machine-readable [correctness gate](correctness-gate.json) is bound to the client-source revision and both DSO hashes. It contains correctness evidence only and is the prerequisite consumed by the uninstrumented comparison runner. No throughput result is included here.

## Recheck

From the repository root, regenerate the gate against the retained pair root:

```sh
python3 benchmarks/websocket_memcpy/evidence/backend-pair/audit_correctness.py \
  --pair-root /home/baidu/scrapanium-experiments/wss-memcpy-backend-pair-20260924
```

This audit reads retained evidence and hashes the external source-built DSOs and pair inputs; it does not build or run tests. Gate artifact paths are repository-root-relative, so invoke the comparison runner from the repository root as well. To check the packaged bytes, run from this directory:

```sh
sha256sum --quiet -c SHA256SUMS.txt
```

The pair root is machine-specific; a recreated pair must first match the frozen DSO and route-object hashes before its evidence can satisfy the gate. This archive excludes compiled executables, libraries, downloaded dependency bundles, TLS private keys, and performance samples.
