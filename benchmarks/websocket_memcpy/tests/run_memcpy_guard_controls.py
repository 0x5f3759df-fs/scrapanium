#!/usr/bin/env python3
"""Correctness-only self-tests for the memcpy guard/canary harness.

Builds tiny test DSOs whose only differences are deliberate faults, then checks
that the harness accepts libc memcpy and detects each fault mode. It does not
collect performance data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        preexec_fn=_disable_core_dump if os.name == "posix" else None,
    )


def _disable_core_dump() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def write_result(path: Path, result: subprocess.CompletedProcess[str]) -> None:
    path.write_text(
        "returncode=" + str(result.returncode) + "\n"
        + "stdout:\n" + result.stdout
        + "stderr:\n" + result.stderr,
        encoding="utf-8",
        newline="\n",
    )

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    tests_dir = Path(__file__).resolve().parent
    harness = tests_dir / "memcpy_guard_test.c"
    provider = tests_dir / "memcpy_fault_provider.c"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    inputs_before = {name: sha256(path) for name, path in (
        (harness.name, harness), (provider.name, provider),
        (Path(__file__).name, Path(__file__).resolve()),
    )}
    compiler = run([args.cc, "--version"], cwd=output)
    if compiler.returncode:
        raise RuntimeError("compiler --version failed: " + compiler.stderr)
    driver = output / "memcpy_guard_test"
    driver_command = [
        args.cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        "-fno-builtin", "-fno-lto", "-fno-tree-loop-distribute-patterns",
        str(harness), "-ldl", "-o", str(driver),
    ]
    driver_build = run(driver_command, cwd=output)
    write_result(output / "build-driver.log", driver_build)
    if driver_build.returncode:
        raise RuntimeError("harness compile failed; see build-driver.log")

    summary: dict[str, object] = {
        "schema": 1,
        "kind": "correctness-only harness self-test",
        "timing_collected": False,
        "compiler_command": driver_command,
        "compiler_version": compiler.stdout,
        "inputs": {
            harness.name: sha256(harness),
            provider.name: sha256(provider),
        },
        "driver_sha256": sha256(driver),
        "cases": [],
    }

    libc_run = run([str(driver), "libc.so.6", "memcpy", "all"],
                   cwd=output, timeout=180)
    write_result(output / "reference-libc.log", libc_run)
    if libc_run.returncode != 0 or "PASS:" not in libc_run.stdout:
        raise RuntimeError("ordinary libc memcpy reference did not pass")

    cases = [
        (1, "regular", "copied bytes differ"),
        (2, "regular", "copy did not return destination"),
        (3, "regular", "destination canary changed outside requested range"),
        (4, "regular", None),
        (5, "edge-src-start", None),
        (6, "edge-dst-end", None),
    ]
    for kind, mode, expected_text in cases:
        library = output / f"fault-{kind}.so"
        build_command = [
            args.cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-fPIC", "-shared", "-fno-builtin", "-fno-lto",
            f"-DFAULT_KIND={kind}", str(provider),
            "-Wl,-z,now", "-Wl,-z,relro", "-o", str(library),
        ]
        built = run(build_command, cwd=output)
        write_result(output / f"build-fault-{kind}.log", built)
        if built.returncode:
            raise RuntimeError(f"fault provider {kind} compile failed")
        result = run([str(driver), str(library), "sp_wss_memcpy_test", mode],
                     cwd=output, timeout=60)
        write_result(output / f"fault-{kind}.log", result)
        if result.returncode == 0:
            raise RuntimeError(f"fault provider {kind} unexpectedly passed")
        if f"fault_provider_kind={kind}" not in result.stderr:
            raise RuntimeError(f"fault provider {kind} did not execute its code")
        if "resolved_dso=" not in result.stdout:
            raise RuntimeError(f"fault provider {kind} was not mapped from its DSO")
        if expected_text and expected_text not in result.stderr:
            raise RuntimeError(
                f"fault provider {kind} failed for an unexpected reason; "
                f"wanted {expected_text!r}"
            )
        if kind in (4, 5, 6) and result.returncode not in (
                -signal.SIGSEGV, -signal.SIGBUS
        ):
            raise RuntimeError(
                f"fault provider {kind} expected SIGSEGV/SIGBUS, got {result.returncode}"
            )
        summary["cases"].append({
            "fault_kind": kind,
            "mode": mode,
            "expected_failure": expected_text or "protected-page signal",
            "returncode": result.returncode,
            "library_sha256": sha256(library),
            "log": f"fault-{kind}.log",
        })

    resolved_line = next(
        (line.split("=", 1)[1] for line in libc_run.stdout.splitlines()
         if line.startswith("resolved_dso=")), None
    )
    if not resolved_line:
        raise RuntimeError("could not identify the resolved libc path")
    libc_path = Path(resolved_line).resolve(strict=True)
    summary["reference"] = {
        "library": "libc.so.6",
        "resolved_path": str(libc_path),
        "resolved_sha256": sha256(libc_path),
        "symbol": "memcpy",
        "returncode": libc_run.returncode,
        "output": libc_run.stdout,
        "log": "reference-libc.log",
    }
    inputs_after = {name: sha256(path) for name, path in (
        (harness.name, harness), (provider.name, provider),
        (Path(__file__).name, Path(__file__).resolve()),
    )}
    if inputs_before != inputs_after:
        raise RuntimeError("test inputs changed during self-test")
    summary["source_sha256_before"] = inputs_before
    summary["source_sha256_after"] = inputs_after
    summary["runner_sha256"] = inputs_before[Path(__file__).name]
    summary["result"] = "PASS"
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print("PASS: libc reference; six expected fault detections; no timings")
    for kind, mode, expected_text in cases:
        result = json.loads((output / "summary.json").read_text())["cases"][kind - 1]
        print(f"  fault-{kind} ({mode}): rejected with rc={result['returncode']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)