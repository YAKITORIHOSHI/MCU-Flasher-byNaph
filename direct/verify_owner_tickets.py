"""Hardware-free developer portal fixtures; never access live storage or Firebase."""
from __future__ import annotations

import argparse
import copy
import os
import sys
import time
import unittest
import warnings
from threading import Event
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
from PySide6.QtCore import QPoint, Qt, QThread
from PySide6.QtGui import QPalette, QTextCursor, QTextOption
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QMessageBox, QStyle, QStyleOptionComboBox
from main.core.theme import Theme
from main.qt import owner_ticket_dialog as portal
from main.qt.toolbar import PrimaryToolbar
from main.qt.owner_ticket_style import WrappedLabel, portal_colors
from main.core.log_colors import contrast_ratio
from main.qt.theme import build_stylesheet, register_fonts
from main.qt.garbage_collection import install_gui_garbage_collector

APP = QApplication.instance() or QApplication([])
COLLECTOR = install_gui_garbage_collector(APP)
register_fonts()
RENDER_DIR = None
TICKETS = [
    {"id": "fixture-1", "title": "Upload progress stays on Connecting after a board mismatch",
     "category": "PlatformIO / Toolchain", "severity": "Critical", "status": "Open",
     "description": "Selected ESP32-S3 while an ESP32 was attached. The upload stopped, but the progress row still asked for BOOT.",
     "created_at": "2026-10-09 10:12", "updated_at": "2026-10-09 10:12"},
    {"id": "fixture-2", "title": "Keep the serial reading position when changing themes",
     "category": "Serial Monitor", "severity": "Medium", "status": "In Progress",
     "description": "The selected text and scroll position should remain available when switching between smoked and frosted glass.",
     "created_at": "2026-10-08 16:40", "updated_at": "2026-10-09 09:30"},
    {"id": "fixture-3", "title": "Compact controls fit a shorter laptop screen",
     "category": "GUI / Interface", "severity": "Low", "status": "Resolved",
     "description": "Actions stay visible while the ticket notes scroll.",
     "created_at": "2026-10-08 14:20", "updated_at": "2026-10-09 08:10"},
]


def pump():
    for _ in range(4):
        APP.processEvents()
        time.sleep(.02)


def settle(widget, timeout=4000):
    deadline = time.monotonic() + timeout / 1000
    while widget._task_name and time.monotonic() < deadline:
        QTest.qWait(10)
    assert not widget._task_name, "A fixture service task did not complete"
    pump()


def show(widget, size=None):
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    if size:
        widget.resize(*size)
    widget.show()
    # Hidden native fixture windows need an explicit active focus scope.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        APP.setActiveWindow(widget)
    pump()


def capture(widget, name):
    if RENDER_DIR:
        assert widget.grab().save(str(RENDER_DIR / (name + ".png")))


