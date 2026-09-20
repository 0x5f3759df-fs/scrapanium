import json
import os
from pathlib import Path
import random
import subprocess
import sys
from urllib.parse import quote, parse_qsl, urlsplit
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build import build
from binding import Session


@pytest.fixture(scope="module")
def probe():
    binary = build(ROOT / "tests/http_probe.bend", ROOT / "build/http-probe", True)
    def run(value="", base="/test", name="X-Test", code=0):
        out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ,
            "PROBE_VALUE": value, "PROBE_BASE": base, "PROBE_NAME": name, "PROBE_CODE": str(code)},
            capture_output=True, text=True, timeout=10)
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout.splitlines()
    return run


def form_reference(value):
    # WHATWG form encoding differs from urllib's always-safe '~'.
    return quote(value, safe="*-._").replace("~", "%7E").replace("%20", "+")


@pytest.mark.parametrize("value", ["", "hello", "a b+c&d=e%f?#/", "~*-.!_'()", "héllo 🌍 中文", "\r\n\t", "".join(map(chr, range(1, 128)))])
def test_component_form_and_repeated_query(probe, value):
    lines = probe(value=value)
    assert lines[0] == quote(value, safe="~-._")
    assert lines[1] == form_reference(value)
    assert parse_qsl(urlsplit(lines[2]).query, keep_blank_values=True) == [("key", value), ("key", ""), ("", "v")]
    assert lines[4:6] == ["one|two", "new"]
    assert lines[7] == "/unchanged?#tail#two"


@pytest.mark.parametrize("base,expected", [("/a", "/a?"), ("/a?", "/a?"), ("/a?x=1", "/a?x=1&"), ("/a?x=1&", "/a?x=1&"), ("/a#f?x", "/a?"), ("/a?x=1#f#more", "/a?x=1&")])
def test_query_is_inserted_before_fragment(probe, base, expected):
    result = probe(base=base)[2]
    fragment = "#" + base.split("#", 1)[1] if "#" in base else ""
    assert result == expected + "key=&key=&=v" + fragment


@pytest.mark.parametrize("code", [0, 127, 128, 2047, 2048, 55295, 55296, 57343, 57344, 65535, 65536, 1114111, 1114112, 4294967295])
def test_unicode_scalar_boundaries_and_invalid_replacement(probe, code):
    scalar = "�" if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF else chr(code)
    assert probe(code=code)[6] == quote(scalar, safe="~-._")


def test_deterministic_random_unicode_differential(probe):
    rng = random.Random(83437)
    for _ in range(20):
        value = "".join(chr(rng.choice([rng.randrange(1, 128), rng.randrange(128, 0xD800), rng.randrange(0xE000, 0x110000)])) for _ in range(30))
        result = probe(value=value)
        assert result[:2] == [quote(value, safe="~-._"), form_reference(value)]


@pytest.mark.parametrize("name,value,valid", [("", "x", False), ("X-A", "", True), ("x_a!", "tab\tvalue", True),
    ("Bad Name", "x", False), ("Bad:Name", "x", False), ("X-É", "x", False), ("X", "bad\r\n", False),
    ("X", "bad\x7f", False), ("X", "bad\x01", False)])
def test_header_validation(probe, name, value, valid):
    assert probe(name=name, value=value)[3] == str(valid)


def test_compiled_request_builders_on_wire(http):
    binary = build(ROOT / "tests/builders.bend", ROOT / "build/builders-test", True)
    out = subprocess.run([str(binary), "--threads", "1"], env={**os.environ, "SCRAPANIUM_TEST_URL": http},
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    query, form, binary = map(json.loads, lines[:3])
    assert parse_qsl(urlsplit(query["path"]).query, keep_blank_values=True) == [("old", "1"), ("q", "a &🌍"), ("q", "")]
    assert "#" not in query["path"]
    assert parse_qsl(bytes(form["body"]).decode(), keep_blank_values=True) == [("q", "a +~*"), ("q", "héllo")]
    assert bytes(binary["body"]) == b"\x00\xff\xc0\x80A\x00"
    assert [v for k, v in binary["headers"] if k.lower() == "x-dup"] == ["one", "two"]
    assert [v for k, v in binary["headers"] if k.lower() == "x-empty"] == [""]
    assert lines[3:] == ["error:1", "error:1"]


@pytest.mark.parametrize("header", [";", "Bad name;", "Bad\r\n;", "Name;tail", "Name;;"])
def test_invalid_empty_header_syntax_rejected(http, header):
    with Session() as s, s.send(http, headers=[header]) as r: assert r.error == 1


def test_raw_empty_header_sends_a_field(http):
    with Session() as s, s.send(http + "/echo", headers=["X-Empty;", "User-Agent:"]) as r:
        assert r.error == 0
        fields = json.loads(r.body)["headers"]
        assert ("X-Empty", "") in map(tuple, fields)
        assert not any(k.lower() == "user-agent" for k, _ in fields)
