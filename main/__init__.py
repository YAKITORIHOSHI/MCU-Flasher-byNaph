"""Public entry points, loaded only when requested."""
from __future__ import annotations

__all__ = ["main", "MCUWebBackendAPI"]


def __getattr__(name):
    if name == "main":
        from main.mcu_flash_gui import main
        return main
    if name == "MCUWebBackendAPI":
        from main.web_bridge import MCUWebBackendAPI
        return MCUWebBackendAPI
    raise AttributeError(name)
