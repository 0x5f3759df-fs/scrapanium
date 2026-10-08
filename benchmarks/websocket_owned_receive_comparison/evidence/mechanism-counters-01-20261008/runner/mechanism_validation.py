from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib

PHASES = ("cold", "gate", "work_end", "released")
VARIANTS = ("baseline", "owned")
STREAM_CELLS = (
    ("stream-30b-flush1", 30, 65536, 1),
    ("stream-1024b-flush1", 1024, 16384, 1),
    ("stream-65536b-flush1", 65536, 1024, 1),
    ("stream-30b-flush64", 30, 65536, 64),
    ("stream-1024b-flush64", 1024, 16384, 64),
    ("stream-65536b-flush64", 65536, 1024, 64),
)
ROUNDTRIP_CELLS = (
    ("go-30b-c1", 30, 5000),
    ("go-65536b-c1", 65536, 300),
)
TLS_VERSION = 772
TLS_CIPHER = 4865
DSO_SHA256 = "e48e14edacc7d8ed0abab6f5f77e5d9cebca1a87c0153ce7e939b407252e9ab9"

REPORT_KEYS = {
    "schema", "status", "scope", "source_pins_path", "source_pins_sha256",
    "source_preflight_sha256", "counter_schema", "dso_realpath", "dso_sha256",
    "planned_attempts", "attempt_count", "attempts", "builds_started",
    "attempts_started", "performance_claims", "acceptance_claims",
    "source_pins_stable_after_run", "peer_processes",
}
ATTEMPT_EXTRA_KEYS = {
    "client", "attempt_status", "returncode", "exact_checker_success",
    "mapped_backend", "peer_observation", "counter_file", "counter_file_sha256",
}

def fixed_schedule() -> list[dict[str, Any]]:
    cells = [
        {"cell": name, "kind": "stream", "bytes": size, "count": count,
         "peer_flush_frames": batch, "connections": 1}
        for name, size, count, batch in STREAM_CELLS
    ] + [
        {"cell": name, "kind": "roundtrip", "bytes": size, "count": count,
         "peer_flush_frames": None, "connections": 1}
        for name, size, count in ROUNDTRIP_CELLS
    ]
    result = []
    for index, workload in enumerate(cells):
        order = ("baseline", "owned") if index % 2 == 0 else ("owned", "baseline")
        for variant in order:
            result.append({"attempt_index": len(result), **workload, "variant": variant})
    return result

def header_bytes(payload_bytes: int) -> int:
    return 2 if payload_bytes < 126 else 4 if payload_bytes < 65536 else 10

def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)

def _no_timing_fields(value: Any, path: str = "$") -> None:
    forbidden = ("elapsed", "duration", "throughput", "latency", "rate",
                 "start_ms", "end_ms", "time_ns", "cpu_seconds", "user_seconds",
                 "system_seconds")
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            _need(not any(token in lowered for token in forbidden),
                  f"timing/performance field is forbidden at {path}.{key}")
            _no_timing_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _no_timing_fields(child, f"{path}[{index}]")
    elif isinstance(value, float):
        raise ValueError(f"floating-point value is forbidden in counters-only data at {path}")

