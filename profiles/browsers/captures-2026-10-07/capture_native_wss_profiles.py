#!/usr/bin/env python3
"""Capture existing Scrapanium native WSS profiles against the browser WSS peer.

This is a bounded local comparison tool, not a general benchmark. It writes only
public TLS ClientHello and RFC 6455 request observations; its temporary CA and
private key are removed with the per-sample temporary directory.
"""
import argparse
import ctypes as C
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
CAPTURE_DIR = Path(__file__).resolve().parent
BINARY_DEFAULT = Path('/home/baidu/scrapanium-experiments/bend2-027-compat/worktree/build/libscrapanium.so')
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

spec = importlib.util.spec_from_file_location('capture_browser_wss', CAPTURE_DIR / 'capture_browser_wss.py')
cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cap)


class Fingerprint(C.Structure):
    _fields_ = [(name, C.c_char_p) for name in (
        'ciphers', 'curves', 'signature_algorithms', 'extension_order',
        'http2_settings', 'pseudo_header_order', 'cert_compression'
    )] + [('grease', C.c_int), ('permute_extensions', C.c_int),
         ('http2_window_update', C.c_uint32)]


class Config(C.Structure):
    _fields_ = [(name, C.c_char_p) for name in ('profile', 'proxy', 'ca_bundle')] + [
        (name, C.c_uint32) for name in ('timeout_ms', 'connect_timeout_ms', 'max_redirects', 'concurrency')
    ] + [(name, C.c_size_t) for name in ('max_body_bytes', 'max_header_bytes')] + [
        (name, C.c_int) for name in ('verify', 'follow_redirects', 'default_headers')
    ] + [('fingerprint', Fingerprint)] + [
        (name, C.c_size_t) for name in ('max_batch_bytes', 'max_batch_requests')
    ]


class Request(C.Structure):
    _fields_ = [('method', C.c_char_p), ('url', C.c_char_p),
                ('headers', C.POINTER(C.c_char_p)), ('header_count', C.c_size_t),
                ('body', C.c_void_p), ('body_size', C.c_size_t)]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def loaded_backend_path():
    for line in Path('/proc/self/maps').read_text().splitlines():
        if 'libcurl-impersonate.so' in line:
            path = line.rsplit(maxsplit=1)[-1]
            if path.startswith('/'):
                return Path(path).resolve()
    raise RuntimeError('could not identify the loaded libcurl-impersonate shared object')


def source_pins(binary, backend):
    paths = [
        Path(__file__).resolve(), CAPTURE_DIR / 'capture_browser_wss.py',
        ROOT / 'native/profiles.inc.c', ROOT / 'native/scrapanium.c', ROOT / 'native/scrapanium.h',
        ROOT / 'native/websocket.inc.c', ROOT / 'native/ws_handshake.inc.c',
        ROOT / 'scripts/capture_browsers.py', ROOT / 'tests/fingerprint.py',
        ROOT / 'tests/browser_lab.py', ROOT / 'tests/ws_lab.py',
    ]
    out = {str(path.relative_to(ROOT)): digest(path) for path in paths}
    out['binary:' + str(binary)] = digest(binary)
    out['backend:' + str(backend)] = digest(backend)
    return out


def call(native, profile, origin, binary):
    config = Config()
    native.sp_config_default(C.byref(config))
    config.profile = profile.encode()
    config.ca_bundle = os.fsencode(origin[1])
    error = C.c_int(-999)
    session = native.sp_session_new(C.byref(config), C.byref(error))
    if not session:
        raise RuntimeError(f'{profile}: session construction failed with error {error.value}')
    header_text = f'Origin: {origin[0]}'.encode()
    headers = (C.c_char_p * 1)(header_text)
    request = Request(b'GET', origin[2].encode(), headers, 1, None, 0)
    native.sp_ws_upgrade.restype = C.c_void_p
    native.sp_ws_upgrade.argtypes = [C.c_void_p, C.POINTER(Request), C.c_void_p, C.POINTER(C.c_int)]
    ws = native.sp_ws_upgrade(session, C.byref(request), None, C.byref(error))
    if not ws:
        raise RuntimeError(f'{profile}: websocket upgrade failed with error {error.value}')
    native.sp_ws_close.argtypes = [C.c_void_p, C.c_uint, C.c_void_p, C.c_size_t, C.c_uint32, C.c_void_p]
    reason = b'capture'
    close_code = native.sp_ws_close(ws, 1000, C.c_char_p(reason), len(reason), 3000, None)
    native.sp_ws_free.argtypes = [C.c_void_p]
    native.sp_ws_free(ws)
    if close_code != 0:
        raise RuntimeError(f'{profile}: WebSocket close returned {close_code}')
    return {'upgrade_error': error.value, 'close_error': close_code}


