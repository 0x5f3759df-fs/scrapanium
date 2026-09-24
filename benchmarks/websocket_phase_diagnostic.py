"""Reproducible phase and observer-effect diagnostic for 64 KiB WSS frames.

Performance mode runs 12 balanced repeats of four conditions for each server
flush size: Bend/curl_cffi crossed with control/phase instrumentation. Controls
skip all per-message diagnostic clock calls; all clients still use the same
total timer, exact checks, expected buffers, and releases. `--smoke` is a short
correctness run and must never be used as performance evidence.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
from build import build, compiler
from lab import certificate

BEND_CLIENT = ROOT / "benchmarks/websocket_phase_diagnostic.bend"
PYTHON_CLIENT = ROOT / "benchmarks/websocket_phase_diagnostic_python.py"
CLOCK_SOURCE = ROOT / "benchmarks/websocket_phase_clock.c"
PEER_SOURCE = ROOT / "benchmarks/websocket_phase_server.go"
MATCHED_PYTHON = ROOT / ".deps/venv-matched/bin/python"
SIZE = 65536
PERFORMANCE_COUNT = 1024
SMOKE_COUNT = 8
REPEATS = 12
CLIENTS = ("scrapanium-bend", "curl_cffi-matched")
MODES = ("control", "phase")
CONDITIONS = (
    (CLIENTS[0], MODES[0]),
    (CLIENTS[0], MODES[1]),
    (CLIENTS[1], MODES[0]),
    (CLIENTS[1], MODES[1]),
)
WILLIAMS = (
    (0, 1, 3, 2),
    (1, 2, 0, 3),
    (2, 3, 1, 0),
    (3, 0, 2, 1),
)
METRIC_KEYS = (
    "total_elapsed_ns", "receive_assembly_ns", "validation_release_ns",
    "start_monotonic_us", "end_monotonic_us",
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command_output(command, env=None):
    return subprocess.check_output(command, env=env, text=True, stderr=subprocess.STDOUT).strip()


def line(process, timeout=30):
    if not select.select([process.stdout], [], [], timeout)[0]:
        raise RuntimeError(f"process {process.pid} did not become ready")
    value = process.stdout.readline().strip()
    if not value:
        error = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"process {process.pid} exited before readiness: {error}")
    return value


def mapped_backend(process):
    matches = {
        entry.split()[-1]
        for entry in Path(f"/proc/{process.pid}/maps").read_text().splitlines()
        if "libcurl-impersonate.so" in entry
    }
    if len(matches) != 1:
        raise RuntimeError(f"expected one mapped curl backend for PID {process.pid}: {matches}")
    path = Path(matches.pop()).resolve()
    return {"path": str(path), "sha256": digest(path)}


def source_paths():
    paths = [
        ROOT / "scrapanium.bend", ROOT / "http.bend", ROOT / "dependencies.json",
        ROOT / "scripts/build.py", ROOT / "tests/lab.py",
        ROOT / "benchmarks/websocket.bend", ROOT / "benchmarks/websocket.py",
        ROOT / "benchmarks/websocket_phase_diagnostic.py",
        BEND_CLIENT, PYTHON_CLIENT, CLOCK_SOURCE, PEER_SOURCE,
        *sorted((ROOT / "native").glob("*")),
    ]
    return [path for path in paths if path.is_file()]


def hash_sources():
    return {str(path.relative_to(ROOT)): digest(path) for path in source_paths()}


def git_revision(path):
    try:
        return command_output(["git", "-C", str(path), "rev-parse", "HEAD"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def compiler_provenance():
    command = compiler()
    compiler_script = Path(command[1]).resolve()
    compiler_root = compiler_script.parent.parent
    bun_version = command_output([command[0], "--version"])
    dependencies = json.loads((ROOT / "dependencies.json").read_text())
    bend_revision = git_revision(compiler_root)
    expected_revision = dependencies["bend"]["commit"]
    if bend_revision != expected_revision:
        raise RuntimeError(f"Bend compiler revision {bend_revision!r} does not match pin {expected_revision}")
    tracked_status = command_output([
        "git", "-C", str(compiler_root), "status", "--porcelain", "--untracked-files=no",
    ])
    if tracked_status:
        raise RuntimeError("Bend compiler checkout has tracked modifications")
    bun_path = Path(command[0]).resolve()
    if not bun_path.is_file():
        found = shutil.which(command[0])
        if not found:
            raise FileNotFoundError(f"Bun executable does not exist: {command[0]}")
        bun_path = Path(found).resolve()
    if bun_version != dependencies["bun"]:
        raise RuntimeError(f"Bun version {bun_version!r} does not match pin {dependencies['bun']}")
    return {
        "command": command,
        "bun_version": bun_version,
        "bun_path": str(bun_path),
        "bun_sha256": digest(bun_path),
        "compiler_script": str(compiler_script),
        "compiler_script_sha256": digest(compiler_script),
        "compiler_git_revision": bend_revision,
        "compiler_tracked_status": "clean",
    }


def curl_binding_provenance(env):
    code = (
        "import curl_cffi,hashlib,json,pathlib,sys; "
        "p=pathlib.Path(curl_cffi.__file__).parent; "
        "files=[p/'__init__.py',p/'curl.py',p/'requests/websockets.py',*p.glob('_wrapper*.so')]; "
        "print(json.dumps({'version':curl_cffi.__version__,'python':sys.version,"
        "'python_executable':str(pathlib.Path(sys.executable).resolve()),"
        "'python_executable_sha256':hashlib.sha256(pathlib.Path(sys.executable).resolve().read_bytes()).hexdigest(),"
        "'files':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}))"
    )
    return json.loads(command_output([str(MATCHED_PYTHON), "-c", code], env=env))


def cpu_model():
    try:
        return next((row.split(":", 1)[1].strip() for row in
                     Path("/proc/cpuinfo").read_text().splitlines()
                     if row.startswith("model name")), "unknown")
    except OSError:
        return "unknown"


def workload_schedule(seed, runs):
    rng = random.Random(seed)
    order = []
    if runs == 1:
        return [list(WILLIAMS[0])]
    for block in range(math.ceil(runs / len(WILLIAMS))):
        rows = list(range(len(WILLIAMS)))
        rng.shuffle(rows)
        for row in rows:
            if len(order) >= runs:
                break
            order.append(list(WILLIAMS[row]))
    return order


def validate_schedule(schedule, runs):
    if len(schedule) != runs:
        raise RuntimeError(f"schedule has {len(schedule)} repeats, expected {runs}")
    for row in schedule:
        if len(row) != 4 or sorted(row) != [0, 1, 2, 3]:
            raise RuntimeError(f"schedule row is not a permutation of four conditions: {row}")
    if runs == 12:
        for block_start in (0, 4, 8):
            block = schedule[block_start:block_start + 4]
            positions = [[0] * 4 for _ in range(4)]
            for row in block:
                for position, condition in enumerate(row):
                    positions[condition][position] += 1
            if any(count != 1 for row in positions for count in row):
                raise RuntimeError("four-repeat Williams block is not position-balanced")


def parse_metrics(output):
    found = {}
    for raw in output.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        if key in METRIC_KEYS:
            if key in found:
                raise RuntimeError(f"client emitted duplicate diagnostic field {key}")
            try:
                found[key] = int(value)
            except ValueError as error:
                raise RuntimeError(f"client emitted a non-integer {key}: {value!r}") from error
    missing = [key for key in METRIC_KEYS if key not in found]
    if missing:
        raise RuntimeError(f"client omitted diagnostic fields: {missing}; output={output!r}")
    if found["total_elapsed_ns"] <= 0:
        raise RuntimeError("client total interval must be positive")
    return found


def sanitizer_findings(*outputs):
    text = "\n".join(outputs)
    markers = (
        "AddressSanitizer:", "LeakSanitizer:", "UndefinedBehaviorSanitizer:",
        "runtime error:",
    )
    return [marker for marker in markers if marker in text]


def peer_stats(server, run_id):
    deadline = time.monotonic() + 20
    while True:
        server.stdin.write("stats\n")
        server.stdin.flush()
        records = json.loads(line(server, timeout=20))
        if run_id not in records:
            raise RuntimeError(f"phase peer has no record for {run_id}")
        record = records[run_id]
        if record.get("completed") is True or record.get("error"):
            return record
        if time.monotonic() >= deadline:
            raise RuntimeError(f"phase peer record did not finalize for {run_id}")
        time.sleep(0.01)


def run_client(client, mode, binary, url, ca, body_path, count, env, timeout=120):
    command = ([str(binary), "--threads", "1"] if client == CLIENTS[0] else
               [str(MATCHED_PYTHON), str(PYTHON_CLIENT)])
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
        env={**env,
             "SCRAPANIUM_BENCH_URL": url,
             "SCRAPANIUM_BENCH_CA": ca,
             "SCRAPANIUM_BENCH_COUNT": str(count),
             "SCRAPANIUM_BENCH_BODY": str(body_path),
             "SCRAPANIUM_BENCH_PHASE_MODE": mode},
    )
    try:
        ready = line(process, timeout=45)
        if ready != "ready":
            raise RuntimeError(f"invalid client readiness line: {ready!r}")
        backend = mapped_backend(process)
        parent_gate_ns = time.monotonic_ns()
        process.stdin.write("x")
        process.stdin.flush()
        output, errors = process.communicate(timeout=timeout)
        parent_done_ns = time.monotonic_ns()
        return {
            "returncode": process.returncode,
            "stdout": output,
            "stderr": errors,
            "mapped_backend": backend,
            "parent_gate_monotonic_ns": parent_gate_ns,
            "parent_done_monotonic_ns": parent_done_ns,
        }
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def validate_peer_positive(peer, count, batch):
    expected = {
        "data_frames": count,
        "data_payload_bytes": count * SIZE,
        "data_frame_bytes": count * (SIZE + 10),
        "data_plaintext_bytes_written": count * (SIZE + 10),
        "send_write_calls": math.ceil(count / batch),
        "warmups": 1,
        "starts": 1,
    }
    errors = []
    if peer.get("completed") is not True:
        errors.append("peer record is not completed")
    if peer.get("error"):
        errors.append(f"peer error: {peer['error']}")
    for key, value in expected.items():
        if type(peer.get(key)) is not int or peer[key] != value:
            errors.append(f"peer {key} was {peer.get(key)!r}, expected {value}")
    timing_fields = (
        "send_interval_wall_ns", "tls_write_wall_ns_sum",
        "peer_process_cpu_user_ns", "peer_process_cpu_system_ns",
    )
    for key in timing_fields:
        if type(peer.get(key)) is not int or peer[key] < 0:
            errors.append(f"peer {key} is not a nonnegative integer")
    if type(peer.get("send_interval_wall_ns")) is int and type(peer.get("tls_write_wall_ns_sum")) is int:
        if peer["tls_write_wall_ns_sum"] > peer["send_interval_wall_ns"]:
            errors.append("sum of TLS Write wall intervals exceeds peer send interval")
    for key in ("tls_version", "tls_cipher"):
        if type(peer.get(key)) is not int or peer[key] <= 0:
            errors.append(f"peer {key} is missing or invalid")
    return errors


def validate_client_positive(client_result, metrics, expected_backend_sha, mode):
    errors = []
    for marker in sanitizer_findings(client_result["stdout"], client_result["stderr"]):
        errors.append(f"sanitizer marker in client output: {marker}")
    if client_result["returncode"] != 0:
        errors.append(f"client exited {client_result['returncode']}: {client_result['stderr']}")
    backend = client_result["mapped_backend"]
    if backend["sha256"] != expected_backend_sha:
        errors.append("client loaded a backend different from the frozen backend hash")
    gate_us = client_result["parent_gate_monotonic_ns"] // 1000
    done_us = client_result["parent_done_monotonic_ns"] // 1000
    start_us = metrics["start_monotonic_us"]
    end_us = metrics["end_monotonic_us"]
    if not (gate_us <= start_us <= end_us <= done_us):
        errors.append("client absolute monotonic boundaries fall outside parent bounds")
    boundary_duration_ns = (end_us - start_us) * 1000
    if abs(metrics["total_elapsed_ns"] - boundary_duration_ns) > 1000:
        errors.append("total nanoseconds disagree with absolute microsecond boundaries by more than 1 us")
    if metrics["receive_assembly_ns"] < 0 or metrics["validation_release_ns"] < 0:
        errors.append("phase duration is negative")
    if metrics["receive_assembly_ns"] + metrics["validation_release_ns"] > metrics["total_elapsed_ns"]:
        errors.append("phase sums exceed total client interval")
    if mode == "control" and (metrics["receive_assembly_ns"] or metrics["validation_release_ns"]):
        errors.append("control mode unexpectedly recorded per-message phase timing")
    if mode == "phase" and (not metrics["receive_assembly_ns"] or not metrics["validation_release_ns"]):
        errors.append("phase mode did not record both client phases")
    return errors


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


class Artifact:
    def __init__(self, output_dir, manifest):
        self.output_dir = output_dir
        if output_dir.exists():
            raise FileExistsError(f"refusing to overwrite existing artifact directory: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=False)
        self.manifest = manifest
        self.manifest_path = output_dir / "manifest.json"
        self.samples_path = output_dir / "samples.jsonl"
        self.samples = self.samples_path.open("x", encoding="utf-8")
        self.manifest["sample_count"] = 0
        self.write_manifest()

    def write_manifest(self):
        write_json(self.manifest_path, self.manifest)

    def append(self, sample):
        self.samples.write(json.dumps(sample, sort_keys=True) + "\n")
        self.samples.flush()
        self.manifest["sample_count"] += 1
        self.write_manifest()

    def close(self):
        if not self.samples.closed:
            self.samples.close()


def build_artifacts(args, build_dir):
    if build_dir.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic build directory: {build_dir}")
    build_dir.mkdir(parents=True, exist_ok=False)
    bend_binary = build_dir / ("bench-ws-phase-diagnostic-asan" if args.sanitize else "bench-ws-phase-diagnostic")
    peer_binary = build_dir / "bench-ws-phase-server"
    build(BEND_CLIENT, bend_binary, sanitize=args.sanitize)
    subprocess.run(["go", "build", "-trimpath", "-o", str(peer_binary), str(PEER_SOURCE)], check=True)
    return bend_binary, peer_binary


def make_env(prefix):
    library = prefix / "lib" if (prefix / "lib/libcurl-impersonate.so").is_file() else prefix
    backend = library / "libcurl-impersonate.so"
    if not backend.is_file():
        raise FileNotFoundError(f"no libcurl-impersonate.so under configured prefix {prefix}")
    env = dict(os.environ)
    old_library_path = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = str(library) + (":" + old_library_path if old_library_path else "")
    env["SCRAPANIUM_CURL_DIR"] = str(prefix)
    return env, library.resolve(), backend.resolve()


def start_peer(binary, ca, key):
    process = subprocess.Popen(
        [str(binary), "-cert", str(ca), "-key", str(key)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        url = line(process, timeout=20)
        if not url.startswith("wss://127.0.0.1:"):
            raise RuntimeError(f"phase peer announced unexpected URL: {url!r}")
        return process, url
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.communicate()
        raise


def condition_name(condition):
    client, mode = condition
    return f"{client}/{mode}"


def run_positive(artifact, peer, peer_url, binary, env, ca, body_path,
                 backend_sha, batch, count, repeat, condition, run_id):
    client, mode = condition
    query = urlencode({"id": run_id, "count": count, "size": SIZE, "batch": batch})
    url = f"{peer_url}/stream?{query}"
    sample = {
        "kind": "performance_sample" if count == PERFORMANCE_COUNT else "correctness_smoke",
        "run_id": run_id,
        "workload": f"64KiB-flush{batch}",
        "batch": batch,
        "count": count,
        "message_bytes": SIZE,
        "repeat": repeat,
        "condition": condition_name(condition),
        "client": client,
        "mode": mode,
    }
    written = False
    try:
        client_result = run_client(client, mode, binary, url, ca, body_path, count, env)
        sample["client_result"] = {key: value for key, value in client_result.items()
                                   if key not in ("stdout", "stderr")}
        sample["client_stdout"] = client_result["stdout"]
        sample["client_stderr"] = client_result["stderr"]
        sample["peer"] = peer_stats(peer, run_id)
        sample["metrics"] = parse_metrics(client_result["stdout"])
        errors = validate_client_positive(client_result, sample["metrics"], backend_sha, mode)
        errors.extend(validate_peer_positive(sample["peer"], count, batch))
        sample["validation_errors"] = errors
        metrics = sample["metrics"]
        sample["residual_ns"] = (metrics["total_elapsed_ns"] -
            metrics["receive_assembly_ns"] - metrics["validation_release_ns"])
        artifact.append(sample)
        written = True
        if errors:
            raise RuntimeError(f"invalid diagnostic sample {run_id}: {'; '.join(errors)}")
        return sample
    except BaseException as error:
        if not written:
            sample["failure"] = f"{type(error).__name__}: {error}"
            artifact.append(sample)
        raise


def run_negative(artifact, peer, peer_url, binary, env, ca, body_path,
                 backend_sha, batch, count, client, mode, fault, run_id):
    query = urlencode({"id": run_id, "count": count, "size": SIZE,
                       "batch": batch, "fault": fault})
    sample = {
        "kind": "negative_correctness_control",
        "run_id": run_id,
        "workload": f"64KiB-flush{batch}",
        "batch": batch,
        "count": count,
        "message_bytes": SIZE,
        "client": client,
        "mode": mode,
        "fault": fault,
    }
    written = False
    try:
        client_result = run_client(client, mode, binary, f"{peer_url}/stream?{query}",
                                   ca, body_path, count, env, timeout=120)
        sample["client_result"] = {key: value for key, value in client_result.items()
                                   if key not in ("stdout", "stderr")}
        sample["client_stdout"] = client_result["stdout"]
        sample["client_stderr"] = client_result["stderr"]
        sample["peer"] = peer_stats(peer, run_id)
        output = client_result["stdout"] + client_result["stderr"]
        errors = []
        for marker in sanitizer_findings(client_result["stdout"], client_result["stderr"]):
            errors.append(f"sanitizer marker in negative client output: {marker}")
        if client_result["returncode"] == 0:
            errors.append("client accepted a deliberately invalid message sequence")
        if "opcode, sequence, or payload mismatch" not in output:
            errors.append("client failed without reporting the expected content/opcode mismatch")
        if client_result["mapped_backend"]["sha256"] != backend_sha:
            errors.append("negative client loaded a backend different from the frozen backend hash")
        record = sample["peer"]
        for key in ("data_frames", "data_payload_bytes", "data_frame_bytes",
                    "data_plaintext_bytes_written", "send_write_calls",
                    "send_interval_wall_ns", "tls_write_wall_ns_sum",
                    "peer_process_cpu_user_ns", "peer_process_cpu_system_ns"):
            if type(record.get(key)) is not int or record[key] < 0:
                errors.append(f"negative-control peer {key} is not a nonnegative integer")
        if record.get("starts") != 1 or record.get("warmups") != 1:
            errors.append("negative-control peer did not observe the common warmup/start")
        sample["validation_errors"] = errors
        artifact.append(sample)
        written = True
        if errors:
            raise RuntimeError(f"invalid negative control {run_id}: {'; '.join(errors)}")
    except BaseException as error:
        if not written:
            sample["failure"] = f"{type(error).__name__}: {error}"
            artifact.append(sample)
        raise


def paired_observer_effect(samples):
    paired = {}
    expected_groups = {
        (f"64KiB-flush{batch}", repeat, client, mode)
        for batch in (1, 64)
        for repeat in range(1, REPEATS + 1)
        for client in CLIENTS
        for mode in MODES
    }
    seen_groups = set()
    performance = [sample for sample in samples
                   if sample.get("kind") == "performance_sample"]
    if len(performance) != len(expected_groups):
        raise ValueError(f"expected {len(expected_groups)} performance rows, got {len(performance)}")
    for sample in samples:
        if sample.get("kind") != "performance_sample":
            continue
        group = (sample.get("workload"), sample.get("repeat"),
                 sample.get("client"), sample.get("mode"))
        if group not in expected_groups:
            raise ValueError(f"unexpected performance group {group!r}")
        if group in seen_groups:
            raise ValueError(f"duplicate performance group {group!r}")
        seen_groups.add(group)
        total = sample.get("metrics", {}).get("total_elapsed_ns")
        if type(total) is not int or total <= 0 or not math.isfinite(total):
            raise ValueError(f"performance group {group!r} has a nonpositive or nonfinite integer total")
        key = group[:3]
        paired.setdefault(key, {})[group[3]] = total
    if seen_groups != expected_groups:
        missing = sorted(expected_groups - seen_groups)
        raise ValueError(f"performance schedule is missing groups: {missing!r}")
    effects = {}
    for batch in (1, 64):
        workload = f"64KiB-flush{batch}"
        effects[workload] = {}
        for client in CLIENTS:
            ratios, deltas = [], []
            for repeat in range(1, REPEATS + 1):
                row = paired[(workload, repeat, client)]
                ratio = row["phase"] / row["control"]
                ratios.append(ratio)
                deltas.append(row["phase"] - row["control"])
            effects[workload][client] = {
                "phase_over_control_total_ratios": ratios,
                "median_phase_over_control_total_ratio": statistics.median(ratios),
                "paired_total_differences_ns": deltas,
                "median_paired_total_difference_ns": statistics.median(deltas),
                "interpretation": "effect of per-message clock calls within the diagnostic client skeleton; not a historical-client comparison",
            }
    return effects


def validate_retained_schedule(artifact, expected_positive_count):
    artifact.samples.flush()
    retained = [json.loads(row) for row in artifact.samples_path.read_text().splitlines() if row]
    planned_ids = [item["run_id"] for item in artifact.manifest["planned_schedule"]]
    execution_ids = artifact.manifest["execution_order"]
    retained_ids = [item.get("run_id") for item in retained]
    if len(set(planned_ids)) != len(planned_ids):
        raise RuntimeError("planned schedule has duplicate IDs")
    if execution_ids != planned_ids:
        raise RuntimeError("execution order does not match the retained full plan")
    if retained_ids != execution_ids:
        raise RuntimeError("retained samples are missing, duplicated, or out of planned order")
    for planned, record in zip(artifact.manifest["planned_schedule"], retained):
        for key, value in planned.items():
            if record.get(key) != value:
                raise RuntimeError(f"retained sample {planned['run_id']} has {key}={record.get(key)!r}, "
                                   f"expected {value!r}")
        expected_workload = f"64KiB-flush{planned['batch']}"
        if record.get("workload") != expected_workload or record.get("message_bytes") != SIZE:
            raise RuntimeError(f"retained sample {planned['run_id']} workload metadata disagrees with its plan")
        if planned["kind"] in ("performance_sample", "correctness_smoke"):
            expected_condition = condition_name((planned["client"], planned["mode"]))
            if record.get("condition") != expected_condition:
                raise RuntimeError(f"retained sample {planned['run_id']} condition disagrees with its plan")
    positive = [item for item in retained if item.get("kind") in ("performance_sample", "correctness_smoke")]
    negative = [item for item in retained if item.get("kind") == "negative_correctness_control"]
    if len(positive) != expected_positive_count or len(negative) != 16:
        raise RuntimeError(f"retained {len(positive)} positive and {len(negative)} negative records; "
                           f"expected {expected_positive_count} and 16")
    expected_negative = {
        (client, mode, fault, batch)
        for client in CLIENTS for mode in MODES for fault in ("corrupt", "swap") for batch in (1, 64)
    }
    actual_negative = {
        (item.get("client"), item.get("mode"), item.get("fault"), item.get("batch"))
        for item in negative
    }
    if actual_negative != expected_negative:
        raise RuntimeError("retained negative controls do not cover both clients/modes/batches/faults")
    if any(item.get("validation_errors") for item in retained):
        raise RuntimeError("one or more retained diagnostic records failed semantic validation")
    tls_by_batch = {batch: set() for batch in (1, 64)}
    for item in positive:
        peer = item.get("peer")
        identity = (peer.get("tls_version"), peer.get("tls_cipher")) if isinstance(peer, dict) else None
        if (not isinstance(identity, tuple) or
                any(type(value) is not int or value <= 0 for value in identity)):
            raise RuntimeError(f"retained sample {item.get('run_id')} has invalid TLS identity")
        tls_by_batch[item["batch"]].add(identity)
    for batch, identities in tls_by_batch.items():
        if len(identities) != 1:
            raise RuntimeError(f"TLS identity changed across conditions for flush batch {batch}")
    return retained


def parse_args():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / f"benchmarks/results/websocket-phase-diagnostic-{stamp}")
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument("--smoke", action="store_true",
                        help="run eight-message correctness cases; never treat timings as performance evidence")
    parser.add_argument("--sanitize", action="store_true",
                        help="build the Bend diagnostic with ASan/UBSan; requires --smoke")
    args = parser.parse_args()
    if args.sanitize and not args.smoke:
        parser.error("--sanitize is supported only with --smoke")
    for name in ("CC", "BEND_SOURCE", "BUN", "LD_PRELOAD"):
        if name in os.environ:
            parser.error(f"clear {name} to use the pinned diagnostic build and runtime")
    if not MATCHED_PYTHON.is_file():
        parser.error(f"matched Python environment is missing: {MATCHED_PYTHON}")
    return args


def main():
    args = parse_args()
    count = SMOKE_COUNT if args.smoke else PERFORMANCE_COUNT
    repeats = 1 if args.smoke else REPEATS
    artifact = Artifact(args.output_dir, {
        "schema": 1,
        "status": "running",
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "diagnostic_kind": "correctness_smoke" if args.smoke else "performance_phase_diagnostic",
        "smoke_only": args.smoke,
        "repeat_count": repeats,
        "seed": args.seed,
        "count_per_workload": count,
        "message_bytes": SIZE,
        "profile": {"bend": "chrome146", "curl_cffi": "chrome146"},
        "conditions": [condition_name(condition) for condition in CONDITIONS],
        "timing_definitions": {
            "total_elapsed_ns": "client monotonic interval starts immediately before sending the start control and ends after the final message check and release; setup, corpus preparation, TLS upgrade, warmup, and close are outside",
            "receive_assembly_ns": "sum of intervals around each WebSocket receive API call; Bend includes result unwrapping, and both include blocking, TLS/socket work, parser/reassembly, and any library assembly/copy",
            "validation_release_ns": "sum of opcode validation, ordered expected-byte equality over the full message, and immediate release of actual and expected buffers; the 8-byte decimal sequence prefix is part of exact equality",
            "residual_ns": "total minus receive/assembly and validation/release; includes start-control send, loop/control work, and gaps between phase samples; not pure overhead or network time",
            "control_mode": "same client validation/release code and common total timer, but no per-message phase clock calls",
            "observer_effect": "paired phase/control total differences estimate the additional per-message phase clock calls within this diagnostic client skeleton, not against the historical client",
            "peer_send_interval": "peer monotonic interval starts after decoding the start control and ends after the final TLS plaintext Write; client total includes sending the start control, so these intervals are not aligned or additive",
            "peer_tls_write_wall": "sum of wall intervals around TLS Conn.Write calls; includes TLS work, blocking, and scheduling, and does not measure wire bytes",
            "peer_process_cpu": "whole peer-process user/system CPU deltas sampled around the peer send interval; not thread-only CPU",
            "peer_observer_effect": "peer timing instrumentation is held constant in all four client conditions; its observer effect is not separately isolated",
        },
        "corpus": {
            "messages": "ordered binary messages of exactly 65,536 bytes",
            "sequence": "zero-padded 8-digit decimal index at bytes 0..7",
            "body": "remaining bytes use (index * 31) mod 128",
            "flush_batches": [1, 64],
            "warmup": "one binary warmup echo, exact byte and opcode checked before total timer",
        },
        "negative_controls": "corrupt final byte in first payload and swap sequence messages 1 and 2; both clients must reject for both flush batches",
        "execution_order": [],
        "source_sha256": {},
        "binary_sha256": {},
        "samples_file": "samples.jsonl",
    })
    all_samples = []
    try:
        prefix = Path(os.environ.get("SCRAPANIUM_CURL_DIR", ROOT / ".deps/curl")).resolve()
        env, library_path, backend_path = make_env(prefix)
        if args.sanitize:
            env["ASAN_OPTIONS"] = "abort_on_error=1:halt_on_error=1:detect_leaks=1"
            env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
        if not backend_path.is_file():
            raise FileNotFoundError(f"configured backend does not exist: {backend_path}")
        source_before_build = hash_sources()
        build_dir = ROOT / "build/phase-diagnostic" / artifact.output_dir.name
        bend_binary, peer_binary = build_artifacts(args, build_dir)
        source_hashes = hash_sources()
        if source_before_build != source_hashes:
            raise RuntimeError("diagnostic source changed during build")
        backend_sha = digest(backend_path)
        compiler_info = compiler_provenance()
        binary_hashes = {"bend": digest(bend_binary), "phase_peer": digest(peer_binary)}
        binding_info = curl_binding_provenance(env)
        expected_binding = json.loads((ROOT / "dependencies.json").read_text())["curl_cffi_matched"]["version"]
        if binding_info["version"] != expected_binding:
            raise RuntimeError(f"curl_cffi version {binding_info['version']!r} does not match pin {expected_binding}")
        artifact.manifest.update({
            "platform": platform.platform(),
            "python": sys.version,
            "cpu_count": os.cpu_count(),
            "cpu_model": cpu_model(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "compiler": compiler_info,
            "cc_version": command_output([os.environ.get("CC", "clang"), "--version"]),
            "go_version": command_output(["go", "version"]),
            "dependencies": json.loads((ROOT / "dependencies.json").read_text()),
            "curl_cffi_binding": binding_info,
            "backend": {"path": str(backend_path), "library_path": str(library_path),
                        "sha256": backend_sha},
            "source_sha256": source_hashes,
            "binary_sha256": binary_hashes,
            "built_by_this_run": True,
            "sanitize_build": args.sanitize,
            "source_and_binary_hashes_frozen_before_first_control": True,
            "source_hashes_checked_after_samples": False,
        })
        artifact.write_manifest()
        corpus_path = None
        with tempfile.TemporaryDirectory(prefix="scrapanium-wss-phase-") as temp:
            temp_path = Path(temp)
            context, ca = certificate(temp_path)
            corpus_path = temp_path / "body.bin"
            body = bytes((index * 31) % 128 for index in range(SIZE - 8))
            corpus_path.write_bytes(body)
            artifact.manifest["corpus"]["body_sha256"] = digest(corpus_path)
            negative_plan = []
            positive_plan = []
            negative_sequence = 0
            for batch in (1, 64):
                for client in CLIENTS:
                    for mode in MODES:
                        for fault in ("corrupt", "swap"):
                            negative_sequence += 1
                            negative_plan.append({
                                "kind": "negative_correctness_control",
                                "run_id": f"negative-{negative_sequence}-{client}-{mode}-{fault}-flush{batch}",
                                "batch": batch,
                                "count": SMOKE_COUNT,
                                "client": client,
                                "mode": mode,
                                "fault": fault,
                            })
            rng = random.Random(args.seed)
            for batch in (1, 64):
                schedule = workload_schedule(rng.randrange(1 << 63), repeats)
                validate_schedule(schedule, repeats)
                for repeat, indices in enumerate(schedule, 1):
                    for condition_index in indices:
                        client, mode = CONDITIONS[condition_index]
                        positive_plan.append({
                            "kind": "correctness_smoke" if args.smoke else "performance_sample",
                            "run_id": f"{artifact.output_dir.name}-flush{batch}-r{repeat}-{client}-{mode}",
                            "batch": batch,
                            "count": count,
                            "repeat": repeat,
                            "client": client,
                            "mode": mode,
                        })
            planned = negative_plan + positive_plan
            if len({item["run_id"] for item in planned}) != len(planned):
                raise RuntimeError("diagnostic schedule has duplicate run IDs")
            artifact.manifest["planned_schedule"] = planned
            artifact.manifest["schedule_method"] = (
                "16 negative controls first (8 messages); then four-condition Williams orders "
                "with every condition once per repeat and each condition in every position once per four-repeat block"
            )
            artifact.manifest["negative_control_count"] = len(negative_plan)
            artifact.write_manifest()
            peer, peer_url = start_peer(peer_binary, ca, temp_path / "key.pem")
            try:
                for item in negative_plan:
                    artifact.manifest["execution_order"].append(item["run_id"])
                    artifact.write_manifest()
                    run_negative(
                        artifact, peer, peer_url,
                        bend_binary if item["client"] == CLIENTS[0] else MATCHED_PYTHON,
                        env, ca, corpus_path, backend_sha,
                        item["batch"], item["count"], item["client"], item["mode"],
                        item["fault"], item["run_id"],
                    )
                for item in positive_plan:
                    artifact.manifest["execution_order"].append(item["run_id"])
                    artifact.write_manifest()
                    condition = (item["client"], item["mode"])
                    sample = run_positive(
                        artifact, peer, peer_url,
                        bend_binary if item["client"] == CLIENTS[0] else MATCHED_PYTHON,
                        env, ca, corpus_path, backend_sha,
                        item["batch"], item["count"], item["repeat"], condition, item["run_id"],
                    )
                    all_samples.append(sample)
            finally:
                peer.stdin.close()
                try:
                    peer.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    peer.kill()
                    peer.wait()
        source_hashes_after = hash_sources()
        if source_hashes_after != source_hashes:
            raise RuntimeError("diagnostic source changed after hashes were frozen")
        if digest(bend_binary) != binary_hashes["bend"] or digest(peer_binary) != binary_hashes["phase_peer"]:
            raise RuntimeError("diagnostic binary changed after hashes were frozen")
        if digest(backend_path) != backend_sha:
            raise RuntimeError("curl backend changed after hashes were frozen")
        if compiler_provenance() != compiler_info:
            raise RuntimeError("Bend compiler/Bun inputs changed after hashes were frozen")
        if curl_binding_provenance(env) != binding_info:
            raise RuntimeError("curl_cffi binding or matched Python inputs changed after hashes were frozen")
        artifact.manifest["source_hashes_checked_after_samples"] = True
        artifact.manifest["compiler_inputs_checked_after_samples"] = True
        artifact.manifest["binding_inputs_checked_after_samples"] = True
        retained_records = validate_retained_schedule(artifact, len(positive_plan))
        artifact.manifest["negative_control_count"] = sum(
            item.get("kind") == "negative_correctness_control" for item in retained_records
        )
        artifact.manifest["completed_sample_count"] = len(retained_records)
        if not args.smoke:
            artifact.manifest["paired_observer_effect"] = paired_observer_effect(all_samples)
            artifact.manifest["performance_sample_count"] = len(all_samples)
            if len(all_samples) != 96:
                raise RuntimeError(f"expected 96 performance samples, got {len(all_samples)}")
        else:
            artifact.manifest["performance_sample_count"] = 0
            artifact.manifest["correctness_smoke_positive_count"] = len(all_samples)
            artifact.manifest["smoke_timing_warning"] = "smoke timings are not performance evidence"
        artifact.manifest["status"] = "complete"
        artifact.manifest["completed_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        artifact.write_manifest()
    except BaseException as error:
        artifact.manifest["status"] = "failed"
        artifact.manifest["failure"] = f"{type(error).__name__}: {error}"
        artifact.manifest["failed_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        artifact.write_manifest()
        raise
    finally:
        artifact.close()
    print(artifact.output_dir)


if __name__ == "__main__":
    main()
