import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time

ROOT = Path('/mnt/c/Users/Baidu/Engrama/scrapanium')
EVID = Path('/home/baidu/wss-perf-feasibility-20260923/wss-actual')
PERF = Path('/home/baidu/wss-perf-feasibility-20260923/extracted/usr/bin/perf')
PERF_LIB = '/home/baidu/wss-perf-feasibility-20260923/extracted/usr/lib/x86_64-linux-gnu'
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT / 'benchmarks')]
from lab import certificate
from websocket import mapped_backend

EVID.mkdir(parents=True, exist_ok=True)

def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def read_line(proc, timeout=30):
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError(f'timeout reading pid {proc.pid}')
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(f'process {proc.pid} exited {proc.poll()}: {proc.stderr.read()}')
    return line.rstrip('\r\n')

def wait_ack(fd, proc, timeout=10):
    deadline = time.monotonic() + timeout
    buf = b''
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('perf exited before ack: ' + proc.stderr.read().decode(errors='replace'))
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        try:
            buf += os.read(fd, 4096)
        except BlockingIOError:
            continue
        if b'ack\n' in buf:
            return buf.decode(errors='replace')
    raise RuntimeError(f'perf ack timeout, bytes={buf!r}')

def send_ctl(fd, command):
    os.write(fd, (command + '\n').encode())

out = EVID / 'run'
out.mkdir(exist_ok=True)
for old in out.iterdir():
    if old.is_file() or old.is_symlink(): old.unlink()
    elif old.is_dir(): raise RuntimeError('unexpected existing directory in output: ' + str(old))

client = ROOT / 'build/bench-ws-stream'
peer = ROOT / 'build/bench-ws-stream-server'
backend = ROOT / '.deps/curl/libcurl-impersonate.so.4.8.0'
body = out / 'body.bin'
body.write_bytes(bytes((i * 31) % 128 for i in range(65536 - 8)))
source_paths = [ROOT / 'benchmarks/websocket_stream.bend', ROOT / 'benchmarks/websocket_streaming.py',
                ROOT / 'benchmarks/websocket_stream_server.go', ROOT / 'native/websocket.inc.c',
                ROOT / 'native/bend_bridge.c']
source_hashes = {str(p.relative_to(ROOT)): sha(p) for p in source_paths}
base_record = {
    'workload': {'client': 'scrapanium-bend', 'count': 1024, 'message_payload_bytes': 65536,
                 'payload_total_bytes': 1024 * 65536, 'peer_batch': 1, 'profile': 'chrome146', 'tls': 'WSS'},
    'measurement_window': 'perf cpu-clock:u event disabled until ready; FIFO enable+ack before start byte; disable+ack after client end output; one session only; no repeat or extension',
    'sampling': {'event': 'cpu-clock:u', 'frequency_hz_requested': 999, 'call_graph': 'DWARF, 16384-byte user stack',
                 'kernel_stacks': False, 'perf_version': None},
    'artifacts': {}, 'source_sha256': source_hashes,
    'binary_sha256': {'bend': sha(client), 'go_peer': sha(peer), 'stock_backend': sha(backend)},
}
base_record['sampling']['perf_version'] = subprocess.check_output(
    [str(PERF), 'version'], env={**os.environ, 'LD_LIBRARY_PATH': PERF_LIB}, text=True).strip()

