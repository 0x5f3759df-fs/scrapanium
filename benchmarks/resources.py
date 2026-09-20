"""Whole-process Bend peak RSS: buffered versus file-streamed response sizes.

Both modes use the same HTTP/1 loopback source. This is a memory scaling probe,
not a network/disk throughput comparison. Run after tests/other builds finish.
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
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
from build import build
from lab import start_http


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1: parser.error("--runs must be positive")
    binary = build(ROOT / "benchmarks/resources.bend", ROOT / "build/bench-resources")
    server, url = start_http()
    results = []; rng = random.Random(20260920)
    try:
        with tempfile.TemporaryDirectory(prefix="scrapanium-memory-") as directory:
            directory = Path(directory)
            cases = [(size, mode) for size in (65536, 1048576, 16777216, 67108864) for mode in ("buffer", "download")]
            for repeat in range(args.runs):
                order = cases[:]; rng.shuffle(order)
                for size, mode in order:
                    destination = directory / "data.bin"
                    usage = directory / "usage.json"
                    env = {**os.environ, "SCRAPANIUM_URL": url + "/bytes/" + str(size),
                        "SCRAPANIUM_OUTPUT": str(destination), "SCRAPANIUM_MODE": mode}
                    out = subprocess.run(["/usr/bin/time", "-f",
                        '{"peak_rss_kib":%M,"elapsed_s":%e,"user_cpu_s":%U,"system_cpu_s":%S}',
                        "-o", str(usage), str(binary), "--threads", "1"], env=env,
                        capture_output=True, text=True, timeout=60)
                    assert out.returncode == 0, out.stdout + out.stderr
                    assert out.stdout.strip() == str(size)
                    if mode == "download": assert destination.stat().st_size == size
                    results.append({"body_bytes": size, "mode": mode, "repeat": repeat, **json.loads(usage.read_text())})
                print("memory run", repeat + 1, flush=True)
    finally:
        server.shutdown(); server.server_close()
    sources = ["scrapanium.bend", "http.bend", *[str(p.relative_to(ROOT)) for p in (ROOT / "native").glob("*")],
               "benchmarks/resources.bend", "benchmarks/resources.py", "tests/lab.py"]
    data = {"date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "platform": platform.platform(),
        "compiler": subprocess.check_output(["clang", "--version"], text=True).splitlines()[0],
        "dependencies": json.loads((ROOT / "dependencies.json").read_text()), "runs": args.runs,
        "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources},
        "limitations": ["whole-process peak RSS, not allocator accounting", "HTTP/1 loopback only",
            "file writes use a Linux temporary directory, no fsync", "no curl_cffi or Go comparison in this probe",
            "buffering and writing have different output semantics; elapsed time is not a speed comparison"], "results": results}
    output = ROOT / "benchmarks/results/resources.json"
    output.parent.mkdir(exist_ok=True); output.write_text(json.dumps(data, indent=2) + "\n")
    lines = ["# Bend response memory scaling", "", "Recorded " + data["date_utc"] + ".",
        "", f"Median whole-process peak RSS over {args.runs} shuffled runs. Linux/WSL2, one Bend compute thread.",
        "Both modes consume identical HTTP/1 response bytes; download writes into a Linux temporary directory.",
        "", "| Decoded body | Buffered RSS | Download RSS |", "| --- | ---: | ---: |"]
    for size in sorted({r["body_bytes"] for r in results}):
        values = [statistics.median(r["peak_rss_kib"] for r in results if r["body_bytes"] == size and r["mode"] == m) / 1024
                  for m in ("buffer", "download")]
        lines.append(f"| {size / 1048576:g} MiB | {values[0]:.1f} MiB | {values[1]:.1f} MiB |")
    lines += ["", "This checks observed memory scaling; it does not establish a process-wide memory bound.",
        "Allocation accounting excludes backend internals and the Bend heap. Disk writing and buffering",
        "produce different outputs, so this probe makes no throughput comparison.", "",
        "Raw samples, versions and source hashes: [resources.json](results/resources.json).", "",
        "```sh", ".deps/venv-matched/bin/python benchmarks/resources.py --runs 3", "```", ""]
    (ROOT / "benchmarks/MEMORY.md").write_text("\n".join(lines))
    print(output)


if __name__ == "__main__": main()
