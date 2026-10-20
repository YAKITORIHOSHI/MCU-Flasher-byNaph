#!/usr/bin/env python3
"""Offline Onehorse recipe regressions; all writes stay in owned fixtures."""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core import platformio_board_compat as compat
from direct import board_compile_campaign as campaign

# Exact v8.0.0 registry board bytes; this deliberately reproduces its missing
# boot/memory overrides. Espressif 3.3.12 boards.txt declares DOUT with boot=DIO.
BOARD_BYTES = b'{\r\n  "build": {\r\n    "arduino":{\r\n      "ldscript": "esp32_out.ld"\r\n    },\r\n    "core": "esp32",\r\n    "extra_flags": "-DARDUINO_ONEHORSE_ESP32_DEV",\r\n    "f_cpu": "240000000L",\r\n    "f_flash": "40000000L",\r\n    "flash_mode": "dout",\r\n    "mcu": "esp32",\r\n    "variant": "onehorse32dev"\r\n  },\r\n  "connectivity": [\r\n    "wifi",\r\n    "bluetooth",\r\n    "ethernet",\r\n    "can"\r\n  ],\r\n  "frameworks": [\r\n    "arduino",\r\n    "espidf"\r\n  ],\r\n  "name": "Onehorse ESP32 Dev Module",\r\n  "upload": {\r\n    "flash_size": "4MB",\r\n    "maximum_ram_size": 327680,\r\n    "maximum_size": 4194304,\r\n    "require_upload_port": true,\r\n    "speed": 460800\r\n  },\r\n  "url": "https://www.tindie.com/products/onehorse/esp32-development-board/",\r\n  "vendor": "Onehorse"\r\n}\r\n'
BOARD_SHA256 = "483741724c41624f1be320ef09ec3396fc0f34d7d0bc633b092e257cd621990b"
MINIMAIN_BYTES = b'{\r\n  "build": {\r\n    "arduino": {\r\n      "ldscript": "esp32s2_out.ld",\r\n      "partitions": "tinyuf2-partitions-4MB.csv"\r\n    },\r\n    "core": "esp32",\r\n    "extra_flags": [\r\n      "-DARDUINO_DEPARTMENT_OF_ALCHEMY_MINIMAIN_ESP32S2",\r\n      "-DARDUINO_RUNNING_CORE=1",\r\n      "-DARDUINO_EVENT_RUNNING_CORE=1",\r\n      "-DBOARD_HAS_PSRAM",\r\n      "-DARDUINO_USB_MODE=1",\r\n      "-DARDUINO_USB_CDC_ON_BOOT=1"\r\n    ],\r\n    "f_cpu": "240000000L",\r\n    "f_flash": "80000000L",\r\n    "flash_mode": "qio",\r\n    "hwids": [\r\n      [\r\n        "0x303A",\r\n        "0x80FF"\r\n      ]\r\n    ],\r\n    "mcu": "esp32s2",\r\n    "variant": "department_of_alchemy_minimain_esp32s2"\r\n  },\r\n  "connectivity": [\r\n    "wifi"\r\n  ],\r\n  "debug": {\r\n    "openocd_target": "esp32s2.cfg"\r\n  },\r\n  "frameworks": [\r\n    "arduino",\r\n    "espidf"\r\n  ],\r\n  "name": "Department of Alchemy MiniMain ESP32-S2",\r\n  "upload": {\r\n    "flash_size": "4MB",\r\n    "maximum_ram_size": 327680,\r\n    "maximum_size": 4194304,\r\n    "use_1200bps_touch": true,\r\n    "wait_for_upload_port": true,\r\n    "require_upload_port": true,\r\n    "speed": 460800\r\n  },\r\n  "url": "https://github.com/DepartmentOfAlchemy/minimain-esp32-s2",\r\n  "vendor": "Department of Alchemy"\r\n}\r\n'
OPTIONS = {"board_build.boot": "dio", "board_build.arduino.memory_type": "dio_qspi"}


