"""Small, non-modal persistence helpers for explicit UI preference changes."""
from __future__ import annotations


def report_preference_failure(label: str) -> None:
    from main.qt.signals import signals
    signals.notification.emit({
        "title": "Setting not saved",
        "message": f"{label} could not be saved. Check that your configuration folder is writable and try again.",
        "type": "warning",
    })


def save_display_preference(key: str, value: bool, label: str) -> bool:
    from main.core.config import load_gui_config, save_gui_config
    try:
        config = load_gui_config()
        config[key] = bool(value)
        if save_gui_config(config, shared_updates={key: bool(value)}) is not False:
            return True
    except Exception:
        pass
    report_preference_failure(label)
    return False


def restore_checkbox(checkbox, checked: bool) -> None:
    previous = checkbox.blockSignals(True)
    checkbox.setChecked(checked)
    checkbox.blockSignals(previous)
