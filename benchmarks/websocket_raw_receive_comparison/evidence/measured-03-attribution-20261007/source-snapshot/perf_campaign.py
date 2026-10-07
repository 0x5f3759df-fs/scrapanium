#!/usr/bin/env python3
"""Fixed 72-row profile-only WSS capture schedule; no retries or extensions."""
import argparse
import datetime
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import runner

CAPTURE = ROOT / "perf_window_smoke.py"
REPEATS = 12
SAMPLE_FLOOR = 100
CELLS = tuple((client, batch) for client in runner.CLIENTS for batch in runner.BATCHES)
TOTAL = len(CELLS) * REPEATS


def schedule_for_seed(seed):
    rng = random.Random(seed)
    base = list(range(len(CELLS)))
    rng.shuffle(base)
    rows = [base[offset:] + base[:offset] for offset in range(len(CELLS))]
    rows *= REPEATS // len(CELLS)
    rng.shuffle(rows)
    positions = [[0] * len(CELLS) for _ in CELLS]
    for row in rows:
        if sorted(row) != list(range(len(CELLS))):
            raise RuntimeError("profile schedule row is not a permutation")
        for position, cell in enumerate(row):
            positions[cell][position] += 1
    if any(count != 2 for row in positions for count in row):
        raise RuntimeError("fixed rotations do not balance all six cell positions twice")
    return rows


def build_paths(directory):
    bend = {name: directory / f"bench-ws-{name}" for name in runner.BEND_CLIENTS}
    generated = {name: path.with_suffix(".generated.c") for name, path in bend.items()}
    return {
        "candidate_bend": bend["candidate-bend"],
        "candidate_generated_c": generated["candidate-bend"],
        "baseline_bend": bend["baseline-bend"],
        "baseline_generated_c": generated["baseline-bend"],
        "normal_shim": directory / "libwsattribution.so",
        "perf_control_shim": directory / "libwsperf-control.so",
        "peer": directory / "websocket-attribution-peer",
    }


def verify_build(directory, expected_sources=None):
    manifest_path = directory / "build-manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"shared build manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = build_paths(directory)
    if set(manifest.get("binary_sha256", {})) != set(paths):
        raise RuntimeError("shared build manifest has an incomplete binary list")
    actual = {}
    for name, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"shared build artifact is missing: {path}")
        actual[name] = runner.phase.digest(path)
    if actual != manifest["binary_sha256"]:
        raise RuntimeError("shared build artifacts do not match their frozen hashes")
    if not isinstance(manifest.get("source_sha256"), dict) or not isinstance(
        manifest.get("toolchain_inputs"), dict
    ):
        raise RuntimeError("shared build manifest omits source/toolchain provenance")
    if expected_sources is not None and manifest["source_sha256"] != expected_sources:
        raise RuntimeError("shared build was produced from different diagnostic sources")
    return runner.phase.digest(manifest_path)


