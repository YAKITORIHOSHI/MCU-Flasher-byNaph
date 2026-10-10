"""Private developer tickets with static, theme-aware glass surfaces.

The owner service remains responsible for authentication, Firebase and local
storage. UI forms share cached cards and retain their widgets during reflow.
"""
from __future__ import annotations

from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict
from weakref import WeakKeyDictionary
from PySide6.QtCore import Qt, Signal, QTimer, QObject, Slot, QEvent
from PySide6.QtGui import QIcon, QTextOption
from PySide6.QtWidgets import (
    QApplication, QDialog, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget,
    QLabel, QLineEdit, QTextEdit, QMessageBox,
    QSizePolicy, QMenu,
)

from main.core.owner_tickets import OwnerTicketService
from main.qt.owner_ticket_style import (
    PortalDialog, ResponsiveGrid, ResponsiveRow, button, combo, field, heading, label,
    refresh_portal_children, retone, scroll_body,
)
from main.qt.setup_components import GlassCard
from main.qt.responsive import fit_dialog

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

# An owned dialog can close while a bounded request completes. Keep workers alive
# independently of its widgets; Qt disconnects the destroyed receiver safely.
_SERVICE_TASKS = set()
_SERVICE_LOCKS = WeakKeyDictionary()


class _ServiceTask(QObject):
    completed = Signal(object)
    finished = Signal()

    def __init__(self, operation, function, service_lock):
        super().__init__(QApplication.instance())
        self.operation, self.function = operation, function
        self.service_lock = service_lock

    def run(self):
        try:
            with self.service_lock:
                value = self.function()
            result = self.operation, value, ""
        except Exception:
            result = (self.operation, None,
                      "The operation could not finish. Check cloud settings and try again.")
        finally:
            self.function = None
        try:
            self.completed.emit(result)
            self.finished.emit()
        except RuntimeError:
            pass  # Application shutdown may already have destroyed this receiver.

    def start(self):
        Thread(target=self.run, name="developer-service", daemon=True).start()


