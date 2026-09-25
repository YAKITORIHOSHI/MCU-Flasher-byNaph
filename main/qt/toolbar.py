#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.toolbar — Top toolbar and secondary controls bar for MCU Flasher.

Replaces the Tkinter title_frame + inner_actions + ctrl_frame sections
from UILayoutMixin._build_ui().

Structure:
  1. Primary Toolbar (QToolBar) — logo, action buttons (Compile, Upload,
     Stop, Clean, Save, Save All, Reload, Modify), sketch path label
  2. Secondary Controls (QWidget bar) — Board selector, Port selector,
     Upload Speed, Options checkboxes, view toggles, Settings, AI buttons

All backend calls are dispatched via the MCUWebBackendAPI instance.
UI state updates (busy / idle) come from the MCUSignals bus.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main.web_bridge import MCUWebBackendAPI

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Qt, Slot, Signal, QTimer, QSize, QPoint
# pyrefly: ignore [missing-import]
from PySide6.QtGui import QColor, QPainter, QPen, QGuiApplication
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QToolBar, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QComboBox, QCheckBox,
    QSizePolicy, QFrame, QStyleOptionComboBox, QStylePainter, QStyle,
    QMessageBox, QApplication,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants (mirror main.core.constants defaults)
# ─────────────────────────────────────────────────────────────────────────────
from main.core.constants import MAX_BAUD_RATE

DEFAULT_BAUD          = 115200
DEFAULT_UPLOAD_SPEED  = 460800
VALID_BAUD_RATES      = [b for b in [9600, 19200, 38400, 57600, 74880, 115200, 230400, 460800, 512000, 921600] if b <= MAX_BAUD_RATE]
UPLOAD_SPEEDS         = [s for s in [115200, 230400, 460800, 512000, 921600] if s <= MAX_BAUD_RATE]


def _make_action_btn(text: str, tooltip: str, object_name: str,
                     color: str = "", hover_color: str = "") -> QPushButton:
    """Create an action button for the primary toolbar."""
    btn = QPushButton(text)
    btn.setObjectName(object_name)
    btn.setToolTip(tooltip)
    btn.setFixedHeight(30)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


