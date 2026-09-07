#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations
# pyright: reportGeneralTypeIssues=false

import sys
import os
import json
import re
import threading
import ctypes
import hashlib
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk


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

# Process-wide RAM cache for source file contents to eliminate repeated disk reads.
# Maps file_path -> (mtime_ns, file_size, raw_bytes).
_SOURCE_FILE_RAM_CACHE: dict[str, tuple[int, int, bytes]] = {}
_SOURCE_FILE_RAM_CACHE_LOCK = threading.Lock()

# Process-wide memoization for calculated sketch MD5 hashes.
# Maps sketch_dir -> (cache_fingerprint_tuple, md5_hex_string).
_SOURCE_HASH_MEMO_CACHE: dict[str, tuple[tuple, str]] = {}

class CompileCacheMixin(_Base):
    """Mixin providing CompileCacheMixin capabilities for MCUUploadGUI."""
    def _is_framework_downloaded(self, board_name: str | None = None) -> bool:
        """Check if the core framework and toolchains for the board's platform are installed."""
        if not board_name:
            board_name = (
                getattr(self, "_active_board_name", "")
                if threading.get_ident() != getattr(self, "_tk_thread_id", None)
                else self.board_var.get()
            )
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        platform = board_info.get("platform", "espressif32")
        board_id = board_info.get("board", "")
        framework = board_info.get("framework", "arduino") or "arduino"
        pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR", str(Path.home() / ".platformio"))

        # A successful main-app prewarm records a board-specific marker. This
        # is the dynamic path for newly supported platforms and avoids
        # treating a downloaded Arduino boards.txt file as a usable compiler.
        if board_id and board_toolchain_ready(
            pio_core_dir, platform, board_id, framework
        ):
            return True
        return False

    def _mark_env_just_created(self, board_name: str | None = None):
        """Mark an environment as newly created so compile won't be skipped until first successful build."""
        if not hasattr(self, "_just_created_envs") or not isinstance(self._just_created_envs, set):
            self._just_created_envs = set()
        env = board_name or self._pio_env_name()
        self._just_created_envs.add(env)
        if board_name:
            self._just_created_envs.add(board_name)
        try:
            self._save_compile_cache()
        except Exception:
            pass

    def _save_compile_cache(self):
        """Snapshot source hashes after a successful compile and save to disk,
        keyed by board so each board keeps its own independent cache entry."""
        self._last_compiled_board = str(
            getattr(self, "_active_board_name", "")
            or (self.board_var.get() if threading.get_ident() == getattr(self, "_tk_thread_id", None) else "")
        )
        board_key = ""
        if self._last_compiled_board:
            board_key = self._board_cache_key(self._last_compiled_board)
            if not hasattr(self, "_build_config_hash_by_board"):
                self._build_config_hash_by_board = {}
            config_hash = self._build_config_fingerprint(
                self._last_compiled_board, allow_cached=False
            )
            if config_hash:
                self._build_config_hash_by_board[board_key] = config_hash
        self._compile_cache_hash = self._hash_sources()
        if self._last_compiled_board and board_key:
            self._compile_cache_by_board[board_key] = self._compile_cache_hash
        if not hasattr(self, "_just_created_envs") or not isinstance(self._just_created_envs, set):
            self._just_created_envs = set()
        # Remove current env / board from newly created envs since compilation succeeded
        self._just_created_envs.discard(self._pio_env_name())
        if self._last_compiled_board:
            self._just_created_envs.discard(self._last_compiled_board)
        try:
            cache_data = {
                # Legacy single-slot fields kept for backward compatibility
                # with any older cache file / external readers.
                "hash": self._compile_cache_hash,
                "board": self._last_compiled_board,
                "boards": self._compile_cache_by_board,
                "build_configs": self._build_config_hash_by_board,
                "build_metadata": getattr(self, "_build_metadata_by_board", {}),
                "just_created_envs": list(self._just_created_envs),
            }
            cache_path = self._get_cache_file_path()
            ensure_file_writable(cache_path)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(
                cache_path.name + f".tmp-{os.getpid()}-{threading.get_ident()}"
            )
            payload = json.dumps(cache_data)
            temporary.write_text(payload, encoding="utf-8")
            try:
                os.replace(temporary, cache_path)
            except PermissionError:
                # Some removable Windows filesystems reject replacing a
                # hidden destination even though it is writable.  Preserve
                # the hidden attribute and fall back to an in-place write.
                temporary.unlink(missing_ok=True)
                if not self._force_write_text(cache_path, payload):
                    raise
            finally:
                temporary.unlink(missing_ok=True)
            hide_hidden_attribute(cache_path)
        except Exception:
            pass
        self._set_symbol_cache_compiled_state(True)

    def _load_compile_cache(self):
        """Load the compile cache from disk."""
        cache_file = None
        try:
            cache_file = self._get_cache_file_path()
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                self._compile_cache_hash = data.get("hash")
                self._last_compiled_board = data.get("board")
                self._compile_cache_by_board = data.get("boards") or {}
                self._build_config_hash_by_board = data.get("build_configs") or {}
                self._build_metadata_by_board = data.get("build_metadata") or {}
                self._just_created_envs = set(data.get("just_created_envs") or [])
                # Migrate an old single-slot cache file (no "boards" key yet)
                # into the per-board dict so it isn't silently discarded.
                if not self._compile_cache_by_board and self._last_compiled_board and self._compile_cache_hash:
                    self._compile_cache_by_board = {self._last_compiled_board: self._compile_cache_hash}
                if self._has_prior_build():
                    recompile_needed, _ = self._needs_recompile()
                    if not recompile_needed:
                        if threading.get_ident() == getattr(self, "_tk_thread_id", None):
                            self._set_symbol_cache_compiled_state(True)
                        elif callable(getattr(self, "_post_ui", None)):
                            self._post_ui(
                                lambda: self._set_symbol_cache_compiled_state(True)
                            )
            else:
                self._compile_cache_hash = None
                self._last_compiled_board = None
                self._compile_cache_by_board = {}
                self._build_config_hash_by_board = {}
                self._build_metadata_by_board = {}
                self._just_created_envs = set()
        except Exception:
            try:
                if cache_file:
                    cache_file.unlink(missing_ok=True)
            except Exception:
                pass
            self._compile_cache_hash = None
            self._last_compiled_board = None
            self._compile_cache_by_board = {}
            self._build_config_hash_by_board = {}
            self._build_metadata_by_board = {}
            self._just_created_envs = set()

    def _capture_build_metadata(self, output_lines: list[str]) -> None:
        """Remember exact PlatformIO summary rows for the current build.

        ``firmware.bin`` size alone cannot reproduce PlatformIO's Flash value,
        and it contains no RAM information.  Capturing the authoritative rows
        at compile time is both exact and essentially free.
        """
        board_name = str(
            getattr(self, "_active_board_name", "")
            or (self.board_var.get() if threading.get_ident() == getattr(self, "_tk_thread_id", None) else "")
        )
        if not board_name:
            return
        board_key = self._board_cache_key(board_name)
        captured: dict[str, str] = {}
        for raw in output_lines:
            line = _strip_terminal_escapes(raw)
            low = line.lower()
            if low.startswith("debug:") and "debug" not in captured:
                captured["debug"] = line
            elif low.startswith("ram:") and "ram" not in captured:
                captured["ram"] = line
            elif low.startswith("flash:") and "flash" not in captured:
                captured["flash"] = line
        if not captured:
            return

        entry: dict = {
            **captured,
            "env_name": self._pio_env_name(board_name),
            "source_hash": self._hash_sources(),
        }
        try:
            firmware = self._board_build_dir() / "firmware.bin"
            stat = firmware.stat()
            entry["firmware_size"] = stat.st_size
            entry["firmware_mtime_ns"] = stat.st_mtime_ns
        except OSError:
            pass
        if not hasattr(self, "_build_metadata_by_board"):
            self._build_metadata_by_board = {}
        self._build_metadata_by_board[board_key] = entry

    def _needs_recompile(self) -> tuple[bool, str]:
        """Return (needs_recompile, reason_string).
        False  → binary is up-to-date for the CURRENTLY selected board, safe to skip compile.
        True   → sources changed, framework missing, env just created, board never compiled,
                 or firmware binary missing from disk (e.g. cleaned manually or by board switch)."""
        if threading.get_ident() != getattr(self, "_tk_thread_id", None):
            board_name = str(getattr(self, "_active_board_name", "") or "")
        else:
            board_name = self.board_var.get()
        env_name = self._pio_env_name(board_name)

        # 1. Check if board framework is downloaded
        if not self._is_framework_downloaded(board_name):
            return True, "core framework / toolchain for board is not downloaded yet"

        # 2. Check if environment was just created
        if hasattr(self, "_just_created_envs") and (env_name in self._just_created_envs or board_name in self._just_created_envs):
            return True, "environment was just created — initial compilation required"

        # 3. Check if a compiled firmware binary actually exists on disk for this board.
        #    The hash cache may say "sources unchanged" from a previous session, but if
        #    the .pio directory was cleaned (or this is a different machine), the binary
        #    is gone and we must recompile regardless.
        if not self._has_prior_build():
            return True, "no firmware binary found for this board (build folder may have been cleaned)"

        board_key = self._board_cache_key(board_name)
        cached_hash = self._compile_cache_by_board.get(board_key)
        if cached_hash is None:
            # One-time compatibility with cache JSON written by older builds.
            cached_hash = self._compile_cache_by_board.get(board_name)
        if cached_hash is None:
            return True, "no previous compile for this board"
        current = self._hash_sources()
        if current != cached_hash:
            return True, "source files have changed since this board was last compiled"
        return False, "sources unchanged"

    def _report_project_includes(
        self,
        project_dir: Path | None = None,
        port_label: str = "",
        board_name: str = "",
    ):
        """Background task: scan includes in the new folder and print a summary."""
        project_dir = Path(project_dir or self.sketch_dir_path)
        # Metadata hiding changes Windows attributes and can turn a simple
        # folder-open operation into many remote share round trips.  Remote
        # source trees are left untouched; generated files are hidden in the
        # local staged workspace instead.
        if not is_unc_or_network_path(project_dir):
            hide_internal_project_metadata(project_dir)
        self._append("")
        self._append("=" * 50, "header")
        self._append("  📁  PROJECT LOADED", "header")
        self._append("=" * 50, "header")
        self._append(f"  Path : {project_dir}", "dim")

        # ── Network / UNC path notice ─────────────────────────────────────
        _is_network = is_unc_or_network_path(project_dir)
        if _is_network:
            _share = _unc_share_root(project_dir) or str(project_dir)
            self._append(f"  🌐 Source : Network share ({_share})", "info")
            self._append(
                "  ℹ Network paths are supported. A temporary drive mapping will "
                "be created automatically during compile/upload.",
                "dim",
            )

        # ── Drive / volume health report ──────────────────────────────────
        # Sketches may live on any volume type (NTFS, exFAT, FAT32, flash
        # drives, external disks, network shares).  Surface the facts that
        # actually affect compile/upload reliability so users on problem
        # volumes get an immediate, actionable hint instead of a cryptic
        # failure later.
        try:
            fs_name, type_label = get_volume_info(project_dir)
            writable = is_volume_writable(project_dir)
            if fs_name or type_label:
                self._append(f"  💾 Drive : {fs_name or '?'} ({type_label or '?'})", "dim")
            if not writable:
                self._append(
                    "  ✖ Volume is write-protected or read-only — compiling and "
                    "uploading will FAIL. Check the USB drive's lock switch or "
                    "its read-only status.",
                    "error",
                )
            elif fs_name.upper() in ("EXFAT", "FAT32", "FAT", "FAT16", "FAT12"):
                self._append(
                    "  ℹ Flash/external drive (FAT/exFAT) detected: builds run "
                    "slower than on an internal disk, and stale .pio caches may "
                    "need extra clean passes. This is normal for removable media.",
                    "info",
                )
            if sys.platform == "win32":
                import ctypes
                _probe = project_dir / "platformio.ini"
                if not _probe.exists():
                    _probe = project_dir
                _a = ctypes.windll.kernel32.GetFileAttributesW(str(_probe))
                if _a != -1 and (_a & 0x1000):  # FILE_ATTRIBUTE_OFFLINE
                    self._append(
                        "  ⚠ Sketch is inside a cloud-synced folder (OneDrive/"
                        "Dropbox placeholder files). Copy it to a local or USB "
                        "drive for reliable builds.",
                        "warning",
                    )
        except Exception:
            pass

        ini_path = self._platformio_ini_path(project_dir)
        if ini_path.exists():
            self._append("  ✔ platformio.ini found", "success")
        else:
            self._append("  ⚠ No platformio.ini — will be created on first compile", "warning")

        # Scan source files
        source_files = get_project_root_source_files(
            project_dir, (".ino", ".cpp", ".h", ".c", ".txt")
        )

        if not source_files:
            self._append("  ⚠ No .ino / .cpp / .h / .c / .txt files found in this folder", "warning")
            self._append("")
            return

        self._append(f"  Source files ({len(source_files)}):", "dim")
        for f in sorted(source_files):
            self._append(f"    • {f.name}", "dim")

        # Detect libraries from includes
        # Read once: on slower storage, repeatedly reopening platformio.ini
        # for every dependency is needlessly expensive.
        ini_text = ""
        if ini_path.exists():
            try:
                ini_text = ini_path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                pass

        detected = self._scan_includes_for_libs(
            project_dir=project_dir,
            board_name=board_name,
        )
        if detected:
            self._append(f"  Detected lib dependencies ({len(detected)}):", "info")
            for lib in detected:
                # Check whether each one is already listed in platformio.ini
                # Strip symlink:// prefix for comparison and display
                lib_for_check = lib.replace("symlink://", "") if lib.startswith("symlink://") else lib
                base = lib_for_check.split('/')[-1].split('@')[0].strip()
                in_ini = base.lower() in ini_text
                status_icon = "✔" if in_ini else "+"
                status_color = "success" if in_ini else "warning"
                display_lib = lib_for_check.split('/')[-1] if '/' in lib_for_check else lib_for_check
                self._append(f"    {status_icon} {display_lib}  (symlink)", status_color)
            if ini_path.exists():
                missing = [
                    lib for lib in detected
                    if lib.split('/')[-1].split('@')[0].strip().lower()
                    not in ini_text
                ]
                if missing:
                    self._append(
                        f"  ⚠ {len(missing)} dep(s) not yet in platformio.ini"
                        " — will be added automatically on compile.", "warning"
                    )
                else:
                    self._append("  ✔ All detected deps already in platformio.ini", "success")
        else:
            self._append_notif("  No known library #includes detected", "info")

        self._append("")
        self._append("  Ready. Click an action to begin.", "info")

        port_raw = port_label
        board_raw = board_name
        no_port = not port_raw or port_raw.startswith("─")
        no_board = not board_raw
        if no_port and no_board:
            self._append_notif("  ✖ No board/port selected — choose a board and plug in your device to enable Compile/Upload/Monitor.", "warning")
        elif no_port:
            self._append_notif("  ✖ No port selected — plug in your device and pick a port to enable Upload/Monitor.", "warning")
        elif no_board:
            self._append_notif("  ✖ No board selected — choose a board to enable Compile/Upload.", "warning")

        self._append("")
        self._set_status(f"Project ready — {project_dir.name}", Theme.GREEN)

    # ──────────────────────────────────────────────────────────
    # COMPILE CACHE
    # ──────────────────────────────────────────────────────────
    def _get_cache_file_path(self) -> Path:
        return get_project_temp_file(self.sketch_dir_path, ".mcu_gui_cache.json")

    @staticmethod
    def _normalize_build_config(content: str) -> str:
        """Canonicalize only PlatformIO options that can affect a binary.

        The app owns upload speed/protocol and monitor settings; changing those
        must not throw away an otherwise valid compile.  Everything else is
        retained in order (including build flags, partitions, libraries,
        extra scripts, and package pins), while comments and formatting noise
        are ignored.
        """
        normalized: list[str] = []
        skip_continuation = False
        for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith((";", "#")):
                continue
            if raw_line[:1].isspace():
                if not skip_continuation:
                    normalized.append(stripped)
                continue

            skip_continuation = False
            section_match = re.match(r"^\[([^]]+)]$", stripped)
            if section_match:
                section = section_match.group(1).strip().lower()
                # Environment names are board-specific routing identifiers,
                # not build inputs.  Their contents remain part of the hash.
                normalized.append("[env]" if section.startswith("env:") else f"[{section}]")
                continue

            option_match = re.match(r"^([^=]+?)\s*=\s*(.*)$", stripped)
            if not option_match:
                normalized.append(stripped)
                continue
            key = option_match.group(1).strip().lower()
            value = option_match.group(2).strip()
            skip_continuation = (
                key == "default_envs"
                or key.startswith("monitor_")
                or key.startswith("upload_")
                or key.startswith("board_upload.")
            )
            if not skip_continuation:
                normalized.append(f"{key}={value}")
        return "\n".join(normalized)

    def _build_config_fingerprint(self, board_name: str | None = None,
                                  allow_cached: bool = True) -> str | None:
        """Return this board's build-config digest without confusing B for A.

        The generated cache ``platformio.ini`` represents only the most recently
        prepared board.  When another board is merely selected in the UI, use
        its last successful fingerprint until preparation rewrites the file.
        A compile/upload always prepares the selected board before the final
        cache decision, so real user changes to build flags or partitions are
        still observed before any binary can be flashed.
        """
        if board_name is not None:
            name = board_name
        elif threading.get_ident() != getattr(self, "_tk_thread_id", None):
            name = str(getattr(self, "_active_board_name", "") or "")
        else:
            name = self.board_var.get()
        info = dict(SUPPORTED_BOARDS.get(name, {}))
        if not info and board_name is None:
            info = dict(self._resolve_board_info())
        ini_path = self._platformio_ini_path()
        try:
            content = ini_path.read_text(encoding="utf-8", errors="replace")
            platform_match = re.search(
                r"^\s*platform\s*=\s*([^;#\r\n]+)", content,
                re.IGNORECASE | re.MULTILINE,
            )
            board_match = re.search(
                r"^\s*board\s*=\s*([^;#\r\n]+)", content,
                re.IGNORECASE | re.MULTILINE,
            )
            expected_platform = str(info.get("platform", "")).strip().lower()
            expected_board = str(info.get("board", "")).strip().lower()
            current_platform = platform_match.group(1).strip().lower() if platform_match else ""
            current_board = board_match.group(1).strip().lower() if board_match else ""
            if (
                expected_platform
                and expected_board
                and current_platform == expected_platform
                and current_board == expected_board
            ):
                canonical = self._normalize_build_config(content)
                return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()
        except OSError:
            pass

        if allow_cached:
            cache = getattr(self, "_build_config_hash_by_board", {})
            key = self._board_cache_key(name)
            return cache.get(key) or cache.get(name)  # legacy display-name key
        return None

    def _hash_sources(self) -> str:
        """Return a single MD5 digest over the content of every source file
        in the current sketch folder.  The desired board configuration is
        included canonically; the mutable generated platformio.ini is not.

        Excluding raw platformio.ini is important for A -> B -> A switching:
        immediately after selecting A the file still describes B until the
        next PlatformIO preparation step, which used to create a false source
        change and disable A's valid cache.
        Files are sorted by name so the hash is order-stable.

        Utilizes process-wide in-memory RAM caching (_SOURCE_FILE_RAM_CACHE and
        _SOURCE_HASH_MEMO_CACHE) to avoid repetitive disk I/O and return
        memoized hashes in sub-microsecond time when sources are unchanged.
        """
        if threading.get_ident() != getattr(self, "_tk_thread_id", None):
            board_name = str(getattr(self, "_active_board_name", "") or "")
            board_info = dict(getattr(self, "_active_board_info", {}) or {})
        else:
            board_name = self.board_var.get()
            board_info = SUPPORTED_BOARDS.get(board_name, {})
        board_key = self._board_cache_key(board_name)
        try:
            board_info_str = json.dumps(board_info, sort_keys=True, default=str)
        except Exception:
            board_info_str = ""
        config_fingerprint = str(self._build_config_fingerprint(board_name) or "missing")

        native_usb = False
        try:
            if is_s3_board(str(board_info.get("board", ""))):
                native_usb = bool(self._is_native_usb_port())
        except Exception:
            pass

        sketch_dir = str(self.sketch_dir_path)
        source_files = get_project_root_source_files(
            self.sketch_dir_path, (".ino", ".cpp", ".c", ".h", ".hpp")
        )
        sorted_files = sorted(source_files)

        # Fast stat fingerprint to verify if any file on disk changed
        file_stats = []
        for f in sorted_files:
            try:
                st = f.stat()
                file_stats.append((f.name, st.st_mtime_ns, st.st_size))
            except Exception:
                file_stats.append((f.name, 0, 0))

        memo_key = (
            board_key,
            board_info_str,
            config_fingerprint,
            native_usb,
            tuple(file_stats),
        )

        with _SOURCE_FILE_RAM_CACHE_LOCK:
            memoized = _SOURCE_HASH_MEMO_CACHE.get(sketch_dir)
            if memoized is not None and memoized[0] == memo_key:
                return memoized[1]

        h = hashlib.md5()
        h.update(board_key.encode())
        if board_info_str:
            h.update(board_info_str.encode())
        h.update(b"build_config=")
        h.update(config_fingerprint.encode("ascii", errors="replace"))
        if is_s3_board(str(board_info.get("board", ""))):
            h.update(b"native_usb=" + (b"1" if native_usb else b"0"))

        for f, (fname, mtime_ns, size) in zip(sorted_files, file_stats):
            h.update(fname.encode())
            raw_bytes = None
            f_key = str(f)
            with _SOURCE_FILE_RAM_CACHE_LOCK:
                cached = _SOURCE_FILE_RAM_CACHE.get(f_key)
                if cached is not None and cached[0] == mtime_ns and cached[1] == size:
                    raw_bytes = cached[2]

            if raw_bytes is None:
                try:
                    raw_bytes = f.read_bytes()
                except Exception:
                    raw_bytes = b""
                with _SOURCE_FILE_RAM_CACHE_LOCK:
                    if len(_SOURCE_FILE_RAM_CACHE) > 500:
                        _SOURCE_FILE_RAM_CACHE.clear()
                    _SOURCE_FILE_RAM_CACHE[f_key] = (mtime_ns, size, raw_bytes)

            h.update(raw_bytes)

        digest = h.hexdigest()
        with _SOURCE_FILE_RAM_CACHE_LOCK:
            _SOURCE_HASH_MEMO_CACHE[sketch_dir] = (memo_key, digest)
        return digest


    def _set_symbol_cache_compiled_state(self, is_compiled: bool):
        """Enable or disable symbol hover/click navigation based on backend build compilation state."""
        self._project_compiled_cache_active = is_compiled
        # Sync to Monaco webview (pywebview window is self.editor_window)
        if hasattr(self, "editor_window") and self.editor_window:
            try:
                js_val = "true" if is_compiled else "false"
                self.editor_window.evaluate_js(f"window.setProjectCompiledState({js_val});")
            except Exception:
                pass

    def _update_skip_compile_state(self):
        """Auto-detect if the project was already compiled.
        If yes, enable the 'Skip recompile' checkbox and check it.
        If no, disable the checkbox and uncheck it."""
        if not hasattr(self, "cb_skip_compile") or getattr(self, "is_busy", False):
            return
        if self._has_prior_build():
            needs_recompile, reason = self._needs_recompile()
            if not needs_recompile:
                self.skip_compile_var.set(True)
                self.cb_skip_compile.configure(state=tk.NORMAL)
                return
        self.skip_compile_var.set(False)
        self.cb_skip_compile.configure(state=tk.DISABLED)

    def _has_prior_build(self) -> bool:
        """Return True if a compiled firmware binary exists for the CURRENTLY
        selected board specifically.  This check is deliberately read-only:
        restoring family-bucketed binaries could flash one board's firmware to
        another board and could also undo an explicit Clean."""
        build_dir = self._board_build_dir()
        return (
            build_dir.exists() and (
                (build_dir / "firmware.elf").exists() or
                (build_dir / "firmware.hex").exists() or
                (build_dir / "firmware.bin").exists()
            )
        )
