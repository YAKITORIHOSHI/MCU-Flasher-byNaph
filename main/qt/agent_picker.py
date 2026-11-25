"""Runs the assistant list: file detection and downloads on workers, results on the GUI thread."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, Signal

from main.core import ai_agents


class AgentPicker(QObject):
    """One chooser's detection and its single download."""

    chosen = Signal(str)
    downloaded = Signal(str)
    _found = Signal(int, object)
    _line = Signal(int, str)
    _finished = Signal(int, str, str)

    def __init__(self, chooser, parent=None):
        super().__init__(parent)
        self._chooser = chooser
        self._scan = 0
        self._job = 0
        self._cancel: threading.Event | None = None
        self._closed = False
        chooser.start_requested.connect(self.chosen)
        chooser.download_requested.connect(self.download)
        chooser.cancel_requested.connect(self.cancel)
        self._found.connect(self._show_found, Qt.ConnectionType.QueuedConnection)
        self._line.connect(self._show_line, Qt.ConnectionType.QueuedConnection)
        self._finished.connect(self._finish, Qt.ConnectionType.QueuedConnection)

    @property
    def downloading(self) -> bool:
        return self._cancel is not None

    def refresh(self) -> None:
        """Look again for installed agents; files only, so nothing is started."""
        if self._closed:
            return
        self._scan += 1
        scan = self._scan

        def look():
            try:
                found = ai_agents.installed_agents()
            except Exception:
                found = {}
            try:
                self._found.emit(scan, found)
            except RuntimeError:
                pass
        threading.Thread(target=look, name="MCU_AgentScan", daemon=True).start()

    def _show_found(self, scan, found) -> None:
        if scan == self._scan and not self._closed:
            self._chooser.set_installed(found)

    def download(self, agent_id: str) -> None:
        if self._closed or self._cancel is not None:
            return
        self._job += 1
        job, stopped = self._job, threading.Event()
        self._cancel = stopped
        name = ai_agents.agent(agent_id).name
        self._chooser.show_progress(agent_id, f"Preparing {name}…")

        def work():
            from src.modules import agent_setup
            sent = [0.0]

            def line(text):
                # A fast installer prints faster than anyone reads; show the newest line.
                now = time.monotonic()
                if now - sent[0] >= .12 and not stopped.is_set():
                    sent[0] = now
                    self._line.emit(job, text)
            error = ""
            try:
                agent_setup.install(agent_id, on_line=line, cancelled=stopped.is_set)
            except agent_setup.AgentSetupError as exc:
                error = str(exc)
            except Exception as exc:
                error = f"{name} could not be downloaded: {exc}"
            try:
                self._finished.emit(job, agent_id, error)
            except RuntimeError:
                pass        # The window closed while the installer was being ended.
        try:
            threading.Thread(target=work, name="MCU_AgentDownload", daemon=True).start()
        except RuntimeError as exc:
            self._finish(job, agent_id, str(exc))

    def _show_line(self, job, text) -> None:
        if job == self._job and self._cancel is not None and not self._cancel.is_set():
            self._chooser.show_progress(self._chooser.busy or self._chooser.selected, text)

    def _finish(self, job, agent_id, error) -> None:
        if job != self._job or self._closed:
            return
        self._cancel = None
        name = ai_agents.agent(agent_id).name
        if error:
            plan = ai_agents.install_plan(agent_id)
            manual = f" Its maker documents: {plan.command}" if plan and "cancelled" not in error else ""
            self._chooser.show_message(error + manual, error="cancelled" not in error)
        else:
            self._chooser.row(agent_id).set_installed(True)
            self._chooser.select(agent_id)
            self._chooser.show_message(f"{name} is downloaded. Start it when you are ready.")
            self.downloaded.emit(agent_id)
        self.refresh()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self._chooser.show_progress(self._chooser.busy or self._chooser.selected, "Cancelling…")

    def close(self) -> None:
        """The owner is closing: end the download with it."""
        self._closed = True
        if self._cancel is not None:
            self._cancel.set()
