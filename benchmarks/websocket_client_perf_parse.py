"""Strict parsers for Linux perf text output used by the WSS client profiler."""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable


class PerfParseError(ValueError):
    """Raised when profiler output is missing fields required for attribution."""


_TIMESTAMP_RE = re.compile(r"^(?P<seconds>0|[1-9][0-9]*)(?:\.(?P<fraction>[0-9]{1,9}))?$")
_SAMPLE_RE = re.compile(
    r"^\s*(?P<pid>[0-9]+)/(?P<tid>[0-9]+)\s+"
    r"(?P<timestamp>[0-9]+(?:\.[0-9]{1,9})?):\s+"
    r"(?P<event>\S+):\s*(?P<details>.*)$"
)
_IP_RE = re.compile(r"^(?:0[xX])?[0-9a-fA-F]+$")
_EVENT_RE = re.compile(r"^#\s*event\s*:\s*name\s*=\s*([^,]+),\s*(.*)$")


class _FrameParseError(ValueError):
    pass


def decimal_seconds_to_ns(value: str) -> int:
    """Convert a perf decimal-seconds timestamp to exact integer nanoseconds.

    The profiler is invoked with ``perf script --ns``. The parser also accepts
    fewer than nine fractional digits and pads them on the right, but rejects
    excess precision rather than rounding it through a float.
    """
    if not isinstance(value, str):
        raise TypeError("perf timestamp must be text")
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise PerfParseError(f"invalid decimal-seconds timestamp: {value!r}")
    seconds = int(match.group("seconds"))
    fraction = match.group("fraction") or ""
    return seconds * 1_000_000_000 + int(fraction.ljust(9, "0") or "0")


def _integer(text: str, field: str, *, minimum: int = 0) -> int:
    if not text.isdigit():
        raise PerfParseError(f"invalid {field}: {text!r}")
    value = int(text)
    if value < minimum:
        raise PerfParseError(f"{field} must be >= {minimum}")
    return value


def _number_attribute(line: str, name: str) -> int | None:
    raw = _attribute(line, name)
    if raw is None:
        return None
    try:
        return int(raw, 0)
    except ValueError as error:
        raise PerfParseError(f"invalid integer perf attribute {name}={raw!r}") from error


def _attribute(line: str, name: str) -> str | None:
    match = re.search(rf"(?:^|[,\s]){re.escape(name)}\s*=\s*([^,\s]+)", line)
    return match.group(1) if match else None


def _bool_attribute(line: str, name: str) -> bool | None:
    raw = _attribute(line, name)
    if raw is None:
        return None
    if raw in ("1", "true", "True"):
        return True
    if raw in ("0", "false", "False"):
        return False
    raise PerfParseError(f"invalid boolean perf attribute {name}={raw!r}")


