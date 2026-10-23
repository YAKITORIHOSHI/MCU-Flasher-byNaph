#!/usr/bin/env python3
"""Hardware-free native-toolbar sizing regressions; use a fresh process per scale.

The real QMainWindow/QWidgetAction host matters: Qt can hide a whole toolbar
container in its extension menu while its children still report isVisible().
No application backend, preference writes, firmware or downloads are started.
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QPlainTextEdit, QToolBar, QToolButton
from main.core.theme import Theme
from main.qt.theme import build_stylesheet, register_fonts
from main.qt.toolbar import ControlsBar, PrimaryToolbar
from src.modules.ui_metrics import WorkArea
from verify_runtime import PreviewBackend

APP = QApplication.instance() or QApplication([])
register_fonts()
RENDER_DIR = None


def pump():
    for _ in range(12):
        APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class ToolbarSizingChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        old_theme = Theme.active_theme
        self.addCleanup(Theme.apply_theme, old_theme)
        self.stack.enter_context(patch("main.core.config.load_gui_config", return_value={}))
        self.stack.enter_context(patch("src.dbs.dbs_create.add_notification"))
        self.backend = PreviewBackend()
        self.window = QMainWindow()
        self.window.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.editor = QPlainTextEdit(self.window)
        self.editor.setPlainText("// unsaved fixture\nvoid setup() {}\n")
        self.editor.document().setModified(True)
        self.editor.setFont(QFont("DejaVu Sans Mono", 17))
        self.editor.setStyleSheet("font-family: 'DejaVu Sans Mono'; font-size: 17pt;")
        self.window.setCentralWidget(self.editor)
        self.toolbar = PrimaryToolbar(self.backend, self.window)
        self.window.addToolBar(self.toolbar)
        self.window.addToolBarBreak()
        self.controls = ControlsBar(self.backend, self.window)
        self.controls_toolbar = QToolBar("Controls", self.window)
        self.controls_toolbar.setObjectName("controls-toolbar")
        self.controls_toolbar.setMovable(False)
        self.controls_toolbar.setFloatable(False)
        self.controls_toolbar.addWidget(self.controls)
        self.window.addToolBar(self.controls_toolbar)
        self.addCleanup(self.dispose)
        self.theme("default")
        self.window.show()
        pump()

    def dispose(self):
        self.window.close()
        self.window.deleteLater()
        pump()

    def theme(self, name):
        Theme.apply_theme(name)
        APP.setStyleSheet(build_stylesheet(name))
        self.toolbar.apply_theme(name)
        self.controls.apply_theme(name)
        pump()

    def resize(self, width, height=680):
        self.window.resize(width, height)
        pump()
        self.toolbar.set_responsive_width(self.toolbar.width())
        self.controls.set_responsive_height(height)
        self.controls.set_responsive_width(self.controls.width())
        pump()
        self.assertLessEqual(self.window.width(), width, "Toolbar hints forced a wider window")

    def contained(self, widget, ancestor):
        self.assertIsNotNone(widget.parentWidget(), widget.objectName())
        self.assertTrue(widget.isVisibleTo(self.window),
                        f"{widget.objectName()} hidden by an ancestor at {self.window.width()} logical pixels")
        owner = widget
        while owner is not ancestor:
            parent = owner.parentWidget()
            self.assertIsNotNone(parent)
            rect = QRect(owner.mapTo(parent, QPoint()), owner.size())
            self.assertTrue(parent.rect().contains(rect),
                            f"{widget.objectName()} clipped through {parent.objectName()}: {rect}, {parent.rect()}")
            owner = parent

    def essentials(self):
        for button in (self.toolbar.btn_compile, self.toolbar.btn_upload,
                       self.toolbar.btn_project, self.toolbar.btn_cloud,
                       self.toolbar.btn_download):
            self.contained(button, self.toolbar)
        for extension in self.toolbar.findChildren(QToolButton, "qt_toolbar_ext_button"):
            self.assertFalse(extension.isVisible(), "Native toolbar overflow concealed primary actions")
        for field in (self.controls.board_selector, self.controls.port_combo,
                      self.controls.upload_speed_combo):
            self.contained(field, self.controls)

    def test_full_actions_fit_1080p_at_150_percent_without_oversized_chrome(self):
        # These are the logical work widths of the user's 1920/1980px screens.
        for width in (1280, 1320):
            with self.subTest(width=width):
                self.resize(width)
                self.assertFalse(self.toolbar.is_compact(), "Intrinsic primary actions fit this work width")
                self.assertFalse(self.controls.is_compact(), "Intrinsic workspace actions fit this allocation")
                self.essentials()
                for button in (self.toolbar.btn_stop, self.toolbar.btn_clean, self.toolbar.btn_save,
                               self.toolbar.btn_save_all, self.toolbar.btn_reload, self.toolbar.btn_modify):
                    self.contained(button, self.toolbar)
                for button in (self.controls.btn_detach_editor, self.controls.btn_toggle_editor,
                               self.controls.btn_toggle_monitors, self.controls.btn_settings, self.controls.btn_ai):
                    self.contained(button, self.controls)

    def test_resize_history_theme_and_long_project_never_hide_essentials(self):
        ratio = APP.primaryScreen().devicePixelRatio()
        content_font = QFont(self.editor.font())
        self.assertEqual(content_font.pointSizeF(), 17)
        content = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        cursor.setPosition(8)
        self.editor.setTextCursor(cursor)
        for theme in ("default", "light", "solarized_dark"):
            self.theme(theme)
            self.window.activateWindow()
            self.editor.setFocus()
            pump()
            for physical_width in (1920, 1980, 1800):
                width = int(physical_width / ratio)
                area = WorkArea(0, 0, width, int(1040 / ratio))
                with self.subTest(theme=theme, physical_width=physical_width, scale=ratio), \
                     patch("main.qt.responsive.work_area", return_value=area):
                    for target in (width, max(320, width // 2), 320, width, 800, width):
                        self.resize(target)
                        self.toolbar.update_sketch_label("/fixture/" + "A long project name with forty characters")
                        pump()
                        self.essentials()
                    before = (self.toolbar.is_compact(), self.controls.is_compact(),
                              self.toolbar.lbl_sketch.isVisibleTo(self.window), self.toolbar.logo.text())
                    for _ in range(3):
                        self.toolbar.set_responsive_width(self.toolbar.width())
                        self.controls.set_responsive_width(self.controls.width())
                        pump()
                    self.assertEqual(before, (self.toolbar.is_compact(), self.controls.is_compact(),
                                              self.toolbar.lbl_sketch.isVisibleTo(self.window), self.toolbar.logo.text()))
                    self.assertIs(APP.focusWidget(), self.editor)
                    if RENDER_DIR:
                        self.assertTrue(self.window.grab().save(str(RENDER_DIR / f"toolbar-{theme}-{physical_width}-{ratio:g}.png")))
        self.assertEqual(self.editor.font(), content_font)
        self.assertEqual(self.editor.toPlainText(), content)
        self.assertTrue(self.editor.document().isModified())
        self.assertEqual(self.editor.textCursor().position(), 8)

    def test_compact_popup_then_expand_restores_all_primary_actions(self):
        self.resize(480)
        self.toolbar._toggle_actions_menu()
        pump()
        self.assertTrue(self.toolbar._actions_popup.isVisible())
        for button in self.toolbar._actions_popup._layout.parentWidget().findChildren(type(self.toolbar.btn_save)):
            self.assertIsNotNone(button.parentWidget())
        self.resize(1320)
        self.essentials()
        self.assertFalse(self.toolbar.is_compact())
        self.assertIsNone(self.toolbar._actions_popup)

    def test_idle_layout_timers_stop_after_each_transition(self):
        counts = [0, 0]
        self.toolbar._reflow_timer.timeout.connect(lambda: counts.__setitem__(0, counts[0] + 1))
        self.controls._controls_reflow_timer.timeout.connect(lambda: counts.__setitem__(1, counts[1] + 1))
        for width, height in ((1280, 680), (320, 680), (500, 312), (1320, 680)):
            self.resize(width, height)
            QTest.qWait(80)
            pump()
            before = tuple(counts)
            QTest.qWait(80)
            pump()
            self.assertEqual(tuple(counts), before, "Settled layouts must not schedule recurring idle work")
            self.assertFalse(self.toolbar._reflow_timer.isActive())
            self.assertFalse(self.controls._controls_reflow_timer.isActive())

    def test_operation_caption_survives_icon_only_and_full_layouts(self):
        self.resize(1280)
        self.toolbar.on_operation_phase({"is_busy": True, "phase": "compile", "op": "compile"})
        self.resize(320)
        self.essentials()
        self.resize(1320)
        self.essentials()
        self.assertEqual(self.toolbar.btn_compile.text(), "Compiling...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-dir", type=Path)
    args, remainder = parser.parse_known_args()
    if args.render_dir:
        RENDER_DIR = args.render_dir.resolve()
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Qt toolbar fixture scale: {APP.primaryScreen().devicePixelRatio():g}")
    unittest.main(argv=[sys.argv[0], *remainder])
