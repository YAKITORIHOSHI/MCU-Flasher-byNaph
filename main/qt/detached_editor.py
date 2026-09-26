#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.detached_editor — Detached Floating Monaco Editor & Placeholder for MCU Flasher by Naph.

Provides:
- DetachedEditorWindow: Standalone floating QMainWindow hosting MonacoEditorPanel.
- EditorDetachedPlaceholder: In-place placeholder shown in the main window vertical splitter
  while the editor is popped out, allowing one-click reattachment.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QCloseEvent, QGuiApplication, QCursor
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QLabel, QPushButton


class DetachedEditorWindow(QMainWindow):
    """Independent standalone window hosting the detached Monaco Code Editor."""

    closing = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("MCU Flasher — Code Editor")
        self.setObjectName("detached-editor-window")

        # Dynamic size clamped to available screen work area
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        avail_w = avail.width() if avail else 1280
        avail_h = avail.height() if avail else 720

        target_w = min(1080, max(600, int(avail_w * 0.85)))
        target_h = min(720, max(400, int(avail_h * 0.85)))
        target_w = min(target_w, avail_w)
        target_h = min(target_h, avail_h)

        self.setMinimumSize(min(600, target_w), min(400, target_h))
        self.resize(target_w, target_h)

        if avail:
            x = avail.x() + max(0, (avail_w - target_w) // 2)
            y = avail.y() + max(0, (avail_h - target_h) // 2)
            self.move(x, y)

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
    Matches the LATEST-WORKING styling and provides an instant 'Attach' button.
    """

    attach_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("editor-detached-placeholder")
        self.setStyleSheet("""
            QWidget#editor-detached-placeholder {
                background-color: #0d1117;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        lbl_title = QLabel("📝 Editor Detached")
        lbl_title.setStyleSheet("""
            color: #00e5ff;
            font-size: 18px;
            font-weight: bold;
            font-family: 'Montserrat', 'Segoe UI', sans-serif;
        """)
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_desc = QLabel("The code editor is running in a separate window.")
        lbl_desc.setStyleSheet("""
            color: #94a3b8;
            font-size: 12px;
            font-family: 'Montserrat', 'Segoe UI', sans-serif;
        """)
        lbl_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_attach = QPushButton("🗕 Attach Editor to Main Window")
        btn_attach.setFixedHeight(30)
        btn_attach.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_attach.setStyleSheet("""
            QPushButton {
                background-color: #e67e22;
                color: #ffffff;
                border: 1px solid #d35400;
                border-radius: 4px;
                padding: 4px 18px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #d35400;
            }
            QPushButton:pressed {
                background-color: #ba4a00;
            }
        """)
        btn_attach.clicked.connect(self.attach_requested.emit)

        layout.addWidget(lbl_title)
        layout.addWidget(lbl_desc)
        layout.addWidget(btn_attach, alignment=Qt.AlignmentFlag.AlignCenter)
