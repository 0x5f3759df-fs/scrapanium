"""Reproduce a rejected receive-cap experiment with unchanged exact-check clients.

The baseline is pinned to the published commit. Preparation reconstructs the
candidate from the retained report by default, never from the current checkout.
Use separate detached worktrees and an otherwise idle machine for measurement.
"""
import argparse
import datetime
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import traceback

MAIN = Path(__file__).resolve().parents[1]
REF = '0704d51c2275f26cd2580c95d7bb67f0655b1554'
NAMES = ('baseline', 'candidate', 'curl_cffi')
SEED = 20260923


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def shell(root, args, **kwargs):
    return subprocess.check_output(args, cwd=root, text=True, **kwargs).strip()


def sources(root):
    paths = ['scrapanium.bend', 'http.bend', 'dependencies.json', 'scripts/build.py',
             'tests/lab.py', 'tests/ws_lab.py', 'benchmarks/websocket.py', 'benchmarks/websocket.bend',
             'benchmarks/websocket_streaming.py', 'benchmarks/websocket_stream.bend',
             'benchmarks/websocket_clock.c', 'benchmarks/websocket_stream_python.py',
             'benchmarks/websocket_stream_server.go']
    paths += [str(p.relative_to(root)) for p in (root / 'native').iterdir() if p.is_file()]
    return {p: sha(root / p) for p in sorted(paths)}


def environment():
    prefix = (BASE / '.deps/curl').resolve()
    lib = prefix / 'lib' if (prefix / 'lib/libcurl-impersonate.so').exists() else prefix
    env = {**os.environ, 'SCRAPANIUM_CURL_DIR': str(prefix), 'LD_LIBRARY_PATH': str(lib)}
    for name in ('LD_PRELOAD', 'ASAN_OPTIONS', 'UBSAN_OPTIONS', 'WSS_PROBE_OUTPUT'):
        env.pop(name, None)
    return env, prefix, lib


