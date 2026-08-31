---
name: mcu-flash-gui-dev
description: "Developer and troubleshooting guide for the Windows MCU Flash GUI (mcu_flash_gui.py, launcher.py, dedicated_AI.py). Use when modifying, debugging, or enhancing the Tkinter flasher interface, serial monitor, Arduino CLI or PlatformIO pipelines, board caches, or Windows launchers."
---

# MCU Flash GUI Development & Maintenance Skill

Use this skill when developing, debugging, or extending the **MCU Flash GUI** desktop application (`mcu_flash_gui.py`) and its supporting execution ecosystem on Windows 10/11.

## Platform Scope

- Focus on the Windows main app, `mcu_flash_gui.py`, Windows launchers (`launcher.py`, `runThisOnWindows.vbs`, `src/launcher.cpp`, `src/launcher.cs`), and supporting modules.
- Treat real sketch files and unknown user content as visible, user-owned data.
  Hide only known app-generated project metadata, and keep it writable.

---

## Core Architecture Overview

The project provides a modern Windows desktop interface for compiling, flashing, and monitoring ESP32 / Arduino microcontrollers. The GUI architecture is modularized into `main/` (32k+ lines total across 27 domain mixins, core utilities, dialogs, widgets, and Monaco bridge), with `mcu_flash_gui.py` acting as both a root forwarder and the assembled main package (`main/mcu_flash_gui.py`).

### Key Components

1. **Main GUI Application (`main/mcu_flash_gui.py` + `main/mixins/`)**
   - Tkinter-based dark-themed GUI (`MCU Flasher by Naph`), main class: `MCUUploadGUI`.
   - Composed of 27 modular domain mixins in `main/mixins/` covering compilation, flashing, serial monitor, terminal, AI side panel, settings, and hardware management.
   - **Dual editor modes** switchable at runtime via Settings dialog:
     - `"default"` — Pure Tkinter `tk.Text` tabbed editor with custom syntax highlighting, line numbers, auto-indent, and bracket matching (built inside `EditorModesMixin._build_editor_default()`).
     - `"monaco"` — Monaco Editor (VS Code engine) embedded in a pywebview WebView2 window that is Win32-reparented into the Tkinter frame (built inside `EditorModesMixin._build_editor_monaco()`).
   - Editor mode is persisted in `src/gui_config.json` under `shared.editor_mode`.
   - Serial port auto-detection, baud rate selection, and live serial monitor.
   - Dual toolchain support: **Arduino CLI** and **PlatformIO**.
   - Supporting modules: `main/editor_api.py` (pywebview JS↔Python bridge), `main/core/theme.py` (color constants), `main/dialogs.py` (`ProjectSelectorDialog`, `BoardSearchDialog`), `main/widgets.py` (`ToolTip`, `CircularLoadingOverlay`, `_ShellTerminalBuffer`).

2. **Monaco Editor Frontend (`src/editor/index.html` + `bundle.js`)**
   - Self-contained HTML page loaded by pywebview, backed by a local offline Monaco `bundle.js` (no CDN).
   - Exposes global JS functions called from Python via `editor_window.evaluate_js()`:
     - `loadProject()`, `saveAllFiles()`, `saveActiveFile()`, `reloadActiveFile()`.
   - `EditorApi` (Python side) is exposed as `window.pywebview.api` (JS side), providing:
     - `get_project_files()`, `read_file(path)`, `save_file(path, content)`, `mark_modified(path, bool)`, `set_active_file(path)`, `realtime_check_syntax(path, content)`, `run_action(action)`, `save_tab_order(paths)`, `on_editor_content_change()`.
   - **Tab bar** with drag-and-drop reordering, dirty-dot indicators, and persistent tab order inside `.mcu_flasher_build_cache/.mcu_flash_tab_order.json`.
   - **Ctrl+Click / F12 Go-To-Definition** with cross-file symbol resolution (`findProjectSymbol()`, `parseSymbolDetails()`), including built-in Arduino function registry (`BUILTIN_ARDUINO`).
   - **Ctrl+Hover** floating definition card with function signature, parameters, and return type.
   - **Realtime syntax checking** via `realtime_check_syntax()` → `src/syntax_checker.py` (C++ linting).
   - **Detached action bar** for when the editor is in a separate window (compile, upload, save, reload, modify buttons).
   - **AI Edit Diff & Glowing Highlights** — When AI edits a file and the editor reloads, the system computes a line-level LCS diff between old and new content and applies Monaco decorations:
     - **Green glow** (pulsing CSS animation `greenGlowPulse`) + `+` gutter icon on added/changed lines.
     - **Red glow** (pulsing CSS animation `redGlowPulse`) + `-` gutter icon on removed lines.
     - A floating **"🤖 AI Edits"** banner shows counts and a "Dismiss Glow ✖" button.
     - Implemented by wrapping `window.reloadActiveFile` to capture before/after content and calling `window.applyAiDiffHighlights(oldContent, newContent)`.
     - `window.clearAiDiffHighlights()` removes all decorations and hides the banner.

