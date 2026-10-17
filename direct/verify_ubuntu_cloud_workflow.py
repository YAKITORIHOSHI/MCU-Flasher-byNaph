#!/usr/bin/env python3
"""Native source/build-input round trip; mocked cloud/compiler, real host GCC.

Everything is disposable under temp/. This does not compile ESP32 firmware,
install tools, authenticate, discover boards/ports or change live persistence.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct import verify_board_build_cache as board_fixture
from direct.verify_cloud_sketch_service import CONFIG, MemoryHTTP, MemoryStore
from main import web_bridge
from main.core import cloud_sketch_service as cloud, compiled_cache
from main.platforms import ubuntu
from src.modules.private_python_guard import is_running_private_python
from src.modules.ubuntu_release import require_supported_ubuntu


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Ubuntu filesystem/compiler checks")
class UbuntuCloudWorkflowChecks(unittest.TestCase):
    def setUp(self):
        self.assertTrue(is_running_private_python(), "Use .venv-linux/bin/python")
        self.assertIsNotNone(require_supported_ubuntu())
        self.build = board_fixture.BoardBuildCacheChecks()
        self.build.setUp()
        self.addCleanup(self.build.doCleanups)
        self.root, self.original = self.build.sandbox, self.build.project
        self.build.source.unlink()
        self.files = {
            "BLE.ino": ("#include <NimBLEDevice.h>\r\n#include \"BleConfig.h\"\r\n"
                        "// BLE Ω😀\r\nvoid setup() { NimBLEDevice::init(\"Probe\"); }\r\n"
                        "void loop() {}\r\n").encode("utf-8"),
            "BleConfig.h": b"#pragma once\r\n#define DEVICE_NAME \"Probe\"\r\n",
            "ble_impl.cpp": b'#include "BleConfig.h"\nint ble_value = 1;\n',
            "wire.c": b"int wire_value(void) { return 2; }\n",
            "Types.hpp": b"#pragma once\nstruct Packet { int value; };\n",
            "NOTE.txt": b"\xef\xbb\xbfUTF-8 notes\r\nwith mixed\nline endings\r\n",
        }
        for name, raw in self.files.items():
            (self.original / name).write_bytes(raw)
        self.library = self.build.core / "lib/NimBLE_Arduino-2.5.1"
        (self.library / "src").mkdir(parents=True)
        (self.library / "src/NimBLEDevice.h").write_bytes(b"// fixture NimBLE header\n")
        (self.library / "library.json").write_text(json.dumps({"name": "NimBLE-Arduino", "version": "2.5.1"}))
        self.http = MemoryHTTP()
        self.service = cloud.CloudSketchService(credential_store=MemoryStore(), transport=self.http, data_dir=self.root / "private-cloud",
            source_root_provider=lambda: self.root / "generated")
        self.build.stack.enter_context(patch.object(cloud.urllib.request, "build_opener",
            side_effect=AssertionError("Real HTTP forbidden")))
        self.build.stack.enter_context(patch.object(web_bridge, "_get_safe_platformio_core_dir",
            return_value=str(self.build.core)))
        self.build.stack.enter_context(patch.object(web_bridge, "_project_root", self.root / "installation"))
        # Do not change HOME or discover actual account library folders.
        self.build.stack.enter_context(patch.object(web_bridge.os.path, "expanduser",
            side_effect=lambda path: str(self.root / "fixture-home") if path == "~" else path))
        self.service.sign_in("fixture@example.com", "fixture password")

    def snapshot_build(self, project):
        api = self.build.api
        api.sketch_dir_path = project
        api._effective_cache_root = lambda *_args: project / ".mcu_flasher_build_cache"
        api._generate_platformio_ini = lambda workspace: web_bridge.MCUWebBackendAPI._generate_platformio_ini(api, workspace)
        self.build.compile_board(board_fixture.ESP)
        workspace = api._board_workspace_dir()
        self.assertTrue(workspace.is_relative_to(self.root))
        self.assertEqual(self.build.calls[-1][0], workspace)
        self.assertIn("ubuntu-", workspace.parent.name)
        staged = {path.name: path.read_bytes() for path in (workspace / "src").iterdir()}
        config = (workspace / "platformio.ini").read_bytes()
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read_string(config.decode("utf-8"))
        self.assertEqual(parsed["env:mcu_env"]["platform"], "espressif32")
        self.assertEqual(parsed["env:mcu_env"]["board"], "esp32dev")
        self.assertEqual(parsed["env:mcu_env"]["framework"], "arduino")
        self.assertEqual(parsed["env:mcu_env"]["lib_deps"].strip(), "symlink://" + self.library.as_posix())
        self.assertEqual(json.loads((self.library / "library.json").read_bytes())["version"], "2.5.1")
        for name in ("BleConfig.h", "ble_impl.cpp", "wire.c", "Types.hpp"):
            self.assertEqual(staged[name], self.files[name])
        self.assertIn("BLE.ino.cpp", staged)
        self.assertTrue(api.check_can_skip_compile())
        return staged, config, compiled_cache.input_signature(workspace), api._hash_sources()

    def test_source_filenames_bytes_exact_target_libraries_and_staging_match(self):
        before = self.snapshot_build(self.original)
        meta = self.service.upload_project(self.original, "BLE cloud source round trip")
        restored = self.service.pull_project(meta["id"])
        self.assertEqual({path.name: path.read_bytes() for path in restored.iterdir()
                          if path.suffix.lower() in cloud.SOURCE_SUFFIXES}, self.files)
        self.assertFalse((restored / ".mcu_flasher_build_cache").exists())
        after = self.snapshot_build(restored)
        self.assertEqual(before, after)
        self.assertEqual(self.service.working_directory(meta["id"]), restored)
        self.assertEqual(list(restored.parent.iterdir()), [restored])
        report = ROOT / "temp/audit/ubuntu-cloud"
        report.mkdir(parents=True, exist_ok=True)
        (report / "source-comparison.json").write_text(json.dumps({
            "host": compiled_cache.host_namespace(), "target": "espressif32:esp32dev", "framework": "arduino",
            "library": "NimBLE-Arduino 2.5.1 (fixture)", "compiler": "mocked; real staging/configuration",
            "sources": {name: {"original_sha256": hashlib.sha256(raw).hexdigest(),
                               "restored_sha256": hashlib.sha256((restored / name).read_bytes()).hexdigest()}
                        for name, raw in self.files.items()},
            "staged_sources": {name: hashlib.sha256(raw).hexdigest() for name, raw in after[0].items()},
            "generated_ini_identical": before[1] == after[1], "build_inputs_identical": before[2:] == after[2:],
        }, indent=2) + "\n", encoding="utf-8")
        print("Native cloud round trip: exact root bytes/names, ESP32 Arduino target, NimBLE 2.5.1 and generated inputs match")

    def test_case_only_pull_renames_remove_old_native_source_and_stage(self):
        meta = self.service.upload_project(self.original)
        restored = self.service.pull_project(meta["id"])
        self.snapshot_build(restored)
        # Reproduce case-only rename on a linked editing copy; keep exact include case.
        (restored / "BleConfig.h").rename(restored / "bleconfig.h")
        (restored / "BLE.ino").write_bytes(self.files["BLE.ino"].replace(b"BleConfig.h", b"bleconfig.h"))
        (restored / "ble_impl.cpp").write_bytes(self.files["ble_impl.cpp"].replace(b"BleConfig.h", b"bleconfig.h"))
        self.service.push_project(restored, meta["id"], 1)
        self.service.pull_project(meta["id"], self.original, replace_existing=True,
            expected_local_state=cloud._local_pull_state(self.original)[0], expected_revision=2)
        self.assertFalse((self.original / "BleConfig.h").exists())
        self.assertEqual((self.original / "bleconfig.h").read_bytes(), self.files["BleConfig.h"])
        api = self.build.api
        api.sketch_dir_path = self.original
        self.build.compile_board(board_fixture.ESP)
        staged = api._board_workspace_dir() / "src"
        self.assertFalse((staged / "BleConfig.h").exists())
        self.assertEqual((staged / "bleconfig.h").read_bytes(), self.files["BleConfig.h"])

    def test_native_environment_keeps_fixture_store_and_clears_foreign_hints(self):
        with patch.dict(os.environ, {"PYTHONEXEPATH": "C:\\foreign.exe", "PIO_PYTHON_EXE": "C:\\foreign.exe",
                                    "PLATFORMIO_CORE_DIR": "C:\\foreign"}), \
             patch.object(ubuntu, "_get_safe_platformio_core_dir", return_value=str(self.build.core)):
            self.assertEqual(ubuntu._configure_platformio_environment(), str(self.build.core))
            self.assertNotIn("PIO_PYTHON_EXE", os.environ)
            self.assertNotIn("PYTHONEXEPATH", os.environ)
            for name in ("PLATFORMIO_CORE_DIR", "PLATFORMIO_PACKAGES_DIR", "TMPDIR"):
                self.assertTrue(Path(os.environ[name]).is_relative_to(self.build.core))

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses ordinary native file permission denials")
    def test_native_denied_sources_and_replacement_folder_preserve_content(self):
        source = self.original / "BleConfig.h"
        source.chmod(0)
        try:
            with self.assertRaisesRegex(cloud.CloudError, "could not be read"):
                self.service.upload_project(self.original)
        finally:
            source.chmod(0o644)
        meta = self.service.upload_project(self.original)
        restored = self.service.pull_project(meta["id"])
        old_link = (restored / cloud.LINK_NAME).read_bytes()
        restored.chmod(0o555)
        try:
            with self.assertRaises(cloud.CloudError):
                self.service.pull_project(meta["id"], restored)
            self.assertEqual((restored / cloud.LINK_NAME).read_bytes(), old_link)
            self.assertEqual((restored / "BLE.ino").read_bytes(), self.files["BLE.ino"])
        finally:
            restored.chmod(0o755)

    @unittest.skipUnless(shutil.which("gcc"), "Native host GCC unavailable")
    def test_host_gcc_creates_deep_dependency_file_without_alias(self):
        # Actual native GCC/filesystem coverage, not an ESP32 firmware compile.
        project = self.root / ("cloud-" + "x" * 95)
        output = project / ".mcu_flasher_build_cache/boards/ubuntu-x86_64" / ("esp32dev_" + "y" * 40)
        output /= ".pio/build/mcu_env/libc62/NimBLE_Arduino-2.5.1/nimble/nimble/transport/esp_ipc_legacy/src"
        output.mkdir(parents=True)
        source = self.root / "hci_esp_ipc_legacy.c"
        source.write_bytes(b"int native_dependency_probe(void) { return 0; }\n")
        dependency = output / "hci_esp_ipc_legacy.c.d"
        self.assertGreater(len(str(dependency)), 281)
        # The build fixture mocks Popen globally; explicitly restore it only for
        # this finite local compiler probe with every output under the fixture.
        with patch.object(subprocess, "Popen", self.build.original_popen):
            result = subprocess.run([shutil.which("gcc"), "-MMD", "-MF", str(dependency), "-c", str(source),
                                     "-o", str(output / "probe.o")], cwd=project, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertTrue(dependency.is_file())
        self.assertIn(source.name.encode(), dependency.read_bytes())
        print(f"Native host GCC dependency path: {len(str(dependency))} characters, successful")


if __name__ == "__main__":
    unittest.main(verbosity=2)