with tempfile.TemporaryDirectory(prefix='wss-perf-one-session-', dir=EVID) as tdir:
    _, ca = certificate(tdir)
    key = str(Path(tdir) / 'key.pem')
    server = subprocess.Popen([str(peer), '-cert', ca, '-key', key], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc = None
    perf = None
    ctlfd = ackfd = None
    try:
        url = read_line(server)
        body_path = body
        env = dict(os.environ)
        libdir = str(backend.parent)
        env['LD_LIBRARY_PATH'] = libdir + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
        env.update({'SCRAPANIUM_BENCH_URL': f'{url}/stream?id=perf-feasibility-one&size=65536&count=1024&batch=1&fault=',
                    'SCRAPANIUM_BENCH_CA': ca, 'SCRAPANIUM_BENCH_COUNT': '1024',
                    'SCRAPANIUM_BENCH_BODY': str(body_path)})
        proc = subprocess.Popen([str(client), '--threads', '1'], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        if read_line(proc, 30) != 'ready': raise RuntimeError('client was not ready')
        mapping = mapped_backend(proc)
        if mapping['sha256'] != base_record['binary_sha256']['stock_backend']:
            raise RuntimeError(f'client mapped wrong backend: {mapping}')
        base_record['mapped_backend'] = mapping

        ctl = out / 'perf.ctl'
        ack = out / 'perf.ack'
        os.mkfifo(ctl); os.mkfifo(ack)
        # Open our FIFO ends O_RDWR so perf startup cannot deadlock on open ordering.
        ctlfd = os.open(ctl, os.O_RDWR | os.O_NONBLOCK)
        ackfd = os.open(ack, os.O_RDWR | os.O_NONBLOCK)
        perfdata = out / 'perf.data'
        perf_cmd = [str(PERF), 'record', '--delay=-1', '--event=cpu-clock:u', '--freq=999',
                    '--call-graph=dwarf,16384', '--clockid=monotonic',
                    f'--control=fifo:{ctl},{ack}', '-p', str(proc.pid), '-o', str(perfdata)]
        perf_env = {**os.environ, 'LD_LIBRARY_PATH': PERF_LIB}
        perf = subprocess.Popen(perf_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=perf_env)
        # First ping proves control channel initialized and perf is attached.
        send_ctl(ctlfd, 'ping'); ping_ack = wait_ack(ackfd, perf)
        enable_requested_ns = time.monotonic_ns()
        send_ctl(ctlfd, 'enable'); enable_ack = wait_ack(ackfd, perf)
        gate_ns = time.monotonic_ns()
        proc.stdin.write('x'); proc.stdin.flush()
        output, client_err = proc.communicate(timeout=120)
        client_finish_ns = time.monotonic_ns()
        if proc.returncode != 0:
            raise RuntimeError(f'client failed rc={proc.returncode}: {output}\n{client_err}')
        lines = output.strip().splitlines()
        if len(lines) != 2: raise RuntimeError(f'unexpected client output: {output!r}')
        start_us, end_us = map(int, lines)
        if end_us <= start_us: raise RuntimeError(f'invalid client interval {start_us}, {end_us}')
        disable_requested_ns = time.monotonic_ns()
        send_ctl(ctlfd, 'disable'); disable_ack = wait_ack(ackfd, perf)
        send_ctl(ctlfd, 'stop')
        perf_out, perf_err = perf.communicate(timeout=30)
        if perf.returncode != 0:
            raise RuntimeError(f'perf failed rc={perf.returncode}: {perf_err.decode(errors="replace")}')

        # Read peer stats only after profiling has stopped. This cannot affect the client interval.
        server.stdin.write('stats\n'); server.stdin.flush()
        stats = json.loads(read_line(server))['perf-feasibility-one']
        expected_frames = 1024
        expected_payload = 1024 * 65536
        expected_frame_bytes = 1024 * (65536 + 10)
        if stats.get('error') or stats['warmups'] != 1 or stats['starts'] != 1 or stats['data_frames'] != expected_frames or stats['data_payload_bytes'] != expected_payload or stats['data_frame_bytes'] != expected_frame_bytes:
            raise RuntimeError(f'peer totals failed exact gate: {stats}')

        base_record.update({'client_returncode': proc.returncode, 'client_stdout': output,
            'client_stderr': client_err, 'client_interval_us': {'start': start_us, 'end': end_us, 'elapsed': end_us-start_us},
            'control_clock_bounds_monotonic_ns': {'perf_enable_ack_after': enable_requested_ns,
                'client_gate_write': gate_ns, 'client_finished_after': client_finish_ns,
                'perf_disable_request': disable_requested_ns},
            'perf_control': {'ping_ack': ping_ack, 'enable_ack': enable_ack, 'disable_ack': disable_ack,
                             'command': perf_cmd, 'stdout': perf_out.decode(errors='replace'),
                             'stderr': perf_err.decode(errors='replace'), 'returncode': perf.returncode},
            'peer': stats, 'exact_gate_passed': True})
    finally:
        if perf is not None and perf.poll() is None:
            try: send_ctl(ctlfd, 'stop')
            except Exception: pass
            try: perf.communicate(timeout=5)
            except Exception: perf.kill(); perf.communicate()
        if proc is not None and proc.poll() is None:
            proc.kill(); proc.communicate()
        if server.stdin and not server.stdin.closed:
            server.stdin.close()
        try: server.wait(timeout=5)
        except subprocess.TimeoutExpired: server.kill(); server.wait()
        if ctlfd is not None: os.close(ctlfd)
        if ackfd is not None: os.close(ackfd)

# Decode one captured perf file and retain machine-readable text/output.
perf_env = {**os.environ, 'LD_LIBRARY_PATH': PERF_LIB}
script = subprocess.run([str(PERF), 'script', '-i', str(out / 'perf.data')],
                        env=perf_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
(out / 'perf.script.txt').write_text(script.stdout)
report = subprocess.run([str(PERF), 'report', '--stdio', '--no-children', '--sort=symbol', '--percent-limit=0',
                         '-i', str(out / 'perf.data')], env=perf_env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
(out / 'perf.report.txt').write_text(report.stdout)
base_record['profile_summary'] = {
    'perf_data_bytes': (out / 'perf.data').stat().st_size,
    'perf_data_sha256': sha(out / 'perf.data'),
    'perf_script_sha256': sha(out / 'perf.script.txt'),
    'perf_report_sha256': sha(out / 'perf.report.txt'),
    'sample_records': sum(1 for line in script.stdout.splitlines() if line and not line[0].isspace() and 'cpu-clock:' in line),
    'resolved_symbols_present': {name: name in script.stdout for name in
        ('curl_ws_recv', 'Curl_easy_recv', 'SSL_read', 'ssl_read_impl', 'EVP_AEAD_CTX_open',
         'aead_aes_gcm_openv_detached_impl', 'sp_ws_io_advance', 'sp_ws_append', 'memcmp')},
    'lost_record_mentions': [line for line in (script.stderr + '\n' + report.stderr).splitlines() if 'lost' in line.lower()],
    'report_top': '\n'.join(report.stdout.splitlines()[-45:]),
}
base_record['artifacts'] = {p.name: {'bytes': p.stat().st_size, 'sha256': sha(p)}
                            for p in sorted(out.iterdir()) if p.is_file() and p.name not in ('perf.ctl','perf.ack')}
(out / 'feasibility.json').write_text(json.dumps(base_record, indent=2) + '\n')
print(json.dumps(base_record, indent=2))
