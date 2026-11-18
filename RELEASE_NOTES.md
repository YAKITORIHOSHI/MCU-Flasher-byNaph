# MCU Flasher by Naph v11 — Release Notes

**Version:** v11 (Stable) · **Download:**
[`MCU_Flasher-v11.zip`](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/releases/latest)

**Platforms:** Windows 10/11 (AMD64) · Ubuntu 24.04+ (amd64, ARM64) ·
Linux Mint 22.x/23.x (amd64) · Fedora 44+ (x86_64, aarch64) · macOS 14+ (Intel, Apple Silicon)

MCU Flasher is a desktop workspace for writing, compiling, flashing and
monitoring microcontroller firmware. A code editor, serial monitor, terminals
and an optional AI assistant share one window. To download, clone and use it,
see the [README](README.md).

---

## What it does

- **Build and flash.** Compiles and uploads with PlatformIO for AVR, ESP32,
  ESP8266 and other installed targets. Unchanged sketches reuse their last
  build, and every upload writes the complete firmware image set (on ESP32:
  bootloader, partition table and application).
- **Serial Monitor.** Opening, reconnecting and closing never reset the board.
  The baud rate follows the sketch's `Serial.begin()`, with timestamps, pause,
  copy and send. After an upload the boot log appears from its first line.
- **Editor.** An offline Monaco editor with source tabs, live syntax checks,
  bracket pairing, auto-save, Ctrl+Click / F12 into sketch and library headers,
  and project-wide search. Save and Auto-Save support writable SMB/network
  folders whose permissions are managed by the share.
- **Projects.** Local sketches, new-project templates, temporary Ghost Sketches
  and Cloud sketches with conflict-safe Push and Pull. Each project can open in
  its own window. Ghost Sketch saves support new and empty network folders,
  preserving the temporary original and refusing occupied destinations.
- **Terminals and AI assistant.** PowerShell and CMD on Windows, Bash on Linux,
  and an embedded OpenCode assistant whose file edits are highlighted for you
  to accept or reject. Once started, **Restart** beside its status opens a
  fresh session.
- **Boards and libraries.** Browse, download and prepare board packages and
  Arduino libraries, and read their example sketches. Offline Mode prepares
  every default board's tools in advance and uses local packages only.
- **Board recovery.** Soft and Hard Reset reflash a board to a known state,
  clearing lockups, boot loops and frozen sketches.
- **Safe hardware use.** A serial port belongs to the window that selected it;
  other MCU Flasher and Lite windows show it as in use. A window cannot close
  while firmware is being written, erased or recovered.
- **Setup and repair.** The first launch prepares a private Python runtime, the
  required packages, the board list and the default boards' tools (Arduino
  Uno, NodeMCU and ESP32 Dev Module), plus the CP210x and CH34x USB drivers on
  Windows, in its own setup window. Any other board's compiler and uploader
  download the first time you compile for it. A repair launch rebuilds
  anything missing.
- **Interface.** Liquid Glass surfaces (specular rims and edge lensing) and
  capsule actions in Glass Dark, Frosted Light and Solarized Dark themes, an
  automatic interface size, and layouts that fit monitors from 800×600 to 8K,
  including half-screen tiling.
- **MCU Flasher Lite.** A separate lightweight workbench for Ubuntu and Windows
  that shares the prepared tools, boards and libraries with the main app.
  Choosing a board configures its platform and default framework automatically.
- **Developer Access.** Private issue tickets shared across your signed-in
  devices.

## What the download contains

| Path | Contents |
| --- | --- |
| `LAUNCHER/` | One launcher per system: `WINDOWS.exe`, `UBUNTU`, `UBUNTU_ARM64`, `LINUX_MINT`, `FEDORA`, `FEDORA_ARM64` and `MacOS.command` |
| `mcu_flash_gui.py`, `main/`, `src/` | The application, its offline editor, fonts, icons and themes |
| `src/_python/` | The private Windows Python runtime and its packages, so Windows needs no system Python |
| `installers/` | Windows installers: CP210x and CH34x USB drivers, Microsoft WebView2, Arduino CLI, MSYS2 and Python |
| `LITE-MODE/` | MCU Flasher Lite for Ubuntu and Windows |
| `soft_reset/` | Recovery sketches for ESP32, ESP8266, AVR, STM32, RP2040, SAMD, nRF52, Renesas and Teensy boards |
| `index_json/` | Board package catalogs |
| `direct/` | Platform setup, launch and repair scripts, and hardware-free checks |
| `cleaner/`, `DANGER-ZONE/` | Maintenance tools: cache and dependency cleanup, runtime reset and crash diagnostics |

The archive holds project files at its root, without a wrapper folder. It
contains no sketches, settings, accounts or build caches.
Release archives can be built from network checkouts while retaining native
launcher permissions inside the ZIP.

## Requirements

- At least 4 CPU cores.
- Free space for the default AVR / ESP32 / ESP8266 setup: about 10 GB on Linux
  or 15 GB on Windows; 20 GB recommended.
- Internet access for the first setup.

## Known limitations

- Windows on ARM is not supported.
- USB upload on Linux Mint, Fedora and macOS has not yet been verified on
  physical hardware.
- macOS builds are not notarized; the first open needs Control-click → Open.
- Keyboard handoff on native Windows 11 has so far been verified only on hosted
  Windows CI.
