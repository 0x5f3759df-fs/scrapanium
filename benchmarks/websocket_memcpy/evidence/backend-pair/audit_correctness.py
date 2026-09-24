#!/usr/bin/env python3
"""Recheck the retained source-built curl memcpy pair''s correctness gate.

This reads the archived logs, manifests, JSONL samples, and ELF text plus the
machine-specific pair root. It does not build binaries or collect timings.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
SOURCE_COMMIT = "411981c05167fa54065fa3d8757bac3c734b19f6"
UPSTREAM_COMMIT = "6e8f87760a4dd96771e96fc9d55440dcd8845243"
BASELINE_SHA256 = "19a3732717119c67002204579881379c0d89beb0ae080cce38a4cc52fd9bbd8b"
CANDIDATE_SHA256 = "c15ed19b2510ea89e94df68e64146612bda86733414706b367f186250ecd0621"
ROUTE_SHA256 = "a5fb141075277aa556c968c3aa88de0d73d7c93dff67874c43907067ed3f2835"
ROUTE_SOURCE_SHA256 = "79f1816530383e34c36df60d46bfd1b672a7f2635cf3cbfebafb6bf617e0aea1"
TLS = (772, 4865)
MISMATCH = "WebSocket opcode, sequence, or payload mismatch"
EXPECTED_WORKLOADS = (
    "stream-30b-flush1", "stream-1024b-flush1", "stream-65536b-flush1",
    "stream-30b-flush64", "stream-1024b-flush64", "stream-65536b-flush64",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            value.update(chunk)
    return value.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def check_pytest(archive_side: Path):
    log = (archive_side / f"{archive_side.name}-pytest.log").read_text(encoding="utf-8")
    require("461 passed, 32 skipped" in log, f"{archive_side.name}: pytest summary differs")
    xml_path = archive_side / "results.xml.gz"
    with gzip.open(xml_path, "rb") as stream:
        root = ET.parse(stream).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(item.attrib.get("tests", "0")) for item in suites)
    failures = sum(int(item.attrib.get("failures", "0")) for item in suites)
    errors = sum(int(item.attrib.get("errors", "0")) for item in suites)
    skipped = sum(int(item.attrib.get("skipped", "0")) for item in suites)
    skip_reasons = collections.Counter(
        node.attrib.get("message", "unspecified")
        for node in root.iter("skipped")
    )
    require((tests, failures, errors, skipped) == (493, 0, 0, 32),
            f"{archive_side.name}: JUnit totals differ")
    expected_skips = {
        "requires the optional source-built browser backend": 21,
        "requires the optional browser backend": 9,
        "requires the optional source build": 2,
    }
    require(dict(skip_reasons) == expected_skips,
            f"{archive_side.name}: unexpected pytest skip reasons: {dict(skip_reasons)}")
    return {
        "tests": tests, "passed": tests - skipped, "skipped": skipped,
        "failures": failures, "errors": errors, "skip_reasons": dict(skip_reasons),
    }


def check_streaming(archive_side: Path, side: str, expected_sha: str):
    outputs = {}
    for threads in (1, 4):
        path = archive_side / f"ws-stream-smoke-{threads}.json"
        doc = read_json(path)
        require(doc.get("smoke_only") is True, f"{side}: stream run is not smoke-only")
        require(doc.get("source_hashes_checked_before_build_and_after_samples") is True,
                f"{side}: stream source hash recheck missing")
        require(doc.get("backend", {}).get("sha256") == expected_sha,
                f"{side}: stream selected backend hash differs")
        workloads = doc.get("workloads", [])
        require([item.get("name") for item in workloads] == list(EXPECTED_WORKLOADS),
                f"{side}: stream workload order differs")
        for workload in workloads:
            size = int(workload["bytes"])
            count = int(workload["count"])
            expected_bytes = size * count
            require(workload.get("expected_corpus_payload_bytes") == expected_bytes,
                    f"{side}: stream expected corpus byte count differs")
            clients = workload.get("clients", {})
            require(set(clients) == {"scrapanium-bend", "curl_cffi-matched"},
                    f"{side}: stream client set differs")
            for client, value in clients.items():
                rows = value.get("samples", [])
                require(len(rows) == 1, f"{side}: stream cell is not one smoke sample")
                peer = rows[0].get("peer_observation", {})
                mapped = rows[0].get("mapped_backend", {})
                require(mapped.get("sha256") == expected_sha,
                        f"{side}: stream client loaded a different backend")
                require(peer.get("data_frames") == count and
                        peer.get("data_payload_bytes") == expected_bytes and
                        peer.get("warmups") == 1 and peer.get("starts") == 1 and
                        (peer.get("tls_version"), peer.get("tls_cipher")) == TLS,
                        f"{side}: stream peer counters/TLS differ")
        negative = doc.get("negative_controls", [])
        require(len(negative) == 4, f"{side}: stream negative-control count differs")
        require(all(row.get("returncode", 0) != 0 and
                    row.get("detected_by_exact_check") is True and
                    row.get("mapped_backend", {}).get("sha256") == expected_sha
                    for row in negative),
                f"{side}: stream corruption/swap control failed")
        outputs[str(threads)] = {
            "smoke_only": True, "threads": threads, "workloads": len(workloads),
            "negative_controls": len(negative), "backend_sha256": expected_sha,
        }
    return outputs


def check_diag(archive_side: Path, diag: str, expected_sha: str):
    folder = archive_side / diag
    manifest = read_json(folder / "manifest.json")
    require(manifest.get("status") == "complete", f"{archive_side.name}/{diag}: incomplete")
    require(manifest.get("smoke_only") is True, f"{archive_side.name}/{diag}: not smoke-only")
    require(manifest.get("sample_count") == 24 and
            manifest.get("completed_sample_count") == 24 and
            manifest.get("negative_control_count") == 16 and
            manifest.get("correctness_smoke_positive_count") == 8,
            f"{archive_side.name}/{diag}: wrong row totals")
    require(manifest.get("backend", {}).get("sha256") == expected_sha,
            f"{archive_side.name}/{diag}: selected backend SHA differs")
    require(manifest.get("source_hashes_checked_after_samples") is True and
            manifest.get("compiler_inputs_checked_after_samples") is True and
            manifest.get("binding_inputs_checked_after_samples") is True,
            f"{archive_side.name}/{diag}: post-run provenance check missing")
    if diag == "phase":
        require(manifest.get("sanitize_build") is True,
                f"{archive_side.name}/{diag}: sanitizer build missing")
    else:
        require(manifest.get("sanitize_bend_smoke") is True and
                manifest.get("tls_private_key_retained") is False,
                f"{archive_side.name}/{diag}: sanitizer/key-retention flags differ")

    rows = read_jsonl(folder / "samples.jsonl")
    require(len(rows) == 24, f"{archive_side.name}/{diag}: JSONL row count differs")
    ids = [row.get("run_id") for row in rows]
    require(len(set(ids)) == 24 and None not in ids,
            f"{archive_side.name}/{diag}: duplicate or missing run IDs")
    positive = negative = 0
    for row in rows:
        require(row.get("validation_errors") == [],
                f"{archive_side.name}/{diag}: retained validation error")
        client = row.get("client_result", {})
        peer = row.get("peer", {})
        mapped = client.get("mapped_backend", {})
        require(mapped.get("sha256") == expected_sha,
                f"{archive_side.name}/{diag}: row mapped backend differs")
        peer_frames = peer.get("data_frames")
        peer_payload = peer.get("data_payload_bytes")
        peer_frame_bytes = peer.get("data_frame_bytes")
        require(peer.get("completed") is True and
                isinstance(peer_frames, int) and 0 <= peer_frames <= 8 and
                peer_payload == peer_frames * 65536 and
                peer_frame_bytes == peer_payload + peer_frames * 10 and
                (peer.get("tls_version"), peer.get("tls_cipher")) == TLS,
                f"{archive_side.name}/{diag}: peer totals/TLS differ")
        if row.get("kind") == "negative_correctness_control":
            negative += 1
            text = row.get("client_stdout", "") + row.get("client_stderr", "")
            require(client.get("returncode") not in (None, 0) and MISMATCH in text,
                    f"{archive_side.name}/{diag}: bad-message control not detected")
        else:
            positive += 1
            require(client.get("returncode") == 0,
                    f"{archive_side.name}/{diag}: positive smoke client failed")
            require(peer_frames == 8 and peer_payload == 8 * 65536,
                    f"{archive_side.name}/{diag}: positive peer totals are incomplete")
            if diag == "attribution":
                shim = row.get("shim", {})
                require(shim.get("started") is True and shim.get("completed") is True and
                        shim.get("clock_error") is False and shim.get("write_error") is False,
                        f"{archive_side.name}/{diag}: positive shim record invalid")
        if diag == "attribution":
            shim = row.get("shim", {})
            require(shim.get("started") is True and shim.get("clock_error") is False,
                    f"{archive_side.name}/{diag}: shim did not start cleanly")
    require((positive, negative) == (8, 16),
            f"{archive_side.name}/{diag}: positive/negative split differs")
    return {
        "status": "complete", "samples": len(rows), "positive": positive,
        "negative_controls": negative, "sanitizer_smoke": True,
        "backend_sha256": expected_sha, "tls": {"version": TLS[0], "cipher": TLS[1]},
    }


def check_link_pair(pair: Path):
    baseline = read_json(pair / "baseline-provenance.json")
    candidate = read_json(pair / "candidate-provenance.json")
    mapping = read_json(pair / "baseline-candidate-object-map.json")
    elf = read_json(pair / "candidate-elf-evidence.json")
    command = read_json(pair / "candidate-link-command.json")
    require(baseline["upstream"]["commit"] == UPSTREAM_COMMIT and
            baseline["upstream"]["tag"] == "v2.2.3" and
            baseline["upstream"]["clean"] is True,
            "baseline upstream identity is not the expected clean release")
    require(baseline["baseline"]["route_object_absent"] is True and
            baseline["baseline"]["microprobe_runtime_object_absent"] is True,
            "baseline includes a forced-probe object")
    require(candidate["upstream"]["commit"] == UPSTREAM_COMMIT and
            candidate["upstream"]["clean"] is True,
            "candidate upstream identity differs")
    base_path = Path(baseline["baseline"]["path"])
    cand_path = Path(candidate["candidate_dso"]["path"])
    require(digest(base_path) == BASELINE_SHA256 and
            digest(cand_path) == CANDIDATE_SHA256,
            "baseline/candidate DSO bytes changed")
    route_path = Path(candidate["route"]["object"])
    require(digest(route_path) == ROUTE_SHA256 and
            digest(REPO_ROOT / "benchmarks/websocket_memcpy/memcpy_route.c") ==
            ROUTE_SOURCE_SHA256,
            "route source/object hash differs")

    base_inputs = {item["path"]: item["sha256"] for item in mapping["baseline_inputs"]}
    cand_inputs = {item["path"]: item["sha256"] for item in mapping["candidate_inputs"]}
    extra = set(cand_inputs) - set(base_inputs)
    require(not (set(base_inputs) - set(cand_inputs)) and
            all(base_inputs[key] == cand_inputs[key] for key in base_inputs),
            "candidate changed or removed a baseline link input")
    require(len(extra) == 1 and next(iter(extra)) == str(route_path) and
            cand_inputs[str(route_path)] == ROUTE_SHA256 and
            mapping["input_hashes_verified_before_link"] is True,
            "candidate link input delta is not exactly the reviewed route object")
    require(mapping["baseline_input_count"] == 209 and
            mapping["candidate_input_count"] == 210,
            "candidate object/archive counts differ")

    args = command
    route_arg = str(route_path)
    base_argv = list(args["baseline_argv"])
    cand_argv = [item for item in args["candidate_argv"] if item != route_arg]
    output_diff = args["expected_differences"]["output_path"]
    base_argv = [("<OUTPUT_DSO>" if item == output_diff[0] else item)
                 for item in base_argv]
    cand_argv = [("<OUTPUT_DSO>" if item == output_diff[1] else item)
                 for item in cand_argv]
    require(base_argv == cand_argv,
            "normalized baseline/candidate link arguments differ beyond route/output")
    require(elf["symbols"]["candidate_route_same_address"] is True and
            elf["symbols"]["candidate_route_body_calls_memcpy_plt"] is True and
            elf["symbols"]["candidate_versioned_relocation"] == "memcpy@GLIBC_2.14",
            "candidate does not show the reviewed alias-to-versioned-libc route")
    require(elf["symbols"]["candidate_ssl_read_calls_route"] and
            elf["symbols"]["candidate_ssl_peek_calls_route"] and
            elf["symbols"]["baseline_ssl_read_calls_local_memcpy"] and
            elf["symbols"]["baseline_ssl_peek_calls_local_memcpy"],
            "TLS copy callers do not differ as expected")
    require(elf["dynamic"]["needed_identical"] is True and
            elf["glibc_versions"]["maximum_candidate"] == "2.17" and
            elf["glibc_versions"]["candidate_within_2_17"] is True,
            "candidate changed NEEDED set or GLIBC ceiling")
    require(candidate["link"]["all_baseline_input_hashes_unchanged_after_link"] is True and
            candidate["link"]["baseline_dso_unchanged_after_link"] is True and
            candidate["link"]["only_input_delta"] ==
            "one reviewed memcpy_route.o added before pinned static archives",
            "candidate post-link integrity flags differ")
    return {
        "upstream_tag": "v2.2.3", "upstream_commit": UPSTREAM_COMMIT,
        "baseline_dso_path": str(base_path), "baseline_dso_sha256": BASELINE_SHA256,
        "candidate_dso_path": str(cand_path), "candidate_dso_sha256": CANDIDATE_SHA256,
        "candidate_route_source_sha256": ROUTE_SOURCE_SHA256,
        "candidate_route_object_path": str(route_path),
        "candidate_route_object_sha256": ROUTE_SHA256,
        "baseline_link_inputs": len(base_inputs), "candidate_link_inputs": len(cand_inputs),
        "candidate_only_input": "one memcpy_route.o; all 209 shared input hashes match",
        "candidate_glibc_max": "2.17", "needed_identical": True,
        "ssl_read_and_peek_route_verified": True,
    }


def check_guard(archive: Path, pair: Path):
    folder = archive / "correctness/candidate-guard"
    summary = read_json(folder / "summary.json")
    log = (folder / "candidate-full-dso-guard.log").read_text(encoding="utf-8")
    require(summary.get("result") == "PASS" and
            summary.get("timing_collected") is False and
            len(summary.get("cases", [])) == 6,
            "guard harness self-test failed")
    require("PASS: 83135 alignment/size cases, 64 guarded-edge cases" in log,
            "candidate DSO guard result missing")
    driver = pair / "candidate-guard-selftest/memcpy_guard_test"
    require(digest(driver) == summary.get("driver_sha256"),
            "guard driver hash differs")
    candidate_dso = pair / "candidate-install/lib/libcurl-impersonate.so.4.8.0"
    require(f"resolved_dso={candidate_dso}"
            in log, "guard log names a different DSO")
    require(digest(candidate_dso) == CANDIDATE_SHA256, "guard DSO changed")
    return {
        "passed": True, "candidate_dso_sha256": CANDIDATE_SHA256,
        "driver_sha256": summary["driver_sha256"],
        "size_alignment_cases": 83135, "guarded_edge_cases": 64,
        "fault_controls_rejected": len(summary["cases"]),
    }


def check_initial_environment_rejection(archive: Path) -> None:
    path = archive / "correctness/baseline/attribution/initial-env-rejection.txt"
    text = path.read_text(encoding="utf-8")
    require("clear SCRAPANIUM_CURL_DIR" in text and
            "no client or peer was started" in text and
            "not the original untouched log file" in text,
            "initial rejected attribution invocation is not transparently documented")


def archive_inventory() -> list[dict]:
    entries = []
    for path in sorted(HERE.rglob("*")):
        if not path.is_file() or path.name in {
                "SHA256SUMS.txt", "correctness-gate.json"} or path.name == ".gitattributes":
            continue
        if "__pycache__" in path.parts:
            continue
        item = {
            "path": path.relative_to(REPO_ROOT).as_posix(),
            "sha256": digest(path),
        }
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as stream:
                raw_hash = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    raw_hash.update(chunk)
            item["uncompressed_sha256"] = raw_hash.hexdigest()
        entries.append(item)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pair-root",
        type=Path,
        default=Path("/home/baidu/scrapanium-experiments/wss-memcpy-backend-pair-20260924"),
    )
    args = parser.parse_args()
    pair = args.pair_root.resolve()

    source_root = REPO_ROOT
    for side in ("baseline", "candidate"):
        worktree = pair / "test-roots" / side
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        require(head == SOURCE_COMMIT, f"{side} checkout is not frozen at {SOURCE_COMMIT}")

    link = check_link_pair(pair)
    guard = check_guard(HERE, pair)
    suites = {}
    for side, expected_sha in (("baseline", BASELINE_SHA256),
                               ("candidate", CANDIDATE_SHA256)):
        archive_side = HERE / "correctness" / side
        suites[side] = {
            "pytest": check_pytest(archive_side),
            "streaming_sanitizer": check_streaming(archive_side, side, expected_sha),
            "phase_sanitizer": check_diag(archive_side, "phase", expected_sha),
            "attribution_sanitizer": check_diag(archive_side, "attribution", expected_sha),
        }

    required_checks = {
        "guard_fixture": guard["passed"],
        "baseline_full_suite": suites["baseline"]["pytest"]["failures"] == 0,
        "candidate_full_suite": suites["candidate"]["pytest"]["failures"] == 0,
        "baseline_ws_sanitizer": len(suites["baseline"]["streaming_sanitizer"]) == 2,
        "candidate_ws_sanitizer": len(suites["candidate"]["streaming_sanitizer"]) == 2,
        "baseline_phase_sanitizer": suites["baseline"]["phase_sanitizer"]["status"] == "complete",
        "candidate_phase_sanitizer": suites["candidate"]["phase_sanitizer"]["status"] == "complete",
        "baseline_attribution_sanitizer":
            suites["baseline"]["attribution_sanitizer"]["status"] == "complete",
        "candidate_attribution_sanitizer":
            suites["candidate"]["attribution_sanitizer"]["status"] == "complete",
    }
    require(all(required_checks.values()), "one or more correctness gates failed")
    check_initial_environment_rejection(HERE)
    gate = {
        "schema": "scrapanium-wss-memcpy-correctness-gate-v1",
        "client_source_revision": SOURCE_COMMIT,
        "baseline_dso_sha256": BASELINE_SHA256,
        "candidate_dso_sha256": CANDIDATE_SHA256,
        "backend_pair": link,
        "checks": required_checks,
        "candidate_full_dso_guard": guard,
        "suites": suites,
        "worktree_deps_mapping": {
            "description": "Each isolated worktree had a private .deps directory; .deps/curl symlinked to the selected source-built pair prefix. Other pinned .deps inputs symlinked to the main checkout's read-only dependency tree.",
            "main_checkout_modified": False,
            "attribution_legacy_backend_label_note": "The historical attribution manifest says 'pinned .deps/curl stock backend'; its backend.path and sha256 identify the selected source-built baseline or candidate prefix recorded here. Do not interpret that label as the prebuilt release DSO.",
        },
        "no_performance_data": True,
        "interpretation": "All phase/attribution outputs are 8-message sanitizer correctness smoke records. Their instrumentation timing fields are not performance measurements.",
        "artifacts": archive_inventory(),
    }
    gate_path = HERE / "correctness-gate.json"
    payload = json.dumps(gate, indent=2, sort_keys=True) + "\n"
    gate_path.write_text(payload, encoding="utf-8", newline="\n")
    (pair / "correctness-gate.json").write_text(payload, encoding="utf-8", newline="\n")
    print(json.dumps({
        "result": "PASS",
        "source_commit": SOURCE_COMMIT,
        "baseline_dso_sha256": BASELINE_SHA256,
        "candidate_dso_sha256": CANDIDATE_SHA256,
        "checks": required_checks,
        "artifact_count": len(gate["artifacts"]),
        "pytest": {side: suites[side]["pytest"] for side in suites},
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
