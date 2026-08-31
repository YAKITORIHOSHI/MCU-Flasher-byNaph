#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import os
import json
import re
import hashlib
from typing import TYPE_CHECKING
from pathlib import Path


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

class SoftResetTemplateMixin(_Base):
    """Mixin providing SoftResetTemplateMixin capabilities for MCUUploadGUI."""
    @staticmethod
    def _reset_project_base_name(board_info: dict) -> str:
        """Return the template family folder used by the reset cache."""
        capabilities = board_reset_capabilities(
            board_info.get("platform", ""),
            board_info.get("board", ""),
            board_info.get("name", ""),
            board_info.get("framework", ""),
        )
        return (
            "soft_reset_project_uno"
            if capabilities.get("family") == "atmelavr"
            else "soft_reset_project"
        )

    def _soft_reset_project_dir(self, board_name: str | None = None,
                                board_info: dict | None = None) -> Path:
        """Persistent, exact-board reset project shared by Hard/Soft Reset.

        Keep both reset template families below one app-owned container. This
        leaves room for future MCU-specific reset projects without adding more
        top-level folders to the repository.
        """
        name = board_name if board_name is not None else self.board_var.get()
        info = dict(board_info or SUPPORTED_BOARDS.get(name, {}))
        base = self._reset_project_base_name(info)
        return SCRIPT_DIR / "soft_reset" / base / "boards" / self._board_cache_key(name)

    def _reset_project_contents(self, board_name: str,
                                board_info: dict) -> tuple[str, str, str]:
        """Return one canonical project template shared by Hard/Soft Reset.

        Keeping byte-identical files means a Hard Reset genuinely warms the
        following Soft Reset (and vice versa).  Board memory traits and S3 USB
        mode are part of the template, so changing physical variants triggers
        PlatformIO's normal incremental reconciliation inside only that exact
        board folder.
        """
        info = dict(board_info or {})
        platform = str(info.get("platform", "atmelavr"))
        board_id = str(info.get("board", "uno"))
        framework = str(info.get("framework", "arduino"))
        reset_capabilities = board_reset_capabilities(
            platform, board_id, board_name, framework
        )
        reset_family = str(reset_capabilities.get("family") or platform).lower()
        is_avr = reset_family == "atmelavr"
        is_esp32 = reset_family == "espressif32"
        is_esp8266 = reset_family == "espressif8266"
        is_s3 = is_s3_board(board_id)
        is_native = bool(is_s3 and self._is_native_usb_port())
        flash_size, has_psram = normalized_board_memory_options(info)
        memory_type = normalized_board_memory_type(info)
        flash_mode = normalized_board_flash_mode(info)
        monitor_speed = default_monitor_baud(platform, board_id, board_name)
        # ESP8266 reset uploads deliberately use the board manifest's reliable
        # 115200 connection speed. Cheap CH340 adapters and bare ESP-01
        # modules frequently fail to enter download mode at 460800, even when
        # normal project uploads can use that higher speed.
        upload_speed = (
            "115200"
            if is_avr or is_esp8266
            else "460800"
        )

        env_lines = [
            f"platform = {platform}",
            f"board = {board_id}",
            f"framework = {framework}",
            f"monitor_speed = {monitor_speed}",
        ]
        if not is_native:
            env_lines.append(f"upload_speed = {upload_speed}")
        if is_esp32 or is_esp8266:
            env_lines.append("upload_protocol = esptool")
        if flash_mode:
            env_lines.append(f"board_build.flash_mode = {flash_mode}")
        if flash_size:
            env_lines.extend((
                f"board_build.flash_size = {flash_size}",
                f"board_upload.flash_size = {flash_size}",
            ))
        if memory_type:
            env_lines.append(
                f"board_build.arduino.memory_type = {memory_type}"
            )

        build_flags: list[str] = []
        if has_psram:
            build_flags.append("-D BOARD_HAS_PSRAM")
        if is_native:
            build_flags.extend((
                "-DARDUINO_USB_MODE=1",
                "-DARDUINO_USB_CDC_ON_BOOT=1",
            ))
        if build_flags:
            env_lines.append(
                "build_flags =\n" + "\n".join(f"    {flag}" for flag in build_flags)
            )

        ini_content = (
            "; PlatformIO Project Configuration File for MCU Flasher Reset\n"
            "[platformio]\n"
            "src_dir = .\n"
            "default_envs = mcu_flash\n\n"
            "[env:mcu_flash]\n"
            + "\n".join(env_lines)
            + "\n"
        )
        cpp_content = (
            "#include <Arduino.h>\n"
            "void setup() {\n"
            f"  Serial.begin({monitor_speed});\n"
            "  Serial.println(\">>> ----- <<<\");\n"
            "}\n"
            "void loop() {\n"
            "}\n"
        )
        return ini_content, cpp_content, monitor_speed

    @staticmethod
    def _reset_template_digest(ini_content: str, cpp_content: str) -> str:
        payload = ini_content.encode("utf-8") + b"\0" + cpp_content.encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _installed_platform_version(platform: str) -> str:
        """Best-effort installed PlatformIO platform version for reset reuse."""
        roots = []
        configured = os.environ.get("PLATFORMIO_CORE_DIR")
        if configured:
            roots.append(Path(configured))
        roots.extend((
            SCRIPT_DIR / "src" / ".platformio-mcu-gui",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / ".mcuflasher-app" / ".platformio-mcu-gui",
            Path.home() / ".platformio",
        ))
        for root in roots:
            manifest_path = root / "platforms" / platform / "platform.json"
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                version = str(data.get("version", "")).strip()
                if version:
                    return version
            except Exception:
                continue
        return ""

    def _write_reset_manifest(self, project_dir: Path, board_name: str,
                              board_info: dict) -> tuple[bool, str]:
        """Record the exact reset template/package and image integrity data."""
        ini_content, cpp_content, _monitor_speed = self._reset_project_contents(
            board_name, board_info
        )
        build_dir = Path(project_dir) / ".pio" / "build" / "mcu_flash"
        hashes: dict[str, str] = {}
        platform = str(board_info.get("platform", ""))
        image_names = (
            ("firmware.bin",)
            if platform == "espressif8266"
            else ("bootloader.bin", "partitions.bin", "firmware.bin")
        )
        for filename in image_names:
            image_path = build_dir / filename
            if not image_path.is_file():
                return False, f"Reset build is missing {filename}."
            try:
                hashes[filename] = hashlib.sha256(image_path.read_bytes()).hexdigest()
            except OSError as exc:
                return False, f"Could not hash {filename}: {exc}"

        manifest = {
            "schema": 1,
            "board_key": self._board_cache_key(board_name),
            "board_name": board_name,
            "platform": platform,
            "board": str(board_info.get("board", "")),
            "platform_version": self._installed_platform_version(
                str(board_info.get("platform", ""))
            ),
            "template_sha256": self._reset_template_digest(
                ini_content, cpp_content
            ),
            "sha256": hashes,
            "partition_scheme": "board-specific reset project partition table",
            "source_label": "MCU Flasher exact-board reset build",
        }
        manifest_path = Path(project_dir) / "hard_reset_manifest.json"
        if not self._force_write_text(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        ):
            return False, "Could not write reset-cache integrity manifest."
        hide_hidden_attribute(manifest_path)
        return True, ""

    def _validate_reset_manifest(self, project_dir: Path, board_name: str,
                                 board_info: dict,
                                 required_images: tuple[str, ...]) -> tuple[dict | None, str]:
        """Validate template, platform version, and exact cached image bytes."""
        project_dir = Path(project_dir)
        manifest_path = project_dir / "hard_reset_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None, "reset cache has no valid integrity manifest"
        if not isinstance(manifest, dict):
            return None, "reset cache integrity manifest is malformed"

        desired_ini, desired_cpp, _monitor_speed = self._reset_project_contents(
            board_name, board_info
        )
        if manifest.get("board_key") != self._board_cache_key(board_name):
            return None, "reset cache belongs to another selectable board"
        if manifest.get("template_sha256") != self._reset_template_digest(
            desired_ini, desired_cpp
        ):
            return None, "reset template changed"
        try:
            if (project_dir / "platformio.ini").read_text(encoding="utf-8") != desired_ini:
                return None, "reset PlatformIO configuration changed"
            if (project_dir / "main.cpp").read_text(encoding="utf-8") != desired_cpp:
                return None, "reset sketch template changed"
        except OSError:
            return None, "reset project files are incomplete"

        platform = str(board_info.get("platform", ""))
        installed_version = self._installed_platform_version(platform)
        cached_version = str(manifest.get("platform_version", ""))
        if installed_version and cached_version and installed_version != cached_version:
            return None, "board platform package changed"

        expected_hashes = manifest.get("sha256")
        if not isinstance(expected_hashes, dict):
            return None, "reset image hashes are missing"
        build_dir = project_dir / ".pio" / "build" / "mcu_flash"
        for filename in required_images:
            expected = str(expected_hashes.get(filename, "")).lower()
            image_path = build_dir / filename
            if not expected or not image_path.is_file():
                return None, f"reset cache is missing validated {filename}"
            try:
                actual = hashlib.sha256(image_path.read_bytes()).hexdigest().lower()
            except OSError as exc:
                return None, f"could not read {filename}: {exc}"
            if actual != expected:
                return None, f"reset image integrity failed for {filename}"
        return manifest, ""

    def _reset_platformio_subprocess_env(self, project_dir: Path,
                                         jobs: int | None = None) -> dict:
        """PlatformIO environment for an already exact-board reset project."""
        workspace = Path(project_dir) / ".pio"
        env = os.environ.copy()
        core_dir, _core_was_refreshed = _refresh_platformio_core_environment(SCRIPT_DIR)
        env["PLATFORMIO_CORE_DIR"] = str(core_dir)
        env["PLATFORMIO_CACHE_DIR"] = str(core_dir / ".cache")
        env["PLATFORMIO_GLOBALLIB_DIR"] = str(core_dir / "lib")
        env["TMP"] = str(core_dir / ".tmp")
        env["TEMP"] = str(core_dir / ".tmp")
        env["TMPDIR"] = str(core_dir / ".tmp")
        env["PLATFORMIO_WORKSPACE_DIR"] = str(workspace)
        env["PLATFORMIO_BUILD_DIR"] = str(workspace / "build")
        env["PLATFORMIO_LIBDEPS_DIR"] = str(workspace / "libdeps")
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

    def _migrate_legacy_reset_project(self, board_name: str,
                                      board_info: dict) -> None:
        """Move a matching old/shared reset cache into its exact-board folder.

        Both layouts are accepted:

        * ``soft_reset/<template>/...`` (current)
        * ``<template>/...`` (legacy releases)

        Migration is content-aware and only moves a cache whose platform and
        board ID match the selected board, so an old cache can never be reused
        for a different MCU.
        """
        destination = self._soft_reset_project_dir(board_name, board_info)
        if (destination / ".pio").exists():
            return
        base_name = self._reset_project_base_name(board_info)
        destination_key = self._board_cache_key(board_name)
        candidate_bases = (
            SCRIPT_DIR / "soft_reset" / base_name / "boards" / destination_key,
            SCRIPT_DIR / base_name / "boards" / destination_key,
            SCRIPT_DIR / "soft_reset" / base_name,
            SCRIPT_DIR / base_name,
        )
        expected_board = str(board_info.get("board", "")).strip().lower()
        expected_platform = str(board_info.get("platform", "")).strip().lower()

        import shutil as _reset_shutil
        destination_resolved = destination.resolve()
        for candidate_base in candidate_bases:
            try:
                if candidate_base.resolve() == destination_resolved:
                    continue
            except OSError:
                pass

            legacy_ini = candidate_base / "platformio.ini"
            if not legacy_ini.is_file():
                continue
            try:
                content = legacy_ini.read_text(encoding="utf-8", errors="replace")
                board_match = re.search(
                    r"^\s*board\s*=\s*([^;#\r\n]+)", content,
                    re.IGNORECASE | re.MULTILINE,
                )
                platform_match = re.search(
                    r"^\s*platform\s*=\s*([^;#\r\n]+)", content,
                    re.IGNORECASE | re.MULTILINE,
                )
                if not board_match or not platform_match:
                    continue
                if board_match.group(1).strip().lower() != expected_board:
                    continue
                if platform_match.group(1).strip().lower() != expected_platform:
                    continue

                destination.mkdir(parents=True, exist_ok=True)
                legacy_pio = candidate_base / ".pio"
                target_pio = destination / ".pio"
                if legacy_pio.is_dir() and not target_pio.exists():
                    _reset_shutil.move(str(legacy_pio), str(target_pio))
                for filename in ("platformio.ini", "main.cpp", "hard_reset_manifest.json"):
                    source = candidate_base / filename
                    target = destination / filename
                    if source.is_file() and not target.exists():
                        _reset_shutil.copy2(source, target)
                return
            except Exception:
                # A locked old cache must not stop a fresh exact-board cache
                # from being created. PlatformIO will rebuild it safely.
                continue

    def _pio_env_name(self, board_name: str | None = None) -> str:
        """Stable PlatformIO [env:...] name / .pio/build subfolder for a
        given board. Each board gets its own slug-based env name so their
        compiled outputs (and lib deps) live in separate .pio/build/<id>
        and .pio/libdeps/<id> folders instead of overwriting each other —
        switching boards and back no longer throws away the other board's
        build."""
        name = board_name if board_name is not None else self.board_var.get()
        if not name:
            return "mcu_flash"
        # The surrounding workspace is already exact-board isolated, so the
        # environment needs only a compact stable discriminator.  Bounding it
        # prevents long downloaded-board display names from exceeding MAX_PATH.
        digest = self._board_cache_key(name).rsplit("_", 1)[-1]
        return f"mcu_{digest}"

    def _legacy_pio_env_name(self, board_name: str | None = None) -> str:
        """Environment name used before paths were bounded; migration only."""
        name = board_name if board_name is not None else self.board_var.get()
        if not name:
            return "mcu_flash"
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
        return f"mcu_flash_{slug}" if slug else "mcu_flash"

    def _get_mcu_folder_name(self, board_name: str | None = None) -> str:
        """Return clean, user-friendly folder name for storing compiled binaries per MCU family (e.g. ESP32, ESP32S3, Arduino_UNO, Arduino_Mega)."""
        name = board_name if board_name is not None else self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(name, {})
        p_board = str(board_info.get("board", "")).lower()
        platform = str(board_info.get("platform", "")).lower()

        if "uno" in p_board or "uno" in name.lower():
            return "Arduino_UNO"
        elif platform == "atmelavr":
            clean_board = re.sub(r'[^a-zA-Z0-9]+', '_', p_board or name).strip('_')
            return f"AVR_{clean_board.upper()}"
        elif "s3" in p_board or "s3" in name.lower():
            return "ESP32S3"
        elif "c3" in p_board or "c3" in name.lower():
            return "ESP32C3"
        elif "s2" in p_board or "s2" in name.lower():
            return "ESP32S2"
        elif "esp8266" in platform or "nodemcu" in name.lower():
            return "ESP8266"
        elif platform == "espressif32" or "esp32" in name.lower():
            return "ESP32"
        else:
            slug = re.sub(r'[^a-zA-Z0-9]+', '_', name).strip('_')
            return slug or "Generic_MCU"
