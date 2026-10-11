"""Tk-only downloader fixture; bypass live settings, discovery and networking."""
from __future__ import annotations

import gc
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock

from src.modules import arduino_lib_req as downloader

ROOT = Path(__file__).resolve().parents[1]


class TkDownloaderFixture(unittest.TestCase):
    def setUp(self):
        # Retire previous disposable Tcl wrapper cycles on their UI thread
        # before the next fixture starts catalog or disk workers.
        gc.collect()
        self.module = downloader
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk desktop unavailable: {error}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.addCleanup(lambda: self.assertEqual(self.callback_errors, [], "Tk callback failed"))
        self.addCleanup(downloader.Theme.apply_theme, downloader.Theme.active_theme)
        app = downloader.ArduinoBrowser.__new__(downloader.ArduinoBrowser)
        app.root = self.root
        app._busy, app._is_online, app._is_hidden = False, True, False
        app._download_dir = str(ROOT / "temp/audit/fixture-download")
        app._additional_board_urls, app._installed_items = [], []
        fixture_root = ROOT / "temp/audit/downloader-jobs"
        fixture_root.mkdir(parents=True, exist_ok=True)
        event_fixture = tempfile.TemporaryDirectory(dir=fixture_root)
        self.addCleanup(event_fixture.cleanup)
        app._package_event_root = event_fixture.name
        app._compute_installed_items_async = Mock()
        app._compute_installed_items = Mock()
        app._update_version_status = Mock()
        app._build_ui()
        self.app = app
