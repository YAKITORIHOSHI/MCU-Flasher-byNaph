#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import time
import json
import re
import shutil
import tempfile
import subprocess
import threading
import queue
import ctypes
import traceback
import hashlib
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font as tkfont


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
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

class HardResetMixin(_Base):
    """Mixin providing HardResetMixin capabilities for MCUUploadGUI."""
    def _append_boot_connection_progress(self, step: int = 0, *, connected: bool = False,
                                         failed: bool = False, cancelled: bool = False):
        """Render the Hard Reset BOOT/download-mode connection indicator.

        Unlike the old fixed five-second countdown, this row advances only when
        esptool emits another real connection dot.  It therefore behaves like
        the Arduino IDE/Cloud connection indicator: keep holding BOOT while the
        row is moving and release it as soon as the row turns green.
        """
        width = 30
        step = max(0, int(step or 0))
        if connected:
            bar = "█" * width
            text = f"  ✔ Boot connection [ {bar} ] | Connected — release BOOT"
            tag = "success"
        elif failed or cancelled:
            bar = "░" * width
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
            bar = "░" * pos + "█" * block + "░" * (width - pos - block)
            dots = "." * ((step % 4) + 1)
            text = f"  🔌 Boot connection [ {bar} ] | Hold BOOT{dots}"
            tag = "magenta"

        def _do():
            try:
                self.console.configure(state=tk.NORMAL)
                total_lines = int(self.console.index("end-1c").split(".")[0])
                found = None
                old_line = ""
                for idx in range(total_lines, max(0, total_lines - 250), -1):
                    candidate = self.console.get(f"{idx}.0", f"{idx}.end")
                    if "boot connection [" in candidate.lower():
                        found = idx
                        old_line = candidate
                        break

                if found is not None:
                    ts_match = re.match(r'^(\[\d+:\d+:\d+\])\s*', old_line)
                    ts_prefix = (ts_match.group(1) + " ") if ts_match else ""
                    self.console.delete(f"{found}.0", f"{found + 1}.0")
                    self.console.mark_set("_boot_conn_mark", f"{found}.0")
                    insert_at = "_boot_conn_mark"
                else:
                    ts_prefix = f"[{datetime.now().strftime('%H:%M:%S')}] "
                    insert_at = tk.END

                self.console.insert(insert_at, ts_prefix, "timestamp")
                self.console.insert(insert_at, text + "\n", tag)
                if found is not None:
                    try:
                        self.console.mark_unset("_boot_conn_mark")
                    except Exception:
                        pass
                self.console.configure(state=tk.DISABLED)
                if self.console_autoscroll_var.get():
                    self.console.see(tk.END)
            except tk.TclError:
                pass

        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _locate_hard_reset_recovery_images(self, board_name=None, board_info=None):
        """Return the dedicated Soft Reset recovery boot images for Hard Reset.

        Hard Reset deliberately does not depend on the active sketch's build.
        It reuses only bootloader.bin and partitions.bin from the persistent
        Soft Reset project.  firmware.bin is recorded for diagnostics but is
        never placed in the esptool write command.
        """
        board_name = board_name or self.board_var.get()
        board_info = dict(board_info or SUPPORTED_BOARDS.get(board_name, {}))
        selected_platform = str(board_info.get("platform", "")).strip().lower()
        selected_board_id = str(board_info.get("board", "")).strip().lower()
        if selected_platform != "espressif32":
            return None, "Dedicated recovery images are only used for ESP32-family boards."
        if not selected_board_id:
            return None, "The selected board has no PlatformIO board identifier."

        self._migrate_legacy_reset_project(board_name, board_info)
        project_dir = self._soft_reset_project_dir(board_name, board_info)
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

        desired_ini, desired_cpp, _monitor_speed = self._reset_project_contents(
            board_name, board_info
        )
        desired_template_hash = self._reset_template_digest(
            desired_ini, desired_cpp
        )
        if not manifest:
            return None, "The reset cache needs one incremental validation build."
        if manifest.get("board_key") != self._board_cache_key(board_name):
            return None, "The reset cache belongs to a different selectable board."
        if manifest.get("template_sha256") != desired_template_hash:
            return None, "The reset project configuration changed and must be rebuilt."
        current_platform_version = self._installed_platform_version(
            str(board_info.get("platform", ""))
        )
        cached_platform_version = str(manifest.get("platform_version", ""))
        if (
            current_platform_version
            and cached_platform_version
            and current_platform_version != cached_platform_version
        ):
            return None, "The board platform package changed and reset images must be rebuilt."

        try:
            if (project_dir / "platformio.ini").read_text(encoding="utf-8") != desired_ini:
                return None, "The reset PlatformIO configuration changed and must be rebuilt."
            if (project_dir / "main.cpp").read_text(encoding="utf-8") != desired_cpp:
                return None, "The reset sketch template changed and must be rebuilt."
        except OSError:
            return None, "The reset project files are incomplete."

        recovery_platform = str(manifest.get("platform", "")).strip().lower()
        recovery_board_id = str(manifest.get("board", "")).strip().lower()
        ini_path = project_dir / "platformio.ini"
        if ini_path.is_file() and (not recovery_platform or not recovery_board_id):
            try:
                ini_text = ini_path.read_text(encoding="utf-8", errors="replace")
                board_match = re.search(
                    r"^\s*board\s*=\s*([^;#\r\n]+)", ini_text,
                    re.IGNORECASE | re.MULTILINE,
                )
                platform_match = re.search(
                    r"^\s*platform\s*=\s*([^;#\r\n]+)", ini_text,
                    re.IGNORECASE | re.MULTILINE,
                )
                if board_match and not recovery_board_id:
                    recovery_board_id = board_match.group(1).strip().lower()
                if platform_match and not recovery_platform:
                    recovery_platform = platform_match.group(1).strip().lower()
            except Exception:
                pass

        if recovery_platform and recovery_platform != selected_platform:
            return None, (
                f"The installed recovery build targets {recovery_platform}, not "
                f"{selected_platform}."
            )
        if recovery_board_id and recovery_board_id != selected_board_id:
            return None, (
                f"The installed recovery build targets '{recovery_board_id}', but "
                f"the selected board uses '{selected_board_id}'. Run Soft Reset once "
                "for the selected board or install its matching recovery bundle."
            )

        bootloader = build_dir / "bootloader.bin"
        partitions = build_dir / "partitions.bin"
        firmware = build_dir / "firmware.bin"
        missing = [p.name for p in (bootloader, partitions) if not p.is_file()]
        if missing:
            return None, (
                "The dedicated Soft Reset recovery cache is incomplete: missing "
                + ", ".join(missing)
                + f" in {build_dir}."
            )

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
            "board_id": recovery_board_id or selected_board_id,
            "platform": recovery_platform or selected_platform,
            "partition_scheme": str(
                manifest.get("partition_scheme")
                or "ESP32 Dev Module default OTA partition table"
            ),
            "source_label": str(
                manifest.get("source_label") or "Soft Reset recovery build"
            ),
        }, ""

    def _build_hard_reset_recovery_images(self, board_name: str,
                                          board_info: dict):
        """Build the exact board's reset bundle on first Hard Reset use.

        This compiles only a tiny known-good reset sketch and never uses the
        active user's firmware.  Its project folder is also the one Soft Reset
        uses, so either operation warms the same exact-board cache.
        """
        project_dir = self._soft_reset_project_dir(board_name, board_info)
        self._migrate_legacy_reset_project(board_name, board_info)
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
            hide_generated_directory(project_dir.parent)
            hide_generated_directory(project_dir)
        except Exception as exc:
            return None, f"Could not create the reset cache folder: {exc}"

        ini_content, cpp_content, _monitor_speed = self._reset_project_contents(
            board_name, board_info
        )
        try:
            ini_path = project_dir / "platformio.ini"
            cpp_path = project_dir / "main.cpp"
            if (
                not ini_path.is_file()
                or ini_path.read_text(encoding="utf-8") != ini_content
            ) and not self._force_write_text(ini_path, ini_content):
                return None, "Could not update reset platformio.ini."
            if (
                not cpp_path.is_file()
                or cpp_path.read_text(encoding="utf-8") != cpp_content
            ) and not self._force_write_text(cpp_path, cpp_content):
                return None, "Could not update reset main.cpp."
        except Exception as exc:
            return None, f"Could not prepare reset project files: {exc}"

        pio_path = find_pio_executable() or ensure_platformio()
        if not pio_path:
            return None, "PlatformIO is unavailable."
        jobs = self._get_cpu_cores_jobs()
        cmd = pio_path + ["run", "-e", "mcu_flash", "-j", str(jobs)]
        self._append(
            "  🔧 First Hard/Soft Reset for this board — building its persistent recovery cache…",
            "warning",
        )
        self._set_status("Building exact-board reset cache...", Theme.YELLOW)
        output_lines: list[str] = []
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
                env=self._reset_platformio_subprocess_env(project_dir, jobs),
            )
            for raw in iter(self.process.stdout.readline, ""):
                line = raw.rstrip()
                if not line:
                    continue
                output_lines.append(line)
                low = line.lower()
                if any(token in low for token in (
                    "compiling", "linking", "building", "error", "warning", "success", "took"
                )):
                    tag = "error" if "error" in low else (
                        "warning" if "warning" in low else "dim"
                    )
                    self._append(f"  {line}", tag)
            self.process.wait()
        except Exception as exc:
            return None, f"Reset cache build could not start: {exc}"
        if self.process.returncode != 0:
            tail = next(
                (line for line in reversed(output_lines) if "error" in line.lower()),
                f"PlatformIO exited with code {self.process.returncode}",
            )
            return None, f"Reset cache build failed: {tail}"
        manifest_ok, manifest_error = self._write_reset_manifest(
            project_dir, board_name, board_info
        )
        if not manifest_ok:
            return None, manifest_error
        images, error = self._locate_hard_reset_recovery_images(
            board_name, board_info
        )
        if images:
            self._append("  ✔ Exact-board reset cache built and saved for future use.", "success")
        return images, error

    def _do_hard_reset(self, parent_dlg):
        # A boot loop is a valid reason to perform the ESP32 full-erase hard reset.
        # Do not block the operation merely because serial output is repeating.

        # 1. Check if busy (with auto-recovery for stale busy flag)
        if self.is_busy:
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._append("  ℹ Stale busy state cleared — proceeding.", "info")
            else:
                from tkinter import messagebox
                messagebox.showwarning("Busy", "The programmer is currently busy with another operation.", parent=parent_dlg)
                return

        if not self.board_var.get():
            from tkinter import messagebox
            messagebox.showwarning(
                "No Board Selected",
                "Please choose a board in the main window before performing a Hard Reset.",
                parent=parent_dlg
            )
            return

        port = self._get_port()
        if not port:
            from tkinter import messagebox
            messagebox.showwarning("No Port Selected", "Please select a serial port in the main window before resetting.", parent=parent_dlg)
            return

        if not self._is_board_recognized():
            from tkinter import messagebox
            messagebox.showwarning(
                "Board Not Recognized",
                "The board on this port hasn't been recognized yet.\n\n"
                "Wait for auto-detect to finish, or verify the correct board/port is selected.",
                parent=parent_dlg
            )
            return

        if not self._is_port_present(port):
            from tkinter import messagebox
            messagebox.showwarning(
                "Port Disconnected",
                f"The selected port '{port}' is not connected.\n\n"
                "Please connect your MCU physical board before performing a Hard Reset.",
                parent=parent_dlg
            )
            return

        # ESP32 Hard Reset uses a dedicated, persistent recovery build instead
        # of whichever sketch happens to be open.  Only its bootloader and
        # partition table are used; its application firmware is never written.
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        platform_name = str(board_info.get("platform", "")).lower()
        self._hard_reset_recovery_images = None
        if platform_name == "espressif32":
            recovery_images, _recovery_error = self._locate_hard_reset_recovery_images(
                board_name, board_info
            )
            self._hard_reset_recovery_images = recovery_images

        # 2. Show confirmation
        from tkinter import messagebox
        confirm = messagebox.askyesno(
            "ESP32 Full Erase + Burn Bootloader",
            "This is a destructive hard reset. It will erase the ENTIRE ESP32 flash, "
            "including the current application, NVS settings, OTA state, and filesystem "
            "data.\n\n"
            "After erasing, it will write only bootloader.bin and partitions.bin from "
            "the dedicated compiled Soft Reset recovery project, plus boot_app0. The "
            "Soft Reset application firmware and the active project firmware will NOT "
            "be uploaded.\n\n"
            "If this board has no saved reset cache yet, the app will build its tiny "
            "known-good recovery project once before erasing anything. Future Hard/Soft "
            "Resets will reuse that exact board folder.\n\n"
            "After preparation, esptool will show a live BOOT connection indicator. "
            "Press and HOLD BOOT until the indicator turns green, then release it.\n\n"
            "Continue with the full erase?",
            parent=parent_dlg
        )
        if not confirm:
            return

        # Close settings dialog to let the user see the console output
        parent_dlg.destroy()

        self._clear_console_if_action_enabled()

        # Run hard reset in background thread
        self._active_reset_kind = "hard"
        self.is_busy = True
        self._set_buttons_state(True, operation="reset")
        threading.Thread(target=self._run_hard_reset, args=(port,), daemon=True).start()

    def _run_hard_reset(self, port: str):
        # _pause_monitor() runs inside the worker and clears the monitor intent.
        # Record whether it must be restored, then perform that restoration only
        # after the reset operation is no longer busy.  Scheduling it earlier is
        # silently discarded by _auto_start_monitor() while operation="reset".
        self._hard_reset_reconnect_monitor = False
        self._hard_reset_completed_successfully = False
        reset_cache_lock = _try_acquire_reset_cache_lock()
        try:
            if reset_cache_lock is None:
                self._append(
                    "  ⚠ Hard Reset blocked: another window is using the shared reset cache.",
                    "warning",
                )
                self._set_status("Hard Reset blocked — reset cache is in use", Theme.YELLOW)
                return
            self._run_hard_reset_inner(port)
        except Exception as e:
            import traceback
            try:
                hard_reset_log = SCRIPT_DIR / "logs" / "hard_reset_error.log"
                hard_reset_log.parent.mkdir(parents=True, exist_ok=True)
                with open(hard_reset_log, "w", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
            except Exception:
                hard_reset_log = None
            self._append(f"Hard reset error: {e}", "error")
            if hard_reset_log:
                self._append(f"Log: {hard_reset_log}", "dim")
            self._set_status("Hard Reset FAILED", Theme.RED)
        finally:
            _release_reset_cache_lock(reset_cache_lock)
            # Clean up any temporary UNC drive mapping created during hard reset.
            self._unmap_unc_after_build()
            reconnect_monitor = bool(
                getattr(self, "_hard_reset_reconnect_monitor", False)
            )
            hard_reset_ok = bool(
                getattr(self, "_hard_reset_completed_successfully", False)
            )
            self._hard_reset_reconnect_monitor = False
            self._hard_reset_completed_successfully = False

            # Set the one-shot focus target before the unlock callback runs.
            if hard_reset_ok:
                self._focus_tab_on_unlock = self._serial_monitor_tab_index()

            # Clear both is_busy and _active_operation before asking the monitor
            # to reopen COM.  Otherwise _auto_start_monitor() exits immediately
            # and leaves the Serial Monitor tab permanently Disconnected.
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_window_closable(True)

            if hard_reset_ok:
                self._activate_serial_monitor_after_success("Hard Reset")

            if reconnect_monitor:
                self._monitor_should_run = True
                # The final DTR/RTS pulse was already sent by Hard Reset.  Reopen
                # the monitor without issuing a second reset pulse.
                self._manual_reset_pending = False

                def _restore_hard_reset_monitor():
                    self._append_notif(
                        "  ↻ Reconnecting Serial Monitor after Hard Reset…",
                        "dim",
                    )
                    self._schedule_auto_start_monitor(250)

                try:
                    self.root.after(0, _restore_hard_reset_monitor)
                except Exception:
                    pass

    def _esptool_target(self, board_name: str | None = None,
                        board_info: dict | None = None) -> tuple[str | None, str]:
        """Resolve esptool chip name and bootloader offset from canonical ids."""
        name = board_name if board_name is not None else self.board_var.get()
        info = dict(board_info or SUPPORTED_BOARDS.get(name, {}))
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
        # Prefer the importable module under the GUI's trusted Python
        # interpreter. Generated console-script .exe launchers are commonly
        # blocked by Windows Application Control even when the Python module
        # itself is allowed.
        try:
            import importlib.util
            if importlib.util.find_spec("esptool") is not None:
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

        import shutil
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

    def _run_hard_reset_inner(self, port: str):
        """Perform the ESP32 equivalent of a clean bootloader burn.

        Esptool has no separate ``burn-bootloader`` command for ESP32. The safe
        serial equivalent is one ``write-flash --erase-all`` transaction that
        erases every flash sector, then writes only the dedicated Soft Reset
        recovery build's second-stage bootloader and partition table plus boot_app0.
        Neither the recovery firmware nor the active sketch firmware is written.
        """
        owner_pid = port_occupied_owner(port)
        if owner_pid:
            self._append(
                f"  ⚠ Hard reset blocked: Port '{port}' is in use by another "
                f"window (PID {owner_pid}).",
                "warning",
            )
            self._set_status("Hard Reset blocked", Theme.RED)
            return

        was_monitoring = self._pause_monitor()
        # Hand this state back to _run_hard_reset(), whose finally block runs
        # after the operation has cleared its busy/reset guard.  Restoring the
        # monitor from this inner worker is too early and gets rejected.
        self._hard_reset_reconnect_monitor = bool(was_monitoring)
        boot_images_written = False
        if was_monitoring:
            time.sleep(0.5)

        try:
            board_name = self.board_var.get()
            board_info = SUPPORTED_BOARDS.get(board_name, {})
            platform_name = str(board_info.get("platform", "")).lower()
            is_avr = platform_name == "atmelavr"

            self._append("")
            self._append("=" * 50, "header")
            self._append("  🔥 BURNING BOOTLOADER (Hard Reset)", "header")
            self._append("=" * 50, "header")
            self._append(f"  Port  : {port}", "port_highlight")
            self._append(f"  Board : {board_name}", "dim")

            try:
                # pyrefly: ignore [missing-import]
                from detector import is_laptop
                system_is_laptop = is_laptop()
            except Exception:
                system_is_laptop = False
            if not system_is_laptop:
                self._append(
                    "  💡 Press and HOLD the BOOT button when prompted below.",
                    "warning",
                )
            self._append("")

            if is_avr:
                self._append("  🔧 Starting the normal PlatformIO bootloader target...", "info")
                if not self._ensure_platformio_ini():
                    self._append("  ✖ Could not prepare platformio.ini.", "error")
                    self._set_status("Hard Reset failed", Theme.RED)
                    return
                pio_path = find_pio_executable() or ensure_platformio()
                if not pio_path:
                    self._append("  ✖ PlatformIO is unavailable.", "error")
                    self._set_status("Hard Reset failed", Theme.RED)
                    return
                cmd = pio_path + [
                    "run", "-e", self._pio_env_name(), "-t", "bootloader",
                    "-j", str(self._get_cpu_cores_jobs()),
                    "--upload-port", port,
                ]
                effective_cwd = self._map_unc_for_build()
                pio_env = self._platformio_subprocess_env(
                    project_dir=effective_cwd,
                    jobs=self._get_cpu_cores_jobs()
                )
                self.process = subprocess.Popen(
                    cmd,
                    cwd=str(effective_cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                    env=pio_env,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                    ),
                )
                for line in iter(self.process.stdout.readline, ""):
                    stripped = line.rstrip()
                    if stripped:
                        low = stripped.lower()
                        tag = "error" if "error" in low or "failed" in low else "normal"
                        self._append(stripped, tag)
                self.process.wait()
                if self.process.returncode != 0:
                    self._append(
                        f"  ✖ Bootloader burn failed with exit code {self.process.returncode}.",
                        "error",
                    )
                    self._set_status("Hard Reset failed", Theme.RED)
                    return

                self._append("  🔄 Resetting board through DTR...", "info")
                time.sleep(0.25)
                reset_ok = self._trigger_actual_board_reset(port)
                if not reset_ok:
                    self._set_status("Bootloader burned - DTR reset failed", Theme.YELLOW)
                    return
                self._hard_reset_completed_successfully = True
                self._append("  ✔ Bootloader burn completed successfully.", "success")
                self._set_status("Bootloader burn successful", Theme.GREEN)
                return

            if platform_name != "espressif32":
                self._append(
                    f"  ✖ Normal bootloader burn is not implemented for platform "
                    f"'{platform_name or 'unknown'}'.",
                    "error",
                )
                self._set_status("Hard Reset unsupported", Theme.RED)
                return

            # _do_hard_reset() normally preloads and validates the dedicated
            # recovery image set before starting this worker.  Initialize the
            # local on every ESP32 path so a missing/stale handoff can safely
            # fall back to locating the bundle instead of raising
            # UnboundLocalError before any flash operation begins.
            image_set = getattr(self, "_hard_reset_recovery_images", None)
            self._hard_reset_recovery_images = None
            recovery_error = ""
            if not image_set:
                image_set, recovery_error = self._locate_hard_reset_recovery_images(
                    board_name, board_info
                )
            if not image_set:
                image_set, recovery_error = self._build_hard_reset_recovery_images(
                    board_name, board_info
                )

            if not image_set:
                self._append("  ✖ Dedicated Hard Reset recovery images are unavailable.", "error")
                if recovery_error:
                    self._append(f"  {recovery_error}", "warning")
                self._append("  No flash data was erased or written.", "success")
                self._set_status("Hard Reset recovery images missing", Theme.RED)
                return

            bootloader_bin = Path(image_set["bootloader"])
            partitions_bin = Path(image_set["partitions"])
            recovery_firmware_bin = image_set.get("firmware")
            boot_app0_bin = self._locate_esp32_boot_app0()
            if boot_app0_bin is None:
                self._append("  ✖ Could not locate boot_app0.bin.", "error")
                self._set_status("Hard Reset failed", Theme.RED)
                return

            def _validate_image(path: Path, label: str, minimum: int, magic=None):
                try:
                    data = path.read_bytes()
                except Exception as exc:
                    self._append(f"  ✖ Could not read {label}: {exc}", "error")
                    return False
                if len(data) < minimum:
                    self._append(
                        f"  ✖ {label} is truncated ({len(data)} bytes).",
                        "error",
                    )
                    return False
                if magic is not None and not data.startswith(magic):
                    self._append(f"  ✖ {label} has an invalid file header.", "error")
                    return False
                return True

            def _validate_partition_table(path: Path):
                try:
                    data = path.read_bytes()
                except Exception as exc:
                    self._append(f"  ✖ Could not read partitions.bin: {exc}", "error")
                    return False
                if len(data) < 0xC00 or data[:2] != b"\xAA\x50":
                    self._append(
                        "  ✖ partitions.bin is not a valid ESP32 partition table.",
                        "error",
                    )
                    return False
                md5_marker = data.find(b"\xEB\xEB")
                if md5_marker < 0 or md5_marker % 32 != 0 or md5_marker + 32 > len(data):
                    self._append(
                        "  ✖ partitions.bin has no valid MD5 record; refusing to flash it.",
                        "error",
                    )
                    return False
                expected = data[md5_marker + 16:md5_marker + 32]
                actual = hashlib.md5(data[:md5_marker]).digest()
                if expected != actual:
                    self._append(
                        "  ✖ partitions.bin failed its MD5 integrity check.",
                        "error",
                    )
                    return False
                return True

            if not all((
                _validate_image(bootloader_bin, "bootloader.bin", 4096, b"\xE9"),
                _validate_partition_table(partitions_bin),
                _validate_image(boot_app0_bin, "boot_app0.bin", 1024),
            )):
                self._append("  ✖ Hard Reset stopped before writing anything.", "error")
                self._set_status("Hard Reset failed", Theme.RED)
                return

            partition_scheme = image_set.get("partition_scheme") or "recovery default"
            source_label = image_set.get("source_label") or "Soft Reset recovery build"
            self._append(f"  ⚡ Using dedicated {source_label}.", "info")
            self._append(f"  Recovery ID: {image_set.get('board_id') or 'unknown'}", "dim")
            self._append(f"  Bootloader : {bootloader_bin.name}", "dim")
            self._append(
                f"  Partitions : {partitions_bin.name} ({partition_scheme})",
                "dim",
            )
            self._append(f"  boot_app0  : {boot_app0_bin.name}", "dim")
            self._append(
                "  Firmware   : ERASED — recovery firmware is NOT written",
                "warning",
            )
            if recovery_firmware_bin:
                self._append(
                    f"  Recovery app: {Path(recovery_firmware_bin).name} stays on disk only",
                    "dim",
                )
            self._append("  Data       : ERASED — NVS, OTA state, and filesystem", "warning")
            self._append("  Active project build is not used by Hard Reset.", "success")
            self._append("  The next normal Upload will write that project's own images.", "dim")
            self._append("")
            self._append("  ✔ Required recovery boot images are ready.", "success")
            self._append("  ⚠ Press and HOLD the BOOT button now.", "warning")
            self._append(
                "  🔌 Esptool is starting now and will wait for ESP32 download mode.",
                "info",
            )
            self._append("  Release BOOT when the connection indicator turns green.", "dim")
            self._set_status("Waiting for BOOT / ESP32 download mode...", Theme.YELLOW)
            self._append_boot_connection_progress(0)

            target_mcu, bootloader_addr = self._esptool_target(
                board_name, board_info
            )
            target_mcu = target_mcu or "esp32"
            burn_cmd = self._get_esptool_cmd() + [
                "--chip", target_mcu,
                "--port", port,
                "--baud", "115200",
                "--before", "default-reset",
                "--after", "no-reset",
                "--connect-attempts", "30",
                "write-flash",
                "--erase-all",
                "--flash-mode", "keep",
                "--flash-freq", "keep",
                "--flash-size", "detect",
                bootloader_addr, str(bootloader_bin),
                "0x8000", str(partitions_bin),
                "0xe000", str(boot_app0_bin),
            ]

            progress_state = {
                "stages": [
                    {
                        "key": "bootloader",
                        "label": "Bootloader",
                        "path": bootloader_bin,
                        "basename": bootloader_bin.name.lower(),
                    },
                    {
                        "key": "partitions",
                        "label": "Partitions",
                        "path": partitions_bin,
                        "basename": partitions_bin.name.lower(),
                    },
                    {
                        "key": "boot_app0",
                        "label": "Boot App",
                        "path": boot_app0_bin,
                        "basename": boot_app0_bin.name.lower(),
                    },
                ],
                "active_index": 0,
                "started": False,
                "last_percent": None,
                "compressed_total": None,
            }

            erase_progress_started = False
            erase_progress_completed = False

            # Read esptool one byte at a time so its real ``Connecting....``
            # dots drive the GUI indicator immediately instead of being hidden
            # inside readline() until the connection attempt ends.
            self.process = subprocess.Popen(
                burn_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )

            import codecs
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            line_buffer = ""
            connection_step = 0
            connected_to_chip = False

            def _mark_connected():
                nonlocal connected_to_chip
                if connected_to_chip:
                    return
                connected_to_chip = True
                self._append_boot_connection_progress(connection_step, connected=True)
                self._append("  ✔ ESP32 connected — release the BOOT button now.", "success")
                self._set_status("Connected - release BOOT", Theme.GREEN)

            def _handle_esptool_line(stripped: str):
                nonlocal erase_progress_started, erase_progress_completed
                nonlocal connection_step, connected_to_chip
                stripped = stripped.rstrip()
                if not stripped:
                    return
                low = stripped.lower()

                # Suppress esptool's raw dot row; each real dot has already
                # advanced the single live Boot connection bar above.
                if low.startswith("connecting"):
                    connection_step = max(connection_step, stripped.count("."))
                    self._append_boot_connection_progress(connection_step)
                    return

                if (
                    "connected to " in low
                    or "uploading stub" in low
                    or "stub flasher running" in low
                ):
                    _mark_connected()

                if self._consume_esptool_upload_progress(
                    progress_state,
                    stripped,
                    phase_callback=lambda phase: self._set_status(
                        f"{phase} boot images...", Theme.YELLOW
                    ),
                ):
                    return

                erase_finished = (
                    "flash memory erased successfully" in low
                    or "chip erase completed successfully" in low
                    or ("erase" in low and "completed" in low and "success" in low)
                )
                if erase_finished:
                    erase_progress_started = True
                    erase_progress_completed = True
                    self._set_status("Full flash erase complete", Theme.GREEN)
                    self._append_progress(
                        "  ✔ Erasing flash [ ██████████████████████████████ ] | 100% Complete",
                        "success",
                        action_type="erasing flash",
                    )
                    self._append(stripped, "success")
                    return
                if "erasing flash" in low or "chip erase" in low:
                    erase_progress_started = True
                    self._set_status("Erasing entire flash...", Theme.YELLOW)
                    self._append_progress(
                        "  Erasing flash [ ███████████████░░░░░░░░░░░░░░░ ] | Please wait",
                        "warning",
                        action_type="erasing flash",
                    )
                    return
                if "connected to " in low or "hash of data verified" in low:
                    tag = "success"
                elif "error" in low or "failed" in low or "fatal" in low:
                    tag = "error"
                elif "uploading stub" in low:
                    tag = "magenta"
                elif low.startswith(("chip type", "features", "crystal", "mac", "serial port")):
                    tag = "dim"
                else:
                    tag = "normal"
                self._append(stripped, tag)

            while True:
                raw = self.process.stdout.read(1)
                if not raw:
                    break
                decoded = decoder.decode(raw)
                for char in decoded:
                    if char in "\r\n":
                        if line_buffer:
                            _handle_esptool_line(line_buffer)
                            line_buffer = ""
                        continue
                    line_buffer += char
                    if line_buffer.lower().startswith("connecting") and char == ".":
                        connection_step += 1
                        self._append_boot_connection_progress(connection_step)

            tail = decoder.decode(b"", final=True)
            if tail:
                line_buffer += tail
            if line_buffer:
                _handle_esptool_line(line_buffer)

            self.process.wait()
            if self.process.returncode != 0:
                if not connected_to_chip:
                    self._append_boot_connection_progress(connection_step, failed=True)
                self._append(
                    f"  ✖ Bootloader burn failed with exit code {self.process.returncode}.",
                    "error",
                )
                self._set_status("Bootloader burn failed", Theme.RED)
                return

            # A zero exit status proves esptool connected even if a future
            # release changes the exact wording of its Connected line.
            if not connected_to_chip:
                _mark_connected()

            # Some esptool releases do not emit a separate parseable erase-done
            # line.  A successful write-flash --erase-all return code still means
            # the erase completed, so never leave the visual bar half-filled.
            if erase_progress_started and not erase_progress_completed:
                self._append_progress(
                    "  ✔ Erasing flash [ ██████████████████████████████ ] | 100% Complete",
                    "success",
                    action_type="erasing flash",
                )
                erase_progress_completed = True

            boot_images_written = True
            self._append("  🔄 Boot images written successfully.", "success")
            self._append("  🔄 Performing final Reset(DTR/RTS)...", "info")
            self._set_status("Resetting board through DTR/RTS...", Theme.YELLOW)
            # Give Windows/esptool enough time to release the COM handle before
            # reopening it for the explicit post-flash reset pulse.
            time.sleep(0.75)
            reset_ok = self._trigger_actual_board_reset(port)
            if not reset_ok:
                self._append(
                    "  ⚠ Flashing succeeded, but the automatic Reset(DTR/RTS) did not complete.",
                    "warning",
                )
                self._append(
                    "  Press the board's EN/RESET button once, or reconnect USB.",
                    "info",
                )
                self._set_status("Hard Reset flashed - DTR reset failed", Theme.YELLOW)
                return

            self._hard_reset_completed_successfully = True
            self._append("  ✔ Full erase, bootloader burn, and Reset(DTR/RTS) completed successfully.", "success")
            self._append("  ℹ The ESP32 is intentionally blank. Use Upload to install a project sketch.", "info")
            self._append("  ℹ No Soft Reset application firmware was uploaded during Hard Reset.", "dim")
            if was_monitoring:
                self._append("  🔌 Reconnecting the Serial Monitor…", "info")
                self._set_status("Hard Reset complete - reconnecting monitor", Theme.GREEN)
            else:
                self._append("  ℹ Serial Monitor was not running before Hard Reset.", "dim")
                self._set_status("Hard Reset complete - board blank", Theme.GREEN)
        finally:
            # Always preserve the user's pre-reset monitor state.  The outer
            # worker restores it only after is_busy/_active_operation are clear.
            self._hard_reset_reconnect_monitor = bool(was_monitoring)

