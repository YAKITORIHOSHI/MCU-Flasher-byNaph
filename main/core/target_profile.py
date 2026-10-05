"""Validate board identities before any build or hardware operation."""
from __future__ import annotations

import re
from typing import Mapping
from main.core.constants import MAX_BAUD_RATE

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def target_problem(info: Mapping, *, arduino_sketch: bool = False) -> str:
    if not info or info.get("pio_resolved") is False:
        return "No verified PlatformIO definition for this board. Install its platform package and select the exact board."
    for key in ("platform", "board"):
        value = str(info.get(key) or "").strip()
        if not _IDENTIFIER.fullmatch(value):
            return f"The board definition has no valid {key}. Install the correct platform package and select the board again."
    framework = str(info.get("framework") or "").strip()
    if not framework or not all(_IDENTIFIER.fullmatch(f.strip()) for f in framework.split(",")):
        return "Select a framework supported by this board in the board picker. Arduino .ino projects require Arduino."
    selected = {f.strip().lower() for f in framework.split(",")}
    unavailable = info.get("unavailable_frameworks")
    if isinstance(unavailable, Mapping):
        for name in sorted(selected):
            reason = unavailable.get(name)
            if isinstance(reason, str) and reason.strip():
                return f"{name} is unavailable for {info.get('platform')}:{info.get('board')}: {reason.strip()}"
    declared = {str(f).lower() for f in (info.get("frameworks") or [])}
    if declared and not selected.issubset(declared):
        return "The selected framework is not supported by this board. Choose one of its declared frameworks."
    if arduino_sketch and "arduino" not in selected:
        return "Arduino .ino files require an Arduino-compatible board and framework."
    protocol = str(info.get("upload_protocol") or "").strip()
    if protocol and not _IDENTIFIER.fullmatch(protocol):
        return "The board definition has an invalid upload protocol. Refresh its platform package."
    return ""


def requires_upload_port(info: Mapping) -> bool:
    """Use the manifest's transport contract; unknown transports require a port."""
    declared = info.get("require_upload_port")
    if isinstance(declared, bool):
        return declared
    protocol = str(info.get("upload_protocol") or "").lower()
    if protocol in {"stlink", "jlink", "cmsis-dap", "dfu", "picotool", "mbed", "uf2", "teensy-cli", "teensy-gui", "micronucleus", "wch-link", "nrfjprog", "atmel-ice", "usbasp", "usbtiny"}:
        return False
    return True


def upload_configuration(info: Mapping, requested_speed: str = "") -> str:
    """Preserve board bootloader defaults; only ESP serial paths expose baud overrides."""
    lines = []
    protocol = str(info.get("upload_protocol") or "").strip()
    if protocol:
        lines.append(f"upload_protocol = {protocol}")
    if str(info.get("platform")) in {"espressif32", "espressif8266"} and requires_upload_port(info):
        try:
            speed = int(requested_speed)
            if speed > 0:
                lines.append(f"upload_speed = {min(speed, MAX_BAUD_RATE)}")
        except (TypeError, ValueError):
            pass
    # Other builders read their own upload.speed from the exact board manifest.
    return "".join(line + "\n" for line in lines)


def upload_target_ready(info: Mapping, port: str = "") -> bool:
    """An explicit serial port or a verified native interface permits Upload."""
    return bool(port) or bool(info.get("board") and info.get("pio_resolved") is not False
                              and not requires_upload_port(info))
