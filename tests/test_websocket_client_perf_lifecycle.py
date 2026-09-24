from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
import websocket_client_perf as perf


VALID_HEADER = """# perf version : 7.0.14
# event : name = cpu-clock:u, , id = { 12 }, type = 1 (PERF_TYPE_SOFTWARE), size = 144, config = 0 (PERF_COUNT_SW_CPU_CLOCK), { sample_period, sample_freq } = 99, sample_type = IP|TID|TIME|CALLCHAIN|PERIOD|REGS_USER|STACK_USER|IDENTIFIER, read_format = ID|LOST, disabled = 1, inherit = 1, exclude_kernel = 1, exclude_hv = 1, freq = 1, sample_id_all = 1, exclude_callchain_user = 1, sample_regs_user = 0xff0fff, sample_stack_user = 16384, use_clockid = 1, clockid = 1
# clockid: monotonic (1)
"""
REPORT = "# Total Lost Samples: 0\n# Samples: 1 of event 'cpu-clock:u'\n"


def script_row(event: str = "cpu-clock:u") -> str:
    return f"10/10 1.500000000: {event}: 0x100 leaf (/tmp/client)\n"


def fake_perf_text(monkeypatch, script: str, header: str = VALID_HEADER) -> None:
    outputs = iter((header, REPORT, script))

    def run_text(command, *, env=None, check=True):
        return subprocess.CompletedProcess(command, 0, next(outputs), "")

    monkeypatch.setattr(perf, "run_text", run_text)


