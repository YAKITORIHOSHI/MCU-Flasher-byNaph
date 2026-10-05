#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.settings_dialog — Comprehensive Settings Dialog for MCU Flasher by Naph.

Uses the active glass palette with structured, resource-aware configuration:
  • Performance Settings (CPU multithreading, Continuous panel resize, Console font size, Warning filter)
  • Appearance & Theme (Glass Smoked Dark, Frosted Light, Solarized and OS following)
  • File Editor (Offline Monaco with automatic low-end resource settings)
  • Auto-Save (Toggle & millisecond delay)
  • Startup (Bootstrap pipeline indicator)
  • Hardware Reset Operations (Hard Reset Bootloader/Erase & Soft Reset Flash)
"""
from __future__ import annotations

import os
import copy
from typing import Optional, TYPE_CHECKING
from pathlib import Path
from uuid import uuid4

if TYPE_CHECKING:
    from main.web_bridge import MCUWebBackendAPI

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QCheckBox, QSpinBox, QGroupBox, QScrollArea,
    QMessageBox, QBoxLayout,
)
from main.qt.icons import ActionButton as QPushButton

from main.core.constants import board_reset_capabilities
from main.core.target_profile import target_problem
from main.core.board_catalog import SUPPORTED_BOARDS
from main.core.config import (
    _load_raw_config, _save_raw_config,
    get_theme_settings, set_theme_mode, _detect_system_theme,
    get_monitor_font_size, get_hide_build_console_warnings, set_hide_build_console_warnings,
    get_autosave_settings, set_autosave_settings,
    get_reset_on_baud_change, set_reset_on_baud_change,
)
from main.core.toolchain import _resource_safe_worker_count, _system_reserved_cpu_count
from main.qt.signals import signals
from main.qt.responsive import fit_dialog, ScreenWatcher


class SettingsDialog(QDialog):
    """
    Comprehensive application settings dialog.
    Scrollable multi-section layout with live applying capabilities.
    """

    def __init__(self, backend: "MCUWebBackendAPI", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self.setWindowTitle("Settings — MCU Flasher by Naph")

        fit_dialog(self, (560, 680), (340, 300))
        _this_file = Path(__file__).resolve()
        _project_root = _this_file.parent.parent.parent
        _icons_dir = _project_root / "src" / "assets" / "icons"
        self._icon_checked = (_icons_dir / "checkbox_checked.svg").as_posix()
        self._icon_checked_dim = (_icons_dir / "checkbox_checked_disabled.svg").as_posix()

        self._raw_cfg = _load_raw_config()
        self._shared = self._raw_cfg.get("shared", {})

        self._build_ui()
        self._screen_watcher = ScreenWatcher(self, lambda _screen: self._adapt_rows())

        from main.core.config import get_theme_mode
        self._apply_dialog_theme(get_theme_mode())

        try:
            from main.qt.signals import signals
            signals.theme_changed.connect(self._apply_dialog_theme)
        except Exception:
            pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Ensure dialog always starts scrolled to top so Performance Settings is visible
        if hasattr(self, "scroll") and self.scroll.verticalScrollBar():
            self.scroll.verticalScrollBar().setValue(0)
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(0) if hasattr(self, "scroll") and self.scroll.verticalScrollBar() else None)
        if hasattr(self, "btn_save"):
            self.btn_save.setFocus()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_form_rows"):
            self._adapt_rows()

    def _adapt_rows(self) -> None:
        available = self.width() - 64
        for row, label in self._form_rows:
            field = row.itemAt(1).widget()
            stacked = available < 164 + field.minimumSizeHint().width() + row.spacing()
            row.setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
            label.setMinimumWidth(0 if stacked else 158)
            label.setMaximumWidth(16777215 if stacked else 158)
            label.setWordWrap(stacked)
        narrow = available < 440
        for row in (self._autosave_row, self._reset_row):
            row.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)

    def _apply_dialog_theme(self, mode: str) -> None:
        from main.qt.theme import get_palette
        pal = get_palette(mode)
        bg, fg = pal["BG_DARK"], pal["TEXT"]
        scroll_bg, scroll_bar = pal["BG_DARKEST"], pal["BG_DARK"]
        scroll_handle, scroll_handle_hover = pal["BORDER"], pal["CYAN"]
        group_bg, group_border, group_title = pal["BG_MID"], pal["BORDER"], pal["CYAN"]
        combo_bg, combo_fg, combo_border = pal["BG_DARKEST"], pal["TEXT"], pal["BORDER"]
        chk_bg, chk_border = pal["BG_DARK"], pal["BORDER"]
        chk_checked_bg, chk_checked_hover = pal["CYAN_DIM"], pal["BTN_MONITOR_H"]

        self.setStyleSheet(f"""
            QDialog {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {pal['BG_MID']}, stop:1 {bg});
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
                border-radius: 12px;
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
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 1px solid {chk_border};
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
                opacity: 0.5;
            }}
            QCheckBox::indicator:checked:disabled {{
                background: {combo_bg};
                border-color: {combo_border};
                image: url("{self._icon_checked_dim}");
            }}
        """)

        if hasattr(self, "btn_reset"):
            self.btn_reset.setStyleSheet(f"""
                QPushButton {{
                    background: {pal.get('BG_MID', '#2d3748')};
                    color: {pal.get('TEXT_MUTED', '#a0aec0')};
                    font-size: 11px;
                    font-weight: 600;
                    border-radius: 4px;
                    border: 1px solid {pal.get('BORDER', '#2d3748')};
                    padding: 0 12px;
                }}
                QPushButton:hover {{
                    background: {pal.get('BTN_STOP', '#6e2020')};
                    color: #ffffff;
                    border-color: #e74c3c;
                }}
            """)
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
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 8, 4)
        layout.setSpacing(12)

        # ── 1. Performance Settings ──────────────────────────────────────────
        perf_box = QGroupBox("Performance Settings")
        pv = QVBoxLayout(perf_box)
        pv.setSpacing(10)

        # CPU multithreading
        cpu_row = QHBoxLayout()
        cpu_lbl = QLabel("CPU Cores Multithreading:")
        cpu_lbl.setFixedWidth(158)
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

        # Continuous splitter resize; retain the legacy persistence key.
        self.cb_g_accel = QCheckBox("Continuous panel resizing")
        self.cb_g_accel.setToolTip("Turn off on slower devices to show a divider preview until you release it. This controls splitter resizing; GPU policy is managed automatically.")
        from src.modules.runtime_resources import performance_profile
        self._default_continuous_resize = not performance_profile().constrained
        current_g = self._shared.get("graphics_acceleration", "ON" if self._default_continuous_resize else "OFF")
        self.cb_g_accel.setChecked(current_g == "ON")
        pv.addWidget(self.cb_g_accel)

        # Font size
        font_row = QHBoxLayout()
        font_lbl = QLabel("Editor & Monitor font size:")
        font_lbl.setFixedWidth(158)
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
        theme_lbl.setFixedWidth(158)
        theme_row.addWidget(theme_lbl)

        self._theme_map = {
            "Glass (Smoked blue)": "default",
            "Glass (Frosted light)": "light",
            "Solarized Dark (Teal / Cyan)": "solarized_dark",
        }
        self._theme_rev = {v: k for k, v in self._theme_map.items()}
        self._theme_rev["dark"] = "Glass (Smoked blue)"

        self.theme_combo = QComboBox()
        for label in self._theme_map.keys():
            self.theme_combo.addItem(label)

        cur_saved_theme, cur_follow_sys = get_theme_settings()
        self._last_manual_theme = "default" if cur_saved_theme == "dark" else cur_saved_theme
        self.theme_combo.setCurrentText(self._theme_rev.get(self._last_manual_theme, "Glass (Smoked blue)"))
        self.theme_combo.currentIndexChanged.connect(self._on_theme_combo_changed)
        theme_row.addWidget(self.theme_combo, stretch=1)
        tv.addLayout(theme_row)

        self.cb_theme_system = QCheckBox("Follow system appearance")
        self.cb_theme_system.setToolTip("Follow the operating system’s light or dark appearance; retain your manual theme for when this is disabled.")
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
        ed_lbl.setFixedWidth(158)
        ed_row.addWidget(ed_lbl)

        editor_label = QLabel("Offline Monaco", editor_box)
        editor_label.setObjectName("editor-engine-label")
        ed_row.addWidget(editor_label, stretch=1)
        ev.addLayout(ed_row)

        editor_note = QLabel("CPU and RAM detection automatically reduce editor animation, minimap and background checks on constrained devices.")
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

        # ── 5. Serial Monitor ────────────────────────────────────────────────
        serial_box = QGroupBox("Serial Monitor")
        sm_v = QVBoxLayout(serial_box)
        sm_v.setSpacing(8)

        self.cb_reset_on_baud = QCheckBox("Reset MCU when baud rate changes")
        self.cb_reset_on_baud.setToolTip("Reset the MCU via DTR/RTS when the serial monitor baud rate changes.")
        self.cb_reset_on_baud.setChecked(get_reset_on_baud_change())
        sm_v.addWidget(self.cb_reset_on_baud)

        reset_baud_note = QLabel(
            "Automatically triggers a hardware reset pulse (DTR/RTS) when changing the Serial Monitor baud rate, "
            "rebooting the microcontroller so setup() runs at the newly selected speed."
        )
        reset_baud_note.setProperty("role", "dim")
        reset_baud_note.setWordWrap(True)
        sm_v.addWidget(reset_baud_note)

        layout.addWidget(serial_box)

        # ── 6. Startup ───────────────────────────────────────────────────────
        startup_box = QGroupBox("Startup")
        sv = QVBoxLayout(startup_box)
        startup_lbl = QLabel("Windows uses cached runtime health checks for faster launches; changed or missing dependencies trigger setup. Ubuntu uses its native Python environment. Resource limits apply automatically on 4- and 6-core devices.")
        startup_lbl.setWordWrap(True)
        startup_lbl.setProperty("role", "dim")
        sv.addWidget(startup_lbl)
        layout.addWidget(startup_box)

        # ── 7. Hardware Reset Operations ─────────────────────────────────────
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
        target_valid = bool(binfo) and not target_problem(binfo)
        can_soft = target_valid and bool(reset_caps.get("soft_reset"))

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

        has_board = bool(board_name and target_valid)
        has_port = bool(self._backend and getattr(self._backend, "current_port", ""))
        can_hard = target_valid and bool(reset_caps.get("hard_reset_ui"))
        busy = bool(getattr(self._backend, "is_busy", False) or getattr(self._backend, "active_operation", None))
        if not has_board or not has_port or busy:
            self.btn_hard_reset.setEnabled(False)
            self.btn_hard_reset.setCursor(Qt.CursorShape.ArrowCursor)
            self.btn_soft_reset.setEnabled(False)
            self.btn_soft_reset.setCursor(Qt.CursorShape.ArrowCursor)
            warn_lbl = QLabel("Wait for the current operation to finish before resetting." if busy else "Select a resolved MCU board and serial port to use Hardware Reset.")
            warn_lbl.setProperty("role", "dim")
            warn_lbl.setWordWrap(True)
            rv.addWidget(warn_lbl)
        else:
            if not can_hard:
                self.btn_hard_reset.setEnabled(False)
                self.btn_hard_reset.setCursor(Qt.CursorShape.ArrowCursor)
                self.btn_hard_reset.setToolTip(
                    f"Hard Reset (bootloader flash) is not supported for {board_name or 'this board'}.\n"
                    "Only ESP32 and ESP8266 boards support this operation."
                )
                self.btn_hard_reset.setText("⚡ Hard Reset (Not Supported)")
            if not can_soft:
                self.btn_soft_reset.setEnabled(False)
                self.btn_soft_reset.setCursor(Qt.CursorShape.ArrowCursor)
                self.btn_soft_reset.setText("Soft Reset unavailable")
                self.btn_soft_reset.setToolTip("Soft reset requires a supported board using the Arduino framework.")

        layout.addWidget(reset_box)

        # Finish scroll setup
        self.scroll.setWidget(container)
        self._form_rows = [(cpu_row, cpu_lbl), (font_row, font_lbl), (theme_row, theme_lbl), (ed_row, ed_lbl)]
        self._autosave_row, self._reset_row = as_row, btn_row
        self._adapt_rows()
        outer_layout.addWidget(self.scroll, stretch=1)

        # ── Dialog Action Buttons (Reset Defaults / Cancel / Save) ───────────
        act_row = QHBoxLayout()

        self.btn_reset = QPushButton("Reset Defaults")
        self.btn_reset.setFixedHeight(30)
        self.btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reset.clicked.connect(self._reset_defaults)
        act_row.addWidget(self.btn_reset)

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
    def _reset_defaults(self) -> None:
        """Reset all configuration values to their factory defaults."""
        ret = QMessageBox.question(
            self,
            "Reset All Settings",
            "Are you sure you want to restore all settings to their default values?\n\n"
            "This will reset CPU Multithreading, Panel Resize, Font Size, Console Warnings, Theme, Auto-Save, and Serial Monitor options to resource-aware defaults.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        # Restore UI controls
        self.cpu_combo.setCurrentText(self._high_val)
        self.cb_g_accel.setChecked(self._default_continuous_resize)
        idx_11 = self.font_combo.findData(11)
        if idx_11 >= 0:
            self.font_combo.setCurrentIndex(idx_11)
        self.cb_hide_warnings.setChecked(False)

        self.cb_theme_system.setChecked(False)
        self.theme_combo.setEnabled(True)
        self.theme_combo.setCurrentText(self._theme_rev["default"])

        self.cb_autosave.setChecked(False)
        self.autosave_spin.setValue(1500)
        self.cb_reset_on_baud.setChecked(False)

        # Apply and save
        self._save_and_apply()
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
            self.theme_combo.setCurrentText(self._theme_rev.get(detected, "Glass (Smoked blue)"))
            self.theme_combo.blockSignals(False)
        else:
            self.theme_combo.setEnabled(True)
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText(self._theme_rev.get(self._last_manual_theme, self._theme_rev["default"]))
            self.theme_combo.blockSignals(False)

    def _reset_target(self, hard: bool = False):
        """Recheck live state before a destructive reset confirmation."""
        backend = self._backend
        if not backend or getattr(backend, "is_busy", False) or getattr(backend, "active_operation", None):
            QMessageBox.warning(self, "Reset unavailable", "Wait for the current operation to finish.")
            return None
        name = getattr(backend, "current_board", "")
        port = getattr(backend, "current_port", "")
        try:
            info = backend._resolve_board_info(name) if name else {}
        except Exception:
            info = {}
        caps = board_reset_capabilities(info.get("platform", ""), info.get("board", ""), name, info.get("framework", ""))
        if not port or not info or target_problem(info) or not caps.get("hard_reset_ui" if hard else "soft_reset"):
            QMessageBox.warning(self, "Reset unavailable", "Select a resolved board and serial port with a supported reset strategy. Soft Reset requires the Arduino framework.")
            return None
        return name, port, dict(info), caps

    def _run_hard_reset(self) -> None:
        target = self._reset_target(hard=True)
        if target is None:
            return
        bname, port, binfo, reset_caps = target
        plat = str(binfo.get("platform", "")).lower()
        strategy = reset_caps.get("hard_strategy")
        if strategy == "esp32_recovery":
            title = "ESP32 Full Erase + Burn Bootloader"
            msg = (
                f"This is a destructive hard reset on port '{port}'. It will erase the ENTIRE "
                "ESP32 flash, including the current application, NVS settings, OTA state, and filesystem data.\n\n"
                "After erasing, it will write only bootloader.bin and partitions.bin from "
                "the dedicated compiled recovery project, plus boot_app0. The board will be left in a clean, "
                "state ready for a fresh upload; no application will be installed.\n\n"
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
            # Unsupported board — do not present a misleading confirmation.
            # The button should already be disabled, but guard here as a safety net.
            QMessageBox.warning(
                self,
                "Hard Reset Not Supported",
                f"Hard Reset (flash erase) is not supported for board '{bname}' "
                f"(platform: '{plat}').\n\n"
                "Only ESP32 and ESP8266 boards support Hard Reset.\n"
                "For other boards, use Upload to push a new sketch.",
            )
            return

        ret = QMessageBox.question(
            self,
            title,
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes and self._backend:
            if self._reset_target(hard=True) != target:
                return
            erase = strategy == "esp8266_erase"
            self._backend.hard_reset(erase_flash=erase)
            self.accept()

    def _run_soft_reset(self) -> None:
        target = self._reset_target()
        if target is None:
            return
        bname, port, _, _ = target
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
            if self._reset_target() != target:
                return
            self._backend.soft_reset()
            self.accept()


    def _save_and_apply(self) -> None:
        """Persist all settings and emit console log confirmations."""
        data = copy.deepcopy(_load_raw_config())
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

        # 2. Splitter resize preference (legacy config key retained)
        g_accel = "ON" if self.cb_g_accel.isChecked() else "OFF"
        data["shared"]["graphics_acceleration"] = g_accel

        # 3. Monitor Font Size
        new_font_size = self.font_combo.currentData() or 11
        data["shared"]["monitor_font_size"] = new_font_size

        # 4. Hide Warnings in Build Console
        hide_warn = self.cb_hide_warnings.isChecked()
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

        # Preserve legacy editor_mode data; the Qt workspace uses offline Monaco
        # with an automatic resource profile and has no alternative engine.

        # 7. Auto-Save
        as_enabled = self.cb_autosave.isChecked()
        as_delay = self.autosave_spin.value()
        data["shared"]["autosave_enabled"] = as_enabled
        data["shared"]["autosave_delay_ms"] = as_delay

        # 8. Serial Monitor (Reset on Baud Change)
        reset_on_baud = self.cb_reset_on_baud.isChecked()
        data["shared"]["reset_on_baud_change"] = reset_on_baud
        # Persist the complete preference set once before applying it live.
        if _save_raw_config(data) is False:
            QMessageBox.critical(self, "Settings not saved", "The configuration files could not be written. Check that your user folder is writable and try again.")
            return

        from main.core.theme import Theme
        from main.qt.theme import build_stylesheet
        from PySide6.QtWidgets import QApplication
        Theme.apply_theme(active_theme)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(build_stylesheet(active_theme))
        self._apply_dialog_theme(active_theme)

        if self._backend and hasattr(self._backend, "set_reset_on_baud_change"):
            self._backend.set_reset_on_baud_change(reset_on_baud)
        if hasattr(signals, "reset_on_baud_changed"):
            signals.reset_on_baud_changed.emit(reset_on_baud)

        # Record into persistent Notification database and emit to Notifications tab
        theme_str = f"System Default ({active_theme.replace('_', ' ').title()})" if follow_sys else active_theme.replace('_', ' ').title()
        autosave_str = f"ON ({as_delay} ms)" if as_enabled else "OFF"
        warn_str = "Hidden" if hide_warn else "Visible"
        reset_baud_str = "Enabled" if reset_on_baud else "Disabled"

        notif_msg = (
            f"• CPU Multithreading: {cpu_key}\n"
            f"• Continuous Panel Resize: {g_accel}\n"
            f"• Editor & Monitor Font: {new_font_size} pt\n"
            f"• Console Warnings: {warn_str}\n"
            f"• Theme Mode: {theme_str}\n"
            f"• Auto-Save: {autosave_str}\n"
            f"• Reset on Baud Change: {reset_baud_str}"
        )
        history_saved = False
        notification_id = "notif_" + uuid4().hex
        try:
            from src.dbs import dbs_create
            history_saved = dbs_create.add_notification(
                category="system",
                level="success",
                title="Settings Applied",
                message=notif_msg,
                notification_id=notification_id,
            ) is not None
        except Exception:
            pass

        signals.notification.emit({
            "title": "Settings Applied",
            "id": notification_id,
            "message": (f"Preferences updated successfully ({new_font_size} pt font)." if history_saved
                        else "Preferences saved, but the notification could not be saved to activity history."),
            "history_message": notif_msg if history_saved else "Preferences saved, but the notification could not be saved to activity history.",
            "type": "success" if history_saved else "warning",
        })

        # Apply font size and theme live to child panels
        signals.font_size_changed.emit(new_font_size)
        signals.theme_changed.emit(active_theme)

        # Apply hide warnings live to Build Console
        if hasattr(signals, "hide_warnings_changed"):
            signals.hide_warnings_changed.emit(hide_warn)

        # Apply autosave settings live to Monaco Editor
        if hasattr(signals, "autosave_settings_changed"):
            signals.autosave_settings_changed.emit(as_enabled, as_delay)

        # Apply continuous resizing live to splitters.
        if hasattr(signals, "graphics_accel_changed"):
            signals.graphics_accel_changed.emit(g_accel == "ON")

        self.accept()
