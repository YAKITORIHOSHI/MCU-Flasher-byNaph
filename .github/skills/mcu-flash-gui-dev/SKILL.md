---
name: mcu-flash-gui-dev
description: "Developer and troubleshooting guide for the Windows MCU Flash GUI (mcu_flash_gui.py, launcher.py, dedicated_AI.py). Use when modifying, debugging, or enhancing the Tkinter flasher interface, serial monitor, Arduino CLI or PlatformIO pipelines, board caches, or Windows launchers."
---

# MCU Flash GUI V6.0 Development & Maintenance Skill

Use this skill when developing, debugging, or extending the **MCU Flash GUI V6.0** desktop application (`mcu_flash_gui.py`) and its supporting execution ecosystem on Windows 10/11.

## Platform Scope

- Focus on the Windows main app, `mcu_flash_gui.py`, Windows launchers (`launcher.py`, `runThisOnWindows.vbs`, `src/launcher.cpp`), and supporting modules.
- Treat real sketch files and unknown user content as visible, user-owned data.
  Hide only known app-generated project metadata, and keep it writable.

---

## Core Architecture Overview

The project provides a modern Windows desktop interface for compiling, flashing, and monitoring ESP32 / Arduino microcontrollers. The main file (`mcu_flash_gui.py`, ~17 000 lines) is a monolithic Tkinter application with an embedded code editor, dual toolchain support, and an integrated AI assistant.

### Key Components

1. **Main GUI Application (`mcu_flash_gui.py`)**
   - Tkinter-based dark-themed GUI (`MCU Flasher by Naph`), main class: `MCUUploadGUI`.
   - **Dual editor modes** switchable at runtime via Settings dialog:
     - `"default"` — Pure Tkinter `tk.Text` tabbed editor with custom syntax highlighting, line numbers, auto-indent, and bracket matching (built inside `_build_editor_default()`).
     - `"monaco"` — Monaco Editor (VS Code engine) embedded in a pywebview WebView2 window that is Win32-reparented into the Tkinter frame (built inside `_build_editor_monaco()` → pywebview starts in `_run_editor_webview_loop()`).
   - Editor mode is persisted in `src/gui_config.json` under `shared.editor_mode`.
   - Serial port auto-detection, baud rate selection, and live serial monitor.
   - Dual toolchain support: **Arduino CLI** and **PlatformIO**.
   - Supporting classes: `EditorApi` (pywebview JS↔Python bridge), `MonacoAutosaveWorker`, `Theme` (color constants), `ProjectSelectorDialog`, `BoardSearchDialog`, `ToolTip`.

2. **Monaco Editor Frontend (`src/editor/index.html` + `bundle.js`)**
   - Self-contained HTML page loaded by pywebview, backed by a local offline Monaco `bundle.js` (no CDN).
   - Exposes global JS functions called from Python via `editor_window.evaluate_js()`:
     - `loadProject()`, `saveAllFiles()`, `saveActiveFile()`, `reloadActiveFile()`.
   - `EditorApi` (Python side) is exposed as `window.pywebview.api` (JS side), providing:
     - `get_project_files()`, `read_file(path)`, `save_file(path, content)`, `mark_modified(path, bool)`, `set_active_file(path)`, `realtime_check_syntax(path, content)`, `run_action(action)`, `save_tab_order(paths)`, `on_editor_content_change()`.
   - **Tab bar** with drag-and-drop reordering, dirty-dot indicators, and persistent tab order (`.mcu_flash_tab_order.json`).
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

3. **Dedicated AI Assistant Controller (`dedicated_AI.py` & Right-Side Panel)**
   - Manages an **OpenCode AI** terminal session rendered directly inside a right-side embedded panel within the main window (`self.h_split_pane` → `self.ai_side_container`).
   - On Windows, the pywebview OS window is reparented into `self.ai_embed_frame` via Win32 `SetParent`, sitting permanently attached right beside the Editor and Monitor panes.
   - Can be toggled visible/hidden dynamically via the `🤖 AI Assistant` toolbar button or `✖ Hide` header button, expanding the Editor & Monitors to full width when hidden.
   - Key exports imported by `mcu_flash_gui.py`: `AIController`, `is_opencode_installed`.
   - `AIController` class: manages launch/close lifecycle, toolbar button state animation, file-watcher for AI-applied edits.
   - When AI edits a file, `AIController` calls `on_ai_edit_func` (wired to `MCUUploadGUI._on_ai_applied_edit()`), which reloads the editor, shows green/red diff glows, and logs a notification in the Notifications tab.

