#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import json
import time
import re
import shutil
import tempfile
import subprocess
import threading
import ctypes
import hashlib
from datetime import datetime
from pathlib import Path


from main.core.constants import *
from main.core.theme import *
from main.core.config import *

_PROJECT_CACHE_MIGRATION_LOCK = threading.RLock()

def get_project_build_cache_root(project_dir, create=True) -> Path:
    """Return the one hidden folder owned by MCU Flasher for a sketch.

    User source files stay at the sketch root.  PlatformIO's staged ``src``
    tree, build objects, generated configuration, editor metadata, and AI
    recovery files all live below this directory instead.
    """
    project = Path(project_dir).expanduser().resolve(strict=False)
    root = project / PROJECT_BUILD_CACHE_DIR
    if create:
        root.mkdir(parents=True, exist_ok=True)
        try:
            marker = root / PROJECT_BUILD_CACHE_MARKER
            if not marker.exists():
                marker.write_text(
                    "MCU Flasher by Naph project build cache\n",
                    encoding="utf-8",
                )
        except OSError:
            pass
        hide_generated_directory(root)
    return root


def _looks_like_mcu_generated_staged_src(project: Path, src_dir: Path) -> bool:
    """Recognize the old app-created staged ``src`` directory conservatively."""
    try:
        entries = [entry for entry in src_dir.iterdir() if entry.is_file()]
        if not entries:
            return False
        if any(entry.name.lower().endswith(".ino.cpp") for entry in entries):
            return True
        root_names = {
            entry.name.lower()
            for entry in project.iterdir()
            if entry.is_file()
        }
        return all(entry.name.lower() in root_names for entry in entries)
    except OSError:
        return False


def _migrate_legacy_project_generated_files(project_dir) -> Path:
    """Move known MCU Flasher artifacts from the old sketch root into one cache.

    Migration is deliberately one-way and best-effort.  It never deletes an
    unrecognized user file and leaves a locked legacy entry in place for the
    next run rather than risking data loss.
    """
    project = Path(project_dir).expanduser().resolve(strict=False)
    cache = get_project_build_cache_root(project, create=True)
    with _PROJECT_CACHE_MIGRATION_LOCK:
        import shutil

        def move_if_possible(source: Path, destination: Path, predicate=True):
            if not predicate or not source.exists():
                return
            try:
                target = destination
                if target.exists():
                    # A previous interrupted migration may have left both
                    # copies. Keep the old generated copy inside the hidden
                    # cache rather than leaving it visible at the project root.
                    legacy_dir = cache / "legacy"
                    legacy_dir.mkdir(parents=True, exist_ok=True)
                    target = legacy_dir / source.name
                    suffix = 2
                    while target.exists():
                        target = legacy_dir / f"{source.name}.{suffix}"
                        suffix += 1
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            except (OSError, shutil.Error):
                pass

        # These names were exclusively created by earlier MCU Flasher builds.
        for name in (
            ".mcu_gui_cache.json",
            ".mcu_flash_syntax_errors.json",
            ".mcu_gui_compat_cache.json",
            ".mcu_flash_tab_order.json",
            ".ai_edit_signal",
            ".ai_ready_signal",
            "MCU-FLASHER-SRC",
            "MCU_FLASHER_SRC",
            "compiled_builds",
            "build_artifacts",
            ".build_artifacts",
            ".pio_cache",
            ".mcu_ai_edits",
        ):
            move_if_possible(project / name, cache / name)

        old_ini = project / "platformio.ini"
        cache_ini = cache / "platformio.ini"
        try:
            ini_text = old_ini.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ini_text = ""
        generated_ini = (
            "Generated automatically by MCU Flasher by Naph" in ini_text
            or "Generated automatically by MCU Flash GUI" in ini_text
        )
        if generated_ini:
            move_if_possible(old_ini, cache_ini)
        elif old_ini.is_file() and not cache_ini.exists():
            # Preserve a user-authored configuration at the root while giving
            # the app a private working copy that it can safely rewrite.
            try:
                shutil.copy2(old_ini, cache_ini)
            except OSError:
                pass

        old_src = project / "src"
        move_if_possible(
            old_src,
            cache / "src",
            predicate=old_src.is_dir()
            and (
                generated_ini
                or _looks_like_mcu_generated_staged_src(project, old_src)
            ),
        )

        # PlatformIO's old shared object tree is app-created in this workflow.
        # Keep it intact under the cache so incremental objects survive the
        # upgrade; the board migration below can then isolate its env.
        move_if_possible(project / ".pio", cache / ".pio")

        # AGENTS/.opencodeignore were generated by this app in older releases.
        # They are moved only after their contents prove MCU Flasher ownership.
        for name in (
            ".opencodeignore", "AGENTS.md", "READ-FIRST.md", ".READ-FIRST.md",
            "SKILL.md", ".SKILL.md", "OPENCODE.md", ".ignore",
        ):
            candidate = project / name
            move_if_possible(
                candidate,
                cache / name,
                predicate=_is_mcu_generated_instruction_file(candidate),
            )

        hide_generated_directory(cache)
    return cache


def _set_windows_file_attributes(path: Path, attributes: int,
                                 attempts: int = 6) -> bool:
    """Apply Windows attributes with a short retry for scanner interference."""
    if sys.platform != "win32":
        return False
    import ctypes
    for attempt in range(max(1, int(attempts))):
        try:
            if ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes):
                return True
        except Exception:
            pass
        if attempt < attempts - 1:
            time.sleep(min(0.25, 0.04 * (attempt + 1)))
    return False

def hide_hidden_attribute(path) -> None:
    """Set the Windows hidden attribute (FILE_ATTRIBUTE_HIDDEN = 0x02) on
    app-generated files/folders so they don't clutter Windows Explorer.

    Hidden and read-only are independent attributes.  App-owned entries stay
    hidden on NTFS, FAT32, and exFAT, while READONLY is always cleared so the
    GUI and ordinary editors can still update them."""
    try:
        p = Path(path)
        if not p.exists() or sys.platform != "win32":
            return
        os.chmod(p, 0o777 if p.is_dir() else 0o666)
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
        if attrs != -1:
            desired = (attrs & ~0x01) | 0x02
            if desired != attrs:
                _set_windows_file_attributes(p, desired)
    except Exception:
        pass

def hide_generated_directory(path) -> None:
    """Hide an app-generated DIRECTORY without making its files unwritable.

    Windows supports FILE_ATTRIBUTE_HIDDEN on NTFS, FAT32, and exFAT.  A hidden
    parent keeps the whole generated tree out of Explorer without spending an
    attribute update on every compiler object inside it.  READONLY is cleared
    independently, so the directory remains editable.

    On non-Windows systems the caller uses a dot-prefixed directory name, which
    is the native hidden-directory convention.
    """
    try:
        p = Path(path)
        if sys.platform != "win32" or not p.is_dir():
            return
        os.chmod(p, 0o777)
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
        if attrs != -1:
            desired = (attrs & ~0x01) | 0x02
            if desired != attrs:
                _set_windows_file_attributes(p, desired)
    except Exception:
        pass


def unhide_hidden_attribute(path) -> None:
    """Remove Windows hidden and system attributes on Windows."""
    try:
        p = Path(path)
        if not p.exists():
            return
        if sys.platform == "win32":
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
            if attrs != -1 and ((attrs & 0x02) or (attrs & 0x04)):
                _set_windows_file_attributes(p, attrs & ~0x02 & ~0x04)
    except Exception:
        pass


