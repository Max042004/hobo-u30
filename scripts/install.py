"""Portable Raspberry Pi OS installer. Never opens HID or writes to U30."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

SOURCE = Path(__file__).resolve().parents[1]
APP = Path('/opt/hobo-u30')
DATA = Path('/var/lib/hobo-u30')
CONFIG = Path('/etc/u30-network/config.json')
ACCOUNT = 'hobo-u30'
NETWORK_GROUP = 'u30-network'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def private_write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.install-new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def station_suffix(sysfs=Path('/sys/bus/usb/devices'), machine_id=Path('/etc/machine-id')):
    attached = []
    for device in sysfs.glob('*'):
        try:
            if (device / 'idVendor').read_text().strip() == '0f1b' and (device / 'idProduct').read_text().strip() == '0301':
                serial = (device / 'serial').read_text().strip()
                if re.fullmatch(r'[0-9]{4,20}', serial):
                    attached.append(serial)
        except OSError:
            pass
    if len(attached) > 1:
        raise ValueError('Connect only one U30 for this single-station installation')
    if attached:
        return attached[0]
    identifier = machine_id.read_text().strip() if machine_id.exists() else uuid.uuid4().hex
    return 'pi-' + identifier[-6:].lower()


def settings(existing=None, suffix=None, hostname=None):
    if existing:
        if hostname and hostname != existing['hostname']:
            raise ValueError('An installed station already has a hostname; credentials and identity are preserved on update')
        return dict(existing)
    suffix = suffix or station_suffix()
    name = hostname or 'u30-' + suffix
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
        raise ValueError('Hostname must be a valid lowercase DNS label (1–63 characters)')
    return {'hostname': name, 'setup_ssid': ('U30-' + suffix)[:32],
            'setup_key': secrets.token_urlsafe(12), 'ap_uuid': str(uuid.uuid4())}


def hotspot_profile(config):
    import hashlib
    ssid = ';'.join(str(value) for value in config['setup_ssid'].encode('utf-8')) + ';'
    key = config['setup_key']
    psk = key if re.fullmatch(r'[0-9a-fA-F]{64}', key) else hashlib.pbkdf2_hmac(
        'sha1', key.encode('ascii'), config['setup_ssid'].encode('utf-8'), 4096, 32).hex()
    return f'''[connection]
id=u30-setup
uuid={config['ap_uuid']}
type=wifi
interface-name=wlan0
autoconnect=false
[wifi]
mode=ap
band=bg
channel=1
ssid={ssid}
[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
pairwise=ccmp;
group=ccmp;
pmf=1
psk={psk}
[ipv4]
method=manual
address1=10.42.0.1/24
never-default=true
[ipv6]
method=disabled
'''


def runtime_copy(source, destination):
    for name in ('src', 'web', 'vendor', 'network', 'scripts', 'tools', 'docs'):
        shutil.copytree(source / name, destination / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'), dirs_exist_ok=True)
    for name in ('run_server.py', 'install.sh', 'VERSION', 'README.md', 'THIRD_PARTY_NOTICES.md',
                 'hobo-u30-server.service', '99-hobo-u30.rules', 'u30-weekly-reboot.service', 'u30-weekly-reboot.timer'):
        shutil.copyfile(source / name, destination / name)
    for file in destination.rglob('*'):
        file.chmod(0o755 if file.is_dir() or file.suffix == '.sh' else 0o644)
    destination.chmod(0o755)
    for file in list((destination / 'src').glob('*.py')) + list((destination / 'network').glob('*.py')):
        ast.parse(file.read_text(encoding='utf-8'), filename=str(file))


def initialize_data(directory, module_root):
    sys.path.insert(0, str(module_root / 'src'))
    from web_server import Store
    store = Store(directory)
    with store.connect() as db:
        # Initial installation reads and records to Pi only. Clearing a native
        # deployment must first be enabled explicitly by the station owner.
        db.execute("INSERT OR IGNORE INTO state VALUES ('native_control', ?)",
                   (json.dumps({'enabled': False, 'blocked': False, 'paused': False}),))


def preflight():
    if os.geteuid() != 0:
        raise ValueError('Run with sudo: sudo bash install.sh')
    release = Path('/etc/os-release').read_text()
    if not re.search(r'^VERSION_ID="?(12|13)"?$', release, re.M):
        raise ValueError('Use Raspberry Pi OS Lite / Debian 12 (Bookworm) or 13 (Trixie)')
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'NetworkManager']).returncode:
        raise ValueError('NetworkManager must already manage the network. Use Raspberry Pi OS Lite; do not switch a remote network stack blindly.')
    if not Path('/sys/class/net/wlan0').exists():
        raise ValueError('wlan0 is required for the setup hotspot (Raspberry Pi 3B onboard Wi-Fi)')
    service = Path('/etc/systemd/system/hobo-u30-server.service')
    if service.exists() and 'WorkingDirectory=/opt/hobo-u30' not in service.read_text():
        raise ValueError('An older custom deployment exists. Use the new Pi; this installer will not overwrite that installation.')
    if (CONFIG.parent / 'password-change.json').exists():
        raise ValueError('Finish/recover the pending password update before installing')
    for path in (APP, DATA, DATA / 'station.sqlite3', DATA / 'station.sqlite3-wal', DATA / 'station.sqlite3-shm'):
        if path.is_symlink():
            raise ValueError('Installation/data paths must not be symbolic links: ' + str(path))


def wait_health():
    for _ in range(40):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2) as response:
                if json.load(response)['ok']:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('Dashboard did not become ready. Run: journalctl -u hobo-u30-server -n 60 --no-pager')


def install(args):
    import grp
    import pwd
    preflight()
    existing = json.loads(CONFIG.read_text()) if CONFIG.exists() else None
    config = settings(existing, hostname=args.hostname)
    if args.dry_run:
        print(json.dumps({'application': str(APP), 'data': str(DATA), 'hostname': config['hostname'],
                          'hotspot': config['setup_ssid'], 'weekly_reboot': not args.no_weekly_reboot,
                          'password': 'preserved' if existing else 'generated privately', 'changes_made': False}, indent=2))
        return
    if not args.skip_packages:
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', '--no-install-recommends', 'python3', 'network-manager',
            'avahi-daemon', 'dnsmasq-base', 'ca-certificates', 'curl', 'tzdata')
    for group in ('plugdev', NETWORK_GROUP):
        try:
            grp.getgrnam(group)
        except KeyError:
            run('groupadd', '--system', group)
    try:
        account = pwd.getpwnam(ACCOUNT)
        if account.pw_uid >= 1000 or account.pw_shell not in ('/usr/sbin/nologin', '/sbin/nologin'):
            raise ValueError('The service account name is already used by an interactive login; no account was changed')
    except KeyError:
        run('useradd', '--system', '--user-group', '--no-create-home', '--home-dir', str(DATA),
            '--shell', '/usr/sbin/nologin', '--groups', 'plugdev,' + NETWORK_GROUP, ACCOUNT)
    run('usermod', '-a', '-G', 'plugdev,' + NETWORK_GROUP, ACCOUNT)
    account = pwd.getpwnam(ACCOUNT)
    APP.parent.mkdir(parents=True, exist_ok=True)
    stage = APP.with_name('.hobo-u30-stage-' + uuid.uuid4().hex[:10])
    runtime_copy(SOURCE, stage)
    previous = APP.with_name('hobo-u30.previous-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    # Stop only our already-installed services. No logger command is sent.
    for service in ('hobo-u30-server.service', 'u30-network.service'):
        if Path('/etc/systemd/system', service).exists():
            run('systemctl', 'stop', service)
    if APP.exists():
        APP.rename(previous)
    stage.rename(APP)
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        DATA.chmod(0o750)
        initialize_data(DATA, APP)
        for path in [DATA, *DATA.rglob('*')]:
            if path.is_symlink():
                continue
            os.chown(path, account.pw_uid, account.pw_gid)
            if path.is_file() and path.suffix != '.part':
                path.chmod(0o640)
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.parent.chmod(0o700)
        if not existing:
            private_write(CONFIG, json.dumps(config, ensure_ascii=False))
        profile = Path('/etc/NetworkManager/system-connections/u30-setup.nmconnection')
        hotspot_migrated = False
        if not profile.exists():
            private_write(profile, hotspot_profile(config))
        else:
            # Updates must never overwrite a password changed through the UI.
            import configparser
            check = configparser.ConfigParser(interpolation=None)
            check.read(profile)
            if check.get('connection', 'uuid') != config['ap_uuid']:
                raise ValueError('Existing setup Wi-Fi profile belongs to another installation')
            if (check.get('wifi', 'mode', fallback=None) != 'ap' or
                    check.get('wifi-security', 'key-mgmt', fallback=None) != 'wpa-psk'):
                raise ValueError('Existing setup Wi-Fi profile is not the expected WPA2 access point')
            # wpa-psk plus default/optional PMF can advertise SAE on newer NM.
            # Our derived WPA2 PSK cannot serve as the SAE password. Explicitly
            # disable PMF to keep the setup AP WPA2-only on Broadcom firmware.
            if check.get('wifi-security', 'pmf', fallback='0') != '1':
                import io
                check.set('wifi-security', 'pmf', '1')
                contents = io.StringIO()
                check.write(contents)
                private_write(profile, contents.getvalue())
                hotspot_migrated = True
        destination = Path('/usr/local/lib/u30-network')
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(APP / 'network/manager.py', destination / 'manager.py')
        (destination / 'manager.py').chmod(0o644)
        for source, target in ((SOURCE / 'hobo-u30-server.service', '/etc/systemd/system/hobo-u30-server.service'),
                               (SOURCE / 'network/u30-network.service', '/etc/systemd/system/u30-network.service'),
                               (SOURCE / 'network/u30-setup-dhcp.service', '/etc/systemd/system/u30-setup-dhcp.service'),
                               (SOURCE / 'u30-weekly-reboot.service', '/etc/systemd/system/u30-weekly-reboot.service'),
                               (SOURCE / 'u30-weekly-reboot.timer', '/etc/systemd/system/u30-weekly-reboot.timer'),
                               (SOURCE / '99-hobo-u30.rules', '/etc/udev/rules.d/99-hobo-u30.rules')):
            target = Path(target)
            shutil.copyfile(source, target)
            target.chmod(0o644)
        avahi = Path('/etc/avahi/avahi-daemon.conf')
        text = avahi.read_text()
        if '[server]' not in text:
            raise ValueError('Avahi configuration has no [server] section')
        original = avahi.with_suffix('.conf.before-hobo-u30')
        if not original.exists():
            shutil.copyfile(avahi, original)
        text = re.sub(r'^host-name=.*\n', '', text, flags=re.M)
        private_write(avahi, text.replace('[server]', '[server]\nhost-name=' + config['hostname'], 1), 0o644)
        discovery = Path('/etc/avahi/services/u30-dashboard.service')
        private_write(discovery, '<service-group><name replace-wildcards="yes">U30 Station on %h</name><service><type>_http._tcp</type><port>8080</port><txt-record>path=/</txt-record></service></service-group>\n', 0o644)
        # The card is created once. Never restore a stale password on an update.
        card = CONFIG.parent / 'connection.txt'
        if not card.exists():
            private_write(card, f'Local URL: http://{config["hostname"]}.local:8080/\nHotspot SSID: {config["setup_ssid"]}\nInitial Pi/hotspot password: {config["setup_key"]}\nHotspot URL: http://10.42.0.1:8080/\nAfter a web password change, the new password takes precedence over this initial card.\n')
        run('timedatectl', 'set-timezone', args.timezone)
        run('timedatectl', 'set-ntp', 'true')
        run('nmcli', 'connection', 'load', str(profile))
        if hotspot_migrated:
            # Reloading the keyfile does not change an already-active AP.
            # Apply the fix now only if wlan0 is this AP; preserve a home Wi-Fi
            # uplink, which must not be disconnected by an installer update.
            active = subprocess.run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', 'wlan0'],
                                    capture_output=True, text=True)
            if active.returncode == 0 and active.stdout.strip() == config['ap_uuid']:
                run('nmcli', '--wait', '20', 'connection', 'up', 'uuid', config['ap_uuid'],
                    'ifname', 'wlan0', timeout=30)
        run('udevadm', 'control', '--reload-rules')
        run('udevadm', 'trigger', '--subsystem-match=hidraw', '--action=change')
        for device in Path('/sys/bus/usb/devices').glob('*'):
            try:
                if ((device / 'idVendor').read_text().strip(), (device / 'idProduct').read_text().strip()) == ('0f1b', '0301'):
                    (device / 'power/control').write_text('on')
                    (device / 'power/autosuspend_delay_ms').write_text('-1')
            except FileNotFoundError:
                pass
        run('systemctl', 'daemon-reload')
        run('systemd-analyze', 'verify', '/etc/systemd/system/hobo-u30-server.service',
            '/etc/systemd/system/u30-network.service', '/etc/systemd/system/u30-weekly-reboot.timer')
        run('systemctl', 'restart', 'avahi-daemon')
        run('systemctl', 'enable', '--now', 'u30-network.service', 'hobo-u30-server.service')
        if args.no_weekly_reboot:
            run('systemctl', 'disable', '--now', 'u30-weekly-reboot.timer')
        else:
            run('systemctl', 'enable', '--now', 'u30-weekly-reboot.timer')
        wait_health()
    except BaseException:
        print('Installation did not finish. Data and credentials were retained. Fix the error and rerun install.sh.', file=sys.stderr)
        if previous.exists():
            print('Previous application code retained at: ' + str(previous), file=sys.stderr)
        raise
    print('\nInstalled HOBO U30 station ' + (APP / 'VERSION').read_text().strip())
    print('Open: http://' + config['hostname'] + '.local:8080/')
    print('Setup Wi-Fi: ' + config['setup_ssid'] + ' -> http://10.42.0.1:8080/')
    print('Read your initial private password: sudo cat /etc/u30-network/connection.txt')
    print('Verify installation: python3 /opt/hobo-u30/tools/doctor.py')
    print('U30 deployments were not changed. Enable automatic sensor enrollment in the UI only when wanted.')
    if previous.exists():
        print('Previous code (data is stored separately): ' + str(previous))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hostname', help='Optional mDNS hostname on first installation')
    parser.add_argument('--timezone', default='Asia/Taipei')
    parser.add_argument('--no-weekly-reboot', action='store_true')
    parser.add_argument('--skip-packages', action='store_true', help='Use only after dependencies are installed')
    parser.add_argument('--dry-run', action='store_true', help='Preflight and show paths; do not modify the Pi')
    args = parser.parse_args()
    try:
        install(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, 'Installation failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
