#!/usr/bin/env python3
"""Fixed eight-run, diagnostic-only curl_ws_recv call-count probe.

This reuses the frozen attribution clients and shim in control mode. It records
the shim's exact successful recv bytes/call counts while retaining the clients'
full sequence/opcode/byte checks and immediate buffer release. The total client
clock is retained only as context; this interposed probe is not performance
evidence and is not compared with uninstrumented throughput samples.
"""
import datetime
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "build/wss-recv-coalescing"
sys.path.insert(0, str(ROOT / "benchmarks"))
attr = importlib.import_module("websocket_attribution")
phase = attr.phase

BASELINE_ROOT = BASE / "client-roots/baseline"
CANDIDATE_ROOT = BASE / "client-roots/candidate"
BASELINE_PREFIX = BASE / "backends/baseline"
CANDIDATE_PREFIX = BASE / "backends/candidate"
MATCHED_PYTHON = ROOT / ".deps/venv-matched/bin/python"
PINNED_BUN = ROOT / ".deps/bun/node_modules/@oven/bun-linux-x64-baseline/bin/bun"
PINNED_BEND = ROOT / ".deps/bend"
CLANG = Path("/usr/bin/clang")
PEER_SOURCE = ROOT / "benchmarks/websocket_stream_server.go"
SHIM_SOURCE = ROOT / "benchmarks/websocket_attribution_shim.c"
BODY_SIZE = 65536
COUNT = 1024
EXPECTED_PAYLOAD = BODY_SIZE * COUNT

# Palindromic order crosses backend/client pairs in both flush conditions.
PLAN = (
    ("baseline", "scrapanium-bend", 1),
    ("candidate", "scrapanium-bend", 1),
    ("baseline", "curl_cffi-matched", 1),
    ("candidate", "curl_cffi-matched", 1),
    ("candidate", "curl_cffi-matched", 64),
    ("baseline", "curl_cffi-matched", 64),
    ("candidate", "scrapanium-bend", 64),
    ("baseline", "scrapanium-bend", 64),
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json_new(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def source_inventory():
    main = attr.hash_sources()
    for path in (PEER_SOURCE, ROOT / "benchmarks/websocket_streaming.py",
                 ROOT / "benchmarks/websocket_stream_python.py", Path(__file__).resolve()):
        main[str(path.relative_to(ROOT))] = digest(path)
    isolated = {}
    relevant = [
        "dependencies.json", "scrapanium.bend", "http.bend", "scripts/build.py",
        "benchmarks/websocket_attribution.bend", "benchmarks/websocket_attribution_clock.c",
        "benchmarks/websocket.bend",
        "native/scrapanium.c",
    ]
    for label, client_root in (("baseline", BASELINE_ROOT), ("candidate", CANDIDATE_ROOT)):
        paths = [client_root / item for item in relevant]
        paths.extend(sorted((client_root / "native").glob("*")))
        isolated[label] = {
            str(path.relative_to(client_root)): digest(path)
            for path in paths if path.is_file()
        }
    return {"main": main, "isolated_clients": isolated}


def tree_inventory(root):
    root = Path(root).resolve()
    found = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"backend snapshot contains a non-regular file: {path}")
            found[str(path.relative_to(root))] = digest(path)
    if not found:
        raise RuntimeError(f"backend snapshot is empty: {root}")
    return found


def pinned_build_env(prefix):
    env = dict(os.environ)
    for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "ASAN_OPTIONS", "UBSAN_OPTIONS"):
        env.pop(key, None)
    env.update({
        "BUN": str(PINNED_BUN.resolve()),
        "BEND_SOURCE": str(PINNED_BEND.resolve()),
        "CC": str(CLANG),
        "SCRAPANIUM_CURL_DIR": str(Path(prefix).resolve()),
    })
    for path in (PINNED_BUN, PINNED_BEND / "bend2/main.ts", CLANG):
        if not path.exists():
            raise FileNotFoundError(f"pinned build input is missing: {path}")
    return env


