#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd -- "$script_dir/../.." && pwd)"
cd -- "$repo"
source desktop/dev-env.sh
pnpm --dir desktop/ui install --frozen-lockfile
pnpm --dir desktop/ui build
cargo build --manifest-path desktop/Cargo.toml --locked -p mcu-desktop-prototype
exec desktop/target/debug/mcu-desktop-prototype "$@"
