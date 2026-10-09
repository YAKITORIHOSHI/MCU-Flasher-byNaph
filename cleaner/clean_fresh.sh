#!/usr/bin/env bash
# Ubuntu-only refresh. Defaults to preview; keeps sketches, settings and recovery.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 -B "$script_dir/ubuntu/maintenance.py" fresh "$@"
