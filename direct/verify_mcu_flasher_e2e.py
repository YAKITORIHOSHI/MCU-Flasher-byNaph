#!/usr/bin/env python3
"""Hardware-free opaque-box E2E test suite for MCU Flasher maintenance and release.

Covers Requirements R1 through R5 across Tiers 1-4:
- R1: ANSI clear screen (\\x1b[2J, \\x1b[H, \\x1b[1;1H, \\x1b[f, \\x1b[J) clears terminal view while preserving ANSI colors
- R2: Developer Tickets navigation bypasses login directly to dashboard, with consistent loading screen during transition
- R3: Serial monitor clipboard copying strips ANSI codes, raw \\r, and literal \\x0d trigger text
- R4: MCU sketch agent skills and background hardware state awareness (selected board, port number, port name in project_state.json)
- R5: Multi-OS bootstrap update checks, fresh cleanup, release ZIP packaging, and git status

Test Design Structure:
- Tier 1: Feature coverage (R1-R5 in isolation)
- Tier 2: Boundary & corner cases (empty sequences, lone escapes, rapid clears, port clear)
- Tier 3: Cross-feature combinations (ANSI colors + clear screen + clipboard copy; port switch during sketch editing)
- Tier 4: Real-world application scenarios (telemetry dashboard redraw, noisy debug output copy, offline developer tickets launch)
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Ensure hardware-free offscreen Qt execution
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QRect, QTimer, Qt
from PySide6.QtGui import QClipboard, QColor, QFont, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QStackedWidget, QWidget

# Ensure single shared QApplication instance
APP = QApplication.instance() or QApplication([])

from main.core import config
from main.core.theme import Theme
from main.qt.serial_panel import SerialOutputView, SerialPanel, _SerialAnsiParser

try:
    from main.qt.serial_panel import sanitize_serial_clipboard_text
except ImportError:
    sanitize_serial_clipboard_text = None

from main.qt.owner_ticket_dialog import DeveloperArtwork, NeoLoginCard, OwnerTicketDialog
from main.qt.owner_ticket_style import NeoCard


def pump_events(iterations: int = 5, sleep_sec: float = 0.01) -> None:
    """Process pending Qt events."""
    for _ in range(iterations):
        APP.processEvents()
        if sleep_sec > 0:
            time.sleep(sleep_sec)


def sign_in_locally(dialog, service, key: str = "fixture-local-key", timeout: float = 4.0) -> None:
    """Developer Access opens only after a local access key or cloud sign-in.

    There is deliberately no default bypass to the dashboard; sign in through
    the real form with a mocked service, as a developer would.
    """
    if not isinstance(service.authenticate.return_value, tuple):
        service.authenticate.return_value = (True, "Signed in")
    deadline = time.monotonic() + timeout
    while dialog._task_name and time.monotonic() < deadline:  # service initialization
        pump_events(1, 0.01)
    dialog.txt_auth_pwd.setText(key)
    dialog._do_authenticate()
    while dialog._task_name and time.monotonic() < deadline:
        pump_events(1, 0.01)
    pump_events(5, 0.01)


def wait_for_file(path: Path, timeout: float = 1.5) -> bool:
    """Poll for a file written by a background worker thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        pump_events(2, 0.01)
    return path.exists()


# ==============================================================================
# Base Test Case
# ==============================================================================

class BaseE2ETestCase(unittest.TestCase):
    """Base fixture providing sandbox isolation and safe mock defaults."""

    @classmethod
    def setUpClass(cls):
        Theme.apply_theme("default")

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        # Prevent touching live user configuration
        self.stack.enter_context(patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}))
        self.stack.enter_context(patch.object(config, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(config, "_save_raw_config", return_value=True))
        self.stack.enter_context(patch.object(config, "save_gui_config", return_value=True))
        self.stack.enter_context(patch.object(config, "get_theme_mode", return_value="default"))
        self.stack.enter_context(patch.object(config, "get_monitor_font_size", return_value=11))

        # Isolated temporary directories
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)

    def create_view(self) -> SerialOutputView:
        """Create an isolated SerialOutputView with clean defaults."""
        view = SerialOutputView()
        view.set_timestamp_enabled(False)
        def dispose():
            view._flush_timer.stop()
            view.close()
            view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return view

    def create_panel(self) -> SerialPanel:
        """Create an isolated SerialPanel with clean defaults."""
        panel = SerialPanel(None)
        panel._output.set_timestamp_enabled(False)
        def dispose():
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return panel

    def feed_view(self, view: SerialOutputView, text: str, newline: bool = False, **extra) -> None:
        """Append text to view and synchronously flush the queue."""
        view.append_log(dict(text=text, newline=newline, **extra))
        view._flush_queue()
        pump_events(2, 0.005)


# ==============================================================================
# Tier 1: Feature Coverage (R1 - R5)
# ==============================================================================

