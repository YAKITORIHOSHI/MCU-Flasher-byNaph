---
name: mcu-flash-gui-dev
description: "Developer and troubleshooting guide for MCU Flasher on Windows and Ubuntu. Use for the PySide6 glass workspace, offline Monaco editor, coding CLI terminals, serial monitor, exact board/framework pipelines, bounded recovery, resource budgets and native launchers."
---

# MCU Flash GUI Development & Maintenance Skill

Use this skill when developing, debugging, or extending the **MCU Flash GUI** desktop application (`mcu_flash_gui.py`, `main/mcu_flash_gui.py`, `main/qt/`, `main/web_bridge.py`) on Windows 10/11 and native Ubuntu. Ordinary firmware work still follows the live root-sketch boundaries in AGENTS.md.

## Platform Scope

- Windows uses the private portable Python runtime and native launchers. Ubuntu uses `.venv-linux`, `direct/setup_ubuntu.py` and `direct/runThisOnUbuntu.sh`. Detect the host through `src/modules/platform_runtime.py`; never run Windows executables or copied Windows PlatformIO packages on Linux.
- Treat real sketch files and unknown user content as visible, user-owned data.
  Hide only known app-generated project metadata, and keep it writable.

---

## Core Architecture Overview

The application compiles, flashes and monitors exact PlatformIO targets through declared frameworks. ESP32, ESP8266 and AVR retain optimized Windows paths; additional resolved targets use native PlatformIO upload. New hardware needs compatible definitions and packages; do not promise arbitrary future boards. Native **PySide6** panels in `main/qt/` connect to `MCUWebBackendAPI` in `main/web_bridge.py`; `mcu_flash_gui.py` forwards startup.

### Key Components

1. **Native PySide6 (Qt) Desktop Application (`main/qt/` + `main/web_bridge.py`)**
   - High-performance, hardware-accelerated Qt interface, root window: `MCUMainWindow` (`main/qt/main_window.py`).
   - Coordinated by `MCUWebBackendAPI` (`main/web_bridge.py`), which manages background worker threads for compilation, uploading, serial monitoring, board catalog queries, and hardware resets.
   - **Cross-Thread Signal Bus Pattern**: Backend worker threads NEVER touch Qt widgets directly. All telemetry, logs, state transitions, and completion notifications are emitted via `QtSignalBus` (`main/qt/signals.py`) and routed to slots on the Qt main thread.
   - **Critical Operation Protection**: `MCUMainWindow.closeEvent` safeguards against closing the application during sensitive hardware writes (flashing firmware, erasing flash, bootloader recovery resets, and toolchain downloads) to prevent bricking connected microcontrollers or corrupting installations.
   - **Modular UI Suite**:
     - `main_window.py`: Root layout with resizable `QSplitter` panes and dockable bottom tabs.
     - `toolbar.py`: Action controls (Compile, Upload, Clean, Reset, Project, Settings), target board combobox, COM port selector, and baud rate selector.
     - `editor_panel.py`: Embedded offline Monaco Code Editor host (`QWebEngineView` + `QWebChannel`).
     - `console_panel.py`: Colorized build output with regex ANSI color parsing and autoscroll.
     - `serial_panel.py`: High-performance real-time serial monitor with line ending selector, baud rate dropdown, timestamp toggle, and quick send bar.
     - `terminal_panel.py`: Multi-session integrated terminal panel supporting PowerShell (`pwsh`) and CMD tabs.
     - `posix_terminal_panel.py`: Native Linux Bash PTYs and optional OpenCode assistant.
     - `glass.py`: Static workspace backdrop and keyboard focus for tool tabs; no board banner.
     - `icons.py`: Theme-aware SVG action icons without emoji font dependencies.
     - `ai_panel.py`: Collapsible OpenCode AI assistant side panel.
     - `syntax_panel.py`: Interactive AST syntax diagnostic tree with line jump navigation.
     - `compat_panel.py`: Board compatibility matrix and GPIO pinout inspector.
     - `notif_panel.py`: Per-sketch notification log viewer.
     - `settings_dialog.py`: Preferences modal (themes, CPU jobs, auto-save, baud reset).
     - `project_dialog.py`: Project selector & new project scaffolding wizard.
     - `modify_dialog.py`: Project sketch file management dialog (add, rename, delete).
     - `download_dialog.py`: Board package & toolchain download manager with 3rd-party URLs support.
     - `theme.py`: QSS generator for Glass Smoked Dark, Glass Frosted Light and Solarized Dark.

   **Startup readiness contract**
   - Construct the main workspace only after project selection. Do not prewarm hidden WebEngine views while the picker is open. Show the window before scheduling idempotent service startup; the editor loader handles its own readiness.
   - Keep hardware enumeration, root-source scans, parsing and metadata reconciliation off the GUI thread. Use cached ports in controls and populate them through the monitor worker. Defer catalog discovery until initial painting settles; never scan USB package trees at module import.
   - Windows warm launch uses a per-user health snapshot validated against installation path, interpreter/version, startup source fingerprints and required runtime paths. Missing/stale health, explicit repair and recorded crashes use setup. Check immediate child failure before accepting the fast path; never replay hardware writes or CLI commands.
   - Startup of an already-attached MCU is passive: never set manual reset pending or pulse DTR/RTS on initial connection. Reset only from explicit reset controls or the upload flow.

   **Project selection and cancellation contract**
   - Right-clicking the project title or icon opens the native picker; Cancel must safely close the dialog and leave the editor and monitor responsive. Project selector cancellation must be idempotent.

