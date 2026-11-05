# Arduino-inspired editor palette

The offline editor adapts the official Arduino IDE
[Light palette](https://github.com/arduino/arduino-ide/blob/615de517ffa5ae828b3109752316b2129363d00a/arduino-ide-extension/src/browser/data/default.color-theme.json)
and
[Dark palette](https://github.com/arduino/arduino-ide/blob/615de517ffa5ae828b3109752316b2129363d00a/arduino-ide-extension/src/browser/data/dark.color-theme.json)
at commit `615de517ffa5ae828b3109752316b2129363d00a`. It is an adaptation,
not an exact reproduction or an official Arduino theme.

The native page and HTML/body fall back to the workspace's `Theme.PALETTES.BG_DARKEST`:
Light `#f1f7fc`, Default `#101b2a` and Solarized `#001b22`. Over that, the page paints its
window's scene and a smoked well in `--reading`; Monaco's canvas and gutter are fully
transparent. Every ink is judged at the well's hardest point, `glass_worst` in
`main/core/theme.py`: Light `#edf2f7`, Default `#233345` and Solarized `#12343b`.
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

| Role | Upstream Light | Adapted Light | Upstream Dark | Adapted Dark |
| --- | --- | --- | --- | --- |
| Comments | `#95a5a6cc` | `#606d6f` | `#7f8c8d` | `#959fa0` |
| Types/storage | `#00979d` | `#00767b` | `#0ca1a6` | `#22b0b5` |
| Functions | `#d35400` | `#b74700` | `#f39c12` | `#f39c12` |
| Control keywords | `#728e00` | `#5d7300` | `#c586c0` | `#c688c1` |
| Directives | `#9e846d` | `#7b6654` | `#569cd6` | `#65a2d8` |

These hold at least 4.5:1 at the hardest point of the smoked well in each theme, so they sit a
little further from the surface than they would on a flat fill.
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
selection, font settings, focus and undo/redo, that Monaco paints nothing over the
glass, and each ink's contrast at the well's hardest point. Optional reports and captures stay in `temp/`.