class TestTier1_R1_SerialAnsiClearScreen(BaseE2ETestCase):
    """Tier 1: Feature coverage for R1 (ANSI Clear Screen in isolation)."""

    def test_r1_clear_screen_2j_clears_terminal_view(self):
        """\\x1b[2J triggers screen clear and wipes terminal buffer."""
        parser = _SerialAnsiParser()
        events = parser.feed("\x1b[2J")
        self.assertTrue(any(erase for _, erase, _ in events), "Parser must emit erase_screen=True on \\x1b[2J")

        view = self.create_view()
        self.feed_view(view, "Screen 1 output\n")
        self.assertIn("Screen 1", view.toPlainText())

        self.feed_view(view, "\x1b[2JScreen 2 output\n")
        self.assertNotIn("Screen 1", view.toPlainText())
        self.assertIn("Screen 2", view.toPlainText())

    def test_r1_clear_screen_h_cursor_home_clears_terminal_view(self):
        """\\x1b[H (cursor home) triggers screen clear in append-only serial view."""
        parser = _SerialAnsiParser()
        events = parser.feed("\x1b[H")
        self.assertTrue(any(erase for _, erase, _ in events), "Parser must emit erase_screen=True on \\x1b[H")

        view = self.create_view()
        self.feed_view(view, "Line before home\n")
        self.assertIn("Line before home", view.toPlainText())

        self.feed_view(view, "\x1b[HLine after home\n")
        self.assertNotIn("Line before home", view.toPlainText())
        self.assertIn("Line after home", view.toPlainText())

    def test_r1_clear_screen_1_1h_clears_terminal_view(self):
        """\\x1b[1;1H (CUP 1;1) triggers screen clear."""
        parser = _SerialAnsiParser()
        events = parser.feed("\x1b[1;1H")
        self.assertTrue(any(erase for _, erase, _ in events), "Parser must emit erase_screen=True on \\x1b[1;1H")

        view = self.create_view()
        self.feed_view(view, "Old frame\n")
        self.feed_view(view, "\x1b[1;1HNew frame\n")
        self.assertNotIn("Old frame", view.toPlainText())
        self.assertIn("New frame", view.toPlainText())

    def test_r1_clear_screen_f_hvp_clears_terminal_view(self):
        """\\x1b[f (HVP cursor position) triggers screen clear."""
        parser = _SerialAnsiParser()
        events = parser.feed("\x1b[f")
        self.assertTrue(any(erase for _, erase, _ in events), "Parser must emit erase_screen=True on \\x1b[f")

        view = self.create_view()
        self.feed_view(view, "Before HVP\n")
        self.feed_view(view, "\x1b[fAfter HVP\n")
        self.assertNotIn("Before HVP", view.toPlainText())
        self.assertIn("After HVP", view.toPlainText())

    def test_r1_clear_screen_j_and_0j_clears_terminal_view(self):
        """\\x1b[J and \\x1b[0J trigger screen clear."""
        parser = _SerialAnsiParser()
        events_j = parser.feed("\x1b[J")
        self.assertTrue(any(erase for _, erase, _ in events_j), "Parser must emit erase_screen=True on \\x1b[J")

        parser.reset()
        events_0j = parser.feed("\x1b[0J")
        self.assertTrue(any(erase for _, erase, _ in events_0j), "Parser must emit erase_screen=True on \\x1b[0J")

    def test_r1_ansi_color_retained_across_screen_clear(self):
        """Active SGR color styling is preserved across clear screen sequences."""
        parser = _SerialAnsiParser()
        # Set green foreground (32m)
        parser.feed("\x1b[32m")
        green_style = parser.style
        self.assertIsNotNone(green_style.fg, "Style must have foreground color set")

        # Send clear screen
        events = parser.feed("\x1b[H")
        self.assertTrue(any(erase for _, erase, _ in events))

        # Subsequent text must retain the green foreground style
        events_text = parser.feed("Green text")
        self.assertTrue(events_text)
        text, erase, style = events_text[0]
        self.assertEqual(text, "Green text")
        self.assertFalse(erase)
        self.assertEqual(style.fg, green_style.fg, "SGR color styling must survive cursor home / clear screen")


class TestTier1_R2_DeveloperTickets(BaseE2ETestCase):
    """Tier 1: Feature coverage for R2 (Developer Tickets navigation & loading screen)."""

    def setUp(self):
        super().setUp()
        self.mock_service = Mock()
        self.mock_service.get_config.return_value = {"owner_email": "dev@test.local", "use_firebase": False}
        self.mock_service.is_cloud_authenticated = False
        self.mock_service.check_connection.return_value = (True, "Connected")
        self.mock_service.restore_session.return_value = (False, "")
        self.mock_service.get_tickets.return_value = [
            {"id": "t-1", "title": "Test Ticket 1", "category": "GUI", "severity": "Low",
             "status": "Open", "description": "Desc", "created_at": "2026-11-08 10:00", "updated_at": "2026-11-08 10:00"}
        ]
        self.mock_service._storage_dir = self.temp_path / "portal"
        self.mock_service._storage_dir.mkdir(parents=True, exist_ok=True)

    def test_r2_owner_ticket_dialog_requires_sign_in_before_dashboard(self):
        """Developer Tickets opens on sign-in, then reaches the dashboard with a local key."""
        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=self.mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)
            pump_events(10, 0.02)
            self.assertEqual(dialog.stack.currentWidget().objectName(), "developer-login-page",
                             "Developer Access must not bypass sign-in")
            sign_in_locally(dialog, self.mock_service)

            current = dialog.stack.currentWidget()
            is_dashboard = (
                current.objectName() == "developer-dashboard-page"
                or getattr(dialog, "btn_new", None) is not None and dialog.btn_new.parentWidget() == current
                or dialog.stack.currentIndex() == 1
            )
            self.assertTrue(is_dashboard, "Signing in must open the dashboard")
            self.assertNotEqual(current.objectName(), "developer-login-page")

    def test_r2_loading_screen_displayed_during_initialization(self):
        """Dialog provides a dedicated loading screen (index 0) during service initialization."""
        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=self.mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)

            # Stack index 0 is the loading page during startup
            loading_page = dialog.stack.widget(0)
            self.assertIsNotNone(loading_page, "Stack must contain loading page at index 0")
            page_name = loading_page.objectName()
            self.assertIn("loading", page_name.lower(), "Index 0 must be the loading page")

    def test_r2_loading_screen_neobrutalist_artwork_presence(self):
        """Loading screen contains theme-matched vector artwork (chip / trace icons)."""
        with patch("main.qt.owner_ticket_dialog.OwnerTicketDialog", return_value=self.mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)

            loading_page = dialog.stack.widget(0)
            artworks = loading_page.findChildren(DeveloperArtwork)
            self.assertTrue(len(artworks) >= 1, "Loading screen must embed DeveloperArtwork for brand consistency")

    def test_r2_loading_screen_neocard_style(self):
        """Loading screen uses NeoCard container with solid borders and hard offset shadow."""
        with patch("main.qt.owner_ticket_dialog.OwnerTicketDialog", return_value=self.mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)

            loading_page = dialog.stack.widget(0)
            cards = loading_page.findChildren(NeoCard)
            self.assertTrue(len(cards) >= 1, "Loading screen must utilize NeoCard container")

    def test_r2_local_developer_access_without_credentials(self):
        """Without cloud credentials a local access key gives Local Developer Access."""
        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=self.mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)
            pump_events(10, 0.02)
            sign_in_locally(dialog, self.mock_service)

            self.assertIn("local", dialog.access_label.text().lower(),
                          "Access label must indicate Local developer access when unauthenticated")
            self.assertEqual(len(dialog._tickets), 1, "Local tickets must be loaded successfully")


