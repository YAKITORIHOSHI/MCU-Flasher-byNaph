#!/usr/bin/env python3
"""A fresh install starts in Frost White everywhere, and Settings agrees with it.

Settings opened on a profile with no saved theme used to show Smoked Blue, so Save
switched the theme. Runs the real settings code against a disposable profile under
temp/; the user's own settings are never read or written. Offscreen only.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name in ("DISPLAY", "WAYLAND_DISPLAY"):
    os.environ.pop(name, None)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
(ROOT / "temp").mkdir(exist_ok=True)
PROFILE = Path(tempfile.mkdtemp(prefix="fresh-theme-", dir=ROOT / "temp"))
os.environ["HOME"] = str(PROFILE)  # Before any import that asks for the home folder.

from PySide6.QtWidgets import QApplication, QMessageBox

APP = QApplication.instance() or QApplication([])

from main.core import config
from main.core.theme import DEFAULT_THEME, Theme, _palette_key


class FreshProfile(unittest.TestCase):
    def setUp(self):
        self.stack = patch.object(config, "LOCAL_GUI_CONFIG", PROFILE / "local_gui_config.json")
        self.stack.start()
        self.addCleanup(self.stack.stop)
        for path in PROFILE.glob("*"):
            if path.is_file():
                path.unlink()
        config._load_raw_config(fresh=True)
        Theme.apply_theme(config.get_theme_mode())
        for target in ("src.dbs.dbs_create.add_notification", "src.dbs.dbs_create.queue_notification"):
            stub = patch(target)
            stub.start()
            self.addCleanup(stub.stop)
        for name in ("warning", "critical", "question", "information"):
            stub = patch.object(QMessageBox, name, return_value=QMessageBox.StandardButton.No)
            stub.start()
            self.addCleanup(stub.stop)

    def settings(self):
        from main.qt.settings_dialog import SettingsDialog
        from direct.verify_runtime import PreviewBackend
        dialog = SettingsDialog(PreviewBackend())
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_the_default_is_frost_white(self):
        self.assertEqual(DEFAULT_THEME, "light")
        self.assertEqual(config.get_theme_settings(), ("light", False))
        self.assertEqual(config.get_theme_mode(), "light")
        self.assertEqual(Theme.active_theme, "light")
        for unknown in (None, "", "bogus"):
            self.assertEqual(_palette_key(unknown), "light")
        self.assertEqual(_palette_key("dark"), "default", "an explicit dark choice stays dark")

    def test_settings_shows_the_theme_in_use_and_saving_keeps_it(self):
        from main.qt import settings_dialog
        dialog = self.settings()
        self.assertEqual(dialog._theme_map[dialog.theme_combo.currentText()], "light")
        self.assertFalse(dialog.cb_theme_system.isChecked())
        changed = []
        settings_dialog.signals.theme_changed.connect(changed.append)
        self.addCleanup(settings_dialog.signals.theme_changed.disconnect, changed.append)
        dialog._save_and_apply()
        self.assertEqual(changed, [], "an unchanged Save does not switch the theme")
        self.assertEqual(config._load_raw_config(fresh=True)["shared"]["theme_mode"], "light")
        self.assertEqual(Theme.active_theme, "light")
        reopened = self.settings()
        self.assertEqual(reopened._theme_map[reopened.theme_combo.currentText()], "light")

    def test_reset_defaults_returns_to_frost_white(self):
        dialog = self.settings()
        dialog.theme_combo.setCurrentText("Glass (Smoked blue)")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(dialog, "_save_and_apply"):
            dialog._reset_defaults()
        self.assertEqual(dialog._theme_map[dialog.theme_combo.currentText()], "light")


def tearDownModule():
    shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
