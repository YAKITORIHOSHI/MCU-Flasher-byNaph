"""Recognize dependency failures that the host Bootstrap can repair."""
from __future__ import annotations

_RUNTIME_MODULES = frozenset({
    "PySide6", "shiboken6", "psutil", "requests", "serial", "urllib3",
    "certifi", "charset_normalizer", "idna",
})


def bootstrap_can_repair(error: Exception) -> bool:
    """Wrong source imports and user files must not trigger installer recovery."""
    name = str(getattr(error, "name", "") or "").split(".", 1)[0]
    if isinstance(error, ModuleNotFoundError):
        return name in _RUNTIME_MODULES
    if not isinstance(error, (ImportError, OSError)):
        return False
    message = str(error).casefold()
    loader_failure = any(marker in message for marker in (
        "dll load failed", "cannot open shared object file", "library not loaded",
        "could not load library", "failed to load library",
    ))
    known_runtime = name in _RUNTIME_MODULES or any(
        module.casefold() in message for module in ("PySide6", "shiboken6")
    )
    return loader_failure and known_runtime
