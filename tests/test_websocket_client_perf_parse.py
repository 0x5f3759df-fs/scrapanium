"""Offline validation of perf header and timestamp parsing."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.websocket_client_perf_parse import (
    PerfParseError,
    decimal_seconds_to_ns,
    filter_samples,
    parse_perf_header,
    parse_perf_script,
)


FEASIBILITY = ROOT / "benchmarks/results/wss-client-perf-feasibility-20260923"

# Captured from `perf report --header-only` for the retained WSS feasibility
# profile. The cpu-clock line intentionally includes exclude_callchain_user=1:
# DWARF user callchains are unwound offline from the captured user stack.
ACTUAL_HEADER = """# perf version : 7.0.14
# event : name = cpu-clock:u, , id = { 12, 13, 14 }, type = 1 (PERF_TYPE_SOFTWARE), size = 144, config = 0 (PERF_COUNT_SW_CPU_CLOCK), { sample_period, sample_freq } = 999, sample_type = IP|TID|TIME|CALLCHAIN|PERIOD|REGS_USER|STACK_USER|IDENTIFIER, read_format = ID|LOST, disabled = 1, inherit = 1, exclude_kernel = 1, exclude_hv = 1, freq = 1, sample_id_all = 1, exclude_callchain_user = 1, sample_regs_user = 0xff0fff, sample_stack_user = 16384, use_clockid = 1, clockid = 1
# event : name = dummy:u, , id = { 15 }, type = 1 (PERF_TYPE_SOFTWARE), { sample_period, sample_freq } = 999, sample_type = IP|TID|TIME|PERIOD|IDENTIFIER, disabled = 1, inherit = 1, exclude_kernel = 1, exclude_hv = 1
# clockid: monotonic (1)
"""


def test_actual_retained_perf_script_parses_all_samples_and_exact_ns():
    text = (FEASIBILITY / "perf.script-ns.txt").read_text()
    parsed = parse_perf_script(text)

    assert parsed["sample_count"] == 72
    assert parsed["malformed_lines"] == []
    first, last = parsed["samples"][0], parsed["samples"][-1]
    assert (first["pid"], first["tid"], first["time_ns"], first["event"]) == (
        758, 758, 40_965_113_990, "cpu-clock:u"
    )
    assert last["time_ns"] == 41_146_340_316
    assert first["frames"][0]["symbol"] == "sp_bend_bytes_equal"
    assert first["frames"][0]["dso"].endswith("/build/bench-ws-stream")
    assert parsed["unknown_stack_sample_count"] >= 0


def test_actual_header_and_report_keep_real_event_and_loss_diagnostics():
    report = (FEASIBILITY / "perf.report.txt").read_text()
    parsed = parse_perf_header(ACTUAL_HEADER, report_text=report)

    assert parsed["perf_version"] == "7.0.14"
    assert parsed["clock"] == {"name": "monotonic", "id": 1}
    assert parsed["event"]["name"] == "cpu-clock:u"
    assert parsed["event"]["type_num"] == 1
    assert parsed["event"]["config"] == 0
    assert parsed["event"]["frequency_mode"] is True
    assert parsed["event"]["frequency_hz"] == 999
    assert parsed["event"]["sample_type"] == [
        "IP", "TID", "TIME", "CALLCHAIN", "PERIOD", "REGS_USER", "STACK_USER", "IDENTIFIER"
    ]
    assert parsed["event"]["sample_regs_user"] == 0xff0fff
    assert parsed["event"]["sample_stack_user_bytes"] == 16384
    assert parsed["event"]["clockid"] == 1
    assert parsed["event"]["use_clockid"] is True
    assert parsed["event"]["inherit"] is True
    assert parsed["event"]["exclude_kernel"] is True
    assert parsed["event"]["exclude_hv"] is True
    # This is expected for the DWARF user-callchain configuration.
    assert parsed["event"]["exclude_callchain_user"] is True
    assert parsed["reported_sample_count"] == 72
    assert parsed["lost_samples"] == 0
    assert parsed["events"] == 1
    assert parsed["event_count_all"] == 2
    assert parsed["event"]["sample_type"] is not None
    assert parsed["throttle_records"] is None
    assert parsed["unthrottle_records"] is None
    assert parsed["diagnostic_sources"]["record_dump_completeness"] == "unknown"


def test_timestamp_conversion_is_integer_exact_and_rejects_rounding_inputs():
    assert decimal_seconds_to_ns("40.965113990") == 40_965_113_990
    assert decimal_seconds_to_ns("0.000000001") == 1
    assert decimal_seconds_to_ns("1.25") == 1_250_000_000
    assert decimal_seconds_to_ns("281474.976710656") == 281_474_976_710_656
    for value in ("-1", "+1", "1e-9", "NaN", "inf", "1.0000000001", "01.0", "1."):
        with pytest.raises(PerfParseError):
            decimal_seconds_to_ns(value)


def test_callchain_unknown_frames_and_malformed_rows_are_retained():
    text = """    42/43        1.000000000: cpu-clock:u:
