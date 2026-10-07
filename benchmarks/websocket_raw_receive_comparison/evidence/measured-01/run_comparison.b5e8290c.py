#!/usr/bin/env python3
"""Predeclared paired WSS comparison. This runner is never invoked by normal tests."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import itertools
import json
import os
import platform
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback

BASELINE = "c4927171753a3cdcb12986f06c598219bba6e5b4"
CFFI = "8cd226f24a06f81d66c42fbdb2a7c49305b1b7b7"
DSO_SHA = "bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3"
BEND = "63bee70b55a71024d6bdcb49a745111bc54b114e"
CFFI_VERSION = "0.16.4b1"
SHARED = (
    "scrapanium.bend", "http.bend", "dependencies.json", "scripts/build.py",
    "benchmarks/websocket.py", "benchmarks/websocket.bend", "benchmarks/websocket_python.py",
    "benchmarks/websocket_server.go", "benchmarks/websocket_streaming.py",
    "benchmarks/websocket_stream.bend", "benchmarks/websocket_stream_python.py",
    "benchmarks/websocket_stream_server.go", "benchmarks/websocket_clock.c",
    "tests/lab.py", "tests/ws_lab.py",
)
VARIANTS = ("baseline", "candidate", "curl_cffi")
TLS_VERSION = 772
TLS_CIPHER = 4865
RUN_ENVIRONMENT_KEYS = ("PYTHONPATH", "BEND_SOURCE", "BUN", "CC",
                       "SCRAPANIUM_CURL_DIR", "LD_LIBRARY_PATH")
_ACTIVE_RUN_OUT: Path | None = None


def balanced_orders(seed, runs, workload_names):
    """Return seeded per-workload orders containing each permutation once per six-run block."""
    rng = random.Random(seed)
    permutations = list(itertools.permutations(VARIANTS))
    result = {}
    for name in workload_names:
        orders = []
        for _ in range((runs + 5) // 6):
            block = permutations.copy()
            rng.shuffle(block)
            orders.extend(block)
        result[name] = orders[:runs]
    return result


def cpu_identity():
    model = platform.processor() or None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith(("model name", "hardware")):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except AttributeError:
        affinity = None
    return {"model": model, "logical_count": os.cpu_count(), "affinity": affinity}


def smoke_projection(result):
    """Keep correctness evidence while discarding every timing/throughput field."""
    keys = ("run_id", "client", "fault", "returncode", "detected_by_exact_check",
            "expected_corpus_payload_bytes", "mapped_backend", "mapped_backends",
            "peer_observation")
    return {key: result[key] for key in keys if key in result}


class AttemptValidationError(RuntimeError):
    def __init__(self, message, partial_result):
        super().__init__(message)
        self.partial_result = partial_result


def validate_with_result(result, validation):
    try:
        validation()
    except Exception as exc:
        raise AttemptValidationError(str(exc), result) from exc
    return result


def record_attempt(path, attempts, phase, workload, repeat, variant, position, run_id,
                   action, smoke=False):
    row = {"utc": now(), "phase": phase, "workload": workload["name"],
           "repeat": repeat, "variant": variant, "variant_order_index": position,
           "run_id": run_id}
    try:
        result = action()
        row["result"] = smoke_projection(result) if smoke else result
        row["status"] = "ok"
    except Exception as exc:
        partial = getattr(exc, "partial_result", None)
        if partial is not None:
            row["result"] = smoke_projection(partial) if smoke else partial
        row.update(status="error", error_type=type(exc).__name__, error=str(exc),
                   traceback=traceback.format_exc())
        logrow(path, row)
        attempts.append(row)
        raise
    logrow(path, row)
    attempts.append(row)
    return row["result"]


def check_peer_tls(observed):
    if observed.get("tls_version") != TLS_VERSION or observed.get("tls_cipher") != TLS_CIPHER:
        raise RuntimeError(f"unexpected peer TLS identity: version={observed.get('tls_version')} "
                           f"cipher={observed.get('tls_cipher')}; expected {TLS_VERSION}/{TLS_CIPHER}")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def sources(root):
    files = [root / name for name in SHARED]
    files += sorted(p for p in (root / "native").iterdir() if p.is_file())
    if any(not p.is_file() for p in files):
        raise RuntimeError(f"benchmark inputs missing under {root}")
    return {str(p.relative_to(root)): digest(p) for p in files}


def native_map(root):
    return {p.name: digest(p) for p in sorted((root / "native").iterdir()) if p.is_file()}


def native_blob_map(root):
    names = git(root, "ls-tree", "-r", "--name-only", BASELINE, "--", "native").splitlines()
    result = {}
    for name in names:
        blob = subprocess.check_output(["git", "-C", str(root), "show", f"{BASELINE}:{name}"])
        result[name.removeprefix("native/")] = hashlib.sha256(blob).hexdigest()
    return result


def pinned_runtime_environment(prefix, inherited=None):
    """Return the shared child/binding-inspection environment and backend library dir."""
    prefix = Path(prefix).resolve()
    library = (prefix / "lib" if (prefix / "lib/libcurl-impersonate.so").exists() else prefix).resolve()
    environment = dict(os.environ if inherited is None else inherited)
    inherited_library_path = environment.get("LD_LIBRARY_PATH", "")
    environment["LD_LIBRARY_PATH"] = str(library) + (
        os.pathsep + inherited_library_path if inherited_library_path else "")
    environment["SCRAPANIUM_CURL_DIR"] = str(prefix)
    return environment, library


class BindingProbeError(RuntimeError):
    def __init__(self, command, returncode, stdout, stderr, selected_environment):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.selected_environment = selected_environment
        super().__init__(f"matched curl_cffi binding probe exited {returncode}: {stderr.strip()}")


def binding_info(python, env=None):
    code = ("import curl_cffi,hashlib,json,pathlib,sys; p=pathlib.Path(curl_cffi.__file__).parent; "
            "fs=[p/'__init__.py',p/'curl.py',p/'requests/websockets.py',*p.glob('_wrapper*.so')]; "
            "print(json.dumps({'version':curl_cffi.__version__,'python':sys.version,"
            "'executable':str(pathlib.Path(sys.executable).resolve()),'executable_sha256':hashlib.sha256(pathlib.Path(sys.executable).resolve().read_bytes()).hexdigest(),"
            "'module_root':str(p.resolve()),'sys_path':sys.path,'files':{str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for f in fs}}))")
    command = [str(python), "-c", code]
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    if result.returncode:
        selected_environment = {key: (env or os.environ).get(key, "")
                                for key in RUN_ENVIRONMENT_KEYS}
        raise BindingProbeError(command, result.returncode, result.stdout, result.stderr,
                                selected_environment)
    return json.loads(result.stdout)


def run_build(name, command, cwd, env, logdir):
    path = logdir / f"{name}.json"
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
        record = {"name": name, "command": command, "cwd": str(cwd),
                  "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as exc:
        record = {"name": name, "command": command, "cwd": str(cwd),
                  "returncode": None, "error_type": type(exc).__name__, "error": str(exc)}
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        record["log_path"] = str(path)
        record["log_sha256"] = digest(path)
        raise RuntimeError(f"build {name} could not start; see {path}") from exc
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    record["log_path"] = str(path)
    record["log_sha256"] = digest(path)
    if result.returncode:
        raise RuntimeError(f"build {name} failed with exit status {result.returncode}; see {path}")
    return record

def snapshot(root):
    diff = subprocess.check_output(["git", "-C", str(root), "diff", "--binary", "HEAD"])
    return {"path": str(root), "head": git(root, "rev-parse", "HEAD"),
            "branch": git(root, "branch", "--show-current"),
            "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
            "status": git(root, "status", "--porcelain"), "source_sha256": sources(root)}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json_durable(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def initialize_run_status(out, *, inputs, options):
    """Reserve a fresh output directory and durably record preflight before subprocesses."""
    global _ACTIVE_RUN_OUT
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir()
    _ACTIVE_RUN_OUT = out
    write_json_durable(out / "status.json", {
        "schema": 1,
        "status": "preflight",
        "created_utc": now(),
        "command": [sys.executable, *sys.argv],
        "inputs": inputs,
        "options": options,
        "selected_environment": {key: os.environ.get(key, "") for key in RUN_ENVIRONMENT_KEYS},
        "builds_started": False,
        "attempts_started": False,
    })
    return out


def update_run_status(out, **fields):
    path = Path(out) / "status.json"
    status = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    status.update(fields)
    write_json_durable(path, status)


def record_run_failure(out, exc):
    path = Path(out) / "status.json"
    if not path.exists():
        return
    status = json.loads(path.read_text(encoding="utf-8"))
    if status.get("status") == "failed":
        return
    status.update({
        "status": "failed",
        "finished_utc": now(),
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback": traceback.format_exc(),
    })
    if isinstance(exc, BindingProbeError):
        status["failed_subprocess"] = {
            "command": exc.command,
            "returncode": exc.returncode,
            "stdout": exc.stdout,
            "stderr": exc.stderr,
            "selected_environment": exc.selected_environment,
        }
    write_json_durable(path, status)


def logrow(path, row):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def interval(values, seed):
    rng = random.Random(seed)
    med = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(10000))
    return [med[249], med[9749]]


def main():
    global _ACTIVE_RUN_OUT
    _ACTIVE_RUN_OUT = None
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--candidate", type=Path, required=True)
    ap.add_argument("--curl-cffi-checkout", type=Path, required=True)
    ap.add_argument("--bend-source", type=Path, required=True)
    ap.add_argument("--bun", type=Path, required=True)
    ap.add_argument("--curl-prefix", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True, help="fresh run directory; never reuse")
    ap.add_argument("--cc", default=os.environ.get("CC", "clang"))
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--smoke", action="store_true", help="correctness-only, reduced counts; never a performance result")
    ap.add_argument("--seed", type=int, default=20261005)
    a = ap.parse_args()
    base, cand, cffi = (a.baseline.resolve(), a.candidate.resolve(), a.curl_cffi_checkout.resolve())
    bend, bun, prefix, out = a.bend_source.resolve(), a.bun.resolve(), a.curl_prefix.resolve(), a.output.resolve()
    if base == cand or not base.is_dir() or not cand.is_dir():
        ap.error("baseline and candidate must be distinct existing checkouts")
    if out.exists():
        ap.error("output path exists; use a fresh path so attempts are never overwritten")
    if a.runs is None:
        a.runs = 1 if a.smoke else 12
    try:
        initialize_run_status(out,
            inputs={"baseline": str(base), "candidate": str(cand),
                    "curl_cffi_checkout": str(cffi), "bend_source": str(bend),
                    "bun": str(bun), "curl_prefix": str(prefix)},
            options={"cc": a.cc, "runs": a.runs, "smoke": a.smoke, "seed": a.seed})
    except FileExistsError:
        ap.error("output path was created concurrently; use a fresh path")
    if a.smoke:
        if a.runs < 1:
            ap.error("smoke --runs must be positive")
    elif a.runs < 6 or a.runs % 6:
        ap.error("performance --runs must be at least 6 and divisible by 6")
    if not bun.is_file():
        ap.error("pass the pinned Bun executable with --bun")
    bun_version = subprocess.check_output([str(bun), "--version"], text=True).strip()
    if bun_version != "1.3.11":
        ap.error(f"Bun must be pinned at 1.3.11, got {bun_version}")
    if git(base, "rev-parse", "HEAD") != BASELINE:
        ap.error(f"baseline must be exact commit {BASELINE}")
    if git(base, "status", "--porcelain", "--untracked-files=no"):
        ap.error("baseline tracked tree must be clean")
    baseline_native = native_map(base)
    if baseline_native != native_blob_map(base):
        ap.error("baseline native files do not match the c492717 Git blobs")
    candidate_native = native_map(cand)
    if set(candidate_native) != set(baseline_native):
        ap.error("candidate and baseline native file sets differ")
    changed_native = {name for name in baseline_native
                      if baseline_native[name] != candidate_native[name]}
    if changed_native != {"websocket.inc.c"}:
        ap.error(f"candidate may change only native/websocket.inc.c; found {sorted(changed_native)}")
    if git(cffi, "rev-parse", "HEAD") != CFFI or git(cffi, "status", "--porcelain"):
        ap.error("curl_cffi checkout must be clean at the recorded matched commit")
    if git(bend, "rev-parse", "HEAD") != BEND or git(bend, "status", "--porcelain"):
        ap.error("Bend compiler checkout must be clean at the recorded shared compiler commit")
    runtime_env, library = pinned_runtime_environment(prefix)
    dso = (library / "libcurl-impersonate.so").resolve()
    if not dso.is_file() or digest(dso) != DSO_SHA:
        ap.error("stock libcurl DSO missing or changed; review the pin before comparison")
    curl_python = cand / ".deps/venv-matched/bin/python"
    if not curl_python.is_file():
        ap.error(f"candidate matched CFFI Python missing: {curl_python}")

    sys.path.insert(0, str(cand / "benchmarks"))
    sys.path.insert(0, str(cand / "tests"))
    sys.path.insert(0, str(cand / "scripts"))
    import websocket as rt
    import websocket_streaming as stream
    if rt.ROOT.resolve() != cand or stream.ROOT.resolve() != cand:
        ap.error("existing benchmark helpers did not load from candidate checkout")
    if len(stream.WORKLOADS) != 6 or len(rt.WORKLOADS) != 7:
        ap.error("existing frozen workload matrix changed")
    baseline_snap, candidate_snap = snapshot(base), snapshot(cand)
    for name in SHARED:
        if baseline_snap["source_sha256"][name] != candidate_snap["source_sha256"][name]:
            ap.error(f"baseline/candidate checker or workload source differs: {name}")
    expected_streams = [
        {"name": f"stream-{s}b-flush{b}", "bytes": s, "count": n,
         "peer_flush_frames": b, "connections": 1}
        for b in (1, 64) for s, n in ((30, 65536), (1024, 16384), (65536, 1024))
    ]
    if stream.WORKLOADS != expected_streams:
        ap.error("stream workload sizes/counts no longer match the frozen six")
    if [w["name"] for w in rt.WORKLOADS] != [
        "original-python-30b-c1", "go-30b-c1", "go-1024b-c1", "go-65536b-c1",
        "go-30b-c4", "go-1024b-c4", "go-65536b-c4",
    ]:
        ap.error("round-trip workload matrix changed")
    stream_matrix = [dict(w, count=32) for w in stream.WORKLOADS] if a.smoke else [dict(w) for w in stream.WORKLOADS]
    roundtrip_matrix = [dict(w, count=20) for w in rt.WORKLOADS] if a.smoke else [dict(w) for w in rt.WORKLOADS]
    cffi_info = binding_info(curl_python, env=runtime_env)
    inherited_pythonpath = [Path(item).resolve() for item in os.environ.get("PYTHONPATH", "").split(os.pathsep) if item]
    local_snapshot_roots = [root for root in inherited_pythonpath if (root / "curl_cffi").is_dir()]
    if local_snapshot_roots and not all(any(Path(path).resolve().is_relative_to(root) for root in local_snapshot_roots)
                                        for path in cffi_info["files"]):
        ap.error("curl_cffi did not consistently load from the configured local package snapshot")
    if not any(Path(path).name.startswith("_wrapper") for path in cffi_info["files"]):
        ap.error("matched curl_cffi installed wrapper binary is missing")
    if cffi_info["version"] != CFFI_VERSION:
        ap.error("matched curl_cffi installed version differs from the frozen 0.16.4b1 checkout")
    for relative in ("__init__.py", "curl.py", "requests/websockets.py"):
        installed = next(Path(p) for p in cffi_info["files"] if p.endswith("/" + relative))
        if digest(installed) != digest(cffi / "curl_cffi" / relative):
            ap.error(f"installed curl_cffi checker source differs from checkout: {relative}")

    runner_path = Path(__file__).resolve()
    plan_path = runner_path.with_name("README.md")
    runner_sha_start, plan_sha_start = digest(runner_path), digest(plan_path)
    python_path = Path(sys.executable).resolve()
    python_sha_start = digest(python_path)
    compiler_path = Path(shutil.which(a.cc) or a.cc).resolve()
    compiler = subprocess.check_output([a.cc, "--version"], text=True)
    compiler_sha_start = digest(compiler_path)
    go = shutil.which("go")
    if not go:
        ap.error("Go is required to build the unchanged benchmark peers")
    go = str(Path(go).resolve())
    go_version = subprocess.check_output([go, "version"], text=True).strip()
    go_sha_start = digest(go)
    bun_sha_start = digest(bun)
    backend = {"path": str(dso), "sha256": digest(dso)}
    cpu = cpu_identity()

    builddir = out / "build"
    builddir.mkdir()
    buildlogdir = out / "build-logs"
    buildlogdir.mkdir()
    env = dict(runtime_env, BEND_SOURCE=str(bend), BUN=str(bun), CC=a.cc)
    bins = {}
    build_records = []
    update_run_status(out, status="building", builds_started=True,
                      attempts_started=False, build_started_utc=now())
    try:
        for variant, root in (("baseline", base), ("candidate", cand)):
            for kind, entry in (("stream", "websocket_stream.bend"), ("roundtrip", "websocket.bend")):
                binary = builddir / f"{variant}-{kind}"
                command = [sys.executable, str(root / "scripts/build.py"),
                           str(root / "benchmarks" / entry), "-o", str(binary)]
                build_records.append(run_build(f"{variant}-{kind}", command, root, env, buildlogdir))
                bins[f"{variant}_{kind}"] = binary
        for kind, source in (("stream_peer", "websocket_stream_server.go"),
                             ("roundtrip_peer", "websocket_server.go")):
            binary = builddir / kind
            command = [go, "build", "-trimpath", "-o", str(binary),
                       str(cand / "benchmarks" / source)]
            build_records.append(run_build(kind, command, cand, os.environ.copy(), buildlogdir))
            bins[kind] = binary
        if sources(base) != baseline_snap["source_sha256"] or sources(cand) != candidate_snap["source_sha256"]:
            raise RuntimeError("benchmark sources changed during compilation")
        binhash = {name: digest(path) for name, path in bins.items()}
        generated_c = {name: digest(path.with_suffix(".generated.c")) for name, path in bins.items()
                       if name.endswith("_stream") or name.endswith("_roundtrip")}
        if (digest(dso) != backend["sha256"] or digest(bun) != bun_sha_start or
            digest(python_path) != python_sha_start or digest(compiler_path) != compiler_sha_start or
            digest(Path(go)) != go_sha_start or digest(runner_path) != runner_sha_start or
            digest(plan_path) != plan_sha_start or binding_info(curl_python, env=runtime_env) != cffi_info):
            raise RuntimeError("a frozen runner, plan, DSO, toolchain or installed binding changed during build")
        if (subprocess.check_output([str(bun), "--version"], text=True).strip() != bun_version or
            subprocess.check_output([a.cc, "--version"], text=True) != compiler or
            subprocess.check_output([go, "version"], text=True).strip() != go_version):
            raise RuntimeError("a frozen toolchain version changed during build")
        if git(bend, "rev-parse", "HEAD") != BEND or git(bend, "status", "--porcelain"):
            raise RuntimeError("Bend compiler checkout changed during build")
        if git(cffi, "rev-parse", "HEAD") != CFFI or git(cffi, "status", "--porcelain"):
            raise RuntimeError("curl_cffi checkout changed during build")
        environment = {
            "platform": platform.platform(), "python": sys.version,
            "python_executable": str(python_path), "python_sha256": python_sha_start,
            "cpu": cpu, "go": go_version, "go_path": go,
            "go_sha256": go_sha_start, "inherited_runtime_paths": {
                "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                "BEND_SOURCE": str(bend),
                "BUN": str(bun),
                "CC": a.cc,
                "SCRAPANIUM_CURL_DIR": str(prefix),
                "LD_LIBRARY_PATH": runtime_env["LD_LIBRARY_PATH"],
                "LD_LIBRARY_PATH_inherited": os.environ.get("LD_LIBRARY_PATH", ""),
            }, "compiler_path": str(compiler_path),
            "compiler_sha256": compiler_sha_start,
        }
        manifest = {
            "schema": 1, "created_utc": now(), "mode": "correctness_smoke_only" if a.smoke else "performance_comparison",
            "baseline": baseline_snap, "candidate": candidate_snap,
            "environment": environment,
            "bend": {"path": str(bend), "commit": BEND, "bun": str(bun), "bun_version": bun_version,
                     "bun_sha256": bun_sha_start, "cc": a.cc, "cc_version": compiler},
            "curl_cffi": {"path": str(cffi), "commit": CFFI, "version": CFFI_VERSION,
                          "installed_binding": cffi_info},
            "backend": backend, "binary_sha256": binhash, "generated_c_sha256": generated_c,
            "build_artifacts": [{key: item[key] for key in ("name", "command", "cwd", "returncode", "log_path", "log_sha256")}
                                for item in build_records],
            "runner_sha256": runner_sha_start,
            "plan_sha256": plan_sha_start,
            "runs_per_workload_variant": a.runs, "seed": a.seed,
            "workloads": {"stream": stream_matrix, "roundtrip": roundtrip_matrix},
            "smoke_reduced_counts": {"stream_each": 32, "roundtrip_each": 20} if a.smoke else None,
            "randomization": ("smoke uses a seeded order with reduced correctness-only counts" if a.smoke else
                "for each workload, the six baseline/candidate/curl_cffi execution permutations are each used once per six repeats in seeded order; stream workload order uses cyclic rotations to balance positions"),
            "stream_timing": None if a.smoke else "warm verified WSS connection; timed client sends start signal then consumes/releases every full exact message in sequence; one connection; excludes startup, corpus creation, TLS, warmup and close",
            "curl_cffi_kind_check": "CurlWsFlag.BINARY bit in aggregate kind flags; complete payload equality and ordered sequence are checked",
            "backend_rule": "every client PID must map this exact DSO path and SHA-256",
            "tls_rule": "Go peers require TLS version 772 and cipher 4865; the Python round-trip peer records negotiated version/cipher per connection and requires one stable identity across baseline/candidate/curl_cffi",
            "targets": None if a.smoke else {
                "large_stream": "candidate/curl_cffi paired throughput-ratio 95% CI lower bound >= 2.0 for both 64KiB workloads",
                "non_regression": "candidate/baseline paired throughput-ratio 95% CI lower bound >= 0.95 for all six stream workloads and all seven round-trip workloads",
            },
            "limitations": ["loopback WSL/Linux only; no WAN inference",
                            "round-trip rows are a regression check, not a broad WebSocket superiority claim",
                            "all original workload counts, checkers and generated corpora are preserved",
                            "a failed checker, peer observation or DSO check aborts the run; errors remain in attempts.jsonl"],
            "output_directory": str(out),
            "smoke_rule": "smoke mode records reduced-count correctness only and omits all timing fields" if a.smoke else None,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        update_run_status(out, status="built", builds_started=True,
                          builds_finished_utc=now(), attempts_started=False,
                          successful_build_count=len(build_records))
        attempts_path = out / "attempts.jsonl"
        attempts = []
        attempts_started = False
        shared_env = runtime_env
        rng = random.Random(a.seed)
        stream_orders = balanced_orders(a.seed + 1, a.runs, [w["name"] for w in stream_matrix])
        roundtrip_orders = balanced_orders(a.seed + 2, a.runs, [w["name"] for w in roundtrip_matrix])

        def attempt(phase, workload, repeat, variant, position, run_id, action):
            nonlocal attempts_started
            if not attempts_started:
                update_run_status(out, status="running", attempts_started=True,
                                  first_attempt_started_utc=now())
                attempts_started = True
            return record_attempt(attempts_path, attempts, phase, workload, repeat,
                                  variant, position, run_id, action, a.smoke)

        def client_label(variant, helper):
            return helper.CLIENTS[1] if variant == "curl_cffi" else helper.CLIENTS[0]

        def client_binary(variant, kind):
            return bins[("candidate" if variant == "curl_cffi" else variant) + "_" + kind]

        def check_dso(result, roundtrip=False):
            maps = result.get("mapped_backends", []) if roundtrip else [result.get("mapped_backend", {})]
            if not maps or any(m.get("sha256") != backend["sha256"] or m.get("path") != backend["path"] for m in maps):
                raise RuntimeError(f"client did not map the same pinned DSO: {maps}")

        def shutdown_peer(process):
            if process is None:
                return
            try:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
            except (OSError, ValueError):
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        with tempfile.TemporaryDirectory(prefix="scrapanium-wss-comparison-") as tmp:
            context, ca = rt.certificate(tmp)
            body_path = Path(tmp) / "body.bin"
            body_path.write_bytes(bytes((i * 31) % 128 for i in range(22)))
            stream_server = subprocess.Popen([str(bins["stream_peer"]), "-cert", ca,
                "-key", str(Path(tmp) / "key.pem")], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stream_url = rt.line(stream_server)

                def stream_observation(run_id, expected_frames=None):
                    until = time.monotonic() + 2
                    last_observation = None
                    while True:
                        stream_server.stdin.write("stats\n")
                        stream_server.stdin.flush()
                        records = json.loads(rt.line(stream_server))
                        if run_id in records:
                            last_observation = records[run_id]
                            if (last_observation.get("error") or expected_frames is None or
                                last_observation.get("data_frames") == expected_frames):
                                return last_observation
                        if time.monotonic() >= until:
                            if last_observation is not None:
                                return last_observation
                            raise RuntimeError(f"stream peer record missing for {run_id}")
                        time.sleep(.01)

                negative = {"bytes": 30, "count": 32, "peer_flush_frames": 64, "connections": 1}
                for fault in ("corrupt", "swap"):
                    for pos, variant in enumerate(VARIANTS):
                        run_id = f"negative-{fault}-{variant}"

                        def run_control(v=variant, f=fault, rid=run_id):
                            result = stream.sample(client_label(v, stream), client_binary(v, "stream"),
                                stream_url, ca, body_path, negative, rid, shared_env,
                                backend["sha256"], 1, f)

                            def validate():
                                observed = stream_observation(rid)
                                result["peer_observation"] = observed
                                if not result.get("detected_by_exact_check"):
                                    raise RuntimeError(f"{v} failed negative {f} control")
                                check_dso(result)
                                check_peer_tls(observed)

                            return validate_with_result(result, validate)

                        attempt("stream-control", {"name": f"negative-{fault}", **negative},
                                0, variant, pos, run_id, run_control)

                stream_order = [w["name"] for w in stream_matrix]
                rng.shuffle(stream_order)
                stream_by_name = {w["name"]: w for w in stream_matrix}
                for repeat in range(a.runs):
                    shift = repeat % len(stream_order)
                    for name in stream_order[shift:] + stream_order[:shift]:
                        workload = stream_by_name[name]
                        body_path.write_bytes(bytes((i * 31) % 128 for i in range(workload["bytes"] - 8)))
                        for pos, variant in enumerate(stream_orders[name][repeat]):
                            run_id = f"{name}-r{repeat:03d}-{variant}"

                            def run_stream(v=variant, w=workload, rid=run_id):
                                result = stream.sample(client_label(v, stream), client_binary(v, "stream"),
                                    stream_url, ca, body_path, w, rid, shared_env,
                                    backend["sha256"], 1)

                                def validate():
                                    observed = stream_observation(rid, w["count"])
                                    result["peer_observation"] = observed
                                    check_dso(result)
                                    check_peer_tls(observed)
                                    head = 2 if w["bytes"] < 126 else 4 if w["bytes"] < 65536 else 10
                                    if (observed.get("error") or observed["warmups"] != 1 or
                                        observed["starts"] != 1 or observed["data_frames"] != w["count"] or
                                        observed["data_payload_bytes"] != w["count"] * w["bytes"] or
                                        observed["data_frame_bytes"] != w["count"] * (w["bytes"] + head)):
                                        raise RuntimeError(f"stream peer check failed for {rid}: {observed}")

                                return validate_with_result(result, validate)

                            attempt("stream", workload, repeat, variant, pos, run_id, run_stream)
            finally:
                shutdown_peer(stream_server)

            python_peer, python_url = rt.start_ws(context)
            python_tls_observations = []
            python_tls_identity = None
            original_finish_request = python_peer.finish_request

            def capture_python_peer_tls(request, client_address):
                cipher = request.cipher()
                python_tls_observations.append({
                    "connection_index": len(python_tls_observations),
                    "tls_version": request.version(),
                    "tls_cipher": cipher[0],
                    "tls_cipher_protocol": cipher[1],
                    "tls_cipher_bits": cipher[2],
                })
                return original_finish_request(request, client_address)

            python_peer.finish_request = capture_python_peer_tls
            roundtrip_server = None
            try:
                roundtrip_server = subprocess.Popen([str(bins["roundtrip_peer"]), "-cert", ca,
                    "-key", str(Path(tmp) / "key.pem")], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except Exception:
                python_peer.shutdown()
                python_peer.server_close()
                raise
            try:
                go_url = rt.line(roundtrip_server)
                roundtrip_by_name = {w["name"]: w for w in roundtrip_matrix}
                roundtrip_order = list(roundtrip_by_name)
                rng.shuffle(roundtrip_order)
                for repeat in range(a.runs):
                    shift = repeat % len(roundtrip_order)
                    for name in roundtrip_order[shift:] + roundtrip_order[:shift]:
                        workload = roundtrip_by_name[name]
                        payload = rt.ORIGINAL if workload["bytes"] == len(rt.ORIGINAL) else bytes(
                            (i * 131 + 17) % 256 for i in range(workload["bytes"]))
                        payload_path = Path(tmp) / "roundtrip.bin"
                        payload_path.write_bytes(payload)
                        url = go_url if workload["peer"] == "go" else python_url
                        for pos, variant in enumerate(roundtrip_orders[name][repeat]):
                            run_id = f"{name}-r{repeat:03d}-{variant}"
                            python_frame_start = len(python_peer.frames)
                            python_tls_start = len(python_tls_observations)

                            def run_roundtrip(v=variant, w=workload, rid=run_id, u=url, p=payload_path,
                                              frame_start=python_frame_start, tls_start=python_tls_start):
                                result = rt.sample(client_label(v, rt), client_binary(v, "roundtrip"),
                                    u, ca, p, w, rid, shared_env)

                                def validate():
                                    nonlocal python_tls_identity
                                    if w["peer"] == "python":
                                        frames = python_peer.frames[frame_start:]
                                        tls_records = python_tls_observations[tls_start:]
                                        observed = {
                                            "frames": len(frames),
                                            "payload_bytes": len(frames) * len(payload),
                                            "opcode_fin_payload_match": all(frame == (2, payload, True) for frame in frames),
                                            "tls_connections": tls_records,
                                        }
                                        result["peer_observation"] = observed
                                        if (len(frames) != (w["count"] + 1) * w["connections"] or
                                            not observed["opcode_fin_payload_match"]):
                                            raise RuntimeError(f"round-trip Python peer frame check failed: {rid}")
                                        if len(tls_records) != w["connections"]:
                                            raise RuntimeError(f"Python peer TLS observation count mismatch: {rid} {tls_records}")
                                        identities = {(x["tls_version"], x["tls_cipher"],
                                                       x["tls_cipher_protocol"], x["tls_cipher_bits"])
                                                      for x in tls_records}
                                        if len(identities) != 1:
                                            raise RuntimeError(f"Python peer TLS identity differed within {rid}: {tls_records}")
                                        identity = next(iter(identities))
                                        if python_tls_identity is None:
                                            python_tls_identity = identity
                                        elif identity != python_tls_identity:
                                            raise RuntimeError(
                                                f"Python peer TLS identity changed across variants: "
                                                f"{identity} != {python_tls_identity}")
                                    else:
                                        roundtrip_server.stdin.write("stats\n")
                                        roundtrip_server.stdin.flush()
                                        records = json.loads(rt.line(roundtrip_server))
                                        observed = [records[f"{rid}-{i}"] for i in range(w["connections"])]
                                        result["peer_observation"] = observed
                                        if any(x.get("error") or x["frames"] != w["count"] + 1 or
                                               x["payload_bytes"] != (w["count"] + 1) * w["bytes"]
                                               for x in observed):
                                            raise RuntimeError(f"round-trip Go peer check failed: {rid} {observed}")
                                        for peer_record in observed:
                                            check_peer_tls(peer_record)
                                    check_dso(result, True)

                                return validate_with_result(result, validate)

                            attempt("roundtrip", workload, repeat, variant, pos, run_id, run_roundtrip)
            finally:
                python_peer.shutdown()
                python_peer.server_close()
                shutdown_peer(roundtrip_server)

        # Refuse a result if build inputs or any binary changed after the frozen manifest.
        for root, snap in ((base, baseline_snap), (cand, candidate_snap)):
            if sources(root) != snap["source_sha256"]:
                raise RuntimeError(f"benchmark source changed during run: {root}")
        if any(digest(bins[name]) != expected for name, expected in binhash.items()):
            raise RuntimeError("binary hash verification failed")
        if any(digest(path.with_suffix(".generated.c")) != expected
               for name, expected in generated_c.items() for path in (bins[name],)):
            raise RuntimeError("generated compiler input changed during run")
        if digest(dso) != backend["sha256"]:
            raise RuntimeError("stock DSO changed during run")
        if (digest(compiler_path) != manifest["environment"]["compiler_sha256"] or
            subprocess.check_output([a.cc, "--version"], text=True) != compiler):
            raise RuntimeError("C compiler changed during run")
        if digest(python_path) != manifest["environment"]["python_sha256"]:
            raise RuntimeError("Python executable changed during run")
        if (digest(Path(go)) != manifest["environment"]["go_sha256"] or
            subprocess.check_output([go, "version"], text=True).strip() != go_version):
            raise RuntimeError("Go executable changed during run")
        if digest(bun) != manifest["bend"]["bun_sha256"] or subprocess.check_output(
                [str(bun), "--version"], text=True).strip() != bun_version:
            raise RuntimeError("Bun changed during run")
        if binding_info(curl_python, env=runtime_env) != cffi_info:
            raise RuntimeError("installed curl_cffi binding module or wrapper changed during run")
        if git(bend, "rev-parse", "HEAD") != BEND or git(cffi, "rev-parse", "HEAD") != CFFI:
            raise RuntimeError("compiler or curl_cffi source commit changed during run")
        if git(bend, "status", "--porcelain") or git(cffi, "status", "--porcelain"):
            raise RuntimeError("compiler or curl_cffi checkout changed during run")
        if digest(Path(__file__)) != manifest["runner_sha256"] or digest(Path(__file__).with_name("README.md")) != manifest["plan_sha256"]:
            raise RuntimeError("comparison harness or plan changed during run")

        def summarize(phase, matrix, seed):
            all_results = []
            for w in matrix:
                grouped = {v: sorted((r for r in attempts if r["phase"] == phase and
                    r["status"] == "ok" and r["workload"] == w["name"] and r["variant"] == v),
                    key=lambda r: r["repeat"]) for v in VARIANTS}
                values = {}
                for v, rows in grouped.items():
                    if len(rows) != a.runs:
                        raise RuntimeError(f"missing rows for {phase} {w['name']} {v}")
                    times = [r["result"]["elapsed_ms"] for r in rows]
                    count = w["count"] * w.get("connections", 1)
                    rates = [count * 1000.0 / t for t in times]
                    values[v] = {"elapsed_ms": times, "median_ms": statistics.median(times),
                                 "rate_per_second": statistics.median(rates),
                                 "rate_median_bootstrap_95_ci": interval(rates, seed + len(all_results))}
                ratios = {}
                for n, d in (("candidate", "baseline"), ("baseline", "curl_cffi"),
                             ("candidate", "curl_cffi")):
                    nr = {r["repeat"]: r["result"]["elapsed_ms"] for r in grouped[n]}
                    dr = {r["repeat"]: r["result"]["elapsed_ms"] for r in grouped[d]}
                    vals = [dr[i] / nr[i] for i in sorted(nr)]
                    ratios[f"{n}_vs_{d}"] = {"samples": vals, "median": statistics.median(vals),
                        "bootstrap_95_ci": interval(vals, seed + len(all_results) + len(ratios))}
                all_results.append({"workload": w, "variants": values, "paired_ratios": ratios})
            return all_results

        if a.smoke:
            expected_attempts = 6 + (len(stream_matrix) + len(roundtrip_matrix)) * a.runs * len(VARIANTS)
            completed = sum(row["status"] == "ok" for row in attempts)
            if len(attempts) != expected_attempts or completed != expected_attempts:
                raise RuntimeError(f"correctness smoke incomplete: {completed}/{expected_attempts} attempts passed")
            report = {
                "schema": 1, "status": "correctness_smoke_complete", "smoke_only": True,
                "performance_claims": False, "acceptance_claims": False,
                "reduced_counts": {"stream_each": 32, "roundtrip_each": 20},
                "runs_per_variant": a.runs, "successful_attempts": completed,
                "stream_workloads": [w["name"] for w in stream_matrix],
                "roundtrip_workloads": [w["name"] for w in roundtrip_matrix],
                "all_correctness_checks_passed": True,
            }
        else:
            stream_summary = summarize("stream", stream_matrix, a.seed)
            report = {"schema": 1, "status": "complete", "stream": stream_summary,
                "targets_met": {x["workload"]["name"]:
                    x["paired_ratios"]["candidate_vs_curl_cffi"]["bootstrap_95_ci"][0] >= 2.0
                    for x in stream_summary if x["workload"]["bytes"] == 65536},
                "stream_non_regression": {x["workload"]["name"]:
                    x["paired_ratios"]["candidate_vs_baseline"]["bootstrap_95_ci"][0] >= 0.95
                    for x in stream_summary}}
            report["roundtrip"] = summarize("roundtrip", roundtrip_matrix, a.seed + 1)
            report["roundtrip_non_regression"] = {x["workload"]["name"]:
                x["paired_ratios"]["candidate_vs_baseline"]["bootstrap_95_ci"][0] >= 0.95
                for x in report["roundtrip"]}
        (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        update_run_status(out, status="complete", finished_utc=now(),
                          successful_attempts=(expected_attempts if a.smoke else len(attempts)))
        print(out)
        return 0
    except Exception as exc:
        record_run_failure(out, exc)
        raise


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as exc:
        if _ACTIVE_RUN_OUT is not None:
            record_run_failure(_ACTIVE_RUN_OUT, exc)
        raise
    raise SystemExit(exit_code)
