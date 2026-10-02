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
                              QMainWindow, QMessageBox, QPushButton)
from main.core.theme import Theme
from main.qt import settings_dialog as settings_module
from main.qt.toolbar import PrimaryToolbar, ControlsBar, CompactDropdownPopup
from src.modules.runtime_resources import performance_profile
from verify_runtime import PreviewBackend

APP = QApplication.instance() or QApplication([])
RENDER_DIR = None


def bootstrap_fixture(mode="default", cores=4):
    """Execute only UI definitions; every persistence callback is a mock."""
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    nodes = [n for n in tree.body if
             isinstance(n, ast.ClassDef) and n.name == "_BootstrapSignals" or
             isinstance(n, ast.If) and any(isinstance(c, ast.ClassDef) and c.name == "_BootstrapDialog" for c in n.body)]
    palette = {"T_" + key: value for key, value in Theme.PALETTES[mode].items()}
    namespace = dict(globals(), HAS_PYSIDE6_BOOTSTRAP=True, SCRIPT_DIR=ROOT,
                     __file__=str(source), _T_PALETTE=palette,
                     _resolve_bootstrap_theme=lambda: (palette, mode),
                     load_bootstrap_config=Mock(return_value={"other": "preserved"}),
                     save_bootstrap_config=Mock(return_value=True),
                     _record_bootstrap_exception=Mock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    gui = SimpleNamespace(_status_text="Verifying the private runtime…", _skip_updates=True,
                          _signals=namespace["_BootstrapSignals"](), _start_time=time.time(),
                          _closed=False, _spinning=True)
    with patch("src.modules.runtime_resources.performance_profile", return_value=performance_profile(cores, 8)):
        dialog = namespace["_BootstrapDialog"](gui)
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
        self.backend.current_board = "Demo"
        bus.board_selected.emit({"board_name": "Demo"})
        self.assertTrue(toolbar.btn_compile.isEnabled())
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
        buttons = [toolbar.btn_stop, toolbar.btn_clean, toolbar.btn_save, toolbar.btn_save_all, toolbar.btn_reload, toolbar.btn_modify]
        callbacks = [self.backend.stop_operation, self.backend.clean_cache, window._shortcut_save,
                     window._shortcut_save_all, window._shortcut_reload_file, window._open_modify_files_dialog]
        for index, (button, callback) in enumerate(zip(buttons, callbacks)):
            self.backend.is_busy = index == 0
            self.backend.active_operation = "compile" if index == 0 else None
            self.backend._current_op_phase = "compiling" if index == 0 else None
            button.setEnabled(True)
            toolbar._toggle_actions_menu()
            toolbar._actions_popup.findChildren(QPushButton)[index].click()
            callback.assert_called_once()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

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
            self.assertEqual(cursor.charFormat().foreground().color().name(), Theme.PALETTES[mode]["CYAN"])
            dialog._on_clear_block()
            self.assertNotIn("PACKAGE", dialog.log_edit.toPlainText())
            self.assertIn("Line 29", dialog.log_edit.toPlainText())
            dialog._on_progress(200)
            self.assertEqual(dialog.prog_bar.value(), 100)
            dialog._on_progress(-5)
            self.assertEqual(dialog.prog_bar.value(), 0)
            dialog._on_stop_spinner("Ready", True)
            self.assertIn(Theme.PALETTES[mode]["GREEN"], dialog.spin_lbl.styleSheet())

    def test_bootstrap_display_character_budget(self):
        dialog, _, _ = bootstrap_fixture()
        self.own(dialog)
        dialog.log_edit.setMaximumBlockCount(4000)
        for _ in range(60):
            dialog._on_log("A" * 10000, "normal")
        self.assertLessEqual(dialog.log_edit.document().characterCount(), 256001)
        self.assertIn("display shortened", dialog.log_edit.toPlainText())

    def test_bootstrap_hidden_window_stays_hidden_and_skip_persists(self):
        dialog, gui, namespace = bootstrap_fixture()
        self.own(dialog)
        dialog.hide()
        dialog._unset_topmost()
        self.assertFalse(dialog.isVisible())
        dialog.skip_cb.setChecked(False)
        self.assertFalse(gui._skip_updates)
        namespace["save_bootstrap_config"].assert_called_once_with({"other": "preserved", "skip_updates": False})
        self.assertEqual(dialog._spinner_timer.interval(), 180)
        dialog._on_close()
        self.assertFalse(dialog._clock_timer.isActive())

    def test_bootstrap_skip_write_failure_reverts_control(self):
        dialog, gui, namespace = bootstrap_fixture(cores=6)
        self.own(dialog)
        namespace["save_bootstrap_config"].return_value = False
        dialog.skip_cb.setChecked(False)
        self.assertTrue(dialog.skip_cb.isChecked())
        self.assertTrue(gui._skip_updates)
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


class DownloaderChecks(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from src.modules import arduino_lib_req as downloader
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
        app._compute_installed_items_async = Mock()
        app._compute_installed_items = Mock()
        app._update_version_status = Mock()
        app._build_ui()
        self.app = app

    def pump(self):
        for _ in range(8):
            self.root.update()
            time.sleep(.025)

    def test_tabs_sources_search_and_version_selection(self):
        app = self.app
        self.root.geometry("900x620+20+20")
        self.root.deiconify()
        self.pump()
        self.assertEqual(len(app.notebook.tabs()), 3)
        app.sources_btn.invoke()
        self.pump()
        self.assertTrue(app._sources_card.winfo_ismapped())
        app.sources_btn.invoke()
        self.assertFalse(app._sources_card.winfo_ismapped())
        version = {"version": "1.0", "size": 1280, "url": "https://example.invalid/demo.zip"}
        lib = {"name": "Demo sensors", "author": "Fixture", "maintainer": "", "category": "Sensors",
               "architectures": ["*"], "sentence": "A library for device readings.",
               "paragraph": "Pick a version, then download its archive.", "versions": [version]}
        app.lib_tab.populate({lib["name"]: lib})
        app.lib_tab.listbox.selection_set(0)
        app.lib_tab._on_select()
        self.assertEqual(app.lib_tab.version_var.get(), "1.0")
        self.assertEqual(len(app.lib_tab.detail_canvas.find_all()), 1)
        app.lib_tab.search_var.set("missing")
        app.lib_tab._execute_search()
        self.assertEqual(app.lib_tab.listbox.size(), 0)
        app.notebook.select(2)
        self.pump()
        app._compute_installed_items_async.assert_called_once()

    def test_invalid_urls_and_busy_folder_change_never_persist(self):
        app, module = self.app, self.module
        with patch.object(module, "_save_settings") as save, patch.object(module.messagebox, "showerror") as error:
            app.board_urls_var.set("file:///bad.json")
            app._apply_board_urls()
            error.assert_called_once()
            save.assert_not_called()
            app._busy = True
            app.folder_var.set("change-during-download")
            app._apply_folder_entry()
            self.assertEqual(app.folder_var.get(), app._download_dir)
            save.assert_not_called()

    def test_solarized_aliases_and_sleeping_window_recolor(self):
        module, app = self.module, self.app
        self.root.geometry("900x620+20+20")
        self.root.deiconify()
        self.pump()
        version = {"version": "2.1.0", "size": 2560, "url": "https://example.invalid/fixture.zip"}
        lib = {"name": "Sensor toolkit", "author": "Preview", "maintainer": "", "category": "Sensors",
               "architectures": ["*"], "sentence": "Read sensors through a compact device interface.",
               "paragraph": "Select the release you need. Downloads retain archive checksums and cancellation support.", "versions": [version]}
        app.lib_tab.populate({lib["name"]: lib})
        app.lib_tab.listbox.selection_set(0)
        app.lib_tab._on_select()
        self.assertEqual(module.Theme.apply_theme("SOLARIZED-DARK"), "solarized_dark")
        for mode in ("light", "solarized_dark", "default"):
            with patch.object(module, "_resolve_lib_req_theme", return_value=mode):
                app._apply_current_theme()
            self.assertEqual(app.root.cget("bg"), module.Theme.BG_DARKEST)
            app.refresh_btn.event_generate("<Enter>")
            self.assertEqual(app.refresh_btn.cget("bg"), app.refresh_btn._hover_bg)
            if RENDER_DIR:
                app.root.geometry("900x620+20+20")
                app.root.deiconify()
                self.pump()
                # Capture only the fixture window through the native Qt platform.
                screen = APP.primaryScreen()
                grab = screen.grabWindow(int(app.root.winfo_id()))
                self.assertFalse(grab.isNull())
                self.assertTrue(grab.save(str(RENDER_DIR / f"downloader-{mode}.png")))
                if mode == "solarized_dark":
                    app.sources_btn.invoke()
                    self.pump()
                    self.assertTrue(screen.grabWindow(int(app.root.winfo_id())).save(str(RENDER_DIR / "downloader-indexes.png")))
                    app.sources_btn.invoke()


def main():
    global RENDER_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromTestCase(ControlChecks),
                               unittest.defaultTestLoader.loadTestsFromTestCase(DownloaderChecks)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