def test_decode_rejects_nonzero_samples_for_disabled_profile(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    data = attempt / "perf.data"
    data.write_bytes(b"profile")
    fake_perf_text(monkeypatch, script_row())

    with pytest.raises(ValueError, match="disabled perf control unexpectedly contains"):
        perf.decode_profile(
            Path("/test/perf"), {}, data, 10, [10],
            {"start_ns": 1_000_000_000, "end_ns": 2_000_000_000},
            "disabled", attempt, True,
        )


def test_profiled_negative_control_retains_samples_without_requiring_zero(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    data = attempt / "perf.data"
    data.write_bytes(b"profile")
    fake_perf_text(monkeypatch, script_row())

    result = perf.decode_negative_profile(
        Path("/test/perf"), {}, data, attempt, "profiled"
    )
    assert result["sample_count"] == 1
    assert result["negative_control_has_no_complete_client_timer"] is True


@pytest.mark.parametrize("script", [
    "not a perf record\n",
    "10/10 1.500000000: task-clock:u: 0x100 leaf (/tmp/client)\n",
])
def test_profile_decode_fails_closed_on_malformed_or_wrong_event_rows(tmp_path, monkeypatch, script):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    data = attempt / "perf.data"
    data.write_bytes(b"profile")
    fake_perf_text(monkeypatch, script)

    with pytest.raises(ValueError):
        perf.decode_profile(
            Path("/test/perf"), {}, data, 10, [10],
            {"start_ns": 1_000_000_000, "end_ns": 2_000_000_000},
            "profiled", attempt, True,
        )


def test_profile_decode_fails_closed_on_wrong_actual_event_configuration(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    data = attempt / "perf.data"
    data.write_bytes(b"profile")
    header = VALID_HEADER.replace("config = 0", "config = 2")
    fake_perf_text(monkeypatch, script_row(), header)

    with pytest.raises(ValueError, match="actual-header/parser contract"):
        perf.decode_profile(
            Path("/test/perf"), {}, data, 10, [10],
            {"start_ns": 1_000_000_000, "end_ns": 2_000_000_000},
            "profiled", attempt, True,
        )


def test_profile_decode_retains_outside_window_pid_tid_exclusions(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    data = attempt / "perf.data"
    data.write_bytes(b"profile")
    script = (
        "99/99 0.999999999: cpu-clock:u: 0x100 before (/tmp/other)\n"
        "10/10 1.500000000: cpu-clock:u: 0x101 selected (/tmp/client)\n"
        "10/11 2.000000000: cpu-clock:u: 0x102 after (/tmp/client)\n"
    )
    fake_perf_text(monkeypatch, script)

    result = perf.decode_profile(
        Path("/test/perf"), {}, data, 10, [10],
        {"start_ns": 1_000_000_000, "end_ns": 2_000_000_000},
        "profiled", attempt, True,
    )
    assert result["selected_window_user_samples"] == 1
    assert result["window_filter"]["excluded_counts"]["wrong_pid"] == 1
    assert result["window_filter"]["excluded_counts"]["wrong_tid"] == 1


class FakePipe:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, value):
        self.writes.append(value)
        return len(value)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, pid=123):
        self.pid = pid
        self.stdin = FakePipe()
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def communicate(self, timeout=None):
        return "partial client output", "startup diagnostic"


def test_perf_control_accepts_nul_terminated_ack_lines_across_commands_and_reads():
    ctl_read, ctl_write = os.pipe()
    ack_read, ack_write = os.pipe()
    failure = []

    def read_command():
        value = bytearray()
        while not value.endswith(b"\n"):
            chunk = os.read(ctl_read, 64)
            if not chunk:
                raise RuntimeError("control pipe closed before command")
            value.extend(chunk)
        return bytes(value)

    def server():
        try:
            assert read_command() == b"ping\n"
            os.write(ack_write, b"ack\n\x00")
            assert read_command() == b"disable\n"
            # Fragment the next tag. The NUL terminator from the first ACK may
            # already be buffered ahead of these bytes.
            os.write(ack_write, b"a")
            time.sleep(0.01)
            os.write(ack_write, b"ck\n\x00")
        except BaseException as error:
            failure.append(error)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    class RunningProcess:
        @staticmethod
        def poll():
            return None

    controller = perf.PerfControl(RunningProcess(), ctl_write, ack_read)
    try:
        first = controller.command("ping", timeout=2)
        second = controller.command("disable", timeout=2)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not failure
        assert first["raw_ack"] == "ack"
        assert second["raw_ack"] == "ack"
        assert [row["command"] for row in controller.transcript] == ["ping", "disable"]
    finally:
        for fd in (ctl_read, ctl_write, ack_read, ack_write):
            try:
                os.close(fd)
            except OSError:
                pass


def test_capture_perf_ack_failure_stops_process_and_retains_profiler_output(tmp_path, monkeypatch):
    process = FakeProcess(pid=333)
    monkeypatch.setattr(perf.subprocess, "Popen", lambda *args, **kwargs: process)

    def fail_ack(self, command, timeout=15):
        raise RuntimeError(f"no ack for {command}")

    monkeypatch.setattr(perf.PerfControl, "command", fail_ack)
    with pytest.raises(RuntimeError, match="no ack for ping"):
        perf.capture_perf(
            Path("/fake/client"), Path("/fake/perf"), tmp_path,
            tmp_path, process.pid, "profiled"
        )

    assert process.killed
    assert (tmp_path / "perf-startup.stdout.txt").read_text() == "partial client output"
    assert (tmp_path / "perf-startup.stderr.txt").read_text() == "startup diagnostic"
    controls = (tmp_path / "perf-startup-controls.json").read_text()
    assert controls.strip() == "[]"


def test_run_one_retains_client_and_row_diagnostics_when_perf_setup_fails(tmp_path, monkeypatch):
    output = tmp_path
    (output / "attempts").mkdir()
    child = FakeProcess()
    monkeypatch.setattr(perf.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(perf, "read_line", lambda process, timeout=30: "ready")
    monkeypatch.setattr(perf, "mapped_backend", lambda pid, expected: {"path": str(expected)})
    monkeypatch.setattr(perf, "task_tids", lambda pid: [pid])

    def fail_capture(*args, **kwargs):
        raise RuntimeError("perf ack setup failed")

    monkeypatch.setattr(perf, "capture_perf", fail_capture)
    monkeypatch.setattr(perf, "peer_stats", lambda *args, **kwargs: None)
    row = perf.run_one(
        {"kind": "positive", "run_id": "setup-failure", "client": perf.CLIENTS[0],
         "sampling": "profiled", "batch": 1, "fault": "", "count": 8},
        output, object(), "wss://127.0.0.1:1/stream", "ca.pem", output / "body.bin",
        {}, {"bend": "/fake/bend"}, Path("/fake/perf"), output, Path("/fake/curl"), False,
    )

    assert row["status"] == "failed"
    assert "perf ack setup failed" in row["failure"]
    assert child.killed
    attempt = output / "attempts" / "setup-failure"
    assert (attempt / "client.stdout.txt").read_text() == "partial client output"
    assert (attempt / "client.stderr.txt").read_text() == "startup diagnostic"
    assert "perf ack setup failed" in (attempt / "row.json").read_text()


@pytest.mark.parametrize("perf_returncode", [0, 1])
def test_run_finalizes_and_checks_perf_before_decoding(tmp_path, monkeypatch, perf_returncode):
    output = tmp_path
    (output / "attempts").mkdir()
    client = FakeProcess(pid=222)
    client.returncode = 0
    order = []
    perf_data = output / "attempts" / "ordered" / "perf.data"

    class FakeControl:
        process = FakeProcess(pid=333)
        transcript = []

    control = FakeControl()
    monkeypatch.setattr(perf.subprocess, "Popen", lambda *args, **kwargs: client)
    monkeypatch.setattr(perf, "read_line", lambda process, timeout=30: "ready")
    monkeypatch.setattr(perf, "mapped_backend", lambda pid, expected: {"path": str(expected)})
    monkeypatch.setattr(perf, "task_tids", lambda pid: [pid])
    monkeypatch.setattr(perf, "capture_perf", lambda *args, **kwargs:
                        (control, [client.pid], {
                            "command": ["/fake/perf", "record"],
                            "ping": {"command": "ping", "ack_monotonic_ns": 1},
                            "arming": {"command": "enable", "ack_monotonic_ns": 2},
                        }, []))

    def finish(process, fds):
        order.append("finish")
        perf_data.parent.mkdir(parents=True, exist_ok=True)
        perf_data.write_bytes(b"complete profile")
        return {"returncode": perf_returncode, "stdout": "", "stderr": "perf diagnostic"}

    monkeypatch.setattr(perf, "finish_perf", finish)

    def validate_client(*args):
        start = time.monotonic_ns()
        return {"start_ns": start, "end_ns": start + 100_000,
                "elapsed_ns": 100_000, "elapsed_us": 100}

    monkeypatch.setattr(perf, "validate_client_result", validate_client)
    monkeypatch.setattr(perf, "peer_stats", lambda *args, **kwargs: {"record": True})
    monkeypatch.setattr(perf, "validate_peer", lambda *args, **kwargs:
                        {"tls_version": 772, "tls_cipher": 4865})

    def decode(*args, **kwargs):
        order.append("decode")
        assert order == ["finish", "decode"]
        assert perf_returncode == 0
        return {"decoded": True}

    monkeypatch.setattr(perf, "decode_profile", decode)
    row = perf.run_one(
        {"kind": "positive", "run_id": "ordered", "client": perf.CLIENTS[0],
         "sampling": "profiled", "batch": 1, "fault": "", "count": 8},
        output, object(), "wss://127.0.0.1:1/stream", "ca.pem", output / "body.bin",
        {}, {"bend": "/fake/bend"}, Path("/fake/perf"), output, Path("/fake/curl"), False,
    )
    if perf_returncode == 0:
        assert row["status"] == "passed"
        assert order == ["finish", "decode"]
        assert row["perf"]["decoded"] is True
    else:
        assert row["status"] == "failed"
        assert order == ["finish"]
        assert "perf recorder exited with status 1" in row["failure"]
