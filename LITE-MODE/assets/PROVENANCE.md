`lite.svg` and the action SVGs are original vector artwork for MCU Flasher Lite.
The PNG and multi-resolution Windows ICO are rasterized from `lite.svg`.
`sun.svg` and `moon.svg` are original vector controls for Light/Dark mode.

`boards.json` is the official PlatformIO board catalog retrieved on 2026-11-13
from https://api.platformio.org/v2/boards (1,656 catalog entries). It supplies
exact picker identities, not hardware detection or toolchain readiness. Installed
native board manifests take precedence. No board is automatically selected.

GUI framework API: egui/eframe 0.33.3, https://github.com/emilk/egui/tree/0.33.3.
Package installation follows PlatformIO 6.2's documented `pio pkg install` API:
https://docs.platformio.org/en/stable/core/userguide/pkg/cmd_install.html.
