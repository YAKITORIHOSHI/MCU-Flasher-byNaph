---
name: mcu-flash-gui-dev
description: "Developer and troubleshooting guide for MCU Flasher on Windows and Ubuntu. Use for the PySide6 glass workspace, offline Monaco editor, coding CLI terminals, serial monitor, exact board/framework pipelines, bounded recovery, resource budgets and native launchers."
---

# MCU Flash GUI Development & Maintenance Skill

Use this skill when developing, debugging, or extending the **MCU Flash GUI** desktop application (`mcu_flash_gui.py`, `main/mcu_flash_gui.py`, `main/qt/`, `main/web_bridge.py`) on Windows 10/11 and native Ubuntu. Ordinary firmware work still follows the live root-sketch boundaries in AGENTS.md.

## Platform Scope

- Keep host implementations in separate files: `main/platforms/windows.py` owns Windows paths and executable discovery; `main/platforms/ubuntu.py` owns native PlatformIO paths and POSIX process sessions. `main/core/toolchain.py` is the stable host-selecting API, and `main/core/build_resources.py` shares CPU/RAM/storage budgets. Core exports stay lazy to avoid Ubuntu-first import cycles and unnecessary board discovery. Runtime must never import bootstrap installers.
- Windows uses the private portable Python runtime and `direct/windows/run.vbs`. Ubuntu uses `.venv-linux`, `direct/ubuntu/setup.py`, `direct/ubuntu/run.sh` and `direct/ubuntu/requirements.txt`. Older direct launch/setup paths remain compatibility forwarders. Detect the host through `src/modules/platform_runtime.py`; never run Windows executables or copied Windows PlatformIO packages on Linux. Ubuntu replaces inherited foreign package/cache/interpreter paths and uses native uploads for every family; setup repairs only the local venv.
- Python dependencies live in the target environment. Do not install Qt into the base runtime again after verifying env. Windows pip children use the same target interpreter through its verified short spelling (extended-path fallback), and a short physical temporary folder so wheel builders do not exceed path limits while canonicalizing core junctions. Do not use an extended-path TEMP for pip: sdist member names mix forward and native slashes. Keep installer temp routing scoped to Python; native compilers retain their own path contracts. Verify with `direct/verify_bootstrap_pip_paths.py` and an empty-runtime install fixture.
- Treat real sketch files and unknown user content as visible, user-owned data.
  Hide only known app-generated project metadata, and keep it writable.

---

## Core Architecture Overview

