#!/usr/bin/env python3
"""Verify reviewed Zephyr migrations using isolated board metadata fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import zephyr_compat as compat
from platformio.platform.board import PlatformBoardConfig


class ZephyrCompatibilityChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="zephyr-compat-", dir=audit))
        self.core = self.root / ".platformio-mcu-gui"
        self.packages = self.core / "packages"
        self.framework = self.packages / "framework-zephyr"
        self.framework.mkdir(parents=True)
        self.write("package.json", json.dumps({"name": "framework-zephyr", "version": "3.40402.0"}))
        self.write("VERSION", "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 2\n"
                   "VERSION_TWEAK = 0\nEXTRAVERSION =\n")
        for filename, identifier in compat._IDENTIFIERS.items():
            self.write(filename, "identifier: " + identifier + "\n")
        self.write("boards/we/oceanus1ev/board.yml",
                   "board:\n  name: we_oceanus1ev\n  full_name: Oceanus-I EV\n"
                   "  socs:\n    - name: stm32wle5xx\n  revision:\n    default: '1.1.0'\n")
        self.environment = {"PLATFORMIO_CORE_DIR": str(self.core),
                            "PLATFORMIO_PACKAGES_DIR": str(self.packages),
                            "ZEPHYR_BASE": str(self.framework), "KEEP": "unchanged"}
        environment = patch.dict(os.environ, self.environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(self.cleanup_fixture)

    def cleanup_fixture(self):
        self.assertEqual(self.root.parent.resolve(), (ROOT / "temp/audit").resolve())
        self.assertEqual(self.root.resolve().parent, (ROOT / "temp/audit").resolve())
        shutil.rmtree(self.root)

    def write(self, filename, text):
        path = self.framework / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def board(self, ident, target=None, mcu=None):
        expected_target, expected_mcu = compat._UNAVAILABLE.get(ident, (ident, "fixture"))
        manifest = {"name": ident, "url": "https://example.invalid", "vendor": "fixture",
                    "frameworks": ["arduino", "zephyr"],
                    "build": {"mcu": mcu if mcu is not None else expected_mcu}}
        if target is not None or expected_target != ident:
            manifest["build"]["zephyr"] = {"variant": target if target is not None else expected_target}
        path = self.root / (ident + ".json")
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return PlatformBoardConfig(str(path))

    def reason(self, ident, **kwargs):
        return compat.unavailable_framework_reason(
            kwargs.pop("platform", "ststm32"), kwargs.pop("version", "20.0.0"),
            self.board(ident, **kwargs), self.framework)

    def test_alias_file_maps_exact_hardware_and_cmake_input_is_unchanged(self):
        expected = {"stm32h747i_disco_m7": "stm32h747i_disco/stm32h747xx/m7",
                    "nucleo_h745zi_q_m7": "nucleo_h745zi_q/stm32h745xx/m7",
                    "we_oceanus1_ev": "we_oceanus1ev/stm32wle5xx"}
        rows = dict(re.findall(r"set\((\w+)/_DEPRECATED\s+([^\s)]+)\)",
                               compat.ALIASES.read_text(encoding="utf-8")))
        self.assertEqual(rows, expected)
        command = ["cmake", "-DBOARD=stm32h747i_disco_m7", "-S", "short/source"]
        original_environment = dict(self.environment)
        result = compat.cmake_environment(command, self.environment)
        self.assertEqual(command, ["cmake", "-DBOARD=stm32h747i_disco_m7", "-S", "short/source"])
        self.assertEqual(self.environment, original_environment)
        self.assertEqual({key: value for key, value in result.items() if key != "ZEPHYR_BOARD_ALIASES"},
                         original_environment)
        self.assertEqual(result["ZEPHYR_BOARD_ALIASES"], compat.ALIASES.resolve().as_posix())

    def test_real_cmake_preserves_complete_qualifiers_in_deprecated_target_flow(self):
        cmake = (ROOT / "src/.platformio-mcu-gui/packages/tool-cmake/bin/cmake.exe"
                 if sys.platform == "win32" else Path(shutil.which("cmake") or ""))
        if not cmake.is_file():
            self.skipTest("Native CMake executable is unavailable")
        # This is the Zephyr 4.4.2 board parser and both migration branches,
        # reduced to their variable operations. No configure/build occurs.
        script = self.root / "board-migration.cmake"
        script.write_text(r'''
function(parse_board_components board_in name_out revision_out qualifiers_out)
  if(NOT "${${board_in}}" MATCHES "^([^@/]+)(@[^@/]+)?(/([^@]+))?$")
    message(FATAL_ERROR "Invalid board format")
  endif()
  string(REPLACE "@" "" board_revision "${CMAKE_MATCH_2}")
  set(${name_out} ${CMAKE_MATCH_1} PARENT_SCOPE)
  set(${revision_out} ${board_revision} PARENT_SCOPE)
  set(${qualifiers_out} ${CMAKE_MATCH_4} PARENT_SCOPE)
endfunction()

# Reproduce why the upstream alias branch is unsuitable for this migration.
set(BOARD stm32h747i_disco_m7)
set(stm32h747i_disco_m7_BOARD_ALIAS stm32h747i_disco/stm32h747xx/m7)
parse_board_components(BOARD BOARD BOARD_REVISION BOARD_QUALIFIERS)
if(${BOARD}_BOARD_ALIAS)
  parse_board_components(${BOARD}_BOARD_ALIAS BOARD BOARD_ALIAS_REVISION BOARD_ALIAS_QUALIFIERS)
  set(BOARD_QUALIFIERS ${BOARD_ALIAS_QUALIFIERS}/${BOARD_QUALIFIERS})
endif()
if(NOT "${BOARD}/${BOARD_QUALIFIERS}" STREQUAL "stm32h747i_disco/stm32h747xx/m7/")
  message(FATAL_ERROR "Upstream alias regression fixture did not reproduce")
endif()
unset(stm32h747i_disco_m7_BOARD_ALIAS)

# The application file is included at Zephyr's existing alias include point.
include("''' + compat.ALIASES.resolve().as_posix() + r'''")
foreach(legacy IN ITEMS stm32h747i_disco_m7 nucleo_h745zi_q_m7 we_oceanus1_ev)
  set(BOARD ${legacy})
  parse_board_components(BOARD BOARD BOARD_REVISION BOARD_QUALIFIERS)
  if(${BOARD}_BOARD_ALIAS)
    message(FATAL_ERROR "Application must not enter upstream alias qualifier join")
  endif()
  if(${BOARD}/${BOARD_QUALIFIERS}_DEPRECATED)
    parse_board_components(${BOARD}/${BOARD_QUALIFIERS}_DEPRECATED
                          BOARD BOARD_DEPRECATED_REVISION BOARD_QUALIFIERS)
  endif()
  if(legacy STREQUAL "stm32h747i_disco_m7")
    set(expected stm32h747i_disco/stm32h747xx/m7)
  elseif(legacy STREQUAL "nucleo_h745zi_q_m7")
    set(expected nucleo_h745zi_q/stm32h745xx/m7)
  else()
    set(expected we_oceanus1ev/stm32wle5xx)
  endif()
  if(NOT "${BOARD}/${BOARD_QUALIFIERS}" STREQUAL "${expected}")
    message(FATAL_ERROR "Incorrect migrated target: ${BOARD}/${BOARD_QUALIFIERS}")
  endif()
  message(STATUS "Verified exact target: ${BOARD}/${BOARD_QUALIFIERS}")
endforeach()
''', encoding="utf-8")
        result = subprocess.run([str(cmake), "-P", str(script)], cwd=self.root,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("Verified exact target:"), 3)

    def test_cmake_sequence_serialized_and_host_native_commands(self):
        for command in (["/opt/tools/cmake", "-DVALUE=x"], [r"C:\tools\cmake.exe", "--version"],
                        '"C:\\tools path\\cmake.exe" -DBOARD=fixture', "cmake -DBOARD=fixture"):
            with self.subTest(command=command):
                self.assertIn("ZEPHYR_BOARD_ALIASES", compat.cmake_environment(command, self.environment))
        for command in (None, [], [object()], "", ["python", "cmake"],
                        ["powershell", "cmake"], "echo cmake", ["not-cmake.exe"]):
            with self.subTest(command=command):
                self.assertIs(compat.cmake_environment(command, self.environment), self.environment)

    def test_explicit_aliases_and_board_roots_are_preserved_including_empty(self):
        for key in ("ZEPHYR_BOARD_ALIASES", "BOARD_ROOT"):
            for value in ("", "custom/source"):
                environment = dict(self.environment, **{key: value})
                self.assertIs(compat.cmake_environment(["cmake"], environment), environment)
                with patch.dict(os.environ, {key: value}):
                    self.assertIsNone(self.reason("ebyte_e77_dev"))

    def test_unreviewed_package_metadata_and_release_keep_native_behavior(self):
        for metadata in ({"name": "different", "version": "3.40402.0"},
                         {"name": "framework-zephyr", "version": "3.40403.0"}, {}):
            self.write("package.json", json.dumps(metadata))
            self.assertIs(compat.cmake_environment(["cmake"], self.environment), self.environment)
            self.assertIsNone(self.reason("ebyte_e77_dev"))
        self.write("package.json", json.dumps({"name": "framework-zephyr", "version": "3.40402.0"}))
        for version in ("VERSION_MAJOR=4\nVERSION_MINOR=4\nPATCHLEVEL=3\n",
                        "VERSION_MAJOR=4\nVERSION_MINOR=4\nPATCHLEVEL=2\nEXTRAVERSION=rc1\n",
                        "VERSION_MAJOR=4\nVERSION_MINOR=4\nPATCHLEVEL=2\nVERSION_TWEAK=1\n"):
            self.write("VERSION", version)
            self.assertIs(compat.cmake_environment(["cmake"], self.environment), self.environment)
            self.assertIsNone(self.reason("ebyte_e77_dev"))

    def test_every_identifier_and_oceanus_soc_guard_is_required(self):
        for filename, expected in compat._IDENTIFIERS.items():
            self.write(filename, "identifier: different/target\n")
            self.assertIs(compat.cmake_environment(["cmake"], self.environment), self.environment)
            self.write(filename, "identifier: " + expected + "\n")
        self.write("boards/we/oceanus1ev/board.yml",
                   "board:\n  name: we_oceanus1ev\n  full_name: Oceanus-I EV\n"
                   "  socs:\n    - name: stm32wl55xx\n  revision:\n    default: '1.1.0'\n")
        self.assertIs(compat.cmake_environment(["cmake"], self.environment), self.environment)

    def test_external_framework_store_and_missing_environment_stay_native(self):
        for environment in ({key: value for key, value in self.environment.items() if key != "PLATFORMIO_CORE_DIR"},
                            {key: value for key, value in self.environment.items() if key != "PLATFORMIO_PACKAGES_DIR"},
                            dict(self.environment, PLATFORMIO_CORE_DIR=str(self.root / "other")),
                            dict(self.environment, PLATFORMIO_PACKAGES_DIR=str(self.root)),
                            dict(self.environment, ZEPHYR_BASE=str(self.root))):
            self.assertIs(compat.cmake_environment(["cmake"], environment), environment)

    def test_versioned_reviewed_package_folder_is_supported(self):
        destination = self.packages / "framework-zephyr@3.40402.0"
        self.framework.rename(destination)
        environment = dict(self.environment, ZEPHYR_BASE=str(destination))
        self.assertIn("ZEPHYR_BOARD_ALIASES", compat.cmake_environment(["cmake"], environment))

    def test_three_reviewed_unavailable_rows_have_specific_reason(self):
        for ident, (target, _) in compat._UNAVAILABLE.items():
            with self.subTest(board=ident):
                reason = self.reason(ident)
                self.assertIn("Zephyr 4.4.2 has no board definition", reason)
                self.assertIn(ident, reason)
                self.assertIn(target, reason)
        self.assertIsNone(self.reason("disco_h747xi"))
        self.assertIsNone(self.reason("we_oceanus1_ev"))

    def test_unknown_platform_version_board_mcu_or_variant_keeps_native_probes(self):
        for ident in compat._UNAVAILABLE:
            for changes in ({"version": "20.0.1"}, {"platform": "atmelsam"},
                            {"mcu": "another-device"}, {"target": "custom_target"}):
                self.assertIsNone(self.reason(ident, **changes))

    def test_present_board_definition_prevents_exclusion_any_vendor_folder(self):
        for ident, (target, _) in compat._UNAVAILABLE.items():
            self.write("boards/custom/renamed-folder/board.yml", "board:\n  name: " + target + "\n")
            self.assertIsNone(self.reason(ident))
        self.write("boards/custom/renamed-folder/board.yml", "boards:\n  - name: custom_target\n"
                   "  - name: ebyte_e77_dev\n")
        self.assertIsNone(self.reason("ebyte_e77_dev"))

    def test_invalid_or_missing_board_catalog_prevents_exclusion(self):
        self.write("boards/we/oceanus1ev/board.yml", "invalid: [\n")
        self.assertIsNone(self.reason("ebyte_e77_dev"))
        shutil.rmtree(self.framework / "boards")
        self.assertIsNone(self.reason("ebyte_e77_dev"))

    @unittest.skipUnless(sys.platform == "win32", "Windows junction containment")
    def test_packages_junction_outside_core_is_unadapted(self):
        external = self.root / "external-packages"
        self.packages.rename(external)
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.packages), str(external)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            self.assertIs(compat.cmake_environment(["cmake"], self.environment), self.environment)
            self.assertIsNone(self.reason("ebyte_e77_dev"))
        finally:
            self.assertEqual(self.packages.parent.resolve(), self.core.resolve())
            os.rmdir(self.packages)
        self.assertTrue(external.is_dir())

    @unittest.skipUnless(sys.platform == "win32", "Windows junction containment")
    def test_framework_junction_outside_configured_store_is_unadapted(self):
        external = self.root / "external-framework"
        self.framework.rename(external)
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.framework), str(external)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            self.assertIs(compat.cmake_environment(["cmake"], self.environment), self.environment)
            self.assertIsNone(self.reason("ebyte_e77_dev"))
        finally:
            self.assertEqual(self.framework.parent.resolve(), self.packages.resolve())
            os.rmdir(self.framework)
        self.assertTrue(external.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
