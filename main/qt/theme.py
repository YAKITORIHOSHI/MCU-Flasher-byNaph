#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.theme — Qt stylesheet generator for MCU Flasher by Naph.

Converts the framework-neutral Theme.* color constants from
main.core.theme into a complete QApplication stylesheet.
Supports "default" (Glass Smoked Dark), "solarized_dark" (Solarized Dark),
and "light" (Glass Frosted Light), with static glass tab surfaces.
"""
from __future__ import annotations

from pathlib import Path

from main.core.theme import get_palette

# Resolve project root for font paths
_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent.parent
_icons_dir = _project_root / "src" / "assets" / "icons"
_icon_checked = (_icons_dir / "checkbox_checked.svg").as_posix()
_icon_checked_dim = (_icons_dir / "checkbox_checked_disabled.svg").as_posix()
_icon_arrow = (_icons_dir / "chevron-down.svg").as_posix()


def install_no_focus_lines(app=None) -> bool:
    """Controls work by mouse and shortcut without taking focus or drawing focus lines.

    Text fields, lists, the editor and terminals keep keyboard focus. Clicking a
    button therefore leaves the cursor where the user was typing. A style polish
    runs once per widget, so this adds no per-event or per-paint work; the
    stylesheet's universal ``outline: none`` removes the remaining focus rectangles.
    """
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QApplication, QComboBox,
                                   QProxyStyle, QTabBar, QTabWidget)

    app = app or QApplication.instance()
    if app is None or app.property("mcuNoFocusLinesInstalled"):
        return False

    class NoFocusLineStyle(QProxyStyle):
        def polish(self, target):
            if (isinstance(target, (QAbstractButton, QAbstractSlider, QTabBar, QTabWidget))
                    or (isinstance(target, QComboBox) and not target.isEditable())):
                target.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if isinstance(target, QComboBox) and not getattr(target, "_mcu_glass_popup_pending", False):
                target._mcu_glass_popup_pending = True
                from main.qt.combo_popup import install_combo_popup
                QTimer.singleShot(0, target, lambda: install_combo_popup(target))
            return super().polish(target)

    # The stylesheet draws nearly every control, and each native style measures and fills
    # in the rest differently: macOS clipped checkbox labels, and Windows 11 drew plain frame
    # lines in the text color across two rows. One base style means one result on every host.
    app.setStyle(NoFocusLineStyle("Fusion"))
    app.setProperty("mcuNoFocusLinesInstalled", True)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Font registration
# ─────────────────────────────────────────────────────────────────────────────

def register_fonts() -> None:
    """Register the UI weights once per Qt application, without recursive scans."""
    try:
        # pyrefly: ignore [missing-import]
        from PySide6.QtGui import QFontDatabase, QGuiApplication
        app = QGuiApplication.instance()
        if app is None or app.property("mcuFontsRegistered"):
            return
        fonts_dir = _project_root / "src" / "fonts" / "Montserrat"
        for weight in ("Regular", "Medium", "SemiBold", "Bold", "Italic"):
            for directory in (fonts_dir / "static", fonts_dir):
                ttf = directory / f"Montserrat-{weight}.ttf"
                if ttf.is_file():
                    QFontDatabase.addApplicationFont(str(ttf))
                    break
        app.setProperty("mcuFontsRegistered", True)
    except Exception:
        pass


def group_title_room(widget=None) -> int:
    """Logical pixels of margin above a QGroupBox rim that hold its whole title.

    Titles are placed in that margin. When it is shorter than the title, the rim runs
    through the text, and a translucent glass card cannot mask it. The title font is the
    stylesheet's 11px semibold Montserrat; Qt metrics are already logical pixels.
    """
    try:
        from PySide6.QtGui import QFont, QFontMetrics
        font = QFont(widget.font()) if widget is not None else QFont("Montserrat")
        font.setFamilies(["Montserrat", "Segoe UI", "Ubuntu", "sans-serif"])
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        return max(20, QFontMetrics(font).height() + 6)
    except Exception:
        return 22


# ─────────────────────────────────────────────────────────────────────────────
# Stylesheet builder
# ─────────────────────────────────────────────────────────────────────────────

def _translucent(color: str, alpha: int) -> str:
    value = color.lstrip("#")
    return f"rgba({int(value[:2], 16)}, {int(value[2:4], 16)}, {int(value[4:6], 16)}, {alpha})"


def _lit(top: str, bottom: str) -> str:
    return f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {top}, stop:1 {bottom})"


def css_color(value: str) -> str:
    """A Qt stylesheet color for a web page: Qt's alpha runs to 255, CSS's to 1."""
    if not value.startswith("rgba("):
        return value
    red, green, blue, alpha = (part.strip() for part in value[5:-1].split(","))
    return f"rgba({red}, {green}, {blue}, {int(alpha) / 255:.3f})"


def glass_tokens(theme_mode: str = "default") -> dict[str, str]:
    """Fills for surfaces over the window's scene. Everything here is translucent but the chip."""
    pal = get_palette(theme_mode)
    bright, cyan = pal.get("TEXT_BRIGHT", "#ffffff"), pal.get("CYAN", "#00d2ff")
    deepest = pal.get("BG_DARKEST", "#0d1117")
    values = {"selected": _translucent(cyan, 44), "rim": _translucent(bright, 46),
              "selection": _translucent(cyan, 76), "hover": _translucent(bright, 22)}
    if theme_mode == "light":
        values.update(glass="rgba(255, 255, 255, 96)", glass_highlight="rgba(255, 255, 255, 170)",
                      reading="rgba(255, 255, 255, 176)", reading_alt="rgba(255, 255, 255, 70)",
                      reading_rim="rgba(255, 255, 255, 210)", well="rgba(255, 255, 255, 190)",
                      chip=cyan, chip_ink="#ffffff", handle=_translucent(bright, 88),
                      button=_lit("rgba(255, 255, 255, 214)", "rgba(255, 255, 255, 138)"),
                      button_hover=_lit("rgba(255, 255, 255, 250)", "rgba(255, 255, 255, 190)"),
                      button_pressed="rgba(255, 255, 255, 96)", button_disabled="rgba(255, 255, 255, 72)",
                      button_rim=_translucent(bright, 58), disabled_ink="#5f6b76",
                      accent_top=_translucent(cyan, 66), accent_bottom=_translucent(cyan, 30),
                      accent_hover_top=_translucent(cyan, 96), accent_hover_bottom=_translucent(cyan, 50))
    else:
        values.update(glass="rgba(255, 255, 255, 10)", glass_highlight="rgba(255, 255, 255, 18)",
                      reading=_translucent(deepest, 170), reading_alt="rgba(255, 255, 255, 8)",
                      reading_rim="rgba(255, 255, 255, 34)", well=_translucent(deepest, 150),
                      chip=bright, chip_ink=deepest, handle=_translucent(bright, 74),
                      button=_lit("rgba(255, 255, 255, 22)", "rgba(255, 255, 255, 8)"),
                      button_hover=_lit("rgba(255, 255, 255, 34)", "rgba(255, 255, 255, 16)"),
                      button_pressed="rgba(0, 0, 0, 64)", button_disabled="rgba(255, 255, 255, 8)",
                      button_rim="rgba(255, 255, 255, 62)",
                      disabled_ink="#a3b6c9" if theme_mode == "default" else "#a8c2c8",
                      accent_top=_translucent(cyan, 72), accent_bottom=_translucent(cyan, 34),
                      accent_hover_top=_translucent(cyan, 104), accent_hover_bottom=_translucent(cyan, 60))
    return values


