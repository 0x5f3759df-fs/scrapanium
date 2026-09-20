#!/usr/bin/env python3
"""Capture real-browser ClientHello offers and optional HTTP/2 navigation.

Linux only. Each sample launches an explicitly supplied browser binary in a
temporary profile, without reading the user's installed browser or profile.
The ClientHello listener closes before ServerHello. Optional HTTP/2 samples use
a separate TLS peer and local test certificate. Neither proves resumption,
ECH acceptance, WebSocket parity, or complete browser behavior.
"""
import argparse
import base64
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from fingerprint import parse_hello, normalized, detailed_hello


def sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_hello(conn):
    """Bound and retain every complete TLS record used for the first hello."""
    def exact(size):
        result = bytearray()
        while len(result) < size:
            block = conn.recv(size - len(result))
            if not block:
                raise EOFError('peer closed before its ClientHello completed')
            result.extend(block)
        return bytes(result)
    handshake, records = bytearray(), []
    while len(handshake) < 4 or len(handshake) < 4 + int.from_bytes(handshake[1:4], 'big'):
        header = exact(5)
        size = int.from_bytes(header[3:], 'big')
        if header[0] != 22 or size > 18432:
            raise ValueError('invalid TLS handshake record')
        payload = exact(size)
        records.append((header + payload).hex())
        handshake.extend(payload)
        if len(handshake) > 262144:
            raise ValueError('ClientHello exceeds capture budget')
    if handshake[0] != 1:
        raise ValueError('first handshake message is not ClientHello')
    size = 4 + int.from_bytes(handshake[1:4], 'big')
    return bytes(handshake[:size]), records


