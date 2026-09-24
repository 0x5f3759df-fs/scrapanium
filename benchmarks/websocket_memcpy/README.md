# WSS memcpy feasibility experiment

This isolated experiment tests whether one ordinary, unknown-size `memcpy`
call can be redirected from Zig's compiler-rt implementation to glibc's
versioned `memcpy@GLIBC_2.14` without exceeding the x86_64 glibc 2.17 target.
It does not change Scrapanium's backend, dependency defaults, or performance
claims. There are no timing results.

`memcpy_probe_callsite.c` is compiled once with Zig 0.15.2 for both links. The
same Zig release's `lib/compiler_rt/memcpy.zig` is compiled into both shared
objects, giving the baseline a hidden local implementation. The candidate adds
only `memcpy_route.c`: it defines a strong hidden `memcpy` alias and exports
`sp_wss_memcpy_test` at the same function address. That body uses an ELF
symbol-version alias to request `memcpy@GLIBC_2.14` directly. This is the
explicitly selected ABI for the probe, not glibc's oldest `memcpy` symbol
version.

This is a forced-runtime link probe, not the upstream curl build recipe. An
initial tiny Zig C link without the compiler_rt object selected libc's
versioned `memcpy` directly, so the probe compiles the verified Zig release's
compiler_rt source into both links to establish a hidden local baseline. This
tests strong-symbol resolution and the wrapper's external versioned target;
the later backend pair must still show the route in the real release link.

The first gate is deliberately narrow. The retained probe must show the
baseline callsite targeting the hidden compiler-rt implementation, the
candidate callsite targeting its strong hidden alias, and that same alias
branching to the dynamic libc relocation for `memcpy@GLIBC_2.14`. The alias
and test export must have the same symbol address. Both links target
`x86_64-linux-gnu.2.17.0`; neither may require a GLIBC version above 2.17.
The candidate may add a 2.14 requirement if the tiny baseline probe has a lower
maximum; the full backend pair must separately preserve the pinned release's
2.17 ceiling. The bounded runtime checks run on the current host; they do not
prove execution on a 2.17 system. An independent fixture exercises the exported
body with guard pages and canaries before any full backend pair is attempted.

## Reproduce the link probe

The Zig binary is not installed system-wide. Download it into a private
persistent directory, verify the SHA-256 from Zig's official
[download index](https://ziglang.org/download/index.json), then extract it:

```sh
mkdir -p "$HOME/scrapanium-experiments/wss-memcpy-tools"
cd "$HOME/scrapanium-experiments/wss-memcpy-tools"
curl -fL -o zig-0.15.2.tar.xz \
  https://ziglang.org/download/0.15.2/zig-x86_64-linux-0.15.2.tar.xz
echo '02aa270f183da276e5b5920b1dac44a63f1a49e55050ebde3aecc9eb82f93239  zig-0.15.2.tar.xz' | sha256sum -c -
tar -xf zig-0.15.2.tar.xz
```

The archive SHA is `02aa270f183da276e5b5920b1dac44a63f1a49e55050ebde3aecc9eb82f93239`,
as published by Zig's official index for `zig-x86_64-linux-0.15.2.tar.xz`.
Run the probe into a new, unused persistent output directory; it refuses to
overwrite one:

```sh
cd /path/to/scrapanium
.deps/venv-matched/bin/python benchmarks/websocket_memcpy/link_probe.py \
  --zig "$HOME/scrapanium-experiments/wss-memcpy-tools/zig-x86_64-linux-0.15.2/zig" \
  --zig-sha256 2858dc89dbbfdd08cceda1b841e7fd0a793a1a67b49f150bc3d0d1de44ed7f51 \
  --zig-archive "$HOME/scrapanium-experiments/wss-memcpy-tools/zig-0.15.2.tar.xz" \
  --output-dir "$HOME/scrapanium-experiments/wss-memcpy-link-probe/run-reproduction-01"
```

This initial probe is separate from the pinned curl-impersonate source build.
A successful link-resolution result is necessary, not sufficient, to proceed
to a same-source backend pair. The full pair must keep all upstream sources,
dependencies, Zig target/options, and Scrapanium client/native source fixed,
then pass independent call-path and correctness checks. It must not be used for
throughput claims without a later separately reviewed experiment.

## Retained evidence

The initial Zig symbol-routing probe is archived in [link-probe evidence](evidence/link-probe/README.md); bounded runtime/canary checks are in [guard evidence](evidence/guard/README.md). These establish probe correctness and symbol routing only, not a curl backend change or performance result.
