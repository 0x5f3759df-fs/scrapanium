# Isolated memcpy link-probe evidence

This package preserves the small Zig 0.15.2 link probe and its failed attempts. It proves only that the tiny probe can route an ordinary copy call through a strong hidden alias to the explicitly versioned glibc import. It does not show that the same route occurs in the real curl-impersonate backend, and it includes no timing results.

The final text evidence is run 05. Its manifest SHA256 is `77da286ad3b7be59a4ae28d2547886427ea4d8a7fbbf5a02e58d5c171db8da76`; its recorded source hashes are stable before and after the build. The baseline and candidate object maps use the same caller object (`a7a19573baccc808b02c13120a71d1508fb6b9abeb2206e3e0b6a38eaab7b4ab`) and same compiler-runtime source/object, with only the route object added to the candidate (`a5fb141075277aa556c968c3aa88de0d73d7c93dff67874c43907067ed3f2835`). The candidate's recorded DSO SHA256 is `1208da01e9ba6b8564bd99c4e5d6b898df78acd70f690514145ae37eb8e7c7a3`; the baseline DSO SHA256 is `87c2bb384acfa42ea5634438e1e0a6110899b29653806c7aa9383aac67029c0d`. Binary files are intentionally omitted.

Run 05's bounded ELF evidence shows the baseline caller targeting its local compiler-rt `memcpy`, while the candidate caller's 10-byte function entry jumps to the hidden `memcpy` override at the same address as exported `sp_wss_memcpy_test`. That wrapper jumps to a PLT slot whose relocation is `memcpy@GLIBC_2.14`; the candidate's recorded GLIBC requirement is 2.14. The three retained run-05 runtime child statuses are all zero. The independent guard run against the exact candidate DSO is archived in [guard evidence](../guard/README.md).

The current parser source SHA256 is `61fb501c7b78a868a5f4723ce11a617db970372e229c69b78598a134ea52ecd7`; run 05 records `9c100302ed17fcab5444b0c2098e5613ddd109c01545cb98c83a999a3fc3fe7c`. The run-05 runner snapshot is not retained as a separate file, so this archive does not reconstruct it. Instead, `audit_saved_outputs.py` replays the current parser's symbol and function-size-bounded disassembly helpers over the saved run-05 readelf/objdump text. It verifies instruction addresses, bytes, operands, call targets, aliases, GLIBC versions, runtime statuses, and the guard-run DSO hash against the manifest; the saved and current header-line formatting differs. Its recorded output is `audit-result.json`. This is an offline audit only; it does not load or rebuild binaries.

The exact compiler/link commands are in `attempts/run-20260924-04/commands.log` (SHA256 `a317d18115279feeb57b1a38825c5efe795a22298196f86c31002d5b67c338a2`) and `attempts/run-20260924-05/commands.log` (SHA256 `05c6ebcdc6914da08ead00fef0ca47cc143ad7a6534377af0e855942d7ce4ae8`). Their compiler-runtime object and whole DSO hashes differ because the DWARF line table embeds a different Zig cache directory. Their compiler-runtime `.text` hashes are identical (`e2150622091c4e49bbfe6b33e559da87c958041b7534b9b77d31383b5ba96fa0`), as are candidate DSO `.text` hashes (`edc8e7466a43b5ba879ae63af874c46e7587d3a239a0a5101a51b04ee662e702`); decoded source-line tables match.

`attempt-history.md` describes runs 01–04. It preserves the explicit unsupported-`--wrap` error and distinguishes incomplete verifier attempts from the complete run-05 result. Where a failure transcript or earlier source snapshot was not retained, the record says so rather than recreating it. `SHA256SUMS.txt` hashes this package's files. The archive contains no `.so` or `.o` files, dependency bundles, private keys, or timing data.

To recheck the saved run-05 parser evidence without compiling or loading a DSO, run from the repository root:

```sh
python3 benchmarks/websocket_memcpy/evidence/link-probe/audit_saved_outputs.py
```
