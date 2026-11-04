#!/usr/bin/env python3
"""Hardware-free exact-board chooser, background I/O and save failure fixtures."""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from direct.qt_browser_fixture import QtBrowserFixture, tasks_idle
from main.qt import package_browser as pb
from PySide6.QtCore import Qt
from src.modules import arduino_board_chooser as chooser, arduino_board_selection as policy
from src.modules import arduino_lib_req as browser
from src.modules.ui_metrics import WorkArea

RENDER_DIR = None


class BoardChooserChecks(QtBrowserFixture):
    def setUp(self):
        super().setUp()
        self.boards = self.folder / 'boards-package'
        self.boards.mkdir()
        (self.boards / 'boards.txt').write_text('alpha.name=Sensor board\nbeta.name=Sensor board\n', encoding='utf-8')
        self.metadata = {'name': 'Vendor sensor boards', 'package': 'fixture', 'architecture': 'avr',
                         'index_url': 'https://example.invalid/package.json', 'version': '1.2.3'}
        # These UI fixtures keep duplicate-name coverage independent of the
        # separately verified official vendor/architecture allowlist.
        self.allowed = {'alpha', 'beta'}
        def eligible(metadata):
            return (frozenset(self.allowed) if metadata.get('package') == 'fixture'
                    and metadata.get('architecture') == 'avr' else frozenset())
        for name, replacement in (('allowed_board_ids', eligible),
                                  ('board_allowed', lambda metadata, identifier: identifier in eligible(metadata))):
            patcher = patch.object(policy, name, side_effect=replacement, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Grouped downloader items keep index provenance on each version.
        item = {key: value for key, value in self.metadata.items() if key not in ('index_url', 'version')}
        item['versions'] = [{'url': 'https://example.invalid/core.zip',
                             **{key: self.metadata[key] for key in ('index_url', 'package', 'architecture', 'version')}}]
        self.tab = self.app.board_tab
        self.tab.populate({'boards': item})
        self.select(self.tab)
        self.settings = {'other': 'preserve', policy.FIELD: {policy.association_key(self.metadata): ['beta']}}
        self.load = patch.object(browser, '_load_settings', side_effect=lambda: copy.deepcopy(self.settings))
        self.save = patch.object(browser, '_save_settings', return_value=True)
        self.load_mock, self.save_mock = self.load.start(), self.save.start()
        self.addCleanup(self.load.stop)
        self.addCleanup(self.save.stop)
        preferences = patch.object(policy, 'load_preferences', side_effect=lambda **_kwargs: self.settings[policy.FIELD])
        preferences.start()
        self.addCleanup(preferences.stop)

    def open(self, *, area=None):
        if area is None:
            dialog = pb.ArduinoCliBoardDialog(self.app, self.tab, self.metadata, self.boards)
        else:
            with patch('main.qt.responsive.work_area', return_value=area):
                dialog = pb.ArduinoCliBoardDialog(self.app, self.tab, self.metadata, self.boards)
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.until(dialog.save_button.isEnabled)
        return dialog

    def rows(self, dialog):
        return [dialog.list.item(index) for index in range(dialog.list.count())]

    def capture(self, dialog, name):
        if RENDER_DIR:
            self.assertTrue(dialog.grab().save(str(RENDER_DIR / (name + '.png'))))

    def closed(self, dialog):
        self.until(lambda: not dialog.isVisible())

    def test_duplicate_names_keep_exact_ids_and_filter_retains_choices(self):
        with patch.object(policy, 'invalidate_preferences') as invalidate:
            dialog = self.open()
            self.assertEqual(dialog._chosen, {'beta'})
            first = self.rows(dialog)[0]
            self.assertIn('[alpha]', first.text())
            first.setCheckState(Qt.CheckState.Checked)
            dialog.search.setText('beta')
            self.pump()
            self.assertEqual(len(self.rows(dialog)), 1)
            self.assertEqual(dialog._chosen, {'alpha', 'beta'})
            dialog.save_button.click()
            self.closed(dialog)
            invalidate.assert_called_once()
        saved = self.save_mock.call_args.args[0]
        self.assertEqual(saved[policy.FIELD][policy.association_key(self.metadata)], ['alpha', 'beta'])
        self.assertEqual(saved['other'], 'preserve')

    def test_whole_row_click_toggles_once_like_its_indicator(self):
        dialog = self.open()
        row = self.rows(dialog)[0]
        self.assertNotIn('alpha', dialog._chosen)
        dialog.list.itemClicked.emit(row)
        self.assertIn('alpha', dialog._chosen)
        self.assertEqual(row.checkState(), Qt.CheckState.Checked)
        # An indicator click reaches itemChanged, then itemClicked, for one toggle.
        row.setCheckState(Qt.CheckState.Unchecked)
        dialog.list.itemClicked.emit(row)
        self.assertNotIn('alpha', dialog._chosen)
        self.assertEqual(row.checkState(), Qt.CheckState.Unchecked)

    def test_failed_save_stays_open_and_clear_removes_only_this_namespace(self):
        other = policy.association_key(dict(self.metadata, package='another'))
        self.settings[policy.FIELD][other] = ['alpha']
        self.save_mock.return_value = False
        dialog = self.open()
        dialog.clear_button.click()
        with patch.object(policy, 'invalidate_preferences') as invalidate:
            dialog.save_button.click()
            self.until(lambda: self.save_mock.called and dialog.save_button.isEnabled())
            invalidate.assert_not_called()
        self.assertIn('could not be saved', dialog.status.text())
        self.assertEqual(self.settings[policy.FIELD][policy.association_key(self.metadata)], ['beta'])
        saved = self.save_mock.call_args.args[0]
        self.assertNotIn(policy.association_key(self.metadata), saved[policy.FIELD])
        self.assertEqual(saved[policy.FIELD][other], ['alpha'])
        self.capture(dialog, 'arduino-choice-save-failure')

    def test_busy_and_changed_version_reject_save_and_cancel_does_not_write(self):
        dialog = self.open()
        self.app._busy = True
        dialog.save_button.click()
        self.save_mock.assert_not_called()
        self.app._busy = False
        self.tab.all_items['boards']['versions'].append(dict(self.tab.all_items['boards']['versions'][0], version='2.0.0'))
        self.tab.set_versions(self.tab.all_items['boards'])
        self.tab.version_combo.setCurrentIndex(1)
        dialog.save_button.click()
        self.save_mock.assert_not_called()
        dialog.reject()
        self.pump()
        self.save_mock.assert_not_called()

    def test_save_uses_selected_version_index_namespace(self):
        item = self.tab.all_items['boards']
        self.assertNotIn('index_url', item)
        self.assertEqual(item['versions'][0]['index_url'], self.metadata['index_url'])
        dialog = self.open()
        dialog.save_button.click()
        self.closed(dialog)
        self.save_mock.assert_called_once()
        stored = self.save_mock.call_args.args[0][policy.FIELD]
        self.assertEqual(stored[policy.association_key(self.metadata)], ['beta'])
        self.assertNotIn(policy.association_key(item), stored)

    def test_save_rejects_changed_index_or_missing_selected_version(self):
        dialog = self.open()
        item = self.tab.all_items['boards']
        item['versions'][0]['index_url'] = 'https://other.invalid/package.json'
        dialog.save_button.click()
        self.save_mock.assert_not_called()
        self.assertIn('reopen this chooser', dialog.status.text())
        item['versions'].clear()
        dialog.save_button.click()
        self.save_mock.assert_not_called()
        self.assertIn('reopen this chooser', dialog.status.text())

    def test_scan_and_persistence_run_off_the_gui_thread(self):
        gui = threading.get_ident()
        threads = []
        read = chooser.read_board_choices
        def scan(*args):
            threads.append(threading.get_ident())
            return read(*args)
        self.load_mock.side_effect = lambda: (threads.append(threading.get_ident()), copy.deepcopy(self.settings))[1]
        self.save_mock.side_effect = lambda _value: (threads.append(threading.get_ident()), True)[1]
        with patch.object(chooser, 'read_board_choices', side_effect=scan):
            dialog = self.open()
            dialog.save_button.click()
            self.closed(dialog)
        self.assertEqual(len(threads), 3)
        self.assertTrue(all(thread != gui for thread in threads))

    def test_cancel_during_scan_discards_late_results(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        finished = threading.Event()
        def delayed(*_args):
            gate.wait(2)
            finished.set()
            return [('alpha', 'Late board')]
        with patch.object(chooser, 'read_board_choices', side_effect=delayed):
            dialog = pb.ArduinoCliBoardDialog(self.app, self.tab, self.metadata, self.boards)
            self.addCleanup(dialog.deleteLater)
            dialog.show()
            dialog.reject()
            gate.set()
            self.until(lambda: finished.is_set() and tasks_idle(self.app._tasks))
            self.pump()
        self.assertEqual(dialog._rows, [])
        self.assertFalse(dialog.save_button.isEnabled())
        self.save_mock.assert_not_called()

    def test_all_palettes_fit_compact_work_area_and_keep_save_visible(self):
        for theme in ('default', 'light', 'solarized_dark'):
            with patch.object(browser, '_resolve_lib_req_theme', return_value=theme):
                self.app._apply_current_theme(force=True)
            for width, height in ((760, 620), (420, 320)):
                with self.subTest(theme=theme, width=width):
                    dialog = self.open(area=WorkArea(40, 35, width, height))
                    self.assertLessEqual(dialog.width(), width)
                    self.assertLessEqual(dialog.height(), height)
                    for button in (dialog.save_button, dialog.clear_button, dialog.cancel_button):
                        self.assertTrue(button.isVisible())
                        corner = button.mapTo(dialog, button.rect().bottomRight())
                        self.assertLessEqual(corner.y(), dialog.height())
                        self.assertLessEqual(corner.x(), dialog.width())
                    self.assertGreaterEqual(dialog.list.height(), 50)
                    self.capture(dialog, f'arduino-choice-{theme}-{width}')
                    dialog.close()
                    self.pump()

    def test_bounded_reader_and_missing_extraction(self):
        self.assertEqual(chooser.read_board_choices(self.boards), [('alpha', 'Sensor board'), ('beta', 'Sensor board')])
        with patch.object(chooser, '_MAX_BOARDS', 1):
            with self.assertRaisesRegex(ValueError, 'too many boards'):
                chooser.read_board_choices(self.boards)
        with self.assertRaisesRegex(ValueError, 'Download and extract'):
            chooser.read_board_choices(self.boards / 'absent')
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual(chooser.read_board_choices(self.boards, cancelled), [])

    def test_invalid_namespace_and_disallowed_package_never_open_or_save(self):
        item = self.tab.all_items['boards']
        original = dict(item), [dict(version) for version in item['versions']]
        with patch.object(pb, 'ArduinoCliBoardDialog') as dialog:
            item['versions'][0]['package'] = '../invalid'
            item['package'] = '../invalid'
            self.app._edit_arduino_cli_boards(self.tab)
            dialog.assert_not_called()
            self.assertIn('no valid vendor', self.app.status_label.text())
            item['package'] = item['versions'][0]['package'] = 'other'
            self.app._edit_arduino_cli_boards(self.tab)
            dialog.assert_not_called()
            self.assertIn('only for Arduino UNO Q', self.app.status_label.text())
        item.clear()
        item.update(original[0])
        item['versions'] = original[1]
        self.load_mock.assert_not_called()
        self.save_mock.assert_not_called()

    def test_stale_other_board_choices_and_matching_names_cannot_enable_cli(self):
        with (self.boards / 'boards.txt').open('a', encoding='utf-8') as stream:
            stream.write('excluded.name=Sensor board\n')
        self.settings[policy.FIELD][policy.association_key(self.metadata)] += ['excluded', 'absent']
        dialog = self.open()
        self.assertEqual(dialog._chosen, {'beta'})
        self.assertEqual(len(self.rows(dialog)), 2)
        self.assertFalse(any('[excluded]' in row.text() for row in self.rows(dialog)))
        dialog.save_button.click()
        self.closed(dialog)
        stored = self.save_mock.call_args.args[0][policy.FIELD][policy.association_key(self.metadata)]
        self.assertEqual(stored, ['beta'])

    def test_toggle_and_save_recheck_eligibility_after_dialog_loads(self):
        dialog = self.open()
        first = self.rows(dialog)[0]
        self.allowed = {'beta'}
        first.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(dialog._chosen, {'beta'})
        self.allowed = set()
        dialog.save_button.click()
        self.closed(dialog)
        stored = self.save_mock.call_args.args[0][policy.FIELD]
        self.assertNotIn(policy.association_key(self.metadata), stored)

    def test_downloader_button_tracks_allowed_package_busy_and_missing_version(self):
        button = self.tab.arduino_cli_btn
        eligible = dict(self.metadata, versions=[{'version': '1.2.3'}])
        other = dict(eligible, package='other', name='Arduino UNO Q')
        self.app._active_download_tab = None
        for metadata, busy, version, enabled in ((eligible, False, '1.2.3', True),
                                                 (other, False, '1.2.3', False),
                                                 (eligible, True, '1.2.3', False),
                                                 (eligible, False, 'absent', False)):
            self.app._busy = busy
            self.app._render_version_status(self.tab, metadata, version, None)
            self.assertEqual(button.isEnabled(), enabled, (metadata.get('package'), busy, version))
        self.app._busy = False
        self.tab.listbox.setCurrentIndex(self.tab.model.index(-1))
        self.app._update_version_status(self.tab)
        self.assertFalse(button.isEnabled())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render-dir', type=Path)
    options = parser.parse_args()
    RENDER_DIR = options.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BoardChooserChecks))
    raise SystemExit(0 if result.wasSuccessful() else 1)
