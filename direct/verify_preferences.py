#!/usr/bin/env python3
"""Hardware-free preference migration, restart, UI and failure checks.

All configuration writes are redirected to an owned temp fixture; terminals
remain hidden and neither hardware nor notification-history writes are used.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox
from main.core import config
from main.qt.console_panel import ConsolePanelContainer
from main.qt.editor_panel import EditorBridgeAPI
from main.qt.serial_panel import SerialPanel
from main.qt.settings_dialog import SettingsDialog
from main.qt.theme import build_stylesheet
from main.qt.toolbar import ControlsBar
from main.qt.signals import signals

APP = QApplication.instance() or QApplication([])


class PreferenceChecks(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "temp/audit"
        directory.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(dir=directory, prefix="preferences-")
        self.addCleanup(self.fixture.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.fixture.name)
        self.stack.enter_context(patch.object(Path, "home", return_value=folder))
        self.stack.enter_context(patch.object(config, "LOCAL_GUI_CONFIG", folder / "portable.json"))
        self.stack.enter_context(patch.object(config, "_CONFIG_MEM_SIGNATURE", None))
        self.stack.enter_context(patch.object(config, "_CONFIG_MEM_CACHE", {}))
        self.stack.enter_context(patch.object(config, "_INSTANCE_ID", "fixture-1"))
        self.stack.enter_context(patch.object(config, "_own_create_time", return_value=100.0))
        self.stack.enter_context(patch.object(config, "_get_alive_pid_create_times", return_value=None))
        self.stack.enter_context(patch.object(QMessageBox, "critical"))
        self.notices = []
        signals.notification.connect(self.notices.append)
        self.addCleanup(signals.notification.disconnect, self.notices.append)
        self.backend = SimpleNamespace(
            current_board="", current_port="", current_baud=115200, upload_speed="460800",
            is_busy=False, serial_running=False, timestamp_enabled=False,
            clear_console_on_action=True, clear_serial_on_action=False,
            get_settings=lambda: {}, _scan_ports=lambda: [],
            set_skip_compile=Mock(), update_skip_compile_availability=Mock(),
            _resolve_board_info=lambda *args: {},
        )

    def own(self, widget):
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.close)
        return widget

    def tearDown(self):
        APP.setStyleSheet("")
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_bootstrap_only_migration_preserves_shared_fonts_and_unknown_keys(self):
        self.assertTrue(config._save_raw_config({"shared": {
            "monitor_font_size": 22, "editor_font_size": 27,
            "autosave_enabled": True, "theme_mode": "solarized_dark", "other": {"kept": True},
        }}))
        cfg = config.load_gui_config()
        self.assertEqual(cfg["selected_board"], "")
        self.assertEqual(config.get_monitor_font_size(), 22)
        self.assertEqual(config.get_editor_font_size(), 27)
        self.assertTrue(config.get_autosave_settings()[0])
        self.assertEqual(config._load_raw_config()["shared"]["other"], {"kept": True})

    def test_new_pid_inherits_checkbox_choices_but_not_hardware(self):
        cfg = config.load_gui_config()
        for key, value in {
            "timestamp_enabled": True, "clear_console_on_action": False,
            "clear_serial_on_action": True, "build_autoscroll": False,
            "serial_autoscroll": False, "serial_ansi_clear": False, "skip_compile": False,
            "selected_board": "Fixture board", "selected_port": "COM999",
        }.items():
            cfg[key] = value
        self.assertTrue(config.save_gui_config(cfg))
        with patch.object(config, "_INSTANCE_ID", "fixture-2"):
            restored = config.load_gui_config()
        for key in config._INSTANCE_PREFERENCE_DEFAULTS:
            self.assertEqual(restored[key], cfg[key])
        self.assertEqual(restored["selected_board"], "")
        self.assertNotIn("selected_port", restored)

    def test_older_instance_preferences_are_migrated_on_restart(self):
        self.assertTrue(config._save_raw_config({"instances": {"exited": {
            "timestamp_enabled": True, "clear_console_on_action": False,
            "clear_serial_on_action": True, "selected_board": "Not inherited",
        }}, "shared": {"editor_font_size": 24}}))
        restored = config.load_gui_config()
        self.assertTrue(restored["timestamp_enabled"])
        self.assertFalse(restored["clear_console_on_action"])
        self.assertTrue(restored["clear_serial_on_action"])
        self.assertEqual(restored["selected_board"], "")
        self.assertEqual(config.get_editor_font_size(), 24)

    def test_stale_instance_snapshot_does_not_override_new_shared_preference(self):
        original = config.load_gui_config()
        with patch.object(config, "_INSTANCE_ID", "fixture-2"):
            second = config.load_gui_config()
            second["timestamp_enabled"] = True
            self.assertTrue(config.save_gui_config(second))
        original["window_width"] = 1100
        self.assertTrue(config.save_gui_config(original))
        self.assertTrue(config._load_raw_config()["shared"]["timestamp_enabled"])
        raw_a = config._load_raw_config()
        raw_b = config._load_raw_config()
        raw_a["shared"]["editor_font_size"] = 25
        raw_b["shared"]["monitor_font_size"] = 20
        self.assertTrue(config._save_raw_config(raw_a))
        self.assertTrue(config._save_raw_config(raw_b))
        self.assertEqual(config.get_editor_font_size(), 25)
        self.assertEqual(config.get_monitor_font_size(), 20)

    def test_panels_restore_auto_scroll_clear_screen_and_rendered_font(self):
        cfg = config.load_gui_config()
        cfg.update(build_autoscroll=False, serial_autoscroll=False, serial_ansi_clear=False)
        self.assertTrue(config.save_gui_config(cfg))
        self.assertTrue(config.set_monitor_font_size(22))
        APP.setStyleSheet(build_stylesheet("default"))
        build = self.own(ConsolePanelContainer(self.backend))
        serial = self.own(SerialPanel(self.backend))
        build.ensurePolished()
        serial.ensurePolished()
        self.assertFalse(build.header.cb_autoscroll.isChecked())
        self.assertFalse(build.console._autoscroll)
        self.assertFalse(serial.cb_autoscroll.isChecked())
        self.assertFalse(serial._output._autoscroll)
        self.assertFalse(serial._output._ansi_clear_enabled)
        self.assertEqual(build.console.font().pointSize(), 22)
        self.assertEqual(serial._output.font().pointSize(), 22)
        serial.apply_theme("light")
        self.assertEqual(serial._output.font().pointSize(), 22)
        serial.cb_ansi_clear.setChecked(True)
        self.assertTrue(config.load_gui_config()["serial_ansi_clear"])

    def test_checkbox_failure_reverts_without_history_or_live_mutation(self):
        panel = self.own(ConsolePanelContainer(self.backend))
        serial = self.own(SerialPanel(self.backend))
        with patch.object(config, "save_gui_config", return_value=False):
            panel.header.cb_auto_clear.setChecked(False)
            panel.header.cb_autoscroll.setChecked(False)
            serial.cb_ansi_clear.setChecked(False)
            serial.cb_autoscroll.setChecked(False)
        self.assertTrue(panel.header.cb_auto_clear.isChecked())
        self.assertTrue(self.backend.clear_console_on_action)
        self.assertTrue(panel.console._autoscroll)
        self.assertTrue(serial._output._ansi_clear_enabled)
        self.assertTrue(serial._output._autoscroll)
        self.assertEqual(len(self.notices), 4)
        self.assertTrue(all(item["type"] == "warning" for item in self.notices))

    def test_skip_choice_survives_availability_and_restart(self):
        controls = self.own(ControlsBar(self.backend))
        controls.on_skip_compile_availability(True)
        self.assertTrue(controls.cb_skip_compile.isChecked())
        controls.cb_skip_compile.setChecked(False)
        controls.on_skip_compile_availability(False)
        controls.on_skip_compile_availability(True)
        self.assertFalse(controls.cb_skip_compile.isChecked())
        self.backend.set_skip_compile.assert_called_with(False)
        with patch.object(config, "_INSTANCE_ID", "fixture-2"):
            second = self.own(ControlsBar(self.backend))
        second.on_skip_compile_availability(True)
        self.assertFalse(second.cb_skip_compile.isChecked())
        self.assertFalse(config._load_raw_config()["shared"]["skip_compile"])

    def test_compact_options_auto_hide_releases_popup(self):
        controls = self.own(ControlsBar(self.backend))
        controls._toggle_options_menu()
        self.assertIsNotNone(controls._opt_popup)
        controls._opt_popup.hide()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNone(controls._opt_popup)

    def test_full_saved_font_range_and_failed_editor_save(self):
        self.assertTrue(config.set_monitor_font_size(48))
        dialog = self.own(SettingsDialog(self.backend))
        self.assertEqual(dialog.font_combo.currentData(), 48)
        bridge = EditorBridgeAPI(self.backend)
        self.addCleanup(bridge.deleteLater)
        with patch.object(config, "set_editor_font_size", return_value=False):
            result = bridge.save_font_size(24)
        self.assertFalse(result["success"])
        self.assertEqual(self.notices[-1]["title"], "Setting not saved")

    def test_both_terminal_hosts_restore_font_without_starting_sessions(self):
        from main.qt.terminal_panel import TerminalPanel
        from main.qt.posix_terminal_panel import PosixTerminalPanel
        self.assertTrue(config.set_monitor_font_size(21))
        with patch.object(TerminalPanel, "ensure_started") as windows_start, \
             patch.object(PosixTerminalPanel, "add_session") as posix_start:
            windows = self.own(TerminalPanel(self.backend))
            posix = self.own(PosixTerminalPanel(self.backend))
            self.assertEqual(windows._current_font_size, 21)
            self.assertEqual(posix._font_size, 21)
            self.assertFalse(windows._is_active)
            self.assertFalse(posix._is_active)
            windows_start.assert_not_called()
            posix_start.assert_not_called()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PreferenceChecks))
    raise SystemExit(0 if result.wasSuccessful() else 1)
