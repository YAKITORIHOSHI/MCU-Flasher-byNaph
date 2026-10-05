#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.project_dialog — Project selector / creator dialog for MCU Flasher.

Replaces the Tkinter ProjectSelectorDialog.
Shows recent projects, allows browsing for existing folders with live file
content preview, and can scaffold a new sketch folder with templates.
"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from main.web_bridge import MCUWebBackendAPI

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
    QWidget, QListWidget, QListWidgetItem, QPushButton, QLabel,
    QLineEdit, QFileDialog, QComboBox, QCheckBox, QGroupBox, QFormLayout,
    QFrame, QApplication, QMessageBox,
)
from main.qt.icons import ActionButton as QPushButton

from main.core.constants import is_application_codebase_dir

# Supported sketch file extensions
SUPPORTED_EXTS = {".ino", ".cpp", ".c", ".h", ".hpp", ".txt"}


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
      1. Existing Project — browse for folder with live file preview
      2. New Project      — scaffold a new sketch with template
      3. Recent Projects  — list of recently opened projects
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

        # Determine start directory (NEVER the application codebase)
        default_user_dir = str(Path.home() / "Documents" / "example")
        if not Path(default_user_dir).is_dir():
            default_user_dir = str(Path.home() / "Documents")

        candidate_start = ""
        if initial_dir and Path(initial_dir).is_dir() and not is_application_codebase_dir(initial_dir):
            candidate_start = initial_dir
        elif self._backend and self._backend.sketch_dir_path and self._backend.sketch_dir_path.is_dir() and not is_application_codebase_dir(self._backend.sketch_dir_path):
            candidate_start = str(self._backend.sketch_dir_path)

        self._start_dir = candidate_start or default_user_dir

        self.setWindowTitle("MCU Flasher by Naph — Select Project")

        from main.qt.responsive import fit_dialog, ScreenWatcher
        fit_dialog(self, (680, 540), (400, 300))
        self.setModal(True)

        self._setup_ui()
        self._screen_watcher = ScreenWatcher(self)
        self._load_recents()

        # Update initial folder preview
        if self._start_dir:
            self._open_path_edit.setText(self._start_dir)
            self._update_existing_preview(self._start_dir)

    def _is_busy(self) -> bool:
        if self._allow_window_choice:
            return False
        if self._backend and (self._backend.is_busy or getattr(self._backend, "active_operation", None) is not None):
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
            try:
                from main.core.config import focus_project_window
                hwnd = int(self.winId())
                if hwnd:
                    focus_project_window(hwnd)
                    QTimer.singleShot(50, lambda: focus_project_window(hwnd))
                    QTimer.singleShot(200, lambda: focus_project_window(hwnd))
            except Exception:
                pass

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # ── Header Banner ───────────────────────────────────────────────────
        # ── Header Banner ───────────────────────────────────────────────────
        header = QWidget()
        hl = QVBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 4)
        hl.setSpacing(2)

        self._title_lbl = QLabel("MCU Flasher by Naph")
        self._sub_lbl = QLabel(
            "Choose whether each project opens here or in a separate window"
            if self._allow_window_choice
            else "Open an existing sketch project, or create a new one"
        )

        hl.addWidget(self._title_lbl)
        hl.addWidget(self._sub_lbl)
        root.addWidget(header)

        # ── Tab Container ───────────────────────────────────────────────────
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, stretch=1)

        # ── Tab 1: Existing Project ─────────────────────────────────────────
        self._setup_existing_tab()

        # ── Tab 2: New Project ──────────────────────────────────────────────
        self._setup_new_tab()

        # ── Tab 3: Recent Projects ──────────────────────────────────────────
        self._setup_recents_tab()
        self._setup_open_projects_tab()

        # Apply active theme dynamically to all widgets
        self._apply_dialog_theme()

        # Connect theme changed signal for live re-theming
        try:
            from main.qt.signals import signals
            signals.theme_changed.connect(self._apply_dialog_theme)
        except Exception:
            pass

    def _apply_dialog_theme(self, theme_mode: str | None = None) -> None:
        """Apply active theme palette across all ProjectDialog components."""
        if not theme_mode:
            from main.core.config import get_theme_mode
            theme_mode = get_theme_mode()
        from main.qt.theme import get_palette
        pal = get_palette(theme_mode)

        bg_darkest = pal.get("BG_DARKEST", "#0d1117")
        bg_dark    = pal.get("BG_DARK", "#151922")
        bg_mid     = pal.get("BG_MID", "#1c2333")
        bg_hover   = pal.get("BG_HOVER", "#2a3a55")

        text        = pal.get("TEXT", "#e0e6ed")
        text_bright = pal.get("TEXT_BRIGHT", "#ffffff")
        text_dim    = pal.get("TEXT_DIM", "#8fa1b3")
        cyan        = pal.get("CYAN", "#00d2ff")
        border      = pal.get("BORDER", "#2d3748")

        btn_compile   = pal.get("BTN_COMPILE", "#1a5c3a")
        btn_compile_h = pal.get("BTN_COMPILE_H", "#216e46")
        btn_stop      = pal.get("BTN_STOP", "#6e2020")
        btn_stop_h    = pal.get("BTN_STOP_H", "#882828")
        btn_clear     = pal.get("BTN_CLEAR", "#2d3748")
        btn_clear_h   = pal.get("BTN_CLEAR_H", "#3a4a60")

        self.setStyleSheet(f"QDialog {{ background-color: {bg_dark}; color: {text}; }}")
        self._title_lbl.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {cyan}; font-family: 'Montserrat', 'Segoe UI', sans-serif;")
        self._sub_lbl.setStyleSheet(f"font-size: 11px; color: {text_dim}; font-family: 'Montserrat', 'Segoe UI', sans-serif;")

        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {border};
                background: {bg_dark};
                border-radius: 6px;
                top: -1px;
            }}
            QTabBar::tab {{
                background: {bg_mid};
                color: {text_dim};
                padding: 7px 18px;
                font-weight: 600;
                font-size: 12px;
                border: 1px solid {border};
                border-bottom: none;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                margin-right: 3px;
            }}
            QTabBar::tab:selected {{
                background: {bg_dark};
                color: {cyan};
                border-bottom: 2px solid {cyan};
            }}
            QTabBar::tab:hover:!selected {{
                background: {bg_hover};
                color: {text};
            }}
        """)

        input_style = f"""
            QLineEdit {{
                background: {bg_darkest};
                color: {text_bright};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 6px 10px;
                font-family: Consolas, monospace;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: {cyan};
            }}
        """
        self._open_path_edit.setStyleSheet(input_style)
        self._new_name_edit.setStyleSheet(input_style)
        self._new_parent_edit.setStyleSheet(input_style)

        btn_browse_style = f"""
            QPushButton:enabled {{
                background: {btn_clear};
                color: {text_bright};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 6px 14px;
                font-weight: 600;
                font-size: 12px;
            }}
            QPushButton:enabled:hover {{
                background: {btn_clear_h};
                border-color: {cyan};
            }}
            QPushButton:enabled:pressed {{
                background: {bg_mid};
                border-color: {cyan};
            }}
            QPushButton:disabled {{
                background: {bg_dark};
                color: {text_dim};
                border: 1px solid {border};
            }}
        """
        self._btn_browse.setStyleSheet(btn_browse_style)
        self._btn_browse_parent.setStyleSheet(btn_browse_style)
        self._btn_clear_recents.setStyleSheet(btn_browse_style)

        self._preview_box.setStyleSheet(f"""
            QFrame {{
                background: {bg_darkest};
                border: 1px solid {border};
                border-radius: 5px;
                padding: 8px;
            }}
        """)
        self._p_title.setStyleSheet(f"color: {cyan}; font-weight: 700; font-size: 11px;")
        if not hasattr(self, "_existing_preview_lbl_colored") or not self._existing_preview_lbl_colored:
            self._existing_preview_lbl.setStyleSheet(f"color: {text_dim}; font-family: Consolas, monospace; font-size: 11px;")

        btn_cancel_style = f"""
            QPushButton:enabled {{
                background: {btn_stop};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                padding: 7px 18px;
                font-weight: 600;
                font-size: 12px;
            }}
            QPushButton:enabled:hover {{
                background: {btn_stop_h};
                border-color: {pal.get('RED', '#e74c3c')};
            }}
            QPushButton:enabled:pressed {{
                background: {btn_stop};
                border-color: {pal.get('RED', '#e74c3c')};
            }}
            QPushButton:disabled {{
                background: {bg_dark};
                color: {text_dim};
                border: 1px solid {border};
            }}
        """
        self._btn_cancel_existing.setStyleSheet(btn_cancel_style)
        self._btn_cancel_new.setStyleSheet(btn_cancel_style)
        self._btn_cancel_recent.setStyleSheet(btn_cancel_style)

        btn_action_style = f"""
            QPushButton:enabled {{
                background: {btn_compile};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                padding: 7px 20px;
                font-weight: 700;
                font-size: 12px;
            }}
            QPushButton:enabled:hover {{
                background: {btn_compile_h};
                border-color: {pal.get('GREEN', '#4ec994')};
            }}
            QPushButton:enabled:pressed {{
                background: {btn_compile};
                border-color: {pal.get('GREEN', '#4ec994')};
            }}
            QPushButton:disabled {{
                background: {bg_dark};
                color: {text_dim};
                border: 1px solid {border};
            }}
        """
        self._btn_open_existing.setStyleSheet(btn_action_style)
        self._btn_create.setStyleSheet(btn_action_style)
        self._btn_open_recent.setStyleSheet(btn_action_style)

        self._form_box.setStyleSheet(f"""
            QGroupBox {{
                background: transparent;
                border: 1px solid {border};
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 14px;
                color: {cyan};
                font-weight: 700;
                font-size: 12px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }}
        """)

        self._template_combo.setStyleSheet(f"""
            QComboBox {{
                background: {bg_darkest};
                color: {text_bright};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 5px 8px;
                font-size: 12px;
            }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{
                background-color: {bg_mid};
                color: {text};
                border: 1px solid {border};
                selection-background-color: {bg_hover};
                selection-color: {text_bright};
            }}
        """)

        self._cb_include_h.setStyleSheet(f"color: {text}; font-size: 12px;")
        self._cb_include_cpp.setStyleSheet(f"color: {text}; font-size: 12px;")

        self._recent_list.setStyleSheet(f"""
            QListWidget {{
                background: {bg_darkest};
                color: {text};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 4px;
                font-size: 12px;
            }}
            QListWidget::item {{
                padding: 6px 10px;
                border-radius: 3px;
            }}
            QListWidget::item:hover {{
                background: {bg_mid};
                color: {cyan};
            }}
            QListWidget::item:selected {{
                background: {bg_hover};
                color: {cyan};
                font-weight: 600;
            }}
        """)

        self._recent_preview_lbl.setStyleSheet(f"""
            QLabel {{
                background: {bg_darkest};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 6px 10px;
                color: {text};
                font-family: Consolas, monospace;
                font-size: 11px;
            }}
        """)

        self._btn_clear_recents.setStyleSheet(f"""
            QPushButton {{
                background: {btn_clear};
                color: {text};
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-size: 11px;
            }}
            QPushButton:hover {{ background: {btn_clear_h}; color: {text_bright}; }}
        """)

    # ─────────────────────────────────────────────────────────────────────────
    # Tab 1: Existing Project
    # ─────────────────────────────────────────────────────────────────────────
    def _setup_existing_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        hint = QLabel(
            "Pick a folder that already contains your sketch files\n"
            "(.ino, .cpp, .c, .h, .hpp, .txt and optional platformio.ini)."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Path input row
        path_row = QHBoxLayout()
        self._open_path_edit = QLineEdit()
        self._open_path_edit.setPlaceholderText("Select or enter sketch folder path…")
        self._open_path_edit.textChanged.connect(self._update_existing_preview)
        path_row.addWidget(self._open_path_edit, stretch=1)

        self._btn_browse = QPushButton("Browse…")
        self._btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_browse.clicked.connect(self._browse_folder)
        path_row.addWidget(self._btn_browse)
        layout.addLayout(path_row)

        # Folder contents preview frame
        self._preview_box = QFrame()
        p_layout = QVBoxLayout(self._preview_box)
        p_layout.setContentsMargins(8, 8, 8, 8)
        p_layout.setSpacing(4)

        self._p_title = QLabel("Folder Contents (.cpp / .ino / .h / .txt):")
        p_layout.addWidget(self._p_title)

        self._existing_preview_lbl = QLabel("Select a folder to view files...")
        self._existing_preview_lbl.setWordWrap(True)
        p_layout.addWidget(self._existing_preview_lbl)
        layout.addWidget(self._preview_box)

        # Status label
        self._existing_status = QLabel("")
        self._existing_status.setStyleSheet("font-size: 11px; min-height: 16px;")
        layout.addWidget(self._existing_status)

        layout.addStretch()

        # Action Buttons Row
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._btn_cancel_existing = QPushButton("Cancel")
        self._btn_cancel_existing.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel_existing.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_cancel_existing)

        self._btn_open_existing = QPushButton("Open Project ▶")
        self._btn_open_existing.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_open_existing.clicked.connect(self._open_existing)
        btn_row.addWidget(self._btn_open_existing)

        layout.addLayout(btn_row)
        self._tabs.addTab(tab, "Existing project")

    # ─────────────────────────────────────────────────────────────────────────
    # Tab 2: New Project
    # ─────────────────────────────────────────────────────────────────────────
    def _setup_new_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self._form_box = QGroupBox("New Sketch Configuration")
        form = QFormLayout(self._form_box)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        # Name
        self._new_name_edit = QLineEdit()
        self._new_name_edit.setPlaceholderText("MySketch")
        self._new_name_edit.setText("MySketch")
        self._new_name_edit.textChanged.connect(self._update_new_preview)
        form.addRow("Project name:", self._new_name_edit)

        # Parent location
        parent_row = QHBoxLayout()
        self._new_parent_edit = QLineEdit()
        default_parent = (
            self._backend.get_default_project_parent()
            if self._backend else str(Path.home() / "Documents" / "Arduino")
        )
        self._new_parent_edit.setText(default_parent)
        self._new_parent_edit.textChanged.connect(self._update_new_preview)
        parent_row.addWidget(self._new_parent_edit, stretch=1)

        self._btn_browse_parent = QPushButton("…")
        self._btn_browse_parent.setFixedWidth(34)
        self._btn_browse_parent.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_browse_parent.clicked.connect(self._browse_parent)
        parent_row.addWidget(self._btn_browse_parent)
        form.addRow("Location:", parent_row)

        # Template
        self._template_combo = QComboBox()
        self._template_combo.addItem("Standard Sketch (setup & loop)", "standard")
        self._template_combo.addItem("Bare Minimum (empty setup & loop)", "bare")
        self._template_combo.addItem("Blink Example (LED_BUILTIN)", "blink")
        form.addRow("Template:", self._template_combo)

        # Include .h / .cpp
        self._cb_include_h = QCheckBox("Include header file (.h)")
        self._cb_include_h.stateChanged.connect(self._update_new_preview)
        form.addRow("", self._cb_include_h)

        self._cb_include_cpp = QCheckBox("Include implementation file (.cpp)")
        self._cb_include_cpp.stateChanged.connect(self._update_new_preview)
        form.addRow("", self._cb_include_cpp)

        layout.addWidget(self._form_box)

        # Preview
        self._new_preview_lbl = QLabel("")
        self._new_preview_lbl.setStyleSheet("color: #56cfbf; font-family: Consolas, monospace; font-size: 11px;")
        self._new_preview_lbl.setWordWrap(True)
        layout.addWidget(self._new_preview_lbl)
        self._update_new_preview()

        # Status
        self._new_status = QLabel("")
        self._new_status.setStyleSheet("font-size: 11px; min-height: 16px;")
        layout.addWidget(self._new_status)

        layout.addStretch()

        # Button row
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._btn_cancel_new = QPushButton("Cancel")
        self._btn_cancel_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel_new.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_cancel_new)

        self._btn_create = QPushButton("✚ Create Project")
        self._btn_create.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_create.clicked.connect(self._create_project)
        btn_row.addWidget(self._btn_create)

        layout.addLayout(btn_row)
        self._tabs.addTab(tab, "New project")

    # ─────────────────────────────────────────────────────────────────────────
    # Tab 3: Recent Projects
    # ─────────────────────────────────────────────────────────────────────────
    def _setup_recents_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self._recent_hint_lbl = QLabel("Select a recently opened sketch project:")
        layout.addWidget(self._recent_hint_lbl)

        self._recent_list = QListWidget()
        self._recent_list.itemClicked.connect(self._on_recent_clicked)
        self._recent_list.itemDoubleClicked.connect(self._open_selected_recent)
        layout.addWidget(self._recent_list, stretch=1)

        # Recent preview
        self._recent_preview_lbl = QLabel("Select a project from the list above...")
        self._recent_preview_lbl.setWordWrap(True)
        layout.addWidget(self._recent_preview_lbl)

        # Button row
        btn_row = QHBoxLayout()

        self._btn_clear_recents = QPushButton("Clear History")
        self._btn_clear_recents.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_recents.clicked.connect(self._clear_recents)
        btn_row.addWidget(self._btn_clear_recents)

        btn_row.addStretch()

        self._btn_cancel_recent = QPushButton("Cancel")
        self._btn_cancel_recent.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel_recent.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_cancel_recent)

        self._btn_open_recent = QPushButton("Open Selected ▶")
        self._btn_open_recent.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_open_recent.clicked.connect(self._open_selected_recent)
        btn_row.addWidget(self._btn_open_recent)

        layout.addLayout(btn_row)
        self._tabs.addTab(tab, "Recent projects")

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers & Event Handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _update_existing_preview(self, text: str) -> None:
        raw = text.strip()
        if not raw:
            self._existing_preview_lbl.setText("No folder selected")
            self._existing_preview_lbl.setStyleSheet("color: #6b7280; font-family: Consolas, monospace;")
            return
        p = Path(raw)
        if p.is_file():
            p = p.parent
        if is_application_codebase_dir(p):
            self._existing_preview_lbl.setText("⛔ Cannot open MCU Flasher application codebase as a sketch project")
            self._existing_preview_lbl.setStyleSheet("color: #e74c3c; font-family: Consolas, monospace;")
            return
        if not p.exists() or not p.is_dir():
            self._existing_preview_lbl.setText("⚠️ Folder does not exist")
            self._existing_preview_lbl.setStyleSheet("color: #e74c3c; font-family: Consolas, monospace;")
            return
        files = _get_project_files_fast(p)
        if files:
            summary = ", ".join(files[:8])
            if len(files) > 8:
                summary += f" (+{len(files) - 8} more)"
            self._existing_preview_lbl.setText(f"Found ({len(files)} source files):\n{summary}")
            self._existing_preview_lbl.setStyleSheet("color: #4ec994; font-family: Consolas, monospace;")
        else:
            self._existing_preview_lbl.setText("ℹ️ No sketch files found (.ino will be scaffolded automatically)")
            self._existing_preview_lbl.setStyleSheet("color: #f1c40f; font-family: Consolas, monospace;")

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
        start = self._open_path_edit.text().strip() or self._start_dir
        # Use getOpenFileName so .ino/.cpp/.h/.txt files are visible
        # (stable reference: dialogs.py:577 uses filedialog.askopenfilename)
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
                self._existing_status.setStyleSheet("color: #e74c3c;")
                self._existing_status.setText("✖ Cannot select MCU Flasher application folder.")
                return
            self._open_path_edit.setText(str(folder))

    def _browse_parent(self) -> None:
        start = self._new_parent_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Select Location", start)
        if folder:
            self._new_parent_edit.setText(folder)

    def _open_existing(self) -> None:
        if self._is_busy():
            self._existing_status.setStyleSheet("color: #e74c3c;")
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
            self._existing_status.setStyleSheet("color: #e74c3c;")
            self._existing_status.setText("✖ Please select a folder path.")
            return
        p = Path(path)
        if p.is_file():
            p = p.parent
            path = str(p)
        if is_application_codebase_dir(p):
            self._existing_status.setStyleSheet("color: #e74c3c;")
            self._existing_status.setText("✖ The MCU Flasher application folder cannot be opened as a project.")
            return
        if not p.is_dir():
            self._existing_status.setStyleSheet("color: #e74c3c;")
            self._existing_status.setText("✖ The specified folder does not exist.")
            return

        self._dispatch_project_action(
            path,
            p.name,
            lambda in_new_window: self._open_project(path, in_new_window),
            lambda: self._accept_selected_project(p),
            self._existing_status,
        )

    def _create_project(self) -> None:
        if self._is_busy():
            self._new_status.setStyleSheet("color: #e74c3c;")
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
            self._new_status.setStyleSheet("color: #e74c3c;")
            self._new_status.setText("✖ Project name and location are required.")
            return
        if is_application_codebase_dir(parent):
            self._new_status.setStyleSheet("color: #e74c3c;")
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
                    item = QListWidgetItem(f"📁 {p.name}  —  {r}")
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
        if not self._allow_window_choice:
            return False

        prompt = QMessageBox(self)
        self._style_project_prompt(prompt)
        prompt.setIcon(QMessageBox.Icon.Question)
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

    @staticmethod
    def _style_project_prompt(prompt: QMessageBox) -> None:
        """Keep native choice prompts readable and aligned with the active glass theme."""
        try:
            from main.core.config import get_theme_mode
            from main.qt.theme import get_palette
            pal = get_palette(get_theme_mode())
        except Exception:
            pal = {}
        bg = pal.get("BG_DARK", "#151922")
        surface = pal.get("BG_MID", "#1c2333")
        hover = pal.get("BG_HOVER", "#2a3a55")
        text = pal.get("TEXT", "#e0e6ed")
        bright = pal.get("TEXT_BRIGHT", "#ffffff")
        border = pal.get("BORDER", "#2d3748")
        cyan = pal.get("CYAN", "#00d2ff")
        prompt.setStyleSheet(f"""
            QMessageBox {{ background-color: {bg}; color: {text}; }}
            QMessageBox QLabel {{ color: {text}; background: transparent; font-size: 12px; }}
            QMessageBox QPushButton {{
                min-width: 88px; padding: 6px 12px; background-color: {surface};
                color: {bright}; border: 1px solid {border}; border-radius: 6px;
            }}
            QMessageBox QPushButton:hover {{ background-color: {hover}; border-color: {cyan}; }}
            QMessageBox QPushButton:pressed {{ background-color: {bg}; }}
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

        prompt = QMessageBox(self)
        self._style_project_prompt(prompt)
        prompt.setIcon(QMessageBox.Icon.Warning)
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
            status_label.setStyleSheet("color: #e74c3c;")
            status_label.setText(f"✖ {message}")
            if result and result.get("already_open"):
                QMessageBox.information(
                    self,
                    "Project Already Open",
                    f"The sketch project '{project_name}' is already open in another window.\n\n"
                    "Switched focus to the active window.",
                )

        def perform() -> None:
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

    def _open_project(self, path: str, open_in_new_window: bool = False) -> dict:
        if not self._backend:
            return {"success": True}
        if open_in_new_window:
            result = self._backend.open_project_window(path)
            if result.get("success") and result.get("already_open"):
                from main.core.config import focus_project_window
                # Closing a modal picker can reactivate its parent; focus the
                # requested project after the dialog has finished closing.
                hwnd, pid = result.get("owner_hwnd", 0), result.get("owner_pid", 0)
                QTimer.singleShot(250, lambda: focus_project_window(hwnd, pid))
            return result
        if self._backend:
            try:
                if Path(self._backend.sketch_dir_path).resolve() == Path(path).resolve():
                    return {"success": True, "already_current": True}
            except (OSError, TypeError, ValueError):
                pass
        return self._backend.open_project(path)

    def _setup_open_projects_tab(self) -> None:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        hint = QLabel(
            "Choose a window when opening a project. Each window keeps its own editor, terminal and board/port selection.",
            tab,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._open_projects_list = QListWidget(tab)
        self._open_projects_list.itemDoubleClicked.connect(self._focus_open_project)
        layout.addWidget(self._open_projects_list, 1)
        self._open_projects_status = QLabel(tab)
        self._open_projects_status.setWordWrap(True)
        layout.addWidget(self._open_projects_status)
        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh", tab)
        refresh.clicked.connect(self._load_open_projects)
        buttons.addWidget(refresh)
        buttons.addStretch()
        self._focus_project_btn = QPushButton("Show window", tab)
        self._focus_project_btn.clicked.connect(self._focus_open_project)
        buttons.addWidget(self._focus_project_btn)
        layout.addLayout(buttons)
        self._tabs.addTab(tab, "Open projects")
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
