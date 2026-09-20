#!/usr/bin/env python3
"""Build the optional browser backend from pinned sources, without root.

Requires Linux x86_64, Python >=3.12, clang/clang++, CMake, Ninja, make,
git, patch, Perl, Go and standard C development tools. Builds in a separate
directory and refuses to reuse it when source inputs have changed.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def output(*args):
    return subprocess.check_output([str(arg) for arg in args], text=True).strip()


def build(work, prefix, jobs):
    lock = json.loads((ROOT / 'backend/lock.json').read_text())
    inputs = {str(p.relative_to(ROOT)): sha256(p) for p in [
        ROOT / 'backend/lock.json', Path(__file__).resolve(),
        *sorted((ROOT / 'backend/patches').glob('*.patch'))]}
    identity = {'inputs': inputs, 'prefix': str(prefix),
                'cc': shutil.which(os.environ.get('CC', 'clang')),
                'cxx': shutil.which(os.environ.get('CXX', 'clang++'))}
    if not identity['cc'] or not identity['cxx']:
        raise RuntimeError('clang and clang++ are required')
    stamp = work / 'inputs.json'
    if stamp.exists():
        if json.loads(stamp.read_text()) != identity:
            raise RuntimeError('Build inputs changed; choose a fresh --work and --prefix directory.')
    elif work.exists() and any(work.iterdir()):
        raise RuntimeError('Refusing an unrecognized nonempty build directory.')
    else:
        if prefix.exists() and any(prefix.iterdir()):
            raise RuntimeError('Refusing an unrecognized nonempty installation directory.')
        work.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps(identity, indent=2) + '\n')
    source, binary = work / 'source', work / 'build'
    env = {**os.environ, 'CC': identity['cc'], 'CXX': identity['cxx']}
    for name in ('CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'LIBRARY_PATH',
                 'PKG_CONFIG_PATH', 'CFLAGS', 'CXXFLAGS', 'LDFLAGS'):
        env.pop(name, None)
    if not source.exists():
        run('git', 'init', source)
        run('git', '-C', source, 'remote', 'add', 'origin', lock['upstream']['repository'])
        run('git', '-C', source, 'fetch', '--depth', '1', 'origin', lock['upstream']['commit'])
        run('git', '-C', source, 'checkout', '--detach', 'FETCH_HEAD')
    if output('git', '-C', source, 'rev-parse', 'HEAD') != lock['upstream']['commit']:
        raise RuntimeError('Unexpected upstream commit')
    # Reconstruct the two modified source files from the pinned Git objects;
    # never append the patch twice on a resumed build.
    cmake_original = subprocess.check_output(['git', '-C', str(source), 'show', 'HEAD:CMakeLists.txt'])
    patch_original = subprocess.check_output(['git', '-C', str(source), 'show', 'HEAD:patches/curl.patch'])
    cmake_path = source / 'CMakeLists.txt'
    with (work / 'CMakeLists.txt').open('wb') as stream:
        stream.write(cmake_original)
    run('patch', '--batch', '--fuzz=0', work / 'CMakeLists.txt',
        ROOT / 'backend/patches/cmake-isolate-dependencies.patch')
    desired = {cmake_path: (work / 'CMakeLists.txt').read_bytes(),
               source / 'patches/curl.patch': patch_original + b'\n' +
                   (ROOT / 'backend/patches/curl-browser-controls.patch').read_bytes()}
    dirty = output('git', '-C', source, 'diff', '--name-only').splitlines()
    if set(dirty) - {'CMakeLists.txt', 'patches/curl.patch'}:
        raise RuntimeError('Unexpected edits in the backend checkout')
    for path, contents in desired.items():
        if path.read_bytes() != contents:
            path.write_bytes(contents)
    spec = lock['libidn2']
    archive = binary / 'deps/downloads' / f"libidn2-{spec['version']}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        temporary = archive.with_suffix('.download')
        urllib.request.urlretrieve(spec['url'], temporary)
        if sha256(temporary) != spec['sha256']:
            raise RuntimeError('libidn2 download SHA-256 mismatch')
        temporary.replace(archive)
    if sha256(archive) != spec['sha256']:
        raise RuntimeError('libidn2 SHA-256 mismatch')
    run('make', 'prepare-libidn2', f'BUILD_DIR={binary}', f'JOBS={jobs}',
        f"LIBIDN2_VERSION={spec['version']}", cwd=source, env=env)
    configure = ['cmake', '-S', source, '-B', binary, '-GNinja',
        f'-DCMAKE_INSTALL_PREFIX={prefix}', '-DCMAKE_INSTALL_LIBDIR=lib',
        f'-DCMAKE_C_COMPILER={identity["cc"]}', f'-DCMAKE_CXX_COMPILER={identity["cxx"]}',
        f'-DSUBJOBS={jobs}', f'-DCURL_IMPERSONATE_VERSION={lock["version"]}',
        '-DCURL_SUBPROJECT_FORCE_OPENSSL_PATHS=ON', '-DUSE_LIBIDN2=ON']
    run(*configure, env=env)
    # Each ExternalProject already uses jobs internally. Avoid jobs squared.
    run('cmake', '--build', binary, '--parallel', '1', env=env)
    run('cmake', '--install', binary, env=env)
    run('cmake', '-P', binary / 'CollectLicenses.cmake', env=env)
    shutil.copytree(binary / 'licenses', prefix / 'licenses', dirs_exist_ok=True)
    shutil.copy2(ROOT / 'LICENSE', prefix / 'licenses/LICENSE_SCRAPANIUM')
    # Keep the matching source/patch recipe alongside the redistributable
    # library, including the LGPL IDN dependency's complete source archive.
    source_bundle = prefix / 'sources'
    source_bundle.mkdir(exist_ok=True)
    shutil.copy2(archive, source_bundle / archive.name)
    shutil.copytree(ROOT / 'backend', source_bundle / 'backend', dirs_exist_ok=True)
    shutil.copy2(__file__, source_bundle / 'build_backend.py')
    library = prefix / 'lib/libcurl-impersonate.so'
    api = ctypes.CDLL(str(library))
    api.curl_easy_option_by_name.argtypes = [ctypes.c_char_p]
    api.curl_easy_option_by_name.restype = ctypes.c_void_p
    for name in ('TLS_GREASE_SIGALGS', 'TLS_SERVER_PADDING', 'HTTP2_STREAM_WINDOW', 'HTTP2_INITIAL_STREAM_ID'):
        if not api.curl_easy_option_by_name(('SCRAPANIUM_' + name).encode()):
            raise RuntimeError('Built library is missing ' + name)
    dynamic = output('readelf', '-d', library)
    if any(name in dynamic for name in ('libssl.so', 'libcrypto.so', 'libcurl.so', 'libidn2.so')):
        raise RuntimeError('Backend unexpectedly links a system transport dependency')
    manifest = {'schema': 1, 'lock': lock, 'input_sha256': inputs,
        'compiler': output(identity['cc'], '--version'),
        'cmake': output('cmake', '--version'), 'platform': platform.platform(),
        'configure': [str(arg) for arg in configure],
        'version_output': output(prefix / 'bin/curl-impersonate', '--version'),
        'dynamic_section': dynamic,
        'files': {str(p.relative_to(prefix)): sha256(p) for p in sorted(prefix.rglob('*'))
                  if p.is_file() and p.name != 'manifest.json'}}
    (prefix / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if inputs != {name: sha256(ROOT / name) for name in inputs}:
        raise RuntimeError('Build inputs changed during compilation; discard this result')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=Path.home() / '.cache/scrapanium-browser-build')
    parser.add_argument('--prefix', type=Path, default=ROOT / '.deps/curl-browser')
    parser.add_argument('--jobs', type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument('--archive', type=Path, help='also write an installation tarball, with licenses and manifest')
    args = parser.parse_args()
    if sys.platform != 'linux' or platform.machine() != 'x86_64' or args.jobs < 1:
        parser.error('requires Linux x86_64 and positive --jobs')
    work, prefix = args.work.resolve(), args.prefix.resolve()
    if prefix == work or prefix.is_relative_to(work) or work.is_relative_to(prefix):
        parser.error('--work and --prefix must be separate directories')
    build(work, prefix, args.jobs)
    if args.archive:
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(args.archive, 'w:gz') as archive:
            archive.add(prefix, arcname='curl-browser')
        print('Archive SHA-256:', sha256(args.archive))
    print('Ready:', prefix)
    print(f'SCRAPANIUM_CURL_DIR={prefix} python3 scripts/build.py')


if __name__ == '__main__':
    main()