@contextlib.contextmanager
def browser_process(kind, binary, url, ca=None, leaf=None, *, headless=True):
    with tempfile.TemporaryDirectory(prefix='scrapanium-browser-') as work:
        profile = Path(work) / 'profile'
        profile.mkdir()
        if kind == 'firefox':
            preferences = {
                'app.update.auto': False,
                'browser.shell.checkDefaultBrowser': False,
                'datareporting.policy.dataSubmissionEnabled': False,
                'toolkit.telemetry.enabled': False,
                'network.captive-portal-service.enabled': False,
                'network.connectivity-service.enabled': False,
            }
            (profile / 'user.js').write_text(''.join(
                f'user_pref({json.dumps(k)}, {json.dumps(v)});\n'
                for k, v in preferences.items()))
            flags = ['--no-remote', '--profile', str(profile)]
        else:
            preferences = {}
            flags = ['--no-first-run', '--no-default-browser-check',
                     '--disable-background-networking', '--disable-dev-shm-usage',
                     '--no-sandbox', '--user-data-dir=' + str(profile)]
        if headless:
            flags.insert(0, '--headless')
        if ca:
            if kind == 'firefox':
                subprocess.run(['certutil', '-N', '-d', 'sql:' + str(profile), '--empty-password'], check=True)
                subprocess.run(['certutil', '-A', '-d', 'sql:' + str(profile), '-n',
                                'Scrapanium local test CA', '-t', 'C,,', '-i', str(ca)], check=True)
            else:
                from cryptography import x509
                from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
                cert = x509.load_pem_x509_certificate(Path(leaf or ca).read_bytes())
                public = cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
                pin = base64.b64encode(hashlib.sha256(public).digest()).decode()
                flags.append('--ignore-certificate-errors-spki-list=' + pin)
        with open(Path(work) / 'stderr.log', 'w+') as log:
            process = subprocess.Popen([str(binary), *flags, url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                start_new_session=True)
            try:
                yield process, [value.replace(str(profile), '<temporary-profile>') for value in flags], preferences, log
            finally:
                # Only the process group we created is terminated; no installed
                # browser processes are discovered or touched.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def capture_browser(kind, binary, timeout, *, headless=True):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        listener.settimeout(.2)
        url = f'https://localhost:{listener.getsockname()[1]}/'
        with browser_process(kind, binary, url, headless=headless) as (process, flags, preferences, log):
            deadline = time.monotonic() + timeout
            while True:
                try:
                    conn, _ = listener.accept()
                    break
                except socket.timeout:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise RuntimeError(f'{kind} produced no ClientHello: ' + log.read()[-3000:])
            with conn:
                conn.settimeout(max(.1, deadline - time.monotonic()))
                hello, records = read_hello(conn)
            parsed = parse_hello(hello)
            return {
                'tls_records_hex': records,
                'clienthello_sha256': hashlib.sha256(hello).hexdigest(),
                'clienthello_bytes': len(hello),
                'parsed_clienthello': parsed,
                'normalized_clienthello': normalized(parsed),
                'detailed_clienthello': detailed_hello(hello),
                'launch_flags': flags, 'preferences': preferences,
            }


def capture_http2(kind, binary, timeout, *, headless=True):
    from browser_lab import browser_certificate, start_browser_h2
    with tempfile.TemporaryDirectory(prefix='scrapanium-browser-ca-') as work:
        context, ca, leaf = browser_certificate(work)
        server, url = start_browser_h2(context)
        try:
            with browser_process(kind, binary, url, ca, leaf, headless=headless) as (process, flags, preferences, log):
                deadline = time.monotonic() + timeout
                while not server.received.wait(.1):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise RuntimeError(f'{kind} produced no HTTP/2 request: ' + log.read()[-3000:] + repr(server.errors))
                return {'connection': server.result, 'launch_flags': flags,
                        'preferences': preferences,
                        'certificate_setup': 'local certificate SPKI allowlist' if kind == 'chrome' else
                                             'local CA imported into fresh NSS profile'}
        finally:
            server.shutdown()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome', type=Path)
    parser.add_argument('--google-chrome', type=Path, help='Google Chrome, kept separate from Chrome for Testing')
    parser.add_argument('--firefox', type=Path)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--http2', action='store_true', help='also capture HTTP/2 against a local test certificate')
    parser.add_argument('--headed', action='store_true', help='normal browser mode; run under xvfb-run on a test display')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'linux' or not 1 <= args.samples <= 100 or args.timeout <= 0:
        parser.error('requires Linux, 1..100 samples and a positive timeout')
    if not (args.chrome or args.google_chrome or args.firefox):
        parser.error('supply at least one explicit browser executable')
    if args.headed and not os.environ.get('DISPLAY'):
        parser.error('--headed requires an X display, for example xvfb-run -a')
    source_paths = [Path(__file__).resolve(), ROOT / 'tests/fingerprint.py',
                    ROOT / 'tests/browser_lab.py', ROOT / 'tests/lab.py']
    sources = {str(path.relative_to(ROOT)): sha256(path) for path in source_paths}
    releases = json.loads((ROOT / 'profiles/browsers/releases.json').read_text())
    results = {}
    for label, kind in [('chrome', 'chrome'), ('google_chrome', 'chrome'), ('firefox', 'firefox')]:
        binary = getattr(args, label)
        if binary is None:
            continue
        binary = binary.resolve(strict=True)
        version = subprocess.check_output([str(binary), '--version'], text=True, timeout=15).strip()
        print(f'Capturing {version}', flush=True)
        samples = [capture_browser(kind, binary, args.timeout, headless=not args.headed) for _ in range(args.samples)]
        results[label] = {
            'version_output': version, 'executable_sha256': sha256(binary),
            'samples': samples,
            'normalized_samples_identical': all(
                sample['normalized_clienthello'] == samples[0]['normalized_clienthello'] for sample in samples),
            'detailed_samples_identical': all(
                sample['detailed_clienthello'] == samples[0]['detailed_clienthello'] for sample in samples),
        }
        pinned = (ROOT / '.deps/browsers' / releases[label]['executable']).resolve()
        if binary == pinned:
            archive = ROOT / '.deps/browsers' / releases[label]['archive']
            if sha256(archive) != releases[label]['sha256']:
                raise ValueError('browser distribution hash mismatch')
            results[label]['download'] = releases[label]
        if kind == 'firefox':
            results[label]['libxul_sha256'] = sha256(binary.parent / 'libxul.so')
        if args.http2:
            print(f'Capturing {kind} HTTP/2', flush=True)
            results[label]['http2_samples'] = [capture_http2(kind, binary, args.timeout, headless=not args.headed) for _ in range(args.samples)]
    report = {
        'schema': 1,
        'captured_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'platform': {'system': platform.system(), 'release': platform.release(),
                     'machine': platform.machine(), 'libc': platform.libc_ver()},
        'capture': 'TCP localhost; fresh temporary profile per sample; first ClientHello',
        'browser_mode': 'headed' if args.headed else 'headless',
        'scope': 'ClientHello offers and optionally fresh HTTP/2 navigation; not complete browser parity',
        'normalization': ['GREASE values (counts retained where parsed)',
                          'random/session/key-share bytes', 'extension permutation',
                          'conditional padding extension 21', 'SNI value', 'ECH payload bytes',
                          'trust-anchor order'],
        'source_sha256': sources,
        'browsers': results,
    }
    if sources != {str(path.relative_to(ROOT)): sha256(path) for path in source_paths}:
        raise RuntimeError('capture sources changed while running; discard and repeat this capture')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(args.output)


if __name__ == '__main__':
    main()
