# 🛠️ MCU Flasher by Naph

> **A modern, dark-themed GUI tool for ESP32/Arduino development — compile, upload, and monitor serial output in one sleek, modular interface.**

![Version](https://img.shields.io/badge/version-V9.0-blue)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 📖 Table of Contents

- [✨ Features](#-features)
- [🚀 Quick Start](#-quick-start)
- [📁 Project Structure](#-project-structure)
- [📘 User Guide & How to Use](#-user-guide--how-to-use)
  - [1. Launching & First-Run Auto-Bootstrap](#1-launching--first-run-auto-bootstrap)
  - [2. Opening, Selecting & Scaffolding Projects](#2-opening-selecting--scaffolding-projects)
  - [3. Selecting Boards & COM Ports](#3-selecting-boards--com-ports)
  - [4. Compiling & Flashing Code](#4-compiling--flashing-code)
  - [5. Live Serial Monitor](#5-live-serial-monitor)
  - [6. Offline Monaco Code Editor](#6-offline-monaco-code-editor)
  - [7. Integrated Project Terminal (PowerShell ↔ CMD)](#7-integrated-project-terminal-powershell--cmd)
  - [8. OpenCode AI Assistant & Diff Glow](#8-opencode-ai-assistant--diff-glow)
  - [9. Soft Reset & Hard Reset Recovery Flashing](#9-soft-reset--hard-reset-recovery-flashing)
  - [10. Remote Network Shares (UNC Paths)](#10-remote-network-shares-unc-paths)
- [🧭 Architectural Reference: What is What & Which is Which](#-architectural-reference-what-is-what--which-is-which)
  - [Root Entry Points & Launchers](#root-entry-points--launchers)
  - [The Modern HTML Web UI & Python Backend Architecture](#the-modern-html-web-ui--python-backend-architecture)
  - [The `main/core/` Foundation Modules](#the-maincore-foundation-modules)
  - [The `src/` System Modules & Offline Assets](#the-src-system-modules--offline-assets)
  - [Caches, Installers & Templates](#caches-installers--templates)
- [⚙️ Configuration](#️-configuration)
- [🛠️ Development & Contributing](#️-development--contributing)

---

## ✨ Features

| Feature | Description |
| --- | --- |
| **🔨 One-Click Build & Flash** | Compile and upload to ESP32 / Arduino microcontrollers via Arduino CLI or PlatformIO |
| **🎯 Manual Hardware Selection** | Clean startup with manual board and port selection, giving full control over target microcontrollers without unexpected auto-switches |
| **🔌 Disconnect-Safe Uploads & Zero-Reset Connection** | Two-phase uploads tolerate unplugging, and passive serial connection with de-asserted DTR/RTS keeps running ESP32 firmware uninterrupted |
| **📟 Advanced Serial Monitor** | Real-time terminal with ANSI color rendering, timestamps, baud rate control, and instant MCU reset |
| **🎨 Modern Multi-Theme UI** | Dark Cyberpunk, Light Mode, and Solarized styling with Montserrat typography and responsive layout |
| **✏️ Offline Monaco Code Editor** | **Monaco Editor** (VS Code engine with C++ syntax highlighting, Go-To-Definition, hover cards, intelligent auto-saving) |
| **🤖 Dedicated AI Assistant** | Embedded OpenCode AI side panel with file-watcher, line-level diffing, and pulsating glowing diff highlights (green added, red removed) |
| **💻 Multi-Session Project Terminal** | Embedded terminal powered by pywinpty + xterm.js with VS Code-style multi-terminal tabs, **PowerShell ↔ CMD** creation, and individual session controls |
| **🌐 Remote & UNC Share Support** | Direct compilation & flashing of projects on network shares (`\\server\share`) with automated drive mapping and local SSD build acceleration |
| **⚡ Dual Reset Modes** | Fast software reset (re-flashing lightweight reset sketch) and native hardware reset via esptool DTR/RTS pulsing or bootloader recovery images |
| **🔄 On-Demand Board Toolchains** | Newly downloaded boards (ESP8266, STM32, RP2040) install and prepare toolchains on demand during first compile without requiring an application restart |
| **🍃 Supported-PC Optimization** | Dynamic CPU/RAM budgeting, below-normal process priority scheduling, and slow-disk watchdogs help keep the UI responsive on supported quad-core hardware |
| **📦 Zero-Touch Bootstrapper** | Self-healing Python environment, pre-built PlatformIO core seeding (~1.7GB fast download), and offline CP210x driver installation |

---

## 🚀 Quick Start

```cmd
# Double-click the native launcher:
MCU_Flasher.exe

# Or launch via the bootstrap script (normal startup does not require Administrator):
direct\runThisOnWindows.vbs
```

**First Run Pipeline:**
1. Verifies storage drive (SSD/HDD recommended for build speed).
2. Auto-heals private portable Python runtime at `src/_python/` if needed.
3. Configures isolated virtual environment (`env/`).
4. Seeds pre-built PlatformIO toolchain and Arduino CLI binaries.
5. Checks required CP210x USB UART drivers; Windows requests UAC only when a machine-level installation is needed.
6. Launches the main GUI seamlessly.

> [!NOTE]
> **Storage Requirement**: Initial installation requires approximately **6GB of starting storage** for core toolchains, compilers, and dependencies. Storage usage may increment as additional Arduino/PlatformIO libraries and board platforms are installed.

> **Hardware Requirement**: This app requires at least **4 logical CPU cores/threads**. On systems with fewer than 4, startup stops and displays a compatibility message because the editor, serial monitor, toolchain, and background services cannot operate reliably on the available CPU resources.

> [!TIP]
> The main window is revealed behind the loading cover while its UI components are laid out and painted. The cover is removed only after the layout is stable and the full startup pass has completed. Project scanning, editor file loading, port detection, and optional services run through the background worker system.
>
> MCU Flasher opens with no port or board pre-selected, giving you full control. An MCU already connected when the app opens is never reset on connection; reset happens only through the physical/on-app reset control or after an upload.

---

## 📁 Project Structure

Below is the complete, full architectural structure of the MCU Flasher ecosystem:

```
MCU Flasher by Naph/
├── MCU_Flasher.exe                   # Compiled native Windows launcher (from src/launcher.cs)
├── mcu_flash_gui.py                 # Root application entry point
├── README.md                         # Comprehensive documentation, user guide & architecture
│
├── main/                             # Python backend & PySide6 Qt GUI architecture
│   ├── __init__.py                  # Public exports (MCUWebBackendAPI, main)
│   ├── mcu_flash_gui.py             # PySide6 desktop application bootstrap & runtime lifecycle
│   ├── web_bridge.py                # Thread-safe Python backend engine & Qt signal bridge
│   │
│   ├── qt/                          # Native PySide6 (Qt for Python) Desktop UI Panels
│   │   ├── main_window.py           # MCUMainWindow root window with dock splitters
│   │   ├── toolbar.py               # Action controls, board/port selectors, baud rate
│   │   ├── editor_panel.py          # Monaco Editor panel (QWebEngineView)
│   │   ├── console_panel.py         # Colorized build and upload console output
│   │   ├── serial_panel.py          # Live serial monitor with baud selection & send bar
│   │   ├── compat_panel.py          # Compatible boards viewer
│   │   ├── terminal_panel.py        # Integrated project terminal
│   │   ├── ai_panel.py              # Collapsible OpenCode AI assistant panel
│   │   ├── settings_dialog.py       # Application preferences & theme configuration
│   │   ├── project_dialog.py        # New/Open project wizard dialog
│   │   ├── download_dialog.py       # Board & toolchain package download manager
│   │   ├── theme.py                 # Precision dark/light QSS stylesheet engine
│   │   └── signals.py               # Shared thread-safe Qt signal bus
│   │
│   └── core/                        # Core foundations & system services
│       ├── __init__.py              # Core package re-exports
│       ├── constants.py             # Global constants, regexes, baud rates, headers & telemetry
│       ├── theme.py                 # Theme class, color tokens, dark/light styling engine
│       ├── config.py                # Config persistence (gui_config.json), PID multi-instance locks
│       ├── file_utils.py            # Windows attributes (attrib +h), UNC path detection, AI backup store
│       ├── toolchain.py             # PlatformIO & Arduino CLI discovery, junctions & CPU worker count
│       ├── board_catalog.py         # 460+ board definitions, dynamic catalog loader, USB VID/PID map
│       └── board_compat.py          # Board compatibility detection & GPIO pin conflict analyzer
│
├── src/                              # Offline Monaco Editor, terminal engine & bootstrap modules
│   ├── editor/                      # Offline Monaco Editor (C/C++ language server, syntax coloring)
│   │   ├── index.html               # Monaco iframe host with pywebview/QWebChannel bridge polyfill
│   │   └── bundle.js                # Self-contained offline Monaco code editor
│   ├── assets/                      # Offline icons, fonts, xterm.js terminal engine
│   └── modules/                     # Unattended bootstrap pipeline & installers
│       ├── bootstrap.py             # Zero-config dependency installer & toolchain warm-up
│       ├── launcher.py              # Single-instance process coordinator & Defender helper
│       └── dedicated_AI.py          # Dedicated OpenCode AI assistant process controller
│
├── cleaner/                          # Maintenance utility scripts
│   ├── clean_pycache.bat            # Recursive __pycache__ cleaner
│   └── terminate_all_python.exe-task.bat  # Kill all Python processes
│
├── DANGER-ZONE/                      # Destructive reset utilities (use with caution)
│   ├── DELETE_EVERYTHING_DO_NOT_RUN.ps1   # Full environment teardown script
│   └── runReset.cmd                 # Reset launcher
│
├── direct/
│   └── runThisOnWindows.vbs         # Windows bootstrap launcher; targeted UAC only when required
│
├── src/                             # Core system modules, offline editor assets & storage
│   ├── _python/                     # Private portable Python 3 runtime (hidden via attrib +h)
│   ├── .platformio-mcu-gui/         # PlatformIO core store (junctioned to avoid MAX_PATH)
│   ├── gui_config.json              # Persisted user settings (editor mode, themes, baud rates)
│   ├── syntax_checker.py            # Realtime C++ syntax linter & AST regex analyzer
│   ├── qscintilla_editor.py         # QScintilla code editor component (PyQt5)
│   ├── qscintilla_viewer.py         # QScintilla sample-code viewer with adaptive filename tabs (PyQt5)
│   ├── launcher.cpp                 # Native Windows executable wrapper source (C++)
│   ├── launcher.cs                  # Native Windows executable wrapper source (C#)
│   ├── resources.res                # Compiled Windows resource file (icon embedding)
│   ├── resources.rc                 # Windows resource definition (icon embedding)
│   │
│   ├── modules/                     # System launchers, AI backend, terminal & bootstrap engine
│   │   ├── bootstrap.py             # Dependency bootstrapper & runtime auto-healer
│   │   ├── launcher.py              # Entry point launcher (AppUserModelID, single-instance lock)
│   │   ├── dedicated_AI.py          # OpenCode AI controller (pywebview + xterm.js + pywinpty)
│   │   ├── project_terminal.py      # Standalone project terminal server & PTY backend
│   │   ├── downloader.py            # Download utility with resume, progress & GDrive support
│   │   ├── arduino_lib_req.py       # Arduino library resolver & header dependency scanner
│   │   ├── detector.py              # USB serial port auto-detection & board probing
│   │   ├── win_subprocess_hide.py   # Windows CREATE_NO_WINDOW subprocess console suppressor
│   │   ├── reset_editor.py          # Editor state reset utility
│   │   ├── setup_ide_paths.py       # compile_commands.json path re-navigation
│   │   └── get-platformio.py        # Official bundled PlatformIO installer script
│   │
│   ├── editor/                      # Offline Monaco Editor Web UI assets
│   │   ├── index.html               # Monaco Editor HTML (tabs, toolbar, diff glow, hover, go-to-def)
│   │   ├── bundle.js                # Monaco Editor offline JS bundle (no CDN dependency)
│   │   ├── 18.bundle.js             # Monaco Editor async chunk (lazy-loaded features)
│   │   ├── editor.worker.js         # Monaco Editor web worker (tokenization & validation)
│   │   └── *.ttf                    # Offline editor icon and UI font assets
│   │
│   ├── assets/                      # Shared graphical assets
│   │   ├── mcu_icon.ico             # Application icon
│   │   └── xterm/                   # xterm.js assets for terminal and AI panels
│   │
│   ├── dbs/                         # Persistent JSON databases and CRUD managers
│   │   ├── __init__.py              # DB package initializer
│   │   ├── bootstrap_config.json    # Bootstrap update and skip configuration
│   │   ├── arduino_browser_settings.json  # Arduino board browser preferences
│   │   ├── arduino_cli_path.txt     # Cached Arduino CLI executable path
│   │   ├── dbs_notif.json           # Global notification fallback (projects use .mcu_flasher_build_cache/dbs_notif.json)
│   │   ├── dbs_create.py            # Notification DB CRUD: create (project-scoped or global)
│   │   ├── dbs_read.py              # Notification DB CRUD: read (project-scoped or global)
│   │   ├── dbs_update.py            # Notification DB CRUD: update (project-scoped or global)
│   │   └── dbs_delete.py            # Notification DB CRUD: delete (project-scoped or global)
│   │
│   └── fonts/                       # Bundled offline Montserrat and system fonts
│
├── installers/                      # Offline installer binaries & drivers (tracked via Git LFS)
│   ├── .handsoff/                   # Portable Python runtime installers (python-*-amd64.exe)
│   ├── CP210x/                      # Silicon Labs USB-to-UART drivers
│   ├── arduino-cli.msi              # Bundled Arduino CLI installer
│   ├── MicrosoftEdgeWebview2Setup.exe # Bundled Microsoft Edge WebView2 installer
│   └── msys2-*.exe                  # Bundled MSYS2 build tools
│
├── soft_reset/                      # App-owned reset templates and exact-board caches
│   ├── soft_reset_project/          # PlatformIO reset template for non-AVR boards
│   └── soft_reset_project_uno/      # PlatformIO reset template for Arduino UNO / AVR
├── index_json/                      # Arduino board and library index caches
├── .mcu_flasher_build_cache/        # Isolated per-board build cache & workspaces (gitignored, hidden)
└── logs/                            # Runtime diagnostic logs & lock files
```

---

## 📘 User Guide & How to Use

### 1. Launching & First-Run Auto-Bootstrap
- Launch via **`MCU_Flasher.exe`** (or **`direct\runThisOnWindows.vbs`**).
- The bootstrapper handles missing dependencies, Python packages (`pyserial`, `pywebview`, `pywinpty`), and the bundled AVR/ESP32 toolchains unattended. Additional downloaded board families are installed automatically on first compile or upload, keeping startup fast.
- The window is revealed behind the loading cover while its components are laid out. The cover is removed only after the layout is stable and the full startup pass is complete; project loading, editor materialization, port detection, serial monitoring, and optional services run through the background worker system.
- Normal startup and monitoring run with the current user permission. Windows shows a UAC prompt only when a missing machine-level component, such as a driver, genuinely requires it.
- If launched from source in a developer terminal:
  ```powershell
  python mcu_flash_gui.py
  # or
  python main/mcu_flash_gui.py
  ```

### 2. Opening, Selecting & Scaffolding Projects
- Click **`📂 Select Project`** on the toolbar or choose from recent projects.
- **Reselect a Project**: Right-click the current project name or folder icon to open the sketch/project picker. Choosing **Cancel** safely closes the native dialog and leaves the editor and monitor usable. The folder/new-project button opens the normal project selector and scaffolding flow.
- **New Project Scaffolding**: Click **`✨ New Project`**, enter a project name, and MCU Flasher creates a structured project directory with boilerplate `.ino`, header inclusions, and ready-to-build configuration.
- **Modify Project Files**: Click **`📝 Modify Files`** to add, rename, or delete sketch files (`.ino`, `.cpp`, `.h`).

### 3. Selecting Boards & COM Ports
- **Manual Selection on Startup**: MCU Flasher opens with no port or board pre-selected (`""`), giving you complete manual control over target hardware and preventing unintended connections or resets.
- **COM Port Selection**: The top-right dropdown enumerates all active serial ports along with their hardware descriptions (e.g. `COM9  -  USB-SERIAL CH340 (COM9)`). Clicking the dropdown opens the list immediately, allowing you to select your target port.
- **Zero-Reset ESP32 Protection**: Opening or selecting a serial port establishes the connection with DTR and RTS strictly de-asserted (`conn.dtr = False`, `conn.rts = False` before opening). Background `esptool` probing is disabled, guaranteeing that running firmware on an attached ESP32 continues running smoothly without an unintended reset.
- **Board Catalog Search**: Click **`🔍 Search Boards`** to open the search modal and filter through 420+ supported microcontrollers by keyword, architecture, or manufacturer.
- **Action Button Gating**:
  - **`⚙ Compile`**: Enabled as soon as a target board is selected (compilation is board-only and does not require an attached MCU).
  - **`⚡ Upload` & `↺ Reset MCU`**: Enabled once both a recognized board and an active COM port are selected.
- **Baud Rate & Upload Speed**: The Serial Monitor defaults to `74880` for ESP8266-family boards, `115200` for ESP32-family boards, and `9600` for AVR boards. ESP8266/ESP32-family boards automatically start uploads at `460800`; upload speed remains a separate setting (up to `921600` baud for ultra-fast uploads).
- **Additional Board Manager URLs**: Open **`⬇ Download Boards/Libraries`**, enter one or more vendor package-index URLs in **Additional board manager URLs** (comma-separated), then click **`⟳ Apply & Refresh`**. The default Arduino index is always included, and HTTP(S), GitHub raw/blob, redirects, stale-cache fallback, checksum verification, and ZIP/tar package archives are handled dynamically so indexes such as the ESP8266 package catalog can be used alongside it.

### 4. Compiling & Flashing Code
- **Compile Only (`🔨 Compile`)**:
  - Compiles your project using the selected toolchain (PlatformIO or Arduino CLI).
  - *Non-blocking*: Serial Monitor remains active and streaming while compiling!
  - Caches build artifacts in `.mcu_flasher_build_cache/` for near-instant incremental builds.
  - **On-Demand Toolchain Setup (Zero App Restart)**: Newly downloaded board families (e.g. ESP8266, STM32, RP2040) automatically install and verify compilers on demand during first compile with live progress bars. When finished, compilation proceeds immediately without restarting the desktop app!
  - **Supported-PC Optimization**: Dynamically measures available physical RAM and CPU cores, caps compiler jobs on supported quad-core/budget PCs, and schedules background processes with `BELOW_NORMAL_PRIORITY_CLASS` to help keep the UI and serial monitor responsive.
- **Upload (`⚡ Upload`)**:
  - Compiles (if changes were made) and flashes the binary to the MCU.
  - Automatically pauses the Serial Monitor during the upload phase to prevent port conflicts, then auto-resumes the monitor once flashing finishes.
- **Stop (`🛑 Stop`)**: Cancels an ongoing compilation, upload, or resets a hung serial session.

### 5. Live Serial Monitor
- View real-time MCU serial output in the bottom **Serial Monitor** tab.
- **Controls**:
  - **Timestamps**: Toggle inline timestamp prefixes.
  - **Pause / Resume**: Freeze scrollback to inspect logs without dropping incoming data.
  - **Send Bar**: Send text commands or newline-terminated strings to the MCU.
  - **Auto-Clear**: Configure automatic clearing on new compile or upload runs.

### 6. Offline Monaco Code Editor
- **Monaco Editor (VS Code Engine)**:
  - Rich editor embedded offline via pywebview (WebView2).
  - Features: Multi-tab editing, C++ autocomplete, F12 / Ctrl+Click Go-To-Definition, Ctrl+Hover documentation cards, debounced auto-saving, and real-time syntax checking.
  - Keyboard shortcuts: `Ctrl+R` to Compile, `Ctrl+U` to Upload, `Ctrl+S` to Save All, `Ctrl+` / `Ctrl-` to zoom.

### 7. Integrated Project Terminal (VS Code Style Multi-Session)
- Click the **`💻 Project Terminal`** tab in the bottom notebook.
- **VS Code Style Multi-Terminal Management**:
  - **`[▾]` New Shell Selector**: Choose between **PowerShell (`pwsh`)** and **Command Prompt (`cmd`)** to spawn a new terminal.
  - **Dynamic Session Tab Bar**: Tab chips for every active session with active status highlighting and individual **`✕`** close buttons.
  - **`[⌧ Clear]`**: Clears the active terminal viewport (`Clear-Host` for PowerShell, `cls` for CMD).
  - **`[🗑 Kill]`**: Destroys the active terminal session, freeing its background PTY worker.
- **Zero-Session Idle State**: Starts cleanly with zero open sessions and seamlessly transitions to an idle placeholder when all sessions are closed.
- **Subprocess ConPTY Architecture**: Powered by pywinpty + xterm.js in an isolated process to prevent GUI thread contention.

### 8. OpenCode AI Assistant & Diff Glow
- Click the **`🤖 AI Assistant`** button on the toolbar to open the embedded AI side panel.
- Ask questions, generate Arduino code, or request refactorings.
- **Live AI Diff & Glow**: When the AI modifies files in your sketch, Monaco Editor automatically detects the change, reloads the file, and highlights modifications with pulsating glowing animations:
  - 🟢 **Green Glow** on added / edited lines.
  - 🔴 **Red Glow** on removed lines.
  - Floating banner with quick **"Dismiss Glow ✖"** button.

### 9. Soft Reset & Hard Reset Recovery Flashing
- **Soft Reset**: Flashes a minimal lightweight Arduino-framework routine through the selected board's PlatformIO definition. It is available to resolved Arduino/PlatformIO boards, including future families that do not have a Hard Reset handler.
- **Hard Reset**: Uses an explicit board-family capability handler: ESP32 recovery images, ESP8266 full SPI-flash erase, or the existing AVR bootloader path. Other MCUs are refused safely instead of receiving an incompatible erase command.
- Opening or switching projects never performs an implicit MCU reset. Reset remains an explicit physical/on-app action or part of the upload flow.

### 10. Remote Network Shares (UNC Paths)
- Open projects directly from network storage (e.g. `\\nas\projects\iot_sensor`).
- MCU Flasher mounts a temporary drive letter dynamically during compilation and routes intermediate `.o` object files to your local SSD (`remote_workspaces/`), avoiding Samba locking errors.

---

## 🧭 Architectural Reference: What is What & Which is Which

### Root Entry Points & Launchers

- **`MCU_Flasher.exe`**: Native C# wrapper compiled from `src/launcher.cs`. Starts `direct\runThisOnWindows.vbs` silently without forcing Administrator permission.
- **`direct\runThisOnWindows.vbs`**: Windows VBScript bootstrapper that verifies drive storage type and calls `src/modules/launcher.py`; the bootstrap requests targeted UAC only for a specific machine-level setup task that needs it.
- **`mcu_flash_gui.py`**: Root forwarder script that delegates directly to `main.mcu_flash_gui` for backward compatibility.

---

### The Native PySide6 (Qt for Python) Desktop Architecture

- **`mcu_flash_gui.py`**: Standalone native desktop launcher. Configures high-DPI scaling, taskbar grouping (`naph.mcuflasher.gui.v3`), minimum 4 CPU cores verification, single-instance mutex locking, and initializes the native PySide6 `QApplication`.
- **`main/qt/`**: Modular PySide6 interface suite:
  - **`main_window.py`**: Host window containing toolbar, resizable QSplitter panes, bottom dock tabs (Serial Monitor, Build Console, Terminal, Compatible Devices), and status bar.
  - **`toolbar.py`**: Action controls (Compile, Flash, Clean, Soft/Hard Reset, Project, Settings), target board combobox, COM port selector, and baud rate selector.
  - **`editor_panel.py`**: Embedded offline Monaco Code Editor hosted via `QWebEngineView` with real-time bidirectional Python-JS communication.
  - **`console_panel.py`**: Colorized build output with regex ANSI parsing and autoscroll.
  - **`serial_panel.py`**: Real-time serial monitor with line endings, baud rate selector, and quick command sender.
  - **`terminal_panel.py`**: Embedded interactive xterm.js terminal engine.
  - **`ai_panel.py`**: Collapsible OpenCode AI assistant side panel.
  - **`settings_dialog.py` / `project_dialog.py` / `download_dialog.py`**: Native Qt modal dialogs.
- **`main/web_bridge.py` (`MCUWebBackendAPI`)**: Thread-safe backend controller coordinating background compilations, COM port serial streaming, telemetry, board catalog search, and project operations via Qt signals (`main.qt.signals.signals`).
- **`src/editor/index.html` & `bundle.js`**: Offline Monaco Editor engine embedded natively via `QWebEngineView` with full C/C++ syntax coloring and intelligent auto-save.

---

### The `main/core/` Foundation Modules

- **`main/core/constants.py`**: Immutable constants, regexes for ANSI sequences (`ANSI_CSI_RE`, `_ESPTOOL_*_RE`), default baud rates, C++ standard headers, and startup telemetry.
- **`main/core/theme.py`**: `Theme` class holding all color tokens, fonts, and dark mode theme definitions.
- **`main/core/config.py`**: Manages `src/gui_config.json`, single-instance mutex locks (`_claim_gui_instance`), occupied COM port locks, and recent projects list.
- **`main/core/file_utils.py`**: Low-level Windows file operations (`attrib +h`, `ensure_file_writable`, `robust_rmtree`), UNC share detection (`is_unc_or_network_path`), and AI review history backup (`AIEditBackupStore`).
- **`main/core/toolchain.py`**: Discovery and verification of PlatformIO core, Arduino CLI, directory junctions (`C:\.platformio-mcu-gui`), and CPU worker allocations.
- **`main/core/board_catalog.py`**: Dynamic board catalog parser (merging PlatformIO and Arduino index boards), USB VID/PID table, and esptool output parsers.
- **`main/core/board_compat.py`**: Heuristic board compatibility analyzer and pinout GPIO conflict checker.

---

### The `src/` System Modules & Offline Assets

- **`src/modules/bootstrap.py`**: Windows runtime bootstrapper with upscale HTML/Edge WebView2 window that self-heals Python, installs dependencies, downloads prebuilt PlatformIO core, and launches the GUI.
- **`src/modules/launcher.py`**: Entry point launcher setting Windows `AppUserModelID` for taskbar grouping and single-instance locking.
- **`src/modules/dedicated_AI.py`**: Controller managing OpenCode AI assistant sessions (HTTP server + WebSocket + pywinpty).
- **`src/modules/project_terminal.py`**: Full-featured integrated project terminal subprocess with ConPTY backend.
- **`src/modules/win_subprocess_hide.py`**: Enforces `CREATE_NO_WINDOW` on background subprocesses.
- **`src/modules/arduino_lib_req.py`**: Resolves required C++ headers and auto-downloads missing Arduino libraries.
- **`src/modules/detector.py`**: USB serial port auto-detection and board identification helper.
- **`src/modules/downloader.py`**: Multi-threaded downloader with resume, SHA-256 validation, and Google Drive virus-scan bypass.
- **`src/editor/`**: Offline Monaco Editor Web UI (`index.html`, `bundle.js`, offline fonts — zero CDN dependency).
- **`src/dbs/`**: Persistent JSON notification store and event database.

---

### Caches, Installers & Templates

- **`installers/`**: Bundled offline installers (`CP210x/` USB drivers, `arduino-cli.msi`, `MicrosoftEdgeWebview2Setup.exe`, `msys2-*.exe`). Tracked via Git LFS.
- **`soft_reset/soft_reset_project/`**: Pre-configured minimal PlatformIO workspace for non-AVR boards and exact-board reset caches.
- **`soft_reset/soft_reset_project_uno/`**: Pre-configured minimal workspace for Arduino AVR (Uno/Nano/Mega) boards and exact-board reset caches. Legacy top-level reset folders are migrated automatically when their board/platform IDs match.
- **`.mcu_flasher_build_cache/`**: Generated build artifacts and per-board workspaces (gitignored, hidden via Windows `attrib +h`).
- **`logs/`**: Runtime crash and diagnostic logs.

---

## ⚙️ Configuration

Preferences are persisted in `src/gui_config.json`:

```json
{
  "theme": "dark",
  "baud_rate": 115200,
  "serial_port": "COM3",
  "board": "esp32:esp32:esp32",
  "programmer": "esptool",
  "shared": {
    "editor_mode": "monaco",
    "autosave_enabled": true,
    "autosave_delay": 2000,
    "auto_clear_serial_on_upload": true
  }
}
```

---

## 🛠️ Development & Contributing

### Requirements
- Windows 10 / 11 (SSD / HDD recommended)
- **4+ logical CPU cores/threads** (required; systems below this minimum are blocked at startup)
- Python 3.10+
- **6GB+** starting storage for toolchains and platform packages
- Git LFS (`git lfs install`) for large binaries

### Verification & Syntax Checking
```powershell
# Verify syntax compilation across the entire modular package:
python -m py_compile main/mcu_flash_gui.py

# Launch GUI in development mode:
python main/mcu_flash_gui.py
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

> Made with ❤️‍🔥 by **Naph** — Happy flashing! 🚀
