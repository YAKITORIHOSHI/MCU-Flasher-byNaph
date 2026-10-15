#!/usr/bin/env bash
# Read native os-release with shell builtins, before even missing-Python rescue.
# Never source OS data as shell code or accept an environment release override.
require_supported_ubuntu() {
    local release_file=/etc/os-release
    [[ -e "$release_file" ]] || release_file=/usr/lib/os-release
    local line key value distro='' version='' seen_id=false seen_version=false
    local requirement='MCU Flasher requires Ubuntu 24.04 or newer.'
    if [[ ! -r "$release_file" ]]; then
        printf '%s\n' "Cannot read the installed Ubuntu release. $requirement" >&2
        return 1
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" == ID=* || "$line" == VERSION_ID=* ]] || continue
        key=${line%%=*}; value=${line#*=}
        if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
            value=${value:1:${#value}-2}
        fi
        if [[ "$key" == ID ]]; then
            if [[ "$seen_id" == true ]]; then distro=''; break; fi
            distro=$value; seen_id=true
        else
            if [[ "$seen_version" == true ]]; then version=''; break; fi
            version=$value; seen_version=true
        fi
    done < "$release_file"
    if [[ "$distro" != ubuntu ]]; then
        printf '%s\n' "Cannot identify this host as native Ubuntu. $requirement" >&2
        return 1
    fi
    if [[ ! "$version" =~ ^([0-9]{1,4})\.([0-9]{1,4})(\.[0-9]{1,4})?$ ]]; then
        printf '%s\n' "The installed Ubuntu VERSION_ID is missing or malformed. $requirement" >&2
        return 1
    fi
    local major=$((10#${BASH_REMATCH[1]})) minor=$((10#${BASH_REMATCH[2]}))
    if (( major < 24 || (major == 24 && minor < 4) )); then
        printf '%s\n' "$requirement" >&2
        return 1
    fi
    # Only publish parsed values after admission; inherited environment values
    # must never select a different release's terminal or recovery behavior.
    mcu_ubuntu_release_major=$major
    mcu_ubuntu_release_minor=$minor
}