class _TaskPortal(PortalDialog):
    """Serialize a service's requests while keeping all storage/network off Qt."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._task_name = ""
        self._task_callback = None

    def _run_task(self, operation, function, callback):
        if self._task_name:
            return False
        self._task_name, self._task_callback = operation, callback
        service = getattr(self, "service", None)
        if service is not None:
            service_lock = _SERVICE_LOCKS.setdefault(service, Lock())
        else:
            service_lock = Lock()
        task = _ServiceTask(operation, function, service_lock)
        _SERVICE_TASKS.add(task)
        task.completed.connect(self._task_complete)
        task.finished.connect(lambda: _SERVICE_TASKS.discard(task))
        task.finished.connect(task.deleteLater)
        self._set_busy(True)
        task.start()
        return True

    @Slot(object)
    def _task_complete(self, result):
        operation, value, error = result
        if operation != self._task_name:
            return
        callback = self._task_callback
        self._task_name, self._task_callback = "", None
        self._set_busy(False)
        callback(value, error)

    def _set_busy(self, busy):
        pass


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
        self.description.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.description.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.title_label.setReadingHeightLimit(128)
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
        self.severity_label = label(severity, self, "label", wrap=True)
        self.severity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.severity_label.setReadingHeightLimit(80)
        retone(self.severity_label, SEVERITY_TONES.get(severity, "muted"))
        controls.addWidget(self.severity_label, 1)
        self.status_cb = combo(STATUSES, self, ticket.get("status", "Open"))
        self.status_cb.setAccessibleName("Ticket status")
        self.status_cb.setMaximumWidth(155)
        self._update_status_style(self.status_cb.currentText())
        self.status_cb.currentTextChanged.connect(self._on_status_change)
        controls.addWidget(self.status_cb)
        layout.addLayout(controls)
        description = ticket.get("description") or ""
        if description:
            self.description_label = label(description, self, wrap=True)
            self.description_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            self.description_label.setReadingHeightLimit(300)
            layout.addWidget(self.description_label)
        created, updated = ticket.get("created_at", ""), ticket.get("updated_at", "")
        stamp = f"Updated {updated}" if updated and updated != created else created
        category = str(ticket.get("category", "General"))
        self.metadata = label(category + (f"\n{stamp}" if stamp else ""), self, "metadata", wrap=True)
        self.metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.metadata.setReadingHeightLimit(128)
        self.metadata.setToolTip(f"{category}\nCreated: {created}\nLast modified: {updated or created}")
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


class EditTicketDialog(_TaskPortal):
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

        self._run_task("save", lambda: self.service.update_ticket(self._ticket_id, updates), self._saved)

    def _set_busy(self, busy):
        self.btn_save.setEnabled(not busy)
        self.fields.setEnabled(not busy)
        self.btn_save.setText("Saving…" if busy else "Save changes")

    def _saved(self, ok, error):
        if ok:
            self.accept()
        else:
            QMessageBox.critical(self, "Save error", error or "The ticket could not be saved. Your changes are still here.")


class FirebaseSettingsDialog(_TaskPortal):
    def __init__(self, service, parent=None, *, config=None):
        super().__init__(parent, preferred=(640, 530))
        self.service = service
        cfg = dict(config if config is not None else getattr(parent, "_config", {}))
        self.setWindowTitle("Connection settings — MCU Flasher")
        self.content.addWidget(heading(self.backdrop, "Connection settings"))
        self.scroll, body, layout = scroll_body(self.backdrop)
        card = GlassCard(body, radius=16)
        form = QVBoxLayout(card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(16)
        form.addWidget(label("Enter new connection details. Saved values stay encrypted and hidden on this device.", card, "muted", wrap=True))
        self.db_input = QLineEdit(card)
        self.db_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.db_input.setAccessibleName("New Firebase database address; hidden while typing")
        self.db_input.setPlaceholderText("Enter HTTPS database address")
        form.addWidget(field("Realtime Database URL", self.db_input, card))
        self.key_input = QLineEdit(card)
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setAccessibleName("New Firebase Web API key; hidden while typing")
        self.key_input.setPlaceholderText("Firebase Web API key")
        form.addWidget(field("Web API key", self.key_input, card))
        self.test_button = button("Test connection", card, self._run_test, vector="reload")
        form.addWidget(self.test_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.feedback = label("", card, "feedback", wrap=True)
        self.feedback.hide()
        form.addWidget(self.feedback)
        layout.addWidget(card)
        self._local_configured = bool(cfg.get("local_access_configured"))
        self._local_unlocked = bool(getattr(parent, "_authenticated_access", False))
        local_card = GlassCard(body, radius=16)
        local_form = QVBoxLayout(local_card)
        local_form.setContentsMargins(20, 20, 20, 20)
        local_form.setSpacing(12)
        local_form.addWidget(label("Local access key", local_card, "title"))
        local_form.addWidget(label("Protect developer tickets on this computer with your own key. This key does not sign in to Firebase.", local_card, "muted", wrap=True))
        self.local_key = QLineEdit(local_card)
        self.local_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.local_key.setPlaceholderText("At least 12 characters")
        local_form.addWidget(field("New local access key", self.local_key, local_card))
        self.local_confirm = QLineEdit(local_card)
        self.local_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        local_form.addWidget(field("Confirm access key", self.local_confirm, local_card))
        self.local_save = button("Change local access key" if self._local_configured else "Set local access key",
                                 local_card, self._configure_local_access, role="quiet")
        local_form.addWidget(self.local_save)
        if self._local_configured and not self._local_unlocked:
            local_form.addWidget(label("Sign in to Developer Access before changing your existing local key.", local_card, "metadata", wrap=True))
        layout.addWidget(local_card)
        layout.addStretch(1)
        self.content.addWidget(self.scroll, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(button("Cancel", self.backdrop, self.reject, role="quiet"))
        self.save_button = button("Save settings", self.backdrop, self._save, role="primary", vector="save")
        footer.addWidget(self.save_button)
        self.content.addLayout(footer)
        self._set_busy(False)
        self.apply_theme(self._theme_name)

    def _show_feedback(self, message, tone):
        self.feedback.setText(message)
        retone(self.feedback, tone)
        self.feedback.show()
        self.scroll.ensureWidgetVisible(self.feedback)

    def _run_test(self):
        url, key = self.db_input.text().strip(), self.key_input.text().strip()
        if not url or not key:
            self._show_feedback("Enter both the database URL and API key to test the Firebase endpoint.", "warn")
            (self.db_input if not url else self.key_input).setFocus()
            return
        self.feedback.hide()
        self._run_task("test", lambda: self.service.test_firebase_connection(key, url), self._tested)

    def _set_busy(self, busy):
        self.test_button.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        self.db_input.setEnabled(not busy)
        self.key_input.setEnabled(not busy)
        local_allowed = not busy and (not self._local_configured or self._local_unlocked)
        self.local_key.setEnabled(local_allowed)
        self.local_confirm.setEnabled(local_allowed)
        self.local_save.setEnabled(local_allowed)
        self.test_button.setText("Testing connection…" if busy and self._task_name == "test" else "Test connection")
        self.save_button.setText("Saving…" if busy and self._task_name == "save" else "Save settings")

    def _configure_local_access(self):
        if self._task_name:
            return
        if self._local_configured and not self._local_unlocked:
            self._show_feedback("Sign in to Developer Access before changing your local access key.", "warn")
            return
        password = self.local_key.text()
        if len(password) < 12 or not password.strip():
            self._show_feedback("Use at least 12 characters for your local access key.", "warn")
            self.local_key.setFocus()
            return
        if password != self.local_confirm.text():
            self._show_feedback("The access keys do not match.", "warn")
            self.local_confirm.setFocus()
            return
        self._run_task("local-key", lambda: self.service.configure_local_access(password), self._local_access_saved)

    def _local_access_saved(self, result, error):
        if error:
            self._show_feedback(error, "fail")
            return
        ok, message = result
        if ok:
            self._local_configured = True
            self.local_key.clear()
            self.local_confirm.clear()
            self.local_save.setText("Change local access key")
            self._set_busy(False)
        self._show_feedback(message, "ok" if ok else "fail")

    def _tested(self, result, error):
        if error:
            self._show_feedback(error, "fail")
            return
        ok, message = result
        self._show_feedback(message, "ok" if ok else "fail")

    def _save(self):
        url = self.db_input.text().strip()
        key = self.key_input.text().strip()
        if not url or not key:
            self._show_feedback("Enter both the database URL and API key to save the Firebase settings.", "warn")
            (self.db_input if not url else self.key_input).setFocus()
            return
        updates = {"firebase_database_url": url, "firebase_api_key": key,
                   "use_firebase": bool(url)}
        self._run_task("save", lambda: self.service.save_config(updates), self._saved)

    def _saved(self, ok, error):
        if ok:
            self.accept()
        else:
            self._show_feedback(error or "Settings could not be saved. Your changes are still here; try again.", "fail")


class OwnerTicketDialog(_TaskPortal):
    def __init__(self, backend=None, parent=None):
        super().__init__(parent, preferred=(590, 580))
        self._backend = backend
        self.service = None
        self._config = {}
        self._tickets = []
        self._cloud_authenticated = False
        self._authenticated_access = False
        self._endpoint_state = None
        self._endpoint_message = ""
        self._dashboard_fitted = False
        self._active_filter = "All Status"
        self._search_query = ""
        self.setObjectName("owner-portal-dialog")
        self.setWindowTitle("Developer tickets — MCU Flasher")
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        if _mcu_icon_path.exists():
            self.setWindowIcon(QIcon(str(_mcu_icon_path)))
        header = QHBoxLayout()
        header.addWidget(label("Developer tickets", self.backdrop, "label"), 1)
        self.cloud_badge = label("Checking connection…", self.backdrop, "metadata")
        header.addWidget(self.cloud_badge)
        self.content.addLayout(header)
        self.stack = QStackedWidget(self.backdrop)
        self.content.addWidget(self.stack, 1)
        self._build_auth_screen()
        self._build_dashboard_screen()
        self.apply_theme(self._theme_name)
        QTimer.singleShot(0, self, self._initialize_service)

    def _initialize_service(self):
        def initialize():
            service = OwnerTicketService()
            return service, service.get_config(), service.check_connection()
        self._run_task("initialize", initialize, self._initialized)

    def _initialized(self, result, error):
        if error:
            self.lbl_auth_error.setText(error)
            self.lbl_auth_error.show()
            self.btn_diagnose.setEnabled(True)
            self.btn_diagnose.setText("Retry settings")
            self._update_cloud_badge()
            return
        self.service, self._config, connection = result
        self._endpoint_state, self._endpoint_message = connection
        if not self.txt_auth_email.isModified():
            self.txt_auth_email.setText(self._config.get("owner_email", ""))
        self._set_busy(False)
        self._update_cloud_badge()

    def _set_busy(self, busy):
        ready = self.service is not None and not busy
        for control in (self.btn_unlock, self.btn_diagnose, self.btn_auth_cloud,
                        self.btn_new, self.btn_cloud, self.btn_lock, self.btn_save_new,
                        self.cards_container, self.new_fields):
            control.setEnabled(ready)
        self.btn_unlock.setText("Signing in…" if busy and self._task_name == "authenticate" else "Sign in")
        self.btn_diagnose.setText("Checking…" if busy and self._task_name == "diagnose" else "Check connection")

    def _build_auth_screen(self):
        page = QWidget(self.stack)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        self.auth_scroll, body, layout = scroll_body(page)
        self.auth_scroll.viewport().installEventFilter(self)
        self.auth_card = GlassCard(body, radius=20, accent=True)
        self.auth_card.setMaximumWidth(520)
        self.auth_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form = QVBoxLayout(self.auth_card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(12)
        form.addWidget(heading(self.auth_card, "Developer access", "Sign in to manage your issue reports."))
        self.connection_note = label("Checking connection…", self.auth_card, "muted", wrap=True)
        self.connection_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addWidget(self.connection_note)
        self.txt_auth_email = QLineEdit(self.auth_card)
        self.txt_auth_email.setPlaceholderText("Email address")
        self.txt_auth_email.returnPressed.connect(self._on_auth_email_return)
        form.addWidget(field("Email", self.txt_auth_email, self.auth_card))
        password = QWidget(self.auth_card)
        password_row = QHBoxLayout(password)
        password_row.setContentsMargins(0, 0, 0, 0)
        password_row.setSpacing(8)
        self.txt_auth_pwd = QLineEdit(password)
        self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_auth_pwd.setPlaceholderText("Password or local access key")
        self.txt_auth_pwd.returnPressed.connect(self._do_authenticate)
        password_row.addWidget(self.txt_auth_pwd, 1)
        self.btn_toggle_eye = button("Show", password, self._toggle_password_mask, role="quiet")
        self._update_eye_icon(True)
        password_row.addWidget(self.btn_toggle_eye)
        form.addWidget(field("Password", password, self.auth_card))
        self.txt_auth_pwd.setAccessibleName("Password or local access key")
        self.lbl_auth_error = label("", self.auth_card, "feedback", wrap=True)
        retone(self.lbl_auth_error, "fail")
        self.lbl_auth_error.hide()
        form.addWidget(self.lbl_auth_error)
        self.btn_unlock = button("Sign in", self.auth_card, self._do_authenticate, role="primary")
        form.addWidget(self.btn_unlock)
        actions = ResponsiveGrid([
            button("Check connection", self.auth_card, self._diagnose_cloud, role="quiet", vector="reload"),
            button("Connection settings", self.auth_card, self._open_firebase_settings_modal, role="quiet", vector="settings"),
        ], self.auth_card, columns=2, cell_width=190)
        self.btn_diagnose, self.btn_auth_cloud = actions._widgets
        form.addWidget(actions)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addStretch(1)
        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.addStretch(1)
        card_row.addWidget(self.auth_card, 100)
        card_row.addStretch(1)
        layout.addLayout(card_row)
        layout.addStretch(1)
        outer.addWidget(self.auth_scroll)
        self.stack.addWidget(page)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "auth_scroll"):
            QTimer.singleShot(0, self, self._fit_auth_card)

    def _fit_auth_card(self):
        self.auth_card.setFixedWidth(min(520, max(1, self.auth_scroll.viewport().width() - 4)))

    def eventFilter(self, watched, event):
        if (hasattr(self, "auth_scroll") and watched is self.auth_scroll.viewport()
                and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)):
            QTimer.singleShot(0, self, self._fit_auth_card)
        return super().eventFilter(watched, event)

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
        if self._task_name or self.service is None:
            return
        try:
            email = self.txt_auth_email.text().strip()
            pwd = self.txt_auth_pwd.text()
            self.lbl_auth_error.setVisible(False)

            if not pwd.strip():
                self.lbl_auth_error.setText("Enter your access key.")
                self.lbl_auth_error.setVisible(True)
                self.txt_auth_pwd.setFocus()
                return

            def authenticate():
                ok, message = self.service.authenticate(email, pwd)
                return ok, message, self.service.is_cloud_authenticated, self.service.get_tickets() if ok else []
            self._run_task("authenticate", authenticate, self._authenticated)
        except Exception as e:
            try:
                self.lbl_auth_error.setText(f"Authentication error: {e}")
                self.lbl_auth_error.setVisible(True)
            except Exception:
                pass

    def _authenticated(self, result, error):
        if error:
            self.lbl_auth_error.setText(error)
            self.lbl_auth_error.show()
            return
        ok, message, self._cloud_authenticated, tickets = result
        self._authenticated_access = ok
        if not ok:
            self.lbl_auth_error.setText(message)
            self.lbl_auth_error.show()
            self._update_cloud_badge()
            return
        self.txt_auth_pwd.clear()
        self._update_eye_icon(True)
        self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._tickets = tickets
        self._update_cloud_badge()
        self.access_label.setText("Signed in to Firebase" if self._cloud_authenticated else "Local developer access")
        self._render_tickets_list()
        self.stack.setCurrentIndex(1)
        if not self._dashboard_fitted:
            fit_dialog(self, preferred=(880, 740), minimum=(360, 300))
            self._dashboard_fitted = True
        self.update()

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
        self.search_input = QLineEdit(body)
        self.search_input.setPlaceholderText("Search tickets")
        self.search_input.setAccessibleName("Search tickets")
        self.search_input.textChanged.connect(self._on_search_changed)
        self.filter_cb = combo(["All Status"] + STATUSES, body)
        self.filter_cb.setAccessibleName("Filter tickets by status")
        self.filter_cb.setMaximumWidth(170)
        self.filter_cb.currentTextChanged.connect(self._on_filter_changed)
        self.search_row = ResponsiveRow([(self.search_input, 1), (self.filter_cb, 0)], body, compact_width=440)
        layout.addWidget(self.search_row)
        self.access_label = label("Private defect reports", body, "metadata", wrap=True)
        account_actions = QWidget(body)
        actions = QHBoxLayout(account_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        self.btn_cloud = button("Connection settings", body, self._open_firebase_settings_modal, role="quiet", vector="settings")
        self.btn_lock = button("Lock", body, self._do_lock, role="quiet")
        actions.addWidget(self.btn_cloud)
        actions.addWidget(self.btn_lock)
        self.account_row = ResponsiveRow([(self.access_label, 1), (account_actions, 0)], body, compact_width=520)
        layout.addWidget(self.account_row)
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
        if self._task_name:
            return
        try:
            title = self.new_title.text().strip()
            if not title:
                QMessageBox.warning(self, "Validation", "Ticket title cannot be blank.")
                return

            cat = self.new_category.currentText()
            sev = self.new_sev.currentText()
            st = self.new_status.currentText()
            desc = self.new_desc.toPlainText().strip()

            def create():
                ticket = self.service.create_ticket(title=title, category=cat, severity=sev, description=desc, status=st)
                return ticket, self.service.get_tickets()
            self._run_task("create", create, self._created)
        except Exception:
            QMessageBox.warning(self, "Create ticket", "The ticket could not be created. Your draft is still here.")

    def _created(self, result, error):
        if not error and result and result[0]:
            self._tickets = result[1]
            self.new_title.clear()
            self.new_desc.clear()
            self.form_card.setVisible(False)
            self._render_tickets_list()
        else:
            QMessageBox.warning(self, "Create ticket", error or "The ticket could not be created. Your draft is still here.")

    def _update_stats(self, tickets):
        values = [len(tickets), sum(t.get("severity") == "Critical" for t in tickets),
                  sum(t.get("status") in ("Open", "In Progress") for t in tickets),
                  sum(t.get("status") in ("Resolved", "Closed") for t in tickets)]
        for widget, value in zip((self.stat_total, self.stat_critical, self.stat_open, self.stat_resolved), values):
            widget.findChild(QLabel, "val-label").setText(str(value))

    def _refresh_tickets_list(self):
        self._run_task("tickets", self.service.get_tickets, self._tickets_loaded)

    def _tickets_loaded(self, tickets, error):
        if error:
            QMessageBox.warning(self, "Load tickets", error)
            return
        self._tickets = tickets
        self._render_tickets_list()

    def _render_tickets_list(self):
        while self.cards_layout.count() > 1:
            widget = self.cards_layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        tickets = self._tickets
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
        def update():
            ok = self.service.update_ticket(ticket_id, {"status": status})
            return ok, self.service.get_tickets()
        self._run_task("update", update, self._ticket_mutated)

    def _ticket_mutated(self, result, error):
        if not error and result:
            self._tickets = result[1]
            self._render_tickets_list()
        if error or not result or not result[0]:
            QMessageBox.warning(self, "Update ticket", error or "The ticket could not be changed. Try again.")

    def _on_ticket_deleted(self, ticket_id: str) -> None:
        try:
            def delete():
                ok = self.service.delete_ticket(ticket_id)
                return ok, self.service.get_tickets()
            self._run_task("delete", delete, self._ticket_mutated)
        except Exception:
            pass

    def _on_filter_changed(self, text: str) -> None:
        try:
            self._active_filter = text
            self._render_tickets_list()
        except Exception:
            pass

    def _on_search_changed(self, text: str) -> None:
        try:
            self._search_query = text.strip()
            self._render_tickets_list()
        except Exception:
            pass

    def _do_lock(self):
        self._run_task("lock", self.service.logout, self._locked)

    def _locked(self, _, error):
        if error:
            QMessageBox.warning(self, "Lock developer access", error)
            return
        self._cloud_authenticated = False
        self._authenticated_access = False
        self.txt_auth_pwd.clear()
        self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._update_eye_icon(True)
        self.lbl_auth_error.hide()
        self._update_cloud_badge()
        self.stack.setCurrentIndex(0)
        self.txt_auth_pwd.setFocus()

    def _update_cloud_badge(self):
        if self.service is None:
            text, tone, message = "Unavailable", "warn", "Settings could not be loaded. Use Retry settings."
        elif self._endpoint_state is False:
            text, tone, message = "No connection", "warn", self._endpoint_message
        elif self._cloud_authenticated:
            text, tone, message = "Signed in", "ok", "You are signed in."
        elif not self._config.get("use_firebase"):
            text, tone, message = "Online" if self._endpoint_state else "Local tickets", "muted", "Use your local access key to sign in on this device."
        elif not (self._config.get("firebase_database_url") and self._config.get("firebase_api_key")):
            text, tone, message = "Online" if self._endpoint_state else "Not set up", "active", "Open Connection settings to set up cloud sign-in."
        elif self._endpoint_state is True:
            text, tone, message = "Online", "ok", "Connected. Sign in to continue."
        else:
            text, tone, message = "Checking…", "muted", "Checking connection…"
        self.cloud_badge.setText(text)
        self.cloud_badge.setToolTip(message)
        self.connection_note.setText(message)
        self.connection_note.setVisible(self._endpoint_state is not True or not self._config.get("use_firebase")
                                        or not self._config.get("firebase_api_key"))
        retone(self.cloud_badge, tone)

    def _diagnose_cloud(self):
        if self.service is None:
            self._initialize_service()
            return
        self._run_task("diagnose", self.service.check_connection, self._diagnosed)

    def _diagnosed(self, result, error):
        self._endpoint_state, self._endpoint_message = result if not error else (False, error)
        self._update_cloud_badge()
        self._fit_auth_card()

    def _open_firebase_settings_modal(self):
        if self._task_name or self.service is None:
            return
        dialog = FirebaseSettingsDialog(self.service, self, config=self._config)
        dialog.exec()
        self._run_task("config", self.service.get_config, self._settings_loaded)
        dialog.deleteLater()

    def _settings_loaded(self, config, error):
        if not error:
            self._config = config
            self._endpoint_state = None
            self._endpoint_message = ""
        self._update_cloud_badge()
        if not error:
            self._diagnose_cloud()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.stack.currentIndex() == 0:
                self._do_authenticate()
            event.accept()
        else:
            super().keyPressEvent(event)
