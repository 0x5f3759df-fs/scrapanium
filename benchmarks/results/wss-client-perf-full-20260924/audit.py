#!/usr/bin/env python3
"""Verify and summarize the retained WSS client CPU-profile experiment.

The audit uses only the files in this result directory plus the frozen tracked
client/runner sources in the repository. It re-parses the archived perf-script
text and recomputes sample-window selection rather than trusting saved totals.
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import random
import re
import subprocess
import sys
import tarfile
import types

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "benchmarks"))
parser = None
runner = None

SOURCE_COMMIT = "061950a8041e6fdf9cbb9602b07bbab5291b6d24"
BOOTSTRAP_SEED = 20260924
BOOTSTRAP_DRAWS = 20_000
EXPECTED_ATTEMPT_FILES = {
    "client.stderr.txt", "client.stdout.txt", "peer-record.json",
    "perf-control.json", "perf.data", "perf.header.stderr.txt",
    "perf.header.txt", "perf.report.stderr.txt", "perf.report.txt",
    "perf.script-ns.txt", "perf.script.stderr.txt", "row.json",
}
MEMBERSHIP_PATTERNS = {
    "tls_aead_crypto": ("aes_gcm", "gcm_", "gcm128", "aead", "hw_gcm"),
    "tls_record_read": ("ssl_read", "ssl_peek", "tls_open_record",
                        "ssl_handle_open_record", "tls_open_app_data"),
    "curl_websocket_receive": ("curl_ws_recv", "ws_dec_pass", "ws_recv"),
    "observed_copy_path": ("copyblocks", "copyforwards", "memcpyfast",
                            "openssl_memcpy", "memmove", "memcpy"),
    "bend_equality_helper": ("sp_bend_bytes_equal",),
}


class AuditError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"expected JSON object at {path.name}")
    return value


def load_frozen_modules(source_hashes: dict[str, str]):
    """Load the audited parser/schedule code from immutable Git blobs."""
    blobs = {}
    for name, expected_hash in source_hashes.items():
        result = subprocess.run(
            ["git", "cat-file", "blob", f"{SOURCE_COMMIT}:{name}"],
            cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        require(sha256_bytes(result.stdout) == expected_hash,
                f"Git blob at source commit does not match recorded hash: {name}")
        blobs[name] = result.stdout
    # Execute the exact parser blob in an isolated module, independent of a
    # later working-tree edit to the benchmark sources.
    parser_module = types.ModuleType("websocket_client_perf_parse_frozen")
    parser_module.__file__ = "<git-blob:websocket_client_perf_parse.py>"
    exec(compile(blobs["benchmarks/websocket_client_perf_parse.py"],
                 parser_module.__file__, "exec"), parser_module.__dict__)
    # The plan builder imports certificate helpers at module import time, but
    # make_plan itself is pure. Stub that unused name while loading the frozen
    # source rather than importing current working-tree support code.
    stub_lab = types.ModuleType("lab")
    stub_lab.certificate = object()
    previous_lab = sys.modules.get("lab")
    sys.modules["lab"] = stub_lab
    try:
        runner_module = types.ModuleType("websocket_client_perf_frozen")
        runner_module.__file__ = "<git-blob:websocket_client_perf.py>"
        exec(compile(blobs["benchmarks/websocket_client_perf.py"],
                     runner_module.__file__, "exec"), runner_module.__dict__)
    finally:
        if previous_lab is None:
            sys.modules.pop("lab", None)
        else:
            sys.modules["lab"] = previous_lab
    return runner_module, parser_module


def percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated percentile (type 7) on already sorted values."""
    if not sorted_values:
        raise AuditError("cannot compute a percentile of an empty sample")
    h = (len(sorted_values) - 1) * p
    lower = math.floor(h)
    upper = math.ceil(h)
    if lower == upper:
        return sorted_values[lower]
    weight = h - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def paired_observer_summary(rows: list[dict]) -> dict:
    by_key = {}
    for row in rows:
        plan = row["plan"]
        if plan["kind"] != "positive":
            continue
        key = (plan["batch"], plan["client"], plan["sampling"], plan["repeat"])
        require(key not in by_key, f"duplicate positive condition key {key}")
        timer = row["timer"]
        elapsed = timer["client_end_monotonic_ns"] - timer["client_start_monotonic_ns"]
        require(elapsed > 0, f"nonpositive client interval for {plan['run_id']}")
        by_key[key] = elapsed

    rng = random.Random(BOOTSTRAP_SEED)
    summary = {}
    for client in runner.CLIENTS:
        for batch in runner.BATCHES:
            ratios = []
            enabled_ms = []
            disabled_ms = []
            for repeat in range(1, runner.FULL_REPEATS + 1):
                profiled = by_key[(batch, client, "profiled", repeat)]
                disabled = by_key[(batch, client, "disabled", repeat)]
                ratios.append(profiled / disabled)
                enabled_ms.append(profiled / 1e6)
                disabled_ms.append(disabled / 1e6)
            require(len(ratios) == 96, f"observer cell incomplete: {client}/flush{batch}")
            boot = []
            for _ in range(BOOTSTRAP_DRAWS):
                draw = [ratios[rng.randrange(len(ratios))] for _ in ratios]
                boot.append(sorted(draw)[len(draw) // 2] if len(draw) % 2 else
                            (sorted(draw)[len(draw) // 2 - 1] + sorted(draw)[len(draw) // 2]) / 2)
            boot.sort()
            key = f"{client}/flush{batch}"
            ratio_sorted = sorted(ratios)
            summary[key] = {
                "paired_repeats": len(ratios),
                "profiled_median_ms": float(sorted(enabled_ms)[len(enabled_ms) // 2 - 1:
                                                       len(enabled_ms) // 2 + 1][0] +
                                                sorted(enabled_ms)[len(enabled_ms) // 2]) / 2,
                "disabled_median_ms": float(sorted(disabled_ms)[len(disabled_ms) // 2 - 1:
                                                       len(disabled_ms) // 2 + 1][0] +
                                                sorted(disabled_ms)[len(disabled_ms) // 2]) / 2,
                "median_paired_profiled_over_disabled": percentile(ratio_sorted, 0.5),
                "nominal_95_percentile_ci": [percentile(boot, 0.025), percentile(boot, 0.975)],
                "bootstrap_draws": BOOTSTRAP_DRAWS,
                "bootstrap_seed": BOOTSTRAP_SEED,
                "interval_definition": "paired ratio = profiled client interval ns / disabled client interval ns; resample repeat IDs within this client/batch cell",
            }
    return summary


def parse_member_inventory(path: Path) -> dict[str, str]:
    inventory = {}
    for line_number, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (attempts/.+)", line)
        require(match is not None, f"invalid member inventory line {line_number}")
        digest, name = match.groups()
        require(name not in inventory, f"duplicate archive inventory path: {name}")
        inventory[name] = digest
    return inventory


def safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts,
            f"unsafe archive member path: {name}")
    return path


def parse_positive_raw(row: dict, raw_script: str) -> tuple[dict, dict]:
    plan = row["plan"]
    perf = row["perf"]
    timer = row["timer"]
    parsed = parser.parse_perf_script(raw_script)
    require(parsed["attribution_valid"] is True and not parsed["malformed_lines"],
            f"malformed perf-script text for {plan['run_id']}")
    start = timer["client_start_monotonic_ns"]
    end = timer["client_end_monotonic_ns"]
    filtered = parser.filter_samples(parsed, row["client_pid"], row["client_tids"], start, end)
    require(filtered["attribution_valid"] is True,
            f"invalid sample attribution for {plan['run_id']}")
    require(filtered["wrong_event_sample_count"] == 0 and
            filtered["in_window_identity_mismatch_count"] == 0,
            f"wrong event/PID/TID inside interval for {plan['run_id']}")
    require(canonical(filtered["selected"]) == canonical(perf["selected_samples"]),
            f"reparsed selected samples differ for {plan['run_id']}")
    window = perf["window_filter"]
    require(window["input_samples"] == parsed["sample_count"] and
            window["selected_samples"] == filtered["selected_sample_count"] and
            window["excluded_counts"] == filtered["excluded_counts"] and
            canonical(window["excluded_rows"]) == canonical(filtered["excluded"]),
            f"stored window filter differs from raw reparse for {plan['run_id']}")
    require(perf["selected_window_user_samples"] == filtered["selected_sample_count"],
            f"stored selected count differs for {plan['run_id']}")
    require(parsed["diagnostic_lines"] == [],
            f"perf script has lost/throttle diagnostic records for {plan['run_id']}")
    return parsed, filtered


def cell_profile_summary(rows: list[dict]) -> dict:
    by_cell: dict[str, dict] = {}
    for client in runner.CLIENTS:
        for batch in runner.BATCHES:
            key = f"{client}/flush{batch}"
            cell_rows = [row for row in rows if row["plan"]["kind"] == "positive" and
                         row["plan"]["sampling"] == "profiled" and
                         row["plan"]["client"] == client and row["plan"]["batch"] == batch]
            leaf_counts = collections.Counter()
            unknown_leaf_contexts = collections.Counter()
            stack_status = collections.Counter()
            unknown_frames = 0
            excluded = collections.Counter()
            sample_total = 0
            raw_sample_total = 0
            membership = collections.Counter()
            for row in cell_rows:
                run_id = row["plan"]["run_id"]
                selected = row["perf"]["selected_samples"]
                sample_total += len(selected)
                raw_sample_total += row["perf"]["window_filter"]["input_samples"]
                excluded.update(row["perf"]["window_filter"]["excluded_counts"])
                for sample in selected:
                    frames = sample["frames"]
                    stack_status[sample["stack_status"]] += 1
                    unknown_frames += sample["unknown_frame_count"]
                    leaf = frames[0] if frames else {}
                    symbol = leaf.get("symbol") or "[unknown]"
                    if leaf.get("dso") is not None:
                        dso = leaf["dso"]
                    elif leaf.get("inlined"):
                        dso = "[DSO not reported for inline frame]"
                    else:
                        dso = "[unknown DSO]"
                    leaf_counts[(dso, symbol)] += 1
                    if leaf.get("unknown", True):
                        parent = frames[1] if len(frames) > 1 else {}
                        parent_symbol = parent.get("symbol") or "[unknown parent]"
                        parent_dso = parent.get("dso") or "[DSO unavailable]"
                        unknown_leaf_contexts[(dso, parent_dso, parent_symbol)] += 1
                    names = [str(frame.get("symbol") or "").lower() for frame in frames]
                    for category, patterns in MEMBERSHIP_PATTERNS.items():
                        if any(pattern in name for pattern in patterns for name in names):
                            membership[category] += 1
            profiled_completions = len(cell_rows)
            floor = 80
            require(profiled_completions == 96, f"profiled rows incomplete for {key}")
            require(sample_total >= floor, f"predeclared sample floor missed for {key}")
            by_cell[key] = {
                "profiled_rows": profiled_completions,
                "selected_samples": sample_total,
                "raw_script_samples_including_excluded": raw_sample_total,
                "stack_status_counts": dict(sorted(stack_status.items())),
                "resolved_stack_samples": stack_status["resolved"],
                "unresolved_stack_samples": stack_status["unresolved"],
                "missing_stack_samples": stack_status["missing"],
                "unknown_leaf_samples": sum(n for (dso, symbol), n in leaf_counts.items()
                                             if symbol == "[unknown]"),
                "unknown_frame_count": unknown_frames,
                "excluded_sample_counts": dict(sorted(excluded.items())),
                "stack_truncation": "unobservable from perf-script text; not treated as zero",
                "top_flat_leaf_groups_dso_symbol": [
                    {"samples": count, "dso": dso, "symbol": symbol}
                    for (dso, symbol), count in leaf_counts.most_common(30)
                ],
                "unknown_leaf_immediate_parent_groups": [
                    {"samples": count, "leaf_dso": dso, "parent_dso": parent_dso,
                     "parent_symbol": parent_symbol}
                    for (dso, parent_dso, parent_symbol), count in unknown_leaf_contexts.most_common(15)
                ],
                "observed_inclusive_stack_memberships_overlap": dict(sorted(membership.items())),
            }
    return by_cell


def verify_header(row: dict, header_text: str, report_text: str,
                  script_text: str) -> dict:
    parsed = parser.parse_perf_header(header_text, report_text, script_text)
    recorded = row["perf"].get("header")
    if recorded is None:
        recorded = row["perf"].get("header")
    require(canonical(parsed) == canonical(recorded),
            f"actual perf header differs from row for {row['plan']['run_id']}")
    event = parsed.get("event") or {}
    clock = parsed.get("clock") or {}
    require(event.get("name") in ("cpu-clock:u", "cpu-clock") and
            event.get("type_num") == 1 and event.get("config") == 0 and
            event.get("frequency_mode") is True and event.get("frequency_hz") == 99 and
            event.get("use_clockid") is True and event.get("clockid") in (1, "1", "monotonic") and
            clock.get("id") in (1, "1") and str(clock.get("name", "")).lower() == "monotonic" and
            event.get("inherit") is True and event.get("exclude_kernel") is True and
            event.get("exclude_hv") is True and event.get("sample_stack_user_bytes") == 16384 and
            {"IP", "TID", "TIME", "CALLCHAIN", "REGS_USER", "STACK_USER"}.issubset(
                set(event.get("sample_type") or [])) and
            bool(event.get("sample_regs_user")) and
            event.get("exclude_callchain_user") is True and
            parsed.get("events") == 1,
            f"perf actual settings mismatch for {row['plan']['run_id']}")
    checks = row["perf"].get("actual_header_checks", {})
    require(bool(checks) and all(value is True for value in checks.values()),
            f"stored actual perf-header checks failed for {row['plan']['run_id']}")
    return parsed


def main() -> int:
    manifest_path = HERE / "manifest.json"
    plan_path = HERE / "plan.json"
    rows_path = HERE / "samples.jsonl"
    archive_path = HERE / "attempts.tar.gz"
    member_list_path = HERE / "attempt-members.sha256"
    manifest = read_json(manifest_path)
    plan_meta = read_json(plan_path)
    require(manifest["mode"] == "full" and manifest["status"] == "complete",
            "manifest is not a completed full run")
    require(sha256_file(plan_path) == manifest["plan_sha256"],
            "public plan.json hash differs from manifest")
    require(manifest["rows_completed"] == manifest["planned_rows"] == 784 and
            manifest["positive_rows_passed"] == 768 and manifest["negative_controls_passed"] == 16 and
            manifest["failed_run_ids"] == [], "manifest completion counts are inconsistent")
    for key in ("source_hashes_unchanged", "binary_hashes_unchanged", "binding_hashes_unchanged",
                "tool_input_hashes_unchanged", "build_inputs_frozen_before_compile",
                "sanitizer_environment_clear", "sample_floor_met"):
        require(manifest.get(key) is True, f"manifest integrity flag not true: {key}")
    require(manifest["source_hashes_before"] == manifest["source_hashes_after"],
            "source hashes changed during collection")
    require(manifest["binary_hashes_before"] == manifest["binary_hashes_after"] and
            manifest["binding_hashes_before"] == manifest["binding_hashes_after"] and
            manifest["tool_input_hashes_before"] == manifest["tool_input_hashes_after"],
            "binary/binding/tool input changed during collection")
    global runner, parser
    runner, parser = load_frozen_modules(manifest["source_hashes_before"])

    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()]
    require(len(rows) == 784 and all(isinstance(row, dict) for row in rows),
            "samples.jsonl does not contain 784 JSON objects")
    seed = plan_meta["seed"]
    expected_plan = runner.make_plan("full", seed)
    require(len(expected_plan) == 784, "reconstructed plan has wrong length")
    expected_ids = [item["run_id"] for item in expected_plan]
    require(manifest["attempted_run_ids"] == expected_ids,
            "manifest attempted IDs differ from deterministic plan")
    actual_ids = [row.get("plan", {}).get("run_id") for row in rows]
    require(actual_ids == expected_ids, "samples.jsonl order/IDs differ from deterministic plan")
    for index, (expected, actual) in enumerate(zip(expected_plan, rows)):
        require(actual.get("plan") == expected, f"row plan mismatch at index {index}")
        require(actual.get("status") == "passed", f"attempt did not pass: {expected['run_id']}")
    require(rows[0]["plan"]["kind"] == "negative_control" and
            all(row["plan"]["kind"] == "negative_control" for row in rows[:16]) and
            all(row["plan"]["kind"] == "positive" for row in rows[16:]),
            "negative controls do not precede positives")
    require(plan_meta["seed"] == 20260923 and plan_meta["positive_repeats_per_batch"] == 96 and
            plan_meta["positive_count"] == 768 and plan_meta["negative_control_count"] == 16 and
            plan_meta["positive_message_count"] == 1024 and plan_meta["size_bytes"] == 65536,
            "public plan metadata differs from fixed schedule contract")

    inventory = parse_member_inventory(member_list_path)
    require(len(inventory) == 784 * 12, "attempt inventory does not contain 12 files per row")
    expected_member_names = set(inventory)
    for name in expected_member_names:
        safe = safe_archive_path(name)
        require(len(safe.parts) == 3 and safe.parts[0] == "attempts" and
                safe.parts[1] in set(expected_ids) and safe.parts[2] in EXPECTED_ATTEMPT_FILES,
                f"unexpected inventory member path: {name}")
    by_id = {row["plan"]["run_id"]: row for row in rows}
    archive_scripts: dict[str, str] = {}
    archive_texts: dict[str, dict[str, str]] = collections.defaultdict(dict)
    archive_sidecars: dict[str, dict[str, bytes]] = collections.defaultdict(dict)
    archive_sizes = 0
    with tarfile.open(archive_path, "r:gz") as tf:
        archive_file_members = set()
        archive_run_dirs = set()
        for member in tf.getmembers():
            safe = safe_archive_path(member.name)
            if member.isdir():
                require(len(safe.parts) in (1, 2) and safe.parts[0] == "attempts" and
                        (len(safe.parts) == 1 or safe.parts[1] in set(expected_ids)),
                        f"unexpected attempt archive directory: {member.name}")
                if len(safe.parts) == 2:
                    archive_run_dirs.add(safe.parts[1])
                continue
            require(member.isfile() and len(safe.parts) == 3 and safe.parts[0] == "attempts",
                    f"nonregular or unexpected archive member: {member.name}")
            name = safe.as_posix()
            require(name not in archive_file_members, f"duplicate archive member: {name}")
            archive_file_members.add(name)
            stream = tf.extractfile(member)
            require(stream is not None, f"archive member unreadable: {name}")
            content = stream.read()
            archive_sizes += len(content)
            require(name in inventory, f"archive member missing from hash inventory: {name}")
            require(sha256_bytes(content) == inventory[name], f"member hash mismatch: {name}")
            run_id, filename = safe.parts[1], safe.parts[2]
            row = by_id[run_id]
            if filename == "row.json":
                archived_row = json.loads(content)
                require(canonical(archived_row) == canonical(row),
                        f"per-attempt row differs from samples.jsonl: {run_id}")
            elif filename == "peer-record.json":
                require(canonical(json.loads(content)) == canonical(row["peer_raw"]),
                        f"peer raw sidecar differs from row: {run_id}")
            elif filename == "perf-control.json":
                control = json.loads(content)
                require(control.get("commands") == row.get("perf_control_commands"),
                        f"control transcript commands differ from row: {run_id}")
                require(control.get("arming") == row.get("perf_arming_ack"),
                        f"control transcript arming ACK differs from row: {run_id}")
            elif filename == "perf.script-ns.txt":
                archive_scripts[run_id] = content.decode("utf-8", errors="strict")
            elif filename in ("perf.header.txt", "perf.report.txt"):
                archive_texts[run_id][filename] = content.decode("utf-8", errors="strict")
            elif filename in ("client.stdout.txt", "client.stderr.txt"):
                key = "client_stdout_sha256" if filename == "client.stdout.txt" else "client_stderr_sha256"
                require(sha256_bytes(content) == row.get(key), f"client output hash mismatch: {run_id}/{filename}")
                archive_sidecars[run_id][filename] = content
            elif filename == "perf.data":
                require(sha256_bytes(content) == row.get("perf_data_sha256") and
                        len(content) == row.get("perf_data_bytes"),
                        f"perf.data hash/size mismatch: {run_id}")
            require(filename in EXPECTED_ATTEMPT_FILES, f"unexpected attempt artifact: {name}")
        require(archive_file_members == expected_member_names,
                "attempt archive member set differs from per-member inventory")
        require(archive_run_dirs == set(expected_ids), "attempt archive omits an attempt directory")
    require(len(archive_scripts) == len(rows), "archive does not retain one perf script per attempt")
    require(len(archive_texts) == len(rows), "archive does not retain header/report text per attempt")
    for name in EXPECTED_ATTEMPT_FILES:
        require(all(f"attempts/{run_id}/{name}" in inventory for run_id in expected_ids),
                f"attempt artifact missing from member inventory: {name}")

    expected_backend = manifest["tools"]["stock_backend"]
    tls_identities = set()
    sample_cell_accum = collections.Counter()
    header_values = collections.Counter()
    for row in rows:
        plan = row["plan"]
        run_id = plan["run_id"]
        require(row["backend"] == row["mapped_backend"], f"mapped backend differs: {run_id}")
        require(row["backend"]["path"] == expected_backend["path"] and
                row["backend"]["sha256"] == expected_backend["sha256"],
                f"attempt does not use frozen stock backend: {run_id}")
        commands = row["perf_control_commands"]
        expected_arming = "enable" if plan["sampling"] == "profiled" else "disable"
        require([item["command"] for item in commands] == ["ping", expected_arming] and
                all(item["raw_ack"] == "ack" and
                    item["request_monotonic_ns"] <= item["ack_monotonic_ns"]
                    for item in commands), f"invalid perf control ACK sequence: {run_id}")
        require(row["perf_attach_ping_ack"] == commands[0] and
                row["perf_arming_ack"] == commands[1], f"row ACK data differs: {run_id}")
        perf_row = row["perf"]
        artifact_hashes = (perf_row.get("artifact_sha256") or
                           perf_row.get("perf_artifact_sha256") or
                           row.get("perf_artifact_sha256"))
        require(isinstance(artifact_hashes, dict), f"missing perf artifact hashes: {run_id}")
        for filename in ("perf.data", "perf.header.txt", "perf.report.txt", "perf.script-ns.txt"):
            require(artifact_hashes.get(filename) == inventory[f"attempts/{run_id}/{filename}"],
                    f"perf artifact hash map differs from inventory: {run_id}/{filename}")
        script = archive_scripts[run_id]
        header_text = archive_texts[run_id]["perf.header.txt"]
        report_text = archive_texts[run_id]["perf.report.txt"]
        header = verify_header(row, header_text, report_text, script)
        event = header.get("event", {})
        header_values[(event.get("name"), event.get("frequency_hz"), event.get("sample_stack_user_bytes"))] += 1
        parsed = parser.parse_perf_script(script)
        require(parsed["attribution_valid"] is True and not parsed["malformed_lines"],
                f"malformed perf script: {run_id}")
        require(parsed["diagnostic_lines"] == [], f"raw perf diagnostic line present: {run_id}")
        diagnostics = header.get("diagnostic_sources", {})
        require(diagnostics.get("record_dump_completeness") == "unknown" and
                diagnostics.get("record_diagnostics_observed") is False,
                f"record loss/throttle completeness mislabeled: {run_id}")
        if plan["kind"] == "negative_control":
            require(perf_row.get("negative_control_has_no_complete_client_timer") is True and
                    not ("client_start_monotonic_ns" in row.get("timer", {}) and
                         "client_end_monotonic_ns" in row.get("timer", {})),
                    f"negative control unexpectedly has complete timer: {run_id}")
            require(row["client_returncode"] != 0 and
                    row["client_result"].get("rejected_expected_corruption") is True,
                    f"negative was not rejected by exact data validation: {run_id}")
            mismatch_text = archive_sidecars[run_id]["client.stderr.txt"].decode("utf-8", errors="strict")
            expected_mismatch = "WebSocket opcode, sequence, or payload mismatch\n"
            expected_stderr = (expected_mismatch if plan["client"] == "scrapanium-bend" else
                               "RuntimeError: " + expected_mismatch)
            require(mismatch_text.endswith(expected_stderr),
                    f"negative rejection did not report the expected exact-data mismatch: {run_id}")
            peer = row["peer"]
            require(peer.get("starts") == 1 and peer.get("warmups") == 1 and
                    0 <= peer.get("data_frames", -1) <= plan["count"] and
                    0 <= peer.get("data_payload_bytes", -1) <= plan["count"] * 65536 and
                    peer.get("data_frame_bytes") == peer.get("data_payload_bytes") +
                    10 * peer.get("data_frames", 0),
                    f"negative peer counters inconsistent: {run_id}")
            if plan["sampling"] == "disabled":
                require(parsed["sample_count"] == 0,
                        f"disabled negative unexpectedly sampled: {run_id}")
            continue

        require(row["client_returncode"] == 0, f"positive client returned nonzero: {run_id}")
        peer = row["peer"]
        expected_bytes = plan["count"] * 65536
        require(peer.get("starts") == 1 and peer.get("warmups") == 1 and
                peer.get("data_frames") == plan["count"] and
                peer.get("data_payload_bytes") == expected_bytes and
                peer.get("data_frame_bytes") == expected_bytes + 10 * plan["count"] and
                peer.get("error") is None,
                f"positive peer totals are wrong: {run_id}")
        require(all(peer.get(key) == value for key, value in row["peer_raw"].items()),
                f"positive peer final/raw records differ: {run_id}")
        identity = (peer.get("tls_version"), peer.get("tls_cipher"))
        tls_identities.add(identity)
        require(identity == (772, 4865), f"unexpected TLS identity: {run_id}")
        timer = row["timer"]
        start, end = timer.get("client_start_monotonic_ns"), timer.get("client_end_monotonic_ns")
        gate = timer.get("parent_gate_write_monotonic_ns")
        require(all(isinstance(value, int) and not isinstance(value, bool)
                    for value in (start, end, gate)) and gate <= start < end,
                f"positive client interval invalid: {run_id}")
        require(commands[1]["ack_monotonic_ns"] < gate and
                commands[0]["ack_monotonic_ns"] < commands[1]["ack_monotonic_ns"],
                f"profiler ACK/gate order invalid: {run_id}")
        if plan["sampling"] == "profiled":
            parsed, filtered = parse_positive_raw(row, script)
            sample_cell_accum[(plan["client"], plan["batch"])]+=filtered["selected_sample_count"]
        else:
            require(parsed["sample_count"] == 0,
                    f"disabled positive recorder produced samples: {run_id}")

    require(tls_identities == {(772, 4865)}, "positive TLS identity varies across cells")
    require(header_values == collections.Counter({("cpu-clock:u", 99, 16384): 784}),
            "actual perf event/frequency/stack settings vary across attempts")
    for client in runner.CLIENTS:
        for batch in runner.BATCHES:
            floor = manifest["profile_floor"][f"{client}/flush{batch}"]
            selected = sample_cell_accum[(client, batch)]
            require(floor["profiled_rows_completed"] == 96 and floor["selected_user_samples"] == selected and
                    floor["floor_met"] is True and floor["predeclared_floor"] == 80 and selected >= 80,
                    f"manifest sample floor differs from raw reparse for {client}/flush{batch}")

    cells = cell_profile_summary(rows)
    observers = paired_observer_summary(rows)
    summary = {
        "schema": 1,
        "source_commit": SOURCE_COMMIT,
        "manifest_sha256": sha256_file(manifest_path),
        "plan_sha256": sha256_file(plan_path),
        "samples_jsonl_sha256": sha256_file(rows_path),
        "attempt_archive_sha256": sha256_file(archive_path),
        "attempt_member_inventory_sha256": sha256_file(member_list_path),
        "attempt_file_count": len(inventory),
        "attempt_archive_uncompressed_bytes": archive_sizes,
        "rows": {"total": len(rows), "positive": 768, "negative_controls": 16,
                 "failed": 0, "plan_order_exact": True, "all_status_passed": True},
        "integrity_flags_all_true": True,
        "git_blob_source_hashes_match": True,
        "stock_backend": expected_backend,
        "tls_identity_set": [list(identity) for identity in sorted(tls_identities)],
        "profile_cells_recomputed_from_raw_perf_script": cells,
        "paired_observer_interval_ratios": observers,
        "perf_header_contract_rows": dict(sorted(("|".join(map(str, key)), count)
                                                  for key, count in header_values.items())),
        "record_diagnostics": {
            "perf_report_lost_sample_values": sorted({row["perf"]["header"].get("lost_samples")
                                                       for row in rows}, key=lambda v: (v is None, str(v))),
            "lost_record_raw_dump_observed": False,
            "lost_record_count": None,
            "throttle_record_count": None,
            "unthrottle_record_count": None,
            "truncated_stack_count": None,
            "stack_truncation_observable": False,
            "explanation": "perf-script text is not a complete raw perf-record dump; unavailable counters remain unknown, not zero",
        },
    }
    out = HERE / "summary.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"result": "PASS", "summary": str(out),
                      "rows": summary["rows"],
                      "profile_cells": {key: {"selected": value["selected_samples"],
                                                "stack_status": value["stack_status_counts"],
                                                "top_leaves": value["top_flat_leaf_groups_dso_symbol"][:3]}
                                        for key, value in cells.items()}}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as error:
        print(f"AUDIT FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
