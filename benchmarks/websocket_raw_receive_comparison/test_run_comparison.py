"""Small pre-run checks for campaign balancing and durable failed-attempt logging."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import run_comparison as campaign


class CampaignPlanTests(unittest.TestCase):
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
            self.assertEqual(runtime_env["LD_LIBRARY_PATH"], f"{library.resolve()}{campaign.os.pathsep}/inherited/lib")
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
