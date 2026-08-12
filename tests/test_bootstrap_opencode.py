import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = PROJECT_ROOT / "src" / "modules" / "bootstrap.py"


def load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("bootstrap_under_test", BOOTSTRAP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BootstrapOpenCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_bootstrap_module()

    def test_stale_npm_shim_is_not_treated_as_usable_without_node(self):
        b = self.bootstrap
        with tempfile.TemporaryDirectory() as td:
            npm_dir = Path(td)
            (npm_dir / "npm.cmd").write_text("@echo off\n", encoding="utf-8")
            with (
                mock.patch.object(b.sys, "platform", "win32"),
                mock.patch.object(b, "_windows_node_dirs", return_value=[npm_dir]),
                mock.patch.object(b.shutil, "which", return_value=None),
                mock.patch.object(b, "_find_usable_node_cmd", return_value=None),
            ):
                self.assertIsNone(b._find_usable_npm_cmd())

    def test_opencode_install_installs_node_before_using_npm(self):
        b = self.bootstrap
        completed = subprocess.CompletedProcess(
            args=["npm.cmd", "install", "-g", "opencode-ai"],
            returncode=0,
            stdout="installed",
            stderr="",
        )

        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "check_opencode_cli", side_effect=[None, "C:\\Users\\tester\\AppData\\Roaming\\npm\\opencode.cmd", "C:\\Users\\tester\\AppData\\Roaming\\npm\\opencode.cmd"]),
            mock.patch.object(b, "_find_usable_npm_cmd", side_effect=[None, "C:\\Program Files\\nodejs\\npm.cmd", "C:\\Program Files\\nodejs\\npm.cmd"]),
            mock.patch.object(b, "_install_nodejs_lts_with_winget", return_value=True) as install_node,
            mock.patch.object(b.subprocess, "run", return_value=completed) as run,
            mock.patch.object(b, "section"),
            mock.patch.object(b, "status"),
            mock.patch.object(b, "ok"),
        ):
            self.assertTrue(b.ensure_opencode_cli())

        install_node.assert_called_once()
        # Verify npm install was called with non-interactive flags and DEVNULL stdin
        calls = [c for c in run.call_args_list if "install" in c.args[0]]
        self.assertTrue(len(calls) >= 1)
        self.assertEqual(calls[0].kwargs.get("stdin"), subprocess.DEVNULL)

    def test_node_lts_falls_back_to_direct_installer_when_winget_fails(self):
        b = self.bootstrap
        with (
            mock.patch.object(b, "_install_nodejs_lts_with_winget", return_value=False),
            mock.patch.object(b, "_install_nodejs_lts_direct", return_value=True) as direct_install,
            mock.patch.object(b, "status"),
        ):
            self.assertTrue(b._install_nodejs_lts())
            direct_install.assert_called_once()


if __name__ == "__main__":
    unittest.main()

