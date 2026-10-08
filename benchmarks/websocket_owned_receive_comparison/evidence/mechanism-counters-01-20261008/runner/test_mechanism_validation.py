from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from mechanism_validation import fixed_schedule, validate_report

ROOT = Path(__file__).resolve().parent
PINS = json.loads((ROOT / "source-pins.json").read_text(encoding="utf-8"))
SCHEMA = PINS["counter_schema"]
NATIVE_LAYOUT = {"layout_segment_size": 0, "layout_buffer_size": 48, "layout_ws_io_size": 272}
CURL_LAYOUT = {
    "layout_chunk_size": 48, "layout_bufq_size": 64,
    "layout_pin_size": 32, "layout_payload_size": 32,
    "layout_recv_chunk_size": 131072,
}
DSO_PATH = PINS["backend"]["dso_realpath"]
DSO_SHA = PINS["backend"]["dso_sha256"]

def zero_counters(area: str) -> dict:
    return {name: 0 for name in SCHEMA[area]}

def make_report():
    attempts, counter_files = [], {}
    for planned in fixed_schedule():
        kind, count, size, variant = (
            planned["kind"], planned["count"], planned["bytes"], planned["variant"])
        warm_bytes, receive_bytes = (6 if kind == "stream" else size), count * size
        native_rows = []
        for phase in ("cold", "gate", "work_end", "released"):
            measured, released = phase in ("work_end", "released"), phase == "released"
            native, curl = zero_counters("native"), zero_counters("curl")
            native.update(NATIVE_LAYOUT)
            if variant == "owned":
                native["layout_segment_size"] = 64
            curl.update(CURL_LAYOUT)
            if phase != "cold":
                native["bytes_equal_calls"] = 1
                native["bytes_equal_bytes"] = warm_bytes
                native["ws_send_calls"] = native["ws_send_success_calls"] = 1
                native["ws_send_return_bytes"] = warm_bytes
                native["ws_send_request_bytes"] = warm_bytes
                if variant == "owned":
                    native["bend_bytes_flatten_calls"] = 1
                if variant == "baseline":
                    native["framed_recv_calls"] = native["framed_recv_success_calls"] = 1
                    native["direct_recv_calls"] = native["direct_recv_success"] = 1
                    native["direct_recv_return_bytes"] = native["framed_recv_bytes"] = warm_bytes
                else:
                    native["owned_recv_calls"] = native["owned_recv_success_calls"] = 1
                    native["owned_recv_bytes"] = warm_bytes
            if measured:
                native["bytes_equal_calls"] += count
                native["bytes_equal_bytes"] += receive_bytes
                sends = 1 if kind == "stream" else count
                sent_bytes = 5 if kind == "stream" else receive_bytes
                native["ws_send_calls"] += sends
                native["ws_send_success_calls"] += sends
                native["ws_send_return_bytes"] += sent_bytes
                native["ws_send_request_bytes"] += sent_bytes
                if variant == "owned":
                    native["bend_bytes_flatten_calls"] += sends
                if kind == "roundtrip" and variant == "owned":
                    native["bend_bytes_flatten_alloc_attempts"] = count
                    native["bend_bytes_flatten_alloc_successes"] = count
                    native["bend_bytes_flatten_alloc_bytes"] = receive_bytes
                    native["bend_bytes_flatten_copy_bytes"] = receive_bytes
                if variant == "baseline":
                    native["framed_recv_calls"] += count
                    native["framed_recv_success_calls"] += count
                    native["direct_recv_calls"] += count
                    native["direct_recv_success"] += count
                    native["direct_recv_return_bytes"] += receive_bytes
                    native["framed_recv_bytes"] += receive_bytes
                else:
                    native["owned_recv_calls"] += count
                    native["owned_recv_success_calls"] += count
                    native["owned_recv_bytes"] += receive_bytes
            if kind == "roundtrip" and variant == "owned" and phase != "cold":
                seg, live = 64, 0 if released else 1
                allocated = 1 if phase == "gate" else count + 1
                freed = 0 if phase == "gate" else count + (1 if released else 0)
                native["segment_alloc_attempts"] = native["segment_alloc_successes"] = allocated
                native["segment_alloc_request_bytes"] = allocated * seg
                native["segment_frees"] = freed
                native["segment_free_request_bytes"] = freed * seg
                native["segments_live"] = live
                native["segments_live_request_bytes"] = live * seg
                native["segment_live_payload_bytes"] = live * size
                curl["chunk_alloc_attempts"] = curl["chunk_alloc_successes"] = allocated
                curl["chunk_alloc_request_bytes"] = curl["chunk_alloc_success_bytes"] = allocated * 131072
                curl["chunk_physical_frees"] = freed
                curl["chunk_physical_free_request_bytes"] = freed * 131072
                curl["chunks_live"] = live
                curl["chunks_live_request_bytes"] = live * 131072
                curl["pin_alloc_attempts"] = curl["pin_alloc_successes"] = allocated
                curl["pin_releases"] = freed
                curl["pins_live"] = live
                curl["payload_alloc_attempts"] = curl["payload_alloc_successes"] = allocated
                curl["payload_frees"] = freed
                curl["payloads_live"] = live
            native_rows.append({"phase": phase, "native": native, "curl": curl})
        counter_rel = f"counters/attempt-{planned['attempt_index']:02d}.jsonl"
        counter_files[counter_rel] = native_rows
        if kind == "stream":
            header = 2 if size < 126 else 4 if size < 65536 else 10
            peer = {"kind": "stream", "data_frames": count, "data_payload_bytes": receive_bytes,
                    "data_frame_bytes": count * (size + header), "warmups": 1, "starts": 1,
                    "tls_version": 772, "tls_cipher": 4865}
        else:
            frames, ext = count + 1, (0 if size < 126 else 2 if size < 65536 else 8)
            payload = (count + 1) * size
            peer = {"kind": "roundtrip", "frames": frames, "payload_bytes": payload,
                    "client_frame_bytes": payload + frames * (6 + ext),
                    "server_frame_bytes": payload + frames * (2 + ext),
                    "tls_version": 772, "tls_cipher": 4865}
        attempts.append({
            **planned, "client": "scrapanium-bend", "attempt_status": "ok",
            "returncode": 0, "exact_checker_success": True,
            "mapped_backend": {"path": DSO_PATH, "sha256": DSO_SHA},
            "peer_observation": peer, "counter_file": counter_rel,
            "counter_file_sha256": f"fixture-{planned['attempt_index']}",
        })
    peer_files = {
        "peers/stream.stdout.txt": b"wss://127.0.0.1:1\n{}\n",
        "peers/stream.stderr.txt": b"",
        "peers/roundtrip.stdout.txt": b"wss://127.0.0.1:2\n{}\n",
        "peers/roundtrip.stderr.txt": b"",
    }
    peer_processes = []
    for name in ("stream", "roundtrip"):
        stdout_rel, stderr_rel = f"peers/{name}.stdout.txt", f"peers/{name}.stderr.txt"
        peer_processes.append({
            "name": name, "exit_code": 0,
            "stdout_file": stdout_rel,
            "stdout_sha256": hashlib.sha256(peer_files[stdout_rel]).hexdigest(),
            "stderr_file": stderr_rel,
            "stderr_sha256": hashlib.sha256(peer_files[stderr_rel]).hexdigest(),
        })
    report = {
        "schema": 1, "status": "complete",
        "scope": "fixed correctness/counter census; no timing, performance, or adoption claim",
        "source_pins_path": "/tmp/source-pins.json", "source_pins_sha256": "pins",
        "source_preflight_sha256": "preflight", "counter_schema": SCHEMA,
        "dso_realpath": DSO_PATH, "dso_sha256": DSO_SHA,
        "planned_attempts": 16, "attempt_count": 16, "attempts": attempts,
        "peer_processes": peer_processes,
        "builds_started": False, "attempts_started": True,
        "performance_claims": False, "acceptance_claims": False,
        "source_pins_stable_after_run": True,
    }
    return report, counter_files, peer_files