def _parse_event(line: str) -> dict[str, object] | None:
    match = _EVENT_RE.match(line)
    if match is None:
        return None
    name, attributes = match.groups()
    name = name.strip()
    frequency = re.search(
        r"\{\s*sample_period\s*,\s*sample_freq\s*\}\s*=\s*([0-9]+)", attributes
    )
    if frequency is None:
        frequency = re.search(r"\bsample_freq\s*=\s*([0-9]+)", attributes)
    period = re.search(r"\{\s*sample_period\s*\}\s*=\s*([0-9]+)", attributes)
    sample_frequency = _attribute(attributes, "sample_freq")
    sample_period = _attribute(attributes, "sample_period")
    if frequency is not None:
        sample_frequency = frequency.group(1)
    elif period is not None:
        sample_period = period.group(1)
    freq_mode = _bool_attribute(attributes, "freq")
    if freq_mode is True:
        frequency_hz = int(sample_frequency) if sample_frequency is not None else None
    else:
        frequency_hz = None
    period_value = int(sample_period) if sample_period is not None else None
    type_match = re.search(r"\btype\s*=\s*([0-9]+)(?:\s*\([^)]*\))?", attributes)
    sample_type_raw = _attribute(attributes, "sample_type")
    sample_type = (
        [token for token in sample_type_raw.split("|") if token]
        if sample_type_raw is not None else None
    )
    regs_user = _number_attribute(attributes, "sample_regs_user")
    stack_user_bytes = _number_attribute(attributes, "sample_stack_user")
    clockid = _number_attribute(attributes, "clockid")
    use_clockid = _bool_attribute(attributes, "use_clockid")
    return {
        "name": name,
        "type_num": int(type_match.group(1)) if type_match else None,
        "config": _number_attribute(attributes, "config"),
        "frequency_mode": freq_mode,
        "frequency_hz": frequency_hz,
        "sample_period": period_value,
        "sample_type": sample_type,
        "sample_regs_user": regs_user,
        "sample_stack_user_bytes": stack_user_bytes,
        "clockid": clockid,
        # Keep the explicit perf attribute; clockid presence alone is not proof
        # that the event was sampled against that clock.
        "use_clockid": use_clockid,
        "inherit": _bool_attribute(attributes, "inherit"),
        "exclude_kernel": _bool_attribute(attributes, "exclude_kernel"),
        "exclude_hv": _bool_attribute(attributes, "exclude_hv"),
        # perf sets this for user DWARF unwinding; do not require it to be false.
        "exclude_callchain_user": _bool_attribute(attributes, "exclude_callchain_user"),
        "disabled": _bool_attribute(attributes, "disabled"),
    }


def _parse_count_lines(text: str, pattern: re.Pattern[str], label: str) -> int | None:
    matches = [int(match.group(1)) for line in text.splitlines() if (match := pattern.search(line))]
    if not matches:
        return None
    if len(set(matches)) != 1:
        raise PerfParseError(f"conflicting {label} values: {matches}")
    return matches[0]


_SAMPLE_COUNT_RE = re.compile(r"^#\s*Samples:\s*([0-9]+)(?:\s|$)")
_LOST_COUNT_RE = re.compile(r"Total Lost Samples:\s*([0-9]+)", re.IGNORECASE)
_LOST_RECORD_RE = re.compile(r"PERF_RECORD_LOST\b", re.IGNORECASE)
_LOST_VALUE_RE = re.compile(r"\blost\s*(?::|=|\s)\s*([0-9]+)", re.IGNORECASE)
_THROTTLE_RE = re.compile(r"PERF_RECORD_THROTTLE\b", re.IGNORECASE)
_UNTHROTTLE_RE = re.compile(r"PERF_RECORD_UNTHROTTLE\b", re.IGNORECASE)