2. **Monaco Editor Frontend (`src/editor/index.html` + `bundle.js` + `qwebchannel.js`)**
   - Embedded offline via `QWebEngineView` and `QWebChannel` (`src/editor/qwebchannel.js`).
   - Self-contained HTML page backed by local offline Monaco `bundle.js` (no CDN dependencies).
   - Python-to-JavaScript bridge exposes `EditorApi` via Qt WebChannel.
   - **Source tabs** use static glass borders, file icons, dirty dots, full-path tooltips, drag ordering and keyboard navigation (arrows/Home/End/Enter/Space). Preserve the offline bundle's models. `role=tab`, roving focus and `aria-selected` must follow the active model.
   - **Ctrl+Click / F12 Go-To-Definition** with cross-file symbol resolution (`findProjectSymbol()`, `parseSymbolDetails()`), including built-in Arduino function registry (`BUILTIN_ARDUINO`).
   - **Ctrl+Hover** floating definition card with function signature, parameters, and return type.
   - **Realtime syntax checking** via `realtime_check_syntax()` → `src/syntax_checker.py` (C++ linting).
   - **AI Edit Diff & Glowing Highlights** — Line-level LCS diff with Monaco decorations (animations disabled under constrained/reduced-motion profiles):
     - 🟢 **Green glow** (pulsing CSS animation `greenGlowPulse`) + `+` gutter icon on added/changed lines.
     - 🔴 **Red glow** (pulsing CSS animation `redGlowPulse`) + `-` gutter icon on removed lines.
     - Floating banner with line count summaries and a quick **"Dismiss Glow ✖"** button.

3. **Dedicated AI Assistant Controller (`src/modules/dedicated_AI.py` & `main/qt/ai_panel.py`)**
   - Manages an **OpenCode AI** terminal session rendered directly inside a right-side collapsible panel.
   - Can be toggled visible/hidden dynamically via the `🤖 AI Assistant` toolbar button or `✖ Hide` header button.
   - `AIController` manages session lifecycle and a file-watcher for AI-applied edits.
   - When AI edits a file, `AIController` triggers editor reload, diff glow animation, and a notification entry.

