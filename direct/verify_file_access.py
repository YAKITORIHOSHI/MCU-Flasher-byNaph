#!/usr/bin/env python3
"""Atomic user-source saves and cloud/backup lock handling in owned fixtures."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core import file_utils
from src.modules.file_access import is_sync_managed_path


class FileAccessChecks(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'temp/audit/file-access'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'User sketch.ino'
        self.before = b'void setup() {}\nvoid loop() {}\n'
        self.path.write_bytes(self.before)

    def test_atomic_save_retries_a_sharing_lock_without_truncating_the_original(self):
        real_replace, calls = os.replace, []
        locked = PermissionError('fixture sharing violation')
        locked.winerror = 32
        def replace(source, destination):
            calls.append(source)
            self.assertEqual(self.path.read_bytes(), self.before)
            if len(calls) < 3:
                raise locked
            return real_replace(source, destination)
        after = 'void setup() { /* µ */ }\nvoid loop() {}\n'
        with patch.object(file_utils.sys, 'platform', 'win32'), \
                patch.object(file_utils.time, 'sleep'), patch.object(file_utils.os, 'replace', side_effect=replace):
            file_utils.write_source_text(self.path, after, expected=self.before)
        self.assertEqual(self.path.read_bytes(), after.encode('utf-8'))
        self.assertEqual(len(calls), 3)
        self.assertEqual(list(self.root.glob('.mcu-save-*')), [])

    def test_persistent_lock_or_concurrent_source_change_preserves_original_bytes(self):
        with patch.object(file_utils.os, 'replace', side_effect=PermissionError('held by fixture')):
            with self.assertRaises(PermissionError):
                file_utils.write_source_text(self.path, 'new buffer', expected=self.before)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.path.write_text('external edit')
        with self.assertRaisesRegex(RuntimeError, 'changed on disk'):
            file_utils.write_source_text(self.path, 'new buffer', expected=self.before)
        self.assertEqual(self.path.read_text(), 'external edit')

    def test_sync_roots_use_native_boundary_checks_without_guessing_folder_names(self):
        env = {'OneDriveCommercial': r'C:\Users\Owner\OneDrive - Company'}
        self.assertTrue(is_sync_managed_path(r'c:\users\owner\onedrive - company\Sketch', environment=env, platform='win32'))
        self.assertFalse(is_sync_managed_path(r'C:\Users\Owner\OneDrive - Company backup\Sketch', environment=env, platform='win32'))
        self.assertFalse(is_sync_managed_path(r'D:\OneDrive\Sketch', environment=env, platform='win32'))
        self.assertFalse(is_sync_managed_path('/home/owner/OneDrive/Sketch', environment=env, platform='linux'))

    def test_file_locks_are_not_cache_corruption_and_real_compiler_errors_keep_priority(self):
        lock = '[WinError 145] .pio/build: directory is not empty; please manually remove the file'
        self.assertEqual(file_utils.classify_platformio_failure([lock]), 'lock')
        self.assertEqual(file_utils.classify_platformio_failure(['sketch.cpp:3:2: error: unknown type', lock]), 'source')
        self.assertEqual(file_utils.classify_platformio_failure(['SCons: corrupt sconsign database']), 'cache')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows-owned diagnostic child')
    def test_diagnostic_deadline_retires_a_windows_venv_child_without_touching_other_apps(self):
        import json
        import psutil
        from src.modules import file_access
        record = self.root / 'diagnostic-pid.json'
        worker = self.root / 'slow-diagnostic.py'
        worker.write_text('import json,os,time\nfrom pathlib import Path\n'
            f'Path({str(record)!r}).write_text(json.dumps({{"pid":os.getpid()}}))\n'
            'time.sleep(60)\n', encoding='utf-8')
        started = time.monotonic()
        with patch.object(file_access, '__file__', str(worker)):
            self.assertEqual(file_access.blocking_processes(self.path), [])
        self.assertLess(time.monotonic() - started, 8)
        self.assertTrue(record.is_file(), 'The diagnostic never reached its controlled slow phase')
        pid = json.loads(record.read_text())['pid']
        deadline = time.monotonic() + 2
        while psutil.pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(psutil.pid_exists(pid), 'An owned diagnostic survived its deadline')


if __name__ == '__main__':
    unittest.main(verbosity=2)