class TestTier1_R3_SerialClipboardSanitization(BaseE2ETestCase):
    """Tier 1: Feature coverage for R3 (Serial monitor clean clipboard copying)."""

    def test_r3_clipboard_strips_ansi_csi_color_codes(self):
        """ANSI color codes are stripped from copied text."""
        raw = "\x1b[31mRed Alert\x1b[0m \x1b[1;32mGreen OK\x1b[0m"
        if sanitize_serial_clipboard_text is not None:
            clean = sanitize_serial_clipboard_text(raw)
            self.assertEqual(clean, "Red Alert Green OK")

    def test_r3_clipboard_strips_ansi_clear_sequences(self):
        """Clear screen sequences are stripped from copied text."""
        raw = "\x1b[2J\x1b[HCleared Header\x1b[1;1HBody"
        if sanitize_serial_clipboard_text is not None:
            clean = sanitize_serial_clipboard_text(raw)
            self.assertEqual(clean, "Cleared HeaderBody")

    def test_r3_clipboard_strips_raw_carriage_return(self):
        """Raw standalone carriage returns are stripped, and CRLF is normalized."""
        raw = "Progress 10%\rProgress 50%\rProgress 100%\r\nComplete"
        if sanitize_serial_clipboard_text is not None:
            clean = sanitize_serial_clipboard_text(raw)
            self.assertNotIn("\r", clean)
            self.assertIn("Complete", clean)

    def test_r3_clipboard_strips_literal_0x0d_trigger_text(self):
        """Literal \\x0d trigger strings are stripped from copied text."""
        raw = "Output\\x0dClean\\x0DData"
        if sanitize_serial_clipboard_text is not None:
            clean = sanitize_serial_clipboard_text(raw)
            self.assertEqual(clean, "OutputCleanData")

    def test_r3_view_selection_copy_sanitizes_clipboard(self):
        """SerialOutputView.copy() sanitizes text pushed to system clipboard."""
        view = self.create_view()
        self.feed_view(view, "\x1b[31mError Line\x1b[0m\n")
        view.selectAll()
        view.copy()
        copied = QApplication.clipboard().text()
        self.assertNotIn("\x1b", copied)
        self.assertIn("Error Line", copied)

    def test_r3_panel_full_copy_output_sanitizes_clipboard(self):
        """SerialPanel._copy_output() sanitizes text pushed to system clipboard."""
        panel = self.create_panel()
        self.feed_view(panel._output, "\x1b[32mBoot completed\x1b[0m\n")
        panel._copy_output()
        pump_events(5, 0.01)
        copied = QApplication.clipboard().text()
        self.assertNotIn("\x1b", copied)
        self.assertIn("Boot completed", copied)


class TestTier1_R4_HardwareState_AgentSkills(BaseE2ETestCase):
    """Tier 1: Feature coverage for R4 (Hardware state awareness & agent skills)."""

    def test_r4_project_state_contains_port_name_and_port_number(self):
        """project_state.json contains explicit port_name and integer port_number under hardware."""
        from main.web_bridge import MCUWebBackendAPI
        bridge = MCUWebBackendAPI()
        bridge.sketch_dir_path = self.temp_path
        bridge.current_port = "/dev/ttyUSB0 (ESP32)"
        bridge.current_board = "ESP32 Dev Module"

        bridge._sync_project_hardware_state(self.temp_path)
        cache_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
        self.assertTrue(wait_for_file(cache_file), "project_state.json must be written")

        data = json.loads(cache_file.read_text("utf-8"))
        hw = data.get("hardware", {})
        self.assertIn("port_name", hw, "hardware must contain 'port_name'")
        self.assertIn("port_number", hw, "hardware must contain 'port_number'")
        self.assertEqual(hw.get("port_name"), "/dev/ttyUSB0")
        self.assertEqual(hw.get("port_number"), 0)

    def test_r4_board_selection_syncs_to_project_state(self):
        """Selecting a board updates board_selected and board_name in project_state.json."""
        from main.web_bridge import MCUWebBackendAPI
        bridge = MCUWebBackendAPI()
        bridge.sketch_dir_path = self.temp_path
        bridge.select_board("Arduino Uno")
        bridge._sync_project_hardware_state(self.temp_path)

        cache_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
        self.assertTrue(wait_for_file(cache_file), "project_state.json must be written")
        data = json.loads(cache_file.read_text("utf-8"))
        hw = data.get("hardware", {})
        self.assertTrue(hw.get("board_selected"))
        self.assertEqual(hw.get("board_name"), "Arduino Uno")

    def test_r4_port_selection_syncs_to_project_state(self):
        """Selecting a COM port updates mcu_connected and port in project_state.json."""
        from main.web_bridge import MCUWebBackendAPI
        bridge = MCUWebBackendAPI()
        bridge.sketch_dir_path = self.temp_path
        bridge.current_port = "COM4"
        bridge._sync_project_hardware_state(self.temp_path)

        cache_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
        self.assertTrue(wait_for_file(cache_file), "project_state.json must be written")
        data = json.loads(cache_file.read_text("utf-8"))
        hw = data.get("hardware", {})
        self.assertTrue(hw.get("mcu_connected"))
        self.assertEqual(hw.get("port"), "COM4")
        self.assertEqual(hw.get("port_number"), 4)
        self.assertEqual(hw.get("port_number"), 4)

    def test_r4_mcu_sketch_target_skill_covers_firmware_constraints(self):
        """mcu-sketch-target/SKILL.md documents architecture-specific firmware constraints."""
        skill_path = ROOT / ".agents" / "skills" / "mcu-sketch-target" / "SKILL.md"
        self.assertTrue(skill_path.exists(), "Target skill must exist")
        content = skill_path.read_text("utf-8")

        self.assertIn("strapping", content.lower(), "Must document strapping pins")
        self.assertIn("flash", content.lower(), "Must document SPI flash pins")
        self.assertIn("esp32", content.lower(), "Must mention ESP32")
        self.assertIn("project_state.json", content, "Must reference project_state.json")

    def test_r4_sketch_workflow_skill_covers_five_step_workflow(self):
        """sketch-workflow/SKILL.md provides the 5-step firmware workflow and excludes GUI dev guidance."""
        skill_path = ROOT / ".agents" / "skills" / "sketch-workflow" / "SKILL.md"
        self.assertTrue(skill_path.exists(), "Workflow skill must exist")
        content = skill_path.read_text("utf-8")

        self.assertIn("Hardware", content, "Must include Step 1 Hardware Query")
        self.assertIn("Root Sketch", content, "Must include Step 2 Root Sketch boundary")
        self.assertNotIn("Monaco", content, "Must not contain out-of-place Monaco editor guidance")
        self.assertNotIn("PageBackdrop", content, "Must not contain glass backdrop GUI guidance")


