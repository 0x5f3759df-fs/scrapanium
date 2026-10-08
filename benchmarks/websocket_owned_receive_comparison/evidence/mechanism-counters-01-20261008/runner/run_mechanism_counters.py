#!/usr/bin/env python3
"""Fixed counters-only WSS mechanism census; execute only after separate release."""
from __future__ import annotations
import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time

ARTIFACTS = Path("/home/baidu/scrapanium-experiments/wss-owned-mechanism-20261008/artifacts/owned-mechanism-02").resolve()
PYTHON = Path("/usr/bin/python3.14").resolve()
DSO_SHA = "e48e14edacc7d8ed0abab6f5f77e5d9cebca1a87c0153ce7e939b407252e9ab9"
DSO_REAL = ARTIFACTS / "curl-diagnostic-build-01/prefix/lib/libcurl-impersonate.so.4.8.0"
CLEAR_ENV = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONPYCACHEPREFIX",
    "LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "LD_DEBUG",
    "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH",
    "PKG_CONFIG_PATH", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
    "BEND_SOURCE", "BUN", "CC", "GOFLAGS", "GOROOT", "GOTOOLCHAIN",
    "GOWORK", "GOCACHE", "GOOS", "GOARCH", "GOAMD64",
    "SCRAPANIUM_CURL_DIR", "SCRAPANIUM_MECHANISM_COUNTERS",
    "SCRAPANIUM_BENCH_URL", "SCRAPANIUM_BENCH_CA", "SCRAPANIUM_BENCH_COUNT",
    "SCRAPANIUM_BENCH_BODY", "SCRAPANIUM_BENCH_PAYLOAD",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "CGO_CFLAGS", "CGO_CPPFLAGS", "CGO_CXXFLAGS", "CGO_LDFLAGS", "CGO_ENABLED",
    "GCCGO", "GODEBUG",
)

def sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

def assert_pin_file(path: Path, expected: str) -> None:
    if path.is_symlink() or not path.is_file() or sha(path) != expected:
        raise RuntimeError(f"pinned file changed or missing: {path}")

