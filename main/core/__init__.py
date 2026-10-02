"""Lazy core exports: selecting a host must not import another host or discover boards."""
from __future__ import annotations
import importlib

_PUBLIC_MODULES = ("constants", "theme", "config", "file_utils", "toolchain", "board_catalog", "board_compat")
_MODULES = _PUBLIC_MODULES + ("build_resources", "target_profile", "config_store")


def __getattr__(name):
    if name in _MODULES:
        module = importlib.import_module(f"main.core.{name}")
        globals()[name] = module
        return module
    if name == "__all__":
        return list(dict.fromkeys(item for module in _PUBLIC_MODULES
                    for item in getattr(importlib.import_module(f"main.core.{module}"), "__all__", [])))
    if name.startswith("__"):
        raise AttributeError(name)
    for module_name in _PUBLIC_MODULES:
        module = importlib.import_module(f"main.core.{module_name}")
        if name in getattr(module, "__all__", ()):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(name)
