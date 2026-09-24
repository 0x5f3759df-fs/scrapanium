#!/usr/bin/env python3
"""Build and verify a bounded same-Zig memcpy link-resolution probe."""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
ROUTE_SOURCE = Path(__file__).with_name('memcpy_route.c')
CALLSITE_SOURCE = Path(__file__).with_name('memcpy_probe_callsite.c')
TARGET = 'x86_64-linux-gnu.2.17.0'
EXPECTED_ZIG_ARCHIVE_SHA256 = '02aa270f183da276e5b5920b1dac44a63f1a49e55050ebde3aecc9eb82f93239'
COMMAND_TIMEOUT_SECONDS = 120
CHILD_TIMEOUT_SECONDS = 15


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def run(argv, *, cwd, log):
    with Path(log).open('a', encoding='utf-8') as stream:
        stream.write('$ ' + ' '.join(map(str, argv)) + '\n')
        stream.flush()
        try:
            result = subprocess.run(argv, cwd=cwd, stdout=stream,
                                    stderr=subprocess.STDOUT, check=False,
                                    timeout=COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            stream.write(f'[timeout after {COMMAND_TIMEOUT_SECONDS}s]\n')
            raise RuntimeError(f'command timed out: {argv[0]}') from error
        stream.write(f'[exit {result.returncode}]\n')
    if result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}): {argv[0]}')


def output(argv):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=True,
                            timeout=30)
    return result.stdout.strip()


def glibc_versions(text):
    return sorted({tuple(map(int, value.split('.'))) for value in
                   re.findall(r'GLIBC_(\d+(?:\.\d+)+)', text)})


def version_text(versions):
    return ['.'.join(map(str, version)) for version in versions]


def symbol_rows(text):
    rows = {}
    for line in text.splitlines():
        fields = line.split(maxsplit=7)
        if len(fields) != 8 or not fields[0].endswith(':'):
            continue
        try:
            value = int(fields[1], 16)
            size = int(fields[2])
        except ValueError:
            continue
        rows.setdefault(fields[7], []).append({
            'value': value,
            'size': size,
            'type': fields[3],
            'bind': fields[4],
            'visibility': fields[5],
            'index': fields[6],
        })
    return rows


def unique_symbol(rows, name):
    found = [item for item in rows.get(name, [])
             if item['type'] == 'FUNC' and item['index'] != 'UND']
    if not found or len({item['value'] for item in found}) != 1:
        raise RuntimeError(f'missing or ambiguous symbol value: {name}')
    return found[-1]


def block_at(disassembly, address, size):
    match = re.search(rf'^{address:016x} <[^>]+>:$', disassembly, re.M)
    if not match:
        raise RuntimeError(f'no disassembly block at address 0x{address:x}')
    end_address = address + size
    lines = [disassembly[match.start():match.end()]]
    for line in disassembly[match.end():].splitlines(keepends=True):
        instruction = re.match(r'^\s*([0-9a-fA-F]+):', line)
        if not instruction:
            continue
        if int(instruction.group(1), 16) >= end_address:
            break
        lines.append(line)
    return ''.join(lines)


def block_until_next_symbol(disassembly, address):
    match = re.search(rf'^{address:016x} <[^>]+>:$', disassembly, re.M)
    if not match:
        raise RuntimeError(f'no disassembly block at address 0x{address:x}')
    following = re.search(r'^\s*[0-9a-fA-F]+ <[^>]+>:$',
                           disassembly[match.end():], re.M)
    end = match.end() + following.start() if following else len(disassembly)
    return disassembly[match.start():end]


def branch_targets(block):
    return [(int(address, 16), label) for address, label in re.findall(
        r'\b(?:call|jmp)\s+([0-9a-fA-F]+)\s+<([^>]+)>', block)]


def exercise(library, symbol):
    loaded = ctypes.CDLL(str(library), mode=ctypes.RTLD_LOCAL)
    copy = getattr(loaded, symbol)
    copy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    copy.restype = ctypes.c_void_p
    for size in (0, 1, 3, 31, 257, 4097, 65536):
        source = (ctypes.c_ubyte * (size + 32))(
            *((index * 37 + 11) & 0xff for index in range(size + 32)))
        destination = (ctypes.c_ubyte * (size + 64))(*([0xa5] * (size + 64)))
        src = ctypes.addressof(source) + 7
        dst = ctypes.addressof(destination) + 19
        returned = copy(dst, src, size)
        if returned != dst:
            raise RuntimeError(f'{library}:{symbol} returned a different destination')
        if ctypes.string_at(dst, size) != ctypes.string_at(src, size):
            raise RuntimeError(f'{library}:{symbol} copied different bytes at size {size}')
        if bytes(destination[:19]) != b'\xa5' * 19 or bytes(destination[19 + size:]) != b'\xa5' * 45:
            raise RuntimeError(f'{library}:{symbol} overwrote a canary at size {size}')


