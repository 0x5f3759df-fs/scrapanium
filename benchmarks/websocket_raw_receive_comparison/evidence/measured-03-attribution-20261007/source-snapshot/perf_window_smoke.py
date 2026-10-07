#!/usr/bin/env python3
"""One full-workload, profile-only proof of the perf FD sampling window."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import runner

PERF = ROOT / "perf-package/extracted/usr/bin/perf"
PERF_LIBS = ROOT / "perf-package/extracted/usr/lib/x86_64-linux-gnu"
COUNT = runner.PERFORMANCE_COUNT
BATCH = 1
CLIENT = "candidate-bend"
MODE = "control"
BATCHES = runner.BATCHES
RUN_ID = "perf-window-proof-candidate-bend-control-flush1-20261007"


def save_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def save_text(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def perf_environment():
    env = dict(os.environ)
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = str(PERF_LIBS) + (":" + existing if existing else "")
    env["DEBUGINFOD_URLS"] = ""
    return env


def read_ack(fd, timeout=15):
    deadline = time.monotonic() + timeout
    expected = b"ack\n\0"
    received = bytearray()
    while len(received) < len(expected) and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(fd, len(expected) - len(received))
        if not chunk:
            raise RuntimeError("perf control acknowledgement pipe closed")
        received.extend(chunk)
    if bytes(received) != expected:
        raise RuntimeError(f"perf control returned an unexpected acknowledgement: {bytes(received)!r}")
    return time.monotonic_ns()


def parent_control(control_fd, ack_fd, command):
    start_ns = time.monotonic_ns()
    os.write(control_fd, (command + "\n").encode("ascii"))
    ack_ns = read_ack(ack_fd)
    return {"command": command, "write_monotonic_ns": start_ns,
            "ack_monotonic_ns": ack_ns}


def parse_client_metrics(output):
    wanted = {"client_interval_wall_ns", "total_elapsed_ns",
              "receive_assembly_ns", "validation_release_ns",
              "start_monotonic_us", "end_monotonic_us"}
    values = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in wanted:
            if key in values:
                raise RuntimeError(f"client duplicated timing field {key}")
            values[key] = int(value)
    if set(values) != wanted:
        raise RuntimeError(f"client timing fields are incomplete: {sorted(values)}")
    return values


def stop_process(process, timeout=15):
    if process is None:
        return "", "", None
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        return stdout, stderr, process.returncode
    return stdout, stderr, process.returncode


def stable_binding_inputs(info):
    return {
        key: value for key, value in info.items()
        if key not in ("pycache_prefix", "loaded_bytecode_cache_paths")
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--client", choices=runner.CLIENTS, default=CLIENT)
    parser.add_argument("--batch", choices=runner.BATCHES, type=int, default=BATCH)
    parser.add_argument("--run-id", default=RUN_ID)
    parser.add_argument("--build-dir", type=Path,
                        help="reuse an immutable, previously hashed build for a profile campaign")
    args = parser.parse_args()
    conflicts = [name for name in ("CC", "LD_PRELOAD", "SCRAPANIUM_CURL_DIR")
                 if os.environ.get(name)]
    if conflicts:
        parser.error("conflicting environment variables must be unset: " + ", ".join(conflicts))
    os.environ.pop("LD_LIBRARY_PATH", None)
    client_name = args.client
    batch = args.batch
    run_id = args.run_id
    if not run_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in run_id):
        parser.error("--run-id must contain only letters, digits, hyphens, or underscores")
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite profile proof directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "shim").mkdir()
    build_dir = args.build_dir.resolve() if args.build_dir else output_dir / "build"
    if args.build_dir is None:
        build_dir.mkdir(parents=True, exist_ok=False)
        (build_dir / "go-cache").mkdir()
    elif not build_dir.is_dir():
        raise FileNotFoundError(f"shared profile build directory is missing: {build_dir}")
    python_cache = output_dir / "python-bytecode-cache"
    python_cache.mkdir()
    source_before = runner.hash_sources()
    manifest = {
        "schema": 1,
        "status": "running",
        "purpose": "one profile-only full-workload capture with samples validated against the saved shim interval",
        "run_id": run_id,
        "client": client_name,
        "mode": MODE,
        "flush_batch": batch,
        "messages": COUNT,
        "message_bytes_including_sequence": runner.SIZE,
        "profile_only_excluded_from_primary_matrix": True,
        "peer_sample_floor_applied": False,
        "sampling": {
            "event": "cpu-clock:u",
            "frequency_hz": 499,
            "callchain": "frame-pointer",
            "clockid": "mono",
            "start_disabled": True,
            "control": "FD enable at wsattr_begin; FD disable inside wsattr_finish before saved end clock",
        },
        "profile_policy_for_following_campaign": {
            "fixed_runs_per_variant_flush": 12,
            "variant_flush_cells": 6,
            "planned_profile_runs_total": 72,
            "aggregate_sample_floor_per_variant_flush": 100,
            "adaptive_extension": False,
            "retain_under_floor_cells": True,
        },
        "source_sha256_before_build": source_before,
        "primary_campaign_started": False,
    }
    peer = client = perf = None
    control_read = control_write = ack_read = ack_write = None
    peer_profile_started = False
    try:
        compiler_root = runner.COMPILER_ROOT
        os.environ["BEND_SOURCE"] = str(compiler_root)
        os.environ["BUN"] = str(runner.BUN_PATH)
        env, library_path, backend_path = runner.phase.make_env(runner.CURL_PREFIX.resolve())
        env["BEND_SOURCE"] = str(compiler_root)
        env["BUN"] = str(runner.BUN_PATH)
        env["PYTHONPATH"] = str(runner.MATCHED_SITE)
        env["PYTHONPYCACHEPREFIX"] = str(python_cache.resolve())
        env.pop("ASAN_OPTIONS", None)
        env.pop("UBSAN_OPTIONS", None)
        if not backend_path.is_file():
            raise FileNotFoundError(f"pinned curl backend is missing: {backend_path}")

        clang = runner.shutil.which("clang")
        go = runner.shutil.which("go")
        if not clang or not go:
            raise FileNotFoundError("clang and go are required by the profile proof")
        compiler_info = runner.phase.compiler_provenance()
        binding_info = runner.phase.curl_binding_provenance(env)
        clang_info = runner.phase.executable_provenance(clang)
        go_info = runner.phase.executable_provenance(go, ("version",))
        clang_resource_info = runner.phase.clang_resource_provenance(clang)
        go_env = runner.phase.go_build_environment(go, build_dir / "go-cache")
        go_inputs_info = runner.phase.go_build_input_provenance(go, runner.PEER_SOURCE, go_env)
        curl_headers_info = runner.phase.directory_file_provenance(runner.CURL_PREFIX / "include")
        backend_sha = runner.phase.digest(backend_path)
        perf_env = perf_environment()
        perf_info = {
            "path": str(PERF),
            "sha256": runner.phase.digest(PERF),
            "version": runner.phase.command_output([str(PERF), "--version"], env=perf_env),
            "package_sha256": "c09e0183f04bab0a74015b0eb5055879ffa8c7b778ad5ab02224954bea2dfe46",
            "libraries": runner.phase.directory_file_provenance(PERF_LIBS),
        }
        build_manifest_path = build_dir / "build-manifest.json"
        profile_shim = build_dir / "libwsperf-control.so"
        if args.build_dir is None:
            build_args = argparse.Namespace(sanitize=False)
            bend_binaries, generated_c, normal_shim, _, peer_binary = runner.build_artifacts(
                build_args, build_dir)
            profile_shim_command = [
                clang, "-std=c11", "-O3", "-g", "-Wall", "-Wextra", "-Werror",
                "-fno-omit-frame-pointer", "-fPIC", "-shared",
                "-I" + str(runner.CURL_PREFIX / "include"),
                str(ROOT / "benchmarks/websocket_perf_shim.c"),
                "-ldl", "-pthread", "-o", str(profile_shim),
            ]
            subprocess.run(profile_shim_command, check=True)
        else:
            if not build_manifest_path.is_file():
                raise FileNotFoundError(f"shared profile build manifest is missing: {build_manifest_path}")
            shared_manifest = json.loads(build_manifest_path.read_text())
            bend_binaries = {
                name: build_dir / f"bench-ws-{name}"
                for name in runner.BEND_CLIENTS
            }
            generated_c = {
                name: binary.with_suffix(".generated.c")
                for name, binary in bend_binaries.items()
            }
            normal_shim = build_dir / "libwsattribution.so"
            peer_binary = build_dir / "websocket-attribution-peer"
            required = [*bend_binaries.values(), *generated_c.values(),
                        normal_shim, peer_binary, profile_shim]
            if any(not path.is_file() for path in required):
                raise FileNotFoundError("shared profile build is missing a pinned executable or generated source")
        binary_hashes = {
            "candidate_bend": runner.phase.digest(bend_binaries["candidate-bend"]),
            "candidate_generated_c": runner.phase.digest(generated_c["candidate-bend"]),
            "baseline_bend": runner.phase.digest(bend_binaries["baseline-bend"]),
            "baseline_generated_c": runner.phase.digest(generated_c["baseline-bend"]),
            "normal_shim": runner.phase.digest(normal_shim),
            "perf_control_shim": runner.phase.digest(profile_shim),
            "peer": runner.phase.digest(peer_binary),
        }
        toolchain_inputs = {
            "compiler": compiler_info,
            "curl_binding": stable_binding_inputs(binding_info),
            "clang": clang_info,
            "go": go_info,
            "clang_builtin_headers": clang_resource_info,
            "go_build_inputs": go_inputs_info,
            "curl_include_headers": curl_headers_info,
            "backend_sha256": backend_sha,
            "perf_sha256": perf_info["sha256"],
            "perf_libraries": perf_info["libraries"],
        }
        if args.build_dir is None:
            if (runner.hash_sources() != source_before or
                runner.phase.compiler_provenance() != compiler_info or
                stable_binding_inputs(runner.phase.curl_binding_provenance(env)) !=
                    stable_binding_inputs(binding_info) or
                runner.phase.executable_provenance(clang) != clang_info or
                runner.phase.executable_provenance(go, ("version",)) != go_info or
                runner.phase.clang_resource_provenance(clang) != clang_resource_info or
                runner.phase.go_build_input_provenance(go, runner.PEER_SOURCE, go_env) != go_inputs_info or
                runner.phase.directory_file_provenance(runner.CURL_PREFIX / "include") != curl_headers_info or
                runner.phase.digest(backend_path) != backend_sha or
                runner.phase.directory_file_provenance(PERF_LIBS) != perf_info["libraries"]):
                raise RuntimeError("a pinned input changed during compilation")
            shared_manifest = {
                "schema": 1,
                "source_sha256": source_before,
                "toolchain_inputs": toolchain_inputs,
                "binary_sha256": binary_hashes,
                "build_flags": {
                    "bend_native": ["-std=c11", "-O3", "-g", "-fno-omit-frame-pointer"],
                    "attribution_shim": ["-std=c11", "-O2", "-g", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared"],
                    "perf_control_shim": [
                        "-std=c11", "-O3", "-g", "-Wall", "-Wextra", "-Werror",
                        "-fno-omit-frame-pointer", "-fPIC", "-shared",
                    ],
                    "go_peer": ["build", "-trimpath"],
                },
            }
            save_json(build_manifest_path, shared_manifest)
        elif (shared_manifest.get("source_sha256") != source_before or
              shared_manifest.get("toolchain_inputs") != toolchain_inputs or
              shared_manifest.get("binary_sha256") != binary_hashes):
            raise RuntimeError("shared profile build does not match the current pinned inputs")
        build_manifest_sha = runner.phase.digest(build_manifest_path)
        manifest.update({
            "compiler": compiler_info,
            "clang": runner.phase.command_output([clang, "--version"]),
            "bend_flags": ["-O3", "-g", "-fno-omit-frame-pointer"],
            "perf_shim_flags": [
                "-std=c11", "-O3", "-g", "-Wall", "-Wextra", "-Werror",
                "-fno-omit-frame-pointer", "-fPIC", "-shared",
                "-I<CURL_PREFIX/include>", "-ldl", "-pthread",
            ],
            "backend": {"path": str(backend_path), "sha256": backend_sha},
            "binary_sha256": binary_hashes,
            "build_manifest_path": str(build_manifest_path),
            "build_manifest_sha256": build_manifest_sha,
            "shared_build_reused": args.build_dir is not None,
            "shared_build_directory": str(build_dir),
            "toolchain_executables": {"clang": clang_info, "go": go_info},
            "clang_builtin_headers": clang_resource_info,
            "go_build_inputs": go_inputs_info,
            "go_build_flags": ["build", "-trimpath", "-o", "<peer-binary>", str(runner.PEER_SOURCE)],
            "go_build_environment": {key: go_env[key] for key in (
                "GO111MODULE", "GOTOOLCHAIN", "GOPROXY", "CGO_ENABLED", "GOENV",
                "GOFLAGS", "GOOS", "GOARCH", "GOAMD64", "GOEXPERIMENT", "GOCACHE")},
            "curl_binding": binding_info,
            "curl_include_headers": curl_headers_info,
            "perf": perf_info,
        })
        cert_dir = output_dir / "tls"
        cert_dir.mkdir(exist_ok=False)
        _, ca = runner.phase.certificate(cert_dir)
        key = cert_dir / "key.pem"
        manifest["tls_certificate_sha256"] = runner.phase.digest(ca)
        peer, peer_url, peer_cpu, peer_trace = runner.start_peer(
            peer_binary, ca, key, output_dir)
        manifest["peer_profile_files"] = {"cpu": str(peer_cpu), "trace": str(peer_trace)}
        profile_start = runner.control_reply(peer, "profile-start 1")
        if profile_start.get("ok") is not True or profile_start.get("expected_profile_runs") != 1:
            raise RuntimeError(f"peer rejected the single-row profile window: {profile_start}")
        peer_profile_started = True

        control_read, control_write = os.pipe()
        ack_read, ack_write = os.pipe()
        record_path = output_dir / "shim" / (run_id + ".json")
        url = peer_url + "/stream?" + urlencode({
            "id": run_id, "count": COUNT, "size": runner.SIZE, "batch": batch,
            "client": client_name, "mode": MODE,
        })
        child_env = {
            **env,
            "SCRAPANIUM_BENCH_URL": url,
            "SCRAPANIUM_BENCH_CA": str(ca),
            "SCRAPANIUM_BENCH_COUNT": str(COUNT),
            "SCRAPANIUM_BENCH_BODY": str(output_dir / "corpus-body.bin"),
            "SCRAPANIUM_ATTRIBUTION_MODE": MODE,
            "SCRAPANIUM_BENCH_PHASE_MODE": MODE,
            "SCRAPANIUM_ATTRIBUTION_RECORD": str(record_path),
            "SCRAPANIUM_PERF_CONTROL_FD": str(control_write),
            "SCRAPANIUM_PERF_ACK_FD": str(ack_read),
            "LD_PRELOAD": str(profile_shim),
        }
        body_path = output_dir / "corpus-body.bin"
        runner.write_bytes_exclusive(
            body_path, bytes((index * 31) % 128 for index in range(runner.SIZE - 8)))
        client_command = (
            [str(bend_binaries[client_name]), "--threads", "1"]
            if client_name in runner.BEND_CLIENTS else
            [str(runner.MATCHED_PYTHON), str(runner.PYTHON_CLIENT)]
        )
        client = subprocess.Popen(
            client_command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=child_env, pass_fds=(control_write, ack_read),
        )
        ready = runner.client_line(client, timeout=45)
        if ready != "ready":
            raise RuntimeError(f"unexpected client readiness line: {ready!r}")
        backend = runner.phase.mapped_backend(client)
        if backend.get("sha256") != backend_sha:
            raise RuntimeError("client did not map the pinned stock curl DSO")

        perf_data = output_dir / "perf.data"
        perf_command = [
            str(PERF), "record", "-D", "-1", "-p", str(client.pid),
            "--control", f"fd:{control_read},{ack_write}",
            "-e", "cpu-clock:u", "-F", "499", "--call-graph", "fp",
            "--clockid", "mono", "-N", "--buildid-all",
            "-o", str(perf_data),
        ]
        perf = subprocess.Popen(
            perf_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=perf_env,
            pass_fds=(control_read, ack_write),
        )
        os.close(control_read)
        control_read = None
        os.close(ack_write)
        ack_write = None
        initial_disable = parent_control(control_write, ack_read, "disable")
        manifest["initial_perf_disable_ack"] = initial_disable
        gate_ns = time.monotonic_ns()
        client.stdin.write("x")
        client.stdin.flush()
        stdout, stderr = client.communicate(timeout=150)
        parent_done_ns = time.monotonic_ns()
        returncode = client.returncode
        client = None
        record = runner.record_for_run(record_path)
        peer_record = runner.peer_stats(peer, run_id)
        perf_stdout, perf_stderr, perf_returncode = stop_process(perf)
        perf = None
        os.close(control_write)
        control_write = None
        os.close(ack_read)
        ack_read = None
        save_text(output_dir / "client.stdout.log", ready + "\n" + stdout)
        save_text(output_dir / "client.stderr.log", stderr)
        save_text(output_dir / "perf.record.stdout.log", perf_stdout)
        save_text(output_dir / "perf.record.stderr.log", perf_stderr)
        if returncode != 0:
            raise RuntimeError(f"client exited with status {returncode}: {stderr[-2000:]}")
        if perf_returncode not in (0, -signal.SIGINT, 130):
            raise RuntimeError(f"perf exited unexpectedly with status {perf_returncode}")
        metrics = parse_client_metrics(stdout)
        client_result = {
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "parent_gate_monotonic_ns": gate_ns,
            "parent_done_monotonic_ns": parent_done_ns,
        }
        errors = runner.validate_peer_identity(peer_record, {
            "kind": "attribution_sample",
            "run_id": run_id, "client": client_name, "mode": MODE, "batch": batch,
        })
        errors.extend(runner.validate_peer_positive(peer_record, COUNT, batch))
        receive_api = "curl_easy_recv" if client_name == "candidate-bend" else "curl_ws_recv"
        errors.extend(runner.validate_shim_positive(
            record, client_result, MODE, COUNT * runner.SIZE,
            peer_record.get("data_plaintext_bytes_written"), receive_api))
        if errors:
            raise RuntimeError("workload validation failed: " + "; ".join(errors))
        interval = record["interval"]
        ctl = record.get("perf_control")
        if not isinstance(ctl, dict) or ctl.get("configured") is not True or ctl.get("error") is not False:
            raise RuntimeError(f"shim perf-control metadata is invalid: {ctl!r}")
        times = [interval.get("start_monotonic_ns"),
                 ctl.get("enable_command_monotonic_ns"), ctl.get("enable_ack_monotonic_ns"),
                 ctl.get("disable_command_monotonic_ns"), ctl.get("disable_ack_monotonic_ns"),
                 interval.get("end_monotonic_ns")]
        if not all(type(value) is int and value > 0 for value in times) or times != sorted(times):
            raise RuntimeError(f"perf command/ack times do not fit inside the shim interval: {times!r}")
        if not (initial_disable["ack_monotonic_ns"] < gate_ns < times[0]):
            raise RuntimeError("perf initial-disabled handshake did not precede the client gate")

        script_command = [
            str(PERF), "script", "-F", "time,event,ip,sym,dso", "-i", str(perf_data),
        ]
        script_result = subprocess.run(
            script_command, check=True, text=True, capture_output=True, env=perf_env)
        perf_script = script_result.stdout
        save_text(output_dir / "perf.script.txt", perf_script)
        sample_times_ns = []
        sample_pattern = re.compile(r"^\s*([0-9]+\.[0-9]+): cpu-clock:u:")
        for line in perf_script.splitlines():
            match = sample_pattern.match(line)
            if match:
                seconds, fraction = match.group(1).split(".")
                sample_times_ns.append(int(seconds) * 1_000_000_000 +
                                       int((fraction + "000000000")[:9]))
        if not sample_times_ns:
            raise RuntimeError("perf data contains no cpu-clock:u samples")
        tolerance_ns = 1000
        outside = [value for value in sample_times_ns
                   if value < interval["start_monotonic_ns"] - tolerance_ns or
                   value > interval["end_monotonic_ns"] + tolerance_ns]
        if outside:
            raise RuntimeError(f"{len(outside)} perf samples fall outside saved client interval")
        report_result = subprocess.run(
            [str(PERF), "report", "--stdio", "--header", "-i", str(perf_data)],
            check=True, text=True, capture_output=True, env=perf_env)
        report = report_result.stdout + report_result.stderr
        save_text(output_dir / "perf.report.txt", report)
        lost_match = re.search(r"Total Lost Samples:\s*(\d+)", report)
        samples_match = re.search(r"Samples:\s*(\d+)\s+of event 'cpu-clock:u'", report)
        if not lost_match or not samples_match:
            raise RuntimeError("perf report did not expose total lost and retained sample counts")
        lost_samples = int(lost_match.group(1))
        report_samples = int(samples_match.group(1))
        if report_samples != len(sample_times_ns):
            raise RuntimeError("perf report and perf script disagree on the retained sample count")
        if lost_samples != 0:
            raise RuntimeError(f"perf reported lost samples: {lost_samples}")

        tools_unchanged = (
            runner.phase.compiler_provenance() == compiler_info and
            runner.phase.curl_binding_provenance(env) == binding_info and
            runner.phase.executable_provenance(clang) == clang_info and
            runner.phase.executable_provenance(go, ("version",)) == go_info and
            runner.phase.clang_resource_provenance(clang) == clang_resource_info and
            runner.phase.go_build_input_provenance(go, runner.PEER_SOURCE, go_env) == go_inputs_info and
            runner.phase.directory_file_provenance(runner.CURL_PREFIX / "include") == curl_headers_info and
            runner.phase.digest(backend_path) == backend_sha and
            runner.phase.digest(PERF) == perf_info["sha256"] and
            runner.phase.directory_file_provenance(PERF_LIBS) == perf_info["libraries"]
        )
        binaries_unchanged = {
            "candidate_bend": runner.phase.digest(bend_binaries["candidate-bend"]),
            "candidate_generated_c": runner.phase.digest(generated_c["candidate-bend"]),
            "baseline_bend": runner.phase.digest(bend_binaries["baseline-bend"]),
            "baseline_generated_c": runner.phase.digest(generated_c["baseline-bend"]),
            "normal_shim": runner.phase.digest(normal_shim),
            "perf_control_shim": runner.phase.digest(profile_shim),
            "peer": runner.phase.digest(peer_binary),
        } == binary_hashes
        manifest.update({
            "status": "complete",
            "build_manifest_unchanged": runner.phase.digest(build_manifest_path) == build_manifest_sha,
            "source_sha256_after_run": runner.hash_sources(),
            "binary_sha256_after_run": binary_hashes,
            "backend_sha256_after_run": runner.phase.digest(backend_path),
            "toolchain_and_dependency_hashes_unchanged": tools_unchanged,
            "source_and_binary_hashes_unchanged": source_before == runner.hash_sources() and binaries_unchanged,
            "client_returncode": returncode,
            "client_metrics": metrics,
            "client_interval": interval,
            "perf_control": ctl,
            "perf_clockid": "mono; timestamps parsed by perf script and checked against CLOCK_MONOTONIC",
            "perf_sample_count": len(sample_times_ns),
            "perf_sample_time_range_monotonic_ns": [min(sample_times_ns), max(sample_times_ns)],
            "perf_samples_outside_client_interval": 0,
            "perf_lost_samples": lost_samples,
            "peer_record": peer_record,
            "peer_cpu_profile_floor_met": False,
            "validation_errors": [],
            "profile_script_sha256": runner.phase.digest(output_dir / "perf.script.txt"),
            "profile_report_sha256": runner.phase.digest(output_dir / "perf.report.txt"),
            "profile_data_sha256": runner.phase.digest(perf_data),
            "primary_campaign_started": False,
        })
        if (manifest["source_and_binary_hashes_unchanged"] is not True or
            not tools_unchanged or manifest["build_manifest_unchanged"] is not True):
            raise RuntimeError("source, binary, or toolchain hash changed during the WSS window proof")
        if peer_profile_started:
            reply = runner.control_reply(peer, "profile-stop", timeout=90)
            peer_profile_started = False
            manifest["peer_profile_summary"] = reply.get("profile")
            if reply.get("ok") is not True:
                raise RuntimeError(f"peer profile did not stop cleanly: {reply}")
        shutdown = runner.control_reply(peer, "shutdown")
        peer.wait(timeout=10)
        if shutdown.get("ok") is not True or peer.returncode != 0:
            raise RuntimeError("peer did not shut down cleanly")
        peer = None
    except BaseException as error:
        manifest["status"] = "failed"
        manifest["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if client is not None and client.poll() is None:
            client.kill()
            client.communicate()
        if perf is not None:
            out, err, rc = stop_process(perf, timeout=15)
            try:
                save_text(output_dir / "perf.record.stdout.log", out)
                save_text(output_dir / "perf.record.stderr.log", err)
            except FileExistsError:
                pass
            manifest["perf_failure_cleanup"] = {"returncode": rc}
        if peer is not None:
            try:
                if peer_profile_started:
                    runner.control_reply(peer, "profile-stop", timeout=90)
                runner.control_reply(peer, "shutdown")
                peer.wait(timeout=10)
            except BaseException:
                if peer.poll() is None:
                    peer.kill()
                    peer.communicate()
        for fd in (control_read, control_write, ack_read, ack_write):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        manifest["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not manifest.get("source_sha256_after_run"):
            manifest["source_sha256_after_run"] = runner.hash_sources()
        try:
            save_json(output_dir / "manifest.json", manifest)
        except FileExistsError:
            pass
    if manifest["status"] != "complete":
        raise RuntimeError(manifest["failure"])


if __name__ == "__main__":
    main()
