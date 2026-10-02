"""Stable toolchain API; host implementations live in separate platform files."""
from main.core import build_resources
from main.platforms import get_platform_backend

_implementation = get_platform_backend()
__all__ = list(dict.fromkeys(build_resources.__all__ + _implementation.__all__))
for _name in __all__:
    globals()[_name] = getattr(_implementation, _name, None) if _name in _implementation.__all__ else getattr(build_resources, _name)


def __getattr__(name):
    # Keep existing callers of internal Windows helpers compatible while their
    # mutable state remains owned by the selected implementation.
    return getattr(_implementation, name)
