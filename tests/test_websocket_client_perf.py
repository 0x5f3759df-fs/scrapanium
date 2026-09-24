from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
import websocket_client_perf as perf


def test_smoke_schedule_is_fixed_and_controls_precede_positive_rows():
    plan = perf.make_plan("smoke", 20260923)
    negatives = [row for row in plan if row["kind"] == "negative_control"]
    positives = [row for row in plan if row["kind"] == "positive"]
    assert len(negatives) == 16
    assert len(positives) == 32
    assert plan[:16] == negatives
    assert plan[16:] == positives
    for batch in perf.BATCHES:
        rows = [row for row in positives if row["batch"] == batch]
        assert len(rows) == perf.SMOKE_REPEATS * 4
        assert Counter((row["client"], row["sampling"]) for row in rows) == Counter({
            (client, sampling): perf.SMOKE_REPEATS
            for client in perf.CLIENTS for sampling in perf.SAMPLING
        })


def test_full_plan_has_exact_predeclared_shape_and_balanced_williams_blocks():
    plan = perf.make_plan("full", 20260923)
    assert len(plan) == 784
    assert sum(row["kind"] == "negative_control" for row in plan) == 16
    positives = plan[16:]
    assert len(positives) == 768
    for batch in perf.BATCHES:
        for client in perf.CLIENTS:
            for sampling in perf.SAMPLING:
                assert sum(row["batch"] == batch and row["client"] == client
                           and row["sampling"] == sampling for row in positives) == 96


def test_full_schedule_seed_reproduces_exact_row_order():
    assert perf.make_plan("full", 20260923) == perf.make_plan("full", 20260923)
    assert perf.make_plan("full", 20260923) != perf.make_plan("full", 20260924)


def test_schedule_rejects_duplicate_or_missing_control_rows():
    plan = perf.make_plan("smoke")
    duplicate = list(plan)
    duplicate[1] = dict(duplicate[0])
    with pytest.raises(ValueError, match="negative controls"):
        perf.validate_plan(duplicate, "smoke")
    with pytest.raises(ValueError, match="wrong fixed number"):
        perf.validate_plan(plan[:-1], "smoke")


def test_williams_validator_rejects_unbalanced_positions_or_pairs():
    rows = perf.condition_rows(17, 4)
    assert len(rows) == 4
    invalid = [row[:] for row in rows]
    invalid[0] = invalid[1][:]
    with pytest.raises(ValueError):
        perf.validate_williams_rows(invalid)


def test_client_interval_accepts_exact_integer_ns_boundaries():
    start = 987654321012345678
    end = start + 1234567
    elapsed_us = (end - start) // 1000
    result = perf.parse_client_interval(
        f"WSS_PERF_INTERVAL_NS={start},{end}\nclient_interval_elapsed_us={elapsed_us}\n")
    assert result == {"start_ns": start, "end_ns": end,
                      "elapsed_ns": end - start, "elapsed_us": elapsed_us}


@pytest.mark.parametrize("text", [
    "WSS_PERF_INTERVAL_NS=10,11\n",
    "WSS_PERF_INTERVAL_NS=10,9\nclient_interval_elapsed_us=0\n",
    "WSS_PERF_INTERVAL_NS=10,11\nclient_interval_elapsed_us=2\n",
    "WSS_PERF_INTERVAL_NS=10,11\nclient_interval_elapsed_us=0\n",
    "WSS_PERF_INTERVAL_NS=10,11\nclient_interval_elapsed_us=1\n"
    "WSS_PERF_INTERVAL_NS=12,13\nclient_interval_elapsed_us=1\n",
])
def test_client_interval_rejects_missing_invalid_or_duplicate_markers(text):
    with pytest.raises(ValueError):
        perf.parse_client_interval(text)


def test_positive_peer_validation_requires_all_exact_payload_counters():
    row = {"kind": "positive", "count": 4}
    record = {"warmups": 1, "starts": 1, "data_frames": 4,
              "data_payload_bytes": 4 * perf.SIZE,
              "data_frame_bytes": 4 * (perf.SIZE + 10),
              "tls_version": 772, "tls_cipher": 4865}
    assert perf.validate_peer(record, row)["data_frames"] == 4
    record["data_frame_bytes"] -= 1
    with pytest.raises(ValueError, match="positive frame/payload"):
        perf.validate_peer(record, row)


def test_negative_peer_validation_retains_consistent_partial_fault_evidence():
    row = {"kind": "negative_control", "count": 8}
    record = {"warmups": 1, "starts": 1, "data_frames": 2,
              "data_payload_bytes": 2 * perf.SIZE,
              "data_frame_bytes": 2 * (perf.SIZE + 10),
              "tls_version": 772, "tls_cipher": 4865,
              "error": "connection reset by peer"}
    assert perf.validate_peer(record, row)["data_frames"] == 2
    record["data_payload_bytes"] += 1
    with pytest.raises(ValueError, match="internally inconsistent"):
        perf.validate_peer(record, row)


def test_negative_client_control_requires_exact_mismatch_diagnostic():
    row = {"kind": "negative_control", "fault": "swap"}
    output = "WebSocket opcode, sequence, or payload mismatch"
    assert perf.validate_client_result(row, 1, output)["rejected_expected_corruption"]
    with pytest.raises(ValueError, match="unexpectedly passed"):
        perf.validate_client_result(row, 0, output)
    with pytest.raises(ValueError, match="other than exact data rejection"):
        perf.validate_client_result(row, 1, "connection reset")

