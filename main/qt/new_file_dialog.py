"""Source-tab file actions: a new file from the + chip, and rename from a tab's menu.

Both ask the backend, which validates names, refuses changes during actions and
records them as the user's own edits. A deleted file is moved to the project's
hidden ``bak`` folder by the backend; nothing here removes data.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QVBoxLayout, QWidget)

from main.qt.glass import GlassDialog
from main.qt.responsive import fit_dialog

ALLOWED_EXTENSIONS = (".h", ".cpp", ".ino", ".hpp", ".c", ".txt")
_INVALID_NAME_CHARACTERS = set('<>:"/\\|?*')


def split_requested_name(raw: str, extension: str) -> str:
    """The typed name keeps an extension it already has; otherwise the chosen one is added."""
    name = raw.strip()
    if name.lower().endswith(ALLOWED_EXTENSIONS):
        return name
    return name + extension


def name_problem(name: str) -> str:
    stem = Path(name).stem
    if not name or not stem or stem.startswith("."):
        return "Enter a file name."
    if any(character in _INVALID_NAME_CHARACTERS or ord(character) < 32 for character in name):
        return "A file name cannot contain < > : \" / \\ | ? * or control characters."
    if name.endswith((" ", ".")):
        return "A file name cannot end with a space or a dot."
    return ""


class NewFileDialog(GlassDialog):
    """Name and extension for a new root sketch file. Extension choice is always explicit."""

    def __init__(self, backend, parent=None, *, default_extension: str = ".h"):
        super().__init__(parent)
        self._backend = backend
        self.created_path = ""
        self.setObjectName("new-file-dialog")
        self.setWindowTitle("New file")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title = QLabel("New file", self)
        title.setProperty("role", "title")
        layout.addWidget(title)
        caption = QLabel("Choose a name and an extension. The file is created in the sketch folder.", self)
        caption.setWordWrap(True)
        layout.addWidget(caption)
        row = QWidget(self)
        fields = QHBoxLayout(row)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(8)
        self.name_edit = QLineEdit(row)
        self.name_edit.setPlaceholderText("File name, e.g. sensors")
        self.name_edit.setAccessibleName("File name")
        self.name_edit.textChanged.connect(self._sync_extension_from_name)
        fields.addWidget(self.name_edit, 1)
        self.extension_combo = QComboBox(row)
        self.extension_combo.setAccessibleName("File extension")
        self.extension_combo.addItems(list(ALLOWED_EXTENSIONS))
        if default_extension in ALLOWED_EXTENSIONS:
            self.extension_combo.setCurrentText(default_extension)
        fields.addWidget(self.extension_combo)
        layout.addWidget(row)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setObjectName("new-file-status")
        self.status.hide()
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(self)
        self.create_button = buttons.addButton("Create", QDialogButtonBox.ButtonRole.AcceptRole)
        self.create_button.setProperty("role", "primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        fit_dialog(self, (440, 220), (300, 180))
        self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _sync_extension_from_name(self, text: str) -> None:
        # A typed extension and the selector never disagree about what will be created.
        typed = text.strip().lower()
        for extension in ALLOWED_EXTENSIONS:
            if typed.endswith(extension):
                self.extension_combo.setCurrentText(extension)
                self.extension_combo.setEnabled(False)
                return
        self.extension_combo.setEnabled(True)

    def requested_name(self) -> str:
        return split_requested_name(self.name_edit.text(), self.extension_combo.currentText())

    def _show_problem(self, message: str) -> None:
        from main.core.log_colors import glass_inks
        from main.core.theme import Theme
        self.status.setStyleSheet(f"color: {glass_inks(Theme.active_theme)['error']};")
        self.status.setText(message)
        self.status.show()

    def _create(self) -> None:
        name = self.requested_name()
        problem = name_problem(name)
        if problem:
            self._show_problem(problem)
            self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        result = self._backend.add_project_file(name, activate=True) if self._backend else {
            "success": False, "error": "No project is open."}
        if not result.get("success"):
            self._show_problem(str(result.get("error") or "The file could not be created."))
            return
        self.created_path = str(result.get("path", ""))
        self.accept()
