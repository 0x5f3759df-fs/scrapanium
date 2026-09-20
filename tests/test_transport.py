import json
import time
import pytest
from binding import Session, request, lib

def echo(response):
    assert response.error == 0, response.message
    assert response.status == 200
    return json.loads(response.body)

@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("body", [None, b"", b"hello", bytes(range(256))])
def test_methods_binary_upload(http, method, body):
    with Session() as session, session.send(http + "/echo", method=method, body=body) as response:
        result = echo(response)
        assert result["method"] == method
        assert result["body"] == list(body or b"")

def test_keepalive_headers_isolation(http):
    with Session() as session:
        with session.send(http + "/echo", headers=["X-Test: first"]) as r:
            first = echo(r)
            assert r.new_connections == 1
        with session.send(http + "/echo") as r:
            second = echo(r)
            assert r.new_connections == 0
        assert first["port"] == second["port"]
        assert ["X-Test", "first"] in first["headers"]
        assert not any(k.lower() == "x-test" for k, v in second["headers"])

def test_cookies_survive_reuse_and_batch(http):
    with Session() as session:
        with session.send(http + "/setcookie") as r: assert r.error == 0
        results = session.batch([request(http + "/echo") for _ in range(32)])
        try:
            for r in results:
                assert any(k.lower() == "cookie" and "session=scrapanium" in v for k, v in echo(r)["headers"])
        finally:
            for r in results: r.close()

@pytest.mark.parametrize("path,body", [("/binary", b"\x00\xff\xc0\x80hello\x00"),
    ("/unicode", "Bend: héllo 🌍".encode()), ("/gzip", b"decompressed" * 100), ("/chunked", b"abcde")])
def test_download(http, path, body):
    with Session() as session, session.send(http + path) as r:
        assert r.error == 0, r.message
        assert r.body == body
        assert r.size == len(body)

@pytest.mark.parametrize("code", [200, 204, 301, 400, 403, 404, 429, 500, 503])
def test_http_status_is_response(http, code):
    with Session() as session, session.send(http + f"/status/{code}") as r:
        assert r.error == 0
        assert r.status == code

def test_head(http):
    with Session() as session, session.send(http + "/echo", method="HEAD") as r:
        assert r.status == 200 and not r.body
    with Session() as session, session.send(http + "/echo", method="HEAD", body=b"no") as r:
        assert r.error == 1
    with Session() as session, session.send(http + "/echo", method="HEAD", body=b"") as r:
        assert r.error == 0 and r.status == 200 and r.body == b""

def test_uppercase_scheme(http):
    with Session() as s, s.send(http.replace("http://", "HTTP://")) as r:
        assert r.error == 0 and r.status == 200

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_303_uses_get(http, method):
    with Session() as s, s.send(http + "/redirect303", method=method, body=b"payload") as r:
        out = echo(r)
        assert out["method"] == "GET" and not out["body"]

@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/redirect", "/redirect307"])
def test_non_post_redirect_preserves_method_and_body(http, method, path):
    with Session() as s, s.send(http + path, method=method, body=b"payload") as r:
        out = echo(r)
        assert out["method"] == method and out["body"] == list(b"payload")

@pytest.mark.parametrize("options", [{"mismatch": True}, {"expired": True}])
def test_trusted_cert_still_requires_hostname_and_valid_dates(tmp_path, options):
    from lab import certificate, start_http
    context, ca = certificate(tmp_path, **options)
    server, url = start_http(context)
    try:
        with Session(ca_bundle=ca) as s, s.send(url) as r:
            assert r.error == 4 and r.curl_code == 60
    finally:
        server.shutdown(); server.server_close()

@pytest.mark.parametrize("path,method", [("/redirect", "GET"), ("/redirect307", "POST")])
def test_redirect_method(http, path, method):
    with Session() as session, session.send(http + path, method="POST", body=b"payload") as r:
        out = echo(r)
        assert out["method"] == method
        assert out["body"] == (list(b"payload") if method == "POST" else [])
        assert r.url.endswith(b"/echo")

def test_redirect_options(http):
    with Session(follow_redirects=0) as s, s.send(http + "/redirect") as r:
        assert r.status == 302 and r.error == 0
    with Session(max_redirects=2) as s, s.send(http + "/loop") as r:
        assert r.error == 4 and r.curl_code == 47
    with Session() as s, s.send(http + "/badredirect") as r:
        assert r.error == 4

def test_cross_origin_authorization_stripped(http):
    with Session() as s, s.send(http + "/cross", headers=["Authorization: Bearer example"]) as r:
        assert not any(k.lower() == "authorization" for k, _ in echo(r)["headers"])

