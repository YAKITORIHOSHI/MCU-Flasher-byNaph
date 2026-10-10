#!/usr/bin/env python3
"""Opt-in Ubuntu first-launch integration in a disposable temp/ application copy.

--execute downloads the real default native package plan into an empty store,
creates an empty private Python environment through the native launcher, opens
the actual workspace under the supplied display, and clicks Compile afterwards.
It does not authenticate with apt, open a serial device, or change live settings.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import pty
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


HOME_ISOLATION = '''"""Fixture-only home path redirection; no environment home variables change."""
import os
from pathlib import Path
_fixture_home = Path(__file__).resolve().parents[2] / "sandbox-home"
_fixture_home.mkdir(parents=True, exist_ok=True)
Path.home = classmethod(lambda cls: _fixture_home)
_original_expanduser = os.path.expanduser
def _fixture_expanduser(value):
    if isinstance(value, str) and (value == "~" or value.startswith("~/")):
        return str(_fixture_home / value[2:]) if value != "~" else str(_fixture_home)
    if isinstance(value, bytes) and (value == b"~" or value.startswith(b"~/")):
        return os.fsencode(_fixture_home / os.fsdecode(value[2:])) if value != b"~" else os.fsencode(_fixture_home)
    return _original_expanduser(value)
os.path.expanduser = _fixture_expanduser
if os.environ.get("MCU_FIRST_RUN_ALLOW_SMALL_RUNNER") == "1":
    # Fixture-only CI admission; production CPU policy and resource budgets stay intact.
    from src.modules import runtime_resources as _fixture_resources
    _fixture_resources.enforce_minimum_cpu_requirement = lambda: True
'''

GUI_SERIAL_ISOLATION = '''
# Fixture-only hardware boundary: discovery is empty and serial opens fail.
import serial as _first_run_serial
from serial.tools import list_ports as _first_run_ports
_first_run_ports.comports = lambda *args, **kwargs: []
def _first_run_no_serial(*args, **kwargs):
    raise AssertionError("First-launch verification must never open a serial device")
_first_run_serial.Serial = _first_run_no_serial
'''

GUI_SMOKE = '''
        # Fixture-only observation and bounded clean shutdown of the real GUI.
        import json as _first_run_json
        import time as _first_run_time
        _first_run_deadline = _first_run_time.monotonic() + 60
        _first_run_finished = [False]
        _first_run_editor = [{}]
        _first_run_source = Path(api.sketch_dir_path) / "probe.ino"
        _first_run_result = SCRIPT_DIR.parent / "gui-result.json"
        def _first_run_finish(success, detail):
            if _first_run_finished[0]:
                return
            _first_run_finished[0] = True
            from src.modules.offline_mode import offline_enabled as _first_run_saved_mode
            from src.modules.offline_runtime import network_access_disabled as _first_run_active_mode
            _first_run_saved_offline = bool(_first_run_saved_mode())
            _first_run_network_disabled = bool(_first_run_active_mode())
            if _first_run_saved_offline != _first_run_network_disabled:
                success = False
                detail = "Saved Online/Offline Mode does not match the active network policy"
            report = {"success": bool(success), "detail": detail,
                      "workspace_visible": window.isVisible(),
                      "services_started": bool(api._services_started),
                      "serial_running": bool(api.serial_running),
                      "saved_offline_enabled": _first_run_saved_offline,
                      "network_access_disabled": _first_run_network_disabled,
                      "source_loaded": bool(_first_run_editor[0].get("source_loaded")),
                      "active_file": str(api.active_file_path or ""),
                      "editor_active_file": _first_run_editor[0].get("active_path", ""),
                      "source_tab_count": _first_run_editor[0].get("tab_count", 0),
                      "project": str(api.sketch_dir_path),
                      "private_prefix": sys.prefix, "upload": False}
            if success:
                report["screenshot"] = str(SCRIPT_DIR.parent / "workspace.png")
                report["capture_saved"] = window.grab().save(report["screenshot"])
            _first_run_result.write_text(_first_run_json.dumps(report, indent=2) + "\\n", encoding="utf-8")
            print("Ubuntu first-launch real workspace: " + ("OK" if success else "FAILED") + ": " + detail, flush=True)
            window.close()
            if not success:
                app.exit(1)
        def _first_run_observe(value):
            if _first_run_finished[0]:
                return
            try:
                observed = _first_run_json.loads(value) if isinstance(value, str) else {}
                content = observed.get("content", "")
                observed["source_loaded"] = bool(
                    observed.get("model_loaded") and "void setup()" in content and "void loop()" in content
                    and Path(str(api.active_file_path or "")).resolve() == _first_run_source.resolve()
                    and Path(str(observed.get("active_path", ""))).resolve() == _first_run_source.resolve()
                    and observed.get("tab_count", 0) > 0)
                _first_run_editor[0] = observed
            except (ValueError, TypeError, AttributeError):
                _first_run_editor[0] = {}
            if _first_run_editor[0].get("source_loaded") and api._services_started and window.isVisible():
                _first_run_finish(True, "Fixture sketch loaded in the active Monaco source tab and background services started")
            elif _first_run_time.monotonic() >= _first_run_deadline:
                _first_run_finish(False, "Workspace or Monaco did not become ready within 60 seconds")
            else:
                QTimer.singleShot(250, _first_run_poll)
        def _first_run_poll():
            if not _first_run_finished[0]:
                window._editor_panel._view.page().runJavaScript(
                    "JSON.stringify({model_loaded: Boolean(window.editorInstance?.getModel()), "
                    "content: window.editorInstance?.getValue() || '', "
                    "active_path: document.querySelector('#tab-bar .tab.active')?._filePath || '', "
                    "tab_count: document.querySelectorAll('#tab-bar .tab').length})", _first_run_observe)
        QTimer.singleShot(250, _first_run_poll)
        QTimer.singleShot(61000, lambda: _first_run_finish(False, "Workspace observation timed out"))
'''


def _copy_source(destination: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    for relative in ("main", "direct", "src/modules", "src/editor", "src/assets", "src/fonts"):
        shutil.copytree(ROOT / relative, destination / relative, ignore=ignore)
    for source in (ROOT / "src").glob("*.py"):
        shutil.copy2(source, destination / "src" / source.name)
    database = destination / "src/dbs"
    database.mkdir(parents=True)
    for source in (ROOT / "src/dbs").glob("*.py"):
        shutil.copy2(source, database / source.name)
    # AGENTS.md is generated local guidance, ignored by Git. A clean checkout
    # must not require it or inherit another sketch's live hardware state.
    for name in ("MCU_Flasher", "mcu_flash_gui.py", "README.md"):
        shutil.copy2(ROOT / name, destination / name)
    (destination / "src/__init__.py").write_text(HOME_ISOLATION, encoding="utf-8")
    # Direct verifiers can import main before src. Redirect home first there too.
    for name in ("verify_runtime.py", "verify_target_resolution.py"):
        script = destination / "direct" / name
        content = script.read_text(encoding="utf-8")
        anchor = 'sys.path.insert(0, str(ROOT))\n'
        if content.count(anchor) != 1:
            raise RuntimeError(f"Cannot safely instrument fixture verifier {name}")
        script.write_text(content.replace(anchor, anchor + "import src  # Fixture home isolation before application imports\n", 1), encoding="utf-8")
    gui = destination / "main/mcu_flash_gui.py"
    content = gui.read_text(encoding="utf-8")
    serial_anchor = "from main.web_bridge import MCUWebBackendAPI\n"
    smoke_anchor = "        window.show()\n"
    if content.count(serial_anchor) != 1 or content.count(smoke_anchor) != 1:
        raise RuntimeError("Cannot safely instrument the copied GUI entry point")
    content = content.replace(serial_anchor, serial_anchor + GUI_SERIAL_ISOLATION, 1)
    content = content.replace(smoke_anchor, smoke_anchor + GUI_SMOKE, 1)
    gui.write_text(content, encoding="utf-8")


def _environment(fixture: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in tuple(env):
        if name.startswith("PLATFORMIO_") or name in (
            "MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_WORKSPACE_RUNTIME", "MCU_FLASHER_APP_ROOT",
            "MCU_FIRST_RUN_ALLOW_SMALL_RUNNER",
            "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PIP_NO_INDEX",
            "PIP_TARGET", "PIP_PREFIX", "PIP_USER", "PIP_ROOT",
            "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH",
            "LOCALAPPDATA", "APPDATA", "USERPROFILE",
        ):
            env.pop(name, None)
    paths = {
        "XDG_DATA_HOME": fixture / "data", "XDG_CACHE_HOME": fixture / "cache",
        "XDG_CONFIG_HOME": fixture / "config", "XDG_RUNTIME_DIR": fixture / "runtime",
        "MCU_FLASHER_STATE_DIR": fixture / "state", "MCU_PACKAGE_EVENTS_ROOT": fixture / "package-events",
        "PIP_CACHE_DIR": fixture / "pip-cache", "TMPDIR": fixture / "tmp",
        "TMP": fixture / "tmp", "TEMP": fixture / "tmp",
    }
    for name, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    (fixture / "runtime").chmod(0o700)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Pip subprocesses do not import the fixture's src package. Prevent a user's
    # pip configuration from redirecting private installs outside this fixture.
    env["PIP_CONFIG_FILE"] = os.devnull
    env["QT_QPA_PLATFORM"] = "xcb"
    env.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    return env


def _stop_process(process) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=10)
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # A descendant can ignore SIGTERM after the coordinator has already exited.
    # This is the private process group created by _run, not a discovered PID.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=10)


def _run(command, *, cwd, env, log: Path, timeout, tty=False) -> int:
    master = slave = None
    process = None
    try:
        if tty:
            master, slave = pty.openpty()
        with log.open("wb") as output:
            process = subprocess.Popen(command, cwd=cwd, env=env,
                                       stdin=slave if tty else subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            if slave is not None:
                os.close(slave)
                slave = None
            return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        raise RuntimeError(f"First-launch verification timed out; see {log}")
    finally:
        _stop_process(process)  # Also release owned children on cancellation/errors.
        for descriptor in (master, slave):
            if descriptor is not None:
                os.close(descriptor)


def _gui_result(fixture: Path, sketch: Path, runtime: Path) -> dict:
    result = json.loads((fixture / "gui-result.json").read_text(encoding="utf-8"))
    expected = {"success": True, "workspace_visible": True, "services_started": True,
                "serial_running": False, "saved_offline_enabled": False,
                "network_access_disabled": False, "upload": False, "capture_saved": True,
                "source_loaded": True}
    if (any(result.get(name) is not value for name, value in expected.items())
            or Path(result.get("project", "")).resolve() != sketch.resolve()
            or Path(result.get("private_prefix", "")).resolve() != runtime.resolve()
            or Path(result.get("active_file", "")).resolve() != (sketch / "probe.ino").resolve()
            or Path(result.get("editor_active_file", "")).resolve() != (sketch / "probe.ino").resolve()
            or result.get("source_tab_count", 0) < 1):
        raise RuntimeError(f"The actual workspace smoke failed or used the wrong fixture: {result}")
    return result


def execute(fixture: Path, timeout: int, *, allow_small_runner=False) -> dict:
    fixture.mkdir(parents=True, exist_ok=False)
    app = fixture / "app"
    app.mkdir()
    _copy_source(app)
    env = _environment(fixture)
    if allow_small_runner:
        env["MCU_FIRST_RUN_ALLOW_SMALL_RUNNER"] = "1"
    sketch = fixture / "sketch"
    sketch.mkdir()
    (sketch / "probe.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
    core = fixture / "data/mcu-flasher/platformio" / platform.machine()
    runtime = app / ".venv-linux"
    if core.exists() or runtime.exists():
        raise RuntimeError("First run must start without native packages or a private runtime")
    report = {"success": False, "fixture": str(fixture), "initial_private_runtime_present": False,
              "initial_native_store_present": False, "default_plan": json.loads((app / "direct/offline-packages.json").read_text()),
              "upload": False, "live_settings_modified": False, "system_packages_installed": False,
              "fixture_instrumentation": {
                  "src/__init__.py": "Redirect Path.home and tilde expansion inside fixture application processes",
                  "main/mcu_flash_gui.py": "Empty serial discovery, prohibit serial opens, observe Monaco/services and close within 61 seconds",
                  "direct/verify_runtime.py": "Import fixture home redirection before application imports",
                  "direct/verify_target_resolution.py": "Import fixture home redirection before application imports",
              },
              "installers_mocked": False, "home_environment_changed": False,
              "fixture_cpu_admission_override": bool(allow_small_runner)}
    result_path = fixture / "result.json"
    result_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"First launch from empty runtime/store: {fixture}", flush=True)
    cold_log = fixture / "first-launch.log"
    cold = _run([str(app / "MCU_Flasher"), "--project", str(sketch)],
                cwd=fixture, env=env, log=cold_log, timeout=timeout, tty=True)
    report.update(first_launch_exit_code=cold, first_launch_log=str(cold_log))
    result_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if cold:
        raise RuntimeError(f"Automatic first-launch Bootstrap failed with exit {cold}; see {cold_log}")
    gui_result = _gui_result(fixture, sketch, runtime)
    shutil.copy2(fixture / "gui-result.json", fixture / "cold-gui-result.json")
    shutil.copy2(fixture / "workspace.png", fixture / "cold-workspace.png")
    gui_result["screenshot"] = str(fixture / "cold-workspace.png")
    report["actual_gui"] = gui_result
    result_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    certificate = json.loads((core / ".mcu-offline-ready.json").read_text(encoding="utf-8"))
    if certificate.get("prepared_plan") != report["default_plan"]:
        raise RuntimeError("Bootstrap did not certify the complete default package plan")
    python = runtime / "bin/python"
    check_log = fixture / "readiness-check.log"
    checked = _run([str(app / "MCU_Flasher"), "--check"], cwd=fixture, env=env,
                   log=check_log, timeout=180)
    if checked:
        raise RuntimeError(f"Native launcher readiness failed after Bootstrap; see {check_log}")
    print("Full Bootstrap certificate and native launcher readiness: OK", flush=True)
    warm_log = fixture / "warm-launch.log"
    (fixture / "gui-result.json").unlink()  # A clean exit alone must not reuse the cold GUI observation.
    warm = _run([str(app / "MCU_Flasher"), "--project", str(sketch)], cwd=fixture, env=env,
                log=warm_log, timeout=120, tty=True)
    warm_text = warm_log.read_text(encoding="utf-8", errors="replace")
    if warm or "Preparing Ubuntu system prerequisites" in warm_text:
        raise RuntimeError(f"Prepared launch unexpectedly failed or repeated Bootstrap; see {warm_log}")
    warm_gui_result = _gui_result(fixture, sketch, runtime)
    print("Prepared launcher opened the actual workspace without Bootstrap: OK", flush=True)
    compile_fixture = app / "temp/audit/first-compile"
    compile_fixture.mkdir(parents=True)
    (compile_fixture / "core").symlink_to(core, target_is_directory=True)
    compiler = core / "packages/toolchain-xtensa-esp32/bin/xtensa-esp32-elf-g++"
    with compiler.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            raise RuntimeError("Bootstrap prepared a non-native ESP32 compiler")
    compile_log = fixture / "compile-verifier.log"
    compiled = _run([str(python), "-B", str(app / "direct/verify_target_resolution.py"),
                     "--verify-built-pipeline", str(compile_fixture)],
                    cwd=app, env=env, log=compile_log, timeout=360)
    if compiled:
        raise RuntimeError(f"The real Compile-button integration failed; see {compile_log}")
    build_result = json.loads((compile_fixture / "application-build-result.json").read_text())
    firmware = Path(build_result["firmware"])
    if not build_result.get("success") or not firmware.is_file():
        raise RuntimeError("The actual Compile button did not produce firmware")
    runtime_log = fixture / "runtime-verifier.log"
    runtime_checked = _run([str(python), "-B", str(app / "direct/verify_runtime.py"),
                            "--render-dir", str(fixture / "runtime-captures")],
                           cwd=app, env=env, log=runtime_log, timeout=120)
    if runtime_checked:
        raise RuntimeError(f"The newly bootstrapped private runtime failed hardware-free checks; see {runtime_log}")
    report.update(success=True, actual_gui=gui_result, core=str(core),
                  certified_platforms=certificate.get("platform_sources"),
                  certificate=str(core / ".mcu-offline-ready.json"), readiness_exit_code=checked,
                  readiness_log=str(check_log), warm_launch_exit_code=warm, warm_launch_log=str(warm_log),
                  warm_gui=warm_gui_result,
                  compile_exit_code=compiled, compile_log=str(compile_log),
                  compile=build_result, firmware_bytes=firmware.stat().st_size,
                  runtime_exit_code=runtime_checked, runtime_log=str(runtime_log))
    result_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Native first-run Bootstrap -> actual workspace -> real ESP32 Compile: OK ({firmware.stat().st_size} firmware bytes)", flush=True)
    print(f"Result: {result_path}", flush=True)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Download and prepare the real full default package plan in an empty temp/ fixture")
    parser.add_argument("--fixture", type=Path, help="New fixture directory inside this checkout's temp/ (must not exist)")
    parser.add_argument("--timeout", type=int, default=3600, help="Maximum first-launch Bootstrap seconds (default: 3600)")
    parser.add_argument("--allow-small-runner", action="store_true",
                        help="Override CPU admission only inside the disposable fixture for CI runners with fewer than four cores")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.print_help()
        return 0
    if not sys.platform.startswith("linux"):
        parser.error("First-launch integration requires native Ubuntu/Linux")
    if not os.environ.get("DISPLAY"):
        parser.error("Supply a desktop/Xvfb DISPLAY for the actual native workspace")
    if args.timeout < 60:
        parser.error("--timeout must be at least 60 seconds")
    sys.path.insert(0, str(ROOT))
    from direct.ubuntu.preflight import host_preflight
    host_preflight(require_display=True)  # Fail before cloning; never invoke apt during this probe.
    parent = ROOT / "temp/audit/ubuntu-first-run"
    parent.mkdir(parents=True, exist_ok=True)
    fixture = args.fixture.resolve() if args.fixture else parent / (time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}")
    if not fixture.is_relative_to((ROOT / "temp").resolve()) or fixture.exists():
        parser.error("--fixture must be a new directory inside this checkout's temp/")
    try:
        execute(fixture, args.timeout, allow_small_runner=args.allow_small_runner)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        print(f"Fixture retained for inspection: {fixture}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