4. **Project Terminal (`src/modules/project_terminal.py` & `main/qt/terminal_panel.py`)**
   - Full-featured integrated terminal using `pywinpty` + `xterm.js` (ConPTY backend).
   - Supports **PowerShell (`pwsh`)** and **Command Prompt (`cmd`)** sessions with dynamic multi-terminal tabs.
   - Switching and spawning shells maintains individual session state, scrollback, and prompt rendering.
   - Clear removes display/history only; never type `Clear-Host` or `cls` into an active CLI. Kill terminates the selected PTY.
   - Runs as a child process to avoid UI thread contention.
   - Pass xterm capability replies, Unicode, alternate screens and bracketed paste intact. Acknowledge output after parsing; bound outstanding output and history. Preserve the user's CLI PATH and remove Python host environment overrides from shell environments.
   - Coalesce resize/font/theme controls in the Qt panel's bounded worker queue. Only explicit user actions create or restart shells; never replay commands after failure.
   - Build/serial displays bound both pending entries and characters before crossing Qt's event queue. Limit render batches and document/history sizes, including streams without newlines. Show truncation/backlog notices; Clear and ANSI clear must clear retained display history too.
   - Use queued QObject signals for syntax completion, not `QTimer.singleShot` callbacks created inside Python worker threads. Keep one latest pending editor revision and discard stale results. Use one parser on constrained PCs, two otherwise, with bounded revision/diagnostic caches.

5. **Runtime Guards & Crash Detection**
   - **`src/modules/private_python_guard.py`**: Windows private runtime / native Linux virtual-environment guard.
   - **`src/modules/platform_runtime.py`**: Host identification, native paths/locks and CPU startup guard. Fewer than four physical cores stops launch; use logical threads only when topology is unavailable. Unknown logical counts fail with a notice.
   - **`src/modules/runtime_resources.py`**: Shared budgets for 4/6 physical cores, at most 6 logical threads or low RAM. Disable editor animation/minimap, reduce syntax cadence and terminal history. Reserve two logical threads for the interface; clamp saved compiler jobs to CPU/RAM/storage budgets.
   - **`src/modules/recovery.py`**: Bounded retry budgets for transient readers/renderers. Preserve dirty buffers and reject stale serial generations. Errors remain visible. Never retry flashing, erasing, resets or shell commands automatically.
   - **`src/modules/crash_detector.py`**: Session sentinel, unhandled exception recorder, and startup crash recovery marker engine.

6. **Launchers & Native Wrappers**
   - `src/modules/launcher.py`: Python entry point for initializing configuration and launching the main GUI.
   - `src/launcher.cpp`: Native C++ executable wrapper that initializes environment variables and launches silently on Windows without popping a console window.
   - `src/launcher.cs`: C# launcher source (compiles to `MCU_Flasher.exe`).
   - `direct/runThisOnWindows.vbs`: Windows bootstrap launcher that normally uses the current user token, hides the console, and requests targeted UAC only for machine-level setup that requires it.

7. **Realtime C++ Syntax Linter (`src/syntax_checker.py`)**
   - Lightweight C++ AST & regex engine for validating `.ino`, `.cpp`, and `.h` files without invoking full compiler runs.
   - Parses missing semicolons, unmatched brackets/quotes, undeclared variables/functions, populating line-numbered diagnostics into the UI tree and Monaco markers.

8. **Database & Notification Store (`src/dbs/` & `.mcu_flasher_build_cache/`)**
   - **Per-Sketch Notification Store**: Notifications are persisted per-project inside `<sketch_dir>/.mcu_flasher_build_cache/dbs_notif.json`.
   - Fallback global store: `src/dbs/dbs_notif.json`.
   - Managed via modular CRUD operations: `dbs_create.py`, `dbs_read.py`, `dbs_update.py`, and `dbs_delete.py`.

---

## File Map

