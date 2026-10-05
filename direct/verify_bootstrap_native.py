#!/usr/bin/env python3
"""Verify the cold setup UI with stdlib Tk only; no installers, Qt or live writes."""
from __future__ import annotations

import argparse
import ast
import importlib.abc
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Optional
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")


class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "PySide6" or fullname.startswith("PySide6."):
            raise ModuleNotFoundError("Qt deliberately unavailable in the cold fixture", name=fullname)


sys.meta_path.insert(0, NoQt())
from main.core.theme import Theme
from src.modules.bootstrap_native import NativeBootstrapWindow, NativeSignals

REPORT_DIR = None


def fixture(mode="default", has_qt=False, *, preimport_qt=False):
    """Use the real BootstrapGUI API while excluding all bootstrap import side effects."""
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    names = {"_BootstrapRootProxy", "BootstrapGUI", "_prepare_bootstrap_qt_path",
             "_load_bootstrap_qt", "_bootstrap_qt_context"}
    nodes = [node for node in tree.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    palette = {"T_" + key: value for key, value in Theme.PALETTES[mode].items()}
    namespace = dict(globals(), HAS_PYSIDE6_BOOTSTRAP=has_qt, _BOOTSTRAP_THEME_MODE=mode,
                     SCRIPT_DIR=ROOT, _BOOTSTRAP_QT_IMPORT_ERROR="",
                     _resolve_bootstrap_theme=lambda: (palette, mode),
                     _T_PALETTE=palette, DEFAULT_SKIP_UPDATES=True, BOOTSTRAP_CLOSE_DELAY_S=0,
                     load_bootstrap_config=Mock(return_value={"other": "kept"}),
                     save_bootstrap_config=Mock(return_value=True),
                     _record_bootstrap_log=Mock(), _record_bootstrap_exception=Mock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    if preimport_qt:
        # Production bootstrap prepares the env path and imports QtWidgets at
        # module scope before BootstrapGUI opens its warm view. Exercise that
        # second path check with Qt's embedded support aliases already loaded.
        assert has_qt
        namespace["_prepare_bootstrap_qt_path"]()
        from PySide6.QtWidgets import QApplication
    return namespace["BootstrapGUI"](), namespace


class ColdWindowChecks(unittest.TestCase):
    def own(self, mode="default"):
        gui, namespace = fixture(mode)
        self.addCleanup(gui._native_window._on_close)
        return gui, namespace

    def drain(self, gui):
        gui._native_window._drain()
        gui.pump()

    def test_cold_constructor_visible_palette_and_native_geometry_without_qt(self):
        facts = []
        for mode in Theme.PALETTES:
            gui, _ = self.own(mode)
            window = gui._native_window
            self.assertIsInstance(window, NativeBootstrapWindow)
            self.assertIsNone(gui._app)
            self.assertTrue(window.root.winfo_viewable())
            self.assertEqual(window.root.cget("bg"), Theme.PALETTES[mode]["BG_DARKEST"])
            self.assertEqual(window.log_edit.cget("bg"), Theme.PALETTES[mode]["BG_DARKEST"])
            self.assertEqual(window.root.resizable(), (1, 1))
            self.assertLessEqual(window.root.winfo_width(), window.root.winfo_screenwidth())
            self.assertLessEqual(window.root.winfo_height(), window.root.winfo_screenheight())
            facts.append({"theme": mode, "window_visible": bool(window.root.winfo_viewable()),
                          "size": [window.root.winfo_width(), window.root.winfo_height()],
                          "background": window.root.cget("bg"), "qt_imported": False})
            window._on_close()
        self.assertFalse(any(name.startswith("PySide6") for name in sys.modules))
        if REPORT_DIR:
            (REPORT_DIR / "cold-window-facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")

    def test_worker_is_queued_and_callbacks_run_on_creating_thread(self):
        gui, namespace = self.own()
        window = gui._native_window
        callback_threads = []
        def worker():
            gui.log_section("Cold dependency preparation")
            gui.set_status("Installing missing packages")
            gui.root.after(0, lambda: callback_threads.append(threading.get_ident()))
            gui.set_progress_percent(60)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(window.log_edit.get("1.0", "end-1c"), "")
        self.assertNotEqual(window.status.cget("text"), "Installing missing packages")
        self.drain(gui)
        self.assertIn("Cold dependency preparation", window.log_edit.get("1.0", "end-1c"))
        self.assertEqual(window.status.cget("text"), "Installing missing packages")
        self.assertEqual(callback_threads, [threading.get_ident()])
        namespace["_record_bootstrap_exception"].assert_not_called()

    def test_live_progress_replaces_and_whole_failed_step_is_red(self):
        gui, _ = self.own("solarized_dark")
        window, text = gui._native_window, gui._native_window.log_edit
        gui.log_section("Earlier success")
        gui.log_ok("Kept green")
        gui.log_section("Failed step")
        gui.log_status("First diagnostic")
        gui.update_pip_table_block("PACKAGE 10%\n")
        gui.update_pip_table_block("PACKAGE 20%\n")
        self.assertNotIn("10%", text.get("1.0", "end-1c"))
        self.assertEqual(text.get("1.0", "end-1c").count("PACKAGE"), 1)
        gui.log_fail("Real fixture failure")
        for value in ("Failed step", "First diagnostic", "PACKAGE 20%", "Real fixture failure"):
            self.assertIn("failed_step", text.tag_names(text.search(value, "1.0")))
        self.assertNotIn("failed_step", text.tag_names(text.search("Kept green", "1.0")))
        gui.update_pip_table_block("PACKAGE 30%\n")
        self.assertIn("failed_step", text.tag_names(text.search("PACKAGE 30%", "1.0")))
        gui.commit_pip_table_block()
        gui.log_section("Next step")
        self.assertNotIn("failed_step", text.tag_names(text.search("Next step", "1.0")))
        gui.update_platformio_progress_block("PIO 50%\n")
        gui.clear_platformio_progress_block()
        self.assertNotIn("PIO 50%", text.get("1.0", "end-1c"))

    def test_actual_scrollbar_press_release_and_off_preserve_view(self):
        gui, _ = self.own()
        window, text = gui._native_window, gui._native_window.log_edit
        window._set_details_expanded(True)
        for number in range(180):
            gui.log_status(f"Native row {number:03d}")
        gui.pump()
        self.assertGreaterEqual(text.yview()[1], .999)

        def press_thumb():
            gui.pump()
            bar = window.scrollbar
            x = bar.winfo_width() // 2
            positions = [y for y in range(bar.winfo_height()) if bar.identify(x, y) == "thumb"]
            self.assertTrue(positions)
            y = positions[len(positions) // 2]
            bar.event_generate("<ButtonPress-1>", x=x, y=y)
            return x, y

        x, y = press_thumb()
        self.assertTrue(window.held)
        text.yview_moveto(.3)
        self.assertLess(text.yview()[1], .999)
        visible = text.get(text.index("@0,0"), text.index("@0,0 lineend"))
        gui.log_status("Incoming while held")
        gui.update_pip_table_block("Native progress 10%\n")
        self.assertEqual(text.get(text.index("@0,0"), text.index("@0,0 lineend")), visible)
        window.scrollbar.event_generate("<ButtonRelease-1>", x=x, y=y)
        gui.pump()
        self.assertFalse(window.held)
        self.assertGreaterEqual(text.yview()[1], .999)
        window.auto_var.set(False)
        window._auto()
        text.yview_moveto(.2)
        visible = text.get(text.index("@0,0"), text.index("@0,0 lineend"))
        x, y = press_thumb()
        window.scrollbar.event_generate("<ButtonRelease-1>", x=x, y=y)
        gui.log_status("Incoming with Auto OFF")
        gui.pump()
        self.assertEqual(text.get(text.index("@0,0"), text.index("@0,0 lineend")), visible)
        window.auto_var.set(True)
        window._auto()
        self.assertGreaterEqual(text.yview()[1], .999)

    def test_off_empty_display_stays_at_top_and_display_budget_trims(self):
        gui, _ = self.own()
        window, text = gui._native_window, gui._native_window.log_edit
        window._set_details_expanded(True)
        window.auto_var.set(False)
        window._auto()
        for number in range(100):
            gui.log_status(f"OFF row {number:03d}")
        gui.pump()
        self.assertIn("OFF row 000", text.get(text.index("@0,0"), text.index("@0,0 lineend")))
        window.DISPLAY_CHARS = 4000
        window.DISPLAY_LINES = 40
        for number in range(80):
            gui.log_status(f"Budget row {number:03d} " + "x" * 80)
        self.assertLessEqual(text.count("1.0", "end-1c", "chars")[0], 4000)
        self.assertLessEqual(int(text.index("end-1c").split(".")[0]), 40)
        self.assertIn("Budget row 079", text.get("1.0", "end-1c"))

    def test_queued_display_bound_and_control_retention(self):
        gui, namespace = self.own()
        window = gui._native_window
        signals = window.signals
        signals.MAX_EVENTS = 12
        signals.MAX_CHARS = 4000
        def worker():
            for number in range(60):
                signals.sig_log.emit("Queue row " + str(number) + "x" * 120, "normal")
            signals.sig_log.emit("Important failure", "fail")
            signals.sig_status.emit("Retained control")
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertLessEqual(len(signals._events), 12)
        self.assertLessEqual(signals._chars, 4000)
        self.drain(gui)
        self.assertIn("Important failure", window.log_edit.get("1.0", "end-1c"))
        self.assertIn("full output remains", window.log_edit.get("1.0", "end-1c"))
        self.assertEqual(window.status.cget("text"), "Retained control")
        namespace["_record_bootstrap_exception"].assert_not_called()

    def test_failed_skip_write_reverts_without_live_persistence(self):
        gui, namespace = self.own()
        window = gui._native_window
        namespace["save_bootstrap_config"].return_value = False
        window.skip_var.set(False)
        window._skip()
        self.assertTrue(gui._skip_updates)
        self.assertTrue(window.skip_var.get())
        self.assertIn("was not saved", window.log_edit.get("1.0", "end-1c"))

    def test_summary_default_disclosure_preserves_raw_and_stage_scoped_successes(self):
        gui, namespace = self.own()
        window = gui._native_window
        self.assertFalse(window._details_expanded)
        gui.log_section("Python dependency preparation")
        gui.log_status("pyserial: not installed, will download and install.")
        gui.log_status("pyserial: Collecting pyserial")
        gui.log_status("pyserial: Downloading pyserial-3.5.whl.metadata (1.6 kB)")
        gui.log_status("pyserial: Saved .\\fixture\\private-wheelhouse\\pyserial.whl")
        gui.log_ok("Downloaded pyserial successfully.")
        gui.log_ok("Downloaded pyserial successfully.")
        gui.log_warn("Declared board framework is unavailable")
        gui.log_fail("Required tool package is missing")
        gui.log_dim("pip: No matching distribution found for fixture-tool")
        gui.pump()
        summary = window.summary_edit.get("1.0", "end-1c")
        details = window.log_edit.get("1.0", "end-1c")
        self.assertIn("Python dependency preparation", summary)
        self.assertEqual(summary.count("Downloaded pyserial successfully."), 1)
        for noise in ("Collecting pyserial", ".whl.metadata", "private-wheelhouse", "not installed, will"):
            self.assertNotIn(noise, summary)
            self.assertIn(noise, details)
        for message, tag in (("Declared board framework is unavailable", "warn"),
                             ("Required tool package is missing", "fail")):
            self.assertIn(message, summary)
            self.assertIn(tag, window.summary_edit.tag_names(window.summary_edit.search(message, "1.0")))
        self.assertIn("No matching distribution found", summary)
        self.assertEqual(details.count("Downloaded pyserial successfully."), 2)
        self.assertEqual(namespace["_record_bootstrap_log"].call_count, 10)
        gui.log_section("A separate verification stage")
        gui.log_ok("Downloaded pyserial successfully.")
        self.assertEqual(window.summary_edit.get("1.0", "end-1c").count("Downloaded pyserial successfully."), 2)
        snapshot = window.snapshot()
        self.assertEqual(snapshot["summary"]["text"], window.summary_edit.get("1.0", "end-1c"))
        self.assertEqual(snapshot["presentation"], window._presentation.snapshot())
        self.assertFalse(snapshot["details_expanded"])
        window._set_details_expanded(True)
        gui.pump()
        self.assertTrue(window._details_expanded)
        self.assertIn("private-wheelhouse", window.log_edit.get("1.0", "end-1c"))
        window._set_details_expanded(False)
        self.assertFalse(window._details_expanded)
        namespace["save_bootstrap_config"].assert_not_called()
        namespace["_record_bootstrap_exception"].assert_not_called()

    def test_compact_package_rows_replace_without_polluting_summary(self):
        gui, namespace = self.own("solarized_dark")
        window = gui._native_window
        divider = "  " + "─" * 104
        def table(percent, status):
            return (f"{divider}\n  pyserial                                  {status}\n"
                    f"  ▰▱▱▱  {percent}%\n{divider}\n"
                    f"  PySide6                                   ⚙ Installing...\n  ▰▰▱▱  80%\n{divider}\n")
        gui.update_pip_table_block(table(25, "⬇ Downloading..."))
        gui.pump()
        self.assertEqual([(row.name, row.percent) for row in window.package_panel.rows],
                         [("pyserial", 25), ("PySide6", 80)])
        self.assertEqual(window.summary_edit.get("1.0", "end-1c"), "")
        drawn = [window.package_panel.itemcget(item, "text") for item in window.package_panel.find_all()
                 if window.package_panel.type(item) == "text"]
        self.assertTrue(any("pyserial" in label for label in drawn), drawn)
        self.assertTrue(any("25%" in label for label in drawn), drawn)
        self.assertFalse(any("▰" in label or "─" * 20 in label for label in drawn), drawn)
        gui.update_pip_table_block(table(100, "✔ Installed"))
        gui.commit_pip_table_block()
        gui.pump()
        self.assertEqual(window.package_panel.rows[0].tone, "ok")
        self.assertEqual(window.package_panel.rows[0].percent, 100)
        self.assertEqual(window.log_edit.get("1.0", "end-1c").count("pyserial"), 1)
        gui.update_pip_table_block("PACKAGE             STATUS\nPlatformIO          ▰▰▰▰▱▱ 65%\n")
        gui.pump()
        self.assertEqual([(row.name, row.status, row.percent) for row in window.package_panel.rows],
                         [("PlatformIO", "Preparing", 65)])
        drawn = [window.package_panel.itemcget(item, "text") for item in window.package_panel.find_all()
                 if window.package_panel.type(item) == "text"]
        self.assertTrue(any("65%" in label for label in drawn), drawn)
        self.assertFalse(any("▰" in label or "STATUS" in label for label in drawn), drawn)
        gui.update_platformio_progress_block("  ESP32: Unpacking — toolchain-xtensa-esp32\n  ▰▰▱▱  60%")
        gui.pump()
        self.assertEqual([(row.name, row.status, row.percent) for row in window.package_panel.rows],
                         [("toolchain-xtensa-esp32", "Unpacking", 60)])
        gui.clear_platformio_progress_block()
        gui.pump()
        self.assertFalse(window.package_panel.rows)
        status = "Installing Python dependencies (" + ", ".join(f"fixture-package-{i}" for i in range(14)) + ")..."
        gui.set_status(status)
        self.assertIn("14 packages", window.status.cget("text"))
        self.assertEqual(window._status_detail, status)
        namespace["save_bootstrap_config"].assert_not_called()
        namespace["_record_bootstrap_exception"].assert_not_called()

    def test_mainloop_worker_close_has_no_qt_timer(self):
        gui, namespace = self.own()
        def worker():
            gui.root.after(0, lambda: gui.log_ok("Cold window ready"))
            gui.stop_spinner("Ready", True)
            gui.close_after_delay(.05)
        thread = threading.Thread(target=worker)
        thread.start()
        gui.mainloop_until_done()
        thread.join(2)
        self.assertTrue(gui._closed)
        self.assertTrue(gui._native_window.closed)
        self.assertTrue(gui._done_event.is_set())
        self.assertFalse(gui._native_window._timers)
        namespace["_record_bootstrap_exception"].assert_not_called()

    def test_short_scaled_window_keeps_reading_and_wrapped_footer_inside_cards(self):
        import tkinter as tk
        from tkinter import font as tkfont
        from src.modules import bootstrap_native as native
        from src.modules.bootstrap_presentation import PackageRow
        from src.modules.ui_metrics import WorkArea

        original_tk = tk.Tk
        def scaled_root(*args, **kwargs):
            root = original_tk(*args, **kwargs)
            root.tk.call("tk", "scaling", 2 * 96 / 72)
            return root
        area = WorkArea(0, 0, 1280, 624)
        screen = WorkArea(0, 0, 1280, 720)
        with patch.object(native.tk, "Tk", side_effect=scaled_root), \
                patch.object(native, "_monitor_area", return_value=(area, screen)):
            gui, namespace = self.own()
            window = gui._native_window
            for number in range(60):
                gui.log_status(f"Activity {number}: validating local package readiness")
            window.package_panel.set_rows([
                PackageRow(f"fixture-toolchain-{number}", "Unpacking local archive", 45)
                for number in range(14)])
            status = ("Preparing local board tools and validating toolchain-xtensa-esp32 "
                      "for the selected framework while checking dependencies and archive records")
            gui.set_status(status)
            window.clock.configure(text="27:48:59")

            def settle():
                until = time.monotonic() + .25
                while time.monotonic() < until:
                    window.pump()
                    time.sleep(.008)

            settle()
            raw, summary = (view.get("1.0", "end-1c") for view in (window.log_edit, window.summary_edit))
            fonts = [view.cget("font") for view in (window.log_edit, window.summary_edit)]
            window.auto_var.set(False)
            window.held = True
            for view in (window.log_edit, window.summary_edit):
                view.tag_add("sel", "1.2", "1.8")
                view.yview_moveto(.35)
            selection = [tuple(str(value) for value in view.tag_ranges("sel"))
                         for view in (window.log_edit, window.summary_edit)]
            for height, details in ((560, False), (512, True), (560, True), (512, False)):
                window.root.geometry(f"1040x{height}+80+0")
                window._set_details_expanded(details)
                settle()
                for card in window._cards:
                    self.assertGreaterEqual(card.winfo_x(), 0)
                    self.assertGreaterEqual(card.winfo_y(), 0)
                    self.assertLessEqual(card.winfo_x() + card.winfo_width(), window.root.winfo_width())
                    self.assertLessEqual(card.winfo_y() + card.winfo_height(), window.root.winfo_height())
                    self.assertLessEqual(card.body.winfo_y() + card.body.winfo_height(), card.winfo_height())
                for view in (window.log_edit, window.summary_edit):
                    font = tkfont.Font(root=window.root, font=view.cget("font"))
                    needed = 2 * font.metrics("linespace") + 2 * view.winfo_pixels(view.cget("pady"))
                    self.assertGreaterEqual(view.winfo_height(), needed)
                for button in window._options.winfo_children():
                    font = tkfont.Font(root=window.root, font=button.cget("font"))
                    inset = sum(button.winfo_pixels(button.cget(key))
                                for key in ("pady", "bd", "highlightthickness"))
                    self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
                    self.assertGreaterEqual(button.winfo_height() - 2 * inset, font.metrics("linespace"))
                    bottom = window._options.winfo_y() + button.winfo_y() + button.winfo_height()
                    self.assertLessEqual(bottom, window._footer.winfo_height())
                self.assertGreaterEqual(window.status.winfo_height(), window.status.winfo_reqheight())
                self.assertEqual(window._status_detail, status)
                self.assertEqual(window.log_edit.get("1.0", "end-1c"), raw)
                self.assertEqual(window.summary_edit.get("1.0", "end-1c"), summary)
                self.assertEqual([view.cget("font") for view in (window.log_edit, window.summary_edit)], fonts)
                self.assertEqual([tuple(str(value) for value in view.tag_ranges("sel"))
                                  for view in (window.log_edit, window.summary_edit)], selection)
                self.assertTrue(window.held)
                self.assertFalse(window.auto_var.get())
            namespace["save_bootstrap_config"].assert_not_called()
            namespace["_record_bootstrap_exception"].assert_not_called()

    def test_fractional_scale_reserves_text_before_the_raw_horizontal_scrollbar(self):
        import tkinter as tk
        from src.modules import bootstrap_native as native
        from src.modules.bootstrap_presentation import PackageRow
        from src.modules.ui_metrics import WorkArea

        original_tk = tk.Tk
        def scaled_root(*args, **kwargs):
            root = original_tk(*args, **kwargs)
            root.tk.call("tk", "scaling", 1.75 * 96 / 72)
            return root
        with patch.object(native.tk, "Tk", side_effect=scaled_root), \
                patch.object(native, "_monitor_area", return_value=(WorkArea(0, 0, 1280, 636), WorkArea(0, 0, 1280, 720))):
            gui, namespace = self.own()
            window = gui._native_window
            window.package_panel.set_rows([
                PackageRow(f"fixture-toolchain-{number}", "Unpacking local archive", 45)
                for number in range(14)])
            gui.set_status("Preparing local board tools and validating toolchain-xtensa-esp32 "
                           "for the selected framework while checking dependencies and archive records")
            window._set_details_expanded(True)
            until = time.monotonic() + .3
            while time.monotonic() < until:
                window.pump()
                time.sleep(.008)
            for view in (window.log_edit, window.summary_edit):
                self.assertGreaterEqual(view.winfo_height(), round(40 * native.ui_scale(window.root)))
            self.assertLessEqual(window._footer.winfo_height() + window._footer.winfo_y(),
                                 window._footer.master.winfo_height())
            namespace["save_bootstrap_config"].assert_not_called()
            namespace["_record_bootstrap_exception"].assert_not_called()


def main():
    global REPORT_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    REPORT_DIR = args.report_dir
    if REPORT_DIR:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ColdWindowChecks))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