3. **Dedicated AI Assistant Controller (`src/modules/dedicated_AI.py` & Right-Side Panel)**
   - Manages an **OpenCode AI** terminal session rendered directly inside a right-side embedded panel within the main window (`self.h_split_pane` → `self.ai_side_container`).
   - On Windows, the pywebview OS window is reparented into `self.ai_embed_frame` via Win32 `SetParent`, sitting permanently attached right beside the Editor and Monitor panes.
   - Can be toggled visible/hidden dynamically via the `🤖 AI Assistant` toolbar button or `✖ Hide` header button, expanding the Editor & Monitors to full width when hidden.
   - Key exports imported by `mcu_flash_gui.py`: `AIController`, `is_opencode_installed`.
   - `AIController` class: manages launch/close lifecycle, toolbar button state animation, file-watcher for AI-applied edits.
   - When AI edits a file, `AIController` calls `on_ai_edit_func` (wired to `MCUUploadGUI._on_ai_applied_edit()`, line ~26150), which reloads the editor, shows green/red diff glows, and logs a notification in the Notifications tab.

4. **Project Terminal (`src/modules/project_terminal.py`)**
   - Full-featured integrated terminal using pywebview + xterm.js + pywinpty (ConPTY backend).
   - Supports **PowerShell** and **Command Prompt** sessions with live shell switching (`_shell_refresh_switcher()`, `_shell_switch_button_click()` at line ~12575).
   - Switching between shells preserves scrollback and prompt state (each shell gets its own xterm instance).
   - Architecture: HTTP server serves the xterm.js page, WebSocket handles PTY I/O on a free port pair.
   - Launched as a subprocess to avoid Tk/WebView2 event loop conflicts.

5. **QScintilla External Viewer (`src/qscintilla_editor.py`, `src/qscintilla_viewer.py`)**
   - Optional rich code viewer using QScintilla (PyQt5). Launched as a subprocess via `sys.argv[1]`.
   - Shares syntax error data with the main GUI through `.mcu_flasher_build_cache/.mcu_flash_syntax_errors.json`.

6. **Launchers & Native Wrappers**
   - `src/modules/launcher.py`: Python entry point for initializing configuration and launching the main GUI.
   - `src/launcher.cpp`: Native C++ executable wrapper that initializes environment variables and launches `launcher.py` / `mcu_flash_gui.py` silently on Windows without popping a console window.
   - `src/launcher.cs`: C# launcher source (compiles to `MCU_Flasher.exe`).
   - `direct/runThisOnWindows.vbs`: Windows bootstrap launcher (elevates when needed, hides console).

7. **Realtime C++ Syntax Linter (`src/syntax_checker.py`)**
   - Lightweight C++ AST & regex engine for validating `.ino`, `.cpp`, and `.h` files without invoking full compiler runs.
   - Parses missing semicolons, unmatched brackets/quotes, undeclared variables/functions, and syntax errors, populating line-numbered diagnostics into the UI tree and Monaco markers.

8. **Database & Notification Store (`src/dbs/` & `.mcu_flasher_build_cache/`)**
   - **Per-Sketch Notification Store**: Notifications are persisted per-project inside `<sketch_dir>/.mcu_flasher_build_cache/dbs_notif.json`, ensuring build logs, compile errors, library installations, and USB device events stay scoped to each sketch.
   - Fallback global store: `src/dbs/dbs_notif.json` (used when no project folder is active).
   - Managed via modular CRUD operations: `dbs_create.py`, `dbs_read.py`, `dbs_update.py`, and `dbs_delete.py` (supports explicit or dynamic `db_path`).
   - Also stores: `bootstrap_config.json`, `arduino_browser_settings.json`, `arduino_cli_path.txt`.

