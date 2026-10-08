#!/usr/bin/env python3
"""Verify the archived counters-only census; never imports or runs its campaign runner."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


FROZEN = {
    "PLAN.md": "014db6acc429a5ad250a2fd0e9a4210b4e8f02af27985fae8821e8e0c83dac63",
    "runner/run_mechanism_counters.py": "caf3fdedfb0182d2ce1f2f170b06545edc8ac012e28b1e539d6427a035deee9d",
    "runner/mechanism_validation.py": "39cdefc5f622c49bb6ffa9acce696aa804d769fa61a6d16fc8bb6c74f1dfe449",
    "runner/test_mechanism_validation.py": "46c54db4e1af4df0a6de3c97a9a4cbaca8f2dfeb1426ab817050075da698142f",
    "runner/make_source_pins.py": "5fe4cbc8dd35b32a3db15375528a35bdd1ded77aa9c506305240a921818728f9",
    "runner/source-pins.json": "edcb4bfa2ba7239144d72d8d709081f44640cd4e68773b852f53acb7dcd0f0bf",
}
EXPECTED_PREFLIGHT = "ce944cf6ac5251bf00865744f3e2712b2ed3965852de7f897b45e6ee852e38e0"
EXPECTED_DSO = "e48e14edacc7d8ed0abab6f5f77e5d9cebca1a87c0153ce7e939b407252e9ab9"
EXPECTED_ATTEMPTS = 16


def fail(message: str) -> None:
    raise ValueError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_path(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        fail(f"unsafe archive path: {relative!r}")
    if "\\" in relative or ":" in relative:
        fail(f"non-portable archive path: {relative!r}")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            fail(f"symlink is forbidden in evidence archive: {relative}")
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve()):
        fail(f"archive path escapes package: {relative}")
    return current


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def list_files(root: Path) -> set[str]:
    result: set[str] = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            fail(f"symlink is forbidden in evidence archive: {item.relative_to(root).as_posix()}")
        if item.is_file():
            result.add(item.relative_to(root).as_posix())
    return result


def verify_checksums(root: Path) -> None:
    sums_path = safe_path(root, "SHA256SUMS")
    rows: dict[str, str] = {}
    for line_number, raw in enumerate(sums_path.read_text(encoding="ascii").splitlines(), 1):
        if len(raw) < 67 or raw[64:66] != "  ":
            fail(f"malformed SHA256SUMS line {line_number}")
        digest, rel = raw[:64], raw[66:]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            fail(f"malformed SHA-256 on line {line_number}")
        safe_path(root, rel)
        if rel in rows:
            fail(f"duplicate checksum path: {rel}")
        rows[rel] = digest
    expected = list_files(root) - {"SHA256SUMS"}
    if set(rows) != expected:
        missing = sorted(expected - set(rows))
        extra = sorted(set(rows) - expected)
        fail(f"checksum file inventory mismatch: missing={missing[:4]} extra={extra[:4]}")
    for rel, expected_hash in rows.items():
        path = safe_path(root, rel)
        if not path.is_file() or hash_file(path) != expected_hash:
            fail(f"checksum mismatch: {rel}")


def import_validation(root: Path):
    sys.dont_write_bytecode = True
    path = safe_path(root, "runner/mechanism_validation.py")
    spec = importlib.util.spec_from_file_location("archived_mechanism_validation", path)
    if spec is None or spec.loader is None:
        fail("cannot load the frozen validation module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_frozen_inputs(root: Path) -> dict[str, Any]:
    for rel, expected_hash in FROZEN.items():
        path = safe_path(root, rel)
        if not path.is_file() or hash_file(path) != expected_hash:
            fail(f"frozen input missing or changed: {rel}")
    pins = read_json(safe_path(root, "runner/source-pins.json"))
    if pins.get("source_preflight", {}).get("sha256") != EXPECTED_PREFLIGHT:
        fail("source-pins preflight digest differs from frozen census")
    preflight_path = safe_path(root, "provenance/source-preflight/source-preflight.json")
    if hash_file(preflight_path) != EXPECTED_PREFLIGHT:
        fail("archived source-preflight hash mismatch")
    preflight = read_json(preflight_path)
    if (preflight.get("attempts_started") is not False or preflight.get("builds_started") is not False
            or preflight.get("performance_claims") is not False
            or preflight.get("acceptance_claims") is not False):
        fail("source-preflight scope flags are not no-build/no-attempt/no-performance")

    snapshot_map = read_json(safe_path(root, "provenance/snapshot-map.json"))
    if set(snapshot_map) != {"baseline", "owned", "shared", "curl_overlay", "extra_patch", "curl_license"}:
        fail("snapshot map sections differ from expected")
    for arm in ("baseline", "owned"):
        expected_map = pins["worktrees"][arm]["changed_file_sha256"]
        records = snapshot_map[arm]
        actual_map: dict[str, str] = {}
        for record in records:
            source_rel = record["source_relative_path"]
            archived = safe_path(root, record["archive_path"])
            if not archived.is_file():
                fail(f"{arm} source snapshot missing: {source_rel}")
            digest = hash_file(archived)
            if digest != record["sha256"] or archived.stat().st_size != record["bytes"]:
                fail(f"{arm} source snapshot hash/size mismatch: {source_rel}")
            actual_map[source_rel] = digest
        if actual_map != expected_map:
            fail(f"{arm} source snapshot inventory differs from source-pins")

    expected_shared = {
        "benchmarks/websocket.bend", "benchmarks/websocket_stream.bend",
        "benchmarks/websocket.py", "benchmarks/websocket_streaming.py",
        "benchmarks/websocket_server.go", "benchmarks/websocket_stream_server.go",
        "benchmarks/websocket_clock.c", "native/bend_websocket.inc.c",
        "scripts/build.py", "tests/lab.py", "tests/ws_lab.py",
    }
    shared_expected_hashes = preflight["baseline"]["source_sha256"]
    shared_actual: dict[str, str] = {}
    for record in snapshot_map["shared"]:
        rel = record["source_relative_path"]
        path = safe_path(root, record["archive_path"])
        if not path.is_file():
            fail(f"shared pinned source snapshot missing: {rel}")
        digest = hash_file(path)
        if digest != record["sha256"] or path.stat().st_size != record["bytes"]:
            fail(f"shared source snapshot hash/size mismatch: {rel}")
        shared_actual[rel] = digest
    if set(shared_actual) != expected_shared:
        fail("shared checker/helper/peer source snapshot inventory mismatch")
    if any(shared_actual[name] != shared_expected_hashes.get(name) for name in expected_shared):
        fail("shared checker/helper/peer sources differ from source preflight")

    expected_overlay = preflight["backend"]["curl_overlay_source_sha256"]
    actual_overlay: dict[str, str] = {}
    for record in snapshot_map["curl_overlay"]:
        rel = record["source_relative_path"]
        path = safe_path(root, record["archive_path"])
        digest = hash_file(path)
        if digest != record["sha256"] or path.stat().st_size != record["bytes"]:
            fail(f"Curl overlay snapshot hash/size mismatch: {rel}")
        actual_overlay[rel] = digest
    if actual_overlay != expected_overlay:
        fail("Curl overlay source snapshots differ from preflight")

    patch = snapshot_map["extra_patch"]
    patch_path = safe_path(root, patch["archive_path"])
    if hash_file(patch_path) != patch["sha256"] or patch_path.stat().st_size != patch["bytes"]:
        fail("diagnostic Curl patch hash/size mismatch")
    build_validation_path = safe_path(root, "provenance/curl-backend/build-validation.json")
    if hash_file(build_validation_path) != pins["backend"]["build_validation_sha256"]:
        fail("private Curl build-validation hash differs from source-pins")
    build_validation = read_json(build_validation_path)
    if (build_validation.get("status") != "pass" or build_validation.get("build_exit_code") != 0
            or build_validation.get("campaign_started") is not False
            or build_validation.get("dso", {}).get("sha256") != EXPECTED_DSO):
        fail("private Curl build validation is incomplete or has an unexpected DSO")
    if build_validation.get("diagnostic_overlay_sha256") != patch["sha256"]:
        fail("diagnostic patch differs from private Curl build record")
    prefix_manifest = safe_path(root, "provenance/curl-backend/prefix-manifest.json")
    if hash_file(prefix_manifest) != pins["backend"]["prefix_manifest_sha256"]:
        fail("private Curl prefix manifest hash differs from source-pins")
    license_record = snapshot_map["curl_license"]
    license_path = safe_path(root, license_record["archive_path"])
    if (hash_file(license_path) != license_record["sha256"]
            or license_path.stat().st_size != license_record["bytes"]):
        fail("Curl COPYING license hash/size mismatch")
    return {"pins": pins, "preflight": preflight, "build_validation": build_validation}


def verify_saved_run(root: Path) -> dict[str, Any]:
    verify_checksums(root)
    frozen = verify_frozen_inputs(root)
    validation = import_validation(root)
    pins = frozen["pins"]
    census = safe_path(root, "census")
    status = read_json(census / "status.json")
    report_path = census / "report.json"
    report_data = report_path.read_bytes()
    report = json.loads(report_data)
    attempts_path = census / "attempts.jsonl"
    attempts = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines() if line]
    if status.get("status") != "complete" or status.get("attempt_count") != EXPECTED_ATTEMPTS:
        fail("saved census status/count mismatch")
    if status.get("report_sha256") != sha256(report_data):
        fail("status/report digest mismatch")
    if status.get("performance_claims") is not False or status.get("acceptance_claims") is not False:
        fail("saved status contains performance or acceptance claim")
    if status.get("builds_started") is not False or status.get("source_pins_stable") is not True:
        fail("saved status scope/source-stability flags mismatch")
    if len(attempts) != EXPECTED_ATTEMPTS or attempts != report.get("attempts"):
        fail("attempt JSONL and report rows differ")
    if report.get("source_pins_sha256") != FROZEN["runner/source-pins.json"]:
        fail("report source-pins digest mismatch")
    if report.get("source_preflight_sha256") != EXPECTED_PREFLIGHT:
        fail("report preflight digest mismatch")
    if report.get("dso_sha256") != EXPECTED_DSO or report.get("dso_realpath") != pins["backend"]["dso_realpath"]:
        fail("report diagnostic DSO pin mismatch")
    if report.get("status") != "complete" or report.get("attempt_count") != EXPECTED_ATTEMPTS:
        fail("report status/count mismatch")

    expected_schedule = validation.fixed_schedule()
    planned = read_json(census / "planned_schedule.json")
    if (not isinstance(planned, dict) or planned.get("attempts") != expected_schedule
            or planned.get("planned_attempts") != EXPECTED_ATTEMPTS or planned.get("status") != "fixed_before_execution"):
        fail("planned schedule differs from frozen fixed schedule")
    if report.get("counter_schema") != pins.get("counter_schema"):
        fail("counter schema differs from frozen source-pins")

    def counter_loader(relative: str, expected_digest: str) -> list[dict[str, Any]]:
        path = safe_path(census, relative)
        data = path.read_bytes()
        if sha256(data) != expected_digest:
            fail(f"counter file digest mismatch: {relative}")
        return [json.loads(line) for line in data.decode("utf-8").splitlines() if line]

    def peer_loader(relative: str, expected_digest: str) -> bytes:
        data = safe_path(census, relative).read_bytes()
        if sha256(data) != expected_digest:
            fail(f"peer log digest mismatch: {relative}")
        return data

    validation.validate_report(report, pins["counter_schema"], counter_loader, peer_loader)
    build_path = safe_path(root, "provenance/client-build/build-session.json")
    if hash_file(build_path) != pins["client_build"]["session_sha256"]:
        fail("client build-session digest differs from source-pins")
    build = read_json(build_path)
    commands_path = safe_path(root, "provenance/client-build/build-commands.json")
    if hash_file(commands_path) != pins["client_build"]["commands_sha256"]:
        fail("client build-command digest differs from source-pins")
    if (build.get("status") != "builds-complete" or build.get("commands_planned") != 10
            or build.get("commands_completed") != 10 or build.get("client_invocations") is not False
            or build.get("peer_invocations") is not False or build.get("workload_attempts") is not False
            or len(build.get("commands", [])) != 10):
        fail("client build provenance is incomplete or includes client/workload execution")
    outputs = build.get("output_hashes", {})
    for command in build["commands"]:
        name = command.get("name")
        if (command.get("exit_code") != 0 or command.get("status") != "complete"
                or command.get("output_exists") is not True
                or outputs.get(command.get("output")) != command.get("output_sha256")):
            fail(f"client build output/exit verification failed: {name}")
        for stream in ("stdout", "stderr"):
            path = safe_path(root, f"provenance/client-build/logs/{name}.{stream}.txt")
            if hash_file(path) != command.get(f"{stream}_sha256"):
                fail(f"client build {stream} log hash mismatch: {name}")
    return {
        "attempts": len(attempts),
        "status": status["status"],
        "performance_claims": False,
        "acceptance_claims": False,
        "tls": {"version": validation.TLS_VERSION, "cipher": validation.TLS_CIPHER},
        "dso_sha256": report["dso_sha256"],
    }


def rewrite_sums(root: Path) -> None:
    rows = []
    for rel in sorted(list_files(root) - {"SHA256SUMS"}):
        rows.append(f"{hash_file(safe_path(root, rel))}  {rel}\n")
    safe_path(root, "SHA256SUMS").write_text("".join(rows), encoding="ascii", newline="\n")


def expect_error(label: str, function, expected_fragment: str) -> None:
    try:
        function()
    except Exception as exc:
        if expected_fragment not in str(exc):
            fail(f"{label}: wrong rejection: {exc}")
        print(f"SELFTEST PASS: {label}: {exc}")
        return
    fail(f"{label}: tampered copy was accepted")


def self_test(root: Path) -> None:
    result = verify_saved_run(root)
    print(f"SELFTEST PASS: original saved census ({result['attempts']} attempts)")
    with tempfile.TemporaryDirectory(prefix="mechanism-census-audit-") as temp_name:
        temp = Path(temp_name) / root.name
        shutil.copytree(root, temp)
        attempts_file = temp / "census/attempts.jsonl"
        report_file = temp / "census/report.json"
        status_file = temp / "census/status.json"
        rows = [json.loads(line) for line in attempts_file.read_text(encoding="utf-8").splitlines()]
        report = json.loads(report_file.read_text(encoding="utf-8"))
        rows[0], rows[1] = rows[1], rows[0]
        report["attempts"][0], report["attempts"][1] = report["attempts"][1], report["attempts"][0]
        report_bytes = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
        report_file.write_bytes(report_bytes)
        attempts_file.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8", newline="\n")
        status = json.loads(status_file.read_text(encoding="utf-8"))
        status["report_sha256"] = sha256(report_bytes)
        status_file.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        rewrite_sums(temp)
        expect_error("reordered rows rejected by frozen schedule", lambda: verify_saved_run(temp), "fixed schedule mismatch")

    with tempfile.TemporaryDirectory(prefix="mechanism-census-source-") as temp_name:
        temp = Path(temp_name) / root.name
        shutil.copytree(root, temp)
        source = temp / "snapshots/baseline/native/scrapanium.h"
        source.unlink()
        rewrite_sums(temp)
        expect_error("missing pinned source snapshot rejected", lambda: verify_saved_run(temp), "source snapshot missing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="verify saved data and temporary tamper cases")
    parser.add_argument("--package", type=Path, help="package directory; defaults to this script's package")
    args = parser.parse_args()
    root = args.package.resolve() if args.package else Path(__file__).resolve().parents[1]
    try:
        if args.self_test:
            self_test(root)
        else:
            result = verify_saved_run(root)
            print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
