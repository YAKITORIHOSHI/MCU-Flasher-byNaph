#!/usr/bin/env python3
"""Real read-only QScintilla sample viewing and isolated Tk launch checks.

Uses existing private dependencies, never installers, external viewers, settings
writes, network or hardware. Run in a separate process from PySide6 checks.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--scale", default="1")
parser.add_argument("--render-dir", type=Path)
OPTIONS, UNITTEST_ARGS = parser.parse_known_args()
os.environ["QT_SCALE_FACTOR"] = OPTIONS.scale

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QFont, QFontMetrics
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from main.core import config
from main.core.log_colors import contrast_ratio
from src import qscintilla_viewer as viewer_module
from src.modules import arduino_lib_req as browser

LAUNCH_VIEWER = browser._launch_code_viewer
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
APP = QApplication.instance() or QApplication([])


class SampleViewerChecks(unittest.TestCase):
    def test_sample_process_never_imports_workspace_qt_toolkit(self):
        self.assertFalse(any(name == "PySide6" or name.startswith("PySide6.")
                             for name in sys.modules))

    def setUp(self):
        directory = ROOT / "temp/audit/library-samples"
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.files = [self.folder / "Blink sample.ino", self.folder / "Sensor µ sample.ino"]
        self.texts = ["// A read-only library sample — µ\n" + "void loop() { delay(100); }\n" * 120,
                      "#include <Arduino.h>\nvoid setup() {}\nvoid loop() {}\n"]
        for file, text in zip(self.files, self.texts):
            file.write_text(text, encoding="utf-8")
        self.windows = []
        self.addCleanup(self.close_windows)
        self.addCleanup(patch.stopall)
        patch.object(config, "_save_raw_config", side_effect=AssertionError("No settings writes")).start()
        patch.object(config, "_load_raw_config", return_value={"shared": {"monitor_font_size": 24,
                                                                    "theme_mode": "light"}}).start()

    def close_windows(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        APP.processEvents()

    def window(self, theme="default", font=12, focus=0):
        window = viewer_module.MainWindow(str(self.files[focus]), list(map(str, self.files)),
                                         theme_mode=theme, font_size=font)
        self.windows.append(window)
        window.show()
        APP.processEvents()
        return window

    def test_read_only_unicode_copy_and_tab_navigation(self):
        window = self.window(focus=1)
        self.assertEqual(window.tabs.currentIndex(), 1)
        editor = window.tabs.currentWidget()
        self.assertEqual(editor.text(), self.texts[1])
        editor.setFocus()
        editor.selectAll()
        QTest.keyClick(editor, Qt.Key_C, Qt.ControlModifier)
        self.assertEqual(APP.clipboard().text(), self.texts[1])
        QTest.keyClicks(editor, "UNWANTED CHANGE")
        self.assertEqual(editor.text(), self.texts[1])
        QTest.keyClick(editor, Qt.Key_Tab, Qt.ControlModifier)
        APP.processEvents()
        self.assertEqual(window.tabs.currentIndex(), 0)
        self.assertIn(self.files[0].name, window.windowTitle())
        self.assertEqual(self.files[1].read_text(encoding="utf-8"), self.texts[1])

    def test_short_tab_labels_fit_the_styled_font_without_clipping(self):
        window = self.window()
        bar = window.tabs.tabBar()
        self.assertEqual(bar.font().pointSize(), 10)
        font = QFont(bar.font())
        font.setBold(True)
        for index, file in enumerate(self.files):
            self.assertEqual(bar.tabText(index), file.name)
            self.assertEqual(window.tabs.tabToolTip(index), str(file))
            text_width = QFontMetrics(font).horizontalAdvance(file.name)
            self.assertGreaterEqual(bar.tabRect(index).width(), text_width + 24)

    def test_actual_lexer_colors_and_saved_font_across_themes(self):
        styles = (0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 16)
        for mode in ("default", "light", "solarized_dark"):
            for font in (12, 24, 48):
                with self.subTest(theme=mode, font=font):
                    window = self.window(mode, font)
                    editor = window.tabs.widget(0)
                    self.assertEqual(editor.font().pointSize(), font)
                    self.assertTrue(editor.isReadOnly())
                    for style in styles:
                        color, paper = editor.lexer().color(style).name(), editor.lexer().paper(style).name()
                        self.assertEqual(editor.lexer().font(style).pointSize(), font)
                        self.assertGreaterEqual(contrast_ratio(color, paper), 4.5)
                        self.assertGreaterEqual(contrast_ratio(color, editor.colors["caret_line"]), 4.5)
                    self.assertGreaterEqual(contrast_ratio(editor.colors["selection_foreground"],
                                                          editor.colors["selection"]), 4.5)
                    if OPTIONS.render_dir and font == 12:
                        OPTIONS.render_dir.mkdir(parents=True, exist_ok=True)
                        self.assertTrue(window.grab().save(str(OPTIONS.render_dir / f"sample-{mode}.png")))
                    window.hide()

    def test_theme_change_retains_text_selection_zoom_and_scroll(self):
        window = self.window(font=24)
        editor = window.tabs.widget(0)
        editor.setSelection(0, 3, 0, 19)
        editor.SendScintilla(editor.SCI_SETFIRSTVISIBLELINE, 45)
        editor.zoomIn()
        before = (editor.text(), editor.getSelection(),
                  editor.SendScintilla(editor.SCI_GETFIRSTVISIBLELINE),
                  editor.SendScintilla(editor.SCI_GETZOOM))
        for theme in ("light", "solarized_dark", "default"):
            window.apply_theme(theme)
            APP.processEvents()
            after = (editor.text(), editor.getSelection(),
                     editor.SendScintilla(editor.SCI_GETFIRSTVISIBLELINE),
                     editor.SendScintilla(editor.SCI_GETZOOM))
            self.assertEqual(after, before)
            self.assertEqual(editor.font().pointSize(), 24)

    def test_small_and_portrait_work_areas_preserve_reading_surface(self):
        window = self.window(font=48)
        scale = float(OPTIONS.scale)
        for physical in ((1280, 720), (1366, 768), (1920, 1080), (3840, 2160), (1080, 1920)):
            area = QRect(-400, 20, round(physical[0] / scale), round(physical[1] / scale) - 48)
            window.resize(1600, 1200)
            window.fit_to_work_area(area)
            APP.processEvents()
            self.assertTrue(area.contains(window.frameGeometry()),
                            (area.getRect(), window.frameGeometry().getRect()))
            editor = window.tabs.currentWidget()
            self.assertGreater(editor.height(), editor.fontMetrics().height())
            self.assertEqual(editor.font().pointSize(), 48)
            editor.SendScintilla(editor.SCI_SETFIRSTVISIBLELINE, 80)
            self.assertGreater(editor.SendScintilla(editor.SCI_GETFIRSTVISIBLELINE), 0)

    def test_missing_sample_reports_visible_error(self):
        missing = str(self.folder / "missing.ino")
        window = viewer_module.MainWindow(missing, [missing], theme_mode="light", font_size=12)
        self.windows.append(window)
        self.assertIn("Unable to read this sample", window.tabs.widget(0).text())
        self.assertIn("missing.ino", window.statusBar().currentMessage())

    def test_saved_preferences_are_read_without_creating_instance(self):
        window = viewer_module.MainWindow(str(self.files[0]), list(map(str, self.files)))
        self.windows.append(window)
        self.assertEqual(window.theme_mode, "light")
        self.assertEqual(window.tabs.widget(0).font().pointSize(), 24)
        config._save_raw_config.assert_not_called()


class SampleLauncherChecks(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.geometry("160x100")
        self.root.update()
        directory = ROOT / "temp/audit/library-samples"
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.folder = Path(self.temporary.name)
        self.files = [self.folder / "first.ino", self.folder / "latest.ino"]
        for file in self.files:
            file.write_text("void setup() {}", encoding="utf-8")
        self.addCleanup(self.cleanup)
        self.thread_ids = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.launch = patch.object(browser, "_launch_code_viewer").start()
        self.info = patch.object(browser.messagebox, "showinfo").start()
        self.error = patch.object(browser.messagebox, "showerror").start()
        patch.object(config, "get_monitor_font_size", return_value=24).start()
        patch.object(browser.subprocess, "Popen", side_effect=AssertionError("No external launch")).start()
        patch.object(browser.subprocess, "run", side_effect=AssertionError("No real subprocess probe")).start()

    def cleanup(self):
        self.release.set()
        try:
            self.root.destroy()
        except Exception:
            pass
        tasks = getattr(self.root, "_mcu_code_viewer_tasks", None)
        deadline = time.monotonic() + 2
        while tasks is not None and tasks._active and time.monotonic() < deadline:
            time.sleep(.01)
        patch.stopall()
        self.temporary.cleanup()

    def pump(self, condition, timeout=2):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertTrue(condition())

    def probe(self, cancel=None):
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        self.release.wait(1)
        return None if cancel is not None and cancel.is_set() else sys.executable

    def open(self, index=0, **kwargs):
        browser._open_code_viewer(str(self.files[index]), list(map(str, self.files)), parent=self.root, **kwargs)

    def test_bounded_probe_keeps_tk_responsive_and_opens_latest(self):
        with patch.object(browser, "_find_code_viewer_python", side_effect=self.probe):
            started = time.monotonic()
            self.open()
            self.assertLess(time.monotonic() - started, .08)
            self.pump(self.started.is_set)
            ticks = []
            self.root.after(1, lambda: ticks.append(threading.get_ident()))
            self.open(1)
            self.pump(lambda: bool(ticks))
            self.assertEqual(self.root._mcu_code_viewer_tasks._active, 1)
            self.release.set()
            self.pump(lambda: self.launch.called)
            self.launch.assert_called_once()
            self.assertEqual(self.launch.call_args.args[0], str(self.files[1]))
            self.assertEqual(self.launch.call_args.kwargs["font_size"], 24)
            self.assertTrue(all(value != threading.get_ident() for value in self.thread_ids))
            self.assertEqual(ticks, [threading.get_ident()])

    def test_changed_selection_does_not_launch_stale_sample(self):
        selected = [True]
        with patch.object(browser, "_find_code_viewer_python", side_effect=self.probe):
            self.open(is_current=lambda: selected[0])
            self.pump(self.started.is_set)
            selected[0] = False
            self.release.set()
            self.pump(lambda: not self.root._mcu_code_viewer_tasks._active)
            for _ in range(8):
                self.root.update()
                time.sleep(.01)
            self.launch.assert_not_called()

    def test_destroyed_parent_discards_probe_completion(self):
        with patch.object(browser, "_find_code_viewer_python", side_effect=self.probe):
            self.open()
            self.pump(self.started.is_set)
            tasks = self.root._mcu_code_viewer_tasks
            self.root.destroy()
            self.release.set()
            deadline = time.monotonic() + 2
            while tasks._active and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(tasks.closed)
            self.assertEqual(tasks._active, 0)
            self.launch.assert_not_called()

    def test_missing_dependency_prompts_bootstrap_without_installing(self):
        with patch.object(browser, "_find_code_viewer_python", return_value=None):
            self.open()
            self.pump(lambda: self.info.called)
            self.assertEqual(self.info.call_args.args[0], "Bootstrap required")
            self.launch.assert_not_called()

    def test_deleted_sample_reports_error_after_probe(self):
        with patch.object(browser, "_find_code_viewer_python", side_effect=self.probe):
            self.open()
            self.pump(self.started.is_set)
            self.files[0].unlink()
            self.release.set()
            self.pump(lambda: self.error.called)
            self.launch.assert_not_called()

    def test_queued_old_completion_cannot_open_an_extra_window(self):
        with patch.object(browser, "_find_code_viewer_python", return_value=sys.executable):
            self.open()
            deadline = time.monotonic() + 2
            while self.root._mcu_code_viewer_tasks._active and time.monotonic() < deadline:
                time.sleep(.005)  # Finish the worker without draining Tk callbacks.
            self.assertEqual(self.root._mcu_code_viewer_tasks._active, 0)
            self.open()
            self.pump(lambda: self.launch.called)
            for _ in range(10):
                self.root.update()
                time.sleep(.01)
            self.launch.assert_called_once()

    def test_launch_passes_prepared_interpreter_theme_and_font(self):
        before = os.environ.copy()
        with patch.object(browser.subprocess, "Popen") as launch:
            LAUNCH_VIEWER(str(self.files[0]), tuple(map(str, self.files)), parent=self.root,
                          python_exe=sys.executable, theme_mode="light", font_size=24)
        launch.assert_called_once()
        args, kwargs = launch.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(Path(args[0][1]), ROOT / "src/qscintilla_viewer.py")
        self.assertEqual(args[0][2:], [str(self.files[0]), *map(str, self.files)])
        self.assertEqual(kwargs["env"]["MCU_FLASHER_VIEWER_THEME"], "light")
        self.assertEqual(kwargs["env"]["MCU_FLASHER_VIEWER_FONT_SIZE"], "24")
        self.assertEqual(os.environ, before)

    def test_probe_error_is_reported_without_requesting_installation(self):
        with patch.object(browser, "_find_code_viewer_python", side_effect=RuntimeError("Probe unavailable")):
            self.open()
            self.pump(lambda: self.error.called)
        self.assertIn("Probe unavailable", self.error.call_args.args[1])
        self.info.assert_not_called()
        self.launch.assert_not_called()

    def test_native_linux_probe_preserves_the_venv_symlink_spelling(self):
        host = self.folder / "host-python"
        python = self.folder / ".venv-linux/bin/python"
        python.parent.mkdir(parents=True)
        for file in (host, python):
            file.write_text("fixture executable", encoding="utf-8")
        # Model a final symlink resolution on Windows without requiring elevated
        # symlink creation or executing a Linux binary on this host.
        with patch.object(browser.sys, "platform", "linux"), \
             patch.object(browser.sys, "executable", str(host)), \
             patch.object(browser, "SCRIPT_DIR", str(self.folder)), \
             patch.object(Path, "resolve", return_value=host) as resolve, \
             patch.object(browser.subprocess, "run", side_effect=[SimpleNamespace(returncode=1),
                                                                 SimpleNamespace(returncode=0)]) as probe:
            selected = browser._find_code_viewer_python()
        self.assertEqual(selected, str(python))
        self.assertEqual([call.args[0][0] for call in probe.call_args_list], [str(host), str(python)])
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *UNITTEST_ARGS], verbosity=2)
