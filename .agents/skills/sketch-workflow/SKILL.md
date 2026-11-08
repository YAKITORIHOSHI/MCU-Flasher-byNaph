---
name: sketch-workflow
description: "Live hardware state, exact board/framework settings, serial ports and root-sketch workflow. Use for firmware and hardware questions; use mcu-flash-gui-dev for application changes."
---

# Sketch Project Workflow & Priority Guide

## Live Hardware State
Hardware values are not fixed in this portable skill. Use the current MCU Flasher GUI when available; otherwise read `.mcu_flasher_build_cache/project_state.json` (read-only). Report only values present in that state. If a value is absent, say it is unavailable; never infer it from manifests or installed packages.

### Step 1: Hardware Query
Use current GUI state or `.mcu_flasher_build_cache/project_state.json` (read-only). Inspect the `hardware` object: if `hardware.mcu_connected` is false or `hardware.port` is null, report that no microcontroller is connected. If `hardware.board_selected` is false or `hardware.board_name` is null, report that no board is selected. Do not recursively scan disks, package trees, or inspect `platformio.ini` to guess board selection.

### Step 2: Read & Edit Only Root Sketch Files
Confine firmware source changes strictly to the active project's root:
- Primary Arduino sketches: `*.ino` (Main sketch file at root)
- Header files: `*.h`, `*.hpp` (C/C++ header files at root)
- Source files: `*.cpp`, `*.c` (C/C++ source code files at root)
- Project documentation: `NOTE.txt` or other user-requested root text files
Never search parent or sibling directories, and never create nested source directories or submodules unless explicitly requested.

### Step 3: Check Build & Notification History
When troubleshooting compilation errors, upload failures, or reset issues, read `.mcu_flasher_build_cache/dbs_notif.json` (read-only) to inspect recent compiler errors, missing library warnings, toolchain logs, upload status, device connect/disconnect events, and library installations.

### Step 4: Strict No-Touch on Build & Cache Files
Files and directories inside `.mcu_flasher_build_cache/`, `.pio/`, `.vscode/`, `.clangd/`, and `.opencode/` are internal build inputs, backups, and PlatformIO toolchain data. NEVER edit, modify, rename, or delete files inside these protected locations.

### Step 5: AI Edit Backup & Recovery
Automated edit backups are maintained in `.mcu_flasher_build_cache/.mcu_ai_edits` (relative to project root) following the layout `M-D-YY/sessionN/editN.txt`. Each file preserves exact BEFORE, AI/AFTER, current-applied, and Undo-target copies. Treat the backup tree as READ-ONLY. Never modify backup files or `.mcu_flasher_build_cache/.mcu_ai_edits/.state`. When the user requests recovering or comparing an earlier AI edit, locate the matching file entry and restore only the requested content section.
