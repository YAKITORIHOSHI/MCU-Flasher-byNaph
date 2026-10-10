#!/usr/bin/env python3
"""Hardware-free Serial Monitor delivery, Clear, and clipboard regressions.

Uses an isolated signal bus and real Qt widgets with all settings persistence
mocked. Opens no serial port, installs nothing, and touches no project cache.
On Windows the native clipboard check preserves and restores its MIME data.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if os.name == "nt":
    # This verifier reads the Win32 clipboard format directly; the offscreen
    # Qt plugin used by the CI job cannot publish CF_UNICODETEXT.
    os.environ["QT_QPA_PLATFORM"] = "windows"
else:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QThread
from PySide6.QtWidgets import QApplication
from main.core import config
from main.qt.serial_panel import SerialPanel
from main.qt.signals import MCUSignals

APP = QApplication.instance() or QApplication([])


class SerialDeliveryChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original = APP.clipboard().mimeData()
        cls.saved_clipboard = QMimeData()
        if original is not None:
            for fmt in original.formats():
                cls.saved_clipboard.setData(fmt, original.data(fmt))

    @classmethod
    def tearDownClass(cls):
        APP.clipboard().setMimeData(cls.saved_clipboard)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}))
        self.stack.enter_context(patch.object(config, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(config, "_save_raw_config", side_effect=AssertionError("live persistence write")))
        self.stack.enter_context(patch.object(config, "save_gui_config", side_effect=AssertionError("live settings write")))
        self.stack.enter_context(patch.object(config, "get_theme_mode", return_value="default"))
        self.stack.enter_context(patch.object(config, "get_monitor_font_size", return_value=11))

    def bus(self):
        bus = MCUSignals()
        def dispose():
            if bus._log_timer is not None:
                bus._log_timer.stop()
            bus.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return bus

    def panel(self, bus, backend=None):
        panel = SerialPanel(backend)
        panel.resize(960, 240)
        panel.connect_signals(bus)
        def dispose():
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return panel

    def copy_panel(self, panel):
        with patch("main.qt.serial_panel.QTimer.singleShot"):
            panel._copy_output()
        return APP.clipboard().text()

    def test_line_batches_preserve_stream_metadata(self):
        bus = self.bus()
        received = []
        bus.serial_log.connect(received.append)
        bus.set_serial_generation(7)
        bus.queue_log("serial", {"lines": ["alpha", "beta"], "generation": 7,
                                 "timestamp": "[01:02:03]", "tag": "warning", "source": "uart"})
        bus.flush_pending_logs("serial")
        self.assertEqual([item["text"] for item in received], ["alpha", "beta"])
        for item in received:
            self.assertEqual(item["generation"], 7)
            self.assertEqual(item["timestamp"], "[01:02:03]")
            self.assertEqual(item["source"], "uart")
            self.assertEqual(item["tag"], "warning")
            self.assertTrue(item["newline"])

    def test_generation_change_keeps_accepted_history_and_rejects_late_worker(self):
        bus = self.bus()
        received = []
        bus.set_serial_generation(1)
        def on_output(payload):
            received.append(payload)
            if payload.get("text") == "old first":
                bus.set_serial_generation(2)
                bus.queue_log("serial", {"text": "new connection", "generation": 2})
        bus.serial_log.connect(on_output)
        bus.queue_log("serial", {"lines": ["old first", "old second"], "generation": 1})
        bus.flush_pending_logs("serial")
        bus.queue_log("serial", {"text": "late old worker", "generation": 1})
        bus.flush_pending_logs("serial")
        self.assertEqual([item["text"] for item in received], ["old first", "old second", "new connection"])

    def test_generation_change_keeps_already_queued_connection_history(self):
        bus = self.bus()
        received = []
        bus.serial_log.connect(received.append)
        bus.set_serial_generation(1)
        bus.queue_log("serial", {"text": "queued old connection", "generation": 1})
        bus.set_serial_generation(2)
        bus.queue_log("serial", {"text": "new connection", "generation": 2})
        bus.queue_log("serial", {"text": "obsolete worker", "generation": 1})
        bus.flush_pending_logs("serial")
        self.assertEqual([item["text"] for item in received], ["queued old connection", "new connection"])

    def test_clear_rejects_remaining_already_drained_batch(self):
        bus = self.bus()
        received = []
        def on_output(payload):
            received.append(payload)
            if payload.get("text") == "first before Clear":
                bus.clear_log_queue("serial")
                bus.queue_log("serial", {"text": "after Clear"})
        bus.serial_log.connect(on_output)
        bus.queue_log("serial", {"lines": ["first before Clear", "second before Clear"]})
        bus.flush_pending_logs("serial")
        bus.flush_pending_logs("serial")
        self.assertEqual([item["text"] for item in received], ["first before Clear", "after Clear"])

    def test_long_serial_events_are_segmented_without_discarding_middle(self):
        bus = self.bus()
        bus.set_serial_generation(3)
        received = []
        bus.serial_log.connect(received.append)
        text = "start-" + "0123456789" * 2400 + "-end"
        bus.queue_log("serial", {"text": text, "newline": True, "generation": 3, "stream_end": True})
        bus.flush_pending_logs("serial")
        self.assertEqual("".join(item["text"] for item in received), text)
        self.assertGreater(len(received), 1)
        self.assertTrue(received[-1]["newline"])
        self.assertTrue(all(not item["newline"] for item in received[:-1]))
        self.assertTrue(received[-1]["stream_end"])
        self.assertTrue(all(not item["stream_end"] for item in received[:-1]))

    def test_copy_flushes_serial_without_consuming_build_output(self):
        bus = self.bus()
        panel = self.panel(bus)
        bus.queue_log("console", {"text": "pending build journal"})
        bus.queue_log("serial", {"text": "banner", "newline": True})
        bus.queue_log("serial", {"text": "prompt without newline", "newline": False})
        copied = self.copy_panel(panel)
        self.assertIn("banner", copied)
        self.assertIn("prompt without newline", copied)
        self.assertFalse(bus._pending_logs["serial"])
        self.assertEqual(len(bus._pending_logs["console"]), 1)

    def test_copy_publishes_reader_snapshot_before_bus_and_view_snapshots(self):
        bus = self.bus()
        order = []
        class ReaderFixture:
            serial_running = False
            def flush_serial_output(self):
                order.append("reader")
                bus.queue_log("serial", {"text": "reader's decoded pending bytes", "newline": False})
                return True
        panel = self.panel(bus, ReaderFixture())
        original_bus_flush = bus.flush_pending_logs
        original_view_snapshot = panel._output.get_content_for_clipboard
        def bus_flush(stream):
            order.append("bus")
            return original_bus_flush(stream)
        def view_snapshot(*args, **kwargs):
            order.append("view")
            return original_view_snapshot(*args, **kwargs)
        with patch.object(bus, "flush_pending_logs", side_effect=bus_flush), \
             patch.object(panel._output, "get_content_for_clipboard", side_effect=view_snapshot):
            copied = self.copy_panel(panel)
        self.assertEqual(order, ["reader", "bus", "view"])
        self.assertEqual(copied, "reader's decoded pending bytes")

    def test_clear_uses_backend_boundary_and_keeps_future_output(self):
        bus = self.bus()
        order = []
        class ReaderFixture:
            serial_running = False
            def clear_serial_output(self):
                order.append("reader boundary")
                bus.queue_log("serial", {"text": "reader bytes retired before Clear", "newline": False})
                bus.clear_log_queue("serial")
                bus.serial_clear.emit()
                return True
        panel = self.panel(bus, ReaderFixture())
        panel._output.append_log({"text": "old view queue"})
        bus.queue_log("serial", {"text": "old bus queue"})
        panel._output_clear()
        bus.flush_pending_logs("serial")
        self.assertEqual(order, ["reader boundary"])
        self.assertEqual(panel._output.get_content_for_clipboard(), "")
        bus.queue_log("serial", {"text": "after Clear"})
        self.assertEqual(self.copy_panel(panel), "after Clear\n")

    def test_clear_purges_bus_and_view_without_replaying_queued_data(self):
        bus = self.bus()
        panel = self.panel(bus)
        panel._output.append_log({"text": "already delivered but not painted"})
        bus.queue_log("serial", {"text": "queued before Clear"})
        panel._output_clear()
        APP.processEvents()
        bus.flush_pending_logs("serial")
        self.assertEqual(self.copy_panel(panel), "")
        self.assertEqual(panel._output.toPlainText(), "")
        bus.queue_log("serial", {"text": "arrived after Clear"})
        self.assertIn("arrived after Clear", self.copy_panel(panel))

    def test_copy_is_safe_for_nul_and_preserves_device_timestamps(self):
        bus = self.bus()
        panel = self.panel(bus)
        bus.queue_log("serial", {"text": "banner", "newline": True})
        bus.queue_log("serial", {"text": "sd\x00after zero", "newline": True})
        bus.queue_log("serial", {"text": "[12:34:56] device message", "newline": True})
        copied = self.copy_panel(panel)
        self.assertNotIn("\x00", copied)
        self.assertIn("sd", copied)
        self.assertIn("after zero", copied)
        self.assertIn("[12:34:56] device message", copied)
        self.assertIn("\\x00", copied)
        if os.name == "nt":
            self.assertEqual(self.native_clipboard_text(), copied)

    def test_pause_retains_worker_output_for_copy_and_resume(self):
        bus = self.bus()
        panel = self.panel(bus)
        bus.queue_log("serial", {"text": "before Pause"})
        self.copy_panel(panel)
        panel._output._flush_queue()
        frozen = panel._output.toPlainText()
        panel._on_pause_toggle(True)
        bus.queue_log("serial", {"text": "received while paused"})
        copied = self.copy_panel(panel)
        self.assertIn("received while paused", copied)
        self.assertEqual(panel._output.toPlainText(), frozen)
        panel._on_pause_toggle(False)
        while panel._output._queue:
            panel._output._flush_queue()
        self.assertIn("received while paused", panel._output.toPlainText())

    def test_worker_flood_is_bounded_and_owner_thread_delivers_latest(self):
        bus = self.bus()
        bus.set_serial_generation(3)
        received = []
        owner_threads = []
        threads = []
        def collect(item):
            received.append(item)
            owner_threads.append(QThread.currentThread() is APP.thread())
        bus.serial_log.connect(collect)
        def produce(worker):
            for number in range(4000):
                bus.queue_log("serial", {"text": f"worker {worker} row {number}:" + "x" * 256,
                                         "generation": 3, "newline": True})
        for worker in range(3):
            thread = threading.Thread(target=produce, args=(worker,))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        bus.queue_log("serial", {"text": "latest sentinel", "generation": 3})
        buffer = bus._pending_logs["serial"]
        self.assertLessEqual(buffer.chars, buffer.max_chars)
        self.assertLessEqual(len(buffer), buffer.max_items)
        self.assertGreater(buffer.dropped, 0)
        bus.flush_pending_logs("serial")
        self.assertEqual(received[-1]["text"], "latest sentinel")
        self.assertTrue(any("older entries omitted" in item["text"] for item in received))
        self.assertTrue(all(owner_threads))
        self.assertFalse(buffer)

    @staticmethod
    def native_clipboard_text():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        for _ in range(20):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.005)
        else:
            raise AssertionError("Could not inspect fixture clipboard")
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                raise AssertionError("Fixture clipboard has no Unicode text")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise AssertionError("Could not read fixture clipboard")
            try:
                return ctypes.wstring_at(pointer).replace("\r\n", "\n")
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    @unittest.skipUnless(os.name == "nt", "Native Windows clipboard regression")
    def test_native_clipboard_reproduces_original_nul_truncation(self):
        APP.clipboard().setText("banner\nsd\x00visible tail")
        self.assertEqual(self.native_clipboard_text(), "banner\nsd")


if __name__ == "__main__":
    if "--no-finalize" in sys.argv:
        # Preserve and restore native clipboard data in unittest cleanup, then
        # avoid the PySide refcount failure seen only during interpreter exit.
        sys.argv.remove("--no-finalize")
        checks = unittest.main(verbosity=2, exit=False)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0 if checks.result.wasSuccessful() else 1)
    unittest.main(verbosity=2)
