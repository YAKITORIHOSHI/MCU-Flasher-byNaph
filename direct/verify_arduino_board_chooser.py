#!/usr/bin/env python3
"""Hardware-free exact-board chooser, background I/O and save failure fixtures."""
from __future__ import annotations

import argparse
import copy
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from src.modules import arduino_board_chooser as chooser, arduino_board_selection as policy
from src.modules import arduino_lib_req as browser, tk_glass
from src.modules.browser_loading import TkTasks
from src.modules.ui_metrics import WorkArea

APP = QApplication.instance() or QApplication([])
RENDER_DIR = None


class BoardChooserChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'temp' / 'audit'
        scratch.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='arduino-chooser-', dir=scratch)
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        (self.folder / 'boards.txt').write_text('alpha.name=Sensor board\nbeta.name=Sensor board\n', encoding='utf-8')
        self.root = tk.Tk()
        self.scale = float(os.environ.get('QT_SCALE_FACTOR', '1'))
        self.root.tk.call('tk', 'scaling', self.scale * 96 / 72)
        self.root.geometry('180x80+30+30')
        self.root.update()
        self.addCleanup(self.root.destroy)
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.addCleanup(lambda: self.assertEqual(self.errors, []))
        self.addCleanup(browser.Theme.apply_theme, browser.Theme.active_theme)
        self.app = browser.ArduinoBrowser.__new__(browser.ArduinoBrowser)
        self.app.root, self.app._busy = self.root, False
        self.app._tasks = TkTasks(self.root)
        self.app._set_status = Mock()
        self.app._post_ui = self.app._tasks.post
        self.metadata = {'name': 'Vendor sensor boards', 'package': 'fixture', 'architecture': 'avr',
                         'index_url': 'https://example.invalid/package.json', 'version': '1.2.3'}
        # These UI fixtures keep duplicate-name coverage independent of the
        # separately verified official vendor/architecture allowlist.
        self.allowed = {'alpha', 'beta'}
        def eligible(metadata):
            return (frozenset(self.allowed) if metadata.get('package') == 'fixture'
                    and metadata.get('architecture') == 'avr' else frozenset())
        self.package_policy = patch.object(policy, 'allowed_board_ids', side_effect=eligible, create=True)
        self.board_policy = patch.object(policy, 'board_allowed',
                                         side_effect=lambda metadata, identifier: identifier in eligible(metadata), create=True)
        self.package_policy.start()
        self.board_policy.start()
        self.addCleanup(self.package_policy.stop)
        self.addCleanup(self.board_policy.stop)
        # Grouped downloader items keep index provenance on each version.
        item = {key: value for key, value in self.metadata.items() if key not in ('index_url', 'version')}
        item['versions'] = [{key: self.metadata[key] for key in ('index_url', 'package', 'architecture', 'version')}]
        self.tab = SimpleNamespace(listbox=SimpleNamespace(curselection=lambda: (0,)),
                                   filtered_names=['boards'], all_items={'boards': item},
                                   version_var=tk.StringVar(self.root, value='1.2.3'))
        self.settings = {'other': 'preserve', policy.FIELD: {policy.association_key(self.metadata): ['beta']}}
        self.load = Mock(side_effect=lambda: copy.deepcopy(self.settings))
        self.save = Mock(return_value=True)
        self.preferences = patch.object(policy, 'load_preferences', side_effect=lambda **_kwargs: self.settings[policy.FIELD])
        self.preferences.start()
        self.addCleanup(self.preferences.stop)

    def pump(self, predicate=lambda: True):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.02)
        self.fail('Dialog worker did not finish')

    def open(self):
        dialog = chooser.open_board_chooser(self.app, self.tab, self.metadata, self.folder,
                     theme=browser.Theme, button=browser.make_flat_button,
                     load_settings=self.load, save_settings=self.save)
        self.pump(lambda: dialog._cli_save.cget('state') == 'normal')
        return dialog

    def capture(self, dialog, name):
        if RENDER_DIR:
            screenshot = APP.primaryScreen().grabWindow(int(dialog.winfo_id()))
            self.assertFalse(screenshot.isNull())
            self.assertTrue(screenshot.save(str(RENDER_DIR / (name + '.png'))))

    def test_duplicate_names_keep_exact_ids_and_filter_retains_choices(self):
        with patch.object(policy, 'invalidate_preferences') as invalidate:
            dialog = self.open()
            self.assertEqual(dialog._cli_chosen, {'beta'})
            first = dialog._cli_tree.get_children()[0]
            self.assertIn('[alpha]', dialog._cli_tree.item(first, 'values')[1])
            dialog._cli_toggle(first)
            dialog._cli_search.insert(0, 'beta')
            self.pump()
            self.assertEqual(len(dialog._cli_tree.get_children()), 1)
            self.assertEqual(dialog._cli_chosen, {'alpha', 'beta'})
            dialog._cli_save.invoke()
            self.pump(lambda: not dialog.winfo_exists())
            invalidate.assert_called_once()
        saved = self.save.call_args.args[0]
        self.assertEqual(saved[policy.FIELD][policy.association_key(self.metadata)], ['alpha', 'beta'])
        self.assertEqual(saved['other'], 'preserve')

    def test_failed_save_stays_open_and_clear_removes_only_this_namespace(self):
        other = policy.association_key(dict(self.metadata, package='another'))
        self.settings[policy.FIELD][other] = ['alpha']
        self.save.return_value = False
        dialog = self.open()
        dialog._cli_clear.invoke()
        with patch.object(policy, 'invalidate_preferences') as invalidate:
            dialog._cli_save.invoke()
            self.pump(lambda: self.save.called and dialog._cli_save.cget('state') == 'normal')
            invalidate.assert_not_called()
        self.assertIn('could not be saved', dialog._cli_status.cget('text'))
        self.assertEqual(self.settings[policy.FIELD][policy.association_key(self.metadata)], ['beta'])
        saved = self.save.call_args.args[0]
        self.assertNotIn(policy.association_key(self.metadata), saved[policy.FIELD])
        self.assertEqual(saved[policy.FIELD][other], ['alpha'])
        self.capture(dialog, 'arduino-choice-save-failure')

    def test_busy_and_changed_version_reject_save_and_cancel_does_not_write(self):
        dialog = self.open()
        self.app._busy = True
        dialog._cli_save.invoke()
        self.save.assert_not_called()
        self.app._busy = False
        self.tab.version_var.set('2.0.0')
        dialog._cli_save.invoke()
        self.save.assert_not_called()
        dialog.event_generate('<Escape>')
        self.pump()
        self.save.assert_not_called()

    def test_save_uses_selected_version_index_namespace(self):
        item = self.tab.all_items['boards']
        self.assertNotIn('index_url', item)
        self.assertEqual(item['versions'][0]['index_url'], self.metadata['index_url'])
        dialog = self.open()
        dialog._cli_save.invoke()
        self.pump(lambda: not dialog.winfo_exists())
        self.save.assert_called_once()
        stored = self.save.call_args.args[0][policy.FIELD]
        self.assertEqual(stored[policy.association_key(self.metadata)], ['beta'])
        self.assertNotIn(policy.association_key(item), stored)

    def test_save_rejects_changed_index_or_missing_selected_version(self):
        dialog = self.open()
        item = self.tab.all_items['boards']
        item['versions'][0]['index_url'] = 'https://other.invalid/package.json'
        dialog._cli_save.invoke()
        self.save.assert_not_called()
        self.assertIn('reopen this chooser', dialog._cli_status.cget('text'))
        item['versions'].clear()
        dialog._cli_save.invoke()
        self.save.assert_not_called()
        self.assertIn('reopen this chooser', dialog._cli_status.cget('text'))
        dialog.destroy()
        self.pump()

    def test_scan_and_persistence_run_off_tk_thread(self):
        gui = threading.get_ident()
        threads = []
        read = chooser.read_board_choices
        def scan(*args):
            threads.append(threading.get_ident())
            return read(*args)
        self.load.side_effect = lambda: (threads.append(threading.get_ident()), copy.deepcopy(self.settings))[1]
        self.save.side_effect = lambda _value: (threads.append(threading.get_ident()), True)[1]
        with patch.object(chooser, 'read_board_choices', side_effect=scan):
            dialog = self.open()
            dialog._cli_save.invoke()
            self.pump(lambda: not dialog.winfo_exists())
        self.assertEqual(len(threads), 3)
        self.assertTrue(all(thread != gui for thread in threads))

    def test_cancel_during_scan_discards_late_results(self):
        gate = threading.Event()
        def delayed(*_args):
            gate.wait(2)
            return [('alpha', 'Late board')]
        with patch.object(chooser, 'read_board_choices', side_effect=delayed):
            dialog = chooser.open_board_chooser(self.app, self.tab, self.metadata, self.folder,
                       theme=browser.Theme, button=browser.make_flat_button,
                       load_settings=self.load, save_settings=self.save)
            dialog.destroy()
            gate.set()
            self.pump(lambda: self.app._tasks._active == 0)
        self.save.assert_not_called()

    def test_all_palettes_fit_compact_work_area_and_keep_save_visible(self):
        for theme in ('default', 'light', 'solarized_dark'):
            browser.Theme.apply_theme(theme)
            self.app._configure_styles()
            for width, height in ((760, 620), (420, 320)):
                area = WorkArea(40, 35, min(round(width * self.scale), 1840), min(round(height * self.scale), 920))
                with patch.object(tk_glass, 'native_work_area', return_value=area):
                    dialog = self.open()
                    for _ in range(5):
                        self.root.update()
                        time.sleep(.03)
                    frame, _handle = tk_glass._native_frame(dialog)
                    self.assertGreaterEqual(frame.x, area.x)
                    self.assertGreaterEqual(frame.y, area.y)
                    self.assertLessEqual(frame.x + frame.width, area.x + area.width)
                    self.assertLessEqual(frame.y + frame.height, area.y + area.height)
                    for button in dialog._cli_save.master.winfo_children():
                        self.assertTrue(button.winfo_ismapped())
                        self.assertGreaterEqual(button.winfo_rooty(), dialog.winfo_rooty())
                        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                             dialog.winfo_rooty() + dialog.winfo_height())
                        self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                             dialog.winfo_rootx() + dialog.winfo_width())
                    self.assertGreaterEqual(dialog._cli_tree.winfo_height(), round(50 * self.scale))
                    self.capture(dialog, f'arduino-choice-{theme}-{width}')
                    dialog.destroy()
                    self.pump()

    def test_bounded_reader_and_missing_extraction(self):
        self.assertEqual(chooser.read_board_choices(self.folder), [('alpha', 'Sensor board'), ('beta', 'Sensor board')])
        with patch.object(chooser, '_MAX_BOARDS', 1):
            with self.assertRaisesRegex(ValueError, 'too many boards'):
                chooser.read_board_choices(self.folder)
        with self.assertRaisesRegex(ValueError, 'Download and extract'):
            chooser.read_board_choices(self.folder / 'absent')
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual(chooser.read_board_choices(self.folder, cancelled), [])

    def test_invalid_namespace_never_opens_dialog_or_saves(self):
        result = chooser.open_board_chooser(self.app, self.tab, dict(self.metadata, package='../invalid'), self.folder,
                       theme=browser.Theme, button=browser.make_flat_button,
                       load_settings=self.load, save_settings=self.save)
        self.assertIsNone(result)
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in self.root.winfo_children()))
        self.load.assert_not_called()
        self.save.assert_not_called()

    def test_disallowed_package_never_opens_dialog_or_reads_settings(self):
        metadata = dict(self.metadata, package='other', name='Arduino UNO Q')
        result = chooser.open_board_chooser(self.app, self.tab, metadata, self.folder,
                       theme=browser.Theme, button=browser.make_flat_button,
                       load_settings=self.load, save_settings=self.save)
        self.assertIsNone(result)
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in self.root.winfo_children()))
        self.load.assert_not_called()
        self.save.assert_not_called()
        self.assertIn('only for Arduino UNO Q', self.app._set_status.call_args.args[0])

    def test_stale_other_board_choices_and_matching_names_cannot_enable_cli(self):
        with (self.folder / 'boards.txt').open('a', encoding='utf-8') as stream:
            stream.write('excluded.name=Sensor board\n')
        self.settings[policy.FIELD][policy.association_key(self.metadata)] += ['excluded', 'absent']
        dialog = self.open()
        self.assertEqual(dialog._cli_chosen, {'beta'})
        self.assertEqual(len(dialog._cli_tree.get_children()), 2)
        self.assertFalse(any('[excluded]' in dialog._cli_tree.item(row, 'values')[1]
                             for row in dialog._cli_tree.get_children()))
        dialog._cli_save.invoke()
        self.pump(lambda: not dialog.winfo_exists())
        stored = self.save.call_args.args[0][policy.FIELD][policy.association_key(self.metadata)]
        self.assertEqual(stored, ['beta'])

    def test_toggle_and_save_recheck_eligibility_after_dialog_loads(self):
        dialog = self.open()
        first = dialog._cli_tree.get_children()[0]
        self.allowed = {'beta'}
        dialog._cli_toggle(first)
        self.assertEqual(dialog._cli_chosen, {'beta'})
        self.allowed.clear()
        dialog._cli_save.invoke()
        self.pump(lambda: not dialog.winfo_exists())
        stored = self.save.call_args.args[0][policy.FIELD]
        self.assertNotIn(policy.association_key(self.metadata), stored)

    def test_downloader_callback_rejects_other_package_before_chooser(self):
        self.tab.all_items['boards'] = dict(self.metadata, package='other', versions=[{'version': '1.2.3'}])
        self.app._download_dir = str(self.folder)
        with patch.object(chooser, 'open_board_chooser') as open_dialog:
            self.app._edit_arduino_cli_boards(self.tab)
        open_dialog.assert_not_called()
        self.load.assert_not_called()
        self.save.assert_not_called()

    def test_downloader_button_tracks_allowed_package_busy_and_missing_version(self):
        self.tab.arduino_cli_btn = Mock()
        self.tab.lbl_available = Mock()
        self.tab.download_btn = Mock()
        self.app._active_download_tab = None
        eligible = dict(self.metadata, versions=[{'version': '1.2.3'}])
        other = dict(eligible, package='other', name='Arduino UNO Q')
        for metadata, busy, version, state in ((eligible, False, '1.2.3', 'normal'),
                                              (other, False, '1.2.3', 'disabled'),
                                              (eligible, True, '1.2.3', 'disabled'),
                                              (eligible, False, 'absent', 'disabled')):
            self.app._busy = busy
            self.app._render_version_status(self.tab, 'boards', metadata, version, None)
            self.assertEqual(self.tab.arduino_cli_btn.configure.call_args.kwargs['state'], state)
        self.tab.listbox.curselection = lambda: ()
        self.app._update_version_status(self.tab)
        self.assertEqual(self.tab.arduino_cli_btn.configure.call_args.kwargs['state'], 'disabled')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render-dir', type=Path)
    options = parser.parse_args()
    RENDER_DIR = options.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BoardChooserChecks))
    raise SystemExit(0 if result.wasSuccessful() else 1)
