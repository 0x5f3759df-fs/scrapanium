#!/usr/bin/env python3
"""Prepare isolated curl and Bend inputs for the frozen WSS experiment.

Copy this file and the adjacent experiment assets into the ignored
build/wss-recv-coalescing directory of a clean checkout at BASE_REV, then run
it once. It creates fresh backend work/snapshots and isolated client roots.
The lengthy cold backend build is intentionally explicit; this helper is not
a timing framework.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


BASE_REV = "7b412fbaae907f4419b94085d0ae5bf81db94341"
ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build/wss-recv-coalescing"
ASSETS = Path(__file__).resolve().parent
PATCH_DIR = ASSETS / "patches"
BACKEND_PATCH = PATCH_DIR / "curl-ws-recv-coalescing.patch"
BRIDGE_PATCH = PATCH_DIR / "native-bridge-coalescing.patch"
BACKEND_WS = Path("build/deps/src/curl/lib/ws.c")
BACKEND_WS_BASE_SHA = "18ec1d509d44732fc94b26dbf8c5f39f3ddab32d0342df4fb20cc42e33726e80"
BACKEND_WS_CANDIDATE_SHA = "9e67cfc98bd2e08de253fd9a09f57b06f1d6c5f5ab97780ac7e1da135946c058"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def output(*command: str) -> str:
    return subprocess.check_output(command, cwd=ROOT, text=True).strip()


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def assert_base_checkout() -> None:
    head = output("git", "rev-parse", "HEAD")
    if head != BASE_REV:
        raise RuntimeError(f"expected clean base {BASE_REV}, found HEAD {head}")
    status = output("git", "status", "--porcelain")
    if status:
        raise RuntimeError("base checkout must be clean; experiment assets belong under ignored build/")
    for path in (BACKEND_PATCH, BRIDGE_PATCH, ROOT / "scripts/build_backend.py"):
        if not path.is_file():
            raise FileNotFoundError(path)


def require_fresh(paths: list[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("refusing to reuse generated inputs: " + ", ".join(existing))


def snapshot_prefix(source: Path, destination: Path, variant: str) -> dict[str, object]:
    shutil.copytree(source, destination, symlinks=False)
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"snapshot retained a symlink: {path}")
    bundle: dict[str, str] = {}
    if variant.startswith("candidate"):
        patch_relative = "sources/backend/patches/curl-ws-recv-coalescing.patch"
        note_relative = "sources/COALESCING-REPRODUCTION.md"
        patch_target = destination / patch_relative
        note_target = destination / note_relative
        patch_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BACKEND_PATCH, patch_target)
        note_target.write_text(
            "# WSS receive-coalescing candidate rebuild\n\n"
            "This candidate is the baseline browser-controls build plus the adjacent "
            "`backend/patches/curl-ws-recv-coalescing.patch`. The initial queue slurp "
            "is unchanged. Only an incomplete current data frame can trigger up to "
            "four additional single-reader sipn attempts when the pending-data hint "
            "is set; the hint never causes waiting or spinning. Control frames and "
            "frame boundaries are excluded. This is a rejected experiment, not a "
            "product backend.\n",
            encoding="utf-8", newline="\n")
        bundle = {"source_bundle_patch_path": patch_relative,
                  "source_bundle_patch_sha256": sha256(patch_target),
                  "reproduction_note_path": note_relative,
                  "reproduction_note_sha256": sha256(note_target)}
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = {
        path.relative_to(destination).as_posix(): sha256(path)
        for path in sorted(destination.rglob("*"))
        if path.is_file() and path != manifest_path
    }
    manifest["files"] = files
    manifest["experiment_variant"] = variant
    if bundle:
        manifest["coalescing_experiment"] = bundle
    write_json(manifest_path, manifest)
    for relative, expected in files.items():
        if sha256(destination / relative) != expected:
            raise RuntimeError(f"snapshot hash mismatch: {destination / relative}")
    return {"path": str(destination.resolve()), "manifest": str(manifest_path.resolve()),
            "manifest_sha256": sha256(manifest_path), "file_count": len(files), "files": files}


def archive_client_root(destination: Path) -> None:
    destination.mkdir(parents=True)
    archive_path = BUILD / (destination.name + ".tar")
    run(["git", "archive", "--format=tar", f"--output={archive_path}", BASE_REV])
    try:
        with tarfile.open(archive_path, "r:") as archive:
            archive.extractall(destination, filter="data")
    finally:
        archive_path.unlink(missing_ok=True)


def build_client(client_root: Path, backend_prefix: Path, bridge_output: Path,
                 bun: Path, bend_source: Path, clang: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "ASAN_OPTIONS", "UBSAN_OPTIONS"):
        env.pop(name, None)
    env.update({"SCRAPANIUM_CURL_DIR": str(backend_prefix.resolve()),
                "BEND_SOURCE": str(bend_source.resolve()), "BUN": str(bun.resolve()),
                "CC": str(clang.resolve())})
    bridge = client_root / "build/libscrapanium.so"
    binary = client_root / "build/websocket_stream"
    run([sys.executable, "scripts/build.py", "-o", str(bridge)], cwd=client_root, env=env)
    run([sys.executable, "scripts/build.py", "benchmarks/websocket_stream.bend",
         "-o", str(binary)], cwd=client_root, env=env)
    bridge_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bridge, bridge_output)
    return {"root": str(client_root.resolve()), "backend_prefix": str(backend_prefix.resolve()),
            "bridge": str(bridge_output.resolve()), "bridge_sha256": sha256(bridge_output),
            "stream_binary": str(binary.resolve()), "stream_binary_sha256": sha256(binary),
            "native_source_sha256": sha256(client_root / "native/websocket.inc.c")}


def main() -> None:
    if sys.platform != "linux":
        raise RuntimeError("run this helper under Linux/WSL")
    assert_base_checkout()
    work = BUILD / "backend-work"
    live = BUILD / "backend-live"
    snapshots = BUILD / "backends"
    roots = BUILD / "client-roots"
    bridges = BUILD / "bridges"
    require_fresh([work, live, snapshots, roots, bridges])
    bun = ROOT / ".deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun"
    bend_source = ROOT / ".deps/bend"
    clang = Path("/usr/bin/clang")
    if not bun.is_file() or not (bend_source / "bend2/main.ts").is_file() or not clang.is_file():
        raise FileNotFoundError("install the pinned Bun/Bend dependencies and Clang before setup")

    backend_env = os.environ.copy()
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
                 "LIBRARY_PATH", "PKG_CONFIG_PATH", "CFLAGS", "CXXFLAGS", "LDFLAGS"):
        backend_env.pop(name, None)
    backend_env.update({"CC": "/usr/bin/clang", "CXX": "/usr/bin/clang++"})
    run([sys.executable, "scripts/build_backend.py", "--work", str(work),
         "--prefix", str(live), "--jobs", "4"], env=backend_env)

    curl_root = work / "build/deps/src/curl"
    ws_source = curl_root / "lib/ws.c"
    if sha256(ws_source) != BACKEND_WS_BASE_SHA:
        raise RuntimeError("fresh backend source does not match the recorded baseline ws.c")
    (snapshots / "baseline").parent.mkdir(parents=True, exist_ok=True)
    baseline = snapshot_prefix(live, snapshots / "baseline", "baseline-browser-controls")
    baseline_library = snapshots / "baseline/lib/libcurl-impersonate.so"
    dependency_libs_before = {
        str(path.relative_to(work)): sha256(path)
        for path in sorted((work / "build/deps/install/lib").glob("*.a"))
    }
    run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(BACKEND_PATCH)], cwd=curl_root)
    if sha256(ws_source) != BACKEND_WS_CANDIDATE_SHA:
        raise RuntimeError("candidate backend ws.c hash differs from the reviewed patch")
    run(["cmake", "--build", str(work / "build/deps/build/curl"),
         "--config", "Release", "--parallel", "4", "--verbose"], env=backend_env)
    run(["cmake", "--install", str(work / "build")], env=backend_env)
    candidate = snapshot_prefix(live, snapshots / "candidate", "candidate-bounded-ws-recv-coalescing")
    candidate_library = snapshots / "candidate/lib/libcurl-impersonate.so"
    dependency_libs_after = {
        str(path.relative_to(work)): sha256(path)
        for path in sorted((work / "build/deps/install/lib").glob("*.a"))
    }
    if dependency_libs_after != dependency_libs_before:
        raise RuntimeError("candidate curl rebuild changed dependency libraries")
    if sha256(baseline_library) == sha256(candidate_library):
        raise RuntimeError("candidate backend library is identical to baseline")
    backend_manifest = json.loads((snapshots / "baseline/manifest.json").read_text(encoding="utf-8"))
    shared_source = {
        "curl_upstream_commit": "6e8f87760a4dd96771e96fc9d55440dcd8845243",
        "curl_archive_sha256": "222c6b5c1f368ac63aed59bce2774eb5def9e8e67e46e800be182e684d2845a3",
        "browser_controls_patch_sha256": "00232264394234559d0857174d37ff33b9922d619729da19e0fe0370d1e5787e",
        "baseline_ws_sha256": BACKEND_WS_BASE_SHA,
        "candidate_ws_sha256": sha256(ws_source),
        "candidate_patch_sha256": sha256(BACKEND_PATCH),
        "baseline_library_sha256": sha256(baseline_library),
        "candidate_library_sha256": sha256(candidate_library),
        "dependency_static_library_hashes_before_candidate": dependency_libs_before,
        "dependency_static_library_hashes_after_candidate": dependency_libs_after,
        "dependency_static_libraries_unchanged": True,
        "backend_build_manifest": {
            "compiler": backend_manifest.get("compiler"),
            "cmake": backend_manifest.get("cmake"),
            "platform": backend_manifest.get("platform"),
            "configure": backend_manifest.get("configure"),
            "input_sha256": backend_manifest.get("input_sha256"),
        },
    }
    common_provenance = {"schema": 1, "main_repo_head": BASE_REV, "source": shared_source,
                         "baseline_snapshot_manifest_sha256": baseline["manifest_sha256"],
                         "baseline_snapshot_file_count": baseline["file_count"],
                         "candidate_snapshot_manifest_sha256": candidate["manifest_sha256"],
                         "candidate_snapshot_file_count": candidate["file_count"]}
    write_json(BUILD / "backend-baseline-provenance.json",
               {**common_provenance, "variant": "baseline", "backend_library_sha256": sha256(baseline_library)})
    write_json(BUILD / "backend-candidate-provenance.json",
               {**common_provenance, "variant": "candidate", "candidate_library_sha256": sha256(candidate_library),
                "source_bundle": candidate["files"].get("sources/backend/patches/curl-ws-recv-coalescing.patch")})

    roots.mkdir()
    archive_client_root(roots / "baseline")
    archive_client_root(roots / "candidate")
    candidate_root = roots / "candidate"
    run(["patch", "--batch", "--fuzz=0", "--dry-run", "-p1", "-i", str(BRIDGE_PATCH)], cwd=candidate_root)
    run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(BRIDGE_PATCH)], cwd=candidate_root)
    if sha256(candidate_root / "native/websocket.inc.c") != "f051b39d89808f9db5a918471574db97a2e60dd4d601b256cc2714779e2de544":
        raise RuntimeError("candidate bridge does not match the reviewed source hash")
    client_info = {}
    for label in ("baseline", "candidate"):
        client_info[label] = build_client(
            roots / label, snapshots / label, bridges / label / "libscrapanium.so",
            bun, bend_source, clang)
    write_json(BUILD / "prepared-pair.json", {
        "schema": 1, "main_repo_head": BASE_REV, "backend": shared_source,
        "snapshots": {"baseline": baseline, "candidate": candidate},
        "clients": client_info,
        "reconstruction_note": "Fresh setup helper; a cold clean-machine end-to-end run is not claimed by this publication.",
    })
    assert_base_checkout()
    print("Prepared isolated pair at", BUILD)
    print("Run the correctness smoke before any fixed full measurement.")


if __name__ == "__main__":
    main()
