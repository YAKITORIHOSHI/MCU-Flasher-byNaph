#!/usr/bin/env python3
"""Verify bootstrap child handling with mocks and isolated synthetic scripts."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import bootstrap_platformio as entry, offline_bootstrap as setup
from src.modules import offline_runtime, windows_tool_paths as paths
import click


def popen_recorder(error=None):
    """A subclassable constructor recorder that never launches a process."""
    class RecordedPopen:
        constructor = Mock(side_effect=error)

        def __init__(self, args, *positional, **kwargs):
            RecordedPopen.constructor(args, *positional, **kwargs)

    return RecordedPopen


class BootstrapProcessChecks(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("MCU_FLASHER_OFFLINE_RUNTIME", None)

    def test_python_script_recognizes_sequence_paths_only(self):
        script = ROOT / "temp/audit/tool-scons/scons.py"
        for value in (str(script), os.fsencode(script), script):
            for sequence in (list, tuple):
                self.assertEqual(entry._python_script(sequence([sys.executable, value])), script)
        for args in (None, "python script.py", [], [sys.executable], [sys.executable, object()]):
            self.assertIsNone(entry._python_script(args))

    def test_scons_child_gets_guarded_script_entry_and_keeps_arguments(self):
        script = ROOT / "temp/audit/tool-scons/scons.py"
        fake = popen_recorder()
        with patch.object(subprocess, "Popen", fake), entry.builder_processes():
            result = subprocess.Popen([sys.executable, str(script), "-Q", "envdump"], cwd="fixture")
        self.assertIsInstance(result, fake)
        fake.constructor.assert_called_once_with(
            [sys.executable, "-B", str(Path(entry.__file__).absolute()), "--script", str(script),
             "-Q", "envdump"], cwd="fixture")

    def test_unrelated_processes_keep_their_arguments_and_stream_defaults(self):
        commands = (
            [sys.executable, "-c", "print('fixture')"],
            [sys.executable, "-m", "platformio", "--version"],
            [sys.executable, str(ROOT / "temp/audit/other/scons.py")],
            [sys.executable, str(ROOT / "temp/audit/scripts/other/install-deps.py")],
            "git --version",
        )
        fake = popen_recorder()
        with patch.object(subprocess, "Popen", fake), entry.builder_processes():
            for command in commands:
                subprocess.Popen(command, cwd="fixture")
        self.assertEqual([call.args[0] for call in fake.constructor.call_args_list], list(commands))
        self.assertTrue(all(call.kwargs == {"cwd": "fixture"} for call in fake.constructor.call_args_list))

    def test_zephyr_installer_receives_current_captured_streams(self):
        script = ROOT / "temp/audit/framework-zephyr/scripts/platformio/install-deps.py"
        stdout, stderr, fake = io.StringIO(), io.StringIO(), popen_recorder()
        with patch.object(subprocess, "Popen", fake), patch.object(sys, "stdout", stdout), \
                patch.object(sys, "stderr", stderr), entry.builder_processes():
            subprocess.Popen([sys.executable, script, "--platform", "atmelsam"], cwd="fixture")
        fake.constructor.assert_called_once_with(
            [sys.executable, script, "--platform", "atmelsam"], cwd="fixture", stdout=stdout, stderr=stderr)
        self.assertIn("Preparing Zephyr Git modules", stdout.getvalue())

    def test_zephyr_installer_respects_explicit_caller_streams(self):
        script = ROOT / "temp/audit/framework-zephyr/scripts/platformio/install-deps.py"
        fake = popen_recorder()
        with patch.object(subprocess, "Popen", fake), patch.object(sys, "stdout", io.StringIO()), \
                entry.builder_processes():
            subprocess.Popen([sys.executable, script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        fake.constructor.assert_called_once_with(
            [sys.executable, script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_builder_context_restores_hooks_after_body_error(self):
        original_popen, original_echo = subprocess.Popen, click.echo
        original_relpath = os.path.relpath
        with self.assertRaisesRegex(RuntimeError, "fixture error"):
            with entry.builder_processes():
                self.assertIsNot(subprocess.Popen, original_popen)
                self.assertIsNot(click.echo, original_echo)
                raise RuntimeError("fixture error")
        self.assertIs(subprocess.Popen, original_popen)
        self.assertIs(click.echo, original_echo)
        self.assertIs(os.path.relpath, original_relpath)

    def test_builder_context_restores_hooks_after_child_error(self):
        original_echo = click.echo
        fake = popen_recorder(OSError("fixture child error"))
        with patch.object(subprocess, "Popen", fake):
            with self.assertRaisesRegex(OSError, "fixture child error"):
                with entry.builder_processes():
                    subprocess.Popen([sys.executable, ROOT / "temp/audit/tool-scons/scons.py"])
            self.assertIs(subprocess.Popen, fake)
            self.assertIs(click.echo, original_echo)

    def test_builder_popen_remains_a_subclassable_process_type(self):
        original_popen = subprocess.Popen
        with entry.builder_processes():
            self.assertIsInstance(subprocess.Popen, type)
            self.assertTrue(issubclass(subprocess.Popen, original_popen))

            class WindowsStyleChild(subprocess.Popen):
                pass

            self.assertTrue(issubclass(WindowsStyleChild, original_popen))
        self.assertIs(subprocess.Popen, original_popen)

    def test_nested_context_restores_outer_and_original_hooks(self):
        original_popen, original_echo = subprocess.Popen, click.echo
        original_relpath = os.path.relpath
        with entry.builder_processes():
            outer_popen, outer_echo = subprocess.Popen, click.echo
            outer_relpath = os.path.relpath
            with entry.builder_processes():
                self.assertIsNot(subprocess.Popen, outer_popen)
            self.assertIs(subprocess.Popen, outer_popen)
            self.assertIs(click.echo, outer_echo)
            self.assertIs(os.path.relpath, outer_relpath)
        self.assertIs(subprocess.Popen, original_popen)
        self.assertIs(click.echo, original_echo)
        self.assertIs(os.path.relpath, original_relpath)

    @contextmanager
    def _idf_components_fixture(self, package="framework-espidf"):
        audit = ROOT / "temp/audit/bootstrap-processes"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            fixture = Path(directory)
            # Deep canonical spelling and a short junction reproduce the
            # original upward traversal clamping at the drive root.
            installation = fixture / "Installation With Spaces" / Path(*(["a"] * 20))
            components = installation / "packages" / package / "components"
            source = components / "bootloader_support/src/bootloader_common.c"
            source.parent.mkdir(parents=True)
            source.write_text("/* Isolated path fixture; never compiled. */\n", encoding="utf-8")
            alias = fixture / "short"
            if sys.platform == "win32":
                result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(installation)],
                                        capture_output=True, creationflags=0x08000000)
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                alias.symlink_to(installation, target_is_directory=True)
            try:
                yield alias, source, components
            finally:
                if sys.platform == "win32":
                    alias.rmdir()  # Fixture junction only; keep target for normal temp cleanup.
                else:
                    alias.unlink()

    @unittest.skipUnless(sys.platform == "win32", "Real Windows junction path handling")
    def test_idf_junction_keeps_application_and_bootloader_objects_in_build_tree(self):
        for package in ("framework-espidf", "framework-espidf@3.40407.0"):
            with self.subTest(package=package), self._idf_components_fixture(package) as (alias, source, components):
                short_source = alias / "packages" / package / "components/bootloader_support/src/bootloader_common.c"
                build = alias / "project/.pio/build/offline"
                broken = os.path.relpath(short_source, components)
                broken_application = Path(os.path.abspath(build / broken))
                broken_bootloader = Path(os.path.abspath(build / "bootloader" / broken))
                self.assertFalse(broken_application.is_relative_to(build),
                                 "Fixture must reproduce the original build-directory escape")
                self.assertEqual(broken_application, broken_bootloader,
                                 "Original path arithmetic must collide before the adapter")
                with entry.builder_processes():
                    relative = os.path.relpath(short_source, components)
                    self.assertEqual(relative, "bootloader_support\\src\\bootloader_common.c")
                    application = Path(os.path.abspath(build / relative))
                    bootloader = Path(os.path.abspath(build / "bootloader" / relative))
                    self.assertTrue(application.is_relative_to(build))
                    self.assertTrue(bootloader.is_relative_to(build))
                    self.assertNotEqual(application, bootloader)
                    self.assertEqual(os.path.relpath(source, components), relative)
                self.assertEqual(os.path.relpath(short_source, components), broken)
                self.assertFalse(source.with_suffix(".c.o").exists())

    def test_idf_relpath_leaves_unrelated_frameworks_bytes_and_relative_arguments_native(self):
        original = os.path.relpath
        with patch.object(paths.sys, "platform", "win32"), paths.espidf_component_relpaths(), \
                patch.object(paths.os.path, "realpath") as resolve:
            for source, base in (("C:/sdk/file.c", "C:/sdk/framework-zephyr/components"),
                                 ("C:/sdk/file.c", "C:/sdk/framework-espidf-other/components"),
                                 ("source.c", "C:/sdk/framework-espidf/components"),
                                 ("C:/sdk/file.c", "components"),
                                 (b"C:/sdk/file.c", b"C:/sdk/framework-espidf/components")):
                self.assertEqual(os.path.relpath(source, base), original(source, base))
            self.assertEqual(os.path.relpath("source.c"), original("source.c"))
            resolve.assert_not_called()

    def test_idf_relpath_requires_canonical_containment_and_same_drive(self):
        original = os.path.relpath
        base = "C:/sdk/framework-espidf/components"
        source = "C:/short/framework-espidf/components/file.c"
        for canonical_source in ("C:/sdk/framework-espidf/components-other/file.c", "D:/sdk/file.c"):
            with self.subTest(source=canonical_source), patch.object(paths.sys, "platform", "win32"), \
                    paths.espidf_component_relpaths(), patch.object(paths.os.path, "realpath", side_effect=[base, canonical_source]):
                self.assertEqual(os.path.relpath(source, base), original(source, base))

    def test_idf_relpath_adapter_leaves_linux_native(self):
        original = os.path.relpath
        with patch.object(paths.sys, "platform", "linux"), patch.object(paths.os.path, "realpath") as resolve, \
                paths.espidf_component_relpaths():
            self.assertIs(os.path.relpath, original)
            paths.install_espidf_component_relpaths()
            self.assertIs(os.path.relpath, original)
            resolve.assert_not_called()

    def test_offline_activation_installs_idf_adapter_once_and_keeps_download_guard(self):
        original = os.path.relpath
        with patch.object(paths.sys, "platform", "win32"), patch.object(paths.os.path, "relpath", original), \
                patch.object(sys, "meta_path", list(sys.meta_path)), \
                patch.object(offline_runtime, "_enabled", False), patch.object(sys, "addaudithook") as audit:
            offline_runtime.activate()
            adapted = os.path.relpath
            self.assertIsNot(adapted, original)
            offline_runtime.activate()
            self.assertIs(os.path.relpath, adapted)
            audit.assert_called_once_with(offline_runtime._audit)
            with self.assertRaises(offline_runtime.OfflineDependencyError):
                offline_runtime._audit("socket.connect", (None, ("example.invalid", 443)))
            self.assertEqual(os.environ["PIP_NO_INDEX"], "1")

    def test_offline_role_refuses_bootstrap_without_changing_hooks(self):
        original_popen, original_echo = subprocess.Popen, click.echo
        with patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1"}):
            with self.assertRaisesRegex(RuntimeError, "bootstrap"):
                with entry.builder_processes():
                    self.fail("Offline runtime entered bootstrap context")
        self.assertIs(subprocess.Popen, original_popen)
        self.assertIs(click.echo, original_echo)

    def test_envdump_omits_environment_and_keeps_plain_messages(self):
        stream = io.StringIO()
        with patch.object(sys, "argv", ["scons.py", "envdump"]), entry.builder_processes():
            click.echo("{'ENV': {'API_KEY': 'secretENV'}, 'OTHER': 1}", file=stream)
            click.echo('  {"ENV": {"API_KEY": "secretENV"}}', file=stream)
            click.echo("EARLY GIT FAILURE", file=stream)
            click.echo("{'OTHER': 1}", file=stream)
        self.assertNotIn("secretENV", stream.getvalue())
        self.assertEqual(stream.getvalue().count("Builder environment prepared."), 2)
        self.assertIn("EARLY GIT FAILURE", stream.getvalue())
        self.assertIn("{'OTHER': 1}", stream.getvalue())

    def test_non_envdump_messages_keep_normal_echo_behavior(self):
        stream = io.StringIO()
        message = "{'ENV': {'API_KEY': 'secretENV'}}"
        with patch.object(sys, "argv", ["scons.py", "build"]), entry.builder_processes():
            click.echo(message, file=stream, nl=False)
        self.assertEqual(stream.getvalue(), message)

    def test_windows_git_config_appends_without_changing_input(self):
        inherited = {
            "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "fixture.first", "GIT_CONFIG_VALUE_0": "one",
            "GIT_CONFIG_KEY_1": "core.longpaths", "GIT_CONFIG_VALUE_1": "false",
            "MCU_FLASHER_OFFLINE_RUNTIME": "1", "PIP_NO_INDEX": "1",
            "PYTHONHOME": "foreign", "PYTHONPATH": "foreign", "OTHER": "retained",
        }
        before = inherited.copy()
        with patch.object(setup.sys, "platform", "win32"):
            result = setup.clean_bootstrap_environment(inherited)
        self.assertEqual(inherited, before)
        for key in ("GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"):
            self.assertEqual(result[key], inherited[key])
        self.assertEqual(result["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(result["GIT_CONFIG_KEY_2"], "core.longpaths")
        self.assertEqual(result["GIT_CONFIG_VALUE_2"], "true")
        self.assertEqual(result["OTHER"], "retained")
        for key in ("MCU_FLASHER_OFFLINE_RUNTIME", "PIP_NO_INDEX", "PYTHONHOME", "PYTHONPATH"):
            self.assertNotIn(key, result)
        self.assertEqual(result["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(result["PYTHONUTF8"], "1")

    def test_zephyr_cmake_uses_canonical_base_and_keeps_short_tool_arguments(self):
        command = ["C:/short/packages/tool-cmake/bin/cmake", "-S", "C:/short/project", "-B", "C:/short/build"]
        environment = {"ZEPHYR_BASE": "C:/short/packages/framework-zephyr", "OTHER": "retained"}
        canonical = "C:\\Installation With Spaces\\packages\\framework-zephyr"
        with patch.object(paths.sys, "platform", "win32"), patch.object(paths.os.path, "realpath", return_value=canonical):
            result = paths.zephyr_cmake_environment(command, environment)
        self.assertEqual(result["ZEPHYR_BASE"], canonical.replace("\\", "/"))
        self.assertEqual(result["OTHER"], "retained")
        self.assertEqual(environment["ZEPHYR_BASE"], "C:/short/packages/framework-zephyr")
        self.assertEqual(command[2:], ["C:/short/project", "-B", "C:/short/build"])

    def test_unrelated_tools_and_native_linux_do_not_change_zephyr_environment(self):
        environment = {"ZEPHYR_BASE": "C:/short/packages/framework-zephyr"}
        with patch.object(paths.sys, "platform", "win32"), patch.object(paths.os.path, "realpath") as resolve:
            for command in (["git", "status"], "echo cmake -S fixture", [], ["other.exe"]):
                self.assertIs(paths.zephyr_cmake_environment(command, environment), environment)
            unrelated = {"ZEPHYR_BASE": "other-framework"}
            self.assertIs(paths.zephyr_cmake_environment(["cmake"], unrelated), unrelated)
            resolve.assert_not_called()
        with patch.object(paths.sys, "platform", "linux"), patch.object(paths.os.path, "realpath") as resolve:
            self.assertIs(paths.zephyr_cmake_environment(["cmake"], environment), environment)
            resolve.assert_not_called()

    def test_bootstrap_cmake_child_receives_consistent_base(self):
        fake = popen_recorder()
        environment = {"ZEPHYR_BASE": "C:/short/packages/framework-zephyr"}
        command = ["C:/short/packages/tool-cmake/bin/cmake", "-S", "fixture"]
        with patch.object(paths.sys, "platform", "win32"), \
                patch.object(paths.os.path, "realpath", return_value="C:/Actual Folder/framework-zephyr"), \
                patch.object(subprocess, "Popen", fake), entry.builder_processes():
            subprocess.Popen(command, env=environment)
        fake.constructor.assert_called_once_with(command, env={"ZEPHYR_BASE": "C:/Actual Folder/framework-zephyr"})
        self.assertEqual(environment["ZEPHYR_BASE"], "C:/short/packages/framework-zephyr")

    def test_offline_cmake_child_gets_same_base_and_install_boundary_remains(self):
        environment = {"ZEPHYR_BASE": "C:/short/packages/framework-zephyr", "PIP_NO_INDEX": "1"}
        with patch.object(paths.sys, "platform", "win32"), \
                patch.object(paths.os.path, "realpath", return_value="C:/Actual Folder/framework-zephyr"):
            offline_runtime._audit("subprocess.Popen", (None, '"C:\\Tool Folder\\cmake.exe" -S fixture', None, environment))
            with self.assertRaises(offline_runtime.OfflineDependencyError):
                offline_runtime._audit("subprocess.Popen", ("pip", ["pip", "install", "fixture"], None, environment))
        self.assertEqual(environment["ZEPHYR_BASE"], "C:/Actual Folder/framework-zephyr")
        self.assertEqual(environment["PIP_NO_INDEX"], "1")

    def test_windows_git_config_starts_at_zero_and_rejects_invalid_count(self):
        with patch.object(setup.sys, "platform", "win32"):
            result = setup.clean_bootstrap_environment({})
            self.assertEqual(result["GIT_CONFIG_COUNT"], "1")
            self.assertEqual(result["GIT_CONFIG_KEY_0"], "core.longpaths")
            self.assertEqual(result["GIT_CONFIG_VALUE_0"], "true")
            for count in ("-1", "invalid"):
                with self.assertRaises(ValueError):
                    setup.clean_bootstrap_environment({"GIT_CONFIG_COUNT": count})

    def test_linux_keeps_git_configuration_unchanged(self):
        inherited = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.longpaths",
                     "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_SYSTEM": "fixture-system"}
        with patch.object(setup.sys, "platform", "linux"):
            result = setup.clean_bootstrap_environment(inherited)
        self.assertEqual({key: value for key, value in result.items() if key.startswith("GIT_CONFIG")}, inherited)

    def test_real_script_wrapper_preserves_nested_failure_without_environment_dump(self):
        audit = ROOT / "temp/audit/bootstrap-processes"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            fixture = Path(directory)
            script = fixture / "tool-scons/scons.py"
            nested = fixture / "framework-zephyr/scripts/platformio/install-deps.py"
            script.parent.mkdir(parents=True)
            nested.parent.mkdir(parents=True)
            nested.write_text(
                "import sys\nprint('EARLY GIT FAILURE', file=sys.stderr, flush=True)\n"
                "print('Nested diagnostic stdout', flush=True)\nsys.exit(3)\n", encoding="utf-8")
            script.write_text(
                "import click,json,subprocess,sys\n"
                "click.echo(\"{'ENV': {'API_KEY': 'secretENV'}}\")\n"
                "click.echo('Synthetic builder message')\n"
                "print('SCRIPT_ARGV=' + json.dumps(sys.argv[1:]), flush=True)\n"
                f"sys.exit(subprocess.call([sys.executable, {str(nested)!r}]))\n", encoding="utf-8")
            environment = setup.clean_bootstrap_environment()
            environment.update(PYTHONDONTWRITEBYTECODE="1", TMP=str(fixture), TEMP=str(fixture), TMPDIR=str(fixture))
            for name, tail in (("CORE", "core"), ("PACKAGES", "core/packages"),
                               ("PLATFORMS", "core/platforms"), ("CACHE", "core/cache")):
                environment[f"PLATFORMIO_{name}_DIR"] = str(fixture / tail)
            result = subprocess.run(
                entry.command() + ["--script", str(script), "envdump", "fixture-argument"],
                cwd=fixture, env=environment, capture_output=True, text=True, errors="replace", timeout=20,
                creationflags=0x08000000 if sys.platform == "win32" else 0)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 3, output)
            self.assertIn("EARLY GIT FAILURE", result.stderr)
            self.assertIn("Nested diagnostic stdout", result.stdout)
            self.assertIn("Preparing Zephyr Git modules", result.stdout)
            self.assertIn("Builder environment prepared.", result.stdout)
            self.assertIn("Synthetic builder message", result.stdout)
            self.assertIn("SCRIPT_ARGV=" + json.dumps(["envdump", "fixture-argument"]), result.stdout)
            self.assertNotIn("secretENV", output)
            self.assertFalse((fixture / "core").exists(), "Synthetic verification touched a package store")
            self.assertFalse(any(fixture.rglob("__pycache__")))

    def test_guarded_cli_version_keeps_windows_asyncio_imports_working(self):
        audit = ROOT / "temp/audit/bootstrap-processes"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            fixture = Path(directory)
            environment = setup.clean_bootstrap_environment()
            environment.update(PYTHONDONTWRITEBYTECODE="1", TMP=str(fixture), TEMP=str(fixture), TMPDIR=str(fixture))
            for name, tail in (("CORE", "core"), ("PACKAGES", "core/packages"),
                               ("PLATFORMS", "core/platforms"), ("GLOBALLIB", "core/lib"),
                               ("CACHE", "core/cache")):
                environment[f"PLATFORMIO_{name}_DIR"] = str(fixture / tail)
            result = subprocess.run(
                entry.command() + ["--version"], cwd=fixture, env=environment,
                capture_output=True, text=True, errors="replace", timeout=20,
                creationflags=0x08000000 if sys.platform == "win32" else 0)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PlatformIO Core, version", result.stdout)
            self.assertFalse((fixture / "core/packages").exists())
            self.assertFalse(any(fixture.rglob("__pycache__")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
