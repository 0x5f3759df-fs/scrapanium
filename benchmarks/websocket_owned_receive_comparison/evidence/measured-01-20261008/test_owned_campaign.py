"""Small pre-run checks for campaign balancing and durable failed-attempt logging."""
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import run_owned_comparison as campaign


def _fixture_git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _copy_file(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _fixture_source_pins(tmp):
    source_pins = json.loads((Path(campaign.__file__).with_name("source-pins.json")).read_text())
    actual_base = Path(source_pins["baseline"]["path"])
    actual_candidate = Path(source_pins["candidate"]["path"])
    base = Path(tmp) / "baseline"
    base.mkdir()
    base_inputs = [actual_base / name for name in campaign.SHARED]
    base_inputs.extend(path for path in (actual_base / "native").iterdir() if path.is_file())
    for source in base_inputs:
        _copy_file(source, base / source.relative_to(actual_base))
    _fixture_git(base, "init", "-q")
    _fixture_git(base, "config", "user.email", "fixture@example.invalid")
    _fixture_git(base, "config", "user.name", "Campaign fixture")
    _fixture_git(base, "add", "-A")
    _fixture_git(base, "commit", "-q", "-m", "immutable fixture baseline")
    head = _fixture_git(base, "rev-parse", "HEAD")
    checker_marker = b"\n# frozen fixture-only delta\n"
    checker = base / "benchmarks/websocket_streaming.py"
    checker.write_bytes(checker.read_bytes() + checker_marker)

    candidate = Path(tmp) / "candidate"
    subprocess.check_call(["git", "clone", "-q", str(base), str(candidate)])
    native_dir = candidate / "native"
    shutil.rmtree(native_dir)
    native_dir.mkdir()
    for source in (actual_candidate / "native").iterdir():
        if source.is_file():
            _copy_file(source, native_dir / source.name)
    for name in campaign.SHARED:
        _copy_file(actual_candidate / name, candidate / name)
    for name in source_pins["candidate"]["changed_file_sha256"]:
        _copy_file(actual_candidate / name, candidate / name)
    # Give the checker an already-dirty tracked status so its later mutation
    # must be rejected by the frozen bytes/diff pins, not merely by git status.
    checker = candidate / "benchmarks/websocket_streaming.py"
    checker.write_bytes(checker.read_bytes() + checker_marker)

    def root_pin(root):
        diff = subprocess.check_output(["git", "-C", str(root), "diff", "--binary", "HEAD"])
        return {
            "path": str(root.resolve()),
            "expected_head": head,
            "status_lines": campaign.status_lines(root),
            "tracked_diff_sha256": campaign.hashlib.sha256(diff).hexdigest(),
            "source_sha256": campaign.sources(root),
        }

    pins = {
        "baseline": root_pin(base),
        "candidate": root_pin(candidate),
    }
    pins["candidate"]["changed_file_sha256"] = {
        name: campaign.digest(candidate / name)
        for name in source_pins["candidate"]["changed_file_sha256"]
    }
    return base, candidate, pins, head


def _make_clean_git_repo(root):
    root = Path(root)
    root.mkdir(parents=True)
    (root / "input.txt").write_text("pinned")
    _fixture_git(root, "init", "-q")
    _fixture_git(root, "config", "user.email", "fixture@example.invalid")
    _fixture_git(root, "config", "user.name", "Campaign fixture")
    _fixture_git(root, "add", "-A")
    _fixture_git(root, "commit", "-q", "-m", "fixture")
    return _fixture_git(root, "rev-parse", "HEAD")


class CampaignPlanTests(unittest.TestCase):

    def test_preflight_only_runs_pinned_clang_newline_check_before_any_build(self):
        pins_path = Path(campaign.__file__).with_name("source-pins.json")
        pins = json.loads(pins_path.read_text())
        tool = pins["toolchain"]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "preflight-only"
            command = [
                tool["python"], str(Path(campaign.__file__).resolve()),
                "--baseline", pins["baseline"]["path"],
                "--candidate", pins["candidate"]["path"],
                "--curl-cffi-checkout", pins["curl_cffi"]["path"],
                "--bend-source", tool["bend_source"],
                "--bun", tool["bun"],
                "--curl-prefix", pins["backend"]["prefix"],
                "--cc", tool["clang"],
                "--preflight-only", "--output", str(output),
            ]
            env = dict(os.environ)
            env.pop("LD_PRELOAD", None)
            env.pop("LD_AUDIT", None)
            completed = subprocess.run(command, cwd=Path(campaign.__file__).parent,
                env=env, text=True, capture_output=True, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            status = json.loads((output / "status.json").read_text())
            self.assertEqual(status["status"], "preflight_complete")
            self.assertTrue(status["preflight_only"])
            self.assertFalse(status["performance_claims"])
            self.assertFalse(status["builds_started"])
            self.assertFalse(status["attempts_started"])
            self.assertFalse((output / "build").exists())
            self.assertFalse((output / "build-logs").exists())
            version = status["clang_version_check"]
            self.assertTrue(version["raw_output"].endswith("\n"))
            self.assertEqual(version["comparison_value"], tool["clang_version"])
            self.assertIn("--preflight-only", status["command"])
            self.assertTrue(status["python_cache_was_empty_before_helpers"])
            cache_root = (output / "python-bytecode-cache").resolve()
            self.assertEqual(status["python_cache_prefix"], str(cache_root))
            helper_records = status["baseline_helper_imports"]["helpers"]
            self.assertEqual(set(helper_records), set(campaign.BASELINE_HELPER_PATHS))
            for module_name, relative in campaign.BASELINE_HELPER_PATHS.items():
                record = helper_records[module_name]
                source_path = Path(record["source_path"]).resolve()
                expected_source = (Path(pins["baseline"]["path"]) / relative).resolve()
                self.assertEqual(source_path, expected_source)
                self.assertEqual(record["source_sha256"], pins["baseline"]["source_sha256"][relative])
                cache_path = Path(record["cache_path"]).resolve()
                self.assertTrue(cache_path.is_relative_to(cache_root), record)
                self.assertTrue(cache_path.is_file(), record)
                self.assertEqual(record["cache_sha256"], campaign.digest(cache_path))

    def test_source_status_guard_rejects_unexpected_changes(self):
        expected = [" M native/websocket.inc.c"]
        self.assertIsNone(campaign.require_exact_status("candidate", expected, expected))
        with self.assertRaisesRegex(RuntimeError, "status differs"):
            campaign.require_exact_status("candidate", expected + ["?? tests/unexpected.py"], expected)
        with self.assertRaisesRegex(RuntimeError, "status differs"):
            campaign.require_exact_status("baseline", [" M README.md"], [])

    def test_source_preflight_returns_snapshots_consumed_by_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, candidate, pins, head = _fixture_source_pins(tmp)
            with mock.patch.object(campaign, "BASELINE", head):
                baseline_snap, candidate_snap = campaign.validate_source_preflight(
                    base, candidate, pins)
            self.assertIn("source_sha256", baseline_snap)
            self.assertIn("source_sha256", candidate_snap)
            for name in campaign.EQUAL_SHARED:
                self.assertEqual(baseline_snap["source_sha256"][name],
                                 candidate_snap["source_sha256"][name])

    def test_actual_source_validator_rejects_checker_and_native_mutations_prebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, candidate, pins, head = _fixture_source_pins(tmp)
            with mock.patch.object(campaign, "BASELINE", head):
                campaign.validate_source_preflight(base, candidate, pins)
                out = Path(tmp) / "checker-preflight"
                campaign.initialize_run_status(out, inputs={}, options={"fixture": True})
                checker = candidate / "benchmarks/websocket_streaming.py"
                original = checker.read_bytes()
                checker.write_bytes(original + b"\n# injected checker mutation\n")
                try:
                    with self.assertRaisesRegex(RuntimeError, "tracked diff differs"):
                        campaign.validate_source_preflight(base, candidate, pins)
                    status = json.loads((out / "status.json").read_text())
                    self.assertFalse(status["builds_started"])
                    self.assertFalse(status["attempts_started"])
                finally:
                    checker.write_bytes(original)
                    campaign._ACTIVE_RUN_OUT = None

                out = Path(tmp) / "native-preflight"
                campaign.initialize_run_status(out, inputs={}, options={"fixture": True})
                native = candidate / "native/websocket.inc.c"
                original = native.read_bytes()
                native.write_bytes(original + b"\n/* injected native mutation */\n")
                try:
                    with self.assertRaisesRegex(RuntimeError, "tracked diff differs"):
                        campaign.validate_source_preflight(base, candidate, pins)
                    status = json.loads((out / "status.json").read_text())
                    self.assertFalse(status["builds_started"])
                    self.assertFalse(status["attempts_started"])
                finally:
                    native.write_bytes(original)
                    campaign._ACTIVE_RUN_OUT = None

    def test_actual_external_validator_rejects_changed_backend_dso_prebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            lib = prefix / "lib"
            include = prefix / "include"
            lib.mkdir(parents=True)
            include.mkdir()
            real_dso = lib / "libcurl-impersonate.so.4"
            real_dso.write_bytes(b"pinned test DSO")
            dso_link = lib / "libcurl-impersonate.so"
            dso_link.symlink_to(real_dso.name)
            (prefix / "manifest.json").write_text("backend manifest")

            candidate = root / "candidate"
            (candidate / "backend/patches").mkdir(parents=True)
            (candidate / "backend/patches/owned.patch").write_text("patch")
            (include / "curl.h").write_text("header")
            clang_resource = root / "clang-resource"
            (clang_resource / "include").mkdir(parents=True)
            (clang_resource / "include/stddef.h").write_text("builtin header")
            site = root / "site"
            site.mkdir()
            (site / "module.py").write_text("value = 1")

            tools = root / "tools"
            tools.mkdir()
            tool_paths = {}
            for name in ("clang", "go", "bun", "python"):
                path = tools / name
                path.write_text(name + " fixture")
                tool_paths[name] = path
            go_tooldir = root / "go-tools"
            go_tooldir.mkdir()
            (go_tooldir / "compile").write_text("compiler tool")

            bend = root / "bend"
            cffi = root / "cffi"
            bend_commit = _make_clean_git_repo(bend)
            cffi_commit = _make_clean_git_repo(cffi)

            pins = {
                "backend": {
                    "dso_link": str(dso_link),
                    "dso_realpath": str(real_dso.resolve()),
                    "dso_sha256": campaign.digest(real_dso),
                    "manifest_sha256": campaign.digest(prefix / "manifest.json"),
                    "prefix": str(prefix),
                },
                "backend_files": campaign.tree_hashes(candidate / "backend"),
                "backend_header_files": campaign.tree_hashes(include),
                "candidate": {"path": str(candidate)},
                "clang_resource_header_files": campaign.tree_hashes(clang_resource / "include"),
                "curl_cffi": {
                    "matched_site": str(site),
                    "commit": cffi_commit,
                    "status_lines": [],
                },
                "matched_site_files": campaign.tree_hashes(site, exclude_suffixes=(".pyc",)),
                "toolchain": {
                    "clang_resource_dir": str(clang_resource),
                    "clang_sha256": campaign.digest(tool_paths["clang"]),
                    "go_sha256": campaign.digest(tool_paths["go"]),
                    "bun_sha256": campaign.digest(tool_paths["bun"]),
                    "python_sha256": campaign.digest(tool_paths["python"]),
                    "bend_commit": bend_commit,
                    "go_tooldir": str(go_tooldir),
                    "go_tools_sha256": {"compile": campaign.digest(go_tooldir / "compile")},
                },
            }
            out = root / "preflight"
            campaign.initialize_run_status(out, inputs={}, options={"fixture": True})
            campaign.verify_external_pins(
                pins, compiler_path=tool_paths["clang"], go_path=tool_paths["go"],
                bend=bend, bun=tool_paths["bun"], python_path=tool_paths["python"],
                cffi=cffi, prefix=prefix)
            real_dso.write_bytes(b"changed test DSO")
            try:
                with self.assertRaisesRegex(RuntimeError, "backend DSO differs"):
                    campaign.verify_external_pins(
                        pins, compiler_path=tool_paths["clang"], go_path=tool_paths["go"],
                        bend=bend, bun=tool_paths["bun"], python_path=tool_paths["python"],
                        cffi=cffi, prefix=prefix)
                status = json.loads((out / "status.json").read_text())
                self.assertFalse(status["builds_started"])
                self.assertFalse(status["attempts_started"])
            finally:
                campaign._ACTIVE_RUN_OUT = None

    def test_tree_hash_order_is_stable_and_byte_sensitive(self):
        first = {"z.h": "1" * 64, "a.h": "2" * 64}
        self.assertEqual(campaign.ordered_tree_sha256(first), campaign.ordered_tree_sha256(dict(reversed(list(first.items())))))
        changed = dict(first, **{"a.h": "3" * 64})
        self.assertNotEqual(campaign.ordered_tree_sha256(first), campaign.ordered_tree_sha256(changed))
    def test_binding_inspection_receives_pinned_dso_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "curl-prefix"
            library = prefix / "lib"
            library.mkdir(parents=True)
            (library / "libcurl-impersonate.so").touch()
            inherited = {"LD_LIBRARY_PATH": "/inherited/lib", "PYTHONPATH": "/local/site-packages",
                         "KEEP_FOR_CHILD": "yes"}
            runtime_env, selected_library = campaign.pinned_runtime_environment(prefix, inherited)
            with mock.patch.object(campaign.subprocess, "run",
                    return_value=campaign.subprocess.CompletedProcess([], 0, "{}", "")) as run:
                self.assertEqual(campaign.binding_info(Path("/matched/python"), env=runtime_env), {})
            self.assertEqual(selected_library, library.resolve())
            self.assertEqual(run.call_args.kwargs["env"], runtime_env)
            self.assertTrue(run.call_args.kwargs["capture_output"])
            self.assertEqual(runtime_env["LD_LIBRARY_PATH"], str(library.resolve()))
            self.assertEqual(runtime_env["SCRAPANIUM_CURL_DIR"], str(prefix.resolve()))
            self.assertEqual(runtime_env["KEEP_FOR_CHILD"], "yes")

    def test_preflight_status_is_durable_before_failure_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "fresh-run"
            try:
                campaign.initialize_run_status(out, inputs={"candidate": "/candidate"},
                    options={"smoke": True, "runs": 1})
                initial = json.loads((out / "status.json").read_text())
                self.assertEqual(initial["status"], "preflight")
                self.assertFalse(initial["builds_started"])
                self.assertFalse(initial["attempts_started"])
                self.assertIn("LD_LIBRARY_PATH", initial["selected_environment"])
                prefix = Path(tmp) / "curl-prefix"
                library = prefix / "lib"
                library.mkdir(parents=True)
                (library / "libcurl-impersonate.so").touch()
                runtime_env, _ = campaign.pinned_runtime_environment(prefix, {})
                probe = campaign.subprocess.CompletedProcess(
                    ["/matched/python"], 1, "partial stdout", "ImportError: pinned DSO missing")
                with mock.patch.object(campaign.subprocess, "run", return_value=probe):
                    with self.assertRaises(campaign.BindingProbeError) as caught:
                        campaign.binding_info(Path("/matched/python"), env=runtime_env)
                campaign.record_run_failure(out, caught.exception)
                failed = json.loads((out / "status.json").read_text())
                self.assertEqual(failed["status"], "failed")
                self.assertFalse(failed["builds_started"])
                self.assertFalse(failed["attempts_started"])
                self.assertEqual(failed["failed_subprocess"]["returncode"], 1)
                self.assertEqual(failed["failed_subprocess"]["stdout"], "partial stdout")
                self.assertEqual(failed["failed_subprocess"]["stderr"], "ImportError: pinned DSO missing")
                self.assertEqual(failed["failed_subprocess"]["selected_environment"]["LD_LIBRARY_PATH"],
                                 runtime_env["LD_LIBRARY_PATH"])
                with self.assertRaises(FileExistsError):
                    campaign.initialize_run_status(out, inputs={}, options={})
            finally:
                campaign._ACTIVE_RUN_OUT = None

    def test_each_six_repeat_block_contains_all_client_orders(self):
        names = ("small-a", "large-b")
        first = campaign.balanced_orders(20261005, 12, names)
        second = campaign.balanced_orders(20261005, 12, names)
        self.assertEqual(first, second)
        expected = set(campaign.itertools.permutations(campaign.VARIANTS))
        for workload in names:
            self.assertEqual(len(first[workload]), 12)
            for offset in (0, 6):
                self.assertEqual(set(first[workload][offset:offset + 6]), expected)

    def test_smoke_projection_discards_timing_and_keeps_peer_evidence(self):
        result = campaign.smoke_projection({
            "run_id": "case-1", "elapsed_ms": 12.5, "messages_per_second": 4.0,
            "mapped_backend": {"path": "/local/libcurl.so", "sha256": "a" * 64},
            "peer_observation": {"tls_version": campaign.TLS_VERSION},
        })
        self.assertEqual(set(result), {"run_id", "mapped_backend", "peer_observation"})
        self.assertNotIn("elapsed_ms", result)
        self.assertNotIn("messages_per_second", result)

    def test_post_client_failure_keeps_client_and_peer_evidence(self):
        client_result = {
            "run_id": "sample-2", "elapsed_ms": 20.0,
            "mapped_backend": {"path": "/local/libcurl.so", "sha256": "b" * 64},
            "peer_observation": {"tls_version": campaign.TLS_VERSION, "data_frames": 10},
        }

        def reject_peer():
            raise RuntimeError("peer count mismatch")

        def action():
            return campaign.validate_with_result(client_result, reject_peer)

        for smoke in (False, True):
            with self.subTest(smoke=smoke), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "attempts.jsonl"
                attempts = []
                with self.assertRaises(campaign.AttemptValidationError):
                    campaign.record_attempt(path, attempts, "stream", {"name": "stream-small"},
                        0, "candidate", 1, "sample-2", action, smoke=smoke)
                row = json.loads(path.read_text().strip())
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["error_type"], "AttemptValidationError")
                self.assertEqual(row["result"]["peer_observation"], client_result["peer_observation"])
                self.assertEqual(row["result"]["mapped_backend"], client_result["mapped_backend"])
                self.assertEqual(attempts, [row])
                if smoke:
                    self.assertNotIn("elapsed_ms", row["result"])
                else:
                    self.assertEqual(row["result"]["elapsed_ms"], 20.0)

    def test_failed_validation_is_durably_logged_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.jsonl"
            attempts = []

            def fail_validation():
                raise ValueError("peer TLS mismatch")

            with self.assertRaisesRegex(ValueError, "peer TLS mismatch"):
                campaign.record_attempt(path, attempts, "stream", {"name": "stream-small"},
                    0, "candidate", 1, "sample-1", fail_validation)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "error")
            self.assertEqual(rows[0]["error_type"], "ValueError")
            self.assertIn("peer TLS mismatch", rows[0]["error"])
            self.assertEqual(attempts, rows)

    def test_roundtrip_failure_observer_retains_evidence_and_restores_hooks(self):
        class FakeStdin:
            def write(self, value):
                return len(value)

            def flush(self):
                return None

        class FakeOutput:
            def readline(self):
                return "ready\n"

        class FakeProcess:
            pid = 314

            def __init__(self):
                self.stdin = FakeStdin()
                self.stdout = FakeOutput()
                self.stderr = FakeOutput()
                self.returncode = 4
                self.communicated = False
                self.killed = False

            def communicate(self, *args, **kwargs):
                self.communicated = True
                return "1.25\n2.50\n", "transport failed\n"

            def poll(self):
                return self.returncode

            def kill(self):
                self.killed = True

        process = FakeProcess()
        subprocess_stub = SimpleNamespace(Popen=mock.Mock(return_value=process))
        line = lambda child, *args, **kwargs: child.stdout.readline().strip()
        digest = lambda path: "dso-sha"
        def mapped_backend(child):
            return {"path": "/pinned/libcurl.so", "sha256": rt.digest("/pinned/libcurl.so")}
        def read_timeout(stream):
            raise TimeoutError("peer read timed out")

        peer_globals = {"read_frame": read_timeout}
        peer_start = SimpleNamespace(__globals__=peer_globals)

        class FakePeer:
            def __init__(self):
                self.frames = [(2, b"payload", True)]
                self.shutdown_request = lambda request: "closed"

        peer = FakePeer()
        original_shutdown = peer.shutdown_request
        tls = [{"tls_version": campaign.TLS_VERSION, "tls_cipher": 4865}]

        class TimedStream:
            class Raw:
                class Socket:
                    @staticmethod
                    def gettimeout():
                        return 4.0
                _sock = Socket()
            raw = Raw()

        class FakeHandler:
            def __init__(self, path, server):
                self.path = path
                self.server = server
                self.rfile = TimedStream()
                self.connection = object()

            def do_GET(self):
                peer.frames.append((2, b"payload", True))
                with self_outer.assertRaises(TimeoutError):
                    peer_globals["read_frame"](self.rfile)

        self_outer = self
        peer_globals["Handler"] = FakeHandler
        matching_handler = FakeHandler("/echo?id=rt-1-0", peer)
        tls_by_socket = {id(matching_handler.connection): tls[0]}
        close_thread = None
        rt = SimpleNamespace(subprocess=subprocess_stub, line=line, digest=digest,
                             mapped_backend=mapped_backend, start_ws=peer_start)

        def failing_sample():
            nonlocal close_thread
            child = rt.subprocess.Popen(["roundtrip-client"], stdin=-1, stdout=-1, stderr=-1)
            self.assertEqual(rt.line(child), "ready")
            rt.mapped_backend(child)
            child.stdin.write("x")
            child.stdin.flush()
            rt.start_ws.__globals__["Handler"]("/echo?id=other-rt-1-0", peer).do_GET()
            rt.start_ws.__globals__["Handler"]("/echo?id=rt-1-0", object()).do_GET()
            matching_handler.do_GET()
            def close_after_sample_failure():
                time.sleep(0.03)
                peer.shutdown_request(matching_handler.connection)
            close_thread = threading.Thread(target=close_after_sample_failure, daemon=True)
            close_thread.start()
            stdout, stderr = child.communicate(timeout=90)
            raise RuntimeError(stdout + stderr)

        original_peer_reader = peer_globals["read_frame"]
        original_subprocess = rt.subprocess
        with self.assertRaisesRegex(RuntimeError, "transport failed") as caught:
            campaign.observed_roundtrip_sample(rt, failing_sample, run_id="rt-1",
                client="candidate", peer=peer, tls_records=tls, frame_start=0,
                tls_start=0, expected_payload=b"payload", tls_by_socket=tls_by_socket)

        partial = caught.exception.partial_result
        close_thread.join(timeout=1)
        diagnostics = partial["failure_diagnostics"]
        child = diagnostics["children"][0]
        self.assertEqual(partial["mapped_backend"]["sha256"], "dso-sha")
        self.assertEqual(child["returncode"], 4)
        self.assertEqual(child["stdout"]["text"], "ready\n1.25\n2.50\n")
        self.assertEqual(child["stderr"]["text"], "transport failed\n")
        stages = [event["stage"] for event in diagnostics["events"]]
        for stage in ("child_spawned", "ready_received", "backend_map_complete",
                      "backend_hash_complete", "gate_released", "child_communicate_complete"):
            self.assertIn(stage, stages)
        self.assertEqual(diagnostics["peer"]["frames"], 1)
        self.assertEqual(diagnostics["peer"]["tls_connections"], tls)
        self.assertEqual(diagnostics["peer"]["first_applied_socket_timeout_seconds"], 4.0)
        self.assertTrue(diagnostics["peer"]["read_error"]["is_socket_timeout"])
        self.assertTrue(diagnostics["peer"]["close_observed"])
        self.assertIn("peer_close_wait_start", [event["stage"] for event in diagnostics["events"]])
        self.assertIs(rt.subprocess, original_subprocess)
        self.assertIs(rt.line, line)
        self.assertIs(rt.digest, digest)
        self.assertIs(rt.mapped_backend, mapped_backend)
        self.assertIs(peer_globals["read_frame"], original_peer_reader)
        self.assertIs(peer.shutdown_request, original_shutdown)

    def test_roundtrip_success_returns_original_result_and_restores_runtime_hooks(self):
        class FakeProcess:
            pid = 271
            returncode = 0

            def __init__(self):
                self.stdin = SimpleNamespace(write=lambda value: len(value), flush=lambda: None)
                self.stdout = SimpleNamespace(readline=lambda: "ready\n")
                self.stderr = SimpleNamespace()

            def communicate(self, *args, **kwargs):
                return "1.0\n2.0\n", ""

            def poll(self):
                return 0

            def kill(self):
                raise AssertionError("successful sample should not be killed")

        process = FakeProcess()
        subprocess_stub = SimpleNamespace(Popen=mock.Mock(return_value=process))
        line = lambda child, *args, **kwargs: child.stdout.readline().strip()
        digest = lambda path: "dso-sha"
        mapped = lambda child: {"path": "/pinned/libcurl.so", "sha256": "dso-sha"}
        rt = SimpleNamespace(subprocess=subprocess_stub, line=line, digest=digest,
                             mapped_backend=mapped, start_ws=SimpleNamespace(__globals__={}))
        result = {"run_id": "success", "elapsed_ms": 12.0}

        def successful_sample():
            child = rt.subprocess.Popen(["roundtrip-client"])
            rt.line(child)
            rt.mapped_backend(child)
            child.stdin.write("x")
            child.stdin.flush()
            child.communicate(timeout=90)
            return result

        self.assertIs(campaign.observed_roundtrip_sample(rt, successful_sample,
            run_id="success", client="candidate"), result)
        self.assertEqual(result, {"run_id": "success", "elapsed_ms": 12.0})
        self.assertIs(rt.subprocess, subprocess_stub)
        self.assertIs(rt.line, line)
        self.assertIs(rt.digest, digest)
        self.assertIs(rt.mapped_backend, mapped)

    def test_real_exited_child_readiness_failure_drains_remaining_output_once(self):
        rt = SimpleNamespace(subprocess=campaign.subprocess,
            line=lambda process, *args, **kwargs: process.stdout.readline().strip(),
            digest=lambda path: "unused", mapped_backend=lambda process: {},
            start_ws=SimpleNamespace(__globals__={}))

        def failed_readiness():
            process = rt.subprocess.Popen([campaign.sys.executable, "-c",
                "import sys; print('not-ready', flush=True); print('later', flush=True); "
                "print('startup failed', file=sys.stderr, flush=True)"],
                stdin=campaign.subprocess.PIPE, stdout=campaign.subprocess.PIPE,
                stderr=campaign.subprocess.PIPE, text=True)
            value = rt.line(process)
            if value != "ready":
                raise RuntimeError("invalid client readiness: " + value)

        with self.assertRaisesRegex(RuntimeError, "invalid client readiness") as caught:
            campaign.observed_roundtrip_sample(rt, failed_readiness, run_id="ready-fail",
                client="candidate")
        child = caught.exception.partial_result["failure_diagnostics"]["children"][0]
        self.assertEqual(child["returncode"], 0)
        self.assertEqual(child["stdout"]["text"], "not-ready\nlater\n")
        self.assertEqual(child["stdout"]["text"].count("not-ready"), 1)
        self.assertEqual(child["stderr"]["text"], "startup failed\n")
        self.assertTrue(child["streams_drained"])

    def test_real_timeout_cleanup_communicate_does_not_duplicate_partial_output(self):
        rt = SimpleNamespace(subprocess=campaign.subprocess,
            line=lambda process, *args, **kwargs: process.stdout.readline().strip(),
            digest=lambda path: "unused", mapped_backend=lambda process: {},
            start_ws=SimpleNamespace(__globals__={}))

        def timed_out_sample():
            process = rt.subprocess.Popen([campaign.sys.executable, "-c",
                "import sys,time; print('ready', flush=True); print('out', flush=True); "
                "print('err', file=sys.stderr, flush=True); time.sleep(10)"],
                stdin=campaign.subprocess.PIPE, stdout=campaign.subprocess.PIPE,
                stderr=campaign.subprocess.PIPE, text=True)
            rt.line(process)
            try:
                process.communicate(timeout=0.01)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        with self.assertRaises(campaign.subprocess.TimeoutExpired) as caught:
            campaign.observed_roundtrip_sample(rt, timed_out_sample, run_id="timeout-fail",
                client="candidate")
        child = caught.exception.partial_result["failure_diagnostics"]["children"][0]
        self.assertEqual(child["returncode"], -9)
        self.assertEqual(child["stdout"]["text"], "ready\nout\n")
        self.assertEqual(child["stderr"]["text"], "err\n")
        self.assertEqual(child["stdout"]["text"].count("out\n"), 1)
        self.assertTrue(child["communicate_completed"])
        self.assertIn("child_communicate_error", [e["stage"] for e in
            caught.exception.partial_result["failure_diagnostics"]["events"]])

    def test_real_success_preserves_stdout_after_readiness_before_communicate(self):
        def make_runtime():
            return SimpleNamespace(subprocess=campaign.subprocess,
                line=lambda process, *args, **kwargs: process.stdout.readline().strip(),
                digest=lambda path: "unused", mapped_backend=lambda process: {},
                start_ws=SimpleNamespace(__globals__={}))

        command = [campaign.sys.executable, "-c",
            "import sys; print('ready', flush=True); sys.stdin.read(1); "
            "print('12.5', flush=True); print('25.0', flush=True); "
            "print('peer stderr', file=sys.stderr, flush=True)"]

        def successful_sample(rt):
            process = rt.subprocess.Popen(command, stdin=campaign.subprocess.PIPE,
                stdout=campaign.subprocess.PIPE, stderr=campaign.subprocess.PIPE,
                text=True)
            self.assertEqual(rt.line(process), "ready")
            rt.mapped_backend(process)
            process.stdin.write("x")
            process.stdin.flush()
            process.wait(timeout=2)
            stdout, stderr = process.communicate(timeout=2)
            return {"stdout": stdout, "stderr": stderr,
                    "returncode": process.returncode}

        expected = successful_sample(make_runtime())
        rt = make_runtime()
        actual = campaign.observed_roundtrip_sample(rt,
            lambda: successful_sample(rt), run_id="success-output", client="candidate")
        self.assertEqual(actual, expected)
        self.assertEqual(actual["stdout"], "12.5\n25.0\n")
        self.assertEqual(actual["stderr"], "peer stderr\n")

    def test_post_sample_validation_failure_merges_observer_without_losing_result(self):
        rt = SimpleNamespace(subprocess=campaign.subprocess,
            line=lambda process, *args, **kwargs: "", digest=lambda path: "unused",
            mapped_backend=lambda process: {}, start_ws=SimpleNamespace(__globals__={}))
        result = {"run_id": "validation-fail", "elapsed_ms": 10.0,
                  "mapped_backend": {"path": "/pinned/libcurl.so", "sha256": "dso"},
                  "peer_observation": {"frames": 2, "tls_connections": [{"tls_version": 772}]}}
        observers = []
        self.assertIs(campaign.observed_roundtrip_sample(rt, lambda: result,
            run_id="validation-fail", client="candidate", observer_sink=observers), result)
        error = RuntimeError("peer frame count mismatch")
        error.partial_result = result
        partial = campaign.attach_observer_failure(error, observers[0], "validation-fail",
            "candidate", "post_sample_validation")
        self.assertIs(partial, result)
        self.assertEqual(partial["peer_observation"], result["peer_observation"])
        self.assertEqual(partial["mapped_backend"], result["mapped_backend"])
        self.assertEqual(partial["failure_diagnostics"]["events"][-1]["reason"],
                         "post_sample_validation")

    def test_smoke_roundtrip_failure_keeps_diagnostics_without_timing(self):
        partial = {
            "run_id": "rt-smoke", "elapsed_ms": 99.0,
            "failure_diagnostics": {
                "observer": "roundtrip-failure-v1",
                "events": [{"stage": "sample_start", "monotonic_ns": 100},
                           {"stage": "gate_released", "monotonic_ns": 200}],
                "children": [{"pid": 4, "returncode": 4,
                    "stdout": {"text": "ready\n1.25\n2.5\n", "truncated": False},
                    "stderr": {"text": "transport failed\n", "truncated": False}}],
                "peer": {"kind": "python", "frames": 1, "payload_bytes": 7,
                    "first_applied_socket_timeout_seconds": 4.0,
                    "first_read_started_monotonic_ns": 150,
                    "read_error": {"type": "TimeoutError", "elapsed_ns": 4_000_000_000},
                    "close_observed": True},
            },
        }
        projected = campaign.smoke_projection(partial)
        diagnostics = projected["failure_diagnostics"]
        self.assertEqual(diagnostics["stages"], ["sample_start", "gate_released"])
        self.assertEqual(diagnostics["children"][0]["stdout"]["text"], "ready")
        self.assertEqual(diagnostics["children"][0]["stderr"]["text"], "transport failed\n")
        self.assertEqual(diagnostics["peer"]["read_error"]["type"], "TimeoutError")
        self.assertNotIn("elapsed_ms", projected)
        self.assertNotIn("monotonic_ns", json.dumps(projected))
        self.assertNotIn("elapsed_ns", json.dumps(projected))
        self.assertNotIn("1.25", json.dumps(projected))

        def failed_action():
            raise RuntimeError("1.25\n2.5\ntransport failed")

        failed_action.__name__ = "failed_action"
        # record_attempt uses the exception's partial result to route diagnostics through smoke projection.
        original_action = failed_action
        def with_partial():
            try:
                original_action()
            except Exception as exc:
                exc.partial_result = partial
                raise

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.jsonl"
            with self.assertRaises(RuntimeError):
                campaign.record_attempt(path, [], "roundtrip", {"name": "rt"},
                    0, "candidate", 0, "rt-smoke", with_partial, smoke=True)
            row = json.loads(path.read_text())
        self.assertEqual(row["result"]["failure_diagnostics"]["peer"]["close_observed"], True)
        self.assertNotIn("1.25", row["error"])
        self.assertNotIn("2.5", row["traceback"])
        serialized = json.dumps(row)
        for timing_key in ("elapsed_ns", "started_monotonic_ns", "ended_monotonic_ns",
                           "first_read_started_monotonic_ns", "close_started_monotonic_ns"):
            self.assertNotIn(timing_key, serialized)

    def test_smoke_status_redacts_client_timing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "smoke"
            try:
                campaign.initialize_run_status(out, inputs={}, options={"smoke": True, "runs": 1})
                campaign.update_run_status(out, attempts_started=True)
                try:
                    raise RuntimeError("1.25\n2.5\ntransport failed")
                except RuntimeError as exc:
                    campaign.record_run_failure(out, exc)
                status_text = (out / "status.json").read_text()
                status = json.loads(status_text)
                self.assertEqual(status["status"], "failed")
                self.assertNotIn("1.25", status_text)
                self.assertNotIn("2.5", status_text)
                self.assertNotIn("transport failed", status_text)
                self.assertIn("correctness-only smoke failed", status["error"])
            finally:
                campaign._ACTIVE_RUN_OUT = None


if __name__ == "__main__":
    unittest.main()
