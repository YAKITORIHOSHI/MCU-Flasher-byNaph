---
name: mcu-sketch-target
description: "Exact target and declared framework, architecture-specific GPIO/library constraints, upload transport and root firmware source boundaries."
---

# Microcontroller Target & Hardware Specifications

## 🎯 TARGET HARDWARE ARCHITECTURE
- **Selected Board**: `None (No board currently selected in GUI)`
- **Platform / Architecture**: `N/A`
- **FQBN / Board ID**: `N/A`
- **Active Port**: `None (No microcontroller connected)`
- **Serial Baud Rate**: `115200`

## 💡 CODING & LIBRARY GUIDELINES
- **Exact target**: Use the resolved PlatformIO definition and declared framework. Arduino `.ino` sketches require Arduino; other frameworks need matching sources/libraries. Never substitute a family or invent hardware.
- **Host and ports**: Windows uses COM ports; Ubuntu uses native device paths. USB/debug programmer targets may use native transports without a serial port.
- **Arduino / ESP32 Code**: Write high-quality, non-blocking Arduino C++ code.
- **Libraries**: Use standard Arduino libraries matching the architecture (e.g. `Adafruit_NeoPixel`, `FastLED`, `WiFi`, `BluetoothSerial`, `Wire`, `SPI`).
- **Pin Assignments**: Verify GPIO pin compatibility with the selected board architecture.
- **File Boundary**: Only modify files in `C:\Users\napht\Documents\MCU Flasher by Naph - Stable Release` (`*.ino`, `*.cpp`, `*.h`). Never touch cache files in `.mcu_flasher_build_cache/`.
