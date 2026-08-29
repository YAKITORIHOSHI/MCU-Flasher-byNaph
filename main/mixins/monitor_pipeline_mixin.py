#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import time
import re
import threading
import queue
from typing import TYPE_CHECKING


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

class MonitorPipelineMixin(_Base):
    """Mixin providing MonitorPipelineMixin capabilities for MCUUploadGUI."""
    def _trigger_actual_board_reset(self, port: str) -> bool:
        """Open the serial port and perform the ESP32 Reset(DTR/RTS) pulse.

        Returns True only when the pulse was actually sent.  Callers that run
        after a flash operation can therefore avoid reporting a fully finished
        reset when Windows still has the COM port locked.
        """
        owner_pid = port_occupied_owner(port)
        if owner_pid:
            self._append(f"  ⚠ Reset(DTR/RTS) blocked: Port '{port}' is in use by another window (PID {owner_pid}).", "warning")
            return False
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        platform = str(board_info.get("platform", "")).lower()
        is_uno = (platform == "atmelavr")
        self._append(f"  🔄 Triggering hardware reset on {port}...", "info")
        try:
            # 1. Native USB-CDC (ESP32-S3 / RP2040 / SAMD) 1200-baud touch reset fallback
            if not is_uno and (self._is_native_usb_port() or platform in ("raspberrypi", "samd")):
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
                    # ARM / RISC-V pulse reset (prevents ESP32 transistor inversion lockup)
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
            self._append("  ✔ Reset pulse completed successfully.", "success")
            self._skip_reconnect_reset = True
            return True
        except Exception as e:
            self._append(f"  ⚠ Could not complete Reset(DTR/RTS): {e}", "warning")
            return False

    # ──────────────────────────────────────────────────────────
    # SERIAL MONITOR (always-on, right panel)
    # ──────────────────────────────────────────────────────────
    def _reset_mcu_from_monitor(self):
        """Reset via the existing reconnect pulse, without blocking/joining Tk."""
        port = self._get_port()
        if not port:
            self._append_notif("  ⚠ No port selected — cannot reset.", "warning")
            return
        owner_pid = port_occupied_owner(port)
        if owner_pid:
            self._append_notif(f"  ⚠ Reset blocked: Port '{port}' is in use by another window (PID {owner_pid}).", "warning")
            return
        if self.is_busy and getattr(self, "_active_operation", None) in ("upload", "flash", "reset"):
            self._append_notif("  ⚠ Reset blocked — an operation (uploading/resetting) is currently in progress.", "warning")
            return
        if not self._is_board_recognized():
            if not getattr(self, "_silent_reset", False):
                self._append_notif("  ⚠ Reset blocked — board on this port hasn't been recognized yet.", "warning")
            return

        silent = getattr(self, "_silent_reset", False)
        if getattr(self, "clear_serial_on_upload_var", None) and self.clear_serial_on_upload_var.get():
            self._clear_serial_console()
        if not silent:
            self._append_notif(f"  ↺ Resetting MCU on {port}…", "dim")
            self._append_serial(f"  ↺ Resetting MCU on {port}…", "dim")

        self._monitor_should_run = False
        self._stop_serial_session()
        self._set_serial_status(False)
        self._manual_reset_pending = True
        self._monitor_should_run = True
        self._schedule_auto_start_monitor(100)

    def _run_monitor(self, port: str, baud: int, generation: int, stop_event: threading.Event, config: dict):
        cur_thread = threading.current_thread()
        conn = None
        silent = bool(config.get("silent", False))
        board_name = str(config.get("board_name", ""))
        board_info = dict(config.get("board_info", {}))
        port_label = str(config.get("port_label", port))
        is_native_usb = bool(config.get("is_native_usb", False))
        is_first_connect = bool(config.get("is_first_connect", False))
        is_manual_reset = bool(config.get("is_manual_reset", False))
        clear_on_connect = bool(config.get("clear_on_connect", False))

        def _session_current() -> bool:
            with self._serial_state_lock:
                return bool(
                    not stop_event.is_set()
                    and self._serial_generation == generation
                    and self.serial_thread is cur_thread
                )

        if not _session_current():
            return
        self._set_serial_status("reconnecting")

        owner_pid = port_occupied_owner(port)
        if not _session_current():
            return
        if owner_pid:
            self._append_notif(f"  ⚠ Serial Monitor blocked: {port} is in use by another window (PID {owner_pid}).", "warning")
            with self._serial_state_lock:
                self.serial_running = False
            self._set_serial_status(False)
            return

        # ── Board-aware pre-connect strategy ──────────────────────────────
        # ESP32/ESP8266: boot takes ~500 ms–2 s after the port opens, so a
        # short delay is fine — we won't miss anything important.
        #
        # Arduino UNO (ATmega328P): opening the port asserts DTR which
        # IMMEDIATELY resets the MCU.  The bootloader runs for ~2 s and then
        # hands off to setup().  If we wait even 1 s before reading, the
        # entire setup() block is gone.
        #
        # Strategy for UNO:
        #  1. Open the port with DTR *de-asserted* (dtr=False) so we don't
        #     accidentally trigger an extra reset when the port opens.
        #  2. Pulse DTR low→high→low to reset the board ourselves, at a
        #     moment we control.
        #  3. Start reading immediately — the bootloader and setup() output
        #     will both be captured.
        is_uno = (board_info.get("platform", "") == "atmelavr")

        # Determine if this connection attempt is a rapid duplicate of a failed attempt
        current_time = time.time()
        is_duplicate = (
            getattr(self, "_last_conn_attempt", {}).get("port") == port
            and getattr(self, "_last_conn_attempt", {}).get("baud") == baud
            and getattr(self, "_last_conn_attempt", {}).get("board") == board_name
            and (current_time - getattr(self, "_last_conn_attempt", {}).get("time", 0.0)) < 4.0
        )

        if not is_duplicate:
            port_warning = self._unrecognized_mcu_port_warning(port, port_label)
            if port_warning:
                self._append_notif(port_warning, "warning")
            if is_uno:
                self._append_notif(f"  Connecting to {port} @ {baud} (Arduino AVR mode)…", "dim")
            else:
                self._append_notif(f"  Connecting to {port} @ {baud}…", "dim")

        # Save this attempt details
        self._last_conn_attempt = {
            "port": port,
            "baud": baud,
            "board": board_name,
            "time": current_time,
        }


        # High-throughput receive tuning.  460800 and 921600 baud can deliver
        # data faster than a Tk terminal can render if the GUI performs tiny
        # reads/inserts.  The reader therefore uses larger OS/Python batches and
        # the display side coalesces them for a few extra milliseconds.
        high_speed_serial = int(baud) >= 460800
        self._serial_display_flush_delay_ms = 40 if high_speed_serial else 25
        serial_read_cap = 131072 if high_speed_serial else 32768
        serial_no_newline_cap = 65536 if high_speed_serial else 32768
        serial_partial_idle_s = 0.12 if high_speed_serial else 0.08

        if not is_uno:
            if is_first_connect or is_manual_reset:
                # First connect: board is already running, no boot delay needed.
                # Manual reset: board hasn't been reset yet — we'll do it after
                # opening the port so we can read the boot output immediately.
                # On native USB-CDC (ESP32-S3) any unnecessary delay between
                # opening the port and starting to read risks filling the MCU's
                # USB TX buffer, which blocks Serial.print() and freezes the
                # running sketch permanently.
                pass
            else:
                if stop_event.wait(1.0):
                    return

        # Try to open the port with retries to handle transient OS/driver locks (silently up to 5s)
        max_attempts = 25
        attempt = 0
        while attempt < max_attempts:
            # Gracefully abort immediately if busy with upload/flash/reset, or if monitor is stopped/paused
            if (not _session_current()
                    or not getattr(self, "_monitor_should_run", False)
                    or (getattr(self, "is_busy", False) and getattr(self, "_active_operation", None) in ("upload", "flash", "reset"))):
                return
            try:
                # Construct serial object and set DTR/RTS to False BEFORE opening
                # so Win32 SetCommState initializes with DTR_CONTROL_DISABLE and RTS_CONTROL_DISABLE
                # from the very first microsecond, preventing an initial DTR/RTS toggle reset pulse.
                conn = serial.Serial()
                conn.dtr = False
                conn.rts = False
                conn.port = port
                conn.baudrate = baud
                # A shorter blocking read timeout keeps the worker draining the
                # Windows receive queue promptly at 921600 without busy-spinning.
                # A short timeout keeps cancellation/TX latency bounded without
                # busy-spinning; write timeout prevents a stalled device from
                # pinning the serial worker indefinitely.
                conn.timeout = 0.02
                conn.write_timeout = 0.25
                conn.dsrdtr = False
                conn.rtscts = False
                conn.open()

                # pyserial exposes the underlying Windows SetupComm receive queue
                # through set_buffer_size() where supported.  Request a generous
                # RX queue so short GUI/CPU scheduling pauses do not immediately
                # overflow the driver at 921600 baud.  Drivers are allowed to
                # ignore/reject the hint, so this is intentionally best-effort.
                if hasattr(conn, "set_buffer_size"):
                    try:
                        conn.set_buffer_size(
                            rx_size=(1024 * 1024 if high_speed_serial else 262144),
                            tx_size=65536,
                        )
                    except Exception:
                        pass

                if not _session_current():
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                with self._serial_state_lock:
                    self.serial_conn = conn
                self._last_monitor_error = ""

                # On Native USB / ESP32-S3 CDC ports, set DTR=True to signal CDC terminal active
                if is_native_usb or is_s3_board(board_info.get("board", "")):
                    try:
                        conn.dtr = True
                        conn.rts = True
                    except Exception:
                        pass

                # Pulse reset only after an explicit Upload/Reset request. For
                # Uno, opening with DTR=False avoids an uncontrolled reset; the
                # deliberate False -> True -> False pulse below then starts the
                # bootloader while the monitor is already open, so setup() output
                # is captured instead of being missed.
                if is_manual_reset:
                    try:
                        if is_uno:
                            conn.rts = False
                            conn.dtr = False
                            if stop_event.wait(0.05):
                                return
                            conn.dtr = True
                            if stop_event.wait(0.10):
                                return
                            conn.dtr = False
                            if stop_event.wait(0.05):
                                return
                        elif not is_native_usb:
                            conn.dtr = False
                            conn.rts = True
                            if stop_event.wait(0.15):
                                return
                            conn.rts = False
                            conn.dtr = False
                            if stop_event.wait(0.05):
                                return
                    except Exception:
                        pass
                    if _session_current():
                        self._manual_reset_pending = False

                break
            except serial.SerialException as e:
                attempt += 1
                if attempt < max_attempts:
                    if stop_event.wait(0.2):
                        return
                else:
                    err_msg = str(e)
                    if getattr(self, "_last_monitor_error", "") != err_msg:
                        self._last_monitor_error = err_msg
                        self._append_notif(f"  ✖ Cannot open {port}: {e}", "error")
                    if _session_current():
                        with self._serial_state_lock:
                            self.serial_running = False
                            if self.serial_conn is conn:
                                self.serial_conn = None
                        self._set_serial_status(False)
                        self._silent_reset = False
                        if getattr(self, "_monitor_should_run", False) and not (getattr(self, "is_busy", False) and getattr(self, "_active_operation", None) in ("upload", "flash", "reset")):
                            self._schedule_auto_start_monitor(2000)
                    return

        if not _session_current():
            try:
                conn.close()
            except Exception:
                pass
            return
        with self._serial_state_lock:
            self.serial_running = True
        self._monitor_should_run = True
        self._first_connect_done = True
        self._set_serial_status(True)
        if is_uno:
            self._append_notif(f"  ✔ Connected — {port} @ {baud}  [Output captured]", "success")
        else:
            if not silent:
                self._append_notif(f"  ✔ Connected — {port} @ {baud}", "success")

        # _silent_reset is a one-shot flag (set by folder-switch auto-reset);
        # clear it here so future connects/messages aren't silenced forever.
        self._silent_reset = False

        # ── ESP boot-loop detection (complete-line only) ─────────────────────
        # Never inspect/delete arbitrary raw byte chunks here. Windows/pyserial is
        # free to split one ROM line anywhere, e.g. ``rst:0x3 (RTC_SW_SYS`` in
        # one read and ``_RST),boot:...`` in the next. Filtering before CR/LF
        # reconstruction was the cause of truncated Serial Monitor rows such as
        # ``_RST),boot:0x8``. Detect reset loops only after a complete line has
        # been reconstructed. Detection is informational only; no complete ROM
        # lines are hidden, so a blank Hard Reset board remains visibly active.
        _boot_cycle_seen = [0]
        _boot_loop_notified = [False]
        _BOOT_LINE_PREFIXES = (
            "esp-rom:", "build:", "rst:0x", "saved pc:", "spiwp:",
            "configsip:", "clk_drv:", "mode:", "load:0x", "entry 0x",
        )
        _OLD_ESP_ROM_BANNER_RE = re.compile(
            r"^ets\s+[a-z]{3}\s+\d{1,2}\s+\d{4}\s+\d{2}:\d{2}:\d{2}",
            re.IGNORECASE,
        )

        def _is_boot_cycle_start(raw_text: str) -> bool:
            text = raw_text.strip()
            low_t = text.lower()
            return low_t.startswith("esp-rom:") or bool(_OLD_ESP_ROM_BANNER_RE.match(text))

        def _observe_complete_boot_loop_line(raw_text: str) -> None:
            """Observe complete ESP ROM lines without ever suppressing serial output.

            A Hard Reset intentionally leaves ESP-family boards without application
            firmware, so the ROM/second-stage bootloader can restart repeatedly.  The
            Serial Monitor must remain a faithful live terminal: after detecting a
            reset loop we emit one informational notification, but every complete ROM
            line continues to be displayed.
            """
            text = raw_text.strip()
            if not text:
                return

            if _is_boot_cycle_start(text):
                _boot_cycle_seen[0] += 1
                if _boot_cycle_seen[0] >= 5 and not _boot_loop_notified[0]:
                    _boot_loop_notified[0] = True
                    self._append_notif("", "")
                    self._append_notif(
                        f"  ⚠ Repeated ESP boot resets detected on {board_name}.",
                        "warning",
                    )
                    self._append_notif(
                        "  ℹ Serial Monitor will keep showing every complete ROM boot "
                        "line. If this followed Hard Reset, upload a sketch to install "
                        "application firmware.",
                        "info",
                    )

        def _resync_complete_boot_line(raw_text: str) -> str:
            """Recover a known ESP ROM banner after a reset tears an UART line.

            A very fast reset can interrupt a line in flight and the next ROM banner
            can begin immediately, occasionally yielding text such as
            ``Build\ufffdESP-ROM:esp32s3-...``.  This is not a byte-buffer slicing bug;
            it is an incomplete line followed by a fresh, recognizable banner.  When
            the replacement-character marker proves the prefix was damaged, discard
            only that damaged prefix and resume at the canonical ROM banner.
            """
            text = str(raw_text or "")
            low_t = text.lower()
            marker = low_t.find("esp-rom:")
            if marker > 0 and "\ufffd" in text[:marker]:
                return text[marker:]
            return text

        def _buffer_looks_like_boot_line(byte_buf: bytes) -> bool:
            """Keep a partial ROM line buffered until its CR/LF terminator arrives."""
            if not byte_buf:
                return False
            text = byte_buf.decode("utf-8", errors="replace").lstrip()
            low_t = text.lower()
            if low_t.startswith(_BOOT_LINE_PREFIXES) or low_t.startswith("esp-rom:"):
                return True
            # Classic ESP32 ROM banner: ``ets Jul 29 2019 12:21:46``. The
            # prefix can arrive before the full date/time, so preserve any ``ets ``
            # partial as well instead of flushing it after the 80 ms idle window.
            return low_t.startswith("ets ")

        def _drain_complete_serial_lines(byte_buf: bytearray) -> list[bytes]:
            """Remove and return every complete CR/LF-delimited line in one scan.

            Repeated ``bytes.find`` + front slicing is fine at 115200, but at
            921600 a single driver read can contain many lines.  Scan that chunk
            once, preserve the incomplete tail, and treat CRLF/LFCR as one line
            ending.  Empty separator-only rows are harmless and ignored later.
            """
            if not byte_buf:
                return []

            complete: list[bytes] = []
            start = 0
            i = 0
            size = len(byte_buf)
            while i < size:
                value = byte_buf[i]
                if value not in (10, 13):  # LF / CR
                    i += 1
                    continue

                complete.append(bytes(byte_buf[start:i]))
                first_sep = value
                i += 1
                if i < size and byte_buf[i] in (10, 13) and byte_buf[i] != first_sep:
                    i += 1
                start = i

            if start:
                del byte_buf[:start]
            return complete

        # ── Read loop ────────────────────────────────────────────────────
        # This is the actual monitor: keep pulling bytes off the port and
        # pushing complete lines into the Serial Monitor panel until either
        # the connection drops or something (pause/restart/port-removal)
        # flips serial_running/_monitor_should_run off.
        buf = bytearray()
        last_read_time = time.monotonic()
        def _drain_tx_queue() -> None:
            for _ in range(32):
                try:
                    tx_generation, payload, display_text = self._serial_tx_queue.get_nowait()
                except queue.Empty:
                    break
                if tx_generation != generation:
                    continue
                try:
                    conn.write(payload)
                    self._append_serial(f"  » {display_text}", "dim")
                except (serial.SerialException, OSError) as exc:
                    self._append_serial(f"  ✖ Send failed: {exc}", "error")
                    break

        try:
            while _session_current() and self._monitor_should_run:
                _drain_tx_queue()
                try:
                    waiting = int(conn.in_waiting or 0)
                    if waiting > 0:
                        # Drain what the driver already has, but cap a single Python
                        # allocation so a pathological producer cannot monopolize the
                        # monitor thread for an arbitrarily large read.
                        chunk = conn.read(min(waiting, serial_read_cap))
                    else:
                        # Block for just one byte.  As soon as the first byte arrives,
                        # the next pass drains the rest of the driver's queue in bulk.
                        chunk = conn.read(1)
                except (serial.SerialException, OSError) as e:
                    if _session_current():
                        self._append_notif(f"  ✖ Serial connection lost: {e}", "error")
                    break

                if not chunk:
                    # Preserve partial ESP ROM/bootloader lines until a real CR/LF
                    # arrives. Ordinary application progress text may still be shown
                    # promptly when it intentionally has no newline. High baud gets a
                    # slightly longer idle threshold to avoid manufacturing fragments
                    # from a line whose bytes are still arriving in another USB packet.
                    if (
                        buf
                        and (time.monotonic() - last_read_time) > serial_partial_idle_s
                        and not _buffer_looks_like_boot_line(bytes(buf))
                    ):
                        text = bytes(buf).decode("utf-8", errors="replace").rstrip("\r")
                        buf.clear()
                        if text and not self._monitor_paused:
                            self._append_tagged_line(text, is_newline=False)
                    continue

                last_read_time = time.monotonic()
                buf.extend(chunk)

                # Pull every complete row from the current RX batch in one linear
                # scan.  This keeps the reader comfortably ahead of a 921600-baud
                # producer instead of repeatedly rescanning the same prefix.
                complete_rows = _drain_complete_serial_lines(buf)
                if not self._monitor_paused and complete_rows:
                    display_rows = []
                    for raw in complete_rows:
                        if not raw:
                            continue
                        text = raw.decode("utf-8", errors="replace").rstrip("\r")
                        if text:
                            text = _resync_complete_boot_line(text)
                            _observe_complete_boot_loop_line(text)
                            # Keep a giant single-line payload from becoming one
                            # giant Tk Text.insert operation.  Segments preserve
                            # every character; only the final segment ends the row.
                            segment_size = 16384
                            if len(text) > segment_size:
                                for pos in range(0, len(text), segment_size):
                                    end = min(len(text), pos + segment_size)
                                    display_rows.append((text[pos:end], end >= len(text)))
                            else:
                                display_rows.append((text, True))
                    self._append_tagged_lines(display_rows)

                # Guard against an application/binary stream that never emits a
                # line ending.  Flush a large bounded chunk rather than allowing RAM
                # to grow indefinitely.  Normal textual monitor traffic never hits
                # this path.
                if len(buf) > serial_no_newline_cap:
                    flush_size = 32768 if high_speed_serial else 16384
                    raw_partial = bytes(buf[:flush_size])
                    del buf[:flush_size]
                    text = raw_partial.decode("utf-8", errors="replace")
                    if text and not self._monitor_paused:
                        self._append_tagged_line(text, is_newline=False)
        finally:
            # This worker owns only `conn`; never close whatever a newer
            # generation may have installed into self.serial_conn.
            active_before_close = _session_current()
            if active_before_close and buf and not self._monitor_paused:
                text = bytes(buf).decode("utf-8", errors="replace").rstrip("\r")
                if text:
                    text = _resync_complete_boot_line(text)
                    _observe_complete_boot_loop_line(text)
                    self._append_tagged_line(text, is_newline=True)

            if conn is not None:
                try:
                    if hasattr(conn, "cancel_read"):
                        conn.cancel_read()
                except Exception:
                    pass
                try:
                    if getattr(conn, "is_open", False):
                        conn.close()
                except Exception:
                    pass

            active = _session_current()
            if active:
                with self._serial_state_lock:
                    self.serial_running = False
                    if self.serial_conn is conn:
                        self.serial_conn = None
                    if self.serial_thread is cur_thread:
                        self.serial_thread = None
                self._set_serial_status(False)
                if getattr(self, "_monitor_should_run", False) and not (
                    getattr(self, "is_busy", False)
                    and getattr(self, "_active_operation", None) in ("upload", "flash", "reset")
                ):
                    self._schedule_auto_start_monitor(2000)

