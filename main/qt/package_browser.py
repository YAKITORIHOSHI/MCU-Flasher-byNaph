#!/usr/bin/env python3
"""PySide6 Library and Board browser.

The window appears before any catalog is read. Lists are virtualized models, so
thousands of rows cost only what is on screen, and all disk and network work stays
on workers that report through one queued boundary. Package behavior lives in
``arduino_lib_req.BrowserEngine``; this module only presents it.
"""
from __future__ import annotations

from collections import deque
from functools import lru_cache
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import (QAbstractListModel, QFileSystemWatcher, QModelIndex, QObject, QSize, QSocketNotifier,
                            QStringListModel, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
                               QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QScrollArea, QSizePolicy, QSplitter,
                               QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from main.core.theme import Theme
from main.qt.glass import GlassDialog, GlassWorkspace
from main.qt.icons import ActionButton, IconLabel, icon
from main.qt.responsive import ScreenWatcher, fit_dialog
from main.qt.taskbar import window_hidden, window_shown
from main.qt.theme import build_stylesheet, glass_tokens, install_no_focus_lines, register_fonts
from main.core.config import get_monitor_font_size
from src.modules import arduino_lib_req as engine
from src.modules import ubuntu_download_manager as manager
from src.modules.browser_loading import LatestScan, scan_package_details, search_catalog
from src.modules.ui_palette import readable_foreground

SEARCH_DELAY_MS = 120
BROWSER_USER_MODEL_ID = "Naph.MCUFlasher.GUI.V6"
THIRD_PARTY_INDEXES ="https://github.com/arduino/Arduino/wiki/Unofficial-list-of-3rd-party-boards-support-urls"
_DRAIN_BUDGET_SECONDS = 0.006


class QtTasks(QObject):
    """Workers post callbacks; only the GUI thread runs them, with no idle timer."""

    _wake = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = deque()
        self._latest = {}
        self._lock = threading.Lock()
        self._scheduled = False
        self.closed = False
        self._cancellations = []
        self._wake.connect(self._drain, Qt.ConnectionType.QueuedConnection)

    def post(self, callback, *args, key=None):
        with self._lock:
            if self.closed:
                return
            if key is not None:
                self._latest[key] = (callback, args)
            else:
                self._queue.append((callback, args))
            schedule, self._scheduled = not self._scheduled, True
        if schedule:
            self._wake.emit()

    def start(self, target, *args, failed=None):
        if self.closed:
            return False
        launch_lock = threading.Lock()
        entered = abandoned = False

        def run():
            nonlocal entered
            with launch_lock:
                if abandoned:
                    return
                entered = True
            try:
                target(*args)
            except Exception as error:  # pylint: disable=broad-except
                if failed:
                    self.post(failed, str(error))

        try:
            threading.Thread(target=run, daemon=True, name="BrowserTask").start()
        except Exception as error:  # pylint: disable=broad-except
            with launch_lock:
                # A worker that already entered owns its completion. A delayed
                # one must not begin after this failed launch was reported.
                if entered:
                    return True
                abandoned = True
            if failed:
                self.post(failed, str(error))
            return False
        return True

    def _drain(self):
        deadline = time.perf_counter() + _DRAIN_BUDGET_SECONDS
        while time.perf_counter() < deadline:
            with self._lock:
                if not self._queue:
                    break
                callback, args = self._queue.popleft()
            self._call(callback, args)
        with self._lock:
            latest, self._latest = self._latest, {}
        for callback, args in latest.values():
            self._call(callback, args)
        with self._lock:
            again = bool(self._queue or self._latest)
            self._scheduled = again
        if again and not self.closed:
            self._wake.emit()

    def _call(self, callback, args):
        if self.closed:
            return
        try:
            callback(*args)
        except Exception:  # pylint: disable=broad-except
            sys.excepthook(*sys.exc_info())

    def close(self):
        with self._lock:
            self.closed = True
            self._queue.clear()
            self._latest.clear()
        for cancel in self._cancellations:
            cancel()


class PackageListModel(QAbstractListModel):
    """Rows exist only as text: Qt asks for what it is about to paint."""

    def __init__(self, label_for, icon_for, parent=None):
        super().__init__(parent)
        self._rows = []
        self._label_for, self._icon_for = label_for, icon_for

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rows(self):
        return self._rows

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self._label_for(row)
        if role == Qt.ItemDataRole.DecorationRole:
            return _list_icon(self._icon_for(row))
        return None

    def refresh_icons(self):
        if self._rows:
            self.dataChanged.emit(self.index(0), self.index(len(self._rows) - 1),
                                  [Qt.ItemDataRole.DecorationRole])


@lru_cache(maxsize=48)
def _render_list_icon(name, normal, selected) -> QIcon:
    result = QIcon()
    size = QSize(16, 16)
    for mode, color in ((QIcon.Mode.Normal, normal), (QIcon.Mode.Selected, selected)):
        rendered = icon(name, color, size=16)
        for ratio in (1, 2):
            result.addPixmap(rendered.pixmap(size, ratio), mode)
    return result


def _list_icon(name) -> QIcon:
    if not name:
        return QIcon()
    normal = {"check": Theme.GREEN, "upload": Theme.YELLOW}.get(name, Theme.TEXT_DIM)
    return _render_list_icon(name, normal, readable_foreground(Theme.CYAN_DIM))


def _package_list(model, parent=None) -> QListView:
    view = QListView(parent)
    view.setObjectName("package-list")
    view.setModel(model)
    view.setUniformItemSizes(True)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
    return view


def _card(parent=None, *, name="package-card") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(parent)
    frame.setObjectName(name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)
    return frame, layout


def _reading(text="", parent=None, *, tone="body", bold=False) -> QLabel:
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setProperty("packageTone", tone)
    if bold:
        font = label.font()
        font.setBold(True)
        label.setFont(font)
    return label


def _action(text, callback=None, *, role="", icon_name="", danger=False) -> ActionButton:
    button = ActionButton(text, icon_name=icon_name)
    if role:
        button.setProperty("role", role)
    if danger:
        button.setProperty("packageDanger", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback is not None:
        button.clicked.connect(lambda _checked=False: callback())
    return button


def _set_tone(label, tone):
    if label.property("packageTone") != tone:
        label.setProperty("packageTone", tone)
        label.style().unpolish(label)
        label.style().polish(label)


class BrowseTab(QWidget):
    """Search, one virtual list and a detail pane; one instance per catalog."""

    def __init__(self, app, *, is_board):
        super().__init__()
        self.app = app
        self.is_board = is_board
        self.all_items = {}
        self.filtered_names = []
        self._search_revision = 0
        self._version_revision = 0
        self._requested = None
        self._search_worker = LatestScan(app._tasks, search_catalog)
        self._version_worker = LatestScan(app._tasks, lambda request, cancel: None if cancel.is_set() else
                                          app._get_installed_info(request[0], request[1], request[2], download_dir=request[3],
                                                                  installed_items=request[4], cancel=cancel))
        self._current_links = {}
        self._download_action = lambda: app._download(self)
        self._status_subject = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(QLabel("Search"))
        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("Find a board package" if self.is_board else "Find a library")
        self.search_entry.setClearButtonEnabled(True)
        self.search_entry.textChanged.connect(lambda _text: self._search_timer.start())
        self.search_entry.returnPressed.connect(self.execute_search)
        search_row.addWidget(self.search_entry, 1)
        self.lbl_search_status = QLabel("")
        self.lbl_search_status.setProperty("packageTone", "dim")
        search_row.addWidget(self.lbl_search_status)
        layout.addLayout(search_row)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self.execute_search)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        left, left_layout = _card()
        heading = _reading("Available boards" if self.is_board else "Available libraries", bold=True)
        left_layout.addWidget(heading)
        self.model = PackageListModel(lambda name: name, self._row_icon, self)
        self.listbox = _package_list(self.model)
        self.listbox.selectionModel().currentChanged.connect(self._on_current_changed)
        left_layout.addWidget(self.listbox, 1)
        left.setMinimumWidth(150)
        splitter.addWidget(left)

        detail, detail_layout = _card()
        detail.setMinimumWidth(250)
        self._detail_stack = QStackedWidget()
        self.lbl_placeholder = _reading("Select a package to see its details.", tone="dim")
        self.lbl_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_stack.addWidget(self.lbl_placeholder)
        self._detail_stack.addWidget(self._build_detail())
        detail_layout.addWidget(self._detail_stack, 1)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([360, 560])

    def _build_detail(self):
        scroll = QScrollArea()
        scroll.setObjectName("package-detail-scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.detail_scroll = scroll
        content = QWidget()
        content.setObjectName("package-detail")
        form = QVBoxLayout(content)
        form.setContentsMargins(0, 0, 4, 0)
        form.setSpacing(5)
        self.lbl_name = IconLabel("", icon_name="usb" if self.is_board else "book")
        self.lbl_name.setWordWrap(True)
        self.lbl_name.setProperty("packageTone", "title")
        form.addWidget(self.lbl_name)
        self.lbl_status_badge = _reading(tone="ok", bold=True)
        form.addWidget(self.lbl_status_badge)
        self.lbl_meta = []
        for _ in range(4):
            label = _reading(tone="dim")
            self.lbl_meta.append(label)
            form.addWidget(label)
        if self.is_board:
            self.lbl_boards_header = _reading("Supported Boards:", bold=True)
            form.addWidget(self.lbl_boards_header)
            self.lbl_boards = _reading()
            form.addWidget(self.lbl_boards)
        else:
            self.lbl_sentence = _reading(tone="bright")
            form.addWidget(self.lbl_sentence)
            self.lbl_paragraph = _reading()
            form.addWidget(self.lbl_paragraph)
        self.link_primary = self._link_button("globe", "website")
        form.addWidget(self.link_primary, 0, Qt.AlignmentFlag.AlignLeft)
        self.link_secondary = self._link_button("book" if self.is_board else "package",
                                                "help" if self.is_board else "repo")
        form.addWidget(self.link_secondary, 0, Qt.AlignmentFlag.AlignLeft)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setObjectName("package-rule")
        form.addWidget(rule)
        version_row = QHBoxLayout()
        version_row.setSpacing(8)
        version_row.addWidget(QLabel("Version"))
        self.version_combo = QComboBox()
        self.version_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.version_combo.setMinimumWidth(72)
        self.version_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.version_combo.currentIndexChanged.connect(lambda _index: self.app._update_version_status(self))
        version_row.addWidget(self.version_combo, 1)
        self.download_btn = _action("Download", self._on_download_clicked, role="primary")
        version_row.addWidget(self.download_btn)
        form.addLayout(version_row)
        self.lbl_available = _reading(tone="ok", bold=True)
        form.addWidget(self.lbl_available)
        self.lbl_size = _reading(tone="dim")
        form.addWidget(self.lbl_size)
        if self.is_board:
            self.prepare_btn = _action("Prepare board support", lambda: self.app._prepare_existing_board(self))
            self.prepare_btn.setEnabled(False)
            form.addWidget(self.prepare_btn, 0, Qt.AlignmentFlag.AlignLeft)
            self.mapping_btn = _action("Custom platform…", lambda: self.app._edit_board_platform(self), icon_name="package")
            form.addWidget(self.mapping_btn, 0, Qt.AlignmentFlag.AlignLeft)
            self.arduino_cli_btn = _action("Choose Arduino CLI boards…", lambda: self.app._edit_arduino_cli_boards(self), icon_name="usb")
            self.arduino_cli_btn.setEnabled(False)
            form.addWidget(self.arduino_cli_btn, 0, Qt.AlignmentFlag.AlignLeft)
            form.addWidget(_reading("Arduino CLI is available only for Arduino UNO Q and Raspberry Pi Pico 2 / RP2350. "
                                    "All other boards build with PlatformIO.", tone="dim"))
        form.addStretch(1)
        scroll.setWidget(content)
        return scroll

    def _link_button(self, icon_name, link_type):
        button = _action("", lambda: self.app._open_link(self, link_type), icon_name=icon_name)
        button.setObjectName("package-link")
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        button.hide()
        return button

    def set_link(self, button, link_type, url):
        self._current_links[link_type] = url or ""
        # Very long URLs must not force the pane wider than its splitter share.
        shown = url if len(url or "") <= 70 else url[:67] + "…"
        button.setText(shown or "")
        button.setToolTip(url or "")
        button.setVisible(bool(url))

    def _row_icon(self, name):
        installed = (getattr(self.app, "_installed_map", None) or {}).get(name.lower())
        if installed:
            return "upload" if installed.get("update_available") else "check"
        return "usb" if self.is_board else "book"

    def refresh_listbox_labels(self):
        self.model.refresh_icons()

    def populate(self, items):
        if self.all_items is items:
            return
        previous = self.selected_name()
        self.all_items = items
        self._restore_selection = previous
        self.execute_search()

    def execute_search(self):
        self._search_timer.stop()
        items = self.all_items
        query = self.search_entry.text().strip().casefold()
        # Repeating the latest request keeps its rows and selection untouched.
        if self._requested == (id(items), query):
            return
        self._requested = (id(items), query)
        self._search_revision += 1
        revision = self._search_revision

        def completed(names, error):
            if revision != self._search_revision or items is not self.all_items:
                return
            if error:
                self._requested = None  # Keep the failed query retryable.
                self.lbl_search_status.setText(f"Search unavailable: {error}")
                return
            if names is not None:
                self._show_names(names)
        if len(items) <= 1000:
            self._search_worker.cancel()
            completed(search_catalog((items, query), threading.Event()), None)
        else:
            self.lbl_search_status.setText("Searching…")
            self._search_worker.submit((items, query), completed)

    def _show_names(self, names):
        selected = getattr(self, "_restore_selection", None) or self.selected_name()
        self._restore_selection = None
        self.filtered_names = names
        blocker = self.listbox.selectionModel().blockSignals(True)
        self.model.set_rows(names)
        self.listbox.selectionModel().blockSignals(blocker)
        total = len(names)
        if total:
            self.lbl_search_status.setText(f"Found {total:,} {'match' if total == 1 else 'matches'}")
        elif not getattr(self.app, "_is_online", True) and not self.all_items:
            self.lbl_search_status.setText("Offline: connect to the internet to browse the catalog")
        else:
            self.lbl_search_status.setText("No matches found")
        if selected in self.all_items and selected in names:
            self.select_name(selected)
        else:
            self._detail_stack.setCurrentIndex(0)

    def select_name(self, name):
        row = self.filtered_names.index(name)
        index = self.model.index(row)
        self.listbox.setCurrentIndex(index)
        self.listbox.scrollTo(index)

    def selected_name(self):
        index = self.listbox.currentIndex() if hasattr(self, "listbox") else QModelIndex()
        rows = self.model.rows() if hasattr(self, "model") else []
        return rows[index.row()] if index.isValid() and index.row() < len(rows) else None

    def selected_item(self):
        return self.all_items.get(self.selected_name())

    def selected_version(self):
        item = self.selected_item() or {}
        chosen = self.version_combo.currentText()
        return next((entry for entry in item.get("versions", []) if entry.get("version") == chosen), None)

    def _on_current_changed(self, current, _previous):
        if current.isValid():
            self._show_item(self.model.rows()[current.row()])

    def _show_item(self, name):
        item = self.all_items.get(name)
        if not item:
            return
        self._detail_stack.setCurrentIndex(1)
        self.app._on_select(self, item)
        self.detail_scroll.verticalScrollBar().setValue(0)

    def set_versions(self, item):
        blocker = self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItems([entry["version"] for entry in item.get("versions", [])])
        self.version_combo.setCurrentIndex(0)
        self.version_combo.blockSignals(blocker)

    def _on_download_clicked(self):
        self._download_action()

    def set_download_action(self, text, callback, *, primary=True, enabled=True):
        self._download_action = callback
        self.download_btn.setText(text)
        self.download_btn.setEnabled(enabled)
        role = "primary" if primary else ""
        if self.download_btn.property("role") != role:
            self.download_btn.setProperty("role", role)
            self.download_btn.style().unpolish(self.download_btn)
            self.download_btn.style().polish(self.download_btn)


class InstalledTab(QWidget):
    """Local packages with lazy detail scans that a new selection cancels."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.installed_items = []
        self.filtered_items = []
        self._all_examples = []
        self._all_boards = []
        self._current_examples = []
        self._select_id = 0
        self._loaded_id = None
        self._detail_worker = LatestScan(app._tasks, scan_package_details)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Search"))
        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("Filter installed packages (Ctrl+F)")
        self.search_entry.setClearButtonEnabled(True)
        self.search_entry.textChanged.connect(lambda _text: self._search_timer.start())
        row.addWidget(self.search_entry, 1)
        self.lbl_search_status = QLabel("0 installed")
        self.lbl_search_status.setProperty("packageTone", "dim")
        row.addWidget(self.lbl_search_status)
        layout.addLayout(row)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self.execute_search)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        left, left_layout = _card()
        left_layout.addWidget(_reading("Installed packages", bold=True))
        self.model = PackageListModel(
            lambda item: f"{item['name']} ({item['installed_version']})",
            lambda item: "upload" if item.get("update_available") else
            "book" if str(item.get("type", "")).casefold() == "library" else "usb", self)
        self.listbox = _package_list(self.model)
        self.listbox.selectionModel().currentChanged.connect(self._on_current_changed)
        left_layout.addWidget(self.listbox, 1)
        left.setMinimumWidth(150)
        splitter.addWidget(left)

        detail, detail_layout = _card()
        detail.setMinimumWidth(250)
        self._stack = QStackedWidget()
        placeholder = _reading("Select an installed package to see its details.", tone="dim")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(placeholder)
        view = QWidget()
        form = QVBoxLayout(view)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(5)
        self.lbl_name = IconLabel("", icon_name="book")
        self.lbl_name.setWordWrap(True)
        self.lbl_name.setProperty("packageTone", "title")
        form.addWidget(self.lbl_name)
        self.lbl_type = _reading(tone="dim")
        form.addWidget(self.lbl_type)
        self.lbl_installed_ver = _reading()
        form.addWidget(self.lbl_installed_ver)
        self.lbl_latest_ver = _reading()
        form.addWidget(self.lbl_latest_ver)
        self.lbl_update_status = _reading(tone="ok", bold=True)
        form.addWidget(self.lbl_update_status)
        self.lbl_size = _reading(tone="dim")
        form.addWidget(self.lbl_size)
        form.addWidget(_reading("Location on Disk:", bold=True))
        self.lbl_path = _reading(tone="dim")
        self.lbl_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addWidget(self.lbl_path)
        actions = QHBoxLayout()
        self.open_btn = _action("Open Folder", self._open_folder)
        actions.addWidget(self.open_btn)
        self.update_btn = _action("Update", self._update_item, role="primary")
        self.update_btn.setEnabled(False)
        actions.addWidget(self.update_btn)
        self.delete_btn = _action("Delete", self._delete_item, danger=True)
        actions.addWidget(self.delete_btn)
        actions.addStretch(1)
        form.addLayout(actions)
        self.lbl_examples_header = _reading("Sample Codes (Examples):", bold=True)
        form.addWidget(self.lbl_examples_header)
        self.lbl_examples_hint = _reading(tone="dim")
        form.addWidget(self.lbl_examples_hint)
        self.examples_search = QLineEdit()
        self.examples_search.setPlaceholderText("Filter")
        self.examples_search.setClearButtonEnabled(True)
        self.examples_search.textChanged.connect(lambda _text: self._render_examples())
        form.addWidget(self.examples_search)
        self.examples_model = QStringListModel(self)
        self.examples_list = QListView()
        self.examples_list.setObjectName("package-list")
        self.examples_list.setModel(self.examples_model)
        self.examples_list.setUniformItemSizes(True)
        self.examples_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.examples_list.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.examples_list.doubleClicked.connect(self._open_example)
        form.addWidget(self.examples_list, 1)
        self._stack.addWidget(view)
        detail_layout.addWidget(self._stack, 1)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([360, 560])

    def populate(self, items):
        if self.installed_items is items:
            return
        self.installed_items = items
        self.execute_search()

    def selected_item(self):
        index = self.listbox.currentIndex()
        return self.filtered_items[index.row()] if index.isValid() and index.row() < len(self.filtered_items) else None

    def execute_search(self):
        self._search_timer.stop()
        selected = self.selected_item()
        selected_path = selected["path"] if selected else None
        query = self.search_entry.text().strip().casefold()
        if not query:
            self.filtered_items = list(self.installed_items)
        else:
            starts, contains = [], []
            for item in self.installed_items:
                name = item["name"].casefold()
                if name.startswith(query):
                    starts.append(item)
                elif query in name or query in item.get("type", "").casefold():
                    contains.append(item)
            self.filtered_items = starts + contains
        self.lbl_search_status.setText(f"{len(self.filtered_items)} installed")
        blocker = self.listbox.selectionModel().blockSignals(True)
        self.model.set_rows(self.filtered_items)
        self.listbox.selectionModel().blockSignals(blocker)
        for row, item in enumerate(self.filtered_items):
            if item["path"] == selected_path:
                index = self.model.index(row)
                self.listbox.setCurrentIndex(index)
                self.listbox.scrollTo(index)
                return
        self._select_id += 1
        self._detail_worker.cancel()
        self._stack.setCurrentIndex(0)
        self.update_btn.setEnabled(False)

    def _on_current_changed(self, current, _previous):
        if current.isValid() and current.row() < len(self.filtered_items):
            self.show_item(self.filtered_items[current.row()])

    def show_item(self, item):
        self._package_root = str(item["path"])
        self._select_id += 1
        request = self._select_id
        self._stack.setCurrentIndex(1)
        library = item["type"] == "Library"
        self.lbl_name.set_icon("book" if library else "usb")
        self.lbl_name.setText(item["name"])
        self.lbl_type.setText(f"Type: {item['type']}")
        self.lbl_installed_ver.setText(f"Installed version: {item['installed_version']}")
        self.lbl_latest_ver.setText(f"Latest version: {item['latest_version']}")
        if item.get("update_available"):
            self.lbl_update_status.setText("Update available")
            _set_tone(self.lbl_update_status, "warn")
        else:
            self.lbl_update_status.setText("Up-to-date" if item.get("latest_version") not in
                                           ("—", "— (Offline)", item.get("installed_version")) else "Installed")
            _set_tone(self.lbl_update_status, "ok")
        self.update_btn.setEnabled(bool(item.get("update_available")))
        self.lbl_path.setText(item["path"])
        self.lbl_examples_header.setText("Sample Codes (Examples):" if library else "Available Boards:")
        self.lbl_examples_hint.setText("Double-click a sketch to view its code" if library else
                                       "Supported microcontrollers inside this downloaded platform package")
        self.lbl_size.setText("Size on Disk: calculating…")
        self._all_examples, self._all_boards = [], []
        self._loaded_id = None
        blocker = self.examples_search.blockSignals(True)
        self.examples_search.clear()
        self.examples_search.blockSignals(blocker)
        self._current_examples = []
        self.examples_model.setStringList(["Scanning this package in the background…"])

        def completed(result, error):
            if request != self._select_id:
                return
            if error or result is None:
                self.lbl_size.setText("Size on Disk: unavailable")
                self.examples_model.setStringList([f"(scan unavailable: {error or 'cancelled'})"])
                return
            size, self._all_examples, self._all_boards = result
            text = f"{size / (1024 * 1024):.1f} MB" if size > 1024 * 1024 else f"{size / 1024:.0f} KB"
            self.lbl_size.setText(f"Size on Disk: {text}")
            self._loaded_id = request
            self._render_examples()
        self._detail_worker.submit(item, completed)

    def _render_examples(self):
        if self._loaded_id != self._select_id:
            return
        query = self.examples_search.text().strip().casefold()
        item = self.selected_item()
        self._current_examples = []
        if item and item["type"] == "Library":
            labels = []
            for path in self._all_examples:
                label = f"{os.path.basename(os.path.dirname(path))} / {os.path.basename(path)}"
                if not query or query in label.casefold():
                    labels.append(label)
                    self._current_examples.append(path)
            empty = "(no sample sketches found)" if not self._all_examples else "(no matching sample sketches found)"
        else:
            labels = [f"•  {board}" for board in self._all_boards if not query or query in board.casefold()]
            empty = "(no board definitions found)" if not self._all_boards else "(no matching board definitions found)"
        self.examples_model.setStringList(labels or [empty])

    def _open_example(self, index):
        if not self._current_examples or index.row() >= len(self._current_examples):
            return
        path = self._current_examples[index.row()]
        if not os.path.exists(path):
            QMessageBox.critical(self, "Error", "Sample file no longer exists on disk.")
            return
        paths = tuple(self._current_examples)

        def is_current():
            current = self.examples_list.currentIndex()
            return (tuple(self._current_examples) == paths and current.isValid()
                    and current.row() < len(paths) and paths[current.row()] == path)
        root = Path(self._package_root)
        self.app.open_code_viewer(path, paths, is_current,
                                 include_roots=(str(root / "src"), str(root / "include"), str(root)))

    def _open_folder(self):
        item = self.selected_item()
        if not item:
            return
        if os.path.exists(item["path"]):
            QDesktopServices.openUrl(QUrl.fromLocalFile(item["path"]))
        else:
            QMessageBox.critical(self, "Error", "Folder does not exist or has been deleted outside this browser.")
            self.app._compute_installed_items_async()

    def _update_item(self):
        if self.app._busy and self.app._active_download_tab is not None:
            self.app._cancel_download()
            return
        item = self.selected_item()
        if not item or not item.get("update_available"):
            self.update_btn.setEnabled(False)
            return
        self.app._download_update(item["name"], item["type"] == "Board Platform", item.get("path", ""), item.get("archive", ""))

    def _delete_item(self):
        item = self.selected_item()
        if not item or self.app._busy:
            return
        answer = QMessageBox.warning(
            self, "Confirm Delete",
            f"Are you sure you want to permanently delete the {item['type'].lower()} '{item['name']}' "
            f"(version {item['installed_version']})?\n\nPath: {item['path']}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.delete_btn.setEnabled(False)
        self.app._set_status(f"Deleting {item['name']}…")

        def remove(path=item["path"], name=item["name"]):
            error = ""
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.isfile(path):
                    os.remove(path)
            except OSError as exc:
                error = str(exc)
            self.app._post_ui(self._deleted, name, error)
        self.app._tasks.start(remove, failed=lambda error: self._deleted(item["name"], error))

    def _deleted(self, name, error):
        self.delete_btn.setEnabled(True)
        if error:
            QMessageBox.critical(self, "Error", f"Failed to delete item:\n{error}")
            return
        self.app._set_status(f"Deleted {name}")
        self.app._compute_installed_items_async()
        self.app._update_version_status(self.app.lib_tab)
        self.app._update_version_status(self.app.board_tab)

    def refresh_update_button_state(self):
        item = self.selected_item()
        self.update_btn.setText("Update")
        self.update_btn.setEnabled(bool(item and item.get("update_available")))


class DownloadOptionsDialog(GlassDialog):
    """Choose what to keep from a download; the result is its chosen option."""

    def __init__(self, parent, archive_name, already_available=False):
        super().__init__(parent)
        self.setWindowTitle("Download Options")
        self.result_option = ""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        text = f"Choose format for:\n{archive_name}"
        if already_available:
            text += "\n\nExtracted files already exist locally, so only the archive can be downloaded."
        label = _reading(text, tone="warn" if already_available else "bright")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        row = QHBoxLayout()
        if already_available:
            for caption in ("Extracted (Already Available)", "Both (Already Available)"):
                button = _action(caption)
                button.setEnabled(False)
                row.addWidget(button)
            row.addWidget(self._choice("Archive Only (ZIP)", "zip", primary=True))
        else:
            row.addWidget(self._choice("Folder / Extracted (Default)", "folder", primary=True))
            row.addWidget(self._choice("Both (ZIP & Folder)", "both"))
            row.addWidget(self._choice("Archive Only", "zip"))
        layout.addLayout(row)
        cancel = _action("Cancel", self.reject)
        cancel.setAutoDefault(False)
        layout.addWidget(cancel, 0, Qt.AlignmentFlag.AlignHCenter)

    def _choice(self, text, option, *, primary=False):
        button = _action(text, lambda: self._choose(option), role="primary" if primary else "")
        button.setDefault(primary)
        return button

    def _choose(self, option):
        self.result_option = option
        self.accept()


class BoardPlatformDialog(GlassDialog):
    """Associate a board package with a vendor's PlatformIO platform."""

    def __init__(self, app, item, configuration, key):
        super().__init__(app)
        self.app, self._key = app, key
        self.setWindowTitle("Custom board platform")
        layout = QVBoxLayout(self)
        # Only the form scrolls, so Save and Cancel stay visible on short screens.
        self.form_scroll = QScrollArea()
        self.form_scroll.setObjectName("package-detail-scroll")
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        content = QWidget()
        content.setObjectName("package-detail")
        form = QVBoxLayout(content)
        form.setContentsMargins(0, 0, 4, 0)
        form.addWidget(_reading(item["name"], tone="title"))
        form.addWidget(_reading("Use your vendor's PlatformIO platform for custom boards. "
                                "Clear both fields to restore automatic matching."))
        form.addWidget(_reading("Platform specification", bold=True))
        self.platform = QLineEdit(configuration.get("platform", ""))
        form.addWidget(self.platform)
        form.addWidget(_reading("owner/platform@version or an HTTPS platform source", tone="dim"))
        form.addWidget(_reading("Optional exact board IDs", bold=True))
        form.addWidget(_reading("One mapping per line: Arduino declaration ID = PlatformIO board ID", tone="dim"))
        self.mappings = QPlainTextEdit("\n".join(f"{left} = {right}" for left, right in configuration.get("board_ids", {}).items()))
        self.mappings.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.mappings.setMinimumHeight(72)
        form.addWidget(self.mappings, 1)
        self.form_scroll.setWidget(content)
        layout.addWidget(self.form_scroll, 1)
        self.status = _reading(tone="dim")
        self.status.hide()
        layout.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        self.save_button = _action("Save", self.save, role="primary")
        row.addWidget(self.save_button)
        self.cancel_button = _action("Cancel", self.reject)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        fit_dialog(self, preferred=(620, 460), minimum=(320, 260))
        self._screen_watcher = ScreenWatcher(self)

    def _message(self, text):
        self.status.setText(text)
        self.status.show()

    def save(self):
        from src.modules.board_index_targets import normalize_platformio_configuration
        if self.app._busy:
            self._message("Wait for the current download before changing its platform.")
            return
        try:
            parsed = {}
            for line in self.mappings.toPlainText().splitlines():
                if not line.strip():
                    continue
                left, separator, right = line.partition("=")
                if not separator or left.strip() in parsed:
                    raise ValueError("Use one unique Arduino ID = PlatformIO ID mapping per line.")
                parsed[left.strip()] = right.strip()
            value = normalize_platformio_configuration({"platform": self.platform.text(), "board_ids": parsed})
        except ValueError as error:
            self._message(str(error))
            return
        self.save_button.setEnabled(False)
        self._message("Saving board support settings…")

        def persist():
            settings = engine._load_settings()
            existing = settings.get("board_platformio_associations")
            stored = dict(existing) if isinstance(existing, dict) else {}
            if value:
                stored[self._key] = value
            else:
                stored.pop(self._key, None)
            settings["board_platformio_associations"] = stored
            self.app._post_ui(self._finished, stored, engine._save_settings(settings), "")
        self.app._tasks.start(persist, failed=lambda error: self._finished({}, False, error))

    def _finished(self, stored, saved, error):
        if saved:
            self.app._board_platformio_associations = stored
            self.app._set_status("Board platform saved. Download or prepare support to verify the exact targets.")
            self.accept()
            return
        self.save_button.setEnabled(True)
        self._message(f"Settings could not be saved: {error}" if error else
                      "Settings could not be saved. Check that settings storage is writable.")


class ArduinoCliBoardDialog(GlassDialog):
    """Explicit Arduino CLI permissions; every disk read and write stays on workers."""

    def __init__(self, app, tab, metadata, folder):
        super().__init__(app)
        from src.modules.arduino_board_selection import association_key
        self.app, self.tab, self.metadata, self.folder = app, tab, metadata, folder
        self._key = association_key(metadata)
        self._version = str(metadata.get("version") or "")
        self._cancelled = threading.Event()
        self._rows, self._chosen = [], set()
        self._loaded = self._saving = False
        self.setWindowTitle("Choose Arduino CLI boards")
        layout = QVBoxLayout(self)
        layout.addWidget(_reading(str(metadata.get("name") or "Board package"), tone="title"))
        layout.addWidget(_reading("Arduino CLI is available only for Arduino UNO Q and Raspberry Pi Pico 2 / RP2350. "
                                  "Check an exact board to enable it; other boards use PlatformIO."))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search board name or exact ID")
        self.search.textChanged.connect(lambda _text: self._render())
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.list.itemChanged.connect(self._toggled)
        self.list.itemClicked.connect(self._row_clicked)
        self._indicator_toggled = False
        layout.addWidget(self.list, 1)
        self.status = _reading("Reading downloaded boards…", tone="dim")
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.clear_button = _action("Clear choices", self._clear)
        self.clear_button.setEnabled(False)
        row.addWidget(self.clear_button)
        row.addStretch(1)
        self.save_button = _action("Save", self.save, role="primary")
        self.save_button.setEnabled(False)
        row.addWidget(self.save_button)
        self.cancel_button = _action("Cancel", self.reject)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        fit_dialog(self, preferred=(640, 470), minimum=(320, 260))
        self._screen_watcher = ScreenWatcher(self)
        self.finished.connect(lambda _code: self._cancelled.set())
        if self.app._tasks.start(self._scan, failed=self._scan_failed) is False:
            self._scan_failed("The package browser is closing.")

    def _scan(self):
        from src.modules.arduino_board_chooser import read_board_choices
        from src.modules.arduino_board_selection import board_allowed, load_preferences, selected_boards
        selected = selected_boards(self.metadata, load_preferences(force_read=True))
        try:
            choices = [(identifier, name) for identifier, name in read_board_choices(self.folder, self._cancelled)
                       if board_allowed(self.metadata, identifier)]
            error = ""
        except (OSError, ValueError) as exc:
            choices, error = [], str(exc)
        self.app._post_ui(self._present, choices, selected, error)

    def _scan_failed(self, error):
        self.status.setText(f"Board choices could not be read: {error}")

    def _present(self, choices, selected, error):
        from src.modules.arduino_board_selection import board_allowed
        if self._cancelled.is_set():
            return
        selected = {identifier for identifier in selected if board_allowed(self.metadata, identifier)}
        self._chosen.update(selected)
        self._rows.extend(choices)
        present = {identifier for identifier, _name in choices}
        self._rows.extend((identifier, "Saved board (absent from this version)") for identifier in sorted(selected - present))
        self._loaded = True
        self.save_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self._render()
        if error:
            self.status.setText(error + " Existing choices can still be cleared.")
        elif not choices:
            self.status.setText("This version has no eligible UNO Q / Pico 2 board IDs. Existing choices can still be cleared.")

    def _render(self):
        query = self.search.text().strip().casefold()
        blocker = self.list.blockSignals(True)
        self.list.clear()
        shown = 0
        for identifier, name in self._rows:
            if query and query not in identifier.casefold() and query not in name.casefold():
                continue
            row = QListWidgetItem(f"{name}  [{identifier}]")
            row.setData(Qt.ItemDataRole.UserRole, identifier)
            row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked if identifier in self._chosen else Qt.CheckState.Unchecked)
            self.list.addItem(row)
            shown += 1
        self.list.blockSignals(blocker)
        if self._loaded and not self._saving:
            self.status.setText(f"{len(self._chosen)} allowed; {shown} of {len(self._rows)} shown.")

    def _row_clicked(self, row):
        # A click on the check indicator already toggled through itemChanged.
        if not self._indicator_toggled:
            row.setCheckState(Qt.CheckState.Unchecked if row.checkState() == Qt.CheckState.Checked
                              else Qt.CheckState.Checked)
        self._indicator_toggled = False

    def _toggled(self, row):
        from src.modules.arduino_board_selection import board_allowed
        self._indicator_toggled = True
        identifier = row.data(Qt.ItemDataRole.UserRole)
        if self._saving or not board_allowed(self.metadata, identifier):
            self._chosen.discard(identifier)
            self._render()
            return
        if row.checkState() == Qt.CheckState.Checked:
            self._chosen.add(identifier)
        else:
            self._chosen.discard(identifier)
        self.status.setText(f"{len(self._chosen)} board(s) allowed.")

    def _clear(self):
        if self._loaded and not self._saving:
            self._chosen.clear()
            self._render()

    def _still_selected(self):
        from src.modules.arduino_board_selection import association_key
        item = self.tab.selected_item() or {}
        version = self.tab.selected_version()
        return (version is not None and version.get("version") == self._version
                and association_key({**item, **version}) == self._key)

    def save(self):
        from src.modules.arduino_board_selection import FIELD, board_allowed
        if not self._loaded or self._saving:
            return
        if self.app._busy or not self._still_selected():
            self.status.setText("Wait for the current operation, then reopen this chooser for the selected package version.")
            return
        self._saving = True
        self.save_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.status.setText("Saving Arduino CLI choices…")
        values = sorted(identifier for identifier in self._chosen if board_allowed(self.metadata, identifier))

        def persist():
            settings = engine._load_settings()
            existing = settings.get(FIELD)
            stored = dict(existing) if isinstance(existing, dict) else {}
            if values:
                stored[self._key] = values
            else:
                stored.pop(self._key, None)
            settings[FIELD] = stored
            self.app._post_ui(self._saved, engine._save_settings(settings), "")
        if self.app._tasks.start(persist, failed=lambda error: self._saved(False, error)) is False:
            self._saved(False, "The package browser is closing.")

    def _saved(self, success, error):
        from src.modules.arduino_board_selection import invalidate_preferences
        self._saving = False
        if success:
            invalidate_preferences()
            self.app._set_status("Arduino CLI choices saved for eligible UNO Q / Pico 2 boards. "
                                 "Prepare board support to verify them; other boards use PlatformIO.")
            self.accept()
            return
        self.save_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self.status.setText(f"Settings could not be saved: {error}" if error else
                            "Settings could not be saved. Check that settings storage is writable.")


_EXTRA_STYLE = """
QFrame#package-card {{ background: {glass}; border: 1px solid {rim}; border-radius: 14px; }}
QWidget#package-detail, QScrollArea#package-detail-scroll {{ background: transparent; }}
QFrame#package-rule {{ background: {border}; border: none; min-height: 1px; max-height: 1px; }}
QLabel[packageTone="title"] {{ color: {text_bright}; font-size: 15px; font-weight: 700; }}
QLabel[packageTone="bright"] {{ color: {text_bright}; }}
QLabel[packageTone="dim"] {{ color: {text_dim}; }}
QLabel[packageTone="ok"] {{ color: {green}; }}
QLabel[packageTone="warn"] {{ color: {yellow}; }}
QLabel[packageTone="error"] {{ color: {red}; }}
QLabel#package-brand {{ color: {text_bright}; font-size: 17px; font-weight: 700; }}
QListView#package-list {{ background: {reading}; color: {text}; border: 1px solid {reading_rim};
                          border-radius: 10px; outline: 0; }}
QListView#package-list::item {{ padding: 4px 6px; border-radius: 4px; }}
QListView#package-list::item:selected {{ background: {selection}; color: {text_bright}; }}
QListView#package-list::item:hover:!selected {{ background: {hover}; }}
QPushButton#package-link {{ background: transparent; border: none; color: {cyan}; padding: 2px 4px;
                            text-align: left; }}
QPushButton#package-link:hover {{ color: {text_bright}; }}
QPushButton[packageDanger="true"]:enabled {{ background: {btn_stop}; color: #ffffff; }}
"""


class ArduinoBrowser(engine.BrowserEngine, QMainWindow):
    """Libraries, Boards and Installed, retained in memory while hidden."""

    def __init__(self, *, linux_channel=None):
        from src.modules.ubuntu_release import require_supported_ubuntu
        require_supported_ubuntu()
        QMainWindow.__init__(self)
        self._linux_channel = linux_channel
        self.setWindowTitle("Arduino Library & Board Browser")
        self._tasks = QtTasks(self)
        self._busy = False
        self._active_download_tab = None
        self._downloading_item_name = None
        self._cancel_event = threading.Event()
        self._is_hidden = False
        self._is_online = True
        self._installed_items = []
        self._installed_map = {}
        self._catalog_revision = 0
        self._inventory_request_id = 0
        self._viewer_request = 0
        self._package_event_root = None

        settings = engine._load_settings()
        saved = settings.get("download_dir", "")
        self._additional_board_urls = engine.parse_additional_board_urls(settings.get("additional_board_urls", []))
        associations = settings.get("board_platformio_associations", {})
        self._board_platformio_associations = associations if isinstance(associations, dict) else {}
        self._download_dir = saved if saved and os.path.isdir(saved) else engine.DEFAULT_DOWNLOAD_DIR
        os.makedirs(self._download_dir, exist_ok=True)
        os.makedirs(engine.INDEX_CACHE_DIR, exist_ok=True)

        self._inventory_worker = LatestScan(self._tasks, self._scan_inventory_request)
        self._viewer_scan = LatestScan(self._tasks, self._probe_code_viewer)
        # Style first: every widget then polishes once instead of twice.
        self._apply_current_theme(force=True)
        self._build_ui()
        fit_dialog(self, preferred=(1040, 740), minimum=(480, 400))
        self._screen_watcher = ScreenWatcher(self)
        if sys.platform == "win32":
            self._publish_window_handle()
        self._start_manager_listener()

    # ── Window ───────────────────────────────────────────────────────────
    def _build_ui(self):
        central = GlassWorkspace()
        self._central = central
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        # The title bar already names the window, so a clipped copy is only noise.
        self._brand = brand = QLabel("Library & Board Browser")
        brand.setObjectName("package-brand")
        header.addWidget(brand)
        header.addStretch(1)
        self.refresh_btn = _action("Refresh indexes", self._refresh_all)
        header.addWidget(self.refresh_btn)
        self.sources_btn = _action("Board indexes", self._toggle_sources, icon_name="globe")
        header.addWidget(self.sources_btn)
        root.addLayout(header)

        folder = QHBoxLayout()
        folder.addWidget(QLabel("Save to"))
        self.folder_entry = QLineEdit(self._download_dir)
        self.folder_entry.editingFinished.connect(self._apply_folder_entry)
        folder.addWidget(self.folder_entry, 1)
        self.browse_btn = _action("Browse…", self._choose_download_dir)
        folder.addWidget(self.browse_btn)
        root.addLayout(folder)

        self._sources_card, sources = _card()
        row = QHBoxLayout()
        row.addWidget(QLabel("Vendor URLs"))
        self.board_urls_entry = QLineEdit(", ".join(self._additional_board_urls))
        row.addWidget(self.board_urls_entry, 1)
        self.board_urls_apply_btn = _action("Apply", self._apply_board_urls, icon_name="check")
        row.addWidget(self.board_urls_apply_btn)
        sources.addLayout(row)
        sources.addWidget(_reading("Add vendor index URLs separated by commas or new lines. "
                                   "Each source is checked independently.", tone="dim"))
        self.sources_status = _reading(tone="dim")
        sources.addWidget(self.sources_status)
        link = _action("Find third-party board index URLs", lambda: QDesktopServices.openUrl(QUrl(THIRD_PARTY_INDEXES)), icon_name="globe")
        link.setObjectName("package-link")
        sources.addWidget(link, 0, Qt.AlignmentFlag.AlignLeft)
        self._sources_card.hide()
        root.addWidget(self._sources_card)

        self.notebook = QTabWidget()
        self.notebook.setDocumentMode(True)
        self.notebook.tabBar().setDrawBase(False)
        self.notebook.tabBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lib_tab = BrowseTab(self, is_board=False)
        self.board_tab = BrowseTab(self, is_board=True)
        self.installed_tab = InstalledTab(self)
        self.notebook.addTab(self.lib_tab, "Libraries")
        self.notebook.addTab(self.board_tab, "Boards")
        self.notebook.addTab(self.installed_tab, "Installed")
        self.notebook.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.notebook, 1)

        bottom = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.status_label.setProperty("packageTone", "dim")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bottom.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        bottom.addWidget(self.progress)
        root.addLayout(bottom)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        self.lib_tab.search_entry.setFocus()

    def _focus_search(self):
        page = self.notebook.currentWidget()
        page.search_entry.setFocus()
        page.search_entry.selectAll()

    def _apply_current_theme(self, force=False):
        mode = engine._resolve_lib_req_theme()
        previous = Theme.active_theme
        Theme.apply_theme(mode)
        if not force and Theme.active_theme == previous:
            return
        palette = Theme.PALETTES[Theme.active_theme]
        tokens = {name.lower(): value for name, value in palette.items()}
        tokens.update(glass_tokens(Theme.active_theme))
        QApplication.instance().setStyleSheet(build_stylesheet(Theme.active_theme) + _EXTRA_STYLE.format(**tokens))
        if hasattr(self, "notebook"):
            self._central.invalidate_cache()
            for tab in (self.lib_tab, self.board_tab, self.installed_tab):
                tab.model.refresh_icons()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._brand.setVisible(self.width() >= 560)

    def showEvent(self, event):
        super().showEvent(event)
        # No relaunch command: the browser has its own taskbar group but no launcher.
        window_shown(self, app_id=BROWSER_USER_MODEL_ID, relaunch=False)

    def hideEvent(self, event):
        super().hideEvent(event)
        window_hidden(self, event)

    def closeEvent(self, event):
        """Retain the loaded window; only workspace shutdown ends this process."""
        if getattr(self, "_exiting", False):
            event.accept()
            return
        event.ignore()
        self._is_hidden = True
        self.hide()

    def _publish_window_handle(self):
        try:
            with open(os.path.join(engine.INDEX_CACHE_DIR, ".dm_hwnd"), "w", encoding="utf-8") as stream:
                stream.write(str(int(self.winId())))
        except OSError:
            pass
        for stale in (".dm_force_exit", ".show_dm_trigger"):
            try:
                os.remove(os.path.join(engine.INDEX_CACHE_DIR, stale))
            except OSError:
                pass

    def _start_manager_listener(self):
        """Native readiness on POSIX; on Windows a directory watch wakes instantly.

        The slow timer only covers storage where directory notifications fail.
        """
        self._channel_notifier = None
        channel = self._linux_channel
        if channel is not None:
            self._channel_notifier = QSocketNotifier(channel.fileno(), QSocketNotifier.Type.Read, self)
            self._channel_notifier.activated.connect(lambda _socket: self._drain_channel())
            self._partial_timer = QTimer(self)
            self._partial_timer.setSingleShot(True)
            self._partial_timer.setInterval(25)
            self._partial_timer.timeout.connect(self._drain_channel)
        elif sys.platform == "win32":
            self._trigger_watch = QFileSystemWatcher([engine.INDEX_CACHE_DIR], self)
            self._trigger_watch.directoryChanged.connect(lambda _path: self._check_show_trigger())
            self._trigger_timer = QTimer(self)
            self._trigger_timer.setInterval(1000)
            self._trigger_timer.timeout.connect(self._check_show_trigger)
            self._trigger_timer.start()

    def _drain_channel(self):
        channel = self._linux_channel
        if channel is None:
            return
        for command in channel.poll():
            if command == "quit":
                self._force_exit()
                return
            if command == "wake":
                self._unhide_window()
        if channel.has_partial():
            self._partial_timer.start()

    def _check_show_trigger(self):
        force = os.path.join(engine.INDEX_CACHE_DIR, ".dm_force_exit")
        if os.path.exists(force):
            try:
                os.remove(force)
            except OSError:
                pass
            self._force_exit()
            return
        trigger = os.path.join(engine.INDEX_CACHE_DIR, ".show_dm_trigger")
        if os.path.exists(trigger):
            try:
                os.remove(trigger)
            except OSError:
                pass
            self._unhide_window()

    def _unhide_window(self):
        """Restore instantly from memory without reloading any index."""
        self._apply_current_theme()
        self._is_hidden = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _force_exit(self):
        self._exiting = True
        self._cancel_event.set()
        self._tasks.close()
        if self._linux_channel is not None:
            # The notifier must stop before its descriptor closes.
            self._channel_notifier.setEnabled(False)
            self._linux_channel.close()
            self._linux_channel = None
        if sys.platform != "win32":
            manager.close_viewers()
        else:
            try:
                os.remove(os.path.join(engine.INDEX_CACHE_DIR, ".dm_hwnd"))
            except OSError:
                pass
        self.close()
        QApplication.instance().quit()

    # ── Boundary used by the engine and workers ──────────────────────────
    def _post_ui(self, callback, *args):
        key = "download-progress" if getattr(callback, "__name__", "") == "_update_progress" else None
        self._tasks.post(callback, *args, key=key)

    def _set_status(self, text):
        self.status_label.setText(str(text))
        self.status_label.setToolTip(str(text))

    def _set_sources_status(self, text):
        self.sources_status.setText(text)

    def _publish_catalogs(self, libraries, boards):
        changed = False
        for tab, items in ((self.lib_tab, libraries), (self.board_tab, boards)):
            if items is not None and tab.all_items is not items:
                tab.populate(items)
                changed = True
        if changed:
            self._catalog_revision += 1

    def _toggle_sources(self):
        visible = not self._sources_card.isVisible()
        self._sources_card.setVisible(visible)
        self.sources_btn.setText("Hide indexes" if visible else "Board indexes")

    def _on_tab_changed(self, index):
        # Typing searches. It must never land in the download folder by accident.
        self.notebook.widget(index).search_entry.setFocus()
        if index == 2:
            if self.installed_tab.installed_items is not self._installed_items:
                self.installed_tab.populate(self._installed_items)
            self._compute_installed_items_async()

    def _compute_installed_items_async(self):
        self._inventory_request_id += 1
        request_id = self._inventory_request_id
        revision = self._catalog_revision
        folder = self._download_dir

        def completed(items, error):
            if request_id != self._inventory_request_id or revision != self._catalog_revision or folder != self._download_dir:
                return
            if error:
                self._set_status(f"Installed packages could not be scanned: {error}")
                return
            if items is None:
                return
            on_installed = self.notebook.currentIndex() == 2
            if items == self._installed_items and self._installed_map:
                if on_installed and self.installed_tab.installed_items is not self._installed_items:
                    self.installed_tab.populate(self._installed_items)
                return
            self._installed_items = items
            self._installed_map = {item["name"].lower(): item for item in items}
            self.lib_tab.refresh_listbox_labels()
            self.board_tab.refresh_listbox_labels()
            self._update_version_status(self.lib_tab)
            self._update_version_status(self.board_tab)
            if on_installed:
                self.installed_tab.populate(items)
                if not self._busy:
                    self._set_status(f"{len(items)} installed package(s)")
        self._inventory_worker.submit((folder, self.lib_tab.all_items, self.board_tab.all_items, self._is_online), completed)

    def _choose_download_dir(self):
        start = self._download_dir if os.path.isdir(self._download_dir) else os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose download folder", start)
        if chosen:
            self.folder_entry.setText(chosen)
            self._apply_folder_entry()

    def _apply_folder_entry(self):
        text = self.folder_entry.text().strip()
        if text == self._download_dir:
            return
        if self._busy:
            self.folder_entry.setText(self._download_dir)
            self._set_status("Wait for the current download before changing folders")
            return
        if not text:
            self.folder_entry.setText(self._download_dir)
            return
        path = os.path.normpath(text)
        if os.path.basename(path) != "_MCUFlasherByNaph_src":
            path = os.path.join(path, "_MCUFlasherByNaph_src")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            self.folder_entry.setText(self._download_dir)
            self._set_status("Invalid folder path — reverted")
            return
        self._download_dir = path
        self.folder_entry.setText(path)
        settings = engine._load_settings()
        settings["download_dir"] = path
        if engine._save_settings(settings):
            self._set_status(f"Download folder set to {path}")
        else:
            self._set_status("Download folder changed for this session; settings could not be saved")
        self._compute_installed_items_async()
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)

    def _apply_board_urls(self):
        if self._busy:
            return
        raw = self.board_urls_entry.text()
        invalid = engine.invalid_additional_board_urls(raw)
        if invalid:
            preview = "\n".join(f"• {url}" for url in invalid[:8])
            if len(invalid) > 8:
                preview += f"\n• …and {len(invalid) - 8} more"
            QMessageBox.critical(self, "Invalid Board Manager URL",
                                 "Each additional board manager URL must be a complete HTTP or HTTPS URL.\n\n"
                                 f"Invalid entries:\n{preview}")
            return
        urls = engine.parse_additional_board_urls(raw)
        settings = engine._load_settings()
        settings["additional_board_urls"] = urls
        if not engine._save_settings(settings):
            self._set_status("Board indexes could not be saved; check that settings storage is writable")
            return
        self._additional_board_urls = urls
        self.board_urls_entry.setText(", ".join(urls))
        self._set_status("Board manager URLs saved — refreshing indexes…")
        self._refresh_all()

    def _open_link(self, tab, link_type):
        url = tab._current_links.get(link_type, "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ── Selection ────────────────────────────────────────────────────────
    def _on_select(self, tab, item):
        tab.lbl_name.setText(item.get("name", ""))
        if tab.is_board:
            texts = (f"Package: {item.get('package', '')}", f"Maintainer: {item.get('maintainer', '')}",
                     f"Architecture: {item.get('architecture', '')}", f"Category: {item.get('category', '')}")
            tab.lbl_boards.setText(", ".join(item.get("boards") or ()) or "—")
            tab.set_link(tab.link_primary, "website", item.get("website", ""))
            tab.set_link(tab.link_secondary, "help", item.get("help_url", ""))
        else:
            author = f"Author: {item.get('author', '')}"
            if item.get("maintainer"):
                author += f"  •  Maintainer: {item['maintainer']}"
            architectures = ", ".join(item.get("architectures") or ()) or "*"
            texts = (author, f"Category: {item.get('category', '')}", f"Architectures: {architectures}", "")
            tab.lbl_sentence.setText(item.get("sentence", ""))
            tab.lbl_paragraph.setText(re.sub(r"<[^>]+>", " ", item.get("paragraph", "")))
            tab.set_link(tab.link_primary, "website", item.get("website", ""))
            tab.set_link(tab.link_secondary, "repo", item.get("repository", ""))
        for label, text in zip(tab.lbl_meta, texts):
            label.setText(text)
            label.setVisible(bool(text))
        tab.set_versions(item)
        self._update_version_status(tab)

    def _update_version_status(self, tab):
        if tab.is_board:
            tab.arduino_cli_btn.setEnabled(False)
        name = tab.selected_name()
        item = tab.all_items.get(name)
        if not item:
            return
        version = tab.selected_version()
        if not version:
            return
        chosen = version["version"]
        if tab.is_board:
            from src.modules.arduino_board_selection import allowed_board_ids
            tab.arduino_cli_btn.setEnabled(not self._busy and bool(allowed_board_ids({**item, **version})))
            tab.prepare_btn.setEnabled(False)
        try:
            size = int(version.get("size", 0))
        except (ValueError, TypeError):
            size = 0
        tab.lbl_size.setText(f"Size: {size / 1048576:.1f} MB" if size > 1048576 else f"Size: {size / 1024:.0f} KB")
        tab._version_revision += 1
        revision = tab._version_revision
        folder = self._download_dir
        if self._busy and tab is self._active_download_tab and item.get("name") == self._downloading_item_name:
            self._render_version_status(tab, item, chosen, None)
            return
        if tab._status_subject != (name, chosen, folder):
            # An action from the previous selection must never apply to this one,
            # so a stale "Update" cannot remove another package's folder.
            tab._status_subject = name, chosen, folder
            self._render_version_status(tab, item, chosen, None)

        def completed(info, error):
            if (revision != tab._version_revision or folder != self._download_dir
                    or chosen != tab.version_combo.currentText() or tab.selected_name() != name):
                return
            if error:
                tab.lbl_available.setText(f"Local package scan failed: {error}")
                _set_tone(tab.lbl_available, "error")
                return
            self._render_version_status(tab, item, chosen, info)
        tab._version_worker.submit((tab, name, item, folder, tuple(self._installed_items)), completed)

    def _render_version_status(self, tab, item, chosen, info):
        if tab.is_board:
            from src.modules.arduino_board_selection import allowed_board_ids
            version = next((entry for entry in item.get("versions", []) if entry.get("version") == chosen), {})
            tab.arduino_cli_btn.setEnabled(not self._busy and bool(version) and bool(allowed_board_ids({**item, **version})))
            available = bool(info and chosen in info["installed_versions"])
            tab.prepare_btn.setEnabled(available and not self._busy)
        if self._busy and tab is self._active_download_tab and item.get("name") == self._downloading_item_name:
            tab.lbl_available.setText("")
            tab.lbl_status_badge.setText("")
            tab.set_download_action("Cancel", self._cancel_download)
            return
        badge, tone = "", "ok"
        action = ("Download", lambda: self._download(tab), True)
        if info is not None:
            installed = info["installed_version"]
            if chosen in info["installed_versions"]:
                badge = f"Downloaded files (v{chosen})" if tab.is_board else f"Already Available (v{chosen})"
                if info["installed_versions"][chosen]["has_folder"]:
                    action = ("Download (ZIP)", lambda: self._download(tab, already_available=True), False)
            elif engine._version_key(chosen) > engine._version_key(installed):
                best = info["best_info"]
                cleanup = (best.get("folder_path") or best.get("archive_path"),
                           best.get("archive") or os.path.basename(best.get("archive_path", "")))
                badge, tone = f"Update Available (Installed: v{installed})", "warn"
                action = (f"Update to v{chosen}", lambda: self._download(tab, cleanup_old=cleanup), True)
            else:
                badge, tone = f"Installed: v{installed} (Older v{chosen} selected)", "dim"
                action = (f"Download v{chosen}", lambda: self._download(tab), True)
        for label in (tab.lbl_available, tab.lbl_status_badge):
            label.setText(badge)
            _set_tone(label, tone)
            label.setVisible(bool(badge))
        tab.set_download_action(action[0], action[1], primary=action[2], enabled=not self._busy)

    # ── Dialogs ──────────────────────────────────────────────────────────
    def _edit_board_platform(self, tab):
        if self._busy:
            return
        item, version = tab.selected_item(), tab.selected_version()
        if not item or not version:
            return
        from src.modules.board_index_targets import normalize_platformio_configuration
        key = self._board_association_key({**item, **version})
        try:
            configuration = normalize_platformio_configuration(self._board_platformio_associations.get(key))
        except ValueError:
            configuration = {}
        BoardPlatformDialog(self, item, configuration or {}, key).exec()

    def _edit_arduino_cli_boards(self, tab):
        if self._busy:
            return
        item, version = tab.selected_item(), tab.selected_version()
        if not item or not version:
            return
        metadata = {**item, **version}
        from src.modules.arduino_board_selection import allowed_board_ids, association_key
        if not association_key(metadata):
            self._set_status("This package has no valid vendor and architecture identity. Choose another package.")
            return
        if not allowed_board_ids(metadata):
            self._set_status("Arduino CLI is available only for Arduino UNO Q and Raspberry Pi Pico 2 / RP2350. "
                             "Use PlatformIO for other boards.")
            return
        archive = engine._archive_filename(metadata.get("url", ""), metadata.get("archiveFileName", ""))
        folder = os.path.join(self._download_dir, "Boards", engine._get_folder_name(archive))
        ArduinoCliBoardDialog(self, tab, metadata, folder).exec()

    def _prompt_download_option(self, archive, already_available=False):
        dialog = DownloadOptionsDialog(self, archive, already_available)
        return dialog.result_option if dialog.exec() == QDialog.DialogCode.Accepted else ""

    # ── Code viewer ──────────────────────────────────────────────────────
    @staticmethod
    def _probe_code_viewer(_request, cancel):
        return engine._find_code_viewer_python(cancel), get_monitor_font_size()

    def open_code_viewer(self, path, paths, is_current, *, include_roots=()):
        self._viewer_request += 1
        request = self._viewer_request

        def completed(result, error):
            if request != self._viewer_request or not is_current() or self._is_hidden:
                return
            if not os.path.isfile(path):
                QMessageBox.critical(self, "Code Viewer", "Sample file no longer exists on disk.")
                return
            if error:
                QMessageBox.critical(self, "Code Viewer", f"Unable to prepare the sample viewer:\n{error}")
                return
            interpreter, font_size = result if result is not None else (None, 12)
            if interpreter is None:
                QMessageBox.information(self, "Bootstrap required",
                                        "Prepare the code viewer dependencies in Bootstrap, then reopen this sample.")
                return
            engine._launch_code_viewer(path, paths, lambda title, text: QMessageBox.critical(self, title, text),
                                       python_exe=interpreter, theme_mode=Theme.active_theme, font_size=font_size,
                                       include_roots=include_roots)
        self._viewer_scan.submit((path, paths), completed)

    # ── Catalog loading ──────────────────────────────────────────────────
    def _initial_load(self):
        if self._busy:
            return
        self._set_status("Loading saved catalogs…")
        self._start_thread(self._load_both)

    def _refresh_all(self):
        if self._busy:
            return
        self._set_status("Refreshing catalog indexes…")
        self._start_thread(lambda: self._load_both(force_refresh=True))

    def _start_thread(self, target):
        if self._busy:
            return
        self._busy = True
        self.refresh_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self._tasks.start(target, failed=lambda error: self._finish_load(f"Catalog load failed: {error}"))

    def _finish_load(self, message):
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._set_status(message)
        self._busy = False
        self.refresh_btn.setEnabled(True)
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)
        self._compute_installed_items_async()

    # ── Downloads ────────────────────────────────────────────────────────
    def _cancel_download(self):
        self._cancel_event.set()
        self._set_status("Cancelling download…")

    def _begin_transfer(self, tab, name, status):
        self._busy = True
        self._active_download_tab = tab
        self._downloading_item_name = name
        self._cancel_event.clear()
        self._begin_package_job(name)
        tab.set_download_action("Cancel", self._cancel_download)
        self._set_status(status)
        self.progress.setRange(0, 0)

    def _download(self, tab, cleanup_old=None, already_available=False):
        name = tab.selected_name()
        if not name or self._busy:
            return
        self._apply_folder_entry()
        item, version = tab.all_items[name], tab.selected_version()
        if version is None or not version.get("url"):
            QMessageBox.critical(self, "Error", f"No download URL found for version '{tab.version_combo.currentText() or '(none)'}'.")
            return
        try:
            metadata = self._package_metadata(item, version, is_board=tab is self.board_tab)
        except ValueError as error:
            self._set_status(f"Custom platform settings are invalid: {error}")
            return
        archive = engine._archive_filename(version["url"], version.get("archiveFileName", ""))
        destination = os.path.join(self._download_dir, "Boards" if tab.is_board else "Libs")
        option = self._prompt_download_option(archive, already_available)
        if not option:
            return
        self._begin_transfer(tab, name, f"{'Updating' if cleanup_old else 'Downloading'} {archive}…")
        self._tasks.start(self._download_worker, tab, version["url"], archive, destination, option, cleanup_old,
                          metadata, self._active_package_job,
                          failed=lambda error: self._download_start_error(tab, f"Unable to start download:\n{error}"))

    def _download_update(self, name, is_board, old_path="", old_archive=""):
        if self._busy:
            return
        self._apply_folder_entry()
        tab = self.board_tab if is_board else self.lib_tab
        item = tab.all_items.get(name)
        if not item or not item.get("versions"):
            QMessageBox.critical(self, "Error", f"Item '{name}' not found in index.")
            return
        version = item["versions"][0]
        if not version.get("url"):
            QMessageBox.critical(self, "Error", f"No download URL found for version '{version.get('version')}'.")
            return
        try:
            metadata = self._package_metadata(item, version, is_board=is_board)
        except ValueError as error:
            self._set_status(f"Custom platform settings are invalid: {error}")
            return
        archive = engine._archive_filename(version["url"], version.get("archiveFileName", ""))
        destination = os.path.join(self._download_dir, "Boards" if is_board else "Libs")
        self._begin_transfer(tab, name, f"Downloading update {archive}…")
        self.installed_tab.update_btn.setText("Cancel")
        self.installed_tab.update_btn.setEnabled(True)
        # Installed updates replace the usable package folder automatically. The
        # worker still verifies and stages it before removing the old copy.
        self._tasks.start(self._download_worker, tab, version["url"], archive, destination, "folder",
                          (old_path, old_archive), metadata, self._active_package_job,
                          failed=lambda error: self._download_start_error(tab, f"Unable to start download update:\n{error}"))

    def _prepare_existing_board(self, tab):
        if self._busy:
            return
        self._apply_folder_entry()
        name, version = tab.selected_name(), tab.selected_version()
        item = tab.all_items.get(name)
        if not item or not version:
            return
        try:
            metadata = self._package_metadata(item, version, is_board=True)
        except ValueError as error:
            self._set_status(f"Custom platform settings are invalid: {error}")
            return
        self._begin_transfer(tab, name, "Checking downloaded board package…")
        tab.prepare_btn.setEnabled(False)
        destination = os.path.join(self._download_dir, "Boards")
        self._tasks.start(self._prepare_existing_worker, tab, destination, metadata, self._active_package_job,
                          failed=lambda error: self._download_start_error(tab, f"Unable to start preparation:\n{error}"))

    def _download_start_error(self, tab, message):
        try:
            self._tasks.start(lambda: self._publish_job("failed", message=message))
        finally:
            self._download_error(tab, message)

    def _set_progress_determinate(self, total):
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)

    def _update_progress(self, downloaded, total):
        if not self._busy or self._active_download_tab is None or not total:
            return
        self.progress.setValue(min(1000, int(downloaded * 1000 / total)))
        self._set_status(f"Downloading… {int(downloaded / total * 100)}%")

    def _end_transfer(self, tab, status):
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._set_status(status)
        self._busy = False
        self._active_download_tab = None
        self._downloading_item_name = None
        tab.set_download_action("Download", lambda: self._download(tab))
        self.installed_tab.refresh_update_button_state()
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)

    def _download_done(self, tab, _filepath, preparing=False):
        status = "Board preparation is running in the background" if preparing else "Download complete"
        if preparing and getattr(self, "_preparation_tracking_error", None):
            status += f"; background tracking unavailable: {self._preparation_tracking_error}"
        if getattr(self, "_package_tracking_error", None):
            status += "; package tracking could not be saved"
        self._end_transfer(tab, status)
        self._compute_installed_items_async()

    def _download_cancelled(self, tab):
        self._end_transfer(tab, "Download cancelled")

    def _download_error(self, tab, message):
        self._end_transfer(tab, "Download failed")
        QMessageBox.critical(self, "Download Error", message)


