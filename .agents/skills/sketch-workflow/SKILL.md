---
name: sketch-workflow
description: "Root-sketch workflow in MCU Flasher: live board and port state, which files to edit, where compile and upload results are, and what never to touch. Use for any firmware or hardware task in this sketch."
---

# Sketch workflow in MCU Flasher

MCU Flasher compiles, uploads and monitors this sketch. You edit its source files; the user presses Compile or Upload. Do not run compilers, PlatformIO or upload tools yourself.

## 1. Live hardware state
Board, port and speeds change while the app runs. Read `.mcu_flasher_build_cache/project_state.json` (read-only) and its `hardware` object. If `hardware.mcu_connected` is false or `hardware.port` is null, say no microcontroller is connected. If `hardware.board_selected` is false or `hardware.board_name` is null, say no board is selected. Report only values present there; never infer a board or port from `platformio.ini`, package folders or older generated text.

## 2. Edit only the sketch's root files
- `*.ino`: the main Arduino sketch, and other tabs of it
- `*.h`, `*.hpp`: headers
- `*.cpp`, `*.c`: C/C++ sources
- `NOTE.txt` and other root text notes the user asks for

Never read parent or sibling folders, and never create nested source folders unless the user asks.

## 3. Compile and upload results
The Build Console shows every compiler error and warning with its file and line, and the upload log. When the user reports a failed build, ask for those lines or read the recent results in `.mcu_flasher_build_cache/dbs_notif.json` (read-only): compiler errors, missing libraries, upload status and device connect or disconnect events. Fix the reported file and line in the root sources; the app checks syntax only by compiling.

## 4. Never touch
`.mcu_flasher_build_cache/`, `.pio/`, `.vscode/`, `.clangd/`, `.opencode/`, `.claude/` and `.agents/` hold build inputs, caches, backups and these instructions. Never edit, rename or delete anything inside them.

## 5. AI edit backups
Every AI edit is backed up in `.mcu_flasher_build_cache/.mcu_ai_edits`, laid out as `M-D-YY/sessionN/editN.txt` with the exact before, after, applied and undo copies. Treat it as read-only and never read `.mcu_flasher_build_cache/.mcu_ai_edits/.state`. When the user asks to recover or compare an earlier AI edit, find the matching file entry and restore only the requested part. The user can also revert an edit or a whole session from the app's AI Changes tab.

---
*Generated automatically by MCU Flash GUI for this sketch's coding assistants; it is rewritten when the sketch opens.*
