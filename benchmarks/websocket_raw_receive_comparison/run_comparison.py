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
from urllib.parse import parse_qs, urlsplit

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
    projected = {key: result[key] for key in keys if key in result}
    diagnostics = result.get("failure_diagnostics")
    if diagnostics is not None:
        projected["failure_diagnostics"] = smoke_failure_projection(diagnostics)
    return projected


def smoke_failure_projection(diagnostics):
    """Keep failure facts, but omit monotonic timestamps and client timing output."""
    children = []
    for child in diagnostics.get("children", []):
        item = {key: child[key] for key in ("pid", "command", "returncode", "mapped_backend")
                if key in child}
        for stream_name in ("stdout", "stderr"):
            stream = child.get(stream_name)
            if stream is None:
                continue
            text = stream.get("text", "") if isinstance(stream, dict) else str(stream)
            if stream_name == "stdout":
                lines = []
                for line in text.splitlines():
                    try:
                        float(line.strip())
                    except ValueError:
                        lines.append(line)
                text = "\n".join(lines)
            item[stream_name] = {"text": text, "truncated": stream.get("truncated", False)
                                 if isinstance(stream, dict) else False}
        children.append(item)
    projected = {"observer": diagnostics.get("observer"),
                 "observer_overhead": diagnostics.get("observer_overhead"),
                 "stages": [event.get("stage") for event in diagnostics.get("events", [])],
                 "children": children}
    peer = diagnostics.get("peer")
    if peer is not None:
        keep = ("kind", "frames", "payload_bytes", "expected_frame_matches", "frame_samples",
                "omitted_frame_samples", "tls_connections", "read_calls", "successful_reads",
                "first_applied_socket_timeout_seconds", "read_error", "close_observed",
                "stats_records", "stats_query_error", "peer_returncode")
        projected["peer"] = {key: peer[key] for key in keep if key in peer}
        read_error = projected["peer"].get("read_error")
        if isinstance(read_error, dict):
            projected["peer"]["read_error"] = {
                key: read_error[key] for key in ("type", "message", "is_socket_timeout")
                if key in read_error
            }
    if "observer_error" in diagnostics:
        projected["observer_error"] = diagnostics["observer_error"]
    return projected


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


_DIAGNOSTIC_TEXT_LIMIT = 64 * 1024
_DIAGNOSTIC_FRAME_SAMPLES = 8


def _bounded_text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value)
    return {"text": value[:_DIAGNOSTIC_TEXT_LIMIT],
            "truncated": len(value) > _DIAGNOSTIC_TEXT_LIMIT}


def _is_numeric_line(line):
    try:
        float(line.strip())
        return True
    except ValueError:
        return False


