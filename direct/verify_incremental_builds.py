#!/usr/bin/env python3
"""Exercise the real compile worker with real PlatformIO, without any hardware.

Fixture verifiers mock the compiler, so they cannot see PlatformIO deleting
objects. This one links an owned core to already prepared AVR packages, builds
owned sketches and counts the compiler lines. No port is opened and no upload
target runs. It skips when this host has no prepared AVR packages.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from main import web_bridge
from main.core import compiled_cache
from src.modules.platform_runtime import native_platformio_dir

API = web_bridge.MCUWebBackendAPI
UNO = "Fixture Uno"
BOARD = {"platform": "atmelavr", "board": "uno", "framework": "arduino", "frameworks": ["arduino"],
         "pio_resolved": True, "backend": "platformio", "mcu": "atmega328p", "flash_mb": None,
         "has_psram": False, "memory_type": None, "flash_mode": None, "upload_protocol": "arduino",
         "upload_speed": 115200, "require_upload_port": True,
         "pio_match_score": 1005.0, "pio_manifest": "/first/spelling/uno.json", "source_core": "1.8.6"}
PACKAGES = ("framework-arduino-avr", "toolchain-atmelavr", "tool-scons", "tool-avrdude")
SKETCH = "#include <Arduino.h>\n#define LED {pin}\nvoid setup() {{ pinMode(LED, OUTPUT); }}\nvoid loop() {{ digitalWrite(LED, HIGH); }}\n"


def prepared_store() -> Path | None:
    store = native_platformio_dir()
    needed = [store / "platforms/atmelavr/platform.json", *(store / "packages" / name / "package.json" for name in PACKAGES)]
    return store if sys.platform.startswith("linux") and all(path.is_file() for path in needed) else None


@unittest.skipUnless(prepared_store(), "Real PlatformIO AVR packages are not prepared on this host")
class RealIncrementalBuilds(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/incremental-builds"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.core = self.root / "core"
        store = prepared_store()
        (self.core / "platforms").mkdir(parents=True)
        (self.core / "packages").mkdir()
        (self.core / "lib").mkdir()
        (self.core / ".tmp").mkdir()
        # Packages stay read-only; only this owned core's state is written.
        os.symlink(store / "platforms/atmelavr", self.core / "platforms/atmelavr")
        for name in PACKAGES:
            os.symlink(store / "packages" / name, self.core / "packages" / name)
        self.library = self.core / "lib" / "FixtureLib"
        self.library.mkdir()
        (self.library / "library.properties").write_text("name=FixtureLib\nversion=1.0.0\n", encoding="utf-8")
        (self.library / "FixtureLib.h").write_text("int fixture_value();\n", encoding="utf-8")
        (self.library / "FixtureLib.cpp").write_text('#include "FixtureLib.h"\nint fixture_value() { return 1; }\n', encoding="utf-8")
        self.boards = {UNO: dict(BOARD)}
        home = self.root / "home"
        home.mkdir()
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(home), "XDG_CACHE_HOME": str(self.root / "xdg")}))
        for name in ("PLATFORMIO_BUILD_CACHE_DIR", "PYTHONDONTWRITEBYTECODE"):
            os.environ.pop(name, None)
        # The app sets this in the real process, and it is what caused the wipe.
        os.environ["PLATFORMIO_BUILD_CACHE_DIR"] = str(self.core / ".cache/build")
        self.stack.enter_context(patch.object(web_bridge, "SUPPORTED_BOARDS", self.boards))
        self.stack.enter_context(patch.object(web_bridge, "SCRIPT_DIR", self.root))
        self.stack.enter_context(patch.object(web_bridge, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(web_bridge, "get_project_remembered_board", return_value=""))
        self.stack.enter_context(patch.object(web_bridge, "set_project_remembered_board"))
        self.stack.enter_context(patch.object(web_bridge, "_get_safe_platformio_core_dir", return_value=str(self.core)))
        self.stack.enter_context(patch.object(web_bridge, "_refresh_platformio_core_environment",
                                             return_value=(self.core, {})))
        self.stack.enter_context(patch.object(web_bridge, "is_unc_or_network_path", return_value=False))
        self.stack.enter_context(patch.object(web_bridge, "hide_generated_directory"))
        self.stack.enter_context(patch.object(web_bridge, "_analyze_gpio_compatibility", return_value={"excluded": set()}))
        self.stack.enter_context(patch.object(web_bridge.serial, "Serial", side_effect=AssertionError("Opened a port")))
        self.sketch = self.new_sketch("first")
        self.api = self.new_api(self.sketch)

    def new_sketch(self, name):
        sketch = self.root / name
        sketch.mkdir()
        (sketch / f"{name}.ino").write_text(SKETCH.format(pin=13), encoding="utf-8")
        return sketch

    def new_api(self, sketch):
        api = API.__new__(API)
        api.sketch_dir_path = sketch
        api.current_board, api.current_port = UNO, ""
        api.current_baud, api.upload_speed = 115200, "115200"
        api._board_frameworks, api.modified_files, api._build_metadata_by_board = {}, {}, {}
        api._last_source_hash = api._last_compiled_board = ""
        api._active_process, api._stop_requested, api.is_busy = None, False, False
        api.active_operation = api._current_op_phase = None
        api.emit = Mock()
        api._effective_cache_root = lambda *_args: sketch / ".mcu_flasher_build_cache"
        api._validate_entry_points = Mock(return_value=(True, "fixture"))
        api._get_jobs = Mock(return_value=6)
        api._refresh_compatible_devices = Mock()
        api._print_info_box = Mock()
        api.update_skip_compile_availability = Mock()
        api._unmap_unc_after_build = Mock()
        return api

    def compile(self, api=None):
        """Return the compiled unit names; a failed build fails the test."""
        api = api or self.api
        api.emit.reset_mock()
        started = time.monotonic()
        result = api._compile_worker(False)
        logs = [call.args[1].get("text", "") for call in api.emit.call_args_list if call.args[0] == "console:log"]
        self.assertTrue(result, "\n".join(logs)[-3000:])
        self.elapsed = time.monotonic() - started
        return [line.strip() for line in logs if line.strip().startswith("Compiling ")]

    def edit_pin(self, pin, api=None, name="first"):
        (self.sketch if api is None else api.sketch_dir_path).joinpath(f"{name}.ino").write_text(
            SKETCH.format(pin=pin), encoding="utf-8")

    def run_pio(self, *arguments):
        """Run PlatformIO exactly as the upload worker does, minus any port access."""
        jobs = 4
        _, environment = self.api._platformio_build_environment(jobs)
        command = [*web_bridge.find_pio_executable(), "run", *web_bridge.PLATFORMIO_RUN_OPTIONS,
                   "-e", "mcu_env", "-j", str(jobs), *arguments]
        import subprocess
        return subprocess.run(command, cwd=self.api._board_workspace_dir(), env=environment,
                              capture_output=True, text=True, timeout=300)

    def test_gpio_edit_recompiles_only_the_sketch(self):
        first = self.compile()
        self.assertGreater(len(first), 20, "The first build should compile the Arduino core")
        self.assertEqual(self.compile(), [], "An unchanged sketch recompiled something")
        self.edit_pin(12)
        self.assertEqual(self.compile(), ["Compiling first.ino.cpp.o..."])

    def test_upload_step_preserves_objects_and_firmware(self):
        self.compile()
        build = self.api._board_build_dir()
        object_file = next(build.glob("FrameworkArduino/*.o"))
        stamp = object_file.stat().st_mtime_ns
        # The real upload command with only its hardware target removed.
        result = self.run_pio("-t", "nobuild")
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        self.assertTrue((build / "firmware.hex").is_file(), "The upload step deleted the firmware")
        self.assertEqual(object_file.stat().st_mtime_ns, stamp, "The upload step deleted the objects")
        self.edit_pin(11)
        self.assertEqual(self.compile(), ["Compiling first.ino.cpp.o..."])

    def test_saved_upload_reuses_the_verified_firmware(self):
        self.compile()
        self.assertTrue(self.api.check_can_skip_compile())
        self.assertEqual(self.run_pio("-t", "nobuild").returncode, 0)
        self.assertTrue(self.api.check_can_skip_compile(), "The upload step invalidated Skip Compile")

    def test_adding_an_include_keeps_framework_objects(self):
        self.compile()
        (self.sketch / "first.ino").write_text(
            "#include <Arduino.h>\n#include <FixtureLib.h>\nvoid setup() { fixture_value(); }\nvoid loop() {}\n",
            encoding="utf-8")
        compiled = self.compile()
        self.assertEqual(len(compiled), 2, compiled)
        self.assertTrue(any("FixtureLib" in line for line in compiled), compiled)

    def test_a_new_sketch_reuses_framework_objects(self):
        first = self.compile()
        other = self.new_api(self.new_sketch("second"))
        compiled = self.compile(other)
        self.assertLess(len(compiled), 3, f"Expected cache hits; compiled {compiled} after {len(first)}")
        firmware = other._board_build_dir() / "firmware.hex"
        self.assertTrue(firmware.is_file())
        self.assertTrue(other.check_can_skip_compile())

    def test_unrelated_library_changes_do_not_invalidate_reuse(self):
        self.compile()
        self.assertTrue(self.api.check_can_skip_compile())
        unrelated = self.core / "lib" / "Unrelated"
        unrelated.mkdir()
        (unrelated / "Unrelated.h").write_text("#define UNRELATED 1\n", encoding="utf-8")
        self.assertTrue(self.api.check_can_skip_compile(), self.api._needs_recompile()[1])
        self.assertEqual(self.compile(), [])

    def test_used_library_edit_rebuilds_and_rejects_older_firmware(self):
        (self.sketch / "first.ino").write_text(
            "#include <Arduino.h>\n#include <FixtureLib.h>\nvoid setup() { fixture_value(); }\nvoid loop() {}\n",
            encoding="utf-8")
        self.compile()
        self.assertTrue(self.api.check_can_skip_compile())
        source = self.library / "FixtureLib.cpp"
        old = source.stat()
        source.write_text(source.read_text(encoding="utf-8").replace("return 1", "return 2"), encoding="utf-8")
        # Same size and timestamp: only the actual bytes reveal this edit.
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertTrue(any("FixtureLib" in line for line in self.compile()))
        self.assertTrue(self.api.check_can_skip_compile())

    def test_library_folder_over_the_limit_never_fails_a_build(self):
        with patch.object(compiled_cache, "_MAX_LIBRARY_ENTRIES", 3):
            self.assertGreater(len(self.compile()), 20)
            self.assertTrue(self.api._board_build_dir().joinpath("firmware.hex").is_file())

    def test_library_without_a_manifest_still_builds(self):
        (self.library / "library.properties").unlink()
        (self.sketch / "first.ino").write_text(
            "#include <Arduino.h>\n#include <FixtureLib.h>\nvoid setup() { fixture_value(); }\nvoid loop() {}\n",
            encoding="utf-8")
        self.assertTrue(any("FixtureLib" in line for line in self.compile()))

    def test_catalog_refresh_keeps_the_workspace(self):
        first = self.api._board_workspace_dir()
        self.boards[UNO].update(pio_match_score=7.5, pio_manifest="/other/spelling/uno.json", source_core="1.8.8")
        self.assertEqual(self.api._board_workspace_dir(), first)
        self.boards[UNO].update(flash_mb=2.0)
        self.assertNotEqual(self.api._board_workspace_dir(), first)

    def test_reset_build_is_dedicated_and_certified_once(self):
        self.compile()
        project, _ = self.api._prepare_reset_project(UNO, BOARD)
        self.assertTrue(project.is_relative_to(self.root / "soft_reset"))
        self.assertFalse(project.is_relative_to(self.sketch))
        jobs = 4
        _, environment = self.api._platformio_build_environment(jobs)
        built, error = self.api._compile_reset_project(project, UNO, BOARD, web_bridge.find_pio_executable(), jobs, environment)
        self.assertTrue(built, error)
        self.assertTrue(self.api._reset_build_status(project, UNO, BOARD)[0])
        # Editing the sketch, or a catalog refresh, leaves the reset firmware valid.
        self.edit_pin(9)
        self.compile()
        self.boards[UNO].update(pio_match_score=1.0, pio_manifest="/moved/uno.json")
        project_again, changed = self.api._prepare_reset_project(UNO, BOARD)
        self.assertEqual(project_again, project)
        self.assertFalse(changed)
        self.assertTrue(self.api._reset_build_status(project, UNO, BOARD)[0])


ESP32 = "Fixture ESP32"
ESP32_BOARD = {"platform": "espressif32", "board": "esp32dev", "framework": "arduino", "frameworks": ["arduino"],
               "pio_resolved": True, "backend": "platformio", "mcu": "esp32", "flash_mb": 4.0,
               "has_psram": False, "memory_type": None, "flash_mode": "dio", "upload_protocol": "esptool",
               "upload_speed": 460800, "require_upload_port": True}


def prepared_esp32_store() -> Path | None:
    store = native_platformio_dir()
    needed = [store / "platforms/espressif32/platform.json", store / "packages/framework-arduinoespressif32/package.json",
              store / "packages/tool-scons/package.json"]
    return store if sys.platform.startswith("linux") and all(path.is_file() for path in needed) else None


# Resolved now: each fixture redirects HOME while it runs.
ESP32_STORE = prepared_esp32_store()


@unittest.skipUnless(ESP32_STORE, "Real PlatformIO ESP32 packages are not prepared on this host")
class RealEsp32Builds(RealIncrementalBuilds):
    """The ESP32 path differs: generated images, partition tools and direct esptool upload."""

    def setUp(self):
        super().setUp()
        os.symlink(ESP32_STORE / "platforms/espressif32", self.core / "platforms/espressif32")
        for package in (ESP32_STORE / "packages").iterdir():
            link = self.core / "packages" / package.name
            if not os.path.lexists(link):
                os.symlink(package, link)
        self.boards[ESP32] = dict(ESP32_BOARD)
        self.api.current_board = ESP32

    def test_gpio_edit_recompiles_only_the_sketch(self):
        first = self.compile()
        self.assertGreater(len(first), 20)
        build = self.api._board_build_dir()
        self.assertTrue(all((build / name).is_file() for name in ("firmware.bin", "bootloader.bin", "partitions.bin")))
        self.edit_pin(4)
        self.assertEqual(self.compile(), ["Compiling first.ino.cpp.o..."])
        self.assertTrue(self.api.check_can_skip_compile())

    def test_hard_reset_images_are_built_once_and_independent_of_the_sketch(self):
        images, error = self.api._build_hard_reset_recovery_images(ESP32, ESP32_BOARD)
        self.assertIsNotNone(images, error)
        self.assertFalse(Path(images["project_dir"]).is_relative_to(self.sketch))
        self.edit_pin(5)
        self.compile()
        located, error = self.api._locate_hard_reset_recovery_images(ESP32, ESP32_BOARD)
        self.assertIsNotNone(located, error)
        self.assertEqual(located["bootloader"], images["bootloader"])


# Only the ESP32-specific checks above need repeating; the rest is covered on AVR.
for _name in [name for name in dir(RealIncrementalBuilds) if name.startswith("test_")]:
    if _name not in vars(RealEsp32Builds):
        setattr(RealEsp32Builds, _name, None)


class ObjectCacheBound(unittest.TestCase):
    def test_prune_removes_only_the_oldest_objects(self):
        from src.modules.platformio_build_env import object_cache_dir, prune_object_cache
        audit = ROOT / "temp/audit/incremental-builds"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as temporary:
            cache = object_cache_dir(temporary) / "ab"
            cache.mkdir(parents=True)
            for index in range(4):
                path = cache / f"{index}.o"
                path.write_bytes(b"x" * 100)
                os.utime(path, (1000 + index, 1000 + index))
            (object_cache_dir(temporary) / "config").write_text("{}", encoding="utf-8")
            self.assertEqual(prune_object_cache(temporary, limit=1000), 0)
            self.assertEqual(prune_object_cache(temporary, limit=300, target=200), 200)
            self.assertEqual(sorted(path.name for path in cache.iterdir()), ["2.o", "3.o"])
            self.assertTrue((object_cache_dir(temporary) / "config").is_file())


class SharedEnvironment(unittest.TestCase):
    def test_every_action_shares_the_checksum_inputs(self):
        from src.modules.platformio_build_env import RUN_OPTIONS, build_environment
        inherited = {"PLATFORMIO_BUILD_CACHE_DIR": "/x", "PYTHONDONTWRITEBYTECODE": "0", "PYTHONOPTIMIZE": "2"}
        environment = build_environment(inherited, "/core", 4)
        for name in inherited:
            self.assertNotIn(name, environment)
        self.assertIn("--disable-auto-clean", RUN_OPTIONS)
        self.assertEqual(environment["SCONSFLAGS"], "-j4")
        self.assertIn("pre:", environment["PLATFORMIO_EXTRA_SCRIPTS"])
        self.assertIn("post:", environment["PLATFORMIO_EXTRA_SCRIPTS"])

    def test_no_platformio_run_omits_the_shared_options(self):
        source = (ROOT / "main/web_bridge.py").read_text(encoding="utf-8")
        import re
        runs = re.findall(r'"run"(?!, \*PLATFORMIO_RUN_OPTIONS)', source)
        self.assertEqual(runs, [], "A PlatformIO run can still erase the build folder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
