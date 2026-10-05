#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyQt5 QScintilla Code Viewer for MCU Flash GUI / Arduino Library Browser.
Specialized for viewing Arduino/C++ code (.ino / .cpp / .h) in read-only mode.
"""

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main.core.theme import get_palette
from main.core.log_colors import contrast_ratio, themed_log_colors
from src.modules.ui_metrics import WorkArea, fit_rect, preferred_size

try:
    # pyrefly: ignore [missing-import]
    from PyQt5.QtCore import Qt, QSize, QTimer, QRect
    # pyrefly: ignore [missing-import]
    from PyQt5.QtGui import QColor, QFont, QFontInfo, QFontMetrics, QIcon
    # pyrefly: ignore [missing-import]
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QVBoxLayout, QWidget, QTabBar, QTabWidget
    )
except ImportError:
    sys.exit("PyQt5 is required. Prepare the sample viewer in MCU Flasher Bootstrap.")

try:
    # pyrefly: ignore [missing-import]
    from PyQt5.Qsci import QsciScintilla, QsciLexerCPP
except ImportError:
    sys.exit("QScintilla is required. Prepare the sample viewer in MCU Flasher Bootstrap.")

ARDUINO_FUNCTIONS = """
setup loop pinMode digitalWrite digitalRead analogWrite analogRead analogReference
analogWriteResolution analogReadResolution tone noTone pulseIn pulseInLong shiftIn shiftOut
attachInterrupt detachInterrupt interrupts noInterrupts delay delayMicroseconds micros millis
min max abs constrain map pow sqrt sq sin cos tan random randomSeed
Serial Serial1 Serial2 Serial3 Wire SPI EEPROM begin end available read write print println peek flush
push pop attach detach write writeMicroseconds read
""".split()

ARDUINO_CONSTANTS = """
HIGH LOW INPUT OUTPUT INPUT_PULLUP LED_BUILTIN true false TRUE FALSE
PI HALF_PI TWO_PI DEG_TO_RAD RAD_TO_DEG A0 A1 A2 A3 A4 A5 A6 A7
CHANGE RISING FALLING DEC BIN HEX OCT
""".split()

ARDUINO_TYPES = """
boolean byte word String Stream Print Printable
uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t size_t
""".split()

ARDUINO_KEYWORDS_SET2 = ARDUINO_FUNCTIONS + ARDUINO_CONSTANTS + ARDUINO_TYPES

class ArduinoLexer(QsciLexerCPP):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFoldComments(True)
        self.setFoldPreprocessor(True)

    def keywords(self, set: int):
        if set == 2:
            return " ".join(ARDUINO_KEYWORDS_SET2)
        return super().keywords(set)

    def description(self, style):
        if style == QsciLexerCPP.KeywordSet2:
            return "Arduino API"
        return super().description(style)

def viewer_colors(theme_mode="default"):
    """Use the workspace palette without loading either toolkit into the other."""
    palette = get_palette(theme_mode)
    normal = themed_log_colors(theme_mode)
    backgrounds = (palette["BG_DARKEST"], palette["BG_DARK"])

    def readable(semantic):
        for color in (normal[semantic], palette["TEXT"], "#000000", "#ffffff"):
            if all(contrast_ratio(color, background) >= 4.5 for background in backgrounds):
                return color
        return normal[semantic]

    selection = themed_log_colors(theme_mode, background=palette["BG_HOVER"])
    return {
        "background": palette["BG_DARKEST"], "foreground": readable("normal"),
        "line_number": readable("dim"), "margin_bg": palette["BG_DARK"],
        "caret_line": palette["BG_DARK"], "selection": palette["BG_HOVER"],
        "selection_foreground": selection["normal"], "comment": readable("dim"),
        "string": readable("success"), "number": readable("warning"),
        "keyword": readable("error"), "arduino_api": readable("system"),
        "identifier": readable("normal"), "operator": readable("system"),
        "preprocessor": readable("warning"), "matched_brace_bg": palette["BG_DARK"],
        "matched_brace_fg": readable("system"), "tab_text": readable("dim"),
        "tab_selected": readable("system"), "tab_hover": selection["bold"],
        "border": palette["BORDER"],
    }


def load_viewer_preferences():
    """Read existing preferences; a viewing-only window never creates settings."""
    from main.core.config import get_monitor_font_size, get_theme_mode
    return get_theme_mode(), get_monitor_font_size()


class AdaptiveTabBar(QTabBar):
    """Adaptive tab bar that fits filenames without aggressive elision.

    Tabs use their natural width up to a generous per-tab ceiling so that
    full filenames are always readable. Middle-elision only kicks in when
    the bar truly runs out of room. The bar still scrolls when more tabs
    are open than can reasonably fit on screen.
    """

    # Minimum width per tab — enough to show even very short names clearly.
    _MIN_TAB_WIDTH = 140
    # Maximum width per tab — prevents a single long filename from eating
    # the entire bar when only one or two tabs are open.
    _MAX_TAB_WIDTH = 340

    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        label_font = QFont(self.font())
        label_font.setBold(True)  # Reserve the selected tab's styled text width.
        label_width = QFontMetrics(label_font).horizontalAdvance(self.tabText(index)) + 26
        desired = min(max(hint.width(), label_width, self._MIN_TAB_WIDTH), self._MAX_TAB_WIDTH)
        # QTabBar's width is its current tab sum when expansion is disabled.
        # Dividing that width traps later hints at the old minimum. Preserve
        # natural short labels; Qt's scroll buttons handle constrained space.
        return QSize(desired, hint.height())

    def resizeEvent(self, a0):
        super().resizeEvent(a0)
        self.updateGeometry()

class CodeViewer(QsciScintilla):
    def __init__(self, parent=None, *, theme_mode="default", font_size=12):
        super().__init__(parent)
        self.colors = viewer_colors(theme_mode)
        self._font_size = max(6, min(72, int(font_size)))
        self.setMinimumSize(0, 0)
        self.setFrameShape(self.NoFrame)
        self.file_path = None
        self.setReadOnly(True)
        focus_policy: Any = getattr(Qt, "StrongFocus", 0x1 | 0x2 | 0x8)
        self.setFocusPolicy(focus_policy)
        self._configure_font()
        self._configure_lexer()
        self._configure_margins()
        self._configure_folding()
        self._configure_braces()
        self._configure_caret_and_selection()
        self._configure_scrollbars()

        # Keyboard-only zoom shortcuts (Ctrl+Plus/Minus/Equal)
        # pyrefly: ignore [missing-import]
        from PyQt5.QtWidgets import QShortcut
        # pyrefly: ignore [missing-import]
        from PyQt5.QtGui import QKeySequence
        QShortcut(QKeySequence("Ctrl++"), self, self.zoomIn)
        QShortcut(QKeySequence("Ctrl+="), self, self.zoomIn)
        QShortcut(QKeySequence("Ctrl+-"), self, self.zoomOut)

    def wheelEvent(self, event):
        if event.modifiers() & getattr(Qt, "ControlModifier", 0x04000000):
            event.accept()
            return
        super().wheelEvent(event)

    def _configure_font(self):
        font = QFont("Consolas")
        if not QFontInfo(font).fixedPitch():
            font = QFont("Monospace")
        font.setFixedPitch(True)
        font.setPointSize(self._font_size)
        self.setFont(font)
        self._base_font = font

    def _configure_lexer(self):
        lexer = self.lexer() or ArduinoLexer(self)
        lexer.setDefaultFont(self._base_font)
        lexer.setDefaultColor(QColor(self.colors["foreground"]))
        lexer.setDefaultPaper(QColor(self.colors["background"]))

        # QScintilla's Default style has its own grey foreground; inherited
        # defaults also affect inactive/comment variants emitted by the lexer.
        for style in range(128):
            lexer.setColor(QColor(self.colors["foreground"]), style)
            lexer.setPaper(QColor(self.colors["background"]), style)
            lexer.setFont(self._base_font, style)

        style_colors = {
            getattr(QsciLexerCPP, "Comment", 1): self.colors["comment"],
            getattr(QsciLexerCPP, "CommentLine", 2): self.colors["comment"],
            getattr(QsciLexerCPP, "CommentDoc", 3): self.colors["comment"],
            getattr(QsciLexerCPP, "Number", 4): self.colors["number"],
            getattr(QsciLexerCPP, "Keyword", 5): self.colors["keyword"],
            getattr(QsciLexerCPP, "DoubleQuotedString", 6): self.colors["string"],
            getattr(QsciLexerCPP, "SingleQuotedString", 7): self.colors["string"],
            getattr(QsciLexerCPP, "PreProcessor", 9): self.colors["preprocessor"],
            getattr(QsciLexerCPP, "Operator", 10): self.colors["operator"],
            getattr(QsciLexerCPP, "Identifier", 11): self.colors["identifier"],
            getattr(QsciLexerCPP, "KeywordSet2", 16): self.colors["arduino_api"],
        }
        for style, color in style_colors.items():
            lexer.setColor(QColor(color), style)
            lexer.setPaper(QColor(self.colors["background"]), style)
            lexer.setFont(self._base_font, style)

        self.setLexer(lexer)
        self.setPaper(QColor(self.colors["background"]))
        self.setColor(QColor(self.colors["foreground"]))

    def _configure_margins(self):
        self.setMarginType(0, QsciScintilla.NumberMargin)
        self.setMarginWidth(0, "0000")
        self.setMarginsForegroundColor(QColor(self.colors["line_number"]))
        self.setMarginsBackgroundColor(QColor(self.colors["margin_bg"]))
        self.setMarginsFont(self._base_font)

    def _configure_folding(self):
        self.setFolding(QsciScintilla.PlainFoldStyle, 2)
        self.setFoldMarginColors(QColor(self.colors["margin_bg"]), QColor(self.colors["margin_bg"]))

    def _configure_braces(self):
        self.setBraceMatching(QsciScintilla.StrictBraceMatch)
        self.setMatchedBraceBackgroundColor(QColor(self.colors["matched_brace_bg"]))
        self.setMatchedBraceForegroundColor(QColor(self.colors["matched_brace_fg"]))

    def _configure_caret_and_selection(self):
        self.setCaretLineVisible(True)
        self.setCaretLineBackgroundColor(QColor(self.colors["caret_line"]))
        self.setCaretForegroundColor(QColor(self.colors["foreground"]))
        self.setSelectionBackgroundColor(QColor(self.colors["selection"]))
        self.setSelectionForegroundColor(QColor(self.colors["selection_foreground"]))

    def apply_theme(self, theme_mode):
        self.colors = viewer_colors(theme_mode)
        self._configure_lexer()
        self._configure_margins()
        self._configure_folding()
        self._configure_braces()
        self._configure_caret_and_selection()
        self._configure_scrollbars()

    def _configure_scrollbars(self):
        style = f"""
            QScrollBar:vertical {{ background: {self.colors['margin_bg']};
                                   width: 14px; margin: 0; }}
            QScrollBar:horizontal {{ background: {self.colors['margin_bg']};
                                     height: 14px; margin: 0; }}
            QScrollBar::handle {{ background: {self.colors['border']}; border-radius: 5px; }}
            QScrollBar::handle:vertical {{ min-height: 24px; margin: 2px; }}
            QScrollBar::handle:horizontal {{ min-width: 24px; margin: 2px; }}
            QScrollBar::handle:hover {{ background: {self.colors['tab_selected']}; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
            QScrollBar::add-page, QScrollBar::sub-page {{ background: {self.colors['margin_bg']}; }}
        """
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.setStyleSheet(style)
        self.setStyleSheet(f"QAbstractScrollArea::corner {{ background: {self.colors['margin_bg']}; }}")

class MainWindow(QMainWindow):
    def __init__(self, focus_file, files, *, theme_mode=None, font_size=None):
        super().__init__()
        if theme_mode is None or font_size is None:
            saved_theme, saved_font = load_viewer_preferences()
            theme_mode = saved_theme if theme_mode is None else theme_mode
            font_size = saved_font if font_size is None else font_size
        self.theme_mode = theme_mode
        self.font_size = font_size
        self.colors = viewer_colors(theme_mode)
        self.setWindowTitle("MCU Flasher — Code Viewer")
        self.resize(1000, 700)
        self.setStyleSheet(f"QMainWindow {{ background-color: {self.colors['background']}; }}")

        # Load application font if available
        src_dir = os.path.dirname(os.path.abspath(__file__))
        fonts_static = os.path.join(src_dir, "fonts", "Montserrat", "static")
        if os.path.isdir(fonts_static):
            try:
                # pyrefly: ignore [missing-import]
                from PyQt5.QtGui import QFontDatabase
                for f_name in ("Montserrat-Regular.ttf", "Montserrat-Medium.ttf", "Montserrat-SemiBold.ttf"):
                    f_path = os.path.join(fonts_static, f_name)
                    if os.path.exists(f_path):
                        QFontDatabase.addApplicationFont(f_path)
            except Exception:
                pass
        else:
            font_path = os.path.join(src_dir, "fonts", "Montserrat", "Montserrat-VariableFont_wght.ttf")
            if os.path.exists(font_path):
                try:
                    # pyrefly: ignore [missing-import]
                    from PyQt5.QtGui import QFontDatabase
                    QFontDatabase.addApplicationFont(font_path)
                except Exception:
                    pass

        # Set window icon if available
        icon_path = os.path.join(src_dir, "assets", "mcu_icon.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(src_dir, "mcu_icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget(central_widget)
        self.tabs.setTabBar(AdaptiveTabBar(self.tabs))
        self.tabs.setTabsClosable(False)
        self.tabs.setUsesScrollButtons(True)
        elide_middle: Any = getattr(Qt, "ElideMiddle", 2)
        self.tabs.setElideMode(elide_middle)
        tb = self.tabs.tabBar()
        if tb is not None:
            # Keep Qt's hint metrics aligned with the font painted by the QSS.
            tb.setFont(QFont("Montserrat", 10))
            tb.setElideMode(elide_middle)
            tb.setExpanding(False)
        self._apply_tab_style()
        layout.addWidget(self.tabs)

        focus_index = 0
        for i, file_path in enumerate(files):
            viewer = CodeViewer(self.tabs, theme_mode=theme_mode, font_size=font_size)
            self.load_file(viewer, file_path)
            tab_name = os.path.basename(file_path)
            self.tabs.addTab(viewer, tab_name)
            self.tabs.setTabToolTip(i, file_path)
            if os.path.normcase(os.path.abspath(file_path)) == os.path.normcase(os.path.abspath(focus_file)):
                focus_index = i

        self.tabs.currentChanged.connect(self._on_tab_changed)
        if files:
            self.tabs.setCurrentIndex(focus_index)
            self._on_tab_changed(focus_index)
        self._screen = None
        self.winId()
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._screen_changed)
            self._screen_changed(handle.screen())
        self.fit_to_work_area(initial=True)
        QTimer.singleShot(0, self.fit_to_work_area)

    def _apply_tab_style(self):
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {self.colors['background']}; }}
            QStatusBar {{ background-color: {self.colors['margin_bg']};
                          color: {self.colors['foreground']}; }}
        """)
        self.tabs.setStyleSheet(f"""
            QTabWidget {{
                background-color: {self.colors['background']};
            }}
            QTabWidget::pane {{
                border: 1px solid {self.colors['margin_bg']};
                background-color: {self.colors['background']};
                top: -1px;
            }}
            QTabBar {{
                qproperty-drawBase: 0;
                background-color: transparent;
            }}
            QTabBar::tab {{
                background-color: {self.colors['margin_bg']};
                color: {self.colors["tab_text"]};
                border: 1px solid {self.colors['margin_bg']};
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                min-height: 28px;
                min-width: 120px;
                padding: 4px 10px 6px 10px;
                margin-right: 3px;
                font-family: "Montserrat Medium", "Montserrat", "Segoe UI", -apple-system, sans-serif;
                font-size: 10pt;
            }}
            QTabBar::tab:selected {{
                background-color: {self.colors['background']};
                color: {self.colors["tab_selected"]};
                border: 1px solid {self.colors['margin_bg']};
                border-bottom: 2px solid {self.colors["tab_selected"]};
                margin-bottom: -1px;
                font-weight: bold;
            }}
            QTabBar::tab:hover:!selected {{
                background-color: {self.colors['selection']};
                color: {self.colors["tab_hover"]};
            }}
            QTabBar::scroller {{
                width: 24px;
            }}
            QTabBar QToolButton {{
                background-color: {self.colors['margin_bg']};
                border: 1px solid {self.colors['margin_bg']};
                color: {self.colors['foreground']};
                border-radius: 2px;
            }}
            QTabBar QToolButton:hover {{
                background-color: {self.colors['selection']};
            }}
        """)

    def apply_theme(self, theme_mode):
        self.theme_mode = theme_mode
        self.colors = viewer_colors(theme_mode)
        self._apply_tab_style()
        for index in range(self.tabs.count()):
            self.tabs.widget(index).apply_theme(theme_mode)

    def _screen_changed(self, screen):
        if self._screen is not None:
            try:
                self._screen.availableGeometryChanged.disconnect(self.fit_to_work_area)
            except (TypeError, RuntimeError):
                pass
        self._screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self.fit_to_work_area)
        self.fit_to_work_area()

    def fit_to_work_area(self, rectangle=None, *, initial=False):
        """Fit Qt logical geometry, reserving the native window frame once."""
        if self.isMaximized() or self.isFullScreen():
            return
        if rectangle is None:
            screen = self._screen or QApplication.primaryScreen()
            if screen is None:
                return
            rectangle = screen.availableGeometry()
        area = WorkArea(rectangle.x(), rectangle.y(), rectangle.width(), rectangle.height())
        if initial:
            width, height = preferred_size(area, 1000, 700)
            x, y = area.x + (area.width - width) // 2, area.y + (area.height - height) // 2
            self.setGeometry(x, y, width, height)
        # A native Windows frame can change its decoration metrics after being
        # shown/moved. Reconcile those metrics in a bounded pass without events
        # or polling; Qt owns DPI scaling and maximized window placement.
        for _ in range(3):
            client, frame = self.geometry(), self.frameGeometry()
            handle = self.windowHandle()
            if handle is not None:
                margins = handle.frameMargins()
                # Qt 5 may cache the old frame rectangle until the next event
                # after Windows updates the border. Native margins are current.
                if any((margins.left(), margins.top(), margins.right(), margins.bottom())):
                    frame = client.adjusted(-margins.left(), -margins.top(),
                                            margins.right(), margins.bottom())
            dx, dy = client.x() - frame.x(), client.y() - frame.y()
            extra_w = max(0, frame.width() - client.width())
            extra_h = max(0, frame.height() - client.height())
            x, y, width, height = fit_rect(frame.x(), frame.y(), frame.width(), frame.height(), area, margin=4)
            fitted = QRect(x + dx, y + dy, max(1, width - extra_w), max(1, height - extra_h))
            if fitted == client:
                break
            self.setGeometry(fitted)

    def _on_tab_changed(self, index):
        widget = self.tabs.widget(index)
        if widget and hasattr(widget, 'file_path') and widget.file_path:
            self.setWindowTitle(f"MCU Flasher — Code Viewer: {os.path.basename(widget.file_path)}")

    def load_file(self, viewer, file_path):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                viewer.setText(f.read())
            viewer.file_path = file_path
            # Clear undo buffer to prevent any modifications from being undoable
            viewer.SendScintilla(viewer.SCI_EMPTYUNDOBUFFER)
            return True
        except Exception as e:
            viewer.setText(f"Unable to read this sample:\n{file_path}\n\n{e}")
            self.statusBar().showMessage(f"Unable to read {os.path.basename(file_path)}")
            return False

def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: qscintilla_viewer.py <focus_file_path> [all_file_paths...]")

    focus_file = sys.argv[1]
    
    if len(sys.argv) > 2:
        files = []
        for path in sys.argv[2:]:
            if path not in files:
                files.append(path)
        # Ensure focus_file is in the list
        if focus_file not in files:
            files.insert(0, focus_file)
    else:
        files = [focus_file]

    # Enable High DPI scaling before QApplication creation
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(getattr(Qt, "AA_EnableHighDpiScaling"), True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(getattr(Qt, "AA_UseHighDpiPixmaps"), True)

    # Set DPI awareness on Windows to match main GUI
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    app = QApplication(sys.argv)
    theme_mode = os.environ.get("MCU_FLASHER_VIEWER_THEME")
    try:
        font_size = int(os.environ["MCU_FLASHER_VIEWER_FONT_SIZE"])
    except (KeyError, ValueError):
        font_size = None
    window = MainWindow(focus_file, files, theme_mode=theme_mode, font_size=font_size)
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
