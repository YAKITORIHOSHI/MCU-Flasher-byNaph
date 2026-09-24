---
name: sketch-workflow
description: "Live microcontroller hardware specs, board settings, COM port, baud rate, and sketch workflow. Use for all Arduino/ESP32 coding, editing, and hardware questions."
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
