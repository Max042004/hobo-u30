#!/usr/bin/env python3
"""Read-only installation check. Does not open HID or disclose credentials."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request


def collect(base='http://127.0.0.1:8080'):
    report = {'services': {}, 'dashboard': False, 'usb_connected': False, 'warnings': []}
    for name in ('hobo-u30-server', 'u30-network', 'avahi-daemon'):
        result = subprocess.run(['systemctl', 'is-active', name], capture_output=True, text=True)
        report['services'][name] = result.stdout.strip()
    try:
        with urllib.request.urlopen(base + '/api/status', timeout=10) as response:
            status = json.load(response)
        report.update(dashboard=True, usb_connected=status['usb_connected'],
                      interval_seconds=status['interval_seconds'], last_success_utc=status['last_success_utc'])
        if status.get('snapshot'):
            snapshot = status['snapshot']
            report.update(serial=snapshot['serial'], connected_channels=len(snapshot['sensors']),
                          native_logging=snapshot['logging'])
        if status.get('error'):
            report['warnings'].append(status['error'])
    except (OSError, ValueError) as exc:
        report['warnings'].append('Dashboard is not ready: ' + str(exc))
    try:
        with urllib.request.urlopen(base + '/api/network', timeout=8) as response:
            network = json.load(response)
        report['network'] = {key: network.get(key) for key in ('available', 'mode', 'hostname_url', 'setup_ssid', 'setup_url')}
        if not network.get('available'):
            report['warnings'].append('Network controller unavailable; check u30-network service and socket group')
    except (OSError, ValueError):
        report['warnings'].append('Cannot query the network controller')
    synced = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'], capture_output=True, text=True)
    report['clock_synchronized'] = synced.stdout.strip() == 'yes'
    if not report['clock_synchronized']:
        report['warnings'].append('Clock is not synchronized; check NTP before changing U30 deployments')
    if not report['usb_connected']:
        report['warnings'].append('No live U30 connection. Connect USB and wait for the configured acquisition interval.')
    disk = shutil.disk_usage('/var/lib/hobo-u30' if Path('/var/lib/hobo-u30').exists() else '/')
    report['free_storage_mb'] = disk.free // 1024 // 1024
    report['ok'] = report['dashboard'] and all(value == 'active' for value in report['services'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-device', action='store_true')
    args = parser.parse_args()
    report = collect()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if report['ok'] and (report['usb_connected'] or not args.require_device) else 1)


if __name__ == '__main__':
    main()
