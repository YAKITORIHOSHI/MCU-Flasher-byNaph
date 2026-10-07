#!/usr/bin/env python3
"""Isolated storage policy checks: no benchmarks, installers or device writes."""
from __future__ import annotations

import ast
import ctypes
import itertools
import os
import re
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from main.core import storage_resources as storage
with patch.object(storage, "storage_worker_limit", return_value=None):
    from main.core import build_resources


class StorageChecks(unittest.TestCase):
    def test_slow_probe_never_blocks_default_caller_and_is_single_flight(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def probe(path):
            calls.append(path)
            entered.set()
            release.wait(2)
            return storage.ROTATIONAL
        cache = storage.StorageProbeCache(probe)
        start = time.monotonic()
        self.assertEqual(cache.request("slow-drive"), storage.UNKNOWN)
        self.assertLess(time.monotonic() - start, 0.10)
        self.assertTrue(entered.wait(1))
        for _ in range(100):
            self.assertEqual(cache.request("slow-drive"), storage.UNKNOWN)
        self.assertEqual(calls, ["slow-drive"])
        release.set()
        self.assertEqual(cache.request("slow-drive", 0.35), storage.ROTATIONAL)

    def test_worker_wait_is_bounded(self):
        release = threading.Event()
        cache = storage.StorageProbeCache(lambda _: (release.wait(2), storage.UNKNOWN)[1])
        start = time.monotonic()
        self.assertEqual(cache.request("slow-drive", 60), storage.UNKNOWN)
        self.assertLess(time.monotonic() - start, 0.6)
        release.set()

    def test_queue_and_cache_have_fixed_bounds(self):
        release = threading.Event()
        cache = storage.StorageProbeCache(lambda _: (release.wait(2), storage.SOLID_STATE)[1])
        for index in range(1000):
            cache.request(str(index))
        self.assertLessEqual(len(cache._pending), 32)
        self.assertLessEqual(len(cache._queue), 32)
        release.set()
        cache.request("0", 0.35)
        for index in range(200):
            cache.request("later" + str(index), 0.35)
        self.assertLessEqual(len(cache._cache), 128)

    def test_drive_swaps_expire_and_unknown_does_not_claim_hdd(self):
        clock = [0]
        profiles = iter((storage.ROTATIONAL, storage.SOLID_STATE, storage.UNKNOWN))
        cache = storage.StorageProbeCache(lambda _: next(profiles), lambda: clock[0])
        self.assertEqual(cache.request("E:/app", 0.35), storage.ROTATIONAL)
        clock[0] = 301
        self.assertEqual(cache.request("E:/app", 0.35), storage.SOLID_STATE)
        clock[0] = 602
        self.assertEqual(cache.request("E:/app", 0.35), storage.UNKNOWN)

    def test_failed_thread_start_recovers_for_explicit_next_request(self):
        cache = storage.StorageProbeCache(lambda _: storage.REMOVABLE)
        with patch.object(storage.threading.Thread, "start", side_effect=RuntimeError("slots")):
            self.assertEqual(cache.request("usb", 0.35), storage.UNKNOWN)
        self.assertFalse(cache._pending)
        self.assertFalse(cache._queue)
        self.assertEqual(cache.request("usb", 0.35), storage.REMOVABLE)

    def test_slowest_relevant_store_limits_high_end_cpu_not_unknown_or_ssd(self):
        with patch.object(storage, "storage_worker_limit", return_value=None):
            self.assertEqual(build_resources._resource_safe_worker_count("MAX", 32, 32, 16), 16)
            self.assertEqual(build_resources._resource_safe_worker_count("MAX", 8, 8, 4), 2)
            self.assertEqual(build_resources._resource_safe_worker_count("MAX", 12, 8, 6), 4)
        for limit in (1, 2):
            with patch.object(storage, "storage_worker_limit", return_value=limit) as query:
                self.assertEqual(build_resources._resource_safe_worker_count("MAX", 32, 32, 16,
                                                                            storage_paths=("project", "tools")), limit)
                query.assert_called_with(("project", "tools"), wait=False)
        with patch.object(storage, "_CACHE", storage.StorageProbeCache(lambda path: storage.NETWORK if "network" in path else storage.SOLID_STATE)):
            self.assertEqual(storage.storage_worker_limit(("network", "ssd"), wait=True), 1)

    def test_relevant_paths_are_lexical_and_include_actual_tool_store(self):
        with patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": "tool-store", "PLATFORMIO_PACKAGES_DIR": "packages"}), patch.object(Path, "resolve", side_effect=AssertionError("caller walked disk")):
            paths = storage.relevant_storage_paths(("project", "workspace", "project"))
        self.assertIn(os.path.normcase(os.path.abspath("tool-store")), paths)
        self.assertIn(os.path.normcase(os.path.abspath("workspace")), paths)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertLessEqual(len(storage.relevant_storage_paths(itertools.repeat("same", 1000))), 4)
        self.assertLessEqual(len(storage.relevant_storage_paths(str(index) for index in itertools.count())), 19)

    def test_bootstrap_policy_caps_inherited_jobs_and_retains_other_flags(self):
        # Extract just the helper; importing bootstrap could start host setup.
        source = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        helper = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_apply_bootstrap_compiler_budget")
        namespace = {"SCRIPT_DIR": ROOT, "os": os, "re": re}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "bootstrap-budget", "exec"), namespace)
        apply_budget = namespace[helper.name]
        for flags, expected in (("-Q -j32 --warn=no-deprecated", 2), ("-Q -j 1 --warn=no-deprecated", 1),
                                ("--jobs=1 -Q", 1), ("--jobs 1 -Q", 1)):
            env = {"PLATFORMIO_BUILD_JOBS": "32", "PLATFORMIO_RUN_JOBS": "32", "SCONSFLAGS": flags}
            with patch.object(build_resources, "get_optimal_compiler_jobs", return_value=2) as query:
                self.assertEqual(apply_budget(env, "package-store"), expected)
                query.assert_called_once_with(storage_paths=(ROOT, "package-store"), storage_wait=True)
            self.assertEqual(env["PLATFORMIO_BUILD_JOBS"], str(expected))
            self.assertIn("-Q", env["SCONSFLAGS"])
            self.assertIn("-j" + str(expected), env["SCONSFLAGS"])
            self.assertNotIn("32", env["SCONSFLAGS"])
        env = {"PLATFORMIO_BUILD_JOBS": "1"}
        with patch.object(build_resources, "get_optimal_compiler_jobs", return_value=4):
            self.assertEqual(apply_budget(env, "package-store"), 1)

    def _windows_api(self, bus=3, seek=True, drive_type=3, missing_seek=False):
        api = MagicMock()
        api.GetVolumePathNameW.side_effect = lambda path, buffer, length: (setattr(buffer, "value", "C:\\"), 1)[1]
        api.GetDriveTypeW.return_value = drive_type
        api.GetVolumeNameForVolumeMountPointW.side_effect = lambda path, buffer, length: (setattr(buffer, "value", "\\\\?\\Volume{fixture}\\"), 1)[1]
        api.CreateFileW.return_value = 0x100000010
        def query(handle, ioctl, request, query_size, output, output_size, returned, overlapped):
            self.assertEqual(handle, 0x100000010)
            self.assertEqual(query_size, 12)
            prop = struct.unpack_from("<I", ctypes.string_at(request, query_size))[0]
            if prop == 0:
                data = bytearray(36)
                struct.pack_into("<II", data, 0, 36, 36)
                struct.pack_into("<I", data, 28, bus)
            else:
                if missing_seek:
                    return 0
                data = struct.pack("<IIB3x", 12, 12, seek)
            ctypes.memmove(output, bytes(data), len(data))
            returned._obj.value = len(data)
            return 1
        api.DeviceIoControl.side_effect = query
        return api

    def test_windows_pointer_safe_hdd_ssd_usb_and_handle_cleanup(self):
        for bus, seek, expected in ((3, True, storage.ROTATIONAL), (3, False, storage.SOLID_STATE), (7, False, storage.REMOVABLE)):
            api = self._windows_api(bus, seek)
            with patch.object(ctypes, "WinDLL", return_value=api, create=True), patch.object(storage.os.path, "realpath", side_effect=lambda value: value):
                self.assertEqual(storage._windows_profile("C:/fixture"), expected)
            self.assertIs(api.CreateFileW.restype, ctypes.c_void_p)
            api.CloseHandle.assert_called_once_with(0x100000010)

    def test_windows_removable_network_and_unsupported_query(self):
        for drive_type, expected in ((2, storage.REMOVABLE), (4, storage.NETWORK)):
            api = self._windows_api(drive_type=drive_type)
            with patch.object(ctypes, "WinDLL", return_value=api, create=True), patch.object(storage.os.path, "realpath", side_effect=lambda value: value):
                self.assertEqual(storage._windows_profile("C:/fixture"), expected)
            api.CreateFileW.assert_not_called()
        api = self._windows_api(missing_seek=True)
        with patch.object(ctypes, "WinDLL", return_value=api, create=True), patch.object(storage.os.path, "realpath", side_effect=lambda value: value):
            self.assertEqual(storage._windows_profile("C:/fixture"), storage.UNKNOWN)
        with patch.object(ctypes, "WinDLL", side_effect=AssertionError("network must not be opened"), create=True):
            self.assertEqual(storage._windows_profile("\\\\server\\share\\sketch"), storage.NETWORK)
            self.assertEqual(storage._windows_profile("\\\\?\\UNC\\server\\share\\sketch"), storage.NETWORK)
        with patch.object(storage.sys, "platform", "win32"):
            cache = storage.StorageProbeCache(lambda _: self.fail("UNC probe queued"))
            self.assertEqual(cache.request("\\\\server\\share\\sketch"), storage.NETWORK)
            self.assertFalse(cache._pending)

    def test_linux_partition_and_removable_flags_without_host_devices(self):
        # No fixture writes are needed: map the two tiny sysfs fields in memory.
        for values, expected in (({"queue/rotational": "1"}, storage.ROTATIONAL),
                                 ({"queue/rotational": "0"}, storage.SOLID_STATE),
                                 ({"removable": "1", "queue/rotational": "0"}, storage.REMOVABLE)):
            with patch.object(Path, "resolve", side_effect=lambda strict=True: Path("/sys/fixture/disk")), patch.object(Path, "iterdir", side_effect=OSError), patch.object(storage, "_small_text", side_effect=lambda path: next((value for suffix, value in values.items() if path.as_posix().endswith(suffix)), "")):
                self.assertEqual(storage._linux_block_profile(Path("/sys/fixture/disk/partition")), expected)

    def test_linux_mount_profile_uses_longest_mount_and_decodes_spaces(self):
        cases = (
            ("/mnt/drive/sketch", "/mnt/drive", "nfs4", storage.NETWORK),
            ("/mnt/drive/sketch", "/mnt/drive", "cifs", storage.NETWORK),
            ("/mnt/drive/sketch", "/mnt/drive", "tmpfs", storage.SOLID_STATE),
            ("/mnt/drive with spaces/sketch", r"/mnt/drive\040with\040spaces", "fuse.sshfs", storage.NETWORK),
            ("/mnt/drive/sketch", "/mnt/drive", "ext4", storage.ROTATIONAL),
        )
        for path, mount, filesystem, expected in cases:
            # A broader network mount must not override a nested local mount.
            mountinfo = ("1 0 0:1 / / rw - nfs server:/root rw\n"
                         f"2 1 8:1 / {mount} rw - {filesystem} fixture rw\n")
            with self.subTest(filesystem=filesystem, mount=mount), \
                 patch.object(storage.os.path, "realpath", side_effect=lambda value: value), \
                 patch.object(Path, "stat", return_value=SimpleNamespace(st_dev=7)), \
                 patch.object(storage, "_small_text", return_value=mountinfo), \
                 patch.object(storage.os, "major", return_value=8, create=True), \
                 patch.object(storage.os, "minor", return_value=1, create=True), \
                 patch.object(storage, "_linux_block_profile", return_value=storage.ROTATIONAL) as block:
                self.assertEqual(storage._linux_profile(path), expected)
                if expected == storage.ROTATIONAL:
                    block.assert_called_once_with(Path("/sys/dev/block/8:1"))
                else:
                    block.assert_not_called()

    def test_linux_absent_destination_uses_parent_and_missing_device_is_unknown(self):
        for failure, expected in ((None, storage.REMOVABLE), (FileNotFoundError, storage.UNKNOWN)):
            with self.subTest(failure=failure), \
                 patch.object(storage.os.path, "realpath", side_effect=lambda value: value), \
                 patch.object(Path, "stat", side_effect=[FileNotFoundError(), SimpleNamespace(st_dev=7)]) as stat, \
                 patch.object(storage, "_small_text", return_value="1 0 8:1 / / rw - ext4 fixture rw\n"), \
                 patch.object(storage.os, "major", return_value=8, create=True), \
                 patch.object(storage.os, "minor", return_value=1, create=True), \
                 patch.object(storage, "_linux_block_profile", return_value=storage.REMOVABLE, side_effect=failure):
                self.assertEqual(storage._linux_profile("/mnt/drive/new-sketch"), expected)
                self.assertEqual(stat.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
