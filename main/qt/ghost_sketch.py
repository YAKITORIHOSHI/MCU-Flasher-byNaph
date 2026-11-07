"""Explicit permanent-save UI for temporary Ghost Sketch projects."""
from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (QApplication, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from main.core.ghost_sketch import DEFAULT_SKETCH_NAME, sketch_name_problem
from main.qt.glass import GlassDialog


class GhostSaveDialog(GlassDialog):
    """Name the sketch and choose where it goes; the sketch's own folder is created for it.

    Like the Arduino IDE, the folder takes the sketch's name and so does the .ino holding
    setup() and loop(). A taken name becomes ``MySketch (1)``, ``(2)``… without a prompt.
    Only the name is checked here: storage is read by the save worker, never the GUI thread.
    """

    def __init__(self, parent=None, default_parent: str = "", name: str = DEFAULT_SKETCH_NAME):
        super().__init__(parent)
        self.setObjectName("ghost-save-dialog")
        self.setWindowTitle("Save sketch")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title = QLabel("Save sketch", self)
        title.setProperty("role", "title")
        layout.addWidget(title)
        form = QFormLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit(name, self)
        self.name_edit.setAccessibleName("Sketch name")
        self.name_edit.selectAll()
        form.addRow("Sketch name", self.name_edit)
        location = QWidget(self)
        row = QHBoxLayout(location)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.location_edit = QLineEdit(default_parent, location)
        self.location_edit.setAccessibleName("Save in folder")
        row.addWidget(self.location_edit, 1)
        browse = QPushButton("Browse…", location)
        browse.setAutoDefault(False)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        form.addRow("Save in", location)
        layout.addLayout(form)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        self.note.setObjectName("ghost-save-note")
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setProperty("role", "primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name_edit.textChanged.connect(self._describe)
        self.location_edit.textChanged.connect(self._describe)
        from main.qt.responsive import fit_dialog
        fit_dialog(self, (520, 250), (320, 200))
        self._describe()
        self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def sketch_name(self) -> str:
        return self.name_edit.text()

    def save_parent(self) -> str:
        return self.location_edit.text().strip()

    def problem(self) -> str:
        if not self.save_parent():
            return "Choose a folder to save the sketch in."
        return sketch_name_problem(self.sketch_name())

    def _describe(self, *_args) -> None:
        problem = self.problem()
        name = self.sketch_name()
        self.note.setText(problem or (f"Creates the folder '{name}' with {name}.ino as its main sketch. "
                                      f"If that name is taken there, '{name} (1)', '{name} (2)'… is used."))
        self.save_button.setEnabled(not problem)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Save sketch in", self.save_parent())
        if chosen:
            self.location_edit.setText(chosen)

    def _accept_if_valid(self) -> None:
        if not self.problem():
            self.accept()


def ask_ghost_save_location(parent, default_parent: str) -> tuple[str, str] | None:
    """(folder to save in, sketch name), or None when the user cancels."""
    dialog = GhostSaveDialog(parent, default_parent)
    try:
        if dialog.exec() != GhostSaveDialog.DialogCode.Accepted:
            return None
        return dialog.save_parent(), dialog.sketch_name()
    finally:
        dialog.deleteLater()


class GhostSketchController(QObject):
    """Keep temporary build flushes separate from the user's Save intent."""

    _saved = Signal(dict)

    def __init__(self, workspace):
        super().__init__(workspace)
        self.workspace = workspace
        self.is_saving = False
        self._callback = self._failure_callback = None
        self._locked_windows = []
        self._editor_enabled = None
        self._promotion_recovery = None
        self._save_warning = ""
        self._sketch_name = None
        self._saved.connect(self._finish_copy)

    @property
    def temporary(self) -> bool:
        return getattr(self.workspace._backend, "is_ghost_sketch", False) is True

    @property
    def needs_recovery(self) -> bool:
        return self._promotion_recovery is not None

    def confirm_leave(self, callback: Callable[[bool], None]) -> None:
        """A successful build does not make a temporary project permanent."""
        if self.is_saving:
            callback(False)
            return
        if not self.temporary and not self.needs_recovery:
            callback(True)
            return
        parent = QApplication.activeModalWidget() or self.workspace
        message = ("The project files were saved, but the editor has not confirmed its new paths. "
                   "Save retries the editor update. Discard leaves the saved files on disk and closes this workspace."
                   if self.needs_recovery else
                   "This Ghost Sketch has no permanent location. Save the project before leaving?")
        choice = QMessageBox.question(
            parent, "Save Ghost Sketch?",
            message,
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            self.request_save(callback=lambda: callback(True), failure_callback=lambda: callback(False))
        else:
            callback(choice == QMessageBox.StandardButton.Discard)

    def request_save(self, callback=None, failure_callback=None) -> None:
        if self.is_saving:
            return
        if self.needs_recovery:
            self._callback, self._failure_callback = callback, failure_callback
            self.is_saving = True
            self.workspace._ghost_save_in_progress = True
            self._lock(True)
            self._apply_promotion(self._promotion_recovery)
            return
        if not self.temporary or self.workspace._is_busy():
            if failure_callback:
                failure_callback()
            return
        parent = QApplication.activeModalWidget() or self.workspace
        chosen = ask_ghost_save_location(parent, self.workspace._backend.get_default_project_parent())
        if not chosen:
            if failure_callback:
                failure_callback()
            return
        destination, sketch_name = chosen
        # Recheck after the native modal dialog has processed workspace events.
        if not self.temporary or self.workspace._is_busy():
            if failure_callback:
                failure_callback()
            return
        self._callback, self._failure_callback = callback, failure_callback
        self._save_warning = ""
        self.is_saving = True
        self.workspace._ghost_save_in_progress = True
        self._source_project = str(self.workspace._backend.sketch_dir_path)
        self._destination = destination
        self._sketch_name = sketch_name
        self._lock(True)
        try:
            self.workspace._editor_panel.trigger_save_all(
                callback=self._copy_project, failure_callback=self._flush_failed)
        except Exception as exc:
            self._fail(f"The editor could not save its temporary files: {exc}")

    def _lock(self, locked: bool, keep_editor_locked: bool = False) -> None:
        editor = self.workspace._editor_panel
        if locked:
            if self._editor_enabled is None:
                self._editor_enabled = editor.isEnabled()
            windows = [self.workspace, getattr(self.workspace, "_detached_window", None)]
            self._locked_windows = [(window, window.isEnabled()) for window in windows if window is not None]
            for window, _ in self._locked_windows:
                window.setEnabled(False)
            script = ("if (window.editorInstance) { if (!Object.hasOwn(window, '__mcuGhostReadOnly')) { "
                      "window.__mcuGhostReadOnly = window.editorInstance.getRawOptions().readOnly; } "
                      "window.editorInstance.updateOptions({readOnly:true}); }")
        else:
            for window, enabled in self._locked_windows:
                window.setEnabled(enabled)
            self._locked_windows = []
            if keep_editor_locked:
                editor.setEnabled(False)
                script = "window.editorInstance?.updateOptions({readOnly:true});"
            else:
                if self._editor_enabled is not None:
                    editor.setEnabled(self._editor_enabled)
                self._editor_enabled = None
                script = ("if (window.editorInstance && Object.hasOwn(window, '__mcuGhostReadOnly')) { "
                          "window.editorInstance.updateOptions({readOnly:!!window.__mcuGhostReadOnly}); "
                          "delete window.__mcuGhostReadOnly; }")
        editor._view.page().runJavaScript(script)

    def _flush_failed(self) -> None:
        self._fail("The editor could not confirm all temporary files were saved. The Ghost Sketch remains open.")

    def _copy_project(self) -> None:
        backend = self.workspace._backend
        if str(backend.sketch_dir_path) != self._source_project or not self.temporary:
            self._fail("The project changed before it could be saved.")
            return

        def copy():
            try:
                result = backend.save_ghost_project(self._destination, sketch_name=self._sketch_name)
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
            self._saved.emit(result)

        try:
            threading.Thread(target=copy, name="MCU_GhostSave", daemon=True).start()
        except RuntimeError as exc:
            self._fail(f"The project save could not start: {exc}")

    @Slot(dict)
    def _finish_copy(self, result: dict) -> None:
        if not self.is_saving:
            return
        if result.get("success") is not True:
            self._fail(result.get("error") or "The project could not be saved. The Ghost Sketch remains open.")
            return
        payload = result["project"]
        self._save_warning = str(result.get("warning") or "")

        self._apply_promotion(payload)

    def _apply_promotion(self, payload: dict) -> None:

        def applied():
            # Release only the exact temporary project after the retained models
            # have acknowledged their new source paths.
            self._release_project(payload["old_path"], preserve_recovery=True)
            self._promotion_recovery = None
            warning, self._save_warning = self._save_warning, ""
            try:
                # A taken name was numbered without asking, so say where the sketch went.
                self.workspace._backend.emit("notification", {
                    "title": "Ghost Sketch saved",
                    "message": warning or f"Saved to {payload.get('path', '')}",
                    "type": "warning" if warning else "success"})
            except Exception:
                pass  # Reporting cannot veto already-published source files.
            self._complete(True)

        def failed():
            self._promotion_recovery = payload
            self._fail(
                "The project files were saved, but the editor could not confirm their new paths. "
                "The temporary copy is retained and the editor is locked. Press Save to retry, "
                "or close the workspace to choose whether to keep it open.",
                keep_editor_locked=True,
            )

        self.workspace._editor_panel.promote_ghost_paths(
            payload, callback=applied,
            failure_callback=failed,
        )

    def _release_project(self, path: str, preserve_recovery: bool = False) -> None:
        backend = self.workspace._backend
        def release():
            try:
                result = (backend.release_saved_ghost(path) if preserve_recovery
                          else backend.discard_ghost_project(path))
                if result.get("warning"):
                    backend.emit("notification", {
                        "title": "Ghost Sketch recovery retained" if result.get("retained") else "Ghost Sketch cleanup",
                        "message": result["warning"], "type": "warning"})
                if result.get("success") is not True:
                    backend.emit("console:log", {"text": result.get("error", "Temporary sketch cleanup failed."),
                                                 "tag": "warning", "newline": True})
            except Exception as exc:
                try:
                    backend.emit("console:log", {"text": f"Temporary sketch cleanup could not finish: {exc}",
                                                 "tag": "warning", "newline": True})
                except Exception:
                    pass  # Retain the temporary project when reporting is unavailable.
        try:
            # An accepted last-window close may end the Qt loop immediately.
            # Python must finish this owned deletion before normal process exit.
            threading.Thread(target=release, name="MCU_GhostCleanup", daemon=False).start()
        except RuntimeError:
            pass  # Source copies remain recoverable if cleanup cannot start.

    def discard_after_close(self) -> None:
        path = (self._promotion_recovery["old_path"] if self.needs_recovery else
                str(self.workspace._backend.sketch_dir_path))
        self._release_project(path)

    def release_after_switch(self, old_path: str) -> None:
        """Release only a project whose replacement has already been accepted."""
        if old_path and old_path != str(self.workspace._backend.sketch_dir_path):
            self._release_project(old_path)

    def _fail(self, message: str, keep_editor_locked: bool = False) -> None:
        # Notify before a caller's failure callback may close/delete its parent.
        failed = self._failure_callback
        self._failure_callback = None
        self._complete(False, keep_editor_locked=keep_editor_locked)
        QMessageBox.warning(self.workspace, "Ghost Sketch not saved", message)
        if failed:
            failed()

    def _complete(self, success: bool, keep_editor_locked: bool = False) -> None:
        callback = self._callback if success else self._failure_callback
        self._callback = self._failure_callback = None
        self._lock(False, keep_editor_locked=keep_editor_locked)
        self.is_saving = False
        self.workspace._ghost_save_in_progress = False
        if callback:
            callback()