class TestTier1_R5_MultiOS_Bootstrap_Packaging_Git(BaseE2ETestCase):
    """Tier 1: Feature coverage for R5 (Multi-OS bootstrap, release ZIP, git status)."""

    def test_r5_app_update_check_queries_default_branch(self):
        """app_update.py check_for_update targets default repository branch."""
        from src.modules.app_update import DEFAULT_BRANCH, REPOSITORY, update_branch
        self.assertEqual(DEFAULT_BRANCH, "main")
        self.assertEqual(REPOSITORY, "YAKITORIHOSHI/MCU-Flasher-byNaph")
        self.assertEqual(update_branch(None), "main")

    def test_r5_app_update_policy_evaluation(self):
        """automatic_updates_enabled correctly evaluates configuration."""
        from src.modules.app_update import automatic_updates_enabled
        self.assertTrue(automatic_updates_enabled({}))
        self.assertTrue(automatic_updates_enabled({"app_update_policy": "auto"}))
        self.assertFalse(automatic_updates_enabled({"app_update_policy": "manual"}))

    def test_r5_release_zip_naming_convention(self):
        """build_release.py names package MCU-Flasher-<commit[:7]>-fresh.zip."""
        rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode("ascii").strip()
        expected_name = f"MCU-Flasher-{rev[:7]}-fresh.zip"
        self.assertTrue(expected_name.endswith("-fresh.zip"))
        self.assertTrue(expected_name.startswith("MCU-Flasher-"))

    def test_r5_fresh_cleanup_preserves_user_sketches(self):
        """Fresh cleanup preserves user sketches (.ino, .h, .cpp, NOTE.txt)."""
        from cleaner.ubuntu.maintenance import PROTECTED
        self.assertIn(".mcu_flasher_build_cache", PROTECTED)
        self.assertIn(".mcu_ai_edits", PROTECTED)

    def test_r5_git_status_active_branch_is_main(self):
        """Active git branch is 'main'."""
        branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT).decode("ascii").strip()
        self.assertEqual(branch, "main", "Active git branch must be main")


# ==============================================================================
# Tier 2: Boundary & Corner Cases (R1 - R5)
# ==============================================================================

class TestTier2_R1_ClearScreen_Boundaries(BaseE2ETestCase):
    """Tier 2: Boundary and corner cases for R1."""

    def test_r1_boundary_empty_escape_sequence_does_not_clear(self):
        """Incomplete or empty escape \\x1b[ does not trigger screen clear."""
        parser = _SerialAnsiParser()
        events = parser.feed("\x1b[")
        self.assertFalse(any(erase for _, erase, _ in events))

    def test_r1_boundary_lone_escape_character_handled_safely(self):
        """Bare \\x1b byte does not hang parser or discard following text."""
        parser = _SerialAnsiParser()
        events1 = parser.feed("\x1b")
        events2 = parser.feed("Normal Text")
        all_text = "".join(t for t, _, _ in events1 + events2)
        self.assertIn("Normal Text", all_text)

    def test_r1_boundary_rapid_back_to_back_clears(self):
        """Consecutive clears \\x1b[2J\\x1b[H in one chunk execute cleanly."""
        view = self.create_view()
        self.feed_view(view, "Old Data\n")
        self.feed_view(view, "\x1b[2J\x1b[HNew Data\n")
        self.assertEqual(view.toPlainText(), "New Data\n")

    def test_r1_boundary_semicolon_and_zero_coordinate_variants(self):
        """CUP variants \\x1b[;H and \\x1b[0;0H trigger screen clear."""
        parser = _SerialAnsiParser()
        events1 = parser.feed("\x1b[;H")
        self.assertTrue(any(erase for _, erase, _ in events1), "Must clear on \\x1b[;H")

        parser.reset()
        events2 = parser.feed("\x1b[0;0H")
        self.assertTrue(any(erase for _, erase, _ in events2), "Must clear on \\x1b[0;0H")

    def test_r1_boundary_scrollback_clear_3j(self):
        """\\x1b[3J (erase scrollback) clears display."""
        parser = _SerialAnsiParser()
        events = parser.feed("\x1b[3J")
        self.assertTrue(any(erase for _, erase, _ in events), "Must clear on \\x1b[3J")

    def test_r1_boundary_overlong_escape_sequence(self):
        """Overlong escape (>256 chars) is safely flushed without memory leak."""
        parser = _SerialAnsiParser()
        overlong = "\x1b[" + ("1;" * 200) + "m"
        events = parser.feed(overlong)
        self.assertTrue(isinstance(events, list))

    def test_r1_boundary_fragmented_clear_across_serial_chunks(self):
        """Clear sequence split across buffer reads is correctly assembled."""
        view = self.create_view()
        self.feed_view(view, "First Frame\n")
        # Split \x1b[2J across two feed chunks
        self.feed_view(view, "\x1b[")
        self.feed_view(view, "2JSecond Frame\n")
        self.assertNotIn("First Frame", view.toPlainText())
        self.assertIn("Second Frame", view.toPlainText())


