#!/usr/bin/env python3
"""Check Mbed provider selection with isolated synthetic packages, never builders."""
from __future__ import annotations

import ast
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import mbed_compat as compat, offline_runtime, bootstrap_platformio


class MbedCompatibilityChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/mbed-compat"
        audit.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.core = self.root / "core"
        self.package = self.core / "packages/framework-mbed"
        self.adapter = self.package / "platformio/pio_mbed_adapter.py"
        self.site = self.root / "site"
        self.bundle = self.package / "platformio/package_deps/py3"
        self._write(self.site / "setuptools/__init__.py", "__version__ = '80.9.0'\n")
        self._write(self.site / "distutils/__init__.py", "# Synthetic compatibility API fixture.\n")
        self._write(self.site / "distutils/spawn.py", "def find_executable(name): return None\n")
        self._write(self.site / "distutils/version.py", "class LooseVersion: pass\n")
        self._write(self.site / "future/__init__.py", "__version__ = '1.0.0'\n")
        self._write(self.site / "future/utils.py", "provider = 'prepared'\n")
        self._write(self.site / "past/__init__.py", "# Prepared past fixture.\n")
        self._write(self.site / "past/builtins/__init__.py", "def cmp(a, b): return (a > b) - (a < b)\n")
        for name, version in (("setuptools", compat.SETUPTOOLS_VERSION), ("future", compat.FUTURE_VERSION)):
            self._write(self.site / f"{name}-{version}.dist-info/METADATA",
                        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        self._write(self.bundle / "future/__init__.py", "__version__ = '0.18.1'\n")
        self._write(self.bundle / "future/utils.py", "raise RuntimeError('obsolete future selected')\n")
        self._write(self.bundle / "past/__init__.py", "# Obsolete past fixture.\n")
        self._write(self.bundle / "past/builtins/__init__.py", "from imp import reload\n")
        self._write(self.adapter,
                    "import sys\nfrom pathlib import Path\n"
                    "sys.path.insert(0, str(Path(__file__).parent / 'package_deps/py3'))\n"
                    "from future.utils import provider\nfrom past.builtins import cmp\n"
                    "from distutils.version import LooseVersion\nassert provider == 'prepared'\n"
                    "assert cmp(1, 2) == -1\n")

    @staticmethod
    def _write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _child(self, body):
        code = ("import os, sys\n"
                f"sys.path[:0] = [{str(self.site)!r}, {str(ROOT)!r}, {str(self.adapter.parent)!r}]\n"
                f"os.environ['PLATFORMIO_CORE_DIR'] = {str(self.core)!r}\n"
                "os.environ.pop('PLATFORMIO_PACKAGES_DIR', None)\n"
                "from src.modules import mbed_compat as compat\n"
                # Prevent fixture imports from concealing an installer/build or writes.
                "def readonly(event, args):\n"
                "    if event == 'open' and ((isinstance(args[1], str) and any(c in args[1] for c in 'wax+')) or "
                "(isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))):\n"
                "        raise AssertionError('write during compatibility import')\n"
                "    if event in {'subprocess.Popen', 'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', "
                "'socket.connect', 'socket.getaddrinfo'}:\n"
                "        raise AssertionError('side effect during compatibility import: ' + event)\n"
                "sys.addaudithook(readonly)\n" + body)
        env = dict(os.environ)
        for name in ("MCU_FLASHER_OFFLINE_RUNTIME", "PYTHONHOME", "PYTHONPATH"):
            env.pop(name, None)
        result = subprocess.run([sys.executable, "-S", "-B", "-c", code], cwd=self.root,
                                capture_output=True, text=True, timeout=15, env=env,
                                creationflags=0x08000000 if sys.platform == "win32" else 0)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prepared_providers_beat_mbed_bundle_without_modifying_it(self):
        before = {str(path): path.read_bytes() for path in self.bundle.rglob("*.py")}
        self._child("with compat.mbed_compat():\n    import pio_mbed_adapter\n"
                    f"assert sys.modules['future'].__file__.startswith({str(self.site)!r})\n"
                    f"assert sys.modules['past.builtins'].__file__.startswith({str(self.site)!r})\n")
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.bundle.rglob("*.py")})
        self.assertFalse(list(self.root.rglob("__pycache__")))

    def test_missing_provider_reports_repair_before_adapter_body(self):
        (self.site / "future-1.0.0.dist-info/METADATA").unlink()
        self._child("with compat.mbed_compat():\n"
                    "    try: import pio_mbed_adapter\n"
                    "    except RuntimeError as exc:\n"
                    "        assert 'Prepare them in bootstrap' in str(exc)\n"
                    "        assert 'pio_mbed_adapter' not in sys.modules\n"
                    "    else: raise AssertionError('missing provider accepted')\n")

    def test_wrong_provider_version_is_rejected(self):
        self._write(self.site / "setuptools-80.9.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: setuptools\nVersion: 84.0.0\n")
        self._child("try: compat.prepare_dependencies()\n"
                    "except RuntimeError as exc: assert '80.9.0 is required; found 84.0.0' in str(exc)\n"
                    "else: raise AssertionError('incompatible provider accepted')\n")

    def test_loaded_obsolete_package_is_rejected_without_eviction(self):
        self._child(f"sys.path.insert(0, {str(self.bundle)!r})\nimport future\n"
                    "sys.path.pop(0)\noriginal = future\n"
                    "try: compat.prepare_dependencies()\n"
                    "except RuntimeError as exc: assert 'unprepared provider' in str(exc)\n"
                    "else: raise AssertionError('loaded obsolete provider accepted')\n"
                    "assert sys.modules['future'] is original\n")

    def test_finder_is_lazy_and_scoped_to_selected_installed_adapter(self):
        finder = compat._MbedFinder()
        spec = importlib.machinery.ModuleSpec("pio_mbed_adapter", loader=None, origin=str(self.adapter))
        with patch.dict(os.environ, {"PLATFORMIO_PACKAGES_DIR": str(self.core / "packages")}), \
                patch.object(compat, "prepare_dependencies") as prepare, \
                patch.object(compat.importlib.machinery.PathFinder, "find_spec", return_value=spec) as find:
            self.assertIsNone(finder.find_spec("unrelated_framework"))
            find.assert_not_called()
            self.assertIs(finder.find_spec("pio_mbed_adapter"), spec)
            prepare.assert_called_once_with()
            for origin in (self.root / "pio_mbed_adapter.py",
                           self.core / "packages/framework-other/platformio/pio_mbed_adapter.py",
                           self.core / "packages/framework-mbed-other/platformio/pio_mbed_adapter.py",
                           self.root / "other/packages/framework-mbed/platformio/pio_mbed_adapter.py",
                           self.core / "packages/framework-mbed/nested/platformio/pio_mbed_adapter.py"):
                spec.origin = str(origin)
                self.assertIsNone(finder.find_spec("pio_mbed_adapter"))
            self.assertEqual(prepare.call_count, 1)

    def test_versioned_framework_and_canonical_package_alias_are_supported(self):
        versioned = self.core / "packages/framework-mbed@6.61700.0/platformio/pio_mbed_adapter.py"
        self._write(versioned, "# Fixture; never executed.\n")
        with patch.dict(os.environ, {"PLATFORMIO_PACKAGES_DIR": str(self.core / "packages")}):
            self.assertTrue(compat._installed_adapter(str(versioned)))
        # Canonical package comparison also follows real junctions/symlinks.
        alias = self.root / "alias"
        if sys.platform == "win32":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(self.core)],
                                    capture_output=True, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(self.core, target_is_directory=True)
            self.addCleanup(alias.unlink)
        with patch.dict(os.environ, {"PLATFORMIO_PACKAGES_DIR": str(alias / "packages")}):
            self.assertTrue(compat._installed_adapter(str(self.adapter)))
            self.assertTrue(compat._installed_adapter(str(alias / "packages/framework-mbed/platformio/pio_mbed_adapter.py")))

    def test_context_is_nested_reversible_and_preserves_other_finders(self):
        initial = list(sys.meta_path)
        with patch.object(sys, "meta_path", initial.copy()), patch.object(compat, "prepare_dependencies") as prepare:
            other = object()
            with self.assertRaisesRegex(RuntimeError, "fixture error"):
                with bootstrap_platformio.builder_processes():
                    finder = compat.install_mbed_compat()
                    with compat.mbed_compat():
                        self.assertIs(compat.install_mbed_compat(), finder)
                    self.assertIn(finder, sys.meta_path)
                    sys.meta_path.append(other)
                    raise RuntimeError("fixture error")
            self.assertEqual(sys.meta_path, [*initial, other])
            prepare.assert_not_called()

    def test_framework_junction_escaping_selected_store_does_not_select_providers(self):
        external = self.root / "foreign/framework-mbed"
        self._write(external / "platformio/pio_mbed_adapter.py", "# Fixture; never executed.\n")
        alias = self.core / "packages/framework-mbed@external"
        if sys.platform == "win32":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(external)],
                                    capture_output=True, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(external, target_is_directory=True)
            self.addCleanup(alias.unlink)
        with patch.dict(os.environ, {"PLATFORMIO_PACKAGES_DIR": str(self.core / "packages")}):
            self.assertFalse(compat._installed_adapter(str(alias / "platformio/pio_mbed_adapter.py")))

    def test_runtime_activation_is_idempotent_lazy_and_keeps_network_guard(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(sys, "meta_path", list(sys.meta_path)), \
                patch.object(sys, "addaudithook") as audit, patch.object(offline_runtime, "_enabled", False), \
                patch.object(compat, "prepare_dependencies") as prepare, \
                patch("src.modules.windows_tool_paths.install_espidf_component_relpaths"):
            offline_runtime.activate()
            finder = compat.install_mbed_compat()
            offline_runtime.activate()
            with compat.mbed_compat():
                self.assertIs(compat.install_mbed_compat(), finder)
            self.assertIn(finder, sys.meta_path)
            self.assertEqual(sum(getattr(item, "_mcu_mbed_compat", False) is True for item in sys.meta_path), 1)
            prepare.assert_not_called()
            audit.assert_called_once_with(offline_runtime._audit)
            with self.assertRaises(offline_runtime.OfflineDependencyError):
                offline_runtime._audit("socket.connect", (None, ("example.invalid", 443)))

    def test_bootstrap_check_uses_target_python_and_real_api_validation(self):
        tree = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_check_import_mbed_python")
        checker, runner = Mock(return_value=True), Mock(return_value=SimpleNamespace(returncode=0))
        scope = {"_check_spec": checker, "_get_target_python": lambda: self.root / "target/python",
                 "subprocess": SimpleNamespace(run=runner, DEVNULL=subprocess.DEVNULL, CREATE_NO_WINDOW=0x08000000),
                 "sys": SimpleNamespace(platform="win32"), "SCRIPT_DIR": self.root}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "<isolated Mbed check>", "exec"), scope)
        self.assertTrue(scope["_check_import_mbed_python"]())
        self.assertEqual(runner.call_args.args[0][0], str(self.root / "target/python"))
        self.assertIn("prepare_dependencies()", runner.call_args.args[0][-1])
        self.assertEqual(runner.call_args.kwargs["cwd"], self.root)
        runner.return_value.returncode = 1
        self.assertFalse(scope["_check_import_mbed_python"]())
        runner.side_effect = OSError("fixture")
        self.assertFalse(scope["_check_import_mbed_python"]())
        checker.return_value = False
        runner.reset_mock()
        self.assertFalse(scope["_check_import_mbed_python"]())
        runner.assert_not_called()
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "PIP_PACKAGES_SPEC" for target in node.targets))
        spec = next(node for node in assignment.value.elts if isinstance(node, ast.Dict)
                    and any(isinstance(key, ast.Constant) and key.value == "id" and isinstance(value, ast.Constant)
                            and value.value == "mbed_python" for key, value in zip(node.keys, node.values)))
        actual = eval(compile(ast.Expression(body=spec), "<isolated dependency spec>", "eval"),
                      {"_check_import_mbed_python": scope["_check_import_mbed_python"], "_MBED_PYTHON_REQUIREMENTS": compat.REQUIREMENTS})
        self.assertTrue(actual["critical"])
        self.assertEqual(actual["pip_args"], list(compat.REQUIREMENTS))
        requirements = (ROOT / "direct/ubuntu/requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(all(requirement in requirements for requirement in compat.REQUIREMENTS))


class MbedAvailabilityChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/mbed-availability"
        audit.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.core = self.root / "core"
        self.packages = self.core / "packages"
        self.framework = self.packages / "framework-mbed"
        self.framework.mkdir(parents=True)
        self.write("package.json", {"name": "framework-mbed", "version": "6.61700.231105"})
        self.write("platformio/variants_remap.json", {"cloud_jam": "NUCLEO_F401RE"})
        self.write("targets/targets.json", {"NUCLEO_F103RB": {}, "NUCLEO_F401RE": {}})
        self.manifest = {"id": "olimex_f103", "name": "Olimex STM32-H103", "vendor": "Olimex",
                         "url": "https://www.olimex.com/Products/ARM/ST/STM32-H103/",
                         "frameworks": ["arduino", "cmsis", "mbed", "libopencm3", "stm32cube"],
                         "build": {"core": "stm32", "cpu": "cortex-m3", "mcu": "stm32f103rbt6"}}
        environment = patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": str(self.core),
                                              "PLATFORMIO_PACKAGES_DIR": str(self.packages)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, filename, value):
        path = self.framework / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def reason(self, **kwargs):
        return compat.unavailable_framework_reason(kwargs.get("platform", "ststm32"),
                                                   kwargs.get("version", "20.0.0"),
                                                   kwargs.get("board", self.manifest),
                                                   kwargs.get("framework", self.framework))

    def test_missing_exact_target_reports_reason_for_actual_platform_board_object(self):
        from platformio.platform.board import PlatformBoardConfig
        path = self.root / "olimex_f103.json"
        manifest = dict(self.manifest)
        manifest.pop("id")
        path.write_text(json.dumps(manifest), encoding="utf-8")
        board = PlatformBoardConfig(str(path))
        before = {str(path): path.read_bytes() for path in self.framework.rglob("*") if path.is_file()}
        reason = self.reason(board=board)
        self.assertIn("Mbed OS 6.17 has no OLIMEX_F103 target", reason)
        self.assertIn("Olimex STM32-H103", reason)
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.framework.rglob("*") if path.is_file()})

    def test_unknown_platform_version_and_changed_board_identity_keep_probe(self):
        for changes in ({"platform": "atmelsam"}, {"version": "20.0.1"}):
            self.assertIsNone(self.reason(**changes))
        for key, value in (("id", "custom_board"), ("name", "Custom hardware"),
                           ("vendor", "Custom vendor"), ("frameworks", ["arduino"]),
                           ("frameworks", None), ("frameworks", "mbed")):
            self.assertIsNone(self.reason(board=dict(self.manifest, **{key: value})))
        for key, value in (("mcu", "stm32f103c8"), ("cpu", "cortex-m4"), ("core", "custom")):
            board = dict(self.manifest, build=dict(self.manifest["build"], **{key: value}))
            self.assertIsNone(self.reason(board=board))

    def test_explicit_mbed_variant_preserves_custom_target_even_when_unavailable(self):
        for target in ("NUCLEO_F103RB", "CUSTOM_H103", "OLIMEX_F103", ""):
            board = dict(self.manifest, build=dict(self.manifest["build"], mbed_variant=target))
            self.assertIsNone(self.reason(board=board))

    def test_new_target_definition_or_authored_remap_prevents_exclusion(self):
        self.write("targets/targets.json", {"NUCLEO_F103RB": {}, "OLIMEX_F103": {"inherits": ["NUCLEO_F103RB"]}})
        self.assertIsNone(self.reason())
        self.write("targets/targets.json", {"NUCLEO_F103RB": {}})
        for target in ("NUCLEO_F103RB", "CUSTOM_H103", "OLIMEX_F103", ""):
            self.write("platformio/variants_remap.json", {"olimex_f103": target})
            self.assertIsNone(self.reason())

    def test_unknown_package_name_version_folder_or_missing_core_keeps_probe(self):
        for metadata in ({"name": "framework-other", "version": "6.61700.231105"},
                         {"name": "framework-mbed", "version": "6.61800.0"}):
            self.write("package.json", metadata)
            self.assertIsNone(self.reason())
        self.write("package.json", {"name": "framework-mbed", "version": "6.61700.231105"})
        destination = self.packages / "custom-framework"
        self.framework.rename(destination)
        self.assertIsNone(self.reason(framework=destination))
        destination.rename(self.framework)
        for key in ("PLATFORMIO_CORE_DIR", "PLATFORMIO_PACKAGES_DIR"):
            value = os.environ.pop(key)
            try:
                self.assertIsNone(self.reason())
            finally:
                os.environ[key] = value

    def test_versioned_reviewed_framework_is_supported(self):
        destination = self.packages / "framework-mbed@6.61700.231105"
        self.framework.rename(destination)
        self.assertIn("OLIMEX_F103", self.reason(framework=destination))

    def test_missing_malformed_oversized_or_empty_metadata_keeps_probe(self):
        for filename in ("platformio/variants_remap.json", "targets/targets.json"):
            path = self.framework / filename
            original = path.read_bytes()
            for payload in (b"[]", b"{", b"x" * 1048577):
                path.write_bytes(payload)
                self.assertIsNone(self.reason())
            path.unlink()
            self.assertIsNone(self.reason())
            path.write_bytes(original)
        self.write("targets/targets.json", {})
        self.assertIsNone(self.reason())
        self.write("targets/targets.json", {"UNKNOWN": "malformed target"})
        self.assertIsNone(self.reason())
        self.write("targets/targets.json", {"NUCLEO_F103RB": {}})
        self.write("platformio/variants_remap.json", {"custom": None})
        self.assertIsNone(self.reason())

    def test_external_framework_and_package_stores_keep_probe(self):
        external = self.root / "external/framework-mbed"
        external.parent.mkdir()
        self.framework.rename(external)
        self.assertIsNone(self.reason(framework=external))
        with patch.dict(os.environ, {"PLATFORMIO_PACKAGES_DIR": str(external.parent)}):
            self.assertIsNone(self.reason(framework=external))

    def test_framework_junction_escape_and_external_metadata_keep_probe(self):
        external = self.root / "external/framework-mbed"
        external.parent.mkdir()
        self.framework.rename(external)
        if sys.platform == "win32":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(self.framework), str(external)],
                                    capture_output=True, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stderr)
            remove = self.framework.rmdir
        else:
            self.framework.symlink_to(external, target_is_directory=True)
            remove = self.framework.unlink
        try:
            self.assertIsNone(self.reason())
        finally:
            self.assertEqual(self.framework.parent.resolve(), self.packages.resolve())
            remove()  # Remove only the fixture's link entry, preserving its target.
        external.rename(self.framework)
        metadata = self.framework / "platformio"
        external = self.root / "external-metadata"
        metadata.rename(external)
        if sys.platform == "win32":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(metadata), str(external)],
                                    capture_output=True, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stderr)
            remove = metadata.rmdir
        else:
            metadata.symlink_to(external, target_is_directory=True)
            remove = metadata.unlink
        try:
            self.assertIsNone(self.reason())
        finally:
            self.assertEqual(metadata.parent.resolve(), self.framework.resolve())
            remove()


if __name__ == "__main__":
    unittest.main(verbosity=2)
