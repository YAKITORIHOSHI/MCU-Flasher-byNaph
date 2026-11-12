#!/usr/bin/env python3
"""Ubuntu's Bootstrap window: the Windows setup view, fed by the native launcher.

The launcher (system Python) does every setup, repair and update step itself. It
starts this process with the private runtime's Python and writes one JSON event per
line on stdin: ``{"event": "log", "args": [text, tag]}`` and the other
``BootstrapDispatcher`` names, plus ``finish``, ``cancellable`` and ``subtitle``.
This window only displays them, so it can never change what setup does.

Closing it is allowed while a source download runs (that skips the update, and the
launcher notices the window has gone) and after setup has finished or failed.
During setup it stays open and says so. A failure stays on screen until closed;
success closes it by itself. End of input without ``finish`` closes it too.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DISPLAY_EVENTS = frozenset(("log", "status", "progress", "stop_spinner", "update_block",
                            "commit_block", "clear_block"))
# A finished window stays this long so "Opening MCU Flasher…" can be read.
SUCCESS_CLOSE_DELAY_MS = 700
_PALETTE_KEYS = {
    "BG_DARKEST": "#0a0e14", "BG_DARK": "#10151c", "BG_MID": "#161d27", "BG_LIGHT": "#1c2532",
    "BG_HOVER": "#243040", "BORDER": "#2a3545", "TEXT": "#c8d2dc", "TEXT_DIM": "#6b7d94",
    "TEXT_BRIGHT": "#e8edf3", "CYAN": "#00d2ff", "GREEN": "#5ccc6e", "YELLOW": "#e8b83a",
    "RED": "#f05050", "MAGENTA": "#c678dd",
}


def resolve_theme() -> tuple[dict, str]:
    """The workspace's own theme, named as the setup view expects (``T_`` keys)."""
    mode = "default"
    palette: dict = {}
    try:
        from main.core.config import get_theme_mode
        from main.core.theme import Theme
        mode = get_theme_mode()
        palette = Theme.PALETTES.get(mode, Theme.PALETTES.get("default", {}))
    except Exception:
        pass
    result = {f"T_{key}": palette.get(key, fallback) for key, fallback in _PALETTE_KEYS.items()}
    result["T_BORDER_LIT"] = palette.get("BORDER_LIT", result["T_CYAN"])
    return result, mode


def record_exception(context: str) -> None:
    print(context, file=sys.stderr, flush=True)


def _config_path() -> Path:
    return ROOT / "src" / "dbs" / "bootstrap_config.json"


def load_config() -> dict:
    try:
        value = json.loads(_config_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(value: dict) -> bool:
    try:
        path = _config_path()
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return True
    except OSError:
        return False


def build_window(subtitle: str):
    """Create the shared setup dialog inside an existing QApplication."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from src.modules.bootstrap_dispatch import BootstrapDispatcher
    from src.modules.bootstrap_qt import BootstrapDialog

    class UbuntuBootstrapDialog(BootstrapDialog):
        def __init__(self, state, context):
            super().__init__(state, context)
            self.cancellable = False
            self.finished = False
            self._close_explained = False

        def closeEvent(self, event):
            if self._allow_close or self.cancellable or self.finished:
                self._allow_close = True
                event.accept()
                QTimer.singleShot(0, QApplication.quit)
                return
            event.ignore()
            if not self._close_explained:
                self._close_explained = True
                self._on_log("Setup is still running. This window closes by itself when it finishes.", "warn")

        def set_cancellable(self, value):
            self.cancellable = bool(value)

        def set_subtitle(self, text):
            self.sub_lbl.setText(str(text)[:240])

        def finish_setup(self, ok, text):
            self.cancellable = False
            self.finished = True
            if ok:
                self._on_progress(100.0)
            self._on_stop_spinner(str(text or ("Ready" if ok else "Setup failed")), bool(ok))
            if ok:
                QTimer.singleShot(SUCCESS_CLOSE_DELAY_MS, self._on_close)
            else:
                # Keep the failure readable: bring the window forward and leave it open.
                self.raise_()
                self.activateWindow()

        def input_closed(self):
            if not self.finished:
                self._on_close()

    state = SimpleNamespace(_start_time=time.time(), _status_text="Starting…", _overall_progress=0.0,
                            _skip_updates=False, _closed=False, _signals=BootstrapDispatcher())
    palette, mode = resolve_theme()
    context = SimpleNamespace(root=ROOT, palette=palette, resolve_theme=lambda: (palette, mode),
                              load_config=load_config, save_config=save_config,
                              record_exception=record_exception)
    window = UbuntuBootstrapDialog(state, context)
    # Updates are always on: there is no choice to make in this window.
    window.skip_cb.hide()
    window.set_subtitle(subtitle)
    return window, state


def forward_event(line: str, state, window) -> None:
    """Pass one launcher event to the GUI thread; unknown or malformed lines are ignored."""
    try:
        record = json.loads(line)
        name, args = record["event"], record.get("args", [])
        if not isinstance(args, list):
            return
    except (ValueError, KeyError, TypeError):
        return
    calls = {"finish": window.finish_setup, "cancellable": window.set_cancellable,
             "subtitle": window.set_subtitle}
    if name in DISPLAY_EVENTS:
        state._signals.emit(name, tuple(args))
    elif name in calls:
        state._signals.emit("call", (calls[name], tuple(args)))


def read_events(stream, state, window) -> None:
    """Forward the launcher's events until its pipe closes; never act on them here."""
    try:
        for line in stream:
            forward_event(line, state, window)
    except (OSError, ValueError):
        pass
    state._signals.emit("call", (window.input_closed, ()))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subtitle", default="Runtime setup · Prepare your workspace")
    args = parser.parse_args(argv)
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtWidgets import QApplication
    QGuiApplication.setDesktopFileName("mcu-flasher")
    app = QApplication.instance() or QApplication([sys.argv[0] if sys.argv else "MCU Flasher"])
    app.setApplicationDisplayName("MCU Flasher by Naph")
    try:
        from main.qt.theme import install_no_focus_lines, register_fonts
        register_fonts()
        install_no_focus_lines(app)
    except Exception as error:
        record_exception(f"Bootstrap window styling unavailable: {error}")
    font = QFont("Montserrat", 10)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)
    window, state = build_window(args.subtitle)
    window.show()
    app.processEvents()
    window.bind_dispatcher()
    threading.Thread(target=read_events, args=(sys.stdin, state, window),
                     name="MCUBootstrapEvents", daemon=True).start()
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
