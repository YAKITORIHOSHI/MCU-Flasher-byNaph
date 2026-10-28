"""Owned temporary sketch roots and source-only permanent publication.

The ownership record lives beside the project, outside its build cache and
source set. Temporary projects remain available until their workspace releases
them; publishing a saved copy never removes the original recovery material.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from src.modules.platform_runtime import app_cache_dir

SOURCE_EXTENSIONS = frozenset({".ino", ".cpp", ".c", ".h", ".hpp", ".txt"})
_MARKER = ".ghost-sketch.json"
_PROJECT_NAME = "GhostSketch"
_MARKER_BYTES = json.dumps({"schema": 1, "project": _PROJECT_NAME}, sort_keys=True).encode("utf-8")


def _ghost_store_root() -> Path:
    return app_cache_dir() / "ghost-sketches"


def create_ghost_project_path() -> Path:
    """Create an independent local project without asking for a save location."""
    store = _ghost_store_root().resolve()
    store.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="ghost-", dir=store))
    project = session / _PROJECT_NAME
    project.mkdir()
    (project / "GhostSketch.ino").write_text(
        "void setup() {\n\n}\n\nvoid loop() {\n\n}\n", encoding="utf-8", newline="")
    (session / _MARKER).write_bytes(_MARKER_BYTES)
    return project


def is_ghost_project(project_path: str | Path | None) -> bool:
    """Recognize only this application's marked, directly owned project roots."""
    if not project_path:
        return False
    try:
        project = Path(project_path).absolute()
        session = project.parent
        if (project.name != _PROJECT_NAME or not session.name.startswith("ghost-")
                or session.parent.resolve() != _ghost_store_root().resolve()
                or session.is_symlink() or project.is_symlink() or not project.is_dir()):
            return False
        marker = session / _MARKER
        if marker.is_symlink() or not marker.is_file():
            return False
        with marker.open("rb") as stream:
            return stream.read(512) == _MARKER_BYTES
    except (OSError, ValueError, RuntimeError):
        return False


def _source_snapshot(project: Path) -> dict[str, tuple[tuple[int, int, int, int], bytes]]:
    sources = {}
    for path in project.iterdir():
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Use a regular root source file before saving: {path.name}")
        identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        content = path.read_bytes()
        after = path.lstat()
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise OSError("Sketch sources changed while preparing the save. Try again.")
        sources[path.name] = (identity, content)
    if not sources:
        raise ValueError("Add a supported sketch source before saving Ghost Sketch.")
    return sources


def promote_ghost_project(project_path: str | Path, destination: str | Path) -> Path:
    """Publish verified root source bytes to an absent or empty destination.

    All staging happens beside the destination. Existing destination content is
    never merged or overwritten, and the original Ghost project stays intact.
    """
    if not str(destination).strip():
        raise ValueError("Choose a folder to save Ghost Sketch.")
    source = Path(project_path).absolute()
    if not is_ghost_project(source):
        raise ValueError("This project is not an owned Ghost Sketch.")
    raw_target = Path(destination).expanduser().absolute()
    if raw_target.is_symlink():
        raise ValueError("Choose a regular folder for the saved project.")
    target = raw_target.resolve()
    store = _ghost_store_root().resolve()
    if target == store or target.is_relative_to(store):
        raise ValueError("Choose a permanent folder outside temporary Ghost Sketch storage.")
    if not target.parent.is_dir():
        raise ValueError("The selected save folder's parent is unavailable.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FileExistsError("Choose a new or empty project folder; existing files will not be overwritten.")
    destination_identity = target.stat() if target.exists() else None
    source_identity = source.stat()
    snapshot = _source_snapshot(source)
    stage = Path(tempfile.mkdtemp(prefix=".mcu-ghost-save-", dir=target.parent))
    try:
        for name, (_identity, content) in snapshot.items():
            shutil.copyfile(source / name, stage / name)
            if (stage / name).read_bytes() != content:
                raise OSError("Sketch sources changed while copying. Try again.")
        current_source = source.stat()
        if (not is_ghost_project(source)
                or (current_source.st_dev, current_source.st_ino) != (source_identity.st_dev, source_identity.st_ino)
                or _source_snapshot(source) != snapshot):
            raise OSError("Sketch sources changed while saving. Try again.")
        if destination_identity is not None:
            current = target.stat()
            if ((current.st_dev, current.st_ino) != (destination_identity.st_dev, destination_identity.st_ino)
                    or target.is_symlink() or any(target.iterdir())):
                raise FileExistsError("The selected folder changed while saving. Choose an empty folder.")
            # Windows cannot rename over an empty directory. rmdir refuses any
            # newly added contents; publication below also refuses a new owner.
            if os.name == "nt":
                target.rmdir()
        elif target.exists() or target.is_symlink():
            raise FileExistsError("The selected project folder already exists.")
        os.rename(stage, target)
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def discard_ghost_project_path(project_path: str | Path) -> None:
    """Release one marked temporary session after its workspace accepts release."""
    project = Path(project_path).absolute()
    if not is_ghost_project(project):
        raise ValueError("Only an owned Ghost Sketch can be discarded.")
    session = project.parent
    identity = session.stat()
    if not is_ghost_project(project) or session.stat() != identity:
        raise OSError("Ghost Sketch ownership changed; temporary files were retained.")
    shutil.rmtree(session)
