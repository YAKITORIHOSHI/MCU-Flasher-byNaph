"""Owned temporary sketch roots and source-only permanent publication.

The ownership record lives beside the project, outside its build cache and
source set. Temporary projects remain available until their workspace releases
them; publishing a saved copy never removes the original recovery material.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from src.modules.platform_runtime import app_cache_dir

SOURCE_EXTENSIONS = frozenset({".ino", ".cpp", ".c", ".h", ".hpp", ".txt"})
_MARKER = ".ghost-sketch.json"
_PROJECT_NAME = "GhostSketch"
_MARKER_BYTES = json.dumps({"schema": 1, "project": _PROJECT_NAME}, sort_keys=True).encode("utf-8")
# Saving works like the Arduino IDE: the user names the sketch, the folder is created for it,
# and the .ino holding setup() and loop() takes the folder's name.
DEFAULT_SKETCH_NAME = "MySketch"
_MAX_NUMBERED_FOLDERS = 10_000
_SETUP = re.compile(r"\bvoid\s+setup\s*\(", re.MULTILINE)
_LOOP = re.compile(r"\bvoid\s+loop\s*\(", re.MULTILINE)
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.IGNORECASE)


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


def promote_ghost_project(project_path: str | Path, destination: str | Path,
                          *, renames: dict[str, str] | None = None) -> Path:
    """Publish verified root source bytes to an absent or empty destination.

    All staging happens beside the destination. Existing destination content is
    never merged or overwritten, and the original Ghost project stays intact.
    ``renames`` gives a source its saved name (the main sketch takes the folder's).
    """
    renames = dict(renames or {})
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
    saved_names = [renames.get(name, name) for name in snapshot]
    if (any(name not in snapshot for name in renames)
            or len({name.casefold() for name in saved_names}) != len(saved_names)
            or any(Path(name).name != name or Path(name).suffix.lower() not in SOURCE_EXTENSIONS
                   for name in saved_names)):
        raise ValueError("The saved source names would collide. Choose another sketch name.")
    stage = Path(tempfile.mkdtemp(prefix=".mcu-ghost-save-", dir=target.parent))
    try:
        for name, (_identity, content) in snapshot.items():
            saved = renames.get(name, name)
            shutil.copyfile(source / name, stage / saved)
            if (stage / saved).read_bytes() != content:
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


def sketch_name_problem(name: str) -> str:
    """Why a sketch name cannot name both a folder and its main .ino, or "" when it can."""
    clean = str(name or "")
    if not clean.strip():
        return "Enter a sketch name."
    if clean != clean.strip():
        return "A sketch name cannot start or end with a space."
    if clean.endswith(".") or clean.startswith("."):
        return "A sketch name cannot start or end with a dot."
    if any(character in '<>:"/\\|?*' or ord(character) < 32 for character in clean):
        return 'A sketch name cannot contain < > : " / \\ | ? * or control characters.'
    if _WINDOWS_RESERVED.match(clean.split(" ")[0]):
        return "That name is reserved by Windows. Choose another sketch name."
    if len(clean) > 120:
        return "Use a sketch name of at most 120 characters."
    return ""


def main_sketch_source(sources: dict[str, bytes]) -> str | None:
    """The root .ino defining setup() and loop(); the Arduino IDE names the folder after it."""
    inos = sorted(name for name in sources if name.lower().endswith(".ino"))
    def defines(name, pattern):
        return bool(pattern.search(sources[name].decode("utf-8", "replace")))
    for wanted in ((_SETUP, _LOOP), (_SETUP,), (_LOOP,)):
        for name in inos:
            if all(defines(name, pattern) for pattern in wanted):
                return name
    return inos[0] if inos else None


def plan_ghost_save(project_path: str | Path, parent: str | Path,
                    name: str = DEFAULT_SKETCH_NAME) -> tuple[Path, dict[str, str]]:
    """Choose ``parent/name``, or ``name (1)``, ``(2)``… when taken, and the main sketch's new name.

    Nothing is asked of the user: an existing folder, file or link with a candidate
    name is skipped, as is a candidate whose ``.ino`` would collide with another source.
    """
    problem = sketch_name_problem(name)
    if problem:
        raise ValueError(problem)
    folder = Path(parent).expanduser().absolute()
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("Choose an existing folder to save the sketch in.")
    sources = {path.name: path.read_bytes() for path in Path(project_path).iterdir()
               if path.suffix.lower() in SOURCE_EXTENSIONS and path.is_file() and not path.is_symlink()}
    main = main_sketch_source(sources)
    others = {source.casefold() for source in sources if source != main}
    for number in range(_MAX_NUMBERED_FOLDERS + 1):
        candidate = name if number == 0 else f"{name} ({number})"
        if os.path.lexists(folder / candidate):
            continue
        if main and f"{candidate}.ino".casefold() in others:
            continue
        renames = {main: f"{candidate}.ino"} if main and main != f"{candidate}.ino" else {}
        return folder / candidate, renames
    raise FileExistsError(f"Too many sketches named '{name}' already exist in this folder.")


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
