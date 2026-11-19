import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'network'))
import manager

spec = importlib.util.spec_from_file_location('u30_update', ROOT / 'scripts/update.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def release_zip(version='1.0.3', extra=None):
    archive = io.BytesIO()
    with ZipFile(archive, 'w') as package:
        package.writestr('hobo-u30-main/VERSION', version + '\n')
        package.writestr('hobo-u30-main/scripts/install.py', '# package installer\n')
        if extra:
            package.writestr(*extra)
    archive.seek(0)
    return archive


def github_response(data, version='1.0.3', prerelease=False):
    return json.dumps({'draft': False, 'prerelease': prerelease, 'tag_name': 'v' + version,
                       'assets': [{'name': update.ASSET, 'state': 'uploaded', 'id': 42, 'size': len(data),
                                   'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}]}).encode()


def test_new_release_accepts_only_verified_asset_and_matching_version(tmp_path, monkeypatch):
    data = release_zip().getvalue()
    calls = []

    def request(url, token, accept, limit, **kwargs):
        calls.append((url, accept))
        return github_response(data) if url.endswith('/latest') else data

    monkeypatch.setattr(update, 'request', request)
    assert update.download_release('token', '1.0.3') is None
    assert update.download_release('token', '1.0.4') is None
    assert all(url.endswith('/latest') for url, _ in calls)
    target, content = update.download_release('token', '1.0.2')
    assert calls[-1] == (update.API + '/assets/42', 'application/octet-stream')
    assert update.unpack(io.BytesIO(content), tmp_path, target).name == 'hobo-u30-main'
    with pytest.raises(ValueError, match='標籤不符'):
        update.unpack(release_zip('1.0.5'), tmp_path / 'mismatch', target)


def test_corrupt_download_and_prerelease_are_rejected(monkeypatch):
    data = release_zip().getvalue()
    monkeypatch.setattr(update, 'request', lambda url, *args, **kwargs:
                        github_response(data) if url.endswith('/latest') else b'x' * len(data))
    with pytest.raises(ValueError, match='驗證失敗'):
        update.download_release('token', '1.0.2')
    monkeypatch.setattr(update, 'request', lambda *args, **kwargs: github_response(data, prerelease=True))
    with pytest.raises(ValueError, match='正式發布'):
        update.download_release('token', '1.0.2')


def test_unpack_rejects_escape_and_symlink(tmp_path):
    symlink = ZipInfo('hobo-u30-main/link')
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    for entry in ('hobo-u30-main/../../escape', symlink):
        with pytest.raises(ValueError, match='無效的檔案路徑'):
            update.unpack(release_zip(extra=(entry, 'test')), tmp_path / str(entry is symlink), '1.0.3')
    assert not (tmp_path / 'escape').exists()


def test_asset_redirect_does_not_forward_credential(monkeypatch):
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise update.HTTPError(request.full_url, 302, '', {'Location': target}, None)
        return io.BytesIO(b'zip')

    monkeypatch.setattr(update, 'build_opener', lambda *args: SimpleNamespace(open=open_request))
    target = 'https://release-assets.githubusercontent.com/signed'
    assert update.request(update.API + '/assets/42', 'token', 'application/octet-stream', 3, download=True) == b'zip'
    assert calls[0].get_header('Authorization') == 'Bearer token'
    assert calls[1].get_header('Authorization') is None
    calls.clear()
    target = 'https://other.invalid/redirect'
    with pytest.raises(ValueError):
        update.request(update.API + '/assets/42', 'token', 'application/octet-stream', 3, download=True)
    assert len(calls) == 1


@pytest.mark.parametrize('scenario', ['current', 'failed', 'success'])
def test_reboot_only_follows_successful_install(tmp_path, monkeypatch, scenario):
    installed = tmp_path / 'app'
    installed.mkdir()
    (installed / 'VERSION').write_text('1.0.2')
    data = release_zip().getvalue()
    states, reboot = [], []
    monkeypatch.setattr(update, 'APP', installed)
    monkeypatch.setattr(update, 'read_token', lambda: 'token')
    monkeypatch.setattr(update, 'write_state', lambda state, **fields: states.append(state))
    monkeypatch.setattr(update, 'download_release', lambda *args: None if scenario == 'current' else ('1.0.3', data))
    monkeypatch.setattr(update.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(update.subprocess, 'run', lambda args, **kwargs: reboot.append(args))

    def install(source):
        assert (source / 'VERSION').read_text().strip() == '1.0.3'
        if scenario == 'failed':
            raise subprocess.CalledProcessError(1, 'install')

    monkeypatch.setattr(update, 'install_release', install)
    if scenario == 'failed':
        with pytest.raises(SystemExit):
            update.main()
    else:
        update.main()
    assert states == {
        'current': ['checking', 'current'], 'failed': ['checking', 'installing', 'failed'],
        'success': ['checking', 'installing', 'rebooting'],
    }[scenario]
    assert reboot == ([[update.SYSTEMCTL, '--no-block', 'reboot']] if scenario == 'success' else [])


def test_manager_authorizes_and_queues_at_most_one_update(tmp_path, monkeypatch):
    installed = tmp_path / 'VERSION'
    installed.write_text('1.0.2')
    boot = tmp_path / 'boot_id'
    boot.write_text('boot-before')
    (tmp_path / 'github-release-token').write_text('test-token')
    monkeypatch.setattr(manager, 'UPDATE_VERSION', installed)
    monkeypatch.setattr(manager, 'BOOT_ID', boot)
    calls = []
    monkeypatch.setattr(manager, 'command', lambda *args, **kwargs:
                        calls.append(args) or SimpleNamespace(returncode=0, stdout=''))
    service = manager.Manager({'hostname': 'test', 'setup_ssid': 'U30-TEST', 'setup_key': 'setup-key'},
                              tmp_path / 'config.json')
    assert service.submit_software_update({'setup_key': 'bad'})[1] == 403
    assert service.submit_software_update({'setup_key': 'setup-key'})[1] == 202
    assert service.submit_software_update({'setup_key': 'setup-key'})[1] == 409
    assert calls == [('/usr/bin/systemctl', 'start', '--no-block', manager.UPDATE_UNIT)]
    service.software_state.write_text(json.dumps({'state': 'rebooting', 'boot_id': 'boot-before', 'version': '1.0.3'}))
    boot.write_text('boot-after')
    installed.write_text('1.0.3')
    assert service.status()['software_update']['state'] == 'success'
