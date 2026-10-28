import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('u30_installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
spec_cf = importlib.util.spec_from_file_location('u30_cloudflare_setup', ROOT / 'tools/setup_cloudflare.py')
cloudflare = importlib.util.module_from_spec(spec_cf)
spec_cf.loader.exec_module(cloudflare)


def test_identity_comes_from_attached_usb_or_unique_pi(tmp_path):
    bus = tmp_path / 'usb'
    device = bus / '1-1'
    device.mkdir(parents=True)
    (device / 'idVendor').write_text('0f1b')
    (device / 'idProduct').write_text('0301')
    (device / 'serial').write_text('12345678')
    machine = tmp_path / 'machine-id'
    machine.write_text('123456789abcdef')
    assert installer.station_suffix(bus, machine) == '12345678'
    (device / 'idProduct').write_text('9999')
    assert installer.station_suffix(bus, machine) == 'pi-abcdef'


def test_settings_are_unique_and_update_never_resets_password():
    first = installer.settings(suffix='test-one')
    second = installer.settings(suffix='test-two')
    assert first['setup_key'] != second['setup_key'] and first['ap_uuid'] != second['ap_uuid']
    first.update(setup_key='customer-changed-value', home_uuid='saved-home')
    assert installer.settings(first) == first
    with pytest.raises(ValueError):
        installer.settings(first, hostname='other')
    with pytest.raises(ValueError):
        installer.settings(suffix='test', hostname='bad\nname')


def test_profile_has_private_ap_network_without_plaintext_password():
    config = installer.settings(suffix='test')
    profile = installer.hotspot_profile(config)
    assert config['setup_key'] not in profile
    assert '10.42.0.1/24' in profile and 'never-default=true' in profile
    assert 'mode=ap' in profile and 'autoconnect=false' in profile
    assert 'pmf=1\n' in profile


@pytest.fixture
def fake_pi(tmp_path, monkeypatch):
    """Run the full installer against a disposable filesystem, never host services."""
    real_path = Path
    # WindowsPath('/etc') resolves to a drive-rooted path; map components directly.
    def system_path(*parts):
        if parts and isinstance(parts[0], str) and parts[0].startswith('/'):
            return tmp_path.joinpath(parts[0].lstrip('/'), *parts[1:])
        return real_path(*parts)
    monkeypatch.setattr(installer, 'Path', system_path)
    monkeypatch.setattr(installer, 'APP', tmp_path / 'opt/hobo-u30')
    monkeypatch.setattr(installer, 'DATA', tmp_path / 'var/lib/hobo-u30')
    monkeypatch.setattr(installer, 'CONFIG', tmp_path / 'etc/u30-network/config.json')
    monkeypatch.setattr(installer, 'station_suffix', lambda: 'test-device')
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(installer.os, 'chown', lambda *args: None, raising=False)
    account = SimpleNamespace(pw_uid=995, pw_gid=995, pw_shell='/usr/sbin/nologin')
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda name: account))
    monkeypatch.setitem(sys.modules, 'grp', SimpleNamespace(getgrnam=lambda name: object()))
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(installer.subprocess, 'run', run)
    monkeypatch.setattr(installer, 'wait_health', lambda: None)
    for name in ('etc/avahi', 'etc/systemd/system', 'etc/udev/rules.d',
                 'etc/NetworkManager/system-connections', 'sys/class/net/wlan0', 'sys/bus/usb/devices'):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / 'etc/os-release').write_text('ID=debian\nVERSION_ID="13"\n')
    (tmp_path / 'etc/avahi/avahi-daemon.conf').write_text('[server]\n#host-name=raspberrypi\n')
    args = SimpleNamespace(hostname=None, timezone='Asia/Taipei', no_weekly_reboot=False,
                           skip_packages=False, dry_run=False)
    return tmp_path, args, calls