def exercise_child(library, symbol, output_dir, label):
    stdout_path = output_dir / f'{label}.stdout.txt'
    stderr_path = output_dir / f'{label}.stderr.txt'
    status_path = output_dir / f'{label}.status.json'
    argv = [sys.executable, str(Path(__file__).resolve()), '--exercise-only',
            '--library', str(library), '--symbol', symbol]
    try:
        result = subprocess.run(argv, cwd=ROOT, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=CHILD_TIMEOUT_SECONDS, check=False)
        stdout_path.write_text(result.stdout, encoding='utf-8')
        stderr_path.write_text(result.stderr, encoding='utf-8')
        status = {'argv': argv, 'returncode': result.returncode,
                  'timed_out': False, 'timeout_seconds': CHILD_TIMEOUT_SECONDS}
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode(errors='replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
        stderr = error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
        stdout_path.write_text(stdout, encoding='utf-8')
        stderr_path.write_text(stderr, encoding='utf-8')
        status = {'argv': argv, 'returncode': None, 'timed_out': True,
                  'timeout_seconds': CHILD_TIMEOUT_SECONDS}
    write_json(status_path, status)
    if status['returncode'] != 0 or status['timed_out']:
        raise RuntimeError(f'bounded runtime exercise failed: {label}; see {status_path}')


def build(args):
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise RuntimeError(f'refusing to reuse existing output directory: {output_dir}')
    zig = args.zig.resolve(strict=True)
    archive = args.zig_archive.resolve(strict=True)
    archive_hash = sha256(archive)
    if archive_hash != EXPECTED_ZIG_ARCHIVE_SHA256:
        raise RuntimeError('Zig archive hash differs from the official 0.15.2 index value')
    zig_hash = sha256(zig)
    if zig_hash != args.zig_sha256:
        raise RuntimeError('Zig executable hash differs from the supplied value')
    version = output([str(zig), 'version'])
    if version != '0.15.2':
        raise RuntimeError(f'expected Zig 0.15.2, found {version}')

    compiler_rt = zig.parent / 'lib/compiler_rt/memcpy.zig'
    compiler_rt_common = zig.parent / 'lib/compiler_rt/common.zig'
    if not compiler_rt.is_file() or not compiler_rt_common.is_file():
        raise RuntimeError(f'Zig compiler_rt sources not found under {zig.parent / "lib/compiler_rt"}')
    source_files = [Path(__file__).resolve(), CALLSITE_SOURCE, ROUTE_SOURCE,
                    compiler_rt, compiler_rt_common]
    source_before = {str(path.resolve()): sha256(path) for path in source_files}
    output_dir.mkdir(parents=True)
    prebuild = {
        'target': TARGET,
        'zig_version': version,
        'zig_executable': str(zig),
        'zig_executable_sha256': zig_hash,
        'zig_archive': str(archive),
        'zig_archive_sha256': archive_hash,
        'compiler_rt_memcpy_source': str(compiler_rt),
        'compiler_rt_common_source': str(compiler_rt_common),
        'source_sha256_before_build': source_before,
    }
    write_json(output_dir / 'prebuild.json', prebuild)

    compile_dir = output_dir / 'objects'
    compile_dir.mkdir()
    commands_log = output_dir / 'commands.log'
    caller_obj = compile_dir / 'memcpy_probe_callsite.o'
    compiler_rt_obj = compile_dir / 'zig_compiler_rt_memcpy.o'
    route_obj = compile_dir / 'memcpy_route.o'
    common = [str(zig), 'cc', '-target', TARGET, '-O2', '-fPIC',
              '-fno-builtin-memcpy', '-fno-lto']
    run([*common, '-c', CALLSITE_SOURCE, '-o', caller_obj], cwd=ROOT,
        log=commands_log)
    run([str(zig), 'build-obj', compiler_rt, '-target', TARGET,
         '-O', 'ReleaseFast', '-femit-bin=' + str(compiler_rt_obj)],
        cwd=ROOT, log=commands_log)
    run([*common, '-fvisibility=hidden', '-c', ROUTE_SOURCE, '-o', route_obj],
        cwd=ROOT, log=commands_log)

    baseline = output_dir / 'libmemcpy-baseline.so'
    candidate = output_dir / 'libmemcpy-candidate.so'
    link_common = [str(zig), 'cc', '-target', TARGET, '-shared', '-Wl,-z,defs']
    run([*link_common, '-Wl,-soname,libmemcpy-baseline.so', '-o', baseline,
         caller_obj, compiler_rt_obj], cwd=ROOT, log=commands_log)
    run([*link_common, '-Wl,-soname,libmemcpy-candidate.so', '-o', candidate,
         caller_obj, compiler_rt_obj, route_obj], cwd=ROOT, log=commands_log)

    artifact_text = {}
    for library in (baseline, candidate):
        label = library.name
        outputs = {
            'symbols': ['readelf', '--wide', '--dyn-syms', '--syms', library],
            'relocations': ['readelf', '--wide', '--relocs', library],
            'versions': ['readelf', '--version-info', library],
            'disassembly': ['objdump', '-drwC', library],
        }
        for suffix, command in outputs.items():
            artifact = output_dir / f'{label}.{suffix}.txt'
            artifact.write_text(output(command) + '\n', encoding='utf-8')
            artifact_text[(label, suffix)] = artifact.read_text(encoding='utf-8')

    baseline_symbols = artifact_text[(baseline.name, 'symbols')]
    candidate_symbols = artifact_text[(candidate.name, 'symbols')]
    candidate_relocations = artifact_text[(candidate.name, 'relocations')]
    route_object_symbols = output(['readelf', '--wide', '--syms', route_obj])
    route_object_relocations = output(['readelf', '--wide', '--relocs', route_obj])
    route_object_disassembly = output(['objdump', '-drwC', route_obj])
    (output_dir / 'memcpy_route.o.symbols.txt').write_text(
        route_object_symbols + '\n', encoding='utf-8')
    (output_dir / 'memcpy_route.o.relocations.txt').write_text(
        route_object_relocations + '\n', encoding='utf-8')
    (output_dir / 'memcpy_route.o.disassembly.txt').write_text(
        route_object_disassembly + '\n', encoding='utf-8')
    baseline_disassembly = artifact_text[(baseline.name, 'disassembly')]
    candidate_disassembly = artifact_text[(candidate.name, 'disassembly')]
    baseline_rows = symbol_rows(baseline_symbols)
    candidate_rows = symbol_rows(candidate_symbols)
    route_rows = symbol_rows(route_object_symbols)
    baseline_memcpy = unique_symbol(baseline_rows, 'memcpy')
    candidate_memcpy = unique_symbol(candidate_rows, 'memcpy')
    candidate_test = unique_symbol(candidate_rows, 'sp_wss_memcpy_test')
    baseline_callsite = unique_symbol(baseline_rows, 'sp_memcpy_probe_callsite')
    candidate_callsite = unique_symbol(candidate_rows, 'sp_memcpy_probe_callsite')
    route_memcpy = unique_symbol(route_rows, 'memcpy')
    route_test = unique_symbol(route_rows, 'sp_wss_memcpy_test')

    if baseline_memcpy['visibility'] != 'HIDDEN' or baseline_memcpy['bind'] not in ('WEAK', 'LOCAL'):
        raise RuntimeError('baseline did not retain the hidden Zig compiler_rt memcpy')
    if (route_memcpy['visibility'] != 'HIDDEN' or route_memcpy['bind'] != 'GLOBAL'
            or route_test['visibility'] != 'DEFAULT' or route_test['bind'] != 'GLOBAL'):
        raise RuntimeError('route object lacks a strong hidden memcpy and exported test alias')
    if route_memcpy['value'] != route_test['value']:
        raise RuntimeError('route object aliases do not share one function body')
    if candidate_memcpy['visibility'] != 'HIDDEN' or candidate_memcpy['bind'] not in ('LOCAL', 'GLOBAL'):
        raise RuntimeError('linked candidate memcpy is not hidden')
    if candidate_memcpy['value'] != candidate_test['value']:
        raise RuntimeError('candidate test alias and memcpy override do not share one body address')
    if not re.search(r'UND\s+memcpy@GLIBC_2\.14', candidate_symbols):
        raise RuntimeError('candidate has no undefined memcpy@GLIBC_2.14 import')
    if 'memcpy@GLIBC_2.14' not in candidate_relocations:
        raise RuntimeError('candidate has no relocation to memcpy@GLIBC_2.14')

    baseline_caller_block = block_at(baseline_disassembly,
                                     baseline_callsite['value'],
                                     baseline_callsite['size'])
    candidate_caller_block = block_at(candidate_disassembly,
                                      candidate_callsite['value'],
                                      candidate_callsite['size'])
    wrapper_block = block_at(candidate_disassembly, candidate_test['value'],
                              candidate_test['size'])
    baseline_targets = branch_targets(baseline_caller_block)
    candidate_targets = branch_targets(candidate_caller_block)
    wrapper_targets = branch_targets(wrapper_block)
    if baseline_memcpy['value'] not in {address for address, _ in baseline_targets}:
        raise RuntimeError('baseline ordinary caller does not target its local compiler_rt memcpy')
    if candidate_memcpy['value'] not in {address for address, _ in candidate_targets}:
        raise RuntimeError('candidate ordinary caller does not target its strong hidden memcpy')
    if any(address == candidate_memcpy['value'] for address, _ in wrapper_targets):
        raise RuntimeError('candidate wrapper recursively targets its local memcpy symbol')
    libc_plt_addresses = [address for address, label in wrapper_targets
                          if label.startswith('memcpy@plt')]
    if len(libc_plt_addresses) != 1:
        raise RuntimeError('candidate wrapper does not branch exactly once to memcpy PLT')
    libc_plt_block = block_until_next_symbol(candidate_disassembly,
                                             libc_plt_addresses[0])
    if 'memcpy@GLIBC_2.14' not in libc_plt_block:
        raise RuntimeError('memcpy PLT does not resolve through the GLIBC_2.14 relocation')

    baseline_versions = glibc_versions(artifact_text[(baseline.name, 'versions')])
    candidate_versions = glibc_versions(artifact_text[(candidate.name, 'versions')])
    max_target = (2, 17)
    baseline_max = max(baseline_versions, default=(0, 0))
    candidate_max = max(candidate_versions, default=(0, 0))
    if baseline_max > max_target or candidate_max > max_target:
        raise RuntimeError('a probe link exceeds the x86_64-linux-gnu.2.17.0 ABI ceiling')

    exercise_child(baseline, 'sp_memcpy_probe_callsite', output_dir,
                   'baseline-callsite')
    exercise_child(candidate, 'sp_memcpy_probe_callsite', output_dir,
                   'candidate-callsite')
    exercise_child(candidate, 'sp_wss_memcpy_test', output_dir,
                   'candidate-test-alias')

    source_after = {str(path.resolve()): sha256(path) for path in source_files}
    if source_after != source_before:
        raise RuntimeError('a source/compiler_rt input changed during the probe')
    report = {
        'schema': 2,
        'status': 'link_feasible',
        **prebuild,
        'source_sha256_after_build': source_after,
        'source_hashes_unchanged': True,
        'object_sha256': {path.name: sha256(path) for path in
                          (caller_obj, compiler_rt_obj, route_obj)},
        'library_sha256': {path.name: sha256(path) for path in
                           (baseline, candidate)},
        'baseline_glibc_versions': version_text(baseline_versions),
        'candidate_glibc_versions': version_text(candidate_versions),
        'both_probe_links_within_glibc_2_17_ceiling': True,
        'checks': {
            'baseline_has_hidden_zig_compiler_rt_memcpy': True,
            'baseline_callsite_targets_local_compiler_rt_memcpy': True,
            'candidate_has_strong_hidden_memcpy_override': True,
            'candidate_has_undefined_memcpy_glibc_2_14': True,
            'candidate_callsite_targets_override': True,
            'test_alias_and_override_share_symbol_address': True,
            'wrapper_branches_to_versioned_libc_not_itself': True,
            'bounded_runtime_copy_and_canary_checks_pass': True,
            'targeted_glibc_requirement_no_higher_than_2_17': True,
        },
        'branch_evidence': {
            'baseline_callsite': baseline_caller_block,
            'candidate_callsite': candidate_caller_block,
            'candidate_wrapper_body': wrapper_block,
            'candidate_versioned_libc_plt': libc_plt_block,
            'baseline_memcpy_symbol': baseline_memcpy,
            'candidate_memcpy_symbol': candidate_memcpy,
            'candidate_test_alias_symbol': candidate_test,
            'route_object_memcpy_symbol': route_memcpy,
            'route_object_test_alias_symbol': route_test,
        },
        'commands_log': str(commands_log),
        'evidence_files': [f'{baseline.name}.{suffix}.txt' for suffix in
                           ('symbols', 'relocations', 'versions', 'disassembly')]
                          + [f'{candidate.name}.{suffix}.txt' for suffix in
                             ('symbols', 'relocations', 'versions', 'disassembly')]
                          + ['memcpy_route.o.symbols.txt',
                             'memcpy_route.o.relocations.txt',
                             'memcpy_route.o.disassembly.txt'],
    }
    write_json(output_dir / 'manifest.json', report)
    print(json.dumps(report, indent=2))


def main():
    if sys.argv[1:2] == ['--exercise-only']:
        child = argparse.ArgumentParser()
        child.add_argument('--exercise-only', action='store_true')
        child.add_argument('--library', required=True, type=Path)
        child.add_argument('--symbol', required=True)
        args = child.parse_args()
        exercise(args.library, args.symbol)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zig', required=True, type=Path)
    parser.add_argument('--zig-sha256', required=True)
    parser.add_argument('--zig-archive', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        build(args)
    except Exception as error:
        print(f'link probe failed: {error}', file=sys.stderr)
        raise


if __name__ == '__main__':
    main()
