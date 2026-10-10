"""Bounded, read-only Find All for root sketch sources and unsaved buffers."""
from __future__ import annotations

import os
from pathlib import Path
import re
import threading
import time
from typing import Callable

from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

SKETCH_EXTENSIONS = frozenset({".ino", ".h", ".hpp", ".cpp", ".c", ".txt"})
MAX_FILES = 512
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_MATCHES = 2000
MAX_ENTRIES = 20000
MAX_SECONDS = 8.0


def search_project(project: Path, query: str, *, case_sensitive: bool = False,
                   whole_word: bool = False, buffers: dict[str, str] | None = None,
                   cancel: threading.Event | None = None) -> dict:
    """Scan only regular root source files; never recurse into metadata/assets.

    Buffers replace their disk contents, including empty buffers. All directory
    work and file reads happen in the caller's worker, never in the Qt thread.
    """
    result = {"matches": [], "files": 0, "skipped": 0, "limited": False}
    if not query:
        return result
    started = time.monotonic()

    def stopped():
        if cancel is not None and cancel.is_set():
            result["cancelled"] = True
            return True
        if time.monotonic() - started > MAX_SECONDS:
            result["limited"] = True
            return True
        return False

    pattern = re.escape(query)
    if whole_word:
        pattern = r"(?<!\w)" + pattern + r"(?!\w)"
    matcher = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    try:
        root = project.resolve()
        overlays = {}
        for raw_path, text in (buffers or {}).items():
            if stopped():
                return result
            path = Path(raw_path)
            # Do not let a buffer alias or symlink escape the sketch root.
            if (path.parent.resolve() == root and not path.is_symlink()
                    and not path.name.startswith(".") and path.suffix.lower() in SKETCH_EXTENSIONS):
                overlays[os.path.normcase(str(path.absolute()))] = str(text)
        paths = {}
        with os.scandir(root) as entries:
            for number, entry in enumerate(entries):
                if stopped():
                    return result
                if number >= MAX_ENTRIES:
                    result["limited"] = True
                    break
                name = entry.name
                if (name.startswith(".") or Path(name).suffix.lower() not in SKETCH_EXTENSIONS
                        or not entry.is_file(follow_symlinks=False)):
                    continue
                paths[os.path.normcase(os.path.abspath(entry.path))] = Path(entry.path)
                if len(paths) >= MAX_FILES:
                    result["limited"] = True
                    break
        # A dirty buffer can survive a temporary external deletion of its file.
        for key in overlays:
            if len(paths) >= MAX_FILES and key not in paths:
                result["limited"] = True
                continue
            paths.setdefault(key, Path(key))
        total_bytes = 0
        for key, path in sorted(paths.items(), key=lambda item: item[1].name.casefold()):
            if stopped():
                break
            try:
                if key in overlays:
                    text = overlays[key]
                    if len(text) > MAX_FILE_BYTES:
                        result["skipped"] += 1
                        continue
                    size = len(text.encode("utf-8", errors="replace"))
                    if size > MAX_FILE_BYTES:
                        result["skipped"] += 1
                        continue
                else:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        result["skipped"] += 1
                        continue
                    with path.open("rb") as stream:
                        data = stream.read(MAX_FILE_BYTES + 1)
                    size = len(data)
                    if size > MAX_FILE_BYTES:
                        result["skipped"] += 1
                        continue
                    text = data.decode("utf-8-sig", errors="replace")
                if total_bytes + size > MAX_TOTAL_BYTES:
                    result["limited"] = True
                    break
                total_bytes += size
                result["files"] += 1
                previous_end, line = 0, 1
                for match in matcher.finditer(text):
                    if stopped():
                        break
                    start = match.start()
                    line += text.count("\n", previous_end, start)
                    previous_end = start
                    line_start = text.rfind("\n", 0, start) + 1
                    line_end = text.find("\n", start)
                    if line_end < 0:
                        line_end = len(text)
                    # Monaco columns are UTF-16 code units, not Python codepoints.
                    column = len(text[line_start:start].encode("utf-16-le", errors="surrogatepass")) // 2 + 1
                    end_column = column + len(match.group().encode("utf-16-le", errors="surrogatepass")) // 2
                    snippet_start = max(line_start, start - 90)
                    snippet = text[snippet_start:min(line_end, start + len(query) + 170)].strip()
                    if snippet_start > line_start:
                        snippet = "…" + snippet
                    result["matches"].append({
                        "path": str(path), "name": path.name, "line": line,
                        "column": column, "end_column": end_column,
                        "preview": snippet, "unsaved": key in overlays,
                    })
                    if len(result["matches"]) >= MAX_MATCHES:
                        result["limited"] = True
                        return result
            except OSError:
                result["skipped"] += 1
    except OSError as exc:
        result["error"] = str(exc)
    return result