def test_fresh_install_and_update_preserve_customer_data_credentials_and_policy(fake_pi, capsys):
    root, args, calls = fake_pi
    installer.install(args)
    config_path = root / 'etc/u30-network/config.json'
    profile_path = root / 'etc/NetworkManager/system-connections/u30-setup.nmconnection'
    config = json.loads(config_path.read_text())
    first_profile = profile_path.read_bytes()
    assert config['setup_key'] not in capsys.readouterr().out
    assert (root / 'opt/hobo-u30/run_server.py').exists()
    with sqlite3.connect(root / 'var/lib/hobo-u30/station.sqlite3') as db:
        policy = json.loads(db.execute("SELECT value FROM state WHERE key='native_control'").fetchone()[0])
        assert policy['enabled'] is False
        db.execute("UPDATE state SET value=? WHERE key='native_control'", (json.dumps({'enabled': True}),))
        db.execute("INSERT INTO state VALUES ('customer-test-marker','retained')")
    # Treat a later customer-changed credential as the source of truth.
    config['setup_key'] = 'customer-password-kept'
    config['home_uuid'] = 'customer-wifi-kept'
    config_path.write_text(json.dumps(config))
    installer.install(args)
    assert json.loads(config_path.read_text()) == config
    assert profile_path.read_bytes() == first_profile
    with sqlite3.connect(root / 'var/lib/hobo-u30/station.sqlite3') as db:
        assert db.execute("SELECT value FROM state WHERE key='customer-test-marker'").fetchone()[0] == 'retained'
        assert json.loads(db.execute("SELECT value FROM state WHERE key='native_control'").fetchone()[0])['enabled']
    assert list((root / 'opt').glob('hobo-u30.previous-*'))
    assert not any('u30-weekly-reboot.service' in call and 'start' in call for call in calls)
    assert any(call[:3] == ('systemctl', 'enable', '--now') or call[:3] == ['systemctl', 'enable', '--now'] for call in calls)


def test_dry_run_does_not_create_data_or_credentials(fake_pi):
    root, args, calls = fake_pi
    args.dry_run = True
    installer.install(args)
    assert not (root / 'opt/hobo-u30').exists()
    assert not (root / 'etc/u30-network/config.json').exists()
    assert not any('apt-get' in call for call in calls)


@pytest.mark.parametrize('old_pmf', [None, '0', '2', '3'])
@pytest.mark.parametrize('active_setup_ap', [True, False])
def test_update_makes_legacy_hotspot_wpa2_only_without_resetting_credentials(fake_pi, monkeypatch, old_pmf, active_setup_ap):
    import configparser
    root, args, calls = fake_pi
    installer.install(args)
    config_path = root / 'etc/u30-network/config.json'
    profile_path = root / 'etc/NetworkManager/system-connections/u30-setup.nmconnection'
    card_path = root / 'etc/u30-network/connection.txt'
    card_before = card_path.read_bytes()
    config = json.loads(config_path.read_text())
    config.update(setup_key='changed-customer-key', home_uuid='saved-home-network')
    config_path.write_text(json.dumps(config))
    legacy = installer.hotspot_profile(config).replace('pmf=1\n', '' if old_pmf is None else f'pmf={old_pmf}\n')
    profile_path.write_text(legacy)
    before = configparser.ConfigParser(interpolation=None)
    before.read_string(legacy)
    previous_run = installer.subprocess.run
    def active_connection(args, **kwargs):
        if args == ['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', 'wlan0']:
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=config['ap_uuid'] if active_setup_ap else 'customer-home-uplink')
        return previous_run(args, **kwargs)
    monkeypatch.setattr(installer.subprocess, 'run', active_connection)

    installer.install(args)

    after = configparser.ConfigParser(interpolation=None)
    after.read(profile_path)
    assert after.get('wifi-security', 'pmf') == '1'
    for section in before.sections():
        for key, value in before.items(section):
            if (section, key) != ('wifi-security', 'pmf'):
                assert after.get(section, key) == value
    assert json.loads(config_path.read_text()) == config
    assert card_path.read_bytes() == card_before
    reconnect = ('nmcli', '--wait', '20', 'connection', 'up', 'uuid', config['ap_uuid'], 'ifname', 'wlan0')
    assert (reconnect in calls) == active_setup_ap


def test_legacy_deployment_is_rejected_before_modifying_it(fake_pi):
    root, args, calls = fake_pi
    service = root / 'etc/systemd/system/hobo-u30-server.service'
    service.write_text('WorkingDirectory=/home/old-user/custom-setup\n')
    with pytest.raises(ValueError, match='older custom deployment'):
        installer.install(args)
    assert service.read_text() == 'WorkingDirectory=/home/old-user/custom-setup\n'
    assert not (root / 'var/lib/hobo-u30').exists()


def test_install_never_follows_a_replaced_data_directory(fake_pi, monkeypatch):
    root, args, calls = fake_pi
    original = type(installer.DATA).is_symlink
    monkeypatch.setattr(type(installer.DATA), 'is_symlink', lambda path: path == installer.DATA or original(path))
    with pytest.raises(ValueError, match='symbolic links'):
        installer.install(args)
    assert not any('apt-get' in call for call in calls)


def test_cloudflare_token_accepts_own_tunnel_and_rejects_uuid_or_full_command():
    import base64
    import uuid
    token = base64.urlsafe_b64encode(json.dumps({'a': 'test-account', 't': str(uuid.uuid4()), 's': 'test-only-secret'}).encode()).decode().rstrip('=')
    assert cloudflare.validate_token(token) == token
    for invalid in ('invalid', str(uuid.uuid4()), 'cloudflared service install ' + token):
        with pytest.raises(ValueError):
            cloudflare.validate_token(invalid)
