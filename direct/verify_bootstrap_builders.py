#!/usr/bin/env python3
"""Verify conservative Arduino builder planning with isolated files/factories.

Never executes installed builders, installers, hardware or live persistence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.modules import offline_bootstrap as setup


class Board:
    def __init__(self, name, *, mcu=None, core="arduino", frameworks=("arduino",),
                 package="framework-arduino", version="1.0.0", owner="vendor"):
        self.id = name
        self.values = {"frameworks": list(frameworks), "build.mcu": mcu or name,
                       "build.core": core}
        self.package, self.version, self.owner = package, version, owner

    def get(self, name, default=None):
        return self.values.get(name, default)


class BuilderChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/bootstrap-builders"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.name, self.version = "atmelavr", "5.3.0"
        self.script = setup._AVR_ARDUINO_SCRIPT
        self.reviewed = copy.deepcopy(setup._REVIEWED_AVR_BUILDERS)
        self.signatures = {}
        for identity, reviewed in setup._REVIEWED_AVR_BUILDERS.items():
            signatures = {}
            for relative in reviewed:
                # Fixture code is hashed and inspected, never executed.
                text = f"# Isolated builder source: {relative}\n"
                path = self.directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode("utf-8"))
                signatures[relative] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self.signatures[identity] = signatures
        self.constants = patch.object(setup, "_REVIEWED_AVR_BUILDERS", self.signatures)
        self.constants.start()
        self.addCleanup(self.constants.stop)

    def instance(self, boards):
        checks = self
        class NativePlatform:
            name, version = checks.name, checks.version

            def __init__(self):
                self.frameworks = {
                    "arduino": {"script": checks.script},
                    "native_sdk": {"script": "builder/frameworks/native_sdk.py"},
                }
                self.packages = {
                    "toolchain": {"owner": "vendor", "version": "2.0.0"},
                    "uploader": {"owner": "vendor", "version": "3.0.0", "optional": True},
                    "debugger": {"owner": "vendor", "version": "4.0.0", "optional": True},
                }

            def get_dir(self):
                return checks.directory

            def configure_default_packages(self, options, targets):
                board = boards[options["board"]]
                self.packages[board.package] = {
                    "owner": board.owner, "version": board.version,
                }
        return NativePlatform()

    def plan(self, boards):
        from platformio.platform.factory import PlatformFactory
        boards = {board.id: board for board in boards}
        with patch.object(PlatformFactory, "new", side_effect=lambda *_: self.instance(boards)), \
                patch.object(setup, "_reviewed_avr_builder", wraps=setup._reviewed_avr_builder) as validation:
            specs, probes = setup.package_plan(object(), boards)
        validation.assert_called_once()
        return specs, probes

    def test_reviewed_avr_versions_share_host_preparation_across_mcus(self):
        for self.name, self.version in (("atmelavr", "5.3.0"), ("atmelmegaavr", "1.10.0")):
            with self.subTest(platform=self.name):
                specs, probes = self.plan([Board("one"), Board("two")])
                self.assertEqual(probes, [("one", "arduino")])
                self.assertEqual({spec.name for spec in specs},
                                 {"toolchain", "uploader", "debugger", "framework-arduino"})

    def test_exact_core_and_framework_still_require_separate_probes(self):
        _, probes = self.plan([Board("one", core="MiniCore"), Board("two", core="minicore")])
        self.assertEqual(len(probes), 2, "Core names must remain case-sensitive")
        _, probes = self.plan([Board("one", frameworks=("arduino", "native_sdk")),
                               Board("two", frameworks=("arduino", "native_sdk"))])
        self.assertEqual(set(probes), {("one", "arduino"), ("one", "native_sdk"),
                                       ("two", "native_sdk")})

    def test_required_package_owner_name_and_version_remain_exact(self):
        variations = ({"version": "2.0.0"}, {"owner": "different-vendor"},
                      {"package": "framework-custom"})
        for variation in variations:
            with self.subTest(variation=variation):
                specs, probes = self.plan([Board("one"), Board("two", **variation)])
                self.assertEqual(len(probes), 2)
                framework_specs = [spec for spec in specs if spec.name.startswith("framework")]
                self.assertEqual(len(framework_specs), 2)

    def test_unknown_platform_or_revision_preserves_mcu_probes(self):
        for self.name, self.version in (("customavr", "5.3.0"), ("atmelavr", "5.4.0"),
                                       ("atmelmegaavr", "1.11.0")):
            with self.subTest(platform=self.name, version=self.version):
                _, probes = self.plan([Board("one"), Board("two")])
                self.assertEqual(len(probes), 2)

    def test_noncanonical_or_modified_builder_preserves_mcu_probes(self):
        self.script = "builder/frameworks/custom_arduino.py"
        _, probes = self.plan([Board("one"), Board("two")])
        self.assertEqual(len(probes), 2)
        self.script = setup._AVR_ARDUINO_SCRIPT
        for relative in self.signatures[(self.name, self.version)]:
            with self.subTest(script=relative):
                path = self.directory / relative
                original = path.read_bytes()
                path.write_bytes(original + b"# Custom host dependency\n")
                _, probes = self.plan([Board("one"), Board("two")])
                self.assertEqual(len(probes), 2)
                path.write_bytes(original)

    def test_megaavr_helper_is_part_of_source_validation(self):
        self.name, self.version = "atmelmegaavr", "1.10.0"
        path = self.directory / "builder/frameworks/_bare.py"
        path.write_bytes(path.read_bytes() + b"# Locally changed helper\n")
        _, probes = self.plan([Board("one"), Board("two")])
        self.assertEqual(len(probes), 2)

    def test_source_validation_accepts_line_endings_and_bounds_missing_or_large_files(self):
        instance = self.instance({})
        self.assertTrue(setup._reviewed_avr_builder(instance))
        for relative in self.signatures[(self.name, self.version)]:
            path = self.directory / relative
            path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertTrue(setup._reviewed_avr_builder(instance), "LF/CRLF packs should match")
        path = self.directory / "platform.py"
        path.write_bytes(b"x" * 65537)
        self.assertFalse(setup._reviewed_avr_builder(instance))
        path.unlink()
        self.assertFalse(setup._reviewed_avr_builder(instance))
        self.assertFalse(setup._reviewed_avr_builder(object()))

    def test_reviewed_identity_scope_and_dependency_script_coverage(self):
        with patch.object(setup, "_REVIEWED_AVR_BUILDERS", self.reviewed):
            signatures = setup._REVIEWED_AVR_BUILDERS
            self.assertEqual(set(signatures), {("atmelavr", "5.3.0"), ("atmelmegaavr", "1.10.0")})
            for entries in signatures.values():
                self.assertTrue({"platform.py", "builder/main.py", setup._AVR_ARDUINO_SCRIPT} <= set(entries))
                self.assertTrue(all(len(value) == 64 for value in entries.values()))
            self.assertIn("builder/frameworks/_bare.py", signatures[("atmelmegaavr", "1.10.0")])


class Stm32BuilderChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/bootstrap-builders"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.platform_dir = self.directory / "platform"
        self.name, self.version = "ststm32", "20.0.0"
        self.script = setup._STM32_ARDUINO_SCRIPT
        self.original_platforms = copy.deepcopy(setup._REVIEWED_STM32_BUILDERS)
        self.original_packages = copy.deepcopy(setup._REVIEWED_STM32_ARDUINO_PACKAGES)
        platforms = {}
        packages = {}
        self.package_dirs = {}
        for identity, entries in self.original_platforms.items():
            platforms[identity] = self.write_sources(self.platform_dir, entries)
        for identity, entries in self.original_packages.items():
            directory = self.directory / identity[0]
            packages[identity] = self.write_sources(directory, entries)
            (directory / "package.json").write_text(json.dumps({"name": identity[0], "version": identity[1]}))
            self.package_dirs[identity[0]] = directory
        for name, value in (("_REVIEWED_STM32_BUILDERS", platforms),
                            ("_REVIEWED_STM32_ARDUINO_PACKAGES", packages)):
            mock = patch.object(setup, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def write_sources(self, directory, entries):
        signatures = {}
        for relative in entries:
            text = f"# Isolated reviewed source: {relative}\n"
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            signatures[relative] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return signatures

    def instance(self, boards):
        checks = self
        class NativePlatform:
            name, version = checks.name, checks.version

            def __init__(self):
                self.frameworks = {
                    "arduino": {"script": checks.script, "package": "framework-arduinoststm32"},
                }
                self.packages = {
                    "toolchain": {"owner": "vendor", "version": "2.0.0"},
                    "uploader": {"owner": "vendor", "version": "3.0.0", "optional": True},
                }

            def get_dir(self):
                return checks.platform_dir

            def get_boards(self):
                return boards

            def get_package_dir(self, package):
                return checks.package_dirs.get(package)

            def configure_default_packages(self, options, targets):
                board = boards[options["board"]]
                core = board.get("build.core")
                package = {"maple": "framework-arduinoststm32-maple",
                           "stm32l0": "framework-arduinoststm32l0"}.get(core, "framework-arduinoststm32")
                if board.package != "framework-arduino":
                    package = board.package
                self.frameworks["arduino"] = {"script": getattr(board, "script", checks.script), "package": package}
                self.packages[package] = {"owner": "platformio" if board.owner == "vendor" else board.owner,
                                          "version": "1.0.0" if board.version == "1.0.0" else board.version}
        return NativePlatform()

    def plan(self, rows):
        from platformio.platform.factory import PlatformFactory
        boards = {board.id: board for board in rows}
        with patch.object(PlatformFactory, "new", side_effect=lambda *_: self.instance(boards)), \
                patch.object(setup, "_reviewed_stm32_builder", wraps=setup._reviewed_stm32_builder) as validation:
            result = setup.package_plan(object(), boards)
        validation.assert_called_once()
        return result

    def pair(self, **kwargs):
        return [Board("one", core="stm32", **kwargs), Board("two", core="stm32", **kwargs)]

    def test_exact_installed_arduino_dispatch_can_share_across_mcus(self):
        specs, probes = self.plan(self.pair())
        self.assertEqual(probes, [("one", "arduino")])
        self.assertEqual({spec.name for spec in specs},
                         {"toolchain", "uploader", "framework-arduinoststm32"})

    def test_core_packages_and_maple_dispatch_remain_distinct(self):
        rows = [Board("one", mcu="stm32f103c8", core="maple"),
                Board("two", mcu="stm32f107vc", core="maple"),
                Board("three", mcu="stm32f407vg", core="maple"),
                Board("four", mcu="stm32f429zi", core="maple"),
                Board("five", core="stm32l0"), Board("six", core="stm32"),
                Board("seven", core="STM32")]
        _, probes = self.plan(rows)
        self.assertEqual(probes, [("one", "arduino"), ("three", "arduino"),
                                  ("five", "arduino"), ("six", "arduino"), ("seven", "arduino")])

    def test_required_owner_name_and_constraints_are_preserved(self):
        for variation in ({"version": "2.0.0"}, {"owner": "different-vendor"},
                          {"package": "framework-custom"}):
            with self.subTest(variation=variation):
                _, probes = self.plan([Board("one", core="stm32"), Board("two", core="stm32", **variation)])
                self.assertEqual(len(probes), 2)

    def test_other_frameworks_and_special_arduino_dispatches_remain_mcu_specific(self):
        frameworks = ("arduino", "cmsis", "stm32cube", "spl", "libopencm3", "zephyr", "mbed")
        _, probes = self.plan(self.pair(frameworks=frameworks))
        self.assertEqual(len(probes), 13)
        for framework in frameworks[1:]:
            self.assertEqual(sum(item[1] == framework for item in probes), 2)
        for script in ("builder/frameworks/arduino/mbed-core/arduino-core-mbed.py",
                       "builder/frameworks/arduino/mxchip.py", "custom/arduino.py"):
            with self.subTest(script=script):
                rows = self.pair()
                for row in rows:
                    row.script = script
                _, probes = self.plan(rows)
                self.assertEqual(len(probes), 2)

    def test_unknown_platform_revision_or_modified_platform_sources_preserve_mcus(self):
        for self.name, self.version in (("custom-stm32", "20.0.0"), ("ststm32", "20.1.0")):
            _, probes = self.plan(self.pair())
            self.assertEqual(len(probes), 2)
        self.name, self.version = "ststm32", "20.0.0"
        for relative in setup._REVIEWED_STM32_BUILDERS[(self.name, self.version)]:
            path = self.platform_dir / relative
            original = path.read_bytes()
            path.write_bytes(original + b"# Unreviewed change\n")
            _, probes = self.plan(self.pair())
            self.assertEqual(len(probes), 2)
            path.write_bytes(original)

    def test_missing_modified_or_large_installed_builder_preserves_mcus(self):
        path = self.package_dirs["framework-arduinoststm32"] / "tools/platformio/platformio-build.py"
        original = path.read_bytes()
        for content in (None, original + b"# Unreviewed dependency\n", b"x" * 65537):
            with self.subTest(size=None if content is None else len(content)):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                _, probes = self.plan(self.pair())
                self.assertEqual(len(probes), 2)
                path.write_bytes(original)
        _, probes = self.plan([Board("one", mcu="stm32f303vc", core="maple"),
                               Board("two", mcu="stm32f373vc", core="maple")])
        self.assertEqual(len(probes), 2, "Unreviewed Maple dispatch families must not share")

    def test_unknown_missing_or_invalid_installed_package_metadata_preserves_mcus(self):
        path = self.package_dirs["framework-arduinoststm32"] / "package.json"
        original = path.read_bytes()
        variants = (None, b"not json", b"[]", b"x" * 65537,
                    json.dumps({"name": "framework-custom", "version": "4.30000.0"}).encode(),
                    json.dumps({"name": "framework-arduinoststm32", "version": "4.30000.1"}).encode())
        for content in variants:
            with self.subTest(content=None if content is None else content[:60]):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                _, probes = self.plan(self.pair())
                self.assertEqual(len(probes), 2)
                path.write_bytes(original)

    def test_source_normalization_and_validation_cache_are_bounded_per_plan(self):
        for directory, entries in [(self.platform_dir, setup._REVIEWED_STM32_BUILDERS[(self.name, self.version)])] + [
                (self.package_dirs[name], entries) for (name, _version), entries in setup._REVIEWED_STM32_ARDUINO_PACKAGES.items()]:
            for relative in entries:
                path = directory / relative
                path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
        with patch.object(setup, "_reviewed_source_files", wraps=setup._reviewed_source_files) as reads:
            _, probes = self.plan([Board(str(number), core="stm32") for number in range(50)])
        self.assertEqual(len(probes), 1)
        self.assertEqual(reads.call_count, 2, "Platform and installed dispatch are each validated once per plan")

    def test_post_install_replan_shares_only_after_sources_exist_and_preserves_specs(self):
        from platformio.platform.factory import PlatformFactory
        path = self.package_dirs["framework-arduinoststm32"] / "tools/platformio/platformio-build.py"
        original = path.read_bytes()
        path.unlink()
        rows = self.pair()
        specs, probes = self.plan(rows)
        self.assertEqual(len(probes), 2)
        path.write_bytes(original)
        boards = {row.id: row for row in rows}
        log = []
        with patch.object(PlatformFactory, "new", side_effect=lambda *_: self.instance(boards)):
            installed = setup._installed_builder_probes(object(), self.instance(boards), specs, probes, log.append)
        self.assertEqual(installed, [("one", "arduino")])
        self.assertIn("2 to 1", log[0])
        from platformio.package.meta import PackageSpec
        changed = specs + [PackageSpec(owner="vendor", name="new-dependency", requirements="1.0.0")]
        with patch.object(setup, "package_plan", return_value=(changed, installed)):
            with self.assertRaisesRegex(RuntimeError, "specifications changed"):
                setup._installed_builder_probes(object(), self.instance(boards), specs, probes, log.append)
        self.name = "unreviewed-platform"
        with patch.object(setup, "package_plan", side_effect=AssertionError("No unnecessary replanning")):
            self.assertIs(setup._installed_builder_probes(object(), self.instance(boards), specs, probes, log.append), probes)

    def test_unavailable_declaration_keeps_valid_same_mcu_probe_and_other_frameworks(self):
        from platformio.platform.factory import PlatformFactory
        rows = [Board("missing", mcu="stm32f405rg", core="stm32", frameworks=("arduino", "zephyr")),
                Board("valid", mcu="stm32f405rg", core="stm32", frameworks=("arduino", "zephyr"))]
        boards = {row.id: row for row in rows}
        unavailable = {"missing": {"zephyr": "No matching installed board definition"}}
        with patch.object(PlatformFactory, "new", side_effect=lambda *_: self.instance(boards)):
            specs, probes = setup.package_plan(object(), boards)
            filtered = setup._installed_builder_probes(object(), self.instance(boards), specs, probes,
                                                       lambda *_: None, unavailable=unavailable)
        self.assertIn(("missing", "arduino"), filtered)
        self.assertIn(("valid", "zephyr"), filtered)
        self.assertNotIn(("missing", "zephyr"), filtered)
        self.assertEqual(boards["missing"].get("frameworks"), ["arduino", "zephyr"])

    def test_changed_platform_sources_cannot_authorize_unavailable_exclusions(self):
        from src.modules import zephyr_compat
        platform = self.instance({})
        with patch.object(setup, "_reviewed_stm32_builder", return_value=False), \
                patch.object(zephyr_compat, "unavailable_framework_reason") as reason:
            self.assertEqual(setup._unavailable_frameworks(platform), {})
        reason.assert_not_called()

    def test_finite_reviewed_scope_includes_platform_wrapper_and_nested_sources(self):
        self.assertEqual(set(self.original_platforms), {("ststm32", "20.0.0")})
        self.assertEqual(set(self.original_packages), {
            ("framework-arduinoststm32", "4.30000.0"),
            ("framework-arduinoststm32-maple", "3.10000.201129"),
            ("framework-arduinoststm32l0", "2.10.220528"),
        })
        for entries in self.original_platforms.values():
            self.assertEqual(set(entries), {"platform.py", "builder/main.py", setup._STM32_ARDUINO_SCRIPT})
        self.assertEqual(set(self.original_packages[("framework-arduinoststm32-maple", "3.10000.201129")]),
                         {"tools/platformio-build-stm32f1.py", "tools/platformio-build-stm32f4.py"})
        self.assertTrue(all(len(value) == 64 for entries in list(self.original_platforms.values()) +
                            list(self.original_packages.values()) for value in entries.values()))


if __name__ == "__main__":
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (BuilderChecks, Stm32BuilderChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