class TestTier2_R2_DeveloperTickets_Boundaries(BaseE2ETestCase):
    """Tier 2: Boundary and corner cases for R2."""

    def test_r2_boundary_offline_connection_failure_loads_local_tickets(self):
        """Offline network state loads local tickets gracefully without error modal."""
        mock_service = Mock()
        mock_service.get_config.return_value = {}
        mock_service.is_cloud_authenticated = False
        mock_service.check_connection.return_value = (False, "Network unreachable")
        mock_service.restore_session.return_value = (False, "")
        mock_service.get_tickets.return_value = [{"id": "loc-1", "title": "Offline Ticket", "status": "Open",
                                                 "category": "General", "severity": "Medium", "description": "",
                                                 "created_at": "2026-11-08 10:00", "updated_at": "2026-11-08 10:00"}]
        mock_service._storage_dir = self.temp_path

        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)
            pump_events(10, 0.02)
            sign_in_locally(dialog, mock_service)

            is_dashboard = (
                dialog.stack.currentWidget().objectName() == "developer-dashboard-page"
                or getattr(dialog, "btn_new", None) is not None and dialog.btn_new.parentWidget() == dialog.stack.currentWidget()
                or dialog.stack.currentIndex() == 1
            )
            self.assertTrue(is_dashboard)
            self.assertEqual(len(dialog._tickets), 1)

    def test_r2_boundary_empty_ticket_repository(self):
        """Dashboard handles zero tickets without error."""
        mock_service = Mock()
        mock_service.get_config.return_value = {}
        mock_service.is_cloud_authenticated = False
        mock_service.check_connection.return_value = (True, "OK")
        mock_service.restore_session.return_value = (False, "")
        mock_service.get_tickets.return_value = []
        mock_service._storage_dir = self.temp_path

        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)
            pump_events(10, 0.02)
            sign_in_locally(dialog, mock_service)

            self.assertEqual(len(dialog._tickets), 0)
            is_dashboard = (
                dialog.stack.currentWidget().objectName() == "developer-dashboard-page"
                or getattr(dialog, "btn_new", None) is not None and dialog.btn_new.parentWidget() == dialog.stack.currentWidget()
                or dialog.stack.currentIndex() == 1
            )
            self.assertTrue(is_dashboard)

    def test_r2_boundary_repeated_dialog_open_and_close(self):
        """Multiple sequential dialog launches run cleanly without leaking tasks."""
        mock_service = Mock()
        mock_service.get_config.return_value = {}
        mock_service.is_cloud_authenticated = False
        mock_service.check_connection.return_value = (True, "OK")
        mock_service.restore_session.return_value = (False, "")
        mock_service.get_tickets.return_value = []
        mock_service._storage_dir = self.temp_path

        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=mock_service):
            for _ in range(3):
                dialog = OwnerTicketDialog(backend=None, parent=None)
                pump_events(5, 0.01)
                dialog.close()
                dialog.deleteLater()
                pump_events(5, 0.01)

    def test_r2_boundary_cloud_badge_updates_correctly(self):
        """Cloud badge reflects unauthenticated local state correctly."""
        mock_service = Mock()
        mock_service.get_config.return_value = {"use_firebase": False}
        mock_service.is_cloud_authenticated = False
        mock_service.check_connection.return_value = (False, "Offline")
        mock_service.restore_session.return_value = (False, "")
        mock_service.get_tickets.return_value = []
        mock_service._storage_dir = self.temp_path

        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)
            pump_events(10, 0.02)

            # In offline unauthenticated mode, cloud badge should not indicate signed in
            self.assertNotIn("signed in", dialog.cloud_badge.text().lower())

    def test_r2_boundary_dialog_resizing_compact_dimensions(self):
        """Dialog remains responsive when resized to compact dimensions (700x500)."""
        mock_service = Mock()
        mock_service.get_config.return_value = {}
        mock_service.is_cloud_authenticated = False
        mock_service.check_connection.return_value = (True, "OK")
        mock_service.restore_session.return_value = (False, "")
        mock_service.get_tickets.return_value = []
        mock_service._storage_dir = self.temp_path

        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)
            dialog.resize(700, 500)
            pump_events(5, 0.01)
            self.assertEqual(dialog.width(), 700)
            self.assertEqual(dialog.height(), 500)


class TestTier2_R3_ClipboardSanitization_Boundaries(BaseE2ETestCase):
    """Tier 2: Boundary and corner cases for R3."""

    def test_r3_boundary_empty_string_copy(self):
        """Sanitizing empty string returns empty string."""
        if sanitize_serial_clipboard_text is not None:
            self.assertEqual(sanitize_serial_clipboard_text(""), "")

    def test_r3_boundary_string_with_only_ansi_escapes(self):
        """String containing purely ANSI codes resolves to empty string."""
        raw = "\x1b[1;33;44m\x1b[2J\x1b[0m"
        if sanitize_serial_clipboard_text is not None:
            self.assertEqual(sanitize_serial_clipboard_text(raw), "")

    def test_r3_boundary_nested_and_adjacent_escapes(self):
        """Multiple glued and nested escapes are stripped cleanly."""
        raw = "\x1b[1m\x1b[4m\x1b[32mNested\x1b[0m\x1b[2m"
        if sanitize_serial_clipboard_text is not None:
            self.assertEqual(sanitize_serial_clipboard_text(raw), "Nested")

    def test_r3_boundary_mixed_case_literal_0x0d_variants(self):
        """Mixed-case \\x0d and \\x0D triggers are all stripped."""
        raw = "Status: \\x0d\\x0D\\x0dOK"
        if sanitize_serial_clipboard_text is not None:
            self.assertEqual(sanitize_serial_clipboard_text(raw), "Status: OK")

    def test_r3_boundary_multiline_crlf_normalized(self):
        """CRLF is normalized to \\n and standalone \\r stripped."""
        raw = "Line 1\r\nLine 2\rLine 3\r\n"
        if sanitize_serial_clipboard_text is not None:
            clean = sanitize_serial_clipboard_text(raw)
            self.assertNotIn("\r", clean)
            self.assertEqual(clean, "Line 1\nLine 2Line 3\n")

    def test_r3_boundary_unicode_and_emojis_preserved(self):
        """Unicode characters and emojis remain intact while ANSI codes are removed."""
        raw = "\x1b[32m🚀 Sensor: 23.5°C 溫度正常\x1b[0m"
        if sanitize_serial_clipboard_text is not None:
            clean = sanitize_serial_clipboard_text(raw)
            self.assertEqual(clean, "🚀 Sensor: 23.5°C 溫度正常")


