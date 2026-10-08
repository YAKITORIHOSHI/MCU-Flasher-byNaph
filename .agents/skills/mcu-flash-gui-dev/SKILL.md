---
name: mcu-flash-gui-dev
description: "Developer and troubleshooting guide for MCU Flasher on Windows and Ubuntu. Use for the PySide6 glass workspace, offline Monaco editor, coding CLI terminals, serial monitor, exact board/framework pipelines, bounded recovery, resource budgets and native launchers."
---

# MCU Flash GUI Development & Maintenance Skill

Use this skill when developing, debugging, or extending the **MCU Flash GUI** desktop application (`mcu_flash_gui.py`, `main/mcu_flash_gui.py`, `main/qt/`, `main/web_bridge.py`) on Windows 10/11 and native Ubuntu. Ordinary firmware work still follows the live root-sketch boundaries in AGENTS.md.

For explicitly requested assistant delegation, use the local
[`agy-delegation`](../agy-delegation/SKILL.md) workflow for bounded investigation.

## Search, review and preparation modes

- Ctrl+Shift+F opens the owned modeless Find All dialog. Search only root editable
  sketch sources/text notes, overlay dirty Monaco snapshots, use literal case/word
  options and UTF-16 locations. Keep one background worker, discard stale queries,
  and bound bytes/files/results. Preserve the active buffer on result navigation.
- Keep C++ brace Enter rules in the lazy language configuration and respect each
  model's tab/space settings. Empty projects/new sketches use empty setup/loop;
  new .cpp/.c are empty, headers only `#pragma once`. Preserve explicit Blink.
- Wrap Modify Add/Rename/Delete in `AIEditWatcher.user_file_operation` before
  mutation. Invalidate in-flight scans, baseline success/failure and deleted paths,
  and refuse changes to a file with a pending review. Never classify these manual
  actions as AI. Watch root .txt notes and source files, excluding symlinks.
- AI Changes uses timestamped per-file cards grouped by known submitted input.
  Observe PTY input passively; capability replies/Unicode/paste must still pass
  unchanged. Unknown external prompts use labeled activity groups, never invented
  titles. Keep before/after previews, per-file Accept/Reject with navigation and
  counters, history bounds (200 cards / 4 MiB), truncated-preview notices and
  atomic replicas. Delete history never removes pending reviews or recovery copies.
- Offline mode defaults off. Online startup verifies the local runtime and SCons;
  missing exact targets use explicit board preparation. Both modes retain the
  bootstrap-only installer boundary. Offline enables network denial and full
  configured-plan readiness. Settings confirms disk/restart or removal of owned
  extras, rechecks busy/other-window state, saves acknowledged buffers, then a
  private helper waits for exit before native Bootstrap --repair. Report failure
  and roll back mode without dropping buffers. Cleanup authenticates per-host
  extras and rejects links/unknown content; preserve shared toolchains and sketches.
- An additional healthy same-installation window skips source-fingerprint
  rechecks while retaining runtime/mode/crash validation and immediate-child
  failure checks. Keep session/crash records and spawn logs per process; mark clean
  only after the event loop exits. Preserve live siblings through repair and
  record native/worker failures without replaying compilation or hardware writes.
- Reserve a fresh operation session before every Compile/Upload/Reset/Clean.
  Track its worker through teardown; refuse a new action while that worker lives.
  Deferred Stop captures its session, operation, worker and process, revalidates
  destructive phases, and cannot clear or terminate a later action. Preserve Stop
  during resolution. On Windows terminate the original Popen handle and checked
  descendants, never an exited process's PID. Verify real fixture children and
  delayed callbacks with `direct/verify_operation_lifecycle.py`.
- Install `GuiGarbageCollector` before backend/window construction. Disable
  automatic cyclic GC for the workspace lifetime; collect only on the GUI thread
  between events, outside modal/nested loops, at bounded intervals. Ordinary
  refcount cleanup remains immediate. Stop its timer at shutdown; do not collect
  from eventFilter or backend workers. Verify with `direct/verify_gui_lifetime.py`
  and the real runtime renderer fixture.
- Build and Serial Auto-scroll follow whenever enabled except during a mouse
  hold; resume on release, including outside the view/lost grab. Auto OFF retains
  the reading anchor. Omit only the routine unexpanded ESP32 objcopy recipe and
  quiet build notice, preserving diagnostics and cancellation polling.
- Reviewed AVR and exact ESP8266 Arduino checks share one bounded worker pool
  after installers complete. Source/package signatures must match; unknown/native
  builders remain serial. Failure cancels siblings and leaves readiness uncertified.
