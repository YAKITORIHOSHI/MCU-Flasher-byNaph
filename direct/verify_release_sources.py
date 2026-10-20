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


class ReleaseSourceChecks(unittest.TestCase):
    def test_production_sources_and_tool_hook_are_included(self):
        for name in ('MCU_Flasher', 'main/web_bridge.py',
                     'src/modules/arduinoq_link_paths.py', 'src/dbs/bootstrap_config.json',
                     'installers/CH34x/CH341SER.exe', '.agents/skills/mcu-flash-gui-dev/SKILL.md'):
            with self.subTest(name=name):
                self.assertTrue(include_committed_source(name))

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