def save_exclusive(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def append_row(stream, value):
    stream.write(json.dumps(value, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def slug(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")


def stop_process_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def run_capture(command, env, timeout=300):
    process = subprocess.Popen(
        command, env=env, text=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        stop_process_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stop_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=10)
        return None, stdout or "", stderr or "", True
    except BaseException:
        stop_process_group(process, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stop_process_group(process, signal.SIGKILL)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise


def cell_summary(rows):
    summary = {}
    for client, batch in CELLS:
        selected = [row for row in rows
                    if row["client"] == client and row["flush_batch"] == batch]
        successful = [row for row in selected if row["capture_status"] == "complete"]
        samples = sum(row.get("sample_count") or 0 for row in successful)
        lost = sum(row.get("lost_samples") or 0 for row in selected)
        if len(successful) == REPEATS and lost == 0 and samples >= SAMPLE_FLOOR:
            state = "floor_met"
        elif len(successful) == REPEATS and lost == 0:
            state = "inconclusive_under_floor"
        else:
            state = "incomplete_or_invalid"
        summary[f"{client}/flush{batch}"] = {
            "client": client, "flush_batch": batch,
            "planned": REPEATS, "completed": len(successful),
            "retained_samples": samples, "lost_samples": lost,
            "sample_floor": SAMPLE_FLOOR, "status": state,
        }
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    conflicts = [name for name in ("CC", "LD_PRELOAD", "SCRAPANIUM_CURL_DIR")
                 if os.environ.get(name)]
    if conflicts:
        parser.error("conflicting environment variables must be unset: " + ", ".join(conflicts))
    child_env = dict(os.environ)
    for name in ("BEND_SOURCE", "BUN", "LD_LIBRARY_PATH"):
        child_env.pop(name, None)
    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite profile campaign directory: {output}")
    output.mkdir(parents=True, exist_ok=False)
    source_before = runner.hash_sources()
    schedule = schedule_for_seed(args.seed)
    plan_rows = []
    for repeat, row in enumerate(schedule, 1):
        for position, cell_index in enumerate(row, 1):
            client, batch = CELLS[cell_index]
            run_id = f"profile-{client}-flush{batch}-seed{args.seed}-r{repeat:02d}"
            plan_rows.append({
                "schedule_index": len(plan_rows) + 1,
                "repeat": repeat, "position": position,
                "client": client, "flush_batch": batch,
                "run_id": run_id,
                "output_relative": f"capture-{len(plan_rows) + 1:03d}-{slug(run_id)}",
            })
    if len(plan_rows) != TOTAL:
        raise RuntimeError("fixed profile schedule must contain exactly 72 rows")
    save_exclusive(output / "campaign.json", {
        "schema": 1,
        "status": "planned",
        "purpose": "exclusive CPU stack attribution; profiles are excluded from performance comparisons",
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "seed": args.seed,
        "fixed_capture_count": TOTAL,
        "captures_per_client_flush_cell": REPEATS,
        "fixed_schedule": plan_rows,
        "sampling": {
            "event": "cpu-clock:u", "frequency_hz": 499,
            "callchain": "frame-pointer", "clockid": "mono",
            "aggregate_sample_floor_per_cell": SAMPLE_FLOOR,
            "adaptive_extension": False, "failed_capture_retry": False,
            "under_floor": "retain and mark inconclusive",
        },
        "workload": {
            "messages": runner.PERFORMANCE_COUNT, "message_bytes": runner.SIZE,
            "checker": "original sequence, opcode, full-byte comparison and immediate buffer-release path",
        },
        "build_reuse": {
            "first_scheduled_capture_builds_once": True,
            "later_captures_reuse_hash_verified_binaries": True,
            "tls_material_is_per_capture": True,
        },
        "source_sha256_before": source_before,
        "primary_campaign_started": False,
    })
    results_file = output / "results.jsonl"
    records = []
    shared_build = None
    shared_sha = None
    fatal = None
    try:
        with results_file.open("x", encoding="utf-8") as result_stream:
            for item in plan_rows:
                if runner.hash_sources() != source_before:
                    fatal = "source inputs changed after campaign planning"
                    break
                capture_dir = output / item["output_relative"]
                command = [
                    sys.executable, str(CAPTURE), "--output-dir", str(capture_dir),
                    "--client", item["client"], "--batch", str(item["flush_batch"]),
                    "--run-id", item["run_id"],
                ]
                if shared_build is not None:
                    if verify_build(shared_build, source_before) != shared_sha:
                        fatal = "shared build changed between captures"
                        break
                    command.extend(["--build-dir", str(shared_build)])
                exit_code, stdout, stderr, timed_out = run_capture(command, child_env)
                capture_error = (
                    "capture exceeded fixed 300-second timeout and its process group was stopped"
                    if timed_out else
                    None if exit_code == 0 else f"capture exited {exit_code}"
                )
                capture_dir.mkdir(parents=True, exist_ok=True)
                (capture_dir / "campaign.stdout.log").write_text(stdout, encoding="utf-8")
                (capture_dir / "campaign.stderr.log").write_text(stderr, encoding="utf-8")
                manifest_path = capture_dir / "manifest.json"
                capture = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
                reported_status = capture.get("status") if capture else "missing_manifest"
                sample_count = capture.get("perf_sample_count") if capture else None
                lost_samples = capture.get("perf_lost_samples") if capture else None
                valid_capture = bool(
                    capture and reported_status == "complete" and
                    exit_code == 0 and capture_error is None and
                    capture.get("source_and_binary_hashes_unchanged") is True and
                    capture.get("toolchain_and_dependency_hashes_unchanged") is True and
                    capture.get("build_manifest_unchanged") is True and
                    capture.get("perf_samples_outside_client_interval") == 0 and
                    lost_samples == 0 and type(sample_count) is int and sample_count > 0
                )
                record = {
                    **item,
                    "capture_status": "complete" if valid_capture else (
                        "invalid_capture" if reported_status == "complete" else reported_status
                    ),
                    "reported_capture_status": reported_status,
                    "capture_valid": valid_capture,
                    "exit_code": exit_code,
                    "capture_error": capture_error,
                    "manifest_sha256": runner.phase.digest(manifest_path) if manifest_path.is_file() else None,
                    "sample_count": sample_count if valid_capture else None,
                    "lost_samples": lost_samples,
                    "samples_outside_interval": capture.get("perf_samples_outside_client_interval") if capture else None,
                    "source_and_binary_hashes_unchanged": capture.get("source_and_binary_hashes_unchanged") if capture else None,
                    "toolchain_and_dependency_hashes_unchanged": capture.get("toolchain_and_dependency_hashes_unchanged") if capture else None,
                    "build_manifest_unchanged": capture.get("build_manifest_unchanged") if capture else None,
                    "build_manifest_sha256": capture.get("build_manifest_sha256") if capture else None,
                }
                records.append(record)
                append_row(result_stream, record)
                if runner.hash_sources() != source_before:
                    fatal = "source inputs changed while a capture was running"
                    break
                if shared_build is None:
                    proposed = capture_dir / "build"
                    if not (proposed / "build-manifest.json").is_file():
                        fatal = "first fixed capture did not leave a reusable build manifest"
                        break
                    shared_build = proposed
                    shared_sha = verify_build(shared_build, source_before)
                else:
                    try:
                        if verify_build(shared_build, source_before) != shared_sha:
                            fatal = "shared build manifest or binaries changed during capture"
                            break
                    except BaseException as error:
                        fatal = f"shared build validation failed: {type(error).__name__}: {error}"
                        break
                if capture and capture.get("build_manifest_sha256") not in (None, shared_sha):
                    fatal = "capture manifest disagrees with the shared build manifest"
                    break
    except BaseException as error:
        fatal = f"{type(error).__name__}: {error}"
    source_after = runner.hash_sources()
    all_valid = len(records) == TOTAL and all(row.get("capture_valid") is True for row in records)
    status = "complete" if all_valid and fatal is None and source_after == source_before else "incomplete"
    if source_after != source_before:
        fatal = fatal or "source inputs changed before finalization"
    save_exclusive(output / "campaign_status.json", {
        "schema": 1, "status": status,
        "finished_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "planned_captures": TOTAL, "attempted_captures": len(records),
        "complete_captures": sum(row["capture_status"] == "complete" for row in records),
        "failed_captures": sum(row["capture_status"] != "complete" for row in records),
        "unattempted_captures": TOTAL - len(records),
        "fatal_error": fatal,
        "shared_build_directory": str(shared_build) if shared_build else None,
        "shared_build_manifest_sha256": shared_sha,
        "source_inputs_unchanged": source_before == source_after,
        "cells": cell_summary(records),
        "throughput_or_adoption_claim": False,
    })
    if status != "complete":
        raise RuntimeError(f"profile campaign ended {status}: {fatal or 'a fixed capture failed'}")


if __name__ == "__main__":
    main()
