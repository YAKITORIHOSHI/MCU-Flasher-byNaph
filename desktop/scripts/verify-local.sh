#!/usr/bin/env bash
# Hardware-free migration checks. Sources are reused from this checkout;
# generated output stays in audit/migration/{logs,scratch} and desktop/target.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd -- "$script_dir/../.." && pwd)"
cd -- "$repo"
source desktop/dev-env.sh
mkdir -p audit/migration/logs audit/migration/scratch
pnpm --dir desktop/ui install --frozen-lockfile
pnpm --dir desktop/ui test
pnpm --dir desktop/ui build
cargo fmt --manifest-path desktop/Cargo.toml --all -- --check
cargo test --manifest-path desktop/Cargo.toml --locked
cargo clippy --manifest-path desktop/Cargo.toml --workspace --all-targets --locked -- -D warnings
cargo build --manifest-path desktop/Cargo.toml --locked -p mcu-host --bins
cargo run --manifest-path desktop/Cargo.toml --locked -p mcu-host --bin pty_probe
python3 -B -W error desktop/scripts/verify-serial-codec.py
python3 -B -W error desktop/scripts/verify-preferences-merge.py
python3 -B -W error desktop/scripts/verify-scaffold.py
python3 -B -W error desktop/scripts/verify-resource-budget.py
python3 -B -W error desktop/scripts/verify-lock-interop.py
python3 -B -W error desktop/scripts/verify-target-transport.py
python3 -B -W error desktop/scripts/verify-ownership.py
python3 -B -W error desktop/scripts/verify-process-identity.py
python3 -B -W error desktop/scripts/verify-registry.py
cargo build --manifest-path desktop/Cargo.toml --locked -p mcu-desktop-prototype
desktop/target/debug/mcu-desktop-prototype --verify-prototype
