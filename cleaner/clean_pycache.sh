#!/usr/bin/env bash
# Ubuntu source bytecode maintenance. Defaults to preview.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 -B "$script_dir/ubuntu/maintenance.py" bytecode "$@"
