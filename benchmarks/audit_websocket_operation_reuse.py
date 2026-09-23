#!/usr/bin/env python3
"""Audit retained operation-state reuse streaming reports; never runs a benchmark."""
import argparse
from collections import Counter
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import statistics
import sys


RESULTS = Path(__file__).resolve().parent / "results"
CLIENTS = ("baseline", "candidate", "curl_cffi")
EXPECTED_WORKLOADS = {
    "stream-30b-flush1": (30, 65536, 1),
    "stream-1024b-flush1": (1024, 16384, 1),
    "stream-65536b-flush1": (65536, 1024, 1),
    "stream-30b-flush64": (30, 65536, 64),
    "stream-1024b-flush64": (1024, 16384, 64),
    "stream-65536b-flush64": (65536, 1024, 64),
}
EXPECTED_REF = "0704d51c2275f26cd2580c95d7bb67f0655b1554"
EXPECTED_CANDIDATE_SHA = "0bc6766607ecfad0594d4a2df85b8247da678f4ab1cc0c9fca09504307082fb5"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_provenance(manifest):
    require(manifest["ref"] == EXPECTED_REF, "unexpected published baseline ref")
    require(manifest["reviewed_changes"] == ["native/websocket.inc.c"],
            "candidate must change only native/websocket.inc.c")
    require(manifest["candidate_source_sha256"]["native/websocket.inc.c"] == EXPECTED_CANDIDATE_SHA,
            "candidate source hash does not identify operation-state reuse v2")
    require(manifest["baseline_source_sha256"] == manifest["baseline_git_blob_sha256"],
            "baseline source hashes differ from the recorded Git blobs")
    baseline = manifest["baseline_source_sha256"]
    candidate = manifest["candidate_source_sha256"]
    require(set(baseline) == set(candidate), "baseline and candidate source-hash key sets differ")
    differences = {path for path in baseline if baseline[path] != candidate[path]}
    require(differences == set(manifest["reviewed_changes"]) and len(differences) == 1,
            "source hashes do not show exactly the single reviewed source difference")
    require(len(manifest["backend_sha256"]) == 64, "invalid backend SHA-256")


def validate_controls(report):
    expected = {(client, fault) for client in CLIENTS for fault in ("corrupt", "swap")}
    controls = report["negative_controls"]
    actual = {(row["variant"], row["fault"]) for row in controls}
    require(actual == expected and len(controls) == len(expected), "negative-control set is incomplete")
    require(all(row["detected_by_exact_check"] and row["returncode"] != 0 for row in controls),
            "a corruption or swap control was not rejected")


