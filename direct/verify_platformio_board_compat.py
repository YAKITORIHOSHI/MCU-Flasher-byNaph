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


class UnoQChecks(unittest.TestCase):
    setUp = OnehorseChecks.setUp
    write_metadata = staticmethod(OnehorseChecks.write_metadata)
    METADATA = {'platform.json': b'{\n  "name": "arduinoq",\n  "title": "Arduino Q",\n  "description": "Arduino Q workflow for PlatformIO",\n  "engines": {\n    "platformio": "^6.1.0"\n  },\n  "repository": {\n    "type": "git",\n    "url": "https://github.com/lee-lab-skku/platform-arduinoq.git"\n  },\n  "version": "1.0.0-rc.4",\n  "frameworks": {\n    "arduino": {\n      "package": "framework-arduino-zephyr",\n      "script": "builder/frameworks/arduino-zephyr.py",\n      "title": "Arduino fork of Zephyr"\n    }\n  },\n  "authors": [\n    {\n      "name": "Taeha Shin",\n      "email": "mirinae3145@skku.edu",\n      "maintainer": true\n    }\n  ],\n  "license": "Apache-2.0",\n  "packages": {\n    "toolchain-gccarmzephyreabi": {\n      "type": "toolchain",\n      "version": "0.16.8-leelabskku"\n    },\n    "framework-arduino-zephyr": {\n      "type": "framework",\n      "version": "0.56.0-leelabskku+unoq"\n    },\n    "tool-zephyrsketch": {\n      "type": "tool",\n      "version": "0.4.0-leelabskku"\n    },\n    "tool-genrodatald": {\n      "type": "tool",\n      "version": "0.1.1-leelabskku"\n    },\n    "tool-zephyrchecksize": {\n      "type": "tool",\n      "version": "0.1.0-leelabskku"\n    }\n  }\n}\n', 'boards/uno_q.json': b'{\n  "build": {\n    "mcu": "stm32u585ai",\n    "f_cpu": "160000000L",\n    "machine_flags": [\n      "-mcpu=cortex-m33",\n      "-mthumb",\n      "-mabi=aapcs",\n      "-mfpu=fpv5-sp-d16",\n      "-mfloat-abi=hard",\n      "-mfp16-format=ieee",\n      "-mtp=soft",\n      "-ftls-model=local-exec",\n      "-mlong-calls"\n    ],\n    "variant": "arduino_uno_q_stm32u585xx",\n    "arduino": {\n      "board": "UNO_Q"\n    }\n  },\n  "frameworks": [\n    "arduino"\n  ],\n  "platforms": [\n    "arduinoq"\n  ],\n  "debug": {\n    "tools": {\n      "openocd": {\n        "default": true,\n        "onboard": true,\n        "server": {\n          "ready_pattern": "Listening on port \\\\d+ for gdb connections"\n        }\n      }\n    }\n  },\n  "name": "Arduino Uno Q",\n  "upload": {\n    "protocol": "linuxgpiod",\n    "maximum_ram_size": 804864,\n    "maximum_size": 2097152,\n    "maximum_sketch_ram_size": 262144,\n    "maximum_sketch_size": 786432\n  },\n  "url": "https://www.arduino.cc",\n  "vendor": "Arduino"\n}\n', 'builder/artifacts/zephyr-llext.py': b'# SPDX-FileCopyrightText: 2026 Advanced Additive Manufacturing Systems Laboratory, Sungkyunkwan University\n# SPDX-License-Identifier: Apache-2.0\n\n#\n# Zephyr LLEXT artifact generation: the multi-pass dynamic relocatable link\n# (temporary pass -> gen-rodata-ld -> final pass), debug-symbol stripping,\n# sketch packing, the static compatibility link check, and the "size"\n# target. This module knows about arm-zephyr-eabi-strip, gen-rodata-ld,\n# zephyr-sketch-tool and the resident firmware ELF exported by the framework\n# adapter; it has no knowledge of Linux GPIO wiring, OpenOCD, or the\n# framework package\'s internal layout beyond the ARDUINO_ZEPHYR_* values it\n# consumes.\n\nfrom os.path import isfile, join\n\nfrom SCons.Script import AlwaysBuild, Builder, Import, Return\n\nfrom arduinoq_common.sketch_size import check_sketch_size\n\nImport("env platform board build_mode")\n\nenv.AddMethod(check_sketch_size, "CheckSketchSize")\n\nenv.Replace(STRIP="arm-zephyr-eabi-strip")\n\n\ndef resolve_tool(package, executable, purpose):\n    """Absolute path to an executable shipped in a PlatformIO tool package.\n\n    Returns "" in "nobuild" mode: nothing is produced there, so a missing\n    tool is not an error, and demanding one would break the upload-only\n    sessions that never install it.\n    """\n    if build_mode.nobuild:\n        return ""\n    package_dir = platform.get_package_dir(package)\n    if not package_dir:\n        raise RuntimeError(\n            "zephyr-llext: missing \'%s\' package, required to %s"\n            % (package, purpose)\n        )\n    path = join(package_dir, executable)\n    if not isfile(path):\n        raise RuntimeError(\n            "zephyr-llext: \'%s\' does not provide %s at %s"\n            % (package, executable, path)\n        )\n    return path\n\n\nenv.Replace(\n    ZEPHYRSKETCH=resolve_tool(\n        "tool-zephyrsketch", "zephyr-sketch-tool",\n        "pack sketch/resident artifacts",\n    ),\n    GENRODATALD=resolve_tool(\n        "tool-genrodatald", "gen-rodata-ld",\n        "generate the split rodata linker script for the dynamic build",\n    ),\n    ZEPHYRCHECKSIZE=resolve_tool(\n        "tool-zephyrchecksize", "zephyr-check-size",\n        "compute sketch flash and LLEXT heap usage",\n    ),\n)\n\n# Startup mode. The resident firmware reads this out of the header that\n# zephyr-sketch-tool writes, so it is a property of the packed artifact,\n# decided at packing time -- not a compile or upload setting. Upstream\n# exposes it as the "wait_linux_boot" board menu; here it is\n# board_build.boot_mode, with the same default.\n#\n# "app" is upstream\'s name for the flag. The loader holds before loading the\n# sketch until released; host/board_control.py is the other end of that.\nSTARTUP_MODE_FLAGS = {\n    "wait": [],                  # hold until Linux is up (default)\n    "app": ["-wait_for_app"],    # hold until the host releases the sketch\n    "immediate": ["-immediate"],\n}\n\n# Defaults per build type, and why each one, are in CONTRIBUTING.md:\n# the "app" path in particular only works if this module, the test reader and\n# the board control agree, so it is documented where all three can find it.\n# board_build.boot_mode overrides these -- PlatformIO folds board_* options\n# into the board manifest, which is why the manifest itself states none.\nbuild_type = env["BUILD_TYPE"]\nif "debug" in build_type:\n    default_boot_mode = "immediate"\nelif "test" in build_type:\n    default_boot_mode = "app"\nelse:\n    default_boot_mode = "wait"\n\nboot_mode = board.get("build.boot_mode", default_boot_mode)\nif boot_mode not in STARTUP_MODE_FLAGS:\n    raise RuntimeError(\n        "zephyr-llext: unknown board_build.boot_mode %r; expected one of %s"\n        % (boot_mode, ", ".join(sorted(STARTUP_MODE_FLAGS)))\n    )\nenv.Replace(ZEPHYRSKETCHFLAGS=" ".join(STARTUP_MODE_FLAGS[boot_mode]))\n\n# The Arduino core is always linked as a relocatable LLEXT here; upstream\n# calls this "dynamic" and both gen-rodata-ld and zephyr-check-size take it\n# as an argument.\nLINK_MODE = "dynamic"\nenv.Replace(ZEPHYR_LINK_MODE=LINK_MODE)\n\nenv.Append(\n    BUILDERS=dict(\n        StripElf=Builder(\n            action=env.VerboseAction(" ".join([\n                \'"$STRIP"\',\n                "--strip-debug",\n                \'"$SOURCE"\',\n                "-o",\n                \'"$TARGET"\',\n            ]), "Stripping $TARGET"),\n            suffix=".elf"\n        ),\n        PackZephyrSketch=Builder(\n            action=env.VerboseAction(" ".join([\n                \'"$ZEPHYRSKETCH"\',\n                "$ZEPHYRSKETCHFLAGS",\n                "--output",\n                \'"$TARGET"\',\n                \'"$SOURCE"\',\n            ]), "Packing $TARGET"),\n            suffix=".bin"\n        ),\n        GenRodataLd=Builder(\n            action=env.VerboseAction(\n                \'"$GENRODATALD" "$SOURCE" "$TARGET" $ZEPHYR_LINK_MODE\',\n                "Generating rodata linker script $TARGET"\n            )\n        )\n    )\n)\n\n\nclass ArtifactResult:\n    """Structured build outputs handed back to builder/main.py."""\n\n    def __init__(self, sketch_elf=None, sketch_artifact=None,\n                 build_target=None, checkprogsize_target=None):\n        self.sketch_elf = sketch_elf\n        self.sketch_artifact = sketch_artifact\n        self.build_target = build_target\n        self.checkprogsize_target = checkprogsize_target\n\n\nsketch_elf = None\nsketch_artifact = None\ncheckprogsize_target = None\ndebug_elf = None\ncheck_elf = None\n\nif build_mode.nobuild:\n    # "nobuild" resolves the paths of artifacts produced by a prior normal\n    # build; it never triggers compilation or packing itself.\n    sketch_elf = join("$BUILD_DIR", "${PROGNAME}.elf")\n    sketch_artifact = join("$BUILD_DIR", "${PROGNAME}-zsk.bin")\nelse:\n    # ------------------------------------------------------------------\n    # Multi-pass dynamic relocatable link\n    #\n    # Upstream Arduino Zephyr 0.56.0 links the LLEXT application in three\n    # passes: a temporary relocatable link, gen-rodata-ld analyzing that\n    # temporary ELF to split .rodata into flash-resident and RAM-resident\n    # parts, and a final relocatable link that applies the generated split.\n    # This is built as an explicit SCons graph (cloned envs + Program()\n    # nodes) instead of a single env.BuildProgram() call, since that helper\n    # only ever constructs one link pass.\n    #\n    # env.BuildProgram()\'s "front matter" (resolving PIOBUILDFILES from the\n    # framework/project sources, applying build flags, enabling the linker\n    # cyclic-reference workaround) still has to run exactly once so both\n    # passes see the same, fully-populated build inputs; only its single\n    # env.Program() call is replaced.\n    # ------------------------------------------------------------------\n    env.ProcessProgramDeps()\n    env.ProcessProjectDeps()\n\n    # The Arduino core archive and the eagerly built framework libraries\n    # reference each other, so ld cannot resolve them in a single\n    # left-to-right pass over the archives; they have to be linked as one\n    # group. Unconditional here, unlike in a general-purpose build script:\n    # this platform pins arm-zephyr-eabi-gcc, always targets hardware, and\n    # always links a non-empty LIBS, so there is nothing to test for.\n    # Still needed under "-r": relocatable output does not stop ld from\n    # selecting archive members, so a cyclic reference can still leave one\n    # out.\n    env.Prepend(_LIBFLAGS="-Wl,--start-group ")\n    env.Append(_LIBFLAGS=" -Wl,--end-group")\n\n    dynamic_ldscript = env.get("LDSCRIPT_PATH")\n    if not dynamic_ldscript:\n        raise RuntimeError(\n            "zephyr-llext: framework adapter did not export a dynamic "\n            "LDSCRIPT_PATH"\n        )\n\n    build_sources = env.get("PIOBUILDFILES", [])\n    rodata_ld = join("$BUILD_DIR", "rodata_split.ld")\n\n    # --- Static compatibility check link --------------------------------\n    # Upstream runs this as the first step of every build\n    # (recipe.c.combine.1), not as an opt-in target: it re-links the same\n    # objects as a normal static executable against memory-check.ld, so\n    # that a sketch which cannot fit or resolve is rejected here rather\n    # than at load time. "-r" and the dynamic linker script are dropped\n    # for this pass, per upstream\'s note that the check has to emulate a\n    # static build.\n    check_env = env.Clone()\n    check_env.Replace(\n        PROGNAME="firmware-check",\n        LINKFLAGS=[\n            f for f in env.get("LINKFLAGS", [])\n            if f != "-r"\n        ]\n    )\n    check_env.Append(\n        LINKFLAGS=check_env.get("STATIC_CHECK_LINKFLAGS", [])\n    )\n    check_elf = check_env.Program(\n        check_env.subst("$BUILD_DIR/${PROGNAME}.elf"), build_sources\n    )\n\n    # PlatformIO\'s own env.BuildProgram() only injects "-T $LDSCRIPT_PATH"\n    # when LDSCRIPT_PATH is non-empty, so clearing it here on the per-pass\n    # clones (rather than on the shared "env") safely omits that automatic\n    # injection; the dynamic linker scripts are instead added explicitly\n    # below, in the exact order each pass requires. The common LLEXT link\n    # flags (machine flags, runtime-suppression flags, specs, --gc-sections,\n    # "-r", "-e main", libs) are inherited unchanged from "env" by both\n    # clones.\n\n    # --- Pass 1: temporary dynamic relocatable link ----------------------\n    temp_env = env.Clone()\n    temp_env.Replace(LDSCRIPT_PATH="")\n    temp_env.Append(LINKFLAGS=[\n        "-T", dynamic_ldscript,\n        \'-Wl,-Map,"%s"\' % join("$BUILD_DIR", "${PROGNAME}_temp.map"),\n    ])\n    temp_elf = temp_env.Program(\n        join("$BUILD_DIR", "${PROGNAME}_temp"), build_sources\n    )\n    temp_env.Depends(temp_elf, check_elf)\n\n    # --- gen-rodata-ld: derive the flash/RAM .rodata split ----------------\n    rodata_ld_node = env.GenRodataLd(rodata_ld, temp_elf)\n    env.Depends(rodata_ld_node, "$GENRODATALD")\n\n    # --- Pass 2: final dynamic relocatable link ----------------------------\n    # Same sources and common flags as pass 1; adds the generated rodata\n    # split ahead of the main dynamic linker script, per upstream order.\n    final_env = env.Clone()\n    final_env.Replace(LDSCRIPT_PATH="")\n    final_env.Append(LINKFLAGS=[\n        "-T", rodata_ld,\n        "-T", dynamic_ldscript,\n        \'-Wl,-Map,"%s"\' % join("$BUILD_DIR", "${PROGNAME}.map"),\n    ])\n    debug_elf = final_env.Program(\n        join("$BUILD_DIR", "${PROGNAME}_debug"), build_sources\n    )\n    final_env.Depends(debug_elf, rodata_ld_node)\n\n    env.Replace(PIOMAINPROG=debug_elf)\n\n    # PlatformIO reports "$PROGPATH" as prog_path in its build metadata,\n    # and that is the ELF a debug session opens in GDB. Its default,\n    # "$BUILD_DIR/$PROGNAME$PROGSUFFIX", is the stripped artifact that gets\n    # packed and flashed and carries no debug information, so the\n    # unstripped ELF is named instead -- the same distinction upstream\n    # makes between the upload artifact and debug.executable. Nothing else\n    # in PlatformIO builds from $PROGPATH except env.BuildProgram(), which this\n    # module replaces.\n    env.Replace(PROGPATH=debug_elf[0])\n\n    sketch_elf = env.StripElf(join("$BUILD_DIR", "${PROGNAME}"), debug_elf)\n    sketch_artifact = env.PackZephyrSketch(\n        join("$BUILD_DIR", "${PROGNAME}-zsk"), sketch_elf\n    )\n    # SCons does not treat an executable named inside an action string as an\n    # input, so without this an upgraded packer would leave the previous\n    # artifact in place. There is deliberately no equivalent for $STRIP: it\n    # is a bare command name resolved from PATH, not a package path, and\n    # Depends() would turn it into a File node relative to the project.\n    env.Depends(sketch_artifact, "$ZEPHYRSKETCH")\n    # zephyr-check-size reports flash usage as the on-disk size of the\n    # packed upload artifact, so unlike a plain "size" pass this check can\n    # only run once packing is done. The dependency direction is therefore\n    # the reverse of the usual PlatformIO arrangement, where checkprogsize\n    # runs straight after the link.\n    checkprogsize_target = AlwaysBuild(\n        env.Alias(\n            "checkprogsize",\n            sketch_elf,\n            env.VerboseAction(env.CheckSketchSize, "Checking size $SOURCE"),\n        )\n    )\n    env.Depends(checkprogsize_target, sketch_artifact)\n\nbuild_target = sketch_artifact\n\n# ---------------------------------------------------------------------------\n# "checklink" target\n#\n# The check link itself is part of every build (see above); this only\n# exposes it as a target that can be requested on its own.\n# Registered unconditionally, including in "nobuild" mode where check_elf is\n# None and the alias has nothing to depend on. That is not a silent no-op\n# standing in for a real check: "pio remote run" always builds locally\n# first, so if a check ran at all it already ran there. What reaches this\n# SCons invocation is only ever an upload-only pass, and "pio remote run"\n# reissues every "-t" the caller passed rather than filtering it -- so a\n# deployment command such as "-t checklink -t upload" reaches the "nobuild"\n# leg with "checklink" still in COMMAND_LINE_TARGETS.\n# ---------------------------------------------------------------------------\n\nenv.AddPlatformTarget(\n    name="checklink",\n    dependencies=check_elf,\n    actions=None\n)\n\n# ---------------------------------------------------------------------------\n# Size reporting\n#\n# Upstream replaced arm-zephyr-eabi-size with its own zephyr-check-size\n# (ArduinoCore-zephyr PR #385), because neither number this board cares\n# about is a plain section total: flash usage is the size of the packed\n# upload artifact, and RAM usage is the LLEXT heap footprint, which in a\n# dynamic build is the sum of every SHF_ALLOC section minus the\n# non-relocated rodata when CONFIG_LLEXT_RODATA_NO_RELOC is set. The tool\n# prints those two figures as ".text" and ".data" lines, matching\n# upstream\'s platform.txt recipe:\n#\n#     zephyr-check-size <link_mode> <config> <upload_file> <elf_file>\n#\n# so PlatformIO\'s size expressions become a direct read of those two\n# lines instead of a hand-maintained list of section names.\n# ---------------------------------------------------------------------------\n\nsize_target = None\nif not build_mode.nobuild:\n    resident_config = env.get("ARDUINO_ZEPHYR_RESIDENT_CONFIG")\n    if not resident_config:\n        raise RuntimeError(\n            "zephyr-llext: framework adapter did not export "\n            "ARDUINO_ZEPHYR_RESIDENT_CONFIG"\n        )\n    if not isfile(env.subst(resident_config)):\n        # Not fatal: zephyr-check-size treats an unreadable config as\n        # "CONFIG_LLEXT_RODATA_NO_RELOC unset", which only inflates the\n        # reported RAM figure. Worth saying out loud all the same.\n        print(\n            "Warning! Zephyr configuration not found at %s; reported RAM "\n            "usage may be too high" % resident_config\n        )\n\n    env.Replace(\n        ZEPHYR_SIZE_UPLOAD_ARTIFACT=sketch_artifact[0],\n        ZEPHYR_SIZE_ELF=sketch_elf[0],\n        SIZECHECKCMD=[\n            "$ZEPHYRCHECKSIZE",\n            "$ZEPHYR_LINK_MODE",\n            "$ARDUINO_ZEPHYR_RESIDENT_CONFIG",\n            "$ZEPHYR_SIZE_UPLOAD_ARTIFACT",\n            "$ZEPHYR_SIZE_ELF",\n        ],\n        SIZEPROGREGEXP=r"^\\.text\\s+(\\d+)",\n        SIZEDATAREGEXP=r"^\\.data\\s+(\\d+)",\n    )\n\n    size_target = env.Alias(\n        "size", sketch_artifact,\n        env.VerboseAction(env.CheckSketchSize, "Calculating size $SOURCE")\n    )\n    AlwaysBuild(size_target)\n\nartifacts = ArtifactResult(\n    sketch_elf=sketch_elf,\n    sketch_artifact=sketch_artifact,\n    build_target=build_target,\n    checkprogsize_target=checkprogsize_target,\n)\n\nReturn("artifacts")\n'}

    def prepare_unoq(self):
        root = self.core / "platforms/arduinoq"
        for name, raw in self.METADATA.items():
            path = root / name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(raw)
        self.write_metadata(self.core / "packages/framework-arduino-zephyr/package.json",
                            {"name":"framework-arduino-zephyr","version":"0.56.0-leelabskku+unoq"})
        return root, {"platform":"arduinoq","board":"uno_q","framework":"arduino","pio_manifest":str(root/"boards/uno_q.json")}

    @unittest.skipUnless(sys.platform == "linux", "The UNO Q toolchain is native Linux only")
    def test_unoq_shared_options_bind_exact_recipe_and_hook_bytes(self):
        root, info = self.prepare_unoq()
        options=compat.board_compatibility_options(info,self.core)
        hook=ROOT/"src/modules/arduinoq_link_paths.py"
        self.assertEqual(options["extra_scripts"],"post:"+hook.as_posix())
        self.assertEqual(options["custom_mcu_arduinoq_hook_sha256"],hashlib.sha256(hook.read_bytes()).hexdigest())
        text,actual=campaign.project_configuration({"platform_spec":"lee-lab-skku/arduinoq@1.0.0-rc.4","board":"uno_q","framework":"arduino"},
                         {"name":"arduinoq","manifest_path":info["pio_manifest"]},self.core)
        self.assertEqual(actual,options)
        self.assertIn("board = uno_q\n",text)
        self.assertIn("extra_scripts = post:",text)
        (root/"builder/artifacts/zephyr-llext.py").write_bytes(b"Changed future builder")
        self.assertEqual(compat.board_compatibility_options(info,self.core),{})

    @unittest.skipUnless(sys.platform == "linux", "The UNO Q toolchain is native Linux only")
    def test_unoq_actual_app_ini_binds_hook_bytes_and_preserves_unchanged_write(self):
        _,info=self.prepare_unoq()
        tree=ast.parse((ROOT/"main/web_bridge.py").read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=="_generate_platformio_ini")
        namespace={"Path":Path,"os":os,"re":__import__("re"),"SCRIPT_DIR":ROOT,"_project_root":self.root,
                   "MAX_BAUD_RATE":2000000,"DEFAULT_UPLOAD_SPEED":460800,"_get_safe_platformio_core_dir":lambda *args:self.core,
                   "write_generated_text":lambda path,text:path.write_text(text,encoding="utf-8")}
        exec(compile(ast.Module(body=[method],type_ignores=[]),"actual-app-unoq-generator","exec"),namespace)
        api=SimpleNamespace(current_board="UNO Q",current_baud=115200,upload_speed="460800",
                            _resolve_board_info=lambda *args:dict(info),_scan_includes_for_libs=lambda:[],emit=lambda *args:None)
        workspace=self.root/"project with spaces";workspace.mkdir()
        with mock.patch.object(os.path,"expanduser",lambda value:str(self.root/"home") if value=="~" else value):
            namespace["_generate_platformio_ini"](api,workspace)
            ini=workspace/"platformio.ini";before=ini.read_bytes();stamp=ini.stat().st_mtime_ns
            self.assertIn(b"board = uno_q\n",before)
            self.assertIn(b"extra_scripts = post:",before)
            self.assertIn(b"custom_mcu_arduinoq_hook_sha256 = ",before)
            namespace["_generate_platformio_ini"](api,workspace)
            self.assertEqual(ini.read_bytes(),before);self.assertEqual(ini.stat().st_mtime_ns,stamp)
        from main.core.compiled_cache import _config_hash
        config_before=_config_hash(workspace)
        ini.write_bytes(before.replace(compat.board_compatibility_options(info,self.core)["custom_mcu_arduinoq_hook_sha256"].encode(),b"0"*64))
        self.assertNotEqual(_config_hash(workspace),config_before)

    def test_unoq_is_refused_on_unsupported_hosts_and_unrelated_backend(self):
        _, info=self.prepare_unoq()
        with mock.patch.object(sys,"platform","win32"):
            self.assertEqual(compat.board_compatibility_options(info,self.core),{})
        with mock.patch.object(compat,"_read_metadata",side_effect=AssertionError("Unrelated source must not read UNO Q metadata")):
            self.assertEqual(compat.board_compatibility_options({**info,"board":"other"},self.core),{})
            self.assertEqual(compat.board_compatibility_options({**info,"backend":"arduino-cli"},self.core),{})

    @unittest.skipUnless(sys.platform == "linux", "The UNO Q toolchain is native Linux only")
    def test_tool_hook_uses_native_file_nodes_for_generated_paths_with_spaces(self):
        root,info=self.prepare_unoq()
        framework=self.core/"packages/framework-arduino-zephyr"
        dynamic=framework/"variants/fixture/build-dynamic.ld"
        dynamic.parent.mkdir(parents=True);dynamic.write_text("SECTIONS {}")
        build=self.root/"project path with spaces/.pio/build/campaign"
        expected=build/"rodata_split.ld"
        self.assertFalse(expected.exists(),"Generated linker script is not present at post-script time")
        class FileNode:
            def __init__(self,path):self.path=str(path)
            def get_abspath(self):return self.path
        class LinkEnv(dict):
            def subst(self,value):return value.replace("$BUILD_DIR",str(build)).replace("${PROGNAME}","firmware")
            def File(self,path):return FileNode(path)
            def Replace(self,**values):self.update(values)
        link=LinkEnv(LINKFLAGS=["-mcpu=cortex-m33","-T","$BUILD_DIR/rodata_split.ld","-T",str(dynamic),"-Wl,--gc-sections"])
        node=SimpleNamespace(get_env=lambda:link)
        platform=SimpleNamespace(get_dir=lambda:str(root),get_package_dir=lambda name:str(framework))
        env=SimpleNamespace(subst=lambda s:"uno_q" if s=="$BOARD" else link.subst(s), PioPlatform=lambda:platform,File=lambda path:node)
        script=ROOT/"src/modules/arduinoq_link_paths.py"
        tree=ast.parse(script.read_text());tree.body=[n for n in tree.body if not (isinstance(n,ast.ImportFrom) and n.module.startswith("SCons")) and not isinstance(n,ast.Expr)]
        namespace={};exec(compile(tree,"actual-tool-hook","exec"),namespace)
        namespace["_repair"](env)
        self.assertEqual(link["LINKFLAGS"][0],"-mcpu=cortex-m33")
        self.assertEqual(link["LINKFLAGS"][-1],"-Wl,--gc-sections")
        self.assertIsInstance(link["LINKFLAGS"][2],FileNode)
        self.assertEqual(link["LINKFLAGS"][2].get_abspath(),str(expected))
        self.assertIsInstance(link["LINKFLAGS"][4],FileNode)
        namespace["_repair"](env)
        self.assertEqual(link["LINKFLAGS"][2].get_abspath(),str(expected))
        self.assertFalse(expected.exists())

if __name__ == "__main__":
    unittest.main(verbosity=2)
