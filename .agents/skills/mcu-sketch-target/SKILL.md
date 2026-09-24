---
name: mcu-sketch-target
description: "Microcontroller target architecture (ESP32/ESP8266/AVR), GPIO pinout constraints, library dependencies (NeoPixel, FastLED, WiFi, BLE, SPI, Wire), and sketch source file boundaries."
---

# Microcontroller Target & Hardware Specifications

## 🎯 TARGET HARDWARE ARCHITECTURE
- **Selected Board**: `None (No board currently selected in GUI)`
- **Platform / Architecture**: `N/A`
- **FQBN / Board ID**: `N/A`
- **Active Port**: `None (No microcontroller connected)`
- **Serial Baud Rate**: `115200`

## 💡 CODING & LIBRARY GUIDELINES
- **Arduino / ESP32 Code**: Write high-quality, non-blocking Arduino C++ code.
- **Libraries**: Use standard Arduino libraries matching the architecture (e.g. `Adafruit_NeoPixel`, `FastLED`, `WiFi`, `BluetoothSerial`, `Wire`, `SPI`).
- **Pin Assignments**: Verify GPIO pin compatibility with the selected board architecture.
- **File Boundary**: Only modify files in `C:\Users\napht\Documents\MCU Flasher by Naph - Stable Release` (`*.ino`, `*.cpp`, `*.h`). Never touch cache files in `.mcu_flasher_build_cache/`.
