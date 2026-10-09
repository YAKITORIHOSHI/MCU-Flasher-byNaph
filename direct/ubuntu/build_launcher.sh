#!/usr/bin/env bash
# Rebuild only the native Ubuntu launcher. Never changes Windows artifacts.
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    printf '%s\n' 'The Ubuntu desktop release currently requires x86_64 Linux.' >&2
    exit 1
fi
output="${1:-$app_root/MCU_Flasher}"
compiler="${CC:-cc}"
if ! command -v "$compiler" >/dev/null; then
    printf '%s\n' 'To rebuild the launcher, install build-essential. The supplied MCU_Flasher needs no compiler.' >&2
    exit 1
fi
"$compiler" -std=c11 -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 \
    -fPIE -pie -Wl,-z,relro,-z,now "$app_root/src/launcher_ubuntu.c" -o "$output"
chmod +x "$output"
printf 'Ubuntu executable created: %s\n' "$output"
if [[ "$output" == "$app_root/MCU_Flasher" ]]; then
    /usr/bin/python3 -B "$app_root/direct/ubuntu/install_desktop.py"
fi
