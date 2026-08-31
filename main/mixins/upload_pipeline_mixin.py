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
import textwrap
from typing import TYPE_CHECKING
from tkinter import messagebox


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

class UploadPipelineMixin(_Base):
    """Mixin providing UploadPipelineMixin capabilities for MCUUploadGUI."""
    def _probe_chip_info(self, port: str) -> None:
        """Connect to the chip via esptool and print hardware info to the
        build console.  Called just before PlatformIO's upload so the port
        is free (monitor already paused).  Never raises — failures are shown
        as a warning and upload continues normally."""
        esp = None
        try:
            # pyrefly: ignore [missing-import]
            import esptool

            # esptool ≥ 4.x exposes get_default_connected_device(); older
            # versions don't.  Fall back gracefully when it's absent.
            if not hasattr(esptool, "get_default_connected_device"):
                self._append("  ⚠ esptool version too old for chip probe (need ≥ 4.x).", "warning")
                return

            # One-shot retry on the initial connect: a failure here right
            # after the monitor port was closed is often a transient
            # reset-timing hiccup (corrupted DTR/RTS pulse from reopening
            # the port too quickly) rather than a real hardware problem —
            # give it a second chance before giving up.
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

            # Upload the stub firmware so that flash-access commands
            # (detect_flash_size / flash_id) are available.  Without the stub
            # those calls silently fail on most ESP32 variants because the ROM
            # loader doesn't implement the underlying SPI commands.
            # run_stub() returns the stub object; fall back to the ROM loader
            # if it's unavailable (very old esptool or unsupported chip rev).
            try:
                esp = esp.run_stub()
            except Exception:
                pass  # keep the ROM-loader object and try anyway

            chip_model = getattr(esp, "CHIP_NAME", "Unknown")

            try:
                features = ", ".join(esp.get_chip_features())
            except Exception:
                features = "N/A"

            try:
                mac_bytes = esp.read_mac()
                mac = ":".join(f"{b:02X}" for b in mac_bytes)
            except Exception:
                mac = "N/A"

            try:
                crystal = esp.get_crystal_freq()
                crystal_str = f"{crystal} MHz"
            except Exception:
                crystal_str = "N/A"

            try:
                # In esptool 5.x, flash size is read via esptool.cmds.detect_flash_size(esp),
                # a module-level function (NOT a method on esp).
                # It requires attach_flash(esp) first to enable SPI access on the stub.
                # Fallback: esp.flash_id() returns a 24-bit int where bits[23:16] are
                # the capacity/size_id looked up in DETECTED_FLASH_SIZES.
                # bits[7:0] are the vendor ID — the old code read the wrong byte.
                flash_str = "N/A"
                try:
                    # pyrefly: ignore [missing-import]
                    from esptool.cmds import detect_flash_size, attach_flash
                    attach_flash(esp)                 # arms SPI flash access on stub
                    detected = detect_flash_size(esp) # returns e.g. "8MB" or None
                    if detected:
                        flash_str = detected
                except Exception:
                    pass

                if flash_str == "N/A":
                    # Fallback: decode JEDEC size_id from bits[23:16] of flash_id()
                    try:
                        # pyrefly: ignore [missing-import]
                        from esptool.cmds import DETECTED_FLASH_SIZES
                        raw = esp.flash_id()
                        size_id = (raw >> 16) & 0xFF   # capacity byte is the HIGH byte
                        flash_str = DETECTED_FLASH_SIZES.get(size_id, "N/A")
                    except Exception:
                        pass
            except Exception:
                flash_str = "N/A"

            # ── Print to build console as a boxed info panel ───────────────
            self._print_chip_info_box(chip_model, [
                ("Chip Model",  chip_model),
                ("Features",    features),
                ("MAC Address", mac),
                ("Crystal",     crystal_str),
                ("Flash Size",  flash_str),
            ])

        except Exception as e:
            self._append(f"  ⚠ Chip probe failed: {e}", "warning")
            self._append("  (Continuing with upload anyway...)", "dim")
        finally:
            if esp is not None:
                try:
                    esp._port.close()
                except Exception:
                    pass
            # Same port-release race as before the probe: give Windows a
            # moment to fully free the handle before PlatformIO's esptool
            # subprocess reopens it for the actual upload.
            time.sleep(0.3)

    def _print_info_box(self, title: str, fields: list[tuple[str, str]]) -> None:
        """
        Render a boxed information panel into the build console using double-line border.
        """
        if not fields:
            return
        label_width = max(len(label) for label, _ in fields)
        label_col_width = label_width + 3  # "label" + " : "

        available_cols = self._console_box_columns()
        max_inner_width = max(available_cols - 4, label_col_width + 10, len(title))
        value_max_width = max(max_inner_width - label_col_width, 10)

        rows = []  # list of (label_field, value_chunk) — one per physical line
        for label, value in fields:
            chunks = textwrap.wrap(str(value), value_max_width) or [""]
            for i, chunk in enumerate(chunks):
                label_field = f"{label:<{label_width}} : " if i == 0 else " " * label_col_width
                rows.append((label_field, chunk))

        inner_width = max(len(title), max(len(lf) + len(v) for lf, v in rows))

        top    = "╔" + "═" * (inner_width + 2) + "╗"
        title_row = "║ " + title.center(inner_width) + " ║"
        sep    = "╠" + "═" * (inner_width + 2) + "╣"
        bottom = "╚" + "═" * (inner_width + 2) + "╝"

        self._append("")
        self._append(top, "purple_header")
        self._append(title_row, "purple_header")
        self._append(sep, "purple_header")
        for label_field, value_chunk in rows:
            value_field = value_chunk.ljust(inner_width - len(label_field))
            self._append_segments([
                ("║ ", "purple_header"),
                (label_field, "purple_info"),
                (value_field, "purple_value"),
                (" ║", "purple_header"),
            ])
        self._append(bottom, "purple_header")
        self._append("")

    def _print_chip_info_box(self, chip_model: str, fields: list[tuple[str, str]]) -> None:
        """Render a boxed chip-info panel into the build console."""
        self._print_info_box(f"{chip_model} Information", fields)

    # ──────────────────────────────────────────────────────────
    # UPLOAD
    # ──────────────────────────────────────────────────────────
    def _abort_upload_if_mcu_missing(self, port: str, *, compiled_fresh: bool) -> bool:
        """Flash-phase gate for the two-phase Upload pipeline.

        Upload runs in up to two phases: compile (only when sources changed;
        skipped entirely when a cached build can be reused) and flash.  Only
        the flash phase needs the physical board, so an unplug mid-compile
        must NOT abort the build — the detach watchdog in _run_upload records
        the disappearance, and this gate then skips just the flash phase once
        compilation completes.  The same gate also re-validates the port right
        before the uploader is spawned (a long compatibility prompt can sit
        between the two checks).

        Returns True when the upload was aborted because the target MCU port
        vanished (either during the compile phase or right now); returns False
        when the board is still connected and flashing may proceed.
        """
        detached_port = getattr(self, "_mcu_detached_during_compile", None)
        present_now = self._is_port_present(port)
        if present_now and not detached_port:
            return False

        lost_port = detached_port or port
        self._append("")
        self._append("─" * 50, "warning")
        if detached_port:
            self._append(f"  ⚠ Upload skipped: the MCU on {detached_port} was disconnected while compiling.", "warning")
        else:
            self._append(f"  ⚠ Upload aborted: the MCU on {port} is no longer connected.", "warning")
        if compiled_fresh:
            self._append("  ✔ Compile phase finished — the fresh build is cached and stays valid.", "success")
        if present_now:
            self._append("  💡 The board is back — press Upload again; Skip Compile reuses the cached build.", "info")
        else:
            self._append("  💡 Reconnect the board, then press Upload again — Skip Compile reuses the cached build.", "info")
        self._append("─" * 50, "warning")

        if compiled_fresh:
            self._set_status("Compile OK — upload skipped (MCU disconnected)", Theme.YELLOW)
        else:
            self._set_status("Upload aborted — MCU disconnected", Theme.YELLOW)
        self._append_notif(
            f"  ⚠ Upload skipped: MCU on {lost_port} disconnected before flashing.",
            "warning",
        )

        self.is_busy = False
        self._current_op_phase = None
        self._set_buttons_busy(False)
        self._set_buttons_state(False)
        return True

    def _run_upload(self, port: str) -> bool:
        self._stop_requested = False
        self._op_session_id = getattr(self, "_op_session_id", 0) + 1
        self._current_op_phase = "compiling"
        self._active_reset_kind = None
        # Set by the detach watchdog below when the target port vanishes
        # mid-compile; consumed by _abort_upload_if_mcu_missing().
        self._mcu_detached_during_compile = None
        self._set_window_closable(True)

        # Remote projects use a temporary mapped drive for the source snapshot.
        # Keep it mounted for the whole upload because this operation may call
        # _run_compile() before it reaches the uploader.
        if is_unc_or_network_path(self.sketch_dir_path):
            self._map_unc_for_build(self.sketch_dir_path)

        # ── Smart compile check (upload path) ──────────────────────────────
        need_compile = True
        if self.skip_compile_var.get() and self._has_prior_build():
            # The root ini may still describe board B while this exact cached
            # binary belongs to board A.  Restore/reconcile A first so both
            # the config fingerprint and PlatformIO's fallback uploader see
            # the correct environment.  This is intentionally done before
            # deciding whether the compile can be skipped.
            if not self._ensure_platformio_ini():
                self._append("  ✖ Could not prepare the selected board configuration.", "error")
                self.is_busy = False
                self._set_buttons_busy(False)
                self._set_buttons_state(False)
                self._set_status("Upload failed: board configuration", Theme.RED)
                return False
            recompile_needed, reason = self._needs_recompile()
            if not recompile_needed:
                self._append("")
                self._append("  ✔ Sources unchanged — skipping recompile", "success_bold_lg")
                self._set_status("Sources up-to-date, uploading...", Theme.CYAN)
                need_compile = False
            else:
                self._append("")
                self._append(f"  🔄 Recompile needed ({reason})", "warning")
        elif not self.skip_compile_var.get():
            pass

        # ── MCU detachment watchdog (compile phase of Upload) ─────────────
        # Compilation never talks to the board, so an unplug mid-compile must
        # NOT abort the build.  The watchdog instead records the disappearance
        # and the gate below then skips only the flash phase.  A cached-build
        # upload has no compile window to watch; the gate still validates
        # presence on its own.
        _detach_watch_stop = threading.Event()

        def _mcu_detach_watchdog():
            while not _detach_watch_stop.wait(0.6):
                if not self._is_port_present(port):
                    self._mcu_detached_during_compile = port
                    self._append("")
                    self._append(f"  ⚠ MCU disconnected from {port} while compiling!", "warning")
                    self._append("    Compile continues — it does not need the board —", "warning")
                    self._append("    but the upload phase will be skipped afterwards.", "warning")
                    return

        if need_compile:
            _detach_watchdog_thread = threading.Thread(
                target=_mcu_detach_watchdog,
                name="MCUDetachWatchdog",
                daemon=True,
            )
            _detach_watchdog_thread.start()
            try:
                compile_result = self._run_compile(is_upload=True)
                if compile_result is None:
                    # Explicit cache-integrity failure: _run_compile repaired only
                    # the selected board workspace. Retry once; siblings survive.
                    self._append("  🔄 Retrying after selected-board cache repair…", "info")
                    compile_result = self._run_compile(is_upload=True)
            finally:
                _detach_watch_stop.set()
                _detach_watchdog_thread.join(timeout=2)
            if not compile_result:
                self.is_busy = False
                self._set_buttons_busy(False)
                self._set_buttons_state(False)
                return False
        else:
            # A cached-build upload still needs an isolated src/ tree.  Older
            # releases may have left hard links there, and PlatformIO's
            # compatibility uploader is allowed to inspect/rebuild the project.
            if not self._freeze_build_sources_at_boundary("Upload"):
                self.is_busy = False
                self._set_buttons_busy(False)
                self._set_buttons_state(False)
                self._set_status("Upload paused for AI review", Theme.YELLOW)
                return False

        # ── Gate 1: compile done (or reused) — is the MCU still attached? ──
        # When the board was unplugged during compilation the build above has
        # finished normally and its cache snapshot is already saved, so a
        # re-upload after replugging goes straight to flashing.
        if self._abort_upload_if_mcu_missing(port, compiled_fresh=need_compile):
            return False

        # ── Sketch-vs-board compatibility guard (run before starting upload timer) ──
        selected_board = self.board_var.get()
        compat_boards, compat_reasons = self._get_compat_analysis()
        
        has_warnings = False
        warnings_list = []
        if compat_boards and selected_board not in compat_boards:
            has_warnings = True
            compat_label = _format_compat_label(compat_boards)
            warnings_list.append(f"Selected board \"{selected_board}\" is not officially supported by this sketch.")
            warnings_list.append(f"This sketch is only compatible with: {compat_label}")
        
        current_hash = self._hash_sources()
        if compat_reasons and self._compat_warnings_approved_hash != current_hash:
            relevant_reasons = []
            for r in compat_reasons:
                if selected_board in compat_boards:
                    # If the selected board is compatible, only keep actual warnings (starting with ⚠)
                    # that are relevant to this board type
                    if r.startswith("⚠"):
                        board_prefix = selected_board.split()[0].lower()
                        if board_prefix in r.lower() or selected_board.lower() in r.lower():
                            relevant_reasons.append(r)
                else:
                    # If selected board is not compatible, all reasons are relevant
                    relevant_reasons.append(r)
            
            if relevant_reasons:
                has_warnings = True
                for r in relevant_reasons:
                    warnings_list.append(r)

        if has_warnings:
            self._set_status("Waiting for compatibility confirmation...", Theme.YELLOW)
            # Split by severity: reasons starting with "⚠" are soft cautions
            # (e.g. "GPIO reserved for SPI flash — may cause instability");
            # everything else is a hard exclusion (board not supported,
            # GPIO out of range, platform-exclusive APIs) that genuinely
            # warrants the blood-red banner.
            severe_reasons = [r for r in warnings_list if not r.startswith("⚠")]
            soft_reasons = [r for r in warnings_list if r.startswith("⚠")]

            # Blood-red bold console notice BEFORE the interactive prompt so the
            # user sees exactly why the upload is being questioned.
            self._append("", "")
            if severe_reasons:
                self._append("  🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨", "severe_alert")
                self._append("  ════════════════════════════════════════════════════════════════════════════", "severe_alert")
                self._append("  ⚠ COMPATIBILITY ISSUE DETECTED — CONTINUING MAY DAMAGE YOUR BOARD OR FIRMWARE! ⚠", "severe_alert")
                self._append("  ════════════════════════════════════════════════════════════════════════════", "severe_alert")
                for reason in severe_reasons:
                    self._append(f"  ✖ {reason}", "severe_alert")
                self._append("  ════════════════════════════════════════════════════════════════════════════", "severe_alert")
                self._append("  🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨", "severe_alert")
            else:
                self._append("  ⚠ Compatibility warning(s) detected:", "warning")
            for reason in soft_reasons:
                # Reflow each caution for the console: the ⚠ belongs to the
                # header line, and the aggregated "on N boards (e.g., ...)"
                # list is noise — the selected board is what matters.
                clean = re.sub(
                    r"\s+on \d+ boards \(e\.g\., .*\)", "",
                    reason[2:].lstrip() if reason.startswith("⚠") else reason,
                )
                self._append(f"  • {clean}", "warning")
            self._append("")
            # Show interactive confirmation dialog box (thread-safe on Windows)
            proceed = [None]
            def _prompt():
                reasons_text = "\n".join(f"- {r}" for r in warnings_list)
                from tkinter import messagebox
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
                proceed[0] = messagebox.askyesno(
                    "Compatibility Warning",
                    msg,
                    icon="warning",
                    parent=self.root
                )
            self.root.after(0, _prompt)
            while proceed[0] is None:
                time.sleep(0.05)
                
            if not proceed[0]:
                self._append("", "")
                self._append("  ℹ Upload cancelled by user.", "info")
                self.is_busy = False
                self._set_buttons_state(False)
                self._set_status("Upload cancelled by user", Theme.YELLOW)
                return False
            
            if compat_reasons:
                self._compat_warnings_approved_hash = current_hash

        # One last synchronous check is the upload-side execution boundary.
        # Sources staged above are frozen, so an edit after this check cannot
        # change either the compiled binaries or PlatformIO's src/ inputs.
        if self._block_action_for_pending_ai_review("Upload"):
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_buttons_state(False)
            self._set_status("Upload paused for AI review", Theme.YELLOW)
            return False

        # ── Gate 2: last live hardware check before flashing ─────────────
        # The compatibility prompt above can park this worker a long time
        # waiting on the user — plenty of time to unplug the board.
        if self._abort_upload_if_mcu_missing(port, compiled_fresh=need_compile):
            return False

        # Release the serial monitor only after compilation, scanning, and
        # user confirmation are complete. The uploader is fired immediately
        # from here, minimizing the manual BOOT-button hold window.
        was_monitoring = self._pause_monitor()
        if was_monitoring and sys.platform == "win32":
            # Windows USB-serial drivers can retain the handle briefly after
            # the monitor thread closes it. This happens before the BOOT
            # prompt, so it improves reliability without lengthening the
            # user's button-hold window.
            time.sleep(0.60)
        # ─────────────────────────────────────────────────────────────────────────

        # ── Upload spinner thread ───────────────────────────────────────────────────
        # Phase-ordered display: each distinct upload step gets a numbered label
        # so the user always knows where they are in the pipeline.
        # Phases in order: 1 Connecting → 2 Erasing → 3 Writing → 4 Verifying → 5 Resetting
        _UPLOAD_PHASES = [
            ("Initialising",  "Starting PlatformIO uploader..."),
            ("Connecting",    "Connecting to board..."),
            ("Erasing",       "Erasing flash memory..."),
            ("Writing",       "Writing firmware to flash..."),
            ("Verifying",     "Verifying written data..."),
            ("Resetting",     "Resetting board..."),
            ("Done",          "Upload complete"),
        ]
        _PHASE_KEYS = [p[0] for p in _UPLOAD_PHASES]
        _upload_phase_idx = [0]   # index into _UPLOAD_PHASES
        _upload_active    = [True]
        _upload_frame     = [0]
        _upload_start     = time.time()
        _upload_spinner   = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

        def _set_upload_phase(key: str):
            """Advance to a named phase, never going backwards."""
            if key in _PHASE_KEYS:
                idx = _PHASE_KEYS.index(key)
                if idx > _upload_phase_idx[0]:
                    _upload_phase_idx[0] = idx
                    # Clear connection timeout once we move past Connecting
                    if key in ("Erasing", "Writing", "Verifying", "Resetting", "Done"):
                        nonlocal _connecting_since
                        _connecting_since = None

        def _upload_spin_loop():
            while _upload_active[0] and not getattr(self, "_stop_requested", False):
                # Yield the status bar to the reconnect countdown while
                # waiting for an unplugged MCU to reappear.
                if getattr(self, "_reconnect_waiting", False):
                    time.sleep(0.2)
                    continue
                idx     = _upload_phase_idx[0]
                key, label = _UPLOAD_PHASES[idx]
                # Keep the status visibly alive while esptool waits for the
                # ROM bootloader. The numbered console bar advances only when
                # a real fresh retry begins, so it never claims progress while
                # the same attempt is still waiting.
                if key == "Connecting":
                    elapsed = int(time.time() - _upload_start)
                    frame = _upload_spinner[_upload_frame[0] % len(_upload_spinner)]
                    _upload_frame[0] += 1
                    self._set_status(
                        f"{frame} Polling bootloader... ({elapsed}s)",
                        Theme.MAGENTA,
                    )
                    time.sleep(0.2)
                    continue
                elapsed = int(time.time() - _upload_start)
                frame   = _upload_spinner[_upload_frame[0] % len(_upload_spinner)]
                _upload_frame[0] += 1
                visible_total = len(_UPLOAD_PHASES) - 2   # 5
                visible_step  = max(0, idx - 1)           # 0-based relative to step 1
                step_str      = f"[{visible_step}/{visible_total}] " if visible_step > 0 else ""
                self._set_status(
                    f"{frame} {step_str}{label} ({elapsed}s)",
                    Theme.MAGENTA,
                )
                time.sleep(0.2)

        import threading as _threading_up
        _upload_spin_thread = _threading_up.Thread(target=_upload_spin_loop, daemon=True)
        _upload_spin_thread.start()

        self.is_busy = True
        self._set_buttons_state(True, operation="flash")
        self._set_status(f"Uploading to {port}...", Theme.MAGENTA)

        self._append("")
        self._append("=" * 50, "header")
        self._append("  ⬆  UPLOADING (PlatformIO)", "header")
        self._append("=" * 50, "header")
        upload_speed = self.upload_speed_var.get() if hasattr(self, "upload_speed_var") else "460800"
        self._append(f"  Port : {port} | Upload Speed : {upload_speed}", "port_highlight")
        if self._detect_port_chip() is None and not getattr(self, "_board_port_confirmed", False):
            self._append("  ⚠ Unrecognized USB-serial port — proceeding anyway.", "warning")

        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        is_avr = (board_info.get("platform", "") == "atmelavr")
        board_reset_family = board_reset_capabilities(
            board_info.get("platform", ""),
            board_info.get("board", ""),
            board_name,
            board_info.get("framework", ""),
        ).get("family")

        # Espressif monitor defaults are board-family settings: ESP8266 ROM
        # boot messages use 74880 and ESP32 uses 115200.  Do not let the
        # optional Serial.begin() scanner replace those selected-board
        # defaults after Upload; the user can still choose another monitor
        # baud explicitly from the Serial Monitor control.
        if board_reset_family in ("espressif32", "espressif8266"):
            self._pending_auto_baud = None
        else:
            self._pending_auto_baud = self._detect_sketch_baud_rate()

        # The fast esptool path follows the legacy reliable connection model:
        # each numbered retry is a fresh esptool session and reset pulse. The
        # PlatformIO compatibility uploader is only used after a fast uploader
        # tool failure, never after a normal BOOT timing miss.
        _MAX_CONNECT_RETRIES = UPLOAD_CONNECTION_ATTEMPTS
        _CONNECT_FAIL_SIGNATURES = (
            "wrong boot mode", "failed to connect",
            "no serial data received", "timed out waiting for packet",
            "device not found", "permissionerror", "access is denied",
            "port is busy", "could not open port", "permission denied",
            "connection timed out", "timed out after", "not responding",
            # esptool v5 reports a reset/re-enumerating USB serial adapter this
            # way before it prints "Connected". This is a retryable COM/BOOT
            # timing miss, not an uploader tool failure.
            "no more data to read from the serial port",
            "a device attached to the system is not functioning",
        )
        _connect_retry_count = 0

        # Fast ESP upload: compilation has already produced the exact images
        # PlatformIO would pass to esptool. Invoke esptool directly so its
        # internal connection loop starts immediately and keeps polling while
        # BOOT is held, without another SCons dependency scan.
        fast_bins = None
        if not is_avr and board_info.get("platform") in ("espressif32", "espressif8266"):
            fast_bins = self._locate_soft_reset_fast_binaries(
                self.sketch_dir_path,
                board_name,
                board_info.get("platform", ""),
                env_name=self._pio_env_name(),
                upload_speed=upload_speed,
                build_dir=self._board_build_dir(),
            )

        fast_upload_attempted = False
        if fast_bins is not None:
            fast_upload_attempted = True
            if (is_s3_board(board_info.get("board", "")) or "s3" in board_name.lower()) and self._is_native_usb_port():
                fast_bins["before"] = "usb-reset"
            self._append("  ⚡ Fast upload: polling the bootloader now…", "info")
            self._append(
                f"  ⚙ Esptool baud: {upload_speed}"
                " (selected upload speed)",
                "dim",
            )
            self._set_status("Connecting to bootloader now…", Theme.MAGENTA)
            self._current_op_phase = "flashing"
            self._set_window_closable(False)
            fast_ok, fast_error, fast_attempts = self._soft_reset_esptool_write(
                fast_bins, port, phase_callback=_set_upload_phase
            )
            if fast_ok:
                self._fast_upload_failure_count = 0
                _set_upload_phase("Done")
                _upload_active[0] = False
                _upload_spin_thread.join(timeout=1)
                self._append("")
                self._append(f"  ✔ Upload successful! {board_name} is running…", "success")
                self._set_status(f"Upload OK — {board_name} running", Theme.GREEN)
                self.root.after(0, self._update_skip_compile_state)
                self._focus_tab_on_unlock = self._serial_monitor_tab_index()
                self.is_busy = False
                self._current_op_phase = None
                self._set_buttons_busy(False)
                self._set_window_closable(True)
                self._activate_serial_monitor_after_success("Upload")
                if getattr(self, "clear_serial_on_upload_var", None) and self.clear_serial_on_upload_var.get():
                    self._clear_serial_console()
                self._manual_reset_pending = True
                self._monitor_should_run = True
                self._resume_monitor()
                return True
            # The direct uploader is authoritative once cached ESP images were
            # found. Do not launch a second PlatformIO/esptool session after a
            # direct write has connected or partially flashed: that can take
            # over the same COM port, reset the board again, and make a real
            # serial-loss error look like throttling or duplicate flashing.
            failure_kind = getattr(self, "_last_fast_upload_failure_kind", "")
            self._fast_upload_failure_count = 0
            self._fast_upload_disabled_reason = None
            _upload_active[0] = False
            _upload_spin_thread.join(timeout=1)
            self._append("")
            if failure_kind == "connection":
                self._append_connecting_progress(
                    min(fast_attempts, _MAX_CONNECT_RETRIES),
                    _MAX_CONNECT_RETRIES,
                    failed=True,
                )
                self._append(
                    f"  ✖ Failed to connect to {board_name} on {port} after "
                    f"{fast_attempts}/{_MAX_CONNECT_RETRIES} attempts.",
                    "error",
                )
                self._append("  💡 ESP32 / ESP32-S3 boards: hold BOOT, press RESET, release BOOT.", "info")
                self._append("  💡 Or: unplug & replug the USB cable, then try again.", "info")
            elif failure_kind == "flash":
                self._append("  ✖ Flash interrupted after the ESP32 connected.", "error")
                self._append(
                    "  The board was not handed to a second uploader session; retry Upload after the port recovers.",
                    "info",
                )
                self._append(f"  Reason: {fast_error}", "error")
            else:
                self._append("  ✖ Fast ESP uploader failed before flashing.", "error")
                self._append(f"  Reason: {fast_error}", "error")
            self._append(
                "  Diagnostic details were saved to logs/fast_upload_fallback.log.",
                "dim",
            )
            self._append("")
            self._append("  ✖ Upload FAILED.", "error")
            self._set_status("Upload FAILED", Theme.RED)
            self.root.after(0, self._update_skip_compile_state)
            self.is_busy = False
            self._current_op_phase = None
            self._set_buttons_busy(False)
            self._set_window_closable(True)
            if was_monitoring:
                self._resume_monitor()
            return False

        # NOTE: we deliberately do NOT run a separate esptool chip-info probe
        # here anymore. It used to open its own esptool connection (connect,
        # upload the stub flasher, read flash size/MAC/crystal) purely to
        # print a pretty info box, then close the port and hand it to
        # PlatformIO's own upload — which immediately reconnects and does the
        # exact same handshake again. That's two full reset+reconnect cycles
        # per upload instead of one. On boards with native USB (e.g. this
        # ESP32-S3), each reset pulse can cause the port to re-enumerate,
        # so the extra round trip was also a real source of intermittent
        # "failed to connect" upload issues, not just wasted time.
        # PlatformIO's own upload output already contains the same chip
        # info (Chip is ..., Features:, Crystal is ..., flash size) — it's
        # just filtered out below as noise instead of boxed up nicely.

        pio_path = find_pio_executable()
        if not pio_path:
            self._append("  ⚠ PlatformIO not found — installing automatically...", "warning")
            self._set_status("Installing PlatformIO...", Theme.YELLOW)
            pio_path = ensure_platformio()
            if not pio_path:
                self._append("  ✖ Failed to install PlatformIO!", "error")
                self._append("  Try manually: pip install platformio", "info")
                self.is_busy = False
                self._set_buttons_busy(False)
                return False
            self._append(f"  ✔ PlatformIO installed: {' '.join(pio_path)}", "success")

        env_name = self._pio_env_name()
        jobs = self._get_cpu_cores_jobs()
        cmd = pio_path + [
            "run",
            "-e", env_name,
            "-t", "upload",
            "-j", str(jobs),
            "--upload-port", port
        ]

        # Cached uploads and compatibility uploads use the same local staged
        # PlatformIO project as compilation.  This avoids reopening the UNC
        # sketch tree during the upload target's dependency/configuration scan.
        effective_cwd = self._platformio_project_dir(self.sketch_dir_path)
        env = self._platformio_subprocess_env(
            project_dir=self.sketch_dir_path,
            board_name=board_name,
            jobs=jobs,
        )
        # Do not inject a compound command-line string through the environment.
        # PlatformIO/esptool v5 treats PLATFORMIO_UPLOAD_FLAGS as one literal
        # argument, producing: invalid choice: '--connect-attempts 10'. The
        # direct fast path handles ESP32 BOOT polling; this compatibility path
        # must receive only the options generated by PlatformIO itself.
        env.pop("PLATFORMIO_UPLOAD_FLAGS", None)

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=str(effective_cwd),
                env=env,
            )
        except FileNotFoundError:
            self._append("  ✖ PlatformIO executable not found at: " + ' '.join(pio_path), "error")
            self.is_busy = False
            self._set_buttons_busy(False)
            return False

        last_pct = -1
        img_count = 0
        has_jtag_error = False
        has_port_busy_error = False
        output_lines: list[str] = []  # full raw output, used to detect "wrong boot mode" /
                                       # "failed to connect" signatures for the connect-retry loop


        # ── Chip-info capture ────────────────────────────────────────────────
        # PlatformIO's own esptool invocation already prints chip model,
        # features, crystal, MAC, and chip size while it connects — we used
        # to open a second, separate esptool connection just to redisplay
        # this same info in a specular box, which meant every upload did two full
        # connect/reset handshakes with the chip. Instead we harvest these
        # fields straight out of the single upload connection PlatformIO is
        # already making, and show the box once they're all in.
        _chip_info: dict[str, str] = {}
        _chip_info_shown = [False]
        _connected_logged = [False]
        _avr_connected_logged = [False]
        _connected_bar_flipped = [False]  # progress line re-rendered as green "✔ Connected"

        # Lines that would normally print BEFORE the chip-info box (build
        # stats, protocol config, connection status) are held here instead,
        # so the box can appear right under the "Port :" line — matching
        # where the user actually wants to see it — with everything else
        # flushed once the box is shown. For AVR boards there is no chip-
        # info box, so buffering is skipped entirely and lines print
        # immediately as before.
        _pending_pre_box: list = []

        def _buffered_append(text: str, tag: str = ""):
            # Always log connection status and live progress immediately so user sees activity
            if is_avr or _chip_info_shown[0] or any(k in text.lower() for k in ("connect", "attempting", "connected")):
                self._append(text, tag)
            else:
                # Deduplicate: PlatformIO reprints the identical protocol-config
                # block (DEBUG/RAM/Flash/Configuring/AVAILABLE/CURRENT) on every
                # connect attempt.  Keep only the first copy; it gets replayed
                # once when the attempt concludes (chip box on success, summary
                # on failure) instead of spamming the console each retry.
                if any(t == text for t, _tag in _pending_pre_box):
                    return
                _pending_pre_box.append((text, tag))

        def _maybe_show_chip_info_box(force: bool = False):
            if not _chip_info_shown[0]:
                model = _chip_info.get("Chip Model") or (self.board_var.get() if force else None)
                if model or force:
                    display_model = model or self.board_var.get()
                    if _chip_info.get("Features"):
                        _chip_info["Features"] = _enrich_chip_features(
                            display_model, _chip_info["Features"]
                        )
                    if "Flash Size" not in _chip_info:
                        configured_flash, _has_psram = normalized_board_memory_options(
                            board_info
                        )
                        if configured_flash:
                            _chip_info["Flash Size"] = f"{configured_flash} (configured, not auto-detected)"
                    self._print_chip_info_box(display_model, list(_chip_info.items()))
                    _chip_info_shown[0] = True
                    for _text, _tag in _pending_pre_box:
                        self._append(_text, _tag)
                    _pending_pre_box.clear()

        def _flip_to_connected_bar():
            """Re-render the last progress line as a green '✔ Connected' bar —
            called ONLY once esptool has actually synced with the chip (stub
            uploaded / erase begun).  While attempts are still in flight the
            line keeps its magenta '🔌 Connecting' look."""
            if not is_avr and not _connected_bar_flipped[0]:
                _connected_bar_flipped[0] = True
                self._append_connecting_progress(
                    _connect_retry_count + 1, _MAX_CONNECT_RETRIES, connected=True
                )

        def _mark_flashing_active():
            if getattr(self, "_current_op_phase", "") != "flashing":
                self._current_op_phase = "flashing"
                self._set_window_closable(False)

        compatibility_progress_state = self._new_upload_progress_state(fast_bins)

        def _before_compatibility_progress():
            _mark_flashing_active()
            _maybe_show_chip_info_box(force=True)
            _flip_to_connected_bar()

        # ── Queue-based reader (same pattern as _run_compile) ─────────────
        # The upload subprocess (PlatformIO → esptool) can hang indefinitely
        # if the chip stops responding (e.g. ESP32-S3 needs manual BOOT press).
        # A blocking for-line-in-iter(readline) loop would freeze the entire
        # GUI forever in that case.  Instead we read via a daemon thread +
        # Queue with a 0.1 s timeout so we can detect process exit, honour
        # the Stop button, and drain late-arriving output after the process
        # terminates.
        import queue as _queue
        import threading as _threading

        _line_queue: _queue.Queue = _queue.Queue()

        def _reader(stream, q):
            try:
                for line in iter(stream.readline, ''):
                    if line:
                        q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)  # sentinel

        _t_out = _threading.Thread(target=_reader, args=(self.process.stdout, _line_queue), daemon=True)
        _t_out.start()

        _sentinels_remaining = 1
        _process_exited_at = None
        _connecting_since = None  # disarmed — armed once esptool reaches the connection stage

        # ── Connection timeout ──────────────────────────────────────────
        # ESP32-S3 / native-USB boards can hang esptool indefinitely if the
        # chip doesn't enter bootloader mode (e.g. user forgot to press BOOT).
        # If the process stays stuck in the "Connecting" phase without
        # syncing after _CONNECT_TIMEOUT seconds, we kill it and report the
        # failed session instead of leaving the app frozen indefinitely.
        #
        # IMPORTANT: the watchdog stays DISARMED until esptool actually
        # reaches the connection stage (PlatformIO prints "Looking for upload
        # port" / "Serial port" / "Connecting..." then).  Everything before
        # that — PlatformIO's silent build-cache clean at startup (triggered
        # by the GUI's platformio.ini rewrite changing project.checksum),
        # which can take several seconds on slow/removable volumes — must
        # never be killed by this watchdog, or every attempt dies before the
        # chip is even addressed.
        _CONNECT_TIMEOUT = 15  # seconds; allow the one uploader session to poll BOOT

        _timeout_triggered = [False]

        # ── Connection session state ──────────────────────────────────────
        # A fresh PlatformIO subprocess repeats the reset pulse and SCons
        # project scan. Keep the compatibility path to one session; esptool
        # receives its own internal polling budget through the environment
        # configured above.
        _connect_retry_count = 0
        _last_dot_update = [0.0]
        _connection_dot_count = [0]
        # A PlatformIO retry starts a brand-new process and repeats the reset
        # pulse/project scan. That is exactly what made the console print the
        # same protocol block over and over and made BOOT timing unreliable.
        # Keep one session; esptool receives the internal connection budget via
        # PLATFORMIO_UPLOAD_FLAGS below.
        _ALLOW_COMPATIBILITY_RETRY = False

        # Paths PlatformIO reported as undeletable (WinError 145) — auto-retried
        # after the process exits so no manual user intervention is needed.
        _stale_clean_paths: list[str] = []

        # ── Print the initial "Connecting..." line proactively ───────────
        # This shows BEFORE esptool's first output, so the user knows
        # immediately that a connection attempt is in progress and can
        # press the BOOT button on their board.
        if not is_avr:
            self._append_connecting_progress(_connect_retry_count + 1, _MAX_CONNECT_RETRIES)
            _connected_logged[0] = True
            _set_upload_phase("Connecting")

        while True:
            while _sentinels_remaining > 0:
                try:
                    line = _line_queue.get(timeout=0.1)
                except _queue.Empty:
                    # ── Animate the "Connecting....." dot count in the status bar ──
                    if (not is_avr and _upload_phase_idx[0] <= _PHASE_KEYS.index("Connecting")
                            and time.time() - _last_dot_update[0] > 0.8):
                        _last_dot_update[0] = time.time()
                        dots = max(3, (_connection_dot_count[0] % 10) + 1)
                        _connection_dot_count[0] = dots
                        dot_str = "." * dots
                        # PlatformIO/esptool emits the connection dots without
                        # newline characters, so the line reader cannot update
                        # the console bar until the session ends. Reflect the
                        # live polling window here without launching another
                        # uploader process.
                        self._append_connecting_progress(
                            min(_MAX_CONNECT_RETRIES, max(1, dots - 2)),
                            _MAX_CONNECT_RETRIES,
                        )
                        self._set_status(
                            f"Connecting{dot_str} ({_connect_retry_count + 1}/{_MAX_CONNECT_RETRIES})",
                            Theme.MAGENTA,
                        )

                    # After the process exits, give the reader thread a generous
                    # window to flush any pending data. Only bail after 5 s of
                    # no new data post-exit.
                    if self.process.poll() is not None:
                        if _process_exited_at is None:
                            _process_exited_at = time.time()
                        elif time.time() - _process_exited_at > 5:
                            break
                    # Allow the Stop button to interrupt a hung upload
                    if getattr(self, "_stop_requested", False):
                        break
                    # Connection timeout: if we've been stuck in "Connecting"
                    # phase for too long with no output, kill the process.
                    if (_connecting_since is not None
                            and _upload_phase_idx[0] <= _PHASE_KEYS.index("Connecting")
                            and time.time() - _connecting_since > _CONNECT_TIMEOUT):
                        _timeout_triggered[0] = True
                        # No console noise here — the progress bar increment
                        # already tells the user the attempt failed.  The
                        # captured protocol-config block and the real error
                        # are replayed once at the end of the retry loop.
                        try:
                            self.process.kill()
                        except Exception:
                            pass
                        break
                    continue

                if line is None:
                    _sentinels_remaining -= 1
                    continue

                # Reset the post-exit timer whenever we receive real data
                _process_exited_at = None

                stripped = line.rstrip()
                if not stripped:
                    continue
                output_lines.append(stripped)
                low = stripped.lower()

                # Arm the connection watchdog only once the upload has really
                # reached the serial/connection stage.  Earlier output (PIO
                # banner, build-cache clean retries, SCons checks) is silent
                # or slow on removable drives and must not count against the
                # connect timeout.
                if _connecting_since is None and (
                    "looking for upload port" in low
                    or "serial port" in low
                    or "connecting" in low
                ):
                    _connecting_since = time.time()

                if any(x in low for x in ("error", "failed", "unable", "cannot")) and any(x in low for x in ("esp_usb_jtag", "openocd", "jtag")):
                    has_jtag_error = True
                if any(x in low for x in ("permissionerror", "access is denied", "port is busy", "could not open port", "permission denied")):
                    has_port_busy_error = True

                # Opportunistic chip-info capture
                if not is_avr:
                    m = re.search(r'chip (?:is|type)\s*:?\s+(.+)$', stripped, re.IGNORECASE)
                    if m:
                        _chip_info["Chip Model"] = m.group(1).strip()
                    m = re.search(r'features\s*:\s*(.+)$', stripped, re.IGNORECASE)
                    if m:
                        _chip_info["Features"] = m.group(1).strip()
                    m = re.search(r'crystal (?:is|frequency)\s*:?\s+(.+)$', stripped, re.IGNORECASE)
                    if m:
                        _chip_info["Crystal"] = m.group(1).strip()
                    m = re.search(r'^\s*mac\s*:\s*(.+)$', stripped, re.IGNORECASE)
                    if m:
                        _chip_info["MAC Address"] = m.group(1).strip()
                    m = re.search(r'(?:auto-detected\s+)?flash size\s*:\s*(.+)$', stripped, re.IGNORECASE)
                    if m:
                        _chip_info["Flash Size"] = m.group(1).strip()

                # ── PlatformIO build-info lines (shown for all boards) ──────────
                if low.startswith("platform:"):
                    continue
                if low.startswith("hardware:"):
                    continue
                if low.startswith("debug:"):
                    _buffered_append(f"  {stripped}", "dim")
                    continue
                if "ram:" in low or "flash:" in low:
                    _buffered_append(f"  {stripped}", "success")
                    continue
                # Upload protocol config lines
                if re.match(r"\s*(configuring upload protocol|available:|current:)", low):
                    _buffered_append(f"  {stripped}", "dim")
                    continue
                # PIO result line  "=== [SUCCESS] Took X.XX seconds ==="
                pio_result = re.search(r'=+\s*\[(SUCCESS|FAILED)\]\s*Took\s*([\d.]+)\s*seconds', stripped, re.IGNORECASE)
                if pio_result:
                    verdict = pio_result.group(1).upper()
                    secs    = pio_result.group(2)
                    if verdict == "SUCCESS":
                        self._append(f"  {stripped}", "success")
                    else:
                        can_retry = (_ALLOW_COMPATIBILITY_RETRY and not is_avr
                                     and _connect_retry_count < _MAX_CONNECT_RETRIES - 1
                                     and not getattr(self, "_stop_requested", False))
                        if not can_retry:
                            self._append(f"  {stripped}", "error")
                    continue

                # Transform esptool's many raw Writing/Wrote rows into the
                # same single responsive progress row used by the fast path.
                if (not is_avr and self._consume_esptool_upload_progress(
                        compatibility_progress_state, stripped,
                        before_progress=_before_compatibility_progress,
                        phase_callback=_set_upload_phase)):
                    continue

                # ── Suppress low-level protocol noise ───────────────────────────
                _NOISE = (
                    "auto-detected:", "chip is", "chip type:", "features:",
                    "crystal is", "crystal frequency:", "mac:",
                    "uploading stub", "running stub", "stub running",
                    "changing baud", "compressed", "leaving...",
                    "warning: espcomm", "esptool.py v", "serial port",
                    "v2.", "v3.", "v4.", "v5.",   # version lines
                )

                if any(n in low for n in _NOISE):
                    # Still advance phase silently from noise lines
                    if "erasing" in low or "erase" in low:
                        _set_upload_phase("Erasing")
                        _mark_flashing_active()
                    elif "writing" in low:
                        _set_upload_phase("Writing")
                        _mark_flashing_active()
                    elif "verifying" in low or "hash" in low:
                        _set_upload_phase("Verifying")
                    elif "leaving" in low or "hard reset" in low:
                        _set_upload_phase("Resetting")
                    continue

                if is_avr:
                    # avrdude output — map to clean structured lines
                    if "avr device initialized" in low or "device signature" in low:
                        _set_upload_phase("Connecting")
                        if not _avr_connected_logged[0]:
                            self._append("  🔌 Connected to Arduino Uno", "system")
                            _avr_connected_logged[0] = True
                    elif "writing flash" in low or "writing eeprom" in low:
                        _set_upload_phase("Writing")
                    elif "verifying flash" in low or "verifying eeprom" in low:
                        _set_upload_phase("Verifying")
                    elif "avrdude done" in low or "bytes of flash" in low or "bytes written" in low:
                        _set_upload_phase("Done")
                        self._append(f"  {stripped}", "success")
                    elif "error" in low or "failed" in low:
                        self._append(f"  {stripped}", "error")
                else:
                    # ESP boards
                    if "connecting" in low:
                        _set_upload_phase("Connecting")
                        _mark_flashing_active()
                        if not _connected_logged[0]:
                            self._append("", "")
                            self._append("  🔌 Attempting to connect to ESP board...", "info")
                            _connected_logged[0] = True
                        if _connecting_since is None:
                            _connecting_since = time.time()
                    elif "stub running" in low or "running stub" in low or "uploading stub" in low:
                        _set_upload_phase("Connecting")
                        _mark_flashing_active()
                        if not _connected_logged[0]:
                            self._append("", "")
                            self._append("  🔌 Attempting to connect to ESP board...", "info")
                            _connected_logged[0] = True
                        _maybe_show_chip_info_box()
                        _flip_to_connected_bar()
                    elif "erasing" in low:
                        _mark_flashing_active()
                        if _upload_phase_idx[0] <= _PHASE_KEYS.index("Connecting"):
                            _maybe_show_chip_info_box(force=True)
                        _flip_to_connected_bar()
                        _set_upload_phase("Erasing")
                    elif re.search(r'\d+\s*%', stripped):
                        _mark_flashing_active()
                        if _upload_phase_idx[0] <= _PHASE_KEYS.index("Connecting"):
                            _maybe_show_chip_info_box(force=True)
                        _flip_to_connected_bar()
                        _set_upload_phase("Writing")
                    elif "verifying" in low:
                        _set_upload_phase("Verifying")
                    elif "hard resetting" in low:
                        _set_upload_phase("Resetting")
                        _maybe_show_chip_info_box()
                        _buffered_append(f"  {stripped}", "success")
                    elif "wrote" in low or ("success" in low and "image" not in low):
                        _set_upload_phase("Done")
                        self._append(f"  {stripped}", "success")
                    elif "successfully created" in low and "image" in low:
                        chip_match = re.search(r'successfully created (\w+) image', low)
                        chip_name = chip_match.group(1).upper() if chip_match else "MCU"
                        img_count += 1
                        label = "Bootloader" if img_count == 1 else "Application"
                        self._append(f"  ✔ Successfully created {chip_name} image ({label})", "success")
                    elif is_nonfatal_pio_clean_report(stripped):
                        self._append(f"  ⚠ {stripped}", "warning")
                        self._capture_stale_clean_path(stripped, _stale_clean_paths)
                    elif "error" in low or "failed" in low:
                        is_conn_sig = any(sig in low for sig in _CONNECT_FAIL_SIGNATURES) or "fatal error occurred" in low or "error 2" in low
                        can_retry = (_ALLOW_COMPATIBILITY_RETRY and not is_avr
                                     and _connect_retry_count < _MAX_CONNECT_RETRIES - 1
                                     and not getattr(self, "_stop_requested", False))
                        if not (is_conn_sig and can_retry):
                            self._append(f"  {stripped}", "error")

            # Drain any lines that arrived after the main loop exited
            while not _line_queue.empty():
                try:
                    line = _line_queue.get_nowait()
                except _queue.Empty:
                    break
                if line is None:
                    continue
                stripped = line.rstrip()
                if not stripped:
                    continue
                output_lines.append(stripped)
                low = stripped.lower()
                if (not is_avr and self._consume_esptool_upload_progress(
                        compatibility_progress_state, stripped,
                        before_progress=_before_compatibility_progress,
                        phase_callback=_set_upload_phase)):
                    continue
                if is_nonfatal_pio_clean_report(stripped):
                    self._append(f"  ⚠ {stripped}", "warning")
                    self._capture_stale_clean_path(stripped, _stale_clean_paths)
                elif "error" in low or "failed" in low:
                    is_conn_sig = any(sig in low for sig in _CONNECT_FAIL_SIGNATURES) or "fatal error occurred" in low or "error 2" in low
                    can_retry = (_ALLOW_COMPATIBILITY_RETRY and not is_avr
                                 and _connect_retry_count < _MAX_CONNECT_RETRIES - 1
                                 and not getattr(self, "_stop_requested", False))
                    if not (is_conn_sig and can_retry):
                        self._append(f"  {stripped}", "error")
                elif "success" in low or "wrote" in low or "hard resetting" in low:
                    self._append(f"  {stripped}", "success")

            _t_out.join(timeout=5)
            self.process.wait()
            rc = self.process.returncode

            # ── Connection result ────────────────────────────────────────────
            # Compatibility retries are intentionally disabled; restarting
            # PlatformIO here would repeat the reset pulse and destabilize BOOT
            # timing. The single session has already consumed esptool's poll
            # budget (when supported by the installed PlatformIO toolchain).
            joined = " ".join(line.rstrip().lower() for line in output_lines)
            is_conn_failure = (not is_avr and rc != 0 and (_timeout_triggered[0] or any(sig in joined for sig in _CONNECT_FAIL_SIGNATURES)))

            if (_ALLOW_COMPATIBILITY_RETRY and is_conn_failure
                    and _connect_retry_count < _MAX_CONNECT_RETRIES - 1
                    and not getattr(self, "_stop_requested", False)):
                _connect_retry_count += 1
                _timeout_triggered[0] = False
                _connected_bar_flipped[0] = False  # next connect must re-flip to green
                retry_dots = "." * (4 + _connect_retry_count)

                if any(x in joined for x in ("permissionerror", "access is denied", "port is busy", "could not open port", "permission denied")):
                    time.sleep(0.2)


                self._append_connecting_progress(_connect_retry_count + 1, _MAX_CONNECT_RETRIES)

                self._set_status(
                    f"Connecting{retry_dots} ({_connect_retry_count + 1}/{_MAX_CONNECT_RETRIES})",
                    Theme.MAGENTA,
                )

                # Before we start a fresh PlatformIO subprocess, give the
                # filesystem a chance to clear the WinError 32/145 lock the
                # previous attempt just hit. The previous process has been
                # wait()'d above, so its child handles are closed; the
                # auto-clean helper retries with backoff and falls back to a
                # hidden rename, so it never throws. build_ok=False is
                # intentional: the locked files live inside
                # .pio/build/<env_name>/, and build_ok=True would skip them.
                # The next `pio run -t upload` regenerates them via SCons'
                # normal incremental build.
                if _stale_clean_paths:
                    self._append(
                        "  ♻ Releasing stale build lock before retry…",
                        "dim",
                    )
                    self._auto_clean_stale_build_paths(
                        list(_stale_clean_paths), env_name, build_ok=False
                    )

                # Restart the upload subprocess with the same command.
                try:
                    self.process = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, encoding="utf-8", errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        cwd=str(effective_cwd),
                        env=env,
                    )
                except Exception:
                    break

                # Reset state for the retried subprocess.
                # NOTE: _pending_pre_box is intentionally NOT cleared — the
                # protocol-config block captured on the first attempt is deduped
                # and replayed once when the upload concludes.
                output_lines.clear()
                _line_queue = _queue.Queue()
                _t_out = _threading.Thread(target=_reader, args=(self.process.stdout, _line_queue), daemon=True)
                _t_out.start()
                _sentinels_remaining = 1
                _process_exited_at = None
                _connecting_since = None  # disarm — re-armed by the connection-stage marker
                continue
            else:
                # rc != 0 and no retry left.  Leave _pending_pre_box intact —
                # it is replayed once, with RAM/Flash recolored red, by the
                # failure summary below (after the actual error message).
                break

        # Stop the spinner (the single uploader session is done).
        _upload_active[0] = False
        _upload_spin_thread.join(timeout=1)

        # ── Auto-clean stale build paths reported by PlatformIO ───────────
        if _stale_clean_paths:
            self._auto_clean_stale_build_paths(_stale_clean_paths, env_name, build_ok=(rc == 0))

        ok = rc == 0

        if ok:
            self._append("")
            board_label = self.board_var.get()
            self._append(f"  ✔ Upload successful! {board_label} is running...", "success")
            self._set_status(f"Upload OK — {board_label} running", Theme.GREEN)
            
            # Enable and check Skip Compile after successful compile and upload
            self.root.after(0, self._update_skip_compile_state)

            # Jump to the Serial Monitor tab once the lock clears below, so
            # the user immediately sees the board's fresh boot output.
            self._focus_tab_on_unlock = self._serial_monitor_tab_index()
        else:

            self._append("")
            # Show the actual error that ended the retry loop (the progress bar
            # already communicated the failed attempts, so only the final
            # verdict + real reason are printed here).
            if _timeout_triggered[0] or is_conn_failure:
                connection_target = (
                    "ESP8266"
                    if board_info.get("platform") == "espressif8266"
                    else "ESP32"
                )
                self._append(
                    f"  ✖ Failed to connect to {connection_target} on {port} — no response "
                    f"during one uploader session with up to "
                    f"{_MAX_CONNECT_RETRIES} internal BOOT polls. "
                    f"MCU needs to be on 'download_mode' state.",
                    "error",
                )
            # Replay the captured protocol-config block once, right after the
            # error.  RAM/Flash usage lines turn RED on failure (the success
            # path colors them GREEN when the chip-info box flushes the block).
            if _pending_pre_box:
                for _text, _tag in _pending_pre_box:
                    if _tag == "success" and ("ram:" in _text.lower() or "flash:" in _text.lower()):
                        self._append(_text, "error")
                    else:
                        self._append(_text, _tag)
                _pending_pre_box.clear()
            self._append("")
            if not is_avr:
                self._append_connecting_progress(
                    _connect_retry_count + 1, _MAX_CONNECT_RETRIES, failed=True
                )
            if _timeout_triggered[0] or is_conn_failure:
                if board_info.get("platform") == "espressif8266":
                    self._append(
                        "  💡 ESP8266: hold BOOT/GPIO0 LOW, press RESET/EN, then release BOOT after Connected.",
                        "info",
                    )
                else:
                    self._append("  💡 ESP32 / ESP32-S3 boards: hold BOOT, press RESET, release BOOT.", "info")
                self._append("  💡 Or: unplug & replug the USB cable, then try again.", "info")
                self._append("")
            else:
                self._append("  ⚠ Upload failure was not caused by BOOT mode connection timing.", "warning")
                self._append(
                    "  ℹ Compiled output and every board cache were preserved; upload failures do not require Clean.",
                    "info",
                )

            self._append("  ✖ Upload FAILED.", "error")
            if has_port_busy_error:
                self._append(f"  💡 Port '{port}' is in use by another program (e.g. Serial Monitor, PuTTY, Arduino IDE, or Python).", "info")
                self._append("  💡 Solution: Close any other app using this port or unplug & replug your USB cable.", "info")
            self._set_status("Upload FAILED", Theme.RED)
            
            # Update Skip Compile on failure
            self.root.after(0, self._update_skip_compile_state)

            if has_jtag_error:
                self._append(
                    "  💡 JTAG uploader conflict detected. The generated configuration now prefers serial esptool; retry Upload without clearing builds.",
                    "info",
                )

        self.is_busy = False
        self._current_op_phase = None
        self._set_buttons_busy(False)
        self._set_window_closable(True)
        
        # Resume the monitor on successful upload (triggering hardware reset so setup() output is captured)
        if ok:
            if getattr(self, "_pending_auto_baud", None):
                auto_baud = self._pending_auto_baud
                curr_baud = self.serial_baud_var.get() if hasattr(self, "serial_baud_var") else self.baud_var.get()
                if auto_baud != curr_baud:
                    def _apply_auto_baud(b=auto_baud):
                        try:
                            if hasattr(self, "serial_baud_var"):
                                self.serial_baud_var.set(b)
                            if hasattr(self, "baud_var"):
                                self.baud_var.set(b)
                            self._append(
                                f"  ⚡ Auto-detected Serial.begin({b}) in sketch — set Serial Monitor baud rate to {b}",
                                "info"
                            )
                        except Exception:
                            pass
                    self.root.after(0, _apply_auto_baud)
                    if hasattr(self, "serial_baud_var"):
                        self.serial_baud_var.set(auto_baud)
                    if hasattr(self, "baud_var"):
                        self.baud_var.set(auto_baud)

            self._activate_serial_monitor_after_success("Upload")
            if getattr(self, "clear_serial_on_upload_var", None) and self.clear_serial_on_upload_var.get():
                self._clear_serial_console()
            self._manual_reset_pending = True
            self._monitor_should_run = True
            self._resume_monitor()
        elif was_monitoring:
            # If upload failed, but we were monitoring, resume the monitor
            self._resume_monitor()

        return ok
