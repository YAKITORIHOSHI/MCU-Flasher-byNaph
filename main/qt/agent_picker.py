"""Runs the assistant list: file detection, downloads and uninstalls on workers, results on the GUI thread."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from main.core import ai_agents


class AgentPicker(QObject):
    """One chooser's detection and its single download."""

    chosen = Signal(str)
    downloaded = Signal(str)
    removed = Signal(str)
    _found = Signal(int, object)
    _planned = Signal(int, str, object, str)
    _uninstalled = Signal(int, str, str)
    _connection = Signal(int, bool)
    _line = Signal(int, str)
    _finished = Signal(int, str, str)

    def __init__(self, chooser, parent=None):
        super().__init__(parent)
        self._chooser = chooser
        self._scan = 0
        self._job = 0
        self._cancel: threading.Event | None = None
        self._removal = None            # The plan being confirmed or carried out
        self._removal_prompt = None     # Its question, while it is asked
        self._closed = False
        chooser.start_requested.connect(self.chosen)
        chooser.download_requested.connect(self.download)
        chooser.uninstall_requested.connect(self.uninstall)
        self._planned.connect(self._confirm_removal, Qt.ConnectionType.QueuedConnection)
        self._uninstalled.connect(self._finish_removal, Qt.ConnectionType.QueuedConnection)
        chooser.cancel_requested.connect(self.cancel)
        self._found.connect(self._show_found, Qt.ConnectionType.QueuedConnection)
        self._connection.connect(self._show_connection, Qt.ConnectionType.QueuedConnection)
        chooser.retry_requested.connect(self.refresh)
        # While offline the list is hidden; look again now and then so it returns by itself.
        self._recheck = QTimer(self)
        self._recheck.setSingleShot(True)
        self._recheck.setInterval(8000)
        self._recheck.timeout.connect(self.refresh)
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
                return
            try:
                online = ai_agents.internet_available()
            except Exception:
                online = False
            try:
                self._connection.emit(scan, online)
            except RuntimeError:
                pass
        threading.Thread(target=look, name="MCU_AgentScan", daemon=True).start()

    def _show_found(self, scan, found) -> None:
        if scan == self._scan and not self._closed:
            self._chooser.set_installed(found)

    def _show_connection(self, scan, online) -> None:
        if scan != self._scan or self._closed:
            return
        # A download already under way keeps its list and reports its own failure.
        self._chooser.set_online(online or self.downloading)
        if not online:
            self._recheck.start()

    def download(self, agent_id: str) -> None:
        if self._closed or self._cancel is not None or self._removal is not None:
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
            self._chooser.show_message(f"{name} is downloaded. Click it to start.")
            self.downloaded.emit(agent_id)
        self.refresh()

    # ── Uninstall ────────────────────────────────────────────────────────────

    @property
    def removing(self) -> bool:
        return self._removal is not None

    def uninstall(self, agent_id: str) -> None:
        """Find what uninstalling would remove, ask, then remove it; nothing is ever cancelled half-way."""
        if self._closed or self._cancel is not None or self._removal is not None or self._chooser.busy:
            return
        self._job += 1
        job = self._job
        self._removal = agent_id
        name = ai_agents.agent(agent_id).name
        self._chooser.show_progress(agent_id, f"Looking for {name}'s files…", cancellable=False)

        def look():
            from src.modules import agent_removal
            plan, error = None, ""
            try:
                plan = agent_removal.plan(agent_id)
                busy = agent_removal.running(plan) if plan.paths else []
                if busy:
                    error = f"{name} is running ({', '.join(busy[:3])}). Close it, then uninstall it again."
            except agent_removal.RemovalError as exc:
                error = str(exc)
            except Exception as exc:
                error = f"{name}'s files could not be checked: {exc}"
            try:
                self._planned.emit(job, agent_id, plan, error)
            except RuntimeError:
                pass        # The window closed meanwhile; nothing was removed.
        try:
            threading.Thread(target=look, name="MCU_AgentRemovalPlan", daemon=True).start()
        except RuntimeError as exc:
            self._planned.emit(job, agent_id, None, str(exc))

    def _confirm_removal(self, job, agent_id, plan, error) -> None:
        if job != self._job or self._closed:
            return
        if error or plan is None or not plan.paths:
            # Not here, the built-in one, running, or installed in a way left to its own tools.
            self._removal = None
            self._chooser.show_message(error or (plan.manual if plan is not None else ""), error=bool(error))
            return
        if not self._ask_removal(plan):
            self._removal = None
            self._chooser.show_message("")
            return
        if job != self._job or self._closed:
            self._removal = None
            return
        self._removal = plan
        self._chooser.show_progress(agent_id, f"Uninstalling {plan.name}…", cancellable=False)

        def work():
            from src.modules import agent_removal
            error = ""
            try:
                agent_removal.remove(plan)
            except agent_removal.RemovalError as exc:
                error = str(exc)
            except Exception as exc:
                error = f"{plan.name} could not be uninstalled: {exc}"
            try:
                self._uninstalled.emit(job, agent_id, error)
            except RuntimeError:
                pass
        # Not a daemon: quitting waits for the removal to finish instead of cutting it short.
        threading.Thread(target=work, name="MCU_AgentRemoval", daemon=False).start()

    def _ask_removal(self, plan) -> bool:
        """The question before anything is removed: what goes, how much, and what stays."""
        from PySide6.QtWidgets import QMessageBox
        from src.modules.agent_removal import describe
        box = QMessageBox(self._chooser.window())
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(f"Uninstall {plan.name}")
        box.setText(f"Uninstall {plan.name} from this computer?")
        box.setInformativeText(describe(plan))
        remove = box.addButton("Uninstall", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        self._removal_prompt = box
        try:
            box.exec()
        finally:
            self._removal_prompt = None
        return box.clickedButton() is remove

    def _finish_removal(self, job, agent_id, error) -> None:
        plan, self._removal = self._removal, None
        if job != self._job or self._closed:
            return
        name = ai_agents.agent(agent_id).name
        if error:
            self._chooser.show_message(error, error=True)
        else:
            self._chooser.row(agent_id).set_installed(False)
            kept = "" if getattr(plan, "everything", False) else " Its settings and sign-ins are kept."
            self._chooser.show_message(f"{name} is uninstalled.{kept}")
            self.removed.emit(agent_id)
        self.refresh()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self._chooser.show_progress(self._chooser.busy or self._chooser.selected, "Cancelling…")

    def close(self) -> None:
        """The owner is closing: end the download with it."""
        self._closed = True
        self._recheck.stop()
        if self._cancel is not None:
            self._cancel.set()
