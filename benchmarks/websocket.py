"""Repeated warm WSS round trips, same backend/profile/payload and TLS checking."""
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
from lab import certificate
from ws_lab import start_ws


def main():
    binary = build(ROOT / "benchmarks/websocket.bend", ROOT / "build/bench-ws")
    values = {"scrapanium-bend": [], "curl_cffi-matched": []}
    rng = random.Random(20260920)
    with tempfile.TemporaryDirectory() as tmp:
        context, ca = certificate(tmp)
        server, url = start_ws(context)
        try:
            for repeat in range(5):
                order = list(values); rng.shuffle(order)
                for client in order:
                    command = [str(binary), "--threads", "1"] if client == "scrapanium-bend" else [str(ROOT / ".deps/venv-matched/bin/python"), str(ROOT / "benchmarks/websocket_python.py")]
                    result = subprocess.run(command, capture_output=True, text=True, timeout=30, env={**os.environ,
                        "SCRAPANIUM_BENCH_URL": url + "/echo", "SCRAPANIUM_BENCH_CA": ca, "SCRAPANIUM_BENCH_COUNT": "1000"})
                    if result.returncode: raise RuntimeError(result.stdout + result.stderr)
                    values[client].append(float(result.stdout.strip()))
                print("WSS run", repeat + 1, flush=True)
        finally: server.shutdown(); server.server_close()
    sources = ["scrapanium.bend", "http.bend", *[str(p.relative_to(ROOT)) for p in (ROOT / "native").glob("*")],
               "benchmarks/websocket.bend", "benchmarks/websocket.py", "benchmarks/websocket_python.py", "tests/ws_lab.py"]
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "platform": platform.platform(),
        "dependencies": json.loads((ROOT / "dependencies.json").read_text()), "count": 1000, "runs": 5,
        "profile": "chrome146", "payload_bytes": len(b"scrapanium-websocket-benchmark"), "verify": True, "timing": "warm round trips, excluding handshake, startup and close",
        "caveats": ["Loopback Python peer; not Internet performance.", "Bend IO.now has millisecond resolution.", "Bend buffers/discards each received message; Python also compares payload bytes.", "Only Scrapanium additionally verifies the WebSocket acceptance challenge; setup is untimed."],
        "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources},
        "clients": {name: {"times_ms": times, "median_ms": statistics.median(times), "round_trips_per_second": 1000000 / statistics.median(times)} for name, times in values.items()}}
    (ROOT / "benchmarks/results/websocket.json").write_text(json.dumps(report, indent=2) + "\n")
    rows = "\n".join(f"| {name} | {d['median_ms']:.2f} | {d['round_trips_per_second']:.0f} |" for name, d in report['clients'].items())
    (ROOT / "benchmarks/WEBSOCKET.md").write_text("# WebSocket benchmark\n\n" + report['utc'] + " · Linux/WSL2 · 5 shuffled runs × 1,000 warm WSS round trips.\n\n"
        f"Chrome 146, same curl-impersonate 2.2.3, verified loopback TLS, {report['payload_bytes']}-byte binary payload. Startup, TLS/HTTP upgrade and close excluded.\n\n"
        "| Client | Median ms | Round trips/s |\n| --- | ---: | ---: |\n" + rows + "\n\n"
        "These results cover one small-message loopback workload. They do not establish WAN throughput or concurrent-connection scaling. Bend uses millisecond timing; Python additionally compares each returned payload. Correctness is tested separately with exact bytes, fragmentation and large frames.\n\n"
        "[Raw runs, source hashes and pins](results/websocket.json). Reproduce with `.deps/venv-matched/bin/python benchmarks/websocket.py`.\n")


if __name__ == "__main__": main()
