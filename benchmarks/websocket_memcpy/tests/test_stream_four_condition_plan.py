"""Schedule and full-run prerequisite checks; these tests do no network or timing work."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = ROOT / "benchmarks/websocket_memcpy/stream_four_condition.py"
SPEC = importlib.util.spec_from_file_location("websocket_memcpy_stream_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_fixed_full_plan_has_480_positives_and_exact_negative_product():
    negative, positive, counts = runner.make_plan(smoke=False)
    assert len(negative) == 8
    assert len(positive) == 480
    assert counts["total_attempts"] == 488
    assert len({row["run_id"] for row in negative + positive}) == 488
    assert {(row["backend"], row["client"], row["fault"]) for row in negative} == {
        (backend, client, fault)
        for backend in runner.BACKEND_NAMES
        for client in runner.streaming.CLIENTS
        for fault in runner.FAULTS
    }
    for workload in runner.streaming.WORKLOADS:
        rows = [row for row in positive if row["workload"]["name"] == workload["name"]]
        assert len(rows) == 80
        assert {row["repeat"] for row in rows} == set(range(20))
        row_counts = counts["row_counts_by_workload"][workload["name"]]
        assert set(row_counts.values()) == {5}
    assert counts["adoption_gate"]["primary_64k_bend"].startswith("both 65,536-byte")
    assert "receive_call_counts" not in counts["adoption_gate"]


def test_smoke_is_24_positive_checks_plus_eight_negatives():
    negative, positive, counts = runner.make_plan(smoke=True)
    assert len(negative) == 8
    assert len(positive) == 24
    assert counts["total_attempts"] == 32
    assert {row["workload"]["count"] for row in positive} == {32}
    for workload in runner.streaming.WORKLOADS:
        rows = [row for row in positive if row["workload"]["name"] == workload["name"]]
        assert len(rows) == 4
        assert {row["condition"]["id"] for row in rows} == {row["id"] for row in runner.CONDITIONS}


def test_backend_conditions_reuse_one_client_build(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BUILD", tmp_path)
    backends = {
        "baseline": {"library_path": "/pair/baseline/lib/libcurl-impersonate.so.4.8.0", "library_sha256": "base"},
        "candidate": {"library_path": "/pair/candidate/lib/libcurl-impersonate.so.4.8.0", "library_sha256": "candidate"},
    }
    binary = "/shared/client/websocket_stream"
    bridge = "/shared/client/libscrapanium.so"
    shared = {
        "stream_binary": binary, "stream_binary_sha256": "binary-sha",
        "bridge_library": bridge, "bridge_library_sha256": "bridge-sha",
    }
    calls = []
    build_record = {**shared, "client_bytes_shared_across_backend_conditions": True}
    monkeypatch.setattr(runner, "build_measurement_client", lambda row: calls.append(row) or build_record)
    monkeypatch.setattr(runner, "client_info", lambda library_dir: shared)

    actual_build, clients = runner.prepare_measurement_clients(backends)

    assert calls == [backends["baseline"]]
    assert actual_build is build_record
    assert clients["baseline"]["stream_binary"] == clients["candidate"]["stream_binary"] == binary
    assert clients["baseline"]["stream_binary_sha256"] == clients["candidate"]["stream_binary_sha256"]
    assert clients["baseline"]["bridge_library"] == clients["candidate"]["bridge_library"] == bridge
    assert clients["baseline"]["bridge_library_sha256"] == clients["candidate"]["bridge_library_sha256"]


def test_runtime_backend_selection_changes_only_library_path(monkeypatch):
    monkeypatch.setenv("SCRAPANIUM_CURL_DIR", "/unused/ambient-prefix")
    backends = {
        "baseline": {"library_path": "/pair/baseline/lib/libcurl-impersonate.so.4.8.0", "prefix": "/pair/baseline"},
        "candidate": {"library_path": "/pair/candidate/lib/libcurl-impersonate.so.4.8.0", "prefix": "/pair/candidate"},
    }
    baseline = runner.make_client_env(backends, "baseline")
    candidate = runner.make_client_env(backends, "candidate")
    assert baseline.keys() == candidate.keys()
    assert {key for key in baseline if baseline[key] != candidate[key]} == {"LD_LIBRARY_PATH"}
    assert "SCRAPANIUM_CURL_DIR" not in baseline
    assert "SCRAPANIUM_CURL_DIR" not in candidate


def _write_gate(path: Path, *, baseline="base-sha", candidate="candidate-sha", checks=None, artifacts=None):
    evidence = path.parent / "evidence.log"
    evidence.write_text("retained correctness output\n", encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    gate = {
        "schema": "scrapanium-wss-memcpy-correctness-gate-v1",
        "client_source_revision": runner.CLIENT_SOURCE_REV,
        "baseline_dso_sha256": baseline,
        "candidate_dso_sha256": candidate,
        "checks": checks or {name: True for name in (
            "guard_fixture", "baseline_full_suite", "candidate_full_suite",
            "baseline_ws_sanitizer", "candidate_ws_sanitizer",
            "baseline_phase_sanitizer", "candidate_phase_sanitizer",
            "baseline_attribution_sanitizer", "candidate_attribution_sanitizer")},
        "artifacts": artifacts or [{"path": str(evidence), "sha256": digest}],
        "no_performance_data": True,
    }
    path.write_text(json.dumps(gate), encoding="utf-8")
    return gate


def test_full_gate_is_bound_to_both_dsos_and_hashed_outputs(tmp_path):
    path = tmp_path / "correctness-gate.json"
    _write_gate(path)
    backends = {
        "baseline": {"library_sha256": "base-sha"},
        "candidate": {"library_sha256": "candidate-sha"},
    }
    parsed = runner.validate_correctness_gate(path, backends)
    assert parsed["no_performance_data"] is True


@pytest.mark.parametrize("kind", ["wrong-pair", "failed-check", "changed-artifact"])
def test_full_gate_rejects_stale_or_incomplete_evidence(tmp_path, kind):
    path = tmp_path / "correctness-gate.json"
    if kind == "wrong-pair":
        _write_gate(path, candidate="different-candidate")
    elif kind == "failed-check":
        checks = {name: True for name in (
            "guard_fixture", "baseline_full_suite", "candidate_full_suite",
            "baseline_ws_sanitizer", "candidate_ws_sanitizer",
            "baseline_phase_sanitizer", "candidate_phase_sanitizer",
            "baseline_attribution_sanitizer", "candidate_attribution_sanitizer")}
        checks["candidate_phase_sanitizer"] = False
        _write_gate(path, checks=checks)
    else:
        _write_gate(path)
        gate = json.loads(path.read_text(encoding="utf-8"))
        gate["artifacts"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(gate), encoding="utf-8")
    backends = {
        "baseline": {"library_sha256": "base-sha"},
        "candidate": {"library_sha256": "candidate-sha"},
    }
    with pytest.raises(RuntimeError):
        runner.validate_correctness_gate(path, backends)
