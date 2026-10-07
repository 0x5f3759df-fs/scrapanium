#!/usr/bin/env python3
"""Bounded attribution diagnostic for the 64 KiB WSS workload.

The schedule has 12 circular-rotation rows across 12 client/mode/batch
cells (144 positive runs), after 24 fault controls. The smoke option runs 12
eight-message positives and 24 fault controls only; neither mode is a throughput claim.
"""
import argparse
import datetime
import importlib
import json
import os
from pathlib import Path
import platform
import random
import select
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "benchmarks"))
phase = importlib.import_module("websocket_phase_diagnostic")

CANDIDATE_ROOT = Path("/home/baidu/scrapanium-experiments/wss-raw-recv-probe-20260924/worktree")
BASELINE_ROOT = Path("/home/baidu/scrapanium-experiments/wss-raw-recv-comparison-20261005/baseline-c492717")
COMPILER_ROOT = Path("/home/baidu/scrapanium-experiments/bend2-027-compat/compiler-2.0.27")
BUN_PATH = CANDIDATE_ROOT / ".deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun"
CURL_PREFIX = Path("/mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl")
MATCHED_PYTHON = Path("/usr/bin/python3.14")
MATCHED_SITE = CANDIDATE_ROOT / "build/local-site-packages-matched-20261005"
phase.MATCHED_PYTHON = MATCHED_PYTHON
BEND_CLIENT = ROOT / "src/benchmarks/wss_diag.bend"
PYTHON_CLIENT = ROOT / "benchmarks/wss_diag_python.py"
CLOCK_SOURCE = ROOT / "src/benchmarks/websocket_attribution_clock.c"
PHASE_CLOCK_SOURCE = ROOT / "src/benchmarks/websocket_phase_clock.c"
SHIM_SOURCE = ROOT / "benchmarks/websocket_attribution_shim.c"
PEER_SOURCE = ROOT / "benchmarks/websocket_attribution_peer.go"
SIZE = 65536
PERFORMANCE_COUNT = 1024
SMOKE_COUNT = 8
REPEATS = 12
SAMPLE_FLOOR = 80
BEND_CLIENTS = ("candidate-bend", "baseline-bend")
CLIENTS = ("candidate-bend", "baseline-bend", "curl_cffi-matched")
MODES = ("control", "attribution")
BATCHES = (1, 64)
FAULTS = ("corrupt", "swap")
CELLS = tuple((client, mode, batch)
              for batch in BATCHES for client in CLIENTS for mode in MODES)


def source_paths():
    fixed = [
        ROOT / "dependencies.json", ROOT / "src/scrapanium.bend", ROOT / "src/http.bend",
        ROOT / "scripts/build.py", ROOT / "tests/lab.py",
        ROOT / "src/benchmarks/websocket.bend", ROOT / "src/native/bend_bridge.c",
        ROOT / "src/benchmarks/wss_diag.bend", ROOT / "src/benchmarks/websocket_phase_clock.c",
        ROOT / "src/benchmarks/websocket_attribution_clock.c",
        ROOT / "benchmarks/websocket_phase_diagnostic.py",
        BEND_CLIENT, PYTHON_CLIENT, CLOCK_SOURCE, PHASE_CLOCK_SOURCE,
        SHIM_SOURCE, ROOT / "benchmarks/websocket_perf_shim.c", PEER_SOURCE,
        Path(__file__).resolve(), ROOT / "perf_window_smoke.py",
        ROOT / "schedule_unit.py", ROOT / "perf_campaign.py",
        ROOT / "PLAN.md", ROOT / "SOURCE_INVENTORY.md",
        *sorted((ROOT / "src").rglob("*.bend")),
        *sorted((ROOT / "src").rglob("*.c")),
    ]
    native = [
        *sorted((BASELINE_ROOT / "native").glob("*")),
        *sorted((CANDIDATE_ROOT / "native").glob("*")),
    ]
    return sorted({path for path in [*fixed, *native] if path.is_file()}, key=str)


def hash_sources():
    result = {}
    for path in source_paths():
        if path.is_relative_to(BASELINE_ROOT):
            key = "baseline/" + str(path.relative_to(BASELINE_ROOT))
        elif path.is_relative_to(CANDIDATE_ROOT):
            key = "candidate/" + str(path.relative_to(CANDIDATE_ROOT))
        else:
            key = str(path.relative_to(ROOT))
        result[key] = phase.digest(path)
    return result

def write_bytes_exclusive(path, value):
    with Path(path).open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def slug(value):
    return "".join(character if character.isalnum() or character in "-_" else "-"
                   for character in value)


