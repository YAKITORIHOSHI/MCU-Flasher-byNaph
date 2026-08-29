#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import time
import json
import re
import shutil
import tempfile
import subprocess
import threading
import queue
import ctypes
import traceback
import hashlib
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font as tkfont


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

class BuildWorkspaceMixin(_Base):
    """Mixin providing BuildWorkspaceMixin capabilities for MCUUploadGUI."""
    def _board_cache_key(self, board_name: str | None = None) -> str:
        """Return a short, collision-resistant key for one exact board config.

        Display names are not safe identifiers (``Foo-Bar`` and ``Foo Bar``
        normalize to the same slug), and family buckets such as ``ESP32`` can
        mix incompatible binaries.  The readable prefix therefore comes from
        PlatformIO's canonical platform/board ids while the digest covers the
        complete board definition that can influence compilation.
        """
        if board_name is not None:
            name = board_name
        else:
            board_var = getattr(self, "board_var", None)
            name = board_var.get() if board_var is not None else ""
        info = dict(SUPPORTED_BOARDS.get(name, {}))
        identity = {
            # Display name is always part of the identity.  Downloaded board
            # aliases can normalize to the same PlatformIO id/definition but
            # the product contract is one cache folder per selectable board.
            "display_name": name,
            "platform": str(info.get("platform", "")),
            "board": str(info.get("board", "")),
            "framework": str(info.get("framework", "")),
            "definition": info,
        }
        encoded = json.dumps(
            identity, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8", errors="replace")
        digest = hashlib.sha256(encoded).hexdigest()[:10]
        readable = "_".join(
            part for part in (
                str(info.get("platform", "")),
                str(info.get("board", "")),
            ) if part
        ) or str(name or "unknown_board")
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", readable).strip("_").lower()
        # Keep this deliberately compact for Windows installations whose
        # sketch path is already close to the legacy MAX_PATH limit.  The
        # digest, rather than the readable prefix, provides uniqueness.
        return f"{(slug or 'board')[:20]}_{digest}"

    def _mapped_or_sketch_dir(self, project_dir: Path | None = None) -> Path:
        """Return the effective sketch directory, rewriting to the mapped drive
        letter if a UNC mapping is currently active."""
        project = Path(project_dir or self.sketch_dir_path)
        drive_spec = getattr(self, "_unc_mapped_drive", None)
        if not drive_spec or not is_unc_or_network_path(project):
            return project
        share_root = _unc_share_root(project)
        if not share_root:
            return project
        relative_part = str(project).replace("/", "\\")
        share_norm = share_root.rstrip("\\")
        if relative_part.lower().startswith(share_norm.lower()):
            relative_part = relative_part[len(share_norm):]
        return Path(f"{drive_spec}{relative_part}")

    def _canonical_sketch_path(self, project_dir: Path | None = None) -> Path:
        """Return the canonical, stable path for a sketch directory.
        For mapped network drives, resolves to the underlying UNC path so
        workspace hashes and cache keys remain identical regardless of whether
        a temporary drive mapping is currently mounted.
        """
        project = Path(project_dir or self.sketch_dir_path)
        p_str = str(project).replace("/", "\\").rstrip("\\")
        mapped_drive = getattr(self, "_unc_mapped_drive", None)
        if mapped_drive and p_str.upper().startswith(mapped_drive.upper()):
            rel = p_str[len(mapped_drive):].lstrip("\\")
            share_root = _unc_share_root(self.sketch_dir_path)
            if share_root:
                return Path(share_root) / rel if rel else Path(share_root)
        if sys.platform == "win32" and len(p_str) >= 2 and p_str[1] == ":":
            try:
                import ctypes
                buf = ctypes.create_unicode_buffer(512)
                cb = ctypes.c_ulong(512)
                res = ctypes.windll.mpr.WNetGetConnectionW(p_str[:2], buf, ctypes.byref(cb))
                if res == 0 and buf.value:
                    unc_root = buf.value.rstrip("\\")
                    rel = p_str[2:].lstrip("\\")
                    return Path(f"{unc_root}\\{rel}" if rel else unc_root)
            except Exception:
                pass
        return project

    def _remote_workspace_root(self, project_dir: Path | None = None) -> Path | None:
        """For a remote/UNC project (e.g. ``\\\\server\\share\\...`` or mapped network drives),
        return the local fast workspace root on the local SSD.

        Building intermediate objects and SCons signature databases (``.sconsign*.dblite``)
        directly over SMB/network shares causes file locking failures and network latency.
        Routing remote project workspaces to local storage guarantees 100% reliable builds
        and high-speed compilation while preserving the remote source files.

        Returns None for local drive projects (which build in the hidden
        ``project/.mcu_flasher_build_cache`` folder).
        """
        canonical = self._canonical_sketch_path(project_dir)
        if not is_unc_or_network_path(canonical):
            return None
        import hashlib
        import re
        proj_hash = hashlib.sha1(str(canonical).lower().encode("utf-8")).hexdigest()[:12]
        proj_name = re.sub(r'[^A-Za-z0-9_.-]', '_', canonical.name) or "project"
        core_store = os.environ.get("PLATFORMIO_CORE_DIR")
        base = Path(core_store) if core_store else SCRIPT_DIR
        return base / "remote_workspaces" / f"{proj_name}_{proj_hash}"

    def _board_workspace(self, project_dir: Path | None = None,
                         board_name: str | None = None) -> Path:
        """Project-local PlatformIO workspace dedicated to one exact board.

        For remote/UNC network projects, automatically resolves to the local fast
        storage workspace under ``remote_workspaces/<project>_<hash>/boards/<board_key>``.
        For local projects, resolves to
        ``<project>/.mcu_flasher_build_cache/boards/<board_key>``.
        """
        remote_root = self._remote_workspace_root(project_dir)
        if remote_root is not None:
            return remote_root / "boards" / self._board_cache_key(board_name)

        project = self._mapped_or_sketch_dir(project_dir)
        cache_root = _migrate_legacy_project_generated_files(project)
        return cache_root / "boards" / self._board_cache_key(board_name)

    def _platformio_project_dir(self, project_dir: Path | None = None,
                                board_name: str | None = None) -> Path:
        """Return the directory PlatformIO should use as its project root.

        A remote sketch must not be used as PlatformIO's working directory:
        SCons repeatedly reads the project tree and writes generated files,
        which is both slow and prone to SMB/UNC locking errors.  Remote
        projects therefore use their local, board-isolated workspace as a
        complete staged PlatformIO project.  Local projects use the hidden
        project build cache so the sketch root contains only user-authored
        files.
        """
        project = Path(project_dir or self.sketch_dir_path)
        if self._remote_workspace_root(project) is not None:
            workspace = self._board_workspace(project, board_name)
            workspace.mkdir(parents=True, exist_ok=True)
            return workspace
        return _migrate_legacy_project_generated_files(
            self._mapped_or_sketch_dir(project)
        )

    def _platformio_ini_path(self, project_dir: Path | None = None,
                             board_name: str | None = None) -> Path:
        """Return the private PlatformIO configuration for this project."""
        return self._platformio_project_dir(project_dir, board_name) / "platformio.ini"

    def _board_build_root(self, project_dir: Path | None = None,
                          board_name: str | None = None) -> Path:
        """Configured PlatformIO ``build_dir`` for one board."""
        return self._board_workspace(project_dir, board_name) / "build"

    def _board_build_dir(self, project_dir: Path | None = None,
                         board_name: str | None = None,
                         env_name: str | None = None) -> Path:
        """Directory containing firmware and objects for one board/env."""
        env = env_name or (
            self._pio_env_name(board_name)
            if board_name is not None else self._pio_env_name()
        )
        return self._board_build_root(project_dir, board_name) / env

    def _migrate_legacy_board_cache(self, project_dir: Path | None = None,
                                    board_name: str | None = None) -> None:
        """Move the old shared-root env into the new isolated workspace once.

        Renaming within the same project volume is cheap even for a large build
        tree.  Migration is best-effort: PlatformIO can always rebuild safely.
        """
        project = Path(project_dir or self.sketch_dir_path)
        # Do not move a legacy build tree across a network share.  That can
        # copy hundreds of megabytes over SMB before the first build and can
        # fail when an antivirus/indexer has a remote file open.  A remote
        # project gets a clean local board workspace and rebuilds incrementally
        # from the staged sources instead.
        if self._remote_workspace_root(project) is not None:
            return
        project = self._mapped_or_sketch_dir(project)
        env_name = (
            self._pio_env_name(board_name)
            if board_name is not None else self._pio_env_name()
        )
        destination_root = self._board_build_root(project, board_name)
        destination_env = destination_root / env_name
        legacy_root = (
            _migrate_legacy_project_generated_files(project) / ".pio" / "build"
        )
        legacy_env_names = [env_name, self._legacy_pio_env_name(board_name)]
        try:
            import shutil as _cache_shutil
            for legacy_name in dict.fromkeys(legacy_env_names):
                legacy_env = legacy_root / legacy_name
                if legacy_env.is_dir() and not destination_env.exists():
                    destination_root.mkdir(parents=True, exist_ok=True)
                    _cache_shutil.move(str(legacy_env), str(destination_env))
                    legacy_checksum = legacy_root / "project.checksum"
                    if legacy_checksum.is_file():
                        _cache_shutil.copy2(
                            legacy_checksum, destination_root / "project.checksum"
                        )
                    break

            # Also absorb a cache created by an early per-board build that
            # still used the unbounded display-name environment.
            old_workspace_env = destination_root / self._legacy_pio_env_name(board_name)
            if old_workspace_env.is_dir() and not destination_env.exists():
                _cache_shutil.move(str(old_workspace_env), str(destination_env))

            destination_libdeps = (
                self._board_workspace(project, board_name) / "libdeps" / env_name
            )
            for legacy_name in dict.fromkeys(legacy_env_names):
                legacy_libdeps = (
                    _migrate_legacy_project_generated_files(project)
                    / ".pio" / "libdeps" / legacy_name
                )
                if legacy_libdeps.is_dir() and not destination_libdeps.exists():
                    destination_libdeps.parent.mkdir(parents=True, exist_ok=True)
                    _cache_shutil.move(str(legacy_libdeps), str(destination_libdeps))
                    break
        except Exception:
            # Cache migration must never block a compile; a miss simply causes
            # PlatformIO to populate the new workspace normally.
            pass

    def _platformio_subprocess_env(self, project_dir: Path | None = None,
                                   board_name: str | None = None,
                                   jobs: int | None = None) -> dict:
        """Build a low-overhead PlatformIO environment for one board.

        PlatformIO checksum-cleans its *entire* configured build_dir whenever
        platformio.ini changes.  Pointing every board at a distinct build_dir
        confines that cleanup to the selected board.  No separate SCons
        BuildCache is configured: the retained build tree already supplies
        incremental objects, avoiding a second object copy on low-storage PCs.
        Framework/toolchain packages remain shared through PLATFORMIO_CORE_DIR.
        """
        project = Path(project_dir or self.sketch_dir_path)
        self._migrate_legacy_board_cache(project, board_name)
        workspace = self._board_workspace(project, board_name)
        build_root = workspace / "build"
        libdeps_root = workspace / "libdeps"
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            if self._remote_workspace_root(project) is None:
                hide_generated_directory(
                    _migrate_legacy_project_generated_files(project)
                )
        except Exception:
            pass

        env = os.environ.copy()
        core_dir, _core_was_refreshed = _refresh_platformio_core_environment(SCRIPT_DIR)
        # Reassert this in the child environment instead of relying on a
        # possibly stale inherited value from an earlier app launch.
        env["PLATFORMIO_CORE_DIR"] = str(core_dir)
        env["PLATFORMIO_CACHE_DIR"] = str(core_dir / ".cache")
        env["PLATFORMIO_GLOBALLIB_DIR"] = str(core_dir / "lib")
        env["TMP"] = str(core_dir / ".tmp")
        env["TEMP"] = str(core_dir / ".tmp")
        env["TMPDIR"] = str(core_dir / ".tmp")
        env["PLATFORMIO_WORKSPACE_DIR"] = str(workspace)
        env["PLATFORMIO_BUILD_DIR"] = str(build_root)
        env["PLATFORMIO_LIBDEPS_DIR"] = str(libdeps_root)
        # An app-global SCons CacheDir used to duplicate objects and share one
        # signature database across projects.  Explicitly disable inheritance.
        env.pop("PLATFORMIO_BUILD_CACHE_DIR", None)
        env["PYTHONUNBUFFERED"] = "1"
        env["PLATFORMIO_UNBUFFERED"] = "1"
        env["PLATFORMIO_SETTING_ENABLE_CACHE"] = "true"
        env["PYTHONDONTWRITEBYTECODE"] = "0"
        if jobs is not None:
            safe_jobs = max(1, int(jobs))
            env["PLATFORMIO_BUILD_JOBS"] = str(safe_jobs)
            env["SCONSFLAGS"] = f"-j{safe_jobs}"
        return env

    # ── UNC / network-path drive mapping ──────────────────────────────────
    # Windows cannot use a UNC path (\\server\share\...) as the current
    # working directory for a subprocess.  When that happens, cmd.exe (and
    # most build tools) silently falls back to C:\Windows, which causes
    # PlatformIO to create .pio inside C:\Windows → PermissionError, and
    # SCons to resolve relative source paths against the wrong root →
    # "No such file or directory".
    #
    # The fix is to temporarily map the UNC share root to a free drive
    # letter via `net use`, translate source reads to that drive, and let the
    # owning Compile/Upload wrapper clean it up only after success.

    def _map_unc_for_build(self, project_path: Path | None = None) -> Path:
        """If the sketch is on a UNC share, map it to a temporary drive letter.

        Returns the effective project path to use as subprocess CWD.
        Stores cleanup state in ``self._unc_mapped_drive`` (the drive spec
        like ``"Z:"``) so ``_unmap_unc_after_build()`` can undo it.  If the
        path is local or mapping fails gracefully, returns the original path
        and sets ``self._unc_mapped_drive = None``.
        """
        project = Path(project_path or self.sketch_dir_path)
        if not is_unc_or_network_path(project):
            return project

        share_root = _unc_share_root(project)
        if not share_root:
            return project

        # Preserve ownership when a previous failed operation left our
        # temporary mapping mounted for a retry.  The old implementation reset
        # this field on every call, which made that mapping look externally
        # owned and prevented a later successful operation from removing it.
        owned_drive = getattr(self, "_unc_mapped_drive", None)
        if owned_drive:
            try:
                if os.path.exists(f"{owned_drive}\\"):
                    relative_part = str(project).replace("/", "\\")
                    share_norm = share_root.rstrip("\\")
                    if relative_part.lower().startswith(share_norm.lower()):
                        relative_part = relative_part[len(share_norm):]
                    return Path(f"{owned_drive}{relative_part}")
            except Exception:
                pass
            self._unc_mapped_drive = None

        # Check if the share is already mapped to an existing drive letter.
        # `net use` lists current mappings.
        try:
            existing = subprocess.run(
                ["net", "use"], capture_output=True, text=True, timeout=10,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
            share_lower = share_root.lower().rstrip("\\")
            for line in existing.stdout.splitlines():
                parts = line.split()
                # Typical line: "OK  Z:  \\server\share  Microsoft Windows Network"
                for idx, part in enumerate(parts):
                    if len(part) == 2 and part[1] == ":" and part[0].isalpha():
                        # Found a drive letter — check if the next token matches our share.
                        if idx + 1 < len(parts) and parts[idx + 1].lower().rstrip("\\") == share_lower:
                            # Already mapped — reuse it without creating a new one.
                            drive_spec = part.upper()
                            relative_part = str(project).replace("/", "\\")
                            share_norm = share_root.rstrip("\\")
                            if relative_part.lower().startswith(share_norm.lower()):
                                relative_part = relative_part[len(share_norm):]
                            mapped_path = Path(f"{drive_spec}{relative_part}")
                            mapping_log_key = (drive_spec, share_root.lower().rstrip("\\"))
                            if getattr(self, "_last_unc_mapping_log_key", None) != mapping_log_key:
                                self._append(
                                    f"  🌐 Using existing drive mapping {drive_spec} → {share_root}",
                                    "info",
                                )
                                self._last_unc_mapping_log_key = mapping_log_key
                            # Don't set _unc_mapped_drive — we didn't create this mapping.
                            return mapped_path
        except Exception:
            pass

        # Find a free drive letter (Z: down to A:).
        import string
        mapped_letter = None
        for letter in reversed(string.ascii_uppercase):
            test_root = f"{letter}:\\"
            if not os.path.exists(test_root):
                mapped_letter = letter
                break

        if mapped_letter is None:
            self._append(
                "  ⚠ Could not find a free drive letter for the network "
                "share — trying UNC path directly.",
                "warning",
            )
            return project

        drive_spec = f"{mapped_letter}:"
        try:
            map_cmd = ["net", "use", drive_spec, share_root]
            map_result = subprocess.run(
                map_cmd, capture_output=True, text=True, timeout=30,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
            if map_result.returncode != 0:
                self._append(
                    f"  ⚠ Could not map {share_root} → {drive_spec} "
                    f"({map_result.stderr.strip()}) — trying UNC path directly.",
                    "warning",
                )
                return project

            self._unc_mapped_drive = drive_spec
            self._last_unc_mapping_log_key = (drive_spec, share_root.lower().rstrip("\\"))
            self._append(
                f"  🌐 Mapped network share → {drive_spec} "
                f"(temporary, for this build session)",
                "info",
            )

            # Rewrite the project path: \\server\share\sub\folder → Z:\sub\folder
            relative_part = str(project).replace("/", "\\")
            share_norm = share_root.rstrip("\\")
            if relative_part.lower().startswith(share_norm.lower()):
                relative_part = relative_part[len(share_norm):]
            return Path(f"{drive_spec}{relative_part}")

        except Exception as exc:
            self._append(
                f"  ⚠ Drive-mapping failed ({exc}) — trying UNC path directly.",
                "warning",
            )
            return project

    def _unmap_unc_after_build(self) -> None:
        """Remove the temporary drive mapping created by _map_unc_for_build.

        Safe to call even if no mapping was created (no-op)."""
        drive_spec = getattr(self, "_unc_mapped_drive", None)
        if not drive_spec:
            return
        try:
            unmap_cmd = ["net", "use", drive_spec, "/delete", "/y"]
            subprocess.run(
                unmap_cmd, capture_output=True, text=True, timeout=15,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
            self._append(
                f"  🌐 Unmapped temporary drive {drive_spec}",
                "dim",
            )
        except Exception:
            pass
        finally:
            self._unc_mapped_drive = None

