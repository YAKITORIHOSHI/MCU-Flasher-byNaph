#!/usr/bin/env bash
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" != "Linux" ]]; then
    printf '%s\n' 'Use MCU_Flasher.exe on Windows.' >&2
    exit 1
fi
unset PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1
if [[ ! -x "$app_root/.venv-linux/bin/python" ]]; then
    python3 "$app_root/direct/setup_ubuntu.py"
fi
exec "$app_root/.venv-linux/bin/python" "$app_root/mcu_flash_gui.py" "$@"