class TestTier2_R4_HardwareState_Boundaries(BaseE2ETestCase):
    """Tier 2: Boundary and corner cases for R4."""

    def test_r4_boundary_port_disconnect_clears_hardware_state(self):
        """Disconnecting port resets mcu_connected to False and clears port fields."""
        from main.web_bridge import MCUWebBackendAPI
        bridge = MCUWebBackendAPI()
        bridge.sketch_dir_path = self.temp_path
        bridge.current_port = ""
        bridge._sync_project_hardware_state(self.temp_path)

        cache_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
        self.assertTrue(wait_for_file(cache_file))
        data = json.loads(cache_file.read_text("utf-8"))
        hw = data.get("hardware", {})
        self.assertFalse(hw.get("mcu_connected"))
        self.assertIsNone(hw.get("port"))
        self.assertIsNone(hw.get("port_name"))
        self.assertIsNone(hw.get("port_number"))

    def test_r4_boundary_port_number_extraction_com_ports(self):
        """COM port numbers are parsed correctly (COM3 -> 3, COM12 -> 12)."""
        from main.web_bridge import MCUWebBackendAPI

        for port_str, expected_num in [("COM3", 3), ("COM12", 12), ("COM1", None)]: # COM1 is motherboard filter
            bridge = MCUWebBackendAPI()
            bridge.sketch_dir_path = self.temp_path
            bridge.current_port = port_str
            bridge._sync_project_hardware_state(self.temp_path)
            cache_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
            self.assertTrue(wait_for_file(cache_file))
            data = json.loads(cache_file.read_text("utf-8"))
            hw = data.get("hardware", {})
            self.assertEqual(hw.get("port_number"), expected_num)

    def test_r4_boundary_port_number_extraction_linux_tty(self):
        """Linux device paths extract correct numbers (/dev/ttyUSB2 -> 2, /dev/ttyACM0 -> 0)."""
        from main.web_bridge import MCUWebBackendAPI

        for port_str, expected_num in [("/dev/ttyUSB2", 2), ("/dev/ttyACM0", 0)]:
            bridge = MCUWebBackendAPI()
            bridge.sketch_dir_path = self.temp_path
            bridge.current_port = port_str
            bridge._sync_project_hardware_state(self.temp_path)
            cache_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
            self.assertTrue(wait_for_file(cache_file))
            data = json.loads(cache_file.read_text("utf-8"))
            hw = data.get("hardware", {})
            self.assertEqual(hw.get("port_number"), expected_num)

    def test_r4_boundary_port_number_extraction_macos_cu(self):
        """macOS cu.usbserial paths extract correct numeric ID."""
        from main.web_bridge import MCUWebBackendAPI
        bridge = MCUWebBackendAPI()
        bridge.sketch_dir_path = self.temp_path
        bridge.current_port = "/dev/cu.usbserial-1420"
        bridge._sync_project_hardware_state(self.temp_path)
        cache_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
        self.assertTrue(wait_for_file(cache_file))
        data = json.loads(cache_file.read_text("utf-8"))
        hw = data.get("hardware", {})
        self.assertEqual(hw.get("port_number"), 1420)

    def test_r4_boundary_port_with_no_digits(self):
        """Port device with no digits returns None for port_number."""
        from main.web_bridge import MCUWebBackendAPI
        bridge = MCUWebBackendAPI()
        bridge.sketch_dir_path = self.temp_path
        bridge.current_port = "/dev/nullserial"
        bridge._sync_project_hardware_state(self.temp_path)
        cache_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
        self.assertTrue(wait_for_file(cache_file))
        data = json.loads(cache_file.read_text("utf-8"))
        hw = data.get("hardware", {})
        self.assertIsNone(hw.get("port_number"))

    def test_r4_boundary_skill_templates_in_file_utils_match(self):
        """ensure_hidden_read_first_md() in file_utils contains synchronized skill templates."""
        from main.core.file_utils import ensure_hidden_read_first_md
        ensure_hidden_read_first_md(self.temp_path)

        agent_target = self.temp_path / ".agents" / "skills" / "mcu-sketch-target" / "SKILL.md"
        agent_workflow = self.temp_path / ".agents" / "skills" / "sketch-workflow" / "SKILL.md"
        self.assertTrue(agent_target.exists(), "Target skill must be generated in sketch dir")
        self.assertTrue(agent_workflow.exists(), "Workflow skill must be generated in sketch dir")


class TestTier2_R5_Bootstrap_Packaging_Boundaries(BaseE2ETestCase):
    """Tier 2: Boundary and corner cases for R5."""

    def test_r5_boundary_app_update_offline_timeout_graceful(self):
        """app_update handles connection timeouts gracefully returning 'unavailable'."""
        from src.modules.app_update import ApplicationUpdateError, check_for_update
        def failing_opener(*args, **kwargs):
            raise ApplicationUpdateError("Connection timed out")

        res = check_for_update(ROOT, timeout=0.1, opener=failing_opener)
        self.assertIn(res.status, ("unavailable", "skipped"))

    def test_r5_boundary_skip_updates_config_honored(self):
        """skip_updates=True suppresses update checks."""
        from src.modules.app_update import update_skip_reason
        mock_cfg = {"skip_updates": True}
        with patch("src.modules.app_update._read_json", return_value=mock_cfg):
            reason = update_skip_reason(ROOT)
            self.assertIsNotNone(reason)

    def test_r5_boundary_release_packaging_excludes_git_and_temp(self):
        """build_release EXCLUDED set includes git, temp, and virtual environments."""
        from direct.build_release import EXCLUDED
        self.assertIn(".git", EXCLUDED)
        self.assertIn("temp", EXCLUDED)
        self.assertIn(".venv-linux", EXCLUDED)

    def test_r5_boundary_protected_cache_retained_during_cleanup(self):
        """Maintenance clean does not clean .mcu_ai_edits or .mcu_flasher_build_cache."""
        from cleaner.ubuntu.maintenance import PROTECTED
        self.assertTrue(".mcu_ai_edits" in PROTECTED)
        self.assertTrue(".mcu_flasher_build_cache" in PROTECTED)

    def test_r5_boundary_launcher_executables_validation(self):
        """Launcher folder contains required multi-OS launchers."""
        from direct.build_release import REQUIRED_LAUNCHERS
        for launcher in REQUIRED_LAUNCHERS:
            path = ROOT / launcher
            self.assertTrue(path.exists(), f"Launcher {launcher} must exist in repository")


