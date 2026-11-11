#!/usr/bin/env python3
"""Real PySide6 package browser checks in owned fixtures; no live settings or network."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.qt_browser_fixture import APP, QtBrowserFixture, tasks_idle
from main.core.theme import Theme
from main.qt import package_browser as pb
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QLineEdit, QListView, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QTabBar, QTabWidget, QVBoxLayout, QWidget)
from src.modules import arduino_lib_req as engine
from src.modules import ubuntu_download_manager as lifecycle
from main.qt.theme import build_stylesheet


RENDER_DIR = None


def library(name, *versions, **fields):
    return {"name": name, "author": "Fixture", "maintainer": "Owner", "category": "Sensors",
            "architectures": ["avr"], "sentence": f"{name} sentence", "paragraph": "<b>Bold</b> text",
            "website": "https://example.invalid/site", "repository": "https://example.invalid/repo",
            "versions": [{"version": version, "url": f"https://example.invalid/{name}-{version}.zip", "size": 2048}
                         for version in versions or ("1.0.0",)], **fields}


def board(name, *versions):
    return {"name": name, "package": "vendor", "maintainer": "Vendor", "architecture": "avr", "category": "Boards",
            "boards": ["Fixture board"], "website": "https://example.invalid/site", "help_url": "https://example.invalid/help",
            "versions": [{"version": version, "url": f"https://example.invalid/{name}-{version}.zip", "size": 5 * 1024 * 1024,
                          "package": "vendor", "architecture": "avr"} for version in versions or ("1.0.0",)]}


class ToolkitChecks(unittest.TestCase):
    def test_browser_process_never_imports_tkinter(self):
        probe = """
import importlib.abc, os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
class NoTk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'tkinter', '_tkinter'}:
            raise AssertionError('Tk imported into the package browser: ' + fullname)
