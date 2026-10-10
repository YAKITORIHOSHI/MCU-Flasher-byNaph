#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.project_dialog — Project selector / creator dialog for MCU Flasher.

Replaces the Tkinter ProjectSelectorDialog.
Shows recent projects, allows browsing for existing folders with live file
content preview, and can scaffold a new sketch folder with templates. The
existing-project preview lists root files and identifies the MAIN .ino file.
"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from main.web_bridge import MCUWebBackendAPI

from PySide6.QtCore import Qt, QTimer, QStandardPaths, QObject, QRunnable, QThreadPool, Signal, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QTabBar,
    QWidget, QListWidget, QListWidgetItem, QPushButton, QLabel,
    QLineEdit, QFileDialog, QComboBox, QCheckBox, QGroupBox, QFormLayout,
    QFrame, QApplication, QMessageBox, QScrollArea, QSizePolicy, QLayout,
    QAbstractItemView, QStyledItemDelegate,
)
from main.qt.icons import ActionButton as QPushButton, icon
from main.qt.setup_components import GlassCard
from main.qt.glass import WorkspaceTabBar
from src.modules.ui_palette import readable_foreground, setup_text_palette

from main.core.constants import is_application_codebase_dir

# Supported sketch file extensions
SUPPORTED_EXTS = {".ino", ".cpp", ".c", ".h", ".hpp", ".txt"}
PROJECT_ENTRY_SOURCE_EXTS = {".ino", ".cpp", ".c"}
PROJECT_SOURCE_CONTENT_PROBE_BYTES = 8192
PROJECT_PREVIEW_MAX_FILES = 256
PROJECT_PREVIEW_MAX_INO_ANALYSIS = 32
PROJECT_PREVIEW_INO_SCAN_BYTES = 2 * 1024 * 1024

_SETUP_DEFINITION_RE = re.compile(
    r'\bvoid\s+setup\s*\(\s*(?:void)?\s*\)\s*\{', re.MULTILINE
)
_LOOP_DEFINITION_RE = re.compile(
    r'\bvoid\s+loop\s*\(\s*(?:void)?\s*\)\s*\{', re.MULTILINE
)


class _ProjectPreviewSignals(QObject):
    finished = Signal(int, object)


def _scan_existing_project_preview(raw_path: str) -> dict:
    """Read a selected project's shallow root contents away from the GUI thread."""
    result = {
        "folder": "", "error": "", "files": [], "files_omitted": 0,
        "valid_sources": [], "entrypoints": {}, "main_file": "",
        "analysis_limited": False,
    }
    try:
        path = Path(raw_path)
        if path.is_dir():
            folder = path
        elif path.is_file():
            folder = path.parent
        else:
            result["error"] = "This folder does not exist. Choose an existing sketch folder."
            return result
        if is_application_codebase_dir(folder):
            result["error"] = "The MCU Flasher application folder cannot be opened as a sketch project."
            return result

        entries = sorted(
            (item for item in folder.iterdir()
             if item.is_file() and item.suffix.lower() in SUPPORTED_EXTS),
            key=lambda item: item.name.casefold(),
        )
        result["folder"] = str(folder)
        result["files_omitted"] = max(0, len(entries) - PROJECT_PREVIEW_MAX_FILES)
        entries = entries[:PROJECT_PREVIEW_MAX_FILES]
        result["files"] = [item.name for item in entries]

        owners: dict[str, dict[str, int]] = {}
        setup_owners: set[str] = set()
        loop_owners: set[str] = set()
        ino_count = 0
        for item in entries:
            ext = item.suffix.lower()
            if ext not in PROJECT_ENTRY_SOURCE_EXTS:
                continue
            try:
                read_limit = (PROJECT_PREVIEW_INO_SCAN_BYTES if ext == ".ino"
                              else PROJECT_SOURCE_CONTENT_PROBE_BYTES)
                with item.open("rb") as handle:
                    content = handle.read(read_limit)
                if content[:PROJECT_SOURCE_CONTENT_PROBE_BYTES].strip():
                    result["valid_sources"].append(item.name)
                if ext != ".ino":
                    continue

                ino_count += 1
                if ino_count > PROJECT_PREVIEW_MAX_INO_ANALYSIS:
                    result["analysis_limited"] = True
                    continue
                if item.stat().st_size > read_limit:
                    result["analysis_limited"] = True
                text = content.decode("utf-8", errors="replace")
                setup_count = len(_SETUP_DEFINITION_RE.findall(text))
                loop_count = len(_LOOP_DEFINITION_RE.findall(text))
                if setup_count or loop_count:
                    owners[item.name] = {"setup": setup_count, "loop": loop_count}
                    if setup_count:
                        setup_owners.add(item.name)
                    if loop_count:
                        loop_owners.add(item.name)
            except OSError:
                continue

        result["entrypoints"] = owners
        if (len(setup_owners) == 1 and len(loop_owners) == 1
                and setup_owners == loop_owners):
            result["main_file"] = next(iter(setup_owners))
        else:
            ino_files = [item.name for item in entries if item.suffix.lower() == ".ino"]
            if len(ino_files) == 1:
                result["main_file"] = ino_files[0]
    except OSError as exc:
        result["error"] = f"Could not read this project's files: {exc}"
    except Exception as exc:
        result["error"] = f"Could not preview this project: {exc}"
    return result


class _ProjectPreviewWorker(QRunnable):
    def __init__(self, revision: int, path: str):
        super().__init__()
        self.revision = revision
        self.path = path
        self.signals = _ProjectPreviewSignals()
        self.setAutoDelete(False)

    def run(self):
        self.signals.finished.emit(
            self.revision, _scan_existing_project_preview(self.path)
        )


class _ProjectFileRowDelegate(QStyledItemDelegate):
    """Keep file rows compact so adjacent marker segments meet cleanly."""

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setHeight(max(28, option.fontMetrics.height() + 4))
        return hint


class _ProjectTabBar(WorkspaceTabBar):
    """Project navigation uses the dialog's palette and native key handling."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._focus_ink = "#80d9ce"
        self.setAccessibleName("Project selection pages")

    def paintEvent(self, event):
        # Skip WorkspaceTabBar's global-palette ring: startup has no workspace
        # to apply Theme.*, and this picker also supports independent fixtures.
        QTabBar.paintEvent(self, event)
        if self.hasFocus() and self.currentIndex() >= 0:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(self._focus_ink), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.tabRect(self.currentIndex()).adjusted(4, 4, -5, -5), 5, 5)


class _ProjectPrompt(QMessageBox):
    """Native message-box semantics over the same cached static glass."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._glass = GlassCard(self, radius=0, accent=True)
        self._glass.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._glass.lower()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._glass.setGeometry(self.rect())
        self._glass.lower()


def _get_project_files_fast(folder_path: Path | str) -> list[str]:
    """Scan folder for sketch source files and return sorted names."""
    try:
        p = Path(folder_path)
        if not p.is_dir() or is_application_codebase_dir(p):
            return []
        found: list[tuple[int, str]] = []
        for item in p.iterdir():
            if item.is_file():
                ext = item.suffix.lower()
                if ext in SUPPORTED_EXTS:
                    prio = 0 if ext == ".ino" else (1 if ext in (".h", ".hpp") else (2 if ext == ".cpp" else 3))
                    found.append((prio, item.name))
        found.sort(key=lambda x: (x[0], x[1].lower()))
        return [name for _, name in found]
    except Exception:
        return []


