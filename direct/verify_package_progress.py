"""Hardware-free package card, delivery and operation exclusion regressions."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow, QWidget, QPushButton
from main.core.theme import Theme
from main.core.package_activity import PackageActivityMonitor
from main.qt.package_progress import PackageProgressCard
from src.modules import package_jobs as jobs

APP = QApplication.instance() or QApplication([])
from main.qt.theme import register_fonts
register_fonts()
APP.setFont(QFont("Montserrat", 10))


def event(job="fixture", seq=1, stage="preparing", progress=None):
    return {"job_id": job, "seq": seq, "stage": stage, "progress": progress,
            "title": "NodeMCU 1.0 (ESP-12E Module)", "created": seq,
            "message": "Preparing the exact ESP8266 Arduino target and its dependencies"}


class PackageProgressChecks(unittest.TestCase):
    def setUp(self):
        self.window = QMainWindow()
        self.window.resize(800, 600)
        self.window.setCentralWidget(QWidget())
        self.editor = QLineEdit(self.window.centralWidget())
        self.editor.setGeometry(10, 10, 200, 30)
        self.card = PackageProgressCard(self.window)
        self.window.show()
        self.window.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(self.window, 2000))
        self.editor.setFocus()
        APP.processEvents()
        self.assertIs(APP.focusWidget(), self.editor)
        self.addCleanup(self.window.close)

    def test_card_is_child_and_does_not_steal_editor_focus(self):
        self.card.update_job(event())
        APP.processEvents()
        self.assertIs(self.card.parentWidget(), self.window)
        self.assertFalse(self.card.isWindow())
        self.assertIs(APP.focusWidget(), self.editor)
        self.assertTrue(self.card.isVisible())

    def test_resize_and_all_palettes_keep_card_inside_window(self):
        for theme in Theme.PALETTES:
            self.card.apply_theme(theme)
            for width, height in ((800, 600), (400, 340), (320, 300), (640, 400)):
                self.window.resize(width, height)
                self.card.update_job(event(seq=width + height, progress=42))
                APP.processEvents()
                self.assertTrue(self.window.rect().contains(self.card.geometry()))
                self.assertEqual(self.card.progress.value(), 42)
                for button in self.card.findChildren(QPushButton):
                    self.assertTrue(self.card.rect().contains(button.geometry()))

    def test_unknown_progress_has_no_fake_percent_or_spinner(self):
        self.card.update_job(event())
        self.assertFalse(self.card.progress.isVisible())
        self.assertNotIn("%", self.card.phase.text())

    def test_dismissal_keeps_job_hidden_and_new_job_appears(self):
        self.card.update_job(event())
        self.card._dismiss()
        self.card.update_job(event(seq=2, stage="ready", progress=100))
        self.assertFalse(self.card.isVisible())
        self.card.update_job(event(job="other", seq=3))
        self.assertTrue(self.card.isVisible())

    def test_stale_events_cannot_replace_current_result(self):
        self.card.update_job(event(seq=3, stage="ready", progress=100))
        self.card.update_job(event(seq=2))
        self.assertEqual(self.card.current_job()["stage"], "ready")

    def test_details_only_opens_after_explicit_click(self):
        callback = Mock()
        self.card.details_requested.connect(callback)
        self.card.update_job(event())
        callback.assert_not_called()
        button = next(b for b in self.card.findChildren(QPushButton) if b.text() == "Details")
        button.click()
        callback.assert_called_once()


class PackageDeliveryChecks(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "temp/audit/package-progress"
        parent.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.backend = SimpleNamespace(emit=Mock(), is_busy=False,
                                       _catalog_refresh_running=False, refresh_board_catalog=Mock())
        self.monitor = PackageActivityMonitor(self.backend, threading.Event(), self.root)
        self.monitor.reader.since = 0

    def test_percentage_coalescing_keeps_stages_and_refreshes_partial_result(self):
        jobs.publish_event("flow", "queued", root=self.root)
        jobs.publish_event("flow", "downloading", root=self.root, progress=20)
        jobs.publish_event("flow", "downloading", root=self.root, progress=40)
        jobs.publish_event("flow", "unavailable", root=self.root, ready_count=1)
        self.monitor.poll()
        notifications = [c.args[1] for c in self.backend.emit.call_args_list if c.args[0] == "notification"]
        self.assertEqual([n["details"]["stage"] for n in notifications], ["queued", "downloading", "unavailable"])
        self.assertTrue(all(n["category"] == "board_install" for n in notifications))
        self.backend.refresh_board_catalog.assert_called_once_with(invalidate_parsed=True)
        self.backend.emit.reset_mock()
        self.monitor.poll()
        self.backend.emit.assert_not_called()

    def test_catalog_refresh_waits_until_build_finishes(self):
        self.backend.is_busy = True
        jobs.publish_event("flow", "ready", root=self.root)
        self.monitor.poll()
        self.backend.refresh_board_catalog.assert_not_called()
        self.backend.is_busy = False
        self.monitor.poll()
        self.backend.refresh_board_catalog.assert_called_once()

    def test_operation_guard_excludes_preparation_and_releases_busy(self):
        backend = SimpleNamespace(emit=Mock(), _release_requested_operation=Mock(), _package_event_root=self.root)
        method = Mock(return_value=True)
        guarded = jobs.guarded_package_operation(method)
        core = self.root / "store"
        with patch.object(jobs, "package_core_directory", return_value=core):
            with jobs.package_store_lease(core, "prepare", root=self.root):
                self.assertFalse(guarded(backend))
                method.assert_not_called()
                backend._release_requested_operation.assert_called_once()
            self.assertTrue(guarded(backend))
            method.assert_called_once()

    def test_refresh_race_keeps_the_refresh_pending(self):
        self.backend.refresh_board_catalog.side_effect = [False, True]
        jobs.publish_event("flow", "ready", root=self.root)
        self.monitor.poll()
        self.assertTrue(self.monitor.pending_refresh)
        self.monitor.poll()
        self.assertFalse(self.monitor.pending_refresh)
        self.assertEqual(self.backend.refresh_board_catalog.call_count, 2)

    def test_operation_filesystem_error_is_not_mislabeled_as_preparation(self):
        backend = SimpleNamespace(emit=Mock(), _release_requested_operation=Mock(), _package_event_root=self.root)
        method = Mock(side_effect=OSError("fixture build failure"))
        core = self.root / "store"
        with patch.object(jobs, "package_core_directory", return_value=core):
            with self.assertRaisesRegex(OSError, "fixture build failure"):
                jobs.guarded_package_operation(method)(backend)
            # A failed operation still releases its read lease.
            with jobs.package_store_lease(core, "prepare", root=self.root):
                pass
        backend.emit.assert_not_called()


def capture(directory):
    directory.mkdir(parents=True, exist_ok=True)
    window = QMainWindow()
    window.resize(680, 420)
    window.setCentralWidget(QWidget())
    card = PackageProgressCard(window)
    window.show()
    for seq, theme in enumerate(Theme.PALETTES, 1):
        Theme.apply_theme(theme)
        from main.qt.theme import build_stylesheet
        APP.setStyleSheet(build_stylesheet(theme))
        card.apply_theme(theme)
        card.update_job(event(seq=seq, progress=42))
        APP.processEvents()
        assert card.grab().save(str(directory / f"package-card-{theme}.png"))
    window.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path)
    options = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    if options.render_dir:
        capture(options.render_dir)
    raise SystemExit(0 if result.wasSuccessful() else 1)
