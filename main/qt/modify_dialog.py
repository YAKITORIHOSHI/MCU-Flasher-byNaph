#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.modify_dialog — Modify Project Files dialog for MCU Flasher by Naph.

Replaces the Tkinter Modify Files dialog (Add, Rename, Delete sketch source files).
Operates directly on the current sketch folder and refreshes the editor on success.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from main.web_bridge import MCUWebBackendAPI

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
    QWidget, QPushButton, QLabel, QLineEdit, QComboBox,
    QMessageBox,
)

ALLOWED_EXTENSIONS = [".h", ".cpp", ".ino", ".txt"]


class ModifyFilesDialog(QDialog):
    """
    Dialog for adding, renaming, and deleting files within the active sketch project.
    """

    def __init__(self, backend: Optional["MCUWebBackendAPI"] = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._backend = backend
        self.setWindowTitle("🛠 Modify Project Files")
        self.setFixedSize(500, 360)
        self.setModal(True)

        self._setup_ui()
        self._apply_dialog_theme()
        self._refresh_file_lists()

        # Connect theme changed signal for live re-theming
        try:
            from main.qt.signals import signals
            signals.theme_changed.connect(self._apply_dialog_theme)
        except Exception:
            pass

    def _is_busy(self) -> bool:
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
                "Modifying project files is not allowed while an action is in progress.\n\n"
                "Please wait for the current action to finish or stop it first.",
            )
            return QDialog.DialogCode.Rejected
        return super().exec()

    def _apply_dialog_theme(self, theme_mode: str | None = None) -> None:
        """Apply active theme palette across all ModifyFilesDialog components."""
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
        btn_upload    = pal.get("BTN_UPLOAD", "#1a4a6e")
        btn_upload_h  = pal.get("BTN_UPLOAD_H", "#1f5a88")
        btn_stop      = pal.get("BTN_STOP", "#6e2020")
        btn_stop_h    = pal.get("BTN_STOP_H", "#882828")
        btn_clear     = pal.get("BTN_CLEAR", "#2d3748")
        btn_clear_h   = pal.get("BTN_CLEAR_H", "#3a4a60")

        self.setStyleSheet(f"QDialog {{ background-color: {bg_dark}; color: {text}; }}")
        self._header_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {cyan}; font-family: 'Montserrat', 'Segoe UI', sans-serif;")
        self._path_lbl.setStyleSheet(f"font-size: 11px; color: {text_dim}; font-family: Consolas, monospace;")

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
                padding: 6px 16px;
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
            QLineEdit:focus {{ border-color: {cyan}; }}
        """
        self._add_name_edit.setStyleSheet(input_style)
        self._rename_edit.setStyleSheet(input_style)

        combo_style = f"""
            QComboBox {{
                background: {bg_darkest};
                color: {text_bright};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 5px 8px;
                font-family: Consolas, monospace;
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
        """
        self._add_ext_combo.setStyleSheet(combo_style)
        self._rename_combo.setStyleSheet(combo_style)
        self._delete_combo.setStyleSheet(combo_style)

        self._btn_close.setStyleSheet(f"""
            QPushButton:enabled {{
                background: {btn_clear};
                color: {text_bright};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 6px 20px;
                font-weight: 600;
                font-size: 12px;
            }}
            QPushButton:enabled:hover {{ background: {btn_clear_h}; border-color: {cyan}; }}
            QPushButton:enabled:pressed {{ background: {bg_mid}; border-color: {cyan}; }}
            QPushButton:disabled {{ background: {bg_dark}; color: {text_dim}; border: 1px solid {border}; }}
        """)

        self._btn_add.setStyleSheet(f"""
            QPushButton:enabled {{
                background: {btn_compile};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                padding: 7px 18px;
                font-weight: 700;
                font-size: 12px;
            }}
            QPushButton:enabled:hover {{ background: {btn_compile_h}; border-color: {pal.get('GREEN', '#4ec994')}; }}
            QPushButton:enabled:pressed {{ background: {btn_compile}; border-color: {pal.get('GREEN', '#4ec994')}; }}
            QPushButton:disabled {{ background: {bg_dark}; color: {text_dim}; border: 1px solid {border}; }}
        """)

        self._btn_rename.setStyleSheet(f"""
            QPushButton:enabled {{
                background: {btn_upload};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                padding: 7px 18px;
                font-weight: 700;
                font-size: 12px;
            }}
            QPushButton:enabled:hover {{ background: {btn_upload_h}; border-color: {pal.get('BLUE', '#5ca4f0')}; }}
            QPushButton:enabled:pressed {{ background: {btn_upload}; border-color: {pal.get('BLUE', '#5ca4f0')}; }}
            QPushButton:disabled {{ background: {bg_dark}; color: {text_dim}; border: 1px solid {border}; }}
        """)

        self._btn_delete.setStyleSheet(f"""
            QPushButton:enabled {{
                background: {btn_stop};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                padding: 7px 18px;
                font-weight: 700;
                font-size: 12px;
            }}
            QPushButton:enabled:hover {{ background: {btn_stop_h}; border-color: {pal.get('RED', '#e74c3c')}; }}
            QPushButton:enabled:pressed {{ background: {btn_stop}; border-color: {pal.get('RED', '#e74c3c')}; }}
            QPushButton:disabled {{ background: {bg_dark}; color: {text_dim}; border: 1px solid {border}; }}
        """)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # Header
        self._header_lbl = QLabel("🛠 Modify Project Files")
        root.addWidget(self._header_lbl)

        sketch_path = str(self._backend.sketch_dir_path) if self._backend else "No project"
        self._path_lbl = QLabel(f"Project: {sketch_path}")
        self._path_lbl.setWordWrap(True)
        root.addWidget(self._path_lbl)

        # Tab Widget
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, stretch=1)

        # Tab 1: Add File
        self._setup_add_tab()

        # Tab 2: Rename File
        self._setup_rename_tab()

        # Tab 3: Delete File
        self._setup_delete_tab()

        # Bottom Close Button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_close = QPushButton("Close")
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self._btn_close)
        root.addLayout(btn_row)

    def _setup_add_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        hint = QLabel("Create a new header or source file inside the active sketch folder:")
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)

        self._add_name_edit = QLineEdit()
        self._add_name_edit.setPlaceholderText("filename (e.g. config or helpers)")
        row.addWidget(self._add_name_edit, stretch=1)

        self._add_ext_combo = QComboBox()
        for ext in ALLOWED_EXTENSIONS:
            self._add_ext_combo.addItem(ext)
        row.addWidget(self._add_ext_combo)
        layout.addLayout(row)

        self._add_status = QLabel("")
        self._add_status.setStyleSheet("font-size: 11px; min-height: 16px;")
        layout.addWidget(self._add_status)

        layout.addStretch()

        self._btn_add = QPushButton("✚ Create File")
        self._btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add.clicked.connect(self._on_add_file)
        layout.addWidget(self._btn_add, alignment=Qt.AlignmentFlag.AlignRight)

        self._tabs.addTab(tab, "✚ Add")

    def _setup_rename_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        hint = QLabel("Select an existing project file to rename:")
        layout.addWidget(hint)

        self._rename_combo = QComboBox()
        layout.addWidget(self._rename_combo)

        lbl_new = QLabel("New name (with extension):")
        layout.addWidget(lbl_new)

        self._rename_edit = QLineEdit()
        self._rename_edit.setPlaceholderText("new_filename.h")
        layout.addWidget(self._rename_edit)

        self._rename_status = QLabel("")
        self._rename_status.setStyleSheet("font-size: 11px; min-height: 16px;")
        layout.addWidget(self._rename_status)

        layout.addStretch()

        self._btn_rename = QPushButton("✏ Rename File")
        self._btn_rename.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_rename.clicked.connect(self._on_rename_file)
        layout.addWidget(self._btn_rename, alignment=Qt.AlignmentFlag.AlignRight)

        self._tabs.addTab(tab, "✏ Rename")

    def _setup_delete_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        hint = QLabel("Select a file to permanently delete from the sketch folder:")
        layout.addWidget(hint)

        self._delete_combo = QComboBox()
        layout.addWidget(self._delete_combo)

        warn_lbl = QLabel("⚠️ Deleted files cannot be restored from the Recycle Bin.")
        warn_lbl.setStyleSheet("color: #e74c3c; font-size: 11px; font-weight: 600;")
        layout.addWidget(warn_lbl)

        self._delete_status = QLabel("")
        self._delete_status.setStyleSheet("font-size: 11px; min-height: 16px;")
        layout.addWidget(self._delete_status)

        layout.addStretch()

        self._btn_delete = QPushButton("🗑 Delete File")
        self._btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_delete.clicked.connect(self._on_delete_file)
        layout.addWidget(self._btn_delete, alignment=Qt.AlignmentFlag.AlignRight)

        self._tabs.addTab(tab, "🗑 Delete")

    # ── Helpers & Actions ────────────────────────────────────────────────────

    def _refresh_file_lists(self) -> None:
        if not self._backend or not self._backend.sketch_dir_path:
            return
        files = self._backend.get_project_files()
        names = [f["name"] for f in files if isinstance(f, dict) and "name" in f]

        self._rename_combo.clear()
        self._delete_combo.clear()
        for name in names:
            self._rename_combo.addItem(name)
            self._delete_combo.addItem(name)

        has_files = bool(names)
        self._btn_rename.setEnabled(has_files)
        self._btn_rename.setCursor(Qt.CursorShape.PointingHandCursor if has_files else Qt.CursorShape.ArrowCursor)
        self._btn_delete.setEnabled(has_files)
        self._btn_delete.setCursor(Qt.CursorShape.PointingHandCursor if has_files else Qt.CursorShape.ArrowCursor)

    def _on_add_file(self) -> None:
        if not self._backend:
            return
        if self._is_busy():
            self._add_status.setStyleSheet("color: #e74c3c;")
            self._add_status.setText("✖ Modifying files is not allowed while an action is in progress.")
            return
        raw = self._add_name_edit.text().strip()
        if not raw:
            self._add_status.setStyleSheet("color: #e74c3c;")
            self._add_status.setText("✖ Please enter a file name.")
            return

        ext = self._add_ext_combo.currentText()
        if not raw.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
            filename = raw + ext
        else:
            filename = raw

        res = self._backend.add_project_file(filename)
        if res.get("success"):
            self._add_status.setStyleSheet("color: #4ec994;")
            self._add_status.setText(f"✔ File '{filename}' created successfully.")
            self._add_name_edit.clear()
            self._refresh_file_lists()
        else:
            self._add_status.setStyleSheet("color: #e74c3c;")
            self._add_status.setText(f"✖ {res.get('error', 'Failed to create file')}")

    def _on_rename_file(self) -> None:
        if not self._backend:
            return
        if self._is_busy():
            self._rename_status.setStyleSheet("color: #e74c3c;")
            self._rename_status.setText("✖ Modifying files is not allowed while an action is in progress.")
            return
        old_name = self._rename_combo.currentText().strip()
        new_name = self._rename_edit.text().strip()
        if not old_name:
            self._rename_status.setStyleSheet("color: #e74c3c;")
            self._rename_status.setText("✖ No file selected to rename.")
            return
        if not new_name:
            self._rename_status.setStyleSheet("color: #e74c3c;")
            self._rename_status.setText("✖ Please enter a new file name.")
            return

        res = self._backend.rename_project_file(old_name, new_name)
        if res.get("success"):
            self._rename_status.setStyleSheet("color: #4ec994;")
            self._rename_status.setText(f"✔ Renamed '{old_name}' → '{new_name}'.")
            self._rename_edit.clear()
            self._refresh_file_lists()
        else:
            self._rename_status.setStyleSheet("color: #e74c3c;")
            self._rename_status.setText(f"✖ {res.get('error', 'Failed to rename file')}")

    def _on_delete_file(self) -> None:
        if not self._backend:
            return
        if self._is_busy():
            self._delete_status.setStyleSheet("color: #e74c3c;")
            self._delete_status.setText("✖ Modifying files is not allowed while an action is in progress.")
            return
        target = self._delete_combo.currentText().strip()
        if not target:
            self._delete_status.setStyleSheet("color: #e74c3c;")
            self._delete_status.setText("✖ No file selected to delete.")
            return

        confirm = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Are you sure you want to permanently delete '{target}'?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        res = self._backend.delete_project_file(target)
        if res.get("success"):
            self._delete_status.setStyleSheet("color: #4ec994;")
            self._delete_status.setText(f"✔ Deleted '{target}'.")
            self._refresh_file_lists()
        else:
            self._delete_status.setStyleSheet("color: #e74c3c;")
            self._delete_status.setText(f"✖ {res.get('error', 'Failed to delete file')}")