Zephyr compatibility uses `src/modules/zephyr_compat.py` and its app-owned CMake
alias file, never edits to installed platform or framework sources. Apply only
verified same-hardware aliases with matching framework version and target
metadata; preserve explicit user aliases. For reviewed STM32 20.0.0/Zephyr
4.4.2, explicitly report the absent Ebyte E77, STM32F405 MicroMod and Oceanus-I
module Zephyr definitions. Keep their Arduino frameworks and exact identities.
Persist unavailable-framework reasons in the prepared catalog, overlay them on
local manifest discovery and reject stale framework choices. Unknown versions,
custom board roots or supplied aliases retain normal preparation checks.
Apply the same exact-version/source/manifest guards to the absent Olimex
STM32-H103 Mbed target in framework-mbed 6.61700.231105. Retain its other
frameworks and never substitute another board with the same MCU. Startup health
tracks the compatibility modules, migration file and availability validators.

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
     - `icons.py`: Original theme-aware circuit-chip and action vectors without emoji font dependencies.
     - `setup_components.py`: Cached glass cards, vector glyphs, status chips and compact determinate progress rows.
     - `ai_panel.py`: Collapsible OpenCode AI assistant side panel.
     - `syntax_panel.py`: Interactive AST syntax diagnostic tree with line jump navigation.
     - `compat_panel.py`: Board compatibility matrix and GPIO pinout inspector.
     - `notif_panel.py`: Per-sketch notification log viewer.
     - `settings_dialog.py`: Preferences modal (themes, CPU jobs, auto-save, baud reset).
     - `project_dialog.py`: Project selector & new project scaffolding wizard.
     - `modify_dialog.py`: Project sketch file management dialog (add, rename, delete).
     - `download_dialog.py`: Explicit handoff to the separate host bootstrap process.
     - `theme.py`: QSS generator for Glass Smoked Dark, Glass Frosted Light and Solarized Dark.

   **Theme and log readability**
   - Build and serial output, ANSI foregrounds, connection state and transient status-bar messages use semantic colors with readable contrast against the active theme. Recolor retained output after theme changes instead of leaving stale inline colors.
   - Check rendered compatibility logs, syntax severity/status brushes and notification cards against their actual reading surfaces. Share terminal ANSI colors between Windows and POSIX and enforce xterm minimumContrastRatio=4.5 for truecolor/indexed CLI text. Verify with `direct/verify_theme_readability.py` and `direct/verify_panel_readability.py`.
   - The workspace owns one theme signal connection and one propagation pass. Hosted panels opt out of direct theme subscriptions; standalone panels retain them. Status timers are window-owned, restart for the latest message and never clear a newer operation status. Failed notification clears preserve history; filters use bounded retained records and apply to live events. Refresh external history off the GUI thread on panel reveal, discard stale project results and preserve unpersisted live events. Report failed history writes without recursively persisting the warning. Notification replacement must be atomic; verify failure preservation with the isolated notification persistence/writes verifiers.
   - Keep Controls checkboxes at intrinsic width so Timestamps and Skip Compile remain a compact pair at wide sizes; let responsive reflow handle constrained widths.

   **Startup readiness contract**
   - Construct the main workspace only after project selection. Do not prewarm hidden WebEngine views while the picker is open. Show the window before scheduling idempotent service startup; the editor loader handles its own readiness.
   - Keep hardware enumeration, root-source scans, parsing and metadata reconciliation off the GUI thread. Use cached ports in controls and populate them through the monitor worker. Defer catalog discovery until initial painting settles; never scan USB package trees at module import.
   - Windows warm launch uses a per-user health snapshot validated against installation path, interpreter/version, startup source fingerprints and required runtime paths. Missing/stale health, explicit repair and recorded crashes use setup. Check immediate child failure before accepting the fast path; never replay hardware writes or CLI commands.
   - Startup of an already-attached MCU is passive: never set manual reset pending or pulse DTR/RTS on initial connection. Reset only from explicit reset controls or the upload flow.

   **Board selection and search contract**
   - Enable Compile when a board is selected. Serial Upload needs a selected port; a verified board's declared native USB/programmer interface can upload without COM. Unknown transports require a port. Describe the native interface in Upload's tooltip. Refresh directly from selection signals and operation completion; keyboard shortcuts recheck the same rules after asynchronous save. Keep exact target/framework validation in the backend pipeline.
   - Compile/Upload must repair unresolved cached Arduino rows in their request worker before target validation. Match installed definitions from the same app-owned store used for builds; disregard an unrelated inherited core directory. Normalize the Arduino build-define prefix and only the manifest's declared vendor prefix when comparing names. Preserve ambiguity rejection and framework constraints. Runtime never queries online registries; missing definitions require bootstrap preparation. Refresh must replace stale empty-ID rows under their existing display names, not retain them or add duplicate aliases.
   - Board picking uses the published catalog without starting a registry refresh on every open. Refresh boards is explicit. Build the immutable metadata index and search it in one background worker with one latest pending request, queued Qt completion and stale-generation rejection. Debounce typing briefly; Enter waits for the current query. Virtualize rows with QListView, bound index/query caches, preserve category filters, recents, keyboard navigation and framework selection, and stop/disconnect closed pickers.
   - During local discovery, compute Arduino/PlatformIO identity, token and define evidence once per refresh and reuse only that pass's candidate string-matching state. Keep the same pair scores, framework/MCU exclusions, ambiguity margin and tie ordering. Rebuild evidence after metadata changes; never persist or share mutable matchers between refresh workers. Verify batch identity changes and ambiguity with `direct/verify_target_resolution.py`.
   - Verify board gating with `direct/verify_controls.py` and search/ranking, responsiveness and lifecycle with hardware-free `direct/verify_board_search.py`. Its optional benchmark compares this checkout's HEAD search class using isolated fixtures; captures/reports go in `temp/`.
   - `direct/verify_target_resolution.py` verifies stale catalog repair through the actual Compile/Upload entry points. On Windows its optional `--compile-installed-esp32` copies installed packages into `temp/` and checks the real Compile button, Arduino conversion and firmware build. Mock persistence/hardware, keep all build outputs in the fixture, and never upload or run setup against the live store. Allow about 1.2 GB scratch space for the isolated package copies.
   - Preserve manifest `upload.protocol`, `upload.require_upload_port` and `upload.speed` through discovery, resolution and refresh. Use bootloader defaults for non-ESP targets; show their speed or Auto with the control disabled. Re-read transport metadata after bootstrap preparation and never pass a serial argument to a native programmer. Uninstalled/unverified transports use the generic PIO worker; only installed Arduino serial targets use optimized family paths. Abort serial uploads if the port changes during compilation; never replay hardware writes.
   - All downloads/installations belong to bootstrap. `offline_bootstrap.py` prepares `direct/offline-packages.json`: all configured platforms, every declared board/framework package variant (including optional uploader/debug tools), builder preparation and configured sketch libraries. Verify transitive package dependencies, respecting framework-bundled libraries; missing dependencies must not become a success certificate after a PlatformIO warning. Both host setup paths run this before launch. Record readiness only after complete success; validate host, architecture, plan and local package files. Custom libraries/platforms belong in the plan. Never fabricate PlatformIO package metadata or defer installation to Compile/Upload/Reset.
   - The default plan prepares AVR and ESP32 with Arduino. An explicit nonempty `frameworks` list selects declared frameworks for preparation; omission preserves complete framework coverage for configured platforms. Certify only the actual plan and explain that other installed targets may need an expanded plan. Preserve optional uploader/debug tools and unmatched package variants; exclude framework-specific packages only with manifest/configuration evidence. Never reduce coverage silently to shorten setup.
   - `offline_runtime.py` and `offline_platformio.py` enforce no runtime downloads while allowing loopback terminal traffic and installed/local symlink packages. Bootstrap installs a conditional `.pth` guard for Python children; the root comes from the launch environment so moves stay supported. Main entry points check offline readiness before opening the workspace. Setup handoffs remove inherited runtime/no-index variables. Runtime board refresh reads local manifests/catalog snapshots; the Bootstrap toolbar action opens a separate setup process and rechecks busy state. Keep Monaco/xterm/assistant assets local and remove pip/CDN fallbacks.
   - The real project build verifies source compatibility with the declared framework. A single declared native framework can be selected automatically; multiple native frameworks stay explicit. Require Arduino for .ino sources. Cache identity reflects current framework/definition on every call, across windows; recognize BIN/HEX/UF2/ELF outputs. Use `direct/verify_offline.py` for isolated package planning, startup certificates, setup handoffs, remote-network denial/local sockets and actual PlatformIO missing-package rejection. Mock all bootstrap installers/builders; never run live setup during verification.
   - Use `direct/verify_board_families.py` for isolated multi-family framework/transport checks and native Upload-button clicks. Optional `direct/verify_target_resolution.py --compile-installed-avr` builds Uno, Nano ATmega328 and Mega 2560 through the actual Compile button with copied installed host-native packages. Ubuntu accepts an explicit read-only `--avr-core` store and refuses copied Windows stores. No verifier runs installs, uploads, or live persistence. The Ubuntu CI integration provisions native AVR packages separately in `temp/` before running this probe.
   - `direct/verify_platforms.py` checks host/import order, native environment replacement, guarded runtime discovery, mocked venv setup/argument forwarding, POSIX upload sessions and Bash/Windows Script Host syntax. Syntax checks never execute installers or launchers; Windows Script Host skips on Linux. Report native Ubuntu CI results separately from Windows simulations.

   **Project selection and cancellation contract**
   - Right-clicking the project title or icon opens the native picker; Cancel must safely close the dialog and leave the editor and monitor responsive. Project selector cancellation must be idempotent.
   - Workspace Project / Ctrl+O asks whether each selected or created project should use the current window or an independent private-runtime process. Opening in a new window preserves the current editor, terminal and target state, including during compilation. Switching the current window revalidates busy state and asks to save, discard or cancel when editor buffers are dirty; await the Monaco WebChannel save acknowledgement before switching. Startup selection opens the first project directly.
   - The picker's Open projects tab lists live registered windows with full folder paths. Reopening a sketch focuses its existing window. Windows uses native foreground activation; Ubuntu uses a same-user local Qt activation endpoint. Never duplicate ownership of one project or serial port.
   - `config_store.py` serializes preference transactions and merges changed snapshot keys across processes. Project/port claims must be atomic, handle failed persistence, and precede sketch writes or serial connection. A new project never overwrites an existing folder. Mock process spawning and metadata in verification.
   - Persist instance settings and shared theme changes in one transaction; report failure and keep the active theme unchanged. After a confirmed project switch, prune only snapshots from discarded projects; retain current-project dirty buffers and recovery through theme changes, resize and detach/reattach.
   - Give live notification events unique IDs shared with persisted records so asynchronous history refresh cannot erase an unpersisted event or duplicate a saved one. Settings may carry full `history_message` text separately from the short status message.

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
   - `direct/windows/run.vbs`: Windows bootstrap launcher that normally uses the current user token, hides the console, and requests targeted UAC only for machine-level setup that requires it. The old VBS path forwards here; preserve ASCII/CRLF for Windows Script Host.
   - `direct/ubuntu/setup.py` and `run.sh`: Native venv setup/launch with no system pip writes; `run.sh --repair` explicitly repairs dependencies. Preserve LF shell line endings, project paths with spaces, and `--new-window` forwarding.

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
│   ├── windows/run.vbs             # Windows bootstrap with early CPU guard
│   ├── ubuntu/run.sh               # Native Linux private-venv launcher
│   ├── ubuntu/setup.py             # Repairable Linux dependency setup
│   ├── ubuntu/requirements.txt     # Native Linux dependencies
│   ├── runThisOnWindows.vbs         # Compatibility forwarder
│   ├── runThisOnUbuntu.sh           # Compatibility forwarder
│   ├── setup_ubuntu.py              # Compatibility forwarder
│   ├── verify_platforms.py          # Isolated host and launcher contracts
│   ├── verify_runtime.py            # Hardware-free runtime and real Qt checks
│   └── verify_terminal.py           # Windows ConPTY/xterm protocol verification
│
├── main/                             # Native PySide6 (Qt) Desktop UI & Backend Engine
│   ├── __init__.py                  # Public exports (MCUWebBackendAPI, main)
│   ├── mcu_flash_gui.py             # Desktop launcher, core checks & Qt application bootstrapper
│   ├── web_bridge.py                # Asynchronous backend controller & toolchain bridge
│   │
│   ├── platforms/                   # Separate Windows and Ubuntu host implementations
│   │   ├── windows.py              # Bootstrap, aliases and Windows executables
│   │   └── ubuntu.py               # Native packages, paths and POSIX process sessions
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
│   │   ├── download_dialog.py       # Separate bootstrap handoff
│   │   ├── theme.py                 # Precision dark/light QSS stylesheet engine
│   │   └── signals.py               # Centralized QtSignalBus for thread-safe event routing
│   │
│   └── core/                        # Core Foundations & System Services
│       ├── constants.py             # Global constants, regexes, baud rates & telemetry
│       ├── theme.py                 # Theme class, color tokens & styling engine
│       ├── config.py                # Settings persistence, multi-instance PID locks
│       ├── file_utils.py            # Windows attributes (attrib +h), UNC paths, robust file I/O
│       ├── toolchain.py             # Stable host-selecting API
│       ├── build_resources.py       # Shared CPU/RAM/storage budgets
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
- **PlatformIO targets**: Validate the exact board and declared framework through `target_profile.py`; never silently substitute a family default. `.ino` projects require Arduino. Runtime refresh reads only installed/cache definitions. Firmware cache fingerprints include host, board/framework/platform, manifests and sources.
- **Bootstrap-Only Board Toolchain Preparation**: Prepare configured board/framework/tool/library packs before the workspace opens. Compile, Upload and Reset never call installers; missing packages require a separate bootstrap repair.
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
- Windows release-first preparation uses the pinned archive size/SHA256 and `bootstrap_seed.py` to inspect genuine package metadata and usable manifests. Import only absent compatible package groups; preserve existing destinations, archives and failed checkpoints. Reinspect after importing platforms, then resolve missing packages normally before readiness. Do not fabricate `.piopm`, overlay installed directories, auto-prune incompatible SCons, or treat the release as a complete plan certificate. Native Ubuntu never imports the Windows seed. Verify with isolated `direct/verify_bootstrap_seed.py` fixtures.
- Read update versions from the same explicit interpreter used by pip, with fresh distribution metadata and PEP440 ordering. Reject conflicting metadata, verify actual installed versions after upgrades and keep the base runtime separate from the application venv. Never seed or sync individual missing package/metadata trees in either direction; use target pip for missing dependencies. An incomplete check must not report all utilities current. Use `direct/verify_bootstrap_updates.py` without installing packages.
- `main/qt/log_follow.py` preserves log anchors, selection, focus and horizontal position around output mutations. Workspace logs follow only when enabled, at the bottom and without a held/dragged scrollbar; wheel/key/manual scrolling resumes at the real bottom. Bootstrap opts into `resume_on_release`: holding or dragging freezes its view, release resumes following immediately even above the bottom, and checking Auto ON resumes unless a hold is still active. Apply the final release after the scrollbar's event handler and recheck the checkbox and held state. Auto OFF prevents forced following in every view. Bounded history may evict an anchor and clamp it. Keep assistant HTML fallback reading interaction equivalent to workspace logs and preserve native xterm following. Verify bootstrap with actual Qt scrollbar press/drag/release in `direct/verify_controls.py`.
- `bootstrap_native.py` provides immediate standard-library Tk feedback while first-run Python dependencies install. After successful fresh target-interpreter verification, promote only the display to the original lazily imported `bootstrap_qt.py` view before bulk board-tool preparation. Preserve the same setup worker, installer results, durable log and elapsed start time; never restart setup or install Qt into the base runtime again. `bootstrap_dispatch.py` owns one bounded queue and main-thread sink: every remaining event in a drained batch must reach the new sink. Acknowledge the worker only after Qt's event loop starts, defer the switch while a scrollbar is held, and retain functional Tk on a promotion error. Transfer tagged bounded displayed text, committed/live tables, failed-step state, selection, scroll anchor, Auto-Scroll and Skip Updates preferences. Prefer env Qt before initial imports, retry imports even if the env path is already present, and never unload a foreign loaded Qt/shiboken native graph. Fit Tk native pixels separately from Qt logical geometry. Verify cold construction with `direct/verify_bootstrap_native.py` and promotion/rollback/held/OFF/queue/close interactions with `direct/verify_bootstrap_promotion.py` in isolated fixtures; mock setup, hardware and persistence. Cold absence proof must not borrow Qt; promotion proof may use only the fixture's prepared environment after that boundary.
- Release-seed publication in `bootstrap_seed.py` retries only Windows errors 5/32/33, with at most eight rename attempts and six seconds of total backoff. Recheck `lexists` before each attempt and after failures so occupied and dangling junction destinations are never replaced. Never alter ACLs, remove staging or retry unrelated/POSIX errors. Preserve the original exhaustion error, ZIP and staged payload. `direct/verify_bootstrap_seed.py` covers real native held-file publication, persistent failure retention, competing destinations and a real dangling Windows junction in isolated retained fixtures.
- A failed bootstrap step recolors its whole retained output, including subsections and later progress; reset only at the next step. Plain stdout cannot retroactively recolor past output. `direct/verify_bootstrap_download_failure.py` uses real loopback HTTP 503 with the actual downloader and failure callback, but requires the seed explicitly in its fixture and runs no fallback installers. Preserve its partial checkpoint and capture/log evidence; do not call it a complete production setup run.
- Setup uses a resizable PySide6 glass dialog after Python dependency verification. Keep immediate Tk feedback usable before Qt is installed. Share theme tokens for status and live progress; persistent QTextCursor anchors must survive log trimming. Closing/hiding must not be undone by the topmost timer. Never unload Qt/shiboken extensions from a foreign or failed partial native import while promoting the view.
- Setup summary stages use larger bold headers, a blank line before each new group, and two-space result indentation. Subsections remain smaller bold headings. Preserve the same displayed summary text in Tk and Qt and restore header type even when the failed-step tag overrides its color. Raw installer details and presenter events remain unchanged. The editor and all tool tabs fill the central workspace without outer margins or pane borders; preserve controls' inner padding, text reading padding, tab focus and splitter handles.
- Setup defaults to a clean activity summary with an explicit Technical details disclosure. `bootstrap_presentation.py` is toolkit-independent presentation only: keep original events flowing to raw details and the durable run log, filter resolver/path chatter, deduplicate success only within a top-level stage and retain warnings, errors and useful failure context. Bound summary history to 700 events/128,000 characters and package rows to 96. Use compact native/Qt progress components rather than drawing text bars in the summary; collapse completed groups, keep real phase percentages and use an em dash when a percentage is unknown. Shorten visible statuses without changing pipeline state. Transfer presenter state and both views' text, selections, scroll anchors and disclosure through promotion; never reconstruct summaries by replaying installer work. Failed-step coloring resets only at the next top-level stage. Glass reflections, rims and chip branding use cached static painting in every palette, with no perpetual spinner or blur effects. The native title-bar mark loads the app-owned PNG through stdlib Tk; exported SVG/PNG/ICO assets must match the shared brand geometry.
- Qt embeds Python support modules with relative `shibokensupport/*.py` labels. They are virtual ZIP entries, not paths relative to the sketch or process working directory. Exempt only recognized support aliases with the exact `signature_bootstrap.EmbeddableZipImporter`, absent spec origin and verified absolute PySide6/shiboken6 package anchors in the target environment. Continue rejecting foreign package/native origins and arbitrary relative origins; never unload loaded native modules. Verify QtWidgets preimport during warm construction and promotion after a recoverable view error.
- `bootstrap_output.py` reads available pipe chunks and parses newline-free percentages into one live row per download/unpack phase. Route Tool/Platform/Library Manager installation confirmations through `log_ok`, warnings through `log_warn` and final errors through `log_fail`. Mark failed phases interrupted at the last reported percentage; only reported completion or a verified installation completes unpacking. Strip split ANSI controls, decode split UTF-8, coalesce queued progress and preserve bounded delivery plus full raw setup output.
- The offline output queue stays bounded without evicting warnings, errors or clear events. Coalesce progress and evict ordinary chatter first; apply backpressure only on the background setup worker when all queued events are critical. GUI flushes never wait, and shutdown releases waiting producers. Verify burst retention and shutdown with `direct/verify_offline.py`.
- Offline package preparation must preserve the short Windows core junction in all manager and child environments; do not call `resolve()` on that path. `bootstrap_platformio.py` scopes extended Windows destinations to PlatformIO's archive unpacker, retaining ZIP/TAR traversal/link checks and post-extraction verification. Use this guarded bootstrap entry point for builder subprocesses; leave native Linux paths and runtime download denial intact. Verify deep local archives, alias propagation, progress/retries and queue bounds with `direct/verify_offline.py`; never run live setup as a verifier.
- PlatformIO's Atmel SAM/STM32/nRF52 definitions intentionally leave the Unix-only Zephyr `platformio/tool-gperf` optional on Windows. Omit it from Windows preparation only after confirming no board/framework configuration activates it; retain it on other hosts and when any configuration requires it. Preserve every other optional tool and package variant. Never suppress general missing-package or installation failures.
- Collect actual primary package owner/version identities from every board/framework configuration before resolving bare `optionalVersions`. Reassign an optional variant only when the same name and exact version has one observed primary owner; preserve all primary pairs, qualified/external declarations, ambiguous owners and unmatched alternate versions. Builder identities use only their selected primary required specifications. This supports ESP32's ESP-IDF RISC-V toolchain owner migration without fabricating registry requests or editing installed platform manifests. Verify owner migration, late configurations, ambiguity, explicit ownership and inactive alternatives with isolated `direct/verify_offline.py` fixtures.
- AVR/megaAVR Arduino host preparation may share a probe across MCUs only for the exact reviewed version and normalized source fingerprints in `offline_bootstrap.py`. Preserve core, framework script and full package specifications; unknown or changed builders remain MCU-specific. `bootstrap_builders.py` bounds reader queues/tails, streams records, emits elapsed heartbeats, preserves full diagnostic files under `logs/offline-builders-*` and kills child trees on a real timeout. Route SCons through the guarded bootstrap script and explicitly capture Zephyr module child streams; omit envdump dictionaries before logging. Scope Windows `core.longpaths` to bootstrap child environments and preserve inherited Git config entries. Verify with `direct/verify_bootstrap_builders.py`, `direct/verify_builder_output.py`, `direct/verify_bootstrap_processes.py` and `direct/verify_offline.py` using isolated fixtures; no verifier runs actual setup/installers.
- Parallel preparation requires exact reviewed builder sources that cannot mutate shared framework/package state. Serialize unknown/native builders, including shared ESP-IDF virtual-environment preparation. Per-probe project, cache and temporary directories alone do not isolate installed framework writes. Keep compiler job budgets and probe concurrency separate; retain full per-builder logs and fail readiness on any worker error.
- Bound reviewed AVR Arduino probe concurrency by the shared CPU/RAM/storage budget; `--jobs` may reduce it and is capped at the safe budget. Each child receives `--jobs 1` and matching environment configuration so nested compiler jobs do not multiply the probe count. Preserve per-probe project/cache/TMP isolation and global unique log identifiers.
- Windows Zephyr CMake resolves some source paths through the package-store junction. `windows_tool_paths.py` canonicalizes only its `ZEPHYR_BASE` child environment so library names use the same base; leave package/compiler/project arguments short. Apply this in bootstrap Popen and offline subprocess audit handling, including Windows' serialized command-line audit events. Do not patch installed frameworks, bypass readiness or weaken download guards. Bootstrap Python child output uses UTF-8 consistently.
- ESP-IDF's component builder resolves its components base but may retain the source's short junction spelling during `relpath`; this can escape both application and bootloader build folders and collide in the package tree. `windows_tool_paths.py` adapts only absolute Windows paths rooted in `framework-espidf` (including versioned `@` folders) components directories, after confirming canonical source containment. Compute relative suffixes from both canonical paths while preserving short framework/tool/build arguments, SCons conflict detection and all offline guards. Bootstrap uses a reversible context; offline activation installs the adapter once for guarded children. Verify real isolated junction collisions, distinct corrected objects, containment and unrelated/Linux passthrough in `direct/verify_bootstrap_processes.py`; never patch installed frameworks.
- Mbed 6.17 needs `setuptools==80.9.0` for `distutils.spawn`/`LooseVersion` and `future==1.0.0` to avoid removed `imp.reload`. Both host setup paths install and validate these providers normally. `mbed_compat.py` preloads their packages only when importing the installed Mbed adapter inside the selected package store, before its bundled future/past can shadow them. Validate pinned distribution/module origins and APIs; reject stale already-loaded providers without evicting them. Bootstrap scopes the lazy finder reversibly; guarded runtime installs it idempotently and never installs dependencies. Warm health checks fingerprint required provider files and selector sources. Verify synthetic bundled-provider precedence, missing/version/origin failures, package containment and hook/guard preservation with `direct/verify_mbed_compat.py`; no live builders or package edits in verification.
- Reviewed STM32 20.0.0 Arduino preparation groups boards only by exact core and full package specifications after matching platform, framework and nested variant script fingerprints. Its Arduino checks reduce from 163 to 7; retain every other framework's probes and separate checks for unknown or modified scripts. Replan after package preparation so missing scripts cannot grant sharing. Verify isolated signatures and source edits with `direct/verify_bootstrap_builders.py`.
- Scope extended paths to PlatformIO's installer-module copying and cleanup only after canonical containment within the configured package/platform/library store or its `pkg-installing` staging directories. Preserve literal trailing-dot TAR entries; leave global filesystem modules and unrelated/native Linux calls untouched. Verify local TAR entries, deep ZIP/TAR paths, overwrite/version detachment, traversal rejection, junction escapes and hook restoration with `direct/verify_bootstrap_archives.py`; no live install/cache writes in verification.
- The downloader (`arduino_lib_req.py`) retains Tk/ttk network and archive workflows. `tk_glass.py` supplies static cards; advanced URLs live behind **Board indexes**. Preserve search, installed-item details, version selection, checksum validation and cancellation. Reopening refreshes the per-user theme without rebuilding widgets or losing state.
- `browser_loading.py` validates compact catalogs against source path/mtime/size and schema, rebuilding damaged or stale derived caches. Cache writes are atomic and bounded. Keep large raw JSON out of the RAM cache, publish cached data before HTTP refresh, and use two network workers on constrained hosts (four otherwise). `TkTasks` is the Tk-thread delivery boundary; workers never call Tcl. Installed inventories and detail selections use one active scan plus one latest request; cancel stale scans and stop dispatcher timers when idle. Verify with `direct/verify_browser_loading.py` using temporary catalogs and mocked HTTP.
- Settings' legacy `graphics_acceleration` key controls continuous splitter resize, not the GPU. Unsaved defaults use divider previews on constrained devices; preserve explicit choices. Save a complete preference set once and report persistence failure. Reset controls must revalidate target/framework/port and busy state in their callbacks.
- `main/qt/responsive.py` owns current-monitor work-area fitting and coalesced screen/font signals. Qt screen geometry and font widths are logical pixels: never multiply them by DPI/DPR. Use `ui_metrics.py` for coordinate arithmetic, including negative monitor origins and frame clearance. Reflow Controls, serial headers and dialog fields; do not let wide size hints lock compact windows. Preserve saved editor/terminal fonts. Tk uses its own native point/pixel scale, coalesced panes and scrollable installed details; cancel bound callbacks on destruction.
- Main windows have a minimum width of half the current monitor's available width in Qt logical pixels. Allow wider resizing and normal maximization, including saved maximized state. Recalculate the minimum on monitor changes and fit normal window frames to the work area. Detached editor and explicit dialog sizing keep their own contracts.
- Verify screen changes and 100/125/150/175/200% scale in fresh processes with `direct/verify_responsive.py`. Couple physical screen dimensions with each scale and taskbar/work-area reservation; convert once to Qt logical pixels. Include half-monitor and full-width windows, portrait and short laptop areas, and chosen content fonts. Assert effective rendered fonts, complete button visibility through ancestors and a complete padded text line. Keep the same hardware-free persistence mocks as the controls verifier. Check full baud digits beside combo padding/arrows and footer visibility. Narrow, short Controls use fewer rows; very short windows retain Timestamps/Skip Compile in Options. Serial Options retains collapsed display/Copy/Clear actions, and all layouts restore when space returns. Reserve Build/Serial output using the document's chosen font, reading margin and scrollbar/frame chrome, and refit after font changes.
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

