#!/usr/bin/env python3
"""Owned UI fixtures for custom board platform settings; no live persistence."""
from __future__ import annotations

import argparse
import copy
import os
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'windows' if sys.platform == 'win32' else 'offscreen')
from PySide6.QtWidgets import QApplication
from src.modules import arduino_lib_req as downloader
from src.modules import tk_glass
from src.modules.ui_metrics import WorkArea

APP = QApplication.instance() or QApplication([])
RENDER_DIR = None


class CustomBoardDialogChecks(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.scale = float(os.environ.get('QT_SCALE_FACTOR', '1'))
        self.root.tk.call('tk', 'scaling', self.scale * 96 / 72)
        self.root.geometry('180x80+30+30')
        self.root.update()
        self.addCleanup(self.root.destroy)
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.addCleanup(lambda: self.assertEqual(self.errors, []))
        self.addCleanup(downloader.Theme.apply_theme, downloader.Theme.active_theme)
        self.app = downloader.ArduinoBrowser.__new__(downloader.ArduinoBrowser)
        self.app.root = self.root
        self.app._busy = False
        self.app._board_platformio_associations = {}
        self.app._set_status = Mock()
        self.app._post_ui = lambda callback: callback()
        self.app._tasks = SimpleNamespace(start=lambda callback, failed=None: callback())
        self.item = {'name': 'Custom vendor sensor boards', 'package': 'fixture-vendor',
                     'architecture': 'fixture-arch', 'index_url': 'https://example.invalid/package.json',
                     'versions': [{'version': '1.2.3', 'boards': [{'name': 'Sensor board'}]}]}
        self.tab = SimpleNamespace(listbox=SimpleNamespace(curselection=lambda: (0,)),
                                   filtered_names=[self.item['name']], all_items={self.item['name']: self.item},
                                   version_var=tk.StringVar(self.root, value='1.2.3'))
        self.key = self.app._board_association_key(self.item)

    def pump(self):
        for _ in range(8):
            self.root.update()
            time.sleep(.02)

    def open_dialog(self):
        self.app._edit_board_platform(self.tab)
        self.pump()
        return next(child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel))

    def capture(self, dialog, name):
        if RENDER_DIR:
            capture = APP.primaryScreen().grabWindow(int(dialog.winfo_id()))
            self.assertFalse(capture.isNull())
            self.assertTrue(capture.save(str(RENDER_DIR / (name + '.png'))))

    def test_all_themes_fit_short_monitors_keep_actions_and_scroll_focus_visible(self):
        self.app._board_platformio_associations[self.key] = {
            'platform': 'fixture-vendor/sensor-platform@1.2.3',
            'board_ids': {'sensor_rev_a': 'sensor_board_rev_a', 'sensor_rev_b': 'sensor_board_rev_b'},
        }
        for theme in ('default', 'light', 'solarized_dark'):
            downloader.Theme.apply_theme(theme)
            self.app._configure_styles()
            for size in ('regular', 'compact'):
                width = min(round((760 if size == 'regular' else 420) * self.scale), 1840)
                height = min(round((620 if size == 'regular' else 320) * self.scale), 920)
                area = WorkArea(40, 35, width, height)
                with patch.object(tk_glass, 'native_work_area', return_value=area):
                    dialog = self.open_dialog()
                    if size == 'compact':
                        dialog.geometry(f'{max(1, width - 60)}x{max(1, height - 64)}')
                        self.pump()
                    frame, _handle = tk_glass._native_frame(dialog)
                    self.assertGreaterEqual(frame.x, area.x)
                    self.assertGreaterEqual(frame.y, area.y)
                    self.assertLessEqual(frame.x + frame.width, area.x + area.width)
                    self.assertLessEqual(frame.y + frame.height, area.y + area.height)
                    for button in dialog._platform_save.master.winfo_children():
                        self.assertTrue(button.winfo_ismapped())
                        self.assertGreaterEqual(button.winfo_rooty(), dialog.winfo_rooty())
                        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                             dialog.winfo_rooty() + dialog.winfo_height())
                    entry = dialog._platform_entry
                    self.assertGreaterEqual(entry.winfo_height(), entry.winfo_reqheight())
                    self.capture(dialog, f'custom-board-{theme}-{size}')
                    form = dialog._platform_form
                    dialog._platform_mappings.focus_force()
                    self.pump()
                    self.assertGreater(form.canvas.winfo_height(), 0)
                    self.assertGreaterEqual(dialog._platform_mappings.winfo_rooty(), form.canvas.winfo_rooty() - 1)
                    if size == 'compact':
                        self.assertGreater(form.body.winfo_height(), form.canvas.winfo_height())
                        self.capture(dialog, f'custom-board-{theme}-{size}-mappings')
                    dialog.destroy()
                    self.pump()

    def test_literal_values_save_and_failed_save_preserves_previous_associations(self):
        literal = 'https://example.invalid/platform.git#sensor-release-1.2.3'
        previous = {'platform': 'vendor/old-platform@1.0.0', 'board_ids': {'old_id': 'old_board'}}
        self.app._board_platformio_associations[self.key] = previous
        settings = {'additional_board_urls': ['https://example.invalid/package.json'],
                    'board_platformio_associations': {self.key: previous}, 'other': 'preserve'}
        for success in (False, True):
            with patch.object(downloader, '_load_settings', return_value=copy.deepcopy(settings)), \
                 patch.object(downloader, '_save_settings', return_value=success) as save:
                dialog = self.open_dialog()
                self.assertEqual(dialog._platform_entry.get(), previous['platform'])
                dialog._platform_entry.delete(0, 'end')
                dialog._platform_entry.insert(0, literal)
                dialog._platform_mappings.delete('1.0', 'end')
                dialog._platform_mappings.insert('1.0', 'arduino_sensor = exact_pio_sensor\nsecond_id = second_pio_board')
                dialog._platform_save.invoke()
                self.pump()
                value = {'platform': literal, 'board_ids': {'arduino_sensor': 'exact_pio_sensor',
                                                          'second_id': 'second_pio_board'}}
                saved = save.call_args.args[0]
                self.assertEqual(saved['board_platformio_associations'][self.key], value)
                self.assertEqual(saved['additional_board_urls'], settings['additional_board_urls'])
                self.assertEqual(saved['other'], 'preserve')
                if success:
                    self.assertEqual(self.app._board_platformio_associations[self.key], value)
                    self.assertFalse(dialog.winfo_exists())
                else:
                    self.assertEqual(self.app._board_platformio_associations[self.key], previous)
                    self.assertEqual(dialog._platform_save.cget('state'), 'normal')
                    self.assertIn('could not be saved', dialog._platform_status.cget('text'))
                    self.capture(dialog, 'custom-board-save-failure')
                    dialog.destroy()

    def test_cancel_and_invalid_mappings_never_save(self):
        with patch.object(downloader, '_load_settings') as load, \
             patch.object(downloader, '_save_settings') as save:
            dialog = self.open_dialog()
            dialog._platform_entry.insert(0, 'vendor/platform@1.2.3')
            dialog._platform_mappings.insert('1.0', 'duplicate = board_a\nduplicate = board_b')
            dialog._platform_save.invoke()
            self.pump()
            self.assertIn('unique', dialog._platform_status.cget('text'))
            next(button for button in dialog._platform_save.master.winfo_children()
                 if button.cget('text') == 'Cancel').invoke()
            self.pump()
            load.assert_not_called()
            save.assert_not_called()
            self.assertEqual(self.app._board_platformio_associations, {})

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native placement fixture')
    def test_native_fit_preserves_negative_monitor_coordinates(self):
        import ctypes
        widget = SimpleNamespace(winfo_exists=lambda: True, winfo_width=lambda: 600,
                                 winfo_height=lambda: 450, minsize=Mock(), geometry=Mock(),
                                 update_idletasks=Mock())
        fitter = tk_glass.DialogFit.__new__(tk_glass.DialogFit)
        fitter.dialog, fitter._pending, fitter._scale = widget, None, 1
        fitter._minimum = (320, 240)
        fitter._fitting = False
        area = WorkArea(-1920, 40, 960, 540)
        with patch.object(tk_glass, '_native_frame', return_value=(WorkArea(-2200, -100, 616, 488), 123)), \
             patch.object(ctypes.windll.user32, 'SetWindowPos') as position:
            fitter._fit(area)
        handle, owner, x, y, width, height, flags = position.call_args.args
        self.assertEqual(handle, 123)
        self.assertEqual((x, y), (-1912, 48))
        self.assertEqual((width, height), (0, 0))
        self.assertEqual(flags, 0x0001 | 0x0004 | 0x0010)
        self.assertFalse(fitter._fitting)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render-dir', type=Path)
    args = parser.parse_args()
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CustomBoardDialogChecks))
    raise SystemExit(0 if result.wasSuccessful() else 1)
