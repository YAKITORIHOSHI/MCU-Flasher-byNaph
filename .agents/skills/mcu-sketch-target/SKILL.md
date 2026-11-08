---
name: mcu-sketch-target
description: "Exact target and declared framework, architecture-specific GPIO/library constraints, upload transport and root firmware source boundaries."
---

# Microcontroller Target & Hardware Specifications

## Current Target & Live Hardware State
No board, port, baud rate, upload speed, platform, or FQBN is embedded here. Read current GUI state or `.mcu_flasher_build_cache/project_state.json` (read-only) when a task needs those values. If the requested value is unavailable, ask rather than guessing.

## Project State JSON Schema
The `.mcu_flasher_build_cache/project_state.json` file contains a `hardware` block updated automatically in the background:
- `board_selected` (boolean): `true` if a board is currently selected in the GUI, `false` otherwise.
- `board_name` (string | null): Resolved board display name (e.g. `"ESP32 Dev Module"`, `"Arduino Uno"`).
- `mcu_connected` (boolean): `true` if a microcontroller is actively detected on a communication port, `false` otherwise.
- `port` (string | null): Operating system port device path (e.g. `"COM4"`, `"/dev/ttyUSB0"`).
- `port_name` (string | null): Active port device name (e.g. `"COM4"`, `"/dev/ttyUSB0"`).
- `port_number` (integer | null): Integer port index extracted from the device string (e.g. `4` for `COM4`, `0` for `/dev/ttyUSB0`).
- `platform` (string | null): Microcontroller platform identifier (e.g. `"espressif32"`, `"espressif8266"`, `"atmelavr"`).
- `framework` (string | null): Declared firmware framework (e.g. `"arduino"`).
- `baud_rate` (integer): Serial monitor baud rate (e.g. `115200`).

## Architecture-Specific Firmware Constraints

### ESP32 (ESP32-WROOM, ESP32-WROVER, ESP32-S2, ESP32-S3, ESP32-C3)
- **Strapping Pins**: Pay careful attention to boot strapping pins:
  - `GPIO 0`: Boot mode selection (LOW = UART download bootloader, HIGH = SPI boot/run). Avoid pulling LOW during normal boot.
  - `GPIO 2`: Must be left floating or pulled LOW during flashing on ESP32-WROOM modules.
  - `GPIO 12` (MTDI): Selects flash voltage (LOW = 3.3V, HIGH = 1.8V). Pulling HIGH at boot will brick/brownout flash on 3.3V flash modules.
  - `GPIO 15` (MTDO): Boot log silencing (pull LOW to suppress ROM debug output).
  - Do not attach external pull-up/pull-down loads, sensors, or relays to strapping pins without verified circuit isolation.
- **Prohibited SPI Flash Pins**: `GPIO 6` through `GPIO 11` (CLK, SD0, SD1, SD2, SD3, CMD) connect directly to the integrated SPI flash IC. Never configure or use GPIO 6–11 in user firmware; accessing them will immediately crash the CPU.
- **ADC2 & WiFi Coexistence Conflict**: All ADC2 channels (`GPIO 0`, `GPIO 2`, `GPIO 4`, `GPIO 12`, `GPIO 13`, `GPIO 14`, `GPIO 15`, `GPIO 25`, `GPIO 26`, `GPIO 27`) share internal hardware with the Wi-Fi subsystem. When Wi-Fi is active (`WiFi.begin()`), `analogRead()` calls on ADC2 channels fail and return invalid readings. Always use ADC1 channels (`GPIO 32` through `GPIO 39`) for analog input whenever Wi-Fi is used.
- **Input-Only Pins (GPI)**: `GPIO 34`, `GPIO 35`, `GPIO 36` (SENSOR_VP), and `GPIO 39` (SENSOR_VN) are input-only. They have no internal pull-up or pull-down resistors (`pinMode(pin, INPUT_PULLUP)` is unsupported). External pull resistors are required.

### ESP8266 (NodeMCU, Wemos D1 Mini, ESP-01)
- **Watchdog Timer (WDT) Starvation**: The single-core CPU handles WiFi, TCP/IP, and user code sequentially. Blocking loops longer than ~1–3 seconds cause hardware/software watchdog resets (`wdt reset`). In heavy loops, always call `yield()` or `delay(0)` to service WiFi and feed the watchdog.
- **Strapping & Boot Pins**: `GPIO 0` (must be HIGH for run mode, LOW for UART flash), `GPIO 2` (must be HIGH at boot), `GPIO 15` (must be pulled LOW at boot).
- **Deep Sleep Wake Connection**: To wake from `ESP.deepSleep()`, connect `GPIO 16` (D0 / WAKE) directly to the `RST` pin.

### AVR Architecture (Arduino Uno, Nano, Mega 2560, Leonardo)
- **SRAM Budget & F() Macro**: ATmega328P has only 2 KB of SRAM. Constant string literals placed in `Serial.print("...")` consume SRAM by default. Always wrap string literals in the `F()` macro (e.g. `Serial.println(F("Message"))`) to store them in Flash memory (PROGMEM).
- **Hardware UART Pins**: `GPIO 0` (RX) and `GPIO 1` (TX) are directly connected to the USB-serial bridge. Avoid using pins 0 and 1 for general I/O to prevent upload errors and corrupted serial monitoring.

### STM32 & RP2040 (Raspberry Pi Pico)
- **Native USB CDC**: Native USB CDC ports require a 1200 bps touch sequence to trigger bootloader mode. When waiting for serial output in `setup()`, use a non-blocking timeout: `while (!Serial && millis() < 3000);` so execution continues if no host serial monitor connects.

## Coding & File Boundaries
- **Exact target**: Use the resolved PlatformIO definition and declared framework. Arduino `.ino` sketches require Arduino; other frameworks need matching sources/libraries. Never substitute a family or invent hardware.
- **Host and ports**: Windows uses COM ports; Linux/macOS uses native device paths. USB/debug programmer targets may use native transports without a serial port.
- **Arduino / ESP32 Code**: Write high-quality, non-blocking Arduino C++ code.
- **Libraries**: Use standard Arduino libraries matching the architecture (e.g. `Adafruit_NeoPixel`, `FastLED`, `WiFi`, `BluetoothSerial`, `Wire`, `SPI`).
- **Pin Assignments**: Verify GPIO pin compatibility with the current board before assigning pins.
- **File Boundary**: Firmware edits stay strictly in root sketch files (`*.ino`, `*.cpp`, `*.c`, `*.h`, `*.hpp`, `NOTE.txt`) relative to the active project. Never touch `.mcu_flasher_build_cache/`, `.pio/`, or other cache/toolchain folders.