def run_build(command, env, log_path):
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    Path(log_path).write_text(
        "COMMAND: " + " ".join(map(str, command)) + "\nEXIT: " + str(result.returncode) +
        "\n--- stdout ---\n" + result.stdout + "\n--- stderr ---\n" + result.stderr,
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError(f"build failed ({result.returncode}); see {log_path}")


def build_probe_artifacts(out):
    build_dir = out / "build"
    build_dir.mkdir(exist_ok=False)
    binaries = {}
    for label, client_root, prefix in (
        ("baseline", BASELINE_ROOT, BASELINE_PREFIX),
        ("candidate", CANDIDATE_ROOT, CANDIDATE_PREFIX),
    ):
        executable = build_dir / f"bench-ws-attribution-{label}"
        entry = client_root / "benchmarks/websocket_attribution.bend"
        env = pinned_build_env(prefix)
        run_build(
            [str(MATCHED_PYTHON), str(client_root / "scripts/build.py"), str(entry),
             "-o", str(executable)],
            env, build_dir / f"build-{label}.log",
        )
        binaries[label] = {
            "path": str(executable.resolve()),
            "sha256": digest(executable),
            "generated_c_path": str(executable.with_suffix(".generated.c").resolve()),
            "generated_c_sha256": digest(executable.with_suffix(".generated.c")),
        }

    shim = build_dir / "libwsattribution.so"
    run_build([
        str(CLANG), "-std=c11", "-O2", "-g", "-Wall", "-Wextra", "-Werror",
        "-fPIC", "-shared", "-I" + str(ROOT / ".deps/curl/include"),
        str(SHIM_SOURCE), "-ldl", "-pthread", "-o", str(shim),
    ], pinned_build_env(BASELINE_PREFIX), build_dir / "build-shim.log")

    peer = build_dir / "websocket_stream_server"
    run_build(["go", "build", "-trimpath", "-o", str(peer), str(PEER_SOURCE)],
              pinned_build_env(BASELINE_PREFIX), build_dir / "build-peer.log")
    return binaries, shim, peer


def backend_info(prefix):
    env, library_dir, backend = phase.make_env(Path(prefix).resolve())
    return env, library_dir, backend, digest(backend), tree_inventory(prefix)


def verify_mapping(mapping, library_dir, backend_sha):
    if not isinstance(mapping, dict):
        return ["client backend mapping is missing"]
    errors = []
    path_text = mapping.get("path")
    try:
        path = Path(path_text).resolve()
    except (TypeError, OSError):
        return ["client backend mapping path is invalid"]
    if path.parent != Path(library_dir).resolve() or not path.name.startswith("libcurl-impersonate.so"):
        errors.append(f"client mapped backend outside planned library directory: {path}")
    if mapping.get("sha256") != backend_sha or not path.is_file() or digest(path) != backend_sha:
        errors.append("client mapped backend hash differs from its planned snapshot")
    return errors


def fetch_peer_record(peer, run_id, timeout=8):
    deadline = time.monotonic() + timeout
    latest = None
    while True:
        if peer.poll() is not None:
            raise RuntimeError(f"stream peer exited with status {peer.returncode}")
        peer.stdin.write("stats\n")
        peer.stdin.flush()
        records = json.loads(phase.line(peer, timeout=5))
        latest = records.get(run_id) if isinstance(records, dict) else None
        if isinstance(latest, dict) and (latest.get("error") or latest.get("data_frames") == COUNT):
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(0.01)


def validate_peer(record, batch):
    if not isinstance(record, dict):
        return ["peer record is missing"]
    expected = {
        "data_frames": COUNT,
        "data_payload_bytes": EXPECTED_PAYLOAD,
        "data_frame_bytes": COUNT * (BODY_SIZE + 10),
        "warmups": 1,
        "starts": 1,
    }
    errors = []
    if record.get("error"):
        errors.append(f"peer error: {record['error']}")
    for key, value in expected.items():
        if type(record.get(key)) is not int or record[key] != value:
            errors.append(f"peer {key}={record.get(key)!r}; expected {value}")
    if type(record.get("tls_version")) is not int or record["tls_version"] <= 0:
        errors.append("peer TLS version is invalid")
    if type(record.get("tls_cipher")) is not int or record["tls_cipher"] <= 0:
        errors.append("peer TLS cipher is invalid")
    return errors


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=BASE / "recv-count-probe-sipn-20260924")
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if out.exists():
        raise FileExistsError(f"refusing to overwrite probe evidence: {out}")
    if "LD_PRELOAD" in os.environ:
        raise RuntimeError("clear LD_PRELOAD before starting the probe")
    for path in (BASELINE_ROOT, CANDIDATE_ROOT, BASELINE_PREFIX, CANDIDATE_PREFIX,
                 MATCHED_PYTHON, PINNED_BUN, PINNED_BEND, CLANG, PEER_SOURCE, SHIM_SOURCE):
        if not path.exists():
            raise FileNotFoundError(f"probe input is missing: {path}")

    out.mkdir(parents=True, exist_ok=False)
    (out / "samples.jsonl").open("x", encoding="utf-8").close()
    plan = [
        {"run_id": f"recv-count-{index:02d}-{backend}-{client}-flush{batch}",
         "backend": backend, "client": client, "batch": batch,
         "mode": "control", "count": COUNT, "message_bytes": BODY_SIZE}
        for index, (backend, client, batch) in enumerate(PLAN, start=1)
    ]
    write_json_new(out / "plan.json", {
        "schema": 1,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "kind": "diagnostic_only_curl_ws_recv_call_count",
        "runs": len(plan),
        "planned_order": plan,
        "body_bytes": BODY_SIZE - 8,
        "payload_bytes_per_client": EXPECTED_PAYLOAD,
        "mode": "control; per-call clocks disabled, exact successful call/result/byte counters retained",
        "execution": "same sequence-tagged 1024-frame exact-check/release clients; TLS setup, corpus preparation, warmup and close outside client interval",
        "timing_warning": "The preloaded shim and client interval clock affect timing. Elapsed values are contextual only and must not be compared with uninstrumented throughput samples.",
        "schedule": "fixed palindrome: baseline/candidate Bend, baseline/candidate curl_cffi at flush1, then reverse backend/client pairs at flush64",
        "controls": "the preceding 32-row isolated stream smoke covered corrupt/swap negative controls for both clients and both backends; this count-only probe adds no new fault run",
        "success_criteria": "eight successful rows, exact peer frames/bytes, exact successful curl_ws_recv bytes and internally consistent shim counters, expected mapped backend path/hash, stable TLS identity; call counts are reported without an adoption claim",
    })

    rows = []
    failure = None
    peer = None
    source_before = source_inventory()
    backend_before = {
        "baseline": tree_inventory(BASELINE_PREFIX),
        "candidate": tree_inventory(CANDIDATE_PREFIX),
    }
    try:
        binaries, shim, peer_binary = build_probe_artifacts(out)
        source_after_build = source_inventory()
        if source_after_build != source_before:
            raise RuntimeError("a frozen source input changed while building probe clients")
        backend_after_build = {
            "baseline": tree_inventory(BASELINE_PREFIX),
            "candidate": tree_inventory(CANDIDATE_PREFIX),
        }
        if backend_after_build != backend_before:
            raise RuntimeError("a backend snapshot changed while building probe clients")

        envs = {}
        backend_records = {}
        for label, prefix in (("baseline", BASELINE_PREFIX), ("candidate", CANDIDATE_PREFIX)):
            env, libdir, backend, sha, tree = backend_info(prefix)
            env.pop("ASAN_OPTIONS", None)
            env.pop("UBSAN_OPTIONS", None)
            envs[label] = (env, libdir, backend, sha)
            backend_records[label] = {
                "prefix": str(prefix.resolve()), "library_dir": str(libdir),
                "backend_path": str(backend), "backend_sha256": sha,
                "snapshot_files_sha256": tree,
            }

        certificate_dir = out / "tls"
        certificate_dir.mkdir(exist_ok=False)
        _, ca = phase.certificate(certificate_dir)
        key = certificate_dir / "key.pem"
        # TLS private material is used only for this local fixture and is removed
        # before final status is written; never copy it into a published report.
        (out / "tls").chmod(0o700)
        try:
            peer = subprocess.Popen(
                [str(peer_binary), "-cert", str(ca), "-key", str(key)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            peer_url = phase.line(peer, timeout=20)
            if not peer_url.startswith("wss://127.0.0.1:"):
                raise RuntimeError(f"unexpected stream peer URL {peer_url!r}")
            body_path = out / "body.bin"
            body_path.write_bytes(bytes((index * 31) % 128 for index in range(BODY_SIZE - 8)))
            if body_path.stat().st_size != BODY_SIZE - 8:
                raise RuntimeError("prepared body has the wrong size")

            clang_real = CLANG.resolve()
            compiler_info = {
                "clang": str(clang_real), "clang_sha256": digest(clang_real),
                "clang_version": phase.command_output([str(CLANG), "--version"]),
                "bun": str(PINNED_BUN.resolve()), "bun_sha256": digest(PINNED_BUN),
                "bun_version": phase.command_output([str(PINNED_BUN), "--version"]),
                "bend_source": str(PINNED_BEND.resolve()),
                "bend_revision": phase.command_output(["git", "-C", str(PINNED_BEND), "rev-parse", "HEAD"]),
                "go_version": phase.command_output(["go", "version"]),
                "matched_python": str(MATCHED_PYTHON.resolve()),
                "matched_python_sha256": digest(MATCHED_PYTHON),
                "curl_cffi_binding": phase.curl_binding_provenance(envs["baseline"][0]),
            }
            manifest = {
                "schema": 1,
                "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "kind": "diagnostic_only_curl_ws_recv_call_count",
                "planned_runs": plan,
                "source_sha256_before_build": source_before,
                "backend_snapshots_before_build": backend_before,
                "backend": backend_records,
                "compiler_and_binding": compiler_info,
                "bend_binaries": binaries,
                "shim": {"path": str(shim.resolve()), "sha256": digest(shim),
                         "source": str(SHIM_SOURCE), "source_sha256": digest(SHIM_SOURCE)},
                "peer": {"path": str(peer_binary.resolve()), "sha256": digest(peer_binary),
                         "source": str(PEER_SOURCE), "source_sha256": digest(PEER_SOURCE)},
                "body_sha256": digest(body_path),
                "tls_certificate_sha256": digest(ca),
                "timing_is_diagnostic_only": True,
                "measurement_started_after_manifest": True,
            }
            write_json_new(out / "manifest.json", manifest)

            tls_ids = set()
            for item in plan:
                sample = dict(item)
                client_result = None
                peer_record = None
                try:
                    env, libdir, backend, backend_sha = envs[item["backend"]]
                    url = f"{peer_url}/stream?{urlencode({'id': item['run_id'], 'size': BODY_SIZE, 'count': COUNT, 'batch': item['batch']})}"
                    shim_path = out / "shim" / f"{item['run_id']}.json"
                    shim_path.parent.mkdir(exist_ok=True)
                    bend_binary = Path(binaries[item["backend"]]["path"])
                    client_result = attr.run_client(
                        item["client"], "control", bend_binary, shim, url, ca,
                        body_path, COUNT, env, shim_path,
                    )
                    try:
                        peer_record = fetch_peer_record(peer, item["run_id"])
                    except BaseException as error:
                        sample["peer_collection_error"] = f"{type(error).__name__}: {error}"
                    sample["client_result"] = {
                        key: value for key, value in client_result.items()
                        if key not in ("stdout", "stderr", "shim_record")
                    }
                    sample["client_stdout"] = client_result.get("stdout", "")
                    sample["client_stderr"] = client_result.get("stderr", "")
                    sample["shim"] = client_result.get("shim_record")
                    sample["peer"] = peer_record

                    errors = []
                    if client_result.get("returncode") != 0:
                        errors.append(f"client return code was {client_result.get('returncode')!r}")
                    if client_result.get("launch_error"):
                        errors.append(f"client launch failed: {client_result['launch_error']}")
                    if client_result.get("shim_record_error"):
                        errors.append(f"shim record failed: {client_result['shim_record_error']}")
                    errors.extend(f"sanitizer marker: {marker}"
                                  for marker in attr.sanitizer_findings(client_result))
                    errors.extend(verify_mapping(client_result.get("mapped_backend"), libdir, backend_sha))
                    errors.extend(validate_peer(peer_record, item["batch"]))
                    shim_record = client_result.get("shim_record")
                    errors.extend(attr.validate_shim_positive(
                        shim_record, client_result, "control", EXPECTED_PAYLOAD,
                    ))
                    if isinstance(shim_record, dict):
                        interval = shim_record.get("interval", {})
                        curl = shim_record.get("curl_ws_recv", {})
                        sample["diagnostic_only_client_interval_wall_ns"] = interval.get("wall_ns")
                        sample["diagnostic_only_caller_thread_cpu_ns"] = interval.get("caller_thread_cpu_ns")
                        sample["curl_ws_recv_calls"] = curl.get("calls")
                        sample["curl_ws_recv_ok"] = curl.get("ok")
                        sample["curl_ws_recv_curle_again"] = curl.get("curle_again")
                        sample["curl_ws_recv_other"] = curl.get("other")
                        sample["curl_ws_recv_successful_bytes"] = curl.get("bytes")
                    if isinstance(peer_record, dict):
                        tls_ids.add((peer_record.get("tls_version"), peer_record.get("tls_cipher")))
                    sample["validation_errors"] = errors
                    if errors:
                        raise RuntimeError("; ".join(errors))
                except BaseException as error:
                    sample["failure"] = f"{type(error).__name__}: {error}"
                    if client_result is not None and "client_stdout" not in sample:
                        sample["client_stdout"] = client_result.get("stdout", "")
                        sample["client_stderr"] = client_result.get("stderr", "")
                        sample["shim"] = client_result.get("shim_record")
                    if peer_record is not None:
                        sample["peer"] = peer_record
                with (out / "samples.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(sample, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                rows.append(sample)
                if sample.get("failure"):
                    raise RuntimeError(f"probe stopped after retained failure in {item['run_id']}: {sample['failure']}")

            if len(tls_ids) != 1:
                raise RuntimeError(f"TLS identity differed across count-probe rows: {tls_ids}")
            if source_inventory() != source_before:
                raise RuntimeError("a source input changed during the call-count probe")
            for label, prefix in (("baseline", BASELINE_PREFIX), ("candidate", CANDIDATE_PREFIX)):
                if tree_inventory(prefix) != backend_before[label]:
                    raise RuntimeError(f"{label} backend snapshot changed during the probe")
            for label, recorded in binaries.items():
                if digest(recorded["path"]) != recorded["sha256"]:
                    raise RuntimeError(f"{label} Bend client binary changed during the probe")
            if digest(shim) != manifest["shim"]["sha256"]:
                raise RuntimeError("attribution shim binary changed during the probe")
            if digest(peer_binary) != manifest["peer"]["sha256"]:
                raise RuntimeError("stream peer binary changed during the probe")
            summary = {
                "status": "complete", "completed_rows": len(rows),
                "tls_identity": next(iter(tls_ids)),
                "call_counts": [
                    {"backend": row["backend"], "client": row["client"], "batch": row["batch"],
                     "curl_ws_recv_calls": row.get("curl_ws_recv_calls"),
                     "curl_ws_recv_ok": row.get("curl_ws_recv_ok"),
                     "curl_ws_recv_curle_again": row.get("curl_ws_recv_curle_again"),
                     "curl_ws_recv_successful_bytes": row.get("curl_ws_recv_successful_bytes")}
                    for row in rows
                ],
                "source_sha256_unchanged": True,
                "backend_snapshots_unchanged": True,
                "client_and_helper_binary_hashes_unchanged": True,
                "interpretation": "Exact call counts only. Timing fields are interposed diagnostic context and are not throughput evidence.",
            }
            write_json_new(out / "summary.json", summary)
        finally:
            if peer is not None:
                if peer.poll() is None:
                    peer.stdin.close()
                    try:
                        peer.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        peer.kill()
                        peer.wait()
                if peer.stderr:
                    (out / "peer.stderr.log").write_text(peer.stderr.read(), encoding="utf-8")
            try:
                (out / "tls/key.pem").unlink()
            except FileNotFoundError:
                pass
    except BaseException as error:
        failure = f"{type(error).__name__}: {error}"
        if not (out / "summary.json").exists():
            write_json_new(out / "summary.json", {
                "status": "failed", "completed_rows": len(rows),
                "failure": failure, "partial_samples_retained": True,
            })
        raise
    finally:
        try:
            (out / "tls/key.pem").unlink()
        except FileNotFoundError:
            pass
    print(out)


if __name__ == "__main__":
    main()
