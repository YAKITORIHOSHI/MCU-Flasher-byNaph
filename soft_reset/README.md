# MCU Flasher — Soft Reset Architecture & Templates

This directory houses the dynamic board recovery templates and caches for **Soft Reset** (flashing a clean, neutral recovery sketch to clear lockups, bootloops, and frozen user sketches).

## Template Directory Structure

| Template Directory | Target Board Family / Architecture | Default Board / Platform |
| :--- | :--- | :--- |
| `soft_reset_project/` | Espressif 32 (ESP32, S2, S3, C3, C6, H2) & General Fallback | `espressif32` (`esp32dev`) |
| `soft_reset_project_uno/` | Atmel AVR (Arduino Uno, Nano, Mega, Leonardo, Pro Mini) | `atmelavr` (`uno`) |
| `soft_reset_project_esp8266/` | Espressif 8266 (ESP8266, NodeMCU, D1 Mini) | `espressif8266` (`nodemcuv2`) |
| `soft_reset_project_stm32/` | ST STM32 (Nucleo, BluePill, BlackPill, Discovery) | `ststm32` (`nucleo_f401re`) |
| `soft_reset_project_rp2040/` | Raspberry Pi RP2040 (Pico, Pico W, RP2040) | `raspberrypi` (`pico`) |
| `soft_reset_project_samd/` | Atmel SAM / SAMD (Arduino Zero, SAMD21, SAMD51) | `atmelsam` (`zeroUSB`) |
| `soft_reset_project_teensy/` | PJRC Teensy (Teensy 4.0, 4.1, 3.2, LC) | `teensy` (`teensy40`) |
| `soft_reset_project_nrf52/` | Nordic nRF52 (nRF52840-DK, micro:bit v2, XIAO) | `nordicnrf52` (`nrf52840_dk`) |
| `soft_reset_project_renesas/` | Renesas RA (Arduino UNO R4 Minima, UNO R4 WiFi) | `renesas-ra` (`uno_r4_minima`) |

## Dynamic Board Cache Resolution

When Soft Reset is invoked for any selected board:
1. `_soft_reset_project_dir()` resolves the board's platform family.
2. The exact board's build cache is isolated to `soft_reset/<template>/boards/<board_cache_key>/`.
3. `_reset_project_contents()` dynamically generates the exact `platformio.ini` with:
   - Board ID, platform, framework, and monitor baud rate.
   - Upload protocols (esptool, stlink, picotool, sam-ba, teensy-cli, jlink, etc.).
   - Board memory options (Flash size, Flash mode, PSRAM, USB CDC on boot).
4. For serial upload targets, `--upload-port` is automatically provided.
5. For native programmer targets (e.g. ST-Link, Picotool, CMSIS-DAP), upload proceeds directly without requiring an artificial COM port.
6. Post-upload reset pulses are tailored to the exact microcontroller family (DTR/RTS auto-reset, Optiboot pulse, ARM/RISC-V hardware reset, or 1200-baud touch reset).
