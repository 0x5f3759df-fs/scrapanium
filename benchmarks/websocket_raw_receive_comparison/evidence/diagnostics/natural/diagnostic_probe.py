#!/usr/bin/env python3
"""Diagnostic-only reproduction of the candidate Python-peer round-trip failure window."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit

OUT = Path(__file__).resolve().parent
MEASURED = Path("/home/baidu/scrapanium-experiments/wss-raw-recv-comparison-20261005/candidate-c492-wss-recv-measured-01-20261006")
MANIFEST = MEASURED / "manifest.json"
ATTEMPTS = MEASURED / "attempts.jsonl"
MANIFEST_SHA256 = "74f31eaef8295e097bdb6f45310d1a1115c21e21e00d719c4515ff359209c3a7"
ATTEMPTS_SHA256 = "57c6f93828db09192ac998254337799543900dbd26ad7a39c3b4e9e2ac2f12a6"
CLIENT_SHA256 = "c18e28a46f79dfc9628bf0b64042a9a8a11eecb151509c2056b950c2c3ed641e"
BACKEND_PATH = "/mnt/c/Users/Baidu/Projetos/scrapanium/.deps/curl/libcurl-impersonate.so.4.8.0"
BACKEND_SHA256 = "bec91922e5f79213bf0e4a6dbb2b4410e6cc92cbee4a72759d6b387f14cad3e3"
PAYLOAD = b"scrapanium-websocket-benchmark"
COUNT = 1000
SOURCE_PATHS = (
    "benchmarks/websocket.bend", "benchmarks/websocket.py",
    "benchmarks/websocket_python.py", "tests/lab.py", "tests/ws_lab.py",
    "native/websocket.inc.c",
)
CANDIDATE_NATIVE_SHA256 = "0f3c4e2c2844975f4bfd63adb1ba0bfb901f6b97deff640c6a80498653ad82f8"

def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def stamp(stage, **extra):
    return {"stage": stage, "utc": utc(), "monotonic_ns": time.monotonic_ns(), **extra}

def encode(row):
    return (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()

def append(name, row):
    with open(OUT / name, "ab") as f:
        f.write(encode(row))
        f.flush()
        os.fsync(f.fileno())

def append_many(name, rows):
    if not rows:
        return
    with open(OUT / name, "ab") as f:
        f.write(b"".join(encode(row) for row in rows))
        f.flush()
        os.fsync(f.fileno())

def create_empty(name):
    with open(OUT / name, "xb"):
        pass

def create_json(name, row):
    with open(OUT / name, "xb") as f:
        f.write(encode(row))
        f.flush()
        os.fsync(f.fileno())

def status(row):
    tmp = OUT / "status.json.tmp"
    with open(tmp, "wb") as f:
        f.write(encode(row))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUT / "status.json")

def pressure_snapshot():
    snap = {"utc": utc(), "monotonic_ns": time.monotonic_ns(),
            "loadavg": os.getloadavg(), "proc_count": len(os.listdir("/proc"))}
    for name in ("meminfo", "pressure/memory"):
        try:
            snap[name.replace("/", "_")] = (Path("/proc") / name).read_text()
        except OSError as exc:
            snap[name.replace("/", "_")] = {"read_error": repr(exc)}
    return snap

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def fresh():
    allowed = {"diagnostic_probe.py", "__pycache__"}
    extra = sorted(item.name for item in OUT.iterdir() if item.name not in allowed)
    if extra:
        raise RuntimeError("refusing to overwrite existing evidence: " + repr(extra))
    cache = OUT / "__pycache__"
    if cache.exists():
        shutil.rmtree(cache)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pause-after-hash", type=float, default=0.0,
                        help="optional intentional-delay control; natural attempt uses zero")
    args = parser.parse_args()
    if not 0 <= args.pause_after_hash <= 20:
        parser.error("pause must be between 0 and 20 seconds")

    fresh()
    harness_hash = digest(__file__)
    initial = {"schema": 1, "state": "initializing", "started_utc": utc(),
               "harness_sha256": harness_hash, "attempts": 0, "performance_claim": False}
    create_json("status.json", initial)
    for filename in ("events.jsonl", "peer_events.jsonl", "attempts.jsonl"):
        create_empty(filename)
    append("events.jsonl", stamp("preflight_start"))
    try:
        measured = json.loads(MANIFEST.read_text())
        manifest_hash = digest(MANIFEST)
        attempts_hash = digest(ATTEMPTS)
        if manifest_hash != MANIFEST_SHA256:
            raise RuntimeError(f"measured manifest changed: {manifest_hash}")
        if attempts_hash != ATTEMPTS_SHA256:
            raise RuntimeError(f"measured attempts changed: {attempts_hash}")
        baseline = Path(measured["baseline"]["path"])
        candidate = Path(measured["candidate"]["path"])
        binary = MEASURED / "build" / "candidate-roundtrip"
        sources = {}
        for relative in SOURCE_PATHS:
            for label, root in (("baseline", baseline), ("candidate", candidate)):
                actual = digest(root / relative)
                expected = (CANDIDATE_NATIVE_SHA256 if label == "candidate" and
                            relative == "native/websocket.inc.c" else
                            measured[label]["source_sha256"][relative])
                if actual != expected:
                    raise RuntimeError(f"{label} source mismatch {relative}: {actual} != {expected}")
                sources[f"{label}:{relative}"] = actual
        binary_hash = digest(binary)
        if binary_hash != CLIENT_SHA256 or binary_hash != measured["binary_sha256"]["candidate_roundtrip"]:
            raise RuntimeError("candidate client binary differs from frozen measured manifest")
        if digest(sys.executable) != measured["environment"]["python_sha256"]:
            raise RuntimeError("diagnostic Python executable differs from frozen measured manifest")
        if measured["backend"] != {"path": BACKEND_PATH, "sha256": BACKEND_SHA256}:
            raise RuntimeError("measured backend pin differs from the reviewed DSO identity")
        runtime = measured["environment"]["inherited_runtime_paths"]
        child_env = os.environ.copy()
        selected_keys = ("PYTHONPATH", "BEND_SOURCE", "BUN", "CC", "SCRAPANIUM_CURL_DIR", "LD_LIBRARY_PATH")
        for key in selected_keys:
            child_env[key] = runtime[key]
        evidence = {
            "schema": 1, "mode": "diagnostic_only", "performance_claim": False,
            "ratio_calculations": False, "created_utc": utc(),
            "harness_path": str(Path(__file__).resolve()), "harness_sha256": harness_hash,
            "measured_manifest_path": str(MANIFEST), "measured_manifest_sha256": manifest_hash,
            "original_attempts_path": str(ATTEMPTS), "original_attempts_sha256": attempts_hash,
            "baseline_head": measured["baseline"]["head"],
            "sources_sha256": sources, "candidate_client": str(binary),
            "candidate_client_sha256": binary_hash,
            "expected_backend": {"path": BACKEND_PATH, "sha256": BACKEND_SHA256},
            "python": {"executable": sys.executable, "sha256": digest(sys.executable),
                       "version": sys.version, "platform": platform.platform()},
            "selected_runtime_environment": {key: child_env.get(key) for key in selected_keys},
            "payload_hex": PAYLOAD.hex(), "payload_length": len(PAYLOAD),
            "warmup_exchanges": 1, "timed_exchanges": COUNT, "expected_peer_frames": COUNT + 1,
            "peer_socket_timeout_seconds": 4,
            "peer": "frozen baseline tests/ws_lab.py; instrumentation wraps read_frame and Handler.do_GET only",
            "timing_use": "parent monotonic stamps explain spawn/ready/map/hash/gate/exit ordering only; child stdout is retained raw",
            "observer_overhead": "peer payload/frame events and all spawn-through-exit parent stamps are buffered in memory and flushed after child exit; abrupt host/process loss can leave status running without those buffers; instrumentation may perturb protocol timing and is not a performance sample",
            "intentional_pause_after_hash_seconds": args.pause_after_hash,
            "host_pressure": pressure_snapshot(),
            "original_measured_failure_preserved": True,
        }
        create_json("manifest.json", evidence)
        status({**initial, "state": "running", "preflight_utc": utc(), "attempts": 1})
        append("events.jsonl", stamp("preflight_complete", client_sha256=binary_hash,
                                    source_count=len(sources)))
    except BaseException as exc:
        append("events.jsonl", stamp("preflight_error", error=repr(exc)))
        (OUT / "preflight-error.txt").write_text(traceback.format_exc())
        status({**initial, "state": "error", "error": repr(exc), "finished_utc": utc()})
        raise

    attempt_id = "natural" if args.pause_after_hash == 0 else "intentional-delay-control"
    stdout_path, stderr_path = OUT / f"{attempt_id}.stdout", OUT / f"{attempt_id}.stderr"
    attempt = {"attempt_id": attempt_id, "state": "started",
               "command": [str(binary), "--threads", "1"],
               "payload_hex": PAYLOAD.hex(), "timed_exchanges": COUNT,
               "started": stamp("attempt_start")}
    append("attempts.jsonl", attempt)
    append("events.jsonl", attempt["started"])
    peer_log, peer_lock, local = [], threading.Lock(), threading.local()
    peer_closed = threading.Event()
    pending_events = []
    def defer(stage, **fields):
        pending_events.append(stamp(stage, **fields))
    server = proc = tempdir = None
    stdout_parts, stderr_parts = [], []
    output_threads = []
    tls = []
    frames = []
    returncode = None
    try:
        sys.path.insert(0, str(baseline / "tests"))
        sys.dont_write_bytecode = True
        lab = load_module("diag_lab", baseline / "tests" / "lab.py")
        ws = load_module("diag_ws_lab", baseline / "tests" / "ws_lab.py")
        old_read, old_get = ws.read_frame, ws.Handler.do_GET

        def peer_event(name, **fields):
            row = stamp(name, attempt_id=attempt_id,
                        request_id=getattr(local, "request_id", None), **fields)
            with peer_lock:
                peer_log.append(row)

        def observed_read_frame(stream):
            began = time.monotonic_ns()
            if not getattr(local, "timeout_observed", False):
                sockio = getattr(getattr(stream, "raw", None), "_sock", None)
                try:
                    timeout = sockio.gettimeout() if sockio is not None else None
                except BaseException as exc:
                    timeout = "capture_error: " + repr(exc)
                peer_event("read_timeout_at_first_frame", socket_timeout_seconds=timeout)
                local.timeout_observed = True
            try:
                result = old_read(stream)
            except BaseException as exc:
                peer_event("read_frame_error", started_monotonic_ns=began,
                           exception_type=type(exc).__name__,
                           is_socket_timeout=isinstance(exc, socket.timeout), error=str(exc))
                raise
            kind, data, final = result
            peer_event("frame_received", started_monotonic_ns=began, opcode=kind,
                       final=final, payload_length=len(data), payload_hex=data.hex(),
                       payload_sha256=hashlib.sha256(data).hexdigest())
            return result

        def observed_get(handler):
            local.request_id = parse_qs(urlsplit(handler.path).query).get("id", [None])[0]
            local.timeout_observed = False
            identity = {"request_id": local.request_id}
            try:
                identity.update({"tls_version": handler.connection.version(),
                                 "tls_cipher": handler.connection.cipher(),
                                 "timeout_before_frozen_handler": handler.connection.gettimeout()})
            except BaseException as exc:
                identity["tls_capture_error"] = repr(exc)
            tls.append(identity)
            peer_event("handler_start", path=handler.path, peer_frames=len(handler.server.frames),
                       tls=identity)
            try:
                return old_get(handler)
            finally:
                try:
                    timeout_after = handler.connection.gettimeout()
                except BaseException as exc:
                    timeout_after = "capture_error: " + repr(exc)
                peer_event("handler_exit", peer_frames=len(handler.server.frames),
                           timeout_after_frozen_handler=timeout_after)

        ws.read_frame, ws.Handler.do_GET = observed_read_frame, observed_get
        tempdir = tempfile.TemporaryDirectory(prefix="scrapanium-wss-diag-")
        context, ca_path = lab.certificate(Path(tempdir.name) / "cert")
        server, url = ws.start_ws(context)
        original_shutdown_request = server.shutdown_request
        def observed_shutdown_request(request):
            peer_event("peer_socket_close_start", peer_frames=len(server.frames))
            try:
                return original_shutdown_request(request)
            finally:
                peer_event("peer_socket_close_complete", peer_frames=len(server.frames))
                peer_closed.set()
        server.shutdown_request = observed_shutdown_request
        append("events.jsonl", stamp("peer_listening", url=url, socket_timeout_seconds=4))
        payload_path = Path(tempdir.name) / "payload.bin"
        payload_path.write_bytes(PAYLOAD)
        run_id = "diag-" + str(time.time_ns())
        env = {**child_env, "SCRAPANIUM_BENCH_URL": f"{url}/echo?id={run_id}",
               "SCRAPANIUM_BENCH_CA": ca_path, "SCRAPANIUM_BENCH_COUNT": str(COUNT),
               "SCRAPANIUM_BENCH_PAYLOAD": str(payload_path)}
        proc = subprocess.Popen([str(binary), "--threads", "1"], cwd=candidate, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        defer("client_spawned", pid=proc.pid, command=attempt["command"])
        ready_lines = queue.Queue()
        def drain(pipe, parts, label):
            for line in iter(pipe.readline, b""):
                parts.append(line)
                ready_lines.put((label, line))
            ready_lines.put((label, None))
        output_threads = [
            threading.Thread(target=drain, args=(proc.stdout, stdout_parts, "stdout"), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr, stderr_parts, "stderr"), daemon=True),
        ]
        for thread in output_threads:
            thread.start()
        ready = False
        end_wait = time.monotonic() + 30
        while time.monotonic() < end_wait:
            try:
                stream, line = ready_lines.get(timeout=max(0.01, end_wait - time.monotonic()))
            except queue.Empty:
                break
            if stream == "stdout" and line is not None and line.strip() == b"ready":
                ready = True
                break
            if line is None and proc.poll() is not None:
                break
        defer("client_ready_observation", pid=proc.pid, ready=ready, child_exit_code=proc.poll())
        if not ready:
            raise RuntimeError("client failed to emit ready within 30 seconds")
        defer("maps_read_start", pid=proc.pid)
        maps = Path(f"/proc/{proc.pid}/maps").read_text()
        paths = {line.split()[-1] for line in maps.splitlines()
                 if "libcurl-impersonate.so" in line and line.split()[-1].startswith("/")}
        defer("maps_read_complete", match_count=len(paths))
        if len(paths) != 1:
            raise RuntimeError("expected exactly one mapped libcurl-impersonate DSO: " + repr(sorted(paths)))
        dso = Path(paths.pop()).resolve()
        defer("dso_hash_start", path=str(dso))
        dso_bytes = dso.read_bytes()
        dso_hash = hashlib.sha256(dso_bytes).hexdigest()
        defer("dso_hash_complete", path=str(dso), sha256=dso_hash, byte_count=len(dso_bytes))
        if str(dso) != BACKEND_PATH or dso_hash != BACKEND_SHA256:
            raise RuntimeError(f"mapped DSO mismatch: {dso} {dso_hash}")
        if args.pause_after_hash:
            defer("intentional_pause_start", seconds=args.pause_after_hash)
            time.sleep(args.pause_after_hash)
            defer("intentional_pause_end")
        defer("gate_write_start", pid=proc.pid)
        proc.stdin.write(b"x")
        proc.stdin.flush()
        proc.stdin.close()
        defer("gate_write_complete", pid=proc.pid)
        try:
            returncode = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            returncode = proc.wait(timeout=10)
            raise RuntimeError("candidate client exceeded 120-second diagnostic bound")
        for thread in output_threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in output_threads):
            raise RuntimeError("client output capture threads did not finish")
        defer("client_exit", pid=proc.pid, returncode=returncode)
        peer_closed_observed = peer_closed.wait(8)
        defer("peer_close_observation", completed=peer_closed_observed)
        frames = list(server.frames)
        passed = (len(frames) == COUNT + 1 and all(row == (2, PAYLOAD, True) for row in frames))
        attempt.update({"state": "ok" if returncode == 0 and passed and peer_closed_observed else "error",
                        "returncode": returncode, "peer_frame_count": len(frames),
                        "peer_checker_passed": passed, "peer_tls": list(tls),
                        "peer_close_observed": peer_closed_observed,
                        "error": None if returncode == 0 and passed and peer_closed_observed else
                            f"returncode={returncode}; exact_frame_check={passed}; peer_close={peer_closed_observed}; frame_count={len(frames)}"})
    except BaseException as exc:
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except BaseException:
                pass
        for thread in output_threads:
            thread.join(timeout=5)
        if server is not None:
            if proc is not None:
                peer_closed_observed = peer_closed.wait(8)
                defer("peer_close_after_client_error", completed=peer_closed_observed)
            frames = list(server.frames)
        attempt.update({"state": "error", "error": repr(exc),
                        "traceback": traceback.format_exc(), "returncode": proc.poll() if proc else None,
                        "peer_frame_count": len(frames), "peer_checker_passed": False,
                        "peer_tls": list(tls), "peer_close_observed": peer_closed.is_set()})
    finally:
        append_many("events.jsonl", pending_events)
        pending_events.clear()
        stdout_path.write_bytes(b"".join(stdout_parts))
        stderr_path.write_bytes(b"".join(stderr_parts))
        if server is not None:
            server.shutdown()
            server.server_close()
        if tempdir is not None:
            tempdir.cleanup()
        append("events.jsonl", stamp("postcheck_start"))
        postcheck = {"passed": False, "hashes": {}, "errors": []}
        try:
            postcheck["hashes"]["harness"] = digest(__file__)
            postcheck["hashes"]["measured_manifest"] = digest(MANIFEST)
            postcheck["hashes"]["original_attempts"] = digest(ATTEMPTS)
            postcheck["hashes"]["candidate_client"] = digest(binary)
            postcheck["hashes"]["backend"] = digest(BACKEND_PATH)
            current_sources = {}
            for relative in SOURCE_PATHS:
                for label, root in (("baseline", baseline), ("candidate", candidate)):
                    current_sources[f"{label}:{relative}"] = digest(root / relative)
            postcheck["hashes"]["sources"] = current_sources
            pinned = {
                "harness": harness_hash,
                "measured_manifest": MANIFEST_SHA256,
                "original_attempts": ATTEMPTS_SHA256,
                "candidate_client": CLIENT_SHA256,
                "backend": BACKEND_SHA256,
            }
            for key, expected in pinned.items():
                if postcheck["hashes"][key] != expected:
                    postcheck["errors"].append(f"{key} changed: {postcheck['hashes'][key]} != {expected}")
            for relative in SOURCE_PATHS:
                for label in ("baseline", "candidate"):
                    key = f"{label}:{relative}"
                    expected = (CANDIDATE_NATIVE_SHA256 if label == "candidate" and
                                relative == "native/websocket.inc.c" else
                                measured[label]["source_sha256"][relative])
                    if current_sources[key] != expected:
                        postcheck["errors"].append(f"{key} changed: {current_sources[key]} != {expected}")
            postcheck["passed"] = not postcheck["errors"]
        except BaseException as exc:
            postcheck["errors"].append(repr(exc))
        attempt["postcheck"] = postcheck
        if not postcheck["passed"]:
            attempt["state"] = "error"
            previous = attempt.get("error")
            attempt["error"] = (str(previous) + "; " if previous else "") + "postcheck failed: " + repr(postcheck["errors"])
        append("events.jsonl", stamp("postcheck_complete", passed=postcheck["passed"],
                                     errors=postcheck["errors"]))
        attempt["finished"] = stamp("attempt_finish")
        with peer_lock:
            append_many("peer_events.jsonl", peer_log)
        append("attempts.jsonl", attempt)
        append("events.jsonl", attempt["finished"])
        final_state = "complete" if attempt["state"] == "ok" else "error"
        status({"schema": 1, "state": final_state, "finished_utc": utc(),
                "attempts": 1, "attempt_id": attempt_id, "attempt_state": attempt["state"],
                "performance_claim": False})
    if attempt["state"] != "ok":
        raise SystemExit(1)

if __name__ == "__main__":
    main()

