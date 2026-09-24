# Memcpy link/correctness fixture

memcpy_guard_test.c receives a DSO path and exported test symbol, resolves
that symbol with dlsym, and confirms dladdr places the function in the requested
DSO. Calls go through the resolved function pointer; the driver is compiled
with -fno-builtin, -fno-lto, and loop-to-memcpy transformation disabled so the
test itself cannot substitute a compiler builtin.

The fixture checks valid zero-length calls; small lengths with every independent
source/destination residue from 0 through 63; representative residues
0, 1, 7, 8, 15, 16, 31, 32, and 63 around 1 KiB, 16 KiB, 64 KiB, 128 KiB,
and 1 MiB boundaries; exact payload and return-pointer behavior; read-only
source pages; canaries across the whole accessible destination region; and
guard pages at source/destination starts and ends. It does not test overlapping
ranges, whose behavior is undefined for memcpy.

Run the correctness-only fixture self-test into a fresh ignored output
directory:

    .deps/venv-matched/bin/python benchmarks/websocket_memcpy/tests/run_memcpy_guard_controls.py --cc /usr/bin/gcc --output-dir build/wss-memcpy-guard-selftest-FRESH

That self-test first exercises ordinary glibc memcpy, then checks the harness
rejects six deliberate faults: changed output byte, wrong return pointer,
write 128 bytes beyond the requested length, source write, read before a
guarded source start, and write after a guarded destination end. Protected-page
controls must terminate specifically with SIGSEGV or SIGBUS. The output
records logs, compiler and fixture hashes, resolved libc path/hash, and
before/after source hashes; it contains no timing data.

For a completed candidate DSO, run the same compiled driver directly against
the exported alias, after verifying its path and hash against the frozen link
manifest:

    build/wss-memcpy-guard-selftest-FRESH/memcpy_guard_test /absolute/path/to/libmemcpy-candidate.so sp_wss_memcpy_test all

This proves functional behavior of the exported body in that DSO. The separate
ELF review must still prove the ordinary candidate copy callsite reaches the
same alias address and that the alias's PLT/GOT relocation targets the
versioned memcpy@GLIBC_2.14 import. The DSO may retain unrelated local-hidden
compiler-rt memcpy code; its presence alone does not establish recursion or
disqualify the narrowly routed call.