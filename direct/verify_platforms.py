#!/usr/bin/env python3
"""Separate Windows/Ubuntu host contracts, using only isolated fixtures."""
from __future__ import annotations
import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from main.platforms import get_platform_backend
from main.platforms import ubuntu
from main.core import toolchain


class PlatformChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/platforms"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        assert self.root.resolve().is_relative_to((ROOT / "temp").resolve())

    def test_host_selector_loads_only_requested_implementation(self):
        with patch("main.platforms.importlib.import_module", side_effect=lambda name: name) as importer:
            self.assertEqual(get_platform_backend("linux"), "main.platforms.ubuntu")
            importer.assert_called_once_with("main.platforms.ubuntu")
            importer.reset_mock()
            self.assertEqual(get_platform_backend("win32"), "main.platforms.windows")
            importer.assert_called_once_with("main.platforms.windows")
            with self.assertRaises(RuntimeError):
                get_platform_backend("darwin")

    def test_fresh_ubuntu_first_import_has_no_windows_or_board_discovery(self):
        code = ("import sys; sys.path.insert(0, " + repr(str(ROOT)) + "); sys.platform = 'linux'; "
                "from main.platforms import ubuntu; from main.core import toolchain; "
                "assert toolchain._implementation is ubuntu; "
                "assert 'main.platforms.windows' not in sys.modules; "
                "assert 'bootstrap' not in sys.modules; "
                "assert 'main.core.board_catalog' not in sys.modules")
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.root, capture_output=True,
                                timeout=15, **get_platform_backend().process_options())
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))

    def test_facade_selects_real_host_and_exports_shared_budgets(self):
        suffix = "windows" if sys.platform == "win32" else "ubuntu"
        self.assertEqual(toolchain._implementation.__name__, "main.platforms." + suffix)
        self.assertIs(toolchain.find_pio_executable, toolchain._implementation.find_pio_executable)
        self.assertEqual(toolchain.get_optimal_compiler_jobs.__module__, "main.core.build_resources")
        if sys.platform.startswith("linux"):
            self.assertNotIn("main.platforms.windows", sys.modules)
            self.assertNotIn("bootstrap", sys.modules)

    def test_all_source_files_compile_without_running_installers(self):
        for folder in ("main/platforms", "direct/ubuntu"):
            for path in (ROOT / folder).glob("*.py"):
                compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")

    def test_ubuntu_replaces_foreign_toolchain_paths(self):
        core = self.root / "native-core"
        foreign = {name: "C:/copied-windows/toolchains" for name in (
            "PLATFORMIO_CORE_DIR", "PLATFORMIO_PACKAGES_DIR", "PLATFORMIO_PLATFORMS_DIR",
            "PLATFORMIO_CACHE_DIR", "PLATFORMIO_BUILD_CACHE_DIR", "PLATFORMIO_GLOBALLIB_DIR",
            "PLATFORMIO_PENV_DIR", "PLATFORMIO_PYTHON_EXE", "PYTHONHOME")}
        with patch.object(ubuntu, "native_platformio_dir", return_value=core), patch.dict(os.environ, foreign):
            actual, repaired = ubuntu._refresh_platformio_core_environment(self.root)
            self.assertEqual(actual, core)
            self.assertFalse(repaired)
            for name in ("PLATFORMIO_CORE_DIR", "PLATFORMIO_PACKAGES_DIR", "PLATFORMIO_PLATFORMS_DIR",
                         "PLATFORMIO_CACHE_DIR", "PLATFORMIO_BUILD_CACHE_DIR", "PLATFORMIO_GLOBALLIB_DIR", "TMPDIR"):
                path = Path(os.environ[name])
                self.assertTrue(path.is_relative_to(core))
                self.assertTrue(path.is_dir())
            for name in ("PLATFORMIO_PENV_DIR", "PLATFORMIO_PYTHON_EXE", "PYTHONHOME"):
                self.assertNotIn(name, os.environ)
        self.assertFalse((self.root / "src/.platformio-mcu-gui").exists())

    def test_ubuntu_core_selection_does_not_write_on_import_or_discovery(self):
        core = self.root / "absent"
        with patch.object(ubuntu, "native_platformio_dir", return_value=core):
            self.assertEqual(ubuntu._get_safe_platformio_core_dir(self.root), str(core))
        self.assertFalse(core.exists())

    def test_ubuntu_uses_native_python_module_without_exe_or_bootstrap_fallback(self):
        runtime = SimpleNamespace(platform="linux", executable="/fixture/.venv-linux/bin/python")
        with patch.object(ubuntu, "sys", runtime), patch.object(ubuntu.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(ubuntu.find_pio_executable()[0], runtime.executable)
            self.assertTrue(ubuntu.find_pio_executable()[-1].endswith("offline_platformio.py"))
        runtime.executable = "C:/copied/src/_python/python.exe"
        with patch.object(ubuntu, "sys", runtime):
            self.assertIsNone(ubuntu.find_pio_executable())
        runtime.executable = "/fixture/.venv-linux/bin/python"
        with patch.object(ubuntu, "sys", runtime), patch.object(ubuntu.importlib.util, "find_spec", return_value=None):
            self.assertIsNone(ubuntu.ensure_platformio())
            self.assertFalse(ubuntu.ensure_scons_ready())

    def test_ubuntu_runtime_rejects_toolchain_installation(self):
        status = Mock()
        with patch.object(ubuntu, "find_pio_executable", return_value=["native-python", "-m", "platformio"]), \
                patch.object(subprocess, "run", side_effect=AssertionError("Preparation must not launch an installer")):
            with self.assertRaisesRegex(RuntimeError, "bootstrap"):
                ubuntu.prepare_platformio_board_toolchain("ststm32", "nucleo_f401re", "cmsis", on_status=status)
        self.assertFalse(ubuntu.board_toolchain_ready(self.root, "ststm32", "nucleo_f401re", "cmsis"))

    def test_ubuntu_processes_use_posix_sessions_for_every_board_family(self):
        self.assertEqual(ubuntu.process_options(priority=True, session=True), {"start_new_session": True})
        self.assertEqual(ubuntu.process_options(), {})
        for platform in ("espressif32", "espressif8266", "atmelavr", "ststm32", "raspberrypi", "nordicnrf52", "future"):
            self.assertTrue(ubuntu.use_native_upload(dict(platform=platform, framework="arduino", pio_manifest="fixture")))

    def test_ubuntu_setup_rejects_windows_and_root_without_installing(self):
        spec = importlib.util.spec_from_file_location("fixture_ubuntu_setup", ROOT / "direct/ubuntu/setup.py")
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        setup.ROOT, setup.ENV_DIR = self.root, self.root / ".venv-linux"
        with patch.object(setup, "sys", SimpleNamespace(platform="win32", version_info=(3, 11), stderr=sys.stderr)), \
                patch.object(setup.venv, "EnvBuilder", side_effect=AssertionError("No install")):
            self.assertEqual(setup.main([]), 1)
        with patch.object(setup, "sys", SimpleNamespace(platform="linux", version_info=(3, 11), stderr=sys.stderr)), \
                patch.object(setup.os, "geteuid", return_value=0, create=True), \
                patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True), \
                patch.object(setup.venv, "EnvBuilder", side_effect=AssertionError("No root install")):
            self.assertEqual(setup.main([]), 1)

    def test_ubuntu_setup_repairs_only_native_venv_and_preserves_launch_arguments(self):
        spec = importlib.util.spec_from_file_location("fixture_ubuntu_setup", ROOT / "direct/ubuntu/setup.py")
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        setup.ROOT, setup.ENV_DIR = self.root, self.root / ".venv-linux"
        with patch.object(setup, "sys", SimpleNamespace(platform="linux", version_info=(3, 11), stderr=sys.stderr)), \
                patch.object(setup.os, "geteuid", return_value=1000, create=True), \
                patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True), \
                patch.object(setup.venv, "EnvBuilder") as builder, \
                patch.object(setup.subprocess, "run") as run, patch.object(setup.subprocess, "call", return_value=0) as launch:
            self.assertEqual(setup.main(["--launch", "--project", "/fixture/Sketch With Spaces", "--new-window"]), 0)
        builder.assert_called_once_with(with_pip=True, symlinks=True)
        builder.return_value.create.assert_called_once_with(setup.ENV_DIR)
        self.assertEqual(run.call_count, 3)
        self.assertTrue(run.call_args_list[-1].args[0][2].endswith("offline_bootstrap.py"))
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[0], str(setup.ENV_DIR / "bin/python"))
        self.assertEqual(command[-1], str(self.root / "direct/ubuntu/requirements.txt"))
        self.assertEqual(launch.call_args.args[0][-3:], ["--project", "/fixture/Sketch With Spaces", "--new-window"])

    def test_ubuntu_bootstrap_from_workspace_relaunches_before_installing(self):
        spec = importlib.util.spec_from_file_location("fixture_ubuntu_handoff", ROOT / "direct/ubuntu/setup.py")
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        setup.ROOT, setup.ENV_DIR = self.root, self.root / ".venv-linux"
        with patch.object(setup, "sys", SimpleNamespace(platform="linux", version_info=(3, 11), stderr=sys.stderr,
                                                       executable="/fixture/.venv-linux/bin/python")), \
                patch.object(setup.os, "geteuid", return_value=1000, create=True), \
                patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True), \
                patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1", "PIP_NO_INDEX": "1"}), \
                patch.object(setup.venv, "EnvBuilder", side_effect=AssertionError("Inherited workspace installed packages")), \
                patch.object(setup.subprocess, "call", return_value=0) as launch:
            self.assertEqual(setup.main(["--plan", "/fixture/offline.json"]), 0)
        self.assertEqual(launch.call_args.args[0][-2:], ["--plan", "/fixture/offline.json"])
        self.assertNotIn("MCU_FLASHER_OFFLINE_RUNTIME", launch.call_args.kwargs["env"])
        self.assertNotIn("PIP_NO_INDEX", launch.call_args.kwargs["env"])

    def test_ubuntu_upload_pipeline_uses_native_session_and_serial_paths(self):
        from main import web_bridge
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api.current_port = api._active_port_label = "/dev/ttyACM0"
        api._active_board_info = dict(platform="atmelavr", board="uno", framework="arduino", require_upload_port=True)
        api._resolve_board_info = lambda name=None: api._active_board_info
        api.sketch_dir_path = self.root
        api._effective_cache_root = lambda path: self.root
        api._generate_platformio_ini = api._stop_serial_monitor = api._start_serial_monitor = Mock()
        api._unmap_unc_after_build = Mock()
        api._get_jobs = lambda: 2
        api._stop_requested = False
        api.emit = Mock()
        process = SimpleNamespace(stdout=[], wait=lambda: 0, poll=lambda: 0)
        with patch.object(web_bridge, "_HOST_RUNTIME", ubuntu), \
                patch.object(web_bridge, "port_occupied_owner", return_value=None), \
                patch.object(web_bridge, "find_pio_executable", return_value=["/fixture/.venv-linux/bin/python", "-m", "platformio"]), \
                patch.object(web_bridge, "_refresh_platformio_core_environment", return_value=(self.root, False)), \
                patch.object(web_bridge.subprocess, "Popen", return_value=process) as launch:
            api._native_upload_worker(can_skip=True)
        args, kwargs = launch.call_args.args[0], launch.call_args.kwargs
        self.assertEqual(args[-2:], ["--upload-port", "/dev/ttyACM0"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("creationflags", kwargs)
        self.assertNotIn("startupinfo", kwargs)
        self.assertEqual(launch.call_count, 1)

    def test_launchers_and_ubuntu_dependencies_are_separate(self):
        requirements = (ROOT / "direct/ubuntu/requirements.txt").read_text(encoding="utf-8")
        actual = [line.lower() for line in requirements.splitlines() if line and not line.startswith("#")]
        self.assertFalse(any(word in line for line in actual for word in ("pywinpty", "pywin32", "webview")))
        windows = (ROOT / "direct/windows/run.vbs").read_text(encoding="utf-8-sig")
        self.assertIn("Win32_Processor", windows)
        self.assertIn('physicalCores < 4', windows)
        self.assertIn('src\\modules', windows)
        self.assertIn('windows\\run.vbs', (ROOT / "direct/runThisOnWindows.vbs").read_text(encoding="utf-8-sig"))
        for path in (ROOT / "direct/ubuntu/run.sh", ROOT / "direct/runThisOnUbuntu.sh"):
            self.assertNotIn(b"\r", path.read_bytes())

    def test_ubuntu_bash_syntax_without_launching(self):
        bash = shutil.which("bash")
        if not bash and sys.platform == "win32":
            candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
            bash = str(candidate) if candidate.is_file() else None
        if not bash:
            self.skipTest("Bash unavailable; Ubuntu CI verifies syntax natively")
        for entry in ("direct/ubuntu/run.sh", "direct/runThisOnUbuntu.sh"):
            result = subprocess.run([bash, "-n", str(ROOT / entry)], capture_output=True, timeout=10,
                                    **get_platform_backend().process_options())
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))

    def test_windows_script_parser_without_launching(self):
        if sys.platform != "win32":
            self.skipTest("Native Windows Script Host syntax check")
        parser = Path(os.environ["WINDIR"]) / "System32/cscript.exe"
        for entry in ("direct/windows/run.vbs", "direct/runThisOnWindows.vbs"):
            source = (ROOT / entry).read_text(encoding="ascii")
            main, declarations, functions, inside = [], [], [], False
            for line in source.splitlines():
                token = line.strip().lower()
                if token.startswith("function ") or token.startswith("sub "):
                    inside = True
                if inside:
                    functions.append(line)
                    if token in ("end function", "end sub"):
                        inside = False
                elif token.startswith("dim ") or token == "option explicit":
                    declarations.append(line)
                else:
                    main.append(line)
            self.assertFalse(inside)
            # Only declarations run. Every launcher statement stays in an
            # uncalled procedure; setup, repairs and UI are never invoked.
            fixture = self.root / "syntax.vbs"
            fixture.write_text("\n".join(declarations + ["Sub ParserOnly()"] + main + ["End Sub"] + functions), encoding="ascii")
            result = subprocess.run([str(parser), "//nologo", str(fixture)], capture_output=True, timeout=10,
                                    **get_platform_backend().process_options())
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
