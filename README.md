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
  - [6. Dual Code Editor Modes (Monaco vs. Default)](#6-dual-code-editor-modes-monaco-vs-default)
  - [7. Integrated Project Terminal (PowerShell ↔ CMD)](#7-integrated-project-terminal-powershell--cmd)
  - [8. OpenCode AI Assistant & Diff Glow](#8-opencode-ai-assistant--diff-glow)
  - [9. Soft Reset & Hard Reset Recovery Flashing](#9-soft-reset--hard-reset-recovery-flashing)
  - [10. Remote Network Shares (UNC Paths)](#10-remote-network-shares-unc-paths)
- [🧭 Architectural Reference: What is What & Which is Which](#-architectural-reference-what-is-what--which-is-which)
  - [Root Entry Points & Launchers](#root-entry-points--launchers)
  - [The `main/` Modular GUI Package](#the-main-modular-gui-package)
  - [The `main/core/` Foundation Modules](#the-maincore-foundation-modules)
  - [The `main/mixins/` Domain Mixins (358 Methods)](#the-mainmixins-domain-mixins-358-methods)
  - [The `src/` System Modules & Offline Assets](#the-src-system-modules--offline-assets)
  - [Caches, Installers & Templates](#caches-installers--templates)
- [⚙️ Configuration](#️-configuration)
- [🛠️ Development & Contributing](#️-development--contributing)

---

## ✨ Features

| Feature | Description |
| --- | --- |
| **🔨 One-Click Build & Flash** | Compile and upload to ESP32 / Arduino microcontrollers via Arduino CLI or PlatformIO |
| **🔌 Disconnect-Safe Uploads** | Two-phase uploads tolerate unplugging: compilation always finishes on host CPU, and flashing is cleanly skipped (with recovery hints) if the MCU is missing |
| **📟 Advanced Serial Monitor** | Real-time terminal with ANSI color rendering, timestamps, baud rate control, and instant MCU reset |
| **🎨 Modern Multi-Theme UI** | Dark Cyberpunk, Light Mode, and Solarized styling with Montserrat typography and responsive layout |
| **✏️ Dual Code Editor** | Switchable between **Monaco Editor** (VS Code engine with C++ syntax highlighting, Go-To-Definition, hover cards) and lightweight **Tkinter Default** editor |
| **🤖 Dedicated AI Assistant** | Embedded OpenCode AI side panel with file-watcher, line-level diffing, and pulsating glowing diff highlights (green added, red removed) |
| **💻 Integrated Project Terminal** | Embedded terminal powered by pywinpty + xterm.js with live **PowerShell ↔ Command Prompt** shell switching |
| **🌐 Remote & UNC Share Support** | Direct compilation & flashing of projects on network shares (`\\server\share`) with automated drive mapping and local SSD build acceleration |
| **⚡ Dual Reset Modes** | Fast software reset (re-flashing lightweight reset sketch) and native hardware reset via esptool DTR/RTS pulsing or bootloader recovery images |
| **📦 Zero-Touch Bootstrapper** | Self-healing Python environment, pre-built PlatformIO core seeding (~1.7GB fast download), and offline CP210x driver installation |

---

## 🚀 Quick Start

```cmd
# Double-click the native launcher:
MCU_Flasher.exe

# Or launch via the elevated bootstrap script:
direct\runThisOnWindows.vbs
```

**First Run Pipeline:**
1. Verifies storage drive (SSD/HDD recommended for build speed).
2. Auto-heals private portable Python runtime at `src/_python/` if needed.
3. Configures isolated virtual environment (`env/`).
4. Seeds pre-built PlatformIO toolchain and Arduino CLI binaries.
5. Installs CP210x USB UART drivers silently.
6. Launches the main GUI seamlessly.

> [!NOTE]
> **Storage Requirement**: Initial installation requires approximately **5GB of starting storage** for core toolchains, compilers, and dependencies. Storage usage may increment as additional Arduino/PlatformIO libraries and board platforms are installed.

---

## 📁 Project Structure

Below is the complete, full architectural structure of the MCU Flasher ecosystem:

```
MCU Flasher by Naph/
├── MCU_Flasher.exe                   # Compiled native Windows launcher (from src/launcher.cs)
├── mcu_flash_gui.py                 # Root forwarder script delegating to main.mcu_flash_gui
├── README.md                         # Comprehensive documentation, user guide & architecture
│
├── main/                             # Modular GUI core architecture (32k+ LOC across 41 files)
│   ├── __init__.py                  # Public exports and package initializer
│   ├── mcu_flash_gui.py             # Primary application assembly (MCUUploadGUI) & main()
│   ├── dialogs.py                   # Modal dialogs (ProjectSelectorDialog, BoardSearchDialog)
│   ├── widgets.py                   # UI widgets (ToolTip, CircularLoadingOverlay, _ShellTerminalBuffer, DPI)
│   ├── editor_api.py                # Monaco JS ↔ Python bridge (EditorApi, MonacoAutosaveWorker, diffs)
│   │
│   ├── core/                        # Core foundations & system services
│   │   ├── __init__.py              # Core package re-exports
│   │   ├── constants.py             # Global constants, regexes, baud rates, headers & telemetry
│   │   ├── theme.py                 # Theme class, color tokens, dark/light styling engine
│   │   ├── config.py                # Config persistence (gui_config.json), PID multi-instance locks
│   │   ├── file_utils.py            # Windows attributes (attrib +h), UNC path detection, AI backup store
│   │   ├── toolchain.py             # PlatformIO & Arduino CLI discovery, junctions & CPU worker count
│   │   ├── board_catalog.py         # 420+ board definitions, dynamic catalog loader, USB VID/PID map
│   │   └── board_compat.py          # Board compatibility detection & GPIO pin conflict analyzer
│   │
│   └── mixins/                      # 27 domain mixins composing MCUUploadGUI (358 methods total)
│       ├── __init__.py              # Mixins package re-exports
│       ├── init_startup_mixin.py    # App init, startup overlay splash & deferred background init
│       ├── ui_layout_mixin.py       # Main UI builder, toolbar, responsive layout & button styling
│       ├── console_serial_mixin.py  # Console output formatting, progress bars, serial pump
│       ├── layout_panes_mixin.py    # Pane collapsing/expanding, editor detachment & floating window
│       ├── async_tasks_mixin.py     # Thread-safe UI dispatch queue (_post_ui, _run_bg_task)
│       ├── compat_devices_mixin.py  # Compatible devices tab, scanning, filtering & rendering
│       ├── project_terminal_mixin.py# Embedded PowerShell/CMD terminal with ConPTY & xterm.js
│       ├── hardware_port_mixin.py   # USB COM port polling, auto-board matching, baud rate controls
│       ├── clean_build_mixin.py     # Allowlist-based build cache cleanup & stale path cleaner
│       ├── build_actions_mixin.py   # Compile, Upload, Stop action handlers & monitor scheduling
│       ├── project_actions_mixin.py # Project selection, sketch folder changer, file modifier dialog
│       ├── compile_cache_mixin.py   # Source hashing, build metadata caching, skip-compile validation
│       ├── library_headers_mixin.py # Header include scanning, Arduino library dependencies resolver
│       ├── build_workspace_mixin.py # PlatformIO workspaces & remote UNC network drive mapping
│       ├── soft_reset_template_mixin.py # Soft-reset templates, digest manifests & env mapping
│       ├── platformio_ini_mixin.py  # Dynamic platformio.ini generation & source synchronization
│       ├── compiler_pipeline_mixin.py # Full compilation workflow (_run_compile), source freezing
│       ├── upload_pipeline_mixin.py # Flashing pipeline (_run_upload), chip feature probing
│       ├── monitor_pipeline_mixin.py# Serial monitor reading loop (_run_monitor) & DTR/RTS reset
│       ├── editor_modes_mixin.py    # Monaco & Default editor builders, WebView2 embedding
│       ├── boards_catalog_mixin.py  # Board download manager, dynamic board reload & internet check
│       ├── ai_assistant_mixin.py    # OpenCode AI side panel lifecycle, Win32 reparenting & diff glow
│       ├── settings_dialog_mixin.py # Settings modal dialog & theme/editor mode switching
│       ├── hard_reset_mixin.py      # Recovery image generation & esptool direct hard reset
│       ├── soft_reset_mixin.py      # Fast soft reset flashing & COM port reconnect watcher
│       ├── window_lifecycle_mixin.py# Window closure cleanup (_on_close) & mutex releasing
│       └── syntax_checker_mixin.py  # Background C++ syntax checker thread & diagnostic tree updates
│
├── direct/
│   └── runThisOnWindows.vbs         # Silent UAC-elevated launcher for Windows
│
├── src/                             # Core system modules, offline editor assets & storage
│   ├── _python/                     # Private portable Python 3 runtime (hidden via attrib +h)
│   ├── .platformio-mcu-gui/         # PlatformIO core store (junctioned to avoid MAX_PATH)
│   ├── gui_config.json              # Persisted user settings (editor mode, themes, baud rates)
│   ├── syntax_checker.py            # Realtime C++ syntax linter & AST regex analyzer
│   ├── qscintilla_editor.py         # Optional QScintilla code editor component (PyQt5)
│   ├── qscintilla_viewer.py         # Optional QScintilla read-only viewer component (PyQt5)
│   ├── launcher.cpp                 # Native Windows executable wrapper source (C++)
│   ├── launcher.cs                  # Native Windows executable wrapper source (C#)
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
│   │   └── *.ttf                    # Offline editor icon and UI font assets
│   │
│   ├── assets/                      # Shared graphical assets
│   │   ├── mcu_icon.ico             # Application icon
│   │   └── xterm/                   # xterm.js assets for terminal and AI panels
│   │
│   ├── dbs/                         # Persistent JSON databases and CRUD managers
│   │   ├── bootstrap_config.json    # Bootstrap update and skip configuration
│   │   ├── dbs_create.py            # Notification DB CRUD: create
│   │   ├── dbs_read.py              # Notification DB CRUD: read
│   │   ├── dbs_update.py            # Notification DB CRUD: update
│   │   └── dbs_delete.py            # Notification DB CRUD: delete
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
├── soft_reset_project/              # PlatformIO soft-reset project template (ESP32)
├── soft_reset_project_uno/          # PlatformIO soft-reset template for Arduino UNO / AVR
├── index_json/                      # Arduino board and library index caches
├── .mcu_flasher_build_cache/        # Isolated per-board build cache & workspaces (gitignored, hidden)
└── logs/                            # Runtime diagnostic logs & lock files
```

---

## 📘 User Guide & How to Use

### 1. Launching & First-Run Auto-Bootstrap
- Launch via **`MCU_Flasher.exe`** (or **`direct\runThisOnWindows.vbs`**).
- The bootstrapper handles all missing dependencies, Python packages (`pyserial`, `pywebview`, `pywinpty`), and toolchains unattended.
- If launched from source in a developer terminal:
  ```powershell
  python mcu_flash_gui.py
  # or
  python main/mcu_flash_gui.py
  ```

### 2. Opening, Selecting & Scaffolding Projects
- Click **`📂 Select Project`** on the toolbar or choose from recent projects.
- **New Project Scaffolding**: Click **`✨ New Project`**, enter a project name, and MCU Flasher creates a structured project directory with boilerplate `.ino`, header inclusions, and ready-to-build configuration.
- **Modify Project Files**: Click **`📝 Modify Files`** to add, rename, or delete sketch files (`.ino`, `.cpp`, `.h`).

### 3. Selecting Boards & COM Ports
- **COM Port Selection**: The top-right dropdown shows detected USB serial ports. Plug in your microcontroller, and MCU Flasher automatically selects the new port and identifies the connected chip (ESP32, ESP32-S3, ESP32-C3, CH340, CP210x).
- **Board Catalog Search**: Click **`🔍 Search Boards`** to search through 420+ supported microcontrollers by keyword, architecture, or manufacturer.
- **Baud Rate & Upload Speed**: Choose your desired Serial Monitor baud rate (e.g. `115200`) and Flashing speed (up to `921600` baud for ultra-fast uploads).

### 4. Compiling & Flashing Code
- **Compile Only (`🔨 Compile`)**:
  - Compiles your project using the selected toolchain (PlatformIO or Arduino CLI).
  - *Non-blocking*: Serial Monitor remains active and streaming while compiling!
  - Caches build artifacts in `.mcu_flasher_build_cache/` for near-instant incremental builds.
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

### 6. Dual Code Editor Modes (Monaco vs. Default)
- **Monaco Mode (VS Code Engine)**:
  - Rich editor embedded via pywebview (WebView2).
  - Features: Multi-tab editing, C++ autocomplete, F12 / Ctrl+Click Go-To-Definition, Ctrl+Hover documentation cards, and debounced auto-saving.
- **Default Mode (Pure Tkinter)**:
  - Ultra-lightweight native editor with syntax coloring, auto-indent, and bracket matching.
- **Switching**: Open **`⚙️ Settings`** → **Editor Mode** → Select Monaco or Default.

### 7. Integrated Project Terminal (PowerShell ↔ CMD)
- Click the **`💻 Project Terminal`** tab in the bottom notebook.
- Live, embedded ConPTY xterm.js terminal initialized directly in your sketch directory.
- Switch seamlessly between **PowerShell** and **Command Prompt** with dedicated session scrollback preservation.

### 8. OpenCode AI Assistant & Diff Glow
- Click the **`🤖 AI Assistant`** button on the toolbar to open the embedded AI side panel.
- Ask questions, generate Arduino code, or request refactorings.
- **Live AI Diff & Glow**: When the AI modifies files in your sketch, Monaco Editor automatically detects the change, reloads the file, and highlights modifications with pulsating glowing animations:
  - 🟢 **Green Glow** on added / edited lines.
  - 🔴 **Red Glow** on removed lines.
  - Floating banner with quick **"Dismiss Glow ✖"** button.

### 9. Soft Reset & Hard Reset Recovery Flashing
- **Soft Reset**: Flashes a minimal lightweight reset routine to clear locked flash or boot loops without wiping entire partition tables.
- **Hard Reset**: Executes full recovery flashing using esptool bootloader images or AVR bootloader sequences.

### 10. Remote Network Shares (UNC Paths)
- Open projects directly from network storage (e.g. `\\nas\projects\iot_sensor`).
- MCU Flasher mounts a temporary drive letter dynamically during compilation and routes intermediate `.o` object files to your local SSD (`remote_workspaces/`), avoiding Samba locking errors.

---

## 🧭 Architectural Reference: What is What & Which is Which

### Root Entry Points & Launchers

- **`MCU_Flasher.exe`**: Native C# wrapper compiled from `src/launcher.cs`. Elevates if needed and executes `direct\runThisOnWindows.vbs` silently.
- **`direct\runThisOnWindows.vbs`**: Windows VBScript bootstrapper that checks elevation, verifies drive storage type, and calls `src/modules/launcher.py`.
- **`mcu_flash_gui.py`**: Root forwarder script that delegates directly to `main.mcu_flash_gui` for backward compatibility.

---

### The `main/` Modular GUI Package

- **`main/mcu_flash_gui.py`**: The primary assembly module. Defines `MCUUploadGUI` by inheriting from all 27 mixins and provides the `main()` entrypoint function.
- **`main/dialogs.py`**: Contains modal dialog classes:
  - `ProjectSelectorDialog`: Interactive project picker with recent project cards, folder browsing, and scaffolding.
  - `BoardSearchDialog`: Fast live-search dialog filtering across all 420+ board definitions.
- **`main/widgets.py`**: Standalone UI components:
  - `ToolTip`: Hover tooltip bubble with dark-mode styling.
  - `CircularLoadingOverlay`: Semi-transparent animated spinner for long operations.
  - `_ShellTerminalBuffer`: Lightweight ANSI/VT terminal screen model for Windows PTY rendering.
  - `center_toplevel`, `safe_reclaim_os_focus`, and DPI scaling helpers.
- **`main/editor_api.py`**:
  - `EditorApi`: Exposes Python methods to JavaScript via `window.pywebview.api` (file read/save, tab switching, syntax linting).
  - `MonacoAutosaveWorker`: Background thread for debounced disk saving.
  - `build_ai_line_diff`: Line-level LCS diff generator for AI code highlights.

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

### The `main/mixins/` Domain Mixins (358 Methods)

| Mixin File | Mixin Class | Responsibilities & Methods |
| --- | --- | --- |
| `init_startup_mixin.py` | `InitStartupMixin` | `__init__`, startup splash overlay, deferred background subsystem initialization, sketch title marquee. |
| `ui_layout_mixin.py` | `UILayoutMixin` | `_build_ui`, toolbar creation, paned window layout, theme restyling, responsive width calculations, button states. |
| `console_serial_mixin.py` | `ConsoleSerialMixin` | Console output appending, progress bar formatting, persistent notification drawer, serial monitor display pump. |
| `layout_panes_mixin.py` | `LayoutPanesMixin` | Collapsing/expanding editor and monitor panes, detached editor window lifecycle, placeholder views. |
| `async_tasks_mixin.py` | `AsyncTasksMixin` | Thread-safe UI dispatch queue (`_post_ui`, `_run_bg_task`) routing background thread events to the Tkinter loop. |
| `compat_devices_mixin.py` | `CompatDevicesMixin` | Compatible Devices tab, hardware compatibility scanning, background caching, and filter rendering. |
| `project_terminal_mixin.py` | `ProjectTerminalMixin` | ConPTY terminal session management, PowerShell ↔ CMD live switcher, xterm.js embedding, keyboard shortcuts. |
| `hardware_port_mixin.py` | `HardwarePortMixin` | USB COM port polling, auto-detecting boards by descriptor/VID:PID, baud rate and flashing speed controls. |
| `clean_build_mixin.py` | `CleanBuildMixin` | Allowlist-based build cache cleanup, stale workspace deletion, clean & compile workflow. |
| `build_actions_mixin.py` | `BuildActionsMixin` | Compile/Upload/Stop action handlers, serial monitor auto-resume scheduling. |
| `project_actions_mixin.py` | `ProjectActionsMixin` | Folder change events, opening sketch in Windows Explorer, project selector integration, file modification dialog. |
| `compile_cache_mixin.py` | `CompileCacheMixin` | Source file hashing, build metadata caching, skip-compile fingerprint validation. |
| `library_headers_mixin.py` | `LibraryHeadersMixin` | C++ `#include` scanning, automatic Arduino library resolution, board variant verification. |
| `build_workspace_mixin.py` | `BuildWorkspaceMixin` | Isolated per-board build folders (`.mcu_flasher_build_cache/boards/<key>/`), dynamic UNC drive mapping/unmapping. |
| `soft_reset_template_mixin.py` | `SoftResetTemplateMixin` | Template digest verification, manifest writing, and reset environment configuration. |
| `platformio_ini_mixin.py` | `PlatformioIniMixin` | Dynamic generation of `platformio.ini`, sketch entry point validation, and source synchronization. |
| `compiler_pipeline_mixin.py` | `CompilerPipelineMixin` | Full compilation pipeline (`_run_compile`), source freezing, compiler output parsing, error classification. |
| `upload_pipeline_mixin.py` | `UploadPipelineMixin` | Flashing pipeline (`_run_upload`), chip feature probing, disconnect-safe flashing guards. |
| `monitor_pipeline_mixin.py` | `MonitorPipelineMixin` | Serial monitor reading loop (`_run_monitor`), DTR/RTS hardware reset pulse. |
| `editor_modes_mixin.py` | `EditorModesMixin` | Monaco Editor pywebview embedding, Tkinter Default editor builder, hang watchdog, fallback handling. |
| `boards_catalog_mixin.py` | `BoardsCatalogMixin` | Board download manager, dynamic board index reloading, internet availability checks. |
| `ai_assistant_mixin.py` | `AIAssistantMixin` | OpenCode AI side panel lifecycle, Win32 reparenting, file watcher, and glowing diff highlight dispatch. |
| `settings_dialog_mixin.py` | `SettingsDialogMixin` | Preferences modal (`_open_settings`), theme switcher, editor mode switching, process restart coordination. |
| `hard_reset_mixin.py` | `HardResetMixin` | Hard reset binary generation, esptool direct flashing pipeline. |
| `soft_reset_mixin.py` | `SoftResetMixin` | Soft reset flashing pipeline, ELF usage extraction, COM port reconnect watcher. |
| `window_lifecycle_mixin.py` | `WindowLifecycleMixin` | Application shutdown cleanup (`_on_close`), mutex releasing, subprocess termination. |
| `syntax_checker_mixin.py` | `SyntaxCheckerMixin` | Background C++ syntax checker thread, realtime diagnostics tree updates. |

---

### The `src/` System Modules & Offline Assets

- **`src/modules/bootstrap.py`**: Windows runtime bootstrapper that self-heals Python, installs dependencies, downloads prebuilt PlatformIO core, and launches the GUI.
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
- **`soft_reset_project/`**: Pre-configured minimal PlatformIO workspace used for rapid software resets of ESP32 boards.
- **`soft_reset_project_uno/`**: Pre-configured minimal workspace for software resets of Arduino AVR (Uno/Nano/Mega) boards.
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
- Python 3.10+
- **5GB+** starting storage for toolchains and platform packages
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

> Made with ❤️ by **Naph** — Happy flashing! 🚀