```
MCU Flasher by Naph/
├── mcu_flash_gui.py                 # Root forwarder script (enforces private python & invokes main/)
├── MCU_Flasher.exe                   # Compiled native Windows launcher (from src/launcher.cs)
├── README.md                         # Comprehensive documentation, user guide & architecture
├── direct/
│   ├── runThisOnWindows.vbs         # Windows bootstrap with early CPU guard
│   ├── runThisOnUbuntu.sh           # Native Linux private-venv launcher
│   ├── setup_ubuntu.py              # Repairable Linux dependency setup
│   ├── verify_runtime.py            # Hardware-free runtime and real Qt checks
│   └── verify_terminal.py           # Windows ConPTY/xterm protocol verification
│
├── main/                             # Native PySide6 (Qt) Desktop UI & Backend Engine
│   ├── __init__.py                  # Public exports (MCUWebBackendAPI, main)
│   ├── mcu_flash_gui.py             # Desktop launcher, core checks & Qt application bootstrapper
│   ├── web_bridge.py                # Asynchronous backend controller & toolchain bridge
│   │
│   ├── qt/                          # Native PySide6 Desktop UI Panels
│   │   ├── main_window.py           # MCUMainWindow root window with dock splitters
│   │   ├── toolbar.py               # Action controls, board/port dropdowns, baud selector
│   │   ├── editor_panel.py          # Monaco Editor panel (QWebEngineView + QWebChannel)
│   │   ├── console_panel.py         # Colorized build and upload console output
│   │   ├── serial_panel.py          # Live serial monitor with baud control & send bar
│   │   ├── terminal_panel.py        # Embedded multi-session terminal panel (pwsh/cmd tabs)
│   │   ├── posix_terminal_panel.py  # Native Linux Bash and optional assistant PTYs
│   │   ├── glass.py                 # Static workspace and focused tool-tab navigation
│   │   ├── icons.py                 # Theme-aware vector icons
│   │   ├── ai_panel.py              # Collapsible OpenCode AI assistant panel
│   │   ├── compat_panel.py          # Compatible boards viewer
│   │   ├── syntax_panel.py          # Interactive AST syntax diagnostic tree
│   │   ├── notif_panel.py           # Per-project notification viewer
│   │   ├── detached_editor.py       # Independent popped-out editor window wrapper
│   │   ├── settings_dialog.py       # Application preferences modal
│   │   ├── project_dialog.py        # Project selector & new project scaffolding wizard
│   │   ├── modify_dialog.py         # Project sketch file management dialog
│   │   ├── download_dialog.py       # Board package & toolchain download manager
│   │   ├── theme.py                 # Precision dark/light QSS stylesheet engine
│   │   └── signals.py               # Centralized QtSignalBus for thread-safe event routing
│   │
│   └── core/                        # Core Foundations & System Services
│       ├── constants.py             # Global constants, regexes, baud rates & telemetry
│       ├── theme.py                 # Theme class, color tokens & styling engine
│       ├── config.py                # Settings persistence, multi-instance PID locks
│       ├── file_utils.py            # Windows attributes (attrib +h), UNC paths, robust file I/O
│       ├── toolchain.py             # PlatformIO & Arduino CLI discovery, junctions
│       ├── board_catalog.py         # Installed/cached/registry board definitions and frameworks
│       ├── target_profile.py        # Exact target validation and upload transport requirements
│       └── board_compat.py          # Board compatibility detection & GPIO pin conflict analyzer
│
├── src/                              # Core System Modules, Offline Assets & Runtime Guards
│   ├── _python/                     # Private portable Python 3 runtime (hidden via attrib +h)
│   ├── .platformio-mcu-gui/         # PlatformIO core store (junctioned to avoid MAX_PATH)
│   ├── gui_config.json              # Persisted user settings (themes, baud rates, auto-save)
│   ├── syntax_checker.py            # Standalone C++ syntax linter & AST analyzer
│   ├── launcher.cpp                 # Native Windows C++ executable wrapper source
│   ├── launcher.cs                  # Native Windows C# launcher source
│   ├── resources.rc                 # Windows executable resource definition
│   │
│   ├── modules/                     # System Services & Execution Drivers
│   │   ├── bootstrap.py             # Windows dependency & runtime bootstrapper
│   │   ├── launcher.py              # Application coordinator & AppUserModelID configurator
│   │   ├── private_python_guard.py  # Strict private Python runtime enforcer
│   │   ├── crash_detector.py        # Session sentinel & crash event recorder
│   │   ├── dedicated_AI.py          # OpenCode AI assistant process controller
│   │   ├── project_terminal.py      # Standalone project terminal server & PTY backend
│   │   ├── platform_runtime.py      # Host detection, native paths/locks and CPU guard
│   │   ├── runtime_resources.py     # Shared low-end CPU/RAM policy
│   │   ├── recovery.py              # Bounded transient retries, no command replay
│   │   ├── arduino_lib_req.py       # Glass Arduino library/board package browser
│   │   ├── detector.py              # USB serial port auto-detection & board probing
│   │   ├── downloader.py            # Resumable downloader with SHA-256 validation
│   │   ├── win_subprocess_hide.py   # Windows CREATE_NO_WINDOW subprocess suppressor
│   │   ├── reset_editor.py          # Historical settings tool; not Qt renderer recovery
│   │   ├── setup_ide_paths.py       # Dynamic compile_commands.json path re-navigator
│   │   └── get-platformio.py        # Bundled official PlatformIO installer script
│   │
│   ├── editor/                      # Offline Monaco Editor Web Assets
│   │   ├── index.html               # Monaco Editor host page with QWebChannel bridge
│   │   ├── terminal.html            # Native Linux xterm frontend and parsed-output ACKs
│   │   ├── bundle.js                # Offline Monaco Editor JS bundle (no CDN)
│   │   ├── qwebchannel.js           # Bidirectional Qt-to-JavaScript communication bridge
│   │   ├── editor.worker.js         # Monaco Editor web worker
│   │   └── *.ttf                    # Offline icon and font assets
│   │
│   ├── assets/                      # Shared Graphical Assets
│   │   ├── mcu_icon.ico             # Application icon
│   │   ├── icons/                   # Custom SVG/PNG checkbox and control assets
│   │   └── xterm/                   # xterm.js assets for terminal/AI panels
│   │
│   └── dbs/                         # Persistent JSON Databases & Notification Stores
│       ├── bootstrap_config.json    # Bootstrap update/skip config
│       ├── arduino_browser_settings.json # Board browser user preferences
│       ├── arduino_cli_path.txt     # Cached Arduino CLI binary path
│       ├── dbs_notif.json           # Global notification fallback store
│       ├── dbs_create.py            # Notification DB CRUD: create
│       ├── dbs_read.py              # Notification DB CRUD: read
│       ├── dbs_update.py            # Notification DB CRUD: update
│       └── dbs_delete.py            # Notification DB CRUD: delete
│
├── installers/                      # Offline Binaries, Toolchains & USB Drivers (Git LFS)
│   ├── .handsoff/                   # Portable Python runtime installers (python-*-amd64.exe)
│   ├── CP210x/                      # Silicon Labs CP210x USB drivers
│   ├── CH34x/                       # WCH CH340 / CH341 USB serial drivers
│   ├── arduino-cli.msi              # Bundled Arduino CLI installer
│   ├── MicrosoftEdgeWebview2Setup.exe # Bundled WebView2 installer
│   └── msys2-*.exe                  # Bundled MSYS2 build tools
│
├── soft_reset/                      # App-Owned Reset Templates & Exact-Board Caches
│   ├── soft_reset_project/          # Minimal PlatformIO reset template for ESP32 / non-AVR
│   └── soft_reset_project_uno/      # Minimal PlatformIO reset template for Arduino AVR
│
├── index_json/                      # Board and library index caches
├── .mcu_flasher_build_cache/        # Per-board build caches and workspaces (hidden, gitignored)
└── logs/                            # Runtime diagnostic logs, crash dumps & mutex locks
```