def validate_finite_numbers(value, where="report"):
    if isinstance(value, float):
        require(math.isfinite(value), f"non-finite number in {where}")
    elif isinstance(value, dict):
        for key, child in value.items():
            validate_finite_numbers(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_finite_numbers(child, f"{where}[{index}]")


def validate_schedule(plan, action, runs):
    require(len(plan) == runs * len(EXPECTED_WORKLOADS) * len(CLIENTS),
            "planned schedule is incomplete")
    permutations = set(itertools.permutations(CLIENTS))
    by_workload = {name: [] for name in EXPECTED_WORKLOADS}
    groups = set()
    for offset in range(0, len(plan), len(CLIENTS)):
        triplet = plan[offset:offset + len(CLIENTS)]
        require(len(triplet) == len(CLIENTS), "planned schedule has an incomplete client triplet")
        workload_names = {row["workload"] for row in triplet}
        repeats = {row["repeat"] for row in triplet}
        clients = tuple(row["client"] for row in triplet)
        require(len(workload_names) == 1 and len(repeats) == 1,
                "planned client triplet mixes workloads or repeats")
        workload = next(iter(workload_names))
        repeat = next(iter(repeats))
        require(workload in by_workload and repeat in range(runs),
                "planned schedule contains an unknown workload or repeat")
        require(clients in permutations, "planned client triplet does not contain all three clients once")
        require((workload, repeat) not in groups, "planned schedule repeats a workload/repeat block")
        groups.add((workload, repeat))
        by_workload[workload].append(clients)

    expected_groups = {(name, repeat) for name in EXPECTED_WORKLOADS for repeat in range(runs)}
    require(groups == expected_groups, "planned schedule omits a workload/repeat block")
    if action == "measure":
        expected_counts = Counter({order: 2 for order in permutations})
        for workload, orders in by_workload.items():
            require(Counter(orders) == expected_counts,
                    f"client ordering is unbalanced across six permutations for {workload}")


def validate_report(report, action, runs):
    validate_finite_numbers(report)
    require(report["status"] == "complete", f"{action} report is not complete")
    require(report["action"] == action and report["runs"] == runs, f"unexpected {action} report settings")
    require(report["failures"] == [], f"{action} report contains failures")
    require(report["verified_unchanged_after_samples"], f"{action} sources changed during the run")
    validate_provenance(report["manifest"])
    validate_controls(report)

    require(len(report["workloads"]) == len(EXPECTED_WORKLOADS),
            "workload row count differs from the published matrix")
    workloads = {row["name"]: row for row in report["workloads"]}
    require(set(workloads) == set(EXPECTED_WORKLOADS), "workload set differs from the published matrix")
    for name, shape in EXPECTED_WORKLOADS.items():
        row = workloads[name]
        require((row["bytes"], row["count"], row["peer_flush_frames"]) == shape,
                f"unexpected workload shape: {name}")

    plan = report["planned_schedule"]
    planned_ids = [f"{row['workload']}-{row['repeat']}-{row['client']}" for row in plan]
    validate_schedule(plan, action, runs)
    samples = report["samples"]
    require(len(samples) == runs * len(EXPECTED_WORKLOADS) * len(CLIENTS),
            f"{action} sample count is incomplete")
    require(report["execution_order"] == planned_ids, "execution order differs from the planned schedule")
    require([row["run_id"] for row in samples] == planned_ids, "samples do not follow the planned schedule")
    require(len(set(planned_ids)) == len(planned_ids), "duplicate run id in schedule")
    for planned, sample in zip(plan, samples):
        planned_key = (planned["workload"], planned["client"], planned["repeat"])
        sample_key = (sample["workload"], sample["client"], sample["repeat"])
        require(sample_key == planned_key,
                f"sample metadata differs from planned schedule for {sample['run_id']}")
        expected_id = f"{sample['workload']}-{sample['repeat']}-{sample['client']}"
        require(sample["run_id"] == expected_id,
                f"sample metadata does not match run id {sample['run_id']}")

    by_key = {}
    for sample in samples:
        key = (sample["workload"], sample["client"], sample["repeat"])
        require(key not in by_key, f"duplicate paired sample: {key}")
        by_key[key] = sample
        require(sample["mapped_backend"]["sha256"] == report["manifest"]["backend_sha256"],
                f"backend identity differs for {sample['run_id']}")
        require(sample["clock_sanity"]["client_interval_within_parent"],
                f"client interval failed parent-clock bounds: {sample['run_id']}")
        workload = workloads[sample["workload"]]
        size, count = workload["bytes"], workload["count"]
        header_bytes = 2 if size < 126 else 4 if size < 65536 else 10
        peer = sample["peer_observation"]
        require(peer["data_frames"] == count, f"peer frame count differs for {sample['run_id']}")
        require(peer["data_payload_bytes"] == count * size,
                f"peer payload byte count differs for {sample['run_id']}")
        require(peer["data_frame_bytes"] == count * (size + header_bytes),
                f"peer frame byte count differs for {sample['run_id']}")
        require(not peer.get("error"), f"peer reported an error for {sample['run_id']}")
        require(peer["warmups"] == 1 and peer["starts"] == 1,
                f"peer warmup/start count differs for {sample['run_id']}")
        require((peer["tls_version"], peer["tls_cipher"]) == (772, 4865),
                f"peer TLS identity differs for {sample['run_id']}")
        require(sample["expected_corpus_payload_bytes"] == size * count,
                f"expected corpus size differs for {sample['run_id']}")
        elapsed = sample["elapsed_ms"]
        require(elapsed > 0 and math.isclose(sample["end_ms"] - sample["start_ms"], elapsed,
                                              rel_tol=1e-9, abs_tol=1e-9),
                f"elapsed time does not match timestamps for {sample['run_id']}")
        expected_rate = count * 1000 / elapsed
        require(math.isclose(sample["messages_per_second"], expected_rate, rel_tol=1e-9),
                f"message rate does not match elapsed time for {sample['run_id']}")

    for workload in EXPECTED_WORKLOADS:
        for client in CLIENTS:
            repeats = {repeat for name, who, repeat in by_key if name == workload and who == client}
            require(repeats == set(range(runs)), f"missing paired sample: {workload}/{client}")
    return workloads, by_key


def bootstrap_median_interval(values, seed):
    rng = random.Random(seed)
    medians = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(10000))
    return medians[249], medians[9749]


def ratio_summary(by_key, workload, numerator, denominator, runs, seed):
    values = [by_key[(workload, denominator, repeat)]["elapsed_ms"] /
              by_key[(workload, numerator, repeat)]["elapsed_ms"] for repeat in range(runs)]
    low, high = bootstrap_median_interval(values, seed)
    return statistics.median(values), low, high, values


