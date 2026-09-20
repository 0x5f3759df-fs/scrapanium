"""Randomized paired loopback runs. Only timed client work, excluding startup.

This measures throughput of buffered responses, not text decoding. The Python
fixture can be the bottleneck. No results here establish Internet performance.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "scripts")]
from build import build
from lab import start_http, start_h2, certificate

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--output", default="benchmarks/results/local.json")
    parser.add_argument("--tls-client", action="store_true", help="also build/run the pinned Go comparison")
    args = parser.parse_args()
    build(ROOT / "benchmarks/client.bend", ROOT / "build/bench-bend")
    curl = ROOT / ".deps/curl"
    subprocess.run(["clang", "-O3", "-std=c11", "-I" + str(ROOT / "native"),
        "-I" + str(curl / "include"), str(ROOT / "benchmarks/native.c"), str(ROOT / "native/scrapanium.c"),
        "-L" + str(curl), "-Wl,-rpath," + str(curl), "-lcurl-impersonate", "-lpthread",
        "-o", str(ROOT / "build/bench-native")], check=True)
    if args.tls_client:
        subprocess.run(["go", "build", "-buildvcs=false", "-o", str(ROOT / "build/bench-tls-client"), "."], cwd=ROOT / "benchmarks/tls-client", check=True)
    servers, rows = [], []
    rng = random.Random(20260919)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            http, url = start_http(); servers.append(http)
            context, ca1 = certificate(Path(tmp) / "h1")
            https, tlsurl = start_http(context); servers.append(https)
            context, ca2 = certificate(Path(tmp) / "h2")
            h2, h2url = start_h2(context); servers.append(h2)
            workloads = [("http1-small", url, "sequential", args.count, ""),
                ("http1-64KiB", url + "/bytes/65536", "sequential", max(100, args.count // 4), ""),
                ("https1-small", tlsurl, "sequential", args.count, ca1),
                ("https2-small", h2url, "sequential", args.count, ca2),
                ("http1-batch16-delay10ms", url + "/slow/10", "batch", 256, ""),
                ("https2-batch16", h2url, "batch", args.count, ca2)]
            clients = ["scrapanium-bend", "scrapanium-c", "curl_cffi-release", "curl_cffi-matched"]
            if args.tls_client: clients.append("tls-client-go")
            for name, target, mode, count, verify in workloads:
                data = {c: [] for c in clients}
                usage = {c: [] for c in clients}
                for repeat in range(args.runs):
                    order = clients[:]; rng.shuffle(order)
                    for client in order:
                        env = {**os.environ, "SCRAPANIUM_BENCH_URL": target, "SCRAPANIUM_BENCH_MODE": mode,
                            "SCRAPANIUM_BENCH_COUNT": str(count), "SCRAPANIUM_BENCH_CA": verify}
                        if client == "scrapanium-bend": cmd = [str(ROOT / "build/bench-bend"), "--threads", "1"]
                        elif client == "scrapanium-c": cmd = [str(ROOT / "build/bench-native"), target, mode, str(count), verify]
                        elif client == "tls-client-go": cmd = [str(ROOT / "build/bench-tls-client"), target, mode, str(count), verify]
                        else:
                            venv = ".deps/venv-matched" if client.endswith("matched") else ".deps/venv"
                            cmd = [str(ROOT / venv / "bin/python"), str(ROOT / "benchmarks/python_client.py"), target, mode, str(count), verify]
                        usage_file = ROOT / "build/bench-usage.json"
                        cmd = ["/usr/bin/time", "-f", '{"peak_rss_kib":%M,"user_cpu_s":%U,"system_cpu_s":%S}',
                               "-o", str(usage_file), *cmd]
                        run = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
                        if run.returncode:
                            raise RuntimeError(f"{name}/{client}: {run.stdout}\n{run.stderr}")
                        ms = float(run.stdout.strip())
                        assert ms > 0
                        data[client].append(ms)
                        usage[client].append(json.loads(usage_file.read_text()))
                    print(name, repeat + 1, flush=True)
                for client, times in data.items():
                    rows.append({"workload": name, "client": client, "requests": count, "times_ms": times,
                        "median_ms": statistics.median(times), "requests_per_second": count * 1000 / statistics.median(times),
                        "process_resources": usage[client],
                        "median_peak_rss_kib": statistics.median(x["peak_rss_kib"] for x in usage[client]),
                        "median_process_cpu_s": statistics.median(x["user_cpu_s"] + x["system_cpu_s"] for x in usage[client]),
                        "min_ms": min(times), "max_ms": max(times)})
            versions = {}
            for label, venv in [("release", "venv"), ("matched", "venv-matched")]:
                versions[label] = subprocess.check_output([str(ROOT / ".deps" / venv / "bin/python"), "-c",
                    "import curl_cffi; print(curl_cffi.__version__); print(curl_cffi.Curl().version().decode())"], text=True).strip()
            report = {"date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "platform": platform.platform(), "cpu": Path("/proc/cpuinfo").read_text().split("model name\t: ")[1].splitlines()[0],
                "cpu_count": os.cpu_count(), "python": sys.version, "baseline_versions": versions,
                "compiler": subprocess.check_output(["clang", "--version"], text=True).splitlines()[0],
                "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                    ["scrapanium.bend", "http.bend", *[str(p.relative_to(ROOT)) for p in (ROOT / "native").glob("*")],
                     "benchmarks/client.bend", "benchmarks/native.c", "benchmarks/python_client.py", "benchmarks/tls-client/main.go"]},
                "runs": args.runs, "shuffle_seed": 20260919, "warmup_requests": 1,
                "bend_runtime_threads": 1, "batch_concurrency": 16,
                "browser_profile": "chrome146", "browser_default_headers": False,
                "request_headers": {"User-Agent": "scrapanium-benchmark", "Accept": "*/*", "Accept-Encoding": "gzip, deflate, br, zstd"},
                "tls_client_commit": "34718e1b514b446b95bc68dc4f096247e69c7939" if args.tls_client else None,
                "go_version": subprocess.check_output(["go", "version"], text=True).strip() if args.tls_client else None,
                "limitations": ["Loopback Python servers may limit throughput", "Bend timer resolution is 1 ms",
                    "Buffers consumed/discarded; no text/JSON decoding", "Batch retains all responses until completion",
                    "CPU and peak RSS cover the entire client process, including startup and warmup, unlike throughput timing",
                    "Startup and session construction excluded; first TLS handshake excluded by warmup"], "results": rows}
            out = ROOT / args.output; out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2) + "\n")
            print(out)
    finally:
        for server in servers: server.shutdown(); server.server_close()

if __name__ == "__main__": main()