---

## Key Workflows & Guidelines

### 1. Modifying Native Qt GUI Layout & Panels (`main/qt/`)
- The main window is `MCUMainWindow` (`main/qt/main_window.py`).
- Panels inherit from `QWidget` or `QFrame` and connect to `signals` (`main/qt/signals.py`) or call methods on `self._backend` (`MCUWebBackendAPI`).
- Colors, borders, and margins must come from `main/qt/theme.py` stylesheets or palette tokens.
- **Thread safety**: Workers emit `QtSignalBus` signals to GUI-thread slots. A contextless `QTimer.singleShot` called by a worker is not a substitute for a GUI-thread receiver.
- **Window ownership**: Parent toolbar labels and controls before showing them. Resizing from compact to wide must not expose independent windows. Keep board selection in Controls; no target-board banner.
- **Detached editor**: `DetachedEditorWindow` opens only through Detach Editor. Closing reattaches the existing `MonacoEditorPanel`. Use `takeCentralWidget()` before reparenting, preserve dirty buffers and restore the previous visibility choice.
- **Tabs**: `WorkspaceTabBar` retains native keyboard navigation and a focus ring separate from selection. Source tabs preserve model identity/order, dirty state and accessible focus. Use theme tokens and static glass surfaces; no compositor blur or perpetual animation.
- **Settings accuracy**: Reset and OS-following fallback use `_theme_rev["default"]`. The Qt workspace always hosts offline Monaco with automatic resource settings; do not offer an unused alternate editor engine or describe legacy `editor_mode` as an active control.
- **Critical Operation Protection**: `closeEvent` checks backend state (`is_busy`, `_current_op_phase`, `active_operation`). Destructive phases (`"flashing"`, `"writing"`, `"erasing"`, `"resetting"`, `"cleaning"`) or active toolchain downloads block exit with an explanatory warning. Pure compilation (`"compiling"`) is safely cancellable.

