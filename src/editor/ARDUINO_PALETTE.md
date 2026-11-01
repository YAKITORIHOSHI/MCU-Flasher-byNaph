# Arduino-inspired editor palette

The offline editor adapts the official Arduino IDE
[Light palette](https://github.com/arduino/arduino-ide/blob/615de517ffa5ae828b3109752316b2129363d00a/arduino-ide-extension/src/browser/data/default.color-theme.json)
and
[Dark palette](https://github.com/arduino/arduino-ide/blob/615de517ffa5ae828b3109752316b2129363d00a/arduino-ide-extension/src/browser/data/dark.color-theme.json)
at commit `615de517ffa5ae828b3109752316b2129363d00a`. It is an adaptation,
not an exact reproduction or an official Arduino theme.

Reading surfaces follow the native workspace's `Theme.PALETTES.BG_DARKEST`:
Light `#f1f7fc`, Default `#101b2a` and Solarized `#001b22`. The native page,
HTML/body, loading state, Monaco canvas and gutter use the same opaque color.
Source tabs and scrollbars follow their corresponding native theme tokens.
Theme and saved font size are supplied before the first editor construction;
changing themes preserves the existing source models.

Light retains the `#4e5b61` foreground, `#434f54`
identifiers/punctuation and `#005c5f` strings/constants. Default uses Arduino's
`#dae3e3` foreground, with orange `#f39c12`
functions, teal `#0ca1a6` types, pale teal `#7fcbcd` strings/constants/numbers,
mauve `#c586c0` control keywords and blue `#569cd6` directives. Solarized retains
its native reading surface and uses the compatible Dark syntax colors;
Arduino does not provide this Solarized adaptation.

Small-text readability adjustments are explicit:

| Role | Upstream Light | Adapted Light | Contrast on native Light |
| --- | --- | --- | --- |
| Comments | `#95a5a6cc` | `#657375` | 4.56:1 |
| Types/storage | `#00979d` | `#007a7f` | 4.76:1 |
| Functions | `#d35400` | `#bf4b00` | 4.61:1 |
| Control keywords | `#728e00` | `#607700` | 4.70:1 |
| Directives | `#9e846d` | `#826c59` | 4.59:1 |

Dark comments retain the adapted `#839091` rather than upstream `#7f8c8d`.
The light type/function/control inks are slightly deeper than the earlier white-
surface adaptation so they retain at least 4.5:1 contrast on native Light.
Numeric literals use each palette's constant color, including inside
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
