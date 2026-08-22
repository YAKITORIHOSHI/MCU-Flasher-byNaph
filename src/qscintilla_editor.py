#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyQt5 QScintilla Code Editor for MCU Flash GUI.
Specialized for writing Arduino/C++ code (.ino / .cpp / .h).
"""

import os
import sys
import argparse

try:
    # pyrefly: ignore [missing-import]
    from PyQt5.QtCore import Qt, QTimer as QTimer_cls
    # pyrefly: ignore [missing-import]
    from PyQt5.QtGui import QColor, QFont, QFontInfo, QKeySequence
    # pyrefly: ignore [missing-import]
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QTabWidget, QAction, QFileDialog,
        QMessageBox, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
        QPushButton, QDialog, QCheckBox, QLabel, QStyle
    )
except ImportError:
    sys.exit(
        "PyQt5 is required. Install with: pip install PyQt5 PyQt5-QScintilla"
    )

try:
    # pyrefly: ignore [missing-import]
    from PyQt5.Qsci import QsciScintilla, QsciLexerCPP, QsciAPIs
except ImportError:
    sys.exit(
        "PyQt5-QScintilla is required. Install with: pip install PyQt5-QScintilla"
    )

# Windows-only window message constants and registrations for communication
WM_MCU_SAVE_ALL = 0
WM_MCU_RELOAD_ALL = 0
WM_MCU_SET_EMBEDDED = 0
WM_MCU_RUN_ACTION = 0
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes
        WM_MCU_SAVE_ALL = ctypes.windll.user32.RegisterWindowMessageW("MCU_Flash_Save_All")
        WM_MCU_RELOAD_ALL = ctypes.windll.user32.RegisterWindowMessageW("MCU_Flash_Reload_All")
        WM_MCU_SET_EMBEDDED = ctypes.windll.user32.RegisterWindowMessageW("MCU_Flash_Set_Embedded")
        WM_MCU_RUN_ACTION = ctypes.windll.user32.RegisterWindowMessageW("MCU_Flash_Run_Action")
    except Exception:
        pass

# Scintilla indicator constants
SCI_INDICSETSTYLE = 2028
SCI_INDICSETFORE = 2029
SCI_SETINDICATORCURRENT = 2500
SCI_INDICATORCLEARRANGE = 2501
SCI_INDICATORFILLRANGE = 2502
SCI_POSITIONFROMLINE = 2167
SCI_GETTEXTLENGTH = 2183

# --------------------------------------------------------------------------
# Arduino API Vocabulary
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Lexer: standard C++ lexing with custom Arduino keywords
# --------------------------------------------------------------------------
class ArduinoLexer(QsciLexerCPP):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFoldComments(True)
        self.setFoldPreprocessor(True)

    def keywords(self, kw_set):
        if kw_set == 2:
            return " ".join(ARDUINO_KEYWORDS_SET2)
        return super().keywords(kw_set)

    def description(self, style):
        if style == QsciLexerCPP.KeywordSet2:
            return "Arduino API"
        return super().description(style)


# --------------------------------------------------------------------------
# Theme: Curated dark theme matching MCU Flash GUI
# --------------------------------------------------------------------------
THEME = {
    "background":       "#0a0e14",  # Theme.BG_DARKEST
    "foreground":       "#c8d2dc",  # Theme.TEXT
    "line_number":      "#6b7d94",  # Theme.TEXT_DIM
    "margin_bg":        "#10151c",  # Theme.BG_DARK
    "caret_line":       "#161d27",  # Theme.BG_MID
    "selection":        "#243040",  # Theme.BG_HOVER
    "comment":          "#6b7d94",  # Theme.TEXT_DIM
    "string":           "#5ccc6e",  # Theme.GREEN
    "number":           "#e8b83a",  # Theme.YELLOW
    "keyword":          "#f05050",  # Theme.RED (C++ keywords)
    "arduino_api":      "#39c5bb",  # Theme.CYAN (Arduino functions/constants)
    "identifier":       "#e8edf3",  # Theme.TEXT_BRIGHT
    "operator":         "#39c5bb",  # Theme.CYAN
    "preprocessor":     "#e8b83a",  # Theme.YELLOW
    "matched_brace_bg": "#1c2532",  # Theme.BG_LIGHT
    "matched_brace_fg": "#39c5bb",  # Theme.CYAN
}


# --------------------------------------------------------------------------
# ArduinoEditor Widget
# --------------------------------------------------------------------------
class ArduinoEditor(QsciScintilla):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.file_path = None
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.StrongFocus)

        self._configure_font()
        self._configure_lexer()
        self._configure_margins()
        self._configure_folding()
        self._configure_braces()
        self._configure_indentation()
        self._configure_caret_and_selection()
        self._configure_autocomplete()
        self._configure_edge_mode()
        self._configure_performance()
        self._configure_indicators()

        self.textChanged.connect(self._on_text_changed)
        self._syntax_timer = QTimer_cls(self)
        self._syntax_timer.setSingleShot(True)
        self._syntax_timer.timeout.connect(self._run_local_syntax_check)

        # Keyboard-only zoom shortcuts (Ctrl+Plus/Minus/Equal)
        # pyrefly: ignore [missing-import]
        from PyQt5.QtWidgets import QShortcut
        QShortcut(QKeySequence("Ctrl++"), self, self.zoomIn)
        QShortcut(QKeySequence("Ctrl+="), self, self.zoomIn)
        QShortcut(QKeySequence("Ctrl+-"), self, self.zoomOut)

    def mousePressEvent(self, event):
        """Ensure this widget has Qt focus when clicked — critical for
        cross-process embedding where Windows delivers mouse events but
        Qt's internal focus tracking may not follow."""
        self.setFocus(Qt.MouseFocusReason)
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
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
        self.lexer = ArduinoLexer(self)
        self.lexer.setDefaultFont(self._base_font)
        self.lexer.setDefaultColor(QColor(THEME["foreground"]))
        self.lexer.setDefaultPaper(QColor(THEME["background"]))

        style_colors = {
            self.lexer.Comment: THEME["comment"],
            self.lexer.CommentLine: THEME["comment"],
            self.lexer.CommentDoc: THEME["comment"],
            self.lexer.Number: THEME["number"],
            self.lexer.Keyword: THEME["keyword"],
            self.lexer.DoubleQuotedString: THEME["string"],
            self.lexer.SingleQuotedString: THEME["string"],
            self.lexer.PreProcessor: THEME["preprocessor"],
            self.lexer.Operator: THEME["operator"],
            self.lexer.Identifier: THEME["identifier"],
            self.lexer.KeywordSet2: THEME["arduino_api"],
        }
        for style, color in style_colors.items():
            self.lexer.setColor(QColor(color), style)
            self.lexer.setPaper(QColor(THEME["background"]), style)
            self.lexer.setFont(self._base_font, style)

        self.setLexer(self.lexer)
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

    def _configure_indentation(self):
        self.setIndentationsUseTabs(False)
        self.setTabWidth(2)
        self.setIndentationGuides(False)
        self.setAutoIndent(True)
        self.setBackspaceUnindents(True)
        self.setTabIndents(True)

    def _configure_caret_and_selection(self):
        self.setCaretLineVisible(True)
        self.setCaretLineBackgroundColor(QColor(THEME["caret_line"]))
        self.setCaretForegroundColor(QColor(THEME["foreground"]))
        self.setSelectionBackgroundColor(QColor(THEME["selection"]))
        self.setSelectionForegroundColor(QColor(THEME["foreground"]))

    def _configure_autocomplete(self):
        self.api = QsciAPIs(self.lexer)
        cpp_keywords = self.lexer.keywords(1) or ""
        for word in cpp_keywords.split() + ARDUINO_KEYWORDS_SET2:
            self.api.add(word)
        # Defer the expensive index build so the editor is interactive immediately.
        # On low-end devices, prepare() can block for 200-800ms per editor widget.
        # pyrefly: ignore [missing-import]
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(600, self.api.prepare)

        # AcsAPIs = use only the pre-built keyword index, not full-document scan.
        # AcsAll scans the entire document on every keystroke — too expensive on
        # low-end hardware for large .ino files.
        self.setAutoCompletionSource(QsciScintilla.AcsAPIs)
        self.setAutoCompletionThreshold(3)
        self.setAutoCompletionCaseSensitivity(False)
        self.setAutoCompletionReplaceWord(False)
        self.setCallTipsStyle(QsciScintilla.CallTipsNoContext)

    def _configure_edge_mode(self):
        self.setEdgeMode(QsciScintilla.EdgeNone)

    def _configure_performance(self):
        """Low-level Scintilla tweaks for responsive editing on low-end hardware."""
        # NOTE: Do NOT enable SCI_SETTECHNOLOGY / DirectWrite here.
        # DirectWrite (SC_TECHNOLOGY_DIRECTWRITE = 1) uses GPU-accelerated
        # text rendering, but on low-end / integrated GPUs it can silently
        # break keyboard input handling inside the Scintilla control,
        # making the editor appear functional but uneditable.  GDI (the
        # default, value 0) is universally compatible.
        #
        # Spread syntax-highlight work across idle cycles instead of doing it
        # all synchronously after each edit.  SC_IDLESTYLING_ALL = 2
        try:
            self.SendScintilla(self.SCI_SETIDLESTYLING, 2)
        except Exception:
            pass
        # Cache layout measurements for the visible page (SC_CACHE_PAGE = 3)
        # to avoid redundant text-measurement calls during scrolling/editing.
        try:
            self.SendScintilla(self.SCI_SETLAYOUTCACHE, 3)
        except Exception:
            pass

    def _configure_indicators(self):
        # Setup indicator 8 (Error: Red wavy underline)
        self.SendScintilla(SCI_INDICSETSTYLE, 8, 3) # INDIC_SQUIGGLY = 3
        self.SendScintilla(SCI_INDICSETFORE, 8, QColor(220, 50, 50)) # Red color

        # Setup indicator 9 (Warning: Orange wavy underline)
        self.SendScintilla(SCI_INDICSETSTYLE, 9, 3) # INDIC_SQUIGGLY = 3
        self.SendScintilla(SCI_INDICSETFORE, 9, QColor(220, 150, 50)) # Orange color

    def set_syntax_indicators(self, errors):
        length = self.SendScintilla(SCI_GETTEXTLENGTH)
        self.SendScintilla(SCI_SETINDICATORCURRENT, 8)
        self.SendScintilla(SCI_INDICATORCLEARRANGE, 0, length)
        self.SendScintilla(SCI_SETINDICATORCURRENT, 9)
        self.SendScintilla(SCI_INDICATORCLEARRANGE, 0, length)
        
        text = self.text()
        text_len = len(text)
        
        for err in errors:
            line = err.get("line", 1) - 1
            col = err.get("col", 1) - 1
            severity = err.get("severity", "error")
            
            line_start = self.SendScintilla(SCI_POSITIONFROMLINE, line)
            if line_start < 0:
                continue
            pos = line_start + col
            if pos >= text_len:
                continue
                
            # Determine word/token length to highlight
            indicator_len = 1
            idx = pos
            while idx < text_len and (text[idx].isalnum() or text[idx] == '_'):
                idx += 1
            word_len = idx - pos
            if word_len > 0:
                indicator_len = word_len
                
            indic_num = 8 if severity == "error" else 9
            self.SendScintilla(SCI_SETINDICATORCURRENT, indic_num)
            self.SendScintilla(SCI_INDICATORFILLRANGE, pos, indicator_len)

    def load_file(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            self.setText(f.read())
        self.file_path = path
        self.setModified(False)
        # Clear the undo stack so Ctrl+Z can't undo past the loaded content.
        # Without this, setText() is itself an undoable operation and the
        # first Ctrl+Z after opening a file empties the entire editor.
        self.SendScintilla(self.SCI_EMPTYUNDOBUFFER)

    def save_file(self, path=None):
        path = path or self.file_path
        if not path:
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.text())
        self.file_path = path
        self.setModified(False)
        return True

    def _on_text_changed(self):
        self._syntax_timer.start(300)

    def _run_local_syntax_check(self):
        if not self.file_path:
            return
        # Only check C++ / Arduino files
        if not (self.file_path.endswith('.ino') or self.file_path.endswith('.cpp') or self.file_path.endswith('.h')):
            self.set_syntax_indicators([])
            return
            
        try:
            from syntax_checker import analyze_cpp_syntax, extract_project_functions
            from pathlib import Path
            import json
            
            p = Path(self.file_path)
            project_dir = p.parent
            defined_funcs = extract_project_functions(project_dir)
            errors = analyze_cpp_syntax(self.text(), p, defined_funcs)
            
            # Apply indicators locally in QScintilla (real-time wavy underlines!)
            self.set_syntax_indicators(errors)
            
            # Keep syntax state beside the project, under the one hidden cache
            # folder used by the main GUI.  The parent folder is hidden once by
            # the project manager, so individual generated files need no flags.
            project_cache_dir = project_dir / ".mcu_flasher_build_cache"
            project_cache_dir.mkdir(parents=True, exist_ok=True)
            err_file = project_cache_dir / ".mcu_flash_syntax_errors.json"
            # Migrate the old root file only when the new cache has no copy.
            legacy_err = project_dir / ".mcu_flash_syntax_errors.json"
            if legacy_err.exists() and not err_file.exists():
                try:
                    os.replace(str(legacy_err), str(err_file))
                except Exception:
                    pass
            try:
                existing_errors = []
                if err_file.exists():
                    try:
                        with open(err_file, "r", encoding="utf-8") as f:
                            existing_errors = json.load(f)
                    except Exception:
                        pass
                
                # Replace entries for this file
                updated_errors = [e for e in existing_errors if e.get("file") != p.name]
                updated_errors.extend(errors)
                
                with open(err_file, "w", encoding="utf-8") as f:
                    json.dump(updated_errors, f, indent=2)
            except Exception:
                pass
        except Exception:
            pass