def build_stylesheet(theme_mode: str = "default") -> str:
    """Return a complete QApplication stylesheet for the given theme mode."""
    pal = get_palette(theme_mode)
    from src.modules.svg_icons import control_assets
    controls = control_assets(theme_mode)
    _icon_checked, _icon_checked_hover = controls["checked"], controls["hover"]
    _icon_checked_dim, _icon_arrow = controls["disabled"], controls["chevron"]

    bg_darkest  = pal.get("BG_DARKEST", "#0d1117")
    bg_dark     = pal.get("BG_DARK", "#151922")
    bg_mid      = pal.get("BG_MID", "#1c2333")
    bg_light    = pal.get("BG_LIGHT", "#243047")
    bg_hover    = pal.get("BG_HOVER", "#2a3a55")

    text        = pal.get("TEXT", "#e0e6ed")
    text_bright = pal.get("TEXT_BRIGHT", "#ffffff")
    text_dim    = pal.get("TEXT_DIM", "#8fa1b3")

    cyan        = pal.get("CYAN", "#00d2ff")
    cyan_dim    = pal.get("CYAN_DIM", "#1f7872")
    blue        = pal.get("BLUE", "#5ca4f0")
    green       = pal.get("GREEN", "#4ec994")
    yellow      = pal.get("YELLOW", "#f1c40f")
    orange      = pal.get("ORANGE", "#e67e22")
    red         = pal.get("RED", "#e74c3c")
    magenta     = pal.get("MAGENTA", "#c678dd")
    purple      = pal.get("PURPLE", "#9b59b6")

    border      = pal.get("BORDER", "#2d3748")
    border_lit  = pal.get("BORDER_LIT", cyan)

    btn_compile   = pal.get("BTN_COMPILE", "#1a5c3a")
    btn_compile_h = pal.get("BTN_COMPILE_H", "#216e46")
    btn_upload    = pal.get("BTN_UPLOAD", "#1a4a6e")
    btn_upload_h  = pal.get("BTN_UPLOAD_H", "#1f5a88")
    btn_stop      = pal.get("BTN_STOP", "#6e2020")
    btn_stop_h    = pal.get("BTN_STOP_H", "#882828")
    btn_clear     = pal.get("BTN_CLEAR", "#2d3748")
    btn_clear_h   = pal.get("BTN_CLEAR_H", "#3a4a60")
    btn_full      = pal.get("BTN_FULL", "#1a5c3a")
    btn_full_h    = pal.get("BTN_FULL_H", "#216e46")
    btn_monitor   = pal.get("BTN_MONITOR", "#1a7a70")
    btn_monitor_h = pal.get("BTN_MONITOR_H", "#219a8d")
    btn_dim       = pal.get("BTN_DIM", "#2a3342")
    btn_dim_h     = pal.get("BTN_DIM_H", "#3a4555")

    btn_disabled_bg     = "#e2e8f0" if theme_mode == "light" else pal.get("BG_DARK", "#11161f")
    btn_disabled_fg     = "#94a3b8" if theme_mode == "light" else "#4b5563"
    btn_disabled_border = "#cbd5e1" if theme_mode == "light" else "#1c2333"
    btn_blue            = "#0969da" if theme_mode == "light" else pal.get("BTN_BLUE", "#1a4f7c")
    btn_blue_h          = "#0858b8" if theme_mode == "light" else pal.get("BTN_BLUE_H", "#2563a6")
    btn_grey            = "#e2e8f0" if theme_mode == "light" else pal.get("BTN_GREY", "#283342")

    from src.modules.ui_palette import contrast_ratio, readable_foreground
    # The current row of an open combo list: the theme's deep accent, with whichever of the
    # theme's bright text or plain black/white actually reads on it.
    combo_selected = cyan_dim
    combo_selected_ink = (text_bright if contrast_ratio(text_bright, cyan_dim) >= 4.5
                          else readable_foreground(cyan_dim))

    title_room = group_title_room()

    translucent = _translucent
    glass_values = glass_tokens(theme_mode)
    lit = _lit
    selected, rim = glass_values["selected"], glass_values["rim"]
    glass, glass_highlight = glass_values["glass"], glass_values["glass_highlight"]
    well, disabled_ink = glass_values["well"], glass_values["disabled_ink"]
    reading, reading_alt = glass_values["reading"], glass_values["reading_alt"]
    reading_rim, selection = glass_values["reading_rim"], glass_values["selection"]
    chip, chip_ink = glass_values["chip"], glass_values["chip_ink"]
    handle, hover = glass_values["handle"], glass_values["hover"]
    from main.core.log_colors import glass_inks, themed_log_colors
    accent_on_sheet = glass_inks(theme_mode)["system"]
    accent_on_reading = themed_log_colors(theme_mode)["system"]
    button, button_hover = glass_values["button"], glass_values["button_hover"]
    button_pressed, button_disabled = glass_values["button_pressed"], glass_values["button_disabled"]
    button_rim = glass_values["button_rim"]
    accent_top, accent_bottom = glass_values["accent_top"], glass_values["accent_bottom"]
    accent_hover_top, accent_hover_bottom = glass_values["accent_hover_top"], glass_values["accent_hover_bottom"]
    glass_button_ids = ("btn-clean", "btn-reload", "btn-project", "btn-download", "btn-detach-editor",
                        "btn-toggle-editor", "btn-toggle-monitors", "btn-settings", "btn-ai-assistant",
                        "btn-copy-console", "btn-clear-console", "btn-serial-send", "btn-terminal-new",
                        "btn-terminal-restart", "btn-terminal-clear", "btn-terminal-open")
    accent_button_ids = ("btn-compile", "btn-upload", "btn-save", "btn-save-all")

    def buttons(ids, state: str, *, plain: bool = False) -> str:
        names = [f"QPushButton#{name}{state}" for name in ids]
        if plain:
            names += [f"QPushButton{state}", f"QDialog QPushButton{state}"]
        return ",\n".join(names)

    return f"""
/* ── Global ─────────────────────────────────────────────────────────────── */
* {{
    font-family: "Montserrat", "Segoe UI", "Ubuntu", sans-serif;
    font-size: 12px;
    outline: none;
}}

QMainWindow {{
    background: {bg_darkest};
    color: {text};
}}

QDialog {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {bg_mid}, stop:1 {bg_dark});
    color: {text};
}}

QWidget {{
    color: {text};
}}

QLabel {{
    background: transparent;
    color: {text};
}}

/* ── Toolbars ────────────────────────────────────────────────────────────── */
QToolBar {{
    background-color: {bg_dark};
    border: none;
    spacing: 4px;
    padding: 4px 8px;
}}

QToolBar#primary-toolbar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {glass_highlight}, stop:1 {glass});
    border: none;
    border-bottom: 1px solid {rim};
    padding: 4px 8px;
    spacing: 4px;
}}

QToolBar#controls-toolbar {{
    background: {glass};
    border: none;
    padding: 0px;
}}

QToolBar#primary-toolbar[shortScreen="true"],
QToolBar#controls-toolbar[shortScreen="true"] {{
    padding-top: 0px;
    padding-bottom: 0px;
}}

QWidget#controls-bar {{
    background: transparent;
}}

QFrame#toolbar-sep {{
    border: none;
    border-left: 1px solid {border};
    margin: 4px 5px;
}}

QWidget#console-header,
QWidget#serial-header,
QWidget#compat-header,
QFrame#notif-header,
QFrame#terminal-header,
QFrame#syntax-header {{
    background-color: {bg_mid};
    border-bottom: 1px solid {border};
}}

QPlainTextEdit#build-console,
QPlainTextEdit#serial-console,
QPlainTextEdit#compat-console,
QTextBrowser#notif-browser {{
    background-color: {bg_darkest};
    color: {text};
    border: none;
}}

QTableWidget#syntax-table {{
    background-color: {bg_darkest};
    alternate-background-color: {bg_dark};
    color: {text};
    border: none;
}}
QTableWidget#syntax-table::item {{
    padding: 4px 8px;
}}
QTableWidget#syntax-table::item:selected {{
    background-color: {bg_hover};
    color: {text_bright};
}}

/* ── Buttons ─────────────────────────────────────────────────────────────── */
QPushButton {{
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
}}

QPushButton:enabled {{
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
}}

QPushButton:enabled:hover {{
    background-color: {btn_clear_h};
    border: 1px solid {border_lit};
    color: {text_bright};
}}

QPushButton:enabled:pressed {{
    background-color: {bg_mid};
    border: 1px solid {cyan};
}}

QPushButton:checked {{
    background-color: {cyan_dim};
    border: 1px solid {cyan};
    color: #ffffff;
}}

QPushButton:checked:hover {{
    background-color: {cyan};
    border: 1px solid {border_lit};
    color: #ffffff;
}}

QPushButton:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-compile:enabled {{
    background-color: {btn_compile};
    color: #ffffff;
    border: 1px solid {green};
}}
QPushButton#btn-compile:enabled:hover {{
    background-color: {btn_compile_h};
    border: 1px solid #34d399;
    color: #ffffff;
}}
QPushButton#btn-compile:enabled:pressed {{
    background-color: {btn_compile};
    border: 1px solid #34d399;
}}
QPushButton#btn-compile:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-upload:enabled {{
    background-color: {btn_upload};
    color: #ffffff;
    border: 1px solid {cyan};
}}
QPushButton#btn-upload:enabled:hover {{
    background-color: {btn_upload_h};
    border: 1px solid #38bdf8;
    color: #ffffff;
}}
QPushButton#btn-upload:enabled:pressed {{
    background-color: {btn_upload};
    border: 1px solid #38bdf8;
}}
QPushButton#btn-upload:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-stop:enabled {{
    background-color: {btn_stop};
    color: #ffffff;
    border: 1px solid {red};
}}
QPushButton#btn-stop:enabled:hover {{
    background-color: {btn_stop_h};
    border: 1px solid #f87171;
    color: #ffffff;
}}
QPushButton#btn-stop:enabled:pressed {{
    background-color: {btn_stop};
    border: 1px solid #f87171;
}}
QPushButton#btn-stop:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-clean:enabled {{
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
}}
QPushButton#btn-clean:enabled:hover {{
    background-color: {btn_clear_h};
    border: 1px solid {border_lit};
    color: {text_bright};
}}
QPushButton#btn-clean:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-save:enabled {{
    background-color: {btn_full};
    color: #ffffff;
    border: 1px solid {cyan_dim};
}}
QPushButton#btn-save:enabled:hover {{
    background-color: {btn_full_h};
    border: 1px solid {border_lit};
    color: #ffffff;
}}
QPushButton#btn-save:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-save-all:enabled {{
    background-color: {btn_upload};
    color: #ffffff;
    border: 1px solid {cyan_dim};
}}
QPushButton#btn-save-all:enabled:hover {{
    background-color: {btn_upload_h};
    border: 1px solid {border_lit};
    color: #ffffff;
}}
QPushButton#btn-save-all:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-reload:enabled {{
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
}}
QPushButton#btn-reload:enabled:hover {{
    background-color: {btn_clear_h};
    border: 1px solid {border_lit};
    color: {text_bright};
}}
QPushButton#btn-reload:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-project:enabled {{
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
}}
QPushButton#btn-project:enabled:hover {{
    background-color: {btn_clear_h};
    border: 1px solid {border_lit};
    color: {text_bright};
}}
QPushButton#btn-project:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-download:enabled {{
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
}}
QPushButton#btn-download:enabled:hover {{
    background-color: {btn_clear_h};
    border: 1px solid {border_lit};
    color: {text_bright};
}}
QPushButton#btn-download:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-detach-editor:enabled {{
    background-color: {bg_hover};
    color: {text_bright};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 10.5px;
    font-weight: 600;
}}
QPushButton#btn-detach-editor:enabled:hover {{
    background-color: {bg_light};
    border: 1px solid {border_lit};
    color: {cyan};
}}
QPushButton#btn-detach-editor:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-search-board:enabled {{
    background-color: {btn_blue};
    color: #ffffff;
    border: 1px solid {border_lit};
    border-radius: 4px;
    padding: 0px;
    text-align: center;
    font-size: 12px;
    font-family: 'Segoe UI Emoji', 'Segoe UI Symbol', 'Segoe UI', sans-serif;
}}
QPushButton#btn-search-board:enabled:hover {{
    background-color: {btn_blue_h};
    border: 1px solid {cyan};
    color: #ffffff;
}}
QPushButton#btn-search-board:enabled:pressed {{
    background-color: {btn_blue};
    border: 1px solid {cyan};
    color: #ffffff;
}}
QPushButton#btn-search-board:disabled {{
    background-color: {btn_grey};
    color: {text_dim};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 0px;
    text-align: center;
    font-size: 12px;
    font-family: 'Segoe UI Emoji', 'Segoe UI Symbol', 'Segoe UI', sans-serif;
}}

QPushButton#btn-toggle-editor:enabled,
QPushButton#btn-toggle-monitors:enabled,
QPushButton#btn-settings:enabled,
QPushButton#btn-ai-assistant:enabled {{
    background-color: {bg_hover};
    color: {text_bright};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 10.5px;
    font-weight: 600;
}}
QPushButton#btn-toggle-editor:enabled:hover,
QPushButton#btn-toggle-monitors:enabled:hover,
QPushButton#btn-settings:enabled:hover,
QPushButton#btn-ai-assistant:enabled:hover {{
    background-color: {bg_light};
    border: 1px solid {border_lit};
    color: {cyan};
}}
QPushButton#btn-toggle-editor:disabled,
QPushButton#btn-toggle-monitors:disabled,
QPushButton#btn-settings:disabled,
QPushButton#btn-ai-assistant:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton#btn-copy-console:enabled,
QPushButton#btn-clear-console:enabled,
QPushButton#btn-serial-send:enabled,
QPushButton#btn-terminal-new:enabled,
QPushButton#btn-terminal-restart:enabled,
QPushButton#btn-terminal-clear:enabled,
QPushButton#btn-terminal-open:enabled {{
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 3px 8px;
    font-weight: 600;
}}
QPushButton#btn-copy-console:enabled:hover,
QPushButton#btn-clear-console:enabled:hover,
QPushButton#btn-serial-send:enabled:hover,
QPushButton#btn-terminal-new:enabled:hover,
QPushButton#btn-terminal-restart:enabled:hover,
QPushButton#btn-terminal-clear:enabled:hover,
QPushButton#btn-terminal-open:enabled:hover {{
    background-color: {btn_clear_h};
    border: 1px solid {border_lit};
    color: {text_bright};
}}
QPushButton#btn-copy-console:disabled,
QPushButton#btn-clear-console:disabled,
QPushButton#btn-serial-send:disabled,
QPushButton#btn-terminal-new:disabled,
QPushButton#btn-terminal-restart:disabled,
QPushButton#btn-terminal-clear:disabled,
QPushButton#btn-terminal-open:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QDialog QPushButton:enabled {{
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
    border-radius: 5px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QDialog QPushButton:enabled:hover {{
    background-color: {btn_clear_h};
    border: 1px solid {border_lit};
    color: {text_bright};
}}
QDialog QPushButton:enabled:pressed {{
    background-color: {bg_mid};
    border: 1px solid {cyan};
}}
QDialog QPushButton:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

QPushButton[role="primary"]:enabled,
QDialogButtonBox QPushButton:first-child:enabled {{
    background-color: {cyan_dim};
    color: #ffffff;
    border: 1px solid {cyan};
}}
QPushButton[role="primary"]:enabled:hover,
QDialogButtonBox QPushButton:first-child:enabled:hover {{
    background-color: {cyan};
    border: 1px solid {border_lit};
    color: #ffffff;
}}
QPushButton[role="primary"]:disabled,
QDialogButtonBox QPushButton:first-child:disabled {{
    background-color: {btn_disabled_bg};
    color: {btn_disabled_fg};
    border: 1px solid {btn_disabled_border};
}}

/* ── ComboBox ─────────────────────────────────────────────────────────────── */
QComboBox {{
    background-color: {bg_mid};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 3px 8px;
    min-height: 22px;
    selection-background-color: {bg_hover};
}}

QComboBox:hover {{
    border-color: {cyan_dim};
}}

QComboBox:focus {{
    border-color: {cyan};
}}

QComboBox:disabled {{
    background-color: {bg_dark};
    color: {text_dim};
    border-color: {border};
}}

QComboBox::drop-down {{
    width: 20px;
    border: none;
    background: transparent;
}}

QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {text_dim};
    margin-right: 6px;
}}

QComboBox QAbstractItemView {{
    background-color: {bg_mid};
    color: {text};
    border: 1px solid {border};
    selection-background-color: {bg_hover};
    selection-color: {text_bright};
    outline: none;
}}

/* ── PlainTextEdit / TextEdit (console, serial) ───────────────────────────── */
QPlainTextEdit, QTextEdit {{
    background-color: {bg_darkest};
    color: {text};
    border: none;
    font-family: "Consolas", "Cascadia Code", "Courier New", monospace;
    selection-background-color: {bg_hover};
    selection-color: {text_bright};
}}

/* ── LineEdit ─────────────────────────────────────────────────────────────── */
QLineEdit {{
    background-color: {bg_light};
    color: {text_bright};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 4px 8px;
    font-family: "Consolas", monospace;
    selection-background-color: {cyan_dim};
}}

QLineEdit:focus {{
    border-color: {cyan};
}}

/* ── TabWidget ───────────────────────────────────────────────────────────── */
QTabWidget {{
    background-color: {bg_dark};
}}

QTabBar {{
    background-color: {bg_dark};
    border: none;
}}

QTabWidget::pane {{
    background-color: {bg_darkest};
    border: 1px solid {rim};
    border-radius: 10px;
    border-top: 1px solid {border};
}}

QTabBar::tab {{
    background-color: {bg_mid};
    color: {text_dim};
    border: 1px solid {border};
    border-bottom: 2px solid {border};
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    margin-right: 2px;
}}

QTabBar::tab:selected {{
    background-color: {bg_dark};
    color: {cyan};
    border: 1px solid {border_lit};
    border-bottom: 2px solid {cyan};
}}

QTabBar::tab:hover:!selected {{
    background-color: {bg_hover};
    color: {text};
    border: 1px solid {border};
    border-bottom: 2px solid {border};
}}

QTabBar::close-button {{
    margin: 2px;
    padding: 1px;
    border-radius: 2px;
}}
QTabBar::close-button:hover {{
    background: rgba(231, 76, 60, 0.4);
}}

/* ── Splitter ─────────────────────────────────────────────────────────────── */
QSplitter::handle {{
    background-color: {border};
}}
QSplitter::handle:horizontal {{
    width: 4px;
}}
QSplitter::handle:vertical {{
    height: 4px;
}}
QSplitter::handle:hover {{
    background-color: {cyan};
}}

/* ── ScrollBar (High-Visibility Modern Pill) ─────────────────────────────── */
QScrollBar:vertical {{
    background: {bg_darkest};
    width: 14px;
    margin: 0px;
    border: none;
    border-left: 1px solid {border};
}}
QScrollBar::handle:vertical {{
    background: {border};
    border-radius: 5px;
    min-height: 28px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: {cyan};
}}
QScrollBar::handle:vertical:pressed {{
    background: {cyan};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
    background: none;
    border: none;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}

QScrollBar:horizontal {{
    background: {bg_darkest};
    height: 14px;
    margin: 0px;
    border: none;
    border-top: 1px solid {border};
}}
QScrollBar::handle:horizontal {{
    background: {border};
    border-radius: 5px;
    min-width: 28px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {cyan};
}}
QScrollBar::handle:horizontal:pressed {{
    background: {cyan};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
    background: none;
    border: none;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
}}

/* ── CheckBox ─────────────────────────────────────────────────────────────── */
QCheckBox {{
    background: transparent;
    color: {text};
    spacing: 7px;
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {border};
    border-radius: 3px;
    background: {bg_darkest};
}}
QCheckBox::indicator:hover {{
    border-color: {cyan};
    background: {bg_hover};
}}
QCheckBox::indicator:checked {{
    background-color: {cyan_dim};
    border: 1px solid {cyan};
    image: url("{_icon_checked}");
}}
QCheckBox::indicator:checked:hover {{
    background-color: {cyan};
    border-color: {border_lit};
    image: url("{_icon_checked_hover}");
}}
QCheckBox::indicator:disabled {{
    background: {bg_dark};
    border-color: {border};
}}
QCheckBox::indicator:checked:disabled {{
    background: {bg_mid};
    border-color: {border};
    image: url("{_icon_checked_dim}");
}}

/* ── StatusBar ───────────────────────────────────────────────────────────── */
QStatusBar {{
    background-color: {bg_dark};
    color: {text_dim};
    border-top: 1px solid {border};
    font-size: 11px;
    max-height: 24px;
}}

QStatusBar::item {{
    border: none;
}}

/* ── ProgressBar ─────────────────────────────────────────────────────────── */
QProgressBar {{
    background-color: {bg_mid};
    border: 1px solid {border};
    border-radius: 4px;
    text-align: center;
    color: transparent;
    max-height: 12px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {cyan_dim}, stop:0.5 {cyan}, stop:1 {border_lit});
    border-radius: 3px;
    width: 38px;
    margin: 1px;
}}

/* ── MenuBar / Menu ───────────────────────────────────────────────────────── */
QMenuBar {{
    background-color: {bg_dark};
    color: {text};
    border-bottom: 1px solid {border};
}}
QMenuBar::item:selected {{
    background-color: {bg_hover};
}}
QMenu {{
    background-color: {bg_mid};
    color: {text};
    border: 1px solid {border};
}}
QMenu::item:selected {{
    background-color: {bg_hover};
    color: {text_bright};
}}

/* ── GroupBox ─────────────────────────────────────────────────────────────── */
QGroupBox {{
    background-color: {bg_dark};
    border: 1px solid {border};
    border-radius: 6px;
    margin-top: {title_room}px;
    padding-top: 10px;
    color: {cyan};
    font-size: 11px;
    font-weight: 600;
}}
/* Titles sit wholly in the margin above the rim; glass cards cannot mask a rim behind them. */
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: 0px;
    padding: 0 4px;
    background-color: {bg_dark};
    color: {cyan};
}}

/* ── Label ───────────────────────────────────────────────────────────────── */
QLabel {{
    background: transparent;
    color: {text};
}}
QLabel[role="dim"] {{
    color: {text};
    font-size: 11px;
    font-weight: 500;
    background: transparent;
}}
QLabel[role="title"] {{
    color: {cyan};
    font-weight: 700;
    font-size: 14px;
}}

/* ── ToolTip ─────────────────────────────────────────────────────────────── */
QToolTip {{
    background-color: {bg_light};
    color: {text_bright};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 11px;
}}

/* ── ListWidget ───────────────────────────────────────────────────────────── */
QListWidget {{
    background-color: {bg_dark};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 6px 10px;
    border-radius: 3px;
}}
QListWidget::item:selected {{
    background-color: {bg_hover};
    color: {text_bright};
}}
QListWidget::item:hover {{
    background-color: {bg_light};
}}

/* ── TreeWidget ───────────────────────────────────────────────────────────── */
QTreeWidget {{
    background-color: {bg_dark};
    color: {text};
    border: 1px solid {border};
    outline: none;
    alternate-background-color: {bg_mid};
}}
QTreeWidget::item:selected {{
    background-color: {bg_hover};
    color: {text_bright};
}}
QHeaderView::section {{
    background-color: {bg_mid};
    color: {cyan};
    border: none;
    border-bottom: 1px solid {border};
    padding: 4px 8px;
    font-size: 11px;
    font-weight: 600;
}}

/* ── StatusBar & SizeGrip ─────────────────────────────────────────────────── */
QStatusBar {{
    background-color: {bg_dark};
    color: {text};
    border-top: 1px solid {border};
}}
QStatusBar::item {{
    border: none;
}}
QSizeGrip {{
    width: 0px;
    height: 0px;
    image: none;
    background: transparent;
}}

/* Portable glass surfaces. Everything sits over one scene, and reading surfaces are
   smoked glass: dark enough to read at the scene's brightest point, and still see-through. */
QWidget#monaco-editor-panel {{
    background: transparent; border: none; border-radius: 0; margin: 0; padding: 0;
}}
QWidget#editor-detached-placeholder {{ background: transparent; }}
QLabel#detached-editor-description {{ color: {text_dim}; font-size: 12px; }}
QWidget#console-header, QWidget#serial-header, QWidget#compat-header {{
    background: transparent; border: none;
}}
QFrame#notif-header, QFrame#terminal-header, QFrame#syntax-header {{
    background: transparent; border: none;
}}
QAbstractScrollArea > QWidget#qt_scrollarea_viewport {{ background: transparent; }}
QPlainTextEdit, QTextEdit, QTextBrowser,
QPlainTextEdit#build-console, QPlainTextEdit#serial-console, QPlainTextEdit#compat-console,
QTextBrowser#notif-browser {{
    background: {reading}; color: {text}; border: 1px solid {reading_rim}; border-radius: 10px;
    selection-background-color: {selection}; selection-color: {text_bright};
}}
QListWidget, QListView, QTreeWidget, QTreeView, QTableWidget, QTableView,
QTableWidget#syntax-table {{
    background: {reading}; alternate-background-color: {reading_alt}; color: {text};
    border: 1px solid {reading_rim}; border-radius: 10px;
    selection-background-color: {selection}; selection-color: {text_bright};
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTreeView::item:selected,
QTableWidget::item:selected, QTableWidget#syntax-table::item:selected {{
    background: {selection}; color: {text_bright};
}}
QListWidget::item:hover {{ background: {hover}; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: transparent; color: {accent_on_reading}; border: none; border-bottom: 1px solid {reading_rim};
}}
QLabel[role="title"] {{ color: {accent_on_sheet}; }}
QTableCornerButton::section {{ background: transparent; border: none; }}
/* Popups are their own windows with nothing behind them, so they keep a solid theme fill.
   Fusion otherwise opens a combo's list over the combo itself, shifted up to line up the
   current item (a long list covered the whole screen), and paints its items with a menu
   delegate that ignores ::item rules. A list that drops straight down from the field's
   left edge, at its width, with theme-colored rows, is what every combo here wants. */
QComboBox {{ combobox-popup: 0; }}
QComboBox QAbstractItemView {{
    background: {bg_mid}; color: {text}; border: 1px solid {border_lit}; border-radius: 0;
    alternate-background-color: {bg_mid}; padding: 2px; outline: none;
    selection-background-color: {combo_selected}; selection-color: {combo_selected_ink};
}}
QComboBox QAbstractItemView::item {{
    min-height: 22px; padding: 2px 8px; border: none; color: {text}; background: transparent;
}}
QComboBox QAbstractItemView::item:hover {{ background: {bg_hover}; color: {text_bright}; }}
QComboBox QAbstractItemView::item:selected {{ background: {combo_selected}; color: {combo_selected_ink}; }}
QComboBox QAbstractItemView::item:disabled {{ color: {disabled_ink}; background: transparent; }}
QScrollBar:vertical {{ background: transparent; border: none; }}
QScrollBar:horizontal {{ background: transparent; border: none; }}
QScrollBar::handle:vertical {{ background: {handle}; border-radius: 4px; margin: 3px; }}
QScrollBar::handle:horizontal {{ background: {handle}; border-radius: 4px; margin: 3px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed,
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{ background: {cyan}; }}
/* Tool pane divider. Without its own border rule a QFrame HLine is drawn by the
   native style in the palette's text color. */
QFrame#separator {{ background: {rim}; border: none; min-height: 1px; max-height: 1px; }}
QLabel#controls-caption {{ color: {text}; font-size: 10px; font-weight: 700; background: transparent; }}
QFrame#separator-vertical {{ background: {rim}; border: none; min-width: 1px; max-width: 1px; margin: 5px 3px; }}
QFrame#toolbar-sep {{ background: {rim}; border: none; min-width: 1px; max-width: 1px; margin: 5px 5px; }}
QTabWidget#workspace-tabs {{ background: transparent; }}
QTabWidget#workspace-tabs::pane,
QTabWidget#workspace-tabs QTabWidget::pane {{
    background: transparent; border: none; border-radius: 0; margin: 0; padding: 0;
}}
/* The window paints one sheet under the whole tool area, tabs included, so the tabs are
   chips on that sheet and its contents stay clear of its rounded corners. */
QTabWidget#workspace-tabs::tab-bar {{ left: 8px; }}
QTabWidget#workspace-tabs::pane {{ padding: 0 7px 7px 7px; }}
QTabWidget#workspace-tabs[compactPane="true"]::tab-bar {{ left: 4px; }}
QTabWidget#workspace-tabs[compactPane="true"]::pane {{ padding: 0 3px 3px 3px; }}
QTabWidget#workspace-tabs QTabBar {{
    background: transparent; border: none;
}}
QTabWidget#workspace-tabs QTabBar::tab {{
    background: {lit(glass_highlight, glass)};
    color: {text}; border: 1px solid {rim}; border-radius: 11px;
    padding: 4px 10px; margin: 5px 4px 4px 0;
    font-size: 12px; font-weight: 600;
}}
QTabWidget#workspace-tabs[compactPane="true"] QTabBar::tab {{ margin: 3px 3px 2px 0; padding: 2px 9px; }}
QTabWidget#workspace-tabs QTabBar[iconOnly="true"]::tab {{ padding: 4px 7px; width: 18px; }}
QTabWidget#workspace-tabs QTabBar[iconOnly="true"][denseIcons="true"]::tab {{
    width: 16px; padding: 2px 2px; margin: 3px 2px 2px 0;
}}
QTabWidget#workspace-tabs QTabBar::tab:selected {{
    background: {chip}; color: {chip_ink}; border: 1px solid {chip};
}}
QTabWidget#workspace-tabs QTabBar::tab:hover:!selected {{
    background: {button_hover}; color: {text_bright}; border: 1px solid {border_lit};
}}
QTabWidget#workspace-tabs QTabBar QToolButton {{
    background: {glass_highlight}; border: 1px solid {rim}; border-radius: 9px;
}}
QToolButton#serial-display-options {{
    background: {button}; color: {text_bright};
    border: 1px solid {button_rim}; border-radius: 7px;
    padding: 3px 20px 3px 8px; min-height: 20px;
}}
QToolButton#serial-display-options:hover {{
    background: {button_hover}; border-color: {border_lit};
}}
QToolButton#serial-display-options::menu-indicator {{
    image: url("{_icon_arrow}"); width: 10px; height: 10px;
    subcontrol-origin: padding; subcontrol-position: right center; right: 5px;
}}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {selected}; border-radius: 3px; }}

/* Bars over the scene. */
QMenuBar {{ background: transparent; border: none; padding-left: 6px; }}
QToolBar#primary-toolbar, QToolBar#controls-toolbar {{ background: transparent; border: none; }}
QMenuBar::item {{ background: transparent; }}
QMenuBar::item:selected {{ background: {glass_highlight}; }}
QStatusBar {{ background: transparent; border: none; padding-left: 8px; padding-right: 8px; }}

/* Fields are wells cut into the pane, dark enough to read through. */
QComboBox {{ background: {well}; border: 1px solid {rim}; border-radius: 10px; }}
QComboBox:hover {{ border-color: {border_lit}; }}
QComboBox:focus {{ border-color: {cyan}; }}
QComboBox:disabled {{ background: {button_disabled}; border-color: {rim}; }}
QLineEdit {{ background: {well}; border: 1px solid {rim}; border-radius: 10px; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:editable:focus {{ border: 2px solid {cyan}; }}
QCheckBox::indicator {{ background: {well}; border: 1px solid {rim}; border-radius: 5px; }}
QCheckBox::indicator:hover {{ background: {glass_highlight}; border-color: {cyan}; }}
QCheckBox::indicator:checked {{ background: {cyan_dim}; border: 1px solid {cyan}; }}
QCheckBox::indicator:checked:hover {{ background: {cyan}; border-color: {border_lit}; }}
QCheckBox::indicator:disabled {{ background: {button_disabled}; border-color: {rim}; }}
QCheckBox::indicator:checked:disabled {{ background: {button_disabled}; border-color: {rim}; }}
QGroupBox, QDialog QGroupBox {{ background: {glass}; border: 1px solid {rim}; border-radius: 14px; }}
QGroupBox, QDialog QGroupBox, QGroupBox::title {{ color: {accent_on_reading}; }}
QGroupBox::title {{ background: transparent; }}

/* Actions are glass too: a lit fill and a rim, never a solid block. */
{buttons(glass_button_ids, ":enabled", plain=True)} {{
    background: {button}; color: {text_bright}; border: 1px solid {button_rim}; border-radius: 10px;
}}
{buttons(glass_button_ids, ":enabled:hover", plain=True)} {{
    background: {button_hover}; color: {text_bright}; border: 1px solid {border_lit};
}}
{buttons(glass_button_ids, ":enabled:pressed", plain=True)} {{
    background: {button_pressed}; color: {text_bright}; border: 1px solid {cyan};
}}
{buttons((*glass_button_ids, *accent_button_ids, "btn-stop"), ":disabled", plain=True)} {{
    background: {button_disabled}; color: {disabled_ink};
    border: 1px solid {translucent(text_bright, 26)}; border-radius: 10px;
}}
{buttons(accent_button_ids, ":enabled")} {{
    background: {lit(accent_top, accent_bottom)}; color: {text_bright};
    border: 1px solid {border_lit}; border-radius: 10px;
}}
{buttons(accent_button_ids, ":enabled:hover")} {{
    background: {lit(accent_hover_top, accent_hover_bottom)}; color: {text_bright}; border: 1px solid {cyan};
}}
{buttons(accent_button_ids, ":enabled:pressed")} {{
    background: {accent_bottom}; color: {text_bright}; border: 1px solid {cyan};
}}
QPushButton#btn-stop:enabled {{
    background: {lit(translucent(red, 110), translucent(red, 62))}; color: {text_bright};
    border: 1px solid {red}; border-radius: 10px;
}}
QPushButton#btn-stop:enabled:hover {{ background: {lit(translucent(red, 150), translucent(red, 92))}; color: {text_bright}; }}
QPushButton:checked, QDialog QPushButton:checked {{
    background: {lit(accent_top, accent_bottom)}; border: 1px solid {cyan}; color: {text_bright};
}}
QPushButton:checked:hover, QDialog QPushButton:checked:hover {{
    background: {lit(accent_hover_top, accent_hover_bottom)}; border: 1px solid {border_lit}; color: {text_bright};
}}
QComboBox::down-arrow {{
    image: url("{_icon_arrow}"); width: 12px; height: 12px; border: none;
}}
"""
