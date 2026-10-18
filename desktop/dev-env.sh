#!/usr/bin/env bash
# Source this file for the optional checkout-local build tools. Never set HOME
# or modify the user's shell profiles or Python installation.
MCU_MIGRATION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export RUSTUP_TOOLCHAIN="1.99.1"
if [[ -x "$MCU_MIGRATION_ROOT/.migration-tools/node/bin/node" ]]; then
  export PATH="$MCU_MIGRATION_ROOT/.migration-tools/node/bin:$PATH"
fi
if [[ -x "$MCU_MIGRATION_ROOT/.migration-tools/cargo/bin/cargo" ]]; then
  export CARGO_HOME="$MCU_MIGRATION_ROOT/.migration-tools/cargo"
  export RUSTUP_HOME="$MCU_MIGRATION_ROOT/.migration-tools/rustup"
  export PATH="$CARGO_HOME/bin:$PATH"
fi
if [[ -d "$MCU_MIGRATION_ROOT/.migration-tools/sysroot/usr/lib" ]]; then
  MCU_MIGRATION_MULTIARCH="$(gcc -print-multiarch)"
  export PKG_CONFIG_PATH="$MCU_MIGRATION_ROOT/.migration-tools/sysroot/usr/lib/$MCU_MIGRATION_MULTIARCH/pkgconfig:$MCU_MIGRATION_ROOT/.migration-tools/sysroot/usr/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  # GCC receives this value without splitting paths containing spaces. Some
  # pkg-config consumers split unescaped -L flags from the optional local SDK.
  export LIBRARY_PATH="$MCU_MIGRATION_ROOT/.migration-tools/sysroot/usr/lib/$MCU_MIGRATION_MULTIARCH${LIBRARY_PATH:+:$LIBRARY_PATH}"
fi
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-4}"