def verify_pins(path: Path, expected_sha: str) -> dict:
    assert_pin_file(path, expected_sha)
    pins = json.loads(path.read_text(encoding="utf-8"))
    if pins.get("schema") != 1:
        raise RuntimeError("unsupported source-pins schema")
    for source_path, expected in pins["files"].items():
        assert_pin_file(Path(source_path), expected)
    compiler = pins["compiler_checkout"]
    compiler_root = Path(compiler["path"])
    if git(compiler_root, "rev-parse", "HEAD") != compiler["head"]:
        raise RuntimeError("pinned Bend compiler HEAD changed")
    compiler_status = subprocess.check_output(
        ["git", "-C", str(compiler_root), "status", "--porcelain", "--untracked-files=all"],
        text=True).splitlines()
    if compiler_status != compiler["status_lines"]:
        raise RuntimeError("pinned Bend compiler checkout status changed")
    for name in ("baseline", "owned"):
        row = pins["worktrees"][name]
        root = Path(row["path"])
        if git(root, "rev-parse", "HEAD") != row["head"]:
            raise RuntimeError(f"{name} worktree HEAD changed")
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            text=True).splitlines()
        if status != row["status_lines"]:
            raise RuntimeError(f"{name} worktree status changed")
        diff = subprocess.check_output(["git", "-C", str(root), "diff", "--binary", "HEAD"])
        if hashlib.sha256(diff).hexdigest() != row["tracked_diff_sha256"]:
            raise RuntimeError(f"{name} tracked diff changed")
        for rel, want in row["changed_file_sha256"].items():
            assert_pin_file(root / rel, want)
    pre = pins["source_preflight"]
    report_path = Path(pre["path"])
    assert_pin_file(report_path, pre["sha256"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (report.get("builds_started") is not False or report.get("attempts_started") is not False
            or report.get("performance_claims") is not False
            or report.get("acceptance_claims") is not False
            or report.get("profile") != "chrome146"
            or report.get("tls_version") != 772 or report.get("tls_cipher") != 4865):
        raise RuntimeError("source-preflight scope/profile flags differ")
    client_build = pins["client_build"]
    build_session_path = Path(client_build["session_path"])
    assert_pin_file(build_session_path, client_build["session_sha256"])
    if client_build["commands_completed"] != 10:
        raise RuntimeError("client build did not complete exactly ten commands")
    build_session = json.loads(build_session_path.read_text(encoding="utf-8"))
    if (build_session.get("status") != "builds-complete"
            or build_session.get("commands_planned") != 10
            or build_session.get("commands_completed") != 10
            or build_session.get("prebuild_pins") != build_session.get("postbuild_pins")
            or build_session.get("client_invocations") is not False
            or build_session.get("peer_invocations") is not False
            or build_session.get("workload_attempts") is not False
            or build_session.get("campaign_started") is not False):
        raise RuntimeError("client build session is incomplete or claims execution")
    assert_pin_file(Path(client_build["commands_path"]), client_build["commands_sha256"])
    backend = pins["backend"]
    assert_pin_file(Path(backend["prefix_manifest_path"]), backend["prefix_manifest_sha256"])
    assert_pin_file(Path(backend["build_validation_path"]), backend["build_validation_sha256"])
    dso = Path(backend["dso_realpath"])
    if dso.resolve() != DSO_REAL or sha(dso) != DSO_SHA or backend["dso_sha256"] != DSO_SHA:
        raise RuntimeError("private diagnostic DSO path/hash differs")
    link = Path(backend["dso_link"])
    if not link.is_symlink() or link.resolve() != DSO_REAL:
        raise RuntimeError("private diagnostic DSO symlink differs")
    if set(pins["binaries"]) != {
            "baseline_stream", "baseline_roundtrip", "owned_stream", "owned_roundtrip",
            "stream_peer", "roundtrip_peer"}:
        raise RuntimeError("binary pin inventory differs from the six reviewed executables")
    for key, info in pins["binaries"].items():
        assert_pin_file(Path(info["path"]), info["sha256"])
    return pins

def import_helpers(pins: dict, cache: Path) -> tuple[dict, object, object, object]:
    if cache.exists() and (not cache.is_dir() or any(cache.iterdir())):
        raise RuntimeError("run-local bytecode cache must be fresh and empty")
    cache.mkdir(parents=True, exist_ok=True)
    prefix = str(cache.resolve())
    sys.pycache_prefix = prefix
    os.environ["PYTHONPYCACHEPREFIX"] = prefix
    if sys.pycache_prefix != prefix:
        raise RuntimeError("interpreter rejected the run-local bytecode prefix")
    names = ("websocket", "websocket_streaming", "build", "lab", "ws_lab", "mechanism_validation")
    if any(name in sys.modules for name in names):
        raise RuntimeError("frozen helper imported before isolated bytecode setup")
    base = Path(pins["worktrees"]["baseline"]["path"])
    sys.path[:0] = [str(base / "benchmarks"), str(base / "tests"), str(base / "scripts"), str(ARTIFACTS)]
    websocket = importlib.import_module("websocket")
    streaming = importlib.import_module("websocket_streaming")
    build = importlib.import_module("build")
    lab = importlib.import_module("lab")
    ws_lab = importlib.import_module("ws_lab")
    validation = importlib.import_module("mechanism_validation")
    cryptography = importlib.import_module("cryptography")
    crypto_path = Path(cryptography.__file__).resolve()
    if pins["cryptography"]["path"] != str(crypto_path):
        raise RuntimeError("cryptography source origin differs from preflight pin")
    if pins["cryptography"]["version"] != importlib.metadata.version("cryptography"):
        raise RuntimeError("cryptography distribution version differs from preflight pin")
    if pins["cryptography"]["sha256"] != sha(crypto_path):
        raise RuntimeError("cryptography source differs from preflight pin")
    expected = {
        "cryptography": crypto_path,
        "websocket": base / "benchmarks/websocket.py",
        "websocket_streaming": base / "benchmarks/websocket_streaming.py",
        "build": base / "scripts/build.py",
        "lab": base / "tests/lab.py",
        "ws_lab": base / "tests/ws_lab.py",
        "mechanism_validation": ARTIFACTS / "mechanism_validation.py",
    }
    provenance = {}
    for name, expected_path in expected.items():
        module = sys.modules[name]
        source = Path(module.__file__).resolve()
        cached_value = getattr(module, "__cached__", None)
        if source != expected_path.resolve() or not cached_value:
            raise RuntimeError(f"helper source origin mismatch: {name}")
        cached = Path(cached_value).resolve()
        if not cached.is_file() or not cached.is_relative_to(cache.resolve()):
            raise RuntimeError(f"helper bytecode escaped fresh cache: {name}")
        source_hash = sha(source)
        if pins["files"].get(str(source)) != source_hash:
            raise RuntimeError(f"helper source hash differs from pins: {name}")
        provenance[name] = {
            "source_path": str(source), "source_sha256": source_hash,
            "cache_path": str(cached), "cache_sha256": sha(cached),
        }
    expected_streams = [
        {"name": f"stream-{size}b-flush{batch}", "bytes": size, "count": count,
         "peer_flush_frames": batch, "connections": 1}
        for batch in (1, 64) for size, count in ((30, 65536), (1024, 16384), (65536, 1024))
    ]
    if streaming.WORKLOADS != expected_streams:
        raise RuntimeError("frozen stream workload declaration changed")
    selected_rt = [row for row in websocket.WORKLOADS if row["name"] in ("go-30b-c1", "go-65536b-c1")]
    expected_rt = [
        {"name": "go-30b-c1", "peer": "go", "bytes": 30, "connections": 1, "count": 5000},
        {"name": "go-65536b-c1", "peer": "go", "bytes": 65536, "connections": 1, "count": 300},
    ]
    if selected_rt != expected_rt:
        raise RuntimeError("frozen selected Go round-trip cells changed")
    if websocket.ORIGINAL != b"scrapanium-websocket-benchmark":
        raise RuntimeError("frozen round-trip warmup payload changed")
    return provenance, streaming, websocket, validation

def sanitized_env(prefix: Path, cache: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in CLEAR_ENV}
    env.update({
        "LD_LIBRARY_PATH": str(prefix / "lib"),
        "SCRAPANIUM_CURL_DIR": str(prefix),
        "PYTHONPYCACHEPREFIX": str(cache.resolve()),
        "PYTHONNOUSERSITE": "1",
    })
    return env

def peer_env_for(env: dict[str, str]) -> dict[str, str]:
    result = dict(env)
    result.pop("LD_LIBRARY_PATH", None)
    result.pop("SCRAPANIUM_CURL_DIR", None)
    return result

def start_peer(binary: Path, cert: str, key: Path, env: dict[str, str], line_reader,
               stderr_path: Path):
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stderr_path.open("wb") as stderr_stream:
        process = subprocess.Popen(
            [str(binary), "-cert", cert, "-key", str(key)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_stream,
            text=True, env=env,
        )
    try:
        url = line_reader(process)
        if not url.startswith("wss://127.0.0.1:"):
            raise RuntimeError("peer did not provide a loopback WSS URL")
        return process, url
    except Exception:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
                process.stdin = None
        except OSError:
            pass
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout and not process.stdout.closed:
            process.stdout.close()
        raise

def stop_peer(process) -> int:
    if process.stdin and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.stdin = None
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if process.stdout and not process.stdout.closed:
        process.stdout.close()
    return process.returncode

def peer_log_reader(process, captured: list[str], timeout: int = 30):
    if not select.select([process.stdout], [], [], timeout)[0]:
        raise RuntimeError("Go peer did not write its next protocol line")
    raw = process.stdout.readline()
    captured.append(raw)
    line = raw.strip()
    if not line:
        raise RuntimeError("Go peer exited before its next protocol line")
    return line

def peer_artifact_record(out: Path, name: str, exit_code: int,
                         stdout_lines: list[str], stderr_path: Path) -> dict:
    stdout_rel = f"peers/{name}.stdout.txt"
    stdout_path = out / stdout_rel
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("".join(stdout_lines), encoding="utf-8")
    if not stderr_path.is_file():
        stderr_path.write_bytes(b"")
    return {
        "name": name, "exit_code": exit_code,
        "stdout_file": stdout_rel, "stdout_sha256": sha(stdout_path),
        "stderr_file": str(stderr_path.relative_to(out)), "stderr_sha256": sha(stderr_path),
    }

def load_peer_log(out: Path, relative: str, expected_sha: str) -> bytes:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise RuntimeError("peer log path escapes run output")
    path = out / rel
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(out.resolve()):
        raise RuntimeError("peer log missing or escaped run output")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise RuntimeError("peer log checksum mismatch")
    return raw

def query_peer(process, key: str, line_reader, expected_frames: int) -> dict:
    if process.poll() is not None or not process.stdin:
        raise RuntimeError("Go peer exited before stats query")
    for _ in range(100):
        process.stdin.write("stats\n")
        process.stdin.flush()
        snapshot = json.loads(line_reader(process))
        row = snapshot.get(key)
        if row and (row.get("error") or row.get("data_frames", row.get("frames")) == expected_frames):
            return row
        time.sleep(0.01)
    raise RuntimeError("Go peer counters did not reach expected frames")

def read_counter_file(run_dir: Path, relative: str) -> tuple[list[dict], str]:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise RuntimeError("counter path escapes run output")
    path = run_dir / rel
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(run_dir.resolve()):
        raise RuntimeError("counter file missing or escaped run output")
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    return rows, hashlib.sha256(raw).hexdigest()

def load_counters(run_dir: Path, relative: str, expected_sha: str):
    rows, actual_sha = read_counter_file(run_dir, relative)
    if actual_sha != expected_sha:
        raise ValueError("counter file checksum mismatch")
    return rows

def write_status(path: Path, status: str, count: int, **extra) -> None:
    write_json(path, {
        "schema": 1, "status": status, "attempt_count": count,
        "builds_started": False, "attempts_started": count > 0,
        "performance_claims": False, "acceptance_claims": False, **extra,
    })

def expected_peer(row: dict, kind: str) -> dict:
    if row.get("error"):
        raise RuntimeError("Go peer reported an error")
    if kind == "stream":
        fields = ("tls_version", "tls_cipher", "warmups", "starts", "data_frames",
                  "data_payload_bytes", "data_frame_bytes")
    else:
        fields = ("tls_version", "tls_cipher", "frames", "payload_bytes",
                  "client_frame_bytes", "server_frame_bytes")
    return {"kind": kind, **{field: row.get(field) for field in fields}}

def run_preflight(pins_path: Path, pins_sha: str, out: Path):
    pins = verify_pins(pins_path, pins_sha)
    cache = out / "python-bytecode-cache"
    provenance, streaming, websocket, validation = import_helpers(pins, cache)
    verify_pins(pins_path, pins_sha)
    schema = pins["counter_schema"]
    if len(schema["native"]) != 61 or len(schema["curl"]) != 60:
        raise RuntimeError("counter schema slot count differs from reviewed ABI")
    return pins, provenance, streaming, websocket, validation, schema

def preflight_only(pins_path: Path, pins_sha: str, out: Path) -> None:
    if out.exists() or out.is_symlink():
        raise RuntimeError("preflight output must be fresh")
    if not out.resolve().is_relative_to(ARTIFACTS):
        raise RuntimeError("preflight output must remain under owned-mechanism-02 artifacts")
    out.mkdir(parents=True)
    pins, provenance, _, _, _, schema = run_preflight(pins_path, pins_sha, out)
    payload = {
        "schema": 1, "status": "preflight_passed",
        "source_pins_path": str(pins_path.resolve()), "source_pins_sha256": pins_sha,
        "source_preflight_sha256": pins["source_preflight"]["sha256"],
        "helper_provenance": provenance, "counter_schema": schema,
        "builds_started": False, "attempts_started": False,
        "performance_claims": False, "acceptance_claims": False,
    }
    write_json(out / "preflight.json", payload)
    write_status(out / "status.json", "preflight_passed", 0, source_pins_stable=True,
                 preflight_sha256=sha(out / "preflight.json"))

def _run_fixed(pins_path: Path, pins_sha: str, out: Path) -> None:
    if out.exists() or out.is_symlink():
        raise RuntimeError("execution output path must be fresh; refusing overwrite")
    if not out.resolve().is_relative_to(ARTIFACTS):
        raise RuntimeError("execution output must remain under owned-mechanism-02 artifacts")
    out.mkdir(parents=True)
    pins, attempts = {}, []
    attempts_started = False
    current_attempt = None
    current_counter_path = None
    peer_records = []
    peer_cleanup_errors = []
    state_path = out / "status.json"
    stage = "source_preflight"
    try:
        pins, provenance, streaming, websocket, validation, schema = run_preflight(pins_path, pins_sha, out)
        schedule = validation.fixed_schedule()
        write_json(out / "planned_schedule.json", {
            "schema": 1, "status": "fixed_before_execution",
            "planned_attempts": 16, "attempts": schedule,
        })
        write_json(out / "preflight.json", {
            "schema": 1, "status": "preflight_passed",
            "source_pins_path": str(pins_path.resolve()), "source_pins_sha256": pins_sha,
            "source_preflight_sha256": pins["source_preflight"]["sha256"],
            "helper_provenance": provenance, "counter_schema": schema,
            "builds_started": False, "attempts_started": False,
            "performance_claims": False, "acceptance_claims": False,
        })
        (out / "attempts.jsonl").write_text("", encoding="utf-8")
        write_status(state_path, "prepared", 0, planned_attempts=16)
        env = sanitized_env(Path(pins["backend"]["prefix"]), out / "python-bytecode-cache")
        peer_env = peer_env_for(env)
        binaries = pins["binaries"]
        stream_peer = roundtrip_peer = None
        stream_stdout_lines, roundtrip_stdout_lines = [], []
        stream_stderr_path = out / "peers/stream.stderr.txt"
        roundtrip_stderr_path = out / "peers/roundtrip.stderr.txt"
        stage = "peer_setup"
        with tempfile.TemporaryDirectory(prefix="wss-mechanism-counters-") as temp:
            _, ca = websocket.certificate(temp)
            cert_dir = Path(temp)
            try:
                stream_peer, stream_url = start_peer(Path(binaries["stream_peer"]["path"]), ca,
                    cert_dir / "key.pem", peer_env,
                    lambda process: peer_log_reader(process, stream_stdout_lines),
                    stream_stderr_path)
                roundtrip_peer, roundtrip_url = start_peer(Path(binaries["roundtrip_peer"]["path"]), ca,
                    cert_dir / "key.pem", peer_env,
                    lambda process: peer_log_reader(process, roundtrip_stdout_lines),
                    roundtrip_stderr_path)
                for planned in schedule:
                    index = planned["attempt_index"]
                    current_attempt = dict(planned)
                    current_counter_path = None
                    variant = planned["variant"]
                    stage = f"attempt_{index:02d}_client"
                    binary_key = f"{variant}_{'stream' if planned['kind'] == 'stream' else 'roundtrip'}"
                    binary = Path(binaries[binary_key]["path"])
                    run_id = f"mechanism-{index:02d}-{variant}-{planned['cell']}"
                    counter_rel = f"counters/attempt-{index:02d}.jsonl"
                    counter_path = out / counter_rel
                    current_counter_path = counter_path
                    counter_path.parent.mkdir(exist_ok=True)
                    if counter_path.exists():
                        raise RuntimeError("counter path already exists")
                    attempt_env = dict(env)
                    attempt_env["SCRAPANIUM_MECHANISM_COUNTERS"] = str(counter_path)
                    write_status(state_path, "running", len(attempts), planned_attempts=16,
                                 next_attempt=index)
                    if planned["kind"] == "stream":
                        body = cert_dir / f"body-{index:02d}.bin"
                        body.write_bytes(bytes((i * 31) % 128 for i in range(planned["bytes"] - 8)))
                        workload = {
                            "name": planned["cell"], "bytes": planned["bytes"],
                            "count": planned["count"], "peer_flush_frames": planned["peer_flush_frames"],
                            "connections": 1,
                        }
                        attempts_started = True
                        write_status(state_path, "running", len(attempts), attempts_started=True,
                                     planned_attempts=16, next_attempt=index)
                        observed = streaming.sample(
                            streaming.CLIENTS[0], binary, stream_url, ca, body, workload,
                            run_id, attempt_env, DSO_SHA, 1,
                        )
                        stage = f"attempt_{index:02d}_peer_stats"
                        raw_peer = query_peer(stream_peer, run_id,
                            lambda process: peer_log_reader(process, stream_stdout_lines),
                            planned["count"])
                    else:
                        size = planned["bytes"]
                        payload = websocket.ORIGINAL if size == len(websocket.ORIGINAL) else bytes(
                            (i * 131 + 17) % 256 for i in range(size))
                        payload_path = cert_dir / f"payload-{index:02d}.bin"
                        payload_path.write_bytes(payload)
                        workload = {
                            "name": planned["cell"], "peer": "go", "bytes": size,
                            "connections": 1, "count": planned["count"],
                        }
                        attempts_started = True
                        write_status(state_path, "running", len(attempts), attempts_started=True,
                                     planned_attempts=16, next_attempt=index)
                        observed = websocket.sample(
                            websocket.CLIENTS[0], binary, roundtrip_url, ca, payload_path,
                            workload, run_id, attempt_env,
                        )
                        stage = f"attempt_{index:02d}_peer_stats"
                        raw_peer = query_peer(roundtrip_peer, run_id + "-0",
                            lambda process: peer_log_reader(process, roundtrip_stdout_lines),
                            planned["count"] + 1)
                    stage = f"attempt_{index:02d}_mapping"
                    mappings = ([observed.get("mapped_backend")] if planned["kind"] == "stream"
                                else observed.get("mapped_backends"))
                    if not isinstance(mappings, list) or len(mappings) != 1:
                        raise RuntimeError("expected exactly one client backend mapping")
                    mapping = mappings[0]
                    if mapping != {"path": str(DSO_REAL), "sha256": DSO_SHA}:
                        raise RuntimeError("client did not map the exact private diagnostic DSO")
                    counter_rows, counter_sha = read_counter_file(out, counter_rel)
                    attempt = {
                        **planned, "client": "scrapanium-bend", "attempt_status": "ok",
                        "returncode": 0, "exact_checker_success": True,
                        "mapped_backend": mapping, "peer_observation": expected_peer(raw_peer, planned["kind"]),
                        "counter_file": counter_rel, "counter_file_sha256": counter_sha,
                    }
                    validation.validate_attempt(attempt, planned, schema, counter_rows, str(DSO_REAL))
                    attempts.append(attempt)
                    with (out / "attempts.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(attempt, sort_keys=True, separators=(",", ":")) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    write_status(state_path, "running", len(attempts), planned_attempts=16)
            finally:
                active_error = sys.exc_info()[0] is not None
                for name, process, captured, stderr_path in (
                    ("stream", stream_peer, stream_stdout_lines, stream_stderr_path),
                    ("roundtrip", roundtrip_peer, roundtrip_stdout_lines, roundtrip_stderr_path),
                ):
                    if process is None:
                        continue
                    try:
                        peer_exit = stop_peer(process)
                    except Exception as cleanup_exc:
                        peer_exit = process.poll()
                        peer_cleanup_errors.append({"name": name, "error_type": type(cleanup_exc).__name__})
                    try:
                        peer_records.append(peer_artifact_record(
                            out, name, peer_exit, captured, stderr_path))
                    except Exception as log_exc:
                        peer_cleanup_errors.append({"name": name, "log_error_type": type(log_exc).__name__})
                    if peer_exit != 0:
                        peer_cleanup_errors.append({"name": name, "exit_code": peer_exit})
                if peer_cleanup_errors and not active_error:
                    raise RuntimeError("peer shutdown or log capture failed")
        stage = "final_validation"
        if peer_cleanup_errors:
            raise RuntimeError("peer shutdown or log capture failed")
        report = {
            "schema": 1, "status": "complete", "scope": "fixed 16-attempt counters-only census",
            "source_pins_path": str(pins_path.resolve()), "source_pins_sha256": pins_sha,
            "source_preflight_sha256": pins["source_preflight"]["sha256"],
            "counter_schema": schema, "dso_realpath": str(DSO_REAL), "dso_sha256": DSO_SHA,
            "planned_attempts": 16, "attempt_count": len(attempts), "attempts": attempts,
            "peer_processes": peer_records,
            "builds_started": False, "attempts_started": True,
            "performance_claims": False, "acceptance_claims": False,
            "source_pins_stable_after_run": False,
        }
        verify_pins(pins_path, pins_sha)
        report["source_pins_stable_after_run"] = True
        validation.validate_report(
            report, schema,
            lambda rel, want: load_counters(out, rel, want),
            lambda rel, want: load_peer_log(out, rel, want),
        )
        write_json(out / "report.json", report)
        write_status(state_path, "complete", len(attempts), planned_attempts=16,
                     source_pins_stable=True, report_sha256=sha(out / "report.json"))
    except Exception as exc:
        failed_attempt = None
        if current_attempt is not None:
            failed_attempt = {**current_attempt, "failure_stage": stage,
                              "failure_error": type(exc).__name__}
            if current_counter_path is not None and current_counter_path.is_file():
                failed_attempt["counter_file"] = str(current_counter_path.relative_to(out))
                failed_attempt["counter_file_sha256"] = sha(current_counter_path)
        write_status(state_path, "failed", len(attempts), attempts_started=attempts_started,
                     planned_attempts=16, failed_stage=stage, failure_class=type(exc).__name__,
                     failure_error=type(exc).__name__, failed_attempt=failed_attempt,
                     peer_processes=peer_records, peer_cleanup_errors=peer_cleanup_errors)
        raise

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("preflight", "execute-fixed-16"))
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--pins-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if Path(sys.executable).resolve() != PYTHON:
        parser.error(f"run with pinned interpreter {PYTHON}")
    if os.environ.get("LD_PRELOAD") or os.environ.get("LD_AUDIT"):
        parser.error("LD_PRELOAD and LD_AUDIT must be unset before interpreter launch")
    output = args.output.absolute()
    if args.mode == "preflight":
        preflight_only(args.pins, args.pins_sha256, output)
        started = False
    else:
        _run_fixed(args.pins, args.pins_sha256, output)
        started = True
    print(json.dumps({
        "status": json.loads((output / "status.json").read_text())["status"],
        "output": str(output.resolve()),
        "status_sha256": sha(output / "status.json"),
        "builds_started": False,
        "attempts_started": started,
        "performance_claims": False,
        "acceptance_claims": False,
    }, sort_keys=True))

if __name__ == "__main__":
    main()
