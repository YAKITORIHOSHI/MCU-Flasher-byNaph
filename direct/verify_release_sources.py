#!/usr/bin/env python3
"""Check Python release source selection without runtime or settings access."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
