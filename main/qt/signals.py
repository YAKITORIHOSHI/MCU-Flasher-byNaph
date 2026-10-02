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

import threading
from PySide6.QtCore import QObject, Signal, Slot, QTimer, Qt


class MCUSignals(QObject):
    """Central signal bus singleton.  Instantiated once in MCUBackend and
    shared with all Qt panels via dependency injection."""

    _logs_ready = Signal()

    def __init__(self):
        super().__init__()
        from main.qt.log_buffer import LogBuffer
        from src.modules.runtime_resources import performance_profile
        self._log_interval = performance_profile().terminal_interval_ms
        limit = 256_000 if performance_profile().constrained else 1_000_000
        self._pending_logs = {name: LogBuffer(limit, 2000, lambda item: item.get("text", "")) for name in ("console", "serial")}
        self._log_lock = threading.Lock()
        self._log_wake_pending = False
        self._log_timer = None
        self._logs_ready.connect(self._start_log_timer, Qt.ConnectionType.QueuedConnection)

    def queue_log(self, stream, payload):
        """Bound worker output before it can fill Qt's cross-thread event queue."""
        from main.qt.log_buffer import display_text
        wake = False
        with self._log_lock:
            buffer = self._pending_logs[stream]
            lines = payload.get("lines")
            if isinstance(lines, list):
                for line in lines:
                    buffer.append({"text": display_text(line), "tag": payload.get("tag", "normal"), "newline": True})
            else:
                item = dict(payload)
                item["text"] = display_text(item.get("text", ""))
                buffer.append(item)
            if buffer and not self._log_wake_pending:
                self._log_wake_pending = True
                wake = True
        if wake:
            self._logs_ready.emit()

    def clear_log_queue(self, stream):
        with self._log_lock:
            self._pending_logs[stream].clear()

    @Slot()
    def _start_log_timer(self):
        if self._log_timer is None:
            self._log_timer = QTimer(self)
            self._log_timer.setInterval(self._log_interval)
            self._log_timer.timeout.connect(self._flush_log_signals)
        self._log_timer.start()

    @Slot()
    def _flush_log_signals(self):
        with self._log_lock:
            batches = {}
            for stream, buffer in self._pending_logs.items():
                batch = buffer.drain(max_items=64, max_chars=16384)
                notice = buffer.take_notice()
                if notice:
                    batch.insert(0, {"text": notice, "tag": "warning", "newline": True})
                batches[stream] = batch
            empty = not any(self._pending_logs.values())
            if empty:
                self._log_wake_pending = False
        for stream, batch in batches.items():
            signal = self.console_log if stream == "console" else self.serial_log
            for payload in batch:
                signal.emit(payload)
        if empty and self._log_timer is not None:
            self._log_timer.stop()

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
    # Explicit selection/clear, independent of enumeration and monitor connection.
    port_selected = Signal(dict)

    # ── Board ────────────────────────────────────────────────────────────────
    # Payload: {"board_name": str}
    board_selected = Signal(dict)
    board_catalog_updated = Signal(dict)

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
    reset_on_baud_changed = Signal(bool)

    # ── Toolbar Action Signals ───────────────────────────────────────────────
    # Request editor to reload current file from disk
    file_reload_requested = Signal()
    # Request main window to open Modify Files dialog
    modify_files_requested = Signal()

    # ── Display / Settings Signals ───────────────────────────────────────────
    font_size_changed = Signal(int)
    theme_changed = Signal(str)
    hide_warnings_changed = Signal(bool)
    autosave_settings_changed = Signal(bool, int)
    graphics_accel_changed = Signal(bool)

    # ── AI Review Signals ────────────────────────────────────────────────────
    ai_review_requested = Signal(str)
    ai_review_resolved = Signal(str)


# Module-level singleton — import and use directly:
#   from main.qt.signals import signals
#   signals.console_log.connect(my_slot)
signals = MCUSignals()
