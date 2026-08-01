#!/usr/bin/env python3
"""
Arduino C++ Code Editor
=======================

A desktop code editor built with PyQt5 + QScintilla (QsciScintilla),
specialized for writing Arduino sketches (.ino / .cpp / .h).

Features
--------
- C++ syntax highlighting via QsciLexerCPP, extended with a dedicated
  keyword set for Arduino core functions, constants and types
  (setup, loop, digitalWrite, HIGH, LOW, INPUT_PULLUP, byte, String, ...)
  so they are colored differently from plain C++ keywords.
- Line numbers, code folding, brace matching, current-line highlight,
  indentation guides, auto-indent, and a monokai-style dark theme.
- Autocompletion + call tips seeded with the Arduino API.
- Multi-file editing via tabs.
- New / Open / Save / Save As / Find & Replace.
- Optional "Compile" and "Upload" actions that shell out to `arduino-cli`
  (https://arduino.github.io/arduino-cli) if it is installed, with
  output streamed live into a bottom output panel. These are best-effort:
  if arduino-cli isn't found, the app tells you how to install it instead
  of crashing.

Dependencies
------------
    pip install PyQt5 PyQt5-QScintilla

Optional (for Compile/Upload/board & port detection):
    - arduino-cli installed and on PATH
    - pip install pyserial   (to auto-list serial ports)

Run
---
    python arduino_editor.py [file1.ino file2.cpp ...]
"""

import os
import sys

try:
    from PyQt5.QtCore import Qt, QProcess
    from PyQt5.QtGui import QColor, QFont, QFontInfo, QKeySequence, QIcon
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QTabWidget, QAction, QToolBar, QFileDialog,
        QMessageBox, QDockWidget, QPlainTextEdit, QComboBox, QLabel, QWidget,
        QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QDialog, QCheckBox,
        QStatusBar, QStyle
    )
except ImportError:
    sys.exit(
        "PyQt5 is required.\n"
        "Install it with:  pip install PyQt5 PyQt5-QScintilla"
    )

try:
    from PyQt5.Qsci import QsciScintilla, QsciLexerCPP, QsciAPIs
except ImportError:
    sys.exit(
        "PyQt5-QScintilla (the QScintilla Python bindings) is required.\n"
        "Install it with:  pip install PyQt5-QScintilla"
    )


# --------------------------------------------------------------------------
# Arduino API vocabulary
# --------------------------------------------------------------------------

ARDUINO_FUNCTIONS = """
setup loop
pinMode digitalWrite digitalRead analogWrite analogRead analogReference
analogWriteResolution analogReadResolution
tone noTone pulseIn pulseInLong shiftIn shiftOut
attachInterrupt detachInterrupt interrupts noInterrupts
delay delayMicroseconds micros millis
min max abs constrain map pow sqrt sq
sin cos tan
random randomSeed
Serial Serial1 Serial2 Serial3 Wire SPI EEPROM
begin end available read write print println peek flush
push pop
attach detach write writeMicroseconds read
""".split()

ARDUINO_CONSTANTS = """
HIGH LOW INPUT OUTPUT INPUT_PULLUP
LED_BUILTIN
true false TRUE FALSE
PI HALF_PI TWO_PI DEG_TO_RAD RAD_TO_DEG
A0 A1 A2 A3 A4 A5 A6 A7
CHANGE RISING FALLING
DEC BIN HEX OCT
""".split()

ARDUINO_TYPES = """
boolean byte word String Stream Print Printable
uint8_t uint16_t uint32_t uint64_t
int8_t int16_t int32_t int64_t
size_t
""".split()

ARDUINO_KEYWORDS_SET2 = ARDUINO_FUNCTIONS + ARDUINO_CONSTANTS + ARDUINO_TYPES


# --------------------------------------------------------------------------
# Lexer: standard C++ lexing, plus a distinct Arduino keyword set
# --------------------------------------------------------------------------

class ArduinoLexer(QsciLexerCPP):
    """QsciLexerCPP with an extra keyword set for the Arduino API."""

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
# Theme (Monokai-inspired dark theme)
# --------------------------------------------------------------------------

