# 🛠️ MCU Flasher by Naph

> **A modern, high-performance Windows desktop application for ESP32, ESP8266, and Arduino microcontrollers — compile, flash, monitor, and code in one unified, modular interface.**

![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue)
![Architecture](https://img.shields.io/badge/architecture-PySide6%20%7C%20Qt%20for%20Python-41CD52)
![Python](https://img.shields.io/badge/python-3.10%2B%20(Private%20Runtime)-blue)
![Toolchain](https://img.shields.io/badge/toolchain-PlatformIO%20%2B%20Arduino%20CLI-orange)
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
  - [5. Live Serial Monitor & Post-Upload Auto-Reset](#5-live-serial-monitor--post-upload-auto-reset)
  - [6. Critical Operation Protection & Safe Shutdown](#6-critical-operation-protection--safe-shutdown)
  - [7. Offline Monaco Code Editor](#7-offline-monaco-code-editor)
  - [8. Multi-Session Project Terminal (PowerShell ↔ CMD)](#8-multi-session-project-terminal-powershell--cmd)
  - [9. OpenCode AI Assistant & Pulsating Diff Glow](#9-opencode-ai-assistant--pulsating-diff-glow)
  - [10. Soft Reset & Hard Reset Recovery Flashing](#10-soft-reset--hard-reset-recovery-flashing)
  - [11. Remote Network Shares (UNC Paths)](#11-remote-network-shares-unc-paths)
- [🧭 Architectural Reference: What is What & Which is Which](#-architectural-reference-what-is-what--which-is-which)
  - [Root Entry Points & Launchers](#root-entry-points--launchers)
  - [The Native PySide6 (Qt) Desktop Architecture & Web Bridge Engine](#the-native-pyside6-qt-desktop-architecture--web-bridge-engine)
  - [The `main/core/` Foundation Modules](#the-maincore-foundation-modules)
  - [The `src/modules/` System Services & Runtime Guards](#the-srcmodules-system-services--runtime-guards)
  - [Offline Monaco Editor & Web Assets](#offline-monaco-editor--web-assets)
  - [Caches, Installers & Reset Templates](#caches-installers--reset-templates)
- [⚙️ Configuration](#️-configuration)
- [🛠️ Development & Contributing](#️-development--contributing)
- [📄 License](#-license)

---

## ✨ Features

| Feature | Description |
| --- | --- |
| **🖥️ Native PySide6 (Qt) Desktop UI** | High-performance, hardware-accelerated desktop interface built with PySide6 (`main/qt/`), featuring Cyberpunk Dark, Clean Light, and Solarized Dark themes with Montserrat typography and responsive splitters. |
| **🔨 Unified One-Click Build & Flash** | Dual toolchain backend (PlatformIO SCons engine + Arduino CLI) for ESP32, ESP8266, and Arduino AVR microcontrollers with incremental caching in `.mcu_flasher_build_cache/`. |
| **🛡️ Critical Operation Protection** | Safeguards against closing the application during sensitive hardware writes (flashing, flash erasing, bootloader recovery resets, and toolchain downloads) to prevent bricking microcontrollers or corrupting installations. |
| **📟 Advanced Serial Monitor & Auto-Reset Parity** | Real-time terminal with ANSI color rendering, timestamps, pause/resume, send bar, and high-throughput batch coalescing. Automatically issues a silent DTR/RTS pulse upon upload completion and focuses the monitor after a 500ms grace delay. |
| **⚡ Reset on Baud Change Toggle** | Configurable setting in Settings Dialog to automatically pulse DTR/RTS when switching baud rates (rebooting MCU into `setup()` at the new baud rate) or maintain uninterrupted execution. |
| **🔌 Zero-Reset Connection & Port Safety** | Passive port opening with explicitly de-asserted DTR/RTS control lines ensures connected ESP32 microcontrollers continue running active firmware without unintentional reboots. |
| **✏️ Offline Monaco Code Editor** | Embedded offline Monaco Editor (VS Code engine) via `QWebEngineView` and `QWebChannel` featuring C/C++ syntax highlighting, Go-To-Definition (`F12`), hover cards, and debounced auto-saving with zero CDN dependencies. |
| **🤖 Dedicated AI Assistant & Diff Glow** | Embedded OpenCode AI assistant with real-time file watcher, line-level LCS diffing, and animated pulsating diff glows (🟢 green added, 🔴 red removed) with a floating quick-dismiss banner. |
| **💻 Multi-Session Project Terminal** | Embedded terminal powered by `pywinpty` + `xterm.js` with VS Code-style multi-terminal tabs, live **PowerShell (`pwsh`) ↔ CMD** creation, per-session viewport controls, and clear/kill options. |
| **🔄 On-Demand Board Toolchains** | Newly downloaded board packages (ESP8266, STM32, RP2040) automatically install and configure required compilers on demand during first build with live progress tracking, requiring **zero application restarts**. |
| **🌐 Dynamic Third-Party Board Manager URLs** | Download Manager supports custom vendor package index URLs (HTTP/HTTPS, GitHub raw/blob, redirects) with archive unpacking and local caching. |
| **📁 Remote Network Share (UNC) Support** | Seamless compilation and flashing of sketches stored on Windows SMB network shares (`\\server\share`) with automatic drive mapping and local SSD build acceleration. |
| **🔒 Strict Private Python Runtime Guard** | `private_python_guard.py` ensures the entire application runs strictly on the isolated bundled Python runtime in `src/_python/`, eliminating conflicts or leaks with desktop/system Python. |
| **🚨 Session Sentinel & Crash Detection** | `crash_detector.py` provides automatic unhandled exception logging, session sentinel tracking, and startup crash recovery. |
| **🍃 Low-End Hardware Optimization** | Dynamic CPU core and RAM budgeting, `BELOW_NORMAL_PRIORITY_CLASS` subprocess scheduling, and generous silence watchdogs keep the UI responsive even on budget quad-core systems. |

---

## 🚀 Quick Start

### Launching the Application

```cmd
# Double-click the compiled native Windows launcher:
MCU_Flasher.exe

# Or launch via the bootstrap launcher (runs under standard user privileges):
direct\runThisOnWindows.vbs

# Or run directly using the private Python environment in a terminal:
python mcu_flash_gui.py
```

### First-Run Auto-Bootstrap Pipeline
1. **Drive Storage Verification**: Inspects installation drive for storage speed and integrity.
2. **Private Python Auto-Healing**: Verifies and heals the isolated portable Python 3 runtime at `src/_python/` using bundled offline installers in `installers/.handsoff/`.
3. **Virtual Environment Isolation**: Configures and validates required dependencies (`PySide6`, `pyserial`, `pywebview`, `pywinpty`).
4. **Pre-Built Toolchain Seeding**: Seeds the pre-built PlatformIO core (~1.7GB fast download with resume & SHA-256 verification) and Arduino CLI binaries.
5. **Driver Verification**: Detects Silicon Labs CP210x and CH34x USB UART drivers; Windows displays a UAC prompt only if a missing machine-level driver installation is strictly required.
6. **GUI Launch**: Boots the native PySide6 desktop interface with smooth layout transition.

> [!NOTE]
> **Storage Requirement**: Initial installation requires approximately **6GB of starting storage** for core toolchains, compilers, and dependencies. Storage usage may increment as additional Arduino/PlatformIO libraries and board platforms are installed.

> [!IMPORTANT]
> **Hardware Requirement**: This application requires at least **4 logical CPU cores/threads**. On systems with fewer than 4 logical cores, startup halts with a clear system compatibility message to prevent system freezing.

> [!TIP]
> **Manual Hardware Gating**: MCU Flasher launches cleanly with no board and no COM port pre-selected. An MCU already connected when the app opens is never reset on connection; reset happens only through the physical/on-app reset control or after an upload.

---

## 📁 Project Structure

Below is the complete architectural layout of the MCU Flasher ecosystem:

```
MCU Flasher by Naph/
├── MCU_Flasher.exe                   # Native Windows launcher (compiled from src/launcher.cs)
├── mcu_flash_gui.py                 # Root application entry point forwarder
├── README.md                         # Comprehensive documentation, user guide & architecture
├── LICENSE                           # MIT license terms
│
├── main/                             # Native PySide6 (Qt) Desktop UI & Backend Engine
│   ├── __init__.py                  # Public exports (MCUWebBackendAPI, main)
│   ├── mcu_flash_gui.py             # PySide6 application lifecycle, core checks & window bootstrapper
│   ├── web_bridge.py                # Centralized thread-safe backend engine & toolchain controller
│   │
│   ├── qt/                          # Native PySide6 (Qt for Python) Desktop UI Panels
│   │   ├── __init__.py              # Qt package initializer
│   │   ├── main_window.py           # MCUMainWindow root window with resizable splitters & dock tabs
│   │   ├── toolbar.py               # Action controls, board/port dropdowns, baud selector & telemetry
│   │   ├── editor_panel.py          # Embedded Monaco Code Editor host (QWebEngineView + QWebChannel)
│   │   ├── console_panel.py         # Colorized build and upload console output with ANSI regex parsing
│   │   ├── serial_panel.py          # Real-time serial monitor with baud control, send bar & timestamp
│   │   ├── terminal_panel.py        # Embedded multi-session terminal panel (PowerShell & CMD tabs)
│   │   ├── ai_panel.py              # Collapsible OpenCode AI assistant panel with session management
│   │   ├── compat_panel.py          # Compatible boards and MCU pinout compatibility viewer
│   │   ├── syntax_panel.py          # Real-time C++ syntax checker diagnostic tree
│   │   ├── notif_panel.py           # Per-project notification and event log viewer
│   │   ├── detached_editor.py       # Independent popped-out Monaco Editor window wrapper
│   │   ├── settings_dialog.py       # Preferences modal (themes, CPU jobs, auto-save, baud reset)
│   │   ├── project_dialog.py        # Project selector & new project scaffolding wizard
│   │   ├── modify_dialog.py         # Project sketch file management dialog (add, rename, delete)
│   │   ├── download_dialog.py       # Board package & toolchain download manager (3rd-party URLs)
│   │   ├── theme.py                 # Multi-theme QSS stylesheet generator (Cyberpunk, Light, Solarized)
│   │   └── signals.py               # Centralized QtSignalBus for thread-safe cross-thread event routing
│   │
│   └── core/                        # Core Foundations & System Services
│       ├── __init__.py              # Core package re-exports
│       ├── constants.py             # Global constants, regex patterns, baud rates, headers & telemetry
│       ├── theme.py                 # Theme color tokens, font definitions, and dark/light styling rules
│       ├── config.py                # Config persistence (gui_config.json) & multi-instance PID locks
│       ├── file_utils.py            # Windows attributes (attrib +h), UNC path detection, robust file I/O
│       ├── toolchain.py             # Toolchain discovery, directory junctions & CPU worker budgeting
│       ├── board_catalog.py         # 460+ board definitions, dynamic catalog loader, USB VID/PID map
│       └── board_compat.py          # Board compatibility detection & GPIO pin conflict analyzer
│
├── src/                              # Core System Modules, Offline Assets & Runtime Guards
│   ├── _python/                     # Private portable Python 3 runtime (isolated, attrib +h)
│   ├── .platformio-mcu-gui/         # PlatformIO core store (junctioned to avoid MAX_PATH issues)
│   ├── gui_config.json              # Persisted user settings (themes, baud rates, auto-save, CPU mode)
│   ├── syntax_checker.py            # Standalone C++ syntax linter & AST analyzer
│   ├── launcher.cs                  # Native Windows C# launcher source
│   ├── launcher.cpp                 # Native Windows C++ executable wrapper source
│   ├── resources.rc                 # Windows executable resource definition (icon embedding)
│   │
│   ├── modules/                     # System Services, Bootstrap Pipeline & Execution Drivers
│   │   ├── bootstrap.py             # Windows runtime bootstrapper & unattended dependency installer
│   │   ├── launcher.py              # Application coordinator & Windows AppUserModelID configurator
│   │   ├── private_python_guard.py  # Strict private Python runtime enforcer (halts system Python leaks)
│   │   ├── crash_detector.py        # Session sentinel, crash event recorder & unhandled exception hook
│   │   ├── dedicated_AI.py          # OpenCode AI assistant process controller & file watcher
│   │   ├── project_terminal.py      # Standalone project terminal server & PTY backend
│   │   ├── arduino_lib_req.py       # C++ header dependency scanner & automatic library downloader
│   │   ├── detector.py              # USB serial port auto-detection & board probing
│   │   ├── downloader.py            # Resumable downloader with SHA-256 verification & GDrive support
│   │   ├── win_subprocess_hide.py   # Windows CREATE_NO_WINDOW background subprocess suppressor
│   │   ├── reset_editor.py          # Editor state reset utility
│   │   ├── setup_ide_paths.py       # Dynamic compile_commands.json path re-navigator
│   │   └── get-platformio.py        # Bundled official PlatformIO installer script
│   │
│   ├── editor/                      # Offline Monaco Editor Web Engine (Zero CDN Dependency)
│   │   ├── index.html               # Monaco iframe host with QWebChannel bridge & diff animation CSS
│   │   ├── bundle.js                # Self-contained offline Monaco code editor engine
│   │   ├── qwebchannel.js           # Bidirectional Qt-to-JavaScript communication bridge
│   │   ├── 18.bundle.js             # Monaco Editor async chunk (lazy-loaded features)
│   │   ├── editor.worker.js         # Monaco Editor web worker (tokenization & validation)
│   │   └── *.ttf                    # Bundled offline editor icons and font assets
│   │
│   ├── assets/                      # Shared Graphics & UI Assets
│   │   ├── mcu_icon.ico             # Application icon
│   │   ├── icons/                   # Custom SVG/PNG checkboxes and control assets
│   │   └── xterm/                   # xterm.js terminal web distribution
│   │
│   └── dbs/                         # Persistent JSON Databases & Notification Store
│       ├── bootstrap_config.json    # Bootstrap update, check, and skip configuration
│       ├── arduino_browser_settings.json  # Board browser user preferences
│       ├── arduino_cli_path.txt     # Cached Arduino CLI binary path
│       ├── dbs_notif.json           # Global notification fallback store
│       ├── dbs_create.py            # Notification DB CRUD: create
│       ├── dbs_read.py              # Notification DB CRUD: read
│       ├── dbs_update.py            # Notification DB CRUD: update
│       └── dbs_delete.py            # Notification DB CRUD: delete
│
├── direct/
│   └── runThisOnWindows.vbs         # Silent VBScript launcher; targeted UAC only when required
│
├── installers/                      # Offline Binaries, Toolchains & USB Drivers (Git LFS)
│   ├── .handsoff/                   # Portable Python runtime installers (python-*-amd64.exe)
│   ├── CP210x/                      # Silicon Labs CP210x USB-to-UART driver installer
│   ├── CH34x/                       # WCH CH340 / CH341 USB serial driver installer
│   ├── arduino-cli.msi              # Bundled Arduino CLI installer
│   ├── MicrosoftEdgeWebview2Setup.exe # Bundled Microsoft Edge WebView2 installer
│   └── msys2-*.exe                  # Bundled MSYS2 build tools
│
├── soft_reset/                      # App-Owned Reset Templates & Exact-Board Caches
│   ├── soft_reset_project/          # Minimal PlatformIO reset template for ESP32 / non-AVR boards
│   └── soft_reset_project_uno/      # Minimal PlatformIO reset template for Arduino Uno / AVR boards
│
├── cleaner/                         # Maintenance Utilities
│   ├── clean_fresh.bat              # Workspace refresh cleaner
│   ├── clean_pycache.bat            # Recursive Python cache cleaner
│   └── terminate_all_python.exe-task.bat # Kill background Python processes
│
├── DANGER-ZONE/                     # Destructive Reset Utilities (Use with Caution)
│   ├── DELETE_EVERYTHING_DO_NOT_RUN.ps1 # Full environment teardown script
│   └── runReset.cmd                 # Reset launcher
│
├── index_json/                      # Board and library index caches
├── .mcu_flasher_build_cache/        # Per-board build caches and workspaces (hidden, gitignored)
└── logs/                            # Runtime diagnostic logs, crash dumps & mutex locks
```

---

## 📘 User Guide & How to Use

### 1. Launching & First-Run Auto-Bootstrap
- Launch the application by double-clicking **`MCU_Flasher.exe`** (or running **`direct\runThisOnWindows.vbs`**).
- **Private Python Runtime Enforcer**: Handled transparently by `src/modules/private_python_guard.py`. The app will only ever run inside `src/_python/` and will auto-relaunch if triggered via system Python.
- **Session Sentinel & Crash Detection**: `src/modules/crash_detector.py` monitors runtime integrity, providing unhandled exception logging and clean startup recovery markers.
- The bootstrapper verifies required dependencies (`PySide6`, `pyserial`, `pywebview`, `pywinpty`) and toolchains unattended. Additional downloaded board platforms are prepared on demand during first compile, keeping initial startup instant.
- Normal startup and serial monitoring operate with current user permissions; Windows prompts for UAC elevation only when a missing driver or system component strictly requires it.

### 2. Opening, Selecting & Scaffolding Projects
- Click **`📂 Select Project`** on the toolbar to choose an existing sketch directory.
- **Quick Reselect**: Right-click the project title or folder icon to re-open the sketch picker. Clicking **Cancel** closes the dialog cleanly without interrupting active editor or monitor sessions.
- **New Project Scaffolding**: Click **`✨ New Project`**, enter a project name, and MCU Flasher creates a structured project directory with boilerplate code, standard header inclusions, and ready-to-build configuration.
- **Modify Project Files**: Click **`📝 Modify Files`** to create new files, rename existing files, or delete sketch files (`.ino`, `.cpp`, `.h`).

### 3. Selecting Boards & COM Ports
- **Manual Hardware Selection**: MCU Flasher opens with no board and no port pre-selected (`""`). You retain full control over target hardware, preventing unintentional flashing or port locking.
- **Active COM Port Enumeration**: The port dropdown enumerates all active serial ports with hardware descriptions (e.g. `COM9 - USB-SERIAL CH340 (COM9)`). Selecting a port connects instantly.
- **Zero-Reset ESP32 Protection**: Serial ports open with DTR and RTS explicitly de-asserted (`conn.dtr = False`, `conn.rts = False`). Background `esptool` probing is disabled, ensuring running firmware on an attached ESP32 continues running smoothly without an unintended reset.
- **Board Catalog Search**: Click **`🔍 Search Boards`** to open the search modal and filter through 460+ microcontrollers by name, architecture, or manufacturer.
- **Additional Board Manager URLs**: Open **`⬇ Download Boards/Libraries`**, enter third-party package index URLs in **Additional board manager URLs**, and click **`⟳ Apply & Refresh`**. The engine dynamically parses HTTP/HTTPS URLs, GitHub raw/blob links, handles redirects, caches metadata, and downloads package archives.

### 4. Compiling & Flashing Code
- **Compile Only (`🔨 Compile`)**:
  - Compiles the sketch using the PlatformIO SCons engine or Arduino CLI.
  - *Non-Blocking Execution*: The Serial Monitor remains active, streaming, and fully interactive during compilation!
  - Caches intermediate objects in `.mcu_flasher_build_cache/boards/<board-key>/` for near-instant incremental rebuilds.
  - **On-Demand Toolchain Setup (Zero App Restart)**: Newly downloaded board packages (e.g. ESP8266, STM32, RP2040) automatically install and verify their compilers on demand during first compile with live progress bars. When finished, compilation proceeds immediately without restarting the application!
  - **Resource-Aware Throttling**: Checks physical RAM and logical CPU cores via `psutil`. Background subprocesses are scheduled with `BELOW_NORMAL_PRIORITY_CLASS` (`0x00004000`), ensuring the UI, Monaco editor, and serial monitor stay fully responsive even during heavy compiles.
- **Upload Firmware (`⚡ Upload`)**:
  - Compiles modified files and flashes the binary to the microcontroller.
  - Features unit-aware byte parsing for modern `esptool` v5.4.0+ outputs.
  - Automatically pauses the Serial Monitor during the write phase to release port contention, then auto-resumes monitoring once flashing finishes.
- **Stop Operation (`🛑 Stop`)**: Cancels an active compilation, upload, or resets a hanging serial session.

### 5. Live Serial Monitor & Post-Upload Auto-Reset
- View real-time MCU serial output in the bottom **Serial Monitor** tab.
- **Upload & Reset Parity**:
  - Upon a successful upload, hard reset, or soft reset, the application automatically switches the bottom view to the **Serial Monitor** after a 500ms grace delay.
  - After upload completion, MCU Flasher issues an automatic silent DTR/RTS reset pulse, guaranteeing the MCU reboots into user application mode and starts streaming boot logs immediately.
  - **Error Visibility Retention**: If an operation fails (`success=False`), the focus strictly remains on the **Build Console** so error traces stay visible and actionable.
- **Reset on Baud Change Toggle**:
  - Configurable in the Settings Dialog (`reset_on_baud_change`).
  - When enabled, switching baud rates triggers a hardware DTR/RTS pulse so microcontroller `setup()` re-runs at the new speed.
  - When disabled (default), baud rates switch cleanly without resetting the microcontroller.
- **High-Throughput Optimization**:
  - Batch chunk coalescing, queue backlog clamping, and smart timestamp bypass eliminate GUI lag during high baud rate streaming (up to 921,600 / 2,000,000 baud).

### 6. Critical Operation Protection & Safe Shutdown
- The application actively protects against closing the window or interrupting sensitive operations:
  - **Toolchain / Framework Downloads**: Prevents closing while downloading core packages to avoid corrupting the PlatformIO installation.
  - **Firmware Uploads & Writing**: Prevents closing while firmware is actively being written to flash memory, preventing unbootable or corrupted MCU states.
  - **Flash Erasing**: Blocks closing during full chip erase operations to prevent half-erased chip lockouts.
  - **Hardware / Soft Resets**: Protects bootloader and recovery write sequences.
  - **Cache Cleaning**: Ensures directory purges finish cleanly.
- If a close attempt is made during these critical operations, a descriptive warning dialog explains what is running and safely ignores the close event.
- Pure compilation (`op == "compile"`) is safely cancellable upon closing.

### 7. Offline Monaco Code Editor
- **Monaco Editor (VS Code Engine)**:
  - Embedded offline via `QWebEngineView` and `QWebChannel` (`src/editor/qwebchannel.js`).
  - 100% offline: zero CDN dependencies, local bundled scripts, web workers, and font assets.
  - Features: Multi-tab editing with drag reordering, C++ autocomplete, F12 / Ctrl+Click Go-To-Definition, Ctrl+Hover documentation cards, and real-time syntax checking.
  - Debounced auto-saving (customizable delay in Settings).
  - Keyboard shortcuts: `Ctrl+R` to Compile, `Ctrl+U` to Upload, `Ctrl+S` to Save All, `Ctrl+` / `Ctrl-` to zoom.

### 8. Multi-Session Project Terminal (PowerShell ↔ CMD)
- Click the **`💻 Project Terminal`** tab in the bottom dock.
- **VS Code Style Multi-Terminal Management**:
  - **`[▾]` New Shell Selector**: Choose between **PowerShell (`pwsh`)** and **Command Prompt (`cmd`)** to spawn a new shell.
  - **Dynamic Session Tab Bar**: Tab chips for every active terminal session with active state highlighting and individual close buttons (**`✕`**).
  - **`[⌧ Clear]`**: Clears the active terminal viewport (`Clear-Host` for PowerShell, `cls` for CMD).
  - **`[🗑 Kill]`**: Destroys the active terminal session, terminating the background PTY worker.
- **Zero-Session Idle State**: Starts cleanly with zero open sessions and seamlessly transitions to an idle placeholder when all sessions are closed.
- **PTY Subprocess Architecture**: Powered by `pywinpty` + `xterm.js` in an isolated process to prevent UI thread contention.

### 9. OpenCode AI Assistant & Pulsating Diff Glow
- Click the **`🤖 AI Assistant`** button on the toolbar to open the embedded AI side panel.
- Chat with OpenCode, generate code, or request refactoring.
- **Live AI Diff & Pulsating Glow**: When the AI modifies files in your sketch, Monaco Editor automatically detects the change, reloads the file, and highlights modifications with pulsating glowing animations:
  - 🟢 **Green Glow** on added / modified lines.
  - 🔴 **Red Glow** on removed lines.
  - Floating banner with line count summaries and a quick **"Dismiss Glow ✖"** button.

### 10. Soft Reset & Hard Reset Recovery Flashing
- **Soft Reset**: Flashes a minimal lightweight Arduino-framework routine through the selected board's PlatformIO definition. Available for all supported boards to reset flash state.
- **Hard Reset**: Executes board-family specific capability routines: ESP32 recovery images, ESP8266 full SPI-flash erase, or AVR bootloader recovery. Unsupported microcontrollers are refused safely.

### 11. Remote Network Shares (UNC Paths)
- Open and compile projects directly from Windows network storage (e.g. `\\server\share\sketch`).
- MCU Flasher dynamically maps a temporary drive letter during build execution and routes intermediate object files to local fast SSD storage (`remote_workspaces/`), eliminating SMB `.sconsign*.dblite` locking errors.

---

## 🧭 Architectural Reference: What is What & Which is Which

### Root Entry Points & Launchers

- **`MCU_Flasher.exe`**: Native C# wrapper compiled from `src/launcher.cs`. Starts `direct\runThisOnWindows.vbs` silently without prompting for unnecessary Administrator elevation.
- **`direct\runThisOnWindows.vbs`**: Windows VBScript bootstrapper that checks drive storage type and launches `src/modules/launcher.py`. Requests targeted UAC elevation only for machine-level driver installations.
- **`mcu_flash_gui.py`**: Root entry point forwarder. Enforces private Python execution via `private_python_guard.py`, installs console-hiding hooks, and delegates directly to `main.mcu_flash_gui.main()`.

---

### The Native PySide6 (Qt) Desktop Architecture & Web Bridge Engine

- **`main/mcu_flash_gui.py`**: Standalone native desktop launcher. Configures high-DPI scaling, Windows taskbar grouping (`naph.mcuflasher.gui.v3`), minimum 4 CPU cores verification, single-instance mutex locking, session tracking via `crash_detector.py`, and initializes the native PySide6 `QApplication`.
- **`main/web_bridge.py` (`MCUWebBackendAPI`)**: Centralized, thread-safe asynchronous backend engine. Coordinates compiler pipelines, COM port serial streaming, telemetry, board catalog search, and project operations, routing all state changes to the UI via `QtSignalBus`.
- **`main/qt/`**: Modular PySide6 interface suite:
  - **`main_window.py`**: Root window with action toolbar, resizable `QSplitter` containers, dockable bottom panels (Serial Monitor, Build Console, Terminal, Compatible Devices, Syntax Diagnostics, Notifications), and status bar.
  - **`toolbar.py`**: Primary control bar (Compile, Upload, Clean, Reset, Project, Settings), target board dropdown, COM port selector, baud rate selector, and status indicators.
  - **`editor_panel.py`**: Embedded offline Monaco Code Editor hosted via `QWebEngineView` with real-time bidirectional communication via `QWebChannel`.
  - **`console_panel.py`**: Colorized build output with regex ANSI color parsing and autoscroll.
  - **`serial_panel.py`**: High-performance real-time serial monitor with line ending selector, baud rate dropdown, timestamp toggling, and quick send bar.
  - **`terminal_panel.py`**: Multi-session integrated terminal panel supporting PowerShell and CMD tabs.
  - **`ai_panel.py`**: Collapsible OpenCode AI assistant side panel.
  - **`syntax_panel.py`**: Interactive AST syntax diagnostic tree with line jump navigation.
  - **`compat_panel.py`**: Board compatibility matrix and GPIO pinout inspector.
  - **`notif_panel.py`**: Per-sketch notification log viewer.
  - **`settings_dialog.py` / `project_dialog.py` / `download_dialog.py` / `modify_dialog.py`**: Native Qt modal dialogs.
  - **`theme.py`**: Precision dark/light QSS stylesheet engine supporting Cyberpunk Dark, Clean Light, and Solarized Dark themes.
  - **`signals.py`**: Centralized `QtSignalBus` maintaining thread-safe Qt signals for all worker-to-UI communication.

---

### The `main/core/` Foundation Modules

- **`main/core/constants.py`**: Immutable constants, regex patterns (`ANSI_CSI_RE`, `_ESPTOOL_*_RE`), default baud rates, C++ standard headers, and telemetry helpers.
- **`main/core/theme.py`**: `Theme` class holding color tokens, typography parameters, and visual styles.
- **`main/core/config.py`**: Manages `src/gui_config.json`, single-instance mutex locks (`_claim_gui_instance`), occupied COM port tracking, and recent projects.
- **`main/core/file_utils.py`**: Low-level Windows file operations (`attrib +h`, `ensure_file_writable`, `robust_rmtree`), UNC share detection (`is_unc_or_network_path`), and AI review history backups (`AIEditBackupStore`).
- **`main/core/toolchain.py`**: Toolchain discovery, PlatformIO core junctions (`C:\.platformio-mcu-gui`), and CPU worker allocations.
- **`main/core/board_catalog.py`**: Dynamic board catalog parser (merging PlatformIO and Arduino index boards), USB VID/PID mapping table, and unit-aware `_parse_byte_size` for modern `esptool`.
- **`main/core/board_compat.py`**: Heuristic board compatibility analyzer and pinout GPIO conflict checker.

---

### The `src/modules/` System Services & Runtime Guards

- **`src/modules/private_python_guard.py`**: Strict private Python runtime enforcer. Halts execution if triggered by system/desktop Python and seamlessly re-launches under `src/_python/python.exe`.
- **`src/modules/crash_detector.py`**: Session sentinel, unhandled exception recorder, and startup crash recovery marker engine.
- **`src/modules/bootstrap.py`**: Windows runtime bootstrapper with upscale HTML/Edge WebView2 window that self-heals Python, installs dependencies, downloads pre-built PlatformIO core, and launches the GUI.
- **`src/modules/launcher.py`**: Entry point launcher configuring Windows `AppUserModelID` for taskbar grouping and single-instance mutex handling.
- **`src/modules/dedicated_AI.py`**: OpenCode AI assistant process controller (HTTP server + WebSocket + pywinpty).
- **`src/modules/project_terminal.py`**: Standalone project terminal server & PTY backend for integrated multi-terminal sessions.
- **`src/modules/win_subprocess_hide.py`**: Enforces `CREATE_NO_WINDOW` on all background subprocesses.
- **`src/modules/arduino_lib_req.py`**: Resolves required C++ headers and auto-downloads missing Arduino libraries.
- **`src/modules/detector.py`**: USB serial port auto-detection and board identification helper.
- **`src/modules/downloader.py`**: Resumable multi-threaded downloader with SHA-256 validation and Google Drive virus-scan bypass.
- **`src/modules/reset_editor.py`**: Editor state reset utility.
- **`src/modules/setup_ide_paths.py`**: Re-navigates `compile_commands.json` paths when sketches move across directories.

---

### Offline Monaco Editor & Web Assets

- **`src/editor/index.html`**: Host page for Monaco Editor with QWebChannel bridge polyfill, tab bar drag-and-drop, and CSS glowing animations.
- **`src/editor/bundle.js`**: Self-contained Monaco Editor offline JS bundle (no CDN dependency).
- **`src/editor/qwebchannel.js`**: Transport layer providing bidirectional Qt-JS object synchronization.
- **`src/editor/editor.worker.js` & `*.ttf`**: Web workers for syntax validation and bundled offline Montserrat/icon fonts.

---

### Caches, Installers & Reset Templates

- **`installers/`**: Bundled offline installers and drivers (tracked via Git LFS):
  - `CP210x/` — Silicon Labs USB-to-UART drivers.
  - `CH34x/` — WCH CH340 / CH341 USB serial drivers.
  - `arduino-cli.msi` — Arduino CLI installer.
  - `MicrosoftEdgeWebview2Setup.exe` — WebView2 installer.
  - `msys2-*.exe` — MSYS2 build tools.
- **`soft_reset/`**: Pre-configured minimal PlatformIO workspaces (`soft_reset_project/` for ESP32/non-AVR and `soft_reset_project_uno/` for Arduino AVR).
- **`.mcu_flasher_build_cache/`**: Generated build artifacts and isolated per-board workspaces (gitignored, hidden via Windows `attrib +h`).
- **`logs/`**: Runtime crash and diagnostic logs.

---

## ⚙️ Configuration

Application settings are persisted in `src/gui_config.json`:

```json
{
  "theme": "default",
  "baud_rate": 115200,
  "serial_port": "",
  "board": "esp32:esp32:esp32",
  "programmer": "esptool",
  "shared": {
    "editor_mode": "monaco",
    "cpu_multithreading": "HIGH",
    "graphics_acceleration": "ON",
    "reset_on_baud_change": false,
    "autosave_enabled": true,
    "autosave_delay": 2000,
    "auto_clear_serial_on_upload": true,
    "hide_build_console_warnings": false
  }
}
```

### Key Configuration Options:
- **`theme`**: Active UI color theme (`"default"` for Dark Cyberpunk, `"light"` for Clean Light, `"solarized_dark"` for Solarized Dark).
- **`cpu_multithreading`**: Compiler worker budgeting mode (`"LOW"`, `"MEDIUM"`, `"HIGH"`).
- **`reset_on_baud_change`**: Whether changing the serial monitor baud rate triggers a silent DTR/RTS hardware reset pulse (`true`/`false`).
- **`autosave_enabled` & `autosave_delay`**: Debounced automatic file saving state and delay in milliseconds.
- **`hide_build_console_warnings`**: Filter out non-fatal compiler warning lines from the build console.

---

## 🛠️ Development & Contributing

### System Requirements
- **Operating System**: Windows 10 or Windows 11
- **Hardware**: Minimum **4 logical CPU cores/threads** (enforced at startup)
- **Storage**: **6GB+** free disk space for toolchains, platforms, and compilers
- **Python**: Python 3.10+ (managed via private runtime at `src/_python/`)
- **Version Control**: Git with Git LFS (`git lfs install`)

### Verification & Syntax Checking
```powershell
# Verify syntax compilation across the PySide6 package entry point:
python -m py_compile main/mcu_flash_gui.py

# Verify syntax compilation across the backend bridge:
python -m py_compile main/web_bridge.py

# Verify syntax across all main Qt panels:
python -m py_compile main/qt/main_window.py
python -m py_compile main/qt/toolbar.py
python -m py_compile main/qt/editor_panel.py
python -m py_compile main/qt/serial_panel.py
python -m py_compile main/qt/terminal_panel.py
python -m py_compile main/qt/settings_dialog.py

# Launch GUI in development mode:
python mcu_flash_gui.py
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

> Made with ❤️‍🔥 by **Naph** — Happy flashing! 🚀
