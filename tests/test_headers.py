import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build import build


@pytest.fixture(scope="module")
def headers():
    binary = build(ROOT / "tests/headers_probe.bend", ROOT / "build/headers-probe", True)
    def run(url, ca="", proxy=""):
        out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ,
            "SCRAPANIUM_TEST_URL": url, "SCRAPANIUM_TEST_CA": ca,
            "SCRAPANIUM_TEST_PROXY": proxy}, capture_output=True, text=True, timeout=10)
        assert out.returncode == 0, out.stdout + out.stderr
        regular, trailers = out.stdout.split("--trailers--\n")
        return [line.split("=", 1) for line in regular.splitlines()], [line.split("=", 1) for line in trailers.splitlines()]
    return run


@pytest.mark.parametrize("path", ["/metadata", "/metadata-redirect"])
def test_final_headers_duplicates_order_and_ows(headers, http, path):
    values, trailers = headers(http + path)
    selected = [v for v in values if v[0].lower().startswith("x-") or v[0] == "Set-Cookie"]
    assert selected == [["X-Empty", ""], ["X-Value", "a: b"], ["Set-Cookie", "a=1"],
                        ["Set-Cookie", "b=2"], ["X-Case", "first"], ["x-case", "second"]]
    assert trailers == []


def test_informational_headers_excluded(headers, http):
    values, trailers = headers(http + "/early")
    assert ["X-Final", "complete"] in values
    assert all(k != "X-Old" for k, _ in values)
    assert trailers == []


def test_h1_trailers_separate(headers, http):
    values, trailers = headers(http + "/trailers")
    assert ["X-Phase", "header"] in values
    assert ["X-Phase", "trailer"] not in values
    assert trailers == [["X-Phase", "trailer"], ["X-Tail", "yes"]]


def test_h2_trailers_separate(headers, h2server):
    _, url, ca = h2server
    values, trailers = headers(url + "/trailers", ca)
    assert values == [["content-length", "2"]]
    assert trailers == [["x-tail", "h2-trailer"]]


def test_unfolded_values(headers, http):
    values, _ = headers(http + "/folded")
    assert ["X-Folded", "one two"] in values


def test_connect_headers_excluded(headers, https, proxy):
    url, ca = https
    _, proxy_url = proxy
    values, trailers = headers(url + "/metadata", ca, proxy_url)
    assert ["X-Value", "a: b"] in values and not trailers