def parse_perf_header(
    header_text: str, report_text: str = "", raw_trace_text: str = ""
) -> dict[str, object]:
    """Parse perf header plus optional report/record-text diagnostics.

    Missing loss/throttle summaries remain ``None``. Callers must not interpret
    absent diagnostics as a zero-loss recording. ``events`` counts sampled
    events and excludes perf's synthetic ``dummy:u`` control event; the full
    parsed list is available as ``event_rows``.
    """
    if not isinstance(header_text, str) or not isinstance(report_text, str) or not isinstance(raw_trace_text, str):
        raise TypeError("perf header, report, and raw trace must be text")

    events = []
    for line in header_text.splitlines():
        if re.match(r"^#\s*event\b", line):
            event_row = _parse_event(line)
            if event_row is None:
                raise PerfParseError(f"malformed perf event header: {line!r}")
            events.append(event_row)
    expected_events = [row for row in events if row["name"] == "cpu-clock:u"]
    if not expected_events:
        raise PerfParseError("perf header has no cpu-clock:u event")
    if len(expected_events) != 1:
        raise PerfParseError("perf header has duplicate cpu-clock:u events")
    event = expected_events[0]

    version_match = re.search(r"^#\s*perf version\s*:\s*(\S+)\s*$", header_text, re.MULTILINE)
    clock_match = re.search(r"^#\s*clockid\s*:\s*([^\r\n]+)$", header_text, re.MULTILINE)
    clock_name = None
    clock_id = None
    if clock_match:
        value = clock_match.group(1).strip()
        named = re.match(r"^(.*?)\s*\(([0-9]+)\)\s*$", value)
        if named:
            clock_name = named.group(1).strip()
            clock_id = int(named.group(2))
        else:
            clock_name = value
    if clock_name is None:
        raise PerfParseError("perf header is missing its clockid line")

    sample_count = _parse_count_lines(report_text, _SAMPLE_COUNT_RE, "sample count")
    lost_samples = _parse_count_lines(report_text, _LOST_COUNT_RE, "lost sample count")
    raw_lost_matches = list(_LOST_RECORD_RE.finditer(raw_trace_text)) if raw_trace_text else []
    raw_lost_records = len(raw_lost_matches) if raw_lost_matches else None
    lost_values = [int(match.group(1)) for match in _LOST_VALUE_RE.finditer(raw_trace_text)]
    if raw_lost_records is not None and lost_values and len(lost_values) != raw_lost_records:
        raise PerfParseError("raw trace lost-record count does not match lost-value count")
    throttle_count = len(_THROTTLE_RE.findall(raw_trace_text)) if raw_trace_text else 0
    unthrottle_count = len(_UNTHROTTLE_RE.findall(raw_trace_text)) if raw_trace_text else 0
    throttle_records = throttle_count if throttle_count else None
    unthrottle_records = unthrottle_count if unthrottle_count else None

    return {
        "perf_version": version_match.group(1) if version_match else None,
        "clock": {"name": clock_name, "id": clock_id},
        "event": event,
        # perf control mode may add a dummy control event. Count only actual
        # sampled events for the one-event contract, while preserving all rows.
        "events": sum(row["name"] != "dummy:u" for row in events),
        "event_count_all": len(events),
        "event_rows": events,
        "reported_sample_count": sample_count,
        "lost_samples": lost_samples,
        "lost_records": raw_lost_records,
        "lost_record_sample_count": sum(lost_values) if lost_values else (0 if raw_lost_records == 0 else None),
        "throttle_records": throttle_records,
        "unthrottle_records": unthrottle_records,
        "diagnostic_sources": {
            "report_present": bool(report_text),
            "record_text_supplied": bool(raw_trace_text),
            "record_diagnostics_observed": raw_lost_records is not None
                                           or throttle_records is not None
                                           or unthrottle_records is not None,
            # Callers may pass perf script output rather than a complete
            # PERF_RECORD dump. Missing diagnostic lines therefore remain
            # unavailable, never evidence of zero loss/throttling.
            "record_dump_completeness": "unknown",
        },
    }


def _is_unknown_symbol(symbol: str | None) -> bool:
    if symbol is None:
        return True
    value = symbol.strip()
    return (
        not value
        or value.lower() in {"[unknown]", "unknown", "??", "?"}
        or _IP_RE.fullmatch(value) is not None
    )


def _parse_frame(line: str) -> dict[str, object]:
    stripped = line.strip()
    match = re.match(r"^(?P<ip>(?:0[xX])?[0-9a-fA-F]+)\s+(?P<rest>.+)$", stripped)
    if match is None:
        raise _FrameParseError(f"not a perf callchain frame: {line!r}")
    ip, rest = match.group("ip"), match.group("rest").strip()
    inline = rest.endswith(" (inlined)")
    if inline:
        rest = rest[: -len(" (inlined)")].rstrip()
    dso = None
    if rest.endswith(")") and " (" in rest:
        symbol_part, candidate = rest.rsplit(" (", 1)
        candidate = candidate[:-1]
        if candidate.startswith("/") or candidate.startswith("[") or ".so" in candidate or candidate.endswith(".exe"):
            rest, dso = symbol_part.rstrip(), candidate
    symbol = rest or None
    return {"ip": ip, "symbol": symbol, "dso": dso, "inlined": inline,
            "unknown": _is_unknown_symbol(symbol)}