class CompactDropdownPopup(QWidget):
    """
    Frameless, floating dropdown menu for compact mode actions and options.
    Auto-closes on focus loss, outside click, or Escape key.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        from main.core.theme import Theme
        self.setStyleSheet(
            f"CompactDropdownPopup {{"
            f"  background-color: {Theme.BG_MID};"
            f"  border: 1px solid {Theme.BORDER};"
            f"  border-radius: 6px;"
            f"}}"
        )
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(4)

    def add_button(
        self,
        text: str,
        callback,
        normal_bg: str,
        hover_bg: str,
        enabled: bool = True,
        custom_style: str = "",
    ) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setFixedHeight(28)
        btn.setCursor(
            Qt.CursorShape.PointingHandCursor
            if enabled
            else Qt.CursorShape.ArrowCursor
        )
        btn.setEnabled(enabled)
        from main.core.theme import Theme

        if custom_style:
            btn.setStyleSheet(custom_style)
        else:
            style = (
                f"QPushButton {{"
                f"  background-color: {normal_bg};"
                f"  color: {Theme.TEXT_BRIGHT};"
                f"  border: 1px solid {Theme.BORDER};"
                f"  border-radius: 4px;"
                f"  padding: 3px 12px;"
                f"  font-size: 11px;"
                f"  font-weight: 600;"
                f"  text-align: center;"
                f"}}"
                f"QPushButton:hover {{"
                f"  background-color: {hover_bg};"
                f"  border: 1px solid {Theme.CYAN};"
                f"  color: #ffffff;"
                f"}}"
                f"QPushButton:pressed {{"
                f"  background-color: {Theme.BG_DARK};"
                f"  border: 1px solid {Theme.CYAN};"
                f"}}"
                f"QPushButton:disabled {{"
                f"  background-color: {Theme.BG_DARK};"
                f"  color: {Theme.TEXT_DIM};"
                f"  border: 1px solid {Theme.BORDER};"
                f"}}"
            )
            btn.setStyleSheet(style)

        def _on_click():
            self.close()
            callback()

        btn.clicked.connect(_on_click)
        self._layout.addWidget(btn)
        return btn

    def show_below(
        self,
        anchor_widget: QWidget,
        min_width: int = 0,
        alignment: str = "left",
    ) -> None:
        self.adjustSize()
        req_w = max(min_width, self.sizeHint().width())
        req_h = self.sizeHint().height()

        anchor_pos = anchor_widget.mapToGlobal(QPoint(0, 0))
        anchor_w = anchor_widget.width()
        anchor_h = anchor_widget.height()

        if alignment == "right":
            # Right-align with anchor: dropdown extends inward (leftward) into the window
            x = anchor_pos.x() + anchor_w - req_w
        elif alignment == "center":
            # Symmetrically center under anchor
            x = anchor_pos.x() + (anchor_w - req_w) // 2
        else:
            # Left-align with anchor
            x = anchor_pos.x()

        y = anchor_pos.y() + anchor_h + 2

        # 1. Constrain strictly within top-level parent window bounds
        win = anchor_widget.window()
        if win:
            win_pos = win.mapToGlobal(QPoint(0, 0))
            win_left = win_pos.x()
            win_right = win_left + win.width()
            if x + req_w > win_right - 6:
                x = max(win_left + 6, win_right - req_w - 6)
            if x < win_left + 6:
                x = win_left + 6

        # 2. Constrain within physical screen geometry
        screen = anchor_widget.screen() or QGuiApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            if x + req_w > geom.right() - 8:
                x = max(geom.left() + 8, geom.right() - req_w - 8)
            if x < geom.left() + 8:
                x = geom.left() + 8
            if y + req_h > geom.bottom() - 8:
                y = anchor_pos.y() - req_h - 2

        self.setGeometry(x, y, req_w, req_h)
        self.show()
        self.raise_()
        self.activateWindow()


class PrimaryToolbar(QToolBar):
    """
    Top-level action toolbar: logo + action buttons + sketch path label.
    Mirrors UILayoutMixin title_frame + inner_actions.
    """

    # Emitted when user clicks sketch label or folder icon
    open_sketch_requested  = Signal()
    select_sketch_requested = Signal()

    def __init__(self, backend: "MCUWebBackendAPI", parent: QWidget | None = None):
        super().__init__("Primary Actions", parent)
        self._backend = backend
        self.setMovable(False)
        self.setFloatable(False)
        self.setIconSize(__import__("PySide6.QtCore", fromlist=["QSize"]).QSize(16, 16))
        self.setObjectName("primary-toolbar")
        self._current_sketch_path: str = ""
        self._setup_widgets()

    def _setup_widgets(self) -> None:
        # ── 1. Left: Logo / Title ───────────────────────────────────────────
        self.logo = QLabel("⚡ MCU Flasher by Naph")
        self.logo.setStyleSheet(
            "color: #56cfbf; font-size: 15px; font-weight: 700; font-family: 'Montserrat', 'Segoe UI', sans-serif; background: transparent;"
        )
        self.addWidget(self.logo)

        # ── 2. Left Expanding Spacer ────────────────────────────────────────
        self.sp_left = QWidget()
        self.sp_left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.addWidget(self.sp_left)

        # ── 3. Centered Actions Container ───────────────────────────────────
        self.actions_container = QWidget()
        self.actions_container.setStyleSheet("background: transparent;")
        ac_layout = QHBoxLayout(self.actions_container)
        ac_layout.setContentsMargins(0, 0, 0, 0)
        ac_layout.setSpacing(4)

        # ACTIONS section label
        self.lbl_actions = QLabel("ACTIONS")
        self.lbl_actions.setProperty("role", "dim")
        self.lbl_actions.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; background: transparent; padding: 0 4px; letter-spacing: 0.8px;")
        ac_layout.addWidget(self.lbl_actions)

        # Compile (Green)
        self.btn_compile = _make_action_btn("⚙ Compile", "Compile sketch (Ctrl+R)", "btn-compile",
                                            "#1a5c3a", "#216e46")
        self.btn_compile.clicked.connect(self._do_compile)
        ac_layout.addWidget(self.btn_compile)

        # Upload (Blue/Purple)
        self.btn_upload = _make_action_btn("⚡ Upload", "Compile & Upload (Ctrl+U)", "btn-upload",
                                           "#1a4a6e", "#1f5a88")
        self.btn_upload.clicked.connect(self._do_upload)
        ac_layout.addWidget(self.btn_upload)

        # Stop (Red)
        self.btn_stop = _make_action_btn("■ Stop", "Cancel current operation", "btn-stop",
                                         "#6e2020", "#882828")
        self.btn_stop.clicked.connect(self._do_stop)
        self.btn_stop.setEnabled(False)
        ac_layout.addWidget(self.btn_stop)

        # Clean (Dark Gray)
        self.btn_clean = _make_action_btn("🧹 Clean", "Clean build cache", "btn-clean",
                                          "#2d3748", "#3a4a60")
        self.btn_clean.clicked.connect(self._do_clean)
        ac_layout.addWidget(self.btn_clean)

        # Separator between build actions and file actions
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setStyleSheet("border: none; border-left: 1px solid #2d3748; margin: 4px 5px;")
        ac_layout.addWidget(sep)
        self._action_sep = sep

        # Save (Green)
        self.btn_save = _make_action_btn("💾 Save", "Save current file (Ctrl+S)", "btn-save",
                                         "#1a5c3a", "#216e46")
        self.btn_save.clicked.connect(self._do_save)
        ac_layout.addWidget(self.btn_save)

        # Save All (Blue)
        self.btn_save_all = _make_action_btn("💾 Save All", "Save all open files (Ctrl+Shift+S)", "btn-save-all",
                                             "#1a4a6e", "#1f5a88")
        self.btn_save_all.clicked.connect(self._do_save_all)
        ac_layout.addWidget(self.btn_save_all)

        # Reload (Dark Gray)
        self.btn_reload = _make_action_btn("↺ Reload", "Reload current file from disk", "btn-reload",
                                           "#2d3748", "#3a4a60")
        self.btn_reload.clicked.connect(self._do_reload)
        ac_layout.addWidget(self.btn_reload)

        # Modify (Teal)
        self.btn_modify = _make_action_btn("🛠 Modify", "Add, rename, or delete project files", "btn-modify",
                                           "#1a7a70", "#219a8d")
        self.btn_modify.clicked.connect(self._do_modify)
        ac_layout.addWidget(self.btn_modify)

        # Compact Actions dropdown button (visible when width < 1200)
        self.btn_actions_dropdown = _make_action_btn(
            "Actions ▾",
            "More actions (Stop, Clean, Save, Reload, Modify)",
            "btn-actions-dropdown",
            "#2d3748", "#3a4a60"
        )
        self.btn_actions_dropdown.setMinimumWidth(100)
        self.btn_actions_dropdown.clicked.connect(self._toggle_actions_menu)
        self.btn_actions_dropdown.setVisible(False)
        ac_layout.addWidget(self.btn_actions_dropdown)

        self._actions_popup = None
        self._is_compact = False

        self.addWidget(self.actions_container)

        # ── 4. Right Expanding Spacer ───────────────────────────────────────
        self.sp_right = QWidget()
        self.sp_right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.addWidget(self.sp_right)

        # ── 5. Right Container (Sketch path + Project + Download) ────────────
        self.right_container = QWidget()
        self.right_container.setStyleSheet("background: transparent;")
        rc_layout = QHBoxLayout(self.right_container)
        rc_layout.setContentsMargins(0, 0, 0, 0)
        rc_layout.setSpacing(4)

        # Sketch path label
        self.lbl_sketch_icon = QLabel("📁")
        self.lbl_sketch_icon.setStyleSheet("color: #56cfbf; font-size: 13px; font-family: 'Segoe UI Emoji', sans-serif; cursor: hand; background: transparent;")
        self.lbl_sketch_icon.setToolTip("Click to select or create a project")
        self.lbl_sketch_icon.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_sketch_icon.mousePressEvent = lambda e: self._on_new_project()
        rc_layout.addWidget(self.lbl_sketch_icon)

        self.lbl_sketch = QLabel("(no project)")
        self.lbl_sketch.setStyleSheet("color: #6b7280; font-size: 12px; font-family: Consolas; background: transparent;")
        self.lbl_sketch.setToolTip("Current sketch folder — left-click: open in Explorer • right-click: change project")
        self.lbl_sketch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_sketch.mousePressEvent = self._on_sketch_label_click
        rc_layout.addWidget(self.lbl_sketch)

        # Project
        self.btn_project = _make_action_btn("📁 Project", "Open or Create Project (Ctrl+O)", "btn-project",
                                            "#2d3748", "#3a4a60")
        self.btn_project.clicked.connect(self._on_new_project)
        rc_layout.addWidget(self.btn_project)

        # Download Boards/Libs button
        self.btn_download = _make_action_btn("⬇ Download Boards/Libraries",
                                             "Download boards and libraries",
                                             "btn-download", "#2d3748", "#3a4a60")
        self.btn_download.clicked.connect(self._open_download_manager)
        rc_layout.addWidget(self.btn_download)

        self.addWidget(self.right_container)

        # Apply initial button gating on startup (both board and port are empty)
        self._update_action_button_states()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._balance_spacers()

    def _balance_spacers(self) -> None:
        """Dynamically position the left spacer to keep Actions centered."""
        if not hasattr(self, "actions_container") or not hasattr(self, "logo") or not hasattr(self, "right_container"):
            return
        tb_w = self.width()
        pad_left = 12
        spacing = 4
        left_w = self.logo.sizeHint().width()
        right_w = self.right_container.sizeHint().width()
        center_w = self.actions_container.sizeHint().width()

        target_center_x = (tb_w - center_w) / 2.0
        min_left_space = pad_left + left_w + spacing + 8
        min_right_space = pad_left + right_w + spacing + 8

        avail_for_spacer = tb_w - (pad_left + left_w + center_w + right_w + 32)
        if avail_for_spacer <= 8:
            self.sp_left.setFixedWidth(8)
            self.sp_right.setFixedWidth(8)
            return

        if target_center_x >= min_left_space and (target_center_x + center_w) <= (tb_w - min_right_space):
            # Perfect mathematical center
            sp_left_w = int(target_center_x - pad_left - left_w - spacing)
            sp_left_w = min(sp_left_w, max(8, avail_for_spacer))
            self.sp_left.setFixedWidth(max(8, sp_left_w))
        elif (target_center_x + center_w) > (tb_w - min_right_space) and (tb_w - min_right_space - center_w) >= min_left_space:
            # Optimal shift near center without pushing right items
            start_x = tb_w - min_right_space - center_w
            sp_left_w = int(start_x - pad_left - left_w - spacing)
            sp_left_w = min(sp_left_w, max(8, avail_for_spacer))
            self.sp_left.setFixedWidth(max(8, sp_left_w))
        else:
            # Narrow window: allow sp_left to shrink
            self.sp_left.setMinimumWidth(8)
            self.sp_left.setMaximumWidth(max(8, avail_for_spacer))

        # sp_right flexibly absorbs remaining space to push right_container flush right
        self.sp_right.setMinimumWidth(8)
        self.sp_right.setMaximumWidth(16777215)

    def _add_separator(self) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setStyleSheet("border: none; border-left: 1px solid #2d3748; margin: 4px 5px;")
        self.addWidget(sep)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _do_compile(self) -> None:
        if self._backend and not self._backend.is_busy:
            mw = self.window()
            if hasattr(mw, "_editor_panel") and mw._editor_panel:
                mw._editor_panel.trigger_save_all(callback=self._backend.compile_sketch)
            else:
                self._backend.compile_sketch()

    def _do_upload(self) -> None:
        if self._backend and not self._backend.is_busy:
            mw = self.window()
            if hasattr(mw, "_editor_panel") and mw._editor_panel:
                mw._editor_panel.trigger_save_all(callback=self._backend.upload_sketch)
            else:
                self._backend.upload_sketch()

    def _do_stop(self) -> None:
        if self._backend:
            op = getattr(self._backend, "active_operation", None)
            phase = getattr(self._backend, "_current_op_phase", None)
            if op in ("flash", "reset") or phase in ("flashing", "writing", "resetting", "erasing"):
                return
            self.btn_stop.setEnabled(False)
            self.btn_stop.setText("■ Stopping...")
            self._backend.stop_operation()

    def _do_clean(self) -> None:
        if not self._backend or self._backend.is_busy:
            return
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self.window(),
            "Clear All Board Build Caches?",
            "Clean will remove generated project configuration and ALL cached "
            "builds for every board used with this sketch.\n\n"
            "The app-wide Hard/Soft Reset board caches shared by all sketches "
            "and windows, plus legacy compiled artifacts, will also be cleared. "
            "The next Compile, Upload, Hard "
            "Reset, or Soft Reset for those boards may need a first-time rebuild.\n\n"
            "Your .ino/.cpp/.c/.h source files, other user files, and shared "
            "PlatformIO frameworks/toolchains will NOT be removed.\n\n"
            "Continue with Clean?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self._backend.clean_cache()

    def _do_reload(self) -> None:
        mw = self.window()
        if hasattr(mw, "_shortcut_reload_file"):
            mw._shortcut_reload_file()
        elif hasattr(mw, "_editor_panel"):
            mw._editor_panel.trigger_reload()

    def _is_busy(self) -> bool:
        if self._backend and (self._backend.is_busy or getattr(self._backend, "active_operation", None) is not None):
            return True
        mw = self.window()
        if mw and getattr(mw, "_active_operation", None) is not None:
            return True
        return False

    def _do_modify(self) -> None:
        if self._is_busy():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self.window(),
                "Action in Progress",
                "Modifying project files is not allowed while an action is in progress.\n\n"
                "Please wait for the current action to finish or stop it first.",
            )
            return
        mw = self.window()
        if hasattr(mw, "_open_modify_files_dialog"):
            mw._open_modify_files_dialog()
        else:
            from main.qt.modify_dialog import ModifyFilesDialog
            dlg = ModifyFilesDialog(self._backend, parent=self.window())
            dlg.exec()

    def _do_save(self) -> None:
        mw = self.window()
        if hasattr(mw, "_shortcut_save"):
            mw._shortcut_save()
        elif hasattr(mw, "_editor_panel"):
            mw._editor_panel.trigger_save()

    def _do_save_all(self) -> None:
        mw = self.window()
        if hasattr(mw, "_shortcut_save_all"):
            mw._shortcut_save_all()
        elif hasattr(mw, "_editor_panel"):
            mw._editor_panel.trigger_save_all()

    def _on_sketch_label_click(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            if self._is_busy():
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self.window(),
                    "Action in Progress",
                    "Changing project is not allowed while an action is in progress.\n\n"
                    "Please wait for the current action to finish or stop it first.",
                )
                return
            self._on_new_project()
        elif self._backend:
            self._backend.open_in_explorer()

    def _on_new_project(self) -> None:
        if self._is_busy():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self.window(),
                "Action in Progress",
                "Changing project is not allowed while an action is in progress.\n\n"
                "Please wait for the current action to finish or stop it first.",
            )
            return
        from main.qt.project_dialog import ProjectDialog
        dlg = ProjectDialog(self._backend, parent=self.window())
        dlg.exec()

    def _open_download_manager(self) -> None:
        from main.qt.download_dialog import launch_download_manager
        launch_download_manager(parent=self.window())

    def update_sketch_label(self, path: str) -> None:
        self._current_sketch_path = path
        p = Path(path)
        text = p.name if p.name else path
        if len(text) > 40:
            text = "…" + text[-38:]
        self.lbl_sketch.setText(text)
        if self._is_busy():
            self.lbl_sketch.setToolTip("Current sketch folder (changing project is not allowed during actions)")
        else:
            self.lbl_sketch.setToolTip(f"{path} — left-click: open in Explorer • right-click: change project" if path else "Current sketch folder — left-click: open in Explorer • right-click: change project")
        self._balance_spacers()

    @Slot(dict)
    def on_operation_phase(self, payload: dict) -> None:
        """Update button states and labels based on operation phase.

        Matches LATEST-WORKING-MCU- FLASHER layout_panes_mixin:
        - STOP: enabled during compile and initial build phase.
          DISABLED during flash/reset (direct flash write — brick risk).
        - Action button text reflects the running phase with visual spinners/icons.
        """
        is_busy: bool = payload.get("is_busy", False)
        phase: str = str(payload.get("phase", "")).lower()
        op: str = str(payload.get("op", "")).lower()
        can_stop: bool = bool(payload.get("can_stop", True))

        if is_busy:
            self.btn_compile.setEnabled(False)
            self.btn_compile.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_upload.setEnabled(False)
            self.btn_upload.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_clean.setEnabled(False)
            self.btn_clean.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_modify.setEnabled(False)
            self.btn_modify.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_project.setEnabled(False)
            self.btn_project.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_project.setToolTip("Changing project is not allowed while an action is in progress")
            self.lbl_sketch_icon.setEnabled(False)
            self.lbl_sketch_icon.setCursor(Qt.CursorShape.ArrowCursor)
            self.lbl_sketch_icon.setToolTip("Changing project is not allowed while an action is in progress")
            if hasattr(self, "lbl_sketch"):
                self.lbl_sketch.setToolTip("Current sketch folder (changing project is not allowed during actions)")

            # STOP button: enabled during compile and build.
            # DISABLED during flash/reset (direct flash write — brick risk) and generic fallback.
            is_flash_or_reset = (
                phase in ("flash", "flashing", "reset", "resetting", "hard_reset", "soft_reset")
                or op in ("flash", "reset", "hard_reset", "soft_reset")
                or not can_stop
            )
            if is_flash_or_reset:
                self.btn_stop.setEnabled(False)
                self.btn_stop.setCursor(Qt.CursorShape.ArrowCursor)
            else:
                self.btn_stop.setEnabled(True)
                self.btn_stop.setText("■ Stop")
                self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)

            # Visual labels so user knows which op is active
            if phase == "compile" or (op == "compile" and phase not in ("flash", "flashing")):
                if op == "upload":
                    self.btn_compile.setText("⚙ Compile")
                    self.btn_upload.setText("⚙ Building...")
                else:
                    self.btn_compile.setText("⚙ Compiling...")
                    self.btn_upload.setText("⚡ Upload")
            elif phase in ("flash", "flashing", "upload") or op in ("upload", "flash"):
                self.btn_compile.setText("⚙ Compile")
                self.btn_upload.setText("⚡ Uploading...")
            elif phase in ("reset", "resetting", "hard_reset", "soft_reset") or op in ("reset", "hard_reset", "soft_reset"):
                self.btn_compile.setText("⚙ Compile")
                if op == "hard_reset" or phase == "hard_reset":
                    self.btn_upload.setText("⚡ Hard Resetting...")
                elif op == "soft_reset" or phase == "soft_reset":
                    self.btn_upload.setText("⚡ Soft Resetting...")
                else:
                    self.btn_upload.setText("⚡ Resetting...")
            elif phase in ("clean", "cleaning") or op == "clean":
                self.btn_compile.setText("⚙ Compile")
                self.btn_upload.setText("⚡ Upload")
                self.btn_clean.setText("🧹 Cleaning...")
        else:
            self.btn_compile.setText("⚙ Compile")
            self.btn_upload.setText("⚡ Upload")
            self.btn_stop.setText("■ Stop")
            self.btn_clean.setText("🧹 Clean")
            self.btn_stop.setEnabled(False)
            self.btn_stop.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_clean.setEnabled(True)
            self.btn_clean.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_modify.setEnabled(True)
            self.btn_modify.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_project.setEnabled(True)
            self.btn_project.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_project.setToolTip("Open or Create Project (Ctrl+O)")
            self.lbl_sketch_icon.setEnabled(True)
            self.lbl_sketch_icon.setCursor(Qt.CursorShape.PointingHandCursor)
            self.lbl_sketch_icon.setToolTip("Click to select or create a project")
            if hasattr(self, "lbl_sketch"):
                cur_path = getattr(self, "_current_sketch_path", "")
                if cur_path:
                    self.lbl_sketch.setToolTip(f"{cur_path} — left-click: open in Explorer • right-click: change project")
                else:
                    self.lbl_sketch.setToolTip("Current sketch folder — left-click: open in Explorer • right-click: change project")
            # Delegate to action button gating (board/port awareness)
            self._update_action_button_states()

    def _update_action_button_states(self) -> None:
        """Gate Compile and Upload buttons on board/port selection.

        Matches stable hardware_port_mixin._update_hardware_action_buttons:
        - Compile: enabled when a board is selected (port irrelevant).
        - Upload: enabled when BOTH a board and port are selected.
        """
        if self._backend and self._backend.is_busy:
            return  # operation state machine owns buttons right now

        board_selected = bool(self._backend.current_board) if self._backend else False
        port_selected = bool(self._backend.current_port) if self._backend else False

        self.btn_compile.setEnabled(board_selected)
        self.btn_compile.setCursor(Qt.CursorShape.PointingHandCursor if board_selected else Qt.CursorShape.ArrowCursor)
        self.btn_upload.setEnabled(board_selected and port_selected)
        self.btn_upload.setCursor(Qt.CursorShape.PointingHandCursor if (board_selected and port_selected) else Qt.CursorShape.ArrowCursor)
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor if self.btn_stop.isEnabled() else Qt.CursorShape.ArrowCursor)

    def connect_signals(self, sig_bus) -> None:
        sig_bus.operation_phase.connect(self.on_operation_phase)
        sig_bus.project_updated.connect(
            lambda p: self.update_sketch_label(p.get("path", ""))
        )
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)

    def apply_theme(self, theme_name: str) -> None:
        from main.core.theme import Theme
        if hasattr(self, "logo"):
            self.logo.setStyleSheet(
                f"color: {Theme.CYAN}; font-size: 15px; font-weight: 700; font-family: 'Montserrat', 'Segoe UI', sans-serif; background: transparent;"
            )
        if hasattr(self, "lbl_sketch_icon"):
            self.lbl_sketch_icon.setStyleSheet(
                f"color: {Theme.CYAN}; font-size: 13px; font-family: 'Segoe UI Emoji', sans-serif; cursor: hand; background: transparent;"
            )
        if hasattr(self, "lbl_sketch"):
            self.lbl_sketch.setStyleSheet(
                f"color: {Theme.TEXT_DIM}; font-size: 12px; font-family: Consolas; background: transparent;"
            )
        self.update()

    def is_compact(self) -> bool:
        return getattr(self, "_is_compact", False)

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt primary toolbar actions, logo, and download button based on width."""
        compact = width < 1200
        try:
            if not getattr(self, "_wide_req_width", 0):
                self._wide_req_width = self.actions_container.sizeHint().width()
            side_req = self.logo.sizeHint().width() + self.right_container.sizeHint().width()
            title_available = max(0, width - side_req)
            if title_available < (self._wide_req_width + 24):
                compact = True
        except Exception:
            pass

        self._is_compact = compact
        if compact:
            if hasattr(self, "lbl_actions"):
                self.lbl_actions.setVisible(False)
            self.btn_stop.setVisible(False)
            self.btn_clean.setVisible(False)
            if hasattr(self, "_action_sep"):
                self._action_sep.setVisible(False)
            self.btn_save.setVisible(False)
            self.btn_save_all.setVisible(False)
            self.btn_reload.setVisible(False)
            self.btn_modify.setVisible(False)
            self.btn_actions_dropdown.setVisible(True)

            if width < 1000:
                self.lbl_sketch.setVisible(False)
                self.btn_download.setText("⬇")
                self.btn_download.setToolTip("Download Boards and Libraries")
                self.btn_download.setFixedWidth(28)
                if width < 520:
                    self.logo.setText("⚡ MCU")
                elif width < 680:
                    self.logo.setText("⚡ MCU Flasher")
                else:
                    self.logo.setText("⚡ MCU Flasher by Naph")
            elif width < 1250:
                self.lbl_sketch.setVisible(True)
                self.btn_download.setText("⬇ Download")
                self.btn_download.setToolTip("Download boards and libraries")
                self.btn_download.setMinimumWidth(0)
                self.btn_download.setMaximumWidth(16777215)
                self.logo.setText("⚡ MCU Flasher by Naph")
            else:
                self.lbl_sketch.setVisible(True)
                self.btn_download.setText("⬇ Download Boards/Libraries")
                self.btn_download.setToolTip("Download boards and libraries")
                self.btn_download.setMinimumWidth(0)
                self.btn_download.setMaximumWidth(16777215)
                self.logo.setText("⚡ MCU Flasher by Naph")
        else:
            if hasattr(self, "lbl_actions"):
                self.lbl_actions.setVisible(True)
            self.btn_stop.setVisible(True)
            self.btn_clean.setVisible(True)
            if hasattr(self, "_action_sep"):
                self._action_sep.setVisible(True)
            self.btn_save.setVisible(True)
            self.btn_save_all.setVisible(True)
            self.btn_reload.setVisible(True)
            self.btn_modify.setVisible(True)
            self.btn_actions_dropdown.setVisible(False)
            if hasattr(self, "_actions_popup") and self._actions_popup and self._actions_popup.isVisible():
                self._actions_popup.close()
                self._actions_popup = None

            self.lbl_sketch.setVisible(True)
            self.btn_download.setText("⬇ Download Boards/Libraries")
            self.btn_download.setToolTip("Download boards and libraries")
            self.btn_download.setMinimumWidth(0)
            self.btn_download.setMaximumWidth(16777215)
            self.logo.setText("⚡ MCU Flasher by Naph")

        self._balance_spacers()

    def _toggle_actions_menu(self) -> None:
        """Show popup containing secondary actions that are collapsed in compact mode."""
        if hasattr(self, "_actions_popup") and self._actions_popup and self._actions_popup.isVisible():
            self._actions_popup.close()
            self._actions_popup = None
            return

        popup = CompactDropdownPopup(self)
        from main.core.theme import Theme

        # Stop (Red)
        popup.add_button(
            self.btn_stop.text() or "■ Stop",
            self._do_stop,
            Theme.BTN_STOP,
            Theme.BTN_STOP_H,
            enabled=self.btn_stop.isEnabled(),
        )
        # Clean (Dark Gray)
        popup.add_button(
            "🧹 Clean",
            self._do_clean,
            Theme.BTN_CLEAR,
            Theme.BTN_CLEAR_H,
            enabled=self.btn_clean.isEnabled(),
        )
        # Save (Green)
        popup.add_button(
            "💾 Save",
            self._do_save,
            Theme.BTN_COMPILE,
            Theme.BTN_COMPILE_H,
            enabled=self.btn_save.isEnabled(),
        )
        # Save All (Blue)
        popup.add_button(
            "💾 Save All",
            self._do_save_all,
            Theme.BTN_UPLOAD,
            Theme.BTN_UPLOAD_H,
            enabled=self.btn_save_all.isEnabled(),
        )
        # Reload (Dark Gray)
        popup.add_button(
            "↺ Reload",
            self._do_reload,
            Theme.BTN_CLEAR,
            Theme.BTN_CLEAR_H,
            enabled=self.btn_reload.isEnabled(),
        )
        # Modify (Teal)
        popup.add_button(
            "🛠 Modify",
            self._do_modify,
            Theme.BTN_MONITOR,
            Theme.BTN_MONITOR_H,
            enabled=self.btn_modify.isEnabled(),
        )

        popup.show_below(self.btn_actions_dropdown, min_width=max(100, self.btn_actions_dropdown.width()), alignment="center")
        self._actions_popup = popup


