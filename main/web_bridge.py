#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Web Architecture Bridge (Python Backend)
Provides the unified, thread-safe backend engine and JS-RPC bridge for the HTML/Web UI.
"""
from __future__ import annotations

import sys
import os
import time
import json
import re
import shutil
import threading
import subprocess
import hashlib
import textwrap
import queue
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Add project root and modules to sys.path
_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent if _this_file.parent.name == "main" else _this_file.parent
_modules_path = _project_root / "src" / "modules"
_main_path = _project_root / "main"

for _p in (_project_root, _modules_path, _main_path):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCRIPT_DIR = _project_root

import serial
import serial.tools.list_ports
import psutil

try:
    # pyrefly: ignore [missing-import]
    import webview
except ImportError:
    webview = None

from main.core.constants import (
    is_application_codebase_dir, MAX_BAUD_RATE, VALID_BAUD_RATES, DEFAULT_UPLOAD_SPEED,
    SCRIPT_DIR, board_reset_capabilities, default_monitor_baud,
)
from main.core.config import (
    load_gui_config, save_gui_config, load_recent_projects, add_recent_project,
    load_recent_boards, add_recent_board, get_theme_mode, set_theme_mode,
    _try_acquire_reset_cache_lock, _release_reset_cache_lock, port_occupied_owner,
    _load_raw_config, _save_raw_config,
    find_project_window, focus_project_window, set_active_sketch_dir,
    get_project_remembered_board, set_project_remembered_board,
    get_reset_on_baud_change, set_reset_on_baud_change,
)
from main.core.file_utils import (
    get_sketch_files_fast, ensure_file_writable, get_project_build_cache_root,
    ensure_hidden_read_first_md, hide_generated_directory, hide_hidden_attribute,
    hide_internal_project_metadata,
    get_project_root_source_files, robust_rmtree,
    is_unc_or_network_path, _unc_share_root, classify_platformio_failure,
    retry_transient_file_operation,
)
from main.core.toolchain import (
    find_pio_executable, ensure_platformio, _refresh_platformio_core_environment,
    get_optimal_compiler_jobs, _max_cpu_jobs,
    board_toolchain_ready, prepare_platformio_board_toolchain,
    ensure_scons_ready,
)
from main.core.board_catalog import (
    SUPPORTED_BOARDS, _enrich_chip_features, _parse_esptool_write_progress,
    _parse_esptool_image_start, _parse_esptool_compressed, _parse_esptool_wrote,
    _strip_terminal_escapes,
)
from main.core.board_compat import (
    is_s3_board, normalized_board_flash_mode,
    normalized_board_memory_options, normalized_board_memory_type,
    _analyze_gpio_compatibility, _board_family, _format_compat_label,
    detect_board_compatibility,
)

try:
    from main.qt.signals import signals as _qt_signals_bus
except ImportError:
    _qt_signals_bus = None

if sys.platform == "win32":
    try:
        from win_subprocess_hide import install as _install_subprocess_hide, install_venv_site_hook as _install_site_hook
        _install_subprocess_hide()
        _install_site_hook(_project_root)
    except Exception:
        pass


class _SketchRAMCache:
    """High-performance process-wide in-memory cache for sketch sources, AST parsing, and baud rate detection.
    Prevents redundant disk I/O across frequent compile/upload cycles and Monaco editor RPCs.
    """
    def __init__(self):
        self._lock = threading.Lock()
        # path_str -> (st_size, st_mtime_ns, content_str)
        self._content_cache: dict[str, tuple[int, int, str]] = {}
        # path_str -> (st_size, st_mtime_ns, list_of_includes)
        self._includes_cache: dict[str, tuple[int, int, list[str]]] = {}
        # dir_str -> (hash_str, baud_rate_or_None)
        self._baud_cache: dict[str, tuple[str, Optional[str]]] = {}

    def get_content(self, path: Path) -> Optional[str]:
        try:
            resolved = str(path.resolve())
            st = path.stat()
            with self._lock:
                if resolved in self._content_cache:
                    size, mtime, content = self._content_cache[resolved]
                    if size == st.st_size and mtime == st.st_mtime_ns:
                        return content
            content = path.read_text(encoding="utf-8", errors="replace")
            with self._lock:
                self._content_cache[resolved] = (st.st_size, st.st_mtime_ns, content)
            return content
        except Exception:
            return None

    def set_content(self, path: Path, content: str) -> None:
        try:
            resolved = str(path.resolve())
            st = path.stat()
            with self._lock:
                self._content_cache[resolved] = (st.st_size, st.st_mtime_ns, content)
                self._includes_cache.pop(resolved, None)
        except Exception:
            pass

    def get_includes(self, path: Path) -> list[str]:
        try:
            resolved = str(path.resolve())
            st = path.stat()
            with self._lock:
                if resolved in self._includes_cache:
                    size, mtime, incs = self._includes_cache[resolved]
                    if size == st.st_size and mtime == st.st_mtime_ns:
                        return incs
            content = self.get_content(path) or ""
            detected: list[str] = []
            for m in re.finditer(r'#include\s*[<"]([^>"]+)[>"]', content):
                hdr = m.group(1).strip()
                if not hdr.endswith((".ino", ".c", ".cpp")):
                    detected.append(hdr)
            with self._lock:
                self._includes_cache[resolved] = (st.st_size, st.st_mtime_ns, detected)
            return detected
        except Exception:
            return []

    def get_baud_rate(self, dir_path: Path, current_hash: str) -> tuple[bool, Optional[str]]:
        key = str(dir_path.resolve())
        with self._lock:
            if key in self._baud_cache:
                cached_hash, baud = self._baud_cache[key]
                if cached_hash == current_hash:
                    return True, baud
        return False, None

    def set_baud_rate(self, dir_path: Path, current_hash: str, baud: Optional[str]) -> None:
        key = str(dir_path.resolve())
        with self._lock:
            self._baud_cache[key] = (current_hash, baud)

    def invalidate(self, path: Optional[Path] = None) -> None:
        with self._lock:
            if path:
                resolved = str(path.resolve())
                self._content_cache.pop(resolved, None)
                self._includes_cache.pop(resolved, None)
            else:
                self._content_cache.clear()
                self._includes_cache.clear()
                self._baud_cache.clear()


_sketch_ram_cache = _SketchRAMCache()


class MCUWebBackendAPI:
    """
    Python backend controller exposed to JavaScript (pywebview) AND to the
    native PySide6 Qt UI via the Qt signal bus.

    The ``emit()`` method now dispatches to BOTH channels:
    - Qt signals  (``main.qt.signals.signals``) — always, for the Qt UI panels
    - pywebview evaluate_js — only when ``self._window`` is set (Monaco editor
      iframe still communicates via pywebview/QWebChannel)

    All existing call sites (``self.emit("event:name", payload)``) are
    unchanged — the routing is transparent to the callers.
    """

    def __init__(self, window: Optional[Any] = None):
        self._window = window
        self._lock = threading.Lock()

        # Qt signal bus reference (bound on main thread)
        self._qt_signals: Optional[Any] = _qt_signals_bus

        # Project state (strictly user sketch folder, NEVER application codebase)
        config = load_gui_config()
        last_dir = config.get("last_sketch_dir", "")
        SCRIPT_DIR.resolve()

        default_doc = Path(os.path.expanduser("~")) / "Documents" / "example"
        if not default_doc.is_dir():
            try:
                default_doc.mkdir(parents=True, exist_ok=True)
                default_ino = default_doc / "example.ino"
                if not default_ino.exists():
                    default_ino.write_text(
                        "void setup() {\n"
                        "  Serial.begin(115200);\n"
                        "  Serial.println(\"Hello from MCU Flasher by Naph!\");\n"
                        "}\n\n"
                        "void loop() {\n"
                        "  delay(1000);\n"
                        "}\n",
                        encoding="utf-8",
                    )
            except Exception:
                pass

        if last_dir and Path(last_dir).is_dir() and not is_application_codebase_dir(last_dir):
            self.sketch_dir_path = Path(last_dir).resolve()
        else:
            self.sketch_dir_path = default_doc

        self.active_file_path: Optional[str] = None
        self.modified_files: dict[str, bool] = {}

        # Hardware & Toolchain state — board and port always start empty on launch (stable reference)
        self.current_board = ""
        self.current_port = ""
        try:
            raw_upload = int(config.get("upload_speed", DEFAULT_UPLOAD_SPEED))
            self.upload_speed = str(min(raw_upload, MAX_BAUD_RATE))
        except Exception:
            self.upload_speed = str(DEFAULT_UPLOAD_SPEED)
        self.skip_compile = False
        self.timestamp_enabled: bool = bool(config.get("timestamp_enabled", False))
        self.clear_console_on_action: bool = bool(config.get("clear_console_on_action", True))
        self.clear_serial_on_action: bool = bool(config.get("clear_serial_on_action", False))
        self.reset_on_baud_change: bool = bool(get_reset_on_baud_change())
        try:
            raw_baud = int(config.get("baud_rate", 115200))
            self.current_baud = min(raw_baud, MAX_BAUD_RATE)
        except Exception:
            self.current_baud = 115200
        self.is_busy = False
        self.active_operation: Optional[str] = None
        self._current_op_phase: Optional[str] = None
        self._active_board_name: Optional[str] = None
        self._active_board_info: dict[str, Any] = {}
        self._active_upload_speed: Optional[str] = None
        self._active_skip_compile: bool = False
        self._active_clear_serial_on_upload: bool = False
        self._active_monitor_baud: Optional[str] = None
        self._active_port_label: Optional[str] = None
        self._active_reset_kind: Optional[str] = None
        self._mcu_detached_during_compile: Optional[bool] = None
        self._op_session_id: int = 0
        self._stop_requested: bool = False
        self._active_process: Optional[subprocess.Popen] = None
        self._last_source_hash: str = ""
        self._last_compiled_board: str = ""

        # UNC / Network drive mapping state
        self._unc_mapped_drive: Optional[str] = None
        self._last_unc_mapping_log_key: Optional[tuple] = None

        # Serial monitor worker
        self._serial_conn: Optional[serial.Serial] = None
        self._serial_thread: Optional[threading.Thread] = None
        self.serial_running = False
        self.serial_paused = False
        self._serial_lock = threading.Lock()
        # Debounce token: incremented each time a reconnect is requested.
        # Only the call whose token matches the latest wins; stale callers abort early.
        self._serial_reconnect_token: int = 0
        # Track the last (port, baud) for which a "connected" banner was printed.
        # Prevents duplicate banners when multiple rapid reconnects land on the same pair.
        self._last_serial_connection_key: Optional[tuple] = None

        # Telemetry worker (started via start_services() once UI is ready)
        # Use threading.Event so stop requests wake the sleeping loop immediately.
        self._stop_telemetry = threading.Event()
        self._telemetry_thread: Optional[threading.Thread] = None

        # Real-time COM port monitoring worker
        self._stop_port_monitor = threading.Event()
        self._port_monitor_thread: Optional[threading.Thread] = None
        self._last_known_ports: list[dict[str, str]] = []

        # Restore project compile state & remembered board for active sketch
        self._last_synced_hardware_payload: Optional[tuple] = None
        self._restore_project_compile_state()

        # AI Review Engine & Filesystem Watcher
        from main.core.ai_review import AIReviewManager, AIEditWatcher
        self.ai_review_manager = AIReviewManager(self.sketch_dir_path)
        self.ai_watcher = AIEditWatcher(self.sketch_dir_path, self.ai_review_manager)
        self.ai_watcher.ai_edit_detected.connect(self._on_ai_edit_detected)

    def _on_ai_edit_detected(
        self,
        path: str,
        before: str,
        after: str,
        before_exists: bool,
        after_exists: bool,
    ) -> None:
        """Handle AI edit or external change detected by AIEditWatcher."""
        file_name = Path(path).name
        # Emit Qt signal to editor to reload active file with diff
        sig_bus = self._get_qt_signals()
        if sig_bus and hasattr(sig_bus, "ai_review_requested"):
            sig_bus.ai_review_requested.emit(path)
        # Also post system notification
        self.emit("notification", {
            "title": "AI Edit Detected",
            "message": f"AI edit detected: {file_name}. Review changes in editor.",
            "type": "info",
        })

    def _block_if_pending_ai_edits(self, action_name: str) -> bool:
        """Check if unreviewed AI edits exist. If so, pause action and prompt review."""
        if not hasattr(self, "ai_review_manager") or not self.ai_review_manager:
            return False
        if not self.ai_review_manager.has_any_pending_ai_edits():
            return False
        reviews = self.ai_review_manager.get_ai_edit_reviews()
        count = len(reviews)
        noun = "edit" if count == 1 else "edits"
        self.emit("console:log", {
            "text": f"⚠ {action_name} paused: please review and accept/decline {count} pending AI {noun} first.",
            "tag": "warning",
            "newline": True,
        })
        self.emit("notification", {
            "title": "AI Review Required",
            "message": f"{action_name} paused: please review {count} pending AI {noun} first.",
            "type": "warning",
        })
        sig_bus = self._get_qt_signals()
        if reviews and sig_bus and hasattr(sig_bus, "ai_review_requested"):
            sig_bus.ai_review_requested.emit(reviews[0].get("path", ""))
        return True

    def start_services(self) -> None:
        """Start background telemetry and hardware monitoring after the UI is ready."""
        if self._telemetry_thread is None or not self._telemetry_thread.is_alive():
            self._stop_telemetry.clear()
            self._telemetry_thread = threading.Thread(
                target=self._telemetry_loop, name="MCU_Telemetry", daemon=True
            )
            self._telemetry_thread.start()
        if self._port_monitor_thread is None or not self._port_monitor_thread.is_alive():
            self._stop_port_monitor.clear()
            self._port_monitor_thread = threading.Thread(
                target=self._port_monitor_loop, name="MCU_PortMonitor", daemon=True
            )
            self._port_monitor_thread.start()
        self._init_hardware()
        self._sync_project_hardware_state()

    def stop_services(self) -> None:
        """Signal all persistent background workers to stop and wait for them to exit.

        Safe to call from any thread.  Blocks for at most ~4 s total.
        """
        self._stop_telemetry.set()
        self._stop_port_monitor.set()
        try:
            self._stop_serial_monitor()
        except Exception:
            pass
        for t, name in (
            (self._telemetry_thread, "MCU_Telemetry"),
            (self._port_monitor_thread, "MCU_PortMonitor"),
        ):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)
        if hasattr(self, "ai_watcher") and self.ai_watcher:
            try:
                self.ai_watcher._timer.stop()
            except Exception:
                pass
        if hasattr(self, "ai_review_manager") and self.ai_review_manager:
            try:
                self.ai_review_manager.shutdown()
            except Exception:
                pass

    def _get_qt_signals(self) -> Optional[Any]:
        """Return the Qt signal bus."""
        if self._qt_signals is not None:
            return self._qt_signals
        return _qt_signals_bus

    def set_window(self, window: Any):
        """Bind the pywebview window instance for JS event dispatching."""
        self._window = window

    def emit(self, event_name: str, data: Any):
        """Dispatch a real-time event to the Qt UI and/or the JS frontend.

        Routing table
        ─────────────
        Qt signal bus  — always attempted; silently skipped if PySide6 not yet
                         installed (bootstrap first-run protection).
        pywebview JS   — only when ``self._window`` is set (Monaco editor
                         iframe uses this channel for editor events).
        """
        # ── 1. Qt signal dispatch ──────────────────────────────────────────
        bus = self._get_qt_signals()
        if bus is not None:
            try:
                # Apply timestamp metadata for console logs
                payload = data
                if event_name == "console:log" and isinstance(data, dict):
                    payload = dict(data)
                    if "timestamp" not in payload:
                        payload["timestamp"] = time.strftime("[%H:%M:%S]")
                    orig_text = str(payload.get("text", ""))
                    m = re.match(r"^(\[\d+:\d+:\d+\])\s*(.*)$", orig_text)
                    if m:
                        payload["timestamp"] = m.group(1)
                        payload["text"] = m.group(2)

                _EVENT_TO_SIGNAL = {
                    "console:log":      bus.console_log,
                    "console:progress": bus.console_progress,
                    "console:clear":    None,            # special: emit() with no data
                    "serial:log":       bus.serial_log,
                    "serial:status":    bus.serial_status,
                    "serial:clear":     None,
                    "operation:phase":  bus.operation_phase,
                    "ports:updated":    bus.ports_updated,
                    "board:selected":   bus.board_selected,
                    "project:updated":  bus.project_updated,
                    "syntax:errors":    bus.syntax_errors,
                    "notification":     bus.notification,
                    "telemetry":        bus.telemetry,
                    "timestamp:toggled": getattr(bus, "timestamp_toggled", None),
                    "skip_compile:availability": getattr(bus, "skip_compile_availability_changed", None),
                    "window:closable": getattr(bus, "window_closable", None),
                    "compat_devices:updated": getattr(bus, "compat_devices_updated", None),
                    "compat_confirm:requested": getattr(bus, "compat_confirm_requested", None),
                }
                sig = _EVENT_TO_SIGNAL.get(event_name)
                if sig is not None:
                    if event_name == "window:closable":
                        closable_val = data.get("closable", True) if isinstance(data, dict) else bool(data)
                        sig.emit(closable_val)
                    else:
                        sig.emit(payload)
                elif event_name == "console:clear":
                    bus.console_clear.emit()
                elif event_name == "serial:clear":
                    bus.serial_clear.emit()
            except Exception:
                pass

        # Persistent notification storage
        if event_name == "notification" and isinstance(data, dict):
            try:
                from src.dbs import dbs_create
                dbs_create.add_notification(
                    category=data.get("category", "system"),
                    level=data.get("type", "info"),
                    title=data.get("title", "Notification"),
                    message=data.get("message", ""),
                )
            except Exception:
                pass

        # ── 2. pywebview JS dispatch (Monaco editor iframe) ────────────────
        if not self._window:
            return
        try:
            payload_json = json.dumps(data, ensure_ascii=False)
            js_code = f"if (window.__onMcuEvent) {{ window.__onMcuEvent({json.dumps(event_name)}, {payload_json}); }}"
            self._window.evaluate_js(js_code)
        except Exception:
            pass


    def _telemetry_loop(self):
        """Periodically emit CPU & RAM telemetry to the status bar.

        Uses ``_stop_telemetry`` (threading.Event) so the loop wakes
        immediately on shutdown rather than blocking for the full interval.
        """
        while not self._stop_telemetry.is_set():
            try:
                cpu = psutil.cpu_percent(interval=None)  # non-blocking snapshot
                mem = psutil.virtual_memory()
                free_gb = round(mem.available / (1024 ** 3), 1)
                self.emit("telemetry", {
                    "cpu_percent": cpu,
                    "ram_free_gb": free_gb,
                })
            except Exception:
                pass  # telemetry is cosmetic — never crash the worker
            # Wait interruptibly: wakes immediately when stop is requested
            self._stop_telemetry.wait(timeout=2.0)

    def _port_monitor_loop(self):
        """Dedicated background thread: monitor serial COM ports in real-time for USB hotplug.

        Uses ``_stop_port_monitor`` (threading.Event) for clean, immediate shutdown.

        Poll strategy
        ─────────────
        • Normal idle          : 1.5 s  (responsive to hotplug)
        • During active op     : 3.0 s  (reduce churn during compile/flash)
        • After a port change  : 0.3 s re-check to confirm state is stable
          before sending notifications (avoids false-positive on transient glitches)
        """
        while not self._stop_port_monitor.is_set():
            try:
                ports = self._scan_ports()
                current_devs = {p["device"] for p in ports}
                last_devs = {p["device"] for p in self._last_known_ports}

                if current_devs != last_devs:
                    # Brief re-check to filter transient WMI / driver-settle glitches
                    self._stop_port_monitor.wait(timeout=0.3)
                    if self._stop_port_monitor.is_set():
                        break
                    ports = self._scan_ports()
                    current_devs = {p["device"] for p in ports}

                    added_devs = current_devs - last_devs
                    removed_devs = last_devs - current_devs
                    self._last_known_ports = list(ports)
                    self.emit("ports:updated", ports)

                    if added_devs:
                        for dev in sorted(added_devs):
                            p_info = next((p for p in ports if p["device"] == dev), None)
                            desc = f" ({p_info['description']})" if p_info and p_info.get("description") else ""
                            self.emit("notification", {
                                "title": "Hardware Connected",
                                "message": f"USB device connected: {dev}{desc}",
                                "type": "success",
                            })
                    if removed_devs:
                        for dev in sorted(removed_devs):
                            self.emit("notification", {
                                "title": "Hardware Disconnected",
                                "message": f"USB device disconnected: {dev}",
                                "type": "warning",
                            })
                            if self.current_port == dev:
                                self._stop_serial_monitor()
                                self.current_port = ""
                                self.emit("serial:status", {
                                    "connected": False,
                                    "port": "",
                                    "baud": self.current_baud,
                                })
                                self._sync_project_hardware_state()
            except Exception:
                pass  # never crash the monitor; back-off handled by wait below

            # Adaptive poll interval: back off during active build/flash operations
            poll_interval = 3.0 if getattr(self, "is_busy", False) else 1.5
            self._stop_port_monitor.wait(timeout=poll_interval)

    def _init_hardware(self):
        """Detect initial COM ports and emit the list.

        Per stable reference (hardware_port_mixin.py:415), neither board
        nor port are auto-selected on launch — user must choose manually.
        """
        ports = self._scan_ports()
        self._last_known_ports = list(ports)
        sig = self._get_qt_signals()
        if sig:
            sig.ports_updated.emit(ports)

    # ──────────────────────────────────────────────────────────
    # JS-RPC: INITIAL STATE & CONFIG
    # ──────────────────────────────────────────────────────────
    def get_initial_state(self) -> dict[str, Any]:
        """Return the complete snapshot of application state on load."""
        files = self.get_project_files()
        active_f = self.active_file_path or (files[0]["path"] if files else "")
        self.active_file_path = active_f

        board_names = sorted(list(SUPPORTED_BOARDS.keys()))
        ports = self._scan_ports()
        recents = load_recent_projects()
        config = load_gui_config()

        return {
            "project": {
                "path": str(self.sketch_dir_path),
                "name": self.sketch_dir_path.name,
                "files": files,
                "active_file": active_f,
            },
            "hardware": {
                "boards": board_names,
                "selected_board": self.current_board,
                "ports": ports,
                "selected_port": self.current_port,
                "baud_rate": self.current_baud,
                "baud_rates": sorted(list(VALID_BAUD_RATES)),
            },
            "settings": {
                "compiler_jobs": int(config.get("compiler_jobs", _max_cpu_jobs) or 4),
                "clear_console": config.get("clear_console", True),
                "autosave_delay_ms": config.get("autosave_delay_ms", 1000),
                "theme_mode": get_theme_mode(),
            },
            "recent_projects": recents,
            "recent_boards": load_recent_boards(),
        }

    # ──────────────────────────────────────────────────────────
    # JS-RPC: PROJECT MANAGEMENT & FILES
    # ──────────────────────────────────────────────────────────
    def get_project_files(self) -> list[dict[str, Any]]:
        """Return all editable files in the current sketch folder."""
        if not self.sketch_dir_path or not self.sketch_dir_path.is_dir() or is_application_codebase_dir(self.sketch_dir_path):
            return []
        try:
            paths = list(get_sketch_files_fast(self.sketch_dir_path, supported_extensions={".ino", ".cpp", ".c", ".h", ".hpp", ".txt"}))
            # Sort main sketch first
            paths.sort(key=lambda p: (0 if p.suffix.lower() == ".ino" else 1, p.name.lower()))
            res = []
            for p in paths:
                res.append({
                    "name": p.name,
                    "path": str(p),
                    "extension": p.suffix.lower(),
                    "is_main": p.suffix.lower() == ".ino",
                    "is_modified": self.modified_files.get(str(p), False),
                })
            return res
        except Exception:
            return []

    def read_file(self, file_path: str) -> dict[str, Any]:
        """Read text content of a sketch file for Monaco editor."""
        try:
            p = Path(file_path).resolve()
            content = _sketch_ram_cache.get_content(p)
            if content is not None:
                return {"content": content, "success": True}
            with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
                content = f.read()
            _sketch_ram_cache.set_content(p, content)
            return {"content": content, "success": True}
        except Exception as e:
            return {"content": f"/* Error reading file: {e} */", "error": str(e), "success": False}

    def save_file(self, file_path: str, content: str) -> dict[str, Any]:
        """Save text content safely to a sketch file."""
        try:
            p = Path(file_path).resolve()
            ensure_file_writable(p)
            def _write():
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
            retry_transient_file_operation(_write, attempts=6, delay=0.08)
            _sketch_ram_cache.set_content(p, content)
            self.modified_files[str(p)] = False
            self.update_skip_compile_availability()
            if hasattr(self, "ai_watcher") and self.ai_watcher:
                self.ai_watcher.note_user_save(p, content)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def save_all_files(self) -> dict[str, Any]:
        """Signal Monaco editor to commit all open buffers."""
        return {"success": True}

    def mark_modified(self, file_path: str, is_modified: bool = True):
        """Track dirty state of files."""
        self.modified_files[str(file_path)] = bool(is_modified)
        if is_modified:
            try:
                _sketch_ram_cache.invalidate(Path(file_path))
            except Exception:
                pass
            self.skip_compile = False
            self.emit("skip_compile:availability", False)

    def set_active_file(self, file_path: str):
        """Set the active file path."""
        self.active_file_path = str(file_path)

    def get_project_dir(self) -> str:
        """Return the current project root path."""
        return str(self.sketch_dir_path)

    def save_tab_order(self, paths: list[str]):
        """Persist tab order."""
        try:
            cache_dir = get_project_build_cache_root(self.sketch_dir_path)
            order_file = cache_dir / ".mcu_flash_tab_order.json"
            order_file.write_text(json.dumps(paths), encoding="utf-8")
        except Exception:
            pass

    def set_upload_speed(self, speed: str | int) -> None:
        """Set the upload baud rate, capped at MAX_BAUD_RATE (921600)."""
        try:
            val = int(speed)
            self.upload_speed = str(min(val, MAX_BAUD_RATE))
        except (ValueError, TypeError):
            self.upload_speed = str(speed)

    def set_skip_compile(self, skip: bool) -> None:
        """Set whether upload should reuse cached build without compiling."""
        self.skip_compile = bool(skip)

    def set_timestamp_enabled(self, enabled: bool) -> None:
        """Toggle console log timestamping."""
        self.timestamp_enabled = bool(enabled)
        cfg = load_gui_config()
        cfg["timestamp_enabled"] = self.timestamp_enabled
        save_gui_config(cfg)
        self.emit("timestamp:toggled", self.timestamp_enabled)

    # ── Remote / UNC Network Path Support ─────────────────────────────────
    def _remote_workspace_root(self, project_dir: Optional[Path] = None) -> Optional[Path]:
        """For a remote/UNC project (e.g. \\\\server\\share\\... or mapped network drives),
        return the local fast workspace root on the local SSD.

        Building intermediate objects and SCons signature databases (.sconsign*.dblite)
        directly over SMB/network shares causes file locking failures and network latency.
        Routing remote project workspaces to local storage guarantees 100% reliable builds
        and high-speed compilation while preserving the remote source files.

        Returns None for local drive projects (which build in the hidden
        project/.mcu_flasher_build_cache folder).
        """
        target = Path(project_dir or self.sketch_dir_path)
        if not is_unc_or_network_path(target):
            return None
        proj_hash = hashlib.sha1(str(target).lower().encode("utf-8")).hexdigest()[:12]
        proj_name = re.sub(r'[^A-Za-z0-9_.-]', '_', target.name) or "project"
        core_store = os.environ.get("PLATFORMIO_CORE_DIR")
        base = Path(core_store) if core_store else SCRIPT_DIR
        return base / "remote_workspaces" / f"{proj_name}_{proj_hash}"

    def _effective_cache_root(self, project_dir: Optional[Path] = None) -> Path:
        """Return the effective build cache root for this sketch.
        Uses fast local storage for remote/UNC network projects, and the project's
        hidden .mcu_flasher_build_cache directory for local sketches.
        """
        target = Path(project_dir or self.sketch_dir_path)
        remote_root = self._remote_workspace_root(target)
        if remote_root is not None:
            remote_root.mkdir(parents=True, exist_ok=True)
            try:
                hide_generated_directory(remote_root.parent)
                hide_generated_directory(remote_root)
            except Exception:
                pass
            return remote_root
        return get_project_build_cache_root(target)

    def _map_unc_for_build(self, project_path: Optional[Path] = None) -> Path:
        """If the sketch is on a UNC share, map it to a temporary drive letter.

        Returns the effective project path with the mapped drive letter.
        Stores cleanup state in self._unc_mapped_drive so _unmap_unc_after_build()
        can cleanly undo it in finally blocks.
        """
        project = Path(project_path or self.sketch_dir_path)
        if not is_unc_or_network_path(project):
            return project

        share_root = _unc_share_root(project)
        if not share_root:
            return project

        owned_drive = getattr(self, "_unc_mapped_drive", None)
        if owned_drive:
            try:
                if os.path.exists(f"{owned_drive}\\"):
                    relative_part = str(project).replace("/", "\\")
                    share_norm = share_root.rstrip("\\")
                    if relative_part.lower().startswith(share_norm.lower()):
                        relative_part = relative_part[len(share_norm):]
                    return Path(f"{owned_drive}{relative_part}")
            except Exception:
                pass
            self._unc_mapped_drive = None

        # Check if the share is already mapped to an existing drive letter via `net use`
        try:
            existing = subprocess.run(
                ["net", "use"], capture_output=True, text=True, timeout=10,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
            share_lower = share_root.lower().rstrip("\\")
            for line in existing.stdout.splitlines():
                parts = line.split()
                for idx, part in enumerate(parts):
                    if len(part) == 2 and part[1] == ":" and part[0].isalpha():
                        if idx + 1 < len(parts) and parts[idx + 1].lower().rstrip("\\") == share_lower:
                            drive_spec = part.upper()
                            relative_part = str(project).replace("/", "\\")
                            share_norm = share_root.rstrip("\\")
                            if relative_part.lower().startswith(share_norm.lower()):
                                relative_part = relative_part[len(share_norm):]
                            mapped_path = Path(f"{drive_spec}{relative_part}")
                            mapping_log_key = (drive_spec, share_root.lower().rstrip("\\"))
                            if getattr(self, "_last_unc_mapping_log_key", None) != mapping_log_key:
                                self.emit("console:log", {
                                    "text": f"  🌐 Using existing drive mapping {drive_spec} → {share_root}",
                                    "tag": "info", "newline": True
                                })
                                self._last_unc_mapping_log_key = mapping_log_key
                            return mapped_path
        except Exception:
            pass

        # Find a free drive letter (Z: down to A:)
        import string
        mapped_letter = None
        for letter in reversed(string.ascii_uppercase):
            test_root = f"{letter}:\\"
            if not os.path.exists(test_root):
                mapped_letter = letter
                break

        if mapped_letter is None:
            self.emit("console:log", {
                "text": "  ⚠ Could not find a free drive letter for the network share — accessing via UNC directly.",
                "tag": "warning", "newline": True
            })
            return project

        drive_spec = f"{mapped_letter}:"
        try:
            map_cmd = ["net", "use", drive_spec, share_root]
            map_result = subprocess.run(
                map_cmd, capture_output=True, text=True, timeout=30,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
            if map_result.returncode != 0:
                self.emit("console:log", {
                    "text": f"  ⚠ Could not map {share_root} → {drive_spec} ({map_result.stderr.strip()}) — accessing via UNC directly.",
                    "tag": "warning", "newline": True
                })
                return project

            self._unc_mapped_drive = drive_spec
            self._last_unc_mapping_log_key = (drive_spec, share_root.lower().rstrip("\\"))
            self.emit("console:log", {
                "text": f"  🌐 Mapped network share → {drive_spec} (temporary, for this build session)",
                "tag": "info", "newline": True
            })

            relative_part = str(project).replace("/", "\\")
            share_norm = share_root.rstrip("\\")
            if relative_part.lower().startswith(share_norm.lower()):
                relative_part = relative_part[len(share_norm):]
            return Path(f"{drive_spec}{relative_part}")
        except Exception as exc:
            self.emit("console:log", {
                "text": f"  ⚠ Drive-mapping failed ({exc}) — accessing via UNC directly.",
                "tag": "warning", "newline": True
            })
            return project

    def _unmap_unc_after_build(self) -> None:
        """Remove the temporary drive mapping created by _map_unc_for_build.
        Safe to call even if no mapping was created (no-op).
        """
        drive_spec = getattr(self, "_unc_mapped_drive", None)
        if not drive_spec:
            return
        try:
            unmap_cmd = ["net", "use", drive_spec, "/delete", "/y"]
            subprocess.run(
                unmap_cmd, capture_output=True, text=True, timeout=15,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
            self.emit("console:log", {
                "text": f"  🌐 Unmapped temporary drive {drive_spec}",
                "tag": "dim", "newline": True
            })
        except Exception:
            pass
        finally:
            self._unc_mapped_drive = None

    def _find_cached_firmware_binary(self, board_name: str | None = None) -> Optional[Path]:
        """Find precompiled binary (firmware.bin, .hex, or .elf) across all cache locations."""
        if not self.sketch_dir_path:
            return None
        target_board = board_name or self.current_board or getattr(self, "_last_compiled_board", "")
        try:
            cache_root = self._effective_cache_root(self.sketch_dir_path)
            candidate_dirs = [
                cache_root / ".pio" / "build" / "mcu_env",
            ]
            if target_board:
                candidate_dirs.append(cache_root / ".pio" / "build" / self._pio_env_name(target_board))
                candidate_dirs.append(
                    cache_root / "boards" / self._board_cache_key(target_board) / ".pio" / "build" / self._pio_env_name(target_board)
                )

            pio_build = cache_root / ".pio" / "build"
            if pio_build.is_dir():
                for sub in pio_build.iterdir():
                    if sub.is_dir() and sub not in candidate_dirs:
                        candidate_dirs.append(sub)

            boards_root = cache_root / "boards"
            if boards_root.is_dir():
                for b_sub in boards_root.iterdir():
                    if b_sub.is_dir():
                        sub_pio = b_sub / ".pio" / "build"
                        if sub_pio.is_dir():
                            for env_dir in sub_pio.iterdir():
                                if env_dir.is_dir() and env_dir not in candidate_dirs:
                                    candidate_dirs.append(env_dir)

            for bdir in candidate_dirs:
                if bdir.is_dir():
                    for fname in ("firmware.bin", "firmware.hex", "firmware.elf"):
                        p = bdir / fname
                        if p.is_file() and p.stat().st_size >= 1024:
                            return p
        except Exception:
            pass
        return None

    def _restore_project_compile_state(self) -> bool:
        """Inspect the active project's build cache and restore remembered board and compiled state."""
        if not self.sketch_dir_path or not self.sketch_dir_path.is_dir():
            return False
        try:
            cache_root = self._effective_cache_root(self.sketch_dir_path)
            cache_file = cache_root / ".mcu_gui_cache.json"
            if not cache_file.is_file():
                alt_file = cache_root / "compile_cache.json"
                if alt_file.is_file():
                    cache_file = alt_file

            restored_board = ""
            cached_hash = ""
            if cache_file.is_file():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        cached_board = data.get("last_board") or ""
                        cached_hash = data.get("last_source_hash") or ""
                        if "build_metadata" in data and isinstance(data["build_metadata"], dict):
                            self._build_metadata_by_board = dict(data["build_metadata"])

                        if cached_board:
                            restored_board = cached_board
                        elif "boards" in data and isinstance(data["boards"], dict):
                            latest_ts = 0.0
                            for b_key, b_info in data["boards"].items():
                                if isinstance(b_info, dict):
                                    ts = float(b_info.get("timestamp", 0.0))
                                    if ts >= latest_ts and b_info.get("board"):
                                        latest_ts = ts
                                        restored_board = str(b_info.get("board"))
                                        if b_info.get("source_hash"):
                                            cached_hash = str(b_info.get("source_hash"))
                except Exception:
                    pass

            if not restored_board:
                remembered = get_project_remembered_board(str(self.sketch_dir_path))
                if remembered and (remembered in SUPPORTED_BOARDS or remembered):
                    restored_board = remembered

            if restored_board:
                self._last_compiled_board = restored_board
                if cached_hash:
                    self._last_source_hash = str(cached_hash)

                bin_file = self._find_cached_firmware_binary(restored_board)
                if bin_file is not None and cached_hash:
                    self._load_compile_cache(restored_board)
                    return True
            else:
                self._last_compiled_board = ""
                self._last_source_hash = ""

            return False
        except Exception:
            return False

    def _save_compile_cache(self, board_name: str | None = None, source_hash: str | None = None,
                            build_metadata: dict | None = None) -> bool:
        """Persist compile cache metadata to disk in the project build cache root."""
        if not self.sketch_dir_path:
            return False
        target_board = board_name or self.current_board
        if not target_board:
            return False
        shash = source_hash or getattr(self, "_last_source_hash", "") or self._hash_sources(target_board)
        if not shash:
            return False
        try:
            cache_root = self._effective_cache_root(self.sketch_dir_path)
            cache_file = cache_root / ".mcu_gui_cache.json"
            data: dict[str, Any] = {}
            if cache_file.is_file():
                try:
                    loaded = json.loads(cache_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except Exception:
                    data = {}

            boards = data.get("boards", {})
            if not isinstance(boards, dict):
                boards = {}

            board_key = self._board_cache_key(target_board)

            entry = {
                "board": target_board,
                "source_hash": shash,
                "timestamp": time.time(),
            }
            boards[board_key] = entry
            boards[target_board] = entry

            if build_metadata:
                if not hasattr(self, "_build_metadata_by_board") or not isinstance(self._build_metadata_by_board, dict):
                    self._build_metadata_by_board = {}
                self._build_metadata_by_board[board_key] = build_metadata
                self._build_metadata_by_board[target_board] = build_metadata

            existing_bmeta = data.get("build_metadata", {})
            if isinstance(existing_bmeta, dict):
                if not hasattr(self, "_build_metadata_by_board") or not isinstance(self._build_metadata_by_board, dict):
                    self._build_metadata_by_board = {}
                for k, v in existing_bmeta.items():
                    if k not in self._build_metadata_by_board and isinstance(v, dict):
                        self._build_metadata_by_board[k] = v

            data["schema"] = 1
            data["last_board"] = target_board
            data["last_source_hash"] = shash
            data["boards"] = boards
            data["build_metadata"] = getattr(self, "_build_metadata_by_board", {})
            data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

            ensure_file_writable(cache_file)
            cache_file.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            hide_hidden_attribute(cache_file)

            # Also persist remembered board for this project directory
            if self.sketch_dir_path:
                set_project_remembered_board(str(self.sketch_dir_path), target_board)

            return True
        except Exception:
            return False

    def _load_compile_cache(self, board_name: str | None = None) -> bool:
        """Load compile cache metadata from disk for the specified or current board."""
        if not self.sketch_dir_path:
            return False
        target_board = board_name or self.current_board
        if not target_board:
            return False
        try:
            cache_root = self._effective_cache_root(self.sketch_dir_path)
            cache_file = cache_root / ".mcu_gui_cache.json"
            if not cache_file.is_file():
                alt_file = cache_root / "compile_cache.json"
                if alt_file.is_file():
                    cache_file = alt_file
                else:
                    return False

            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False

            if "build_metadata" in data and isinstance(data["build_metadata"], dict):
                self._build_metadata_by_board = dict(data["build_metadata"])

            board_key = self._board_cache_key(target_board)
            boards = data.get("boards", {})
            entry = None
            if isinstance(boards, dict):
                entry = boards.get(board_key) or boards.get(target_board)

            if entry and isinstance(entry, dict) and entry.get("source_hash"):
                self._last_source_hash = str(entry.get("source_hash", ""))
                self._last_compiled_board = str(entry.get("board", target_board))
                return True

            if data.get("last_board") == target_board and data.get("last_source_hash"):
                self._last_source_hash = str(data.get("last_source_hash", ""))
                self._last_compiled_board = str(data.get("last_board", ""))
                return True

            return False
        except Exception:
            return False

    def _needs_recompile(self, board_name: str | None = None) -> tuple[bool, str]:
        """Check whether the active sketch needs recompilation for the target board.
        Matches LATEST-WORKING-MCU- FLASHER contract:
        Returns (recompile_needed: bool, reason: str).
        """
        target_board = board_name or self.current_board
        if not target_board:
            return True, "no board selected"
        if not self.sketch_dir_path:
            return True, "no sketch folder loaded"

        try:
            # 1. Check if firmware binary exists on disk
            bin_file = self._find_cached_firmware_binary(target_board)
            if bin_file is None:
                return True, "no firmware binary found for this board (build folder may have been cleaned)"

            # 2. Check if cache exists and board matches
            board_matches = (target_board == getattr(self, "_last_compiled_board", ""))
            if not getattr(self, "_last_source_hash", "") or not board_matches:
                self._load_compile_cache(target_board)

            cached_hash = getattr(self, "_last_source_hash", "")
            if not cached_hash or target_board != getattr(self, "_last_compiled_board", ""):
                return True, "no previous compile for this board"

            # 3. Check for any dirty/unsaved buffers in Monaco
            if any(self.modified_files.values()):
                return True, "unsaved modifications in editor"

            # 4. Hash actual source files on disk
            current_hash = self._hash_sources(target_board)
            if current_hash != cached_hash:
                return True, "source files have changed since this board was last compiled"

            return False, "sources unchanged"
        except Exception as e:
            return True, f"cache check error: {e}"

    def check_can_skip_compile(self, board_name: str | None = None) -> bool:
        """Check whether a precompiled firmware binary exists and source code has not changed."""
        needs_recomp, _ = self._needs_recompile(board_name)
        return not needs_recomp

    def update_skip_compile_availability(self) -> None:
        """Evaluate whether skip compile is possible and notify the UI."""
        def _bg():
            available = self.check_can_skip_compile()
            self.emit("skip_compile:availability", available)
        threading.Thread(target=_bg, name="MCU_SkipCompileCheck", daemon=True).start()

    def check_can_skip_compile_for_upload(self, board_name: str | None = None) -> bool:
        """Synchronously check whether upload can skip compilation and flash directly."""
        # Only skip compilation if user explicitly checked Skip Compile
        if not getattr(self, "skip_compile", False):
            return False

        target_board = board_name or self.current_board
        if not target_board or not self.sketch_dir_path:
            return False

        try:
            binfo = self._resolve_board_info(target_board)
            platform = str(binfo.get("platform", "")).lower()
            if platform in ("espressif32", "espressif8266"):
                fast_bins = self._locate_soft_reset_fast_binaries(
                    self.sketch_dir_path, target_board, platform
                )
                if fast_bins is None:
                    return False

            needs_recomp, _ = self._needs_recompile(target_board)
            return not needs_recomp
        except Exception:
            return False

    def _clean_temporary_compile_artifacts(self, cache_root: Path) -> None:
        """Remove partial binaries, lock files, and temp files left behind by an interrupted compile.
        Ensures the project remains 100% recompilable and rebuildable.
        """
        try:
            env_dir = cache_root / ".pio" / "build" / "mcu_env"
            if env_dir.is_dir():
                for fname in ("firmware.bin", "firmware.hex", "firmware.elf", "firmware.map"):
                    p = env_dir / fname
                    if p.is_file():
                        try:
                            ensure_file_writable(p)
                            p.unlink(missing_ok=True)
                        except Exception:
                            pass
                for pattern in ("*.tmp", "*.lock", "*~"):
                    for tmp_f in env_dir.glob(pattern):
                        try:
                            ensure_file_writable(tmp_f)
                            tmp_f.unlink(missing_ok=True)
                        except Exception:
                            pass
            self._last_source_hash = ""
            self.update_skip_compile_availability()
        except Exception:
            pass

    def _clean_board_cache(self, board_name: str | None = None) -> bool:
        """Wipe the board build directory and SCons signature state to recover from cache corruption."""
        try:
            cache_root = self._effective_cache_root(self.sketch_dir_path)
            build_dir = cache_root / ".pio" / "build"
            if build_dir.exists():
                robust_rmtree(build_dir)
            for sconsign in cache_root.glob(".sconsign*"):
                try:
                    ensure_file_writable(sconsign)
                    sconsign.unlink(missing_ok=True)
                except Exception:
                    pass
            for sconsign in (cache_root / ".pio").glob(".sconsign*"):
                try:
                    ensure_file_writable(sconsign)
                    sconsign.unlink(missing_ok=True)
                except Exception:
                    pass
            self._clean_temporary_compile_artifacts(cache_root)
            self._last_source_hash = ""
            self.update_skip_compile_availability()
            return True
        except Exception:
            return False

    def _kill_active_process_tree(self) -> None:
        """Forcefully terminate the active compiler process and all child processes recursively.
        Prevents compiler or linker child processes from lingering and holding file locks on Windows.
        """
        proc = getattr(self, "_active_process", None)
        if not proc:
            return
        try:
            pid = proc.pid
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=5,
                )
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @staticmethod
    def _parse_size_value(value) -> int | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        multipliers = {"k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2}
        match = re.fullmatch(r"(0x[0-9a-f]+|\d+(?:\.\d+)?)\s*(kb|mb|k|m)?", text)
        if not match:
            return None
        number = match.group(1)
        base = int(number, 16) if number.startswith("0x") else float(number)
        return int(base * multipliers.get(match.group(2) or "", 1))

    def _platformio_ini_path(self, project_dir: Path | None = None, board_name: str | None = None) -> Path:
        """Return the private PlatformIO configuration for this project."""
        pdir = Path(project_dir or self.sketch_dir_path) if (project_dir or self.sketch_dir_path) else Path(".")
        cache_root = self._effective_cache_root(pdir)
        return cache_root / "platformio.ini"

    def _pio_env_name(self, board_name: str | None = None) -> str:
        """Stable PlatformIO [env:...] name / .pio/build subfolder for a given board."""
        name = board_name or getattr(self, "current_board", "") or ""
        if not name:
            return "mcu_env"
        digest = self._board_cache_key(name).rsplit("_", 1)[-1]
        return f"mcu_{digest}"

    def _project_option(self, name: str) -> str | None:
        """Read one scalar option from the generated PlatformIO environment."""
        try:
            ini_path = self._platformio_ini_path()
            if not ini_path.is_file():
                return None
            content = ini_path.read_text(encoding="utf-8", errors="replace")
            match = re.search(
                rf"^\s*{re.escape(name)}\s*=\s*([^;#\r\n]+)",
                content, re.IGNORECASE | re.MULTILINE,
            )
            return match.group(1).strip() if match else None
        except Exception:
            return None

    def _resolve_platform_upload_metadata(self) -> dict:
        """Resolve PlatformIO's exact board/debug/upload options locally."""
        board_info = self._resolve_board_info()
        platform_name = str(board_info.get("platform", "") or "")
        board_id = str(board_info.get("board", "") or "")
        configured_debug = self._project_option("debug_tool")
        cache_key = (platform_name, board_id, configured_debug or "")
        cache = getattr(self, "_platform_upload_metadata_cache", {})
        if cache_key in cache:
            return dict(cache[cache_key])

        result = {
            "debug": None,
            "available": ["esptool"],
            "current": "esptool",
            "max_ram": None,
            "max_flash": None,
            "partitions": None,
        }
        try:
            core_dir = Path(os.environ.get("PLATFORMIO_CORE_DIR", ""))
            platform_dir = core_dir / "platforms" / platform_name
            if platform_dir.is_dir():
                from platformio.platform.factory import PlatformFactory  # type: ignore
                platform = PlatformFactory.new(str(platform_dir))
                board: Any = platform.board_config(board_id)
                debug_tools = board.get("debug.tools", {}) or {}
                if debug_tools:
                    current_debug = board.get_debug_tool_name(configured_debug)
                    onboard = sorted(
                        key for key, value in debug_tools.items()
                        if (value or {}).get("onboard")
                    )
                    external = sorted(
                        key for key, value in debug_tools.items()
                        if not (value or {}).get("onboard")
                    )
                    parts = [f"DEBUG: Current ({current_debug})"]
                    if onboard:
                        parts.append(f"On-board ({', '.join(onboard)})")
                    if external:
                        parts.append(f"External ({', '.join(external)})")
                    result["debug"] = " ".join(parts)

                protocols = set(board.get("upload.protocols", []) or [])
                protocols.add("esptool")
                result["available"] = sorted(protocols)
                result["max_ram"] = int(board.get("upload.maximum_ram_size", 0) or 0) or None
                result["max_flash"] = int(board.get("upload.maximum_size", 0) or 0) or None
                result["partitions"] = board.get("build.partitions")
        except Exception:
            pass

        if not hasattr(self, "_platform_upload_metadata_cache"):
            self._platform_upload_metadata_cache = {}
        self._platform_upload_metadata_cache[cache_key] = dict(result)
        return result

    def _cached_build_metadata(self, fast_bins: dict) -> dict:
        board_name = getattr(self, "current_board", "") or ""
        board_key = self._board_cache_key(board_name)
        metadata = getattr(self, "_build_metadata_by_board", {}) or {}
        entry = dict(
            metadata.get(board_key) or metadata.get(board_name) or {}
        )
        if not entry:
            return {}
        if entry.get("source_hash"):
            try:
                if entry["source_hash"] != self._hash_sources():
                    return {}
            except Exception:
                return {}
        try:
            fw_path = fast_bins.get("firmware")
            if fw_path and Path(fw_path).is_file():
                stat = Path(fw_path).stat()
                if entry.get("firmware_size") is not None and int(entry["firmware_size"]) != stat.st_size:
                    return {}
                if (entry.get("firmware_mtime_ns") is not None
                        and int(entry["firmware_mtime_ns"]) != stat.st_mtime_ns):
                    return {}
        except OSError:
            return {}
        return entry

    def _partition_upload_capacity(self, fast_bins: dict,
                                   default_size: int | None,
                                   default_scheme: str | None = None) -> int | None:
        """Resolve the selected app partition size without starting SCons."""
        scheme = self._project_option("board_build.partitions") or default_scheme
        if not scheme:
            return default_size
        scheme_path = Path(scheme)
        names = [scheme_path.name]
        if not scheme_path.suffix:
            names.append(scheme_path.name + ".csv")
        candidates: list[Path] = []
        if scheme_path.is_absolute():
            candidates.append(scheme_path)
        else:
            if self.sketch_dir_path:
                candidates.extend(self.sketch_dir_path / name for name in names)
            boot_app0 = fast_bins.get("boot_app0")
            if boot_app0:
                candidates.extend(Path(boot_app0).parent / name for name in names)
            try:
                packages_dir = Path(os.environ.get("PLATFORMIO_CORE_DIR", "")) / "packages"
                for framework in packages_dir.glob("framework-arduinoespressif32*"):
                    candidates.extend(framework / "tools" / "partitions" / name for name in names)
                    candidates.extend(framework / "partitions" / name for name in names)
            except Exception:
                pass

        csv_path = next((path for path in candidates if path.is_file()), None)
        if csv_path is None:
            return default_size
        try:
            for raw in csv_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                fields = [field.strip() for field in line.split(",")]
                if len(fields) < 5:
                    continue
                p_type, subtype = fields[1].lower(), fields[2].lower()
                if p_type in ("0", "app") and subtype in ("factory", "ota_0"):
                    return self._parse_size_value(fields[4]) or default_size
        except Exception:
            pass
        return default_size

    @staticmethod
    def _format_platformio_memory_row(label: str, used: int, maximum: int) -> str:
        ratio = float(used) / float(maximum) if maximum else 0.0
        blocks = max(0, min(10, int(round(10 * ratio))))
        bar = ("=" * blocks).ljust(10)
        prefix = "RAM:  " if label.lower() == "ram" else "Flash:"
        return (
            f"{prefix} [{bar}] {ratio: 6.1%} "
            f"(used {int(used)} bytes from {int(maximum)} bytes)"
        )

    def _derive_build_usage_from_elf(self, fast_bins: dict, platform_meta: dict) -> dict:
        """Fallback for pre-upgrade caches: reproduce PIO's ESP size regexes."""
        fw_path = fast_bins.get("firmware", "")
        if not fw_path:
            return {}
        elf_path = Path(fw_path).with_suffix(".elf")
        if not elf_path.is_file():
            return {}
        try:
            from elftools.elf.elffile import ELFFile  # type: ignore
            with elf_path.open("rb") as handle:
                elf = ELFFile(handle)
                sections = {
                    section.name: int(section.data_size)
                    for section in elf.iter_sections()
                }
        except Exception:
            return {}

        platform_name = fast_bins.get("platform")
        if platform_name == "espressif32":
            flash_names = (
                ".iram0.text", ".iram0.vectors", ".dram0.data",
                ".flash.text", ".flash.rodata",
            )
            ram_names = (".dram0.data", ".dram0.bss", ".noinit")
        elif platform_name == "espressif8266":
            flash_names = (".text", ".data", ".rodata", ".irom0.text")
            ram_names = (".data", ".rodata", ".bss", ".noinit")
        else:
            return {}

        flash_used = sum(sections.get(name, 0) for name in flash_names)
        ram_used = sum(sections.get(name, 0) for name in ram_names)
        max_ram = platform_meta.get("max_ram")
        max_flash = self._partition_upload_capacity(
            fast_bins,
            platform_meta.get("max_flash"),
            platform_meta.get("partitions"),
        )
        result = {}
        if ram_used and max_ram:
            result["ram"] = self._format_platformio_memory_row("RAM", ram_used, max_ram)
        if flash_used and max_flash:
            result["flash"] = self._format_platformio_memory_row("Flash", flash_used, max_flash)
        return result

    def _fast_upload_metadata_lines(self, fast_bins: dict) -> list[tuple[str, str]]:
        """Return the compact PlatformIO metadata block for direct upload."""
        cached = self._cached_build_metadata(fast_bins)
        platform_meta = self._resolve_platform_upload_metadata()
        derived = {}
        if not cached.get("ram") or not cached.get("flash"):
            derived = self._derive_build_usage_from_elf(fast_bins, platform_meta)

        rows: list[tuple[str, str]] = []
        # Keep metadata clean by displaying only actual memory usage (RAM & Flash)
        # without verbose debugger probe / JTAG method listings.

        ram_line = cached.get("ram") or derived.get("ram")
        if ram_line:
            rows.append((f"  {ram_line}", "info"))

        flash_line = cached.get("flash") or derived.get("flash")
        if flash_line:
            rows.append((f"  {flash_line}", "info"))

        if not ram_line and not flash_line:
            fw_size = fast_bins.get("firmware_size")
            if fw_size:
                rows.append((f"  Firmware binary size: {int(fw_size):,} bytes", "info"))

        rows.append(("", "normal"))
        return rows

    def _probe_chip_info(self, port: str) -> bool:
        """Connect to the chip via esptool and print hardware info to the build console."""
        esp = None
        try:
            import esptool

            if not hasattr(esptool, "get_default_connected_device"):
                self.emit("console:log", {
                    "text": "  ⚠ esptool version too old for chip probe (need ≥ 4.x).",
                    "tag": "warning",
                    "newline": True,
                })
                return False

            try:
                esp = esptool.get_default_connected_device(
                    serial_list=[port],
                    port=port,
                    connect_attempts=3,
                    initial_baud=115200,
                )
            except Exception:
                time.sleep(0.5)
                esp = esptool.get_default_connected_device(
                    serial_list=[port],
                    port=port,
                    connect_attempts=3,
                    initial_baud=115200,
                )

            if esp is None:
                return False

            esp_device: Any = esp
            try:
                esp_device = esp_device.run_stub()
            except Exception:
                pass

            chip_model = getattr(esp_device, "CHIP_NAME", "Unknown")

            try:
                features = ", ".join(esp_device.get_chip_features())
            except Exception:
                features = "N/A"

            try:
                mac_bytes = esp_device.read_mac()
                mac = ":".join(f"{b:02X}" for b in mac_bytes)
            except Exception:
                mac = "N/A"

            try:
                crystal = esp_device.get_crystal_freq()
                crystal_str = f"{crystal} MHz"
            except Exception:
                crystal_str = "N/A"

            try:
                flash_str = "N/A"
                try:
                    from esptool.cmds import detect_flash_size, attach_flash
                    attach_flash(esp_device)
                    detected = detect_flash_size(esp_device)
                    if detected:
                        flash_str = detected
                except Exception:
                    pass

                if flash_str == "N/A":
                    try:
                        from esptool.cmds import DETECTED_FLASH_SIZES
                        raw = esp_device.flash_id()
                        if isinstance(raw, int):
                            size_id = (raw >> 16) & 0xFF
                            flash_str = DETECTED_FLASH_SIZES.get(size_id, "N/A")
                    except Exception:
                        pass
            except Exception:
                flash_str = "N/A"

            self._print_chip_info_box(chip_model, [
                ("Chip Model",  chip_model),
                ("Features",    features),
                ("MAC Address", mac),
                ("Crystal",     crystal_str),
                ("Flash Size",  flash_str),
            ])
            return True
        except Exception as e:
            self.emit("console:log", {
                "text": f"  ⚠ Chip probe failed: {e}",
                "tag": "warning",
                "newline": True,
            })
            self.emit("console:log", {
                "text": "  (Continuing with upload anyway...)",
                "tag": "dim",
                "newline": True,
            })
            return False
        finally:
            if esp is not None:
                try:
                    esp._port.close()
                except Exception:
                    pass
            time.sleep(0.3)

    def _compat_cache_file(self) -> Path | None:
        """Path of the per-project compatible-devices cache JSON."""
        if not self.sketch_dir_path:
            return None
        return get_project_build_cache_root(self.sketch_dir_path) / ".mcu_gui_compat_cache.json"

    def _save_compat_cache(self, boards, reasons, src_hash) -> None:
        try:
            path = self._compat_cache_file()
            if path is None:
                return
            payload = {
                "hash": src_hash,
                "boards": sorted(boards),
                "reasons": list(reasons),
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
            ensure_file_writable(path)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            hide_hidden_attribute(path)
        except Exception:
            pass

    def _load_compat_cache(self) -> None:
        """Reload the compatible-devices list cached at the last successful compile."""
        cached = None
        path = None
        try:
            path = self._compat_cache_file()
            if path and path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                cached = {
                    "boards": set(data.get("boards", [])),
                    "reasons": list(data.get("reasons", [])),
                    "hash": str(data.get("hash", "")),
                    "updated_at": str(data.get("updated_at", "")),
                }
        except Exception:
            cached = None
            try:
                if path:
                    path.unlink(missing_ok=True)
            except Exception:
                pass
        self._compat_cache = cached

        if not cached:
            self._compat_full_state = None
            initial_lines = [
                ("", "normal"),
                ("  ℹ Compatible devices are detected when this project is compiled.", "dim"),
                ("  👉 Click '⚙ Compile' (or 'Upload') to generate the list.", "dim"),
            ]
            self.emit("compat_devices:updated", {
                "lines": initial_lines,
                "content": initial_lines,
                "status": "Please compile to see the list of compatible devices",
            })
            return

        try:
            current_hash = self._hash_sources()
        except Exception:
            current_hash = ""
        stale = cached.get("hash", "") != current_hash
        status = "Please recompile to update the list" if stale else f"✔ {len(cached['boards'])}/{len(SUPPORTED_BOARDS)} · {cached.get('updated_at', '')}"
        state = {
            "boards": cached["boards"],
            "reasons": cached["reasons"],
            "stale": stale,
            "updated_at": cached.get("updated_at", ""),
            "status": status,
        }
        self._compat_full_state = state
        self._render_compat_from_state(state)

    def _build_compat_content(self, boards, reasons, stale: bool = False,
                              filter_text: str | None = None,
                              updated_at: str | None = None) -> list[tuple[str, str]]:
        total = len(SUPPORTED_BOARDS)
        sketch_name = self.sketch_dir_path.name if self.sketch_dir_path else "—"

        if filter_text:
            needle = filter_text.strip().lower()
            filtered = {b for b in boards if needle in b.lower()}
        else:
            needle = None
            filtered = boards

        family_order = ["ESP32-S3", "ESP32-C6", "ESP32-C3", "ESP32-S2",
                        "ESP32-C2", "ESP32", "ESP32 (other)",
                        "ESP8266", "ESP8266 (other)", "Arduino AVR", "Uno"]
        groups: dict[str, list[str]] = {}
        for b in filtered:
            groups.setdefault(
                _board_family(b, SUPPORTED_BOARDS.get(b, {}).get("platform")), []
            ).append(b)
        for names in groups.values():
            names.sort(key=str.lower)

        lines: list[tuple[str, str]] = []

        def _add(text: str, tag: str = "normal") -> None:
            lines.append((text, tag))

        _add("  " + "─" * 46, "dim")
        _add(f"  🔧 COMPATIBLE DEVICES — {sketch_name}", "system")
        if stale:
            cache_state = getattr(self, "_compat_cache", None) or {}
            last = updated_at or cache_state.get("updated_at") or "-"
            _add(f"  ⚠ Sources changed since the last compile — recompile to refresh. (last: {last})", "warning")
        if needle:
            filter_str = (filter_text or "").strip()
            if filtered:
                _add(f"  🔍 {len(filtered)} of {len(boards)} boards match '{filter_str}'.", "dim")
            else:
                _add(f"  ✖ No boards match '{filter_str}'.", "error")
        else:
            _add(f"  ✔ {len(boards)} of {total} supported boards pass the static check.",
                 "success" if boards else "error")
        if not reasons and len(boards) == total:
            _add("  ℹ No platform-specific APIs detected — likely portable across all boards.", "dim")
        _add("  " + "─" * 46, "dim")
        _add("")

        if not filtered:
            _add("  ✖ No compatible boards found.", "error")
        else:
            for fam in family_order:
                if fam not in groups:
                    continue
                names = groups.pop(fam)
                _add(f"  ✔ {fam}  ({len(names)})", "success")
                for name in names:
                    _add(f"      • {name}", "normal")
                _add("")
            for fam, names in groups.items():
                _add(f"  ✔ {fam}  ({len(names)})", "success")
                for name in names:
                    _add(f"      • {name}", "normal")
                _add("")

        if reasons:
            _add("  ⚠ Excluded / cautioned because:", "warning")
            for r in reasons:
                _add(f"      - {r}", "warning")
            _add("")

        _add("  ℹ Static estimate from headers, API calls, GPIO range, flash size and", "dim")
        _add("    PSRAM metadata — not a guarantee. The selected board must still", "dim")
        _add("    expose the used pins on its physical variant.", "dim")
        return lines

    def _render_compat_from_state(self, state: dict, filter_text: str | None = None) -> None:
        lines = self._build_compat_content(
            state["boards"], state["reasons"],
            stale=state.get("stale", False),
            filter_text=filter_text,
            updated_at=state.get("updated_at"),
        )
        status = state.get("status", "")
        self.emit("compat_devices:updated", {
            "lines": lines,
            "content": lines,
            "status": status,
        })

    def _get_compat_analysis(self) -> tuple[set, list]:
        try:
            current_hash = self._hash_sources()
        except Exception:
            current_hash = ""
        cached = getattr(self, "_compat_cache", None)
        if cached and not cached.get("error") and cached.get("hash") == current_hash:
            return cached["boards"], cached["reasons"]
        try:
            self._refresh_compatible_devices()
        except Exception:
            pass
        return set(), []

    def _refresh_compatible_devices(self, force: bool = False):
        self._compat_analysis_gen = getattr(self, "_compat_analysis_gen", 0) + 1
        gen = self._compat_analysis_gen
        self.emit("compat_devices:updated", {
            "lines": [
                ("", "normal"),
                ("  ⏳ Analyzing sketch compatibility in background...", "dim"),
            ],
            "content": [
                ("", "normal"),
                ("  ⏳ Analyzing sketch compatibility in background...", "dim"),
            ],
            "status": "Analyzing compatible devices...",
        })
        threading.Thread(
            target=self._compat_worker_thread,
            args=(gen,),
            daemon=True,
            name=f"MCU_CompatWorker_{gen}",
        ).start()

    def _compat_worker_thread(self, gen: int):
        try:
            if not self.sketch_dir_path:
                return
            boards, reasons = detect_board_compatibility(self.sketch_dir_path)
            res = {
                "gen": gen,
                "boards": boards,
                "reasons": reasons,
                "hash": self._hash_sources(),
            }
        except Exception as exc:
            res = {"gen": gen, "error": str(exc)}
        self._render_compatible_devices(res)

    def _render_compatible_devices(self, result: dict) -> None:
        gen = result.get("gen")
        if gen is not None and gen != getattr(self, "_compat_analysis_gen", 0):
            return

        if result.get("error"):
            self._compat_full_state = None
            err_lines = [
                ("  ✖ Compatibility analysis failed:", "error"),
                (f"  {result['error']}", "error"),
            ]
            self.emit("compat_devices:updated", {
                "lines": err_lines,
                "content": err_lines,
                "status": "✖ Analysis failed",
            })
            return

        boards: set[str] = result["boards"]
        reasons: list[str] = result["reasons"]
        now = datetime.now().strftime("%H:%M:%S")
        self._compat_cache = {
            "boards": boards,
            "reasons": reasons,
            "hash": result.get("hash", ""),
            "updated_at": now,
        }
        status = f"✔ {len(boards)}/{len(SUPPORTED_BOARDS)} · {now}"
        self._compat_full_state = {
            "boards": boards,
            "reasons": reasons,
            "stale": False,
            "updated_at": now,
            "status": status,
        }
        self._render_compat_from_state(self._compat_full_state)
        self._save_compat_cache(boards, reasons, result.get("hash", ""))

    def filter_compatible_devices(self, filter_text: str):
        state = getattr(self, "_compat_full_state", None)
        if not state:
            return
        self._render_compat_from_state(state, filter_text=filter_text)

    def add_project_file(self, filename: str) -> dict[str, Any]:
        """Create a new source/header file in the sketch folder."""
        if self.is_busy or getattr(self, "active_operation", None) is not None:
            return {"success": False, "error": "Modifying project files is not allowed while an action is in progress."}
        if not self.sketch_dir_path or not self.sketch_dir_path.is_dir() or is_application_codebase_dir(self.sketch_dir_path):
            return {"success": False, "error": "Cannot modify files in the MCU Flasher application folder."}
        clean_name = filename.strip()
        if not clean_name:
            return {"success": False, "error": "Filename cannot be empty."}
        target = self.sketch_dir_path / clean_name
        if target.exists():
            return {"success": False, "error": f"File '{clean_name}' already exists."}
        ext = target.suffix.lower()
        if ext not in {".ino", ".h", ".hpp", ".cpp", ".c", ".txt"}:
            return {"success": False, "error": f"Unsupported file extension '{ext}' (allowed: .ino, .h, .cpp, .c, .txt)."}

        content = ""
        if ext in (".h", ".hpp"):
            guard = re.sub(r'[^A-Za-z0-9_]', '_', clean_name.upper())
            content = f"#ifndef {guard}\n#define {guard}\n\n#include <Arduino.h>\n\n#endif // {guard}\n"
        elif ext == ".cpp":
            h_pair = target.with_suffix(".h")
            if h_pair.exists():
                content = f'#include "{h_pair.name}"\n\n'
            else:
                content = '#include <Arduino.h>\n\n'

        try:
            target.write_text(content, encoding="utf-8")
            _sketch_ram_cache.invalidate()
            self.update_skip_compile_availability()
            self.emit("project:updated", {"path": str(self.sketch_dir_path), "active_file": str(target)})
            return {"success": True, "path": str(target)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def rename_project_file(self, old_name: str, new_name: str) -> dict[str, Any]:
        """Rename a file in the sketch folder."""
        if self.is_busy or getattr(self, "active_operation", None) is not None:
            return {"success": False, "error": "Modifying project files is not allowed while an action is in progress."}
        if not self.sketch_dir_path or not self.sketch_dir_path.is_dir() or is_application_codebase_dir(self.sketch_dir_path):
            return {"success": False, "error": "Cannot modify files in the MCU Flasher application folder."}
        old_file = self.sketch_dir_path / old_name.strip()
        new_file = self.sketch_dir_path / new_name.strip()
        if not old_file.is_file():
            return {"success": False, "error": f"File '{old_name}' not found."}
        if new_file.exists():
            return {"success": False, "error": f"File '{new_name}' already exists."}
        try:
            old_file.rename(new_file)
            _sketch_ram_cache.invalidate()
            self.update_skip_compile_availability()
            self.emit("project:updated", {"path": str(self.sketch_dir_path), "active_file": str(new_file)})
            return {"success": True, "path": str(new_file)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_project_file(self, filename: str) -> dict[str, Any]:
        """Permanently delete a file from the sketch folder."""
        if self.is_busy or getattr(self, "active_operation", None) is not None:
            return {"success": False, "error": "Modifying project files is not allowed while an action is in progress."}
        if not self.sketch_dir_path or not self.sketch_dir_path.is_dir() or is_application_codebase_dir(self.sketch_dir_path):
            return {"success": False, "error": "Cannot modify files in the MCU Flasher application folder."}
        target = self.sketch_dir_path / filename.strip()
        if not target.is_file():
            return {"success": False, "error": f"File '{filename}' not found."}
        all_sources = get_sketch_files_fast(self.sketch_dir_path)
        if len(all_sources) <= 1:
            return {"success": False, "error": "Cannot delete the only source file in the project."}
        try:
            target.unlink()
            _sketch_ram_cache.invalidate()
            self.update_skip_compile_availability()
            self.emit("project:updated", {"path": str(self.sketch_dir_path)})
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def run_action(self, action: str) -> dict[str, Any]:
        """Dispatch named action from editor or detached controls."""
        act = (action or "").strip().lower()
        if act == "compile":
            self.compile_sketch()
        elif act == "upload":
            self.upload_sketch()
        elif act == "stop":
            self.stop_operation()
        elif act == "clean":
            self.clean_cache()
        elif act == "soft_reset":
            self.soft_reset()
        elif act == "hard_reset":
            self.hard_reset()
        elif act == "save_all":
            self.save_all_files()
        elif act == "reload":
            # Reload the currently active editor file from disk
            sig = self._get_qt_signals()
            if sig:
                sig.file_reload_requested.emit()
        elif act == "modify":
            # Open the modify-files dialog
            sig = self._get_qt_signals()
            if sig:
                sig.modify_files_requested.emit()
        return {"success": True, "action": act}

    def on_editor_content_change(self):
        """Callback invoked by the Monaco Editor iframe on buffer content changes."""
        self.skip_compile = False
        self.emit("skip_compile:availability", False)

    def open_project(self, folder_path: str, active_file: Optional[str] = None) -> dict[str, Any]:
        """Open and switch to an existing sketch directory or code file."""
        if self.is_busy or getattr(self, "active_operation", None) is not None:
            self.emit("notification", {
                "title": "Action in Progress",
                "message": "Changing project is not allowed while an action is in progress.",
                "type": "warning",
            })
            return {
                "success": False,
                "error": "Changing project is not allowed while an action is in progress.",
            }

        target = Path(folder_path).resolve()
        if target.is_file():
            active_file = str(target)
            p = target.parent
        elif target.is_dir():
            p = target
        else:
            return {"success": False, "error": f"Path does not exist: {folder_path}"}

        # REJECT APPLICATION CODEBASE
        if is_application_codebase_dir(p):
            return {
                "success": False,
                "error": "The MCU Flasher application folder cannot be opened as an Arduino sketch project."
            }

        # Check if project is already active in another window
        owner = find_project_window(p)
        if owner and owner.get("pid") != os.getpid():
            focus_project_window(owner.get("hwnd", 0), owner.get("pid", 0))
            self.emit("notification", {
                "title": "Project Already Open",
                "message": f"The sketch '{p.name}' is already open in another window. Switched focus to it.",
                "type": "warning",
            })
            return {
                "success": False,
                "error": f"The project '{p.name}' is already open in another window.",
                "already_open": True,
                "owner_pid": owner.get("pid"),
            }

        # Auto-scaffold default .ino if empty or missing source files
        source_extensions = {".ino", ".cpp", ".c", ".h", ".hpp"}
        has_source = False
        try:
            for item in p.iterdir():
                if item.is_file() and item.suffix.lower() in source_extensions:
                    has_source = True
                    break
        except Exception:
            pass

        if not has_source:
            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', p.name.strip()) or "sketch"
            default_ino = p / f"{clean_name}.ino"
            try:
                if not default_ino.exists():
                    template = (
                        "void setup() {\n"
                        "  Serial.begin(115200);\n"
                        "  Serial.println(\"Hello from MCU Flasher by Naph!\");\n"
                        "}\n\n"
                        "void loop() {\n"
                        "  delay(1000);\n"
                        "}\n"
                    )
                    default_ino.write_text(template, encoding="utf-8")
                    if not active_file:
                        active_file = str(default_ino)
            except Exception:
                pass

        self.sketch_dir_path = p
        add_recent_project(str(p))
        set_active_sketch_dir(str(p), hwnd=getattr(self, "_hwnd", 0))

        # Enforce file hiding and cleanup immediately upon opening sketch
        try:
            hide_internal_project_metadata(p)
        except Exception:
            pass

        # Update config
        cfg = load_gui_config()
        cfg["last_sketch_dir"] = str(p)
        save_gui_config(cfg)

        files = self.get_project_files()
        if active_file and any(f["path"] == active_file for f in files):
            self.active_file_path = active_file
        else:
            self.active_file_path = files[0]["path"] if files else ""
        self.modified_files.clear()

        payload = {
            "path": str(p),
            "name": p.name,
            "files": files,
            "active_file": self.active_file_path,
        }
        self.emit("project:updated", payload)

        # Auto-detect sketch baud rate from Serial.begin(...) in background
        def _bg_detect_baud():
            try:
                detected_baud = self._detect_sketch_baud_rate()
                if detected_baud:
                    self.set_baud_rate(int(detected_baud))
            except Exception:
                pass
        threading.Thread(target=_bg_detect_baud, name="MCU_DetectBaud", daemon=True).start()

        self.emit("notification", {
            "title": "Project Opened",
            "message": f"Loaded sketch: {p.name}",
            "type": "info",
        })
        self._restore_project_compile_state()
        self.update_skip_compile_availability()
        self._load_compat_cache()
        self._sync_project_hardware_state(p)

        # Bind AI review manager and watcher to the new sketch project
        if hasattr(self, "ai_review_manager") and self.ai_review_manager:
            self.ai_review_manager.bind_project(p)
        if hasattr(self, "ai_watcher") and self.ai_watcher:
            self.ai_watcher.bind_project(p)
            reviews = self.ai_review_manager.get_ai_edit_reviews()
            if reviews:
                sig_bus = self._get_qt_signals()
                if sig_bus and hasattr(sig_bus, "ai_review_requested"):
                    sig_bus.ai_review_requested.emit(reviews[0].get("path", ""))

        return {"success": True, "project": payload}

    def open_project_picker(self) -> str:
        """Open native folder browser dialog."""
        if self.is_busy or getattr(self, "active_operation", None) is not None:
            return ""
        if self._window and hasattr(self._window, "create_file_dialog"):
            try:
                res = self._window.create_file_dialog(
                    webview.FOLDER_DIALOG, directory=str(self.sketch_dir_path)
                )
                if res and len(res) > 0:
                    return str(res[0])
            except Exception:
                pass

        # Fallback to PowerShell FolderBrowserDialog
        try:
            cmd = [
                "powershell", "-NoProfile", "-Command",
                "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
                "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
                f"$f.SelectedPath = '{str(self.sketch_dir_path)}'; "
                "$f.ShowNewFolderButton = $true; "
                "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath }"
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            out = result.stdout.strip()
            if out and Path(out).is_dir() and not is_application_codebase_dir(out):
                return out
        except Exception:
            pass
        return ""

    def open_file_picker(self) -> str:
        """Open native file browser dialog for .ino, .cpp, .c, .h files."""
        if self._window and hasattr(self._window, "create_file_dialog"):
            try:
                res = self._window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    directory=str(self.sketch_dir_path),
                    file_types=('Arduino & C/C++ Files (*.ino;*.cpp;*.c;*.h;*.hpp)', 'All files (*.*)')
                )
                if res and len(res) > 0:
                    selected = str(res[0])
                    if not is_application_codebase_dir(Path(selected).parent):
                        return selected
            except Exception:
                pass

        # Fallback to PowerShell OpenFileDialog
        try:
            cmd = [
                "powershell", "-NoProfile", "-Command",
                "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
                "$f = New-Object System.Windows.Forms.OpenFileDialog; "
                f"$f.InitialDirectory = '{str(self.sketch_dir_path)}'; "
                "$f.Filter = 'Arduino & C/C++ (*.ino;*.cpp;*.c;*.h;*.hpp)|*.ino;*.cpp;*.c;*.h;*.hpp|All files (*.*)|*.*'; "
                "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.FileName }"
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            out = result.stdout.strip()
            if out and Path(out).is_file() and not is_application_codebase_dir(Path(out).parent):
                return out
        except Exception:
            pass
        return ""

    def get_default_project_parent(self) -> str:
        """Return the default parent directory for new projects."""
        try:
            if self.sketch_dir_path.is_dir() and not is_application_codebase_dir(self.sketch_dir_path) and not is_application_codebase_dir(self.sketch_dir_path.parent):
                return str(self.sketch_dir_path.parent)
            docs = Path(os.path.expanduser("~")) / "Documents" / "Arduino"
            if docs.is_dir():
                return str(docs)
            return str(Path(os.path.expanduser("~")) / "Documents")
        except Exception:
            return str(Path(os.path.expanduser("~")))

    def create_project(
        self,
        parent_dir: str,
        name: str,
        include_h: bool = False,
        include_cpp: bool = False,
        template_type: str = "standard",
    ) -> dict[str, Any]:
        """Create a new sketch folder with full scaffold (matching old ProjectSelectorDialog)."""
        if self.is_busy or getattr(self, "active_operation", None) is not None:
            self.emit("notification", {
                "title": "Action in Progress",
                "message": "Creating or changing project is not allowed while an action is in progress.",
                "type": "warning",
            })
            return {
                "success": False,
                "error": "Creating or changing project is not allowed while an action is in progress.",
            }

        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name.strip()) or "NewSketch"
        target_dir = Path(parent_dir).resolve() / clean_name
        if is_application_codebase_dir(parent_dir) or is_application_codebase_dir(target_dir):
            return {"success": False, "error": "Cannot create sketch inside MCU Flasher application folder."}
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            ensure_hidden_read_first_md(target_dir)

            ino_includes = f'#include "{clean_name}.h"\n\n' if include_h else ""

            if template_type == "bare":
                body = (
                    "void setup() {\n"
                    "  \n"
                    "}\n\n"
                    "void loop() {\n"
                    "  \n"
                    "}\n"
                )
            elif template_type == "blink":
                body = (
                    "void setup() {\n"
                    "  pinMode(LED_BUILTIN, OUTPUT);\n"
                    "}\n\n"
                    "void loop() {\n"
                    "  digitalWrite(LED_BUILTIN, HIGH);\n"
                    "  delay(1000);\n"
                    "  digitalWrite(LED_BUILTIN, LOW);\n"
                    "  delay(1000);\n"
                    "}\n"
                )
            else:  # standard
                body = (
                    "void setup() {\n"
                    "  Serial.begin(115200);\n"
                    "  Serial.println(\"Hello from MCU Flasher by Naph!\");\n"
                    "}\n\n"
                    "void loop() {\n"
                    "  delay(1000);\n"
                    "}\n"
                )

            ino_file = target_dir / f"{clean_name}.ino"
            ino_file.write_text(f"{ino_includes}{body}", encoding="utf-8")

            if include_h:
                h_file = target_dir / f"{clean_name}.h"
                h_content = (
                    "#pragma once\n\n"
                    "// Header declarations for " + clean_name + "\n"
                )
                h_file.write_text(h_content, encoding="utf-8")

            if include_cpp:
                cpp_file = target_dir / f"{clean_name}.cpp"
                cpp_includes = f'#include "{clean_name}.h"\n\n' if include_h else ""
                cpp_content = (
                    cpp_includes +
                    "// Implementation details for " + clean_name + "\n"
                )
                cpp_file.write_text(cpp_content, encoding="utf-8")

            return self.open_project(str(target_dir))
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_recent_projects(self) -> list[str]:
        """Return the current list of valid recent project paths."""
        return load_recent_projects()

    def clear_recent_projects(self) -> list[str]:
        """Clear all stored recent projects history."""
        data = _load_raw_config()
        if "shared" not in data:
            data["shared"] = {}
        data["shared"]["recent_projects"] = []
        _save_raw_config(data)
        return []

    def open_in_explorer(self):
        """Open the active sketch directory in Windows File Explorer."""
        if sys.platform == "win32" and self.sketch_dir_path.exists():
            try:
                os.startfile(str(self.sketch_dir_path))
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # JS-RPC: HARDWARE & COM PORTS
    # ──────────────────────────────────────────────────────────
    def _scan_ports(self) -> list[dict[str, str]]:
        """Scan active serial COM ports on the system using pyserial and Windows registry fallback."""
        ports: list[dict[str, str]] = []
        seen: set[str] = set()

        # 1. Primary discovery via pyserial SetupAPI enumeration
        try:
            for p in serial.tools.list_ports.comports():
                dev = str(p.device or "").strip()
                if not dev:
                    continue
                seen.add(dev.upper())
                ports.append({
                    "device": dev,
                    "description": p.description or "",
                    "hwid": p.hwid or "",
                })
        except Exception:
            pass

        # 2. Windows registry fallback: HKLM\HARDWARE\DEVICEMAP\SERIALCOMM
        # Catches CH340, CP210x, and virtual ports if SetupAPI misses them
        if sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
                i = 0
                while True:
                    try:
                        val_name, val_data, _ = winreg.EnumValue(key, i)
                        dev = str(val_data).strip()
                        if dev and dev.upper() not in seen:
                            seen.add(dev.upper())
                            name_low = val_name.lower()
                            if "cp210" in name_low or "silab" in name_low:
                                desc = "Silicon Labs CP210x USB to UART Bridge"
                            elif "ch34" in name_low or "wch" in name_low:
                                desc = "USB-SERIAL CH340"
                            elif "usb" in name_low:
                                desc = "USB Serial Device"
                            else:
                                desc = "Communications Port"
                            ports.append({
                                "device": dev,
                                "description": f"{desc} ({dev})",
                                "hwid": val_name,
                            })
                        i += 1
                    except WindowsError:
                        break
                winreg.CloseKey(key)
            except Exception:
                pass

        # 3. Sort ports naturally (COM1, COM2, COM4, COM10...)
        def _sort_key(item: dict[str, str]) -> int:
            dev = item.get("device", "")
            nums = re.findall(r"\d+", dev)
            return int(nums[0]) if nums else 9999

        ports.sort(key=_sort_key)
        return ports

    def refresh_ports(self) -> list[dict[str, str]]:
        """Re-scan ports and notify frontend."""
        ports = self._scan_ports()
        self._last_known_ports = list(ports)
        self.emit("ports:updated", ports)
        return ports

    def _extract_port_device(self, text: str) -> str:
        """Extract COM device (e.g. 'COM4') from port label or raw string."""
        if not text:
            return ""
        match = re.match(r"(COM\d+|/dev/\S+)", str(text).strip())
        return match.group(1) if match else str(text).strip().split()[0]

    def _sync_project_hardware_state(self, target_dir: Optional[str | Path] = None) -> None:
        """Write current target board, port, and serial settings to
        <sketch_dir>/.mcu_flasher_build_cache/project_state.json so AI assistants (OpenCode & Antigravity)
        can instantly know the active MCU architecture, pinouts, and COM connection in real-time.
        """
        sketch_dir = Path(target_dir) if target_dir else getattr(self, "sketch_dir_path", None)
        if not sketch_dir or not Path(sketch_dir).is_dir():
            return

        try:
            cache_dir = get_project_build_cache_root(sketch_dir)

            port_label = getattr(self, "current_port", "") or ""
            port_device = self._extract_port_device(port_label) or ""

            # Filter generic motherboard Communications Port (COM1) so it is not mistaken for an attached MCU
            if port_device.upper() == "COM1":
                lbl = (port_label or "").lower()
                mcu_kws = ["cp210", "ch34", "ch91", "ftdi", "esp32", "silicon labs", "wch", "jtag", "usb bridge", "arduino", "mcu"]
                if "communications port" in lbl or not any(kw in lbl for kw in mcu_kws):
                    port_device = ""
                    port_label = ""

            board_name = (getattr(self, "current_board", "") or "").strip()
            mcu_connected = bool(port_device)
            board_selected = bool(board_name)

            if board_selected and mcu_connected:
                status_summary = f"Ready: {board_name} on {port_device}."
            elif board_selected and not mcu_connected:
                status_summary = f"{board_name} selected in GUI (No microcontroller connected on COM port)."
            elif not board_selected and mcu_connected:
                status_summary = f"Microcontroller connected on {port_device}, but no board selected in GUI."
            else:
                status_summary = "No board selected in GUI and no microcontroller connected."

            board_info = self._resolve_board_info(board_name) if board_selected else {}

            baud_val = getattr(self, "current_baud", 115200)
            upload_spd_val = getattr(self, "_active_upload_speed", "460800") or "460800"

            state_payload = (
                Path(sketch_dir).name,
                str(Path(sketch_dir).resolve(strict=False)),
                status_summary,
                tuple(sorted((k, str(v)) for k, v in {
                    "board_selected": board_selected,
                    "mcu_connected": mcu_connected,
                    "board_name": board_name if board_selected else None,
                    "platform": (board_info.get("platform", "") if board_selected else "") or None,
                    "framework": (board_info.get("framework", "arduino") if board_selected else "") or None,
                    "fqbn": ((board_info.get("fqbn", "") or board_info.get("board", "")) if board_selected else "") or None,
                    "build_mcu": ((board_info.get("build_mcu", "") or board_info.get("mcu", "")) if board_selected else "") or None,
                    "port": port_device if mcu_connected else None,
                    "port_label": port_label if mcu_connected else None,
                    "baud_rate": int(baud_val) if str(baud_val).isdigit() else 115200,
                    "upload_speed": int(upload_spd_val) if str(upload_spd_val).isdigit() else 460800,
                    "flash_mb": board_info.get("flash_mb") if board_selected else None,
                    "has_psram": board_info.get("has_psram", False) if board_selected else False,
                }.items())),
            )

            if getattr(self, "_last_synced_hardware_payload", None) == state_payload:
                return
            self._last_synced_hardware_payload = state_payload

            state_data = {
                "project_name": Path(sketch_dir).name,
                "project_path": str(Path(sketch_dir).resolve(strict=False)),
                "status_summary": status_summary,
                "hardware": {
                    "board_selected": board_selected,
                    "mcu_connected": mcu_connected,
                    "board_name": board_name if board_selected else None,
                    "platform": (board_info.get("platform", "") if board_selected else "") or None,
                    "framework": (board_info.get("framework", "arduino") if board_selected else "") or None,
                    "fqbn": ((board_info.get("fqbn", "") or board_info.get("board", "")) if board_selected else "") or None,
                    "build_mcu": ((board_info.get("build_mcu", "") or board_info.get("mcu", "")) if board_selected else "") or None,
                    "port": port_device if mcu_connected else None,
                    "port_label": port_label if mcu_connected else None,
                    "baud_rate": int(baud_val) if str(baud_val).isdigit() else 115200,
                    "upload_speed": int(upload_spd_val) if str(upload_spd_val).isdigit() else 460800,
                    "flash_mb": board_info.get("flash_mb") if board_selected else None,
                    "has_psram": board_info.get("has_psram", False) if board_selected else False,
                },
                "last_updated": datetime.now().isoformat(timespec="seconds"),
            }

            state_file = cache_dir / "project_state.json"
            payload_text = json.dumps(state_data, indent=2, ensure_ascii=False)

            def _write_state_bg():
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    hide_generated_directory(cache_dir)
                    ensure_file_writable(state_file)
                    state_file.write_text(payload_text, encoding="utf-8")
                    ensure_hidden_read_first_md(sketch_dir)
                    hide_internal_project_metadata(sketch_dir)
                except Exception:
                    pass

            threading.Thread(target=_write_state_bg, name="MCU_SyncHardwareState", daemon=True).start()
        except Exception:
            pass

    def select_port(self, port: str):
        """Select COM port and update serial monitor."""
        self.current_port = port
        cfg = load_gui_config()
        cfg["selected_port"] = port
        save_gui_config(cfg)
        self._sync_project_hardware_state()

        if not self.is_busy:
            self._serial_reconnect_token += 1
            token = self._serial_reconnect_token

            def _switch_port_worker():
                # Debounce: coalesce rapid port/baud changes within 200ms
                time.sleep(0.2)
                if self._serial_reconnect_token != token:
                    return
                self._start_serial_monitor()

            threading.Thread(
                target=_switch_port_worker, name="MCU_SwitchPort", daemon=True
            ).start()

    def select_board(self, board_name: str):
        """Select microcontroller board model for the active session."""
        self.current_board = board_name
        cfg = load_gui_config()
        cfg["selected_board"] = board_name
        save_gui_config(cfg)
        if self.sketch_dir_path and board_name:
            try:
                set_project_remembered_board(str(self.sketch_dir_path), board_name)
            except Exception:
                pass
        if board_name:
            try:
                add_recent_board(board_name)
            except Exception:
                pass
        self._load_compile_cache(board_name)
        self.emit("board:selected", {"board_name": board_name})
        self.update_skip_compile_availability()
        self._sync_project_hardware_state()

    def get_recent_boards(self) -> list[str]:
        """Return recently selected MCU boards (max 5)."""
        return load_recent_boards()

    def search_boards(self, query: str) -> list[dict[str, str]]:
        """Search boards by query string across name, platform, and MCU."""
        q = (query or "").strip().lower()
        results = []
        for name, info in SUPPORTED_BOARDS.items():
            plat = str(info.get("platform", "") or "").lower()
            mcu = str(info.get("mcu", "") or "").lower()
            if not q or q in name.lower() or q in plat or q in mcu:
                results.append({
                    "name": name,
                    "platform": str(info.get("platform", "") or "Microcontroller"),
                    "mcu": str(info.get("mcu", "") or ""),
                })
                if len(results) >= 80:
                    break
        return results

    def set_reset_on_baud_change(self, enabled: bool) -> None:
        """Update whether MCU resets via DTR/RTS when baud rate changes."""
        self.reset_on_baud_change = bool(enabled)

    def set_baud_rate(self, baud: int):
        """Change serial communication baud rate, capped at MAX_BAUD_RATE (921600)."""
        new_baud = min(int(baud), MAX_BAUD_RATE)
        old_baud = getattr(self, "current_baud", 115200)
        baud_changed = (old_baud != new_baud)
        self.current_baud = new_baud
        cfg = load_gui_config()
        cfg["baud_rate"] = self.current_baud
        save_gui_config(cfg)
        self._sync_project_hardware_state()

        # If the baud rate did not change and the connection is active, avoid reconnect spam
        if not baud_changed and self.serial_running and self._serial_conn and self._serial_conn.is_open:
            return

        if not self.is_busy:
            should_reset = baud_changed and getattr(self, "reset_on_baud_change", False)

            # Bump the debounce token so any pending _switch_baud_worker aborts
            self._serial_reconnect_token += 1
            token = self._serial_reconnect_token

            def _switch_baud_worker():
                # Debounce: wait 200ms then check if a newer request superseded this one
                time.sleep(0.2)
                if self._serial_reconnect_token != token:
                    return  # A more-recent baud/port change will handle reconnect
                self._start_serial_monitor()
                if should_reset:
                    time.sleep(0.15)
                    self.pulse_dtr_reset()

            threading.Thread(
                target=_switch_baud_worker, name="MCU_SwitchBaud", daemon=True
            ).start()

    # ──────────────────────────────────────────────────────────
    # JS-RPC: SERIAL MONITOR & SEND
    # ──────────────────────────────────────────────────────────
    def _start_serial_monitor(self):
        """Open the COM port and stream data in a background thread."""
        with self._serial_lock:
            self.serial_running = False
            if self._serial_conn:
                try:
                    self._serial_conn.close()
                except Exception:
                    pass
                self._serial_conn = None

            if not self.current_port or self.is_busy:
                self.emit("serial:status", {
                    "connected": False,
                    "state": "disconnected",
                    "port": self.current_port or "",
                    "baud": self.current_baud,
                })
                return

            try:
                self._serial_conn = serial.Serial()
                self._serial_conn.port = self.current_port
                self._serial_conn.baudrate = self.current_baud
                self._serial_conn.timeout = 0.2
                self._serial_conn.dsrdtr = False
                self._serial_conn.rtscts = False
                self._serial_conn.dtr = False
                self._serial_conn.rts = False
                self._serial_conn.open()
                self._serial_conn.dtr = False
                self._serial_conn.rts = False
                self.serial_running = True
                self.emit("serial:status", {
                    "connected": True,
                    "state": "connected",
                    "port": self.current_port,
                    "baud": self.current_baud,
                })
                # Only log the "connected" banner when the (port, baud) pair actually changes.
                # This deduplications the banner when multiple rapid reconnects land on the
                # same pair (e.g. board-default baud + sketch-detected baud both == 115200).
                connection_key = (self.current_port, self.current_baud)
                if connection_key != getattr(self, "_last_serial_connection_key", None):
                    self._last_serial_connection_key = connection_key
                    self.emit("serial:log", {
                        "text": f"--- Serial Monitor connected to {self.current_port} @ {self.current_baud} baud ---",
                        "tag": "info",
                        "newline": True,
                    })
            except Exception as e:
                self.serial_running = False
                self.emit("serial:status", {
                    "connected": False,
                    "state": "disconnected",
                    "port": self.current_port,
                    "baud": self.current_baud,
                })
                self.emit("serial:log", {
                    "text": f"--- Could not open {self.current_port}: {e} ---",
                    "tag": "error",
                    "newline": True,
                })
                return

        def _reader():
            buf = bytearray()
            while self.serial_running and self._serial_conn and self._serial_conn.is_open:
                try:
                    raw = self._serial_conn.read(self._serial_conn.in_waiting or 1)
                    if not raw:
                        continue
                    buf.extend(raw)
                    if b"\n" in buf or len(buf) > 1024:
                        lines = buf.split(b"\n")
                        buf = lines[-1]
                        for line in lines[:-1]:
                            text = line.decode("utf-8", errors="replace").rstrip("\r")
                            self.emit("serial:log", {"text": text, "newline": True})
                except Exception:
                    break
            self.serial_running = False
            self.emit("serial:status", {
                "connected": False,
                "state": "disconnected",
                "port": self.current_port,
                "baud": self.current_baud,
            })

        self._serial_thread = threading.Thread(
            target=_reader, name="MCU_SerialReader", daemon=True
        )
        self._serial_thread.start()

    def _stop_serial_monitor(self):
        """Stop and close the serial monitor before upload/flash."""
        with self._serial_lock:
            self.serial_running = False
            if self._serial_conn:
                try:
                    self._serial_conn.dtr = False
                    self._serial_conn.rts = False
                    self._serial_conn.close()
                except Exception:
                    pass
                self._serial_conn = None
            self.emit("serial:status", {
                "connected": False,
                "state": "disconnected",
                "port": self.current_port,
                "baud": self.current_baud,
            })

    def pulse_dtr_reset(self) -> None:
        """Issue a brief, silent reset pulse on the live serial connection.

        This reboots the MCU immediately after an upload completes, so the
        sketch starts running and boot logs appear in the Serial Monitor without
        Safe to call at any time: silently no-ops if the serial port is not
        open or if a destructive operation (upload / flash / reset) is active.
        """
        now = time.monotonic()
        if now - getattr(self, "_last_dtr_pulse_time", 0.0) < 0.6:
            return
        self._last_dtr_pulse_time = now

        def _pulse():
            # Don't interfere with an ongoing operation.
            if getattr(self, "is_busy", False):
                return

            # Wait up to 2.0s for the serial monitor to finish connecting if it was just restarted
            conn = None
            for _ in range(20):
                if getattr(self, "is_busy", False):
                    return
                with self._serial_lock:
                    if self._serial_conn and self._serial_conn.is_open:
                        conn = self._serial_conn
                        break
                time.sleep(0.1)

            if conn is None:
                return

            try:
                binfo = self._resolve_board_info(getattr(self, "current_board", ""))
                platform = str(binfo.get("platform", "")).lower()
                is_uno = ("avr" in platform)
                is_arm = platform in ("ststm32", "raspberrypi", "ch32v", "samd")

                if is_uno:
                    conn.rts = False
                    conn.dtr = False
                    time.sleep(0.05)
                    conn.dtr = True
                    time.sleep(0.10)
                    conn.dtr = False
                    time.sleep(0.05)
                elif is_arm:
                    conn.dtr = False
                    conn.rts = False
                    time.sleep(0.05)
                    conn.dtr = True
                    time.sleep(0.05)
                    conn.dtr = False
                    time.sleep(0.05)
                else:
                    # ESP32 / ESP8266 auto-reset transistor circuit:
                    # RTS=1, DTR=0 asserts EN (RESET pulled LOW)
                    # RTS=0, DTR=0 releases EN (MCU boots normally into flash)
                    conn.dtr = False
                    conn.rts = True
                    time.sleep(0.15)
                    conn.rts = False
                    conn.dtr = False
                    time.sleep(0.05)
            except Exception:
                pass  # Port may have been closed or disconnected mid-pulse; ignore silently.

        threading.Thread(target=_pulse, name="MCU_SilentDTR", daemon=True).start()

    def serial_send(self, text: str, line_ending: str = "both"):
        """Transmit text command to the connected microcontroller."""
        if not self._serial_conn or not self._serial_conn.is_open:
            self.emit("serial:log", {"text": "✖ Cannot send: Serial port not connected", "tag": "error", "newline": True})
            return

        endings = {
            "both": "\r\n",
            "nl": "\n",
            "cr": "\r",
            "none": "",
        }
        suffix = endings.get(line_ending, "\r\n")
        data = (text + suffix).encode("utf-8", errors="replace")

        try:
            self._serial_conn.write(data)
            self._serial_conn.flush()
            self.emit("serial:log", {"text": f"❯ {text}", "tag": "dim", "newline": True})
        except Exception as e:
            self.emit("serial:log", {"text": f"✖ Send error: {e}", "tag": "error", "newline": True})

    # ──────────────────────────────────────────────────────────
    # JS-RPC: COMPILATION PIPELINE (PlatformIO)
    # ──────────────────────────────────────────────────────────
    def compile_sketch(self):
        """Run sketch compilation on a background thread."""
        if self.is_busy:
            return

        if self._block_if_pending_ai_edits("Compile"):
            return

        cfg = load_gui_config()
        if getattr(self, "clear_console_on_action", cfg.get("clear_console_on_action", True)):
            self.emit("console:clear", None)
        if getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)):
            self.emit("serial:clear", None)

        if not self.current_board:
            self._restore_project_compile_state()
            if self.current_board:
                self.emit("board:selected", {"board_name": self.current_board})
        if not self.current_board:
            self.emit("console:log", {"text": "✖ Compile error: No board selected. Please select a board first.", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Compile Failed", "message": "No board selected.", "type": "warning"})
            return

        self.is_busy = True
        self._stop_requested = False
        self.active_operation = "compile"
        self._current_op_phase = "compiling"
        self.emit("operation:phase", {"phase": "compile", "is_busy": True, "can_stop": True, "op": "compile"})
        self.emit("window:closable", {"closable": True})

        threading.Thread(
            target=self._compile_worker, args=(False,), name="MCU_Compile", daemon=True
        ).start()

    @staticmethod
    def _convert_sketch_inos_to_cpp(primary_ino: Path, ino_files: list[Path]) -> str:
        """Convert Arduino .ino sketch files into a unified .cpp unit with Arduino.h and prototypes."""
        main_name = primary_ino.name
        c = None
        try:
            from platformio.builder.tools.pioino import InoToCPPConverter

            class _DummyEnv:
                pass

            c = InoToCPPConverter(_DummyEnv())
            c._main_ino = main_name
        except Exception:
            c = None

        SETUP_LOOP_RE = re.compile(r"\bvoid\s+(?:setup|loop)\s*\(", re.MULTILINE | re.IGNORECASE)
        main_lines = []
        other_lines = []
        for f in ino_files:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            f_label = f.name.replace("\\", "/")
            cur_block = [f'#line 1 "{f_label}"', content]
            if f == primary_ino or SETUP_LOOP_RE.search(content):
                main_lines = cur_block + main_lines
            else:
                other_lines.extend(cur_block)

        merged_body = "\n".join(["#include <Arduino.h>"] + main_lines + other_lines)
        if c is not None:
            try:
                return c.append_prototypes(merged_body)
            except Exception:
                pass
        return merged_body

    def _compile_worker(self, is_upload: bool = False, is_clean_retry: bool = False) -> bool:
        """Core compile worker executing PlatformIO."""
        if not is_clean_retry:
            self.is_busy = True
            self._stop_requested = False
            self.active_operation = "upload" if is_upload else "compile"
            self._current_op_phase = "compiling"
            self.emit("operation:phase", {"phase": "compile", "is_busy": True, "can_stop": True, "op": self.active_operation})
            self.emit("window:closable", {"closable": True})

        start_time = time.time()
        self._resolve_board_info(self.current_board)  # primes RAM cache
        compiler_name = "PlatformIO"
        core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)

        self.emit("console:log", {"text": "", "newline": True})
        self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
        self.emit("console:log", {"text": f"  ⚙  COMPILING ({compiler_name})", "tag": "header", "newline": True})
        self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
        self.emit("console:log", {"text": f"  Sketch : {self.sketch_dir_path}", "tag": "dim", "newline": True})
        if self.current_board:
            self.emit("console:log", {"text": f"  Board  : {self.current_board}", "tag": "dim", "newline": True})
        self.emit("console:log", {"text": f"  Tool   : {compiler_name}", "tag": "dim", "newline": True})
        self.emit("console:log", {"text": f"  Store  : {core_dir}", "tag": "dim", "newline": True})
        self.emit("console:log", {"text": "", "newline": True})

        if is_upload and not getattr(self, "skip_compile", False):
            self.emit("console:log", {
                "text": "  🔄 Skip Compile is unchecked — compiling firmware before upload.",
                "tag": "info",
                "newline": True
            })

        is_remote = is_unc_or_network_path(self.sketch_dir_path)
        effective_sketch_dir = self.sketch_dir_path
        if is_remote:
            effective_sketch_dir = self._map_unc_for_build(self.sketch_dir_path)
            _share = _unc_share_root(self.sketch_dir_path) or str(self.sketch_dir_path)
            self.emit("console:log", {"text": f"  🌐 Source  : Network share ({_share})", "tag": "info", "newline": True})

        cache_root = self._effective_cache_root(self.sketch_dir_path)
        if is_remote:
            self.emit("console:log", {"text": f"  🌐 Workspace: Local fast storage ({cache_root.name})", "tag": "info", "newline": True})

        is_fresh_board = not (cache_root / ".pio").exists()
        if is_fresh_board:
            self.emit("console:log", {
                "text": "  🔧 First build for this board — creating its isolated workspace.",
                "tag": "info",
                "newline": True
            })
        else:
            self.emit("console:log", {
                "text": "  ℹ Incremental build enabled; successful objects are preserved.",
                "tag": "info",
                "newline": True
            })

        self.emit("console:progress", {"action": "Compiling"})

        # Ensure PlatformIO environment & INI
        try:
            pio_cmd = find_pio_executable()
            if not pio_cmd:
                self.emit("console:log", {"text": "✖ PlatformIO executable not found.", "tag": "error", "newline": True})
                self.is_busy = False
                self.emit("operation:phase", {"phase": "idle", "is_busy": False})
                return False

            # If board toolchain is not yet downloaded or verified, prepare it on-demand
            binfo = self._resolve_board_info(self.current_board)
            platform_name = str(binfo.get("platform", "")).strip()
            board_id = str(binfo.get("board", "")).strip()
            framework = str(binfo.get("framework", "arduino")).strip() or "arduino"
            if platform_name and board_id:
                try:
                    if not board_toolchain_ready(core_dir, platform_name, board_id, framework):
                        self.emit("console:log", {
                            "text": f"  ⬇ Preparing toolchain for {self.current_board} ({platform_name}:{board_id})...",
                            "tag": "info",
                            "newline": True,
                        })
                        def _on_pio_line(line):
                            self.emit("console:log", {"text": f"    {line}", "tag": "dim", "newline": True})
                        def _on_pio_status(st):
                            self.emit("console:log", {"text": f"  ℹ {st}", "tag": "info", "newline": True})
                        prepare_platformio_board_toolchain(
                            platform=platform_name,
                            board_id=board_id,
                            framework=framework,
                            label=self.current_board,
                            on_line=_on_pio_line,
                            on_status=_on_pio_status,
                        )
                except Exception as _toolchain_err:
                    self.emit("console:log", {
                        "text": f"  ⚠ Toolchain readiness check: {_toolchain_err}",
                        "tag": "dim",
                        "newline": True,
                    })

            # Pre-create build directories to avoid SCons dbm/dblite FileNotFoundError
            (cache_root / ".pio" / "build" / "mcu_env").mkdir(parents=True, exist_ok=True)
            (cache_root / ".pio" / "libdeps" / "mcu_env").mkdir(parents=True, exist_ok=True)

            self._generate_platformio_ini(cache_root)

            entry_ok, entry_owners = self._validate_entry_points()
            if entry_ok and entry_owners:
                self.emit("console:log", {
                    "text": f"  ✔ Entry points OK — setup()/loop() found in: {entry_owners}",
                    "tag": "success",
                    "newline": True
                })

            # Sync sketch sources into cache_root / "src" matching LATEST-WORKING-MCU- FLASHER
            src_dir = cache_root / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            hide_generated_directory(src_dir)

            sketch_files = {
                path.name: path
                for path in get_project_root_source_files(
                    effective_sketch_dir, (".ino", ".cpp", ".c", ".h", ".hpp")
                )
            }
            if not sketch_files and not effective_sketch_dir.is_dir():
                raise OSError(f"Could not read sketch directory '{effective_sketch_dir}'")

            ino_files = {name: path for name, path in sketch_files.items() if path.suffix.lower() == ".ino"}
            non_ino_files = {name: path for name, path in sketch_files.items() if path.suffix.lower() != ".ino"}

            for name, src_path in non_ino_files.items():
                dst_path = src_dir / name
                should_replace = True
                if dst_path.is_file():
                    try:
                        src_stat = src_path.stat()
                        dst_stat = dst_path.stat()
                        if src_stat.st_size == dst_stat.st_size:
                            should_replace = src_path.read_bytes() != dst_path.read_bytes()
                    except OSError:
                        should_replace = True
                if should_replace:
                    shutil.copy2(src_path, dst_path)

            target_ino_cpp_names: set[str] = set()
            if ino_files:
                sketch_dir_name = effective_sketch_dir.name.lower()
                primary_ino = None
                for p in ino_files.values():
                    if p.stem.lower() == sketch_dir_name:
                        primary_ino = p
                        break
                if not primary_ino:
                    SETUP_LOOP_RE = re.compile(r'\bvoid\s+(?:setup|loop)\s*\(', re.MULTILINE | re.IGNORECASE)
                    for p in ino_files.values():
                        try:
                            if SETUP_LOOP_RE.search(p.read_text(encoding="utf-8", errors="replace")):
                                primary_ino = p
                                break
                        except Exception:
                            pass
                if not primary_ino:
                    primary_ino = sorted(ino_files.values(), key=lambda p: p.name)[0]

                target_ino_cpp_name = f"{primary_ino.name}.cpp"
                target_ino_cpp_names.add(target_ino_cpp_name)
                dst_ino_cpp = src_dir / target_ino_cpp_name

                converted_code = self._convert_sketch_inos_to_cpp(primary_ino, sorted(ino_files.values(), key=lambda p: p.name))
                should_replace_ino = True
                if dst_ino_cpp.is_file():
                    try:
                        old_code = dst_ino_cpp.read_text(encoding="utf-8", errors="replace")
                        if old_code == converted_code:
                            should_replace_ino = False
                    except Exception:
                        should_replace_ino = True

                if should_replace_ino:
                    ensure_file_writable(dst_ino_cpp)
                    dst_ino_cpp.write_text(converted_code, encoding="utf-8")

                # Remove any raw .ino files from src_dir so PlatformIO doesn't delete the .cpp file at exit
                for existing_ino in list(src_dir.glob("*.ino")):
                    try:
                        ensure_file_writable(existing_ino)
                        existing_ino.unlink(missing_ok=True)
                    except OSError:
                        pass

            allowed_names = set(non_ino_files.keys()) | target_ino_cpp_names

            # Remove stale entries that no longer have a source file
            for dst_path in list(src_dir.iterdir()):
                if dst_path.name not in allowed_names:
                    try:
                        ensure_file_writable(dst_path)
                        dst_path.unlink()
                    except OSError:
                        pass

            # Dynamically determine optimal compiler workers using real-time available RAM
            configured_jobs = load_gui_config().get("compiler_jobs")
            if configured_jobs is not None and str(configured_jobs).isdigit() and int(configured_jobs) > 0:
                jobs = int(configured_jobs)
            else:
                from main.core.toolchain import get_optimal_compiler_jobs
                jobs = get_optimal_compiler_jobs()

            logical_processors = max(1, os.cpu_count() or jobs)
            reserved_processors = max(1, logical_processors - jobs) if logical_processors > jobs else 1
            reserved_word = "Processor" if reserved_processors == 1 else "Processors"
            text_part = f"⚡ Running Parallel Compilation on {jobs} Logical Processors"
            inner_w = max(73, len(text_part) + 4)

            top = " ╔" + "═" * inner_w + "╗"
            mid = "   " + text_part.center(inner_w)
            bot = " ╚" + "═" * inner_w + "╝"

            self.emit("console:log", {"text": "", "newline": True})
            self.emit("console:log", {"text": top, "tag": "header", "newline": True})
            self.emit("console:log", {"text": mid, "tag": "header", "newline": True})
            self.emit("console:log", {"text": bot, "tag": "header", "newline": True})
            self.emit("console:log", {
                "text": f"   >>> System Reserved — {reserved_processors} Logical {reserved_word} <<<",
                "tag": "dim",
                "newline": True
            })
            self.emit("console:log", {"text": "", "newline": True})
            self.emit("console:log", {"text": "  ℹ Selected-board workspace is isolated from every other board.", "tag": "info", "newline": True})
            self.emit("console:log", {"text": "    PlatformIO will compile only missing or changed units.", "tag": "dim", "newline": True})
            self.emit("console:log", {"text": "", "newline": True})
            self.emit("console:log", {"text": "  ⚙ Initializing PlatformIO build engine & dependency tree...", "tag": "purple", "newline": True})
            self.emit("console:log", {"text": "    SCons is resolving header dependencies in memory (takes 15–30s on fresh build)...", "tag": "purple_dim", "newline": True})

            cmd = pio_cmd + ["run", "-j", str(jobs)]

            # Configure high-performance SCons and PlatformIO environment variables matching LATEST-WORKING-MCU- FLASHER
            launch_env = os.environ.copy()
            core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)
            # Pre-verify SCons build engine so PlatformIO doesn't
            # re-download it mid-compile (avoids scary console noise).
            ensure_scons_ready(core_dir)
            launch_env["PLATFORMIO_CORE_DIR"] = str(core_dir)
            launch_env["PLATFORMIO_CACHE_DIR"] = str(core_dir / ".cache")
            launch_env["PLATFORMIO_GLOBALLIB_DIR"] = str(core_dir / "lib")
            launch_env["TMP"] = str(core_dir / ".tmp")
            launch_env["TEMP"] = str(core_dir / ".tmp")
            launch_env["TMPDIR"] = str(core_dir / ".tmp")
            launch_env["PYTHONUNBUFFERED"] = "1"
            launch_env["PYTHONWARNINGS"] = "ignore"
            launch_env["PLATFORMIO_UNBUFFERED"] = "1"
            launch_env["PLATFORMIO_SETTING_ENABLE_CACHE"] = "true"
            launch_env["PLATFORMIO_DISABLE_UPGRADE_CHECK"] = "1"
            launch_env["PLATFORMIO_DISABLE_PROMPTS"] = "1"
            launch_env["PLATFORMIO_NO_TELEMETRY"] = "1"
            launch_env["PLATFORMIO_DISABLE_TELEMETRY"] = "1"
            launch_env["PYTHONDONTWRITEBYTECODE"] = "0"
            launch_env.pop("PLATFORMIO_BUILD_CACHE_DIR", None)
            launch_env.pop("PYTHONOPTIMIZE", None)
            launch_env["PLATFORMIO_BUILD_JOBS"] = str(jobs)
            launch_env["PLATFORMIO_RUN_JOBS"] = str(jobs)
            launch_env["SCONSFLAGS"] = f"-j{jobs}"

            creation_flags = (
                (subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
            )
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
                startupinfo.wShowWindow = 0

            self._active_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
                startupinfo=startupinfo,
                cwd=str(cache_root),
                env=launch_env,
            )

            output_lines: list[str] = []
            _in_error_block = [False]
            _error_block_type = ["error"]
            _last_progress_text: list[str | None] = [None]
            _framework_logged_installs: set[str] = set()
            _framework_logged_done: set[str] = set()
            _framework_banner_shown = [False]
            _current_framework_item = [""]
            _first_divider_printed = [False]
            _second_divider_printed = [False]
            _deps_content_printed = [False]
            _tool_dl_start: list[float | None] = [None]
            _tool_dl_total: list[float] = [0.0]
            _build_start: list[float | None] = [None]

            def _ensure_post_deps_divider() -> None:
                if _deps_content_printed[0] and not _second_divider_printed[0]:
                    _second_divider_printed[0] = True
                    self.emit("console:log", {"text": "  ──────────────────────────────────────────────────", "tag": "purple_dim", "newline": True})

            def _diagnostic_location(raw_path: str) -> str:
                value = str(raw_path).strip().strip('"').replace("\\", "/")
                lowered = value.lower()
                for marker in ("/src/", "/lib/", "/include/"):
                    marker_pos = lowered.rfind(marker)
                    if marker_pos >= 0:
                        return value[marker_pos + 1:]
                return value.rsplit("/", 1)[-1] or value

            def _format_gcc_diagnostic(raw_line: str):
                diagnostic = re.match(
                    r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+):\s*"
                    r"(?P<kind>fatal error|error|warning|note)\s*:\s*(?P<message>.*)$",
                    raw_line,
                    re.IGNORECASE,
                )
                if diagnostic:
                    kind = diagnostic.group("kind").lower()
                    label = {
                        "fatal error": "Fatal error",
                        "error": "Error",
                        "warning": "Warning",
                        "note": "Note",
                    }.get(kind, kind.title())
                    tag = "warning" if kind == "warning" else "info" if kind == "note" else "error"
                    icon = "⚠" if kind == "warning" else "ℹ" if kind == "note" else "✖"
                    location = _diagnostic_location(diagnostic.group("file"))
                    self.emit("console:log", {
                        "text": f"  {icon} {label} at {location}:{diagnostic.group('line')}:{diagnostic.group('column')}",
                        "tag": tag,
                        "newline": True
                    })
                    message = diagnostic.group("message").strip()
                    if message:
                        self.emit("console:log", {"text": f"      {message}", "tag": tag, "newline": True})
                    return kind, False

                context = re.match(
                    r"^(?P<file>.+?):\s+(?P<context>"
                    r"(?:In function|In member function|In constructor|In destructor|At global scope|"
                    r"In file included from).*)$",
                    raw_line,
                    re.IGNORECASE,
                )
                if context:
                    self.emit("console:log", {
                        "text": f"    {_diagnostic_location(context.group('file'))}: {context.group('context').strip()}",
                        "tag": "dim",
                        "newline": True
                    })
                    return "info", True
                return None

            for line in iter(self._active_process.stdout.readline, ""):
                if self._stop_requested:
                    self._kill_active_process_tree()
                    break
                line_clean = line.rstrip("\r\n")
                if not line_clean:
                    continue
                output_lines.append(line_clean)
                low = line_clean.lower()

                LINKER_ERROR_HINTS = (
                    "undefined reference to",
                    "multiple definition of",
                    "cannot find -l",
                    "undefined symbol",
                    "duplicate symbol",
                    "ld returned",
                    "collect2",
                    "overflowed by",
                    "will not fit in region",
                    "relocation truncated",
                    "ld.exe:",
                    "ld:",
                    "section `",
                    "region `",
                )
                is_linker_error = any(hint in low for hint in LINKER_ERROR_HINTS)
                is_gcc_diagnostic = bool(re.search(r':\d+:\d+:\s+(fatal\s+error|error|warning|note)\s*:', low))
                is_scons_wrapper = bool(re.search(r'^\*\*\*\s+\[', line_clean))

                is_scons_progress = (
                    "compiling" in low or
                    "archiving" in low or
                    "linking" in low or
                    "building" in low or
                    "checking size" in low or
                    "retrieving maximum" in low or
                    "took" in low or
                    low.startswith("platform:") or
                    low.startswith("hardware:") or
                    low.startswith("package") or
                    low.startswith("embedded") or
                    low.startswith("configuration") or
                    low.startswith("sdk") or
                    "ram:" in low or
                    "flash:" in low or
                    line_clean.startswith("===") or
                    line_clean.startswith("---") or
                    is_scons_wrapper
                )

                is_context_header = (
                    "in function" in low or
                    "in member function" in low or
                    "in constructor" in low or
                    "in destructor" in low or
                    "at global scope" in low or
                    "in file included from" in low or
                    low.startswith("from ") or
                    low.startswith("in file included")
                )

                if is_scons_progress or "has been installed" in low:
                    _in_error_block[0] = False

                if any(kw in low for kw in ("compiling", "archiving", "linking", "building", "creating", "created", "checking size", "took")):
                    if _tool_dl_start[0] is not None:
                        _tool_dl_total[0] += time.time() - _tool_dl_start[0]
                        _tool_dl_start[0] = None
                    if _build_start[0] is None:
                        _build_start[0] = time.time()

                if is_gcc_diagnostic or is_context_header:
                    _in_error_block[0] = True
                    if "warning" in low and "error" not in low:
                        _error_block_type[0] = "warning"
                    elif "note" in low:
                        _error_block_type[0] = "info"
                    elif is_context_header and not is_gcc_diagnostic:
                        _error_block_type[0] = "info"
                    else:
                        _error_block_type[0] = "error"

                pct_match = re.search(r'\[\s*(\d+)%\s*\]', line_clean)
                if pct_match:
                    self.emit("console:progress", {"action": "Compiling"})

                # Swallow promotional registry noise and decoration lines
                if any(kw in low for kw in (
                    "looking for ", "check our library registry", "* cli  >", "* web  >",
                    "if you like platformio", "star it on github", "follow us on linkedin",
                    "try platformio ide", "please wait while upgrading", "successfully upgraded"
                )) or line_clean.strip().startswith("*****") or line_clean.strip() == "*":
                    continue

                # Route line display
                if is_linker_error:
                    _ensure_post_deps_divider()
                    self.emit("console:log", {"text": f"  ✖ {line_clean}", "tag": "error", "newline": True})
                    _in_error_block[0] = False
                elif is_gcc_diagnostic or is_context_header:
                    _ensure_post_deps_divider()
                    if _format_gcc_diagnostic(line_clean) is None:
                        prefix = "    " if is_context_header else "      "
                        self.emit("console:log", {"text": f"{prefix}{line_clean}", "tag": _error_block_type[0], "newline": True})
                elif _in_error_block[0]:
                    prefix = "    " if is_context_header else "      "
                    self.emit("console:log", {"text": f"{prefix}{line_clean}", "tag": _error_block_type[0], "newline": True})
                    if "compilation terminated" in low:
                        _in_error_block[0] = False
                elif is_scons_wrapper:
                    pass  # Swallow SCons wrapper noise
                elif line_clean.startswith("Processing") or ("processing " in low and "(" in low):
                    self.emit("console:log", {"text": f"    {line_clean}", "tag": "purple", "newline": True})
                    if not _first_divider_printed[0]:
                        _first_divider_printed[0] = True
                        self.emit("console:log", {"text": "  ──────────────────────────────────────────────────", "tag": "purple_dim", "newline": True})
                elif "tool manager:" in low or "platform manager:" in low or "tool-manager:" in low or "platform-manager:" in low:
                    manager_kind = "Toolchain/Tool" if ("tool" in low) else "Platform/Framework"
                    item = re.sub(r'^(?:tool|platform)[\s-]manager:\s*', '', line_clean, flags=re.IGNORECASE).strip()
                    item = re.sub(r'^(?:installing|downloading|unpacking)\s+', '', item, flags=re.IGNORECASE).strip()
                    item = re.split(r'\s+has been installed!?$', item, flags=re.IGNORECASE)[0].strip()
                    _current_framework_item[0] = item
                    if "installing" in low:
                        if item not in _framework_logged_installs:
                            _framework_logged_installs.add(item)
                            self.emit("console:log", {"text": f"  🔎 Checking {manager_kind}: {item}", "tag": "info", "newline": True})
                            _deps_content_printed[0] = True
                    elif "installed" in low:
                        if item not in _framework_logged_done:
                            _framework_logged_done.add(item)
                            item_label = item if len(item) <= 44 else item[:41] + "..."
                            full_bar = "▰" * 30
                            self.emit("console:log", {
                                "text": f"  ✔ Unpacking   [{item_label}]  {full_bar}  100%",
                                "tag": "success",
                                "replace_pattern": rf"(?:Downloading|Unpacking)\s+\[{re.escape(item_label)}\]",
                                "newline": True
                            })
                            self.emit("console:log", {"text": f"  ✔ Installed {manager_kind}: {item}", "tag": "success", "newline": True})
                            _deps_content_printed[0] = True
                elif "library manager:" in low:
                    if not _first_divider_printed[0]:
                        _first_divider_printed[0] = True
                        self.emit("console:log", {"text": "  ──────────────────────────────────────────────────", "tag": "purple_dim", "newline": True})
                    formatted_lib_line = line_clean
                    formatted_lib_line = re.sub(r'\bInstalling\b', 'Linking', formatted_lib_line)
                    formatted_lib_line = re.sub(r'\binstalling\b', 'linking', formatted_lib_line)
                    formatted_lib_line = re.sub(r'\bhas been installed\b', 'has been linked', formatted_lib_line, flags=re.IGNORECASE)
                    formatted_lib_line = re.sub(r'\bInstalled\b', 'Linked', formatted_lib_line)
                    self.emit("console:log", {"text": f"    {formatted_lib_line}", "tag": "info", "newline": True})
                    _deps_content_printed[0] = True
                elif "downloading" in low or "unpacking" in low:
                    if _tool_dl_start[0] is None:
                        _tool_dl_start[0] = time.time()
                    if not _framework_banner_shown[0]:
                        _framework_banner_shown[0] = True
                        self.emit("console:log", {"text": "", "newline": True})
                        self.emit("console:log", {"text": "  ────────────────────────────────────────────────────────────────────────────", "tag": "warning", "newline": True})
                        self.emit("console:log", {"text": "  ⚠ Preparing required core framework & toolchain packages...", "tag": "warning", "newline": True})
                        self.emit("console:log", {"text": "    A required shared PlatformIO package is actually being downloaded/unpacked.", "tag": "info", "newline": True})
                        self.emit("console:log", {"text": "    Keep the application open until package preparation finishes.", "tag": "info", "newline": True})
                        self.emit("console:log", {"text": "  ────────────────────────────────────────────────────────────────────────────", "tag": "warning", "newline": True})
                        self.emit("console:log", {"text": "", "newline": True})

                    pcts = re.findall(r'(\d+)%', line_clean)
                    if pcts:
                        pct = int(pcts[-1])
                        filled = int(pct / 100 * 30)
                        bar = "▰" * filled + "▱" * (30 - filled)
                        act_name = "Downloading" if "downloading" in low else "Unpacking"
                        item = _current_framework_item[0] or "platformio/tool-scons @ ~4.41101.0"
                        item_label = item if len(item) <= 44 else item[:41] + "..."
                        icon = "✔ " if pct >= 100 else "  "
                        progress_text = f"  {icon}{act_name:<11} [{item_label}]  {bar}  {pct:3d}%"
                        self.emit("console:log", {
                            "text": progress_text,
                            "tag": "success" if pct >= 100 else "info",
                            "replace_pattern": rf"(?:Downloading|Unpacking)\s+\[{re.escape(item_label)}\]",
                            "newline": True
                        })
                elif is_scons_progress:
                    _prog_text = None
                    _prog_tag = "dim"
                    if "linking" in low:
                        _prog_text, _prog_tag = "  🔗 Linking...", "dim"
                    elif "checking size" in low or "retrieving maximum" in low:
                        _prog_text, _prog_tag = "  📏 Checking firmware size...", "dim"
                    elif "compiling" in low:
                        match = re.search(r'compiling\s+(.+)$', low)
                        if match:
                            filename = Path(match.group(1)).name
                            _prog_text = f"  ⚙ Compiling {filename}..."
                            _prog_tag = "info"
                        else:
                            _prog_text = f"  ⚙ {line_clean}"
                            _prog_tag = "info"
                    elif "archiving" in low:
                        _prog_text, _prog_tag = "  📦 Archiving...", "dim"
                    elif "building" in low:
                        match = re.search(r'building\s+(.+)$', line_clean, re.IGNORECASE)
                        if match:
                            raw_target = re.sub(r'\s+with\s+action:?.*$', '', match.group(1).strip(), flags=re.IGNORECASE)
                            target_name = Path(raw_target.strip().strip('"')).name
                            if target_name:
                                if "bootloader" in target_name.lower():
                                    _prog_text = f"  ⚡ Building bootloader image ({target_name})..."
                                    _prog_tag = "info"
                                elif "partition" in target_name.lower():
                                    _prog_text = f"  ⚡ Building partition table ({target_name})..."
                                    _prog_tag = "info"
                                elif "firmware" in target_name.lower() or target_name.endswith((".bin", ".hex")):
                                    _prog_text = f"  ⚡ Building firmware image ({target_name})..."
                                    _prog_tag = "info"
                                else:
                                    _prog_text = f"  ⚙ Building {target_name}..."
                                    _prog_tag = "info"
                            else:
                                _prog_text = "  ⚙ Building..."
                                _prog_tag = "info"
                        else:
                            _prog_text = "  ⚙ Building..."
                            _prog_tag = "info"

                    if _prog_text and _prog_text != _last_progress_text[0]:
                        _ensure_post_deps_divider()
                        _last_progress_text[0] = _prog_text
                        self.emit("console:log", {"text": _prog_text, "tag": _prog_tag, "newline": True})
                elif "error:" in low and "werror" not in low:
                    _ensure_post_deps_divider()
                    self.emit("console:log", {"text": f"  ✖ {line_clean}", "tag": "error", "newline": True})
                elif "warning:" in low:
                    _ensure_post_deps_divider()
                    self.emit("console:log", {"text": f"  ⚠ {line_clean}", "tag": "warning", "newline": True})

            self._active_process.stdout.close()
            rc = self._active_process.wait()

            duration = round(time.time() - start_time, 2)
            was_stopped = getattr(self, "_stop_requested", False) or (sys.platform != "win32" and rc < 0)

            if was_stopped:
                self._clean_temporary_compile_artifacts(cache_root)
                self.emit("console:log", {"text": "", "newline": True})
                self.emit("console:log", {"text": "  ■ Compilation stopped safely by user.", "tag": "warning", "newline": True})
                self.emit("console:log", {"text": "  ♻ Temporary build artifacts cleared. Project is ready to be rebuilt.", "tag": "info", "newline": True})
                self.is_busy = False
                self.active_operation = None
                self._current_op_phase = None
                self.emit("operation:phase", {"phase": "idle", "is_busy": False})
                self.emit("window:closable", {"closable": True})
                return False
            elif rc == 0:
                if _tool_dl_start[0] is not None:
                    _tool_dl_total[0] += time.time() - _tool_dl_start[0]
                    _tool_dl_start[0] = None

                self._last_source_hash = self._hash_sources()
                self._last_compiled_board = self.current_board

                captured_meta: dict[str, Any] = {}
                for raw in output_lines:
                    line = _strip_terminal_escapes(raw)
                    low = line.lower()
                    if low.startswith("debug:") and "debug" not in captured_meta:
                        captured_meta["debug"] = line
                    elif low.startswith("ram:") and "ram" not in captured_meta:
                        captured_meta["ram"] = line
                    elif low.startswith("flash:") and "flash" not in captured_meta:
                        captured_meta["flash"] = line
                if captured_meta:
                    captured_meta["env_name"] = self._pio_env_name(self.current_board)
                    captured_meta["source_hash"] = self._last_source_hash
                    try:
                        board_build_dir = cache_root / ".pio" / "build" / "mcu_env"
                        if not board_build_dir.is_dir():
                            board_build_dir = cache_root / ".pio" / "build" / self._pio_env_name(self.current_board)
                        if not board_build_dir.is_dir():
                            board_build_dir = self._effective_cache_root(self.sketch_dir_path) / "boards" / self._board_cache_key(self.current_board) / ".pio" / "build" / self._pio_env_name(self.current_board)
                        firmware = board_build_dir / "firmware.bin"
                        if not firmware.exists():
                            firmware = board_build_dir / "firmware.hex"
                        if firmware.exists():
                            stat = firmware.stat()
                            captured_meta["firmware_size"] = stat.st_size
                            captured_meta["firmware_mtime_ns"] = stat.st_mtime_ns
                    except OSError:
                        pass

                self._save_compile_cache(self.current_board, self._last_source_hash, build_metadata=captured_meta or None)
                self.update_skip_compile_availability()
                if not is_upload:
                    self.emit("console:progress", {"action": "Completed"})

                for line in output_lines:
                    low = line.lower()
                    if any(kw in low for kw in ("ram:", "flash:")):
                        self.emit("console:log", {"text": f"  {line.strip()}", "tag": "success", "newline": True})

                # Artifact confirmation banner
                try:
                    target_env_dir = cache_root / ".pio" / "build" / "mcu_env"
                    if not target_env_dir.is_dir():
                        target_env_dir = cache_root / ".pio" / "build" / self._pio_env_name(self.current_board)
                    fw_file = target_env_dir / "firmware.bin"
                    if not fw_file.exists():
                        fw_file = target_env_dir / "firmware.hex"
                    bl_file = target_env_dir / "bootloader.bin"
                    pt_file = target_env_dir / "partitions.bin"
                    if fw_file.exists():
                        fw_kb = round(fw_file.stat().st_size / 1024, 1)
                        binfo = self._resolve_board_info(self.current_board)
                        chip_label = str(binfo.get("board", "")).upper() or self.current_board
                        self.emit("console:log", {"text": f"  ✔ Successfully created {chip_label} image ({fw_file.name})", "tag": "success", "newline": True})
                        self.emit("console:log", {"text": f"  📦 Binary artifact: {fw_file.name} ({fw_kb} KB) ready for upload", "tag": "success", "newline": True})
                    if bl_file.exists() and pt_file.exists():
                        self.emit("console:log", {"text": "  📦 Bootloader & partitions verified OK", "tag": "dim", "newline": True})
                except Exception:
                    pass

                total_sec = round(time.time() - start_time, 1)
                tool_dl_sec = round(_tool_dl_total[0], 1)
                if _build_start[0] is not None:
                    build_sec = max(0.0, round(time.time() - _build_start[0], 1))
                    build_sec = min(build_sec, total_sec)
                else:
                    build_sec = max(0.0, round(total_sec - tool_dl_sec, 1))

                self.emit("console:log", {"text": "", "newline": True})
                if tool_dl_sec >= 1.5:
                    breakdown_fields = [
                        ("Framework & Tool Download", f"{tool_dl_sec}s"),
                        ("Code Build & Compilation",  f"{build_sec}s"),
                        ("Total Elapsed Time",        f"{total_sec}s"),
                    ]
                    self._print_info_box("Compilation Time Breakdown", breakdown_fields)
                    self.emit("console:log", {
                        "text": f"  ✔ Compilation successful! (Build: {build_sec}s | Tools Download: {tool_dl_sec}s | Total: {total_sec}s)",
                        "tag": "success",
                        "newline": True
                    })
                else:
                    breakdown_fields = [
                        ("Code Build & Compilation",  f"{build_sec}s"),
                        ("Total Elapsed Time",        f"{total_sec}s"),
                    ]
                    self._print_info_box("Compilation Time Breakdown", breakdown_fields)
                    self.emit("console:log", {
                        "text": f"  ✔ Compilation successful! ({total_sec}s)",
                        "tag": "success",
                        "newline": True
                    })

                self._refresh_compatible_devices(force=True)
                self.emit("console:log", {
                    "text": "  ℹ Compatible devices analysis updated → see the '🔧 Compatible Devices' tab.",
                    "tag": "dim",
                    "newline": True
                })

                # ── Check for Selected Board Hardware Compatibility ─────────────
                selected_board_name = str(self.current_board or "")
                try:
                    gpio_analysis = _analyze_gpio_compatibility(self.sketch_dir_path)
                    if selected_board_name in gpio_analysis.get("excluded", set()):
                        bad_hits = gpio_analysis.get("pin_hits", {}).get(selected_board_name, [])
                        bad_pins = sorted({pin for pin, _ in bad_hits})
                        if bad_pins:
                            pins_formatted = ", ".join(f"GPIO {p}" for p in bad_pins)
                            is_s3_selected = "s3" in selected_board_name.lower()
                            max_gpio = 48 if is_s3_selected else 39

                            self.emit("console:log", {"text": "", "newline": True})
                            self.emit("console:log", {"text": "  🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  ════════════════════════════════════════════════════════════════════════════", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  CRITICAL HARDWARE INCOMPATIBILITY DETECTED ON SELECTED BOARD!", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  ════════════════════════════════════════════════════════════════════════════", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": f"  SELECTED BOARD : {selected_board_name.upper()}", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": f"  INVALID GPIO(S): {pins_formatted.upper()} IS OUT OF HARDWARE RANGE (MAX VALID: GPIO {max_gpio})!", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  ", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  THE COMPILER SUCCEEDED BUT THIS CODE WILL NOT WORK AT RUNTIME ON THIS BOARD!", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": f"  {selected_board_name.upper()} DOES NOT HAVE {pins_formatted.upper()} CONNECTED OR ACCESSIBLE ON THIS CHIP/BOARD VARIANT!", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  ", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  BOARD PINOUT VARIANT CONSIDERATIONS (30-PIN / 38-PIN / 44-PIN BOARDS):", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  - ESP32 DEV MODULE (30-PIN & 38-PIN BOARDS): HARDWARE GPIO RANGE IS 0-39 (MAX GPIO 39).", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  - ESP32-S3 DEV MODULE (38-PIN & 44-PIN BOARDS): HARDWARE GPIO RANGE IS 0-48 (MAX GPIO 48).", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "    NOTE: ON 38-PIN ESP32-S3 VARIANTS, HIGHER PINS (GPIO 45-48) & PSRAM PINS (GPIO 26-32)", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "    MAY NOT BE BROKEN OUT TO PHYSICAL HEADERS OR MAY BE USED BY ONBOARD NEOPIXEL/FLASH.", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  ", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  RECOMMENDED ACTION:", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  - CHANGE THE PIN IN YOUR CODE TO AN ACCESSIBLE GPIO (E.G., GPIO 2, 4, 16, 17), OR", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  - SWITCH SELECTED BOARD TO ESP32-S3 DEV MODULE IF YOUR PHYSICAL BOARD USES AN S3 CHIP!", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  ════════════════════════════════════════════════════════════════════════════", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "  🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨", "tag": "severe_alert", "newline": True})
                            self.emit("console:log", {"text": "", "newline": True})
                except Exception:
                    pass

                self.emit("notification", {"title": "Build Succeeded", "message": f"Compilation finished in {total_sec}s", "type": "success"})
                if not is_upload:
                    self.is_busy = False
                    self.active_operation = None
                    self._current_op_phase = None
                    self.emit("operation:phase", {"phase": "idle", "is_busy": False})
                    self.emit("window:closable", {"closable": True})
                return True
            else:
                self._clean_temporary_compile_artifacts(cache_root)
                failure_kind = classify_platformio_failure(output_lines)
                if failure_kind == "cache" and not is_clean_retry:
                    self.emit("console:log", {"text": "", "newline": True})
                    self.emit("console:log", {"text": "  ⚠ Selected-board build cache inconsistency detected.", "tag": "warning", "newline": True})
                    self.emit("console:log", {"text": "  ♻ Automatically repairing workspace cache and retrying build...", "tag": "info", "newline": True})
                    self._clean_board_cache(self.current_board)
                    return self._compile_worker(is_upload=is_upload, is_clean_retry=True)
                elif failure_kind == "cache":
                    self.emit("console:log", {"text": "  ⚠ Selected-board cache repair was already attempted once; preserving diagnostics and stopping.", "tag": "warning", "newline": True})

                self.emit("console:log", {"text": "", "newline": True})
                self.emit("console:log", {"text": f"  ✖ Compilation FAILED after {duration}s:", "tag": "error", "newline": True})

                parsed_errors = []
                err_pattern = re.compile(
                    r'^(?:([a-zA-Z]:[\\/][^:]+)|([^:]+)):(\d+):(\d+):\s+(fatal\s+error|error|warning|note):\s+(.+)$',
                    re.IGNORECASE
                )
                for l in output_lines:
                    m = err_pattern.match(l.strip())
                    if m:
                        file_path = m.group(1) or m.group(2)
                        line_num = m.group(3)
                        col_num = m.group(4)
                        error_type = m.group(5)
                        error_msg = m.group(6)
                        parsed_errors.append({
                            "file": _diagnostic_location(file_path),
                            "line": line_num,
                            "col": col_num,
                            "type": error_type,
                            "msg": error_msg.strip()
                        })

                # ── Check if this is a board-mismatch rather than a real code bug ──
                selected_board = str(self.current_board or "")
                try:
                    compat_boards, compat_reasons = detect_board_compatibility(self.sketch_dir_path)
                    is_board_mismatch = bool(compat_boards) and selected_board not in compat_boards
                except Exception:
                    is_board_mismatch = False
                    compat_boards = set()

                if is_board_mismatch:
                    # ── Board mismatch: show a single, clear message ──
                    _platforms = {
                        SUPPORTED_BOARDS.get(b, {}).get("platform", "")
                        for b in compat_boards
                    }
                    _plat_labels = {
                        "espressif32": "ESP32-family",
                        "espressif8266": "ESP8266-family",
                        "atmelavr": "Arduino AVR (Uno, Nano, Mega …)",
                    }
                    family_names = sorted(
                        str(_plat_labels.get(p, p) or p) for p in _platforms if p
                    )
                    compat_summary = ", ".join(family_names) if family_names else "other boards"
                    self.emit("console:log", {"text": "", "newline": True})
                    self.emit("console:log", {"text": "  ⚠ This sketch is NOT compatible with the selected board.", "tag": "warning", "newline": True})
                    self.emit("console:log", {"text": f"     Selected board : {selected_board}", "tag": "warning", "newline": True})
                    self.emit("console:log", {"text": f"     Compatible with: {compat_summary}", "tag": "info", "newline": True})
                    self.emit("console:log", {"text": "", "newline": True})
                    self.emit("console:log", {"text": "  💡 Please select a compatible board and try again.", "tag": "info", "newline": True})
                elif parsed_errors:
                    seen_issues = set()
                    errors_by_file: dict[str, list[dict]] = {}
                    for err in parsed_errors:
                        key = (err["file"], err["line"], err["col"], err["type"].lower(), err["msg"])
                        if key not in seen_issues:
                            seen_issues.add(key)
                            errors_by_file.setdefault(err["file"], []).append(err)

                    has_real_errors = any(
                        "error" in e["type"].lower() and "warning" not in e["type"].lower()
                        for e in parsed_errors
                    )
                    header_tag = "error" if has_real_errors else "warning"
                    self.emit("console:log", {"text": "⚠️  ISSUES LISTED BY FILE:", "tag": header_tag, "newline": True})
                    self.emit("console:log", {"text": "─" * 50, "tag": header_tag, "newline": True})

                    for fname, errs in errors_by_file.items():
                        self.emit("console:log", {"text": f"  📁 {fname}", "tag": "warning", "newline": True})
                        for e in errs:
                            severity = e["type"].lower().strip()
                            tag = "info" if "note" in severity else ("warning" if "warning" in severity else "error")
                            bullet = "•" if tag == "error" else ("⚠" if tag == "warning" else "ℹ")
                            self.emit("console:log", {
                                "text": f"     {bullet} Line {e['line']} (Col {e['col']}): {severity} — {e['msg']}",
                                "tag": tag,
                                "newline": True
                            })
                    self.emit("console:log", {"text": "─" * 50, "tag": header_tag, "newline": True})

                self.emit("notification", {"title": "Build Failed", "message": f"Compilation failed with exit code {rc}", "type": "error"})
                self.is_busy = False
                self.active_operation = None
                self._current_op_phase = None
                self.emit("operation:phase", {"phase": "idle", "is_busy": False})
                self.emit("window:closable", {"closable": True})
                return False

        except Exception as e:
            self.emit("console:log", {"text": f"✖ Fatal compile error: {e}", "tag": "error", "newline": True})
            self.is_busy = False
            self.active_operation = None
            self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False})
            self.emit("window:closable", {"closable": True})
            return False
        finally:
            if not is_upload:
                self._unmap_unc_after_build()

    _board_info_ram_cache: dict[str, dict[str, Any]] = {}

    def _resolve_board_info(self, board_name: str | None = None) -> dict[str, Any]:
        """Resolve board parameters (platform, board ID, framework) with process-wide RAM cache."""
        if not board_name:
            board_name = getattr(self, "current_board", "") or ""
        if not board_name:
            return {
                "platform": "espressif32",
                "board": "esp32dev",
                "framework": "arduino",
            }
        cache_key = board_name.strip().lower()
        if cache_key in self._board_info_ram_cache:
            return self._board_info_ram_cache[cache_key]

        if board_name in SUPPORTED_BOARDS:
            info = dict(SUPPORTED_BOARDS[board_name])
            self._board_info_ram_cache[cache_key] = info
            return info
        for k, v in SUPPORTED_BOARDS.items():
            if k.lower() == cache_key:
                info = dict(v)
                self._board_info_ram_cache[cache_key] = info
                return info

        default_info = {
            "platform": "espressif32",
            "board": "esp32dev",
            "framework": "arduino",
        }
        self._board_info_ram_cache[cache_key] = default_info
        return default_info

    def _hash_sources(self, board_name: str | None = None) -> str:
        """Calculate MD5 digest of all sketch sources for build cache hit detection."""
        hasher = hashlib.md5()
        try:
            files = sorted(get_sketch_files_fast(self.sketch_dir_path), key=lambda p: p.name)
            for f in files:
                hasher.update(f.name.encode("utf-8"))
                content = _sketch_ram_cache.get_content(f)
                if content is not None:
                    hasher.update(content.encode("utf-8", errors="replace"))
                else:
                    hasher.update(f.read_bytes())
        except Exception:
            pass
        return hasher.hexdigest()

    def _detect_sketch_baud_rate(self) -> Optional[str]:
        """Scan active sketch directory for Serial.begin(...) calls and return a valid baud rate string, or None."""
        if not self.sketch_dir_path or not self.sketch_dir_path.is_dir():
            return None
        valid_baud_rates = {b for b in VALID_BAUD_RATES if b <= MAX_BAUD_RATE}
        try:
            curr_hash = self._hash_sources()
            hit, cached_baud = _sketch_ram_cache.get_baud_rate(self.sketch_dir_path, curr_hash)
            if hit:
                return cached_baud

            macros: dict[str, int] = {}
            sketch_files = get_sketch_files_fast(self.sketch_dir_path)
            for file_path in sketch_files:
                content = _sketch_ram_cache.get_content(file_path)
                if content is None:
                    continue

                content_clean = re.sub(r'//.*?\n|/\*.*?\*/', '', content, flags=re.DOTALL)
                for macro_match in re.finditer(r"#define\s+([A-Za-z_][A-Za-z0-9_]*)\s+([0-9]+)\b", content_clean):
                    macros[macro_match.group(1)] = int(macro_match.group(2))
                for const_match in re.finditer(r"(?:const\s+)?(?:unsigned\s+)?(?:long|int|uint32_t)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([0-9]+)\b", content_clean):
                    macros[const_match.group(1)] = int(const_match.group(2))

                for match in re.finditer(r"\bSerial[0-9A-Za-z_]*\.begin\s*\(\s*([A-Za-z0-9_]+)\b", content_clean):
                    raw_val = match.group(1)
                    if raw_val.isdigit():
                        candidate = int(raw_val)
                        if candidate in valid_baud_rates:
                            _sketch_ram_cache.set_baud_rate(self.sketch_dir_path, curr_hash, str(candidate))
                            return str(candidate)
                    elif raw_val in macros:
                        candidate = macros[raw_val]
                        if candidate in valid_baud_rates:
                            _sketch_ram_cache.set_baud_rate(self.sketch_dir_path, curr_hash, str(candidate))
                            return str(candidate)

            _sketch_ram_cache.set_baud_rate(self.sketch_dir_path, curr_hash, None)
        except Exception:
            pass
        return None

    def _scan_includes_for_libs(self) -> list[str]:
        """Scan sketch files for external #include directives to populate lib_deps."""
        if not self.sketch_dir_path or not self.sketch_dir_path.is_dir():
            return []
        detected_headers: set[str] = set()
        try:
            files = get_sketch_files_fast(self.sketch_dir_path)
            for f in files:
                for hdr in _sketch_ram_cache.get_includes(f):
                    detected_headers.add(hdr)
        except Exception:
            pass
        core_headers = {
            "Arduino.h", "stdint.h", "stdlib.h", "stdio.h", "string.h", "math.h",
            "avr/io.h", "avr/pgmspace.h", "avr/interrupt.h", "WiFi.h", "SPI.h", "Wire.h",
            "EEPROM.h", "FS.h", "SPIFFS.h", "SD.h", "Update.h", "esp_system.h"
        }
        return sorted([h for h in detected_headers if h not in core_headers])

    def _print_info_box(self, title: str, fields: list[tuple[str, str]]) -> None:
        """Render a boxed information panel into the build console using double-line border."""
        if not fields:
            return
        label_width = max(len(label) for label, _ in fields)
        label_col_width = label_width + 3  # "label" + " : "

        available_cols = 75
        max_inner_width = max(available_cols - 4, label_col_width + 10, len(title))
        value_max_width = max(max_inner_width - label_col_width, 10)

        rows = []
        for label, value in fields:
            chunks = textwrap.wrap(str(value), value_max_width) or [""]
            for i, chunk in enumerate(chunks):
                label_field = f"{label:<{label_width}} : " if i == 0 else " " * label_col_width
                rows.append((label_field, chunk))

        inner_width = max(len(title), max(len(lf) + len(v) for lf, v in rows))

        top       = "╔" + "═" * (inner_width + 2) + "╗"
        title_row = "║ " + title.center(inner_width) + " ║"
        sep       = "╠" + "═" * (inner_width + 2) + "╣"
        bottom    = "╚" + "═" * (inner_width + 2) + "╝"

        self.emit("console:log", {"text": "", "newline": True})
        self.emit("console:log", {"text": top, "tag": "purple_header", "newline": True})
        self.emit("console:log", {"text": title_row, "tag": "purple_header", "newline": True})
        self.emit("console:log", {"text": sep, "tag": "purple_header", "newline": True})
        for label_field, value_chunk in rows:
            value_field = value_chunk.ljust(inner_width - len(label_field))
            line_str = f"║ {label_field}{value_field} ║"
            self.emit("console:log", {"text": line_str, "tag": "purple_header", "newline": True})
        self.emit("console:log", {"text": bottom, "tag": "purple_header", "newline": True})
        self.emit("console:log", {"text": "", "newline": True})

    def _print_chip_info_box(self, chip_model: str, fields: list[tuple[str, str]]) -> None:
        """Render a boxed chip-info panel into the build console."""
        self._print_info_box(f"{chip_model} Information", fields)

    def _append_connecting_progress(self, current: int, total: int, bar_width: int = 30,
                                    connected: bool = False, failed: bool = False):
        if failed:
            current = total
        current = max(0, min(total, current))
        if total > 0:
            multiplier = max(1, round(bar_width / total))
            width = total * multiplier
        else:
            width = bar_width
            multiplier = 1
        filled = current * multiplier
        bar = "▰" * filled + "▱" * max(0, width - filled)

        if failed:
            text = f"  🔌 Connecting [ {bar} ] | FAILED >> 💡 Please hold 'BOOT' button on MCU physical board"
            tag = "error"
        elif connected:
            text = f"  ✔ Connected [ {bar} ] | {current}/{total}"
            tag = "success"
        else:
            text = f"  🔌 Connecting [ {bar} ] | {current}/{total}"
            text += " >> 💡 Please hold 'BOOT' button on MCU physical board"
            tag = "magenta"

        self.emit("console:log", {
            "text": text,
            "tag": tag,
            "replace_pattern": r"(?:connecting|connected)\s*\[.*\]\s*\|\s*(?:\d+/\d+|FAILED)",
            "newline": True
        })

    def _append_upload_progress(self, label: str, stage: int, stage_total: int,
                                percent: float, written: int | None = None,
                                total: int | None = None,
                                force_new: bool = False) -> None:
        """Render responsive, in-place firmware flashing progress."""
        bar_width = 30
        pct = max(0.0, min(100.0, float(percent)))
        filled = int(pct / 100.0 * bar_width)
        bar = "▰" * filled + "▱" * max(0, bar_width - filled)
        icon = "✔" if pct >= 100.0 else "⚙"
        stage_str = f"[{stage}/{stage_total}] " if stage_total > 1 else ""
        size_label = ""
        if written is not None and total is not None:
            if total > 1024:
                size_label = f" | {written // 1024}KB/{total // 1024}KB"
            else:
                size_label = f" | {written}/{total}B"

        progress_text = f"  {icon} Flashing {stage_str}{label} [ {bar} ] | {pct:5.1f}%{size_label}"
        self.emit("console:log", {
            "text": progress_text,
            "tag": "success" if pct >= 100.0 else "info",
            "replace_pattern": rf"(?:Flashing)\s+{re.escape(stage_str + label)}\s*\[" if not force_new else None,
            "newline": True,
        })
        self.emit("console:progress", {"action": "Uploading"})

    def _new_upload_progress_state(self, fast_bins: dict | None = None) -> dict:
        """Create image-tracking state shared by both ESP upload paths."""
        board_info = dict(self._resolve_board_info(self.current_board or ""))
        platform = (fast_bins or {}).get("platform") or str(board_info.get("platform", "")).lower()
        definitions = (
            [("firmware", "Firmware")]
            if platform == "espressif8266"
            else [
                ("bootloader", "Bootloader"),
                ("partitions", "Partitions"),
                ("boot_app0", "Boot App"),
                ("firmware", "Firmware"),
            ]
        )
        stages = []
        for key, label in definitions:
            path_value = (fast_bins or {}).get(key)
            path_obj = Path(path_value) if path_value else None
            stages.append({
                "key": key,
                "label": label,
                "path": path_obj,
                "basename": path_obj.name.lower() if path_obj else "",
            })
        return {
            "stages": stages,
            "active_index": 0,
            "started": False,
            "last_percent": None,
            "compressed_total": None,
        }

    @staticmethod
    def _select_upload_stage_for_source(state: dict, source: str) -> None:
        """Select an image once from its path or filename."""
        source_name = Path(str(source).replace("\\", "/")).name.lower()
        aliases = {
            "bootloader.bin": "bootloader",
            "partitions.bin": "partitions",
            "boot_app0.bin": "boot_app0",
            "firmware.bin": "firmware",
        }
        wanted_key = aliases.get(source_name)
        for idx, stage in enumerate(state.get("stages") or []):
            if (stage.get("basename") and stage["basename"] == source_name
                    or wanted_key and stage.get("key") == wanted_key):
                if state.get("active_index") != idx:
                    state["active_index"] = idx
                    state["last_percent"] = None
                    state["compressed_total"] = None
                    state["started"] = False
                return

    @staticmethod
    def _select_upload_stage_for_address(state: dict, address: str | int) -> None:
        """Map a flash memory address to an upload stage key using ESP flash memory layout."""
        try:
            int_addr = int(str(address), 16) if isinstance(address, str) else int(address)
        except Exception:
            return

        stages = state.get("stages") or []
        if len(stages) <= 1:
            return

        if int_addr < 0x8000:
            wanted_key = "bootloader"
        elif int_addr < 0x9000:
            wanted_key = "partitions"
        elif int_addr < 0x10000:
            wanted_key = "boot_app0"
        else:
            wanted_key = "firmware"

        for idx, stage in enumerate(state.get("stages") or []):
            if stage.get("key") == wanted_key:
                if state.get("active_index") != idx:
                    state["active_index"] = idx
                    state["last_percent"] = None
                    state["compressed_total"] = None
                    state["started"] = False
                return

    def _consume_esptool_upload_progress(self, state: dict, line: str,
                                         before_progress=None,
                                         phase_callback=None) -> bool:
        """Consume/suppress one raw esptool image or progress line.
        Returns True when the caller should not display the raw line.
        """
        image_start = _parse_esptool_image_start(line)
        if image_start:
            self._select_upload_stage_for_source(state, image_start["source"])
            if image_start.get("address"):
                self._select_upload_stage_for_address(state, image_start["address"])
            state["stage_locked"] = True
            if callable(phase_callback):
                phase_callback("Writing")
            return True

        compressed = _parse_esptool_compressed(line)
        if compressed:
            state["compressed_total"] = compressed["compressed"]
            if callable(phase_callback):
                phase_callback("Writing")
            return True

        progress = _parse_esptool_write_progress(line)
        if progress:
            # esptool v5 reports running write pointer (address + bytes_written) in the progress description.
            # Never use the running pointer to change stages when a stage is locked or in v5,
            # as it will hit the boundary address of the next stage (e.g. 0xe000 + 0x2000 = 0x10000) at 100%!
            if progress.get("version") != 5 and not state.get("stage_locked"):
                if progress.get("address"):
                    self._select_upload_stage_for_address(state, progress["address"])
            if callable(before_progress):
                before_progress()
            if callable(phase_callback):
                phase_callback("Writing")
            stages = state.get("stages") or [{"label": "Firmware"}]
            idx = max(0, min(int(state.get("active_index", 0)), len(stages) - 1))
            stage = stages[idx]
            self._append_upload_progress(
                stage.get("label") or "Firmware",
                idx + 1,
                len(stages),
                progress["percent"],
                progress.get("written"),
                progress.get("total"),
                force_new=not bool(state.get("started")),
            )
            state["started"] = True
            state["last_percent"] = progress["percent"]
            return True

        wrote = _parse_esptool_wrote(line)
        if wrote:
            if wrote.get("address"):
                self._select_upload_stage_for_address(state, wrote["address"])
            state["stage_locked"] = False
            if callable(before_progress):
                before_progress()
            if callable(phase_callback):
                phase_callback("Writing")
            stages = state.get("stages") or []
            if stages:
                idx = max(0, min(int(state.get("active_index", 0)), len(stages) - 1))
                stage = stages[idx]
                pct = 100.0
                file_bytes = wrote.get("compressed") or wrote.get("raw")
                self._append_upload_progress(
                    stage.get("label") or "Firmware",
                    idx + 1,
                    len(stages),
                    pct,
                    file_bytes,
                    file_bytes,
                    force_new=not bool(state.get("started")),
                )
                state["started"] = True
            return True

        return False

    def _is_port_present(self, port: str | None = None) -> bool:
        """Check if the given COM port is currently enumerated by the operating system."""
        target = str(port or getattr(self, "current_port", "") or "").strip().upper()
        if not target:
            return False
        match = re.match(r"(COM\d+|/dev/\S+)", target)
        dev_target = match.group(1) if match else target
        try:
            for candidate in serial.tools.list_ports.comports():
                c_dev = str(candidate.device or "").strip().upper()
                if c_dev == target or c_dev == dev_target:
                    return True
            return False
        except Exception:
            return True

    def _is_native_usb_port(self, port: str | None = None) -> bool:
        """Detect if the specified or current port uses a native USB-CDC / OTG connection.
        Genuine Espressif native USB controllers use VID 0x303A. External USB-to-UART bridge
        chips (CH34x, CP210x, FTDI) use standard DTR/RTS auto-reset circuits and must NEVER
        be configured with 'usb-reset'.
        """
        target_str = str(port or getattr(self, "current_port", "") or "").strip().upper()
        if not target_str:
            return False
        match = re.match(r"(COM\d+|/dev/\S+)", target_str)
        target_dev = match.group(1) if match else target_str

        try:
            for p in serial.tools.list_ports.comports():
                dev_str = str(p.device or "").strip().upper()
                if dev_str == target_dev or dev_str == target_str:
                    # 1. Espressif native USB silicon always uses VID 0x303A (12346 decimal)
                    # (e.g. ESP32-S3 USB Serial/JTAG PID 0x1001, USB OTG PID 0x1002, ESP32-S2 PID 0x0002)
                    vid = getattr(p, "vid", None)
                    desc = (str(getattr(p, "description", "") or "") + " " + str(getattr(p, "hwid", "") or "")).lower()
                    if vid == 0x303A or "vid_303a" in desc or "vid:303a" in desc or "vid:pid=303a" in desc:
                        return True

                    # 2. Known external USB-to-UART bridge manufacturers:
                    # 0x1A86: WCH (CH340, CH341, CH342, CH343, CH9102)
                    # 0x10C4: Silicon Labs (CP2101..CP2108)
                    # 0x0403: FTDI
                    # 0x067B: Prolific (PL2303)
                    # 0x2341, 0x2A03: Arduino / Atmel
                    if vid in (0x1A86, 0x10C4, 0x0403, 0x067B, 0x2341, 0x2A03):
                        return False

                    uart_keywords = ("ch340", "ch341", "ch342", "ch343", "ch910", "cp210", "silicon labs", "ftdi", "wch", "prolific", "pl2303", "uart")
                    if any(k in desc for k in uart_keywords):
                        return False

                    native_keywords = ("esp32-s3", "esp32s3", "esp32-s2", "jtag", "usb bridge", "otg", "native", "cdc", "usb debug")
                    if any(k in desc for k in native_keywords):
                        return True
                    return False
        except Exception:
            pass

        return False

    def _wait_for_port_reconnect(self, port: str, timeout: float = 10.0) -> bool:
        """Wait up to timeout seconds for a disconnected COM port to reappear."""
        self.emit("console:log", {"text": "", "newline": True})
        self.emit("console:log", {"text": "  ⚠ MCU disconnected — COM port is no longer available.", "tag": "warning", "newline": True})
        self.emit("console:log", {"text": f"  ⏳ Waiting {int(timeout)} seconds for MCU reconnection… Press ■ STOP to cancel.", "tag": "info", "newline": True})
        poll_interval = 0.5
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(self, "_stop_requested", False):
                break
            time.sleep(poll_interval)
            if self._is_port_present(port):
                self.emit("console:log", {"text": f"  ✔ MCU reconnected on {port} — resuming upload.", "tag": "success", "newline": True})
                return True
        return False

    def _release_port_lines(self, port: str, pulse_reset: bool = True) -> None:
        """Cleanly release serial control lines (DTR/RTS) so MCU returns to normal run mode."""
        if not port:
            return
        try:
            with serial.Serial(port, baudrate=115200, timeout=0.1) as conn:
                conn.dtr = False
                conn.rts = False
                if pulse_reset:
                    time.sleep(0.05)
                    conn.rts = True
                    time.sleep(0.05)
                    conn.rts = False
                    conn.dtr = False
        except Exception:
            pass

    def _record_fast_upload_diagnostic(self, port: str, command: list[str] | None = None,
                                       return_code: int | None = None, output_lines: list[str] | None = None,
                                       error: str = "") -> None:
        """Persist fast-path diagnostic logs."""
        try:
            log_dir = SCRIPT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "fast_upload_fallback.log"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] port={port} return_code={return_code}\n")
                if command:
                    handle.write("command=" + subprocess.list2cmdline([str(x) for x in command]) + "\n")
                if error:
                    handle.write(f"error={error}\n")
                for line in output_lines or []:
                    handle.write(str(line).rstrip() + "\n")
                handle.write("\n")
        except Exception:
            pass

    def _append_fast_upload_metadata(self, fast_bins: dict) -> None:
        """Emit compact upload metadata banner for direct fast upload."""
        try:
            lines = self._fast_upload_metadata_lines(fast_bins)
            for text, tag in lines:
                self.emit("console:log", {"text": text, "tag": tag or "dim", "newline": True})
        except Exception:
            self.emit("console:log", {"text": "  Configuring upload protocol...", "tag": "dim", "newline": True})
            self.emit("console:log", {"text": "  AVAILABLE: esptool", "tag": "dim", "newline": True})
            self.emit("console:log", {"text": "  CURRENT: upload_protocol = esptool", "tag": "dim", "newline": True})

    def _locate_soft_reset_fast_binaries(self, project_dir: Path, board_name: str,
                                         p_platform: str, env_name: str = "mcu_env",
                                         upload_speed: str = "460800",
                                         build_dir: Path | None = None,
                                         skip_mtime_check: bool = False) -> dict | None:
        """Check whether a previously compiled build is still usable for direct esptool flash,
        skipping 'pio run' dependency scans entirely. Returns None if any required binary
        is missing or if sketch source files have been modified since compilation.
        """
        if build_dir is not None:
            build_dir = Path(build_dir)
        elif project_dir == getattr(self, "sketch_dir_path", None):
            cached_fw = self._find_cached_firmware_binary(board_name)
            if cached_fw and cached_fw.is_file():
                build_dir = cached_fw.parent
            else:
                build_dir = self._effective_cache_root(project_dir) / ".pio" / "build" / env_name
        else:
            build_dir = Path(project_dir) / ".pio" / "build" / env_name

        firmware_bin = build_dir / "firmware.bin"
        if not firmware_bin.exists() or firmware_bin.stat().st_size < 1024:
            return None

        # Check sketch source modification times against binary timestamp.
        # Skipped when we know a fresh compile just completed (skip_mtime_check=True),
        # or when source hash matches the cached compiled hash.
        if not skip_mtime_check:
            cached_hash = getattr(self, "_last_source_hash", "")
            curr_hash = ""
            if cached_hash:
                try:
                    curr_hash = self._hash_sources(board_name)
                except Exception:
                    curr_hash = ""
            if not (cached_hash and curr_hash == cached_hash):
                try:
                    bin_mtime = firmware_bin.stat().st_mtime
                    source_candidates = (
                        list(project_dir.glob("src/**/*")) +
                        list(project_dir.glob("*.cpp")) +
                        list(project_dir.glob("*.ino")) +
                        list(project_dir.glob("*.h")) +
                        list(project_dir.glob("*.hpp"))
                    )
                    for sc in source_candidates:
                        if sc.is_file() and sc.stat().st_mtime > bin_mtime:
                            return None  # source was modified after compilation -> recompile needed
                except Exception:
                    pass

        if p_platform == "espressif8266":
            # ESP8266 Arduino core produces a single merged image flashed at 0x0
            return {
                "platform": p_platform,
                "firmware": firmware_bin,
                "bootloader": None,
                "partitions": None,
                "boot_app0": None,
                "bootloader_addr": "0x0",
                "upload_speed": upload_speed,
            }

        if p_platform != "espressif32":
            return None

        bootloader_bin = build_dir / "bootloader.bin"
        partitions_bin = build_dir / "partitions.bin"
        if not (bootloader_bin.exists() and partitions_bin.exists()):
            alt_root = Path(project_dir) / ".pio" / "build" / env_name
            if not bootloader_bin.exists() and (alt_root / "bootloader.bin").exists():
                bootloader_bin = alt_root / "bootloader.bin"
            if not partitions_bin.exists() and (alt_root / "partitions.bin").exists():
                partitions_bin = alt_root / "partitions.bin"
            if not (bootloader_bin.exists() and partitions_bin.exists()):
                return None

        boot_app0_bin = self._locate_esp32_boot_app0()
        if boot_app0_bin is None:
            return None

        _chip_name, bootloader_addr = self._esptool_target(
            board_name, self._resolve_board_info(board_name)
        )

        return {
            "platform": p_platform,
            "firmware": firmware_bin,
            "bootloader": bootloader_bin,
            "partitions": partitions_bin,
            "boot_app0": boot_app0_bin,
            "bootloader_addr": bootloader_addr,
            "upload_speed": upload_speed,
        }

    def _soft_reset_esptool_write(self, fast_bins: dict, port: str,
                                  phase_callback=None,
                                  start_attempt: int = 1) -> tuple[bool, str, int]:
        """Write compiled binaries directly to flash with esptool, skipping PlatformIO scan overhead.
        Each retry executes a fresh esptool process with --connect-attempts 1, providing a real
        DTR/RTS reset pulse for physical BOOT button boards. Returns (ok, err_msg, attempts_used).
        """
        esptool_cmd_base = self._get_esptool_cmd()
        board_name = self.current_board or ""
        board_info = dict(self._resolve_board_info(board_name) or {})
        chip_name, _bootloader_address = self._esptool_target(
            board_name, board_info
        )

        write_cmd = list(esptool_cmd_base)
        if chip_name:
            write_cmd += ["--chip", chip_name]
        write_cmd += [
            "--port", port,
            "--baud", str(fast_bins.get("upload_speed") or "460800"),
            "--before", str(fast_bins.get("before") or "default-reset"),
            "--after", "no-reset",
            "--connect-attempts", "1",
            "write-flash",
            "--flash-mode", "keep",
            "--flash-freq", "keep",
            "--flash-size", "detect",
        ]

        if fast_bins.get("platform") == "espressif32":
            write_cmd += [
                str(fast_bins["bootloader_addr"]), str(fast_bins["bootloader"]),
                "0x8000", str(fast_bins["partitions"]),
                "0xe000", str(fast_bins["boot_app0"]),
                "0x10000", str(fast_bins["firmware"]),
            ]
        else:
            write_cmd += ["0x0", str(fast_bins["firmware"])]

        _WATCHDOG_SECS = 90
        _MAX_CONNECT_RETRIES = 10
        _connect_retry_count = max(0, start_attempt - 1)
        _CONNECT_FAIL_SIGNATURES = (
            "wrong boot mode", "failed to connect",
            "no serial data received", "timed out waiting for packet",
            "device not found", "permissionerror", "access is denied",
            "port is busy", "could not open port", "permission denied",
            "connection timed out", "timed out after", "not responding",
            "no more data to read from the serial port",
            "a device attached to the system is not functioning",
            "write timeout", "serial exception", "serialexception",
            "cannot configure port", "clearcommerror", "setcommstate",
            "getcommstate", "timeout", "serial timeout", "failed to write",
            "fatal error occurred", "fatal error",
        )

        chip_info: dict[str, str] = {}
        chip_info_shown = False
        fast_metadata_shown = False
        connected_bar_flipped = False
        upload_started = time.perf_counter()
        self._last_fast_upload_failure_kind = ""
        connection_poll_count = [max(1, min(_MAX_CONNECT_RETRIES, start_attempt))]
        baud_recovery_used = False
        recovery_baud: str | None = None

        def _set_fast_phase(name: str):
            if callable(phase_callback):
                try:
                    phase_callback(name)
                except Exception:
                    pass

        def _show_fast_chip_info(force: bool = False):
            nonlocal chip_info_shown
            if chip_info_shown:
                return
            model = chip_info.get("Chip Model")
            if not model and not force:
                return
            model = model or board_name
            fields = dict(chip_info)
            if fields.get("Features"):
                try:
                    fields["Features"] = _enrich_chip_features(model, fields["Features"])
                except Exception:
                    pass
            if "Flash Size" not in fields:
                try:
                    configured_flash, _ = normalized_board_memory_options(board_info)
                    if configured_flash:
                        fields["Flash Size"] = f"{configured_flash} (configured, not auto-detected)"
                except Exception:
                    pass
            self._print_chip_info_box(model, list(fields.items()))
            chip_info_shown = True

        def _show_fast_context(force: bool = False):
            nonlocal fast_metadata_shown
            if fast_metadata_shown:
                return
            _show_fast_chip_info(force=force)
            if chip_info_shown:
                self._append_fast_upload_metadata(fast_bins)
                fast_metadata_shown = True

        def _flip_fast_connected_bar():
            nonlocal connected_bar_flipped
            if connected_bar_flipped:
                return
            connected_bar_flipped = True
            self._append_connecting_progress(
                connection_poll_count[0], _MAX_CONNECT_RETRIES, connected=True
            )
            self.emit("console:log", {
                "text": "  ✔ Bootloader synced — you may release the BOOT button now.",
                "tag": "success",
                "replace_pattern": r"💡\s*Hold BOOT now.*",
                "newline": True,
            })

        def _before_fast_progress():
            _flip_fast_connected_bar()
            _show_fast_context(force=True)

        def _terminate_attempt(proc):
            if not proc or proc.poll() is not None:
                return
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        def _handle_fast_line(raw_line: str):
            nonlocal attempt_connected, verified_images
            stripped = raw_line.rstrip()
            if not stripped:
                return
            output_lines.append(stripped)
            low = stripped.lower()

            match = re.search(r"chip (?:is|type)\s*:?\s+(.+)$", stripped, re.IGNORECASE)
            if match:
                chip_info["Chip Model"] = match.group(1).strip()
            match = re.search(r"features\s*:\s*(.+)$", stripped, re.IGNORECASE)
            if match:
                chip_info["Features"] = match.group(1).strip()
            match = re.search(r"crystal (?:is|frequency)\s*:?\s+(.+)$", stripped, re.IGNORECASE)
            if match:
                chip_info["Crystal"] = match.group(1).strip()
            match = re.search(r"^\s*mac\s*:\s*(.+)$", stripped, re.IGNORECASE)
            if match:
                chip_info["MAC Address"] = match.group(1).strip()
            match = re.search(r"(?:auto-detected\s+)?flash size\s*:\s*(.+)$", stripped, re.IGNORECASE)
            if match:
                chip_info["Flash Size"] = match.group(1).strip()

            if "connecting" in low:
                _set_fast_phase("Connecting")
                self._append_connecting_progress(
                    connection_poll_count[0], _MAX_CONNECT_RETRIES
                )
            if "connected to" in low or "uploading stub" in low or "stub flasher running" in low:
                attempt_connected = True
                _set_fast_phase("Connecting")
                _flip_fast_connected_bar()
            if "will be erased" in low or "erasing flash" in low:
                attempt_connected = True
                _set_fast_phase("Erasing")

            wrote_event = _parse_esptool_wrote(stripped)
            if wrote_event:
                if wrote_event.get("address"):
                    self._select_upload_stage_for_address(upload_progress_state, wrote_event["address"])
                stages = upload_progress_state.get("stages") or []
                idx = max(0, min(
                    int(upload_progress_state.get("active_index", 0)),
                    max(0, len(stages) - 1),
                ))
                if stages:
                    completed_images.add(str(stages[idx].get("key") or idx))
                attempt_connected = True

            if "hash of data verified" in low:
                verified_images += 1
                attempt_connected = True

            if self._consume_esptool_upload_progress(
                    upload_progress_state, stripped,
                    before_progress=_before_fast_progress,
                    phase_callback=_set_fast_phase):
                return
            if "verifying written data" in low or "hash of data verified" in low:
                _set_fast_phase("Verifying")
            if "hard resetting" in low:
                _set_fast_phase("Resetting")

        self._append_connecting_progress(start_attempt, _MAX_CONNECT_RETRIES)

        while True:
            output_lines = []
            upload_progress_state = self._new_upload_progress_state(fast_bins)
            attempt_connected = False
            completed_images: set[str] = set()
            verified_images = 0
            expected_image_count = len(upload_progress_state.get("stages") or [])
            timed_out = False
            verification_complete_seen = False

            try:
                if _connect_retry_count == max(0, start_attempt - 1):
                    time.sleep(0.75)

                attempt_cmd = list(write_cmd)
                if recovery_baud:
                    try:
                        baud_idx = attempt_cmd.index("--baud") + 1
                        attempt_cmd[baud_idx] = recovery_baud
                    except (ValueError, IndexError):
                        pass

                creationflags = (subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
                proc = subprocess.Popen(
                    attempt_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                    creationflags=creationflags,
                )
                self._active_process = proc
                _start = time.time()
                output_queue: queue.Queue = queue.Queue()

                def _read_fast_output():
                    try:
                        if proc.stdout is not None:
                            for raw_line in iter(proc.stdout.readline, ""):
                                output_queue.put(raw_line)
                    finally:
                        output_queue.put(None)

                threading.Thread(
                    target=_read_fast_output, name="MCU_FastUpload_Reader", daemon=True
                ).start()
                reader_done = False
                _last_output_time = time.time()  # tracks last received line for silence watchdog
                # Silence watchdog: if esptool is connected but goes quiet mid-write,
                # kill it after this many seconds.  Separate from _WATCHDOG_SECS which
                # only covers the pre-connection phase.
                _SILENCE_WATCHDOG_SECS = 30
                while not reader_done:
                    try:
                        line = output_queue.get(timeout=0.10)
                    except queue.Empty:
                        now = time.time()
                        elapsed = now - _start
                        silent_for = now - _last_output_time
                        # Pre-connection watchdog: no bootloader response within _WATCHDOG_SECS
                        if (elapsed > _WATCHDOG_SECS
                                and not attempt_connected
                                and not completed_images):
                            timed_out = True
                            self.emit("console:log", {
                                "text": f"  ⚠ Bootloader connection timed out after {_WATCHDOG_SECS}s — retrying.",
                                "tag": "warning",
                                "newline": True,
                            })
                            _terminate_attempt(proc)
                            break
                        # Post-connection silence watchdog: esptool stopped emitting mid-write
                        if (attempt_connected
                                and silent_for > _SILENCE_WATCHDOG_SECS
                                and not completed_images):
                            self.emit("console:log", {
                                "text": f"  ⚠ Uploader silent for {_SILENCE_WATCHDOG_SECS}s during write — aborting attempt.",
                                "tag": "warning",
                                "newline": True,
                            })
                            timed_out = True
                            _terminate_attempt(proc)
                            break
                        continue

                    _last_output_time = time.time()
                    if line is None:
                        reader_done = True
                        continue

                    _handle_fast_line(line)
                    if (
                        expected_image_count > 0
                        and len(completed_images) >= expected_image_count
                        and verified_images >= expected_image_count
                    ):
                        verification_complete_seen = True
                        break


                verified_before_wait = (
                    verification_complete_seen
                    or (
                        expected_image_count > 0
                        and len(completed_images) >= expected_image_count
                        and verified_images >= expected_image_count
                    )
                )
                try:
                    proc.wait(timeout=1.5 if verified_before_wait else (3 if timed_out else 10))
                except subprocess.TimeoutExpired:
                    if verified_before_wait:
                        self.emit("console:log", {
                            "text": "  ℹ Flash verified; finalizing the uploader process.",
                            "tag": "dim",
                            "newline": True,
                        })
                    else:
                        self.emit("console:log", {
                            "text": "  ⚠ Process did not exit cleanly — force killing.",
                            "tag": "warning",
                            "newline": True,
                        })
                    _terminate_attempt(proc)
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass

                rc = proc.returncode
                joined = " ".join(l.rstrip().lower() for l in output_lines)
                all_images_written = (
                    expected_image_count > 0
                    and len(completed_images) >= expected_image_count
                )
                all_images_verified = (
                    all_images_written
                    and verified_images >= expected_image_count
                )

                cli_syntax_error = any(
                    err_token in joined for err_token in (
                        "unrecognized arguments", "invalid choice",
                        "usage: esptool", "no such option",
                    )
                )

                is_conn_failure = (
                    rc != 0
                    and not attempt_connected
                    and not completed_images
                    and not cli_syntax_error
                )

                if is_conn_failure:
                    if (_connect_retry_count < _MAX_CONNECT_RETRIES - 1
                            and not getattr(self, "_stop_requested", False)):
                        _connect_retry_count += 1
                        connection_poll_count[0] = _connect_retry_count + 1
                        self._append_connecting_progress(
                            connection_poll_count[0], _MAX_CONNECT_RETRIES
                        )
                        port_reenumerating = any(
                            token in joined for token in (
                                "no more data to read from the serial port",
                                "a device attached to the system is not functioning",
                                "could not open port", "port is busy",
                                "cannot configure port", "write timeout",
                                "permissionerror", "serial exception",
                            )
                        )
                        if port_reenumerating and not self._is_port_present(port):
                            if not self._wait_for_port_reconnect(port):
                                error_message = (
                                    f"MCU disconnected during upload ({port} is no longer available)"
                                )
                                self._last_fast_upload_failure_kind = "connection"
                                self._record_fast_upload_diagnostic(
                                    port, attempt_cmd, return_code=rc,
                                    output_lines=output_lines, error=error_message,
                                )
                                return False, error_message, _connect_retry_count + 1
                        time.sleep(0.75 if port_reenumerating else 0.4)
                        continue
                    connection_poll_count[0] = _MAX_CONNECT_RETRIES

                post_connect_transport_failure = (
                    rc != 0
                    and attempt_connected
                    and not all_images_verified
                )
                if (rc != 0 and attempt_connected
                        and not baud_recovery_used
                        and str(fast_bins.get("upload_speed") or "460800") != "115200"
                        and post_connect_transport_failure
                        and not getattr(self, "_stop_requested", False)):
                    if not self._is_port_present(port):
                        if not self._wait_for_port_reconnect(port):
                            error_message = (
                                f"MCU disconnected during upload ({port} is no longer available)"
                            )
                            self._last_fast_upload_failure_kind = "flash"
                            self._record_fast_upload_diagnostic(
                                port, attempt_cmd, return_code=rc,
                                output_lines=output_lines, error=error_message,
                            )
                            return False, error_message, _connect_retry_count + 1
                    baud_recovery_used = True
                    recovery_baud = "115200"
                    self.emit("console:log", {
                        "text": "  ⚠ Serial data stopped during high-speed flash; retrying once at 115200 baud…",
                        "tag": "warning",
                        "newline": True,
                    })
                    time.sleep(0.35)
                    continue

                ok = (rc == 0)
                if ok or all_images_verified:
                    _flip_fast_connected_bar()
                    _show_fast_context(force=True)
                    _set_fast_phase("Done")
                    self.emit("console:log", {"text": "", "newline": True})
                    if rc != 0:
                        self.emit("console:log", {
                            "text": "  ⚠ Esptool returned an error after every image was written and hash-verified. Treating upload as successful.",
                            "tag": "warning",
                            "newline": True,
                        })
                        self._record_fast_upload_diagnostic(
                            port, attempt_cmd, return_code=rc,
                            output_lines=output_lines, error="post-flash exit after all images verified",
                        )
                    self.emit("console:log", {
                        "text": "  ✔ Flash write and verification completed. Reset will continue through the Serial Monitor.",
                        "tag": "success",
                        "newline": True,
                    })
                    elapsed = max(0.0, time.perf_counter() - upload_started)
                    self.emit("console:log", {
                        "text": f"  {'=' * 25} [SUCCESS] Took {elapsed:.2f} seconds {'=' * 25}",
                        "tag": "success",
                        "newline": True,
                    })
                    self._last_fast_upload_failure_kind = ""
                    return True, "", _connect_retry_count + 1

                detail = ""
                # First pass: find explicit ERROR: / fatal error / Exception lines
                for line in output_lines:
                    s = line.strip()
                    low = s.lower()
                    if low.startswith("error:") or "fatal error" in low or "exception" in low:
                        clean = re.sub(r"^(?:ERROR:\s*)?(?:A fatal error occurred:\s*)?", "", s, flags=re.IGNORECASE).strip()
                        if clean and not clean.lower().startswith("note:"):
                            detail = clean
                            break
                if not detail:
                    # Second pass: reverse search skipping non-informative URLs/headers
                    for line in reversed(output_lines):
                        s = line.strip()
                        low = s.lower()
                        if not s:
                            continue
                        if (low.startswith("hint:")
                                or low.startswith("note:")
                                or "troubleshooting" in low
                                or low.startswith("for troubleshooting steps")
                                or low.startswith("connecting")
                                or low.startswith("serial port")
                                or low.startswith("esptool v")
                                or low.startswith("chip is")
                                or low.startswith("features:")
                                or low.startswith("crystal is")
                                or low.startswith("mac:")
                                or low.startswith("http://")
                                or low.startswith("https://")):
                            continue
                        detail = s
                        break
                if not detail:
                    detail = "esptool exited with a non-zero status"
                detail = detail[:300]
                if timed_out:
                    error_message = f"esptool connection timed out after {_WATCHDOG_SECS}s"
                else:
                    error_message = f"esptool exit code {rc}: {detail}"

                if is_conn_failure:
                    self._last_fast_upload_failure_kind = "connection"
                elif attempt_connected or completed_images:
                    self._last_fast_upload_failure_kind = "flash"
                elif not cli_syntax_error:
                    self._last_fast_upload_failure_kind = "connection"
                else:
                    self._last_fast_upload_failure_kind = "tool"

                self._record_fast_upload_diagnostic(
                    port, attempt_cmd, return_code=rc,
                    output_lines=output_lines, error=error_message,
                )
                return False, error_message, _connect_retry_count + 1
            except Exception as e:
                self._record_fast_upload_diagnostic(
                    port, attempt_cmd, error=str(e),
                )
                return False, str(e), _connect_retry_count + 1

    def _validate_entry_points(self) -> tuple[bool, str]:
        """Validate that setup() and loop() are defined in the sketch files."""
        if not self.sketch_dir_path:
            return False, ""

        ino_files = sorted(list(self.sketch_dir_path.glob("*.ino")))
        if not ino_files:
            return True, ""

        SETUP_RE = re.compile(r'\bvoid\s+setup\s*\(\s*(?:void)?\s*\)', re.MULTILINE)
        LOOP_RE = re.compile(r'\bvoid\s+loop\s*\(\s*(?:void)?\s*\)', re.MULTILINE)

        setup_owners = []
        loop_owners = []
        for f in ino_files:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                if SETUP_RE.search(content):
                    setup_owners.append(f.name)
                if LOOP_RE.search(content):
                    loop_owners.append(f.name)
            except Exception:
                pass

        if not setup_owners or not loop_owners:
            return False, ""

        owners = sorted(list(set(setup_owners) | set(loop_owners)))
        return True, ", ".join(owners)

    def _generate_platformio_ini(self, cache_root: Path):
        """Generate platformio.ini dynamically for the active board in cache."""
        binfo = self._resolve_board_info(self.current_board)
        platform = binfo.get("platform", "espressif32")
        board = binfo.get("board", "esp32dev")
        framework = binfo.get("framework", "arduino")
        speed_val = getattr(self, "upload_speed", "") or ("115200" if platform == "atmelavr" else str(DEFAULT_UPLOAD_SPEED))
        try:
            speed_val = str(min(int(speed_val), MAX_BAUD_RATE))
        except Exception:
            pass
        upload_speed = "115200" if platform == "atmelavr" else str(speed_val)

        extra_dirs = []
        app_lib_dir = Path(os.path.expanduser("~")) / "Documents" / "_MCUFlasherByNaph_src" / "Libs"
        if app_lib_dir.is_dir():
            extra_dirs.append(app_lib_dir.as_posix())
        arduino_libs = Path(os.path.expanduser("~")) / "Documents" / "Arduino" / "libraries"
        if arduino_libs.is_dir():
            extra_dirs.append(arduino_libs.as_posix())
        local_libs = _project_root / "Libs"
        if local_libs.is_dir():
            extra_dirs.append(local_libs.as_posix())
        lib_extra = f"lib_extra_dirs = {', '.join(extra_dirs)}\n" if extra_dirs else ""

        # Check sketch includes for matching libraries in extra_dirs
        headers = self._scan_includes_for_libs()
        detected_symlinks = []
        seen_lib_names = set()
        matched_headers = set()
        for ed in extra_dirs:
            p_ed = Path(ed)
            if not p_ed.is_dir():
                continue
            for item in p_ed.iterdir():
                if not item.is_dir():
                    continue
                # Normalize folder name to avoid duplicate versions (e.g. "ESP32Servo-3.2.1" and "ESP32Servo")
                norm_name = re.sub(r"[-_]v?\d+.*$", "", item.name).strip().lower()
                if norm_name in seen_lib_names:
                    continue
                for h in headers:
                    if h in matched_headers:
                        continue
                    if (item / h).is_file() or (item / "src" / h).is_file():
                        seen_lib_names.add(norm_name)
                        matched_headers.add(h)
                        detected_symlinks.append(f"symlink://{item.as_posix()}")
                        break

        detected_symlinks.sort()
        lib_deps_str = ""
        if detected_symlinks:
            lib_deps_str = "lib_deps =\n" + "\n".join(f"    {s}" for s in detected_symlinks) + "\n"

        safe_monitor_baud = min(self.current_baud, MAX_BAUD_RATE)
        ini_content = (
            "; PlatformIO Project Configuration File\n"
            "; Generated automatically by MCU Flasher by Naph\n\n"
            "[platformio]\n"
            "default_envs = mcu_env\n\n"
            "[env:mcu_env]\n"
            f"platform = {platform}\n"
            f"board = {board}\n"
            f"framework = {framework}\n"
            f"monitor_speed = {safe_monitor_baud}\n"
            f"upload_speed = {upload_speed}\n"
            f"{lib_extra}"
            f"{lib_deps_str}"
        )
        ini_file = cache_root / "platformio.ini"
        old_content = ""
        if ini_file.is_file():
            try:
                old_content = ini_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                old_content = ""

        # Normalize line endings and whitespace to compare content accurately.
        # Ignore upload_speed and monitor_speed line differences so that changing
        # baud rates or upload speeds in the UI toolbar does NOT touch platformio.ini's
        # mtime and does NOT invalidate PlatformIO/SCons compiled C++ objects.
        def _norm_ini(text: str) -> str:
            lines = []
            for line in text.replace("\r\n", "\n").strip().splitlines():
                stripped = line.strip()
                if stripped.startswith("upload_speed") or stripped.startswith("monitor_speed"):
                    continue
                lines.append(line.rstrip())
            return "\n".join(lines)

        # If platformio.ini already exists and has identical contents, DO NOT rewrite it!
        # Touching platformio.ini updates its modification timestamp (mtime), which causes
        # PlatformIO/SCons to invalidate all pre-compiled library and framework objects
        # (.o and .a files) and forces a full re-compilation of all 40+ dependency files.
        if old_content and _norm_ini(old_content) == _norm_ini(ini_content):
            return

        old_symlinks = set(re.findall(r"symlink://[^\r\n]+", old_content))
        new_symlinks = set(detected_symlinks)
        added_symlinks = new_symlinks - old_symlinks
        if added_symlinks:
            self.emit("console:log", {
                "text": f"  📝 Adding dependencies: {', '.join(sorted(added_symlinks))}",
                "tag": "warning",
                "newline": True
            })
            self.emit("console:log", {
                "text": "  Rebuilding lib_deps in platformio.ini...",
                "tag": "info",
                "newline": True
            })
        elif not old_content and detected_symlinks:
            self.emit("console:log", {
                "text": f"  📝 Adding dependencies: {', '.join(detected_symlinks)}",
                "tag": "warning",
                "newline": True
            })

        ensure_file_writable(ini_file)
        ini_file.write_text(ini_content, encoding="utf-8")

        if ("upload_protocol = esptool" in ini_content and "upload_protocol = esptool" not in old_content) or (
            old_content and f"platform = {platform}" not in old_content
        ):
            self.emit("console:log", {
                "text": "  📝 Serial upload protocol updated; PlatformIO will reconcile the selected board incrementally.",
                "tag": "info",
                "newline": True
            })
        self.emit("console:log", {
            "text": "  ✔ Updated platformio.ini successfully.",
            "tag": "success",
            "newline": True
        })

    # ──────────────────────────────────────────────────────────
    # JS-RPC: UPLOAD PIPELINE
    # ──────────────────────────────────────────────────────────
    def upload_sketch(self):
        """Compile and upload firmware to the target microcontroller."""
        if self.is_busy:
            return

        if self._block_if_pending_ai_edits("Upload"):
            return

        cfg = load_gui_config()
        if getattr(self, "clear_console_on_action", cfg.get("clear_console_on_action", True)):
            self.emit("console:clear", None)
        if getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)):
            self.emit("serial:clear", None)

        if not self.current_board:
            self._load_compile_cache()
            if self.current_board:
                self.emit("board:selected", {"board_name": self.current_board})
        if not self.current_board:
            self.emit("console:log", {"text": "✖ Upload error: No board selected. Please select a board first.", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Upload Failed", "message": "No board selected.", "type": "warning"})
            return
        if not self.current_port:
            self.emit("console:log", {"text": "✖ Upload error: No COM port selected. Please select a port first.", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Upload Failed", "message": "No port selected.", "type": "warning"})
            return

        selected_board = self.current_board
        selected_board_info = dict(self._resolve_board_info(selected_board) or {})
        self._active_board_name = selected_board
        self._active_board_info = selected_board_info
        self._active_upload_speed = str(getattr(self, "upload_speed", None) or cfg.get("upload_speed", DEFAULT_UPLOAD_SPEED))
        self._active_skip_compile = bool(getattr(self, "skip_compile", False))
        self._active_clear_serial_on_upload = bool(getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)))
        self._active_monitor_baud = str(getattr(self, "serial_baud", None) or cfg.get("serial_baud", 115200))
        self._active_port_label = str(self.current_port or "")
        self._active_reset_kind = None
        self._mcu_detached_during_compile = None
        self._op_session_id = getattr(self, "_op_session_id", 0) + 1
        self._stop_requested = False

        # Synchronously determine whether upload can skip compilation
        can_skip = self.check_can_skip_compile_for_upload(selected_board)

        self.is_busy = True
        if can_skip:
            self.active_operation = "flash"
            self._current_op_phase = "flashing"
            self.emit("operation:phase", {"phase": "flash", "is_busy": True, "can_stop": False, "op": "upload"})
            self.emit("window:closable", {"closable": False})
        else:
            self.active_operation = "upload"
            self._current_op_phase = "compiling"
            self.emit("operation:phase", {"phase": "compile", "is_busy": True, "can_stop": True, "op": "upload"})
            self.emit("window:closable", {"closable": True})

        threading.Thread(
            target=self._upload_worker, args=(can_skip,), name="MCU_Upload", daemon=True
        ).start()

    def _get_jobs(self) -> int:
        configured_jobs = load_gui_config().get("compiler_jobs")
        if configured_jobs is not None and str(configured_jobs).isdigit() and int(configured_jobs) > 0:
            return int(configured_jobs)
        return get_optimal_compiler_jobs()

    _board_cache_key_memo: dict[str, str] = {}

    def _board_cache_key(self, board_name: str | None = None) -> str:
        name = board_name or self.current_board or "unknown_board"
        if name in self._board_cache_key_memo:
            return self._board_cache_key_memo[name]
        binfo = self._resolve_board_info(name)
        identity = {
            "display_name": name,
            "platform": str(binfo.get("platform", "")),
            "board": str(binfo.get("board", "")),
            "framework": str(binfo.get("framework", "")),
            "definition": binfo,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8", errors="replace")
        digest = hashlib.sha256(encoded).hexdigest()[:10]
        readable = "_".join(
            part for part in (
                str(binfo.get("platform", "")),
                str(binfo.get("board", "")),
            ) if part
        ) or str(name or "unknown_board")
        safe_prefix = re.sub(r"[^\w\-.]+", "_", readable).strip("_")
        key = f"{safe_prefix}_{digest}"
        self._board_cache_key_memo[name] = key
        return key

    def _esptool_target(self, board_name: str | None = None,
                        board_info: dict | None = None) -> tuple[str | None, str]:
        """Resolve esptool chip name and bootloader offset from canonical ids."""
        name = board_name or self.current_board or ""
        info = dict(board_info or self._resolve_board_info(name))
        platform = str(info.get("platform", "")).lower()
        identity = " ".join((
            str(info.get("mcu", "")),
            str(info.get("board", "")),
            str(name or ""),
        )).lower().replace("-", "").replace("_", "")
        variants = (
            ("esp32p4", "esp32p4", "0x0"),
            ("esp32c6", "esp32c6", "0x0"),
            ("esp32c5", "esp32c5", "0x0"),
            ("esp32c3", "esp32c3", "0x0"),
            ("esp32c2", "esp32c2", "0x0"),
            ("esp32h2", "esp32h2", "0x0"),
            ("esp32s3", "esp32s3", "0x0"),
            ("esp32s2", "esp32s2", "0x1000"),
        )
        for marker, chip, address in variants:
            if marker in identity:
                return chip, address
        if platform == "espressif8266" or "esp8266" in identity:
            return "esp8266", "0x0"
        if platform == "espressif32" or "esp32" in identity:
            return "esp32", "0x1000"
        return None, "0x0"

    def _get_esptool_cmd(self) -> list[str]:
        """Dynamically resolve the most reliable esptool command across Windows environments."""
        try:
            from importlib import util as importlib_util
            if importlib_util.find_spec("esptool") is not None:
                return [sys.executable, "-m", "esptool"]
        except Exception:
            pass

        if sys.platform == "win32":
            local_exe = SCRIPT_DIR / "env" / "Scripts" / "esptool.exe"
            if local_exe.exists():
                return [str(local_exe)]
            pio_exe = Path.home() / ".platformio" / "penv" / "Scripts" / "esptool.exe"
            if pio_exe.exists():
                return [str(pio_exe)]

        w = shutil.which("esptool")
        if w:
            return [w]

        pio_home = Path.home() / ".platformio"
        for candidate in (pio_home / "packages").glob("tool-esptoolpy*"):
            for s_name in ("esptool.py", "esptool.exe"):
                sp = candidate / s_name
                if sp.exists():
                    return [str(sp)] if sp.suffix == ".exe" else [sys.executable, str(sp)]

        return [sys.executable, "-m", "esptool"]

    def _locate_esp32_boot_app0(self) -> Path | None:
        """Find boot_app0.bin inside PlatformIO's installed Arduino-ESP32 framework package.

        Searches, in order:
          1. core_dir/packages/framework-arduinoespressif32*/
          2. core_dir/.cache/tmp/pkg-installing-*/  (package staging area — framework
             may not be finalised into packages/ yet on a fresh install)
          3. ~/.platformio/packages/framework-arduinoespressif32*/
        """
        core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)

        def _search_framework_dir(framework_dir: Path) -> Path | None:
            """Return boot_app0.bin from a known framework root, or None."""
            for candidate in (
                framework_dir / "tools" / "partitions" / "boot_app0.bin",
                framework_dir / "partitions" / "boot_app0.bin",
            ):
                if candidate.exists():
                    return candidate
            hits = [p for p in framework_dir.rglob("boot_app0.bin") if "variants" not in p.parts]
            return sorted(hits)[0] if hits else None

        # 1. Stable install location: core_dir/packages/
        pio_packages = core_dir / "packages"
        if not pio_packages.exists():
            local_pio = SCRIPT_DIR / "env" / ".platformio"
            pio_packages = (local_pio / "packages") if local_pio.exists() else Path.home() / ".platformio" / "packages"

        try:
            for candidate in sorted(pio_packages.glob("framework-arduinoespressif32*"), reverse=True):
                if candidate.is_dir():
                    result = _search_framework_dir(candidate)
                    if result:
                        return result
        except Exception:
            pass

        # 2. Staging area: .cache/tmp/pkg-installing-*/ — framework may still be
        #    here on a fresh install before PlatformIO finalises the extraction.
        try:
            cache_tmp = core_dir / ".cache" / "tmp"
            if cache_tmp.is_dir():
                for staging in sorted(cache_tmp.glob("pkg-installing-*"), reverse=True):
                    if staging.is_dir():
                        result = _search_framework_dir(staging)
                        if result:
                            return result
        except Exception:
            pass

        # 3. Fallback: user-level ~/.platformio
        try:
            user_pkgs = Path.home() / ".platformio" / "packages"
            for candidate in sorted(user_pkgs.glob("framework-arduinoespressif32*"), reverse=True):
                if candidate.is_dir():
                    result = _search_framework_dir(candidate)
                    if result:
                        return result
        except Exception:
            pass

        return None

    def _soft_reset_project_dir(self, board_name: str | None = None, board_info: dict | None = None) -> Path:
        name = board_name or self.current_board
        binfo = dict(board_info or self._resolve_board_info(name))
        capabilities = board_reset_capabilities(
            binfo.get("platform", ""),
            binfo.get("board", ""),
            name,
            binfo.get("framework", ""),
        )
        base = "soft_reset_project_uno" if capabilities.get("family") == "atmelavr" else "soft_reset_project"
        return SCRIPT_DIR / "soft_reset" / base / "boards" / self._board_cache_key(name)

    def _reset_project_contents(self, board_name: str, board_info: dict) -> tuple[str, str, str]:
        info = dict(board_info or self._resolve_board_info(board_name))
        platform = str(info.get("platform", "atmelavr"))
        board_id = str(info.get("board", "uno"))
        framework = str(info.get("framework", "arduino"))
        reset_capabilities = board_reset_capabilities(
            platform, board_id, board_name, framework
        )
        reset_family = str(reset_capabilities.get("family") or platform).lower()
        is_avr = reset_family == "atmelavr"
        is_esp32 = reset_family == "espressif32"
        is_esp8266 = (
            reset_family == "espressif8266"
            or "esp8266" in board_name.lower()
            or "nodemcu" in board_name.lower()
            or "node" in board_name.lower()
        )
        is_s3 = is_s3_board(board_id)
        is_native = bool(is_s3 and self._is_native_usb_port(getattr(self, "current_port", None)))
        flash_size, has_psram = normalized_board_memory_options(info)
        memory_type = normalized_board_memory_type(info)
        flash_mode = normalized_board_flash_mode(info)
        if is_esp8266:
            monitor_speed = "115200"
        else:
            monitor_speed = default_monitor_baud(platform, board_id, board_name)
        upload_speed = "115200" if is_avr or is_esp8266 else "460800"

        env_lines = [
            f"platform = {platform}",
            f"board = {board_id}",
            f"framework = {framework}",
            f"monitor_speed = {monitor_speed}",
        ]
        if not is_native:
            env_lines.append(f"upload_speed = {upload_speed}")
        if is_esp32 or is_esp8266:
            env_lines.append("upload_protocol = esptool")
        if flash_mode:
            env_lines.append(f"board_build.flash_mode = {flash_mode}")
        if flash_size:
            env_lines.extend((
                f"board_build.flash_size = {flash_size}",
                f"board_upload.flash_size = {flash_size}",
            ))
        if memory_type:
            env_lines.append(f"board_build.arduino.memory_type = {memory_type}")

        build_flags: list[str] = []
        if has_psram:
            build_flags.append("-D BOARD_HAS_PSRAM")
        if is_native:
            build_flags.extend((
                "-DARDUINO_USB_MODE=1",
                "-DARDUINO_USB_CDC_ON_BOOT=1",
            ))
        if build_flags:
            env_lines.append(
                "build_flags =\n" + "\n".join(f"    {flag}" for flag in build_flags)
            )

        ini_content = (
            "; PlatformIO Project Configuration File for MCU Flasher Reset\n"
            "[platformio]\n"
            "src_dir = .\n"
            "default_envs = mcu_flash\n\n"
            "[env:mcu_flash]\n"
            + "\n".join(env_lines)
            + "\n"
        )
        cpp_content = (
            "#include <Arduino.h>\n"
            "void setup() {\n"
            f"  Serial.begin({monitor_speed});\n"
            "  Serial.println(\">>> ----- <<<\");\n"
            "}\n"
            "void loop() {\n"
            "}\n"
        )
        return ini_content, cpp_content, monitor_speed

    @staticmethod
    def _reset_template_digest(ini_content: str, cpp_content: str) -> str:
        payload = ini_content.encode("utf-8") + b"\0" + cpp_content.encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _write_reset_manifest(self, project_dir: Path, board_name: str, board_info: dict) -> tuple[bool, str]:
        ini_content, cpp_content, _ = self._reset_project_contents(board_name, board_info)
        build_dir = Path(project_dir) / ".pio" / "build" / "mcu_flash"
        hashes: dict[str, str] = {}
        platform = str(board_info.get("platform", ""))
        image_names = ("firmware.bin",) if platform == "espressif8266" else ("bootloader.bin", "partitions.bin", "firmware.bin")
        for filename in image_names:
            image_path = build_dir / filename
            if not image_path.is_file():
                return False, f"Reset build is missing {filename}."
            try:
                hashes[filename] = hashlib.sha256(image_path.read_bytes()).hexdigest()
            except OSError as exc:
                return False, f"Could not hash {filename}: {exc}"

        manifest = {
            "schema": 1,
            "board_key": self._board_cache_key(board_name),
            "board_name": board_name,
            "platform": platform,
            "board": str(board_info.get("board", "")),
            "template_sha256": self._reset_template_digest(ini_content, cpp_content),
            "sha256": hashes,
            "partition_scheme": "board-specific reset project partition table",
            "source_label": "MCU Flasher exact-board reset build",
        }
        manifest_path = Path(project_dir) / "hard_reset_manifest.json"
        try:
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            hide_hidden_attribute(manifest_path)
            return True, ""
        except Exception as exc:
            return False, f"Could not write reset manifest: {exc}"

    def _locate_hard_reset_recovery_images(self, board_name=None, board_info=None) -> tuple[dict | None, str]:
        name = board_name or self.current_board
        binfo = dict(board_info or self._resolve_board_info(name))
        selected_platform = str(binfo.get("platform", "")).strip().lower()
        selected_board_id = str(binfo.get("board", "")).strip().lower()
        if selected_platform != "espressif32":
            return None, "Dedicated recovery images are only used for ESP32-family boards."
        if not selected_board_id:
            return None, "The selected board has no PlatformIO board identifier."

        project_dir = self._soft_reset_project_dir(name, binfo)
        build_dir = project_dir / ".pio" / "build" / "mcu_flash"
        manifest_path = project_dir / "hard_reset_manifest.json"
        manifest = {}
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except Exception:
                manifest = {}

        desired_ini, desired_cpp, _monitor_speed = self._reset_project_contents(name, binfo)
        desired_template_hash = self._reset_template_digest(desired_ini, desired_cpp)
        if not manifest:
            return None, "The reset cache needs one incremental validation build."
        if manifest.get("board_key") != self._board_cache_key(name):
            return None, "The reset cache belongs to a different selectable board."
        if manifest.get("template_sha256") != desired_template_hash:
            return None, "The reset project configuration changed and must be rebuilt."

        bootloader = build_dir / "bootloader.bin"
        partitions = build_dir / "partitions.bin"
        firmware = build_dir / "firmware.bin"
        missing = [p.name for p in (bootloader, partitions) if not p.is_file()]
        if missing:
            return None, f"Dedicated reset recovery cache is incomplete: missing {', '.join(missing)}."

        expected_hashes = manifest.get("sha256", {})
        if not isinstance(expected_hashes, dict):
            return None, "The reset cache has no valid recovery-image hashes."
        for key, path in (("bootloader.bin", bootloader), ("partitions.bin", partitions)):
            expected = str(expected_hashes.get(key, "")).strip().lower()
            if not expected:
                return None, f"The reset cache has no validated hash for {path.name}."
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
            except Exception as exc:
                return None, f"Could not read recovery image {path.name}: {exc}"
            if actual != expected:
                return None, f"Recovery image integrity check failed for {path.name}."

        return {
            "project_dir": project_dir,
            "build_dir": build_dir,
            "bootloader": bootloader,
            "partitions": partitions,
            "firmware": firmware if firmware.is_file() else None,
            "board_id": selected_board_id,
            "platform": selected_platform,
            "partition_scheme": str(manifest.get("partition_scheme") or "default OTA partition table"),
            "source_label": str(manifest.get("source_label") or "Soft Reset recovery build"),
        }, ""

    def _build_hard_reset_recovery_images(self, board_name: str, board_info: dict) -> tuple[dict | None, str]:
        """Build the exact board's reset bundle on first Hard Reset use."""
        project_dir = self._soft_reset_project_dir(board_name, board_info)
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
            hide_generated_directory(project_dir.parent.parent)
            hide_generated_directory(project_dir.parent)
            hide_generated_directory(project_dir)
        except Exception as exc:
            return None, f"Could not create the reset cache folder: {exc}"

        ini_content, cpp_content, _monitor_speed = self._reset_project_contents(board_name, board_info)
        try:
            ini_path = project_dir / "platformio.ini"
            cpp_path = project_dir / "main.cpp"
            ini_path.write_text(ini_content, encoding="utf-8")
            cpp_path.write_text(cpp_content, encoding="utf-8")
        except Exception as exc:
            return None, f"Could not prepare reset project files: {exc}"

        pio_path = find_pio_executable() or ensure_platformio()
        if not pio_path:
            return None, "PlatformIO is unavailable."
        jobs = self._get_jobs()
        cmd = pio_path + ["run", "-e", "mcu_flash", "-j", str(jobs)]
        self.emit("console:log", {
            "text": "  🔧 First Hard/Soft Reset for this board — building its persistent recovery cache…",
            "tag": "warning",
            "newline": True,
        })
        output_lines: list[str] = []
        try:
            launch_env = os.environ.copy()
            core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)
            launch_env["PLATFORMIO_CORE_DIR"] = str(core_dir)
            launch_env["TMP"] = str(core_dir / ".tmp")
            launch_env["TEMP"] = str(core_dir / ".tmp")
            launch_env["PYTHONUNBUFFERED"] = "1"
            launch_env.pop("PYTHONOPTIMIZE", None)
            launch_env["PLATFORMIO_BUILD_JOBS"] = str(jobs)
            launch_env["PLATFORMIO_RUN_JOBS"] = str(jobs)
            launch_env["SCONSFLAGS"] = f"-j{jobs}"

            proc = subprocess.Popen(
                cmd,
                cwd=str(project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    (subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
                ),
                env=launch_env,
            )
            self._active_process = proc
            if proc.stdout:
                for raw in iter(proc.stdout.readline, ""):
                    line = raw.rstrip()
                    if not line:
                        continue
                    output_lines.append(line)
                    low = line.lower()
                    if any(token in low for token in (
                        "compiling", "linking", "building", "creating", "created", "error", "warning", "success", "took"
                    )):
                        tag = "error" if "error" in low else ("warning" if "warning" in low else "info")
                        self.emit("console:log", {"text": f"  {line}", "tag": tag, "newline": True})
            proc.wait()
            self._active_process = None
            if proc.returncode != 0:
                tail = next(
                    (l for l in reversed(output_lines) if "error" in l.lower()),
                    f"PlatformIO exited with code {proc.returncode}"
                )
                return None, f"Reset cache build failed: {tail}"
        except Exception as exc:
            self._active_process = None
            return None, f"Reset cache build could not start: {exc}"

        manifest_ok, manifest_error = self._write_reset_manifest(project_dir, board_name, board_info)
        if not manifest_ok:
            return None, manifest_error

        images, error = self._locate_hard_reset_recovery_images(board_name, board_info)
        if images:
            self.emit("console:log", {"text": "  ✔ Exact-board reset cache built and saved for future use.", "tag": "success", "newline": True})
        return images, error

    def _emit_boot_connection_progress(self, step: int = 0, *, connected: bool = False, failed: bool = False, cancelled: bool = False):
        width = 30
        step = max(0, int(step or 0))
        if connected:
            bar = "▰" * width
            text = f"  ✔ Boot connection [ {bar} ] | Connected — release BOOT"
            tag = "success"
        elif failed or cancelled:
            bar = "▱" * width
            suffix = "Cancelled" if cancelled else "FAILED — hold BOOT and try again"
            text = f"  ✖ Boot connection [ {bar} ] | {suffix}"
            tag = "warning" if cancelled else "error"
        else:
            block = 6
            travel = max(1, width - block)
            cycle = max(1, travel * 2)
            pos = step % cycle
            if pos > travel:
                pos = cycle - pos
            bar = "▱" * pos + "▰" * block + "▱" * (width - pos - block)
            dots = "." * ((step % 4) + 1)
            text = f"  🔌 Boot connection [ {bar} ] | Hold BOOT{dots}"
            tag = "warning"
        self.emit("console:log", {
            "text": text,
            "tag": tag,
            "replace_pattern": r"(?:boot connection|connected)\s*\[.*\]\s*\|",
            "newline": True,
        })

    def _trigger_actual_board_reset(self, port: str, board_name: str | None = None, board_info: dict | None = None) -> bool:
        """Open serial port and perform architecture-correct hardware reset pulse."""
        owner_pid = port_occupied_owner(port)
        if owner_pid:
            self.emit("console:log", {"text": f"  ⚠ Reset(DTR/RTS) blocked: Port '{port}' is in use by another window (PID {owner_pid}).", "tag": "warning", "newline": True})
            return False
        b_name = board_name or self.current_board or ""
        b_info = board_info or self._resolve_board_info(b_name)
        platform = str(b_info.get("platform", "")).lower()
        is_uno = ("avr" in platform)
        self.emit("console:log", {"text": f"  🔄 Triggering hardware reset on {port}...", "tag": "info", "newline": True})
        try:
            # 1. Native USB-CDC (ESP32-S3 / RP2040 / SAMD) 1200-baud touch reset fallback
            if not is_uno and (self._is_native_usb_port(port) or platform in ("raspberrypi", "samd")):
                try:
                    with serial.Serial(port, baudrate=1200, timeout=0.1) as c1200:
                        c1200.dtr = False
                        c1200.rts = True
                        time.sleep(0.1)
                        c1200.rts = False
                        c1200.dtr = False
                except Exception:
                    pass
                time.sleep(0.3)

            # 2. Architecture-correct hardware reset pulse
            with serial.Serial(port, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False) as conn:
                if is_uno:
                    # AVR / Arduino Optiboot reset pulse
                    conn.dtr = False
                    time.sleep(0.05)
                    conn.dtr = True
                    time.sleep(0.05)
                    conn.dtr = False
                elif platform in ("ststm32", "raspberrypi", "ch32v", "samd"):
                    # ARM / RISC-V pulse reset
                    conn.dtr = False
                    conn.rts = False
                    time.sleep(0.05)
                    conn.dtr = True
                    time.sleep(0.05)
                    conn.dtr = False
                else:
                    # Official esptool hard_reset sequence for ESP32 / ESP8266 auto-reset circuit
                    # DTR=False, RTS=True -> pulls EN low (Reset)
                    # RTS=False -> EN goes high (MCU boots sketch)
                    conn.dtr = False
                    conn.rts = True
                    time.sleep(0.15)
                    conn.rts = False
                    conn.dtr = False
                time.sleep(0.05)
            self.emit("console:log", {"text": "  ✔ Reset pulse completed successfully.", "tag": "success", "newline": True})
            return True
        except Exception as e:
            self.emit("console:log", {"text": f"  ⚠ Could not complete Reset(DTR/RTS): {e}", "tag": "warning", "newline": True})
            return False

    def _release_port_lines(self, port: str, pulse_reset: bool = False):
        """Cleanly release DTR/RTS serial control lines so MCU is never stuck in reset or bootloop."""
        if not port:
            return
        owner_pid = port_occupied_owner(port)
        if owner_pid:
            return
        try:
            with serial.Serial(port, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False) as conn:
                conn.dtr = False
                conn.rts = False
                if pulse_reset:
                    # Gentle EN pulse with GPIO0 high to cleanly reboot MCU into its existing sketch
                    conn.rts = True
                    time.sleep(0.08)
                    conn.rts = False
                    conn.dtr = False
                    time.sleep(0.05)
                else:
                    time.sleep(0.02)
        except Exception:
            pass

    def _write_esptool_connect_config(self, cache_root: Path, connect_attempts: int = 10) -> str | None:
        """Write the esptool connect config shared by every esptool subprocess.

        esptool's default ClassicReset asserts IO0 (BOOT) and releases EN
        back-to-back with no settle delay between them. On boards with slow
        auto-reset transistor propagation the chip then boots into run mode
        ("Wrong boot mode detected (0x13)") instead of download mode and every
        connection attempt fails while the user sketch keeps running.
        The custom sequence below mirrors the proven recovery: the chip is held
        in reset (EN low) while IO0 is asserted and settled, and then EN is released
        with 500ms of bootloader settle time so the chip reliably enters download mode.
        """
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
            esptool_cfg = cache_root / "esptool.cfg"
            esptool_cfg.write_text(
                "[esptool]\n"
                f"connect_attempts = {int(connect_attempts)}\n"
                "reset_delay = 0.5\n"
                "custom_reset_sequence = D0|R1|W0.15|D1|R0|W0.5|D0\n",
                encoding="utf-8",
            )
            return str(esptool_cfg)
        except Exception:
            return None

    def _upload_worker(self, can_skip: bool = False):
        """Upload worker executing compile + flash with full hardware safety gates.

        Produces the LATEST-WORKING rich upload console output:
        ─ Upload header banner with borders
        ─ Port / upload speed info line
        ─ Bootloader polling progress bar with in-place updates
        ─ Chip info capture from esptool output → boxed panel
        ─ Firmware flash progress bar with in-place replacement
        ─ Phase-ordered display (Connecting → Erasing → Writing → Verifying → Resetting → Done)
        ─ Detailed success / failure banners with timing info
        """
        if not self.current_port:
            self.emit("console:log", {"text": "✖ Upload error: No COM port selected.", "tag": "error", "newline": True})
            return

        port = self.current_port
        owner_pid = port_occupied_owner(port)
        if owner_pid:
            self.emit("console:log", {
                "text": f"  ⚠ Upload blocked: Port '{port}' is in use by another window (PID {owner_pid}).",
                "tag": "warning",
                "newline": True,
            })
            self.is_busy = False
            self.emit("operation:phase", {"phase": "idle", "is_busy": False})
            return

        # 1. Compile sketch or reuse compile cache if sources and board are unchanged
        if is_unc_or_network_path(self.sketch_dir_path):
            self._map_unc_for_build(self.sketch_dir_path)

        cache_root = self._effective_cache_root(self.sketch_dir_path)
        current_hash = self._hash_sources()
        bin_file = self._find_cached_firmware_binary(self.current_board)
        cached_binary_exists = (bin_file is not None)

        if not getattr(self, "_last_source_hash", "") or self.current_board != getattr(self, "_last_compiled_board", ""):
            self._load_compile_cache(self.current_board)

        # Detach watchdog during compile phase
        mcu_detached_during_compile = [False]
        detach_stop = threading.Event()

        def _detach_watchdog():
            while not detach_stop.wait(0.6):
                if not self._is_port_present(port):
                    mcu_detached_during_compile[0] = True
                    self.emit("console:log", {
                        "text": f"  ⚠ MCU disconnected from {port} while compiling!\n    Compilation continues to populate cache, but upload will be skipped.",
                        "tag": "warning",
                        "newline": True,
                    })
                    break

        # ── Smart compile check (upload path) matching LATEST-WORKING-MCU- FLASHER ──
        need_compile = True
        skip_comp = bool(getattr(self, "skip_compile", False))
        bin_file = self._find_cached_firmware_binary(self.current_board)
        has_prior_build = (bin_file is not None)

        if skip_comp and has_prior_build:
            recompile_needed, reason = self._needs_recompile(self.current_board)
            if not recompile_needed:
                need_compile = False
                self.emit("console:log", {
                    "text": "  ✔ Sources unchanged — skipping recompile",
                    "tag": "success",
                    "newline": True,
                })
            else:
                self.emit("console:log", {
                    "text": f"  🔄 Recompile needed ({reason})",
                    "tag": "warning",
                    "newline": True,
                })
        elif not skip_comp:
            pass

        if not need_compile:
            cfg = load_gui_config()
            if cfg.get("clear_console_on_action", True):
                self.emit("console:clear", None)
            if cfg.get("clear_serial_on_action", False):
                self.emit("serial:clear", None)
            self.emit("console:log", {
                "text": "⚡ Sources unchanged & binary cached — proceeding directly to upload...",
                "tag": "info",
                "newline": True,
            })
        else:
            t_watch = threading.Thread(target=_detach_watchdog, daemon=True)
            t_watch.start()
            try:
                compiled = self._compile_worker(is_upload=True)
            finally:
                detach_stop.set()
                t_watch.join(timeout=1)

            if not compiled or self._stop_requested:
                self.is_busy = False
                self.active_operation = None
                self._current_op_phase = None
                self.emit("operation:phase", {"phase": "idle", "is_busy": False})
                self.emit("window:closable", {"closable": True})
                return

        # Check if MCU detached during compile
        if mcu_detached_during_compile[0] or not self._is_port_present(port):
            self.emit("console:log", {
                "text": f"  ⚠ Upload skipped: MCU on {port} is disconnected.\n  💡 Reconnect your board and click Upload again (cached build will be reused).",
                "tag": "warning",
                "newline": True,
            })
            self.is_busy = False
            self.active_operation = None
            self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False})
            self.emit("window:closable", {"closable": True})
            return

        # ── Sketch-vs-board compatibility guard (run before entering non-stoppable flash phase) ──
        selected_board = str(self.current_board or "")
        compat_boards, compat_reasons = self._get_compat_analysis()

        has_warnings = False
        warnings_list: list[str] = []
        if compat_boards and selected_board not in compat_boards:
            has_warnings = True
            compat_label = _format_compat_label(compat_boards)
            warnings_list.append(f"Selected board \"{selected_board}\" is not officially supported by this sketch.")
            warnings_list.append(f"This sketch is only compatible with: {compat_label}")

        current_hash = self._hash_sources()
        approved_hash = getattr(self, "_compat_warnings_approved_hash", "")
        if compat_reasons and approved_hash != current_hash:
            relevant_reasons = []
            for r in compat_reasons:
                if selected_board in compat_boards:
                    if r.startswith("⚠"):
                        board_prefix = selected_board.split()[0].lower()
                        if board_prefix in r.lower() or selected_board.lower() in r.lower():
                            relevant_reasons.append(r)
                else:
                    relevant_reasons.append(r)
            if relevant_reasons:
                has_warnings = True
                for r in relevant_reasons:
                    warnings_list.append(r)

        if has_warnings:
            self.emit("console:log", {"text": "  Waiting for compatibility confirmation...", "tag": "warning", "newline": True})
            severe_reasons = [r for r in warnings_list if not r.startswith("⚠")]
            soft_reasons = [r for r in warnings_list if r.startswith("⚠")]

            self.emit("console:log", {"text": "", "newline": True})
            if severe_reasons:
                self.emit("console:log", {"text": "  🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨", "tag": "severe_alert", "newline": True})
                self.emit("console:log", {"text": "  ════════════════════════════════════════════════════════════════════════════", "tag": "severe_alert", "newline": True})
                self.emit("console:log", {"text": "  ⚠ COMPATIBILITY ISSUE DETECTED — CONTINUING MAY DAMAGE YOUR BOARD OR FIRMWARE! ⚠", "tag": "severe_alert", "newline": True})
                self.emit("console:log", {"text": "  ════════════════════════════════════════════════════════════════════════════", "tag": "severe_alert", "newline": True})
                for reason in severe_reasons:
                    self.emit("console:log", {"text": f"  ✖ {reason}", "tag": "severe_alert", "newline": True})
                self.emit("console:log", {"text": "  ════════════════════════════════════════════════════════════════════════════", "tag": "severe_alert", "newline": True})
                self.emit("console:log", {"text": "  🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨", "tag": "severe_alert", "newline": True})
            else:
                self.emit("console:log", {"text": "  ⚠ Compatibility warning(s) detected:", "tag": "warning", "newline": True})
            for reason in soft_reasons:
                clean = re.sub(
                    r"\s+on \d+ boards \(e\.g\., .*\)", "",
                    reason[2:].lstrip() if reason.startswith("⚠") else reason,
                )
                self.emit("console:log", {"text": f"  • {clean}", "tag": "warning", "newline": True})
            self.emit("console:log", {"text": "", "newline": True})

            proceed: list[bool | None] = [None]
            proceed_ev = threading.Event()
            def _on_confirm(answer: bool):
                proceed[0] = answer
                proceed_ev.set()

            reasons_text = "\n".join(f"- {r}" for r in warnings_list)
            if severe_reasons:
                msg = (
                    f"Warning: This project has compatibility warnings/exclusions:\n\n"
                    f"{reasons_text}\n\n"
                    "It may be dangerous to proceed and could affect MCU operation.\n"
                    "Do you still want to proceed with the upload?"
                )
            else:
                msg = (
                    f"The sketch uses pins/features that may not behave as expected on "
                    f"the selected board(s):\n\n{reasons_text}\n\n"
                    "It will usually still work, but could cause instability.\n"
                    "Do you still want to proceed with the upload?"
                )

            self.emit("compat_confirm:requested", {
                "title": "Compatibility Warning — MCU Flasher",
                "message": msg,
                "callback": _on_confirm,
            })
            proceed_ev.wait(timeout=60.0)

            if not proceed[0]:
                self.emit("console:log", {"text": "", "newline": True})
                self.emit("console:log", {"text": "  ℹ Upload cancelled by user.", "tag": "info", "newline": True})
                self.is_busy = False
                self.active_operation = None
                self._current_op_phase = None
                self.emit("operation:phase", {"phase": "idle", "is_busy": False})
                self.emit("window:closable", {"closable": True})
                return

            if compat_reasons:
                self._compat_warnings_approved_hash = current_hash

        # ── Upload / Flash phase (strictly non-cancellable) ──────────
        self.active_operation = "flash"
        self._current_op_phase = "flashing"
        self.emit("operation:phase", {"phase": "flash", "is_busy": True, "can_stop": False, "op": "upload"})
        self.emit("window:closable", {"closable": False})
        self.emit("console:progress", {"action": "Uploading"})
        was_monitoring = getattr(self, "_serial_thread", None) is not None
        self._stop_serial_monitor()
        time.sleep(0.4)

        upload_start = time.time()
        binfo = self._resolve_board_info(self.current_board)
        board_name = self.current_board or ""
        platform_str = str(binfo.get("platform", "")).lower()
        is_avr = platform_str == "atmelavr"
        is_esp = platform_str in ("espressif32", "espressif8266")
        upload_speed = str(getattr(self, "upload_speed", None)
                          or load_gui_config().get("upload_speed", DEFAULT_UPLOAD_SPEED))

        # ── Upload header banner ─────────────────────────────────────
        self.emit("console:log", {"text": "", "newline": True})
        self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
        self.emit("console:log", {"text": "  ⬆  UPLOADING (PlatformIO)", "tag": "header", "newline": True})
        self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
        board_label = f" | Board : {board_name}" if board_name else ""
        self.emit("console:log", {
            "text": f"  Port : {port}{board_label} | Upload Speed : {upload_speed}",
            "tag": "port_highlight",
            "newline": True
        })

        _MAX_CONNECT_RETRIES = 10
        rc: int | None = None
        try:
            # ── Fast direct ESP upload check ──────────────────────────────
            fast_bins = None
            if is_esp:
                fast_bins = self._locate_soft_reset_fast_binaries(
                    self.sketch_dir_path,
                    board_name,
                    str(binfo.get("platform", "")).lower(),
                    env_name="mcu_env",
                    upload_speed=upload_speed,
                )

            if fast_bins is not None:
                if (is_s3_board(str(binfo.get("board", ""))) or "s3" in board_name.lower()) and self._is_native_usb_port(port):
                    fast_bins["before"] = "usb-reset"
                self.emit("console:log", {"text": "  ⚡ Fast upload: polling the bootloader now…", "tag": "info", "newline": True})
                self.emit("console:log", {"text": f"  ⚙ Esptool baud: {upload_speed} (selected upload speed)", "tag": "dim", "newline": True})
                self.emit("console:log", {"text": "", "newline": True})
                self.emit("console:log", {
                    "text": "  💡 Hold BOOT now — the uploader will keep polling the bootloader.",
                    "tag": "info",
                    "replace_pattern": r"💡\s*Hold BOOT now.*",
                    "newline": True,
                })

                fast_ok, fast_error, fast_attempts = self._soft_reset_esptool_write(fast_bins, port)
                upload_duration = round(time.time() - upload_start, 2)
                if fast_ok:
                    self.emit("console:log", {"text": "", "newline": True})
                    self.emit("console:log", {"text": f"  ✔ Upload successful! {board_name} is running…", "tag": "success", "newline": True})
                    upload_fields = [
                        ("Upload Port", f"{port} @ {upload_speed} baud"),
                        ("Upload Time", f"{upload_duration}s"),
                    ]
                    self._print_info_box("Upload Summary", upload_fields)
                    self.emit("console:progress", {"action": "Completed"})
                    self.emit("notification", {"title": "Upload Succeeded", "message": f"Successfully flashed to {port} ({upload_duration}s)", "type": "success"})
                    time.sleep(0.5)
                    self._trigger_actual_board_reset(port, self.current_board, binfo)
                    rc = 0
                    return
                elif self._stop_requested:
                    self.emit("console:log", {"text": "", "newline": True})
                    self.emit("console:log", {"text": "  ■ Upload stopped by user.", "tag": "warning", "newline": True})
                    rc = -1
                    return
                else:
                    failure_kind = getattr(self, "_last_fast_upload_failure_kind", "")
                    self.emit("console:log", {"text": "", "newline": True})
                    if failure_kind == "connection":
                        self._append_connecting_progress(
                            min(fast_attempts, _MAX_CONNECT_RETRIES),
                            _MAX_CONNECT_RETRIES,
                            failed=True,
                        )
                        self.emit("console:log", {
                            "text": f"  ✖ Failed to connect to {board_name} on {port} after {fast_attempts}/{_MAX_CONNECT_RETRIES} attempts.",
                            "tag": "error",
                            "newline": True,
                        })
                        self.emit("console:log", {
                            "text": "  ✔ Safe state: Existing firmware on your MCU was NOT erased or modified.",
                            "tag": "success",
                            "newline": True,
                        })
                        if any(token in str(fast_error).lower() for token in ("not functioning", "write timeout", "cannot configure port", "permissionerror")):
                            self.emit("console:log", {
                                "text": "  💡 USB driver stalled (Windows error 31): Please unplug your board's USB cable, plug it back in, and retry.",
                                "tag": "warning",
                                "newline": True,
                            })
                        platform_str = str(binfo.get("platform", "")).lower()
                        if platform_str == "espressif8266":
                            self.emit("console:log", {"text": "  💡 ESP8266: hold BOOT/GPIO0 LOW, press RESET/EN, then release BOOT after Connected.", "tag": "info", "newline": True})
                        else:
                            self.emit("console:log", {
                                "text": "  💡 ESP32 / ESP32-S3 boards: hold BOOT, press RESET, release BOOT.",
                                "tag": "info",
                                "newline": True,
                            })
                        self.emit("console:log", {"text": "  💡 Or: unplug & replug the USB cable, then try again.", "tag": "info", "newline": True})
                    else:
                        self.emit("console:log", {"text": f"  ✖ Fast upload failed: {fast_error}", "tag": "error", "newline": True})
                    self.emit("console:log", {"text": f"  ✖ Upload FAILED after {upload_duration}s", "tag": "error", "newline": True})
                    self.emit("console:log", {
                        "text": "  ℹ Compiled output and board caches were preserved; upload failures do not require Clean.",
                        "tag": "info",
                        "newline": True,
                    })
                    self.emit("notification", {"title": "Upload Failed", "message": f"Upload failed ({fast_error})", "type": "error"})
                    rc = 1
                    return

            _connect_retry = [1]
            probed_ok = False
            if is_esp:
                try:
                    probed_ok = bool(self._probe_chip_info(port))
                except Exception:
                    probed_ok = False
                self.emit("console:log", {"text": "  ⚡ PlatformIO upload: polling the bootloader now…", "tag": "info", "newline": True})
                self.emit("console:log", {"text": f"  ⚙ Esptool baud: {upload_speed} (selected upload speed)", "tag": "dim", "newline": True})
                self.emit("console:log", {"text": "", "newline": True})
                self._append_connecting_progress(_connect_retry[0], _MAX_CONNECT_RETRIES)
                self.emit("console:log", {
                    "text": "  💡 Hold BOOT now — the uploader will keep polling the bootloader.",
                    "tag": "info",
                    "replace_pattern": r"💡\s*Hold BOOT now.*",
                    "newline": True,
                })
            pio_cmd = find_pio_executable() or ensure_platformio()
            if not pio_cmd:
                self.emit("console:log", {"text": "✖ PlatformIO not available for upload.", "tag": "error", "newline": True})
                return
            cache_root = self._effective_cache_root(self.sketch_dir_path)
            # Ensure platformio.ini and build directories exist before invoking PlatformIO!
            if not (cache_root / "platformio.ini").is_file():
                self._generate_platformio_ini(cache_root)
            (cache_root / ".pio" / "build" / "mcu_env").mkdir(parents=True, exist_ok=True)
            (cache_root / ".pio" / "libdeps" / "mcu_env").mkdir(parents=True, exist_ok=True)

            jobs = self._get_jobs()
            cmd = pio_cmd + ["run", "-e", "mcu_env", "-t", "upload", "-j", str(jobs), "--upload-port", port]

            creation_flags = (
                (subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
            )
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
                startupinfo.wShowWindow = 0

            launch_env = os.environ.copy()
            core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)
            launch_env["PLATFORMIO_CORE_DIR"] = str(core_dir)
            tmp_dir = core_dir / ".tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            launch_env["TMP"] = str(tmp_dir)
            launch_env["TEMP"] = str(tmp_dir)
            launch_env["PYTHONUNBUFFERED"] = "1"
            launch_env.pop("PYTHONOPTIMIZE", None)
            launch_env["PLATFORMIO_BUILD_JOBS"] = str(jobs)
            launch_env["PLATFORMIO_RUN_JOBS"] = str(jobs)
            launch_env["SCONSFLAGS"] = f"-j{jobs}"

            # Synchronize esptool connect behavior via cache_root and env vars
            esptool_cfg_path = self._write_esptool_connect_config(cache_root, _MAX_CONNECT_RETRIES)
            if esptool_cfg_path:
                launch_env["ESPTOOL_CFGFILE"] = esptool_cfg_path
            launch_env["ESPTOOL_CONNECT_ATTEMPTS"] = str(_MAX_CONNECT_RETRIES)

            self._active_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
                startupinfo=startupinfo,
                cwd=str(cache_root),
                env=launch_env,
            )

            # ── Chip-info capture from esptool output ───────────────
            _chip_info: dict[str, str] = {}
            _chip_info_shown = [probed_ok]
            _connected_bar_flipped = [False]
            _connect_failed_flipped = [False]
            _flash_image_count = [0]
            _hash_verified_count = [0]
            _current_phase = ["Preparing"]
            _connection_started = [False]
            _chip_stopped_responding = [False]
            _pending_pre_box: list[tuple[str, str]] = []

            def _buffered_append(text: str, tag: str = "dim"):
                """Buffer pre-connection metadata so it only prints after connection succeeds."""
                if not is_esp or _chip_info_shown[0] or _connected_bar_flipped[0]:
                    self.emit("console:log", {"text": text, "tag": tag, "newline": True})
                else:
                    if not any(t == text for t, _ in _pending_pre_box):
                        _pending_pre_box.append((text, tag))

            def _maybe_show_chip_info_box(force: bool = False):
                """Render the boxed chip-info panel once all fields are in."""
                if _chip_info_shown[0]:
                    return
                model = _chip_info.get("Chip Model") or (board_name if force else None)
                if model or force:
                    display_model = model or board_name
                    if _chip_info.get("Features"):
                        try:
                            _chip_info["Features"] = _enrich_chip_features(
                                display_model, _chip_info["Features"]
                            )
                        except Exception:
                            pass
                    if "Flash Size" not in _chip_info:
                        try:
                            configured_flash, _ = normalized_board_memory_options(binfo)
                            if configured_flash:
                                _chip_info["Flash Size"] = f"{configured_flash} (configured, not auto-detected)"
                        except Exception:
                            pass
                    _chip_info_shown[0] = True
                    fields = list(_chip_info.items())
                    self._print_chip_info_box(display_model, fields)
                    _pending_pre_box.clear()

            def _flip_to_connected_bar():
                """Re-render the last progress line as a green '✔ Connected' bar."""
                if is_esp and not _connected_bar_flipped[0]:
                    _connected_bar_flipped[0] = True
                    _current_phase[0] = "Connected"
                    self._append_connecting_progress(
                        _connect_retry[0], _MAX_CONNECT_RETRIES, connected=True
                    )
                    self.emit("console:log", {
                        "text": "  ✔ Bootloader synced — you may release the BOOT button now.",
                        "tag": "success",
                        "replace_pattern": r"💡\s*Hold BOOT now.*",
                        "newline": True
                    })

            def _flip_to_failed_bar():
                """Re-render the last progress line as a red '🔌 Connecting [...] | FAILED' bar."""
                if is_esp and not _connected_bar_flipped[0] and not _connect_failed_flipped[0]:
                    _connect_failed_flipped[0] = True
                    self._append_connecting_progress(
                        _connect_retry[0], _MAX_CONNECT_RETRIES, failed=True
                    )
                    self.emit("console:log", {
                        "text": "  💡 If connection timed out, hold the BOOT button on the physical board while clicking Upload.",
                        "tag": "warning",
                        "replace_pattern": r"💡\s*Hold BOOT now.*",
                        "newline": True
                    })

            _fallback_upload_state = self._new_upload_progress_state()

            for line in iter(self._active_process.stdout.readline, ""):
                if (
                    self._stop_requested
                    and self._current_op_phase not in ("flashing", "writing", "resetting", "erasing")
                    and self.active_operation not in ("flash", "reset")
                ):
                    self._active_process.terminate()
                    break
                line_clean = line.rstrip("\r\n")
                if not line_clean:
                    continue
                low = line_clean.lower()

                # ── Suppress ALL SCons / PIO build-scan boilerplate ─────
                # PlatformIO's upload subprocess re-runs a lightweight dependency
                # scan before invoking esptool.  All of that internal output
                # (Retrieved from cache, Compiling, Archiving, LDF, Dependency
                # Graph, etc.) is invisible noise during upload and must be fully
                # swallowed.  Only esptool connection + flash lines should show.
                _stripped = line_clean.strip()
                if (
                    # PIO environment / system headers
                    low.startswith("platform:")
                    or low.startswith("hardware:")
                    or low.startswith("packages:")
                    or low.startswith("configuration:")
                    or low.startswith("sdk:")
                    or low.startswith("embedded:")
                    # SCons build-scan lines
                    or low.startswith("compiling ")
                    or low.startswith("archiving ")
                    or low.startswith("linking ")
                    or low.startswith("building ")
                    or low.startswith("retrieving ")
                    or low.startswith("checking size")
                    or low.startswith("retrieved ")
                    or low.startswith("converting ")
                    or "from cache" in low
                    # Dependency / library scan
                    or low.startswith("ldf:")
                    or low.startswith("ldf modes:")
                    or low.startswith("found ")
                    or low.startswith("scanning dependencies")
                    or low.startswith("dependency graph")
                    or low.startswith("|--")
                    or low.startswith("|   ")
                    or low.startswith("processing ")
                    # Packages list items (lines like " - toolchain-xtensa @ …")
                    or re.match(r"^\s+-\s+\S+\s+@\s+", line_clean)
                    # PIO misc noise
                    or "verbose mode can be enabled" in low
                    or "building in release mode" in low
                    or "advanced memory usage" in low
                    or "platformio home" in low
                    # Separator lines (--- and ===)
                    or line_clean.startswith("---")
                    or line_clean.startswith("===")
                ):
                    continue

                # ── Swallow promotional / registry noise ────────────
                if any(kw in low for kw in (
                    "looking for ", "check our library registry", "* cli  >", "* web  >",
                    "if you like platformio", "star it on github", "follow us on linkedin",
                    "try platformio ide", "please wait while upgrading", "successfully upgraded"
                )) or _stripped.startswith("*****") or _stripped == "*":
                    continue


                # ── PIO result line === [SUCCESS] Took X.XX seconds ===
                pio_result = re.search(r'=+\s*\[(SUCCESS|FAILED)\]\s*Took\s*([\d.]+)\s*seconds', line_clean, re.IGNORECASE)
                if pio_result:
                    verdict = pio_result.group(1).upper()
                    tag = "success" if verdict == "SUCCESS" else "error"
                    self.emit("console:log", {"text": f"  {line_clean.strip()}", "tag": tag, "newline": True})
                    continue

                # ── Chip-info capture from esptool ──────────────────
                if is_esp:
                    m = re.search(r'chip (?:is|type)\s*:?\s+(.+)$', line_clean, re.IGNORECASE)
                    if m:
                        _chip_info["Chip Model"] = m.group(1).strip()
                    m = re.search(r'features\s*:\s*(.+)$', line_clean, re.IGNORECASE)
                    if m:
                        _chip_info["Features"] = m.group(1).strip()
                    m = re.search(r'crystal (?:is|frequency)\s*:?\s+(.+)$', line_clean, re.IGNORECASE)
                    if m:
                        _chip_info["Crystal"] = m.group(1).strip()
                    m = re.search(r'^\s*mac\s*:\s*(.+)$', line_clean, re.IGNORECASE)
                    if m:
                        _chip_info["MAC Address"] = m.group(1).strip()
                    m = re.search(r'(?:auto-detected\s+)?flash size\s*:\s*(.+)$', line_clean, re.IGNORECASE)
                    if m:
                        _chip_info["Flash Size"] = m.group(1).strip()

                # ── Multi-partition upload progress parsing ─────────
                if is_esp and self._consume_esptool_upload_progress(
                    _fallback_upload_state, line_clean,
                    before_progress=_flip_to_connected_bar,
                    phase_callback=lambda ph: _current_phase.__setitem__(0, ph)
                ):
                    continue

                # ── Parse esptool write progress (standalone fallback) ─
                parsed = _parse_esptool_write_progress(line_clean)
                if parsed:
                    pct = float(parsed.get("percent", 0.0))
                    written = parsed.get("written", "")
                    total = parsed.get("total", "")

                    # Advance to Writing phase
                    if _current_phase[0] in ("Connecting", "Connected", "Erasing"):
                        _current_phase[0] = "Writing"
                        _flip_to_connected_bar()
                        _maybe_show_chip_info_box()

                    addr_str = str(parsed.get("address", "")).lower()
                    try:
                        int_addr = int(addr_str, 16)
                    except Exception:
                        int_addr = -1

                    if int_addr < 0x8000:
                        part_label = "Bootloader"
                    elif int_addr < 0x9000:
                        part_label = "Partitions"
                    elif int_addr < 0x10000:
                        part_label = "Boot App"
                    else:
                        part_label = "Firmware"

                    # Build in-place progress bar
                    bar_width = 30
                    filled = int(pct / 100.0 * bar_width)
                    bar = "▰" * filled + "▱" * max(0, bar_width - filled)
                    icon = "✔" if pct >= 100.0 else "⚙"
                    size_label = f" | {written}/{total}" if written and total else ""
                    progress_text = f"  {icon} Flashing {part_label} [ {bar} ] | {pct:5.1f}%{size_label}"
                    self.emit("console:log", {
                        "text": progress_text,
                        "tag": "success" if pct >= 100.0 else "info",
                        "replace_pattern": rf"(?:Flashing)\s+{re.escape(part_label)}\s*\[",
                        "newline": True
                    })
                    self.emit("console:progress", {"action": "Uploading"})
                    continue

                # ── Track mid-write communication failures ───────────
                if "the chip stopped responding" in low or "stopiteration" in low:
                    _chip_stopped_responding[0] = True

                # ── Upload protocol noise (suppress but capture phase) ──
                _NOISE = (
                    "auto-detected:", "uploading stub", "running stub", "stub running",
                    "stub flasher running", "connected to",
                    "changing baud", "compressed", "leaving...",
                    "warning: espcomm", "esptool.py v", "esptool v", "serial port",
                    "v2.", "v3.", "v4.", "v5.", "loaded custom configuration",
                )
                if any(n in low for n in _NOISE):
                    # Detect start of connection when esptool opens the port
                    if not is_avr and not _connection_started[0]:
                        if "serial port" in low or "esptool" in low:
                            _connection_started[0] = True
                            _current_phase[0] = "Connecting"
                    # Advance phase only on genuine stub sync
                    if any(t in low for t in ("stub running", "running stub", "uploading stub", "stub flasher running", "connected to")):
                        _flip_to_connected_bar()
                        _maybe_show_chip_info_box()
                    elif "erasing" in low or "erase" in low:
                        if _current_phase[0] in ("Connecting", "Connected"):
                            _current_phase[0] = "Erasing"
                            _flip_to_connected_bar()
                            _maybe_show_chip_info_box()
                    elif "leaving" in low or "hard reset" in low:
                        _current_phase[0] = "Resetting"
                    continue

                # ── Suppress chip-info lines already captured ───────
                if not is_avr and any(kw in low for kw in ("chip is", "chip type:", "features:", "crystal is", "crystal frequency:", "mac:")):
                    continue

                # ── Connection lines ────────────────────────────────
                if "connecting" in low and not is_avr:
                    _connection_started[0] = True
                    _current_phase[0] = "Connecting"
                    continue

                # ── Erasing ─────────────────────────────────────────
                if "erasing" in low:
                    if _current_phase[0] in ("Connecting", "Connected"):
                        _flip_to_connected_bar()
                        _maybe_show_chip_info_box()
                    _current_phase[0] = "Erasing"
                    self.emit("console:log", {"text": f"  ⚡ {line_clean.strip()}", "tag": "info", "newline": True})
                    continue

                # ── Writing / Wrote ─────────────────────────────────
                if "writing at" in low:
                    _current_phase[0] = "Writing"
                    continue  # Handled by _parse_esptool_write_progress above
                if "wrote" in low:
                    _current_phase[0] = "Writing"
                    self.emit("console:log", {"text": f"  ✔ {line_clean.strip()}", "tag": "success", "newline": True})
                    continue

                # ── Verifying ───────────────────────────────────────
                if "hash of data verified" in low:
                    _current_phase[0] = "Verifying"
                    continue
                if "verifying" in low:
                    _current_phase[0] = "Verifying"
                    self.emit("console:log", {"text": f"  ✔ {line_clean.strip()}", "tag": "success", "newline": True})
                    continue

                # ── Hard resetting ──────────────────────────────────
                if "hard resetting" in low:
                    _current_phase[0] = "Resetting"
                    self.emit("console:log", {"text": f"  ✔ {line_clean.strip()}", "tag": "success", "newline": True})
                    continue

                # ── Creating image ──────────────────────────────────
                if "creating" in low and "image" in low and "created" not in low:
                    chip_match = re.search(r'creating (\w+) image', low)
                    chip_name = chip_match.group(1).upper() if chip_match else "MCU"
                    self.emit("console:log", {"text": f"  ⚙ Creating {chip_name} image...", "tag": "info", "newline": True})
                    continue

                # ── Successfully created image ──────────────────────
                if "successfully created" in low and "image" in low:
                    chip_match = re.search(r'successfully created (\w+) image', low)
                    chip_name = chip_match.group(1).upper() if chip_match else "MCU"
                    _flash_image_count[0] += 1
                    label = "Bootloader" if _flash_image_count[0] == 1 else "Application"
                    self.emit("console:log", {"text": f"  ✔ Successfully created {chip_name} image ({label})", "tag": "success", "newline": True})
                    continue

                # ── AVR-specific handling ────────────────────────────
                if is_avr:
                    if "avr device initialized" in low or "device signature" in low:
                        self.emit("console:log", {"text": f"  🔌 Connected to {board_name}", "tag": "success", "newline": True})
                    elif "writing flash" in low or "writing eeprom" in low:
                        self.emit("console:log", {"text": f"  ⚙ {line_clean.strip()}", "tag": "info", "newline": True})
                    elif "verifying flash" in low or "verifying eeprom" in low:
                        self.emit("console:log", {"text": f"  ✔ {line_clean.strip()}", "tag": "success", "newline": True})
                    elif "avrdude done" in low or "bytes of flash" in low or "bytes written" in low:
                        self.emit("console:log", {"text": f"  ✔ {line_clean.strip()}", "tag": "success", "newline": True})
                    elif "error" in low or "failed" in low:
                        self.emit("console:log", {"text": f"  ✖ {line_clean.strip()}", "tag": "error", "newline": True})
                    else:
                        self.emit("console:log", {"text": f"  {line_clean}", "tag": "dim", "newline": True})
                    continue

                # ── Suppress RAM/Flash during upload (already logged during compile) ─
                if "ram:" in low or "flash:" in low:
                    continue

                # ── Suppress upload protocol config boilerplate ──────
                if re.match(r"\s*(configuring upload protocol|available:|current:|debug:)", low):
                    continue

                # ── Suppress esptool flash setup / erase boilerplate ─
                if any(kw in low for kw in (
                    "using manually specified:", "uploading .pio", "looking for upload port",
                    "configuring flash size", "flash will be erased from", "changed.",
                    "sha digest in image updated"
                )):
                    continue

                # ── Suppress raw Python traceback frames from esptool exceptions ────
                if (
                    "traceback (most recent call last)" in low
                    or ('file "' in low and "esptool" in low)
                    or "stopiteration" in low
                    or _stripped.startswith("~~~")
                    or _stripped.startswith("^^^")
                    or _stripped.startswith("...<")
                ):
                    continue

                # ── Error / failure lines ───────────────────────────
                if "error" in low or "failed" in low:
                    _flip_to_failed_bar()
                    self.emit("console:log", {"text": f"  ✖ {line_clean.strip()}", "tag": "error", "newline": True})
                    continue

                # ── Warning lines ───────────────────────────────────
                if "warning" in low:
                    self.emit("console:log", {"text": f"  ⚠ {line_clean.strip()}", "tag": "warning", "newline": True})
                    continue

                # ── Fallthrough: print generic info ─────────────────
                self.emit("console:log", {"text": f"  {line_clean}", "tag": "dim", "newline": True})

            self._active_process.stdout.close()
            rc = self._active_process.wait()
            upload_duration = round(time.time() - upload_start, 2)

            if rc == 0:
                # Show chip-info box if not shown yet (e.g. AVR boards or unprobed chips)
                if is_esp and not _chip_info_shown[0]:
                    _maybe_show_chip_info_box(force=True)

                self.emit("console:log", {"text": "", "newline": True})
                self.emit("console:log", {
                    "text": "  ✔ Flash write and verification completed. Reset will continue through the Serial Monitor.",
                    "tag": "success",
                    "newline": True
                })
                self.emit("console:log", {
                    "text": f"  ✔ Upload successful! {board_name} is running…",
                    "tag": "success",
                    "newline": True
                })

                # Upload time breakdown
                upload_fields = [
                    ("Upload Port", f"{port} @ {upload_speed} baud"),
                    ("Upload Time", f"{upload_duration}s"),
                ]
                self._print_info_box("Upload Summary", upload_fields)

                self.emit("console:progress", {"action": "Completed"})
                self.emit("notification", {"title": "Upload Succeeded", "message": f"Successfully flashed to {port} ({upload_duration}s)", "type": "success"})

                time.sleep(0.5)
                binfo = self._resolve_board_info(self.current_board)
                self._trigger_actual_board_reset(port, self.current_board, binfo)
            elif self._stop_requested:
                _pending_pre_box.clear()
                self.emit("console:log", {"text": "", "newline": True})
                self.emit("console:log", {"text": "  ■ Upload stopped by user.", "tag": "warning", "newline": True})
            else:
                _pending_pre_box.clear()
                self.emit("console:log", {"text": "", "newline": True})

                # Check if it was a connection failure
                if is_esp and not _connected_bar_flipped[0]:
                    _flip_to_failed_bar()
                    self.emit("console:log", {
                        "text": "  ✔ Safe state: Existing firmware on your MCU was NOT erased or modified.",
                        "tag": "success",
                        "newline": True,
                    })
                    platform_str = str(binfo.get("platform", "")).lower()
                    if platform_str == "espressif8266":
                        self.emit("console:log", {"text": "  💡 ESP8266: hold BOOT/GPIO0 LOW, press RESET/EN, then release BOOT after Connected.", "tag": "info", "newline": True})
                    else:
                        self.emit("console:log", {
                            "text": "  💡 Manual Bootloader Mode (ESP32 Dev Module):",
                            "tag": "warning",
                            "newline": True,
                        })
                        self.emit("console:log", {
                            "text": "     1. Press and HOLD the physical 'BOOT' button on your ESP32 board.",
                            "tag": "info",
                            "newline": True,
                        })
                        self.emit("console:log", {
                            "text": "     2. Click 'Upload' in MCU Flasher while holding 'BOOT'.",
                            "tag": "info",
                            "newline": True,
                        })
                        self.emit("console:log", {
                            "text": "     3. Release 'BOOT' as soon as '✔ Bootloader synced' appears in console.",
                            "tag": "info",
                            "newline": True,
                        })
                    self.emit("console:log", {"text": "  💡 Or: unplug & replug the USB cable, then try again.", "tag": "info", "newline": True})
                elif _chip_stopped_responding[0]:
                    self.emit("console:log", {"text": "", "newline": True})
                    self.emit("console:log", {
                        "text": "  💡 The MCU stopped responding mid-flash (UART communication loss or power droop):",
                        "tag": "warning",
                        "newline": True,
                    })
                    self.emit("console:log", {
                        "text": f"    1. Lower Upload Speed: Switch from {upload_speed} to '115200' in the top toolbar.",
                        "tag": "info",
                        "newline": True,
                    })
                    self.emit("console:log", {
                        "text": "    2. Use Direct USB Port: Connect directly to PC motherboard USB (avoid hubs/front ports).",
                        "tag": "info",
                        "newline": True,
                    })
                    self.emit("console:log", {
                        "text": "    3. Reconnect Board: Unplug and reconnect the USB cable, then retry upload.",
                        "tag": "info",
                        "newline": True,
                    })

                self.emit("console:log", {"text": f"  ✖ Upload FAILED after {upload_duration}s (exit code {rc})", "tag": "error", "newline": True})
                self.emit("console:log", {
                    "text": "  ℹ Compiled output and board caches were preserved; upload failures do not require Clean.",
                    "tag": "info",
                    "newline": True
                })
                self.emit("notification", {"title": "Upload Failed", "message": f"Upload failed with exit code {rc}", "type": "error"})

        except Exception as e:
            self.emit("console:log", {"text": f"✖ Upload error: {e}", "tag": "error", "newline": True})
        finally:
            try:
                if cache_root and (cache_root / "esptool.cfg").exists():
                    (cache_root / "esptool.cfg").unlink(missing_ok=True)
            except Exception:
                pass
            self._unmap_unc_after_build()
            self.is_busy = False
            self.active_operation = None
            self._current_op_phase = None
            is_success = (rc == 0) if rc is not None else False
            self.emit("operation:phase", {
                "phase": "idle",
                "is_busy": False,
                "op": "upload",
                "success": is_success,
            })
            self.emit("window:closable", {"closable": True})
            if not is_success:
                # Cleanly release serial control lines (DTR/RTS) so MCU returns to normal run mode and does not bootloop
                self._release_port_lines(port, pulse_reset=True)
            if is_success or was_monitoring:
                time.sleep(0.5)
                self._start_serial_monitor()

    # ──────────────────────────────────────────────────────────
    # JS-RPC: RESETS & CACHE ACTIONS
    # ──────────────────────────────────────────────────────────
    def reset_mcu(self):
        """Perform a hardware reboot of the connected microcontroller via DTR/RTS without dropping the serial monitor."""
        if self.is_busy:
            self.emit("serial:log", {"text": "--- ⚠ Cannot reset MCU: Operation currently in progress. ---", "tag": "warning", "newline": True})
            return

        cfg = load_gui_config()
        if getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)):
            self.emit("serial:clear", None)

        if not self.current_port:
            self.emit("serial:log", {"text": "--- ✖ Cannot reset MCU: No COM port selected. ---", "tag": "error", "newline": True})
            return

        def _worker():
            try:
                self.emit("console:progress", {"action": "Resetting MCU"})
                binfo = self._resolve_board_info(self.current_board)
                platform = str(binfo.get("platform", "")).lower()
                board_id = str(binfo.get("board", "")).lower()
                is_uno = ("avr" in platform)
                is_arm = platform in ("ststm32", "raspberrypi", "ch32v", "samd")
                is_s3_or_cdc = "s3" in board_id or platform in ("raspberrypi", "samd")

                self.emit("serial:log", {
                    "text": f"--- ↺ Triggering hardware reset on {self.current_port}... ---",
                    "tag": "info",
                    "newline": True,
                })

                # 1. Check if serial connection is already active and open
                with self._serial_lock:
                    active_conn = self._serial_conn if (self._serial_conn and self._serial_conn.is_open) else None

                if active_conn:
                    try:
                        if is_uno:
                            active_conn.rts = False
                            active_conn.dtr = False
                            time.sleep(0.05)
                            active_conn.dtr = True
                            time.sleep(0.10)
                            active_conn.dtr = False
                            time.sleep(0.05)
                        elif is_arm:
                            active_conn.dtr = False
                            active_conn.rts = False
                            time.sleep(0.05)
                            active_conn.dtr = True
                            time.sleep(0.05)
                            active_conn.dtr = False
                            time.sleep(0.05)
                        else:
                            active_conn.dtr = False
                            active_conn.rts = True
                            time.sleep(0.15)
                            active_conn.rts = False
                            active_conn.dtr = False
                            time.sleep(0.05)

                        self.emit("serial:log", {
                            "text": "--- ✔ Hardware reset pulse completed successfully ---",
                            "tag": "success",
                            "newline": True,
                        })
                        return
                    except Exception as ex:
                        self.emit("serial:log", {
                            "text": f"--- ⚠ In-place reset pulse error: {ex}, attempting direct port reset... ---",
                            "tag": "warning",
                            "newline": True,
                        })

                # 2. If not active or in-place failed, briefly connect directly, pulse, and resume monitor
                self._stop_serial_monitor()
                time.sleep(0.1)
                try:
                    if is_s3_or_cdc and not is_uno:
                        try:
                            with serial.Serial(port=self.current_port, baudrate=1200, timeout=0.1) as c1200:
                                c1200.dtr = False
                                c1200.rts = True
                                time.sleep(0.1)
                                c1200.rts = False
                                c1200.dtr = False
                        except Exception:
                            pass
                        time.sleep(0.2)

                    with serial.Serial(
                        port=self.current_port,
                        baudrate=self.current_baud or 115200,
                        timeout=0.1,
                        dsrdtr=False,
                        rtscts=False,
                    ) as conn:
                        if is_uno:
                            conn.rts = False
                            conn.dtr = False
                            time.sleep(0.05)
                            conn.dtr = True
                            time.sleep(0.10)
                            conn.dtr = False
                            time.sleep(0.05)
                        elif is_arm:
                            conn.dtr = False
                            conn.rts = False
                            time.sleep(0.05)
                            conn.dtr = True
                            time.sleep(0.05)
                            conn.dtr = False
                            time.sleep(0.05)
                        else:
                            conn.dtr = False
                            conn.rts = True
                            time.sleep(0.15)
                            conn.rts = False
                            conn.dtr = False
                            time.sleep(0.05)

                    self.emit("serial:log", {
                        "text": "--- ✔ Hardware reset pulse completed successfully ---",
                        "tag": "success",
                        "newline": True,
                    })
                except Exception as e:
                    self.emit("serial:log", {
                        "text": f"--- ✖ Could not complete Reset(DTR/RTS): {e} ---",
                        "tag": "error",
                        "newline": True,
                    })
                finally:
                    self._start_serial_monitor()
            finally:
                self.emit("console:progress", {"action": "Completed"})

        threading.Thread(target=_worker, name="MCU_ResetPulse", daemon=True).start()

    def hard_reset(self, erase_flash: bool = False):
        """Perform a safe board-specific hard reset matching LATEST-WORKING-MCU- FLASHER."""
        if self.is_busy:
            if not self._active_process or self._active_process.poll() is not None:
                self.is_busy = False
                self.emit("console:log", {"text": "  ℹ Stale busy state cleared — proceeding with Hard Reset.", "tag": "info", "newline": True})
            else:
                self.emit("console:log", {"text": "⚠ Busy — cannot perform hard reset right now.", "tag": "warning", "newline": True})
                return

        cfg = load_gui_config()
        if getattr(self, "clear_console_on_action", cfg.get("clear_console_on_action", True)):
            self.emit("console:clear", None)
        if getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)):
            self.emit("serial:clear", None)

        if not self.current_board:
            self.emit("console:log", {"text": "✖ Hard Reset error: No board selected.", "tag": "error", "newline": True})
            return
        if not self.current_port:
            self.emit("console:log", {"text": "✖ Hard Reset error: No COM port selected.", "tag": "error", "newline": True})
            return

        def _worker():
            port = self.current_port
            board_name = self.current_board
            binfo = self._resolve_board_info(board_name)
            plat = str(binfo.get("platform", "")).lower()

            owner_pid = port_occupied_owner(port)
            if owner_pid:
                self.emit("console:log", {
                    "text": f"  ⚠ Hard reset blocked: Port '{port}' is in use by another window (PID {owner_pid}).",
                    "tag": "warning",
                    "newline": True,
                })
                return

            reset_cache_lock = _try_acquire_reset_cache_lock()
            if reset_cache_lock is None:
                self.emit("console:log", {
                    "text": "  ⚠ Hard Reset blocked: another window is using the shared reset cache.",
                    "tag": "warning",
                    "newline": True,
                })
                return

            self.is_busy = True
            self.active_operation = "reset"
            self._active_reset_kind = "hard"
            self._current_op_phase = "resetting"
            self.emit("operation:phase", {"phase": "reset", "is_busy": True, "can_stop": False, "op": "hard_reset"})
            self.emit("window:closable", {"closable": False})
            self.emit("console:progress", {"action": "Resetting"})
            was_monitoring = getattr(self, "_serial_thread", None) is not None
            self._stop_serial_monitor()
            time.sleep(0.5)
            hard_reset_success = False

            try:
                reset_caps = board_reset_capabilities(
                    plat, binfo.get("board", ""), board_name, binfo.get("framework", "")
                )
                reset_strategy = reset_caps.get("hard_strategy")

                self.emit("console:log", {"text": "", "newline": True})
                self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
                header_title = (
                    "  🔥 ERASING ESP8266 FLASH (Hard Reset)"
                    if reset_strategy == "esp8266_erase"
                    else "  🔥 BURNING BOOTLOADER / FULL RECOVERY (Hard Reset)"
                )
                self.emit("console:log", {"text": header_title, "tag": "header", "newline": True})
                self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
                self.emit("console:log", {"text": f"  Port  : {port}", "tag": "info", "newline": True})
                self.emit("console:log", {"text": f"  Board : {board_name}", "tag": "dim", "newline": True})
                self.emit("console:log", {"text": "  💡 Press and HOLD the BOOT button when prompted below.", "tag": "warning", "newline": True})
                self.emit("console:log", {"text": "", "newline": True})

                if reset_strategy == "avr_bootloader":
                    pio_path = find_pio_executable()
                    if not pio_path:
                        pio_path = ensure_platformio()
                    if not pio_path:
                        self.emit("console:log", {"text": "  ✖ PlatformIO is unavailable.", "tag": "error", "newline": True})
                        return
                    cache_root = get_project_build_cache_root(self.sketch_dir_path)
                    jobs = self._get_jobs()
                    cmd = pio_path + [
                        "run", "-e", "mcu_flash", "-t", "bootloader",
                        "-j", str(jobs),
                        "--upload-port", port,
                    ]
                    self.emit("console:progress", {"action": "Burning bootloader"})
                    launch_env = os.environ.copy()
                    core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)
                    launch_env["PLATFORMIO_CORE_DIR"] = str(core_dir)
                    launch_env["TMP"] = str(core_dir / ".tmp")
                    launch_env["TEMP"] = str(core_dir / ".tmp")
                    launch_env["PYTHONUNBUFFERED"] = "1"
                    launch_env.pop("PYTHONOPTIMIZE", None)
                    launch_env["PLATFORMIO_BUILD_JOBS"] = str(jobs)
                    launch_env["PLATFORMIO_RUN_JOBS"] = str(jobs)
                    launch_env["SCONSFLAGS"] = f"-j{jobs}"

                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(cache_root),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        encoding="utf-8",
                        errors="replace",
                        env=launch_env,
                        creationflags=(
                            (subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
                        ),
                    )
                    self._active_process = proc
                    if proc.stdout:
                        for line in iter(proc.stdout.readline, ""):
                            stripped = line.rstrip()
                            if stripped:
                                tag = "error" if "error" in stripped.lower() or "failed" in stripped.lower() else "normal"
                                self.emit("console:log", {"text": f"  {stripped}", "tag": tag, "newline": True})
                    proc.wait()
                    self._active_process = None
                    if proc.returncode != 0:
                        self.emit("console:log", {"text": f"  ✖ Bootloader burn failed with code {proc.returncode}.", "tag": "error", "newline": True})
                        return
                    time.sleep(0.25)
                    self._trigger_actual_board_reset(port, board_name, binfo)
                    self.emit("console:log", {"text": "  ✔ Bootloader burn completed successfully.", "tag": "success", "newline": True})
                    self.emit("notification", {"title": "Hard Reset Complete", "message": "Bootloader burned successfully.", "type": "success"})
                    hard_reset_success = True
                    return

                elif reset_strategy == "esp8266_erase":
                    self._emit_boot_connection_progress(0)
                    self.emit("console:progress", {"action": "Erasing flash"})
                    erase_cmd = self._get_esptool_cmd() + [
                        "--chip", "esp8266",
                        "--port", port,
                        "--baud", "115200",
                        "--before", "default-reset",
                        "--after", "no-reset",
                        "--connect-attempts", "30",
                        "erase_flash",
                    ]
                    launch_env = os.environ.copy()
                    erase_cfg_path = self._write_esptool_connect_config(
                        get_project_build_cache_root(self.sketch_dir_path), 30
                    )
                    if erase_cfg_path:
                        launch_env["ESPTOOL_CFGFILE"] = erase_cfg_path
                    proc = subprocess.Popen(
                        erase_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        encoding="utf-8",
                        errors="replace",
                        env=launch_env,
                        creationflags=(
                            (subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
                        ),
                    )
                    self._active_process = proc
                    connected = False
                    connection_step = 0
                    if proc.stdout:
                        for raw in iter(proc.stdout.readline, ""):
                            line = raw.strip()
                            if not line:
                                continue
                            low = line.lower()
                            if low.startswith("connecting"):
                                connection_step = max(connection_step, line.count("."))
                                self._emit_boot_connection_progress(connection_step)
                                continue
                            if "connected to " in low or "chip is" in low:
                                if not connected:
                                    connected = True
                                    self._emit_boot_connection_progress(connection_step, connected=True)
                            if "erasing flash" in low:
                                self.emit("console:log", {"text": "  🔥 Erasing entire ESP8266 flash memory...", "tag": "warning", "newline": True})
                            elif "erased successfully" in low:
                                self.emit("console:log", {"text": "  ✔ Flash memory erased successfully.", "tag": "success", "newline": True})
                            elif any(k in low for k in ("error", "failed", "fatal")):
                                self.emit("console:log", {"text": f"  ✖ {line}", "tag": "error", "newline": True})
                    proc.wait()
                    self._active_process = None
                    if proc.returncode != 0:
                        self._emit_boot_connection_progress(connection_step, failed=True)
                        self.emit("console:log", {"text": f"  ✖ ESP8266 flash erase failed (exit code {proc.returncode}).", "tag": "error", "newline": True})
                        return
                    self.emit("console:log", {"text": "  ✔ ESP8266 full flash erase completed successfully.", "tag": "success", "newline": True})
                    time.sleep(0.75)
                    self._trigger_actual_board_reset(port, board_name, binfo)
                    self.emit("notification", {"title": "Hard Reset Complete", "message": "ESP8266 flash erased successfully.", "type": "success"})
                    hard_reset_success = True
                    return

                elif reset_strategy == "esp32_recovery":
                    self.emit("console:progress", {"action": "Preparing recovery"})
                    image_set, recovery_err = self._locate_hard_reset_recovery_images(board_name, binfo)
                    if not image_set:
                        image_set, recovery_err = self._build_hard_reset_recovery_images(board_name, binfo)
                    if not image_set:
                        self.emit("console:log", {"text": f"  ✖ Hard Reset failed: {recovery_err}", "tag": "error", "newline": True})
                        return

                    bootloader_bin = Path(image_set["bootloader"])
                    partitions_bin = Path(image_set["partitions"])
                    boot_app0_bin = self._locate_esp32_boot_app0()
                    if boot_app0_bin is None:
                        self.emit("console:log", {"text": "  ✖ Could not locate boot_app0.bin in Arduino ESP32 framework.", "tag": "error", "newline": True})
                        return

                    if bootloader_bin.stat().st_size < 4096:
                        self.emit("console:log", {"text": "  ✖ bootloader.bin is truncated. Flash aborted.", "tag": "error", "newline": True})
                        return
                    if partitions_bin.stat().st_size < 0xC00:
                        self.emit("console:log", {"text": "  ✖ partitions.bin is invalid. Flash aborted.", "tag": "error", "newline": True})
                        return

                    self.emit("console:log", {"text": f"  ⚡ Using dedicated {image_set.get('source_label', 'recovery build')}.", "tag": "info", "newline": True})
                    self.emit("console:log", {"text": f"  Bootloader : {bootloader_bin.name}", "tag": "dim", "newline": True})
                    self.emit("console:log", {"text": f"  Partitions : {partitions_bin.name}", "tag": "dim", "newline": True})
                    self.emit("console:log", {"text": f"  boot_app0  : {boot_app0_bin.name}", "tag": "dim", "newline": True})
                    self.emit("console:log", {"text": "  User App   : ERASED (clean recovery state)", "tag": "warning", "newline": True})
                    self.emit("console:log", {"text": "  ⚠ Press and HOLD the BOOT button now.", "tag": "warning", "newline": True})
                    self._emit_boot_connection_progress(0)

                    target_mcu, bootloader_addr = self._esptool_target(board_name, binfo)
                    target_mcu = target_mcu or "esp32"
                    burn_cmd = self._get_esptool_cmd() + [
                        "--chip", target_mcu,
                        "--port", port,
                        "--baud", "115200",
                        "--before", "default-reset",
                        "--after", "no-reset",
                        "--connect-attempts", "30",
                        "write_flash",
                        "--erase-all",
                        "--flash_mode", "keep",
                        "--flash_freq", "keep",
                        "--flash_size", "detect",
                        bootloader_addr, str(bootloader_bin),
                        "0x8000", str(partitions_bin),
                        "0xe000", str(boot_app0_bin),
                    ]

                    launch_env = os.environ.copy()
                    recovery_cfg_path = self._write_esptool_connect_config(
                        get_project_build_cache_root(self.sketch_dir_path), 30
                    )
                    if recovery_cfg_path:
                        launch_env["ESPTOOL_CFGFILE"] = recovery_cfg_path
                    proc = subprocess.Popen(
                        burn_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=False,
                        bufsize=0,
                        env=launch_env,
                        creationflags=(
                            (subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
                        ),
                    )
                    self._active_process = proc

                    import codecs
                    import queue as _queue
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    line_buffer = ""
                    connection_step = 0
                    connected_to_chip = False
                    _byte_queue: _queue.Queue = _queue.Queue()

                    def _byte_reader():
                        try:
                            while proc and proc.stdout:
                                raw = proc.stdout.read(1)
                                if not raw:
                                    break
                                _byte_queue.put(raw)
                        except Exception:
                            pass
                        finally:
                            _byte_queue.put(None)

                    t_reader = threading.Thread(target=_byte_reader, daemon=True)
                    t_reader.start()

                    erase_started = False
                    erase_completed = False
                    erase_ticks = 0

                    def _handle_line(stripped: str):
                        nonlocal erase_started, erase_completed, connection_step, connected_to_chip
                        stripped = stripped.strip()
                        if not stripped:
                            return
                        low = stripped.lower()
                        if low.startswith("connecting"):
                            connection_step = max(connection_step, stripped.count("."))
                            self._emit_boot_connection_progress(connection_step)
                            return
                        if "connected to " in low or "uploading stub" in low or "stub flasher running" in low:
                            if not connected_to_chip:
                                connected_to_chip = True
                                self._emit_boot_connection_progress(connection_step, connected=True)
                                self.emit("console:log", {"text": "  ✔ ESP32 connected — release the BOOT button now.", "tag": "success", "newline": True})
                        if "erasing flash" in low or "chip erase" in low:
                            erase_started = True
                            self.emit("console:progress", {"action": "Erasing flash"})
                            self.emit("console:log", {"text": "  🔥 Erasing entire flash memory (this may take 30-60s)...", "tag": "warning", "newline": True})
                        elif "erased successfully" in low:
                            erase_completed = True
                            self.emit("console:log", {"text": "  ✔ Flash memory erased successfully.", "tag": "success", "newline": True})
                        elif "writing at" in low:
                            parsed = _parse_esptool_write_progress(stripped)
                            if parsed:
                                pct = float(parsed.get("percent", 0.0))
                                bar_width = 30
                                filled = int(pct / 100.0 * bar_width)
                                bar = "▰" * filled + "▱" * max(0, bar_width - filled)
                                icon = "✔" if pct >= 100.0 else "⚙"
                                progress_text = f"  {icon} Writing Bootloader [ {bar} ]"
                                self.emit("console:log", {
                                    "text": progress_text,
                                    "tag": "success" if pct >= 100.0 else "info",
                                    "replace_pattern": r"(?:Writing Bootloader)\s*\[",
                                    "newline": True
                                })
                                self.emit("console:progress", {"action": "Writing recovery bootloader"})
                                return
                        elif any(token in low for token in ("error", "failed", "fatal")):
                            self.emit("console:log", {"text": f"  ✖ {stripped}", "tag": "error", "newline": True})
                        else:
                            self.emit("console:log", {"text": f"  {stripped}", "tag": "info", "newline": True})

                    while True:
                        if self._stop_requested:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            break
                        try:
                            raw = _byte_queue.get(timeout=0.25)
                        except _queue.Empty:
                            if erase_started and not erase_completed:
                                erase_ticks += 1
                                dots = "." * ((erase_ticks % 3) + 1)
                                self.emit("console:progress", {"action": f"Erasing entire flash{dots}"})
                            continue

                        if raw is None:
                            break

                        decoded = decoder.decode(raw)
                        for char in decoded:
                            if char in "\r\n":
                                if line_buffer:
                                    _handle_line(line_buffer)
                                    line_buffer = ""
                                continue
                            line_buffer += char
                            if line_buffer.lower().startswith("connecting") and char == ".":
                                connection_step += 1
                                self._emit_boot_connection_progress(connection_step)

                    t_reader.join(timeout=2)
                    if line_buffer:
                        _handle_line(line_buffer)

                    proc.wait()
                    self._active_process = None
                    if proc.returncode != 0:
                        if not connected_to_chip:
                            self._emit_boot_connection_progress(connection_step, failed=True)
                        self.emit("console:log", {"text": f"  ✖ Hard Reset burn failed with code {proc.returncode}.", "tag": "error", "newline": True})
                        return

                    self.emit("console:log", {"text": "  ✔ Flash erased and clean bootloader/partition table written successfully.", "tag": "success", "newline": True})
                    self.emit("console:log", {"text": "  ℹ The board is in a clean, blank recovery state. Use Upload to install your sketch.", "tag": "info", "newline": True})
                    time.sleep(0.75)
                    self._trigger_actual_board_reset(port, board_name, binfo)
                    self.emit("notification", {"title": "Hard Reset Complete", "message": "ESP32 erased and bootloader restored.", "type": "success"})
                    hard_reset_success = True
                else:
                    self._trigger_actual_board_reset(port, board_name, binfo)
                    self.emit("notification", {"title": "Hard Reset Complete", "message": "Reset pulse sent to MCU.", "type": "success"})
                    hard_reset_success = True

            except Exception as e:
                self.emit("console:log", {"text": f"✖ Hard Reset error: {e}", "tag": "error", "newline": True})
            finally:
                _release_reset_cache_lock(reset_cache_lock)
                self.is_busy = False
                self.active_operation = None
                self._current_op_phase = None
                self.emit("operation:phase", {
                    "phase": "idle",
                    "is_busy": False,
                    "op": "hard_reset",
                    "success": hard_reset_success,
                })
                self.emit("window:closable", {"closable": True})
                self.emit("console:progress", {"action": "Completed"})
                if hard_reset_success or was_monitoring:
                    time.sleep(0.5)
                    self._start_serial_monitor()

        threading.Thread(target=_worker, name="MCU_HardReset", daemon=True).start()

    def soft_reset(self):
        """Perform a soft reset by compiling and uploading the clean board-specific recovery sketch."""
        if self.is_busy:
            if not self._active_process or self._active_process.poll() is not None:
                self.is_busy = False
            else:
                self.emit("console:log", {"text": "⚠ Busy — cannot perform soft reset right now.", "tag": "warning", "newline": True})
                return

        cfg = load_gui_config()
        if getattr(self, "clear_console_on_action", cfg.get("clear_console_on_action", True)):
            self.emit("console:clear", None)
        if getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)):
            self.emit("serial:clear", None)

        if not self.current_board:
            self.emit("console:log", {"text": "✖ Soft Reset error: No board selected.", "tag": "error", "newline": True})
            return
        if not self.current_port:
            self.emit("console:log", {"text": "✖ Soft Reset error: No COM port selected.", "tag": "error", "newline": True})
            return

        def _worker():
            port = self.current_port
            board_name = self.current_board
            binfo = self._resolve_board_info(board_name)

            p_platform = binfo.get("platform", "")
            is_avr = (p_platform == "atmelavr")
            is_esp = p_platform in ("espressif32", "espressif8266")

            owner_pid = port_occupied_owner(port)
            if owner_pid:
                self.emit("console:log", {
                    "text": f"  ⚠ Soft reset blocked: Port '{port}' is in use by another window (PID {owner_pid}).",
                    "tag": "warning",
                    "newline": True,
                })
                return

            reset_cache_lock = _try_acquire_reset_cache_lock()
            if reset_cache_lock is None:
                self.emit("console:log", {
                    "text": "  ⚠ Soft Reset blocked: another window is using the shared reset cache.",
                    "tag": "warning",
                    "newline": True,
                })
                return

            self.is_busy = True
            self.active_operation = "reset"
            self._active_reset_kind = "soft"
            self._current_op_phase = "resetting"
            self.emit("operation:phase", {"phase": "reset", "is_busy": True, "can_stop": False, "op": "soft_reset"})
            self.emit("window:closable", {"closable": False})
            self.emit("console:progress", {"action": "Soft resetting"})
            self.emit("console:log", {"text": "", "newline": True})
            self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
            self.emit("console:log", {
                "text": "  🔄 SOFT RESET (Arduino UNO Minimal Sketch)" if is_avr else f"  🔄 SOFT RESET ({board_name})",
                "tag": "header",
                "newline": True
            })
            self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
            self.emit("console:log", {"text": f"  Port  : {port}", "tag": "info", "newline": True})
            self.emit("console:log", {"text": f"  Board : {board_name}", "tag": "dim", "newline": True})
            if is_esp:
                self.emit("console:log", {"text": "  💡 Tip: On Desktop PCs, some ESP modules may need BOOT held during connection.", "tag": "dim", "newline": True})
            elif is_avr:
                self.emit("console:log", {"text": "  ℹ Arduino UNO uses automatic DTR reset; do not hold a BOOT button.", "tag": "dim", "newline": True})
            self.emit("console:log", {"text": "", "newline": True})

            was_monitoring = getattr(self, "_serial_thread", None) is not None
            self._stop_serial_monitor()
            time.sleep(0.4)
            ok = False
            err_msg = ""

            try:
                project_dir = self._soft_reset_project_dir(board_name, binfo)
                project_dir.mkdir(parents=True, exist_ok=True)
                hide_generated_directory(project_dir.parent.parent)
                hide_generated_directory(project_dir.parent)
                hide_generated_directory(project_dir)

                ini_content, cpp_content, monitor_speed = self._reset_project_contents(board_name, binfo)
                ini_path = project_dir / "platformio.ini"
                cpp_path = project_dir / "main.cpp"

                files_changed = False
                existing_ini = ini_path.read_text(encoding="utf-8") if ini_path.exists() else ""
                existing_cpp = cpp_path.read_text(encoding="utf-8") if cpp_path.exists() else ""

                if existing_ini != ini_content or existing_cpp != cpp_content:
                    files_changed = True
                    env_build_dir = project_dir / ".pio" / "build" / "mcu_flash"
                    is_first_time = not env_build_dir.exists()
                    ini_path.write_text(ini_content, encoding="utf-8")
                    cpp_path.write_text(cpp_content, encoding="utf-8")
                    if is_first_time:
                        self.emit("console:log", {"text": "  🔧 First-time setup for this board — this may take a minute. Subsequent Soft Resets will be instant.", "tag": "warning", "newline": True})
                    else:
                        self.emit("console:log", {"text": "  ✔ Reset project updated; its existing incremental objects were preserved.", "tag": "dim", "newline": True})
                else:
                    self.emit("console:log", {"text": "  ✔ Using cached build (no recompilation needed).", "tag": "success", "newline": True})

                pio_cmd = find_pio_executable()
                if not pio_cmd:
                    pio_cmd = ensure_platformio()
                if not pio_cmd:
                    self.emit("console:log", {"text": "✖ PlatformIO core not available for Soft Reset.", "tag": "error", "newline": True})
                    return

                jobs = self._get_jobs()
                launch_env = os.environ.copy()
                core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)
                launch_env["PLATFORMIO_CORE_DIR"] = str(core_dir)
                launch_env["TMP"] = str(core_dir / ".tmp")
                launch_env["TEMP"] = str(core_dir / ".tmp")
                launch_env["PYTHONUNBUFFERED"] = "1"
                launch_env.pop("PYTHONOPTIMIZE", None)
                launch_env["PLATFORMIO_BUILD_JOBS"] = str(jobs)
                launch_env["PLATFORMIO_RUN_JOBS"] = str(jobs)
                launch_env["SCONSFLAGS"] = f"-j{jobs}"

                ok = False
                err_msg = ""

                # Fast path check for ESP boards: if reset project files are identical,
                # skip mtime verification to avoid false recompile triggers from filesystem timestamp shifts.
                fast_bins = None
                if not files_changed and is_esp:
                    fast_bins = self._locate_soft_reset_fast_binaries(
                        project_dir, board_name, p_platform, env_name="mcu_flash",
                        skip_mtime_check=True,
                    )

                if fast_bins is not None:
                    self.emit("console:log", {"text": "  ⚡ Cached build found — flashing directly with esptool (skipping PlatformIO).", "tag": "success", "newline": True})
                    self.emit("console:progress", {"action": "Flashing recovery sketch"})
                    ok, err_msg, _attempts = self._soft_reset_esptool_write(fast_bins, port)
                elif is_esp:
                    # ESP board without valid fast_bins: compile first so COM port is never touched during compilation!
                    self.emit("console:log", {"text": f"  🔨 Compiling clean recovery sketch for {board_name}...", "tag": "info", "newline": True})
                    self.emit("console:progress", {"action": "Compiling recovery sketch"})
                    compile_cmd = pio_cmd + [
                        "run", "-e", "mcu_flash",
                        "-j", str(jobs),
                    ]
                    proc = subprocess.Popen(
                        compile_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        cwd=str(project_dir),
                        env=launch_env,
                        creationflags=(subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
                    )
                    self._active_process = proc
                    _current_tool_item = [""]
                    _logged_installs = set()
                    _logged_done = set()
                    if proc.stdout:
                        for line in iter(proc.stdout.readline, ""):
                            l = line.rstrip("\r\n")
                            if not l:
                                continue
                            low = l.lower()
                            _stripped = l.strip()

                            # Swallow promotional banners & PlatformIO upgrade ads
                            if any(kw in low for kw in (
                                "if you like platformio", "star it on github", "follow us on linkedin",
                                "try platformio ide", "please wait while upgrading", "successfully upgraded",
                                "looking for ", "check our library registry", "* cli  >", "* web  >"
                            )) or _stripped.startswith("*****") or _stripped == "*" or _stripped.startswith("https://") or _stripped.startswith("http://"):
                                continue

                            # Suppress build environment headers, dividers, and PlatformIO metadata
                            _sl = _stripped.lower()
                            if (
                                _stripped.startswith("--------------------------------------------------------------------------------")
                                or _stripped.startswith("==================================================")
                                or _sl.startswith("processing mcu_flash")
                                or _sl.startswith("platform:") or _sl.startswith("hardware:")
                                or _sl.startswith("packages:") or _sl.startswith("configuration:")
                                or _sl.startswith("sdk:") or _sl.startswith("embedded:")
                                or _sl.startswith("ram:") or _sl.startswith("flash:")
                                or _sl.startswith("debug:") or _sl.startswith("method:")
                                or _sl.startswith("ldf modes:")
                                or _sl.startswith("no dependencies")
                                or _sl.startswith("advanced memory usage")
                                or _sl.startswith("- ") and any(kw in _sl for kw in ("@", "framework-", "tool-", "toolchain-"))
                                or "building in release mode" in _sl
                                or "verbose mode can be enabled" in _sl
                                or "method = " in _sl
                            ):
                                continue

                            # Tool / Platform Manager
                            if "tool manager:" in low or "platform manager:" in low or "tool-manager:" in low or "platform-manager:" in low:
                                manager_kind = "Toolchain/Tool" if ("tool" in low) else "Platform/Framework"
                                item = re.sub(r'^(?:tool|platform)[\s-]manager:\s*', '', l, flags=re.IGNORECASE).strip()
                                item = re.sub(r'^(?:installing|downloading|unpacking)\s+', '', item, flags=re.IGNORECASE).strip()
                                item = re.split(r'\s+has been installed!?$', item, flags=re.IGNORECASE)[0].strip()
                                _current_tool_item[0] = item
                                item_label = item if len(item) <= 44 else item[:41] + "..."
                                if "installing" in low:
                                    if item not in _logged_installs:
                                        _logged_installs.add(item)
                                        self.emit("console:log", {"text": f"  🔎 Checking {manager_kind}: {item}", "tag": "info", "newline": True})
                                elif "installed" in low:
                                    if item not in _logged_done:
                                        _logged_done.add(item)
                                        full_bar = "▰" * 30
                                        self.emit("console:log", {
                                            "text": f"  ✔ Unpacking   [{item_label}]  {full_bar}  100%",
                                            "tag": "success",
                                            "replace_pattern": rf"(?:Downloading|Unpacking)\s+\[{re.escape(item_label)}\]",
                                            "newline": True
                                        })
                                        self.emit("console:log", {"text": f"  ✔ Installed {manager_kind}: {item}", "tag": "success", "newline": True})
                                continue

                            # Downloading / Unpacking progress
                            if "downloading" in low or "unpacking" in low:
                                pcts = re.findall(r'(\d+)%', l)
                                if pcts:
                                    pct = int(pcts[-1])
                                    filled = int(pct / 100.0 * 30)
                                    bar = "▰" * filled + "▱" * (30 - filled)
                                    act_name = "Downloading" if "downloading" in low else "Unpacking"
                                    item = _current_tool_item[0] or "tool-package"
                                    item_label = item if len(item) <= 44 else item[:41] + "..."
                                    icon = "✔ " if pct >= 100 else "  "
                                    progress_text = f"  {icon}{act_name:<11} [{item_label}]  {bar}  {pct:3d}%"
                                    self.emit("console:log", {
                                        "text": progress_text,
                                        "tag": "success" if pct >= 100 else "info",
                                        "replace_pattern": rf"(?:Downloading|Unpacking)\s+\[{re.escape(item_label)}\]",
                                        "newline": True
                                    })
                                continue

                            # SCons build boilerplate
                            if any(low.startswith(prefix) for prefix in (
                                "compiling ", "archiving ", "linking ", "building ", "retrieving ",
                                "checking size", "retrieved ", "converting "
                            )) or "from cache" in low or low.startswith("ldf:") or low.startswith("found ") or low.startswith("scanning dependencies"):
                                continue

                            tag = "error" if "error" in low or "failed" in low else ("success" if "success" in low else "info")
                            self.emit("console:log", {"text": f"  {l}", "tag": tag, "newline": True})

                    proc.wait()
                    self._active_process = None

                    if proc.returncode == 0:
                        self.emit("console:log", {"text": "  ✔ Recovery sketch compiled successfully.", "tag": "success", "newline": True})
                        # Pass build_dir explicitly and skip mtime check — we know compile just
                        # finished so the source files (just rewritten) will have newer mtimes
                        # than firmware.bin on some filesystems, causing a false staleness hit.
                        _env_build_dir = project_dir / ".pio" / "build" / "mcu_flash"
                        try:
                            _fw = _env_build_dir / "firmware.bin"
                            if _fw.exists():
                                os.utime(str(_fw), None)
                        except Exception:
                            pass
                        fast_bins = self._locate_soft_reset_fast_binaries(
                            project_dir, board_name, p_platform, env_name="mcu_flash",
                            build_dir=_env_build_dir, skip_mtime_check=True,
                        )
                        if fast_bins:
                            self.emit("console:progress", {"action": "Flashing recovery sketch"})
                            ok, err_msg, _attempts = self._soft_reset_esptool_write(fast_bins, port)
                        else:
                            ok = False
                            err_msg = "Could not locate compiled binaries for soft reset flash."
                            self.emit("console:log", {"text": f"  ✖ {err_msg}", "tag": "error", "newline": True})
                    else:
                        ok = False
                        err_msg = f"Compilation failed with exit code {proc.returncode}"
                        self.emit("console:log", {"text": f"  ✖ {err_msg}", "tag": "error", "newline": True})

                else:
                    # Non-ESP (e.g. AVR/Arduino UNO): run PlatformIO upload with connection retry loop
                    _MAX_CONNECT_RETRIES = 10
                    _connect_retry_count = 0
                    _CONNECT_FAIL_SIGNATURES = (
                        "wrong boot mode", "failed to connect",
                        "no serial data received", "timed out waiting for packet",
                        "device not found", "permissionerror", "access is denied",
                        "port is busy", "could not open port", "permission denied",
                        "connection timed out", "timed out after", "not responding",
                        "no more data to read from the serial port",
                        "a device attached to the system is not functioning",
                        "write timeout", "serial exception", "cannot configure port",
                        "clearcommerror", "setcommstate", "getcommstate",
                    )
                    self.emit("console:log", {"text": f"  Executing soft reset upload on {port}...", "tag": "info", "newline": True})

                    while True:
                        cmd = pio_cmd + [
                            "run", "-e", "mcu_flash", "-t", "upload",
                            "-j", str(jobs),
                            "--upload-port", port
                        ]
                        proc = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            cwd=str(project_dir),
                            env=launch_env,
                            creationflags=(subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
                        )
                        self._active_process = proc
                        output_lines = []
                        if proc.stdout:
                            for line in iter(proc.stdout.readline, ""):
                                l = line.rstrip("\r\n")
                                if not l:
                                    continue
                                output_lines.append(l)
                                low = l.lower()
                                _stripped = l.strip()

                                if any(kw in low for kw in (
                                    "if you like platformio", "star it on github", "follow us on linkedin",
                                    "try platformio ide", "please wait while upgrading", "successfully upgraded",
                                    "looking for ", "check our library registry", "* cli  >", "* web  >"
                                )) or _stripped.startswith("*****") or _stripped == "*" or _stripped.startswith("https://") or _stripped.startswith("http://"):
                                    continue

                                if (
                                    _stripped.startswith("--------------------------------------------------------------------------------")
                                    or _stripped.startswith("==================================================")
                                    or low.startswith("processing mcu_flash")
                                    or low.startswith("platform:") or low.startswith("hardware:")
                                    or low.startswith("packages:") or low.startswith("configuration:")
                                    or low.startswith("sdk:") or low.startswith("embedded:")
                                    or low.startswith("ram:") or low.startswith("flash:")
                                    or "building in release mode" in low
                                    or "verbose mode can be enabled" in low
                                    or "method = " in low
                                ):
                                    continue

                                tag = "error" if "error" in low or "failed" in low else ("success" if "success" in low else "info")
                                self.emit("console:log", {"text": f"  {l}", "tag": tag, "newline": True})

                        proc.wait()
                        self._active_process = None
                        rc = proc.returncode

                        joined = " ".join(line.rstrip().lower() for line in output_lines)
                        is_conn_failure = (rc != 0 and any(sig in joined for sig in _CONNECT_FAIL_SIGNATURES))

                        if is_conn_failure and _connect_retry_count < _MAX_CONNECT_RETRIES - 1 and not getattr(self, "_stop_requested", False):
                            if not self._is_port_present(port):
                                if not self._wait_for_port_reconnect(port):
                                    err_msg = f"MCU disconnected during soft reset ({port} is no longer available)"
                                    ok = False
                                    break
                            _connect_retry_count += 1
                            if any(x in joined for x in ("permissionerror", "access is denied", "port is busy", "could not open port", "permission denied")):
                                time.sleep(1.0)
                            self._append_connecting_progress(_connect_retry_count + 1, _MAX_CONNECT_RETRIES)
                            continue

                        ok = (rc == 0)
                        if not ok:
                            err_msg = f"Upload failed with exit code {rc}"
                        break

                if ok:
                    if p_platform in ("espressif32", "espressif8266"):
                        self._write_reset_manifest(project_dir, board_name, binfo)
                    time.sleep(0.5)
                    self._trigger_actual_board_reset(port, board_name, binfo)
                    self.emit("console:log", {"text": "✔ Soft Reset successful! Board restored to clean recovery state.", "tag": "success", "newline": True})
                    self.emit("notification", {"title": "Soft Reset Complete", "message": "Recovery sketch flashed successfully.", "type": "success"})
                else:
                    self.emit("console:log", {"text": f"✖ Soft Reset failed: {err_msg or 'Upload error'}", "tag": "error", "newline": True})
                    self.emit("notification", {"title": "Soft Reset Failed", "message": err_msg or "Soft Reset failed", "type": "error"})

            except Exception as e:
                self.emit("console:log", {"text": f"✖ Soft Reset exception: {e}", "tag": "error", "newline": True})
            finally:
                _release_reset_cache_lock(reset_cache_lock)
                self.is_busy = False
                self.active_operation = None
                self._current_op_phase = None
                self.emit("operation:phase", {
                    "phase": "idle",
                    "is_busy": False,
                    "op": "soft_reset",
                    "success": ok,
                })
                self.emit("window:closable", {"closable": True})
                self.emit("console:progress", {"action": "Completed"})
                if ok or was_monitoring:
                    time.sleep(0.5)
                    self._start_serial_monitor()
                    if ok:
                        time.sleep(0.15)
                        self.pulse_dtr_reset()

        threading.Thread(target=_worker, name="MCU_SoftReset", daemon=True).start()


    def clean_cache(self):
        """Clean intermediate build files, temporary directories, and generated configs matching LATEST-WORKING-MCU- FLASHER."""
        if self.is_busy:
            self.emit("console:log", {"text": "⚠ Busy — stop the current operation first", "tag": "warning", "newline": True})
            return

        self.is_busy = True
        self.active_operation = "clean"
        self._current_op_phase = "cleaning"
        self.emit("operation:phase", {"phase": "clean", "is_busy": True, "can_stop": False, "op": "clean"})
        self.emit("window:closable", {"closable": True})
        self.emit("console:progress", {"action": "Cleaning build cache"})

        cfg = load_gui_config()
        if getattr(self, "clear_console_on_action", cfg.get("clear_console_on_action", True)):
            self.emit("console:clear", None)

        self.emit("console:log", {"text": "🧹 Cleaning build cache...", "tag": "header", "newline": True})
        try:
            sketch = self.sketch_dir_path
            targets = [
                (get_project_build_cache_root(sketch, create=False), "MCU Flasher project build cache"),
                (sketch / ".pio", "all cached board workspaces"),
                (sketch / "src", "generated build sources"),
                (sketch / "platformio.ini", "generated PlatformIO configuration"),
                (sketch / "build_artifacts", "board-specific binary archives"),
                (sketch / ".build_artifacts", "legacy binary archives"),
                (sketch / "compiled_builds", "legacy compiled binaries"),
                (sketch / ".mcu_gui_cache.json", "legacy compile metadata"),
                (get_project_build_cache_root(sketch, create=False) / ".mcu_gui_cache.json", "compile metadata"),
                (get_project_build_cache_root(sketch, create=False) / "compile_cache.json", "compile cache"),
                (get_project_build_cache_root(sketch, create=False) / ".mcu_flash_syntax_errors.json", "syntax metadata"),
                (sketch / ".mcu_flash_syntax_errors.json", "legacy syntax metadata"),
                (sketch / ".mcu_gui_compat_cache.json", "legacy compatible-devices metadata"),
                (sketch / ".mcu_flash_tab_order.json", "legacy editor tab order"),
                (sketch / ".mcu_ai_edits", "legacy AI edit backups"),
                (sketch / "MCU-FLASHER-SRC", "legacy generated source cache"),
                (sketch / ".ai_edit_signal", "generated editor signal"),
                (sketch / ".mcu_flasher_project_hardware.json", "legacy hardware metadata"),
                (sketch / ".ai_ready_signal", "stale AI ready signal"),
                (SCRIPT_DIR / "soft_reset" / "soft_reset_project" / "boards", "Soft/Hard Reset board caches"),
                (SCRIPT_DIR / "soft_reset" / "soft_reset_project_uno" / "boards", "Arduino reset board caches"),
                (SCRIPT_DIR / "soft_reset" / "soft_reset_project" / ".pio", "Soft/Hard Reset shared legacy cache"),
                (SCRIPT_DIR / "soft_reset" / "soft_reset_project_uno" / ".pio", "Arduino shared legacy cache"),
            ]
            remote_root = self._remote_workspace_root(sketch)
            if remote_root is not None:
                targets.append((remote_root, "remote project local build workspace"))
            removed = []
            for target, label in targets:
                if target.exists():
                    try:
                        if target.is_dir():
                            robust_rmtree(target)
                        else:
                            target.unlink(missing_ok=True)
                        removed.append(label)
                    except Exception:
                        pass

            # Invalidate all in-memory caches and reset compile tracking
            self._last_source_hash = ""
            self._last_compiled_board = ""
            self.skip_compile = False
            self.emit("skip_compile:availability", False)
            _sketch_ram_cache.invalidate()

            # Recreate AGENTS.md / AI project state
            try:
                self._sync_project_hardware_state(self.sketch_dir_path)
            except Exception:
                pass

            if removed:
                self.emit("console:log", {"text": f"✔ Clean completed successfully: removed {len(removed)} cached items.", "tag": "success", "newline": True})
            else:
                self.emit("console:log", {"text": "✔ Project is already clean. Ready to rebuild from scratch.", "tag": "success", "newline": True})
            self.emit("console:log", {"text": "  Ready. Compile or Upload to rebuild from scratch.", "tag": "dim", "newline": True})
        except Exception as e:
            self.emit("console:log", {"text": f"✖ Clean error: {e}", "tag": "error", "newline": True})
        finally:
            self.is_busy = False
            self.active_operation = None
            self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False})
            self.emit("window:closable", {"closable": True})
            self.emit("console:progress", {"action": "Completed"})

    def stop_operation(self):
        """Cancel the currently running compilation or building phase safely."""
        if getattr(self, "active_operation", None) in ("flash", "reset") or getattr(self, "_current_op_phase", None) in ("flashing", "writing", "resetting", "erasing"):
            self.emit("console:log", {
                "text": "  ⚠ Stop rejected: Firmware upload / flash write is currently in progress.\n    Interrupting flash writes can permanently brick or corrupt your MCU.",
                "tag": "warning",
                "newline": True,
            })
            return

        if not self.is_busy:
            return

        self._stop_requested = True
        session_id = getattr(self, "_op_session_id", 0)
        self.emit("console:log", {"text": "⏹ Stopping compilation process...", "tag": "warning", "newline": True})

        def _kill_bg():
            self._kill_active_process_tree()
            time.sleep(4)
            if self.is_busy and getattr(self, "_op_session_id", 0) == session_id:
                try:
                    cache_root = self._effective_cache_root(self.sketch_dir_path)
                    self._clean_temporary_compile_artifacts(cache_root)
                except Exception:
                    pass
                self.is_busy = False
                self.active_operation = None
                self._current_op_phase = None
                self.emit("operation:phase", {"phase": "idle", "is_busy": False})
                self.emit("window:closable", {"closable": True})
                self.emit("console:log", {"text": "  ⚠ Busy state cleared by failsafe timer.", "tag": "warning", "newline": True})

        threading.Thread(target=_kill_bg, name="MCU_StopWorker", daemon=True).start()

    # ──────────────────────────────────────────────────────────
    # JS-RPC: SETTINGS & SYNTAX
    # ──────────────────────────────────────────────────────────
    def get_settings(self) -> dict[str, Any]:
        return load_gui_config()

    def save_settings(self, settings: dict[str, Any]):
        cfg = load_gui_config()
        cfg.update(settings)
        save_gui_config(cfg)
        if "theme_mode" in settings:
            set_theme_mode(settings["theme_mode"])

    def get_theme_mode(self) -> str:
        return get_theme_mode()

    def realtime_check_syntax(self, file_path: str, content: str) -> str:
        """Realtime syntax checking bridge for Monaco editor."""
        try:
            from src.syntax_checker import analyze_cpp_syntax
            diagnostics = analyze_cpp_syntax(content, Path(file_path))
            self.emit("syntax:errors", diagnostics)
            return json.dumps(diagnostics)
        except Exception:
            return "[]"

