#!/usr/bin/env bash
# Rebuild only the native Ubuntu launcher. Never changes Windows artifacts.
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$(uname -s)" != Linux ]]; then
    printf '%s\n' 'Build the Ubuntu native launcher on native Ubuntu/Linux.' >&2
    exit 1
fi
source "$app_root/direct/ubuntu/release_guard.sh"
require_supported_ubuntu || exit 1
source "$app_root/direct/ubuntu/architecture_guard.sh"
require_supported_ubuntu_architecture || exit 1
# Native binaries are CPU-specific. Ship both in the application folder; the
# ARM64 name makes the Raspberry Pi entry distinguishable in Ubuntu Files.
case "$mcu_ubuntu_architecture" in
    amd64) default_output="$app_root/MCU_Flasher" ;;
    arm64) default_output="$app_root/MCU_Flasher-arm64" ;;
esac
output="${1:-$default_output}"
output_directory="$(cd -- "$(dirname -- "$output")" && pwd)"
output="$output_directory/$(basename -- "$output")"
if [[ "${output,,}" == *.exe || "$output" -ef "$app_root/MCU_Flasher.exe" ]]; then
    printf '%s\n' 'Choose an Ubuntu output; Windows executables must remain unchanged.' >&2
    exit 1
fi
compiler="${CC:-cc}"
if ! command -v "$compiler" >/dev/null; then
    printf '%s\n' 'To rebuild the native launcher, install build-essential. The supplied matching-CPU executable needs no compiler; bash direct/ubuntu/run.sh also works.' >&2
    exit 1
fi
temporary="$(mktemp "$output_directory/.mcu-launcher-build.XXXXXX")"
trap 'rm -f -- "$temporary"' EXIT
"$compiler" -std=c11 -O2 -Wall -Wextra -Wpedantic -Wformat=2 -Wwrite-strings -Werror -D_FORTIFY_SOURCE=2 \
    -fPIE -pie -Wl,-z,relro,-z,now "$app_root/src/launcher_ubuntu.c" -o "$temporary"
chmod 755 "$temporary"
mv -- "$temporary" "$output"
printf 'Ubuntu executable created: %s\n' "$output"
