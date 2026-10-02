#!/usr/bin/env python3
"""Bootstrap-only downloads and offline runtime checks, with isolated fixtures."""
from __future__ import annotations
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import offline_bootstrap as setup, offline_runtime as runtime


class OfflineChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/offline"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / "core"
        self.core.mkdir()

    def child(self, command):
        env = setup.clean_bootstrap_environment()
        for name, tail in (("CORE", ""), ("PACKAGES", "packages"), ("PLATFORMS", "platforms"),
                           ("GLOBALLIB", "lib"), ("CACHE", ".cache"), ("BUILD_CACHE", ".cache/build")):
            env[f"PLATFORMIO_{name}_DIR"] = str(self.core / tail)
        env.update(PYTHONDONTWRITEBYTECODE="1", TMP=str(self.root), TEMP=str(self.root), TMPDIR=str(self.root))
        return subprocess.run(command, cwd=self.root, env=env, capture_output=True, text=True,
                              errors="replace", timeout=30,
                              creationflags=0x08000000 if sys.platform == "win32" else 0)

    def test_guarded_cli_version_needs_no_network(self):
        result = self.child(runtime.offline_pio_command() + ["--version"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PlatformIO", result.stdout)
        self.assertFalse((self.core / "packages").exists())

    def test_missing_platform_stops_before_install_or_network(self):
        (self.root / "src").mkdir()
        (self.root / "src/main.cpp").write_text("int main() { return 0; }")
        (self.root / "platformio.ini").write_text("[env:offline]\nplatform = atmelavr\nboard = uno\nframework = arduino\n")
        result = self.child(runtime.offline_pio_command() + ["run", "-d", str(self.root)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap", result.stdout + result.stderr)
        self.assertFalse(any(self.core.rglob("package.json")))

    def test_network_denial_keeps_loopback_terminal_working(self):
        code = ("import sys,socket; sys.path.insert(0," + repr(str(ROOT)) + "); "
                "from src.modules.offline_runtime import activate,OfflineDependencyError; activate(); "
                "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(1); "
                "c=socket.socket(); c.connect(s.getsockname()); a,_=s.accept(); c.sendall(b'local'); "
                "assert a.recv(5)==b'local'; "
                "remote=socket.socket();\ntry: remote.connect(('192.0.2.1',443))\n"
                "except OfflineDependencyError: print('Network blocked before connect; loopback OK')\n"
                "else: raise AssertionError('Remote network allowed')")
        result = self.child([sys.executable, "-B", "-c", code])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("loopback OK", result.stdout)

    def test_runtime_cannot_spawn_pip_install(self):
        with self.assertRaisesRegex(runtime.OfflineDependencyError, "bootstrap"):
            runtime._audit("subprocess.Popen", (sys.executable, [sys.executable, "-m", "pip", "install", "demo"], None, {}))
        runtime._audit("subprocess.Popen", ("shell", ["shell", "--version"], None, {}))

    def test_package_guard_allows_installed_and_local_only(self):
        from platformio.package.manager._install import PackageManagerInstallMixin
        from platformio.package.exception import PackageException
        original = Mock(return_value="already-prepared")
        spec = SimpleNamespace(uri=None, humanize=lambda: "vendor/missing")
        manager = SimpleNamespace(ensure_spec=lambda value: value, get_package=Mock(return_value=None))
        with patch.object(PackageManagerInstallMixin, "_install", original):
            runtime.guard_platformio()
            with self.assertRaisesRegex(PackageException, "bootstrap"):
                PackageManagerInstallMixin._install(manager, spec)
            original.assert_not_called()
            manager.get_package.return_value = object()
            self.assertEqual(PackageManagerInstallMixin._install(manager, spec), "already-prepared")
            manager.get_package.return_value = None
            spec.uri = "symlink://" + str(self.root)
            self.assertEqual(PackageManagerInstallMixin._install(manager, spec), "already-prepared")
            spec.uri = "https://example.invalid/archive.zip"
            with self.assertRaises(PackageException):
                PackageManagerInstallMixin._install(manager, spec)
            with self.assertRaises(PackageException):
                PackageManagerInstallMixin._install(manager, spec, force=True)

    def test_bootstrap_environment_removes_inherited_runtime_restrictions(self):
        env = setup.clean_bootstrap_environment({"MCU_FLASHER_OFFLINE_RUNTIME": "1", "PIP_NO_INDEX": "1", "KEEP": "yes"})
        self.assertNotIn("PIP_NO_INDEX", env)
        self.assertNotIn("MCU_FLASHER_OFFLINE_RUNTIME", env)
        self.assertEqual(env["KEEP"], "yes")

    def test_certificate_checks_plan_host_architecture_and_missing_files(self):
        plan = {"schema": 1, "platforms": ["demo"], "libraries": []}
        manifest = self.core / "packages/demo/package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")
        payload = {"schema": setup.SCHEMA, "plan": setup.plan_hash(plan), "default_plan": setup.plan_hash(plan),
                   "host": sys.platform, "architecture": platform.machine(), "files": ["packages/demo/package.json"]}
        marker = self.core / setup.MARKER
        marker.write_text(json.dumps(payload))
        self.assertTrue(setup.ready(self.core, plan))
        self.assertFalse(setup.ready(self.core, dict(plan, libraries=["new/library"])))
        payload["architecture"] = "foreign"
        marker.write_text(json.dumps(payload))
        self.assertFalse(setup.ready(self.core, plan))
        payload["architecture"] = platform.machine()
        marker.write_text(json.dumps(payload))
        manifest.unlink()
        self.assertFalse(setup.ready(self.core, plan))

    def test_certificate_rejects_missing_child_guard_and_editor_assets(self):
        manifest = self.core / "package.json"
        manifest.write_text("{}")
        hook = self.root / "guard.pth"
        hook.write_text("# isolated guard fixture")
        payload = {"schema": setup.SCHEMA, "plan": setup.plan_hash(setup.load_plan()),
                   "default_plan": setup.plan_hash(setup.load_plan()), "host": sys.platform,
                   "architecture": platform.machine(), "files": ["package.json"],
                   "guards": [str(hook.relative_to(ROOT))]}
        (self.core / setup.MARKER).write_text(json.dumps(payload))
        self.assertTrue(setup.ready(self.core))
        hook.unlink()
        self.assertFalse(setup.ready(self.core))
        hook.write_text("# isolated guard fixture")
        with patch.object(setup, "ASSETS", ("temp/nonexistent-offline-editor-asset",)):
            self.assertFalse(setup.ready(self.core))

    def test_custom_plan_changes_certificate_identity_and_rejects_invalid_lists(self):
        path = self.root / "custom-plan.json"
        plan = {"schema": 1, "platforms": ["demo"], "libraries": ["vendor/sensor@1.0"]}
        path.write_text(json.dumps(plan))
        selected, selected_path = setup.requested_plan(["--repair", "--plan", str(path)])
        self.assertEqual(selected, plan)
        self.assertEqual(selected_path, path.resolve())
        self.assertNotEqual(setup.plan_hash(selected), setup.plan_hash(setup.load_plan()))
        with self.assertRaisesRegex(ValueError, "--plan requires"):
            setup.requested_plan(["--plan"])
        for invalid in (dict(plan, libraries="vendor/sensor"), dict(plan, platforms=[]), dict(plan, libraries=["\n"])):
            path.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError):
                setup.load_plan(path)

    def test_missing_private_runtime_cannot_install_from_workspace(self):
        from src.modules import private_python_guard as guard
        with patch.object(guard, "PRIVATE_PYTHON_DIR", self.root / "missing-python"), \
                patch.object(guard, "sys", SimpleNamespace(platform="win32")), \
                patch.object(guard, "_heal_private_runtime_if_needed", side_effect=AssertionError("Workspace installed runtime")):
            with self.assertRaisesRegex(RuntimeError, "bootstrap|run.vbs"):
                guard.get_private_python_exe()

    def test_preparation_refuses_workspace_role(self):
        with patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1"}):
            with self.assertRaisesRegex(RuntimeError, "bootstrap"):
                setup.prepare(self.core)
        self.assertFalse((self.core / setup.MARKER).exists())

    def test_bootstrap_certifies_only_after_all_builder_steps_succeed(self):
        from contextlib import ExitStack
        from platformio.package.manager import core, platform as platforms, tool, library
        from platformio.platform.factory import PlatformFactory
        folder = self.core / "platforms/demo"
        folder.mkdir(parents=True)
        (folder / "platform.json").write_text("{}")
        package = SimpleNamespace(path=str(folder))
        plan = {"schema": 1, "platforms": ["demo"], "libraries": []}
        for success in (False, True):
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, setup.clean_bootstrap_environment(), clear=True))
                stack.enter_context(patch.object(core, "get_core_package_dir"))
                manager = stack.enter_context(patch.object(platforms, "PlatformPackageManager"))
                manager.return_value.install.return_value = package
                stack.enter_context(patch.object(tool, "ToolPackageManager"))
                stack.enter_context(patch.object(library, "LibraryPackageManager"))
                stack.enter_context(patch.object(PlatformFactory, "new", return_value=SimpleNamespace(get_boards=lambda: {})))
                stack.enter_context(patch.object(setup, "package_plan", return_value=([], [("demo", "arduino")])))
                stack.enter_context(patch.object(setup, "install_runtime_guard", return_value=[]))
                run = stack.enter_context(patch.object(subprocess, "run", return_value=SimpleNamespace(
                    returncode=0 if success else 1, stdout="[]", stderr="simulated builder failure")))
                if success:
                    setup.prepare(self.core, plan, log=lambda *args: None)
                    self.assertTrue(setup.ready(self.core, plan))
                    self.assertEqual(run.call_count, 2)
                else:
                    with self.assertRaisesRegex(RuntimeError, "Builder preparation failed"):
                        setup.prepare(self.core, plan, log=lambda *args: None)
                    self.assertFalse((self.core / setup.MARKER).exists())

    def test_runtime_guard_hook_uses_launch_root_and_is_bootstrap_conditional(self):
        import sysconfig
        with patch.object(sysconfig, "get_paths", return_value={"purelib": str(self.root)}), patch.object(setup, "ROOT", self.root):
            setup.install_runtime_guard()
        hook = (self.root / "mcu_flasher_offline.pth").read_text()
        self.assertIn("MCU_FLASHER_APP_ROOT", hook)
        self.assertIn("MCU_FLASHER_OFFLINE_RUNTIME", hook)
        compile(hook, "<isolated-hook>", "exec")

    def test_runtime_guard_certificate_resolves_short_path_alias(self):
        actual = self.root / "application"
        actual.mkdir()
        alias = self.root / "short-path"
        if sys.platform == "win32":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(actual)],
                                    capture_output=True, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            alias.symlink_to(actual, target_is_directory=True)
        try:
            import sysconfig
            with patch.object(sysconfig, "get_paths", return_value={"purelib": str(alias)}), \
                    patch.object(setup, "ROOT", actual):
                hooks = setup.install_runtime_guard()
            self.assertEqual(hooks, ["mcu_flasher_offline.pth"])
            self.assertTrue((actual / hooks[0]).is_file())
        finally:
            if sys.platform == "win32":
                alias.rmdir()  # Remove only the fixture junction; preserve its target.
            else:
                alias.unlink()

    def test_all_board_framework_variants_and_optional_tools_are_planned(self):
        from platformio.platform.factory import PlatformFactory
        calls = []
        class Board:
            def __init__(self, name): self.id = name
            def get(self, key, default=None):
                return {"frameworks": ["arduino", "native_sdk"], "build.mcu": self.id}.get(key, default)
        class NativePlatform:
            def __init__(self):
                self.packages = {"debugger": {"owner": "vendor", "version": "1.0", "optional": True}}
            def configure_default_packages(self, options, targets):
                calls.append((options["board"], options["framework"][0]))
                self.packages[options["board"] + options["framework"][0]] = {"owner": "vendor", "version": "2.0"}
        with patch.object(PlatformFactory, "new", side_effect=lambda *args: NativePlatform()):
            specs, probes = setup.package_plan(object(), {name: Board(name) for name in ("one", "two")})
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(probes), 4)
        self.assertEqual(len(specs), 5)
        self.assertTrue(any("debugger" in spec.humanize() for spec in specs))

    def test_dependency_completeness_handles_builtin_libraries_and_cycles(self):
        from platformio.package.manager.base import BasePackageManager
        packages = {name: SimpleNamespace(path=str(self.root / name)) for name in ("sensor", "transport")}
        dependencies = {"sensor": None, "transport": []}
        manager = SimpleNamespace(
            get_pkg_dependencies=lambda item: dependencies[Path(item.path).name],
            dependency_to_spec=BasePackageManager.dependency_to_spec,
            get_package=lambda spec: packages.get(spec.name),
            is_builtin_lib=lambda name: name == "Wire",
        )
        setup.verify_package_dependencies(manager, packages["sensor"])
        dependencies["sensor"] = [{"name": "transport", "owner": "vendor"}]
        dependencies["transport"] = [{"name": "sensor", "owner": "vendor"}, {"name": "Wire"}]
        setup.verify_package_dependencies(manager, packages["sensor"])
        del packages["transport"]
        with self.assertRaisesRegex(RuntimeError, "vendor/transport"):
            setup.verify_package_dependencies(manager, packages["sensor"])

    def test_bootstrap_progress_burst_bounds_queued_gui_work(self):
        import ast
        import threading
        tree = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_stream_offline_setup_output")
        scope = {"threading": threading, "_record_bootstrap_log": lambda *args: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<isolated setup output>", "exec"), scope)
        callbacks, shown = [], []
        gui = SimpleNamespace(root=SimpleNamespace(after=lambda delay, callback: callbacks.append(callback)), log_dim=shown.append)
        process = SimpleNamespace(stdout=(f"{index}:" + "x" * 4000 for index in range(10000)))
        scope["_stream_offline_setup_output"](gui, process)
        self.assertEqual(len(callbacks), 1)
        callbacks.pop()()
        self.assertEqual(len(shown), 64)
        self.assertTrue(shown[-1].startswith("9999:"))
        self.assertLessEqual(sum(map(len, shown)), 64 * 3000)

    def test_workspace_bootstrap_button_launches_separate_sanitized_process(self):
        from main.qt import download_dialog
        with patch.object(download_dialog, "sys", SimpleNamespace(platform="win32")), \
                patch.object(download_dialog.subprocess, "Popen") as launch, \
                patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1", "PIP_NO_INDEX": "1"}):
            self.assertTrue(download_dialog.launch_download_manager())
        command = launch.call_args.args[0]
        self.assertTrue(command[-2].endswith("direct\\windows\\run.vbs") or command[-2].endswith("direct/windows/run.vbs"))
        self.assertEqual(command[-1], "--repair")
        self.assertNotIn("PIP_NO_INDEX", launch.call_args.kwargs["env"])

    def test_catalog_reads_bootstrap_snapshot_without_subprocess(self):
        from main.core import board_catalog
        payload = [{"id": "future", "name": "Future", "platform": "future", "frameworks": ["arduino"]}]
        (self.core / ".mcu-offline-catalog.json").write_text(json.dumps(payload))
        with patch.object(board_catalog, "_get_safe_platformio_core_dir", return_value=str(self.core)), \
                patch.object(board_catalog.subprocess, "run", side_effect=AssertionError("No online catalog process")):
            self.assertEqual(board_catalog.load_registry_board_catalog()[0]["id"], "future")


if __name__ == "__main__":
    unittest.main(verbosity=2)
