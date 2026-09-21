#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.signals — Shared Qt signal bus for MCU Flasher by Naph.

All backend events (console output, serial data, compile progress, port
updates, etc.) that previously went through pywebview evaluate_js() are
emitted here as typed PySide6 signals.  UI panels connect to these signals
on the main thread and update their widgets safely — no polling, no JS.

Signal naming convention mirrors the original JS event names:
    "console:log"       → console_log
    "serial:log"        → serial_log
    "operation:phase"   → operation_phase
    etc.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class MCUSignals(QObject):
    """Central signal bus singleton.  Instantiated once in MCUBackend and
    shared with all Qt panels via dependency injection."""

    # ── Console (Build Log) ──────────────────────────────────────────────────
    # Payload: {"text": str, "tag": str, "newline": bool}
    console_log = Signal(dict)
    # Payload: {"action": str, "percent": float}
    console_progress = Signal(dict)
    # Emitted when the console should be fully cleared
    console_clear = Signal()

    # ── Serial Monitor ───────────────────────────────────────────────────────
    # Payload: {"text": str, "tag": str, "newline": bool}
    serial_log = Signal(dict)
    # Payload: {"connected": bool, "port": str, "baud": int}
    serial_status = Signal(dict)
    # Emitted when the serial console should be fully cleared
    serial_clear = Signal()

    # ── Operation State ──────────────────────────────────────────────────────
    # Payload: {"phase": str, "is_busy": bool}
    #   phase values: "idle" | "compile" | "upload" | "flash" | "reset"
    operation_phase = Signal(dict)
    # Emitted to toggle native OS [X] close button and Alt+F4 during flash writes
    window_closable = Signal(bool)

    # ── Ports ────────────────────────────────────────────────────────────────
    # Payload: list[{"device": str, "description": str, "hwid": str}]
    ports_updated = Signal(list)

    # ── Board ────────────────────────────────────────────────────────────────
    # Payload: {"board_name": str}
    board_selected = Signal(dict)

    # ── Compatible Devices ───────────────────────────────────────────────────
    # Payload: {"lines": list[tuple[str, str]], "status": str}
    compat_devices_updated = Signal(dict)
    # Payload: {"title": str, "message": str, "severe": bool, "callback": Callable[[bool], None]}
    compat_confirm_requested = Signal(dict)

    # ── Project ──────────────────────────────────────────────────────────────
    # Payload: {"path": str, "name": str, "files": list, "active_file": str}
    project_updated = Signal(dict)

    # ── Syntax Diagnostics ───────────────────────────────────────────────────
    # Payload: list[diagnostic dicts]
    syntax_errors = Signal(list)

    # ── Notifications ────────────────────────────────────────────────────────
    # Payload: {"title": str, "message": str, "type": str}
    #   type: "info" | "success" | "warning" | "error"
    notification = Signal(dict)

    # ── Telemetry (CPU / RAM) ────────────────────────────────────────────────
    # Payload: {"cpu_percent": float, "ram_free_gb": float}
    telemetry = Signal(dict)

    # ── Editor (Monaco ↔ Python) ─────────────────────────────────────────────
    # Request editor to load a file
    editor_load_file = Signal(str)
    # Request editor to navigate to a specific file and line
    editor_goto_line = Signal(str, int)
    # Editor reports content changed (file_path)
    editor_content_changed = Signal(str)
    # Request editor theme change
    editor_set_theme = Signal(str)

    # ── Options Signals ──────────────────────────────────────────────────────
    timestamp_toggled = Signal(bool)
    skip_compile_availability_changed = Signal(bool)
    clear_on_action_changed = Signal(bool)
    clear_serial_on_action_changed = Signal(bool)

    # ── Toolbar Action Signals ───────────────────────────────────────────────
    # Request editor to reload current file from disk
    file_reload_requested = Signal()
    # Request main window to open Modify Files dialog
    modify_files_requested = Signal()

    # ── Display / Settings Signals ───────────────────────────────────────────
    font_size_changed = Signal(int)
    theme_changed = Signal(str)


# Module-level singleton — import and use directly:
#   from main.qt.signals import signals
#   signals.console_log.connect(my_slot)
signals = MCUSignals()
