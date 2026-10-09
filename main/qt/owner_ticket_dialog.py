"""Private developer tickets with static, theme-aware glass surfaces.

The owner service remains responsible for authentication, Firebase and local
storage. UI forms share cached cards and retain their widgets during reflow.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QDialog, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget,
    QLabel, QLineEdit, QTextEdit, QMessageBox,
    QSizePolicy, QMenu,
)

from main.core.owner_tickets import OwnerTicketService, cloud_network_error, is_internet_available
from main.qt.owner_ticket_style import (
    PortalDialog, ResponsiveGrid, button, combo, field, heading, label,
    refresh_portal_children, retone, scroll_body,
)
from main.qt.setup_components import GlassCard

_assets_dir = Path(__file__).resolve().parents[2] / "src/assets"
_mcu_icon_path = _assets_dir / "mcu_icon.ico"

CATEGORIES = [
    "GUI / Interface", "Monaco Code Editor", "Serial Monitor",
    "PlatformIO / Toolchain", "Hardware & COM Port", "Low-End & HDD", "General Defect",
]
STATUSES = ["Open", "In Progress", "Resolved", "Closed"]
SEVERITIES = ["Critical", "High", "Medium", "Low"]
SEVERITY_TONES = {"Critical": "fail", "High": "high", "Medium": "warn", "Low": "active"}
STATUS_TONES = {"Open": "active", "In Progress": "warn", "Resolved": "ok", "Closed": "muted"}


class TicketFields(QWidget):
    """Shared create/edit fields; narrow windows stack classifications."""

    def __init__(self, parent, ticket=None):
        super().__init__(parent)
        ticket = ticket or {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.title = QLineEdit(ticket.get("title", ""), self)
        self.title.setPlaceholderText("Briefly describe the issue")
        layout.addWidget(field("Title", self.title, self))
        self.category = combo(CATEGORIES, self, ticket.get("category", CATEGORIES[0]))
        self.severity = combo(SEVERITIES, self, ticket.get("severity", "Medium"))
        self.status = combo(STATUSES, self, ticket.get("status", "Open"))
        self.classifications = ResponsiveGrid([
            field("Category", self.category, self),
            field("Severity", self.severity, self),
            field("Status", self.status, self),
        ], self, cell_width=170)
        layout.addWidget(self.classifications)
        self.severity.currentTextChanged.connect(lambda value: retone(self.severity, SEVERITY_TONES.get(value, "muted")))
        self.status.currentTextChanged.connect(lambda value: retone(self.status, STATUS_TONES.get(value, "muted")))
        retone(self.severity, SEVERITY_TONES.get(self.severity.currentText(), "muted"))
        retone(self.status, STATUS_TONES.get(self.status.currentText(), "muted"))
        self.description = QTextEdit(self)
        self.description.setAcceptRichText(False)
        self.description.setPlainText(ticket.get("description", ""))
        self.description.setPlaceholderText("What happened?\nSteps to reproduce and useful logs.")
        self.description.setMinimumHeight(110)
        self.description.setMaximumHeight(180)
        layout.addWidget(field("Notes", self.description, self))


class TicketCard(GlassCard):
    status_changed = Signal(str, str)
    deleted = Signal(str)
    edit_requested = Signal(dict)

    def __init__(self, ticket, parent=None):
        super().__init__(parent, radius=14)
        self.ticket = dict(ticket)
        self._ticket_id = ticket.get("id", "")
        self.setObjectName("ticket-card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title_label = label(ticket.get("title", "Untitled defect"), self, "title", wrap=True)
        top.addWidget(self.title_label, 1)
        self.edit_button = button("", self, self._on_edit, role="icon", vector="modify")
        self.edit_button.setFixedSize(34, 34)
        self.edit_button.setToolTip("Edit ticket")
        self.edit_button.setAccessibleName("Edit ticket")
        self.delete_button = button("", self, self._on_delete, role="icon", vector="clear")
        self.delete_button.setFixedSize(34, 34)
        self.delete_button.setToolTip("Delete ticket")
        self.delete_button.setAccessibleName("Delete ticket")
        top.addWidget(self.edit_button, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.delete_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        controls = QHBoxLayout()
        controls.setSpacing(12)
        severity = ticket.get("severity", "Medium")
        self.severity_label = label(severity, self, "label")
        retone(self.severity_label, SEVERITY_TONES.get(severity, "muted"))
        controls.addWidget(self.severity_label)
        self.status_cb = combo(STATUSES, self, ticket.get("status", "Open"))
        self.status_cb.setAccessibleName("Ticket status")
        self.status_cb.setMaximumWidth(155)
        self._update_status_style(self.status_cb.currentText())
        self.status_cb.currentTextChanged.connect(self._on_status_change)
        controls.addWidget(self.status_cb)
        controls.addStretch(1)
        layout.addLayout(controls)
        description = (ticket.get("description") or "").strip()
        if description:
            self.description_label = label(description, self, wrap=True)
            self.description_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(self.description_label)
        created, updated = ticket.get("created_at", ""), ticket.get("updated_at", "")
        stamp = f"Updated {updated}" if updated and updated != created else created
        self.metadata = label(f"{ticket.get('category', 'General')}\n{stamp}".rstrip(), self, "metadata", wrap=True)
        self.metadata.setToolTip(f"Created: {created}\nLast modified: {updated or created}")
        layout.addWidget(self.metadata)

    def _update_status_style(self, status):
        retone(self.status_cb, STATUS_TONES.get(status, "muted"))

    def _on_status_change(self, value):
        self.ticket["status"] = value
        self._update_status_style(value)
        self.status_changed.emit(self._ticket_id, value)

    def _on_edit(self):
        self.edit_requested.emit(self.ticket)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_edit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Edit ticket", self._on_edit)
        menu.addAction("Delete ticket", self._on_delete)
        menu.exec(event.globalPos())

    def _on_delete(self) -> None:
        try:
            res = QMessageBox.question(
                self,
                "Delete Ticket",
                f"Are you sure you want to delete ticket:\n\n\"{self.ticket.get('title')}\"?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if res == QMessageBox.StandardButton.Yes:
                self.deleted.emit(self._ticket_id)
        except Exception:
            pass


class EditTicketDialog(PortalDialog):
    def __init__(self, ticket, service, parent=None):
        super().__init__(parent, preferred=(680, 640))
        self.ticket = ticket
        self.service = service
        self._ticket_id = ticket.get("id", "")
        self.setObjectName("edit-ticket-dialog")
        self.setWindowTitle("Edit ticket — MCU Flasher")
        self.content.addWidget(heading(self.backdrop, "Edit ticket", "Update the issue and investigation notes."))
        self.scroll, body, layout = scroll_body(self.backdrop)
        card = GlassCard(body, radius=16)
        fields_layout = QVBoxLayout(card)
        fields_layout.setContentsMargins(20, 20, 20, 20)
        self.fields = TicketFields(card, ticket)
        fields_layout.addWidget(self.fields)
        self.txt_title = self.fields.title
        self.cb_category = self.fields.category
        self.cb_severity = self.fields.severity
        self.cb_status = self.fields.status
        self.txt_desc = self.fields.description
        layout.addWidget(card)
        layout.addStretch(1)
        self.content.addWidget(self.scroll, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(button("Cancel", self.backdrop, self.reject, role="quiet"))
        self.btn_save = button("Save changes", self.backdrop, self._do_save, role="primary", vector="save")
        footer.addWidget(self.btn_save)
        self.content.addLayout(footer)
        self.apply_theme(self._theme_name)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._do_save()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _do_save(self) -> None:
        title = self.txt_title.text().strip()
        if not title:
            QMessageBox.warning(self, "Validation", "Ticket title cannot be blank.")
            self.txt_title.setFocus()
            return

        cat = self.cb_category.currentText()
        sev = self.cb_severity.currentText()
        st = self.cb_status.currentText()
        desc = self.txt_desc.toPlainText().strip()

        updates = {
            "title": title,
            "category": cat,
            "severity": sev,
            "status": st,
            "description": desc,
        }

        try:
            ok = self.service.update_ticket(self._ticket_id, updates)
            if ok:
                self.accept()
            else:
                QMessageBox.critical(self, "Save Error", "Failed to update defect report in database.")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"An error occurred while updating the ticket:\n{e}")


class FirebaseSettingsDialog(PortalDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent, preferred=(640, 530))
        self.service = service
        cfg = service.get_config()
        self.setWindowTitle("Firebase settings — MCU Flasher")
        self.content.addWidget(heading(self.backdrop, "Cloud settings", "Firebase synchronization for developer tickets."))
        self.scroll, body, layout = scroll_body(self.backdrop)
        card = GlassCard(body, radius=16)
        form = QVBoxLayout(card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(16)
        form.addWidget(label("Tickets use the local cache when cloud synchronization is unavailable.", card, "muted", wrap=True))
        self.db_input = QLineEdit(cfg.get("firebase_database_url", ""), card)
        self.db_input.setPlaceholderText("https://your-project.firebasedatabase.app/")
        form.addWidget(field("Realtime Database URL", self.db_input, card))
        self.key_input = QLineEdit(cfg.get("firebase_api_key", ""), card)
        self.key_input.setPlaceholderText("Firebase Web API key")
        form.addWidget(field("Web API key", self.key_input, card))
        self.test_button = button("Test connection", card, self._run_test, vector="reload")
        form.addWidget(self.test_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.feedback = label("", card, "feedback", wrap=True)
        self.feedback.hide()
        form.addWidget(self.feedback)
        layout.addWidget(card)
        layout.addStretch(1)
        self.content.addWidget(self.scroll, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(button("Cancel", self.backdrop, self.reject, role="quiet"))
        self.save_button = button("Save settings", self.backdrop, self._save, role="primary", vector="save")
        footer.addWidget(self.save_button)
        self.content.addLayout(footer)
        self.apply_theme(self._theme_name)

    def _show_feedback(self, message, tone):
        self.feedback.setText(message)
        retone(self.feedback, tone)
        self.feedback.show()
        self.scroll.ensureWidgetVisible(self.feedback)

    def _run_test(self):
        url, key = self.db_input.text().strip(), self.key_input.text().strip()
        if not url:
            self._show_feedback("Enter a database URL first.", "warn")
            self.db_input.setFocus()
            return
        self.test_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.test_button.setText("Testing connection…")
        self.feedback.hide()
        QApplication.processEvents()
        try:
            ok, message = self.service.test_firebase_connection(key, url)
            self._show_feedback(message, "ok" if ok else "fail")
        except Exception as error:
            self._show_feedback(str(error), "fail")
        finally:
            self.test_button.setEnabled(True)
            self.save_button.setEnabled(True)
            self.test_button.setText("Test connection")

    def _save(self):
        url = self.db_input.text().strip()
        try:
            ok = self.service.save_config({
                "firebase_database_url": url,
                "firebase_api_key": self.key_input.text().strip(),
                "use_firebase": bool(url),
            })
            if not ok:
                self._show_feedback("Settings could not be saved. Your changes are still here; try again.", "fail")
                return
            self.accept()
        except Exception as error:
            self._show_feedback(f"Settings could not be saved: {error}", "fail")


class OwnerTicketDialog(PortalDialog):
    def __init__(self, backend=None, parent=None):
        super().__init__(parent)
        self._backend = backend
        self.service = OwnerTicketService()
        self._active_filter = "All Status"
        self._search_query = ""
        self.setObjectName("owner-portal-dialog")
        self.setWindowTitle("Developer tickets — MCU Flasher")
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        if _mcu_icon_path.exists():
            self.setWindowIcon(QIcon(str(_mcu_icon_path)))
        header = QHBoxLayout()
        header.addWidget(label("MCU Flasher / Developer", self.backdrop, "label"), 1)
        self.cloud_badge = label("Local cache", self.backdrop, "metadata")
        header.addWidget(self.cloud_badge)
        self.content.addLayout(header)
        self.stack = QStackedWidget(self.backdrop)
        self.content.addWidget(self.stack, 1)
        self._build_auth_screen()
        self._build_dashboard_screen()
        self._update_cloud_badge()
        self.apply_theme(self._theme_name)

    def _build_auth_screen(self):
        page = QWidget(self.stack)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        self.auth_scroll, body, layout = scroll_body(page)
        layout.addStretch(1)
        self.auth_card = GlassCard(body, radius=20, accent=True)
        self.auth_card.setMaximumWidth(470)
        self.auth_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form = QVBoxLayout(self.auth_card)
        form.setContentsMargins(28, 28, 28, 28)
        form.setSpacing(20)
        form.addWidget(heading(self.auth_card, "Developer access", "Sign in to manage private issue tickets."))
        self.txt_auth_email = QLineEdit(self.auth_card)
        self.txt_auth_email.setText(self.service.get_config().get("owner_email", ""))
        self.txt_auth_email.setPlaceholderText("Developer ID or email")
        self.txt_auth_email.returnPressed.connect(self._on_auth_email_return)
        form.addWidget(field("Developer ID / email", self.txt_auth_email, self.auth_card))
        password = QWidget(self.auth_card)
        password_row = QHBoxLayout(password)
        password_row.setContentsMargins(0, 0, 0, 0)
        password_row.setSpacing(8)
        self.txt_auth_pwd = QLineEdit(password)
        self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_auth_pwd.setPlaceholderText("Enter your access key")
        self.txt_auth_pwd.returnPressed.connect(self._do_authenticate)
        password_row.addWidget(self.txt_auth_pwd, 1)
        self.btn_toggle_eye = button("Show", password, self._toggle_password_mask, role="quiet")
        self._update_eye_icon(True)
        password_row.addWidget(self.btn_toggle_eye)
        form.addWidget(field("Access key", password, self.auth_card))
        self.txt_auth_pwd.setAccessibleName("Access key")
        self.lbl_auth_error = label("", self.auth_card, "feedback", wrap=True)
        retone(self.lbl_auth_error, "fail")
        self.lbl_auth_error.hide()
        form.addWidget(self.lbl_auth_error)
        self.btn_unlock = button("Sign in", self.auth_card, self._do_authenticate, role="primary")
        form.addWidget(self.btn_unlock)
        form.addWidget(label("Private developer workspace", self.auth_card, "metadata"))
        layout.addWidget(self.auth_card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        outer.addWidget(self.auth_scroll)
        self.stack.addWidget(page)

    def _update_eye_icon(self, is_masked):
        # A textual control stays visible and readable in every palette.
        self.btn_toggle_eye.setText("Show" if is_masked else "Hide")
        self.btn_toggle_eye.setAccessibleName("Show access key" if is_masked else "Hide access key")

    def _on_auth_email_return(self) -> None:
        """Handle Enter key in email field by advancing to password or authenticating."""
        try:
            if not self.txt_auth_pwd.text():
                self.txt_auth_pwd.setFocus()
            else:
                self._do_authenticate()
        except Exception:
            pass

    def _toggle_password_mask(self) -> None:
        try:
            if self.txt_auth_pwd.echoMode() == QLineEdit.EchoMode.Password:
                self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Normal)
                self._update_eye_icon(is_masked=False)
            else:
                self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
                self._update_eye_icon(is_masked=True)
        except Exception:
            pass

    def _do_authenticate(self) -> None:
        try:
            email = self.txt_auth_email.text().strip()
            pwd = self.txt_auth_pwd.text()
            self.lbl_auth_error.setVisible(False)

            if not pwd.strip():
                self.lbl_auth_error.setText("Enter your access key.")
                self.lbl_auth_error.setVisible(True)
                self.txt_auth_pwd.setFocus()
                return

            ok, msg = self.service.authenticate(email, pwd)
            if ok:
                self.txt_auth_pwd.clear()
                self._update_eye_icon(is_masked=True)
                self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
                self._update_cloud_badge()
                self._refresh_tickets_list()
                self.stack.setCurrentIndex(1)
                self.update()
            else:
                self.lbl_auth_error.setText(f"{msg}")
                self.lbl_auth_error.setVisible(True)
        except Exception as e:
            try:
                self.lbl_auth_error.setText(f"Authentication error: {e}")
                self.lbl_auth_error.setVisible(True)
            except Exception:
                pass

    def _build_dashboard_screen(self):
        page = QWidget(self.stack)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)
        title_row = QHBoxLayout()
        title_row.addWidget(label("Issue tracker", page, "heading", wrap=True), 1)
        self.btn_new = button("New ticket", page, self._toggle_new_ticket_form, role="primary", vector="modify")
        title_row.addWidget(self.btn_new)
        outer.addLayout(title_row)
        self.scroll, body, layout = scroll_body(page)
        ribbon = GlassCard(body, radius=14)
        stats_layout = QVBoxLayout(ribbon)
        stats_layout.setContentsMargins(18, 14, 18, 14)
        self.stat_total = self._make_stat_badge("Total tickets", "0", "active", ribbon)
        self.stat_critical = self._make_stat_badge("Critical", "0", "fail", ribbon)
        self.stat_open = self._make_stat_badge("Active", "0", "warn", ribbon)
        self.stat_resolved = self._make_stat_badge("Completed", "0", "ok", ribbon)
        self.stats_grid = ResponsiveGrid([self.stat_total, self.stat_critical, self.stat_open, self.stat_resolved], ribbon, columns=4, cell_width=130)
        stats_layout.addWidget(self.stats_grid)
        layout.addWidget(ribbon)
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.search_input = QLineEdit(body)
        self.search_input.setPlaceholderText("Search tickets")
        self.search_input.setAccessibleName("Search tickets")
        self.search_input.textChanged.connect(self._on_search_changed)
        controls.addWidget(self.search_input, 1)
        self.filter_cb = combo(["All Status"] + STATUSES, body)
        self.filter_cb.setAccessibleName("Filter tickets by status")
        self.filter_cb.setMaximumWidth(170)
        self.filter_cb.currentTextChanged.connect(self._on_filter_changed)
        controls.addWidget(self.filter_cb)
        layout.addLayout(controls)
        actions = QHBoxLayout()
        actions.addWidget(label("Private defect reports", body, "metadata"), 1)
        self.btn_cloud = button("Cloud settings", body, self._open_firebase_settings_modal, role="quiet", vector="settings")
        self.btn_lock = button("Lock", body, self._do_lock, role="quiet")
        actions.addWidget(self.btn_cloud)
        actions.addWidget(self.btn_lock)
        layout.addLayout(actions)
        self.form_card = GlassCard(body, radius=16, accent=True)
        form = QVBoxLayout(self.form_card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(16)
        form.addWidget(label("New ticket", self.form_card, "title"))
        self.new_fields = TicketFields(self.form_card)
        form.addWidget(self.new_fields)
        self.new_title = self.new_fields.title
        self.new_category = self.new_fields.category
        self.new_sev = self.new_fields.severity
        self.new_status = self.new_fields.status
        self.new_desc = self.new_fields.description
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(button("Cancel", self.form_card, self.form_card.hide, role="quiet"))
        self.btn_save_new = button("Create ticket", self.form_card, self._do_save_new_ticket, role="primary", vector="save")
        footer.addWidget(self.btn_save_new)
        form.addLayout(footer)
        self.form_card.hide()
        layout.addWidget(self.form_card)
        self.cards_container = QWidget(body)
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(14)
        self.cards_layout.addStretch(1)
        layout.addWidget(self.cards_container)
        layout.addStretch(1)
        outer.addWidget(self.scroll, 1)
        self.stack.addWidget(page)

    def _make_stat_badge(self, caption, value, tone, parent):
        widget = QWidget(parent)
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        val = label(value, widget, "value")
        val.setObjectName("val-label")
        retone(val, tone)
        box.addWidget(val)
        box.addWidget(label(caption, widget, "metadata"))
        return widget

    def _toggle_new_ticket_form(self):
        opening = self.form_card.isHidden()
        self.form_card.setVisible(opening)
        if opening:
            self.new_title.setFocus()
            QTimer.singleShot(0, self, lambda: self.scroll.ensureWidgetVisible(self.new_title))

    def _do_save_new_ticket(self) -> None:
        try:
            title = self.new_title.text().strip()
            if not title:
                QMessageBox.warning(self, "Validation", "Ticket title cannot be blank.")
                return

            cat = self.new_category.currentText()
            sev = self.new_sev.currentText()
            st = self.new_status.currentText()
            desc = self.new_desc.toPlainText().strip()

            self.service.create_ticket(
                title=title,
                category=cat,
                severity=sev,
                description=desc,
                status=st,
            )

            # Reset form
            self.new_title.clear()
            self.new_desc.clear()
            self.form_card.setVisible(False)
            self._refresh_tickets_list()
        except Exception:
            pass

    def _update_stats(self, tickets):
        values = [len(tickets), sum(t.get("severity") == "Critical" for t in tickets),
                  sum(t.get("status") in ("Open", "In Progress") for t in tickets),
                  sum(t.get("status") in ("Resolved", "Closed") for t in tickets)]
        for widget, value in zip((self.stat_total, self.stat_critical, self.stat_open, self.stat_resolved), values):
            widget.findChild(QLabel, "val-label").setText(str(value))

    def _refresh_tickets_list(self):
        while self.cards_layout.count() > 1:
            widget = self.cards_layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        tickets = self.service.get_tickets()
        self._update_stats(tickets)
        query = self._search_query.lower()
        filtered = [ticket for ticket in tickets
                    if (self._active_filter in ("All", "All Status") or ticket.get("status") == self._active_filter)
                    and (not query or query in " ".join(str(ticket.get(key, "")) for key in ("title", "description", "category")).lower())]
        if not filtered:
            empty = GlassCard(self.cards_container, radius=14)
            box = QVBoxLayout(empty)
            box.setContentsMargins(24, 30, 24, 30)
            box.setSpacing(8)
            box.addWidget(label("No matching tickets" if tickets else "No tickets yet", empty, "title", wrap=True))
            box.addWidget(label("Try a different search or status filter." if tickets else "Create a ticket to record an issue and track its progress.", empty, "muted", wrap=True))
            self.cards_layout.insertWidget(0, empty)
        for index, ticket in enumerate(filtered):
            card = TicketCard(ticket, self.cards_container)
            card.status_changed.connect(self._on_ticket_status_changed)
            card.deleted.connect(self._on_ticket_deleted)
            card.edit_requested.connect(self._open_edit_ticket_modal)
            self.cards_layout.insertWidget(index, card)
        refresh_portal_children(self.cards_container, self._theme_name)

    def _open_edit_ticket_modal(self, ticket: Dict[str, Any]) -> None:
        try:
            dlg = EditTicketDialog(ticket=ticket, service=self.service, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._refresh_tickets_list()
        except Exception:
            pass

    def _on_ticket_status_changed(self, ticket_id, status):
        self.service.update_ticket(ticket_id, {"status": status})
        self._update_stats(self.service.get_tickets())

    def _on_ticket_deleted(self, ticket_id: str) -> None:
        try:
            self.service.delete_ticket(ticket_id)
            self._refresh_tickets_list()
        except Exception:
            pass

    def _on_filter_changed(self, text: str) -> None:
        try:
            self._active_filter = text
            self._refresh_tickets_list()
        except Exception:
            pass

    def _on_search_changed(self, text: str) -> None:
        try:
            self._search_query = text.strip()
            self._refresh_tickets_list()
        except Exception:
            pass

    def _do_lock(self):
        self.service.logout()
        self.txt_auth_pwd.clear()
        self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._update_eye_icon(True)
        self.lbl_auth_error.hide()
        self._update_cloud_badge()
        self.stack.setCurrentIndex(0)
        self.txt_auth_pwd.setFocus()

    def _update_cloud_badge(self):
        if cloud_network_error():
            self.cloud_badge.setText("Offline Mode")
            self.cloud_badge.setToolTip(cloud_network_error())
            retone(self.cloud_badge, "warn")
            return
        cfg = self.service.get_config()
        cloud = bool(cfg.get("use_firebase") and cfg.get("firebase_database_url") and is_internet_available(timeout=0.4))
        signed_in = cloud and self.service.is_cloud_authenticated
        self.cloud_badge.setText("Cloud signed in" if signed_in else "Cloud configured" if cloud else "Local cache")
        self.cloud_badge.setToolTip("Authenticated with Firebase. Ticket access depends on database rules." if signed_in else
                                   "Firebase is configured. Sign in to verify credentials and ticket access." if cloud else
                                   "Using locally cached tickets.")
        retone(self.cloud_badge, "ok" if signed_in else "active" if cloud else "muted")

    def _open_firebase_settings_modal(self):
        dialog = FirebaseSettingsDialog(self.service, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._update_cloud_badge()
        dialog.deleteLater()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.stack.currentIndex() == 0:
                self._do_authenticate()
            event.accept()
        else:
            super().keyPressEvent(event)