def main() -> int:
    channel = None
    cycle_collector = None
    if sys.platform != "win32":
        try:
            channel = manager.DownloadManagerChannel(engine.SCRIPT_DIR)
        except manager.AlreadyRunning:
            return 0 if manager.request(engine.SCRIPT_DIR, "wake") else 1
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(BROWSER_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass
    try:
        from main.qt.interface_size import prepare_interface_size, settle_interface_size
        interface_factor = prepare_interface_size()
        app = QApplication.instance() or QApplication(sys.argv)
        from main.qt.garbage_collection import install_gui_garbage_collector
        cycle_collector = install_gui_garbage_collector(app)
        settle_interface_size(app, interface_factor)
        app.setApplicationName("MCU Flasher Library & Board Browser")
        app.setQuitOnLastWindowClosed(False)
        register_fonts()
        install_no_focus_lines(app)
        icon_path = ROOT / "src" / "assets" / "mcu_icon.ico"
        if icon_path.exists():
            app.setWindowIcon(QIcon(str(icon_path)))
        window = ArduinoBrowser(linux_channel=channel)
        window.show()
        # The window paints first; catalogs arrive after it is already usable.
        QTimer.singleShot(0, window._initial_load)
        return app.exec()
    finally:
        if cycle_collector is not None:
            cycle_collector.stop()
        if channel is not None:
            channel.close()
            manager.close_viewers()


if __name__ == "__main__":
    raise SystemExit(main())