# --------------------------------------------------------------------------
# Find & Replace Dialog
# --------------------------------------------------------------------------
class FindReplaceDialog(QDialog):
    def __init__(self, editor_getter, parent=None):
        super().__init__(parent)
        self.editor_getter = editor_getter
        self.setWindowTitle("Find & Replace")
        self.setModal(False)

        self.find_edit = QLineEdit()
        self.replace_edit = QLineEdit()
        self.case_box = QCheckBox("Match case")
        self.regex_box = QCheckBox("Regex")

        find_btn = QPushButton("Find Next")
        replace_btn = QPushButton("Replace")
        replace_all_btn = QPushButton("Replace All")

        find_btn.clicked.connect(self.find_next)
        replace_btn.clicked.connect(self.replace_one)
        replace_all_btn.clicked.connect(self.replace_all)

        layout = QVBoxLayout(self)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Find:"))
        row1.addWidget(self.find_edit)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Replace:"))
        row2.addWidget(self.replace_edit)
        row3 = QHBoxLayout()
        row3.addWidget(self.case_box)
        row3.addWidget(self.regex_box)
        row4 = QHBoxLayout()
        row4.addWidget(find_btn)
        row4.addWidget(replace_btn)
        row4.addWidget(replace_all_btn)

        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        layout.addLayout(row4)

        # Style find/replace dialog
        self.setStyleSheet(f"""
            QDialog {{ background: {THEME['margin_bg']}; color: {THEME['foreground']}; }}
            QLabel {{ color: {THEME['foreground']}; }}
            QLineEdit {{ background: {THEME['background']}; color: {THEME['foreground']}; border: 1px solid {THEME['selection']}; padding: 2px; }}
            QPushButton {{ background: {THEME['caret_line']}; color: {THEME['foreground']}; border: 1px solid {THEME['selection']}; padding: 4px 8px; }}
            QPushButton:hover {{ background: {THEME['selection']}; }}
            QCheckBox {{ color: {THEME['foreground']}; }}
        """)

    def find_next(self):
        editor = self.editor_getter()
        if not editor:
            return
        editor.findFirst(
            self.find_edit.text(), self.regex_box.isChecked(),
            self.case_box.isChecked(), False, True, forward=True
        )

    def replace_one(self):
        editor = self.editor_getter()
        if not editor or not editor.hasSelectedText():
            self.find_next()
            return
        editor.replaceSelectedText(self.replace_edit.text())
        self.find_next()

    def replace_all(self):
        editor = self.editor_getter()
        if not editor:
            return
        text = self.find_edit.text()
        if not text:
            return
        found = editor.findFirst(text, self.regex_box.isChecked(),
                                  self.case_box.isChecked(), False, False,
                                  forward=True, line=0, index=0)
        count = 0
        while found:
            editor.replaceSelectedText(self.replace_edit.text())
            found = editor.findNext()
            count += 1
        QMessageBox.information(self, "Replace All", f"Replaced {count} occurrence(s).")