class _ObservedStdin:
    def __init__(self, stream, observer, child):
        self._stream = stream
        self._observer = observer
        self._child = child

    def write(self, value):
        is_gate = value == "x"
        if is_gate:
            self._observer.stage("gate_write_start", pid=self._child["pid"])
        result = self._stream.write(value)
        if is_gate:
            self._child["gate_pending"] = True
            self._observer.stage("gate_write_complete", pid=self._child["pid"])
        return result

    def flush(self):
        pending = self._child.get("gate_pending", False)
        if pending:
            self._observer.stage("gate_flush_start", pid=self._child["pid"])
        result = self._stream.flush()
        if pending:
            self._child["gate_pending"] = False
            self._observer.stage("gate_released", pid=self._child["pid"])
        return result

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _ObservedPipe:
    def __init__(self, stream, observer, child, key):
        self._stream = stream
        self._observer = observer
        self._child = child
        self._key = key

    def read(self, *args, **kwargs):
        value = self._stream.read(*args, **kwargs)
        self._observer.capture_child_stream(self._child, self._key, value)
        return value

    def readline(self, *args, **kwargs):
        if (self._key == "stdout" and not self._child.get("readiness_line_read")
                and not args and not kwargs and hasattr(self._stream, "encoding")):
            # Consume only the readiness line from the pipe. TextIOWrapper.readline()
            # may prefetch later timing output into its private buffer, which a later
            # Popen.communicate() reads around; bytewise reads leave that output on
            # the fd for the original communicate() call.
            raw = bytearray()
            fd = self._stream.fileno()
            while True:
                chunk = os.read(fd, 1)
                if not chunk:
                    break
                raw.extend(chunk)
                if chunk in (b"\n", b"\r"):
                    break
            value = raw.decode(self._stream.encoding or "utf-8",
                               self._stream.errors or "strict")
            value = value.replace("\r\n", "\n").replace("\r", "\n")
            self._child["readiness_line_read"] = True
        else:
            value = self._stream.readline(*args, **kwargs)
        self._observer.capture_child_stream(self._child, self._key, value)
        return value

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _ObservedProcess:
    def __init__(self, process, observer, child):
        self._process = process
        self._observer = observer
        self._child = child
        self._stdin = None
        self._stdout = None
        self._stderr = None

    @property
    def stdin(self):
        stream = self._process.stdin
        if stream is None:
            return None
        if self._stdin is None:
            self._stdin = _ObservedStdin(stream, self._observer, self._child)
        return self._stdin

    @property
    def stdout(self):
        stream = self._process.stdout
        if stream is None:
            return None
        if self._stdout is None:
            self._stdout = _ObservedPipe(stream, self._observer, self._child, "stdout")
        return self._stdout

    @property
    def stderr(self):
        stream = self._process.stderr
        if stream is None:
            return None
        if self._stderr is None:
            self._stderr = _ObservedPipe(stream, self._observer, self._child, "stderr")
        return self._stderr

    @property
    def returncode(self):
        return self._process.returncode

    def communicate(self, *args, **kwargs):
        self._observer.stage("child_communicate_start", pid=self._child["pid"],
                             timeout=kwargs.get("timeout"))
        try:
            output, errors = self._process.communicate(*args, **kwargs)
            self._observer.capture_child_output(self._child, output, errors)
            returncode = self._process.returncode
            if returncode is not None:
                self._child["returncode"] = returncode
            self._child["communicate_completed"] = True
            self._child["streams_drained"] = True
            self._observer.stage("child_communicate_complete", pid=self._child["pid"],
                                 returncode=returncode)
            return output, errors
        except BaseException as exc:
            if not isinstance(exc, self._observer.subprocess_module.TimeoutExpired):
                self._observer.capture_child_output(
                    self._child, getattr(exc, "output", None), getattr(exc, "stderr", None))
            self._child["returncode"] = self._process.poll()
            self._observer.stage("child_communicate_error", pid=self._child["pid"],
                                 error_type=type(exc).__name__,
                                 returncode=self._child["returncode"])
            raise

    def kill(self):
        self._observer.stage("child_kill", pid=self._child["pid"])
        return self._process.kill()

    def __getattr__(self, name):
        return getattr(self._process, name)


