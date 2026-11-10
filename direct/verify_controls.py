#!/usr/bin/env python3
"""Hardware-free Actions, Settings and auxiliary-screen verification.

Setup UI classes are extracted with AST so setup never installs or launches.
The downloader uses a fixture without its persistence/network constructor.
Use --render-dir for previews. Tk checks require a desktop or Xvfb.
"""
from __future__ import annotations

import argparse
import ast
import copy
import gc
import json
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot, QCoreApplication, QEvent
from PySide6.QtGui import QColor, QFont, QIcon, QTextCursor, QTextCharFormat
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                              QProgressBar, QPlainTextEdit, QCheckBox, QFrame,
                              QMainWindow, QMessageBox, QPushButton, QWidget)
from main.core.theme import Theme
from main.qt import settings_dialog as settings_module
from main.qt.toolbar import PrimaryToolbar, ControlsBar, CompactDropdownPopup
from main.qt.garbage_collection import install_gui_garbage_collector
from main.qt.theme import install_no_focus_lines
from src.modules.runtime_resources import performance_profile
from verify_runtime import PreviewBackend

APP = QApplication.instance() or QApplication([])
COLLECTOR = install_gui_garbage_collector(APP)
install_no_focus_lines(APP)
RENDER_DIR = None


def bootstrap_fixture(mode="default", cores=4):
    """Construct only the lazy setup view; every persistence callback is a mock."""
    from src.modules.bootstrap_dispatch import BootstrapDispatcher
    from src.modules.bootstrap_qt import BootstrapDialog
    source = ROOT / "src/modules/bootstrap.py"
    palette = {"T_" + key: value for key, value in Theme.PALETTES[mode].items()}
    namespace = dict(globals(), HAS_PYSIDE6_BOOTSTRAP=True, SCRIPT_DIR=ROOT,
                     __file__=str(source), _T_PALETTE=palette,
                     _resolve_bootstrap_theme=lambda: (palette, mode),
                     load_bootstrap_config=Mock(return_value={"other": "preserved"}),
                     save_bootstrap_config=Mock(return_value=True),
                     _record_bootstrap_exception=Mock())
    gui = SimpleNamespace(_status_text="Verifying the private runtime…", _skip_updates=True,
                          _signals=BootstrapDispatcher(), _start_time=time.time(),
                          _closed=False, _spinning=True)
    context = SimpleNamespace(root=ROOT, palette=palette,
                              resolve_theme=namespace["_resolve_bootstrap_theme"],
                              load_config=namespace["load_bootstrap_config"],
                              save_config=namespace["save_bootstrap_config"],
                              record_exception=namespace["_record_bootstrap_exception"])
    with patch("src.modules.runtime_resources.performance_profile", return_value=performance_profile(cores, 8)):
        dialog = BootstrapDialog(gui, context)
        dialog.bind_dispatcher()
    return dialog, gui, namespace


class ControlChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.original_theme = Theme.active_theme
        self.addCleanup(Theme.apply_theme, self.original_theme)
        self.config = {"shared": {"theme_mode": "solarized_dark", "theme_follow_system": False,
                                  "editor_mode": "legacy", "cpu_multithreading": "HIGH"},
                       "instances": {"other": {"last_sketch_dir": "kept"}}}
        for name, value in (("_load_raw_config", self.config),
                            ("get_theme_settings", ("solarized_dark", False)),
                            ("get_monitor_font_size", 11), ("get_hide_build_console_warnings", False),
                            ("get_autosave_settings", (False, 1200)), ("get_reset_on_baud_change", False)):
            self.stack.enter_context(patch.object(settings_module, name, return_value=value))
        self.save = self.stack.enter_context(patch.object(settings_module, "_save_raw_config", return_value=True))
        self.stack.enter_context(patch("src.dbs.dbs_create.add_notification"))
        self.stack.enter_context(patch.object(QMessageBox, "warning"))
        self.stack.enter_context(patch.object(QMessageBox, "critical"))
        self.question = self.stack.enter_context(patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes))
        self.backend = PreviewBackend()
        self.info = {"platform": "espressif32", "board": "esp32dev", "framework": "arduino", "pio_resolved": True}
        self.backend._resolve_board_info = lambda _name=None: self.info if self.backend.current_board else {}
        for name in ("compile_sketch", "upload_sketch", "stop_operation", "clean_cache", "soft_reset", "hard_reset", "set_reset_on_baud_change"):
            setattr(self.backend, name, Mock())

    def own(self, widget):
        def dispose():
            if hasattr(widget, "_on_close"):
                widget._on_close()
            else:
                widget.close()
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return widget

    def settings(self):
        return self.own(settings_module.SettingsDialog(self.backend))

    def test_compact_settings_releases_popup_and_retires_each_dialog(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        window = self.own(QMainWindow())
        controls = ControlsBar(self.backend, window)
        window.setCentralWidget(controls)
        window.resize(700, 220)
        window.show()
        APP.processEvents()
        for _ in range(8):
            controls._toggle_options_menu()
            popup = controls._opt_popup
            button = next(b for b in popup.findChildren(QPushButton) if b.text() == "Settings")
            button.click()
            self.assertIsNone(getattr(controls, "_settings_dialog", None), "Dialog ran inside popup click")
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()
            dialog = controls._settings_dialog
            self.assertIsNotNone(dialog)
            self.assertIsNone(APP.activePopupWidget())
            self.assertEqual(APP.activeModalWidget(), dialog)
            controls._open_settings()
            self.assertEqual(window.findChildren(settings_module.SettingsDialog), [dialog])
            ticks = []
            timer = QTimer(dialog)
            timer.setInterval(5)
            timer.timeout.connect(lambda: ticks.append(True))
            timer.start()
            # Wait for actual dispatch, allowing native paint and Windows timer
            # granularity under load; a fixed 30 ms can expire during one paint.
            deadline = time.monotonic() + 1.0
            while not ticks and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertGreater(len(ticks), 0, "Qt stopped dispatching while Settings was open")
            self.assertEqual(dialog.autosave_spin.buttonSymbols(), dialog.autosave_spin.ButtonSymbols.NoButtons)
            dialog.cb_autosave.setChecked(True)
            dialog.autosave_spin.setFocus()
            dialog.autosave_spin.selectAll()
            QTest.keyClicks(dialog.autosave_spin, "2100")
            dialog.autosave_spin.interpretText()
            self.assertEqual(dialog.autosave_spin.value(), 2100)
            dialog.reject()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertIsNone(controls._settings_dialog)
            self.assertEqual(window.findChildren(settings_module.SettingsDialog), [])

    def test_update_prompt_stays_on_top_and_skip_survives_failed_persistence(self):
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QMessageBox
        dialog, gui, namespace = bootstrap_fixture()
        self.own(dialog)
        namespace["save_bootstrap_config"].return_value = False
        dialog.skip_cb.setChecked(False)
        self.assertFalse(gui._skip_updates)
        self.assertFalse(dialog.skip_cb.isChecked())
        dialog.show()
        answers = []
        dialog.ask_update(2, answers.append)
        prompt = dialog._update_prompt
        self.assertIsNotNone(prompt)
        QTest.qWait(1100)  # The setup's initial elevation expires; the prompt's must not.
        self.assertTrue(prompt.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(prompt.isVisible())
        prompt.button(QMessageBox.StandardButton.No).click()
        self.assertEqual(answers, [False])
        dialog.skip_cb.setChecked(True)
        self.assertTrue(gui._skip_updates)
        self.assertTrue(dialog.skip_cb.isChecked())
        dialog.ask_update(2, answers.append)
        self.assertIsNone(dialog._update_prompt)
        self.assertEqual(answers, [False, False])

    def test_combo_lists_drop_from_their_field_with_theme_rows(self):
        """Lists opened over their combo, and a bare container sheet cleared their fill."""
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox
        from main.qt.serial_panel import SerialPanel
        from main.qt.theme import build_stylesheet, get_palette
        from src.modules.ui_palette import contrast_ratio
        previous = APP.styleSheet()
        self.addCleanup(APP.setStyleSheet, previous)

        def pump(seconds=.15):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                APP.processEvents()
                time.sleep(.003)

        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            APP.setStyleSheet(build_stylesheet(mode))
            dialog = self.settings()
            serial = self.own(SerialPanel(self.backend))
            serial.resize(900, 320)
            for host in (dialog, serial):
                host.show()
                pump()
                for combo in host.findChildren(QComboBox):
                    if not combo.isVisible() or combo.count() < 2:
                        continue
                    if host is dialog:
                        # isVisible() is true for fields clipped below a scroll
                        # viewport too. Exercise the reachable control a user
                        # can click before checking its popup's screen anchoring.
                        dialog.scroll.ensureWidgetVisible(combo, 8, 8)
                        pump()
                        self.assertFalse(combo.visibleRegion().isEmpty())
                    option = QStyleOptionComboBox()
                    combo.initStyleOption(option)
                    self.assertFalse(combo.style().styleHint(QStyle.StyleHint.SH_ComboBox_Popup, option, combo),
                                     f"{mode}: the list opens over its combo")
                    combo.setCurrentIndex(combo.count() - 1)
                    combo.showPopup()
                    pump()
                    popup = combo.view().window().geometry()
                    field = combo.mapToGlobal(QPoint(0, 0))
                    # Only the screen's own edge may push a list sideways off its field.
                    area = combo.screen().availableGeometry().adjusted(4, 4, -4, -4)
                    at_edge = popup.right() >= area.right() - 1
                    self.assertTrue(popup.x() == field.x() or at_edge,
                                    f"{mode}: list at x={popup.x()} not aligned with its field at x={field.x()}")
                    below, above = popup.top() - (field.y() + combo.height()), (field.y()) - popup.bottom()
                    self.assertTrue(-2 <= below <= 1 or -2 <= above <= 1,
                                    f"{mode}: list at {popup.getRect()} is detached from field at {field.toTuple()}")
                    image = combo.view().window().grab().toImage()
                    material = combo._mcu_glass_popup
                    backdrop = material.backdrop.grab().toImage()
                    # The top visible row, inside the list's rim; the current (last) row is below it.
                    # grab() images contain device pixels. At 200%, raw (5, 6)
                    # samples the bright rounded rim at logical (2.5, 3), outside
                    # the reading row. Probe the same logical point at every DPI.
                    ratio = image.devicePixelRatio()
                    x, y = round(5 * ratio), round(6 * ratio)
                    row = image.pixelColor(x, y)
                    fill = backdrop.pixelColor(x, y)
                    self.assertEqual(row.alpha(), 255, f"{mode}: list has no fill")
                    self.assertLessEqual(max(abs(row.red() - fill.red()), abs(row.green() - fill.green()),
                                             abs(row.blue() - fill.blue())), 3,
                                         f"{mode}: unselected rows cover the frosted scene: {row.name()} instead of {fill.name()}")
                    self.assertGreaterEqual(contrast_ratio(get_palette(mode)["TEXT"], fill.name()), 4.5,
                                            f"{mode}: reading ink against {fill.name()} at DPR {ratio}")
                    combo.hidePopup()
                    pump(.05)
                host.hide()

    def test_settings_section_titles_clear_their_rims(self):
        """Titles sat in a margin shorter than themselves, so each rim ran through its title."""
        from PySide6.QtWidgets import QGroupBox, QStyle, QStyleOptionGroupBox
        from main.qt.theme import build_stylesheet
        previous = APP.styleSheet()
        self.addCleanup(APP.setStyleSheet, previous)
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            APP.setStyleSheet(build_stylesheet(mode))
            dialog = self.settings()
            dialog._apply_dialog_theme(mode)
            dialog.show()
            APP.processEvents()
            boxes = [box for box in dialog.findChildren(QGroupBox) if box.title() and box.isVisible()]
            self.assertTrue(boxes)
            for box in boxes:
                option = QStyleOptionGroupBox()
                box.initStyleOption(option)
                style = box.style()
                label = style.subControlRect(QStyle.ComplexControl.CC_GroupBox, option,
                                             QStyle.SubControl.SC_GroupBoxLabel, box)
                frame = style.subControlRect(QStyle.ComplexControl.CC_GroupBox, option,
                                             QStyle.SubControl.SC_GroupBoxFrame, box)
                text_height = box.fontMetrics().height()
                self.assertGreaterEqual(label.height(), text_height - 1, f"{mode} {box.title()!r} title is clipped")
                self.assertLessEqual(label.top() + text_height, frame.top(),
                                     f"{mode} {box.title()!r}: title {label.getRect()} crosses its rim at {frame.top()}")
                contents = box.contentsRect()
                self.assertGreater(contents.top(), frame.top(), f"{mode} {box.title()!r} contents overlap the rim")
            dialog.hide()

    def test_settings_save_once_preserves_other_data_and_manual_solarized(self):
        dialog = self.settings()
        self.assertEqual(dialog._theme_map[dialog.theme_combo.currentText()], "solarized_dark")
        dialog.cb_theme_system.setChecked(True)
        dialog.cb_autosave.setChecked(True)
        dialog.autosave_spin.setValue(1600)
        dialog.cb_g_accel.setChecked(False)
        dialog._save_and_apply()
        self.save.assert_called_once()
        data = self.save.call_args.args[0]
        self.assertEqual(data["shared"]["theme_mode"], "solarized_dark")
        self.assertTrue(data["shared"]["theme_follow_system"])
        self.assertEqual(data["shared"]["graphics_acceleration"], "OFF")
        self.assertEqual(data["shared"]["autosave_delay_ms"], 1600)
        self.assertEqual(data["shared"]["editor_mode"], "legacy")
        self.assertEqual(data["instances"], self.config["instances"])
        self.assertNotIn("autosave_delay_ms", self.config["shared"])

    def test_settings_write_failure_does_not_apply_or_close(self):
        dialog = self.settings()
        self.save.return_value = False
        dialog.theme_combo.setCurrentText(dialog._theme_rev["light"])
        before = Theme.active_theme
        with patch.object(dialog, "accept") as accept:
            dialog._save_and_apply()
            accept.assert_not_called()
        self.assertEqual(Theme.active_theme, before)
        self.backend.set_reset_on_baud_change.assert_not_called()
        QMessageBox.critical.assert_called_once()

    def test_offline_settings_default_confirmation_and_restart_request(self):
        dialog = self.settings()
        self.assertFalse(dialog.cb_offline.isChecked())
        dialog.cb_offline.setChecked(True)
        with patch("src.modules.offline_mode.transition_blocker", return_value=""):
            dialog._save_and_apply()
        self.assertIn("gigabytes", self.question.call_args.args[2])
        self.assertTrue(self.save.call_args.args[0]["shared"]["offline_enabled"])
        self.assertTrue(self.save.call_args.args[0]["shared"]["offline_preparation_pending"])
        self.assertTrue(dialog.offline_mode_restart_requested)
        self.assertFalse(dialog.offline_mode_previous)

    def test_offline_settings_cancel_and_failed_save_preserve_mode(self):
        for save_fails in (False, True):
            self.save.reset_mock()
            self.question.return_value = (QMessageBox.StandardButton.Yes if save_fails
                                          else QMessageBox.StandardButton.No)
            self.save.return_value = not save_fails
            dialog = self.settings()
            dialog.cb_offline.setChecked(True)
            with patch("src.modules.offline_mode.transition_blocker", return_value=""), \
                    patch.object(dialog, "accept") as accept:
                dialog._save_and_apply()
            accept.assert_not_called()
            self.assertFalse(dialog.offline_mode_restart_requested)
            self.assertNotIn("offline_enabled", self.config["shared"])
            if not save_fails:
                self.save.assert_not_called()

    def test_offline_settings_revalidates_after_confirmation(self):
        dialog = self.settings()
        dialog.cb_offline.setChecked(True)
        with patch("src.modules.offline_mode.transition_blocker", side_effect=["", "Another window opened"]):
            dialog._save_and_apply()
        self.save.assert_not_called()
        self.assertFalse(dialog.offline_mode_restart_requested)
        QMessageBox.warning.assert_called_once()

    def test_disabling_offline_confirms_preserved_toolchains_and_projects(self):
        self.config["shared"]["offline_enabled"] = True
        dialog = self.settings()
        dialog.cb_offline.setChecked(False)
        with patch("src.modules.offline_mode.transition_blocker", return_value=""):
            dialog._save_and_apply()
        self.assertIn("Installed runtimes and toolchains", self.question.call_args.args[2])
        self.assertIn("AI edit history", self.question.call_args.args[2])
        self.assertFalse(self.save.call_args.args[0]["shared"]["offline_enabled"])
        self.assertTrue(dialog.offline_mode_previous)

    def test_offline_settings_control_fits_compact_dialog_in_each_palette(self):
        for theme in ("default", "light", "solarized_dark"):
            self.config["shared"]["theme_mode"] = theme
            dialog = self.settings()
            dialog._apply_dialog_theme(theme)
            dialog.resize(360, 480)
            dialog.show()
            APP.processEvents()
            dialog.scroll.ensureWidgetVisible(dialog._offline_box)
            APP.processEvents()
            self.assertGreaterEqual(dialog.cb_offline.width(), dialog.cb_offline.minimumSizeHint().width())
            self.assertLessEqual(dialog.cb_offline.width(), dialog.scroll.viewport().width())
            if RENDER_DIR:
                self.assertTrue(dialog.grab().save(str(RENDER_DIR / f"settings-offline-{theme}.png")))

    def test_settings_history_failure_keeps_saved_preferences_and_reports_warning(self):
        dialog = self.settings()
        notice = Mock()
        settings_module.signals.notification.connect(notice)
        self.addCleanup(settings_module.signals.notification.disconnect, notice)
        with patch("src.dbs.dbs_create.add_notification", return_value=None), \
             patch.object(dialog, "accept") as accept:
            dialog._save_and_apply()
        self.save.assert_called_once()
        accept.assert_called_once()
        notice.assert_called_once()
        self.assertEqual(notice.call_args.args[0]["type"], "warning")
        self.assertIn("Preferences saved", notice.call_args.args[0]["message"])

    def test_settings_cancel_does_not_persist_and_live_signals_match_saved_values(self):
        dialog = self.settings()
        dialog.theme_combo.setCurrentText(dialog._theme_rev["light"])
        dialog.cb_reset_on_baud.setChecked(True)
        dialog.reject()
        self.save.assert_not_called()
        self.backend.set_reset_on_baud_change.assert_not_called()
        callbacks = {}
        for name in ("font_size_changed", "theme_changed", "hide_warnings_changed", "autosave_settings_changed", "graphics_accel_changed"):
            signal = getattr(settings_module.signals, name)
            callback = Mock()
            callbacks[name] = callback
            signal.connect(callback)
            self.addCleanup(signal.disconnect, callback)
        dialog.font_combo.setCurrentIndex(dialog.font_combo.findData(17))
        dialog.cb_hide_warnings.setChecked(True)
        dialog.cb_autosave.setChecked(True)
        dialog.autosave_spin.setValue(1800)
        dialog.cb_g_accel.setChecked(False)
        dialog._save_and_apply()
        callbacks["font_size_changed"].assert_called_once_with(17)
        callbacks["theme_changed"].assert_called_once_with("light")
        callbacks["hide_warnings_changed"].assert_called_once_with(True)
        callbacks["autosave_settings_changed"].assert_called_once_with(True, 1800)
        callbacks["graphics_accel_changed"].assert_called_once_with(False)
        self.backend.set_reset_on_baud_change.assert_called_once_with(True)

    def test_config_atomic_failure_keeps_files_and_memory(self):
        from main.core import config
        old_cache, old_time = config._CONFIG_MEM_CACHE, config._CONFIG_MEM_MTIME
        self.addCleanup(setattr, config, "_CONFIG_MEM_CACHE", old_cache)
        self.addCleanup(setattr, config, "_CONFIG_MEM_MTIME", old_time)
        directory = ROOT / "temp/audit"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            root = Path(temporary)
            portable = root / "portable.json"
            user = root / ".mcu_gui_config.json"
            with patch.object(config, "LOCAL_GUI_CONFIG", portable), patch.object(Path, "home", return_value=root):
                before = {"shared": {"theme_mode": "solarized_dark"}}
                self.assertTrue(config._save_raw_config(before))
                with patch("os.replace", side_effect=OSError("Read-only filesystem")):
                    self.assertFalse(config._save_raw_config({"shared": {"theme_mode": "light"}}))
                self.assertEqual(config._CONFIG_MEM_CACHE, before)
                self.assertEqual(portable.read_bytes(), user.read_bytes())
                self.assertIn("solarized_dark", user.read_text(encoding="utf-8"))
                self.assertEqual(list(root.glob("*.tmp")), [])

    def test_low_end_resize_defaults_and_explicit_override(self):
        for cores in (4, 6):
            with patch("src.modules.runtime_resources.performance_profile", return_value=performance_profile(cores, 8)):
                dialog = self.settings()
                self.assertFalse(dialog.cb_g_accel.isChecked())
                with patch.object(dialog, "_save_and_apply"):
                    dialog.cb_g_accel.setChecked(True)
                    dialog._reset_defaults()
                    self.assertFalse(dialog.cb_g_accel.isChecked())
                self.config["shared"]["graphics_acceleration"] = "ON"
                self.assertTrue(self.settings().cb_g_accel.isChecked())
                del self.config["shared"]["graphics_acceleration"]

    def test_reset_guards_missing_busy_unresolved_and_non_arduino(self):
        for name, port, busy, info in (("", "", False, self.info),
                                     ("Demo", "COM99", True, self.info),
                                     ("Demo", "COM99", False, dict(self.info, pio_resolved=False)),
                                     ("Demo", "COM99", False, dict(self.info, framework="espidf"))):
            self.backend.current_board, self.backend.current_port, self.backend.is_busy = name, port, busy
            self.info = info
            dialog = self.settings()
            self.assertFalse(dialog.btn_soft_reset.isEnabled())
            self.question.reset_mock()
            dialog._run_soft_reset()
            self.backend.soft_reset.assert_not_called()
            self.question.assert_not_called()

    def test_reset_rechecks_disconnection_and_uses_strategy_not_label(self):
        self.backend.current_board, self.backend.current_port = "Demo", "COM99"
        dialog = self.settings()
        self.assertTrue(dialog.btn_soft_reset.isEnabled())
        self.backend.current_port = ""
        dialog._run_soft_reset()
        self.backend.soft_reset.assert_not_called()
        self.backend.current_port = "COM99"
        self.info = dict(self.info, platform="espressif8266", board="nodemcuv2")
        dialog.btn_hard_reset.setText("Renamed action")
        dialog._run_hard_reset()
        self.backend.hard_reset.assert_called_once_with(erase_flash=True)

    def test_reset_rechecks_target_after_confirmation(self):
        for hard in (False, True):
            self.backend.current_board, self.backend.current_port = "Demo", "COM99"
            dialog = self.settings()
            def change_target(*args):
                self.backend.current_port = "COM100"
                return QMessageBox.StandardButton.Yes
            self.question.side_effect = change_target
            (dialog._run_hard_reset if hard else dialog._run_soft_reset)()
            self.backend.soft_reset.assert_not_called()
            self.backend.hard_reset.assert_not_called()

    def test_editor_save_waits_for_ack_and_rejects_failed_disk_write(self):
        from main.qt.editor_panel import EditorBridgeAPI, MonacoEditorPanel
        for fail in (False, True):
            panel = self.own(QWidget())
            panel._bridge = EditorBridgeAPI(self.backend, panel)
            panel._backend = self.backend
            panel._view = Mock()
            callback = Mock()
            MonacoEditorPanel.trigger_save_all(panel, callback)
            callback.assert_not_called()
            token = panel._save_pending
            if fail:
                panel._bridge._save_failure_count += 1
            panel._bridge.finish_save_request(token, True)
            self.assertEqual(callback.call_count, 0 if fail else 1)
            self.assertIsNone(panel._save_pending)
            panel._bridge.finish_save_request(token, True)
            self.assertEqual(callback.call_count, 0 if fail else 1)

    def test_reload_calls_editor_reload_not_tab_activation(self):
        from main.qt.editor_panel import MonacoEditorPanel
        panel = self.own(QWidget())
        panel._view = Mock()
        MonacoEditorPanel.trigger_reload(panel)
        js = panel._view.page().runJavaScript.call_args.args[0]
        self.assertIn("await window.reloadActiveFile()", js)
        self.assertNotIn("activateProjectFile", js)


    def test_actions_save_before_build_and_busy_during_transition(self):
        self.backend.current_board, self.backend.current_port = "Demo", "COM99"
        window = self.own(QMainWindow())
        window._editor_panel = SimpleNamespace(trigger_save_all=Mock())
        toolbar = PrimaryToolbar(self.backend, window)
        window.addToolBar(toolbar)
        toolbar._do_compile()
        self.backend.compile_sketch.assert_not_called()
        window._editor_panel.trigger_save_all.call_args.kwargs["callback"]()
        self.backend.compile_sketch.assert_called_once()
        toolbar._do_upload()
        window._editor_panel.trigger_save_all.call_args.kwargs["callback"]()
        self.backend.upload_sketch.assert_called_once()
        self.backend.active_operation = "compile"
        window._editor_panel.trigger_save_all.reset_mock()
        toolbar._do_compile()
        toolbar._do_clean()
        window._editor_panel.trigger_save_all.assert_not_called()
        self.backend.clean_cache.assert_not_called()

    def test_compile_upload_follow_selections_including_unresolved_and_programmer_targets(self):
        window = self.own(QMainWindow())
        toolbar = PrimaryToolbar(self.backend, window)
        window.addToolBar(toolbar)
        # Definition validation belongs to the build pipeline, not selection gating.
        self.backend._resolve_board_info = lambda: ({"board": "fixture", "platform": "ststm32", "pio_resolved": True,
            "upload_protocol": "stlink"} if self.backend.current_board == "ST-Link board" else {})
        for board, port, compile_enabled, upload_enabled in (
                ("", "", False, False), ("", "COM99", False, False),
                ("Unresolved board", "", True, False),
                ("ST-Link board", "COM99", True, True),
                ("ST-Link board", "", True, True)):
            self.backend.current_board, self.backend.current_port = board, port
            toolbar._update_action_button_states()
            self.assertEqual(toolbar.btn_compile.isEnabled(), compile_enabled)
            self.assertEqual(toolbar.btn_upload.isEnabled(), upload_enabled)
        self.backend.current_board = ""
        toolbar._do_compile()
        toolbar._do_upload()
        self.backend.compile_sketch.assert_not_called()
        self.backend.upload_sketch.assert_not_called()

    def test_upload_speed_displays_board_defaults_without_changing_esp_preference(self):
        from main.core import board_catalog
        rows = {"Nano": {"platform": "atmelavr", "board": "nanoatmega328", "framework": "arduino", "upload_speed": 57600},
                "Pico": {"platform": "raspberrypi", "board": "pico", "framework": "arduino"},
                "ESP": self.info}
        window = self.own(QMainWindow())
        controls = ControlsBar(self.backend, window)
        self.backend.upload_speed = "460800"
        self.backend.set_upload_speed = Mock()
        self.backend.set_baud_rate = Mock()
        with patch.object(board_catalog, "SUPPORTED_BOARDS", rows):
            for name, text, enabled in (("Nano", "57600", False), ("Pico", "Auto", False), ("ESP", "460800", True)):
                controls._update_hardware_defaults_for_board(name, update_monitor=False)
                self.assertEqual(controls.upload_speed_combo.currentText(), text)
                self.assertEqual(controls.upload_speed_combo.isEnabled(), enabled)
        self.backend.set_upload_speed.assert_not_called()
        self.backend.set_baud_rate.assert_not_called()

    def test_action_gating_updates_from_signals_and_after_busy_phase(self):
        from main.qt.signals import MCUSignals
        bus = MCUSignals()
        window = self.own(QMainWindow())
        toolbar = PrimaryToolbar(self.backend, window)
        window.addToolBar(toolbar)
        toolbar.connect_signals(bus)
        self.backend.current_board = "Demo"
        bus.board_selected.emit({"board_name": "Demo"})
        self.assertTrue(toolbar.btn_compile.isEnabled())
        self.assertFalse(toolbar.btn_upload.isEnabled())
        self.backend.current_port = "COM99"
        bus.port_selected.emit({"port": "COM99"})
        self.assertTrue(toolbar.btn_upload.isEnabled())
        self.backend.is_busy = True
        bus.operation_phase.emit({"phase": "compile", "is_busy": True})
        bus.board_selected.emit({"board_name": "Demo"})
        self.assertFalse(toolbar.btn_compile.isEnabled())
        self.assertFalse(toolbar.btn_upload.isEnabled())
        self.backend.current_port = ""
        self.backend.is_busy = False
        bus.operation_phase.emit({"phase": "idle", "is_busy": False})
        self.assertTrue(toolbar.btn_compile.isEnabled())
        self.assertFalse(toolbar.btn_upload.isEnabled())
        self.backend.current_board = ""
        bus.board_selected.emit({"board_name": ""})
        self.assertFalse(toolbar.btn_compile.isEnabled())

    def test_idle_cannot_enable_actions_before_the_operation_thread_finishes(self):
        from main.qt.signals import MCUSignals
        bus = MCUSignals()
        window = self.own(QMainWindow())
        window._editor_panel = SimpleNamespace(trigger_save_all=Mock())
        toolbar = PrimaryToolbar(self.backend, window)
        window.addToolBar(toolbar)
        toolbar.connect_signals(bus)
        self.backend.current_board, self.backend.current_port = "Demo", "COM99"
        alive = [True]
        self.backend._operation_worker = SimpleNamespace(is_alive=lambda: alive[0])
        self.backend.is_busy, self.backend.active_operation = True, "compile"
        self.backend._current_op_phase = "compiling"
        bus.operation_phase.emit({"phase": "compile", "is_busy": True, "op": "compile"})
        # A delayed idle payload must not override the actual reservation.
        bus.operation_phase.emit({"phase": "idle", "is_busy": False})
        self.assertFalse(toolbar.btn_compile.isEnabled())
        self.assertFalse(toolbar.btn_upload.isEnabled())
        self.assertFalse(toolbar._action_state_timer.isActive())
        self.backend.is_busy = False
        self.backend.active_operation = self.backend._current_op_phase = None
        bus.operation_phase.emit({"phase": "idle", "is_busy": False})
        self.assertFalse(toolbar.btn_compile.isEnabled())
        self.assertFalse(toolbar.btn_upload.isEnabled())
        self.assertIn("Finishing the previous action", toolbar.btn_compile.toolTip())
        self.assertTrue(toolbar._action_state_timer.isActive())
        toolbar._do_compile()
        window._editor_panel.trigger_save_all.assert_not_called()
        alive[0] = False
        deadline = time.monotonic() + 1
        while not toolbar.btn_compile.isEnabled() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.005)
        self.assertTrue(toolbar.btn_compile.isEnabled())
        self.assertTrue(toolbar.btn_upload.isEnabled())
        self.assertFalse(toolbar._action_state_timer.isActive())
        toolbar._do_compile()
        window._editor_panel.trigger_save_all.call_args.kwargs["callback"]()
        self.backend.compile_sketch.assert_called_once()

    def test_failed_or_timed_out_save_allows_a_fresh_build_request(self):
        from main.qt.editor_panel import EditorBridgeAPI, MonacoEditorPanel
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.backend.compile_sketch.reset_mock()
                window = self.own(QMainWindow())
                panel = QWidget(window)
                panel._bridge = EditorBridgeAPI(self.backend, panel)
                panel._backend, panel._view = self.backend, Mock()
                panel.trigger_save_all = lambda **kw: MonacoEditorPanel.trigger_save_all(panel, **kw)
                window._editor_panel = panel
                toolbar = PrimaryToolbar(self.backend, window)
                window.addToolBar(toolbar)
                self.backend.current_board, self.backend.current_port = "Demo", "COM99"
                with patch("main.qt.editor_panel.QTimer.singleShot") as timer:
                    toolbar._do_compile()
                    token = panel._save_pending
                    if timeout:
                        timer.call_args.args[2]()
                    else:
                        panel._bridge.finish_save_request(token, False)
                    self.assertIsNone(panel._save_pending)
                    self.backend.compile_sketch.assert_not_called()
                    toolbar._update_action_button_states()
                    self.assertTrue(toolbar.btn_compile.isEnabled())
                    self.assertTrue(toolbar.btn_upload.isEnabled())
                    # Late acknowledgement cannot launch the failed request.
                    panel._bridge.finish_save_request(token, True)
                    self.backend.compile_sketch.assert_not_called()
                    toolbar._do_compile()
                    panel._bridge.finish_save_request(panel._save_pending, True)
                    self.backend.compile_sketch.assert_called_once()

    def test_build_rechecks_selection_and_busy_state_after_editor_save(self):
        window = self.own(QMainWindow())
        window._editor_panel = SimpleNamespace(trigger_save_all=Mock())
        toolbar = PrimaryToolbar(self.backend, window)
        window.addToolBar(toolbar)
        self.backend.current_board, self.backend.current_port = "Demo", "COM99"
        toolbar._do_upload()
        self.backend.current_port = ""
        window._editor_panel.trigger_save_all.call_args.kwargs["callback"]()
        self.backend.upload_sketch.assert_not_called()
        toolbar._do_compile()
        self.backend.is_busy = True
        window._editor_panel.trigger_save_all.call_args.kwargs["callback"]()
        self.backend.compile_sketch.assert_not_called()

    def test_controls_selection_does_not_hash_sources_and_port_clear_updates_combo(self):
        from main.qt.signals import MCUSignals
        bus = MCUSignals()
        window = self.own(QMainWindow())
        toolbar = PrimaryToolbar(self.backend, window)
        window._primary_toolbar = toolbar
        window.addToolBar(toolbar)
        controls = ControlsBar(self.backend, window)
        toolbar.connect_signals(bus)
        controls.connect_signals(bus)
        self.backend.check_can_skip_compile = Mock(side_effect=AssertionError("GUI source hashing"))
        self.backend.update_skip_compile_availability = Mock()
        self.backend.current_board = "Demo"
        bus.board_selected.emit({"board_name": "Demo"})
        self.assertTrue(toolbar.btn_compile.isEnabled())
        bus.project_updated.emit({"path": "isolated fixture"})
        self.backend.check_can_skip_compile.assert_not_called()
        self.backend.update_skip_compile_availability.assert_called_once_with()
        self.assertFalse(controls.cb_skip_compile.isEnabled())
        controls.port_combo.blockSignals(True)
        controls.port_combo.addItem("Fixture port", "COM99")
        controls.port_combo.blockSignals(False)
        self.backend.current_port = "COM99"
        bus.port_selected.emit({"port": "COM99"})
        self.assertEqual(controls.port_combo.currentData(), "COM99")
        self.assertTrue(toolbar.btn_upload.isEnabled())
        self.backend.current_port = ""
        bus.port_selected.emit({"port": ""})
        self.assertEqual(controls.port_combo.currentIndex(), -1)
        self.assertTrue(toolbar.btn_compile.isEnabled())
        self.assertFalse(toolbar.btn_upload.isEnabled())

    def test_compact_actions_respect_live_state_and_release_popup(self):
        window = self.own(QMainWindow())
        toolbar = PrimaryToolbar(self.backend, window)
        window.addToolBar(toolbar)
        invoked = Mock()
        toolbar.btn_save.clicked.connect(invoked)
        for enabled in (False, True):
            toolbar.btn_save.setEnabled(True)
            toolbar._toggle_actions_menu()
            popup = toolbar._actions_popup
            source = popup.findChildren(QPushButton)[2]
            toolbar.btn_save.setEnabled(enabled)
            source.click()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()
            self.assertIsNone(toolbar._actions_popup)
        invoked.assert_called_once()
        for _ in range(10):
            toolbar._toggle_actions_menu()
            toolbar._actions_popup.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(toolbar.findChildren(CompactDropdownPopup), [])

    def test_every_compact_action_dispatches_to_its_workflow(self):
        window = self.own(QMainWindow())
        for name in ("_shortcut_save", "_shortcut_save_all", "_shortcut_reload_file", "_open_modify_files_dialog"):
            setattr(window, name, Mock())
        toolbar = PrimaryToolbar(self.backend, window)
        window.addToolBar(toolbar)
        buttons = [toolbar.btn_stop, toolbar.btn_clean, toolbar.btn_save, toolbar.btn_save_all, toolbar.btn_reload]
        callbacks = [self.backend.stop_operation, self.backend.clean_cache, window._shortcut_save,
                     window._shortcut_save_all, window._shortcut_reload_file]
        self.assertFalse(hasattr(toolbar, "btn_modify"), "Modify left the Actions row for the source tabs")
        for index, (button, callback) in enumerate(zip(buttons, callbacks)):
            self.backend.is_busy = index == 0
            self.backend.active_operation = "compile" if index == 0 else None
            self.backend._current_op_phase = "compiling" if index == 0 else None
            button.setEnabled(True)
            toolbar._toggle_actions_menu()
            toolbar._actions_popup.findChildren(QPushButton)[index].click()
            callback.assert_not_called()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()
            callback.assert_called_once()

    def test_compact_options_can_be_reopened_after_dismissal(self):
        window = self.own(QMainWindow())
        controls = ControlsBar(self.backend, window)
        window.setCentralWidget(controls)
        for _ in range(5):
            controls._toggle_options_menu()
            self.assertIsNotNone(controls._opt_popup)
            controls._opt_popup.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertIsNone(controls._opt_popup)
        self.assertEqual(controls.findChildren(CompactDropdownPopup), [])

    def test_stop_cannot_interrupt_flash_or_erase(self):
        window = self.own(QMainWindow())
        toolbar = PrimaryToolbar(self.backend, window)
        for op, phase in (("flash", "uploading"), ("reset", "compiling"), ("compile", "writing"), (None, "erasing")):
            self.backend.active_operation, self.backend._current_op_phase = op, phase
            toolbar._do_stop()
        self.backend.stop_operation.assert_not_called()
        self.backend.active_operation, self.backend._current_op_phase = "compile", "compiling"
        toolbar._do_stop()
        self.backend.stop_operation.assert_called_once()

    def test_action_button_text_contrast_across_palettes(self):
        from src.modules.ui_palette import readable_foreground
        def luminance(color):
            values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            return sum((v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4) * w
                       for v, w in zip(values, (.2126, .7152, .0722)))
        for palette in Theme.PALETTES.values():
            for key, background in palette.items():
                if key.startswith("BTN_"):
                    fg = readable_foreground(background)
                    high, low = sorted((luminance(background), luminance(fg)), reverse=True)
                    self.assertGreaterEqual((high + .05) / (low + .05), 4.5)

    def test_bootstrap_palette_progress_and_log_trimming(self):
        for mode in Theme.PALETTES:
            dialog, _, _ = bootstrap_fixture(mode)
            self.own(dialog)
            dialog.log_edit.setMaximumBlockCount(8)
            dialog._on_log("Runtime verification", "section")
            dialog._on_update_block("pip", "PACKAGE ▰▱ 50%\n")
            for number in range(30):
                dialog._on_log(f"Line {number}", "normal")
            dialog._on_update_block("pip", "PACKAGE ▰▰ 100%\n")
            text = dialog.log_edit.toPlainText()
            self.assertIn("Line 29", text)
            self.assertIn("100%", text)
            self.assertNotIn("50%", text)
            self.assertLessEqual(dialog.log_edit.blockCount(), 8)
            self.assertEqual(text.count("PACKAGE"), 1)
            cursor = QTextCursor(dialog.log_edit.document())
            cursor.setPosition(dialog._live_block_start + 1)
            self.assertEqual(cursor.charFormat().foreground().color().name(), dialog._theme_pal["T_CYAN"])
            dialog._on_clear_block()
            self.assertNotIn("PACKAGE", dialog.log_edit.toPlainText())
            self.assertIn("Line 29", dialog.log_edit.toPlainText())
            dialog._on_progress(200)
            self.assertEqual(dialog.prog_bar.value(), 100)
            dialog._on_progress(-5)
            self.assertEqual(dialog.prog_bar.value(), 0)
            dialog._on_stop_spinner("Ready", True)
            self.assertEqual(dialog.spin_lbl._color, dialog._theme_pal["T_GREEN"])
            self.assertEqual(dialog.spin_lbl.accessibleName(), "Check")

    def test_bootstrap_display_character_budget(self):
        dialog, _, _ = bootstrap_fixture()
        self.own(dialog)
        dialog.log_edit.setMaximumBlockCount(4000)
        for _ in range(60):
            dialog._on_log("A" * 10000, "normal")
        self.assertLessEqual(dialog.log_edit.document().characterCount(), 256001)
        self.assertIn("display shortened", dialog.log_edit.toPlainText())

    def test_bootstrap_scroll_input_live_replacement_and_retained_history(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        dialog, _, _ = bootstrap_fixture()
        self.own(dialog)
        dialog._set_details_expanded(True)
        dialog.resize(700, 430)
        dialog.show()
        view = dialog.log_edit
        view.setMaximumBlockCount(110)
        for number in range(100):
            dialog._on_log(f"Row {number:03d}", "normal")
        APP.processEvents()
        self.assertFalse(view.textCursor().hasSelection())
        bar = view.verticalScrollBar()
        bar.setValue(bar.maximum())
        point = QPointF(30, 30)
        APP.sendEvent(view.viewport(), QWheelEvent(point, point, QPoint(), QPoint(0, 120),
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                      Qt.ScrollPhase.NoScrollPhase, False))
        visible = view.firstVisibleBlock().text()
        dialog._on_update_block("pip", "PACKAGE 10%\n")
        dialog._on_update_block("pip", "PACKAGE 20%\nPACKAGE TWO 50%\n")
        dialog._on_commit_block()
        self.assertEqual(view.firstVisibleBlock().text(), visible)
        self.assertLess(bar.value(), bar.maximum())
        bar.setValue(bar.maximum())
        bar.setSliderDown(True)
        visible = view.firstVisibleBlock().text()
        dialog._on_log("Output while scrollbar is held", "normal")
        self.assertEqual(view.firstVisibleBlock().text(), visible)
        bar.setSliderDown(False)
        bar.setValue(bar.maximum())
        dialog._on_log("Following resumes", "normal")
        self.assertEqual(bar.value(), bar.maximum())
        dialog.auto_scroll_cb.setChecked(False)
        view.setTextCursor(view.document().find("Row 050"))
        bar.setValue(45)
        visible = view.firstVisibleBlock().text()
        for number in range(100, 130):
            dialog._on_log(f"Row {number:03d}", "normal")
        self.assertEqual(view.firstVisibleBlock().text(), visible)
        self.assertEqual(view.textCursor().selectedText(), "Row 050")
        dialog._on_update_block("pio", "Preparing package 90%\n")
        dialog._on_clear_block()
        self.assertEqual(view.firstVisibleBlock().text(), visible)
        for number in range(130, 250):
            dialog._on_log(f"Row {number:03d}", "normal")
        self.assertEqual(bar.value(), 0)
        self.assertLessEqual(view.blockCount(), 110)

    def test_bootstrap_scrollbar_release_resumes_and_checkbox_off_holds(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QStyle, QStyleOptionSlider

        dialog, _, _ = bootstrap_fixture()
        self.own(dialog)
        dialog._set_details_expanded(True)
        dialog.resize(700, 430)
        dialog.show()
        view = dialog.log_edit
        for number in range(180):
            dialog._on_log(f"Drag row {number:03d}", "normal")
        APP.processEvents()
        # Native fractional-scale text layout can publish a second scroll
        # range. Let queued layout/follow timers finish before starting a drag.
        QTest.qWait(50)
        bar = view.verticalScrollBar()
        observations = []

        def record(name):
            observations.append({"stage": name, "scroll_value": bar.value(),
                                 "scroll_maximum": bar.maximum(),
                                 "first_visible": view.firstVisibleBlock().text(),
                                 "selection": view.textCursor().selectedText(),
                                 "enabled": dialog._follow.enabled,
                                 "held": dialog._follow.scrollbar_held})
            if RENDER_DIR:
                dialog.grab().save(str(RENDER_DIR / f"bootstrap-scroll-{name}.png"))

        def hold_and_drag_up():
            option = QStyleOptionSlider()
            bar.initStyleOption(option)
            thumb = bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, option,
                                             QStyle.SubControl.SC_ScrollBarSlider, bar)
            point = thumb.center()
            QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=point)
            self.assertTrue(bar.isSliderDown())
            self.assertTrue(dialog._follow.scrollbar_held)
            point = QPoint(point.x(), max(20, point.y() - bar.height() // 2))
            APP.sendEvent(bar, QMouseEvent(QEvent.Type.MouseMove, QPointF(point),
                          QPointF(bar.mapToGlobal(point)), Qt.MouseButton.NoButton,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            self.assertLess(bar.value(), bar.maximum())
            return point

        self.assertEqual(bar.value(), bar.maximum())
        view.setTextCursor(view.document().find("Drag row 090"))
        bar.setValue(bar.maximum())
        point = hold_and_drag_up()
        visible = view.firstVisibleBlock().text()
        record("held-before-output")
        dialog._on_log("Output while dragging", "normal")
        dialog._on_update_block("pip", "Download 10%\n")
        dialog._on_update_block("pip", "Download 20%\nSecond progress row\n")
        dialog._on_commit_block()
        self.assertEqual(view.firstVisibleBlock().text(), visible)
        self.assertEqual(view.textCursor().selectedText(), "Drag row 090")
        record("held-after-output")
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=point)
        APP.processEvents()
        QTest.qWait(30)
        self.assertFalse(dialog._follow.scrollbar_held)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertEqual(view.textCursor().selectedText(), "Drag row 090")
        record("released-following")
        dialog._on_log("Output after release", "normal")
        self.assertEqual(bar.value(), bar.maximum())

        # A pending release must honor OFF even when output arrives first.
        point = hold_and_drag_up()
        dialog.auto_scroll_cb.setChecked(False)
        visible = view.firstVisibleBlock().text()
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=point)
        dialog._on_log("Output with following disabled", "normal")
        APP.processEvents()
        self.assertEqual(view.firstVisibleBlock().text(), visible)
        self.assertLess(bar.value(), bar.maximum())
        record("off-after-release")

        # Checking ON resumes an earlier paused view without another drag.
        dialog.auto_scroll_cb.setChecked(True)
        self.assertEqual(bar.value(), bar.maximum())
        dialog._on_log("Output after re-enabling", "normal")
        self.assertEqual(bar.value(), bar.maximum())

        # Re-enabling while a thumb is held must wait for its release.
        point = hold_and_drag_up()
        visible = view.firstVisibleBlock().text()
        dialog.auto_scroll_cb.setChecked(False)
        dialog.auto_scroll_cb.setChecked(True)
        dialog._on_log("Re-enabled while still held", "normal")
        self.assertEqual(view.firstVisibleBlock().text(), visible)
        record("reenabled-while-held")
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=point)
        APP.processEvents()
        self.assertEqual(bar.value(), bar.maximum())
        record("reenabled-released")
        if RENDER_DIR:
            (RENDER_DIR / "bootstrap-scroll-observations.json").write_text(
                json.dumps({"qt_platform": APP.platformName(), "observations": observations}, indent=2),
                encoding="utf-8")

    def test_workspace_log_release_keeps_reading_position(self):
        from main.qt.log_follow import LogFollow
        view = self.own(QPlainTextEdit())
        view.resize(400, 220)
        view.show()
        follow = LogFollow(view)
        with follow.update():
            view.setPlainText("\n".join(f"Workspace row {number}" for number in range(120)))
        APP.processEvents()
        bar = view.verticalScrollBar()
        bar.setSliderDown(True)
        bar.setValue(30)
        visible = view.firstVisibleBlock().text()
        bar.setSliderDown(False)
        APP.processEvents()
        with follow.update():
            view.appendPlainText("Workspace reader stays here")
        self.assertEqual(view.firstVisibleBlock().text(), visible)
        self.assertLess(bar.value(), bar.maximum())

    def test_bootstrap_whole_failed_step_after_trim_and_progress_updates(self):
        for mode in Theme.PALETTES:
            dialog, _, _ = bootstrap_fixture(mode)
            self.own(dialog)
            view = dialog.log_edit
            dialog._on_log("Earlier successful step", "section")
            dialog._on_log("Completed previous stage", "ok")
            dialog._on_log("Failed step begins", "section")
            dialog._on_log("First diagnostic", "normal")
            dialog._on_log("Nested package phase", "subsection")
            dialog._on_log("Last diagnostic", "warn")
            dialog._on_log("Download failed", "fail")
            red = dialog._theme_pal["T_RED"]
            for text in ("Failed step begins", "First diagnostic", "Nested package phase", "Last diagnostic"):
                self.assertEqual(view.document().find(text).charFormat().foreground().color().name(), red)
            self.assertEqual(view.document().find("Completed previous stage").charFormat().foreground().color().name(),
                             dialog._theme_pal["T_GREEN"])
            view.setMaximumBlockCount(8)
            dialog._display_char_limit = 150
            dialog._on_log("Long failed step", "section")
            for number in range(12):
                dialog._on_log(f"Remaining failure output {number}", "normal")
            dialog._on_log("Failure after earlier output was trimmed", "fail")
            dialog._on_update_block("pio", "FAILED PACKAGE ▰▱ 50%\n")
            dialog._on_update_block("pio", "FAILED PACKAGE ▰▰ 100%\n")
            block = view.document().firstBlock()
            while block.isValid():
                fragments = block.begin()
                while not fragments.atEnd():
                    fragment = fragments.fragment()
                    if fragment.isValid() and fragment.text().strip():
                        self.assertEqual(fragment.charFormat().foreground().color().name(), red)
                    fragments += 1
                block = block.next()
            dialog._on_log("Next step", "section")
            dialog._on_log("Normal again", "normal")
            self.assertEqual(view.document().find("Normal again").charFormat().foreground().color().name(),
                             dialog._theme_pal["T_TEXT"])

    def test_bootstrap_console_failure_colors_future_output_without_replay(self):
        import io
        source = ROOT / "src/modules/bootstrap.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BootstrapGUI")
        append = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_append")
        namespace = {"_record_bootstrap_log": Mock(), "sys": sys, "os": os,
                     "RED": "\033[91m", "RESET": "\033[0m"}
        exec(compile(ast.Module(body=[append], type_ignores=[]), str(source), "exec"), namespace)
        gui = SimpleNamespace(_signals=None, _log_history=[], _log_history_chars=0, _console_step_failed=False)
        output = io.StringIO()
        output.isatty = lambda: True
        with patch.object(sys, "stdout", output), patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": ""}):
            namespace["_append"](gui, "Earlier output", "normal")
            namespace["_append"](gui, "Actual failure", "fail")
            namespace["_append"](gui, "Later failure output", "dim")
            namespace["_append"](gui, "Next step", "section")
        self.assertEqual(output.getvalue(), "Earlier output\n\033[91mActual failure\033[0m\n"
                         "\033[91mLater failure output\033[0m\nNext step\n")

    def test_bootstrap_hidden_window_stays_hidden_and_skip_persists(self):
        dialog, gui, namespace = bootstrap_fixture()
        self.own(dialog)
        dialog.hide()
        dialog._unset_topmost()
        self.assertFalse(dialog.isVisible())
        dialog.skip_cb.setChecked(False)
        self.assertFalse(gui._skip_updates)
        namespace["save_bootstrap_config"].assert_called_once_with({"other": "preserved", "skip_updates": False})
        self.assertFalse(dialog._spinner_timer.isActive())
        dialog._on_close()
        self.assertFalse(dialog._clock_timer.isActive())

    def test_bootstrap_skip_write_failure_keeps_explicit_choice_for_current_run(self):
        dialog, gui, namespace = bootstrap_fixture(cores=6)
        self.own(dialog)
        namespace["save_bootstrap_config"].return_value = False
        dialog.skip_cb.setChecked(False)
        self.assertFalse(dialog.skip_cb.isChecked())
        self.assertFalse(gui._skip_updates)
        self.assertIn("was not saved", dialog.log_edit.toPlainText())

    def test_bootstrap_config_failure_preserves_existing_json(self):
        source = ROOT / "src/modules/bootstrap.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "save_bootstrap_config"]
        directory = ROOT / "temp/audit"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            config_file = Path(temporary) / "setup.json"
            namespace = dict(globals(), BOOTSTRAP_CONFIG_FILE=config_file)
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
            save = namespace["save_bootstrap_config"]
            self.assertTrue(save({"skip_updates": True}))
            before = config_file.read_bytes()
            with patch("os.replace", side_effect=OSError("Read-only filesystem")):
                self.assertFalse(save({"skip_updates": False}))
            self.assertEqual(config_file.read_bytes(), before)
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])

    def test_render_setup_and_settings(self):
        if not RENDER_DIR:
            return
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
        for mode in Theme.PALETTES:
            dialog, _, _ = bootstrap_fixture(mode)
            self.own(dialog)
            dialog.resize(720, 520)
            dialog._on_log("[1/9] Runtime verification", "section")
            dialog._on_log("Python runtime is healthy", "ok")
            dialog._on_log("Preparing board and upload tools…", "normal")
            dialog._on_update_block("pip", "PACKAGE             STATUS\nPlatformIO          ▰▰▰▰▱▱ 65%\n")
            dialog._on_progress(65)
            dialog.show()
            APP.processEvents()
            self.assertTrue(dialog.grab().save(str(RENDER_DIR / f"setup-{mode}.png")))
            dialog.hide()
            Theme.apply_theme(mode)
            settings = self.settings()
            settings.theme_combo.setCurrentText(settings._theme_rev[mode])
            settings._apply_dialog_theme(mode)
            settings.show()
            APP.processEvents()
            self.assertTrue(settings.grab().save(str(RENDER_DIR / f"settings-{mode}.png")))
            settings.hide()


def main():
    global RENDER_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ControlChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
