import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import pytest
from binding import Session, declare, lib
from fingerprint import capture, normalized

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "profiles/catalog.json").read_text())
declare("sp_profile_count", C.c_size_t)
declare("sp_profile_name", C.c_char_p, C.c_size_t)


def test_profile_catalog_matches_runtime():
    assert [lib.sp_profile_name(i).decode() for i in range(lib.sp_profile_count())] == [p["id"] for p in CATALOG["profiles"]]
    assert lib.sp_profile_name(lib.sp_profile_count()) is None


@pytest.mark.parametrize("profile", [p["id"] for p in CATALOG["profiles"]])
def test_every_profile_connects_with_verification(profile, https):
    url, ca = https
    with Session(profile=profile, ca_bundle=ca, concurrency=1) as s, s.send(url + "/echo") as r:
        assert r.error == 0, r.message
        headers = dict((k.lower(), v) for k, v in json.loads(r.body)["headers"])
        if profile == "firefox148": assert "Firefox/148.0" in headers["user-agent"]
        if profile == "chrome152_preview": assert 'v="152"' in headers["sec-ch-ua"]


@pytest.fixture(scope="module")
def go_probe():
    subprocess.run(["go", "build", "-buildvcs=false", "-o", str(ROOT / "build/profile-go"), "."], cwd=ROOT / "benchmarks/tls-client", check=True)
    return ROOT / "build/profile-go"


@pytest.mark.parametrize("profile,reference", [("firefox148", "firefox_148"), ("chrome152_preview", "chrome_152")])
def test_new_profile_wire_against_independent_go(go_probe, profile, reference):
    def native(url):
        with Session(profile=profile, concurrency=1) as s, s.send(url) as r: assert r.error == 4
    def go(url):
        subprocess.run([str(go_probe), url, "sequential", "1", ""], capture_output=True, timeout=12,
                       env={**os.environ, "SCRAPANIUM_PROFILE": reference})
    got, expected = normalized(capture(native)), normalized(capture(go))
    if profile == "chrome152_preview":
        # Explicit known gap, never normalize it away as browser parity.
        assert expected.pop("grease_signature_algorithms") == 1
        assert got.get("grease_signature_algorithms", 0) == 0
        assert len(got["details"]["51764"]) == 28
    assert got == expected


def test_unknown_current_browser_names_do_not_fall_back():
    for profile in ("chrome152", "chrome999", "safari999"):
        with pytest.raises(ValueError): Session(profile=profile)
