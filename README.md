# MCU Flasher by Naph v11

A desktop workspace for writing, compiling, flashing and monitoring
microcontroller firmware, with an offline code editor, serial monitor,
terminals and an optional AI assistant in one window.

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Ubuntu%20%7C%20Mint%20%7C%20Fedora%20%7C%20macOS-blue)
![Version](https://img.shields.io/badge/version-v11-brightgreen)
![Toolchain](https://img.shields.io/badge/toolchain-PlatformIO%20%2B%20Arduino%20CLI-orange)
![License](https://img.shields.io/badge/license-MIT-green)

[Release notes](RELEASE_NOTES.md) · [Launcher guide](LAUNCHER/README.md) ·
[Ubuntu guide](direct/UBUNTU.md) · [Mint, Fedora and macOS](direct/OTHER_PLATFORMS.md)

---

## Features

- **Build and flash:** PlatformIO compiles and uploads for AVR, ESP32, ESP8266
  and other installed targets. Unchanged sketches can reuse their last build
  (**Skip Compile**), and every upload writes the complete firmware image set.
- **Serial Monitor:** Opening, reconnecting and closing never reset the board.
  The baud rate follows the sketch's `Serial.begin()`, with timestamps, pause,
  copy and send. After an upload the boot log appears from its first line.
- **Editor:** Offline Monaco editor with source tabs, live syntax checks,
  Ctrl+Click / F12 navigation into sketch and library headers, project-wide
  search (Ctrl+Shift+F) and auto-save.
- **Projects:** Local sketches, new-project templates, temporary Ghost Sketches
  and Cloud sketches synced to your account. Each project can open in its own
  window.
- **Terminals and AI:** PowerShell and CMD on Windows, Bash on Linux, plus an
  embedded OpenCode assistant with a quiet loading screen and no elapsed-time
  counter. Its file edits are highlighted for you to accept or reject.
- **Developer Access:** Private issue tickets shared across your signed-in
  devices. New tickets appear after **Create ticket** succeeds; cancelled drafts
  add nothing. The main workspace stays usable while the portal, its forms or
  notices are open. Deletions sync across devices, including an empty ticket list.
  Laptop layouts keep filters together; short windows scroll complete controls.
- **Resources:** Browse, download and prepare board packages and Arduino
  libraries. Soft and Hard Reset recovery reflash a board to a known state.
- **Lite:** A separate lightweight workbench with shared Bootstrap preparation,
  one shared board-tool and library store, installed library/sample browsing,
  and a native read-only code viewer. Prepared packages are used directly by both
  apps; older Lite duplicates are verified and consolidated automatically.
  See [Lite](LITE-MODE/README.md).
- **Interface:** Glass Dark, Frosted Light and Solarized Dark themes, an
  automatic interface size, and layouts that fit monitors from 800×600 to 8K,
  including half-screen tiling.

## Supported systems

| System | Architectures | Launcher entry |
| --- | --- | --- |
| Windows 10 / 11 | AMD64 | `LAUNCHER/WINDOWS.exe` |
| Ubuntu 24.04 or newer | amd64, ARM64 (incl. Raspberry Pi 4/5) | `LAUNCHER/UBUNTU`, `LAUNCHER/UBUNTU_ARM64` |
| Linux Mint 22.x | amd64 | `LAUNCHER/LINUX_MINT` |
| Fedora 44 or newer | x86_64, aarch64 | `LAUNCHER/FEDORA`, `LAUNCHER/FEDORA_ARM64` |
| macOS 14 or newer | Intel, Apple Silicon | `LAUNCHER/MacOS.command` |

Windows on ARM is not supported. USB upload on Linux Mint, Fedora and macOS
has not yet been verified on physical hardware; see the
[platform guide](direct/OTHER_PLATFORMS.md).

**Requirements**

- A CPU with at least 4 cores (checked at startup).
- Free disk space for the default AVR / ESP32 / ESP8266 setup: about 10 GB on
  Linux or 15 GB on Windows; 20 GB is recommended.
- An internet connection for the first setup.
- No system Python is needed on Windows or macOS. Linux uses the
  distribution's own Python 3.10 or newer.

## Getting started

1. Download `MCU_Flasher-v11.zip` from the
   [latest release](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/releases/latest)
   and extract it. Keep the folder together.
2. Open `LAUNCHER` and run the entry for your system (table above).
3. The first launch runs **Bootstrap**. **Technical details** shows installer
   output; the same button changes to **Activity summary** to switch back.
   Bootstrap prepares a private Python runtime,
   the required packages, the default board support and, on Windows, the
   CP210x and CH34x USB drivers. Linux may ask for your password to install
   system packages.
4. Choose or create a project, then select a board and serial port in the
   Controls bar.

On Ubuntu, Bootstrap installs the application for your account
(`~/.local/share/mcu-flasher/application`) and adds **MCU Flasher** to All
Apps; launch it from there afterwards. Later repairs and updates appear in the
same Bootstrap window as on Windows; only the very first setup uses a terminal. On macOS, the first open needs
Control-click → **Open**, because the app is not notarized.

**Lite:** `LITE-MODE/` contains the separate Rust workbench for Ubuntu and Windows.
Open its `UBUNTU` or `WINDOWS.exe` launcher; it reuses the prepared Python tools.
Lite's board picker follows the main app's rows and confirmation controls; its
five-click Developer Access opens the existing authenticated portal.
Stop stays visible with reason-specific enablement, and narrow windows switch
between the monitor and scrollable Sketch & Connection controls. Like the main
app, Lite opens each sketch in its own window. A serial port selected in either
app is marked in use in the other and cannot be selected there. On Ubuntu, the
installed app also lists MCU Flasher Lite in All Apps.
See [Lite's guide](LITE-MODE/README.md). Bootstrap includes Lite and future public
application folders automatically: checkouts use tracked files, and release ZIPs
carry a generated source manifest. Private settings, caches and build scratch
remain excluded from installation sources.

## Everyday use

| Task | How |
| --- | --- |
| Compile | **Compile** or Ctrl+R |
| Compile and upload | **Upload** or Ctrl+U (needs a board, and a port for serial uploads) |
| Save / Save All | Ctrl+S / Ctrl+Shift+S |
| Open or create a project | **Project** or Ctrl+O |
| Find in project | Ctrl+Shift+F |
| Go to definition | Ctrl+Click or F12 |
| Add, rename or delete files | The **+** chip and the context menu on source tabs |
| Show hidden toolbars | **View → Show All Toolbars** |

The window cannot be closed while firmware is being written, erased or
recovered, so a board is never left half-flashed.

## Updates

On each launch, MCU Flasher checks GitHub for a newer version and installs it
before opening, without asking first. Only the files that changed are
downloaded; files already installed are reused. Progress is shown in the
Bootstrap window with a bar, the transfer rate and the elapsed time. Closing the
window during a download skips that update and opens the installed version.

- Updates wait until every MCU Flasher window is closed.
- Sketches, settings, build caches and private runtimes are never replaced.
- An update never installs an older version. A failed update leaves the current
  version in place.
- Two launches at once share one download instead of each fetching the update.
- Updates are on by default. **Skip Updates** in the Windows Bootstrap window,
  or the environment variable `MCU_FLASH_GUI_SKIP_UPDATES=1`, turns them off.
- A Git checkout is never updated automatically; use `git pull`.

## Settings

**Settings** covers the theme, interface size, editor behaviour (auto-save,
bracket pairing), compiler parallelism and serial options. Preferences are
saved per user in `.mcu_gui_config.json`. **Offline Mode** prepares boards and
libraries only from local packages; it does not block Cloud sketches or other
network features.

## Troubleshooting

| Problem | Action |
| --- | --- |
| The app does not start, or reports missing components | Run the launcher with `--repair`, or choose **Repair** from the All Apps entry on Ubuntu |
| Check readiness without changing anything | `./LAUNCHER/UBUNTU --check` (Linux) |
| Repair the Windows runtime | `direct\windows\run.vbs --repair` |
| Compile or upload errors | The **Build** tab and the per-sketch history in **Notifications** |
| Upload cannot connect to an ESP board | Hold **BOOT** while the upload connects, then release |

Launch logs are written to the `logs` folder of the installation.

## Development

```text
main/        PySide6 interface (main/qt) and core services (main/core)
src/         Runtime modules, offline editor assets, fonts and icons
direct/      Platform launchers, setup scripts and hardware-free verifiers
LAUNCHER/    Native launcher entries
```

- Run the application from a checkout with its private runtime:
  `.venv-linux/bin/python mcu_flash_gui.py` on Linux, or `LAUNCHER\WINDOWS.exe`
  on Windows.
- Run the hardware-free checks with `python direct/run_verifiers.py`. They mock
  hardware and persistence, and never open windows on your desktop.
- Contributor and assistant guidance lives in `.agents/skills/`. Platform
  verification status is recorded in
  [audit/PLATFORM_VERIFICATION.md](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/blob/main/audit/PLATFORM_VERIFICATION.md).
- Build public archives only with `direct/build_release.py`.

## License

MIT. See [LICENSE](LICENSE).
