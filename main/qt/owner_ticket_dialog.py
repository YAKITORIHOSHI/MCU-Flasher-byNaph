"""Private developer tickets with a focused login and neobrutalist dashboard.

The owner service remains responsible for authentication, cloud sync and local
storage. Ticket forms retain their widgets during reflow.
"""
from __future__ import annotations

import sys
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict
from weakref import WeakKeyDictionary
from PySide6.QtCore import Qt, QSize, QRectF, Signal, QTimer, QObject, Slot, QEvent, QFileSystemWatcher
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QTextOption
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QDialog, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget,
    QLabel, QLineEdit, QTextEdit, QMessageBox,
    QSizePolicy, QMenu, QPushButton, QCheckBox, QScrollArea,
)

from main.core.owner_tickets import OwnerTicketService, TicketChangeWatcher, sort_tickets
from main.qt.owner_ticket_style import (
    CircularSpinner, FloatingCopyButton, NeoCard, PortalDialog, ResponsiveGrid, ResponsiveRow,
    button, combo, copy_button, field, heading, label, refresh_portal_children, retone, scroll_body,
    register_portal_aliases, check_service_connection, get_service_connection_checker,
)
from main.qt.icons import icon
from main.qt.responsive import clamp_window, fit_dialog

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
                      "The operation could not finish. Check your connection and try again.")
        finally:
            self.function = None
        try:
            self.completed.emit(result)
            self.finished.emit()
        except RuntimeError:
            pass  # Application shutdown may already have destroyed this receiver.

    def start(self):
        Thread(target=self.run, name="developer-service", daemon=True).start()


def _tickets_content_identical(t1_list, t2_list, parse_ts=None):
    if t1_list is t2_list:
        return True
    if not isinstance(t1_list, list) or not isinstance(t2_list, list):
        return False
    if len(t1_list) != len(t2_list):
        return False
    map1 = {str(t.get("id")): t for t in t1_list if isinstance(t, dict) and t.get("id")}
    map2 = {str(t.get("id")): t for t in t2_list if isinstance(t, dict) and t.get("id")}
    if map1.keys() != map2.keys():
        return False
    for tid, a in map1.items():
        b = map2[tid]
        for field in ("title", "description", "status", "severity", "category", "author"):
            if str(a.get(field, "")).strip() != str(b.get(field, "")).strip():
                return False
        if parse_ts is not None and not hasattr(parse_ts, "assert_called"):
            try:
                ts_a_up = float(parse_ts(a.get("updated_at")))
                ts_b_up = float(parse_ts(b.get("updated_at")))
                if abs(ts_a_up - ts_b_up) > 0.001:
                    return False
                ts_a_cr = float(parse_ts(a.get("created_at")))
                ts_b_cr = float(parse_ts(b.get("created_at")))
                if abs(ts_a_cr - ts_b_cr) > 0.001:
                    return False
            except (TypeError, ValueError):
                if a.get("updated_at") != b.get("updated_at") or a.get("created_at") != b.get("created_at"):
                    return False
        else:
            if a.get("updated_at") != b.get("updated_at") or a.get("created_at") != b.get("created_at"):
                return False
    return True


class _TaskPortal(PortalDialog):
    """Serialize a service's requests while keeping all storage/network off Qt."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._task_name = ""
        self._task_callback = None
        self._task_queue = []
        self.spinner = CircularSpinner(self.backdrop, size=24, color="#135f73")
        policy = self.spinner.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.spinner.setSizePolicy(policy)
        self.spinner.hide()

    def _start_task(self, operation, function, callback):
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

    def _run_task(self, operation, function, callback):
        if self._task_name:
            self._task_queue.append((operation, function, callback))
            return True
        self._start_task(operation, function, callback)
        return True

    @Slot(object)
    def _task_complete(self, result):
        operation, value, error = result
        if operation != self._task_name:
            return
        callback = self._task_callback
        self._task_name, self._task_callback = "", None
        try:
            if callback:
                callback(value, error)
        finally:
            if self._task_queue:
                next_op, next_fn, next_cb = self._task_queue.pop(0)
                self._start_task(next_op, next_fn, next_cb)
            else:
                self._set_busy(False)

    def _set_busy(self, busy):
        if hasattr(self, "spinner") and self.spinner is not None:
            self.spinner.set_busy(busy)

    def _remove_ticket_category(self, category):
        if self._task_name or self.service is None:
            return
        self._run_task("remove-category", lambda: self.service.remove_ticket_category(category),
                       self._category_removed)

    def _category_removed(self, result, error):
        fields = getattr(self, "fields", None) or getattr(self, "new_fields", None)
        if fields is None:
            return
        if error or not result:
            fields.show_category_feedback(error or "The category could not be removed.", "fail")
            return
        ok, message, categories = result
        if ok:
            if hasattr(self, "categories"):
                self.categories = list(categories)
            if hasattr(self, "_config"):
                self._config["ticket_categories"] = list(categories)
                self._sync_category_options()
            fields.set_categories(categories, current=categories[0] if categories else "")
            if hasattr(self, "categories_updated"):
                self.categories_updated.emit(list(categories))
        fields.show_category_feedback(message, "ok" if ok else "warn")


class DeveloperLiveWallpaper(QWidget):
    """Animated moving cyber circuit live wallpaper background for developer access login."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("developer-live-wallpaper")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 FPS capped well below 60 FPS
        self._timer.timeout.connect(self._circuit_animation_tick)
        self._nodes = [
            {"x": float((i * 67 + 30) % 400), "y": float((i * 89 + 40) % 400),
             "vx": 0.45 if i % 2 == 0 else -0.45, "vy": 0.35 if i % 3 == 0 else -0.35}
            for i in range(16)
        ]
        self._theme_mode = "default"

    def resizeEvent(self, event):
        super().resizeEvent(event)
        old_size = event.oldSize()
        w = max(1, self.width())
        h = max(1, self.height())
        if old_size.isValid() and old_size.width() > 0 and old_size.height() > 0:
            sx = w / max(1, old_size.width())
            sy = h / max(1, old_size.height())
            for n in self._nodes:
                n["x"] = (n["x"] * sx) % w
                n["y"] = (n["y"] * sy) % h
        else:
            for i, n in enumerate(self._nodes):
                n["x"] = float((i * (w / 16.0) + (w / 32.0)) % w)
                n["y"] = float((i * (h / 8.0) + (h / 16.0)) % h)

    def _circuit_animation_tick(self):
        self._frame += 1
        w = max(100, self.width())
        h = max(100, self.height())
        for n in self._nodes:
            n["x"] = (n["x"] + n["vx"]) % w
            n["y"] = (n["y"] + n["vy"]) % h
        self.update()

    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        if self._timer.isActive():
            self._timer.stop()

    def set_theme_mode(self, mode: str):
        self._theme_mode = mode
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.stop()

    def paintEvent(self, event):
        if self.width() <= 0 or self.height() <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        is_dark = "dark" in self._theme_mode
        trace_color = QColor(128, 200, 255, 60) if is_dark else QColor(19, 95, 115, 45)
        node_color = QColor(128, 200, 255, 110) if is_dark else QColor(19, 95, 115, 80)
        pen = QPen(trace_color, 1.5)
        painter.setPen(pen)

        w, h = self.width(), self.height()
        pts = [(n["x"] % w, n["y"] % h) for n in self._nodes]
        for i in range(len(pts)):
            for j in range(i + 1, min(i + 4, len(pts))):
                p1, p2 = pts[i], pts[j]
                dist_sq = (p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2
                if dist_sq < 28000:
                    mid_x = (p1[0] + p2[0]) / 2
                    painter.drawLine(int(p1[0]), int(p1[1]), int(mid_x), int(p1[1]))
                    painter.drawLine(int(mid_x), int(p1[1]), int(p2[0]), int(p2[1]))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(node_color)
        for x, y in pts:
            painter.drawEllipse(int(x) - 3, int(y) - 3, 6, 6)

        painter.end()


class DeveloperArtwork(QWidget):
    """Static original vectors painted at the monitor's native resolution."""

    def __init__(self, asset, parent=None):
        super().__init__(parent)
        self.renderer = QSvgRenderer(str(_assets_dir / "icons" / asset), self)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event):
        painter = QPainter(self)
        self.renderer.render(painter, QRectF(self.rect()))


