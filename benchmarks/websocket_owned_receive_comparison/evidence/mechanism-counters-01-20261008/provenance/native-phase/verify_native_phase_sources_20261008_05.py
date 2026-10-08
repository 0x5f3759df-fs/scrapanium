from __future__ import annotations
import hashlib, json, re, subprocess
from pathlib import Path

ART = Path("/home/baidu/scrapanium-experiments/wss-owned-mechanism-20261008/artifacts/owned-mechanism-02")
ROOT = ART.parent.parent
COMPILER = Path("/home/baidu/scrapanium-experiments/bend2-027-compat/compiler-2.0.27")
GENERATED = ART / "phase-hook-probe.generated.c"
OUT = ART / "native-phase-verification-20261008-05.json"
EXPECTED_CID = "CID_WSBENCHDIAG_CHECKED_MARK"

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def ordered(text: str, tokens: list[str]) -> bool:
    offsets = [text.find(token) for token in tokens]
    return all(offset >= 0 for offset in offsets) and offsets == sorted(offsets)

def counter_layout(variant: str):
    repo = ROOT / variant
    header = (repo / "native/scrapanium.h").read_text()
    source = (repo / "native/websocket.inc.c").read_text()
    start = header.index("enum sp_wss_diag_index {")
    end = header.index("SP_WSS_DIAG_COUNTER_COUNT", start)
    ids = [v.strip() for v in header[start:end].split("{", 1)[1].replace("}", "").split(",") if v.strip()]
    names_start = source.index("static const char *const names[SP_WSS_DIAG_COUNTER_COUNT] = {")
    names_end = source.index("\n  };", names_start)
    names = re.findall(r'"([^"]+)"', source[names_start:names_end])
    if len(ids) != len(names):
        raise AssertionError((variant, len(ids), len(names)))
    if any(x in header + source for x in ("SP_WSS_DIAG_SOCKET_POLL_", "SP_WSS_DIAG_RAW_RECV", "raw_recv")):
        raise AssertionError((variant, "forbidden poll/raw counter label"))
    return ids, names

if OUT.exists():
    raise SystemExit(f"refusing overwrite: {OUT}")
if not GENERATED.is_file():
    raise SystemExit(f"missing generated-C probe: {GENERATED}")

files, clients = {}, {}
for variant in ("baseline", "owned"):
    repo = ROOT / variant
    for original, diagnostic in (
        ("benchmarks/websocket_stream.bend", "benchmarks/websocket_mechanism_stream.bend"),
        ("benchmarks/websocket.bend", "benchmarks/websocket_mechanism_roundtrip.bend"),
    ):
        op, dp = repo / original, repo / diagnostic
        ot, dt = op.read_text(), dp.read_text()
        declaration = (
            'def WsBenchDiag.mark(phase: String) -> IO(Unit):\n'
            '  IO.try(Unit, WsBenchDiag.checked_mark(phase))\n\n'
            'def WsBenchDiag.checked_mark(phase: String) -> IO(Result<&1, &1, U32 & String, Unit>):\n'
            '  import "./websocket_mechanism_diag.c"\n'
        )
        if dt.count(declaration) != 1:
            raise AssertionError((variant, diagnostic, "foreign checked_mark declaration/wrapper mismatch"))
        if "WsBenchDiag.checked_mark" not in dt or "IO.try(Unit, WsBenchDiag.checked_mark(phase))" not in dt:
            raise AssertionError((variant, diagnostic, "Result failure propagation absent"))
        calls = [line for line in dt.splitlines() if line.lstrip().startswith('WsBenchDiag.mark("')]
        phases = [line.split('"')[1] for line in calls]
        if sorted(phases) != sorted(["cold", "gate", "work_end", "released"]):
            raise AssertionError((variant, diagnostic, phases))
        stripped = dt.replace(declaration + "\n", "")
        stripped = "".join(line for line in stripped.splitlines(keepends=True)
                           if not line.lstrip().startswith("WsBenchDiag.mark("))
        if stripped != ot:
            raise AssertionError((variant, diagnostic, "checker differs after exact diagnostic marker removal"))
        if diagnostic.endswith("_stream.bend"):
            timed = dt.split("def timed(", 1)[1].split("\ndef warmed", 1)[0]
            main = dt.split("def main(", 1)[1]
            flow_ok = (
                ordered(main, ["S.Ws.connect(", 'WsBenchDiag.mark("cold")', "warmed(expected, socket)"]) and
                ordered(timed, ["B.gate()", 'WsBenchDiag.mark("gate")', "start : Nat <-",
                                "S.Ws.send(", "receive_all(expected, socket)", "end : Nat <-",
                                'WsBenchDiag.mark("work_end")', "S.Ws.discard(socket)",
                                'WsBenchDiag.mark("released")'])
            )
        else:
            timed = dt.split("def timed(", 1)[1].split("\ndef main(", 1)[0]
            main = dt.split("def main(", 1)[1]
            flow_ok = (
                ordered(main, ["S.Ws.connect(", 'WsBenchDiag.mark("cold")', "timed("]) and
                ordered(timed, ["repeat(1n", "gate()", 'WsBenchDiag.mark("gate")', "start : Nat <-",
                                "repeat(n", "end : Nat <-", 'WsBenchDiag.mark("work_end")',
                                "release(state)", 'WsBenchDiag.mark("released")'])
            )
        if not flow_ok:
            raise AssertionError((variant, diagnostic, "phase execution flow"))
        clients[f"{variant}:{diagnostic}"] = {
            "original": str(op), "original_sha256": sha(op),
            "diagnostic": str(dp), "diagnostic_sha256": sha(dp),
            "marker_phases_in_text_order": phases,
            "execution_flow_order_checked": True,
            "source_matches_after_exact_marker_removal": True,
        }

    helper = repo / "benchmarks/websocket_mechanism_diag.c"
    ht = helper.read_text()
    for required in (
        "extern const char *curl_ws_diag_counter_name(size_t);",
        "curl_ws_diag_counter_name((size_t)i)",
        "static const char *native_name(unsigned i)",
        "return io_fail(e, SP_BACKEND, message);",
        "return io_done(e, term_pak(CID_UNIT, 0));",
        f"io_eff({EXPECTED_CID}, ws_bench_diag_mark, 0);",
        "free(phase);",
    ):
        if required not in ht:
            raise AssertionError((variant, "helper contract absent", required))
    if "io_eff(CID_WSBENCHDIAG_MARK" in ht:
        raise AssertionError((variant, "wrapper CID still registered"))
    for rel in ("native/scrapanium.h", "native/websocket.inc.c", "native/bend_bridge.c",
                "benchmarks/websocket_mechanism_diag.c"):
        p = repo / rel
        files[f"{variant}:{rel}"] = {"path": str(p), "sha256": sha(p)}

