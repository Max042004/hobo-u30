#!/usr/bin/env python3
"""Optional Cloudflare connector setup using a user-owned, pre-protected tunnel."""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'network'))
from manager import private_write


def validate_token(token):
    token = token.strip().lstrip('\ufeff')
    if not 20 <= len(token) <= 16384 or not token.isascii():
        raise ValueError('Invalid connector token')
    try:
        data = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
        uuid.UUID(data['t'])
        if not isinstance(data['a'], str) or not data['a'] or not isinstance(data['s'], str) or not data['s']:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError('Expected the complete connector token, not the Tunnel ID or install command') from None
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', type=Path, help='Optional root-readable file containing only the token')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo')
    arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    if arch not in ('arm64', 'armhf', 'amd64'):
        parser.error('Unsupported cloudflared package architecture: ' + arch)
    print('Use a separate tunnel/hostname for this Pi. Configure Cloudflare Access before publishing it.')
    try:
        raw = args.token_file.read_text() if args.token_file else getpass.getpass('Cloudflare connector token (input hidden): ')
        token = validate_token(raw)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    target = Path('/etc/cloudflared/tunnel-token')
    if target.exists() and target.read_text().strip() != token:
        parser.error('A different connector already exists. No credential or route was overwritten.')
    repo = Path('/etc/apt/sources.list.d/cloudflared.list')
    desired = 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main\n'
    if repo.exists() and repo.read_text() != desired:
        parser.error('A different Cloudflare APT source exists; inspect it before changing it')
    key = subprocess.check_output(['curl', '-fsSL', '--max-time', '30', 'https://pkg.cloudflare.com/cloudflare-main.gpg'])
    if not 100 <= len(key) <= 1024 * 1024:
        parser.error('Unexpected package signing key size')
    Path('/usr/share/keyrings/cloudflare-main.gpg').write_bytes(key)
    Path('/usr/share/keyrings/cloudflare-main.gpg').chmod(0o644)
    repo.write_text(desired)
    repo.chmod(0o644)
    subprocess.run(['apt-get', 'update'], check=True)
    subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends', 'cloudflared'], check=True)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    private_write(target, token + '\n')
    service = Path('/etc/systemd/system/cloudflared-u30.service')
    shutil.copyfile(ROOT / 'network/cloudflared-u30.service', service)
    service.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'cloudflared-u30.service'], check=True)
    subprocess.run(['systemctl', 'restart', 'cloudflared-u30.service'], check=True)
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:20241/ready', timeout=2) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        time.sleep(2)
    else:
        parser.exit(1, 'Connector is not ready yet. Check DNS, system time and: journalctl -u cloudflared-u30 -n 40 --no-pager\n')
    print('Connector ready. Route the protected hostname to http://localhost:8080 and verify Access sign-in.')
    print('Check: systemctl status cloudflared-u30 --no-pager')


if __name__ == '__main__':
    main()
