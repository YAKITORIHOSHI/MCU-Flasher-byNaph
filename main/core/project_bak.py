"""Recoverable deletion: a deleted sketch file moves into the project's hidden ``bak`` folder.

Nothing is removed. The folder is named exactly ``bak`` and hidden the way each desktop
hides a folder without renaming it: the hidden attribute on Windows, ``UF_HIDDEN`` on
macOS, and the project's ``.hidden`` list, which Linux file managers (GNOME Files, Nemo,
Caja, Dolphin, Thunar) read. A folder name already used by a file, a link or anything
that is not a plain folder is never adopted or replaced.
"""
from __future__ import annotations

import os
import stat
import sys
import uuid
from pathlib import Path

BAK_DIR_NAME = "bak"
_MAX_COPIES = 10_000


class ProjectBakError(OSError):
    """The file could not be moved into ``bak``; it is still where it was."""


def bak_directory(project_dir: Path | str) -> Path:
    return Path(project_dir) / BAK_DIR_NAME


def _numbered(name: str, number: int) -> str:
    """``Function.cpp`` → ``Function (2).cpp``; the same pattern Ghost Sketch saves use."""
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        return f"{name} ({number})"
    return f"{stem} ({number}).{suffix}"


def available_bak_name(bak: Path, name: str) -> Path:
    candidate = bak / name
    number = 1
    while os.path.lexists(candidate):
        if number > _MAX_COPIES:
            raise ProjectBakError(f"The bak folder already holds too many copies of '{name}'.")
        candidate = bak / _numbered(name, number)
        number += 1
    return candidate


def _hide_on_linux(project: Path) -> None:
    """List ``bak`` in the project's ``.hidden`` file, keeping every name already there."""
    listing = project / ".hidden"
    if listing.is_symlink() or (listing.exists() and not listing.is_file()):
        return
    try:
        existing = listing.read_text(encoding="utf-8") if listing.exists() else ""
    except (OSError, UnicodeDecodeError):
        return
    if BAK_DIR_NAME in (line.strip() for line in existing.splitlines()):
        return
    text = existing + ("" if not existing or existing.endswith("\n") else "\n") + BAK_DIR_NAME + "\n"
    temporary = project / f".hidden.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, listing)
    except OSError:
        pass  # Hiding is cosmetic; the move itself already succeeded.
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def hide_bak_directory(project: Path, bak: Path) -> None:
    if sys.platform == "win32":
        from main.core.file_utils import hide_hidden_attribute
        hide_hidden_attribute(bak)
    elif sys.platform == "darwin":
        try:
            flags = os.lstat(bak).st_flags
            if not flags & stat.UF_HIDDEN:
                os.chflags(bak, flags | stat.UF_HIDDEN, follow_symlinks=False)
        except (AttributeError, OSError):
            pass
    else:
        _hide_on_linux(project)


def ensure_bak_directory(project_dir: Path | str) -> Path:
    project = Path(project_dir)
    bak = bak_directory(project)
    if bak.is_symlink() or (os.path.lexists(bak) and not bak.is_dir()):
        raise ProjectBakError("The project's 'bak' name is used by something that is not a folder, "
                              "so deleted files have nowhere safe to go. Rename it and try again.")
    try:
        bak.mkdir(exist_ok=True)
    except OSError as error:
        raise ProjectBakError(f"Could not create the project's bak folder: {error.strerror or error}") from None
    hide_bak_directory(project, bak)
    return bak


def move_to_bak(project_dir: Path | str, target: Path | str) -> Path:
    """Move one root file into ``bak`` under a free name and return where it went."""
    project, source = Path(project_dir), Path(target)
    if source.parent != project or source.is_symlink() or not source.is_file():
        raise ProjectBakError("Only a file in the project's own folder can be moved to bak.")
    bak = ensure_bak_directory(project)
    destination = available_bak_name(bak, source.name)
    try:
        if sys.platform == "win32":
            os.rename(source, destination)  # Refuses to replace an existing name.
        else:
            # A hard link refuses an existing name too, then the original is released.
            try:
                os.link(source, destination, follow_symlinks=False)
            except (OSError, NotImplementedError):
                if os.path.lexists(destination):
                    raise
                os.rename(source, destination)
            else:
                source.unlink()
    except OSError as error:
        raise ProjectBakError(f"Could not move '{source.name}' to the bak folder: {error.strerror or error}") from None
    return destination
