#!/bin/bash
# Finder opens this in Terminal, which shows first-run progress and any refusal.
# Bash 3.2 syntax only: that is the bash macOS ships.
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
entry="$here/../direct/macos/run.sh"
if [[ ! -r "$entry" ]]; then
    printf '%s\n' 'Cannot locate direct/macos/run.sh. Keep the complete application folder together.' >&2
    status=1
else
    /bin/bash "$entry" "$@"
    status=$?
fi
if (( status != 0 )) && [[ -t 0 ]]; then
    read -r -p 'Press Enter to close this window.' || true
fi
exit "$status"
