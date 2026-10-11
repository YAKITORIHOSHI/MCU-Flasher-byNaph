#!/usr/bin/env python3
"""Isolated host-runtime selection and native Windows relocation checks.

All writable fixtures stay in temp/. Never install Python, launch Bootstrap,
touch live settings, or open hardware from this verifier.
"""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import private_python_guard as guard


class PrivateRuntimeChecks(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "temp/audit/private-runtimes"
        parent.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.windows = self.root / "src/_python"
        self.env = self.root / "env"
        self.linux = self.root / ".venv-linux"
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        for name, value in (("_PROJECT_ROOT", self.root), ("PRIVATE_PYTHON_DIR", self.windows),
                            ("ENV_PYTHON_DIR", self.env), ("LINUX_ENV_DIR", self.linux)):
            self.scope.enter_context(patch.object(guard, name, value))

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture; never execute")
        return path

    def test_linux_identity_uses_only_its_native_environment(self):
        with patch.object(guard.sys, "platform", "linux"), patch.object(guard.sys, "base_prefix", "/usr"):
            for prefix, accepted in ((self.linux, True), (self.env, False), (self.windows, False)):
                with patch.object(guard.sys, "prefix", str(prefix)):
                    self.assertEqual(guard.is_running_private_python(), accepted)

    def test_windows_identity_rejects_linux_and_external_python(self):
        with patch.object(guard.sys, "platform", "win32"):
            for exe, accepted in ((self.windows / "python.exe", True),
                                  (self.env / "Scripts/python.exe", True),
                                  (self.linux / "bin/python", False),
                                  (self.root / "external/python.exe", False)):
                with patch.object(guard.sys, "executable", str(exe)):
                    self.assertEqual(guard.is_running_private_python(), accepted)

    def test_linux_does_not_borrow_windows_env_when_native_runtime_is_missing(self):
        self.write(self.env / "bin/python")
        self.write(self.windows / "python.exe")
        with patch.object(guard.sys, "platform", "linux"), patch.object(guard.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "bash direct/ubuntu/run.sh"):
                guard.get_private_python_exe(allow_repair=True)
            run.assert_not_called()

    def test_linux_selects_native_python_with_foreign_resources_present(self):
        native = self.write(self.linux / "bin/python")
        self.write(self.windows / "python.exe")
        self.write(self.env / "Scripts/python.exe")
        with patch.object(guard.sys, "platform", "linux"), \
                patch.object(guard.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(guard.get_private_python_exe(), native)
            self.assertEqual(run.call_args.args[0][0], str(native))
            self.assertIn("-I", run.call_args.args[0])

    def test_windows_missing_runtime_never_selects_linux(self):
        self.write(self.linux / "bin/python")
        with patch.object(guard.sys, "platform", "win32"), patch.object(guard.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "Windows private Python"):
                guard.get_private_python_exe()
            run.assert_not_called()

    def test_windows_checks_private_python_in_isolated_mode(self):
        private = self.write(self.windows / "python.exe")
        with patch.object(guard.sys, "platform", "win32"), \
                patch.object(guard.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(guard.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(guard.get_private_python_exe(), private)
            self.assertEqual(run.call_args.args[0][0], str(private))
            self.assertIn("-I", run.call_args.args[0])

    def test_installer_success_requires_a_working_windows_interpreter(self):
        installer = self.write(self.root / "installers/.handsoff/python-3.14.7-amd64.exe")
        with patch.object(guard.sys, "platform", "win32"), \
                patch.object(guard.subprocess, "CREATE_NO_WINDOW", 0, create=True):
            for probe_exit in (0, 1):
                with patch.object(guard.subprocess, "run", side_effect=[
                        SimpleNamespace(returncode=0), SimpleNamespace(returncode=probe_exit)]) as run:
                    self.assertEqual(guard._heal_private_runtime_if_needed(self.windows), probe_exit == 0)
                    command = run.call_args_list[0].args[0]
                    self.assertEqual(command[0], str(installer))
                    self.assertIn(f"TargetDir={self.windows}", command)
                    self.assertNotIn(str(self.linux), command)
                    self.assertEqual(run.call_args_list[1].args[0][0], str(self.windows / "python.exe"))

    @unittest.skipUnless(sys.platform == "win32", "Native Windows Script Host required")
    def test_native_windows_moved_venv_and_contaminated_environment(self):
        # Execute the production WSH environment setup and helper definitions,
        # with no app launch, CPU-policy bypass or live runtime installation.
        source = (ROOT / "direct/windows/run.vbs").read_text(encoding="utf-8-sig")
        environment_setup = source.split("' Bootstrap may", 1)[1].split("' Reject unsupported", 1)[0]
        environment_setup = "' Bootstrap may" + environment_setup
        helpers = []
        for name, kind in (("IsPythonExeValid", "Function"), ("RepairVenvInPlace", "Sub")):
            pattern = rf"^{kind} {name}\(.*?^End {kind}"
            helpers.append(re.search(pattern, source, re.M | re.S).group())
        clean = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"):
            clean.pop(name, None)
        subprocess.run([sys.executable, "-I", "-B", "-m", "venv", str(self.env)],
                       env=clean, check=True, timeout=90, capture_output=True)
        config = self.env / "pyvenv.cfg"
        text = config.read_text()
        text = re.sub(r"^home\s*=.*$", "home = Z:/missing-old-copy", text, flags=re.M)
        text = re.sub(r"^executable\s*=.*$", "executable = Z:/missing-old-copy/python.exe", text, flags=re.M)
        config.write_text(text)
        base = Path(sys.base_prefix) / "python.exe"
        env_python = self.env / "Scripts/python.exe"
        def literal(value):
            return '"' + str(value).replace('"', '""') + '"'
        script = self.root / "relocation.vbs"
        script.write_text('\n'.join([
            'Dim fso, shell',
            'Set fso = CreateObject("Scripting.FileSystemObject")',
            'Set shell = CreateObject("WScript.Shell")', environment_setup, *helpers,
            f'If Not IsPythonExeValid({literal(base)}) Then WScript.Quit 10',
            f'RepairVenvInPlace {literal(self.env)}, {literal(base)}',
            f'If Not IsPythonExeValid({literal(env_python)}) Then WScript.Quit 11',
            'WScript.Echo "Native Windows runtime and moved env: OK"',
        ]), encoding="utf-8")
        contaminated = dict(clean, PYTHONHOME=str(self.linux), PYTHONPATH=str(self.linux))
        result = subprocess.run(["cscript.exe", "//nologo", str(script)], env=contaminated,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(base.parent), config.read_text())
        print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
