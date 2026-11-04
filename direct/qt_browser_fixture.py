"""Real package browser in an offscreen window with no live settings or networking."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent, QEventLoop
from PySide6.QtWidgets import QApplication

from main.core.theme import Theme
from main.qt import package_browser as pb
from src.modules import arduino_lib_req as engine

APP = QApplication.instance() or QApplication(["verify-package-browser"])
pb.install_no_focus_lines(APP)


def tasks_idle(tasks) -> bool:
    with tasks._lock:
        return not (tasks._queue or tasks._latest or tasks._scheduled)


class QtBrowserFixture(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "temp/audit/downloader"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        downloads = self.folder / "downloads"
        downloads.mkdir()
        (self.folder / "jobs").mkdir()
        self.main_thread = threading.get_ident()
        self.dialogs = []
        for target, value in ((engine, {"SETTINGS_FILE": str(self.folder / "settings.json"),
                                        "INDEX_CACHE_DIR": str(self.folder),
                                        "LIBRARY_CACHE_FILE": str(self.folder / "library_index.json"),
                                        "BOARD_CACHE_FILE": str(self.folder / "package_index.json"),
                                        "DEFAULT_DOWNLOAD_DIR": str(downloads)}),):
            patcher = patch.multiple(target, **value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for module, name, replacement in ((engine, "_resolve_lib_req_theme", lambda: "default"),
                                          (engine.requests, "get", self._no_network),
                                          ("urllib.request", "urlopen", self._no_network)):
            patcher = patch(f"{module}.{name}", replacement) if isinstance(module, str) else patch.object(module, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("critical", "information"):
            patcher = patch.object(pb.QMessageBox, name, side_effect=lambda *args, kind=name: self.dialogs.append((kind, args)))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(Theme.apply_theme, Theme.active_theme)
        self.app = pb.ArduinoBrowser()
        self.app._package_event_root = str(self.folder / "jobs")
        self.addCleanup(self.retire)

    def _no_network(self, *args, **kwargs):
        raise AssertionError(f"Live network access attempted: {args}")

    def retire(self):
        window = self.app
        window._exiting = True
        window._tasks.close()
        window.close()
        window.deleteLater()
        APP.processEvents()
        # Event type 0 excludes deferred deletes. Every retired window would then stay
        # alive, and each later stylesheet change would repolish all of them.
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    def set_download_dir(self, path):
        os.makedirs(path, exist_ok=True)
        self.app._download_dir = str(path)
        self.app.folder_entry.setText(str(path))

    def pump(self, seconds=.02):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
            time.sleep(.001)

    def until(self, predicate, timeout=5, message="Timed out waiting for the isolated worker"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
            if predicate():
                return
            time.sleep(.002)
        self.fail(message)

    def settle(self):
        self.until(lambda: tasks_idle(self.app._tasks))

    def select(self, tab, row=0):
        tab.listbox.setCurrentIndex(tab.model.index(row))
        self.settle()