def parse_perf_script(text: str) -> dict[str, object]:
    """Parse `perf script --ns` rows while retaining all malformed/unknown data.

    The expected command prints fields in this order: pid,tid,time,event,ip,sym,dso,
    callindent. Callchain frames are attached to their preceding sample header.
    """
    if not isinstance(text, str):
        raise TypeError("perf script output must be text")
    samples: list[dict[str, object]] = []
    malformed: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    ignored_lines = 0
    current: dict[str, object] | None = None

    def finish() -> None:
        if current is None:
            return
        frames = current["frames"]
        current["unknown_frame_count"] = sum(bool(frame["unknown"]) for frame in frames)
        current["unknown_stack"] = not frames or bool(current["unknown_frame_count"])
        current["leaf_symbol"] = frames[0]["symbol"] if frames else None
        if not frames:
            current["stack_status"] = "missing"
        elif current["unknown_frame_count"]:
            current["stack_status"] = "unresolved"
        else:
            current["stack_status"] = "resolved"
        # No stable truncation sentinel is emitted by perf script for every
        # incomplete DWARF unwind; this is deliberately not inferred from depth.
        current["truncation_status"] = "unobservable_from_perf_script_text"
        samples.append(current)

    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            ignored_lines += 1
            continue
        match = _SAMPLE_RE.match(line)
        if match:
            finish()
            try:
                pid = _integer(match.group("pid"), "pid", minimum=1)
                tid = _integer(match.group("tid"), "tid", minimum=1)
                timestamp_ns = decimal_seconds_to_ns(match.group("timestamp"))
                event = match.group("event")
            except (PerfParseError, TypeError) as error:
                malformed.append({"line": line_number, "text": line, "error": str(error)})
                current = None
                continue
            current = {"pid": pid, "tid": tid, "time_ns": timestamp_ns, "event": event,
                       "frames": [], "raw_header": line}
            details = match.group("details").strip()
            if details:
                try:
                    current["frames"].append(_parse_frame(details))
                except _FrameParseError as error:
                    malformed.append({"line": line_number, "text": line, "error": str(error)})
            continue

        stripped = line.strip()
        if "PERF_RECORD_LOST" in stripped or "PERF_RECORD_THROTTLE" in stripped or "PERF_RECORD_UNTHROTTLE" in stripped:
            diagnostics.append({"line": line_number, "text": line})
            continue
        if current is not None:
            try:
                current["frames"].append(_parse_frame(line))
                continue
            except _FrameParseError:
                pass
        malformed.append({"line": line_number, "text": line, "error": "unrecognized perf script record"})

    finish()
    unknown_frames = sum(int(sample["unknown_frame_count"]) for sample in samples)
    missing_stack_samples = sum(sample["stack_status"] == "missing" for sample in samples)
    unresolved_stack_samples = sum(sample["stack_status"] == "unresolved" for sample in samples)
    resolved_stack_samples = sum(sample["stack_status"] == "resolved" for sample in samples)
    unknown_samples = missing_stack_samples + unresolved_stack_samples
    diagnostic_counts = Counter()
    for row in diagnostics:
        value = row["text"]
        if "PERF_RECORD_LOST" in value:
            diagnostic_counts["lost"] += 1
        elif "PERF_RECORD_THROTTLE" in value:
            diagnostic_counts["throttle"] += 1
        elif "PERF_RECORD_UNTHROTTLE" in value:
            diagnostic_counts["unthrottle"] += 1
    diagnostic_lost_values = [
        int(match.group(1)) for row in diagnostics
        if "PERF_RECORD_LOST" in row["text"]
        for match in _LOST_VALUE_RE.finditer(row["text"])
    ]
    return {
        "samples": samples,
        "sample_count": len(samples),
        "malformed_lines": malformed,
        "attribution_valid": not bool(malformed),
        "diagnostic_lines": diagnostics,
        "lost_record_count": diagnostic_counts["lost"] or None,
        "lost_record_sample_count": sum(diagnostic_lost_values) if diagnostic_lost_values else None,
        "throttle_record_count": diagnostic_counts["throttle"] or None,
        "unthrottle_record_count": diagnostic_counts["unthrottle"] or None,
        "ignored_lines": ignored_lines,
        "unknown_frame_count": unknown_frames,
        "unknown_stack_sample_count": unknown_samples,
        "missing_stack_sample_count": missing_stack_samples,
        "unresolved_stack_sample_count": unresolved_stack_samples,
        "resolved_stack_sample_count": resolved_stack_samples,
        # perf script does not provide a reliable marker for every truncated
        # DWARF callchain. Preserve that uncertainty instead of treating the
        # absence of a marker as proof that no stack was truncated.
        "truncated_stack_sample_count": None,
        "truncation_status": "unobservable_from_perf_script_text",
        # Compatibility names for the current runner. None means unknown, not
        # zero truncation. Consumers must not subtract it as an observed count.
        "truncated_sample_count": None,
        "truncation_observable": False,
    }


