#!/usr/bin/env python3
"""Verify the plain-text setup view with no window toolkit, installers or live writes."""
from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Optional
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")


class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "PySide6" or fullname.startswith("PySide6."):
            raise ModuleNotFoundError("Qt deliberately unavailable in the cold fixture", name=fullname)


sys.meta_path.insert(0, NoQt())
from main.core.theme import Theme
from src.modules.bootstrap_console import ConsoleBootstrapView
from src.modules.bootstrap_dispatch import BootstrapDispatcher

REPORT_DIR = None


def fixture(mode="default", has_qt=False, *, preimport_qt=False, stream=None):
    """Use the real BootstrapGUI API while excluding all bootstrap import side effects."""
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    names = {"_BootstrapRootProxy", "BootstrapGUI", "_prepare_bootstrap_qt_path",
             "_load_bootstrap_qt", "_bootstrap_qt_context"}
    nodes = [node for node in tree.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    palette = {"T_" + key: value for key, value in Theme.PALETTES[mode].items()}
    namespace = dict(globals(), HAS_PYSIDE6_BOOTSTRAP=has_qt, _BOOTSTRAP_THEME_MODE=mode,
                     SCRIPT_DIR=ROOT, _BOOTSTRAP_QT_IMPORT_ERROR="",
                     _resolve_bootstrap_theme=lambda: (palette, mode),
                     _T_PALETTE=palette, DEFAULT_SKIP_UPDATES=True, BOOTSTRAP_CLOSE_DELAY_S=0,
                     load_bootstrap_config=Mock(return_value={"other": "kept"}),
                     save_bootstrap_config=Mock(return_value=True),
                     _record_bootstrap_log=Mock(), _record_bootstrap_exception=Mock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    if preimport_qt:
        # Production bootstrap prepares the env path and imports QtWidgets at
        # module scope before BootstrapGUI opens its warm view. Exercise that
        # second path check with Qt's embedded support aliases already loaded.
        assert has_qt
        namespace["_prepare_bootstrap_qt_path"]()
        from PySide6.QtWidgets import QApplication
    with contextlib.redirect_stdout(stream or io.StringIO()):
        return namespace["BootstrapGUI"](), namespace


class ColdViewChecks(unittest.TestCase):
    def own(self, mode="default"):
        self.stream = io.StringIO()
        gui, namespace = fixture(mode, stream=self.stream)
        self.addCleanup(gui._native_window._on_close)
        return gui, namespace

    def output(self):
        return self.stream.getvalue()

    def test_cold_view_reports_without_qt_or_a_live_config_write(self):
        for mode in Theme.PALETTES:
            gui, namespace = self.own(mode)
            view = gui._native_window
            self.assertIsInstance(view, ConsoleBootstrapView)
            self.assertIsNone(gui._app)
            self.assertIn("MCU Flasher setup", self.output())
            namespace["save_bootstrap_config"].assert_not_called()
            view._on_close()
        self.assertFalse(any(name.startswith("PySide6") for name in sys.modules))

    def test_view_module_loads_without_any_window_toolkit(self):
        probe = """
import importlib.abc, sys
class NoToolkit(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6', 'shiboken6', 'tkinter', '_tkinter'}:
            raise AssertionError('Toolkit imported by the plain-text view: ' + fullname)
sys.meta_path.insert(0, NoToolkit())
from src.modules import bootstrap_console
"""
        result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=ROOT, capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_worker_is_queued_and_callbacks_run_on_creating_thread(self):
        gui, namespace = self.own()
        callback_threads = []

        def worker():
            gui.log_section("Cold dependency preparation")
            gui.set_status("Installing missing packages")
            gui.root.after(0, lambda: callback_threads.append(threading.get_ident()))
            gui.set_progress_percent(60)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("Cold dependency preparation", self.output())
        gui.pump()
        self.assertIn("Cold dependency preparation", self.output())
        self.assertIn("Installing missing packages", self.output())
        self.assertEqual(callback_threads, [threading.get_ident()])
        self.assertEqual(gui._native_window._progress, round(gui._overall_progress))
        namespace["_record_bootstrap_exception"].assert_not_called()

    def test_summary_hides_raw_package_lines_but_keeps_them_for_the_window(self):
        gui, _ = self.own()
        gui.log_section("Preparing Python dependencies")
        gui.log_status("pyserial: Downloading pyserial-3.5.whl.metadata (1.6 kB)")
        gui.log_ok("Downloaded pyserial successfully.")
        gui.pump()
        self.assertNotIn(".whl.metadata", self.output())
        self.assertIn("Downloaded pyserial successfully.", self.output())
        self.assertTrue(any(".whl.metadata" in text for text, _tag in gui._native_window.snapshot()),
                        "Raw details must still reach the Qt window")

    def test_repeated_status_prints_once_and_long_status_stays_readable(self):
        gui, _ = self.own()
        for _ in range(5):
            gui.set_status("Same status")
            gui.pump()
        self.assertEqual(self.output().count("Same status"), 1)
        long = "Installing Python dependencies (" + ", ".join(f"fixture-{i}" for i in range(60)) + ")..."
        gui.set_status(long)
        gui.pump()
        self.assertIn("60 packages", self.output())

    def test_queued_display_bound_and_control_retention(self):
        gui, namespace = self.own()
        view = gui._native_window
        signals = view.signals
        signals.MAX_EVENTS = 12
        signals.MAX_CHARS = 4000

        def worker():
            for number in range(60):
                signals.sig_log.emit("Queue row " + str(number) + "x" * 120, "normal")
            signals.sig_log.emit("Important failure", "fail")
            signals.sig_status.emit("Retained control")
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertLessEqual(len(signals._events), 12)
        self.assertLessEqual(signals._chars, 4000)
        gui.pump()
        self.assertIn("Important failure", self.output())
        self.assertIn("Retained control", self.output())
        self.assertTrue(any("full output remains" in text for text, _tag in view.snapshot()))
        namespace["_record_bootstrap_exception"].assert_not_called()

    def test_history_given_to_the_window_is_bounded(self):
        gui, _ = self.own()
        view = gui._native_window
        view.MAX_SHOWN_EVENTS, view.MAX_SHOWN_CHARS = 5, 10000
        for number in range(20):
            view._on_log(f"Row {number}", "normal")
        self.assertEqual([text for text, _tag in view.snapshot()], [f"Row {n}" for n in range(15, 20)])
        view.MAX_SHOWN_CHARS = 8
        view._on_log("Last row", "normal")
        self.assertEqual(view.snapshot(), [("Last row", "normal")])

    def test_unwritable_console_never_stops_setup(self):
        gui, namespace = self.own()
        view = gui._native_window
        view._stream = Mock(write=Mock(side_effect=OSError("Console closed")))
        gui.log_ok("Still running")
        gui.pump()
        self.assertIsNone(view._stream)
        self.assertEqual(view.snapshot()[-1][0].strip(), "Still running")
        namespace["_record_bootstrap_exception"].assert_not_called()

    def test_mainloop_worker_close_ends_the_view(self):
        gui, namespace = self.own()

        def worker():
            gui.root.after(0, lambda: gui.log_ok("Cold view ready"))
            gui.stop_spinner("Ready", True)
            gui.close_after_delay(.05)
        thread = threading.Thread(target=worker)
        thread.start()
        gui.mainloop_until_done()
        thread.join(2)
        self.assertTrue(gui._closed)
        self.assertTrue(gui._native_window.closed)
        self.assertTrue(gui._done_event.is_set())
        self.assertIn("Cold view ready", self.output())
        namespace["_record_bootstrap_exception"].assert_not_called()


class WindowsConsoleChecks(unittest.TestCase):
    """The Win32 call sequence, with the native boundary replaced."""

    def view(self, windll, opened=None):
        record = Mock()
        with patch("ctypes.windll", windll, create=True), patch.object(sys, "platform", "win32"), \
                patch("builtins.open", return_value=opened if opened is not None else io.StringIO()):
            view = ConsoleBootstrapView(Mock(), record, dispatcher=BootstrapDispatcher())
        return view, record

    def test_windowless_python_opens_a_console_that_cannot_be_closed_by_accident(self):
        windll = MagicMock()
        windll.kernel32.GetConsoleWindow.side_effect = [0, 99]
        windll.kernel32.AllocConsole.return_value = 1
        view, record = self.view(windll)
        windll.kernel32.FreeConsole.assert_called_once()
        windll.kernel32.AllocConsole.assert_called_once()
        windll.user32.DeleteMenu.assert_called_once_with(windll.user32.GetSystemMenu.return_value, 0xF060, 0)
        windll.kernel32.SetConsoleCtrlHandler.assert_called_once_with(None, True)
        windll.kernel32.SetConsoleTitleW.assert_called()
        self.assertTrue(view._owns_console)
        record.assert_not_called()
        windll.kernel32.FreeConsole.reset_mock()
        with patch("ctypes.windll", windll, create=True), patch.object(sys, "platform", "win32"):
            view.retire()
        windll.kernel32.FreeConsole.assert_called_once()
        self.assertTrue(view.closed)

    def test_failed_console_leaves_setup_running_without_output(self):
        windll = MagicMock()
        windll.kernel32.GetConsoleWindow.return_value = 0
        windll.kernel32.AllocConsole.return_value = 0
        view, record = self.view(windll)
        self.assertIsNone(view._stream)
        self.assertFalse(view._owns_console)
        view._on_log("Retained for the window", "ok")
        self.assertEqual(view.snapshot(), [("Retained for the window", "ok")])
        record.assert_not_called()

    def test_console_error_is_recorded_and_setup_continues(self):
        windll = MagicMock()
        windll.kernel32.GetConsoleWindow.side_effect = OSError("Fixture console failure")
        view, record = self.view(windll)
        self.assertIsNone(view._stream)
        self.assertIn("Fixture console failure", record.call_args.args[0])


def main():
    global REPORT_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    REPORT_DIR = args.report_dir
    if REPORT_DIR:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite((loader.loadTestsFromTestCase(ColdViewChecks), loader.loadTestsFromTestCase(WindowsConsoleChecks)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
