#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.settings_dialog — Comprehensive Settings Dialog for MCU Flasher by Naph.

Faithfully aligned with the original stable release (settings_dialog_mixin.py).
Provides structured multi-section configuration:
  • Performance Settings (CPU multithreading, Graphics acceleration, Console font size, Warning filter)
  • Appearance & Theme (Color themes, System Default OS following)
  • File Editor (Default vs Monaco with crash risk guard for low-spec PCs)
  • Auto-Save (Toggle & millisecond delay)
  • Startup (Bootstrap pipeline indicator)
  • Hardware Reset Operations (Hard Reset Bootloader/Erase & Soft Reset Flash)
"""
from __future__ import annotations

import os
from typing import Optional
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QCursor
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QCheckBox, QSpinBox, QGroupBox, QScrollArea,
    QMessageBox,
)

from main.core.constants import board_reset_capabilities
from main.core.board_catalog import SUPPORTED_BOARDS
from main.core.config import (
    _load_raw_config, _save_raw_config,
    get_theme_settings, set_theme_mode, _detect_system_theme,
    get_monitor_font_size, get_hide_build_console_warnings, set_hide_build_console_warnings,
    get_editor_mode, set_editor_mode,
    get_autosave_settings, set_autosave_settings,
)
from main.core.toolchain import _resource_safe_worker_count, _system_reserved_cpu_count
from main.qt.signals import signals


class SettingsDialog(QDialog):
    """
    Comprehensive application settings dialog.
    Scrollable multi-section layout with live applying capabilities.
    """

    def __init__(self, backend: "MCUWebBackendAPI", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self.setWindowTitle("Settings — MCU Flasher by Naph")

        # Adaptive dimensions based on active screen available work area
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        avail_w = avail.width() if avail else 1280
        avail_h = avail.height() if avail else 720

        target_w = min(600, max(520, int(avail_w * 0.85)))
        target_h = min(680, max(460, int(avail_h * 0.88)))
        target_w = min(target_w, avail_w)
        target_h = min(target_h, avail_h)

        self.setMinimumWidth(min(520, target_w))
        self.resize(target_w, target_h)

        if parent:
            geo = parent.geometry()
            self.move(
                geo.x() + max(0, (geo.width() - target_w) // 2),
                geo.y() + max(0, (geo.height() - target_h) // 2),
            )
        elif avail:
            self.move(
                avail.x() + max(0, (avail_w - target_w) // 2),
                avail.y() + max(0, (avail_h - target_h) // 2),
            )
        _this_file = Path(__file__).resolve()
        _project_root = _this_file.parent.parent.parent
        _icons_dir = _project_root / "src" / "assets" / "icons"
        self._icon_checked = (_icons_dir / "checkbox_checked.svg").as_posix()
        self._icon_checked_dim = (_icons_dir / "checkbox_checked_disabled.svg").as_posix()

        self._raw_cfg = _load_raw_config()
        self._shared = self._raw_cfg.get("shared", {})

        self._build_ui()

        from main.core.config import get_theme_mode
        self._apply_dialog_theme(get_theme_mode())

        try:
            from main.qt.signals import signals
            signals.theme_changed.connect(self._apply_dialog_theme)
        except Exception:
            pass

    def _apply_dialog_theme(self, mode: str) -> None:
        if mode == "light":
            bg = "#f5f6f8"
            fg = "#2e3440"
            scroll_bg = "#ffffff"
            scroll_bar = "#e5e9f0"
            scroll_handle = "#d8dee9"
            scroll_handle_hover = "#88c0d0"
            group_bg = "#ffffff"
            group_border = "#d8dee9"
            group_title = "#5e81ac"
            combo_bg = "#f0f4f8"
            combo_fg = "#2e3440"
            combo_border = "#d8dee9"
            chk_bg = "#f0f4f8"
            chk_checked_bg = "#d8dee9"
            chk_checked_hover = "#b48ead"
        elif mode == "solarized_dark":
            bg = "#002b36"
            fg = "#93a1a1"
            scroll_bg = "#073642"
            scroll_bar = "#073642"
            scroll_handle = "#586e75"
            scroll_handle_hover = "#2aa198"
            group_bg = "#073642"
            group_border = "#586e75"
            group_title = "#2aa198"
            combo_bg = "#002b36"
            combo_fg = "#93a1a1"
            combo_border = "#586e75"
            chk_bg = "#002b36"
            chk_checked_bg = "#073642"
            chk_checked_hover = "#2aa198"
        else:
            bg = "#0c0d10"
            fg = "#cdd6f4"
            scroll_bg = "#0b0e14"
            scroll_bar = "#0b0e14"
            scroll_handle = "#3e4f6d"
            scroll_handle_hover = "#56cfbf"
            group_bg = "#151922"
            group_border = "#2d3748"
            group_title = "#56cfbf"
            combo_bg = "#1c2333"
            combo_fg = "#e8eaf6"
            combo_border = "#2d3748"
            chk_bg = "#1c2333"
            chk_checked_bg = "#2a5f58"
            chk_checked_hover = "#36776e"

        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg};
                color: {fg};
            }}
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollBar:vertical {{
                background: {scroll_bar};
                width: 14px;
                margin: 0px;
                border-left: 1px solid {group_border};
            }}
            QScrollBar::handle:vertical {{
                background: {scroll_handle};
                border-radius: 5px;
                min-height: 28px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {scroll_handle_hover};
            }}
            QScrollBar::handle:vertical:pressed {{
                background: {scroll_handle_hover};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
                background: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QGroupBox {{
                background-color: {group_bg};
                border: 1px solid {group_border};
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 14px;
                color: {group_title};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 6px;
                background-color: {group_bg};
            }}
            QLabel {{
                color: {fg};
            }}
            QComboBox, QSpinBox {{
                background-color: {combo_bg};
                color: {combo_fg};
                border: 1px solid {combo_border};
                border-radius: 4px;
                padding: 4px 8px;
                min-height: 22px;
            }}
            QComboBox:focus, QSpinBox:focus {{
                border-color: {group_title};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QCheckBox {{
                color: {fg};
                font-size: 11px;
                spacing: 6px;
            }}
            QCheckBox::indicator {{
                width: 15px;
                height: 15px;
                border-radius: 3px;
                border: 1px solid {combo_border};
                background: {chk_bg};
            }}
            QCheckBox::indicator:hover {{
                border-color: {group_title};
            }}
            QCheckBox::indicator:checked {{
                background-color: {chk_checked_bg};
                border-color: {group_title};
                image: url("{self._icon_checked}");
            }}
            QCheckBox::indicator:checked:hover {{
                background-color: {chk_checked_hover};
                border-color: {group_title};
                image: url("{self._icon_checked}");
            }}
            QCheckBox::indicator:disabled {{
                background: {group_bg};
                border-color: {combo_border};
            }}
            QCheckBox::indicator:checked:disabled {{
                background: {combo_bg};
                border-color: {combo_border};
                image: url("{self._icon_checked_dim}");
            }}
        """)

        from main.qt.theme import get_palette
        pal = get_palette(mode)
        if hasattr(self, "btn_cancel"):
            self.btn_cancel.setStyleSheet(f"""
                QPushButton {{
                    background: {pal.get('BTN_CLEAR', '#2d3748')};
                    color: {pal.get('TEXT_BRIGHT', '#ffffff')};
                    font-size: 11px;
                    font-weight: 600;
                    border-radius: 4px;
                    border: 1px solid {pal.get('BORDER', '#2d3748')};
                }}
                QPushButton:hover {{ background: {pal.get('BTN_CLEAR_H', '#3a4a60')}; }}
            """)
        if hasattr(self, "btn_save"):
            self.btn_save.setStyleSheet(f"""
                QPushButton {{
                    background: {pal.get('BTN_COMPILE', '#1a5c3a')};
                    color: #ffffff;
                    font-size: 11px;
                    font-weight: 700;
                    border-radius: 4px;
                    border: 1px solid rgba(255,255,255,0.1);
                }}
                QPushButton:hover {{ background: {pal.get('BTN_COMPILE_H', '#216e46')}; }}
            """)
        if hasattr(self, "btn_hard_reset"):
            self.btn_hard_reset.setStyleSheet(f"""
                QPushButton {{
                    background: {pal.get('BTN_STOP', '#6e2020')};
                    color: #ffffff;
                    font-weight: 700;
                    font-size: 11px;
                    border-radius: 4px;
                    border: 1px solid rgba(255,255,255,0.1);
                }}
                QPushButton:hover {{ background: {pal.get('BTN_STOP_H', '#882828')}; }}
                QPushButton:disabled {{ background: {pal.get('BG_MID', '#2d3748')}; color: {pal.get('TEXT_DIM', '#6b7280')}; }}
            """)
        if hasattr(self, "btn_soft_reset"):
            self.btn_soft_reset.setStyleSheet(f"""
                QPushButton {{
                    background: {pal.get('BTN_UPLOAD', '#1e3a5f')};
                    color: #ffffff;
                    font-weight: 700;
                    font-size: 11px;
                    border-radius: 4px;
                    border: 1px solid rgba(255,255,255,0.1);
                }}
                QPushButton:hover {{ background: {pal.get('BTN_UPLOAD_H', '#2a5080')}; }}
                QPushButton:disabled {{ background: {pal.get('BG_MID', '#2d3748')}; color: {pal.get('TEXT_DIM', '#6b7280')}; }}
            """)

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(12, 12, 12, 12)
        outer_layout.setSpacing(10)

        # Scroll container
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 8, 4)
        layout.setSpacing(14)

        # ── 1. Performance Settings ──────────────────────────────────────────
        perf_box = QGroupBox("Performance Settings")
        pv = QVBoxLayout(perf_box)
        pv.setSpacing(10)

        # CPU multithreading
        cpu_row = QHBoxLayout()
        cpu_lbl = QLabel("CPU Cores Multithreading:")
        cpu_lbl.setFixedWidth(190)
        cpu_row.addWidget(cpu_lbl)

        total_processors = os.cpu_count() or 4
        reserved_processors = _system_reserved_cpu_count(total_processors)
        low_jobs = _resource_safe_worker_count("LOW", total_processors)
        med_jobs = _resource_safe_worker_count("MEDIUM", total_processors)
        high_jobs = _resource_safe_worker_count("HIGH", total_processors)

        self._low_val = f"LOW ({low_jobs} Jobs)"
        self._med_val = f"MEDIUM ({med_jobs} Jobs)"
        self._high_val = f"HIGH ({high_jobs} Jobs + {reserved_processors} Reserved)"

        current_cpu = self._shared.get("cpu_multithreading", "HIGH")
        self.cpu_combo = QComboBox()
        self.cpu_combo.addItems([self._low_val, self._med_val, self._high_val])
        if current_cpu == "LOW":
            self.cpu_combo.setCurrentText(self._low_val)
        elif current_cpu == "MEDIUM":
            self.cpu_combo.setCurrentText(self._med_val)
        else:
            self.cpu_combo.setCurrentText(self._high_val)
        cpu_row.addWidget(self.cpu_combo, stretch=1)
        pv.addLayout(cpu_row)

        # Graphics acceleration
        self.cb_g_accel = QCheckBox("Graphics Acceleration (Smooth sash resize)")
        current_g = self._shared.get("graphics_acceleration", "ON")
        self.cb_g_accel.setChecked(current_g == "ON")
        pv.addWidget(self.cb_g_accel)

        # Font size
        font_row = QHBoxLayout()
        font_lbl = QLabel("Editor & Monitor font size:")
        font_lbl.setFixedWidth(190)
        font_row.addWidget(font_lbl)

        self.font_combo = QComboBox()
        for sz in range(8, 25):
            self.font_combo.addItem(f"{sz} pt", userData=sz)
        cur_font_size = get_monitor_font_size()
        font_idx = self.font_combo.findData(cur_font_size)
        if font_idx >= 0:
            self.font_combo.setCurrentIndex(font_idx)
        else:
            self.font_combo.setCurrentIndex(self.font_combo.findData(11))
        self.font_combo.setFixedWidth(90)
        font_row.addWidget(self.font_combo)
        font_row.addStretch()
        pv.addLayout(font_row)

        # Hide console warnings
        self.cb_hide_warnings = QCheckBox("Hide warnings in Build Console")
        self.cb_hide_warnings.setChecked(get_hide_build_console_warnings())
        pv.addWidget(self.cb_hide_warnings)

        hide_note = QLabel("Only warning-tagged lines are hidden from the Build Console; compiler and toolchain behavior is unchanged.")
        hide_note.setProperty("role", "dim")
        hide_note.setWordWrap(True)
        pv.addWidget(hide_note)

        layout.addWidget(perf_box)

        # ── 2. Appearance & Theme ─────────────────────────────────────────────
        theme_box = QGroupBox("Appearance & Theme")
        tv = QVBoxLayout(theme_box)
        tv.setSpacing(10)

        theme_row = QHBoxLayout()
        theme_lbl = QLabel("Color Theme:")
        theme_lbl.setFixedWidth(190)
        theme_row.addWidget(theme_lbl)

        self._theme_map = {
            "Default (Dark Cyberpunk)": "default",
            "Light (Clean & Bright)": "light",
            "Solarized Dark (Teal / Cyan)": "solarized_dark",
        }
        self._theme_rev = {v: k for k, v in self._theme_map.items()}
        self._theme_rev["dark"] = "Default (Dark Cyberpunk)"

        self.theme_combo = QComboBox()
        for label in self._theme_map.keys():
            self.theme_combo.addItem(label)

        cur_saved_theme, cur_follow_sys = get_theme_settings()
        self._last_manual_theme = "default" if cur_saved_theme == "dark" else cur_saved_theme
        self.theme_combo.setCurrentText(self._theme_rev.get(self._last_manual_theme, "Default (Dark Cyberpunk)"))
        self.theme_combo.currentIndexChanged.connect(self._on_theme_combo_changed)
        theme_row.addWidget(self.theme_combo, stretch=1)
        tv.addLayout(theme_row)

        self.cb_theme_system = QCheckBox("System Default (Follow Windows Light / Dark mode)")
        self.cb_theme_system.setChecked(cur_follow_sys)
        self.cb_theme_system.toggled.connect(self._on_theme_system_toggled)
        tv.addWidget(self.cb_theme_system)
        self._on_theme_system_toggled(cur_follow_sys)

        theme_note = QLabel("Changing the theme applies across the main UI, editor, and integrated terminal.")
        theme_note.setProperty("role", "dim")
        theme_note.setWordWrap(True)
        tv.addWidget(theme_note)

        layout.addWidget(theme_box)

        # ── 3. File Editor ───────────────────────────────────────────────────
        editor_box = QGroupBox("File Editor")
        ev = QVBoxLayout(editor_box)
        ev.setSpacing(10)

        ed_row = QHBoxLayout()
        ed_lbl = QLabel("Editor Engine:")
        ed_lbl.setFixedWidth(190)
        ed_row.addWidget(ed_lbl)

        self.editor_combo = QComboBox()
        self._ed_default_label = "Default (Lightweight)"
        self._ed_monaco_label = "Monaco (VS Code-style, heavier)"
        self.editor_combo.addItems([self._ed_default_label, self._ed_monaco_label])

        cur_editor = get_editor_mode()
        self._current_editor_mode = cur_editor
        if cur_editor == "monaco":
            self.editor_combo.setCurrentText(self._ed_monaco_label)
        else:
            self.editor_combo.setCurrentText(self._ed_default_label)

        self._monaco_confirmed = (cur_editor == "monaco")
        self.editor_combo.currentIndexChanged.connect(self._on_editor_choice)
        ed_row.addWidget(self.editor_combo, stretch=1)
        ev.addLayout(ed_row)

        editor_note = QLabel("Changing the editor takes effect the next time the app is started.")
        editor_note.setProperty("role", "dim")
        editor_note.setWordWrap(True)
        ev.addWidget(editor_note)

        layout.addWidget(editor_box)

        # ── 4. Auto-Save ─────────────────────────────────────────────────────
        autosave_box = QGroupBox("Auto-Save")
        av = QVBoxLayout(autosave_box)
        av.setSpacing(10)

        cur_autosave_en, cur_autosave_delay = get_autosave_settings()

        as_row = QHBoxLayout()
        self.cb_autosave = QCheckBox("Enable Auto-Save")
        self.cb_autosave.setChecked(cur_autosave_en)
        as_row.addWidget(self.cb_autosave)

        delay_lbl = QLabel("Delay (ms):")
        as_row.addWidget(delay_lbl)

        self.autosave_spin = QSpinBox()
        self.autosave_spin.setRange(500, 10000)
        self.autosave_spin.setSingleStep(500)
        self.autosave_spin.setValue(int(cur_autosave_delay))
        self.autosave_spin.setEnabled(cur_autosave_en)
        self.cb_autosave.toggled.connect(self.autosave_spin.setEnabled)
        as_row.addWidget(self.autosave_spin)
        as_row.addStretch()
        av.addLayout(as_row)

        as_note = QLabel("Automatically saves modified files after you stop typing for the given delay.")
        as_note.setProperty("role", "dim")
        as_note.setWordWrap(True)
        av.addWidget(as_note)

        layout.addWidget(autosave_box)

        # ── 5. Startup ───────────────────────────────────────────────────────
        startup_box = QGroupBox("Startup")
        sv = QVBoxLayout(startup_box)
        startup_lbl = QLabel("Bootstrap runs before the main app on every launch.")
        startup_lbl.setProperty("role", "dim")
        sv.addWidget(startup_lbl)
        layout.addWidget(startup_box)

        # ── 6. Hardware Reset Operations ─────────────────────────────────────
        reset_box = QGroupBox("Hardware Reset Operations")
        rv = QVBoxLayout(reset_box)
        rv.setSpacing(10)

        # Query board reset capabilities
        board_name = getattr(self._backend, "current_board", "")
        binfo = {}
        if self._backend and hasattr(self._backend, "_resolve_board_info"):
            try:
                binfo = self._backend._resolve_board_info(board_name)
            except Exception:
                binfo = {}
        if not binfo:
            binfo = SUPPORTED_BOARDS.get(board_name, {})

        platform = str(binfo.get("platform", "")).lower()
        reset_caps = board_reset_capabilities(
            platform,
            binfo.get("board", ""),
            board_name,
            binfo.get("framework", ""),
        )
        can_soft = bool(reset_caps.get("soft_reset") or binfo.get("pio_resolved", True))

        btn_row = QHBoxLayout()
        hard_reset_label = (
            "⚡ Hard Reset (Erase Flash)"
            if reset_caps.get("hard_strategy") == "esp8266_erase"
            else "⚡ Hard Reset (Bootloader)"
        )
        self.btn_hard_reset = QPushButton(hard_reset_label)
        self.btn_hard_reset.setFixedHeight(30)
        self.btn_hard_reset.setStyleSheet("""
            QPushButton:enabled {
                background: #6e2020;
                color: #ffffff;
                font-weight: 700;
                font-size: 11px;
                border-radius: 4px;
                border: 1px solid rgba(255,255,255,0.15);
            }
            QPushButton:enabled:hover {
                background: #882828;
                border: 1px solid #e74c3c;
            }
            QPushButton:enabled:pressed {
                background: #501616;
                border: 1px solid #e74c3c;
            }
            QPushButton:disabled {
                background: #2d3748;
                color: #6b7280;
                border: 1px solid #1c2333;
            }
        """)
        self.btn_hard_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hard_reset.clicked.connect(self._run_hard_reset)
        btn_row.addWidget(self.btn_hard_reset)

        self.btn_soft_reset = QPushButton("🔄 Soft Reset (Reset Flash)")
        self.btn_soft_reset.setFixedHeight(30)
        self.btn_soft_reset.setStyleSheet("""
            QPushButton:enabled {
                background: #1e3a5f;
                color: #ffffff;
                font-weight: 700;
                font-size: 11px;
                border-radius: 4px;
                border: 1px solid rgba(255,255,255,0.15);
            }
            QPushButton:enabled:hover {
                background: #2a5080;
                border: 1px solid #5ca4f0;
            }
            QPushButton:enabled:pressed {
                background: #142842;
                border: 1px solid #5ca4f0;
            }
            QPushButton:disabled {
                background: #2d3748;
                color: #6b7280;
                border: 1px solid #1c2333;
            }
        """)
        self.btn_soft_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_soft_reset.clicked.connect(self._run_soft_reset)
        btn_row.addWidget(self.btn_soft_reset)
        rv.addLayout(btn_row)

        has_board = bool(board_name and binfo)
        has_port = bool(self._backend and getattr(self._backend, "current_port", ""))
        if not has_board or not has_port:
            self.btn_hard_reset.setEnabled(False)
            self.btn_hard_reset.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_soft_reset.setEnabled(False)
            self.btn_soft_reset.setCursor(Qt.CursorShape.ArrowCursor)
            warn_lbl = QLabel("⚠ Please select an MCU board and COM port in the main toolbar to use Hardware Reset.")
            warn_lbl.setStyleSheet("color: #f1c40f; font-size: 10px; font-style: italic;")
            rv.addWidget(warn_lbl)
        elif not can_soft:
            self.btn_soft_reset.setEnabled(False)
            self.btn_soft_reset.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_soft_reset.setText("Soft Reset unavailable (Arduino framework required)")

        layout.addWidget(reset_box)

        # Finish scroll setup
        scroll.setWidget(container)
        outer_layout.addWidget(scroll, stretch=1)

        # ── Dialog Action Buttons (Save / Cancel) ─────────────────────────────
        act_row = QHBoxLayout()
        act_row.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedSize(85, 30)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.reject)
        act_row.addWidget(self.btn_cancel)

        self.btn_save = QPushButton("Save")
        self.btn_save.setProperty("role", "primary")
        self.btn_save.setFixedSize(85, 30)
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.clicked.connect(self._save_and_apply)
        act_row.addWidget(self.btn_save)

        outer_layout.addLayout(act_row)

    # ── Slots & Logic ────────────────────────────────────────────────────────
    def _on_theme_combo_changed(self, index: int) -> None:
        if self.theme_combo.isEnabled():
            chosen = self._theme_map.get(self.theme_combo.currentText(), "default")
            self._last_manual_theme = chosen

    def _on_theme_system_toggled(self, checked: bool) -> None:
        if checked:
            if self.theme_combo.isEnabled():
                chosen = self._theme_map.get(self.theme_combo.currentText(), self._last_manual_theme)
                self._last_manual_theme = chosen
            self.theme_combo.setEnabled(False)
            detected = _detect_system_theme()
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText(self._theme_rev.get(detected, "Default (Dark Cyberpunk)"))
            self.theme_combo.blockSignals(False)
        else:
            self.theme_combo.setEnabled(True)
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText(self._theme_rev.get(self._last_manual_theme, "Default (Dark Cyberpunk)"))
            self.theme_combo.blockSignals(False)

    def _on_editor_choice(self, index: int) -> None:
        chosen = self.editor_combo.currentText()
        if chosen == self._ed_monaco_label and not self._monaco_confirmed:
            ret = QMessageBox.question(
                self,
                "Monaco Editor Warning",
                "The Monaco editor is a heavier, browser-based editor.\n\n"
                "On low-spec devices, it may cause the application to "
                "freeze or crash on startup.\n\n"
                "Do you want to continue selecting Monaco?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret == QMessageBox.StandardButton.Yes:
                self._monaco_confirmed = True
            else:
                self.editor_combo.setCurrentText(self._ed_default_label)

    def _run_hard_reset(self) -> None:
        port = getattr(self._backend, "current_port", "")
        bname = getattr(self._backend, "current_board", "")
        binfo = self._backend._resolve_board_info(bname) if hasattr(self._backend, "_resolve_board_info") else {}
        plat = str(binfo.get("platform", "")).lower()
        reset_caps = board_reset_capabilities(
            plat,
            binfo.get("board", ""),
            bname,
            binfo.get("framework", ""),
        )
        strategy = reset_caps.get("hard_strategy")
        if strategy == "esp32_recovery":
            title = "ESP32 Full Erase + Burn Bootloader"
            msg = (
                f"This is a destructive hard reset on port '{port}'. It will erase the ENTIRE "
                "ESP32 flash, including the current application, NVS settings, OTA state, and filesystem data.\n\n"
                "After erasing, it will write only bootloader.bin and partitions.bin from "
                "the dedicated compiled recovery project, plus boot_app0. The board will be left in a clean, "
                "bootable state ready for a fresh upload.\n\n"
                "After preparation, esptool will show a live BOOT connection indicator. "
                "Press and HOLD BOOT until the indicator turns green, then release it.\n\n"
                "Continue with the full erase?"
            )
        elif strategy == "esp8266_erase":
            title = "ESP8266 Full Flash Erase"
            msg = (
                f"This is a destructive hard reset on port '{port}'. It will erase the ENTIRE "
                "ESP8266 flash, including the application, settings, OTA state, and filesystem data.\n\n"
                "The ESP8266 ROM bootloader is built into the chip and will not be erased or rewritten. "
                "After the erase, use Upload to install a project sketch again.\n\n"
                "If the module or USB adapter has no automatic reset circuit, hold BOOT/GPIO0 LOW when "
                "prompted and release it after the connection indicator turns green.\n\n"
                "Continue with the full flash erase?"
            )
        else:
            title = "Hard Reset Confirmation"
            msg = (
                f"Are you sure you want to perform a Hard Reset on port '{port}'?\n\n"
                "This operation will reset the microcontroller hardware to restore clean startup state.\n\n"
                "Continue?"
            )

        ret = QMessageBox.question(
            self,
            title,
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes and self._backend:
            erase = "Erase" in self.btn_hard_reset.text()
            self._backend.hard_reset(erase_flash=erase)
            self.accept()

    def _run_soft_reset(self) -> None:
        port = getattr(self._backend, "current_port", "")
        bname = getattr(self._backend, "current_board", "")
        ret = QMessageBox.question(
            self,
            "Soft Reset (Reset Flash)",
            f"Are you sure you want to perform a Soft Reset on port '{port}' for '{bname}'?\n\n"
            "This will compile and upload a minimal safe recovery sketch to clear "
            "any corrupted user code, bad application state, or crash bootloops.\n\n"
            "Continue with Soft Reset?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes and self._backend:
            self._backend.soft_reset()
            self.accept()


    def _save_and_apply(self) -> None:
        """Persist all settings and emit console log confirmations."""
        data = _load_raw_config()
        if "shared" not in data or not isinstance(data["shared"], dict):
            data["shared"] = {}

        # 1. CPU Multithreading
        cpu_text = self.cpu_combo.currentText()
        if cpu_text == self._low_val:
            cpu_key = "LOW"
        elif cpu_text == self._med_val:
            cpu_key = "MEDIUM"
        else:
            cpu_key = "HIGH"
        data["shared"]["cpu_multithreading"] = cpu_key

        # 2. Graphics Acceleration
        g_accel = "ON" if self.cb_g_accel.isChecked() else "OFF"
        data["shared"]["graphics_acceleration"] = g_accel

        # 3. Monitor Font Size
        new_font_size = self.font_combo.currentData() or 11
        data["shared"]["monitor_font_size"] = new_font_size

        # 4. Hide Warnings in Build Console
        hide_warn = self.cb_hide_warnings.isChecked()
        set_hide_build_console_warnings(hide_warn)
        data["shared"]["hide_build_console_warnings"] = hide_warn

        # 5. Appearance & Theme
        follow_sys = self.cb_theme_system.isChecked()
        if follow_sys:
            saved_mode = self._last_manual_theme
            active_theme = _detect_system_theme()
        else:
            chosen_theme = self._theme_map.get(self.theme_combo.currentText(), "default")
            saved_mode = chosen_theme
            active_theme = chosen_theme
            self._last_manual_theme = chosen_theme

        data["shared"]["theme_mode"] = saved_mode
        data["shared"]["theme_follow_system"] = follow_sys
        set_theme_mode(saved_mode, follow_system=follow_sys)

        from main.core.theme import Theme
        Theme.apply_theme(active_theme)

        from main.qt.theme import build_stylesheet
        # pyrefly: ignore [missing-import]
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.setStyleSheet(build_stylesheet(active_theme))

        self._apply_dialog_theme(active_theme)

        # 6. File Editor
        new_editor_mode = "monaco" if self.editor_combo.currentText() == self._ed_monaco_label else "default"
        data["shared"]["editor_mode"] = new_editor_mode
        set_editor_mode(new_editor_mode)

        # 7. Auto-Save
        as_enabled = self.cb_autosave.isChecked()
        as_delay = self.autosave_spin.value()
        set_autosave_settings(as_enabled, as_delay)
        data["shared"]["autosave_enabled"] = as_enabled
        data["shared"]["autosave_delay_ms"] = as_delay

        # Save to disk
        _save_raw_config(data)

        # Record into persistent Notification database and emit to Notifications tab
        theme_str = f"System Default ({active_theme.replace('_', ' ').title()})" if follow_sys else active_theme.replace('_', ' ').title()
        autosave_str = f"ON ({as_delay} ms)" if as_enabled else "OFF"
        warn_str = "Hidden" if hide_warn else "Visible"

        notif_msg = (
            f"• CPU Multithreading: {cpu_key}\n"
            f"• Graphics Acceleration: {g_accel}\n"
            f"• Editor & Monitor Font: {new_font_size} pt\n"
            f"• Console Warnings: {warn_str}\n"
            f"• Theme Mode: {theme_str}\n"
            f"• Auto-Save: {autosave_str}"
        )
        try:
            from src.dbs import dbs_create
            dbs_create.add_notification(
                category="system",
                level="success",
                title="Settings Applied",
                message=notif_msg,
            )
        except Exception:
            pass

        signals.notification.emit({
            "title": "Settings Applied",
            "message": f"Preferences updated successfully ({new_font_size} pt font).",
            "type": "success",
        })

        # Apply font size and theme live to child panels
        signals.font_size_changed.emit(new_font_size)
        signals.theme_changed.emit(active_theme)

        self.accept()