### 2. Modifying the Monaco Editor (`src/editor/index.html` + `qwebchannel.js`)
- Hosted inside `editor_panel.py` using `QWebEngineView`.
- Bidirectional Python ↔ JS bridge:
  - Python calls JS via `evaluate_js("functionName()")`.
  - JS calls Python via `window.pywebview.api.methodName()` (polyfilled through `QWebChannel`).
- Monaco decorations (`deltaDecorations`) are used for diff highlights and hover cards.
- **AI Edit Diff & Glow**: When the AI edits a file, the editor runs a line-level LCS diff and applies animated pulsating CSS classes (`.ai-edit-added-line` for green glow, `.ai-edit-removed-line` for red glow).

### 3. Toolchain & Serial Monitor Execution Pipeline
- **PlatformIO targets**: Validate the exact board and declared framework through `target_profile.py`; never silently substitute a family default. `.ino` projects require Arduino. Registry refresh preserves installed/cache definitions offline. Firmware cache fingerprints include host, board/framework/platform, manifests and sources.
- **On-Demand Board Toolchain Preparation**: Downloaded board packages automatically prepare toolchains during first compile (`prepare_platformio_board_toolchain()`) without requiring an application restart.
- **Process Scheduling Priority**: Background compiler subprocesses are launched with `BELOW_NORMAL_PRIORITY_CLASS` (`0x00004000`) on Windows, ensuring the Qt event loop, Monaco editor, and serial monitor remain responsive under full CPU load.
- **Operation Phase Scoping**:
  - `_active_operation == "compile"`: Serial Monitor, Reset DTR/RTS pulse, Baud Rate selection, and Send bar remain **fully functional and active**.
  - `_active_operation in ("upload", "flash", "reset")`: Flashing tools take exclusive ownership of the COM port; Serial Monitor is temporarily paused and auto-resumes once flashing completes.
