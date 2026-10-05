"""Framework-neutral readable theme colors for logs and terminal renderers."""
from __future__ import annotations

from main.core.theme import get_palette


def _luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(foreground: str, background: str) -> float:
    """Return WCAG relative-luminance contrast for two hex colors."""
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def themed_log_colors(theme_name: str = "default", *, background: str | None = None) -> dict[str, str]:
    """Map log semantics to readable colors from the active workspace theme."""
    palette = get_palette(theme_name)
    background = background or palette["BG_DARKEST"]

    def readable(token: str, dark: str, light: str) -> str:
        for color in (palette[token], dark, light, palette["TEXT"], "#000000", "#ffffff"):
            if contrast_ratio(color, background) >= 4.5:
                return color
        return palette["TEXT"]

    colors = {
        "normal": readable("TEXT", "#24292f", "#e3edf6"),
        "bold": readable("TEXT_BRIGHT", "#1f2328", "#f6fbff"),
        "timestamp": readable("TEXT_DIM", "#57606a", "#adc0d3"),
        "dim": readable("TEXT_DIM", "#57606a", "#adc0d3"),
        "info": readable("BLUE", "#0969da", "#61afef"),
        "success": readable("GREEN", "#1a7f37", "#5ccc6e"),
        "warning": readable("YELLOW", "#7d4e00", "#e8b83a"),
        "error": readable("RED", "#cf222e", "#ff7b75"),
        "system": readable("CYAN", "#0969da", "#80d9ce"),
        "header": readable("CYAN", "#0969da", "#80d9ce"),
        "magenta": readable("MAGENTA", "#8250df", "#f05b9c"),
        "connecting_magenta": readable("MAGENTA", "#8250df", "#f05b9c"),
        "sent": readable("MAGENTA", "#8250df", "#f05b9c"),
        "magenta_bold_lg": readable("MAGENTA", "#8250df", "#f05b9c"),
        "purple_info": readable("MAGENTA", "#8250df", "#f05b9c"),
        "orange": readable("ORANGE", "#bc4c00", "#ff8a54"),
        "port_highlight": readable("MAGENTA", "#8250df", "#f05b9c"),
        "purple": readable("PURPLE", "#6639ba", "#aeb2ff"),
        "purple_dim": readable("PURPLE_DIM", "#542c9f", "#aeb2ff"),
        "purple_header": readable("PURPLE", "#6639ba", "#aeb2ff"),
        "purple_value": readable("TEXT_BRIGHT", "#1f2328", "#f6fbff"),
        "success_bold_lg": readable("GREEN", "#1a7f37", "#5ccc6e"),
        "severe_alert": readable("RED", "#cf222e", "#ff7b75"),
    }
    return colors


def themed_ansi_colors(theme_name: str = "default") -> dict[int, str]:
    """Return readable semantic equivalents for standard ANSI foregrounds."""
    colors = themed_log_colors(theme_name)
    return {
        30: colors["dim"],
        31: colors["error"],
        32: colors["success"],
        33: colors["warning"],
        34: colors["info"],
        35: colors["magenta"],
        36: colors["system"],
        37: colors["normal"],
        90: colors["dim"],
        91: colors["error"],
        92: colors["success"],
        93: colors["warning"],
        94: colors["info"],
        95: colors["magenta"],
        96: colors["system"],
        97: colors["bold"],
    }


def themed_terminal_colors(theme_name: str = "default") -> dict[str, str]:
    """Share readable xterm colors between native Windows and POSIX panels."""
    palette = get_palette(theme_name)
    ansi = themed_ansi_colors(theme_name)
    colors = {
        "background": palette["BG_DARKEST"],
        "foreground": themed_log_colors(theme_name)["normal"],
        "cursor": palette["TEXT_BRIGHT"],
        "cursorAccent": palette["BG_DARKEST"],
        "selectionBackground": palette["BG_HOVER"],
        "selectionForeground": themed_log_colors(theme_name, background=palette["BG_HOVER"])["bold"],
    }
    for index, name in enumerate(("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")):
        colors[name] = ansi[30 + index]
        colors["bright" + name.title()] = ansi[90 + index]
    return colors
