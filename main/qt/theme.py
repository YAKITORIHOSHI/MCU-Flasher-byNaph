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


def install_checkbox_focus_style(app=None) -> bool:
    """Keep native checkbox focus feedback on the indicator, not its label."""
    from PySide6.QtWidgets import QApplication, QCheckBox, QProxyStyle, QStyle

    app = app or QApplication.instance()
    if app is None or app.property("mcuCheckboxFocusStyleInstalled"):
        return False

    class CheckboxFocusStyle(QProxyStyle):
        def drawPrimitive(self, element, option, painter, widget=None):
            if (element == QStyle.PrimitiveElement.PE_FrameFocusRect
                    and isinstance(widget, QCheckBox)):
                return
            super().drawPrimitive(element, option, painter, widget)

    # Qt's platform style draws a focus rectangle around a checkbox's label.
    # The stylesheet paints the focus border on ::indicator instead.
    app.setStyle(CheckboxFocusStyle(app.style()))
    app.setProperty("mcuCheckboxFocusStyleInstalled", True)
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


# ─────────────────────────────────────────────────────────────────────────────
# Stylesheet builder
# ─────────────────────────────────────────────────────────────────────────────

def build_stylesheet(theme_mode: str = "default") -> str:
    """Return a complete QApplication stylesheet for the given theme mode."""
    pal = get_palette(theme_mode)

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

    def translucent(color: str, alpha: int) -> str:
        value = color.lstrip("#")
        return f"rgba({int(value[:2], 16)}, {int(value[2:4], 16)}, {int(value[4:6], 16)}, {alpha})"

    glass = translucent(bg_dark, 225)
    glass_highlight = translucent(bg_light, 220)
    rim = translucent(text_bright, 55)
    selected = translucent(cyan, 35)

    return f"""
/* ── Global ─────────────────────────────────────────────────────────────── */
* {{
    font-family: "Montserrat", "Segoe UI", "Ubuntu", sans-serif;
    font-size: 12px;
}}

QMainWindow {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {bg_mid}, stop:0.48 {bg_darkest}, stop:1 {bg_dark});
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
    padding: 7px 10px;
    spacing: 4px;
}}

QToolBar#controls-toolbar {{
    background-color: {glass};
    border: none;
    padding: 0px;
}}

QToolBar#primary-toolbar[shortScreen="true"],
QToolBar#controls-toolbar[shortScreen="true"] {{
    padding-top: 0px;
    padding-bottom: 0px;
}}

QWidget#controls-bar {{
    background-color: {glass};
    border-bottom: 1px solid {border};
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

QPushButton#btn-modify:enabled {{
    background-color: {btn_monitor};
    color: #ffffff;
    border: 1px solid {cyan_dim};
}}
QPushButton#btn-modify:enabled:hover {{
    background-color: {btn_monitor_h};
    border: 1px solid {border_lit};
    color: #ffffff;
}}
QPushButton#btn-modify:disabled {{
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
    background-color: {btn_clear};
    color: {text_bright};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 0px;
    text-align: center;
    font-size: 12px;
    font-family: 'Segoe UI Emoji', 'Segoe UI Symbol', 'Segoe UI', sans-serif;
}}
QPushButton#btn-search-board:enabled:hover {{
    background-color: {btn_clear_h};
    border: 1px solid {border_lit};
    color: {cyan};
}}
QPushButton#btn-search-board:disabled {{
    background-color: {bg_darkest};
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
    border-bottom: 1px solid {border};
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
    image: url("{_icon_checked}");
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
QCheckBox:focus {{ border: none; }}
QCheckBox::indicator:focus {{ border: 2px solid {cyan}; }}

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
    margin-top: 10px;
    padding-top: 14px;
    color: {cyan};
    font-size: 11px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    background-color: {bg_dark};
    color: {cyan};
}}

/* ── Label ───────────────────────────────────────────────────────────────── */
QLabel {{
    background: transparent;
    color: {text};
}}
QLabel[role="dim"] {{
    color: {text_dim};
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
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

/* Portable glass surfaces. Reading panes stay opaque for stable contrast. */
QWidget#monaco-editor-panel {{
    background: {bg_darkest}; border: none; border-radius: 0; margin: 0; padding: 0;
}}
QWidget#editor-detached-placeholder {{ background: {bg_darkest}; }}
QLabel#detached-editor-description {{ color: {text_dim}; font-size: 12px; }}
QWidget#console-header, QWidget#serial-header, QWidget#compat-header,
QFrame#notif-header, QFrame#terminal-header, QFrame#syntax-header {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {glass_highlight}, stop:1 {glass});
    border-bottom: 1px solid {rim};
}}
QTabWidget#workspace-tabs {{ background: transparent; }}
QTabWidget#workspace-tabs::pane,
QTabWidget#workspace-tabs QTabWidget::pane {{
    background: {bg_darkest}; border: none; border-radius: 0; margin: 0; padding: 0;
}}
QTabWidget#workspace-tabs QTabBar {{
    background: transparent; border: none;
}}
QTabWidget#workspace-tabs QTabBar::tab {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {glass_highlight}, stop:1 {glass});
    color: {text_dim}; border: 1px solid {rim}; border-radius: 9px;
    padding: 6px 12px; margin-right: 4px; margin-bottom: 4px;
    font-size: 12px; font-weight: 600;
}}
QTabWidget#workspace-tabs QTabBar::tab:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {selected}, stop:1 {glass_highlight});
    color: {text_bright}; border: 1px solid {cyan}; border-bottom: 2px solid {cyan};
}}
QTabWidget#workspace-tabs QTabBar::tab:hover:!selected {{
    background: {glass_highlight}; color: {text}; border: 1px solid {border_lit};
}}
QTabWidget#workspace-tabs QTabBar QToolButton {{
    background: {bg_dark}; border: 1px solid {rim}; border-radius: 7px;
}}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {selected}; }}
QPushButton:focus, QToolButton:focus,
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 2px solid {cyan}; }}
QPushButton#btn-compile:focus, QPushButton#btn-upload:focus,
QPushButton#btn-stop:focus, QPushButton#btn-save:focus,
QPushButton#btn-save-all:focus {{ border: 2px solid {cyan}; }}
QDialog QGroupBox {{ border: 1px solid {rim}; border-radius: 12px; }}
QPushButton#btn-compile:enabled, QPushButton#btn-upload:enabled,
QPushButton#btn-save:enabled, QPushButton#btn-save-all:enabled,
QPushButton#btn-modify:enabled {{
    background: {glass_highlight}; color: {text_bright}; border: 1px solid {border_lit};
}}
QPushButton#btn-compile:enabled:hover, QPushButton#btn-upload:enabled:hover,
QPushButton#btn-save:enabled:hover, QPushButton#btn-save-all:enabled:hover,
QPushButton#btn-modify:enabled:hover {{ background: {selected}; color: {text_bright}; }}
QPushButton#btn-stop:enabled {{ background: {btn_stop}; }}
QComboBox::down-arrow {{
    image: url("{_icon_arrow}"); width: 12px; height: 12px; border: none;
}}
"""
