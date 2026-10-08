#!/usr/bin/env python3
"""Read-only source/toolchain preflight for the WSS mechanism counter preparation."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ARTIFACTS = Path("/home/baidu/scrapanium-experiments/wss-owned-mechanism-20261008/artifacts/owned-mechanism-02")
BASELINE = Path("/home/baidu/scrapanium-experiments/wss-owned-mechanism-20261008/baseline")
OWNED = Path("/home/baidu/scrapanium-experiments/wss-owned-mechanism-20261008/owned")
BEND = Path("/home/baidu/scrapanium-experiments/bend2-027-compat/compiler-2.0.27")
BUN = Path("/mnt/c/Users/Baidu/Projetos/scrapanium/.deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun")
CLANG = Path("/usr/lib/llvm-21/bin/clang")
GO = Path("/usr/lib/go-1.26/bin/go")
PYTHON = Path("/usr/bin/python3.14")
PREFIX = ARTIFACTS / "curl-diagnostic-build-01/prefix"
DSO_LINK = PREFIX / "lib/libcurl-impersonate.so"
DSO_REAL = PREFIX / "lib/libcurl-impersonate.so.4.8.0"
CURL_OVERLAY = Path("/home/baidu/scrapanium-experiments/wss-owned-mechanism-20261008/artifacts/owned-mechanism-01/curl-counter-authoring-02")
CURL_SOURCE = ARTIFACTS / "curl-diagnostic-build-01/work/build/deps/src/curl"
BUILD_VALIDATION = ARTIFACTS / "curl-diagnostic-build-01/build-validation-20261008-01.json"
PHASE_VERIFY = ARTIFACTS / "native-phase-verification-20261008-05.json"
PHASE_PROBE = ARTIFACTS / "phase-hook-probe-20261008-01/runtime-validation-04.json"
NEGATIVE_PROBES = ARTIFACTS / "phase-hook-negative-probes-20261008-02/capture-01/negative-probe-results.json"
EXPECTED_HEAD = "7b52d7f1cbbfe4c21b1abf1ad71e8e3061c47c17"
EXPECTED_DSO_SHA = "e48e14edacc7d8ed0abab6f5f77e5d9cebca1a87c0153ce7e939b407252e9ab9"
EXPECTED_BUILD_VALIDATION_SHA = "996b5b2361eacc0daaa14750529ccbaf00c3f460320766f4b898a5cecb84811a"
EXPECTED_CURL_SOURCE_SHA256 = {
    "lib/bufq.c": "596a94189776caf7c2f1f701c5da097245d456c051f32ba89ba6233f69802c96",
    "lib/bufq.h": "1c7c9d6380876c6e782188b15ea109d1c25d74ee65ab9e95150fcac83ec2bd81",
    "lib/ws.c": "3c5dcc7fa6f775730c04b0cf0009a368fcb79dc0d7f59f45ba30fe433aa7f4d9",
    "lib/ws_diag.h": "0d10f2ec2de952694625f54442eccd64c8de72810f433914a016d54b13a9e82e",
    "lib/vtls/openssl.c": "78388f7a65ed847386b19c7e616eaa03af6b0ba3102681ba65a8b7738f514e66",
    "include/curl/websockets.h": "a639a52c5c81b3873beef2469e16aa71aaf9f13b271756c2a81147ae50adbbad",
}
SHARED = (
    "scrapanium.bend", "http.bend", "dependencies.json", "scripts/build.py",
    "benchmarks/websocket.py", "benchmarks/websocket.bend", "benchmarks/websocket_server.go",
    "benchmarks/websocket_streaming.py", "benchmarks/websocket_stream.bend",
    "benchmarks/websocket_stream_server.go", "benchmarks/websocket_clock.c",
    "tests/lab.py", "tests/ws_lab.py",
    "benchmarks/websocket_mechanism_diag.c",
    "benchmarks/websocket_mechanism_stream.bend",
    "benchmarks/websocket_mechanism_roundtrip.bend",
)
EQUAL_SHARED = tuple(x for x in SHARED if x not in {"scripts/build.py", "tests/lab.py", "tests/ws_lab.py"})
HELPERS = {
    "websocket": ("benchmarks", "websocket.py"),
    "websocket_streaming": ("benchmarks", "websocket_streaming.py"),
    "build": ("scripts", "build.py"),
    "lab": ("tests", "lab.py"),
    "ws_lab": ("tests", "ws_lab.py"),
}

def sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise RuntimeError(f"required provenance tree is missing or not a directory: {root}")
    root = root.resolve()
    result = {}
    for p in sorted(root.rglob("*")):
        if ".git" in p.parts:
            continue
        if p.is_symlink():
            result[p.relative_to(root).as_posix()] = "symlink:" + str(p.resolve())
        elif p.is_file():
            result[p.relative_to(root).as_posix()] = sha(p)
    if not result:
        raise RuntimeError(f"required provenance tree is empty: {root}")
    return result

def run(args, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()

def git(root: Path, *args) -> str:
    return run(["git", "-C", str(root), *args])

def worktree(root: Path) -> dict:
    if git(root, "rev-parse", "HEAD") != EXPECTED_HEAD:
        raise RuntimeError(f"unexpected checkout HEAD: {root}")
    status = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        text=True).splitlines()
    diff = subprocess.check_output(["git", "-C", str(root), "diff", "--binary", "HEAD"])
    changed = {}
    for row in status:
        rel = row[3:]
        p = root / rel
        if not p.is_file() or p.is_symlink():
            raise RuntimeError(f"changed source is missing or is a symlink: {root}/{rel}")
        changed[rel] = sha(p)
    files = {name: sha(root / name) for name in SHARED}
    files.update({p.relative_to(root).as_posix(): sha(p)
                  for p in sorted((root / "native").iterdir()) if p.is_file()})
    missing = [name for name in SHARED if not (root / name).is_file()]
    if missing:
        raise RuntimeError(f"required benchmark/checker source missing: {missing}")
    return {
        "path": str(root.resolve()), "head": EXPECTED_HEAD,
        "branch": git(root, "branch", "--show-current"),
        "status_lines": status,
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "changed_file_sha256": changed,
        "source_sha256": files,
    }

def helper_provenance(cache: Path) -> dict:
    if any(name in sys.modules for name in HELPERS):
        raise RuntimeError("frozen helper was imported before isolated bytecode setup")
    if cache.exists() and any(cache.iterdir()):
        raise RuntimeError("run-local Python cache must be empty before helper import")
    cache.mkdir(parents=True, exist_ok=True)
    sys.pycache_prefix = str(cache.resolve())
    if sys.pycache_prefix != str(cache.resolve()):
        raise RuntimeError("interpreter rejected the run-local bytecode prefix")
    sys.path[:0] = [str(BASELINE / "benchmarks"), str(BASELINE / "tests"), str(BASELINE / "scripts")]
    import websocket
    import websocket_streaming
    from importlib import import_module
    for name in ("build", "lab", "ws_lab"):
        import_module(name)
    modules = {}
    for name, (folder, rel) in HELPERS.items():
        mod = sys.modules.get(name)
        if mod is None:
            raise RuntimeError(f"required helper did not import: {name}")
        source = Path(mod.__file__).resolve()
        expected = (BASELINE / folder / rel).resolve()
        cached_value = getattr(mod, "__cached__", None)
        if source != expected or not cached_value:
            raise RuntimeError(f"helper source/cache origin differs: {name}")
        cached = Path(cached_value).resolve()
        if not cached.is_file() or not cached.is_relative_to(cache.resolve()):
            raise RuntimeError(f"helper bytecode did not use the empty local cache: {name}")
        modules[name] = {
            "source_path": str(source), "source_sha256": sha(source),
            "cache_path": str(cached), "cache_sha256": sha(cached),
        }
    expected_streams = [
        {"name": f"stream-{s}b-flush{b}", "bytes": s, "count": n,
         "peer_flush_frames": b, "connections": 1}
        for b in (1, 64) for s, n in ((30, 65536), (1024, 16384), (65536, 1024))
    ]
    if websocket_streaming.WORKLOADS != expected_streams:
        raise RuntimeError("frozen stream workload list changed")
    expected_rt = [
        "original-python-30b-c1", "go-30b-c1", "go-1024b-c1", "go-65536b-c1",
        "go-30b-c4", "go-1024b-c4", "go-65536b-c4",
    ]
    if [x["name"] for x in websocket.WORKLOADS] != expected_rt:
        raise RuntimeError("frozen round-trip workload list changed")
    return {"cache_prefix": str(cache.resolve()), "cache_empty_before_import": True,
            "helpers": modules, "stream_workloads": websocket_streaming.WORKLOADS,
            "roundtrip_workload_names": expected_rt}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    out = a.output.resolve()
    if out.exists():
        ap.error("output must be fresh; refusing to overwrite")
    out.mkdir(parents=True)
    if Path(sys.executable).resolve() != PYTHON:
        raise RuntimeError(f"run with pinned interpreter {PYTHON}")
    if os.environ.get("LD_PRELOAD") or os.environ.get("LD_AUDIT"):
        raise RuntimeError("LD_PRELOAD and LD_AUDIT must be unset")
    roots = {"baseline": worktree(BASELINE), "owned": worktree(OWNED)}
    for rel in EQUAL_SHARED:
        if roots["baseline"]["source_sha256"][rel] != roots["owned"]["source_sha256"][rel]:
            raise RuntimeError(f"shared benchmark/checker input differs: {rel}")
    bend_status = git(BEND, "status", "--porcelain").splitlines()
    bend_head = git(BEND, "rev-parse", "HEAD")
    if bend_head != "63bee70b55a71024d6bdcb49a745111bc54b114e" or bend_status:
        raise RuntimeError("pinned Bend compiler checkout changed")
    if sha(CLANG) != "412bbe8c60571a1eb06f48fde89635033621caeb01a9b4ee76d46711bae8e932":
        raise RuntimeError("Clang binary differs from pinned compiler")
    if sha(BUN) != "45598a2814020c231575487a560e47d397d6902355d7e08171a2e56221a6d675":
        raise RuntimeError("Bun binary differs from pinned runtime")
    if sha(GO) != "170f0aeacbfecf200f42cfa837034239860b50dc8f62f664258b887a1f0776d1":
        raise RuntimeError("Go binary differs from pinned compiler")
    if sha(PYTHON) != "52e0a13e60a981d8c4b6478be2ba5176f69da07948a056bf49cf6f077e30cb41":
        raise RuntimeError("Python binary differs from pinned runtime")
    if not DSO_LINK.is_symlink() or DSO_LINK.resolve() != DSO_REAL or sha(DSO_REAL) != EXPECTED_DSO_SHA:
        raise RuntimeError("private diagnostic DSO path/hash differs")
    manifest = PREFIX / "manifest.json"
    if not BUILD_VALIDATION.is_file() or sha(BUILD_VALIDATION) != EXPECTED_BUILD_VALIDATION_SHA:
        raise RuntimeError("frozen diagnostic build-validation record hash differs")
    build_validation = json.loads(BUILD_VALIDATION.read_text())
    if build_validation.get("status") != "pass" or build_validation.get("build_exit_code") != 0:
        raise RuntimeError("frozen diagnostic Curl build did not pass")
    if build_validation.get("campaign_started") is not False:
        raise RuntimeError("diagnostic Curl build record claims a campaign was started")
    if sha(manifest) != build_validation["prefix_manifest_sha256"]:
        raise RuntimeError("private diagnostic DSO prefix manifest mismatch")
    if build_validation.get("diagnostic_overlay_sha256") != "2dec87802c8e0fe708fbe35f76952f01ede74d5e80d16418cf8b48091376aa53":
        raise RuntimeError("diagnostic overlay digest differs from the reviewed build record")
    recorded_source_hashes = build_validation.get("patched_curl_source_sha256", {})
    if recorded_source_hashes != EXPECTED_CURL_SOURCE_SHA256:
        raise RuntimeError("build-validation Curl source table differs from the independently reviewed source pins")
    actual_source_hashes = {}
    for rel, want in EXPECTED_CURL_SOURCE_SHA256.items():
        source_file = CURL_SOURCE / rel
        if not source_file.is_file() or source_file.is_symlink():
            raise RuntimeError(f"required built Curl source missing or symlinked: {source_file}")
        actual_source_hashes[rel] = sha(source_file)
        if actual_source_hashes[rel] != want:
            raise RuntimeError(f"built Curl source hash differs: {rel}")
    overlay_files = tree(CURL_OVERLAY)
    if overlay_files != EXPECTED_CURL_SOURCE_SHA256:
        raise RuntimeError("authoring overlay files differ from the six reviewed Curl source pins")
    if not CURL_SOURCE.is_dir():
        raise RuntimeError("built Curl source root is missing")
    toolchain = {
        "clang": str(CLANG), "clang_sha256": sha(CLANG),
        "clang_version": run([str(CLANG), "--version"]),
        "bun": str(BUN), "bun_sha256": sha(BUN), "bun_version": run([str(BUN), "--version"]),
        "go": str(GO), "go_sha256": sha(GO), "go_version": run([str(GO), "version"]),
        "python": str(PYTHON), "python_sha256": sha(PYTHON), "python_version": run([str(PYTHON), "--version"]),
        "bend": str(BEND), "bend_head": bend_head, "bend_status": bend_status,
    }
    resource_dir = Path(run([str(CLANG), "-print-resource-dir"]))
    go_tooldir = Path(run([str(GO), "env", "GOTOOLDIR"]))
    backend_validation = {
        "sha256": sha(BUILD_VALIDATION), "status": build_validation["status"],
        "prefix_manifest_sha256": sha(manifest),
        "dso_link": str(DSO_LINK), "dso_realpath": str(DSO_REAL.resolve()),
        "dso_sha256": sha(DSO_REAL),
        "headers": tree(PREFIX / "include"),
        "curl_overlay_path": str(CURL_OVERLAY.resolve()),
        "curl_overlay_source_sha256": overlay_files,
        "built_curl_source_path": str(CURL_SOURCE.resolve()),
        "built_curl_source_sha256": actual_source_hashes,
        "build_validation_sha256": sha(BUILD_VALIDATION),
        "overlay_sha256": build_validation["diagnostic_overlay_sha256"],
    }
    phase = {
        "native_phase_verification_sha256": sha(PHASE_VERIFY),
        "phase_probe_validation_sha256": sha(PHASE_PROBE),
        "negative_probe_result_sha256": sha(NEGATIVE_PROBES),
    }
    helpers = helper_provenance(out / "python-bytecode-cache")
    try:
        import cryptography
        cryptography_info = {
            "version": importlib.metadata.version("cryptography"),
            "module_path": str(Path(cryptography.__file__).resolve()),
            "module_sha256": sha(Path(cryptography.__file__).resolve()),
        }
    except Exception as exc:
        raise RuntimeError(f"cryptography runtime needed by frozen local CA helper is unavailable: {exc}") from exc
    payload = {
        "schema": 1, "scope": "read-only preparation preflight; no builds or benchmark attempts",
        "preflight_script": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha(Path(__file__).resolve()),
            "argv": sys.argv,
        },
        "supersedes": {
            "preflight_02_report_sha256": "e767020b6f9bfc3ae78f0441b274770433b4b3cca4bb52e0235d22179a93a780",
            "preflight_02_script_snapshot_sha256": "8115ff33e7090fa003fcc160bdb5553cb07ce5d6ddac48a7e2357a20986ced92",
            "reason": "correct Curl overlay path; reject missing/empty trees; independently rehash six built Curl source files",
        },
        "baseline": roots["baseline"], "owned": roots["owned"],
        "toolchain": toolchain,
        "clang_resource_dir": str(resource_dir),
        "clang_resource_headers": tree(resource_dir / "include"),
        "go_tooldir": str(go_tooldir),
        "go_tools": {name: sha(go_tooldir / name) for name in ("asm", "compile", "link")},
        "backend": backend_validation,
        "phase_hook_evidence": phase,
        "python_helper_provenance": helpers,
        "cryptography": cryptography_info,
        "profile": "chrome146", "tls_version": 772, "tls_cipher": 4865,
        "builds_started": False, "attempts_started": False,
        "performance_claims": False, "acceptance_claims": False,
    }
    target = out / "source-preflight.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "source_preflight_passed", "report": str(target),
                      "report_sha256": sha(target), "builds_started": False,
                      "attempts_started": False}, sort_keys=True))

if __name__ == "__main__":
    main()
