"""Theme-aware foreground colors for serial and build log text."""
from __future__ import annotations

from main.qt.theme import get_palette


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


def themed_log_colors(theme_name: str = "default") -> dict[str, str]:
    """Map log semantics to readable colors from the active workspace theme."""
    palette = get_palette(theme_name)
    background = palette["BG_DARKEST"]

    def readable(token: str, fallback: str) -> str:
        color = palette[token]
        return color if contrast_ratio(color, background) >= 4.5 else fallback

    colors = {
        "normal": palette["TEXT"],
        "bold": palette["TEXT_BRIGHT"],
        "timestamp": palette["TEXT_DIM"],
        "dim": palette["TEXT_DIM"],
        "info": readable("BLUE", "#0969da"),
        "success": readable("GREEN", "#1a7f37"),
        "warning": readable("YELLOW", "#7d4e00"),
        "error": readable("RED", "#ff7b75"),
        "system": readable("CYAN", "#0969da"),
        "header": readable("CYAN", "#0969da"),
        "magenta": readable("MAGENTA", "#f05b9c"),
        "connecting_magenta": readable("MAGENTA", "#f05b9c"),
        "sent": readable("MAGENTA", "#f05b9c"),
        "magenta_bold_lg": readable("MAGENTA", "#f05b9c"),
        "purple_info": readable("MAGENTA", "#f05b9c"),
        "orange": readable("ORANGE", "#ff8a54"),
        "port_highlight": readable("MAGENTA", "#f05b9c"),
        "purple": readable("PURPLE", "#aeb2ff"),
        "purple_dim": readable("PURPLE_DIM", "#aeb2ff"),
        "purple_header": readable("PURPLE", "#aeb2ff"),
        "purple_value": palette["TEXT_BRIGHT"],
        "success_bold_lg": readable("GREEN", "#1a7f37"),
        "severe_alert": readable("RED", "#ff7b75"),
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
