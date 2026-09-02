#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import time
import re
import subprocess
import threading
import queue
import traceback
from datetime import datetime
from typing import TYPE_CHECKING
import tkinter as tk
from tkinter import messagebox


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core import board_catalog as _board_catalog
from main.core.board_catalog import *
from main.core.board_compat import *
from main.widgets import *
from main.dialogs import *
from main.editor_api import *

if TYPE_CHECKING:
    from main.mcu_flash_gui import MCUUploadGUI
    _Base = MCUUploadGUI
else:
    _Base = object

class BuildActionsMixin(_Base):
    """Mixin providing BuildActionsMixin capabilities for MCUUploadGUI."""
    def _prepare_board_toolchain_for_operation(
        self, selected_board: str, board_info: dict
    ) -> bool:
        """Automatically prepare one selected board before compile/upload.

        The caller already owns the operation state and worker thread. This
        method only performs the shared PlatformIO preparation and reports its
        output through the existing main-app console.
        """
        platform = str(board_info.get("platform") or "").strip()
        board_id = str(board_info.get("board") or "").strip()
        framework = str(board_info.get("framework") or "arduino").strip() or "arduino"

        if not platform:
            platform = _fallback_platform_from_mcu(str(board_info.get("mcu") or ""))
        if not board_id:
            arduino_id = str(board_info.get("arduino_board_id") or "").strip()
            board_id = _fallback_board_id_for_platform(
                platform, arduino_id, str(board_info.get("mcu") or ""), selected_board
            )

        if not platform or not board_id:
            self._append(
                f"  ✖ PlatformIO cannot prepare '{selected_board}': no canonical board manifest is available.",
                "error",
            )
            return False

        core_dir, core_refreshed = _refresh_platformio_core_environment(SCRIPT_DIR)
        if board_toolchain_ready(str(core_dir), platform, board_id, framework):
            return True

        self._framework_download_active = False
        self._append("  🔧 First-time toolchain setup for this board family...", "purple_header")
        self._append(f"    Board    : {selected_board}", "purple_dim")
        self._append(f"    Platform : {platform} (PIO ID: {board_id})", "purple_dim")
        self._append("    ⏳ Notice: Downloading compiler & packages (may take 3–5 minutes depending on internet speed).", "warning")
        self._append("    ✔ This is a one-time setup. Subsequent builds will start immediately without this delay.", "purple_dim")
        self._append("")

        active_package = [""]

        def _append_toolchain_progress(item_label: str, percent: float, phase: str = "Downloading"):
            pct = max(0.0, min(100.0, float(percent)))
            total_bar_width = 24
            filled = int(round((pct / 100.0) * total_bar_width))
            bar = "\u2588" * filled + "\u2591" * max(0, total_bar_width - filled)
            icon = "\u2714" if pct >= 99.9 else "\u23f3"
            action = "Downloaded" if pct >= 99.9 and phase == "Downloading" else ("Unpacked" if pct >= 99.9 else phase)
            tag = "success" if pct >= 99.9 else "info"
            clean_item = re.sub(r"^(?:Installing|Downloading|Unpacking)\s+", "", item_label, flags=re.I)
            clean_item = clean_item.replace("platformio/", "").strip() or "package"
            row_text = f"    {icon} {action} {clean_item} [{bar}] {pct:.1f}%"

            def _do():
                self.console.configure(state=tk.NORMAL)
                total_lines_cnt = int(self.console.index("end-1c").split(".")[0])
                found_line_idx = None
                found_line_text = ""
                for check_idx in range(total_lines_cnt, max(0, total_lines_cnt - 50), -1):
                    line_str = self.console.get(f"{check_idx}.0", f"{check_idx}.end")
                    if re.search(r"(?:downloading|downloaded|unpacking|unpacked)\s+.*?\[.*\]\s*\d+(?:\.\d+)?%", line_str.lower()):
                        found_line_idx = check_idx
                        found_line_text = line_str
                        break

                if found_line_idx is not None:
                    ts_match = re.match(r"^(\[\d+:\d+:\d+\])\s*", found_line_text)
                    ts_prefix = (ts_match.group(1) + " ") if ts_match else ""
                    self.console.delete(f"{found_line_idx}.0", f"{found_line_idx + 1}.0")
                    self.console.mark_set("_toolchain_ins_mark", f"{found_line_idx}.0")
                    insert_at = "_toolchain_ins_mark"
                else:
                    ts_prefix = f"[{datetime.now().strftime('%H:%M:%S')}] "
                    insert_at = tk.END

                self.console.insert(insert_at, ts_prefix, "timestamp")
                self.console.insert(insert_at, row_text + "\n", tag)
                if found_line_idx is not None:
                    try:
                        self.console.mark_unset("_toolchain_ins_mark")
                    except Exception:
                        pass
                self.console.configure(state=tk.DISABLED)
                if self.console_autoscroll_var.get():
                    self.console.see(tk.END)

            self._post_ui(_do)

        def _line_tag(line: str) -> str:
            low = str(line).lower()
            if "error" in low or "failed" in low or "fatal" in low:
                return "error"
            if "warning" in low or "warn" in low:
                return "warning"
            if "installed" in low and "installing" not in low:
                return "success"
            if "downloading" in low or "unpacking" in low or "installing" in low:
                return "info"
            return "dim"

        def _on_line(line: str):
            text = str(line or "").strip()
            if not text:
                return
            low = text.lower()
            if "command is deprecated" in low or "pio pkg install" in low or "telemetry" in low:
                return
            if "syntaxwarning:" in low or "invalid escape sequence" in low or text.startswith("words = re.split"):
                return
            if text.startswith("=" * 10) or text.startswith("-" * 10):
                return
            if "tool manager:" in low or "platform manager:" in low:
                if "@" in text:
                    pkg = text.split("@")[0].split(":")[-1].strip()
                    pkg = re.sub(r"^(?:Installing|Downloading|Unpacking)\s+", "", pkg, flags=re.I).strip()
                    active_package[0] = pkg
                self._append(f"    {text}", "info" if "installing" in low else "success")
                return
            pct_matches = re.findall(r"(\d+(?:\.\d+)?)%", text)
            if ("downloading" in low or "unpacking" in low) and pct_matches:
                pct = float(pct_matches[-1])
                phase = "Unpacking" if "unpacking" in low else "Downloading"
                item = active_package[0] or "toolchain"
                _append_toolchain_progress(item, pct, phase=phase)
                return
            self._append(f"    {text}", _line_tag(text))

        def _on_status(status_text: str):
            self._set_status(f"Toolchain: {status_text}", Theme.YELLOW)

        def _on_progress(pct: float):
            # Coarse progress from stage runner — real package progress is driven via _on_line
            pass

        def _on_download_start():
            self._framework_download_active = True
            self._set_status(
                f"Downloading {selected_board} framework/toolchain packages...",
                Theme.YELLOW,
            )

        def _on_process(process):
            self.process = process

        try:
            prep_ok = bool(
                prepare_platformio_board_toolchain(
                    platform,
                    board_id,
                    framework,
                    selected_board,
                    on_line=_on_line,
                    on_status=_on_status,
                    on_progress=_on_progress,
                    on_download_start=_on_download_start,
                    on_process=_on_process,
                    cancel_requested=lambda: bool(
                        getattr(self, "_stop_requested", False)
                    ),
                )
            )
            if prep_ok:
                try:
                    reloaded = load_dynamic_boards({})
                    shared_boards = _board_catalog.SUPPORTED_BOARDS
                    shared_boards.clear()
                    shared_boards.update(reloaded)
                    if selected_board in shared_boards:
                        board_info.update(shared_boards[selected_board])
                except Exception:
                    pass
            return prep_ok
        finally:
            self._framework_download_active = False
            self.process = None

    def _do_compile(self):
        if self.is_busy:
            # Auto-recover: if no subprocess is actually running, clear stale busy flag
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._append("  ℹ Stale busy state cleared — proceeding with compile.", "info")
            else:
                return

        if self._block_action_for_pending_ai_review("Compile"):
            return

        # Auto-save editor files before compiling so the compiler sees the latest source.
        if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
            try:
                self._save_all_editor_files()
            except Exception:
                pass

        if not self.board_var.get():
            self._append_notif("  ✖ Compile failed: No board selected! Choose a board before compiling.", "warning")
            return

        # Pre-check: detect board/sketch mismatch so _run_compile can display the warning
        # inside the COMPILING section header (not before it).
        compat_boards, compat_reasons = self._get_compat_analysis()
        selected_board = self.board_var.get()
        self._board_mismatch_detected = (
            bool(compat_boards) and selected_board not in compat_boards
        )
        # Store reasons so _run_compile can emit them after the COMPILING header
        self._pending_compat_reasons = compat_reasons if compat_reasons else []

        if self._block_action_for_pending_ai_review("Compile"):
            return

        self._clear_console_if_action_enabled()
        self.is_busy = True
        self._stop_requested = False
        self._compile_background_lock.set()
        self._set_buttons_state(True, operation="compile")

        # Capture the selected board before the worker starts. The worker may
        # prepare packages for a while, but it must never follow a later board
        # change or allow a background recognizer to alter its target.
        selected_board_info = dict(SUPPORTED_BOARDS.get(selected_board, {}))

        def _safe_compile():
            compile_succeeded = False
            try:
                if not self._prepare_board_toolchain_for_operation(
                    selected_board, selected_board_info
                ):
                    if self._stop_requested:
                        self._append("  ■ Compile cancelled during board toolchain preparation.", "warning")
                        self._set_status("Compile cancelled", Theme.YELLOW)
                    else:
                        self._append("  ✖ Board toolchain preparation failed; compile was not started.", "error")
                        self._set_status("Compile FAILED: board toolchain", Theme.RED)
                    return
                # Final skip decision belongs in the worker because preparing
                # PlatformIO can scan libraries and touch disk.  More
                # importantly, A -> B -> A must restore A's generated config
                # before comparing its build-config fingerprint.
                if self.skip_compile_var.get() and self._has_prior_build():
                    if self._ensure_platformio_ini():
                        recompile_needed, _reason = self._needs_recompile()
                        if not recompile_needed:
                            self._append("")
                            self._append("=" * 50, "header")
                            self._append("  ⚙  COMPILE CHECK", "header")
                            self._append("=" * 50, "header")
                            self._append("")
                            self._append("  ✔ Already compiled — sources unchanged.", "success")
                            self._append("  No recompilation needed. Cached build is up-to-date.", "dim")
                            self._append("  (Uncheck 'Skip recompile' or edit a source file to force rebuild)", "dim")
                            self._set_status("Compile skipped — sources unchanged", Theme.GREEN)
                            compile_succeeded = True
                            return
                compile_succeeded = self._run_compile() is True
            except Exception as e:
                import traceback
                try:
                    err_log = SCRIPT_DIR / "logs" / "error_log.txt"
                    err_log.parent.mkdir(parents=True, exist_ok=True)
                    with open(err_log, "w", encoding="utf-8") as f:
                        traceback.print_exc(file=f)
                except Exception:
                    pass
                self._append(f"  ✖ Internal error in compile thread: {e}", "error")
                self._set_status("Compile FAILED", Theme.RED)
            finally:
                # Guarantee busy state is always cleared, even on unhandled exceptions
                self.is_busy = False
                self._compile_background_lock.clear()
                self._set_buttons_state(False)
                # Keep a failed mapping available for diagnostics/retry.  A
                # standalone Compile owns its temporary network drive and may
                # remove it only after the compile genuinely succeeds.
                if compile_succeeded:
                    self._unmap_unc_after_build()

        threading.Thread(target=_safe_compile, daemon=True).start()

    def _do_upload(self):
        if self.is_busy:
            # Auto-recover: if no subprocess is actually running, clear stale busy flag
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._append("  ℹ Stale busy state cleared — proceeding with upload.", "info")
            else:
                return

        if self._block_action_for_pending_ai_review("Upload"):
            return

        # Auto-save editor files before uploading so the compiler sees the latest source.
        if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
            try:
                self._save_all_editor_files()
            except Exception:
                pass
        port = self._get_port()
        if not port:
            self._append_notif("  ✖ Upload failed: No serial port selected! Please select a port.", "warning")
            return

        if not self._is_port_present(port):
            self._append_notif(f"  ✖ Upload rejected: Selected port '{port}' is disconnected. Please connect your board.", "warning")
            self._set_status(f"Upload failed — {port} disconnected", Theme.RED)
            self._update_hardware_action_buttons()
            return

        owner_pid = port_occupied_owner(port)
        if owner_pid:
            self._append_notif(f"  ✖ Upload rejected: Port '{port}' is currently in use by another window (PID {owner_pid}).", "error")
            if self.root.winfo_exists():
                from tkinter import messagebox
                messagebox.showerror(
                    "Port In Use",
                    f"Cannot upload to '{port}' because it is in use by another MCU Flasher window (PID {owner_pid}).",
                    parent=self.root,
                )
            return

        if not self.board_var.get():
            self._append_notif("  ✖ Upload failed: No board selected! Choose a board before uploading.", "warning")
            return

        selected_board = self.board_var.get()
        selected_board_info = dict(SUPPORTED_BOARDS.get(selected_board, {}))

        if not self._is_board_recognized():
            self._append("  ✖ Upload rejected: board on this port hasn't been recognized yet.", "error")
            return

        if not self._is_valid_port():
            self._append(f"  ✖ Upload rejected: {self._port_mismatch_reason()}.", "error")
            return

        if self._block_action_for_pending_ai_review("Upload"):
            return

        self._clear_console_if_action_enabled()

        # Cancel any queued auto-reconnect before the worker starts compiling.
        # The monitor worker is stopped at the upload boundary below, but a
        # pending Tk timer could otherwise reopen COM12 between compilation and
        # esptool's first reset pulse.
        pending_monitor_job = getattr(self, "_auto_start_after_id", None)
        if pending_monitor_job:
            try:
                self.root.after_cancel(pending_monitor_job)
            except Exception:
                pass
            self._auto_start_after_id = None

        def _safe_run():
            upload_succeeded = False
            try:
                self._stop_requested = False
                if not self._prepare_board_toolchain_for_operation(
                    selected_board, selected_board_info
                ):
                    if self._stop_requested:
                        self._append("  ■ Upload cancelled during board toolchain preparation.", "warning")
                        self._set_status("Upload cancelled", Theme.YELLOW)
                    else:
                        self._append("  ✖ Board toolchain preparation failed; upload was not started.", "error")
                        self._set_status("Upload FAILED: board toolchain", Theme.RED)
                    return
                # _run_upload may compile first.  Keep the temporary network
                # mapping alive across that entire operation and clean it only
                # after the final upload result is successful.
                upload_succeeded = self._run_upload(port) is True
            except Exception as e:
                import traceback
                try:
                    err_log = SCRIPT_DIR / "logs" / "error_log.txt"
                    err_log.parent.mkdir(parents=True, exist_ok=True)
                    with open(err_log, "w", encoding="utf-8") as f:
                        traceback.print_exc(file=f)
                except Exception:
                    pass
                self._set_status("Upload FAILED", Theme.RED)
                self._append(f"  ✖ Internal error in upload thread: {e}", "error")
            finally:
                self.is_busy = False
                self._compile_background_lock.clear()
                self._set_buttons_busy(False)
                self._set_buttons_state(False)
                if upload_succeeded:
                    self._unmap_unc_after_build()


        self.is_busy = True
        self._set_buttons_state(True, operation="upload")
        threading.Thread(target=_safe_run, daemon=True).start()

    def _schedule_auto_start_monitor(self, delay_ms: int):
        """Schedule monitor start on Tk's thread; worker callers are marshaled."""
        if threading.get_ident() != getattr(self, "_tk_thread_id", None):
            self._post_ui(lambda d=int(delay_ms): self._schedule_auto_start_monitor(d))
            return
        if getattr(self, "_auto_start_after_id", None):
            try:
                self.root.after_cancel(self._auto_start_after_id)
            except Exception:
                pass
        self._auto_start_after_id = self.root.after(max(0, int(delay_ms)), self._auto_start_monitor)

    def _auto_start_monitor(self):
        """Start one generation-owned serial worker from a main-thread config snapshot."""
        self._auto_start_after_id = None
        if self.is_busy and getattr(self, "_active_operation", None) in ("upload", "flash", "reset"):
            return

        with self._serial_state_lock:
            current_thread = self.serial_thread
            current_running = self.serial_running
        if current_thread and current_thread.is_alive():
            self._schedule_auto_start_monitor(60)
            return
        if current_running:
            return

        port_raw = self.port_var.get()
        self._board_changed_no_port_msg = None
        if not port_raw or port_raw.startswith("─"):
            return
        port = self._get_port()
        if not port:
            return
        if not SUPPORTED_BOARDS:
            err_msg = "No boards are currently installed."
            if self._last_monitor_error != err_msg:
                self._last_monitor_error = err_msg
                self._append_notif("  ✖ Monitor blocked: No boards are currently installed.", "error")
                self._append_notif("    Please download a board framework first via the 'Download Boards/Libraries' manager.", "error")
            return

        # Capture every Tk Variable needed by the worker here.  The background
        # monitor never calls Variable.get()/set() or widget APIs.
        board_name = self.board_var.get()
        board_info = dict(SUPPORTED_BOARDS.get(board_name, {}))
        clear_on_connect = bool(
            getattr(self, "clear_serial_on_upload_var", None)
            and self.clear_serial_on_upload_var.get()
        )
        if clear_on_connect:
            # Clear synchronously on Tk before RX starts so the first boot bytes
            # cannot be rendered and then erased by a delayed UI callback.
            self._clear_serial_console()
        silent = bool(getattr(self, "_silent_reset", False))
        is_manual_reset = bool(getattr(self, "_manual_reset_pending", False))
        is_first_connect = not bool(getattr(self, "_first_connect_done", False))
        port_label = str(port_raw)

        native_keywords = ("esp32-s3", "esp32s3", "jtag", "usb bridge", "otg", "native", "usb serial device", "usb serial", "cdc", "usb debug")
        uart_keywords = ("ch340", "ch341", "ch342", "ch343", "cp210", "silicon labs", "ftdi", "wch")
        low_label = port_label.lower()
        has_native = any(k in low_label for k in native_keywords)
        has_uart = any(k in low_label for k in uart_keywords)
        p_board = board_info.get("board", "")
        is_native_usb = bool(
            (has_native and not has_uart)
            or ((is_s3_board(p_board) or "s3" in board_name.lower()) and not has_uart)
        )

        self._last_monitor_error = ""
        self._monitor_should_run = True
        with self._serial_state_lock:
            self._serial_generation += 1
            generation = self._serial_generation
            stop_event = threading.Event()
            self._serial_stop_event = stop_event
            self.serial_running = True
            config = {
                "board_name": board_name,
                "board_info": board_info,
                "port_label": port_label,
                "clear_on_connect": clear_on_connect,
                "silent": silent,
                "is_manual_reset": is_manual_reset,
                "is_first_connect": is_first_connect,
                "is_native_usb": is_native_usb,
            }
            worker = threading.Thread(
                target=self._run_monitor,
                args=(port, int(self.baud_var.get()), generation, stop_event, config),
                daemon=True,
                name=f"SerialMonitor-{generation}",
            )
            self.serial_thread = worker
        worker.start()

    def _do_stop(self):
        """Stop compile/upload process (does NOT stop serial monitor).

        NOTE: may be invoked from a worker thread (esptool watchdog timeouts),
        so every Tk widget access is marshaled to the main thread."""
        self._stop_requested = True
        session_id = getattr(self, "_op_session_id", 0)
        if self.process and self.process.poll() is None:
            try:
                self.root.after(0, lambda: self.btn_stop.configure(
                    text="■ Stopping...", state=tk.DISABLED))
            except Exception:
                pass
            self._set_status("Stopping process...", Theme.YELLOW)
            proc_to_kill = self.process
            
            def _kill():
                try:
                    if sys.platform == "win32":
                        import subprocess
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc_to_kill.pid)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    else:
                        proc_to_kill.kill()
                    self._append("  ■ Process killed.", "warning")
                except Exception as e:
                    self._append(f"  ⚠ Failed to stop process: {e}", "warning")
                # ── Failsafe: if the background thread doesn't clear is_busy
                # within 5 seconds after the process was killed, force-clear it
                # so the UI never gets permanently stuck — but ONLY if no new
                # operation session has started in the meantime.
                time.sleep(5)
                if self.is_busy and getattr(self, "_op_session_id", 0) == session_id:
                    self.is_busy = False
                    self._set_buttons_state(False)
                    self._set_status("Stopped (failsafe)", Theme.YELLOW)
                    self._append("  ⚠ Busy state cleared by failsafe timer.", "warning")

            threading.Thread(target=_kill, daemon=True).start()
        elif self.is_busy:
            if getattr(self, "_reconnect_waiting", False):
                try:
                    self.root.after(0, lambda: self.btn_stop.configure(
                        text="■ Stopping...", state=tk.DISABLED))
                except Exception:
                    pass
                self._set_status("Stopping...", Theme.YELLOW)
            else:
                # Process is already dead but is_busy is stuck — clear it immediately
                self.is_busy = False
                self._set_buttons_state(False)
                self._set_status("Ready", Theme.GREEN)
                self._append("  ℹ Busy state was stale — cleared.", "info")

    def _stop_serial_session(self):
        """Cancel/detach the active serial generation without joining the caller.

        close()/cancel_read() releases the COM handle immediately; the stale worker
        then exits on its Event using only its local connection object.  This keeps
        Upload/Reset and reconnect paths responsive even when a driver is sluggish.
        """
        with self._serial_state_lock:
            self._serial_generation += 1
            stop_event = self._serial_stop_event
            try:
                stop_event.set()
            except Exception:
                pass
            conn = self.serial_conn
            thread = self.serial_thread
            self.serial_conn = None
            self.serial_thread = None
            self.serial_running = False

        # Drop TX belonging to the retired generation.
        while True:
            try:
                self._serial_tx_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break

        if conn:
            try:
                if hasattr(conn, "cancel_read"):
                    conn.cancel_read()
            except Exception:
                pass
            try:
                if hasattr(conn, "cancel_write"):
                    conn.cancel_write()
            except Exception:
                pass
            try:
                if getattr(conn, "is_open", False):
                    conn.close()
            except Exception:
                pass
        return thread

    def _pause_monitor(self) -> bool:
        """Pause for upload/reset and release the port before the uploader starts."""
        was_running = bool(self.serial_running or getattr(self, "_monitor_should_run", False))
        self._monitor_should_run = False
        retired_thread = self._stop_serial_session()
        # Closing/cancelling pyserial releases the handle quickly, but the old
        # worker can still be unwinding a read on a slow USB-serial driver.  A
        # short bounded join prevents esptool from racing that worker for COM.
        if retired_thread and retired_thread is not threading.current_thread():
            try:
                retired_thread.join(timeout=1.25)
            except Exception:
                pass
        self._set_serial_status(False)
        if was_running:
            self._append_notif("  ⏸ Paused for upload…", "dim")
        return was_running

    def _resume_monitor(self):
        """Resume monitor after an operation; Tk configuration stays on Tk's thread."""
        def _resume_on_ui():
            self._monitor_should_run = True
            self._manual_reset_pending = True
            board_name = self.board_var.get()
            board_info = SUPPORTED_BOARDS.get(board_name, {})
            is_avr = (board_info.get("platform", "") == "atmelavr")
            self._schedule_auto_start_monitor(100 if is_avr else 150)
        self._post_ui(_resume_on_ui)

    def _restart_monitor(self, reason: str):
        """Cancel the current generation and relaunch without spawning a join thread."""
        if threading.get_ident() != getattr(self, "_tk_thread_id", None):
            self._post_ui(lambda r=str(reason): self._restart_monitor(r))
            return
        self._monitor_should_run = False
        was_running = bool(self.serial_running)
        self._stop_serial_session()

        if was_running or "baud" in reason.lower() or "reconnect" in reason.lower():
            self._set_serial_status("reconnecting")
            self._append_notif(f"  ↻ Restarting monitor — {reason}…", "dim")
            self._append_notif("─" * 40, "dim")

        if getattr(self, "clear_serial_on_upload_var", None) and self.clear_serial_on_upload_var.get():
            self._clear_serial_console()

        self._monitor_should_run = True
        self._schedule_auto_start_monitor(20)
