#!/usr/bin/env python3
"""Create the fixed counter-census source pin manifest from reviewed records."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
from pathlib import Path

ART = Path("/home/baidu/scrapanium-experiments/wss-owned-mechanism-20261008/artifacts/owned-mechanism-02").resolve()
ROOT = ART.parent.parent
BASE = ROOT / "baseline"
OWNED = ROOT / "owned"
PRE = ART / "source-preflight-03/source-preflight.json"
BUILD_DIR = ART / "client-build-02"
BUILD_SESSION = BUILD_DIR / "build-session.json"
BUILD_COMMANDS = ART / "build-commands-02.json"
CURL_BUILD = ART / "curl-diagnostic-build-01"
CURL_OVERLAY = ROOT / "artifacts/owned-mechanism-01/curl-counter-authoring-02"
CURL_SOURCE = CURL_BUILD / "work/build/deps/src/curl"
PREFIX = CURL_BUILD / "prefix"
BEND = Path("/home/baidu/scrapanium-experiments/bend2-027-compat/compiler-2.0.27")
PINNED_HEAD = "63bee70b55a71024d6bdcb49a745111bc54b114e"
BINS = {
    "baseline_stream": BUILD_DIR / "baseline-stream",
    "baseline_roundtrip": BUILD_DIR / "baseline-roundtrip",
    "owned_stream": BUILD_DIR / "owned-stream",
    "owned_roundtrip": BUILD_DIR / "owned-roundtrip",
    "stream_peer": BUILD_DIR / "stream-peer",
    "roundtrip_peer": BUILD_DIR / "roundtrip-peer",
}

def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def add(files: dict[str, str], path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"required pin is missing or symlinked: {path}")
    path = path.resolve()
    files[str(path)] = sha(path)

def add_tree(files: dict[str, str], root: Path) -> None:
    if not root.is_dir():
        raise RuntimeError(f"required tree missing: {root}")
    found = 0
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            raise RuntimeError(f"unexpected symlink in pinned output tree: {path}")
        if path.is_file():
            add(files, path)
            found += 1
    if not found:
        raise RuntimeError(f"required tree empty: {root}")

def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

def parse_names(source: Path, declaration: str, closing: str | None = None) -> list[str]:
    text = source.read_text(encoding="utf-8")
    start = text.index(declaration)
    end = text.index("};", start)
    return re.findall(r'"([^"]+)"', text[start:end])

def main() -> None:
    pre = json.loads(PRE.read_text(encoding="utf-8"))
    build = json.loads(BUILD_SESSION.read_text(encoding="utf-8"))
    if build["status"] != "builds-complete" or build["commands_planned"] != 10 or build["commands_completed"] != 10:
        raise RuntimeError("client build session is incomplete")
    if build["prebuild_pins"] != build["postbuild_pins"]:
        raise RuntimeError("pre/post build source pins differ")
    if build["client_invocations"] or build["peer_invocations"] or build["workload_attempts"] or build["campaign_started"]:
        raise RuntimeError("build record claims a client, peer, workload, or campaign")
    if any(c["exit_code"] != 0 or c["status"] != "complete" for c in build["commands"]):
        raise RuntimeError("a generation/link command failed")
    if pre["baseline"]["head"] != pre["owned"]["head"] or pre["baseline"]["head"] != "7b52d7f1cbbfe4c21b1abf1ad71e8e3061c47c17":
        raise RuntimeError("benchmark worktree commit mismatch")
    files: dict[str, str] = {}
    for fixed in (
        ART / "PLAN.md", ART / "run_mechanism_counters.py",
        ART / "mechanism_validation.py", ART / "test_mechanism_validation.py",
        ART / "make_source_pins.py", ART / "validation-unit-final-20261008-01.log",
        ART / "validation-unit-command-20261008-01.txt",
        ART / "validation-unit-final-20261008-02.log", ART / "validation-unit-command-20261008-02.txt",
        ART / "validation-unit-final-20261008-03.log", ART / "validation-unit-command-20261008-03.txt",
        ART / "validation-unit-final-20261008-04.log", ART / "validation-unit-command-20261008-04.txt",
        ART / "validation-unit-final-20261008-05.log", ART / "validation-unit-command-20261008-05.txt",
        ART / "preflight_sources.py",
        ART / "source-preflight-03-terminal.log", PRE,
        ART / "native-phase-verification-20261008-05.json",
        ART / "verify_native_phase_sources_20261008_05.py",
        ART / "phase-hook-probe-20261008-01/runtime-validation-04.json",
        ART / "phase-hook-probe-20261008-01/command-record-retrospective-01.txt",
        ART / "phase-hook-negative-probes-20261008-02/capture-01/negative-probe-results.json",
        ART / "build-commands-02.json", ART / "prepare_client_builds_20261008_02.py",
        ART / "prepare_client_builds_20261008_01.py",
        ART / "client-build-01/build-session.json", ART / "client-build-01/invocation.txt",
        BUILD_SESSION, BUILD_DIR / "invocation.txt",
        ART / "curl-diagnostic-build-01/build-validation-20261008-01.json",
        ART / "curl-diagnostic-build-01/build-command.txt",
        ART / "curl-diagnostic-build-01/validate_build_20261008_01.py",
        ART / "curl-gauge-test-01/gauge_client.c",
        ART / "curl-gauge-test-01/run_gauge_test.py",
        ART / "curl-gauge-test-01/run-02-summary.json",
        ART / "curl-gauge-test-01/run-02.log",
    ):
        add(files, fixed)
    for name in ("baseline", "owned"):
        row = pre[name]
        root = Path(row["path"])
        for rel in row["source_sha256"]:
            add(files, root / rel)
        for rel in row["changed_file_sha256"]:
            add(files, root / rel)
    add_tree(files, BUILD_DIR)
    for rel in pre["backend"]["curl_overlay_source_sha256"]:
        add(files, CURL_OVERLAY / rel)
    for rel in pre["backend"]["built_curl_source_sha256"]:
        add(files, CURL_SOURCE / rel)
    prefix_manifest = PREFIX / "manifest.json"
    add(files, prefix_manifest)
    add(files, PREFIX / "lib/libcurl-impersonate.so.4.8.0")
    for rel in pre["backend"]["headers"]:
        add(files, PREFIX / "include" / rel)
    tc = pre["toolchain"]
    for key in ("clang", "bun", "go", "python"):
        add(files, Path(tc[key]))
    clang_resource = Path(pre["clang_resource_dir"]) / "include"
    for rel in pre["clang_resource_headers"]:
        add(files, clang_resource / rel)
    go_tooldir = Path(pre["go_tooldir"])
    for name in ("asm", "compile", "link"):
        add(files, go_tooldir / name)
    crypto = pre["cryptography"]
    add(files, Path(crypto["module_path"]))
    bend_status = subprocess.check_output(
        ["git", "-C", str(BEND), "status", "--porcelain", "--untracked-files=all"],
        text=True).splitlines()
    bend_head = git(BEND, "rev-parse", "HEAD")
    if bend_head != PINNED_HEAD or bend_status:
        raise RuntimeError("Bend compiler checkout is not the pinned clean commit")
    native_text = (OWNED / "native/websocket.inc.c").read_text(encoding="utf-8")
    native_names = parse_names(OWNED / "native/websocket.inc.c",
                               "static const char *const names[SP_WSS_DIAG_COUNTER_COUNT] = {")
    native_names.extend(("layout_segment_size", "layout_buffer_size", "layout_ws_io_size"))
    curl_names = parse_names(CURL_SOURCE / "lib/ws.c",
                             "static const char *const ws_diag_names[CURL_WS_DIAG_SNAPSHOT_COUNT] = {")
    if len(native_names) != 61 or len(curl_names) != 60:
        raise RuntimeError(f"counter name table size mismatch: native={len(native_names)} Curl={len(curl_names)}")
    if len(set(native_names)) != 61 or len(set(curl_names)) != 60:
        raise RuntimeError("counter names are not unique")
    binary_info = {name: {"path": str(path.resolve()), "sha256": sha(path)} for name, path in BINS.items()}
    for name, path in BINS.items():
        if build["output_hashes"].get(str(path.resolve())) != binary_info[name]["sha256"]:
            raise RuntimeError(f"build-session binary hash differs: {name}")
    dso_link = PREFIX / "lib/libcurl-impersonate.so"
    dso_real = dso_link.resolve()
    dso_sha = sha(dso_real)
    if dso_sha != "e48e14edacc7d8ed0abab6f5f77e5d9cebca1a87c0153ce7e939b407252e9ab9":
        raise RuntimeError("diagnostic DSO hash differs from reviewed build")
    result = {
        "schema": 1,
        "files": dict(sorted(files.items())),
        "worktrees": {
            name: {
                "path": row["path"], "head": row["head"], "status_lines": row["status_lines"],
                "tracked_diff_sha256": row["tracked_diff_sha256"],
                "changed_file_sha256": row["changed_file_sha256"],
            } for name, row in (("baseline", pre["baseline"]), ("owned", pre["owned"]))
        },
        "compiler_checkout": {"path": str(BEND), "head": bend_head, "status_lines": bend_status},
        "source_preflight": {"path": str(PRE.resolve()), "sha256": sha(PRE)},
        "client_build": {
            "session_path": str(BUILD_SESSION.resolve()), "session_sha256": sha(BUILD_SESSION),
            "commands_path": str(BUILD_COMMANDS.resolve()), "commands_sha256": sha(BUILD_COMMANDS),
            "prebuild_pins": build["prebuild_pins"], "postbuild_pins": build["postbuild_pins"],
            "commands_completed": build["commands_completed"], "builds_started": True,
            "client_invocations": False, "peer_invocations": False, "workload_attempts": False,
        },
        "backend": {
            "prefix": str(PREFIX.resolve()), "dso_link": str(dso_link),
            "dso_realpath": str(dso_real), "dso_sha256": dso_sha,
            "prefix_manifest_path": str(prefix_manifest.resolve()),
            "prefix_manifest_sha256": sha(prefix_manifest),
            "build_validation_path": str((CURL_BUILD / "build-validation-20261008-01.json").resolve()),
            "build_validation_sha256": sha(CURL_BUILD / "build-validation-20261008-01.json"),
        },
        "binaries": binary_info,
        "cryptography": {"path": crypto["module_path"], "sha256": crypto["module_sha256"],
                         "version": crypto["version"]},
        "counter_schema": {"native": native_names, "curl": curl_names},
        "profile": {"name": "chrome146", "tls_version": 772, "tls_cipher": 4865},
        "fixed_attempts": 16,
    }
    out = ART / "source-pins.json"
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out)
    print(json.dumps({"source_pins": str(out), "sha256": sha(out),
                      "pinned_regular_files": len(files),
                      "native_counter_slots": len(native_names),
                      "curl_counter_slots": len(curl_names)}, sort_keys=True))

if __name__ == "__main__":
    main()
