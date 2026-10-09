#!/usr/bin/env python3
"""Hardware-free Ubuntu cleanup safety fixtures; never inspect live resources."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ubuntu_maintenance_fixture", ROOT / "cleaner/ubuntu/maintenance.py")
maintenance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = maintenance
SPEC.loader.exec_module(maintenance)


class UbuntuMaintenanceTests(unittest.TestCase):
    def setUp(self):
        sandbox = ROOT / "temp"
        sandbox.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="ubuntu-maintenance-", dir=sandbox)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "application"
        self.root.mkdir()
        self.write("mcu_flash_gui.py", "# fixture entry")
        self.write("direct/ubuntu/run.sh", "# fixture launcher")
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()

    def write(self, relative, content="fixture"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def venv(self):
        self.write(".venv-linux/pyvenv.cfg", "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12.1\n")
        self.write(".venv-linux/bin/pip", "fixture launcher")
        self.write(".venv-linux/lib/python3.12/site-packages/fixture.py")
        return self.root / ".venv-linux"

    def tool(self, name):
        directory = self.root / ".ubuntu-tools" / name
        directory.mkdir(parents=True)
        executable = directory / name
        executable.write_bytes(b"\x7fELFfixture")
        info = executable.stat()
        receipt = {"owner": "mcu-flasher-ubuntu-" + name, "architecture": "linux-amd64",
                   "executable": name, "executable_sha256": "a" * 64, "schema": 1,
                   "size": info.st_size, "mtime_ns": info.st_mtime_ns, "binary_sha256": "b" * 64}
        (directory / "installation.json").write_text(json.dumps(receipt), encoding="utf-8")
        return directory

    def extras(self):
        directory = self.root / "src/offline-extras/linux"
        directory.mkdir(parents=True)
        owner = {"schema": 1, "installation": str(self.root).casefold(), "host": "linux",
                 "purpose": "MCU Flasher offline preparation extras"}
        (directory / ".mcu-offline-extras.json").write_text(json.dumps(owner), encoding="utf-8")
        (directory / "readiness.json").write_text("{}", encoding="utf-8")
        return directory

    def core(self, arch="x86_64"):
        base = self.home / "data"
        core = base / "mcu-flasher/platformio" / arch
        core.mkdir(parents=True)
        (core / ".mcu-offline-ready.json").write_text(json.dumps(
            {"schema": 4, "host": "linux", "architecture": arch,
             "plan": "a" * 64, "files": ["packages/fixture"]}), encoding="utf-8")
        (core / "packages").mkdir()
        (core / "packages/fixture").write_text("native package", encoding="utf-8")
        return base, core

    def native_plan(self, base):
        return self.plan("runtime", include_native_store=True,
                         environment={"XDG_DATA_HOME": str(base)}, architecture="x86_64")

    def fcntl_fixture(self, effect=None):
        return SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=Mock(side_effect=effect))

    def plan(self, mode="bytecode", **kwargs):
        return maintenance.build_plan(self.root, mode, **kwargs)

    def apply(self, plan):
        with patch.object(maintenance.sys, "platform", "linux"):
            return maintenance.apply_plan(self.root, plan, proc_root=self.proc)

    def test_source_bytecode_is_scoped_and_protected_data_survives(self):
        targets = [self.write("main/__pycache__/app.cpython-312.pyc"),
                   self.write("src/modules/fixture.pyc"), self.write("root.pyc")]
        protected = [self.write(".mcu_flasher_build_cache/__pycache__/keep.pyc"),
                     self.write(".mcu_ai_edits/session/keep.pyc"),
                     self.write("main/.pio/keep.pyc"), self.write("src/_python/keep.pyc"),
                     self.write("src/dbs/bootstrap_config.json", "preserved settings"),
                     self.write("src/gui_config.json"), self.write("logs/session.txt"),
                     self.write("Sketch.ino"), self.write("extra_user_files/keep.pyc")]
        self.apply(self.plan())
        self.assertTrue(all(not path.exists() for path in targets))
        self.assertTrue(all(path.exists() for path in protected))

    def test_fresh_removes_only_owned_venv_and_source_bytecode(self):
        venv = self.venv()
        windows = [self.write("env/pyvenv.cfg"), self.write("src/env/pyvenv.cfg"),
                   self.write("src/.platformio-mcu-gui/packages/keep"),
                   self.write("src/offline-extras/win32/readiness.json")]
        self.apply(self.plan("fresh"))
        self.assertFalse(venv.exists())
        self.assertTrue(all(path.exists() for path in windows))

    def test_runtime_receipts_and_linux_extras_are_removed(self):
        arduino, opencode, extras = self.tool("arduino-cli"), self.tool("opencode"), self.extras()
        self.apply(self.plan("runtime"))
        self.assertFalse(arduino.exists())
        self.assertFalse(opencode.exists())
        self.assertFalse(extras.exists())

    def test_native_store_requires_explicit_flag_and_current_architecture(self):
        base, current = self.core()
        other = base / "mcu-flasher/platformio/aarch64"
        other.mkdir()
        (other / "keep").write_text("other architecture", encoding="utf-8")
        options = {"environment": {"XDG_DATA_HOME": str(base)}, "home": self.home, "architecture": "x86_64"}
        self.assertEqual(self.plan("runtime", **options), [])
        self.apply(self.plan("runtime", include_native_store=True, **options))
        self.assertFalse(current.exists())
        self.assertTrue((other / "keep").exists())

    def test_foreign_native_store_certificate_is_refused(self):
        base, core = self.core()
        marker = core / ".mcu-offline-ready.json"
        state = json.loads(marker.read_text())
        state["host"] = "win32"
        marker.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "matching Linux"):
            self.plan("runtime", include_native_store=True, environment={"XDG_DATA_HOME": str(base)}, architecture="x86_64")
        self.assertTrue(core.exists())

    def test_known_native_target_metadata_and_stale_locks_are_supported(self):
        base, core = self.core()
        (core / ".mcu-index-targets.json").write_text('{"schema": 1, "host": "linux", "rows": []}', encoding="utf-8")
        for name in maintenance.CORE_LOCK_NAMES:
            (core / name).write_text("stale lock fixture", encoding="utf-8")
        lock_api = self.fcntl_fixture()
        original_open = maintenance.os.open
        with patch.dict(sys.modules, {"fcntl": lock_api}), patch.object(
                maintenance.os, "open", wraps=original_open) as opened:
            self.apply(self.native_plan(base))
        self.assertFalse(core.exists())
        self.assertEqual(lock_api.flock.call_count, 20)
        self.assertTrue(all(not call.args[1] & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
                            for call in opened.call_args_list))

    def test_held_native_lock_blocks_before_any_deletion(self):
        base, core = self.core()
        lock = core / "packages.lock"
        lock.write_text("held lock fixture", encoding="utf-8")
        bytecode = self.write("main/fixture.pyc")
        with patch.dict(sys.modules, {"fcntl": self.fcntl_fixture(BlockingIOError("held"))}):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "lock is held"):
                self.apply(self.native_plan(base))
        self.assertTrue(bytecode.exists())
        self.assertEqual(lock.read_text(), "held lock fixture")

    def test_native_lock_is_revalidated_after_tree_scan(self):
        base, core = self.core()
        (core / "packages.lock").write_text("stale lock", encoding="utf-8")
        bytecode = self.write("main/fixture.pyc")
        exclusive = []
        def lock_changed(descriptor, operation):
            if operation & 2:
                exclusive.append(descriptor)
                if len(exclusive) == 2:
                    raise BlockingIOError("became held after scan")
        with patch.dict(sys.modules, {"fcntl": self.fcntl_fixture(lock_changed)}):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "lock is held"):
                self.apply(self.native_plan(base))
        self.assertTrue(bytecode.exists())
        self.assertTrue(core.exists())

    def test_unknown_native_lock_names_are_retained(self):
        base, core = self.core()
        unknown = core / "my-own-data.lock"
        unknown.write_text("user-owned fixture", encoding="utf-8")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "Unrecognized"):
            self.native_plan(base)
        self.assertTrue(unknown.exists())

    def test_unknown_venv_content_blocks_every_mutation(self):
        self.venv()
        bytecode = self.write("main/fixture.pyc")
        self.write(".venv-linux/user_notes.txt")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "Unrecognized"):
            self.plan("fresh")
        self.assertTrue(bytecode.exists())

    def test_bad_tools_and_foreign_extras_are_retained(self):
        self.tool("arduino-cli")
        self.write(".ubuntu-tools/arduino-cli/user_notes.txt")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "Unrecognized"):
            self.plan("runtime")
        self.write(".ubuntu-tools/arduino-cli/installation.json", "{}")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "ownership"):
            self.plan("runtime")

    def test_unknown_pycache_content_is_retained(self):
        self.write("main/__pycache__/user_notes.txt")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "Unrecognized bytecode"):
            self.plan()

    def test_native_host_guard_refuses_windows_mutation(self):
        bytecode = self.write("main/fixture.pyc")
        with patch.object(maintenance.sys, "platform", "win32"):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "native Linux"):
                maintenance.apply_plan(self.root, self.plan(), proc_root=self.proc)
        self.assertTrue(bytecode.exists())

    def test_maintenance_cannot_delete_its_own_interpreter(self):
        self.venv()
        with patch.object(maintenance.sys, "platform", "linux"), patch.object(
                maintenance.sys, "executable", str(self.root / ".venv-linux/bin/python")):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "system Python"):
                maintenance.apply_plan(self.root, self.plan("fresh"), proc_root=self.proc)

    def test_app_and_package_processes_refuse_mutation_without_killing(self):
        bytecode = self.write("main/fixture.pyc")
        proc = self.proc / "9999999"
        proc.mkdir()
        (proc / "cmdline").write_bytes((str(self.root / ".venv-linux/bin/python") + "\0-m\0platformio\0").encode())
        with patch.object(maintenance.os, "readlink", return_value="/usr/bin/python3"):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "Close MCU Flasher"):
                self.apply(self.plan())
        self.assertTrue(bytecode.exists())
        self.assertTrue(proc.exists())

    def test_unrelated_similarly_named_checkout_does_not_block(self):
        bytecode = self.write("main/fixture.pyc")
        proc = self.proc / "9999999"
        proc.mkdir()
        (proc / "cmdline").write_bytes((str(self.root) + "-other/mcu_flash_gui.py\0").encode())
        with patch.object(maintenance.os, "readlink", return_value="/usr/bin/python3"):
            self.apply(self.plan())
        self.assertFalse(bytecode.exists())

    def fake_shared_process(self, environment):
        proc = self.proc / "9999999"
        proc.mkdir()
        (proc / "cmdline").write_bytes(b"/usr/bin/python3\0/home/other-checkout/mcu_flash_gui.py\0")
        (proc / "environ").write_bytes(environment)
        return proc

    def test_shared_core_environment_blocks_other_checkouts(self):
        base, core = self.core()
        bytecode = self.write("main/fixture.pyc")
        self.fake_shared_process(("PLATFORMIO_CORE_DIR=" + str(core) + "\0").encode())
        with patch.object(maintenance.os, "readlink", return_value="/usr/bin/python3"):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "Close MCU Flasher"):
                self.apply(self.native_plan(base))
        self.assertTrue(bytecode.exists())
        self.assertTrue(core.exists())

    def test_shared_package_directory_environment_blocks_package_workers(self):
        base, core = self.core()
        self.fake_shared_process(("PLATFORMIO_PACKAGES_DIR=" + str(core / "packages") + "\0").encode())
        with patch.object(maintenance.os, "readlink", return_value="/usr/bin/python3"):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "Close MCU Flasher"):
                self.apply(self.native_plan(base))
        self.assertTrue(core.exists())

    def test_similarly_named_shared_store_environment_does_not_block(self):
        base, core = self.core()
        self.fake_shared_process(("PLATFORMIO_CORE_DIR=" + str(core) + "-other\0").encode())
        with patch.object(maintenance.os, "readlink", return_value="/usr/bin/python3"):
            self.apply(self.native_plan(base))
        self.assertFalse(core.exists())

    def test_shared_process_environment_inspection_is_bounded(self):
        base, core = self.core()
        self.fake_shared_process(b"X=" + b"x" * 262145)
        with patch.object(maintenance.os, "readlink", return_value="/usr/bin/python3"):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "environment exceeds"):
                self.apply(self.native_plan(base))
        self.assertTrue(core.exists())

    def test_missing_process_inventory_blocks_cleanup(self):
        bytecode = self.write("main/fixture.pyc")
        with patch.object(maintenance.sys, "platform", "linux"):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "inventory is unavailable"):
                maintenance.apply_plan(self.root, self.plan(), proc_root=self.base / "missing")
        self.assertTrue(bytecode.exists())

    def test_new_unknown_content_after_preview_is_retained(self):
        self.write("main/__pycache__/fixture.pyc")
        plan = self.plan()
        unknown = self.write("main/__pycache__/notes.txt")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "Unrecognized"):
            self.apply(plan)
        self.assertTrue(unknown.exists())

    def test_changed_ownership_after_preview_blocks_every_mutation(self):
        self.venv()
        bytecode = self.write("main/fixture.pyc")
        plan = self.plan("fresh")
        self.write(".venv-linux/pyvenv.cfg", "user-authored material")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "isolated Python"):
            self.apply(plan)
        self.assertTrue(bytecode.exists())

    def test_regular_file_named_pycache_is_retained(self):
        path = self.write("__pycache__", "user-owned material")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "Unrecognized bytecode cache"):
            self.plan()
        self.assertTrue(path.exists())

    def test_exact_confirmation_is_required_for_runtime_reset(self):
        self.venv()
        with patch.object(maintenance, "ROOT", self.root), patch("builtins.print"), patch.object(
                maintenance.sys, "platform", "linux"), patch("builtins.input", return_value="CLEAN MCU UBUNTU"):
            self.assertEqual(maintenance.main(["runtime", "--apply"]), 2)
        self.assertTrue((self.root / ".venv-linux").exists())

    def test_automation_acknowledgement_requires_apply(self):
        with patch.object(maintenance, "ROOT", self.root), patch("builtins.print"):
            self.assertEqual(maintenance.main(["fresh", "--yes"]), 1)

    def test_preview_and_cancel_do_not_write_settings(self):
        bytecode = self.write("main/fixture.pyc")
        config = self.write("src/dbs/bootstrap_config.json", '{"keep": true}')
        with patch.object(maintenance, "ROOT", self.root), patch("builtins.print"), patch.object(
                maintenance.sys, "platform", "linux"), patch("builtins.input", return_value="cancel"):
            self.assertEqual(maintenance.main(["fresh", "--preview"]), 0)
            self.assertEqual(maintenance.main(["fresh", "--apply"]), 2)
        self.assertTrue(bytecode.exists())
        self.assertEqual(config.read_text(), '{"keep": true}')

    def test_escaping_architecture_and_xdg_namespace_are_refused(self):
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.native_store({}, self.home, "../../windows")
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.native_store({"XDG_DATA_HOME": "relative-path"}, self.home, "x86_64")

    def test_deletion_failure_is_reported_and_keeps_remaining_files(self):
        bytecode = self.write("main/fixture.pyc")
        with patch.object(maintenance, "_delete_node", side_effect=PermissionError("fixture denial")):
            with self.assertRaises(PermissionError):
                self.apply(self.plan())
        self.assertTrue(bytecode.exists())

    def test_native_descriptor_deletion_unlinks_interpreter_link_only(self):
        path = self.root / ".venv-linux/bin/python"
        identity = (1, 2, 0o120777)
        info = SimpleNamespace(st_dev=1, st_ino=2, st_mode=0o120777)
        real_unlink = maintenance.os.unlink
        with patch.object(maintenance.os, "supports_dir_fd", {real_unlink}), patch.object(
                maintenance.os, "O_NOFOLLOW", 0x20000, create=True), patch.object(
                maintenance.os, "O_DIRECTORY", 0x10000, create=True), patch.object(
                maintenance.os, "open", side_effect=[10, 11, 12]) as opened, patch.object(
                maintenance.os, "stat", return_value=info), patch.object(
                maintenance.os, "unlink") as unlinked, patch.object(maintenance.os, "rmdir") as removed, patch.object(
                maintenance.os, "close"):
            # Keep the mocked function in the capability set, as Python exposes
            # function identities rather than names in supports_dir_fd.
            maintenance.os.supports_dir_fd.add(maintenance.os.unlink)
            maintenance._delete_node(path, identity, self.root)
        unlinked.assert_called_once_with("python", dir_fd=12)
        removed.assert_not_called()
        self.assertEqual(opened.call_count, 3)
        self.assertTrue(all(call.args[1] & 0x20000 for call in opened.call_args_list))

    def test_native_descriptor_parent_link_refuses_without_unlink(self):
        path = self.root / ".venv-linux/bin/python"
        with patch.object(maintenance.os, "supports_dir_fd", set()), patch.object(
                maintenance.os, "O_NOFOLLOW", 0x20000, create=True), patch.object(
                maintenance.os, "O_DIRECTORY", 0x10000, create=True), patch.object(
                maintenance.os, "open", side_effect=[10, OSError("parent became a link")]), patch.object(
                maintenance.os, "unlink") as unlinked, patch.object(maintenance.os, "close"):
            maintenance.os.supports_dir_fd.add(maintenance.os.unlink)
            with self.assertRaises(OSError):
                maintenance._delete_node(path, (1, 2, 0o120777), self.root)
        unlinked.assert_not_called()

    def test_bounded_scan_refuses_oversized_fixture(self):
        self.write("main/fixture.pyc")
        with patch.object(maintenance, "MAX_ENTRIES", 1), patch.object(
                maintenance, "Budget", side_effect=lambda *args, **kwargs: BudgetFixture()):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "limit"):
                self.plan()

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("Host does not permit isolated symlink creation.")

    def test_linked_venv_root_never_deletes_its_target(self):
        outside = self.base / "outside"
        outside.mkdir()
        keep = outside / "keep"
        keep.write_text("preserve", encoding="utf-8")
        self.symlink(self.root / ".venv-linux", outside, directory=True)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "Linked"):
            self.plan("fresh")
        self.assertTrue(keep.exists())

    def test_normal_interpreter_links_are_unlinked_without_following(self):
        venv = self.venv()
        interpreter = self.base / "python3.12"
        interpreter.write_text("system interpreter fixture", encoding="utf-8")
        self.symlink(venv / "bin/python", interpreter)
        self.symlink(venv / "lib64", venv / "lib", directory=True)
        self.apply(self.plan("fresh"))
        self.assertFalse(venv.exists())
        self.assertEqual(interpreter.read_text(), "system interpreter fixture")

    def test_linked_xdg_namespace_cannot_escape_to_other_resources(self):
        outside = self.base / "outside"
        outside.mkdir()
        alias = self.home / "data"
        self.symlink(alias, outside, directory=True)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "local namespace"):
            maintenance.native_store({"XDG_DATA_HOME": str(alias)}, self.home, "x86_64")

    def test_launch_wrappers_use_system_python_and_correct_mode(self):
        for relative, mode in (("cleaner/clean_fresh.sh", "fresh"),
                               ("cleaner/clean_pycache.sh", "bytecode"),
                               ("DANGER-ZONE/reset_ubuntu.sh", "runtime")):
            content = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("exec /usr/bin/python3 -B", content)
            self.assertIn(f'" {mode} "$@"', content)
            self.assertNotIn("sudo", content)


class BudgetFixture:
    def __init__(self):
        self.remaining = 1

    def check(self):
        self.remaining -= 1
        if self.remaining < 0:
            raise maintenance.MaintenanceError("Fixture maintenance limit reached")


if __name__ == "__main__":
    unittest.main(verbosity=2)
