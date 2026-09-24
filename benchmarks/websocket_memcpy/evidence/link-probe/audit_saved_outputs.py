#!/usr/bin/env python3
"""Re-evaluate run-05 ELF text evidence without rebuilding or loading a DSO."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re


EXPECTED_CURRENT_PARSER_SHA256 = "61fb501c7b78a868a5f4723ce11a617db970372e229c69b78598a134ea52ecd7"
RUN_NAME = "run-20260924-05"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def instruction_rows(disassembly_text: str, start: int, size: int) -> list[str]:
    rows = []
    for line in disassembly_text.splitlines():
        match = re.match(
            r"\s*([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{2}\s+)+)\s*(.*)$", line)
        if not match:
            continue
        address = int(match.group(1), 16)
        if start <= address < start + size:
            rows.append(
                f"{address:016x}: {match.group(2).strip()} {match.group(3).strip()}".rstrip())
    return rows


def main() -> int:
    evidence_dir = Path(__file__).resolve().parent
    repo_root = evidence_dir.parents[3]
    parser_path = repo_root / "benchmarks/websocket_memcpy/link_probe.py"
    parser_sha = sha256(parser_path)
    require(parser_sha == EXPECTED_CURRENT_PARSER_SHA256,
            "current link_probe.py changed from the parser version audited here")

    spec = importlib.util.spec_from_file_location("saved_link_probe_parser", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the current link-probe parser")
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)

    run_dir = evidence_dir / "attempts" / RUN_NAME
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("status") == "link_feasible", "run-05 status is not link_feasible")
    require(manifest.get("source_hashes_unchanged") is True,
            "run-05 source hashes were not stable")
    require(manifest.get("source_sha256_before_build") ==
            manifest.get("source_sha256_after_build"),
            "run-05 before/after source-hash maps differ")

    saved_script_hashes = manifest["source_sha256_before_build"]
    recorded_script_sha = next(
        value for name, value in saved_script_hashes.items()
        if name.endswith("/benchmarks/websocket_memcpy/link_probe.py")
    )

    def text(name: str) -> str:
        return (run_dir / name).read_text(encoding="utf-8")

    baseline_symbols_text = text("libmemcpy-baseline.so.symbols.txt")
    candidate_symbols_text = text("libmemcpy-candidate.so.symbols.txt")
    candidate_relocations = text("libmemcpy-candidate.so.relocations.txt")
    baseline_versions_text = text("libmemcpy-baseline.so.versions.txt")
    candidate_versions_text = text("libmemcpy-candidate.so.versions.txt")
    baseline_disassembly = text("libmemcpy-baseline.so.disassembly.txt")
    candidate_disassembly = text("libmemcpy-candidate.so.disassembly.txt")
    route_symbols_text = text("memcpy_route.o.symbols.txt")

    baseline_symbols = parser.symbol_rows(baseline_symbols_text)
    candidate_symbols = parser.symbol_rows(candidate_symbols_text)
    route_symbols = parser.symbol_rows(route_symbols_text)
    baseline_memcpy = parser.unique_symbol(baseline_symbols, "memcpy")
    candidate_memcpy = parser.unique_symbol(candidate_symbols, "memcpy")
    candidate_alias = parser.unique_symbol(candidate_symbols, "sp_wss_memcpy_test")
    baseline_callsite = parser.unique_symbol(baseline_symbols, "sp_memcpy_probe_callsite")
    candidate_callsite = parser.unique_symbol(candidate_symbols, "sp_memcpy_probe_callsite")
    route_memcpy = parser.unique_symbol(route_symbols, "memcpy")
    route_alias = parser.unique_symbol(route_symbols, "sp_wss_memcpy_test")

    branches = manifest["branch_evidence"]
    blocks = {
        "baseline_callsite": parser.block_at(
            baseline_disassembly, baseline_callsite["value"], baseline_callsite["size"]),
        "candidate_callsite": parser.block_at(
            candidate_disassembly, candidate_callsite["value"], candidate_callsite["size"]),
        "candidate_wrapper_body": parser.block_at(
            candidate_disassembly, candidate_alias["value"], candidate_alias["size"]),
    }
    plt_address_candidates = [
        address for address, label in parser.branch_targets(blocks["candidate_wrapper_body"])
        if label.startswith("memcpy@plt")
    ]
    require(len(plt_address_candidates) == 1,
            "candidate wrapper has no unique memcpy PLT branch in the bounded symbol")
    blocks["candidate_versioned_libc_plt"] = parser.block_until_next_symbol(
        candidate_disassembly, plt_address_candidates[0])

    bounds = {
        "baseline_callsite": (baseline_callsite["value"], baseline_callsite["size"]),
        "candidate_callsite": (candidate_callsite["value"], candidate_callsite["size"]),
        "candidate_wrapper_body": (candidate_alias["value"], candidate_alias["size"]),
    }
    for name, block in blocks.items():
        if name in bounds:
            start, size = bounds[name]
            disassembly = (baseline_disassembly if name == "baseline_callsite"
                           else candidate_disassembly)
            current_instructions = instruction_rows(disassembly, start, size)
            saved_instructions = instruction_rows(branches[name], start, size)
            require(current_instructions == saved_instructions,
                    f"current parser instruction rows differ from saved {name}")
        else:
            require(instruction_rows(candidate_disassembly, plt_address_candidates[0], 16) ==
                    instruction_rows(branches[name], plt_address_candidates[0], 16),
                    "current parser PLT rows differ from saved candidate versioned PLT")

    baseline_targets = parser.branch_targets(blocks["baseline_callsite"])
    candidate_targets = parser.branch_targets(blocks["candidate_callsite"])
    wrapper_targets = parser.branch_targets(blocks["candidate_wrapper_body"])
    require(baseline_memcpy["value"] in {address for address, _ in baseline_targets},
            "baseline callsite does not target the saved local memcpy")
    require(candidate_memcpy["value"] == candidate_alias["value"],
            "candidate hidden memcpy and export alias addresses differ")
    require(route_memcpy["value"] == route_alias["value"],
            "route object symbols do not share an address")
    require(candidate_memcpy["value"] in {address for address, _ in candidate_targets},
            "candidate callsite does not target its override")
    require(all(address != candidate_memcpy["value"] for address, _ in wrapper_targets),
            "candidate wrapper branches recursively to itself")
    require("memcpy@GLIBC_2.14" in candidate_relocations,
            "candidate relocation text lacks memcpy@GLIBC_2.14")
    require("memcpy@GLIBC_2.14" in blocks["candidate_versioned_libc_plt"],
            "candidate PLT text lacks the exact GLIBC_2.14 relocation")

    baseline_versions = parser.glibc_versions(baseline_versions_text)
    candidate_versions = parser.glibc_versions(candidate_versions_text)
    require(max(candidate_versions, default=(0, 0)) <= (2, 17),
            "candidate version text exceeds the recorded GLIBC 2.17 ceiling")
    require(manifest["candidate_glibc_versions"] == parser.version_text(candidate_versions),
            "candidate GLIBC versions differ from the manifest")

    runtime_status = {}
    for stem in ("baseline-callsite", "candidate-callsite", "candidate-test-alias"):
        status = json.loads(text(f"{stem}.status.json"))
        require(status.get("returncode") == 0 and not status.get("timed_out"),
                f"saved runtime child failed: {stem}")
        require(text(f"{stem}.stderr.txt") == "", f"saved runtime stderr is nonempty: {stem}")
        runtime_status[stem] = status["returncode"]

    candidate_run_guard = json.loads(
        (repo_root / "benchmarks/websocket_memcpy/evidence/guard/candidate-run05/summary.json")
        .read_text(encoding="utf-8"))
    require(candidate_run_guard["candidate_sha256"] ==
            manifest["library_sha256"]["libmemcpy-candidate.so"],
            "independent guard run DSO hash differs from run-05 manifest")

    report = {
        "result": "PASS",
        "mode": "offline saved-text audit; did not build or load a binary",
        "run_name": RUN_NAME,
        "run05_manifest_sha256": sha256(manifest_path),
        "current_parser_path": str(parser_path),
        "current_parser_sha256": parser_sha,
        "run05_recorded_parser_sha256": recorded_script_sha,
        "run05_parser_snapshot_present": False,
        "note": "The run-05 runner snapshot is not retained separately; this replays current function-size-bounded parsing against saved run-05 text.",
        "baseline_callsite_size": baseline_callsite["size"],
        "candidate_callsite_size": candidate_callsite["size"],
        "candidate_alias_size": candidate_alias["size"],
        "candidate_memcpy_alias_address_matches": candidate_memcpy["value"] == candidate_alias["value"],
        "candidate_route_object_alias_address_matches": route_memcpy["value"] == route_alias["value"],
        "candidate_callsite_target": candidate_memcpy["value"],
        "candidate_glibc_versions": parser.version_text(candidate_versions),
        "baseline_glibc_versions": parser.version_text(baseline_versions),
        "saved_runtime_children": runtime_status,
        "candidate_dso_hash_from_run05_manifest": manifest["library_sha256"]["libmemcpy-candidate.so"],
        "candidate_guard_dso_hash_matches_manifest": True,
        "current_parser_text_header_format_differs_from_saved_text": True,
        "parser_comparison": "instruction addresses/bytes/operands match within recorded FUNC bounds; label-line separator is normalized",
        "checked_text_files": sorted(manifest["evidence_files"]),
    }
    destination = evidence_dir / "audit-result.json"
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8", newline="\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
