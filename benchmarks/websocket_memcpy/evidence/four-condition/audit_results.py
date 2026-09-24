#!/usr/bin/env python3
"""Independently validate and summarize a retained four-condition WSS run.

The verifier reads only manifest.json and samples.jsonl. It checks the fixed
schedule and raw row invariants, recomputes paired ratios and the declared
repeat-level bootstrap intervals, and writes a machine-readable verdict.
It does not build clients, start a peer, or collect measurements.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import statistics
from collections import Counter, defaultdict

SCHEMA = "scrapanium-wss-memcpy-full-audit-v1"
CLIENT_SOURCE_REV = "411981c05167fa54065fa3d8757bac3c734b19f6"
BASELINE_SHA256 = "19a3732717119c67002204579881379c0d89beb0ae080cce38a4cc52fd9bbd8b"
CANDIDATE_SHA256 = "c15ed19b2510ea89e94df68e64146612bda86733414706b367f186250ecd0621"
TLS = (772, 4865)
SEED = 20260924
BOOTSTRAPS = 10000
CONDITIONS = {
    "baseline-bend": ("baseline", "scrapanium-bend"),
    "baseline-curl-cffi": ("baseline", "curl_cffi-matched"),
    "candidate-bend": ("candidate", "scrapanium-bend"),
    "candidate-curl-cffi": ("candidate", "curl_cffi-matched"),
}
EXPECTED_WORKLOADS = {
    "stream-30b-flush1": (30, 65536, 1),
    "stream-1024b-flush1": (1024, 16384, 1),
    "stream-65536b-flush1": (65536, 1024, 1),
    "stream-30b-flush64": (30, 65536, 64),
    "stream-1024b-flush64": (1024, 16384, 64),
    "stream-65536b-flush64": (65536, 1024, 64),
}
EXPECTED_NEGATIVES = set(itertools.product(
    ("baseline", "candidate"),
    ("scrapanium-bend", "curl_cffi-matched"),
    ("corrupt", "swap"),
))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def bootstrap_median_ci(values: list[float], seed: int) -> list[float]:
    """Match the runner: 10k size-n resamples, sorted median draws [249,9749]."""
    require(bool(values) and all(math.isfinite(value) for value in values),
            "bootstrap input is empty or non-finite")
    rng = random.Random(seed)
    medians = sorted(
        statistics.median(rng.choices(values, k=len(values)))
        for _ in range(BOOTSTRAPS)
    )
    return [medians[249], medians[9749]]


def header_size(payload_bytes: int) -> int:
    return 2 if payload_bytes < 126 else 4 if payload_bytes < 65536 else 10


def read_rows(samples_dir: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((samples_dir / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            (samples_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return manifest, rows


def verify_manifest(manifest: dict, rows: list[dict]) -> dict:
    require(manifest.get("schema") == 1, "unexpected manifest schema")
    require(manifest.get("status") == "complete" and manifest.get("smoke_only") is False,
            "input is not a completed full run")
    require(manifest.get("timings_are_performance_evidence") is True,
            "full-run manifest does not identify its timing output")
    require(manifest.get("client_source_revision") == CLIENT_SOURCE_REV,
            "client source revision differs from the frozen plan")
    require(manifest.get("seed") == SEED and manifest.get("bootstrap_resamples") == BOOTSTRAPS,
            "fixed seed/bootstrap count differs")
    require(manifest.get("hashes_verified_after_run") is True,
            "post-run hash validation did not complete")
    require(manifest.get("attempts_completed") == 488 and manifest.get("attempts_failed") == 0,
            "attempt completion/failure counts differ")
    require(manifest.get("peer_exit_code") == 0, "stream peer did not exit successfully")
    counts = manifest.get("plan_counts", {})
    require(counts.get("negative_controls") == 8 and
            counts.get("positive_measurements") == 480 and
            counts.get("total_attempts") == 488 and
            counts.get("repeats_per_workload") == 20 and
            counts.get("adaptive_extension") is False,
            "the declared fixed schedule differs from 8 + 480 rows")
    require(manifest.get("environment", {}).get("LD_PRELOAD_present") is False and
            manifest.get("environment", {}).get("threads") == 1 and
            manifest.get("environment", {}).get("profile") == "chrome146" and
            manifest.get("environment", {}).get("TLS") == "WSS",
            "runtime environment does not match the declared client conditions")
    require(len(rows) == 488 and all(row.get("status") == "passed" for row in rows),
            "JSONL does not contain 488 passed attempts")

    negative_plan = manifest.get("negative_plan", [])
    positive_plan = manifest.get("positive_plan", [])
    require(len(negative_plan) == 8 and len(positive_plan) == 480,
            "retained plan row counts differ")
    plan = [*negative_plan, *positive_plan]
    ids = [row.get("plan", {}).get("run_id") for row in rows]
    planned_ids = [item.get("run_id") for item in plan]
    require(ids == planned_ids == manifest.get("execution_order_ids"),
            "retained execution IDs/order differ from the frozen plan")
    require([row.get("attempt_index") for row in rows] == list(range(488)),
            "attempt indices are incomplete or reordered")
    require(all(row.get("plan") == item for row, item in zip(rows, plan)),
            "a retained attempt's plan metadata differs from its planned row")

    neg_cells = Counter((row["plan"].get("backend"), row["plan"].get("client"),
                         row["plan"].get("fault"))
                        for row in rows if row["plan"].get("kind") == "negative")
    require(set(neg_cells) == EXPECTED_NEGATIVES and all(value == 1 for value in neg_cells.values()),
            "negative controls do not cover each backend/client/fault cell exactly once")

    workloads = counts.get("workloads", [])
    observed_workloads = {item.get("name"): (item.get("bytes"), item.get("count"),
                                                 item.get("peer_flush_frames"))
                          for item in workloads}
    require(observed_workloads == EXPECTED_WORKLOADS,
            f"workload corpus differs from fixed matrix: {observed_workloads}")
    conditions = counts.get("conditions", [])
    observed_conditions = {
        item.get("id"): (item.get("backend"), item.get("client")) for item in conditions
    }
    require(observed_conditions == CONDITIONS, "backend/client condition matrix differs")

    williams = counts.get("williams_rows", [])
    require(len(williams) == 4 and all(len(row) == 4 and set(row) == set(CONDITIONS)
                                       for row in williams),
            "retained Williams rows are malformed")
    row_counts = counts.get("row_counts_by_workload", {})
    for name in EXPECTED_WORKLOADS:
        require(row_counts.get(name) == {str(i): 5 for i in range(4)},
                f"{name}: Williams row plan is not 5× each order")

    positive_by_workload_repeat: dict[tuple[str, int], list[str]] = defaultdict(list)
    positives = [row for row in rows if row["plan"].get("kind") == "positive"]
    require(len(positives) == 480, "positive count differs")
    for row in positives:
        item = row["plan"]
        workload = item.get("workload", {})
        condition = item.get("condition", {})
        name, repeat, cid = workload.get("name"), item.get("repeat"), condition.get("id")
        require(name in EXPECTED_WORKLOADS and isinstance(repeat, int) and 0 <= repeat < 20,
                "positive row has invalid workload/repeat")
        require((condition.get("backend"), condition.get("client")) == CONDITIONS.get(cid),
                "positive condition ID and metadata disagree")
        require((workload.get("bytes"), workload.get("count"), workload.get("peer_flush_frames"))
                == EXPECTED_WORKLOADS[name], "positive workload metadata changed")
        positive_by_workload_repeat[(name, repeat)].append(cid)
    require(len(positive_by_workload_repeat) == 120,
            "each workload/repeat must have exactly one Williams block")
    used_orders = {name: Counter() for name in EXPECTED_WORKLOADS}
    for key, order in positive_by_workload_repeat.items():
        name, _repeat = key
        require(len(order) == 4 and len(set(order)) == 4,
                f"{name}: a repeat block is missing or duplicates a condition")
        try:
            order_id = williams.index(order)
        except ValueError as error:
            raise RuntimeError(f"{name}: block is not one of the declared Williams orders: {order}") from error
        used_orders[name][str(order_id)] += 1
    for name, used in used_orders.items():
        require(dict(used) == {str(i): 5 for i in range(4)},
                f"{name}: executed Williams orders differ from the balanced plan: {dict(used)}")

    before_after = (
        ("source_sha256_before", "source_sha256_after"),
        ("toolchain", "toolchain_after"),
        ("matched_curl_cffi_binding", "matched_curl_cffi_binding_after"),
        ("binary_sha256_before", "binary_sha256_after"),
    )
    for before, after in before_after:
        require(manifest.get(before) == manifest.get(after), f"{before} changed during the run")

    client_build = manifest.get("client_build_reuse", {})
    require(client_build.get("same_stream_binary_sha256") is True and
            client_build.get("same_bridge_library_sha256") is True,
            "the same clients/bridge were not reused across the backends")
    client_provenance = manifest.get("client_provenance", {})
    baseline_client = client_provenance.get("baseline", {})
    candidate_client = client_provenance.get("candidate", {})
    stream_hash = baseline_client.get("stream_binary_sha256")
    bridge_hash = baseline_client.get("bridge_library_sha256")
    require(stream_hash and candidate_client.get("stream_binary_sha256") == stream_hash and
            bridge_hash and candidate_client.get("bridge_library_sha256") == bridge_hash and
            baseline_client.get("stream_binary") == candidate_client.get("stream_binary") and
            baseline_client.get("bridge_library") == candidate_client.get("bridge_library"),
            "the two runtime conditions did not reuse byte-identical client binaries")
    backends = manifest.get("backend_provenance", {})
    require(backends.get("baseline", {}).get("library_sha256") == BASELINE_SHA256 and
            backends.get("candidate", {}).get("library_sha256") == CANDIDATE_SHA256,
            "backend pair SHA differs from the correctness gate")
    require(manifest.get("backend_provenance_after", {}).get("baseline", {}).get("library_sha256")
            == BASELINE_SHA256 and
            manifest.get("backend_provenance_after", {}).get("candidate", {}).get("library_sha256")
            == CANDIDATE_SHA256,
            "post-run backend hashes differ")
    require(manifest.get("correctness_gate", {}).get("no_performance_data") is True and
            all(value is True for value in manifest.get("correctness_gate", {}).get("checks", {}).values()),
            "pre-run correctness gate is missing or failed")

    return {
        "negative_plan": negative_plan,
        "positive_plan": positive_plan,
        "rows": rows,
        "workloads": observed_workloads,
        "williams_rows": williams,
        "client_provenance": client_provenance,
        "backend_provenance": backends,
        "schedule_valid": True,
        "source_and_binary_hashes_stable": True,
        "correctness_gate_passed": True,
    }


def verify_rows(data: dict) -> dict:
    rows = data["rows"]
    backends = data["backend_provenance"]
    conditions = defaultdict(lambda: defaultdict(dict))
    tls_seen: set[tuple[int, int]] = set()
    negatives = 0
    positives = 0
    for row in rows:
        plan = row["plan"]
        result = row.get("result", {})
        client_result = result.get("client_result", {})
        peer = result.get("peer_observation", {})
        cid = plan.get("condition", {}).get("id") if plan.get("kind") == "positive" else None
        if plan.get("kind") == "negative":
            negatives += 1
            backend, client, fault = plan["backend"], plan["client"], plan["fault"]
            require(result.get("backend") == backend and result.get("client") == client and
                    result.get("fault") == fault, "negative row metadata differs from its plan")
            require(client_result.get("returncode") not in (None, 0) and
                    client_result.get("detected_by_exact_check") is True and
                    client_result.get("fault") == fault,
                    f"negative control was not rejected by the exact validation path: {plan['run_id']}")
            expected = backends[backend]
            mapped = client_result.get("mapped_backend", {})
            require(mapped.get("sha256") == expected.get("library_sha256") and
                    mapped.get("path") == expected.get("library_path"),
                    f"negative client loaded the wrong backend: {plan['run_id']}")
            frame_count, payload_bytes = 32, 30 * 32
        else:
            positives += 1
            workload = plan["workload"]
            condition = plan["condition"]
            backend, client = condition["backend"], condition["client"]
            require(result.get("condition") == cid and result.get("workload") == workload["name"] and
                    result.get("repeat") == plan["repeat"],
                    f"positive result metadata differs from its plan: {plan['run_id']}")
            expected = backends[backend]
            mapped = client_result.get("mapped_backend", {})
            require(client_result.get("returncode", 0) == 0 and
                    mapped.get("sha256") == expected.get("library_sha256") and
                    mapped.get("path") == expected.get("library_path"),
                    f"positive client failed or loaded wrong backend: {plan['run_id']}")
            size, frame_count, batch = data["workloads"][workload["name"]]
            payload_bytes = size * frame_count
            require(client_result.get("expected_corpus_payload_bytes") == payload_bytes,
                    f"positive client expected-byte count differs: {plan['run_id']}")
            elapsed = client_result.get("elapsed_ms")
            start, end = client_result.get("start_ms"), client_result.get("end_ms")
            require(all(isinstance(x, (float, int)) and math.isfinite(x) for x in (elapsed, start, end))
                    and elapsed > 0 and end >= start and close(float(end) - float(start), float(elapsed)),
                    f"positive client clock interval is invalid: {plan['run_id']}")
            clock = client_result.get("clock_sanity", {})
            require(clock.get("client_interval_within_parent") is True and
                    clock.get("parent_gate_ms") <= start <= end <= clock.get("parent_finish_ms"),
                    f"positive timing escaped the parent gate interval: {plan['run_id']}")
            prior = conditions[workload["name"]][cid]
            repeat = int(plan["repeat"])
            require(repeat not in prior, f"duplicate timing repeat {workload['name']}/{cid}/{repeat}")
            prior[repeat] = float(elapsed)

        require(peer.get("warmups") == 1 and peer.get("starts") == 1,
                f"peer warmup/start counters invalid: {plan['run_id']}")
        require(peer.get("data_frames") == frame_count and
                peer.get("data_payload_bytes") == payload_bytes and
                peer.get("data_frame_bytes") == payload_bytes + frame_count * header_size(
                    30 if plan.get("kind") == "negative" else plan["workload"]["bytes"]),
                f"peer frame/payload totals invalid: {plan['run_id']}")
        tls = (peer.get("tls_version"), peer.get("tls_cipher"))
        require(tls == TLS, f"TLS version/cipher differs: {plan['run_id']}: {tls}")
        tls_seen.add(tls)

    require((negatives, positives, tls_seen) == (8, 480, {TLS}),
            "negative/positive totals or TLS identity differ")
    for workload, condition_map in conditions.items():
        require(set(condition_map) == set(CONDITIONS), f"{workload}: condition set is incomplete")
        for cid, repeats in condition_map.items():
            require(set(repeats) == set(range(20)), f"{workload}/{cid}: repeat set is incomplete")
    return {"negative_controls": negatives, "positive_samples": positives,
            "tls_version": TLS[0], "tls_cipher": TLS[1], "peer_and_clock_rows_valid": True,
            "timings": {workload: dict(condition_map) for workload, condition_map in conditions.items()}}


def paired_summaries(data: dict, verified: dict) -> dict:
    matrix = verified["timings"]
    table = {}
    manifest_summary = data["manifest"].get("summary", {}).get("workloads", {})
    for name, workload in data["workloads"].items():
        per_condition = matrix[name]
        result = {"bytes_per_message": workload[0], "messages_per_run": workload[1],
                  "peer_flush_frames": workload[2], "condition_median_elapsed_ms": {},
                  "backend_speed_ratios": {}, "matched_client_speed_ratios": {}}
        for cid in CONDITIONS:
            elapsed = list(per_condition[cid].values())
            result["condition_median_elapsed_ms"][cid] = statistics.median(elapsed)
        for client, baseline_id, candidate_id in (
                ("scrapanium-bend", "baseline-bend", "candidate-bend"),
                ("curl_cffi-matched", "baseline-curl-cffi", "candidate-curl-cffi")):
            ratios = [per_condition[baseline_id][repeat] / per_condition[candidate_id][repeat]
                      for repeat in range(20)]
            ci = bootstrap_median_ci(ratios, SEED)
            measured = {
                "definition": "baseline elapsed / candidate elapsed; >1 favors candidate",
                "paired_repeat_count": len(ratios),
                "median_paired_ratio": statistics.median(ratios),
                "nominal_95_percentile_ci": ci,
                "ratio_values": ratios,
                "bootstrap_seed": SEED,
                "bootstrap_resamples": BOOTSTRAPS,
            }
            stored = manifest_summary[name]["paired_speed_ratios"][client]
            require(close(measured["median_paired_ratio"], float(stored["median"])) and
                    all(close(measured["nominal_95_percentile_ci"][i], float(stored["bootstrap_95_ci"][i]))
                        for i in range(2)),
                    f"{name}/{client}: independent paired bootstrap differs from the runner")
            result["backend_speed_ratios"][client] = measured

        for backend, bend_id, curl_id in (
                ("baseline", "baseline-bend", "baseline-curl-cffi"),
                ("candidate", "candidate-bend", "candidate-curl-cffi")):
            ratios = [per_condition[curl_id][repeat] / per_condition[bend_id][repeat]
                      for repeat in range(20)]
            ci = bootstrap_median_ci(ratios, SEED + 1)
            measured = {
                "definition": "curl_cffi elapsed / Bend elapsed; >1 favors Bend",
                "paired_repeat_count": len(ratios),
                "median_paired_ratio": statistics.median(ratios),
                "nominal_95_percentile_ci": ci,
                "ratio_values": ratios,
                "bootstrap_seed": SEED + 1,
                "bootstrap_resamples": BOOTSTRAPS,
            }
            stored = manifest_summary[name]["matched_client_speed_ratios"][backend]
            require(close(measured["median_paired_ratio"], float(stored["median"])) and
                    all(close(measured["nominal_95_percentile_ci"][i], float(stored["bootstrap_95_ci"][i]))
                        for i in range(2)),
                    f"{name}/{backend}: independent matched-client bootstrap differs from runner")
            result["matched_client_speed_ratios"][backend] = measured
        table[name] = result
    return table


def make_verdict(samples_dir: Path) -> dict:
    manifest, rows = read_rows(samples_dir)
    data = verify_manifest(manifest, rows)
    data["manifest"] = manifest
    data["workloads"] = data["workloads"]
    checked = verify_rows(data)
    table = paired_summaries(data, checked)
    primary = {}
    small_medium = {}
    for name, row in table.items():
        size = row["bytes_per_message"]
        if size == 65536:
            measure = row["backend_speed_ratios"]["scrapanium-bend"]
            median = measure["median_paired_ratio"]
            lower = measure["nominal_95_percentile_ci"][0]
            primary[name] = {
                "median_paired_speed_ratio": median,
                "nominal_95_percentile_ci": measure["nominal_95_percentile_ci"],
                "required_median_at_least": 1.05,
                "required_ci_lower_above": 1.0,
                "passed": median >= 1.05 and lower > 1.0,
            }
        elif size in (30, 1024):
            measure = row["backend_speed_ratios"]["scrapanium-bend"]
            lower = measure["nominal_95_percentile_ci"][0]
            small_medium[name] = {
                "median_paired_speed_ratio": measure["median_paired_ratio"],
                "nominal_95_percentile_ci": measure["nominal_95_percentile_ci"],
                "required_ci_lower_at_least": 0.95,
                "passed": lower >= 0.95,
            }
    require(len(primary) == 2 and len(small_medium) == 4,
            "promotion gate did not cover two 64 KiB and four smaller Bend cells")
    primary_passed = all(row["passed"] for row in primary.values())
    small_passed = all(row["passed"] for row in small_medium.values())
    return {
        "schema": SCHEMA,
        "run_status": manifest["status"],
        "source_revision": manifest["client_source_revision"],
        "main_repo_head": manifest.get("main_repo_head"),
        "seed": SEED,
        "attempts": {"total": 488, "negatives": checked["negative_controls"],
                     "positives": checked["positive_samples"], "failed": 0},
        "schedule_audit": {
            "williams_rows": data["williams_rows"],
            "repeats_per_workload": 20,
            "uses_each_williams_row_five_times_per_workload": True,
            "plan_and_execution_ids_match": True,
            "adaptive_extension": False,
        },
        "integrity_audit": {
            "source_toolchain_binding_binary_before_after_match": data["source_and_binary_hashes_stable"],
            "same_stream_binary_sha256_across_backends": data["client_provenance"]["baseline"]["stream_binary_sha256"],
            "same_bridge_sha256_across_backends": data["client_provenance"]["baseline"]["bridge_library_sha256"],
            "baseline_dso_sha256": BASELINE_SHA256,
            "candidate_dso_sha256": CANDIDATE_SHA256,
            "mapped_backend_peer_tls_clock_rows_valid": checked["peer_and_clock_rows_valid"],
            "tls_version": checked["tls_version"], "tls_cipher": checked["tls_cipher"],
        },
        "inference_method": {
            "primary_unit": "paired repeat-level ratios within workload and client/backend cell",
            "backend_speed_ratio": "baseline elapsed / candidate elapsed; >1 favors candidate",
            "matched_client_ratio": "curl_cffi elapsed / Bend elapsed; >1 favors Bend",
            "interval": "nominal pointwise 95% percentile bootstrap CI for the median paired ratio; 10,000 size-20 resamples with replacement; sorted median draws at indices 249 and 9749",
            "backend_ratio_seed": SEED,
            "matched_client_ratio_seed": SEED + 1,
            "bootstrap_resamples": BOOTSTRAPS,
            "smoke_or_profiler_data_used": False,
        },
        "all_workloads": table,
        "promotion_gates": {
            "primary_64k_bend_cells": primary,
            "primary_64k_all_pass": primary_passed,
            "small_medium_bend_cells": small_medium,
            "small_medium_all_pass": small_passed,
            "promote_candidate": primary_passed and small_passed,
            "decision": "PROMOTE" if primary_passed and small_passed else "REJECT FOR ADOPTION",
            "interpretation": (
                "Both 64 KiB Bend cells must clear the predeclared median and CI thresholds; smaller-cell intervals are a regression screen."
            ),
        },
        "raw_inputs": {
            "manifest_sha256": sha256(samples_dir / "manifest.json"),
            "samples_jsonl_sha256": sha256(samples_dir / "samples.jsonl"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples-dir", type=Path, required=True,
                        help="directory containing the retained manifest.json and samples.jsonl")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).with_name("verdict.json"))
    args = parser.parse_args()
    report = make_verdict(args.samples_dir.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "result": "PASS",
        "decision": report["promotion_gates"]["decision"],
        "attempts": report["attempts"],
        "primary_64k": report["promotion_gates"]["primary_64k_bend_cells"],
        "small_medium_all_pass": report["promotion_gates"]["small_medium_all_pass"],
        "output": str(args.output.resolve()),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
