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
# The portable root MCU_Flasher works on both architectures without compilation.
# An optional native build stays beside it, where its folder lookup is valid.
output="${1:-$app_root/MCU_Flasher-$mcu_ubuntu_architecture}"
output_directory="$(cd -- "$(dirname -- "$output")" && pwd)"
output="$output_directory/$(basename -- "$output")"
if [[ "$output" == "$app_root/MCU_Flasher" || "$output" -ef "$app_root/MCU_Flasher" || "${output,,}" == *.exe ]]; then
    printf '%s\n' 'Choose a separate Ubuntu native output; keep the portable MCU_Flasher and Windows executables unchanged.' >&2
    exit 1
fi
compiler="${CC:-cc}"
if ! command -v "$compiler" >/dev/null; then
    printf '%s\n' 'To build the optional native launcher, install build-essential. The supplied portable MCU_Flasher needs no compiler.' >&2
    exit 1
fi
temporary="$(mktemp "$output_directory/.mcu-launcher-build.XXXXXX")"
trap 'rm -f -- "$temporary"' EXIT
"$compiler" -std=c11 -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 \
    -fPIE -pie -Wl,-z,relro,-z,now "$app_root/src/launcher_ubuntu.c" -o "$temporary"
chmod +x "$temporary"
mv -- "$temporary" "$output"
printf 'Ubuntu executable created: %s\n' "$output"