4. **QScintilla External Viewer (`src/qscintilla_editor.py`, `src/qscintilla_viewer.py`)**
   - Optional rich code viewer using QScintilla (PyQt5). Launched as a subprocess via `sys.argv[1]`.
   - Shares syntax error data with the main GUI through a temp JSON file (`.mcu_flash_syntax_errors.json`).

5. **Launchers & Native Wrappers**
   - `launcher.py`: Python entry point for initializing configuration and launching the main GUI.
   - `src/launcher.cpp`: Native C++ executable wrapper that initializes environment variables and launches `launcher.py` / `mcu_flash_gui.py` silently on Windows without popping a console window.
   - `runThisOnWindows.vbs`: Windows bootstrap launcher (elevates when needed, hides console).

6. **Realtime C++ Syntax Linter (`src/syntax_checker.py`)**
   - Lightweight C++ AST & regex engine for validating `.ino`, `.cpp`, and `.h` files without invoking full compiler runs.
   - Parses missing semicolons, unmatched brackets/quotes, undeclared variables/functions, and syntax errors, populating line-numbered diagnostics into the UI tree and Monaco markers.

7. **Database & Notification Store (`src/dbs/`)**
   - Persistent JSON notification store and event database (`dbs_notif.json`).
   - Managed via modular CRUD operations: `dbs_create.py`, `dbs_read.py`, `dbs_update.py`, and `dbs_delete.py`.
   - Stores build warnings, upload events, status logs, and MCU reset notifications.

8. **Toolchain Execution & Utilities (`src/modules/` & `src/libs/`)**
   - `src/modules/bootstrap.py`: Manages private Python environment setup/auto-heal, PlatformIO virtual environments (`penv`), Arduino CLI binaries, Node.js LTS, and OpenCode AI Assistant. Junctions PlatformIO core to avoid MAX_PATH (>260 char) issues.
   - `_heal_private_python_runtime()`: Self-repair mechanism that detects missing or damaged portable Python at `src/_python/`, reinstalling it silently from `installers/.handsoff/python-*-amd64.exe` with `attrib +h` project isolation.
   - `src/libs/arduino_lib_req.py`: Resolves required C++ headers and auto-downloads missing Arduino libraries.
   - `src/libs/detector.py`: Windows USB serial port auto-detection and board identification.
   - `src/libs/win_subprocess_hide.py`: Suppresses console window popups when spawning background subprocesses on Windows (`CREATE_NO_WINDOW`).
   - `src/libs/reset_editor.py`: Utility to reset cached editor configuration and state.

---

## File Map

```
MCU Flasher by Naph/
├── mcu_flash_gui.py           # Main GUI application (~17k lines, monolithic)
├── dedicated_AI.py            # OpenCode AI controller (pywebview + xterm.js + pywinpty)
├── launcher.py                # Python entry point / bootstrapper
├── runThisOnWindows.vbs       # Windows VBS launcher
├── arduino_cli_path.txt       # Arduino CLI path config
├── arduino_browser_settings.json
├── bootstrap_config.json
├── installers/                # Bundled silent installers & drivers
│   ├── .handsoff/             # Offline Python runtime installers (python-*-amd64.exe)
│   ├── CP210x/                # CP210x USB-to-UART drivers
│   ├── arduino-cli.msi        # Bundled Arduino CLI installer
│   ├── MicrosoftEdgeWebview2Setup.exe # Bundled WebView2 installer
│   └── msys2-*.exe            # Bundled MSYS2 build tools
├── src/                       # Application core modules and assets
│   ├── _python/               # Private portable Python 3 runtime (hidden attribute)
│   ├── env/                   # Virtual environment created on bootstrap
│   ├── gui_config.json        # Persisted GUI settings (editor_mode, autosave, baud rates, themes)
│   ├── syntax_checker.py      # Realtime C++ syntax linter & AST analyzer
│   ├── qscintilla_editor.py   # QScintilla code editor component (PyQt5)
│   ├── qscintilla_viewer.py   # QScintilla read-only viewer component (PyQt5)
│   ├── launcher.cpp           # Native Windows executable wrapper (suppresses console popup)
│   ├── editor/                # Monaco Editor offline Web UI
│   │   ├── index.html         # Monaco Editor HTML (tabs, toolbar, diff glows, hover, go-to-def)
│   │   ├── bundle.js          # Monaco Editor offline JS bundle (no CDN dependency)
│   │   └── *.ttf              # Offline icon and font assets
│   ├── modules/               # Core bootstrap and environment management
│   │   └── bootstrap.py       # Windows dependency & runtime bootstrapper (auto-heal, penv, tools)
│   ├── libs/                  # Additional utility libraries
│   │   ├── arduino_lib_req.py # Arduino library dependency resolver & downloader
│   │   ├── detector.py        # USB serial port auto-detection & board probing
│   │   ├── win_subprocess_hide.py  # Windows subprocess console suppression
│   │   └── reset_editor.py    # Editor state reset utility
│   ├── dbs/                   # Persistent JSON notification & event database store
│   └── fonts/                 # Bundled custom font assets (Montserrat, etc.)
└── index_json/                # Arduino board/library index caches
```

