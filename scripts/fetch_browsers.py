#!/usr/bin/env python3
"""Download optional pinned Linux browsers for capture work, inside .deps only."""
import hashlib
import argparse
import json
from pathlib import Path
import sys
import subprocess
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != 'linux':
        raise SystemExit('Use Linux or WSL for these capture binaries.')
    lock = json.loads((ROOT / 'profiles/browsers/releases.json').read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='append', choices=['chrome', 'google_chrome', 'firefox'])
    args = parser.parse_args()
    target = ROOT / '.deps/browsers'
    target.mkdir(parents=True, exist_ok=True)
    for name in args.browser or ('chrome', 'google_chrome', 'firefox'):
        spec = lock[name]
        archive = target / spec['archive']
        if not archive.exists():
            print('Downloading', name, spec['version'], flush=True)
            urllib.request.urlretrieve(spec['url'], archive)
        with archive.open('rb') as data:
            if hashlib.file_digest(data, 'sha256').hexdigest() != spec['sha256']:
                raise SystemExit(f'{name}: archive SHA-256 mismatch; refusing extraction')
        if archive.suffix == '.deb':
            subprocess.run(['dpkg-deb', '--extract', str(archive), str(target / 'google-chrome')], check=True)
        elif archive.suffix == '.zip':
            with zipfile.ZipFile(archive) as pack:
                for entry in pack.infolist():
                    path = (target / entry.filename).resolve()
                    if not path.is_relative_to(target.resolve()):
                        raise ValueError('unsafe browser archive path')
                    pack.extract(entry, target)
                    if (entry.external_attr >> 16) & 0o111:
                        path.chmod(path.stat().st_mode | 0o111)
        else:
            with tarfile.open(archive) as pack:
                pack.extractall(target, filter='data')
        print('Ready:', target / spec['executable'], flush=True)


if __name__ == '__main__':
    main()
