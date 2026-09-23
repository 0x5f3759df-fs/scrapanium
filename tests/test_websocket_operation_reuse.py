"""Sanitizer regression for per-socket operation-state ownership and reuse."""
import os
from pathlib import Path
import subprocess


def test_operation_state_reuse_lifecycle():
    root = Path(__file__).resolve().parents[1]
    prefix = Path(os.environ.get("SCRAPANIUM_CURL_DIR", root / ".deps/curl")).resolve()
    library = prefix / "lib" if (prefix / "lib/libcurl-impersonate.so").exists() else prefix
    binary = root / "build/ws-operation-reuse"
    binary.parent.mkdir(exist_ok=True)
    subprocess.run([os.environ.get("CC", "clang"), str(root / "tests/websocket_operation_reuse.c"),
        "-std=c11", "-O2", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I" + str(prefix / "include"), "-lpthread", "-lm", "-L" + str(library),
        "-Wl,-rpath," + str(library), "-lcurl-impersonate", "-o", str(binary)], check=True)
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
        env={**os.environ, "LD_LIBRARY_PATH": str(library),
             "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1", "UBSAN_OPTIONS": "halt_on_error=1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "operation reuse preserves parked state, ownership, busy gates, controls, and failure cleanup"
