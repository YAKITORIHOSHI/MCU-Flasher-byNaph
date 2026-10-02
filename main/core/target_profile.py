"""Validate board identities before any build or hardware operation."""
from __future__ import annotations

import re
from typing import Mapping

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
    if arduino_sketch and "arduino" not in {f.strip().lower() for f in framework.split(",")}:
        return "Arduino .ino files require an Arduino-compatible board and framework."
    return ""


def requires_upload_port(info: Mapping) -> bool:
    """Serial families need an explicit port; native programmers resolve in PIO."""
    protocol = str(info.get("upload_protocol") or "").lower()
    if protocol in {"stlink", "jlink", "cmsis-dap", "dfu", "picotool", "mbed", "uf2", "teensy-cli"}:
        return False
    return str(info.get("platform") or "").lower() in {"atmelavr", "espressif32", "espressif8266"} or protocol in {"serial", "esptool", "avrdude", "sam-ba", "bossac"}
