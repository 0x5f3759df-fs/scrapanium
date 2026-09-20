"""The backend installation can be moved; application packaging is separate."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from binding import ROOT, lib

pytestmark = pytest.mark.skipif(b'2.2.3-scrapanium.1' not in lib.sp_backend_version(),
                               reason='requires the optional source build')


def test_manifest_matches_build_inputs_and_installed_files():
    prefix = Path(os.environ['SCRAPANIUM_CURL_DIR'])
    manifest = json.loads((prefix / 'manifest.json').read_text())
    for group, base in [('input_sha256', ROOT), ('files', prefix)]:
        for name, expected in manifest[group].items():
            with (base / name).open('rb') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected, name
    assert 'libssl.so' not in manifest['dynamic_section']
    assert (prefix / 'licenses/LICENSE_BORINGSSL').is_file()
    assert (prefix / 'licenses/LICENSE_LIBIDN2_LGPL3').is_file()
    assert (prefix / 'sources/libidn2-2.3.7.tar.gz').is_file()


def test_relocated_backend_loads_without_original_search_path(tmp_path):
    prefix = Path(os.environ['SCRAPANIUM_CURL_DIR'])
    relocated = tmp_path / 'moved-backend'
    shutil.copytree(prefix / 'lib', relocated / 'lib', symlinks=True)
    shutil.copytree(prefix / 'bin', relocated / 'bin', symlinks=True)
    env = {k: v for k, v in os.environ.items() if k not in ('LD_LIBRARY_PATH', 'LD_PRELOAD')}
    version = subprocess.check_output([str(relocated / 'bin/curl-impersonate'), '--version'], text=True, env=env)
    assert '2.2.3-scrapanium.1' in version
    # A new process ensures an already-loaded library cannot satisfy the probe.
    script = '''
import ctypes, pathlib, sys
prefix = pathlib.Path(sys.argv[1]).resolve()
library = ctypes.CDLL(str(prefix / 'lib/libcurl-impersonate.so'))
library.curl_easy_option_by_name.argtypes = [ctypes.c_char_p]
library.curl_easy_option_by_name.restype = ctypes.c_void_p
assert library.curl_easy_option_by_name(b'SCRAPANIUM_TLS_GREASE_SIGALGS')
paths = {line.split()[-1] for line in pathlib.Path('/proc/self/maps').read_text().splitlines()
         if 'libcurl-impersonate.so' in line}
assert paths and all(pathlib.Path(path).is_relative_to(prefix) for path in paths), paths
print('relocated backend loaded')
'''
    result = subprocess.check_output([sys.executable, '-c', script, str(relocated)], text=True, env=env)
    assert result.strip() == 'relocated backend loaded'
