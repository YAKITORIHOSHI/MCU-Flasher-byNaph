#!/usr/bin/env python3
"""Disposable per-user application installations; never install a runtime or menu live."""
from __future__ import annotations

import os
import contextlib
import io
import json
import shutil
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import user_install as install


class UserInstallChecks(unittest.TestCase):
    def test_checkout_inventory_discovers_future_tracked_folders_and_omits_untracked_sketches(self):
        import subprocess
        future = self.source / 'FutureAddon/module.py'
        future.parent.mkdir()
        future.write_text('public source')
        private = self.source / 'UserSketch/Private.ino'
        private.parent.mkdir()
        private.write_text('private source')
        subprocess.run(['git', 'init', '-q', str(self.source)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.source), 'add', '--', *install.REQUIRED, 'FutureAddon/module.py'], check=True, capture_output=True)
        names = {path.as_posix() for path in install._inventory(self.source)[0]}
        self.assertIn('FutureAddon/module.py', names)
        self.assertNotIn('UserSketch/Private.ino', names)

    def test_release_inventory_copies_lite_and_future_public_folders_without_user_data(self):
        from src.modules.application_sources import MANIFEST, manifest_bytes
        additions = ('LITE-MODE/UBUNTU', 'LITE-MODE/WINDOWS.exe', 'FutureAddon/module.py')
        for name in additions:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('public fixture')
        (self.source / 'LITE-MODE/UBUNTU').chmod(0o755)
        names = list(install.REQUIRED) + list(additions)
        (self.source / MANIFEST).write_bytes(manifest_bytes('a' * 40, '', names))
        private = self.source / 'UnrelatedSketch/Private.ino'
        private.parent.mkdir()
        private.write_text('private sketch')
        destination = self.stage()
        for name in additions:
            self.assertEqual((destination / name).read_text(), 'public fixture')
        self.assertTrue((destination / 'LITE-MODE/UBUNTU').stat().st_mode & 0o111)
        self.assertFalse((destination / 'UnrelatedSketch').exists())

    def test_public_inventory_rejects_paths_outside_source_or_private_state(self):
        from src.modules.application_sources import MANIFEST
        for name in ('../escape', '/absolute', '.', 'LITE-MODE/.venv/private', 'src/_python/python.exe'):
            (self.source / MANIFEST).write_text(json.dumps({'schema': 1, 'commit': 'a' * 40, 'files': [name]}))
            with self.subTest(path=name), self.assertRaises(RuntimeError):
                self.stage()

    def test_legacy_zip_includes_lite_but_never_its_private_or_build_folders(self):
        for name in ('LITE-MODE/UBUNTU', 'LITE-MODE/target/object.o', 'LITE-MODE/.tools/compiler',
                     'LITE-MODE/.venv/private', 'LITE-MODE/evidence/audit.json'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture')
        destination = self.stage()
        self.assertTrue((destination / 'LITE-MODE/UBUNTU').is_file())
        self.assertEqual([p.name for p in (destination / 'LITE-MODE').iterdir()], ['UBUNTU'])

    def setUp(self):
        base = ROOT / 'temp/audit/user-install'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.data = self.root / 'Source Ω with spaces', self.root / 'user data'
        self.source.mkdir()
        for name in install.REQUIRED:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('application source: ' + name).encode())
        (self.source / 'direct/ubuntu/run.sh').chmod(0o755)

    def stage(self):
        return install.ensure_user_installation(self.source, data_home=self.data, report=lambda _text: None)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX first-launch fixture isolation')
    def test_reused_first_launch_fixture_keeps_its_owned_home_without_launch_environment(self):
        from direct.verify_ubuntu_first_run import _home_isolation
        env = dict(os.environ)
        env.pop('MCU_FIRST_RUN_FIXTURE', None)
        env.pop('MCU_FIRST_RUN_ALLOW_SMALL_RUNNER', None)
        source = _home_isolation(self.root) + '\nprint(Path.home())\nprint(os.path.expanduser("~/probe"))\n'
        result = subprocess.run([sys.executable, '-I', '-B', '-c', source], env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=5, check=True)
        home = self.root / 'sandbox-home'
        self.assertEqual(result.stdout.splitlines(), [str(home), str(home / 'probe')])
        self.assertTrue(home.is_dir())

    def test_copies_verified_sources_preserves_modes_and_omits_projects_settings_and_runtimes(self):
        for name in ('User.ino', 'NOTE.txt', 'AGENTS.md', '.mcu_flasher_build_cache/.mcu_ai_edits/keep.txt',
                     'src/dbs/bootstrap_config.json', 'src/gui_config.json', 'src/_python/private.exe',
                     'src/env/state.json', '.venv-linux/bin/python'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private content to preserve')
        destination = self.stage()
        self.assertEqual(destination, self.data / 'mcu-flasher/application')
        self.assertTrue(install.is_owned_installation(destination))
        for name in install.REQUIRED:
            self.assertEqual((destination / name).read_bytes(), (self.source / name).read_bytes())
        for name in ('User.ino', 'NOTE.txt', 'AGENTS.md', '.mcu_flasher_build_cache', 'src/dbs/bootstrap_config.json',
                     'src/gui_config.json', 'src/_python', 'src/env', '.venv-linux'):
            self.assertFalse((destination / name).exists(), name)
        self.assertFalse((self.data / 'applications/mcu-flasher.desktop').exists())
        if os.name != 'nt':
            self.assertEqual((destination / 'direct/ubuntu/run.sh').stat().st_mode & 0o777, 0o755)

    @unittest.skipIf(shutil.which('git') is None, 'Git is needed to create the checkout fixture')
    def test_a_checkout_installation_records_its_actual_commit_not_the_tracked_record(self):
        record = self.source / install.REVISION_RECORD
        record.parent.mkdir(parents=True, exist_ok=True)
        stale = '{"commit": "7710ca71197071002fdd24219c42b99ded36c610", "timestamp": "2026-11-09T02:23:07Z"}'
        record.write_text(stale)
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture@invalid',
                           GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture@invalid',
                           GIT_AUTHOR_DATE='2026-11-11T01:02:03Z', GIT_COMMITTER_DATE='2026-11-11T01:02:03Z')
        for command in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'fixture']):
            subprocess.run(['git', '-C', str(self.source), *command], check=True, env=environment,
                           capture_output=True, timeout=30)
        head = subprocess.run(['git', '-C', str(self.source), 'rev-parse', 'HEAD'], check=True, env=environment,
                              capture_output=True, text=True, timeout=30).stdout.strip()
        destination = self.stage()
        written = json.loads((destination / install.REVISION_RECORD).read_text())
        self.assertEqual(written, {'repository': 'YAKITORIHOSHI/MCU-Flasher-byNaph', 'commit': head,
                                   'committed_at': '2026-11-11T01:02:03Z'})
        self.assertEqual(record.read_text(), stale, 'the checkout itself is never modified')
        self.assertFalse((destination / '.git').exists())

    def test_a_release_folder_keeps_its_own_record(self):
        record = self.source / install.REVISION_RECORD
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"commit": "' + '1' * 40 + '"}')
        destination = self.stage()
        self.assertEqual((destination / install.REVISION_RECORD).read_text(), record.read_text())

    def test_menu_registration_is_one_explicit_post_bootstrap_step_and_is_idempotent(self):
        destination = self.stage()
        self.assertTrue(install.register_completed_installation(destination, data_home=self.data, report=lambda _text: None))
        entry = self.data / 'applications/mcu-flasher.desktop'
        before = entry.stat().st_mtime_ns
        text = entry.read_text()
        from direct.ubuntu.install_desktop import exec_argument
        self.assertIn(exec_argument(str(destination / 'direct/ubuntu/run.sh')), text)
        self.assertNotIn(str(self.source), text)
        self.assertEqual((self.data / 'icons/hicolor/256x256/apps/mcu-flasher.png').read_bytes(),
                         (self.source / 'src/assets/mcu_icon.png').read_bytes())
        self.assertTrue(install.register_completed_installation(destination, data_home=self.data, report=lambda _text: None))
        self.assertEqual(entry.stat().st_mtime_ns, before)
        self.assertEqual(len(list((self.data / 'applications').glob('*.desktop'))), 1)

    def test_lite_joins_all_apps_after_setup_and_on_later_launches(self):
        from direct.ubuntu.install_desktop import LITE_ENTRY, exec_argument
        destination = self.stage()
        lite = destination / 'LITE-MODE'
        (lite / 'assets').mkdir(parents=True)
        (lite / 'run.sh').write_text('#!/usr/bin/env bash\n')
        (lite / 'assets/lite.png').write_bytes(b'lite icon')
        entry = self.data / 'applications' / LITE_ENTRY
        theme = self.data / 'icons/hicolor'
        theme.mkdir(parents=True)
        os.utime(theme, (1, 1))
        # Never before the main entry exists: that marks a completed setup.
        self.assertFalse(install.refresh_registered_lite(destination, data_home=self.data))
        self.assertFalse(entry.exists())
        self.assertTrue(install.register_completed_installation(destination, data_home=self.data, report=lambda _text: None))
        text = entry.read_text()
        self.assertIn('Name=MCU Flasher Lite', text)
        self.assertIn(exec_argument(str(lite / 'run.sh')), text)
        self.assertIn('StartupWMClass=dev.naph.mcuflasher.lite', text)
        self.assertEqual((self.data / 'icons/hicolor/256x256/apps/mcu-flasher-lite.png').read_bytes(), b'lite icon')
        self.assertNotEqual(theme.stat().st_mtime, 1, 'A new icon bumps the theme so GNOME shows it without logging out')
        os.utime(theme, (1, 1))
        # An installation registered before Lite had an entry gains it on launch, once.
        entry.unlink()
        self.assertTrue(install.refresh_registered_lite(destination, data_home=self.data))
        before = entry.stat().st_mtime_ns
        self.assertTrue(install.refresh_registered_lite(destination, data_home=self.data))
        self.assertEqual(entry.stat().st_mtime_ns, before)
        self.assertEqual(theme.stat().st_mtime, 1, 'Unchanged icons leave the theme timestamp alone')
        self.assertFalse(install.refresh_registered_lite(self.source, data_home=self.data),
                         'A portable folder never registers itself')
        self.assertEqual(sorted(path.name for path in (self.data / 'applications').glob('*.desktop')),
                         sorted([LITE_ENTRY, 'mcu-flasher.desktop']))

    def test_repeated_portable_setup_reuses_installed_copy_without_downgrading_or_copying_venv(self):
        destination = self.stage()
        installed = destination / 'main/web_bridge.py'
        installed.write_text('newer source from the forward-only updater')
        runtime = destination / '.venv-linux/pyvenv.cfg'
        runtime.parent.mkdir()
        runtime.write_text('owned runtime at its final path')
        self.assertEqual(self.stage(), destination)
        self.assertEqual(installed.read_text(), 'newer source from the forward-only updater')
        self.assertEqual(runtime.read_text(), 'owned runtime at its final path')

    def test_unrecognized_destination_and_redirected_inputs_are_preserved(self):
        destination = install.installation_directory(self.data)
        destination.mkdir(parents=True)
        unknown = destination / 'keep.txt'
        unknown.write_text('unrelated')
        with self.assertRaisesRegex(RuntimeError, 'unrecognized'):
            self.stage()
        self.assertEqual(unknown.read_text(), 'unrelated')

    def test_a_same_size_source_change_fails_verification_and_retains_stage_without_menu(self):
        original_copy = install.shutil.copy2
        selected = self.source / 'main/web_bridge.py'
        def changing_copy(source, destination):
            result = original_copy(source, destination)
            if source == selected:
                before = source.stat()
                data = source.read_bytes()
                source.write_bytes(b'X' + data[1:])
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result
        with patch.object(install.shutil, 'copy2', side_effect=changing_copy):
            with self.assertRaisesRegex(RuntimeError, 'retained'):
                self.stage()
        self.assertFalse(install.installation_directory(self.data).exists())
        self.assertTrue(list((self.data / 'mcu-flasher').glob('.application-stage-*')))
        self.assertFalse((self.data / 'applications/mcu-flasher.desktop').exists())

    def test_bad_xdg_path_never_starts_an_installation(self):
        with self.assertRaisesRegex(RuntimeError, 'absolute'):
            install.ensure_user_installation(self.source, data_home='relative-user-data')

    def test_private_preferences_survive_installation_without_copying_accounts(self):
        import json
        settings = self.source / 'src/dbs/bootstrap_config.json'
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({'skip_updates':False, 'source_updates':{'enabled':False}}))
        account = self.source / 'src/dbs/.owner_tickets.json'
        account.write_text('{"private account data":true}')
        destination = self.stage()
        self.assertEqual(json.loads((destination / 'src/dbs/bootstrap_config.json').read_text()),
                         {'skip_updates':False, 'source_updates':{'enabled':False}})
        self.assertFalse((destination / 'src/dbs/.owner_tickets.json').exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Native Linux setup policy; staging checks run on every host')
    def test_setup_registers_the_menu_only_after_all_runtime_and_board_checks_succeed(self):
        from direct.ubuntu import setup
        for failure in (True, False):
            events = []
            def run(command, **options):
                events.append('check')
                if failure and 'offline_bootstrap.py' in ' '.join(map(str,command)):
                    raise RuntimeError('isolated board validation failed')
            with contextlib.ExitStack() as stack:
                patches = (
                    patch('src.modules.ubuntu_release.enforce_supported_ubuntu',return_value=True),
                    patch('src.modules.runtime_resources.enforce_minimum_cpu_requirement',return_value=True),
                    patch.object(setup.os,'geteuid',return_value=1000,create=True),
                    patch('src.modules.ubuntu_release.linux_label',return_value='Ubuntu'),
                    patch('direct.ubuntu.system_setup.ensure_system_dependencies'),
                    patch.object(install,'persistent_setup_root',return_value=self.source),
                    patch.object(install,'register_completed_installation',side_effect=lambda *_args: events.append('menu')),
                    patch.object(setup,'ROOT',self.source), patch.object(setup,'ENV_DIR',self.source/'.venv-linux'),
                    patch.object(setup,'prepare_environment_directory'), patch.object(setup.venv,'EnvBuilder'),
                    patch('src.modules.ubuntu_architecture.host_architecture',return_value='amd64'),
                    patch('direct.ubuntu.runtime_architecture.needs_architecture_repair',return_value=False),
                    patch('direct.ubuntu.runtime_architecture.record_runtime_architecture'),
                    patch.object(setup.subprocess,'run',side_effect=run),
                    patch('direct.ubuntu.arduino_cli.ensure_arduino_cli'),
                    patch('direct.ubuntu.opencode_setup.ensure_opencode_cli'),
                    patch('src.modules.platform_runtime.native_platformio_dir',return_value=self.root/'core'),
                    patch('src.modules.offline_bootstrap.ready',return_value=True),
                    patch('src.modules.offline_bootstrap.load_plan',return_value={}),
                    patch('src.modules.offline_mode.finish_bootstrap'),
                    patch('src.modules.package_jobs.package_store_lease',return_value=contextlib.nullcontext()),
                    patch('src.modules.default_board_folders.prepare_default_board_folders',side_effect=lambda *_args,**_kw: events.append('defaults')),
                    patch.dict(os.environ,{'MCU_FLASHER_OFFLINE_RUNTIME':'','MCU_FLASHER_WORKSPACE_RUNTIME':''}),
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
                )
                for item in patches: stack.enter_context(item)
                self.assertEqual(setup.main([]),1 if failure else 0)
            self.assertEqual('menu' in events,not failure)
            if not failure: self.assertEqual(events[-2:],['defaults','menu'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
