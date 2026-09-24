#!/usr/bin/env python3
"""Run ignored direct curl_ws_recv fixtures against isolated backend binaries."""
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
from ws_lab import start_ws  # noqa: E402
from direct_recv_peer import start_peer  # noqa: E402


CASES = {
    "large": ("large", "/large", False),
    "fragmented": ("fragmented", "/fragmented-binary", False),
    "queued": ("queued", "/queued", True),
    "autopong": ("autopong", "/split-ping", True),
    "stale": ("stale", "/stale", True),
    "truncated": ("truncated", "/truncated", False),
    "fatal": ("truncated", "/fatal", True),
}
NORMAL_FRAMES = {
    "large": [(2, b"next\x00\xff", True), (2, b"", True), (8, b"\x03\xe8", True)],
    "fragmented": [*((10, bytes((i, 0, 255)), True) for i in range(6)),
                   (8, b"\x03\xe8", True)],
    "queued": [(8, b"\x03\xe8", True)],
    "stale": [(9, b"resume", True), (8, b"\x03\xe8", True)],
    "truncated": [],
    "fatal": [],
}


def wait_peer_done(server, name: str, timeout: float = 5.0) -> list[tuple]:
    expected = NORMAL_FRAMES.get(name)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = list(server.frames)
        if name == "autopong":
            if observed and observed[-1] == (8, b"\x03\xe8", True):
                return observed
        elif expected == [] or len(observed) >= len(expected):
            return observed
        time.sleep(0.005)
    raise TimeoutError(f"peer did not finish {name}; captured {list(server.frames)!r}")


def run_case(binary: Path, label: str, name: str, context, ca_path: str) -> list[tuple]:
    mode, route, custom = CASES[name]
    server, origin = start_peer(context) if custom else start_ws(context)
    try:
        result = subprocess.run([str(binary), mode, origin + route, ca_path],
                                text=True, capture_output=True, timeout=30, check=False)
        sys.stdout.write(f"[{label}/{name}] exit={result.returncode}\n")
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.returncode:
            raise RuntimeError(
                f"{label}/{name} failed ({result.returncode}); peer frames={server.frames!r}; "
                f"peer errors={getattr(server, 'errors', [])!r}; stderr={result.stderr!r}"
            )
        observed = wait_peer_done(server, name)
        sys.stdout.write(f"[{label}/{name}] peer_frames={observed!r}\n")
        if name == "autopong":
            if (not observed or observed[-1] != (8, b"\x03\xe8", True) or
                    any(kind != 10 or not final for kind, _, final in observed[:-1])):
                raise AssertionError(f"{label}/autopong peer capture is malformed: {observed!r}")
            payloads = [payload.hex() for kind, payload, _ in observed[:-1]]
            sys.stdout.write(f"[{label}/autopong] observed auto-PONG payloads={payloads!r}\n")
        else:
            expected = NORMAL_FRAMES[name]
            if observed != expected:
                raise AssertionError(
                    f"{label}/{name}: expected peer frames {expected!r}, got {observed!r}"
                )
        peer_errors = getattr(server, "errors", [])
        if peer_errors:
            sys.stdout.write(f"[{label}/{name}] peer errors={peer_errors!r}\n")
        return observed
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True,
                        help="baseline direct-receive binary")
    parser.add_argument("--compare-binary", type=Path,
                        help="optional candidate binary; results are run separately and compared")
    parser.add_argument("--label", default="baseline", help="label for --binary")
    parser.add_argument("--compare-label", default="candidate", help="label for --compare-binary")
    parser.add_argument("--cases", nargs="+", choices=(*CASES, "all"), default=["all"])
    args = parser.parse_args()
    baseline = args.binary.resolve(strict=True)
    candidate = args.compare_binary.resolve(strict=True) if args.compare_binary else None
    names = list(CASES) if "all" in args.cases else args.cases
    if len(set(names)) != len(names):
        parser.error("case names must be unique")

    with tempfile.TemporaryDirectory(prefix="wss-direct-cases-") as temp:
        context, ca_path = certificate(Path(temp) / "tls")
        for name in names:
            before = run_case(baseline, args.label, name, context, ca_path)
            if candidate:
                after = run_case(candidate, args.compare_label, name, context, ca_path)
                if before != after:
                    raise AssertionError(
                        f"baseline/candidate direct behavior differs for {name}: "
                        f"{before!r} != {after!r}"
                    )
                sys.stdout.write(f"[{name}] baseline/candidate peer observations match\n")
                if name == "autopong":
                    sys.stdout.write(
                        "[autopong] known split-PING auto-PONG limitation has parity only; "
                        "this is not a protocol-conformance pass\n"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
