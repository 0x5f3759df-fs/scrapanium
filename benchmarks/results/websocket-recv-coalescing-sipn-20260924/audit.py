#!/usr/bin/env python3
"""Verify and summarize the retained fixed WebSocket receive-coalescing run."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import statistics

HERE = Path(__file__).resolve().parent
EXPECTED_HASHES = {
    "manifest.json": "b5cd640a8122b4e6fa5196d8cc78be04f930fbe23eec085720cbdd1a8ae0851a",
    "samples.jsonl": "ae465c551d353a5d11aa6e201363a4dcc8dd0768443e9b2537266a82694a02c8",
}
BACKENDS = ("baseline", "candidate")
CLIENTS = ("scrapanium-bend", "curl_cffi-matched")
CONDITIONS = ("baseline-bend", "baseline-curl-cffi", "candidate-bend", "candidate-curl-cffi")


def require(ok: bool, message: str) -> None:
    if not ok:
        raise SystemExit("AUDIT FAILED: " + message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def interval(values: list[float], seed: int) -> list[float]:
    """Same 10,000-draw repeat-level median bootstrap and percentile indices as the runner."""
    rng = random.Random(seed)
    medians = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(10_000))
    return [medians[249], medians[9749]]


manifest_path = HERE / "manifest.json"
samples_path = HERE / "samples.jsonl"
for name, expected in EXPECTED_HASHES.items():
    require(digest(HERE / name) == expected, f"{name} hash changed")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
rows = [json.loads(line) for line in samples_path.read_text(encoding="utf-8").splitlines()]
require(manifest["status"] == "complete" and manifest["attempts_completed"] == 488
        and manifest["attempts_failed"] == 0 and manifest["hashes_verified_after_run"],
        "manifest completion/hash flags")
negative_plan = manifest["negative_plan"]
positive_plan = manifest["positive_plan"]
require(len(rows) == 488 and len(negative_plan) == 8 and len(positive_plan) == 480,
        "expected 8 controls plus 480 positive rows")
require(manifest["execution_order_ids"] == [row["plan"]["run_id"] for row in rows],
        "execution IDs do not match retained row order")
require([plan["run_id"] for plan in negative_plan] == [row["plan"]["run_id"] for row in rows[:8]],
        "negative plan/order mismatch")
require([plan["run_id"] for plan in positive_plan] == [row["plan"]["run_id"] for row in rows[8:]],
        "positive plan/order mismatch")
require(all(plan == row["plan"] for plan, row in zip(negative_plan, rows[:8]))
        and all(plan == row["plan"] for plan, row in zip(positive_plan, rows[8:])),
        "retained row metadata differs from plan")
require(all(row["status"] == "passed" for row in rows), "a retained attempt did not pass")
expected_negative = {(backend, client, fault) for backend in BACKENDS
                     for client in CLIENTS for fault in ("corrupt", "swap")}
actual_negative = {(plan["backend"], plan["client"], plan["fault"]) for plan in negative_plan}
require(actual_negative == expected_negative, "negative-control matrix")
for row in rows[:8]:
    plan = row["plan"]
    result = row["result"]["client_result"]
    require(result["detected_by_exact_check"] and result["returncode"] != 0
            and result["fault"] == plan["fault"], f"negative control failed: {plan['run_id']}")
    require(result["mapped_backend"]["sha256"]
            == manifest["backend_provenance"][plan["backend"]]["library_sha256"],
            f"negative backend map: {plan['run_id']}")

workloads = {workload["name"]: workload for workload in manifest["plan_counts"]["workloads"]}
require(len(workloads) == 6 and set(manifest["summary"]["workloads"]) == set(workloads),
        "expected six workloads")
condition_ids = {condition["id"] for condition in manifest["plan_counts"]["conditions"]}
for row in rows[8:]:
    plan = row["plan"]
    result = row["result"]
    client = result["client_result"]
    workload = plan["workload"]
    condition = plan["condition"]
    require(condition["id"] in condition_ids and result["condition"] == condition["id"]
            and result["workload"] == workload["name"] and result["repeat"] == plan["repeat"],
            f"positive plan metadata: {plan['run_id']}")
    require(client["run_id"] == plan["run_id"] and client["elapsed_ms"] > 0
            and client["clock_sanity"]["client_interval_within_parent"],
            f"positive client/timing: {plan['run_id']}")
    expected_bytes = workload["count"] * workload["bytes"]
    require(client["expected_corpus_payload_bytes"] == expected_bytes,
            f"client byte expectation: {plan['run_id']}")
    backend = condition["backend"]
    require(client["mapped_backend"]["sha256"]
            == manifest["backend_provenance"][backend]["library_sha256"],
            f"mapped backend hash: {plan['run_id']}")
    prefix = manifest["backend_provenance"][backend]["prefix"]
    require(client["mapped_backend"]["path"].startswith(prefix + "/lib/libcurl-impersonate.so"),
            f"mapped backend path: {plan['run_id']}")
    peer = result["peer_observation"]
    size = workload["bytes"]
    header_bytes = 2 if size <= 125 else 4 if size <= 65_535 else 10
    require(peer["data_frames"] == workload["count"]
            and peer["data_payload_bytes"] == expected_bytes
            and peer["data_frame_bytes"] == workload["count"] * (size + header_bytes),
            f"peer frame/byte totals: {plan['run_id']}")
    require(peer["starts"] == 1 and peer["warmups"] == 1
            and peer["tls_version"] == 772 and peer["tls_cipher"] == 4865,
            f"peer controls/TLS: {plan['run_id']}")

williams_rows = Counter(tuple(row) for row in manifest["plan_counts"]["williams_rows"])
for name in workloads:
    for condition in CONDITIONS:
        repeats = [plan["repeat"] for plan in positive_plan
                   if plan["workload"]["name"] == name and plan["condition"]["id"] == condition]
        require(sorted(repeats) == list(range(20)), f"repeat coverage {name}/{condition}")
    blocks = [tuple(plan["condition"]["id"] for plan in positive_plan
                    if plan["workload"]["name"] == name and plan["repeat"] == repeat)
              for repeat in range(20)]
    require(all(len(block) == 4 and set(block) == condition_ids for block in blocks),
            f"paired four-condition blocks {name}")
    require(Counter(blocks) == Counter({row: 5 for row in williams_rows}),
            f"Williams row balance {name}")
require(manifest["source_sha256_before"] == manifest["source_sha256_after"]
        and manifest["binary_sha256_before"] == manifest["binary_sha256_after"]
        and manifest["matched_curl_cffi_binding"] == manifest["matched_curl_cffi_binding_after"],
        "source/binary/binding post-run hashes")

paired: dict[str, dict[str, dict[int, float]]] = {
    name: {condition: {} for condition in CONDITIONS} for name in workloads
}
for row in rows[8:]:
    plan = row["plan"]
    paired[plan["workload"]["name"]][plan["condition"]["id"]][plan["repeat"]] = \
        row["result"]["client_result"]["elapsed_ms"]

print("AUDIT PASS: 488/488 retained rows; 8 controls first; 480 positives; 6 workloads × 20 repeats × 4 conditions.")
print("Each workload has five of every planned Williams order; client, peer, TLS, mapping and frozen-hash checks pass.")
print("Ratio definitions: baseline/candidate elapsed (>1 favors candidate); matched curl_cffi/Bend (>1 favors Bend).")
print("95% CIs are paired repeat-level median bootstraps, n=20, 10,000 resamples; seeds 20260924 (backend ratios) and 20260925 (matched clients).")
print("Workload | Bend ms B→C | Bend ratio [95% CI] | curl ms B→C | curl ratio [95% CI] | baseline curl/Bend [95% CI] | candidate curl/Bend [95% CI]")
for name in sorted(workloads):
    group = paired[name]
    repeats = range(20)
    bend = [group["baseline-bend"][r] / group["candidate-bend"][r] for r in repeats]
    curl = [group["baseline-curl-cffi"][r] / group["candidate-curl-cffi"][r] for r in repeats]
    matched_baseline = [group["baseline-curl-cffi"][r] / group["baseline-bend"][r] for r in repeats]
    matched_candidate = [group["candidate-curl-cffi"][r] / group["candidate-bend"][r] for r in repeats]
    bend_ci, curl_ci = interval(bend, 20260924), interval(curl, 20260924)
    matched_baseline_ci = interval(matched_baseline, 20260925)
    matched_candidate_ci = interval(matched_candidate, 20260925)
    stored = manifest["summary"]["workloads"][name]
    checks = ((bend, bend_ci, stored["paired_speed_ratios"]["scrapanium-bend"]),
              (curl, curl_ci, stored["paired_speed_ratios"]["curl_cffi-matched"]),
              (matched_baseline, matched_baseline_ci, stored["matched_client_speed_ratios"]["baseline"]),
              (matched_candidate, matched_candidate_ci, stored["matched_client_speed_ratios"]["candidate"]))
    for values, ci, summary in checks:
        require(abs(statistics.median(values) - summary["median"]) < 1e-12
                and ci == summary["bootstrap_95_ci"], f"stored interval mismatch {name}")
    bend_before = statistics.median(group["baseline-bend"][r] for r in repeats)
    bend_after = statistics.median(group["candidate-bend"][r] for r in repeats)
    curl_before = statistics.median(group["baseline-curl-cffi"][r] for r in repeats)
    curl_after = statistics.median(group["candidate-curl-cffi"][r] for r in repeats)
    print(f"{name} | {bend_before:.3f}→{bend_after:.3f} | {statistics.median(bend):.3f} [{bend_ci[0]:.3f},{bend_ci[1]:.3f}] | "
          f"{curl_before:.3f}→{curl_after:.3f} | {statistics.median(curl):.3f} [{curl_ci[0]:.3f},{curl_ci[1]:.3f}] | "
          f"{statistics.median(matched_baseline):.3f} [{matched_baseline_ci[0]:.3f},{matched_baseline_ci[1]:.3f}] | "
          f"{statistics.median(matched_candidate):.3f} [{matched_candidate_ci[0]:.3f},{matched_candidate_ci[1]:.3f}]")
    if name.startswith("stream-65536b-"):
        print(f"Primary 64KiB Bend gate {name}: "
              f"{statistics.median(bend) >= 1.05 and bend_ci[0] > 1.0}")
    else:
        print(f"Small/medium Bend regression gate {name}: {bend_ci[0] >= 0.95}")
print("raw_sha256 manifest=" + EXPECTED_HASHES["manifest.json"] + " samples=" + EXPECTED_HASHES["samples.jsonl"])