THEME = {
    "background":   "#272822",
    "foreground":   "#F8F8F2",
    "line_number":  "#75715E",
    "margin_bg":    "#2D2E27",
    "caret_line":   "#3E3D32",
    "selection":    "#49483E",
    "comment":      "#75715E",
    "string":       "#E6DB74",
    "number":       "#AE81FF",
    "keyword":      "#F92672",   # C++ keywords: if/for/while/class...
    "arduino_api":  "#66D9EF",   # Arduino functions/constants/types
    "identifier":   "#F8F8F2",
    "operator":     "#F8F8F2",
    "preprocessor": "#FD971F",
    "matched_brace_bg": "#3E3D32",
    "matched_brace_fg": "#A6E22E",
}


# --------------------------------------------------------------------------
# The editor widget itself
# --------------------------------------------------------------------------

class ArduinoEditor(QsciScintilla):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.file_path = None

        self._configure_font()
        self._configure_lexer()
        self._configure_margins()
        self._configure_folding()
        self._configure_braces()
        self._configure_indentation()
        self._configure_caret_and_selection()
        self._configure_autocomplete()
        self._configure_edge_mode()

    # -- setup helpers -----------------------------------------------------

    def _configure_font(self):
        font = QFont("Consolas")
        if not QFontInfo(font).fixedPitch():
            font = QFont("Monospace")
        font.setFixedPitch(True)
        font.setPointSize(11)
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
        # Margin 0: line numbers
        self.setMarginType(0, QsciScintilla.NumberMargin)
        self.setMarginWidth(0, "00000")
        self.setMarginsForegroundColor(QColor(THEME["line_number"]))
        self.setMarginsBackgroundColor(QColor(THEME["margin_bg"]))
        self.setMarginsFont(self._base_font)

    def _configure_folding(self):
        self.setFolding(QsciScintilla.BoxedTreeFoldStyle, 2)
        self.setFoldMarginColors(QColor(THEME["margin_bg"]), QColor(THEME["margin_bg"]))

    def _configure_braces(self):
        self.setBraceMatching(QsciScintilla.SloppyBraceMatch)
        self.setMatchedBraceBackgroundColor(QColor(THEME["matched_brace_bg"]))
        self.setMatchedBraceForegroundColor(QColor(THEME["matched_brace_fg"]))

    def _configure_indentation(self):
        self.setIndentationsUseTabs(False)
        self.setTabWidth(2)
        self.setIndentationGuides(True)
        self.setIndentationGuidesForegroundColor(QColor(THEME["line_number"]))
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
        self.api.prepare()

        self.setAutoCompletionSource(QsciScintilla.AcsAll)
        self.setAutoCompletionThreshold(2)
        self.setAutoCompletionCaseSensitivity(False)
        self.setAutoCompletionReplaceWord(False)
        self.setCallTipsStyle(QsciScintilla.CallTipsNoContext)

    def _configure_edge_mode(self):
        self.setEdgeMode(QsciScintilla.EdgeLine)
        self.setEdgeColumn(100)
        self.setEdgeColor(QColor(THEME["margin_bg"]))

    # -- file helpers --------------------------------------------------

    def load_file(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            self.setText(f.read())
        self.file_path = path
        self.setModified(False)

    def save_file(self, path=None):
        path = path or self.file_path
        if not path:
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.text())
        self.file_path = path
        self.setModified(False)
        return True


