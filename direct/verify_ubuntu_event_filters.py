#!/usr/bin/env python3
"""Isolated Ubuntu pointer/log event dispatch, including native WebEngine.

Starts no backend, setup, hardware, or persistence. The native renderer probe
runs in a fresh process with a real QApplication event loop. Use xcb under a
desktop/Xvfb to check the native Ubuntu path; offscreen is also supported.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget
from shiboken6 import isValid

from main.qt.cursor_visibility import WorkspacePointerGuard
from main.qt.garbage_collection import install_gui_garbage_collector
from main.qt.log_follow import LogFollow
NATIVE_LINUX = sys.platform.startswith("linux")


def mouse_event(kind):
    buttons = Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton
    return QMouseEvent(kind, QPointF(5, 5), QPointF(5, 5),
                       Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)


def native_probe():
    """Exercise Qt private renderer creation while Python watches local widgets."""
    from PySide6.QtWebEngineWidgets import QWebEngineView
    app = QApplication([])
    collector = install_gui_garbage_collector(app)
    owner = QWidget()
    owner.resize(640, 460)
    layout = QVBoxLayout(owner)
    renderer = QWebEngineView(owner)
    layout.addWidget(renderer)
    log = QPlainTextEdit(owner)
    log.setPlainText("\n".join(f"retained output {number}" for number in range(200)))
    layout.addWidget(log)
    guard = WorkspacePointerGuard(owner)
    follow = LogFollow(log, hold_to_pause=True)
    loaded = []
    renderer.loadFinished.connect(loaded.append)
    renderer.setHtml("<html><body><input value='unsaved renderer sentinel'>"
                     "<div id='changes'></div><script>let n=0;setInterval(()=>{"
                     "document.getElementById('changes').textContent=++n},10)"
                     "</script></body></html>")
    owner.show()
    cycles = [0]
    failures = []
    timers = []

    def churn():
        follow._press()
        dialog = QDialog(owner)
        child = QLineEdit(dialog)
        child.setCursor(Qt.CursorShape.BlankCursor)
        dialog.show()

        def finish():
            if child.cursor().shape() != Qt.CursorShape.IBeamCursor:
                failures.append("owned dialog pointer was not restored")
            QCoreApplication.sendEvent(child, mouse_event(QEvent.Type.MouseButtonRelease))
            if follow.scrollbar_held:
                failures.append("outside release did not resume log")
            dialog.close()
            dialog.deleteLater()
            cycles[0] += 1
            owner.resize(640 + cycles[0] % 3 * 8, 460)
            if cycles[0] < 30:
                timer.start(30)
            else:
                owner.close()
                owner.deleteLater()
                QTimer.singleShot(30, app.quit)

        callback = QTimer(owner)
        callback.setSingleShot(True)
        callback.timeout.connect(finish)
        timers.append(callback)
        callback.start(10)

    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(churn)
    timer.start(200)
    deadline = QTimer(app)
    deadline.setSingleShot(True)
    deadline.timeout.connect(lambda: app.exit(2))
    deadline.start(10000)
    result = app.exec()
    collector.restore()
    if result or not any(loaded) or cycles[0] != 30 or failures:
        print(f"Native dispatch failed: exit={result}, loaded={loaded}, cycles={cycles[0]}, failures={failures}", flush=True)
        return 1
    print("Native WebEngine event loop survived 30 owned-dialog/hold/release cycles.", flush=True)
    return 0


class UbuntuEventFilterChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.collector = install_gui_garbage_collector(cls.app)

    def setUp(self):
        if not NATIVE_LINUX:
            # Exercise the Linux registration/dispatch contracts with real
            # owned Qt widgets; native WebEngine probes still require Linux.
            platform = patch("sys.platform", "linux")
            platform.start()
            self.addCleanup(platform.stop)

    def widget(self, cls=QWidget, parent=None):
        widget = cls(parent)

        def dispose():
            if isValid(widget):
                widget.close()
                widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        self.addCleanup(dispose)
        return widget

    def process(self):
        QTest.qWait(20)

    def log(self):
        owner = self.widget()
        owner.resize(500, 240)
        layout = QVBoxLayout(owner)
        view = self.widget(QPlainTextEdit, owner)
        layout.addWidget(view)
        view.setPlainText("\n".join(f"line {number}" for number in range(200)))
        follow = LogFollow(view, hold_to_pause=True)
        owner.show()
        self.process()
        return owner, view, follow

    def test_linux_does_not_install_application_filters_and_windows_keeps_them(self):
        owner = self.widget()
        view = self.widget(QPlainTextEdit, owner)
        with patch.object(self.app, "installEventFilter") as install:
            guard = WorkspacePointerGuard(owner)
            follow = LogFollow(view)
            install.assert_not_called()
            with patch("sys.platform", "win32"):
                windows_guard = WorkspacePointerGuard(owner)
                windows_follow = LogFollow(view)
            self.assertEqual([item.args[0] for item in install.call_args_list],
                             [windows_guard, windows_follow])

    def test_new_owned_and_detached_widgets_restore_pointer_without_changing_normal_cursors(self):
        owner = self.widget()
        owner.show()
        guard = WorkspacePointerGuard(owner)
        dialog = self.widget(QDialog, owner)
        edit = self.widget(QLineEdit, dialog)
        edit.setCursor(Qt.CursorShape.BlankCursor)
        dialog.show()
        self.process()
        self.assertEqual(edit.cursor().shape(), Qt.CursorShape.IBeamCursor)
        detached = self.widget(QWidget, owner)
        detached.setWindowFlag(Qt.WindowType.Window)
        detached.setProperty("mcuTextPointer", True)
        child = self.widget(QWidget, detached)
        child.setCursor(Qt.CursorShape.BlankCursor)
        detached.show()
        child.show()
        self.process()
        self.assertEqual(child.cursor().shape(), Qt.CursorShape.IBeamCursor)
        edit.setCursor(Qt.CursorShape.PointingHandCursor)
        QCoreApplication.sendEvent(edit, QEvent(QEvent.Type.Enter))
        self.assertEqual(edit.cursor().shape(), Qt.CursorShape.PointingHandCursor)

    def test_reparented_and_deleted_widgets_are_released_after_queued_scan(self):
        owner = self.widget()
        other = self.widget()
        child = self.widget(QWidget, owner)
        owner.show()
        child.show()
        guard = WorkspacePointerGuard(owner)
        self.process()
        child.setParent(other)
        child.show()
        self.process()
        child.setCursor(Qt.CursorShape.BlankCursor)
        QCoreApplication.sendEvent(child, QEvent(QEvent.Type.Enter))
        self.assertEqual(child.cursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertNotIn(id(child), guard._watched_widgets)
        owned = self.widget(QWidget, owner)
        owned.show()
        self.process()
        key = id(owned)
        owned.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.process()
        self.assertNotIn(key, guard._watched_widgets)
        # A pending scan must disappear safely with its workspace parent.
        guard._scan_timer.start(0)
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.process()

    def test_auto_resumes_on_release_outside_view_and_new_owned_dialog(self):
        owner, view, follow = self.log()
        other = self.widget()
        child = self.widget(QWidget, other)
        other.show()
        child.show()
        self.process()
        QCoreApplication.sendEvent(view.viewport(), mouse_event(QEvent.Type.MouseButtonPress))
        self.process()
        bar = view.verticalScrollBar()
        bar.setValue(12)
        with follow.update():
            view.appendPlainText("output during hold")
        self.assertTrue(follow.scrollbar_held)
        self.assertLess(bar.value(), bar.maximum())
        QCoreApplication.sendEvent(child, mouse_event(QEvent.Type.MouseButtonRelease))
        self.process()
        self.assertFalse(follow.scrollbar_held)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertFalse(follow._release_widgets)
        QCoreApplication.sendEvent(view.viewport(), mouse_event(QEvent.Type.MouseButtonPress))
        dialog = self.widget(QDialog, owner)
        edit = self.widget(QLineEdit, dialog)
        # Polish the new owned window without activating it: deactivation has
        # its own resume check, while this case exercises its outside release.
        dialog.ensurePolished()
        self.process()
        bar.setValue(10)
        QCoreApplication.sendEvent(edit, mouse_event(QEvent.Type.MouseButtonRelease))
        self.process()
        self.assertFalse(follow.scrollbar_held)
        self.assertEqual(bar.value(), bar.maximum())

    def test_lost_mouse_grab_and_deactivation_resume_only_enabled_auto(self):
        owner, view, follow = self.log()
        bar = view.verticalScrollBar()
        for kind in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate):
            with self.subTest(kind=kind):
                follow._press()
                self.process()
                bar.setValue(10)
                QCoreApplication.sendEvent(owner, QEvent(kind))
                self.process()
                self.assertFalse(follow.scrollbar_held)
                self.assertEqual(bar.value(), bar.maximum())
        follow.set_enabled(False)
        follow._press()
        self.process()
        bar.setValue(10)
        QCoreApplication.sendEvent(owner, QEvent(QEvent.Type.UngrabMouse))
        self.process()
        self.assertEqual(bar.value(), 10)
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.process()

    @unittest.skipUnless(NATIVE_LINUX, "Native Ubuntu WebEngine event loop")
    def test_native_renderer_event_loop_survives_local_filter_churn(self):
        audit = ROOT / "temp" / "audit" / "ubuntu-event-filters"
        audit.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ, XDG_CACHE_HOME=str(audit / "cache"))
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--native-probe"],
                                cwd=ROOT, env=environment, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("survived 30", result.stdout)

    @unittest.skipUnless(NATIVE_LINUX, "Native Ubuntu log cursor/renderer lifetime")
    def test_long_output_clear_and_deferred_document_destruction_do_not_corrupt_heap(self):
        # This exact sequence used to release retained QTextCursor objects from
        # a scrollbar signal while Qt's finishEdit was still using them. The
        # next syntax completion exposed the resulting native heap corruption.
        result = subprocess.run([
            sys.executable, "-B", str(ROOT / "direct/verify_performance.py"), "--no-finalize",
            "PerformanceChecks.test_streaming_display_memory_and_no_newline",
            "PerformanceChecks.test_syntax_completion_is_on_gui_thread_and_can_repeat",
        ], cwd=ROOT, env=os.environ.copy(), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Ran 2 tests", result.stdout)


if __name__ == "__main__":
    if "--native-probe" in sys.argv:
        if not NATIVE_LINUX:
            raise SystemExit("Use the native Ubuntu private runtime for --native-probe.")
        raise SystemExit(native_probe())
    if "--no-finalize" in sys.argv:
        sys.argv.remove("--no-finalize")
        checks = unittest.main(verbosity=2, exit=False)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0 if checks.result.wasSuccessful() else 1)
    unittest.main(verbosity=2)
