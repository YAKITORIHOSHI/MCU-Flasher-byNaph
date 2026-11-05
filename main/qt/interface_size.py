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


def prepare_interface_size() -> float:
    """Call just before creating the QApplication. Returns the factor to pass to settle."""
    if _VARIABLE in os.environ:
        return 0.0  # Whoever set it decides: a person, or a verifier emulating a monitor.
    choice = interface_choice()
    if choice == "auto":
        try:
            choice = float(_shared().get("interface_scale_auto", 1.0))
        except (TypeError, ValueError):
            choice = 1.0
        if choice not in _CHOICES:
            choice = 1.0
    if choice != 1.0:
        os.environ[_VARIABLE] = str(choice)
    return float(choice)


def settle_interface_size(app, applied: float) -> None:
    """Call right after. Child processes choose for themselves; Automatic records this monitor."""
    if not applied:
        return
    os.environ.pop(_VARIABLE, None)
    screen = app.primaryScreen()
    if screen is None or interface_choice() != "auto":
        return
    area = screen.availableGeometry()
    wanted = interface_scale(area.width() * applied, area.height() * applied,
                             screen.physicalDotsPerInch() * applied, screen.devicePixelRatio() / applied)
    if wanted != _shared().get("interface_scale_auto", 1.0):
        from main.core.config import _load_raw_config, _save_raw_config
        try:
            data = _load_raw_config()
            data.setdefault("shared", {})["interface_scale_auto"] = wanted
            _save_raw_config(data)
        except Exception:
            pass  # Only the next launch's size depends on this.
