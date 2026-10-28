import configparser
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'network'))
import manager

OLD = 'test-only-original'
NEW = 'test-only-replacement'


def setup(tmp_path):
    config = {'hostname': 'u30-test', 'setup_ssid': 'U30-test', 'setup_key': OLD,
              'ap_uuid': 'ap-id', 'home_uuid': 'home-id'}
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    profile = ('[connection]\nuuid=ap-id\ntype=wifi\n[wifi]\nmode=ap\nssid=U30-test\n'
               '[wifi-security]\nkey-mgmt=wpa-psk\npsk='+OLD+'\n[ipv4]\nmethod=manual\naddress1=10.42.0.1/24\n')
    (tmp_path / 'u30-setup.nmconnection').write_text(profile, encoding='utf-8')
    service = manager.Manager(config, config_path)
    service.device = Mock(return_value={'connected': False, 'uuid': ''})
    service.ensure_ap = Mock()
    return service


def payload(**changes):
    return {'setup_key': OLD, 'new_password': NEW, 'confirm_password': NEW,
            'confirmed': True, **changes}


def psk(password=NEW):
    return hashlib.pbkdf2_hmac('sha1', password.encode('ascii'), b'U30-test', 4096, 32).hex()


def success_command(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout=psk()+'\n' if '--show-secrets' in args else '')


@pytest.mark.parametrize('changes,status', [
    ({'setup_key': 'wrong'}, 403), ({'new_password': 'short'}, 400),
    ({'new_password': 'x'*64}, 400), ({'new_password': '中文密碼12345678'}, 400),
    ({'new_password': 'abcdefg\n'}, 400), ({'confirm_password': 'different'}, 400),
    ({'new_password': OLD, 'confirm_password': OLD}, 400), ({'confirmed': False}, 400),
    ({'new_password': None}, 400),
])
def test_invalid_request_never_queues_or_exposes_password(tmp_path, changes, status):
    service = setup(tmp_path)
    before = service.config_path.read_bytes()
    result, actual = service.submit_password(payload(**changes))
    assert actual == status and service.pending.empty() and not service.busy
    assert service.config_path.read_bytes() == before
    assert OLD not in json.dumps(result) and NEW not in json.dumps(service.status())


def test_password_and_wifi_changes_are_mutually_exclusive(tmp_path):
    service = setup(tmp_path)
    assert service.submit_password(payload())[1] == 202
    assert service.submit_password(payload())[1] == 409
    assert service.submit({'setup_key': OLD, 'ssid': 'Home', 'password': 'wifi-test-only'})[1] == 409
    assert service.pending.get_nowait() == ('password', NEW)
    assert OLD not in json.dumps(service.status()) and NEW not in json.dumps(service.status())


def test_commit_updates_portal_and_hotspot_without_touching_home_wifi(tmp_path):
    service = setup(tmp_path)
    with patch.object(manager, 'PROFILES', tmp_path), patch.object(manager, 'command', side_effect=success_command) as command:
        service.change_password(NEW)
    assert not service.authorized({'setup_key': OLD})
    assert service.authorized({'setup_key': NEW})
    assert json.loads(service.config_path.read_text())['setup_key'] == NEW
    profile = configparser.ConfigParser(interpolation=None)
    profile.read(tmp_path / 'u30-setup.nmconnection')
    assert profile['wifi-security']['psk'] == psk()
    assert profile['ipv4']['address1'] == '10.42.0.1/24'
    assert service.config['home_uuid'] == 'home-id'
    assert not service.password_journal.exists()
    assert service.status()['job']['state'] == 'success'
    assert all(NEW not in str(c.args) and OLD not in str(c.args) and psk() not in str(c.args) for c in command.call_args_list)
    assert NEW not in json.dumps(service.status()) and psk() not in json.dumps(service.status())


@pytest.mark.parametrize('failure', ['load', 'readback', 'config_write', 'commit_marker'])
def test_failed_change_restores_both_files_and_keeps_old_auth(tmp_path, failure):
    service = setup(tmp_path)
    original_config = service.config_path.read_text()
    profile_path = tmp_path / 'u30-setup.nmconnection'
    original_profile = profile_path.read_text()
    load_calls = []
    def command(*args, **kwargs):
        if 'load' in args:
            load_calls.append(args)
            if failure == 'load' and len(load_calls) == 1:
                return SimpleNamespace(returncode=1, stdout='')
        if '--show-secrets' in args and failure == 'readback':
            return SimpleNamespace(returncode=0, stdout='wrong-readback')
        return success_command(*args, **kwargs)
    private_write = manager.private_write
    def write(path, text):
        if failure == 'config_write' and path == service.config_path and NEW in text:
            raise OSError('simulated write failure')
        if failure == 'commit_marker' and path == service.password_journal and json.loads(text)['phase'] == 'committed':
            raise OSError('simulated commit failure')
        private_write(path, text)
    with patch.object(manager, 'PROFILES', tmp_path), patch.object(manager, 'command', side_effect=command), patch.object(manager, 'private_write', side_effect=write):
        service.change_password(NEW)
    assert service.authorized({'setup_key': OLD}) and not service.authorized({'setup_key': NEW})
    assert service.config_path.read_text() == original_config
    assert profile_path.read_text() == original_profile
    assert service.status()['job']['state'] == 'failed' and not service.password_journal.exists()


