"""Actual browser evidence, kept distinct from reference-library parity."""
import hashlib
import json
from pathlib import Path
import pytest
from binding import Session, lib
from browser_lab import browser_certificate, start_browser_h2
from fingerprint import capture, detailed_hello

ROOT = Path(__file__).resolve().parents[1]
REPORTS = {
    mode: json.loads((ROOT / f'profiles/browsers/linux-{mode}-2026-09-20.json').read_text())
    for mode in ('headed', 'headless')
}
TARGETS = [('chrome153', 'google_chrome', 'headed'),
           ('firefox156', 'firefox', 'headed'),
           ('firefox156', 'firefox', 'headless'),
           ('chrome153_headless', 'chrome', 'headless')]
BROWSER_BACKEND = b'2.2.3-scrapanium.1' in lib.sp_backend_version()


def h2_fingerprint(record):
    """Normalize only the ephemeral origin in the ordered header list.

    Include actual frame types/order, stream IDs, priority bits and settings;
    HPACK bytes are retained in the artifact, but compared as decoded headers.
    ACK timing depends on scheduling and is excluded explicitly.
    """
    from hyperframe.frame import Frame
    wire = bytes.fromhex(''.join(record['received_hex']))
    assert wire.startswith(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
    at, frames = 24, []
    while at < len(wire):
        frame, length = Frame.parse_frame_header(memoryview(wire[at:at + 9]))
        body = wire[at + 9:at + 9 + length]
        assert len(body) == length
        frame.parse_body(memoryview(body))
        at += 9 + length
        if 'ACK' in frame.flags:
            continue
        item = [frame.type, frame.stream_id, sorted(frame.flags)]
        if frame.type == 1:
            item += [frame.depends_on, frame.stream_weight, frame.exclusive]
        else:
            item.append(body.hex())
        frames.append(item)
    return {'alpn': record['alpn'], 'tls_version': record['tls_version'],
            'settings': [[list(pair) for pair in settings] for settings in record['settings']],
            'windows': [list(w) for w in record['windows']],
            'headers': [[[k, '<origin>' if k == ':authority' else v] for k, v in headers]
                        for headers in record['headers']], 'frames': frames}


@pytest.mark.parametrize('mode', ['headed', 'headless'])
def test_browser_artifacts_reparse_and_agree(mode):
    report = REPORTS[mode]
    assert report['browser_mode'] == mode
    for browser in report['browsers'].values():
        assert len(browser['samples']) == len(browser['http2_samples']) == 3
        for sample in browser['samples']:
            records = [bytes.fromhex(record) for record in sample['tls_records_hex']]
            for record in records:
                assert record[0] == 22 and len(record) == 5 + int.from_bytes(record[3:5], 'big')
            hello = b''.join(record[5:] for record in records)
            assert len(hello) == 4 + int.from_bytes(hello[1:4], 'big') == sample['clienthello_bytes']
            assert hashlib.sha256(hello).hexdigest() == sample['clienthello_sha256']
            assert detailed_hello(hello) == sample['detailed_clienthello']
            assert sample['detailed_clienthello'] == browser['samples'][0]['detailed_clienthello']
        expected = h2_fingerprint(browser['http2_samples'][0]['connection'])
        assert all(h2_fingerprint(sample['connection']) == expected for sample in browser['http2_samples'])


@pytest.mark.skipif(not BROWSER_BACKEND, reason='requires the optional source-built browser backend')
@pytest.mark.parametrize('profile,browser,mode', TARGETS)
def test_profile_clienthello_matches_real_browser(profile, browser, mode):
    def send(url):
        with Session(profile=profile, timeout_ms=5000) as session, session.send(url) as response:
            assert response.error == 4
    expected = REPORTS[mode]['browsers'][browser]['samples'][0]['detailed_clienthello']
    for _ in range(3):
        assert capture(send, detailed=True) == expected


@pytest.mark.skipif(not BROWSER_BACKEND, reason='requires the optional source-built browser backend')
@pytest.mark.parametrize('profile,browser,mode', TARGETS)
def test_profile_http2_matches_real_browser(profile, browser, mode, tmp_path):
    context, ca, _ = browser_certificate(tmp_path)
    server, url = start_browser_h2(context)
    try:
        with Session(profile=profile, ca_bundle=str(ca), concurrency=1) as session, session.send(url) as response:
            assert response.error == 0, response.message
            assert b'capture complete' in response.body
        assert server.received.wait(2), server.errors
        expected = REPORTS[mode]['browsers'][browser]['http2_samples'][0]['connection']
        assert h2_fingerprint(server.result) == h2_fingerprint(expected)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(not BROWSER_BACKEND, reason='requires the optional source-built browser backend')
@pytest.mark.parametrize('profile', ['chrome153', 'chrome153_headless', 'firefox156'])
def test_profile_verified_wss_preserves_binary_and_utf8(profile, tmp_path):
    from test_websocket import WebSocket
    from ws_lab import start_ws
    context, ca, _ = browser_certificate(tmp_path)
    server, url = start_ws(context)
    try:
        with WebSocket(url + '/echo', profile=profile, ca_bundle=str(ca)) as ws:
            assert ws.error == 0
            for kind, payload in [(2, bytes(range(256)) * 1025 + b'\0tail'),
                                  (1, 'TLS \U0001f30d\0'.encode())]:
                assert ws.send(payload, kind) == 0
                assert ws.recv() == (0, kind, payload)
            assert ws.close() == 0
        # The profile must not weaken ordinary certificate verification.
        with WebSocket(url + '/echo', profile=profile) as ws:
            assert ws.error != 0 and not ws.ptr
    finally:
        server.shutdown()
        server.server_close()
