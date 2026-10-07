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

This directory contains the MCU Flasher application. When the user requests app changes, the root-sketch restriction in Step 2 applies to firmware work only. App work may edit `main/`, `src/` source/assets, `direct/`, documentation, `.github/` and `.agents/skills/` within this checkout. Preserve live hardware specifications, user sketches, settings, and the protected cache/journal boundaries above. Do not infer a connected board.

- Read `.agents/skills/mcu-flash-gui-dev/SKILL.md` for app development; use frontend-design for visual changes and project-hygiene for generated metadata logic.
- Keep the workspace compact: board selection stays in Controls; no target-board banner. Every toolbar widget must have a parent before being shown. Only explicit Project, dialogs, menus and Detach Editor actions open additional windows. Project opens independent sketch windows without replacing dirty buffers; set the main window minimum width to half its monitor work-area width and preserve normal resizing and maximization.
- Preserve keyboard focus, source-tab dirty state/order and editor buffers through resizing, theme changes and detach/reattach. Use static theme-based glass surfaces.
- Use Qt logical sizing without applying DPI twice. Fit window frames to the current monitor work area, reflow compact rows and scroll short details. Keep Tk native metrics separate and preserve saved content fonts. Reserve height for tool tabs and active panel controls before log output. Verify with `direct/verify_responsive.py`.
- Keep setup and downloader palettes consistent with the workspace, including Solarized. Revalidate busy/target state in Actions and Settings callbacks; report failed settings writes. Verify auxiliary screens with isolated fixtures, without installing or launching.
- Support Windows and native Ubuntu paths separately. Require four CPU cores (physical when detectable, logical fallback), and keep four/six-core resource budgets.
- Show the workspace before starting services. Keep discovery and parsing off the GUI thread; bound log events, display histories and caches. Return syntax completion through queued Qt signals and discard stale revisions.
- Consider HDD/removable/network storage independently of CPU speed. Keep storage probes and source scans off the GUI thread, avoid unchanged metadata writes, and verify actual source bytes before firmware reuse on coarse-timestamp filesystems.
- Preserve PTY capability replies, Unicode, bracketed paste and output backpressure for coding CLIs. Clear is display-only. Never replay commands or hardware writes.
- Run hardware-free `direct/verify_runtime.py` using the private runtime; on Windows also run `direct/verify_terminal.py`. Put captures and audit scratch work in `temp/`. Use `direct/verify_performance.py` for isolated resource regressions. Mock persistence/hardware calls; never run metadata generation against live caches during verification. Update README and relevant skills with behavior changes.

