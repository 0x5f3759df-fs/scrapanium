#!/usr/bin/env python3
"""Run deterministic --wrap fault cases against the real candidate TLS peer."""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(FIXTURES))

from lab import certificate  # noqa: E402
from direct_recv_peer import start_peer  # noqa: E402


MODES = {
    "pending-false": "/stale",
    "stale-again": "/stale",
    "empty-eof": "/stale",
    "fatal": "/stale",
    "drain-bound": "/drain",
}
EXPECTED = {
    "pending-false": [(9, b"resume", True), (8, b"\x03\xe8", True)],
    "stale-again": [(9, b"resume", True), (8, b"\x03\xe8", True)],
    "empty-eof": [(9, b"resume", True), (8, b"\x03\xe8", True)],
    "fatal": [(9, b"resume", True), (8, b"\x03\xe8", True)],
    "drain-bound": [(8, b"\x03\xe8", True)],
}


def wait_for_peer(server, expected: list[tuple], timeout: float = 5.0) -> list[tuple]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = list(server.frames)
        if len(observed) >= len(expected):
            return observed
        time.sleep(0.005)
    raise TimeoutError(f"fault peer did not capture expected marker+close: {server.frames!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--modes", nargs="+", choices=tuple(MODES), default=tuple(MODES))
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    if len(set(args.modes)) != len(args.modes):
        parser.error("fault modes must be unique")

    with tempfile.TemporaryDirectory(prefix="wss-wrapped-faults-") as temp:
        context, ca_path = certificate(Path(temp) / "tls")
        for mode in args.modes:
            server, origin = start_peer(context)
            try:
                result = subprocess.run([str(binary), mode, origin + MODES[mode], ca_path],
                                        capture_output=True, text=True, timeout=30,
                                        check=False)
                sys.stdout.write(f"[{mode}] exit={result.returncode}\n{result.stdout}")
                sys.stderr.write(result.stderr)
                observed = wait_for_peer(server, expected=EXPECTED[mode])
                sys.stdout.write(f"[{mode}] peer_frames={observed!r}\n")
                if result.returncode:
                    raise RuntimeError(
                        f"{mode} failed; peer frames={observed!r}; "
                        f"peer errors={server.errors!r}; stderr={result.stderr!r}"
                    )
                if observed != EXPECTED[mode]:
                    raise AssertionError(
                        f"{mode}: peer expected {EXPECTED[mode]!r}, got {observed!r}"
                    )
                if server.errors:
                    raise AssertionError(f"{mode}: unexpected peer errors {server.errors!r}")
            finally:
                server.shutdown()
                server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