- **Post-Upload Silent Auto-Reset & Tab Parity**: Upon successful upload or reset, the application automatically switches to the Serial Monitor tab after a 500ms delay and issues a silent DTR/RTS reset pulse so microcontroller boot logs stream immediately. If an error occurs, focus remains on the Build Console.
- **Reset on Baud Change**: Configurable preference (`reset_on_baud_change`) to toggle automatic DTR/RTS reset pulses when switching baud rates.

### 4. Remote / UNC Network Path Pipeline
- `is_unc_or_network_path()` detects UNC paths (`\\server\share`) and network drives.
- `_map_unc_for_build()` maps UNC shares to free drive letters for subprocess CWD compatibility.
- `_unmap_unc_after_build()` cleans up mappings in `finally:` blocks.
- `_remote_workspace_root()` routes build workspaces to local fast storage to avoid SMB `.sconsign*.dblite` locking errors.

### 5. Bootstrap & First-Run Pipeline (`src/modules/bootstrap.py`)
- Setup uses a resizable native PySide6 glass dialog, not WebView2. Keep its early console fallback usable before Qt is installed. Share theme tokens for status and live progress; persistent QTextCursor anchors must survive log trimming. Closing/hiding must not be undone by the topmost timer.
- The downloader (`arduino_lib_req.py`) retains Tk/ttk network and archive workflows. `tk_glass.py` supplies static cards; advanced URLs live behind **Board indexes**. Preserve search, installed-item details, version selection, checksum validation and cancellation. Reopening refreshes the per-user theme without rebuilding widgets or losing state.
- `browser_loading.py` validates compact catalogs against source path/mtime/size and schema, rebuilding damaged or stale derived caches. Cache writes are atomic and bounded. Keep large raw JSON out of the RAM cache, publish cached data before HTTP refresh, and use two network workers on constrained hosts (four otherwise). `TkTasks` is the Tk-thread delivery boundary; workers never call Tcl. Installed inventories and detail selections use one active scan plus one latest request; cancel stale scans and stop dispatcher timers when idle. Verify with `direct/verify_browser_loading.py` using temporary catalogs and mocked HTTP.
- Settings' legacy `graphics_acceleration` key controls continuous splitter resize, not the GPU. Unsaved defaults use divider previews on constrained devices; preserve explicit choices. Save a complete preference set once and report persistence failure. Reset controls must revalidate target/framework/port and busy state in their callbacks.
- `main/qt/responsive.py` owns current-monitor work-area fitting and coalesced screen/font signals. Qt screen geometry and font widths are logical pixels: never multiply them by DPI/DPR. Use `ui_metrics.py` for coordinate arithmetic, including negative monitor origins and frame clearance. Reflow Controls, serial headers and dialog fields; do not let wide size hints lock compact windows. Preserve saved editor/terminal fonts. Tk uses its own native point/pixel scale, coalesced panes and scrollable installed details; cancel bound callbacks on destruction.
- Verify screen changes and 100/125/150/200% scale in fresh processes with `direct/verify_responsive.py`. Keep the same hardware-free persistence mocks as the controls verifier. Check full baud digits beside combo padding/arrows and footer visibility, not just widget containment.
- Short-screen pane budgets use the active tool's minimum size hint. Keep log views shrinkable while preserving headers and serial send controls. Check every tool tab in the runtime preview. Mock terminal session creation for layout probes; real PTY checks belong in `direct/verify_terminal.py`.
- `_heal_private_python_runtime()`: Auto-detects and repairs damaged/missing portable Python.
- `_ensure_platformio_core_prebuilt()`: Downloads pre-built PlatformIO toolchain zip with progress bar, resume, and SHA-256 verification.
- **Strict Private Python Guard**: `src/modules/private_python_guard.py` ensures the application never executes under desktop/system Python.
- Progress reporting is streamed inline with download percent, speed (MB/s), and extraction progress.