def _validate_counter_file(counter_rows: list[dict], schema: dict[str, list[str]]) -> dict[str, dict]:
    _need([row.get("phase") for row in counter_rows] == list(PHASES),
          "counter phase order/count mismatch")
    _need(set(schema) == {"native", "curl"}, "counter schema object mismatch")
    for row in counter_rows:
        _need(set(row) == {"phase", "native", "curl"}, "counter row has unexpected/missing fields")
        for area in ("native", "curl"):
            values = row.get(area)
            _need(isinstance(values, dict), f"{area} counter object missing")
            _need(set(values) == set(schema[area]),
                  f"{area} counter names differ from the frozen source schema")
            for name, value in values.items():
                _need(type(value) is int and value >= 0,
                      f"counter must be a nonnegative integer: {area}.{name}")
    layouts = {
        area: {name: counter_rows[0][area][name] for name in schema[area] if name.startswith("layout_")}
        for area in ("native", "curl")
    }
    live_gauges = {
        "native": {"segments_live", "segments_live_request_bytes", "segment_live_payload_bytes"},
        "curl": {"chunks_live", "chunks_live_request_bytes", "pins_live", "payloads_live"},
    }
    prior = counter_rows[0]
    for row in counter_rows[1:]:
        for area in ("native", "curl"):
            current_layout = {name: row[area][name] for name in layouts[area]}
            _need(current_layout == layouts[area], f"{area} layout changed between snapshots")
            for name in schema[area]:
                if name.startswith("layout_") or name in live_gauges[area]:
                    continue
                _need(row[area][name] >= prior[area][name],
                      f"cumulative counter decreased: {area}.{name}")
        prior = row
    _need(layouts["curl"] == {
        "layout_chunk_size": 48, "layout_bufq_size": 64,
        "layout_pin_size": 32, "layout_payload_size": 32,
        "layout_recv_chunk_size": 131072,
    }, "Curl diagnostic layout differs from the reviewed DSO")
    return {row["phase"]: row for row in counter_rows}

def _validate_balances(phases: dict[str, dict], schema: dict[str, list[str]]) -> None:
    for area, gauges in (
        ("native", {
            "segments_live": ("segment_alloc_successes", "segment_frees"),
            "segments_live_request_bytes": ("segment_alloc_request_bytes", "segment_free_request_bytes"),
        }),
        ("curl", {
            "chunks_live": ("chunk_alloc_successes", "chunk_physical_frees"),
            "chunks_live_request_bytes": ("chunk_alloc_success_bytes", "chunk_physical_free_request_bytes"),
            "pins_live": ("pin_alloc_successes", "pin_releases"),
            "payloads_live": ("payload_alloc_successes", "payload_frees"),
        }),
    ):
        available = set(schema[area])
        for phase_name, snapshot in phases.items():
            values = snapshot[area]
            for gauge, (allocated, freed) in gauges.items():
                _need({gauge, allocated, freed} <= available,
                      f"required balance counters missing: {area}.{gauge}")
                _need(values[gauge] == values[allocated] - values[freed],
                      f"{area}.{gauge} allocation/free reconciliation failed at {phase_name}")
    native = phases["released"]["native"]
    curl = phases["released"]["curl"]
    _need(native["segment_alloc_attempts"] == native["segment_alloc_successes"]
          and native["segment_alloc_failures"] == 0,
          "native segment allocation failure/attempt mismatch")
    _need(native["message_realloc_failures"] == 0
          and native["ws_flatten_alloc_failures"] == 0
          and native["bend_bytes_flatten_alloc_failures"] == 0,
          "native message/flatten allocation failure observed")
    _need(curl["chunk_alloc_attempts"] == curl["chunk_alloc_successes"],
          "Curl chunk allocation attempt mismatch")
    _need(curl["chunk_alloc_request_bytes"] == curl["chunk_alloc_success_bytes"],
          "Curl chunk requested/success byte mismatch")
    _need(curl["pin_alloc_attempts"] == curl["pin_alloc_successes"]
          and curl["pin_alloc_failures"] == 0 and curl["pin_acquire_failures"] == 0,
          "Curl pin allocation/acquire failure observed")
    _need(curl["payload_alloc_attempts"] == curl["payload_alloc_successes"]
          and curl["payload_alloc_failures"] == 0,
          "Curl payload allocation failure observed")
    for gauge in ("segments_live", "segments_live_request_bytes", "segment_live_payload_bytes"):
        _need(native[gauge] == 0, f"native final gauge not zero: {gauge}")
    for gauge in ("chunks_live", "chunks_live_request_bytes", "pins_live", "payloads_live"):
        _need(curl[gauge] == 0, f"Curl final gauge not zero: {gauge}")