def validate_probe_samples(report, by_key):
    for key, sample in by_key.items():
        probe = sample.get("probe")
        require(probe is not None, f"instrumentation missing for {sample['run_id']}")
        require(probe["failed"] == 0, f"instrumented receive failed for {sample['run_id']}")
        workload = next(row for row in report["workloads"] if row["name"] == sample["workload"])
        require(probe["returned_bytes"] == workload["bytes"] * workload["count"] + 6,
                f"instrumented byte total differs for {sample['run_id']}")
        successful = probe["recv_calls"] - probe["again"] - probe["failed"]
        histogram = probe["successful_return_histogram"]
        require(sum(histogram) == successful, f"receive histogram total differs for {sample['run_id']}")
        require(sum(histogram[3:]) == 0, f"receive returned more than 16 KiB for {sample['run_id']}")
        expected_request = 131072 if sample["client"] == "curl_cffi" else 16384
        require(probe["max_request"] == expected_request,
                f"receive buffer size differs for {sample['run_id']}")
        if workload["bytes"] == 65536:
            require(successful == 5125, f"unexpected 64 KiB successful receive count for {sample['run_id']}")


def audit(measure, probe, manifest_file):
    measure_workloads, measure_samples = validate_report(measure, "measure", 12)
    _, probe_samples = validate_report(probe, "probe", 1)
    require(measure["manifest_sha256"] == probe["manifest_sha256"], "measure/probe manifest hashes differ")
    require(measure["manifest"] == probe["manifest"], "measure/probe frozen manifests differ")
    require(load(manifest_file) == measure["manifest"], "standalone and embedded manifests differ")
    require(file_sha(manifest_file) == measure["manifest_sha256"], "standalone manifest hash differs")
    validate_probe_samples(probe, probe_samples)

    seed = measure["seed"]
    rows = []
    for name in EXPECTED_WORKLOADS:
        comparisons = [
            ratio_summary(measure_samples, name, "candidate", "baseline", 12, seed),
            ratio_summary(measure_samples, name, "candidate", "curl_cffi", 12, seed),
            ratio_summary(measure_samples, name, "baseline", "curl_cffi", 12, seed),
        ]
        stored = measure_workloads[name]
        for label, computed in zip(("candidate_vs_baseline", "candidate_vs_cffi"), comparisons):
            saved = stored[label]
            require(len(saved["samples"]) == 12,
                    f"saved {label} paired-ratio count differs for {name}")
            require(len(saved["bootstrap_95_ci"]) == 2,
                    f"saved {label} interval must have exactly two bounds for {name}")
            require(math.isclose(saved["median"], computed[0], rel_tol=1e-12),
                    f"stored {label} median differs for {name}")
            require(all(math.isclose(a, b, rel_tol=1e-12) for a, b in
                        zip(saved["bootstrap_95_ci"], computed[1:3])),
                    f"stored {label} interval differs for {name}")
            require(all(math.isclose(a, b, rel_tol=1e-12) for a, b in zip(saved["samples"], computed[3])),
                    f"stored {label} paired ratios differ for {name}")
        rows.append((name, comparisons))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measure", type=Path, default=RESULTS / "websocket-operation-reuse.json")
    parser.add_argument("--probe", type=Path, default=RESULTS / "websocket-operation-reuse-probe.json")
    parser.add_argument("--manifest", type=Path, default=RESULTS / "websocket-operation-reuse-manifest.json")
    args = parser.parse_args()
    try:
        measure, probe = load(args.measure), load(args.probe)
        rows = audit(measure, probe, args.manifest)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"audit failed: {error}", file=sys.stderr)
        return 1

    print(f"measure SHA-256: {file_sha(args.measure)}")
    print(f"probe SHA-256:   {file_sha(args.probe)}")
    print(f"manifest SHA-256: {measure['manifest_sha256']}")
    print("Ratios are paired throughput A/B; >1 means A completed more messages per second.")
    print("| Payload | Frames / write | Candidate / baseline | Candidate / curl_cffi | Baseline / curl_cffi |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for name, comparisons in rows:
        payload, frames = name.removeprefix("stream-").split("-flush")
        payload = {"30b": "30 B", "1024b": "1 KiB", "65536b": "64 KiB"}[payload]
        cells = [f"{median:.3f}× ({low:.3f}–{high:.3f}×)" for median, low, high, _ in comparisons]
        print(f"| {payload} | {frames} | " + " | ".join(cells) + " |")
    print("Audit passed: schedule, paired sample completeness, peer totals, backend identities, controls, and saved math.")
    print("Probe passed: byte totals, receive histograms, and max buffer sizes; probe timings were excluded.")
    print("This is a report-internal audit. It does not reopen absolute source/binary paths in the manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
