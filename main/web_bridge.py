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
from uuid import uuid4
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

from src.modules.package_jobs import guarded_package_operation, package_store_lease, package_core_directory

SCRIPT_DIR = _project_root


def _iter_process_output(process, stop_requested, stop_process, on_silence,
                         *, poll_interval: float = 0.2,
                         notice_after: float = 15.0,
                         notice_every: float = 20.0):
    """Yield child output without blocking cancellation or quiet-build reporting.

    A reader thread drains the pipe into a bounded queue. The operation worker
    polls that queue, so a quiet build scan or bootloader handoff cannot trap
    the operation worker inside ``readline()`` and delay Stop or status updates.
    """
    stdout = getattr(process, "stdout", None)
    if stdout is None:
        return

    records: queue.Queue = queue.Queue(maxsize=256)
    finished = threading.Event()
    abandoned = threading.Event()
    sentinel = object()

    def _publish(record):
        while not abandoned.is_set():
            try:
                records.put(record, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _read_pipe():
        try:
            for output_line in iter(lambda: stdout.readline(8192), ""):
                if not _publish(("line", output_line)):
                    break
        except Exception as exc:
            _publish(("error", exc))
        finally:
            _publish(("eof", sentinel))
            finished.set()

    reader = threading.Thread(target=_read_pipe, name="MCU_BuildOutputReader", daemon=True)
    reader.start()
    last_output = time.monotonic()
    next_notice = last_output + max(0.1, float(notice_after))
    killed_for_stop = False
    try:
        while True:
            if stop_requested() and not killed_for_stop:
                killed_for_stop = True
                try:
                    stop_process()
                except Exception:
                    pass

            try:
                kind, value = records.get(timeout=max(0.02, float(poll_interval)))
            except queue.Empty:
                now = time.monotonic()
                if finished.is_set() and records.empty():
                    break
                if now >= next_notice:
                    try:
                        on_silence(max(1, int(now - last_output)))
                    except Exception:
                        pass
                    next_notice = now + max(1.0, float(notice_every))
                continue

            if kind == "eof" and value is sentinel:
                break
            if kind == "error":
                raise value
            last_output = time.monotonic()
            yield value
    finally:
        # Early consumer exit must not strand a reader on a full queue.
        abandoned.set()
        reader.join(timeout=0.25)


def _classify_platformio_upload_line(raw_line: str) -> tuple[str, str | None]:
    """Keep upload diagnostics/results while suppressing PlatformIO scan chatter."""
    line_clean = raw_line.rstrip("\r\n")
    low = line_clean.lower()
    outcome = re.search(r"=+\s*\[(SUCCESS|FAILED)\]\s*Took\s*([\d.]+)\s*seconds", line_clean, re.IGNORECASE)
    if outcome:
        return "outcome", outcome.group(1).upper()

    has_diagnostic = bool(re.search(
        r"\b(?:fatal\s+error|error|warning|failed|failure|exception)\b", low
    ))
    stripped = line_clean.strip()
    scan_line = stripped.lower()
    if not has_diagnostic and (
            scan_line.startswith((
                "platform:", "hardware:", "packages:", "configuration:", "sdk:", "embedded:",
                "compiling ", "archiving ", "linking ", "building ", "retrieving ",
                "checking size", "retrieved ", "converting ", "ldf:", "ldf modes:",
                "scanning dependencies", "dependency graph", "|--", "|   ",
                "processing ",
            ))
            or re.match(r"^found\s+\d+\s+compatible\s+(?:libraries|library)\b", scan_line)
        or "from cache" in low
        or re.match(r"^\s+-\s+\S+\s+@\s+", line_clean)
        or any(phrase in low for phrase in (
            "verbose mode can be enabled", "building in release mode",
            "advanced memory usage", "platformio home",
        ))
        or stripped.startswith(("---", "==="))
    ):
        return "suppress", None

    if not has_diagnostic and (
        any(phrase in low for phrase in (
            "looking for ", "check our library registry", "* cli  >", "* web  >",
            "if you like platformio", "star it on github", "follow us on linkedin",
            "try platformio ide", "please wait while upgrading", "successfully upgraded",
        ))
        or stripped.startswith("*****")
        or stripped == "*"
    ):
        return "suppress", None

    return "show", None


def _platformio_upload_write_started(raw_line: str) -> bool:
    """Conservatively recognize programmer output that means flash writes began."""
    low = raw_line.lower()
    return bool(re.search(
        r"\b(?:erasing|erase(?:\s+flash)?|writing\s+(?:at|to|flash|memory)|"
        r"programming\s+(?:flash|memory)|flashing|downloading\s+element\s+to\s+address)\b"
        r"|\bwriting\s*\||\bwill\s+be\s+erased\b",
        low,
    ))

import serial
import serial.tools.list_ports
import psutil

from main.core.constants import (
    is_application_codebase_dir, MAX_BAUD_RATE, VALID_BAUD_RATES, DEFAULT_UPLOAD_SPEED,
    SCRIPT_DIR, board_reset_capabilities, default_monitor_baud,
)
from main.core.config import (
    load_gui_config, save_gui_config, load_recent_projects, add_recent_project,
    load_recent_boards, add_recent_board, get_theme_mode,
    _try_acquire_reset_cache_lock, _release_reset_cache_lock, port_occupied_owner,
    claim_serial_port,
    _load_raw_config, _save_raw_config,
    find_project_window, focus_project_window, set_active_sketch_dir,
    get_project_remembered_board, set_project_remembered_board,
    get_reset_on_baud_change, set_reset_on_baud_change,
)
from main.core.file_utils import (
    get_sketch_files_fast, ensure_file_writable, get_project_build_cache_root,
    ensure_hidden_read_first_md, hide_generated_directory, hide_hidden_attribute, write_generated_text,
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
    _get_safe_platformio_core_dir,
)
from main.platforms import get_platform_backend
_HOST_RUNTIME = get_platform_backend()
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
        from src.modules.runtime_resources import performance_profile
        self._content_limit = 4_000_000 if performance_profile().constrained else 8_000_000
        self._content_chars = 0
        # path_str -> (st_size, st_mtime_ns, content_str)
        self._content_cache: dict[str, tuple[int, int, str]] = {}
        # path_str -> (st_size, st_mtime_ns, list_of_includes)
        self._includes_cache: dict[str, tuple[int, int, list[str]]] = {}
        # dir_str -> (hash_str, baud_rate_or_None)
        self._baud_cache: dict[str, tuple[str, Optional[str]]] = {}

    def get_content(self, path: Path, *, refresh: bool = False) -> Optional[str]:
        try:
            # Cache aliases separately; do not traverse junctions a second time
            # for every RPC after read_file has already validated the path.
            resolved = os.path.normcase(os.path.abspath(path))
            st = path.stat()
            with self._lock:
                if not refresh and resolved in self._content_cache:
                    size, mtime, content = self._content_cache[resolved]
                    if size == st.st_size and mtime == st.st_mtime_ns:
                        return content
            content = path.read_text(encoding="utf-8", errors="replace")
            with self._lock:
                self._cache_content(resolved, st.st_size, st.st_mtime_ns, content)
            return content
        except Exception:
            return None

    def set_content(self, path: Path, content: str) -> None:
        try:
            resolved = os.path.normcase(os.path.abspath(path))
            st = path.stat()
            with self._lock:
                self._cache_content(resolved, st.st_size, st.st_mtime_ns, content)
                self._includes_cache.pop(resolved, None)
        except Exception:
            pass

    def _cache_content(self, key, size, mtime, content):
        previous = self._content_cache.pop(key, None)
        if previous:
            self._content_chars -= len(previous[2])
        if len(content) > self._content_limit:
            return
        self._content_cache[key] = (size, mtime, content)
        self._content_chars += len(content)
        while self._content_chars > self._content_limit or len(self._content_cache) > 128:
            oldest = self._content_cache.pop(next(iter(self._content_cache)))
            self._content_chars -= len(oldest[2])

    def get_includes(self, path: Path, *, refresh: bool = False) -> list[str]:
        try:
            resolved = os.path.normcase(os.path.abspath(path))
            st = path.stat()
            with self._lock:
                if not refresh and resolved in self._includes_cache:
                    size, mtime, incs = self._includes_cache[resolved]
                    if size == st.st_size and mtime == st.st_mtime_ns:
                        return incs
            content = self.get_content(path, refresh=refresh) or ""
            detected: list[str] = []
            for m in re.finditer(r'#include\s*[<"]([^>"]+)[>"]', content):
                hdr = m.group(1).strip()
                if not hdr.endswith((".ino", ".c", ".cpp")):
                    detected.append(hdr)
            with self._lock:
                self._includes_cache[resolved] = (st.st_size, st.st_mtime_ns, detected)
                while len(self._includes_cache) > 128:
                    self._includes_cache.pop(next(iter(self._includes_cache)))
            return detected
        except Exception:
            return []

    def get_baud_rate(self, dir_path: Path, current_hash: str) -> tuple[bool, Optional[str]]:
        key = os.path.normcase(os.path.abspath(dir_path))
        with self._lock:
            if key in self._baud_cache:
                cached_hash, baud = self._baud_cache[key]
                if cached_hash == current_hash:
                    return True, baud
        return False, None

    def set_baud_rate(self, dir_path: Path, current_hash: str, baud: Optional[str]) -> None:
        key = os.path.normcase(os.path.abspath(dir_path))
        with self._lock:
            self._baud_cache[key] = (current_hash, baud)
            while len(self._baud_cache) > 64:
                self._baud_cache.pop(next(iter(self._baud_cache)))

    def invalidate(self, path: Optional[Path] = None) -> None:
        with self._lock:
            if path:
                resolved = os.path.normcase(os.path.abspath(path))
                previous = self._content_cache.pop(resolved, None)
                if previous:
                    self._content_chars -= len(previous[2])
                self._includes_cache.pop(resolved, None)
            else:
                self._content_cache.clear()
                self._content_chars = 0
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
        frameworks_config = config.get("board_frameworks")
        self._board_frameworks = dict(frameworks_config) if isinstance(frameworks_config, dict) else {}
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
        self._operation_worker: Optional[threading.Thread] = None
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
        self._serial_generation = 0
        from src.modules.recovery import RecoveryBudget
        self._serial_recovery_budget = RecoveryBudget()
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
        self._services_started = False
        self._services_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._catalog_refresh_running = False
        self._catalog_refresh_generation = 0
        self._catalog_refreshed_once = False
        self._skip_compile_check_lock = threading.Lock()
        self._skip_compile_check_gen = 0
        self._skip_compile_check_running = False

        # Restore project compile state & remembered board for active sketch
        self._last_synced_hardware_payload: Optional[tuple] = None
        self._hardware_state_lock = threading.Lock()
        self._hardware_state_pending = None
        self._hardware_state_running = False
        self._hardware_state_active_payload = None
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
        _sketch_ram_cache.invalidate(Path(path))
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
        with self._services_lock:
            if self._services_started:
                return
            self._services_started = True
        try:
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
            # Port enumeration belongs to the monitor worker. Catalog discovery
            # waits for first paint/editor startup to settle on slower CPUs.
            def discover():
                if not self._stop_port_monitor.wait(2.0):
                    self.refresh_board_catalog()
            threading.Thread(target=discover, name="MCU_DeferredCatalog", daemon=True).start()
            from main.core.package_activity import PackageActivityMonitor
            self._package_monitor = PackageActivityMonitor(self, self._stop_port_monitor,
                                                          getattr(self, "_package_event_root", None))
            self._package_monitor_thread = threading.Thread(target=self._package_monitor.run,
                                                           name="MCU_PackageActivity", daemon=True)
            self._package_monitor_thread.start()
        except Exception as exc:
            with self._services_lock:
                self._services_started = False
            self.emit("console:log", {"text": f"Background services could not start: {exc}",
                                      "tag": "warning", "newline": True})

    def refresh_board_catalog(self, *, include_registry=False, invalidate_parsed=False):
        """Discover installed board manifests off the GUI thread."""
        with self._catalog_lock:
            if self.is_busy or self._catalog_refresh_running or self._stop_port_monitor.is_set():
                return False
            self._catalog_refresh_running = True
            self._catalog_refresh_generation += 1
            refresh_id = self._catalog_refresh_generation

        def worker():
            try:
                from main.core.board_catalog import (
                    load_dynamic_boards, load_registry_board_catalog,
                    load_downloaded_board_usb_ids, DOWNLOADED_BOARD_USB_IDS,
                )
                seed = dict(SUPPORTED_BOARDS)
                registry = None
                warning = ""
                if include_registry:
                    warning = "Refreshed local board packs. Add or update packs in Boards & Libraries Manager."
                def publish_batch(batch):
                    if (self._stop_port_monitor.is_set()
                            or refresh_id != self._catalog_refresh_generation):
                        return False
                    if batch:
                        self.emit("boards:updated", {
                            "batch": batch, "partial": True, "refresh_id": refresh_id,
                        })
                    return True

                # Startup has already read the persisted snapshot. Discover real
                # manifests incrementally rather than returning that cache again.
                catalog = load_dynamic_boards(seed, prefer_cache=False,
                                              registry_catalog=registry, on_batch=publish_batch,
                                              invalidate_parsed=bool(invalidate_parsed))
                usb_ids = load_downloaded_board_usb_ids(catalog)
                if not self._stop_port_monitor.is_set():
                    DOWNLOADED_BOARD_USB_IDS.clear()
                    DOWNLOADED_BOARD_USB_IDS.update(usb_ids)
                    self._catalog_refreshed_once = True
                    self.emit("boards:updated", {"boards": catalog, "warning": warning,
                                                  "refresh_id": refresh_id})
            except Exception as exc:
                if not self._stop_port_monitor.is_set():
                    self.emit("boards:updated", {"error": str(exc), "refresh_id": refresh_id})
            finally:
                with self._catalog_lock:
                    self._catalog_refresh_running = False

        def coordinated_worker():
            try:
                with package_store_lease(package_core_directory(), wait=True,
                                         cancel=self._stop_port_monitor,
                                         root=getattr(self, "_package_event_root", None)):
                    worker()
            except InterruptedError:
                pass
            except Exception as exc:
                self.emit("boards:updated", {"error": str(exc), "refresh_id": refresh_id})
            finally:
                with self._catalog_lock:
                    self._catalog_refresh_running = False

        try:
            threading.Thread(target=coordinated_worker, name="MCU_BoardCatalog", daemon=True).start()
            return True
        except Exception as exc:
            with self._catalog_lock:
                self._catalog_refresh_running = False
            self.emit("boards:updated", {"error": str(exc), "refresh_id": refresh_id})
            return False

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
                self.ai_watcher.shutdown()
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
        if event_name == "notification" and isinstance(data, dict):
            data = dict(data)
            data["category"] = data.get("category") or "system"
            data["type"] = data.get("type") or "info"
            data["title"] = data.get("title") or "Notification"
            data["message"] = data.get("message") or ""
            if not data.get("id"):
                data["id"] = "notif_" + uuid4().hex
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
                    "port:selected":    bus.port_selected,
                    "board:selected":   bus.board_selected,
                    "boards:updated":   bus.board_catalog_updated,
                    "project:updated":  bus.project_updated,
                    "syntax:errors":    bus.syntax_errors,
                    "notification":     bus.notification,
                    "package:progress": getattr(bus, "package_progress", None),
                    "telemetry":        bus.telemetry,
                    "timestamp:toggled": getattr(bus, "timestamp_toggled", None),
                    "skip_compile:availability": getattr(bus, "skip_compile_availability_changed", None),
                    "window:closable": getattr(bus, "window_closable", None),
                    "compat_devices:updated": getattr(bus, "compat_devices_updated", None),
                    "compat_confirm:requested": getattr(bus, "compat_confirm_requested", None),
                }
                sig = _EVENT_TO_SIGNAL.get(event_name)
                if sig is not None:
                    if event_name in ("console:log", "serial:log") and hasattr(bus, "queue_log"):
                        bus.queue_log("console" if event_name == "console:log" else "serial", payload)
                    elif event_name == "window:closable":
                        closable_val = data.get("closable", True) if isinstance(data, dict) else bool(data)
                        sig.emit(closable_val)
                    else:
                        sig.emit(payload)
                elif event_name == "console:clear":
                    bus.clear_log_queue("console")
                    bus.console_clear.emit()
                elif event_name == "serial:clear":
                    bus.clear_log_queue("serial")
                    bus.serial_clear.emit()
            except Exception:
                pass

        # Persistent notification storage
        if event_name == "notification" and isinstance(data, dict):
            stored = None
            try:
                from src.dbs import dbs_create
                stored = dbs_create.add_notification(
                    category=data.get("category", "system"),
                    level=data.get("type", "info"),
                    title=data.get("title", "Notification"),
                    message=data.get("history_message", data["message"]),
                    details=data.get("details", {}),
                    notification_id=data["id"],
                )
            except Exception:
                pass
            if stored is None:
                if not getattr(self, "_notification_storage_failed", False):
                    self._notification_storage_failed = True
                    if bus is not None:
                        # Dispatch directly so a storage warning cannot try to
                        # persist itself and recursively generate more warnings.
                        try:
                            bus.notification.emit({
                                "category": "system", "type": "warning",
                                "id": "notif_" + uuid4().hex,
                                "title": "Activity history not saved",
                                "message": "Notifications remain visible, but could not be saved to activity history. Check that the project folder is writable.",
                            })
                        except Exception:
                            pass
            else:
                self._notification_storage_failed = False

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
        try:
            self._init_hardware()
            self._stop_port_monitor.wait(timeout=1.5)
        except Exception:
            pass  # The next monitor iteration retries a transient scan failure.
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
                                if not claim_serial_port(""):
                                    self.emit("notification", {
                                        "title": "Port settings unavailable",
                                        "message": "The device disconnected, but its saved port selection could not be cleared.",
                                        "type": "warning",
                                    })
                                self.emit("port:selected", {"port": ""})
                                self.emit("serial:status", {
                                    "connected": False,
                                    "port": "",
                                    "baud": self.current_baud,
                                })
                                self._sync_project_hardware_state()
                    # Publish after clearing disconnected selections, so the
                    # GUI cannot race cleanup with a stale combo selection.
                    self.emit("ports:updated", ports)
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
        if self._stop_port_monitor.is_set():
            return
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
        ports = list(self._last_known_ports)
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
            encoded = content.encode("utf-8")
            try:
                unchanged = p.read_bytes() == encoded
            except OSError:
                unchanged = False
            if not unchanged:
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
            write_generated_text(order_file, json.dumps(paths))
        except Exception:
            pass

    def is_compile_or_upload_active(self) -> bool:
        """Return True if compiling or uploading/flashing is actively in progress."""
        if not getattr(self, "is_busy", False):
            return False
        phase = str(getattr(self, "_current_op_phase", "") or "").lower()
        op = str(getattr(self, "active_operation", "") or "").lower()
        if phase in ("idle", "") and not op:
            return False
        return (
            phase in (
                "compile", "compiling", "resolving", "installing",
                "upload", "uploading", "flash", "flashing",
                "connecting", "writing", "erasing", "verifying", "toolchain"
            )
            or op in ("compile", "upload", "flash")
        )

    def is_upload_phase_active(self) -> bool:
        """Return True if an actual firmware compile or upload/flash phase is actively in progress."""
        return self.is_compile_or_upload_active()

    def set_upload_speed(self, speed: str | int) -> None:
        """Set the upload baud rate, capped at MAX_BAUD_RATE (921600)."""
        if self.is_upload_phase_active():
            return
        try:
            val = int(speed)
            self.upload_speed = str(min(val, MAX_BAUD_RATE))
        except (ValueError, TypeError):
            self.upload_speed = str(speed)

    def set_skip_compile(self, skip: bool) -> None:
        """Set whether upload should reuse cached build without compiling."""
        self.skip_compile = bool(skip)

    def set_timestamp_enabled(self, enabled: bool) -> bool:
        """Toggle console log timestamping."""
        previous = bool(getattr(self, "timestamp_enabled", False))
        cfg = load_gui_config()
        cfg["timestamp_enabled"] = bool(enabled)
        if not save_gui_config(cfg):
            self.emit("notification", {"title": "Settings not saved",
                      "message": "The Timestamps preference could not be saved. The previous setting was kept.",
                      "type": "warning"})
            self.emit("timestamp:toggled", previous)
            return False
        self.timestamp_enabled = bool(enabled)
        self.emit("timestamp:toggled", self.timestamp_enabled)
        return True

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
        """Find the precompiled firmware binary for the SPECIFIED board only.

        Strictly board-scoped — never returns a binary built for a different board.
        Using another board's binary would invoke the wrong upload tool (e.g. avrdude
        instead of esptool) and could brick or corrupt the target MCU.
        """
        if not self.sketch_dir_path:
            return None
        target_board = board_name or self.current_board or getattr(self, "_last_compiled_board", "")
        if not target_board:
            return None
        try:
            cache_root = self._effective_cache_root(self.sketch_dir_path)
            candidate_dirs: list[Path] = []

            # 1. Board-isolated workspace
            try:
                candidate_dirs.append(self._board_build_dir(target_board))
            except Exception:
                pass

            # 2. Primary flat build directory (<cache_root>/.pio/build/mcu_env)
            # Only valid if the last build in this workspace was indeed for target_board
            last_board = getattr(self, "_last_compiled_board", "")
            if not last_board:
                cache_file = cache_root / ".mcu_gui_cache.json"
                if not cache_file.is_file():
                    cache_file = cache_root / "compile_cache.json"
                if cache_file.is_file():
                    try:
                        cdata = json.loads(cache_file.read_text(encoding="utf-8"))
                        if isinstance(cdata, dict):
                            last_board = cdata.get("last_board") or ""
                    except Exception:
                        pass

            if not last_board or last_board == target_board:
                candidate_dirs.append(cache_root / ".pio" / "build" / "mcu_env")
                try:
                    candidate_dirs.append(cache_root / ".pio" / "build" / self._pio_env_name(target_board))
                except Exception:
                    pass

            for bdir in candidate_dirs:
                if bdir and bdir.is_dir():
                    for fname in ("firmware.bin", "firmware.hex", "firmware.uf2", "firmware.elf"):
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

            write_generated_text(cache_file, json.dumps(data, indent=2, sort_keys=True) + "\n")

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
        Matches LATEST-WORKING-MCU-FLASHER contract:
        Returns (recompile_needed: bool, reason: str).
        """
        target_board = board_name or self.current_board
        if not target_board:
            return True, "no board selected"
        if not self.sketch_dir_path:
            return True, "no sketch folder loaded"

        try:
            # 1. Check if firmware binary exists for THIS board specifically
            if not self._has_prior_build(target_board):
                return True, "no firmware binary found for this board (build folder may have been cleaned)"

            # 2. Retrieve per-board cached hash — reject cross-board hits explicitly
            board_key = self._board_cache_key(target_board)
            cache_file = self._effective_cache_root(self.sketch_dir_path) / ".mcu_gui_cache.json"
            if not cache_file.is_file():
                alt = self._effective_cache_root(self.sketch_dir_path) / "compile_cache.json"
                if alt.is_file():
                    cache_file = alt

            cached_hash = ""
            if cache_file.is_file():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    boards = data.get("boards", {})
                    entry = boards.get(board_key) or boards.get(target_board)
                    if entry and isinstance(entry, dict):
                        cached_hash = str(entry.get("source_hash", ""))
                    elif not cached_hash:
                        # Legacy flat format — only accept if board matches
                        if data.get("last_board") == target_board:
                            cached_hash = str(data.get("last_source_hash", ""))
                except Exception:
                    pass

            if not cached_hash:
                return True, "no previous compile cache for this board"

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
        """Evaluate whether skip compile is possible and notify the UI.

        A single worker coalesces requests through a generation counter.
        Older results cannot overwrite the latest source/target state, and
        rapid editor changes cannot create unbounded fingerprint threads.
        """
        with self._skip_compile_check_lock:
            self._skip_compile_check_gen += 1
            if self._skip_compile_check_running:
                return
            self._skip_compile_check_running = True

        def _bg() -> None:
            while not self._stop_port_monitor.is_set():
                with self._skip_compile_check_lock:
                    my_gen = self._skip_compile_check_gen
                try:
                    available = self.check_can_skip_compile()
                except Exception:
                    available = False
                with self._skip_compile_check_lock:
                    if my_gen != self._skip_compile_check_gen:
                        continue  # Coalesce edits into one latest pending check.
                    self._skip_compile_check_running = False
                self.emit("skip_compile:availability", available)
                return
            with self._skip_compile_check_lock:
                self._skip_compile_check_running = False

        try:
            threading.Thread(target=_bg, name="MCU_SkipCompileCheck", daemon=True).start()
        except Exception as exc:
            with self._skip_compile_check_lock:
                self._skip_compile_check_running = False
            self.emit("skip_compile:availability", False)
            self.emit("console:log", {"text": f"Cached-build check could not start: {exc}",
                                      "tag": "warning", "newline": True})

    def check_can_skip_compile_for_upload(self, board_name: str | None = None) -> bool:
        """Synchronously check whether upload can skip compilation and flash directly.

        Auto-skips recompile whenever:
          • A prior firmware binary exists for this board, AND
          • Source files have not changed since the last compile.

        A checked Skip Compile option never bypasses source/target validation.
        Missing or changed fingerprints require a fresh build.
        """
        target_board = board_name or self.current_board
        if not target_board or not self.sketch_dir_path:
            return False

        try:
            # If no firmware binary at all → must compile
            if not self._has_prior_build(target_board):
                return False

            # For ESP platforms, also verify the fast-upload binaries exist
            binfo = self._resolve_board_info(target_board)
            platform = str(binfo.get("platform", "")).lower()
            if platform in ("espressif32", "espressif8266"):
                fast_bins = self._locate_soft_reset_fast_binaries(
                    self.sketch_dir_path, target_board, platform,
                    skip_mtime_check=False
                )
                if fast_bins is None:
                    return False

            # Sources unchanged → safe to skip
            needs_recomp, _ = self._needs_recompile(target_board)
            if not needs_recomp:
                return True

            return False
        except Exception:
            return False

    def _clean_temporary_compile_artifacts(self, cache_root: Path) -> None:
        """Remove partial binaries, lock files, and temp files left behind by an interrupted compile.
        Ensures the project remains 100% recompilable and rebuildable.
        """
        try:
            env_dir = cache_root / ".pio" / "build" / "mcu_env"
            if env_dir.is_dir():
                for fname in ("firmware.bin", "firmware.hex", "firmware.uf2", "firmware.elf", "firmware.map"):
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
                import signal
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGTERM)
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

    def _board_build_dir(self, board_name: str | None = None) -> Path:
        """Return the isolated .pio build directory for the given board.

        Each board compiles into its own workspace::

            <cache_root>/boards/<board_key>/build/<env_name>/

        This is the canonical location used for binary lookup, upload, and
        cache validation.  Never use a shared or cross-board path.
        """
        if not self.sketch_dir_path:
            raise RuntimeError("No sketch loaded")
        target = board_name or self.current_board or ""
        cache_root = self._effective_cache_root(self.sketch_dir_path)
        return (
            cache_root / "boards" / self._board_cache_key(target)
            / ".pio" / "build" / self._pio_env_name(target)
        )

    def _has_prior_build(self, board_name: str | None = None) -> bool:
        """Return True only if a compiled firmware binary exists for the target board.

        Checks the actual flat build output path that _compile_worker writes to:
            <cache_root>/.pio/build/mcu_env/firmware.*

        Also falls back to the board-isolated workspace path for legacy cache
        compatibility.  Cross-board binaries are never accepted — using the wrong
        board's firmware would invoke the wrong upload tool and could brick the MCU.
        """
        try:
            if not self.sketch_dir_path:
                return False
            target = board_name or self.current_board or ""
            cache_root = self._effective_cache_root(self.sketch_dir_path)

            # Primary location: where _compile_worker actually writes via platformio.ini [env:mcu_env]
            primary_build = cache_root / ".pio" / "build" / "mcu_env"
            if primary_build.is_dir() and any(
                (primary_build / fname).is_file()
                for fname in ("firmware.bin", "firmware.hex", "firmware.uf2", "firmware.elf")
            ):
                return True

            # Fallback: board-isolated workspace (used by direct esptool upload path)
            if target:
                try:
                    isolated_dir = self._board_build_dir(target)
                    if isolated_dir.is_dir() and any(
                        (isolated_dir / fname).is_file()
                        for fname in ("firmware.bin", "firmware.hex", "firmware.uf2", "firmware.elf")
                    ):
                        return True
                except Exception:
                    pass

            return False
        except Exception:
            return False

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
            # pyrefly: ignore [missing-import]
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
                    # pyrefly: ignore [missing-import]
                    from esptool.cmds import detect_flash_size, attach_flash
                    attach_flash(esp_device)
                    detected = detect_flash_size(esp_device)
                    if detected:
                        flash_str = detected
                except Exception:
                    pass

                if flash_str == "N/A":
                    try:
                        # pyrefly: ignore [missing-import]
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
            write_generated_text(path, json.dumps(payload, indent=2))
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

        # Claim before scaffolding, scans or metadata writes. A simultaneous
        # opener must lose this claim without touching the project's files.
        if not set_active_sketch_dir(str(p), hwnd=getattr(self, "_hwnd", 0)):
            owner = find_project_window(p, exclude_self=True)
            if owner:
                focus_project_window(owner.get("hwnd", 0), owner.get("pid", 0))
            return {"success": False, "already_open": bool(owner),
                    "error": "Project is already open in another window." if owner else
                             "Could not register the project. Check that your settings folder is writable."}

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

    def open_project_window(self, folder_path: str) -> dict[str, Any]:
        """Explicitly open another sketch without changing this window's state."""
        target = Path(folder_path).resolve()
        folder = target.parent if target.is_file() else target
        if not folder.is_dir() or is_application_codebase_dir(folder):
            return {"success": False, "error": "Select an existing sketch folder outside the application."}
        owner = find_project_window(folder)
        if owner:
            focused = focus_project_window(owner.get("hwnd", 0), owner.get("pid", 0))
            return {"success": focused, "already_open": True,
                    "owner_pid": owner.get("pid", 0), "owner_hwnd": owner.get("hwnd", 0),
                    "error": "The project is already open. Select its window from your desktop."}
        try:
            from src.modules.private_python_guard import is_running_private_python
            if not is_running_private_python():
                return {"success": False, "error": "Open projects using the application's private runtime."}
            interpreter = Path(sys.executable)
            if sys.platform == "win32" and (interpreter.parent / "pythonw.exe").is_file():
                interpreter = interpreter.parent / "pythonw.exe"
            args = [str(interpreter), "-B", str(SCRIPT_DIR / "mcu_flash_gui.py"),
                    "--new-window", "--project", str(target)]
            options = {"cwd": str(SCRIPT_DIR), "stdin": subprocess.DEVNULL,
                       "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if sys.platform == "win32":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            child = subprocess.Popen(args, **options)
            # Retain handles for nonblocking early-exit reporting, never replay.
            pending = getattr(self, "_project_window_processes", [])
            self._project_window_processes = [p for p in pending if p.poll() is None] + [child]
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2000, self._check_project_window_startup)
            return {"success": True, "pid": child.pid, "opened_new_window": True}
        except (OSError, ValueError) as exc:
            return {"success": False, "error": f"Could not open project window: {exc}"}

    def _check_project_window_startup(self) -> None:
        pending = []
        for child in getattr(self, "_project_window_processes", []):
            code = child.poll()
            if code is None:
                pending.append(child)
            elif code != 0:
                self.emit("notification", {"title": "Project window failed to open",
                          "message": f"The new window exited with code {code}. Check logs/gui_crash.log.",
                          "type": "error"})
        self._project_window_processes = pending

    def open_project_picker(self) -> str:
        """Open native folder browser dialog."""
        if self.is_busy or getattr(self, "active_operation", None) is not None:
            return ""
        if self._window and hasattr(self._window, "create_file_dialog"):
            try:
                import webview
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
                import webview
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
        open_in_new_window: bool = False,
    ) -> dict[str, Any]:
        """Create a new sketch folder with full scaffold (matching old ProjectSelectorDialog)."""
        if not open_in_new_window and (self.is_busy or getattr(self, "active_operation", None) is not None):
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
            # A new-project action must never overwrite an existing sketch.
            target_dir.mkdir(parents=True, exist_ok=False)
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

            return (self.open_project_window(str(target_dir)) if open_in_new_window
                    else self.open_project(str(target_dir)))
        except FileExistsError:
            return {"success": False, "error": "That project folder already exists. Open it as an existing project."}
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
        """Open the active sketch directory in the native file manager."""
        if sys.platform.startswith("linux") and self.sketch_dir_path.exists():
            try:
                subprocess.Popen(["xdg-open", str(self.sketch_dir_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as exc:
                self.emit("notification", {"title": "Could not open folder", "message": str(exc), "type": "error"})
            return
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
        if not sketch_dir:
            return

        try:
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
                str(sketch_dir),
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

            state_data = {
                "project_name": Path(sketch_dir).name,
                "project_path": str(sketch_dir),
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

            payload_text = json.dumps(state_data, indent=2, ensure_ascii=False)
            self._queue_hardware_state_write(sketch_dir, state_payload, payload_text)
        except Exception as exc:
            self.emit("console:log", {"text": f"Project connection state could not be saved: {exc}",
                                      "tag": "warning", "newline": True})

    def _queue_hardware_state_write(self, project, state_payload, payload_text) -> None:
        """Serialize metadata writes, retaining only the latest pending state.

        A slow USB drive must not turn rapid target/baud clicks into overlapping
        writers that finish out of order. Deduplicate only confirmed writes;
        a failed write stays eligible for the next explicit state update.
        """
        with self._hardware_state_lock:
            pending = self._hardware_state_pending
            if (pending is not None and pending[1] == state_payload
                    or pending is None and self._hardware_state_active_payload == state_payload):
                return
            if (not self._hardware_state_running
                    and self._last_synced_hardware_payload == state_payload):
                return
            self._hardware_state_pending = (Path(project), state_payload, payload_text)
            if self._hardware_state_running:
                return
            self._hardware_state_running = True

        def worker():
            while True:
                with self._hardware_state_lock:
                    item = self._hardware_state_pending
                    self._hardware_state_pending = None
                    if item is None:
                        self._hardware_state_running = False
                        self._hardware_state_active_payload = None
                        return
                    self._hardware_state_active_payload = item[1]
                target, payload, text = item
                try:
                    if not target.is_dir():
                        raise OSError("The sketch folder is no longer available.")
                    cache_dir = get_project_build_cache_root(target)
                    write_generated_text(cache_dir / "project_state.json", text)
                    # Cosmetic reconciliation is independent from durability:
                    # its failure must not cause the saved state to be replayed.
                    with self._hardware_state_lock:
                        self._last_synced_hardware_payload = payload
                    ensure_hidden_read_first_md(target)
                    hide_internal_project_metadata(target)
                except Exception as exc:
                    self.emit("console:log", {"text": f"Project connection state could not be saved: {exc}",
                                              "tag": "warning", "newline": True})

        try:
            threading.Thread(target=worker, name="MCU_SyncHardwareState", daemon=True).start()
        except Exception as exc:
            with self._hardware_state_lock:
                self._hardware_state_running = False
                self._hardware_state_active_payload = None
                self._hardware_state_pending = None
            self.emit("console:log", {"text": f"Project connection state writer could not start: {exc}",
                                      "tag": "warning", "newline": True})

    def select_port(self, port: str):
        """Select COM port and update serial monitor."""
        if self.is_compile_or_upload_active():
            self.emit("notification", {
                "title": "Port locked",
                "message": "Serial port cannot be changed while compiling or uploading firmware.",
                "type": "warning",
            })
            return False
        if not claim_serial_port(port):
            owner = port_occupied_owner(port)
            self.emit("notification", {"title": "Serial port unavailable",
                      "message": f"{port} is selected in another project window (PID {owner})." if owner
                                 else "Could not save the serial port selection. Check your settings folder.",
                      "type": "warning"})
            return False
        self.current_port = port
        self.emit("port:selected", {"port": port})
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
        return True

    def select_board(self, board_name: str):
        """Select microcontroller board model for the active session."""
        if self.is_compile_or_upload_active():
            self.emit("notification", {
                "title": "Board locked",
                "message": "Board selection cannot be changed while compiling or uploading firmware.",
                "type": "warning",
            })
            return False
        self.current_board = board_name
        self.emit("board:selected", {"board_name": board_name})
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
        self.update_skip_compile_availability()
        self._sync_project_hardware_state()

    def get_recent_boards(self) -> list[str]:
        """Return recently selected MCU boards (max 5)."""
        return load_recent_boards()

    def set_board_framework(self, board_name, framework):
        if self.is_upload_phase_active():
            return False
        info = SUPPORTED_BOARDS.get(board_name, {})
        allowed = info.get("frameworks") or [info.get("framework", "")]
        if framework not in allowed or not framework:
            return False
        self._board_frameworks[board_name] = framework
        config = load_gui_config()
        config["board_frameworks"] = dict(self._board_frameworks)
        save_gui_config(config)
        return True

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
    def _start_serial_monitor(self, _expected_generation=None):
        """Open the COM port and stream data in a background thread."""
        with self._serial_lock:
            if _expected_generation is not None and _expected_generation != self._serial_generation:
                return
            self._serial_generation += 1
            generation = self._serial_generation
            self.serial_running = False
            if self._serial_conn:
                try:
                    self._serial_conn.close()
                except Exception:
                    pass
                self._serial_conn = None

            if not self.current_port or (self.is_busy and self.active_operation != "compile"):
                self.emit("serial:status", {
                    "connected": False,
                    "state": "disconnected",
                    "port": self.current_port or "",
                    "baud": self.current_baud,
                })
                return

            port, baud = self.current_port, self.current_baud

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
                if self._serial_conn:
                    try:
                        self._serial_conn.close()
                    except serial.SerialException:
                        pass
                    self._serial_conn = None
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
                self._schedule_serial_recovery(generation, port, e)
                return

            conn = self._serial_conn

        def _reader():
            buf = bytearray()
            error = None
            while generation == self._serial_generation and conn and conn.is_open:
                try:
                    raw = conn.read(min(conn.in_waiting or 1, 8192))
                    if generation != self._serial_generation:
                        return
                    if not raw:
                        continue
                    buf.extend(raw)
                    if b"\n" in buf:
                        lines = buf.split(b"\n")
                        buf = lines[-1]
                        decoded_batch = [
                            l.decode("utf-8", errors="replace").rstrip("\r")
                            for l in lines[:-1]
                        ]
                        if len(decoded_batch) > 1:
                            for chunk_start in range(0, len(decoded_batch), 40):
                                chunk = decoded_batch[chunk_start:chunk_start + 40]
                                self.emit("serial:log", {"lines": chunk, "newline": True})
                        elif len(decoded_batch) == 1:
                            self.emit("serial:log", {"text": decoded_batch[0], "newline": True})
                    if len(buf) > 8192:
                        # Streams without newline must not grow without bounds.
                        self.emit("serial:log", {"text": buf.decode("utf-8", errors="replace"), "newline": False})
                        buf.clear()
                except Exception as exc:
                    error = exc
                    break
            with self._serial_lock:
                if generation != self._serial_generation:
                    return  # An old reader never changes a newer connection.
                self.serial_running = False
                if self._serial_conn is conn:
                    self._serial_conn = None
                try:
                    conn.close()
                except serial.SerialException:
                    pass
                self.emit("serial:status", {
                    "connected": False, "state": "disconnected", "port": port, "baud": baud,
                })
                if error:
                    self._schedule_serial_recovery(generation, port, error)

        self._serial_thread = threading.Thread(
            target=_reader, name="MCU_SerialReader", daemon=True
        )
        self._serial_thread.start()

    def _schedule_serial_recovery(self, generation, port, error):
        """Retry opening the selected port only; never reset or replay writes."""
        from src.modules.platform_runtime import serial_access_hint
        hint = serial_access_hint(port)
        if "Access denied" in hint or "permission" in str(error).lower() or "access is denied" in str(error).lower():
            delay = None
        else:
            delay = self._serial_recovery_budget.next_delay()
        if delay is None:
            self.emit("serial:log", {"text": f"Serial recovery stopped. {hint}", "tag": "warning", "newline": True})
            return
        self.emit("serial:log", {"text": f"Serial connection interrupted: {error}. Retrying in {delay:g}s.", "tag": "warning", "newline": True})

        def reconnect():
            if self._stop_port_monitor.is_set() or self.current_port != port:
                return
            if self.is_busy and self.active_operation != "compile":
                return
            self._start_serial_monitor(_expected_generation=generation)

        timer = threading.Timer(delay, reconnect)
        timer.daemon = True
        timer.start()

    def _stop_serial_monitor(self):
        """Stop and close the serial monitor before upload/flash."""
        old_thread = None
        with self._serial_lock:
            self._serial_generation += 1
            self.serial_running = False
            if self._serial_conn:
                try:
                    self._serial_conn.dtr = False
                    self._serial_conn.rts = False
                    self._serial_conn.close()
                except Exception:
                    pass
                self._serial_conn = None
            old_thread = getattr(self, "_serial_thread", None)
            self._serial_thread = None
            self.emit("serial:status", {
                "connected": False,
                "state": "disconnected",
                "port": self.current_port,
                "baud": self.current_baud,
            })
        if old_thread and old_thread.is_alive() and old_thread != threading.current_thread():
            try:
                old_thread.join(timeout=0.5)
            except Exception:
                pass

    def pulse_dtr_reset(self) -> None:
        """Issue a brief, silent reset pulse on the live serial connection.

        This reboots the MCU immediately after an upload completes, so the
        sketch starts running and boot logs appear in the Serial Monitor without
        Safe to call at any time: silently no-ops if the serial port is not
        open or if a destructive operation (upload / flash / reset) is active.
        """
        now = time.monotonic()
        previous_pulse_time = getattr(self, "_last_dtr_pulse_time", 0.0)
        if now - previous_pulse_time < 0.6:
            return
        self._last_dtr_pulse_time = now
        requested_board = self.current_board
        requested_port = self.current_port
        requested_generation = self._serial_generation

        def target_unchanged():
            return (self.current_board == requested_board
                    and self.current_port == requested_port
                    and self._serial_generation == requested_generation)

        def _pulse():
            # Don't interfere with an ongoing operation.
            if not target_unchanged() or (self.is_busy and self.active_operation != "compile"):
                return

            # Wait up to 2.0s for the serial monitor to finish connecting if it was just restarted
            conn = None
            for _ in range(20):
                if not target_unchanged() or (self.is_busy and self.active_operation != "compile"):
                    return
                with self._serial_lock:
                    if self._serial_conn and self._serial_conn.is_open:
                        conn = self._serial_conn
                        break
                time.sleep(0.1)

            if conn is None:
                return

            try:
                if not target_unchanged() or self._serial_conn is not conn:
                    return
                binfo = self._resolve_board_info(requested_board)
                platform = str(binfo.get("platform", "")).lower()
                if platform not in {"atmelavr", "espressif32", "espressif8266", "ststm32", "raspberrypi", "ch32v", "samd"}:
                    return  # No reset sequence is declared for this target.
                is_uno = ("avr" in platform)
                is_arm = platform in ("ststm32", "raspberrypi", "ch32v", "samd")

                # Resolve outside the lock, then revalidate after that work.
                # Keep the brief waveform under the same lock used to stop/
                # replace the monitor, so upload cannot acquire this port until
                # an already-started pulse has released both control lines.
                with self._serial_lock:
                    if (not target_unchanged() or self._serial_conn is not conn
                            or not conn.is_open
                            or (self.is_busy and self.active_operation != "compile")):
                        return
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

        try:
            threading.Thread(target=_pulse, name="MCU_SilentDTR", daemon=True).start()
        except Exception as exc:
            if self._last_dtr_pulse_time == now:
                self._last_dtr_pulse_time = previous_pulse_time
            self.emit("serial:log", {"text": f"Reset pulse could not start: {exc}",
                                     "tag": "warning", "newline": True})

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
        if self.is_busy or self.active_operation:
            return

        if self._block_if_pending_ai_edits("Compile"):
            return

        cfg = load_gui_config()
        if getattr(self, "clear_console_on_action", cfg.get("clear_console_on_action", True)):
            self.emit("console:clear", None)
        if getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)):
            self.emit("serial:clear", None)

        if not self.current_board:
            self.emit("console:log", {"text": "✖ Compile error: No board selected. Please select a board first.", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Compile Failed", "message": "No board selected.", "type": "warning"})
            return
        self.is_busy = True
        self._stop_requested = False
        self.active_operation = "compile"
        self._current_op_phase = "resolving"
        self.emit("operation:phase", {"phase": "compile", "is_busy": True, "can_stop": False, "op": "compile"})
        self.emit("window:closable", {"closable": True})

        try:
            self._operation_worker = threading.Thread(
                target=self._compile_requested_worker, name="MCU_Compile", daemon=True
            )
            self._operation_worker.start()
        except Exception as exc:
            self._operation_worker = None
            self.emit("console:log", {"text": f"Compile worker could not start: {exc}",
                                      "tag": "error", "newline": True})
            self._release_requested_operation()

    def _release_requested_operation(self):
        self.is_busy = False
        self.active_operation = self._current_op_phase = None
        self.emit("operation:phase", {"phase": "idle", "is_busy": False})
        self.emit("window:closable", {"closable": True})

    def _resolve_requested_target(self, action: str) -> bool:
        """Repair an unresolved cached selection off the GUI thread before rejecting it."""
        from main.core.board_catalog import (
            _load_platformio_board_catalog, resolve_board_definition,
            load_registry_board_catalog, _save_board_catalog_cache,
        )
        name = self.current_board
        info = self._resolve_board_info(name)
        if info:
            needs_resolution = info.get("pio_resolved") is False or not info.get("board")
            if needs_resolution:
                self.emit("console:log", {"text": f"  Resolving board definition for {name}…", "tag": "info", "newline": True})
            resolved = resolve_board_definition(name, info, _load_platformio_board_catalog())
            fallback = resolved.get("backend") == "arduino-cli"
            if not fallback and (resolved.get("pio_resolved") is False or not resolved.get("board")):
                from src.modules.offline_runtime import bootstrap_instruction
                self.emit("console:log", {"text": bootstrap_instruction(f"Offline board definition unavailable: {name}"),
                                          "tag": "error", "newline": True})
            if (fallback or (resolved.get("pio_resolved") and resolved.get("board"))) and resolved != info:
                SUPPORTED_BOARDS.set_definition(name, resolved)
                _revision, catalog = SUPPORTED_BOARDS.snapshot()
                _save_board_catalog_cache(catalog)
                self.emit("boards:updated", {"boards": catalog})
                target = str(resolved.get("arduino_fqbn")) if fallback else f"{resolved['platform']}:{resolved['board']}"
                self.emit("console:log", {"text": f"  Target: {target} ({self._resolve_board_info(name).get('framework', '')})",
                                          "tag": "success", "newline": True})
        return self._check_target(action, sketch=True)

    @guarded_package_operation
    def _compile_requested_worker(self):
        try:
            if not self._resolve_requested_target("Compile"):
                self._release_requested_operation()
                return False
            if self._resolve_board_info().get("backend") == "arduino-cli":
                from main.core.arduino_backend import run_arduino_operation
                return run_arduino_operation(self)
            return self._compile_worker(False)
        except Exception as exc:
            self.emit("console:log", {"text": f"Build preparation failed: {exc}", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Build failed", "message": str(exc), "type": "error"})
            self._release_requested_operation()
            return False

    @staticmethod
    def _convert_sketch_inos_to_cpp(primary_ino: Path, ino_files: list[Path]) -> str:
        """Convert Arduino .ino sketch files into a unified .cpp unit with Arduino.h and prototypes."""
        main_name = primary_ino.name
        c = None
        try:
            # pyrefly: ignore [missing-import]
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
        """Keep preparation failures observable and release the operation safely."""
        try:
            return self._compile_worker_impl(is_upload, is_clean_retry)
        except Exception as exc:
            self.emit("console:log", {"text": f"Build preparation failed: {exc}", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Build failed", "message": str(exc), "type": "error"})
            process = getattr(self, "_active_process", None)
            if process is not None and process.poll() is None:
                self._kill_active_process_tree()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=2)
                    except Exception:
                        pass
                except Exception:
                    pass
            self._active_process = None
            self.is_busy = False
            self.active_operation = self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False})
            self.emit("window:closable", {"closable": True})
            self._unmap_unc_after_build()
            return False
        finally:
            process = getattr(self, "_active_process", None)
            if process is None or process.poll() is not None:
                self._framework_download_active = False

    def _compile_worker_impl(self, is_upload: bool = False, is_clean_retry: bool = False) -> bool:
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

        if is_upload:
            self.emit("console:log", {
                "text": "  🔄 Sources changed or no prior build — compiling firmware before upload.",
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

        # Check the actual env build directory — not just the .pio root — so the
        # "Incremental build" message is only shown when compiled objects really exist.
        _env_build_dir = cache_root / ".pio" / "build" / "mcu_env"
        is_fresh_board = not _env_build_dir.is_dir() or not any(
            (_env_build_dir / fname).is_file()
            for fname in ("firmware.bin", "firmware.hex", "firmware.uf2", "firmware.elf")
        )
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
                self.active_operation = self._current_op_phase = None
                self.emit("operation:phase", {"phase": "idle", "is_busy": False})
                return False

            binfo = self._resolve_board_info(self.current_board)
            platform_name = str(binfo.get("platform", "")).strip()
            board_id = str(binfo.get("board", "")).strip()
            framework = str(binfo.get("framework", "arduino")).strip() or "arduino"
            if not board_toolchain_ready(core_dir, platform_name, board_id, framework):
                from src.modules.offline_runtime import bootstrap_instruction
                raise RuntimeError(bootstrap_instruction(f"Board pack unavailable: {platform_name}:{board_id} ({framework})"))

            # Pre-create build directories to avoid SCons dbm/dblite FileNotFoundError
            (cache_root / ".pio" / "build" / "mcu_env").mkdir(parents=True, exist_ok=True)
            (cache_root / ".pio" / "libdeps" / "mcu_env").mkdir(parents=True, exist_ok=True)

            self._generate_platformio_ini(cache_root)

            entry_ok, entry_owners = self._validate_entry_points() if "arduino" in framework.lower().split(",") else (True, "")
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
                self._stage_root_source_file(src_path, dst_path)

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
            jobs = self._get_jobs()

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
            self.emit("console:log", {"text": "  ⚙ Starting PlatformIO build process...", "tag": "purple", "newline": True})

            cmd = pio_cmd + ["run", "-j", str(jobs)]

            # Configure high-performance SCons and PlatformIO environment variables matching LATEST-WORKING-MCU- FLASHER
            launch_env = os.environ.copy()
            core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)
            # Pre-verify SCons build engine so PlatformIO doesn't
            # re-download it mid-compile (avoids scary console noise).
            if not ensure_scons_ready(core_dir):
                from src.modules.offline_runtime import bootstrap_instruction
                raise RuntimeError(bootstrap_instruction("PlatformIO tool-scons is missing"))
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

            self._active_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                cwd=str(cache_root),
                env=launch_env,
                **_HOST_RUNTIME.process_options(priority=True, session=True),
            )

            output_lines: list[str] = []
            _dependency_graph_active = [False]
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
            # Match complete PlatformIO records, never words in diagnostic or
            # source excerpts (for example, a function named "building").
            _build_progress_pattern = re.compile(
                r"^(?P<action>Compiling|Archiving|Linking|Building|Generating partitions|Checking size|"
                r"Retrieving maximum program size)\s+(?P<target>.+)$",
                re.IGNORECASE,
            )
            _build_artifact_suffixes = {
                "compiling": (".o", ".obj"),
                "archiving": (".a", ".lib"),
                "linking": (".elf", ".axf", ".out", ".exe"),
                "building": (".bin", ".hex", ".uf2", ".elf"),
                "generating partitions": (".bin",),
                "checking size": (".elf", ".axf", ".out", ".exe"),
                "retrieving maximum program size": (".elf", ".axf", ".out", ".exe"),
            }
            _build_metadata_pattern = re.compile(
                r"(?:Configuration:\s+https?://docs\.platformio\.org/(?:page/)?boards/\S+|"
                r"Platform:\s+.+\([^)]+\)\s*>\s*\S.+|"
                r"Hardware:\s+.*\d+(?:[.,]\d+)?MHz(?:\s*[, ].*)?|Packages?:\s*|"
                r"Debug:\s+Current\s+\([^)]+\)(?:\s+.*)?|"
                r"(?:RAM|Flash):\s*\[[=\s]*\]\s*[\d.]+%\s+"
                r"\(used\s+\d+\s+bytes\s+from\s+\d+\s+bytes\))",
                re.IGNORECASE,
            )
            _build_summary_pattern = re.compile(
                r"^=+\s*\[(?:SUCCESS|FAILED)\]\s+Took\b.*=+\s*$",
                re.IGNORECASE,
            )
            _build_promotion_pattern = re.compile(
                r"^(?:Looking for\s+.*\blibrar(?:y|ies)\b|Check our library registry|"
                r"\*\s+(?:CLI|WEB)\s*>|If you like PlatformIO|Star it on GitHub|"
                r"Follow us on LinkedIn|Try PlatformIO IDE|Please wait while upgrading|"
                r"Successfully upgraded)(?=\W|$)",
                re.IGNORECASE,
            )
            # Keep the familiar phase/file log concise. These are complete,
            # routine PlatformIO records, never substring matches in source,
            # diagnostic context or custom builder output.
            _build_routine_pattern = re.compile(
                r"(?:Verbose mode can be enabled via `-v, --verbose` option|"
                r"[ \t]*-[ \t]+(?:framework|toolchain|tool|platform)-[\w.+-]+[ \t]+@[ \t]+"
                r"\S+(?:[ \t]+\([^()\r\n]+\))?[ \t]*|"
                r"LDF: Library Dependency Finder -> https://bit\.ly/configure-pio-ldf|"
                r"LDF Modes: Finder ~ [\w+]+, Compatibility ~ [\w+]+|"
                r"Found \d+ compatible libraries|Scanning dependencies\.{3}|No dependencies|"
                r'Advanced Memory Usage is available via "PlatformIO Home > Project Inspect"|'
                r"esptool(?:\.py)? v\d+(?:\.\d+)+(?:-[\w.-]+)?|"
                r"Creating (?:esp32[\w-]*|esp8266) image\.{3}|"
                r"Merged \d+ ELF sections?|"
                r"Successfully created (?:esp32[\w-]*|esp8266) image\.?)",
                re.IGNORECASE,
            )

            def _match_build_progress(raw_line: str) -> tuple[str, str] | None:
                match = _build_progress_pattern.match(raw_line)
                if match is None:
                    return None
                action = match.group("action").lower()
                target = match.group("target")
                if action == "building":
                    target = re.sub(r'\s+with\s+action:?.*$', '', target, flags=re.IGNORECASE)
                quoted = target.startswith('"') and target.endswith('"')
                target = (target[1:-1] if quoted else target).replace("\\", "/")
                filename = target.rsplit("/", 1)[-1]
                if not any(filename.lower().endswith(suffix) and len(filename) > len(suffix)
                           for suffix in _build_artifact_suffixes[action]):
                    return None
                # Relative artifact names have no spaces unless quoted. Native
                # absolute paths and .pio/build paths may contain user folders
                # with spaces. A sentence ending in "target .o" is not a path.
                if not (quoted or re.match(r'^(?:[a-z]:/|/|(?:\./)?\.pio/build/)', target, re.IGNORECASE)
                        or not re.search(r'\s', target)):
                    return None
                return action, target

            def _ensure_post_deps_divider() -> None:
                if _deps_content_printed[0] and not _second_divider_printed[0]:
                    _second_divider_printed[0] = True
                    self.emit("console:log", {"text": "  ──────────────────────────────────────────────────", "tag": "purple_dim", "newline": True})

            def _diagnostic_location(raw_path: str) -> str:
                value = str(raw_path).strip().strip('"').replace("\\", "/")
                lowered = value.lower()
                for marker in ("/src/", "/lib/", "/include/"):
                    if lowered.startswith(marker.lstrip("/")):
                        return value
                    marker_pos = lowered.rfind(marker)
                    if marker_pos >= 0:
                        return value[marker_pos + 1:]
                return value.rsplit("/", 1)[-1] or value

            def _format_gcc_diagnostic(raw_line: str):
                diagnostic = re.match(
                    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
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
                    location += f":{diagnostic.group('line')}"
                    if diagnostic.group("column"):
                        location += f":{diagnostic.group('column')}"
                    self.emit("console:log", {
                        "text": f"  {icon} {label} at {location}",
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

            def _report_build_silence(seconds: int) -> None:
                self.emit("console:log", {
                    "text": f"  ℹ PlatformIO has not emitted build output for {seconds} seconds; dependency scanning can be quiet.",
                    "tag": "dim",
                    "replace_pattern": r"PlatformIO has not emitted build output for \d+ seconds",
                    "newline": True,
                })

            for line in _iter_process_output(
                self._active_process,
                lambda: bool(self._stop_requested),
                self._kill_active_process_tree,
                _report_build_silence,
            ):
                line_clean = line.rstrip("\r\n")
                if not line_clean:
                    continue
                output_lines.append(line_clean)
                low = line_clean.lower()

                # PlatformIO's dependency tree is repetitive in the GUI log;
                # hide only its heading and contiguous tree rows. The next
                # non-tree line (including diagnostics and build status) still
                # follows the normal routing path.
                if _dependency_graph_active[0]:
                    if line_clean.lstrip().startswith("|"):
                        continue
                    _dependency_graph_active[0] = False
                if line_clean.strip().casefold() == "dependency graph":
                    _dependency_graph_active[0] = True
                    continue

                # PlatformIO downloads native packages itself on Linux. Keep
                # those installations protected just like Windows bootstrap.
                if re.match(r"^(platform|tool|library) manager:\s+installing", low):
                    self._framework_download_active = True
                    self._current_op_phase = "installing"
                    self.emit("operation:phase", {"phase": "installing", "is_busy": True, "can_stop": False, "op": self.active_operation})
                    self.emit("window:closable", {"closable": False})
                elif getattr(self, "_framework_download_active", False) and re.match(r"^(compiling|building|linking|archiving|checking size)\b", low):
                    self._framework_download_active = False
                    self._current_op_phase = "compiling"
                    self.emit("operation:phase", {"phase": "compile", "is_busy": True, "can_stop": True, "op": self.active_operation})
                    self.emit("window:closable", {"closable": True})

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
                    "section `",
                    "region `",
                )
                is_source_excerpt = bool(re.match(r'^\s*(?:\d+\s*\||\|)', line_clean))
                is_linker_error = not is_source_excerpt and (
                    any(hint in low for hint in LINKER_ERROR_HINTS)
                    or bool(re.search(r'(?:^|[\\/])(?:[\w.+-]*-)?ld(?:\.exe)?:', low))
                )
                is_gcc_diagnostic = not is_source_excerpt and bool(re.search(r':\d+(?::\d+)?:\s*(fatal\s+error|error|warning|note)\s*:', low))
                is_generic_error = "error:" in low and "werror" not in low
                is_generic_warning = "warning:" in low
                is_scons_wrapper = bool(re.search(r'^\*\*\*\s+\[', line_clean))
                build_progress = _match_build_progress(line_clean)
                is_scons_progress = build_progress is not None
                is_build_mode = bool(re.fullmatch(r'Building in (?:release|debug) mode(?:\.{3})?', line_clean, re.IGNORECASE))
                is_build_metadata = bool(_build_metadata_pattern.fullmatch(line_clean))
                is_build_summary = bool(_build_summary_pattern.match(line_clean))
                is_build_decoration = bool(re.fullmatch(r'[-=*]{3,}\s*', line_clean)) or line_clean == "*"
                if not is_scons_progress:
                    _last_progress_text[0] = None

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

                if is_scons_progress or is_build_mode or is_build_summary or re.match(r'^(?:tool|platform|library) manager:.*has been installed!?$', low):
                    _in_error_block[0] = False

                if is_scons_progress or is_build_mode:
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

                # Suppress only known banners outside diagnostic context. A
                # source string or custom builder status remains useful output.
                if not (is_linker_error or is_gcc_diagnostic or is_context_header or _in_error_block[0]
                        or is_generic_error or is_generic_warning) and (
                    _build_promotion_pattern.match(line_clean) or is_build_decoration
                    or _build_routine_pattern.fullmatch(line_clean)
                ):
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
                elif is_scons_wrapper:
                    _ensure_post_deps_divider()
                    self.emit("console:log", {"text": f"  ✖ {line_clean}", "tag": "error", "newline": True})
                    _in_error_block[0] = False
                elif _in_error_block[0]:
                    prefix = "    " if is_context_header else "      "
                    self.emit("console:log", {"text": f"{prefix}{line_clean}", "tag": _error_block_type[0], "newline": True})
                    if "compilation terminated" in low:
                        _in_error_block[0] = False
                elif (is_build_metadata or is_build_summary) and not (is_generic_error or is_generic_warning):
                    pass  # Standard metadata/outcome is summarized after exit.
                elif is_build_mode:
                    _ensure_post_deps_divider()
                    self.emit("console:log", {
                        "text": f"  ⚙ {line_clean}" + ("" if line_clean.endswith("...") else "..."),
                        "tag": "info", "newline": True,
                    })
                elif is_generic_error:
                    _ensure_post_deps_divider()
                    self.emit("console:log", {"text": f"  ✖ {line_clean}", "tag": "error", "newline": True})
                elif is_generic_warning:
                    _ensure_post_deps_divider()
                    self.emit("console:log", {"text": f"  ⚠ {line_clean}", "tag": "warning", "newline": True})
                elif re.match(r'^Processing\s+\S+\s*\(', line_clean, re.IGNORECASE):
                    self.emit("console:log", {"text": f"    {line_clean}", "tag": "purple", "newline": True})
                    if not _first_divider_printed[0]:
                        _first_divider_printed[0] = True
                        self.emit("console:log", {"text": "  ──────────────────────────────────────────────────", "tag": "purple_dim", "newline": True})
                elif re.match(r'^(?:tool|platform)[\s-]manager:', low):
                    item = re.sub(r'^(?:tool|platform)[\s-]manager:\s*', '', line_clean, flags=re.IGNORECASE).strip()
                    item = re.sub(r'^(?:installing|downloading|unpacking)\s+', '', item, flags=re.IGNORECASE).strip()
                    item = re.split(r'\s+has been installed!?$', item, flags=re.IGNORECASE)[0].strip()
                    if "tool-scons" in low and (
                        re.match(r'^(?:tool|platform)[\s-]manager:\s*(?:installing|downloading|unpacking)\b', low)
                        or re.search(r'\bhas been installed!?$', low)
                    ):
                        continue  # Routine internal engine checks; failures still remain visible.
                    manager_kind = "Toolchain/Tool" if ("tool" in low) else "Platform/Framework"
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
                    else:
                        self.emit("console:log", {"text": line_clean, "tag": "dim", "newline": True})
                elif low.startswith("library manager:"):
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
                elif re.match(r'^(?:downloading|unpacking)\b', low):
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
                        item = _current_framework_item[0] or "core framework package"
                        item_label = item if len(item) <= 44 else item[:41] + "..."
                        icon = "✔ " if pct >= 100 else "  "
                        progress_text = f"  {icon}{act_name:<11} [{item_label}]  {bar}  {pct:3d}%"
                        self.emit("console:log", {
                            "text": progress_text,
                            "tag": "success" if pct >= 100 else "info",
                            "replace_pattern": rf"(?:Downloading|Unpacking)\s+\[{re.escape(item_label)}\]",
                            "newline": True
                        })
                    else:
                        self.emit("console:log", {"text": line_clean, "tag": "dim", "newline": True})
                elif is_scons_progress:
                    progress_action, progress_target = build_progress
                    _prog_text = None
                    _prog_tag = "dim"
                    if progress_action == "linking":
                        _prog_text, _prog_tag = "  🔗 Linking...", "dim"
                    elif progress_action in ("checking size", "retrieving maximum program size"):
                        _prog_text, _prog_tag = "  📏 Checking firmware size...", "dim"
                    elif progress_action == "compiling":
                        filename = progress_target.rsplit("/", 1)[-1]
                        _prog_text = f"  ⚙ Compiling {filename}..."
                        _prog_tag = "info"
                    elif progress_action == "archiving":
                        _prog_text, _prog_tag = "  📦 Archiving...", "dim"
                    elif progress_action in ("building", "generating partitions"):
                        target_name = progress_target.rsplit("/", 1)[-1]
                        if "bootloader" in target_name.lower():
                            _prog_text = f"  ⚡ Building bootloader image ({target_name})..."
                            _prog_tag = "info"
                        elif progress_action == "generating partitions" or "partition" in target_name.lower():
                            _prog_text = f"  ⚡ Building partition table ({target_name})..."
                            _prog_tag = "info"
                        elif "firmware" in target_name.lower() or target_name.endswith((".bin", ".hex")):
                            _prog_text = f"  ⚡ Building firmware image ({target_name})..."
                            _prog_tag = "info"
                        else:
                            _prog_text = f"  ⚙ Building {target_name}..."
                            _prog_tag = "info"

                    if _prog_text and (progress_action == "compiling" or _prog_text != _last_progress_text[0]):
                        _ensure_post_deps_divider()
                        _last_progress_text[0] = _prog_text
                        self.emit("console:log", {"text": _prog_text, "tag": _prog_tag, "newline": True})
                    elif not _prog_text:
                        self.emit("console:log", {"text": line_clean, "tag": "dim", "newline": True})
                else:
                    # Frameworks and custom builders can emit useful diagnostics
                    # outside GCC's syntax. Do not silently lose unknown stdout.
                    self.emit("console:log", {"text": line_clean, "tag": "dim", "newline": True})

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
                            firmware = next((board_build_dir / filename for filename in ("firmware.hex", "firmware.uf2", "firmware.elf")
                                             if (board_build_dir / filename).is_file()), firmware)
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
                        fw_file = next((target_env_dir / filename for filename in ("firmware.hex", "firmware.uf2", "firmware.elf")
                                        if (target_env_dir / filename).is_file()), fw_file)
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

    def _resolve_board_info(self, board_name: str | None = None) -> dict[str, Any]:
        """Resolve current catalog data without guessing or retaining stale IDs."""
        if not board_name:
            board_name = getattr(self, "current_board", "") or ""
        if not board_name:
            return {}
        cache_key = board_name.strip().lower()

        if board_name in SUPPORTED_BOARDS:
            info = dict(SUPPORTED_BOARDS[board_name])
            framework = getattr(self, "_board_frameworks", {}).get(board_name)
            if framework and framework in info.get("frameworks", []):
                info["framework"] = framework
            return info
        for k, v in SUPPORTED_BOARDS.items():
            if k.lower() == cache_key:
                info = dict(v)
                framework = getattr(self, "_board_frameworks", {}).get(k)
                if framework and framework in info.get("frameworks", []):
                    info["framework"] = framework
                return info

        return {}

    def _check_target(self, action: str, *, sketch: bool = False) -> bool:
        from main.core.target_profile import target_problem
        has_ino = bool(sketch and self.sketch_dir_path and next(self.sketch_dir_path.glob("*.ino"), None))
        problem = target_problem(self._resolve_board_info(), arduino_sketch=has_ino)
        if not problem:
            return True
        self.emit("console:log", {"text": f"{action} unavailable: {problem}", "tag": "error", "newline": True})
        self.emit("notification", {"title": f"{action} unavailable", "message": problem, "type": "warning"})
        return False

    def _hash_sources(self, board_name: str | None = None, *, source_files: dict[str, Path] | None = None) -> str:
        """Calculate MD5 digest of all sketch sources for build cache hit detection."""
        hasher = hashlib.md5()
        hasher.update(sys.platform.encode("utf-8"))
        # Firmware built with another framework/target must never be reused.
        info = self._resolve_board_info(board_name)
        identity_keys = ("platform", "board", "framework", "flash_mb", "has_psram", "memory_type", "flash_mode", "backend", "arduino_fqbn", "arduino_cli")
        hasher.update(json.dumps({key: info.get(key, "") for key in identity_keys}, sort_keys=True).encode("utf-8"))
        manifest = str(info.get("pio_manifest") or "")
        if manifest and Path(manifest).is_file():
            hasher.update(Path(manifest).read_bytes())
        try:
            files = ({path.name: path for path in get_sketch_files_fast(self.sketch_dir_path)}
                     if source_files is None else source_files)
            for name, f in sorted(files.items()):
                hasher.update(name.encode("utf-8"))
                # This is a build/upload safety boundary, not an editor preview.
                # FAT/exFAT timestamps can be coarse or deliberately preserved;
                # same-sized replacements must never reuse older firmware.
                with f.open("rb") as source:
                    for chunk in iter(lambda: source.read(131072), b""):
                        hasher.update(chunk)
        except OSError as exc:
            raise RuntimeError(f"Cannot verify source content: {exc}") from exc
        return hasher.hexdigest()

    @staticmethod
    def _stage_root_source_file(source: Path, destination: Path) -> bool:
        """Keep unchanged objects without treating coarse timestamps as proof.

        Compare bounded sequential chunks, avoiding both rewrite wear and a
        whole-file pair in RAM. A genuinely changed staging file retains its
        new write time, so even timestamp-based compiler deciders see it.
        """
        same = False
        previous_mtime_ns = None
        try:
            previous = destination.stat()
            previous_mtime_ns = previous.st_mtime_ns
            if source.stat().st_size == previous.st_size:
                with source.open("rb") as incoming, destination.open("rb") as staged:
                    same = True
                    while True:
                        chunk = incoming.read(131072)
                        if chunk != staged.read(131072):
                            same = False
                            break
                        if not chunk:
                            break
        except OSError:
            pass
        if same:
            return False
        ensure_file_writable(destination)
        shutil.copyfile(source, destination)
        if previous_mtime_ns is not None:
            written = destination.stat()
            if written.st_mtime_ns == previous_mtime_ns:
                # FAT's two-second clock can give a changed same-size file its
                # former write time. Advance only the changed staged copy, not
                # user sources or unchanged objects; fail rather than compile
                # stale bytes if the device cannot represent a new timestamp.
                os.utime(destination, ns=(written.st_atime_ns, previous_mtime_ns + 2_000_000_000))
                if destination.stat().st_mtime_ns == previous_mtime_ns:
                    raise OSError("The build drive could not mark the staged source change.")
        return True

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
                content = _sketch_ram_cache.get_content(file_path, refresh=True)
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
                for hdr in _sketch_ram_cache.get_includes(f, refresh=True):
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
            text = f"  🔌 Connecting [ {bar} ] | FAILED >> 💡 Please hold 'BOOT' button on MCU physical board during attempt."
            tag = "error"
        elif connected:
            text = "  ✔ Connected"
            tag = "success"
        else:
            text = f"  🔌 Connecting [ {bar} ] | {current}/{total}"
            text += " >> 💡 Please hold 'BOOT' button on MCU physical board"
            tag = "magenta"

        self.emit("console:log", {
            "text": text,
            "tag": tag,
            "replace_pattern": r"(?:connecting|connected).*",
            "newline": True
        })

    @staticmethod
    def _fast_upload_retry_allowed(*, return_code: int | None,
                                   attempt_connected: bool,
                                   write_started: bool,
                                   all_images_verified: bool,
                                   retry_used: bool,
                                   operation: str | None,
                                   stop_requested: bool) -> bool:
        """Only retry a connected esptool attempt before erase/write begins."""
        return bool(
            return_code not in (None, 0)
            and attempt_connected
            and not write_started
            and not all_images_verified
            and operation != "reset"
            and not retry_used
            and not stop_requested
        )

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
            "replace_pattern": rf"(?:Flashing)\s+{re.escape(stage_str + label)}\s*\[",
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
            if key == "firmware" and (fast_bins or {}).get("recovery_only"):
                continue
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
        board_name = fast_bins.get("board_name") or self.current_board or ""
        board_info = dict(fast_bins.get("board_info") or self._resolve_board_info(board_name) or {})
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
        launch_env = os.environ.copy()
        reset_mode = str(fast_bins.get("before") or "default-reset")
        if self._is_native_usb_port(port):
            reset_mode = "usb-reset"
        config_path = self._write_esptool_connect_config(
            self._effective_cache_root(self.sketch_dir_path), 1, reset_mode
        )
        if config_path:
            launch_env["ESPTOOL_CFGFILE"] = config_path
        # Keep the outer budget authoritative; inherited options must not turn
        # each of its ten pre-write attempts into another ten attempts.
        launch_env["ESPTOOL_CONNECT_ATTEMPTS"] = "1"
        launch_env["PYTHONUNBUFFERED"] = "1"

        if fast_bins.get("platform") == "espressif32":
            write_cmd += [
                str(fast_bins["bootloader_addr"]), str(fast_bins["bootloader"]),
                "0x8000", str(fast_bins["partitions"]),
                "0xe000", str(fast_bins["boot_app0"]),
            ]
            if not fast_bins.get("recovery_only"):
                write_cmd += ["0x10000", str(fast_bins["firmware"])]
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
            "getcommstate", "serial timeout",
        )

        chip_info: dict[str, str] = {}
        chip_info_shown = False
        fast_metadata_shown = False
        connected_bar_flipped = False
        upload_started = time.perf_counter()
        self._last_fast_upload_failure_kind = ""
        self._last_fast_upload_write_started = False
        connection_poll_count = [max(1, min(_MAX_CONNECT_RETRIES, start_attempt))]
        flash_retry_used = False

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
            nonlocal attempt_connected, verified_images, write_started
            stripped = raw_line.rstrip()
            if not stripped:
                return
            output_lines.append(stripped)
            if len(output_lines) > 256:
                del output_lines[0]
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
            if "will be erased" in low or "erasing" in low:
                attempt_connected = True
                write_started = True
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
                write_started = True

            if "hash of data verified" in low:
                verified_images += 1
                attempt_connected = True

            if self._consume_esptool_upload_progress(
                    upload_progress_state, stripped,
                    before_progress=_before_fast_progress,
                    phase_callback=_set_fast_phase):
                if upload_progress_state.get("stage_locked") or upload_progress_state.get("started"):
                    write_started = True
                return
            if "verifying written data" in low or "hash of data verified" in low:
                _set_fast_phase("Verifying")
            if "hard resetting" in low:
                _set_fast_phase("Resetting")

        self._append_connecting_progress(start_attempt, _MAX_CONNECT_RETRIES)

        flash_retry_used = False  # True after the first post-connect retry at the chosen baud
        while True:
            output_lines = []
            upload_progress_state = self._new_upload_progress_state(fast_bins)
            attempt_connected = False
            write_started = False
            completed_images: set[str] = set()
            verified_images = 0
            expected_image_count = len(upload_progress_state.get("stages") or [])
            timed_out = False
            verification_complete_seen = False
            proc = None
            reader_thread = None
            discard_output = threading.Event()

            try:
                if _connect_retry_count == max(0, start_attempt - 1):
                    time.sleep(0.75)

                attempt_cmd = list(write_cmd)

                creationflags = (subprocess.CREATE_NO_WINDOW | 0x00004000) if sys.platform == "win32" else 0
                proc = subprocess.Popen(
                    attempt_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                    creationflags=creationflags, env=launch_env,
                )
                self._active_process = proc
                _start = time.monotonic()
                output_queue: queue.Queue = queue.Queue(maxsize=128)

                def _publish_fast_output(record, attempt_queue=output_queue,
                                         attempt_discard=discard_output):
                    while not attempt_discard.is_set():
                        try:
                            attempt_queue.put(record, timeout=0.1)
                            return
                        except queue.Full:
                            continue

                def _read_fast_output(attempt_process=proc, publish=_publish_fast_output):
                    try:
                        if attempt_process.stdout is not None:
                            for raw_line in iter(lambda: attempt_process.stdout.readline(8192), ""):
                                # After verification/consumer failure, continue
                                # draining this same child so it can exit; never
                                # feed a later attempt or block on a full queue.
                                publish(raw_line)
                    except Exception as exc:
                        # A broken pipe reader cannot certify that flash was
                        # untouched. Propagate it to the terminal unknown-write
                        # path rather than treating it as ordinary EOF/retry.
                        publish(exc)
                    finally:
                        publish(None)

                reader_thread = threading.Thread(
                    target=_read_fast_output, name="MCU_FastUpload_Reader", daemon=True
                )
                reader_thread.start()
                reader_done = False
                _last_output_time = time.monotonic()
                # Silence watchdog: if esptool is connected but goes quiet mid-write,
                # kill it after this many seconds.  Separate from _WATCHDOG_SECS which
                # only covers the pre-connection phase.
                _SILENCE_WATCHDOG_SECS = 30
                while not reader_done:
                    try:
                        line = output_queue.get(timeout=0.10)
                    except queue.Empty:
                        now = time.monotonic()
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

                    _last_output_time = time.monotonic()
                    if line is None:
                        reader_done = True
                        continue
                    if isinstance(line, BaseException):
                        raise line

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
                    and not write_started
                    and not upload_progress_state.get("started")
                    and not upload_progress_state.get("stage_locked")
                    and not completed_images
                    and not cli_syntax_error
                    and any(signature in joined for signature in _CONNECT_FAIL_SIGNATURES)
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

                # Retry a post-connect failure only while esptool has not begun
                # erasing or writing flash. Never replay a hardware write.
                writing_started = (
                    write_started
                    or bool(completed_images)
                    or bool(upload_progress_state.get("started"))
                    or bool(upload_progress_state.get("stage_locked"))
                )
                post_connect_transport_failure = (
                    rc != 0
                    and attempt_connected
                    and not all_images_verified
                )
                self._last_fast_upload_write_started = writing_started
                if (post_connect_transport_failure and self._fast_upload_retry_allowed(
                        return_code=rc,
                        attempt_connected=attempt_connected,
                        write_started=writing_started,
                        all_images_verified=all_images_verified,
                        retry_used=flash_retry_used,
                        operation=getattr(self, "active_operation", None),
                        stop_requested=bool(getattr(self, "_stop_requested", False)),
                )):
                    if not self._is_port_present(port):
                        if not self._wait_for_port_reconnect(port):
                            error_message = (
                                f"MCU disconnected during upload ({port} is no longer available)"
                            )
                            self._last_fast_upload_failure_kind = "flash" if writing_started else "connection"
                            self._record_fast_upload_diagnostic(
                                port, attempt_cmd, return_code=rc,
                                output_lines=output_lines, error=error_message,
                            )
                            return False, error_message, _connect_retry_count + 1

                    chosen_speed = str(fast_bins.get("upload_speed") or "460800")
                    flash_retry_used = True

                    # If a stage was actively writing when it dropped, mark it as stalled.
                    if writing_started:
                        _stages_list = upload_progress_state.get("stages") or []
                        _n = len(_stages_list)
                        _active_idx = int(upload_progress_state.get("active_index", 0))
                        if 0 <= _active_idx < _n:
                            _rlabel = _stages_list[_active_idx].get("label") or "Firmware"
                            _stage_str = f"[{_active_idx + 1}/{_n}] " if _n > 1 else ""
                            _pct = float(upload_progress_state.get("last_percent") or 0.0)
                            _bar_w = 30
                            _fld = int(_pct / 100.0 * _bar_w)
                            _sbar = "▰" * _fld + "▱" * max(0, _bar_w - _fld)
                            self.emit("console:log", {
                                "text": f"  ✖ Flashing {_stage_str}{_rlabel} [ {_sbar} ] | {_pct:5.1f}% (stalled)",
                                "tag": "warning",
                                "replace_pattern": rf"Flashing\s+{re.escape(_stage_str + _rlabel)}\s*\[",
                                "newline": True,
                            })
                        self.emit("console:log", {
                            "text": f"  ⚠ Serial data stalled at {chosen_speed} baud.",
                            "tag": "warning",
                            "newline": True,
                        })
                    else:
                        self.emit("console:log", {
                            "text": f"  ⚠ Upload failed at {chosen_speed} baud — device did not respond.",
                            "tag": "warning",
                            "newline": True,
                        })

                    self.emit("console:log", {
                        "text": f"  🔄 Retrying at {chosen_speed} baud (your selected upload speed)…",
                        "tag": "info",
                        "newline": True,
                    })
                    self.emit("console:log", {
                        "text": "  💡 Hold the 'BOOT' button now if your board requires manual download mode.",
                        "tag": "info",
                        "replace_pattern": r"💡\s*Hold BOOT now.*",
                        "newline": True,
                    })

                    # Give the bootloader a moment, then retry with a fresh connection budget.
                    _connect_retry_count = 0
                    connection_poll_count[0] = 1
                    connected_bar_flipped = False
                    self._append_connecting_progress(1, _MAX_CONNECT_RETRIES)
                    time.sleep(1.0)
                    continue

                ok = (rc == 0)
                if ok or all_images_verified:
                    self._last_fast_upload_write_started = bool(
                        writing_started or all_images_verified
                    )
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

                if attempt_connected and writing_started and not all_images_verified:
                    self.emit("console:log", {
                        "text": "  ⚠ Flash erase/write had begun before communication failed. Automatic retry was stopped to avoid replaying flash commands.",
                        "tag": "warning",
                        "newline": True,
                    })
                    self.emit("console:log", {
                        "text": "  ℹ The firmware may be incomplete. Keep the board powered, reconnect it if needed, and retry Upload once the serial link is stable.",
                        "tag": "info",
                        "newline": True,
                    })

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
                elif writing_started or completed_images:
                    self._last_fast_upload_failure_kind = "flash"
                elif attempt_connected and not cli_syntax_error:
                    self._last_fast_upload_failure_kind = "connection"
                else:
                    self._last_fast_upload_failure_kind = "tool"

                self._record_fast_upload_diagnostic(
                    port, attempt_cmd, return_code=rc,
                    output_lines=output_lines, error=error_message,
                )
                return False, error_message, _connect_retry_count + 1
            except Exception as e:
                # Missing reader telemetry is not evidence of untouched flash:
                # the launched child can already be erasing/writing while we
                # drain it in finally. Treat that state as potentially written.
                launched = proc is not None
                self._last_fast_upload_write_started = bool(write_started or launched)
                self._last_fast_upload_failure_kind = "flash" if launched else "tool"
                error_message = str(e)
                if launched:
                    error_message += "; uploader status was interrupted after launch, so firmware may have changed"
                self._record_fast_upload_diagnostic(
                    port, attempt_cmd, error=error_message,
                )
                return False, error_message, _connect_retry_count + 1
            finally:
                discard_output.set()
                if proc is not None:
                    # A consumer/worker exception is not proof that a launched
                    # write is safe to kill. Drain and await it before unlocking.
                    if proc.poll() is None:
                        if reader_thread is None or not reader_thread.is_alive():
                            if proc.stdout is not None:
                                for _unused in iter(lambda: proc.stdout.readline(8192), ""):
                                    pass
                        proc.wait()
                    if reader_thread is not None and reader_thread.is_alive():
                        reader_thread.join(timeout=0.5)
                    if proc.stdout is not None:
                        proc.stdout.close()
                    if getattr(self, "_active_process", None) is proc:
                        self._active_process = None

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
        from main.core.target_profile import target_problem
        problem = target_problem(binfo)
        if problem:
            raise ValueError(problem)
        platform = binfo["platform"]
        board = binfo["board"]
        framework = binfo["framework"]
        from main.core.target_profile import upload_configuration
        upload_options = upload_configuration(binfo, getattr(self, "upload_speed", "") or str(DEFAULT_UPLOAD_SPEED))

        offline_libraries = Path(_get_safe_platformio_core_dir(SCRIPT_DIR)) / "lib"
        extra_dirs = [offline_libraries.as_posix()] if offline_libraries.is_dir() else []
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
            f"{upload_options}"
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

        write_generated_text(ini_file, ini_content)

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
        if self.is_busy or self.active_operation:
            return

        if self._block_if_pending_ai_edits("Upload"):
            return

        cfg = load_gui_config()
        if getattr(self, "clear_console_on_action", cfg.get("clear_console_on_action", True)):
            self.emit("console:clear", None)
        if getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)):
            self.emit("serial:clear", None)

        if not self.current_board:
            self.emit("console:log", {"text": "✖ Upload error: No board selected. Please select a board first.", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Upload Failed", "message": "No board selected.", "type": "warning"})
            return
        from main.core.target_profile import upload_target_ready
        if not upload_target_ready(self._resolve_board_info(), self.current_port):
            self.emit("console:log", {"text": "✖ Upload error: No COM port selected. Please select a port first.", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Upload Failed", "message": "No port selected.", "type": "warning"})
            return

        self.is_busy = True
        self._stop_requested = False
        self.active_operation = "upload"
        self._current_op_phase = "resolving"
        self.emit("operation:phase", {"phase": "compile", "is_busy": True, "can_stop": False, "op": "upload"})
        self.emit("window:closable", {"closable": True})
        try:
            self._operation_worker = threading.Thread(target=self._upload_requested_worker, args=(cfg,), name="MCU_Upload", daemon=True)
            self._operation_worker.start()
        except Exception as exc:
            self._operation_worker = None
            self.emit("console:log", {"text": f"Upload worker could not start: {exc}",
                                      "tag": "error", "newline": True})
            self._release_requested_operation()

    @guarded_package_operation
    def _upload_requested_worker(self, cfg):
        try:
            if not self._resolve_requested_target("Upload"):
                self._release_requested_operation()
                return
            self._start_resolved_upload(cfg)
        except Exception as exc:
            self.emit("console:log", {"text": f"Upload preparation failed: {exc}", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Upload failed", "message": str(exc), "type": "error"})
            self._release_requested_operation()

    def _start_resolved_upload(self, cfg):
        """Snapshot the verified target and check cached sources on the worker."""
        from main.core.target_profile import requires_upload_port
        if not self.current_port and requires_upload_port(self._resolve_board_info()):
            self.emit("console:log", {"text": "Upload cancelled: the selected port disconnected during board resolution.",
                                      "tag": "error", "newline": True})
            self._release_requested_operation()
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

        if selected_board_info.get("backend") == "arduino-cli":
            from main.core.arduino_backend import run_arduino_operation
            return run_arduino_operation(self, upload=True)

        # Fingerprint sources on the worker, after board resolution.
        can_skip = (self._active_skip_compile
                    and self.check_can_skip_compile_for_upload(selected_board))

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

        native = _HOST_RUNTIME.use_native_upload(selected_board_info)
        worker = self._native_upload_worker if native else self._upload_worker
        worker(can_skip)

    def _native_upload_worker(self, can_skip=False):
        """Delegate native programmer protocols to PIO, with one write attempt."""
        from main.core.target_profile import requires_upload_port
        monitor_port = str(self._active_port_label or "")
        monitor_paused = False
        write_started = False
        try:
            owner = port_occupied_owner(monitor_port) if monitor_port else None
            if owner:
                raise RuntimeError(f"Port {monitor_port} is in use by another window (PID {owner}).")
            if not can_skip and not self._compile_worker(is_upload=True):
                return
            if self._stop_requested:
                return
            # First compile can install the platform and reveal its native
            # transport. Re-read metadata before passing any serial argument.
            info = self._resolve_board_info() or getattr(self, "_active_board_info", {})
            port = monitor_port if requires_upload_port(info) else ""
            if info and requires_upload_port(info) and (not port or self.current_port != port):
                raise RuntimeError("The selected serial upload port disconnected or changed during compilation.")
            command = list(find_pio_executable() or [])
            if not command:
                raise RuntimeError("PlatformIO is unavailable. Repair the application runtime.")
            cache_root = self._effective_cache_root(self.sketch_dir_path)
            self._generate_platformio_ini(cache_root)
            if monitor_port:
                self._stop_serial_monitor()
                monitor_paused = True
            self.is_busy = True
            self.active_operation = "upload"
            self._current_op_phase = "connecting"
            self.emit("operation:phase", {"phase": "upload", "is_busy": True, "can_stop": True, "op": "upload"})
            self.emit("window:closable", {"closable": False})
            jobs = self._get_jobs()
            command += ["run", "-e", "mcu_env", "-t", "upload", "-j", str(jobs)]
            if port:
                command += ["--upload-port", port]
            core_dir, _ = _refresh_platformio_core_environment(SCRIPT_DIR)
            env = os.environ.copy()
            env["PLATFORMIO_CORE_DIR"] = str(core_dir)
            env["PLATFORMIO_BUILD_JOBS"] = str(jobs)
            env["PLATFORMIO_RUN_JOBS"] = str(jobs)
            env["SCONSFLAGS"] = f"-j{jobs}"
            self.emit("console:log", {"text": "Uploading through the selected board's PlatformIO programmer protocol…", "tag": "info", "newline": True})
            self._active_process = subprocess.Popen(
                command, cwd=str(cache_root), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                **_HOST_RUNTIME.process_options(priority=True, session=True),
            )

            def _report_upload_silence(seconds: int) -> None:
                self.emit("console:log", {
                    "text": f"  ℹ PlatformIO upload has not emitted output for {seconds} seconds; waiting for the current build or upload step.",
                    "tag": "dim",
                    "replace_pattern": r"PlatformIO upload has not emitted output for \d+ seconds",
                    "newline": True,
                })

            def _stop_upload_if_safe() -> None:
                if (self._current_op_phase not in ("flashing", "writing", "resetting", "erasing")
                        and self.active_operation not in ("flash", "reset")):
                    self._kill_active_process_tree()

            for line in _iter_process_output(
                self._active_process,
                lambda: bool(self._stop_requested),
                _stop_upload_if_safe,
                _report_upload_silence,
            ):
                line_clean = line.rstrip("\r\n")
                if not line_clean:
                    continue
                if not write_started and _platformio_upload_write_started(line_clean):
                    write_started = True
                    self._current_op_phase = "flashing"
                    self.emit("operation:phase", {
                        "phase": "flash", "is_busy": True,
                        "can_stop": False, "op": "upload",
                    })
                action, verdict = _classify_platformio_upload_line(line_clean)
                if action == "outcome":
                    tag = "success" if verdict == "SUCCESS" else "error"
                    self.emit("console:log", {
                        "text": f"  {line_clean.strip()}", "tag": tag, "newline": True,
                    })
                    continue
                if action == "suppress":
                    continue

                low = line_clean.lower()
                tag = "error" if re.search(
                    r"\b(?:fatal\s+error|error|failed|failure|exception|timed?\s*out|"
                    r"permission\s+denied|no\s+device|cannot\s+open|can't\s+open)\b", low
                ) else "warning" if "warning" in low else "info"
                self.emit("console:log", {"text": line_clean, "tag": tag, "newline": True})
            if self._active_process.stdout:
                self._active_process.stdout.close()
            code = self._active_process.wait()
            if self._stop_requested and not write_started:
                self.emit("console:log", {
                    "text": "Upload cancelled before flash erase/write began.",
                    "tag": "warning", "newline": True,
                })
                self.emit("notification", {
                    "title": "Upload cancelled",
                    "message": "The programmer was stopped before flash erase/write began.",
                    "type": "warning",
                })
                self.emit("console:progress", {"action": "Cancelled"})
                return
            if code:
                if write_started:
                    self.emit("console:log", {
                        "text": "  ⚠ Flash erase/write began before communication failed. The firmware may be incomplete; MCU Flasher did not replay the upload or reset the board.",
                        "tag": "warning", "newline": True,
                    })
                    self.emit("console:log", {
                        "text": "  ℹ Keep the board powered, stabilize the serial link, then retry Upload once. Lower Upload Speed if the connection remains unreliable.",
                        "tag": "info", "newline": True,
                    })
                raise RuntimeError(f"PlatformIO upload exited with code {code}. Review the console for the programmer or device requirement.")
            self.emit("notification", {"title": "Upload completed", "message": "PlatformIO reported a successful upload.", "type": "success"})
            self.emit("console:progress", {"action": "Completed", "percent": 100})
        except Exception as exc:
            self.emit("console:log", {"text": f"Upload failed: {exc}", "tag": "error", "newline": True})
            self.emit("notification", {"title": "Upload failed", "message": str(exc), "type": "error"})
        finally:
            process = self._active_process
            if process is not None and process.poll() is None:
                # A read/log failure must not unlock the window during a write.
                process.wait()
            self._active_process = None
            self.is_busy = False
            self.active_operation = None
            self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False})
            self.emit("window:closable", {"closable": True})
            self._unmap_unc_after_build()
            if monitor_paused and self.current_port == monitor_port:
                self._start_serial_monitor()

    def _get_jobs(self) -> int:
        configured_jobs = load_gui_config().get("compiler_jobs")
        storage_paths = [SCRIPT_DIR, getattr(self, "sketch_dir_path", SCRIPT_DIR)]
        for key in ("PLATFORMIO_CORE_DIR", "PLATFORMIO_PACKAGES_DIR"):
            if os.environ.get(key):
                storage_paths.append(Path(os.environ[key]))
        # Called by build workers; a bounded warm probe includes portable app
        # and source drives even if the process started on a fast system disk.
        safe_jobs = get_optimal_compiler_jobs(storage_paths=storage_paths, storage_wait=True)
        if configured_jobs is not None and str(configured_jobs).isdigit() and int(configured_jobs) > 0:
            return min(int(configured_jobs), safe_jobs)
        return safe_jobs

    def _board_cache_key(self, board_name: str | None = None) -> str:
        name = board_name or self.current_board or "unknown_board"
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
        family = str(capabilities.get("family") or binfo.get("platform") or "").lower()
        family_map = {
            "atmelavr": "soft_reset_project_uno",
            "avr": "soft_reset_project_uno",
            "espressif8266": "soft_reset_project_esp8266",
            "ststm32": "soft_reset_project_stm32",
            "stm32": "soft_reset_project_stm32",
            "raspberrypi": "soft_reset_project_rp2040",
            "rp2040": "soft_reset_project_rp2040",
            "atmelsam": "soft_reset_project_samd",
            "samd": "soft_reset_project_samd",
            "teensy": "soft_reset_project_teensy",
            "nordicnrf52": "soft_reset_project_nrf52",
            "nrf52": "soft_reset_project_nrf52",
            "renesas-ra": "soft_reset_project_renesas",
            "renesas": "soft_reset_project_renesas",
            "espressif32": "soft_reset_project",
            "esp32": "soft_reset_project",
        }
        base = family_map.get(family, "soft_reset_project")
        board_key = self._board_cache_key(name)
        target_dir = SCRIPT_DIR / "soft_reset" / base / "boards" / board_key
        legacy_dir = SCRIPT_DIR / "soft_reset" / "soft_reset_project" / "boards" / board_key
        if not target_dir.exists() and legacy_dir.exists():
            return legacy_dir
        return target_dir

    def _reset_project_contents(self, board_name: str, board_info: dict) -> tuple[str, str, str]:
        info = dict(board_info or self._resolve_board_info(board_name))
        platform = str(info.get("platform", "atmelavr"))
        board_id = str(info.get("board", "uno"))
        framework = str(info.get("framework", "arduino"))
        reset_capabilities = board_reset_capabilities(
            platform, board_id, board_name, framework
        )
        reset_family = str(reset_capabilities.get("family") or platform).lower()
        is_avr = reset_family in ("atmelavr", "avr")
        is_esp32 = reset_family in ("espressif32", "esp32")
        is_esp8266 = (
            reset_family in ("espressif8266", "esp8266")
            or "esp8266" in board_name.lower()
            or "nodemcu" in board_name.lower()
            or "node" in board_name.lower()
        )
        is_s3 = is_s3_board(board_id)
        is_native = bool(
            (is_s3 or "s2" in board_id.lower() or "c3" in board_id.lower() or "c6" in board_id.lower() or "h2" in board_id.lower())
            and self._is_native_usb_port(getattr(self, "current_port", None))
        )
        flash_size, has_psram = normalized_board_memory_options(info)
        memory_type = normalized_board_memory_type(info)
        flash_mode = normalized_board_flash_mode(info)
        if is_esp8266:
            monitor_speed = "115200"
        else:
            monitor_speed = default_monitor_baud(platform, board_id, board_name)
        from main.core.target_profile import upload_configuration

        env_lines = [
            f"platform = {platform}",
            f"board = {board_id}",
            f"framework = {framework}",
            f"monitor_speed = {monitor_speed}",
        ]
        reset_upload_info = dict(info)
        if is_esp32 or is_esp8266:
            reset_upload_info["upload_protocol"] = "esptool"
        env_lines.extend(upload_configuration(reset_upload_info, "115200" if is_esp8266 else "460800").strip().splitlines())
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
            "  unsigned long _start = millis();\n"
            "  while (!Serial && (millis() - _start < 1500)) {\n"
            "    delay(10);\n"
            "  }\n"
            "  Serial.println(\">>> ----- <<<\");\n"
            "}\n"
            "void loop() {\n"
            "  delay(1000);\n"
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
            write_generated_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
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

        pio_path = find_pio_executable()
        if not pio_path:
            return None, "PlatformIO is unavailable."
        jobs = self._get_jobs()
        cmd = pio_path + ["run", "-e", "mcu_flash", "-j", str(jobs)]
        self.emit("console:log", {
            "text": f"  🔧 First Hard/Soft Reset for {board_name} — preparing exact-board recovery binaries…",
            "tag": "info",
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
                    # Suppress internal PlatformIO banners that confuse users
                    if any(kw in low for kw in (
                        "building in release mode", "took ", "====", "processing mcu_flash",
                        "if you like platformio", "star it on github", "follow us on linkedin",
                        "looking for ", "check our library registry", "verbose mode can be enabled"
                    )):
                        continue
                    if any(token in low for token in ("error", "fatal", "failed")):
                        self.emit("console:log", {"text": f"  {line}", "tag": "error", "newline": True})
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
            self.emit("console:log", {"text": "  ✔ Exact-board recovery binaries ready and saved.", "tag": "success", "newline": True})
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
        if not port:
            return True
        owner_pid = port_occupied_owner(port)
        if owner_pid:
            self.emit("console:log", {"text": f"  ⚠ Reset(DTR/RTS) blocked: Port '{port}' is in use by another window (PID {owner_pid}).", "tag": "warning", "newline": True})
            return False
        b_name = board_name or self.current_board or ""
        b_info = board_info or self._resolve_board_info(b_name)
        platform = str(b_info.get("platform", "")).lower()
        is_uno = ("avr" in platform)
        self.emit("console:log", {"text": f"  🔄 Rebooting board on {port}...", "tag": "info", "newline": True})
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

    def _write_esptool_connect_config(self, cache_root: Path, connect_attempts: int = 10,
                                      reset_mode: str = "default-reset") -> str | None:
        """Write the esptool connect config shared by every esptool subprocess.

        Share the application's existing longer UART reset/boot settle timing
        with optimized and PlatformIO children. Keep DTR/RTS's transition adjacent:
        two-transistor auto-reset circuits release EN when both controls are
        asserted, so inserting a delay between D1 and R0 can boot run mode.
        This is not a substitute for manual BOOT on boards without working reset
        wiring. Native USB and POSIX keep esptool's own reset strategies.
        """
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
            esptool_cfg = cache_root / "esptool.cfg"
            # A custom sequence overrides even esptool's USB reset strategy.
            # Only Windows external UART bridges need this timing adaptation;
            # native USB and POSIX retain esptool's transport-specific reset.
            custom_reset = ("custom_reset_sequence = D0|R1|W0.15|D1|R0|W0.5|D0\n"
                            if sys.platform == "win32" and reset_mode == "default-reset" else "")
            esptool_cfg.write_text(
                "[esptool]\n"
                f"connect_attempts = {int(connect_attempts)}\n"
                "reset_delay = 0.5\n"
                + custom_reset,
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
            self.is_busy = False
            self.active_operation = self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False})
            self.emit("window:closable", {"closable": True})
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
            self.active_operation = self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False})
            self.emit("window:closable", {"closable": True})
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

        # ── Smart compile check: auto-skip when binary is cached and sources unchanged ──
        need_compile = True
        skip_comp = bool(getattr(self, "_active_skip_compile", getattr(self, "skip_compile", False)))
        skip_reason_msg: str | None = None  # message to show after console clear
        bin_file = self._find_cached_firmware_binary(self.current_board)
        has_prior_build = (bin_file is not None) or self._has_prior_build(self.current_board)

        if skip_comp and has_prior_build:
            recompile_needed, reason = self._needs_recompile(self.current_board)
            if not recompile_needed:
                # Reuse only when the user requested it and identity is valid.
                need_compile = False
                skip_reason_msg = "  ✔ Sources unchanged — skipping recompile (using cached firmware)"
            else:
                self.emit("console:log", {
                    "text": f"  🔄 Recompile needed: {reason}",
                    "tag": "warning",
                    "newline": True,
                })

        if not need_compile:
            cfg = load_gui_config()
            if cfg.get("clear_console_on_action", True):
                self.emit("console:clear", None)
            if cfg.get("clear_serial_on_action", False):
                self.emit("serial:clear", None)
            if skip_reason_msg:
                self.emit("console:log", {
                    "text": skip_reason_msg,
                    "tag": "success",
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
        if (mcu_detached_during_compile[0] or self.current_port != port
                or self.current_board != getattr(self, "_active_board_name", self.current_board)
                or not self._is_port_present(port)):
            self.emit("console:log", {
                "text": f"  ⚠ Upload skipped: the target or port changed, or MCU on {port} disconnected.\n  💡 Select the intended board and stable port, then click Upload again.",
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
                    skip_mtime_check=bool(skip_comp or can_skip or not need_compile),
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
                    elif failure_kind == "flash" and getattr(self, "_last_fast_upload_write_started", False):
                        self.emit("console:log", {"text": "", "newline": True})
                        self.emit("console:log", {
                            "text": "  ⚠ Flashing stopped after erase/write began. MCU Flasher did not restart the flash automatically.",
                            "tag": "warning",
                            "newline": True,
                        })
                        self.emit("console:log", {
                            "text": "  💡 Reconnect the board, select a stable port, lower Upload Speed (try 115200), then click Upload once. Clean is not required.",
                            "tag": "info",
                            "newline": True,
                        })
                    else:
                        self.emit("console:log", {"text": "", "newline": True})
                        self.emit("console:log", {"text": f"  ✖ Fast upload failed: {fast_error}", "tag": "error", "newline": True})
                        if str(upload_speed) in ("921600", "512000"):
                            self.emit("console:log", {
                                "text": f"  💡 High upload speed ({upload_speed} baud) may exceed hardware limits for this USB bridge or cable. Try selecting 460800 or 115200 baud in the toolbar.",
                                "tag": "warning",
                                "newline": True,
                            })
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
            pio_cmd = find_pio_executable()
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

            # Synchronize esptool connect behavior via cache_root and env vars (ESP only)
            if is_esp:
                reset_mode = "usb-reset" if self._is_native_usb_port(port) else "default-reset"
                esptool_cfg_path = self._write_esptool_connect_config(cache_root, _MAX_CONNECT_RETRIES, reset_mode)
                if esptool_cfg_path:
                    launch_env["ESPTOOL_CFGFILE"] = esptool_cfg_path
                launch_env["ESPTOOL_CONNECT_ATTEMPTS"] = str(_MAX_CONNECT_RETRIES)

            if is_avr and not (Path(core_dir) / "packages/tool-avrdude/package.json").is_file():
                from src.modules.offline_runtime import bootstrap_instruction
                raise RuntimeError(bootstrap_instruction("AVR upload tool tool-avrdude is missing"))

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

            def _stop_cancellable_upload() -> None:
                if (
                    self._current_op_phase not in ("flashing", "writing", "resetting", "erasing")
                    and self.active_operation not in ("flash", "reset")
                ):
                    self._kill_active_process_tree()

            def _report_upload_silence(seconds: int) -> None:
                self.emit("console:log", {
                    "text": f"  ℹ PlatformIO upload has not emitted output for {seconds} seconds; waiting for the device or programmer.",
                    "tag": "dim",
                    "replace_pattern": r"PlatformIO upload has not emitted output for \d+ seconds",
                    "newline": True,
                })

            for line in _iter_process_output(
                self._active_process,
                lambda: bool(self._stop_requested),
                _stop_cancellable_upload,
                _report_upload_silence,
            ):
                line_clean = line.rstrip("\r\n")
                if not line_clean:
                    continue
                low = line_clean.lower()
                _stripped = line_clean.strip()
                upload_line_action, verdict = _classify_platformio_upload_line(line_clean)
                if upload_line_action == "outcome":
                    tag = "success" if verdict == "SUCCESS" else "error"
                    self.emit("console:log", {"text": f"  {line_clean.strip()}", "tag": tag, "newline": True})
                    continue
                if upload_line_action == "suppress":
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
                elif is_avr:
                    self.emit("console:log", {"text": "", "newline": True})
                    self.emit("console:log", {
                        "text": "  💡 Arduino UNO does not have a BOOT button. Press the physical RESET button on the board once if the bootloader did not respond, then retry.",
                        "tag": "info",
                        "newline": True,
                    })
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
                # Release DTR/RTS without resetting the board. A partial flash
                # must not be rebooted automatically after a transport failure.
                self._release_port_lines(port, pulse_reset=False)
            if is_success or was_monitoring:
                time.sleep(0.5)
                self._start_serial_monitor()

    # ──────────────────────────────────────────────────────────
    # JS-RPC: RESETS & CACHE ACTIONS
    # ──────────────────────────────────────────────────────────
    def reset_mcu(self):
        """Perform a hardware reboot of the connected microcontroller via DTR/RTS without dropping the serial monitor."""
        if self.is_busy and self.active_operation != "compile":
            self.emit("serial:log", {"text": "--- ⚠ Cannot reset MCU: Operation currently in progress. ---", "tag": "warning", "newline": True})
            return
        if not self._check_target("Reset"):
            return
        if str(self._resolve_board_info().get("platform", "")).lower() not in {
            "atmelavr", "espressif32", "espressif8266", "ststm32", "raspberrypi", "ch32v", "samd",
        }:
            self.emit("serial:log", {"text": "No serial reset sequence is defined for this board. Use its physical reset button or programmer.", "tag": "warning", "newline": True})
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
                    "text": f"--- ↺ Triggering pin reset (DTR/RTS) on {self.current_port}... ---",
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
                            "text": "--- ✔ Pin reset (DTR/RTS) completed successfully ---",
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
                        "text": "--- ✔ Pin reset (DTR/RTS) completed successfully ---",
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

    def _start_reset_worker(self, kind, worker):
        """Reserve the operation before dispatch, including early failure cleanup."""
        if self.is_busy or self.active_operation:
            return False
        self.is_busy = True
        self.active_operation = "reset"
        self._active_reset_kind = kind
        self._current_op_phase = "resetting"
        self._stop_requested = False
        self._op_session_id = getattr(self, "_op_session_id", 0) + 1
        reset_session = self._op_session_id
        self.emit("operation:phase", {"phase": "reset", "is_busy": True, "can_stop": False, "op": kind + "_reset"})
        self.emit("window:closable", {"closable": False})

        def run():
            try:
                with package_store_lease(package_core_directory(),
                                         root=getattr(self, "_package_event_root", None)):
                    worker()
            except Exception as exc:
                self.emit("console:log", {"text": f"Reset failed: {exc}", "tag": "error", "newline": True})
            finally:
                if self.active_operation == "reset" and self._op_session_id == reset_session:
                    self._active_reset_kind = None
                    self._active_process = None
                    self.is_busy = False
                    self.active_operation = None
                    self._current_op_phase = None
                    self.emit("operation:phase", {"phase": "idle", "is_busy": False, "op": kind + "_reset", "success": False})
                    self.emit("window:closable", {"closable": True})
                    self.emit("console:progress", {"action": "Failed"})

        try:
            threading.Thread(target=run, name="MCU_" + kind.title() + "Reset", daemon=True).start()
        except Exception as exc:
            self.is_busy = False
            self.active_operation = None
            self._active_reset_kind = None
            self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False, "op": kind + "_reset", "success": False})
            self.emit("window:closable", {"closable": True})
            self.emit("console:log", {"text": f"Cannot start reset: {exc}", "tag": "error", "newline": True})
            return False
        return True

    def hard_reset(self, erase_flash: bool = False):
        """Perform a safe board-specific hard reset matching LATEST-WORKING-MCU- FLASHER."""
        if self.is_busy or self.active_operation:
            self.emit("console:log", {"text": "Busy — wait for the current operation before resetting.", "tag": "warning", "newline": True})
            return
        if not self._check_target("Hard reset"):
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

        port, board_name = self.current_port, self.current_board
        binfo = dict(self._resolve_board_info(board_name))
        caps = board_reset_capabilities(binfo.get("platform"), binfo.get("board"), board_name, binfo.get("framework"))
        if not caps["hard_reset_ui"]:
            self.emit("console:log", {"text": "Hard Reset is unavailable for this target.", "tag": "error", "newline": True})
            return False

        def _worker():
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

            self.emit("console:progress", {"action": "Resetting"})
            was_monitoring = getattr(self, "_serial_thread", None) is not None
            hard_reset_success = False

            try:
                self._stop_serial_monitor()
                time.sleep(0.5)
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
                        pio_path = find_pio_executable()
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
                    images, error = self._locate_hard_reset_recovery_images(board_name, binfo)
                    if images is None:
                        images, error = self._build_hard_reset_recovery_images(board_name, binfo)
                    if images is None:
                        raise RuntimeError(error or "Recovery preparation failed; flash was not erased.")
                    boot_app0 = self._locate_esp32_boot_app0()
                    if boot_app0 is None or not Path(boot_app0).is_file():
                        raise RuntimeError("boot_app0.bin is unavailable; flash was not erased.")
                    chip, boot_address = self._esptool_target(board_name, binfo)
                    if not chip or boot_address is None:
                        raise RuntimeError("Cannot determine exact chip/bootloader address; flash was not erased.")
                    recovery_bins = dict(images, platform="espressif32", boot_app0=boot_app0,
                                         bootloader_addr=boot_address, recovery_only=True,
                                         board_name=board_name, board_info=binfo,
                                         upload_speed=getattr(self, "upload_speed", "460800"))
                    self.emit("console:progress", {"action": "Connecting to ESP32"})
                    is_native = bool(self._is_native_usb_port(port))
                    before_reset = "usb-reset" if is_native else "default-reset"
                    if is_native:
                        self.emit("console:log", {"text": "  ⚡ Native USB detected — using usb-reset strategy.", "tag": "dim", "newline": True})
                    else:
                        self.emit("console:log", {"text": "  💡 Hold BOOT button now if your board requires manual download mode.", "tag": "info", "newline": True})
                    self._emit_boot_connection_progress(0)

                    target_mcu, _ = self._esptool_target(board_name, binfo)
                    target_mcu = target_mcu or "esp32"
                    baud_rate = getattr(self, "upload_speed", "460800") or "460800"
                    erase_cmd = self._get_esptool_cmd() + [
                        "--chip", target_mcu,
                        "--port", port,
                        "--baud", str(baud_rate),
                        "--before", before_reset,
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
                    erase_started = False
                    erase_completed = False
                    erase_ticks = 0

                    def _handle_line(line: str):
                        nonlocal connected, erase_started, erase_completed, connection_step
                        line = line.strip()
                        if not line:
                            return
                        low = line.lower()
                        if low.startswith("connecting"):
                            connection_step = max(connection_step, line.count("."))
                            self._emit_boot_connection_progress(connection_step)
                            return
                        if "connected to " in low or "uploading stub" in low or "stub flasher running" in low:
                            if not connected:
                                connected = True
                                self._emit_boot_connection_progress(connection_step, connected=True)
                                self.emit("console:log", {"text": "  ✔ ESP32 connected — release the BOOT button now.", "tag": "success", "newline": True})
                        if "erasing flash" in low or "chip erase" in low:
                            erase_started = True
                            self.emit("console:progress", {"action": "Erasing flash"})
                            self.emit("console:log", {"text": "  🔥 Erasing entire flash memory (this may take a while)...", "tag": "warning", "newline": True})
                        elif "erased successfully" in low:
                            erase_completed = True
                            self.emit("console:log", {"text": "  ✔ Flash memory erased successfully.", "tag": "success", "newline": True})
                        elif "hard resetting" in low or "hard reset" in low:
                            self.emit("console:log", {"text": "  🔄 Hard resetting via RTS pin...", "tag": "info", "newline": True})
                        elif any(k in low for k in ("error", "failed", "fatal")):
                            self.emit("console:log", {"text": f"  ✖ {line}", "tag": "error", "newline": True})
                        else:
                            self.emit("console:log", {"text": f"  {line}", "tag": "info", "newline": True})

                    if proc.stdout:
                        for raw_line in iter(proc.stdout.readline, ""):
                            if self._stop_requested:
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                                break
                            _handle_line(raw_line)

                    proc.wait()
                    self._active_process = None
                    if proc.returncode != 0:
                        if not connected:
                            self._emit_boot_connection_progress(connection_step, failed=True)
                        self.emit("console:log", {"text": f"  ✖ Hard Reset failed with code {proc.returncode}.", "tag": "error", "newline": True})
                        return

                    self.emit("console:log", {"text": "  ✔ ESP32 flash erased completely.", "tag": "success", "newline": True})
                    self.emit("console:progress", {"action": "Writing recovery bootloader"})
                    recovery_bins["before"] = before_reset
                    restored, error, _attempts = self._soft_reset_esptool_write(recovery_bins, port)
                    if not restored:
                        raise RuntimeError(f"Flash erased, but bootloader recovery failed: {error}")
                    self._trigger_actual_board_reset(port, board_name, binfo)
                    self.emit("console:log", {"text": "  ✔ Recovery bootloader and partition metadata restored. Use Upload to install your sketch.", "tag": "success", "newline": True})
                    self.emit("notification", {"title": "Hard Reset Complete", "message": "ESP32 erased and recovery bootloader restored. Ready for a fresh upload.", "type": "success"})
                    hard_reset_success = True
                else:
                    # No hard reset strategy for this board — do NOT silently
                    # fall back to a plain pin toggle and claim "Hard Reset Complete".
                    # That cheats the user. Fail loudly so they know nothing was wiped.
                    self.emit("console:log", {
                        "text": (
                            f"  ✖ Hard Reset is not supported for board '{board_name}' "
                            f"(platform: '{plat}'). No flash was erased or modified."
                        ),
                        "tag": "error",
                        "newline": True,
                    })
                    self.emit("console:log", {
                        "text": "  ℹ Hard Reset requires an ESP32/ESP8266 or AVR board. For other boards, use Upload to push a new sketch.",
                        "tag": "info",
                        "newline": True,
                    })
                    self.emit("notification", {
                        "title": "Hard Reset Not Supported",
                        "message": f"Board '{board_name}' does not support Hard Reset (flash erase). No changes were made.",
                        "type": "warning",
                    })
                    # hard_reset_success stays False — no cheat

            except Exception as e:
                self.emit("console:log", {"text": f"✖ Hard Reset error: {e}", "tag": "error", "newline": True})
            finally:
                _release_reset_cache_lock(reset_cache_lock)
                self._active_process = None
                self._active_reset_kind = None
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
                self.emit("console:progress", {"action": "Completed" if hard_reset_success else "Failed"})
                if hard_reset_success or was_monitoring:
                    time.sleep(0.5)
                    if not self.is_busy and self.current_board == board_name and self.current_port == port:
                        self._start_serial_monitor()

        return self._start_reset_worker("hard", _worker)

    def soft_reset(self):
        """Perform a soft reset by compiling and uploading the clean board-specific recovery sketch."""
        if self.is_busy or self.active_operation:
            self.emit("console:log", {"text": "Busy — wait for the current operation before resetting.", "tag": "warning", "newline": True})
            return
        if not self._check_target("Soft reset"):
            return

        cfg = load_gui_config()
        if getattr(self, "clear_console_on_action", cfg.get("clear_console_on_action", True)):
            self.emit("console:clear", None)
        if getattr(self, "clear_serial_on_action", cfg.get("clear_serial_on_action", False)):
            self.emit("serial:clear", None)

        if not self.current_board:
            self.emit("console:log", {"text": "✖ Soft Reset error: No board selected.", "tag": "error", "newline": True})
            return
        binfo = dict(self._resolve_board_info(self.current_board))
        from main.core.target_profile import requires_upload_port
        needs_port = requires_upload_port(binfo)
        if needs_port and not self.current_port:
            self.emit("console:log", {"text": "✖ Soft Reset error: No COM port selected.", "tag": "error", "newline": True})
            return

        port = self.current_port or ""
        board_name = self.current_board
        caps = board_reset_capabilities(binfo.get("platform"), binfo.get("board"), board_name, binfo.get("framework"))
        if not caps["soft_reset"]:
            self.emit("console:log", {"text": "Soft Reset requires a supported Arduino target.", "tag": "error", "newline": True})
            return False

        def _worker():

            p_platform = binfo.get("platform", "")
            is_avr = (p_platform == "atmelavr")
            is_esp = p_platform in ("espressif32", "espressif8266")

            owner_pid = port_occupied_owner(port) if port else None
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

            self.emit("console:progress", {"action": "Soft resetting"})
            self.emit("console:log", {"text": "", "newline": True})
            self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
            self.emit("console:log", {
                "text": f"  🔄 SOFT RESET ({board_name})",
                "tag": "header",
                "newline": True
            })
            self.emit("console:log", {"text": "=" * 50, "tag": "header", "newline": True})
            port_label = port if port else "(Native Programmer Interface)"
            self.emit("console:log", {"text": f"  Port  : {port_label}", "tag": "info", "newline": True})
            self.emit("console:log", {"text": f"  Board : {board_name}", "tag": "dim", "newline": True})
            if is_esp:
                self.emit("console:log", {"text": "  💡 Tip: On Desktop PCs, some ESP modules may need BOOT held during connection.", "tag": "dim", "newline": True})
            elif is_avr:
                self.emit("console:log", {"text": "  ℹ AVR reset/upload behavior follows the selected board and bootloader.", "tag": "dim", "newline": True})
            elif not needs_port:
                self.emit("console:log", {"text": "  ℹ Native programmer target (e.g. ST-Link / Picotool / J-Link).", "tag": "dim", "newline": True})
            self.emit("console:log", {"text": "", "newline": True})

            was_monitoring = getattr(self, "_serial_thread", None) is not None
            ok = False
            err_msg = ""

            try:
                self._stop_serial_monitor()
                time.sleep(0.4)
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
                    pio_cmd = find_pio_executable()
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
                    fast_bins.update(board_name=board_name, board_info=binfo, before="usb-reset" if self._is_native_usb_port(port) else "default-reset")
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
                            fast_bins.update(board_name=board_name, board_info=binfo, before="usb-reset" if self._is_native_usb_port(port) else "default-reset")
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
                    # Non-ESP (e.g. AVR/Arduino UNO): run PlatformIO upload
                    is_avr_target = (p_platform == "atmelavr" or "avr" in str(p_platform).lower())
                    # Arduino UNO/AVR has only a physical RESET button (no BOOT mode) — never loop 10 retries
                    _MAX_CONNECT_RETRIES = 1
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
                    target_label = port if port else "native programmer"
                    self.emit("console:log", {"text": f"  Executing soft reset upload on {target_label}...", "tag": "info", "newline": True})

                    while True:
                        cmd = pio_cmd + [
                            "run", "-e", "mcu_flash", "-t", "upload",
                            "-j", str(jobs),
                        ]
                        if port and needs_port:
                            cmd.extend(["--upload-port", port])
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

                        if not is_avr_target and is_conn_failure and _connect_retry_count < _MAX_CONNECT_RETRIES - 1 and not getattr(self, "_stop_requested", False):
                            if port and not self._is_port_present(port):
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
                            if is_avr_target:
                                self.emit("console:log", {
                                    "text": "  💡 Arduino UNO does not have a BOOT button. Press the physical RESET button on the board once if the bootloader did not respond, then retry.",
                                    "tag": "info",
                                    "newline": True,
                                    })
                        break

                if ok:
                    if p_platform in ("espressif32", "espressif8266"):
                        self._write_reset_manifest(project_dir, board_name, binfo)
                    time.sleep(0.5)
                    if port:
                        self._trigger_actual_board_reset(port, board_name, binfo)
                    self.emit("console:log", {"text": "✔ Soft Reset successful! Board restored to clean recovery state.", "tag": "success", "newline": True})
                    self.emit("notification", {"title": "Soft Reset Complete", "message": "Recovery sketch flashed successfully.", "type": "success"})
                else:
                    self.emit("console:log", {"text": f"✖ Soft Reset failed: {err_msg or 'Upload error'}", "tag": "error", "newline": True})
                    self.emit("notification", {"title": "Soft Reset Failed", "message": err_msg or "Soft Reset failed", "type": "error"})

            except Exception as e:
                ok = False
                self.emit("console:log", {"text": f"✖ Soft Reset exception: {e}", "tag": "error", "newline": True})
            finally:
                _release_reset_cache_lock(reset_cache_lock)
                self._active_process = None
                self._active_reset_kind = None
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
                self.emit("console:progress", {"action": "Completed" if ok else "Failed"})
                if ok or was_monitoring:
                    time.sleep(0.5)
                    if not self.is_busy and self.current_board == board_name and port and self.current_port == port:
                        self._start_serial_monitor()

        return self._start_reset_worker("soft", _worker)


    def clean_cache(self):
        """Clean intermediate build files, temporary directories, and generated configs matching LATEST-WORKING-MCU- FLASHER."""
        if self.is_busy:
            self.emit("console:log", {"text": "⚠ Busy — stop the current operation first", "tag": "warning", "newline": True})
            return

        sketch = self.sketch_dir_path
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

        def _worker():
            sketch_name = sketch.name if sketch else "Project"
            reset_cache_lock = None
            clean_success = False
            try:
                reset_cache_lock = _try_acquire_reset_cache_lock()
                if reset_cache_lock is None:
                    raise RuntimeError("Another window is using the shared reset cache. Try Clean after it finishes.")
                cache_root = get_project_build_cache_root(sketch, create=False)
                targets = [
                    (cache_root / "boards", "exact-board build workspaces"),
                    (cache_root / ".pio", "legacy build workspace"),
                    (cache_root / "src", "staged build sources"),
                    (cache_root / "platformio.ini", "generated build configuration"),
                    (cache_root / "build_artifacts", "compiled artifacts"),
                    (cache_root / ".mcu_gui_cache.json", "compile metadata"),
                    (cache_root / "compile_cache.json", "compile cache"),
                    (cache_root / ".mcu_flash_syntax_errors.json", "syntax metadata"),
                    (SCRIPT_DIR / "soft_reset" / "soft_reset_project" / "boards", "Soft/Hard Reset board caches"),
                    (SCRIPT_DIR / "soft_reset" / "soft_reset_project_uno" / "boards", "Arduino reset board caches"),
                    (SCRIPT_DIR / "soft_reset" / "soft_reset_project" / ".pio", "Soft/Hard Reset shared legacy cache"),
                    (SCRIPT_DIR / "soft_reset" / "soft_reset_project_uno" / ".pio", "Arduino shared legacy cache"),
                ]
                soft_reset_dir = SCRIPT_DIR / "soft_reset"
                if soft_reset_dir.is_dir():
                    for sub in soft_reset_dir.iterdir():
                        if sub.is_dir() and sub.name not in ("soft_reset_project", "soft_reset_project_uno"):
                            b_dir = sub / "boards"
                            if b_dir.exists():
                                targets.append((b_dir, f"{sub.name} reset board caches"))
                            p_dir = sub / ".pio"
                            if p_dir.exists():
                                targets.append((p_dir, f"{sub.name} shared cache"))
                remote_root = self._remote_workspace_root(sketch)
                if remote_root is not None:
                    targets.append((remote_root, "remote project local build workspace"))
                removed = []
                failures = []
                for target, label in targets:
                    if target.exists():
                        try:
                            # A redirected cache must never delete material outside its owner.
                            owner = SCRIPT_DIR if target.is_relative_to(SCRIPT_DIR / "soft_reset") else (remote_root.parent if remote_root is not None and target == remote_root else cache_root)
                            if not target.resolve().is_relative_to(owner.resolve()) or target.is_symlink() or getattr(target, "is_junction", lambda: False)():
                                raise RuntimeError("Refusing redirected cache path")
                            if target.is_dir():
                                robust_rmtree(target)
                            else:
                                target.unlink(missing_ok=True)
                            removed.append(label)
                        except Exception as exc:
                            failures.append(f"{label}: {exc}")

                # Invalidate all in-memory caches and reset compile tracking
                self._last_source_hash = ""
                self._last_compiled_board = ""
                self.skip_compile = False
                self.emit("skip_compile:availability", False)
                _sketch_ram_cache.invalidate()

                if failures:
                    raise RuntimeError("Some cache items could not be removed: " + "; ".join(failures))
                clean_success = True

                if removed:
                    clean_msg = f"Clean completed successfully: removed {len(removed)} cached items."
                    self.emit("console:log", {"text": f"✔ {clean_msg}", "tag": "success", "newline": True})
                    self.emit("notification", {
                        "title": "Clean Complete",
                        "message": f"Build cache cleaned for '{sketch_name}': removed {len(removed)} cached items.",
                        "type": "success",
                        "category": "system",
                        "details": {"sketch": str(sketch), "removed_count": len(removed), "removed_items": removed},
                    })
                else:
                    clean_msg = "Project is already clean. Ready to rebuild from scratch."
                    self.emit("console:log", {"text": f"✔ {clean_msg}", "tag": "success", "newline": True})
                    self.emit("notification", {
                        "title": "Clean Complete",
                        "message": f"Build cache for '{sketch_name}' is already clean. Ready to rebuild from scratch.",
                        "type": "info",
                        "category": "system",
                        "details": {"sketch": str(sketch), "removed_count": 0},
                    })
                self.emit("console:log", {"text": "  Ready. Compile or Upload to rebuild from scratch.", "tag": "dim", "newline": True})
            except Exception as e:
                err_msg = f"Clean error: {e}"
                self.emit("console:log", {"text": f"✖ {err_msg}", "tag": "error", "newline": True})
                self.emit("notification", {
                    "title": "Clean Failed",
                    "message": f"Clean error for '{sketch_name}': {e}",
                    "type": "error",
                    "category": "system",
                    "details": {"sketch": str(sketch), "error": str(e)},
                })
            finally:
                _release_reset_cache_lock(reset_cache_lock)
                self.is_busy = False
                self.active_operation = None
                self._current_op_phase = None
                self.emit("operation:phase", {"phase": "idle", "is_busy": False, "op": "clean", "success": clean_success})
                self.emit("window:closable", {"closable": True})
                self.emit("console:progress", {"action": "Completed" if clean_success else "Failed"})

        try:
            threading.Thread(target=_worker, name="MCU_CleanCache", daemon=True).start()
        except Exception as exc:
            self.is_busy = False
            self.active_operation = None
            self._current_op_phase = None
            self.emit("operation:phase", {"phase": "idle", "is_busy": False, "op": "clean", "success": False})
            self.emit("console:progress", {"action": "Failed"})
            self.emit("console:log", {"text": f"Cannot start Clean: {exc}", "tag": "error", "newline": True})

    def stop_operation(self):
        """Cancel the currently running compilation or building phase safely."""
        if getattr(self, "_framework_download_active", False):
            self.emit("console:log", {"text": "Wait for the native package installation to finish before stopping.", "tag": "warning", "newline": True})
            return
        if getattr(self, "active_operation", None) == "clean":
            return
        if getattr(self, "active_operation", None) in ("flash", "reset", "hard_reset", "soft_reset") or getattr(self, "_current_op_phase", None) in ("flashing", "writing", "resetting", "erasing"):
            self.emit("console:log", {
                "text": "  ⚠ Stop rejected: Firmware upload / flash write is currently in progress.\n    Interrupting flash writes can permanently brick or corrupt your MCU.",
                "tag": "warning",
                "newline": True,
            })
            return

        if not self.is_busy:
            return
        if self._stop_requested:
            return  # Repeated clicks must not spawn redundant kill/failsafe workers.

        self._stop_requested = True
        session_id = getattr(self, "_op_session_id", 0)
        self.emit("console:log", {"text": "⏹ Stopping the active operation...", "tag": "warning", "newline": True})

        def _kill_bg():
            self._kill_active_process_tree()
            time.sleep(4)
            if self.is_busy and getattr(self, "_op_session_id", 0) == session_id:
                process = getattr(self, "_active_process", None)
                worker = getattr(self, "_operation_worker", None)
                if ((process is not None and process.poll() is None)
                        or (worker is not None and worker.is_alive())
                        or getattr(self, "_framework_download_active", False)):
                    self.emit("console:log", {"text": "The operation is still stopping; its worker and process must exit before another action can begin.", "tag": "warning", "newline": True})
                    return
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

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        cfg = load_gui_config()
        cfg.update(settings)
        shared_updates = None
        if "theme_mode" in settings:
            from main.core.theme import Theme
            mode = settings["theme_mode"]
            if mode not in Theme.PALETTES:
                mode = "default"
            shared_updates = {"theme_mode": mode, "theme_follow_system": False}
        if save_gui_config(cfg, shared_updates=shared_updates) is False:
            error = "The configuration files could not be written. Check that your settings folder is writable and try again."
            self.emit("notification", {"title": "Settings not saved", "message": error, "type": "error"})
            return {"success": False, "error": error}
        if shared_updates is not None:
            Theme.apply_theme(shared_updates["theme_mode"])
        return {"success": True}

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