class OnehorseChecks(unittest.TestCase):
    def setUp(self):
        base = ROOT / "temp/audit/onehorse-recipe-verifier"
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.core = self.root / "core"
        self.manifest = self.core / "platforms/espressif32/boards/onehorse32dev.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_bytes(BOARD_BYTES)
        self.board = json.loads(BOARD_BYTES)
        self.platform = {"name": "espressif32", "version": "8.0.0", "frameworks": {
            "arduino": {"package": "framework-arduinoespressif32", "script": "builder/frameworks/arduino.py"}},
            "packages": {name: {"owner": "platformio"} for name in (
                "framework-arduinoespressif32", "framework-arduinoespressif32-esp32-libs")}}
        self.framework = {"name": "framework-arduinoespressif32", "version": "4.30312.261016"}
        self.sdk = {"name": "framework-arduinoespressif32-esp32-libs", "version": "3.3.12"}
        self.write_metadata(self.manifest.parent.parent / "platform.json", self.platform)
        for metadata in (self.framework, self.sdk):
            self.write_metadata(self.core / "packages" / metadata["name"] / "package.json", metadata)
        self.info = {"platform": "espressif32", "board": "onehorse32dev", "framework": "arduino",
                     "pio_manifest": str(self.manifest), "pio_resolved": True}

    @staticmethod
    def write_metadata(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def options(self, **updates):
        evidence = {"board": self.board, "platform": self.platform, "framework_package": self.framework,
                    "sdk_package": self.sdk, "board_sha256": BOARD_SHA256}
        evidence.update(updates)
        return compat.onehorse_recipe_options("espressif32", "onehorse32dev", "arduino", **evidence)

    def test_known_recipe_selects_dio_sdk_without_changing_dout_or_variant(self):
        before = deepcopy(self.board)
        self.assertEqual(self.options(), OPTIONS)
        self.assertEqual(self.board, before)
        self.assertEqual(self.board["build"]["flash_mode"], "dout")
        self.assertEqual(self.board["build"]["variant"], "onehorse32dev")

    def test_unknown_identity_or_recipe_never_receives_overrides(self):
        evidence = {"board": self.board, "platform": self.platform, "framework_package": self.framework,
                    "sdk_package": self.sdk, "board_sha256": BOARD_SHA256}
        for identity in (("espressif32", "esp32dev", "arduino"), ("other", "onehorse32dev", "arduino"),
                         ("espressif32", "onehorse32dev", "espidf")):
            with self.subTest(identity=identity):
                self.assertEqual(compat.onehorse_recipe_options(*identity, **evidence), {})
        for updates in ({"board_sha256": "0" * 64}, {"platform": {**self.platform, "version": "8.1.0"}},
                        {"framework_package": {**self.framework, "version": "4.30313.0"}},
                        {"sdk_package": {**self.sdk, "version": "3.3.13"}}):
            with self.subTest(updates=updates):
                self.assertEqual(self.options(**updates), {})
        altered = deepcopy(self.platform)
        altered["packages"]["framework-arduinoespressif32"]["owner"] = "other-owner"
        self.assertEqual(self.options(platform=altered), {})

    def test_explicit_boot_or_memory_configuration_is_preserved(self):
        for key in ("boot", "memory_type", "arduino"):
            board = deepcopy(self.board)
            board["build"][key] = {"memory_type": "qio_qspi"} if key == "arduino" else "explicit"
            self.assertEqual(self.options(board=board), {})

    def test_worker_reader_requires_known_installed_bytes_and_never_writes_packages(self):
        before = {str(path): path.read_bytes() for path in self.core.glob("**/*") if path.is_file()}
        self.assertEqual(hashlib.sha256(BOARD_BYTES).hexdigest(), BOARD_SHA256)
        self.assertEqual(compat.board_compatibility_options(self.info, self.core), OPTIONS)
        self.assertEqual({str(path): path.read_bytes() for path in self.core.glob("**/*") if path.is_file()}, before)
        self.manifest.write_bytes(BOARD_BYTES + b" ")
        self.assertEqual(compat.board_compatibility_options(self.info, self.core), {})

    def test_unrelated_targets_do_not_read_metadata(self):
        with mock.patch.object(compat, "_read_metadata", side_effect=AssertionError("Unrelated target triggered I/O"), create=True):
            self.assertEqual(compat.board_compatibility_options({**self.info, "board": "esp32dev"}, self.core), {})

    def test_external_and_linked_manifest_paths_are_refused(self):
        outside = self.root / "external/onehorse32dev.json"
        outside.parent.mkdir()
        outside.write_bytes(BOARD_BYTES)
        self.assertEqual(compat.board_compatibility_options({**self.info, "pio_manifest": str(outside)}, self.core), {})
        if os.name == "posix":
            self.manifest.unlink()
            self.manifest.symlink_to(outside)
            self.assertEqual(compat.board_compatibility_options(self.info, self.core), {})
        self.assertEqual(outside.read_bytes(), BOARD_BYTES)

    def test_app_generates_same_board_recipe_and_keeps_unchanged_ini(self):
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and node.name == "_generate_platformio_ini")
        namespace = {"Path": Path, "os": os, "re": __import__("re"), "SCRIPT_DIR": ROOT,
                     "_project_root": self.root, "MAX_BAUD_RATE": 2000000, "DEFAULT_UPLOAD_SPEED": 460800,
                     "_get_safe_platformio_core_dir": lambda *args: self.core,
                     "write_generated_text": lambda path, text: path.write_text(text, encoding="utf-8")}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "actual-app-ini-generator", "exec"), namespace)
        api = SimpleNamespace(current_board="Onehorse", current_baud=115200, upload_speed="460800",
                              _resolve_board_info=lambda *args: dict(self.info), _scan_includes_for_libs=lambda: [], emit=lambda *args: None)
        target = self.root / "generated"
        target.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(self.root / "home")}), mock.patch.object(os.path, "expanduser", lambda value: str(self.root / "home") if value == "~" else value):
            namespace["_generate_platformio_ini"](api, target)
            ini = target / "platformio.ini"
            text, stamp = ini.read_text(), ini.stat().st_mtime_ns
            self.assertIn("board = onehorse32dev\n", text)
            self.assertIn("board_build.boot = dio\n", text)
            self.assertIn("board_build.arduino.memory_type = dio_qspi\n", text)
            self.assertNotIn("board_build.flash_mode", text)
            namespace["_generate_platformio_ini"](api, target)
            self.assertEqual(ini.stat().st_mtime_ns, stamp)
        self.assertEqual(self.manifest.read_bytes(), BOARD_BYTES)

    def test_campaign_emits_the_shared_options_without_changing_target(self):
        generator = getattr(campaign, "project_configuration", None)
        self.assertIsNotNone(generator, "Campaign must use the shared exact-board compatibility decision")
        target = {"platform_spec": "platformio/espressif32@8.0.0", "board": "onehorse32dev", "framework": "arduino"}
        identity = {"name": "espressif32", "manifest_path": str(self.manifest)}
        text, options = generator(target, identity, self.core)
        self.assertEqual(options, OPTIONS)
        self.assertIn("board = onehorse32dev\n", text)
        self.assertIn("board_build.boot = dio\n", text)
        self.assertIn("board_build.arduino.memory_type = dio_qspi\n", text)
        self.assertNotIn("board_build.flash_mode", text)
        self.assertEqual(target["board"], "onehorse32dev")



