#!/bin/bash
set -euo pipefail
bridge_root=$(cd "$(dirname "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.9+ is required. Install it before running this script.' >&2
  exit 1
fi
exec python3 "$bridge_root/scripts/bridge.py" "$@"