def prepare(candidate_report, candidate_sources):
    if MANIFEST.exists():
        raise RuntimeError('immutable manifest already exists; choose a separate experiment directory')
    baseline = sources(BASE)
    git_blobs = {}
    for path, digest in baseline.items():
        raw = subprocess.check_output(['git', '-C', str(BASE), 'show', f'{REF}:{path}'])
        git_blobs[path] = hashlib.sha256(raw).hexdigest()
        assert digest == git_blobs[path], ('baseline does not match published Git blob', path)
    for root in (BASE, CANDIDATE):
        assert shell(root, ['git', 'rev-parse', 'HEAD']) == REF
        assert not shell(root, ['git', 'status', '--porcelain', '--untracked-files=no'])
    provenance = {}
    if candidate_sources:
        files = []
        frozen = {}
        for entry in candidate_sources:
            path, separator, source = entry.partition('=')
            if not separator or not path.startswith('native/') or '..' in Path(path).parts or path not in baseline:
                raise RuntimeError('--candidate-source requires native/path=/explicit/source/file')
            raw = Path(source).resolve().read_bytes()
            frozen[path] = hashlib.sha256(raw).hexdigest()
            (CANDIDATE / path).write_bytes(raw)
            files.append(path)
        provenance = {'explicit_candidate_sources': frozen}
    else:
        historical = json.loads(candidate_report.read_text())
        retained = historical['manifest']
        if retained['ref'] != REF:
            raise RuntimeError('candidate report uses a different baseline')
        files = retained['reviewed_changes']
        if not files or any(not p.startswith('native/') or '..' in Path(p).parts for p in files):
            raise RuntimeError('retained candidate must change only reviewed native files')
        patch = (retained['candidate_diff'].rstrip('\n') + '\n').encode()
        changed = subprocess.check_output(['git', 'apply', '--numstat', '-'], input=patch, cwd=CANDIDATE).decode()
        if {line.split('\t')[-1] for line in changed.splitlines()} != set(files):
            raise RuntimeError('retained patch paths differ from its explicit reviewed file list')
        subprocess.run(['git', 'apply', '--check', '-'], input=patch, cwd=CANDIDATE, check=True)
        subprocess.run(['git', 'apply', '-'], input=patch, cwd=CANDIDATE, check=True)
        provenance = {'retained_report': str(candidate_report), 'retained_report_sha256': sha(candidate_report)}
    candidate = sources(CANDIDATE)
    actual_changes = {p for p in baseline if baseline[p] != candidate[p]}
    assert actual_changes and actual_changes <= set(files), actual_changes
    if not candidate_sources:
        assert candidate == retained['candidate_source_sha256'], 'retained candidate reconstruction differs'
    env, prefix, lib = environment()
    binaries = {'baseline': str(BASE / 'build/stream-baseline'),
                'candidate': str(CANDIDATE / 'build/stream-candidate')}
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for name, root in (('baseline', BASE), ('candidate', CANDIDATE)):
        subprocess.run([sys.executable, 'scripts/build.py', 'benchmarks/websocket_stream.bend',
                        '-o', binaries[name]], cwd=root, env=env, check=True)
    peer = BASE / 'build/stream-peer'
    subprocess.run(['go', 'build', '-trimpath', '-o', str(peer), 'benchmarks/websocket_stream_server.go'], cwd=BASE, check=True)
    probe = ARTIFACTS / 'recv-probe.so'
    probe_source = MAIN / 'benchmarks/websocket_receive_probe.c'
    subprocess.run(['clang', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
        '-I' + str(prefix / 'include'), str(probe_source), '-ldl', '-lpthread', '-o', str(probe)], check=True)
    assert sources(BASE) == baseline and sources(CANDIDATE) == candidate
    binding = json.loads(shell(BASE, [str(BASE / '.deps/venv-matched/bin/python'), '-c',
        "import curl_cffi,hashlib,json,pathlib,sys; p=pathlib.Path(curl_cffi.__file__).parent; "
        "fs=[p/'curl.py',p/'requests/websockets.py',*p.glob('_wrapper*.so')]; "
        "print(json.dumps({'version':curl_cffi.__version__,'python':sys.version,'files':"
        "{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in fs}}))"], env=env))
    deps = json.loads((BASE / 'dependencies.json').read_text())
    bend = (BASE / '.deps/bend').resolve()
    assert shell(bend, ['git', 'rev-parse', 'HEAD']) == deps['bend']['commit']
    assert not shell(bend, ['git', 'status', '--porcelain', '--untracked-files=no'])
    external = [*(Path(p) for p in binaries.values()), peer, probe, probe_source,
                Path(__file__).resolve(), lib / 'libcurl-impersonate.so']
    external += [Path(p).with_suffix('.generated.c') for p in binaries.values()]
    external += list((prefix / 'include').rglob('*.h'))
    manifest = {'ref': REF, 'baseline_root': str(BASE), 'candidate_root': str(CANDIDATE),
        'baseline_git_blob_sha256': git_blobs, 'baseline_source_sha256': baseline,
        'candidate_source_sha256': candidate, 'reviewed_changes': sorted(actual_changes),
        'candidate_provenance': provenance,
        'candidate_diff': shell(CANDIDATE, ['git', 'diff', '--', *sorted(actual_changes)]),
        'binaries': binaries, 'peer': str(peer), 'probe': str(probe),
        'backend_sha256': sha(lib / 'libcurl-impersonate.so'), 'binding': binding,
        'external_sha256': {str(p): sha(p) for p in external}, 'dependencies': deps,
        'compiler': shell(BASE, ['clang', '--version']), 'go': shell(BASE, ['go', 'version']),
        'uname': platform.uname()._asdict(), 'affinity': sorted(os.sched_getaffinity(0))}
    write(MANIFEST, manifest)
    print(MANIFEST, flush=True)


def verify(manifest):
    assert sources(BASE) == manifest['baseline_source_sha256']
    assert sources(CANDIDATE) == manifest['candidate_source_sha256']
    for path, digest in {**manifest['external_sha256'], **manifest['binding']['files']}.items():
        assert sha(path) == digest, ('source or artifact changed', path)


