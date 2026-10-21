# Arduino-inspired editor palette

The offline editor adapts the official Arduino IDE
[Light palette](https://github.com/arduino/arduino-ide/blob/615de517ffa5ae828b3109752316b2129363d00a/arduino-ide-extension/src/browser/data/default.color-theme.json)
and
[Dark palette](https://github.com/arduino/arduino-ide/blob/615de517ffa5ae828b3109752316b2129363d00a/arduino-ide-extension/src/browser/data/dark.color-theme.json)
at commit `615de517ffa5ae828b3109752316b2129363d00a`. It is an adaptation,
not an exact reproduction or an official Arduino theme.

Light retains the white editor surface, `#4e5b61` foreground, `#434f54`
identifiers/punctuation and `#005c5f` strings/constants. Default uses Arduino's
`#1f272a` Dark surface and `#dae3e3` foreground, with orange `#f39c12`
functions, teal `#0ca1a6` types, pale teal `#7fcbcd` strings/constants/numbers,
mauve `#c586c0` control keywords and blue `#569cd6` directives. Solarized retains
the application's `#002b36` surface and uses the compatible Dark syntax colors;
Arduino does not provide this Solarized adaptation.

Small-text readability adjustments are explicit:

| Role | Upstream Light | Adapted Light | Contrast on white |
| --- | --- | --- | --- |
| Comments | `#95a5a6cc` | `#657375` | 4.93:1 |
| Types/storage | `#00979d` | `#007f84` | 4.81:1 |
| Functions | `#d35400` | `#c94f00` | 4.56:1 |
| Control keywords | `#728e00` | `#657c00` | 4.73:1 |
| Directives | `#9e846d` | `#826c59` | 4.95:1 |

Dark comments change from `#7f8c8d` to `#839091`: 4.60:1 on Dark and 4.55:1 on
Solarized. Numeric literals use each palette's constant color, including inside
preprocessor expressions, rather than the upstream dark numeric-preprocessor
override. Normal punctuation replaces inherited rainbow bracket colors while
retaining bracket matching. Theme selection does not change the saved font
family or size, editor models, undo history or source tabs.

The upstream theme names TextMate scopes; bundled Monaco uses Monarch tokens.
A cached grammar adaptation adds call-name and known type/constant categories
to the existing C/C++ identifier rules. Existing keywords, includes, comments,
strings and C++ raw-string states remain in place. C keeps its separate keyword
list and standard types; Arduino names apply to the C++/`.ino` grammar. This is
lexical highlighting, not type inference, board validation or library discovery.
There is no whole-file scan per edit and no additional language service.

`direct/verify_arduino_palette.py` checks actual rendered `.ino`, C and C++ spans
in every theme, including comments/raw strings and C/C++ distinctions. Its
in-memory fixture also checks retained dirty text, model versions, tab order,
selection, font settings, focus and undo/redo, plus agreement between native
page and HTML/editor backgrounds. Optional reports and captures stay in `temp/`.
