#!/usr/bin/env bash
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$(uname -s)" != "Linux" ]]; then
    printf '%s\n' 'Use direct/windows/run.vbs on Windows.' >&2
    exit 1
fi
# A double-clicked entry has no terminal, so its refusal must also reach the desktop.
# Only LAUNCHER entries pass --desktop: scripts, checks and terminals never get a dialog.
report_refusal() {
    local message=$1 argument desktop=false
    shift
    printf '%s\n' "$message" >&2
    for argument in "$@"; do
        [[ "$argument" != --desktop ]] || desktop=true
    done
    [[ "$desktop" == true && ! -t 2 && -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] || return 0
    if [[ -x /usr/bin/zenity ]]; then
        /usr/bin/zenity --error --title='MCU Flasher' --width=560 --no-markup --text="$message" || true
    elif [[ -x /usr/bin/kdialog ]]; then
        /usr/bin/kdialog --title 'MCU Flasher' --error "$message" || true
    fi
}
source "$app_root/direct/ubuntu/release_guard.sh"
refusal=$(require_supported_ubuntu 2>&1 >/dev/null) || { report_refusal "$refusal" "$@"; exit 1; }
require_supported_ubuntu
source "$app_root/direct/ubuntu/architecture_guard.sh"
require_supported_ubuntu_architecture || exit 1
# Bootstrap's coordinator uses the system Python; the workspace always uses its venv.
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE
unset PYTHONEXEPATH PIO_PYTHON_EXE PLATFORMIO_PYTHON_EXE PLATFORMIO_PENV_DIR
unset MCU_FLASHER_OFFLINE_RUNTIME MCU_FLASHER_WORKSPACE_RUNTIME MCU_FLASHER_APP_ROOT PIP_NO_INDEX
export PYTHONNOUSERSITE=1
if [[ ! -x /usr/bin/python3 ]]; then
    # Minimal rescue for stripped-down desktops: Python is needed to run
    # the full Bootstrap coordinator. --check never authenticates or installs.
    if [[ "$mcu_linux_family" == fedora ]]; then
        python_packages=(python3)
        package_tool=/usr/bin/dnf
    else
        python_packages=(python3 python3-venv)
        package_tool=/usr/bin/apt-get
    fi
    bootstrap_window=false
    for argument in "$@"; do
        if [[ "$argument" == '--check' ]]; then
            printf '%s\n' "$mcu_linux_label system Python is missing. Launch normally to install ${python_packages[*]}." >&2
            exit 1
        fi
        [[ "$argument" != '--bootstrap-window' ]] || bootstrap_window=true
    done
    if (( EUID == 0 )); then
        printf '%s\n' "Run MCU Flasher as your normal $mcu_linux_label desktop account, without sudo." >&2
        exit 1
    fi
    if [[ ! -t 0 && "$bootstrap_window" == false ]]; then
        terminals=(x-terminal-emulator gnome-terminal konsole xfce4-terminal xterm kitty alacritty)
        if [[ "$mcu_linux_family" == fedora ]] || (( mcu_ubuntu_release_major > 26 || (mcu_ubuntu_release_major == 26 && mcu_ubuntu_release_minor >= 4) )); then
            terminals=(ptyxis "${terminals[@]}")
        fi
        for terminal in "${terminals[@]}"; do
            if command -v "$terminal" >/dev/null; then
                case "$terminal" in
                    ptyxis) terminal_options=(--standalone --) ;;
                    gnome-terminal|kitty) terminal_options=(--) ;;
                    xfce4-terminal) terminal_options=(--execute) ;;
                    *) terminal_options=(-e) ;;
                esac
                exec "$terminal" "${terminal_options[@]}" /bin/bash "$app_root/direct/ubuntu/run.sh" --bootstrap-window "$@"
            fi
        done
        printf '%s\n' "Bootstrap needs a desktop terminal to install $mcu_linux_label Python. Install ${python_packages[*]}, then launch again." >&2
        exit 1
    fi
    if [[ ! -x /usr/bin/sudo || ! -x "$package_tool" ]]; then
        printf '%s\n' "Automatic Python setup requires $mcu_linux_label sudo and ${package_tool##*/}. Install ${python_packages[*]}, then launch again." >&2
        exit 1
    fi
    printf '%s\n' "$mcu_linux_label Bootstrap will install ${python_packages[*]}. Authenticate using the $mcu_linux_label administrator prompt."
    if [[ "$mcu_linux_family" == fedora ]]; then
        python_installed() {
            /usr/bin/sudo /usr/bin/dnf install --assumeyes --refresh --setopt=install_weak_deps=False --setopt=timeout=30 --setopt=retries=1 python3
        }
    else
        python_installed() {
            local apt_options=(-o DPkg::Lock::Timeout=120 -o Acquire::Retries=1 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
            /usr/bin/sudo /usr/bin/apt-get "${apt_options[@]}" update &&
                /usr/bin/sudo /usr/bin/apt-get "${apt_options[@]}" install --yes --no-remove --no-install-recommends python3 python3-venv
        }
    fi
    if ! python_installed; then
        printf '%s\n' "$mcu_linux_label Python setup failed or administrator authentication was cancelled. Resolve the error above and launch again." >&2
        if [[ "$bootstrap_window" == true ]]; then
            read -r -p 'Press Enter to close this setup window…' || true
        fi
        exit 1
    fi
    if [[ ! -x /usr/bin/python3 ]]; then
        printf '%s\n' "$mcu_linux_label Python is still unavailable after package installation. Bootstrap has stopped." >&2
        exit 1
    fi
fi
exec /usr/bin/python3 -B "$app_root/direct/ubuntu/launch.py" "$@"
