"""Cloud account and explicit sketch synchronization in owned Qt dialogs.

Vault access, source scans and network calls run in one bounded background job.
Closing a view invalidates its completion; no automatic push/pull is scheduled.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QScrollArea, QFrame, QLabel, QLineEdit, QCheckBox, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QInputDialog, QComboBox, QSizePolicy,
)

from main.qt.icons import ActionButton, icon
from main.qt.setup_components import GlassCard
from main.core.cloud_sketch_service import CloudSketchService, read_project_link
from main.core.credential_store import load_cloud_configuration, save_cloud_configuration


_CLOUD_UI_GATE = threading.Lock()


class CloudSketchPanel(QWidget):
    project_pulled = Signal(str)
    cloud_opened = Signal(str)
    _finished = Signal(int, object)

    def __init__(self, backend=None, parent=None, *, service=None):
        super().__init__(parent)
        self._backend = backend
        self._service = service or getattr(backend, "cloud_sketch_service", None) or CloudSketchService()
        self._closed = False
        self._initialized = False
        self._busy = False
        self._waiting_for_save = False
        self._generation = 0
        self._state = {}
        self._saved_login_exists = False
        self._cards = []
        self._forms = []
        self._job_buttons = []
        self._project_token = None
        self._held_editors = []
        self._reload_pending = None
        self._reload_running = False
        self.setObjectName("cloud-sketch-panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumSize(0, 0)
        self._setup_ui()
        self._finished.connect(self._finish_job, Qt.ConnectionType.QueuedConnection)
        self.apply_theme()
        self._update_controls()

    def _label(self, text, parent=None):
        label = QLabel(text, parent or self)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def _button(self, text, callback, parent=None):
        button = ActionButton(text, parent or self)
        button.clicked.connect(callback)
        self._job_buttons.append(button)
        return button

    def _card(self, parent):
        card = GlassCard(parent, radius=9)
        self._cards.append(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self._content_layout.addWidget(card)
        return card, layout

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(5)
        self._scroll = QScrollArea(self)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(self._scroll)
        content.setObjectName("cloud-content")
        content.setMinimumWidth(0)
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(0, 0, 4, 0)
        self._content_layout.setSpacing(7)
        self._scroll.setWidget(content)
        outer.addWidget(self._scroll, 1)

        self._content_layout.addStretch(1)
        self._account_card, account = self._card(content)
        account.setContentsMargins(10, 8, 10, 8)
        account.setSpacing(6)
        heading = QHBoxLayout()
        self._account_title = self._label("Cloud sketches", self._account_card)
        self._account_title.setObjectName("cloud-heading")
        heading.addWidget(self._account_title, 1)
        self._connection = self._label("Select Cloud to connect", self._account_card)
        heading.addWidget(self._connection, 1)
        account.addLayout(heading)
        self._account_summary = self._label("Sign in to keep your sketches in your private cloud account.", self._account_card)
        account.addWidget(self._account_summary)
        self._auth_pages = QWidget(self._account_card)
        auth_pages_layout = QVBoxLayout(self._auth_pages)
        auth_pages_layout.setContentsMargins(0, 0, 0, 0)
        self._login_fields = QWidget(self._auth_pages)
        login = QVBoxLayout(self._login_fields)
        login.setContentsMargins(0, 0, 0, 0)
        login.setSpacing(5)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(5)
        form.setHorizontalSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._forms.append(form)
        self._email = QLineEdit(self._login_fields)
        self._email.setPlaceholderText("Email address")
        self._email.setAccessibleName("Cloud account email")
        self._password = QLineEdit(self._login_fields)
        self._password.setPlaceholderText("Password")
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setAccessibleName("Cloud account password")
        self._password.returnPressed.connect(self._sign_in)
        form.addRow("Email", self._email)
        form.addRow("Password", self._password)
        login.addLayout(form)
        self._save_login = QCheckBox("Save credentials", self._account_card)
        self._save_login.setToolTip("Store your login encrypted in your operating system's credential vault")
        self._remember = QCheckBox("Remember me", self._account_card)
        self._remember.setToolTip("Restore your encrypted sign-in session on your next launch")
        self._sign_in_btn = self._button("Sign in", self._sign_in, self._login_fields)
        self._sign_in_btn.setProperty("cloudPrimary", True)
        self._create_btn = self._button("Create account", self._show_create_account, self._login_fields)
        auth_pages_layout.addWidget(self._login_fields)

        self._register_fields = QWidget(self._auth_pages)
        register = QVBoxLayout(self._register_fields)
        register.setContentsMargins(0, 0, 0, 0)
        register.setSpacing(5)
        register_heading = self._label("Create your cloud account", self._register_fields)
        register_heading.setObjectName("cloud-heading")
        register.addWidget(register_heading)
        register_form = QFormLayout()
        register_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        register_form.setContentsMargins(0, 0, 0, 0)
        register_form.setVerticalSpacing(5)
        self._forms.append(register_form)
        self._register_email = QLineEdit(self._register_fields)
        self._register_email.setPlaceholderText("Email address")
        self._register_email.setAccessibleName("New cloud account email")
        self._register_password = QLineEdit(self._register_fields)
        self._register_password.setPlaceholderText("At least 6 characters")
        self._register_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._register_password.setAccessibleName("New cloud account password")
        self._register_password.returnPressed.connect(self._create_account)
        self._register_confirm = QLineEdit(self._register_fields)
        self._register_confirm.setPlaceholderText("Enter password again")
        self._register_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._register_confirm.setAccessibleName("Confirm new cloud account password")
        self._register_confirm.returnPressed.connect(self._create_account)
        register_form.addRow("Email", self._register_email)
        register_form.addRow("Password", self._register_password)
        register_form.addRow("Confirm", self._register_confirm)
        register.addLayout(register_form)
        self._register_btn = self._button("Create account", self._create_account, self._register_fields)
        self._register_btn.setProperty("cloudPrimary", True)
        self._back_to_sign_in = self._button("Back to sign in", self._show_sign_in, self._register_fields)
        auth_pages_layout.addWidget(self._register_fields)
        self._register_fields.hide()
        account.addWidget(self._auth_pages)
        auth_options = QHBoxLayout()
        auth_options.setContentsMargins(0, 0, 0, 0)
        auth_options.addWidget(self._save_login)
        auth_options.addWidget(self._remember)
        auth_options.addStretch(1)
        account.addLayout(auth_options)
        self._auth_action_pages = QWidget(self._account_card)
        auth_actions_layout = QVBoxLayout(self._auth_action_pages)
        auth_actions_layout.setContentsMargins(0, 0, 0, 0)
        self._login_action_page = QWidget(self._auth_action_pages)
        self._sign_in_btn.setParent(self._login_action_page)
        self._create_btn.setParent(self._login_action_page)
        login_actions = QHBoxLayout(self._login_action_page)
        login_actions.setContentsMargins(0, 0, 0, 0)
        login_actions.addWidget(self._sign_in_btn)
        login_actions.addWidget(self._create_btn)
        auth_actions_layout.addWidget(self._login_action_page)
        self._register_action_page = QWidget(self._auth_action_pages)
        self._register_btn.setParent(self._register_action_page)
        self._back_to_sign_in.setParent(self._register_action_page)
        register_actions = QHBoxLayout(self._register_action_page)
        register_actions.setContentsMargins(0, 0, 0, 0)
        register_actions.addWidget(self._register_btn)
        register_actions.addWidget(self._back_to_sign_in)
        auth_actions_layout.addWidget(self._register_action_page)
        self._register_action_page.hide()
        account.addWidget(self._auth_action_pages)
        self._signed_in_actions = QWidget(self._account_card)
        self._signed_in_actions.hide()
        account_actions = QHBoxLayout(self._signed_in_actions)
        account_actions.setContentsMargins(0, 0, 0, 0)
        self._sign_out_btn = self._button("Sign out", self._sign_out, self._signed_in_actions)
        self._delete_account_btn = self._button("Delete account", self._delete_account, self._signed_in_actions)
        account_actions.addWidget(self._sign_out_btn)
        account_actions.addWidget(self._delete_account_btn)
        account_actions.addStretch()
        account.addWidget(self._signed_in_actions)
        self._security = self._label("Saved passwords and sessions use your OS credential vault. Cloud traffic uses HTTPS.", self._account_card)
        self._security.setObjectName("cloud-secondary")
        account.addWidget(self._security)
        self._forget_btn = self._button("Forget saved login", self._forget_login, self._account_card)
        self._forget_btn.hide()
        account.addWidget(self._forget_btn)
        self._auth_screen = 0

        self._management = QWidget(content)
        management = QVBoxLayout(self._management)
        management.setContentsMargins(0, 0, 0, 0)
        management.setSpacing(7)
        self._content_layout.addWidget(self._management)
        self._management.hide()

        sketches_card = GlassCard(self._management, radius=9)
        self._cards.append(sketches_card)
        sketches = QVBoxLayout(sketches_card)
        sketches.setContentsMargins(10, 8, 10, 8)
        sketches.setSpacing(6)
        management.addWidget(sketches_card)
        sketches.addWidget(self._label("Your sketches", sketches_card))
        self._sketches = QListWidget(sketches_card)
        self._sketches.setAccessibleName("Cloud sketch list")
        self._sketches.setMinimumHeight(105)
        self._sketches.setMaximumHeight(240)
        self._sketches.currentItemChanged.connect(self._selection_changed)
        self._sketches.itemDoubleClicked.connect(lambda *_: self._open_cloud())
        sketches.addWidget(self._sketches)
        self._empty = self._label("Sign in to see your sketches. Upload a local sketch to start.", sketches_card)
        sketches.addWidget(self._empty)
        self._linked = self._label("Open a cloud sketch in a separate workspace to push, pull or restore a revision.", sketches_card)
        sketches.addWidget(self._linked)
        self._history_box = QWidget(sketches_card)
        history_row = QHBoxLayout(self._history_box)
        history_row.setContentsMargins(0, 0, 0, 0)
        self._revisions = QComboBox(self._history_box)
        self._revisions.setMinimumWidth(0)
        self._revisions.setAccessibleName("Cloud revision to restore")
        history_row.addWidget(self._revisions, 1)
        self._restore_btn = self._button("Restore", self._restore_revision, self._history_box)
        history_row.addWidget(self._restore_btn)
        sketches.addWidget(self._history_box)
        self._history_box.hide()

        upload_card = GlassCard(self._management, radius=9)
        self._cards.append(upload_card)
        upload = QVBoxLayout(upload_card)
        upload.setContentsMargins(10, 8, 10, 8)
        upload.setSpacing(6)
        management.addWidget(upload_card)
        upload.addWidget(self._label("Upload a local project", upload_card))
        row = QHBoxLayout()
        self._local_path = QLineEdit(upload_card)
        self._local_path.setReadOnly(True)
        self._local_path.setMinimumWidth(0)
        self._local_path.setPlaceholderText("Choose a sketch folder")
        self._local_path.setAccessibleName("Local sketch folder to upload")
        self._local_path.setText(self._active_root())
        row.addWidget(self._local_path, 1)
        self._browse_btn = self._button("Browse", self._browse_local, upload_card)
        row.addWidget(self._browse_btn)
        upload.addLayout(row)
        self._name = QLineEdit(upload_card)
        self._name.setPlaceholderText("Cloud sketch name (optional)")
        self._name.setAccessibleName("Cloud sketch name")
        upload.addWidget(self._name)
        upload.addWidget(self._label("Root source files and text notes are uploaded. Build caches and credentials stay on this computer.", upload_card))

        self._configure_toggle = QCheckBox("Cloud connection settings", content)
        self._content_layout.addWidget(self._configure_toggle)
        self._configuration_card = GlassCard(content, radius=9)
        self._cards.append(self._configuration_card)
        configuration = QVBoxLayout(self._configuration_card)
        configuration.setContentsMargins(10, 8, 10, 8)
        configuration.setSpacing(6)
        self._content_layout.addWidget(self._configuration_card)
        config_form = QFormLayout()
        config_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._forms.append(config_form)
        self._api_key = QLineEdit(self._configuration_card)
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setAccessibleName("Firebase web API key")
        self._database_url = QLineEdit(self._configuration_card)
        self._database_url.setPlaceholderText("https://your-project-default-rtdb.firebaseio.com")
        self._database_url.setAccessibleName("Firebase realtime database URL")
        self._project_id = QLineEdit(self._configuration_card)
        self._project_id.setAccessibleName("Firebase project ID")
        config_form.addRow("API key", self._api_key)
        config_form.addRow("Database URL", self._database_url)
        config_form.addRow("Project ID", self._project_id)
        configuration.addLayout(config_form)
        configuration.addWidget(self._label("Saved encrypted in your operating system's credential vault, outside the application folder.", self._configuration_card))
        self._save_config_btn = self._button("Save connection", self._save_configuration, self._configuration_card)
        configuration.addWidget(self._save_config_btn)
        self._configuration_card.hide()
        self._configure_toggle.toggled.connect(self._configuration_visibility_changed)
        self._content_layout.addStretch()

        self._sync_actions = QWidget(self)
        self._sync_actions.hide()
        footer = QGridLayout(self._sync_actions)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        self._refresh_btn = self._button("Refresh", self._refresh)
        self._upload_btn = self._button("Upload local", self._upload_local)
        self._open_btn = self._button("Open sketch", self._open_cloud)
        self._open_btn.setProperty("cloudPrimary", True)
        self._open_btn.setToolTip("Open the selected cloud sketch in a separate workspace")
        self._open_btn.setAccessibleName("Open cloud sketch in another window")
        self._upload_btn.setToolTip("Save and upload the selected local sketch's root sources and notes")
        self._upload_btn.setAccessibleName("Upload local sketch to cloud")
        self._push_btn = self._button("Push", self._push_current)
        self._pull_btn = self._button("Pull", self._pull_current)
        self._history_btn = self._button("History", self._load_history)
        self._push_btn.setToolTip("Save and push the current linked sketch; conflicts never overwrite a newer cloud version")
        self._pull_btn.setToolTip("Restore the latest cloud version into the current linked sketch; keep a source recovery copy")
        for row_index, buttons in enumerate(((self._refresh_btn, self._upload_btn, self._open_btn),
                                             (self._push_btn, self._pull_btn, self._history_btn))):
            for column, button in enumerate(buttons):
                footer.addWidget(button, row_index, column)
                footer.setColumnStretch(column, 1)
        outer.addWidget(self._sync_actions)
        self._status = self._label("Sign in to manage cloud sketches.")
        self._status.setAccessibleName("Cloud operation status")
        outer.addWidget(self._status)
        for field in self.findChildren(QLineEdit):
            field.setMinimumWidth(0)
            field.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusIn:
            self._scroll.ensureWidgetVisible(watched, 8, 12)
        return super().eventFilter(watched, event)

    def _active_root(self):
        return str(getattr(self._backend, "sketch_dir_path", None) or "")

    def _backend_busy(self):
        backend = self._backend
        return bool(backend and (getattr(backend, "is_busy", False)
                    or getattr(backend, "active_operation", None) is not None
                    or getattr(backend, "_current_op_phase", None) is not None
                    or (getattr(backend, "_cloud_project_operation", None)
                        and getattr(backend, "_cloud_project_operation") != self._project_token)))

    def _can_change_project(self):
        if self._backend_busy():
            self._status.setText("Wait for the current project action to finish before synchronizing.")
            return False
        manager = getattr(self._backend, "ai_review_manager", None)
        if manager and manager.has_any_pending_ai_edits():
            self._status.setText("Accept or reject pending AI changes before synchronizing this sketch.")
            return False
        return not self._closed

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initialized and not self._closed:
            self._initialize()

    def _initialize(self):
        def initialize():
            if self._service.configured and not self._service.is_authenticated:
                self._service.restore_session()
        if self._request("initialize", "Checking cloud account…", initialize, include_saved=True, include_config=True):
            self._initialized = True

    def _snapshot(self, root, *, load_sketches=True, include_saved=False, include_config=False):
        service = self._service
        state = {"configured": service.configured, "authenticated": service.is_authenticated,
                 "account": service.account_info, "secure": service.secure_storage_status,
                 "root": root, "link": read_project_link(root) if root else None}
        if load_sketches or not state["authenticated"]:
            state["sketches"] = service.list_sketches() if state["authenticated"] else []
        if include_saved:
            state["saved_login"] = service.saved_login()
        if include_config:
            state["configuration"] = load_cloud_configuration(getattr(service, "_store", None))
        return state

    def _request(self, kind, message, operation, *, project_io=False, include_saved=False,
                 include_config=False, redactions=()):
        if self._closed or self._busy or self._waiting_for_save:
            return False
        if kind in {"refresh", "upload", "open", "push", "pull", "restore", "history"} \
                and not self._state.get("authenticated"):
            self._status.setText("Sign in to manage cloud sketches.")
            return False
        if project_io and not self._can_change_project():
            return False
        if not _CLOUD_UI_GATE.acquire(blocking=False):
            self._status.setText("Another cloud operation is running. Try again when it finishes.")
            return False
        self._busy = True
        self._generation += 1
        generation, root = self._generation, self._active_root()
        token = object() if project_io and self._backend else None
        self._project_token = token
        if token:
            self._backend._cloud_project_operation = token
            self._hold_editor()
            self._emit_project_phase(True)
        self._status.setText(message)
        self._update_controls()

        def run():
            result = {"kind": kind, "value": None, "state": {}, "error": "", "refresh_error": ""}
            def safe_message(exc):
                message = str(exc) or "The cloud operation could not complete. Try again."
                for secret in redactions:
                    if secret:
                        message = message.replace(secret, "[hidden]")
                return message[:1500]
            try:
                result["value"] = operation()
            except Exception as exc:
                result["error"] = safe_message(exc)
                if getattr(exc, "local_sources_changed", False):
                    result["sources_changed"] = True
                    result["value"] = root
            try:
                result["state"] = self._snapshot(root, include_saved=include_saved, include_config=include_config)
            except Exception as exc:
                result["refresh_error"] = safe_message(exc)
                try:
                    result["state"] = self._snapshot(root, load_sketches=False,
                        include_saved=include_saved, include_config=include_config)
                except Exception:
                    pass
            try:
                self._finished.emit(generation, result)
            except RuntimeError:
                if token and getattr(self._backend, "_cloud_project_operation", None) is token:
                    self._backend._cloud_project_operation = False
                _CLOUD_UI_GATE.release()
        try:
            threading.Thread(target=run, daemon=True, name="MCU_CloudSketch").start()
        except Exception:
            if token:
                self._backend._cloud_project_operation = False
                self._release_editor()
                self._emit_project_phase(False)
            _CLOUD_UI_GATE.release()
            self._busy = False
            self._status.setText("The cloud worker could not start. Try again.")
            self._update_controls()
            return False
        return True

    def _finish_job(self, generation, result):
        if self._closed or generation != self._generation:
            self._complete_job()
            return
        self._apply_state(result.get("state", {}))
        kind, value = result["kind"], result.get("value")
        if result.get("error") and result.get("sources_changed"):
            self._reload_pending = result
            self._reload_current()
            return
        if result.get("error"):
            self._status.setText(result["error"])
        elif kind == "open":
            if self._backend_busy():
                self._status.setText("Cloud sketch downloaded. Wait for the current action before opening it.")
            else:
                try:
                    response = self._backend.open_project_window(str(value)) if self._backend else {"success": False}
                    if response.get("success"):
                        self._status.setText("Cloud sketch opened in its own workspace.")
                        self._complete_job()
                        self.cloud_opened.emit(str(value))
                        return
                    else:
                        self._status.setText(response.get("error", "The cloud sketch window could not open."))
                except Exception:
                    self._status.setText("The cloud sketch window could not open. Try again.")
        elif kind in {"pull", "restore"}:
            self._reload_pending = result
            self._reload_current()
            return
        elif kind == "history":
            self._revisions.clear()
            for row in (value or [])[:20]:
                when = self._display_time(row.get("created_at"))
                self._revisions.addItem(f"Revision {row['revision']}   {when}", row["revision"])
            self._history_box.setVisible(bool(self._revisions.count()))
            self._scroll.ensureWidgetVisible(self._history_box, 8, 12)
            self._status.setText("Choose a revision, then Restore to revert the current cloud sketch.")
        elif kind == "configuration":
            self._status.setText("Cloud connection saved in your encrypted credential vault.")
        elif kind == "upload":
            self._status.setText("Local sketch uploaded. Open it from the cloud list in its own workspace.")
        elif kind == "push":
            self._status.setText(f"Cloud sketch updated to revision {(value or {}).get('revision', '')}.")
            refresh_link = getattr(self._backend, "refresh_cloud_project_link", None)
            if callable(refresh_link):
                refresh_link()
        elif kind in {"sign_in", "create"}:
            self._password.clear()
            self._register_password.clear()
            self._register_confirm.clear()
            self._status.setText("Signed in. Cloud sketches are ready.")
        elif kind in {"sign_out", "forget", "delete"}:
            self._password.clear()
            self._history_box.hide()
            self._status.setText("Account and cloud sketches deleted." if kind == "delete" else
                                 "Signed out and saved login removed." if kind == "forget" else "Signed out.")
        else:
            self._status.setText("Cloud sketches are ready." if self._state.get("authenticated") else
                                 "Sign in to open or upload cloud sketches." if self._state.get("configured") else
                                 "Set up the cloud connection, then sign in or create an account.")
        if result.get("refresh_error") and not result.get("error"):
            self._status.setText(self._status.text() + " Cloud list refresh failed; use Refresh to reconnect.")
        self._complete_job()

    def _complete_job(self, *, restart_autosave=True):
        token = self._project_token
        if token and getattr(self._backend, "_cloud_project_operation", None) is token:
            self._backend._cloud_project_operation = False
            self._emit_project_phase(False)
        self._project_token = None
        _CLOUD_UI_GATE.release()
        self._busy = False
        self._release_editor(restart_autosave=restart_autosave)
        if not self._closed:
            self._update_controls()

    def _pull_handler(self):
        ancestor = self.parentWidget()
        while ancestor is not None:
            handler = getattr(ancestor, "_on_cloud_project_pulled", None)
            if callable(handler):
                return handler
            ancestor = ancestor.parentWidget()
        return None

    def _reload_current(self):
        result = self._reload_pending
        if not result or self._reload_running:
            return
        self._reload_running = True
        self._status.setText("Cloud sources restored. Reloading editor buffers…")
        self._update_controls()
        def success():
            if self._reload_pending is not result:
                return
            self._reload_running = False
            self._reload_pending = None
            self._status.setText((result.get("error") or "Cloud sources restored. A recovery copy of the previous source files was kept.")
                + (" Cloud list refresh failed; use Refresh to reconnect." if result.get("refresh_error") else ""))
            self.project_pulled.emit(str(result["value"]))
            self._complete_job(restart_autosave=False)
        def failure():
            self._reload_running = False
            self._status.setText("Cloud files were restored, but editor reload failed. Use Retry reload before editing or closing.")
            self._update_controls()
        handler = self._pull_handler()
        try:
            if handler:
                handler(str(result["value"]), callback=success, failure_callback=failure)
            elif self._editor() is not None:
                self._editor().reload_cloud_snapshot(callback=success, failure_callback=failure)
            else:
                success()
        except Exception:
            failure()

    def _editor(self):
        ancestor = self.parentWidget()
        while ancestor is not None:
            editor = getattr(ancestor, "_editor_panel", None)
            if editor is not None:
                return editor
            ancestor = ancestor.parentWidget()
        return None

    def _hold_editor(self):
        editor = self._editor()
        if editor is None:
            return
        timer = getattr(editor, "_autosave_timer", None)
        remaining = timer.remainingTime() if timer and timer.isActive() else -1
        enabled = editor.isEnabled()
        autosave = getattr(editor, "_autosave_enabled", False)
        self._held_editors.append((editor, enabled, autosave, remaining))
        if timer:
            timer.stop()
        editor._autosave_enabled = False
        editor.setEnabled(False)

    def _release_editor(self, *, restart_autosave=True):
        for editor, enabled, autosave, remaining in self._held_editors:
            try:
                editor.setEnabled(enabled)
                editor._autosave_enabled = autosave
                if restart_autosave and autosave and remaining >= 0:
                    editor._autosave_timer.start(max(1, remaining))
            except RuntimeError:
                pass
        self._held_editors.clear()

    def _emit_project_phase(self, busy):
        emit = getattr(self._backend, "emit", None)
        if callable(emit):
            emit("operation:phase", {"phase": "cloud_sync" if busy else "idle",
                 "op": "cloud_sync" if busy else "", "is_busy": busy, "can_stop": False})

    @staticmethod
    def _display_time(value):
        try:
            return datetime.fromtimestamp(float(value) / 1000).strftime("%b %d, %Y %H:%M")
        except (ValueError, OSError, TypeError):
            return ""

    def _apply_state(self, state):
        self._state.update({key: value for key, value in state.items()
                            if key not in {"saved_login", "configuration"}})
        authenticated = self._state.get("authenticated", False)
        self._auth_pages.setVisible(not authenticated)
        self._auth_action_pages.setVisible(not authenticated)
        self._management.setVisible(authenticated)
        self._sync_actions.setVisible(authenticated)
        self._signed_in_actions.setVisible(authenticated)
        self._configure_toggle.setVisible(not authenticated)
        self._configuration_card.setVisible(not authenticated and self._configure_toggle.isChecked())
        self._connection.setText("Signed in" if authenticated else "Sign in required" if self._state.get("configured") else "Connection not configured")
        email = self._state.get("account", {}).get("email", "")
        self._account_summary.setText(f"Signed in as {email}" if authenticated else
                                      "Sign in to access your private cloud sketches.")
        secure, detail = self._state.get("secure", (False, "Credential storage is unavailable."))
        self._save_login.setEnabled(secure)
        self._remember.setEnabled(secure)
        self._security.setText("Login encryption: " + detail)
        if not secure:
            self._save_login.setChecked(False)
            self._remember.setChecked(False)
        if "saved_login" in state:
            saved = state["saved_login"]
            self._saved_login_exists = bool(saved)
            self._email.setText(saved.get("email", "") or email)
            self._password.setText(saved.get("password", ""))
            self._register_email.setText(saved.get("email", ""))
            self._save_login.setChecked(bool(saved) and secure)
            self._remember.setChecked(bool(self._state.get("account", {}).get("remember_me")) and secure)
            self._forget_btn.setVisible(bool(saved) and secure)
        if authenticated:
            self._set_auth_screen(0)
        storage_warning = self._state.get("account", {}).get("storage_warning", "")
        if storage_warning and not self._state.get("secure", (False, ""))[0]:
            self._security.setText("Login encryption: " + storage_warning)
        if "configuration" in state:
            cfg = state["configuration"]
            self._api_key.setText(cfg.get("firebase_api_key", ""))
            self._database_url.setText(cfg.get("firebase_database_url", ""))
            self._project_id.setText(cfg.get("firebase_project_id", ""))
            self._configure_toggle.setChecked(not self._state.get("configured"))
        if "sketches" in state:
            selected = self._selected_sketch().get("id")
            self._sketches.clear()
            target = None
            for row in state["sketches"][:100]:
                item = QListWidgetItem(f"{row.get('name', 'Cloud sketch')}\nRevision {row.get('revision', '')}   {row.get('file_count', 0)} files")
                item.setData(Qt.ItemDataRole.UserRole, row)
                item.setIcon(icon("cloud", self._palette["CYAN"]))
                item.setToolTip(f"Updated {self._display_time(row.get('updated_at'))}\nCloud sketch: {row.get('id', '')}")
                self._sketches.addItem(item)
                if row.get("id") == selected:
                    target = item
            self._sketches.setCurrentItem(target or self._sketches.item(0))
            self._empty.setVisible(not self._sketches.count())
            self._empty.setText("No cloud sketches yet. Choose a local project and Upload local." if authenticated else "Sign in to see your sketches.")
        link = self._state.get("link")
        self._linked.setText(f"Current project: {Path(self._state.get('root', '')).name}   Cloud revision {link['revision']}" if link else
                             "Push, Pull and History apply to the current cloud-linked project.")
        self._update_controls()

    def _selected_sketch(self):
        item = self._sketches.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else {}

    def _selection_changed(self, *_):
        self._update_controls()

    def _update_controls(self):
        idle = not (self._closed or self._busy or self._waiting_for_save)
        authenticated = self._state.get("authenticated", False)
        for button in self._job_buttons:
            button.setEnabled(idle)
        for field in (self._email, self._password, self._api_key, self._database_url, self._project_id, self._name):
            field.setEnabled(idle)
        for field in (self._register_email, self._register_password, self._register_confirm):
            field.setEnabled(idle)
        self._sign_in_btn.setEnabled(idle and self._state.get("configured", False))
        self._create_btn.setEnabled(self._sign_in_btn.isEnabled())
        self._register_btn.setEnabled(idle and self._state.get("configured", False))
        self._back_to_sign_in.setEnabled(idle)
        self._open_btn.setEnabled(idle and authenticated and bool(self._selected_sketch()) and not self._backend_busy())
        self._upload_btn.setEnabled(idle and authenticated and not self._backend_busy())
        link = self._state.get("link")
        linked_account = bool(link and link.get("uid") == self._state.get("account", {}).get("uid"))
        for button in (self._push_btn, self._pull_btn, self._history_btn):
            button.setEnabled(idle and authenticated and linked_account and not self._backend_busy())
        self._restore_btn.setEnabled(self._pull_btn.isEnabled() and self._revisions.count() > 0)
        secure = self._state.get("secure", (False, ""))[0]
        self._save_login.setEnabled(idle and secure)
        self._remember.setEnabled(idle and secure)
        self._save_config_btn.setEnabled(idle and secure and not authenticated)
        self._forget_btn.setEnabled(idle and secure)
        self._forget_btn.setText("Sign out and forget" if authenticated else "Forget saved login")
        self._forget_btn.setVisible(secure and self._saved_login_exists)
        self._forget_btn.setEnabled(idle and secure and self._saved_login_exists)
        self._configure_toggle.setEnabled(idle and not authenticated)
        self._refresh_btn.setText("Retry reload" if self._reload_pending else "Refresh")
        if self._reload_pending:
            self._refresh_btn.setEnabled(not self._reload_running)

    def _sign_in(self):
        self._authenticate(False)

    def _show_create_account(self):
        if not self._state.get("configured"):
            self._status.setText("Set up the cloud connection before creating an account.")
            return
        if not self._register_email.text().strip():
            self._register_email.setText(self._email.text().strip())
        self._register_password.clear()
        self._register_confirm.clear()
        self._set_auth_screen(1)
        self._register_email.setFocus()

    def _show_sign_in(self):
        self._set_auth_screen(0)

    def _set_auth_screen(self, index):
        self._auth_screen = 1 if index else 0
        self._login_fields.setVisible(self._auth_screen == 0)
        self._register_fields.setVisible(self._auth_screen == 1)
        self._login_action_page.setVisible(self._auth_screen == 0)
        self._register_action_page.setVisible(self._auth_screen == 1)
        self._auth_pages.updateGeometry()
        self._auth_action_pages.updateGeometry()

    def _create_account(self):
        self._authenticate(True)

    def _authenticate(self, create):
        email = (self._register_email.text() if create else self._email.text()).strip()
        password = self._register_password.text() if create else self._password.text()
        if not email or not password:
            self._status.setText("Enter your email address and password.")
            return
        if create and password != self._register_confirm.text():
            self._status.setText("The two passwords do not match.")
            self._register_confirm.setFocus()
            return
        save, remember = self._save_login.isChecked(), self._remember.isChecked()
        operation = self._service.create_account if create else self._service.sign_in
        self._request("create" if create else "sign_in", "Creating account…" if create else "Signing in…",
                      lambda: operation(email, password, save_login=save, remember_me=remember),
                      include_saved=True, redactions=(password, self._register_confirm.text()))

    def _sign_out(self):
        self._request("sign_out", "Signing out…", self._service.sign_out)

    def _forget_login(self):
        self._request("forget", "Removing saved login…", lambda: self._service.sign_out(forget_saved=True), include_saved=True)

    def _delete_account(self):
        if not self._state.get("authenticated"):
            self._status.setText("Sign in before deleting a cloud account.")
            return
        if QMessageBox.question(self, "Delete cloud account", "Permanently delete this account and all its cloud sketches? Local sketch files are kept.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        password, accepted = QInputDialog.getText(self, "Confirm account deletion", "Enter your account password:", QLineEdit.EchoMode.Password)
        if accepted and password and not self._closed:
            self._request("delete", "Deleting cloud account…", lambda: self._service.delete_account(password), redactions=(password,))

    def _save_configuration(self):
        if self._state.get("authenticated"):
            self._status.setText("Sign out before changing the cloud connection.")
            return
        cfg = {"firebase_api_key": self._api_key.text().strip(), "firebase_database_url": self._database_url.text().strip(),
               "firebase_project_id": self._project_id.text().strip()}
        def save():
            from urllib.parse import urlsplit
            parsed = urlsplit(cfg["firebase_database_url"])
            if not all(cfg.values()) or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Enter an API key, project ID and HTTPS realtime database URL.")
            save_cloud_configuration(cfg, getattr(self._service, "_store", None))
        self._request("configuration", "Saving cloud connection…", save, include_config=True, redactions=(cfg["firebase_api_key"],))

    def _configuration_visibility_changed(self, visible):
        self._configuration_card.setVisible(bool(visible) and not self._state.get("authenticated", False))

    def _refresh(self):
        if not self._state.get("authenticated"):
            self._status.setText("Sign in to manage cloud sketches.")
            return
        if self._reload_pending:
            self._reload_current()
        elif not self._initialized:
            self._initialize()
        else:
            self._request("refresh", "Refreshing cloud sketches…", lambda: None)

    def _browse_local(self):
        path = QFileDialog.getExistingDirectory(self, "Choose a local sketch project", self._local_path.text() or self._active_root())
        if path:
            self._local_path.setText(path)

    def _with_saved_project(self, root, callback):
        if not self._can_change_project() or self._busy or self._waiting_for_save:
            return
        current = self._active_root()
        dirty = bool(self._backend and any(getattr(self._backend, "modified_files", {}).values()))
        if Path(root) != Path(current) or not dirty:
            callback()
            return
        editor = self._editor()
        save_all = getattr(editor, "trigger_save_all", None)
        if not callable(save_all):
            self._status.setText("Save the current sketch in the editor before uploading or pushing.")
            return
        self._waiting_for_save = True
        self._status.setText("Saving editor changes before synchronizing…")
        self._update_controls()
        def saved():
            self._waiting_for_save = False
            if not self._closed and self._active_root() == current and self._can_change_project():
                callback()
            self._update_controls()
        def failed():
            self._waiting_for_save = False
            if not self._closed:
                self._status.setText("Save All failed or timed out. No cloud sources were changed.")
                self._update_controls()
        try:
            save_all(callback=saved, failure_callback=failed)
        except Exception:
            failed()

    def _upload_local(self):
        if not self._state.get("authenticated"):
            self._status.setText("Sign in to upload a local sketch.")
            return
        root, name = self._local_path.text().strip(), self._name.text().strip() or None
        if not root:
            self._status.setText("Choose a local sketch folder to upload.")
            return
        def upload():
            from main.core.constants import is_application_codebase_dir
            if is_application_codebase_dir(root):
                raise ValueError("Choose a sketch folder; the MCU Flasher application cannot be uploaded as a sketch.")
            return self._service.upload_project(root, name=name)
        self._with_saved_project(root, lambda: self._request("upload", "Uploading local sketch…", upload, project_io=True))

    def _open_cloud(self):
        if not self._state.get("authenticated"):
            self._status.setText("Sign in to open cloud sketches.")
            return
        row = self._selected_sketch()
        if not row or self._backend_busy():
            return
        sketch_id = row["id"]
        def download():
            from main.core.config import find_project_window
            working = self._service.working_directory(sketch_id)
            if find_project_window(str(working)) or read_project_link(working):
                return working
            return self._service.pull_project(sketch_id)
        self._request("open", "Opening cloud sketch in another workspace…", download)

    def _current_link(self):
        root = self._active_root()
        if root != self._state.get("root") or not self._state.get("link"):
            self._status.setText("Refresh cloud status after opening a cloud-linked project.")
            return None
        return dict(self._state["link"])

    def _push_current(self):
        if not self._state.get("authenticated"):
            self._status.setText("Sign in to push cloud changes.")
            return
        link = self._current_link()
        if link:
            root = self._active_root()
            self._with_saved_project(root, lambda: self._request("push", "Pushing saved sketch…",
                lambda: self._service.push_project(root, link["sketch_id"], link["revision"]), project_io=True))

    def _pull_current(self):
        self._pull_revision(None)

    def _restore_revision(self):
        revision = self._revisions.currentData()
        if revision is not None:
            self._pull_revision(revision)

    def _pull_revision(self, revision):
        if not self._state.get("authenticated"):
            self._status.setText("Sign in to pull cloud changes.")
            return
        link = self._current_link()
        if not link or not self._can_change_project():
            return
        root = self._active_root()
        title = f"Restore revision {revision}" if revision else "Pull latest cloud sketch"
        if QMessageBox.question(self, title, "Replace this sketch's linked source files with the cloud version? Unsaved editor changes will be discarded. A recovery copy of saved source files is kept.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        if root != self._active_root() or not self._can_change_project():
            return
        def pull():
            watcher = getattr(self._backend, "ai_watcher", None)
            kwargs = {"destination": root, "revision": revision}
            if watcher:
                kwargs["source_guard"] = lambda paths: watcher.user_file_operation(*paths)
            return self._service.pull_project(link["sketch_id"], **kwargs)
        self._request("restore" if revision else "pull", "Restoring cloud source files…", pull, project_io=True)

    def _load_history(self):
        if not self._state.get("authenticated"):
            self._status.setText("Sign in to view cloud history.")
            return
        link = self._current_link()
        if link:
            self._request("history", "Loading cloud revision history…", lambda: self._service.list_revisions(link["sketch_id"]))

    def dispose(self):
        self._closed = True
        self._generation += 1
        self._password.clear()
        self._api_key.clear()

    def closeEvent(self, event):
        if self._busy or self._waiting_for_save:
            self._status.setText("Wait for the cloud operation to finish before closing this view.")
            event.ignore()
            return
        self.dispose()
        super().closeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = event.size().width() < 480
        if hasattr(self, "_forms"):
            for form in self._forms:
                form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows if compact else QFormLayout.RowWrapPolicy.DontWrapRows)
        if hasattr(self, "_open_btn"):
            self._open_btn.setText("Open" if compact else "Open sketch")
            self._upload_btn.setText("Upload" if compact else "Upload local")

    def apply_theme(self, mode=None):
        from main.qt.theme import get_palette
        from main.core.theme import Theme
        from src.modules.ui_palette import readable_foreground, setup_text_palette
        original = get_palette(mode or Theme.active_theme)
        adjusted = setup_text_palette({"T_" + key: value for key, value in original.items()}, glass=True)
        pal = self._palette = dict(original)
        pal.update({key[2:]: value for key, value in adjusted.items() if key.startswith("T_")})
        for card in self._cards:
            card.set_palette(original)
        primary = readable_foreground(pal["BTN_COMPILE"])
        self.setStyleSheet(f"""
            QWidget#cloud-sketch-panel {{ background: {pal['BG_DARKEST']}; }}
            QWidget#cloud-content, QScrollArea {{ background: transparent; }}
            QLabel {{ color: {pal['TEXT']}; background: transparent; font-size: 12px; }}
            QLabel#cloud-heading {{ font-size: 15px; font-weight: 700; color: {pal['TEXT_BRIGHT']}; }}
            QLabel#cloud-secondary {{ color: {pal['TEXT_DIM']}; font-size: 11px; }}
            QLineEdit, QComboBox {{ color: {pal['TEXT_BRIGHT']}; background: {pal['BG_DARKEST']}; border: 1px solid {pal['BORDER']}; border-radius: 6px; padding: 7px; }}
            QLineEdit:focus, QComboBox:focus, QListWidget:focus {{ border-color: {pal['CYAN']}; }}
            QCheckBox {{ color: {pal['TEXT']}; background: transparent; font-size: 12px; }}
            QListWidget {{ color: {pal['TEXT']}; background: {pal['BG_DARKEST']}; border: 1px solid {pal['BORDER']}; border-radius: 6px; }}
            QListWidget::item {{ padding: 7px; }}
            QListWidget::item:selected {{ background: {pal['BG_HOVER']}; color: {pal['TEXT_BRIGHT']}; }}
            QPushButton {{ color: {pal['TEXT_BRIGHT']}; background: {pal['BG_MID']}; border: 1px solid {pal['BORDER']}; border-radius: 6px; padding: 7px 10px; font-size: 12px; font-weight: 600; }}
            QPushButton:hover {{ background: {pal['BG_HOVER']}; border-color: {pal['BORDER_LIT']}; }}
            QPushButton:focus {{ border: 2px solid {pal['CYAN']}; padding: 6px 9px; }}
            QPushButton[cloudPrimary="true"]:enabled {{ background: {pal['BTN_COMPILE']}; color: {primary}; border-color: {pal['BORDER_LIT']}; }}
            QPushButton:disabled {{ color: {pal['TEXT_DIM']}; background: {pal['BG_DARK']}; }}
        """)
        for field in self.findChildren(QLineEdit):
            field.setMinimumHeight(max(32, field.fontMetrics().height() + 18))
        for button in self._job_buttons:
            if button._icon_name:
                button.setIcon(icon(button._icon_name, primary if button.property("cloudPrimary") else pal["TEXT_BRIGHT"]))
        for index in range(self._sketches.count()):
            self._sketches.item(index).setIcon(icon("cloud", pal["CYAN"]))


class CloudSketchDialog(QDialog):
    def __init__(self, backend, parent=None, *, on_pulled=None):
        super().__init__(parent)
        self.setWindowTitle("Cloud sketches — MCU Flasher")
        self.setModal(True)
        self._glass = GlassCard(self, radius=0)
        self._glass.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._glass.lower()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.panel = CloudSketchPanel(backend, self)
        layout.addWidget(self.panel)
        if on_pulled and self.panel._pull_handler() is None:
            self.panel.project_pulled.connect(on_pulled)
        from main.qt.responsive import fit_dialog, ScreenWatcher
        fit_dialog(self, (720, 650), (360, 300))
        self._screen_watcher = ScreenWatcher(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_glass"):
            self._glass.setGeometry(self.rect())

    def done(self, code):
        if self.panel._busy or self.panel._waiting_for_save:
            self.panel._status.setText("Wait for the cloud operation to finish before closing this view.")
            return
        self.panel.dispose()
        super().done(code)


def show_cloud_dialog(backend, parent=None, *, on_pulled=None):
    dialog = CloudSketchDialog(backend, parent, on_pulled=on_pulled)
    dialog.exec()
    dialog.deleteLater()
    return dialog
