#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import os
import time
import re
import tempfile
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

class PlatformioIniMixin(_Base):
    """Mixin providing PlatformioIniMixin capabilities for MCUUploadGUI."""
    def _ensure_platformio_ini(self) -> bool:
        """Serialize platformio.ini preparation across all GUI/background writers."""
        with _PLATFORMIO_INI_WRITE_LOCK:
            return self._ensure_platformio_ini_impl()

    def _ensure_platformio_ini_impl(self) -> bool:
        """Ensure platformio.ini exists and has all required library dependencies."""
        ini_path = self._platformio_ini_path()

        board_info = self._resolve_board_info()
        if not board_info.get("pio_resolved", True):
            selected_name = self.board_var.get()
            arduino_id = str(board_info.get("arduino_board_id") or board_info.get("board") or "").strip()
            inferred_platform = str(board_info.get("platform") or "").strip()
            self._append(
                f"  ✖ PlatformIO does not currently expose a canonical board manifest for: {selected_name}",
                "error",
            )
            if arduino_id:
                self._append(f"    Arduino board ID: {arduino_id}", "dim")
            if inferred_platform:
                self._append(f"    Detected PlatformIO family: {inferred_platform}", "dim")
            self._append(
                "    Bootstrap can prepare every toolchain family PlatformIO supports, but an unknown board ID cannot be fixed by downloading more compiler packages.",
                "warning",
            )
            self._append(
                "    Update/re-run Bootstrap so the PlatformIO board catalog can be refreshed, then retry.",
                "info",
            )
            return False
        p_platform = board_info["platform"]
        p_board = board_info["board"]
        p_framework = board_info["framework"]

        # 1. Scan for required libraries based on includes
        detected_libs = self._scan_includes_for_libs()

        # Check if WiFiProv / BLE / large-stack libraries are used, OR if the
        # board is an ESP32 / ESP32-S3 (which benefits from huge_app by default).
        # WiFiProv.h is ESP32-only and requires wifi_provisioning headers that
        # do not exist on ESP8266 — so we also gate huge_app on ESP32 platforms.
        _LARGE_STACK_HEADERS = {
            "WiFiProv.h",
            "wifi_provisioning/manager.h",
            "wifi_provisioning/scheme_softap.h",
            "wifi_provisioning/scheme_ble.h",
            "BLEDevice.h",
            "SimpleBLE.h",
            "BluetoothSerial.h",
        }
        _NETWORK_PROV_MARKERS = (
            "WiFiProv.h",
            "wifi_provisioning/",
            "NETWORK_PROV_SCHEME_",
            "NETWORK_PROV_SECURITY_",
        )
        needs_network_prov_compat = False
        needs_huge_app = False
        # Default huge_app for ESP32 / ESP32-S3 boards — their base firmware
        # already consumes most of the default 1.25 MB app partition, and any
        # WiFiProv / BLE sketch will overflow it at link time.
        if p_platform == "espressif32":
            needs_huge_app = True
        # Also scan source files in case the board is not yet selected but the
        # headers make the intent clear.
        if not needs_huge_app and self.sketch_dir_path.exists():
            for file_path in get_project_root_source_files(
                self.sketch_dir_path, (".ino", ".cpp", ".h", ".c")
            ):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    if any(marker in content for marker in _NETWORK_PROV_MARKERS):
                        needs_network_prov_compat = True
                    if any(h in content for h in _LARGE_STACK_HEADERS):
                        needs_huge_app = True
                        if needs_network_prov_compat:
                            break
                except Exception:
                    pass

        # ESP32 defaults to huge_app before scanning, so provisioning aliases
        # need their own source-driven pass.
        if p_platform == "espressif32" and not needs_network_prov_compat and self.sketch_dir_path.exists():
            for file_path in get_project_root_source_files(
                self.sketch_dir_path, (".ino", ".cpp", ".h", ".hpp", ".c")
            ):
                try:
                    source_text = file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if any(marker in source_text for marker in _NETWORK_PROV_MARKERS):
                    needs_network_prov_compat = True
                    break
        
        # 2. Get Arduino user libraries directory from local settings
        arduino_lib_dir = ""
        try:
            download_dir = _get_download_dir()
            arduino_lib_dir = os.path.join(download_dir, "Libs").replace("\\", "/")
        except Exception:
            pass

        flash_size, has_psram = normalized_board_memory_options(board_info)
        memory_type = normalized_board_memory_type(board_info)
        flash_mode = normalized_board_flash_mode(board_info)
        
        if not ini_path.exists():
            self._append(f"  📝 platformio.ini not found in {ini_path.parent}.", "warning")
            self._append("  Creating a default platformio.ini with detected dependencies...", "info")
            
            lib_deps_str = ""
            if detected_libs:
                lib_deps_str = "\nlib_deps =\n" + "\n".join(f"    {lib}" for lib in detected_libs)
                
            lib_extra_dirs_str = f"\nlib_extra_dirs = {arduino_lib_dir}" if arduino_lib_dir else ""

            # ESP32-S3 requires extra build flags for USB-Serial to work correctly.
            # Without these the Serial monitor is silent even when the sketch runs.
            #   -DARDUINO_USB_MODE=1        → use TinyUSB (not ROM CDC)
            #   -DARDUINO_USB_CDC_ON_BOOT=1 → enable CDC-over-USB on boot
            # Flash mode is taken from the board's actual manifest when it is
            # explicit; S3 boards are not all DIO (some use QIO/DOUT).
            
            build_flags_list = []
            if p_platform == "espressif32" and needs_network_prov_compat:
                build_flags_list.extend([
                    "-D NETWORK_PROV_SCHEME_SOFTAP=WIFI_PROV_SCHEME_SOFTAP",
                    "-D NETWORK_PROV_SCHEME_HANDLER_NONE=WIFI_PROV_SCHEME_HANDLER_NONE",
                    "-D NETWORK_PROV_SECURITY_1=WIFI_PROV_SECURITY_1",
                ])
            
            board_extra: list[str] = []
            _uspd = self.upload_speed_var.get() if hasattr(self, "upload_speed_var") else "460800"
            # AVR bootloader speeds vary by chip/bootloader version:
            # - Uno: 115200
            # - Nano (old bootloader) / Mega 2560: 57600
            # - Pro Mini (8MHz): 38400 / 57600
            if p_platform == "atmelavr":
                if p_board in ("nanoatmega328", "nano", "pro8MHz", "pro16MHz", "megaatmega2560", "mega", "nanoatmega168"):
                    _uspd = "57600"
                elif p_board in ("pro384", "pro384MHz"):
                    _uspd = "38400"
                else:
                    _uspd = "115200"
            upload_speed_line = f"\nupload_speed = {_uspd}"
            if has_psram:
                build_flags_list.append("-D BOARD_HAS_PSRAM")

            if is_s3_board(p_board):
                is_native = self._is_native_usb_port()
                upload_speed_line = "" if is_native else f"\nupload_speed = {_uspd}"
                if is_native:
                    build_flags_list.extend([
                        "-DARDUINO_USB_MODE=1",
                        "-DARDUINO_USB_CDC_ON_BOOT=1"
                    ])
            if p_platform in ("espressif32", "espressif8266"):
                board_extra.append("upload_protocol = esptool")
                if flash_mode:
                    board_extra.append(f"board_build.flash_mode = {flash_mode}")

            build_flags_str = (
                "build_flags =\n" + "\n".join(f"    {flag}" for flag in build_flags_list)
                if build_flags_list else ""
            )
            partition_str = "board_build.partitions = huge_app.csv\n" if (needs_huge_app and p_platform == "espressif32") else ""

            # Build the [env:mcu_flash] body line-by-line so no key ever gets
            # concatenated onto the tail of another key's value line.
            monitor_speed = "9600" if p_platform == "atmelavr" else "115200"
            env_lines: list[str] = [
                f"platform = {p_platform}",
                f"board = {p_board}",
                f"framework = {p_framework}",
                f"monitor_speed = {monitor_speed}",
            ]
            if flash_size:
                env_lines.append(f"board_build.flash_size = {flash_size}")
                env_lines.append(f"board_upload.flash_size = {flash_size}")
            if memory_type:
                env_lines.append(
                    f"board_build.arduino.memory_type = {memory_type}"
                )

            # upload_speed only for non-native-USB boards (upload_speed_line is
            # "\nupload_speed = {speed}" when set, "" when skipped)
            if upload_speed_line:
                env_lines.append(f"upload_speed = {_uspd}")
            env_lines.extend(board_extra)
            if build_flags_str:
                env_lines.append(build_flags_str)
            if lib_extra_dirs_str:
                env_lines.append(f"lib_extra_dirs = {arduino_lib_dir}")
            if lib_deps_str:
                env_lines.append(lib_deps_str.strip())
            if partition_str:
                env_lines.append(partition_str.strip())

            # Join with a blank line between every key block so Python's
            # configparser never treats build_flags' indented -D lines as a
            # multi-line continuation of board_build.flash_mode.  A blank line
            # always terminates a multi-line value in configparser semantics.
            env_body = "\n\n".join(env_lines)

            content = f"""; PlatformIO Project Configuration File
; Generated automatically by MCU Flasher by Naph

[platformio]
default_envs = {self._pio_env_name()}

[env:{self._pio_env_name()}]
{env_body}
"""
            try:
                if not self._force_write_text(ini_path, content):
                    raise OSError(
                        "platformio.ini update failed after retries" +
                        (f": {getattr(self, '_last_platformio_ini_write_error', '')}"
                         if getattr(self, '_last_platformio_ini_write_error', '') else "")
                    )
                if not is_unc_or_network_path(self.sketch_dir_path):
                    hide_internal_project_metadata(self.sketch_dir_path)
                self._append("  ✔ Created default platformio.ini successfully.", "success")
                self._append_notif(
                    f"  📄 platformio.ini created for {self.board_var.get()} in {ini_path.parent}",
                    tag="success", category="pio_ini", title="platformio.ini Created"
                )
                if detected_libs:
                    self._append(f"  Detected libraries: {', '.join(detected_libs)}", "info")
                return True
            except Exception as e:
                self._append(f"  ✖ Failed to create platformio.ini: {e}", "error")
                return False
        else:
            # platformio.ini already exists. Validate and heal it first, then update.
            try:
                ensure_file_writable(ini_path)
                if heal_platformio_ini_symlinks_and_dirs(ini_path, self.sketch_dir_path):
                    self._append("  ✔ Auto-healed stale library paths/symlinks in platformio.ini for current device.", "success")
                content = ini_path.read_text(encoding="utf-8", errors="replace")
                old_content = content
                _previous_board_match = re.search(
                    r"^\s*board\s*=\s*([^;#\r\n]+)", content,
                    re.IGNORECASE | re.MULTILINE,
                )
                _previous_board_id = (
                    _previous_board_match.group(1).strip()
                    if _previous_board_match else ""
                )
                _previous_had_s3_settings = (
                    is_s3_board(_previous_board_id)
                    or "ARDUINO_USB_MODE" in content
                    or "ARDUINO_USB_CDC_ON_BOOT" in content
                )


                # ── Per-board env rename ────────────────────────────────────────
                # The ini historically kept editing platform=/board= in place
                # inside the SAME [env:mcu_flash] section no matter which board
                # was selected, so every board switch overwrote one shared
                # .pio/build/mcu_flash folder. Instead, rename the section (and
                # default_envs) to this board's own env name so its build
                # output lives in its own .pio/build/<env> folder and a
                # previously-built board's folder is left untouched on disk.
                target_env = self._pio_env_name()
                _env_hdr_match = re.search(r"^\[env:([^\]]*)\]", content, re.MULTILINE)
                _switching_board_env = bool(
                    _env_hdr_match
                    and _env_hdr_match.group(1) != target_env
                )
                if _env_hdr_match and _env_hdr_match.group(1) != target_env:
                    old_env = _env_hdr_match.group(1)
                    content = re.sub(
                        rf"^\[env:{re.escape(old_env)}\]",
                        f"[env:{target_env}]",
                        content, count=1, flags=re.MULTILINE
                    )
                    content = re.sub(
                        r"^default_envs\s*=.*",
                        f"default_envs = {target_env}",
                        content, count=1, flags=re.MULTILINE
                    )

                # ── Corruption guard ───────────────────────────────────────────
                # A previously malformed ini may have bare build-flag lines
                # injected immediately after [env:...] (no key = value format),
                # or monitor_speed whose value trails into build_flags text,
                # or -D / -I / -W lines that appear in the file but have no
                # preceding "build_flags =" key to own them.
                # Detect any of these symptoms and regenerate from scratch.
                _env_hdr_junk = re.compile(
                    r"^\[env:[^\]]*\]\s*\n(?:[ \t]+-[^\n]+\n)+", re.MULTILINE
                )
                _monitor_junk = re.compile(
                    r"^monitor_speed\s*=\s*\d+\s*\n[ \t]+-D\s", re.MULTILINE
                )
                # Orphaned flag lines: a -D/-I/-W/-O line at column 0 (no indent)
                # means a flag escaped from build_flags entirely.
                # NOTE: indented -D lines under build_flags are VALID and must NOT be
                # flagged — the old check caused false positives when upload_speed or
                # any other key appeared immediately before the build_flags block.
                # The _missing_build_flags check below already catches the case where
                # -D lines exist but build_flags is absent.
                _orphan_flags = re.compile(
                    r"^-[DIWOfdm]", re.MULTILINE
                )
                # -D lines present but build_flags key is completely absent
                _has_defines     = bool(re.search(r"^\s+-D\s", content, re.MULTILINE))
                _has_build_flags = bool(re.search(r"^build_flags\s*=", content, re.MULTILINE))
                _missing_build_flags = _has_defines and not _has_build_flags
                _has_platform = bool(re.search(r"^platform\s*=", content, re.MULTILINE))
                _has_board    = bool(re.search(r"^board\s*=",    content, re.MULTILINE))
                if (
                    _env_hdr_junk.search(content)
                    or _monitor_junk.search(content)
                    or _orphan_flags.search(content)
                    or _missing_build_flags
                    or not (_has_platform and _has_board)
                ):
                    self._append("  ⚠ platformio.ini is malformed — regenerating from scratch.", "warning")
                    ini_path.unlink(missing_ok=True)
                    # Also wipe the stale build cache — it was compiled against the
                    # wrong board/flags and will cause "no input files" on next build.
                    _stale_workspace = self._board_workspace()
                    if _stale_workspace.exists():
                        if robust_rmtree(_stale_workspace):
                            self._append(
                                "  🗑 Repaired malformed configuration cache for the selected board only.",
                                "warning",
                            )
                    return self._ensure_platformio_ini()

                # Remove src_dir=. if present.  With src_dir=., PlatformIO's
                # InoToCPPConverter writes the intermediate .ino.cpp into the
                # sketch root, but SCons expects it under .pio/build/<env>/src/.
                # The mismatch leaves g++ with no input file.  Dropping src_dir
                # lets PlatformIO use its default (src/), which the GUI keeps
                # synced below via _sync_src_dir().
                content = re.sub(r"^src_dir\s*=.*\n?", "", content, flags=re.MULTILINE)

                # Update platform and board
                if re.search(r"^platform\s*=", content, re.MULTILINE):
                    content = re.sub(r"^platform\s*=.*", f"platform = {p_platform}", content, flags=re.MULTILINE)
                if re.search(r"^board\s*=", content, re.MULTILINE):
                    content = re.sub(r"^board\s*=.*", f"board = {p_board}", content, flags=re.MULTILINE)

                # Keep the generated monitor baud aligned with the selected
                # platform. Arduino Uno uses 9600; ESP boards use 115200.
                desired_monitor_speed = "9600" if p_platform == "atmelavr" else "115200"
                if re.search(r"^monitor_speed\s*=", content, re.MULTILINE):
                    content = re.sub(
                        r"^monitor_speed\s*=.*",
                        f"monitor_speed = {desired_monitor_speed}",
                        content, flags=re.MULTILINE,
                    )
                else:
                    content = re.sub(
                        r"(\[env:[^\]]*\]\n)",
                        rf"\1monitor_speed = {desired_monitor_speed}\n",
                        content, count=1,
                    )

                # Update upload_speed based on board type
                if p_board == "uno":
                    if re.search(r"^upload_speed\s*=", content, re.MULTILINE):
                        content = re.sub(r"^upload_speed\s*=.*", "upload_speed = 115200", content, flags=re.MULTILINE)
                    else:
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1upload_speed = 115200\n",
                            content, count=1
                        )
                elif is_s3_board(p_board) and self._is_native_usb_port():
                    # Native USB removes upload_speed below, so we do nothing here
                    pass
                else:
                    current_speed = self.upload_speed_var.get() if hasattr(self, "upload_speed_var") else "460800"
                    if re.search(r"^upload_speed\s*=", content, re.MULTILINE):
                        content = re.sub(r"^upload_speed\s*=.*", f"upload_speed = {current_speed}", content, flags=re.MULTILINE)
                    else:
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            rf"\1upload_speed = {current_speed}\n",
                            content, count=1
                        )
                    
                if arduino_lib_dir:
                    if re.search(r"^lib_extra_dirs\s*=", content, re.MULTILINE):
                        content = re.sub(r"^lib_extra_dirs\s*=.*$", f"lib_extra_dirs = {arduino_lib_dir}", content, flags=re.MULTILINE)
                    else:
                        if not content.endswith("\n"):
                            content += "\n"
                        content += f"lib_extra_dirs = {arduino_lib_dir}\n"

                # Ensure Arduino core v3 compatibility defines are present in build_flags.
                # These work around an ESP32 Arduino-core v3 API rename in WiFiProv —
                # they don't exist on AVR and must never be injected for Uno/atmelavr.
                if p_platform == "espressif32" and needs_network_prov_compat:
                    compat_flags = [
                        "-D NETWORK_PROV_SCHEME_SOFTAP=WIFI_PROV_SCHEME_SOFTAP",
                        "-D NETWORK_PROV_SCHEME_HANDLER_NONE=WIFI_PROV_SCHEME_HANDLER_NONE",
                        "-D NETWORK_PROV_SECURITY_1=WIFI_PROV_SECURITY_1"
                    ]
                    existing_build_flags = re.search(r"^build_flags\s*=", content, re.MULTILINE)
                    if existing_build_flags:
                        # Append any missing flags to the existing build_flags block
                        for flag in compat_flags:
                            if flag not in content:
                                content = re.sub(
                                    r"(^build_flags\s*=.*(?:\n[ \t]+\S.*)*)",
                                    lambda m, f=flag: m.group(0).rstrip() + f"\n    {f}",
                                    content, count=1, flags=re.MULTILINE
                                )
                    else:
                        # No build_flags at all — append at end of file
                        flags_block = "build_flags =\n" + "".join(f"    {f}\n" for f in compat_flags)
                        if not content.endswith("\n"):
                            content += "\n"
                        content += flags_block
                else:
                    # Strip generated provisioning aliases when this sketch does
                    # not use WiFiProv, including ordinary ESP32 and all AVR boards.
                    for _flag in (
                        "-D NETWORK_PROV_SCHEME_SOFTAP=WIFI_PROV_SCHEME_SOFTAP",
                        "-D NETWORK_PROV_SCHEME_HANDLER_NONE=WIFI_PROV_SCHEME_HANDLER_NONE",
                        "-D NETWORK_PROV_SECURITY_1=WIFI_PROV_SECURITY_1",
                    ):
                        content = content.replace(f"    {_flag}\n", "")
                    # If that emptied out the build_flags block entirely, drop the key too.
                    # The negative lookahead prevents stripping `build_flags =` when
                    # indented continuation lines (the actual flags) follow on the next line.
                    content = re.sub(r"^build_flags[ \t]*=[ \t]*$(?:\n[ \t]*$)*(?!\n[ \t]+\S)", "", content, flags=re.MULTILINE)

                # Ensure huge_app.csv partitions are selected when needed for ESP32, and purged for non-ESP platforms
                if needs_huge_app and p_platform == "espressif32":
                    if not re.search(r"^board_build\.partitions\s*=", content, re.MULTILINE):
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1board_build.partitions = huge_app.csv\n",
                            content, count=1
                        )
                else:
                    content = re.sub(
                        r"^board_build\.partitions\s*=\s*huge_app\.csv\s*\n?",
                        "",
                        content,
                        flags=re.MULTILINE | re.IGNORECASE,
                    )

                # Reconcile every GUI-owned board option on every switch.  The
                # previous implementation nested all cleanup under the *new*
                # board being S3, so S3 USB/PSRAM/flash settings leaked into
                # Uno and ordinary ESP32 environments.
                def _set_env_option(key: str, value: str) -> None:
                    nonlocal content
                    pattern = rf"^{re.escape(key)}\s*=.*$"
                    replacement = f"{key} = {value}"
                    if re.search(pattern, content, re.MULTILINE):
                        content = re.sub(
                            pattern, replacement, content, count=1,
                            flags=re.MULTILINE,
                        )
                    else:
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            lambda m: m.group(1) + replacement + "\n",
                            content, count=1,
                        )

                def _remove_env_option(key: str, value_pattern: str = r".*") -> None:
                    nonlocal content
                    content = re.sub(
                        rf"^{re.escape(key)}\s*=\s*{value_pattern}\s*\n?",
                        "", content, flags=re.MULTILINE | re.IGNORECASE,
                    )

                def _ensure_build_flag(flag: str) -> None:
                    nonlocal content
                    if re.search(
                        rf"^[ \t]*{re.escape(flag)}[ \t]*$",
                        content, re.MULTILINE,
                    ):
                        return
                    block = re.search(
                        r"^build_flags\s*=.*(?:\n[ \t]+\S.*)*",
                        content, re.MULTILINE,
                    )
                    if block:
                        replacement = block.group(0).rstrip() + f"\n    {flag}"
                        content = content[:block.start()] + replacement + content[block.end():]
                    else:
                        if not content.endswith("\n"):
                            content += "\n"
                        content += f"\nbuild_flags =\n    {flag}\n"

                # Remove app-generated flags before applying this board's
                # exact traits.  User-defined unrelated flags remain intact.
                for _managed_flag_pattern in (
                    r"^[ \t]*-D\s*ARDUINO_USB_MODE\s*=.*\n?",
                    r"^[ \t]*-D\s*ARDUINO_USB_CDC_ON_BOOT\s*=.*\n?",
                    r"^[ \t]*-D\s*BOARD_HAS_PSRAM\s*\n?",
                    r"^[ \t]*-mfix-esp32-psram-cache-issue\s*\n?",
                ):
                    content = re.sub(
                        _managed_flag_pattern, "", content,
                        flags=re.MULTILINE,
                    )

                current_is_s3 = is_s3_board(p_board)
                is_native = current_is_s3 and self._is_native_usb_port()
                if flash_mode:
                    _set_env_option("board_build.flash_mode", flash_mode)
                elif _switching_board_env or _previous_had_s3_settings:
                    # Remove a value injected by older blanket-S3 handling and
                    # let the exact PlatformIO board manifest choose its mode.
                    _remove_env_option("board_build.flash_mode")
                if is_native:
                    _ensure_build_flag("-DARDUINO_USB_MODE=1")
                    _ensure_build_flag("-DARDUINO_USB_CDC_ON_BOOT=1")

                if memory_type:
                    _set_env_option(
                        "board_build.arduino.memory_type", memory_type
                    )
                else:
                    _remove_env_option("board_build.arduino.memory_type")
                if has_psram:
                    _ensure_build_flag("-D BOARD_HAS_PSRAM")

                if flash_size:
                    _set_env_option("board_build.flash_size", str(flash_size))
                    _set_env_option("board_upload.flash_size", str(flash_size))
                else:
                    _remove_env_option("board_build.flash_size")
                    _remove_env_option("board_upload.flash_size")

                # Upload options do not affect object reuse, but must match the
                # selected board so a cached A -> B -> A upload is safe.
                if p_platform in ("espressif32", "espressif8266"):
                    if re.search(
                        r"^upload_protocol\s*=\s*(?:esp-builtin|esp-usb-jtag)\b",
                        content, re.MULTILINE | re.IGNORECASE,
                    ):
                        _set_env_option("upload_protocol", "esptool")
                    elif not re.search(r"^upload_protocol\s*=", content, re.MULTILINE):
                        _set_env_option("upload_protocol", "esptool")
                else:
                    _remove_env_option("upload_protocol")
                _remove_env_option("upload_resetmethod")
                if is_native:
                    _remove_env_option("upload_speed")

                # Drop an empty generated build_flags key after managed flags
                # were removed for a board that does not need them.
                content = re.sub(
                    r"^build_flags[ \t]*=[ \t]*$(?:\n[ \t]*$)*(?!\n[ \t]+\S)",
                    "", content, flags=re.MULTILINE,
                )

                def _lib_key(slug: str) -> str:
                    # 'dhrubasaha08/DHT11 @ ^2.1.0'                       -> 'dht11'
                    # 'symlink://C:/Users/.../DHT11'                       -> 'dht11'
                    # 'C:/Users/napht/Documents/Arduino/libraries/DHT11'  -> 'dht11'
                    # Strip symlink:// prefix before extracting the key
                    s = slug
                    if s.startswith("symlink://"):
                        s = s[len("symlink://"):]
                    tail = s.replace("\\", "/").rstrip("/").split("/")[-1]
                    tail = tail.split("@")[0].strip()
                    return tail.lower()

                lib_deps_block_re = re.compile(r"^lib_deps\s*=[ \t]*\n?(?:[ \t]+\S.*\n?)*", re.MULTILINE)
                existing_match = lib_deps_block_re.search(content)
                old_entries = []
                if existing_match:
                    for line in existing_match.group(0).splitlines()[1:]:
                        s = line.strip()
                        if s:
                            old_entries.append(s)

                old_keys = {_lib_key(e) for e in old_entries}
                new_keys = {_lib_key(e) for e in detected_libs}

                # ── Cross-device symlink stale check ────────────────────────
                # _lib_key() normalises both old and new to the same basename,
                # so two entries that LOOK identical (same library name) but
                # point to DIFFERENT machines would pass the key comparison
                # unchanged and leave the foreign C:/Users/Admin/… path in the
                # file.  Fix: if ANY existing symlink:// entry points to a
                # directory that does NOT exist on this machine, force a full
                # rebuild so detected_libs (which uses THIS machine's paths) is
                # written instead.
                _has_stale_symlink = any(
                    e.startswith("symlink://") and not Path(e[len("symlink://"):]).exists()
                    for e in old_entries
                )

                rebuild_needed = _has_stale_symlink or (old_keys != new_keys) or any(
                    e not in detected_libs for e in old_entries if _lib_key(e) in new_keys
                )


                if rebuild_needed:
                    if detected_libs:
                        new_block = "lib_deps =\n" + "\n".join(f"    {lib}" for lib in detected_libs) + "\n"
                    else:
                        new_block = ""

                    if existing_match:
                        content = lib_deps_block_re.sub(new_block, content, count=1)
                    elif new_block:
                        if not content.endswith("\n"):
                            content += "\n"
                        content += "\n" + new_block

                    stale = [e for e in old_entries if _lib_key(e) in old_keys - new_keys]
                    added = [lib for lib in detected_libs if _lib_key(lib) in new_keys - old_keys]
                    if _has_stale_symlink:
                        stale_paths = [e for e in old_entries if e.startswith("symlink://") and not Path(e[len("symlink://"):]).exists()]
                        for sp in stale_paths:
                            self._append("  🔄 Auto-healing foreign library path → adapting to this device:", "warning")
                            self._append(f"     From: {sp}", "dim")
                        # Show what it was healed to
                        for lib in detected_libs:
                            self._append(f"     To  : {lib}", "success")
                    if stale:
                        self._append(f"  📝 Removing stale/incorrect dependencies: {', '.join(stale)}", "warning")
                    if added:
                        self._append(f"  📝 Adding dependencies: {', '.join(added)}", "warning")
                    self._append("  Rebuilding lib_deps in platformio.ini...", "info")



                if content != old_content:
                    if not self._force_write_text(ini_path, content):
                        raise OSError(
                        "platformio.ini update failed after retries" +
                        (f": {getattr(self, '_last_platformio_ini_write_error', '')}"
                         if getattr(self, '_last_platformio_ini_write_error', '') else "")
                    )
                    if "upload_protocol = esptool" in content and "upload_protocol = esptool" not in old_content:
                        self._append(
                            "  📝 Serial upload protocol updated; PlatformIO will reconcile the selected board incrementally.",
                            "info",
                        )
                    if _has_stale_symlink and target_env:
                        libdeps_dir = self._board_workspace() / "libdeps" / target_env
                        if libdeps_dir.exists():
                            if robust_rmtree(libdeps_dir):
                                self._append("  📝 Cleared stale .pio/libdeps cache (cross-device library paths healed).", "info")
                else:
                    pass
                if rebuild_needed or (arduino_lib_dir and "lib_extra_dirs" in content):
                    self._append("  ✔ Updated platformio.ini successfully.", "success")
                return True
            except Exception as e:
                self._append(f"  ⚠ Failed to inspect/update existing platformio.ini: {e}", "warning")
                return False

    def _force_write_text(self, path: Path, content: str, attempts: int = 14, delay: float = 0.08) -> bool:
        """Reliably update a generated text file without requiring administrator rights.

        The old implementation repeatedly opened the destination with ``write_text``.
        That is vulnerable to a short Windows sharing violation when Monaco, Defender,
        OneDrive, or another GUI callback has the file open.  This version serializes
        app-owned writers, writes a fully flushed sibling temporary file, then uses an
        atomic replace.  If the destination handle permits writing but not replacement,
        it falls back to an in-place rewrite for that attempt.

        A board switch should never need UAC: the sketch belongs to the signed-in user.
        Elevating only changes the security context and can create files owned by a
        different administrator account; it does not solve a sharing-mode lock.
        """
        path = Path(path)
        last_exc = None
        with _PLATFORMIO_INI_WRITE_LOCK:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            for i in range(max(1, int(attempts))):
                temp_path = None
                try:
                    # A previous writer may already have completed the exact update.
                    if path.exists():
                        try:
                            if path.read_text(encoding="utf-8", errors="replace") == content:
                                self._last_platformio_ini_write_error = ""
                                return True
                        except Exception:
                            pass

                    ensure_file_writable(path)
                    fd, temp_name = tempfile.mkstemp(
                        prefix=f".{path.name}.mcu-write-",
                        suffix=".tmp",
                        dir=str(path.parent),
                    )
                    temp_path = Path(temp_name)
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8", errors="strict", newline="") as stream:
                            stream.write(content)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except Exception:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        raise

                    ensure_file_writable(path)
                    try:
                        os.replace(str(temp_path), str(path))
                        temp_path = None
                        self._last_platformio_ini_write_error = ""
                        return True
                    except Exception as replace_exc:
                        last_exc = replace_exc
                        # Some Windows handles deny delete/rename sharing but still allow
                        # an ordinary write. Try that before waiting for the next retry.
                        try:
                            ensure_file_writable(path)
                            with open(path, "w", encoding="utf-8", errors="strict", newline="") as stream:
                                stream.write(content)
                                stream.flush()
                                os.fsync(stream.fileno())
                            self._last_platformio_ini_write_error = ""
                            return True
                        except Exception as direct_exc:
                            last_exc = direct_exc
                except Exception as exc:
                    last_exc = exc
                finally:
                    if temp_path is not None:
                        try:
                            temp_path.unlink(missing_ok=True)
                        except Exception:
                            pass

                # Short capped backoff: enough for AV/editor handles to release without
                # making a compile look frozen. Total wait is only a few seconds.
                if i < attempts - 1:
                    time.sleep(min(0.45, delay * (i + 1)))

            # One final equality check handles the case where another serialized writer
            # won the race immediately after our last failed filesystem operation.
            try:
                matched = path.exists() and path.read_text(encoding="utf-8", errors="replace") == content
                if matched:
                    self._last_platformio_ini_write_error = ""
                    return True
            except Exception as final_exc:
                last_exc = final_exc
            self._last_platformio_ini_write_error = str(last_exc or "unknown Windows file-sharing/permission error")
            return False

    def _check_libraries_installed(self) -> bool:
        """Query arduino-cli for installed libraries and check if required ones are missing."""
        cli_path = find_arduino_cli_executable()
        if not cli_path:
            self._append("  ✖ Arduino-CLI not found on the computer!", "error")
            self._append("  Please install Arduino-CLI first.", "info")
            return False

        # 1. Query installed libraries map from arduino-cli
        installed_libs_map, installed_header_map = self._get_installed_libraries_map()

        # 2. Extract includes from sketch files
        required_headers = []
        if self.sketch_dir_path.exists():
            for file_path in get_project_root_source_files(
                self.sketch_dir_path, (".ino", ".cpp", ".h", ".c")
            ):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    includes = re.findall(r'#include\s*[<"]([^>"]+)[>"]', content)
                    for header in includes:
                        if header not in required_headers:
                            required_headers.append(header)
                except Exception:
                    pass

        # Builtin/standard libraries to ignore
        BUILTIN_AND_STD = set(STANDARD_C_CPP_HEADERS) | {
            # Arduino/ESP32 core standard builtins
            "arduino.h", "wifi.h", "wire.h", "spi.h", "fs.h", "sd.h", "bledevice.h",
            "update.h", "webserver.h", "wificlient.h", "wificlientsecure.h", "ticker.h",
            "spiffs.h", "littlefs.h", "eeprom.h", "preferences.h", "soc/soc.h",
            "driver/adc.h", "esp_adc_cal.h", "soc/rtc_cntl_reg.h", "pgmspace.h",
            "freertos/freertos.h", "freertos/task.h", "esp_system.h", "esp_spi_flash.h",
            "esp_partition.h", "esp_ota_ops.h", "nvs_flash.h", "nvs.h", "pins_arduino.h",
            "esp_bt.h", "esp_bt_main.h", "esp_gap_ble_api.h", "esp_gatts_api.h",
            "esp_gatt_common_api.h", "esp_bt_device.h", "esp_gap_bt_api.h",
            "hardwareserial.h",
            # softwareserial.h is NOT in CORE_HEADERS: on ESP32/ESP8266 it
            # requires the EspSoftwareSerial lib_dep — let the scanner find it.
            "sd_mmc.h", "wifiap.h",
            "wifimulti.h", "wifiscan.h", "wifiserver.h", "ethernet.h", "client.h",
            "server.h", "stream.h", "print.h", "printable.h", "wstring.h",
            "ipaddress.h", "ipv6address.h", "wifiudp.h",
            "wifiprov.h", "netbios.h", "espmdns.h", "simpleble.h", "bluetoothserial.h", "httpclient.h",
            # ESP32 specific / built-in standard sub-headers
            "http_client.h", "wifiudp.h", "dnsserver.h", "esp_wifi.h", "esp_event.h",
            "esp_log.h", "lwip/err.h", "lwip/sockets.h", "lwip/sys.h", "lwip/netdb.h",
            "lwip/dns.h", "mbedtls/aes.h", "mbedtls/entropy.h", "mbedtls/ctr_drbg.h",
            "mbedtls/md.h", "mbedtls/sha256.h", "rom/ets_sys.h", "esp_sleep.h",
            "hal/gpio_hal.h", "driver/gpio.h", "driver/ledc.h", "driver/uart.h",
            "driver/spi_master.h", "driver/i2c.h", "driver/timer.h", "driver/pcnt.h",
            "driver/mcpwm.h", "driver/rmt.h", "driver/pulse_cnt.h", "driver/sdspi_host.h",
            "driver/sdmmc_host.h", "sdmmc_cmd.h", "esp_vfs.h", "esp_vfs_fat.h",
            "fatfs_vfs.h", "ff.h", "diskio.h", "esp_spiffs.h", "esp_littlefs.h",
            "esp_camera.h", "fd_forward.h", "fr_forward.h", "image_util.h"
        }

        # Override mappings from header file to library name (internal key format for matching)
        HEADER_TO_LIB_NAME = {
            "ESP32Servo.h": "ESP32Servo",
            "FastAccelStepper.h": "FastAccelStepper",
            "HX711.h": "HX711",
            "NimBLEDevice.h": "NimBLE-Arduino",
            "OneWire.h": "OneWire",
            "DallasTemperature.h": "DallasTemperature",
            "Adafruit_Sensor.h": "Adafruit Unified Sensor",
            "DHT.h": "DHT sensor library",
            "PubSubClient.h": "PubSubClient",
            "ArduinoJson.h": "ArduinoJson",
            "TFT_eSPI.h": "TFT_eSPI",
            "TinyGsmClient.h": "TinyGSM",
            "FirebaseESP32.h": "Firebase ESP32 Client",
            "addons/TokenHelper.h": "Firebase ESP32 Client",
            "addons/RTDBHelper.h": "Firebase ESP32 Client",
        }

        # Collect local files to avoid false alarms on local headers
        local_files = []
        try:
            for f in get_sketch_files_fast(self.sketch_dir_path):
                local_files.append(f.name.lower())
        except Exception:
            pass

        missing_libs = []
        def normalize_lib_name(name: str) -> str:
            return "".join(c for c in name.lower() if c.isalnum())

        for header in required_headers:
            h_lower = header.lower()
            # Skip C++ standard libraries, built-in ESP32 core libraries, and local files
            if h_lower in BUILTIN_AND_STD:
                continue
            if h_lower.startswith("esp_") or h_lower.startswith("driver/") or h_lower.startswith("soc/") or h_lower.startswith("hal/") or h_lower.startswith("freertos/") or h_lower.startswith("rom/") or h_lower.startswith("lwip/") or h_lower.startswith("mbedtls/"):
                continue
            if not h_lower.endswith(".h") or h_lower in local_files:
                continue

            # Determine the display name and search key
            lib_display_name = HEADER_TO_LIB_NAME.get(header, header[:-2]) # remove .h if not in override map
            norm_key = normalize_lib_name(lib_display_name)
            
            # Check if this library is in the list of installed libraries
            if header in installed_header_map:
                continue

            if norm_key not in installed_libs_map:
                # Also try checking if the header itself without .h matches (just in case of overrides mismatch)
                alt_norm_key = normalize_lib_name(header[:-2])
                if alt_norm_key not in installed_libs_map:
                    missing_libs.append((lib_display_name, header))

        if missing_libs:
            self._append("  ✖ Cannot compile: Missing required Arduino libraries!", "error")
            for lib_name, header in missing_libs:
                self._append(f"    • Library '{lib_name}' is not installed (required by #include <{header}>)", "warning")
            self._append("  Please install the missing libraries via the Download Boards/Libraries manager at the top-right of the window.", "info")
            return False

        return True

    def _validate_entry_points(self) -> bool:
        """Check that the project defines setup() and loop() exactly once.

        Arduino/ESP32 compiles all .ino files in the sketch folder into a
        single translation unit, so across the entire set of .ino files there
        must be EXACTLY ONE setup() and EXACTLY ONE loop().

        Rules:
        • Project-wide (all .ino files combined): exactly 1 setup(), 1 loop().
          - 0 of either  → error: missing entry point.
          - 2+ of either → error: duplicate definition, linker will reject it.
        • .cpp / .c files: checked project-wide only when no .ino files exist
          (pure C++ PlatformIO project).
        • .h files: never required to carry these; ignored for entry-point check.
        • A completely blank file (or whitespace-only) is flagged as a warning
          regardless of type.
        """
        # Matches a function *definition* (has opening brace), not a forward
        # declaration.  Allows optional whitespace everywhere.
        SETUP_RE = re.compile(r'\bvoid\s+setup\s*\(\s*\)\s*\{', re.MULTILINE)
        LOOP_RE  = re.compile(r'\bvoid\s+loop\s*\(\s*\)\s*\{', re.MULTILINE)

        all_source = get_project_root_source_files(
            self.sketch_dir_path, (".ino", ".cpp", ".c", ".h")
        )
        ino_files = [f for f in all_source if f.suffix.lower() == ".ino"]
        cpp_files = [f for f in all_source if f.suffix.lower() == ".cpp"]
        c_files   = [f for f in all_source if f.suffix.lower() == ".c"]
        h_files   = [f for f in all_source if f.suffix.lower() == ".h"]

        if not all_source:
            self._append("  ✖ No source files found in project folder.", "error")
            return False

        # ── Read all files ───────────────────────────────────────────────────
        file_contents: dict[Path, str] = {}
        blank_files:   list[Path]      = []

        for f in all_source:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                self._append(f"  ⚠ Could not read {f.name}: {e}", "warning")
                continue
            file_contents[f] = text
            if not text.strip():
                blank_files.append(f)

        # Warn about blank files (informational, not a hard stop on its own)
        for bf in blank_files:
            self._append(f"  ⚠ {bf.name} is empty — no code inside.", "warning")

        problems_found = False

        # ── .ino path: count definitions across ALL .ino files combined ──────
        if ino_files:
            # Track which file each definition lives in for useful error messages
            setup_owners: list[str] = []
            loop_owners:  list[str] = []

            for f in ino_files:
                text = file_contents.get(f, "")
                if SETUP_RE.search(text):
                    setup_owners.append(f.name)
                if LOOP_RE.search(text):
                    loop_owners.append(f.name)

            # ── setup() ─────────────────────────────────────────────────────
            if len(setup_owners) == 0:
                problems_found = True
                self._append("  ✖ No void setup() {} found in any .ino file.", "error")
                self._append(
                    "     Arduino requires exactly one setup() across all sketch files.",
                    "warning"
                )
                self._append("     Add this to your main .ino file:", "info")
                self._append("       void setup() { }", "dim")

            elif len(setup_owners) > 1:
                problems_found = True
                self._append(
                    f"  ✖ void setup() defined in {len(setup_owners)} files — only one is allowed:",
                    "error"
                )
                for fname in setup_owners:
                    self._append(f"       • {fname}", "warning")
                self._append(
                    "     Remove setup() from all but one file.", "info"
                )

            # ── loop() ──────────────────────────────────────────────────────
            if len(loop_owners) == 0:
                problems_found = True
                self._append("  ✖ No void loop() {} found in any .ino file.", "error")
                self._append(
                    "     Arduino requires exactly one loop() across all sketch files.",
                    "warning"
                )
                self._append("     Add this to your main .ino file:", "info")
                self._append("       void loop()  { }", "dim")

            elif len(loop_owners) > 1:
                problems_found = True
                self._append(
                    f"  ✖ void loop() defined in {len(loop_owners)} files — only one is allowed:",
                    "error"
                )
                for fname in loop_owners:
                    self._append(f"       • {fname}", "warning")
                self._append(
                    "     Remove loop() from all but one file.", "info"
                )

            # All good — show confirmation
            if not problems_found:
                owner_set = set(setup_owners) | set(loop_owners)
                owners_str = ", ".join(sorted(owner_set))
                ino_count  = len(ino_files)
                self._append(
                    f"  ✔ Entry points OK — setup()/loop() found in: {owners_str}"
                    + (f"  ({ino_count} .ino files total)" if ino_count > 1 else ""),
                    "success"
                )
                self._append_notif(
                    f"  ✔ Entry points OK — setup()/loop() found in: {owners_str}",
                    tag="success", category="entry_points", title="Entry Points Verified"
                )

        # ── Pure C++/C path (no .ino files) ─────────────────────────────────
        else:
            cpp_c_files   = cpp_files + c_files
            all_cpp_text  = "\n".join(file_contents.get(f, "") for f in cpp_c_files)
            project_has_setup = bool(SETUP_RE.search(all_cpp_text))
            project_has_loop  = bool(LOOP_RE.search(all_cpp_text))

            if not project_has_setup or not project_has_loop:
                problems_found = True
                missing = []
                if not project_has_setup:
                    missing.append("void setup() {}")
                if not project_has_loop:
                    missing.append("void loop() {}")
                self._append(
                    f"  ✖ Project is missing: {', '.join(missing)}", "error"
                )
                self._append(
                    "     No .ino file found; Arduino entry points must be defined "
                    "in a .cpp or .c source file.", "warning"
                )

        if problems_found:
            self._append("")
            self._append("  ✖ Fix the issues above before compiling.", "error")
            return False

        return True

    def _sync_src_dir(self, project_root: Path | None = None) -> None:
        """Freeze sketch sources into a local PlatformIO project ``src/``.

        PlatformIO's default src_dir is 'src/'.  We abandoned src_dir=. because
        InoToCPPConverter writes the intermediate .ino.cpp next to the .ino,
        but SCons looks for it under .pio/build/<env>/src/ — a path mismatch that
        causes 'no input files' from g++.  Using the default src/ layout fixes
        that: PlatformIO finds the .ino in src/, writes .ino.cpp there, and SCons
        correctly variant-copies it into the build tree.

        Each source is an independent copy, never a hard link.  That isolation is
        important for AI review: once this method returns, an OpenCode write to the
        original cannot mutate PlatformIO's input halfway through a build.  The
        caller brackets this copy with synchronous approval scans so a write before
        or during the freeze is reviewed before PlatformIO starts.

        Files are copied to a temporary sibling and atomically replaced so a stale
        hard link left by an older app version is broken without exposing a partial
        destination.  Entries with no matching source are removed as before.
        """
        # Read the remote source snapshot through the temporary mapped drive
        # when one is active.  PlatformIO still builds in the local staged
        # workspace, but this avoids repeated UNC round-trips while copying and
        # hashing the source boundary.
        source_root = self._mapped_or_sketch_dir(self.sketch_dir_path)
        destination_root = Path(project_root or source_root)
        is_remote = self._remote_workspace_root(source_root) is not None
        src_dir = destination_root / "src"
        destination_root.mkdir(parents=True, exist_ok=True)
        src_dir.mkdir(exist_ok=True)
        hide_generated_directory(src_dir)

        sketch_files = {
            path.name: path
            for path in get_project_root_source_files(
                source_root, (".ino", ".cpp", ".c", ".h", ".hpp")
            )
        }
        if not sketch_files and not source_root.is_dir():
            raise OSError(f"Could not read sketch directory '{source_root}'")

        # PlatformIO also needs the generated configuration beside the staged
        # src/ directory.  Keep the user-facing copy on the remote share, but
        # give the compiler a local copy for remote projects.
        source_ini = self._platformio_ini_path(source_root)
        destination_ini = destination_root / "platformio.ini"
        if destination_ini != source_ini and source_ini.is_file():
            ini_content = source_ini.read_text(encoding="utf-8", errors="replace")
            if (
                not destination_ini.is_file()
                or destination_ini.read_text(encoding="utf-8", errors="replace") != ini_content
            ):
                if not self._force_write_text(destination_ini, ini_content):
                    raise OSError(
                        f"Could not stage platformio.ini into '{destination_root}'"
                    )

        # Replace only changed sources.  Rewriting every staged file on every
        # action needlessly invalidated SCons nodes and generated .ino.cpp files
        # on low-end/slow storage.  Legacy hard links are still always replaced
        # so the frozen build boundary remains independent from editor writes.
        for name, src_path in sketch_files.items():
            if not is_remote:
                unhide_hidden_attribute(src_path)
            dst_path = src_dir / name
            should_replace = True
            if dst_path.is_file():
                try:
                    is_legacy_hardlink = os.path.samefile(src_path, dst_path)
                except OSError:
                    is_legacy_hardlink = False
                if not is_legacy_hardlink:
                    try:
                        src_stat = src_path.stat()
                        dst_stat = dst_path.stat()
                        if src_stat.st_size == dst_stat.st_size:
                            # Never trust timestamps here. FAT/exFAT can retain
                            # the same coarse mtime for a same-length edit; if
                            # that stale copy were compiled, the cache hash
                            # could incorrectly bless the wrong firmware.
                            should_replace = src_path.read_bytes() != dst_path.read_bytes()
                    except OSError:
                        should_replace = True
            if not should_replace:
                continue
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{name}.freeze-", suffix=".tmp", dir=str(src_dir)
            )
            os.close(file_descriptor)
            temporary_path = Path(temporary_name)
            try:
                import shutil as _sh
                # Antivirus/indexer scans can briefly hold either the source
                # snapshot or the temporary destination.  Retry only those
                # short-lived sharing violations; persistent errors still
                # surface normally to the compile worker.
                retry_transient_file_operation(
                    lambda: _sh.copy2(src_path, temporary_path)
                )
                ensure_file_writable(dst_path)
                retry_transient_file_operation(
                    lambda: os.replace(temporary_path, dst_path)
                )
            finally:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    # A scanner may still be releasing the temporary file;
                    # it is harmless and will be cleaned on the next staging
                    # pass rather than masking the real build result.
                    pass

        # Preserve PlatformIO's generated <sketch>.ino.cpp for unchanged .ino
        # files.  Deleting it on every action defeated incremental conversion
        # and made the old interruption detector erase valid SCons state.
        generated_ino_cpp = {
            f.name + ".cpp" for f in sketch_files.values() if f.suffix.lower() == ".ino"
        }
        allowed_names = set(sketch_files) | generated_ino_cpp

        # Remove stale entries that no longer have a source file.
        for dst_path in list(src_dir.iterdir()):
            if dst_path.name not in allowed_names:
                try:
                    dst_path.unlink()
                except OSError:
                    pass

