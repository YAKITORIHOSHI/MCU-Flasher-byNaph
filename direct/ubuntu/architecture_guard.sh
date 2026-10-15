#!/usr/bin/env bash
# Keep this early admission independent of Python, including first-run rescue.
# Python preflight independently checks its interpreter word size.
require_supported_ubuntu_architecture() {
    local machine
    machine=$(uname -m)
    case "${machine,,}" in
        x86_64|amd64) mcu_ubuntu_architecture=amd64 ;;
        aarch64|arm64) mcu_ubuntu_architecture=arm64 ;;
        *)
            printf '%s\n' 'MCU Flasher requires 64-bit Ubuntu on Intel/AMD (amd64) or ARM (arm64/aarch64). Use a 64-bit Ubuntu desktop image on Raspberry Pi.' >&2
            return 1
            ;;
    esac
    # A 64-bit kernel alone does not prove a 64-bit desktop/userspace. Ubuntu's
    # glibc getconf is available before Python and must not authorize armhf.
    local bits
    bits=$(getconf LONG_BIT) || {
        printf '%s\n' 'Cannot determine the Ubuntu userspace word size. MCU Flasher requires a 64-bit Ubuntu desktop.' >&2
        return 1
    }
    if [[ "$bits" != 64 ]]; then
        printf '%s\n' 'MCU Flasher requires a 64-bit Ubuntu desktop userspace. The 32-bit armhf/ARMv7 Raspberry Pi images are unsupported.' >&2
        return 1
    fi
}
