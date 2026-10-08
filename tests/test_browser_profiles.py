"""Actual browser evidence, kept distinct from reference-library parity."""
import hashlib
import json
from pathlib import Path
import pytest
from binding import Session, lib
from browser_lab import browser_certificate, start_browser_h2
from fingerprint import capture, detailed_hello

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_DATES = ('2026-09-20', '2026-09-24', '2026-10-07')
REPORTS = {
    (date, mode): json.loads((ROOT / path).read_text())
    for date, paths in {
        '2026-09-20': {
            'headed': 'profiles/browsers/linux-headed-2026-09-20.json',
            'headless': 'profiles/browsers/linux-headless-2026-09-20.json',
        },
        '2026-09-24': {
            'headed': 'profiles/browsers/captures-2026-09-24/linux-headed.json',
            'headless': 'profiles/browsers/captures-2026-09-24/linux-headless.json',
        },
        '2026-10-07': {
            'headed': 'profiles/browsers/captures-2026-10-07/linux-headed.json',
            'headless': 'profiles/browsers/captures-2026-10-07/linux-headless.json',
        },
    }.items()
    for mode, path in paths.items()
}
TARGETS = [
    ('chrome153', 'google_chrome', 'headed', '2026-09-20'),
    ('firefox156', 'firefox', 'headed', '2026-09-20'),
    ('firefox156', 'firefox', 'headless', '2026-09-20'),
    ('chrome153_headless', 'chrome', 'headless', '2026-09-20'),
    ('chrome154', 'google_chrome', 'headed', '2026-09-24'),
    ('firefox156', 'firefox', 'headed', '2026-09-24'),
    ('firefox156', 'firefox', 'headless', '2026-09-24'),
    ('chrome154_headless', 'chrome', 'headless', '2026-09-24'),
    ('chrome155', 'google_chrome', 'headed', '2026-10-07'),
    ('chrome155_headless', 'chrome', 'headless', '2026-10-07'),
    ('firefox157', 'firefox', 'headed', '2026-10-07'),
    ('firefox157', 'firefox', 'headless', '2026-10-07'),
]
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


@pytest.mark.parametrize('date,mode', [(date, mode) for date in CAPTURE_DATES
                                      for mode in ('headed', 'headless')])
def test_browser_artifacts_reparse_and_agree(date, mode):
    report = REPORTS[(date, mode)]
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
@pytest.mark.parametrize('profile,browser,mode,date', TARGETS)
def test_profile_clienthello_matches_real_browser(profile, browser, mode, date):
    def send(url):
        with Session(profile=profile, timeout_ms=5000) as session, session.send(url) as response:
            assert response.error == 4
    expected = REPORTS[(date, mode)]['browsers'][browser]['samples'][0]['detailed_clienthello']
    for _ in range(3):
        assert capture(send, detailed=True) == expected


@pytest.mark.skipif(not BROWSER_BACKEND, reason='requires the optional source-built browser backend')
@pytest.mark.parametrize('profile,browser,mode,date', TARGETS)
def test_profile_http2_matches_real_browser(profile, browser, mode, date, tmp_path):
    context, ca, _ = browser_certificate(tmp_path)
    server, url = start_browser_h2(context)
    try:
        with Session(profile=profile, ca_bundle=str(ca), concurrency=1) as session, session.send(url) as response:
            assert response.error == 0, response.message
            assert b'capture complete' in response.body
        assert server.received.wait(2), server.errors
        expected = REPORTS[(date, mode)]['browsers'][browser]['http2_samples'][0]['connection']
        assert h2_fingerprint(server.result) == h2_fingerprint(expected)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(not BROWSER_BACKEND, reason='requires the optional source-built browser backend')
@pytest.mark.parametrize('profile', ['chrome153', 'chrome153_headless', 'chrome154', 'chrome154_headless', 'firefox156',
                                         'chrome155', 'chrome155_headless', 'firefox157'])
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


def test_2026_10_07_browser_wss_captures_validate_clean_close():
    import hashlib
    capture_root = ROOT / 'profiles/browsers/captures-2026-10-07'
    reports = [
        capture_root / 'linux-headed.json', capture_root / 'linux-headless.json',
        capture_root / 'wss-headed.json', capture_root / 'wss-headless.json',
    ]
    source_map = json.loads((capture_root / 'source-map.json').read_text())
    source_paths = set(source_map['sources'])
    for path in reports:
        report = json.loads(path.read_text())
        assert set(report['source_sha256']).issubset(source_paths)
        for sample_path, captured_hash in report['source_sha256'].items():
            pinned = source_map['sources'][sample_path]
            assert pinned['sha256'] == captured_hash
            snapshot = capture_root / pinned['snapshot']
            assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == captured_hash
        if not path.name.startswith('wss-'):
            continue
        assert report['mode'] == ('headed' if 'headed' in path.name else 'headless')
        assert len(report['browsers']) == (3 if report['mode'] == 'headed' else 2)
        for browser in report['browsers'].values():
            assert len(browser['samples']) == 3
            baseline = browser['samples'][0]['tls']['detailed_clienthello']
            for sample in browser['samples']:
                tls = sample['tls']
                records = [bytes.fromhex(record) for record in tls['tls_records_hex']]
                assert records and all(record[0] == 22 and len(record) == 5 + int.from_bytes(record[3:5], 'big')
                                       for record in records)
                hello = b''.join(record[5:] for record in records)
                assert len(hello) == 4 + int.from_bytes(hello[1:4], 'big') == tls['clienthello_bytes']
                assert hashlib.sha256(hello).hexdigest() == tls['clienthello_sha256']
                assert detailed_hello(hello) == tls['detailed_clienthello'] == baseline
                page = sample['browser_page_result']
                assert page == {'state': 'open', 'close_code': '1000', 'was_clean': 'true'}
                websocket = sample['websocket']
                assert websocket['status'] == 101 and websocket['validated_by_browser_onopen'] is True
                assert websocket['http_version'] == 'HTTP/1.1'
                assert websocket['tls_version'] == 'TLSv1.3'
                assert websocket['alpn'] == 'http/1.1'
                close_frames = [frame for frame in websocket['client_frames'] if frame['opcode'] == 8]
                assert close_frames == [{'opcode': 8, 'payload_hex': '03e863617074757265', 'final': True}]
