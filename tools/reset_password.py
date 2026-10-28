#!/usr/bin/env python3
"""Root recovery for a forgotten Pi/hotspot password. Prompts without echo."""
import getpass
import json
import os
from pathlib import Path
import re
import socket
import time


def request(data):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect('/run/u30-network/control.sock')
        connection.sendall(json.dumps(data).encode() + b'\n')
        with connection.makefile('rb') as stream:
            return json.loads(stream.readline(16385))


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo')
    password = getpass.getpass('New Pi/setup-hotspot password (8–63 ASCII characters): ')
    confirmation = getpass.getpass('Confirm new password: ')
    if not re.fullmatch(r'[ -~]{8,63}', password) or password != confirmation:
        raise SystemExit('Invalid password or confirmation mismatch; nothing changed')
    key = json.loads(Path('/etc/u30-network/config.json').read_text())['setup_key']
    result = request({'action': 'password', 'setup_key': key, 'new_password': password,
                      'confirm_password': confirmation, 'confirmed': True})
    if result['status'] != 202:
        raise SystemExit(result['data'].get('error', 'Password update was not accepted'))
    for _ in range(40):
        time.sleep(1)
        job = request({'action': 'status'})['data'].get('job') or {}
        if job.get('kind') == 'password' and job.get('state') in ('success', 'failed'):
            print(job['message'])
            raise SystemExit(0 if job['state'] == 'success' else 1)
    raise SystemExit('Update status is not yet confirmed; check the station before retrying')


if __name__ == '__main__':
    main()
