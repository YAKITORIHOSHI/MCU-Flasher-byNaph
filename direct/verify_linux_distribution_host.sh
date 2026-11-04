#!/usr/bin/env bash
# Qualify the real userland of one Linux distribution: its own os-release, its own
# package manager and a cold first launch through to real firmware.
# Run as root inside a disposable container only: this creates a user and installs packages.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
root="$PWD"
if [[ ! -e /.dockerenv && ! -e /run/.containerenv ]]; then
    printf '%s\n' 'This verifier changes system packages. Run it only inside a disposable container.' >&2
    exit 2
fi
evidence="$root/temp/audit/other-platforms"
mkdir -p "$evidence"
{ cat /etc/os-release; uname -a; python3 --version; } | tee "$evidence/host.txt"
id builder >/dev/null 2>&1 || useradd -m builder
printf 'builder ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/mcu-builder
chmod 440 /etc/sudoers.d/mcu-builder
chown -R builder: "$root"
as_builder() {
    sudo -H -u builder env PYTHONFAULTHANDLER=1 PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-}" \
        QTWEBENGINE_CHROMIUM_FLAGS="${QTWEBENGINE_CHROMIUM_FLAGS:-}" \
        QTWEBENGINE_DISABLE_SANDBOX="${QTWEBENGINE_DISABLE_SANDBOX:-}" "$@"
}
echo '--- Admission from the real os-release, in shell and Python'
as_builder bash -c 'source direct/ubuntu/release_guard.sh && require_supported_ubuntu && printf "%s %s.%s %s\n" "$mcu_linux_family" "$mcu_ubuntu_release_major" "$mcu_ubuntu_release_minor" "$mcu_linux_label"'
as_builder /usr/bin/python3 -B -c 'from src.modules.ubuntu_release import linux_distribution; print(linux_distribution())'
# Passing on the wrong policy proves nothing about the distribution this job claims to test.
family="$(as_builder /usr/bin/python3 -B -c 'from src.modules.ubuntu_release import linux_distribution; print(linux_distribution().family)')"
if [[ -n "${EXPECTED_LINUX_FAMILY:-}" && "$family" != "$EXPECTED_LINUX_FAMILY" ]]; then
    printf 'This job qualifies %s, but the host was admitted as %s.\n' "$EXPECTED_LINUX_FAMILY" "$family" >&2
    exit 1
fi
echo '--- Hardware-free adapter checks'
as_builder /usr/bin/python3 -B direct/verify_ubuntu_release.py
as_builder /usr/bin/python3 -B direct/verify_linux_distributions.py
echo '--- The real package adapter, with a terminal and sudo like a desktop session'
# shellcheck disable=SC2016
as_builder script -qfec '/usr/bin/python3 -B -c "from direct.ubuntu.system_setup import ensure_system_dependencies; ensure_system_dependencies()"' /dev/null
echo '--- Native launcher entries from the current source'
as_builder /usr/bin/python3 -B direct/build_launcher_folder.py
echo '--- Cold first launch: empty runtime and store, real workspace, real Compile'
as_builder xvfb-run -a /usr/bin/python3 -B direct/verify_ubuntu_first_run.py --execute --allow-small-runner --timeout 3000
fixture="$(dirname "$(ls -d temp/audit/ubuntu-first-run/*/result.json | tail -1)")"
echo '--- The package browser in the runtime that first launch created'
cd "$fixture/app"
as_builder env QT_QPA_PLATFORM=offscreen "$PWD/.venv-linux/bin/python" -B direct/verify_package_browser.py
as_builder env QT_QPA_PLATFORM=offscreen "$PWD/.venv-linux/bin/python" -B direct/verify_browser_loading.py
