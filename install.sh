#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$EUID" -ne 0 ]]; then
    exec sudo -- bash "$project_dir/install.sh" "$@"
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo 'python3 is required. Run: sudo apt-get update && sudo apt-get install -y python3' >&2
    exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$project_dir/scripts/install.py" "$@"
