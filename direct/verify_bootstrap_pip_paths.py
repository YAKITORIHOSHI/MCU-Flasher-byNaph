"""Hardware-free Python installer paths, with only scratch filesystem probes."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules.windows_tool_paths import pip_environment, python_install_path


class PipPathChecks(unittest.TestCase):
    def test_pip_temp_routes_only_child_environment_to_physical_folder(self):
        env = {'TEMP': str(ROOT / 'temp/audit/long install folder/temp'),
               'TMP': 'compiler-temp', 'MCU_FLASHER_PIP_TEMP_DIR': 'short-physical-folder',
               'PATH': 'fixture', 'MCU_FLASHER_STATE_DIR': 'fixture-state'}
        original = dict(env)
        with patch.object(sys, 'platform', 'win32'):
            result = pip_environment(env)
        self.assertEqual(env, original)
        self.assertEqual(result['TEMP'], 'short-physical-folder')
        self.assertEqual(result['TMP'], 'short-physical-folder')
        self.assertEqual(result['TMPDIR'], 'short-physical-folder')
        self.assertEqual(result['PATH'], 'fixture')
        self.assertEqual(result['MCU_FLASHER_STATE_DIR'], 'fixture-state')

    def test_unconfigured_environment_remains_unchanged(self):
        original = {'TEMP': r'\\server\share\temp'}
        with patch.object(sys, 'platform', 'win32'):
            self.assertEqual(pip_environment(original), original)

    def test_native_linux_paths_are_unchanged(self):
        env = {'TMP': '/fixture/tmp', 'PATH': '/usr/bin'}
        with patch.object(sys, 'platform', 'linux'):
            self.assertEqual(pip_environment(env), env)
            self.assertEqual(python_install_path('/fixture/env/bin/python'), Path('/fixture/env/bin/python'))

    @unittest.skipUnless(os.name == 'nt', 'Windows interpreter path identity')
    def test_short_interpreter_keeps_same_runtime_and_prefix(self):
        path = Path(sys.executable)
        spelling = python_install_path(path)
        self.assertTrue(spelling.samefile(path))
        original = subprocess.check_output([str(path), '-I', '-c', 'import sys;print(sys.version)'], text=True)
        alternate = subprocess.check_output([str(spelling), '-I', '-c', 'import sys;print(sys.version)'], text=True)
        self.assertEqual(original, alternate)

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical temporary paths')
    def test_wheel_backend_avoids_long_junction_expansion(self):
        directory = tempfile.mkdtemp(prefix='pip-path-', dir=ROOT / 'temp')
        long_directory = Path(directory) / ('download-folder-' * 4)
        long_directory.mkdir()
        original = {**os.environ, 'TEMP': str(long_directory), 'TMP': str(long_directory),
                    'MCU_FLASHER_PIP_TEMP_DIR': directory}
        env = pip_environment(original)
        code = ('import os,tempfile;from pathlib import Path;'
                'p=Path(os.path.realpath(tempfile.gettempdir()))/"pip-ephem-wheel-cache-12345678"/'
                '"wheels"/"f3"/"05"/"08"/("a"*64)/"tmpabcdefgh"/".tmp-abcdefgh";'
                'p.mkdir(parents=True);(p/"wheel.txt").write_text("wheel");'
                'assert (p/"wheel.txt").read_text()=="wheel";print(len(str(p)))')
        result = subprocess.run([str(python_install_path(sys.executable)), '-I', '-B', '-c', code],
                                env=env, capture_output=True, text=True, check=True)
        self.assertLess(int(result.stdout.strip()), 260)
        before = subprocess.run([str(python_install_path(sys.executable)), '-I', '-B', '-c', code],
                                env=original, capture_output=True, text=True)
        self.assertNotEqual(before.returncode, 0)
        self.assertRegex(before.stderr, r'WinError (3|206)')

    def test_base_qt_sync_never_launches_an_unverified_second_install(self):
        source = ROOT / 'src/modules/bootstrap.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == '_sync_private_python_site_packages')
        namespace = {'subprocess': type('Forbidden', (), {'run': lambda *args, **kwargs: self.fail('base install')})}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        self.assertIsNone(namespace[node.name]())


if __name__ == '__main__':
    unittest.main(verbosity=2)
