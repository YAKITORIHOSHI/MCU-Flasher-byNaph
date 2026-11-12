"""Interface size: one scale for everything in the window, chosen per monitor or by the user.

This uses Qt's own QT_SCALE_FACTOR, so icons, fonts, spacing and the editor stay in
proportion and take the same fractional-scale path as a Windows display set to 125%. Qt
reads the variable once, while the application is created, and a monitor can only be
measured after that. Automatic therefore measures now and applies what it finds next time.
"""
from __future__ import annotations

import os

from src.modules.ui_metrics import interface_scale

INTERFACE_SIZES = (("Automatic", "auto"), ("Compact (87.5%)", .875), ("Normal (100%)", 1.0),
                   ("Large (112.5%)", 1.125), ("Larger (125%)", 1.25), ("Largest (150%)", 1.5))
_CHOICES = {value for _label, value in INTERFACE_SIZES}
_VARIABLE = "QT_SCALE_FACTOR"
# What this process started with: the factor Qt applies, and whether the
# environment (a person or a verifier) chose it instead of the setting.
_STARTED = {"factor": 1.0, "external": False, "prepared": False}


def _shared() -> dict:
    from main.core.config import _load_raw_config
    try:
        shared = _load_raw_config().get("shared", {})
    except Exception:
        return {}
    return shared if isinstance(shared, dict) else {}


def interface_choice() -> object:
    choice = _shared().get("interface_size", "auto")
    return choice if choice in _CHOICES else "auto"


def effective_scale(choice: object = None) -> float:
    """The factor a window opened now would use for this choice (default: the saved one)."""
    choice = interface_choice() if choice is None else choice
    if choice == "auto":
        try:
            choice = float(_shared().get("interface_scale_auto", 1.0))
        except (TypeError, ValueError):
            choice = 1.0
        if choice not in _CHOICES:
            choice = 1.0
    return float(choice)


def started_scale() -> float:
    """The interface factor this process is running at."""
    return float(_STARTED["factor"])


def environment_override() -> bool:
    """True when QT_SCALE_FACTOR came from the environment, so the setting cannot apply."""
    return bool(_STARTED["external"])


def restart_can_apply() -> bool:
    """This process started through prepare_interface_size, so a restart applies the setting."""
    return bool(_STARTED["prepared"]) and not environment_override()


def prepare_interface_size() -> float:
    """Call just before creating the QApplication. Returns the factor to pass to settle."""
    if _VARIABLE in os.environ:
        # Whoever set it decides: a person, or a verifier emulating a monitor.
        try:
            _STARTED.update(factor=float(os.environ[_VARIABLE]), external=True, prepared=True)
        except ValueError:
            _STARTED.update(factor=1.0, external=True, prepared=True)
        return 0.0
    choice = effective_scale()
    if choice != 1.0:
        os.environ[_VARIABLE] = str(choice)
    _STARTED.update(factor=choice, external=False, prepared=True)
    return float(choice)


def automatic_scale_for(screen, applied: float | None = None) -> float:
    """What Automatic chooses for this screen, measured in this process's logical units."""
    applied = started_scale() if applied is None else applied
    area = screen.availableGeometry()
    return interface_scale(area.width() * applied, area.height() * applied,
                           screen.physicalDotsPerInch() * applied, screen.devicePixelRatio() / applied)


def settle_interface_size(app, applied: float) -> None:
    """Call right after. Child processes choose for themselves; Automatic records this monitor."""
    if not applied:
        return
    os.environ.pop(_VARIABLE, None)
    screen = app.primaryScreen()
    if screen is None or interface_choice() != "auto":
        return
    wanted = automatic_scale_for(screen, applied)
    if wanted != _shared().get("interface_scale_auto", 1.0):
        from main.core.config import _load_raw_config, _save_raw_config
        try:
            data = _load_raw_config()
            data.setdefault("shared", {})["interface_scale_auto"] = wanted
            _save_raw_config(data)
        except Exception:
            pass  # Only the next launch's size depends on this.