def _hide_junction(path) -> None:
    """Hide only a junction entry; /L prevents applying the flag to its target."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["attrib", "/L", "+h", str(path)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass

def ensure_file_writable(path) -> None:
    """Ensure file is writable by clearing POSIX read-only flags and Windows
    file attributes to FILE_ATTRIBUTE_NORMAL (0x80) to prevent Win32 CreateFile /
    open('w') ERROR_ACCESS_DENIED ([Errno 13] Permission denied)."""
    try:
        p = Path(path)
        if not p.exists():
            return
        os.chmod(p, 0o666)
        if sys.platform == "win32":
            import ctypes
            # 0x80 = FILE_ATTRIBUTE_NORMAL
            ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x80)
    except Exception:
        pass


def is_nonfatal_pio_clean_report(text: str) -> bool:
    """True for PlatformIO's non-fatal fs.rmtree retry reports, e.g.

        [WinError 145] The directory is not empty: '...' \n Please manually remove the file `...'

    These appear when a stale build cache can't be fully deleted (a file is
    locked by another process, e.g. antivirus scanning a removable drive).
    PlatformIO retries once and then CONTINUES the build, so these lines must
    never be rendered as fatal errors by the console classifiers."""
    low = text.lower()
    return (
        ("[winerror" in low and "is not empty" in low)
        or "manually remove the file" in low
    )


_TRANSIENT_FILE_LOCK_WINERRORS = {5, 32, 33, 145}


def is_transient_file_lock_error(error: BaseException) -> bool:
    """Return whether *error* looks like a short-lived Windows file lock.

    Defender, search indexing, and file preview providers can briefly open a
    newly-created compiler object or archive.  These are safe to retry; real
    source/configuration errors are not.
    """
    if sys.platform != "win32":
        return False
    winerror = getattr(error, "winerror", None)
    if winerror in _TRANSIENT_FILE_LOCK_WINERRORS:
        return True
    text = str(error).lower()
    return any(marker in text for marker in (
        "being used by another process",
        "sharing violation",
        "cannot access the file",
        "access is denied",
        "permission denied",
        "directory is not empty",
    ))


def retry_transient_file_operation(operation, attempts: int = 8,
                                   delay: float = 0.08):
    """Run one filesystem operation, retrying only transient lock failures."""
    last_error = None
    for attempt in range(max(1, int(attempts))):
        try:
            return operation()
        except OSError as error:
            last_error = error
            if not is_transient_file_lock_error(error) or attempt >= attempts - 1:
                raise
            time.sleep(min(0.5, delay * (attempt + 1)))
    if last_error is not None:
        raise last_error


def is_transient_platformio_lock_report(output_lines) -> bool:
    """Identify a build failure likely caused by a temporary AV/indexer lock."""
    joined = "\n".join(str(line or "") for line in (output_lines or [])).lower()
    if not joined:
        return False

    # A genuine compiler diagnostic must remain authoritative.  Do not retry
    # a code error merely because a later SCons summary mentions a file.
    if re.search(r":\d+(?::\d+)?:\s+(?:fatal\s+error|error)\s*:", joined):
        return False
    if any(marker in joined for marker in (
        "undefined reference to", "multiple definition of", "cannot find -l",
        "unknown board", "missing package", "library dependency finder",
    )):
        return False

    lock_markers = (
        "winerror 5", "winerror 32", "winerror 33", "winerror 145",
        "permissionerror", "sharing violation",
        "being used by another process", "cannot access the file",
        "access is denied", "permission denied", "directory is not empty",
        "manually remove the file",
    )
    if not any(marker in joined for marker in lock_markers):
        return False

    # Restrict the automatic retry to build-system/file-operation context.
    return any(marker in joined for marker in (
        ".pio", "scons", "compiler", "compiling", "archiving",
        "deleting", "removing", "renaming", "build",
    ))


def classify_platformio_failure(output_lines) -> str:
    """Classify a failed PlatformIO run without guessing that every failure
    means the cache is corrupt.

    Normal compiler/linker diagnostics are expected incremental-build state:
    SCons keeps all successful objects and recompiles only the failed/changed
    units after the source is fixed.  Only explicit signature-database or
    build-directory corruption is classified as ``cache`` and eligible for a
    selected-board-only repair.
    """
    lines = [str(line or "") for line in (output_lines or [])]
    joined = "\n".join(lines).lower()

    cache_markers = (
        "database disk image is malformed",
        "pickle data was truncated",
        "corrupt sconsign",
        "invalid sconsign",
        "sconsign file is corrupt",
        "cannot decode sconsign",
        "no input files",
        "please manually remove the file",
        "directory is not empty",
        "winerror 145",
    )
    source_patterns = (
        # Warnings/notes can precede a genuine SCons database failure and do
        # not explain a non-zero exit by themselves.  Only actual compiler
        # errors outrank an explicit cache-corruption signature.
        r":\d+(?::\d+)?:\s+(?:fatal\s+error|error)\s*:",
        r"\bundefined reference to\b",
        r"\bmultiple definition of\b",
        r"\bduplicate symbol\b",
        r"\bundefined symbol\b",
        r"\bcannot find -l",
        r"\bwill not fit in region\b",
        r"\boverflowed by\b",
        r"\bld(?:\.exe)?:.*(?:error|failed)\b",
        r"\bcollect2(?:\.exe)?: error\b",
    )
    if any(re.search(pattern, joined, re.IGNORECASE) for pattern in source_patterns):
        return "source"
    if any(marker in joined for marker in cache_markers):
        return "cache"

    configuration_markers = (
        "unknown board",
        "unknown environment",
        "could not find the package",
        "could not find a version that satisfies",
        "missing package manifest",
        "platformio.ini",
        "library dependency finder",
        "dependency graph",
        "no such file or directory",
        "permission denied",
        "access is denied",
    )
    if any(marker in joined for marker in configuration_markers):
        return "configuration"
    return "tool"


def is_unc_or_network_path(path) -> bool:
    """Return True if *path* is a UNC path (``\\\\server\\share``) or lives on
    a mapped network drive.  Works for both raw UNC and IP-based paths."""
    s = str(path)
    if s.startswith("\\\\") or s.startswith("//"):
        return True
    # A mapped drive letter (e.g. Z:) can also point to a remote share.
    drive = os.path.splitdrive(s)[0]
    if drive and sys.platform == "win32":
        try:
            import ctypes
            _DRIVE_REMOTE = 4
            return ctypes.windll.kernel32.GetDriveTypeW(drive + os.sep) == _DRIVE_REMOTE
        except Exception:
            pass
    return False


def _unc_share_root(path) -> str:
    """Extract the UNC share root (``\\\\server\\share``) from a UNC path.

    Returns an empty string when *path* is not UNC."""
    s = str(path).replace("/", "\\")
    if not s.startswith("\\\\"):
        return ""
    parts = s.lstrip("\\").split("\\")
    if len(parts) >= 2:
        return f"\\\\{parts[0]}\\{parts[1]}"
    return ""


def _volume_cache_key_for(path) -> str:
    """Return a stable cache key representing the volume that *path* lives on.

    For regular drive-letter paths this is the drive root (``C:\\``).  For UNC
    paths it is the share root (``\\\\server\\share``).  Returns ``""`` if
    neither can be determined."""
    s = str(path)
    if s.startswith("\\\\") or s.startswith("//"):
        return _unc_share_root(s)
    drive = os.path.splitdrive(s)[0]
    return (drive + os.sep) if drive else ""


_volume_info_cache: dict = {}

def get_volume_info(path) -> tuple:
    """Return (filesystem_name, drive_type_label) for the volume containing
    `path`, e.g. ('NTFS', 'Fixed'), ('exFAT', 'Removable'), ('', '').
    UNC paths are recognised as ('Network', 'Network') unless the share's
    actual filesystem can be queried.  Results are cached per volume."""
    try:
        cache_key = _volume_cache_key_for(path)
        if not cache_key:
            return "", ""
        cached = _volume_info_cache.get(cache_key)
        if cached is not None:
            return cached
        fs_name, type_label = "", ""
        if sys.platform == "win32":
            import ctypes
            _type_names = {2: "Removable", 3: "Fixed", 4: "Network", 5: "CD/DVD", 6: "RAM"}
            # For UNC paths, GetDriveTypeW needs the share root with a trailing backslash.
            is_unc = cache_key.startswith("\\\\")
            probe_root = (cache_key.rstrip("\\") + "\\") if is_unc else cache_key
            _dt = ctypes.windll.kernel32.GetDriveTypeW(probe_root)
            type_label = _type_names.get(_dt, "Network" if is_unc else "")
            _buf = ctypes.create_unicode_buffer(64)
            if ctypes.windll.kernel32.GetVolumeInformationW(
                probe_root, None, 0, None, None, None, _buf, 64
            ):
                fs_name = _buf.value
            elif is_unc:
                fs_name = "Network"
        _volume_info_cache[cache_key] = (fs_name, type_label)
        return fs_name, type_label
    except Exception:
        return "", ""


def is_ntfs_path(path) -> bool:
    """True if the volume containing `path` is NTFS (the only volume type on
    which Windows hidden-attribute tricks are both reliable and harmless)."""
    fs_name, _ = get_volume_info(path)
    return fs_name.upper() == "NTFS"


_writability_cache: dict = {}

def is_volume_writable(path) -> bool:
    """Probe whether the volume containing `path` accepts writes.  Catches
    USB flash drives with the hardware lock switch engaged, read-only
    mounts, and volumes flagged dirty after an unsafe removal.  Result is
    cached per volume.  Handles both drive-letter and UNC paths."""
    try:
        cache_key = _volume_cache_key_for(path)
        if not cache_key or cache_key in _writability_cache:
            return _writability_cache.get(cache_key, True)
        # Probe inside the nearest existing directory along the path.  Never
        # probe the volume root itself — roots are frequently unwritable for
        # standard users (e.g. C:\) even though the volume works fine.
        probe_dir = os.path.abspath(str(path))
        if not os.path.isdir(probe_dir):
            probe_dir = os.path.dirname(probe_dir)
        # For UNC paths, volume_root is the share root.
        volume_root = cache_key
        while probe_dir and not os.path.isdir(probe_dir):
            _parent = os.path.dirname(probe_dir)
            if _parent == probe_dir or os.path.normpath(_parent) == os.path.normpath(volume_root):
                break
            probe_dir = _parent
        if (
            not probe_dir
            or not os.path.isdir(probe_dir)
            or os.path.normpath(probe_dir) == os.path.normpath(volume_root)
        ):
            return True  # no sensible place to probe — don't guess False
        import tempfile as _tf
        _probe = _tf.NamedTemporaryFile(
            prefix=".mcu_fs_probe_", suffix=".tmp", dir=probe_dir, delete=False
        )
        _probe.close()
        os.unlink(_probe.name)
        result = True
    except OSError:
        result = False
    except Exception:
        result = True
    _writability_cache[cache_key] = result
    return result


def robust_rmtree(path, max_attempts: int = 5) -> bool:
    """Delete a file or directory tree as robustly as possible across every
    filesystem a sketch may live on (NTFS, exFAT, FAT32, removable drives):

      1. clears hidden/system/read-only attributes on every entry (Windows),
      2. retries the whole removal with short backoff — transient WinError 145
         ("directory is not empty") on removable/exFAT volumes is normally
         antivirus/indexer locking that clears within a second,
      3. sweeps leftover '*.trash-*' siblings from previous failed removals,
      4. as a last resort renames the tree to a hidden '*.trash-<ts>' sibling —
         rename touches only the directory entry, so it succeeds even when
         children are locked — then removes the renamed copy best-effort.

    Returns True if `path` no longer exists afterwards."""
    import stat as _stat
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return True

    def _on_rm_error(func, p, exc_info):
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(p), 128)  # FILE_ATTRIBUTE_NORMAL
            os.chmod(p, _stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    # 3. Sweep old trash siblings from earlier failed removals.
    try:
        import glob as _glob
        for stale in _glob.glob(str(target.parent / (target.name + ".trash-*"))):
            stale_p = Path(stale)
            if stale_p.is_dir():
                import shutil as _sh
                try:
                    _sh.rmtree(stale_p, onerror=_on_rm_error)
                except Exception:
                    pass
            else:
                try:
                    stale_p.unlink()
                except Exception:
                    pass
    except Exception:
        pass

    for attempt in range(max_attempts):
        if not target.exists():
            return True
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(target), 128)
            if target.is_dir():
                import shutil as _sh
                _sh.rmtree(target, onerror=_on_rm_error)
            else:
                try:
                    target.unlink()
                except OSError:
                    os.chmod(str(target), _stat.S_IWRITE)
                    target.unlink()
            return True
        except Exception:
            time.sleep(0.2 * (attempt + 1))

    # 4. Last resort: rename the locked tree aside (rename touches only the
    # directory entry, so it succeeds even when children are still locked).
    try:
        if not target.exists():
            return True
        trash = target.with_name(target.name + f".trash-{int(time.time())}")
        if target.is_dir():
            os.rename(str(target), str(trash))
            import shutil as _sh
            try:
                _sh.rmtree(trash, onerror=_on_rm_error)
            except Exception:
                pass
        else:
            try:
                target.unlink()
            except OSError:
                os.rename(str(target), str(trash))
                trash.unlink(missing_ok=True)
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(trash), 0x02)  # keep Explorer clean
        return not target.exists()
    except Exception:
        return not target.exists()


def get_project_temp_file(project_dir, filename: str) -> Path:
    """Return an app-owned metadata file inside the project's hidden cache."""
    try:
        cache = _migrate_legacy_project_generated_files(project_dir)
        return cache / filename
    except Exception:
        return Path(tempfile.gettempdir()) / filename


AI_PROJECT_STORAGE_DIR = ".mcu_ai_edits"


def _legacy_ai_review_state_file(project_dir) -> Path:
    """Return the pre-v20 LocalAppData journal path for one-time migration."""
    project_path = Path(project_dir).resolve(strict=False)
    project_hash = hashlib.sha256(
        os.path.normcase(str(project_path)).encode("utf-8")
    ).hexdigest()[:20]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        state_dir = Path(local_app_data) / "MCU Flasher by Naph" / "ai-reviews"
    else:
        state_dir = Path.home() / ".mcu_flash_gui" / "ai-reviews"
    return state_dir / f"{project_hash}.json"


def get_ai_project_storage_root(project_dir, create=True) -> Path:
    """Return the hidden project-local root used by AI review and backups.

    The root always lives beside the sketch files, so projects remain portable
    when moved to another drive or computer.  Creation is retried because USB
    flash drives and secondary volumes can be briefly locked by antivirus or
    indexing services.  Only the DIRECTORY receives the Windows hidden bit;
    the .txt/.json files remain normal and atomically writable on NTFS, FAT32,
    and exFAT.
    """
    if not project_dir:
        raise OSError("No active sketch project is available for AI edit storage.")
    project_path = Path(project_dir).expanduser().resolve(strict=False)
    if not project_path.is_dir():
        raise OSError(f"Sketch project folder is unavailable: {project_path}")

    cache_root = (
        _migrate_legacy_project_generated_files(project_path)
        if create else get_project_build_cache_root(project_path, create=False)
    )
    root = cache_root / AI_PROJECT_STORAGE_DIR
    if not create:
        return root

    last_error = None
    for attempt in range(6):
        try:
            root.mkdir(parents=False, exist_ok=True)
            hide_generated_directory(root)
            return root
        except OSError as exc:
            last_error = exc
            time.sleep(0.12 * (attempt + 1))
    raise OSError(
        f"Could not create project-local AI edit storage at {root}: {last_error}"
    )


def get_ai_review_state_file(project_dir) -> Path:
    """Return the project-local pending-review journal and migrate v19 data."""
    root = get_ai_project_storage_root(project_dir, create=True)
    state_dir = root / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)
    hide_generated_directory(state_dir)
    target = state_dir / "pending_reviews.json"

    # Preserve any unresolved v19 review when a project is first opened in v20.
    # Both replicas are copied because the backup is the journal commit point.
    legacy = _legacy_ai_review_state_file(project_dir)
    for source, destination in (
        (legacy, target),
        (legacy.with_suffix(legacy.suffix + ".bak"), target.with_suffix(target.suffix + ".bak")),
    ):
        if destination.exists() or not source.exists():
            continue
        try:
            import shutil
            shutil.copy2(source, destination)
        except OSError:
            # Migration is best effort.  Never delete or alter the legacy copy.
            pass
    return target


def get_ai_edit_backup_root(project_dir, create=True) -> Path:
    """Return the hidden project-local root for dated AI edit sessions."""
    return get_ai_project_storage_root(project_dir, create=create)

class AIEditBackupStore:
    """RAM-first AI edit history with asynchronous plain-text backups.

    The complete current-session records stay in memory. Accept, Reject,
    Undo, and Redo therefore never read an edit backup from disk. A single
    background writer mirrors each record to the dated session folder so a
    crash still leaves human-readable recovery copies.
    """

    FORMAT_VERSION = 1

    def __init__(self, project_dir, root=None):
        self.project_dir = Path(project_dir).expanduser().resolve(strict=False)
        self.root = Path(root) if root else get_ai_edit_backup_root(self.project_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now().astimezone()
        date_name = f"{self.started_at.month}-{self.started_at.day}-{str(self.started_at.year)[-2:]}"
        self.date_dir = self.root / date_name
        self.date_dir.mkdir(parents=True, exist_ok=True)
        hide_generated_directory(self.root)
        hide_generated_directory(self.date_dir)
        self.session_dir, self.session_number = self._allocate_session_dir()

        # Full bodies for every edit made during this app session. Live
        # Undo/Redo consults these dictionaries and the EditorApi stacks only.
        self._records = {}
        self._review_to_edit = {}
        self._edit_counter = 0
        self._lock = threading.RLock()

        # Coalescing write queue: repeated status changes for edit1.txt replace
        # the pending write instead of creating a backlog or touching the UI.
        self._write_condition = threading.Condition()
        self._pending_writes = {}
        self._closing = False
        self._last_write_error = ""
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="AI-Edit-Backup-Writer",
        )
        self._writer_thread.start()

        # Old sessions are indexed in the background without loading their
        # source bodies into RAM. This keeps startup fast while avoiding a new
        # directory scan whenever recovery history is requested.
        self._archive_index = []
        self._archive_index_ready = False
        self._archive_index_thread = threading.Thread(
            target=self._build_archive_index,
            daemon=True,
            name="AI-Edit-Backup-Indexer",
        )
        self._archive_index_thread.start()

        self._schedule_write(
            self.session_dir / "session.txt",
            self._format_session_info(closed=False),
        )

    def _allocate_session_dir(self):
        # mkdir(exist_ok=False) makes session allocation safe even when two
        # MCU Flasher windows start at almost the same time.
        for number in range(1, 100000):
            candidate = self.date_dir / f"session{number}"
            try:
                candidate.mkdir(parents=False, exist_ok=False)
                hide_generated_directory(candidate)
                return candidate, number
            except FileExistsError:
                continue
        raise OSError(f"Could not allocate an AI backup session under {self.date_dir}")

    @staticmethod
    def _timestamp():
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _format_session_info(self, closed=False):
        lines = [
            "MCU Flasher by Naph - AI Edit Backup Session",
            "================================================",
            f"Format-Version: {self.FORMAT_VERSION}",
            f"Session: session{self.session_number}",
            f"Started: {self.started_at.isoformat(timespec='seconds')}",
            f"PID: {os.getpid()}",
            f"State: {'closed-cleanly' if closed else 'active'}",
            f"Edit-Count: {self._edit_counter}",
        ]
        if closed:
            lines.append(f"Closed: {self._timestamp()}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _safe_text(value):
        return str(value if value is not None else "")

    def _format_record(self, record):
        history = record.get("historyActions") or []
        history_lines = [
            f"  {item.get('timestamp', '')} | {item.get('direction', '')} | "
            f"exists={item.get('exists', '')}"
            for item in history
        ] or ["  (none)"]
        return "\n".join([
            "MCU Flasher by Naph - AI Edit Backup",
            "=======================================",
            f"Format-Version: {self.FORMAT_VERSION}",
            f"Edit-Number: {record.get('editNumber', '')}",
            f"Session: session{self.session_number}",
            f"Created: {record.get('createdAt', '')}",
            f"Updated: {record.get('updatedAt', '')}",
            f"Status: {record.get('status', 'pending')}",
            f"Decision: {record.get('decision', '')}",
            f"Review-ID: {record.get('reviewId', '')}",
            f"Decision-ID: {record.get('decisionId', '')}",
            f"Project: {record.get('project', '')}",
            f"File: {record.get('path', '')}",
            f"Before-Exists: {bool(record.get('beforeExists', True))}",
            f"After-Exists: {bool(record.get('afterExists', True))}",
            f"Applied-Exists: {record.get('appliedExists', '')}",
            f"Undo-Exists: {record.get('undoExists', '')}",
            "",
            "History:",
            *history_lines,
            "",
            "<<< BEFORE CONTENT >>>",
            self._safe_text(record.get("beforeContent", "")),
            "<<< END BEFORE CONTENT >>>",
            "",
            "<<< AFTER / AI CONTENT >>>",
            self._safe_text(record.get("afterContent", "")),
            "<<< END AFTER / AI CONTENT >>>",
            "",
            "<<< CURRENT APPLIED CONTENT >>>",
            self._safe_text(record.get("appliedContent", "")),
            "<<< END CURRENT APPLIED CONTENT >>>",
            "",
            "<<< UNDO TARGET CONTENT >>>",
            self._safe_text(record.get("undoContent", "")),
            "<<< END UNDO TARGET CONTENT >>>",
            "",
        ])

    def _schedule_write(self, target, content):
        target = str(Path(target))
        with self._write_condition:
            self._pending_writes[target] = str(content)
            self._write_condition.notify()

    @staticmethod
    def _write_text_atomic(target, content):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(
                file_descriptor, "w", encoding="utf-8", errors="strict", newline=""
            ) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _writer_loop(self):
        while True:
            with self._write_condition:
                while not self._pending_writes and not self._closing:
                    self._write_condition.wait(timeout=0.5)
                if self._closing and not self._pending_writes:
                    return
                target = next(iter(self._pending_writes))
                content = self._pending_writes.pop(target)
            try:
                self._write_text_atomic(target, content)
                self._last_write_error = ""
            except Exception as exc:
                self._last_write_error = str(exc)
                # Requeue the newest copy and back off. The GUI thread is never
                # blocked by a temporarily locked antivirus/indexer handle.
                with self._write_condition:
                    self._pending_writes[target] = content
                time.sleep(0.25)

    def _build_archive_index(self):
        try:
            current = os.path.normcase(str(self.session_dir.resolve(strict=False)))
            indexed = []
            for candidate in self.root.glob("*/*/edit*.txt"):
                try:
                    parent = os.path.normcase(str(candidate.parent.resolve(strict=False)))
                    if parent == current:
                        continue
                    stat = candidate.stat()
                    indexed.append({
                        "path": str(candidate),
                        "modified": stat.st_mtime,
                        "size": stat.st_size,
                    })
                except OSError:
                    continue
            indexed.sort(key=lambda item: item["modified"], reverse=True)
            self._archive_index = indexed
        finally:
            self._archive_index_ready = True

    def record_edit(self, payload, status="pending", decision_entry=None):
        review_id = self._safe_text(payload.get("reviewId") or payload.get("path"))
        if not review_id:
            return ""
        with self._lock:
            edit_number = self._review_to_edit.get(review_id)
            if edit_number is None:
                self._edit_counter += 1
                edit_number = self._edit_counter
                self._review_to_edit[review_id] = edit_number
                created_at = self._timestamp()
            else:
                created_at = self._records.get(edit_number, {}).get(
                    "createdAt", self._timestamp()
                )
            existing = dict(self._records.get(edit_number, {}))
            project_dir = getattr(self, "current_project", "")
            record = {
                **existing,
                "editNumber": edit_number,
                "reviewId": review_id,
                "createdAt": created_at,
                "updatedAt": self._timestamp(),
                "status": status,
                "project": self._safe_text(
                    payload.get("project") or project_dir
                ),
                "path": self._safe_text(payload.get("path")),
                "beforeExists": bool(payload.get("beforeExists", True)),
                "afterExists": bool(payload.get("afterExists", True)),
                "beforeContent": self._safe_text(payload.get("beforeContent", "")),
                "afterContent": self._safe_text(payload.get("content", "")),
                "historyActions": list(existing.get("historyActions") or []),
            }
            if decision_entry:
                record.update({
                    "decision": self._safe_text(decision_entry.get("action")),
                    "decisionId": self._safe_text(decision_entry.get("decisionId")),
                    "appliedExists": bool(decision_entry.get("appliedExists", True)),
                    "appliedContent": self._safe_text(decision_entry.get("appliedContent", "")),
                    "undoExists": bool(decision_entry.get("undoExists", True)),
                    "undoContent": self._safe_text(decision_entry.get("undoContent", "")),
                })
            self._records[edit_number] = record
            backup_path = self.session_dir / f"edit{edit_number}.txt"
            self._schedule_write(backup_path, self._format_record(record))
            self._schedule_write(
                self.session_dir / "session.txt",
                self._format_session_info(closed=False),
            )
            return str(backup_path)

    def record_history_action(self, entry, direction, exists, content):
        review_id = self._safe_text(entry.get("reviewId") or entry.get("sourceReviewId"))
        if not review_id:
            # Older decisions created before this store was initialized still
            # receive their own recovery record when first undone/redone.
            review_id = self._safe_text(entry.get("decisionId") or entry.get("path"))
        payload = {
            "reviewId": review_id,
            "project": entry.get("project", ""),
            "path": entry.get("path", ""),
            "beforeExists": entry.get("originalBeforeExists", entry.get("undoExists", True)),
            "afterExists": entry.get("originalAfterExists", entry.get("appliedExists", True)),
            "beforeContent": entry.get("originalBeforeContent", entry.get("undoContent", "")),
            "content": entry.get("originalAfterContent", entry.get("appliedContent", "")),
        }
        with self._lock:
            backup_path = self.record_edit(payload, status=f"{direction}-applied", decision_entry=entry)
            edit_number = self._review_to_edit.get(review_id)
            if edit_number is not None:
                record = self._records.get(edit_number)
                if record is not None:
                    record.setdefault("historyActions", []).append({
                        "timestamp": self._timestamp(),
                        "direction": direction,
                        "exists": bool(exists),
                    })
                    record["appliedExists"] = bool(exists)
                    record["appliedContent"] = self._safe_text(content)
                    record["updatedAt"] = self._timestamp()
                    self._schedule_write(
                        self.session_dir / f"edit{edit_number}.txt",
                        self._format_record(record),
                    )
            return backup_path

    def mark_cancelled(self, payload):
        return self.record_edit(payload, status="cancelled")

    def get_memory_state(self):
        with self._lock:
            return {
                "sessionPath": str(self.session_dir),
                "currentSessionEditsInRam": len(self._records),
                "archiveIndexReady": bool(self._archive_index_ready),
                "archivedEditCount": len(self._archive_index),
                "lastWriteError": self._last_write_error,
            }

    def shutdown(self, timeout=1.5):
        # Queue the final session marker first, then ask the writer to drain.
        self._schedule_write(
            self.session_dir / "session.txt",
            self._format_session_info(closed=True),
        )
        with self._write_condition:
            self._closing = True
            self._write_condition.notify_all()
        self._writer_thread.join(timeout=max(0.0, float(timeout)))

        # If Windows was still holding a file and the daemon worker did not
        # drain in time, make one final best-effort synchronous pass before the
        # process exits. This runs only during application shutdown.
        with self._write_condition:
            remaining = list(self._pending_writes.items())
            self._pending_writes.clear()
        for target, content in remaining:
            try:
                self._write_text_atomic(target, content)
            except Exception:
                pass

def _is_mcu_generated_instruction_file(path) -> bool:
    """True only for a legacy/current instruction file proven app-owned."""
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > 2 * 1024 * 1024:
            return False
        text = p.read_text(encoding="utf-8", errors="replace")
        if (
            "Auto-generated by MCU Flash GUI" in text
            or "Generated automatically by MCU Flash GUI" in text
        ):
            return True
        return (
            "project-sketch-scope" in text
            and "CRITICAL OPENCODE AI WORKSPACE INSTRUCTIONS" in text
            and "MCU Flash GUI" in text
        )
    except OSError:
        return False


def ensure_hidden_read_first_md(sketch_dir) -> None:
    """
    Generate hidden .opencodeignore and AGENTS.md in the project build cache.
    Instructs OpenCode CLI to ONLY read root sketch files (*.ino, *.h, *.cpp) and NOTE.txt.
    Excludes the private build cache from OpenCode file scans.
    Removes redundant duplicate instruction files and applies Windows hidden attribute so Windows Explorer stays 100% clean.
    """
    try:
        s_dir = Path(sketch_dir)
        if not s_dir.exists() or not s_dir.is_dir():
            return
        cache_dir = get_project_build_cache_root(s_dir, create=True)

        # 1. Clean up old duplicate instruction & ignore files from project root
        redundant_files = (
            "READ-FIRST.md", ".READ-FIRST.md", "SKILL.md", ".SKILL.md",
            "OPENCODE.md", ".ignore"
        )
        for r_name in redundant_files:
            rf = cache_dir / r_name
            if _is_mcu_generated_instruction_file(rf):
                try:
                    rf.unlink(missing_ok=True)
                except Exception:
                    pass

        # 2. Single native OpenCode ignore configuration (.opencodeignore)
        ignore_content = (
            "# Auto-generated by MCU Flash GUI for OpenCode AI\n"
            f"{PROJECT_BUILD_CACHE_DIR}/boards/\n"
            f"{PROJECT_BUILD_CACHE_DIR}/compiled_builds/\n"
            f"{PROJECT_BUILD_CACHE_DIR}/MCU-FLASHER-SRC/\n"
            "build_artifacts/\n"
            ".build_artifacts/\n"
            ".mcu_gui_cache.json\n"
            ".mcu_flash_syntax_errors.json\n"
            ".ai_edit_signal\n"
            ".vscode/\n"
            ".clangd/\n"
            ".cache/\n"
            f"{PROJECT_BUILD_CACHE_DIR}/{AI_PROJECT_STORAGE_DIR}/.state/\n"
        )
        # 2. Single native OpenCode ignore configuration (.opencodeignore in build cache)
        ignore_content = (
            "# Auto-generated by MCU Flash GUI for OpenCode AI\n"
            f"{PROJECT_BUILD_CACHE_DIR}/boards/\n"
            f"{PROJECT_BUILD_CACHE_DIR}/compiled_builds/\n"
            f"{PROJECT_BUILD_CACHE_DIR}/MCU-FLASHER-SRC/\n"
            "build_artifacts/\n"
            ".build_artifacts/\n"
            ".mcu_gui_cache.json\n"
            ".mcu_flash_syntax_errors.json\n"
            ".ai_edit_signal\n"
            ".vscode/\n"
            ".clangd/\n"
            ".cache/\n"
            f"{PROJECT_BUILD_CACHE_DIR}/{AI_PROJECT_STORAGE_DIR}/.state/\n"
        )
        opencode_ign = cache_dir / ".opencodeignore"
        try:
            if not opencode_ign.exists() or _is_mcu_generated_instruction_file(opencode_ign):
                ensure_file_writable(opencode_ign)
                opencode_ign.write_text(ignore_content, encoding="utf-8")
                hide_hidden_attribute(opencode_ign)
        except Exception:
            pass

        # Read live hardware state payload from project_state.json if available
        state_file = cache_dir / "project_state.json"
        state_info = {}
        if state_file.is_file():
            try:
                state_info = json.loads(state_file.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                state_info = {}

        hw = state_info.get("hardware", {})
        live_board = hw.get("board_name") if hw.get("board_selected") and hw.get("board_name") else "None (No board currently selected in GUI)"
        live_port = hw.get("port") if hw.get("mcu_connected") and hw.get("port") else "None (No microcontroller connected)"
        live_baud = hw.get("baud_rate") or 115200
        live_upload_spd = hw.get("upload_speed") or 460800
        live_summary = state_info.get("status_summary") or "No board selected in GUI and no microcontroller connected."
        live_platform = hw.get("platform") or "N/A"
        live_fqbn = hw.get("fqbn") or "N/A"

        # Enumerate root sketch files
        sketch_file_names = []
        try:
            for p in s_dir.iterdir():
                if p.is_file() and p.suffix.lower() in (".ino", ".cpp", ".c", ".h", ".hpp", ".txt"):
                    sketch_file_names.append(p.name)
        except Exception:
            pass
        files_summary_str = ", ".join(sorted(sketch_file_names)) if sketch_file_names else "None found"

        # 3. Single instructions file recognized by OpenCode CLI (AGENTS.md in build cache)
        ai_backup_root = get_ai_edit_backup_root(s_dir, create=False).as_posix()
        content = (
            "---\n"
            "name: project-sketch-scope\n"
            "description: \"AI workspace instructions for MCU Flash GUI project. Specifies files to read vs ignore.\"\n"
            "---\n\n"
            "# 🚨 MCU FLASHER — ACTIVE SKETCH & HARDWARE SPECIFICATION (LIVE) 🚨\n\n"
            "## 📋 LIVE HARDWARE & PROJECT SPECIFICATIONS (ALWAYS USE THIS LIST)\n"
            f"- **Active Project Directory**: `{s_dir.resolve()}`\n"
            f"- **Authoritative Hardware Status**: `{live_summary}`\n"
            f"- **Currently Selected Board**: `{live_board}`\n"
            f"- **Target Platform / Architecture**: `{live_platform}`" + (f" ({live_fqbn})\n" if live_fqbn else "\n") +
            f"- **Connected COM Port**: `{live_port}`\n"
            f"- **Serial Monitor Baud Rate**: `{live_baud}`\n"
            f"- **Upload Speed**: `{live_upload_spd}` bps\n"
            f"- **Main Sketch Files in Project Root**: `{files_summary_str}`\n\n"
            "*CRITICAL DIRECTIVE FOR AI: The specifications above are live and authoritative from MCU Flasher GUI. "
            "When the user asks what board is selected, what port is connected, or what baud rate/upload speed is configured, "
            "answer directly from this specification list immediately without searching the disk or running PowerShell commands.*\n\n"
            "## ⚡ EXECUTION SEQUENCE\n\n"
            "### 1️⃣ STEP 1: Hardware & Board Questions\n"
            f"- Use the Live Hardware Specifications above or inspect `{PROJECT_BUILD_CACHE_DIR}/project_state.json` (Read-Only).\n"
            "  - If `hardware.mcu_connected` is false or `hardware.port` is null: answer immediately: \"No microcontroller is currently connected.\"\n"
            "  - If `hardware.board_selected` is false or `hardware.board_name` is null: answer immediately: \"No board is currently selected in the GUI.\"\n"
            f"  *Never run recursive PowerShell/find/glob searches or inspect `platformio.ini` to guess board selection.*\n\n"
            "### 2️⃣ STEP 2: Read & Edit ONLY Main Sketch Files at Root\n"
            "- Work strictly on primary sketch source files located at the root of this project folder:\n"
            "  - `*.ino` (Main Arduino sketch file at root)\n"
            "  - `*.h` / `*.hpp` (C/C++ header files at root)\n"
            "  - `*.cpp` / `*.c` (C/C++ source code files at root)\n"
            "  - `NOTE.txt` / `*.txt` (Notes & project documentation files created for the user)\n"
            "- Do NOT search, traverse, or inspect parent directories or external sibling folders.\n"
            "- Do NOT create nested source directories or submodules unless explicitly requested.\n\n"
            "### 3️⃣ STEP 3: Check Notifications & History When Troubleshooting\n"
            f"- If the user asks about a compilation error, upload failure, or reset issue, read `{PROJECT_BUILD_CACHE_DIR}/dbs_notif.json` (Read-Only) to see:\n"
            "  - Recent compiler errors, missing library warnings, and toolchain logs\n"
            "  - Upload status, device connect/disconnect logs, and library installations.\n\n"
            "### 4️⃣ STEP 4: Strict No-Touch on Build & Cache Files\n"
            f"- Files inside `{PROJECT_BUILD_CACHE_DIR}/` are internal build inputs, backups, and PlatformIO toolchain data.\n"
            f"- **NEVER** edit, modify, rename, or delete files inside `{PROJECT_BUILD_CACHE_DIR}/`, `.pio/`, `.vscode/`, `.clangd/`, or `.opencode/`.\n\n"
            "### 5️⃣ AI Edit Backup & Recovery\n"
            f"- Backup root: `{ai_backup_root}`\n"
            "- Folder layout: `M-D-YY/sessionN/editN.txt`. Each edit file contains exact BEFORE, AI/AFTER, current-applied, and Undo-target copies.\n"
            f"- The hidden `{PROJECT_BUILD_CACHE_DIR}/{AI_PROJECT_STORAGE_DIR}` folder travels with this sketch project across drives and computers.\n"
            "- Treat the backup tree as READ-ONLY. Never modify, rename, or delete backup files.\n"
            f"- Never read or edit `{PROJECT_BUILD_CACHE_DIR}/{AI_PROJECT_STORAGE_DIR}/.state`; it is application journal data.\n"
            "- When the user explicitly asks to recover or compare an earlier AI edit, locate the matching project/file entry and restore only the requested content section.\n\n"
            "---\n"
            "*Generated automatically by MCU Flash GUI by Naph for OpenCode AI Assistant.*\n"
        )

        agents_md = cache_dir / "AGENTS.md"
        try:
            if not agents_md.exists() or _is_mcu_generated_instruction_file(agents_md):
                ensure_file_writable(agents_md)
                agents_md.write_text(content, encoding="utf-8")
                hide_hidden_attribute(agents_md)
        except Exception:
            pass

        # 4. Clean up any stale generated files from sketch root so project root stays 100% clean
        for stale_name in (".opencodeignore", "AGENTS.md", "OPENCODE.md", "READ-FIRST.md", ".READ-FIRST.md", "SKILL.md", ".SKILL.md"):
            stale_p = s_dir / stale_name
            if stale_p.is_file() and _is_mcu_generated_instruction_file(stale_p):
                try:
                    ensure_file_writable(stale_p)
                    stale_p.unlink(missing_ok=True)
                except Exception:
                    pass

        # 4. OpenCode native skills for Sketch Workflow & Target Discovery
        workflow_skill_content = (
            "---\n"
            "name: sketch-workflow\n"
            "description: \"Live hardware specifications and workflow for this sketch: Selected board, COM port, baud rate, upload speed, and sketch file boundaries.\"\n"
            "---\n\n"
            "# Sketch Project Workflow & Priority Guide\n\n"
            "## 📋 LIVE HARDWARE & PROJECT SPECIFICATIONS (ALWAYS USE THIS LIST)\n"
            f"- **Active Project Directory**: `{s_dir.resolve()}`\n"
            f"- **Authoritative Hardware Status**: `{live_summary}`\n"
            f"- **Currently Selected Board**: `{live_board}`\n"
            f"- **Target Platform / Architecture**: `{live_platform}`" + (f" ({live_fqbn})\n" if live_fqbn else "\n") +
            f"- **Connected COM Port**: `{live_port}`\n"
            f"- **Serial Monitor Baud Rate**: `{live_baud}`\n"
            f"- **Upload Speed**: `{live_upload_spd}` bps\n"
            f"- **Main Sketch Files in Project Root**: `{files_summary_str}`\n\n"
            "*CRITICAL DIRECTIVE FOR AI: Answer all board, port, and upload speed questions directly from the list above without searching the disk.*\n\n"
            "### Step 1: Live Hardware Query\n"
            f"Use the live specifications above or read `{PROJECT_BUILD_CACHE_DIR}/project_state.json`. Never perform recursive disk scans or read `platformio.ini`.\n\n"
            "### Step 2: Read & Edit Only Root Sketch Files\n"
            "Confine all source code changes strictly to the root sketch directory:\n"
            "- Primary sketches: `*.ino`\n"
            "- Header files: `*.h`, `*.hpp`\n"
            "- Source files: `*.cpp`, `*.c`\n"
            "- Documentation: `NOTE.txt`\n"
            "Never search parent directories or edit files in `.mcu_flasher_build_cache/`, `.pio/`, or `.opencode/`.\n\n"
            "### Step 3: Check Build & Notification History\n"
            f"If troubleshooting compiler or upload errors, read `{PROJECT_BUILD_CACHE_DIR}/dbs_notif.json` to view recent error logs and device events.\n"
        )

        try:
            opencode_skills_base = s_dir / ".opencode" / "skills"
            workflow_dir = opencode_skills_base / "sketch-workflow"
            workflow_dir.mkdir(parents=True, exist_ok=True)
            workflow_file = workflow_dir / "SKILL.md"
            ensure_file_writable(workflow_file)
            workflow_file.write_text(workflow_skill_content, encoding="utf-8")
            hide_hidden_attribute(workflow_file)

            target_dir = opencode_skills_base / "mcu-sketch-target"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / "SKILL.md"
            ensure_file_writable(target_file)
            target_file.write_text(workflow_skill_content, encoding="utf-8")
            hide_hidden_attribute(target_file)

            # Also generate .opencode/AGENTS.md inside .opencode so OpenCode CLI finds it natively
            opencode_agents_file = s_dir / ".opencode" / "AGENTS.md"
            ensure_file_writable(opencode_agents_file)
            opencode_agents_file.write_text(content, encoding="utf-8")
            hide_hidden_attribute(opencode_agents_file)

            hide_generated_directory(s_dir / ".opencode")
        except Exception:
            pass
    except Exception:
        pass

def get_sketch_files_fast(sketch_dir, supported_extensions=None) -> list[Path]:
    """Perform an optimized, shallow-pruned directory walk over sketch_dir.
    Prunes hidden/internal directories (.pio, .git, build, MCU-FLASHER-SRC, etc.)
    at the directory level BEFORE recursing, avoiding massive disk scan freezes."""
    if not sketch_dir:
        return []
    s_dir = Path(sketch_dir)
    if not s_dir.exists():
        return []
    
    ignored_dir_names = {
        ".git", ".vscode", "env", "node_modules", "__pycache__",
        ".platformio", "build", ".pio", "src", "mcu-flasher-src", "mcu_flasher_src",
        "compiled_builds", "build_artifacts", ".build_artifacts", ".clangd", ".cache", "_temp",
        ".mcu_ai_edits", PROJECT_BUILD_CACHE_DIR, "logs",
    }
    
    results = []
    supp_set = {ext.lower() for ext in supported_extensions} if supported_extensions else None

    def _walk(current_dir):
        try:
            for entry in os.scandir(current_dir):
                name_low = entry.name.lower()
                if name_low.startswith(".") or name_low in ignored_dir_names or name_low.startswith("mcu-flasher"):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    _walk(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    p = Path(entry.path)
                    if supp_set is None or p.suffix.lower() in supp_set:
                        results.append(p)
        except Exception:
            pass

    _walk(s_dir)
    return results


def get_project_root_source_files(sketch_dir, supported_extensions=None) -> list[Path]:
    """List root-level sketch sources with one directory read.

    ``Path.glob('*.ino')`` repeated for every extension is noticeably slow on
    SMB/UNC shares and can produce inconsistent results when the share briefly
    drops a directory handle.  The compiler only treats root-level sketch
    sources as the Arduino project inputs, so enumerate the directory once.
    A short retry handles transient network-share errors without hiding a real
    missing-project condition.
    """
    if not sketch_dir:
        return []
    root = Path(sketch_dir)
    if not root.is_dir():
        return []
    extensions = {
        str(ext).lower() if str(ext).startswith(".") else f".{str(ext).lower()}"
        for ext in (supported_extensions or (".ino", ".cpp", ".c", ".h", ".hpp", ".txt"))
    }
    attempts = 2 if is_unc_or_network_path(root) else 1
    for attempt in range(attempts):
        try:
            files: list[Path] = []
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            if Path(entry.name).suffix.lower() in extensions:
                                files.append(Path(entry.path))
                    except OSError:
                        continue
            return sorted(files, key=lambda path: path.name.lower())
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(0.15)
    return []


def get_mcu_flasher_src_dir(sketch_dir) -> Path:
    """Return dedicated MCU-FLASHER-SRC folder inside sketch_dir for app-generated files,
    ensuring it exists and is marked as a hidden directory on Windows."""
    try:
        mcu_src = _migrate_legacy_project_generated_files(sketch_dir) / "MCU-FLASHER-SRC"
        mcu_src.mkdir(parents=True, exist_ok=True)
        hide_generated_directory(mcu_src)
        return mcu_src
    except Exception:
        return Path(sketch_dir)

def hide_internal_project_metadata(sketch_dir) -> None:
    """Hide the explicit app-generated allowlist in Windows Explorer.

    User sources and every unrecognized root entry remain visible and
    writable. Unknown names are never inferred to be metadata merely because
    of their extension, preventing the app from hiding user docs, assets,
    configuration, or future source types.
    """
    try:
        s_dir = Path(sketch_dir)
        if not s_dir.exists():
            return

        # Callers that already have the staged cache must still operate on the
        # project root; otherwise instruction files would be nested forever.
        if s_dir.name.lower() == PROJECT_BUILD_CACHE_DIR.lower():
            s_dir = s_dir.parent
        _migrate_legacy_project_generated_files(s_dir)

        try:
            is_codebase_root = s_dir.resolve(strict=False) == SCRIPT_DIR.resolve(strict=False)
        except Exception:
            is_codebase_root = False

        # Older releases could hide the codebase itself through a junction
        # attribute. Repair only these exact directories; do not walk children.
        if is_codebase_root:
            unhide_hidden_attribute(s_dir)
            unhide_hidden_attribute(s_dir / "src")
            unhide_hidden_attribute(s_dir / "src" / "modules")
            unhide_hidden_attribute(s_dir / "platformio.ini")
        
        # Ensure OpenCode ignore files & instructions exist first before anything else
        ensure_hidden_read_first_md(s_dir)

        internal_names = [
            PROJECT_BUILD_CACHE_DIR,
            ".pio", "MCU-FLASHER-SRC", "MCU_FLASHER_SRC",
            "compiled_builds", "build_artifacts", ".build_artifacts",
            ".mcu_gui_cache.json", ".mcu_flash_syntax_errors.json",
            ".mcu_gui_compat_cache.json", ".mcu_flash_tab_order.json",
            ".ai_edit_signal",
            ".pio_cache", ".mcu_ai_edits", ".opencode",
        ]
        if is_codebase_root:
            # The application root is code, not a user sketch. Keep its source
            # tree and PlatformIO file visible while still hiding app caches.
            internal_names = [
                ".pio", "index_json", "compiled_builds", "build_artifacts",
                ".build_artifacts", ".mcu_gui_cache.json",
                ".mcu_flash_syntax_errors.json", ".mcu_gui_compat_cache.json",
                ".mcu_flash_tab_order.json", ".ai_edit_signal", ".pio_cache",
                ".mcu_ai_edits", PROJECT_BUILD_CACHE_DIR, "env", "logs",
            ]
        for name in internal_names:
            p = s_dir / name
            if p.exists():
                if p.is_dir():
                    hide_generated_directory(p)
                else:
                    hide_hidden_attribute(p)

        # These standard filenames are hidden only when their content proves
        # MCU Flasher created them.  Never hide a user's own AGENTS.md,
        # .opencodeignore, SKILL.md, or other instruction file by name alone.
        for name in (
            ".opencodeignore", "AGENTS.md", "OPENCODE.md", ".ignore",
            "READ-FIRST.md", ".READ-FIRST.md", "SKILL.md", ".SKILL.md",
        ):
            p = s_dir / name
            if _is_mcu_generated_instruction_file(p):
                hide_hidden_attribute(p)

        # The allowlist is authoritative.  Do not sweep and unhide unknown
        # entries: they may be user-hidden files, private assets, or files
        # created by another tool.  This also prevents a scanner/other process
        # from being needlessly disturbed by attribute churn on user files.
        if is_codebase_root:
            for generated_dir in (
                s_dir / "src" / ".platformio-mcu-gui",
                s_dir / "src" / "_board-frameworks",
            ):
                if generated_dir.is_dir():
                    hide_generated_directory(generated_dir)
    except Exception:
        pass

def heal_platformio_ini_symlinks_and_dirs(ini_path, sketch_dir=None) -> bool:
    """Heal platformio.ini when moved across devices or user accounts.
    Scans for symlink://<path> entries in lib_deps and paths in lib_extra_dirs.
    If a target path does not exist on the current machine (e.g. C:/Users/Admin/... on a machine with user napht),
    this function re-navigates it to the current device's download directory (Libs/<folder>),
    or standard Arduino/PlatformIO library locations using arduino_lib_req helper.
    """
    try:
        p = Path(ini_path)
        if not p.exists() or not p.is_file():
            return False

        ensure_file_writable(p)
        content = p.read_text(encoding="utf-8", errors="replace")
        old_content = content

        try:
            from src.modules.arduino_lib_req import heal_library_path_on_current_device
        except Exception:
            heal_library_path_on_current_device = lambda s: s

        try:
            from main.core.board_catalog import _get_download_dir
            download_dir = _get_download_dir()
        except Exception:
            download_dir = str(Path.home() / "Documents" / "Arduino")
        current_libs_dir = Path(download_dir) / "Libs"
        modified = False

        # 1. Heal symlink:// entries
        def _heal_symlink_match(match):
            nonlocal modified
            full_slug = match.group(0)
            healed = heal_library_path_on_current_device(full_slug)
            if healed != full_slug:
                modified = True
            return healed

        symlink_pattern = re.compile(r'symlink://[^\s\n\r]+', re.IGNORECASE)
        content = symlink_pattern.sub(_heal_symlink_match, content)

        # 2. Heal lib_extra_dirs
        def _heal_extra_dirs_match(match):
            nonlocal modified
            line = match.group(0)
            key = match.group(1)
            raw_dirs = match.group(2).strip()

            dir_parts = [d.strip() for d in raw_dirs.split(",") if d.strip()]
            valid_dirs = []
            for d in dir_parts:
                d_obj = Path(d.replace("\\", "/"))
                if d_obj.exists():
                    valid_dirs.append(d_obj.resolve().as_posix())
                else:
                    if current_libs_dir.exists():
                        valid_dirs.append(current_libs_dir.resolve().as_posix())
                        modified = True

            if not valid_dirs and current_libs_dir.exists():
                valid_dirs.append(current_libs_dir.resolve().as_posix())
                modified = True

            if valid_dirs:
                return f"lib_extra_dirs = {', '.join(valid_dirs)}"
            return line

        extra_dirs_pattern = re.compile(r'^(lib_extra_dirs\s*=)(.*)$', re.MULTILINE | re.IGNORECASE)
        content = extra_dirs_pattern.sub(_heal_extra_dirs_match, content)

        if modified or content != old_content:
            _write_ok = False
            _last_err = None
            for _i in range(6):
                try:
                    ensure_file_writable(p)
                    p.write_text(content, encoding="utf-8")
                    _write_ok = True
                    break
                except Exception as _e:
                    _last_err = _e
                    time.sleep(0.15 * (_i + 1))
            if not _write_ok:
                try:
                    import tempfile as _tf
                    _bak = p.with_suffix(p.suffix + ".locked")
                    _bak.write_text(content, encoding="utf-8")
                except Exception:
                    pass
                return False
            if sketch_dir and sketch_dir.exists():
                _healed_stale = any(
                    s.startswith("symlink://") and not Path(s[len("symlink://"):]).exists()
                    for s in symlink_pattern.findall(old_content)
                )
                if _healed_stale:
                    libdeps_dir = get_project_build_cache_root(
                        sketch_dir, create=False
                    ) / ".pio" / "libdeps"
                    if libdeps_dir.exists():
                        robust_rmtree(libdeps_dir)
            return True
        return False
    except Exception:
        return False


def align_sketch_filename_case(folder_path: Path | str) -> list[tuple[Path, Path]]:
    """Ensure sketch (.ino), header (.h/.hpp), and source (.cpp/.c) filenames
    match the exact casing of their parent project directory.

    For example, if folder is 'TestSketch' and the file is 'Testsketch.ino',
    safely renames 'Testsketch.ino' -> 'TestSketch.ino' (and matching .h/.cpp).
    Uses a temporary two-step rename to guarantee Windows NTFS case updates.

    Returns a list of (old_path, new_path) pairs for all renamed files.
    """
    renamed: list[tuple[Path, Path]] = []
    try:
        if not folder_path:
            return renamed
        folder = Path(folder_path)
        if not folder.exists() or not folder.is_dir():
            return renamed

        folder_name = folder.name
        folder_name_lower = folder_name.lower()

        # Check all files directly inside the project root
        try:
            entries = list(folder.iterdir())
        except Exception:
            return renamed

        for f in entries:
            try:
                if not f.is_file():
                    continue

                ext = f.suffix.lower()
                if ext not in (".ino", ".h", ".hpp", ".cpp", ".c"):
                    continue

                stem = f.stem
                # Check if file stem matches folder name case-insensitively but differs in exact case
                if stem.lower() == folder_name_lower and stem != folder_name:
                    target_filename = f"{folder_name}{f.suffix}"
                    target_path = folder / target_filename

                    # If target already exists as a distinct file, don't overwrite
                    if target_path.exists() and target_path.resolve() != f.resolve():
                        continue

                    ensure_file_writable(f)
                    # Use a two-step temporary rename to force Windows NTFS to record the case change
                    temp_name = folder / f"__case_align_{int(time.time() * 1000)}_{f.name}"
                    f.rename(temp_name)
                    temp_name.rename(target_path)
                    renamed.append((f, target_path))
            except Exception as item_err:
                try:
                    target_filename = f"{folder_name}{f.suffix}"
                    target_path = folder / target_filename
                    f.replace(target_path)
                    renamed.append((f, target_path))
                except Exception:
                    pass
    except Exception:
        pass
    return renamed


__all__ = [
    "AIEditBackupStore",
    "AI_PROJECT_STORAGE_DIR",
    "_TRANSIENT_FILE_LOCK_WINERRORS",
    "_hide_junction",
    "_is_mcu_generated_instruction_file",
    "_legacy_ai_review_state_file",
    "_looks_like_mcu_generated_staged_src",
    "_migrate_legacy_project_generated_files",
    "_set_windows_file_attributes",
    "_unc_share_root",
    "_volume_cache_key_for",
    "align_sketch_filename_case",
    "classify_platformio_failure",
    "ensure_file_writable",
    "ensure_hidden_read_first_md",
    "get_ai_edit_backup_root",
    "get_ai_project_storage_root",
    "get_ai_review_state_file",
    "get_mcu_flasher_src_dir",
    "get_project_build_cache_root",
    "get_project_root_source_files",
    "get_project_temp_file",
    "get_sketch_files_fast",
    "get_volume_info",
    "heal_platformio_ini_symlinks_and_dirs",
    "hide_generated_directory",
    "hide_hidden_attribute",
    "hide_internal_project_metadata",
    "is_nonfatal_pio_clean_report",
    "is_ntfs_path",
    "is_transient_file_lock_error",
    "is_transient_platformio_lock_report",
    "is_unc_or_network_path",
    "is_volume_writable",
    "retry_transient_file_operation",
    "robust_rmtree",
    "unhide_hidden_attribute"
]
