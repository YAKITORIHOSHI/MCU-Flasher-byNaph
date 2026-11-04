#!/usr/bin/env python3
"""Owned UI fixtures for custom board platform settings; no live persistence."""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from direct.qt_browser_fixture import QtBrowserFixture
from main.core.theme import Theme
from main.qt import package_browser as pb
from src.modules import arduino_lib_req as downloader
from src.modules.ui_metrics import WorkArea

RENDER_DIR = None


class CustomBoardDialogChecks(QtBrowserFixture):
    def setUp(self):
        super().setUp()
        self.item = {'name': 'Custom vendor sensor boards', 'package': 'fixture-vendor',
                     'architecture': 'fixture-arch', 'index_url': 'https://example.invalid/package.json',
                     'versions': [{'version': '1.2.3', 'url': 'https://example.invalid/core.zip',
                                   'boards': [{'name': 'Sensor board'}]}]}
        self.key = self.app._board_association_key(self.item)

    def open_dialog(self, *, area=None):
        configuration = self.app._board_platformio_associations.get(self.key) or {}
        if area is None:
            dialog = pb.BoardPlatformDialog(self.app, self.item, configuration, self.key)
        else:
            with patch('main.qt.responsive.work_area', return_value=area):
                dialog = pb.BoardPlatformDialog(self.app, self.item, configuration, self.key)
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.pump()
        return dialog

    def capture(self, dialog, name):
        if RENDER_DIR:
            self.assertTrue(dialog.grab().save(str(RENDER_DIR / (name + '.png'))))

    def test_all_themes_fit_short_monitors_keep_actions_and_scroll_form(self):
        self.app._board_platformio_associations[self.key] = {
            'platform': 'fixture-vendor/sensor-platform@1.2.3',
            'board_ids': {'sensor_rev_a': 'sensor_board_rev_a', 'sensor_rev_b': 'sensor_board_rev_b'},
        }
        for theme in ('default', 'light', 'solarized_dark'):
            with patch.object(downloader, '_resolve_lib_req_theme', return_value=theme):
                self.app._apply_current_theme(force=True)
            for size, width, height in (('regular', 760, 620), ('compact', 420, 320)):
                with self.subTest(theme=theme, size=size):
                    dialog = self.open_dialog(area=WorkArea(40, 35, width, height))
                    self.assertLessEqual(dialog.width(), width)
                    self.assertLessEqual(dialog.height(), height)
                    for button in (dialog.save_button, dialog.cancel_button):
                        self.assertTrue(button.isVisible())
                        position = button.mapTo(dialog, button.rect().bottomRight())
                        self.assertLessEqual(position.y(), dialog.height())
                        self.assertLessEqual(position.x(), dialog.width())
                    self.assertGreaterEqual(dialog.platform.height(), dialog.platform.fontMetrics().height())
                    self.capture(dialog, f'custom-board-{theme}-{size}')
                    dialog.mappings.setFocus()
                    self.pump()
                    self.assertGreater(dialog.form_scroll.viewport().height(), 0)
                    dialog.close()

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
                self.assertEqual(dialog.platform.text(), previous['platform'])
                dialog.platform.setText(literal)
                dialog.mappings.setPlainText('arduino_sensor = exact_pio_sensor\nsecond_id = second_pio_board')
                dialog.save_button.click()
                self.until(lambda: save.called and (not dialog.isVisible() or dialog.save_button.isEnabled()))
                value = {'platform': literal, 'board_ids': {'arduino_sensor': 'exact_pio_sensor',
                                                            'second_id': 'second_pio_board'}}
                saved = save.call_args.args[0]
                self.assertEqual(saved['board_platformio_associations'][self.key], value)
                self.assertEqual(saved['additional_board_urls'], settings['additional_board_urls'])
                self.assertEqual(saved['other'], 'preserve')
                if success:
                    self.assertEqual(self.app._board_platformio_associations[self.key], value)
                    self.assertFalse(dialog.isVisible())
                else:
                    self.assertEqual(self.app._board_platformio_associations[self.key], previous)
                    self.assertTrue(dialog.save_button.isEnabled())
                    self.assertIn('could not be saved', dialog.status.text())
                    self.capture(dialog, 'custom-board-save-failure')
                    dialog.close()

    def test_cancel_and_invalid_mappings_never_save(self):
        with patch.object(downloader, '_load_settings') as load, \
             patch.object(downloader, '_save_settings') as save:
            dialog = self.open_dialog()
            dialog.platform.setText('vendor/platform@1.2.3')
            dialog.mappings.setPlainText('duplicate = board_a\nduplicate = board_b')
            dialog.save_button.click()
            self.pump()
            self.assertIn('unique', dialog.status.text())
            dialog.cancel_button.click()
            self.pump()
            self.assertFalse(dialog.isVisible())
            load.assert_not_called()
            save.assert_not_called()
            self.assertEqual(self.app._board_platformio_associations, {})

    def test_busy_download_refuses_a_platform_change(self):
        with patch.object(downloader, '_save_settings') as save:
            dialog = self.open_dialog()
            dialog.platform.setText('vendor/platform@1.2.3')
            self.app._busy = True
            dialog.save_button.click()
            self.pump()
            self.assertIn('Wait for the current download', dialog.status.text())
            save.assert_not_called()
            self.app._busy = False
            dialog.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render-dir', type=Path)
    args = parser.parse_args()
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CustomBoardDialogChecks))
    raise SystemExit(0 if result.wasSuccessful() else 1)