base_ids, base_names = counter_layout("baseline")
owned_ids, owned_names = counter_layout("owned")
if base_ids != owned_ids or base_names != owned_names or len(base_ids) != 58:
    raise AssertionError("native counter layout differs or is not 58 entries")
for name in ("framed_recv_success_calls", "framed_recv_again_calls", "framed_recv_error_calls",
             "owned_recv_success_calls", "owned_recv_again_calls", "owned_recv_error_calls"):
    if name not in base_names:
        raise AssertionError(("missing API outcome counter", name))

gen = GENERATED.read_text()
if not re.search(r"^#define " + EXPECTED_CID + r" \d+$", gen, re.M):
    raise AssertionError("generated C does not define imported checked_mark constructor")
if f"term_ctr({EXPECTED_CID}," not in gen:
    raise AssertionError("generated C does not construct imported checked_mark")
if f"io_eff({EXPECTED_CID}, ws_bench_diag_mark, 0);" not in gen:
    raise AssertionError("generated C's registered effect CID does not match imported checked_mark")
if "CID_WSBENCHDIAG_MARK" in gen:
    raise AssertionError("generated C still contains wrapper mark CID")
comp_ts = COMPILER / "bend2/comp.ts"
if "return \"CID_\" + name_clean(k).toUpperCase();" not in comp_ts.read_text():
    raise AssertionError("pinned compiler CID normalization rule changed")

result = {
    "schema": 1,
    "status": "pass",
    "purpose": "read-only source/layout verification plus one Bend C generation; no C compile/link, peer, workload, or timing",
    "worktrees": {v: {"path": str(ROOT / v), "head": "7b52d7f1cbbfe4c21b1abf1ad71e8e3061c47c17"} for v in ("baseline", "owned")},
    "native_counter_count": len(base_ids),
    "native_counter_names": base_names,
    "counter_order_matches_between_arms": True,
    "client_marker_removal": clients,
    "native_and_phase_sources": files,
    "foreign_effect_identity": {
        "bend_foreign_definition": "WsBenchDiag.checked_mark",
        "bend_result_type": "IO(Result<&1, &1, U32 & String, Unit>)",
        "compiler_cid_normalization": "CID_ + uppercase(qualified name with punctuation normalized to underscores)",
        "expected_cid": EXPECTED_CID,
        "generated_c_path": str(GENERATED),
        "generated_c_sha256": sha(GENERATED),
        "generated_definition_cid_macro_and_call_present": True,
        "generated_registration_matches_imported_definition": True,
        "compiler_source": str(comp_ts),
        "compiler_source_sha256": sha(comp_ts),
        "compiler_commit": subprocess.check_output(["git", "-C", str(COMPILER), "rev-parse", "HEAD"], text=True).strip(),
    },
    "phase_contract": {
        "marker_phases_in_text_order_is_not_runtime_order": True,
        "execution_flow": "cold after connect/handshake and before warmup; gate after warmup and external gate; work_end after full original receive/equality workload and end timestamp; released after original resource release",
        "snapshot_model": "cumulative atomic fields; synchronous markers on one Bend IO loop reach quiescent boundaries after earlier effects; no reset and no simultaneous multi-field atomic snapshot claim",
        "file_contract": "one O_EXCL JSONL per client; fsync only at four phase markers",
    },
    "metric_scope": {
        "framed_recv": "clean baseline uses curl_ws_recv; owned counts fallback calls to the same framed API, not a raw parser",
        "receive_api_outcomes": "framed and owned success/AGAIN/error classes are counted at the public API return sites; lower Curl reader_AGAIN remains separate",
        "wait": "Bend-loop step and readiness-query counts only; no blocking poll-result or kernel wait-duration counters",
        "copies": "only explicit source-site memcpy/memcmp bytes; no allocator, mmap, implicit realloc movement, SSL-internal-copy, or process-wide copy census",
    },
}
OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(OUT)
