#!/bin/bash
# This is an availability notice, not an application or installer.
printf '%s\n' 'MCU Flasher for macOS is not available in this release.' \
    'The native macOS application, firmware tools and packaging are still being implemented.' \
    'This file does not install or launch the Windows or Ubuntu application.'
if [[ -t 0 ]]; then
    read -r -p 'Press Enter to close this notice.' || true
fi
exit 1
