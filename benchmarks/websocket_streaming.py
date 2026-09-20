"""Separate inbound WSS streaming benchmark; never replaces round-trip results."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import random
import resource
import statistics
import subprocess
import sys
import tempfile
import time

import websocket as shared
from lab import certificate
from build import build

ROOT = shared.ROOT
CLIENTS = shared.CLIENTS
WORKLOADS = [{"name": f"stream-{size}b-flush{batch}", "bytes": size, "count": count,
              "peer_flush_frames": batch, "connections": 1}
             for batch in (1, 64) for size, count in ((30, 65536), (1024, 16384), (65536, 1024))]


def sample(client, binary, url, ca, body, workload, run_id, env, expected_backend, threads, fault=""):
    command = [str(binary), "--threads", str(threads)] if client == CLIENTS[0] else [
        str(ROOT / ".deps/venv-matched/bin/python"), str(ROOT / "benchmarks/websocket_stream_python.py")]
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    setup_start = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env={**env, "SCRAPANIUM_BENCH_URL":
            f"{url}/stream?id={run_id}&size={workload['bytes']}&count={workload['count']}&batch={workload['peer_flush_frames']}&fault={fault}",
            "SCRAPANIUM_BENCH_CA": ca, "SCRAPANIUM_BENCH_COUNT": str(workload["count"]),
            "SCRAPANIUM_BENCH_BODY": str(body)})
    try:
        if shared.line(process, timeout=120) != "ready":
            raise RuntimeError("invalid readiness signal")
        setup_ms = (time.monotonic() - setup_start) * 1000
        mapping = shared.mapped_backend(process)
        if mapping["sha256"] != expected_backend:
            raise RuntimeError("stream clients loaded a different backend")
        status = Path(f"/proc/{process.pid}/status").read_text().splitlines()
        ready_rss_kib = int(next(line.split()[1] for line in status if line.startswith("VmRSS:")))
        process.stdin.write("x"); process.stdin.flush()
        output, errors = process.communicate(timeout=120)
        cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        if any(marker in errors for marker in ("AddressSanitizer", "UndefinedBehaviorSanitizer", "LeakSanitizer", "runtime error:")):
            raise RuntimeError("sanitizer failure: " + errors)
        if fault:
            if process.returncode == 0 or "stream sequence or payload mismatch" not in output + errors:
                raise RuntimeError(f"negative control {fault} did not detect the intended corruption: {output}{errors}")
            return {"client": client, "fault": fault, "returncode": process.returncode,
                    "detected_by_exact_check": True, "mapped_backend": mapping}
        if process.returncode:
            raise RuntimeError(output + errors)
        start, end = map(float, output.splitlines())
        elapsed = end - start
        if elapsed < 0:
            raise RuntimeError("invalid monotonic timing")
        return {"run_id": run_id, "elapsed_ms": elapsed, "start_ms": start, "end_ms": end,
            "messages_per_second": workload["count"] * 1000 / elapsed if elapsed else None,
            "mapped_backend": mapping, "setup_and_corpus_wall_ms": setup_ms,
            "resident_kib_at_ready": ready_rss_kib,
            "expected_corpus_payload_bytes": workload["count"] * workload["bytes"],
            "client_cpu_including_preparation_setup_close": {
                "user_seconds": cpu_after.ru_utime - cpu_before.ru_utime,
                "system_seconds": cpu_after.ru_stime - cpu_before.ru_stime,
                "voluntary_context_switches": cpu_after.ru_nvcsw - cpu_before.ru_nvcsw,
                "involuntary_context_switches": cpu_after.ru_nivcsw - cpu_before.ru_nivcsw}}
    finally:
        if process.poll() is None:
            process.kill(); process.communicate()


def summarize(rows):
    rates = [row["messages_per_second"] for row in rows]
    if any(rate is None for rate in rates):
        raise RuntimeError("stream sample below timer resolution; increase message count")
    return {"median_ms": statistics.median(row["elapsed_ms"] for row in rows),
        "messages_per_second": statistics.median(rates), "mps_min": min(rates), "mps_max": max(rates),
        "mps_median_bootstrap_95_ci": shared.interval(rates, 20260920), "samples": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--sanitize", action="store_true")
    parser.add_argument("--threads", type=int, choices=(1, 4), default=1)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--workload", action="append", choices=[w["name"] for w in WORKLOADS])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 5 and not args.smoke:
        parser.error("performance reports require at least 5 repeats")
    if args.sanitize and not args.smoke:
        parser.error("sanitizer runs are correctness smoke only")
    output = args.output or ROOT / ("build/wss-stream-smoke.json" if args.smoke else "benchmarks/results/websocket-streaming.json")
    sources = ["scrapanium.bend", "http.bend", "dependencies.json", "scripts/build.py", "tests/lab.py",
        *[str(p.relative_to(ROOT)) for p in (ROOT / "native").glob("*") if p.is_file()],
        "benchmarks/websocket.py", "benchmarks/websocket.bend", "benchmarks/websocket_streaming.py",
        "benchmarks/websocket_stream.bend", "benchmarks/websocket_stream_python.py", "benchmarks/websocket_stream_server.go"]
    source_hashes = {p: shared.digest(ROOT / p) for p in sources}
    binary, peer = ROOT / "build/bench-ws-stream", ROOT / "build/bench-ws-stream-server"
    if not args.no_build:
        build(ROOT / "benchmarks/websocket_stream.bend", binary, args.sanitize)
        subprocess.run(["go", "build", "-trimpath", "-o", peer, ROOT / "benchmarks/websocket_stream_server.go"], check=True)
    binary_hashes = {"bend": shared.digest(binary), "go_peer": shared.digest(peer)}
    env = dict(os.environ)
    if args.sanitize:
        env["ASAN_OPTIONS"] = "detect_leaks=1:halt_on_error=1"
        env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    prefix = Path(env.get("SCRAPANIUM_CURL_DIR", ROOT / ".deps/curl")).resolve()
    library = prefix / "lib" if (prefix / "lib/libcurl-impersonate.so").exists() else prefix
    env["LD_LIBRARY_PATH"] = str(library) + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    backend_hash = shared.digest(library / "libcurl-impersonate.so")
    binding = json.loads(subprocess.check_output([str(ROOT / ".deps/venv-matched/bin/python"), "-c",
        "import curl_cffi,hashlib,json,pathlib,sys; p=pathlib.Path(curl_cffi.__file__).parent; "
        "files=[p/'curl.py',p/'requests/websockets.py',*p.glob('_wrapper*.so')]; "
        "print(json.dumps({'version':curl_cffi.__version__,'python':sys.version,"
        "'files':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}))"], text=True, env=env))
    workloads = [dict(w) for w in WORKLOADS if not args.workload or w["name"] in args.workload]
    if args.smoke:
        for w in workloads: w["count"] = 32
    results, controls, order = [], [], []
    rng = random.Random(args.seed)
    with tempfile.TemporaryDirectory(prefix="scrapanium-stream-") as tmp:
        _, ca = certificate(tmp)
        server = subprocess.Popen([str(peer), "-cert", ca, "-key", str(Path(tmp) / "key.pem")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            url = shared.line(server)
            body = Path(tmp) / "body.bin"
            body.write_bytes(bytes((i * 31) % 128 for i in range(22)))
            negative = {"bytes": 30, "count": 32, "peer_flush_frames": 64}
            for fault in ("corrupt", "swap"):
                for client in CLIENTS:
                    controls.append(sample(client, binary, url, ca, body, negative, f"negative-{fault}-{client}",
                                           env, backend_hash, args.threads, fault))
            print("Both clients rejected byte corruption and reordered sequence tags", flush=True)
            for w in workloads:
                body.write_bytes(bytes((i * 31) % 128 for i in range(w["bytes"] - 8)))
                samples = {client: [] for client in CLIENTS}
                for repeat in range(args.runs):
                    clients = list(CLIENTS); rng.shuffle(clients)
                    for client in clients:
                        run_id = f"{w['name']}-{repeat}-{client}"
                        row = sample(client, binary, url, ca, body, w, run_id, env, backend_hash, args.threads)
                        # The client can exit before the peer gets scheduled to
                        # publish its last write's counters. Wait outside timing.
                        stats_deadline = time.monotonic() + 2
                        while True:
                            server.stdin.write("stats\n"); server.stdin.flush()
                            observed = json.loads(shared.line(server))[run_id]
                            if observed.get("error") or observed["data_frames"] == w["count"] or time.monotonic() >= stats_deadline:
                                break
                            time.sleep(.01)
                        header_size = 2 if w["bytes"] < 126 else 4 if w["bytes"] < 65536 else 10
                        if observed.get("error") or observed["warmups"] != 1 or observed["starts"] != 1 or observed["data_frames"] != w["count"] or observed["data_payload_bytes"] != w["count"] * w["bytes"] or observed["data_frame_bytes"] != w["count"] * (w["bytes"] + header_size):
                            raise RuntimeError(f"stream peer frame/byte count mismatch: {observed}")
                        row["peer_observation"] = observed
                        samples[client].append(row); order.append(run_id)
                    print(f"{w['name']}: repeat {repeat + 1}/{args.runs}", flush=True)
                row = {**w, "expected_corpus_payload_bytes": w["count"] * w["bytes"],
                    "clients": {client: {"samples": values} if args.smoke else summarize(values) for client, values in samples.items()}}
                if not args.smoke:
                    ratios = [py["elapsed_ms"] / bend["elapsed_ms"] for bend, py in zip(samples[CLIENTS[0]], samples[CLIENTS[1]])]
                    row["paired_bend_speed_ratio"] = {"median": statistics.median(ratios),
                        "bootstrap_95_ci": shared.interval(ratios, args.seed), "samples": ratios}
                results.append(row)
        finally:
            server.stdin.close()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired: server.kill(); server.wait()
    if source_hashes != {p: shared.digest(ROOT / p) for p in sources}:
        raise RuntimeError("stream sources changed during build/measurement")
    if binary_hashes != {"bend": shared.digest(binary), "go_peer": shared.digest(peer)} or backend_hash != shared.digest(library / "libcurl-impersonate.so"):
        raise RuntimeError("stream binaries changed during measurement")
    report = {"schema": 1, "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "platform": platform.platform(), "python": sys.version, "cpu_count": os.cpu_count(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)), "go": subprocess.check_output(["go", "version"], text=True).strip(),
        "compiler": subprocess.check_output([os.environ.get("CC", "clang"), "--version"], text=True),
        "dependencies": json.loads((ROOT / "dependencies.json").read_text()), "runs": args.runs, "seed": args.seed,
        "profile": "chrome146", "verify": True, "operation_timeout_ms": 5000,
        "smoke_only": args.smoke, "sanitizers": args.sanitize, "bend_threads": args.threads,
        "metric": "inbound messages per second, never round trips per second",
        "timing": "start signal plus ordered exact-byte-checked receives; excludes expected corpus preparation, startup, TLS upgrade, warmup and close",
        "payload": "8 ASCII decimal sequence digits, then bytes(j*31 %128); every message is unique and checked in order",
        "corpus_limit_per_connection_bytes": 64 * 1024 * 1024,
        "peer_batching": "1 or 64 distinct RFC6455 frames per TLS Write call; identical wire bytes and batching for both clients",
        "uncertainty": "95% repeat-level bootstrap intervals of medians (10,000 resamples); all samples and min/max retained",
        "caveats": ["Loopback WSL/Linux inbound streaming; does not establish WAN or request/response performance.",
            "Expected corpus payload memory is bounded equally; runtime/container overhead differs and RSS is recorded at readiness.",
            "CPU/context-switch accounting includes corpus preparation, setup and close; it is not timed-receive CPU.",
            "Bend IO.now has millisecond resolution; Python uses CLOCK_MONOTONIC nanoseconds.",
            "All frames use one verified TLS connection per sample; no compression, reconnect or TLS resumption in timing.",
            "Scrapanium additionally validates the acceptance challenge during untimed setup.",
            "WebSocket byte counts exclude TLS record overhead. Round-trip results remain in websocket.json."],
        "negative_controls": controls, "execution_order": order,
        "backend": {"path": str((library / "libcurl-impersonate.so").resolve()), "sha256": backend_hash},
        "curl_cffi_binding": binding, "source_sha256": source_hashes, "binary_sha256": binary_hashes,
        "source_hashes_checked_before_build_and_after_samples": True, "built_by_this_run": not args.no_build,
        "workloads": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(output)


if __name__ == "__main__": main()
