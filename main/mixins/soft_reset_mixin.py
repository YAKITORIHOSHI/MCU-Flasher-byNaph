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
from tkinter import messagebox


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

class SoftResetMixin(_Base):
    """Mixin providing SoftResetMixin capabilities for MCUUploadGUI."""
    def _locate_esp32_boot_app0(self) -> Path | None:
        """
        Find boot_app0.bin inside PlatformIO's installed Arduino-ESP32
        framework package. Cheap directory lookup (no subprocess) used by the
        Soft Reset esptool fast path.
        """
        pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR")
        if pio_core_dir:
            pio_packages = Path(pio_core_dir) / "packages"
        else:
            local_pio = SCRIPT_DIR / "env" / ".platformio"
            pio_packages = (local_pio / "packages") if local_pio.exists() else Path.home() / ".platformio" / "packages"

        framework_dir = None
        try:
            for candidate in sorted(pio_packages.glob("framework-arduinoespressif32*"), reverse=True):
                if candidate.is_dir():
                    framework_dir = candidate
                    break
        except Exception:
            return None
        if framework_dir is None:
            return None

        tools_dir = framework_dir / "tools"
        for candidate in (tools_dir / "partitions" / "boot_app0.bin", framework_dir / "partitions" / "boot_app0.bin"):
            if candidate.exists():
                return candidate

        hits = [p for p in framework_dir.rglob("boot_app0.bin") if "variants" not in p.parts]
        return sorted(hits)[0] if hits else None

    def _locate_soft_reset_fast_binaries(self, project_dir: Path, board_name: str,
                                         p_platform: str, env_name: str = "mcu_flash",
                                         upload_speed: str = "460800",
                                         build_dir: Path | None = None,
                                         require_reset_manifest: bool = False) -> dict | None:
        """
        Check whether a previously compiled Soft Reset build is still usable
        for a direct esptool flash, skipping "pio run" entirely. Returns None
        if any required binary can't be found — the caller then falls back to
        the normal "pio run -t upload" path.
        """
        build_dir = Path(build_dir) if build_dir is not None else (
            project_dir / ".pio" / "build" / env_name
        )
        firmware_bin = build_dir / "firmware.bin"
        if not firmware_bin.exists():
            return None

        if require_reset_manifest:
            board_info = dict(SUPPORTED_BOARDS.get(board_name, {}))
            required_images = (
                ("firmware.bin",)
                if p_platform == "espressif8266"
                else ("bootloader.bin", "partitions.bin", "firmware.bin")
            )
            manifest, _manifest_error = self._validate_reset_manifest(
                project_dir, board_name, board_info, required_images
            )
            if manifest is None:
                return None

        # Check sketch source modification times (main.cpp, src/*, *.ino, *.cpp, *.h)
        # Note: We deliberately do NOT check platformio.ini mtime because platformio.ini is modified when switching boards!
        try:
            bin_mtime = firmware_bin.stat().st_mtime
            source_candidates = (
                list(project_dir.glob("src/**/*")) +
                list(project_dir.glob("*.cpp")) +
                list(project_dir.glob("*.ino")) +
                list(project_dir.glob("*.h")) +
                list(project_dir.glob("*.hpp"))
            )
            for sc in source_candidates:
                if sc.is_file() and sc.stat().st_mtime > bin_mtime:
                    return None  # source was modified after compilation -> recompile needed
        except Exception:
            pass

        if p_platform == "espressif8266":
            # ESP8266's Arduino core produces a single merged image flashed at 0x0.
            return {
                "platform": p_platform,
                "firmware": firmware_bin,
                "bootloader": None,
                "partitions": None,
                "boot_app0": None,
                "bootloader_addr": "0x0",
                "upload_speed": upload_speed,
            }

        if p_platform != "espressif32":
            return None

        bootloader_bin = build_dir / "bootloader.bin"
        partitions_bin = build_dir / "partitions.bin"
        if not (bootloader_bin.exists() and partitions_bin.exists()):
            return None

        boot_app0_bin = self._locate_esp32_boot_app0()
        if boot_app0_bin is None:
            return None

        _chip_name, bootloader_addr = self._esptool_target(
            board_name, SUPPORTED_BOARDS.get(board_name, {})
        )

        return {
            "platform": p_platform,
            "firmware": firmware_bin,
            "bootloader": bootloader_bin,
            "partitions": partitions_bin,
            "boot_app0": boot_app0_bin,
            "bootloader_addr": bootloader_addr,
            "upload_speed": upload_speed,
        }

    def _new_upload_progress_state(self, fast_bins: dict | None = None) -> dict:
        """Create image-tracking state shared by both ESP upload paths."""
        board_info = self._resolve_board_info()
        platform = (fast_bins or {}).get("platform") or board_info.get("platform", "")
        definitions = (
            [("firmware", "Firmware")]
            if platform == "espressif8266"
            else [
                ("bootloader", "Bootloader"),
                ("partitions", "Partitions"),
                ("boot_app0", "Boot App"),
                ("firmware", "Firmware"),
            ]
        )
        stages = []
        for key, label in definitions:
            path_value = (fast_bins or {}).get(key)
            path_obj = Path(path_value) if path_value else None
            stages.append({
                "key": key,
                "label": label,
                "path": path_obj,
                "basename": path_obj.name.lower() if path_obj else "",
            })
        return {
            "stages": stages,
            "active_index": 0,
            "started": False,
            "last_percent": None,
            "compressed_total": None,
        }

    @staticmethod
    def _select_upload_stage_for_source(state: dict, source: str) -> None:
        """Select an image once from its v5 path; never infer it per address."""
        source_name = Path(str(source).replace("\\", "/")).name.lower()
        aliases = {
            "bootloader.bin": "bootloader",
            "partitions.bin": "partitions",
            "boot_app0.bin": "boot_app0",
            "firmware.bin": "firmware",
        }
        wanted_key = aliases.get(source_name)
        for idx, stage in enumerate(state.get("stages") or []):
            if (stage.get("basename") and stage["basename"] == source_name
                    or wanted_key and stage.get("key") == wanted_key):
                state["active_index"] = idx
                state["last_percent"] = None
                state["compressed_total"] = None
                return

    def _consume_esptool_upload_progress(self, state: dict, line: str,
                                         before_progress=None,
                                         phase_callback=None) -> bool:
        """Consume/suppress one raw esptool image or progress line.

        Returns True when the caller should not display the raw line.
        """
        image_start = _parse_esptool_image_start(line)
        if image_start:
            self._select_upload_stage_for_source(state, image_start["source"])
            if callable(phase_callback):
                phase_callback("Writing")
            return True

        compressed = _parse_esptool_compressed(line)
        if compressed:
            state["compressed_total"] = compressed["compressed"]
            if callable(phase_callback):
                phase_callback("Writing")
            return True

        progress = _parse_esptool_write_progress(line)
        if progress:
            if callable(before_progress):
                before_progress()
            if callable(phase_callback):
                phase_callback("Writing")
            stages = state.get("stages") or [{"label": "Firmware"}]
            idx = max(0, min(int(state.get("active_index", 0)), len(stages) - 1))
            stage = stages[idx]
            self._append_upload_progress(
                stage.get("label") or "Firmware",
                idx + 1,
                len(stages),
                progress["percent"],
                progress.get("written"),
                progress.get("total"),
                force_new=not bool(state.get("started")),
            )
            state["started"] = True
            state["last_percent"] = progress["percent"]
            return True

        wrote = _parse_esptool_wrote(line)
        if wrote:
            if callable(before_progress):
                before_progress()
            if callable(phase_callback):
                phase_callback("Writing")
            stages = state.get("stages") or [{"label": "Firmware"}]
            idx = max(0, min(int(state.get("active_index", 0)), len(stages) - 1))
            stage = stages[idx]
            byte_total = wrote.get("compressed") or state.get("compressed_total")
            # Re-render 100% even if esptool already emitted it so the final
            # live row always switches from ⚡ Flashing to ✔ Flashed.
            self._append_upload_progress(
                stage.get("label") or "Firmware",
                idx + 1,
                len(stages),
                100.0,
                byte_total,
                byte_total,
                force_new=not bool(state.get("started")),
            )
            state["started"] = True
            state["last_percent"] = 100.0
            if idx < len(stages) - 1:
                state["active_index"] = idx + 1
                state["compressed_total"] = None
                state["last_percent"] = None
            return True

        return False

    @staticmethod
    def _parse_size_value(value) -> int | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        multipliers = {"k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2}
        match = re.fullmatch(r"(0x[0-9a-f]+|\d+(?:\.\d+)?)\s*(kb|mb|k|m)?", text)
        if not match:
            return None
        number = match.group(1)
        base = int(number, 16) if number.startswith("0x") else float(number)
        return int(base * multipliers.get(match.group(2) or "", 1))

    def _project_option(self, name: str) -> str | None:
        """Read one scalar option from the generated PlatformIO environment."""
        try:
            ini_path = self._platformio_ini_path()
            content = ini_path.read_text(encoding="utf-8", errors="replace")
            match = re.search(
                rf"^\s*{re.escape(name)}\s*=\s*([^;#\r\n]+)",
                content, re.IGNORECASE | re.MULTILINE,
            )
            return match.group(1).strip() if match else None
        except Exception:
            return None

    def _resolve_platform_upload_metadata(self) -> dict:
        """Resolve PlatformIO's exact board/debug/upload options locally.

        This uses the already-installed PlatformIO Python API and performs no
        network access or SCons run.  It reproduces the same dynamic protocol
        list PlatformIO would print, including S3 ``esp-builtin`` and board-
        specific onboard tools.
        """
        board_info = self._resolve_board_info()
        platform_name = board_info.get("platform", "")
        board_id = board_info.get("board", "")
        configured_debug = self._project_option("debug_tool")
        cache_key = (platform_name, board_id, configured_debug or "")
        cache = getattr(self, "_platform_upload_metadata_cache", {})
        if cache_key in cache:
            return dict(cache[cache_key])

        result = {
            "debug": None,
            "available": ["esptool"],
            "current": "esptool",
            "max_ram": None,
            "max_flash": None,
            "partitions": None,
        }
        try:
            core_dir = Path(os.environ.get("PLATFORMIO_CORE_DIR", ""))
            platform_dir = core_dir / "platforms" / platform_name
            if not platform_dir.is_dir():
                raise FileNotFoundError(platform_dir)
            # pyrefly: ignore [missing-import]
            # pyrefly: ignore [missing-import]
            from platformio.platform.factory import PlatformFactory  # type: ignore

            platform = PlatformFactory.new(str(platform_dir))
            board = platform.board_config(board_id)
            debug_tools = board.get("debug.tools", {}) or {}
            if debug_tools:
                current_debug = board.get_debug_tool_name(configured_debug)
                onboard = sorted(
                    key for key, value in debug_tools.items()
                    if (value or {}).get("onboard")
                )
                external = sorted(
                    key for key, value in debug_tools.items()
                    if not (value or {}).get("onboard")
                )
                parts = [f"DEBUG: Current ({current_debug})"]
                if onboard:
                    parts.append(f"On-board ({', '.join(onboard)})")
                if external:
                    parts.append(f"External ({', '.join(external)})")
                result["debug"] = " ".join(parts)

            protocols = set(board.get("upload.protocols", []) or [])
            protocols.add("esptool")
            result["available"] = sorted(protocols)
            result["max_ram"] = int(board.get("upload.maximum_ram_size", 0) or 0) or None
            result["max_flash"] = int(board.get("upload.maximum_size", 0) or 0) or None
            result["partitions"] = board.get("build.partitions")
        except Exception:
            # Direct flashing is still valid if metadata lookup fails. Keep a
            # truthful minimal protocol block instead of delaying/failing it.
            pass

        if not hasattr(self, "_platform_upload_metadata_cache"):
            self._platform_upload_metadata_cache = {}
        self._platform_upload_metadata_cache[cache_key] = dict(result)
        return result

    def _cached_build_metadata(self, fast_bins: dict) -> dict:
        board_name = self.board_var.get()
        board_key = self._board_cache_key(board_name)
        metadata = getattr(self, "_build_metadata_by_board", {}) or {}
        entry = dict(
            metadata.get(board_key) or metadata.get(board_name) or {}
        )
        if not entry:
            return {}
        if entry.get("env_name") and entry["env_name"] != self._pio_env_name():
            return {}
        if entry.get("source_hash"):
            try:
                if entry["source_hash"] != self._hash_sources():
                    return {}
            except Exception:
                return {}
        try:
            stat = Path(fast_bins["firmware"]).stat()
            if entry.get("firmware_size") is not None and int(entry["firmware_size"]) != stat.st_size:
                return {}
            if (entry.get("firmware_mtime_ns") is not None
                    and int(entry["firmware_mtime_ns"]) != stat.st_mtime_ns):
                return {}
        except OSError:
            return {}
        return entry

    def _partition_upload_capacity(self, fast_bins: dict,
                                   default_size: int | None,
                                   default_scheme: str | None = None) -> int | None:
        """Resolve the selected app partition size without starting SCons."""
        scheme = self._project_option("board_build.partitions") or default_scheme
        if not scheme:
            return default_size
        scheme_path = Path(scheme)
        names = [scheme_path.name]
        if not scheme_path.suffix:
            names.append(scheme_path.name + ".csv")
        candidates: list[Path] = []
        if scheme_path.is_absolute():
            candidates.append(scheme_path)
        else:
            candidates.extend(self.sketch_dir_path / name for name in names)
            boot_app0 = fast_bins.get("boot_app0")
            if boot_app0:
                candidates.extend(Path(boot_app0).parent / name for name in names)
            try:
                packages_dir = Path(os.environ.get("PLATFORMIO_CORE_DIR", "")) / "packages"
                for framework in packages_dir.glob("framework-arduinoespressif32*"):
                    candidates.extend(framework / "tools" / "partitions" / name for name in names)
                    candidates.extend(framework / "partitions" / name for name in names)
            except Exception:
                pass

        csv_path = next((path for path in candidates if path.is_file()), None)
        if csv_path is None:
            return default_size
        try:
            for raw in csv_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                fields = [field.strip() for field in line.split(",")]
                if len(fields) < 5:
                    continue
                p_type, subtype = fields[1].lower(), fields[2].lower()
                if p_type in ("0", "app") and subtype in ("factory", "ota_0"):
                    return self._parse_size_value(fields[4]) or default_size
        except Exception:
            pass
        return default_size

    @staticmethod
    def _format_platformio_memory_row(label: str, used: int, maximum: int) -> str:
        ratio = float(used) / float(maximum) if maximum else 0.0
        blocks = max(0, min(10, int(round(10 * ratio))))
        bar = ("=" * blocks).ljust(10)
        prefix = "RAM:  " if label.lower() == "ram" else "Flash:"
        return (
            f"{prefix} [{bar}] {ratio: 6.1%} "
            f"(used {int(used)} bytes from {int(maximum)} bytes)"
        )

    def _derive_build_usage_from_elf(self, fast_bins: dict,
                                     platform_meta: dict) -> dict:
        """Fallback for pre-upgrade caches: reproduce PIO's ESP size regexes."""
        elf_path = Path(fast_bins["firmware"]).with_suffix(".elf")
        if not elf_path.is_file():
            return {}
        try:
            # pyrefly: ignore [missing-import]
            # pyrefly: ignore [missing-import]
            from elftools.elf.elffile import ELFFile  # type: ignore
            with elf_path.open("rb") as handle:
                elf = ELFFile(handle)
                sections = {
                    section.name: int(section.data_size)
                    for section in elf.iter_sections()
                }
        except Exception:
            return {}

        platform_name = fast_bins.get("platform")
        if platform_name == "espressif32":
            flash_names = (
                ".iram0.text", ".iram0.vectors", ".dram0.data",
                ".flash.text", ".flash.rodata",
            )
            ram_names = (".dram0.data", ".dram0.bss", ".noinit")
        elif platform_name == "espressif8266":
            flash_names = (".text", ".data", ".rodata", ".irom0.text")
            ram_names = (".data", ".rodata", ".bss", ".noinit")
        else:
            return {}

        flash_used = sum(sections.get(name, 0) for name in flash_names)
        ram_used = sum(sections.get(name, 0) for name in ram_names)
        max_ram = platform_meta.get("max_ram")
        max_flash = self._partition_upload_capacity(
            fast_bins,
            platform_meta.get("max_flash"),
            platform_meta.get("partitions"),
        )
        result = {}
        if ram_used and max_ram:
            result["ram"] = self._format_platformio_memory_row("RAM", ram_used, max_ram)
        if flash_used and max_flash:
            result["flash"] = self._format_platformio_memory_row(
                "Flash", flash_used, max_flash
            )
        return result

    def _fast_upload_metadata_lines(self, fast_bins: dict) -> list[tuple[str, str]]:
        """Return the compact PlatformIO metadata block for direct upload."""
        cached = self._cached_build_metadata(fast_bins)
        platform_meta = self._resolve_platform_upload_metadata()
        derived = {}
        if not cached.get("ram") or not cached.get("flash"):
            derived = self._derive_build_usage_from_elf(fast_bins, platform_meta)

        rows: list[tuple[str, str]] = []
        debug_line = cached.get("debug") or platform_meta.get("debug")
        if debug_line:
            rows.append((f"  {debug_line}", "dim"))
        ram_line = cached.get("ram") or derived.get("ram")
        flash_line = cached.get("flash") or derived.get("flash")
        if ram_line:
            rows.append((f"  {ram_line}", "success"))
        if flash_line:
            rows.append((f"  {flash_line}", "success"))
        rows.append(("  Configuring upload protocol...", "dim"))
        available = platform_meta.get("available") or ["esptool"]
        rows.append((f"  AVAILABLE: {', '.join(sorted(set(available)))}", "dim"))
        rows.append(("  CURRENT: upload_protocol = esptool", "dim"))
        return rows

    def _append_fast_upload_metadata(self, fast_bins: dict) -> None:
        for text, tag in self._fast_upload_metadata_lines(fast_bins):
            self._append(text, tag)

    def _record_fast_upload_diagnostic(self, port: str, command: list[str],
                                       return_code=None, output_lines=None,
                                       error: str = "") -> None:
        """Persist hidden fast-path failures without cluttering the console."""
        try:
            log_dir = SCRIPT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "fast_upload_fallback.log"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] port={port} return_code={return_code}\n")
                handle.write("command=" + subprocess.list2cmdline([str(x) for x in command]) + "\n")
                if error:
                    handle.write(f"error={error}\n")
                for line in output_lines or []:
                    handle.write(str(line).rstrip() + "\n")
                handle.write("\n")
        except Exception:
            pass

    def _wait_for_port_reconnect(self, port: str, timeout: float = 10.0) -> bool:
        """Wait up to *timeout* seconds for a disconnected COM port to reappear.

        Re-enables the Stop button during the wait so the user can cancel.
        Returns True if the port came back, False if timed out or user stopped.
        """
        self._append("")
        self._append(
            "  ⚠ MCU disconnected — COM port is no longer available.",
            "warning",
        )
        self._append(
            f"  ⏳ Waiting {int(timeout)} seconds for MCU reconnection… "
            "Press ■ STOP to cancel.",
            "info",
        )
        # Re-enable the Stop button so the user can abort the wait.
        try:
            self.root.after(0, lambda: self.btn_stop.configure(state=tk.NORMAL))
        except Exception:
            pass

        # Signal the upload spin-loop to yield the status bar to the
        # reconnect countdown so the two don't overwrite each other.
        self._reconnect_waiting = True

        poll_interval = 0.5
        deadline = time.monotonic() + timeout
        reconnected = False
        try:
            while time.monotonic() < deadline:
                if getattr(self, "_stop_requested", False):
                    break
                remaining = max(0, int(deadline - time.monotonic()))
                self._set_status(
                    f"⏳ Waiting for MCU reconnection… ({remaining}s)",
                    Theme.YELLOW,
                )
                time.sleep(poll_interval)
                if self._is_port_present(port):
                    reconnected = True
                    self._append(
                        f"  ✔ MCU reconnected on {port} — resuming upload.",
                        "success",
                    )
                    break
        finally:
            # Always clear the flag so the upload spin-loop resumes its
            # normal status-bar updates after the reconnect window closes.
            self._reconnect_waiting = False

        if not reconnected and not getattr(self, "_stop_requested", False):
            self._append(
                f"  ✖ MCU was not reconnected within {int(timeout)} seconds.",
                "error",
            )

        # Restore the Stop button to the flash-phase disabled state.
        try:
            self.root.after(0, lambda: self.btn_stop.configure(state=tk.DISABLED))
        except Exception:
            pass

        return reconnected

    def _soft_reset_esptool_write(self, fast_bins: dict, port: str,
                                  phase_callback=None,
                                  start_attempt: int = 1) -> tuple[bool, str, int]:
        """
        Write the cached Soft Reset binaries straight to flash with esptool.
        This performs the exact same upload "pio run -t upload" would have
        done, just invoked directly so PlatformIO's project-scan overhead is
        skipped entirely. Returns (ok, err_msg, attempts_used).

        ``start_attempt`` continues an already-running 1/10-10/10 series
        (for example, after a previous connection miss). Each retry gets a
        fresh esptool session so the adapter receives the same reset pulse that
        made the legacy uploader reliable on physical BOOT-button boards.
        """
        # Use sys.executable -m esptool directly to avoid Windows setuptools
        # esptool.exe wrapper crashes when running in background subprocesses.
        esptool_cmd_base = self._get_esptool_cmd()

        # Determine target chip from authoritative PlatformIO board metadata to
        # avoid autodetect reset glitches and display-name-only mistakes.
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        chip_name, _bootloader_address = self._esptool_target(
            board_name, board_info
        )

        write_cmd = esptool_cmd_base + []
        if chip_name:
            write_cmd += ["--chip", chip_name]
        write_cmd += [
            "--port", port,
            "--baud", str(fast_bins.get("upload_speed") or "460800"),
            "--before", str(fast_bins.get("before") or "default-reset"),
            # Do not let esptool perform the final reset itself. On some Windows
            # USB-serial drivers the flash and verification finish successfully,
            # but the post-write RTS operation returns a non-zero exit code. The
            # caller already reopens the Serial Monitor with a controlled DTR/RTS
            # pulse, which both resets the MCU and captures setup() output.
            "--after", "no-reset",
            # Let each app-level retry own one esptool sync session.  This is
            # important for boards that need a fresh DTR/RTS pulse after a
            # missed BOOT window; a single endless esptool process cannot
            # re-arm those boards.
            "--connect-attempts", "1",
            "write-flash",
            "--flash-mode", "keep",
            "--flash-freq", "keep",
            "--flash-size", "detect",
        ]

        if fast_bins["platform"] == "espressif32":
            write_cmd += [
                fast_bins["bootloader_addr"], str(fast_bins["bootloader"]),
                "0x8000", str(fast_bins["partitions"]),
                "0xe000", str(fast_bins["boot_app0"]),
                "0x10000", str(fast_bins["firmware"]),
            ]
        else:
            # espressif8266 — single merged image at 0x0
            write_cmd += ["0x0", str(fast_bins["firmware"])]

        # Keep the generous legacy window.  Some USB-serial adapters and
        # manually-held BOOT buttons take several seconds before the ROM
        # answers, and the app-level retry below still refreshes the reset
        # pulse between attempts.
        _WATCHDOG_SECS = 90
        _MAX_CONNECT_RETRIES = UPLOAD_CONNECTION_ATTEMPTS
        _connect_retry_count = max(0, start_attempt - 1)
        _CONNECT_FAIL_SIGNATURES = (
            "wrong boot mode", "failed to connect",
            "no serial data received", "timed out waiting for packet",
            "device not found", "permissionerror", "access is denied",
            "port is busy", "could not open port", "permission denied",
            "connection timed out", "timed out after", "not responding",
            "no more data to read from the serial port",
            "a device attached to the system is not functioning",
        )

        chip_info: dict[str, str] = {}
        chip_info_shown = False
        fast_metadata_shown = False
        connected_bar_flipped = False
        upload_started = time.perf_counter()
        self._last_fast_upload_failure_kind = ""
        connection_poll_count = [max(1, min(_MAX_CONNECT_RETRIES, start_attempt))]
        baud_recovery_used = False
        recovery_baud: str | None = None

        def _set_fast_phase(name: str):
            if callable(phase_callback):
                try:
                    phase_callback(name)
                except Exception:
                    pass

        def _show_fast_chip_info(force: bool = False):
            nonlocal chip_info_shown
            if chip_info_shown:
                return
            model = chip_info.get("Chip Model")
            if not model and not force:
                return
            model = model or self.board_var.get()
            fields = dict(chip_info)
            if fields.get("Features"):
                fields["Features"] = _enrich_chip_features(model, fields["Features"])
            self._print_chip_info_box(model, list(fields.items()))
            chip_info_shown = True

        def _show_fast_context(force: bool = False):
            nonlocal fast_metadata_shown
            if fast_metadata_shown:
                return
            _show_fast_chip_info(force=force)
            if chip_info_shown:
                self._append_fast_upload_metadata(fast_bins)
                fast_metadata_shown = True

        def _flip_fast_connected_bar():
            nonlocal connected_bar_flipped
            if connected_bar_flipped:
                return
            connected_bar_flipped = True
            self._append_connecting_progress(
                connection_poll_count[0], _MAX_CONNECT_RETRIES, connected=True
            )

        def _before_fast_progress():
            _flip_fast_connected_bar()
            _show_fast_context(force=True)

        def _terminate_attempt(proc):
            """Stop one stuck esptool attempt without setting the user stop flag."""
            if not proc or proc.poll() is not None:
                return
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        def _handle_fast_line(raw_line: str):
            """Consume one esptool line while keeping the existing progress UI."""
            nonlocal attempt_connected, verified_images
            stripped = raw_line.rstrip()
            if not stripped:
                return
            output_lines.append(stripped)
            low = stripped.lower()

            match = re.search(
                r"chip (?:is|type)\s*:?\s+(.+)$", stripped, re.IGNORECASE
            )
            if match:
                chip_info["Chip Model"] = match.group(1).strip()
            match = re.search(r"features\s*:\s*(.+)$", stripped, re.IGNORECASE)
            if match:
                chip_info["Features"] = match.group(1).strip()
            match = re.search(
                r"crystal (?:is|frequency)\s*:?\s+(.+)$",
                stripped, re.IGNORECASE,
            )
            if match:
                chip_info["Crystal"] = match.group(1).strip()
            match = re.search(r"^\s*mac\s*:\s*(.+)$", stripped, re.IGNORECASE)
            if match:
                chip_info["MAC Address"] = match.group(1).strip()
            match = re.search(
                r"(?:auto-detected\s+)?flash size\s*:\s*(.+)$",
                stripped, re.IGNORECASE,
            )
            if match:
                chip_info["Flash Size"] = match.group(1).strip()
            if "connecting" in low:
                _set_fast_phase("Connecting")
                self._append_connecting_progress(
                    connection_poll_count[0], _MAX_CONNECT_RETRIES
                )
            if ("connected to" in low or "uploading stub" in low
                    or "stub flasher running" in low):
                attempt_connected = True
                _set_fast_phase("Connecting")
                _flip_fast_connected_bar()
            if "will be erased" in low or "erasing flash" in low:
                attempt_connected = True
                _set_fast_phase("Erasing")

            wrote_event = _parse_esptool_wrote(stripped)
            if wrote_event:
                stages = upload_progress_state.get("stages") or []
                idx = max(0, min(
                    int(upload_progress_state.get("active_index", 0)),
                    max(0, len(stages) - 1),
                ))
                if stages:
                    completed_images.add(str(stages[idx].get("key") or idx))
                attempt_connected = True

            if "hash of data verified" in low:
                verified_images += 1
                attempt_connected = True

            if self._consume_esptool_upload_progress(
                    upload_progress_state, stripped,
                    before_progress=_before_fast_progress,
                    phase_callback=_set_fast_phase):
                return
            if "verifying written data" in low or "hash of data verified" in low:
                _set_fast_phase("Verifying")
            if "hard resetting" in low:
                _set_fast_phase("Resetting")

        self._append_connecting_progress(start_attempt, _MAX_CONNECT_RETRIES, force_new=True)

        while True:
            output_lines = []
            upload_progress_state = self._new_upload_progress_state(fast_bins)
            attempt_connected = False
            completed_images: set[str] = set()
            verified_images = 0
            expected_image_count = len(upload_progress_state.get("stages") or [])
            timed_out = False
            verification_complete_seen = False
            try:
                if _connect_retry_count == max(0, start_attempt - 1):
                    self._append(
                        "  💡 Hold BOOT now — the uploader will keep polling the bootloader.",
                        "info",
                    )
                    # Give the user a stable arming window before the first
                    # DTR/RTS pulse. Retries do not pause this long again.
                    time.sleep(0.75)
                # Each connection retry gets its own uploader process so the
                # --before action can issue a fresh DTR/RTS reset pulse. Once
                # connected, this process writes the complete image set at the
                # selected baud rate without PlatformIO starting another scan.
                attempt_cmd = list(write_cmd)
                if recovery_baud:
                    try:
                        baud_idx = attempt_cmd.index("--baud") + 1
                        attempt_cmd[baud_idx] = recovery_baud
                    except (ValueError, IndexError):
                        pass
                self.process = subprocess.Popen(
                    attempt_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                proc = self.process
                _start = time.time()
                output_queue: queue.Queue = queue.Queue()

                def _read_fast_output():
                    try:
                        for raw_line in iter(proc.stdout.readline, ""):
                            output_queue.put(raw_line)
                    finally:
                        output_queue.put(None)

                threading.Thread(target=_read_fast_output, daemon=True).start()
                reader_done = False
                while not reader_done:
                    try:
                        line = output_queue.get(timeout=0.10)
                    except queue.Empty:
                        if (time.time() - _start > _WATCHDOG_SECS
                                and not attempt_connected
                                and not completed_images):
                            timed_out = True
                            self._append(
                                f"  ⚠ Bootloader connection timed out after {_WATCHDOG_SECS}s — retrying.",
                                "warning",
                            )
                            _terminate_attempt(proc)
                            break
                        continue
                    if line is None:
                        reader_done = True
                        continue
                    _handle_fast_line(line)
                    if (
                        expected_image_count > 0
                        and len(completed_images) >= expected_image_count
                        and verified_images >= expected_image_count
                    ):
                        # esptool has already written and hash-verified every
                        # image. Some Windows serial drivers keep stdout open
                        # while esptool performs its final no-reset cleanup;
                        # waiting for EOF here added 10–12 seconds after the
                        # actual flash was finished.
                        verification_complete_seen = True
                        break
                verified_before_wait = (
                    verification_complete_seen
                    or (
                        expected_image_count > 0
                        and len(completed_images) >= expected_image_count
                        and verified_images >= expected_image_count
                    )
                )
                try:
                    # Once every image has reported a verified hash, the flash
                    # is complete even if a Windows esptool child lingers while
                    # closing stdout. Do not make the user wait the old 10s
                    # process-cleanup timeout after a successful write.
                    proc.wait(timeout=1.5 if verified_before_wait else (3 if timed_out else 10))
                except subprocess.TimeoutExpired:
                    if verified_before_wait:
                        self._append(
                            "  ℹ Flash verified; finalizing the uploader process.",
                            "dim",
                        )
                    else:
                        self._append("  ⚠ Process did not exit cleanly — force killing.", "warning")
                    _terminate_attempt(proc)
                    proc.wait(timeout=3)

                rc = proc.returncode
                joined = " ".join(line.rstrip().lower() for line in output_lines)
                all_images_written = (
                    expected_image_count > 0
                    and len(completed_images) >= expected_image_count
                )
                all_images_verified = (
                    all_images_written
                    and verified_images >= expected_image_count
                )
                # Retry only a genuine pre-flash sync failure. Once the chip has
                # connected or any image has been written, a later non-zero exit
                # is a flash/post-flash error and must never be relabeled as
                # "failed to connect".
                is_conn_failure = (
                    rc != 0
                    and not attempt_connected
                    and not completed_images
                    and (
                        timed_out
                        or any(sig in joined for sig in _CONNECT_FAIL_SIGNATURES)
                    )
                )

                if is_conn_failure:
                    if (_connect_retry_count < _MAX_CONNECT_RETRIES - 1
                            and not getattr(self, "_stop_requested", False)):
                        # A fresh process is intentional: esptool's
                        # ``default-reset`` sequence sends a new DTR/RTS pulse
                        # before every retry. This is the behavior used by the
                        # known-good uploader and is required by boards whose
                        # first BOOT window was missed.
                        _connect_retry_count += 1
                        connection_poll_count[0] = _connect_retry_count + 1
                        self._append_connecting_progress(
                            connection_poll_count[0], _MAX_CONNECT_RETRIES
                        )
                        port_reenumerating = any(
                            token in joined for token in (
                                "no more data to read from the serial port",
                                "a device attached to the system is not functioning",
                                "could not open port", "port is busy",
                            )
                        )
                        if port_reenumerating and not self._is_port_present(port):
                            # Port physically gone — MCU was unplugged.
                            if not self._wait_for_port_reconnect(port):
                                error_message = (
                                    "MCU disconnected during upload "
                                    f"({port} is no longer available)"
                                )
                                if getattr(self, "_stop_requested", False):
                                    self._append(
                                        "  ■ Upload cancelled by user during reconnection wait.",
                                        "warning",
                                    )
                                self._last_fast_upload_failure_kind = "flash"
                                self._record_fast_upload_diagnostic(
                                    port, attempt_cmd, return_code=rc,
                                    output_lines=output_lines,
                                    error=error_message,
                                )
                                return False, error_message, _connect_retry_count + 1
                        elif port_reenumerating:
                            self._append(
                                "  ℹ COM port is resetting — waiting briefly before the next BOOT pulse…",
                                "dim",
                            )
                        # USB serial drivers need longer than a simple sync miss
                        # to release/re-enumerate after an unexpected reset.
                        time.sleep(0.75 if port_reenumerating else 0.25)
                        continue
                    connection_poll_count[0] = _MAX_CONNECT_RETRIES

                # Once esptool has synced with the chip, any non-zero exit
                # before every image is verified is a transport/flash-session
                # loss from the app's point of view. Do not depend only on one
                # wording variant from esptool v4/v5; some drivers close the
                # stream before the final diagnostic line is delivered.
                post_connect_transport_failure = (
                    rc != 0
                    and attempt_connected
                    and not all_images_verified
                )
                if (rc != 0 and attempt_connected
                        and not baud_recovery_used
                        and str(fast_bins.get("upload_speed") or "460800") != "115200"
                        and post_connect_transport_failure
                        and not getattr(self, "_stop_requested", False)):
                    # Before attempting baud recovery, check if the port
                    # has physically vanished (MCU unplugged). A lower baud
                    # rate cannot help a missing device.
                    if not self._is_port_present(port):
                        if not self._wait_for_port_reconnect(port):
                            error_message = (
                                "MCU disconnected during upload "
                                f"({port} is no longer available)"
                            )
                            if getattr(self, "_stop_requested", False):
                                self._append(
                                    "  ■ Upload cancelled by user during reconnection wait.",
                                    "warning",
                                )
                            self._last_fast_upload_failure_kind = "flash"
                            self._record_fast_upload_diagnostic(
                                port, attempt_cmd, return_code=rc,
                                output_lines=output_lines,
                                error=error_message,
                            )
                            return False, error_message, _connect_retry_count + 1
                    # A high-speed USB-serial bridge can lose bytes after the
                    # board has already entered the ROM loader. Rewriting the
                    # complete image at 115200 is safe and far more reliable
                    # than handing a partially flashed board to a second
                    # PlatformIO process. This recovery is deliberately one
                    # shot so a bad cable cannot create an endless loop.
                    baud_recovery_used = True
                    recovery_baud = "115200"
                    self._append(
                        "  ⚠ Serial data stopped during the high-speed flash; retrying once at 115200 baud…",
                        "warning",
                    )
                    time.sleep(0.35)
                    continue

                ok = (rc == 0)
                if ok or all_images_verified:
                    _flip_fast_connected_bar()
                    _show_fast_context(force=True)
                    _set_fast_phase("Done")
                    self._append("")
                    if rc != 0:
                        self._append(
                            "  ⚠ Esptool returned an error after every image was written "
                            "and hash-verified. Treating the upload as successful.",
                            "warning",
                        )
                        self._record_fast_upload_diagnostic(
                            port, attempt_cmd, return_code=rc,
                            output_lines=output_lines,
                            error="post-flash exit after all images verified",
                        )
                    self._append(
                        "  ✔ Flash write and verification completed. "
                        "Reset will continue through the Serial Monitor.",
                        "success",
                    )
                    elapsed = max(0.0, time.perf_counter() - upload_started)
                    self._last_fast_upload_elapsed = elapsed
                    self._append(
                        f"  {'=' * 25} [SUCCESS] Took {elapsed:.2f} seconds {'=' * 25}",
                        "success",
                    )
                    self._last_fast_upload_failure_kind = ""
                    return True, "", _connect_retry_count + 1
                detail = next(
                    (line.strip() for line in reversed(output_lines)
                     if line.strip() and not line.lower().startswith("hint:")),
                    "esptool exited with a non-zero status",
                )
                detail = detail[:300]
                if timed_out:
                    error_message = f"esptool connection timed out after {_WATCHDOG_SECS}s"
                else:
                    error_message = f"esptool exit code {rc}: {detail}"
                if is_conn_failure:
                    self._last_fast_upload_failure_kind = "connection"
                elif attempt_connected or completed_images:
                    self._last_fast_upload_failure_kind = "flash"
                else:
                    self._last_fast_upload_failure_kind = "tool"
                self._record_fast_upload_diagnostic(
                    port, attempt_cmd, return_code=rc,
                    output_lines=output_lines, error=error_message,
                )
                return False, error_message, _connect_retry_count + 1
            except Exception as e:
                error_message = str(e)
                self._last_fast_upload_failure_kind = "tool"
                self._record_fast_upload_diagnostic(
                    port, write_cmd, return_code=None,
                    output_lines=[], error=error_message,
                )
                return False, error_message, _connect_retry_count + 1

    def _do_soft_reset(self, parent_dlg):
        # 0. Block if the board is already running the soft-reset sketch
        if getattr(self, '_soft_reset_sketch_active', False):
            from tkinter import messagebox
            messagebox.showwarning(
                "Already Reset",
                "The board is already running the soft-reset sketch.\n\n"
                "There is nothing to reset — upload your very own project sketch to continue.",
                parent=parent_dlg
            )
            return

        # 1. Check if busy (with auto-recovery for stale busy flag)
        if self.is_busy:
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._append("  ℹ Stale busy state cleared — proceeding.", "info")
            else:
                from tkinter import messagebox
                messagebox.showwarning("Busy", "The programmer is currently busy with another operation.", parent=parent_dlg)
                return

        if not self.board_var.get():
            from tkinter import messagebox
            messagebox.showwarning(
                "No Board Selected",
                "Please choose a board in the main window before performing a Soft Reset.",
                parent=parent_dlg
            )
            return

        port = self._get_port()
        if not port:
            from tkinter import messagebox
            messagebox.showwarning("No Port Selected", "Please select a serial port in the main window before resetting.", parent=parent_dlg)
            return

        if not self._is_board_recognized():
            from tkinter import messagebox
            messagebox.showwarning(
                "Board Not Recognized",
                "The board on this port hasn't been recognized yet.\n\n"
                "Wait for auto-detect to finish, or verify the correct board/port is selected.",
                parent=parent_dlg
            )
            return

        if not self._is_port_present(port):
            from tkinter import messagebox
            messagebox.showwarning(
                "Port Disconnected",
                f"The selected port '{port}' is not connected.\n\n"
                "Please connect your MCU physical board before resetting.",
                parent=parent_dlg
            )
            return

        # Close settings dialog to let the process start
        parent_dlg.destroy()

        self._clear_console_if_action_enabled()

        # Run soft reset in background thread
        self._active_reset_kind = "soft"
        self.is_busy = True
        self._set_buttons_state(True, operation="reset")
        threading.Thread(target=self._run_soft_reset, args=(port,), daemon=True).start()

    def _run_soft_reset(self, port: str):
        reset_cache_lock = _try_acquire_reset_cache_lock()
        try:
            if reset_cache_lock is None:
                self._append(
                    "  ⚠ Soft Reset blocked: another window is using the shared reset cache.",
                    "warning",
                )
                self._set_status("Soft Reset blocked — reset cache is in use", Theme.YELLOW)
                return
            self._run_soft_reset_inner(port)
        except Exception as e:
            import traceback
            try:
                err_log = SCRIPT_DIR / "logs" / "error_log.txt"
                err_log.parent.mkdir(parents=True, exist_ok=True)
                with open(err_log, "w", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
            except Exception:
                pass
            self._append(f"  ✖ Internal error in soft reset thread: {e}", "error")
            self._set_status("Soft Reset FAILED", Theme.RED)
        finally:
            _release_reset_cache_lock(reset_cache_lock)
            # Guarantee busy state is always cleared
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_window_closable(True)

    def _run_soft_reset_inner(self, port: str):
        owner_pid = port_occupied_owner(port)
        if owner_pid:
            self._append(f"  ⚠ Soft reset blocked: Port '{port}' is in use by another window (PID {owner_pid}).", "warning")
            return
        # Pause monitor (so port isn't blocked)
        was_monitoring = self._pause_monitor()
        if was_monitoring:
            time.sleep(0.4)

        board_name = self.board_var.get()
        if board_name in SUPPORTED_BOARDS:
            board_info = SUPPORTED_BOARDS[board_name]
        elif SUPPORTED_BOARDS:
            board_info = next(iter(SUPPORTED_BOARDS.values()))
        else:
            board_info = {"platform": "atmelavr", "board": "uno", "framework": "arduino"}
        p_platform = board_info["platform"]
        p_board = board_info["board"]
        p_framework = board_info["framework"]
        is_avr = p_platform == "atmelavr"

        self._append("")
        self._append("=" * 50, "header")
        self._append(
            "  🔄 SOFT RESET (Arduino UNO Minimal Sketch)"
            if is_avr else "  🔄 SOFT RESET (Clearing Flash Memory)",
            "header",
        )
        self._append("=" * 50, "header")
        self._append(f"  Port  : {port}", "port_highlight")
        self._append(f"  Board : {board_name}", "dim")
        if self._detect_port_chip() is None and not getattr(self, "_board_port_confirmed", False):
            self._append("  ⚠ Unrecognized USB-serial port — proceeding anyway.", "warning")

        # Desktop vs Laptop tip
        try:
            # pyrefly: ignore [missing-import]
            from detector import is_laptop
            system_is_laptop = is_laptop()
        except Exception:
            system_is_laptop = False

        if not system_is_laptop and p_platform in ("espressif32", "espressif8266"):
            self._append("  💡 Tip: On Desktop PCs, some ESP modules may need BOOT held during connection.", "dim")
        elif is_avr:
            self._append("  ℹ Arduino UNO uses automatic DTR reset; do not hold a BOOT button.", "dim")

        self._append("")
        self._set_status("Soft Reset: Initializing...", Theme.YELLOW)

        pio_path = find_pio_executable()
        if not pio_path:
            self._append("  ✖ PlatformIO executable not found!", "error")
            self._append("  Try manually: pip install platformio", "info")
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_status("Soft Reset: Failed", Theme.RED)
            if was_monitoring:
                self._resume_monitor()
            from tkinter import messagebox
            self.root.after(0, lambda: messagebox.showerror(
                "Soft Reset Error",
                "PlatformIO not found! Could not perform soft reset.",
                parent=self.root,
            ))
            return

        # ── Use an exact-board persistent reset project ────────────────────────
        # A/B/A board switching returns to A's own .pio tree instead of
        # rewriting and deleting the last board's shared reset build.
        import shutil

        self._set_status("Soft Reset: Preparing project files...", Theme.YELLOW)
        project_dir = self._soft_reset_project_dir(board_name, board_info)
        self._migrate_legacy_reset_project(board_name, board_info)
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
            hide_generated_directory(project_dir.parent)
            hide_generated_directory(project_dir)
        except Exception as e:
            self._append(f"  ✖ Failed to create soft reset project directory:\n  {e}", "error")
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_status("Soft Reset: Failed", Theme.RED)
            if was_monitoring:
                self._resume_monitor()
            from tkinter import messagebox
            self.root.after(0, lambda e=e: messagebox.showerror(
                "Soft Reset Error",
                f"Failed to create project directory:\n{e}",
                parent=self.root,
            ))
            return

        # Hard and Soft Reset deliberately use the same byte-identical project
        # template so either operation warms this exact board's cache.
        ini_content, cpp_content, monitor_speed = self._reset_project_contents(
            board_name, board_info
        )

        if is_avr:
            # Force the GUI monitor to the same baud before it reconnects.
            self.baud_var.set(monitor_speed)
            if hasattr(self, "serial_baud_var"):
                self.serial_baud_var.set(monitor_speed)

        # ── Board-aware caching: only rewrite files if content changed ─────────
        # When the board changes, the ini_content changes → we detect that,
        # clear the build cache, and rewrite.  Otherwise we skip writing
        # entirely and PlatformIO will see "nothing changed" → upload only.
        ini_path = project_dir / "platformio.ini"
        cpp_path = project_dir / "main.cpp"
        files_changed = False
        try:
            existing_ini = ini_path.read_text(encoding="utf-8") if ini_path.exists() else ""
            existing_cpp = cpp_path.read_text(encoding="utf-8") if cpp_path.exists() else ""

            if existing_ini != ini_content or existing_cpp != cpp_content:
                files_changed = True
                env_build_dir = project_dir / ".pio" / "build" / "mcu_flash"
                is_first_time = not env_build_dir.exists()
                self._force_write_text(ini_path, ini_content)
                self._force_write_text(cpp_path, cpp_content)
                if is_first_time:
                    self._append("  🔧 First-time setup for this board — this may take a minute. Subsequent Soft Resets will be instant.", "warning")
                else:
                    self._append("  ✔ Reset project updated; its existing incremental objects were preserved.", "dim")
            else:
                self._append("  ✔ Using cached build (no recompilation needed).", "success")
        except Exception as e:
            self._append(f"  ✖ Failed to write project files:\n  {e}", "error")
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_status("Soft Reset: Failed", Theme.RED)
            if was_monitoring:
                self._resume_monitor()
            from tkinter import messagebox
            self.root.after(0, lambda e=e: messagebox.showerror(
                "Soft Reset Error",
                f"Failed to write project files:\n{e}",
                parent=self.root,
            ))
            return

        # Run parallel build + upload
        jobs = self._get_cpu_cores_jobs()
        reset_env = self._reset_platformio_subprocess_env(project_dir, jobs)

        # ── FAST PATH: nothing changed + ESP32/ESP8266 board ────────────────────
        # "pio run -t upload" always pays PlatformIO's SCons project-scan cost
        # (dependency graph, board/package checks, timestamp hashing) even when
        # there's nothing to compile — for an "almost empty sketch" that scan is
        # the whole delay, not the upload itself. When the cached build is still
        # valid we skip PlatformIO entirely and hand the already-compiled
        # binaries straight to esptool, the same way Hard Reset already does for
        # the bootloader burn.
        is_esp = p_platform in ("espressif32", "espressif8266")
        fast_bins = None
        if not files_changed and is_esp:
            fast_bins = self._locate_soft_reset_fast_binaries(
                project_dir, board_name, p_platform,
                require_reset_manifest=True,
            )

        # ── Spinner thread for Soft Reset ──────────────────────────────────────
        _sr_state   = ["Uploading" if not files_changed else "Compiling"]

        if fast_bins is None:
            # Check if platform is already installed to notice the user
            pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR", "")
            platform_installed = False
            if pio_core_dir:
                platform_dir = Path(pio_core_dir) / "platforms" / p_platform
                if platform_dir.exists() and any(platform_dir.iterdir()):
                    packages_dir = Path(pio_core_dir) / "packages"
                    if packages_dir.exists() and any(packages_dir.iterdir()):
                        platform_installed = True
            
            if not platform_installed:
                self._append("", "")
                self._append("  ────────────────────────────────────────────────────────────────────────────", "warning")
                self._append("  ⚠ Preparing/Installing the required framework and toolchain...", "warning")
                self._append("    This is a first-time setup for board framework '" + p_platform + "'.", "info")
                if is_avr:
                    self._append("    Arduino AVR packages are being prepared for UNO compilation/upload.", "info")
                else:
                    self._append("    ESP framework packages can be large and may take several minutes.", "info")
                self._append("    Please keep the application open until setup completes.", "warning")
                self._append("  ────────────────────────────────────────────────────────────────────────────", "warning")
                self._append("", "")
                _sr_state[0] = "Preparing/Installing Framework"

        self._append(
            "  🔨 Uploading Arduino UNO reset sketch at 9600 baud..."
            if is_avr else "  🔨 Resetting Flash Memory...",
            "info",
        )

        img_count = 0
        output_lines = []
        ok = False
        err_msg = ""
        _sr_active  = [True]
        _sr_frame   = [0]
        _sr_start   = time.time()
        _sr_spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

        def _sr_spin_loop():
            while _sr_active[0] and self.is_busy:
                if str(_sr_state[0]).startswith("Connecting") or getattr(self, "_reconnect_waiting", False):
                    time.sleep(0.2)
                    continue
                elapsed = int(time.time() - _sr_start)
                frame   = _sr_spinner[_sr_frame[0] % len(_sr_spinner)]
                _sr_frame[0] += 1
                self._set_status(
                    f"{frame} Soft Reset: {_sr_state[0]}... ({elapsed}s)",
                    Theme.YELLOW,
                )
                time.sleep(0.2)

        import threading as _threading_sr
        _sr_spin_thread = _threading_sr.Thread(target=_sr_spin_loop, daemon=True)
        _sr_spin_thread.start()

        # Paths PlatformIO reported as undeletable (WinError 145) — auto-
        # retried after the process exits so no manual intervention needed.
        _stale_clean_paths: list[str] = []

        _MAX_CONNECT_RETRIES = UPLOAD_CONNECTION_ATTEMPTS
        _connect_retry_count = 0
        _CONNECT_FAIL_SIGNATURES = (
            "wrong boot mode", "failed to connect",
            "no serial data received", "timed out waiting for packet",
            "device not found", "permissionerror", "access is denied",
            "port is busy", "could not open port", "permission denied",
            "connection timed out", "timed out after", "not responding",
        )

        if fast_bins is not None:
            # ── Fast path: write cached binaries directly with esptool ─────────
            self._append("  ⚡ Cached build found — flashing directly with esptool (skipping PlatformIO).", "success")
            ok, err_msg, _fast_attempts_used = self._soft_reset_esptool_write(fast_bins, port)
        else:
            cmd = pio_path + [
                "run",
                "-t", "upload",
                "-j", str(jobs),
                "--upload-port", port
            ]
            chip_info: dict[str, str] = {}
            chip_info_shown = False
            connected_bar_flipped = False
            upload_progress_state = self._new_upload_progress_state(None)

            def _flip_sr_connected_bar():
                nonlocal connected_bar_flipped
                if not is_avr and not connected_bar_flipped:
                    connected_bar_flipped = True
                    self._append_connecting_progress(
                        _connect_retry_count + 1, _MAX_CONNECT_RETRIES, connected=True
                    )

            def _show_sr_chip_info(force: bool = False):
                nonlocal chip_info_shown
                if chip_info_shown:
                    return
                model = chip_info.get("Chip Model")
                if not model and not force:
                    return
                model = model or board_name
                fields = dict(chip_info)
                if fields.get("Features"):
                    fields["Features"] = _enrich_chip_features(model, fields["Features"])
                self._print_chip_info_box(model, list(fields.items()))
                chip_info_shown = True

            def _before_sr_progress():
                _flip_sr_connected_bar()
                _show_sr_chip_info(force=True)

            while True:
                output_lines.clear()
                try:
                    self.process = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, encoding="utf-8", errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                        cwd=str(project_dir),
                        env=reset_env,
                    )
                    for line in iter(self.process.stdout.readline, ""):
                        stripped = line.rstrip()
                        if not stripped:
                            continue
                        output_lines.append(stripped)
                        low = stripped.lower()

                        if "compiling" in low or "building" in low:
                            _sr_state[0] = "Compiling"
                        elif any(kw in low for kw in ("tool manager:", "platform manager:", "downloading", "unpacking", "installing")):
                            _sr_state[0] = "Preparing/Installing Framework"

                        # Parse chip info fields if emitted by esptool
                        match = re.search(r"chip (?:is|type)\s*:?\s+(.+)$", stripped, re.IGNORECASE)
                        if match:
                            chip_info["Chip Model"] = match.group(1).strip()
                        match = re.search(r"features\s*:\s*(.+)$", stripped, re.IGNORECASE)
                        if match:
                            chip_info["Features"] = match.group(1).strip()
                        match = re.search(r"crystal (?:is|frequency)\s*:?\s+(.+)$", stripped, re.IGNORECASE)
                        if match:
                            chip_info["Crystal"] = match.group(1).strip()
                        match = re.search(r"^\s*mac\s*:\s*(.+)$", stripped, re.IGNORECASE)
                        if match:
                            chip_info["MAC Address"] = match.group(1).strip()
                        match = re.search(r"(?:auto-detected\s+)?flash size\s*:\s*(.+)$", stripped, re.IGNORECASE)
                        if match:
                            chip_info["Flash Size"] = match.group(1).strip()

                        if ("connected to" in low or "uploading stub" in low
                                or "stub flasher running" in low):
                            _sr_state[0] = "Connecting"
                            _flip_sr_connected_bar()
                            _show_sr_chip_info()

                        LINKER_ERROR_HINTS = (
                            "undefined reference to",
                            "multiple definition of",
                            "cannot find -l",
                            "undefined symbol",
                            "duplicate symbol",
                            "ld returned",
                            "collect2",
                        )
                        is_linker_error = any(hint in low for hint in LINKER_ERROR_HINTS)

                        if is_nonfatal_pio_clean_report(stripped):
                            self._append(f"  ⚠ {stripped}", "warning")
                            self._capture_stale_clean_path(stripped, _stale_clean_paths)
                        elif is_linker_error:
                            self._append(f"  ✖ {stripped}", "error")
                        elif "error" in low and "werror" not in low:
                            is_conn_sig = any(sig in low for sig in _CONNECT_FAIL_SIGNATURES) or "fatal error occurred" in low or "error 2" in low
                            can_retry = (_connect_retry_count < _MAX_CONNECT_RETRIES - 1 and not getattr(self, "_stop_requested", False))
                            if not (is_conn_sig and can_retry):
                                self._append(f"  ✖ {stripped}", "error")
                        elif "warning" in low:
                            self._append(f"  ⚠ {stripped}", "warning")
                        elif "connecting" in low:
                            _sr_state[0] = "Connecting"
                            self._append_connecting_progress(_connect_retry_count + 1, _MAX_CONNECT_RETRIES)
                        elif "successfully created" in low and "image" in low:
                            chip_match = re.search(r'successfully created (\w+) image', low)
                            chip_name = chip_match.group(1).upper() if chip_match else "MCU"
                            img_count += 1
                            label = "Bootloader" if img_count == 1 else "Application"
                            self._append(f"  ✔ Successfully created {chip_name} image ({label})", "success")
                        elif self._consume_esptool_upload_progress(
                                upload_progress_state, stripped,
                                before_progress=_before_sr_progress):
                            continue
                        elif any(kw in low for kw in ["hard resetting", "leaving"]):
                            _sr_state[0] = "Done"
                            self._append(f"  {stripped}", "success")

                    self.process.wait()
                    rc = self.process.returncode
                    joined = " ".join(line.rstrip().lower() for line in output_lines)
                    is_conn_failure = (rc != 0 and any(sig in joined for sig in _CONNECT_FAIL_SIGNATURES))

                    if is_conn_failure and _connect_retry_count < _MAX_CONNECT_RETRIES - 1 and not getattr(self, "_stop_requested", False):
                        if not self._is_port_present(port):
                            if not self._wait_for_port_reconnect(port):
                                err_msg = f"MCU disconnected during soft reset ({port} is no longer available)"
                                ok = False
                                break
                        _connect_retry_count += 1
                        _sr_state[0] = f"Connecting ({_connect_retry_count + 1}/{_MAX_CONNECT_RETRIES})"
                        if any(x in joined for x in ("permissionerror", "access is denied", "port is busy", "could not open port", "permission denied")):
                            time.sleep(1.0)
                        self._append_connecting_progress(_connect_retry_count + 1, _MAX_CONNECT_RETRIES)

                        # Before we start a fresh PlatformIO subprocess, give
                        # the filesystem a chance to clear the WinError 32/145
                        # lock the previous attempt just hit. process.wait()
                        # has already closed the child handles; the auto-clean
                        # helper is best-effort and never throws. build_ok=False
                        # is intentional: the locked files live inside
                        # .pio/build/mcu_flash/, and build_ok=True would skip
                        # them. The next invocation regenerates them via SCons'
                        # normal incremental build. build_root mirrors the
                        # post-loop call below (soft-reset builds outside the
                        # sketch dir, into its own .pio/build root).
                        if _stale_clean_paths:
                            self._append(
                                "  ♻ Releasing stale build lock before retry…",
                                "dim",
                            )
                            self._auto_clean_stale_build_paths(
                                list(_stale_clean_paths),
                                "mcu_flash",
                                build_ok=False,
                                build_root=project_dir / ".pio" / "build",
                            )

                        continue

                    ok = (rc == 0)
                    if ok:
                        _flip_sr_connected_bar()
                        _show_sr_chip_info(force=True)
                        self._append("")
                        self._append(
                            "  ✔ Flash write and verification completed. "
                            "Reset will continue through the Serial Monitor.",
                            "success",
                        )
                        elapsed = max(0.0, time.time() - _sr_start)
                        self._append(
                            f"  {'=' * 25} [SUCCESS] Took {elapsed:.2f} seconds {'=' * 25}",
                            "success",
                        )
                    break
                except Exception as e:
                    ok = False
                    err_msg = str(e)
                    self._append(f"  ✖ Execution error: {err_msg}", "error")
                    break

        # Stop spinner
        _sr_active[0] = False
        _sr_spin_thread.join(timeout=1)

        # ── Auto-clean stale build paths reported by PlatformIO ───────────
        # The soft-reset project builds OUTSIDE the sketch dir, so point the
        # cleanup at its own .pio/build root (the fresh env cache stays).
        if _stale_clean_paths:
            self._auto_clean_stale_build_paths(
                _stale_clean_paths, "mcu_flash", build_ok=ok,
                build_root=project_dir / ".pio" / "build",
            )

        # NOTE: Do NOT delete project_dir — preserving it is the whole point.
        # The .pio/build/ cache inside it makes subsequent soft resets instant.

        if ok and p_platform in ("espressif32", "espressif8266"):
            manifest_ok, manifest_error = self._write_reset_manifest(
                project_dir, board_name, board_info
            )
            if not manifest_ok:
                self._append(
                    f"  ⚠ Reset completed, but cache integrity metadata was not saved: {manifest_error}",
                    "warning",
                )

        if ok:
            # Land on the Serial Monitor tab once the lock clears below so the
            # user immediately sees the freshly-flashed board boot output.
            self._focus_tab_on_unlock = self._serial_monitor_tab_index()

        self.is_busy = False
        self._set_buttons_busy(False)

        if ok:
            self._activate_serial_monitor_after_success("Soft Reset")

        if ok and not was_monitoring:
            self._trigger_actual_board_reset(port)

        if was_monitoring:
            # _resume_monitor() re-arms the intent flag itself and triggers the
            # DTR/RTS hardware reset so the freshly-flashed board boots into
            # the new code (the "Hard/Soft Reset won't trigger RESET" bug fix).
            self._resume_monitor()

        from tkinter import messagebox
        if ok:
            self._append("")
            self._append("  ✔ Soft Reset completed successfully!", "success")
            self._set_status("Soft Reset: SUCCESS", Theme.GREEN)
            self.root.after(0, lambda: messagebox.showinfo(
                "Soft Reset Success",
                "Soft Reset (flash memory reset) completed successfully!",
                parent=self.root,
            ))
        else:
            self._append("")
            self._append_connecting_progress(_MAX_CONNECT_RETRIES, _MAX_CONNECT_RETRIES, failed=True)
            self._append("  ✖ Soft Reset FAILED.", "error")
            self._set_status("Soft Reset: FAILED", Theme.RED)
            is_connection_error = getattr(self, "_last_fast_upload_failure_kind", "") == "connection"
            if not is_connection_error:
                self._append("  ⚠ Failure was not caused by BOOT mode connection timing.", "warning")
                self._append(
                    "  ℹ The active sketch and all other board caches were preserved.",
                    "info",
                )
                dialog_message = (
                    f"Soft Reset failed.\n{err_msg}\n\n"
                    "The selected board's reset cache was preserved for diagnosis."
                )
            else:
                dialog_message = (
                    f"Soft Reset failed.\n{err_msg}\n"
                    f"Check if the device is connected to {port}."
                )
            self.root.after(0, lambda message=dialog_message: messagebox.showerror(
                "Soft Reset Failed", message, parent=self.root
            ))

