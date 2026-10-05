"""Hardware-free rendering checks for diagnostics, notifications and terminals."""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette
from main.qt.compat_panel import CompatPanel
from main.qt.log_colors import contrast_ratio
from main.qt.notif_panel import NotifPanel
from main.qt.posix_terminal_panel import PosixTerminalPanel
from main.qt.syntax_panel import SyntaxPanel
from main.qt.terminal_panel import TerminalPanel
from main.qt.theme import get_palette

APP = QApplication.instance() or QApplication([])
MODES = ("default", "light", "solarized_dark")


def rendered_color(widget, text: str) -> str:
    cursor = widget.document().find(text)
    if cursor.isNull():
        raise AssertionError(f"Missing rendered text {text!r}")
    return cursor.charFormat().foreground().color().name()


def select_text(widget, text: str) -> None:
    widget.setTextCursor(widget.document().find(text))


class PanelReadabilityChecks(unittest.TestCase):
    def setUp(self):
        self.real_thread_class = threading.Thread
        self.config_patch = patch("main.core.config._load_raw_config", return_value={"shared": {}, "instances": {}})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.persistence_patch = patch("main.core.config._save_raw_config", side_effect=AssertionError("Unexpected persistence"))
        self.persistence_patch.start()
        self.addCleanup(self.persistence_patch.stop)
        self.callback_errors = []
        exception_hook = patch.object(sys, "excepthook", side_effect=lambda *error: self.callback_errors.append(error))
        exception_hook.start()
        self.addCleanup(exception_hook.stop)
        fixture_db = str(ROOT / "temp" / "audit" / "notification-fixture" / "dbs_notif.json")
        database_patch = patch("src.dbs.dbs_create.get_default_db_path", return_value=fixture_db)
        database_patch.start()
        self.addCleanup(database_patch.stop)
        worker_patch = patch("main.qt.notif_panel.threading.Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=target))
        worker_patch.start()
        self.addCleanup(worker_patch.stop)

    def tearDown(self):
        self.assertEqual(self.callback_errors, [], "An asynchronous Qt callback failed")

    def widget(self, instance):
        if isinstance(instance, SyntaxPanel):
            instance._bg_timer.stop()
        self.addCleanup(instance.deleteLater)
        if isinstance(instance, NotifPanel):
            APP.processEvents()
        return instance

    def assert_readable(self, color, *backgrounds):
        for background in backgrounds:
            self.assertGreaterEqual(contrast_ratio(color, background), 4.5,
                                    f"{color} is unreadable against {background}")

    def test_compatible_output_recolors_and_preserves_filter_selection_font(self):
        panel = self.widget(CompatPanel())
        panel._output.setFont(panel._output.font())
        font = panel._output.font().toString()
        entries = [(f"{kind} readable device", kind) for kind in ("normal", "system", "success", "error", "warning", "dim")]
        panel.set_content(entries)
        panel.search_input.setText("readable")
        select_text(panel._output, "normal readable device")
        for mode in MODES:
            panel.apply_theme(mode)
            bg = get_palette(mode)["BG_DARKEST"]
            for text, _ in entries:
                self.assert_readable(rendered_color(panel._output, text), bg)
            self.assertEqual(panel._output.textCursor().selectedText(), "normal readable device")
            self.assertEqual(panel._output.font().toString(), font)
            self.assertEqual(panel.search_input.text(), "readable")
        panel.search_input.setText("missing device")
        self.assertIn("No devices match", panel._output.toPlainText())
        self.assertNotIn("normal readable device", panel._output.toPlainText())
        for mode in MODES:
            panel.apply_theme(mode)
            self.assert_readable(rendered_color(panel._output, "No devices match"), get_palette(mode)["BG_DARKEST"])

    def test_syntax_severity_and_status_recolor_without_losing_selected_rows(self):
        panel = self.widget(SyntaxPanel())
        panel.set_font_size(14)
        panel.set_diagnostics([{"file": "demo.ino", "line": 1, "severity": severity, "message": "Readable fixture"}
                               for severity in ("error", "warning", "note")])
        panel._table.selectRow(1)
        for mode in MODES:
            pal = get_palette(mode)
            panel.apply_theme(mode)
            for row in range(3):
                self.assert_readable(panel._table.item(row, 2).foreground().color().name(), pal["BG_DARKEST"], pal["BG_DARK"])
            self.assertEqual(panel._table.currentRow(), 1)
            self.assertEqual({item.row() for item in panel._table.selectedItems()}, {1})
            self.assertEqual(panel._table.font().pointSize(), 14)
            self.assertEqual(panel._lbl_status.text(), "1 err, 1 warn")
            self.assertIn(panel._semantic_colors["error"], panel._lbl_status.styleSheet())
            panel.set_diagnostics([])
            self.assertIn(panel._semantic_colors["success"], panel._lbl_status.styleSheet())
            self.assert_readable(panel._semantic_colors["success"], pal["BG_DARK"], pal["BG_MID"])
            panel.set_diagnostics([{"file": "demo.ino", "line": 1, "severity": severity, "message": "Readable fixture"}
                                   for severity in ("error", "warning", "note")])
            panel._table.selectRow(1)

    def test_notification_cards_recolor_existing_records_without_database_reload(self):
        records = [{"level": kind, "title": f"{kind} retained title", "message": f"{kind} retained message", "time": "12:34:56"}
                   for kind in ("error", "warning", "info", "success")]
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=records) as load:
            panel = self.widget(NotifPanel())
            select_text(panel._browser, "success retained message")
            for mode in MODES:
                pal = get_palette(mode)
                panel.apply_theme(mode)
                for record in records:
                    for text in (record["title"], record["message"], record["time"]):
                        self.assert_readable(rendered_color(panel._browser, text), pal["BG_DARK"], pal["BG_DARKEST"])
                self.assertEqual(panel._browser.textCursor().selectedText(), "success retained message")
            self.assertEqual(load.call_count, 1)
            panel._append_notification_card({"type": "info", "message": "live retained message"})
            panel.apply_theme("light")
            self.assert_readable(rendered_color(panel._browser, "live retained message"), get_palette("light")["BG_DARK"])

    def test_notification_display_retention_stays_bounded_after_recolor(self):
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[]):
            panel = self.widget(NotifPanel())
        for index in range(151):
            panel._append_notification_card({"message": f"Notification {index:03d}"})
        self.assertEqual(len(panel._records), 151)
        self.assertEqual(len(panel._visible_records()), 150)
        self.assertNotIn("Notification 000", panel._browser.toPlainText())
        for mode in MODES:
            panel.apply_theme(mode)
            self.assertNotIn("Notification 000", panel._browser.toPlainText())
            self.assertIn("Notification 150", panel._browser.toPlainText())
            self.assertLessEqual(panel._browser.document().characterCount(), 256_000)

    def test_notification_live_filters_retain_events_and_reuse_bounded_history(self):
        records = [
            {"category": "board_install", "level": "success", "message": "board persisted"},
            {"category": "library_install", "level": "success", "message": "library persisted"},
            {"category": "device", "level": "info", "message": "device persisted"},
            {"category": "system", "level": "error", "message": "error persisted"},
        ]
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=records) as load:
            panel = self.widget(NotifPanel())
            panel._filter_combo.setCurrentText("📦 Boards & Libraries")
            self.assertIn("board persisted", panel._browser.toPlainText())
            self.assertIn("library persisted", panel._browser.toPlainText())
            self.assertNotIn("device persisted", panel._browser.toPlainText())
            panel._append_notification_card({"category": "usb", "type": "info", "message": "usb live"})
            panel._append_notification_card({"category": "library_install", "type": "success", "message": "library live"})
            panel._append_notification_card({"type": "info", "message": "uncategorized live"})
            self.assertNotIn("usb live", panel._browser.toPlainText())
            self.assertNotIn("uncategorized live", panel._browser.toPlainText())
            self.assertIn("library live", panel._browser.toPlainText())
            panel.apply_theme("light")
            panel._filter_combo.setCurrentText("🔌 USB Devices")
            self.assertIn("usb live", panel._browser.toPlainText())
            self.assertIn("device persisted", panel._browser.toPlainText())
            self.assertNotIn("library live", panel._browser.toPlainText())
            panel._append_notification_card({"category": "board_install", "type": "error", "message": "board error live"})
            panel._filter_combo.setCurrentText("✖ Errors")
            self.assertIn("error persisted", panel._browser.toPlainText())
            self.assertIn("board error live", panel._browser.toPlainText())
            self.assertNotIn("usb live", panel._browser.toPlainText())
            panel._filter_combo.setCurrentText("All")
            self.assertIn("uncategorized live", panel._browser.toPlainText())
            self.assertEqual(load.call_count, 1)

    def test_notification_clear_failure_preserves_history_and_reports_the_error(self):
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[{"category": "board_install", "message": "preserved history"}]):
            panel = self.widget(NotifPanel())
        panel._filter_combo.setCurrentText("📦 Boards & Libraries")
        for outcome in (False, OSError("Read-only database")):
            with patch("main.qt.notif_panel.dbs_delete.clear_all_notifications", return_value=outcome if outcome is False else None,
                       side_effect=outcome if isinstance(outcome, Exception) else None):
                panel._clear_notifications()
            self.assertIn("preserved history", panel._browser.toPlainText())
            self.assertIn("Notifications could not be cleared", panel._browser.toPlainText())
            self.assertNotIn("All notifications cleared", panel._browser.toPlainText())
            self.assertEqual(len(panel._records), 1)
            self.assertEqual(panel._filter_combo.currentText(), "📦 Boards & Libraries")
            for mode in MODES:
                panel.apply_theme(mode)
                self.assert_readable(rendered_color(panel._browser, "Notifications could not be cleared"), get_palette(mode)["BG_DARK"])
        with patch("main.qt.notif_panel.dbs_delete.clear_all_notifications", return_value=True):
            panel._clear_notifications()
        self.assertEqual(len(panel._records), 0)
        self.assertEqual(panel._browser.toPlainText(), "All notifications cleared.")
        for mode in MODES:
            panel.apply_theme(mode)
            self.assertEqual(panel._browser.toPlainText(), "All notifications cleared.")

    def test_notification_record_memory_is_bounded_and_discards_unused_details(self):
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[]):
            panel = self.widget(NotifPanel())
        for index in range(550):
            panel._append_notification_card({"message": f"bounded notification {index}", "details": {"unused": "x" * 100_000}})
        self.assertLessEqual(len(panel._records), 500)
        self.assertLessEqual(panel._records.chars, 256_000)
        self.assertTrue(all("details" not in record for record in panel._records))
        self.assertLessEqual(len(panel._visible_records()), 150)

    def test_project_change_reloads_only_new_project_history_and_retains_filter(self):
        first = [{"category": "library_install", "message": "first project notification"}]
        second = [{"category": "library_install", "message": "second project notification"}]
        first_path = str(ROOT / "temp" / "audit" / "notifications-first")
        second_path = str(ROOT / "temp" / "audit" / "notifications-second")
        with patch("main.qt.notif_panel.dbs_read.get_notifications", side_effect=[first, second]) as load:
            panel = self.widget(NotifPanel(SimpleNamespace(sketch_dir_path=first_path)))
            panel._filter_combo.setCurrentText("📦 Boards & Libraries")
            panel._append_notification_card({"category": "library_install", "message": "first project live"})
            panel._on_project_updated({"path": first_path})
            self.assertEqual(load.call_count, 1)
            panel._on_project_updated({"path": second_path})
            APP.processEvents()
            self.assertEqual(load.call_count, 2)
            self.assertEqual(panel._filter_combo.currentText(), "📦 Boards & Libraries")
            self.assertIn("second project notification", panel._browser.toPlainText())
            self.assertNotIn("first project notification", panel._browser.toPlainText())
            self.assertNotIn("first project live", panel._browser.toPlainText())
            panel.apply_theme("light")
            self.assertEqual(load.call_count, 2)

    def test_external_refresh_merges_new_persisted_events_without_losing_failed_live_writes(self):
        base = {"id": "base", "category": "system", "level": "warning", "title": "Warning", "message": "Repeated warning"}
        persisted_live = {"id": "base", "category": "system", "level": "info", "title": "Live", "message": "Persisted live message"}
        external = {"id": "external", "category": "library_install", "level": "success", "message": "External installation"}
        with patch("main.qt.notif_panel.dbs_read.get_notifications", side_effect=[[base], [external, persisted_live, base], [external, persisted_live, base]]) as load:
            panel = self.widget(NotifPanel())
            panel._append_notification_card({"category": "system", "type": "warning", "title": "Warning", "message": "Repeated warning"})
            panel._append_notification_card({"id": "base", "category": "system", "type": "info", "title": "Live", "message": "Persisted live message"})
            panel._on_tab_revealed()
            self.assertNotIn("External installation", panel._browser.toPlainText())
            APP.processEvents()
            self.assertIn("External installation", panel._browser.toPlainText())
            self.assertEqual(panel._browser.toPlainText().count("Persisted live message"), 1)
            self.assertEqual(panel._browser.toPlainText().count("Repeated warning"), 2)
            panel._on_tab_revealed()
            APP.processEvents()
            self.assertEqual(panel._browser.toPlainText().count("Persisted live message"), 1)
            self.assertEqual(panel._browser.toPlainText().count("Repeated warning"), 2)
            self.assertEqual(load.call_count, 3)
            self.assertTrue(all(call.kwargs["db_path"].startswith(str(ROOT / "temp")) for call in load.call_args_list))

    def test_failed_live_write_before_initial_read_does_not_match_historical_text(self):
        jobs = []
        old = {"id": "historical", "category": "system", "level": "warning", "title": "Warning", "message": "Repeated initial warning"}
        with patch("main.qt.notif_panel.threading.Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=lambda: jobs.append(target))), \
                patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[old]):
            panel = self.widget(NotifPanel())
            panel._append_notification_card({"id": "failed-write", "type": "warning", "title": "Warning", "message": "Repeated initial warning"})
            jobs[0]()
            APP.processEvents()
            self.assertEqual(panel._browser.toPlainText().count("Repeated initial warning"), 2)
            self.assertEqual([record["id"] for record in panel._pending_live], ["failed-write"])

    def test_a_live_signal_already_discovered_in_database_is_not_duplicated(self):
        event = {"id": "already-stored", "category": "system", "level": "info", "title": "Notification", "message": "Already discovered event"}
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[event]):
            panel = self.widget(NotifPanel())
        panel._append_notification_card({"id": "already-stored", "type": "info", "message": "Already discovered event"})
        self.assertEqual(panel._browser.toPlainText().count("Already discovered event"), 1)
        self.assertEqual(len(panel._pending_live), 0)

    def test_settings_history_message_matches_persistence_while_status_stays_concise(self):
        event = {"id": "settings-history", "category": "system", "level": "success", "title": "Preferences", "message": "Detailed saved preference values"}
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[]):
            panel = self.widget(NotifPanel())
        panel._append_notification_card({"id": "settings-history", "type": "success", "title": "Preferences",
                                         "message": "Preferences saved", "history_message": "Detailed saved preference values"})
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[event]):
            panel._on_tab_revealed()
            APP.processEvents()
        self.assertEqual(panel._browser.toPlainText().count("Detailed saved preference values"), 1)
        self.assertEqual(len(panel._pending_live), 0)

    def test_failed_reader_start_releases_refresh_and_can_retry(self):
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[]):
            panel = self.widget(NotifPanel())
        with patch("main.qt.notif_panel.threading.Thread", side_effect=RuntimeError("Reader unavailable")):
            panel._on_tab_revealed()
        self.assertFalse(panel._refresh_running)
        self.assertIn("Notifications could not be refreshed", panel._browser.toPlainText())
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[{"id": "retry", "message": "Reader retry succeeded"}]):
            panel._on_tab_revealed()
            APP.processEvents()
        self.assertIn("Reader retry succeeded", panel._browser.toPlainText())
        self.assertNotIn("Notifications could not be refreshed", panel._browser.toPlainText())

    def test_stale_project_refresh_is_discarded_and_latest_refresh_is_coalesced(self):
        jobs = []
        schedule = lambda target, **kwargs: SimpleNamespace(start=lambda: jobs.append(target))
        first_path = str(ROOT / "temp" / "audit" / "async-notifications-first")
        second_path = str(ROOT / "temp" / "audit" / "async-notifications-second")
        with patch("main.qt.notif_panel.threading.Thread", side_effect=schedule), \
                patch("main.qt.notif_panel.dbs_read.get_notifications", side_effect=[
                    [{"id": "old", "message": "old project database"}],
                    [{"id": "new", "message": "new project database"}],
                ]) as load:
            panel = self.widget(NotifPanel(SimpleNamespace(sketch_dir_path=first_path)))
            panel._on_tab_revealed()
            panel._on_tab_revealed()
            self.assertEqual(len(jobs), 1)
            panel._on_project_updated({"path": second_path})
            jobs[0]()
            APP.processEvents()
            self.assertNotIn("old project database", panel._browser.toPlainText())
            self.assertEqual(len(jobs), 2)
            panel._append_notification_card({"type": "warning", "message": "new project live warning"})
            jobs[1]()
            APP.processEvents()
            self.assertIn("new project database", panel._browser.toPlainText())
            self.assertIn("new project live warning", panel._browser.toPlainText())
            self.assertEqual(load.call_count, 2)
            self.assertEqual(Path(load.call_args_list[0].kwargs["db_path"]), Path(first_path) / ".mcu_flasher_build_cache" / "dbs_notif.json")
            self.assertEqual(Path(load.call_args_list[1].kwargs["db_path"]), Path(second_path) / ".mcu_flasher_build_cache" / "dbs_notif.json")

    def test_clearing_notifications_invalidates_an_older_inflight_refresh(self):
        records = [{"id": "old", "message": "cleared database history"}]
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=records):
            panel = self.widget(NotifPanel())
            jobs = []
            with patch("main.qt.notif_panel.threading.Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=lambda: jobs.append(target))):
                panel._on_tab_revealed()
                with patch("main.qt.notif_panel.dbs_delete.clear_all_notifications", return_value=True):
                    panel._clear_notifications()
                jobs[0]()
                APP.processEvents()
            self.assertEqual(panel._browser.toPlainText(), "All notifications cleared.")
            self.assertEqual(len(panel._records), 0)

    def test_notification_database_reads_run_off_the_qt_thread(self):
        done = threading.Event()
        reader_threads = []
        main_thread = threading.get_ident()
        def schedule(target, **kwargs):
            def run():
                target()
                done.set()
            return self.real_thread_class(target=run, daemon=True)
        def read(**kwargs):
            reader_threads.append(threading.get_ident())
            return [{"id": "worker", "message": "background reader fixture"}]
        with patch("main.qt.notif_panel.threading.Thread", side_effect=schedule), \
                patch("main.qt.notif_panel.dbs_read.get_notifications", side_effect=read):
            panel = self.widget(NotifPanel())
            self.assertTrue(done.wait(2))
            APP.processEvents()
            self.assertIn("background reader fixture", panel._browser.toPlainText())
            self.assertTrue(reader_threads)
            self.assertTrue(all(identifier != main_thread for identifier in reader_threads))

    def test_native_terminal_payloads_match_and_cover_all_ansi_foregrounds(self):
        for mode in MODES:
            captured = []
            view = SimpleNamespace(page=lambda: SimpleNamespace(runJavaScript=captured.append))
            probe = SimpleNamespace(_theme=mode, _font_size=18)
            PosixTerminalPanel._configure(probe, view)
            source = captured.pop()
            payload, _ = json.JSONDecoder().raw_decode(source.split("window.configureTerminal?.(", 1)[1])
            self.assertEqual(payload, TerminalPanel._build_terminal_theme_payload(None, mode))
            for name in ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"):
                self.assert_readable(payload[name], payload["background"])
                self.assert_readable(payload["bright" + name.title()], payload["background"])
            self.assert_readable(payload["foreground"], payload["background"])
            self.assert_readable(payload["selectionForeground"], payload["selectionBackground"])
            self.assertIn(", 18,", source)

    def test_theme_dispatch_can_be_owned_once_by_main_window(self):
        class Signal:
            def __init__(self):
                self.connected = []
            def connect(self, handler):
                self.connected.append(handler)
        for cls in (CompatPanel, SyntaxPanel, NotifPanel, TerminalPanel, PosixTerminalPanel):
            bus = SimpleNamespace(**{name: Signal() for name in ("theme_changed", "font_size_changed", "compat_devices_updated", "syntax_errors", "notification", "project_updated")})
            probe = SimpleNamespace(**{name: (lambda *args: None) for name in ("apply_theme", "set_font_size", "_on_compat_updated", "_on_syntax_diagnostics", "_on_live_notification", "_on_project_updated")})
            cls.connect_signals(probe, bus, connect_theme=False)
            self.assertEqual(bus.theme_changed.connected, [])
            cls.connect_signals(probe, bus)
            self.assertEqual(len(bus.theme_changed.connected), 1)

    def test_plain_and_rich_log_selection_remains_readable_when_unfocused(self):
        compat = self.widget(CompatPanel())
        with patch("main.qt.notif_panel.dbs_read.get_notifications", return_value=[]):
            notifications = self.widget(NotifPanel())
        for mode in MODES:
            for panel, view in ((compat, compat._output), (notifications, notifications._browser)):
                panel.apply_theme(mode)
                view.ensurePolished()
                for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
                    foreground = view.palette().color(group, QPalette.ColorRole.HighlightedText).name()
                    background = view.palette().color(group, QPalette.ColorRole.Highlight).name()
                    self.assert_readable(foreground, background)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PanelReadabilityChecks))
    raise SystemExit(not result.wasSuccessful())
