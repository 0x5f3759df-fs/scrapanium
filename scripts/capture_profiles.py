#!/usr/bin/env python3
"""Record local wire regression fixtures after explicit backend/profile updates."""
import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from binding import Session, lib
from fingerprint import capture, normalized

PROFILES = ["chrome150", "chrome146", "chrome131_android", "safari2601", "safari260_ios", "firefox147", "tor145"]
profiles = {}
for profile in PROFILES:
    print("Capturing", profile, flush=True)
    def send(url):
        with Session(profile=profile, timeout_ms=5000) as s, s.send(url) as r:
            assert r.error == 4
    hello = capture(send)
    profiles[profile] = {"normalized_clienthello": normalized(hello), "sample_extension_order": hello["extension_order"],
        "evidence": "loopback capture of pinned backend; not a real-browser capture", "real_browser_verified": False}
report = {"captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "backend": lib.sp_backend_version().decode(), "dependencies": json.loads((ROOT / "dependencies.json").read_text()),
    "normalization": ["GREASE values (counts retained)", "ephemeral key bytes and random", "Chrome extension permutation",
        "conditional padding extension 21", "SNI value", "ECH payload bytes"], "profiles": profiles}
out = ROOT / "profiles/captured.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(report, indent=2) + "\n")
print(out)
