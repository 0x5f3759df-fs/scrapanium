"""Ignored-build runner for paired baseline/candidate WSS streaming checks.

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
BUILD = ROOT / "build/wss-recv-coalescing"
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
    for backend in BACKEND_NAMES:
        isolated = BUILD / "client-roots" / backend
        for rel in sorted(paths):
            relative = rel.relative_to(ROOT)
            path = isolated / relative
            if not path.is_file():
                raise RuntimeError(f"isolated client source is missing: {path}")
            inventory[f"client-roots/{backend}/{relative.as_posix()}"] = digest(path)
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


def backend_info() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name in BACKEND_NAMES:
        prefix = BUILD / "backends" / name
        manifest_path = prefix / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        file_hashes = manifest.get("files", {})
        if not file_hashes:
            raise RuntimeError(f"backend manifest has no file hashes: {manifest_path}")
        mismatches = {rel: (expected, digest(prefix / rel))
                      for rel, expected in file_hashes.items()
                      if not (prefix / rel).is_file() or digest(prefix / rel) != expected}
        if mismatches:
            raise RuntimeError(f"backend snapshot files differ from manifest ({name}): {mismatches}")
        lib = (prefix / "lib/libcurl-impersonate.so").resolve()
        provenance_path = BUILD / f"backend-{name}-provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        expected = (provenance.get("candidate_library_sha256") if name == "candidate"
                    else provenance.get("backend_library_sha256"))
        if not expected or digest(lib) != expected:
            raise RuntimeError(f"backend library hash differs from provenance ({name})")
        result[name] = {
            "prefix": str(prefix.resolve()),
            "library_path": str(lib),
            "library_sha256": digest(lib),
            "manifest_path": str(manifest_path.resolve()),
            "manifest_sha256": digest(manifest_path),
            "manifest_file_count": len(file_hashes),
            "manifest_matches_files": True,
            "provenance_path": str(provenance_path.resolve()),
            "provenance_sha256": digest(provenance_path),
            "provenance": provenance,
        }
    return result


def client_info(backend: str, library_dir: Path) -> dict[str, object]:
    isolated = BUILD / "client-roots" / backend
    binary = isolated / "build/websocket_stream"
    bridge = BUILD / "bridges" / backend / "libscrapanium.so"
    if not binary.is_file() or not bridge.is_file():
        raise RuntimeError(f"missing isolated client build for {backend}")
    return {
        "root": str(isolated.resolve()),
        "stream_binary": str(binary.resolve()),
        "stream_binary_sha256": digest(binary),
        "stream_build_command": (
            f"cd {isolated}; env SCRAPANIUM_CURL_DIR={BUILD / 'backends' / backend} "
            f"BEND_SOURCE={ROOT / '.deps/bend'} BUN={ROOT / '.deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun'} "
            f"CC=/usr/bin/clang python3 scripts/build.py benchmarks/websocket_stream.bend -o build/websocket_stream"
        ),
        "bridge_library": str(bridge.resolve()),
        "bridge_library_sha256": digest(bridge),
        "bridge_rpath_backend": str(library_dir.resolve()),
        "native_websocket_sha256": digest(isolated / "native/websocket.inc.c"),
    }


def python_binding(backend: str, library_dir: Path) -> dict[str, object]:
    python = ROOT / ".deps/venv-matched/bin/python"
    env = {**os.environ, "LD_LIBRARY_PATH": str(library_dir.resolve())}
    env.pop("LD_PRELOAD", None)
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
            "receive_call_counts": "candidate must reduce native curl_ws_recv calls in the separate fixed 8-run probe; not measured by this uninstrumented runner",
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
    env.pop("SCRAPANIUM_WSATTR_MODE", None)
    env.pop("SCRAPANIUM_WSATTR_PATH", None)
    env["LD_LIBRARY_PATH"] = str(Path(backends[backend]["library_path"]).parent)
    env["SCRAPANIUM_CURL_DIR"] = str(backends[backend]["prefix"])
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


def run(smoke: bool, output: Path) -> None:
    if output.exists():
        raise RuntimeError(f"refusing to overwrite existing output: {output}")
    if "LD_PRELOAD" in os.environ:
        raise RuntimeError("LD_PRELOAD must be absent for the uninstrumented streaming run")
    output.mkdir(parents=True)
    negative_plan, positive_plan, plan_counts = make_plan(smoke)
    (output / "samples.jsonl").touch(exist_ok=False)
    manifest: dict[str, object] = {
        "schema": 1, "created_utc": utc_now(),
        "status": "preparing", "mode": "correctness-smoke-only" if smoke else "fixed-full-measurement",
        "smoke_only": smoke, "timings_are_performance_evidence": not smoke,
        "seed": SEED, "plan_counts": plan_counts,
        "negative_plan": negative_plan, "positive_plan": positive_plan,
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
        expected_head = json.loads((BUILD / "backend-candidate-provenance.json").read_text())["main_repo_head"]
        if current_head != expected_head:
            raise RuntimeError(f"main source HEAD drifted: expected {expected_head}, found {current_head}")
        if git_text("status", "--porcelain"):
            raise RuntimeError("tracked main worktree is dirty")
        sources = source_inventory()
        backends = backend_info()
        clients = {name: client_info(name, Path(backends[name]["library_path"]).parent)
                   for name in BACKEND_NAMES}
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
            "backend_provenance": backends, "client_provenance": clients,
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
        backends_after = backend_info()
        for name in BACKEND_NAMES:
            for field in ("library_sha256", "manifest_sha256", "provenance_sha256"):
                if backends_after[name][field] != backends[name][field]:
                    raise RuntimeError(f"backend {field} changed during run: {name}")
        if len(completed_rows) != len(planned_items) or attempted != {str(x["run_id"]) for x in planned_items}:
            raise RuntimeError("executed attempt IDs/count differ from the frozen plan")
        bindings_after = {name: matched_binding(backends, name) for name in BACKEND_NAMES}
        toolchain_after = compiler_info()
        if bindings_after != bindings:
            raise RuntimeError("matched Python binding/interpreter changed during run")
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
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true", help="24 positive count-32 correctness checks plus 8 negatives; no performance inference")
    mode.add_argument("--run-full", action="store_true", help="execute the fixed 20-repeat/480-positive plan")
    parser.add_argument("--print-plan", action="store_true", help="print the deterministic schedule and exit without building or connecting")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.print_plan:
        print_plan(args.smoke)
        return
    if not args.smoke and not args.run_full:
        parser.error("default is plan-only; pass --smoke or obtain review before --run-full")
    default = BUILD / ("stream-four-condition-smoke-20260924" if args.smoke
                       else "stream-four-condition-20260924")
    run(args.smoke, (args.output_dir or default).resolve())


if __name__ == "__main__":
    main()
