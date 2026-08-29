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

class CompatDevicesMixin(_Base):
    """Mixin providing CompatDevicesMixin capabilities for MCUUploadGUI."""
    def _get_compat_analysis(self):
        """Return (boards, reasons) for the current sketch — served from the
        cached background analysis when the sources are unchanged.  NEVER runs
        the full static analysis on the UI thread: when the cache is missing
        or stale it schedules a thread-pool refresh and returns an empty
        "not known yet" result so Compile/Upload can proceed instantly.  The
        thread-pool result fills the tab + cache, and the next pre-check then
        has the fresh answer at zero UI cost."""
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

    def _serial_monitor_tab_index(self) -> int:
        """Index of the '📡 Serial Monitor' tab in the bottom notebook.

        Resolves the position from the tab's own frame widget (authoritative,
        survives tab reordering), with a text-match fallback.  The pre-cached
        value is invalidated whenever the tab layout is reordered."""
        cached = getattr(self, "_serial_monitor_tab_index_cache", None)
        if cached is not None:
            return cached
        try:
            frame = getattr(self, "_serial_monitor_frame", None)
            if frame is not None and self.bottom_notebook.winfo_exists():
                idx = self.bottom_notebook.index(frame)
                self._serial_monitor_tab_index_cache = idx
                return idx
        except Exception:
            pass
        try:
            for i, tab_id in enumerate(self.bottom_notebook.tabs()):
                if "Serial Monitor" in self.bottom_notebook.tab(tab_id, "text"):
                    self._serial_monitor_tab_index_cache = i
                    return i
        except Exception:
            pass
        return 1

    def _compatible_devices_tab_index(self) -> int:
        """Resolve the Compatible Devices tab index after dynamic tab reordering."""
        cached = getattr(self, "_compatible_devices_tab_index_cache", None)
        if cached is not None:
            return cached
        try:
            frame = getattr(self, "_compat_frame", None)
            if frame is not None and self.bottom_notebook.winfo_exists():
                idx = self.bottom_notebook.index(frame)
                self._compatible_devices_tab_index_cache = idx
                return idx
        except Exception:
            pass
        try:
            for i, tab_id in enumerate(self.bottom_notebook.tabs()):
                if "Compatible Devices" in self.bottom_notebook.tab(tab_id, "text"):
                    self._compatible_devices_tab_index_cache = i
                    return i
        except Exception:
            pass
        return 2

    def _compatible_devices_is_selected(self) -> bool:
        try:
            current = self.bottom_notebook.select()
            return bool(current) and self.bottom_notebook.index(current) == self._compatible_devices_tab_index()
        except Exception:
            return False

    def _repair_compatible_devices_interaction(self):
        """Repair focus/state after native Monaco/OpenCode windows or a busy-state transition.

        A reparented WebView2 window can retain the Windows keyboard focus even
        after the user clicks back into Tk. Reassert the tab and Entry state,
        raise the Tk widgets, and release only a stale invisible Tk grab.
        """
        try:
            idx = self._compatible_devices_tab_index()
            self.bottom_notebook.tab(idx, state="normal")
        except Exception:
            pass
        try:
            entry = self.compat_search_entry
            entry.configure(state=tk.NORMAL, takefocus=True)
            for widget in (
                getattr(self, "_compat_frame", None),
                getattr(self, "_compat_header", None),
                getattr(self, "_compat_search_row", None),
                entry,
            ):
                if widget is not None and widget.winfo_exists():
                    widget.lift()
        except Exception:
            pass
        try:
            grabbed = self.root.grab_current()
            if grabbed is not None and (
                not grabbed.winfo_exists() or not grabbed.winfo_ismapped()
            ):
                grabbed.grab_release()
        except Exception:
            pass

    def _focus_compatible_search(self, select_all: bool = False, select_tab: bool = True):
        """Reveal Compatible Devices and reliably focus its search Entry."""
        try:
            if not getattr(self, "monitors_pane_visible", True):
                self._toggle_monitors_pane()
            idx = self._compatible_devices_tab_index()
            self.bottom_notebook.tab(idx, state="normal")
            if select_tab:
                self.bottom_notebook.select(idx)
            self._repair_compatible_devices_interaction()
            entry = self.compat_search_entry
            safe_reclaim_os_focus(entry)

            def _finish_focus():
                try:
                    entry.configure(state=tk.NORMAL)
                    entry.focus_force()
                    entry.focus_set()
                    if select_all:
                        entry.selection_range(0, tk.END)
                        entry.icursor(tk.END)
                except Exception:
                    pass

            self.root.after(25, _finish_focus)
        except Exception:
            pass
        return "break"

    def _refresh_compatible_devices(self, force: bool = False):
        """Re-analyse the sketch in the background thread pool and redraw the
        '🔧 Compatible Devices' tab.  Called ONLY from the compile-success
        path — the list is compile-driven.  Never blocks the UI, and stale
        results are discarded through a generation counter."""
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        self._compat_analysis_gen = getattr(self, "_compat_analysis_gen", 0) + 1
        gen = self._compat_analysis_gen

        if hasattr(self, "lbl_compat_status"):
            try:
                self.lbl_compat_status.config(text="Please compile to see the list of compatible devices")
            except Exception:
                pass
        self._run_bg_task(self._compat_worker, gen, on_success=self._render_compatible_devices)

    def _compat_worker(self, gen: int) -> dict:
        try:
            boards, reasons = detect_board_compatibility(self.sketch_dir_path)
            return {"gen": gen, "boards": boards, "reasons": reasons,
                    "hash": self._hash_sources()}
        except Exception as exc:
            return {"gen": gen, "error": str(exc)}

    def _compat_cache_file(self) -> Path | None:
        """Path of the per-project compatible-devices cache JSON.  Written by
        the last successful compile so the tab survives app restarts."""
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
        """Reload the compatible-devices list cached at the last successful
        compile (project cache → .mcu_gui_compat_cache.json).

        The analysis is compile-driven, so opening a project just restores
        the cached snapshot: not yet compiled → 'Please compile…', already
        compiled → 'Please recompile…' (last known list still shown)."""
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return

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
            # This is generated app state. A malformed file should be removed
            # so the next successful compile can recreate it after a copy or
            # interrupted write.
            try:
                if path:
                    path.unlink(missing_ok=True)
            except Exception:
                pass
        self._compat_cache = cached

        if not cached:
            self._compat_full_state = None
            try:
                self.compat_search_var.set("")
            except Exception:
                pass
            self._apply_compat_content(
                [
                    ("", "normal"),
                    ("  ℹ Compatible devices are detected when this project is compiled.", "dim"),
                    ("  👉 Click '⚙ Compile' (or 'Upload') to generate the list.", "dim"),
                ],
                "Please compile to see the list of compatible devices",
            )
            return

        try:
            current_hash = self._hash_sources()
        except Exception:
            current_hash = ""
        stale = cached.get("hash", "") != current_hash
        state = {
            "boards": cached["boards"],
            "reasons": cached["reasons"],
            "stale": stale,
            "updated_at": cached.get("updated_at", ""),
            "status": "Please recompile to update the list",
        }
        self._compat_full_state = state
        try:
            self._render_compat_from_state(state)
        except Exception as exc:
            # A compatibility-cache display problem is non-essential. Never
            # let it abort the whole project load or freeze the main window.
            import traceback
            try:
                log_path = SCRIPT_DIR / "logs" / "compat_cache_error.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                log_path = SCRIPT_DIR / "logs" / "compat_cache_error.log"
            self._compat_full_state = None
            self._apply_compat_content(
                [
                    ("  ⚠ The saved compatible-device list could not be displayed.", "warning"),
                    (f"  {type(exc).__name__}: {exc}", "error"),
                    ("  Recompile to regenerate the compatibility cache.", "dim"),
                ],
                "Compatibility cache ignored — recompile to refresh",
            )

    def _build_compat_content(self, boards, reasons, stale: bool = False,
                              filter_text: str | None = None,
                              updated_at: str | None = None) -> list:
        """Build the display lines (text, tag) for the Compatible Devices tab.

        ``filter_text`` narrows the board list to names containing the
        substring (case-insensitive) so the user can look up a specific
        model / family without scrolling the full list."""
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
            # This renderer is called outside `_load_compat_cache`, so it must
            # not reference that function's local `cached` variable.
            cache_state = getattr(self, "_compat_cache", None) or {}
            last = updated_at or cache_state.get("updated_at") or "-"
            _add(f"  ⚠ Sources changed since the last compile — recompile to refresh. (last: {last})", "warning")
        if needle:
            if filtered:
                _add(f"  🔍 {len(filtered)} of {len(boards)} boards match '{filter_text.strip()}'.",
                     "dim")
            else:
                _add(f"  ✖ No boards match '{filter_text.strip()}'.", "error")
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

    def _apply_compat_content(self, lines, status_text: str) -> None:
        """Write lines into the Compatible Devices tab and update its status.

        One bulk insert + one tag-add pass over line ranges (no per-line
        insert calls) so rendering 400+ board entries stays cheap on the
        UI thread. Preserve the search Entry's focus/caret while a background
        compatibility refresh finishes.
        """
        search_had_focus = False
        search_insert = None
        search_selection = None
        try:
            search_had_focus = self.root.focus_get() is self.compat_search_entry
            search_insert = self.compat_search_entry.index(tk.INSERT)
            if self.compat_search_entry.selection_present():
                search_selection = (
                    self.compat_search_entry.index(tk.SEL_FIRST),
                    self.compat_search_entry.index(tk.SEL_LAST),
                )
        except Exception:
            pass
        try:
            self.compat_text.configure(state=tk.NORMAL)
            self.compat_text.delete("1.0", tk.END)
            self.compat_text.insert("1.0", "".join(f"{text}\n" for text, _t in lines))
            offset = 0
            for text, tag in lines:
                length = len(text) + 1  # + trailing newline
                if tag and tag != "normal":
                    try:
                        self.compat_text.tag_add(
                            tag, f"1.0+{offset}c", f"1.0+{offset + length}c"
                        )
                    except Exception:
                        pass
                offset += length
            self.compat_text.configure(state=tk.DISABLED)
            self.compat_text.see("1.0")
            self.lbl_compat_status.config(text=status_text)
            if search_had_focus:
                def _restore_search_focus():
                    try:
                        self._repair_compatible_devices_interaction()
                        self.compat_search_entry.focus_force()
                        self.compat_search_entry.focus_set()
                        if search_selection:
                            self.compat_search_entry.selection_range(*search_selection)
                        if search_insert is not None:
                            self.compat_search_entry.icursor(search_insert)
                    except Exception:
                        pass
                self.root.after_idle(_restore_search_focus)
        except Exception:
            pass

    def _apply_compat_filter(self):
        """Re-render the Compatible Devices list narrowed by the search box.

        Debounced (120 ms) so rapid typing never triggers a full 400+-line
        rebuild per keystroke on the UI thread; the render runs once the
        user pauses."""
        if getattr(self, "_compat_filter_job", None):
            try:
                self.root.after_cancel(self._compat_filter_job)
            except Exception:
                pass
            self._compat_filter_job = None
        try:
            self._compat_filter_job = self.root.after(120, self._do_compat_filter_render)
        except Exception:
            self._do_compat_filter_render()

    def _do_compat_filter_render(self):
        self._compat_filter_job = None
        state = getattr(self, "_compat_full_state", None)
        if not state:
            return
        try:
            self._render_compat_from_state(state)
        except Exception:
            pass

    def _render_compat_from_state(self, state: dict) -> None:
        """Render the stored full compatible-devices snapshot, applying the
        current search-box filter to the board list."""
        filter_text = self.compat_search_var.get().strip()
        lines = self._build_compat_content(
            state["boards"], state["reasons"],
            stale=state.get("stale", False),
            filter_text=filter_text or None,
            updated_at=state.get("updated_at"),
        )
        self._apply_compat_content(lines, state.get("status", ""))

    def _render_compatible_devices(self, result: dict) -> None:
        """Draw a compile-triggered analysis result into the tab and persist
        it to the project's cache JSON."""
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        gen = result.get("gen")
        if gen is not None and gen != self._compat_analysis_gen:
            return  # stale result from an older refresh — discard

        if result.get("error"):
            self._compat_full_state = None
            try:
                self.compat_search_var.set("")
            except Exception:
                pass
            self._apply_compat_content(
                [
                    ("  ✖ Compatibility analysis failed:", "error"),
                    (f"  {result['error']}", "error"),
                ],
                "✖ analysis failed",
            )
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