def _delta(phases: dict[str, dict], area: str, name: str) -> int:
    return phases["work_end"][area][name] - phases["gate"][area][name]

def _validate_measured_interval(actual: dict, phases: dict[str, dict]) -> None:
    count, size = actual["count"], actual["bytes"]
    kind, variant = actual["kind"], actual["variant"]
    expected_recv_bytes = count * size

    framed_calls = _delta(phases, "native", "framed_recv_calls")
    framed_success = _delta(phases, "native", "framed_recv_success_calls")
    framed_again = _delta(phases, "native", "framed_recv_again_calls")
    framed_error = _delta(phases, "native", "framed_recv_error_calls")
    owned_calls = _delta(phases, "native", "owned_recv_calls")
    owned_success = _delta(phases, "native", "owned_recv_success_calls")
    owned_again = _delta(phases, "native", "owned_recv_again_calls")
    owned_error = _delta(phases, "native", "owned_recv_error_calls")
    direct_calls = _delta(phases, "native", "direct_recv_calls")
    scratch_calls = _delta(phases, "native", "scratch_recv_calls")
    direct_success = _delta(phases, "native", "direct_recv_success")
    scratch_success = _delta(phases, "native", "scratch_recv_success")
    _need(framed_calls == framed_success + framed_again + framed_error,
          "framed receive call/status classes do not reconcile")
    _need(owned_calls == owned_success + owned_again + owned_error,
          "owned receive call/status classes do not reconcile")
    _need(framed_calls == direct_calls + scratch_calls,
          "framed receive API calls do not reconcile with direct/scratch calls")
    _need(framed_success == direct_success + scratch_success,
          "framed receive successes do not reconcile with direct/scratch successes")
    _need(framed_error == 0 and owned_error == 0,
          "native receive API reported an error")
    framed_bytes = _delta(phases, "native", "framed_recv_bytes")
    owned_bytes = _delta(phases, "native", "owned_recv_bytes")
    _need(framed_bytes + owned_bytes == expected_recv_bytes,
          "native receive API byte totals differ from checked payload bytes")
    if variant == "owned":
        _need(owned_calls > 0 and owned_success > 0 and owned_bytes == expected_recv_bytes
              and framed_bytes == 0,
              "owned arm did not receive the complete measured payload through the owned API")
    else:
        _need(framed_calls > 0 and framed_success > 0 and framed_bytes == expected_recv_bytes
              and owned_calls == 0 and owned_bytes == 0,
              "baseline arm did not receive the complete measured payload through framed recv")

    send_calls = _delta(phases, "native", "ws_send_calls")
    send_success = _delta(phases, "native", "ws_send_success_calls")
    send_again = _delta(phases, "native", "ws_send_again_calls")
    send_error = _delta(phases, "native", "ws_send_error_calls")
    _need(send_calls == send_success + send_again + send_error,
          "native send call/status classes do not reconcile")
    _need(send_error == 0, "native WebSocket send reported an error")
    expected_sends = 1 if kind == "stream" else count
    expected_sent = 5 if kind == "stream" else expected_recv_bytes
    _need(send_calls >= expected_sends,
          "native send calls are fewer than logical messages")
    _need(_delta(phases, "native", "ws_send_return_bytes") == expected_sent,
          "native successful send bytes differ from checked workload")
    _need(_delta(phases, "native", "ws_send_request_bytes") >= expected_sent,
          "native send request bytes are below returned payload bytes")

    expected_flatten_calls = (1 if kind == "stream" else count) if variant == "owned" else 0
    _need(_delta(phases, "native", "bend_bytes_flatten_calls") == expected_flatten_calls,
          "Bend WebSocket Bytes.flatten call count differs from source path")
    flatten_allocs = _delta(phases, "native", "bend_bytes_flatten_alloc_attempts")
    flatten_successes = _delta(phases, "native", "bend_bytes_flatten_alloc_successes")
    flatten_failures = _delta(phases, "native", "bend_bytes_flatten_alloc_failures")
    flatten_bytes = _delta(phases, "native", "bend_bytes_flatten_alloc_bytes")
    flatten_copy_bytes = _delta(phases, "native", "bend_bytes_flatten_copy_bytes")
    _need(flatten_allocs == flatten_successes and flatten_failures == 0,
          "Bend WebSocket flatten allocation attempts/successes differ")
    if kind == "roundtrip" and variant == "owned":
        _need((flatten_allocs, flatten_bytes, flatten_copy_bytes) ==
              (count, expected_recv_bytes, expected_recv_bytes),
              "owned round-trip flatten allocation/copy totals differ")
    else:
        _need((flatten_allocs, flatten_bytes, flatten_copy_bytes) == (0, 0, 0),
              "flat-input path unexpectedly allocated/copied during Bend flatten")

    for phase in PHASES:
        for name in ("ws_flatten_calls", "ws_flatten_alloc_attempts",
                     "ws_flatten_alloc_successes", "ws_flatten_alloc_failures",
                     "ws_flatten_alloc_bytes", "ws_flatten_copy_bytes"):
            _need(phases[phase]["native"][name] == 0,
                  f"unexpected native receive flatten counter at {phase}: {name}")

    native_gate, curl_gate = phases["gate"]["native"], phases["gate"]["curl"]
    native_work_end = phases["work_end"]["native"]
    curl_work_end = phases["work_end"]["curl"]
    if kind == "stream" or variant == "baseline":
        for boundary, native_values, curl_values in (
            ("gate", native_gate, curl_gate), ("work_end", native_work_end, curl_work_end)):
            for gauge in ("segments_live", "segments_live_request_bytes", "segment_live_payload_bytes"):
                _need(native_values[gauge] == 0, f"native {boundary} gauge not zero: {gauge}")
            _need(curl_values["pins_live"] == 0 and curl_values["payloads_live"] == 0,
                  f"{boundary} retained Curl pins or payloads on a released-message path")
    else:
        for boundary, native_values, curl_values in (
            ("gate", native_gate, curl_gate), ("work_end", native_work_end, curl_work_end)):
            _need(native_values["segments_live"] > 0
                  and native_values["segments_live_request_bytes"] > 0
                  and native_values["segment_live_payload_bytes"] == size,
                  f"owned round-trip retained body is missing at {boundary}")
            _need(curl_values["pins_live"] > 0 and curl_values["payloads_live"] > 0,
                  f"owned round-trip retained body has no Curl pin/payload at {boundary}")