9. **Per-Project Hardware State & AI Context Discovery (`project_state.json`)**
   - Synchronized automatically into `<sketch_dir>/.mcu_flasher_build_cache/project_state.json` on board, port, baud rate, and setting changes.
   - Contains: active `board_name`, `platform` (e.g. `espressif32`, `atmelavr`), `fqbn`, `build_mcu`, `port`, `baud_rate`, `upload_speed`, `editor_mode`, and settings.
   - Allows AI assistants (Antigravity & OpenCode) to read active MCU target architecture, pinouts, and COM port directly without manual user prompting.

10. **Toolchain, Bootstrap & Utilities (`src/modules/`)**
    - `bootstrap.py`: Manages private Python environment setup/auto-heal, PlatformIO virtual environments (`penv`), Arduino CLI binaries, Node.js LTS, and OpenCode AI Assistant. Junctions PlatformIO core to avoid MAX_PATH (>260 char) issues.
    - `_heal_private_python_runtime()` (line ~7578): Self-repair mechanism that detects missing or damaged portable Python at `src/_python/`, reinstalling it silently from `installers/.handsoff/python-*-amd64.exe` with `attrib +h` project isolation.
    - `_ensure_platformio_core_prebuilt()` (line ~882): Downloads a pre-built PlatformIO toolchain zip from GitHub release, with Google Drive fallback, resume support, SHA256 verification, and progress bar. Seeds the core store before the slower `ensure_platformio()` path.
    - `downloader.py`: Custom download utility with progress bar, resume support, and Google Drive virus-scan confirmation page handling (urllib-based, no `requests` dependency).
    - `arduino_lib_req.py`: Resolves required C++ headers and auto-downloads missing Arduino libraries.
    - `detector.py`: Windows USB serial port auto-detection and board identification.
    - `win_subprocess_hide.py`: Suppresses console window popups when spawning background subprocesses on Windows (`CREATE_NO_WINDOW`).
    - `reset_editor.py`: Utility to reset cached editor configuration and state.
    - `setup_ide_paths.py`: Dynamically re-navigates `compile_commands.json` paths when projects are moved across machines or user accounts.
    - `project_terminal.py`: Integrated project terminal (see component #4 above).
    - `get-platformio.py`: PlatformIO official installer script (bundled).

11. **Remote UNC & Network Share Pipeline (`mcu_flash_gui.py`)**
    - `is_unc_or_network_path()`, `_unc_share_root()`: Dynamically detects Windows UNC paths (`\\server\share`) and remote drive types.
    - `_map_unc_for_build()` (line ~17918) / `_unmap_unc_after_build()` (line ~18044): Dynamically maps the remote share to a free drive letter (`Z:` to `A:`) for subprocess CWD compatibility and cleanly removes it in `finally:` blocks.
    - `_remote_workspace_root()` (line ~17710): Automatically routes PlatformIO build workspaces (`PLATFORMIO_WORKSPACE_DIR`, `PLATFORMIO_BUILD_DIR`, `PLATFORMIO_LIBDEPS_DIR`) to local fast storage (`remote_workspaces/<project>_<hash>/`), avoiding Samba/SMB file-locking errors on `.sconsign*.dblite` while reading source files directly from the network.

---

## File Map

```
MCU Flasher by Naph/
├── mcu_flash_gui.py           # Backward-compatible main entry point (invokes main/)
├── main/                      # Modular GUI core architecture (32k+ LOC)
│   ├── __init__.py            # Package exports
│   ├── mcu_flash_gui.py       # Assembled MCUUploadGUI class & main()
│   ├── dialogs.py             # Modal dialogs (ProjectSelectorDialog, BoardSearchDialog)
│   ├── widgets.py             # Custom UI widgets (ToolTip, CircularLoadingOverlay, _ShellTerminalBuffer)
│   ├── editor_api.py          # JS ↔ Python bridge for Monaco Editor & autosave
│   ├── core/                  # Core engine foundations
│   │   ├── constants.py       # Global constants, regexes, baud rates & telemetry
│   │   ├── theme.py           # Theme class, color tokens & styling engine
│   │   ├── config.py          # Settings persistence, multi-instance PID locks
│   │   ├── file_utils.py      # File attributes (attrib +h), UNC paths, AI backup store
│   │   ├── toolchain.py       # PlatformIO & Arduino CLI discovery, junctions
│   │   ├── board_catalog.py   # 420+ board definitions, catalog cache, USB IDs
│   │   └── board_compat.py    # Board compatibility detection & GPIO analyzer
│   └── mixins/                # 27 domain mixins composing MCUUploadGUI
├── MCU_Flasher.exe            # Compiled native launcher (from launcher.cs)
├── README.md                  # Project documentation & user guide
├── direct/
│   └── runThisOnWindows.vbs   # Windows VBS launcher
├── installers/                # Bundled silent installers & drivers (Git LFS)
│   ├── .handsoff/             # Offline Python runtime installers (python-*-amd64.exe)
│   ├── CP210x/                # CP210x USB-to-UART drivers
│   ├── arduino-cli.msi        # Bundled Arduino CLI installer
│   ├── MicrosoftEdgeWebview2Setup.exe # Bundled WebView2 installer
│   └── msys2-*.exe            # Bundled MSYS2 build tools
├── soft_reset_project/        # PlatformIO soft-reset project templates
├── soft_reset_project_uno/    # PlatformIO soft-reset template for Arduino UNO
├── index_json/                # Arduino board/library index caches
├── src/                       # Application core modules and assets
│   ├── _python/               # Private portable Python 3 runtime (hidden attribute)
│   ├── .platformio-mcu-gui/   # PlatformIO core store (junctioned to avoid MAX_PATH)
│   ├── gui_config.json        # Persisted GUI settings (editor_mode, autosave, baud, themes)
│   ├── syntax_checker.py      # Realtime C++ syntax linter & AST analyzer
│   ├── qscintilla_editor.py   # QScintilla code editor component (PyQt5)
│   ├── qscintilla_viewer.py   # QScintilla read-only viewer component (PyQt5)
│   ├── launcher.cpp           # Native Windows executable wrapper (suppresses console)
│   ├── launcher.cs            # C# native launcher source
│   ├── resources.rc           # Windows resource definition (icon embed)
│   ├── editor/                # Monaco Editor offline Web UI
│   │   ├── index.html         # Monaco Editor HTML (tabs, toolbar, diff glows, hover, go-to-def)
│   │   ├── bundle.js          # Monaco Editor offline JS bundle (no CDN dependency)
│   │   └── *.ttf              # Offline icon and font assets
│   ├── assets/                # Shared UI assets
│   │   ├── mcu_icon.ico       # Application icon
│   │   └── xterm/             # xterm.js assets for terminal/AI panels
│   ├── modules/               # Core bootstrap, AI, terminal, and utility modules
│   │   ├── bootstrap.py       # Windows dependency & runtime bootstrapper (auto-heal, penv)
│   │   ├── launcher.py        # Python entry point / bootstrapper
│   │   ├── dedicated_AI.py    # OpenCode AI controller (pywebview + xterm.js + pywinpty)
│   │   ├── project_terminal.py# Integrated project terminal (pwsh/cmd, xterm.js)
│   │   ├── downloader.py      # Custom download utility (resume, progress, GDrive support)
│   │   ├── arduino_lib_req.py # Arduino library dependency resolver & downloader
│   │   ├── detector.py        # USB serial port auto-detection & board probing
│   │   ├── win_subprocess_hide.py  # Windows subprocess console suppression
│   │   ├── reset_editor.py    # Editor state reset utility
│   │   ├── setup_ide_paths.py # compile_commands.json path re-navigation
│   │   └── get-platformio.py  # PlatformIO official installer script
│   ├── dbs/                   # Persistent JSON stores
│   │   ├── bootstrap_config.json  # Bootstrap skip/update config
│   │   ├── dbs_create.py      # DB CRUD: create
│   │   ├── dbs_read.py        # DB CRUD: read
│   │   ├── dbs_update.py      # DB CRUD: update
│   │   └── dbs_delete.py      # DB CRUD: delete
│   └── fonts/                 # Bundled custom font assets (Montserrat, etc.)
└── .mcu_flasher_build_cache/  # Generated build cache (gitignored, hidden)
```

---

## Key Workflows & Guidelines

### 1. Modifying GUI Layout & Components (`mcu_flash_gui.py`)
- The main GUI class is `MCUUploadGUI` (line ~7436).
- Standard Tkinter / `ttk` widgets are used for controls. Colors come from the `Theme` class.
- Maintain dark-mode visual hierarchy and styling consistency.
- Avoid dynamic layout jitter: compute container bounds cleanly without arbitrary pixel hardcoding.
- Maintain non-blocking operations: compile, upload, and serial reading tasks MUST run on background threads using `threading.Thread`.
- **Thread safety**: All GUI updates from worker threads MUST go through `self.root.after(0, callback)`.
- **Detached windows**: Closing a detached editor/panel window via the native `[X]` button must NOT freeze the main app. Always use `protocol("WM_DELETE_WINDOW", handler)` and ensure the handler runs reattach/cleanup on the main thread via `root.after()`.

### 2. Modifying the Monaco Editor (`src/editor/index.html`)
- The HTML is a single self-contained page with `<style>` + inline `<script>` blocks after `bundle.js`.
- The JS is organized as multiple immediately-invoked function expressions (IIFEs) for encapsulation:
  - **Detached action bar** IIFE — toolbar button dispatch to `pywebview.api.run_action()`.
  - **Tab drag-and-drop** IIFE — tab reordering with `save_tab_order()`.
  - **Zoom** IIFE — Ctrl+Plus/Minus font scaling.
  - **Realtime syntax check** IIFE — `onDidChangeModelContent` → `realtime_check_syntax()`.
  - **AI Edit Diff & Glow** IIFE — LCS diff engine, `applyAiDiffHighlights()`, `clearAiDiffHighlights()`, `reloadActiveFile` hook.
  - **Go-To-Definition & Hover** IIFE — `findProjectSymbol()`, `parseSymbolDetails()`, Ctrl+Click, F12, `registerDefinitionProvider`, `registerHoverProvider`.
- `window.editorInstance` is the Monaco editor instance (set by `bundle.js`).
- `window.pywebview.api` is the Python `EditorApi` bridge.
- Monaco decorations (`deltaDecorations`) are used for diff highlights and Ctrl+hover underlines.
- When adding new JS functions callable from Python, expose them on `window` and call via `editor_window.evaluate_js("functionName()")` from `mcu_flash_gui.py`.
- **Known fix**: Monaco/pywebview on Windows requires WebView2 runtime. The app bundles `installers/MicrosoftEdgeWebview2Setup.exe` for offline WebView2 installation during bootstrap.

### 3. Modifying the AI Assistant (`src/modules/dedicated_AI.py`)
- `AIController` is instantiated in `MCUUploadGUI.__init__()` only if OpenCode CLI is installed.
- It receives a callback `on_ai_edit_func` wired to `MCUUploadGUI._on_ai_applied_edit()`.
- The AI terminal uses pywebview + xterm.js rendering with a pywinpty ConPTY backend.
- `TerminalServer` runs HTTP (page serve) + WebSocket (PTY I/O) on a free port pair.
- When adding features: keep the dedicated_AI module self-contained — it should only export `AIController`, `is_opencode_installed`, and optionally `DedicatedAIApp`.

### 4. Modifying the Project Terminal (`src/modules/project_terminal.py`)
- Uses the same architecture as the AI Assistant (pywebview + xterm.js + pywinpty).
- Supports PowerShell and Command Prompt sessions with live shell switching.
- Each shell type gets its own xterm instance to preserve scrollback.
- Shell switching between pwsh and cmd must handle environment variable inheritance correctly — failing to reinitialize the shell env on switch causes PATH and prompt corruption.
- The terminal runs as a subprocess to prevent Tk/WebView2 event loop conflicts.

### 5. Toolchain & Serial Monitor Execution Pipeline
- **Unified PlatformIO Toolchain Engine**:
  - All microcontroller architectures (**ESP32**, **ESP8266**, **Atmel AVR**, etc.) compile uniformly through the PlatformIO SCons build engine using parallel CPU workers (`-j <jobs>`).
  - Automatic `platformio.ini` environment generation for all boards based on canonical board manifests.
  - SCons incremental compilation caches object files in isolated board workspaces (`.mcu_flasher_build_cache/boards/<board-key>/`), recompiling only modified or dependent translation units.
- **PlatformIO & Toolchain Management**: Managed via bootstrap virtual environment wrappers (`ensure_platformio_penv_with_hook`). Core dir is junctioned to `C:\.platformio-mcu-gui` on Windows to avoid long path issues.
- **Dynamic Board Search Roots & USB VID/PID Discovery**:
  - `_get_arduino_board_search_roots()` scans both the application's download folder (`Boards/`) and local Arduino packages (`%LOCALAPPDATA%/Arduino15/packages`).
  - `load_downloaded_board_usb_ids()` maps USB VID/PID tuples across all `boards.txt` to recognize connected microcontrollers reliably.
  - Duplicate board display names are disambiguated by appending `(arduino_board_id)`.
- **Pre-built Toolchain Seeding & Multi-Attempt Retries**:
  - `_ensure_platformio_core_prebuilt()` downloads a ~1.7GB pre-built zip from GitHub release with SHA256 verification and resume support.
  - `_PLATFORMIO_SETUP_ATTEMPTS` (3 attempts) with retry loops protects platform installs and dummy compile prewarming against transient network/registry drops.
  - Deferred first-use toolchain setup (`_scan_downloaded_platforms`, `_BOOTSTRAP_DEFAULT_PLATFORMS`) ensures only default families (`espressif32`, `atmelavr`) are prewarmed on initial boot, saving startup time.
- **Operation Phase Scoping (`_active_operation`)**:
  - `_active_operation == "compile"` (**Compile Phase**): Compiles code strictly on host CPU using background threads. Compiler does **not** open or touch the COM port. Serial Monitor, MCU Reset (DTR/RTS pulse), Baud Rate selection, Send input bar, and live serial output reading remain **fully functional and active**.
  - `_active_operation in ("upload", "flash", "reset")` (**Upload Phase**): Flashing tools (`esptool`, `avrdude`) take exclusive ownership of the COM port. Serial Monitor is automatically paused, port closed, and UI controls locked until upload finishes.
- **Serial Monitor**: Uses `pyserial` for thread-safe asynchronous reading in worker threads. Guard checks in `_auto_start_monitor()`, `_run_monitor()`, and `_reset_mcu_from_monitor()` check specifically for `_active_operation in ("upload", "flash", "reset")` to avoid blocking during compilation.
- Console windows on Windows are suppressed via `src/modules/win_subprocess_hide.py`.

### 6. Remote / UNC Network Path Pipeline
- `is_unc_or_network_path()` detects UNC paths (`\\server\share`) and network-mapped drives.
- `_map_unc_for_build()` maps UNC shares to free drive letters for subprocess CWD compatibility.
- `_unmap_unc_after_build()` cleans up mappings in `finally:` blocks.
- `_remote_workspace_root()` routes build workspaces to local fast storage to avoid SMB `.sconsign*.dblite` locking errors.
- **Critical pattern**: Always pre-create the exact target environment directories (`build/<env_name>/`, `libdeps/<env_name>/`) before launching PlatformIO on network paths, because `dbm`/`dblite` on Samba will throw `FileNotFoundError` if the parent dir doesn't exist.
- UNC mapping lifecycle is integrated into `_safe_compile`, `_safe_run` (upload), `_run_hard_reset`, and `_hard_reset_direct_flash`.

### 7. Bootstrap & First-Run Pipeline (`src/modules/bootstrap.py`)
- `_heal_private_python_runtime()`: Auto-detects and repairs damaged/missing portable Python.
- `_ensure_platformio_core_prebuilt()`: Downloads pre-built PlatformIO toolchain zip with progress bar, resume, and SHA256 verification.
- Google Drive downloads: Uses `urllib` (not `requests`) because bootstrap runs before pip dependencies are installed. Handles Google's virus-scan confirmation HTML page by parsing the form and extracting hidden fields.
- `[WinError 32]` file lock handling: `.zip.part` files during download may be locked by antivirus. The bootstrap pipeline retries with exponential backoff and falls back to from-scratch installation.
- Progress reporting is inline to the bootstrap console: download percent, speed (MB/s), and extraction progress.

### 8. Adding New Features, Board Resolution & Tooling
- **Safe Board Resolution Pattern**: In any mixin or helper method needing board metadata (e.g. `platform`, `board`, `framework`, `arduino_board_id`), always call `board_info = self._resolve_board_info()` first. Never assume `board_info` exists in local scope or rely on unverified global dicts.
- All utility modules belong in `src/modules/`. The `src/libs/` directory is legacy (now empty) — do not add new files there.
- Ensure paths are resolved relatively using `Path(__file__).resolve().parent`.
- Settings are persisted in `src/gui_config.json` using `get_editor_mode()` / `set_editor_mode()` pattern.
- Database/settings files go in `src/dbs/`.

---

## Editor Communication Flow (Monaco Mode)

```
┌─────────────────────┐         ┌───────────────────────┐
│  mcu_flash_gui.py   │         │  src/editor/index.html │
│  (Python / Tkinter)  │◄───────►│  (JS / Monaco Editor)  │
│                     │  pywebview│                       │
│  EditorApi class    │  JS↔Py   │  window.pywebview.api  │
│                     │  bridge   │                       │
│  evaluate_js(...)   │─────────►│  loadProject()         │
│                     │          │  reloadActiveFile()    │
│                     │          │  saveAllFiles()        │
│                     │◄─────────│  read_file(path)       │
│                     │          │  save_file(path, data) │
│                     │          │  mark_modified(p, bool)│
│                     │          │  realtime_check_syntax │
└──────────┬──────────┘         └───────────┬───────────┘
           │                                │
           │  Win32 reparenting             │  Monaco decorations
           │  (SetParent/SetWindowPos)      │  (deltaDecorations)
           │                                │
           ▼                                ▼
   Tkinter editor_frame              AI Diff Glow CSS
   (embedded WebView2 HWND)          (green/red animations)
```

---

## AI Edit → Diff Glow Flow

1. **AI edits a file** → `AIController` file watcher detects change → calls `on_ai_edit_func(filepath)`.
2. **`MCUUploadGUI._on_ai_applied_edit()`** → schedules `_reload_current_editor_file()` via `root.after(0, ...)`.
3. **Monaco mode**: `evaluate_js("reloadActiveFile()")` is called.
4. **Hooked `reloadActiveFile()`** (in `index.html`):
   - Captures `oldContent = model.getValue()` before reload.
   - Calls original `reloadActiveFile()` which fetches new content from disk via `pywebview.api.read_file()`.
   - Captures `newContent = model.getValue()` after reload.
   - If different, calls `applyAiDiffHighlights(oldContent, newContent)`.
5. **`applyAiDiffHighlights()`**:
   - Runs LCS-based line diff (`computeLineDiffs()`).
   - Applies Monaco `deltaDecorations` with CSS classes `.ai-edit-added-line` (green glow) / `.ai-edit-removed-line` (red glow).
   - Shows the floating `#ai-diff-banner` with counts.
   - Scrolls editor to first edited line.
6. **User clicks "Dismiss Glow ✖"** → `clearAiDiffHighlights()` removes decorations and hides banner.

---

## Verification & Testing Checklist

- [ ] **GUI Launch**: Verify application opens cleanly with `python mcu_flash_gui.py` or `python launcher.py`.
- [ ] **Toolchain Detection**: Check that Arduino CLI / PlatformIO are detected without thrown exceptions.
- [ ] **Thread Safety**: Ensure GUI updates from worker threads are routed through `root.after()` or queue-based events to prevent UI crashes.
- [ ] **Monaco Editor**: Verify the pywebview editor loads, embeds into the Tkinter frame, and tab switching works.
- [ ] **Default Editor**: Verify the Tkinter-based editor loads files, highlights syntax, and saves correctly.
- [ ] **Editor Detach/Reattach**: Verify detaching and closing the editor via `[X]` does NOT freeze the main app.
- [ ] **Compile Phase Serial Monitor**: Verify Serial Monitor remains open, streaming, and fully functional (Reset DTR, Baud selection, Send bar) while Compiling.
- [ ] **Upload Phase Serial Lock**: Verify Serial Monitor is strictly closed/disabled during Upload Phase to prevent port access conflicts.
- [ ] **AI Diff Highlights**: After an AI edit, verify green/red glowing lines appear and the banner shows correct counts.
- [ ] **Editor Mode Switch**: In Settings, toggle between Default and Monaco — confirm the editor rebuilds without crashes.
- [ ] **AI Assistant**: If OpenCode is installed, verify the "🤖 AI Assistant" button launches and closes the AI terminal.
- [ ] **Project Terminal**: Verify terminal launches, shell switching (pwsh ↔ cmd) works without corrupting PATH or prompt.
- [ ] **Remote/UNC Paths**: Verify compilation and upload succeed from a UNC share (`\\server\share\sketch`).
- [ ] **Bootstrap First Run**: Verify bootstrap self-heals missing Python, downloads PlatformIO prebuilt, and shows progress.
- [ ] **Syntax Compilation**: Run `python -m py_compile mcu_flash_gui.py` — must pass with zero errors.
