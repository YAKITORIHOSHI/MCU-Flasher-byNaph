#!/usr/bin/env python3
"""Bootstrap-only downloads and offline runtime checks, with isolated fixtures."""
from __future__ import annotations
import json
import copy
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

    def test_bootstrap_environment_disables_real_platformio_telemetry_and_maintenance(self):
        from platformio import app, maintenance
        env = setup.clean_bootstrap_environment({"PLATFORMIO_SETTING_ENABLE_TELEMETRY": "true",
                                                "PLATFORMIO_DISABLE_UPGRADE_CHECK": "1"})
        self.assertEqual(env["PLATFORMIO_SETTING_ENABLE_TELEMETRY"], "false")
        self.assertEqual(env["PLATFORMIO_DISABLE_UPGRADE_CHECK"], "true")
        with patch.dict(os.environ, env, clear=True), \
                patch.object(app, "State", side_effect=AssertionError("Live settings were accessed")), \
                patch.object(maintenance.PlatformioCLI, "in_silence", return_value=False), \
                patch.object(maintenance, "check_platformio_upgrade") as upgrade, \
                patch.object(maintenance, "check_prune_system") as prune:
            self.assertFalse(app.get_setting("enable_telemetry"))
            maintenance.on_cmd_end()
            upgrade.assert_not_called()
            prune.assert_not_called()

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

    def test_framework_filter_requires_nonempty_list_and_rejects_old_certificate(self):
        path = self.root / "filtered-plan.json"
        plan = {"schema": 1, "platforms": ["demo"], "libraries": []}
        for frameworks in ("arduino", [], None, [""], [3], ["arduino\n"]):
            with self.subTest(frameworks=frameworks):
                candidate = dict(plan, frameworks=frameworks)
                path.write_text(json.dumps(candidate))
                with self.assertRaisesRegex(ValueError, "frameworks"):
                    setup.load_plan(path)
                with self.assertRaisesRegex(ValueError, "frameworks"):
                    setup._validate_plan(candidate)
        path.write_text(json.dumps(dict(plan, frameworks=["arduino"])))
        self.assertEqual(setup.load_plan(path)["frameworks"], ["arduino"])
        package = self.core / "package.json"
        package.write_text("{}")
        (self.core / setup.MARKER).write_text(json.dumps({
            "schema": 2, "plan": setup.plan_hash(plan), "default_plan": setup.plan_hash(plan),
            "host": sys.platform, "architecture": platform.machine(), "files": ["package.json"]}))
        self.assertFalse(setup.ready(self.core, plan))

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
        from src.modules import bootstrap_builders
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
                stack.enter_context(patch("src.modules.bootstrap_seed.prepare_scons"))
                manager = stack.enter_context(patch.object(platforms, "PlatformPackageManager"))
                manager.return_value.install.return_value = package
                stack.enter_context(patch.object(tool, "ToolPackageManager"))
                stack.enter_context(patch.object(library, "LibraryPackageManager"))
                stack.enter_context(patch.object(PlatformFactory, "new", return_value=SimpleNamespace(get_boards=lambda: {})))
                stack.enter_context(patch.object(setup, "package_plan", return_value=([], [("demo", "arduino")])))
                stack.enter_context(patch.object(setup, "install_runtime_guard", return_value=[]))
                builder = stack.enter_context(patch.object(bootstrap_builders, "run_builder"))
                if not success:
                    builder.side_effect = RuntimeError("Builder preparation failed for demo: simulated failure")
                run = stack.enter_context(patch.object(subprocess, "run", return_value=SimpleNamespace(
                    returncode=0 if success else 1, stdout="[]", stderr="simulated builder failure")))
                if success:
                    setup.prepare(self.core, plan, log=lambda *args: None)
                    self.assertTrue(setup.ready(self.core, plan))
                    self.assertEqual(run.call_count, 1)
                    builder.assert_called_once()
                else:
                    with self.assertRaisesRegex(RuntimeError, "Builder preparation failed"):
                        setup.prepare(self.core, plan, log=lambda *args: None)
                    self.assertFalse((self.core / setup.MARKER).exists())

    def test_builder_jobs_reject_invalid_direct_call_values(self):
        for jobs in (0, -1, True, 1.5, "2"):
            with self.subTest(jobs=jobs), self.assertRaisesRegex(ValueError, "positive integer"):
                setup.prepare(self.core, jobs=jobs)
        self.assertFalse((self.core / setup.MARKER).exists())

    def test_builder_scheduler_serializes_shared_state_and_bounds_reviewed_workers(self):
        from contextlib import ExitStack
        import threading
        import time
        from src.modules import bootstrap_builders
        from platformio.package.manager import core, platform as platforms, tool, library
        from platformio.platform.factory import PlatformFactory
        packages = {}
        for name in ("one", "two"):
            folder = self.core / "platforms" / name
            folder.mkdir(parents=True)
            (folder / "platform.json").write_text("{}")
            packages[name] = SimpleNamespace(path=str(folder))
        for reviewed in (False, True):
            with self.subTest(reviewed=reviewed), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, setup.clean_bootstrap_environment(), clear=True))
                stack.enter_context(patch("main.core.build_resources.get_optimal_compiler_jobs", return_value=2))
                stack.enter_context(patch("src.modules.bootstrap_seed.prepare_scons"))
                manager = stack.enter_context(patch.object(platforms, "PlatformPackageManager"))
                manager.return_value.install.side_effect = lambda name, **kwargs: packages[name]
                stack.enter_context(patch.object(tool, "ToolPackageManager"))
                stack.enter_context(patch.object(library, "LibraryPackageManager"))
                stack.enter_context(patch.object(PlatformFactory, "new", return_value=SimpleNamespace(get_boards=lambda: {})))
                stack.enter_context(patch.object(setup, "package_plan", return_value=([], [(str(i), "arduino") for i in range(4)])))
                stack.enter_context(patch.object(setup, "_parallel_builder_safe", return_value=reviewed))
                stack.enter_context(patch.object(setup, "install_runtime_guard", return_value=[]))
                stack.enter_context(patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="[]")))
                active = peak = 0
                lock = threading.Lock()
                outputs, logs = [], []

                def builder(command, **kwargs):
                    nonlocal active, peak
                    self.assertEqual(command[command.index("--jobs") + 1], "1")
                    self.assertEqual(kwargs["env"]["SCONSFLAGS"], "-j1")
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        outputs.append(Path(kwargs["output_path"]).name)
                    time.sleep(0.03)
                    with lock:
                        active -= 1

                stack.enter_context(patch.object(bootstrap_builders, "run_builder", side_effect=builder))
                setup.prepare(self.core, {"schema": 1, "platforms": ["one", "two"], "libraries": []},
                              log=logs.append, jobs=99)
                self.assertEqual(peak, 2 if reviewed else 1)
                self.assertEqual(set(outputs), {f"builder-{i:04d}.log" for i in range(1, 9)})
                self.assertTrue(any("two:0 (arduino) [1/4]" in line for line in logs))
                self.assertTrue(setup.ready(self.core, {"schema": 1, "platforms": ["one", "two"], "libraries": []}))

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

    def _variant_package_plan(self, baseline, configurations, *, allowed_frameworks=None):
        from platformio.platform.factory import PlatformFactory
        boards = {}
        configured = {}
        class Board:
            def __init__(self, name):
                self.id, self.frameworks = name, []
            def get(self, key, default=None):
                return {"frameworks": self.frameworks, "build.mcu": "same-mcu"}.get(key, default)
        for name, framework, packages in configurations:
            boards.setdefault(name, Board(name)).frameworks.append(framework)
            configured[(name, framework)] = packages
        class NativePlatform:
            def __init__(self):
                self.packages = copy.deepcopy(baseline)
                self.frameworks = {"arduino": {"package": "framework-arduino"},
                                   "espidf": {"package": "framework-espidf"}}
            def configure_default_packages(self, options, targets):
                self.packages = copy.deepcopy(configured[(options["board"], options["framework"][0])])
        with patch.object(PlatformFactory, "new", side_effect=lambda *_: NativePlatform()):
            return setup.package_plan(object(), boards, allowed_frameworks=allowed_frameworks)

    def test_explicit_framework_filter_keeps_selected_variants_and_optional_transports(self):
        baseline = {
            "compiler": {"owner": "old-owner", "version": "1.0.0", "type": "toolchain",
                         "optionalVersions": ["2.0.0", "3.0.0", "4.0.0"]},
            "framework-arduino": {"owner": "vendor", "version": "1.0.0", "type": "framework", "optional": True},
            "framework-espidf": {"owner": "vendor", "version": "1.0.0", "type": "framework", "optional": True},
            "uploader": {"owner": "vendor", "version": "1.0.0", "type": "uploader", "optional": True},
            "debugger": {"owner": "vendor", "version": "1.0.0", "type": "debugger", "optional": True},
        }
        arduino_one, arduino_two, espidf = (copy.deepcopy(baseline) for _ in range(3))
        for packages in (arduino_one, arduino_two):
            packages["framework-arduino"]["optional"] = False
        arduino_two["compiler"]["version"] = "3.0.0"
        espidf["compiler"].update(owner="new-owner", version="2.0.0")
        espidf["framework-espidf"]["optional"] = False
        specs, probes = self._variant_package_plan(baseline, [
            ("one", "arduino", arduino_one), ("one", "espidf", espidf),
            ("two", "arduino", arduino_two)], allowed_frameworks={"arduino"})
        self.assertEqual(set(probes), {("one", "arduino"), ("two", "arduino")})
        self.assertEqual({str(spec.requirements) for spec in specs if spec.name == "compiler"},
                         {"1.0.0", "3.0.0", "4.0.0"})
        self.assertNotIn("framework-espidf", {spec.name for spec in specs})
        self.assertTrue({"framework-arduino", "uploader", "debugger"}.issubset({spec.name for spec in specs}))

    def test_explicit_filter_retains_ambiguous_compiler_alternatives(self):
        baseline = {"compiler": {"owner": "old-owner", "version": "1.0.0", "type": "toolchain",
                                 "optionalVersions": ["2.0.0"]}}
        one, two = copy.deepcopy(baseline), copy.deepcopy(baseline)
        one["compiler"].update(owner="one-owner", version="2.0.0")
        two["compiler"].update(owner="two-owner", version="2.0.0")
        specs, probes = self._variant_package_plan(baseline, [
            ("selected", "arduino", baseline), ("one", "espidf", one),
            ("two", "espidf", two)], allowed_frameworks={"arduino"})
        self.assertEqual(probes, [("selected", "arduino")])
        self.assertIn(("old-owner", "2.0.0"), {(spec.owner, str(spec.requirements)) for spec in specs})

    def test_explicit_filter_fails_when_no_board_declares_it(self):
        baseline = {"compiler": {"owner": "vendor", "version": "1.0.0"}}
        with self.assertRaisesRegex(RuntimeError, "No declared board/framework"):
            self._variant_package_plan(baseline, [("demo", "espidf", baseline)],
                                       allowed_frameworks={"arduino"})

    def test_optional_version_uses_observed_framework_owner_after_migration(self):
        old = {"toolchain-riscv32-esp": {"owner": "espressif", "version": "8.4.0+2021r2-patch5",
                                        "optional": True, "optionalVersions": ["15.2.0+20251204"]}}
        arduino, espidf = copy.deepcopy(old), copy.deepcopy(old)
        arduino["toolchain-riscv32-esp"]["optional"] = False
        espidf["toolchain-riscv32-esp"].update(owner="platformio", version="15.2.0+20251204", optional=False)
        specs, probes = self._variant_package_plan(old, [("demo", "arduino", arduino),
                                                        ("demo", "espidf", espidf)])
        self.assertEqual({(spec.owner, str(spec.requirements)) for spec in specs}, {
            ("espressif", "8.4.0+2021r2-patch5"), ("platformio", "15.2.0+20251204")})
        self.assertEqual(set(probes), {("demo", "arduino"), ("demo", "espidf")})

    def test_unmatched_optional_version_and_all_primary_identities_stay_planned(self):
        baseline = {"compiler": {"owner": "old-owner", "version": "1.0.0", "optionalVersions": ["3.0.0"]}}
        changed = {"compiler": {"owner": "new-owner", "version": "2.0.0"}}
        specs, _ = self._variant_package_plan(baseline, [("demo", "arduino", changed)])
        self.assertEqual({(spec.owner, str(spec.requirements)) for spec in specs}, {
            ("old-owner", "1.0.0"), ("new-owner", "2.0.0"), ("old-owner", "3.0.0")})

    def test_ambiguous_optional_owner_keeps_authored_owner_and_all_primary_pairs(self):
        baseline = {"compiler": {"owner": "third-owner", "version": "1.0.0", "optionalVersions": ["2.0.0"]}}
        first = {"compiler": {"owner": "one-owner", "version": "2.0.0"}}
        second = {"compiler": {"owner": "two-owner", "version": "2.0.0"}}
        specs, _ = self._variant_package_plan(baseline, [("one", "arduino", first), ("two", "arduino", second)])
        self.assertEqual({(spec.owner, str(spec.requirements)) for spec in specs}, {
            ("third-owner", "1.0.0"), ("third-owner", "2.0.0"),
            ("one-owner", "2.0.0"), ("two-owner", "2.0.0")})

    def test_unknown_primary_owner_does_not_erase_optional_owner(self):
        baseline = {"compiler": {"owner": "vendor", "version": "1.0.0", "optionalVersions": ["2.0.0"]}}
        unspecified = {"compiler": {"version": "2.0.0"}}
        specs, _ = self._variant_package_plan(baseline, [("demo", "arduino", unspecified)])
        self.assertEqual({(spec.owner, str(spec.requirements)) for spec in specs}, {
            ("vendor", "1.0.0"), ("vendor", "2.0.0"), (None, "2.0.0")})

    def test_explicit_optional_owner_and_external_source_are_not_reassigned(self):
        baseline = {"compiler": {"owner": "old-owner", "version": "1.0.0",
                                  "optionalVersions": ["old-owner/compiler @ 2.0.0",
                                                       "https://example.invalid/compiler.tar.gz"]}}
        changed = {"compiler": {"owner": "new-owner", "version": "2.0.0"}}
        specs, _ = self._variant_package_plan(baseline, [("demo", "arduino", changed)])
        self.assertIn(("old-owner", "2.0.0"), {(spec.owner, str(spec.requirements)) for spec in specs})
        self.assertTrue(any(spec.uri == "https://example.invalid/compiler.tar.gz" for spec in specs))
        self.assertEqual(len(specs), 4)

    def test_duplicate_bare_variant_cannot_erase_explicit_owner(self):
        baseline = {"compiler": {"owner": "old-owner", "version": "1.0.0",
                                  "optionalVersions": ["old-owner/compiler @ 2.0.0"]}}
        bare = {"compiler": {"owner": "old-owner", "version": "1.0.0", "optionalVersions": ["2.0.0"]}}
        changed = {"compiler": {"owner": "new-owner", "version": "2.0.0"}}
        specs, _ = self._variant_package_plan(baseline, [("one", "arduino", bare), ("two", "arduino", changed)])
        self.assertEqual({(spec.owner, str(spec.requirements)) for spec in specs}, {
            ("old-owner", "1.0.0"), ("old-owner", "2.0.0"), ("new-owner", "2.0.0")})

    def test_optional_owner_resolution_waits_for_later_boards_and_is_order_independent(self):
        baseline = {"compiler": {"owner": "old-owner", "version": "1.0.0", "optionalVersions": ["2.0.0"]}}
        changed = {"compiler": {"owner": "new-owner", "version": "2.0.0"}}
        configurations = [("first", "arduino", baseline), ("last", "arduino", changed)]
        for order in (configurations, list(reversed(configurations))):
            with self.subTest(order=[item[0] for item in order]):
                specs, _ = self._variant_package_plan(baseline, order)
                self.assertEqual({(spec.owner, str(spec.requirements)) for spec in specs}, {
                    ("old-owner", "1.0.0"), ("new-owner", "2.0.0")})

    def test_probe_identity_uses_active_version_and_keeps_inactive_alternatives(self):
        baseline = {"compiler": {"owner": "vendor", "version": "1.0.0", "optionalVersions": ["2.0.0"]}}
        changed = {"compiler": {"owner": "vendor", "version": "1.0.0", "optionalVersions": ["3.0.0"]}}
        specs, probes = self._variant_package_plan(baseline, [("one", "arduino", baseline),
                                                             ("two", "arduino", changed)])
        self.assertEqual({str(spec.requirements) for spec in specs}, {"1.0.0", "2.0.0", "3.0.0"})
        self.assertEqual(probes, [("one", "arduino")], "Optional alternatives are not active builder inputs")

    def _host_gperf_plan(self, host, *, owner="platformio", raw_required=False,
                         required_framework=None):
        from platformio.platform.factory import PlatformFactory
        class Board:
            id = "demo"
            def get(self, key, default=None):
                return {"frameworks": ["arduino", "zephyr"], "build.mcu": "demo"}.get(key, default)
        class NativePlatform:
            def __init__(self):
                self.packages = {
                    "tool-gperf": {"owner": owner, "version": "^3.0.0", "optional": not raw_required,
                                   "optionalVersions": ["^4.0.0"]},
                    "tool-gperf-extra": {"owner": "platformio", "version": "1.0", "optional": True},
                    "tool-uploader": {"owner": "vendor", "version": "1.0", "optional": True,
                                      "type": "uploader"},
                    "tool-debugger": {"owner": "vendor", "version": "1.0", "optional": True,
                                      "type": "debugger"},
                    "tool-filesystem": {"owner": "vendor", "version": "1.0", "optional": True},
                    "custom-debugger": {"owner": "vendor", "version": "1.0", "optional": True},
                }
            def configure_default_packages(self, options, targets):
                framework = options["framework"][0]
                self.packages["framework-" + framework] = {"owner": "vendor", "version": "2.0"}
                # A later configuration must not erase a raw-manifest requirement.
                self.packages["tool-gperf"]["optional"] = True
                if framework == required_framework:
                    self.packages["tool-gperf"].update(optional=False, version="^5.0.0")
        log = Mock()
        with patch.object(setup.sys, "platform", host), \
                patch.object(PlatformFactory, "new", side_effect=lambda *args: NativePlatform()):
            specs, probes = setup.package_plan(object(), {"demo": Board()}, log=log)
        return specs, probes, log

    def test_windows_omits_only_unused_platformio_gperf_and_logs_once(self):
        specs, probes, log = self._host_gperf_plan("win32")
        identities = {(spec.owner, spec.name) for spec in specs}
        self.assertNotIn(("platformio", "tool-gperf"), identities)
        self.assertEqual(identities, {
            ("platformio", "tool-gperf-extra"), ("vendor", "tool-uploader"),
            ("vendor", "tool-debugger"), ("vendor", "tool-filesystem"),
            ("vendor", "custom-debugger"), ("vendor", "framework-arduino"),
            ("vendor", "framework-zephyr"),
        })
        self.assertEqual(set(probes), {("demo", "arduino"), ("demo", "zephyr")})
        log.assert_called_once()
        self.assertIn("tool-gperf", log.call_args.args[0])

    def test_linux_retains_all_optional_gperf_versions(self):
        specs, _, log = self._host_gperf_plan("linux")
        gperf = [spec for spec in specs if (spec.owner, spec.name) == ("platformio", "tool-gperf")]
        self.assertEqual(len(gperf), 2)
        self.assertTrue(any("^3.0.0" in spec.humanize() for spec in gperf))
        self.assertTrue(any("^4.0.0" in spec.humanize() for spec in gperf))
        log.assert_not_called()

    def test_windows_retains_gperf_when_any_framework_requires_it(self):
        specs, _, log = self._host_gperf_plan("win32", required_framework="zephyr")
        gperf = [spec for spec in specs if (spec.owner, spec.name) == ("platformio", "tool-gperf")]
        self.assertEqual(len(gperf), 3)
        for version in ("^3.0.0", "^4.0.0", "^5.0.0"):
            self.assertTrue(any(version in spec.humanize() for spec in gperf), version)
        log.assert_not_called()

    def test_windows_retains_gperf_required_in_raw_manifest(self):
        specs, _, log = self._host_gperf_plan("win32", raw_required=True)
        self.assertEqual(sum((spec.owner, spec.name) == ("platformio", "tool-gperf") for spec in specs), 2)
        log.assert_not_called()

    def test_windows_retains_optional_gperf_from_other_owner(self):
        specs, _, log = self._host_gperf_plan("win32", owner="vendor")
        self.assertEqual(sum((spec.owner, spec.name) == ("vendor", "tool-gperf") for spec in specs), 2)
        log.assert_not_called()

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

    @staticmethod
    def _isolated_setup_output():
        import ast
        import threading
        tree = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == "_stream_offline_setup_output")
        scope = {"threading": threading, "_record_bootstrap_log": lambda *args: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<isolated setup output>", "exec"), scope)
        return scope["_stream_offline_setup_output"]

    def test_bootstrap_output_burst_retains_early_errors_and_warnings(self):
        import io
        callbacks, shown = [], []
        gui = SimpleNamespace(root=SimpleNamespace(after=lambda delay, callback: callbacks.append(callback)),
                              log_dim=Mock(), log_warn=lambda text: shown.append(("warn", text)),
                              log_fail=lambda text: shown.append(("fail", text)), set_status=Mock())
        lines = "Error: required package failed\nWarning: missing dependency\n"
        lines += "".join(f"Diagnostic line {index}\n" for index in range(1000))
        self._isolated_setup_output()(gui, SimpleNamespace(stdout=io.StringIO(lines)))
        self.assertEqual(len(callbacks), 1)
        callbacks.pop()()
        self.assertEqual(shown, [("fail", "Error: required package failed"),
                                 ("warn", "Warning: missing dependency")])
        self.assertLessEqual(gui.log_dim.call_count + len(shown), 64)

    def test_bootstrap_protected_output_burst_is_lossless_and_bounded(self):
        import collections
        import io
        from queue import Empty, Queue
        import threading
        import time
        callbacks, shown, errors = Queue(), [], []
        peak = [0]
        class ObservedDeque(collections.deque):
            def append(self, value):
                super().append(value)
                peak[0] = max(peak[0], len(self))
        gui = SimpleNamespace(root=SimpleNamespace(after=lambda delay, callback: callbacks.put(callback)),
                              _signals=SimpleNamespace(thread_id=threading.get_ident(), _closed=False),
                              _closed=False, log_warn=lambda text: shown.append(("warn", text)),
                              log_fail=lambda text: shown.append(("fail", text)), set_status=Mock())
        lines = "".join(f"Warning: fixture {index}\nError: fixture {index}\n" for index in range(140))
        stream = self._isolated_setup_output()
        def produce():
            try:
                stream(gui, SimpleNamespace(stdout=io.StringIO(lines)))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=produce, daemon=True)
        with patch.object(collections, "deque", ObservedDeque):
            worker.start()
            deadline = time.monotonic() + 5
            try:
                while (worker.is_alive() or not callbacks.empty()) and time.monotonic() < deadline:
                    try:
                        callbacks.get(timeout=0.1)()
                    except Empty:
                        pass
            finally:
                gui._closed = True
                worker.join(2)
        self.assertFalse(worker.is_alive(), "Protected-output backpressure did not finish")
        self.assertEqual(errors, [])
        self.assertLessEqual(peak[0], 64)
        self.assertEqual(shown, [(kind, f"{prefix}: fixture {index}") for index in range(140)
                                 for kind, prefix in (("warn", "Warning"), ("fail", "Error"))])

    def test_bootstrap_output_shutdown_releases_backpressure(self):
        import collections
        import io
        import threading
        full, finished = threading.Event(), threading.Event()
        class ObservedDeque(collections.deque):
            def append(self, value):
                super().append(value)
                if len(self) == 64:
                    full.set()
        gui = SimpleNamespace(root=SimpleNamespace(after=Mock()), _closed=False)
        stream = self._isolated_setup_output()
        errors = []
        def produce():
            try:
                stream(gui, SimpleNamespace(stdout=io.StringIO("Warning: fixture\n" * 100)))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        worker = threading.Thread(target=produce, daemon=True)
        with patch.object(collections, "deque", ObservedDeque):
            worker.start()
            try:
                self.assertTrue(full.wait(2), "Fixture did not fill protected output")
                self.assertFalse(finished.is_set(), "Fixture did not exercise backpressure")
            finally:
                gui._closed = True
                worker.join(2)
        self.assertFalse(worker.is_alive(), "Closed setup left its output worker waiting")
        self.assertEqual(errors, [])

    def test_bootstrap_output_refuses_gui_thread_and_failed_scheduler(self):
        import io
        import threading
        stream = self._isolated_setup_output()
        process = SimpleNamespace(stdout=io.StringIO("Warning: fixture\n"))
        gui = SimpleNamespace(_signals=SimpleNamespace(thread_id=threading.get_ident()))
        with self.assertRaisesRegex(RuntimeError, "not the GUI thread"):
            stream(gui, process)
        gui = SimpleNamespace(root=SimpleNamespace(after=Mock(side_effect=RuntimeError("Dispatcher closed"))))
        with self.assertRaisesRegex(RuntimeError, "Dispatcher closed"):
            stream(gui, process)

    def test_bootstrap_progress_is_live_without_a_newline(self):
        from src.modules.bootstrap_output import output_chunks, PackageOutput
        code = ("import sys; sys.stdout.write('Unpacking 50%'); sys.stdout.flush(); "
                "sys.stdin.readline(); print(' 100%')")
        process = subprocess.Popen([sys.executable, "-B", "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   creationflags=0x08000000 if sys.platform == "win32" else 0)
        try:
            chunks = output_chunks(process.stdout)
            events = []
            output = PackageOutput(lambda *event: events.append(event))
            output.feed(next(chunks))
            self.assertEqual(events[-1][0], "progress")
            self.assertTrue(events[-1][1].endswith("50%"))
            process.stdin.write("\n")
            process.stdin.flush()
            for chunk in chunks:
                output.feed(chunk)
            output.finish()
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertIn(("ok", "Unpacked PlatformIO package — 100%"), events)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_builder_status_and_pip_success_finish_live_download_phase(self):
        from src.modules.bootstrap_output import PackageOutput
        events = []
        output = PackageOutput(lambda *event: events.append(event))
        output.feed("Preparing builder atmelsam:fixture (zephyr) [3/30]\n")
        output.feed("Downloading fixture.whl\nSuccessfully installed fixture-1.0\n")
        output.feed("Still preparing builder atmelsam:fixture (zephyr) — 20s elapsed\n")
        output.feed("Builder ready: fixture (21.0s)\n")
        output.finish()
        self.assertTrue(any(kind == "status" and "[3/30]" in text for kind, text in events))
        self.assertTrue(any(kind == "status" and "20s elapsed" in text for kind, text in events))
        self.assertTrue(any(kind == "ok" and "Successfully installed" in text for kind, text in events))
        self.assertTrue(any(kind == "ok" and "Builder ready:" in text for kind, text in events))
        self.assertFalse(any("interrupted" in text for _, text in events))

    def test_bootstrap_interrupted_unpack_and_mirror_retry_remain_truthful(self):
        from src.modules.bootstrap_output import PackageOutput
        events = []
        output = PackageOutput(lambda *event: events.append(event))
        output.feed("Tool Manager: Installing platformio/framework-arduino-avr-megacore @ ~3.1.0\n")
        output.feed("Downloading 0% 10% 20% 100%\nUnpacking 0% 10% 20% 50%")
        self.assertTrue(events[-1][1].endswith("50%"))
        output.feed("Unpacking...\nTool Manager: Warning! Package Mirror: [Errno 2] missing file\n")
        self.assertTrue(any(kind == "warn" and "interrupted at 50%" in text for kind, text in events))
        self.assertFalse(any(kind == "ok" and text.startswith("Unpacked") for kind, text in events))
        output.feed("Tool Manager: Looking for another mirror...\nDownloading 0%")
        self.assertTrue(events[-1][1].endswith("0%"))
        output.feed(" 100%\nUnpacking 0% 50%\n")
        # A verified installation is valid completion evidence when PIO omits 100%.
        output.feed("\x1b[3")
        output.feed("2mTool Manager: framework-arduino-avr-megacore@3.1.0 has been installed!\x1b[0m\n")
        output.finish()
        self.assertEqual(sum(kind == "ok" and text.startswith("Unpacked") for kind, text in events), 1)
        self.assertTrue(any(kind == "ok" and text.endswith("has been installed!") for kind, text in events))
        self.assertFalse(any("50%Unpacking" in text or "\x1b" in text for _, text in events))
        errors = []
        failed = PackageOutput(lambda *event: errors.append(event))
        failed.feed("Unpacking 50%\nOffline bootstrap failed: extraction failed\n")
        failed.finish()
        self.assertIn(("fail", "Offline bootstrap failed: extraction failed"), errors)
        self.assertFalse(any(kind == "ok" for kind, _ in errors))

    def test_bootstrap_output_decodes_split_unicode_and_bounds_lines(self):
        from src.modules.bootstrap_output import output_chunks, PackageOutput
        data = "Preparing toolkit 🚀\n".encode("utf-8")
        chunks = iter(bytes([value]) for value in data)
        stream = SimpleNamespace(buffer=SimpleNamespace(read1=lambda size: next(chunks, b"")))
        events = []
        output = PackageOutput(lambda *event: events.append(event))
        for chunk in output_chunks(stream):
            output.feed(chunk)
        output.feed("X" * 100000 + "\n")
        output.finish()
        self.assertIn(("dim", "Preparing toolkit 🚀"), events)
        self.assertLessEqual(max(len(text) for _, text in events), 3000)
        self.assertIn("display shortened", events[-1][1])

    def test_bootstrap_live_progress_burst_coalesces_gui_updates(self):
        import ast
        import io
        import threading
        tree = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_stream_offline_setup_output")
        scope = {"threading": threading, "_record_bootstrap_log": Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<isolated live output>", "exec"), scope)
        callbacks = []
        gui = SimpleNamespace(root=SimpleNamespace(after=lambda delay, callback: callbacks.append(callback)),
                              log_dim=Mock(), log_warn=Mock(), log_ok=Mock(), set_status=Mock(),
                              update_platformio_progress_block=Mock(), clear_platformio_progress_block=Mock())
        frames = "Tool Manager: Installing toolkit\nUnpacking 0%" + "".join(f" {i / 100:.2f}%" for i in range(10000))
        scope["_stream_offline_setup_output"](gui, SimpleNamespace(stdout=io.StringIO(frames)))
        self.assertEqual(len(callbacks), 1)
        callbacks.pop()()
        gui.update_platformio_progress_block.assert_called_once()
        self.assertTrue(gui.update_platformio_progress_block.call_args.args[0].endswith("99%"))
        gui.clear_platformio_progress_block.assert_called_once()
        gui.log_warn.assert_called_once_with("Unpacking toolkit interrupted at 99%")
        gui.log_ok.assert_not_called()

    def test_bootstrap_platformio_entrypoint_needs_no_installation_for_version(self):
        from src.modules.bootstrap_platformio import command
        result = self.child(command() + ["--version"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PlatformIO", result.stdout)
        self.assertFalse((self.core / "packages").exists())

    def test_bootstrap_preserves_short_core_alias_for_managers_and_children(self):
        from contextlib import ExitStack
        from src.modules import bootstrap_builders
        from platformio.package.manager import core, platform as platforms, tool, library
        from platformio.platform.factory import PlatformFactory
        alias = self.root / "short-core"
        if sys.platform == "win32":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(self.core)],
                                    capture_output=True, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            alias.symlink_to(self.core, target_is_directory=True)
        try:
            folder = alias / "platforms/demo"
            folder.mkdir(parents=True)
            (folder / "platform.json").write_text("{}")
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, setup.clean_bootstrap_environment(), clear=True))
                stack.enter_context(patch("src.modules.bootstrap_seed.prepare_scons"))
                manager = stack.enter_context(patch.object(platforms, "PlatformPackageManager"))
                manager.return_value.install.return_value = SimpleNamespace(path=str(folder))
                tools = stack.enter_context(patch.object(tool, "ToolPackageManager"))
                libraries = stack.enter_context(patch.object(library, "LibraryPackageManager"))
                stack.enter_context(patch.object(PlatformFactory, "new", return_value=SimpleNamespace(get_boards=lambda: {})))
                stack.enter_context(patch.object(setup, "package_plan", return_value=([], [("demo", "arduino")])))
                stack.enter_context(patch.object(setup, "install_runtime_guard", return_value=[]))
                builder = stack.enter_context(patch.object(bootstrap_builders, "run_builder"))
                run = stack.enter_context(patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="[]")))
                setup.prepare(alias, {"schema": 1, "platforms": ["demo"], "libraries": []}, log=lambda *args: None)
                manager.assert_called_once_with(str(alias / "platforms"))
                tools.assert_called_once_with(str(alias / "packages"))
                libraries.assert_called_once_with(str(alias / "lib"))
                for call in [*run.call_args_list, *builder.call_args_list]:
                    self.assertEqual(call.kwargs["env"]["PLATFORMIO_CORE_DIR"], str(alias))
                    self.assertTrue(call.args[0][2].endswith("bootstrap_platformio.py"))
                self.assertEqual(run.call_args.kwargs["env"]["PLATFORMIO_CACHE_DIR"], str(alias / ".cache"))
                child_env = builder.call_args.kwargs["env"]
                project = Path(builder.call_args.args[0][builder.call_args.args[0].index("-d") + 1])
                self.assertEqual(project.parent, alias)
                self.assertEqual(child_env["PLATFORMIO_CACHE_DIR"], str(project / ".pio-cache"))
                self.assertEqual(child_env["PLATFORMIO_BUILD_CACHE_DIR"], str(project / ".pio-cache/build"))
                for name in ("TEMP", "TMP", "TMPDIR"):
                    self.assertEqual(child_env[name], str(project / ".tmp"))
                self.assertEqual(child_env["SCONSFLAGS"], "-j1")
                self.assertEqual(child_env["PLATFORMIO_RUN_JOBS"], "1")
        finally:
            if sys.platform == "win32":
                alias.rmdir()  # Delete only this fixture junction entry.
            else:
                alias.unlink()

    def test_bootstrap_extracts_deep_zip_and_tar_and_keeps_traversal_checks(self):
        import io
        import shutil
        import tarfile
        import zipfile
        from platformio.package.unpack import FileUnpacker
        from platformio.package.exception import PackageException
        from src.modules.bootstrap_platformio import archive_paths, extended_windows_path
        original = FileUnpacker.unpack
        relative = ("bootloaders/urboot/atmega1280/watchdog_1_s/external_oscillator/1000000_hz/"
                    "125000_baud/uart0_rxe0_txe1/led+b7/urboot_atmega1280_pr_ee_ce.hex")
        payload = b"representative bootloader content"
        for kind in ("zip", "tar.gz"):
            archive = self.root / ("framework." + kind)
            destination = self.root / ("extraction-" + "x" * 70)
            destination.mkdir()
            target = destination / relative
            self.assertGreater(len(str(target)), 260)
            try:
                for insecure in (False, True):
                    if kind == "zip":
                        with zipfile.ZipFile(archive, "w") as writer:
                            writer.writestr(relative, payload)
                            if insecure:
                                writer.writestr("../outside.txt", b"must be blocked")
                    else:
                        with tarfile.open(archive, "w:gz") as writer:
                            info = tarfile.TarInfo(relative)
                            info.size = len(payload)
                            writer.addfile(info, io.BytesIO(payload))
                            if insecure:
                                escape = tarfile.TarInfo("../outside.txt")
                                escape.size = 0
                                writer.addfile(escape, io.BytesIO())
                    with archive_paths(), FileUnpacker(str(archive)) as unpacker:
                        if insecure:
                            with self.assertRaises((PackageException, tarfile.FilterError)):
                                unpacker.unpack(str(destination), with_progress=False)
                        else:
                            self.assertTrue(unpacker.unpack(str(destination), with_progress=False))
                path = extended_windows_path(target) if sys.platform == "win32" else str(target)
                with open(path, "rb") as stream:
                    self.assertEqual(stream.read(), payload)
                self.assertFalse((self.root / "outside.txt").exists())
                self.assertIs(FileUnpacker.unpack, original)
            finally:
                # The target is an exact, owned child of this temporary fixture.
                self.assertEqual(destination.parent.resolve(), self.root.resolve())
                shutil.rmtree(extended_windows_path(destination) if sys.platform == "win32" else destination)

    def test_bootstrap_archive_paths_restore_after_errors_and_leave_linux_native(self):
        from platformio.package.unpack import FileUnpacker
        from src.modules import bootstrap_platformio
        original = FileUnpacker.unpack
        from platformio.package.unpack import ZIPArchiver
        metadata = {name: ZIPArchiver.__dict__[name] for name in ("preserve_permissions", "preserve_mtime")}
        with patch.object(bootstrap_platformio.sys, "platform", "linux"):
            with bootstrap_platformio.archive_paths():
                self.assertIs(FileUnpacker.unpack, original)
        with self.assertRaisesRegex(RuntimeError, "fixture error"):
            with bootstrap_platformio.archive_paths():
                raise RuntimeError("fixture error")
        self.assertIs(FileUnpacker.unpack, original)
        for name, method in metadata.items():
            self.assertIs(ZIPArchiver.__dict__[name], method)

    def test_workspace_download_manager_button_launches_browser(self):
        from main.qt import download_dialog
        with patch.object(download_dialog, "_find_python_executable", return_value=Path("C:/test/pythonw.exe")), \
                patch.object(download_dialog.subprocess, "Popen") as launch:
            self.assertTrue(download_dialog.launch_download_manager())
        command = launch.call_args.args[0]
        self.assertTrue(str(command[1]).endswith("arduino_lib_req.py"))
        self.assertEqual(launch.call_args.kwargs["env"]["MCU_PREF_DIR"], str(download_dialog.ROOT))

    def test_catalog_reads_bootstrap_snapshot_without_subprocess(self):
        from main.core import board_catalog
        payload = [{"id": "future", "name": "Future", "platform": "future", "frameworks": ["arduino"]}]
        (self.core / ".mcu-offline-catalog.json").write_text(json.dumps(payload))
        with patch.object(board_catalog, "_get_safe_platformio_core_dir", return_value=str(self.core)), \
                patch.object(board_catalog.subprocess, "run", side_effect=AssertionError("No online catalog process")):
            self.assertEqual(board_catalog.load_registry_board_catalog()[0]["id"], "future")


if __name__ == "__main__":
    unittest.main(verbosity=2)