def _validate_peer(attempt: dict) -> None:
    peer = attempt.get("peer_observation")
    _need(isinstance(peer, dict), "peer observation missing")
    _need(peer.get("tls_version") == TLS_VERSION and peer.get("tls_cipher") == TLS_CIPHER,
          "peer TLS version/cipher differs")
    _need(not peer.get("error"), "peer reported an error")
    count, size = attempt["count"], attempt["bytes"]
    if attempt["kind"] == "stream":
        _need(peer.get("kind") == "stream", "stream peer observation kind mismatch")
        _need(peer.get("warmups") == 1 and peer.get("starts") == 1,
              "stream peer warmup/start signal count mismatch")
        _need(peer.get("data_frames") == count, "stream data frame count mismatch")
        _need(peer.get("data_payload_bytes") == count * size,
              "stream payload byte count mismatch")
        _need(peer.get("data_frame_bytes") == count * (size + header_bytes(size)),
              "stream framed-byte count mismatch")
    else:
        _need(peer.get("kind") == "roundtrip", "round-trip peer observation kind mismatch")
        frames = count + 1
        # The original Go round-trip client sends and checks one full-size payload before the gate.
        expected_payload = (count + 1) * size
        ext = 0 if size < 126 else 2 if size < 65536 else 8
        _need(peer.get("frames") == frames, "round-trip frame count mismatch")
        _need(peer.get("payload_bytes") == expected_payload, "round-trip payload byte count mismatch")
        _need(peer.get("client_frame_bytes") == expected_payload + frames * (6 + ext),
              "round-trip client frame bytes mismatch")
        _need(peer.get("server_frame_bytes") == expected_payload + frames * (2 + ext),
              "round-trip server frame bytes mismatch")

