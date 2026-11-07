"""ANSI Select Graphic Rendition for the workspace's log views; toolkit-free.

Terminals (xterm) render SGR themselves. The Serial Monitor and Build Console are
document views, so they keep an ``SgrStyle`` beside each run of text and turn it into
theme colors only when painting. A theme change therefore recolors retained output.

Supported: reset, bold, dim, italic, underline (including ``4:n`` and 21), inverse,
strikethrough, their resets, the 8/16 standard and bright colors, 256-color
(``38;5;n``/``48;5;n``) and truecolor (``38;2;r;g;b``) in both semicolon and colon
forms, and default foreground/background (39/49). Conceal (8) is deliberately not
honoured: hiding device output would defeat a debugging monitor. Unknown or malformed
parameters are skipped, never thrown. Every painted foreground keeps at least 4.5:1
contrast against what it is painted on, like xterm's minimumContrastRatio.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

_MAX_PARAMETERS = 64


@dataclass(frozen=True)
class SgrStyle:
    fg: tuple | None = None      # ("i", 0..255) or ("rgb", r, g, b)
    bg: tuple | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    inverse: bool = False
    strike: bool = False

    @property
    def plain(self) -> bool:
        return self == PLAIN


PLAIN = SgrStyle()


def _numbers(text: str) -> list[int | None]:
    values: list[int | None] = []
    for part in text.split(":"):
        if part == "":
            values.append(None)
        elif part.isdigit() and len(part) <= 6:
            values.append(int(part))
        else:
            return []
    return values


def _color(values: list[int | None], start: int) -> tuple[tuple | None, int]:
    """Read ``5;n`` or ``2;r;g;b`` after a 38/48; return (color, items consumed)."""
    if start >= len(values):
        return None, 0
    kind = values[start]
    if kind == 5 and start + 1 < len(values):
        index = values[start + 1]
        return (("i", index) if index is not None and 0 <= index <= 255 else None), 2
    if kind == 2 and start + 3 < len(values):
        rgb = values[start + 1:start + 4]
        if all(value is not None and 0 <= value <= 255 for value in rgb):
            return ("rgb", *rgb), 4
        return None, 4
    return None, 1


def apply_sgr(style: SgrStyle, parameters: str) -> SgrStyle:
    """The style after one ``ESC [ parameters m`` sequence."""
    fields = parameters.split(";")[:_MAX_PARAMETERS] if parameters else [""]
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if ":" in field:
            # Colon sub-parameters keep a color or underline style inside one field.
            values = _numbers(field)
            if not values:
                continue
            head = values[0]
            if head in (38, 48, 58):
                # 38:2::r:g:b carries an empty color-space slot before the channels.
                tail = values[1:]
                if tail[:1] == [2] and len(tail) == 5:
                    tail = [2, *tail[2:]]
                color, _ = _color(tail, 0)
                if head == 38:
                    style = replace(style, fg=color)
                elif head == 48:
                    style = replace(style, bg=color)
            elif head == 4:
                style = replace(style, underline=(values[1:2] != [0]))
            continue
        if field == "":
            code = 0
        elif field.isdigit() and len(field) <= 6:
            code = int(field)
        else:
            continue
        if code in (38, 48, 58):
            numbers = [int(value) if value.isdigit() and len(value) <= 6 else None for value in fields[index:index + 4]]
            color, used = _color(numbers, 0)
            index += used
            if code == 38:
                style = replace(style, fg=color)
            elif code == 48:
                style = replace(style, bg=color)
        elif code == 0:
            style = PLAIN
        elif code == 1:
            style = replace(style, bold=True)
        elif code == 2:
            style = replace(style, dim=True)
        elif code == 3:
            style = replace(style, italic=True)
        elif code in (4, 21):
            style = replace(style, underline=True)
        elif code == 7:
            style = replace(style, inverse=True)
        elif code == 9:
            style = replace(style, strike=True)
        elif code == 22:
            style = replace(style, bold=False, dim=False)
        elif code == 23:
            style = replace(style, italic=False)
        elif code == 24:
            style = replace(style, underline=False)
        elif code == 27:
            style = replace(style, inverse=False)
        elif code == 29:
            style = replace(style, strike=False)
        elif 30 <= code <= 37:
            style = replace(style, fg=("i", code - 30))
        elif code == 39:
            style = replace(style, fg=None)
        elif 40 <= code <= 47:
            style = replace(style, bg=("i", code - 40))
        elif code == 49:
            style = replace(style, bg=None)
        elif 90 <= code <= 97:
            style = replace(style, fg=("i", code - 90 + 8))
        elif 100 <= code <= 107:
            style = replace(style, bg=("i", code - 100 + 8))
    return style


def xterm_rgb(index: int) -> tuple[int, int, int]:
    """The xterm 256-color table: 16 system colors, a 6×6×6 cube, then 24 grays."""
    system = ((0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205),
              (0, 205, 205), (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0),
              (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255))
    if index < 16:
        return system[index]
    if index < 232:
        index -= 16
        levels = (0, 95, 135, 175, 215, 255)
        return levels[index // 36], levels[index // 6 % 6], levels[index % 6]
    gray = 8 + (index - 232) * 10
    return gray, gray, gray


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, int(channel))):02x}" for channel in rgb)


def _system_color(index: int, theme_name: str) -> str:
    """The theme's readable stand-ins for the 16 system colors, as the terminals use."""
    from main.core.log_colors import themed_ansi_colors
    ansi = themed_ansi_colors(theme_name)
    return ansi[30 + index] if index < 8 else ansi[90 + index - 8]


def _rgb_of(color: tuple, theme_name: str) -> str:
    if color[0] == "i":
        index = color[1]
        return _system_color(index, theme_name) if index < 16 else _hex(xterm_rgb(index))
    return _hex(color[1:4])


def readable_on(foreground: str, background: str, minimum: float = 4.5) -> str:
    """Move a color toward white or black, keeping its hue, until it reads on the background."""
    from main.core.log_colors import _luminance, _mixed, contrast_ratio
    if contrast_ratio(foreground, background) >= minimum:
        return foreground
    target = "#ffffff" if _luminance(background) < .18 else "#000000"
    for step in range(1, 21):
        mixed = _mixed(foreground, target, step / 20)
        if contrast_ratio(mixed, background) >= minimum:
            return mixed
    return target


def resolve_colors(style: SgrStyle, theme_name: str, default_fg: str, surface: str) -> tuple[str, str | None]:
    """(foreground, background or None) in hex for one styled run on a reading surface."""
    foreground = _rgb_of(style.fg, theme_name) if style.fg else default_fg
    background = _rgb_of(style.bg, theme_name) if style.bg else None
    if style.inverse:
        foreground, background = (background or surface), foreground
    painted_on = background or surface
    if style.dim:
        from main.core.log_colors import _mixed
        foreground = readable_on(_mixed(foreground, painted_on, .35), painted_on, 3.0)
    else:
        foreground = readable_on(foreground, painted_on)
    return foreground, background


class SgrStream:
    """Incremental CSI filter for append-only views: keeps text, returns styled runs.

    Bounded like the serial parser: an unfinished or malformed escape longer than 256
    characters is released as text instead of swallowing later output. Non-SGR CSI and
    OSC/DCS strings are dropped, as before; only ``m`` changes the style.
    """

    def __init__(self):
        self.style = PLAIN
        self._pending = ""
        self._state = "text"

    def reset(self, *, style: bool = True) -> None:
        self._pending, self._state = "", "text"
        if style:
            self.style = PLAIN

    def feed(self, text: str) -> list[tuple[str, SgrStyle]]:
        if self._state == "text" and "\x1b" not in text:
            return [(text, self.style)] if text else []
        runs: list[tuple[str, SgrStyle]] = []
        piece: list[str] = []

        def emit():
            if piece:
                runs.append(("".join(piece), self.style))
                piece.clear()

        for char in text:
            if self._state == "text":
                if char == "\x1b":
                    self._pending, self._state = char, "escape"
                else:
                    piece.append(char)
                continue
            self._pending += char
            if len(self._pending) > 256:
                piece.append(self._pending)
                self._pending, self._state = "", "text"
                continue
            if self._state == "escape":
                self._state = {"[": "csi", "]": "string", "P": "string", "^": "string", "_": "string"}.get(char, "text")
                if self._state == "text":
                    self._pending = ""
            elif self._state == "csi":
                if "@" <= char <= "~":
                    if char == "m":
                        emit()
                        self.style = apply_sgr(self.style, self._pending[2:-1])
                    self._pending, self._state = "", "text"
                elif not (" " <= char <= "?"):
                    piece.append(self._pending)
                    self._pending, self._state = "", "text"
            elif self._state == "string":
                if char == "\x07":
                    self._pending, self._state = "", "text"
                elif char == "\x1b":
                    self._state = "string_end"
            elif self._state == "string_end":
                self._state = "text" if char == "\\" else "string"
                if self._state == "text":
                    self._pending = ""
        emit()
        return runs