def balanced_schedule(seed):
    rng = random.Random(seed)
    base = list(range(len(CELLS)))
    rng.shuffle(base)
    schedule = []
    for _ in range(REPEATS // len(CELLS)):
        rows = [base[offset:] + base[:offset] for offset in range(len(CELLS))]
        rng.shuffle(rows)
        schedule.extend(rows)
    if len(schedule) != REPEATS:
        raise RuntimeError("fixed schedule does not contain 12 complete rotation rows")
    for start in range(0, REPEATS, len(CELLS)):
        rows = schedule[start:start + len(CELLS)]
        positions = [[0] * len(CELLS) for _ in CELLS]
        for row in rows:
            if sorted(row) != list(range(len(CELLS))):
                raise RuntimeError("schedule row is not a permutation of all 12 cells")
            for position, condition in enumerate(row):
                positions[condition][position] += 1
        if any(value != 1 for row in positions for value in row):
            raise RuntimeError("12-row rotation block is not position-balanced")
    return schedule


def control_reply(peer, command, timeout=45):
    if peer.poll() is not None:
        raise RuntimeError(f"attribution peer exited with status {peer.returncode}")
    peer.stdin.write(command + "\n")
    peer.stdin.flush()
    try:
        result = json.loads(phase.line(peer, timeout=timeout))
    except (ValueError, RuntimeError) as error:
        raise RuntimeError(f"invalid attribution peer response to {command!r}: {error}") from error
    if result.get("command") != command.split()[0]:
        raise RuntimeError(f"peer answered {result.get('command')!r} to {command!r}")
    return result


def client_line(process, timeout):
    if not select.select([process.stdout], [], [], timeout)[0]:
        raise RuntimeError(f"client {process.pid} did not become ready")
    value = process.stdout.readline()
    if not value:
        raise RuntimeError(f"client {process.pid} closed stdout before readiness")
    return value.strip()


def start_peer(binary, ca, key, profile_dir):
    cpu_path = (profile_dir / "cpu.prof").resolve()
    trace_path = (profile_dir / "trace.out").resolve()
    process = subprocess.Popen(
        [str(binary), "-cert", str(ca), "-key", str(key),
         "-cpu-profile", str(cpu_path), "-trace", str(trace_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        url = phase.line(process, timeout=20)
        if not url.startswith("wss://127.0.0.1:"):
            raise RuntimeError(f"peer announced an unexpected URL: {url!r}")
        return process, url, cpu_path, trace_path
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.communicate()
        raise


def build_artifacts(args, build_dir):
    if build_dir.exists():
        allowed = {build_dir / "go-cache", build_dir / "python-bytecode-cache"}
        if not build_dir.is_dir() or any(path not in allowed for path in build_dir.iterdir()):
            raise FileExistsError(f"refusing to overwrite diagnostic build directory: {build_dir}")
    else:
        build_dir.mkdir(parents=True, exist_ok=False)
    compiler = shutil.which("clang")
    if not compiler:
        raise FileNotFoundError("clang is required to build the diagnostic shim")
    curl_lib = CURL_PREFIX / "lib" if (CURL_PREFIX / "lib/libcurl-impersonate.so").exists() else CURL_PREFIX
    bend_binaries, generated = {}, {}
    for client, source_root in (("candidate-bend", CANDIDATE_ROOT),
                                ("baseline-bend", BASELINE_ROOT)):
        suffix = "-asan" if args.sanitize else ""
        binary = build_dir / f"bench-ws-{client}{suffix}"
        generated_c = binary.with_suffix(".generated.c")
        subprocess.run(phase.compiler() + [str(BEND_CLIENT), "-o", str(generated_c)], check=True)
        command = [
            compiler, str(generated_c), str(source_root / "native/scrapanium.c"),
            "-std=c11", "-O2" if args.sanitize else "-O3", "-g", "-fno-omit-frame-pointer",
            "-I" + str(CURL_PREFIX / "include"), "-I" + str(source_root / "native"),
            "-lpthread", "-lm", "-L" + str(curl_lib),
            "-Wl,-rpath," + str(curl_lib), "-lcurl-impersonate",
        ]
        if args.sanitize:
            runtime = Path(phase.command_output([
                compiler, "-print-file-name=libclang_rt.asan-x86_64.so",
            ])).resolve()
            if not runtime.is_file():
                raise RuntimeError("Clang shared ASan runtime is unavailable")
            command.extend(["-Wl,-rpath," + f"{curl_lib}:{runtime.parent}",
                            "-fsanitize=address,undefined", "-shared-libasan"])
        command.extend(["-o", str(binary)])
        subprocess.run(command, check=True)
        bend_binaries[client] = binary
        generated[client] = generated_c
    shim_binary = build_dir / "libwsattribution.so"
    shim_sanitized = build_dir / "libwsattribution-asan.so" if args.sanitize else None
    peer_binary = build_dir / "websocket-attribution-peer"
    shim_command = [
        compiler, "-std=c11", "-O2", "-g", "-Wall", "-Wextra", "-Werror",
        "-fPIC", "-shared", "-I" + str(CURL_PREFIX / "include"), str(SHIM_SOURCE),
        "-ldl", "-pthread",
    ]
    subprocess.run([*shim_command, "-o", str(shim_binary)], check=True)
    if shim_sanitized:
        subprocess.run([*shim_command, "-fsanitize=address", "-o", str(shim_sanitized)], check=True)
    go_executable = shutil.which("go")
    if not go_executable:
        raise FileNotFoundError("go is required to build the attribution peer")
    go_env = phase.go_build_environment(go_executable, build_dir / "go-cache")
    (build_dir / "go-cache").mkdir(exist_ok=True)
    subprocess.run([go_executable, "build", "-trimpath", "-o", str(peer_binary), str(PEER_SOURCE)],
                   check=True, env=go_env)
    return bend_binaries, generated, shim_binary, shim_sanitized, peer_binary

def record_for_run(path):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"invalid shim record {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"shim record is not an object: {path}")
    return value


def run_client(client, mode, bend_binaries, shim_binary, url, ca, body_path,
               count, env, record_path, asan_runtime=None, timeout=120):
    command = ([str(bend_binaries[client]), "--threads", "1"]
               if client in BEND_CLIENTS else
               [str(MATCHED_PYTHON), str(PYTHON_CLIENT)])
    child_env = {
        **env,
        "SCRAPANIUM_BENCH_URL": url, "SCRAPANIUM_BENCH_CA": ca,
        "SCRAPANIUM_BENCH_COUNT": str(count), "SCRAPANIUM_BENCH_BODY": str(body_path),
        "SCRAPANIUM_ATTRIBUTION_MODE": mode,
        "SCRAPANIUM_BENCH_PHASE_MODE": mode,
        "SCRAPANIUM_ATTRIBUTION_RECORD": str(record_path),
    }
    preload = [str(shim_binary)]
    if client in BEND_CLIENTS and asan_runtime:
        preload.insert(0, str(asan_runtime))
    child_env["LD_PRELOAD"] = ":".join(preload)
    if client in BEND_CLIENTS and asan_runtime:
        child_env["ASAN_OPTIONS"] = "abort_on_error=1:halt_on_error=1:detect_leaks=1"
        child_env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    process = None
    parent_gate_ns = parent_done_ns = None
    backend = launch_error = None
    output = errors = ""
    returncode = None
    collected = False
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, bufsize=1, env=child_env)
        ready = client_line(process, timeout=45)
        if ready != "ready":
            raise RuntimeError(f"invalid client readiness line: {ready!r}")
        backend = phase.mapped_backend(process)
        parent_gate_ns = time.monotonic_ns()
        process.stdin.write("x")
        process.stdin.flush()
        output, errors = process.communicate(timeout=timeout)
        collected, returncode = True, process.returncode
    except BaseException as error:
        launch_error = f"{type(error).__name__}: {error}"
    finally:
        if process is not None and not collected:
            if process.poll() is None:
                process.kill()
            try:
                output, errors = process.communicate(timeout=5)
            except BaseException as error:
                launch_error = launch_error or f"output collection failed: {error}"
    parent_done_ns = time.monotonic_ns()
    shim_record = None
    shim_record_error = None
    try:
        shim_record = record_for_run(record_path)
    except BaseException as error:
        shim_record_error = f"{type(error).__name__}: {error}"
    phase_metrics = {}
    for raw in output.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        if key in ("client_interval_wall_ns", "receive_assembly_ns", "validation_release_ns"):
            if key in phase_metrics:
                raise RuntimeError(f"client emitted duplicate diagnostic field {key}")
            try:
                phase_metrics[key] = int(value)
            except ValueError as error:
                raise RuntimeError(f"client emitted a non-integer {key}: {value!r}") from error
    return {
        "returncode": returncode, "stdout": output, "stderr": errors,
        "mapped_backend": backend, "parent_gate_monotonic_ns": parent_gate_ns,
        "parent_done_monotonic_ns": parent_done_ns, "launch_error": launch_error,
        "shim_record": shim_record, "shim_record_error": shim_record_error,
        "shim_record_path": str(record_path),
        "shim_record_sha256": phase.digest(record_path) if Path(record_path).is_file() else None,
        "phase_metrics": phase_metrics,
    }

def peer_stats(peer, run_id):
    deadline = time.monotonic() + 30
    while True:
        reply = control_reply(peer, "stats", timeout=15)
        records = reply.get("records")
        if not isinstance(records, dict) or run_id not in records:
            raise RuntimeError(f"peer has no finalized record for {run_id}")
        record = records[run_id]
        if record.get("completed") is True or record.get("error"):
            return record
        if time.monotonic() >= deadline:
            raise RuntimeError(f"peer record did not finalize for {run_id}")
        time.sleep(0.01)


def sanitizer_findings(client_result):
    return phase.sanitizer_findings(client_result.get("stdout", ""),
                                    client_result.get("stderr", ""))


def validate_peer_positive(record, count, batch):
    errors = []
    expected = {
        "data_frames": count,
        "data_payload_bytes": count * SIZE,
        "data_frame_bytes": count * (SIZE + 10),
        "data_plaintext_bytes_written": count * (SIZE + 10),
        "send_write_calls": (count + batch - 1) // batch,
        "warmups": 1,
        "starts": 1,
    }
    if record.get("completed") is not True or record.get("error"):
        errors.append(f"peer did not complete cleanly: {record.get('error')!r}")
    for key, value in expected.items():
        if type(record.get(key)) is not int or record[key] != value:
            errors.append(f"peer {key} was {record.get(key)!r}, expected {value}")
    for key in ("send_interval_wall_ns", "tls_write_wall_ns_sum",
                "peer_process_cpu_user_ns", "peer_process_cpu_system_ns"):
        if type(record.get(key)) is not int or record[key] < 0:
            errors.append(f"peer {key} is not a nonnegative integer")
    if all(type(record.get(key)) is int for key in
           ("send_interval_wall_ns", "tls_write_wall_ns_sum")) and \
       record["tls_write_wall_ns_sum"] > record["send_interval_wall_ns"]:
        errors.append("TLS Write wall sum exceeds the peer send interval")
    for key in ("tls_version", "tls_cipher"):
        if type(record.get(key)) is not int or record[key] <= 0:
            errors.append(f"peer {key} is missing or invalid")
    return errors


def validate_peer_identity(record, plan_item):
    errors = []
    expected_profiled = plan_item["kind"] != "negative_correctness_control"
    for key in ("run_id", "client", "mode", "batch"):
        if record.get(key) != plan_item.get(key):
            errors.append(f"peer {key}={record.get(key)!r} disagrees with plan {plan_item.get(key)!r}")
    if record.get("profiled") is not expected_profiled:
        errors.append(f"peer profiled={record.get('profiled')!r}, expected {expected_profiled}")
    if record.get("fault", "") != plan_item.get("fault", ""):
        errors.append("peer fault label disagrees with plan")
    return errors


def validate_receive_api(record, api, mode, expected_bytes, required):
    errors = []
    metrics = record.get(api)
    if not isinstance(metrics, dict):
        return [f"{api} accounting is missing"]
    if metrics.get("timing_enabled") != (mode == "attribution"):
        errors.append(f"{api} timing mode disagrees with the planned client mode")
    calls = metrics.get("calls")
    again = metrics.get("curle_again")
    ok = metrics.get("ok")
    other = metrics.get("other")
    if type(calls) is not int or calls < 0:
        errors.append(f"{api} call count is invalid")
    elif required and calls < 1:
        errors.append(f"{api} was not interposed")
    elif not required and calls != 0:
        errors.append(f"unexpected receive API {api} was interposed")
    if all(type(value) is int and value >= 0 for value in (calls, again, ok, other)):
        if calls != ok + again + other:
            errors.append(f"{api} result counts do not sum to calls")
    else:
        errors.append(f"{api} result counts are invalid")
    if other != 0:
        errors.append(f"{api} returned an unexpected error")
    if metrics.get("bytes") != expected_bytes:
        errors.append(f"{api} successful bytes were {metrics.get('bytes')!r}, expected {expected_bytes}")
    for field in ("wall_sum_across_threads_ns", "thread_cpu_sum_across_threads_ns"):
        value = metrics.get(field)
        if type(value) is not int or value < 0:
            errors.append(f"{api} {field} is invalid")
        elif mode == "control" and value != 0:
            errors.append(f"control mode unexpectedly timed {api} ({field})")
        elif mode == "attribution" and calls and value == 0:
            errors.append(f"attribution mode has no {api} {field}")
    threads = record.get("threads")
    if not isinstance(threads, list) or not threads:
        return errors + ["shim per-thread records are missing"]
    totals = {"calls": 0, "curle_again": 0, "ok": 0, "other": 0, "bytes": 0,
              "wall_sum_across_threads_ns": 0,
              "thread_cpu_sum_across_threads_ns": 0}
    suffixes = {
        "calls": "_calls",
        "curle_again": "_curle_again",
        "ok": "_ok",
        "other": "_other",
        "bytes": "_bytes",
        "wall_sum_across_threads_ns": "_wall_sum_ns",
        "thread_cpu_sum_across_threads_ns": "_thread_cpu_sum_ns",
    }
    for thread in threads:
        if not isinstance(thread, dict):
            errors.append("shim per-thread record is malformed")
            continue
        for target, suffix in suffixes.items():
            source = api + suffix
            value = thread.get(source)
            if type(value) is not int or value < 0:
                errors.append(f"per-thread {api} {source} is invalid")
            else:
                totals[target] += value
        if thread.get(api + "_tid_mismatch") != 0:
            errors.append(f"{api} clock crossed OS threads")
        wall = thread.get(api + "_wall_sum_ns", 0)
        cpu = thread.get(api + "_thread_cpu_sum_ns", 0)
        if type(wall) is int and type(cpu) is int and wall < cpu:
            errors.append(f"{api} thread CPU sum exceeds nested wall sum")
    for key, value in totals.items():
        if metrics.get(key) != value:
            errors.append(f"aggregate {api} {key} disagrees with per-thread sum")
    return errors


def validate_shim_positive(record, result, mode, expected_payload_bytes,
                           expected_raw_bytes, receive_api):
    errors = []
    if not isinstance(record, dict):
        return ["client did not write a shim record"]
    if record.get("mode") != mode:
        errors.append(f"shim mode {record.get('mode')!r} disagrees with plan {mode!r}")
    for field in ("started", "completed"):
        if record.get(field) is not True:
            errors.append(f"shim record {field} is not true")
    for field in ("clock_error", "thread_overflow", "write_error"):
        if record.get(field) is not False:
            errors.append(f"shim record {field} is not false")
    interval = record.get("interval")
    if not isinstance(interval, dict):
        return errors + ["shim interval is missing"]
    if interval.get("caller_thread_cpu_valid") is not True:
        errors.append("caller-thread CPU endpoints are invalid or migrated")
    if interval.get("process_cpu_valid") is not True:
        errors.append("process CPU endpoints are invalid")
    if type(interval.get("start_tid")) is not int or type(interval.get("end_tid")) is not int or \
       interval.get("start_tid") != interval.get("end_tid"):
        errors.append("client interval crossed OS threads")
    for field in ("wall_ns", "caller_thread_cpu_ns", "process_cpu_ns"):
        if type(interval.get(field)) is not int or interval[field] <= 0:
            errors.append(f"client interval {field} is not a positive integer")
    if type(interval.get("wall_ns")) is int and type(interval.get("caller_thread_cpu_ns")) is int and \
       interval["caller_thread_cpu_ns"] > interval["wall_ns"]:
        errors.append("caller thread CPU exceeds its nested total wall interval")
    if type(interval.get("start_monotonic_ns")) is not int or \
       type(interval.get("end_monotonic_ns")) is not int or \
       interval.get("end_monotonic_ns", 0) <= interval.get("start_monotonic_ns", 0):
        errors.append("client monotonic boundaries are invalid")
    else:
        if not (result["parent_gate_monotonic_ns"] <= interval["start_monotonic_ns"] <
                interval["end_monotonic_ns"] <= result["parent_done_monotonic_ns"]):
            errors.append("shim boundaries fall outside the independently sampled parent interval")
        boundary_delta = interval["end_monotonic_ns"] - interval["start_monotonic_ns"]
        if abs(boundary_delta - interval.get("wall_ns", -2)) > 1:
            errors.append("shim total duration disagrees with absolute monotonic boundaries")
    if result["returncode"] == 0:
        emitted = []
        for line in result.get("stdout", "").splitlines():
            if line.startswith("client_interval_wall_ns="):
                emitted.append(line.split("=", 1)[1])
        if len(emitted) != 1:
            errors.append("client omitted or duplicated its interval duration")
        else:
            try:
                duration = int(emitted[0])
            except ValueError:
                duration = -1
            if duration != interval.get("wall_ns"):
                errors.append("client-reported interval differs from the shim record")
    expected = {
        "curl_ws_recv": expected_payload_bytes if receive_api == "curl_ws_recv" else 0,
        "curl_easy_recv": expected_raw_bytes if receive_api == "curl_easy_recv" else 0,
    }
    if receive_api not in expected:
        errors.append(f"unsupported client receive API {receive_api!r}")
    for api, byte_count in expected.items():
        errors.extend(validate_receive_api(record, api, mode, byte_count,
                                           api == receive_api))
    threads = record.get("threads")
    if not isinstance(threads, list) or not threads:
        return errors + ["shim per-thread records are missing"]
    wait_totals = {name: {key: 0 for key in (
        "calls", "ready", "timeout", "error", "wall_sum_across_threads_ns",
        "thread_cpu_sum_across_threads_ns")} for name in ("poll", "ppoll", "select", "epoll")}
    tids = set()
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        tid = thread.get("tid")
        if type(tid) is not int or tid <= 0 or tid in tids:
            errors.append("shim per-thread TID is invalid or duplicated")
        else:
            tids.add(tid)
        for wait_name in ("poll", "ppoll", "select", "epoll"):
            item = thread.get(wait_name)
            if not isinstance(item, dict):
                errors.append(f"per-thread {wait_name} counters are missing")
                continue
            if item.get("tid_mismatch") != 0:
                errors.append(f"per-thread {wait_name} clock crossed OS threads")
            for key in ("calls", "ready", "timeout", "error", "wall_sum_ns", "thread_cpu_sum_ns"):
                value = item.get(key)
                if type(value) is not int or value < 0:
                    errors.append(f"per-thread {wait_name} {key} is invalid")
            if item.get("calls") != item.get("ready", 0) + item.get("timeout", 0) + item.get("error", 0):
                errors.append(f"per-thread {wait_name} outcomes do not sum to calls")
            if item.get("wall_sum_ns", 0) < item.get("thread_cpu_sum_ns", 0):
                errors.append(f"per-thread {wait_name} CPU sum exceeds nested wall sum")
            total = wait_totals[wait_name]
            total["calls"] += item.get("calls", 0)
            total["ready"] += item.get("ready", 0)
            total["timeout"] += item.get("timeout", 0)
            total["error"] += item.get("error", 0)
            total["wall_sum_across_threads_ns"] += item.get("wall_sum_ns", 0)
            total["thread_cpu_sum_across_threads_ns"] += item.get("thread_cpu_sum_ns", 0)
    aggregate_waits = record.get("waits")
    if not isinstance(aggregate_waits, dict):
        errors.append("aggregate wait counters are missing")
    else:
        for wait_name, totals in wait_totals.items():
            aggregate = aggregate_waits.get(wait_name)
            if not isinstance(aggregate, dict):
                errors.append(f"aggregate {wait_name} counters are missing")
                continue
            for key, value in totals.items():
                if aggregate.get(key) != value:
                    errors.append(f"aggregate {wait_name} {key} disagrees with per-thread sum")
    if mode == "control" and any(
        total["wall_sum_across_threads_ns"] or total["thread_cpu_sum_across_threads_ns"]
        for total in wait_totals.values()
    ):
        errors.append("control mode unexpectedly clock-timed wait calls")
    return errors

def validate_negative(plan, client_result, peer_record, shim_record, backend_sha):
    errors = []
    if sanitizer_findings(client_result):
        errors.extend(f"sanitizer marker: {marker}" for marker in sanitizer_findings(client_result))
    output = client_result.get("stdout", "") + client_result.get("stderr", "")
    if client_result.get("launch_error"):
        errors.append(f"client launch/readiness failed: {client_result['launch_error']}")
    if client_result.get("shim_record_error"):
        errors.append(f"shim record read failed: {client_result['shim_record_error']}")
    if client_result.get("returncode") in (None, 0):
        errors.append("client accepted a deliberately invalid message sequence")
    if "opcode, sequence, or payload mismatch" not in output:
        errors.append("client failed without the expected opcode/sequence/payload mismatch")
    backend = client_result.get("mapped_backend")
    if not isinstance(backend, dict) or backend.get("sha256") != backend_sha:
        errors.append("negative client loaded a different curl backend")
    if not isinstance(shim_record, dict) or shim_record.get("started") is not True or \
       shim_record.get("completed") is not False:
        errors.append("negative client shim record did not retain its interrupted interval")
    if isinstance(shim_record, dict) and shim_record.get("mode") != plan["mode"]:
        errors.append("negative client shim mode disagrees with plan")
    if isinstance(shim_record, dict) and (shim_record.get("clock_error") or
       shim_record.get("thread_overflow") or shim_record.get("write_error")):
        errors.append("negative client shim record reports an accounting/write error")
    if not isinstance(peer_record, dict):
        errors.append("negative peer record is missing")
    else:
        errors.extend(validate_peer_identity(peer_record, plan))
        if peer_record.get("completed") is not True:
            errors.append("negative peer record was not finalized")
        if peer_record.get("profiled") is not False:
            errors.append("negative control was profiled")
        if peer_record.get("warmups") != 1 or peer_record.get("starts") != 1:
            errors.append("negative peer missed common warmup/start protocol")
        for field in ("data_frames", "data_payload_bytes", "data_frame_bytes",
                      "data_plaintext_bytes_written", "send_write_calls",
                      "send_interval_wall_ns", "tls_write_wall_ns_sum",
                      "peer_process_cpu_user_ns", "peer_process_cpu_system_ns"):
            if type(peer_record.get(field)) is not int or peer_record[field] < 0:
                errors.append(f"negative peer {field} is invalid")
    return errors


def plan_schedule(smoke, seed, name):
    kind = "correctness_smoke" if smoke else "attribution_sample"
    count = SMOKE_COUNT if smoke else PERFORMANCE_COUNT
    positives = []
    if smoke:
        for client, mode, batch in CELLS:
            positives.append({
                "kind": kind,
                "run_id": f"{slug(name)}-{client}-{mode}-flush{batch}",
                "client": client, "mode": mode, "batch": batch,
                "count": count, "message_bytes": SIZE, "repeat": 1,
            })
    else:
        for repeat, row in enumerate(balanced_schedule(seed), start=1):
            for condition in row:
                client, mode, batch = CELLS[condition]
                positives.append({
                    "kind": kind,
                    "run_id": f"{slug(name)}-{client}-{mode}-flush{batch}-r{repeat:02d}",
                    "client": client, "mode": mode, "batch": batch,
                    "count": count, "message_bytes": SIZE, "repeat": repeat,
                })
    negatives = []
    number = 0
    for batch in BATCHES:
        for client in CLIENTS:
            for mode in MODES:
                for fault in FAULTS:
                    number += 1
                    negatives.append({
                        "kind": "negative_correctness_control",
                        "run_id": f"{slug(name)}-negative-{number:02d}-{client}-{mode}-{fault}-flush{batch}",
                        "client": client, "mode": mode, "batch": batch,
                        "count": SMOKE_COUNT, "message_bytes": SIZE, "fault": fault,
                    })
    plan = negatives + positives
    ids = [item["run_id"] for item in plan]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate run IDs in the fixed schedule")
    expected_pos = len(CELLS) if smoke else REPEATS * len(CELLS)
    if len(negatives) != len(CELLS) * len(FAULTS) or len(positives) != expected_pos:
        raise RuntimeError("fixed schedule has an incorrect row count")
    expected_negative_cells = {
        (client, mode, batch, fault)
        for client in CLIENTS for mode in MODES for batch in BATCHES for fault in FAULTS
    }
    observed_negative_cells = {
        (item["client"], item["mode"], item["batch"], item["fault"])
        for item in negatives
    }
    if observed_negative_cells != expected_negative_cells:
        raise RuntimeError("negative controls do not cover all planned cells")
    per_cell = {(client, mode, batch): 0 for client, mode, batch in CELLS}
    for item in positives:
        per_cell[(item["client"], item["mode"], item["batch"])] += 1
    expected_each = 1 if smoke else REPEATS
    if set(per_cell) != set(CELLS) or any(count != expected_each for count in per_cell.values()):
        raise RuntimeError("positive schedule has incorrect cell counts")
    if not smoke:
        for repeat in range(1, REPEATS + 1):
            block = [item for item in positives if item["repeat"] == repeat]
            if {(item["client"], item["mode"], item["batch"]) for item in block} != set(CELLS):
                raise RuntimeError(f"repeat {repeat} omits or duplicates a cell")
    return plan

def run_attempt(artifact, plan_item, peer, peer_url, bend_binaries, shim_binary,
                env, ca, body_path, backend_sha, asan_runtime, sanitize_bend):
    sample = dict(plan_item)
    sample["sanitizer_coverage"] = (
        "Bend executable and embedded clock bridge are ASan/UBSan instrumented; preloaded shim is AddressSanitizer-instrumented; "
        "matched Python executable and its shim are uninstrumented"
        if sanitize_bend and plan_item["client"] in BEND_CLIENTS
        else "none" if not sanitize_bend
        else "matched Python executable and its preloaded shim are uninstrumented"
    )
    record_path = artifact.output_dir / "shim" / f"{slug(plan_item['run_id'])}.json"
    record_path.parent.mkdir(exist_ok=True)
    url_query = {
        "id": plan_item["run_id"], "count": plan_item["count"], "size": SIZE,
        "batch": plan_item["batch"], "client": plan_item["client"], "mode": plan_item["mode"],
    }
    if "fault" in plan_item:
        url_query["fault"] = plan_item["fault"]
    url = f"{peer_url}/stream?{urlencode(url_query)}"
    client_result = None
    appended = False
    try:
        client_result = run_client(
            plan_item["client"], plan_item["mode"], bend_binaries, shim_binary,
            url, ca, body_path, plan_item["count"], env, record_path,
            asan_runtime=asan_runtime,
        )
        sample["client_result"] = {
            key: value for key, value in client_result.items()
            if key not in ("stdout", "stderr", "shim_record")
        }
        sample["client_stdout"] = client_result["stdout"]
        sample["client_stderr"] = client_result["stderr"]
        # Preserve peer counters before validating/parsing client telemetry.
        sample["peer"] = peer_stats(peer, plan_item["run_id"])
        sample["shim"] = client_result["shim_record"]
        errors = []
        if plan_item["kind"] == "negative_correctness_control":
            errors.extend(validate_negative(plan_item, client_result, sample["peer"],
                                            sample["shim"], backend_sha))
        else:
            errors.extend(validate_peer_identity(sample["peer"], plan_item))
            if client_result["returncode"] != 0:
                errors.append(f"client exited {client_result['returncode']}")
            if client_result.get("launch_error"):
                errors.append(f"client launch/readiness failed: {client_result['launch_error']}")
            if client_result.get("shim_record_error"):
                errors.append(f"shim record read failed: {client_result['shim_record_error']}")
            errors.extend(f"sanitizer marker in client output: {marker}"
                          for marker in sanitizer_findings(client_result))
            backend = client_result.get("mapped_backend")
            if not isinstance(backend, dict) or backend.get("sha256") != backend_sha:
                errors.append("client loaded a curl backend different from the frozen stock backend")
            errors.extend(validate_peer_positive(sample["peer"], plan_item["count"],
                                                 plan_item["batch"]))
            receive_api = "curl_easy_recv" if plan_item["client"] == "candidate-bend" else "curl_ws_recv"
            errors.extend(validate_shim_positive(
                sample["shim"], client_result, plan_item["mode"], plan_item["count"] * SIZE,
                sample["peer"].get("data_plaintext_bytes_written"), receive_api))
            interval = sample["shim"].get("interval") if isinstance(sample["shim"], dict) else None
            interval = interval if isinstance(interval, dict) else {}
            sample["client_interval_wall_ns"] = interval.get("wall_ns")
            sample["caller_thread_cpu_ns"] = interval.get("caller_thread_cpu_ns")
            sample["process_cpu_ns"] = interval.get("process_cpu_ns")
            sample["receive_api"] = receive_api
            sample["curl_ws_recv"] = sample["shim"].get("curl_ws_recv") if isinstance(sample["shim"], dict) else None
            sample["curl_easy_recv"] = sample["shim"].get("curl_easy_recv") if isinstance(sample["shim"], dict) else None
            sample["wait_calls"] = sample["shim"].get("waits") if isinstance(sample["shim"], dict) else None
            sample["client_wall_minus_thread_cpu_ns"] = (
                sample["client_interval_wall_ns"] - sample["caller_thread_cpu_ns"]
                if type(sample["client_interval_wall_ns"]) is int and
                type(sample["caller_thread_cpu_ns"]) is int else None
            )
            metrics = client_result["phase_metrics"]
            sample["phase_metrics"] = metrics
            if set(metrics) != {"client_interval_wall_ns", "receive_assembly_ns",
                                "validation_release_ns"}:
                errors.append("phase timing fields are incomplete")
            elif metrics["client_interval_wall_ns"] != interval.get("wall_ns"):
                errors.append("client interval output disagrees with shim boundary")
            elif metrics["receive_assembly_ns"] < 0 or metrics["validation_release_ns"] < 0:
                errors.append("phase timing contains a negative value")
            elif plan_item["mode"] == "control" and (
                metrics["receive_assembly_ns"] != 0 or metrics["validation_release_ns"] != 0
            ):
                errors.append("control mode unexpectedly timed per-message phases")
            elif plan_item["mode"] == "attribution" and (
                metrics["receive_assembly_ns"] <= 0 or metrics["validation_release_ns"] <= 0
            ):
                errors.append("attribution mode omitted receive or checker timing")
        sample["validation_errors"] = errors
        artifact.append(sample)
        appended = True
        if errors:
            raise RuntimeError(f"invalid diagnostic run {plan_item['run_id']}: {'; '.join(errors)}")
        return sample
    except BaseException as error:
        if not appended:
            sample["failure"] = f"{type(error).__name__}: {error}"
            if client_result is not None:
                sample.setdefault("client_result", {
                    key: value for key, value in client_result.items()
                    if key not in ("stdout", "stderr", "shim_record")
                })
                sample.setdefault("client_stdout", client_result.get("stdout", ""))
                sample.setdefault("client_stderr", client_result.get("stderr", ""))
                sample.setdefault("shim", client_result.get("shim_record"))
                try:
                    sample.setdefault("peer", peer_stats(peer, plan_item["run_id"]))
                except BaseException as peer_error:
                    sample["peer_failure"] = f"{type(peer_error).__name__}: {peer_error}"
            artifact.append(sample)
        raise


def validate_completed_plan(artifact, plan):
    artifact.samples.flush()
    rows = [json.loads(value) for value in artifact.samples_path.read_text().splitlines() if value]
    retained = [row.get("run_id") for row in rows]
    planned = [item["run_id"] for item in plan]
    if retained != planned or artifact.manifest.get("execution_order") != planned:
        raise RuntimeError("retained attempts do not match the complete frozen schedule")
    if any(row.get("validation_errors") or row.get("failure") for row in rows):
        raise RuntimeError("one or more retained diagnostic attempts failed validation")
    tls_identities = set()
    for row in rows:
        for key, value in next(item for item in plan if item["run_id"] == row["run_id"]).items():
            if row.get(key) != value:
                raise RuntimeError(f"retained run {row['run_id']} disagrees with its planned {key}")
        if row["kind"] != "negative_correctness_control":
            peer_record = row.get("peer", {})
            tls_identities.add((peer_record.get("tls_version"), peer_record.get("tls_cipher")))
    if len(tls_identities) != 1:
        raise RuntimeError(f"TLS identity changed across positive conditions: {tls_identities}")
    return rows


def parse_args():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / f"benchmarks/results/websocket-attribution-diagnostic-{stamp}")
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--smoke", action="store_true",
                        help="run fixed eight-message correctness controls only")
    parser.add_argument("--sanitize", action="store_true",
                        help="sanitize the Bend smoke client; requires --smoke")
    args = parser.parse_args()
    if args.sanitize and not args.smoke:
        parser.error("--sanitize is supported only with --smoke")
    for name in ("CC", "BEND_SOURCE", "BUN", "LD_PRELOAD", "SCRAPANIUM_CURL_DIR"):
        if name in os.environ:
            parser.error(f"clear {name} to use pinned compiler/backend inputs")
    if not MATCHED_PYTHON.is_file():
        parser.error(f"matched Python executable is missing: {MATCHED_PYTHON}")
    if not MATCHED_SITE.is_dir():
        parser.error(f"matched Python package directory is missing: {MATCHED_SITE}")
    return args


def main():
    args = parse_args()
    args.output_dir = args.output_dir.resolve()
    if args.output_dir.exists():
        raise FileExistsError(f"refusing to overwrite artifact directory: {args.output_dir}")
    smoke = args.smoke
    count = SMOKE_COUNT if smoke else PERFORMANCE_COUNT
    name = args.output_dir.name
    plan = plan_schedule(smoke, args.seed, name)
    artifact = phase.Artifact(args.output_dir, {
        "schema": 1,
        "status": "running",
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "diagnostic_kind": "correctness_smoke" if smoke else "64KiB_wss_attribution",
        "smoke_only": smoke,
        "smoke_timing_warning": "correctness smoke timings are not profiling or performance evidence" if smoke else None,
        "seed": args.seed,
        "message_count_per_positive": count,
        "message_bytes": SIZE,
        "fixed_repeat_count": 1 if smoke else REPEATS,
        "positive_count": len(CELLS) if smoke else REPEATS * len(CELLS),
        "negative_control_count": len(CELLS) * len(FAULTS),
        "cpu_profile_sample_floor_per_variant": SAMPLE_FLOOR,
        "cpu_profile_floor_policy": "peer Go samples aggregate all mode/batch labels by variant; below-floor client stack cells are retained and marked inconclusive with no extension",
        "client_cells": [
            {"client": client, "mode": mode, "batch": batch}
            for client, mode, batch in CELLS
        ],
        "timing_definitions": {
            "client_interval_wall_ns": "shim CLOCK_MONOTONIC start immediately before sending the start control and stop after the final exact opcode/sequence/full-byte check and immediate actual/expected release; setup, corpus creation, TLS upgrade, warmup, and close are outside",
            "caller_thread_cpu_ns": "CLOCK_THREAD_CPUTIME_ID delta between nested boundaries, valid only when start/end Linux TIDs match; includes all caller-thread work in the total client interval, not only recv or Bend IO",
            "process_cpu_ns": "CLOCK_PROCESS_CPUTIME_ID delta over the same client interval; includes any helper threads and is separately reported",
            "curl_ws_recv_wall_sum_across_threads_ns": "sum of measured wall intervals around interposed curl_ws_recv calls, grouped by calling TID; may overlap wait wrappers and must not be added to them or treated as exclusive time",
            "curl_ws_recv_thread_cpu_sum_across_threads_ns": "sum of per-call caller-thread CPU intervals when each call starts and ends on the same TID; across-thread sums can overlap in time",
            "curl_ws_recv_scope": "per-call timing starts immediately before the real framed backend call and stops immediately after it; bytes are WebSocket payload bytes, and this scope includes libcurl's WebSocket frame parser",
            "curl_easy_recv_wall_sum_across_threads_ns": "sum of measured wall intervals around interposed raw curl_easy_recv calls; returned decrypted plaintext includes WebSocket wire frame headers and payload but excludes local application parser work",
            "curl_easy_recv_thread_cpu_sum_across_threads_ns": "sum of per-call caller-thread CPU intervals when each raw receive starts and ends on the same TID; across-thread sums can overlap in time",
            "curl_easy_recv_scope": "per-call timing starts immediately before the raw backend call and stops immediately after it; it includes backend TLS/decryption work but excludes Scrapanium's application frame parsing, cache handling, and payload assembly",
            "receive_api_comparison": "Bend uses curl_easy_recv and counts plaintext frame header plus payload bytes; Python uses curl_ws_recv and counts payload bytes after libcurl framing. These measurements have different scopes and byte counts and are not exclusive-time alternatives",
            "caller_cpu_residual": "client caller-thread CPU minus the selected receive API call CPU includes validation, releases, assembly, wrapper work, start-control send, and shim bookkeeping; it is not pure parser or wrapper CPU and must not be combined with phase fractions from a different run",
            "wait_wrappers": "counts and, in attribution mode, nested wall/thread-CPU sums for poll, ppoll, select, and epoll_wait only; other waits/syscalls are not covered, and wait intervals can be nested inside either receive API",
            "wall_minus_thread_cpu": "an unaccounted/off-CPU wall gap that can include scheduling and clock-boundary overhead; it is not automatically socket-blocked time",
            "control_mode": "same shim, exact validation/release, client wall/thread/process clocks, and cheap call/result counters; skips per-call recv/wait clock reads but still pays common interposition, mutex/atomic bookkeeping, and the peer CPU profiler/trace",
            "observer_effect": "control versus attribution estimates only the incremental per-call client timing clocks in this instrumented skeleton; peer profiling remains enabled and its observer effect is not isolated",
            "peer_metrics": "peer send interval, TLS Conn.Write wall sum, whole-process user/system CPU, Go CPU profile and runtime trace are separate metrics; peer wall/CPU/profile intervals are not additive to client intervals",
            "pprof_floor": "Go pprof labels retain client/mode/batch; the predeclared 80-sample floor aggregates all four labels per client variant, with no adaptive extension",
        },
        "corpus": {
            "messages": "ordered binary payloads exactly 65,536 bytes each",
            "sequence": "zero-padded eight-digit decimal at bytes 0..7; exact complete payload comparison on every response",
            "body": "remaining bytes repeat the fixed deterministic (offset * 31) mod 128 pattern",
            "batches": list(BATCHES),
            "warmup": "one binary warmup echoed, opcode and exact bytes checked before the interval",
            "raw_frame_bytes": "Bend raw curl_easy_recv returns each unmasked 65,536-byte binary frame as ten header bytes plus 65,536 payload bytes; curl_ws_recv reports payload bytes only",
        },
        "negative_controls": "24 count-eight runs: corrupt and swap faults for each of 12 client/mode/flush cells, before profile-start",
        "schedule_method": "fixed 12 complete 12-cell repeat blocks across variants, instrumentation modes, and flush sizes; cyclic rotations balance cell position within each repeat; no adaptive extension or rerun",
        "planned_schedule": plan,
        "execution_order": [],
        "source_sha256": {},
        "binary_sha256": {},
        "samples_file": "samples.jsonl",
        "profiles": {"peer_cpu": "cpu.prof", "peer_trace": "trace.out"},
        "client_perf_sampling_policy_pending_WSS_interval_proof_and_root_release": {
            "package": "private Ubuntu linux-perf 7.0.0-38.38; runtime 7.0.14",
            "event": "cpu-clock:u at 499 Hz with frame-pointer callchains and --clockid mono",
            "sampling_window": "profile-only processes attach disabled after WSS warmup; shim sends perf --control FD enable/disable within saved client interval bounds",
            "fixed_samples": "12 full-workload runs in each of six variant/flush cells (72 total); aggregate floor 100 retained samples per variant/flush cell; no adaptive extension",
            "compiler_flags": "-O3 -g -fno-omit-frame-pointer for both Bend variants",
            "timing_use": "profile-only runs are excluded from all primary timing/observer-effect rows",
        },
    })
    peer = None
    profile_started = False
    try:
        stock_prefix = CURL_PREFIX.resolve()
        os.environ["BEND_SOURCE"] = str(COMPILER_ROOT)
        os.environ["BUN"] = str(BUN_PATH)
        env, library_path, backend_path = phase.make_env(stock_prefix)
        env["BEND_SOURCE"] = str(COMPILER_ROOT)
        env["BUN"] = str(BUN_PATH)
        env["PYTHONPATH"] = str(MATCHED_SITE)
        env.pop("ASAN_OPTIONS", None)
        env.pop("UBSAN_OPTIONS", None)
        if not backend_path.is_file():
            raise FileNotFoundError(f"pinned stock backend is missing: {backend_path}")
        clang_path = shutil.which("clang")
        go_path = shutil.which("go")
        if not clang_path or not go_path:
            raise FileNotFoundError("clang and go are required by the diagnostic build")
        build_dir = ROOT / "build/attribution-diagnostic" / name
        build_dir.mkdir(parents=True, exist_ok=False)
        (build_dir / "go-cache").mkdir()
        python_cache = build_dir / "python-bytecode-cache"
        python_cache.mkdir()
        env["PYTHONPYCACHEPREFIX"] = str(python_cache.resolve())
        go_env = phase.go_build_environment(go_path, build_dir / "go-cache")
        source_before_build = hash_sources()
        compiler_info = phase.compiler_provenance()
        binding_info = phase.curl_binding_provenance(env)
        clang_info = phase.executable_provenance(clang_path)
        clang_resource_info = phase.clang_resource_provenance(clang_path)
        go_info = phase.executable_provenance(go_path, ("version",))
        go_inputs_info = phase.go_build_input_provenance(go_path, PEER_SOURCE, go_env)
        curl_headers_info = phase.directory_file_provenance(CURL_PREFIX / "include")
        backend_sha = phase.digest(backend_path)
        bend_binaries, generated_c, shim_binary, shim_sanitized, peer_binary = build_artifacts(args, build_dir)
        source_hashes = hash_sources()
        if source_before_build != source_hashes:
            raise RuntimeError("diagnostic source changed during build")
        if (phase.compiler_provenance() != compiler_info or
            phase.curl_binding_provenance(env) != binding_info or
            phase.executable_provenance(clang_path) != clang_info or
            phase.executable_provenance(go_path, ("version",)) != go_info or
            phase.clang_resource_provenance(clang_path) != clang_resource_info or
            phase.go_build_input_provenance(go_path, PEER_SOURCE, go_env) != go_inputs_info or
            phase.directory_file_provenance(CURL_PREFIX / "include") != curl_headers_info or
            phase.digest(backend_path) != backend_sha):
            raise RuntimeError("a pinned build input changed during compilation")
        expected_binding = json.loads((ROOT / "dependencies.json").read_text())["curl_cffi_matched"]["version"]
        if binding_info["version"] != expected_binding:
            raise RuntimeError(f"curl_cffi version {binding_info['version']!r} does not match pin {expected_binding}")
        clang_version = clang_info["version"]
        go_version = go_info["version"]
        binary_hashes = {
            **{"bend_" + client: phase.digest(binary) for client, binary in bend_binaries.items()},
            **{"generated_" + client: phase.digest(path) for client, path in generated_c.items()},
            "shim": phase.digest(shim_binary),
            "peer": phase.digest(peer_binary),
        }
        if shim_sanitized:
            binary_hashes["shim_sanitized"] = phase.digest(shim_sanitized)
        artifact.manifest.update({
            "platform": platform.platform(), "python": sys.version,
            "cpu_count": os.cpu_count(), "cpu_model": phase.cpu_model(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "compiler": compiler_info, "clang_path": clang_path,
            "clang_version": clang_version, "go_version": go_version,
            "toolchain_executables": {"clang": clang_info, "go": go_info},
            "curl_include_headers": curl_headers_info,
            "clang_builtin_headers": clang_resource_info,
            "go_build_inputs": go_inputs_info,
            "go_build_flags": ["build", "-trimpath", "-o", "<peer-binary>", str(PEER_SOURCE)],
            "clang_build_flags": {
                "bend_native": [
                    "-std=c11", "-O2" if args.sanitize else "-O3", "-g",
                    "-fno-omit-frame-pointer", "-I<CURL_PREFIX/include>", "-I<native-tree>",
                    "-lpthread", "-lm", "-L<curl-library-dir>", "-Wl,-rpath,<curl-library-dir>",
                    "-lcurl-impersonate",
                ],
                "bend_native_sanitizer_additions": [
                    "-Wl,-rpath,<curl-library-dir>:<asan-runtime-dir>",
                    "-fsanitize=address,undefined", "-shared-libasan",
                ] if args.sanitize else [],
                "attribution_shim": [
                    "-std=c11", "-O2", "-g", "-Wall", "-Wextra", "-Werror",
                    "-fPIC", "-shared", "-I<CURL_PREFIX/include>", "-ldl", "-pthread",
                ],
                "sanitized_attribution_shim_additions": ["-fsanitize=address"] if args.sanitize else [],
            },
            "go_build_environment": {key: go_env[key] for key in (
                "GO111MODULE", "GOTOOLCHAIN", "GOPROXY", "CGO_ENABLED", "GOENV",
                "GOFLAGS", "GOOS", "GOARCH", "GOAMD64", "GOEXPERIMENT", "GOCACHE")},
            "dependencies": json.loads((ROOT / "dependencies.json").read_text()),
            "curl_cffi_binding": binding_info,
            "backend": {"path": str(backend_path), "library_path": str(library_path),
                        "sha256": backend_sha, "selection": "pinned .deps/curl stock backend"},
            "source_sha256": source_hashes,
            "binary_sha256": binary_hashes,
            "built_by_this_run": True,
            "sanitize_bend_smoke": bool(args.sanitize),
            "sanitizer_coverage": {
                "both Bend variants/native source/embedded bridges": "ASan/UBSan instrumented" if args.sanitize else "not instrumented",
                "bend_preloaded_shim": "AddressSanitizer instrumented" if args.sanitize else "not instrumented",
                "matched_python_and_its_shim": "not instrumented",
            },
            "source_and_binary_hashes_frozen_before_first_control": True,
            "source_hashes_checked_after_samples": False,
            "compiler_inputs_checked_after_samples": False,
            "binding_inputs_checked_after_samples": False,
        })
        artifact.write_manifest()
        body_path = artifact.output_dir / "corpus-body.bin"
        write_bytes_exclusive(body_path, bytes((index * 31) % 128 for index in range(SIZE - 8)))
        artifact.manifest["corpus"]["body_sha256"] = phase.digest(body_path)
        artifact.manifest["corpus"]["body_path"] = body_path.name
        artifact.write_manifest()
        asan_runtime = None
        if args.sanitize:
            candidate = phase.command_output([clang_path, "-print-file-name=libclang_rt.asan-x86_64.so"])
            if candidate and Path(candidate).is_file():
                asan_runtime = Path(candidate).resolve()
            else:
                raise RuntimeError("could not locate Clang ASan runtime for LD_PRELOAD ordering")
            for client, binary in bend_binaries.items():
                linkage = phase.command_output(["ldd", str(binary)])
                if str(asan_runtime) not in linkage:
                    raise RuntimeError(f"sanitized {client} executable does not link the expected shared ASan runtime")
            artifact.manifest["asan_runtime"] = {
                "path": str(asan_runtime), "sha256": phase.digest(asan_runtime),
                "linkage": "same shared runtime is a Bend dependency and is preloaded once before the sanitized shim",
            }
            artifact.write_manifest()
        cert_dir = build_dir / "tls"
        cert_dir.mkdir(exist_ok=False)
        _, ca = phase.certificate(cert_dir)
        key = cert_dir / "key.pem"
        artifact.manifest["tls_certificate_sha256"] = phase.digest(ca)
        artifact.manifest["tls_private_key_retained"] = False
        artifact.write_manifest()
        peer, peer_url, cpu_path, trace_path = start_peer(peer_binary, ca, key, artifact.output_dir)
        artifact.manifest["peer_url"] = peer_url
        artifact.manifest["profile_paths"] = {"cpu": str(cpu_path), "trace": str(trace_path)}
        artifact.write_manifest()
        positives = [item for item in plan if item["kind"] != "negative_correctness_control"]
        for item in plan:
            artifact.manifest["execution_order"].append(item["run_id"])
            artifact.write_manifest()
            if item["kind"] != "negative_correctness_control" and not profile_started:
                # The peer's profile window contains only the fixed positive schedule.
                ack = control_reply(peer, f"profile-start {len(positives)}")
                if ack.get("ok") is not True or ack.get("expected_profile_runs") != len(positives):
                    raise RuntimeError(f"peer rejected planned profile start: {ack}")
                profile_started = True
            selected_shim = shim_sanitized if args.sanitize and item["client"] in BEND_CLIENTS else shim_binary
            run_attempt(artifact, item, peer, peer_url, bend_binaries, selected_shim,
                        env, ca, body_path, backend_sha, asan_runtime, args.sanitize)
        stop_reply = control_reply(peer, "profile-stop", timeout=90)
        artifact.manifest["peer_profile_summary"] = stop_reply.get("profile")
        artifact.manifest["peer_profile_stop_reply"] = stop_reply
        if stop_reply.get("ok") is not True:
            raise RuntimeError(f"peer did not finalize CPU profile/trace cleanly: {stop_reply}")
        profile_started = False
        summary = stop_reply.get("profile")
        if not isinstance(summary, dict) or summary.get("cpu_profile_parseable") is not True or \
           summary.get("trace_parseable") is not True:
            raise RuntimeError("peer CPU profile or runtime trace is not parseable")
        if not smoke and summary.get("cpu_sample_floor_met") is not True:
            artifact.manifest["attribution_interpretation"] = "inconclusive: at least one peer pprof label is below the predeclared 80-sample floor"
        rows = validate_completed_plan(artifact, plan)
        if smoke:
            artifact.manifest["correctness_smoke_positive_count"] = sum(
                row["kind"] == "correctness_smoke" for row in rows)
            artifact.manifest["profile_floor_applicable"] = False
        else:
            artifact.manifest["positive_count"] = sum(row["kind"] == "attribution_sample" for row in rows)
            artifact.manifest["profile_floor_applicable"] = True
            artifact.manifest["per_cell_cpu_sample_counts"] = summary.get("cpu_samples_by_label")
        if peer.poll() is None:
            shutdown = control_reply(peer, "shutdown")
            if shutdown.get("ok") is not True:
                raise RuntimeError(f"peer did not shut down cleanly: {shutdown}")
        peer_exit = peer.wait(timeout=10)
        if peer_exit != 0:
            raise RuntimeError(f"peer exited with status {peer_exit}")
        if hash_sources() != source_hashes:
            raise RuntimeError("diagnostic source changed after hashes were frozen")
        frozen_binaries = [
            *[("bend_" + client, binary) for client, binary in bend_binaries.items()],
            *[("generated_" + client, path) for client, path in generated_c.items()],
            ("shim", shim_binary), ("peer", peer_binary),
        ]
        if shim_sanitized:
            frozen_binaries.append(("shim_sanitized", shim_sanitized))
        for key_name, path in frozen_binaries:
            if phase.digest(path) != binary_hashes[key_name]:
                raise RuntimeError(f"diagnostic {key_name} changed after hashes were frozen")
        if phase.digest(backend_path) != backend_sha:
            raise RuntimeError("pinned stock curl backend changed after hashes were frozen")
        if phase.compiler_provenance() != compiler_info:
            raise RuntimeError("Bend compiler/Bun inputs changed after hashes were frozen")
        if phase.curl_binding_provenance(env) != binding_info:
            raise RuntimeError("curl_cffi binding, matched Python, or package version inputs changed after hashes were frozen")
        if (phase.executable_provenance(clang_path) != clang_info or
            phase.executable_provenance(go_path, ("version",)) != go_info):
            raise RuntimeError("Clang or Go executable changed after hashes were frozen")
        if (phase.clang_resource_provenance(clang_path) != clang_resource_info or
            phase.go_build_input_provenance(go_path, PEER_SOURCE, go_env) != go_inputs_info):
            raise RuntimeError("Clang builtin headers or Go build inputs changed after hashes were frozen")
        if phase.directory_file_provenance(CURL_PREFIX / "include") != curl_headers_info:
            raise RuntimeError("stock curl include headers changed after hashes were frozen")
        if args.sanitize and phase.digest(asan_runtime) != artifact.manifest["asan_runtime"]["sha256"]:
            raise RuntimeError("ASan runtime changed after hashes were frozen")
        artifact.manifest["source_hashes_checked_after_samples"] = True
        artifact.manifest["compiler_inputs_checked_after_samples"] = True
        artifact.manifest["binding_inputs_checked_after_samples"] = True
        artifact.manifest["status"] = "complete"
        artifact.manifest["completed_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        artifact.manifest["completed_sample_count"] = len(rows)
        artifact.write_manifest()
    except BaseException as error:
        artifact.manifest["status"] = "failed"
        artifact.manifest["failure"] = f"{type(error).__name__}: {error}"
        artifact.manifest["failed_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        artifact.write_manifest()
        raise
    finally:
        if peer is not None:
            try:
                if peer.poll() is None and profile_started:
                    reply = control_reply(peer, "profile-stop", timeout=90)
                    artifact.manifest["failure_profile_stop_reply"] = reply
                if peer.poll() is None:
                    peer.stdin.write("shutdown\n")
                    peer.stdin.flush()
                    peer.wait(timeout=10)
            except BaseException:
                if peer.poll() is None:
                    peer.kill()
                    peer.wait()
                artifact.manifest["peer_shutdown_error"] = "peer cleanup required forced termination"
        artifact.write_manifest()
        artifact.close()
    print(artifact.output_dir)


if __name__ == "__main__":
    main()