class ProjectSearchDialog(QDialog):
    """One cancellable scan and one latest pending query per owned dialog."""

    _finished = Signal(dict)

    def __init__(self, project: Path, buffers_provider: Callable[[], dict],
                 navigate: Callable[[str, int, int, int], None], parent: QWidget | None = None):
        super().__init__(parent)
        self.project = Path(project)
        self._buffers_provider, self._navigate = buffers_provider, navigate
        self._generation = 0
        self._running = self._pending = self._closed = False
        self._completed_generation = -1
        self._waiting_for_storage = False
        self._cancel = None
        self.setWindowTitle("Find all in project")
        self.setModal(False)
        from main.qt.responsive import fit_dialog, ScreenWatcher
        fit_dialog(self, (680, 440), (340, 240))
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)
        self.query = QLineEdit(self)
        self.query.setMaxLength(512)
        self.query.setPlaceholderText("Find a word or sentence in this sketch")
        self.query.setClearButtonEnabled(True)
        self.query.setAccessibleName("Search project")
        root.addWidget(self.query)
        options = QHBoxLayout()
        self.match_case = QCheckBox("Match case", self)
        self.whole_word = QCheckBox("Whole word", self)
        options.addWidget(self.match_case)
        options.addWidget(self.whole_word)
        options.addStretch()
        self.search_button = QPushButton("Search", self)
        self.search_button.clicked.connect(self._search_now)
        options.addWidget(self.search_button)
        root.addLayout(options)
        self.results = QTreeWidget(self)
        self.results.setHeaderLabels(["File", "Line", "Match"])
        self.results.setRootIsDecorated(False)
        self.results.setUniformRowHeights(True)
        self.results.setAlternatingRowColors(True)
        # Size the line column from visible rows, rather than repeatedly
        # measuring hundreds of off-screen matches during result delivery.
        self.results.header().setResizeContentsPrecision(0)
        self.results.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.results.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.results.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.results.setColumnWidth(0, 155)
        root.addWidget(self.results, 1)
        self.status = QLabel("Searches root sketch files, including unsaved changes.", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        close_row = QHBoxLayout()
        self.previous_button = QPushButton("Previous", self)
        self.previous_button.setToolTip("Previous match (Shift+F3)")
        self.previous_button.clicked.connect(lambda: self._step_result(-1))
        close_row.addWidget(self.previous_button)
        self.position = QLabel("0 of 0", self)
        self.position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position.setAccessibleName("Current search result")
        close_row.addWidget(self.position)
        self.next_button = QPushButton("Next", self)
        self.next_button.setToolTip("Next match (F3 or Enter in the search box)")
        self.next_button.clicked.connect(lambda: self._step_result(1))
        close_row.addWidget(self.next_button)
        close_row.addStretch()
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.close)
        close_row.addWidget(self.close_button)
        root.addLayout(close_row)
        for button in (self.search_button, self.previous_button, self.next_button, self.close_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self._start_search)
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(int((MAX_SECONDS + 2.0) * 1000))
        self._watchdog.timeout.connect(self._search_waiting)
        self._finished.connect(self._finish_search, Qt.ConnectionType.QueuedConnection)
        self.query.textChanged.connect(self._queue_search)
        self.match_case.toggled.connect(self._queue_search)
        self.whole_word.toggled.connect(self._queue_search)
        self.query.returnPressed.connect(self._query_return)
        self.results.itemActivated.connect(self._activate_result)
        self.results.currentItemChanged.connect(self._update_navigation)
        self._next_shortcut = QShortcut(QKeySequence("F3"), self)
        self._next_shortcut.activated.connect(lambda: self._step_result(1))
        self._previous_shortcut = QShortcut(QKeySequence("Shift+F3"), self)
        self._previous_shortcut.activated.connect(lambda: self._step_result(-1))
        self._update_navigation()
        self._screen_watcher = ScreenWatcher(self)
        self._apply_theme()
        from main.qt.signals import signals
        signals.theme_changed.connect(self._apply_theme)

    @Slot()
    def _queue_search(self):
        self._generation += 1
        if self._cancel:
            self._cancel.set()
        self.results.clear()
        self._completed_generation = -1
        self._update_navigation()
        self._pending = False
        if self.query.text():
            self.status.setText("Searching…")
            self._timer.start()
        else:
            self._timer.stop()
            self.status.setText("Searches root sketch files, including unsaved changes.")

    @Slot()
    def _search_now(self):
        self._queue_search()
        self._timer.stop()
        self._start_search()

    @Slot()
    def _query_return(self):
        if self.results.topLevelItemCount() and self._completed_generation == self._generation:
            self._step_result(1)
        else:
            self._search_now()

    @Slot()
    def _start_search(self):
        if self._closed or not self.query.text():
            return
        if self._running:
            self._pending = True
            if self._waiting_for_storage:
                self.status.setText("Waiting for the previous search to stop. Project storage is not responding.")
            elif not self._watchdog.isActive():
                self._watchdog.start()
            return
        self._pending = False
        self._running = True
        self._waiting_for_storage = False
        generation = self._generation
        query, match_case, whole_word = self.query.text(), self.match_case.isChecked(), self.whole_word.isChecked()
        try:
            buffers = dict(self._buffers_provider())
        except Exception as exc:
            self._finish_search({"generation": generation, "error": str(exc), "matches": []})
            return
        cancel = self._cancel = threading.Event()
        project = self.project
        self._watchdog.start()

        def run():
            try:
                result = search_project(project, query, case_sensitive=match_case,
                                        whole_word=whole_word, buffers=buffers, cancel=cancel)
            except Exception as exc:
                result = {"error": str(exc), "matches": []}
            result["generation"] = generation
            try:
                self._finished.emit(result)
            except RuntimeError:
                pass  # An owned window can close before a slow read finishes.

        try:
            threading.Thread(target=run, name="MCU_ProjectSearch", daemon=True).start()
        except Exception as exc:
            self._finish_search({"generation": generation, "error": str(exc), "matches": []})

    @Slot(dict)
    def _finish_search(self, result):
        self._running = False
        self._watchdog.stop()
        if not self._closed and result["generation"] == self._generation:
            self._completed_generation = self._generation
            self.results.setUpdatesEnabled(False)
            try:
                self.results.clear()
                for match in result.get("matches", []):
                    item = QTreeWidgetItem([
                        match["name"] + (" •" if match["unsaved"] else ""),
                        f'{match["line"]}:{match["column"]}', match["preview"],
                    ])
                    item.setData(0, Qt.ItemDataRole.UserRole, match)
                    item.setToolTip(0, match["path"] + ("\nUnsaved changes" if match["unsaved"] else ""))
                    item.setToolTip(2, match["preview"])
                    self.results.addTopLevelItem(item)
            finally:
                self.results.setUpdatesEnabled(True)
            if result.get("error"):
                self.status.setText("Search failed: " + result["error"] + ". Press Search to retry.")
            elif result.get("cancelled"):
                self.status.setText("Search interrupted while reading project storage. Press Search to retry.")
            else:
                count = len(result.get("matches", []))
                files = result.get("files", 0)
                message = f'{count} match{"es" if count != 1 else ""}. {files} file{"s" if files != 1 else ""} searched.'
                if result.get("limited"):
                    message += " Search limit reached; narrow the search."
                if result.get("skipped"):
                    message += f' {result["skipped"]} large or unreadable files skipped.'
                self.status.setText(message)
            if self.results.topLevelItemCount():
                self.results.setCurrentItem(self.results.topLevelItem(0))
            self._update_navigation()
        if self._pending and not self._closed:
            self._start_search()

    @Slot()
    def _search_waiting(self):
        if self._running and not self._closed:
            self._waiting_for_storage = True
            if self._cancel:
                self._cancel.set()
            self.status.setText("Project storage is not responding. Search will stop when the current read returns.")

    @Slot()
    def _update_navigation(self):
        count = self.results.topLevelItemCount()
        index = self.results.indexOfTopLevelItem(self.results.currentItem())
        ready = count > 0 and self._completed_generation == self._generation and not self._running
        self.previous_button.setEnabled(ready)
        self.next_button.setEnabled(ready)
        self.position.setText(f"{index + 1 if index >= 0 else 0} of {count}")

    def _step_result(self, direction: int):
        count = self.results.topLevelItemCount()
        if not count or self._running or self._completed_generation != self._generation:
            return
        current = self.results.indexOfTopLevelItem(self.results.currentItem())
        index = (current + direction) % count if current >= 0 else (0 if direction > 0 else count - 1)
        item = self.results.topLevelItem(index)
        self.results.setCurrentItem(item)
        self.results.scrollToItem(item)
        self._activate_result(item)

    @Slot(QTreeWidgetItem, int)
    def _activate_result(self, item, _column=0):
        match = item.data(0, Qt.ItemDataRole.UserRole)
        if match:
            focused = self.focusWidget()
            self._navigate(match["path"], match["line"], match["column"], match["end_column"])
            # Keep the modeless result list and its keyboard navigation usable.
            if self.isVisible():
                self.raise_()
                self.activateWindow()
                if focused:
                    focused.setFocus(Qt.FocusReason.OtherFocusReason)

    def open_search(self, selection: str = ""):
        self._closed = False
        if selection and "\n" not in selection and len(selection) <= 512:
            self.query.setText(selection)
        if self.query.text():
            self._queue_search()
        self.show()
        self.raise_()
        self.activateWindow()
        self.query.setFocus()
        self.query.selectAll()

    def closeEvent(self, event):
        self._closed = True
        self._generation += 1
        self._completed_generation = -1
        self._update_navigation()
        self._pending = False
        self._timer.stop()
        self._watchdog.stop()
        if self._cancel:
            self._cancel.set()
        super().closeEvent(event)

    def reject(self):
        self.close()

    @Slot(str)
    def _apply_theme(self, mode: str | None = None):
        if not mode:
            from main.core.config import get_theme_mode
            mode = get_theme_mode()
        from main.qt.theme import build_stylesheet, get_palette
        pal = get_palette(mode)
        self.setStyleSheet(build_stylesheet(mode))
        self.status.setStyleSheet(f'color: {pal["TEXT_DIM"]};')
