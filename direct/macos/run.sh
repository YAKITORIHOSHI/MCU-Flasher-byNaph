#!/bin/bash
# Native macOS entry. Bash 3.2 syntax only: that is the bash macOS ships.
# macOS has no system Python to rely on, so this prepares a pinned private one
# (like Windows' private runtime) with no administrator rights or Homebrew.
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$(uname -s)" != Darwin ]]; then
    printf '%s\n' 'direct/macos/run.sh is for macOS. Use the matching entry in LAUNCHER on other systems.' >&2
    exit 1
fi
# Read the installed release before anything is downloaded or created.
product_version="$(/usr/bin/sw_vers -productVersion)"
if [[ ! "$product_version" =~ ^([0-9]{1,4})(\.[0-9]{1,4}){0,2}$ ]]; then
    printf '%s\n' 'The installed macOS version is missing or malformed. MCU Flasher requires macOS 14 or newer.' >&2
    exit 1
fi
if (( 10#${BASH_REMATCH[1]} < 14 )); then
    printf '%s\n' 'MCU Flasher requires macOS 14 or newer.' >&2
    exit 1
fi
if (( EUID == 0 )); then
    printf '%s\n' 'Run MCU Flasher as your normal macOS account, without sudo.' >&2
    exit 1
fi
# A Rosetta-translated shell reports x86_64 on Apple Silicon. The GUI, its private
# Python and its tools must all be native, so never select them from that answer.
if [[ "$(/usr/sbin/sysctl -n sysctl.proc_translated 2>/dev/null || printf 0)" == 1 ]]; then
    printf '%s\n' 'MCU Flasher is running under Rosetta. Open Terminal natively (not "Open using Rosetta") and launch again.' >&2
    exit 1
fi
python_version=3.14.10
python_release=20261026
case "$(uname -m)" in
    arm64)
        python_target=aarch64-apple-darwin
        python_sha256=95eb8c87d16af37c54e9765e31d8c56c0d8ffa9968ea0f704f73a9b83f84e9f9
        ;;
    x86_64)
        python_target=x86_64-apple-darwin
        python_sha256=2fd8becd48cf0ff5f0a15df3c0db7cae7aa708dcaacf901bd30d6cdd6229370b
        ;;
    *)
        printf '%s\n' 'MCU Flasher requires a 64-bit Mac (Intel or Apple Silicon).' >&2
        exit 1
        ;;
esac
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE
unset PYTHONEXEPATH PIO_PYTHON_EXE PLATFORMIO_PYTHON_EXE PLATFORMIO_PENV_DIR
unset MCU_FLASHER_OFFLINE_RUNTIME MCU_FLASHER_WORKSPACE_RUNTIME MCU_FLASHER_APP_ROOT PIP_NO_INDEX
export PYTHONNOUSERSITE=1
tools="$app_root/.macos-tools"
python_home="$tools/python-$python_version-$python_target"
python="$python_home/bin/python3"
if [[ ! -x "$python" ]]; then
    for argument in "$@"; do
        if [[ "$argument" == --check ]]; then
            printf '%s\n' 'The macOS private Python is not prepared yet. Launch normally to prepare it.' >&2
            exit 1
        fi
    done
    if [[ -L "$tools" || ( -e "$tools" && ! -d "$tools" ) ]]; then
        printf '%s\n' '.macos-tools must be an app-owned local directory.' >&2
        exit 1
    fi
    mkdir -p "$tools"
    asset="cpython-$python_version+$python_release-$python_target-install_only_stripped.tar.gz"
    stage="$(mktemp -d "$tools/.python-stage.XXXXXX")"
    trap 'rm -rf -- "$stage"' EXIT
    printf '%s\n' "Preparing the private Python $python_version for this Mac..."
    /usr/bin/curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
        --retry 2 --connect-timeout 20 --max-time 900 --output "$stage/$asset" \
        "https://github.com/astral-sh/python-build-standalone/releases/download/$python_release/${asset//+/%2B}"
    actual="$(/usr/bin/shasum -a 256 "$stage/$asset" | /usr/bin/cut -d ' ' -f 1)"
    if [[ "$actual" != "$python_sha256" ]]; then
        printf '%s\n' 'The private Python download failed its SHA-256 verification. Nothing was installed.' >&2
        exit 1
    fi
    /usr/bin/tar -xzf "$stage/$asset" -C "$stage"
    "$stage/python/bin/python3" -I -B -c 'import ensurepip, ssl, venv'
    mv "$stage/python" "$python_home"
    rm -rf -- "$stage"
    trap - EXIT
fi
exec "$python" -I -B "$app_root/direct/macos/launch.py" "$@"
