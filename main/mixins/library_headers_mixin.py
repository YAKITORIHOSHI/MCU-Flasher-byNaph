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

class LibraryHeadersMixin(_Base):
    """Mixin providing LibraryHeadersMixin capabilities for MCUUploadGUI."""
    def _get_installed_libraries_map(self) -> tuple[dict[str, str], dict[str, str]]:
        """Scan our downloaded libraries directory for installed libraries and header files.

        Returns
        -------
        libs_map   : normalized_name -> local install_dir path
        header_map : header_filename -> local install_dir path
        """
        libs_map = {}
        header_map = {}

        def normalize(name: str) -> str:
            return "".join(c for c in name.lower() if c.isalnum())

        download_dir = _get_download_dir()

        libs_dir = Path(download_dir) / "Libs"
        if not libs_dir.exists():
            return libs_map, header_map

        # Scan each subdirectory in Libs
        try:
            for item in libs_dir.iterdir():
                if item.is_dir():
                    # Auto-heal double nested folder if present
                    try:
                        subdirs = [p for p in item.iterdir() if p.is_dir()]
                        files = [p for p in item.iterdir() if p.is_file()]
                        if len(subdirs) == 1 and len(files) == 0:
                            nested = subdirs[0]
                            import shutil
                            for nested_item in nested.iterdir():
                                shutil.move(str(nested_item), str(item))
                            nested.rmdir()
                    except Exception:
                        pass

                    lib_path = str(item).replace("\\", "/")
                    lib_name = item.name
                    
                    # Try reading library.properties for correct library name if exists
                    props = item / "library.properties"
                    if props.exists():
                        try:
                            content = props.read_text(encoding="utf-8", errors="replace")
                            for line in content.splitlines():
                                if line.startswith("name="):
                                    lib_name = line.split("=", 1)[1].strip()
                                    break
                        except Exception:
                            pass
                            
                    slug = "symlink://" + lib_path
                    libs_map[normalize(lib_name)] = slug
                    
                    # Scan headers in the root directory and inside 'src'
                    search_dirs = [item, item / "src"]
                    seen_header_paths: set[Path] = set()
                    for s_dir in search_dirs:
                        if s_dir.exists() and s_dir.is_dir():
                            # One recursive walk per search root.  The previous
                            # nested rglob repeated the complete tree once for
                            # every root header, which was costly on low-end and
                            # removable storage.
                            for h_file in s_dir.rglob("*.h"):
                                if h_file in seen_header_paths:
                                    continue
                                seen_header_paths.add(h_file)
                                header_map[h_file.name] = slug
        except Exception as e:
            self._append(f"  ⚠ Error scanning downloaded libraries: {e}", "warning")

        old_libs = getattr(self, "_known_installed_libs", None)
        current_lib_names = set(libs_map.keys())
        if old_libs is not None:
            added_libs = current_lib_names - old_libs
            for lib_norm in added_libs:
                display_name = lib_norm.upper() if len(lib_norm) <= 5 else lib_norm.title()
                self._append_notif(
                    f"  📚 New Library Installed: \"{display_name}\"",
                    tag="success",
                    category="library_install",
                    title="Library Installed"
                )
        self._known_installed_libs = current_lib_names

        return libs_map, header_map

    def _get_core_headers(self, platform: str) -> set[str]:
        """Dynamically detect built-in headers for the selected platform
        by scanning the platform core files in the Boards directory.
        """
        core_headers = set(STANDARD_C_CPP_HEADERS) | {
            # Arduino / framework built-ins
            "arduino.h", "pins_arduino.h", "pgmspace.h",
        }

        download_dir = _get_download_dir()

        boards_path = Path(download_dir) / "Boards"
        if not boards_path.is_dir():
            return core_headers

        platform_dirs = []
        for p in boards_path.glob("**/boards.txt"):
            parent_dir = p.parent
            parent_name = parent_dir.name.lower()
            
            p_platform = None
            if "esp32" in parent_name:
                p_platform = "espressif32"
            elif "esp8266" in parent_name:
                p_platform = "espressif8266"
            elif "avr" in parent_name or "uno" in parent_name:
                p_platform = "atmelavr"
                
            if p_platform == platform:
                platform_dirs.append(parent_dir)

        for p_dir in platform_dirs:
            # 1. Scan cores/
            cores_dir = p_dir / "cores"
            if cores_dir.exists() and cores_dir.is_dir():
                for h_file in cores_dir.rglob("*.h"):
                    core_headers.add(h_file.name.lower())
                    try:
                        rel = h_file.relative_to(cores_dir)
                        core_headers.add(rel.as_posix().lower())
                    except Exception:
                        pass

            # 2. Scan libraries/
            libs_dir = p_dir / "libraries"
            if libs_dir.exists() and libs_dir.is_dir():
                for h_file in libs_dir.rglob("*.h"):
                    core_headers.add(h_file.name.lower())
                    try:
                        parts = h_file.relative_to(libs_dir).parts
                        if len(parts) > 1:
                            lib_root = libs_dir / parts[0]
                            if (lib_root / "src").exists():
                                try:
                                    core_headers.add(h_file.relative_to(lib_root / "src").as_posix().lower())
                                except Exception:
                                    pass
                            try:
                                core_headers.add(h_file.relative_to(lib_root).as_posix().lower())
                            except Exception:
                                pass
                    except Exception:
                        pass

        return core_headers

    def _scan_includes_for_libs(
        self,
        project_dir: Path | None = None,
        board_name: str | None = None,
    ) -> list[str]:
        """Scan sketch files for #include statements and resolve them against
        whatever libraries are actually installed on this machine via arduino-cli.
        No hardcoded library table — the CLI output is the single source of truth.
        """
        # Resolve board platform
        project_dir = Path(project_dir or self.sketch_dir_path)
        board_name = board_name if board_name is not None else self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        platform = board_info.get("platform", "espressif32")
        
        # Headers that are part of the core platform or standard library —
        # they never need a lib_deps entry.
        CORE_HEADERS = self._get_core_headers(platform)

        detected_libs: list[str] = []
        if not project_dir.exists():
            return detected_libs

        # Collect local project header filenames so we don't chase them as libs
        local_files: set[str] = set()
        try:
            for f in get_sketch_files_fast(project_dir):
                local_files.add(f.name.lower())
        except Exception:
            pass

        # Ask arduino-cli once for everything installed on this machine
        installed_libs_map, installed_header_map = self._get_installed_libraries_map()

        def normalize(name: str) -> str:
            return "".join(c for c in name.lower() if c.isalnum())

        for file_path in get_project_root_source_files(
            project_dir, (".ino", ".cpp", ".h", ".c")
        ):
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            for header in re.findall(r'#include\s*[<"]([^>"]+)[>"]', content):
                h_lower = header.lower()

                # Skip core/stdlib headers and local project files
                if h_lower in CORE_HEADERS:
                    continue
                if h_lower.startswith("esp_") or h_lower.startswith("driver/") or h_lower.startswith("soc/") or h_lower.startswith("hal/") or h_lower.startswith("freertos/") or h_lower.startswith("rom/") or h_lower.startswith("lwip/") or h_lower.startswith("mbedtls/"):
                    continue
                if not h_lower.endswith(".h"):
                    continue
                if h_lower in local_files:
                    continue

                # 1. Exact header match from provides_includes (most reliable)
                slug = installed_header_map.get(header)

                # 2. Normalised name match (handles case variation)
                if not slug:
                    slug = installed_libs_map.get(normalize(header[:-2]))

                if slug and slug not in detected_libs:
                    detected_libs.append(slug)

        return detected_libs

    def _verify_board_variant_exists(self, p_platform: str, p_board: str) -> tuple[bool, str]:
        """Verify the installed framework package actually contains the
        pin-mapping header (pins_arduino.h) this board needs.

        Fully dynamic: reads PlatformIO's own board JSON manifest
        (<core_dir>/platforms/<platform>/boards/<board>.json) to find the
        board's declared 'variant', then checks every installed
        framework-* package for that variant's pins_arduino.h. Nothing
        board/platform-specific is hardcoded — this works the same for
        an ESP32-S3, an ESP8266, a Cardputer, or a board that doesn't
        exist yet.

        Returns (ok, variant_name). ok=True whenever we can't positively
        confirm a problem (missing manifest, no 'variant' key, platform
        not installed yet, etc.) — this check must never block a compile
        just because it wasn't able to look something up.
        """
        try:
            core_dir_str = os.environ.get("PLATFORMIO_CORE_DIR")
            if not core_dir_str:
                return True, ""
            core_dir = Path(core_dir_str)

            board_json = core_dir / "platforms" / p_platform / "boards" / f"{p_board}.json"
            if not board_json.exists():
                return True, ""  # nothing to verify against

            data = json.loads(board_json.read_text(encoding="utf-8", errors="replace"))
            variant = (data.get("build") or {}).get("variant")
            if not variant:
                return True, ""  # this board/platform doesn't use per-board variants

            packages_dir = core_dir / "packages"
            if not packages_dir.is_dir():
                return True, ""

            # Check if the framework package for this specific platform is installed yet.
            # If not, let PlatformIO download it automatically during the first compile.
            platform_keyword = p_platform.lower()
            if platform_keyword.startswith("atmel"):
                platform_keyword = platform_keyword[5:]  # e.g., "atmelavr" -> "avr"
            
            framework_installed = False
            for d in packages_dir.glob("framework-*"):
                if platform_keyword in d.name.lower():
                    framework_installed = True
                    break
            
            if not framework_installed:
                return True, ""  # not installed yet, let PlatformIO handle it

            # Search every installed framework-* package rather than assuming
            # which specific package name provides variants for this platform.
            found = any(packages_dir.glob(f"framework-*/variants/{variant}/pins_arduino.h"))
            return (True, "") if found else (False, variant)
        except Exception:
            return True, ""  # never block a compile due to our own check failing

    def _attempt_framework_repair(self, pio_path: list[str], p_platform: str) -> bool:
        """Force PlatformIO to reinstall the given platform + framework
        package via its own CLI. p_platform is whatever the resolved
        board actually specifies — never a hardcoded platform name — so
        this repairs any corrupted/incomplete platform, not just ESP32."""
        self._append(f"  🔄 Reinstalling '{p_platform}' platform (this can take a few minutes)...", "info")
        try:
            result = subprocess.run(
                pio_path + ["platform", "install", p_platform, "--force"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            for line in (result.stdout or "").splitlines():
                if line.strip():
                    self._append(f"    {line}", "dim")
            if result.returncode != 0:
                for line in (result.stderr or "").splitlines():
                    if line.strip():
                        self._append(f"    {line}", "error")
                return False
            return True
        except Exception as e:
            self._append(f"  ✖ Repair attempt failed: {e}", "error")
            return False

    def _resolve_board_info(self) -> dict:
        """Resolve the selected display name to a canonical PlatformIO board.

        A stale/unsupported board is never silently replaced with some unrelated
        first entry.  If PlatformIO was updated after the GUI started, one live
        reload is attempted so newly-supported boards become usable immediately.
        """
        global SUPPORTED_BOARDS
        board_name = self.board_var.get()
        info = SUPPORTED_BOARDS.get(board_name)
        if info and info.get("pio_resolved", True):
            return info
        if info:
            try:
                refreshed = load_dynamic_boards({})
                refreshed_info = refreshed.get(board_name)
                if refreshed_info:
                    SUPPORTED_BOARDS = refreshed
                    if refreshed_info.get("pio_resolved", False):
                        return refreshed_info
                    info = refreshed_info
            except Exception:
                pass
            return info
        return {
            "platform": "",
            "board": "",
            "framework": "arduino",
            "pio_resolved": False,
            "resolution_error": f"Board '{board_name}' is not present in the current downloaded board catalog.",
        }

