#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyQt5 QScintilla Code Viewer for MCU Flash GUI / Arduino Library Browser.
Specialized for viewing Arduino/C++ code (.ino / .cpp / .h) in read-only mode.
"""

import os
import sys
from typing import Any

try:
    # pyrefly: ignore [missing-import]
    from PyQt5.QtCore import Qt, QSize
    # pyrefly: ignore [missing-import]
    from PyQt5.QtGui import QColor, QFont, QFontInfo, QIcon
    # pyrefly: ignore [missing-import]
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QVBoxLayout, QWidget, QTabBar, QTabWidget
    )
except ImportError:
    sys.exit("PyQt5 is required. Install with: pip install PyQt5 PyQt5-QScintilla")

try:
    # pyrefly: ignore [missing-import]
    from PyQt5.Qsci import QsciScintilla, QsciLexerCPP
except ImportError:
    sys.exit("PyQt5-QScintilla is required. Install with: pip install PyQt5-QScintilla")

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

THEME = {
    "background":       "#0a0e14",
    "foreground":       "#c8d2dc",
    "line_number":      "#6b7d94",
    "margin_bg":        "#10151c",
    "caret_line":       "#161d27",
    "selection":        "#243040",
    "comment":          "#6b7d94",
    "string":           "#5ccc6e",
    "number":           "#e8b83a",
    "keyword":          "#f05050",
    "arduino_api":      "#39c5bb",
    "identifier":       "#e8edf3",
    "operator":         "#39c5bb",
    "preprocessor":     "#e8b83a",
    "matched_brace_bg": "#1c2532",
    "matched_brace_fg": "#39c5bb",
}


class AdaptiveTabBar(QTabBar):
    """Keep filename tabs inside the available bar width.

    Long sample filenames remain identifiable through middle elision while
    their complete paths stay available in the tab tooltip. The bar still
    scrolls when there are more tabs than can reasonably fit on screen.
    """

    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        count = max(1, self.count())
        available = self.width()
        if available <= 0:
            available = 1000

        # Give every tab a useful minimum, but share the visible width when
        # several long sample names are open. QTabBar then applies the
        # configured middle-elision mode instead of painting text out of bounds.
        gap = max(0, count - 1) * 3
        width_per_tab = max(92, min(260, (available - gap) // count))
        return QSize(min(hint.width(), width_per_tab), hint.height())

    def resizeEvent(self, a0):
        super().resizeEvent(a0)
        self.updateGeometry()

class CodeViewer(QsciScintilla):
    def __init__(self, parent=None):
        super().__init__(parent)
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
        font.setPointSize(10)
        self.setFont(font)
        self._base_font = font

    def _configure_lexer(self):
        lexer = ArduinoLexer(self)
        lexer.setDefaultFont(self._base_font)
        lexer.setDefaultColor(QColor(THEME["foreground"]))
        lexer.setDefaultPaper(QColor(THEME["background"]))

        style_colors = {
            getattr(QsciLexerCPP, "Comment", 1): THEME["comment"],
            getattr(QsciLexerCPP, "CommentLine", 2): THEME["comment"],
            getattr(QsciLexerCPP, "CommentDoc", 3): THEME["comment"],
            getattr(QsciLexerCPP, "Number", 4): THEME["number"],
            getattr(QsciLexerCPP, "Keyword", 5): THEME["keyword"],
            getattr(QsciLexerCPP, "DoubleQuotedString", 6): THEME["string"],
            getattr(QsciLexerCPP, "SingleQuotedString", 7): THEME["string"],
            getattr(QsciLexerCPP, "PreProcessor", 9): THEME["preprocessor"],
            getattr(QsciLexerCPP, "Operator", 10): THEME["operator"],
            getattr(QsciLexerCPP, "Identifier", 11): THEME["identifier"],
            getattr(QsciLexerCPP, "KeywordSet2", 16): THEME["arduino_api"],
        }
        for style, color in style_colors.items():
            lexer.setColor(QColor(color), style)
            lexer.setPaper(QColor(THEME["background"]), style)
            lexer.setFont(self._base_font, style)

        self.setLexer(lexer)
        self.setPaper(QColor(THEME["background"]))
        self.setColor(QColor(THEME["foreground"]))

    def _configure_margins(self):
        self.setMarginType(0, QsciScintilla.NumberMargin)
        self.setMarginWidth(0, "0000")
        self.setMarginsForegroundColor(QColor(THEME["line_number"]))
        self.setMarginsBackgroundColor(QColor(THEME["margin_bg"]))
        self.setMarginsFont(self._base_font)

    def _configure_folding(self):
        self.setFolding(QsciScintilla.PlainFoldStyle, 2)
        self.setFoldMarginColors(QColor(THEME["margin_bg"]), QColor(THEME["margin_bg"]))

    def _configure_braces(self):
        self.setBraceMatching(QsciScintilla.StrictBraceMatch)
        self.setMatchedBraceBackgroundColor(QColor(THEME["matched_brace_bg"]))
        self.setMatchedBraceForegroundColor(QColor(THEME["matched_brace_fg"]))

    def _configure_caret_and_selection(self):
        self.setCaretLineVisible(True)
        self.setCaretLineBackgroundColor(QColor(THEME["caret_line"]))
        self.setCaretForegroundColor(QColor(THEME["foreground"]))
        self.setSelectionBackgroundColor(QColor(THEME["selection"]))
        self.setSelectionForegroundColor(QColor(THEME["foreground"]))

class MainWindow(QMainWindow):
    def __init__(self, focus_file, files):
        super().__init__()
        self.setWindowTitle("MCU Flasher — Code Viewer")
        self.resize(1000, 700)
        self.setStyleSheet(f"QMainWindow {{ background-color: {THEME['background']}; }}")

        # Load application font if available
        src_dir = os.path.dirname(os.path.abspath(__file__))
        fonts_static = os.path.join(src_dir, "fonts", "Montserrat", "static")
        if os.path.isdir(fonts_static):
            try:
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
            tb.setElideMode(elide_middle)
            tb.setExpanding(False)
        self.tabs.setStyleSheet(f"""
            QTabWidget {{
                background-color: {THEME['background']};
            }}
            QTabWidget::pane {{
                border: 1px solid {THEME['margin_bg']};
                background-color: {THEME['background']};
                top: -1px;
            }}
            QTabBar {{
                qproperty-drawBase: 0;
                background-color: transparent;
            }}
            QTabBar::tab {{
                background-color: {THEME['margin_bg']};
                color: #8b99a7;
                border: 1px solid {THEME['margin_bg']};
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                min-height: 28px;
                min-width: 80px;
                padding: 4px 10px 6px 10px;
                margin-right: 3px;
                font-family: "Montserrat Medium", "Montserrat", "Segoe UI", -apple-system, sans-serif;
                font-size: 10pt;
            }}
            QTabBar::tab:selected {{
                background-color: {THEME['background']};
                color: #39c5bb; /* Theme.CYAN */
                border: 1px solid {THEME['margin_bg']};
                border-bottom: 2px solid #39c5bb;
                margin-bottom: -1px;
                font-weight: bold;
            }}
            QTabBar::tab:hover:!selected {{
                background-color: {THEME['selection']};
                color: #ffffff;
            }}
            QTabBar::scroller {{
                width: 24px;
            }}
            QTabBar QToolButton {{
                background-color: {THEME['margin_bg']};
                border: 1px solid {THEME['margin_bg']};
                color: {THEME['foreground']};
                border-radius: 2px;
            }}
            QTabBar QToolButton:hover {{
                background-color: {THEME['selection']};
            }}
        """)
        layout.addWidget(self.tabs)

        focus_index = 0
        for i, file_path in enumerate(files):
            viewer = CodeViewer(self.tabs)
            self.load_file(viewer, file_path)
            tab_name = os.path.basename(file_path)
            self.tabs.addTab(viewer, tab_name)
            self.tabs.setTabToolTip(i, file_path)
            if os.path.normpath(file_path) == os.path.normpath(focus_file):
                focus_index = i

        self.tabs.currentChanged.connect(self._on_tab_changed)
        if files:
            self.tabs.setCurrentIndex(focus_index)
            self._on_tab_changed(focus_index)

    def _on_tab_changed(self, index):
        widget = self.tabs.widget(index)
        if widget and hasattr(widget, 'file_path') and widget.file_path:
            self.setWindowTitle(f"MCU Flasher — Code Viewer: {os.path.basename(widget.file_path)}")

    def load_file(self, viewer, file_path):
        if not os.path.exists(file_path):
            return
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                viewer.setText(f.read())
            viewer.file_path = file_path
            # Clear undo buffer to prevent any modifications from being undoable
            viewer.SendScintilla(viewer.SCI_EMPTYUNDOBUFFER)
        except Exception as e:
            print(f"Error loading file: {e}")

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
    window = MainWindow(focus_file, files)
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
