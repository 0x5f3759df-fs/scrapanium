#!/usr/bin/env python3
"""Bounded attribution diagnostic for the 64 KiB WSS workload.

The full schedule is fixed at 48 balanced blocks across eight client/mode/batch
cells (384 positive runs), after 16 small fault controls. `--smoke` runs eight
message correctness controls only; neither mode is throughput evidence.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
phase = importlib.import_module("websocket_phase_diagnostic")

BEND_CLIENT = ROOT / "benchmarks/websocket_attribution.bend"
PYTHON_CLIENT = ROOT / "benchmarks/websocket_attribution_python.py"
CLOCK_SOURCE = ROOT / "benchmarks/websocket_attribution_clock.c"
SHIM_SOURCE = ROOT / "benchmarks/websocket_attribution_shim.c"
PEER_SOURCE = ROOT / "benchmarks/websocket_attribution_peer.go"
MATCHED_PYTHON = ROOT / ".deps/venv-matched/bin/python"
SIZE = 65536
PERFORMANCE_COUNT = 1024
SMOKE_COUNT = 8
REPEATS = 48
SAMPLE_FLOOR = 80
CLIENTS = ("scrapanium-bend", "curl_cffi-matched")
MODES = ("control", "attribution")
BATCHES = (1, 64)
FAULTS = ("corrupt", "swap")
CELLS = tuple((client, mode, batch)
              for batch in BATCHES for client in CLIENTS for mode in MODES)


def source_paths():
    fixed = [
        ROOT / "dependencies.json", ROOT / "scrapanium.bend", ROOT / "http.bend",
        ROOT / "scripts/build.py", ROOT / "tests/lab.py",
        ROOT / "benchmarks/websocket.bend", ROOT / "benchmarks/websocket.py",
        ROOT / "benchmarks/websocket_phase_diagnostic.py",
        ROOT / "benchmarks/websocket_phase_diagnostic.bend",
        ROOT / "benchmarks/websocket_phase_diagnostic_python.py",
        ROOT / "benchmarks/websocket_phase_clock.c",
        BEND_CLIENT, PYTHON_CLIENT, CLOCK_SOURCE, SHIM_SOURCE, PEER_SOURCE,
        Path(__file__).resolve(),
    ]
    native = sorted((ROOT / "native").glob("*"))
    return [path for path in [*fixed, *native] if path.is_file()]


def hash_sources():
    return {str(path.relative_to(ROOT)): phase.digest(path) for path in source_paths()}


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
        raise RuntimeError("fixed schedule does not contain 48 complete repeat blocks")
    for start in range(0, REPEATS, len(CELLS)):
        rows = schedule[start:start + len(CELLS)]
        positions = [[0] * len(CELLS) for _ in CELLS]
        for row in rows:
            if sorted(row) != list(range(len(CELLS))):
                raise RuntimeError("schedule row is not a permutation of all eight cells")
            for position, condition in enumerate(row):
                positions[condition][position] += 1
        if any(value != 1 for row in positions for value in row):
            raise RuntimeError("eight-repeat rotation block is not position-balanced")
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
        raise FileExistsError(f"refusing to overwrite diagnostic build directory: {build_dir}")
    build_dir.mkdir(parents=True, exist_ok=False)
    bend_binary = build_dir / ("bench-ws-attribution-asan" if args.sanitize
                               else "bench-ws-attribution")
    shim_binary = build_dir / "libwsattribution.so"
    shim_sanitized = build_dir / "libwsattribution-asan.so" if args.sanitize else None
    peer_binary = build_dir / "websocket-attribution-peer"
    compiler = shutil.which("clang")
    if not compiler:
        raise FileNotFoundError("clang is required to build the diagnostic shim")
    if args.sanitize:
        generated = bend_binary.with_suffix(".generated.c")
        subprocess.run(phase.compiler() + [str(BEND_CLIENT), "-o", str(generated)], check=True)
        curl = (ROOT / ".deps/curl").resolve()
        curl_lib = curl / "lib" if (curl / "lib/libcurl-impersonate.so").exists() else curl
        asan_runtime = Path(phase.command_output([
            compiler, "-print-file-name=libclang_rt.asan-x86_64.so",
        ])).resolve()
        if not asan_runtime.is_file():
            raise RuntimeError("Clang shared ASan runtime is unavailable")
        runpath = f"{curl_lib}:{asan_runtime.parent}"
        subprocess.run([
            compiler, str(generated), str(ROOT / "native/scrapanium.c"),
            "-std=c11", "-O2", "-g", "-I" + str(curl / "include"),
            "-I" + str(ROOT / "native"), "-lpthread", "-lm",
            "-L" + str(curl_lib), "-Wl,-rpath," + runpath,
            "-lcurl-impersonate", "-fsanitize=address,undefined", "-shared-libasan",
            "-o", str(bend_binary),
        ], check=True)
    else:
        phase.build(BEND_CLIENT, bend_binary, sanitize=False)
    curl_include = ROOT / ".deps/curl/include"
    shim_command = [
        compiler, "-std=c11", "-O2", "-g", "-Wall", "-Wextra", "-Werror",
        "-fPIC", "-shared", "-I" + str(curl_include), str(SHIM_SOURCE),
        "-ldl", "-pthread",
    ]
    subprocess.run([*shim_command, "-o", str(shim_binary)], check=True)
    if shim_sanitized:
        subprocess.run([*shim_command, "-fsanitize=address",
                        "-o", str(shim_sanitized)], check=True)
    subprocess.run(["go", "build", "-trimpath", "-o", str(peer_binary),
                    str(PEER_SOURCE)], check=True)
    return bend_binary, shim_binary, shim_sanitized, peer_binary


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


def run_client(client, mode, bend_binary, shim_binary, url, ca, body_path,
               count, env, record_path, asan_runtime=None, timeout=120):
    command = ([str(bend_binary), "--threads", "1"] if client == CLIENTS[0] else
               [str(MATCHED_PYTHON), str(PYTHON_CLIENT)])
    child_env = {
        **env,
        "SCRAPANIUM_BENCH_URL": url,
        "SCRAPANIUM_BENCH_CA": ca,
        "SCRAPANIUM_BENCH_COUNT": str(count),
        "SCRAPANIUM_BENCH_BODY": str(body_path),
        "SCRAPANIUM_ATTRIBUTION_MODE": mode,
        "SCRAPANIUM_ATTRIBUTION_RECORD": str(record_path),
    }
    preload = [str(shim_binary)]
    if client == CLIENTS[0] and asan_runtime:
        preload.insert(0, str(asan_runtime))
    child_env["LD_PRELOAD"] = ":".join(preload)
    if client == CLIENTS[0] and asan_runtime:
        child_env["ASAN_OPTIONS"] = "abort_on_error=1:halt_on_error=1:detect_leaks=1"
        child_env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    process = None
    parent_gate_ns = None
    backend = None
    launch_error = None
    output = errors = ""
    returncode = None
    collected = False
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=child_env,
        )
        ready = client_line(process, timeout=45)
        if ready != "ready":
            raise RuntimeError(f"invalid client readiness line: {ready!r}")
        backend = phase.mapped_backend(process)
        parent_gate_ns = time.monotonic_ns()
        process.stdin.write("x")
        process.stdin.flush()
        output, errors = process.communicate(timeout=timeout)
        collected = True
        returncode = process.returncode
    except BaseException as error:
        launch_error = f"{type(error).__name__}: {error}"
    finally:
        if process is not None and not collected:
            if process.poll() is None:
                process.kill()
            try:
                tail, err_tail = process.communicate(timeout=5)
                output = tail
                errors = err_tail
            except BaseException as collect_error:
                launch_error = launch_error or f"output collection failed: {collect_error}"
            returncode = process.returncode
    parent_done_ns = time.monotonic_ns()
    shim_record = None
    shim_record_error = None
    try:
        shim_record = record_for_run(record_path)
    except BaseException as error:
        shim_record_error = f"{type(error).__name__}: {error}"
    return {
        "returncode": returncode,
        "stdout": output,
        "stderr": errors,
        "mapped_backend": backend,
        "parent_gate_monotonic_ns": parent_gate_ns,
        "parent_done_monotonic_ns": parent_done_ns,
        "launch_error": launch_error,
        "shim_record": shim_record,
        "shim_record_error": shim_record_error,
        "shim_record_path": str(record_path),
        "shim_record_sha256": phase.digest(record_path) if Path(record_path).is_file() else None,
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


def validate_shim_positive(record, result, mode, expected_bytes):
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
    curl = record.get("curl_ws_recv")
    if not isinstance(curl, dict):
        return errors + ["curl_ws_recv accounting is missing"]
    if curl.get("timing_enabled") != (mode == "attribution"):
        errors.append("curl_ws_recv timing mode disagrees with the planned client mode")
    if type(curl.get("calls")) is not int or curl["calls"] < 1:
        errors.append("curl_ws_recv was not interposed")
    elif type(curl.get("ok")) is not int or type(curl.get("curle_again")) is not int or \
         type(curl.get("other")) is not int or \
         curl["calls"] != curl["ok"] + curl["curle_again"] + curl["other"]:
        errors.append("curl_ws_recv result counts do not sum to calls")
    if curl.get("other") != 0:
        errors.append("curl_ws_recv returned an unexpected error")
    if curl.get("bytes") != expected_bytes:
        errors.append(f"curl_ws_recv successful bytes were {curl.get('bytes')!r}, expected {expected_bytes}")
    for field in ("wall_sum_across_threads_ns", "thread_cpu_sum_across_threads_ns"):
        value = curl.get(field)
        if type(value) is not int or value < 0:
            errors.append(f"curl_ws_recv {field} is invalid")
        elif mode == "control" and value != 0:
            errors.append(f"control mode unexpectedly timed curl_ws_recv ({field})")
        elif mode == "attribution" and value == 0:
            errors.append(f"attribution mode has no curl_ws_recv {field}")
    threads = record.get("threads")
    if not isinstance(threads, list) or not threads:
        errors.append("shim per-thread records are missing")
    else:
        recv_totals = {"calls": 0, "curle_again": 0, "ok": 0, "other": 0, "bytes": 0,
                       "wall_sum_across_threads_ns": 0,
                       "thread_cpu_sum_across_threads_ns": 0}
        wait_totals = {name: {key: 0 for key in (
            "calls", "ready", "timeout", "error", "wall_sum_across_threads_ns",
            "thread_cpu_sum_across_threads_ns")} for name in ("poll", "ppoll", "select", "epoll")}
        tids = set()
        for thread in threads:
            if not isinstance(thread, dict):
                errors.append("shim per-thread record is malformed")
                continue
            tid = thread.get("tid")
            if type(tid) is not int or tid <= 0 or tid in tids:
                errors.append("shim per-thread TID is invalid or duplicated")
            else:
                tids.add(tid)
            recv_names = {
                "curl_ws_recv_calls": "calls",
                "curl_ws_recv_curle_again": "curle_again",
                "curl_ws_recv_ok": "ok",
                "curl_ws_recv_other": "other",
                "curl_ws_recv_bytes": "bytes",
                "curl_ws_recv_wall_sum_ns": "wall_sum_across_threads_ns",
                "curl_ws_recv_thread_cpu_sum_ns": "thread_cpu_sum_across_threads_ns",
            }
            for source, target in recv_names.items():
                value = thread.get(source)
                if type(value) is not int or value < 0:
                    errors.append(f"per-thread curl_ws_recv {source} is invalid")
                else:
                    recv_totals[target] += value
            if thread.get("curl_ws_recv_tid_mismatch") != 0:
                errors.append("curl_ws_recv clock crossed OS threads")
            if thread.get("curl_ws_recv_wall_sum_ns", 0) < thread.get("curl_ws_recv_thread_cpu_sum_ns", 0):
                errors.append("curl_ws_recv thread CPU sum exceeds nested wall sum")
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
        curl_fields = record["curl_ws_recv"]
        for key, value in recv_totals.items():
            if curl_fields.get(key) != value:
                errors.append(f"aggregate curl_ws_recv {key} disagrees with per-thread sum")
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
        schedule = balanced_schedule(seed)
        for repeat, row in enumerate(schedule, start=1):
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
        raise RuntimeError("planned diagnostic schedule contains duplicate run IDs")
    expected_positives = 8 if smoke else 384
    if len(negatives) != 16 or len(positives) != expected_positives:
        raise RuntimeError(f"planned {len(negatives)} negatives and {len(positives)} positives")
    expected_negative_cells = {
        (client, mode, batch, fault)
        for client in CLIENTS for mode in MODES for batch in BATCHES for fault in FAULTS
    }
    actual_negative_cells = {
        (item["client"], item["mode"], item["batch"], item["fault"])
        for item in negatives
    }
    if actual_negative_cells != expected_negative_cells:
        raise RuntimeError("negative plan does not cover both faults across all client/mode/batch cells")
    expected_per_cell = 1 if smoke else REPEATS
    positive_cells = {
        (item["client"], item["mode"], item["batch"]): 0 for item in positives
    }
    for item in positives:
        key = (item["client"], item["mode"], item["batch"])
        positive_cells[key] += 1
    if set(positive_cells) != set(CELLS) or any(
        count_for_cell != expected_per_cell for count_for_cell in positive_cells.values()
    ):
        raise RuntimeError("positive schedule does not contain the planned exact count per cell")
    if not smoke:
        for repeat in range(1, REPEATS + 1):
            block = [item for item in positives if item["repeat"] == repeat]
            if {(item["client"], item["mode"], item["batch"]) for item in block} != set(CELLS):
                raise RuntimeError(f"repeat block {repeat} does not contain all eight cells exactly once")
    return plan


def run_attempt(artifact, plan_item, peer, peer_url, bend_binary, shim_binary,
                env, ca, body_path, backend_sha, asan_runtime, sanitize_bend):
    sample = dict(plan_item)
    sample["sanitizer_coverage"] = (
        "Bend executable and embedded clock bridge are ASan/UBSan instrumented; preloaded shim is AddressSanitizer-instrumented; "
        "matched Python executable and its shim are uninstrumented"
        if sanitize_bend and plan_item["client"] == CLIENTS[0]
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
            plan_item["client"], plan_item["mode"], bend_binary, shim_binary,
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
            errors.extend(validate_shim_positive(sample["shim"], client_result,
                                                 plan_item["mode"], plan_item["count"] * SIZE))
            interval = sample["shim"].get("interval") if isinstance(sample["shim"], dict) else None
            interval = interval if isinstance(interval, dict) else {}
            sample["client_interval_wall_ns"] = interval.get("wall_ns")
            sample["caller_thread_cpu_ns"] = interval.get("caller_thread_cpu_ns")
            sample["process_cpu_ns"] = interval.get("process_cpu_ns")
            sample["curl_ws_recv"] = sample["shim"].get("curl_ws_recv") if isinstance(sample["shim"], dict) else None
            sample["wait_calls"] = sample["shim"].get("waits") if isinstance(sample["shim"], dict) else None
            sample["client_wall_minus_thread_cpu_ns"] = (
                sample["client_interval_wall_ns"] - sample["caller_thread_cpu_ns"]
                if type(sample["client_interval_wall_ns"]) is int and
                type(sample["caller_thread_cpu_ns"]) is int else None
            )
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
        parser.error(f"matched Python environment is missing: {MATCHED_PYTHON}")
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
        "positive_count": 8 if smoke else 384,
        "negative_control_count": 16,
        "cpu_profile_sample_floor_per_label": SAMPLE_FLOOR,
        "cpu_profile_floor_policy": "if any label is below 80 samples, report attribution inconclusive; never extend or repeat the run",
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
            "caller_cpu_residual": "client caller-thread CPU minus measured curl_ws_recv call CPU includes validation, releases, assembly, Bend/Python wrapper work, start-control send, and shim bookkeeping; it is not pure wrapper CPU and must not be combined with phase fractions from a different run",
            "curl_ws_recv_scope": "per-call timing starts immediately before the real backend call and stops immediately after it; interposer bookkeeping is included only in client total clocks",
            "wait_wrappers": "counts and, in attribution mode, nested wall/thread-CPU sums for poll, ppoll, select, and epoll_wait only; other waits/syscalls are not covered, and wait intervals can be nested inside curl_ws_recv",
            "wall_minus_thread_cpu": "an unaccounted/off-CPU wall gap that can include scheduling and clock-boundary overhead; it is not automatically socket-blocked time",
            "control_mode": "same shim, exact validation/release, client wall/thread/process clocks, and cheap call/result counters; skips per-call recv/wait clock reads but still pays common interposition, mutex/atomic bookkeeping, and the peer CPU profiler/trace",
            "observer_effect": "control versus attribution estimates only the incremental per-call client timing clocks in this instrumented skeleton; peer profiling remains enabled and its observer effect is not isolated",
            "peer_metrics": "peer send interval, TLS Conn.Write wall sum, whole-process user/system CPU, Go CPU profile and runtime trace are separate metrics; peer wall/CPU/profile intervals are not additive to client intervals",
            "pprof_floor": "Go CPU pprof samples are grouped by client/mode/batch inside the peer send loop; a cell below the predeclared 80-sample floor is inconclusive with no adaptive extension",
        },
        "corpus": {
            "messages": "ordered binary payloads exactly 65,536 bytes each",
            "sequence": "zero-padded eight-digit decimal at bytes 0..7; exact complete payload comparison on every response",
            "body": "remaining bytes repeat the fixed deterministic (offset * 31) mod 128 pattern",
            "batches": list(BATCHES),
            "warmup": "one binary warmup echoed, opcode and exact bytes checked before the interval",
        },
        "negative_controls": "16 count-eight runs crossing both clients, modes, flush batches, and corrupt/swap sequence faults, all before profile-start",
        "schedule_method": "fixed 48 complete eight-cell repeat blocks across both batch sizes; each block cyclically rotates all eight cells so each cell occupies each position once; this balances position but not carryover; no adaptive extension or rerun",
        "planned_schedule": plan,
        "execution_order": [],
        "source_sha256": {},
        "binary_sha256": {},
        "samples_file": "samples.jsonl",
        "profiles": {"cpu": "cpu.prof", "trace": "trace.out"},
    })
    peer = None
    profile_started = False
    try:
        stock_prefix = (ROOT / ".deps/curl").resolve()
        env, library_path, backend_path = phase.make_env(stock_prefix)
        env.pop("ASAN_OPTIONS", None)
        env.pop("UBSAN_OPTIONS", None)
        if not backend_path.is_file():
            raise FileNotFoundError(f"pinned stock backend is missing: {backend_path}")
        source_before_build = hash_sources()
        build_dir = ROOT / "build/attribution-diagnostic" / name
        bend_binary, shim_binary, shim_sanitized, peer_binary = build_artifacts(args, build_dir)
        source_hashes = hash_sources()
        if source_before_build != source_hashes:
            raise RuntimeError("diagnostic source changed during build")
        backend_sha = phase.digest(backend_path)
        compiler_info = phase.compiler_provenance()
        binding_info = phase.curl_binding_provenance(env)
        expected_binding = json.loads((ROOT / "dependencies.json").read_text())["curl_cffi_matched"]["version"]
        if binding_info["version"] != expected_binding:
            raise RuntimeError(f"curl_cffi version {binding_info['version']!r} does not match pin {expected_binding}")
        clang_path = shutil.which("clang")
        clang_version = phase.command_output([clang_path, "--version"])
        go_version = phase.command_output(["go", "version"])
        binary_hashes = {
            "bend_client": phase.digest(bend_binary),
            "shim": phase.digest(shim_binary),
            "peer": phase.digest(peer_binary),
        }
        if shim_sanitized:
            binary_hashes["shim_sanitized"] = phase.digest(shim_sanitized)
        generated = bend_binary.with_suffix(".generated.c")
        binary_hashes["generated_bend_c"] = phase.digest(generated)
        artifact.manifest.update({
            "platform": platform.platform(), "python": sys.version,
            "cpu_count": os.cpu_count(), "cpu_model": phase.cpu_model(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "compiler": compiler_info, "clang_path": clang_path,
            "clang_version": clang_version, "go_version": go_version,
            "dependencies": json.loads((ROOT / "dependencies.json").read_text()),
            "curl_cffi_binding": binding_info,
            "backend": {"path": str(backend_path), "library_path": str(library_path),
                        "sha256": backend_sha, "selection": "pinned .deps/curl stock backend"},
            "source_sha256": source_hashes,
            "binary_sha256": binary_hashes,
            "built_by_this_run": True,
            "sanitize_bend_smoke": bool(args.sanitize),
            "sanitizer_coverage": {
                "bend_and_embedded_bridge": "ASan/UBSan instrumented" if args.sanitize else "not instrumented",
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
            linkage = phase.command_output(["ldd", str(bend_binary)])
            if str(asan_runtime) not in linkage:
                raise RuntimeError("sanitized Bend executable does not link the expected shared ASan runtime")
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
            selected_shim = shim_sanitized if args.sanitize and item["client"] == CLIENTS[0] else shim_binary
            run_attempt(artifact, item, peer, peer_url, bend_binary, selected_shim,
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
        frozen_binaries = [("bend_client", bend_binary), ("shim", shim_binary),
                           ("peer", peer_binary), ("generated_bend_c", generated)]
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
            raise RuntimeError("curl_cffi binding or matched Python inputs changed after hashes were frozen")
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