Preserve Bootstrap's chip-branded glass header, concise wrapped status, clean activity view, expandable raw details, compact package progress and preference rows. Start at 70% of the active monitor's full Qt logical screen geometry, with preferred 520×420 floors and shared native frame fitting to the work area; preserve resizing. Reduce card chrome and active package rows on short windows; use a package count with retained technical details when rows would consume reading space. Reserve at least two text lines, text padding and the raw horizontal scrollbar without shrinking content fonts. Verify populated clean and details fixtures in all three palettes. Tk uses native metrics separately; retain card Configure callbacks with add='+' when adding wrap/budget callbacks, and check wrapped footer bodies remain inside their cards.

Actions must wait for Monaco's WebChannel save acknowledgement, not the immediate `runJavaScript` Promise result. Reload must use the editor's reload/dirty-state routine. Reset workers reserve busy state before dispatch and capture the confirmed target. Prepare and validate ESP32 recovery images before erase; restore only bootloader/partition/boot_app0 for Hard Reset. Keep write failure terminal. Run `direct/verify_actions.py` for isolated lifecycle, recovery ordering and Clean preservation checks. Clean must preserve settings, user source directories and AI edit journals, and report deletion failures.

Use the app's private runtime. On Windows:

```powershell
& src/_python/python.exe -B direct/verify_runtime.py --render-dir temp/audit/workspace
& src/_python/python.exe -B direct/verify_runtime.py --preview-cpus 4
& src/_python/python.exe -B direct/verify_terminal.py
& src/_python/python.exe -B direct/verify_performance.py
& src/_python/python.exe -B direct/verify_platforms.py
& src/_python/python.exe -B direct/verify_offline.py
& src/_python/python.exe -B direct/verify_bootstrap_seed.py
& src/_python/python.exe -B direct/verify_bootstrap_updates.py
& src/_python/python.exe -B direct/verify_bootstrap_download_failure.py
& src/_python/python.exe -B direct/verify_bootstrap_native.py --report-dir temp/audit/bootstrap-cold
& src/_python/python.exe -B direct/verify_bootstrap_promotion.py --report-dir temp/audit/bootstrap-promotion
& src/_python/python.exe -B direct/verify_bootstrap_pip_paths.py
& src/_python/python.exe -B direct/verify_controls.py
& src/_python/python.exe -B direct/verify_browser_loading.py
& src/_python/python.exe -B direct/verify_projects.py --render-dir temp/audit/projects
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
