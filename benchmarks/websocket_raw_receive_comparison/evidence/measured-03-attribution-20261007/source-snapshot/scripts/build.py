#!/usr/bin/env python3
"""Build native library or a Bend entry point using the pinned compiler."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def compiler():
    checkout = Path(os.environ.get("BEND_SOURCE", ROOT / ".deps/bend"))
    if not (checkout / "bend2/main.ts").exists():
        checkout = ROOT.parent / ".scrapanium-research/bend"
    bun = os.environ.get("BUN")
    if not bun:
        candidates = list((ROOT / ".deps/bun/node_modules/@oven").glob("bun-linux-x64-baseline/bin/bun"))
        bun = str(candidates[0]) if candidates else "bun"
    return [bun, str(checkout / "bend2/main.ts")]

def build(entry=None, output=None, sanitize=False):
    (ROOT / "build").mkdir(exist_ok=True)
    curl = Path(os.environ.get("SCRAPANIUM_CURL_DIR", ROOT / ".deps/curl")).resolve()
    curl_lib = curl / "lib" if (curl / "lib/libcurl-impersonate.so").exists() else curl
    # O2 avoids an LLVM 21 preserve_none + ASan register-allocation failure at O1.
    flags = ["-std=c11", "-O2" if sanitize else "-O3", "-g", "-I" + str(curl / "include"),
             "-I" + str(ROOT / "native"), "-lpthread", "-lm", "-L" + str(curl_lib),
             "-Wl,-rpath," + str(curl_lib), "-lcurl-impersonate"]
    if sanitize:
        flags += ["-fsanitize=address,undefined"]
        # Retain frame pointers for the standalone native library diagnostics.
        if not entry:
            flags += ["-fno-omit-frame-pointer"]
    cc = os.environ.get("CC", "clang")
    output = Path(output or ROOT / "build/libscrapanium.so").resolve()
    if entry:
        generated = output.with_suffix(".generated.c")
        subprocess.run(compiler() + [str(Path(entry).resolve()), "-o", str(generated)], check=True)
        cmd = [cc, str(generated), str(ROOT / "native/scrapanium.c"), *flags, "-o", str(output)]
    else:
        cmd = [cc, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
               str(ROOT / "native/scrapanium.c"), *flags, "-o", str(output)]
    subprocess.run(cmd, check=True)
    return output

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("entry", nargs="?")
    p.add_argument("-o", "--output")
    p.add_argument("--sanitize", action="store_true")
    args = p.parse_args()
    print(build(args.entry, args.output, args.sanitize))
