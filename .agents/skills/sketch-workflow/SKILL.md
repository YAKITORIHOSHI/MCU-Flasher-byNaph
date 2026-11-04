---
name: sketch-workflow
description: "Live hardware state, exact board/framework settings, serial ports and root-sketch workflow. Use for firmware and hardware questions; use mcu-flash-gui-dev for application changes."
---

# Sketch Project Workflow & Priority Guide

## Live Hardware State
Hardware values are not fixed in this portable skill. Use the current MCU Flasher GUI when available; otherwise read `.mcu_flasher_build_cache/project_state.json` (read-only). Report only values present in that state. If a value is absent, say it is unavailable; never infer it from manifests or installed packages.

### Step 1: Hardware Query
Use current GUI state or the project-relative state file above. Do not recursively scan disks or inspect `platformio.ini` to guess board selection.

### Step 2: Read & Edit Only Root Sketch Files
Confine firmware source changes to the active project's root:
- Primary sketches: `*.ino`
- Header files: `*.h`, `*.hpp`
- Source files: `*.cpp`, `*.c`
- Documentation: `NOTE.txt` or other user-requested root text files
Never search parent or sibling directories, and never edit `.mcu_flasher_build_cache/`, `.pio/`, or `.opencode/`.

### Step 3: Check Build & Notification History
For compiler or upload troubleshooting, read `.mcu_flasher_build_cache/dbs_notif.json` (read-only).
## Application development scope

This directory contains the MCU Flasher application. When the user requests app changes, the root-sketch restriction in Step 2 applies to firmware work only. App work may edit `main/`, `src/` source/assets, `direct/`, `desktop/`, `LAUNCHER/`, `cleaner/`, documentation, `.github/` and `.agents/skills/` within this checkout. Preserve live hardware specifications, user sketches, settings, and the protected cache/journal boundaries above. Do not infer a connected board.