def validate_fixture(report, files, peer_files):
    validate_report(report, SCHEMA, lambda path, digest: files[path],
                    lambda path, digest: peer_files[path])

class MechanismValidationTests(unittest.TestCase):
    def test_fixed_matrix_has_eight_workloads_and_alternates_arms(self):
        schedule = fixed_schedule()
        self.assertEqual(len(schedule), 16)
        self.assertEqual(len({row["cell"] for row in schedule}), 8)
        for index in range(0, 16, 2):
            self.assertEqual(schedule[index]["variant"],
                             "baseline" if (index // 2) % 2 == 0 else "owned")
            self.assertEqual(schedule[index + 1]["variant"],
                             "owned" if (index // 2) % 2 == 0 else "baseline")
            self.assertEqual(schedule[index]["cell"], schedule[index + 1]["cell"])

    def test_full_schema_fixture_accounting_passes(self):
        report, files, peer_files = make_report()
        validate_fixture(report, files, peer_files)

    def test_reordered_attempt_fails_fixed_order_check(self):
        report, files, peer_files = make_report()
        report["attempts"][0], report["attempts"][1] = report["attempts"][1], report["attempts"][0]
        with self.assertRaisesRegex(ValueError, "fixed schedule mismatch"):
            validate_fixture(report, files, peer_files)

    def test_missing_phase_fails(self):
        report, files, peer_files = make_report()
        files[report["attempts"][0]["counter_file"]].pop()
        with self.assertRaisesRegex(ValueError, "phase order/count mismatch"):
            validate_fixture(report, files, peer_files)

    def test_final_live_chunk_fails_release_gate(self):
        report, files, peer_files = make_report()
        files[report["attempts"][0]["counter_file"]][-1]["curl"]["chunks_live"] = 1
        with self.assertRaisesRegex(ValueError, "chunks_live"):
            validate_fixture(report, files, peer_files)

    def test_native_layout_may_differ_by_arm_but_not_within_arm(self):
        report, files, peer_files = make_report()
        for attempt in report["attempts"]:
            if attempt["variant"] == "owned":
                for row in files[attempt["counter_file"]]:
                    row["native"]["layout_segment_size"] = 64
        validate_fixture(report, files, peer_files)

    def test_cumulative_counter_decrease_fails(self):
        report, files, peer_files = make_report()
        files[report["attempts"][0]["counter_file"]][2]["native"]["bytes_equal_calls"] = 0
        with self.assertRaisesRegex(ValueError, "cumulative counter decreased"):
            validate_fixture(report, files, peer_files)

    def test_rt_full_warmup_and_owned_flatten_census(self):
        report, files, peer_files = make_report()
        for attempt in report["attempts"]:
            if attempt["kind"] == "roundtrip":
                self.assertEqual(attempt["peer_observation"]["payload_bytes"],
                                 (attempt["count"] + 1) * attempt["bytes"])
                counters = files[attempt["counter_file"]]
                if attempt["variant"] == "owned":
                    self.assertEqual(counters[2]["native"]["segment_live_payload_bytes"], attempt["bytes"])
                    self.assertEqual(counters[2]["native"]["bend_bytes_flatten_alloc_successes"],
                                     attempt["count"])
                    self.assertEqual(counters[2]["native"]["bend_bytes_flatten_copy_bytes"],
                                     attempt["count"] * attempt["bytes"])
        validate_fixture(report, files, peer_files)

    def test_receive_class_partition_fails_closed(self):
        report, files, peer_files = make_report()
        rows = files[report["attempts"][0]["counter_file"]]
        rows[2]["native"]["framed_recv_error_calls"] += 1
        rows[3]["native"]["framed_recv_error_calls"] += 1
        with self.assertRaisesRegex(ValueError, "framed receive call/status classes"):
            validate_fixture(report, files, peer_files)

    def test_stream_owned_body_must_be_released_by_work_end(self):
        report, files, peer_files = make_report()
        attempt = next(row for row in report["attempts"]
                       if row["kind"] == "stream" and row["variant"] == "owned")
        rows = files[attempt["counter_file"]]
        rows[2]["native"]["segment_alloc_attempts"] += 1
        rows[2]["native"]["segment_alloc_successes"] += 1
        rows[2]["native"]["segment_alloc_request_bytes"] += 64
        rows[2]["native"]["segments_live"] = 1
        rows[2]["native"]["segments_live_request_bytes"] = 64
        rows[2]["native"]["segment_live_payload_bytes"] = 1
        rows[3]["native"]["segment_alloc_attempts"] += 1
        rows[3]["native"]["segment_alloc_successes"] += 1
        rows[3]["native"]["segment_alloc_request_bytes"] += 64
        rows[3]["native"]["segment_frees"] += 1
        rows[3]["native"]["segment_free_request_bytes"] += 64
        rows[3]["native"]["segments_live"] = 0
        rows[3]["native"]["segments_live_request_bytes"] = 0
        rows[3]["native"]["segment_live_payload_bytes"] = 0
        with self.assertRaisesRegex(ValueError, "native work_end gauge not zero"):
            validate_fixture(report, files, peer_files)

    def test_peer_nonzero_exit_fails(self):
        report, files, peer_files = make_report()
        report["peer_processes"][0]["exit_code"] = 1
        with self.assertRaisesRegex(ValueError, "peer process record"):
            validate_fixture(report, files, peer_files)

    def test_peer_log_checksum_fails(self):
        report, files, peer_files = make_report()
        peer_files["peers/stream.stdout.txt"] += b"tampered"
        with self.assertRaisesRegex(ValueError, "peer log checksum"):
            validate_fixture(report, files, peer_files)

    def test_peer_missing_file_fails(self):
        report, files, peer_files = make_report()
        del peer_files["peers/stream.stderr.txt"]
        with self.assertRaises(KeyError):
            validate_fixture(report, files, peer_files)

    def test_peer_tls_mismatch_fails(self):
        report, files, peer_files = make_report()
        report["attempts"][0]["peer_observation"]["tls_cipher"] = 4866
        with self.assertRaisesRegex(ValueError, "TLS version/cipher"):
            validate_fixture(report, files, peer_files)

    def test_timing_field_fails_closed(self):
        report, files, peer_files = make_report()
        report["attempts"][0]["elapsed_ms"] = 1
        with self.assertRaisesRegex(ValueError, "timing/performance field"):
            validate_fixture(report, files, peer_files)

    def test_checker_failure_fails(self):
        report, files, peer_files = make_report()
        report["attempts"][0]["exact_checker_success"] = False
        with self.assertRaisesRegex(ValueError, "exact checker"):
            validate_fixture(report, files, peer_files)

if __name__ == "__main__":
    unittest.main(verbosity=2)