---

## Verification & Testing Checklist

- [ ] **GUI Launch**: Verify application opens cleanly with `python mcu_flash_gui.py` or double-clicking `MCU_Flasher.exe`.
- [ ] **Hardware core requirements**: Verify fewer than 4 physical cores is blocked (logical fallback if topology unavailable); unknown logical counts stop with a diagnostic. Verify 4/6-core policy and saved job clamping.
- [ ] **Manual Startup Hardware Selection**: On startup, verify that no board and no port are automatically selected, and action buttons (Compile, Upload, Reset) remain disabled until the user manually selects them.
- [ ] **Critical Operation Protection**: Verify attempting to close the application during active flashing, erasing, or downloading triggers a safety warning and keeps the app open.
- [ ] **Compile Phase Serial Monitor**: Verify Serial Monitor remains open, streaming, and fully functional (Reset DTR, Baud selection, Send bar) while compiling.
- [ ] **Upload Phase Serial Lock & Parity**: Verify Serial Monitor is paused during flashing, switches to Serial Monitor tab after 500ms on success, issues silent reset pulse, and auto-resumes monitoring.
- [ ] **Error Visibility Retention**: Verify failing an upload or compile keeps the Build Console focused so error logs are immediately readable.
- [ ] **Reset on Baud Change**: Verify toggling the setting in Settings Dialog reboots the MCU on baud change when enabled, and preserves state when disabled.
- [ ] **Window ownership**: Resize across compact/wide layouts and verify there are no visible top-level widgets except the main window and explicitly requested dialogs/editor. Verify board banner absence.
- [ ] **Monaco editor**: Verify offline models, dirty tab indicators, drag ordering, keyboard tab navigation and selected/focus contrast in dark/light/Solarized. Verify detach/close/reattach and dirty-buffer renderer recovery.
- [ ] **Project Terminal**: Verify terminal spawns, multi-terminal tabs work, shell switching (`pwsh` ↔ `cmd`) operates cleanly, and clear/kill functions operate as expected.
- [ ] **AI Assistant & Diff Glow**: Verify OpenCode AI panel connects, edits trigger green/red glowing lines, and the dismiss banner clears highlights.
- [ ] **Remote/UNC Paths**: Verify compilation and upload succeed from a UNC share (`\\server\share\sketch`).
- [ ] **Syntax Compilation**: Run `python -m py_compile main/mcu_flash_gui.py` — must pass with zero errors.

### Hardware-free verification

Use the app's private runtime. On Windows:

```powershell
& src/_python/python.exe -B direct/verify_runtime.py --render-dir temp/audit/workspace
& src/_python/python.exe -B direct/verify_runtime.py --preview-cpus 4
& src/_python/python.exe -B direct/verify_terminal.py
& src/_python/python.exe -B direct/verify_performance.py
& src/_python/python.exe -B direct/verify_controls.py
& src/_python/python.exe -B direct/verify_browser_loading.py
```

On Ubuntu use `.venv-linux/bin/python -B direct/verify_runtime.py`. The Windows
terminal probe exercises the real PTY and local xterm renderer, including installed
CLI version commands; it does not authenticate or request AI work. CPU previews
simulate policy and cannot prove timing on a physical low-end machine.

`verify_controls.py` extracts only bootstrap UI definitions and uses a downloader
fixture without its constructor. Keep installation, launch, network and live
configuration writes mocked. Native captures use `QT_QPA_PLATFORM=windows` or
Ubuntu `xcb` under a desktop/Xvfb; headless Tk skips must be reported.

Patch persistence, hardware and metadata generation in verifier fixtures; never
write live sketch/cache/journal state. Store screenshots and scratch scripts in
`temp/`. Keep README, Ubuntu notes and generated AGENTS application guidance
aligned. Native Ubuntu and physical board compile/upload results must be reported
separately from local Windows verification.
