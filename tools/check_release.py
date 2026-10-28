"""Check tracked file paths and common credential formats before a release."""
from pathlib import Path
import re
import subprocess


def main():
    root = Path(__file__).resolve().parents[1]
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    errors = []
    patterns = [re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}'),
                re.compile(rb'github_pat_[A-Za-z0-9_]{30,}'),
                re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]
    for name in filter(None, names):
        path = Path(name)
        if (path.parts[0] in ('data', 'tmp', 'research', 'output', 'dist', '.venv') or
                'PRIVATE' in name or path.suffix in ('.pem', '.key', '.pcap', '.pcapng', '.sqlite3', '.part') or
                path.name == 'tunnel-token'):
            errors.append(name + ': private/runtime artifact must not be tracked')
        raw = (root / name).read_bytes()
        if any(pattern.search(raw) for pattern in patterns):
            errors.append(name + ': possible credential content')
    if errors:
        raise SystemExit('\n'.join(errors))
    print('Tracked source scan passed; no runtime/private artifacts or common credential formats found.')


if __name__ == '__main__':
    main()