def filter_samples(
    parsed: dict[str, object], pid: int, tids: Iterable[int], start_ns: int, end_ns: int,
    expected_event: str = "cpu-clock:u",
) -> dict[str, object]:
    """Select client samples in the half-open monotonic interval ``[start,end)``.

    Every unselected sample is retained with exactly one exclusion reason. PID
    is checked before TID so samples from other processes cannot be attributed
    to the client merely because a numeric TID happens to match.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ValueError("pid must be a positive integer")
    if isinstance(start_ns, bool) or not isinstance(start_ns, int) or start_ns < 0:
        raise ValueError("start_ns must be a nonnegative integer")
    if isinstance(end_ns, bool) or not isinstance(end_ns, int) or end_ns <= start_ns:
        raise ValueError("end_ns must be an integer greater than start_ns")
    if not isinstance(expected_event, str) or not expected_event:
        raise ValueError("expected_event must be nonempty text")
    tid_set = set(tids)
    if not tid_set or any(isinstance(tid, bool) or not isinstance(tid, int) or tid < 1 for tid in tid_set):
        raise ValueError("tids must contain positive integers")
    rows = parsed.get("samples")
    if not isinstance(rows, list):
        raise ValueError("parsed perf script must contain a samples list")

    excluded: list[dict[str, object]] = []
    counts: Counter[str] = Counter({
        "wrong_pid": 0,
        "wrong_tid": 0,
        "wrong_event": 0,
        "before_window": 0,
        "at_or_after_end": 0,
    })
    selected = []
    in_window_identity_mismatches = 0
    for sample in rows:
        in_window = start_ns <= sample["time_ns"] < end_ns
        if in_window and (sample["pid"] != pid or sample["tid"] not in tid_set):
            in_window_identity_mismatches += 1
        if sample["pid"] != pid:
            reason = "wrong_pid"
        elif sample["tid"] not in tid_set:
            reason = "wrong_tid"
        elif sample["time_ns"] < start_ns:
            reason = "before_window"
        elif sample["time_ns"] >= end_ns:
            reason = "at_or_after_end"
        elif sample["event"] != expected_event:
            reason = "wrong_event"
        else:
            selected.append(sample)
            continue
        counts[reason] += 1
        excluded.append({"reason": reason, "sample": sample})
    return {
        "selected": selected,
        "selected_sample_count": len(selected),
        "excluded": excluded,
        "excluded_counts": dict(counts),
        "input_sample_count": len(rows),
        "malformed_line_count": len(parsed.get("malformed_lines", [])),
        "attribution_valid": (
            not bool(parsed.get("malformed_lines", []))
            and counts["wrong_event"] == 0
            and in_window_identity_mismatches == 0
        ),
        "wrong_event_sample_count": counts["wrong_event"],
        "in_window_identity_mismatch_count": in_window_identity_mismatches,
        "selected_missing_stack_sample_count": sum(row["stack_status"] == "missing" for row in selected),
        "selected_unresolved_stack_sample_count": sum(row["stack_status"] == "unresolved" for row in selected),
        "selected_resolved_stack_sample_count": sum(row["stack_status"] == "resolved" for row in selected),
        "selected_unknown_stack_sample_count": sum(bool(row["unknown_stack"]) for row in selected),
        "selected_truncated_stack_sample_count": None,
        "selected_truncation_status": "unobservable_from_perf_script_text",
    }