# --------------------------------------------------------------------------
# MainWindow
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, embedded=False, project_dir=None, session=""):
        super().__init__()
        self.started_embedded = embedded
        self.embedded = embedded
        self.project_dir = project_dir
        self.session = session

        self.setWindowTitle(f"MCU Flasher — Embedded QScintilla Editor (Session: {session})")
        self.resize(1000, 600)
        self.setStyleSheet(self._stylesheet())

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(False)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._update_title)
        self.setCentralWidget(self.tabs)

        if self.layout():
            self.layout().setContentsMargins(0, 0, 0, 0)
            self.layout().setSpacing(0)

        # Always build menu and toolbar so they are available when detached
        self._build_menu_and_toolbar()

        if embedded:
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.Tool)
            self.move(-10000, -10000)
            self.menuBar().hide()
            self.statusBar().hide()
            if hasattr(self, "action_toolbar"):
                self.action_toolbar.hide()
            self.tabs.setDocumentMode(True)
            self.tabs.setContentsMargins(0, 0, 0, 0)
            self.setContentsMargins(0, 0, 0, 0)
            # When embedded cross-process, Qt's focus tracking can lose
            # sync with Win32 focus.  Poll periodically and fix it.
            self._focus_timer = QTimer_cls(self)
            self._focus_timer.timeout.connect(self._ensure_editor_focus)
            self._focus_timer.start(100)

        # Connect tab movement signal to save tab order
        self.tabs.tabBar().tabMoved.connect(self.on_tab_moved)
        
        self.find_dialog = FindReplaceDialog(self.current_editor, self)
        
        # Load directory or files if specified
        if project_dir:
            self.load_directory(project_dir)
        else:
            self.new_tab()

        # Keyboard shortcuts
        self._setup_shortcuts()

    def showEvent(self, event):
        super().showEvent(event)
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(self.winId())
                style = ctypes.windll.user32.GetWindowLongW(hwnd, -16)
                is_child = bool(style & 0x40000000)
                if is_child:
                    if not self.embedded:
                        self.set_embedded_state(True)
                else:
                    if self.embedded:
                        self.set_embedded_state(False)
            except Exception:
                pass

    def _stylesheet(self):
        return f"""
            QMainWindow {{ background: {THEME['background']}; margin: 0px; padding: 0px; border: none; }}
            QMenuBar, QMenu {{
                background: {THEME['margin_bg']};
                color: {THEME['foreground']};
                border: none;
            }}
            QMenuBar::item:selected, QMenu::item:selected {{ background: {THEME['selection']}; }}
            QToolBar {{ background: {THEME['margin_bg']}; border: none; spacing: 4px; padding: 4px; }}
            QTabWidget {{ margin: 0px; padding: 0px; border: none; }}
            QTabWidget::pane {{ border: none; margin: 0px; padding: 0px; background: {THEME['background']}; }}
            QTabBar::tab {{
                background: {THEME['margin_bg']};
                color: {THEME['foreground']};
                padding: 6px 12px;
                border-right: 1px solid {THEME['background']};
            }}
            QTabBar::tab:selected {{ background: {THEME['background']}; color: {THEME['foreground']}; border-bottom: 2px solid {THEME['arduino_api']}; }}
            QTabBar::tab:hover {{ background: {THEME['selection']}; }}
            QScrollBar:vertical {{
                border: none;
                background: {THEME['background']};
                width: 10px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical {{
                background: {THEME['selection']};
                min-height: 20px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {THEME['line_number']};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                border: none;
                background: none;
                height: 0px;
            }}
            QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical {{
                border: none;
                background: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """

    def _build_menu_and_toolbar(self):
        menubar = self.menuBar()
        menubar.hide()

        self.action_toolbar = self.addToolBar("Actions")
        self.action_toolbar.setMovable(False)
        self.action_toolbar.setFloatable(False)
        self.action_toolbar.setStyleSheet(
            f"QToolBar {{ background: {THEME['margin_bg']}; border: none; spacing: 4px; padding: 6px; }}"
        )

        label = QLabel("ACTIONS")
        label.setStyleSheet(f"color: #7f8c8d; font-weight: 700; font-size: 11px; "
                            f"background: transparent; padding-right: 6px;")
        self.action_toolbar.addWidget(label)

        def _add_action_btn(text, color, action_id):
            btn = QPushButton(text)
            btn.setStyleSheet(
                f"QPushButton {{ "
                f"  background: {color}; color: #f4f7fb; border: 0; "
                f"  padding: 6px 12px; font: 600 11px 'Montserrat', 'Segoe UI', sans-serif; "
                f"}}"
                f"QPushButton:hover {{ filter: brightness(1.18); }}"
            )
            btn.clicked.connect(lambda checked, aid=action_id: self._dispatch_action(aid))
            self.action_toolbar.addWidget(btn)
            return btn

        _add_action_btn("Compile", "#2d7d46", "compile")
        _add_action_btn("Upload", "#2077b0", "upload")
        _add_action_btn("Stop", "#a03030", "stop")
        _add_action_btn("Clean", "#3a4555", "clean")

        div = QLabel("")
        div.setStyleSheet(f"background: #222938; min-width: 2px; max-width: 2px; "
                          f"min-height: 22px; margin: 0 6px;")
        self.action_toolbar.addWidget(div)

        _add_action_btn("Save", "#2d7d46", "save")
        _add_action_btn("Save All", "#8244a0", "save_all")
        _add_action_btn("Reload", "#3a4555", "reload")
        _add_action_btn("Modify", "#1a7a70", "modify")

    def _setup_shortcuts(self):
        # Save shortcut
        save_shortcut = QAction(self)
        save_shortcut.setShortcut(QKeySequence("Ctrl+S"))
        save_shortcut.triggered.connect(self.save_file)
        self.addAction(save_shortcut)

        # Find shortcut
        find_shortcut = QAction(self)
        find_shortcut.setShortcut(QKeySequence("Ctrl+F"))
        find_shortcut.triggered.connect(self.show_find_dialog)
        self.addAction(find_shortcut)

        # Copy shortcut
        copy_shortcut = QAction(self)
        copy_shortcut.setShortcut(QKeySequence("Ctrl+C"))
        copy_shortcut.triggered.connect(lambda *args: self.current_editor() and self.current_editor().SendScintilla(2178))
        self.addAction(copy_shortcut)

        # Paste shortcut
        paste_shortcut = QAction(self)
        paste_shortcut.setShortcut(QKeySequence("Ctrl+V"))
        paste_shortcut.triggered.connect(lambda *args: self.current_editor() and self.current_editor().SendScintilla(2179))
        self.addAction(paste_shortcut)

        # Cut shortcut
        cut_shortcut = QAction(self)
        cut_shortcut.setShortcut(QKeySequence("Ctrl+X"))
        cut_shortcut.triggered.connect(lambda *args: self.current_editor() and self.current_editor().SendScintilla(2177))
        self.addAction(cut_shortcut)

        # Undo shortcut
        undo_shortcut = QAction(self)
        undo_shortcut.setShortcut(QKeySequence("Ctrl+Z"))
        undo_shortcut.triggered.connect(lambda *args: self.current_editor() and self.current_editor().undo())
        self.addAction(undo_shortcut)

        # Redo shortcut
        redo_shortcut = QAction(self)
        redo_shortcut.setShortcut(QKeySequence("Ctrl+Y"))
        redo_shortcut.triggered.connect(lambda *args: self.current_editor() and self.current_editor().redo())
        self.addAction(redo_shortcut)

        # Select All shortcut
        select_all_shortcut = QAction(self)
        select_all_shortcut.setShortcut(QKeySequence("Ctrl+A"))
        select_all_shortcut.triggered.connect(lambda *args: self.current_editor() and self.current_editor().selectAll())
        self.addAction(select_all_shortcut)

    def current_editor(self):
        return self.tabs.currentWidget()

    def new_tab(self, path=None):
        editor = ArduinoEditor()
        editor.modificationChanged.connect(lambda _m: self._update_title())

        if path:
            try:
                editor.load_file(path)
                title = os.path.basename(path)
            except Exception as e:
                QMessageBox.warning(self, "Load Error", f"Failed to load file {path}: {e}")
                return None
        else:
            editor.setText(
                "void setup() {\n"
                "  // put your setup code here, to run once:\n\n"
                "}\n\n"
                "void loop() {\n"
                "  // put your main code here, to run repeatedly:\n\n"
                "}\n"
            )
            editor.setModified(False)
            title = "untitled.ino"

        index = self.tabs.addTab(editor, title)
        self.tabs.setCurrentIndex(index)
        editor.setFocus()
        return editor

    def close_tab(self, index):
        editor = self.tabs.widget(index)
        if editor.isModified():
            resp = QMessageBox.question(
                self, "Unsaved changes",
                f"Save changes to {self.tabs.tabText(index)}?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
            )
            if resp == QMessageBox.Save:
                self.save_file()
            elif resp == QMessageBox.Cancel:
                return
        self.tabs.removeTab(index)
        if self.tabs.count() == 0:
            self.new_tab()

    def _dispatch_action(self, action_id):
        if sys.platform == "win32" and WM_MCU_RUN_ACTION != 0:
            try:
                import ctypes
                my_hwnd = int(self.winId())
                actions_map = {
                    "compile": 1, "upload": 2, "stop": 3, "clean": 4,
                    "save": 5, "save_all": 6, "reload": 7, "modify": 8,
                }
                action_code = actions_map.get(action_id, 0)
                if action_code:
                    ctypes.windll.user32.PostMessageW(my_hwnd, WM_MCU_RUN_ACTION, action_code, 0)
            except Exception:
                pass

    def _update_title(self):
        editor = self.current_editor()
        if not editor:
            return
        index = self.tabs.currentIndex()
        name = os.path.basename(editor.file_path) if editor.file_path else "untitled.ino"
        if editor.isModified():
            name += " *"
            self.tabs.setTabText(index, name)
        else:
            self.tabs.setTabText(index, name)
        
        if self.embedded:
            self.setWindowTitle(f"{name} — Embedded QScintilla Editor (Session: {self.session})")
            self.repaint()
        else:
            self.setWindowTitle(f"{name} — MCU Flasher Editor")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.embedded and hasattr(self, "tabs") and self.tabs:
            self.tabs.setGeometry(0, 0, self.width(), self.height())
            self.tabs.repaint()


    def open_file(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open File", "",
            "Arduino / C++ Files (*.ino *.cpp *.h *.hpp *.c *.txt);;All Files (*)"
        )
        for path in paths:
            self.new_tab(path)

    def save_file(self):
        editor = self.current_editor()
        if not editor:
            return
        if not editor.file_path:
            self.save_file_as()
        else:
            editor.save_file()
            self._update_title()

    def save_file_as(self):
        editor = self.current_editor()
        if not editor:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save File", editor.file_path or "sketch.ino",
            "Arduino Sketch (*.ino);;C++ Source (*.cpp);;Header (*.h);;All Files (*)"
        )
        if path:
            editor.save_file(path)
            self._update_title()

    def show_find_dialog(self):
        self.find_dialog.show()
        self.find_dialog.raise_()
        self.find_dialog.activateWindow()

    def save_all_files(self):
        for i in range(self.tabs.count()):
            editor = self.tabs.widget(i)
            if editor and editor.isModified() and editor.file_path:
                try:
                    editor.save_file()
                except Exception:
                    pass
        self._update_title()

    def reload_all_files(self):
        # Reload unchanged files, or if they were modified externally
        for i in range(self.tabs.count()):
            editor = self.tabs.widget(i)
            if editor and editor.file_path:
                if not editor.isModified():
                    try:
                        editor.load_file(editor.file_path)
                    except Exception:
                        pass
        self._update_title()

    def on_tab_moved(self, from_idx, to_idx):
        if not self.project_dir:
            return
        paths = []
        for i in range(self.tabs.count()):
            editor = self.tabs.widget(i)
            if editor and editor.file_path:
                try:
                    rel = os.path.relpath(editor.file_path, self.project_dir)
                except Exception:
                    rel = editor.file_path
                paths.append(rel)
        cache_dir = os.path.join(self.project_dir, ".mcu_flasher_build_cache")
        os.makedirs(cache_dir, exist_ok=True)
        order_file = os.path.join(cache_dir, ".mcu_flash_tab_order.json")
        try:
            import json
            with open(order_file, "w", encoding="utf-8") as f:
                json.dump(paths, f, indent=2)
        except Exception:
            pass

    def load_directory(self, dir_path):
        if not dir_path or not os.path.isdir(dir_path):
            return
        self.project_dir = dir_path
        
        # Scan files
        files = []
        for ext in ("*.ino", "*.cpp", "*.c", "*.h", "*.txt"):
            import glob
            files.extend(glob.glob(os.path.join(dir_path, ext)))
        files = [os.path.abspath(f) for f in files]
        
        # Load tab order
        cache_dir = os.path.join(dir_path, ".mcu_flasher_build_cache")
        order_file = os.path.join(cache_dir, ".mcu_flash_tab_order.json")
        legacy_order_file = os.path.join(dir_path, ".mcu_flash_tab_order.json")
        if os.path.isfile(legacy_order_file) and not os.path.isfile(order_file):
            try:
                os.makedirs(cache_dir, exist_ok=True)
                os.replace(legacy_order_file, order_file)
            except OSError:
                pass
        ordered_files = []
        if os.path.exists(order_file):
            try:
                import json
                with open(order_file, "r", encoding="utf-8") as f:
                    saved_order = json.load(f)
                file_map = {os.path.basename(f): f for f in files}
                for name in saved_order:
                    if name in file_map:
                        ordered_files.append(file_map.pop(name))
                ordered_files.extend(file_map.values())
                files = ordered_files
            except Exception:
                pass
        
        # Keep track of existing open files
        existing_paths = {}
        for i in range(self.tabs.count()):
            editor = self.tabs.widget(i)
            if editor and editor.file_path:
                existing_paths[os.path.abspath(editor.file_path)] = i
                
        # Remove tabs for deleted files
        tabs_to_remove = []
        for path, idx in list(existing_paths.items()):
            if path not in files:
                tabs_to_remove.append(idx)
        for idx in sorted(tabs_to_remove, reverse=True):
            self.tabs.removeTab(idx)
            
        # Re-build existing_paths map
        existing_paths = {}
        for i in range(self.tabs.count()):
            editor = self.tabs.widget(i)
            if editor and editor.file_path:
                existing_paths[os.path.abspath(editor.file_path)] = editor
                
        self.tabs.blockSignals(True)
        active_editor = self.current_editor()
        active_path = active_editor.file_path if active_editor else None
        
        # Extract widgets
        widgets_by_path = {}
        for path in files:
            if path in existing_paths:
                widgets_by_path[path] = existing_paths[path]
                
        # Clear QTabWidget tabs without deleting the widgets
        self.tabs.clear()
        
        # Re-add in correct order
        for path in files:
            if path in widgets_by_path:
                editor = widgets_by_path[path]
                title = os.path.basename(path)
                if editor.isModified():
                    title += " *"
                self.tabs.addTab(editor, title)
            else:
                self.new_tab(path)
                
        # Restore active tab
        if active_path:
            for i in range(self.tabs.count()):
                editor = self.tabs.widget(i)
                if editor and os.path.abspath(editor.file_path) == os.path.abspath(active_path):
                    self.tabs.setCurrentIndex(i)
                    editor.setFocus()
                    break
        elif self.tabs.count() > 0:
            self.tabs.setCurrentIndex(0)
            
        self.tabs.blockSignals(False)
        self._update_title()

    def _ensure_editor_focus(self):
        """Called periodically when embedded.  If the native Win32
        focus is on this window (or one of its children) but Qt's internal
        focus widget isn't one of our editors, fix it.  This closes the
        gap where cross-process reparenting delivers WM_SETFOCUS to the
        HWND but Qt never translates it into a QFocusEvent."""
        try:
            import ctypes
            focused_hwnd = ctypes.windll.user32.GetFocus()
            if not focused_hwnd:
                return
            my_hwnd = int(self.winId())
            if focused_hwnd != my_hwnd:
                parent = ctypes.windll.user32.GetAncestor(focused_hwnd, 1)  # GA_PARENT
                if parent != my_hwnd:
                    return
            editor = self.tabs.currentWidget()
            if editor and not editor.hasFocus():
                editor.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    # native Win32 event handler in Qt for interprocess control
    def nativeEvent(self, eventType, message):
        if eventType == b"windows_generic_MSG" and sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                msg = ctypes.wintypes.MSG.from_address(int(message))
                # WM_SETFOCUS = 0x0007 — forward Qt focus to the active
                # editor tab when the native window receives keyboard
                # focus.  Without this, cross-process reparenting often
                # delivers WM_SETFOCUS to the HWND but Qt's internal
                # focus tracking doesn't follow, so keystrokes are lost.
                if msg.message == 0x0007:
                    editor = self.tabs.currentWidget()
                    if editor:
                        editor.setFocus(Qt.OtherFocusReason)
                if msg.message == WM_MCU_SAVE_ALL and WM_MCU_SAVE_ALL != 0:
                    self.save_all_files()
                    return True, 0
                elif msg.message == WM_MCU_RELOAD_ALL and WM_MCU_RELOAD_ALL != 0:
                    try:
                        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gui_config.json")
                        if os.path.exists(config_path):
                            import json
                            with open(config_path, "r", encoding="utf-8") as f:
                                config = json.load(f)
                                new_project_dir = config.get("last_sketch_dir")
                                if new_project_dir and os.path.isdir(new_project_dir):
                                    self.project_dir = new_project_dir
                    except Exception:
                        pass
                    self.reload_all_files()
                    if self.project_dir:
                        self.load_directory(self.project_dir)
                    return True, 0
                elif msg.message == WM_MCU_SET_EMBEDDED and WM_MCU_SET_EMBEDDED != 0:
                    embedded_state = bool(msg.wParam)
                    self.set_embedded_state(embedded_state)
                    return True, 0
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def set_embedded_state(self, embedded):
        if self.embedded == embedded:
            return
        self.embedded = embedded
        self.tabs.setTabsClosable(False)
        if embedded:
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.Tool)
            self.menuBar().hide()
            self.statusBar().hide()
            if hasattr(self, "action_toolbar"):
                self.action_toolbar.hide()
            self.tabs.setDocumentMode(True)
            self.tabs.setContentsMargins(0, 0, 0, 0)
            self.setContentsMargins(0, 0, 0, 0)
            self.show()
            editor = self.tabs.currentWidget()
            if editor:
                editor.setFocus(Qt.OtherFocusReason)
        else:
            self.setWindowFlags(Qt.Window)
            self.menuBar().hide()
            self.statusBar().show()
            if hasattr(self, "action_toolbar"):
                self.action_toolbar.show()
            self.tabs.setDocumentMode(False)
            self.show()
            editor = self.tabs.currentWidget()
            if editor:
                QTimer_cls.singleShot(100, lambda: editor.setFocus(Qt.OtherFocusReason))

    def closeEvent(self, event):
        if self.started_embedded:
            self.hide()
            event.ignore()
            return
        
        for i in range(self.tabs.count()):
            editor = self.tabs.widget(i)
            if editor.isModified():
                self.tabs.setCurrentIndex(i)
                resp = QMessageBox.question(
                    self, "Unsaved changes",
                    f"Save changes to {self.tabs.tabText(i)} before closing?",
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
                )
                if resp == QMessageBox.Save:
                    self.save_file()
                elif resp == QMessageBox.Cancel:
                    event.ignore()
                    return
        event.accept()


def main():
    # Set DPI awareness on Windows to match main GUI
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--embedded", action="store_true")
    parser.add_argument("--dir", type=str, default="")
    parser.add_argument("--session", type=str, default="")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    
    # Hide taskbar icon if embedded in main window
    if args.embedded and sys.platform == "win32":
        # We want to make sure it doesn't float separately
        pass

    window = MainWindow(embedded=args.embedded, project_dir=args.dir, session=args.session)

    # If specific files are passed (standalone mode)
    if not args.embedded and args.files:
        valid_files = [f for f in args.files if os.path.isfile(f)]
        if valid_files:
            if hasattr(window, 'tabs'):
                window.tabs.clear()
            elif hasattr(window, 'tabWidget'):
                window.tabWidget.clear()
            elif hasattr(window, 'tab_widget'):
                window.tab_widget.clear()
                
            for path in valid_files:
                window.new_tab(path)

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
