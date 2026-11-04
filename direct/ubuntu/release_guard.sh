#!/usr/bin/env bash
# Read native os-release with shell builtins, before even missing-Python rescue.
# Never source OS data as shell code or accept an environment release override.
# Each accepted distribution is an explicit policy that mirrors
# src/modules/ubuntu_release.py; ID_LIKE never admits a host.
require_supported_ubuntu() {
    local release_file=/etc/os-release
    [[ -e "$release_file" ]] || release_file=/usr/lib/os-release
    local line key value distro='' version='' codename=''
    local seen_id=false seen_version=false seen_codename=false
    local requirement='MCU Flasher requires Ubuntu 24.04 or newer.'
    local mint_requirement='MCU Flasher requires Linux Mint 22.x, based on Ubuntu 24.04.'
    local fedora_requirement='MCU Flasher requires Fedora 44 or newer.'
    local any_requirement='MCU Flasher requires Ubuntu 24.04 or newer, Linux Mint 22.x or Fedora 44 or newer.'
    if [[ ! -r "$release_file" ]]; then
        printf '%s\n' "Cannot read the installed Linux release. $any_requirement" >&2
        return 1
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" == ID=* || "$line" == VERSION_ID=* || "$line" == UBUNTU_CODENAME=* ]] || continue
        key=${line%%=*}; value=${line#*=}
        if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
            value=${value:1:${#value}-2}
        fi
        case "$key" in
            ID)
                if [[ "$seen_id" == true ]]; then distro=''; break; fi
                distro=$value; seen_id=true ;;
            VERSION_ID)
                if [[ "$seen_version" == true ]]; then version=''; break; fi
                version=$value; seen_version=true ;;
            UBUNTU_CODENAME)
                if [[ "$seen_codename" == true ]]; then codename=''; break; fi
                codename=$value; seen_codename=true ;;
        esac
    done < "$release_file"
    local major=0 minor=0 label=''
    case "$distro" in
        ubuntu)
            label='Ubuntu'
            if [[ ! "$version" =~ ^([0-9]{1,4})\.([0-9]{1,4})(\.[0-9]{1,4})?$ ]]; then
                printf '%s\n' "The installed Ubuntu VERSION_ID is missing or malformed. $requirement" >&2
                return 1
            fi
            major=$((10#${BASH_REMATCH[1]})); minor=$((10#${BASH_REMATCH[2]}))
            if (( major < 24 || (major == 24 && minor < 4) )); then
                printf '%s\n' "$requirement" >&2
                return 1
            fi
            ;;
        linuxmint)
            label='Linux Mint'
            if [[ ! "$version" =~ ^([0-9]{1,4})(\.[0-9]{1,4}){0,2}$ ]]; then
                printf '%s\n' "The installed Linux Mint VERSION_ID is missing or malformed. $mint_requirement" >&2
                return 1
            fi
            # A Mint release is defined by its Ubuntu base. LMDE has no such base.
            if (( 10#${BASH_REMATCH[1]} != 22 )) || [[ "$codename" != noble ]]; then
                printf '%s\n' "$mint_requirement" >&2
                return 1
            fi
            major=24; minor=4
            ;;
        fedora)
            label='Fedora'
            if [[ ! "$version" =~ ^[0-9]{1,4}$ ]]; then
                printf '%s\n' "The installed Fedora VERSION_ID is missing or malformed. $fedora_requirement" >&2
                return 1
            fi
            if (( 10#$version < 44 )); then
                printf '%s\n' "$fedora_requirement" >&2
                return 1
            fi
            ;;
        *)
            printf '%s\n' "Cannot identify this host as a supported Linux desktop. $any_requirement" >&2
            return 1
            ;;
    esac
    # Only publish parsed values after admission; inherited environment values
    # must never select a different release's terminal or recovery behavior.
    # Fedora publishes no Ubuntu release, leaving these at zero.
    mcu_linux_family=$distro
    mcu_linux_label=$label
    mcu_ubuntu_release_major=$major
    mcu_ubuntu_release_minor=$minor
}
