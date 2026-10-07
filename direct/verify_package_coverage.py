#!/usr/bin/env python3
"""Isolated coverage report safety, async loading, filtering and notification links."""
from __future__ import annotations

import json
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
os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from PySide6.QtCore import QUrl, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from main.qt import package_coverage as coverage
from main.qt.notif_panel import NotifPanel
from src.modules import package_jobs

APP = QApplication.instance() or QApplication([])


class CoverageChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit/package-coverage"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = self.root / "events"
        self.env = patch.dict(os.environ, {"MCU_PACKAGE_EVENTS_ROOT": str(self.events)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.theme = patch("main.core.config.get_theme_mode", return_value="default")
        self.theme.start()
        self.addCleanup(self.theme.stop)
        self.rows = [{"name": "NodeMCU 0.9 (ESP-12 Module)", "status": "ready", "platform": "espressif8266", "board": "nodemcu", "reason": "Exact target prepared"},
                     {"name": "NodeMCU 1.0 (ESP-12E Module)", "status": "ready", "platform": "espressif8266", "board": "nodemcuv2", "reason": "Exact target prepared"},
                     {"name": "Custom board", "status": "unavailable", "platform": "", "board": "", "reason": "No verified PlatformIO definition"}]
        self.path = self.write(self.rows)

    def write(self, rows, **fields):
        return package_jobs.write_report("fixture-job", dict(schema=1, job_id="fixture-job", boards=rows, **fields), root=self.events)

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertTrue(predicate(), "Queued coverage update did not complete")

    def dialog(self):
        dialog = coverage.PackageCoverageDialog("fixture-job")
        self.addCleanup(dialog.close)
        dialog.show()
        self.wait(lambda: dialog.model.rowCount() == 3)
        return dialog

    def dispose(self, widget):
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_complete_reports_keep_every_board_beyond_event_snapshot_limit(self):
        self.write([dict(self.rows[0], name=f"Board {number}") for number in range(1500)])
        report = coverage.read_package_coverage("fixture-job")
        self.assertEqual(len(report["rows"]), 1500)
        self.assertEqual(report["rows"][-1]["name"], "Board 1499")

    def test_invalid_job_and_other_job_report_are_rejected(self):
        for identifier in ("../fixture-job", "fixture-job/../../outside", "", "https://example.com"):
            with self.assertRaisesRegex(ValueError, "identity"):
                coverage.read_package_coverage(identifier)
        self.path.write_text(json.dumps({"schema": 1, "job_id": "other-job", "boards": self.rows}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "another job"):
            coverage.read_package_coverage("fixture-job")

    def test_resolved_report_escape_is_rejected_before_file_read(self):
        original = Path.resolve
        outside = self.root / "outside.json"
        def resolve(path, *args, **kwargs):
            if path == self.path:
                return outside
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve), patch.object(Path, "open") as opened:
            with self.assertRaisesRegex(ValueError, "outside"):
                coverage.read_package_coverage("fixture-job")
        opened.assert_not_called()

    def test_oversized_report_and_rows_are_refused_without_partial_success(self):
        self.path.write_bytes(b" " * (coverage.MAX_REPORT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "16 MB"):
            coverage.read_package_coverage("fixture-job")
        self.write([self.rows[0]] * (coverage.MAX_REPORT_ROWS + 1))
        with self.assertRaisesRegex(ValueError, "25,000"):
            coverage.read_package_coverage("fixture-job")

    def test_invalid_row_or_unrecognized_status_is_rejected(self):
        for row in (dict(self.rows[0], status="guessed"), dict(self.rows[0], reason=["invalid"]), dict(self.rows[0], reason="x" * 8193)):
            self.write([row])
            with self.assertRaisesRegex(ValueError, "invalid"):
                coverage.read_package_coverage("fixture-job")

    def test_arduino_fallback_has_exact_identity_backend_and_notice(self):
        self.write([dict(self.rows[0], platform='vendor:future', board='exact', arduino_fqbn='vendor:future:exact',
            backend='arduino-cli', reason='PlatformIO does not support this exact board; Arduino CLI prepared')])
        row = coverage.read_package_coverage('fixture-job')['rows'][0]
        self.assertEqual(row['target'], 'vendor:future:exact')
        self.assertEqual(row['backend_label'], 'Arduino CLI')
        self.assertIn('arduino cli', row['search'])
        self.assertIn('does not support this exact board', row['reason'])

    def test_platformio_target_is_retained_with_arduino_source_identity(self):
        self.write([dict(self.rows[1], backend='platformio', arduino_fqbn='esp8266:esp8266:nodemcuv2')])
        row = coverage.read_package_coverage('fixture-job')['rows'][0]
        self.assertEqual(row['target'], 'espressif8266:nodemcuv2')
        self.assertEqual(row['backend_label'], 'PlatformIO')

    def test_load_is_off_ui_thread_and_model_is_virtual(self):
        called = []
        original = coverage.read_package_coverage
        def read(*args, **kwargs):
            called.append(threading.get_ident())
            return original(*args, **kwargs)
        with patch.object(coverage, "read_package_coverage", side_effect=read):
            dialog = self.dialog()
        self.assertEqual(len(called), 1)
        self.assertNotEqual(called[0], threading.get_ident())
        self.assertIsInstance(dialog.table.model(), coverage.CoverageModel)
        self.assertEqual(dialog.model.data(dialog.model.index(1, 2)), "espressif8266:nodemcuv2")
        dialog.table.setCurrentIndex(dialog.model.index(2, 0))
        self.assertIn("No verified PlatformIO definition", dialog.reason.toPlainText())

    def test_latest_search_and_status_filter_keep_complete_source_rows(self):
        dialog = self.dialog()
        dialog.search.setText("NodeMCU")
        dialog.search.setText("ESP-12E")
        self.wait(lambda: dialog.model.rowCount() == 1)
        self.assertEqual(dialog.model.data(dialog.model.index(0, 2)), "espressif8266:nodemcuv2")
        dialog.search.clear()
        dialog.status.setCurrentIndex(dialog.status.findData("unavailable"))
        self.wait(lambda: dialog.model.rowCount() == 1 and dialog.model.data(dialog.model.index(0, 0)) == "Custom board")
        self.assertEqual(len(dialog.model.rows), 3)

    def test_theme_switch_preserves_search_selection_and_loaded_rows(self):
        dialog = self.dialog()
        dialog.search.setText("NodeMCU")
        self.wait(lambda: dialog.model.rowCount() == 2)
        dialog.table.setCurrentIndex(dialog.model.index(1, 0))
        for mode in ("default", "light", "solarized_dark"):
            dialog.apply_theme(mode)
            QTest.qWait(30)
            self.assertIn(coverage.get_palette(mode)["BG_DARKEST"], dialog.styleSheet())
            self.assertEqual(dialog.search.text(), "NodeMCU")
            self.assertEqual(dialog.model.rowCount(), 2)
            self.assertEqual(dialog.table.currentIndex().row(), 1)
            self.assertGreater(dialog.count.geometry().top(), dialog.reason.geometry().bottom())
            render = ROOT / "temp/audit/package-coverage"
            dialog.grab().save(str(render / f"coverage-{mode}.png"))

    def test_missing_report_has_clear_readable_failure(self):
        self.path.unlink()
        dialog = coverage.PackageCoverageDialog("fixture-job")
        self.addCleanup(dialog.close)
        dialog.show()
        self.wait(lambda: not dialog.search.isEnabled())
        self.assertIn("could not be loaded", dialog.summary.text())
        self.assertIn("no longer available", dialog.reason.toPlainText())

    def test_compact_dialog_keeps_controls_reason_and_footer_separate(self):
        dialog = self.dialog()
        dialog.resize(360, 280)
        QTest.qWait(40)
        self.assertGreater(dialog.reason.geometry().top(), dialog.table.geometry().bottom())
        self.assertGreater(dialog.count.geometry().top(), dialog.reason.geometry().bottom())
        self.assertLessEqual(dialog.count.geometry().bottom(), dialog.height() - 8)
        self.assertGreaterEqual(dialog.table.height(), 40)

    def test_notification_preserves_report_identity_live_and_persisted(self):
        details = {"job_id": "fixture-job", "coverage_report": str(self.path)}
        persisted = NotifPanel._display_record({"message": "Prepared boards", "details": details})
        self.assertEqual(persisted["job_id"], "fixture-job")
        self.assertEqual(persisted["coverage_report"], str(self.path))
        with patch.object(NotifPanel, "_request_refresh"):
            panel = NotifPanel(SimpleNamespace(sketch_dir_path=str(self.root)))
        self.addCleanup(self.dispose, panel)
        panel._on_live_notification(dict(type="success", title="Boards prepared", message="Prepared boards", details=details))
        record = list(panel._records)[-1]
        self.assertEqual(record["job_id"], persisted["job_id"])
        self.assertEqual(record["coverage_report"], persisted["coverage_report"])
        self.assertIn("mcu-coverage:fixture-job", panel._format_record_html(record))

    def test_notification_anchors_open_only_safe_job_identity(self):
        with patch.object(NotifPanel, "_request_refresh"):
            panel = NotifPanel(SimpleNamespace(sketch_dir_path=str(self.root)))
        self.addCleanup(self.dispose, panel)
        with patch.object(coverage, "open_package_coverage") as opened:
            for url in ("file:///outside.json", "https://example.com", "mcu-coverage:../outside", "mcu-coverage:fixture-job?path=outside"):
                panel._on_report_link(QUrl(url))
            opened.assert_not_called()
            panel._on_report_link(QUrl("mcu-coverage:fixture-job"))
            opened.assert_called_once_with(panel.window(), "fixture-job")


if __name__ == "__main__":
    unittest.main(verbosity=2)
