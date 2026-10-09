#!/usr/bin/env bash
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$(uname -s)" != "Linux" ]]; then
    printf '%s\n' 'Use direct/windows/run.vbs on Windows.' >&2
    exit 1
fi
# Bootstrap's coordinator uses Ubuntu Python; the workspace always uses its venv.
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE
unset MCU_FLASHER_OFFLINE_RUNTIME MCU_FLASHER_WORKSPACE_RUNTIME MCU_FLASHER_APP_ROOT PIP_NO_INDEX
export PYTHONNOUSERSITE=1
if [[ ! -x /usr/bin/python3 ]]; then
    # Minimal rescue for stripped-down Ubuntu desktops: Python is needed to run
    # the full Bootstrap coordinator. --check never authenticates or installs.
    bootstrap_window=false
    for argument in "$@"; do
        if [[ "$argument" == '--check' ]]; then
            printf '%s\n' 'Ubuntu system Python is missing. Launch normally to install python3 and python3-venv.' >&2
            exit 1
        fi
        [[ "$argument" != '--bootstrap-window' ]] || bootstrap_window=true
    done
    if (( EUID == 0 )); then
        printf '%s\n' 'Run MCU Flasher as your normal Ubuntu desktop account, without sudo.' >&2
        exit 1
    fi
    if [[ ! -t 0 && "$bootstrap_window" == false ]]; then
        for terminal in x-terminal-emulator gnome-terminal konsole xfce4-terminal xterm kitty alacritty; do
            if command -v "$terminal" >/dev/null; then
                separator=-e
                [[ "$terminal" != gnome-terminal && "$terminal" != alacritty ]] || separator=--
                [[ "$terminal" != xfce4-terminal ]] || separator=--execute
                exec "$terminal" "$separator" /bin/bash "$app_root/direct/ubuntu/run.sh" --bootstrap-window "$@"
            fi
        done
        printf '%s\n' 'Bootstrap needs a desktop terminal to install Ubuntu Python. Install python3 and python3-venv, then launch again.' >&2
        exit 1
    fi
    if [[ ! -x /usr/bin/sudo || ! -x /usr/bin/apt-get ]]; then
        printf '%s\n' 'Automatic Python setup requires Ubuntu sudo and apt-get. Install python3 and python3-venv, then launch again.' >&2
        exit 1
    fi
    printf '%s\n' 'Ubuntu Bootstrap will install python3 and python3-venv. Authenticate using the Ubuntu administrator prompt.'
    apt_options=(-o DPkg::Lock::Timeout=120 -o Acquire::Retries=1 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
    if ! /usr/bin/sudo /usr/bin/apt-get "${apt_options[@]}" update ||
       ! /usr/bin/sudo /usr/bin/apt-get "${apt_options[@]}" install --yes --no-remove --no-install-recommends python3 python3-venv; then
        printf '%s\n' 'Ubuntu Python setup failed or administrator authentication was cancelled. Resolve the error above and launch again.' >&2
        if [[ "$bootstrap_window" == true ]]; then
            read -r -p 'Press Enter to close this setup window…' || true
        fi
        exit 1
    fi
    if [[ ! -x /usr/bin/python3 ]]; then
        printf '%s\n' 'Ubuntu Python is still unavailable after package installation. Bootstrap has stopped.' >&2
        exit 1
    fi
fi
exec /usr/bin/python3 -B "$app_root/direct/ubuntu/launch.py" "$@"