# --------------------------------------------------------------------------
# Simple Find & Replace dialog
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
# Main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):

    UNTITLED_TEMPLATE = (
        "void setup() {\n"
        "  // put your setup code here, to run once:\n\n"
        "}\n\n"
        "void loop() {\n"
        "  // put your main code here, to run repeatedly:\n\n"
        "}\n"
    )

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Arduino C++ Editor")
        self.resize(1200, 800)
        self.setStyleSheet(self._stylesheet())

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._update_title)
        self.setCentralWidget(self.tabs)

        self._build_output_dock()
        self._build_menu_and_toolbar()
        self._build_statusbar()

        self.find_dialog = FindReplaceDialog(self.current_editor, self)

        self.compile_process = None

    # -- UI construction -------------------------------------------------

    def _stylesheet(self):
        return f"""
            QMainWindow {{ background: {THEME['background']}; }}
            QMenuBar, QMenu {{
                background: {THEME['margin_bg']};
                color: {THEME['foreground']};
            }}
            QMenu::item:selected {{ background: {THEME['selection']}; }}
            QToolBar {{ background: {THEME['margin_bg']}; border: none; spacing: 4px; }}
            QTabWidget::pane {{ border: 0; }}
            QTabBar::tab {{
                background: {THEME['margin_bg']};
                color: {THEME['foreground']};
                padding: 6px 12px;
            }}
            QTabBar::tab:selected {{ background: {THEME['caret_line']}; }}
            QStatusBar {{ background: {THEME['margin_bg']}; color: {THEME['foreground']}; }}
            QPlainTextEdit {{
                background: #1E1F1C; color: #F8F8F2;
                font-family: Consolas, Monospace;
            }}
            QComboBox, QLineEdit {{
                background: {THEME['caret_line']}; color: {THEME['foreground']};
                border: 1px solid {THEME['selection']}; padding: 2px 4px;
            }}
        """

    def _build_output_dock(self):
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        dock = QDockWidget("Output", self)
        dock.setWidget(self.output)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.output_dock = dock

    def _build_menu_and_toolbar(self):
        style = self.style()

        new_act = QAction(style.standardIcon(QStyle.SP_FileIcon), "New", self)
        new_act.setShortcut(QKeySequence.New)
        new_act.triggered.connect(self.new_tab)

        open_act = QAction(style.standardIcon(QStyle.SP_DialogOpenButton), "Open...", self)
        open_act.setShortcut(QKeySequence.Open)
        open_act.triggered.connect(self.open_file)

        save_act = QAction(style.standardIcon(QStyle.SP_DialogSaveButton), "Save", self)
        save_act.setShortcut(QKeySequence.Save)
        save_act.triggered.connect(self.save_file)

        save_as_act = QAction("Save As...", self)
        save_as_act.setShortcut(QKeySequence.SaveAs)
        save_as_act.triggered.connect(self.save_file_as)

        find_act = QAction("Find/Replace...", self)
        find_act.setShortcut(QKeySequence.Find)
        find_act.triggered.connect(self.show_find_dialog)

        undo_act = QAction("Undo", self)
        undo_act.setShortcut(QKeySequence.Undo)
        undo_act.triggered.connect(lambda: self.current_editor() and self.current_editor().undo())

        redo_act = QAction("Redo", self)
        redo_act.setShortcut(QKeySequence.Redo)
        redo_act.triggered.connect(lambda: self.current_editor() and self.current_editor().redo())

        zoom_in_act = QAction("Zoom In", self)
        zoom_in_act.setShortcut(QKeySequence.ZoomIn)
        zoom_in_act.triggered.connect(lambda: self.current_editor() and self.current_editor().zoomIn())

        zoom_out_act = QAction("Zoom Out", self)
        zoom_out_act.setShortcut(QKeySequence.ZoomOut)
        zoom_out_act.triggered.connect(lambda: self.current_editor() and self.current_editor().zoomOut())

        compile_act = QAction(style.standardIcon(QStyle.SP_DialogApplyButton), "Verify/Compile", self)
        compile_act.triggered.connect(self.compile_sketch)

        upload_act = QAction(style.standardIcon(QStyle.SP_MediaPlay), "Upload", self)
        upload_act.triggered.connect(self.upload_sketch)

        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        for a in (new_act, open_act, save_act, save_as_act):
            file_menu.addAction(a)
        file_menu.addSeparator()
        exit_act = QAction("Exit", self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        edit_menu = menubar.addMenu("&Edit")
        for a in (undo_act, redo_act, find_act):
            edit_menu.addAction(a)

        view_menu = menubar.addMenu("&View")
        for a in (zoom_in_act, zoom_out_act):
            view_menu.addAction(a)
        view_menu.addAction(self.output_dock.toggleViewAction())

        sketch_menu = menubar.addMenu("&Sketch")
        sketch_menu.addAction(compile_act)
        sketch_menu.addAction(upload_act)

        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for a in (new_act, open_act, save_act):
            toolbar.addAction(a)
        toolbar.addSeparator()
        toolbar.addAction(compile_act)
        toolbar.addAction(upload_act)
        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Board: "))
        self.board_box = QComboBox()
        self.board_box.setEditable(True)
        self.board_box.addItems([
            "arduino:avr:uno",
            "arduino:avr:nano",
            "arduino:avr:mega",
            "esp32:esp32:esp32",
            "esp8266:esp8266:nodemcuv2",
        ])
        toolbar.addWidget(self.board_box)

        toolbar.addWidget(QLabel(" Port: "))
        self.port_box = QComboBox()
        self.port_box.setEditable(True)
        self._populate_ports()
        toolbar.addWidget(self.port_box)

        refresh_ports_act = QAction("Refresh Ports", self)
        refresh_ports_act.triggered.connect(self._populate_ports)
        toolbar.addAction(refresh_ports_act)

        self.new_tab()

    def _populate_ports(self):
        self.port_box.clear()
        try:
            import serial.tools.list_ports as list_ports
            ports = [p.device for p in list_ports.comports()]
        except ImportError:
            ports = []
        if not ports:
            ports = ["(install pyserial to auto-detect ports)"]
        self.port_box.addItems(ports)

    def _build_statusbar(self):
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.pos_label = QLabel("Line 1, Col 1")
        self.status.addPermanentWidget(self.pos_label)

    # -- tab / editor management ------------------------------------------

    def current_editor(self):
        return self.tabs.currentWidget()

    def new_tab(self, path=None):
        editor = ArduinoEditor()
        editor.cursorPositionChanged.connect(self._update_position_label)
        editor.modificationChanged.connect(lambda _m: self._update_title())

        if path:
            editor.load_file(path)
            title = os.path.basename(path)
        else:
            editor.setText(self.UNTITLED_TEMPLATE)
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

    def _update_title(self):
        editor = self.current_editor()
        if not editor:
            return
        index = self.tabs.currentIndex()
        name = os.path.basename(editor.file_path) if editor.file_path else "untitled.ino"
        if editor.isModified():
            name += " *"
        self.tabs.setTabText(index, name)
        self.setWindowTitle(f"{name} — Arduino C++ Editor")

    def _update_position_label(self, line, col):
        self.pos_label.setText(f"Line {line + 1}, Col {col + 1}")

    # -- file actions -------------------------------------------------------

    def open_file(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open File", "",
            "Arduino / C++ Files (*.ino *.cpp *.h *.hpp *.c);;All Files (*)"
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

    # -- arduino-cli integration --------------------------------------------

    def _arduino_cli_available(self):
        from shutil import which
        return which("arduino-cli") is not None

    def compile_sketch(self):
        self._run_arduino_cli("compile")

    def upload_sketch(self):
        self._run_arduino_cli("upload")

    def _run_arduino_cli(self, action):
        editor = self.current_editor()
        if not editor or not editor.file_path:
            QMessageBox.warning(self, "Save required", "Please save the sketch before "
                                 f"{'compiling' if action == 'compile' else 'uploading'}.")
            return
        if not self._arduino_cli_available():
            QMessageBox.warning(
                self, "arduino-cli not found",
                "arduino-cli was not found on your PATH.\n\n"
                "Install it from https://arduino.github.io/arduino-cli/latest/installation/ "
                "to enable Verify/Compile and Upload."
            )
            return

        sketch_dir = os.path.dirname(editor.file_path)
        board = self.board_box.currentText().strip()
        port = self.port_box.currentText().strip()

        args = [action, "--fqbn", board, sketch_dir]
        if action == "upload":
            if not port or port.startswith("("):
                QMessageBox.warning(self, "Port required", "Please select a serial port to upload to.")
                return
            args += ["--port", port]

        self.output.clear()
        self.output_dock.show()
        self.output.appendPlainText(f"$ arduino-cli {' '.join(args)}\n")

        self.compile_process = QProcess(self)
        self.compile_process.setProcessChannelMode(QProcess.MergedChannels)
        self.compile_process.readyReadStandardOutput.connect(self._read_process_output)
        self.compile_process.finished.connect(
            lambda code, _status: self.output.appendPlainText(f"\n[process exited with code {code}]")
        )
        self.compile_process.start("arduino-cli", args)

    def _read_process_output(self):
        data = self.compile_process.readAllStandardOutput()
        text = bytes(data).decode("utf-8", errors="replace")
        self.output.appendPlainText(text.rstrip("\n"))

    # -- window close ---------------------------------------------------

    def closeEvent(self, event):
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
    app = QApplication(sys.argv)
    app.setApplicationName("Arduino C++ Editor")
    window = MainWindow()

    file_args = [a for a in sys.argv[1:] if os.path.isfile(a)]
    if file_args:
        # remove the default blank tab if real files were passed in
        window.tabs.removeTab(0)
        for path in file_args:
            window.new_tab(path)
    else:
        pass  # already has one "untitled.ino" tab from _build_menu_and_toolbar

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()