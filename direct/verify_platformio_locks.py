"""Native lock contention/attribute fixtures, without toolchain installation."""
from __future__ import annotations
import os
import ast
import errno
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules.platformio_locks import package_locks
from platformio.package import lockfile


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows byte locks')
class LockChecks(unittest.TestCase):
    def setUp(self):
        parent = ROOT / 'temp/audit/platformio-locks'
        parent.mkdir(parents=True, exist_ok=True)
        folder = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(folder.cleanup)
        self.core = Path(folder.name)
        self.environment = patch.dict(os.environ, {'PLATFORMIO_CORE_DIR': str(self.core)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_hidden_lock_is_opened_without_truncation(self):
        import ctypes
        path = self.core / 'packages.lock'
        path.write_bytes(b'keep')
        self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(str(path), 2))
        try:
            with self.assertRaises(PermissionError):
                with path.open('w'):
                    pass
            with package_locks():
                item = lockfile.LockFile(str(self.core / 'packages'), timeout=2)
                self.assertTrue(item.acquire())
                item._unlock()
                self.assertEqual(path.read_bytes(), b'keep')
                item.release()
        finally:
            if path.exists():
                ctypes.windll.kernel32.SetFileAttributesW(str(path), 128)

    def test_contended_lock_waits_instead_of_truncating(self):
        code = """import os, sys, time
sys.path.insert(0, sys.argv[1])
from src.modules.platformio_locks import package_locks
from platformio.package.lockfile import LockFile
with package_locks():
    for iteration in range(30):
        with LockFile(os.path.join(os.environ['PLATFORMIO_CORE_DIR'], 'packages'), timeout=5, delay=.002):
            time.sleep(.001)
    print('acquired', flush=True)
"""
        with package_locks():
            first = lockfile.LockFile(str(self.core / 'packages'), timeout=2)
            first.acquire()
            children = [subprocess.Popen([sys.executable, '-B', '-c', code, str(ROOT)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(4)]
            try:
                time.sleep(.3)
                self.assertTrue(all(child.poll() is None for child in children))
                first.release()
                for child in children:
                    out, err = child.communicate(timeout=8)
                    self.assertEqual(child.returncode, 0, err.decode('utf-8', errors='replace'))
                    self.assertEqual(out.strip(), b'acquired')
            finally:
                first.release()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.communicate()

    def test_real_read_only_permission_is_reported_and_preserved(self):
        import ctypes
        path = self.core / 'packages.lock'
        path.write_bytes(b'keep')
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 1)
        try:
            with package_locks():
                with self.assertRaisesRegex(PermissionError, 'Check write access'):
                    lockfile.LockFile(str(self.core / 'packages'), timeout=1).acquire()
            self.assertEqual(path.read_bytes(), b'keep')
        finally:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 128)

    def test_unexpected_lock_errors_propagate_without_retry_and_close_handle(self):
        native_fdopen = os.fdopen
        for code in (errno.EINVAL, errno.EBADF):
            with self.subTest(errno=code):
                opened = []
                def capture_handle(*args, **kwargs):
                    handle = native_fdopen(*args, **kwargs)
                    opened.append(handle)
                    return handle
                error = OSError(code, 'Injected unexpected native byte-lock failure')
                item = lockfile.LockFile(str(self.core / 'packages'), timeout=3600)
                with package_locks(), patch.object(os, 'fdopen', side_effect=capture_handle), \
                        patch.object(lockfile.msvcrt, 'locking', side_effect=error) as operation, \
                        patch.object(lockfile, 'sleep', side_effect=AssertionError('Unexpected errors must not enter the retry loop')) as pause:
                    with self.assertRaises(OSError) as raised:
                        item.acquire()
                    self.assertIs(raised.exception, error)
                    operation.assert_called_once()
                    pause.assert_not_called()
                self.assertIsNone(item._fp)
                self.assertEqual(len(opened), 1)
                self.assertTrue(opened[0].closed)
                item.release()

    def test_eacces_lock_contention_remains_retryable_and_closes_handle(self):
        native_fdopen = os.fdopen
        opened = []
        def capture_handle(*args, **kwargs):
            handle = native_fdopen(*args, **kwargs)
            opened.append(handle)
            return handle
        error = OSError(errno.EACCES, 'Injected native lock contention')
        item = lockfile.LockFile(str(self.core / 'packages'))
        with package_locks(), patch.object(os, 'fdopen', side_effect=capture_handle), \
                patch.object(lockfile.msvcrt, 'locking', side_effect=error):
            with self.assertRaises(lockfile.LockFileExists) as raised:
                item._lock()
            self.assertIs(raised.exception.__cause__, error)
        self.assertIsNone(item._fp)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)
        item.release()

    def test_outside_core_uses_original_and_context_restores(self):
        original = lockfile.LockFile._lock
        with patch.object(lockfile.LockFile, '_lock', return_value='original') as upstream:
            with package_locks():
                self.assertEqual(lockfile.LockFile(str(self.core.parent / 'foreign'))._lock(), 'original')
                upstream.assert_called_once()
            self.assertIs(lockfile.LockFile._lock, upstream)
        self.assertIs(lockfile.LockFile._lock, original)

    def test_bootstrap_does_not_adopt_another_users_inherited_store(self):
        source = ROOT / 'src/modules/bootstrap.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_get_safe_platformio_core_dir')
        namespace = {'Path': Path, 'os': os, '_short_platformio_core_alias': lambda path: str(path)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        foreign = self.core / 'other-user/core'
        installation = self.core / 'new-installation'
        with patch.dict(os.environ, {'PLATFORMIO_CORE_DIR': str(foreign)}):
            selected = namespace['_get_safe_platformio_core_dir'](installation)
        self.assertEqual(Path(selected), installation / 'src/.platformio-mcu-gui')
        self.assertFalse(foreign.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
