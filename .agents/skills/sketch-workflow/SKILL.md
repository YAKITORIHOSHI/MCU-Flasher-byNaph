---
name: sketch-workflow
description: "Live hardware specs, exact board/framework settings, serial ports and root-sketch workflow. Use for firmware and hardware questions; use mcu-flash-gui-dev for application changes."
---

# Sketch Project Workflow & Priority Guide

## 📋 LIVE HARDWARE & PROJECT SPECIFICATIONS (ALWAYS USE THIS LIST)
- **Active Project Directory**: `C:\Users\napht\Documents\MCU Flasher by Naph - Stable Release`
- **Authoritative Hardware Status**: `No board selected in GUI and no microcontroller connected.`
- **Currently Selected Board**: `None (No board currently selected in GUI)`
- **Target Platform / Architecture**: `N/A` (N/A)
- **Connected COM Port**: `None (No microcontroller connected)`
- **Serial Monitor Baud Rate**: `115200`
- **Upload Speed**: `460800` bps
- **Main Sketch Files in Project Root**: `None found`

*CRITICAL DIRECTIVE FOR AI: Answer all board, port, and upload speed questions directly from the list above without searching the disk.*

### Step 1: Live Hardware Query
Use the live specifications above or read `.mcu_flasher_build_cache/project_state.json`. Never perform recursive disk scans or read `platformio.ini`.

### Step 2: Read & Edit Only Root Sketch Files
Confine all source code changes strictly to the root sketch directory:
- Primary sketches: `*.ino`
- Header files: `*.h`, `*.hpp`
- Source files: `*.cpp`, `*.c`
- Documentation: `NOTE.txt`
Never search parent directories or edit files in `.mcu_flasher_build_cache/`, `.pio/`, or `.opencode/`.

### Step 3: Check Build & Notification History
If troubleshooting compiler or upload errors, read `.mcu_flasher_build_cache/dbs_notif.json` to view recent error logs and device events.
## Application development scope

This directory contains the MCU Flasher application. When the user requests app changes, the root-sketch restriction in Step 2 applies to firmware work only. App work may edit `main/`, `src/` source/assets, `direct/`, documentation, `.github/` and `.agents/skills/` within this checkout. Preserve live hardware specifications, user sketches, settings, and the protected cache/journal boundaries above. Do not infer a connected board.

- Read `.agents/skills/mcu-flash-gui-dev/SKILL.md` for app development; use frontend-design for visual changes and project-hygiene for generated metadata logic.
- Keep the workspace compact: board selection stays in Controls; no target-board banner. Every toolbar widget must have a parent before being shown. Only explicit Project, dialogs, menus and Detach Editor actions open additional windows. Project opens independent sketch windows without replacing dirty buffers; set the main window minimum width to half its monitor work-area width and preserve normal resizing and maximization.
- Preserve keyboard focus, source-tab dirty state/order and editor buffers through resizing, theme changes and detach/reattach. Use static theme-based glass surfaces.
- Use Qt logical sizing without applying DPI twice. Fit window frames to the current monitor work area, reflow compact rows and scroll short details. Keep Tk native metrics separate and preserve saved content fonts. Reserve height for tool tabs and active panel controls before log output. Verify with `direct/verify_responsive.py`.
- Keep setup and downloader palettes consistent with the workspace, including Solarized. Revalidate busy/target state in Actions and Settings callbacks; report failed settings writes. Verify auxiliary screens with isolated fixtures, without installing or launching.
- Support Windows and native Ubuntu paths separately. Require four CPU cores (physical when detectable, logical fallback), and keep four/six-core resource budgets.
- Show the workspace before starting services. Keep discovery and parsing off the GUI thread; bound log events, display histories and caches. Return syntax completion through queued Qt signals and discard stale revisions.
- Preserve PTY capability replies, Unicode, bracketed paste and output backpressure for coding CLIs. Clear is display-only. Never replay commands or hardware writes.
- Run hardware-free `direct/verify_runtime.py` using the private runtime; on Windows also run `direct/verify_terminal.py`. Put captures and audit scratch work in `temp/`. Use `direct/verify_performance.py` for isolated resource regressions. Mock persistence/hardware calls; never run metadata generation against live caches during verification. Update README and relevant skills with behavior changes.