def observe(server, run_id, workload):
    deadline = time.monotonic() + 2
    while True:
        server.stdin.write('stats\n'); server.stdin.flush()
        observed = json.loads(shared.line(server))[run_id]
        if observed.get('error') or observed['data_frames'] == workload['count'] or time.monotonic() >= deadline:
            break
        time.sleep(.01)
    size, count = workload['bytes'], workload['count']
    header = 2 if size < 126 else 4 if size < 65536 else 10
    assert not observed.get('error') and observed['warmups'] == observed['starts'] == 1, observed
    assert observed['data_frames'] == count and observed['data_payload_bytes'] == count * size, observed
    assert observed['data_frame_bytes'] == count * (size + header), observed
    assert (observed['tls_version'], observed['tls_cipher']) == (772, 4865), observed
    return observed


def run(action, output):
    if output.exists():
        raise RuntimeError('report path exists; refusing to discard prior samples')
    manifest = json.loads(MANIFEST.read_text()); verify(manifest)
    env, _, _ = environment()
    workloads = [dict(w) for w in stream.WORKLOADS]
    runs = 12 if action == 'measure' else 1
    if action in ('smoke', 'probe-smoke'):
        for w in workloads: w['count'] = 32
    rng = random.Random(SEED)
    orders = {}
    for w in workloads:
        choices = list(itertools.permutations(NAMES)) * 2
        rng.shuffle(choices); orders[w['name']] = choices
    schedule = []
    for repeat in range(runs):
        block = list(workloads); rng.shuffle(block)
        for w in block:
            schedule.extend({'repeat': repeat, 'workload': w['name'], 'client': name}
                            for name in orders[w['name']][repeat])
    report = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'status': 'running',
        'action': action, 'runs': runs, 'seed': SEED, 'manifest_sha256': sha(MANIFEST),
        'manifest': manifest, 'planned_schedule': schedule, 'execution_order': [], 'samples': [], 'failures': [],
        'negative_controls': [], 'workloads': workloads,
        'timing': 'Unchanged published real clients: start signal + exact opcode/byte/sequence check and release of both expected/actual buffers per message. Preparation/upgrade/warmup/close excluded.',
        'cpu_scope': 'Entire child process including expected corpus preparation and close; not receive-only CPU.',
        'probe_scope': 'Instrumented whole-client warmup + stream call counts; NEVER used for performance claims.',
        'probe_histogram_bins': {'offered': ['0..125', '126..16384', '16385..32768', '32769..65536', '65537..131072', '>131072'],
                                 'successful_return': ['zero', '1..125', '126..16384', '16385..32768', '32769..65536', '65537..131072', '>131072']}}
    write(output, report)
    with tempfile.TemporaryDirectory(prefix='wss-recv-cap-') as tmp:
        _, ca = stream.certificate(tmp)
        server = subprocess.Popen([manifest['peer'], '-cert', ca, '-key', str(Path(tmp) / 'key.pem')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        url = shared.line(server); body = Path(tmp) / 'body.bin'
        try:
            negative = {'bytes': 30, 'count': 32, 'peer_flush_frames': 64}
            body.write_bytes(bytes((i * 31) % 128 for i in range(22)))
            for name in NAMES:
                client = stream.CLIENTS[1] if name == 'curl_cffi' else stream.CLIENTS[0]
                for fault in ('corrupt', 'swap'):
                    row = stream.sample(client, manifest['binaries'].get(name), url, ca, body, negative,
                        f'negative-{name}-{fault}', env, manifest['backend_sha256'], 1, fault)
                    report['negative_controls'].append({'variant': name, **row})
                    write(output, report)
            print('All three clients rejected corruption and swaps', flush=True)
            lookup = {w['name']: w for w in workloads}
            for planned in schedule:
                w = lookup[planned['workload']]; name = planned['client']
                body.write_bytes(bytes((i * 31) % 128 for i in range(w['bytes'] - 8)))
                run_id = f"{w['name']}-{planned['repeat']}-{name}"
                client = stream.CLIENTS[1] if name == 'curl_cffi' else stream.CLIENTS[0]
                sample_env = dict(env)
                probe_path = ARTIFACTS / f'{output.stem}-{run_id}-probe.json'
                if action in ('probe', 'probe-smoke'):
                    sample_env.update(LD_PRELOAD=manifest['probe'], WSS_PROBE_OUTPUT=str(probe_path))
                started = time.monotonic()
                row = stream.sample(client, manifest['binaries'].get(name), url, ca, body, w, run_id,
                    sample_env, manifest['backend_sha256'], 1)
                row['peer_observation'] = observe(server, run_id, w)
                row['whole_sample_wall_ms'] = (time.monotonic() - started) * 1000
                if action in ('probe', 'probe-smoke'):
                    row['probe'] = json.loads(probe_path.read_text())
                    assert row['probe']['recv_calls'] > 0
                    assert row['probe']['returned_bytes'] == w['count'] * w['bytes'] + 6
                    assert not row['probe']['failed']
                report['samples'].append({**planned, **row})
                report['execution_order'].append(run_id)
                write(output, report)
                if len(report['samples']) % 18 == 0:
                    print(f"completed block {len(report['samples']) // 18}/{runs}", flush=True)
            verify(manifest)
            if action == 'measure':
                for w in workloads:
                    by_name = {n: [s for s in report['samples'] if s['workload'] == w['name'] and s['client'] == n] for n in NAMES}
                    w['clients'] = {n: stream.summarize(rows) for n, rows in by_name.items()}
                    for n, rows in by_name.items():
                        values = [r['elapsed_ms'] for r in rows]
                        w['clients'][n]['duration_coefficient_of_variation'] = statistics.stdev(values) / statistics.mean(values)
                    for label, numerator, denominator in (('candidate_vs_baseline', 'baseline', 'candidate'), ('candidate_vs_cffi', 'curl_cffi', 'candidate')):
                        ratios = [a['elapsed_ms'] / b['elapsed_ms'] for a, b in zip(by_name[numerator], by_name[denominator])]
                        w[label] = {'samples': ratios, 'median': statistics.median(ratios),
                                    'bootstrap_95_ci': shared.interval(ratios, SEED)}
            report['status'] = 'complete'; report['verified_unchanged_after_samples'] = True
            write(output, report)
        except BaseException as error:
            report['status'] = 'failed'
            report['failures'].append({'planned_sample': locals().get('planned'), 'exception': repr(error), 'traceback': traceback.format_exc()})
            write(output, report)
            raise
        finally:
            server.stdin.close()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired: server.kill(); server.wait()
    print(output, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'smoke', 'probe-smoke', 'measure', 'probe'))
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--candidate-root', type=Path, required=True)
    parser.add_argument('--artifact-dir', type=Path, default=MAIN / 'build/wss-receive-capacity')
    parser.add_argument('--candidate-report', type=Path,
                        default=MAIN / 'benchmarks/results/websocket-receive-capacity.json')
    parser.add_argument('--candidate-source', action='append', default=[],
                        help='Explicit reviewed native/path=/source/file; otherwise reconstruct retained report diff')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    BASE = args.baseline_root.resolve()
    CANDIDATE = args.candidate_root.resolve()
    ARTIFACTS = args.artifact_dir.resolve()
    MANIFEST = ARTIFACTS / 'manifest.json'
    if BASE == CANDIDATE or MAIN in (BASE, CANDIDATE):
        parser.error('baseline and candidate must be separate roots distinct from the helper checkout')
    if not (BASE / '.git').exists() or not (CANDIDATE / '.git').exists():
        parser.error('baseline and candidate must be Git worktrees/checkouts')
    sys.path.insert(0, str(BASE / 'benchmarks'))
    import websocket_streaming as stream
    shared = stream.shared
    if args.action == 'prepare': prepare(args.candidate_report.resolve(), args.candidate_source)
    else: run(args.action, args.output or ARTIFACTS / (args.action + '.json'))
