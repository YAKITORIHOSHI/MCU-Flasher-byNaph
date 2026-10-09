#!/usr/bin/env bash
# Explicit owned Ubuntu runtime reset; no system packages or Windows resources.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 -B "$script_dir/../cleaner/ubuntu/maintenance.py" runtime "$@"