class MiniMainChecks(unittest.TestCase):
    def setUp(self):
        OnehorseChecks.setUp(self)
        self.platform["packages"].pop("framework-arduinoespressif32-esp32-libs")
        self.platform["packages"]["framework-arduinoespressif32-esp32s2-libs"] = {"owner":"platformio"}
        self.sdk = {"name":"framework-arduinoespressif32-esp32s2-libs","version":"3.3.12"}
        self.write_metadata(self.manifest.parent.parent / "platform.json", self.platform)
        self.write_metadata(self.core / "packages" / self.sdk["name"] / "package.json", self.sdk)
    write_metadata = staticmethod(OnehorseChecks.write_metadata)

    def test_minimain_only_changes_unsupported_usb_mode(self):
        board = json.loads(MINIMAIN_BYTES)
        options = compat.minimain_recipe_options("espressif32", "minimain_esp32s2", "arduino",
                    board=board, platform=self.platform, framework_package=self.framework,
                    sdk_package=self.sdk, board_sha256=hashlib.sha256(MINIMAIN_BYTES).hexdigest())
        flags = options["board_build.extra_flags"].split()
        before = board["build"]["extra_flags"]
        self.assertEqual(flags, ["-DARDUINO_USB_MODE=0" if value == "-DARDUINO_USB_MODE=1" else value for value in before])
        self.assertIn("-DARDUINO_USB_CDC_ON_BOOT=1", flags)
        self.assertEqual(board["build"]["mcu"], "esp32s2")
        self.assertEqual(board["build"]["variant"], "department_of_alchemy_minimain_esp32s2")

    def test_minimain_unknown_or_already_fixed_recipe_is_preserved(self):
        board = json.loads(MINIMAIN_BYTES)
        evidence = dict(board=board, platform=self.platform, framework_package=self.framework,
                        sdk_package=self.sdk, board_sha256=hashlib.sha256(MINIMAIN_BYTES).hexdigest())
        for updates in ({"sdk_package": {**self.sdk,"name":"framework-arduinoespressif32-esp32-libs"}}, {"board_sha256":"0"*64}, {"sdk_package":{**self.sdk,"version":"3.3.13"}},
                        {"platform":{**self.platform,"frameworks":[]}}):
            with self.subTest(updates=updates):
                self.assertEqual(compat.minimain_recipe_options("espressif32", "minimain_esp32s2", "arduino", **{**evidence, **updates}), {})
        board["build"]["extra_flags"][-2] = "-DARDUINO_USB_MODE=0"
        self.assertEqual(compat.minimain_recipe_options("espressif32", "minimain_esp32s2", "arduino", **evidence), {})

    def test_minimain_worker_and_campaign_share_exact_options(self):
        manifest = self.manifest.with_name("minimain_esp32s2.json")
        manifest.write_bytes(MINIMAIN_BYTES)
        info = {**self.info, "board":"minimain_esp32s2", "pio_manifest":str(manifest)}
        before = manifest.read_bytes()
        options = compat.board_compatibility_options(info,self.core)
        self.assertIn("-DARDUINO_USB_MODE=0", options["board_build.extra_flags"])
        target = {"platform_spec":"platformio/espressif32@8.0.0", "board":"minimain_esp32s2", "framework":"arduino"}
        text, actual = campaign.project_configuration(target, {"name":"espressif32","manifest_path":str(manifest)},self.core)
        self.assertEqual(actual,options)
        self.assertIn("board = minimain_esp32s2\n",text)
        self.assertEqual(manifest.read_bytes(),before)

if __name__ == "__main__":
    unittest.main(verbosity=2)