class ProjectDialog(QDialog):
    """
    Open / Create Project dialog for MCU Flasher by Naph.

    Tabs:
      1. Existing Project — browse for folder with root-file and entry-point preview
      2. New Project      — scaffold a new sketch with template
      3. Recent Projects  — list of recently opened projects
      4. Open Projects    — focus an existing sketch workspace
    """

    def __init__(
        self,
        backend: Optional["MCUWebBackendAPI"] = None,
        initial_dir: str = "",
        parent: QWidget | None = None,
        open_in_new_window: bool = False,
    ):
        super().__init__(parent)
        self._backend = backend
        # True means the dialog was opened from an existing workspace and
        # should ask where each selected project belongs. Startup pickers have
        # no active workspace to switch, so they keep the direct current path.
        self._allow_window_choice = bool(open_in_new_window)
        self.selected_project: Optional[Path] = None
        self._existing_preview_revision = 0
        self._existing_preview_applied_revision = 0
        self._existing_preview_running = False
        self._existing_preview_pending: tuple[int, str] | None = None
        self._existing_preview_worker: _ProjectPreviewWorker | None = None
        self._existing_preview_pool = QThreadPool(self)
        self._existing_preview_pool.setMaxThreadCount(1)
        self._existing_preview_timer = QTimer(self)
        self._existing_preview_timer.setSingleShot(True)
        self._existing_preview_timer.setInterval(140)
        self._existing_preview_timer.timeout.connect(self._start_existing_preview_scan)
        self._foreground_timers = []
        for interval in (50, 200):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(interval)
            timer.timeout.connect(self._restore_foreground_focus)
            self._foreground_timers.append(timer)

        self.setWindowTitle("MCU Flasher by Naph — Select Project")

        from main.qt.responsive import fit_dialog, ScreenWatcher
        fit_dialog(self, (720, 560), (360, 280))
        self.setModal(True)

        self._setup_ui()
        self._screen_watcher = ScreenWatcher(self)
        self._load_recents()

        # The existing-project path intentionally starts empty. The Browse
        # action opens Documents without preselecting a project for the user.

    def _is_busy(self) -> bool:
        if self._backend and (self._backend.is_busy
                              or getattr(self._backend, "active_operation", None) is not None
                              or getattr(self._backend, "_current_op_phase", None) is not None):
            return True
        p = self.parent()
        if p and getattr(p, "_active_operation", None) is not None:
            return True
        return False

    def exec(self) -> int:
        if self._is_busy():
            QMessageBox.warning(
                self.parent() if isinstance(self.parent(), QWidget) else None,
                "Action in Progress",
                "Changing project is not allowed while an action is in progress.\n\n"
                "Please wait for the current action to finish or stop it first.",
            )
            return QDialog.DialogCode.Rejected
        return super().exec()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Ensure centered on active screen work area on initial show
        if not getattr(self, "_centered", False):
            self._centered = True
            from main.qt.responsive import active_screen
            screen = active_screen(self)
            if screen:
                avail = screen.availableGeometry()
                x = avail.x() + max(0, (avail.width() - self.width()) // 2)
                y = avail.y() + max(0, (avail.height() - self.height()) // 2)
                self.move(x, y)

        self.raise_()
        self.activateWindow()
        if hasattr(self, "_open_btn") and self._open_btn.isEnabled():
            self._open_btn.setFocus()
        elif hasattr(self, "_open_path_edit"):
            self._open_path_edit.setFocus()
        if sys.platform == "win32":
            self._restore_foreground_focus()
            for timer in self._foreground_timers:
                timer.start()

    def _restore_foreground_focus(self) -> None:
        """Never reactivate a dismissed picker or steal its child prompt's focus."""
        if sys.platform != "win32" or not self.isVisible():
            return
        modal = QApplication.activeModalWidget()
        if modal is not None and modal is not self:
            return
        try:
            from main.core.config import focus_project_window
            hwnd = int(self.winId())
            if hwnd:
                focus_project_window(hwnd)
        except (OSError, RuntimeError, ValueError):
            pass

    def hideEvent(self, event) -> None:
        for timer in self._foreground_timers:
            timer.stop()
        super().hideEvent(event)

    def _setup_ui(self) -> None:
        self.setObjectName("project-selector")
        self._glass_backdrop = GlassCard(self, radius=0)
        self._glass_backdrop.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._glass_backdrop.lower()
        self._glass_cards = [self._glass_backdrop]
        self._tab_scrolls = []

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self._header_card = GlassCard(self, radius=12, accent=True)
        self._glass_cards.append(self._header_card)
        header = QHBoxLayout(self._header_card)
        header.setContentsMargins(14, 12, 14, 12)
        header.setSpacing(12)
        self._brand_mark = QLabel(self._header_card)
        self._brand_mark.setFixedSize(42, 42)
        self._brand_mark.setAccessibleName("MCU Flasher circuit-chip mark")
        header.addWidget(self._brand_mark, 0, Qt.AlignmentFlag.AlignVCenter)
        heading = QVBoxLayout()
        heading.setSpacing(3)
        self._title_lbl = QLabel("MCU Flasher by Naph", self._header_card)
        self._title_lbl.setObjectName("project-heading")
        self._sub_lbl = QLabel(
            "Open a sketch here or in a separate workspace."
            if self._allow_window_choice else "Open a sketch project, or start a new one.",
            self._header_card,
        )
        self._sub_lbl.setObjectName("project-subheading")
        self._sub_lbl.setWordWrap(True)
        heading.addWidget(self._title_lbl)
        heading.addWidget(self._sub_lbl)
        header.addLayout(heading, 1)
        root.addWidget(self._header_card)

        self._body_card = GlassCard(self, radius=12)
        self._glass_cards.append(self._body_card)
        body = QVBoxLayout(self._body_card)
        body.setContentsMargins(8, 8, 8, 8)
        self._tabs = QTabWidget(self._body_card)
        self._tabs.setObjectName("project-tabs")
        self._tabs.setTabBar(_ProjectTabBar(self._tabs))
        self._tabs.setDocumentMode(True)
        body.addWidget(self._tabs)
        root.addWidget(self._body_card, 1)

        self._setup_existing_tab()
        self._setup_new_tab()
        self._setup_recents_tab()
        self._setup_open_projects_tab()
        self._apply_dialog_theme()
        try:
            from main.qt.signals import signals
            signals.theme_changed.connect(self._apply_dialog_theme)
        except Exception:
            pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "_glass_backdrop"):
            return
        self._glass_backdrop.setGeometry(self.rect())
        self._glass_backdrop.lower()
        compact = self.width() < 560 or self.height() < 430
        self.layout().setContentsMargins(10 if compact else 14, 8 if compact else 12,
                                        10 if compact else 14, 8 if compact else 12)
        self._header_card.layout().setContentsMargins(12, 8 if compact else 12,
                                                     12, 8 if compact else 12)
        self._new_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows
                                       if self.width() < 560 else QFormLayout.RowWrapPolicy.DontWrapRows)
        self._btn_clear_recents.setText("Clear" if self.width() < 500 else "Clear history")
        self._btn_clear_recents.setAccessibleName("Clear recent project history")

    def _make_tab(self, title: str):
        """Keep actions visible while short-screen page content can scroll."""
        tab = QWidget(self._tabs)
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(6, 12, 6, 6)
        outer.setSpacing(10)
        scroll = QScrollArea(tab)
        scroll.setObjectName("project-page-scroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        content.setObjectName("project-page-content")
        content.setMinimumWidth(0)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(10)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        scroll.setWidget(content)
        self._tab_scrolls.append(scroll)
        outer.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        outer.addLayout(actions)
        self._tabs.addTab(tab, title)
        return tab, layout, actions

    def _reading_label(self, text: str = "", parent=None) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("project-reading")
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def _action(self, text: str, role: str, callback, parent=None) -> QPushButton:
        button = QPushButton(text, parent)
        button.setProperty("projectAction", role)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def _set_label_tone(self, label: QLabel, tone: str) -> None:
        label.setProperty("projectTone", tone)
        colors = getattr(self, "_dialog_palette", {})
        key = {"muted": "TEXT_DIM", "error": "RED", "ok": "GREEN", "warn": "YELLOW"}.get(tone, "TEXT")
        label.setStyleSheet(f"color: {colors.get(key, '#e3edf6')};")

    def _set_existing_preview_expanded(self, expanded: bool) -> None:
        vertical_policy = (
            QSizePolicy.Policy.Expanding if expanded else QSizePolicy.Policy.Maximum
        )
        self._preview_box.setSizePolicy(QSizePolicy.Policy.Preferred, vertical_policy)
        self._existing_content_layout.setStretchFactor(
            self._preview_box, 1 if expanded else 0)
        self._existing_content_layout.setStretch(
            self._existing_preview_bottom_stretch_index, 0 if expanded else 1)

    def _project_file_marker_icon(
        self, kind: str, connect_above: bool, connect_below: bool,
    ) -> QIcon:
        """Draw a themed tree connector and marker without changing file labels."""
        palette = getattr(self, "_dialog_palette", {})
        kind = kind if kind in {"main", "entry"} else "file"
        connect_above = bool(connect_above)
        connect_below = bool(connect_below)
        key = (self._theme_mode, kind, connect_above, connect_below)
        cache = getattr(self, "_project_file_marker_icons", None)
        if cache is None:
            cache = self._project_file_marker_icons = {}
        if key in cache:
            return cache[key]

        tone = "GREEN" if kind == "main" else "CYAN" if kind == "entry" else "TEXT_DIM"
        color = QColor(palette.get(tone, "#aebdca"))
        rail = QColor(palette.get("CYAN", "#73d9d0"))
        rail.setAlpha(150)
        pixmap = QPixmap(16, 26)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(rail, 1.2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        if connect_above:
            top_edge = 6 if kind == "main" else 8 if kind == "entry" else 9
            painter.drawLine(8, 0, 8, top_edge)
        if connect_below:
            bottom_edge = 20 if kind == "main" else 18 if kind == "entry" else 17
            painter.drawLine(8, bottom_edge, 8, 25)

        painter.setPen(Qt.PenStyle.NoPen)
        if kind == "main":
            halo = QColor(color)
            halo.setAlpha(38)
            ring = QColor(color)
            ring.setAlpha(185)
            painter.setBrush(halo)
            painter.drawEllipse(1, 6, 14, 14)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ring, 1.2))
            painter.drawEllipse(1, 6, 14, 14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(5, 10, 6, 6)
        elif kind == "entry":
            painter.setPen(QPen(color, 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(3, 8, 10, 10)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(6, 11, 4, 4)
        else:
            painter.setBrush(color)
            painter.drawEllipse(5, 10, 6, 6)
        painter.end()
        cache[key] = QIcon(pixmap)
        return cache[key]

    def _style_existing_file_items(self) -> None:
        """Style file rows and mark MAIN/entry-point ownership at a glance."""
        palette = getattr(self, "_dialog_palette", {})
        if not palette or not hasattr(self, "_existing_files_list"):
            return
        item_count = self._existing_files_list.count()
        for index in range(item_count):
            item = self._existing_files_list.item(index)
            is_main = bool(item.data(Qt.ItemDataRole.UserRole + 1))
            entry_roles = item.data(Qt.ItemDataRole.UserRole + 2) or ()
            marker = "main" if is_main else "entry" if entry_roles else "file"
            item.setIcon(self._project_file_marker_icon(
                marker, index > 0, index < item_count - 1))
            if is_main:
                item.setBackground(QColor(palette["GREEN_DIM"]))
                item.setForeground(QColor(palette["GREEN"]))
            else:
                item.setBackground(QColor(0, 0, 0, 0))
                item.setForeground(QColor(palette["CYAN"] if entry_roles else palette["TEXT"]))
            font = item.font()
            font.setBold(is_main)
            item.setFont(font)

    def _apply_dialog_theme(self, theme_mode: str | None = None) -> None:
        if not theme_mode:
            from main.core.config import get_theme_mode
            theme_mode = get_theme_mode()
        from main.qt.theme import get_palette
        original = get_palette(theme_mode)
        # Reading inks are checked against all static glass/reflection bounds.
        adjusted = setup_text_palette({"T_" + key: value for key, value in original.items()}, glass=True)
        pal = dict(original)
        pal.update({key[2:]: value for key, value in adjusted.items() if key.startswith("T_")})
        self._dialog_palette = pal
        self._theme_mode = theme_mode
        for card in self._glass_cards:
            card.set_palette(original)
        self._brand_mark.setPixmap(icon("brand", pal["CYAN"], size=40).pixmap(40, 40))
        self._tabs.tabBar()._focus_ink = pal["CYAN"]
        primary_ink = readable_foreground(pal["BTN_COMPILE"])
        self.setStyleSheet(f"""
            QDialog#project-selector {{ background: {pal['BG_DARKEST']}; color: {pal['TEXT']}; }}
            QLabel {{ background: transparent; color: {pal['TEXT']}; font-size: 12px; }}
            QLabel#project-heading {{ color: {pal['TEXT_BRIGHT']}; font-size: 17px; font-weight: 700; }}
            QLabel#project-subheading {{ color: {pal['TEXT_DIM']}; font-size: 11px; }}
            QTabWidget#project-tabs {{ background: transparent; }}
            QTabWidget#project-tabs::pane {{ border: 0; background: transparent; }}
            QTabBar {{ background: transparent; border: 0; }}
            QTabBar::tab {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {pal['BG_MID']},stop:1 {pal['BG_DARK']});
                color: {pal['TEXT_DIM']}; border: 1px solid {pal['BORDER']}; border-radius: 7px;
                padding: 8px 10px; margin-right: 4px; font-size: 11px; font-weight: 600;
            }}
            QTabBar::tab:selected {{ background: {pal['BG_HOVER']}; color: {pal['TEXT_BRIGHT']}; border-color: {pal['BORDER_LIT']}; }}
            QTabBar::tab:hover:!selected {{ background: {pal['BG_MID']}; color: {pal['TEXT_BRIGHT']}; }}
            QTabBar QToolButton {{ background: {pal['BG_MID']}; color: {pal['TEXT']}; border: 1px solid {pal['BORDER']}; }}
            QScrollArea#project-page-scroll, QWidget#project-page-content {{ background: transparent; }}
            QLineEdit, QComboBox {{
                background: {pal['BG_DARKEST']}; color: {pal['TEXT_BRIGHT']};
                border: 1px solid {pal['BORDER']}; border-radius: 7px; padding: 7px 9px; font-size: 12px;
                selection-background-color: {pal['BG_HOVER']}; selection-color: {pal['TEXT_BRIGHT']};
            }}
            QLineEdit:focus, QComboBox:focus {{ border-color: {pal['CYAN']}; }}
            QComboBox QAbstractItemView {{ background: {pal['BG_DARKEST']}; color: {pal['TEXT']};
                selection-background-color: {pal['BG_HOVER']}; selection-color: {pal['TEXT_BRIGHT']}; }}
            QGroupBox#project-form {{ background: transparent; border: 0; margin: 0; padding: 0; }}
            QLabel#project-section-title {{ color: {pal['TEXT_BRIGHT']}; font-weight: 600; font-size: 12px; }}
            QLabel#project-reading {{ background: {pal['BG_DARKEST']}; color: {pal['TEXT']};
                border: 1px solid {pal['BORDER']}; border-radius: 8px; padding: 9px 10px; font-size: 11px; }}
            QListWidget {{ background: {pal['BG_DARKEST']}; color: {pal['TEXT']}; border: 1px solid {pal['BORDER']};
                border-radius: 8px; padding: 4px; font-size: 12px; outline: none; }}
            QListWidget::item {{ padding: 8px 9px; border: 1px solid transparent; border-radius: 5px; }}
            QListWidget::item:hover {{ background: {pal['BG_MID']}; }}
            QListWidget::item:selected {{ background: {pal['BG_HOVER']}; color: {pal['TEXT_BRIGHT']}; border-color: {pal['BORDER_LIT']}; }}
            QListWidget:focus {{ border-color: {pal['CYAN']}; }}
            QListWidget#existing-project-files::item {{ padding: 0px 7px; border-width: 0px; }}
            QCheckBox {{ color: {pal['TEXT']}; font-size: 12px; background: transparent; }}
            QPushButton {{ min-height: 18px; padding: 7px 12px; border: 1px solid {pal['BORDER']};
                border-radius: 7px; color: {pal['TEXT_BRIGHT']}; font-size: 12px; font-weight: 600;
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {pal['BG_MID']},stop:1 {pal['BG_DARK']}); }}
            QPushButton:hover {{ background: {pal['BG_HOVER']}; border-color: {pal['BORDER_LIT']}; }}
            QPushButton:pressed {{ background: {pal['BG_DARKEST']}; }}
            QPushButton:focus {{ border: 2px solid {pal['CYAN']}; padding: 6px 11px; }}
            QPushButton[projectAction="primary"]:enabled {{ color: {primary_ink}; border-color: {pal['BORDER_LIT']};
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {pal['BTN_COMPILE_H']},stop:1 {pal['BTN_COMPILE']}); }}
            QPushButton[projectAction="primary"]:enabled:hover {{ background: {pal['BTN_COMPILE_H']}; }}
            QPushButton:disabled {{ background: {pal['BG_DARK']}; color: {pal['TEXT_DIM']}; border-color: {pal['BORDER']}; }}
        """)
        for label in self.findChildren(QLabel):
            tone = label.property("projectTone")
            if tone:
                self._set_label_tone(label, tone)
        self._set_label_tone(self._existing_preview_lbl,
                             self._existing_preview_lbl.property("projectTone") or "muted")
        for widget in self.findChildren(QPushButton):
            if getattr(widget, "_icon_name", ""):
                ink = primary_ink if widget.property("projectAction") == "primary" else pal["TEXT_BRIGHT"]
                widget.setIcon(icon(widget._icon_name, ink))
        self._style_existing_file_items()
        self._tabs.tabBar().update()
        for field in (self._open_path_edit, self._new_name_edit, self._new_parent_edit, self._template_combo):
            # Native styles may otherwise compress a line edit to its frame
            # while the short page shrinks. Scroll the form instead of text.
            field.setMinimumHeight(max(32, field.fontMetrics().height() + 18))

    def _setup_existing_tab(self) -> None:
        tab, layout, actions = self._make_tab("Existing")
        self._existing_content_layout = layout
        hint = QLabel("Choose the main .ino file or enter its project folder.", tab)
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(hint)
        path_row = QHBoxLayout()
        self._open_path_edit = QLineEdit(tab)
        self._open_path_edit.setPlaceholderText("Sketch file or project folder")
        self._open_path_edit.setAccessibleName("Existing project file or folder")
        self._open_path_edit.setMinimumWidth(0)
        self._open_path_edit.textChanged.connect(self._update_existing_preview)
        path_row.addWidget(self._open_path_edit, 1)
        self._btn_browse = self._action("Browse…", "secondary", self._browse_folder, tab)
        path_row.addWidget(self._btn_browse)
        layout.addLayout(path_row)
        self._preview_box = GlassCard(tab, radius=9)
        self._preview_box.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._glass_cards.append(self._preview_box)
        preview = QVBoxLayout(self._preview_box)
        preview.setContentsMargins(10, 9, 10, 10)
        preview.setSpacing(7)
        self._p_title = QLabel("Sketch files", self._preview_box)
        self._p_title.setObjectName("project-section-title")
        preview.addWidget(self._p_title)
        self._existing_preview_lbl = self._reading_label("Choose a folder to preview its files.", self._preview_box)
        self._existing_preview_lbl.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        preview.addWidget(self._existing_preview_lbl)
        self._existing_files_list = QListWidget(self._preview_box)
        self._existing_files_list.setObjectName("existing-project-files")
        self._existing_files_list.setAccessibleName("Files in selected sketch project")
        self._existing_files_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._existing_files_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._existing_files_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._existing_files_list.setSpacing(0)
        self._existing_files_list.setIconSize(QSize(16, 26))
        self._existing_files_list.setItemDelegate(
            _ProjectFileRowDelegate(self._existing_files_list))
        self._existing_files_list.setUniformItemSizes(True)
        self._existing_files_list.setMinimumHeight(0)
        self._existing_files_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._existing_files_list.hide()
        preview.addWidget(self._existing_files_list, 1)
        layout.addWidget(self._preview_box)
        self._existing_status = QLabel("", tab)
        self._existing_status.setWordWrap(True)
        self._existing_status.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self._existing_status)
        self._existing_preview_bottom_stretch_index = layout.count()
        layout.addStretch(1)
        actions.addStretch()
        self._btn_cancel_existing = self._action("Cancel", "secondary", self.reject, tab)
        self._btn_open_existing = self._action("Open project", "primary", self._open_existing, tab)
        actions.addWidget(self._btn_cancel_existing)
        actions.addWidget(self._btn_open_existing)

    def _setup_new_tab(self) -> None:
        tab, layout, actions = self._make_tab("New")
        title = QLabel("Create a sketch project", tab)
        title.setObjectName("project-section-title")
        layout.addWidget(title)
        self._form_box = QGroupBox(tab)
        self._form_box.setObjectName("project-form")
        form = self._new_form = QFormLayout(self._form_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._new_name_edit = QLineEdit(self._form_box)
        self._new_name_edit.setPlaceholderText("MySketch")
        self._new_name_edit.setText("MySketch")
        self._new_name_edit.setMinimumWidth(0)
        self._new_name_edit.textChanged.connect(self._update_new_preview)
        form.addRow("Project name", self._new_name_edit)
        parent_row = QHBoxLayout()
        self._new_parent_edit = QLineEdit(self._form_box)
        self._new_parent_edit.setText(self._backend.get_default_project_parent()
                                      if self._backend else str(Path.home() / "Documents" / "Arduino"))
        self._new_parent_edit.setMinimumWidth(0)
        self._new_parent_edit.textChanged.connect(self._update_new_preview)
        parent_row.addWidget(self._new_parent_edit, 1)
        self._btn_browse_parent = self._action("Browse…", "secondary", self._browse_parent, self._form_box)
        parent_row.addWidget(self._btn_browse_parent)
        form.addRow("Location", parent_row)
        self._template_combo = QComboBox(self._form_box)
        self._template_combo.setMinimumWidth(0)
        self._template_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._template_combo.addItem("Empty sketch (setup and loop)", "standard")
        self._template_combo.addItem("Blink (LED_BUILTIN)", "blink")
        form.addRow("Template", self._template_combo)
        self._cb_include_h = QCheckBox("Include header file (.h)", self._form_box)
        self._cb_include_h.stateChanged.connect(self._update_new_preview)
        form.addRow(self._cb_include_h)
        self._cb_include_cpp = QCheckBox("Include implementation file (.cpp)", self._form_box)
        self._cb_include_cpp.stateChanged.connect(self._update_new_preview)
        form.addRow(self._cb_include_cpp)
        layout.addWidget(self._form_box)
        self._new_preview_lbl = self._reading_label(parent=tab)
        layout.addWidget(self._new_preview_lbl)
        self._update_new_preview()
        self._new_status = QLabel("", tab)
        self._new_status.setWordWrap(True)
        layout.addWidget(self._new_status)
        layout.addStretch()
        actions.addStretch()
        self._btn_cancel_new = self._action("Cancel", "secondary", self.reject, tab)
        self._btn_create = self._action("Create project", "primary", self._create_project, tab)
        actions.addWidget(self._btn_cancel_new)
        actions.addWidget(self._btn_create)

    def _setup_recents_tab(self) -> None:
        tab, layout, actions = self._make_tab("Recent")
        self._recent_hint_lbl = QLabel("Return to a recently opened sketch.", tab)
        self._recent_hint_lbl.setWordWrap(True)
        layout.addWidget(self._recent_hint_lbl)
        self._recent_list = QListWidget(tab)
        self._recent_list.setMinimumHeight(90)
        self._recent_list.itemClicked.connect(self._on_recent_clicked)
        self._recent_list.itemDoubleClicked.connect(self._open_selected_recent)
        layout.addWidget(self._recent_list, 1)
        self._recent_preview_lbl = self._reading_label("Choose a project to preview its files.", tab)
        layout.addWidget(self._recent_preview_lbl)
        self._btn_clear_recents = self._action("Clear history", "secondary", self._clear_recents, tab)
        actions.addWidget(self._btn_clear_recents)
        actions.addStretch()
        self._btn_cancel_recent = self._action("Cancel", "secondary", self.reject, tab)
        self._btn_open_recent = self._action("Open selected", "primary", self._open_selected_recent, tab)
        actions.addWidget(self._btn_cancel_recent)
        actions.addWidget(self._btn_open_recent)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers & Event Handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _update_existing_preview(self, text: str) -> None:
        raw = text.strip()
        self._existing_preview_revision += 1
        revision = self._existing_preview_revision
        self._existing_preview_timer.stop()
        self._existing_preview_pending = None
        self._existing_files_list.clear()
        self._existing_files_list.hide()
        self._existing_files_list.setMinimumHeight(0)
        self._existing_files_list.setMaximumHeight(16777215)
        self._set_existing_preview_expanded(False)
        if not raw:
            self._existing_preview_lbl.setText("No folder selected")
            self._set_label_tone(self._existing_preview_lbl, "muted")
            self._existing_preview_lbl.show()
            return
        self._existing_preview_lbl.setText("Reading the project's root files…")
        self._set_label_tone(self._existing_preview_lbl, "muted")
        self._existing_preview_lbl.show()
        self._existing_preview_pending = (revision, raw)
        if not self._existing_preview_running:
            self._existing_preview_timer.start()

    def _start_existing_preview_scan(self) -> None:
        if self._existing_preview_running or not self._existing_preview_pending:
            return
        revision, path = self._existing_preview_pending
        self._existing_preview_pending = None
        worker = _ProjectPreviewWorker(revision, path)
        worker.signals.finished.connect(self._on_existing_preview_scanned)
        self._existing_preview_worker = worker
        self._existing_preview_running = True
        self._existing_preview_pool.start(worker)

    def _on_existing_preview_scanned(self, revision: int, result: dict) -> None:
        self._existing_preview_running = False
        self._existing_preview_worker = None
        if revision == self._existing_preview_revision:
            self._existing_preview_applied_revision = revision
            self._show_existing_preview(result)
        if self._existing_preview_pending:
            self._existing_preview_timer.start(0)

    def _show_existing_preview(self, result: dict) -> None:
        self._existing_files_list.clear()
        self._existing_files_list.hide()
        self._existing_files_list.setMinimumHeight(0)
        self._existing_files_list.setMaximumHeight(16777215)
        self._set_existing_preview_expanded(False)
        self._existing_preview_lbl.hide()
        if result.get("error"):
            self._existing_preview_lbl.setText(result["error"])
            self._set_label_tone(self._existing_preview_lbl, "error")
            self._existing_preview_lbl.show()
            return

        files = list(result.get("files", ()))
        main_file = result.get("main_file", "")
        if main_file in files:
            files.remove(main_file)
            files.insert(0, main_file)
        entrypoints = result.get("entrypoints", {})
        for name in files:
            roles = tuple(role for role in ("setup", "loop")
                          if entrypoints.get(name, {}).get(role, 0))
            is_main = name == main_file
            if is_main and set(roles) == {"setup", "loop"}:
                badge = "MAIN · setup() + loop()"
            elif is_main and roles:
                missing = "loop()" if "loop" not in roles else "setup()"
                badge = f"MAIN · {', '.join(f'{role}()' for role in roles)} · {missing} missing"
            elif is_main:
                badge = "MAIN SKETCH · entry points not detected"
            else:
                badge = " · ".join(f"{role}()" for role in roles)
            text = f"{name}  —  {badge}" if badge else name
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, str(Path(result["folder"]) / name))
            item.setData(Qt.ItemDataRole.UserRole + 1, is_main)
            item.setData(Qt.ItemDataRole.UserRole + 2, roles)
            functions = ", ".join(f"{role}()" for role in roles)
            item.setToolTip(
                str(Path(result["folder"]) / name)
                + (f"\nDefines: {functions}" if functions else "")
            )
            self._existing_files_list.addItem(item)

        if files:
            row_height = self._existing_files_list.iconSize().height()
            if len(files) == 1:
                self._existing_files_list.setFixedHeight(row_height + 16)
            else:
                visible_rows = min(len(files), 4)
                self._existing_files_list.setMinimumHeight(
                    visible_rows * row_height + 16)
                self._set_existing_preview_expanded(True)
            self._existing_files_list.setVisible(True)
            self._existing_files_list.scrollToTop()
            self._style_existing_file_items()

        sources = list(result.get("valid_sources", ()))
        if not sources:
            self._existing_preview_lbl.setText(
                "Existing projects need a non-empty .ino, .cpp, or .c file in this folder. "
                "Use New project to create one."
            )
            self._set_label_tone(self._existing_preview_lbl, "warn")
            self._existing_preview_lbl.show()
            return

        notices = []
        ino_files = [name for name in files if Path(name).suffix.lower() == ".ino"]
        if ino_files and not entrypoints:
            notices.append(
                "No setup()/loop() definition was detected in the root .ino files."
            )
        if result.get("analysis_limited"):
            notices.append(
                "Entry-point detection was limited for oversized or numerous .ino files."
            )
        if result.get("files_omitted"):
            notices.append(f"{result['files_omitted']} additional files are not shown.")
        if notices:
            self._existing_preview_lbl.setText("\n".join(notices))
            self._set_label_tone(self._existing_preview_lbl, "warn")
            self._existing_preview_lbl.show()

    def _update_new_preview(self) -> None:
        name = re.sub(r'[^a-zA-Z0-9_-]', '_', self._new_name_edit.text().strip()) or "MySketch"
        parent = self._new_parent_edit.text().strip() or str(Path.home() / "Documents")
        target = Path(parent) / name
        files = [f"{name}.ino"]
        if self._cb_include_h.isChecked():
            files.append(f"{name}.h")
        if self._cb_include_cpp.isChecked():
            files.append(f"{name}.cpp")
        self._new_preview_lbl.setText(
            f"Target: {target}\n"
            f"Files to create: {', '.join(files)}"
        )

    def _browse_folder(self) -> None:
        # Reset every Browse click to the user's OS Documents location, not
        # the active sketch or the native dialog's last-used folder.
        start = (
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
            or str(Path.home() / "Documents")
        )
        # Selecting a source file keeps that exact file as the editor's initial
        # tab; backend validation still applies to its containing project root.
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select Existing Sketch / Project File (.ino, .cpp, .h, .txt)",
            start,
            "Project Files & Sketches (*.ino *.cpp *.h *.hpp *.txt platformio.ini);;"
            "Arduino Sketches (*.ino);;"
            "C/C++ Source & Headers (*.cpp *.h *.hpp);;"
            "Text Files (*.txt);;"
            "All Files (*.*)"
        )
        if selected:
            p = Path(selected)
            folder = p.parent if p.is_file() else p
            if is_application_codebase_dir(folder):
                self._set_label_tone(self._existing_status, "error")
                self._existing_status.setText("✖ Cannot select MCU Flasher application folder.")
                return
            self._open_path_edit.setText(str(p))

    def _browse_parent(self) -> None:
        start = self._new_parent_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Select Location", start)
        if folder:
            self._new_parent_edit.setText(folder)

    def _open_existing(self) -> None:
        if self._is_busy():
            self._set_label_tone(self._existing_status, "error")
            self._existing_status.setText("✖ Changing project is not allowed while an action is in progress.")
            QMessageBox.warning(
                self,
                "Action in Progress",
                "Changing project is not allowed while an action is in progress.\n\n"
                "Please wait for the current action to finish or stop it first.",
            )
            return
        path = self._open_path_edit.text().strip()
        if not path:
            self._set_label_tone(self._existing_status, "error")
            self._existing_status.setText("✖ Please select a folder path.")
            return
        requested_path = Path(path).expanduser()
        try:
            requested_path = requested_path.resolve()
        except (OSError, RuntimeError):
            requested_path = requested_path.absolute()
        selected_file = str(requested_path) if requested_path.is_file() else None
        p = requested_path
        if p.is_file():
            p = p.parent
        path = str(p)
        if is_application_codebase_dir(p):
            self._set_label_tone(self._existing_status, "error")
            self._existing_status.setText("✖ The MCU Flasher application folder cannot be opened as a project.")
            return
        if not p.is_dir():
            self._set_label_tone(self._existing_status, "error")
            self._existing_status.setText("✖ The specified folder does not exist.")
            return
        validation = self._validate_existing_project_folder(p)
        if not validation.get("success"):
            self._set_label_tone(self._existing_status, "error")
            self._existing_status.setText(f"✖ {validation.get('error', 'Could not open project.')}")
            return

        self._dispatch_project_action(
            path,
            p.name,
            lambda in_new_window: self._open_project(
                path, in_new_window, active_file=selected_file,
            ),
            lambda: self._accept_selected_project(p),
            self._existing_status,
        )

    def _create_project(self) -> None:
        if self._is_busy():
            self._set_label_tone(self._new_status, "error")
            self._new_status.setText("✖ Creating or changing project is not allowed while an action is in progress.")
            QMessageBox.warning(
                self,
                "Action in Progress",
                "Creating or changing project is not allowed while an action is in progress.\n\n"
                "Please wait for the current action to finish or stop it first.",
            )
            return
        name = self._new_name_edit.text().strip()
        parent = self._new_parent_edit.text().strip()
        if not name or not parent:
            self._set_label_tone(self._new_status, "error")
            self._new_status.setText("✖ Project name and location are required.")
            return
        if is_application_codebase_dir(parent):
            self._set_label_tone(self._new_status, "error")
            self._new_status.setText("✖ Cannot create sketch inside MCU Flasher application folder.")
            return

        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
        target = Path(parent) / clean_name
        tmpl = self._template_combo.currentData() or "standard"
        self._dispatch_project_action(
            str(target),
            clean_name,
            lambda in_new_window: self._backend.create_project(
                parent_dir=parent,
                name=name,
                include_h=self._cb_include_h.isChecked(),
                include_cpp=self._cb_include_cpp.isChecked(),
                template_type=tmpl,
                open_in_new_window=in_new_window,
            ) if self._backend else {"success": True},
            lambda: self._accept_selected_project(target),
            self._new_status,
        )

    def _load_recents(self) -> None:
        self._recent_list.clear()
        if not self._backend:
            self._btn_open_recent.setEnabled(False)
            self._btn_open_recent.setCursor(Qt.CursorShape.ArrowCursor)
            self._btn_clear_recents.setEnabled(False)
            self._btn_clear_recents.setCursor(Qt.CursorShape.ArrowCursor)
            return
        try:
            recents = self._backend.get_recent_projects()
            for r in recents:
                p = Path(r)
                if p.is_dir() and not is_application_codebase_dir(p):
                    item = QListWidgetItem(f"{p.name}\n{r}")
                    item.setToolTip(r)
                    item.setData(Qt.ItemDataRole.UserRole, r)
                    self._recent_list.addItem(item)
            has_recents = self._recent_list.count() > 0
            self._btn_open_recent.setEnabled(has_recents)
            self._btn_open_recent.setCursor(Qt.CursorShape.PointingHandCursor if has_recents else Qt.CursorShape.ArrowCursor)
            self._btn_clear_recents.setEnabled(has_recents)
            self._btn_clear_recents.setCursor(Qt.CursorShape.PointingHandCursor if has_recents else Qt.CursorShape.ArrowCursor)
            if has_recents:
                self._recent_list.setCurrentRow(0)
                self._on_recent_clicked(self._recent_list.item(0))
            else:
                self._recent_preview_lbl.setText("No recent projects found.")
        except Exception:
            pass

    def _on_recent_clicked(self, item: QListWidgetItem | None) -> None:
        if not item:
            self._btn_open_recent.setEnabled(False)
            self._btn_open_recent.setCursor(Qt.CursorShape.ArrowCursor)
            return
        self._btn_open_recent.setEnabled(True)
        self._btn_open_recent.setCursor(Qt.CursorShape.PointingHandCursor)
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        if is_application_codebase_dir(path):
            self._recent_preview_lbl.setText(f"Path: {path}\n⛔ Application codebase (invalid project)")
            return
        files = _get_project_files_fast(path)
        if files:
            summary = ", ".join(files[:6])
            if len(files) > 6:
                summary += f" (+{len(files) - 6} more)"
            self._recent_preview_lbl.setText(f"Path: {path}\nFiles ({len(files)}): {summary}")
        else:
            self._recent_preview_lbl.setText(f"Path: {path}\n(No source files found)")

    def _open_selected_recent(self, item: QListWidgetItem | None = None) -> None:
        if self._is_busy():
            self._recent_preview_lbl.setText("✖ Changing project is not allowed while an action is in progress.")
            QMessageBox.warning(
                self,
                "Action in Progress",
                "Changing project is not allowed while an action is in progress.\n\n"
                "Please wait for the current action to finish or stop it first.",
            )
            return
        if item is None:
            item = self._recent_list.currentItem()
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        p = Path(path)
        if not p.is_dir():
            self._recent_preview_lbl.setText(f"⚠️ Folder no longer exists: {path}")
            return
        if is_application_codebase_dir(p):
            self._recent_preview_lbl.setText("✖ The MCU Flasher application folder cannot be opened as a project.")
            return
        validation = self._validate_existing_project_folder(p)
        if not validation.get("success"):
            self._recent_preview_lbl.setText(f"✖ {validation.get('error', 'Could not open project.')}")
            return

        self._dispatch_project_action(
            path,
            p.name,
            lambda in_new_window: self._open_project(path, in_new_window),
            lambda: self._accept_selected_project(p),
            self._recent_preview_lbl,
        )

    def _clear_recents(self) -> None:
        if self._backend:
            self._backend.clear_recent_projects()
        self._recent_list.clear()
        self._recent_preview_lbl.setText("Recent projects history cleared.")
        self._btn_open_recent.setEnabled(False)
        self._btn_open_recent.setCursor(Qt.CursorShape.ArrowCursor)
        self._btn_clear_recents.setEnabled(False)
        self._btn_clear_recents.setCursor(Qt.CursorShape.ArrowCursor)

    def _choose_project_window(self, project_name: str) -> bool | None:
        """Ask whether this project belongs in this workspace or another."""
        if self._is_busy():
            return None
        if not self._allow_window_choice:
            return False

        prompt = _ProjectPrompt(self)
        self._style_project_prompt(prompt)
        prompt.setIconPixmap(icon("project", self._dialog_palette["CYAN"], size=36).pixmap(36, 36))
        prompt.setWindowTitle("Choose Project Window")
        prompt.setText(f"Where would you like to open ‘{project_name}’?")
        prompt.setInformativeText(
            "Current window switches this workspace to the selected project. "
            "New window keeps this project, editor and hardware selection open here."
        )
        current = prompt.addButton("This window", QMessageBox.ButtonRole.AcceptRole)
        current.setAccessibleName("Open in current window")
        new_window = prompt.addButton("New window", QMessageBox.ButtonRole.ActionRole)
        new_window.setAccessibleName("Open in a new window")
        cancel = prompt.addButton(QMessageBox.StandardButton.Cancel)
        prompt.setDefaultButton(current)
        prompt.setEscapeButton(cancel)
        prompt.exec()
        clicked = prompt.clickedButton()
        if clicked is current:
            return False
        if clicked is new_window:
            return True
        return None

    def _style_project_prompt(self, prompt: QMessageBox) -> None:
        """Keep native choice prompts readable and aligned with the active glass theme."""
        try:
            from main.core.config import get_theme_mode
            from main.qt.theme import get_palette
            pal = get_palette(getattr(self, "_theme_mode", None) or get_theme_mode())
        except Exception:
            pal = {}
        if isinstance(prompt, _ProjectPrompt):
            prompt._glass.set_palette(pal)
        bg = pal.get("BG_DARK", "#151922")
        surface = pal.get("BG_MID", "#1c2333")
        hover = pal.get("BG_HOVER", "#2a3a55")
        text = pal.get("TEXT", "#e0e6ed")
        bright = pal.get("TEXT_BRIGHT", "#ffffff")
        border = pal.get("BORDER", "#2d3748")
        cyan = pal.get("CYAN", "#00d2ff")
        prompt.setStyleSheet(f"""
            QMessageBox {{ background: transparent; color: {text}; }}
            QMessageBox QLabel {{ color: {text}; background: transparent; font-size: 12px; }}
            QMessageBox QPushButton {{
                min-width: 82px; padding: 7px 12px;
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {surface},stop:1 {bg});
                color: {bright}; border: 1px solid {border}; border-radius: 6px;
            }}
            QMessageBox QPushButton:hover {{ background-color: {hover}; border-color: {cyan}; }}
            QMessageBox QPushButton:pressed {{ background-color: {bg}; }}
            QMessageBox QPushButton:focus, QMessageBox QPushButton:default {{ border: 2px solid {cyan}; padding: 6px 11px; }}
        """)

    def _prepare_current_window_switch(
        self,
        target: str,
        callback: Callable[[bool, str, bool], None],
        on_wait: Callable[[], None] | None = None,
    ) -> None:
        """Protect the current editor before replacing its active project."""
        backend = self._backend
        parent = self.parent()
        if (backend and (backend.is_busy or getattr(backend, "active_operation", None) is not None)) or \
                (parent and getattr(parent, "_active_operation", None) is not None):
            callback(False, "Changing project is not allowed while an action is in progress.", False)
            return

        try:
            current_dir = Path(backend.sketch_dir_path).resolve() if backend else None
            target_dir = Path(target).resolve()
            if current_dir == target_dir:
                callback(True, "", False)
                return
        except (OSError, TypeError, ValueError):
            pass

        dirty = bool(backend and any(getattr(backend, "modified_files", {}).values()))
        if not dirty:
            callback(True, "", False)
            return

        prompt = _ProjectPrompt(self)
        self._style_project_prompt(prompt)
        prompt.setIconPixmap(icon("warning", self._dialog_palette["YELLOW"], size=36).pixmap(36, 36))
        prompt.setWindowTitle("Unsaved Editor Changes")
        prompt.setText("The current project has unsaved editor changes.")
        prompt.setInformativeText(
            "Save them before switching, discard them and continue, or cancel this project change."
        )
        save = prompt.addButton("Save All", QMessageBox.ButtonRole.AcceptRole)
        save.setAccessibleName("Save all changes and continue")
        discard = prompt.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        discard.setAccessibleName("Discard changes and continue")
        cancel = prompt.addButton(QMessageBox.StandardButton.Cancel)
        prompt.setDefaultButton(save)
        prompt.setEscapeButton(cancel)
        prompt.exec()
        clicked = prompt.clickedButton()
        if clicked is discard:
            callback(True, "", False)
            return
        if clicked is not save:
            callback(False, "", True)
            return

        editor = getattr(parent, "_editor_panel", None)
        save_all = getattr(editor, "trigger_save_all", None)
        if not callable(save_all):
            callback(False, "The editor could not confirm that its changes were saved.", False)
            return
        if on_wait:
            on_wait()
        save_all(
            callback=lambda: callback(True, "", False),
            failure_callback=lambda: callback(False, "Save All failed or timed out; the project was left open.", False),
        )

    def _accept_selected_project(self, project: Path) -> None:
        self.selected_project = project
        self.hide()
        QApplication.processEvents()
        self.accept()

    def _dispatch_project_action(
        self,
        target: str,
        project_name: str,
        action: Callable[[bool], dict],
        on_success: Callable[[], None],
        status_label: QLabel,
    ) -> None:
        if self._is_busy():
            status_label.setText("✖ Changing project is not allowed while an action is in progress.")
            return
        in_new_window = self._choose_project_window(project_name)
        if in_new_window is None:
            return

        state = {"finished": False, "waiting": False}

        def begin_wait() -> None:
            if state["waiting"]:
                return
            state["waiting"] = True
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self.setEnabled(False)
            QApplication.processEvents()

        def end_wait() -> None:
            if not state["waiting"]:
                return
            state["waiting"] = False
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)

        def finish(result: dict | None = None, error: str = "", cancelled: bool = False) -> None:
            if state["finished"]:
                return
            if result is None and not error and cancelled:
                state["finished"] = True
                end_wait()
                return
            if result is not None and result.get("success"):
                state["finished"] = True
                end_wait()
                on_success()
                return

            state["finished"] = True
            end_wait()
            message = error or (result or {}).get("error", "Could not open project")
            self._set_label_tone(status_label, "error")
            status_label.setText(f"✖ {message}")
            if result and result.get("already_open"):
                QMessageBox.information(
                    self,
                    "Project Already Open",
                    f"The sketch project '{project_name}' is already open in another window.\n\n"
                    "Switched focus to the active window.",
                )

        def perform() -> None:
            if self._is_busy():
                finish(error="Changing project is not allowed while an action is in progress.")
                return
            begin_wait()
            try:
                finish(action(bool(in_new_window)))
            except Exception as exc:
                finish(error=str(exc))

        if not in_new_window and self._allow_window_choice:
            self._prepare_current_window_switch(
                target,
                lambda ready, error, cancelled: perform() if ready else finish(error=error, cancelled=cancelled),
                on_wait=begin_wait,
            )
        else:
            perform()

    def _open_project(
        self,
        path: str,
        open_in_new_window: bool = False,
        active_file: str | None = None,
    ) -> dict:
        if not self._backend:
            return {"success": True}
        if open_in_new_window:
            result = self._backend.open_project_window(active_file or path)
            if result.get("success") and result.get("already_open"):
                from main.core.config import focus_project_window
                # Closing a modal picker can reactivate its parent; focus the
                # requested project after the dialog has finished closing.
                hwnd, pid = result.get("owner_hwnd", 0), result.get("owner_pid", 0)
                QTimer.singleShot(250, lambda: focus_project_window(hwnd, pid))
            return result
        try:
            same_project = Path(self._backend.sketch_dir_path).resolve() == Path(path).resolve()
        except (OSError, TypeError, ValueError):
            same_project = False
        if same_project:
            if active_file:
                set_active_file = getattr(self._backend, "set_active_file", None)
                if callable(set_active_file):
                    set_active_file(active_file)
                editor = getattr(self.parentWidget(), "_editor_panel", None)
                if editor and hasattr(editor, "open_file"):
                    editor.open_file(active_file)
            return {"success": True, "already_current": True}
        if active_file:
            return self._backend.open_project(path, active_file=active_file)
        return self._backend.open_project(path)

    def _validate_existing_project_folder(self, folder: Path) -> dict:
        """Use the backend's no-mutation project check before window prompts."""
        if self._backend and hasattr(self._backend, "validate_existing_project_folder"):
            try:
                return self._backend.validate_existing_project_folder(str(folder))
            except Exception as exc:
                return {"success": False, "error": str(exc)}
        return {"success": True}

    def _setup_open_projects_tab(self) -> None:
        tab, layout, actions = self._make_tab("Open projects")
        hint = QLabel("Bring an open sketch workspace to the front.", tab)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._open_projects_list = QListWidget(tab)
        self._open_projects_list.setMinimumHeight(90)
        self._open_projects_list.itemDoubleClicked.connect(self._focus_open_project)
        layout.addWidget(self._open_projects_list, 1)
        self._open_projects_status = QLabel(tab)
        self._open_projects_status.setWordWrap(True)
        layout.addWidget(self._open_projects_status)
        self._btn_refresh_projects = self._action("Refresh", "secondary", self._load_open_projects, tab)
        self._btn_cancel_open = self._action("Cancel", "secondary", self.reject, tab)
        self._focus_project_btn = self._action("Show window", "primary", self._focus_open_project, tab)
        actions.addWidget(self._btn_refresh_projects)
        actions.addStretch()
        actions.addWidget(self._btn_cancel_open)
        actions.addWidget(self._focus_project_btn)
        self._tabs.currentChanged.connect(lambda index: self._load_open_projects()
                                          if self._tabs.widget(index) is tab else None)
        self._load_open_projects()

    def _load_open_projects(self) -> None:
        from main.core.config import get_open_projects
        self._open_projects_list.clear()
        for project in get_open_projects():
            folder = Path(project["folder"])
            label = folder.name + (" (this window)" if project["current"] else "")
            item = QListWidgetItem(f"{label}\n{folder}")
            item.setToolTip(str(folder))
            item.setData(Qt.ItemDataRole.UserRole, project)
            self._open_projects_list.addItem(item)
        has_projects = self._open_projects_list.count() > 0
        self._focus_project_btn.setEnabled(has_projects)
        if has_projects:
            self._open_projects_list.setCurrentRow(0)
        self._open_projects_status.setText("" if has_projects else "No sketch windows are open yet.")

    def _focus_open_project(self, item=None) -> None:
        if self._is_busy():
            self._open_projects_status.setText("Project switching is unavailable while an action is running.")
            return
        from main.core.config import find_project_window, focus_project_window
        if not isinstance(item, QListWidgetItem):
            item = self._open_projects_list.currentItem()
        if item is None:
            return
        project = item.data(Qt.ItemDataRole.UserRole)
        owner = find_project_window(project["folder"])
        if not owner:
            self._load_open_projects()
            self._open_projects_status.setText("That project window has closed.")
            return
        if project["current"] and self.parentWidget():
            self.reject()
            self.parentWidget().raise_()
            self.parentWidget().activateWindow()
        elif focus_project_window(owner.get("hwnd", 0), owner.get("pid", 0)):
            self.reject()
            hwnd, pid = owner.get("hwnd", 0), owner.get("pid", 0)
            QTimer.singleShot(250, lambda: focus_project_window(hwnd, pid))
        else:
            self._open_projects_status.setText("Select this project window from your desktop.")
