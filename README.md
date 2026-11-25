# MCU Flasher by Naph v11

A desktop workspace for writing, compiling, flashing and monitoring
microcontroller firmware, with a code editor, serial monitor, terminals and an
optional AI assistant in one window.

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Ubuntu%20%7C%20Mint%20%7C%20Fedora%20%7C%20macOS-blue)
![Version](https://img.shields.io/badge/version-v11-brightgreen)
![Toolchain](https://img.shields.io/badge/toolchain-PlatformIO%20%2B%20Arduino%20CLI-orange)
![License](https://img.shields.io/badge/license-MIT-green)

[Release notes](RELEASE_NOTES.md) (what it does and contains) ·
[Launcher guide](LAUNCHER/README.md) · [Ubuntu guide](direct/UBUNTU.md) ·
[Mint, Fedora and macOS](direct/OTHER_PLATFORMS.md) · [Lite](LITE-MODE/README.md)

---

## Supported systems

| System | Architectures | Launcher |
| --- | --- | --- |
| Windows 10 / 11 | AMD64 | `LAUNCHER/WINDOWS.exe` |
| Ubuntu 24.04 or newer | amd64, ARM64 (incl. Raspberry Pi 4/5) | `LAUNCHER/UBUNTU`, `LAUNCHER/UBUNTU_ARM64` |
| Linux Mint 22.x / 23.x | amd64 | `LAUNCHER/LINUX_MINT` |
| Fedora 44 or newer | x86_64, aarch64 | `LAUNCHER/FEDORA`, `LAUNCHER/FEDORA_ARM64` |
| macOS 14 or newer | Intel, Apple Silicon | `LAUNCHER/MacOS.command` |

You need:

- A CPU with at least 4 cores (checked at startup).
- Free disk space for the default AVR / ESP32 / ESP8266 setup: about 10 GB on
  Linux or 15 GB on Windows; 20 GB is recommended.
- An internet connection for the first setup.
- No system Python on Windows or macOS. Linux uses the distribution's own
  Python 3.10 or newer.

Windows on ARM is not supported. USB upload on Linux Mint, Fedora and macOS
has not yet been verified on physical hardware.

## Download

1. Download `MCU_Flasher-v11.zip` from the
   [latest release](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/releases/latest).
2. Extract it to a folder you can write to, and keep the folder together.
3. Continue with [First launch](#first-launch).

The download already includes the private Windows Python runtime, so it is the
quickest way to start on Windows.

## Clone with Git

The Windows Python runtime and USB driver installers are stored with
[Git LFS](https://git-lfs.com). Install Git LFS before cloning, or Windows
setup will find only placeholder files.

```bash
git lfs install
git clone https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph.git
cd MCU-Flasher-byNaph
```

Run your system's launcher from the clone, for example `LAUNCHER\WINDOWS.exe`
on Windows or `./LAUNCHER/UBUNTU` on Ubuntu. On Windows, the first launch
installs the private Python runtime into `src\_python\` with winget or the
bundled installer. A clone is never updated automatically; run `git pull` to get the
latest version.

## First launch

1. Open the `LAUNCHER` folder and run the entry for your system (table above).
2. **Bootstrap** prepares a private Python runtime, the required packages, the
   board list and the default boards' tools (Arduino Uno, NodeMCU, ESP32 Dev
   Module), plus the CP210x and CH34x USB drivers on Windows, in its own setup window. Linux may ask for your password to install system packages; Windows asks for permission once, for the USB drivers.
   **Technical details** shows the installer output.
3. Choose or create a project, then select a board and serial port in the
   **Controls** bar. On Linux and macOS, the first **Compile** for any other
   board downloads that board's compiler and uploader once.

- **Ubuntu, Linux Mint and Fedora:** setup installs the application for your
  account in `~/.local/share/mcu-flasher/application` and adds **MCU Flasher**
  to All Apps (with **MCU Flasher Lite** on Ubuntu and Mint). Start it from there afterwards.
- **macOS:** the app is not notarized. Open a downloaded copy the first time
  with Control-click → **Open** (on macOS 15 or newer: **System Settings →
  Privacy & Security → Open Anyway**).

## Using MCU Flasher

| Task | How |
| --- | --- |
| Compile | **Compile** or Ctrl+R |
| Compile and upload | **Upload** or Ctrl+U (needs a board, and a port for serial uploads) |
| Save / Save All | Ctrl+S / Ctrl+Shift+S |
| Open or create a project | **Project** or Ctrl+O |
| Start a temporary sketch | **Ghost Sketch** in the project window; choose a folder when saving |
| Find in project | Ctrl+Shift+F |
| Go to definition | Ctrl+Click or F12 |
| Add, rename or delete files | The **+** chip and the context menu on source tabs |
| Watch serial output | The **Serial Monitor** tab; the baud rate follows `Serial.begin()` |
| Use an AI assistant | **AI Assistant**, then choose one from the list; **Download** gets one that is not there yet |
| Take an AI edit back | **AI Changes** → **Revert edit** or **Revert session** |
| Boards, libraries and examples | **Resources** |
| Recover a stuck board | **Settings** → **Soft Reset** or **Hard Reset** for the selected board |
| Show hidden toolbars | **View → Show All Toolbars** |

The window cannot be closed while firmware is being written, erased or
recovered, so a board is never left half-flashed. A serial port selected in one
window is shown as in use in the others.

Sketches can stay in writable SMB/network folders. Save, Save All and Auto-Save
replace complete source files even when the share does not support Unix
permissions. Failed saves keep the editor buffer available to retry.
Ghost Sketch can also save into a new or empty folder on a network share.

**Settings** covers the theme, interface size, editor behaviour, compiler
parallelism and serial options. To auto-save, enable **Auto-Save** and type a
delay from 500 to 10,000 ms. **Offline Mode** prepares every default board's
tools in advance and then uses local packages only.

**MCU Flasher Lite** is a lightweight workbench for Ubuntu and Windows. Open
`LITE-MODE/UBUNTU` or `LITE-MODE/WINDOWS.exe`. Use **Open sketch** to choose a
folder, then choose its exact board in **Connection**. Lite uses the
tools the main app prepared. See [Lite's guide](LITE-MODE/README.md).

## Updates

Downloaded and installed copies check GitHub on launch and install a newer
version before opening, without asking.

- Updates wait until every MCU Flasher window is closed.
- Sketches, settings, build caches and private runtimes are never replaced, and
  a failed update keeps the current version.
- To turn updates off, choose **Skip Updates** in the Windows Bootstrap window
  or set `MCU_FLASH_GUI_SKIP_UPDATES=1`.
- An update refreshes the application files only. For a newer Windows runtime,
  download the latest release ZIP.

## Troubleshooting

| Problem | Action |
| --- | --- |
| The app does not start, or reports missing components | Run the launcher with `--repair`, or choose **Repair** from the All Apps entry on Ubuntu |
| Check readiness without changing anything | `./LAUNCHER/UBUNTU --check` (Linux) |
| Repair the Windows runtime | `direct\windows\run.vbs --repair` |
| Compile or upload errors | The **Build Console** tab and the per-sketch history in **Notifications** |
| Upload cannot connect to an ESP board | Hold **BOOT** while the upload connects, then release |

Launch logs are written to the `logs` folder of the installation.

## Development

```text
main/        PySide6 interface (main/qt) and core services (main/core)
src/         Runtime modules, offline editor assets, fonts and icons
direct/      Platform launchers, setup scripts and hardware-free verifiers
LAUNCHER/    Native launcher entries
LITE-MODE/   Lite mode launcher entries
```

- From a clone, run the app with its private runtime:
  `.venv-linux/bin/python mcu_flash_gui.py` on Linux, or `LAUNCHER\WINDOWS.exe`
  on Windows.
- Run the hardware-free checks with `python direct/run_verifiers.py`.
- Contributor guidance lives in `.agents/skills/`. Work happens on `main`.
- Build release archives only with `direct/build_release.py`.
  Network checkouts are supported; ZIP permissions are verified independently
  of the share's support for host file modes.

## License

MIT. See [LICENSE](LICENSE).