---

## Key Workflows & Guidelines

### 1. Modifying GUI Layout & Components (`mcu_flash_gui.py`)
- The main GUI class is `MCUUploadGUI` (line ~3002).
- Standard Tkinter / `ttk` widgets are used for controls. Colors come from the `Theme` class (line ~1564).
- Maintain dark-mode visual hierarchy and styling consistency.
- Avoid dynamic layout jitter: compute container bounds cleanly without arbitrary pixel hardcoding.
- Maintain non-blocking operations: compile, upload, and serial reading tasks MUST run on background threads using `threading.Thread`.
- **Thread safety**: All GUI updates from worker threads MUST go through `self.root.after(0, callback)`.

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

### 3. Modifying the AI Assistant (`dedicated_AI.py`)
- `AIController` is instantiated in `MCUUploadGUI.__init__()` (line ~3014) only if OpenCode CLI is installed.
- It receives a callback `on_ai_edit_func` wired to `MCUUploadGUI._on_ai_applied_edit()`.
- The AI terminal uses pywebview + xterm.js rendering with a pywinpty ConPTY backend.
- `TerminalServer` runs HTTP (page serve) + WebSocket (PTY I/O) on a free port pair.
- When adding features: keep the dedicated_AI module self-contained — it should only export `AIController`, `is_opencode_installed`, and optionally `DedicatedAIApp`.

### 4. Toolchain & Serial Monitor Execution Pipeline
- **Arduino CLI**: Invoked via sub-process execution using paths resolved from `arduino_cli_path.txt` or system PATH. Bootstrap logic in `src/libs/bootstrap.py`.
- **PlatformIO**: Managed via bootstrap virtual environment wrappers (`ensure_platformio_penv_with_hook`). Core dir is junctioned to `C:\.platformio-mcu-gui` on Windows to avoid long path issues.
- **Operation Phase Scoping (`_active_operation`)**:
  - `_active_operation == "compile"` (**Compile Phase**): Compiles code strictly on host CPU using background threads. Compiler does **not** open or touch the COM port. Serial Monitor, MCU Reset (DTR/RTS pulse), Baud Rate selection, Send input bar, and live serial output reading remain **fully functional and active**.
  - `_active_operation in ("upload", "flash", "reset")` (**Upload Phase**): Flashing tools (`esptool`, `avrdude`) take exclusive ownership of the COM port. Serial Monitor is automatically paused, port closed, and UI controls locked until upload finishes.
- **Serial Monitor**: Uses `pyserial` for thread-safe asynchronous reading in worker threads. Guard checks in `_auto_start_monitor()`, `_run_monitor()`, and `_reset_mcu_from_monitor()` check specifically for `_active_operation in ("upload", "flash", "reset")` to avoid blocking during compilation.
- Console windows on Windows are suppressed via `src/libs/win_subprocess_hide.py`.

### 5. Adding New Features or Tooling
- Keep utility functions modular inside `src/libs/`.
- Ensure paths are resolved relatively using `Path(__file__).resolve().parent`.
- Cross-platform support: Test path compatibility with both Windows (`\\`) and POSIX (`/`) separators.
- Settings are persisted in `src/gui_config.json` using `get_editor_mode()` / `set_editor_mode()` pattern.

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
- [ ] **Compile Phase Serial Monitor**: Verify Serial Monitor remains open, streaming, and fully functional (Reset DTR, Baud selection, Send bar) while Compiling.
- [ ] **Upload Phase Serial Lock**: Verify Serial Monitor is strictly closed/disabled during Upload Phase to prevent port access conflicts.
- [ ] **AI Diff Highlights**: After an AI edit, verify green/red glowing lines appear and the banner shows correct counts.
- [ ] **Editor Mode Switch**: In Settings, toggle between Default and Monaco — confirm the editor rebuilds without crashes.
- [ ] **AI Assistant**: If OpenCode is installed, verify the "🤖 AI Assistant" button launches and closes the AI terminal.
