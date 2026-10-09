"""Safe workspace close with isolated workers and mocked persistence/hardware."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow
from main.qt import main_window as workspace

APP = QApplication.instance() or QApplication([])


class CloseProbe(QMainWindow):
    _operation_children_alive = workspace.MCUMainWindow._operation_children_alive
    _defer_operation_close = workspace.MCUMainWindow._defer_operation_close
    _finish_operation_close = workspace.MCUMainWindow._finish_operation_close
    closeEvent = workspace.MCUMainWindow.closeEvent

    def __init__(self, backend):
        super().__init__()
        self._backend = backend
        self._pending_operation_close = None
        self._operation_close_timer = QTimer(self)
        self._operation_close_timer.setInterval(10)
        self._operation_close_timer.timeout.connect(self._finish_operation_close)
        self._save_geometry = Mock()
        self._detached_window = None
        self.show()


class OperationCloseChecks(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "temp/audit/operation-close"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root_patch = patch.object(workspace, "_project_root", Path(self.temporary.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.warning = Mock()
        self.warning_patch = patch.object(workspace.QMessageBox, "warning", self.warning)
        self.warning_patch.start()
        self.addCleanup(self.warning_patch.stop)
        self.worker = Mock(is_alive=Mock(return_value=True))
        self.backend = SimpleNamespace(
            is_busy=True, active_operation="compile", _current_op_phase="resolving",
            _active_process=None, _operation_worker=self.worker, _op_session_id=1,
            _framework_download_active=False, _stop_requested=False,
            stop_operation=Mock(), stop_services=Mock(), emit=Mock(),
        )
        self.window = CloseProbe(self.backend)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.window._operation_close_timer.stop()
        self.backend.is_busy = False
        self.backend.active_operation = self.backend._current_op_phase = None
        self.backend._operation_worker = self.backend._active_process = None
        self.window.close()
        self.window.deleteLater()

    def request_close(self):
        event = QCloseEvent()
        self.window.closeEvent(event)
        return event

    def test_resolution_worker_without_process_cannot_be_mistaken_for_idle(self):
        self.assertFalse(self.request_close().isAccepted())
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.backend.is_busy)
        self.backend.stop_operation.assert_called_once()
        self.backend.stop_services.assert_not_called()
        self.window._save_geometry.assert_not_called()

    def test_compile_close_waits_for_both_process_and_worker(self):
        self.backend._current_op_phase = "compiling"
        process = Mock(poll=Mock(return_value=None))
        self.backend._active_process = process
        self.request_close()
        self.worker.is_alive.return_value = False
        self.window._finish_operation_close()
        self.backend.stop_services.assert_not_called()
        process.poll.return_value = 0
        self.window._finish_operation_close()
        self.backend.stop_services.assert_called_once()
        self.assertFalse(self.window.isVisible())

    def test_quiet_upload_preparation_can_cancel_before_hardware_write(self):
        self.backend.active_operation = "upload"
        self.backend._current_op_phase = "connecting"
        self.assertFalse(self.request_close().isAccepted())
        self.backend.stop_operation.assert_called_once()
        self.backend.stop_services.assert_not_called()

    def test_active_write_and_reset_remain_protected(self):
        for operation, phase in (("upload", "flashing"), ("reset", "resetting"), ("clean", "cleaning")):
            with self.subTest(phase=phase):
                self.backend.active_operation = operation
                self.backend._current_op_phase = phase
                self.assertFalse(self.request_close().isAccepted())
                self.assertIsNone(self.window._pending_operation_close)
        self.assertEqual(self.warning.call_count, 3)
        self.backend.stop_operation.assert_not_called()
        self.backend.stop_services.assert_not_called()

    def test_deferred_close_never_closes_a_later_operation(self):
        self.request_close()
        self.backend._op_session_id = 2
        self.backend._operation_worker = Mock(is_alive=Mock(return_value=False))
        self.backend.is_busy = False
        self.window._finish_operation_close()
        self.assertIsNone(self.window._pending_operation_close)
        self.assertFalse(self.window._operation_close_timer.isActive())
        self.assertTrue(self.window.isVisible())
        self.backend.stop_services.assert_not_called()

    def test_failed_or_slow_cancellation_keeps_workspace_and_buffers_open(self):
        self.request_close()
        session, worker, _ = self.window._pending_operation_close
        self.window._pending_operation_close = (session, worker, time.monotonic() - 1)
        self.window._finish_operation_close()
        self.assertTrue(self.window.isVisible())
        self.assertIsNone(self.window._pending_operation_close)
        self.backend.stop_services.assert_not_called()
        self.window._save_geometry.assert_not_called()

    def test_worker_after_phase_release_still_defers_close(self):
        self.backend.is_busy = False
        self.backend.active_operation = self.backend._current_op_phase = None
        self.assertFalse(self.request_close().isAccepted())
        self.backend.stop_services.assert_not_called()

    def test_real_worker_teardown_completes_before_qt_timer_closes_window(self):
        finish = threading.Event()
        worker = threading.Thread(target=lambda: finish.wait(2), daemon=True)
        worker.start()
        self.backend._operation_worker = worker
        try:
            self.request_close()
            self.backend.stop_services.assert_not_called()
            finish.set()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            QTest.qWait(60)
            self.backend.stop_services.assert_called_once()
            self.assertFalse(self.window.isVisible())
        finally:
            finish.set()
            worker.join(timeout=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
