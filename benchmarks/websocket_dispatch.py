"""Attribute WSS gains to readiness dispatch, using one checked Bend workload.

Reproduce: .deps/venv-matched/bin/python benchmarks/websocket_dispatch.py
The before variant replaces only the native WS implementation and bridge with
git objects from BASELINE. By default both variants use the recorded readiness
revision's exact-byte checks, compiler, flags and backend; --after-ref current
explicitly compares the working tree. This is not a curl_cffi comparison.
"""
import argparse
import datetime
import io
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tarfile
import tempfile

import websocket as shared

ROOT = shared.ROOT
BASELINE = "b9b5bc1524b067117b4715029a79666b44ca0c83"
READINESS = "64be2ff9e7459225bacb6fcb40223db95e980b2d"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--after-ref", default=READINESS,
                        help="revision supplying the checked harness and after implementation, or 'current'")
    args = parser.parse_args()
    if args.runs < 5 and not args.smoke:
        parser.error("performance attribution requires at least 5 paired repeats")
    output = args.output or ROOT / ("build/ws-dispatch-smoke.json" if args.smoke else
                                   "benchmarks/results/websocket-dispatch-attribution.json")
    if args.after_ref != "current":
        # Keep the original attribution reproducible after later WS optimizations.
        # Materialize tracked files only; use the existing pinned dependencies.
        revision = subprocess.check_output(["git", "rev-parse", "--verify", args.after_ref + "^{commit}"],
                                           cwd=ROOT, text=True).strip()
        (ROOT / "build").mkdir(exist_ok=True)
        checkout = Path(tempfile.mkdtemp(prefix="ws-dispatch-ref-", dir=ROOT / "build"))
        archive = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tracked:
            tracked.extractall(checkout, filter="data")
        (checkout / ".deps").symlink_to((ROOT / ".deps").resolve(), target_is_directory=True)
        # Run this reproducer with the selected revision's benchmark/build code.
        runner = checkout / "benchmarks/websocket_dispatch.py"
        runner.write_bytes(Path(__file__).read_bytes())
        command = [sys.executable, str(runner), "--after-ref", "current", "--runs", str(args.runs),
                   "--seed", str(args.seed), "--output", str(output.resolve())]
        if args.smoke:
            command.append("--smoke")
        subprocess.run(command, cwd=checkout, check=True,
                       env={**os.environ, "SCRAPANIUM_DISPATCH_RESOLVED_REF": revision})
        return
    prefix = Path(os.environ.get("SCRAPANIUM_CURL_DIR", ROOT / ".deps/curl")).resolve()
    library = prefix / "lib" if (prefix / "lib/libcurl-impersonate.so").exists() else prefix
    backend = library / "libcurl-impersonate.so"
    backend_hash = shared.digest(backend)
    env = {**os.environ, "LD_LIBRARY_PATH": str(library)}
    env.pop("LD_PRELOAD", None)
    sources = [ROOT / name for name in ("scrapanium.bend", "http.bend", "dependencies.json",
        "scripts/build.py", "tests/lab.py", "tests/ws_lab.py", "benchmarks/websocket.py",
        "benchmarks/websocket.bend", "benchmarks/websocket_server.go", "benchmarks/websocket_dispatch.py")]
    sources += [p for p in (ROOT / "native").iterdir() if p.is_file()]
    source_hashes = {str(p.relative_to(ROOT)): shared.digest(p) for p in sources}
    scratch = ROOT / "build/ws-dispatch"
    scratch.mkdir(parents=True, exist_ok=True)
    old_sources = {}
    for name in ("websocket.inc.c", "bend_websocket.inc.c"):
        path = scratch / name
        path.write_bytes(subprocess.check_output(["git", "show", f"{BASELINE}:native/{name}"], cwd=ROOT))
        old_sources[name] = path
    assert b"sp_ws_io" not in old_sources["bend_websocket.inc.c"].read_bytes()
    binaries = {"worker_dispatch": scratch / "before", "readiness_dispatch": scratch / "after"}
    shared.build(ROOT / "benchmarks/websocket.bend", binaries["readiness_dispatch"])
    generated = binaries["readiness_dispatch"].with_suffix(".generated.c")
    client = generated.read_text()
    native = (ROOT / "native/scrapanium.c").read_text()
    if client.count('#include "bend_websocket.inc.c"') != 1 or native.count('#include "websocket.inc.c"') != 1:
        raise RuntimeError("WS include layout changed; re-audit attribution build")
    before_client, before_native = scratch / "before.generated.c", scratch / "before-native.c"
    before_client.write_text(client.replace('#include "bend_websocket.inc.c"',
        f'#include "{old_sources["bend_websocket.inc.c"]}"'))
    before_native.write_text(native.replace('#include "websocket.inc.c"',
        f'#include "{old_sources["websocket.inc.c"]}"'))
    # Match scripts/build.py's release flags, including the selected backend.
    subprocess.run([os.environ.get("CC", "clang"), str(before_client), str(before_native),
        "-std=c11", "-O3", "-g", "-I" + str(prefix / "include"), "-I" + str(ROOT / "native"),
        "-lpthread", "-lm", "-L" + str(library), "-Wl,-rpath," + str(library),
        "-lcurl-impersonate", "-o", str(binaries["worker_dispatch"])], check=True)
    peer = scratch / "server"
    subprocess.run(["go", "build", "-trimpath", "-o", peer, ROOT / "benchmarks/websocket_server.go"], check=True)
    artifacts = [*old_sources.values(), generated, before_client, before_native, *binaries.values(), peer]
    artifact_hashes = {str(p.relative_to(ROOT)): shared.digest(p) for p in artifacts}
    workloads = [dict(w) for w in shared.WORKLOADS if w["name"] in ("original-python-30b-c1", "go-30b-c1")]
    if args.smoke:
        for w in workloads:
            w["count"] = 20
    rng = random.Random(args.seed)
    results, execution_order = [], []
    with tempfile.TemporaryDirectory(prefix="scrapanium-dispatch-") as tmp:
        context, ca = shared.certificate(tmp)
        server, url = shared.start_ws(context)
        go = subprocess.Popen([str(peer), "-cert", ca, "-key", str(Path(tmp) / "key.pem")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            go_url = shared.line(go)
            payload = Path(tmp) / "payload.bin"
            payload.write_bytes(shared.ORIGINAL)
            for w in workloads:
                samples = {name: [] for name in binaries}
                for repeat in range(args.runs):
                    order = list(binaries)
                    rng.shuffle(order)
                    for name in order:
                        run_id = f"{w['name']}-{repeat}-{name}"
                        before = len(server.frames)
                        row = shared.sample(shared.CLIENTS[0], binaries[name], go_url if w["peer"] == "go" else url,
                            ca, payload, w, run_id, env)
                        if any(m["sha256"] != backend_hash for m in row["mapped_backends"]):
                            raise RuntimeError("dispatch variants loaded different backends")
                        if w["peer"] == "python":
                            actual = server.frames[before:]
                            if len(actual) != w["count"] + 1 or any(f != (2, shared.ORIGINAL, True) for f in actual):
                                raise RuntimeError("unexpected payload, opcode, FIN or frame count")
                            row["observed_frames"] = len(actual)
                        else:
                            go.stdin.write("stats\n"); go.stdin.flush()
                            observed = json.loads(shared.line(go))[run_id + "-0"]
                            if observed.get("error") or observed["frames"] != w["count"] + 1 or observed["payload_bytes"] != (w["count"] + 1) * len(shared.ORIGINAL):
                                raise RuntimeError(f"unexpected peer frame counts: {observed}")
                            row["peer_observation"] = observed
                        samples[name].append(row)
                        execution_order.append(run_id)
                    print(f"{w['name']}: paired repeat {repeat + 1}/{args.runs}", flush=True)
                row = {**w, "samples": samples}
                if not args.smoke:
                    ratios = [old["elapsed_ms"] / new["elapsed_ms"] for old, new in
                        zip(samples["worker_dispatch"], samples["readiness_dispatch"])]
                    row["paired_speed_ratio"] = {"samples": ratios, "median": statistics.median(ratios),
                        "bootstrap_95_ci": shared.interval(ratios, args.seed)}
                results.append(row)
        finally:
            server.shutdown(); server.server_close(); go.stdin.close()
            try:
                go.wait(timeout=5)
            except subprocess.TimeoutExpired:
                go.kill(); go.wait()
    if source_hashes != {str(p.relative_to(ROOT)): shared.digest(p) for p in sources}:
        raise RuntimeError("sources changed during attribution")
    if artifact_hashes != {str(p.relative_to(ROOT)): shared.digest(p) for p in artifacts} or shared.digest(backend) != backend_hash:
        raise RuntimeError("binaries changed during attribution")
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "platform": platform.platform(),
        "purpose": "old/new Scrapanium WS dispatch using the same exact-byte-checked Bend harness; not a curl_cffi comparison",
        "baseline_ref": BASELINE, "after_ref": os.environ.get("SCRAPANIUM_DISPATCH_RESOLVED_REF", "current"),
        "runs": args.runs, "seed": args.seed, "smoke_only": args.smoke,
        "backend": {"path": str(backend.resolve()), "sha256": backend_hash},
        "source_sha256": source_hashes, "artifact_sha256": artifact_hashes,
        "caveats": ["Legacy published figures used asymmetric validation and are not directly comparable.",
            "Both variants retain TLS verification, challenge checks, masking, deadlines, and message validation.",
            "Includes the shared receive-state refactor, not only an isolated scheduler primitive.",
            "Loopback measurements and 5-repeat uncertainty do not establish a universal speedup."],
        "execution_order": execution_order, "workloads": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
