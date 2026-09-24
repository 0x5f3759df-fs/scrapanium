#!/usr/bin/env python3
"""Fixed-schedule, diagnostic-only user CPU profiles for exact WSS streams.

The explicit ``--smoke`` mode validates the clients, perf control channel,
peer counters, and retained profile artifacts with short messages. The full
768-positive/16-negative plan requires ``--full`` and is intentionally fixed;
it never extends the schedule in response to observed sample counts.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "scripts"), str(ROOT / "benchmarks")]
from lab import certificate

CLIENTS = ("scrapanium-bend", "curl_cffi-matched")
SAMPLING = ("disabled", "profiled")
BATCHES = (1, 64)
FAULTS = ("corrupt", "swap")
SIZE = 65536
FULL_COUNT = 1024
SMOKE_COUNT = 8
FULL_REPEATS = 96
SMOKE_REPEATS = 4
SAMPLE_FLOOR = 80
SCHEDULE_SEED = 20260923
BODY_FILE = ROOT / "benchmarks/websocket_client_perf_stream.bend"
PYTHON_CLIENT = ROOT / "benchmarks/websocket_client_perf_stream_python.py"
CLOCK_SOURCE = ROOT / "benchmarks/websocket_client_perf_clock.c"
PEER_SOURCE = ROOT / "benchmarks/websocket_stream_server.go"
MATCHED_PYTHON = ROOT / ".deps/venv-matched/bin/python"
PERF_DEFAULT = Path(
    "/home/baidu/wss-perf-feasibility-20260923/extracted/usr/bin/perf"
)
PERF_LIB_DEFAULT = Path(
    "/home/baidu/wss-perf-feasibility-20260923/extracted/usr/lib/x86_64-linux-gnu"
)

# Williams rows for four conditions. Each block contains every treatment once
# at every position and every ordered non-identical adjacent pair once.
WILLIAMS_4 = (
    (0, 1, 3, 2),
    (1, 2, 0, 3),
    (2, 3, 1, 0),
    (3, 0, 2, 1),
)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path = Path(path)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    # Direct writes avoid rename failures on mounted Windows filesystems when
    # an external reader briefly opens the manifest during a smoke run.
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def append_jsonl(path: Path, value) -> None:
    with Path(path).open("ab") as stream:
        stream.write((json.dumps(value, sort_keys=True) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())


def condition_rows(seed: int, repeats: int) -> list[list[int]]:
    if repeats <= 0 or repeats % len(WILLIAMS_4):
        raise ValueError("repeat count must be a positive multiple of four")
    rng = random.Random(seed)
    labels = list(range(4))
    rng.shuffle(labels)
    base_rows = [tuple(labels[index] for index in row) for row in WILLIAMS_4]
    result = []
    for _ in range(repeats // 4):
        block = list(base_rows)
        rng.shuffle(block)
        result.extend([list(row) for row in block])
    validate_williams_rows(result)
    return result


def validate_williams_rows(rows: list[list[int]]) -> None:
    if not rows or len(rows) % 4:
        raise ValueError("schedule is not a complete four-row Williams block")
    expected = {0, 1, 2, 3}
    for start in range(0, len(rows), 4):
        block = rows[start:start + 4]
        if any(len(row) != 4 or set(row) != expected for row in block):
            raise ValueError("schedule row is not a four-condition permutation")
        positions = [[0] * 4 for _ in range(4)]
        transitions = collections.Counter()
        for row in block:
            for position, condition in enumerate(row):
                positions[condition][position] += 1
            transitions.update(zip(row, row[1:]))
        if any(count != 1 for condition in positions for count in condition):
            raise ValueError("Williams block does not balance positions")
        if any(transitions[(left, right)] != 1 for left in expected
               for right in expected if left != right):
            raise ValueError("Williams block does not balance adjacent transitions")


def make_plan(mode: str, seed: int = SCHEDULE_SEED) -> list[dict]:
    if mode not in ("smoke", "full"):
        raise ValueError("mode must be smoke or full")
    repeats = SMOKE_REPEATS if mode == "smoke" else FULL_REPEATS
    count = SMOKE_COUNT if mode == "smoke" else FULL_COUNT
    conditions = [(client, sampling) for client in CLIENTS for sampling in SAMPLING]
    plan = []
    serial = 0
    for batch_index, batch in enumerate(BATCHES):
        rows = condition_rows(seed + batch_index * 0x9E3779B1, repeats)
        for repeat, row in enumerate(rows, 1):
            for position, condition_index in enumerate(row, 1):
                client, sampling = conditions[condition_index]
                serial += 1
                plan.append({
                    "kind": "positive",
                    "run_id": f"positive-{serial:04d}-flush{batch}-repeat{repeat:03d}-position{position}",
                    "batch": batch,
                    "repeat": repeat,
                    "position": position,
                    "client": client,
                    "sampling": sampling,
                    "fault": "",
                    "count": count,
                })
    controls = []
    serial = 0
    for batch in BATCHES:
        for client in CLIENTS:
            for sampling in SAMPLING:
                for fault in FAULTS:
                    serial += 1
                    controls.append({
                        "kind": "negative_control",
                        "run_id": f"negative-{serial:02d}-flush{batch}-{client}-{sampling}-{fault}",
                        "batch": batch,
                        "repeat": 0,
                        "position": 0,
                        "client": client,
                        "sampling": sampling,
                        "fault": fault,
                        "count": SMOKE_COUNT,
                    })
    plan = controls + plan
    validate_plan(plan, mode)
    return plan


def validate_plan(plan: list[dict], mode: str) -> None:
    repeats = SMOKE_REPEATS if mode == "smoke" else FULL_REPEATS
    count = SMOKE_COUNT if mode == "smoke" else FULL_COUNT
    expected_ids = set()
    negative = [row for row in plan if row.get("kind") == "negative_control"]
    positive = [row for row in plan if row.get("kind") == "positive"]
    if len(negative) != 16 or len(positive) != repeats * len(BATCHES) * 4:
        raise ValueError("plan has the wrong fixed number of positive/negative rows")
    if plan != negative + positive:
        raise ValueError("all negative controls must precede profiled positives")
    expected_negative = {
        (batch, client, sampling, fault)
        for batch in BATCHES for client in CLIENTS
        for sampling in SAMPLING for fault in FAULTS
    }
    actual_negative = set()
    for row in negative:
        actual_negative.add((row["batch"], row["client"], row["sampling"], row["fault"]))
        if row["count"] != SMOKE_COUNT:
            raise ValueError("negative control count changed from the fixed small corpus")
    if actual_negative != expected_negative:
        raise ValueError("negative controls do not cover client/mode/batch/fault product")
    by_batch_repeat = collections.defaultdict(list)
    by_cell = collections.Counter()
    for row in positive:
        run_id = row["run_id"]
        if run_id in expected_ids:
            raise ValueError("duplicate run ID in fixed plan")
        expected_ids.add(run_id)
        if row["count"] != count or row["fault"]:
            raise ValueError("positive workload differs from fixed count/fault contract")
        if row["client"] not in CLIENTS or row["sampling"] not in SAMPLING:
            raise ValueError("positive condition is outside the fixed four-cell set")
        by_batch_repeat[(row["batch"], row["repeat"])].append(row)
        by_cell[(row["batch"], row["client"], row["sampling"])] += 1
    if len(expected_ids) != len(positive):
        raise ValueError("positive IDs are not unique")
    for batch in BATCHES:
        if set(repeat for row_batch, repeat in by_batch_repeat if row_batch == batch) != set(range(1, repeats + 1)):
            raise ValueError("positive plan is missing a repeat for one batch")
        for repeat in range(1, repeats + 1):
            rows = by_batch_repeat[(batch, repeat)]
            if len(rows) != 4 or {(row["client"], row["sampling"]) for row in rows} != {
                (client, sampling) for client in CLIENTS for sampling in SAMPLING
            }:
                raise ValueError("repeat does not contain each positive condition exactly once")
        for client in CLIENTS:
            for sampling in SAMPLING:
                if by_cell[(batch, client, sampling)] != repeats:
                    raise ValueError("positive cell count differs from fixed repeat count")


def parse_client_interval(text: str) -> dict:
    intervals = re.findall(r"^WSS_PERF_INTERVAL_NS=([0-9]+),([0-9]+)$", text, re.MULTILINE)
    elapsed_values = re.findall(r"^client_interval_elapsed_us=([0-9]+)$", text, re.MULTILINE)
    if len(intervals) != 1 or len(elapsed_values) != 1:
        raise ValueError("client must emit exactly one interval and elapsed marker")
    start_ns, end_ns = map(int, intervals[0])
    elapsed_us = int(elapsed_values[0])
    if start_ns <= 0 or end_ns <= start_ns or elapsed_us <= 0:
        raise ValueError("client emitted an invalid interval")
    if elapsed_us != (end_ns - start_ns) // 1000:
        raise ValueError("client elapsed microseconds disagree with nanosecond boundaries")
    return {"start_ns": start_ns, "end_ns": end_ns,
            "elapsed_ns": end_ns - start_ns, "elapsed_us": elapsed_us}


def validate_peer(record: dict, plan_row: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("peer did not retain a run record")
    count = plan_row["count"]
    expected_start = {"warmups": 1, "starts": 1}
    for key, value in expected_start.items():
        if record.get(key) != value:
            raise ValueError(f"peer {key}={record.get(key)!r}; expected {value}")
    frames = record.get("data_frames")
    payload_bytes = record.get("data_payload_bytes")
    frame_bytes = record.get("data_frame_bytes")
    if any(type(value) is not int for value in (frames, payload_bytes, frame_bytes)):
        raise ValueError("peer frame/payload counters are not integers")
    if plan_row["kind"] == "positive":
        if record.get("error"):
            raise ValueError(f"peer reported an error: {record['error']}")
        if (frames, payload_bytes, frame_bytes) != (count, count * SIZE, count * (SIZE + 10)):
            raise ValueError("peer positive frame/payload totals differ from the exact corpus")
    else:
        if not 0 <= frames <= count or payload_bytes != frames * SIZE or frame_bytes != frames * (SIZE + 10):
            raise ValueError("peer negative frame/byte counters are internally inconsistent")
    if not isinstance(record.get("tls_version"), int) or not isinstance(record.get("tls_cipher"), int):
        raise ValueError("peer did not report TLS version/cipher")
    return {key: record.get(key) for key in (
        "warmups", "starts", "data_frames", "data_payload_bytes", "data_frame_bytes",
        "tls_version", "tls_cipher", "error")}


def validate_client_result(plan_row: dict, returncode: int | None, output: str) -> dict:
    if plan_row["kind"] == "positive":
        if returncode != 0:
            raise ValueError(f"positive client exited with status {returncode}")
        return parse_client_interval(output)
    if returncode == 0:
        raise ValueError("faulted negative control unexpectedly passed")
    if "WebSocket opcode, sequence, or payload mismatch" not in output:
        raise ValueError("negative control failed for a reason other than exact data rejection")
    if "WSS_PERF_INTERVAL_NS=" in output:
        raise ValueError("negative control unexpectedly emitted a completed timer interval")
    return {"rejected_expected_corruption": True}


def resolve_perf(explicit: str | None) -> Path:
    value = explicit or os.environ.get("SCRAPANIUM_PERF")
    if value:
        result = Path(value).resolve()
    else:
        found = shutil.which("perf")
        result = Path(found).resolve() if found else PERF_DEFAULT
    if not result.is_file():
        raise FileNotFoundError("Linux perf is unavailable; pass --perf or set SCRAPANIUM_PERF")
    return result


def run_text(command: list[str], *, env=None, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, check=False)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command!r}\n{result.stderr}")
    return result


def read_line(process: subprocess.Popen, timeout: float = 30) -> str:
    if process.stdout is None:
        raise RuntimeError("process stdout is not captured")
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError(f"process {process.pid} did not produce a line")
    line = process.stdout.readline()
    if not line:
        errors = ""
        if process.stderr:
            ready, _, _ = select.select([process.stderr], [], [], 0)
            if ready:
                errors = process.stderr.read()
        raise RuntimeError(f"process {process.pid} closed stdout ({process.poll()}): {errors}")
    return line.rstrip("\r\n")


class PerfControl:
    def __init__(self, process: subprocess.Popen, ctl_fd: int, ack_fd: int):
        self.process = process
        self.ctl_fd = ctl_fd
        self.ack_fd = ack_fd
        self.buffer = b""
        self.transcript = []

    def command(self, value: str, timeout: float = 15) -> dict:
        if self.process.poll() is not None:
            raise RuntimeError(f"perf exited before {value} acknowledgement")
        raw = (value + "\n").encode("ascii")
        written = os.write(self.ctl_fd, raw)
        if written != len(raw):
            raise RuntimeError("short write to perf control FIFO")
        request_ns = time.monotonic_ns()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"perf exited before {value} acknowledgement")
            ready, _, _ = select.select([self.ack_fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.ack_fd, 4096)
                except BlockingIOError:
                    continue
                self.buffer += chunk
                tokens = re.split(rb"[\x00\n]", self.buffer)
                self.buffer = tokens.pop()
                ack_found = False
                for token in tokens:
                    normalized = token.strip()
                    if normalized == b"ack" and not ack_found:
                        ack_found = True
                    elif normalized:
                        decoded = token.decode("utf-8", errors="replace")
                        self.transcript.append({"unsolicited": decoded})
                if ack_found:
                    ack_ns = time.monotonic_ns()
                    entry = {"command": value, "request_monotonic_ns": request_ns,
                             "ack_monotonic_ns": ack_ns, "raw_ack": "ack"}
                    self.transcript.append(entry)
                    return entry
        raise TimeoutError(f"timed out waiting for perf {value} acknowledgement")


def task_tids(pid: int) -> list[int]:
    directory = Path(f"/proc/{pid}/task")
    return sorted(int(path.name) for path in directory.iterdir() if path.name.isdecimal())


def collect_client_output(process: subprocess.Popen, initial_tids: list[int],
                          timeout: float) -> tuple[str, str, list[int]]:
    deadline = time.monotonic() + timeout
    tids = set(initial_tids)
    while process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            stdout, stderr = process.communicate()
            raise TimeoutError(f"client did not finish within {timeout}s\n{stdout}\n{stderr}")
        try:
            tids.update(task_tids(process.pid))
        except FileNotFoundError:
            pass
        time.sleep(0.002)
    stdout, stderr = process.communicate(timeout=5)
    return stdout, stderr, sorted(tids)


def mapped_backend(pid: int, expected_library: Path) -> dict:
    matches = set()
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        fields = line.split()
        if fields and "libcurl-impersonate.so" in fields[-1]:
            name = fields[-1].removesuffix(" (deleted)")
            matches.add(Path(name).resolve())
    if len(matches) != 1:
        raise RuntimeError(f"expected one mapped curl backend for PID {pid}, got {matches}")
    path = matches.pop()
    mapped = {"path": str(path), "sha256": digest(path)}
    if path != expected_library.resolve() or mapped["sha256"] != digest(expected_library):
        raise RuntimeError(f"client did not map the frozen stock backend: {mapped}")
    maps = Path(f"/proc/{pid}/maps").read_text().lower()
    if any(marker in maps for marker in ("libasan", "libubsan", "liblsan")):
        raise RuntimeError("client unexpectedly mapped a sanitizer runtime")
    return mapped


def capture_perf(binary: Path, perf: Path, perf_lib: Path, attempt: Path,
                 pid: int, sampling: str) -> tuple[PerfControl, list[int], dict, list[int]]:
    control_path = attempt / "perf.ctl"
    ack_path = attempt / "perf.ack"
    data_path = attempt / "perf.data"
    command = [str(perf), "record", "--delay=-1", "--event=cpu-clock:u", "--freq=99",
               "--call-graph=dwarf,16384", "--clockid=monotonic", "--inherit",
               f"--control=fifo:{control_path},{ack_path}", "-p", str(pid),
               "-o", str(data_path)]
    perf_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LD_LIBRARY_PATH": str(perf_lib), "LANG": "C", "LC_ALL": "C"}
    ctl_fd = ack_fd = None
    process = None
    controller = None
    try:
        os.mkfifo(control_path)
        os.mkfifo(ack_path)
        ctl_fd = os.open(control_path, os.O_RDWR | os.O_NONBLOCK)
        ack_fd = os.open(ack_path, os.O_RDWR | os.O_NONBLOCK)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=perf_env)
        controller = PerfControl(process, ctl_fd, ack_fd)
        ping = controller.command("ping")
        if sampling == "profiled":
            armed = controller.command("enable")
        else:
            armed = controller.command("disable")
        tids = task_tids(pid)
        if not tids or pid not in tids:
            raise RuntimeError(f"client TID snapshot is incomplete: pid={pid}, tids={tids}")
        return controller, tids, {"command": command, "ping": ping, "arming": armed,
                                  "perf_env": perf_env, "data_path": str(data_path)}, [ctl_fd, ack_fd]
    except BaseException as error:
        stdout = stderr = ""
        if process is not None and process.poll() is None:
            try:
                if controller is not None:
                    controller.command("stop", timeout=2)
                else:
                    process.kill()
            except BaseException:
                process.kill()
        if process is not None:
            try:
                stdout, stderr = process.communicate(timeout=5)
            except BaseException:
                process.kill()
                stdout, stderr = process.communicate()
        (attempt / "perf-startup.stdout.txt").write_text(stdout)
        (attempt / "perf-startup.stderr.txt").write_text(stderr)
        (attempt / "perf-startup-failure.txt").write_text(
            f"{type(error).__name__}: {error}\ncommand={command!r}\n")
        (attempt / "perf-startup-controls.json").write_text(json.dumps(
            controller.transcript if controller else [], indent=2) + "\n")
        for fd in (ctl_fd, ack_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        for path in (control_path, ack_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def finish_perf(process: subprocess.Popen, fds: list[int], timeout: float = 60) -> dict:
    # Do not send disable/stop after the client exits: the recorder is attached
    # to that PID and naturally finalizes when it terminates.
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise TimeoutError("perf recorder did not exit with the target client")
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr}


def parser_module():
    import websocket_client_perf_parse
    return websocket_client_perf_parse


def decode_profile(perf: Path, perf_env: dict, data_path: Path,
                   pid: int, tids: list[int], interval: dict, sampling: str,
                   attempt: Path, full: bool) -> dict:
    parser = parser_module()
    header = run_text([str(perf), "report", "--header-only", "--stdio", "-i", str(data_path)],
                      env=perf_env, check=False)
    report = run_text([str(perf), "report", "--stdio", "--no-children", "--sort=symbol",
                       "--percent-limit=0", "-i", str(data_path)], env=perf_env, check=False)
    script = run_text([str(perf), "script", "--ns", "-F",
                       "pid,tid,time,event,ip,sym,dso,callindent", "-i", str(data_path)],
                      env=perf_env, check=False)
    (attempt / "perf.header.txt").write_text(header.stdout)
    (attempt / "perf.report.txt").write_text(report.stdout)
    (attempt / "perf.script-ns.txt").write_text(script.stdout)
    (attempt / "perf.header.stderr.txt").write_text(header.stderr)
    (attempt / "perf.report.stderr.txt").write_text(report.stderr)
    (attempt / "perf.script.stderr.txt").write_text(script.stderr)
    if any(result.returncode for result in (header, report, script)):
        raise RuntimeError("perf decode command failed; raw stdout/stderr retained")
    parsed_header = parser.parse_perf_header(header.stdout, report.stdout, script.stdout)
    parsed_script = parser.parse_perf_script(script.stdout)
    filtered = parser.filter_samples(
        parsed_script, pid, set(tids), interval["start_ns"], interval["end_ns"],
        expected_event="cpu-clock:u")
    event = parsed_header.get("event") or {}
    actual_clock = parsed_header.get("clock") or {}
    sample_type = event.get("sample_type") or []
    clock = parsed_header.get("clock") or {}
    sample_fields = {"IP", "TID", "TIME", "CALLCHAIN", "REGS_USER", "STACK_USER"}
    checks = {
        "event_name": event.get("name") in ("cpu-clock:u", "cpu-clock"),
        "event_type": event.get("type_num") == 1 and event.get("config") == 0,
        "frequency_mode": event.get("frequency_mode") is True,
        "frequency_hz": event.get("frequency_hz") == 99,
        "sample_fields": sample_fields.issubset(set(sample_type)),
        "sample_registers": bool(event.get("sample_regs_user")),
        "user_stack_bytes": event.get("sample_stack_user_bytes") == 16384,
        "clock_id": str(event.get("clockid", "")).lower()
                    in ("monotonic", "clock_monotonic", "1") and
                    actual_clock.get("id") in (1, "1") and
                    str(actual_clock.get("name", "")).lower() in ("monotonic", "clock_monotonic"),
        "use_clockid": event.get("use_clockid") is True,
        "inherit": event.get("inherit") is True,
        "kernel_excluded": event.get("exclude_kernel") is True,
        "hypervisor_excluded": event.get("exclude_hv") is True,
        "dwarf_callchain_register_filter": event.get("exclude_callchain_user") is True,
        "one_event": parsed_header.get("events") == 1,
        "all_perf_lines_parsed": parsed_script.get("attribution_valid") is True
                                  and not parsed_script.get("malformed_lines"),
        "window_attribution_valid": filtered.get("attribution_valid") is True
                                    and filtered.get("in_window_identity_mismatch_count") == 0
                                    and filtered.get("wrong_event_sample_count") == 0,
    }
    if not all(checks.values()):
        raise ValueError(f"perf artifact failed its actual-header/parser contract: {checks}")
    if sampling == "disabled" and parsed_script.get("sample_count") != 0:
        raise ValueError("disabled perf control unexpectedly contains CPU samples")
    if sampling == "profiled" and full and parsed_script.get("sample_count") == 0:
        # A full per-row zero is retained; the predeclared client/batch floor is
        # evaluated only after the complete fixed plan, without extension.
        zero_sample_status = "zero_for_this_profiled_run"
    else:
        zero_sample_status = None
    if parsed_header.get("lost_samples") not in (None, 0):
        raise ValueError(f"perf reports lost samples: {parsed_header.get('lost_samples')}")
    if parsed_header.get("throttle_records") not in (None, 0):
        raise ValueError(f"perf reports throttling: {parsed_header.get('throttle_records')}")

    selected = filtered["selected"]
    leaf_counts = collections.Counter()
    inclusive = collections.Counter()
    memberships = {
        "curl_websocket": ("curl_ws_recv", "ws_dec_pass", "ws_recv"),
        "tls_read_crypto": ("SSL_read", "ssl_read_impl", "ssl_handle_open_record",
                            "EVP_AEAD_CTX_open", "aes_gcm", "aesni"),
        "scrapanium_websocket": ("sp_ws_io_advance", "sp_ws_append", "sp_bend_ws_receive"),
    }
    for sample in selected:
        leaf_counts[sample.get("leaf_symbol") or "[unknown]"] += 1
        frames = sample.get("frames") or []
        stack = " ".join(str(frame.get("symbol", "")) if isinstance(frame, dict)
                         else str(frame) for frame in frames).lower()
        for name, needles in memberships.items():
            if any(needle.lower() in stack for needle in needles):
                inclusive[name] += 1
    unknown_stack = filtered.get("selected_unknown_stack_sample_count", 0)
    malformed = parsed_script.get("malformed_lines", 0)
    excluded = filtered.get("excluded_counts", {})
    resolved_stack_samples = sum(
        sample.get("stack_status") == "resolved" and not sample.get("unknown_stack")
        for sample in selected)
    return {
        "sampling": sampling,
        "profile_zero_status": zero_sample_status,
        "header": parsed_header,
        "actual_header_checks": checks,
        "script_diagnostics": {key: parsed_script.get(key) for key in (
            "sample_count", "malformed_lines", "diagnostic_lines", "unknown_frame_count",
            "unknown_stack_sample_count", "missing_stack_sample_count",
            "unresolved_stack_sample_count", "truncated_stack_sample_count",
            "truncation_status")
            if key in parsed_script},
        "window_filter": {"start_inclusive_ns": interval["start_ns"],
                           "end_exclusive_ns": interval["end_ns"],
                           "pid": pid, "tids": tids,
                           "input_samples": filtered.get("input_sample_count"),
                           "selected_samples": filtered.get("selected_sample_count"),
                           "excluded_counts": excluded,
                           "excluded_rows": filtered.get("excluded")},
        "selected_samples": selected,
        "selected_window_user_samples": len(selected),
        "resolved_stack_samples": resolved_stack_samples,
        "truncation_status": parsed_script.get("truncation_status", "unreported"),
        "flat_leaf_sample_counts": dict(leaf_counts),
        "inclusive_stack_membership_counts_overlap": dict(inclusive),
        "inclusive_membership_note": "observed-stack lower bounds; memberships overlap and must not be summed",
        "unknown_stack_selected_samples": unknown_stack,
        "malformed_lines": malformed,
        "artifact_sha256": {name: digest(attempt / name) for name in (
            "perf.data", "perf.header.txt", "perf.report.txt", "perf.script-ns.txt")},
    }


def decode_negative_profile(perf: Path, perf_env: dict, data_path: Path,
                            attempt: Path, sampling: str) -> dict:
    parser = parser_module()
    header = run_text([str(perf), "report", "--header-only", "--stdio", "-i", str(data_path)],
                      env=perf_env, check=False)
    report = run_text([str(perf), "report", "--stdio", "--no-children", "--sort=symbol",
                       "--percent-limit=0", "-i", str(data_path)], env=perf_env, check=False)
    script = run_text([str(perf), "script", "--ns", "-F",
                       "pid,tid,time,event,ip,sym,dso,callindent", "-i", str(data_path)],
                      env=perf_env, check=False)
    for name, value in (("perf.header.txt", header.stdout),
                        ("perf.report.txt", report.stdout),
                        ("perf.script-ns.txt", script.stdout),
                        ("perf.header.stderr.txt", header.stderr),
                        ("perf.report.stderr.txt", report.stderr),
                        ("perf.script.stderr.txt", script.stderr)):
        (attempt / name).write_text(value)
    if any(result.returncode for result in (header, report, script)):
        raise RuntimeError("negative-control perf decode failed; output retained")
    parsed_header = parser.parse_perf_header(header.stdout, report.stdout, script.stdout)
    parsed_script = parser.parse_perf_script(script.stdout)
    event = parsed_header.get("event") or {}
    clock = parsed_header.get("clock") or {}
    sample_type = event.get("sample_type") or []
    checks = {
        "event_name": event.get("name") in ("cpu-clock:u", "cpu-clock"),
        "event_type": event.get("type_num") == 1 and event.get("config") == 0,
        "frequency_mode": event.get("frequency_mode") is True,
        "frequency_hz": event.get("frequency_hz") == 99,
        "sample_fields": {"IP", "TID", "TIME", "CALLCHAIN", "REGS_USER", "STACK_USER"}.issubset(set(sample_type)),
        "sample_registers": bool(event.get("sample_regs_user")),
        "user_stack_bytes": event.get("sample_stack_user_bytes") == 16384,
        "clock_id": str(event.get("clockid", "")).lower() in ("monotonic", "clock_monotonic", "1")
                    and clock.get("id") in (1, "1")
                    and str(clock.get("name", "")).lower() in ("monotonic", "clock_monotonic"),
        "use_clockid": event.get("use_clockid") is True,
        "inherit": event.get("inherit") is True,
        "kernel_excluded": event.get("exclude_kernel") is True,
        "hypervisor_excluded": event.get("exclude_hv") is True,
        "dwarf_callchain_register_filter": event.get("exclude_callchain_user") is True,
        "one_event": parsed_header.get("events") == 1,
        "script_parsed": parsed_script.get("attribution_valid") is True
                         and not parsed_script.get("malformed_lines"),
    }
    if not all(checks.values()):
        raise ValueError(f"negative-control perf file failed actual header/parser contract: {checks}")
    samples = parsed_script.get("sample_count", 0)
    if sampling == "disabled" and samples != 0:
        raise ValueError("disabled negative control unexpectedly contains CPU samples")
    return {"header": parsed_header, "script_diagnostics": parsed_script,
            "actual_header_checks": checks, "sample_count": samples,
            "negative_control_has_no_complete_client_timer": True,
            "perf_artifact_sha256": {name: digest(attempt / name) for name in (
                "perf.data", "perf.header.txt", "perf.report.txt", "perf.script-ns.txt")}}


def source_inventory() -> list[Path]:
    fixed = [ROOT / "dependencies.json", ROOT / "scrapanium.bend", ROOT / "http.bend",
             ROOT / "scripts/build.py", ROOT / "tests/lab.py", ROOT / "benchmarks/websocket.bend",
             ROOT / "benchmarks/websocket.py", PEER_SOURCE, BODY_FILE, PYTHON_CLIENT,
             CLOCK_SOURCE, Path(__file__).resolve(), ROOT / "benchmarks/websocket_client_perf_parse.py"]
    native = sorted(path for path in (ROOT / "native").glob("*") if path.is_file())
    return sorted(set(path.resolve() for path in [*fixed, *native] if path.is_file()))


def source_hashes() -> dict:
    return {str(path.relative_to(ROOT)): digest(path) for path in source_inventory()}


def binding_inventory() -> dict:
    script = (
        "import curl_cffi,hashlib,json,pathlib,sys;"
        "p=pathlib.Path(curl_cffi.__file__).parent;"
        "files=[p/'curl.py',p/'requests/websockets.py',*p.glob('_wrapper*.so')];"
        "print(json.dumps({'version':curl_cffi.__version__,'python':sys.version,"
        "'executable':sys.executable,'files':{str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}))"
    )
    result = subprocess.run([str(MATCHED_PYTHON), "-c", script], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return json.loads(result.stdout)


def tool_identity(path: str | Path, version_args: tuple[str, ...]) -> dict:
    resolved = Path(shutil.which(str(path)) or path).resolve()
    version = run_text([str(resolved), *version_args]).stdout.strip()
    return {"path": str(resolved), "sha256": digest(resolved), "version": version}


def compiler_source_inventory(bend_source: Path) -> dict:
    bend_source = Path(bend_source).resolve()
    result = {}
    compiler_root = bend_source / "bend2"
    for path in sorted(compiler_root.rglob("*")):
        if path.is_file() and path.suffix in (".ts", ".json"):
            result[str(path.relative_to(bend_source))] = digest(path)
    for name in ("package.json", "tsconfig.json"):
        path = bend_source / name
        if path.is_file():
            result[name] = digest(path)
    status = subprocess.run(["git", "-C", str(bend_source), "status", "--porcelain"],
                            text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL).stdout
    return {"files": result, "working_tree_status": status}


def source_build_environment() -> tuple[dict, dict]:
    compiler = shutil.which(os.environ.get("CC", "clang"))
    if not compiler:
        raise FileNotFoundError("clang is required for an unsanitized diagnostic client")
    bend_source = Path(os.environ.get("BEND_SOURCE", ROOT / ".deps/bend")).resolve()
    if not (bend_source / "bend2/main.ts").is_file():
        fallback = ROOT.parent / ".scrapanium-research/bend"
        if (fallback / "bend2/main.ts").is_file():
            bend_source = fallback.resolve()
        else:
            raise FileNotFoundError("pinned Bend compiler source is unavailable")
    bun_text = os.environ.get("BUN")
    if bun_text:
        bun = Path(bun_text).resolve()
    else:
        candidates = sorted((ROOT / ".deps/bun/node_modules/@oven").glob(
            "bun-linux-x64-baseline/bin/bun"))
        bun = candidates[0].resolve() if candidates else Path(shutil.which("bun") or "")
    if not bun.is_file():
        raise FileNotFoundError("pinned Bun executable is unavailable")
    child_env = dict(os.environ)
    for variable in ("LD_PRELOAD", "ASAN_OPTIONS", "UBSAN_OPTIONS"):
        if child_env.get(variable):
            raise RuntimeError(f"refusing inherited sanitizer/preload environment: {variable}")
    child_env.update({"CC": str(Path(compiler).resolve()), "BEND_SOURCE": str(bend_source),
                      "BUN": str(bun), "SCRAPANIUM_CURL_DIR": str((ROOT / ".deps/curl").resolve())})
    compiler_sources = compiler_source_inventory(bend_source)
    tools = {
        "clang": tool_identity(compiler, ("--version",)),
        "bun": tool_identity(bun, ("--version",)),
        "bend_source": {"path": str(bend_source),
                        "git_head": subprocess.run(["git", "-C", str(bend_source), "rev-parse", "HEAD"],
                                                   text=True, stdout=subprocess.PIPE,
                                                   stderr=subprocess.DEVNULL).stdout.strip() or None,
                        "source_file_sha256": compiler_sources["files"],
                        "working_tree_status": compiler_sources["working_tree_status"]},
    }
    return child_env, tools


def tool_hash_snapshot(tools: dict) -> dict:
    result = {}
    for name in ("clang", "bun", "go", "matched_python", "perf", "stock_backend"):
        value = tools.get(name)
        if not isinstance(value, dict) or not value.get("path"):
            raise ValueError(f"tool identity is missing path: {name}")
        path = Path(value["path"]).resolve()
        result[name] = {"path": str(path), "sha256": digest(path)}
    bend = tools.get("bend_source", {})
    source = Path(bend.get("path", "")).resolve()
    result["bend_source"] = {"path": str(source),
                             **compiler_source_inventory(source)}
    return result


def binary_sanitizer_check(binary: Path) -> dict:
    ldd = run_text(["ldd", str(binary)])
    symbols = run_text(["nm", "-a", str(binary)])
    forbidden = re.compile(r"(__asan_|__ubsan_|__lsan_|libasan|libubsan|liblsan)", re.IGNORECASE)
    if forbidden.search(ldd.stdout + symbols.stdout):
        raise RuntimeError(f"sanitizer runtime/symbol found in supposedly unsanitized binary {binary}")
    return {"ldd": ldd.stdout, "nm_sanitizer_matches": [],
            "sanitizer_runtime_absent": True}


def build_artifacts(output: Path, env: dict, perf: Path) -> dict:
    build_dir = output / "build"
    build_dir.mkdir(exist_ok=False)
    bend_binary = build_dir / "bench-ws-client-perf-bend"
    peer_binary = build_dir / "websocket-stream-peer"
    build_command = [sys.executable, str(ROOT / "scripts/build.py"), str(BODY_FILE),
                     "-o", str(bend_binary)]
    bend_build = run_text(build_command, env=env, check=False)
    (build_dir / "bend-build.stdout.txt").write_text(bend_build.stdout)
    (build_dir / "bend-build.stderr.txt").write_text(bend_build.stderr)
    if bend_build.returncode:
        raise RuntimeError(f"Bend diagnostic build failed: {bend_build.stderr}")
    go = shutil.which("go")
    if not go:
        raise FileNotFoundError("Go is required to build the local WSS peer")
    peer_command = [go, "build", "-trimpath", "-o", str(peer_binary), str(PEER_SOURCE)]
    peer_build = run_text(peer_command, env=env, check=False)
    (build_dir / "peer-build.stdout.txt").write_text(peer_build.stdout)
    (build_dir / "peer-build.stderr.txt").write_text(peer_build.stderr)
    if peer_build.returncode:
        raise RuntimeError(f"Go peer build failed: {peer_build.stderr}")
    binding = binding_inventory()
    python_sanitizer_check = binary_sanitizer_check(MATCHED_PYTHON.resolve())
    wrapper_checks = {}
    for path_text in binding["files"]:
        path = Path(path_text)
        if path.name.startswith("_wrapper") and path.suffix == ".so":
            wrapper_checks[str(path)] = binary_sanitizer_check(path)
    return {
        "bend": str(bend_binary), "peer": str(peer_binary),
        "bend_sha256": digest(bend_binary), "peer_sha256": digest(peer_binary),
        "build_commands": {"bend": build_command, "peer": peer_command},
        "bend_unsanitized_check": binary_sanitizer_check(bend_binary),
        "matched_python_unsanitized_check": python_sanitizer_check,
        "curl_cffi_wrapper_unsanitized_checks": wrapper_checks,
        "perf_sha256": digest(perf),
    }


def expected_backend_path() -> Path:
    curl = ROOT / ".deps/curl"
    link = curl / "libcurl-impersonate.so"
    if not link.is_file():
        link = curl / "lib/libcurl-impersonate.so"
    if not link.is_file():
        raise FileNotFoundError("stock source-built .deps/curl backend is unavailable")
    return link.resolve()


def start_peer(binary: Path, ca: str, key: Path) -> tuple[subprocess.Popen, str]:
    process = subprocess.Popen([str(binary), "-cert", ca, "-key", str(key)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, bufsize=1)
    url = read_line(process, timeout=30)
    if not url.startswith("wss://127.0.0.1:"):
        raise RuntimeError(f"peer announced unexpected WSS URL: {url!r}")
    return process, url


def peer_stats(peer: subprocess.Popen, run_id: str, count: int,
               positive: bool) -> dict | None:
    deadline = time.monotonic() + 5
    last = None
    stable = 0
    previous = None
    while time.monotonic() < deadline:
        if peer.poll() is not None:
            raise RuntimeError(f"WSS peer exited with status {peer.returncode}")
        if peer.stdin is None:
            raise RuntimeError("WSS peer stdin unavailable")
        peer.stdin.write("stats\n")
        peer.stdin.flush()
        records = json.loads(read_line(peer, timeout=20))
        value = records.get(run_id)
        if isinstance(value, dict):
            last = value
            if positive and value.get("data_frames") == count and not value.get("error"):
                return value
            if not positive:
                fingerprint = tuple(value.get(key) for key in (
                    "warmups", "starts", "data_frames", "data_payload_bytes",
                    "data_frame_bytes", "error"))
                stable = stable + 1 if fingerprint == previous else 0
                previous = fingerprint
                if value.get("error") or value.get("data_frames") == count or stable >= 2:
                    return value
        time.sleep(0.01)
    return last


def write_attempt_output(attempt: Path, name: str, value: str) -> None:
    (attempt / name).write_text(value, errors="replace")


def remove_control_fifos(attempt: Path) -> None:
    for name in ("perf.ctl", "perf.ack"):
        try:
            (attempt / name).unlink()
        except FileNotFoundError:
            pass


def run_one(plan_row: dict, output: Path, peer: subprocess.Popen, peer_url: str,
            ca: str, body_path: Path, env: dict, build: dict, perf: Path,
            perf_lib: Path, expected_backend: Path, full: bool) -> dict:
    attempt = output / "attempts" / plan_row["run_id"]
    attempt.mkdir(parents=True, exist_ok=False)
    row = {"plan": dict(plan_row), "attempt_dir": str(attempt), "status": "started"}
    client_process = None
    perf_process = None
    perf_fds: list[int] = []
    perf_controller = None
    collected = False
    stdout = stderr = ""
    perf_result = None
    try:
        query = {"id": plan_row["run_id"], "size": SIZE, "count": plan_row["count"],
                 "batch": plan_row["batch"], "fault": plan_row["fault"]}
        url = f"{peer_url}/stream?{urlencode(query)}"
        command = ([build["bend"], "--threads", "1"] if plan_row["client"] == CLIENTS[0]
                   else [str(MATCHED_PYTHON), str(PYTHON_CLIENT)])
        client_env = {
            **env,
            "SCRAPANIUM_BENCH_URL": url,
            "SCRAPANIUM_BENCH_CA": ca,
            "SCRAPANIUM_BENCH_COUNT": str(plan_row["count"]),
            "SCRAPANIUM_BENCH_BODY": str(body_path),
            "LD_LIBRARY_PATH": str(expected_backend.parent),
        }
        for variable in ("LD_PRELOAD", "ASAN_OPTIONS", "UBSAN_OPTIONS"):
            client_env.pop(variable, None)
        client_process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True, bufsize=1,
                                          env=client_env)
        ready = read_line(client_process, timeout=60)
        if ready != "ready":
            raise RuntimeError(f"invalid client readiness line: {ready!r}")
        backend = mapped_backend(client_process.pid, expected_backend)
        tids_before = task_tids(client_process.pid)
        row.update({"client_pid": client_process.pid, "mapped_backend": backend,
                    "client_tids_before_perf": tids_before})
        control, tids_after, perf_details, perf_fds = capture_perf(
            Path(command[0]), perf, perf_lib, attempt, client_process.pid,
            plan_row["sampling"])
        perf_controller = control
        perf_process = control.process
        tids = sorted(set(tids_before) | set(tids_after))
        row.update({"client_tids_before_gate": tids,
                    "perf_command": perf_details["command"],
                    "perf_attach_ping_ack": perf_details["ping"],
                    "perf_arming_ack": perf_details["arming"]})
        if client_process.stdin is None:
            raise RuntimeError("client stdin unavailable for start gate")
        gate_write_ns = time.monotonic_ns()
        client_process.stdin.write("x")
        client_process.stdin.flush()
        try:
            stdout, stderr, tids_after_run = collect_client_output(
                client_process, tids, timeout=180)
            tids = sorted(set(tids) | set(tids_after_run))
        except TimeoutError:
            raise
        collected = True
        perf_result = finish_perf(perf_process, perf_fds)
        perf_process = None
        perf_fds = []
        remove_control_fifos(attempt)
        if perf_result["returncode"] != 0:
            raise RuntimeError(f"perf recorder exited with status {perf_result['returncode']}: {perf_result['stderr']}")
        client_result = validate_client_result(plan_row, client_process.returncode,
                                              stdout + "\n" + stderr)
        peer_record = peer_stats(peer, plan_row["run_id"], plan_row["count"],
                                 plan_row["kind"] == "positive")
        if peer_record is None:
            raise ValueError("peer omitted this run ID from its stats snapshot")
        peer_validation = validate_peer(peer_record, plan_row)
        if plan_row["kind"] == "negative_control":
            if plan_row["fault"] not in FAULTS:
                raise ValueError("negative plan has unknown corruption type")
        interval = client_result if plan_row["kind"] == "positive" else None
        if interval:
            timer_mark = {"parent_gate_write_monotonic_ns": gate_write_ns,
                          "client_start_monotonic_ns": interval["start_ns"],
                          "client_end_monotonic_ns": interval["end_ns"]}
            if interval["start_ns"] < gate_write_ns:
                raise ValueError("client timer starts before the parent wrote the gate")
            decode = decode_profile(perf, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                             "LD_LIBRARY_PATH": str(perf_lib),
                                             "LANG": "C", "LC_ALL": "C"},
                                    attempt / "perf.data", client_process.pid, tids,
                                    interval, plan_row["sampling"], attempt, full)
        else:
            timer_mark = {"parent_gate_write_monotonic_ns": gate_write_ns,
                          "client_interval": "expected absent because exact negative rejection stops early"}
            decode = decode_negative_profile(perf, {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LD_LIBRARY_PATH": str(perf_lib), "LANG": "C", "LC_ALL": "C"},
                attempt / "perf.data", attempt, plan_row["sampling"])
        write_attempt_output(attempt, "client.stdout.txt", stdout)
        write_attempt_output(attempt, "client.stderr.txt", stderr)
        write_json(attempt / "peer-record.json", peer_record)
        write_json(attempt / "perf-control.json", {
            **perf_details, "commands": perf_controller.transcript if perf_controller else [],
            **perf_result})
        row.update({
            "status": "passed", "client_command": command,
            "client_returncode": client_process.returncode,
            "client_result": client_result, "backend": backend, "client_tids": tids,
            "parent_gate_write_monotonic_ns": gate_write_ns,
            "timer": timer_mark, "peer": peer_validation, "peer_raw": peer_record,
            "perf": decode, "perf_control_commands": perf_controller.transcript if perf_controller else [],
            "perf_data_sha256": digest(attempt / "perf.data"),
            "perf_data_bytes": (attempt / "perf.data").stat().st_size,
        })
    except BaseException as error:
        row.update({"status": "failed", "failure": f"{type(error).__name__}: {error}"})
        if client_process is not None and not collected:
            if client_process.poll() is None:
                client_process.kill()
            try:
                tail, err_tail = client_process.communicate(timeout=10)
                stdout += tail
                stderr += err_tail
            except BaseException as collect_error:
                row["collection_error"] = f"{type(collect_error).__name__}: {collect_error}"
        write_attempt_output(attempt, "client.stdout.txt", stdout)
        write_attempt_output(attempt, "client.stderr.txt", stderr)
        try:
            record = peer_stats(peer, plan_row["run_id"], plan_row["count"],
                                plan_row["kind"] == "positive")
            if record is not None:
                write_json(attempt / "peer-record.json", record)
                row["peer_raw"] = record
        except BaseException as peer_error:
            row["peer_collection_error"] = f"{type(peer_error).__name__}: {peer_error}"
        if perf_process is not None:
            try:
                # The client is already being collected/killed here. Let the
                # attached recorder exit naturally; never send a post-exit
                # disable acknowledgement request.
                stdout_perf, stderr_perf = perf_process.communicate(timeout=10)
                perf_result = {"returncode": perf_process.returncode,
                               "stdout": stdout_perf, "stderr": stderr_perf}
            except BaseException as perf_error:
                perf_process.kill()
                stdout_perf, stderr_perf = perf_process.communicate()
                perf_result = {"returncode": perf_process.returncode,
                               "stdout": stdout_perf, "stderr": stderr_perf}
                row["perf_collection_error"] = f"{type(perf_error).__name__}: {perf_error}"
            (attempt / "perf.stdout.txt").write_text(perf_result["stdout"])
            (attempt / "perf.stderr.txt").write_text(perf_result["stderr"])
        for fd in perf_fds:
            try:
                os.close(fd)
            except OSError:
                pass
    finally:
        if client_process is not None:
            row.setdefault("client_returncode", client_process.poll())
        if perf_result is not None and not (attempt / "perf-control.json").exists():
            write_json(attempt / "perf-control.json", {
                "commands": perf_controller.transcript if perf_controller else [],
                **perf_result})
        remove_control_fifos(attempt)
        row["client_stdout_sha256"] = hashlib.sha256(stdout.encode()).hexdigest()
        row["client_stderr_sha256"] = hashlib.sha256(stderr.encode()).hexdigest()
        row["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_json(attempt / "row.json", row)
    return row


def summarize_floor(rows: list[dict]) -> dict:
    result = {}
    for client in CLIENTS:
        for batch in BATCHES:
            cell = [row for row in rows
                    if row.get("status") == "passed"
                    and row["plan"]["kind"] == "positive"
                    and row["plan"]["sampling"] == "profiled"
                    and row["plan"]["client"] == client
                    and row["plan"]["batch"] == batch]
            valid = sum(int((row.get("perf") or {}).get("selected_window_user_samples", 0))
                        for row in cell)
            result[f"{client}/flush{batch}"] = {
                "profiled_rows_completed": len(cell),
                "selected_user_samples": valid,
                "predeclared_floor": SAMPLE_FLOOR,
                "floor_met": valid >= SAMPLE_FLOOR,
            }
    return result


def check_full_storage(output: Path) -> dict:
    result = run_text(["findmnt", "-T", str(output), "-n", "-o", "FSTYPE"])
    fs_type = result.stdout.strip()
    if fs_type not in {"ext4", "xfs", "btrfs"}:
        raise RuntimeError(f"full profiling output must use persistent Linux storage, got {fs_type!r}")
    return {"filesystem": fs_type, "path": str(output)}


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    mode_group = cli.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--smoke", action="store_true", help="fixed correctness-only smoke")
    mode_group.add_argument("--full", action="store_true", help="fixed 768-positive profile schedule")
    cli.add_argument("--output-dir", required=True, type=Path)
    cli.add_argument("--perf", help="perf executable; defaults to SCRAPANIUM_PERF or PATH")
    cli.add_argument("--seed", type=int, default=SCHEDULE_SEED)
    args = cli.parse_args(argv)
    mode = "full" if args.full else "smoke"
    output = args.output_dir.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic output: {output}")
    if mode == "full":
        storage = check_full_storage(output.parent)
    else:
        storage = {"filesystem_check": "not required for correctness-only smoke"}
    plan = make_plan(mode, args.seed)
    output.mkdir(parents=True, exist_ok=False)
    (output / "attempts").mkdir()
    write_json(output / "plan.json", {
        "mode": mode, "seed": args.seed,
        "positive_repeats_per_batch": FULL_REPEATS if mode == "full" else SMOKE_REPEATS,
        "positive_count": sum(row["kind"] == "positive" for row in plan),
        "negative_control_count": sum(row["kind"] == "negative_control" for row in plan),
        "sampling_contract": {"event": "cpu-clock:u", "frequency_hz": 99,
                              "call_graph": "DWARF", "user_stack_bytes": 16384,
                              "clock": "CLOCK_MONOTONIC", "inherit": True,
                              "kernel_samples": False,
                              "sample_floor_per_client_batch": SAMPLE_FLOOR,
                              "no_adaptive_extension": True},
        "timer_contract": "integer CLOCK_MONOTONIC nanoseconds; half-open sample filter [start,end); start immediately before sending start control; end after exact opcode/bytes/order validation and immediate release, before socket close",
        "conditions": [{"client": client, "sampling": sampling}
                      for client in CLIENTS for sampling in SAMPLING],
        "batches": list(BATCHES), "size_bytes": SIZE,
        "positive_message_count": FULL_COUNT if mode == "full" else SMOKE_COUNT,
        "controls_run_first": True,
        "client_check_contract": "binary opcode, ordered eight-digit sequence, full exact payload comparison, immediate actual and expected release",
        "observer_control": "attached perf recorder is identical; event enabled only for profiled condition and held disabled in control",
    })
    manifest = {
        "schema": 1, "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": mode, "status": "planned", "output_dir": str(output),
        "storage": storage, "plan_sha256": digest(output / "plan.json"),
        "planned_rows": len(plan), "attempted_run_ids": [], "failed_run_ids": [],
        "source_hashes_before": None, "source_hashes_after": None,
        "results": "samples.jsonl", "correctness_only_smoke_is_not_performance_evidence": True,
    }
    write_json(output / "manifest.json", manifest)
    # The experiment is intentionally tied to the repository's stock source-
    # built curl backend and to a fresh, unsanitized client executable.
    perf = resolve_perf(args.perf)
    perf_lib = Path(os.environ.get("SCRAPANIUM_PERF_LIB", PERF_LIB_DEFAULT)).resolve()
    if not perf_lib.is_dir():
        raise FileNotFoundError(f"perf runtime library directory is unavailable: {perf_lib}")
    expected_backend = expected_backend_path()
    body_path = output / "body.bin"
    body = bytes((index * 31) % 128 for index in range(SIZE - 8))
    body_path.write_bytes(body)
    if len(body) != SIZE - 8 or digest(body_path) != hashlib.sha256(body).hexdigest():
        raise RuntimeError("diagnostic corpus construction failed")
    env, tools = source_build_environment()
    tool_versions = {
        **tools,
        "go": tool_identity(shutil.which("go") or "go", ("version",)),
        "matched_python": {"path": str(MATCHED_PYTHON.resolve()),
                           "sha256": digest(MATCHED_PYTHON.resolve()),
                           "binding": binding_inventory()},
        "perf": {"path": str(perf), "sha256": digest(perf),
                 "version": run_text([str(perf), "version"],
                                     env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                          "LD_LIBRARY_PATH": str(perf_lib),
                                          "LANG": "C", "LC_ALL": "C"}).stdout.strip()},
        "stock_backend": {"path": str(expected_backend), "sha256": digest(expected_backend)},
    }
    # Freeze all build inputs before compiling either client or peer. This
    # inventory is also the pre-run baseline checked again after the schedule.
    source_before = source_hashes()
    binding_before = tool_versions["matched_python"]["binding"]
    tool_hashes_before = tool_hash_snapshot(tool_versions)
    manifest.update({"source_hashes_before": source_before,
                     "binding_hashes_before": binding_before,
                     "tool_input_hashes_before": tool_hashes_before,
                     "build_inputs_frozen_before_compile": True})
    write_json(output / "manifest.json", manifest)
    built = build_artifacts(output, env, perf)
    binary_before = {"bend": digest(Path(built["bend"])), "peer": digest(Path(built["peer"])),
                     "bend_generated_c": digest(Path(built["bend"]).with_suffix(".generated.c")),
                     "matched_python": digest(MATCHED_PYTHON.resolve()),
                     "binding_inventory_sha256": hashlib.sha256(
                         json.dumps(binding_before, sort_keys=True).encode()).hexdigest(),
                     "perf": digest(perf), "backend": digest(expected_backend)}
    manifest.update({"status": "running", "tools": tool_versions,
                     "build": built, "body_sha256": digest(body_path),
                     "binary_hashes_before": binary_before,
                     "sanitizer_environment_clear": True,
                     "sampling_floor_scope": "profiled positive rows aggregated by client and flush batch; below floor remains inconclusive; no extension"})
    write_json(output / "manifest.json", manifest)

    # A generated TLS key lives only in this temporary directory, then is
    # deleted. Its public certificate and hash remain as reconstruction data.
    rows = []
    tls_identities = set()
    failure = None
    with tempfile.TemporaryDirectory(prefix="wss-client-perf-tls-", dir=output) as temp:
        temp_path = Path(temp)
        context, ca = certificate(temp_path)
        key_path = temp_path / "key.pem"
        ca_path = Path(ca)
        certificate_copy = output / "peer-ca.pem"
        shutil.copyfile(ca_path, certificate_copy)
        tls_meta = {"certificate_sha256": digest(certificate_copy),
                    "certificate_bytes": certificate_copy.stat().st_size,
                    "private_key_retained": False}
        peer, peer_url = start_peer(Path(built["peer"]), ca, key_path)
        try:
            for plan_row in plan:
                row = run_one(plan_row, output, peer, peer_url, ca, body_path, env,
                              built, perf, perf_lib, expected_backend, mode == "full")
                rows.append(row)
                append_jsonl(output / "samples.jsonl", row)
                manifest["attempted_run_ids"].append(plan_row["run_id"])
                if row["status"] != "passed":
                    manifest["failed_run_ids"].append(plan_row["run_id"])
                    manifest["status"] = "failed_partial"
                    manifest["last_failure"] = row.get("failure")
                    write_json(output / "manifest.json", manifest)
                    failure = RuntimeError(f"diagnostic run failed: {plan_row['run_id']}: {row.get('failure')}")
                    break
                tls_identities.add((row["peer"]["tls_version"], row["peer"]["tls_cipher"]))
                manifest["status"] = "running"
                write_json(output / "manifest.json", manifest)
                if len(rows) % 16 == 0:
                    print(f"completed {len(rows)}/{len(plan)} fixed rows", flush=True)
        finally:
            peer.stdin.close()
            try:
                peer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                peer.kill()
                peer.wait()
            manifest["peer_returncode"] = peer.returncode
            manifest["tls"] = tls_meta
    source_after = source_hashes()
    binding_after = binding_inventory()
    tool_hashes_after = tool_hash_snapshot(tool_versions)
    binary_after = {"bend": digest(Path(built["bend"])), "peer": digest(Path(built["peer"])),
                    "bend_generated_c": digest(Path(built["bend"]).with_suffix(".generated.c")),
                    "matched_python": digest(MATCHED_PYTHON.resolve()),
                    "binding_inventory_sha256": hashlib.sha256(
                        json.dumps(binding_after, sort_keys=True).encode()).hexdigest(),
                    "perf": digest(perf), "backend": digest(expected_backend)}
    manifest["source_hashes_after"] = source_after
    manifest["binding_hashes_after"] = binding_after
    manifest["binding_hashes_unchanged"] = binding_before == binding_after
    manifest["tool_input_hashes_after"] = tool_hashes_after
    manifest["tool_input_hashes_unchanged"] = tool_hashes_before == tool_hashes_after
    manifest["binary_hashes_after"] = binary_after
    manifest["source_hashes_unchanged"] = source_before == source_after
    manifest["binary_hashes_unchanged"] = binary_before == binary_after
    manifest["tls_identity_set"] = [list(item) for item in sorted(tls_identities)]
    manifest["rows_completed"] = len(rows)
    manifest["negative_controls_passed"] = sum(row["status"] == "passed" and
                                                 row["plan"]["kind"] == "negative_control"
                                                 for row in rows)
    manifest["positive_rows_passed"] = sum(row["status"] == "passed" and
                                             row["plan"]["kind"] == "positive"
                                             for row in rows)
    manifest["profile_floor"] = summarize_floor(rows)
    if failure is None and source_before != source_after:
        failure = RuntimeError("diagnostic sources changed after the fixed schedule")
    if failure is None and binding_before != binding_after:
        failure = RuntimeError("matched curl_cffi binding files changed after the fixed schedule")
    if failure is None and tool_hashes_before != tool_hashes_after:
        failure = RuntimeError("compiler/source/runtime/binding/perf inputs changed after the fixed schedule")
    if failure is None and binary_before != binary_after:
        failure = RuntimeError("diagnostic binaries/backend changed after the fixed schedule")
    if failure is None and len(tls_identities) > 1:
        failure = RuntimeError(f"TLS identity changed between positive cells: {tls_identities}")
    if failure is None and len(rows) != len(plan):
        failure = RuntimeError("fixed plan stopped before all planned rows completed")
    if failure:
        manifest["status"] = "failed_partial"
        manifest["failure"] = f"{type(failure).__name__}: {failure}"
        write_json(output / "manifest.json", manifest)
        raise failure
    floor_met = all(item["floor_met"] for item in manifest["profile_floor"].values())
    manifest["status"] = "complete" if mode == "smoke" or floor_met else "complete_inconclusive_sample_floor"
    manifest["completed_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["sample_floor_met"] = floor_met if mode == "full" else None
    write_json(output / "manifest.json", manifest)
    if mode == "full" and not floor_met:
        print("fixed full schedule completed below the predeclared sample floor; no extension", file=sys.stderr)
        return 2
    print(json.dumps({"status": manifest["status"], "rows": len(rows),
                      "output_dir": str(output), "tls_identity_set": manifest["tls_identity_set"],
                      "profile_floor": manifest["profile_floor"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