class PortalChecks(unittest.TestCase):
    def setUp(self):
        Theme.apply_theme("default")
        APP.setStyleSheet(build_stylesheet("default"))
        self.tickets = copy.deepcopy(TICKETS)
        self.service = Mock()
        self.service.get_config.return_value = {"owner_email": "developer@example.com", "use_firebase": False}
        self.service.authenticate.return_value = (True, "Signed in")
        self.service.is_cloud_authenticated = False
        self.service.get_tickets.side_effect = lambda: copy.deepcopy(self.tickets)
        self.service.create_ticket.return_value = {"id": "created-fixture"}
        def update_ticket(ticket_id, updates):
            for ticket in self.tickets:
                if ticket["id"] == ticket_id:
                    ticket.update(updates)
            return True
        self.service.update_ticket.side_effect = update_ticket
        def delete_ticket(ticket_id):
            self.tickets[:] = [ticket for ticket in self.tickets if ticket["id"] != ticket_id]
            return True
        self.service.delete_ticket.side_effect = delete_ticket
        self.service.save_config.return_value = True
        self.service.test_firebase_connection.return_value = (True, "Connection verified in the fixture.")
        self.service.check_connection.return_value = (True, "Connected. Sign in to continue.")
        self.service.configure_local_access.return_value = (True, "Your local access key is saved.")
        patcher = patch.object(portal, "OwnerTicketService", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dialog = portal.OwnerTicketDialog()
        show(self.dialog)
        settle(self.dialog)
        self.addCleanup(self.dialog.close)
        self.addCleanup(self.dialog.deleteLater)

    def sign_in(self):
        self.dialog.txt_auth_pwd.setText("fixture-only")
        self.dialog._do_authenticate()
        settle(self.dialog)
        self.assertEqual(self.dialog.stack.currentIndex(), 1)

    def assert_scroll_width(self, scroll):
        viewport = scroll.viewport()
        self.assertLessEqual(scroll.widget().width(), viewport.width())
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)

    def assert_wrapped_text(self, widget, *, fully_visible=True):
        self.assertEqual(widget.wordWrapMode(), QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.assertEqual(widget.horizontalScrollBar().maximum(), 0)
        available = widget.viewport().width() - 2 * widget.document().documentMargin()
        block = widget.document().firstBlock()
        lines = 0
        while block.isValid():
            layout = block.layout()
            for index in range(layout.lineCount()):
                self.assertLessEqual(layout.lineAt(index).naturalTextWidth(), available + 1, widget.toPlainText()[:80])
                lines += 1
            block = block.next()
        self.assertGreater(lines, 0)
        if fully_visible:
            self.assertLessEqual(widget.document().size().height(), widget.viewport().height() + 1)
            self.assertEqual(widget.verticalScrollBar().maximum(), 0)

    def test_unbroken_ticket_content_cannot_expand_dashboard_or_clip_card_actions(self):
        literal = '  Serial.println("' + "A" * 4096 + '");\nhttps://fixture.invalid/' + "path" * 512 + '\n<b>literal markup</b>\n  '
        ticket = dict(TICKETS[0], title="Title" * 512, description=literal,
                      category="CustomCategory" * 128, severity="UnexpectedSeverity" * 32,
                      status="UnknownStatus" * 128, created_at="Time" * 128, updated_at="Updated" * 128)
        self.tickets[:] = [ticket]
        self.sign_in()
        for widget in self.dialog.findChildren(WrappedLabel):
            if not (widget.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard):
                self.assertEqual(widget.focusPolicy(), Qt.FocusPolicy.NoFocus)
        card = self.dialog.cards_layout.itemAt(0).widget()
        self.assertEqual(card.description_label.text(), literal)
        self.assertEqual(card.title_label.text(), ticket["title"])
        for size in ((880, 740), (520, 620), (380, 520), (360, 420), (880, 740)):
            self.dialog.resize(*size)
            pump()
            self.assertEqual(self.dialog.width(), size[0], size)
            self.assert_scroll_width(self.dialog.scroll)
            viewport = self.dialog.scroll.viewport()
            for widget in (card, self.dialog.search_row, self.dialog.account_row, self.dialog.stats_grid):
                left = widget.mapTo(viewport, QPoint(0, 0)).x()
                right = widget.mapTo(viewport, QPoint(widget.width(), 0)).x()
                self.assertGreaterEqual(left, 0, (size, widget.objectName()))
                self.assertLessEqual(right, viewport.width(), (size, widget.objectName()))
            for widget in (card.title_label, card.description_label, card.metadata, card.severity_label):
                self.assert_wrapped_text(widget, fully_visible=False)
                self.assertLessEqual(widget.minimumSizeHint().width(), 2)
                self.assertLessEqual(widget.height(), widget.maximumHeight())
                self.assertGreater(widget.verticalScrollBar().maximum(), 0)
            self.assertLessEqual(card.height(), 720)
            for action in (card.edit_button, card.delete_button, card.status_cb):
                self.assertTrue(card.rect().contains(action.mapTo(card, action.rect().topLeft())), size)
                self.assertTrue(card.rect().contains(action.mapTo(card, action.rect().bottomRight())), size)
            self.assertLessEqual(card.status_cb.minimumSizeHint().width(), card.status_cb.maximumWidth())
            self.assertEqual(card.status_cb.currentText(), ticket["status"])
            self.assertEqual(card.status_cb.toolTip(), ticket["status"])
        cursor = card.description_label.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        card.description_label.setTextCursor(cursor)
        selected = cursor.selection().toPlainText()
        self.assertEqual(selected, literal)
        card.description_label.setFocus()
        QTest.keyClick(card.description_label, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
        pump()
        self.assertEqual(card.description_label.textCursor().position(), card.description_label.document().characterCount() - 1)
        self.assertEqual(card.description_label.verticalScrollBar().value(), card.description_label.verticalScrollBar().maximum())
        cursor = card.description_label.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        card.description_label.setTextCursor(cursor)
        displayed_document = card.description_label.document()
        measurement_document = card.description_label._measure
        for mode in ("light", "solarized_dark", "default"):
            self.dialog.apply_theme(mode)
            self.dialog.resize(380, 520)
            pump()
            self.assertEqual(card.description_label.textCursor().selection().toPlainText(), literal)
            self.assert_scroll_width(self.dialog.scroll)
            self.assert_wrapped_text(card.description_label, fully_visible=False)
            self.assertIs(card.description_label.document(), displayed_document)
            self.assertIs(card.description_label._measure, measurement_document)
            self.assertLessEqual(len(card.description_label._heights), 8)
        # Render a realistic long token while keeping title/actions visible.
        self.dialog.resize(880, 740)
        self.dialog._tickets = [dict(TICKETS[0], title="A long serial message and URL", description=literal)]
        self.dialog._render_tickets_list()
        pump()
        capture(self.dialog, "tickets-long-code-wide")
        self.dialog.resize(380, 520)
        pump()
        self.dialog.scroll.verticalScrollBar().setValue(self.dialog.cards_container.y())
        pump()
        capture(self.dialog, "tickets-long-code-compact")

    def test_long_edit_and_create_content_wraps_without_replacing_draft_widgets(self):
        self.sign_in()
        literal = '<b>literal</b>\nSerial.println("' + "x" * 8192 + '");\nhttps://fixture.invalid/' + "segment" * 256
        ticket = dict(TICKETS[0], title="LongTitle" * 512, category="LongCategory" * 256, description=literal)
        edit = portal.EditTicketDialog(ticket, self.service, self.dialog)
        self.addCleanup(edit.close)
        self.addCleanup(edit.deleteLater)
        show(edit, (380, 420))
        for size in ((680, 640), (380, 420), (360, 320)):
            edit.resize(*size)
            pump()
            self.assertEqual(edit.width(), size[0])
            self.assert_scroll_width(edit.scroll)
            self.assert_wrapped_text(edit.txt_desc, fully_visible=False)
            self.assertEqual(edit.txt_title.text(), ticket["title"])
            self.assertEqual(edit.cb_category.currentText(), ticket["category"])
            self.assertEqual(edit.cb_category.toolTip(), ticket["category"])
            self.assertEqual(edit.txt_desc.toPlainText(), literal)
            self.assertLessEqual(edit.cb_category.minimumSizeHint().width(), 220)
        capture(edit, "edit-long-fields-compact")
        edit.scroll.ensureWidgetVisible(edit.txt_desc, 0, 0)
        pump()
        capture(edit, "edit-long-code-compact")
        edit.close()
        show(self.dialog)
        self.dialog.btn_new.click()
        self.dialog.new_title.setText(ticket["title"])
        self.dialog.new_desc.setPlainText(literal)
        self.dialog.new_desc.setFocus()
        cursor = self.dialog.new_desc.textCursor()
        cursor.setPosition(10)
        cursor.setPosition(30, QTextCursor.MoveMode.KeepAnchor)
        self.dialog.new_desc.setTextCursor(cursor)
        original = self.dialog.new_desc
        for size in ((880, 740), (380, 520), (360, 420), (880, 740)):
            self.dialog.resize(*size)
            pump()
            self.assert_scroll_width(self.dialog.scroll)
            self.assert_wrapped_text(self.dialog.new_desc, fully_visible=False)
            self.assertIs(self.dialog.new_desc, original)
            self.assertEqual(self.dialog.new_desc.toPlainText(), literal)
            self.assertEqual(self.dialog.new_title.text(), ticket["title"])
            self.assertEqual(self.dialog.new_desc.textCursor().anchor(), 10)
            self.assertEqual(self.dialog.new_desc.textCursor().position(), 30)
            self.assertTrue(self.dialog.new_desc.hasFocus())
        self.service.update_ticket.assert_not_called()
        self.service.create_ticket.assert_not_called()

    def test_wrapped_reading_widgets_keep_semantic_inks_and_keyboard_focus(self):
        self.sign_in()
        card = self.dialog.cards_layout.itemAt(0).widget()
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            APP.setStyleSheet(build_stylesheet(mode))
            self.dialog.apply_theme(mode)
            pump()
            colors = portal_colors(mode)
            for widget, token in ((card.title_label, "TEXT_BRIGHT"),
                                  (card.severity_label, "RED"),
                                  (card.description_label, "TEXT"),
                                  (card.metadata, "TEXT_DIM"),
                                  (self.dialog.lbl_auth_error, "RED")):
                self.assertEqual(widget.palette().color(QPalette.ColorRole.Text).name(), colors[token], (mode, token))
            self.assertEqual(self.dialog.access_label.focusPolicy(), Qt.FocusPolicy.NoFocus)
            self.assertEqual(card.description_label.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        # Reflow must not recreate the reading documents or their copied text.
        document = card.description_label.document()
        measurement = card.description_label._measure
        for width in (360, 460, 580, 880, 520, 380, 440, 640, 780, 880):
            self.dialog.resize(width, 620)
            pump()
            self.assertIs(card.description_label.document(), document)
            self.assertIs(card.description_label._measure, measurement)
            self.assertEqual(card.description_label.text(), TICKETS[0]["description"])
            self.assert_wrapped_text(card.description_label)
            self.assertLessEqual(len(card.description_label._heights), 8)

    def test_cached_non_string_metadata_still_renders_as_plain_text(self):
        # Cached ticket dictionaries are not guaranteed to normalize every field.
        for category in (None, 42):
            card = portal.TicketCard(dict(TICKETS[0], category=category), self.dialog.backdrop)
            self.assertEqual(card.metadata.text(), f"{category}\n{TICKETS[0]['created_at']}")
            self.assertIn(str(category), card.metadata.toolTip())
            card.deleteLater()

    def test_login_keyboard_mask_and_lock(self):
        capture(self.dialog, "login-default")
        self.dialog.txt_auth_email.setFocus()
        QTest.keyClick(self.dialog.txt_auth_email, Qt.Key.Key_Return)
        self.assertTrue(self.dialog.txt_auth_pwd.hasFocus())
        self.dialog.txt_auth_pwd.setText("fixture-only")
        self.dialog.btn_toggle_eye.click()
        self.assertEqual(self.dialog.txt_auth_pwd.echoMode(), QLineEdit.EchoMode.Normal)
        QTest.keyClick(self.dialog.txt_auth_pwd, Qt.Key.Key_Return)
        settle(self.dialog)
        self.service.authenticate.assert_called_once_with("developer@example.com", "fixture-only")
        self.assertEqual(self.dialog.stack.currentIndex(), 1)
        self.assertEqual(self.dialog.txt_auth_pwd.text(), "")
        self.assertEqual(self.dialog.txt_auth_pwd.echoMode(), QLineEdit.EchoMode.Password)
        self.dialog._do_lock()
        settle(self.dialog)
        self.assertEqual(self.dialog.stack.currentIndex(), 0)
        self.service.logout.assert_called_once()
        self.service.authenticate.reset_mock()
        self.dialog.txt_auth_email.setFocus()
        self.dialog.txt_auth_pwd.setText("fixture-only")
        QTest.keyClick(self.dialog.txt_auth_email, Qt.Key.Key_Return)
        settle(self.dialog)
        self.service.authenticate.assert_called_once_with("developer@example.com", "fixture-only")

    def test_search_filter_create_edit_and_delete_routes(self):
        self.sign_in()
        self.assertEqual(self.dialog.stat_total.findChild(QLabel, "val-label").text(), "3")
        self.dialog.search_input.setText("serial")
        pump()
        self.assertEqual(self.dialog.cards_layout.count(), 2)
        card = self.dialog.cards_layout.itemAt(0).widget()
        self.assertEqual(card.ticket["id"], "fixture-2")
        card.status_cb.setCurrentText("Resolved")
        settle(self.dialog)
        self.service.update_ticket.assert_called_with("fixture-2", {"status": "Resolved"})
        self.assertEqual(card.ticket["status"], "Resolved")
        card = self.dialog.cards_layout.itemAt(0).widget()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            card.delete_button.click()
        settle(self.dialog)
        self.service.delete_ticket.assert_called_once_with("fixture-2")
        self.dialog.search_input.clear()
        self.dialog.filter_cb.setCurrentText("Resolved")
        self.assertEqual(self.dialog.cards_layout.itemAt(0).widget().ticket["id"], "fixture-3")
        self.dialog.btn_new.click()
        self.dialog.new_title.setText("Preserve draft")
        self.dialog.new_desc.setPlainText("Reproduction steps\\nFixture notes")
        self.dialog.new_category.setCurrentText("Serial Monitor")
        self.dialog.btn_save_new.click()
        settle(self.dialog)
        self.service.create_ticket.assert_called_once_with(
            title="Preserve draft", category="Serial Monitor", severity="Medium",
            description="Reproduction steps\\nFixture notes", status="Open")
        self.assertTrue(self.dialog.form_card.isHidden())
        ticket = dict(TICKETS[0], category="Custom category", title="<plain title>")
        edit = portal.EditTicketDialog(ticket, self.service, self.dialog)
        show(edit)
        self.assertEqual(edit.cb_category.currentText(), "Custom category")
        self.assertEqual(edit.txt_title.text(), "<plain title>")
        edit.txt_desc.setPlainText("Updated notes")
        QTest.keyClick(edit.txt_desc, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        settle(edit)
        self.service.update_ticket.assert_called_with("fixture-1", {
            "title": "<plain title>", "category": "Custom category", "severity": "Critical",
            "status": "Open", "description": "Updated notes"})
        self.assertEqual(edit.result(), QDialog.DialogCode.Accepted)
        edit.deleteLater()

    def test_theme_preserves_drafts_focus_and_static_card_cache(self):
        self.sign_in()
        self.dialog.btn_new.click()
        self.dialog.new_title.setText("Unfinished report")
        self.dialog.new_desc.setPlainText("Keep these notes")
        self.dialog.new_title.setFocus()
        for mode in ("light", "solarized_dark", "default"):
            Theme.apply_theme(mode)
            APP.setStyleSheet(build_stylesheet(mode))
            self.dialog.apply_theme(mode)
            pump()
            self.assertEqual(self.dialog.new_title.text(), "Unfinished report")
            self.assertEqual(self.dialog.new_desc.toPlainText(), "Keep these notes")
            self.assertTrue(self.dialog.new_title.hasFocus())
            self.assertFalse(self.dialog.form_card.isHidden())
            self.dialog.form_card.grab()
            cached = self.dialog.form_card._cached_surface
            self.dialog.form_card.grab()
            self.assertIs(self.dialog.form_card._cached_surface, cached)
        self.assertIsNone(self.dialog.auth_card.graphicsEffect())

    def test_cloud_failure_stays_open_and_connection_feedback(self):
        cloud = portal.FirebaseSettingsDialog(self.service, self.dialog, config={
            "firebase_database_url": "https://stored-secret.firebasedatabase.app/",
            "firebase_api_key": "stored-secret-key",
        })
        show(cloud, (500, 360))
        self.assertEqual(cloud.db_input.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(cloud.key_input.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(cloud.db_input.text(), "")
        self.assertEqual(cloud.key_input.text(), "")
        cloud._run_test()
        self.service.test_firebase_connection.assert_not_called()
        self.assertIn("URL", cloud.feedback.text())
        cloud.db_input.setText("https://fixture.invalid/")
        cloud.key_input.setText("fixture-key")
        cloud._run_test()
        settle(cloud)
        self.service.test_firebase_connection.assert_called_once_with("fixture-key", "https://fixture.invalid/")
        self.assertTrue(cloud.test_button.isEnabled())
        self.assertTrue(cloud.save_button.isEnabled())
        self.service.save_config.return_value = False
        cloud._save()
        settle(cloud)
        self.assertTrue(cloud.isVisible())
        self.assertEqual(cloud.db_input.text(), "https://fixture.invalid/")
        self.assertEqual(cloud.feedback.property("tone"), "fail")
        capture(cloud, "cloud-save-error-default")
        self.service.save_config.return_value = True
        cloud._save()
        settle(cloud)
        self.assertEqual(cloud.result(), QDialog.DialogCode.Accepted)
        cloud.deleteLater()

    def test_connection_status_is_independent_from_package_offline_mode(self):
        self.dialog._config.update(use_firebase=True, firebase_database_url="https://fixture.invalid", firebase_api_key="fixture-key")
        with patch("src.modules.offline_runtime.network_access_disabled", return_value=True) as mode:
            self.dialog._update_cloud_badge()
            self.assertEqual(self.dialog.cloud_badge.text(), "Online")
            self.assertNotIn("Offline", self.dialog.connection_note.text())
            self.assertTrue(self.dialog.connection_note.isHidden())
            self.dialog._cloud_authenticated = True
            self.dialog._update_cloud_badge()
            self.assertEqual(self.dialog.cloud_badge.text(), "Signed in")
            mode.assert_not_called()
        message = "The cloud connection timed out. Try again."
        self.service.authenticate.return_value = (False, message)
        self.dialog.txt_auth_pwd.setText(" fixture-only ")
        self.dialog._do_authenticate()
        settle(self.dialog)
        self.service.authenticate.assert_called_once_with("developer@example.com", " fixture-only ")
        self.assertEqual(self.dialog.stack.currentIndex(), 0)
        self.assertEqual(self.dialog.lbl_auth_error.text(), message)
        self.assertFalse(self.dialog.lbl_auth_error.isHidden())

    def test_five_logo_clicks_open_ticket_portal_offline_and_six_do_not(self):
        backend = SimpleNamespace(
            is_busy=True, active_operation=None, _current_op_phase=None,
            current_board="", current_port="",
        )
        toolbar = PrimaryToolbar(backend)
        self.addCleanup(toolbar.deleteLater)
        owner_dialog = Mock()
        owner_dialog.isVisible.return_value = False
        with patch.object(portal, "OwnerTicketDialog", return_value=owner_dialog) as create_dialog:
            for _ in range(5):
                QTest.mouseClick(toolbar.logo, Qt.MouseButton.LeftButton)
            QTest.qWait(850)
            create_dialog.assert_called_once_with(backend=backend, parent=toolbar)
            owner_dialog.show.assert_called_once()
            owner_dialog.raise_.assert_called_once()
            owner_dialog.activateWindow.assert_called_once()

        with patch.object(toolbar, "_open_owner_ticket_dialog") as open_portal:
            for _ in range(6):
                QTest.mouseClick(toolbar.logo, Qt.MouseButton.LeftButton)
            QTest.qWait(850)
            open_portal.assert_not_called()

    def test_cloud_diagnosis_is_async_and_never_claims_sign_in(self):
        self.dialog._config.update(use_firebase=True, firebase_database_url="https://fixture.firebasedatabase.app/", firebase_api_key="fixture-key")
        started, release = Event(), Event()
        self.addCleanup(release.set)
        def diagnose():
            self.assertIsNot(QThread.currentThread(), APP.thread())
            started.set()
            self.assertTrue(release.wait(2))
            return True, "Database endpoint reached; account access has not been verified."
        self.service.check_connection.side_effect = diagnose
        self.dialog._diagnose_cloud()
        self.assertTrue(started.wait(.5))
        QTest.qWait(60)
        self.assertEqual(self.dialog._task_name, "diagnose")
        self.assertFalse(self.dialog.btn_diagnose.isEnabled())
        self.dialog.txt_auth_email.setText("still-responsive@example.com")
        release.set()
        settle(self.dialog)
        self.assertEqual(self.dialog.cloud_badge.text(), "Online")
        self.assertIn("Sign in", self.dialog.connection_note.text())
        self.service.authenticate.assert_not_called()
        self.service.check_connection.side_effect = None
        self.service.check_connection.return_value = (False, "The database endpoint timed out.")
        self.dialog._diagnose_cloud()
        settle(self.dialog)
        self.assertEqual(self.dialog.cloud_badge.text(), "No connection")
        self.assertIn("timed out", self.dialog.connection_note.text())

    def test_storage_initialization_and_authentication_never_run_on_qt(self):
        def read_config():
            self.assertIsNot(QThread.currentThread(), APP.thread())
            return {"owner_email": "developer@example.com", "use_firebase": False}
        def authenticate(email, password):
            self.assertIsNot(QThread.currentThread(), APP.thread())
            return True, "Local access unlocked"
        self.service.get_config.side_effect = read_config
        self.service.authenticate.side_effect = authenticate
        self.dialog._initialize_service()
        settle(self.dialog)
        self.sign_in()
        self.assertEqual(self.dialog.cloud_badge.text(), "Online")

    def test_local_key_setup_keeps_passwords_out_of_config_updates(self):
        settings = portal.FirebaseSettingsDialog(self.service, self.dialog, config={"local_access_configured": False})
        show(settings, (420, 420))
        settings.local_key.setText("long-fixture-key")
        settings.local_confirm.setText("different-key")
        settings._configure_local_access()
        self.service.configure_local_access.assert_not_called()
        self.assertIn("match", settings.feedback.text())
        settings.local_confirm.setText("long-fixture-key")
        settings._configure_local_access()
        settle(settings)
        self.service.configure_local_access.assert_called_once_with("long-fixture-key")
        self.service.save_config.assert_not_called()
        self.assertEqual(settings.local_key.text(), "")
        self.assertEqual(settings.local_confirm.text(), "")
        self.assertFalse(settings.local_save.isEnabled())
        settings._configure_local_access()
        self.assertEqual(self.service.configure_local_access.call_count, 1)
        settings.close()
        settings.deleteLater()

    def test_closing_portal_during_endpoint_check_does_not_destroy_running_worker(self):
        self.dialog._config.update(use_firebase=True, firebase_database_url="https://fixture.firebasedatabase.app/", firebase_api_key="fixture-key")
        started, release = Event(), Event()
        self.addCleanup(release.set)
        def diagnose(*_):
            started.set()
            release.wait(2)
            return True, "Fixture endpoint reached"
        self.service.check_connection.side_effect = diagnose
        self.dialog._diagnose_cloud()
        self.assertTrue(started.wait(.5))
        self.dialog.close()
        self.assertTrue(portal._SERVICE_TASKS)
        release.set()
        settle(self.dialog)
        self.assertFalse(self.dialog.isVisible())

    def test_connection_checked_automatically_and_login_centered_after_resize(self):
        self.service.check_connection.assert_called_once_with()
        for size in ((590, 580), (880, 740), (380, 600)):
            self.dialog.resize(*size)
            pump()
            viewport = self.dialog.auth_scroll.viewport()
            center = self.dialog.auth_card.mapTo(viewport, self.dialog.auth_card.rect().center())
            self.assertLessEqual(abs(center.x() - viewport.rect().center().x()), 3, size)
            if self.dialog.auth_card.height() <= viewport.height() - 4:
                self.assertLessEqual(abs(center.y() - viewport.rect().center().y()), 3, size)
            self.assertLessEqual(self.dialog.auth_card.width(), viewport.width())
            self.assertEqual(self.dialog.auth_scroll.horizontalScrollBar().maximum(), 0)
        capture(self.dialog, "login-centered-compact")

    def test_render_all_themes_and_compact_forms(self):
        self.sign_in()
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            APP.setStyleSheet(build_stylesheet(mode))
            self.dialog.apply_theme(mode)
            self.dialog.resize(880, 740)
            self.dialog.form_card.hide()
            self.dialog.scroll.verticalScrollBar().setValue(0)
            pump()
            capture(self.dialog, "tickets-" + mode)
            for control in [self.dialog.filter_cb] + [card.status_cb for card in self.dialog.findChildren(portal.TicketCard) if card.isVisible()]:
                option = QStyleOptionComboBox()
                control.initStyleOption(option)
                text_rect = control.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, control)
                self.assertGreaterEqual(text_rect.width(), control.fontMetrics().horizontalAdvance(control.currentText()), (mode, control.currentText()))
            self.dialog.stack.setCurrentIndex(0)
            capture(self.dialog, "login-" + mode)
            self.dialog.stack.setCurrentIndex(1)
            edit = portal.EditTicketDialog(TICKETS[0], self.service, self.dialog)
            show(edit)
            capture(edit, "edit-" + mode)
            cloud = portal.FirebaseSettingsDialog(self.service, self.dialog)
            show(cloud)
            capture(cloud, "cloud-" + mode)
            edit.close()
            cloud.close()
            edit.deleteLater()
            cloud.deleteLater()
        self.dialog.resize(380, 420)
        pump()
        self.assertEqual(self.dialog.width(), 380)
        self.assertLessEqual(self.dialog.minimumSizeHint().width(), 380)
        self.assertEqual(self.dialog.stats_grid._columns, 2)
        capture(self.dialog, "tickets-compact")
        self.dialog.btn_new.click()
        self.dialog.new_title.setText("A draft on a small screen")
        pump()
        self.dialog.scroll.ensureWidgetVisible(self.dialog.btn_save_new)
        pump()
        self.assertEqual(self.dialog.new_fields.classifications._columns, 1)
        point = self.dialog.btn_save_new.mapTo(self.dialog.scroll.viewport(), self.dialog.btn_save_new.rect().center())
        self.assertTrue(self.dialog.scroll.viewport().rect().contains(point))
        capture(self.dialog, "create-compact")
        self.dialog.stack.setCurrentIndex(0)
        pump()
        self.dialog.auth_scroll.ensureWidgetVisible(self.dialog.btn_unlock)
        pump()
        self.assertLessEqual(self.dialog.auth_card.width(), self.dialog.auth_scroll.viewport().width())
        capture(self.dialog, "login-compact")
        edit = portal.EditTicketDialog(TICKETS[0], self.service, self.dialog)
        show(edit, (380, 320))
        self.assertEqual(edit.width(), 380)
        self.assertTrue(edit.rect().contains(edit.btn_save.mapTo(edit, edit.btn_save.rect().center())))
        edit.scroll.ensureWidgetVisible(edit.txt_desc)
        pump()
        capture(edit, "edit-compact")
        edit.close()
        edit.deleteLater()

    def test_reading_inks_against_card_reflection(self):
        for mode in ("default", "light", "solarized_dark"):
            c = portal_colors(mode)
            card = portal.GlassCard(palette=Theme.PALETTES[mode])
            show(card, (400, 200))
            surface = card._cached_surface.toImage()
            samples = [surface.pixelColor(x, y).name() for x in range(20, 380, 8) for y in range(16, 184, 8)]
            for token in ("TEXT", "TEXT_DIM", "TEXT_BRIGHT", "CYAN", "RED", "GREEN", "ORANGE", "YELLOW"):
                self.assertGreaterEqual(min(contrast_ratio(c[token], background) for background in samples), 4.5, (mode, token))
            card.close()
            card.deleteLater()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-dir", type=Path)
    args, rest = parser.parse_known_args()
    if args.render_dir:
        RENDER_DIR = args.render_dir.resolve()
        if not RENDER_DIR.is_relative_to(ROOT / "temp"):
            parser.error("Captures must stay in the project's temp folder.")
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[sys.argv[0]] + rest)