class _ObservedSubprocess:
    def __init__(self, subprocess_module, observer):
        self._subprocess = subprocess_module
        self._observer = observer

    def Popen(self, *args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        self._observer.stage("child_spawn_start", command=command)
        try:
            process = self._subprocess.Popen(*args, **kwargs)
        except BaseException as exc:
            self._observer.stage("child_spawn_error", error_type=type(exc).__name__)
            raise
        child = {"pid": process.pid, "command": command, "returncode": None,
                 "stdout": None, "stderr": None, "mapped_backend": None,
                 "communicate_completed": False}
        self._observer.children.append(child)
        self._observer.children_by_pid[process.pid] = child
        wrapped = _ObservedProcess(process, self._observer, child)
        self._observer.processes_by_pid[process.pid] = wrapped
        self._observer.stage("child_spawned", pid=process.pid)
        return wrapped

    def __getattr__(self, name):
        return getattr(self._subprocess, name)


class RoundtripFailureObserver:
    """Buffer low-cost failure evidence; flush only when the sample returns or raises."""

    def __init__(self, rt, run_id, peer=None, tls_records=None, frame_start=0, tls_start=0,
                 expected_payload=b"", tls_by_socket=None, connections=1):
        self.rt = rt
        self.run_id = run_id
        self.peer = peer
        self.tls_records = tls_records if tls_records is not None else []
        self.frame_start = frame_start
        self.tls_start = tls_start
        self.expected_payload = expected_payload
        self.tls_by_socket = tls_by_socket if tls_by_socket is not None else {}
        self.expected_request_ids = {f"{run_id}-{index}" for index in range(connections)}
        self.events = []
        self.children = []
        self.children_by_pid = {}
        self.processes_by_pid = {}
        self.subprocess_module = None
        self.mapped_backends = []
        self.mapping_pid = None
        self.restore_actions = []
        self.observer_errors = []
        self.peer_state = {"read_calls": 0, "successful_reads": 0,
                           "first_read_started_monotonic_ns": None,
                           "latest_read_started_monotonic_ns": None,
                           "first_applied_socket_timeout_seconds": None,
                           "read_error": None, "close_started_monotonic_ns": None,
                           "close_completed_monotonic_ns": None,
                           "tracked_stream_ids": set(), "tracked_socket_ids": set(),
                           "frame_ranges": [], "tls_connections": []}

    def stage(self, name, **fields):
        self.events.append({"stage": name, "monotonic_ns": time.monotonic_ns(), **fields})

    def capture_child_output(self, child, stdout, stderr):
        if stdout is not None:
            self.capture_child_stream(child, "stdout", stdout)
        if stderr is not None:
            self.capture_child_stream(child, "stderr", stderr)

    def capture_child_stream(self, child, name, value):
        if value is None:
            return
        captured = _bounded_text(value)
        previous = child.get(name)
        if previous is None:
            child[name] = captured
            return
        joined = previous["text"] + captured["text"]
        child[name] = {"text": joined[:_DIAGNOSTIC_TEXT_LIMIT],
                       "truncated": previous["truncated"] or captured["truncated"] or
                       len(joined) > _DIAGNOSTIC_TEXT_LIMIT}

    def close_child_streams(self, child, process):
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name)
            if stream is not None:
                try:
                    stream.close()
                except BaseException as observer_error:
                    self.observer_errors.append({"stage": "child_pipe_close",
                        "pid": child["pid"], "stream": stream_name,
                        "error_type": type(observer_error).__name__})

    def _replace(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.restore_actions.append(("attribute", owner, name, original))
        return original

    def __enter__(self):
        try:
            original_subprocess = self.rt.subprocess
            self.subprocess_module = original_subprocess
            self._replace(self.rt, "subprocess", _ObservedSubprocess(original_subprocess, self))

            original_line = self.rt.line

            def observed_line(process, *args, **kwargs):
                child = self.children_by_pid.get(getattr(process, "pid", None))
                if child is None:
                    return original_line(process, *args, **kwargs)
                self.stage("ready_wait_start", pid=child["pid"])
                try:
                    value = original_line(process, *args, **kwargs)
                    self.stage("ready_received", pid=child["pid"], value=str(value)[:256])
                    return value
                except BaseException as exc:
                    self.stage("ready_wait_error", pid=child["pid"],
                               error_type=type(exc).__name__)
                    raise

            self._replace(self.rt, "line", observed_line)

            original_mapped_backend = self.rt.mapped_backend

            def observed_mapped_backend(process):
                pid = getattr(process, "pid", None)
                self.mapping_pid = pid
                self.stage("backend_map_start", pid=pid)
                try:
                    mapped = original_mapped_backend(process)
                    self.mapped_backends.append(mapped)
                    child = self.children_by_pid.get(pid)
                    if child is not None:
                        child["mapped_backend"] = mapped
                    self.stage("backend_map_complete", pid=pid, path=mapped.get("path"))
                    return mapped
                except BaseException as exc:
                    self.stage("backend_map_error", pid=pid, error_type=type(exc).__name__)
                    raise
                finally:
                    self.mapping_pid = None

            self._replace(self.rt, "mapped_backend", observed_mapped_backend)

            original_digest = self.rt.digest

            def observed_digest(path):
                pid = self.mapping_pid
                self.stage("backend_hash_start", pid=pid, path=str(path))
                try:
                    value = original_digest(path)
                    self.stage("backend_hash_complete", pid=pid, path=str(path), sha256=value)
                    return value
                except BaseException as exc:
                    self.stage("backend_hash_error", pid=pid, path=str(path),
                               error_type=type(exc).__name__)
                    raise

            self._replace(self.rt, "digest", observed_digest)
            self._install_peer_observers()
            self.stage("sample_start")
            return self
        except BaseException:
            self.restore()
            raise

    def _install_peer_observers(self):
        if self.peer is None:
            return
        peer_globals = getattr(self.rt.start_ws, "__globals__", None)
        if peer_globals is not None and callable(peer_globals.get("read_frame")):
            original_read_frame = peer_globals["read_frame"]

            def observed_read_frame(stream):
                if id(stream) not in self.peer_state["tracked_stream_ids"]:
                    return original_read_frame(stream)
                started = time.monotonic_ns()
                state = self.peer_state
                state["read_calls"] += 1
                state["latest_read_started_monotonic_ns"] = started
                if state["first_read_started_monotonic_ns"] is None:
                    state["first_read_started_monotonic_ns"] = started
                    try:
                        state["first_applied_socket_timeout_seconds"] = stream.raw._sock.gettimeout()
                    except Exception as exc:
                        state["socket_timeout_observation_error"] = type(exc).__name__
                try:
                    frame = original_read_frame(stream)
                    state["successful_reads"] += 1
                    return frame
                except BaseException as exc:
                    ended = time.monotonic_ns()
                    state["read_error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc)[:256],
                        "is_socket_timeout": type(exc).__name__ in ("TimeoutError", "timeout"),
                        "started_monotonic_ns": started,
                        "ended_monotonic_ns": ended,
                        "elapsed_ns": ended - started,
                    }
                    raise

            peer_globals["read_frame"] = observed_read_frame
            self.restore_actions.append(("dictionary", peer_globals, "read_frame", original_read_frame))

        handler_class = peer_globals.get("Handler") if peer_globals is not None else None
        if handler_class is not None and callable(getattr(handler_class, "do_GET", None)):
            original_do_get = handler_class.do_GET

            def observed_do_get(handler):
                query = parse_qs(urlsplit(getattr(handler, "path", "")).query)
                request_ids = query.get("id", [])
                matched = (getattr(handler, "server", None) is self.peer and
                           len(request_ids) == 1 and request_ids[0] in self.expected_request_ids)
                frame_range = [len(getattr(self.peer, "frames", [])), None] if matched else None
                if matched:
                    self.peer_state["tracked_stream_ids"].add(id(handler.rfile))
                    self.peer_state["tracked_socket_ids"].add(id(handler.connection))
                    self.peer_state["frame_ranges"].append(frame_range)
                    tls = self.tls_by_socket.get(id(handler.connection))
                    if tls is not None:
                        self.peer_state["tls_connections"].append(dict(tls))
                try:
                    return original_do_get(handler)
                finally:
                    if matched:
                        frame_range[1] = len(getattr(self.peer, "frames", []))

            handler_class.do_GET = observed_do_get
            self.restore_actions.append(("class_attribute", handler_class, "do_GET", original_do_get))

        original_shutdown_request, had_instance_override = self._install_shutdown_observer()
        self.restore_actions.append(("peer_shutdown", self.peer, "shutdown_request",
                                     original_shutdown_request, had_instance_override))

    def _install_shutdown_observer(self):
        had_instance_override = "shutdown_request" in getattr(self.peer, "__dict__", {})
        original_shutdown_request = self.peer.shutdown_request

        def observed_shutdown_request(request):
            if id(request) not in self.peer_state["tracked_socket_ids"]:
                return original_shutdown_request(request)
            self.peer_state["close_started_monotonic_ns"] = time.monotonic_ns()
            try:
                return original_shutdown_request(request)
            finally:
                self.peer_state["close_completed_monotonic_ns"] = time.monotonic_ns()

        self.peer.shutdown_request = observed_shutdown_request
        return original_shutdown_request, had_instance_override

    def restore(self):
        while self.restore_actions:
            action = self.restore_actions.pop()
            try:
                if action[0] == "attribute":
                    _, owner, name, original = action
                    setattr(owner, name, original)
                elif action[0] == "dictionary":
                    _, owner, name, original = action
                    owner[name] = original
                elif action[0] == "class_attribute":
                    _, owner, name, original = action
                    setattr(owner, name, original)
                else:
                    _, peer, name, original, had_override = action
                    if had_override:
                        setattr(peer, name, original)
                    else:
                        delattr(peer, name)
            except BaseException as exc:
                self.observer_errors.append({"stage": "restore", "error_type": type(exc).__name__})

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                try:
                    self.finalize_children()
                except BaseException as observer_error:
                    self.observer_errors.append({"stage": "child_finalize",
                                                 "error_type": type(observer_error).__name__})
                try:
                    self.wait_for_peer_close()
                except BaseException as observer_error:
                    self.observer_errors.append({"stage": "peer_close_wait",
                                                 "error_type": type(observer_error).__name__})
            try:
                self.stage("sample_exit", error_type=exc_type.__name__ if exc_type else None)
            except BaseException as observer_error:
                self.observer_errors.append({"stage": "sample_exit",
                                             "error_type": type(observer_error).__name__})
        finally:
            self.restore()
        return False

    def finalize_children(self):
        for child in self.children:
            process = self.processes_by_pid[child["pid"]]
            try:
                returncode = process.poll()
                if returncode is not None:
                    child["returncode"] = returncode
                if returncode is None and not child.get("communicate_completed"):
                    # This is failure-only cleanup. Let a client finish briefly,
                    # terminate it only if needed, then use its original communicate
                    # path to retain complete stdout/stderr.
                    self.stage("child_exit_wait_start", pid=child["pid"])
                    try:
                        child["returncode"] = process.wait(timeout=1)
                    except self.subprocess_module.TimeoutExpired:
                        self.stage("child_exit_wait_timeout", pid=child["pid"])
                        process.kill()
                        child["returncode"] = process.poll()
                if not child.get("communicate_completed"):
                    process.communicate()
            except BaseException as exc:
                if child.get("returncode") is None:
                    child["returncode"] = process.poll()
                child["finalize_error"] = type(exc).__name__
            self.close_child_streams(child, process)
            self.stage("child_finalized", pid=child["pid"], returncode=child["returncode"],
                       communicate_completed=child.get("communicate_completed", False))

    def wait_for_peer_close(self):
        if not self.peer_state["tracked_socket_ids"] or self.peer_state["close_completed_monotonic_ns"] is not None:
            return
        self.stage("peer_close_wait_start")
        deadline = time.monotonic() + 1.0
        while (self.peer_state["close_completed_monotonic_ns"] is None and
               time.monotonic() < deadline):
            time.sleep(0.005)
        self.stage("peer_close_wait_complete",
                   close_observed=self.peer_state["close_completed_monotonic_ns"] is not None)

    def wait_for_peer_close_after_sample(self):
        if self.peer is None or self.peer_state["close_completed_monotonic_ns"] is not None:
            return
        original, had_override = self._install_shutdown_observer()
        try:
            self.wait_for_peer_close()
        finally:
            if had_override:
                self.peer.shutdown_request = original
            else:
                delattr(self.peer, "shutdown_request")

    def _peer_observation(self):
        if self.peer is None:
            return None
        all_frames = getattr(self.peer, "frames", [])
        frames = []
        for start, end in self.peer_state["frame_ranges"]:
            if end is None:
                end = len(all_frames)
            frames.extend(all_frames[start:end])
        sample_indexes = list(range(min(len(frames), _DIAGNOSTIC_FRAME_SAMPLES)))
        if len(frames) > _DIAGNOSTIC_FRAME_SAMPLES:
            sample_indexes.extend(range(max(_DIAGNOSTIC_FRAME_SAMPLES, len(frames) - _DIAGNOSTIC_FRAME_SAMPLES), len(frames)))
        frame_samples = []
        expected = (2, self.expected_payload, True)
        for index in sample_indexes:
            opcode, payload, fin = frames[index]
            frame_samples.append({"index": index, "opcode": opcode,
                                  "payload_bytes": len(payload), "fin": bool(fin),
                                  "matches_expected": frames[index] == expected})
        tls = list(self.peer_state["tls_connections"])
        return {
            "kind": "python",
            "frames": len(frames),
            "payload_bytes": sum(len(frame[1]) for frame in frames),
            "expected_frame_matches": sum(frame == expected for frame in frames),
            "frame_samples": frame_samples,
            "omitted_frame_samples": max(0, len(frames) - len(frame_samples)),
            "tls_connections": tls,
            "read_calls": self.peer_state["read_calls"],
            "successful_reads": self.peer_state["successful_reads"],
            "first_applied_socket_timeout_seconds": self.peer_state["first_applied_socket_timeout_seconds"],
            "first_read_started_monotonic_ns": self.peer_state["first_read_started_monotonic_ns"],
            "latest_read_started_monotonic_ns": self.peer_state["latest_read_started_monotonic_ns"],
            "read_error": self.peer_state["read_error"],
            "close_observed": self.peer_state["close_completed_monotonic_ns"] is not None,
            "close_started_monotonic_ns": self.peer_state["close_started_monotonic_ns"],
            "close_completed_monotonic_ns": self.peer_state["close_completed_monotonic_ns"],
        }

    def failure_result(self, run_id, client, reason="sample_failed"):
        self.stage("attempt_failed", reason=reason)
        return {
            "run_id": run_id,
            "client": client,
            "mapped_backends": list(self.mapped_backends),
            "mapped_backend": self.mapped_backends[-1] if self.mapped_backends else None,
            "failure_diagnostics": {
                "observer": "roundtrip-failure-v1",
                "observer_overhead": (
                    "In-memory monotonic stage capture during the sample; no file writes, fsyncs, "
                    "or payload hashing occur before the sample exits. Peer reads update bounded counters; "
                    "frame summaries are assembled only after failure."
                ),
                "events": list(self.events),
                "children": [dict(child) for child in self.children],
                "peer": self._peer_observation(),
                "observer_errors": list(self.observer_errors),
            },
        }


def observed_roundtrip_sample(rt, sample_call, *, run_id, client, peer=None,
                              tls_records=None, frame_start=0, tls_start=0,
                              expected_payload=b"", tls_by_socket=None,
                              observer_sink=None, connections=1):
    observer = RoundtripFailureObserver(rt, run_id, peer, tls_records, frame_start, tls_start,
                                        expected_payload, tls_by_socket, connections)
    sample_completed = False
    try:
        with observer:
            result = sample_call()
            sample_completed = True
        if observer_sink is not None:
            observer_sink.append(observer)
        return result
    except Exception as exc:
        attach_observer_failure(exc, observer, run_id, client,
                                "post_sample_validation" if sample_completed else "sample")
        raise


def attach_observer_failure(exc, observer, run_id, client, reason):
    """Merge observer evidence into an existing partial result without dropping checks."""
    partial = getattr(exc, "partial_result", None)
    if not isinstance(partial, dict):
        partial = {"run_id": run_id, "client": client}
    try:
        observed = observer.failure_result(run_id, client, reason)
        for key in ("mapped_backend", "mapped_backends"):
            if key not in partial and observed.get(key) is not None:
                partial[key] = observed[key]
        partial["failure_diagnostics"] = observed["failure_diagnostics"]
    except Exception as observer_error:
        partial["failure_diagnostics"] = {
            "observer_error": type(observer_error).__name__,
            "events": list(observer.events),
            "children": [dict(child) for child in observer.children],
        }
    exc.partial_result = partial
    return partial


def failed_go_peer_observation(rt, server, run_id, connections):
    """Read the Go peer's existing stats command after a client sample has failed."""
    observation = {"kind": "go", "stats_records": [],
                   "peer_returncode": server.poll() if server is not None else None}
    if server is None:
        observation["stats_query_error"] = "peer process was not started"
        return observation
    try:
        server.stdin.write("stats\n")
        server.stdin.flush()
        records = json.loads(rt.line(server, timeout=2))
        observation["stats_records"] = [records.get(f"{run_id}-{index}")
                                         for index in range(connections)
                                         if f"{run_id}-{index}" in records]
    except Exception as exc:
        observation["stats_query_error"] = f"{type(exc).__name__}: {str(exc)[:256]}"
    observation["peer_returncode"] = server.poll()
    return observation


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
        smoke_diagnostics = smoke and isinstance(partial, dict) and "failure_diagnostics" in partial
        if smoke:
            if smoke_diagnostics:
                message = f"{type(exc).__name__}: round-trip sample failed; see failure_diagnostics"
            else:
                message = "\n".join(line for line in str(exc).splitlines()
                                     if not _is_numeric_line(line))
            trace = "traceback omitted in correctness-only smoke"
        else:
            message = str(exc)
            trace = traceback.format_exc()
        row.update(status="error", error_type=type(exc).__name__, error=message,
                   traceback=trace)
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
    smoke = status.get("options", {}).get("smoke", False)
    if smoke:
        error = f"{type(exc).__name__}: correctness-only smoke failed; inspect retained failure evidence"
        trace = "traceback omitted in correctness-only smoke"
    else:
        error = str(exc)
        trace = traceback.format_exc()
    status.update({
        "status": "failed",
        "finished_utc": now(),
        "error_type": type(exc).__name__,
        "error": error,
        "traceback": trace,
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
            python_tls_by_socket = {}
            python_tls_identity = None
            original_finish_request = python_peer.finish_request

            def capture_python_peer_tls(request, client_address):
                cipher = request.cipher()
                record = {
                    "connection_index": len(python_tls_observations),
                    "tls_version": request.version(),
                    "tls_cipher": cipher[0],
                    "tls_cipher_protocol": cipher[1],
                    "tls_cipher_bits": cipher[2],
                }
                python_tls_observations.append(record)
                python_tls_by_socket[id(request)] = record
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
                                observer_holder = []
                                try:
                                    result = observed_roundtrip_sample(
                                        rt,
                                        lambda: rt.sample(client_label(v, rt),
                                            client_binary(v, "roundtrip"), u, ca, p, w, rid, shared_env),
                                        run_id=rid, client=client_label(v, rt),
                                        peer=python_peer if w["peer"] == "python" else None,
                                        tls_records=python_tls_observations,
                                        frame_start=frame_start, tls_start=tls_start,
                                        expected_payload=payload, tls_by_socket=python_tls_by_socket,
                                        observer_sink=observer_holder,
                                        connections=w["connections"])
                                except Exception as exc:
                                    partial = getattr(exc, "partial_result", None)
                                    if (w["peer"] == "go" and isinstance(partial, dict) and
                                        "failure_diagnostics" in partial):
                                        partial["failure_diagnostics"]["peer"] = failed_go_peer_observation(
                                            rt, roundtrip_server, rid, w["connections"])
                                    raise

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

                                try:
                                    return validate_with_result(result, validate)
                                except Exception as exc:
                                    if observer_holder:
                                        observer = observer_holder[0]
                                        observer.wait_for_peer_close_after_sample()
                                        partial = attach_observer_failure(
                                            exc, observer, rid, client_label(v, rt),
                                            "post_sample_validation")
                                        if w["peer"] == "go":
                                            partial["failure_diagnostics"]["peer"] = failed_go_peer_observation(
                                                rt, roundtrip_server, rid, w["connections"])
                                    raise

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
