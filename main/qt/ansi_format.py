"""Paint an ANSI SGR style onto a Qt text format for the workspace's reading surfaces."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QTextCharFormat

from main.core.ansi_sgr import SgrStyle, resolve_colors


def reading_surface(theme_name: str) -> str:
    """The hardest point of a reading surface: inks are judged against it, as everywhere else."""
    from main.core.theme import glass_worst
    return glass_worst(theme_name)["reading"]


def styled_format(base: QTextCharFormat, style: SgrStyle | None, theme_name: str) -> QTextCharFormat:
    """A copy of ``base`` with the style's colors, weight, slant and lines applied."""
    fmt = QTextCharFormat(base)
    if style is None or style.plain:
        return fmt
    default_fg = base.foreground().color().name() if base.hasProperty(QTextCharFormat.Property.ForegroundBrush) else None
    surface = reading_surface(theme_name)
    if default_fg is None:
        from main.core.log_colors import themed_log_colors
        default_fg = themed_log_colors(theme_name)["normal"]
    if style.fg or style.bg or style.inverse or style.dim:
        foreground, background = resolve_colors(style, theme_name, default_fg, surface)
        fmt.setForeground(QColor(foreground))
        if background:
            fmt.setBackground(QColor(background))
    if style.bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if style.italic:
        fmt.setFontItalic(True)
    if style.underline:
        fmt.setFontUnderline(True)
    if style.strike:
        fmt.setFontStrikeOut(True)
    return fmt
