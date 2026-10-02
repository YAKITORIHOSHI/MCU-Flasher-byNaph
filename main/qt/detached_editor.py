#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.detached_editor — Explicit editor detachment and reattachment.

Provides:
- DetachedEditorWindow: Standalone floating QMainWindow hosting MonacoEditorPanel.
- EditorDetachedPlaceholder: In-place placeholder shown in the main window vertical splitter
  while the editor is popped out, allowing one-click reattachment.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QCloseEvent
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QLabel, QPushButton


class DetachedEditorWindow(QMainWindow):
    """Independent standalone window hosting the detached Monaco Code Editor."""

    closing = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("MCU Flasher — Code Editor")
        self.setObjectName("detached-editor-window")

        from main.qt.responsive import fit_dialog, ScreenWatcher
        fit_dialog(self, (1040, 680), (360, 280))
        self._screen_watcher = ScreenWatcher(self)

        # Set window icon matching the application icon
        try:
            icon_path = Path(__file__).resolve().parent.parent.parent / "src" / "assets" / "mcu_icon.ico"
            if not icon_path.exists():
                icon_path = Path(__file__).resolve().parent.parent.parent / "src" / "mcu_icon.ico"
            if icon_path.exists():
                self.setWindowIcon(QIcon(str(icon_path)))
        except Exception:
            pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cw = self.centralWidget()
        if cw and hasattr(cw, "force_layout"):
            cw.force_layout()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        cw = self.centralWidget()
        if cw and hasattr(cw, "force_layout"):
            cw.force_layout()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Closing the detached window automatically re-attaches the editor to the main window."""
        self.closing.emit()
        event.accept()


class EditorDetachedPlaceholder(QWidget):
    """
    Placeholder displayed in the main window splitter when the editor is detached.
    Uses the active workspace theme and offers a direct Attach Editor action.
    """

    attach_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("editor-detached-placeholder")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        lbl_title = QLabel("Editor detached", self)
        lbl_title.setProperty("role", "title")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_desc = QLabel("Close the editor window or attach it here to return.", self)
        lbl_desc.setObjectName("detached-editor-description")
        lbl_desc.setWordWrap(True)
        lbl_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_attach = QPushButton("Attach Editor", self)
        btn_attach.setObjectName("btn-attach-editor")
        btn_attach.setProperty("role", "primary")
        btn_attach.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_attach.clicked.connect(self.attach_requested.emit)

        layout.addWidget(lbl_title)
        layout.addWidget(lbl_desc)
        layout.addWidget(btn_attach, alignment=Qt.AlignmentFlag.AlignCenter)