def test_startup_recovers_interrupted_uncommitted_change(tmp_path):
    service = setup(tmp_path)
    profile_path = tmp_path / 'u30-setup.nmconnection'
    journal = {'phase': 'prepared', 'config': service.config_path.read_text(), 'profile': profile_path.read_text()}
    service.password_journal.write_text(json.dumps(journal))
    profile_path.write_text('interrupted profile')
    changed = {**service.config, 'setup_key': NEW}
    service.config_path.write_text(json.dumps(changed))
    service = manager.Manager(changed, service.config_path)
    with patch.object(manager, 'PROFILES', tmp_path), patch.object(manager, 'command', return_value=SimpleNamespace(returncode=0)):
        service.recover_password_change()
    assert service.authorized({'setup_key': OLD}) and not service.authorized({'setup_key': NEW})
    assert profile_path.read_text() == journal['profile'] and not service.password_journal.exists()


def test_committed_journal_cleanup_never_reverts_new_password(tmp_path):
    service = setup(tmp_path)
    old_config = service.config_path.read_text()
    new_config = {**service.config, 'setup_key': NEW}
    service.config_path.write_text(json.dumps(new_config))
    service.password_journal.write_text(json.dumps({'phase': 'committed', 'config': old_config, 'profile': 'old profile'}))
    with patch.object(manager, 'command') as command:
        service.recover_password_change()
    assert service.authorized({'setup_key': NEW}) and not service.password_journal.exists()
    command.assert_not_called()


def test_ap_restarts_only_if_active_and_restart_failure_still_reports_new_key(tmp_path):
    service = setup(tmp_path)
    service.device.return_value = {'connected': True, 'uuid': 'ap-id'}
    service.ensure_ap.side_effect = RuntimeError('AP start failed')
    with patch.object(manager, 'PROFILES', tmp_path), patch.object(manager, 'command', side_effect=success_command) as command:
        service.change_password(NEW)
    assert service.authorized({'setup_key': NEW})
    assert service.status()['job']['state'] == 'success'
    assert any('down' in call.args and 'ap-id' in call.args for call in command.call_args_list)
    service.ensure_ap.assert_called_once()


def test_home_wifi_is_never_disconnected_for_password_change(tmp_path):
    service = setup(tmp_path)
    service.device.return_value = {'connected': True, 'uuid': 'home-id'}
    with patch.object(manager, 'PROFILES', tmp_path), patch.object(manager, 'command', side_effect=success_command) as command:
        service.change_password(NEW)
    service.ensure_ap.assert_not_called()
    assert all('down' not in call.args for call in command.call_args_list)


def test_nm_byte_array_ssid_and_password_symbols_are_preserved(tmp_path):
    service = setup(tmp_path)
    profile_path = tmp_path / 'u30-setup.nmconnection'
    encoded = ';'.join(str(b) for b in b'U30-test')+';'
    profile_path.write_text(profile_path.read_text().replace('ssid=U30-test', 'ssid='+encoded))
    password = ' spaces\\$`"#;%=123 '
    def command(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=psk(password) if '--show-secrets' in args else '')
    assert service.submit_password(payload(new_password=password, confirm_password=password))[1] == 202
    with patch.object(manager, 'PROFILES', tmp_path), patch.object(manager, 'command', side_effect=command):
        service.change_password(password)
    assert service.authorized({'setup_key': password})
    profile = configparser.ConfigParser(interpolation=None)
    profile.read(profile_path)
    assert profile['wifi']['ssid'] == encoded and profile['wifi-security']['psk'] == psk(password)


def test_wrong_setup_ssid_never_changes_either_file(tmp_path):
    service = setup(tmp_path)
    profile = tmp_path / 'u30-setup.nmconnection'
    profile.write_text(profile.read_text().replace('ssid=U30-test', 'ssid=Other'))
    before = profile.read_bytes()
    with patch.object(manager, 'PROFILES', tmp_path), patch.object(manager, 'command') as command:
        service.change_password(NEW)
    command.assert_not_called()
    assert profile.read_bytes() == before and service.authorized({'setup_key': OLD})


def test_password_http_bridge_checks_origin_and_does_not_echo_secrets(http_app, monkeypatch):
    import web_server
    app, request = http_app
    calls = []
    def bridge(action, data):
        calls.append((action, deepcopy(data)))
        return {'accepted': True}, 202
    monkeypatch.setattr(web_server, 'network_request', bridge)
    assert request('/api/network/password', payload(), origin='http://evil.invalid')[0] == 403
    assert not calls
    status, result = request('/api/network/password', payload())
    assert status == 202 and calls == [('password', payload())]
    assert NEW not in json.dumps(result) and OLD not in json.dumps(result)


from test_history_tools import http_app