- Verify with `direct/verify_project_search.py`, `direct/verify_ai_changes.py`,
  `direct/verify_offline_mode.py`, `direct/verify_sessions.py` and the existing
  runtime/terminal/responsive/performance checks. Mock all live persistence,
  hardware and installers; captures belong in temp/. Native Ubuntu and physical
  compile/upload remain separate verification requirements.

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
     - `console_panel.py`: Original build journal with bounded streaming, retained-log copy and autoscroll.
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
     - `package_progress.py`: Window-owned, focus-preserving package job card with stage/progress display and a Notifications action.
     - `package_coverage.py`: Explicit asynchronous viewer for every board result with a virtual table, search/status filters and full reasons.
     - `theme.py`: QSS generator for Glass Smoked Dark, Glass Frosted Light and Solarized Dark.

   **Theme and log readability**
   - Build and serial output, ANSI foregrounds, connection state and transient status-bar messages use semantic colors with readable contrast against the active theme. Recolor retained output after theme changes instead of leaving stale inline colors.
   - Check rendered compatibility logs, syntax severity/status brushes and notification cards against their actual reading surfaces. Share terminal ANSI colors between Windows and POSIX and enforce xterm minimumContrastRatio=4.5 for truecolor/indexed CLI text. Verify with `direct/verify_theme_readability.py` and `direct/verify_panel_readability.py`.
   - The workspace owns one theme signal connection and one propagation pass. Hosted panels opt out of direct theme subscriptions; standalone panels retain them. Status timers are window-owned, restart for the latest message and never clear a newer operation status. Failed notification clears preserve history; filters use bounded retained records and apply to live events. Refresh external history off the GUI thread on panel reveal, discard stale project results and preserve unpersisted live events. Report failed history writes without recursively persisting the warning. Notification replacement must be atomic; verify failure preservation with the isolated notification persistence/writes verifiers.
   - Keep Controls checkboxes at intrinsic width so Timestamps and Skip Compile remain a compact pair at wide sizes; let responsive reflow handle constrained widths.
   - Keep keyboard focus on checkboxes visually confined to the indicator; do not draw a focus border around the full checkbox label.
   - Anchor the Workspace heading to the actions column, above Detach Editor or the compact Options button. Put spare width between the checkbox pair and actions; check heading alignment and complete action visibility through wide/compact transitions with `direct/verify_responsive.py`.
   - Keep palette and semantic contrast helpers in `main/core/theme.py` and `main/core/log_colors.py`. PyQt5/QScintilla sample processes must never import `main.qt`, whose signal bus loads PySide6. The read-only sample viewer inherits the saved content font and theme, colors every lexer style and fits the complete native frame in logical work-area units. Probe only prepared host interpreters off Tk; preserve Ubuntu venv symlink spelling. Bound requests and reject already-queued superseded completions. Missing dependencies require Bootstrap; never install on a View Source click. Verify real sample copy/navigation, themes and scaling with `direct/verify_library_samples.py` in its own process.
   - Balance Tk worker slots when thread construction/start fails, report failure through queued callbacks and release scan busy state without replay. Wire Catalog, Download and Update failure callbacks to their existing UI reset paths. A subsequent explicit click can retry. Verify resource failures and archive preservation with `direct/verify_browser_loading.py`.
   - Setup derives readable text inks with `src/modules/ui_palette.py` against solid native reading surfaces or static Qt glass/reflection bounds. Include selected log text and failed Solarized stages; preserve semantic hues and the shared palette. Keep this helper free of Qt imports so native fallback can paint before dependencies are available. Verify actual character formats and composited backgrounds with `direct/verify_bootstrap_headers.py`.

   **Startup readiness contract**
   - Construct the main workspace only after project selection. Do not prewarm hidden WebEngine views while the picker is open. Show the window before scheduling idempotent service startup; the editor loader handles its own readiness.
   - Keep hardware enumeration, root-source scans, parsing and metadata reconciliation off the GUI thread. Use cached ports in controls and populate them through the monitor worker. Defer catalog discovery until initial painting settles; never scan USB package trees at module import.
   - Windows warm launch uses a per-user health snapshot validated against installation path, interpreter/version, startup source fingerprints and required runtime paths. Missing/stale health, explicit repair and recorded crashes use setup. Check immediate child failure before accepting the fast path; never replay hardware writes or CLI commands.
   - Slow storage is independent of CPU speed. `main/core/storage_resources.py` queues read-only host volume/sysfs hints in one bounded worker with TTL caches; default callers never wait. Build/Bootstrap workers may wait at most 350 ms total and must pass the current project/tool-store paths. Known HDD/USB-removable paths cap jobs at two, network paths at one; unknown storage retains the CPU/RAM policy. Do not benchmark-write, relocate user data, or run WMI/shell probes during UI construction. Verify with `direct/verify_storage_resources.py` and `direct/verify_storage_io.py` using isolated fixtures.
   - Startup of an already-attached MCU is passive: never set manual reset pending or pulse DTR/RTS on initial connection. Reset only from explicit reset controls or the upload flow.

   **Board selection and search contract**
   - Enable Compile when a board is selected. Serial Upload needs a selected port; a verified board's declared native USB/programmer interface can upload without COM. Unknown transports require a port. Describe the native interface in Upload's tooltip. Refresh directly from selection signals and operation completion; keyboard shortcuts recheck the same rules after asynchronous save. Keep exact target/framework validation in the backend pipeline.
   - Compile/Upload must repair unresolved cached Arduino rows in their request worker before target validation. Match installed definitions from the same app-owned store used for builds; disregard an unrelated inherited core directory. Normalize the Arduino build-define prefix and only the manifest's declared vendor prefix when comparing names. Preserve ambiguity rejection and framework constraints. Runtime never queries online registries; missing definitions require bootstrap preparation. Refresh must replace stale empty-ID rows under their existing display names, not retain them or add duplicate aliases.
   - Diagnose identity ambiguity separately from missing packages using the same matching scores and margin. Retain bounded competing definitions in published/cache rows; Compile/Upload must name the exact-model choice and must not prescribe repair as a way to choose between boards. Generic ESP32S3/C3 Arduino declarations are not automatically equivalent to a concrete DevKit or its memory configuration. Read every build define in compound and nested flags, both Arduino USB-property layouts, and exclude hidden discovery-only declarations. Cache schema 6 invalidates older parsed identities. Verify all parser cases with `direct/verify_board_declarations.py` and actual Compile/Upload ambiguity with `direct/verify_target_resolution.py`.
   - Board picking uses the published catalog without starting a registry refresh on every open. Refresh boards is explicit. Build the immutable metadata index and search it in one background worker with one latest pending request, queued Qt completion and stale-generation rejection. Debounce typing briefly; Enter waits for the current query. Virtualize rows with QListView, bound index/query caches, preserve category filters, recents and keyboard navigation, and stop/disconnect closed pickers. The footer contains only the count, Cancel and Select Board; do not add a Framework label or dropdown. On confirmation preserve a valid saved framework, otherwise choose Arduino or the sole available native framework. Exclude unavailable frameworks and leave multiple native choices unresolved without an explicit saved `board_frameworks` preference.
   - During local discovery, compute Arduino/PlatformIO identity, token and define evidence once per refresh and reuse only that pass's candidate string-matching state. Keep framework/MCU exclusions and deterministic tie ordering. For generic ESP32S3/C3 Arduino identities, strong variant/build evidence may select a distinct top PlatformIO candidate even with a small score lead; exact ties stay ambiguous. Other/name-only matches need the normal score margin. Rebuild evidence after metadata changes; never persist or share mutable matchers between refresh workers. Verify batch identity changes and ambiguity with `direct/verify_target_resolution.py`.
   - Publish incremental preview batches before expensive matching, but let only the completed catalog become authoritative. Enumerate manifest directories once per refresh, reuse bounded parsed records only after current-file validation, and make equal catalogs/revisions no-ops. Explicit user Refresh sets `invalidate_parsed=True` to reread current bytes even when metadata is reused; startup/automatic refresh stays warm. Use real stat identity when Windows DirEntry omits file IDs. Keep aliases, stale readers and concurrent cache publication safe; firmware checks still re-read source/manifest bytes. Verify with `direct/verify_catalog_io.py`.
   - Verify board gating with `direct/verify_controls.py` and search/ranking, responsiveness and lifecycle with hardware-free `direct/verify_board_search.py`. Its optional benchmark compares this checkout's HEAD search class using isolated fixtures; captures/reports go in `temp/`.
   - `direct/verify_target_resolution.py` verifies stale catalog repair through the actual Compile/Upload entry points. On Windows its optional `--compile-installed-esp32` copies installed packages into `temp/` and checks the real Compile button, Arduino conversion and firmware build. Mock persistence/hardware, keep all build outputs in the fixture, and never upload or run setup against the live store. Allow about 1.2 GB scratch space for the isolated package copies.
   - Preserve manifest `upload.protocol`, `upload.require_upload_port` and `upload.speed` through discovery, resolution and refresh. Use bootloader defaults for non-ESP targets; show their speed or Auto with the control disabled. Re-read transport metadata after bootstrap preparation and never pass a serial argument to a native programmer. Uninstalled/unverified transports use the generic PIO worker; only installed Arduino serial targets use optimized family paths. Abort serial uploads if the port changes during compilation. ESP connection retries are allowed only before erase/write starts; after a flash write begins, never replay it or pulse reset on failure. Keep the partial-write diagnostic visible and let the user retry after stabilizing the board/port.
   - All downloads/installations belong to bootstrap. `offline_bootstrap.py` prepares `direct/offline-packages.json`: all configured platforms, every declared board/framework package variant (including optional uploader/debug tools), builder preparation and configured sketch libraries. Verify transitive package dependencies, respecting framework-bundled libraries; missing dependencies must not become a success certificate after a PlatformIO warning. Both host setup paths run this before launch. Record readiness only after complete success; validate host, architecture, plan and local package files. Custom libraries/platforms belong in the plan. Never fabricate PlatformIO package metadata or defer installation to Compile/Upload/Reset.
   - The default plan prepares AVR, ESP32 and ESP8266 with Arduino. NodeMCU 0.9 and 1.0 must resolve independently to their exact definitions; downloaded Arduino archives alone never prove PlatformIO readiness. An explicit nonempty `frameworks` list selects declared frameworks for preparation; omission preserves complete framework coverage for configured platforms. Certify only the actual plan and explain that other installed targets may need an expanded plan. Preserve optional uploader/debug tools and unmatched package variants; exclude framework-specific packages only with manifest/configuration evidence. Never reduce coverage silently to shorten setup.
   - Before publishing Bootstrap success, audit every visible Arduino declaration from discovered/requested source roots in the separate preparation worker. Bind Ready to current parsed source bytes, original board-manifest receipts and the actual prepared plan, or an existing verified Arduino CLI certificate. Persist the complete `.mcu-bootstrap-board-coverage.json` atomically in the host package store and require it in the readiness certificate. Log ready/ambiguous/unavailable/outside-plan totals without claiming all Arduino names are prepared. Keep source scans and byte verification off startup/GUI threads; inject isolated source roots for `direct/verify_bootstrap_board_coverage.py` and offline preparation fixtures.
   - PlatformIO is the normal compiler. Generic Arduino selections may have a verified source-bound CLI certificate, but that certificate must never override a genuinely tied PlatformIO model match. For generic ESP32S3/C3 identities, allow strong variant/build evidence to choose a distinct top PIO match even when the score lead is small; exact ties remain ambiguous and must clear CLI routing. Use Arduino CLI only as a verified fallback when PlatformIO has no exact compatible target; never switch compilers after a PIO build failure. Keep exact source receipts/core/tool checks for any retained CLI fallback. Never infer metadata from folder names or silently replace a physical model. Verify with `direct/verify_bootstrap_arduino_sources.py`, `direct/verify_arduino_source_targets.py` and `direct/verify_target_resolution.py`. On Windows, keep the short app path for launch/config placement but write canonical physical paths for Arduino CLI's `data`, `downloads` and `user` directories so its hardware scanner can read packages through the junction alias. Ubuntu requires native Arduino CLI on PATH; report missing tools truthfully.
   - Preparation certificates record board-definition byte hashes. Verify selected definitions in the separate preparation worker before reusing a certificate or reporting Ready; older certificates or changed bytes require explicit preparation. Keep this byte validation off the GUI/startup thread, preserve short package-store aliases and never weaken Compile/Upload source validation.
   - Isolate primary Arduino stores by exact core/version/index identity; preparing another source version must not replace a ready target. Search all existing verified receipts before adopting legacy folders and accept receipt identity after folder renames. Carry source-byte-bound preparation failure reasons into full coverage, revoke previous success before a mutating coverage refresh, and synchronize final downloader associations with aggregate coverage/readiness.
   - Preserve an established original Arduino namespace across missing/corrupt certificates, failed repair and declaration changes. Retain unverified preparation intent independently from Ready certificates, require current byte proofs to compile, and allow a backend change only through an explicit verified PlatformIO association or a current unique compatible PlatformIO manifest match. Keep every equal-name source/version visible under stable distinct labels. Verify with `direct/verify_arduino_source_namespace.py`.
   - Arduino CLI builds consume both downloader libraries and the app-owned PlatformIO library collection using repeated collection flags. Keep input discovery and byte checks in build workers. Bind reused firmware to the current library search roots, library metadata, used source trees and compiler dependency bytes; force a clean compile when inputs change. Include the Arduino input helper in launch guards and warm health fingerprints. Verify with `direct/verify_arduino_library_inputs.py`, including coarse-timestamp mutations and the upload write boundary.
   - Explicit board downloads hand off to `src/modules/board_preparation.py` in a separate private-runtime process after verified extraction. Retain default/previous coverage and pins. Arbitrary user-added indexes use exact installed/registered ID, name and hardware evidence with ambiguity rejection. Query a complete fresh registry in the private preparation child; the standard boards command can hide network failures and cannot establish absence. Optional user platform/version/HTTPS and board ID associations belong to requested index/package/architecture; remote documents cannot override them. Record actual source-to-installed-platform bindings, manifest hashes and exact requested builder receipts before Ready. Resolve every downloaded/index-listed board, retain failure reasons and write complete coverage under `logs/package-jobs/reports/`; partial packages still refresh ready definitions. File handoffs avoid command-line limits; bounded snapshots never truncate full reports.
   - Arduino CLI fallback is permitted only after a successful complete registry absence check for the exact board, bound to its downloaded source bytes. Failed/ambiguous/conflicting discovery, unsupported frameworks and ordinary PlatformIO compile/upload failures never authorize fallback. `arduino_cli_support.py` prepares the exact core/version/FQBN in the exclusive online worker and certifies local core/tools. Independent fallback compile probes run concurrently within the shared CPU, RAM and storage worker budget; compiler jobs are divided across concurrent targets to avoid oversubscription. Parse supported `core list` JSON layouts (`installed_version`, legacy version fields, or one explicitly installed release) while requiring the exact installed version; never interpret a boolean `installed` flag as a version. `arduino_backend.py` uses root sources and prepared inputs offline, rechecks receipts/current source/firmware before one upload attempt, and announces why the backend changed. A newly installed exact or ambiguous PlatformIO identity invalidates fallback. Keep source-bound alias proofs in app-owned package storage, revalidate in workers and show backend plus exact target in coverage. Verify with `direct/verify_board_index_targets.py` and `direct/verify_arduino_fallback.py`, using mocked registry/install/hardware calls only.
   - Hash the exact raw Arduino declaration bytes parsed into records, retain that receipt through catalog/cache/worker publication, and refuse missing or stale parsed receipts when applying prepared targets. Publication must compare current source and manifest bytes with their original verified receipts and downgrade changed rows before counting Ready. CLI builds bind staged copies to the captured root-source hash, then recheck Stop, board and port after expensive final preflight. Cache schema 6 rebuilds earlier Arduino identities. Startup health fingerprints include the CLI/backend/index/lock helpers. Verify source mutation, cache migration and malformed metadata with isolated catalog/index/fallback fixtures.
   - Native Windows PlatformIO processes and in-process setup managers use `platformio_locks.package_locks()` for the configured app-owned core. Open with O_RDWR|O_CREAT without truncating; release the byte lock while retaining a stable path, including finalizers, so parallel builders do not race deletion. Retry only native nonblocking EACCES contention; propagate unexpected lock errors immediately after closing the handle. Do not delete a held lock, alter ACLs or turn genuine access failures into retries/success. Keep Ubuntu native behavior and physical containment through aliases. Verify hidden/read-only files and real concurrent children in `direct/verify_platformio_locks.py`.
   - Windows setup ignores inherited PlatformIO cores outside this installation, matching workspace ownership; never create or modify a foreign inherited store. Ubuntu CLI fallback requires native Arduino CLI on PATH, whereas Windows Bootstrap prepares its executable; missing-tool guidance must reflect the host rather than claiming Ubuntu repair installs it.
   - `src/modules/package_jobs.py` owns cross-process package-store coordination. Build/upload/reset workers hold shared use leases; background preparation reserves an exclusive waiting writer before mutating packages, blocking later readers while active users finish. Windows host repair guards initial store/configuration work and its complete seed/SCons/board-folder/offline preparation stage through `_bootstrap_tool_store_lease`; Ubuntu guards the native offline preparation child. Release host repair leases on failure/return and before workspace launch. Python environment repair remains a separate setup concern. Queue and disk failures must terminate truthfully, cancellation releases reservations, and dead-worker leases are reclaimable. Keep waiting/acquisition and job I/O off the GUI thread, preserve short Windows core aliases, and never replay a hardware operation after lease failure.
   - Publish atomic bounded live job snapshots with job identity, monotonic sequence and retained stage transitions. Coalesce percentages without losing diagnostics or final outcomes; independent workspace readers reject stale events and report interrupted processes once. Retain the newest 48 completed/interrupted jobs and their associated request/report files; active jobs are never pruned. `main/core/package_activity.py` forwards progress through queued Qt signals. The `PackageProgressCard` stays parented inside the main window's bottom-right corner, preserves typing focus and theme changes, and hides independently of the job. Notifications retain stage/final history, while completed ready subsets trigger local catalog refresh. **View all board results** opens `PackageCoverageDialog` only on an explicit details action; load reports and filter immutable rows off Qt, reject stale/closed completions and validate report identity/containment/limits. Preserve full reason text and report every included board. Verification must route events/leases through `MCU_PACKAGE_EVENTS_ROOT` or explicit fixture roots under `temp/`, never live logs or caches.
   - `offline_runtime.py` and `offline_platformio.py` keep package installation in Bootstrap in both modes, including nested Python builders. Offline additionally denies external network traffic while allowing loopback terminals and installed/local symlink packages. Bootstrap installs a conditional `.pth` guard; the root comes from the launch environment so moves stay supported. Main entry points check mode-specific readiness before opening the workspace. Setup and user-terminal handoffs remove inherited runtime/no-index variables. Runtime board refresh reads local manifests/catalog snapshots; the Bootstrap toolbar action opens a separate setup process and rechecks busy state. Keep Monaco/xterm/assistant assets local and remove pip/CDN fallbacks.
   - The real project build verifies source compatibility with the declared framework. A single declared native framework can be selected automatically; multiple native frameworks stay explicit. Require Arduino for .ino sources. Cache identity reflects current framework/definition on every call, across windows; recognize BIN/HEX/UF2/ELF outputs. Use `direct/verify_offline.py` for isolated package planning, startup certificates, setup handoffs, remote-network denial/local sockets and actual PlatformIO missing-package rejection. Mock all bootstrap installers/builders; never run live setup during verification.
   - Use `direct/verify_board_families.py` for isolated multi-family framework/transport checks and native Upload-button clicks. Optional `direct/verify_target_resolution.py --compile-installed-avr` builds Uno, Nano ATmega328 and Mega 2560 through the actual Compile button with copied installed host-native packages. Ubuntu accepts an explicit read-only `--avr-core` store and refuses copied Windows stores. No verifier runs installs, uploads, or live persistence. The Ubuntu CI integration provisions native AVR packages separately in `temp/` before running this probe.
   - `direct/verify_platforms.py` checks host/import order, native environment replacement, guarded runtime discovery, mocked venv setup/argument forwarding, POSIX upload sessions and Bash/Windows Script Host syntax. Syntax checks never execute installers or launchers; Windows Script Host skips on Linux. Report native Ubuntu CI results separately from Windows simulations.

   **Project selection and cancellation contract**
   - Windows foreground retries belong to the selector, stop on hide and skip a different active modal widget. Never re-show a dismissed native handle or focus over a project/dirty-buffer prompt; verify cancellation and nested prompts in `direct/verify_projects.py`.
   - Project selection uses cached static glass cards/header in every palette, four compact tabs, opaque reading surfaces, pinned actions and scrolling/reflow on short screens. Destination and dirty-buffer prompts retain native semantics over the same glass. Verify physical monitor-frame fit and padded text-line heights at 100–200%; preserve selection/input/focus through theme and sizing changes. No idle blur/animation loop.
   - Existing project Browse starts at `QStandardPaths.DocumentsLocation` on every click, with `Path.home()/Documents` only as an empty-location fallback. Do not reuse the active sketch, edited path or last native-dialog folder as its start. Preserve chosen-path previews and cancellation; the New Project location picker keeps its own behavior. Verify startup/workspace, redirected Documents and repeated clicks with isolated `direct/verify_projects.py` fixtures.
   - Right-clicking the project title or icon opens the native picker; Cancel must safely close the dialog and leave the editor and monitor responsive. Project selector cancellation must be idempotent.
   - Workspace Project / Ctrl+O asks whether each selected or created project should use the current window or an independent private-runtime process while idle. During any active operation (compile, upload, clean, reset, or another action), disable project-selector entry points and reject project creation, opening, switching or focusing in the backend, including requests targeting a new window. Re-enable the controls after the operation ends. When idle, opening in a new window preserves the current editor, terminal and target state. Switching the current window asks to save, discard or cancel when editor buffers are dirty; await the Monaco WebChannel save acknowledgement before switching. Startup selection opens the first project directly.
   - The picker's Open projects tab lists live registered windows with full folder paths. Reopening a sketch focuses its existing window. Windows uses native foreground activation; Ubuntu uses a same-user local Qt activation endpoint. Never duplicate ownership of one project or serial port.
   - `config_store.py` serializes preference transactions and merges changed snapshot keys across processes. Project/port claims must be atomic, handle failed persistence, and precede sketch writes or serial connection. A new project never overwrites an existing folder. Mock process spawning and metadata in verification.
   - Equal settings copies and generated metadata must not replace files or update timestamps. Preserve authoritative per-user save failures, portable-copy repair and fresh transactional merging. Last-used fonts/display checkboxes are shared; hardware ownership remains per-instance. Keep failed-save rollback visible. Preserve editor focus through theme/detach changes and recover only unexpected blank mouse cursors; never override ordinary resize, wait, link or text cursors.
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
   - Windows defaults to PowerShell for new sessions, initial reveal and visible project changes. The split `+` button opens PowerShell on its main click and keeps explicit PowerShell/CMD choices in its arrow menu. Hide the empty session tab bar so `+` starts at the header's left padding; restore intrinsic-width tabs when sessions exist. Resize through the workspace splitter, without a terminal Full/Restore toggle. Verify default/explicit shell dispatch and empty/populated/closed tab geometry with `direct/verify_responsive.py` using mocked terminal startup and controls.
   - Switching and spawning shells maintains individual session state, scrollback, and prompt rendering.
   - Clear removes display/history only; never type `Clear-Host` or `cls` into an active CLI. Kill terminates the selected PTY.
   - Runs as a child process to avoid UI thread contention.
   - Pass xterm capability replies, Unicode, alternate screens and bracketed paste intact. Acknowledge output after parsing; bound outstanding output and history. Preserve the user's CLI PATH and remove Python host environment overrides from shell environments.
   - Coalesce resize/font/theme controls in the Qt panel's bounded worker queue. Only explicit user actions create or restart shells; never replay commands after failure.
   - Build/serial displays bound both pending entries and characters before crossing Qt's event queue. Limit render batches and document/history sizes, including streams without newlines. Show truncation/backlog notices; Clear and ANSI clear must clear retained display history too.
   - Serial input uses `main/core/serial_stream.py` for incremental UTF-8, split CRLF and visible invalid-byte/control escapes. Never pass raw NUL to Qt/clipboard or replace invalid bytes silently. Deliver newline-free prompts in bounded coalesced chunks; finalize already read data and incomplete controls on disconnect/stop. Preserve passive DTR/RTS and never infer a baud from garbage or switch/reset automatically. Non-UTF-8 input gets one explanatory notification per connection, outside the device text. Use one bounded notice worker (one active and latest pending) so history storage cannot stop UART reception. Reader log locks cover memory only; Copy/Clear never wait on port opening/closing or notification writes. Rebuild bounded visual paragraphs before Qt insertion while retaining full bounded copy history. Clipboard retries reuse one snapshot, have a strict attempt limit and cannot overwrite a newer copy.
   - Serial Pause freezes painting while retaining bounded incoming history. Header Copy flushes one retained signal-bus snapshot and includes pending view history; selection copy preserves visible selection. Keep firmware-authored timestamp prefixes; add application timestamps once per logical line. Fragmented ANSI parsing must bound pending controls and expose incomplete tails on EOF/Copy; only explicit erase-screen commands clear history. Clear purges reader coalescing and both queues without resetting the byte decoder, connection or device. Reject late reader generations while preserving already accepted history. Verify with `direct/verify_serial_reader.py`, `direct/verify_serial_view.py`, `direct/verify_serial_delivery.py` and the existing runtime/performance verifiers using mocked persistence/hardware.
   - Preserve the original Build Console journal: section dividers, worker banner, each compiling filename and boxed timing breakdown. Do not replace compilation files with a count/latest-file summary or hide dividers behind another view. Use the same saved monospace point size for headings, frames and values to keep columns aligned. Header Copy includes retained events hidden by warning filters; selection copy uses visible selected text. Respect timestamps and move clear-on-action preferences into Options on narrow headers.
   - Build delivery queues evict ordinary chatter before diagnostics while keeping both character/entry bounds and visible omission notices. Coalesce only valid adjacent declared progress patterns, never compilation file events; preserve diagnostic barriers, cache semantic formats and update retained sizes incrementally. Use one bounded history, expiring FIFO. Drain PlatformIO build output and both optimized/native upload paths through a bounded reader queue so quiet dependency scans or bootloader handoffs can emit truthful silence updates and Stop can reach a cancellable process without waiting for another line. Native upload Stop stays available during scans and connection, then disables on the first erase/write indication; a failed write is never replayed or followed by an automatic reset. Share the upload scan-noise classifier across Windows and Ubuntu while preserving diagnostics, programmer output and PlatformIO outcomes. Do not claim a fixed SCons duration. Strip fragmented terminal controls with `main/core/console_text.py`; Clear resets pending escape state and history. Never replay build or upload operations.
   - Recognize PlatformIO status/banner records conservatively in `web_bridge.py`; suppress only exact routine boilerplate outside diagnostic context. Preserve every compiler file, compiler notes, source/carets, unknown custom-builder output and SCons failures. Test parser routing without importing the backend or executing builds using `direct/verify_build_log_routing.py`, Qt rendering/copy/follow with `direct/verify_build_console.py`, and queue/resource limits with `direct/verify_performance.py`.
   - Scope ESP connection and BOOT-hint rows to `_op_session_id` with explicit `progress_key` values. Finalize pending polling on every failure, exception and Stop; use compact terminal labels without a live bar. Keyed rows may update across intervening output, but completed diagnostics and generic regex barriers remain immutable. Preserve esptool's wrong-chip detail, show detected/selected chips and the matching-board action, omit upload-speed advice and never retry a confirmed target mismatch. Verify display/copy/theme/clear behavior with `direct/verify_build_console.py` and mocked child/status paths with `direct/verify_upload_workers.py`.
   - Use queued QObject signals for syntax completion, not `QTimer.singleShot` callbacks created inside Python worker threads. Keep one latest pending editor revision and discard stale results. Use one parser on constrained PCs, two otherwise, with bounded revision/diagnostic caches. Send dirty state and the current recovery snapshot together for every edit; do not debounce recovery text. Syntax requests reuse the snapshot, with a full-source fallback for clean models. Invalidate queued/in-flight syntax generations immediately on edits. Use Monaco model/version identity for duplicate checks, a 600 ms typing debounce on constrained PCs (300 ms otherwise), and stop initialization polling once listeners are installed. Preserve table row objects and reading state for unchanged diagnostics. Verify the actual JavaScript scheduler with `direct/verify_editor_resources.js` and the queued/recovery lifecycle with `direct/verify_performance.py`.
   - External edit scans and settled review-journal writes run in one background worker, with coalesced filesystem wakeups and stale-project/manual-save rejection; Qt watcher/timer updates return through queued completion. Hardware-state persistence similarly serializes one active and one latest pending payload. Do not use a timestamp-only source cache at build/upload boundaries or restore copied source timestamps that can hide a changed same-size file from compiler deciders. On a changed staged-file timestamp collision, advance only that generated copy or fail rather than silently accepting stale objects. Verify with `direct/verify_slow_storage_flow.py`.