def validate_attempt(actual: dict, expected: dict, schema: dict[str, list[str]],
                     counter_rows: list[dict], dso_realpath: str) -> dict[str, dict]:
    _need(set(actual) == set(expected) | ATTEMPT_EXTRA_KEYS,
          "attempt row has unexpected or missing fields")
    _no_timing_fields(actual)
    for key, expected_value in expected.items():
        _need(actual.get(key) == expected_value,
              f"fixed schedule mismatch at attempt {expected['attempt_index']} field {key}")
    _need(actual.get("attempt_status") == "ok" and actual.get("returncode") == 0
          and actual.get("exact_checker_success") is True,
          f"attempt did not pass the existing exact checker: {expected['attempt_index']}")
    _need(actual.get("client") == "scrapanium-bend", "unexpected client in fixed matrix")
    _need(actual.get("mapped_backend") == {
        "path": dso_realpath, "sha256": DSO_SHA256},
        "client DSO mapping mismatch")
    _validate_peer(actual)
    phases = _validate_counter_file(counter_rows, schema)
    _validate_balances(phases, schema)
    values = phases
    expected_warmup = 6 if actual["kind"] == "stream" else actual["bytes"]
    _need(values["gate"]["native"]["bytes_equal_calls"] -
          values["cold"]["native"]["bytes_equal_calls"] == 1,
          "warmup exact comparison count mismatch")
    _need(values["gate"]["native"]["bytes_equal_bytes"] -
          values["cold"]["native"]["bytes_equal_bytes"] == expected_warmup,
          "warmup exact comparison byte count mismatch")
    _need(values["work_end"]["native"]["bytes_equal_calls"] -
          values["gate"]["native"]["bytes_equal_calls"] == actual["count"],
          "measured exact comparison count mismatch")
    _need(values["work_end"]["native"]["bytes_equal_bytes"] -
          values["gate"]["native"]["bytes_equal_bytes"] == actual["count"] * actual["bytes"],
          "measured exact comparison byte count mismatch")
    _need(values["released"]["native"]["bytes_equal_calls"] ==
          values["work_end"]["native"]["bytes_equal_calls"]
          and values["released"]["native"]["bytes_equal_bytes"] ==
          values["work_end"]["native"]["bytes_equal_bytes"],
          "exact comparison counters changed after work_end")
    _validate_measured_interval(actual, values)
    return phases