sys.meta_path.insert(0, NoTk())
from main.qt import package_browser
assert not any(name.split('.')[0] in {'tkinter', '_tkinter'} for name in sys.modules)
"""
        result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=ROOT, capture_output=True,
                                text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class BrowserWindowChecks(QtBrowserFixture):
    def fill(self, *names, tab=None):
        tab = tab or self.app.lib_tab
        tab.populate({name: library(name) for name in names})
        self.settle()
        return tab

    def test_window_is_complete_before_any_catalog_work(self):
        window = self.app
        with patch.object(pb.ArduinoBrowser, "_load_both", side_effect=AssertionError("Catalogs loaded by construction")):
            window.show()
            self.pump()
        self.assertTrue(window.isVisible())
        self.assertEqual(window.notebook.count(), 3)
        self.assertEqual(window.lib_tab.model.rowCount(), 0)
        self.assertFalse(window._busy)

    def test_large_catalog_asks_only_for_rows_on_screen(self):
        window = self.app
        window.show()
        self.pump()
        tab = window.lib_tab
        names = [f"Sensor {index:05}" for index in range(25000)]
        labels = []
        tab.model._label_for = lambda name: (labels.append(name), name)[1]
        started = time.perf_counter()
        tab.populate({name: {"name": name, "versions": []} for name in names})
        self.until(lambda: tab.model.rowCount() == 25000, message="The full catalog was not presented")
        self.pump(.05)
        elapsed = time.perf_counter() - started
        self.assertLess(len(labels), 1500, "A virtual list must not read every row")
        self.assertLess(elapsed, 5, "Presenting a large catalog stalled")
        # Thousands of rows are reachable at once, with no incremental paging.
        tab.listbox.scrollToBottom()
        self.pump()
        self.assertEqual(tab.model.data(tab.model.index(24999)), "Sensor 24999")

    def test_typing_burst_runs_one_search(self):
        tab = self.fill("Alpha", "Beta", "Gamma")
        with patch.object(pb, "search_catalog", wraps=pb.search_catalog) as search:
            for text in ("a", "al", "alp", "alph", "alpha"):
                tab.search_entry.setText(text)
            self.assertTrue(tab._search_timer.isActive())
            search.assert_not_called()
            self.until(lambda: search.called)
            self.pump(pb.SEARCH_DELAY_MS / 1000 + .05)
        search.assert_called_once()
        self.assertEqual(tab.model.rows(), ["Alpha"])
        self.assertEqual(tab.lbl_search_status.text(), "Found 1 match")

    def test_library_and_board_details_show_every_field(self):
        window = self.app
        tab = window.lib_tab
        tab.populate({"Sensor": library("Sensor", "2.0.0", "1.0.0")})
        self.select(tab)
        self.assertEqual(tab.lbl_name.text(), "Sensor")
        self.assertIn("Maintainer: Owner", tab.lbl_meta[0].text())
        self.assertEqual(tab.lbl_meta[1].text(), "Category: Sensors")
        self.assertEqual(tab.lbl_meta[2].text(), "Architectures: avr")
        self.assertEqual(tab.lbl_paragraph.text(), " Bold  text")
        self.assertEqual([tab.version_combo.itemText(row) for row in range(tab.version_combo.count())], ["2.0.0", "1.0.0"])
        self.assertEqual(tab.lbl_size.text(), "Size: 2 KB")
        with patch.object(pb.QDesktopServices, "openUrl") as opened:
            tab.link_secondary.click()
        self.assertEqual(opened.call_args.args[0].toString(), "https://example.invalid/repo")
        tab = window.board_tab
        tab.populate({"Core": board("Core")})
        self.select(tab)
        self.assertEqual(tab.lbl_meta[0].text(), "Package: vendor")
        self.assertEqual(tab.lbl_boards.text(), "Fixture board")
        self.assertEqual(tab.lbl_size.text(), "Size: 5.0 MB")

    def test_stale_update_action_cannot_reach_another_selection(self):
        window, tab = self.app, self.app.lib_tab
        tab.populate({"First": library("First"), "Second": library("Second")})
        self.select(tab, 0)
        self.pump(.05)
        cleanup = ("old-folder", "old.zip")
        tab.set_download_action("Update to v2", lambda: window._download(tab, cleanup_old=cleanup))
        release = threading.Event()
        self.addCleanup(release.set)
        with patch.object(window, "_get_installed_info", side_effect=lambda *a, **k: (release.wait(2), None)[1]), \
                patch.object(window, "_download") as download:
            tab.listbox.setCurrentIndex(tab.model.index(1))
            self.assertEqual(tab.download_btn.text(), "Download")
            tab.download_btn.click()
            release.set()
            self.settle()
        self.assertEqual(download.call_args.args, (tab,))
        self.assertEqual(download.call_args.kwargs, {})

    def test_typed_folder_applies_before_a_download_without_taking_focus(self):
        window, tab = self.app, self.app.lib_tab
        tab.populate({"Sensor": library("Sensor")})
        self.select(tab)
        target = self.folder / "typed"
        window.folder_entry.setText(str(target))
        with patch.object(window, "_prompt_download_option", return_value=""):
            tab.download_btn.click()
        expected = str(target / "_MCUFlasherByNaph_src")
        self.assertEqual(window._download_dir, expected)
        self.assertEqual(window.folder_entry.text(), expected)
        self.assertTrue(os.path.isdir(expected))

    def test_download_starts_the_worker_with_the_chosen_format_and_cancels(self):
        window, tab = self.app, self.app.lib_tab
        tab.populate({"Sensor": library("Sensor")})
        self.select(tab)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def worker(*args):
            calls.append(args)
            entered.set()
            release.wait(2)
            window._post_ui(window._download_cancelled, tab)
        with patch.object(window, "_prompt_download_option", return_value="both"), \
                patch.object(window, "_download_worker", side_effect=worker):
            tab.download_btn.click()
            self.until(entered.is_set)
            self.assertEqual(tab.download_btn.text(), "Cancel")
            self.assertTrue(window._busy)
            self.assertEqual(window.progress.maximum(), 0, "Unknown progress shows the busy indicator")
            tab.download_btn.click()
            self.assertTrue(window._cancel_event.is_set())
            self.assertIn("Cancelling", window.status_label.text())
            release.set()
            self.until(lambda: not window._busy)
        self.settle()
        self.assertEqual(calls[0][1], "https://example.invalid/Sensor-1.0.0.zip")
        self.assertEqual(calls[0][4], "both")
        self.assertEqual(window.status_label.text(), "Download cancelled")
        self.assertEqual(window.progress.maximum(), 1)

    def test_progress_updates_coalesce_and_show_percent(self):
        window = self.app
        window._busy, window._active_download_tab = True, window.lib_tab
        window._post_ui(window._set_progress_determinate, 1000)
        for done in range(0, 1001):
            window._post_ui(window._update_progress, done, 1000)
        with patch.object(window, "_update_progress", wraps=window._update_progress):
            self.assertEqual(len(window._tasks._latest), 1, "Progress must keep only its newest value")
        self.settle()
        self.assertEqual(window.progress.value(), 1000)
        self.assertEqual(window.status_label.text(), "Downloading… 100%")

    def test_installed_rows_show_update_state_and_details_scan_off_the_gui_thread(self):
        window = self.app
        tab = window.installed_tab
        package = self.folder / "downloads/Libs/Sensor"
        example = package / "examples/Blink/Blink.ino"
        example.parent.mkdir(parents=True)
        example.write_text("void setup() {}", encoding="utf-8")
        items = [dict(name="Sensor", type="Library", path=str(package), installed_version="1.0.0",
                      latest_version="2.0.0", update_available=True, archive=""),
                 dict(name="Core", type="Board Platform", path=str(package), installed_version="1.0.0",
                      latest_version="1.0.0", update_available=False, archive="")]
        window._compute_installed_items_async = Mock()
        window.notebook.setCurrentIndex(2)
        tab.populate(items)
        self.assertEqual(tab.lbl_search_status.text(), "2 installed")
        self.assertEqual(tab.model.data(tab.model.index(0)), "Sensor (1.0.0)")
        threads = []
        scan = tab._detail_worker.scan
        tab._detail_worker.scan = lambda request, cancel: (threads.append(threading.get_ident()), scan(request, cancel))[1]
        self.select(tab)
        self.until(lambda: tab._loaded_id == tab._select_id)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(tab.lbl_update_status.text(), "Update available")
        self.assertTrue(tab.update_btn.isEnabled())
        self.assertEqual(tab.examples_model.stringList(), ["Blink / Blink.ino"])
        self.assertTrue(tab.lbl_size.text().startswith("Size on Disk:"))
        tab.listbox.setCurrentIndex(tab.model.index(1))
        self.until(lambda: tab._loaded_id == tab._select_id)
        self.assertEqual(tab.lbl_examples_header.text(), "Available Boards:")
        self.assertFalse(tab.update_btn.isEnabled())
        self.assertEqual(tab.lbl_update_status.text(), "Installed")

    def test_delete_asks_then_removes_on_a_worker_and_declining_keeps_files(self):
        window = self.app
        tab = window.installed_tab
        package = self.folder / "downloads/Libs/Sensor"
        package.mkdir(parents=True)
        (package / "keep.txt").write_text("fixture", encoding="utf-8")
        item = dict(name="Sensor", type="Library", path=str(package), installed_version="1.0.0",
                    latest_version="1.0.0", update_available=False, archive="")
        tab.populate([item])
        self.select(tab)
        with patch.object(pb.QMessageBox, "warning", return_value=QMessageBox.StandardButton.No):
            tab.delete_btn.click()
        self.pump()
        self.assertTrue(package.exists())
        threads = []
        real = shutil.rmtree

        def remove(path, *args, **kwargs):
            threads.append(threading.get_ident())
            return real(path, *args, **kwargs)
        with patch.object(pb.QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(pb.shutil, "rmtree", side_effect=remove), \
                patch.object(window, "_compute_installed_items_async") as refresh:
            tab.delete_btn.click()
            self.assertFalse(tab.delete_btn.isEnabled())
            self.until(tab.delete_btn.isEnabled)
            refresh.assert_called_once()
        self.assertFalse(package.exists())
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(window.status_label.text(), "Deleted Sensor")

    def test_failed_delete_reports_and_keeps_the_row(self):
        window = self.app
        tab = window.installed_tab
        package = self.folder / "downloads/Libs/Sensor"
        package.mkdir(parents=True)
        tab.populate([dict(name="Sensor", type="Library", path=str(package), installed_version="1.0.0",
                           latest_version="1.0.0", update_available=False, archive="")])
        self.select(tab)
        with patch.object(pb.QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(pb.shutil, "rmtree", side_effect=OSError("Fixture in use")):
            tab.delete_btn.click()
            self.until(tab.delete_btn.isEnabled)
        self.assertEqual(self.dialogs[0][0], "critical")
        self.assertIn("Fixture in use", self.dialogs[0][1][2])
        self.assertTrue(package.exists())

    def test_download_options_dialog_offers_exactly_the_available_formats(self):
        dialog = pb.DownloadOptionsDialog(self.app, "core.zip")
        self.addCleanup(dialog.deleteLater)
        buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
        self.assertEqual(set(buttons), {"Folder / Extracted (Default)", "Both (ZIP & Folder)", "Archive Only", "Cancel"})
        self.assertTrue(buttons["Folder / Extracted (Default)"].isDefault())
        buttons["Both (ZIP & Folder)"].click()
        self.assertEqual(dialog.result_option, "both")
        available = pb.DownloadOptionsDialog(self.app, "core.zip", already_available=True)
        self.addCleanup(available.deleteLater)
        buttons = {button.text(): button for button in available.findChildren(QPushButton)}
        self.assertFalse(buttons["Extracted (Already Available)"].isEnabled())
        self.assertFalse(buttons["Both (Already Available)"].isEnabled())
        buttons["Archive Only (ZIP)"].click()
        self.assertEqual(available.result_option, "zip")

    @unittest.skipIf(sys.platform == "win32", "Windows retires through its trigger files, tested separately")
    def test_sleeping_window_retains_its_process_and_force_exit_retires_everything(self):
        window = self.app
        window.show()
        self.pump()
        scan_cancelled = Mock()
        window._tasks._cancellations.append(scan_cancelled)
        window._linux_channel = Mock()
        window._channel_notifier = Mock()
        channel, notifier = window._linux_channel, window._channel_notifier
        with patch.object(lifecycle, "close_viewers") as viewers, \
                patch.object(APP, "quit") as quit_app, \
                patch.object(pb.os.path, "exists", side_effect=AssertionError("Windows handle read")):
            window._force_exit()
        self.assertTrue(window._cancel_event.is_set())
        scan_cancelled.assert_called_once()
        notifier.setEnabled.assert_called_once_with(False)
        channel.close.assert_called_once()
        self.assertIsNone(window._linux_channel)
        viewers.assert_called_once()
        quit_app.assert_called_once()
        self.assertFalse(window.isVisible())

    def test_windows_exit_removes_its_published_handle_only(self):
        window = self.app
        handle = self.folder / ".dm_hwnd"
        handle.write_text("1", encoding="utf-8")
        window._linux_channel = None
        with patch.object(pb.sys, "platform", "win32"), \
                patch.object(lifecycle, "close_viewers", side_effect=AssertionError("Linux cleanup on Windows")), \
                patch.object(APP, "quit") as quit_app:
            window._force_exit()
        self.assertFalse(handle.exists())
        quit_app.assert_called_once()


class FormChecks(QtBrowserFixture):
    def test_tabs_sources_search_and_version_selection(self):
        window, tab = self.app, self.app.lib_tab
        window.resize(900, 620)
        window.show()
        self.pump()
        self.assertEqual(window.notebook.count(), 3)
        window.sources_btn.click()
        self.pump()
        self.assertTrue(window._sources_card.isVisible())
        self.assertEqual(window.sources_btn.text(), "Hide indexes")
        window.sources_btn.click()
        self.pump()
        self.assertFalse(window._sources_card.isVisible())
        self.assertEqual(window.sources_btn.text(), "Board indexes")
        tab.populate({"Demo sensors": library("Demo sensors")})
        self.select(tab)
        self.assertEqual(tab.version_combo.currentText(), "1.0.0")
        tab.search_entry.setText("missing")
        tab.execute_search()
        self.assertEqual(tab.model.rowCount(), 0)
        self.assertEqual(tab.lbl_search_status.text(), "No matches found")
        with patch.object(window, "_compute_installed_items_async") as scan:
            window.notebook.setCurrentIndex(2)
            self.pump()
        scan.assert_called_once()

    def test_invalid_urls_and_busy_folder_change_never_persist(self):
        window = self.app
        with patch.object(engine, "_save_settings") as save:
            window.board_urls_entry.setText("file:///bad.json")
            window._apply_board_urls()
            self.assertEqual(self.dialogs[0][1][1], "Invalid Board Manager URL")
            save.assert_not_called()
            window._busy = True
            window.folder_entry.setText("change-during-download")
            window._apply_folder_entry()
            self.assertEqual(window.folder_entry.text(), window._download_dir)
            save.assert_not_called()
            window._busy = False

    def test_valid_urls_save_then_refresh_each_catalog(self):
        window = self.app
        window.board_urls_entry.setText("https://vendor.invalid/a.json, https://vendor.invalid/b.json")
        with patch.object(window, "_refresh_all") as refresh:
            window._apply_board_urls()
        refresh.assert_called_once()
        self.assertEqual(window._additional_board_urls, ["https://vendor.invalid/a.json", "https://vendor.invalid/b.json"])
        self.assertEqual(engine._load_settings()["additional_board_urls"], window._additional_board_urls)

    def test_solarized_aliases_reach_the_palette(self):
        self.assertEqual(Theme.apply_theme("SOLARIZED-DARK"), "solarized_dark")
        Theme.apply_theme("default")


class FocusLineChecks(QtBrowserFixture):
    def test_no_control_takes_focus_and_text_inputs_still_do(self):
        window = self.app
        window._toggle_sources()
        window.show()
        self.pump()
        controls = window.findChildren(QAbstractButton) + window.findChildren(QTabBar) + window.findChildren(QTabWidget)
        controls += [combo for combo in window.findChildren(QComboBox) if not combo.isEditable()]
        self.assertGreater(len(controls), 10)
        for control in controls:
            control.ensurePolished()
            self.assertEqual(control.focusPolicy(), Qt.FocusPolicy.NoFocus, control)
        for field in window.findChildren(QLineEdit):
            self.assertNotEqual(field.focusPolicy(), Qt.FocusPolicy.NoFocus, field)
        for view in window.findChildren(QListView):
            self.assertNotEqual(view.focusPolicy(), Qt.FocusPolicy.NoFocus, view)

    def test_button_click_keeps_the_cursor_in_the_field_being_typed(self):
        window = self.app
        window.show()
        self.pump()
        window.activateWindow()
        entry = window.lib_tab.search_entry
        entry.setFocus()
        self.pump()
        if not entry.hasFocus():
            self.skipTest("This platform plugin does not deliver window focus")
        QTest.mouseClick(window.sources_btn, Qt.MouseButton.LeftButton)
        self.pump()
        self.assertTrue(window._sources_card.isVisible(), "The button must still work")
        self.assertTrue(entry.hasFocus(), "A button click must not take focus from the search field")
        QTest.mouseClick(window.notebook.tabBar(), Qt.MouseButton.LeftButton,
                         pos=window.notebook.tabBar().tabRect(1).center())
        self.pump()
        self.assertEqual(window.notebook.currentIndex(), 1, "Tabs must still switch")

    def test_ctrl_f_reaches_the_current_search_field(self):
        window = self.app
        window.show()
        self.pump()
        window.notebook.setCurrentIndex(1)
        window._focus_search()
        self.pump()
        self.assertIs(window.notebook.currentWidget().search_entry, window.board_tab.search_entry)

    def test_keyboard_focus_rectangles_never_paint_in_any_theme(self):
        def image(widget):
            return widget.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
        for mode in ("default", "light", "solarized_dark"):
            APP.setStyleSheet(build_stylesheet(mode))
            host = QMainWindow()
            self.addCleanup(host.deleteLater)
            panel = QWidget()
            layout = QVBoxLayout(panel)
            controls = [QPushButton("Button"), QCheckBox("Check box"), QTabBar()]
            controls[2].addTab("Tab")
            for control in controls:
                layout.addWidget(control)
            host.setCentralWidget(panel)
            host.show()
            host.activateWindow()
            self.pump()
            for control in controls:
                with self.subTest(theme=mode, control=type(control).__name__):
                    plain = image(control)
                    control.setFocus()
                    self.pump()
                    if not control.hasFocus():
                        self.skipTest("This platform plugin does not deliver window focus")
                    self.assertEqual(image(control), plain, "Holding focus must not change a control's pixels")
                    control.clearFocus()
            host.close()

    def test_every_palette_has_no_dashed_lines(self):
        for mode in ("default", "light", "solarized_dark"):
            stylesheet = build_stylesheet(mode)
            self.assertNotIn("dashed", stylesheet, mode)
            self.assertNotIn("dotted", stylesheet, mode)
            self.assertNotIn("QPushButton:focus", stylesheet, mode)
            self.assertIn("outline: none", stylesheet.split("}", 1)[0], mode)


class AppearanceChecks(QtBrowserFixture):
    def test_theme_change_preserves_search_selection_version_and_focus(self):
        window, tab = self.app, self.app.lib_tab
        tab.populate({"Sensor": library("Sensor", "2.0.0", "1.0.0")})
        tab.search_entry.setText("sensor")
        tab.execute_search()
        self.select(tab)
        tab.version_combo.setCurrentIndex(1)
        self.settle()
        window.show()
        self.pump()
        before = tab.listbox, tab.search_entry, tab.version_combo
        for mode in ("light", "solarized_dark", "default"):
            with self.subTest(theme=mode), patch.object(engine, "_resolve_lib_req_theme", return_value=mode):
                window._apply_current_theme()
                self.pump()
                self.assertEqual(Theme.active_theme, mode)
                self.assertEqual(tab.selected_name(), "Sensor")
                self.assertEqual(tab.search_entry.text(), "sensor")
                self.assertEqual(tab.version_combo.currentText(), "1.0.0")
                self.assertEqual((tab.listbox, tab.search_entry, tab.version_combo), before)

    def test_every_palette_has_readable_list_selection(self):
        from src.modules.ui_palette import contrast_ratio
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            ink = pb.readable_foreground(Theme.CYAN_DIM)
            self.assertGreaterEqual(contrast_ratio(ink, Theme.CYAN_DIM), 4.5, mode)

    def test_titles_stay_readable_over_a_transparent_label_surface(self):
        from src.modules.ui_palette import contrast_ratio
        window = self.app
        window.show()
        for mode in ("default", "light", "solarized_dark"):
            with self.subTest(theme=mode), patch.object(engine, "_resolve_lib_req_theme", return_value=mode):
                window._apply_current_theme(force=True)
                self.pump()
                label = window.lib_tab.lbl_name
                from main.qt.icons import _ink
                from PySide6.QtGui import QPalette
                color = _ink(label, QPalette.ColorRole.WindowText, QPalette.ColorRole.Window)
                self.assertGreaterEqual(contrast_ratio(color, Theme.BG_MID), 3, "Icon must contrast with its card")

    def test_compact_window_keeps_each_action_reachable(self):
        window, tab = self.app, self.app.board_tab
        tab.populate({"Core": board("Core")})
        window.notebook.setCurrentIndex(1)
        self.select(tab)
        window.show()
        for mode in ("default", "light", "solarized_dark"):
            with self.subTest(theme=mode), patch.object(engine, "_resolve_lib_req_theme", return_value=mode):
                window._apply_current_theme(force=True)
                window.resize(480, 400)
                self.pump(.05)
                self.assertLessEqual(window.width(), 640, "The window must compact on a small monitor")
                for button in (tab.download_btn, tab.prepare_btn, tab.mapping_btn, tab.arduino_cli_btn):
                    tab.detail_scroll.ensureWidgetVisible(button)
                    self.pump()
                    self.assertTrue(button.isVisible())
                    corner = button.mapTo(tab.detail_scroll.viewport(), button.rect().bottomRight())
                    self.assertLessEqual(corner.y(), tab.detail_scroll.viewport().height(), button.text())
                    self.assertGreaterEqual(button.mapTo(tab.detail_scroll.viewport(), QPoint(0, 0)).y(), 0)

    def test_header_and_panes_fit_every_width_and_keep_the_selection(self):
        window, tab = self.app, self.app.lib_tab
        tab.populate({"Sensor": library("Sensor", "2.0.0", "1.0.0")})
        self.select(tab)
        window.show()
        for width in (1040, 760, 620, 480, 900):
            with self.subTest(width=width):
                window.resize(width, 520)
                self.pump(.05)
                for button in (window.refresh_btn, window.sources_btn, window.browse_btn):
                    corner = button.mapTo(window, button.rect().bottomRight())
                    self.assertLessEqual(corner.x(), window.width(), button.text())
                self.assertEqual(tab.selected_name(), "Sensor")
                self.assertGreater(tab.listbox.height(), 40, "The list must stay usable")
                self.assertGreater(tab.detail_scroll.viewport().height(), 40, "Details must stay readable")
                if RENDER_DIR:
                    self.assertTrue(window.grab().save(str(RENDER_DIR / f"browser-{width}.png")))

    def test_typing_reaches_search_rather_than_the_download_folder(self):
        window = self.app
        window.show()
        self.pump(.05)
        for index, tab in enumerate((window.lib_tab, window.board_tab, window.installed_tab)):
            with self.subTest(tab=index):
                window.notebook.setCurrentIndex(index)
                self.pump(.02)
                self.assertIs(window.focusWidget(), tab.search_entry)
        window.notebook.setCurrentIndex(0)
        self.assertIs(window.focusWidget(), window.lib_tab.search_entry)

    def test_controls_fit_and_the_title_yields_at_the_smallest_width(self):
        window, tab = self.app, self.app.lib_tab
        tab.populate({"Sensor": library("Sensor", "2.0.0")})
        self.settle()
        self.select(tab)
        window.show()
        window.resize(480, 520)
        self.pump(.05)
        self.assertEqual(window._brand.isVisible(), window.width() >= 560)
        button = tab.download_btn
        corner = button.mapTo(tab.detail_scroll.viewport(), button.rect().topRight())
        self.assertLessEqual(corner.x(), tab.detail_scroll.viewport().width(), "Download must not be clipped")
        # A small screen at a high scale factor may not offer this width at all.
        window.resize(700, 520)
        self.pump(.05)
        self.assertEqual(window._brand.isVisible(), window.width() >= 560)

    def test_board_controls_and_source_status_wrap_on_a_short_screen(self):
        window, tab = self.app, self.app.board_tab
        item = board("ESP8266 device core")
        item["boards"] = [f"NodeMCU {index} (ESP-12E Module with a long descriptive name)" for index in range(8)]
        tab.populate({item["name"]: item})
        self.select(tab)
        window.notebook.setCurrentIndex(1)
        window._toggle_sources()
        window._set_sources_status("https://vendor.invalid/a-long-index-name.json: Refresh failed; using saved catalog\n"
                                   "https://vendor.invalid/another.json: Available")
        window.resize(480, 510)
        window.show()
        self.pump(.05)
        self.assertGreater(window.sources_status.height(), 12)
        self.assertLessEqual(window.sources_status.width(), window._sources_card.width())
        # The window fits itself to a small screen in a coalesced pass after
        # show; scroll only once its final size has landed, or a later shrink
        # leaves an earlier scroll position pointing past the action.
        deadline, previous = time.monotonic() + 2, None
        while time.monotonic() < deadline and previous != (window.size(), tab.detail_scroll.viewport().size()):
            previous = (window.size(), tab.detail_scroll.viewport().size())
            self.pump(.1)
        # On a short 200% screen the viewport can be shorter than
        # ensureWidgetVisible's default 50 px margins; ask for the action itself.
        tab.detail_scroll.ensureWidgetVisible(tab.arduino_cli_btn, 0, 0)
        self.pump()
        self.assertTrue(tab.arduino_cli_btn.visibleRegion().boundingRect().height() > 0, "Its action must scroll into view")

    def test_capture_every_page_in_every_theme(self):
        window = self.app
        window.lib_tab.populate({"Sensor": library("Sensor", "2.0.0", "1.0.0")})
        window.board_tab.populate({"Core": board("Core")})
        self.select(window.lib_tab)
        self.select(window.board_tab)
        window._compute_installed_items_async = Mock()
        package = self.folder / "downloads/Libs/Sensor"
        package.mkdir(parents=True)
        window.installed_tab.populate([dict(name="Sensor", type="Library", path=str(package), installed_version="1.0.0",
                                            latest_version="2.0.0", update_available=True, archive="")])
        self.select(window.installed_tab)
        window.show()
        for mode in ("default", "light", "solarized_dark"):
            with patch.object(engine, "_resolve_lib_req_theme", return_value=mode):
                window._apply_current_theme(force=True)
            for index, name in enumerate(("libraries", "boards", "installed")):
                window.notebook.setCurrentIndex(index)
                self.pump(.05)
                image = window.grab().toImage()
                self.assertFalse(image.isNull())
                self.assertGreater(len({image.pixel(x, image.height() // 2) for x in range(0, image.width(), 7)}), 2,
                                   f"{mode} {name} must paint its surfaces")
                if RENDER_DIR:
                    self.assertTrue(image.save(str(RENDER_DIR / f"browser-{mode}-{name}.png")))

    def test_idle_window_runs_no_periodic_work(self):
        window = self.app
        window.show()
        self.pump()
        from PySide6.QtCore import QTimer
        # Windows keeps one slow fallback beside its directory watcher for its wake files.
        fallback = getattr(window, "_trigger_timer", None)
        active = [timer for timer in window.findChildren(QTimer)
                  if timer.isActive() and timer.parent() is not window._screen_watcher and timer is not fallback]
        self.assertEqual(active, [], "An idle window must not wake on a timer")


class SampleViewerProbeChecks(QtBrowserFixture):
    """Probe the prepared viewer off the GUI thread, then open the latest selection."""

    def setUp(self):
        super().setUp()
        self.files = [self.folder / "first.ino", self.folder / "latest.ino"]
        for file in self.files:
            file.write_text("void setup() {}", encoding="utf-8")
        self.thread_ids = []
        self.release, self.started = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        for target, name, value in ((engine, "_launch_code_viewer", Mock()), (pb, "get_monitor_font_size", Mock(return_value=24)),):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.launch = engine._launch_code_viewer
        self.is_current = lambda: True

    def probe(self, cancel=None):
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        self.release.wait(2)
        return None if cancel is not None and cancel.is_set() else sys.executable

    def open(self, index=0):
        self.app.open_code_viewer(str(self.files[index]), tuple(map(str, self.files)), lambda: self.is_current())

    def test_bounded_probe_keeps_the_view_responsive_and_opens_latest(self):
        with patch.object(engine, "_find_code_viewer_python", side_effect=self.probe):
            started = time.monotonic()
            self.open()
            self.assertLess(time.monotonic() - started, .08)
            self.until(self.started.is_set)
            self.open(1)
            self.pump()
            self.release.set()
            self.until(lambda: self.launch.called)
            self.pump(.05)
        self.launch.assert_called_once()
        self.assertEqual(self.launch.call_args.args[0], str(self.files[1]))
        self.assertEqual(self.launch.call_args.kwargs["font_size"], 24)
        self.assertEqual(self.launch.call_args.kwargs["theme_mode"], Theme.active_theme)
        self.assertNotIn(threading.get_ident(), self.thread_ids)

    def test_changed_selection_or_hidden_window_does_not_launch_a_stale_sample(self):
        with patch.object(engine, "_find_code_viewer_python", side_effect=self.probe):
            self.is_current = lambda: False
            self.open()
            self.until(self.started.is_set)
            self.release.set()
            self.pump(.1)
            self.settle()
            self.launch.assert_not_called()
            self.is_current = lambda: True
            self.app._is_hidden = True
            self.open()
            self.pump(.1)
            self.settle()
            self.launch.assert_not_called()

    def test_closed_window_discards_probe_completion(self):
        with patch.object(engine, "_find_code_viewer_python", side_effect=self.probe):
            self.open()
            self.until(self.started.is_set)
            self.app._tasks.close()
            self.release.set()
            self.pump(.1)
            self.launch.assert_not_called()

    def test_missing_dependency_prompts_bootstrap_without_installing(self):
        with patch.object(engine, "_find_code_viewer_python", return_value=None):
            self.open()
            self.until(lambda: bool(self.dialogs))
        self.assertEqual(self.dialogs[0][0], "information")
        self.assertEqual(self.dialogs[0][1][1], "Bootstrap required")
        self.launch.assert_not_called()

    def test_deleted_sample_reports_error_after_probe(self):
        with patch.object(engine, "_find_code_viewer_python", side_effect=self.probe):
            self.open()
            self.until(self.started.is_set)
            self.files[0].unlink()
            self.release.set()
            self.until(lambda: bool(self.dialogs))
        self.assertEqual(self.dialogs[0][0], "critical")
        self.launch.assert_not_called()

    def test_queued_old_completion_cannot_open_an_extra_window(self):
        with patch.object(engine, "_find_code_viewer_python", return_value=sys.executable):
            self.open()
            deadline = time.monotonic() + 2
            while not self.app._tasks._queue and time.monotonic() < deadline:
                time.sleep(.005)  # Finish the worker without draining the GUI queue.
            self.open()
            self.until(lambda: self.launch.called)
            self.pump(.1)
        self.launch.assert_called_once()

    def test_probe_error_is_reported_without_requesting_installation(self):
        with patch.object(engine, "_find_code_viewer_python", side_effect=RuntimeError("Probe unavailable")):
            self.open()
            self.until(lambda: bool(self.dialogs))
        self.assertEqual(self.dialogs[0][0], "critical")
        self.assertIn("Probe unavailable", self.dialogs[0][1][2])
        self.launch.assert_not_called()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path)
    options, remaining = parser.parse_known_args()
    RENDER_DIR = options.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
