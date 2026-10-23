#!/usr/bin/env python3
"""Exercise real workspace/WebEngine close ownership and normal process exit.

Only owned preview data is used. No backend service, settings write, device,
download manager or project metadata operation is allowed by this fixture.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")


def verify_case(app, scenario, fixture_root, *, last_window=False):
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QMessageBox
    from shiboken6 import isValid
    from direct.verify_runtime import PreviewBackend
    from main.core import config, file_utils
    from main.qt.main_window import MCUMainWindow

    with ExitStack() as stack:
        for name, value in (("_load_raw_config", {"shared": {}, "instances": {}}),
                            ("_save_raw_config", None), ("save_gui_config", None),
                            ("get_theme_mode", "default"), ("focus_project_window", False)):
            stack.enter_context(patch.object(config, name, return_value=value))
        stack.enter_context(patch.object(file_utils, "hide_internal_project_metadata"))
        stack.enter_context(patch.object(config, "clean_instance_config"))
        stack.enter_context(patch("src.modules.ubuntu_download_manager.quit_if_last_workspace"))
        stack.enter_context(patch.object(QMessageBox, "warning"))
        backend = PreviewBackend()
        backend.sketch_dir_path = fixture_root / scenario
        backend.active_file_path = str(backend.sketch_dir_path / "preview.ino")
        backend.start_services = Mock()
        backend.stop_services = Mock()
        backend.save_settings = Mock()
        backend._scan_ports = Mock(side_effect=AssertionError("Unexpected hardware discovery"))
        window = MCUMainWindow(backend)
        survivor = MCUMainWindow(PreviewBackend()) if scenario == "survivor" else None
        # The real editor assets come from ROOT. Only the close-side download
        # manager trigger is redirected to this fixture's private directory.
        stack.enter_context(patch("main.qt.main_window._project_root", fixture_root))
        window.show()
        if survivor:
            survivor.show()
        page = window._editor_panel._view.page()
        resources = [window, *window.findChildren(QWebEngineView)]
        resources.extend(view.page() for view in resources[1:])
        destroyed = []
        for index, resource in enumerate(resources):
            resource.destroyed.connect(lambda _obj=None, index=index: destroyed.append(index))
        errors = []
        loop = QEventLoop()
        done = [False]
        phase = ["renderer load"]
        deadline = time.monotonic() + 20
        if last_window:
            app.setQuitOnLastWindowClosed(True)

        def finish(error=None):
            if done[0]:
                return
            done[0] = True
            if error:
                errors.append(error)
            loop.quit()

        def check_destroyed():
            if done[0]:
                return
            if all(not isValid(resource) for resource in resources):
                assert len(set(destroyed)) == len(resources), "Owned destruction signals were missing"
                if survivor and phase[0] != "survivor destruction":
                    phase[0] = "survivor callback"
                    assert isValid(survivor) and survivor.isVisible()
                    survivor._editor_panel._view.page().runJavaScript("1 + 1", survivor_ready)
                    return
                if scenario == "startup":
                    # Stay in the live loop beyond the pending 100ms startup
                    # callback; a closed workspace must not start services.
                    QTimer.singleShot(160, finish_startup)
                    return
                finish()
            elif time.monotonic() >= deadline:
                alive = [type(resource).__name__ for resource in resources if isValid(resource)]
                finish(AssertionError(f"{scenario}: accepted close retained owned Qt resources: {alive}"))
            else:
                QTimer.singleShot(10, check_destroyed)

        def finish_startup():
            try:
                assert not backend.start_services.called, "Closed workspace started background services"
                finish()
            except Exception as exc:
                finish(exc)

        def survivor_ready(value):
            nonlocal deadline
            try:
                assert value == 2, "Closing one workspace damaged another workspace's renderer"
                for resource in (survivor, survivor._editor_panel._view, survivor._editor_panel._view.page()):
                    index = len(resources)
                    resources.append(resource)
                    resource.destroyed.connect(lambda _obj=None, index=index: destroyed.append(index))
                phase[0] = "survivor destruction"
                deadline = time.monotonic() + 5
                assert survivor.close()
                QTimer.singleShot(0, check_destroyed)
            except Exception as exc:
                finish(exc)

        def renderer_ready(ready):
            nonlocal deadline
            if done[0]:
                return
            if not ready:
                QTimer.singleShot(50, probe)
                return
            try:
                if scenario in ("detached", "reattached"):
                    window.detach_editor()
                    floating = window._detached_window
                    resources.append(floating)
                    index = len(resources) - 1
                    floating.destroyed.connect(lambda _obj=None: destroyed.append(index))
                    assert window._editor_panel._view.page() is page
                    if scenario == "reattached":
                        assert floating.close()
                        assert window._editor_panel._view.page() is page and isValid(page)
                if scenario == "vetoed":
                    backend._cloud_project_operation = True
                    assert not window.close(), "Cloud refresh close veto was bypassed"
                    assert window.isVisible() and isValid(page)
                    backend._cloud_project_operation = False
                    backend.is_busy = True
                    backend.active_operation = "flash"
                    backend._current_op_phase = "flashing"
                    assert not window.close(), "Active hardware-write close veto was bypassed"
                    assert window.isVisible() and isValid(page)
                    backend.is_busy = False
                    backend.active_operation = backend._current_op_phase = None
                phase[0] = "owned Qt destruction"
                deadline = time.monotonic() + 5
                assert window.close(), "Idle workspace refused an accepted close"
                # Destruction must occur after this real JavaScript callback
                # unwinds, while the application event loop is still alive.
                assert all(isValid(resource) for resource in resources)
                QTimer.singleShot(0, check_destroyed)
            except Exception as exc:
                finish(exc)

        def probe():
            if not done[0]:
                page.runJavaScript("Boolean(window.editorInstance?.getModel())", renderer_ready)

        def timeout():
            if not done[0]:
                finish(AssertionError(f"{scenario}: timed out during {phase[0]}"))

        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(timeout)
        watchdog.start(25000)
        if scenario == "startup":
            QTimer.singleShot(0, lambda: renderer_ready(True))
        else:
            QTimer.singleShot(0, probe)
        loop.exec()
        watchdog.stop()
        if last_window:
            if errors:
                raise errors[0]
            return resources, destroyed
        # A failing assertion must still leave a normally finalized process.
        # This cleanup is fixture-only and never supplies the assertion above.
        if isValid(window):
            if errors:
                print(str(errors[0]), file=sys.stderr, flush=True)
            window.deleteLater()
            QTimer.singleShot(0, loop.quit)
            loop.exec()
        if errors:
            raise errors[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=1)
    args = parser.parse_args()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from main.qt.garbage_collection import install_gui_garbage_collector
    app = QApplication(["MCU shutdown verification"])
    app.setQuitOnLastWindowClosed(False)
    collector = install_gui_garbage_collector(app)
    parent = ROOT / "temp/audit/shutdown"
    parent.mkdir(parents=True, exist_ok=True)
    errors = []
    last_resources = []
    last_destroyed = []
    final_watchdog = QTimer()
    final_watchdog.setSingleShot(True)
    def final_timeout():
        errors.append(AssertionError("Closing the last workspace did not quit QApplication"))
        app.quit()
    final_watchdog.timeout.connect(final_timeout)
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=parent) as directory:
        def run_checks():
            nonlocal last_resources, last_destroyed
            try:
                for repetition in range(args.repeat):
                    for scenario in ("attached", "detached", "reattached", "vetoed", "startup", "survivor"):
                        verify_case(app, scenario, Path(directory))
                        print(f"{repetition + 1}/{args.repeat} {scenario}: owned renderer destroyed before normal shutdown", flush=True)
                last_resources, last_destroyed = verify_case(app, "last-window", Path(directory), last_window=True)
                final_watchdog.start(5000)
            except Exception as exc:
                errors.append(exc)
                app.quit()

        QTimer.singleShot(0, run_checks)
        try:
            app.exec()
        finally:
            collector.restore()
        if errors:
            raise errors[0]
        from shiboken6 import isValid
        assert last_resources and all(not isValid(resource) for resource in last_resources), "Last-window exit retained an owned renderer"
        assert len(set(last_destroyed)) == len(last_resources), "Last-window destruction signals were missing"
        print("Last workspace close: QApplication returned after owned renderer destruction", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