def validate_report(report: dict, schema: dict[str, list[str]], counter_loader, peer_loader) -> None:
    _need(set(report) == REPORT_KEYS, "report has unexpected or missing fields")
    _no_timing_fields(report)
    schedule = fixed_schedule()
    _need(report.get("schema") == 1, "report schema mismatch")
    _need(report.get("status") == "complete", "report is not complete")
    _need(report.get("performance_claims") is False and report.get("acceptance_claims") is False,
          "counters-only report contains a performance/acceptance claim")
    _need(report.get("builds_started") is False and report.get("attempts_started") is True,
          "report scope flags mismatch")
    _need(report.get("source_pins_stable_after_run") is True,
          "pinned sources were not rechecked after the run")
    _need(report.get("dso_sha256") == DSO_SHA256 and report.get("dso_realpath"),
          "report DSO identity mismatch")
    _need(report.get("planned_attempts") == 16 and report.get("attempt_count") == 16
          and len(report.get("attempts", [])) == 16,
          "fixed attempt count mismatch")
    _need(report.get("counter_schema") == schema, "report counter schema differs from source pins")
    _need(report.get("source_pins_sha256") and report.get("source_preflight_sha256"),
          "source preflight/pin hashes missing")
    peer_processes = report.get("peer_processes")
    _need(isinstance(peer_processes, list) and
          [p.get("name") for p in peer_processes] == ["stream", "roundtrip"],
          "peer process records missing or out of order")
    for peer in peer_processes:
        _need(set(peer) == {"name", "exit_code", "stdout_file", "stdout_sha256",
                            "stderr_file", "stderr_sha256"} and peer["exit_code"] == 0,
              "peer process record has unexpected fields or nonzero exit")
        for stream_name in ("stdout", "stderr"):
            rel, digest = peer[f"{stream_name}_file"], peer[f"{stream_name}_sha256"]
            _need(isinstance(rel, str) and not Path(rel).is_absolute() and
                  ".." not in Path(rel).parts,
                  "peer log path is absolute or escapes output")
            _need(isinstance(digest, str) and len(digest) == 64,
                  "peer log checksum is malformed")
            data = peer_loader(rel, digest)
            _need(isinstance(data, bytes) and hashlib.sha256(data).hexdigest() == digest,
                  "peer log checksum mismatch")
    seen_counter_files = set()
    observed_layouts = {}
    warmup_totals = {variant: {"stream_calls": 0, "stream_bytes": 0, "rt_calls": 0, "rt_bytes": 0}
                     for variant in VARIANTS}
    for actual, expected in zip(report["attempts"], schedule):
        counter_rel = actual.get("counter_file")
        _need(isinstance(counter_rel, str) and counter_rel not in seen_counter_files,
              "counter file path missing or reused")
        seen_counter_files.add(counter_rel)
        rows = counter_loader(counter_rel, actual.get("counter_file_sha256"))
        phases = validate_attempt(actual, expected, schema, rows, report["dso_realpath"])
        layout = {area: {name: value for name, value in phases["cold"][area].items()
                         if name.startswith("layout_")} for area in ("native", "curl")}
        for area in ("native", "curl"):
            layout_key = (area, actual["variant"] if area == "native" else "shared")
            previous = observed_layouts.get(layout_key)
            if previous is None:
                observed_layouts[layout_key] = layout[area]
            else:
                _need(layout[area] == previous,
                      f"{area} layout differs between attempts in {actual['variant']} arm")
        total = warmup_totals[actual["variant"]]
        if actual["kind"] == "stream":
            total["stream_calls"] += phases["gate"]["native"]["bytes_equal_calls"] - phases["cold"]["native"]["bytes_equal_calls"]
            total["stream_bytes"] += phases["gate"]["native"]["bytes_equal_bytes"] - phases["cold"]["native"]["bytes_equal_bytes"]
        else:
            total["rt_calls"] += phases["gate"]["native"]["bytes_equal_calls"] - phases["cold"]["native"]["bytes_equal_calls"]
            total["rt_bytes"] += phases["gate"]["native"]["bytes_equal_bytes"] - phases["cold"]["native"]["bytes_equal_bytes"]
    _need(warmup_totals == {
        variant: {"stream_calls": 6, "stream_bytes": 36, "rt_calls": 2, "rt_bytes": 65566}
        for variant in VARIANTS
    }, "per-arm warmup equality totals mismatch")
    for variant in VARIANTS:
        arm = [row for row in report["attempts"] if row["variant"] == variant]
        _need(len(arm) == 8, f"fixed arm count mismatch: {variant}")
        measured_stream_calls = sum(row["count"] for row in arm if row["kind"] == "stream")
        measured_stream_bytes = sum(row["count"] * row["bytes"] for row in arm if row["kind"] == "stream")
        _need((measured_stream_calls, measured_stream_bytes) == (165888, 171704320),
              f"per-arm stream equality totals mismatch: {variant}")
        measured_rt_calls = sum(row["count"] for row in arm if row["kind"] == "roundtrip")
        measured_rt_bytes = sum(row["count"] * row["bytes"] for row in arm if row["kind"] == "roundtrip")
        _need((measured_rt_calls, measured_rt_bytes) == (5300, 19810800),
              f"per-arm round-trip equality totals mismatch: {variant}")
