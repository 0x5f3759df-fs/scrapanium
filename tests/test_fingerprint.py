import json
from pathlib import Path
import pytest
from curl_cffi import requests
from binding import Session
from fingerprint import capture, normalized

PROFILES = ["chrome150", "chrome146", "chrome131_android", "safari2601", "safari260_ios", "firefox147", "tor145"]

@pytest.mark.parametrize("profile", PROFILES)
def test_clienthello_reference_parity(profile):
    def native(url):
        with Session(profile=profile, timeout_ms=5000) as s, s.send(url) as r:
            assert r.error == 4  # capture intentionally closes before ServerHello
    def reference(url):
        try: requests.get(url, impersonate=profile, timeout=5)
        except requests.RequestsError: pass
    got, expected = capture(native), capture(reference)
    assert normalized(got) == normalized(expected)
    if profile.startswith(("safari", "firefox", "tor")):
        assert got["extension_order"] == expected["extension_order"]

@pytest.mark.parametrize("profile", ["chrome150", "safari2601", "firefox147"])
def test_http2_settings_and_header_order_reference_parity(h2server, profile):
    server, url, ca = h2server
    settings_at, headers_at, windows_at = len(server.settings), len(server.headers), len(server.windows)
    with Session(profile=profile, ca_bundle=ca) as s, s.send(url) as r:
        assert r.error == 0
    native = (server.settings[settings_at:], server.headers[headers_at:], server.windows[windows_at:])
    settings_at, headers_at, windows_at = len(server.settings), len(server.headers), len(server.windows)
    with requests.Session(impersonate=profile, verify=ca, trust_env=False) as s:
        assert s.get(url).content == b"h2"
    reference = (server.settings[settings_at:], server.headers[headers_at:], server.windows[windows_at:])
    assert native == reference

@pytest.mark.parametrize("profile", PROFILES)
def test_versioned_clienthello_regression(profile):
    fixture = Path(__file__).resolve().parents[1] / "profiles/captured.json"
    expected = json.loads(fixture.read_text())["profiles"][profile]["normalized_clienthello"]
    def send(url):
        with Session(profile=profile, timeout_ms=5000) as s, s.send(url) as r:
            assert r.error == 4
    assert normalized(capture(send)) == expected

def test_custom_tls_overrides_change_wire():
    def customized(url):
        with Session(profile="chrome146", fp_curves="P-256:P-384",
                     fp_grease=0, fp_permute_extensions=0) as s, s.send(url) as r:
            assert r.error == 4
    first, second = capture(customized), capture(customized)
    assert first["details"]["10"] == [23, 24]
    assert first["grease_ciphers"] == first["grease_extensions"] == 0
    assert [x for x in first["extension_order"] if x != 21] == [x for x in second["extension_order"] if x != 21]

def test_custom_http2_overrides_change_wire(h2server):
    server, url, ca = h2server
    at = len(server.settings)
    with Session(ca_bundle=ca, fp_http2_settings="1:65536;2:0;4:6291456;6:262144",
                 fp_pseudo_header_order="masp", fp_http2_window_update=15663105) as s, s.send(url) as r:
        assert r.error == 0, r.message
    assert server.settings[at] == [(1, 65536), (2, 0), (4, 6291456), (6, 262144)]
    assert [k for k, v in server.headers[-1] if k.startswith(b":")] == [b":method", b":authority", b":scheme", b":path"]
    assert (0, 15663105) in server.windows
