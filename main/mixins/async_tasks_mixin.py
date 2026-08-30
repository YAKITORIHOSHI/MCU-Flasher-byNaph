#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import time
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

class AsyncTasksMixin(_Base):
    """Mixin providing AsyncTasksMixin capabilities for MCUUploadGUI."""
    def _post_ui(self, callback) -> None:
        """Run/queue *callback* on Tk's owning thread without cross-thread Tcl calls."""
        if not callable(callback):
            return
        # Tk callbacks that are already on the owning thread can execute directly.
        # Worker threads only enqueue plain Python callables; they never call
        # root.after(), Variable.get(), or any widget API themselves.
        if threading.get_ident() == getattr(self, "_tk_thread_id", None):
            try:
                callback()
            except Exception:
                pass
            return
        try:
            self._ui_dispatch_queue.put(callback)
        except Exception:
            pass

    def _drain_ui_dispatch_queue(self) -> None:
        """Main-thread callback pump with a small per-frame time budget.

        A count-only limit still allows 100+ expensive callbacks to monopolize
        one Tk frame.  Bound both callback count and wall-clock time so serial,
        editor, tab, and window events keep getting turns under bursty workloads.
        """
        self._ui_dispatch_after_id = None
        processed = 0
        deadline = time.perf_counter() + 0.008  # ~8 ms of worker callbacks/frame for high refresh rates
        try:
            while processed < 120 and time.perf_counter() < deadline:
                try:
                    callback = self._ui_dispatch_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback()
                except Exception:
                    pass
                processed += 1
        finally:
            try:
                if self.root and self.root.winfo_exists():
                    # If we consumed work, continue immediately on next turn; idle stays relaxed
                    self._ui_dispatch_after_id = self.root.after(
                        1 if processed else 15, self._drain_ui_dispatch_queue
                    )
            except Exception:
                self._ui_dispatch_after_id = None

    def _run_bg_task(self, task_func, *args, on_success=None, on_error=None):
        """Submit work to the central ThreadPoolExecutor.

        Worker threads never touch Tk.  Completion callbacks are posted to the
        main-thread dispatch queue, avoiding cross-thread Tcl calls that can
        intermittently stall or deadlock the GUI on Windows.
        """
        def _worker():
            try:
                result = task_func(*args)
                if on_success and callable(on_success):
                    self._post_ui(lambda result=result: on_success(result))
                return result
            except Exception as exc:
                if on_error and callable(on_error):
                    self._post_ui(lambda exc=exc: on_error(exc))
                return None

        if hasattr(self, "_bg_executor") and self._bg_executor:
            try:
                return self._bg_executor.submit(_worker)
            except Exception:
                pass
        import threading
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

