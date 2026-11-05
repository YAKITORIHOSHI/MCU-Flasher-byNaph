#!/usr/bin/env python3
"""Every monitor shape: real workspace and dialogs, nothing cut off, nothing shaking.

Each monitor runs in a fresh process with a real QScreen of that size, through
Qt's offscreen platform and QT_SCALE_FACTOR, so geometry goes through the same
code paths as a desktop instead of a mocked work area. A workspace is checked at
its default size, tiled to half width, tiled to half height and full screen, with
and without the assistant. Persistence, hardware and services are mocked and no
window can reach the desktop. Native window managers need their own evidence: see
verify_window_alignment.py and verify_ui_layout.py.

Default run: the core monitors. --all sweeps every monitor. --monitor runs one
and prints its measurements. --render-dir saves a capture of every window state.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]

# Physical width, height, display scale and the height a taskbar reserves.
CORE_MONITORS = (
    (800, 600, 1, 48), (1024, 768, 1, 48), (1280, 720, 1.5, 48), (1280, 720, 2, 48),
    (1366, 768, 1, 40), (1366, 768, 1.25, 48), (1366, 768, 2, 48), (1920, 1080, 1, 48),
    (1920, 1080, 1.5, 48), (2560, 1440, 1.25, 48), (3440, 1440, 1, 48), (3840, 2160, 1, 48),
    (5120, 1440, 1, 48), (1080, 1920, 1, 48), (2160, 3840, 2, 48), (768, 1366, 1, 48),
)
OTHER_MONITORS = (
    (1024, 600, 1, 40), (1280, 720, 1, 48), (1280, 720, 1.25, 48), (1280, 800, 1, 48), (1280, 800, 1.5, 48),
    (1366, 768, 1.5, 48), (1366, 768, 1.75, 48), (1440, 900, 1, 48), (1536, 864, 1, 48), (1600, 900, 1, 48),
    (1680, 1050, 1, 48), (1920, 1080, 1.25, 48), (1920, 1080, 1.75, 48), (1920, 1080, 2, 48), (1920, 1200, 1, 48),
    (2256, 1504, 1.5, 48), (2560, 1080, 1, 48), (2560, 1440, 1, 48), (2560, 1440, 1.5, 48), (2880, 1800, 2, 48),
    (3840, 1600, 1, 48), (3840, 2160, 1.5, 48), (3840, 2160, 2, 48), (5120, 2880, 2, 48), (7680, 4320, 1, 48),
    (1080, 1920, 1.25, 48), (1200, 1920, 1, 48), (1440, 2560, 1, 48), (800, 1280, 1, 48),
)


def monitor_name(monitor) -> str:
    width, height, scale, _reserved = monitor
    return f"{width}x{height}@{round(scale * 100)}"


# ── One monitor, in its own process ──────────────────────────────────────────

def check_monitor(render_dir: Path | None) -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "direct"))
    from unittest.mock import Mock, patch
    from PySide6.QtCore import QEvent, QObject, QPoint, QRect
    from PySide6.QtWidgets import (QAbstractButton, QAbstractScrollArea, QApplication, QComboBox, QLineEdit,
                                   QPlainTextEdit, QTextEdit, QToolButton, QWidget)

    app = QApplication.instance() or QApplication(["verify-monitor-matrix"])
    from main.qt.garbage_collection import install_gui_garbage_collector
    collector = install_gui_garbage_collector(app)
    from verify_responsive import QtResponsiveChecks, bootstrap_fixture
    from verify_runtime import PreviewBackend
    from main.core import config, file_utils
    from main.qt.main_window import MCUMainWindow

    problems: list[tuple[str, str]] = []
    measured: list[dict] = []

    def pump(seconds=.2):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(.003)

    def clipped(widget, stop):
        """Name the first ancestor cutting this widget off; scrolled content can be reached."""
        owner = widget
        while owner is not stop and owner is not None:
            parent = owner.parentWidget()
            if parent is None:
                return "has no parent"
            if isinstance(parent.parentWidget(), QAbstractScrollArea) and parent is parent.parentWidget().viewport():
                return None
            rect = QRect(owner.mapTo(parent, QPoint()), owner.size())
            if not parent.rect().contains(rect):
                return f"{type(owner).__name__}{rect.getRect()} outside {type(parent).__name__}{parent.rect().getRect()}"
            owner = parent
        return None

    def require_reachable(state, widget, stop, name):
        if not widget.isVisibleTo(stop):
            problems.append((state, f"{name} is hidden"))
            return
        cut = clipped(widget, stop)
        if cut:
            problems.append((state, f"{name} is cut off: {cut}"))

    def capture(widget, name):
        if render_dir is not None:
            assert widget.grab().save(str(render_dir / f"{name}.png"))

    def require_on_screen(state, window):
        area = window.screen().availableGeometry()
        if not area.contains(window.frameGeometry()):
            problems.append((state, f"frame {window.frameGeometry().getRect()} leaves work area {area.getRect()}"))

    class Moves(QObject):
        def __init__(self):
            super().__init__()
            self.count = 0
            self.heights = set()

        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
                self.count += 1
            return False

    def check_workspace(window, state):
        tabs, bar = window._bottom_tabs, window._bottom_tabs.tabBar()
        toolbar, controls = window._primary_toolbar, window._controls_bar
        require_on_screen(state, window)
        for name in ("btn_compile", "btn_upload", "btn_project", "btn_cloud", "btn_download"):
            require_reachable(state, getattr(toolbar, name), window, name)
        for name in ("board_selector", "port_combo", "upload_speed_combo"):
            require_reachable(state, getattr(controls, name), window, name)
        if any(button.isVisible() for button in toolbar.findChildren(QToolButton, "qt_toolbar_ext_button")):
            problems.append((state, "native toolbar overflow is hiding actions"))
        if any(button.isVisible() for button in bar.findChildren(QToolButton)):
            problems.append((state, f"tool tabs need scrolling at {bar.width()} wide"))
        if bar.tabRect(tabs.count() - 1).right() > bar.width():
            problems.append((state, f"tool tabs end at {bar.tabRect(tabs.count() - 1).right()} of {bar.width()}"))
        if not bar.tabRect(0).width() or any(not bar.tabToolTip(index) for index in range(tabs.count())):
            problems.append((state, "a tool tab lost its name"))
        # The reported defect: switching tools must not move anything around them.
        moves = Moves()
        outside = (window, toolbar, window._controls_toolbar, controls, window.centralWidget(), window._h_splitter,
                   window._v_splitter, window._editor_area, window._editor_panel, tabs, bar, window.statusBar())
        for widget in outside:
            widget.installEventFilter(moves)
        lines = {}
        original = tabs.currentIndex()
        for index in [*range(tabs.count()), original]:
            divider = window._editor_area.height()
            strip = [bar.tabRect(tab).getRect() for tab in range(tabs.count())]
            tabs.setCurrentIndex(index)
            pump(.12)
            if window._editor_area.height() != divider:
                problems.append((state, f"divider moved on tab {index}: {divider} to {window._editor_area.height()}"))
            if [bar.tabRect(tab).getRect() for tab in range(tabs.count())] != strip:
                problems.append((state, f"tool tabs moved on tab {index}"))
            page = tabs.currentWidget()
            for control in page.findChildren(QWidget):
                if (isinstance(control, (QAbstractButton, QComboBox, QLineEdit)) and control.isVisibleTo(page)
                        and not control.objectName().startswith("qt_")):
                    cut = clipped(control, page)
                    if cut:
                        problems.append((state, f"tab {index} {type(control).__name__} {control.objectName()!r} is cut off: {cut}"))
            view = {0: window._console_panel, 1: window._serial_panel._output}.get(index)
            if view is not None:
                lines[index] = view.viewport().height() // max(1, view.fontMetrics().lineSpacing())
                if not lines[index]:
                    problems.append((state, f"tab {index} shows no complete line: {view.viewport().height()}px"))
            if index == 1:
                capture(window, f"{state}-serial")
        for widget in outside:
            widget.removeEventFilter(moves)
        if moves.count:
            problems.append((state, f"{moves.count} moves or resizes outside the tool pane while switching tabs"))
        capture(window, state)
        measured.append(dict(state=state, window=window.size().toTuple(), minimum=window.minimumSize().toTuple(),
                             editor=window._editor_area.height(), tools=tabs.height(), tab=tabs.tabText(0),
                             toolbar_compact=toolbar.is_compact(), controls_row=controls._row_mode, lines=lines))

    def check_dialog(state, dialog, *pages):
        dialog.show()
        pump(.3)
        for page in pages or (None,):
            if page is not None:
                page()
                pump(.12)
            require_on_screen(state, dialog)
            for button in dialog.findChildren(QAbstractButton):
                if button.isVisibleTo(dialog) and not button.objectName().startswith(("qt_", "Scroll")):
                    cut = clipped(button, dialog)
                    if cut:
                        problems.append((state, f"{type(button).__name__} {button.text()!r} is cut off: {cut}"))
            for reading in dialog.findChildren(QAbstractScrollArea):
                if isinstance(reading, (QPlainTextEdit, QTextEdit)) and reading.isVisibleTo(dialog):
                    if reading.viewport().height() < reading.fontMetrics().lineSpacing():
                        problems.append((state, f"{reading.objectName()!r} shows no complete line"))
        capture(dialog, state)
        dialog.hide()

    screen = app.primaryScreen()
    fixture = QtResponsiveChecks("test_coordinate_bounds")
    fixture.setUp()
    with patch.object(file_utils, "hide_internal_project_metadata"), \
         patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}), \
         patch.object(config, "_save_raw_config"), patch.object(config, "save_gui_config"), \
         patch.object(config, "get_theme_mode", return_value="default"), \
         patch.object(config, "focus_project_window", return_value=False):
        backend = PreviewBackend()
        backend.start_services = Mock()
        window = MCUMainWindow(backend)
        window._ai_panel.ensure_started = lambda: None
        window.show()
        pump(1.0)
        window._primary_toolbar.update_sketch_label("/home/user/Documents/Sketches/weather_station_v3")
        pump(.2)
        check_workspace(window, "default")
        area = screen.availableGeometry()
        frame_w = window.frameGeometry().width() - window.geometry().width()
        frame_h = window.frameGeometry().height() - window.geometry().height()

        def place(width, height):
            window.setGeometry(area.x() + frame_w // 2, area.y() + frame_h // 2, width - frame_w, height - frame_h)
            pump(.3)

        place(area.width() // 2, area.height())
        if window.frameGeometry().width() > area.width() // 2:
            problems.append(("half", f"a half-monitor frame was refused: {window.frameGeometry().width()} of {area.width()}"))
        check_workspace(window, "half")
        place(area.width(), area.height() // 2)
        check_workspace(window, "half-height")
        place(area.width(), area.height())
        check_workspace(window, "full")
        # Directly from full screen: the narrowest window anyone can drag to.
        window.resize(window.minimumWidth(), window.minimumHeight())
        pump(.3)
        check_workspace(window, "smallest")
        place(area.width(), area.height())

        # Opening the assistant keeps a divider the user placed.
        panes = window._v_splitter.sizes()
        window._v_splitter.setSizes([panes[0] - 10, panes[1] + 10])
        pump(.1)
        placed = window._editor_area.height()
        window.toggle_ai_panel(True)
        pump(.35)
        # A narrower tool pane can genuinely need more height for its reflowed controls.
        tools = window._bottom_tabs
        if (window._h_splitter.orientation().name == "Horizontal" and window._editor_area.height() != placed
                and tools.height() > tools.minimumHeight()):
            problems.append(("full-ai", f"opening the assistant moved the divider: {placed} to {window._editor_area.height()}"))
        check_workspace(window, "full-ai")
        place(area.width() // 2, area.height())
        check_workspace(window, "half-ai")
        window.toggle_ai_panel(False)
        place(area.width(), area.height())

        # Themes must not bump the toolbars either.
        heights = set()

        class Heights(QObject):
            def eventFilter(self, watched, event):
                if event.type() == QEvent.Type.Resize:
                    heights.add(event.size().height())
                return False

        watcher = Heights()
        before = window._primary_toolbar.height()
        window._primary_toolbar.installEventFilter(watcher)
        for mode in ("light", "solarized_dark", "default"):
            window._on_theme_changed(mode)
            pump(.15)
        window._primary_toolbar.removeEventFilter(watcher)
        if heights - {before}:
            problems.append(("themes", f"changing theme bumped the primary toolbar to {sorted(heights)} from {before}"))

        window.detach_editor()
        pump(.3)
        require_on_screen("detached", window._detached_window)
        require_on_screen("detached-main", window)
        for name in ("btn_compile", "btn_upload", "btn_project", "btn_download"):
            require_reachable("detached", getattr(window._primary_toolbar, name), window._detached_window, name)
        capture(window._detached_window, "detached")
        window.attach_editor()
        pump(.3)

    # ── Dialogs and auxiliary screens ────────────────────────────────────────
    from main.qt.board_dialog import BoardSearchDialog
    from main.qt.modify_dialog import ModifyFilesDialog
    from main.qt.project_dialog import ProjectDialog
    from main.qt.project_search import ProjectSearchDialog
    from main.qt import board_dialog
    check_dialog("settings", fixture.settings())
    with patch.object(board_dialog, "load_recent_boards", return_value=[]):
        check_dialog("boards", fixture.own(BoardSearchDialog(board_list=["ESP32-S3-DevKitC-1-N8R8 (8 MB QD, 8 MB PSRAM)", "Demo target"])))
    check_dialog("modify", fixture.own(ModifyFilesDialog(backend)))
    check_dialog("find-all", fixture.own(ProjectSearchDialog(ROOT / "temp/scratch/preview-project", dict, Mock())))
    with patch("main.core.config.get_open_projects", return_value=[]), \
         patch("main.core.config.get_theme_mode", return_value="default"):
        selector = fixture.own(ProjectDialog(initial_dir=""))
        frames = set()

        def show_page(index):
            selector._tabs.setCurrentIndex(index)
            pump(.1)
            frames.add(selector.frameGeometry().getRect())

        check_dialog("project", selector, *(lambda index=index: show_page(index)
                                            for index in range(selector._tabs.count())))
        if len(frames) > 1:
            problems.append(("project", f"the dialog moved between its tabs: {sorted(frames)}"))
    setup, _gui, _namespace = bootstrap_fixture()
    fixture.own(setup)
    setup._on_status("Preparing the selected board tools")
    setup._on_log("Private Python runtime ready", "ok")
    setup._on_update_block("package_progress", "  – Unpacking 60%\n")
    setup._on_progress(60)
    check_dialog("setup", setup)
    setup._set_details_expanded(True)
    check_dialog("setup-details", setup)

    print(json.dumps(dict(screen=screen.availableGeometry().size().toTuple(), scale=screen.devicePixelRatio(),
                          measured=measured, problems=problems)), flush=True)
    sys.stdout.flush()
    collector.stop() if hasattr(collector, "stop") else None
    # Everything is reported. Qt's interpreter teardown adds nothing to this check.
    os._exit(0)


# ── The sweep ────────────────────────────────────────────────────────────────

def run_monitor(monitor, config_dir: Path, render_dir: Path | None, timeout: int):
    width, height, scale, reserved = monitor
    name = monitor_name(monitor)
    config_file = config_dir / f"{name}.json"
    config_file.write_text(json.dumps({
        "synchronousWindowSystemEvents": False, "windowFrameMargins": True,
        "screens": [{"name": name, "x": 0, "y": 0, "width": width, "height": height - reserved,
                     "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if key not in ("DISPLAY", "WAYLAND_DISPLAY")}
    environment.update(QT_QPA_PLATFORM=f"offscreen:configfile={config_file}", QT_SCALE_FACTOR=str(scale),
                       QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu", PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--monitor"]
    if render_dir is not None:
        target = render_dir / name
        target.mkdir(parents=True, exist_ok=True)
        command += ["--render-dir", str(target)]
    try:
        completed = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True,
                                   errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return name, None, f"timed out after {timeout} seconds"
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            return name, json.loads(line), ""
    return name, None, (completed.stderr or completed.stdout)[-1500:] or f"exit code {completed.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--monitor", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--all", action="store_true", help="Sweep every monitor, not only the core set")
    parser.add_argument("--only", action="append", default=[], help="Run monitors whose name contains this text")
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--jobs", type=int, default=max(2, min(6, (os.cpu_count() or 4) // 2)))
    parser.add_argument("--timeout", type=int, default=180)
    arguments = parser.parse_args()
    if arguments.monitor:
        return check_monitor(arguments.render_dir)
    monitors = (*CORE_MONITORS, *OTHER_MONITORS) if arguments.all or arguments.only else CORE_MONITORS
    if arguments.only:
        monitors = tuple(item for item in monitors if any(part in monitor_name(item) for part in arguments.only))
    if arguments.render_dir:
        render_dir = arguments.render_dir.resolve()
        if not render_dir.is_relative_to((ROOT / "temp").resolve()):
            parser.error("Captures must stay inside this checkout's temp directory")
    else:
        render_dir = None
    audit = ROOT / "temp/audit/monitor-matrix"
    audit.mkdir(parents=True, exist_ok=True)
    failures = 0
    with tempfile.TemporaryDirectory(dir=audit) as directory, ThreadPoolExecutor(arguments.jobs) as pool:
        for name, report, error in pool.map(
                lambda monitor: run_monitor(monitor, Path(directory), render_dir, arguments.timeout), monitors):
            if report is None:
                failures += 1
                print(f"FAIL: {name} could not finish: {error}")
                continue
            for state, problem in report["problems"]:
                failures += 1
                print(f"FAIL: {name}/{state}: {problem}")
            default = report["measured"][0]
            print(f"{name:15} work area {report['screen'][0]:>4}x{report['screen'][1]:<4} "
                  f"window {default['window'][0]:>4}x{default['window'][1]:<4} "
                  f"minimum {default['minimum'][0]:>3}x{default['minimum'][1]:<3} editor {default['editor']:>4}px "
                  f"lines {sorted(default['lines'].values())} problems {len(report['problems'])}", flush=True)
    if failures:
        print(f"FAILED: {failures} problems across {len(monitors)} monitors")
        return 1
    print(f"OK: {len(monitors)} monitors, every window state and dialog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
