# MCU Flasher by Naph v11 — Release Notes

**Version:** v11 (Stable) · **Download:**
[`MCU_Flasher-v11.zip`](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/releases/latest/download/MCU_Flasher-v11.zip)

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
  bootloader, partition table and application). The console lists Libraries,
  Target Board, Compile Time, Actual Board and Upload Time, with one-line
  progress for flashing and repeat full builds. Actual Board shows what the
  connected board reports for every family: ESP chips their model, features,
  flash, crystal and MAC; AVR boards (Uno, Nano, Mega and others) their chip by
  signature, its flash, SRAM and EEPROM, and the bootloader version; any USB board
  its bridge or USB chip and serial number, with the board's clock and memory.
  ESP boards upload at 115200 to 2000000 (2000000 asks first, since some USB
  adapters cannot keep up); other boards, such as the Uno, keep their own speed.
- **Serial Monitor.** Opening, reconnecting and closing never reset the board, and
  a board that is unplugged and plugged back in is picked up again.
  The baud menu is the Arduino IDE's (300 to 2000000) plus 1500000, and the rate follows the
  sketch's `Serial.begin()`, with timestamps, pause,
  copy and send. Firmware can color its output with the full 16-color ANSI
  palette, text and backgrounds, normal and bright. After an upload the boot log
  appears from its first line, and runs of unreadable bytes are summarized.
- **Editor.** An offline Monaco editor with source tabs, bracket pairing,
  auto-save, Ctrl+Click / F12 into sketch and library headers and project-wide
  search. Code is checked by compiling: the Build Console shows each error with
  its file and line. Save and Auto-Save support writable SMB/network
  folders whose permissions are managed by the share. With Auto-Save on, edits
  save themselves; a Ghost Sketch offers only Save, which chooses its folder.
- **Projects.** Local sketches, new-project templates, temporary Ghost Sketches
  and Cloud sketches with conflict-safe Push and Pull. Each project can open in
  its own window. Ghost Sketch saves support new and empty network folders,
  preserving the temporary original and refusing occupied destinations.
- **Terminals and AI assistant.** PowerShell and CMD on Windows, Bash on Linux,
  and the coding assistant you choose: OpenCode (built in), Claude, Codex,
  Antigravity or Hermes, downloaded only when you ask for it. One click on a tool
  starts it, and right-click offers to uninstall a downloaded one, keeping its
  settings and sign-ins. The list shows only while the computer is online and
  returns by itself when it reconnects; its ⓘ opens a disclaimer stating that each
  tool belongs to its own maker, not to MCU Flasher. On Linux and macOS,
  Hermes and the tools it brings (about 3 GB) stay inside the app's `.ai-agents`
  folder. Each assistant on the computer gets the sketch's instructions and skills
  where it reads them, and Settings sizes the assistant's text. Its file edits are
  highlighted for you to accept or reject; AI Changes gives each assistant session
  its own section, groups its edits by prompt and names the assistant that made
  them, and an edit, a prompt or a whole session can be reverted later. Beside its status, one button returns to the list and
  **Restart** opens a fresh session.
- **Notifications and devices.** Both are cards with a short line and details on
  request.
- **Boards and libraries.** Browse, download and prepare board packages and
  Arduino libraries, and read their example sketches. Each board is listed once.
  Offline Mode prepares every default board's tools in advance and uses local
  packages only.
- **Board recovery.** Soft and Hard Reset reflash a board to a known state,
  clearing lockups, boot loops and frozen sketches.
- **Safe hardware use.** A serial port belongs to the window that selected it;
  other MCU Flasher and Lite windows show it as in use. A window cannot close
  while firmware is being written, erased or recovered.
- **Setup and repair.** The first launch prepares a private Python runtime, the
  required packages, the board list and the default boards' tools (Arduino
  Uno, NodeMCU and ESP32 Dev Module), plus the CP210x and CH34x USB drivers on
  Windows, in its own setup window, and adds MCU Flasher to the Windows Start menu
  so Windows Search finds it. Any other board's compiler and uploader
  download the first time you compile for it. Windows components come from winget
  where it offers them, and a normal start asks for no permission. A repair launch
  rebuilds anything missing. If setup is ended part-way (for example from Task
  Manager), the next start stops what it left running, returns the boards and tools
  to the last safe point and continues, resuming partial downloads; a failed board
  step is retried once the same way.
- **Interface.** Liquid Glass in each theme's own material: white frost held by a
  steel edge in Frosted Light (the default), smoked blue glass in Glass Dark and teal
  sea glass in Solarized Dark, with caught light along the top of every sheet and
  button and the theme's accent along the bottom. The AI review bar and the editor
  follow the same glass, as do the project selector with its Cloud sketches and
  Settings, and the assistant
  list shows each tool's own icon. Tooltips are the theme's glass too. Also an
  automatic interface size, and layouts that fit monitors from 800×600 to 8K,
  including half-screen tiling.
- **MCU Flasher Lite.** A separate lightweight workbench for Ubuntu and Windows
  that shares the prepared tools, boards and libraries with the main app.
  Open sketches refresh after external saves. Upload checks the latest saved
  source bytes after tool preparation before reusing a build.
  Sketch and board pickers keep their confirmation actions visible on small
  windows. Choosing a board configures its platform and framework automatically.
  Sample code opens beside its list, and Lite reopens as it was left.
- **Developer Access.** Private issue tickets shared across your signed-in
  devices, with aligned search and filter fields and evenly spaced count tiles.

## What the download contains

| Path | Contents |
| --- | --- |
| `LAUNCHER/` | One launcher per system: `WINDOWS.exe`, `UBUNTU`, `UBUNTU_ARM64`, `LINUX_MINT`, `FEDORA`, `FEDORA_ARM64` and `MacOS.command` |
| `mcu_flash_gui.py`, `main/`, `src/` | The application, its offline editor, fonts, icons and themes |
| `src/_python/` | The private Windows Python runtime and its packages, so Windows needs no system Python |
| `installers/` | Windows CP210x and CH34x USB drivers and the Microsoft WebView2 installer |
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