\t 0x1234 [unknown] ([unknown])
\t 0x1235 known_symbol (/tmp/client)
not a perf sample
    42/43 1e-9: cpu-clock:u:
"""
    parsed = parse_perf_script(text)

    assert parsed["sample_count"] == 1
    assert parsed["unknown_frame_count"] == 1
    assert parsed["unknown_stack_sample_count"] == 1
    assert parsed["samples"][0]["stack_status"] == "unresolved"
    assert parsed["truncated_stack_sample_count"] is None
    assert parsed["truncation_status"] == "unobservable_from_perf_script_text"
    assert parsed["samples"][0]["frames"][0]["symbol"] == "[unknown]"
    assert len(parsed["malformed_lines"]) == 2
    assert parsed["malformed_lines"][0]["text"] == "not a perf sample"
    assert parsed["malformed_lines"][1]["text"].strip().startswith("42/43 1e-9")


def test_callchain_frames_and_inline_symbols_are_preserved():
    parsed = parse_perf_script(
        "11/11 3.000000000: cpu-clock:u: 0x100 leaf (/tmp/client)\n"
        "\t 0x200 inline_helper (/tmp/client) (inlined)\n"
        "\t 0x300 caller (/tmp/lib.so)\n"
    )
    sample = parsed["samples"][0]
    assert [frame["symbol"] for frame in sample["frames"]] == [
        "leaf", "inline_helper", "caller"
    ]
    assert sample["frames"][1]["inlined"] is True
    assert sample["frames"][2]["dso"] == "/tmp/lib.so"
    assert sample["stack_status"] == "resolved"


def test_window_filter_is_integer_half_open_pid_and_tid_scoped_and_retains_exclusions():
    text = """    42/43 0.999999999: cpu-clock:u:
\t 0x100 before (/tmp/client)
    42/43 1.000000000: cpu-clock:u:
\t 0x101 [unknown] ([unknown])
    42/44 1.500000000: cpu-clock:u:
\t 0x102 other_thread (/tmp/client)
    99/43 1.500000000: cpu-clock:u:
\t 0x103 other_process (/tmp/other)
    42/43 1.999999999: cpu-clock:u:
\t 0x104 last_inside (/tmp/client)
    42/43 2.000000000: cpu-clock:u:
\t 0x105 at_end (/tmp/client)
"""
    parsed = parse_perf_script(text)
    result = filter_samples(parsed, 42, {43}, 1_000_000_000, 2_000_000_000)

    assert [row["time_ns"] for row in result["selected"]] == [
        1_000_000_000, 1_999_999_999
    ]
    assert result["selected_sample_count"] == 2
    assert result["selected_unknown_stack_sample_count"] == 1
    assert result["excluded_counts"] == {
        "wrong_pid": 1, "wrong_tid": 1, "wrong_event": 0,
        "before_window": 1, "at_or_after_end": 1
    }
    assert len(result["excluded"]) == 4
    assert {row["reason"] for row in result["excluded"]} == {
        reason for reason, count in result["excluded_counts"].items() if count
    }


def test_filter_rejects_invalid_pid_tid_and_window_boundaries():
    parsed = {"samples": []}
    bad_calls = [
        (True, {1}, 0, 1),
        (1, set(), 0, 1),
        (1, {True}, 0, 1),
        (1, {1}, 1, 1),
        (1, {1}, 2, 1),
        (1, {1}, False, 1),
    ]
    for args in bad_calls:
        with pytest.raises(ValueError):
            filter_samples(parsed, *args)


def test_wrong_event_is_an_explicit_exclusion_and_malformed_rows_invalidate_attribution():
    text = """10/10 1.000000000: task-clock:u:
