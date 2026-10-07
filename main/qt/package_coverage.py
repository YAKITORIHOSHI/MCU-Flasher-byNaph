"""Explicit, asynchronous viewer for complete downloaded-board coverage."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal, Slot, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                              QComboBox, QTableView, QHeaderView, QPlainTextEdit,
                              QPushButton, QSizePolicy)
from main.qt.responsive import ScreenWatcher, fit_dialog
from main.qt.theme import get_palette

JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_REPORT_ROWS = 25000
STATUS_LABELS = {"ready": "Ready", "preparation_required": "Needs preparation",
                 "unavailable": "Unavailable", "unsupported": "Unsupported"}


def read_package_coverage(job_id, *, event_root=None):
    """Validate and load the exact app report; call only from a worker."""
    from src.modules.package_jobs import event_directory
    if not JOB_ID.fullmatch(str(job_id or "")):
        raise ValueError("The board report has an invalid job identity")
    base = event_directory(event_root).resolve()
    expected = base / "reports" / (str(job_id) + ".json")
    try:
        resolved = expected.resolve(strict=True)
    except OSError as exc:
        raise ValueError("The board preparation report is no longer available") from exc
    if not resolved.is_relative_to(base) or resolved.parent != (base / "reports"):
        raise ValueError("The board report is outside the application's report directory")
    if resolved.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("The complete board report exceeds the supported 16 MB limit")
    with resolved.open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("The complete board report exceeds the supported 16 MB limit")
    report = json.loads(data.decode("utf-8"))
    if not isinstance(report, dict) or report.get("schema") != 1 or report.get("job_id") != job_id:
        raise ValueError("The board report has an invalid format or belongs to another job")
    rows = report.get("boards")
    if not isinstance(rows, list) or len(rows) > MAX_REPORT_ROWS:
        raise ValueError("The complete board report exceeds the supported 25,000-board limit or is invalid")
    cleaned = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in STATUS_LABELS:
            raise ValueError("The board report contains an invalid result")
        clean = {}
        for key, limit in (("name", 1024), ("status", 64), ("platform", 256), ("board", 256),
                           ("reason", 8192), ("backend", 64), ("arduino_fqbn", 512)):
            value = row.get(key, "")
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError("The board report contains an invalid or oversized field")
            clean[key] = value
        if clean["backend"] not in ("", "platformio", "arduino-cli"):
            raise ValueError("The board report contains an invalid backend")
        clean["backend_label"] = "Arduino CLI" if clean["backend"] == "arduino-cli" else "PlatformIO"
        clean["target"] = (clean["arduino_fqbn"] if clean["backend"] == "arduino-cli" else "") or ((clean["platform"] + ":" + clean["board"]) if clean["board"] else clean["platform"])
        clean["search"] = " ".join(clean[key] for key in ("name", "target", "reason", "backend_label")).casefold()
        cleaned.append(clean)
    return {"job_id": job_id, "rows": cleaned}


class CoverageModel(QAbstractTableModel):
    HEADERS = ("Board", "Status", "Target", "Backend", "Reason")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.indices = [], []

    def set_rows(self, rows, indices=None):
        self.beginResetModel()
        self.rows = rows
        self.indices = list(range(len(rows))) if indices is None else indices
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.indices)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 5

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.indices):
            return None
        row = self.rows[self.indices[index.row()]]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return (row["name"], STATUS_LABELS[row["status"]], row["target"] or "—", row["backend_label"], row["reason"])[index.column()]
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        return super().headerData(section, orientation, role)


class PackageCoverageDialog(QDialog):
    _loaded = Signal(dict)
    _filtered = Signal(dict)

    def __init__(self, job_id, parent=None):
        super().__init__(parent)
        self.job_id = str(job_id)
        self._closed = threading.Event()
        self._rows = []
        self._filter_generation = 0
        self._filter_running = False
        self._filter_pending = None
        self._load_started = False
        self.setWindowTitle("Board preparation results")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setModal(False)
        self.setObjectName("package-coverage-dialog")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        self.summary = QLabel("Loading the complete board report…", self)
        self.summary.setWordWrap(True)
        self.summary.setObjectName("coverage-summary")
        layout.addWidget(self.summary)
        filter_row = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search boards, targets or reasons")
        self.search.setAccessibleName("Search board preparation results")
        self.status = QComboBox(self)
        self.status.setAccessibleName("Filter board preparation status")
        self.status.addItem("All results", "")
        for value, label in STATUS_LABELS.items():
            self.status.addItem(label, value)
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(self.status)
        layout.addLayout(filter_row)
        self.table = QTableView(self)
        self.table.setAccessibleName("Complete board preparation results")
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setMinimumHeight(40)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.model = CoverageModel(self)
        self.table.setModel(self.model)
        self.table.selectionModel().currentRowChanged.connect(self._show_reason)
        layout.addWidget(self.table, 1)
        self.reason = QPlainTextEdit(self)
        self.reason.setReadOnly(True)
        self.reason.setPlaceholderText("Select a board to read its complete result")
        self.reason.setAccessibleName("Selected board preparation reason")
        self.reason.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.reason.setMinimumHeight(40)
        self.reason.setMaximumHeight(110)
        layout.addWidget(self.reason)
        footer = QHBoxLayout()
        self.count = QLabel("", self)
        footer.addWidget(self.count, 1)
        close = QPushButton("Close", self)
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self._request_filter)
        self.search.textChanged.connect(self._schedule_filter)
        self.status.currentIndexChanged.connect(self._schedule_filter)
        self._loaded.connect(self._finish_load, Qt.ConnectionType.QueuedConnection)
        self._filtered.connect(self._finish_filter, Qt.ConnectionType.QueuedConnection)
        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())
        from main.qt.signals import signals
        signals.theme_changed.connect(self.apply_theme)
        fit_dialog(self, preferred=(920, 620), minimum=(360, 250))
        self._screen_watcher = ScreenWatcher(self, lambda screen: self._size_columns())
        self._size_columns()

    def _size_columns(self):
        width = max(320, self.table.viewport().width())
        self.table.setColumnWidth(0, max(150, int(width * 0.29)))
        self.table.setColumnWidth(1, max(120, self.fontMetrics().horizontalAdvance("Needs preparation") + 22))
        self.table.setColumnWidth(2, max(150, int(width * 0.24)))
        self.table.setColumnWidth(3, 115)
        self.table.verticalHeader().setDefaultSectionSize(max(26, self.fontMetrics().height() + 12))

    @Slot(str)
    def apply_theme(self, theme_name):
        palette = get_palette(theme_name)
        self.setStyleSheet(
            f"QDialog#package-coverage-dialog {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            f"stop:0 {palette['BG_LIGHT']}, stop:0.35 {palette['BG_DARK']}, stop:1 {palette['BG_DARKEST']}); color:{palette['TEXT']}; }}"
            f"QLabel {{ color:{palette['TEXT']}; background:transparent; }}"
            f"QLabel#coverage-summary {{ color:{palette['TEXT_BRIGHT']}; font-weight:600; }}"
            f"QTableView, QPlainTextEdit, QLineEdit, QComboBox {{ background:{palette['BG_DARKEST']}; color:{palette['TEXT']}; "
            f"border:1px solid {palette['BORDER']}; border-radius:4px; padding:4px; "
            f"selection-background-color:{palette['BG_HOVER']}; selection-color:{palette['TEXT_BRIGHT']}; }}"
            f"QTableView {{ gridline-color:{palette['BORDER']}; }}"
            f"QHeaderView::section {{ background:{palette['BG_DARK']}; color:{palette['TEXT']}; border:none; padding:6px; }}"
            f"QPushButton {{ background:{palette['BG_MID']}; color:{palette['TEXT']}; border:1px solid {palette['BORDER']}; border-radius:4px; padding:5px 14px; }}"
            f"QPushButton:hover {{ background:{palette['BG_HOVER']}; color:{palette['TEXT_BRIGHT']}; }}"
        )

    def showEvent(self, event):
        super().showEvent(event)
        if not self._load_started:
            self._load_started = True
            self._request_load()

    def _request_load(self):
        job_id, closed = self.job_id, self._closed
        signal = self._loaded
        def read():
            try:
                result = read_package_coverage(job_id)
            except Exception as exc:
                result = {"error": str(exc)}
            if not closed.is_set():
                try:
                    signal.emit(result)
                except RuntimeError:
                    pass
        try:
            threading.Thread(target=read, name="MCU_PackageCoverageReader", daemon=True).start()
        except (OSError, RuntimeError) as exc:
            self._finish_load({"error": str(exc)})

    @Slot(dict)
    def _finish_load(self, result):
        if self._closed.is_set():
            return
        if "error" in result:
            self.summary.setText("The complete board report could not be loaded.")
            self.reason.setPlainText(result["error"])
            self.search.setEnabled(False)
            self.status.setEnabled(False)
            return
        self._rows = result["rows"]
        ready = sum(row["status"] == "ready" for row in self._rows)
        cli = sum(row["status"] == "ready" and row["backend"] == "arduino-cli" for row in self._rows)
        message = f"{len(self._rows)} boards, {ready} ready, {len(self._rows) - ready} requiring attention"
        if cli:
            message += f". {cli} use Arduino CLI because PlatformIO has no exact board support yet."
        self.summary.setText(message)
        self._request_filter()

    def _schedule_filter(self, *args):
        self._filter_generation += 1
        self._search_timer.start()

    def _request_filter(self):
        request = (self._filter_generation, self.search.text().casefold().strip(), self.status.currentData())
        if self._filter_running:
            self._filter_pending = request
            return
        self._start_filter(request)

    def _start_filter(self, request):
        self._filter_running = True
        rows, closed, signal = self._rows, self._closed, self._filtered
        generation, query, status = request
        def search():
            indices = [index for index, row in enumerate(rows)
                       if (not query or query in row["search"]) and (not status or row["status"] == status)]
            if not closed.is_set():
                try:
                    signal.emit({"generation": generation, "indices": indices})
                except RuntimeError:
                    pass
        try:
            threading.Thread(target=search, name="MCU_PackageCoverageFilter", daemon=True).start()
        except (OSError, RuntimeError) as exc:
            self._filter_running = False
            self.reason.setPlainText(f"The board report could not be filtered: {exc}")

    @Slot(dict)
    def _finish_filter(self, result):
        self._filter_running = False
        if self._closed.is_set():
            return
        if result["generation"] == self._filter_generation:
            self.model.set_rows(self._rows, result["indices"])
            self.count.setText(f"Showing {len(result['indices'])} of {len(self._rows)} boards")
            self.reason.clear()
        pending, self._filter_pending = self._filter_pending, None
        if pending is not None:
            self._start_filter(pending)

    def _show_reason(self, current, previous):
        if current.isValid() and current.row() < len(self.model.indices):
            row = self.model.rows[self.model.indices[current.row()]]
            self.reason.setPlainText(f"{row['name']}\n{STATUS_LABELS[row['status']]} · {row['backend_label']}\n\n{row['reason']}")

    def done(self, result):
        self._closed.set()
        self._search_timer.stop()
        super().done(result)

    def closeEvent(self, event):
        self._closed.set()
        self._search_timer.stop()
        super().closeEvent(event)


def open_package_coverage(parent, job_id):
    """Open or raise a window only in response to an explicit details action."""
    dialogs = getattr(parent, "_package_coverage_dialogs", None)
    if dialogs is None:
        dialogs = {}
        parent._package_coverage_dialogs = dialogs
    if job_id in dialogs:
        dialog = dialogs[job_id]
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog
    dialog = PackageCoverageDialog(job_id, parent)
    dialogs[job_id] = dialog
    dialog.destroyed.connect(lambda: dialogs.pop(job_id, None))
    dialog.show()
    return dialog
