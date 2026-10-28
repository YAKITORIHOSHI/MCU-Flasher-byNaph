#!/usr/bin/env python3
"""Check release selection and flat ZIPs without live runtime or settings access."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.build_release import include_committed_source
from direct import build_release


class ReleaseSourceChecks(unittest.TestCase):
    def launcher_entries(self):
        return {name: ((ROOT / name).read_bytes(), 0o644 if name.endswith(('.exe', '.md')) else 0o755)
                for name in ('LAUNCHER/WINDOWS.exe', 'LAUNCHER/UBUNTU', 'LAUNCHER/UBUNTU_ARM64',
                             'LAUNCHER/MacOS.command', 'LAUNCHER/FEDORA', 'LAUNCHER/FEDORA_ARM64',
                             'LAUNCHER/LINUX_MINT', 'LAUNCHER/README.md')}

    def test_release_preserves_complete_launcher_folder_and_native_permissions(self):
        entries = self.launcher_entries()
        build_release.validate_launcher_entries(entries)
        for name in entries:
            with self.subTest(missing=name), self.assertRaisesRegex(RuntimeError, 'launcher'):
                build_release.validate_launcher_entries({key: value for key, value in entries.items() if key != name})
        for name in entries:
            if not name.endswith(('.exe', '.md')):
                with self.subTest(mode=name), self.assertRaisesRegex(RuntimeError, 'executable'):
                    build_release.validate_launcher_entries({**entries, name: (entries[name][0], 0o644)})
        for extra in ('LAUNCHER/UBUNTU.desktop', 'LAUNCHER/WINDOWS.vbs', 'LAUNCHER/unknown'):
            with self.subTest(extra=extra), self.assertRaisesRegex(RuntimeError, 'launcher'):
                build_release.validate_launcher_entries({**entries, extra: (b'legacy entry', 0o755)})

    def test_release_refuses_damaged_windows_executable(self):
        entries = self.launcher_entries()
        name = 'LAUNCHER/WINDOWS.exe'
        original = entries[name][0]
        offset = int.from_bytes(original[60:64], 'little')
        mutations = (b'', b'#!/bin/sh\n', b'MZ' + bytes(62), original[:64],
                     original[:offset] + b'NOPE' + original[offset + 4:],
                     original[:offset + 4] + b'\x64\x86' + original[offset + 6:],
                     original[:offset + 92] + b'\x03\x00' + original[offset + 94:])
        for content in mutations:
            with self.subTest(content=content[:12]), self.assertRaisesRegex(RuntimeError, '[Ll]auncher'):
                build_release.validate_launcher_entries({**entries, name: (content, 0o644)})

    def test_release_refuses_shell_substitutes_or_wrong_cpu_linux_executables(self):
        entries = self.launcher_entries()
        for name in ('LAUNCHER/UBUNTU', 'LAUNCHER/UBUNTU_ARM64', 'LAUNCHER/FEDORA',
                     'LAUNCHER/FEDORA_ARM64', 'LAUNCHER/LINUX_MINT'):
            original, mode = entries[name]
            wrong_cpu = b'\x3e\x00' if name.endswith('_ARM64') else b'\xb7\x00'
            mutations = (b'', b'#!/bin/bash\n', original[:32],
                         original[:4] + b'\x01' + original[5:],
                         original[:6] + b'\x00' + original[7:],
                         original[:16] + b'\x01\x00' + original[18:],
                         original[:18] + wrong_cpu + original[20:])
            for content in mutations:
                with self.subTest(name=name, content=content[:20]), self.assertRaisesRegex(RuntimeError, '[Ll]auncher'):
                    build_release.validate_launcher_entries({**entries, name: (content, mode)})

    def test_production_sources_and_tool_hook_are_included(self):
        for name in ('LAUNCHER/UBUNTU', 'main/web_bridge.py',
                     'src/modules/arduinoq_link_paths.py', 'src/dbs/bootstrap_config.json',
                     'installers/CH34x/CH341SER.exe', '.agents/skills/mcu-flash-gui-dev/SKILL.md'):
            with self.subTest(name=name):
                self.assertTrue(include_committed_source(name))

    def test_duplicate_root_launchers_are_absent_and_refused_by_packager(self):
        for name in ('MCU_Flasher', 'MCU_Flasher-arm64', 'MCU_Flasher.exe', 'MCU Flasher.desktop'):
            with self.subTest(name=name):
                self.assertFalse((ROOT / name).exists())
                with self.assertRaisesRegex(RuntimeError, 'Duplicate root launcher'):
                    include_committed_source(name)

    def test_only_top_level_developer_trees_are_omitted(self):
        for name in ('desktop/', 'desktop/crates/core/tests/project_service.rs',
                     'audit/migration/source-inventory.json'):
            with self.subTest(name=name):
                self.assertFalse(include_committed_source(name))
        self.assertTrue(include_committed_source('src/desktop/widget.py'))
        self.assertTrue(include_committed_source('direct/audit_notes.md'))

    def test_unsafe_paths_are_refused_even_in_omitted_trees(self):
        for name in ('/desktop/test.rs', '../desktop/test.rs', 'desktop/../main/app.py',
                     'audit/../../secret'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                include_committed_source(name)

    def test_accidental_personal_or_protected_state_still_fails(self):
        for name in ('main/tests/unexpected.py', 'temp/user.ino', 'src/__pycache__/app.pyc',
                     '.mcu_flasher_build_cache/project_state.json', 'src/gui_config.json',
                     'main/.owner_tickets.json', 'main/app.pyc'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                include_committed_source(name)

    def test_actual_committed_inventory_has_a_valid_python_release_selection(self):
        names = subprocess.check_output(
            ['git', 'ls-tree', '-rz', '--name-only', 'HEAD'], cwd=ROOT).decode().split('\0')
        included, omitted = [], []
        for name in filter(None, names):
            (included if include_committed_source(name) else omitted).append(name)
        self.assertIn('main/web_bridge.py', included)
        self.assertIn('src/modules/arduinoq_link_paths.py', included)
        self.assertIn('desktop/crates/core/tests/project_service.rs', omitted)
        self.assertIn('audit/migration/source-inventory.json', omitted)
        print(f'Committed source selection: {len(included)} included, {len(omitted)} developer files omitted.')


class ReleaseArchiveChecks(unittest.TestCase):
    """Build tiny releases from an owned repository, never the live runtime."""

    def setUp(self):
        scratch = ROOT / 'temp/audit'
        scratch.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix='release-sources-', dir=scratch)
        self.addCleanup(fixture.cleanup)
        self.fixture = Path(fixture.name)
        self.project = self.fixture / 'project with spaces'
        self.project.mkdir()
        self.output = self.fixture / 'release.zip'
        self.reports = self.fixture / 'reports'
        self.sources = {
            'README.md': b'Owned release fixture.\n',
            'main/gui.py': b'print("committed source")\n',
            'src/dbs/bootstrap_config.json': b'{"skip_updates": false}\n',
            'src/launch.vbs': b"' Windows fixture\n",
        }
        self.sources.update({name: (ROOT / name).read_bytes()
                             for name in build_release.REQUIRED_LAUNCHERS})
        self.installer = b'hydrated installer fixture\x00\x01\xff'
        self.installer_path = 'installers/fixture.exe'
        receipt = ('version https://git-lfs.github.com/spec/v1\n'
                   f'oid sha256:{hashlib.sha256(self.installer).hexdigest()}\n'
                   f'size {len(self.installer)}\n').encode()
        committed = {**self.sources, self.installer_path: receipt,
                     'desktop/private-preview.rs': b'// developer-only fixture\n',
                     'audit/private-report.json': b'{"fixture": true}\n'}
        for name, data in committed.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if name in build_release.REQUIRED_LAUNCHERS and not name.endswith(('.exe', '.md')):
                path.chmod(0o755)
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        self.git('add', '--force', '--', *committed)
        # Force Unix launcher modes even when this verifier runs on Windows.
        for name in build_release.REQUIRED_LAUNCHERS:
            if not name.endswith(('.exe', '.md')):
                self.git('update-index', '--chmod=+x', '--', name)
        hooks = self.fixture / 'empty-hooks'
        hooks.mkdir()
        self.git('-c', 'user.name=Release Fixture', '-c', 'user.email=fixture@localhost',
                 '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={hooks}',
                 'commit', '-qm', 'Owned release fixture')
        (self.project / self.installer_path).write_bytes(self.installer)
        # Local settings and sketches remain present but never enter git archive.
        (self.project / 'gui_config.json').write_bytes(b'private fixture settings')
        (self.project / 'user.ino').write_bytes(b'private fixture sketch')
        (self.project / 'main/gui.py').write_bytes(b'print("uncommitted edit")\n')
        header = bytearray(70)
        header[:2] = b'MZ'
        header[60:64] = (64).to_bytes(4, 'little')
        header[64:70] = b'PE\0\0\x64\x86'
        self.runtime = {name: b'private runtime fixture\n'
                        for name in build_release.REQUIRED_RUNTIME}
        for name in ('python.exe', 'pythonw.exe', 'python3.dll', 'python314.dll'):
            self.runtime[name] = bytes(header)
        self.runtime['Lib/site-packages/example/__init__.py'] = b'# installed package\n'
        self.runtime['Lib/site-packages/example/resources/data.bin'] = bytes(range(256))
        for name, data in self.runtime.items():
            path = self.project / 'src/_python' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def git(self, *arguments):
        return subprocess.check_output(['git', *arguments], cwd=self.project,
                                       stderr=subprocess.PIPE, timeout=30)

    def build(self):
        with patch.object(build_release, 'ROOT', self.project), redirect_stdout(io.StringIO()):
            return build_release.build(self.output, self.reports)

    def test_zip_has_project_files_at_root_and_exact_verified_manifest(self):
        manifest = self.build()
        expected = {**self.sources, self.installer_path: self.installer,
                    **{'src/_python/' + name: data for name, data in self.runtime.items()}}
        expected['src/launch.vbs'] = expected['src/launch.vbs'].replace(b'\n', b'\r\n')
        with zipfile.ZipFile(self.output) as bundle:
            self.assertEqual(set(bundle.namelist()), set(expected))
            self.assertIn('LAUNCHER/WINDOWS.exe', bundle.namelist())
            self.assertIn('README.md', bundle.namelist())
            for name, data in expected.items():
                with self.subTest(path=name):
                    self.assertEqual(bundle.read(name), data)
            launchers = {name: (bundle.read(name), bundle.getinfo(name).external_attr >> 16 & 0o777)
                         for name in build_release.REQUIRED_LAUNCHERS}
            build_release.validate_launcher_entries(launchers)
        self.assertEqual(manifest['archive_layout'], 'project-files-at-root')
        self.assertEqual(manifest['files'], len(expected))
        self.assertEqual(manifest['windows_private_runtime_files'], len(self.runtime))
        self.assertEqual(manifest['windows_private_runtime_bytes'], sum(map(len, self.runtime.values())))
        self.assertEqual(manifest['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(manifest['bytes'], self.output.stat().st_size)
        self.assertEqual({entry['path']: (entry['sha256'], entry['size']) for entry in manifest['entries']},
                         {name: (hashlib.sha256(data).hexdigest(), len(data))
                          for name, data in expected.items()})
        self.assertEqual(manifest['verified_lfs_installers'][0]['path'], self.installer_path)
        self.assertEqual(manifest['omitted_developer_roots']['desktop']['committed_files'], 1)
        self.assertEqual(manifest['omitted_developer_roots']['audit']['committed_files'], 1)
        self.assertEqual(json.loads((self.reports / 'release-manifest.json').read_text()), manifest)
        self.assertEqual((self.project / 'gui_config.json').read_bytes(), b'private fixture settings')
        self.assertEqual((self.project / 'user.ino').read_bytes(), b'private fixture sketch')
        for name, data in self.runtime.items():
            with self.subTest(retained_runtime=name):
                self.assertEqual((self.project / 'src/_python' / name).read_bytes(), data)

    def test_incomplete_runtime_refuses_publication(self):
        (self.project / 'src/_python' / build_release.REQUIRED_RUNTIME[-1]).unlink()
        with self.assertRaisesRegex(RuntimeError, 'Incomplete private Windows runtime'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_changed_installer_refuses_publication(self):
        (self.project / self.installer_path).write_bytes(b'damaged installer')
        with self.assertRaisesRegex(RuntimeError, 'committed LFS receipt'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_archive_verification_refuses_wrapped_paths_or_changed_file_contract(self):
        original_zip = zipfile.ZipFile
        for mutation, error in (('paths', 'ZIP root'), ('bytes', 'Archive bytes changed'),
                                ('size', 'Archive size or permissions changed'),
                                ('mode', 'Archive size or permissions changed')):
            with self.subTest(mutation=mutation):
                def changed_archive(path, mode='r', *arguments, **options):
                    if mode == 'r':
                        # Mutate only this completed disposable ZIP before the
                        # packager's independent archive verification opens it.
                        with original_zip(path) as source:
                            content = [(item, source.read(item)) for item in source.infolist()]
                        with original_zip(path, 'w') as destination:
                            for item, data in content:
                                if mutation == 'paths':
                                    item.filename = 'unexpected-wrapper/' + item.filename
                                elif item.filename == 'main/gui.py':
                                    if mutation == 'bytes':
                                        data = b'#' + data[1:]
                                    elif mutation == 'size':
                                        data += b'\n'
                                    elif mutation == 'mode':
                                        item.external_attr ^= 0o111 << 16
                                destination.writestr(item, data)
                    return original_zip(path, mode, *arguments, **options)

                with patch.object(build_release.zipfile, 'ZipFile', side_effect=changed_archive):
                    with self.assertRaisesRegex(RuntimeError, error):
                        self.build()
                self.assertFalse(self.output.exists())
                self.assertFalse((self.reports / 'release-manifest.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
