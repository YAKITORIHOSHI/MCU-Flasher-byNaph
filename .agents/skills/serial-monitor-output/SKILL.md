---
name: serial-monitor-output
description: "How MCU Flasher's Serial Monitor shows a sketch's Serial output: the baud it follows, line endings, clearing, and the ANSI color palette (30-37/90-97 text, 40-47/100-107 backgrounds). Use when writing Serial diagnostics or colored output."
---

# Serial Monitor output

- **Baud**: after an upload the monitor follows the sketch's `Serial.begin(rate)`. Keep one literal rate in a root source (for example `Serial.begin(115200);`). The monitor offers exactly the Arduino IDE's rates: 300, 600, 750, 1200, 2400, 4800, 9600, 19200, 31250, 38400, 57600, 74880, 115200, 230400, 250000, 460800, 500000, 921600, 1000000 and 2000000; use one of them.
- **Input**: the Send bar adds the line ending the user picked (none, newline, carriage return or both). Read with `Serial.readStringUntil('\n')` and `trim()` the result.
- **Timestamps** are added by the monitor when the user turns them on; do not print your own unless asked.
- **Clearing**: `\033[2J` clears the monitor's view when that option is on.

## ANSI colors
The monitor paints ANSI SGR sequences (`ESC [ codes m`) with a real 16-color palette. Normal and bright colors stay distinct, a black or white background is black or white, and text is kept readable on the user's theme.

| Codes | Meaning |
|---|---|
| 0 | reset everything |
| 1, 2, 3, 4, 7, 9 | bold, dim, italic, underline, inverse, strikethrough (22-29 undo them) |
| 30-37 | text: black, red, green, yellow, blue, magenta, cyan, white |
| 90-97 | bright text in the same order |
| 40-47, 100-107 | normal and bright backgrounds |
| 39, 49 | default text, default background |
| 38;5;n / 48;5;n, 38;2;r;g;b / 48;2;r;g;b | 256-color and truecolor text / background |

Helpers for any `Print` (Serial, Serial1, ...):

```cpp
constexpr uint8_t C_RST = 0;
constexpr uint8_t C_RED = 31,  C_GREEN = 32,  C_YELLOW = 33,  C_BLUE = 34;   // +60: bright (91-94)
constexpr uint8_t BG_BLUE = 44, BG_RED = 41;                                 // +60: bright (101, 104)

inline void ansiSet(Print &out, uint8_t code) { out.print("\033["); out.print(code); out.print('m'); }
inline void ansiSet(Print &out, uint8_t fg, uint8_t bg) {
  out.print("\033["); out.print(fg); out.print(';'); out.print(bg); out.print('m');
}
inline void ansiReset(Print &out) { ansiSet(out, C_RST); }

// ansiSet(Serial, 92); Serial.println(F("OK")); ansiReset(Serial);
// ansiSet(Serial, 97, 44) -> ESC[97;44m
```

End every colored line with a reset (`ESC[0m`) so later output, and the next boot log, start plain. On AVR boards wrap literal text in `F()` to keep it out of RAM.

---
*Generated automatically by MCU Flash GUI for this sketch's coding assistants; it is rewritten when the sketch opens.*