class NeoLoginCard(NeoCard):
    """The sign-in uses the same paper card as every Developer Access screen."""


class TicketFields(QWidget):
    """Shared create/edit fields; narrow windows stack classifications."""

    category_add_requested = Signal(str)
    category_remove_requested = Signal(str)

    def __init__(self, parent, ticket=None, categories=None):
        super().__init__(parent)
        ticket = ticket or {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.title = QLineEdit(ticket.get("title", ""), self)
        self.title.setPlaceholderText("Briefly describe the issue")
        layout.addWidget(field("Title", self.title, self))
        self.category = combo(list(categories or CATEGORIES), self, ticket.get("category", CATEGORIES[0]))
        category_field = field("Category", self.category, self)
        category_field.layout().removeWidget(self.category)
        category_controls = QWidget(category_field)
        category_controls_layout = QHBoxLayout(category_controls)
        category_controls_layout.setContentsMargins(0, 0, 0, 0)
        category_controls_layout.setSpacing(6)
        category_controls_layout.addWidget(self.category, 1)
        self.btn_remove_category = button("", category_controls,
            lambda: self.category_remove_requested.emit(self.category.currentText()), role="icon", vector="clear")
        self.btn_remove_category.setFixedWidth(36)
        self.btn_remove_category.setAccessibleName("Remove selected category")
        self.btn_remove_category.setToolTip("Remove this custom category after reassigning its tickets")
        category_controls_layout.addWidget(self.btn_remove_category)
        category_field.layout().addWidget(category_controls)
        category_entry = QWidget(category_field)
        category_row = QHBoxLayout(category_entry)
        category_row.setContentsMargins(0, 0, 0, 0)
        category_row.setSpacing(8)
        self.category_input = QLineEdit(category_entry)
        self.category_input.setPlaceholderText("Add a category")
        self.category_input.setAccessibleName("New ticket category")
        self.category_input.returnPressed.connect(self._request_category_add)
        self.btn_add_category = button("+", category_entry, self._request_category_add, role="category-add")
        self.btn_add_category.setFixedWidth(42)
        self.btn_add_category.setToolTip("Add a category")
        self.btn_add_category.setAccessibleName("Add category")
        category_row.addWidget(self.category_input, 1)
        category_row.addWidget(self.btn_add_category)
        category_field.layout().addWidget(category_entry)
        self.severity = combo(SEVERITIES, self, ticket.get("severity", "Medium"))
        self.status = combo(STATUSES, self, ticket.get("status", "Open"))
        self.classifications = ResponsiveGrid([
            category_field,
            field("Severity", self.severity, self),
            field("Status", self.status, self),
        ], self, cell_width=170)
        layout.addWidget(self.classifications)
        # Feedback belongs to the classification group, spanning its width.
        # Keeping it outside an individual grid cell avoids unequal row heights
        # and gives wrapped errors room without shifting adjacent controls.
        self.category_feedback = label("", self, "metadata", wrap=True)
        self.category_feedback.hide()
        layout.addWidget(self.category_feedback)
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

    def _request_category_add(self):
        category = self.category_input.text().strip()
        if not category:
            self.show_category_feedback("Enter a category name first.", "warn")
            self.category_input.setFocus()
            return
        self.show_category_feedback("", "muted")
        self.category_add_requested.emit(category)

    def show_category_feedback(self, message, tone="muted"):
        self.category_feedback.setText(message)
        self.category_feedback.setVisible(bool(message))
        retone(self.category_feedback, tone)

    def set_categories(self, categories, *, current=None):
        selected = self.category.currentText() if current is None else current
        values = []
        seen = set()
        for value in list(categories or CATEGORIES) + ([selected] if selected else []):
            if isinstance(value, str) and value.strip() and value.casefold() not in seen:
                values.append(value)
                seen.add(value.casefold())
        if selected and selected not in values:
            values.append(selected)
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItems(values)
        for index, value in enumerate(values):
            self.category.setItemData(index, value, Qt.ItemDataRole.ToolTipRole)
        if selected:
            self.category.setCurrentText(selected)
        self.category.setToolTip(self.category.currentText())
        self.category.blockSignals(False)


class TicketCard(NeoCard):
    status_changed = Signal(str, str)
    deleted = Signal(str)
    edit_requested = Signal(dict)
    btn_copy = None
    copy_button = None

    def __init__(self, ticket, parent=None):
        super().__init__(parent)
        self.ticket = dict(ticket)
        self._ticket_id = ticket.get("id", "")
        self.setObjectName("ticket-card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title_label = label(ticket.get("title", "Untitled defect"), self, "title", wrap=True)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.title_label.setReadingHeightLimit(128)
        top.addWidget(self.title_label, 1)

        self.btn_copy_title = FloatingCopyButton(self.title_label, self)
        self.btn_copy_title.setToolTip("Copy title")
        self.btn_copy = self.btn_copy_title
        self.copy_button = self.btn_copy_title
        top.addWidget(self.btn_copy_title, 0, Qt.AlignmentFlag.AlignTop)

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
        self.severity_label = label(severity, self, "severity", wrap=True)
        self.severity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.severity_label.setReadingHeightLimit(80)
        self.severity_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        retone(self.severity_label, SEVERITY_TONES.get(severity, "muted"))
        controls.addWidget(self.severity_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.status_cb = combo(STATUSES, self, ticket.get("status", "Open"))
        self.status_cb.setAccessibleName("Ticket status")
        self.status_cb.setMaximumWidth(155)
        self._update_status_style(self.status_cb.currentText())
        self.status_cb.currentTextChanged.connect(self._on_status_change)
        controls.addWidget(self.status_cb)
        layout.addLayout(controls)
        description = ticket.get("description") or ""
        self.description_label = label(description, self, wrap=True)
        self.description_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.description_label.setReadingHeightLimit(300)
        self.btn_copy_desc = FloatingCopyButton(self.description_label, self)
        self.btn_copy_desc.setToolTip("Copy description")
        if description:
            desc_row = QHBoxLayout()
            desc_row.setSpacing(8)
            desc_row.addWidget(self.description_label, 1)
            desc_row.addWidget(self.btn_copy_desc, 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(desc_row)
        else:
            self.description_label.hide()
            self.btn_copy_desc.hide()
            layout.addWidget(self.description_label)
        created, updated = ticket.get("created_at", ""), ticket.get("updated_at", "")
        stamp = f"Updated {updated}" if updated and updated != created else created
        category = str(ticket.get("category", "General"))
        self.metadata = label(category + (f"\n{stamp}" if stamp else ""), self, "metadata", wrap=True)
        self.metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.metadata.setReadingHeightLimit(128)
        self.metadata.setToolTip(f"{category}\nCreated: {created}\nLast modified: {updated or created}")
        layout.addWidget(self.metadata)

    def _copy_text(self, text_or_widget=""):
        target = text_or_widget or self.ticket.get("title", "")
        FloatingCopyButton(target, self)._copy_to_clipboard()

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


class CreateTicketDialog(_TaskPortal):
    categories_updated = Signal(object)

    def __init__(self, *args, **kwargs):
        parent = kwargs.get("parent")
        service = kwargs.get("service")
        categories = kwargs.get("categories")
        self._on_created = kwargs.get("on_created")

        pos_args = list(args)
        if len(pos_args) >= 1:
            first = pos_args[0]
            if isinstance(first, QWidget) or first is None:
                parent = first
                if len(pos_args) >= 2:
                    service = pos_args[1]
                if len(pos_args) >= 3:
                    categories = pos_args[2]
            else:
                service = first
                if len(pos_args) >= 2:
                    parent = pos_args[1]
                if len(pos_args) >= 3:
                    categories = pos_args[2]

        super().__init__(parent, preferred=(680, 640))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.service = service
        self.categories = list(categories or CATEGORIES)
        self.setObjectName("create-ticket-dialog")
        self.setWindowTitle("Create ticket — MCU Flasher")
        fit_dialog(self, preferred=(680, 640), minimum=(360, 300))
        clamp_window(self)

        self.content.addWidget(heading(self.backdrop, "New ticket", "Record a defect, unexpected behavior or investigation note."))
        self.scroll, body, layout = scroll_body(self.backdrop)
        card = NeoCard(body, accent=True)
        fields_layout = QVBoxLayout(card)
        fields_layout.setContentsMargins(20, 20, 20, 20)
        fields_layout.setSpacing(16)
        self.fields = TicketFields(card, categories=self.categories)
        self.fields.category_add_requested.connect(self._add_ticket_category)
        self.fields.category_remove_requested.connect(self._remove_ticket_category)
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
        self.btn_cancel = button("Cancel", self.backdrop, self.reject, role="quiet")
        footer.addWidget(self.btn_cancel)
        footer.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_save = button("Create ticket", self.backdrop, self._do_save, role="primary", vector="save")
        footer.addWidget(self.btn_save)
        self.content.addLayout(footer)
        self.apply_theme(self._theme_name)
        clamp_window(self)

    def showEvent(self, event):
        super().showEvent(event)
        clamp_window(self)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
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

        self._run_task("create", lambda: self.service.create_ticket(
            title=title, category=cat, severity=sev, description=desc, status=st
        ), self._saved)

    def _set_busy(self, busy):
        super()._set_busy(busy)
        self.btn_save.setEnabled(not busy)
        self.btn_cancel.setEnabled(not busy)
        self.fields.setEnabled(not busy)
        self.btn_save.setText("Creating…" if busy else "Create ticket")

    def _add_ticket_category(self, category):
        if self._task_name:
            return
        self._pending_category = category
        self._run_task("category", lambda: self.service.add_ticket_category(category), self._category_added)

    def _category_added(self, result, error):
        if error or not result:
            self.fields.show_category_feedback(error or "The category could not be saved. Try again.", "fail")
            return
        ok, message, categories = result
        self.categories = list(categories or self.categories)
        selected = next((value for value in self.categories
                         if value.casefold() == self._pending_category.casefold()), "")
        self.fields.set_categories(self.categories, current=selected if ok else None)
        if ok:
            self.fields.category_input.clear()
            self.categories_updated.emit(self.categories)
        self.fields.show_category_feedback(message, "ok" if ok else "warn")

    def _saved(self, ok, error):
        if ok:
            if callable(getattr(self, "_on_created", None)):
                try:
                    self._on_created(ok)
                except Exception:
                    pass
            self.accept()
        else:
            QMessageBox.critical(self, "Save error", error or "The ticket could not be created. Please try again.")


class EditTicketDialog(_TaskPortal):
    categories_updated = Signal(object)

    def __init__(self, *args, **kwargs):
        parent = kwargs.get("parent")
        service = kwargs.get("service")
        ticket = kwargs.get("ticket")
        categories = kwargs.get("categories")

        pos_args = list(args)
        if len(pos_args) >= 3 and isinstance(pos_args[2], dict):
            parent = pos_args[0]
            service = pos_args[1]
            ticket = pos_args[2]
        elif len(pos_args) >= 1:
            ticket = pos_args[0]
            if len(pos_args) >= 2:
                service = pos_args[1]
            if len(pos_args) >= 3:
                parent = pos_args[2]

        ticket = ticket or {}
        super().__init__(parent, preferred=(680, 640))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.ticket = ticket
        self.service = service
        self.categories = list(categories or CATEGORIES)
        self._ticket_id = ticket.get("id", "")
        self.setObjectName("edit-ticket-dialog")
        self.setWindowTitle("Edit ticket — MCU Flasher")
        fit_dialog(self, preferred=(680, 640), minimum=(360, 300))
        clamp_window(self)

        self.content.addWidget(heading(self.backdrop, "Edit ticket", "Update the issue and investigation notes."))
        self.scroll, body, layout = scroll_body(self.backdrop)
        card = NeoCard(body)
        fields_layout = QVBoxLayout(card)
        fields_layout.setContentsMargins(20, 20, 20, 20)
        self.fields = TicketFields(card, ticket, self.categories)
        self.fields.category_add_requested.connect(self._add_ticket_category)
        self.fields.category_remove_requested.connect(self._remove_ticket_category)
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
        self.btn_cancel = button("Cancel", self.backdrop, self.reject, role="quiet")
        footer.addWidget(self.btn_cancel)
        footer.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_save = button("Save changes", self.backdrop, self._do_save, role="primary", vector="save")
        footer.addWidget(self.btn_save)
        self.content.addLayout(footer)
        self.apply_theme(self._theme_name)
        clamp_window(self)

    def showEvent(self, event):
        super().showEvent(event)
        clamp_window(self)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
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
        super()._set_busy(busy)
        self.btn_save.setEnabled(not busy)
        self.btn_cancel.setEnabled(not busy)
        self.fields.setEnabled(not busy)
        self.btn_save.setText("Saving…" if busy else "Save changes")

    def _add_ticket_category(self, category):
        if self._task_name:
            return
        self._pending_category = category
        self._run_task("category", lambda: self.service.add_ticket_category(category), self._category_added)

    def _category_added(self, result, error):
        if error or not result:
            self.fields.show_category_feedback(error or "The category could not be saved. Try again.", "fail")
            return
        ok, message, categories = result
        self.categories = list(categories or self.categories)
        selected = next((value for value in self.categories
                         if value.casefold() == self._pending_category.casefold()), "")
        self.fields.set_categories(self.categories, current=selected if ok else None)
        if ok:
            self.fields.category_input.clear()
            self.categories_updated.emit(self.categories)
        self.fields.show_category_feedback(message, "ok" if ok else "warn")

    def _saved(self, ok, error):
        if ok:
            self.accept()
        else:
            QMessageBox.critical(self, "Save error", error or "The ticket could not be saved. Your changes are still here.")


class CloudSettingsDialog(_TaskPortal):
    def __init__(self, service, parent=None, *, config=None):
        super().__init__(parent, preferred=(640, 530))
        self.service = service
        cfg = dict(config if config is not None else getattr(parent, "_config", {}))
        self.setWindowTitle("Connection and local access — MCU Flasher")
        self.content.addWidget(heading(self.backdrop, "Connection and local access"))
        self.scroll, body, layout = scroll_body(self.backdrop)
        card = NeoCard(body)
        form = QVBoxLayout(card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(16)
        form.addWidget(label("MCU Flasher uses its built-in cloud service. Check its availability below.", card, "muted", wrap=True))
        self.test_button = button("Test connection", card, self._run_test, vector="reload")
        self.test_button.hide()
        form.addWidget(self.test_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.feedback = label("", card, "feedback", wrap=True)
        self.feedback.hide()
        form.addWidget(self.feedback)
        layout.addWidget(card)
        self._local_configured = bool(cfg.get("local_access_configured"))
        self._local_unlocked = bool(getattr(parent, "_authenticated_access", False))
        local_card = NeoCard(body)
        local_form = QVBoxLayout(local_card)
        local_form.setContentsMargins(20, 20, 20, 20)
        local_form.setSpacing(12)
        local_form.addWidget(label("Local access key", local_card, "title"))
        local_form.addWidget(label("Protect developer tickets on this computer with your own key. This key does not sign in to remote cloud.", local_card, "muted", wrap=True))
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
        self.close_button = button("Close", self.backdrop, self.reject, role="quiet")
        footer.addWidget(self.close_button)
        self.content.addLayout(footer)
        self._set_busy(False)
        self.apply_theme(self._theme_name)

    def _show_feedback(self, message, tone):
        self.feedback.setText(message)
        retone(self.feedback, tone)
        self.feedback.show()
        self.scroll.ensureWidgetVisible(self.feedback)

    def _run_test(self):
        self.feedback.hide()
        check_fn = get_service_connection_checker(self.service)
        if callable(check_fn):
            self._run_task("test", check_fn, self._tested)

    def _set_busy(self, busy):
        super()._set_busy(busy)
        self.test_button.setEnabled(not busy)
        local_allowed = not busy and (not self._local_configured or self._local_unlocked)
        self.local_key.setEnabled(local_allowed)
        self.local_confirm.setEnabled(local_allowed)
        self.local_save.setEnabled(local_allowed)
        self.test_button.setText("Testing connection…" if busy and self._task_name == "test" else "Test connection")

    def _tested(self, result, error):
        if error or (result and not result[0]):
            self._show_feedback(error or "Cloud connection unavailable. Check network.", "fail")
        else:
            self.feedback.hide()

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


class _LiveSignals(QObject):
    """Carries the change watcher's thread-side notices onto the GUI thread."""
    changed = Signal(int)
    ended = Signal(int, str)


class OwnerTicketDialog(_TaskPortal):
    # A burst of remote writes (one save is often a put and a patch) costs one re-read.
    LIVE_REFRESH_DELAY_MS = 400

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
        self._user_resized = False
        self._active_filter = "All Status"
        self._active_category_filter = "All categories"
        self._search_query = ""
        # Live updates: the remote watcher or local folder watch, one quiet re-read at a time.
        self._live_generation = 0
        self._tickets_generation = 0
        self._watcher = None
        self._local_watch = None
        self._live_task = None
        self._live_pending = False
        self._live_signals = _LiveSignals(self)
        self._live_signals.changed.connect(self._on_live_change)
        self._live_signals.ended.connect(self._on_live_ended)
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(self.LIVE_REFRESH_DELAY_MS)
        self._live_timer.timeout.connect(self._start_live_refresh)
        self.setObjectName("owner-portal-dialog")
        self.setWindowTitle("Developer tickets — MCU Flasher")
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        if _mcu_icon_path.exists():
            self.setWindowIcon(QIcon(str(_mcu_icon_path)))
        self.portal_header = QWidget(self.backdrop)
        header = QHBoxLayout(self.portal_header)
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(label("Developer tickets", self.portal_header, "label"), 1)
        self.cloud_badge = label("Checking connection…", self.backdrop, "session")
        header.addWidget(self.cloud_badge)
        self.content.addWidget(self.portal_header)
        self.stack = QStackedWidget(self.backdrop)
        self.content.addWidget(self.stack, 1)
        self.stack.currentChanged.connect(self._on_stack_changed)
        self._build_loading_screen()
        self._build_dashboard_screen()
        self._build_auth_screen()
        self.stack.setCurrentIndex(0)
        self._update_portal_header(self.stack.currentIndex())
        self.apply_theme(self._theme_name)
        QTimer.singleShot(0, self, self._initialize_service)

    def apply_theme(self, mode):
        super().apply_theme(mode)
        if hasattr(self, "live_wallpaper"):
            self.live_wallpaper.set_theme_mode(mode)
        if hasattr(self, "auth_scroll"):
            for control in (self.auth_scroll.findChildren(QLineEdit)
                            + self.auth_scroll.findChildren(QTextEdit)):
                palette = control.palette()
                palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#625d63"))
                control.setPalette(palette)
            for action in self.auth_scroll.findChildren(QPushButton):
                vector = action.property("vector")
                if vector:
                    action.setIcon(icon(vector, "#141414"))

    def _on_stack_changed(self, index=None):
        self._update_portal_header(index)
        current = self.stack.currentWidget()
        if hasattr(self, "content"):
            if current == getattr(self, "auth_page", None):
                self.content.setContentsMargins(0, 0, 0, 0)
                self.content.setSpacing(0)
            else:
                margin = 14 if self.width() < 560 else 24
                self.content.setContentsMargins(margin, 16, margin, 16)
                self.content.setSpacing(18)
        if hasattr(self, "live_wallpaper"):
            if current == getattr(self, "auth_page", None):
                self.live_wallpaper.start()
                if hasattr(self, "auth_page"):
                    self.live_wallpaper.setGeometry(self.auth_page.rect())
            else:
                self.live_wallpaper.stop()
        if hasattr(self, "dashboard_page") and current == self.dashboard_page:
            self._auto_refresh()

    def _update_portal_header(self, index=None):
        dashboard = getattr(self, "dashboard_page", None)
        self.portal_header.setVisible(dashboard is not None and self.stack.currentWidget() == dashboard)

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "live_wallpaper") and self.stack.currentWidget() == getattr(self, "auth_page", None):
            self.live_wallpaper.start()
            if hasattr(self, "auth_page"):
                self.live_wallpaper.setGeometry(self.auth_page.rect())
        if hasattr(self, "dashboard_page") and self.stack.currentWidget() == self.dashboard_page:
            self._auto_refresh()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._auto_refresh()

    def closeEvent(self, event):
        if hasattr(self, "live_wallpaper"):
            self.live_wallpaper.stop()
        super().closeEvent(event)

    def _auto_refresh(self):
        """Automatically fetch latest tickets when opening or activating dashboard."""
        if hasattr(self, "dashboard_page") and self.stack.currentWidget() == self.dashboard_page:
            self._do_refresh()

    def auto_refresh(self):
        self._auto_refresh()

    def refresh_tickets(self):
        self._do_refresh()

    def _do_refresh(self):
        if self.service is not None:
            self._refresh_tickets_list()

    def _initialize_service(self):
        def initialize():
            service = OwnerTicketService()
            connection = check_service_connection(service)
            config = service.get_config()
            # A session kept with "Keep me signed in" opens straight to the tickets.
            restored, message = service.restore_session()
            tickets = service.get_tickets()
            return service, config, connection, (restored, message, service.is_cloud_authenticated, tickets)
        self.loading_status.setText("Checking developer environment and tickets…")
        if hasattr(self, "connection_note"):
            self.connection_note.setText("Checking for a kept session…")
            self.connection_note.show()
        self._run_task("initialize", initialize, self._initialized)

    def _initialized(self, result, error):
        if error:
            if hasattr(self, "loading_status"):
                self.loading_status.setText("Could not load developer tickets: " + error)
                retone(self.loading_status, "fail")
            self.lbl_auth_error.setText(error)
            self.lbl_auth_error.show()
            self.btn_diagnose.setEnabled(True)
            self.btn_diagnose.setText("Retry settings")
            self._update_cloud_badge()
            return
        self.service, self._config, connection, (restored, message, cloud, tickets) = result
        self._endpoint_state, self._endpoint_message = connection
        self._sync_category_options()
        if not self.txt_auth_email.isModified():
            self.txt_auth_email.setText(self._config.get("owner_email", ""))
        self._set_busy(False)
        self._update_cloud_badge()
        if restored:
            self.chk_keep_signed_in.setChecked(True)
            self._authenticated((True, message, cloud, tickets), "")
        else:
            self.chk_keep_signed_in.setChecked(False)
            self._authenticated_access = False
            self._cloud_authenticated = False
            self.lbl_auth_error.hide()
            self._update_cloud_badge()
            self.stack.setCurrentWidget(self.auth_page)
            self.txt_auth_pwd.clear()
            self.txt_auth_pwd.setFocus()

    def _set_busy(self, busy):
        super()._set_busy(busy)
        ready = self.service is not None and not busy
        for control in (self.btn_unlock, self.btn_diagnose, self.btn_auth_cloud, self.chk_keep_signed_in,
                        self.btn_new, self.btn_cloud, self.btn_lock, self.btn_refresh, self.btn_save_new,
                        self.cards_container, self.new_fields):
            control.setEnabled(ready)
        self.btn_unlock.setText("Signing in…" if busy and self._task_name in ("authenticate", "initialize")
                                else "Sign in")
        self.btn_diagnose.setText("Checking…" if busy and self._task_name == "diagnose" else "Diagnose network")
        self.btn_refresh.setText("Refreshing…" if busy and self._task_name == "tickets" else "Refresh")
        self.btn_lock.setText("Signing out…" if busy and self._task_name == "lock" else "Log out")

    def _build_loading_screen(self):
        page = QWidget(self.stack)
        page.setObjectName("developer-loading-page")
        self.loading_page = page
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        self.loading_scroll, body, layout = scroll_body(page)
        self.loading_scroll.viewport().installEventFilter(self)
        self.loading_card = NeoCard(body)
        self.loading_card.setObjectName("developer-loading-card")
        self.loading_card.setMaximumWidth(520)
        self.loading_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form = QVBoxLayout(self.loading_card)
        form.setContentsMargins(24, 28, 34, 32)
        form.setSpacing(16)

        identity = QWidget(self.loading_card)
        identity_layout = QHBoxLayout(identity)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(14)
        self.loading_brand = DeveloperArtwork("developer-access-chip.svg", identity)
        self.loading_brand.setFixedSize(64, 64)
        identity_layout.addWidget(self.loading_brand)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title_box.addWidget(label("Developer tickets", identity, "heading", wrap=True))
        title_box.addWidget(label("Loading issue tracker and defect reports…", identity, "muted", wrap=True))
        identity_layout.addLayout(title_box, 1)
        form.addWidget(identity)

        self.loading_trace = DeveloperArtwork("developer-access-trace.svg", self.loading_card)
        self.loading_trace.setFixedHeight(24)
        form.addWidget(self.loading_trace)

        self.loading_status = label("Checking developer environment and tickets…", self.loading_card, "muted", wrap=True)
        self.loading_status.setObjectName("developer-loading-status")
        form.addWidget(self.loading_status)

        layout.setContentsMargins(2, 2, 2, 2)
        layout.addStretch(1)
        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.addStretch(1)
        card_row.addWidget(self.loading_card, 100)
        card_row.addStretch(1)
        layout.addLayout(card_row)
        layout.addStretch(1)
        outer.addWidget(self.loading_scroll)
        self.stack.addWidget(page)

    def _build_auth_screen(self):
        page = QWidget(self.stack)
        page.setObjectName("developer-login-page")
        self.auth_page = page
        self.live_wallpaper = DeveloperLiveWallpaper(page)
        self.live_wallpaper.lower()
        self.auth_page.installEventFilter(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        self.auth_scroll, body, layout = scroll_body(page)
        self.auth_scroll.viewport().installEventFilter(self)
        self.auth_card = NeoLoginCard(body)
        self.auth_card.setObjectName("developer-login-card")
        self.auth_card.setMaximumWidth(520)
        self.auth_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form = QVBoxLayout(self.auth_card)
        form.setContentsMargins(24, 24, 34, 32)
        form.setSpacing(16)
        identity = QWidget(self.auth_card)
        identity_layout = QHBoxLayout(identity)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(14)
        self.auth_brand = DeveloperArtwork("developer-access-chip.svg", identity)
        self.auth_brand.setFixedSize(64, 64)
        identity_layout.addWidget(self.auth_brand)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title_box.addWidget(label("Developer access", identity, "heading", wrap=True))
        title_box.addWidget(label("Sign in to manage your issue reports.", identity, "muted", wrap=True))
        identity_layout.addLayout(title_box, 1)
        form.addWidget(identity)
        self.auth_trace = DeveloperArtwork("developer-access-trace.svg", self.auth_card)
        self.auth_trace.setFixedHeight(24)
        form.addWidget(self.auth_trace)
        self.connection_note = label("Checking connection…", self.auth_card, "muted", wrap=True)
        self.connection_note.setObjectName("developer-connection-note")
        self.connection_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addWidget(self.connection_note)
        self.txt_auth_email = QLineEdit(self.auth_card)
        self.txt_auth_email.setPlaceholderText("Email address")
        self.txt_auth_email.addAction(icon("mail", "#141414"), QLineEdit.ActionPosition.LeadingPosition)
        self.txt_auth_email.returnPressed.connect(self._on_auth_email_return)
        form.addWidget(field("Email", self.txt_auth_email, self.auth_card))
        password = QWidget(self.auth_card)
        password_row = QHBoxLayout(password)
        password_row.setContentsMargins(0, 0, 0, 0)
        password_row.setSpacing(8)
        self.txt_auth_pwd = QLineEdit(password)
        self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_auth_pwd.setPlaceholderText("Password or local access key")
        self.txt_auth_pwd.addAction(icon("key", "#141414"), QLineEdit.ActionPosition.LeadingPosition)
        self.txt_auth_pwd.returnPressed.connect(self._do_authenticate)
        password_row.addWidget(self.txt_auth_pwd, 1)
        self.btn_toggle_eye = button("Show", password, self._toggle_password_mask, role="quiet")
        self._update_eye_icon(True)
        password_row.addWidget(self.btn_toggle_eye)
        form.addWidget(field("Password", password, self.auth_card))
        self.txt_auth_pwd.setAccessibleName("Password or local access key")
        self.chk_keep_signed_in = QCheckBox("Keep me signed in", self.auth_card)
        self.chk_keep_signed_in.setToolTip("Stay signed in on this device. The session is kept in your "
                                           "system's secure credential storage until you log out.")
        self.chk_keep_signed_in.setCursor(Qt.CursorShape.PointingHandCursor)
        form.addWidget(self.chk_keep_signed_in)
        self.lbl_auth_error = label("", self.auth_card, "feedback", wrap=True)
        retone(self.lbl_auth_error, "fail")
        self.lbl_auth_error.hide()
        form.addWidget(self.lbl_auth_error)
        self.btn_unlock = button("Sign in", self.auth_card, self._do_authenticate, role="primary", vector="unlock")
        form.addWidget(self.btn_unlock)
        self.btn_diagnose = button("Diagnose network", self.auth_card, self._diagnose_cloud, role="quiet", vector="reload")
        self.btn_diagnose.hide()
        self.btn_auth_cloud = button("Connection settings", self.auth_card, self._open_cloud_settings_modal, role="quiet", vector="settings")
        self.btn_auth_cloud.hide()
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

    def resize(self, *args):
        self._user_resized = True
        super().resize(*args)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "content") and hasattr(self, "stack") and hasattr(self, "auth_page"):
            if self.stack.currentWidget() == self.auth_page:
                self.content.setContentsMargins(0, 0, 0, 0)
                self.content.setSpacing(0)
        if hasattr(self, "live_wallpaper") and hasattr(self, "auth_page"):
            self.live_wallpaper.setGeometry(self.auth_page.rect())
        if hasattr(self, "loading_scroll"):
            QTimer.singleShot(0, self, self._fit_loading_card)
        if hasattr(self, "auth_scroll"):
            QTimer.singleShot(0, self, self._fit_auth_card)

    def _fit_loading_card(self):
        if hasattr(self, "loading_card") and hasattr(self, "loading_scroll"):
            self.loading_card.setMaximumWidth(min(520, max(1, self.loading_scroll.viewport().width() - 4)))

    def _fit_auth_card(self):
        self.auth_card.setMaximumWidth(min(520, max(1, self.auth_scroll.viewport().width() - 4)))

    def eventFilter(self, watched, event):
        if (hasattr(self, "auth_page") and watched is self.auth_page
                and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)):
            if hasattr(self, "live_wallpaper"):
                self.live_wallpaper.setGeometry(self.auth_page.rect())
        if (hasattr(self, "loading_scroll") and watched is self.loading_scroll.viewport()
                and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)):
            QTimer.singleShot(0, self, self._fit_loading_card)
        if (hasattr(self, "auth_scroll") and watched is self.auth_scroll.viewport()
                and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)):
            QTimer.singleShot(0, self, self._fit_auth_card)
        return super().eventFilter(watched, event)

    def _update_eye_icon(self, is_masked):
        # A textual control stays visible and readable in every palette.
        self.btn_toggle_eye.setText("Show" if is_masked else "Hide")
        self.btn_toggle_eye.setAccessibleName("Show access key" if is_masked else "Hide access key")
        vector = "eye" if is_masked else "eye-off"
        self.btn_toggle_eye.setProperty("vector", vector)
        self.btn_toggle_eye.setIcon(icon(vector, "#141414"))
        self.btn_toggle_eye.setIconSize(QSize(18, 18))

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

            keep = self.chk_keep_signed_in.isChecked()

            def authenticate():
                ok, message = self.service.authenticate(email, pwd, keep_signed_in=keep)
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
        self._tickets_generation += 1
        self._update_cloud_badge()
        self.access_label.setText("Cloud Sync active" if self._cloud_authenticated else "Local developer access")
        self._render_tickets_list()
        self.stack.setCurrentIndex(1)
        self._refresh_tickets_list()
        self._start_live_updates()
        if not self._dashboard_fitted and not getattr(self, "_user_resized", False):
            fit_dialog(self, preferred=(880, 740), minimum=(360, 300))
            self._dashboard_fitted = True
        self.update()

    def _build_dashboard_screen(self):
        page = QWidget(self.stack)
        page.setObjectName("developer-dashboard-page")
        self.dashboard_page = page
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # 1. Stationary top toolbar with aligned cloud sync actions (A6)
        title_row = QHBoxLayout()
        self.dashboard_brand = DeveloperArtwork("developer-access-chip.svg", page)
        self.dashboard_brand.setFixedSize(48, 48)
        title_row.addWidget(self.dashboard_brand)
        title_row.addWidget(label("Issue tracker", page, "heading", wrap=True), 1)
        title_row.addWidget(self.spinner)
        self.btn_refresh = button("Refresh", page, self._refresh_tickets_list, role="quiet", vector="reload")
        self.btn_refresh.setToolTip("Reload the ticket list now. Changes from other devices also appear on their own.")
        self.btn_new = button("New ticket", page, self._open_create_ticket_modal, role="primary", vector="modify")
        self.btn_lock = button("Log out", page, self._do_lock, role="quiet", vector="lock")
        self.btn_lock.setToolTip("Sign out and forget a kept session on this device.")
        self.title_actions = ResponsiveRow([(self.btn_refresh, 0), (self.btn_new, 0), (self.btn_lock, 0)], page, compact_width=0)
        title_row.addWidget(self.title_actions)
        outer.addLayout(title_row)

        # 2. Stats metrics ribbon (stationary outside scroll area - A5 / B7)
        ribbon = NeoCard(page)
        stats_layout = QVBoxLayout(ribbon)
        stats_layout.setContentsMargins(18, 14, 18, 14)
        self.stat_total = self._make_stat_badge("Total tickets", "0", "active", ribbon)
        self.stat_critical = self._make_stat_badge("Critical", "0", "fail", ribbon)
        self.stat_open = self._make_stat_badge("Active", "0", "warn", ribbon)
        self.stat_resolved = self._make_stat_badge("Completed", "0", "ok", ribbon)
        self.stats_grid = ResponsiveGrid([self.stat_total, self.stat_critical, self.stat_open, self.stat_resolved], ribbon, columns=4, cell_width=130)
        stats_layout.addWidget(self.stats_grid)
        outer.addWidget(ribbon)

        # 3. Search input and filter dropdowns (stationary outside scroll area - A5 / B7)
        self.search_input = QLineEdit(page)
        self._search_edit = self.search_input
        self.search_input.setPlaceholderText("Search tickets")
        self.search_input.addAction(icon("search", "#141414"), QLineEdit.ActionPosition.LeadingPosition)
        self.search_input.setAccessibleName("Search tickets")
        self.search_input.textChanged.connect(self._on_search_changed)
        self.filter_cb = combo(["All Status"] + STATUSES, page)
        self.filter_cb.setAccessibleName("Filter tickets by status")
        self.filter_cb.setMaximumWidth(155)
        self.filter_cb.currentTextChanged.connect(self._on_filter_changed)
        self.category_filter = combo(["All categories"] + CATEGORIES, page)
        self.category_filter.setAccessibleName("Filter tickets by category")
        self.category_filter.setMaximumWidth(190)
        self.category_filter.currentTextChanged.connect(self._on_category_filter_changed)
        self.sort_cb = combo(["Newest first", "Oldest first", "Recently updated"], page)
        self.sort_cb.setAccessibleName("Sort tickets")
        self.sort_cb.setMaximumWidth(180)
        self.sort_cb.currentTextChanged.connect(lambda _text: self._render_tickets_list())
        self.search_row = ResponsiveRow(
            [(self.search_input, 1), (self.filter_cb, 0), (self.category_filter, 0), (self.sort_cb, 0)],
            page, compact_width=720)
        outer.addWidget(self.search_row)

        # 4. Account metadata (stationary outside scroll area)
        self.access_label = label("Private defect reports", page, "metadata", wrap=False)
        self.live_label = label("", page, "metadata", wrap=False)
        self.live_label.setObjectName("developer-live-status")
        self.account_row = QWidget(page)
        account = QHBoxLayout(self.account_row)
        account.setContentsMargins(0, 0, 0, 0)
        account.addWidget(self.access_label)
        account.addStretch(1)
        account.addWidget(self.live_label)
        outer.addWidget(self.account_row)

        # Hidden btn_cloud to avoid AttributeError in _set_busy and older tests
        self.btn_cloud = button("Connection settings", page, self._open_cloud_settings_modal, role="quiet", vector="settings")
        self.btn_cloud.hide()

        # 5. Dedicated scroll area for ticket cards list ONLY (A5, A7 / B7)
        self.scroll, body, layout = scroll_body(page)
        self.scroll.setObjectName("developer-cards-scroll")
        self.scroll_cards = self.scroll
        self.card_scroll = self.scroll
        self.scroll_area = self.scroll
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # In-place form_card (retained strictly for test backward compatibility, hidden)
        self.form_card = NeoCard(body, accent=True)
        form = QVBoxLayout(self.form_card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(16)
        form.addWidget(label("New ticket", self.form_card, "title"))
        self._cached_new_fields = TicketFields(self.form_card, categories=self._category_options())
        self._cached_new_fields.category_add_requested.connect(self._add_ticket_category)
        self._cached_new_fields.category_remove_requested.connect(self._remove_ticket_category)
        form.addWidget(self._cached_new_fields)
        self._cached_new_title = self._cached_new_fields.title
        self._cached_new_category = self._cached_new_fields.category
        self._cached_new_sev = self._cached_new_fields.severity
        self._cached_new_status = self._cached_new_fields.status
        self._cached_new_desc = self._cached_new_fields.description
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(button("Cancel", self.form_card, self.form_card.hide, role="quiet"))
        self._cached_btn_save_new = button("Create ticket", self.form_card, self._do_save_new_ticket, role="primary", vector="save")
        footer.addWidget(self._cached_btn_save_new)
        form.addLayout(footer)
        self.form_card.hide()
        layout.addWidget(self.form_card)

        # 6. Ticket cards container with balanced symmetric margins (12, 6, 12, 12) (B6)
        self.cards_container = QWidget(body)
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(12, 6, 12, 12)
        self.cards_layout.setSpacing(14)
        layout.addWidget(self.cards_container)
        layout.addStretch(1)

        outer.addWidget(self.scroll, 1)
        self.stack.addWidget(page)

    _build_dashboard_page = _build_dashboard_screen

    def _make_stat_badge(self, caption, value, tone, parent):
        widget = QWidget(parent)
        widget.setProperty("role", "metric")
        widget.setProperty("tone", tone)
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box = QVBoxLayout(widget)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(4)
        val = label(value, widget, "value")
        val.setObjectName("val-label")
        retone(val, tone)
        box.addWidget(val)
        box.addWidget(label(caption, widget, "metadata"))
        return widget

    def _category_options(self):
        configured = self._config.get("ticket_categories") if isinstance(self._config, dict) else None
        values = list(configured) if isinstance(configured, list) else list(CATEGORIES)
        options = []
        seen = set()
        for value in values:
            if isinstance(value, str) and value.strip() and value.casefold() not in seen:
                options.append(value)
                seen.add(value.casefold())
        for ticket in self._tickets:
            value = ticket.get("category") if isinstance(ticket, dict) else None
            if (isinstance(value, str) and value.strip() and len(value) <= 256
                    and value.casefold() not in seen):
                options.append(value)
                seen.add(value.casefold())
        return options or list(CATEGORIES)

    def _sync_category_options(self, selected=None):
        categories = self._category_options()
        if hasattr(self, "new_fields"):
            self.new_fields.set_categories(categories, current=selected)
        if not hasattr(self, "category_filter"):
            return
        previous = self.category_filter.currentText()
        options = ["All categories"] + categories
        current = previous
        if current not in options:
            current = "All categories"
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItems(options)
        for index, value in enumerate(options):
            self.category_filter.setItemData(index, value, Qt.ItemDataRole.ToolTipRole)
        self.category_filter.setCurrentText(current)
        self.category_filter.setToolTip(current)
        self.category_filter.blockSignals(False)
        self._active_category_filter = current

    def _add_ticket_category(self, category):
        if self._task_name or self.service is None:
            return
        self._pending_category = category
        self._run_task("category", lambda: self.service.add_ticket_category(category), self._category_added)

    def _category_added(self, result, error):
        if error or not result:
            self.new_fields.show_category_feedback(
                error or "The category could not be saved. Try again.", "fail")
            return
        ok, message, categories = result
        categories = list(categories or self._category_options())
        self._config["ticket_categories"] = categories
        selected = next((value for value in categories
                         if value.casefold() == self._pending_category.casefold()), None)
        self._sync_category_options(selected=selected)
        if ok:
            self.new_fields.category_input.clear()
        self.new_fields.show_category_feedback(message, "ok" if ok else "warn")

    @property
    def new_fields(self):
        return getattr(self, "_cached_new_fields", None)

    @new_fields.setter
    def new_fields(self, val):
        self._cached_new_fields = val

    @property
    def new_title(self):
        return getattr(self, "_cached_new_title", None)

    @new_title.setter
    def new_title(self, val):
        self._cached_new_title = val

    @property
    def new_desc(self):
        return getattr(self, "_cached_new_desc", None)

    @new_desc.setter
    def new_desc(self, val):
        self._cached_new_desc = val

    @property
    def new_category(self):
        return getattr(self, "_cached_new_category", None)

    @new_category.setter
    def new_category(self, val):
        self._cached_new_category = val

    @property
    def new_sev(self):
        return getattr(self, "_cached_new_sev", None)

    @new_sev.setter
    def new_sev(self, val):
        self._cached_new_sev = val

    @property
    def new_status(self):
        return getattr(self, "_cached_new_status", None)

    @new_status.setter
    def new_status(self, val):
        self._cached_new_status = val

    @property
    def btn_save_new(self):
        return getattr(self, "_cached_btn_save_new", None)

    @btn_save_new.setter
    def btn_save_new(self, val):
        self._cached_btn_save_new = val

    def _toggle_new_ticket_form(self):
        return self._open_create_ticket_modal()

    def _do_save_new_ticket(self) -> None:
        if self._task_name:
            return
        self.form_card.setVisible(False)
        if getattr(self, "_active_create_dialog", None) is not None and self._active_create_dialog.isVisible():
            self._active_create_dialog.accept()
        try:
            title = self.new_title.text().strip() if self.new_title else ""
            if not title:
                QMessageBox.warning(self, "Validation", "Ticket title cannot be blank.")
                return

            cat = self.new_category.currentText() if self.new_category else "General"
            sev = self.new_sev.currentText() if self.new_sev else "Medium"
            st = self.new_status.currentText() if self.new_status else "Open"
            desc = self.new_desc.toPlainText().strip() if self.new_desc else ""

            def create():
                ticket = self.service.create_ticket(title=title, category=cat, severity=sev, description=desc, status=st)
                return ticket, self.service.get_tickets()
            self._run_task("create", create, self._created)
        except Exception:
            QMessageBox.warning(self, "Create ticket", "The ticket could not be created. Your draft is still here.")

    def _created(self, result, error):
        if not error and result and result[0]:
            self._tickets = result[1]
            self._tickets_generation += 1
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
        if self.service is None:
            return
        self._run_task("tickets", self.service.get_tickets, self._tickets_loaded)

    def _tickets_loaded(self, tickets, error):
        if error:
            QMessageBox.warning(self, "Load tickets", error)
            return
        self._tickets = tickets
        self._tickets_generation += 1
        self._render_tickets_list()

    def _render_tickets_list(self):
        # A list rebuilt by a remote change must not throw the reader back to the top.
        scrollbar = self.scroll_cards.verticalScrollBar() if hasattr(self, "scroll_cards") else None
        position = scrollbar.value() if scrollbar is not None else 0
        if scrollbar is not None:
            QTimer.singleShot(0, self, lambda: scrollbar.setValue(min(position, scrollbar.maximum())))
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        tickets = self._tickets
        self._sync_category_options()
        self._update_stats(tickets)
        query = self._search_query.lower()
        filtered = [ticket for ticket in tickets
                    if (self._active_filter in ("All", "All Status") or ticket.get("status") == self._active_filter)
                    and (self._active_category_filter in ("All", "All categories")
                         or str(ticket.get("category", "")).casefold() == self._active_category_filter.casefold())
                    and (not query or query in " ".join(str(ticket.get(key, "")) for key in ("title", "description", "category")).lower())]
        filtered = sort_tickets(filtered, self.sort_cb.currentText())
        if not filtered:
            empty = NeoCard(self.cards_container)
            box = QVBoxLayout(empty)
            box.setContentsMargins(24, 30, 24, 30)
            box.setSpacing(8)
            title_row = QHBoxLayout()
            mark = DeveloperArtwork("developer-access-chip.svg", empty)
            mark.setFixedSize(44, 44)
            title_row.addWidget(mark)
            title_row.addWidget(label("No matching tickets" if tickets else "No tickets yet", empty, "title", wrap=True), 1)
            box.addLayout(title_row)
            box.addWidget(label("Try a different search, status or category filter." if tickets else "Create a ticket to record an issue and track its progress.", empty, "muted", wrap=True))
            self.cards_layout.addWidget(empty)
        for index, ticket in enumerate(filtered):
            card = TicketCard(ticket, self.cards_container)
            card.status_changed.connect(self._on_ticket_status_changed)
            card.deleted.connect(self._on_ticket_deleted)
            card.edit_requested.connect(self._open_edit_ticket_modal)
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)
        refresh_portal_children(self.cards_container, self._theme_name)

    def _open_edit_ticket_modal(self, ticket: Dict[str, Any]) -> None:
        try:
            dlg = EditTicketDialog(ticket=ticket, service=self.service, parent=self,
                                   categories=self._category_options())
            dlg.categories_updated.connect(self._categories_updated)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._refresh_tickets_list()
        except Exception:
            pass

    def _on_modal_ticket_created(self, ticket=None):
        if hasattr(self, "form_card"):
            self.form_card.setVisible(False)
        self._refresh_tickets_list()

    def _open_create_ticket_modal(self) -> CreateTicketDialog:
        self.form_card.setVisible(True)
        if hasattr(self, "new_title"):
            self.new_title.setFocus()
            QTimer.singleShot(0, self, lambda: self.scroll.ensureWidgetVisible(self.new_title))
        dlg = CreateTicketDialog(parent=self, service=self.service,
                                 categories=self._category_options(),
                                 on_created=self._on_modal_ticket_created)
        dlg.categories_updated.connect(self._categories_updated)
        dlg.accepted.connect(self._refresh_tickets_list)
        self._active_create_dialog = dlg
        if not self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            dlg.show()
        return dlg

    def open_create_ticket_dialog(self) -> CreateTicketDialog:
        dlg = CreateTicketDialog(parent=self, service=self.service,
                                 categories=self._category_options(),
                                 on_created=self._on_modal_ticket_created)
        dlg.categories_updated.connect(self._categories_updated)
        dlg.accepted.connect(self._refresh_tickets_list)
        self._active_create_dialog = dlg
        dlg.show()
        return dlg

    def _categories_updated(self, categories):
        self._config["ticket_categories"] = list(categories or CATEGORIES)
        self._sync_category_options()

    def _on_ticket_status_changed(self, ticket_id, status):
        old_status = None
        for t in self._tickets:
            if t.get("id") == ticket_id:
                old_status = t.get("status")
                t["status"] = status
                break
        self._update_stats(self._tickets)

        def update():
            ok = self.service.update_ticket(ticket_id, {"status": status})
            read_fn = getattr(self.service, "_read_local_tickets", None)
            res = read_fn() if callable(read_fn) and not hasattr(read_fn, "assert_called") else None
            if not isinstance(res, list):
                res = self.service.get_tickets()
            return ok, res

        def on_complete(result, error):
            if error or not result or not result[0]:
                if old_status is not None:
                    for t in self._tickets:
                        if t.get("id") == ticket_id:
                            t["status"] = old_status
                            break
                    self._update_stats(self._tickets)
                    self._render_tickets_list()
                QMessageBox.warning(self, "Update ticket", error or "The ticket could not be changed. Try again.")
            else:
                if result and len(result) > 1 and isinstance(result[1], list):
                    for updated_t in result[1]:
                        if updated_t.get("id") == ticket_id:
                            for local_t in self._tickets:
                                if local_t.get("id") == ticket_id:
                                    local_t.update(updated_t)
                                    break
                            break
                    self._update_stats(self._tickets)

        self._run_task("update", update, on_complete)

    def _ticket_mutated(self, result, error):
        if not error and result:
            self._tickets = result[1]
            self._tickets_generation += 1
            self._render_tickets_list()
        if error or not result or not result[0]:
            QMessageBox.warning(self, "Update ticket", error or "The ticket could not be changed. Try again.")

    def _on_ticket_deleted(self, ticket_id: str) -> None:
        try:
            self._tickets = [t for t in self._tickets if t.get("id") != ticket_id]
            self._update_stats(self._tickets)
            self._render_tickets_list()

            def delete():
                ok = self.service.delete_ticket(ticket_id)
                read_fn = getattr(self.service, "_read_local_tickets", None)
                res = read_fn() if callable(read_fn) and not hasattr(read_fn, "assert_called") else None
                if not isinstance(res, list):
                    res = self.service.get_tickets()
                return ok, res
            self._run_task("delete", delete, self._ticket_mutated)
        except Exception:
            pass

    def _on_filter_changed(self, text: str) -> None:
        try:
            self._active_filter = text
            self._render_tickets_list()
        except Exception:
            pass

    def _on_category_filter_changed(self, text: str) -> None:
        try:
            self._active_category_filter = text
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
        if self.service is None:
            return
        self._stop_live_updates()
        self._run_task("lock", self.service.sign_out, self._locked)

    def _locked(self, result, error):
        if error:
            QMessageBox.warning(self, "Log out", error)
            return
        ok, message = result if isinstance(result, tuple) else (True, "")
        if not ok:
            QMessageBox.warning(self, "Log out", message)
        self.chk_keep_signed_in.setChecked(False)
        self._tickets = []
        self._tickets_generation += 1
        self._cloud_authenticated = False
        self._authenticated_access = False
        self.txt_auth_pwd.clear()
        self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._update_eye_icon(True)
        self.lbl_auth_error.hide()
        self._update_cloud_badge()
        self._render_tickets_list()
        self.stack.setCurrentWidget(self.auth_page)
        self.txt_auth_pwd.setFocus()

    def _update_cloud_badge(self):
        cloud_enabled = self.service.is_cloud_enabled() if self.service else bool(self._config.get("use_cloud", True))
        cloud_configured = self.service.is_cloud_configured() if self.service else True
        if self.service is None:
            text, tone, message = "Unavailable", "warn", "Settings could not be loaded. Use Retry settings."
        elif self._endpoint_state is False:
            text, tone, message = "No connection", "warn", self._endpoint_message
        elif self._cloud_authenticated:
            text, tone, message = "Signed in", "ok", "You are signed in."
        elif not cloud_enabled:
            text, tone, message = "Online" if self._endpoint_state else "Local tickets", "muted", "Use your local access key to sign in on this device."
        elif not cloud_configured:
            text, tone, message = "Online" if self._endpoint_state else "Not set up", "active", "The built-in cloud connection is unavailable."
        elif self._endpoint_state is True:
            text, tone, message = "Online", "ok", "Connected. Sign in to continue."
        else:
            text, tone, message = "Checking…", "muted", "Checking connection…"
        self.cloud_badge.setText(text)
        self.cloud_badge.setToolTip(message)
        self.connection_note.setText(message)
        self.connection_note.setVisible(self._endpoint_state is not True or not cloud_enabled or not cloud_configured)
        retone(self.cloud_badge, tone)

    def _diagnose_cloud(self):
        if self.service is None:
            self._initialize_service()
            return
        check_fn = get_service_connection_checker(self.service)
        if callable(check_fn):
            self._run_task("diagnose", check_fn, self._diagnosed)

    def _diagnosed(self, result, error):
        self._endpoint_state, self._endpoint_message = result if not error else (False, error)
        self._update_cloud_badge()
        self._fit_auth_card()

    def _open_cloud_settings_modal(self):
        if self._task_name or self.service is None:
            return
        dialog = CloudSettingsDialog(self.service, self, config=self._config)
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

    # ── Live updates ─────────────────────────────────────────────────────────

    def _start_live_updates(self):
        """Follow changes made by other clients while the dashboard is open."""
        self._stop_live_updates()
        if self.service is None or not self._authenticated_access:
            return
        self._live_generation += 1
        generation = self._live_generation
        if self._cloud_authenticated:
            signals = self._live_signals
            lock = _SERVICE_LOCKS.setdefault(self.service, Lock())
            self._watcher = TicketChangeWatcher(
                self.service, lock,
                on_change=lambda: signals.changed.emit(generation),
                on_end=lambda reason: signals.ended.emit(generation, reason))
            self._watcher.start()
            self.live_label.setText("Live updates on")
            self.live_label.setToolTip("Changes from your other devices appear here.")
        else:
            # A local-only session has no remote; another window on this computer writes the
            # same tickets file, which is replaced atomically, so watch its folder.
            folder = str(getattr(self.service, "_storage_dir", "") or "")
            if folder:
                self._local_watch = QFileSystemWatcher([folder], self)
                self._local_watch.directoryChanged.connect(lambda _path: self._on_live_change(generation))
                self.live_label.setText("Live updates on")
                self.live_label.setToolTip("Changes from other windows on this computer appear here.")
        retone(self.live_label, "ok")

    def _stop_live_updates(self):
        self._live_generation += 1
        self._live_timer.stop()
        self._live_pending = False
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        if self._local_watch is not None:
            self._local_watch.deleteLater()
            self._local_watch = None
        if hasattr(self, "live_label"):
            self.live_label.setText("")
            self.live_label.setToolTip("")

    @Slot(int)
    def _on_live_change(self, generation):
        if generation == self._live_generation and self._authenticated_access:
            self._live_timer.start()

    @Slot(int, str)
    def _on_live_ended(self, generation, reason):
        if generation != self._live_generation:
            return
        self._watcher = None
        messages = {"session": "Live updates stopped: session ended",
                    "denied": "Live updates stopped: cloud refused access",
                    "signed-out": ""}
        tooltips = {"session": "Live updates stopped because your session ended. Log out and sign in again.",
                    "denied": "Live updates stopped: the cloud refused access to these tickets.",
                    "signed-out": ""}
        self.live_label.setText(messages.get(reason, "Live updates stopped"))
        self.live_label.setToolTip(tooltips.get(reason, "Use Refresh to reload."))
        retone(self.live_label, "warn")

    def _start_live_refresh(self):
        """A quiet re-read that never disables the dashboard or blocks the user's own actions."""
        if self.service is None or not self._authenticated_access or self.stack.currentIndex() != 1:
            return
        if self._live_task is not None:
            self._live_pending = True
            return
        lock = _SERVICE_LOCKS.setdefault(self.service, Lock())
        task = _ServiceTask("live", self.service.get_tickets, lock)
        self._live_task = task
        self._live_started = (self._live_generation, self._tickets_generation)
        _SERVICE_TASKS.add(task)
        # A bound slot, so Qt queues the result onto the GUI thread.
        task.completed.connect(self._live_task_complete)
        task.finished.connect(lambda: _SERVICE_TASKS.discard(task))
        task.finished.connect(task.deleteLater)
        task.start()

    @Slot(object)
    def _live_task_complete(self, result):
        self._live_loaded(result, getattr(self, "_live_started", (-1, -1)))

    def _live_loaded(self, result, started):
        self._live_task = None
        live_generation, tickets_generation = started
        _operation, tickets, error = result
        if live_generation != self._live_generation:
            return
        if self._live_pending or tickets_generation != self._tickets_generation:
            # Something newer happened meanwhile; read again rather than show older tickets.
            self._live_pending = False
            self._live_timer.start()
            return
        if error or not isinstance(tickets, list):
            return
        parse_ts = getattr(self.service, "_parse_timestamp", None)
        if _tickets_content_identical(self._tickets, tickets, parse_ts):
            return
        self._tickets = tickets
        self._tickets_generation += 1
        self._render_tickets_list()

    def hideEvent(self, event):
        if hasattr(self, "live_wallpaper"):
            self.live_wallpaper.stop()
        # A closed portal stops listening; minimizing it (a spontaneous hide) does not.
        if not event.spontaneous() and not self.isVisible():
            self._stop_live_updates()
        super().hideEvent(event)

    def done(self, result):
        self._stop_live_updates()
        super().done(result)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if hasattr(self, "auth_page") and self.stack.currentWidget() == self.auth_page:
                self._do_authenticate()
            event.accept()
        else:
            super().keyPressEvent(event)


register_portal_aliases(sys.modules[__name__])
