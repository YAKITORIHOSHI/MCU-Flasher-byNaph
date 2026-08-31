#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import os
import time
import json
import re
import threading
from datetime import datetime
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
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

class HardwarePortMixin(_Base):
    """Mixin providing HardwarePortMixin capabilities for MCUUploadGUI."""
    def _save_selected_port(self, port_name: str | None):
        """Save the currently selected COM port in the instance config asynchronously on thread pool."""
        def _do_save():
            try:
                config = load_gui_config()
                config["selected_port"] = port_name or ""
                save_gui_config(config)
            except Exception:
                pass
        self._run_bg_task(_do_save)

    def _refresh_ports(self, force_select_port=None, called_from_hotplug=False):
        """Scan serial ports and update the combobox, hiding ports that
        another live instance already has selected.
        If force_select_port is specified, it will select that port immediately.

        Enumeration (pyserial/WMI) plus the psutil occupancy scan used to run
        inline on the Tk thread here — freezing the UI for 100–500 ms on every
        dropdown open (postcommand) and at deferred init. The heavy scan now
        runs on a worker thread and results are applied back on the Tk thread;
        while a scan is in flight the dropdown opens instantly with the last
        known list."""
        # Paint last-known values immediately so postcommand never stalls.
        cached = getattr(self, "_last_port_scan_result", None)
        if cached is not None:
            try:
                self.port_combo["values"] = cached
            except Exception:
                pass

        if getattr(self, "_port_scan_inflight", False):
            # Coalesce bursts (hotplug + user click) into one follow-up scan.
            self._port_scan_pending = (force_select_port, called_from_hotplug)
            return

        self._port_scan_inflight = True
        self._port_scan_pending = None

        def _scan_worker():
            try:
                ports = list(serial.tools.list_ports.comports())
                occupied = get_occupied_ports()
            except Exception:
                ports, occupied = [], None
            try:
                self.root.after(
                    0,
                    lambda: self._apply_port_scan(ports, occupied, force_select_port, called_from_hotplug),
                )
            except Exception:
                # Window destroyed mid-scan; nothing left to update.
                self._port_scan_inflight = False

        threading.Thread(target=_scan_worker, name="PortScan", daemon=True).start()

    def _chain_pending_port_scan(self):
        """Run a scan that was requested while another one was in flight."""
        pending = getattr(self, "_port_scan_pending", None)
        self._port_scan_pending = None
        if pending is not None:
            try:
                self.root.after(80, lambda: self._refresh_ports(*pending))
            except Exception:
                pass

    def _apply_port_scan(self, ports, occupied_ports, force_select_port=None, called_from_hotplug=False):
        """Tk-thread application of a completed background port scan."""
        self._port_scan_inflight = False
        if occupied_ports is None:
            # Scan itself failed; retry a coalesced request if one queued up.
            self._chain_pending_port_scan()
            return

        # Computed once and reused for the whole pass, so the dropdown
        # contents, current-selection check, and auto-select fallback all
        # agree on the same snapshot of what's taken. This instance's own
        # claimed port is never in here (get_occupied_ports() excludes
        # _INSTANCE_ID), so it can never hide itself from its own dropdown.
        visible_ports = []
        for p in ports:
            if p.device in occupied_ports:
                continue
            desc = (p.description or "").lower()
            hwid = (p.hwid or "").lower()
            
            # Filter out standard Bluetooth serial links to avoid COM name collision/overlap
            if "bluetooth" in desc or "bthenum" in hwid:
                continue
            visible_ports.append(p)

        port_list = [f"{p.device}  -  {p.description or ''}" for p in visible_ports]

        # Remember for instant-paint on the next dropdown open.
        self._last_port_scan_result = port_list

        # Update last-known port set for hotplug detection. This tracks the
        # *physical* device set (not the occupancy-filtered view) using (device, hwid)
        # tuples so that port name collisions (e.g. sharing COM3) are detected properly.
        self._last_known_ports = {(p.device, p.hwid or "") for p in ports}

        self.port_combo["values"] = port_list

        # If the dropdown popup is currently visible AND we are handling a hotplug event,
        # dismiss and re-post it so the user sees the updated list without having to close/reopen.
        if called_from_hotplug:
            try:
                popdown_name = self.port_combo.tk.call("ttk::combobox::PopdownWindow", str(self.port_combo))
                popdown = self.root.nametowidget(popdown_name)
                if popdown.winfo_ismapped():
                    self.port_combo.event_generate('<Escape>')
                    self.root.after(30, lambda: self.port_combo.event_generate('<Button-1>'))
            except Exception:
                pass

        # Check if the current selection is still connected, visible, and valid
        current_val = self.port_var.get()
        current_device = ""
        if current_val:
            match = re.match(r"(COM\d+|/dev/\S+)", current_val)
            current_device = match.group(1) if match else current_val.split()[0]

        # If a force_select_port is requested (e.g. newly plugged known MCU on hotplug), select it immediately
        if force_select_port:
            for p in visible_ports:
                if p.device == force_select_port:
                    target_val = f"{p.device}  -  {p.description or ''}"
                    self.port_combo.set(target_val)
                    self._save_selected_port(p.device)
                    self._board_port_confirmed = False
                    self._update_hardware_action_buttons()
                    self._auto_select_board(show_msg=False)
                    if p.device and not self._port_is_avr_only():
                        threading.Thread(
                            target=self._auto_detect_board_from_port,
                            args=(p.device,),
                            daemon=True,
                        ).start()
                    self._sync_project_hardware_state()
                    self._chain_pending_port_scan()
                    return

        is_current_valid = False
        mcu_keywords = ["cp210", "ch34", "ch91", "ftdi", "esp32", "silicon labs", "wch", "jtag", "usb bridge", "usb", "serial", "arduino", "mcu"]
        if current_val:
            if current_val in port_list and current_device.upper() != "COM1":
                is_current_valid = True
            elif current_val in port_list and current_device.upper() == "COM1":
                # COM1 on PC motherboards is a generic Communications Port, NOT an MCU.
                # Only keep COM1 if it explicitly contains known MCU bridge signatures.
                for p in visible_ports:
                    if p.device.upper() == "COM1":
                        combined = f"{p.description} {p.hwid}".lower()
                        if any(kw in combined for kw in mcu_keywords) and "communications port" not in combined:
                            is_current_valid = True
                            break

        if is_current_valid:
            # Current selection is still valid, keep it and update config
            self._save_selected_port(current_device)
            self._auto_select_board(show_msg=False)
            if current_device and current_device.upper() != "COM1" and not self._port_is_avr_only():
                threading.Thread(
                    target=self._auto_detect_board_from_port,
                    args=(current_device,),
                    daemon=True,
                ).start()
            self._sync_project_hardware_state()
            self._chain_pending_port_scan()
            return

        # If the current selection is invalid, empty, or was just hidden:
        # Select a new one from what's still visible (strictly EXCLUDING COM1)
        unoccupied_ports = visible_ports

        mcu_keywords = ["cp210", "ch34", "ch91", "ftdi", "esp32", "silicon labs", "wch", "jtag", "usb bridge", "usb", "serial", "arduino", "mcu"]
        auto_port = None
        auto_port_device = None

        # 1. Search for MCU port first (excluding COM1)
        for p in unoccupied_ports:
            if p.device.upper() == "COM1":
                continue
            combined = f"{p.description} {p.hwid}".lower()
            if any(kw in combined for kw in mcu_keywords):
                auto_port = f"{p.device}  —  {p.description}"
                auto_port_device = p.device
                break

        # 2. Search for any non-COM1 port if no MCU port found
        if not auto_port:
            for p in unoccupied_ports:
                if p.device.upper() != "COM1":
                    auto_port = f"{p.device}  —  {p.description}"
                    auto_port_device = p.device
                    break

        if auto_port:
            self.port_combo.set(auto_port)
            self._save_selected_port(auto_port_device)
            self._board_port_confirmed = False
            self._update_hardware_action_buttons()
            self._auto_select_board(show_msg=False)

            if auto_port_device and auto_port_device.upper() != "COM1" and not self._port_is_avr_only():
                threading.Thread(
                    target=self._auto_detect_board_from_port,
                    args=(auto_port_device,),
                    daemon=True,
                ).start()
        else:
            # A port scan only owns the physical-port selection.  A board is
            # also the compile target, so it must remain available when no
            # device is connected (or while a device is temporarily absent).
            # Clearing board_var here made a clean/no-port project impossible
            # to compile even though the user had already selected a board.
            self.port_combo.set("")
            self.port_var.set("")
            self._save_selected_port("")
            self._board_port_confirmed = False
            self._update_hardware_action_buttons()

        self._sync_project_hardware_state()
        self._chain_pending_port_scan()

    def _start_port_polling(self):
        """Start a dedicated background thread to monitor serial ports in real-time
        for USB hotplug, MCU disconnections, and instance port occupancy changes."""
        self._last_known_occupied_ports: set[str] = get_occupied_ports()
        self._port_poll_active = True

        # Perform one-time initial hardware state sync on thread start so project_state.json
        # is immediately and accurately populated with current board & port availability
        try:
            self._post_ui(self._sync_project_hardware_state)
        except Exception:
            pass

        def _poll_thread_worker():
            poll_ticks = 0
            while getattr(self, "_port_poll_active", True):
                try:
                    cpus = os.cpu_count() or 4
                    poll_interval = 2.5 if cpus <= 4 else 2.0
                    time.sleep(poll_interval)
                    poll_ticks += 1

                    current_ports = {(p.device, p.hwid or "") for p in serial.tools.list_ports.comports()}
                    hardware_changed = current_ports != getattr(self, "_last_known_ports", set())

                    # Check occupancy when hardware changes or every ~3.6s
                    occupancy_changed = False
                    current_occupied = getattr(self, "_last_known_occupied_ports", set())
                    if hardware_changed or (poll_ticks % 3 == 0):
                        current_occupied = get_occupied_ports()
                        occupancy_changed = current_occupied != getattr(self, "_last_known_occupied_ports", set())

                    if hardware_changed or occupancy_changed:
                        self._post_ui(
                            lambda ports=current_ports, occupied=current_occupied:
                                self._handle_port_change(ports, occupied)
                        )
                    elif poll_ticks % 5 == 0:
                        # Periodic one-time heartbeat sync to ensure project_state.json always
                        # stays 100% current and fresh for AI assistants (OpenCode / Antigravity)
                        self._post_ui(self._sync_project_hardware_state)
                except Exception as e:
                    self._post_ui(
                        lambda exc=e: self._append_notif(f"  ✖ Port poll thread error: {exc}", "error")
                    )

        self._port_poll_thread = threading.Thread(target=_poll_thread_worker, name="RealtimePortMonitorThread", daemon=True)
        self._port_poll_thread.start()

    def _handle_port_change(self, current_ports, current_occupied):
        """Handle serial port additions, removals, and occupancy changes on the main Tk thread."""
        try:
            old = self._last_known_ports  # Set of (device, hwid)
            
            # Map them back to devices to detect added/removed COM ports
            old_devices = {dev for dev, hwid in old}
            new_devices = {dev for dev, hwid in current_ports}
            
            added_devs = new_devices - old_devices
            removed_devs = old_devices - new_devices

            # Check if any of the added devices is a known MCU
            has_new_known_mcu = False
            new_mcu_device = None
            if added_devs:
                mcu_keywords = ["cp210", "ch34", "ch91", "ftdi", "esp32", "silicon labs", "wch", "jtag", "usb bridge", "usb", "serial", "arduino", "mcu"]
                ports_info = serial.tools.list_ports.comports()
                for p in ports_info:
                    if p.device in added_devs:
                        combined = f"{p.description} {p.hwid}".lower()
                        if any(kw in combined for kw in mcu_keywords):
                            has_new_known_mcu = True
                            new_mcu_device = p.device
                            break
            
            self._last_known_occupied_ports = current_occupied
            self._last_known_ports = current_ports

            if added_devs:
                for p in added_devs:
                    self._append_notif(f"  🔌 USB device connected: {p}", "success")
            if removed_devs:
                for p in removed_devs:
                    self._append_notif(f"  ⚠ USB device disconnected: {p}", "warning")

            # Do not mutate active port selection, status bar, or trigger
            # auto-monitor starts while an upload/compile/reset operation is busy.
            if not getattr(self, "is_busy", False):
                current_port = self._get_port()

                # If the currently selected/monitored port was disconnected:
                if removed_devs and current_port and (current_port in removed_devs or current_port not in new_devices):
                    # Stop active serial monitor connection immediately
                    self._monitor_should_run = False
                    self._stop_serial_session()
                    self._set_serial_status(False)
                    self._board_port_confirmed = False
                    self._set_status(f"MCU disconnected ({current_port}) — Port cleared", Theme.YELLOW)

                # Auto-switch to newly connected MCU only if current port is not valid/recognized (e.g. empty, disconnected, or placeholder COM1)
                current_port = self._get_port()
                is_recognized = bool(
                    current_port 
                    and current_port.upper() != "COM1" 
                    and current_port in new_devices
                    and (getattr(self, "_board_port_confirmed", False) or self._is_valid_port() or self._port_is_avr_only())
                )
                
                force_select_port = None
                if has_new_known_mcu and not is_recognized:
                    force_select_port = new_mcu_device

                self._refresh_ports(force_select_port=force_select_port, called_from_hotplug=True)

                # If a new device appeared, auto-start monitor on it with hardware reset so setup() is captured
                if added_devs and not self.serial_running:
                    self._manual_reset_pending = True
                    self._schedule_auto_start_monitor(500)
            elif removed_devs:
                current_port = self._get_port()
                if current_port and (current_port in removed_devs or current_port not in new_devices):
                    # Stop serial monitor connection if disconnected while busy
                    self._monitor_should_run = False
                    self._stop_serial_session()
                    self._set_serial_status(False)

        except Exception as e:
            self._append_notif(f"  ✖ Error handling port change: {e}", "error")

    def _start_marquee(self):
        delay_ms = 1500  # Default to relaxed slow poll if nothing is scrolling
        
        # 1. Port marquee
        try:
            if self.port_combo.winfo_exists():
                pos = self.port_combo.xview()
                if pos[0] == 0.0 and pos[1] >= 0.999:
                    self._marquee_dir = 1
                    self._marquee_pause = 0
                else:
                    if getattr(self, '_marquee_pause', 0) > 0:
                        self._marquee_pause -= 1
                    else:
                        if pos[1] >= 0.999:
                            self._marquee_dir = -1
                            self._marquee_pause = 8  # pause at end
                        elif pos[0] <= 0.0:
                            self._marquee_dir = 1
                            self._marquee_pause = 8  # pause at start
                            
                        self.port_combo.xview_scroll(self._marquee_dir, "units")
                    delay_ms = 150
        except Exception:
            pass

        # 2. Board marquee (only if not currently focused by user)
        try:
            if hasattr(self, 'board_combo') and self.board_combo.entry.winfo_exists():
                if self.root.focus_get() != self.board_combo.entry:
                    pos = self.board_combo.entry.xview()
                    if pos[0] == 0.0 and pos[1] >= 0.999:
                        self._board_marquee_dir = 1
                        self._board_marquee_pause = 0
                    else:
                        if getattr(self, '_board_marquee_pause', 0) > 0:
                            self._board_marquee_pause -= 1
                        else:
                            if pos[1] >= 0.999:
                                self._board_marquee_dir = -1
                                self._board_marquee_pause = 8  # pause at end
                            elif pos[0] <= 0.0:
                                self._board_marquee_dir = 1
                                self._board_marquee_pause = 8  # pause at start
                                
                            self.board_combo.entry.xview_scroll(self._board_marquee_dir, "units")
                        delay_ms = 150
        except Exception:
            pass

        self.root.after(delay_ms, self._start_marquee)

    # ── Custom filterable dropdown (replaces ttk.Combobox for board select) ──
    #
    # Three rounds of patching ttk::combobox::Post's focus-grab timing
    # (ismapped-guard, then debounce, then after_idle refocus) produced
    # zero observable change to the reported symptom -- not "improved",
    # not "different", literally unchanged each time. That's a strong
    # signal the theory of the mechanism was wrong, not that the next
    # timing tweak will land. Compounding that: there's no tkinter or
    # network access in the environment these fixes were authored in, so
    # every one of those three attempts was reasoning about Tk's internal
    # event queue without ever being able to watch it run -- exactly the
    # situation where switching strategy beats a fourth guess.
    #
    # This replaces ttk.Combobox + Post entirely with a plain tk.Entry
    # (for typing) and a manually-managed tk.Toplevel popup (for the
    # filtered list), wired with ordinary Python event bindings only.
    # There is no Post call anywhere in this replacement, so Post's
    # internal focus grab -- whatever it actually does on this platform,
    # which was never confirmed -- cannot run, and cannot fight for focus
    # with the entry. The class still drives self.board_var (a
    # tk.StringVar) on every change and still calls _on_board_changed()
    # on confirmed selection, so all ~25 other call sites in this file
    # that read self.board_var.get() need no changes.
    def _build_board_dropdown(self, parent):
        """Constructs the clean board textfield + 🔍 Search Button (no combobox arrowdown)."""
        initial_board = ""
        self.board_var = tk.StringVar(value=initial_board)
        self._last_valid_board = initial_board

        self.board_entry = tk.Entry(
            parent,
            textvariable=self.board_var,
            width=26,
            justify="center",
            font=self.font_mono_sm,
            bg=Theme.BG_DARKEST,
            fg=Theme.TEXT_BRIGHT,
            readonlybackground=Theme.BG_DARKEST,
            disabledbackground=Theme.BG_DARKEST,
            disabledforeground=Theme.TEXT_DIM,
            relief=tk.FLAT,
            state="readonly",
            cursor="arrow",
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
            highlightcolor=Theme.CYAN,
        )
        pady_val = getattr(self, "_btn_pady", 3)
        self.board_entry.pack(side=tk.LEFT, padx=(0, 4), fill=tk.BOTH, expand=True, ipady=pady_val)

        # Alias for backward compatibility with external references
        self.board_entry.entry = self.board_entry
        self.board_combo = self.board_entry

        self.board_entry.bind("<Button-1>", lambda e: safe_reclaim_os_focus(self.board_entry), add="+")

        btn_search_board = self._make_btn(
            parent, "🔍", self._open_board_search_dialog,
            Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, font=self.font_mono_sm
        )
        btn_search_board.pack(side=tk.LEFT, fill=tk.Y)
        self.btn_search_board = btn_search_board

    def _open_board_search_dialog(self):
        if getattr(self, "board_entry", None) and str(self.board_entry.cget("state")) == "disabled":
            return
        dlg = BoardSearchDialog(
            self.root,
            current_board=self.board_var.get(),
            board_list=SUPPORTED_BOARDS.keys(),
            on_select_callback=self._select_board_from_dialog
        )
        safe_reclaim_os_focus(dlg.search_ent)

    def _select_board_from_dialog(self, selected_board):
        if selected_board == self.board_var.get():
            return
        self.board_var.set(selected_board)
        self._on_board_changed()

    def _on_board_changed(self):
        """Handle board selection change."""
        old_board = getattr(self, "_last_valid_board", "")
        board_name = self.board_var.get()

        if old_board and old_board == board_name:
            return

        self._last_valid_board = board_name

        if not board_name:
            # Nothing selected (yet) — nothing to configure or report.
            self._board_changed_no_port_msg = None
            self._update_hardware_action_buttons()
            return

        if old_board and old_board != board_name:
            self._board_changed_no_port_msg = f"Board Changed: {old_board} >>> {board_name} | No port selected!"
            self._append_notif(f"  🔀 Board changed: \"{old_board}\" → \"{board_name}\"", "info")
        else:
            self._board_changed_no_port_msg = None
            if not old_board:
                self._append_notif(f"  >>> Board set to \"{board_name}\" <<<", "success")

        self._set_status(f"Board changed to {board_name}", Theme.CYAN)

        # Configure UPLOAD SPD based on platform:
        #   - AVR (Arduino Uno): force 115200 and lock the combobox (disabled)
        #   - ESP32: set to stable high speed (460800) and keep combobox editable
        #   - Others: just show the combobox normally
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        platform = board_info.get("platform", "")
        is_avr = (platform == "atmelavr")
        is_esp32 = (platform == "espressif32")
        if is_avr:
            self.upload_speed_var.set("115200")
            self.upload_speed_combo.configure(state="disabled")
            # Arduino Uno sketches conventionally use 9600 baud. Keep both the
            # internal and visible Serial Monitor controls synchronized so a
            # successful upload cannot reconnect at the previous ESP32 115200.
            self.baud_var.set("9600")
            if hasattr(self, "serial_baud_var"):
                self.serial_baud_var.set("9600")
        else:
            if is_esp32:
                self.upload_speed_var.set("460800")
                self.baud_var.set("115200")
                if hasattr(self, "serial_baud_var"):
                    self.serial_baud_var.set("115200")
            self.upload_speed_combo.configure(state="readonly")

        self._restart_monitor(f"board → {board_name}")
        self._update_skip_compile_state()
        self._update_hardware_action_buttons()

        # Remember this (port -> board) pairing for next time, as long as a
        # real physical port is currently selected. Harmless to re-write the
        # same value every time this fires.
        port_device = self._extract_port_device(self.port_var.get())
        if port_device:
            remember_port_board(port_device, board_name)

        # Sync active target hardware state to .mcu_flasher_build_cache/project_state.json
        self._sync_project_hardware_state()

    def _on_port_changed(self):
        """Handle port selection change — stop the current monitor,
        reconnect on the new port, and kick off esptool chip detection."""
        port_label = self.port_var.get()
        self._board_port_confirmed = False
        self._update_hardware_action_buttons()
        if not port_label:
            self._save_selected_port("")
            # Clearing a physical port must not clear the selected compile
            # target.  Compile is intentionally board-only and remains valid
            # without a connected MCU.
            self._set_status("Port cleared", Theme.CYAN)
            self._restart_monitor("port cleared")
            self._sync_project_hardware_state()
            return

        # Extract just the device name for the status message
        match = re.match(r"(COM\d+|/dev/\S+)", port_label)
        port_name = match.group(1) if match else port_label.split()[0]
        
        # Check if another window is already using this port
        owner_pid = port_occupied_owner(port_name)
        if owner_pid:
            if self.root.winfo_exists():
                from tkinter import messagebox
                messagebox.showerror(
                    "Port In Use",
                    f"Port '{port_name}' is currently in use by another MCU Flasher window (PID {owner_pid}).\n\n"
                    "This window cannot connect to or control a port when it is used by another instance.",
                    parent=self.root,
                )
            self._save_selected_port("")
            self.port_var.set("")
            self._set_status(f"Port {port_name} in use by PID {owner_pid}", Theme.YELLOW)
            self._restart_monitor("port occupied by another window")
            self._sync_project_hardware_state()
            return

        # Save to config
        self._save_selected_port(port_name)

        self._set_status(f"Port changed to {port_name}", Theme.CYAN)
        self._restart_monitor(f"port → {port_name}")
        
        # Fast local auto-select based on project files + port chip description
        self._auto_select_board(show_msg=True)

        # Sync active target hardware state to .mcu_flasher_build_cache/project_state.json
        self._sync_project_hardware_state()

        # Skip esptool chip probing when the port or selected board is confirmed
        # non-Espressif (e.g. AVR, STM32, RP2040).
        if self._port_is_non_espressif() or not port_name:
            return
            
        # Kick off chip auto-detection in background — non-blocking
        threading.Thread(
            target=self._auto_detect_board_from_port,
            args=(port_name,),
            daemon=True,
        ).start()

    def _detect_board_from_descriptor(self, port: str) -> str | None:
        """Apply only the three intentionally-supported passive port heuristics.

        These mappings are deliberately conservative.  A USB descriptor normally
        identifies the bridge/USB interface, not the exact PCB, so the application
        does not try to infer XIAO/Bee/M5/etc. from VID/PID, vendor names, or generic
        MCU-family clues here.

        Supported common signatures only:
          * Silicon Labs / CP210x-style descriptor -> generic ESP32 Dev Module
          * USB-SERIAL (classic hyphenated WCH-style label) -> Arduino Uno
          * USB-Enhanced-SERIAL / USB Enhanced SERIAL -> generic ESP32-S3 Dev Module

        Anything else returns None and leaves the user's board selection untouched.
        """
        try:
            port_device = self._extract_port_device(port) or str(port or '').strip()
            if not port_device:
                return None

            for candidate in serial.tools.list_ports.comports():
                if candidate.device.upper() != port_device.upper():
                    continue

                raw_text = " ".join((
                    candidate.description or "",
                    candidate.product or "",
                    candidate.manufacturer or "",
                    candidate.hwid or "",
                ))
                text = re.sub(r"\s+", " ", raw_text).strip().lower()
                compact = re.sub(r"[^a-z0-9]+", "", text)

                # 1) Common CP210x/Silicon Labs bridge used by classic ESP32
                #    development modules.  Prefer the generic family board, never
                #    a vendor-specific board sharing the same ESP32 silicon.
                if "silicon labs" in text or "siliconlabs" in compact:
                    return find_board_for_platform("espressif32", variant_hint="esp32")

                # 2) Common S3 USB-enhanced serial descriptor.  Check this BEFORE
                #    the generic USB-SERIAL rule so an enhanced S3 interface can
                #    never fall through to Arduino Uno.
                enhanced_serial = (
                    "usb-enhanced-serial" in text
                    or "usb enhanced serial" in text
                    or "usb-enchanced-serial" in text  # tolerate common misspelling
                    or "usbenhancedserial" in compact
                    or "usbenchancedserial" in compact
                )
                if enhanced_serial:
                    return find_board_for_platform("espressif32", variant_hint="esp32s3")

                # 3) Classic Arduino/clone descriptor.  Intentionally require the
                #    hyphenated USB-SERIAL spelling (or CH340/CH341 beside it) so a
                #    generic Windows "USB Serial Device" CDC interface is NOT
                #    misclassified as an Uno.
                classic_usb_serial = (
                    "usb-serial" in text
                    and "enhanced" not in text
                    and "enchanced" not in text
                )
                if classic_usb_serial:
                    return find_arduino_uno_board()

                return None
        except Exception:
            pass
        return None


    def _auto_detect_board_from_port(self, port: str, _attempt: int = 1, _max_attempts: int = 4):
        """Background worker: probe *port* to auto-select board safely.

        Prioritizes non-disruptive USB descriptor matching so already-running
        MCUs attached at startup are never reset or interrupted by esptool.
        """
        if port_occupied_owner(port):
            return

        # Compilation owns the machine while it runs; don't compete with it
        # through esptool probes or retry timers.
        if self._compile_background_lock.is_set():
            return

        # Bail out early if the user has since switched to a different
        # port — no point continuing to retry probing a stale target.
        if self._extract_port_device(self.port_var.get()) != port:
            return

        # 1. Non-disruptive USB Descriptor Check (Fast, zero-reset)
        descriptor_board = self._detect_board_from_descriptor(port)
        if descriptor_board:
            def _apply_desc():
                if self.board_var.get() != descriptor_board and descriptor_board in SUPPORTED_BOARDS:
                    self.board_var.set(descriptor_board)
                    self._on_board_changed()
                    self._append(
                        f"  🔌 Auto-detected board on {port}: \"{descriptor_board}\"",
                        "info",
                    )
                self._board_port_confirmed = True
                self._update_hardware_action_buttons()
                self._sync_project_hardware_state()
            self.root.after(0, _apply_desc)
            return

        # 2. Non-disruptive fallback: do NOT run esptool live probes on port selection/startup.
        # Live esptool probing toggles DTR/RTS into ROM bootloader mode and forces a hardware reset.
        # Arduino IDE never probes chips with esptool on startup — it relies strictly on USB descriptors,
        # remembered board history, and sketch auto-selection so already-running MCUs are never reset.
        def _non_disruptive_fallback():
            self._auto_select_board(show_msg=True)
            self._board_port_confirmed = True
            self._update_hardware_action_buttons()
            self._sync_project_hardware_state()
            self._schedule_auto_start_monitor(50)
        self.root.after(0, _non_disruptive_fallback)

    def _on_baud_changed(self):
        """Handle a real baud-rate change; ignore re-selecting the current value."""
        baud = str(self.baud_var.get()).strip()
        current_baud = (
            str(self.serial_baud_var.get()).strip()
            if hasattr(self, "serial_baud_var") else baud
        )
        # ttk.Combobox emits <<ComboboxSelected>> even when the user picks the
        # item that is already selected.  Treat that as a no-op so it cannot
        # restart the Serial Monitor or pulse/reset the attached MCU.
        if baud == current_baud:
            return

        self._set_status(f"Baud rate changed to {baud}", Theme.CYAN)
        self._set_serial_status("reconnecting")
        # Also sync the serial monitor tab baud var.
        if hasattr(self, "serial_baud_var"):
            self.serial_baud_var.set(baud)
        self._restart_monitor(f"baud → {baud}")
        self._sync_project_hardware_state()

    def _on_serial_baud_changed(self):
        """Handle a real Serial Monitor baud change; ignore same-value re-selection."""
        baud = str(self.serial_baud_var.get()).strip()
        current_baud = str(self.baud_var.get()).strip()
        # Selecting the currently-active entry (for example 115200 -> 115200)
        # still fires <<ComboboxSelected>>.  Do not convert that UI event into
        # a reconnect/reset when the effective baud rate did not change.
        if baud == current_baud:
            return

        self._set_status(f"Serial monitor baud rate: {baud}", Theme.CYAN)
        self._set_serial_status("reconnecting")
        # Also sync the main/internal baud var.
        self.baud_var.set(baud)
        self._restart_monitor(f"baud → {baud}")
        self._sync_project_hardware_state()

    def _detect_sketch_baud_rate(self) -> str | None:
        """Scan active sketch directory for Serial.begin(...) calls and return a valid baud rate string, or None if invalid or not found."""
        if not hasattr(self, "sketch_dir_path") or not self.sketch_dir_path:
            return None
        try:
            sketch_dir = Path(self.sketch_dir_path)
            if not sketch_dir.exists() or not sketch_dir.is_dir():
                return None

            macros: dict[str, int] = {}
            sketch_files = (
                list(sketch_dir.glob("*.ino")) +
                list(sketch_dir.glob("*.cpp")) +
                list(sketch_dir.glob("*.h")) +
                list(sketch_dir.glob("*.hpp"))
            )

            for file_path in sketch_files:
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                # Strip C/C++ single-line and multi-line comments before matching
                content_clean = re.sub(r'//.*?\n|/\*.*?\*/', '', content, flags=re.DOTALL)

                for macro_match in re.finditer(r"#define\s+([A-Za-z_][A-Za-z0-9_]*)\s+([0-9]+)\b", content_clean):
                    macros[macro_match.group(1)] = int(macro_match.group(2))
                for const_match in re.finditer(r"(?:const\s+)?(?:unsigned\s+)?(?:long|int|uint32_t)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([0-9]+)\b", content_clean):
                    macros[const_match.group(1)] = int(const_match.group(2))

                for match in re.finditer(r"\bSerial[0-9A-Za-z_]*\.begin\s*\(\s*([A-Za-z0-9_]+)\b", content_clean):
                    arg = match.group(1)
                    val = None
                    if arg.isdigit():
                        val = int(arg)
                    elif arg in macros:
                        val = macros[arg]

                    if val in VALID_BAUD_RATES:
                        return str(val)
        except Exception:
            pass
        return None

    def _on_upload_speed_changed(self):
        """Handle upload speed selection change — updates platformio.ini asynchronously on thread pool when CPU is ready."""
        speed = self.upload_speed_var.get()

        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        is_avr = (board_info.get("platform", "") == "atmelavr")
        if is_avr and speed != "115200":
            self._append(
                f"  ⚠ {board_name} (AVR) requires upload_speed = 115200 — ignoring {speed}.",
                "warning",
            )
            self.upload_speed_var.set("115200")
            speed = "115200"

        self._set_status(f"Upload speed changed to {speed}", Theme.CYAN)
        self._sync_project_hardware_state()

        def _update_ini_task():
            ini_path = self._platformio_ini_path()
            with _PLATFORMIO_INI_WRITE_LOCK:
                if ini_path.exists():
                    content = ini_path.read_text(encoding="utf-8", errors="replace")
                    if re.search(r"^upload_speed\s*=", content, re.MULTILINE):
                        content = re.sub(
                            r"^upload_speed\s*=.*",
                            f"upload_speed = {speed}",
                            content,
                            flags=re.MULTILINE,
                        )
                    else:
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1" + f"upload_speed = {speed}\n",
                            content,
                            count=1,
                        )
                    if not self._force_write_text(ini_path, content):
                        raise OSError(
                        "platformio.ini update failed after retries" +
                        (f": {getattr(self, '_last_platformio_ini_write_error', '')}"
                         if getattr(self, '_last_platformio_ini_write_error', '') else "")
                    )
                    return speed
                return None

        def _on_done(res):
            if res:
                self._append(f"  ⚙  upload_speed set to {res} in platformio.ini", "info")

        def _on_err(exc):
            self._append(f"  ⚠ Could not update upload_speed in platformio.ini: {exc}", "warning")

        self._run_bg_task(_update_ini_task, on_success=_on_done, on_error=_on_err)

    def _auto_select_board(self, show_msg: bool = True) -> str | None:
        """Auto-select only from the three explicitly-supported common descriptors.

        Unrecognized ports never consume the remembered COM-port board cache and
        never trigger a best-guess family mapping; the current/manual board choice
        is left untouched.
        """
        port_device = self._extract_port_device(self.port_var.get())
        if not port_device:
            return None

        detected = (
            self._detect_board_from_descriptor(self.port_var.get())
            or self._detect_board_from_descriptor(port_device)
        )

        # Only the three explicit descriptor signatures above are allowed to
        # auto-select a board.  Do NOT restore a remembered COM-port mapping for
        # an unrecognized descriptor: Windows can reuse COM numbers for entirely
        # different hardware, and the user's requested behavior is to leave the
        # current/manual board choice untouched in every other case.
        target_board = detected
        if not target_board or target_board not in SUPPORTED_BOARDS:
            return None

        if self.board_var.get() == target_board:
            self._board_port_confirmed = True
            return target_board  # already selected — nothing to do

        self.board_var.set(target_board)
        self._board_port_confirmed = True
        self._on_board_changed()
        if show_msg:
            self._append(
                f"  🔌 Auto-detected common port signature on {port_device}: \"{target_board}\"",
                "info"
            )
        return target_board

    def _get_usb_chip_board_families(self) -> dict:
        """Return only the three intentionally-recognized common port signatures.

        This helper is used for lightweight port-family validation.  Keep it in
        lockstep with ``_detect_board_from_descriptor`` so an unknown bridge never
        becomes an implicit board guess elsewhere in the application.
        """
        return {
            # Check enhanced serial before generic USB-SERIAL.
            "usb-enhanced-serial": ({"espressif32"}, "USB-Enhanced-SERIAL (common ESP32-S3)"),
            "usb enhanced serial": ({"espressif32"}, "USB Enhanced SERIAL (common ESP32-S3)"),
            "usb-enchanced-serial": ({"espressif32"}, "USB-Enchanced-SERIAL (common ESP32-S3)"),
            "silicon labs":        ({"espressif32"}, "Silicon Labs USB-serial (common ESP32)"),
            "usb-serial":          ({"atmelavr"}, "USB-SERIAL (common Arduino Uno/clone)"),
        }


    def _detect_port_chip(self) -> tuple[str, set, str] | None:
        """UI-facing selected-port wrapper around the snapshot-safe detector."""
        port = self._get_port()
        if not port:
            return None
        return self._detect_port_chip_for(port, self.port_var.get())

    def _detect_port_chip_for(self, port: str, port_label: str = "") -> tuple[str, set, str] | None:
        """Detect a USB/serial family without reading Tk Variables.

        This variant is safe for serial/background workers because all UI state
        arrives as immutable strings captured on the Tk thread.
        """
        if not port:
            return None
        search_targets = [str(port_label or "").lower(), str(port).lower()]
        try:
            for candidate in serial.tools.list_ports.comports():
                if candidate.device.upper() == str(port).upper():
                    if candidate.description:
                        search_targets.append(candidate.description.lower())
                    if candidate.manufacturer:
                        search_targets.append(candidate.manufacturer.lower())
                    if candidate.hwid:
                        search_targets.append(candidate.hwid.lower())
                    break
        except Exception:
            pass

        full_text = " ".join(search_targets)
        families = self._get_usb_chip_board_families()
        for keyword, (allowed_platforms, label) in families.items():
            if keyword == "com":
                continue
            if keyword in full_text:
                return (keyword, allowed_platforms, label)

        if re.match(r"^(COM\d+|/dev/\S+)", str(port), re.IGNORECASE):
            installed_platforms = {info.get("platform", "") for info in SUPPORTED_BOARDS.values()} - {""}
            if not installed_platforms:
                installed_platforms = {"espressif32", "espressif8266", "atmelavr"}
            return ("com", installed_platforms, f"Serial Port ({port})")
        return None

    def _port_is_avr_only(self) -> bool:
        """True only for the explicit USB-SERIAL -> Arduino Uno signature.

        Unknown descriptors are intentionally ambiguous and therefore never
        treated as AVR-only.
        """
        chip = self._detect_port_chip()
        if not chip:
            return False
        keyword, allowed_platforms, _label = chip
        if keyword == "usb-serial":
            descriptor_board = self._detect_board_from_descriptor(self._get_port() or "")
            return bool(
                descriptor_board
                and SUPPORTED_BOARDS.get(descriptor_board, {}).get("platform") == "atmelavr"
            )
        return False

    def _port_is_non_espressif(self) -> bool:
        """True when the current board or port descriptor is explicitly a non-Espressif platform (AVR, STM32, RP2040, etc.)."""
        if self._port_is_avr_only():
            return True
        current_board = self.board_var.get() if hasattr(self, "board_var") else ""
        if current_board and current_board in SUPPORTED_BOARDS:
            platform = str(SUPPORTED_BOARDS[current_board].get("platform", "")).lower()
            if platform and platform not in ("espressif32", "espressif8266"):
                return True
        return False

    def _is_board_recognized(self) -> bool:
        """True only once we have genuine confirmation of what's attached.
        However, we now disregard the 'unrecognized port' block to allow the user
        to compile/upload/reset to any selected port.
        """
        if not self.port_var.get():
            return False
        return True

    def _update_hardware_action_buttons(self):
        """Compile only needs a board *type* selected — it never talks to
        the physical port, so it's gated on board_var alone. Upload and the
        hardware Reset button additionally need the board on the selected
        port to actually be recognized and the port to be physically present."""
        if self.is_busy:
            return  # the running operation's own state machine owns button states right now
        board_selected = bool(self.board_var.get())

        btn_compile = getattr(self, "btn_compile", None)
        if btn_compile is not None:
            try:
                btn_compile.configure(state=tk.NORMAL if board_selected else tk.DISABLED)
            except Exception:
                pass

        port = self._get_port()
        port_present = bool(port and self._is_port_present(port))
        state = tk.NORMAL if (board_selected and self._is_board_recognized() and port_present) else tk.DISABLED
        for attr in ("btn_upload", "btn_reset_mcu"):
            btn = getattr(self, attr, None)
            if btn is not None:
                try:
                    btn.configure(state=state)
                except Exception:
                    pass

    def _is_native_usb_port(self) -> bool:
        port_label = self.port_var.get()
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        low_label = port_label.lower()
        if not low_label:
            return False
        native_keywords = ("esp32-s3", "esp32s3", "jtag", "usb bridge", "otg", "native", "usb serial device", "usb serial", "cdc", "usb debug")
        uart_keywords = ("ch340", "ch341", "ch342", "ch343", "cp210", "silicon labs", "ftdi", "wch")
        has_native = any(k in low_label for k in native_keywords)
        has_uart = any(k in low_label for k in uart_keywords)
        p_board = board_info.get("board", "")
        return bool(
            (has_native and not has_uart)
            or ((is_s3_board(p_board) or "s3" in board_name.lower()) and not has_uart)
        )

    def _is_valid_port(self) -> bool:
        """Check if the selected port's USB-serial chip is actually sold
        with the currently-selected board's platform."""
        if getattr(self, "_board_port_confirmed", False):
            return True
        val = self.port_var.get().lower()
        if not val:
            return False
        if "communications port" in val or val.strip() == "com1":
            return False

        board_name = self.board_var.get()
        if not board_name or board_name not in SUPPORTED_BOARDS:
            return True

        chip = self._detect_port_chip()
        if chip is None:
            generic_keywords = ["usb serial", "usb-serial", "uart", "usb", "serial", "jtag", "bridge"]
            return any(kw in val for kw in generic_keywords)

        _keyword, allowed_platforms, _label = chip
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        board_platform = board_info.get("platform", "")
        return board_platform in allowed_platforms

    def _port_mismatch_reason(self) -> str:
        """Build a human-readable explanation of why the selected port
        doesn't match the selected board, for error messages."""
        board_name = self.board_var.get()
        chip = self._detect_port_chip()
        if chip is None:
            return "port doesn't match any recognized USB-serial chip for this board"
        _keyword, allowed_platforms, label = chip
        platform_labels = {
            "atmelavr": "Arduino AVR",
            "espressif32": "ESP32",
            "espressif8266": "ESP8266"
        }
        allowed = " or ".join(platform_labels.get(p, p) for p in sorted(allowed_platforms))
        return f"{label} detected — that's a {allowed} board, not \"{board_name}\""

    def _unrecognized_mcu_port_warning(self, port: str, port_label: str | None = None) -> str | None:
        """Return the existing unrecognized-port warning using an optional UI snapshot."""
        if getattr(self, "_board_port_confirmed", False):
            return None

        if port_label is None:
            port_label = self.port_var.get()
        descriptor = str(port_label or "")
        port_info = None
        try:
            for candidate in serial.tools.list_ports.comports():
                if candidate.device.upper() == port.upper():
                    port_info = candidate
                    descriptor = candidate.description or descriptor
                    break
        except Exception:
            pass

        if port_info is not None and port_info.vid is not None and port_info.pid is not None:
            if (port_info.vid, port_info.pid) in DOWNLOADED_BOARD_USB_IDS:
                return None

        installed_platforms = {info.get("platform", "") for info in SUPPORTED_BOARDS.values()} - {""}
        chip = self._detect_port_chip_for(port, str(port_label or ""))
        if chip is not None and (chip[1] & installed_platforms):
            return None

        signature = (port.upper(), descriptor.lower())
        if signature in self._warned_unrecognized_port_signatures:
            return None
        self._warned_unrecognized_port_signatures.add(signature)

        if chip is None:
            reason = f'it reports as "{descriptor}" and has no known USB-microcontroller signature'
        else:
            reason = "its USB signature does not match any board platform currently installed"
        return (
            f"  Warning: {port} is not recognized as a microcontroller port from "
            f"the installed Download Boards/Libraries definitions - {reason}. "
            "COM1 is commonly a built-in PC serial port.\n"
            "  💡 Note: The board definition for this MCU might not be downloaded yet. "
            "You can download it using the Download Manager (src/modules/arduino_lib_req.py)."
        )

    def _extract_port_device(self, port_label: str) -> str:
        """Pull just the device name (e.g. 'COM16') out of a port combobox
        label like 'COM16  —  Silicon Labs CP210x...'. Unlike _get_port(),
        this never logs an error for an empty/missing label — it's used for
        passive lookups (e.g. the remembered port→board cache) where a
        blank port is completely normal and shouldn't be reported."""
        if not port_label:
            return ""
        match = re.match(r"(COM\d+|/dev/\S+)", port_label)
        return match.group(1) if match else port_label.split()[0]

    def _get_port(self) -> str | None:
        """Extract COM port name from the combobox."""
        val = self.port_var.get()
        if not val or val.startswith("─"):
            return None
        # Extract COMx from "COM8  —  Silicon Labs..."
        match = re.match(r"(COM\d+|/dev/\S+)", val)
        return match.group(1) if match else val.split()[0]

    def _is_port_present(self, port: str | None) -> bool:
        """True when the OS still enumerates `port` (e.g. 'COM7').

        Live hardware gate for the Upload pipeline: compilation never talks
        to the board, but flashing does — when the MCU is unplugged the port
        simply disappears from enumeration, and the upload phase must then be
        skipped.  Fail-open on enumeration errors so a transient WMI/SetupAPI
        hiccup never fabricates a disconnect; the uploader's own connect
        logic remains the final arbiter in that case."""
        if not port:
            return False
        target = str(port).strip().upper()
        if not target:
            return False
        try:
            for candidate in serial.tools.list_ports.comports():
                if str(candidate.device or "").upper() == target:
                    return True
        except Exception:
            return True
        return False

    def _setup_hardware_state_auto_sync(self):
        """Attach reactive traces to all hardware and settings Tk variables so project_state.json
        is automatically and immediately synchronized in real-time whenever any board, port,
        baud rate, or upload speed changes."""
        self._hardware_sync_job = None
        vars_to_trace = [
            ("board_var", getattr(self, "board_var", None)),
            ("port_var", getattr(self, "port_var", None)),
            ("upload_speed_var", getattr(self, "upload_speed_var", None)),
            ("baud_var", getattr(self, "baud_var", None)),
            ("serial_baud_var", getattr(self, "serial_baud_var", None)),
        ]
        for name, v in vars_to_trace:
            if isinstance(v, tk.StringVar):
                try:
                    v.trace_add("write", lambda *args, n=name: self._debounced_sync_project_hardware_state())
                except Exception:
                    pass

        # Perform initial real-time sync immediately on setup
        try:
            self._sync_project_hardware_state()
        except Exception:
            pass

    def _debounced_sync_project_hardware_state(self, delay_ms: int = 50):
        """Debounced sync to prevent disk thrashing when multiple vars update in quick succession."""
        job = getattr(self, "_hardware_sync_job", None)
        if job and hasattr(self, "root") and self.root:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
            self._hardware_sync_job = None

        if hasattr(self, "root") and self.root:
            try:
                self._hardware_sync_job = self.root.after(delay_ms, self._sync_project_hardware_state)
            except Exception:
                self._sync_project_hardware_state()
        else:
            self._sync_project_hardware_state()

    def _sync_project_hardware_state(self):
        """Write current board, microcontroller target, port, baud, and active settings to
        <sketch_dir>/.mcu_flasher_build_cache/project_state.json so AI assistants (OpenCode & Antigravity)
        can instantly know the active MCU architecture, pinouts, and COM connection in real-time.
        """
        sketch_dir = getattr(self, "sketch_dir_path", None)
        if not sketch_dir or not Path(sketch_dir).is_dir():
            return

        try:
            cache_dir = Path(sketch_dir) / PROJECT_BUILD_CACHE_DIR
            cache_dir.mkdir(parents=True, exist_ok=True)
            hide_generated_directory(cache_dir)

            port_label = (self.port_var.get() if hasattr(self, "port_var") else "") or ""
            if not port_label and hasattr(self, "port_combo") and self.port_combo:
                try:
                    port_label = self.port_combo.get() or ""
                except Exception:
                    pass

            port_device = self._extract_port_device(port_label) or ""
            if not port_device and hasattr(self, "_get_port"):
                try:
                    port_device = self._get_port() or ""
                except Exception:
                    pass

            # Filter generic motherboard Communications Port (COM1) so it is not mistaken for an attached MCU
            if port_device.upper() == "COM1":
                lbl = (port_label or "").lower()
                mcu_kws = ["cp210", "ch34", "ch91", "ftdi", "esp32", "silicon labs", "wch", "jtag", "usb bridge", "arduino", "mcu"]
                if "communications port" in lbl or not any(kw in lbl for kw in mcu_kws):
                    port_device = ""
                    port_label = ""

            board_name = (self.board_var.get() if hasattr(self, "board_var") else "") or ""
            board_name = board_name.strip()

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

            # Lookup board info with case/trim tolerance whenever board is selected
            board_info = {}
            if board_selected and board_name:
                board_info = SUPPORTED_BOARDS.get(board_name, {})
                if not board_info:
                    for b_name, b_data in SUPPORTED_BOARDS.items():
                        if b_name.strip().lower() == board_name.lower():
                            board_info = b_data
                            break

            editor_mode = get_editor_mode()
            clear_serial = get_clear_serial_on_upload()

            # Prefer serial_baud_var if active, then baud_var
            baud_val = ""
            if hasattr(self, "serial_baud_var") and self.serial_baud_var.get():
                baud_val = str(self.serial_baud_var.get()).strip()
            if not baud_val and hasattr(self, "baud_var"):
                baud_val = str(self.baud_var.get()).strip()
            if not baud_val:
                baud_val = "115200"

            upload_spd_val = self.upload_speed_var.get() if hasattr(self, "upload_speed_var") else "460800"
            upload_spd_val = str(upload_spd_val).strip()

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
                editor_mode,
                clear_serial,
            )

            # Avoid redundant disk writes and file attribute operations if state is identical
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
                "settings": {
                    "editor_mode": editor_mode,
                    "clear_serial_on_upload": clear_serial,
                },
                "last_updated": datetime.now().isoformat(timespec="seconds"),
            }

            state_file = cache_dir / "project_state.json"
            ensure_file_writable(state_file)
            state_file.write_text(json.dumps(state_data, indent=2, ensure_ascii=False), encoding="utf-8")
            ensure_hidden_read_first_md(sketch_dir)
        except Exception as exc:
            print(f"[MCU Flasher] Error syncing project state: {exc}")