5. **Runtime Guards & Crash Detection**
   - **`src/modules/private_python_guard.py`**: Windows private runtime / native Linux virtual-environment guard.
   - **`src/modules/platform_runtime.py`**: Host identification, native paths/locks and CPU startup guard. Fewer than four physical cores stops launch; use logical threads only when topology is unavailable. Unknown logical counts fail with a notice.
   - **`src/modules/runtime_resources.py`**: Shared budgets for 4/6 physical cores, at most 6 logical threads or low RAM. Disable editor animation/minimap, reduce syntax cadence and terminal history. Reserve two logical threads for the interface; `main/core/build_resources.py` also caps compiler jobs at 2 on four physical cores and 4 on six, including 8/12-thread SMT CPUs. RAM/storage limits can reduce these further. Clamp saved compiler jobs to these budgets; favor responsiveness over maximum throughput.
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
- **Bootstrap-Only Board Toolchain Preparation**: Explicit offline mode prepares its complete configured plan before launch; online default prepares runtime/SCons and uses explicit board preparation for missing targets. Compile, Upload and Reset never call installers.
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
- Serial output wraps to the viewport by default (`serial_line_wrap`), with a saved Wrap lines action in the output context menu and compact Options. Reflow changes display only; Copy retains original logical line breaks. Anchor wrapped output by UTF-16 character and visual row, retain the character across consecutive resizes, and coalesce deferred layout following with one owned timer. Cancel pending restoration on reader input, selection changes and Clear. Verify with `direct/verify_serial_wrap.py`.
- `main/qt/log_follow.py` preserves log anchors, selection, focus and horizontal position around output mutations. Serial opts into `hold_to_pause`: Auto ON always follows unless its scrollbar is held or dragged; release resumes at the true wrapped bottom without waiting for new output. Checking Auto ON also resumes, or waits for an active hold to end. Coalesce release callbacks with one owned timer, suppress programmatic scroll feedback, preserve selections and horizontal position, and preserve the hold through Clear. Auto OFF keeps the reading anchor through release and reflow. Build output and other workspace logs follow only when enabled, at the bottom and without a held/dragged scrollbar; wheel/key/manual scrolling resumes at the real bottom. Bootstrap opts into `resume_on_release`: holding or dragging freezes its view, release resumes following immediately even above the bottom, and checking Auto ON resumes unless a hold is still active. Apply the final release after the scrollbar's event handler and recheck the checkbox and held state. Auto OFF prevents forced following in every view. Bounded history may evict an anchor and clamp it. Keep assistant HTML fallback reading interaction equivalent to workspace logs and preserve native xterm following. Verify bootstrap with actual Qt scrollbar press/drag/release in `direct/verify_controls.py`.
- `bootstrap_native.py` provides immediate standard-library Tk feedback while first-run Python dependencies install. After successful fresh target-interpreter verification, promote only the display to the original lazily imported `bootstrap_qt.py` view before bulk board-tool preparation. Preserve the same setup worker, installer results, durable log and elapsed start time; never restart setup or install Qt into the base runtime again. `bootstrap_dispatch.py` owns one bounded queue and main-thread sink: every remaining event in a drained batch must reach the new sink. Acknowledge the worker only after Qt's event loop starts, defer the switch while a scrollbar is held, and retain functional Tk on a promotion error. Transfer tagged bounded displayed text, committed/live tables, failed-step state, selection, scroll anchor, Auto-Scroll and Skip Updates preferences. Prefer env Qt before initial imports, retry imports even if the env path is already present, and never unload a foreign loaded Qt/shiboken native graph. Fit Tk native pixels separately from Qt logical geometry. Verify cold construction with `direct/verify_bootstrap_native.py` and promotion/rollback/held/OFF/queue/close interactions with `direct/verify_bootstrap_promotion.py` in isolated fixtures; mock setup, hardware and persistence. Cold absence proof must not borrow Qt; promotion proof may use only the fixture's prepared environment after that boundary.
- Release-seed publication in `bootstrap_seed.py` retries only Windows errors 5/32/33, with at most eight rename attempts and six seconds of total backoff. Recheck `lexists` before each attempt and after failures so occupied and dangling junction destinations are never replaced. Never alter ACLs, remove staging or retry unrelated/POSIX errors. Preserve the original exhaustion error, ZIP and staged payload. `direct/verify_bootstrap_seed.py` covers real native held-file publication, persistent failure retention, competing destinations and a real dangling Windows junction in isolated retained fixtures.
- A failed bootstrap step recolors its whole retained output, including subsections and later progress; reset only at the next step. Plain stdout cannot retroactively recolor past output. `direct/verify_bootstrap_download_failure.py` uses real loopback HTTP 503 with the actual downloader and failure callback, but requires the seed explicitly in its fixture and runs no fallback installers. Preserve its partial checkpoint and capture/log evidence; do not call it a complete production setup run.
- Setup uses a resizable PySide6 glass dialog after Python dependency verification. Keep immediate Tk feedback usable before Qt is installed. Share theme tokens for status and live progress; persistent QTextCursor anchors must survive log trimming. Closing/hiding must not be undone by the topmost timer. Never unload Qt/shiboken extensions from a foreign or failed partial native import while promoting the view.
- Journal stage headings use visible-block-only circuit divider painting, separate from selectable/copied text. Preserve markers on native snapshot restoration, reflow long headings, and select failed-stage text from the tracked cursor's position using a fresh collapsed cursor rather than its advancing anchor. Update checks start their own top-level stage before skip/offline outcomes. Verify with `direct/verify_bootstrap_headers.py`, controls and promotion fixtures.
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
- The downloader (`arduino_lib_req.py`) retains Tk/ttk network and archive workflows. `tk_glass.py` supplies static glass cards around package lists, details and installed items in all three palettes; advanced URLs live behind **Board indexes**. Normalize comma/newline-separated HTTP(S) sources, validate extensible package structures, deduplicate URLs and retain usable catalogs on source errors. Failed atomic settings writes preserve the previous active/saved list and report failure. Preserve search, installed-item details, version selection, checksum validation and extraction cancellation. Reopening refreshes the per-user theme without rebuilding widgets or losing state. Extracted/both downloads start explicit background preparation; Archive Only remains unprepared until **Prepare board support** is chosen. Retain the previous extracted payload if promotion fails and use nonmodal completion status/history.
- `browser_loading.py` validates compact catalogs against source path/mtime/size and schema, rebuilding damaged or stale derived caches. Cache writes are atomic and bounded. Keep large raw JSON out of the RAM cache, publish cached data before HTTP refresh, and use two network workers on constrained hosts (four otherwise). `TkTasks` is the Tk-thread delivery boundary; workers never call Tcl. Search ranking, selected-version status, installed inventories and detail selections use one active scan plus one latest request; cancel stale scans and stop dispatcher timers when idle. Preserve active search on catalog updates, batch list/label delivery and cancel closed-widget callbacks. Verify with `direct/verify_browser_loading.py` using temporary catalogs and mocked HTTP.
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
& src/_python/python.exe -B direct/verify_build_log_routing.py
& src/_python/python.exe -B direct/verify_build_console.py
& src/_python/python.exe -B direct/verify_platforms.py
& src/_python/python.exe -B direct/verify_offline.py
& src/_python/python.exe -B direct/verify_bootstrap_board_coverage.py
& src/_python/python.exe -B direct/verify_bootstrap_arduino_sources.py
& src/_python/python.exe -B direct/verify_arduino_source_targets.py
& src/_python/python.exe -B direct/verify_board_declarations.py
& src/_python/python.exe -B direct/verify_bootstrap_seed.py
& src/_python/python.exe -B direct/verify_bootstrap_updates.py
& src/_python/python.exe -B direct/verify_bootstrap_download_failure.py
& src/_python/python.exe -B direct/verify_bootstrap_native.py --report-dir temp/audit/bootstrap-cold
& src/_python/python.exe -B direct/verify_bootstrap_promotion.py --report-dir temp/audit/bootstrap-promotion
& src/_python/python.exe -B direct/verify_bootstrap_pip_paths.py
& src/_python/python.exe -B direct/verify_controls.py
& src/_python/python.exe -B direct/verify_browser_loading.py
& src/_python/python.exe -B direct/verify_package_jobs.py
& src/_python/python.exe -B direct/verify_board_preparation.py
& src/_python/python.exe -B direct/verify_board_index_targets.py
& src/_python/python.exe -B direct/verify_arduino_fallback.py
& src/_python/python.exe -B direct/verify_platformio_locks.py
& src/_python/python.exe -B direct/verify_custom_board_dialog.py --render-dir temp/audit/custom-board-dialog
& src/_python/python.exe -B direct/verify_package_progress.py
& src/_python/python.exe -B direct/verify_package_coverage.py
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
`verify_package_jobs.py` runs real private-runtime lease children against fake
stores under `temp/`: concurrent readers, exclusive/queued writers, cancellation,
crash cleanup, denied writes, atomic preservation, record/retention bounds,
independent readers and full request/coverage files. All backend verifiers must
set `MCU_PACKAGE_EVENTS_ROOT` to their own `temp/` fixture directory when they
exercise package guards.
Host-repair fixtures execute the source-extracted Windows worker with all
installers mocked and the Ubuntu entry point with subprocesses/venv mocked;
check exclusive tool-store phases, early failure release and launch after
release. The Windows repair helper must reject waiting on the GUI thread.
`verify_board_preparation.py` mocks installation/readiness and verifies both
exact NodeMCU identities, reviewed/custom mappings, complete plan preservation,
unsupported frameworks, failed preparation and full per-board coverage. These
checks never run live setup or prove physical flashing.
`verify_package_progress.py` checks focus, stale events, concurrent jobs,
dismissal, themes, compact card placement and deferred catalog refresh.
`verify_package_coverage.py` checks full reports, asynchronous search/filtering,
validation and notification links with isolated report files.
`verify_custom_board_dialog.py` mocks settings persistence and checks literal
platform specifications, failed-save retry, cancellation and all palettes on
short monitors. Keep its actions pinned, reveal focused fields by scrolling and
use `tk_glass.DialogFit` for measured native frames and negative monitor origins;
do not multiply Qt scaling into Tk metrics. Check at 100/125/150/175/200%.

Patch persistence, hardware and metadata generation in verifier fixtures; never
write live sketch/cache/journal state. Store screenshots and scratch scripts in
`temp/`. Keep README, Ubuntu notes and generated AGENTS application guidance
aligned. Native Ubuntu and physical board compile/upload results must be reported
separately from local Windows verification.
