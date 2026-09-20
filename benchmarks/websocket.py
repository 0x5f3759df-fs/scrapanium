"""Fair warm WSS measurements using real Bend and curl_cffi clients.

Run after correctness tests, with the machine otherwise idle. All samples and
workloads are retained. No browsers or external network service are involved.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import resource
import select
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "scripts")]
from build import build
from lab import certificate
from ws_lab import start_ws

ORIGINAL = b"scrapanium-websocket-benchmark"
CLIENTS = ("scrapanium-bend", "curl_cffi-matched")
WORKLOADS = [
    {"name": "original-python-30b-c1", "peer": "python", "bytes": len(ORIGINAL), "connections": 1, "count": 1000},
    *[{"name": f"go-{size}b-c{connections}", "peer": "go", "bytes": size,
       "connections": connections, "count": count}
      for connections in (1, 4) for size, count in ((30, 5000), (1024, 2000), (65536, 300))],
]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def line(process, timeout=30):
    if not select.select([process.stdout], [], [], timeout)[0]:
        raise RuntimeError(f"process {process.pid} did not become ready")
    value = process.stdout.readline().strip()
    if not value:
        raise RuntimeError(f"process {process.pid} exited: {process.stderr.read()}")
    return value


def mapped_backend(process):
    matches = {entry.split()[-1] for entry in Path(f"/proc/{process.pid}/maps").read_text().splitlines()
               if "libcurl-impersonate.so" in entry}
    if len(matches) != 1:
        raise RuntimeError(f"expected one mapped curl backend for PID {process.pid}: {matches}")
    path = Path(matches.pop()).resolve()
    return {"path": str(path), "sha256": digest(path)}


def sample(client, binary, url, ca, payload, workload, run_id, env):
    processes, maps = [], []
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    command = [str(binary), "--threads", "1"] if client == CLIENTS[0] else [
        str(ROOT / ".deps/venv-matched/bin/python"), str(ROOT / "benchmarks/websocket_python.py")]
    try:
        for connection in range(workload["connections"]):
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env={**env, "SCRAPANIUM_BENCH_URL": f"{url}/echo?id={run_id}-{connection}",
                    "SCRAPANIUM_BENCH_CA": ca, "SCRAPANIUM_BENCH_COUNT": str(workload["count"]),
                    "SCRAPANIUM_BENCH_PAYLOAD": str(payload)})
            processes.append(process)
        for process in processes:
            if line(process) != "ready":
                raise RuntimeError("invalid client readiness signal")
            maps.append(mapped_backend(process))
        # Each process has a dedicated warm TLS connection before any start signal.
        for process in processes:
            process.stdin.write("x"); process.stdin.flush()
        intervals = []
        for process in processes:
            output, errors = process.communicate(timeout=90)
            if process.returncode:
                raise RuntimeError(output + errors)
            start, end = map(float, output.splitlines())
            if end <= start:
                raise RuntimeError("invalid timing interval")
            intervals.append({"start_ms": start, "end_ms": end, "elapsed_ms": end - start})
        elapsed = max(row["end_ms"] for row in intervals) - min(row["start_ms"] for row in intervals)
        cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        return {"run_id": run_id, "elapsed_ms": elapsed, "intervals": intervals,
            "start_spread_ms": max(row["start_ms"] for row in intervals) - min(row["start_ms"] for row in intervals),
            "round_trips_per_second": workload["count"] * workload["connections"] * 1000 / elapsed,
            "mapped_backends": maps,
            "client_cpu_including_setup_close": {"user_seconds": cpu_after.ru_utime - cpu_before.ru_utime,
                "system_seconds": cpu_after.ru_stime - cpu_before.ru_stime,
                "voluntary_context_switches": cpu_after.ru_nvcsw - cpu_before.ru_nvcsw,
                "involuntary_context_switches": cpu_after.ru_nivcsw - cpu_before.ru_nivcsw}}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill(); process.communicate()


def interval(values, seed):
    """Repeat-level nonparametric bootstrap CI, not a claim about all machines."""
    rng = random.Random(seed)
    medians = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(10000))
    return [medians[249], medians[9749]]


def summarize(samples, count):
    times = [sample["elapsed_ms"] for sample in samples]
    rates = [sample["round_trips_per_second"] for sample in samples]
    return {"times_ms": times, "median_ms": statistics.median(times),
        "round_trips_per_second": count * 1000 / statistics.median(times),
        "rps_median_bootstrap_95_ci": interval(rates, 20260920),
        "rps_min": min(rates), "rps_max": max(rates), "samples": samples}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--workload", action="append", choices=[w["name"] for w in WORKLOADS])
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/results/websocket.json")
    parser.add_argument("--smoke", action="store_true", help="20 exchanges; correctness only, never publish as performance")
    args = parser.parse_args()
    if args.runs < 5 and not args.smoke:
        parser.error("performance reports require at least 5 repeats")
    binary = ROOT / "build/bench-ws"
    go_binary = ROOT / "build/bench-ws-server"
    sources = ["scrapanium.bend", "http.bend", "dependencies.json", "scripts/build.py",
        *[str(p.relative_to(ROOT)) for p in (ROOT / "native").glob("*") if p.is_file()],
        "benchmarks/websocket.bend", "benchmarks/websocket.py", "benchmarks/websocket_python.py",
        "benchmarks/websocket_server.go", "tests/ws_lab.py"]
    source_hashes = {p: digest(ROOT / p) for p in sources}
    if not args.no_build:
        build(ROOT / "benchmarks/websocket.bend", binary)
        subprocess.run(["go", "build", "-trimpath", "-o", go_binary, ROOT / "benchmarks/websocket_server.go"], check=True)
    binary_hashes = {"bend": digest(binary), "go_peer": digest(go_binary)}
    env = dict(os.environ)
    prefix = Path(env.get("SCRAPANIUM_CURL_DIR", ROOT / ".deps/curl")).resolve()
    library = prefix / "lib" if (prefix / "lib/libcurl-impersonate.so").exists() else prefix
    if not (library / "libcurl-impersonate.so").exists():
        raise RuntimeError("set SCRAPANIUM_CURL_DIR to the tested backend prefix")
    env["LD_LIBRARY_PATH"] = str(library) + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    expected_backend = digest(library / "libcurl-impersonate.so")
    binding_provenance = json.loads(subprocess.check_output([
        str(ROOT / ".deps/venv-matched/bin/python"), "-c",
        "import curl_cffi, hashlib, json, pathlib, sys; "
        "p=pathlib.Path(curl_cffi.__file__).parent; "
        "files=[p/'curl.py',p/'requests/websockets.py',*p.glob('_wrapper*.so')]; "
        "print(json.dumps({'version':curl_cffi.__version__,'python':sys.version,"
        "'files':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}))"], env=env, text=True))
    workloads = [dict(w) for w in WORKLOADS if not args.workload or w["name"] in args.workload]
    if args.smoke:
        for w in workloads: w["count"] = 20
    rng = random.Random(args.seed)
    results, execution_order = [], []
    with tempfile.TemporaryDirectory(prefix="scrapanium-wss-") as tmp:
        context, ca = certificate(tmp)
        python_server, python_url = start_ws(context)
        go_server = subprocess.Popen([str(go_binary), "-cert", ca, "-key", str(Path(tmp) / "key.pem")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            go_url = line(go_server)
            for w in workloads:
                payload = ORIGINAL if w["bytes"] == len(ORIGINAL) else bytes((i * 131 + 17) % 256 for i in range(w["bytes"]))
                path = Path(tmp) / "payload.bin"; path.write_bytes(payload)
                samples = {client: [] for client in CLIENTS}
                for repeat in range(args.runs):
                    order = list(CLIENTS); rng.shuffle(order)
                    for client in order:
                        run_id = f"{w['name']}-{repeat}-{client}"
                        before = len(python_server.frames)
                        result = sample(client, binary, go_url if w["peer"] == "go" else python_url,
                                        ca, path, w, run_id, env)
                        if any(m["sha256"] != expected_backend for m in result["mapped_backends"]):
                            raise RuntimeError("clients did not load the same expected backend binary")
                        frames = (w["count"] + 1) * w["connections"]
                        if w["peer"] == "python":
                            captured = python_server.frames[before:]
                            if len(captured) != frames or any(frame != (2, payload, True) for frame in captured):
                                raise RuntimeError("Python peer observed unexpected bytes, opcode, FIN or frame count")
                            length_bytes = 0 if len(payload) < 126 else 2 if len(payload) < 65536 else 8
                            result["peer_observation"] = {"frames": len(captured), "payload_bytes": frames * len(payload),
                                "client_frame_bytes": frames * (len(payload) + 6 + length_bytes),
                                "server_frame_bytes": frames * (len(payload) + 2 + length_bytes)}
                        else:
                            go_server.stdin.write("stats\n"); go_server.stdin.flush()
                            records = json.loads(line(go_server))
                            observed = [records[f"{run_id}-{i}"] for i in range(w["connections"])]
                            if any(row.get("error") or row["frames"] != w["count"] + 1 or
                                   row["payload_bytes"] != (w["count"] + 1) * len(payload) for row in observed):
                                raise RuntimeError(f"Go peer observed wrong frame/byte counts: {observed}")
                            result["peer_observation"] = observed
                        samples[client].append(result)
                        execution_order.append(run_id)
                    print(f"{w['name']}: repeat {repeat + 1}/{args.runs}", flush=True)
                result = {**w, "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "clients": {client: summarize(rows, w["count"] * w["connections"]) for client, rows in samples.items()}}
                ratios = [py["elapsed_ms"] / bend["elapsed_ms"] for bend, py in zip(samples[CLIENTS[0]], samples[CLIENTS[1]])]
                result["paired_bend_speed_ratio"] = {"median": statistics.median(ratios),
                    "bootstrap_95_ci": interval(ratios, args.seed), "samples": ratios}
                results.append(result)
        finally:
            python_server.shutdown(); python_server.server_close()
            go_server.stdin.close()
            try: go_server.wait(timeout=5)
            except subprocess.TimeoutExpired: go_server.kill(); go_server.wait()
    if source_hashes != {p: digest(ROOT / p) for p in sources}:
        raise RuntimeError("benchmark sources changed after build or during measurement; refusing provenance")
    if binary_hashes != {"bend": digest(binary), "go_peer": digest(go_binary)} or expected_backend != digest(library / "libcurl-impersonate.so"):
        raise RuntimeError("benchmark binaries changed during measurement; refusing provenance")
    report = {"schema": 2, "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "platform": platform.platform(), "python": sys.version, "cpu_count": os.cpu_count(),
        "cpu_model": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                           if line.startswith("model name")), "unknown"),
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "compiler": subprocess.check_output([os.environ.get("CC", "clang"), "--version"], text=True),
        "go": subprocess.check_output(["go", "version"], text=True).strip(),
        "dependencies": json.loads((ROOT / "dependencies.json").read_text()), "runs": args.runs,
        "seed": args.seed, "smoke_only": args.smoke, "profile": "chrome146", "verify": True,
        "timing": "warm exact-byte-checked binary round trips; startup, upgrade, one warmup and close excluded",
        "concurrency": "one dedicated connection per client process; common stdin start gate; aggregate max(end)-min(start)",
        "uncertainty": "95% repeat-level bootstrap intervals of medians (10,000 resamples); report all samples and min/max",
        "caveats": ["Loopback WSL/Linux workloads do not establish Internet performance.",
            "Bend IO.now has millisecond resolution; Python uses CLOCK_MONOTONIC nanoseconds.",
            "Independent processes measure multi-connection throughput, not single-event-loop application scaling.",
            "Binary payload length and every byte plus opcode checked by both clients inside timing.",
            "Both clients reuse one TLS connection per process; no reconnect, TLS resumption or compression in timed section.",
            "Bend additionally validates the WebSocket acceptance challenge during untimed setup.",
            "WebSocket byte counts include frame headers/masks; TLS record overhead is not measured.",
            "Original published benchmark used asymmetric validation; original payload, peer and 1000-count workload retained here with parity corrected."],
        "backend": {"path": str((library / "libcurl-impersonate.so").resolve()), "sha256": expected_backend},
        "curl_cffi_binding": binding_provenance,
        "source_sha256": source_hashes,
        "source_hashes_checked_before_build_and_after_samples": True,
        "built_by_this_run": not args.no_build,
        "binary_sha256": binary_hashes,
        "execution_order": execution_order, "workloads": results}
    # Keep the original top-level shape for readers of the single-workload chart.
    original = next((w for w in results if w["name"].startswith("original-")), None)
    if original:
        report.update({"count": original["count"], "payload_bytes": original["bytes"], "clients": original["clients"]})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__": main()