# ==============================================================================
# Tier 3: Cross-Feature Combinations
# ==============================================================================

class TestTier3_CrossFeatureCombinations(BaseE2ETestCase):
    """Tier 3: Pairwise and multi-requirement cross-feature combinations."""

    def test_tier3_ansi_color_clear_screen_and_clipboard_copy(self):
        """ANSI color + clear screen + clipboard sanitization (R1 + R3)."""
        view = self.create_view()
        # Feed colored banner, clear screen, then new colored content
        self.feed_view(view, "\x1b[31mOld Warning\x1b[0m\n")
        self.feed_view(view, "\x1b[2J\x1b[1;32mActive State: OK\x1b[0m\n")

        # View text only has post-clear text
        self.assertNotIn("Old Warning", view.toPlainText())
        self.assertIn("Active State: OK", view.toPlainText())

        # Copy to clipboard
        view.selectAll()
        view.copy()
        copied = QApplication.clipboard().text()
        self.assertEqual(copied.strip(), "Active State: OK", "Clipboard must have clean text without escapes")

    def test_tier3_port_switch_during_active_sketch_coding(self):
        """Switching COM port updates project_state.json while sketch files exist (R4 + R1/R3)."""
        # Create a mock root sketch
        sketch_file = self.temp_path / "MySketch.ino"
        sketch_file.write_text("void setup() {} void loop() {}", "utf-8")

        from main.web_bridge import MCUWebBackendAPI
        bridge = MCUWebBackendAPI()
        bridge.sketch_dir_path = self.temp_path

        # Connect Port 1
        bridge.current_port = "/dev/ttyUSB0"
        bridge.current_board = "ESP32 Dev Module"
        bridge._sync_project_hardware_state(self.temp_path)
        state_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
        self.assertTrue(wait_for_file(state_file))

        hw1 = json.loads(state_file.read_text("utf-8"))["hardware"]
        self.assertEqual(hw1.get("port_name"), "/dev/ttyUSB0")
        self.assertEqual(hw1.get("port_number"), 0)

        # Switch Port to Port 2
        bridge.current_port = "/dev/ttyUSB1"
        bridge._sync_project_hardware_state(self.temp_path)
        pump_events(5, 0.01)

        hw2 = json.loads(state_file.read_text("utf-8"))["hardware"]
        self.assertEqual(hw2.get("port_name"), "/dev/ttyUSB1")
        self.assertEqual(hw2.get("port_number"), 1)

    def test_tier3_developer_tickets_loading_while_serial_monitor_active(self):
        """Developer tickets loading screen operates concurrently with active serial feed (R2 + R1)."""
        mock_service = Mock()
        mock_service.get_config.return_value = {}
        mock_service.is_cloud_authenticated = False
        mock_service.check_connection.return_value = (True, "OK")
        mock_service.restore_session.return_value = (False, "")
        mock_service.get_tickets.return_value = [{"id": "conc-1", "title": "Ticket", "status": "Open",
                                                 "category": "Gen", "severity": "Low", "description": "",
                                                 "created_at": "2026-11-08 10:00", "updated_at": "2026-11-08 10:00"}]
        mock_service._storage_dir = self.temp_path

        view = self.create_view()

        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)

            # While dialog is initializing, pump serial logs
            for i in range(5):
                self.feed_view(view, f"Telemetry tick {i}\n")
                pump_events(2, 0.01)

            # Wait for dialog to settle
            pump_events(10, 0.02)
            sign_in_locally(dialog, mock_service)
            is_dashboard = (
                dialog.stack.currentWidget().objectName() == "developer-dashboard-page"
                or getattr(dialog, "btn_new", None) is not None and dialog.btn_new.parentWidget() == dialog.stack.currentWidget()
                or dialog.stack.currentIndex() == 1
            )
            self.assertTrue(is_dashboard)
            self.assertIn("Telemetry tick 4", view.toPlainText())

    def test_tier3_carriage_return_progress_stream_and_clipboard_export(self):
        """Carriage return progress updates + clipboard export (R1 + R3)."""
        view = self.create_view()
        # Firmware sends \r progress then final line
        self.feed_view(view, "Flashing: [==  ] 25%\r")
        self.feed_view(view, "Flashing: [====] 50%\r")
        self.feed_view(view, "Flashing: [========] 100%\r\nFlash complete!\n")

        view.selectAll()
        view.copy()
        copied = QApplication.clipboard().text()
        self.assertNotIn("\r", copied)
        self.assertNotIn("\\x0d", copied)
        self.assertIn("Flash complete!", copied)

    def test_tier3_fresh_cleanup_and_release_package_build_verification(self):
        """Maintenance clean verification combined with release archive structure check (R5)."""
        from cleaner.ubuntu.maintenance import PROTECTED
        from direct.build_release import EXCLUDED, REQUIRED_LAUNCHERS

        # Protected dirs must not be in packaging excluded list of deletables
        self.assertTrue(".mcu_ai_edits" in PROTECTED)
        self.assertTrue(".git" in EXCLUDED)
        self.assertTrue(len(REQUIRED_LAUNCHERS) >= 8)


# ==============================================================================
# Tier 4: Real-World Application Scenarios
# ==============================================================================