def test_duplicate_headers(http):
    with Session() as s, s.send(http + "/duplicate") as r:
        assert r.headers.count(b"Set-Cookie:") == 2

@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://localhost/a", "gopher://localhost", "no-scheme", "http://"])
def test_reject_non_http_or_invalid_url(url):
    with Session() as s, s.send(url) as r: assert r.error != 0

@pytest.mark.parametrize("header", ["X: good\r\nInjected: yes", "X: bad\x01", ":authority: x", "bad name: x", "no-colon", "X: bad\x7f"])
def test_header_injection_rejected_and_session_recovers(http, header):
    with Session() as s:
        with s.send(http, headers=[header]) as r: assert r.error == 1
        with s.send(http) as r: assert r.error == 0

@pytest.mark.parametrize("method", ["", "G ET", "GET\r\nX: injected"])
def test_invalid_method(http, method):
    with Session() as s, s.send(http, method=method) as r: assert r.error == 1

@pytest.mark.parametrize("options,path,error", [({"max_body_bytes": 20}, "/bytes/256", 5),
    ({"max_body_bytes": 1024}, "/bomb", 5), ({"max_header_bytes": 256}, "/headers", 6)])
def test_memory_limits_and_recovery(http, options, path, error):
    with Session(**options) as s:
        with s.send(http + path) as r: assert r.error == error
        with s.send(http) as r: assert r.error == 0 and r.body == b"ok"

def test_timeout_and_truncation(http):
    with Session(timeout_ms=50) as s:
        start = time.monotonic()
        with s.send(http + "/slow/250") as r: assert r.error == 4 and r.curl_code == 28
        assert time.monotonic() - start < 0.2
        with s.send(http + "/truncated") as r: assert r.error == 4 and r.curl_code == 18

@pytest.mark.parametrize("profile", ["chrome150", "chrome146", "chrome131_android", "safari2601", "safari260_ios", "firefox147", "tor145"])
def test_profile_https(https, profile):
    url, ca = https
    with Session(profile=profile, ca_bundle=ca) as s, s.send(url) as r:
        assert r.error == 0, r.message
        assert r.status == 200

def test_verification_enabled_by_default(https):
    url, ca = https
    with Session() as s, s.send(url) as r:
        assert r.error == 4 and r.curl_code == 60
    with Session(verify=0) as s, s.send(url) as r:
        assert r.error == 0

@pytest.mark.parametrize("kw", [{"profile":"invented9000"}, {"concurrency":0}, {"concurrency":257},
    {"timeout_ms":0}, {"connect_timeout_ms":0}, {"max_body_bytes":0}, {"max_header_bytes":0},
    {"fp_grease": 2}, {"fp_permute_extensions": -2}, {"fp_http2_window_update": 0xffffffff}])
def test_invalid_configuration(kw):
    with pytest.raises(ValueError): Session(**kw)

def test_batch_order_partial_failure_bounded_concurrency(http):
    with Session(concurrency=4) as s:
        start = time.monotonic()
        results = s.batch([request(http + "/slow/70") for _ in range(12)] + [request("file:///etc/passwd")])
        elapsed = time.monotonic() - start
        try:
            assert all(r.error == 0 for r in results[:-1])
            assert results[-1].error == 1
            assert 0.19 < elapsed < 0.7
        finally:
            for r in results: r.close()
        assert s.batch([]) == []

def test_response_outlives_session(http):
    with Session() as s: r = s.send(http)
    with r: assert r.body == b"ok"

def test_explicit_proxy_http_and_connect(http, https, proxy):
    server, proxy_url = proxy
    url, ca = https
    with Session(proxy=proxy_url, ca_bundle=ca) as s:
        with s.send(http) as r: assert r.error == 0
        with s.send(url) as r: assert r.error == 0, r.message
    assert any(x.startswith("GET http://") for x in server.requests)
    assert any(x.startswith("CONNECT ") for x in server.requests)

def test_ambient_proxy_ignored(http, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    with Session() as s, s.send(http) as r: assert r.error == 0

def test_http2_and_pooling(h2server):
    server, url, ca = h2server
    with Session(ca_bundle=ca, concurrency=8) as s:
        with s.send(url) as r:
            assert r.error == 0, r.message
            assert r.body == b"h2" and r.http_version == 3
        results = s.batch([request(url) for _ in range(32)])
        try:
            assert all(r.error == 0 and r.body == b"h2" and r.http_version == 3 for r in results)
            assert sum(r.new_connections for r in results) <= 7
        finally:
            for r in results: r.close()

def test_backend_is_impersonate():
    assert b"BoringSSL" in lib.sp_backend_version()
