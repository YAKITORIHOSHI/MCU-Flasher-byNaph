"""Hardware-free cyclic Qt lifetime and event-dispatch regressions."""
import ast
import gc
import os
from pathlib import Path
import sys
import threading
import time
import unittest
import weakref
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from main.qt.garbage_collection import GuiGarbageCollector
from main.qt.cursor_visibility import WorkspacePointerGuard

APP = QApplication.instance() or QApplication([])


class LifetimeChecks(unittest.TestCase):
    def setUp(self):
        self.collector = GuiGarbageCollector(APP)
        self.collector._timer.stop()
        self.addCleanup(self.collector.deleteLater)
        self.addCleanup(self.collector.restore)

    def test_worker_allocations_do_not_finalize_qt_cycles(self):
        destroyed = []
        node = QObject()
        node.cycle = node
        node.destroyed.connect(lambda: destroyed.append(threading.get_ident()))
        reference = weakref.ref(node)
        del node
        def allocate():
            for _ in range(10000):
                values = [None]
                values[0] = values
        worker = threading.Thread(target=allocate)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(gc.isenabled())
        self.assertIsNotNone(reference())
        self.collector._last_full -= 61
        self.collector.collect_pending()
        self.assertIsNone(reference())
        self.assertEqual(destroyed, [threading.get_ident()])

    def test_collection_runs_after_virtual_dispatch_not_inside_it(self):
        observed = []
        owner = QWidget()
        guard = WorkspacePointerGuard(owner)
        class AllocatingFilter(QObject):
            def eventFilter(self, watched, event):
                values = [None]
                values[0] = values
                observed.append(gc.isenabled())
                return False
        probe = AllocatingFilter(owner)
        owner.installEventFilter(probe)
        gc_events = []
        def record(phase, info):
            if phase == "start":
                gc_events.append(QThread.currentThread() == APP.thread())
        gc.callbacks.append(record)
        try:
            QCoreApplication.sendEvent(owner, QEvent(QEvent.Type.Enter))
            self.assertTrue(observed)
            self.assertFalse(any(observed))
            self.assertFalse(gc_events)
            self.collector._last_full -= 61
            QTimer.singleShot(0, self.collector.collect_pending)
            QTest.qWait(20)
            self.assertTrue(gc_events)
            self.assertTrue(all(gc_events))
        finally:
            gc.callbacks.remove(record)
            owner.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_wrong_thread_nested_modal_and_stopped_skip_collection(self):
        with patch("main.qt.garbage_collection.gc.collect") as collect:
            self.collector._last_full -= 61
            worker = threading.Thread(target=self.collector.collect_pending)
            worker.start()
            worker.join(2)
            collect.assert_not_called()
            with patch("main.qt.garbage_collection.QApplication.activeModalWidget", return_value=object()):
                self.collector.collect_pending()
            collect.assert_not_called()
            self.collector.stop()
            self.collector.collect_pending()
            collect.assert_not_called()

    def test_idle_threshold_and_full_collection_are_bounded(self):
        with patch("main.qt.garbage_collection.gc.collect") as collect, \
             patch("main.qt.garbage_collection.gc.get_count", return_value=(0, 0, 0)):
            self.collector.collect_pending()
            collect.assert_not_called()
            self.collector._last_full -= 61
            self.collector.collect_pending()
            collect.assert_called_once_with(2)
            self.collector.collect_pending()
            collect.assert_called_once()

    def test_entry_installs_before_backend_and_retires_at_exit(self):
        tree = ast.parse((ROOT / "main/mcu_flash_gui.py").read_text(encoding="utf-8-sig"))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = sorted((node.lineno, ast.unparse(node.func)) for node in ast.walk(main) if isinstance(node, ast.Call))
        install = next(line for line, name in calls if name == "install_gui_garbage_collector")
        backend = next(line for line, name in calls if name == "MCUWebBackendAPI")
        self.assertLess(install, backend)
        self.assertTrue(any(name == "cycle_collector.stop" for _, name in calls))

    def test_real_webengine_and_modal_churn_with_worker_allocations(self):
        from PySide6.QtWebEngineWidgets import QWebEngineView
        owner = QWidget()
        guard = WorkspacePointerGuard(owner)
        view = QWebEngineView(owner)
        loaded = []
        view.loadFinished.connect(loaded.append)
        view.setHtml("<html><body><input value='dirty buffer sentinel'></body></html>")
        deadline = time.monotonic() + 8
        while not loaded and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertEqual(loaded, [True])
        stop = threading.Event()
        def allocate():
            while not stop.wait(0.002):
                for _ in range(1000):
                    cycle = [None]
                    cycle[0] = cycle
        worker = threading.Thread(target=allocate)
        worker.start()
        try:
            for _ in range(20):
                dialog = QDialog(owner)
                dialog.cycle = dialog
                QTimer.singleShot(0, dialog.accept)
                dialog.exec()
                del dialog
                self.collector._last_full -= 61
                QTimer.singleShot(0, self.collector.collect_pending)
                QTest.qWait(5)
                QCoreApplication.sendEvent(view, QEvent(QEvent.Type.Enter))
            self.assertFalse(gc.isenabled())
            self.assertIs(view.parentWidget(), owner)
        finally:
            stop.set()
            worker.join(2)
            owner.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main(verbosity=2)
