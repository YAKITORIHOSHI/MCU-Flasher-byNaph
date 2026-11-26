"""Safe points for Bootstrap's board preparation, so a forced stop heals on the next start.

Board preparation downloads and unpacks gigabytes. If Bootstrap is ended from Task Manager (or
killed any other way) part-way through, it leaves:

* its hidden board-pack child still running, holding the tool store, so the next Bootstrap
  waits on it forever;
* PlatformIO's half-installed ``_tmp_installing*`` folders and scratch files;
* now and then a package whose metadata or manifest was being written;
* a board core half unpacked.

``begin`` records a safe point before that work and ``add_child`` records each process it
starts. ``finish`` clears the record after a complete preparation. A record whose owner is no
longer running therefore means the last preparation never finished; ``heal`` then stops what
it left running and returns the store to the last safe point. Finished packages and partial
downloads stay: downloads resume where they stopped and PlatformIO fetches only what was
taken out. Nothing outside the app's own tool store and Boards folder is touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time

JOURNAL = ".mcu-board-prep.json"
RECOVERY = ".mcu-recovery"
# Board cores are unpacked under this name and renamed when complete.
STAGING_PREFIX = ".extracting-"
_STORES = (("packages", "tool"), ("platforms", "platform"), ("lib", "library"))
_MANIFESTS = {"packages": ("package.json",), "platforms": ("platform.json",),
              "lib": ("library.json", "library.properties", "module.json", "package.json")}
_SCRATCH = (".cache/tmp", ".tmp")


def _process_identity(pid: int):
    import psutil
    process = psutil.Process(int(pid))
    return process, process.create_time()


def _write(core: Path, data: dict) -> None:
    path = Path(core) / JOURNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read(core) -> dict | None:
    try:
        data = json.loads((Path(core) / JOURNAL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == 1 else None


def begin(core, stage: str = "boards") -> None:
    """Record the safe point: this process is about to change the tool store."""
    try:
        _process, created = _process_identity(os.getpid())
    except Exception:
        created = 0.0
    _write(Path(core), {"schema": 1, "pid": os.getpid(), "created": created, "stage": stage,
                        "started": time.time(), "children": []})


def add_child(core, pid: int, marker: str) -> None:
    """Remember a process this preparation started, by identity, so only it is ever stopped."""
    data = read(core)
    if data is None:
        return
    try:
        _process, created = _process_identity(pid)
    except Exception:
        return
    data["children"].append({"pid": int(pid), "created": created, "marker": str(marker)})
    _write(Path(core), data)


def finish(core) -> None:
    """The preparation completed: no safe point is needed and nothing is held aside."""
    core = Path(core)
    try:
        (core / JOURNAL).unlink(missing_ok=True)
    except OSError:
        pass
    shutil.rmtree(core / RECOVERY, ignore_errors=True)


def _same_process(entry: dict):
    """The recorded process if it is still the one recorded, else None."""
    try:
        process, created = _process_identity(entry.get("pid"))
    except Exception:
        return None
    recorded = entry.get("created") or 0
    return process if recorded and abs(created - float(recorded)) < 1.0 else None


def interrupted(core) -> bool:
    """A preparation began and its owner is gone without finishing it."""
    data = read(core)
    return data is not None and _same_process(data) is None


def stop_leftovers(core) -> list[str]:
    """Stop the processes an interrupted preparation left running. Identity-checked: a reused
    process ID or anything whose command line lacks the recorded marker is left alone."""
    import psutil
    data = read(core)
    if data is None or _same_process(data) is not None:
        return []
    stopped = []
    for entry in data.get("children", []):
        process = _same_process(entry)
        if process is None:
            continue
        try:
            if entry.get("marker") and entry["marker"] not in " ".join(process.cmdline()):
                continue
            tree = process.children(recursive=True) + [process]
        except (psutil.Error, OSError):
            continue
        for member in reversed(tree):
            try:
                member.kill()
            except (psutil.Error, OSError):
                pass
        psutil.wait_procs(tree, timeout=5)
        stopped.append(f"{entry.get('marker') or 'process'} ({entry['pid']})")
    return stopped


def _entry_readable(kind: str, expected_type: str, folder: Path) -> bool:
    """Whether PlatformIO can read this installed entry (its metadata and manifest)."""
    metadata = next((folder / relative for relative in (".git/.piopm", ".hg/.piopm", ".svn/.piopm", ".piopm")
                     if (folder / relative).exists()), None)
    manifest = next((folder / name for name in _MANIFESTS[kind] if (folder / name).is_file()), None)
    if metadata is None:
        # A library without metadata may be the user's own; it is never touched. A tool or
        # platform PlatformIO never finished has neither file; a local one has its manifest.
        return kind == "lib" or manifest is not None
    try:
        value = json.loads(metadata.read_bytes()[:524289])
        if (not isinstance(value, dict) or value.get("type") != expected_type
                or not value.get("name") or not value.get("version") or not value.get("spec")):
            return False
        if manifest is None or manifest.stat().st_size == 0:
            return False
        if manifest.suffix == ".json" and not isinstance(json.loads(manifest.read_bytes()), dict):
            return False
    except (OSError, ValueError):
        return False
    return True


def _set_aside(core: Path, path: Path, stamp: str) -> str:
    target = core / RECOVERY / stamp / path.relative_to(core)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(path, target)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
    return str(path.relative_to(core))


def clean_staging(boards_dir) -> list[str]:
    """Board cores that were still being unpacked; complete ones were already renamed."""
    removed = []
    directory = Path(boards_dir) if boards_dir else None
    if directory is None or not directory.is_dir():
        return removed
    for staging in directory.glob(STAGING_PREFIX + "*"):
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
            removed.append(staging.name[len(STAGING_PREFIX):])
    return removed


def heal(core, boards_dir=None, *, force: bool = False) -> list[str]:
    """Return the tool store to the last safe point after an unfinished preparation.

    ``force`` heals after this process's own failed attempt, whose record it still owns.
    Returns what was done, for the log; an empty list means there was nothing to heal.
    """
    core = Path(core)
    if not force and not interrupted(core):
        return []
    actions = [f"stopped {name}" for name in stop_leftovers(core)]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for kind, expected_type in _STORES:
        store = core / kind
        if not store.is_dir() or store.is_symlink():
            continue
        for folder in sorted(store.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            if folder.name.startswith("_tmp_installing"):
                shutil.rmtree(folder, ignore_errors=True)
                actions.append(f"removed unfinished install {kind}/{folder.name}")
            elif not _entry_readable(kind, expected_type, folder):
                actions.append(f"set aside damaged {_set_aside(core, folder, stamp)} to download again")
    for relative in _SCRATCH:
        scratch = core / relative
        if scratch.is_dir() and not scratch.is_symlink() and any(scratch.iterdir()):
            shutil.rmtree(scratch, ignore_errors=True)
            actions.append(f"cleared {relative}")
    actions += [f"removed half-unpacked board core {name}" for name in clean_staging(boards_dir)]
    # Readiness must be proven again by a full preparation, not inherited from before the stop.
    from src.modules.offline_bootstrap import CATALOG_MARKER, MARKER
    for marker in (MARKER, CATALOG_MARKER):
        try:
            if (core / marker).exists():
                (core / marker).unlink()
                actions.append(f"cleared {marker}")
        except OSError:
            pass
    data = read(core)
    if data is not None:
        data["children"] = []
        if not force:
            data["pid"], data["created"] = 0, 0
        _write(core, data)
    return actions or ["nothing was left half-done"]