- Read `.agents/skills/mcu-flash-gui-dev/SKILL.md` for app development; use frontend-design for visual changes and project-hygiene for metadata, Fresh cleanup and public release preparation.
- The shipping GUI/backend still uses Python; native Rust development is separate. Read `audit/migration/CURRENT.md` before continuing migration work and preserve exact source/host evidence and unfinished platform gates.
- Keep the workspace compact: board selection stays in Controls; no target-board banner. Every toolbar widget must have a parent before being shown. Only explicit Project, dialogs, menus and Detach Editor actions open additional windows. Project opens independent sketch windows without replacing dirty buffers; permit a half-monitor complete frame by subtracting measured native borders from the client minimum in Qt logical pixels, and preserve normal resizing and maximization.
- Preserve keyboard focus, source-tab dirty state/order and editor buffers through resizing, theme changes and detach/reattach. Use static theme-based glass surfaces.
- Initialize editor surfaces, fonts and resource options before loading Monaco; use the native theme's reading background and one initial source manifest. Compile/Upload must await complete model loading and acknowledged buffer saves.
- A launch without an explicit project opens the project selector. Ghost Sketch requires an explicit action; dialog Return defaults must not create it while editing another field.
- Preserve incremental sketch objects after source edits and failures. Verify source, dependency and firmware bytes before reuse; keep Hard/Soft Reset builds in shared exact-board, framework and native-host storage independent of sketch projects and project Clean.
- Move the same primary action row and shortcuts with the detached editor and restore them above Controls. Keep board/port Controls in the main window and project callbacks/busy state bound to the original workspace.
- Ghost Sketch uses owned temporary application storage. Compile/Upload flush temporary inputs without a location prompt; Save/Ctrl+S/Save All chooses an empty permanent project folder. Lock editing through acknowledged buffer flush, supported root-source copy and model-path remap; preserve undo/tab state. Disable autosave until permanent save and require Save/Discard/Cancel on close or switch even after a clean compile. Release only the exact marked temporary session when eligible. After acknowledged save, retain the temporary original if an AI recovery-history directory exists; check only directory existence, never backup contents or `.state`. Keep automatic saved-session cleanup separate from explicit discard.
- Use Qt logical sizing without applying DPI twice. Fit window frames to the current monitor work area, reflow compact rows and scroll short details. Preserve saved content fonts. Reserve height for tool tabs and active panel controls before log output. Verify with `direct/verify_responsive.py`.
- Keep setup and downloader palettes consistent with the workspace, including Solarized. Revalidate busy/target state in Actions and Settings callbacks; report failed settings writes. Verify auxiliary screens with isolated fixtures, without installing or launching.
- Support Windows, native Ubuntu 24.04 or newer, Linux Mint 22.x, Fedora 44 or newer and macOS 14 or newer through separate reviewed policies and package adapters. Validate native OS-release before startup, setup or repair; reject missing/malformed releases without cleanup, never admit a host from `ID_LIKE`, and never route Fedora through apt. Require four CPU cores (physical when detectable, logical fallback), and keep four/six-core resource budgets.
- Show the workspace before starting services. Keep discovery and parsing off the GUI thread; bound log events, display histories and caches. Return syntax completion through queued Qt signals and discard stale revisions.
- Consider HDD/removable/network storage independently of CPU speed. Keep storage probes and source scans off the GUI thread, avoid unchanged metadata writes, and verify actual source bytes before firmware reuse on coarse-timestamp filesystems.
- Preserve PTY capability replies, Unicode, bracketed paste and output backpressure for coding CLIs. Clear is display-only. Never replay commands or hardware writes.
- Run hardware-free `direct/verify_runtime.py` using the private runtime; on Windows also run `direct/verify_terminal.py`. Put captures and audit scratch work in `temp/`. Use `direct/verify_performance.py` for isolated resource regressions. Mock persistence/hardware calls; never run metadata generation against live caches during verification. Run verifiers with `DISPLAY` and `WAYLAND_DISPLAY` removed, or through `direct/run_verifiers.py`, and give native dialogs an owned recorder: a fixture must never open a real window on the developer's desktop.
- Before Fresh cleanup, finish checks that need the private runtime. Prepare public archives from committed defaults with `direct/build_release.py`; put project files directly at the ZIP root without a wrapper folder, retain the complete private Windows runtime and verify installer hashes, archive contents and Git integrity before pushing. Local settings and protected state stay out of the release.
- Bootstrap dependency work uses finite elapsed-time budgets, never console-silence retries. Preserve cancellation of owned children, independent staged-package verification and native copy semantics; measure performance only in owned temp fixtures and retain phase timings without reducing declared coverage.
- Bootstrap application source updates use finite elapsed-time GitHub checks before standalone Windows, native Linux or macOS launches. A release records its commit in `src/dbs/source_revision.json`; replace it only when GitHub reports the branch is ahead or diverged, because a fresh ZIP can be newer than `main`. Stage a verified shallow clone in per-user state and atomically replace only application-owned source; preserve user settings, protected caches, private runtimes and installers. Defer automatic updates while any workspace is active, retain the current source after failures, and verify with `direct/verify_app_update.py` and `direct/verify_ubuntu_launcher.py`.
- Measure toolbar and panel content against their allocated logical widths, including native margins and spacing. Keep primary-toolbar native insets symmetric and its bottom divider clear of buttons in normal and short windows. Keep Project and Resources reachable through native toolbar overflow and resize/theme changes; verify real QMainWindow containers, not only ordinary layouts.
- Accepted workspace closes must retire owned browser pages while Qt runs; preserve close vetoes and other workspaces. Verify normal process exit with `direct/verify_shutdown.py`.
- With behavior changes, update README, RELEASE_NOTES, relevant SKILL.md instructions and descriptions, and existing agent metadata. Keep this application guidance template and the checkout's AGENTS.md aligned without regenerating live project/cache metadata.

