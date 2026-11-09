"""CMD directory quoting/ownership; native shell and owned SMB fixtures on Windows."""
from __future__ import annotations
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.platforms import windows_terminal as terminal


class CmdDirectories(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/cmd-directory"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.stack.enter_context(patch.object(tempfile, "tempdir", str(self.root)))

    def directory(self, target):
        item = terminal.CmdProjectDirectory(target, "cmd.exe", {})
        self.addCleanup(item.close)
        return item

    def test_unc_path_is_environment_data_and_cmd_starts_locally(self):
        target = r"\\server\share\project %PATH% ! & ^ Unicode Ω"
        with patch.object(terminal, "_remote", return_value=None):
            item = self.directory(target)
        self.assertEqual(item.env["MCU_TERMINAL_CWD"], target)
        self.assertFalse(item.cwd.startswith("\\\\"))
        script = Path(item.argv[-1]).read_text()
        self.assertIn('pushd "%MCU_TERMINAL_CWD%"', script)
        self.assertIn("popd", script)
        self.assertNotIn(target, script)
        self.assertIn("/V:OFF", item.argv)

    def test_invalid_path_never_enters_a_command(self):
        for value in ('bad"path', "bad\npath", "bad\rpath", "bad\0path"):
            with self.assertRaises(ValueError):
                self.directory(value)

    def test_cleanup_releases_only_the_recorded_new_mapping_to_this_share(self):
        remote = self.stack.enter_context(patch.object(terminal, "_remote", return_value=None))
        cancel = Mock(return_value=0)
        self.stack.enter_context(patch.object(terminal.ctypes, "windll",
            SimpleNamespace(mpr=SimpleNamespace(WNetCancelConnection2W=cancel)), create=True))
        item = self.directory(r"\\server\share\project")
        item.record.write_text("Z:\n")
        remote.return_value = r"\\server\share"
        item.close()
        cancel.assert_called_once_with("Z:", 0, False)

    def test_cleanup_preserves_preexisting_and_reassigned_drives(self):
        remote = self.stack.enter_context(patch.object(terminal, "_remote", return_value=r"\\server\share"))
        cancel = Mock(return_value=0)
        self.stack.enter_context(patch.object(terminal.ctypes, "windll",
            SimpleNamespace(mpr=SimpleNamespace(WNetCancelConnection2W=cancel)), create=True))
        item = self.directory(r"\\server\share\project")
        item.record.write_text("Z:\n")
        item.close()
        remote.return_value = None
        other = self.directory(r"\\server\share\project")
        other.record.write_text("Y:\n")
        remote.return_value = r"\\other\share"
        other.close()
        cancel.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "native CMD")
    def test_native_cmd_keeps_literal_local_directory(self):
        target = self.root / "project %PATH% ! & Ω"
        target.mkdir()
        self.run_native(str(target))

    def run_native(self, target):
        executable = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        item = terminal.CmdProjectDirectory(target, executable, os.environ)
        try:
            result = subprocess.run(item.argv, cwd=item.cwd, env=item.env,
                input="echo MCU_CMD_DIRECTORY:%CD%\nexit\n", capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("UNC paths are not supported", result.stdout + result.stderr)
            self.assertIn("MCU_CMD_DIRECTORY:", result.stdout)
            if not target.startswith("\\\\"):
                self.assertIn(target, result.stdout)
            else:
                self.assertNotIn("Could not open the project", result.stdout)
                self.assertIn("MCU_SHARE_MARKER", result.stdout)
        finally:
            item.close()

    @unittest.skipUnless(sys.platform == "win32" and os.environ.get("CI") == "true", "owned Windows CI SMB share")
    def test_native_cmd_enters_and_releases_owned_unc_share(self):
        target = self.root / "network project"
        target.mkdir()
        share = "MCUFixture" + uuid.uuid4().hex[:12]
        created = subprocess.run(["net", "share", f"{share}={target}"], capture_output=True, timeout=15)
        if created.returncode:
            self.skipTest("Runner cannot create the owned SMB fixture")
        try:
            # A literal child command proves that CMD reads this share.
            (target / "MCU_SHARE_MARKER").write_text("fixture")
            executable = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
            item = terminal.CmdProjectDirectory(f"\\\\localhost\\{share}", executable, os.environ)
            try:
                result = subprocess.run(item.argv, cwd=item.cwd, env=item.env,
                    input="dir /b MCU_SHARE_MARKER\nexit\n", capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=0x08000000)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("MCU_SHARE_MARKER", result.stdout)
                self.assertNotIn("UNC paths are not supported", result.stdout + result.stderr)
                drive = item.record.read_text().strip()
                self.assertIsNone(terminal._remote(drive), "Normal CMD exit leaked its pushd drive")
            finally:
                item.close()
        finally:
            subprocess.run(["net", "share", share, "/delete", "/y"], capture_output=True, timeout=15, check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