def browser_origin(report, browser):
    record = report['browsers'][browser]['samples'][0]['websocket']
    return dict((name.lower(), value) for name, value in record['headers'])['origin']


def one_profile(native, profile, browser_report, browser, temp_root):
    with tempfile.TemporaryDirectory(prefix='scrapanium-native-wss-', dir=temp_root) as work:
        context, ca, _leaf = cap.browser_certificate(work)
        peer, _ = cap.start_ws(context)
        cap.observe_ws_server(peer)
        proxy = cap.ProxyServer(('127.0.0.1', 0), cap.ProxyHandler)
        proxy.backend = peer.server_address
        proxy.observations, proxy.errors = [], []
        import threading
        proxy.observed = threading.Event()
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        origin = browser_origin(browser_report, browser)
        url = f'wss://localhost:{proxy.server_address[1]}/echo'
        try:
            result = call(native, profile, (origin, ca, url), None)
            if not proxy.observed.wait(5):
                raise TimeoutError(f'{profile}: proxy did not observe the TLS ClientHello')
            if len(proxy.observations) != 1 or len(peer.ws_metadata) != 1:
                raise AssertionError(f'{profile}: expected one ClientHello and one Upgrade')
            if not any(opcode == 8 and payload == b'\x03\xe8capture' for opcode, payload, _ in peer.frames):
                raise AssertionError(f'{profile}: peer did not observe close code 1000 and reason capture')
            return {
                'profile': profile,
                'browser_reference': browser,
                'browser_reference_sample_sha256': hashlib.sha256(
                    json.dumps(browser_report['browsers'][browser]['samples'][0], sort_keys=True,
                               separators=(',', ':')).encode()).hexdigest(),
                'native': result,
                'tls': proxy.observations[0],
                'websocket': {**peer.ws_metadata[0], 'status': 101,
                              'status_semantics': 'server configured 101; native API accepted upgrade'},
                'peer_client_frames': [
                    {'opcode': opcode, 'payload_hex': payload.hex(), 'final': final}
                    for opcode, payload, final in peer.frames
                ],
                'close_observed': True,
            }
        finally:
            proxy.shutdown(); proxy.server_close()
            peer.shutdown(); peer.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=BINARY_DEFAULT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--temp-root', type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    output = args.output.resolve()
    temp_root = args.temp_root.resolve(strict=True)
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error('output must be a fresh path inside the repository')
    if not temp_root.is_relative_to(Path('/tmp')):
        parser.error('temporary certificate root must be beneath /tmp')
    library = C.CDLL(str(binary))
    library.sp_backend_version.restype = C.c_char_p
    backend_version = library.sp_backend_version().decode()
    if 'curl-impersonate/2.2.3' not in backend_version:
        parser.error(f'unexpected optional backend: {backend_version}')
    backend = loaded_backend_path()
    reports = {
        'chrome': json.loads((CAPTURE_DIR / 'wss-headed.json').read_text()),
        'chrome_headless': json.loads((CAPTURE_DIR / 'wss-headless.json').read_text()),
    }
    manifest = json.loads((ROOT / 'profiles/browsers/releases-2026-10-07.json').read_text())
    candidates = [
        ('chrome154', 'chrome', reports['chrome'], 'google_chrome'),
        ('chrome154_headless', 'chrome_headless', reports['chrome_headless'], 'chrome'),
        ('firefox156', 'chrome', reports['chrome'], 'firefox'),
    ]
    result = {
        'schema': 1,
        'captured_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'scope': 'existing native profile WSS control against a local RFC 6455 peer; one fresh connection per profile',
        'native_commit': '7b52d7f1cbbfe4c21b1abf1ad71e8e3061c47c17',
        'binary': str(binary),
        'backend_version': backend_version,
        'binary_sha256': digest(binary),
        'backend_sha256': digest(backend),
        'release_manifest_sha256': digest(ROOT / 'profiles/browsers/releases-2026-10-07.json'),
        'browser_capture_sha256': {
            'headed': digest(CAPTURE_DIR / 'wss-headed.json'),
            'headless': digest(CAPTURE_DIR / 'wss-headless.json'),
        },
        'source_sha256': source_pins(binary, backend),
        'profiles': {},
        'failures': [],
    }
    for profile, _mode, report, browser in candidates:
        result['profiles'][profile] = one_profile(library, profile, report, browser, temp_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.tmp')
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    temporary.replace(output)
    print(f'wrote {output}')
    print(f'binary sha256 {result["binary_sha256"]}')
    print(f'backend sha256 {result["backend_sha256"]}')
    for name, record in result['profiles'].items():
        print(f'{name}: upgrade ok; close ok; header-count={len(record["websocket"]["headers"])}; TLS hello bytes={record["tls"]["clienthello_bytes"]}')


if __name__ == '__main__':
    main()