\t 0x100 wrong_event (/tmp/client)
10/10 1.000000001: cpu-clock:u:
\t 0x101 [unknown] ([unknown])
malformed record
"""
    parsed = parse_perf_script(text)
    result = filter_samples(parsed, 10, {10}, 1_000_000_000, 2_000_000_000)

    assert result["excluded_counts"]["wrong_event"] == 1
    assert result["excluded"][0]["reason"] == "wrong_event"
    assert result["selected_sample_count"] == 1
    assert result["malformed_line_count"] == 1
    assert result["attribution_valid"] is False

    no_malformed = parse_perf_script(text.rsplit("malformed record\n", 1)[0])
    no_malformed_result = filter_samples(
        no_malformed, 10, {10}, 1_000_000_000, 2_000_000_000
    )
    assert no_malformed_result["malformed_line_count"] == 0
    assert no_malformed_result["wrong_event_sample_count"] == 1
    assert no_malformed_result["attribution_valid"] is False


def test_wrong_pid_tid_outside_window_are_retained_but_do_not_invalidate_scope():
    parsed = parse_perf_script(
        "99/99 0.999999999: cpu-clock:u: 0x100 before (/tmp/other)\n"
        "10/11 1.500000000: cpu-clock:u: 0x101 unrelated_thread (/tmp/client)\n"
        "10/10 1.500000000: cpu-clock:u: 0x102 selected (/tmp/client)\n"
        "10/12 2.000000000: cpu-clock:u: 0x103 after (/tmp/client)\n"
    )
    result = filter_samples(parsed, 10, {10}, 1_000_000_000, 2_000_000_000)
    assert result["selected_sample_count"] == 1
    assert result["excluded_counts"]["wrong_pid"] == 1
    assert result["excluded_counts"]["wrong_tid"] == 2
    assert result["in_window_identity_mismatch_count"] == 1
    assert result["attribution_valid"] is False

    outside_only = parse_perf_script(
        "99/99 0.999999999: cpu-clock:u: 0x100 before (/tmp/other)\n"
        "10/12 2.000000000: cpu-clock:u: 0x103 after (/tmp/client)\n"
        "10/10 1.500000000: cpu-clock:u: 0x102 selected (/tmp/client)\n"
    )
    outside_result = filter_samples(outside_only, 10, {10}, 1_000_000_000, 2_000_000_000)
    assert outside_result["selected_sample_count"] == 1
    assert outside_result["in_window_identity_mismatch_count"] == 0
    assert outside_result["attribution_valid"] is True


def test_missing_stack_is_distinct_from_unresolved_and_truncation_remains_unknown():
    parsed = parse_perf_script(
        "10/10 1.0: cpu-clock:u:\n"
        "10/10 1.1: cpu-clock:u: 0x101 [unknown] ([unknown])\n"
        "10/10 1.2: cpu-clock:u: 0x102 resolved (/tmp/client)\n"
    )
    assert [row["stack_status"] for row in parsed["samples"]] == [
        "missing", "unresolved", "resolved"
    ]
    assert parsed["missing_stack_sample_count"] == 1
    assert parsed["unresolved_stack_sample_count"] == 1
    assert parsed["truncated_stack_sample_count"] is None


def test_header_requires_profile_fields_to_be_parseable_and_unique():
    duplicate = ACTUAL_HEADER + ACTUAL_HEADER.splitlines()[1] + "\n"
    with pytest.raises(PerfParseError, match="duplicate"):
        parse_perf_header(duplicate)
    bad_number = ACTUAL_HEADER.replace("config = 0", "config = nope")
    with pytest.raises(PerfParseError, match="config"):
        parse_perf_header(bad_number)

    disabled_clock = ACTUAL_HEADER.replace("use_clockid = 1", "use_clockid = 0")
    assert parse_perf_header(disabled_clock)["event"]["use_clockid"] is False
    missing_clock_setting = ACTUAL_HEADER.replace(", use_clockid = 1", "")
    assert parse_perf_header(missing_clock_setting)["event"]["use_clockid"] is None


def test_header_reports_lost_and_throttle_records_without_silently_zeroing_missing_data():
    report = "# Samples: 3 of event 'cpu-clock:u'\n# Total Lost Samples: 7\n"
    raw = """PERF_RECORD_LOST: id 3 lost 4
PERF_RECORD_THROTTLE: time 12
PERF_RECORD_UNTHROTTLE: time 20
"""
    parsed = parse_perf_header(ACTUAL_HEADER, report, raw)
    assert parsed["reported_sample_count"] == 3
    assert parsed["lost_samples"] == 7
    assert parsed["lost_records"] == 1
    assert parsed["lost_record_sample_count"] == 4
    assert parsed["throttle_records"] == 1
    assert parsed["unthrottle_records"] == 1

    script = parse_perf_script(raw)
    assert script["lost_record_count"] == 1
    assert script["lost_record_sample_count"] == 4
    assert script["throttle_record_count"] == 1
    assert script["unthrottle_record_count"] == 1

    absent = parse_perf_header(ACTUAL_HEADER)
    assert absent["reported_sample_count"] is None
    assert absent["lost_samples"] is None
    assert absent["lost_records"] is None
    assert absent["throttle_records"] is None
    assert absent["unthrottle_records"] is None


def test_header_requires_cpu_clock_and_clockid():
    with pytest.raises(PerfParseError):
        parse_perf_header("# perf version : 7.0.14\n# clockid: monotonic (1)\n")
    no_clock = ACTUAL_HEADER.replace("# clockid: monotonic (1)\n", "")
    with pytest.raises(PerfParseError):
        parse_perf_header(no_clock)
