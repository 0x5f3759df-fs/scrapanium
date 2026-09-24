"""Ignored-build runner for paired baseline/candidate WSS memcpy checks.

Full measurements require the explicit --run-full switch. The --smoke mode is
correctness-only (32 messages per workload) and retains the same validation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build/wss-memcpy-backend-pair"
PAIR_ROOT_DEFAULT = Path("/home/baidu/scrapanium-experiments/wss-memcpy-backend-pair-20260924")
CLIENT_SOURCE_REV = "411981c05167fa54065fa3d8757bac3c734b19f6"
sys.path[:0] = [str(ROOT / "benchmarks"), str(ROOT / "tests"), str(ROOT / "scripts")]
import websocket_streaming as streaming  # noqa: E402


SEED = 20260924
REPEATS = 20
CLIENT_BEND, CLIENT_PY = streaming.CLIENTS
BACKEND_NAMES = ("baseline", "candidate")
CONDITIONS = (
    {"id": "baseline-bend", "backend": "baseline", "client": CLIENT_BEND},
    {"id": "baseline-curl-cffi", "backend": "baseline", "client": CLIENT_PY},
    {"id": "candidate-bend", "backend": "candidate", "client": CLIENT_BEND},
    {"id": "candidate-curl-cffi", "backend": "candidate", "client": CLIENT_PY},
)
# Williams design for four conditions: each condition occupies each period
# five times over 20 repeats and each directed adjacent pair is represented.
WILLIAMS_ROWS = (
    (0, 1, 3, 2),
    (1, 2, 0, 3),
    (2, 3, 1, 0),
    (3, 0, 2, 1),
)
FAULTS = ("corrupt", "swap")
HEADER_BYTES = lambda size: 2 if size < 126 else (4 if size < 65536 else 10)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_inventory(root: Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows[relative] = {"kind": "symlink", "target": path.readlink().as_posix()}
        elif path.is_file():
            rows[relative] = {"kind": "file", "size_bytes": path.stat().st_size, "sha256": digest(path)}
    return rows


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def source_inventory() -> dict[str, str]:
    paths: set[Path] = {
        ROOT / "dependencies.json",
        ROOT / "scrapanium.bend",
        ROOT / "http.bend",
        ROOT / "scripts/build.py",
        ROOT / "tests/lab.py",
        ROOT / "tests/ws_lab.py",
        ROOT / "benchmarks/websocket.py",
        ROOT / "benchmarks/websocket_streaming.py",
        ROOT / "benchmarks/websocket_stream.bend",
        ROOT / "benchmarks/websocket_stream_python.py",
        ROOT / "benchmarks/websocket_stream_server.go",
        ROOT / "benchmarks/websocket_clock.c",
        ROOT / "benchmarks/websocket.bend",
    }
    paths.update(p for p in (ROOT / "native").rglob("*") if p.is_file())
    inventory: dict[str, str] = {}
    for rel in sorted(paths):
        if not rel.is_file():
            raise RuntimeError(f"shared runtime source is missing: {rel}")
        inventory[f"main/{rel.relative_to(ROOT).as_posix()}"] = digest(rel)
    isolated = BUILD / "client-root"
    isolated_hashes: dict[str, str] = {}
    for rel in sorted(paths):
        relative = rel.relative_to(ROOT)
        path = isolated / relative
        if not path.is_file():
            raise RuntimeError(f"shared isolated client source is missing: {path}")
        isolated_hashes[relative.as_posix()] = digest(path)
        inventory[f"client-root/{relative.as_posix()}"] = digest(path)
    main_hashes = {key.removeprefix("main/"): value for key, value in inventory.items()
                   if key.startswith("main/")}
    if isolated_hashes != main_hashes:
        mismatches = {key: (main_hashes.get(key), isolated_hashes.get(key))
                      for key in main_hashes.keys() | isolated_hashes.keys()
                      if main_hashes.get(key) != isolated_hashes.get(key)}
        raise RuntimeError(f"shared client/runtime sources differ between main and isolated source root: {mismatches}")
    inventory[f"runner/{Path(__file__).resolve().relative_to(ROOT).as_posix()}"] = digest(Path(__file__).resolve())
    return inventory


def git_text(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def command_output(command: list[str]) -> str:
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()


def build_peer(path: Path) -> dict[str, str]:
    command = ["go", "build", "-trimpath", "-o", str(path), str(ROOT / "benchmarks/websocket_stream_server.go")]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    (path.parent / "peer-build.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (path.parent / "peer-build.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(f"Go peer build failed ({completed.returncode}): {completed.stderr}")
    return {"command": " ".join(command), "sha256": digest(path), "path": str(path.resolve())}


def backend_info(pair_root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    baseline_manifest_path = pair_root / "baseline-provenance.json"
    candidate_manifest_path = pair_root / "candidate-provenance.json"
    baseline_manifest = json.loads(baseline_manifest_path.read_text(encoding="utf-8"))
    candidate_manifest = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
    candidate_elf = candidate_manifest.get("elf_evidence", {}).get("checks", {})
    symbols = candidate_elf.get("symbols", {})
    dynamic = candidate_elf.get("dynamic", {})
    glibc = candidate_elf.get("glibc_versions", {})
    if (symbols.get("candidate_route_same_address") is not True
            or symbols.get("candidate_route_body_calls_memcpy_plt") is not True
            or symbols.get("candidate_versioned_relocation") != "memcpy@GLIBC_2.14"
            or dynamic.get("needed_identical") is not True
            or glibc.get("candidate_within_2_17") is not True):
        raise RuntimeError("candidate provenance lacks passing real-DSO memcpy/GLIBC evidence")
    expected_baseline = baseline_manifest["baseline"]
    expected_candidate = candidate_manifest["candidate_dso"]
    if candidate_manifest.get("baseline_dso", {}).get("sha256") != expected_baseline["sha256"]:
        raise RuntimeError("candidate manifest baseline identity does not match frozen natural baseline")
    manifest_rows = {"baseline": (baseline_manifest_path, baseline_manifest),
                     "candidate": (candidate_manifest_path, candidate_manifest)}
    expected_rows = {"baseline": expected_baseline, "candidate": expected_candidate}
    object_map_path = pair_root / "baseline-candidate-object-map.json"
    object_map = json.loads(object_map_path.read_text(encoding="utf-8"))
    base_inputs = object_map.get("baseline_inputs", [])
    candidate_inputs = object_map.get("candidate_inputs", [])
    route_row = candidate_manifest.get("route", {})
    route_input = {"path": route_row.get("object"), "sha256": route_row.get("object_sha256"), "kind": "route_object"}
    if (len(base_inputs) != 209 or len(candidate_inputs) != 210
            or object_map.get("only_input_addition") != route_input
            or base_inputs != [row for row in candidate_inputs if row.get("kind") != "route_object"]):
        raise RuntimeError("baseline/candidate object map does not contain exactly the reviewed single-route-object delta")
    for row in candidate_inputs:
        input_path = Path(str(row["path"]))
        if not input_path.is_file() or digest(input_path) != row["sha256"]:
            raise RuntimeError(f"shared-link input no longer matches frozen object map: {input_path}")
    elf_record = candidate_manifest.get("elf_evidence", {})
    elf_path = Path(str(elf_record.get("path", "")))
    if not elf_path.is_file() or digest(elf_path) != elf_record.get("sha256"):
        raise RuntimeError("candidate ELF inspection artifact is missing or changed")
    candidate_command_path = Path(str(candidate_manifest.get("link", {}).get("candidate_command_file", "")))
    candidate_command_json_path = Path(str(candidate_manifest.get("link", {}).get("candidate_command_json", "")))
    baseline_command_path = pair_root / "baseline-link-command.txt"
    if not candidate_command_path.is_file() or not candidate_command_json_path.is_file() or not baseline_command_path.is_file():
        raise RuntimeError("candidate exact link command is missing")
    for name in BACKEND_NAMES:
        manifest_path, provenance = manifest_rows[name]
        row = expected_rows[name]
        library_path = Path(str(row["path"])).resolve()
        prefix = library_path.parent.parent.resolve()
        if not library_path.is_file() or digest(library_path) != row["sha256"]:
            raise RuntimeError(f"backend DSO differs from frozen provenance ({name}): {library_path}")
        if library_path.name != "libcurl-impersonate.so.4.8.0" or not (prefix / "include").is_dir():
            raise RuntimeError(f"backend prefix is not a complete isolated install ({name}): {prefix}")
        if name == "candidate" and provenance.get("route", {}).get("object_sha256") != "a5fb141075277aa556c968c3aa88de0d73d7c93dff67874c43907067ed3f2835":
            raise RuntimeError("candidate route-object hash is not the independently reviewed object")
        result[name] = {
            "prefix": str(prefix),
            "library_path": str(library_path),
            "library_sha256": digest(library_path),
            "manifest_path": str(manifest_path.resolve()),
            "manifest_sha256": digest(manifest_path),
            "link_input_count": len(base_inputs) if name == "baseline" else len(candidate_inputs),
            "provenance_path": str(manifest_path.resolve()),
            "provenance_sha256": digest(manifest_path),
            "provenance": provenance,
            "object_map_path": str(object_map_path.resolve()),
            "object_map_sha256": digest(object_map_path),
            "elf_evidence_path": str(elf_path.resolve()),
            "elf_evidence_sha256": digest(elf_path),
            "link_command_path": str(candidate_command_path.resolve()) if name == "candidate" else str((pair_root / "baseline-link-command.txt").resolve()),
            "link_command_sha256": digest(candidate_command_path) if name == "candidate" else digest(baseline_command_path),
            "candidate_command_json_path": str(candidate_command_json_path.resolve()),
            "candidate_command_json_sha256": digest(candidate_command_json_path),
        }
    baseline_prefix = Path(result["baseline"]["prefix"])
    candidate_prefix = Path(result["candidate"]["prefix"])
    baseline_files = file_inventory(baseline_prefix)
    candidate_files = file_inventory(candidate_prefix)
    dso_relative = "lib/libcurl-impersonate.so.4.8.0"
    baseline_other = {key: value for key, value in baseline_files.items() if key != dso_relative}
    candidate_other = {key: value for key, value in candidate_files.items() if key != dso_relative}
    if baseline_other != candidate_other:
        changed = {key: (baseline_other.get(key), candidate_other.get(key))
                   for key in baseline_other.keys() | candidate_other.keys()
                   if baseline_other.get(key) != candidate_other.get(key)}
        raise RuntimeError(f"baseline and candidate install prefixes differ outside the versioned DSO: {changed}")
    result["baseline"]["prefix_inventory_sha256"] = hashlib.sha256(json.dumps(baseline_files, sort_keys=True).encode()).hexdigest()
    result["candidate"]["prefix_inventory_sha256"] = hashlib.sha256(json.dumps(candidate_files, sort_keys=True).encode()).hexdigest()
    result["baseline"]["prefix_non_dso_files_identical"] = True
    result["candidate"]["prefix_non_dso_files_identical"] = True
    return result


def client_info(library_dir: Path) -> dict[str, object]:
    isolated = BUILD / "client-root"
    binary = isolated / "build/websocket_stream"
    bridge = isolated / "build/libscrapanium.so"
    if not binary.is_file() or not bridge.is_file():
        raise RuntimeError("missing shared isolated client build")
    compile_prefix = library_dir.parent
    return {
        "root": str(isolated.resolve()),
        "stream_binary": str(binary.resolve()),
        "stream_binary_sha256": digest(binary),
        "compile_backend": "baseline headers and link inputs; runtime DSO selected only by per-condition LD_LIBRARY_PATH",
        "compile_backend_prefix": str(compile_prefix.resolve()),
        "bridge_library": str(bridge.resolve()),
        "bridge_library_sha256": digest(bridge),
        "compile_backend_library_dir": str(library_dir.resolve()),
        "native_websocket_sha256": digest(isolated / "native/websocket.inc.c"),
    }


def python_binding(backend: str, library_dir: Path) -> dict[str, object]:
    python = ROOT / ".deps/venv-matched/bin/python"
    env = {**os.environ, "LD_LIBRARY_PATH": str(library_dir.resolve())}
    env.pop("LD_PRELOAD", None)
    env.pop("SCRAPANIUM_CURL_DIR", None)
    source = (
        "import curl_cffi,hashlib,json,pathlib,sys; p=pathlib.Path(curl_cffi.__file__).parent; "
        "files=[p/'curl.py',p/'requests/websockets.py',*p.glob('_wrapper*.so')]; "
        "print(json.dumps({'version':curl_cffi.__version__,'python':sys.version,'package':str(p),"
        "'files':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}))"
    )
    info = json.loads(subprocess.check_output([str(python), "-c", source], text=True, env=env))
    info["interpreter"] = str(python.resolve())
    info["interpreter_sha256"] = digest(python.resolve())
    info["backend_context"] = backend
    return info


def compiler_info() -> dict[str, object]:
    clang = Path("/usr/bin/clang").resolve()
    clangxx = Path("/usr/bin/clang++").resolve()
    bun = ROOT / ".deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun"
    go = Path(shutil.which("go") or "go").resolve()
    bend = ROOT / ".deps/bend"
    bend_rev = None
    bend_status = None
    if (bend / ".git").exists():
        bend_rev = command_output(["git", "-C", str(bend), "rev-parse", "HEAD"])
        bend_status = command_output(["git", "-C", str(bend), "status", "--porcelain"])
        if bend_status:
            raise RuntimeError(f"pinned Bend compiler checkout is dirty: {bend_status}")
    return {
        "clang_path": str(clang), "clang_sha256": digest(clang),
        "clang_version": command_output([str(clang), "--version"]),
        "clangxx_path": str(clangxx), "clangxx_sha256": digest(clangxx),
        "clangxx_version": command_output([str(clangxx), "--version"]),
        "cmake_version": command_output(["cmake", "--version"]),
        "go_path": str(go), "go_sha256": digest(go),
        "go_version": command_output([str(go), "version"]),
        "bun_path": str(bun.resolve()), "bun_sha256": digest(bun),
        "bun_version": command_output([str(bun), "--version"]),
        "bend_source": str(bend.resolve()), "bend_source_revision": bend_rev,
        "bend_source_dirty": bend_status,
        "python": sys.version,
        "platform": platform.platform(), "cpu_count": os.cpu_count(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
    }


def make_plan(smoke: bool) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    rng = random.Random(SEED)
    workloads = [dict(w) for w in streaming.WORKLOADS]
    if smoke:
        for workload in workloads:
            workload["count"] = 32
    repeats = 1 if smoke else REPEATS
    negative_plan = [
        {"kind": "negative", "backend": backend, "client": client, "fault": fault,
         "run_id": f"negative-{fault}-{backend}-{client}"}
        for backend in BACKEND_NAMES for client in streaming.CLIENTS for fault in FAULTS
    ]
    rng.shuffle(negative_plan)
    row_order: dict[str, list[int]] = {}
    for workload in workloads:
        if smoke:
            row_order[workload["name"]] = [rng.randrange(len(WILLIAMS_ROWS))]
        else:
            indices = list(range(len(WILLIAMS_ROWS))) * (REPEATS // len(WILLIAMS_ROWS))
            rng.shuffle(indices)
            row_order[workload["name"]] = indices
    positive_plan: list[dict[str, object]] = []
    repeat_workloads: list[list[dict[str, object]]] = []
    for _ in range(repeats):
        ordered = [dict(w) for w in workloads]
        rng.shuffle(ordered)
        repeat_workloads.append(ordered)
    counters = {name: 0 for name in row_order}
    for repeat, ordered in enumerate(repeat_workloads):
        for workload in ordered:
            name = workload["name"]
            row = WILLIAMS_ROWS[row_order[name][counters[name]]]
            counters[name] += 1
            for condition_index in row:
                condition = CONDITIONS[condition_index]
                positive_plan.append({
                    "kind": "positive", "repeat": repeat, "workload": workload,
                    "condition": dict(condition),
                    "run_id": f"{name}-r{repeat:02d}-{condition['id']}",
                })
    counts: dict[str, object] = {
        "negative_controls": len(negative_plan),
        "positive_measurements": len(positive_plan),
        "total_attempts": len(negative_plan) + len(positive_plan),
        "repeats_per_workload": repeats,
        "workloads": workloads,
        "conditions": list(CONDITIONS),
        "williams_rows": [[CONDITIONS[i]["id"] for i in row] for row in WILLIAMS_ROWS],
        "row_counts_by_workload": {
            name: {str(row_id): row_order[name].count(row_id) for row_id in range(len(WILLIAMS_ROWS))}
            for name in row_order
        },
        "seed": SEED,
        "schedule_description": (
            "Smoke selects one seeded Williams row per workload. Full mode uses each of the four Williams "
            "rows five times per workload. Row order and workload order are generated from the fixed seed."
        ),
        "adaptive_extension": False,
        "adoption_gate": {
            "speed_ratio": "baseline elapsed / candidate elapsed; values greater than 1 favor candidate",
            "primary_64k_bend": "both 65,536-byte flush modes require median ratio >= 1.05 and paired 95% CI lower bound > 1.0",
            "small_and_medium_bend": "all four 30-byte/1,024-byte × flush1/flush64 cells require paired 95% CI lower bound >= 0.95; curl_cffi cells are retained and reported separately",
            "memcpy_route_prerequisites": "the candidate SSL_read/SSL_peek callsites must target the verified wrapper, the wrapper must target memcpy@GLIBC_2.14, the independent guard fixture and baseline/candidate full WebSocket suites must pass, and both DSOs must remain within the pinned GLIBC_2.17 ceiling",
            "call_counts": "not a promotion gate; this experiment changes memcpy dispatch, not WebSocket receive-call count",
        },
        "bootstrap_resamples": 10000,
    }
    validate_plan(negative_plan, positive_plan, counts, smoke)
    return negative_plan, positive_plan, counts


def validate_plan(negative: list[dict[str, object]], positive: list[dict[str, object]],
                  counts: dict[str, object], smoke: bool) -> None:
    negative_keys = {(row["backend"], row["client"], row["fault"]) for row in negative}
    expected_negative = {(backend, client, fault) for backend in BACKEND_NAMES
                         for client in streaming.CLIENTS for fault in FAULTS}
    if len(negative) != 8 or negative_keys != expected_negative or len({r["run_id"] for r in negative}) != 8:
        raise ValueError("negative controls are not the exact backend×client×fault product")
    repeats = 1 if smoke else REPEATS
    expected = len(streaming.WORKLOADS) * repeats * len(CONDITIONS)
    if len(positive) != expected or len({r["run_id"] for r in positive}) != expected:
        raise ValueError(f"positive plan count/IDs invalid: {len(positive)} vs {expected}")
    by_workload: dict[str, list[dict[str, object]]] = {w["name"]: [] for w in streaming.WORKLOADS}
    for row in positive:
        by_workload[row["workload"]["name"]].append(row)
    row_lookup = {tuple(CONDITIONS[i]["id"] for i in row) for row in WILLIAMS_ROWS}
    for name, rows in by_workload.items():
        if len(rows) != repeats * len(CONDITIONS):
            raise ValueError(f"wrong positive count for {name}")
        grouped: dict[int, list[str]] = {}
        for row in rows:
            grouped.setdefault(int(row["repeat"]), []).append(row["condition"]["id"])
        for group in grouped.values():
            if tuple(group) not in row_lookup:
                raise ValueError(f"non-Williams row in schedule for {name}: {group}")
        if not smoke:
            row_counts = counts["row_counts_by_workload"][name]
            if set(row_counts.values()) != {REPEATS // len(WILLIAMS_ROWS)}:
                raise ValueError(f"Williams row counts are not balanced for {name}: {row_counts}")
    if counts["total_attempts"] != len(negative) + len(positive):
        raise ValueError("planned attempt count is inconsistent")


def print_plan(smoke: bool) -> None:
    negative, positive, counts = make_plan(smoke)
    print(json.dumps({
        "mode": "correctness-smoke-only" if smoke else "fixed-full-plan-not-executed",
        **counts,
        "first_negative_controls": negative[:2],
        "first_positive_rows": positive[:8],
        "workloads_are_six": len(counts["workloads"]) == 6,
    }, indent=2, sort_keys=True))


def archive_client_root(destination: Path) -> dict[str, object]:
    if destination.exists():
        raise FileExistsError(f"refusing to reuse isolated measurement client root: {destination}")
    destination.mkdir(parents=True)
    archive_path = BUILD / f"{destination.name}-{CLIENT_SOURCE_REV[:12]}.tar"
    if archive_path.exists():
        raise FileExistsError(f"refusing to reuse archive path: {archive_path}")
    archive_command = ["git", "archive", "--format=tar", f"--output={archive_path}", CLIENT_SOURCE_REV]
    subprocess.run(archive_command, cwd=ROOT, check=True)
    archive_sha256 = digest(archive_path)
    try:
        import tarfile
        with tarfile.open(archive_path, "r:") as archive:
            archive.extractall(destination, filter="data")
    finally:
        archive_path.unlink(missing_ok=True)
    os.symlink((ROOT / ".deps").resolve(), destination / ".deps", target_is_directory=True)
    if (destination / ".deps").resolve() != (ROOT / ".deps").resolve():
        raise RuntimeError(f"isolated client .deps link does not resolve to pinned workspace deps: {destination}")
    return {
        "argv": archive_command,
        "cwd": str(ROOT.resolve()),
        "source_revision": CLIENT_SOURCE_REV,
        "archive_sha256": archive_sha256,
        "archive_removed_after_extraction": not archive_path.exists(),
        "destination": str(destination.resolve()),
        "deps_symlink_target": str((ROOT / ".deps").resolve()),
    }


def build_measurement_client(backend_info_row: dict[str, object]) -> dict[str, object]:
    client_root = BUILD / "client-root"
    source_archive = archive_client_root(client_root)
    source_hashes_before = source_inventory()
    env = os.environ.copy()
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "ASAN_OPTIONS", "UBSAN_OPTIONS",
                 "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"):
        env.pop(name, None)
    backend_prefix = Path(str(backend_info_row["prefix"]))
    env.update({
        "SCRAPANIUM_CURL_DIR": str(backend_prefix),
        "BEND_SOURCE": str((ROOT / ".deps/bend").resolve()),
        "BUN": str((ROOT / ".deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun").resolve()),
        "CC": "/usr/bin/clang",
    })
    bridge = client_root / "build/libscrapanium.so"
    binary = client_root / "build/websocket_stream"
    build_log_dir = BUILD / "client-build-logs" / "shared"
    build_log_dir.mkdir(parents=True, exist_ok=False)
    commands = [
        [sys.executable, "scripts/build.py", "-o", str(bridge)],
        [sys.executable, "scripts/build.py", "benchmarks/websocket_stream.bend", "-o", str(binary)],
    ]
    build_records = []
    for index, command in enumerate(commands):
        completed = subprocess.run(command, cwd=client_root, env=env, capture_output=True, text=True)
        (build_log_dir / f"step-{index + 1}.stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (build_log_dir / f"step-{index + 1}.stderr.txt").write_text(completed.stderr, encoding="utf-8")
        build_records.append({
            "argv": command, "cwd": str(client_root.resolve()),
            "returncode": completed.returncode,
            "stdout_path": str(build_log_dir / f"step-{index + 1}.stdout.txt"),
            "stderr_path": str(build_log_dir / f"step-{index + 1}.stderr.txt"),
        })
        if completed.returncode:
            raise RuntimeError(f"isolated shared-client build step {index + 1} failed: {completed.stderr}")
    if not binary.is_file() or not bridge.is_file():
        raise RuntimeError("isolated shared-client build did not produce the stream client and native bridge")
    source_hashes_after = source_inventory()
    if source_hashes_before != source_hashes_after:
        raise RuntimeError("client/runtime source hashes changed while the shared client was built")
    return {
        "client_bytes_shared_across_backend_conditions": True,
        "source_revision": CLIENT_SOURCE_REV,
        "source_archive": source_archive,
        "root": str(client_root.resolve()),
        "compile_backend": "baseline",
        "compile_backend_prefix": str(backend_prefix.resolve()),
        "build_environment": {key: env[key] for key in ("SCRAPANIUM_CURL_DIR", "BEND_SOURCE", "BUN", "CC")},
        "build_commands": build_records,
        "stream_binary": str(binary.resolve()), "stream_binary_sha256": digest(binary),
        "bridge_library": str(bridge.resolve()), "bridge_library_sha256": digest(bridge),
        "build_logs": str(build_log_dir.resolve()),
        "source_sha256_before_build": source_hashes_before,
        "source_sha256_after_build": source_hashes_after,
        "reuse_policy": "Both backend conditions use these exact same immutable executable and bridge files; runtime backend selection is per-process LD_LIBRARY_PATH.",
    }


def prepare_measurement_clients(backends: dict[str, dict[str, object]]) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    root_dir = BUILD / "client-root"
    if root_dir.exists():
        raise FileExistsError(f"refusing to reuse shared measurement client root: {root_dir}")
    build_record = build_measurement_client(backends["baseline"])
    shared_client = client_info(Path(backends["baseline"]["library_path"]).parent)
    if (shared_client["stream_binary_sha256"] != build_record["stream_binary_sha256"]
            or shared_client["bridge_library_sha256"] != build_record["bridge_library_sha256"]):
        raise RuntimeError("client provenance does not match the single shared build record")
    reused = {
        name: {**shared_client, "runtime_backend": name,
               "runtime_library_path": backends[name]["library_path"],
               "runtime_library_sha256": backends[name]["library_sha256"]}
        for name in BACKEND_NAMES
    }
    if (reused["baseline"]["stream_binary_sha256"] != reused["candidate"]["stream_binary_sha256"]
            or reused["baseline"]["bridge_library_sha256"] != reused["candidate"]["bridge_library_sha256"]
            or reused["baseline"]["stream_binary"] != reused["candidate"]["stream_binary"]
            or reused["baseline"]["bridge_library"] != reused["candidate"]["bridge_library"]):
        raise RuntimeError("backend conditions do not reuse identical client and bridge bytes")
    return build_record, reused


def validate_correctness_gate(path: Path, backends: dict[str, dict[str, object]]) -> dict[str, object]:
    gate = json.loads(path.read_text(encoding="utf-8"))
    if gate.get("schema") != "scrapanium-wss-memcpy-correctness-gate-v1":
        raise RuntimeError("correctness gate has an unknown schema")
    if gate.get("client_source_revision") != CLIENT_SOURCE_REV or gate.get("no_performance_data") is not True:
        raise RuntimeError("correctness gate must bind the frozen client revision and contain correctness-only evidence")
    expected = {
        "baseline_dso_sha256": backends["baseline"]["library_sha256"],
        "candidate_dso_sha256": backends["candidate"]["library_sha256"],
    }
    for key, value in expected.items():
        if gate.get(key) != value:
            raise RuntimeError(f"correctness gate {key} does not match the current pair")
    checks = gate.get("checks", {})
    required = ("guard_fixture", "baseline_full_suite", "candidate_full_suite",
                "baseline_ws_sanitizer", "candidate_ws_sanitizer",
                "baseline_phase_sanitizer", "candidate_phase_sanitizer",
                "baseline_attribution_sanitizer", "candidate_attribution_sanitizer")
    if any(checks.get(name) is not True for name in required):
        raise RuntimeError(f"full measurement requires passing correctness checks {required}; observed {checks}")
    artifacts = gate.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError("correctness gate must retain at least one hashed artifact")
    for row in artifacts:
        artifact = Path(str(row.get("path", "")))
        if not artifact.is_file() or digest(artifact) != row.get("sha256"):
            raise RuntimeError(f"correctness-gate artifact missing or hash mismatch: {row}")
    return gate


def read_stats(peer: subprocess.Popen[str], run_id: str, workload: dict[str, object],
               require_full: bool, timeout: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    observed: dict[str, object] | None = None
    while time.monotonic() < deadline:
        peer.stdin.write("stats\n")
        peer.stdin.flush()
        records = json.loads(streaming.shared.line(peer, timeout=max(0.1, deadline - time.monotonic())))
        observed = records.get(run_id)
        if observed is not None:
            if observed.get("error") or observed.get("data_frames") == workload["count"]:
                break
        time.sleep(0.01)
    if observed is None:
        raise RuntimeError(f"peer did not publish stats for {run_id}")
    if observed.get("warmups") != 1 or observed.get("starts") != 1:
        raise RuntimeError(f"peer warmup/start counters invalid for {run_id}: {observed}")
    if require_full:
        size, count = int(workload["bytes"]), int(workload["count"])
        expected_payload = count * size
        expected_frames = count * (size + HEADER_BYTES(size))
        if (observed.get("error") or observed.get("data_frames") != count
                or observed.get("data_payload_bytes") != expected_payload
                or observed.get("data_frame_bytes") != expected_frames):
            raise RuntimeError(f"peer frame/payload totals invalid for {run_id}: {observed}")
    return observed


def validate_mapping(result: dict[str, object], backend_info: dict[str, object], backend: str) -> None:
    mapping = result.get("mapped_backend")
    if not isinstance(mapping, dict):
        raise RuntimeError(f"client did not retain mapped backend evidence for {backend}")
    expected_hash = str(backend_info["library_sha256"])
    expected_dir = Path(backend_info["library_path"]).parent.resolve()
    mapped_path = Path(str(mapping.get("path", ""))).resolve()
    allowed_names = {"libcurl-impersonate.so", "libcurl-impersonate.so.4", "libcurl-impersonate.so.4.8.0"}
    if (mapped_path.parent != expected_dir or mapped_path.name not in allowed_names
            or mapping.get("sha256") != expected_hash):
        raise RuntimeError(
            f"mapped backend does not belong to the planned {backend} snapshot: {mapping}; "
            f"expected directory={expected_dir}, SHA256={expected_hash}"
        )


def matched_binding(backends: dict[str, dict[str, object]], backend: str) -> dict[str, object]:
    library_dir = Path(backends[backend]["library_path"]).parent
    return python_binding(backend, library_dir)


def execute_one(item: dict[str, object], backends: dict[str, dict[str, object]],
                clients: dict[str, dict[str, object]], peer: subprocess.Popen[str], body: Path,
                evidence: dict[str, object]) -> dict[str, object]:
    if item["kind"] == "negative":
        backend, client = str(item["backend"]), str(item["client"])
        workload = {"bytes": 30, "count": 32, "peer_flush_frames": 64, "connections": 1}
        body.write_bytes(bytes((i * 31) % 128 for i in range(workload["bytes"] - 8)))
        result = streaming.sample(client, Path(clients[backend]["stream_binary"]), CURRENT_URL,
            CURRENT_CA, body, workload, str(item["run_id"]), make_client_env(backends, backend),
            str(backends[backend]["library_sha256"]), 1, str(item["fault"]))
        validate_mapping(result, backends[backend], backend)
        evidence["client_result"] = result
        peer_record = read_stats(peer, str(item["run_id"]), workload, require_full=False)
        evidence["peer_observation"] = peer_record
        return evidence

    condition = item["condition"]
    backend, client = str(condition["backend"]), str(condition["client"])
    workload = dict(item["workload"])
    body.write_bytes(bytes((i * 31) % 128 for i in range(int(workload["bytes"]) - 8)))
    result = streaming.sample(client, Path(clients[backend]["stream_binary"]), CURRENT_URL,
        CURRENT_CA, body, workload, str(item["run_id"]), make_client_env(backends, backend),
        str(backends[backend]["library_sha256"]), 1)
    validate_mapping(result, backends[backend], backend)
    evidence["client_result"] = result
    peer_record = read_stats(peer, str(item["run_id"]), workload, require_full=True)
    expected_map = str(backends[backend]["library_sha256"])
    if result["mapped_backend"].get("sha256") != expected_map:
        raise RuntimeError(f"client mapped an unexpected backend for {item['run_id']}")
    evidence["peer_observation"] = peer_record
    return evidence


def make_client_env(backends: dict[str, dict[str, object]], backend: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    env.pop("SCRAPANIUM_CURL_DIR", None)
    env.pop("SCRAPANIUM_WSATTR_MODE", None)
    env.pop("SCRAPANIUM_WSATTR_PATH", None)
    env["LD_LIBRARY_PATH"] = str(Path(backends[backend]["library_path"]).parent)
    return env


def summarize(rows: list[dict[str, object]], smoke: bool) -> dict[str, object]:
    grouped: dict[str, dict[str, dict[int, dict[str, object]]]] = {
        workload["name"]: {condition["id"]: {} for condition in CONDITIONS}
        for workload in streaming.WORKLOADS
    }
    for row in rows:
        item = row["plan"]
        condition = item["condition"]
        result = row["result"]
        client_result = result.get("client_result", result)
        grouped[item["workload"]["name"]][condition["id"]][int(item["repeat"])] = client_result
    summary: dict[str, object] = {"workloads": {}}
    for workload_name, conditions in grouped.items():
        workload_summary: dict[str, object] = {
            "conditions": {}, "paired_speed_ratios": {}, "matched_client_speed_ratios": {},
        }
        by_condition = conditions
        for condition_id, repeats in conditions.items():
            values = list(repeats.values())
            times = [float(value["elapsed_ms"]) for value in values]
            workload_summary["conditions"][condition_id] = {
                "samples": len(values), "median_elapsed_ms": statistics.median(times),
                "elapsed_ms_by_repeat": {str(repeat): float(value["elapsed_ms"])
                                         for repeat, value in sorted(repeats.items())},
            }
        for client in (CLIENT_BEND, CLIENT_PY):
            baseline_id = f"baseline-{ 'bend' if client == CLIENT_BEND else 'curl-cffi' }"
            candidate_id = f"candidate-{ 'bend' if client == CLIENT_BEND else 'curl-cffi' }"
            shared_repeats = sorted(set(by_condition[baseline_id]) & set(by_condition[candidate_id]))
            ratios = [float(by_condition[baseline_id][r]["elapsed_ms"])
                      / float(by_condition[candidate_id][r]["elapsed_ms"])
                      for r in shared_repeats]
            workload_summary["paired_speed_ratios"][client] = {
                "definition": "baseline elapsed / candidate elapsed; values > 1 favor candidate",
                "median": statistics.median(ratios), "samples": ratios,
                "bootstrap_95_ci": None if smoke else streaming.shared.interval(ratios, SEED),
                "inference": "correctness smoke; no performance inference" if smoke else
                             "paired repeat-level bootstrap CI of the median ratio",
            }
        for backend in BACKEND_NAMES:
            bend_id = f"{backend}-bend"
            py_id = f"{backend}-curl-cffi"
            shared_repeats = sorted(set(by_condition[bend_id]) & set(by_condition[py_id]))
            ratios = [float(by_condition[py_id][r]["elapsed_ms"])
                      / float(by_condition[bend_id][r]["elapsed_ms"])
                      for r in shared_repeats]
            workload_summary["matched_client_speed_ratios"][backend] = {
                "definition": "curl_cffi elapsed / Bend elapsed; values > 1 favor Bend",
                "median": statistics.median(ratios), "samples": ratios,
                "bootstrap_95_ci": None if smoke else streaming.shared.interval(ratios, SEED + 1),
            }
        summary["workloads"][workload_name] = workload_summary
    return summary


def run(smoke: bool, output: Path, pair_root: Path, correctness_gate_path: Path) -> None:
    if output.exists():
        raise RuntimeError(f"refusing to overwrite existing output: {output}")
    if BUILD.exists():
        raise RuntimeError(f"refusing to reuse measurement build root: {BUILD}")
    if not output.resolve().is_relative_to(BUILD.resolve()):
        raise RuntimeError("output directory must be inside the fresh --build-root")
    if "LD_PRELOAD" in os.environ:
        raise RuntimeError("LD_PRELOAD must be absent for the uninstrumented streaming run")
    backends = backend_info(pair_root)
    correctness_gate = None if smoke else validate_correctness_gate(correctness_gate_path, backends)
    output.mkdir(parents=True)
    negative_plan, positive_plan, plan_counts = make_plan(smoke)
    (output / "samples.jsonl").touch(exist_ok=False)
    manifest: dict[str, object] = {
        "schema": 1, "created_utc": utc_now(),
        "status": "preparing", "mode": "correctness-smoke-only" if smoke else "fixed-full-measurement",
        "smoke_only": smoke, "timings_are_performance_evidence": not smoke,
        "seed": SEED, "plan_counts": plan_counts,
        "negative_plan": negative_plan, "positive_plan": positive_plan,
        "pair_root": str(pair_root.resolve()),
        "correctness_gate_path": str(correctness_gate_path.resolve()),
        "correctness_gate": correctness_gate,
        "timing_method": (
            "Reuses websocket_streaming.sample without modifying client code. Corpus creation, process startup, "
            "TLS upgrade, warmup, and close are outside the client interval; start signal and ordered exact "
            "opcode/sequence/full-byte checks with immediate expected/actual release are inside."
        ),
        "client_validation": "exact binary opcode, unique 8-digit sequence, full payload equality, order, and immediate release in both clients",
        "negative_controls": "corrupt and swapped sequence/payload inputs; all eight backend×client×fault cells execute before positives",
        "backend_map_validation": "each ready client process must map exactly one backend; retained mapped path must be one of the libcurl SONAME aliases in the planned prefix/lib directory and its SHA256 must match the frozen library",
        "positive_peer_validation": "one warmup and start, exact data frame/payload/frame-byte totals; no peer error",
        "call_counts": "not measured here; this driver is uninstrumented",
        "uncertainty": "paired repeat-level bootstrap 95% CI of median baseline_elapsed/candidate_elapsed ratios; raw rows retained",
        "bootstrap_resamples": 10000,
        "adoption_gate": plan_counts["adoption_gate"],
    }
    manifest_path = output / "manifest.json"
    records_path = output / "samples.jsonl"
    atomic_json(manifest_path, manifest)
    completed_rows: list[dict[str, object]] = []
    peer: subprocess.Popen[str] | None = None
    attempted: set[str] = set()
    positive_tls_identity: tuple[int, int] | None = None
    global CURRENT_URL, CURRENT_CA
    try:
        current_head = git_text("rev-parse", "HEAD")
        if git_text("cat-file", "-t", CLIENT_SOURCE_REV) != "commit":
            raise RuntimeError(f"frozen client source revision is unavailable: {CLIENT_SOURCE_REV}")
        if git_text("status", "--porcelain"):
            raise RuntimeError("main worktree must be clean before preparing/running the measurement")
        client_builds, clients = prepare_measurement_clients(backends)
        sources = source_inventory()
        bindings = {name: matched_binding(backends, name) for name in BACKEND_NAMES}
        compiler = compiler_info()
        peer_binary = output / "websocket_stream_server"
        peer_build_info = build_peer(peer_binary)
        binary_hashes_before = {
            **{f"{name}-bend": info["stream_binary_sha256"] for name, info in clients.items()},
            **{f"{name}-bridge": info["bridge_library_sha256"] for name, info in clients.items()},
            "go_peer": peer_build_info["sha256"],
        }
        manifest.update({
            "status": "running", "source_sha256_before": sources,
            "main_repo_head": current_head, "client_source_revision": CLIENT_SOURCE_REV,
            "backend_provenance": backends, "client_provenance": clients,
            "client_build": client_builds,
            "client_build_reuse": {
                "policy": "one Bend stream executable and bridge are compiled once against baseline headers and reused byte-for-byte under both runtime backend conditions",
                "same_stream_binary_sha256": len({info["stream_binary_sha256"] for info in clients.values()}) == 1,
                "same_bridge_library_sha256": len({info["bridge_library_sha256"] for info in clients.values()}) == 1,
                "runtime_selection": "LD_LIBRARY_PATH is the only backend selection difference between the four client/backend processes",
            },
            "matched_curl_cffi_binding": bindings, "toolchain": compiler,
            "peer_binary": peer_build_info, "binary_sha256_before": binary_hashes_before,
            "environment": {"LD_PRELOAD_present": False, "LD_LIBRARY_PATH_set_per_condition": True,
                            "threads": 1, "profile": "chrome146", "TLS": "WSS"},
            "execution_order_ids": [], "attempts_completed": 0, "attempts_failed": 0,
        })
        atomic_json(manifest_path, manifest)
        planned_items = [*negative_plan, *positive_plan]
        with tempfile.TemporaryDirectory(prefix="scrapanium-four-condition-") as tmp:
            _, ca = streaming.certificate(tmp)
            peer = subprocess.Popen([str(peer_binary), "-cert", ca, "-key", str(Path(tmp) / "key.pem")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            url = streaming.shared.line(peer)
            CURRENT_URL, CURRENT_CA = url, ca
            body = Path(tmp) / "body.bin"
            for index, item in enumerate(planned_items):
                run_id = str(item["run_id"])
                if run_id in attempted:
                    raise RuntimeError(f"duplicate execution ID: {run_id}")
                attempted.add(run_id)
                record: dict[str, object] = {
                    "plan": item, "attempt_index": index,
                    "started_utc": utc_now(), "status": "running",
                }
                partial_result: dict[str, object] = {}
                appended = False
                try:
                    result = execute_one(item, backends, clients, peer, body, partial_result)
                    if item["kind"] == "negative":
                        result["fault"] = item["fault"]
                        result["backend"] = item["backend"]
                        result["client"] = item["client"]
                    else:
                        peer_row = result["peer_observation"]
                        tls_identity = (int(peer_row["tls_version"]), int(peer_row["tls_cipher"]))
                        if positive_tls_identity is None:
                            positive_tls_identity = tls_identity
                        elif tls_identity != positive_tls_identity:
                            raise RuntimeError(
                                f"TLS identity differs across positive conditions: {tls_identity} vs {positive_tls_identity}"
                            )
                        result["repeat"] = item["repeat"]
                        result["workload"] = item["workload"]["name"]
                        result["condition"] = item["condition"]["id"]
                    record.update({"status": "passed", "result": result, "finished_utc": utc_now()})
                    with records_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, sort_keys=True) + "\n")
                        handle.flush(); os.fsync(handle.fileno())
                    appended = True
                    completed_rows.append(record)
                    manifest["execution_order_ids"].append(run_id)
                    manifest["attempts_completed"] = len(completed_rows)
                    atomic_json(manifest_path, manifest)
                except Exception as error:
                    if not appended:
                        record.update({"status": "failed", "error": repr(error),
                                       "traceback": traceback.format_exc(), "finished_utc": utc_now()})
                        if partial_result:
                            record["partial_result"] = partial_result
                        with records_path.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(record, sort_keys=True) + "\n")
                            handle.flush(); os.fsync(handle.fileno())
                    manifest["status"] = "failed"
                    manifest["failure"] = {"attempt_index": index, "run_id": run_id, "error": repr(error)}
                    manifest["attempts_failed"] = int(manifest.get("attempts_failed", 0)) + 1
                    atomic_json(manifest_path, manifest)
                    raise
            if peer.stdin and not peer.stdin.closed:
                peer.stdin.close()
            try:
                peer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                peer.kill()
                peer.wait()
                raise RuntimeError("stream peer failed to shut down within five seconds")
            manifest["peer_exit_code"] = peer.returncode
            if peer.returncode != 0:
                raise RuntimeError(f"stream peer exited with status {peer.returncode}")
            atomic_json(manifest_path, manifest)
            peer = None
        manifest["status"] = "validating-final-hashes"
        atomic_json(manifest_path, manifest)
        sources_after = source_inventory()
        if sources != sources_after:
            raise RuntimeError("source hashes changed during run")
        binaries_after = {
            **{f"{name}-bend": digest(Path(info["stream_binary"])) for name, info in clients.items()},
            **{f"{name}-bridge": digest(Path(info["bridge_library"])) for name, info in clients.items()},
            "go_peer": digest(peer_binary),
        }
        if binary_hashes_before != binaries_after:
            raise RuntimeError("client/peer binary hashes changed during run")
        for name in BACKEND_NAMES:
            lib = Path(backends[name]["library_path"])
            if digest(lib) != backends[name]["library_sha256"]:
                raise RuntimeError(f"backend changed during run: {name}")
        backends_after = backend_info(pair_root)
        for name in BACKEND_NAMES:
            for field in ("library_sha256", "manifest_sha256", "provenance_sha256",
                          "object_map_sha256", "elf_evidence_sha256", "link_command_sha256",
                          "candidate_command_json_sha256", "prefix_inventory_sha256"):
                if backends_after[name][field] != backends[name][field]:
                    raise RuntimeError(f"backend {field} changed during run: {name}")
        if len(completed_rows) != len(planned_items) or attempted != {str(x["run_id"]) for x in planned_items}:
            raise RuntimeError("executed attempt IDs/count differ from the frozen plan")
        bindings_after = {name: matched_binding(backends, name) for name in BACKEND_NAMES}
        toolchain_after = compiler_info()
        if bindings_after != bindings:
            raise RuntimeError("matched Python binding/interpreter changed during run")
        if not smoke:
            gate_after = validate_correctness_gate(correctness_gate_path, backends_after)
            if gate_after != correctness_gate:
                raise RuntimeError("correctness-gate artifact changed during the measurement")
        if toolchain_after != compiler:
            raise RuntimeError("compiler/toolchain/Bend inputs changed during run")
        positive_rows = [r for r in completed_rows if r["plan"]["kind"] == "positive"]
        if len(positive_rows) != len(positive_plan):
            raise RuntimeError("positive attempt count differs from the plan")
        manifest["source_sha256_after"] = sources_after
        manifest["binary_sha256_after"] = binaries_after
        manifest["matched_curl_cffi_binding_after"] = bindings_after
        manifest["toolchain_after"] = toolchain_after
        manifest["backend_provenance_after"] = {
            name: {field: backends_after[name][field]
                   for field in ("library_sha256", "manifest_sha256", "provenance_sha256")}
            for name in BACKEND_NAMES
        }
        manifest["hashes_verified_after_run"] = True
        manifest["summary"] = summarize(positive_rows, smoke)
        manifest["status"] = "complete"
        manifest["finished_utc"] = utc_now()
        atomic_json(manifest_path, manifest)
    except Exception as error:
        if manifest.get("status") != "failed":
            manifest["status"] = "failed"
            manifest["failure"] = {"error": repr(error), "traceback": traceback.format_exc()}
        manifest["finished_utc"] = utc_now()
        atomic_json(manifest_path, manifest)
        raise
    finally:
        if peer is not None:
            if peer.stdin and not peer.stdin.closed:
                peer.stdin.close()
            if peer.poll() is None:
                try:
                    peer.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    peer.kill(); peer.wait()


CURRENT_URL = ""
CURRENT_CA = ""


def main() -> None:
    global BUILD
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true", help="24 positive count-32 correctness checks plus 8 negatives; no performance inference")
    mode.add_argument("--run-full", action="store_true", help="execute the fixed 20-repeat/480-positive plan")
    parser.add_argument("--print-plan", action="store_true", help="print the deterministic schedule and exit without building or connecting")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--build-root", type=Path,
                        help="new, persistent Linux directory for this mode's isolated clients/output; must not already exist")
    parser.add_argument("--pair-root", type=Path, default=PAIR_ROOT_DEFAULT,
                        help="persistent directory containing the frozen natural baseline/candidate DSO pair")
    parser.add_argument("--correctness-gate", type=Path,
                        help="machine-readable guard/full-suite evidence required by --run-full")
    args = parser.parse_args()
    if args.print_plan:
        print_plan(args.smoke)
        return
    if not args.smoke and not args.run_full:
        parser.error("default is plan-only; pass --smoke or obtain review before --run-full")
    if args.build_root is None:
        parser.error("--build-root is required for smoke/full execution")
    BUILD = args.build_root.resolve()
    default = BUILD / ("samples-smoke" if args.smoke else "samples-full")
    pair_root = args.pair_root.resolve()
    gate = (args.correctness_gate or (pair_root / "correctness-gate.json")).resolve()
    run(args.smoke, (args.output_dir or default).resolve(), pair_root, gate)


if __name__ == "__main__":
    main()
