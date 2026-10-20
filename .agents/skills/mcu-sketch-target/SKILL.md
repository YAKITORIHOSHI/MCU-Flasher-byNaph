---
name: mcu-sketch-target
description: "Exact target and declared framework, architecture-specific GPIO/library constraints, upload transport and root firmware source boundaries."
---

# Microcontroller Target & Hardware Specifications

## Current Target
No board, port, baud rate, upload speed, platform, or FQBN is embedded here. Read current GUI state or `.mcu_flasher_build_cache/project_state.json` (read-only) when a task needs those values. If the requested value is unavailable, ask rather than guessing.

## Coding & Library Guidelines
- **Exact target**: Use the resolved PlatformIO definition and declared framework. Arduino `.ino` sketches require Arduino; other frameworks need matching sources/libraries. Never substitute a family or invent hardware.
- **Board identifiers**: Use the shared board-only syntax validator. Valid registered board IDs may contain `+` (for example `STC89C516RD+`); this does not relax platform/framework/protocol validation. An index compile audit requires unique exact ID/name evidence; common chips, variants, USB IDs or defines alone never establish board equivalence.
- **Host and ports**: Windows uses COM ports; Ubuntu uses native device paths. USB/debug programmer targets may use native transports without a serial port.
- **Arduino / ESP32 Code**: Write high-quality, non-blocking Arduino C++ code.
- **Libraries**: Use standard Arduino libraries matching the architecture (e.g. `Adafruit_NeoPixel`, `FastLED`, `WiFi`, `BluetoothSerial`, `Wire`, `SPI`).
- **Pin Assignments**: Verify GPIO pin compatibility with the current board before assigning pins.
- **File Boundary**: Firmware edits stay in root sketch files (`*.ino`, `*.cpp`, `*.h`, `*.hpp`) relative to the active project. Never touch `.mcu_flasher_build_cache/`.
