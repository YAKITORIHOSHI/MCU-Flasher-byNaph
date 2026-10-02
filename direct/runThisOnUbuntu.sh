#!/usr/bin/env bash
# Compatible old entry point; the Ubuntu implementation lives separately.
set -euo pipefail
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/ubuntu/run.sh" "$@"
