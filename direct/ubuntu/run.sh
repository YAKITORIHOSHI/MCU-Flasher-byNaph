#!/usr/bin/env bash
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$(uname -s)" != "Linux" ]]; then
    printf '%s\n' 'Use direct/windows/run.vbs on Windows.' >&2
    exit 1
fi
if [[ "${EUID}" == 0 ]]; then
    printf '%s\n' 'Run MCU Flasher as your normal desktop account, without sudo.' >&2
    exit 1
fi
unset PYTHONHOME PYTHONPATH MCU_FLASHER_OFFLINE_RUNTIME MCU_FLASHER_APP_ROOT PIP_NO_INDEX
export PYTHONNOUSERSITE=1
runtime="$app_root/.venv-linux/bin/python"
if [[ "${1:-}" == "--repair" ]]; then
    shift
    python3 "$app_root/direct/ubuntu/setup.py"
elif [[ ! -x "$runtime" ]]; then
    python3 "$app_root/direct/ubuntu/setup.py"
fi
if ! "$runtime" -c 'import PySide6.QtWebEngineWidgets, serial, psutil, platformio, ptyprocess' >/dev/null 2>&1; then
    printf '%s\n' 'Ubuntu runtime needs repair: python3 direct/ubuntu/setup.py' >&2
    exit 1
fi
if ! "$runtime" -c 'import sys; sys.path.insert(0, sys.argv[1]); from src.modules.offline_bootstrap import ready; from src.modules.platform_runtime import native_platformio_dir; sys.exit(0 if ready(native_platformio_dir()) else 1)' "$app_root"; then
    printf '%s\n' 'Offline board/library packs need bootstrap preparation.' >&2
    "$runtime" "$app_root/direct/ubuntu/setup.py"
fi
exec "$runtime" "$app_root/mcu_flash_gui.py" "$@"
