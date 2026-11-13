#!/usr/bin/env bash
set -euo pipefail
lite_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
case "$(uname -m)" in
  aarch64|arm64) lite_executable="$lite_dir/UBUNTU_ARM64" ;;
  x86_64) lite_executable="$lite_dir/UBUNTU" ;;
  *) printf '%s\n' 'Lite supports Ubuntu x64/ARM64 and Windows x64.' >&2; exit 1 ;;
esac
if [[ ! -x "$lite_executable" ]]; then
  if ! command -v cargo >/dev/null 2>&1; then
    printf '%s\n' 'Build Lite with Rust first: python3 LITE-MODE/tools/build.py' >&2
    exit 1
  fi
  cargo build --locked --release -j 4 --manifest-path "$lite_dir/Cargo.toml"
  cp -- "$lite_dir/target/release/mcu-flasher-lite" "$lite_executable"
  chmod +x -- "$lite_executable"
fi
exec "$lite_executable" "$@"
