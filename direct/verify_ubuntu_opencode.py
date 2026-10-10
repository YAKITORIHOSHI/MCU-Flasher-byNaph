#!/usr/bin/env python3
"""OpenCode discovery contracts; POSIX filesystem checks require a native host."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main.platforms import ubuntu_opencode as helper


class UbuntuOpenCodeChecks(unittest.TestCase):
    def setUp(self):
        fixture_root = ROOT / "temp" / "audit" / "ubuntu-opencode"
        fixture_root.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(prefix="helpers-", dir=fixture_root)
        self.addCleanup(self.fixture.cleanup)
        self.home = Path(self.fixture.name) / "user home"
        self.home.mkdir()
        root = patch.object(helper, "ROOT", Path(self.fixture.name) / "application")
        root.start()
        self.addCleanup(root.stop)
        home = patch.object(helper.Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        path = patch.object(helper.shutil, "which", return_value=None)
        self.which = path.start()
        self.addCleanup(path.stop)
        self.permissions = {}
        if os.name == "nt":
            # Windows chmod does not implement executable bits. Model only
            # this POSIX contract; do not claim a native permission check.
            original_access = os.access

            def access(candidate, mode):
                permission = self.permissions.get(os.path.abspath(candidate))
                if mode == os.X_OK and permission is not None:
                    return permission
                return original_access(candidate, mode)

            access_patch = patch.object(helper.os, "access", side_effect=access)
            access_patch.start()
            self.addCleanup(access_patch.stop)

    def executable(self, relative, *, data=b"#!/bin/sh\nexit 0\n", executable=True):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755 if executable else 0o644)
        self.permissions[str(path.absolute())] = executable
        return path

    def test_path_installation_takes_precedence(self):
        selected = self.executable("system bin/opencode")
        self.executable(".opencode/bin/opencode")
        self.which.return_value = str(selected)
        self.assertEqual(helper.find_opencode_cli(), str(selected))
        self.which.assert_called_once_with("opencode")

    def test_desktop_user_installations_are_discovered_without_path(self):
        for directory in helper._USER_BIN_DIRS:
            with self.subTest(directory=directory):
                binary = self.executable(directory + "/opencode")
                self.assertEqual(helper.find_opencode_cli(), str(binary))
                binary.unlink()

    def test_user_directory_order_is_stable(self):
        local = self.executable(".local/bin/opencode")
        native = self.executable(".opencode/bin/opencode")
        self.assertEqual(helper.find_opencode_cli(), str(native))
        native.unlink()
        self.assertEqual(helper.find_opencode_cli(), str(local))

    def test_missing_nonexecutable_and_directory_candidates_are_rejected(self):
        self.assertIsNone(helper.find_opencode_cli())
        binary = self.executable(".opencode/bin/opencode", executable=False)
        self.which.return_value = str(binary)
        self.assertIsNone(helper.find_opencode_cli())
        binary.unlink()
        binary.mkdir()
        self.assertIsNone(helper.find_opencode_cli())

    def test_windows_names_and_renamed_pe_files_are_rejected(self):
        for suffix in (".exe", ".EXE", ".cmd", ".bat", ".ps1", ""):
            with self.subTest(suffix=suffix):
                payload = b"#!/bin/sh\nexit 0\n" if suffix else b"MZWindows fixture"
                binary = self.executable("foreign/opencode" + suffix, data=payload)
                self.which.return_value = str(binary)
                self.assertIsNone(helper.find_opencode_cli())
        native = self.executable(".local/bin/opencode")
        self.assertEqual(helper.find_opencode_cli(), str(native))

    @unittest.skipUnless(os.name == "posix", "Native POSIX symlink permissions")
    def test_symlink_invocation_preserved_and_windows_target_rejected(self):
        native = self.executable("package/bin/opencode-native")
        link = self.home / ".opencode/bin/opencode"
        link.parent.mkdir(parents=True)
        link.symlink_to(native)
        self.assertEqual(helper.find_opencode_cli(), str(link))
        link.unlink()
        foreign = self.executable("package/bin/opencode.exe")
        link.symlink_to(foreign)
        self.assertIsNone(helper.find_opencode_cli())

    @unittest.skipUnless(os.name == "posix", "Native POSIX symlink permissions")
    def test_broken_symlink_and_failed_lookup_are_safe(self):
        link = self.home / ".opencode/bin/opencode"
        link.parent.mkdir(parents=True)
        link.symlink_to(link.parent / "absent")
        self.which.side_effect = OSError("lookup unavailable")
        self.assertIsNone(helper.find_opencode_cli())

    def test_failed_path_lookup_retains_user_directory_fallback(self):
        self.which.side_effect = OSError("lookup unavailable")
        self.assertIsNone(helper.find_opencode_cli())
        binary = self.executable(".local/bin/opencode")
        self.assertEqual(helper.find_opencode_cli(), str(binary))

    def test_arguments_preserve_spaces_unicode_and_shell_metacharacters(self):
        executable = str(self.home / "bin $(echo secret)/opencode")
        project = self.home / "project '雪'; `touch stolen` $HOME"
        self.assertEqual(helper.opencode_argv(executable, project), [executable, str(project)])
        self.assertEqual(helper.opencode_argv(executable, "relative sketch"),
                         [executable, os.path.abspath("relative sketch")])
        for exe, target in (("", project), (executable, "")):
            with self.assertRaises(ValueError):
                helper.opencode_argv(exe, target)

    def test_unexpected_restarts_are_bounded_and_expire(self):
        policy = helper.OpenCodeRestartPolicy()
        self.assertTrue(policy.allow_restart(now=0))
        self.assertTrue(policy.allow_restart(now=1))
        self.assertFalse(policy.allow_restart(now=2))
        self.assertFalse(policy.allow_restart(now=59))
        self.assertTrue(policy.allow_restart(now=60))
        self.assertFalse(policy.allow_restart(now=60.5))
        self.assertTrue(policy.allow_restart(now=61))

    def test_explicit_exit_is_fresh_and_does_not_erase_failure_budget(self):
        policy = helper.OpenCodeRestartPolicy()
        self.assertTrue(policy.allow_restart(now=0))
        self.assertTrue(policy.allow_restart(now=1))
        for _ in range(100):
            self.assertTrue(policy.allow_restart(intentional=True, now=2))
        self.assertFalse(policy.allow_restart(now=3))
        self.assertLessEqual(len(policy._unexpected), 2)
        policy.reset()
        self.assertTrue(policy.allow_restart(now=4))

    def test_zero_restart_budget_and_invalid_limits(self):
        policy = helper.OpenCodeRestartPolicy(max_restarts=0)
        self.assertFalse(policy.allow_restart(now=0))
        self.assertTrue(policy.allow_restart(intentional=True, now=1))
        for limits in ((-1, 60), (2, 0), (2, -1)):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                helper.OpenCodeRestartPolicy(*limits)


if __name__ == "__main__":
    unittest.main(verbosity=2)
