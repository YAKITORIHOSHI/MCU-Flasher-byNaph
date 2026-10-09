"""Resolve Ubuntu recovery tools only from the application's prepared runtime."""
from __future__ import annotations

import importlib.util
from importlib import metadata
import sys


def esptool_command() -> list[str]:
    from src.modules.private_python_guard import is_running_private_python
    if not is_running_private_python():
        raise RuntimeError("Ubuntu Reset requires MCU Flasher's private Python runtime. Relaunch MCU Flasher.")
    if importlib.util.find_spec("esptool") is None:
        from src.modules.offline_runtime import bootstrap_instruction
        raise RuntimeError(bootstrap_instruction("Ubuntu's ESP recovery tool is not prepared"))
    # The GUI's recovery tool is independent of PlatformIO's framework-pinned
    # uploader packages. Never reuse a copied Windows executable or PATH shim.
    return [sys.executable, "-m", "esptool"]


def subcommand(name: str) -> str:
    """Use v5 command names while retaining the prepared v4 runtime's syntax."""
    aliases = {"erase_flash": "erase-flash", "image_info": "image-info"}
    if name not in aliases:
        return name
    try:
        major = int(metadata.version("esptool").split(".", 1)[0])
    except (metadata.PackageNotFoundError, ValueError, OSError):
        return name
    return aliases[name] if major >= 5 else name


def connect_chip(module, **arguments):
    """Replace v5's deprecated alias without changing exact-port probe behavior."""
    connector = getattr(module, "connect_first_available", None)
    if not callable(connector):
        connector = module.get_default_connected_device
    return connector(**arguments)