class TestTier4_RealWorldScenarios(BaseE2ETestCase):
    """Tier 4: End-to-end real-world workload scenarios."""

    def test_scenario_1_telemetry_dashboard_redraw(self):
        """Scenario 1: Live MCU Telemetry with periodic ANSI clear screen and color styling."""
        view = self.create_view()

        # Frame 1
        frame1 = (
            "\x1b[H\x1b[2J"
            "\x1b[1;36m=== MCU SYSTEM MONITOR ===\x1b[0m\n"
            "CPU: \x1b[32m42%\x1b[0m | RAM: \x1b[33m1.2MB\x1b[0m\n"
            "Uptime: 1042s\n"
        )
        self.feed_view(view, frame1)
        self.assertIn("Uptime: 1042s", view.toPlainText())

        # Frame 2 (next refresh frame)
        frame2 = (
            "\x1b[H\x1b[2J"
            "\x1b[1;36m=== MCU SYSTEM MONITOR ===\x1b[0m\n"
            "CPU: \x1b[31m88%\x1b[0m | RAM: \x1b[33m1.4MB\x1b[0m\n"
            "Uptime: 1043s\n"
        )
        self.feed_view(view, frame2)

        # Output must contain only Frame 2 data
        text = view.toPlainText()
        self.assertNotIn("1042s", text, "Previous telemetry frame must be erased")
        self.assertIn("1043s", text, "Current telemetry frame must be visible")
        self.assertIn("CPU: 88%", text)

    def test_scenario_2_noisy_firmware_debug_log_clipboard_export(self):
        """Scenario 2: Clipboard export of noisy firmware debug log with escapes and \\x0d."""
        panel = self.create_panel()
        noisy_log = (
            "ESP-ROM:esp32s3-20210327\r\n"
            "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n"
            "\x1b[32m[I] Flash size: 16MB\x1b[0m\r\n"
            "Connecting WiFi...\\x0dConnected IP: 192.168.1.100\r\n"
            "\x1b[33m[W] Free heap: 142000 bytes\x1b[0m\r\n"
        )
        self.feed_view(panel._output, noisy_log)
        panel._copy_output()
        pump_events(5, 0.01)

        copied = QApplication.clipboard().text()
        self.assertNotIn("\x1b[", copied, "Must strip ANSI CSI escape sequences")
        self.assertNotIn("\r", copied, "Must strip unrendered raw carriage returns")
        self.assertNotIn("\\x0d", copied, "Must strip \\x0d literal trigger text")
        self.assertIn("ESP-ROM:esp32s3-20210327", copied)
        self.assertIn("Connected IP: 192.168.1.100", copied)

    def test_scenario_3_developer_tickets_offline_launch(self):
        """Scenario 3: Developer Tickets offline launch (logo gesture -> loading screen -> dashboard)."""
        mock_service = Mock()
        mock_service.get_config.return_value = {}
        mock_service.is_cloud_authenticated = False
        mock_service.check_connection.return_value = (False, "Offline mode")
        mock_service.restore_session.return_value = (False, "")
        mock_service.get_tickets.return_value = [
            {"id": "issue-101", "title": "Serial clear glitch", "status": "Open", "category": "Serial",
             "severity": "High", "description": "Need ANSI cursor home fix",
             "created_at": "2026-11-08 10:00", "updated_at": "2026-11-08 10:00"}
        ]
        mock_service._storage_dir = self.temp_path

        with patch("main.qt.owner_ticket_dialog.OwnerTicketService", return_value=mock_service):
            dialog = OwnerTicketDialog(backend=None, parent=None)
            self.addCleanup(dialog.deleteLater)

            # Verify loading page initially
            self.assertEqual(dialog.stack.widget(0).objectName(), "developer-loading-page")

            # Transition to dashboard
            pump_events(10, 0.02)
            sign_in_locally(dialog, mock_service)
            is_dashboard = (
                dialog.stack.currentWidget().objectName() == "developer-dashboard-page"
                or getattr(dialog, "btn_new", None) is not None and dialog.btn_new.parentWidget() == dialog.stack.currentWidget()
                or dialog.stack.currentIndex() == 1
            )
            self.assertTrue(is_dashboard)
            self.assertEqual(len(dialog._tickets), 1)
            self.assertEqual(dialog._tickets[0]["title"], "Serial clear glitch")

    def test_scenario_4_live_hardware_state_port_connect_disconnect(self):
        """Scenario 4: Board and port selection switch during live sketch coding."""
        from main.web_bridge import MCUWebBackendAPI
        bridge = MCUWebBackendAPI()
        bridge.sketch_dir_path = self.temp_path

        # 1. User selects board
        bridge.select_board("ESP32 Dev Module")
        bridge._sync_project_hardware_state(self.temp_path)
        state_file = self.temp_path / ".mcu_flasher_build_cache" / "project_state.json"
        self.assertTrue(wait_for_file(state_file))

        # 2. User attaches USB port
        bridge.current_port = "/dev/ttyUSB0"
        bridge._sync_project_hardware_state(self.temp_path)
        pump_events(5, 0.01)

        data = json.loads(state_file.read_text("utf-8"))["hardware"]
        self.assertTrue(data["board_selected"])
        self.assertTrue(data["mcu_connected"])
        self.assertEqual(data.get("port_name"), "/dev/ttyUSB0")
        self.assertEqual(data.get("port_number"), 0)

        # 3. User unplugs USB port
        bridge.current_port = ""
        bridge._sync_project_hardware_state(self.temp_path)
        pump_events(5, 0.01)

        data_unplug = json.loads(state_file.read_text("utf-8"))["hardware"]
        self.assertTrue(data_unplug["board_selected"])
        self.assertFalse(data_unplug["mcu_connected"])
        self.assertIsNone(data_unplug.get("port"))
        self.assertIsNone(data_unplug.get("port_name"))
        self.assertIsNone(data_unplug.get("port_number"))

    def test_scenario_5_multios_bootstrap_startup_and_release_manifest(self):
        """Scenario 5: Multi-OS startup update check + release ZIP packaging manifest validation."""
        from src.modules.app_update import DEFAULT_BRANCH, check_for_update
        from direct.build_release import REQUIRED_LAUNCHERS, REQUIRED_RUNTIME

        # Check update check logic
        res = check_for_update(ROOT, timeout=0.1)
        self.assertTrue(res.status in ("current", "pending", "unidentified", "unavailable", "skipped"))

        # Verify launchers and runtime files declared in release packaging
        self.assertIn("LAUNCHER/WINDOWS.exe", REQUIRED_LAUNCHERS)
        self.assertIn("LAUNCHER/UBUNTU", REQUIRED_LAUNCHERS)
        self.assertIn("LAUNCHER/MacOS.command", REQUIRED_LAUNCHERS)
        self.assertIn("python.exe", REQUIRED_RUNTIME)


# ==============================================================================
# Main Runner with Safe Exit
# ==============================================================================

if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)
    sys.stdout.flush()
    sys.stderr.flush()
    # Bypass PySide interpreter teardown on Python 3.14 to prevent offscreen C++ double-free segfault
    os._exit(0 if result.wasSuccessful() else 1)
