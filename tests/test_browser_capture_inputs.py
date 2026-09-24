"""Path/provenance guards for the actual-browser capture command."""
from pathlib import Path
import sys
import json

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import capture_browsers


def _fake_root(tmp_path, monkeypatch):
    (tmp_path / 'profiles/browsers').mkdir(parents=True)
    manifest = tmp_path / 'profiles/browsers/releases.json'
    manifest.write_text('{}\n')
    monkeypatch.setattr(capture_browsers, 'ROOT', tmp_path)
    return manifest


def test_external_browser_keeps_optional_default_store_optional(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)

    manifest, browser_root = capture_browsers.resolve_release_inputs()

    assert manifest == (tmp_path / 'profiles/browsers/releases.json').resolve()
    assert browser_root == (tmp_path / '.deps/browsers').resolve()
    assert not browser_root.exists()


def test_cli_external_browser_does_not_require_default_store(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    (root / 'profiles/browsers').mkdir(parents=True)
    (root / 'tests').mkdir()
    for name in ('fingerprint.py', 'browser_lab.py', 'lab.py'):
        (root / 'tests' / name).write_text('# fixture source\n')
    manifest = root / 'profiles/browsers/releases.json'
    manifest.write_text(json.dumps({'chrome': {'executable': 'pinned/chrome'}}) + '\n')
    external_binary = tmp_path / 'external/chrome'
    external_binary.parent.mkdir()
    external_binary.write_bytes(b'test executable placeholder')
    output = tmp_path / 'capture.json'

    monkeypatch.setattr(capture_browsers, 'ROOT', root)
    monkeypatch.setattr(sys, 'argv', [
        'capture_browsers.py', '--chrome', str(external_binary), '--output', str(output)])
    monkeypatch.setattr(capture_browsers.subprocess, 'check_output',
                        lambda *args, **kwargs: 'Google Chrome external')
    monkeypatch.setattr(capture_browsers, 'capture_browser',
                        lambda *args, **kwargs: {
                            'normalized_clienthello': {}, 'detailed_clienthello': {}})

    capture_browsers.main()

    report = json.loads(output.read_text())
    assert report['release_manifest']['browser_root'] == str((root / '.deps/browsers').resolve())
    assert not (root / '.deps/browsers').exists()
    assert 'download' not in report['browsers']['chrome']


def test_capture_inputs_accepts_dated_store_inside_deps(tmp_path, monkeypatch):
    manifest = _fake_root(tmp_path, monkeypatch)
    dated_store = tmp_path / '.deps/browsers/current-2026-09-24'

    resolved_manifest, browser_root = capture_browsers.resolve_release_inputs(
        manifest, dated_store)

    assert resolved_manifest == manifest.resolve()
    assert browser_root == dated_store.resolve()


def test_capture_inputs_rejects_store_outside_deps(tmp_path, monkeypatch):
    manifest = _fake_root(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match='browser root'):
        capture_browsers.resolve_release_inputs(manifest, tmp_path / 'outside')


def test_capture_inputs_rejects_manifest_outside_repository(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    external_manifest = tmp_path.parent / 'external-releases.json'
    external_manifest.write_text('{}\n')

    with pytest.raises(ValueError, match='release manifest'):
        capture_browsers.resolve_release_inputs(external_manifest)
