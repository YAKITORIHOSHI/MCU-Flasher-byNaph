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

class ConsoleSerialMixin(_Base):
    """Mixin providing ConsoleSerialMixin capabilities for MCUUploadGUI."""
    def _apply_monitor_font_size(self, size: int):
        """Apply one readable font size to Build, Serial, and Syntax tabs."""
        size = max(8, min(24, int(size)))
        self.monitor_font_size = size
        self.monitor_font.configure(size=size)
        self.monitor_font_bold.configure(size=size)
        self.monitor_font_header.configure(size=size + 1)
        if hasattr(self, "monitor_font_large_bold") and self.monitor_font_large_bold:
            self.monitor_font_large_bold.configure(size=size + 2)
        self.monitor_heading_font.configure(size=size)
        try:
            ttk.Style().configure("Syntax.Treeview", font=self.monitor_font)
            ttk.Style().configure("Syntax.Treeview.Heading", font=self.monitor_heading_font)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # CONSOLE OUTPUT
    # ──────────────────────────────────────────────────────────
    def _toggle_timestamps(self):
        show = self.timestamp_var.get()
        self.console.tag_configure("timestamp", elide=not show)
        self.serial_console.tag_configure("timestamp", elide=not show)
        if self.console_autoscroll_var.get():
            self.console.see(tk.END)
        if self.serial_autoscroll_var.get():
            self.serial_console.see(tk.END)

    def _append(self, text: str, tag: str = "", newline: bool = True):
        """Append text to console (thread-safe)."""
        def _do():
            self.console.configure(state=tk.NORMAL)
            if newline and text.strip():
                ts = datetime.now().strftime("%H:%M:%S")
                self.console.insert(tk.END, f"[{ts}] ", "timestamp")
            self.console.insert(tk.END, text + ("\n" if newline else ""), tag)
            total_lines = int(self.console.index("end-1c").split(".")[0])
            if total_lines > 2000:
                self.console.delete("1.0", f"{total_lines - 2000 + 1}.0")
            self.console.configure(state=tk.DISABLED)
            if self.console_autoscroll_var.get():
                self.console.see(tk.END)
        self._post_ui(_do)

    def _append_segments(self, segments, newline: bool = True):
        """Append a single line built from multiple (text, tag) segments,
        e.g. a dim label followed by a bright value, sharing one timestamp.
        Thread-safe, mirrors _append()."""
        def _do():
            self.console.configure(state=tk.NORMAL)
            has_content = any(seg_text.strip() for seg_text, _ in segments)
            if newline and has_content:
                ts = datetime.now().strftime("%H:%M:%S")
                self.console.insert(tk.END, f"[{ts}] ", "timestamp")
            for seg_text, seg_tag in segments:
                self.console.insert(tk.END, seg_text, seg_tag)
            if newline:
                self.console.insert(tk.END, "\n")
            total_lines = int(self.console.index("end-1c").split(".")[0])
            if total_lines > 2000:
                self.console.delete("1.0", f"{total_lines - 2000 + 1}.0")
            self.console.configure(state=tk.DISABLED)
            if self.autoscroll_var.get():
                self.console.see(tk.END)
        self._post_ui(_do)

    def _console_box_columns(self, min_cols: int = 50, default_cols: int = 100) -> int:
        """Return how many monospace character columns actually fit on one
        visual line of the build console right now, so boxed panels (like
        the chip info panel) can size themselves to the window instead of
        being cut mid-line by the console's own word-wrap.

        Accounts for the console's left/right padding and the width of the
        "[HH:MM:SS] " timestamp prefix that _append()/_append_segments()
        insert before every line. Falls back to default_cols if the widget
        hasn't been drawn yet (winfo_width() not yet meaningful)."""
        try:
            self.console.update_idletasks()
            widget_px = self.console.winfo_width()
            if widget_px <= 1:
                return default_cols
            padx = int(self.console.cget("padx") or 0)
            char_px = self.font_mono.measure("0")
            if char_px <= 0:
                return default_cols
            ts_px = self.font_mono.measure("[00:00:00] ")
            usable_px = widget_px - (padx * 2) - ts_px - 4  # small safety margin
            cols = usable_px // char_px
            return max(int(cols), min_cols)
        except Exception:
            return default_cols

    def _append_progress(self, text: str, tag: str = "", action_type: str = ""):
        """Append/update one live progress row for a specific action/package.

        ``action_type`` may be ``Downloading:<package>`` or
        ``Unpacking:<package>``.  Rows are only replaced when BOTH the phase and
        package match, so progress for one PlatformIO package can never overwrite
        another package's completed row.
        """
        def _do():
            self.console.configure(state=tk.NORMAL)
            last_line = self.console.get("end-2c linestart", "end-2c lineend")
            last_low = last_line.lower()

            act_low = action_type.lower() if action_type else ""
            if ":" in act_low:
                act_kind, act_item = act_low.split(":", 1)
            else:
                act_kind, act_item = act_low, ""
            act_kind = act_kind.strip()
            act_item = act_item.strip()

            # Never fabricate a missing 100% row when PlatformIO jumps directly
            # from downloading to unpacking.  Show exactly what PlatformIO
            # reported and simply start a new phase row.
            same_action = False
            if act_kind:
                same_action = act_kind in last_low
                if same_action and act_item:
                    same_action = act_item in last_low
            else:
                same_action = "downloading" in last_low or "unpacking" in last_low

            is_completed = ("100%" in last_line or "✔" in last_line)

            if same_action and not is_completed:
                ts_match = re.match(r'^\[\d+:\d+:\d+\]\s*', last_line)
                ts_prefix = ts_match.group(0) if ts_match else ""
                self.console.delete("end-2c linestart", "end-1c")
                self.console.insert(tk.END, ts_prefix, "timestamp")
                self.console.insert(tk.END, text + "\n", tag)
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                self.console.insert(tk.END, f"[{ts}] ", "timestamp")
                self.console.insert(tk.END, text + "\n", tag)

            total_lines = int(self.console.index("end-1c").split(".")[0])
            if total_lines > 2000:
                self.console.delete("1.0", f"{total_lines - 2000 + 1}.0")
            self.console.configure(state=tk.DISABLED)
            if self.console_autoscroll_var.get():
                self.console.see(tk.END)
        self._post_ui(_do)

    def _append_connecting_progress(self, current: int, total: int, bar_width: int = 30,
                                    connected: bool = False, force_new: bool = False,
                                    failed: bool = False):
        """Render a live progress bar in the console, replacing the previous
        connecting-bar line in place on every tick/retry.

        While attempts are still in flight the line reads
        '🔌 Connecting [ █████████░░░░░░░░░░░░░░░░░░░░░ ] | 3/10' (magenta,
        with the BOOT hint on retries).  Only when the chip has ACTUALLY
        synced (esptool uploaded its stub) should `connected=True` be passed,
        re-rendering the same line as '✔ Connected [ ... ] | 3/10' in green.
        Once the whole retry budget is exhausted pass `failed=True` to flip
        that same line, in place, to '🔌 Connecting [ ... ] | FAILED' (red)."""
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
        bar = "\u2588" * filled + "\u2591" * max(0, width - filled)

        def _do():
            self.console.configure(state=tk.NORMAL)
            total_lines_cnt = int(self.console.index("end-1c").split(".")[0])
            found_line_idx = None
            found_line_text = ""
            # The bar line can sit far above the current position by the time
            # it is re-rendered: between attempts and the flip moment, the
            # chip-info box + config block print ~15 lines.  Scan a wide
            # window (200) so we always replace the original bar in place
            # instead of appending a duplicate "Connected" line.
            if not force_new:
                for check_idx in range(total_lines_cnt, max(0, total_lines_cnt - 200), -1):
                    line_str = self.console.get(f"{check_idx}.0", f"{check_idx}.end")
                    if re.search(r'(?:connecting|connected)\s*\[.*\]\s*\|\s*\d+/\d+', line_str.lower()):
                        found_line_idx = check_idx
                        found_line_text = line_str
                        break

            if found_line_idx is not None:
                ts_match = re.match(r'^(\[\d+:\d+:\d+\])\s*', found_line_text)
                ts_prefix = (ts_match.group(1) + " ") if ts_match else ""
                self.console.delete(f"{found_line_idx}.0", f"{found_line_idx + 1}.0")
                ts_to_use = ts_prefix
                # Re-insert the re-rendered line at the ORIGINAL position —
                # never at tk.END, or the bar would jump below whatever has
                # printed since (chip-info box, config block, ...).  Use a
                # right-gravity Tk MARK as the insertion point: a fixed index
                # like "N.0" never advances (fragments come out reversed),
                # and manual column math breaks on wide emoji — 🔌 occupies
                # 2 widget columns while len() counts 1, so positions drift
                # and separator spaces get eaten.  A mark follows every
                # insert exactly, keeping order and columns correct.
                self.console.mark_set("_prog_ins_mark", f"{found_line_idx}.0")
                insert_mark = "_prog_ins_mark"
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                ts_to_use = f"[{ts}] "
                insert_mark = None

            def _ins(text, tag):
                idx = insert_mark if insert_mark is not None else tk.END
                self.console.insert(idx, text, tag)

            _ins(ts_to_use, "timestamp")
            if failed:
                _ins("  🔌 ", "error")
                _ins("Connecting ", ("bold", "error"))
                _ins(f"[ {bar} ]", ("bold", "error"))
                _ins(" | FAILED", "error")
                _ins(" >>  ", "error")
                _ins("💡 Please hold 'BOOT' button on MCU physical board", ("bold", "orange"))
            elif connected:
                _ins("  ✔ ", "success")
                _ins("Connected ", ("bold", "success"))
                _ins(f"[ {bar} ]", "success_bold_lg")
                _ins(f" | {current}/{total}", "success")
            else:
                _ins("  🔌 ", "magenta")
                _ins("Connecting ", ("bold", "magenta"))
                _ins(f"[ {bar} ]", "magenta_bold_lg")
                _ins(f" | {current}/{total}", "magenta")
                if current > 1:
                    _ins(" >>  ", "magenta")
                    _ins("💡 Please hold 'BOOT' button on MCU physical board", ("bold", "orange"))
            _ins("\n", "")
            if insert_mark is not None:
                try:
                    self.console.mark_unset(insert_mark)
                except Exception:
                    pass

            total_lines = int(self.console.index("end-1c").split(".")[0])
            if total_lines > 2000:
                self.console.delete("1.0", f"{total_lines - 2000 + 1}.0")
            self.console.configure(state=tk.DISABLED)
            if self.console_autoscroll_var.get():
                self.console.see(tk.END)
        self._post_ui(_do)

    def _append_upload_progress(self, label: str, stage: int, stage_total: int,
                                percent: float, written: int | None = None,
                                total: int | None = None,
                                force_new: bool = False) -> None:
        """Render one responsive, in-place firmware flashing row.

        esptool may emit dozens of percentage rows for a single image.  The
        console keeps one live row and replaces it in place, retaining the
        original timestamp.  A new upload passes ``force_new=True`` once so
        an earlier upload's completed row can never be overwritten.
        """
        pending = {
            "label": label,
            "stage": stage,
            "stage_total": stage_total,
            "percent": float(percent),
            "written": written,
            "total": total,
            "force_new": bool(force_new),
        }
        lock = getattr(self, "_upload_progress_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._upload_progress_lock = lock
        with lock:
            previous = getattr(self, "_upload_progress_pending", None)
            if previous:
                pending["force_new"] = bool(
                    pending["force_new"] or previous.get("force_new")
                )
            self._upload_progress_pending = pending
            if getattr(self, "_upload_progress_flush_scheduled", False):
                return
            self._upload_progress_flush_scheduled = True

        def _flush_latest():
            with lock:
                current = self._upload_progress_pending
                self._upload_progress_pending = None
                self._upload_progress_flush_scheduled = False
            if not current:
                return

            # All geometry and Text operations happen on Tk's thread. This is
            # important during rapid flashing and prevents worker/UI races.
            try:
                available_cols = self._console_box_columns(default_cols=105, min_cols=45)
            except Exception:
                available_cols = 105

            current_percent = float(current["percent"])
            current_written = current.get("written")
            current_total = current.get("total")
            show_bytes = (
                current_written is not None
                and current_total is not None
                and available_cols >= 88
            )
            bytes_suffix = (
                f" | {int(current_written):,}/{int(current_total):,} bytes"
                if show_bytes else ""
            )
            status = "✔ Flashed" if current_percent >= 99.95 else "⚡ Flashing"
            fixed_width = len(
                f"  {status} [{current['stage']}/{current['stage_total']}] "
                f"{current['label']} [  ] | {current_percent:.1f}%"
                + bytes_suffix
            )
            bar_width = min(30, max(8, available_cols - fixed_width - 2))
            row = _format_upload_progress_row(
                current["label"], current["stage"], current["stage_total"],
                current_percent,
                current_written if show_bytes else None,
                current_total if show_bytes else None,
                bar_width=bar_width,
            )

            self.console.configure(state=tk.NORMAL)
            total_lines_cnt = int(self.console.index("end-1c").split(".")[0])
            found_line_idx = None
            found_line_text = ""
            if not current["force_new"]:
                for check_idx in range(total_lines_cnt, max(0, total_lines_cnt - 250), -1):
                    line_str = self.console.get(f"{check_idx}.0", f"{check_idx}.end")
                    if re.search(r"(?:flashing|flashed)\s+\[\d+/\d+\]", line_str.lower()):
                        found_line_idx = check_idx
                        found_line_text = line_str
                        break

            if found_line_idx is not None:
                ts_match = re.match(r"^(\[\d+:\d+:\d+\])\s*", found_line_text)
                ts_prefix = (ts_match.group(1) + " ") if ts_match else ""
                self.console.delete(f"{found_line_idx}.0", f"{found_line_idx + 1}.0")
                self.console.mark_set("_upload_prog_ins_mark", f"{found_line_idx}.0")
                insert_at = "_upload_prog_ins_mark"
            else:
                ts_prefix = f"[{datetime.now().strftime('%H:%M:%S')}] "
                insert_at = tk.END

            self.console.insert(insert_at, ts_prefix, "timestamp")
            self.console.insert(
                insert_at, row + "\n",
                "success" if current_percent >= 99.95 else "magenta",
            )
            if found_line_idx is not None:
                try:
                    self.console.mark_unset("_upload_prog_ins_mark")
                except Exception:
                    pass

            total_lines = int(self.console.index("end-1c").split(".")[0])
            if total_lines > 2000:
                self.console.delete("1.0", f"{total_lines - 2000 + 1}.0")
            self.console.configure(state=tk.DISABLED)
            if self.console_autoscroll_var.get():
                self.console.see(tk.END)

        def _schedule_flush():
            # Let chip-info and metadata callbacks already queued for this
            # upload render first.  Without this small barrier, the final
            # progress row can overtake the preceding box and make one upload
            # look like two interleaved/throttled sessions.
            try:
                self.root.after(20, _flush_latest)
            except Exception:
                _flush_latest()

        self._post_ui(_schedule_flush)


    def _get_project_notif_db_path(self) -> str:
        """Return the notifications JSON database path for the active project.
        Saves inside <sketch_dir>/.mcu_flasher_build_cache/dbs_notif.json,
        or falls back to src/dbs/dbs_notif.json if no sketch directory is active.
        """
        sketch_dir = getattr(self, "sketch_dir_path", None)
        if sketch_dir and Path(sketch_dir).is_dir():
            cache_dir = Path(sketch_dir) / PROJECT_BUILD_CACHE_DIR
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                hide_generated_directory(cache_dir)
            except Exception:
                pass
            return str((cache_dir / "dbs_notif.json").resolve(strict=False))
        return str((SCRIPT_DIR / "src" / "dbs" / "dbs_notif.json").resolve(strict=False))

    def _append_notif(
        self,
        text: str,
        tag: str = "",
        newline: bool = True,
        category: str | None = None,
        title: str | None = None,
        persist: bool = True
    ):
        """Append text to the Notifications tab and persist to project's dbs_notif.json (thread-safe)."""
        def _do():
            now_dt = datetime.now()
            dt_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

            # Determine category & level if auto-detecting
            cat = category
            if not cat:
                low_text = text.lower()
                if "board" in low_text or "framework" in low_text:
                    cat = "board_install"
                elif "librar" in low_text:
                    cat = "library_install"
                elif "usb" in low_text or "connected" in low_text:
                    cat = "device"
                elif tag == "error" or "error" in low_text or "fail" in low_text:
                    cat = "error"
                else:
                    cat = "system"

            lvl = "info"
            if tag in ("success", "info", "warning", "error", "system", "header"):
                lvl = tag
            elif "✖" in text or "error" in text.lower() or "fail" in text.lower():
                lvl = "error"
            elif "⚠" in text or "warn" in text.lower():
                lvl = "warning"
            elif "✔" in text or "success" in text.lower():
                lvl = "success"

            # Check if category filter matches current view
            curr_filter = getattr(self, "_notif_filter_var", None)
            filter_val = curr_filter.get() if curr_filter else "All"
            should_display = True
            if filter_val == "📦 Boards & Libraries" and cat not in ("board_install", "library_install"):
                should_display = False
            elif filter_val == "🔌 USB Devices" and cat != "device":
                should_display = False
            elif filter_val == "✖ Errors" and lvl != "error":
                should_display = False

            if should_display:
                self.notif_console.configure(state=tk.NORMAL)
                if newline and text.strip():
                    self.notif_console.insert(tk.END, f"[{dt_str}] ", "timestamp")
                self.notif_console.insert(tk.END, text + ("\n" if newline else ""), tag or lvl or "info")
                total_lines = int(self.notif_console.index("end-1c").split(".")[0])
                if total_lines > 1500:
                    self.notif_console.delete("1.0", f"{total_lines - 1500 + 1}.0")
                self.notif_console.configure(state=tk.DISABLED)
                self.notif_console.see(tk.END)

            # Persist to database through the shared worker pool instead of
            # creating one new OS thread for every notification.
            if persist and text.strip():
                db_target = self._get_project_notif_db_path()
                def _bg_persist():
                    try:
                        dbs_create.add_notification(
                            category=cat,
                            level=lvl,
                            title=title or text.strip()[:50],
                            message=text.strip(),
                            db_path=db_target,
                        )
                    except Exception:
                        pass
                self._run_bg_task(_bg_persist)

        self._post_ui(_do)

    def _load_persistent_notifications(self, category_filter: str | None = None):
        """Load notification data off-thread, but render it only on Tk's thread."""
        db_target = self._get_project_notif_db_path()
        def _read_records():
            cat_query = None
            lvl_query = None
            if category_filter == "🔌 USB Devices":
                cat_query = "device"
            elif category_filter == "✖ Errors":
                lvl_query = "error"

            records = dbs_read.get_notifications(category=cat_query, level=lvl_query, limit=200, db_path=db_target)
            if category_filter == "📦 Boards & Libraries":
                records = [r for r in records if r.get("category") in ("board_install", "library_install")]
            return list(reversed(records))

        def _render(records):
            try:
                self.notif_console.configure(state=tk.NORMAL)
                self.notif_console.delete("1.0", tk.END)
                if not records:
                    self.notif_console.insert(tk.END, "  ℹ No saved notifications recorded yet for this project.\n", "dim")
                else:
                    insert_args: list[object] = []
                    for r in records:
                        date_str = r.get("date", "")
                        time_str = r.get("time", "")
                        ts_display = f"{date_str} {time_str}".strip() or r.get("timestamp", "")
                        msg = r.get("message", "")
                        lvl = r.get("level", "info")
                        tag = lvl if lvl in ("success", "info", "warning", "error") else "info"
                        insert_args.extend((f"[{ts_display}] ", "timestamp", f"{msg}\n", tag))
                    if insert_args:
                        self.notif_console.insert(tk.END, insert_args[0], *insert_args[1:])
                self.notif_console.configure(state=tk.DISABLED)
                self.notif_console.see(tk.END)
            except Exception as exc:
                print(f"[MCU Flasher] Error rendering persistent notifications: {exc}")

        self._run_bg_task(_read_records, on_success=_render)

    def _append_tagged_line(self, line: str, is_newline: bool = True):
        """Queue one parsed serial row; retained for all existing callers."""
        self._append_tagged_lines([(line, is_newline)])

    def _append_tagged_lines(self, rows):
        """Classify and enqueue a serial batch with one lock acquisition.

        The read worker often receives dozens/hundreds of complete lines in one
        USB packet.  Locking the display deque once per packet substantially
        reduces contention at high baud while preserving per-line tags/order.
        """
        if not rows:
            return
        entries = []
        for line, is_newline in rows:
            low = line.lower()
            if "error" in low or "fatal" in low or "fail" in low:
                tag = "error"
            elif "warning" in low or "warn" in low:
                tag = "warning"
            elif any(k in low for k in ("ok", "success", "done", "ready", "established")):
                tag = "success"
            elif "[debug]" in low:
                tag = "dim"
            elif line.startswith("[") and "]" in line:
                tag = "system"
            else:
                tag = ""
            entries.append((line, tag, bool(is_newline)))

        with self._serial_display_lock:
            maxlen = self._serial_display_queue.maxlen
            if maxlen:
                overflow = max(0, len(self._serial_display_queue) + len(entries) - maxlen)
                self._serial_display_dropped_rows += overflow
            self._serial_display_queue.extend(entries)

    def _serial_monitor_is_selected(self) -> bool:
        """Return whether the Serial Monitor tab is currently visible (Tk thread)."""
        try:
            if not getattr(self, "monitors_pane_visible", True):
                return False
            current = self.bottom_notebook.select()
            return bool(current) and self.bottom_notebook.index(current) == self._serial_monitor_tab_index()
        except Exception:
            return False

    def _serial_display_pump(self):
        """Tk-owned serial renderer; never lets terminal painting own a frame."""
        self._serial_display_pump_after_id = None
        try:
            visible = self._serial_monitor_is_selected()
            self._serial_tab_visible = visible
            if visible:
                self._flush_tagged_serial_lines()

            with self._serial_display_lock:
                backlog = len(self._serial_display_queue)
            high_rate = int(getattr(self, "_serial_display_flush_delay_ms", 25)) >= 40

            if not visible:
                delay_ms = 120
            elif backlog > 4000:
                delay_ms = 12
            elif backlog > 1000:
                delay_ms = 16
            elif high_rate:
                delay_ms = 20
            elif backlog:
                delay_ms = 25
            else:
                delay_ms = 40

            if self.root and self.root.winfo_exists():
                self._serial_display_pump_after_id = self.root.after(delay_ms, self._serial_display_pump)
        except Exception:
            try:
                self._serial_display_pump_after_id = self.root.after(120, self._serial_display_pump)
            except Exception:
                self._serial_display_pump_after_id = None

    def _flush_tagged_serial_lines(self):
        """Render a row/character-bounded batch on Tk's main thread.

        Character budgeting matters because one newline-free/binary chunk can be
        far more expensive than hundreds of short log rows.  The limits below
        comfortably exceed 921600-baud input throughput while bounding the work
        handed to a single Tk Text.insert call.
        """
        if not getattr(self, "_serial_tab_visible", False):
            return

        high_rate = int(getattr(self, "_serial_display_flush_delay_ms", 25)) >= 40
        max_rows = 300 if high_rate else 220
        max_chars = 32768 if high_rate else 24576
        lines = []
        chars = 0
        with self._serial_display_lock:
            while self._serial_display_queue and len(lines) < max_rows:
                item = self._serial_display_queue[0]
                item_chars = len(item[0]) + 1
                if lines and chars + item_chars > max_chars:
                    break
                lines.append(self._serial_display_queue.popleft())
                chars += item_chars
            dropped = self._serial_display_dropped_rows
            self._serial_display_dropped_rows = 0
        if not lines and not dropped:
            return

        try:
            self.serial_console.configure(state=tk.NORMAL)
            insert_args: list[object] = []
            batch_ts = datetime.now().strftime("%H:%M:%S")

            if dropped:
                insert_args.extend((f"[{batch_ts}] ", "timestamp"))
                insert_args.extend((f"… {dropped} serial rows skipped while the display was busy/hidden …\n", "warning"))
                self._serial_at_line_start = True

            ansi_clear_enabled = bool(getattr(self, "ansi_clear_var", None) and self.ansi_clear_var.get())
            for clean_text, tag, is_newline in lines:
                if ansi_clear_enabled and ANSI_CLEAR_RE.search(clean_text):
                    insert_args = []
                    self.serial_console.delete("1.0", tk.END)
                    self._serial_at_line_start = True
                    clean_text = ANSI_CLEAR_RE.sub("", clean_text)

                clean_text = ANSI_CSI_RE.sub("", clean_text)
                if not clean_text and not is_newline:
                    continue

                if self._serial_at_line_start and clean_text:
                    insert_args.extend((f"[{batch_ts}] ", "timestamp"))

                payload = clean_text + ("\n" if is_newline else "")
                if payload:
                    insert_args.extend((payload, tag or ""))
                self._serial_at_line_start = bool(is_newline)

            if insert_args:
                self.serial_console.insert(tk.END, insert_args[0], *insert_args[1:])

            now = time.monotonic()
            if now - self._serial_last_trim_monotonic >= 0.75:
                self._serial_last_trim_monotonic = now
                total_lines = int(self.serial_console.index("end-1c").split(".")[0])
                if total_lines > 2600:
                    keep_lines = 1700
                    self.serial_console.delete("1.0", f"{total_lines - keep_lines + 1}.0")

            self.serial_console.configure(state=tk.DISABLED)
            if self.serial_autoscroll_var.get() and now - self._serial_last_autoscroll_monotonic >= 0.06:
                self._serial_last_autoscroll_monotonic = now
                self.serial_console.see(tk.END)
        except tk.TclError:
            pass
        finally:
            try:
                if str(self.serial_console.cget("state")) != str(tk.DISABLED):
                    self.serial_console.configure(state=tk.DISABLED)
            except Exception:
                pass

    def _clear_console(self):
        self.console.configure(state=tk.NORMAL)
        self.console.delete("1.0", tk.END)
        self.console.configure(state=tk.DISABLED)

    def _clear_console_if_action_enabled(self):
        """Clear the Build Console if 'Clear Screen on Action' is enabled."""
        if getattr(self, "clear_build_console_on_action_var", None) and self.clear_build_console_on_action_var.get():
            self._clear_console()

    def _clear_serial_console(self):
        with self._serial_display_lock:
            self._serial_display_queue.clear()
            self._serial_display_dropped_rows = 0
        self._serial_at_line_start = True

        def _do():
            try:
                self.serial_console.configure(state=tk.NORMAL)
                self.serial_console.delete("1.0", tk.END)
                self.serial_console.configure(state=tk.DISABLED)
            except tk.TclError:
                pass
        self._post_ui(_do)

    def _append_serial(self, text: str, tag: str = "", newline: bool = True):
        """Append a manual/status line to Serial Monitor without cross-thread Tk."""
        def _do():
            try:
                self.serial_console.configure(state=tk.NORMAL)
                if not getattr(self, "_serial_at_line_start", True):
                    self.serial_console.insert(tk.END, "\n")
                    self._serial_at_line_start = True
                if newline and text.strip():
                    ts = datetime.now().strftime("%H:%M:%S")
                    self.serial_console.insert(tk.END, f"[{ts}] ", "timestamp")
                self.serial_console.insert(tk.END, text + ("\n" if newline else ""), tag)
                if newline:
                    self._serial_at_line_start = True
                total_lines = int(self.serial_console.index("end-1c").split(".")[0])
                if total_lines > 1800:
                    self.serial_console.delete("1.0", f"{total_lines - 1500 + 1}.0")
                self.serial_console.configure(state=tk.DISABLED)
                if self.serial_autoscroll_var.get():
                    self.serial_console.see(tk.END)
            except tk.TclError:
                pass
        self._post_ui(_do)

    def _toggle_serial_pause(self):
        """Pause/resume the Serial Monitor's live display. While paused, the
        port keeps being read in the background (so nothing backs up or
        drops the connection) — only the printing to the panel is held
        back. Anything that arrives while paused is not queued or replayed;
        it simply isn't shown, and display picks back up on resume."""
        self._monitor_paused = not self._monitor_paused
        if self._monitor_paused:
            self.btn_pause_serial.config(text="▶ Resume")
            self._append_serial("  ⏸ Serial monitoring paused — the port is still connected.", "dim")
        else:
            self.btn_pause_serial.config(text="⏸ Pause")
            self._append_serial("  ▶ Serial monitoring resumed.", "dim")

    def _activate_serial_monitor_after_success(self, action_name: str):
        """Unpause, reveal, and select the Serial Monitor after success.

        Upload/reset workers may finish off the Tk thread while the Serial
        Monitor tab is still temporarily disabled.  Clear the pause flag
        immediately so incoming bytes are displayed, then marshal the visual
        tab activation to Tk and retry briefly until the operation lock has
        released the tab.
        """
        was_paused = bool(getattr(self, "_monitor_paused", False))
        self._monitor_paused = False

        def _activate(attempt: int = 0):
            try:
                if hasattr(self, "btn_pause_serial") and self.btn_pause_serial.winfo_exists():
                    current_text = str(self.btn_pause_serial.cget("text") or "").strip()
                    compact = current_text in ("Pause", "Resume")
                    self.btn_pause_serial.configure(text="Pause" if compact else "⏸ Pause")

                # The entire Monitors pane may have been hidden by the user.
                # Reveal it before selecting its Serial Monitor tab.
                if not getattr(self, "monitors_pane_visible", True):
                    self._toggle_monitors_pane()

                tab_index = self._serial_monitor_tab_index()
                tab_state = self.bottom_notebook.tab(tab_index, "state")
                if tab_state == "disabled":
                    if attempt < 30:
                        self.root.after(50, lambda: _activate(attempt + 1))
                    return

                self.bottom_notebook.select(tab_index)
                self.bottom_notebook.update_idletasks()
                if was_paused:
                    self._append_serial(
                        f"  ▶ Serial monitoring resumed automatically after {action_name}.",
                        "dim",
                    )
            except (tk.TclError, AttributeError):
                if attempt < 30:
                    try:
                        self.root.after(50, lambda: _activate(attempt + 1))
                    except Exception:
                        pass

        self._post_ui(_activate)

    def _send_serial(self, event=None):
        """Queue terminal TX for the serial I/O worker; never block Tk on write()."""
        text = self.serial_input.get()
        if not text:
            return "break" if event else None

        with self._serial_state_lock:
            conn = self.serial_conn
            running = bool(self.serial_running)
            generation = self._serial_generation
        if not (conn and getattr(conn, "is_open", False) and running):
            self._append_serial("  ✖ Not connected — open the Serial Monitor first.", "error")
            self.serial_input.delete(0, tk.END)
            return "break" if event else None

        ending_map = {"None": "", "\\n": "\n", "\\r": "\r", "\\r\\n": "\r\n"}
        ending = ending_map.get(self.line_ending_var.get(), "\r\n")
        payload = (text + ending).encode("utf-8", errors="replace")
        try:
            self._serial_tx_queue.put_nowait((generation, payload, text))
        except queue.Full:
            self._append_serial("  ✖ Send queue is busy — try again in a moment.", "error")
        self.serial_input.delete(0, tk.END)
        return "break" if event else None

    def _set_status(self, text: str, color: str = Theme.TEXT_DIM):
        if getattr(self, "_last_status_text", None) == text and getattr(self, "_last_status_color", None) == color:
            return
        self._last_status_text = text
        self._last_status_color = color
        def _do():
            if hasattr(self, "status_label") and self.status_label.winfo_exists():
                self.status_label.configure(text=text, fg=color)
        self._post_ui(_do)

