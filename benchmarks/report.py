"""Render recorded evidence without inventing missing measurements."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "benchmarks/results/local.json"
data = json.loads(source.read_text())
clients = list(dict.fromkeys(r["client"] for r in data["results"]))
workloads = list(dict.fromkeys(r["workload"] for r in data["results"]))
indexed = {(r["workload"], r["client"]): r for r in data["results"]}
lines = ["# Local benchmark results", "", f"Recorded {data['date_utc']}. CPU: {data['cpu']}.",
    f"Platform: {data['platform']}. {data['runs']} shuffled runs per case; medians below.", "",
    "Values are requests/second, higher is better. All clients use persistent sessions,",
    "one warmup request, Chrome 146 TLS profiles, common headers and certificate verification.",
    "Bend runs with one compute thread; batches permit 16 concurrent requests.", "",
    "| Workload | " + " | ".join(clients) + " |",
    "| --- | " + " | ".join(["---:"] * len(clients)) + " |"]
for workload in workloads:
    lines.append("| " + workload + " | " + " | ".join(f"{indexed[workload, c]['requests_per_second']:,.0f}" for c in clients) + " |")
lines += ["", "## Bend versus the same-backend curl_cffi", "",
          "Ratios compare throughput, not single-request latency.", "", "| Workload | Ratio |", "| --- | ---: |"]
for workload in workloads:
    ratio = indexed[workload, "scrapanium-bend"]["requests_per_second"] / indexed[workload, "curl_cffi-matched"]["requests_per_second"]
    lines.append(f"| {workload} | {ratio:.2f}x |")
lines += ["", "## Whole-process peak memory", "",
          "Median peak resident memory in MiB. Includes startup, imports, warmup and teardown.", "",
          "| Workload | " + " | ".join(clients) + " |",
          "| --- | " + " | ".join(["---:"] * len(clients)) + " |"]
for workload in workloads:
    lines.append("| " + workload + " | " + " | ".join(f"{indexed[workload, c]['median_peak_rss_kib'] / 1024:.1f}" for c in clients) + " |")
lines += ["", "## Interpretation and limits", ""]
lines += ["- This run supersedes the initial alpha measurements: the Bend configuration bridge now decodes flattened records and verification flags correctly."]
lines += ["- " + item + "." for item in data["limitations"]]
lines += ["- Local Python fixtures can cap throughput; these results do not establish Internet performance.",
          "- HTTP/1 cleartext upgrade behavior differs between curl and Go; the wire streams are not identical.",
          "- TLS-client has a different transport and feature/copying model; its values are a separate implementation comparison.",
          "- No p95/p99 per-request latency or cold-handshake claim is supported by this run.",
          "- Whole-process CPU seconds and peak RSS are recorded; they do not isolate transport CPU or buffer memory.",
          "- Source and raw min/max/repeated timings are retained in [local.json](results/local.json).", "",
          "## Versions", "", "```text", "Bend 2.0.17 (see dependencies.json)"]
for label, version in data["baseline_versions"].items(): lines += [f"curl_cffi {label}: {version}"]
if data.get("go_version"): lines += [data["go_version"], "tls-client: " + data["tls_client_commit"]]
lines += ["```", "", "Reproduce after completing tests and other builds:", "", "```sh",
    ".deps/venv-matched/bin/python benchmarks/run.py --runs 5 --count 1000 --tls-client",
    "python3 benchmarks/report.py", "```", ""]
output = ROOT / "benchmarks/RESULTS.md"
output.write_text("\n".join(lines))
print(output)