class MarqueeComboBox(QComboBox):
    """
    QComboBox with smooth text marquee sliding when the text overflows.
    Mirrors the behavior of self._start_marquee() in the stable reference.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._offset = 0
        self._dir = 1
        self._pause = 30  # initial pause ~1.2s
        self._is_popup_open = False

        self._timer = QTimer(self)
        self._timer.setInterval(40)  # ~25 FPS
        self._timer.timeout.connect(self._step)
        self._timer.start()

        self.currentIndexChanged.connect(self._on_index_changed)
        view = self.view()
        if view:
            view.setTextElideMode(Qt.TextElideMode.ElideNone)

    def _on_index_changed(self) -> None:
        self._offset = 0
        self._dir = 1
        self._pause = 30
        self.update()

    def showPopup(self) -> None:
        self._is_popup_open = True
        self._offset = 0
        view = self.view()
        if view:
            view.setTextElideMode(Qt.TextElideMode.ElideNone)
            fm = self.fontMetrics()
            max_w = self.width()
            for i in range(self.count()):
                t = self.itemText(i)
                item_w = fm.horizontalAdvance(t) + 48
                if item_w > max_w:
                    max_w = item_w
            view.setMinimumWidth(max(self.width(), max_w))
        super().showPopup()

    def hidePopup(self) -> None:
        self._is_popup_open = False
        self._offset = 0
        self._dir = 1
        self._pause = 30
        super().hidePopup()

    def sizeHint(self) -> QSize:
        sh = super().sizeHint()
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        fm = opt.fontMetrics
        text = opt.currentText or (self.placeholderText() if self.currentIndex() < 0 else "")
        tw = fm.horizontalAdvance(text) if text else 90
        w = max(self.minimumWidth(), tw + 42)
        return QSize(min(self.maximumWidth(), w), max(26, sh.height()))

    def minimumSizeHint(self) -> QSize:
        sh = super().minimumSizeHint()
        return QSize(self.minimumWidth(), max(26, sh.height()))

    def resizeEvent(self, event) -> None:
        self._offset = 0
        self._dir = 1
        self._pause = 30
        super().resizeEvent(event)

    def _step(self) -> None:
        if self._is_popup_open or not self.isEnabled():
            return
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt,
            QStyle.SubControl.SC_ComboBoxEditField, self
        )
        avail = max(10, rect.width() - 8)
        fm = opt.fontMetrics
        text = opt.currentText or (self.placeholderText() if self.currentIndex() < 0 else "")
        if not text:
            return
        text_w = fm.horizontalAdvance(text)
        overflow = text_w - avail
        if overflow <= 0:
            if self._offset != 0:
                self._offset = 0
                self.update()
            return

        if self._pause > 0:
            self._pause -= 1
            return

        self._offset += self._dir
        if self._offset >= overflow:
            self._offset = overflow
            self._dir = -1
            self._pause = 30  # pause at right end (~1.2s)
        elif self._offset <= 0:
            self._offset = 0
            self._dir = 1
            self._pause = 30  # pause at left start (~1.2s)
        self.update()

    def paintEvent(self, event) -> None:
        p = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        text = opt.currentText
        is_placeholder = False
        if not text and self.currentIndex() < 0 and self.placeholderText():
            text = self.placeholderText()
            is_placeholder = True

        # Clear text so standard control doesn't paint elided text with an ellipsis
        opt.currentText = ""
        p.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)

        rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt,
            QStyle.SubControl.SC_ComboBoxEditField, self
        )
        text_rect = rect.adjusted(4, 0, -4, 0)
        p.setClipRect(text_rect)
        fm = opt.fontMetrics
        p.setFont(self.font())
        from main.core.theme import Theme
        if is_placeholder:
            color = QColor(Theme.TEXT_DIM)
        else:
            color = QColor(Theme.TEXT if self.isEnabled() else Theme.TEXT_DIM)
        p.setPen(color)

        avail = max(10, text_rect.width())
        text_w = fm.horizontalAdvance(text)
        if text_w <= avail:
            p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        else:
            y = text_rect.y() + (text_rect.height() + fm.ascent() - fm.descent()) // 2
            p.drawText(text_rect.x() - self._offset, y, text)


class MarqueeBoardSelector(QWidget):
    """
    Board display widget with marquee sliding text on overflow and click-to-open
    the BoardSearchDialog modal window.
    """
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = ""
        self._offset = 0
        self._dir = 1
        self._pause = 30
        self._is_hovered = False

        self.setFixedHeight(28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to search & select MCU board")

        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._step)
        self._timer.start()

    def set_board(self, name: str) -> None:
        self._text = name or ""
        self._offset = 0
        self._dir = 1
        self._pause = 30
        self.update()

    def setText(self, name: str) -> None:
        self.set_board(name)

    def text(self) -> str:
        return self._text

    def currentText(self) -> str:
        return self._text

    def _get_adaptive_width(self) -> int:
        """Calculate optimal width based on screen dimensions and DPI scaling."""
        screen_w = 1920
        dpi_scale = 1.0
        try:
            screen = self.screen() or QGuiApplication.primaryScreen()
            if screen:
                geom = screen.availableGeometry()
                if geom.width() > 1000:
                    screen_w = geom.width()
                dpi = screen.logicalDotsPerInch()
                if dpi > 0:
                    dpi_scale = max(1.0, dpi / 96.0)
        except Exception:
            pass

        if screen_w <= 1000 and sys.platform == "win32":
            try:
                import ctypes
                w = ctypes.windll.user32.GetSystemMetrics(0)
                if w > 1000:
                    screen_w = w
            except Exception:
                pass

        if screen_w >= 2560:
            base_w = 250
        elif screen_w >= 1920:
            base_w = 215
        elif screen_w >= 1600:
            base_w = 195
        elif screen_w >= 1366:
            base_w = 180
        else:
            base_w = 160

        return int(base_w * dpi_scale)

    def sizeHint(self) -> QSize:
        return QSize(self._get_adaptive_width(), 28)

    def minimumSizeHint(self) -> QSize:
        return QSize(max(150, int(self._get_adaptive_width() * 0.85)), 28)

    def _step(self) -> None:
        if not self.isEnabled() or not self._text:
            return
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self._text)
        avail = max(10, self.width() - 16)
        overflow = text_w - avail
        if overflow <= 0:
            if self._offset != 0:
                self._offset = 0
                self.update()
            return

        if self._pause > 0:
            self._pause -= 1
            return

        self._offset += self._dir
        if self._offset >= overflow:
            self._offset = overflow
            self._dir = -1
            self._pause = 30
        elif self._offset <= 0:
            self._offset = 0
            self._dir = 1
            self._pause = 30
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def enterEvent(self, event) -> None:
        self._is_hovered = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._is_hovered = False
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()

        # Background & Border
        from main.core.theme import Theme
        bg = QColor(Theme.BG_DARKEST)
        border = QColor(Theme.CYAN if self._is_hovered else Theme.BORDER)
        p.setBrush(bg)
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 4, 4)

        inner = r.adjusted(8, 2, -8, -2)
        p.setClipRect(inner)
        display_text = self._text if self._text else "Select Board"
        p.setFont(self.font())
        if not self._text:
            p.setPen(QColor(Theme.TEXT_DIM))
        else:
            p.setPen(QColor(Theme.TEXT if self.isEnabled() else Theme.TEXT_DIM))
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(display_text)
        avail = inner.width()
        if text_w <= avail:
            p.drawText(inner, Qt.AlignmentFlag.AlignCenter, display_text)
        else:
            y = inner.y() + (inner.height() + fm.ascent() - fm.descent()) // 2
            p.drawText(inner.x() - self._offset, y, display_text)


class ControlsBar(QWidget):
    """
    Secondary controls bar: Board, Port, Upload Speed, Options checkboxes,
    view toggles, Settings, AI Assistant.
    Mirrors UILayoutMixin ctrl_frame.
    """

    def __init__(self, backend: "MCUWebBackendAPI", parent: QWidget | None = None):
        super().__init__(parent)
        self._backend = backend
        self.setObjectName("controls-bar")
        self.setFixedHeight(64)
        self._setup_ui()
        self._populate_initial()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(6)

        # ── Board group ───────────────────────────────────────────────────────
        board_group = QVBoxLayout()
        board_group.setSpacing(1)
        lbl_board = QLabel("BOARD")
        lbl_board.setProperty("role", "dim")
        lbl_board.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; background: transparent; letter-spacing: 0.8px;")

        board_row = QHBoxLayout()
        board_row.setSpacing(4)

        self.board_selector = MarqueeBoardSelector(self)
        adaptive_w = self.board_selector._get_adaptive_width()
        self.board_selector.setMinimumWidth(max(180, int(adaptive_w * 0.95)))
        self.board_selector.setMaximumWidth(max(400, int(adaptive_w * 1.6)))
        self.board_selector.clicked.connect(self._open_board_search_dialog)

        self.btn_search_board = QPushButton("🔍")
        self.btn_search_board.setObjectName("btn-search-board")
        self.btn_search_board.setFixedSize(26, 26)
        self.btn_search_board.setToolTip("Search & Select MCU Board")
        self.btn_search_board.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_search_board.clicked.connect(self._open_board_search_dialog)

        board_row.addWidget(self.board_selector)
        board_row.addWidget(self.btn_search_board)

        board_group.addWidget(lbl_board)
        board_group.addLayout(board_row)
        layout.addLayout(board_group)

        # Backward compatibility alias
        self.board_combo = self.board_selector

        # ── Port group ────────────────────────────────────────────────────────
        port_group = QVBoxLayout()
        port_group.setSpacing(1)
        lbl_port = QLabel("PORT")
        lbl_port.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; background: transparent; letter-spacing: 0.8px;")
        port_row = QHBoxLayout()
        port_row.setSpacing(4)
        self.port_combo = MarqueeComboBox(self)
        self.port_combo.setMinimumWidth(180)
        self.port_combo.setMaximumWidth(420)
        self.port_combo.setToolTip("Serial COM port for upload and monitor")
        self.port_combo.setPlaceholderText("Select COM Port")
        self.port_combo.showPopup = self._refresh_ports_and_show  # type: ignore
        self.port_combo.currentIndexChanged.connect(self._on_port_changed)
        port_row.addWidget(self.port_combo)
        port_group.addWidget(lbl_port)
        port_group.addLayout(port_row)
        layout.addLayout(port_group)

        # ── Upload speed group ────────────────────────────────────────────────
        spd_group = QVBoxLayout()
        spd_group.setSpacing(1)
        self.lbl_spd = QLabel("UPLOAD SPD")
        self.lbl_spd.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; background: transparent; letter-spacing: 0.8px;")
        self.upload_speed_combo = QComboBox()
        self.upload_speed_combo.addItems([str(s) for s in UPLOAD_SPEEDS])
        self.upload_speed_combo.setCurrentText(str(DEFAULT_UPLOAD_SPEED))
        self.upload_speed_combo.setFixedWidth(self._get_adaptive_upload_speed_width())
        self.upload_speed_combo.setToolTip("Upload baud rate for flashing firmware")
        self.upload_speed_combo.currentTextChanged.connect(self._on_upload_speed_changed)
        spd_group.addWidget(self.lbl_spd)
        spd_group.addWidget(self.upload_speed_combo)
        layout.addLayout(spd_group)

        # ── Spacer ────────────────────────────────────────────────────────────
        layout.addStretch()

        # ── OPTIONS section ───────────────────────────────────────────────────
        opt_group = QVBoxLayout()
        opt_group.setSpacing(2)
        lbl_opt = QLabel("OPTIONS")
        lbl_opt.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; background: transparent; letter-spacing: 0.8px;")
        opt_row = QHBoxLayout()
        opt_row.setSpacing(3)

        self.cb_timestamp = QCheckBox("Time Stamp")
        self.cb_timestamp.setToolTip("Show timestamps in console and monitor output")
        from main.core.config import load_gui_config
        init_ts = bool(getattr(self._backend, "timestamp_enabled", False)) if (self._backend and hasattr(self._backend, "timestamp_enabled")) else bool(load_gui_config().get("timestamp_enabled", False))
        self.cb_timestamp.setChecked(init_ts)
        self.cb_timestamp.stateChanged.connect(self._on_timestamp_changed)
        opt_row.addWidget(self.cb_timestamp)

        self.cb_skip_compile = QCheckBox("Skip Compile")
        self.cb_skip_compile.setToolTip("Upload without recompiling (use cached firmware)")
        self.cb_skip_compile.setEnabled(False)
        self.cb_skip_compile.stateChanged.connect(self._on_skip_compile_changed)
        opt_row.addWidget(self.cb_skip_compile)

        self.btn_detach_editor = QPushButton("Detach Editor")
        self.btn_detach_editor.setFixedHeight(26)
        self.btn_detach_editor.setObjectName("btn-detach-editor")
        self.btn_detach_editor.setToolTip("Pop out the code editor into an independent floating window")
        self.btn_detach_editor.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_detach_editor.clicked.connect(self._toggle_editor_detachment)
        opt_row.addWidget(self.btn_detach_editor)

        self.btn_toggle_editor = QPushButton("Hide Editor")
        self.btn_toggle_editor.setFixedHeight(26)
        self.btn_toggle_editor.setObjectName("btn-toggle-editor")
        self.btn_toggle_editor.setToolTip("Toggle visibility of the code editor pane")
        self.btn_toggle_editor.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_editor.clicked.connect(self._toggle_editor_pane)
        opt_row.addWidget(self.btn_toggle_editor)

        self.btn_toggle_monitors = QPushButton("Hide Monitors")
        self.btn_toggle_monitors.setFixedHeight(26)
        self.btn_toggle_monitors.setObjectName("btn-toggle-monitors")
        self.btn_toggle_monitors.setToolTip("Toggle visibility of the bottom console and monitor tabs")
        self.btn_toggle_monitors.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_monitors.clicked.connect(self._toggle_monitors_pane)
        opt_row.addWidget(self.btn_toggle_monitors)

        self.btn_settings = QPushButton("⚙ Settings")
        self.btn_settings.setFixedHeight(26)
        self.btn_settings.setObjectName("btn-settings")
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.clicked.connect(self._open_settings)
        opt_row.addWidget(self.btn_settings)

        self.btn_ai = QPushButton("🤖 AI Assistant")
        self.btn_ai.setFixedHeight(26)
        self.btn_ai.setObjectName("btn-ai-assistant")
        self.btn_ai.setCheckable(True)
        self.btn_ai.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ai.clicked.connect(self._toggle_ai_panel)
        opt_row.addWidget(self.btn_ai)

        # Compact Options dropdown button (visible when width < 1350)
        self.btn_opt_dropdown = QPushButton("Options ▾")
        self.btn_opt_dropdown.setObjectName("btn-options-dropdown")
        self.btn_opt_dropdown.setFixedHeight(26)
        self.btn_opt_dropdown.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_opt_dropdown.setToolTip("Additional view options, settings, and AI Assistant")
        self.btn_opt_dropdown.clicked.connect(self._toggle_options_menu)
        self.btn_opt_dropdown.setVisible(False)
        opt_row.addWidget(self.btn_opt_dropdown)

        self._opt_popup = None
        self._is_compact = False

        opt_group.addWidget(lbl_opt, alignment=Qt.AlignmentFlag.AlignHCenter)
        opt_group.addLayout(opt_row)
        layout.addLayout(opt_group)

    def _populate_initial(self) -> None:
        """Populate board and port combos from backend initial state.

        Per stable reference (hardware_port_mixin.py:415), board and port
        are always empty on launch — user must explicitly select them.
        """
        if not self._backend:
            return

        # Board always starts empty / unconfigured on launch (per user requirement)
        self.board_selector.set_board("")
        if self._backend:
            self._backend.current_board = ""

        # Populate port dropdown but don't auto-select
        self._refresh_ports()
        if not self._backend.current_port:
            self.port_combo.setCurrentIndex(-1)
        else:
            idx = self.port_combo.findData(self._backend.current_port)
            if idx < 0:
                idx = self.port_combo.findText(self._backend.current_port)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
            else:
                self.port_combo.setCurrentIndex(-1)

        # Apply initial button gating
        self._update_action_button_states_on_controls()

        # Synchronize skip compile availability based on whether project is already compiled
        can_skip = self._backend.check_can_skip_compile() if getattr(self._backend, "current_board", "") else False
        self.cb_skip_compile.setEnabled(can_skip)
        self.cb_skip_compile.setChecked(can_skip)
        if self._backend:
            self._backend.set_skip_compile(can_skip)

    def _open_board_search_dialog(self) -> None:
        """Open the BoardSearchDialog modal matching the stable release."""
        if self._backend and self._backend.is_busy:
            return
        from main.qt.board_dialog import BoardSearchDialog
        from main.core.board_catalog import SUPPORTED_BOARDS

        current = self.board_selector.text() or (self._backend.current_board if self._backend else "")
        dlg = BoardSearchDialog(
            parent=self.window(),
            current_board=current,
            board_list=sorted(SUPPORTED_BOARDS.keys()),
            on_select_callback=self._select_board_from_dialog,
        )
        dlg.exec()

    def _select_board_from_dialog(self, selected_board: str) -> None:
        if not selected_board:
            return
        from main.core.config import add_recent_board
        add_recent_board(selected_board)
        self.board_selector.set_board(selected_board)
        self._on_board_changed(selected_board)

    def _update_hardware_defaults_for_board(self, board_name: str) -> None:
        """Apply board family defaults for monitor baud and upload speed."""
        from main.core.board_catalog import SUPPORTED_BOARDS
        from main.core.constants import default_monitor_baud, board_reset_capabilities

        b_info = SUPPORTED_BOARDS.get(board_name)
        if not b_info:
            for k, v in SUPPORTED_BOARDS.items():
                if k.lower() == board_name.lower():
                    b_info = v
                    break
        b_info = b_info or {}

        fam = board_reset_capabilities(
            b_info.get("platform", ""),
            b_info.get("board", ""),
            board_name,
            b_info.get("framework", ""),
        ).get("family")

        # Upload speed configuration
        if fam == "atmelavr":
            self.upload_speed_combo.setCurrentText("115200")
            self.upload_speed_combo.setEnabled(False)
        elif fam in {"espressif32", "espressif8266"}:
            self.upload_speed_combo.setEnabled(True)
            pref_spd = str(getattr(self._backend, "upload_speed", "") or "460800") if self._backend else "460800"
            if self.upload_speed_combo.findText(pref_spd) >= 0:
                self.upload_speed_combo.setCurrentText(pref_spd)
            else:
                self.upload_speed_combo.setCurrentText("460800")
        else:
            self.upload_speed_combo.setEnabled(True)
            self.upload_speed_combo.setCurrentText(str(DEFAULT_UPLOAD_SPEED))

        # Default monitor baud rate configuration
        mon_baud = default_monitor_baud(
            b_info.get("platform", ""),
            b_info.get("board", ""),
            board_name,
        )
        if self._backend:
            try:
                self._backend.set_baud_rate(int(mon_baud))
            except Exception:
                pass

    def _refresh_ports(self) -> None:
        if not self._backend:
            return
        ports = self._backend._scan_ports()
        self.on_ports_updated(ports)

    def _refresh_ports_and_show(self) -> None:
        self._refresh_ports()
        MarqueeComboBox.showPopup(self.port_combo)

    def _on_board_changed(self, board_name: str) -> None:
        if self._backend and board_name:
            self._backend.select_board(board_name)
        self._update_hardware_defaults_for_board(board_name)
        self._update_action_button_states_on_controls()

    def _on_port_changed(self, index: int) -> None:
        if not self._backend:
            return
        port = self.port_combo.currentData() or ""
        self._backend.select_port(port)
        self._update_action_button_states_on_controls()

    def _on_upload_speed_changed(self, speed: str) -> None:
        if self._backend and speed:
            self._backend.set_upload_speed(speed)

    def _on_timestamp_changed(self, state: int) -> None:
        enabled = bool(state)
        if self._backend and hasattr(self._backend, "set_timestamp_enabled"):
            self._backend.set_timestamp_enabled(enabled)
        else:
            from main.core.config import load_gui_config, save_gui_config
            cfg = load_gui_config()
            cfg["timestamp_enabled"] = enabled
            save_gui_config(cfg)
            from main.qt.signals import signals
            if hasattr(signals, "timestamp_toggled"):
                signals.timestamp_toggled.emit(enabled)

    def _on_skip_compile_changed(self, state: int) -> None:
        if self._backend:
            self._backend.set_skip_compile(bool(state))

    def _update_action_button_states_on_controls(self) -> None:
        """Notify the PrimaryToolbar to refresh compile/upload gating."""
        mw = self.window()
        if mw and hasattr(mw, '_primary_toolbar'):
            mw._primary_toolbar._update_action_button_states()

    def _open_settings(self) -> None:
        from main.qt.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self._backend, parent=self.window())
        dlg.exec()

    def _toggle_ai_panel(self, checked: bool) -> None:
        self.btn_ai.setText("🤖 Hide AI" if checked else "🤖 AI Assistant")
        mw = self.window()
        if hasattr(mw, "toggle_ai_panel"):
            mw.toggle_ai_panel(checked)

    def set_ai_panel_visible(self, visible: bool) -> None:
        self.btn_ai.blockSignals(True)
        self.btn_ai.setChecked(visible)
        self.btn_ai.setText("🤖 Hide AI" if visible else "🤖 AI Assistant")
        self.btn_ai.blockSignals(False)

    def _toggle_editor_detachment(self) -> None:
        mw = self.window()
        if hasattr(mw, "toggle_editor_detachment"):
            mw.toggle_editor_detachment()

    def _toggle_editor_pane(self) -> None:
        mw = self.window()
        if hasattr(mw, "toggle_editor_pane"):
            mw.toggle_editor_pane()

    def _toggle_monitors_pane(self) -> None:
        mw = self.window()
        if hasattr(mw, "toggle_monitors_pane"):
            mw.toggle_monitors_pane()

    def set_editor_detached(self, detached: bool) -> None:
        from main.core.theme import Theme
        if detached:
            self.btn_detach_editor.setText("Attach Editor")
            self.btn_detach_editor.setStyleSheet(
                "QPushButton#btn-detach-editor:enabled {"
                f"  background-color: {Theme.ORANGE};"
                "  color: #ffffff;"
                f"  border: 1px solid {Theme.BORDER};"
                "  border-radius: 4px;"
                "  padding: 3px 8px;"
                "  font-size: 11px;"
                "  font-weight: 600;"
                "}"
                "QPushButton#btn-detach-editor:enabled:hover {"
                f"  background-color: {Theme.ORANGE};"
                f"  border: 1px solid {Theme.CYAN};"
                "  color: #ffffff;"
                "}"
                "QPushButton#btn-detach-editor:enabled:pressed {"
                f"  background-color: {Theme.BG_MID};"
                f"  border: 1px solid {Theme.CYAN};"
                "  color: #ffffff;"
                "}"
                "QPushButton#btn-detach-editor:disabled {"
                f"  background-color: {Theme.BG_DARK};"
                f"  color: {Theme.TEXT_DIM};"
                f"  border: 1px solid {Theme.BORDER};"
                "}"
            )
        else:
            self.btn_detach_editor.setText("Detach Editor")
            self.btn_detach_editor.setStyleSheet("")

    def set_editor_visible(self, visible: bool) -> None:
        self.btn_toggle_editor.setText("Hide Editor" if visible else "Show Editor")

    def set_monitors_visible(self, visible: bool) -> None:
        self.btn_toggle_monitors.setText("Hide Monitors" if visible else "Show Monitors")

    @Slot(list)
    def on_ports_updated(self, ports: list) -> None:
        """Refresh port combo from signal, preserving selection if still present."""
        current_port = self.port_combo.currentData() or (self._backend.current_port if self._backend else "")

        # Diff against existing items to prevent closing an open popup or unnecessary redraws
        existing_items = []
        for i in range(self.port_combo.count()):
            d = self.port_combo.itemData(i)
            if d:
                existing_items.append((d, self.port_combo.itemText(i)))
        new_items = [(p["device"], f"{p['device']}  -  {p['description']}") for p in ports]

        if existing_items == new_items and existing_items:
            if current_port:
                idx = self.port_combo.findData(current_port)
                if idx >= 0 and self.port_combo.currentIndex() != idx:
                    self.port_combo.blockSignals(True)
                    self.port_combo.setCurrentIndex(idx)
                    self.port_combo.blockSignals(False)
            self._update_action_button_states_on_controls()
            return

        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        restore_idx = -1
        if ports:
            for i, p in enumerate(ports):
                label = f"{p['device']}  -  {p['description']}"
                self.port_combo.addItem(label, userData=p["device"])
                if current_port and p["device"] == current_port:
                    restore_idx = i

            # If current_port was not set, check if previously saved config port is present
            if restore_idx < 0 and not current_port and self._backend:
                from main.core.config import load_gui_config
                cfg_port = load_gui_config().get("selected_port", "")
                if cfg_port:
                    for i, p in enumerate(ports):
                        if p["device"] == cfg_port:
                            restore_idx = i
                            break

            if restore_idx >= 0:
                self.port_combo.setCurrentIndex(restore_idx)
                if self._backend and not self._backend.current_port:
                    self._backend.select_port(self.port_combo.itemData(restore_idx))
            else:
                # The previously selected port is no longer present — clear it
                # so the Upload button correctly disables.
                self.port_combo.setCurrentIndex(-1)
                if self._backend and self._backend.current_port:
                    self._backend.current_port = ""
        else:
            self.port_combo.addItem("No ports detected", userData="")
            self.port_combo.setCurrentIndex(-1)
            if self._backend and self._backend.current_port:
                self._backend.current_port = ""
        self.port_combo.blockSignals(False)
        self._update_action_button_states_on_controls()

    @Slot(dict)
    def on_board_selected(self, payload: dict) -> None:
        board = payload.get("board_name", "")
        if board:
            self.board_selector.set_board(board)
            self._update_hardware_defaults_for_board(board)
            can_skip = self._backend.check_can_skip_compile(board) if self._backend else False
            self.cb_skip_compile.setEnabled(can_skip)
            self.cb_skip_compile.setChecked(can_skip)
            if self._backend:
                self._backend.set_skip_compile(can_skip)
            self._update_action_button_states_on_controls()

    @Slot(bool)
    def on_skip_compile_availability(self, available: bool) -> None:
        self.cb_skip_compile.setEnabled(available)
        self.cb_skip_compile.setChecked(available)
        if self._backend:
            self._backend.set_skip_compile(available)

    @Slot(dict)
    def _on_project_updated(self, payload: dict) -> None:
        """Handle project folder change: refresh board and compile cache state."""
        if not self._backend:
            return
        current = self._backend.current_board or ""
        self.board_selector.set_board(current)
        if current:
            self._update_hardware_defaults_for_board(current)
        can_skip = self._backend.check_can_skip_compile()
        self.cb_skip_compile.setEnabled(can_skip)
        self.cb_skip_compile.setChecked(can_skip)
        if self._backend:
            self._backend.set_skip_compile(can_skip)
        self._update_action_button_states_on_controls()

    def sync_timestamp(self, enabled: bool) -> None:
        if hasattr(self, "cb_timestamp") and self.cb_timestamp.isChecked() != enabled:
            self.cb_timestamp.blockSignals(True)
            self.cb_timestamp.setChecked(enabled)
            self.cb_timestamp.blockSignals(False)

    def connect_signals(self, sig_bus) -> None:
        sig_bus.ports_updated.connect(self.on_ports_updated)
        sig_bus.board_selected.connect(self.on_board_selected)
        if hasattr(sig_bus, "project_updated"):
            sig_bus.project_updated.connect(self._on_project_updated)
        if hasattr(sig_bus, "skip_compile_availability_changed"):
            sig_bus.skip_compile_availability_changed.connect(self.on_skip_compile_availability)
        if hasattr(sig_bus, "timestamp_toggled"):
            sig_bus.timestamp_toggled.connect(self.sync_timestamp)
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)

    def apply_theme(self, theme_name: str) -> None:
        if hasattr(self, "board_selector"):
            self.board_selector.update()
        if hasattr(self, "port_combo"):
            self.port_combo.update()
        self.update()

    def _get_adaptive_upload_speed_width(self, width: int | None = None) -> int:
        """Calculate responsive width for upload speed combobox considering screen dimension, font metrics, and DPI scale."""
        w = width if width is not None else getattr(self, "_current_width", self.width())
        try:
            fm = self.upload_speed_combo.fontMetrics()
            text_w = max(fm.horizontalAdvance(str(s)) for s in UPLOAD_SPEEDS)
        except Exception:
            text_w = 48

        if w >= 1500:
            extra = 58
            floor = 104
        elif w >= 1200:
            extra = 50
            floor = 96
        elif w >= 950:
            extra = 44
            floor = 90
        else:
            extra = 38
            floor = 84

        dpi_scale = 1.0
        try:
            screen = self.screen() or (QApplication.primaryScreen() if QApplication.instance() else None)
            if screen:
                dpi_scale = max(1.0, screen.logicalDotsPerInch() / 96.0)
        except Exception:
            dpi_scale = 1.0

        computed = int((text_w + extra) * min(1.25, max(1.0, dpi_scale ** 0.5)))
        return max(floor, computed)

    def update_adaptive_sizing(self) -> None:
        """Refresh adaptive sizing when screen resolution or DPI scaling changes."""
        if hasattr(self, "board_selector") and hasattr(self.board_selector, "_get_adaptive_width"):
            adaptive_w = self.board_selector._get_adaptive_width()
            self.board_selector.setMinimumWidth(max(160, int(adaptive_w * 0.85)))
            self.board_selector.setMaximumWidth(max(360, int(adaptive_w * 1.5)))
            self.board_selector.updateGeometry()
        if hasattr(self, "upload_speed_combo") and hasattr(self, "_get_adaptive_upload_speed_width"):
            self.upload_speed_combo.setFixedWidth(self._get_adaptive_upload_speed_width())

    def is_compact(self) -> bool:
        return getattr(self, "_is_compact", False)

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt controls bar buttons, labels, and input fields based on width."""
        self._current_width = width
        compact_buttons = width < 1350
        self._is_compact = compact_buttons

        if compact_buttons:
            self.btn_detach_editor.setVisible(False)
            self.btn_toggle_editor.setVisible(False)
            self.btn_toggle_monitors.setVisible(False)
            self.btn_settings.setVisible(False)
            self.btn_ai.setVisible(False)
            self.btn_opt_dropdown.setVisible(True)
        else:
            self.btn_detach_editor.setVisible(True)
            self.btn_toggle_editor.setVisible(True)
            self.btn_toggle_monitors.setVisible(True)
            self.btn_settings.setVisible(True)
            self.btn_ai.setVisible(True)
            self.btn_opt_dropdown.setVisible(False)
            if hasattr(self, "_opt_popup") and self._opt_popup and self._opt_popup.isVisible():
                self._opt_popup.close()
                self._opt_popup = None

        # Upload speed combo responsive width
        if hasattr(self, "upload_speed_combo") and hasattr(self, "_get_adaptive_upload_speed_width"):
            self.upload_speed_combo.setFixedWidth(self._get_adaptive_upload_speed_width(width))

        # Checkboxes & SPD label adaptation
        if width < 1100:
            self.cb_timestamp.setText("TS")
            self.cb_skip_compile.setText("Skip")
            if hasattr(self, "lbl_spd"):
                self.lbl_spd.setText("SPD")
        elif width < 1350:
            self.cb_timestamp.setText("Time Stamp")
            self.cb_skip_compile.setText("Skip")
            if hasattr(self, "lbl_spd"):
                self.lbl_spd.setText("UPLOAD SPD")
        else:
            self.cb_timestamp.setText("Time Stamp")
            self.cb_skip_compile.setText("Skip Compile")
            if hasattr(self, "lbl_spd"):
                self.lbl_spd.setText("UPLOAD SPD")

        # Board and port adaptive widths
        if hasattr(self, "board_selector") and hasattr(self.board_selector, "_get_adaptive_width"):
            adaptive_w = self.board_selector._get_adaptive_width()
            if width < 950:
                self.board_selector.setMinimumWidth(max(140, int(adaptive_w * 0.75)))
                self.port_combo.setMinimumWidth(140)
            elif width < 1200:
                self.board_selector.setMinimumWidth(max(150, int(adaptive_w * 0.85)))
                self.port_combo.setMinimumWidth(170)
            elif width < 1400:
                self.board_selector.setMinimumWidth(max(160, int(adaptive_w * 0.95)))
                self.port_combo.setMinimumWidth(200)
            else:
                self.board_selector.setMinimumWidth(max(175, adaptive_w))
                self.port_combo.setMinimumWidth(230)

    def _toggle_options_menu(self) -> None:
        """Show popup containing option buttons that are collapsed in compact mode."""
        if hasattr(self, "_opt_popup") and self._opt_popup and self._opt_popup.isVisible():
            self._opt_popup.close()
            self._opt_popup = None
            return

        popup = CompactDropdownPopup(self)
        from main.core.theme import Theme
        mw = self.window()

        # 1. Detach / Attach Editor
        detached = getattr(mw, "_editor_detached", False) or (self.btn_detach_editor.text() == "Attach Editor")
        det_text = "Attach Editor" if detached else "Detach Editor"
        det_bg = Theme.ORANGE if detached else "#2d7d46"
        det_hover = "#d35400" if detached else "#38a058"
        popup.add_button(
            det_text,
            self._toggle_editor_detachment,
            det_bg,
            det_hover,
            enabled=self.btn_detach_editor.isEnabled(),
        )

        # 2. Hide / Show Editor
        editor_visible = getattr(mw, "_editor_pane_visible", True)
        ed_text = "Hide Editor" if editor_visible else "Show Editor"
        popup.add_button(
            ed_text,
            self._toggle_editor_pane,
            Theme.BTN_CLEAR,
            Theme.BTN_CLEAR_H,
            enabled=self.btn_toggle_editor.isEnabled(),
        )

        # 3. Hide / Show Monitors
        monitors_visible = getattr(mw, "_monitors_pane_visible", True)
        mon_text = "Hide Monitors" if monitors_visible else "Show Monitors"
        popup.add_button(
            mon_text,
            self._toggle_monitors_pane,
            Theme.BTN_CLEAR,
            Theme.BTN_CLEAR_H,
            enabled=self.btn_toggle_monitors.isEnabled(),
        )

        # 4. Settings
        popup.add_button(
            "⚙ Settings",
            self._open_settings,
            Theme.BTN_CLEAR,
            Theme.BTN_CLEAR_H,
            enabled=True,
        )

        # 5. AI Assistant
        ai_active = getattr(mw, "_ai_visible", False) or self.btn_ai.isChecked()
        ai_text = "🤖 Hide AI" if ai_active else "🤖 AI Assistant"
        ai_bg = Theme.CYAN_DIM if ai_active else Theme.BTN_CLEAR
        ai_hover = Theme.CYAN if ai_active else Theme.BTN_CLEAR_H
        popup.add_button(
            ai_text,
            lambda: self._toggle_ai_panel(not ai_active),
            ai_bg,
            ai_hover,
            enabled=True,
        )

        popup.show_below(self.btn_opt_dropdown, min_width=max(125, self.btn_opt_dropdown.width()), alignment="right")
        self._opt_popup = popup
