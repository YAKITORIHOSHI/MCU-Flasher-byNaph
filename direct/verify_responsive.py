#!/usr/bin/env python3
"""Hardware-free screen/scale checks. Set QT_SCALE_FACTOR before starting.

Use --render-dir for owned fixture images. No application backend, downloads,
firmware operations or live preference writes are started by these checks.
"""
from __future__ import annotations
import argparse
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
# Tk captures and negative monitor coordinates require the native Windows
# plugin; Qt's offscreen plugin offsets negative frame origins by its margins.
# Preserve an explicit platform choice for headless fixture runs.
if sys.platform == 'win32':
    os.environ.setdefault('QT_QPA_PLATFORM', 'windows')
from verify_controls import ControlChecks, DownloaderChecks, APP, bootstrap_fixture
from PySide6.QtCore import QRect, QPoint, QObject, Signal, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QComboBox, QAbstractButton,
                              QLineEdit, QToolButton,
                              QApplication)
from main.qt.toolbar import ControlsBar, PrimaryToolbar, CompactDropdownPopup
from main.qt.serial_panel import SerialPanel
from main.qt.theme import build_stylesheet, register_fonts
from main.qt.responsive import clamp_window, fit_dialog, ScreenWatcher
from src.modules.ui_metrics import WorkArea, fit_rect, preferred_size

RENDER_DIR = None


def pump():
    for _ in range(5):
        APP.processEvents()
        time.sleep(.025)


def capture(widget, name):
    if RENDER_DIR:
        assert widget.grab().save(str(RENDER_DIR / (name + '.png')))


class QtResponsiveChecks(ControlChecks):
    def setUp(self):
        super().setUp()
        from verify_projects import isolate_layout_cloud
        isolate_layout_cloud(self, self.stack)
        register_fonts()
        APP.setStyleSheet(build_stylesheet('default'))

    def contained(self, widget, parent):
        rect = QRect(widget.mapTo(parent, QPoint()), widget.size())
        self.assertTrue(parent.rect().contains(rect),
                        f'{widget.objectName() or type(widget).__name__} {rect} outside {parent.rect()}')

    def terminal(self, parent=None):
        from main.qt.terminal_panel import TerminalPanel
        panel = self.own(TerminalPanel(self.backend, parent))
        for name in ('ensure_started', 'refresh_terminal', 'focus_terminal',
                     '_resize_embedded_terminal'):
            self.stack.enter_context(patch.object(panel, name))
        sent = self.stack.enter_context(patch.object(panel, '_send_control'))
        return panel, sent

    def test_coordinate_bounds(self):
        for area in (WorkArea(0, 0, 640, 480), WorkArea(-1280, 40, 1280, 680), WorkArea(1920, -900, 900, 900)):
            x, y, w, h = fit_rect(-4000, -4000, 4000, 4000, area, 8)
            self.assertGreaterEqual(x, area.x + 8)
            self.assertGreaterEqual(y, area.y + 8)
            self.assertLessEqual(x + w, area.x + area.width - 8)
            self.assertLessEqual(y + h, area.y + area.height - 8)
            width, height = preferred_size(area, 900, 660)
            self.assertLessEqual(width, area.width - 24)
            self.assertLessEqual(height, area.height - 48)
        widget = self.own(QWidget())
        with patch('main.qt.responsive.work_area', return_value=WorkArea(-640, 20, 640, 480)):
            fit_dialog(widget, (900, 660), (340, 300))
            widget.show()
            pump()
            widget.setGeometry(-900, -600, 900, 660)
            clamp_window(widget)
            frame = widget.frameGeometry()
            self.assertTrue(QRect(-640, 20, 640, 480).contains(frame), str(frame))

    def test_project_selector_glass_pages_and_frame(self):
        # Reuse the owned project fixtures, including safe action dispatch,
        # rather than constructing a backend that can touch live preferences.
        import verify_projects
        from main.qt.project_dialog import ProjectDialog
        with patch.object(verify_projects, 'RENDER_DIR', RENDER_DIR):
            verify_projects.ProjectChecks(
                'test_glass_picker_pages_fit_and_preserve_inputs_through_themes_and_resizing'
            ).debug()
        ratio = APP.primaryScreen().devicePixelRatio()
        for physical_width, physical_height in ((1920, 1040), (1366, 728), (1080, 1880)):
            area = WorkArea(0, 0, int(physical_width / ratio), int(physical_height / ratio))
            with patch('main.qt.responsive.work_area', return_value=area), \
                 patch('main.core.config.get_open_projects', return_value=[]), \
                 patch('main.core.config.get_theme_mode', return_value='default'):
                dialog = self.own(ProjectDialog(initial_dir=''))
                dialog.show()
                pump()
                clamp_window(dialog)
                for page in range(dialog._tabs.count()):
                    dialog._tabs.setCurrentIndex(page)
                    pump()
                    frame = dialog.frameGeometry()
                    self.assertTrue(QRect(area.x, area.y, area.width, area.height).contains(frame),
                                    f'{physical_width}x{physical_height} scale {ratio}: {frame}')
                    for button in dialog._tabs.currentWidget().findChildren(QAbstractButton):
                        # Scrolled page content may extend below its viewport;
                        # the page-owned footer actions must remain visible.
                        if button.isVisible() and button.parentWidget() is dialog._tabs.currentWidget():
                            self.contained(button, dialog)
                    if page == 1:
                        scroll = dialog._tab_scrolls[page]
                        scroll.ensureWidgetVisible(dialog._cb_include_cpp)
                        pump()
                        self.contained(dialog._cb_include_cpp, scroll.viewport())
                        scroll.verticalScrollBar().setValue(0)
                    if page in (0, 1):
                        capture(dialog, f'project-frame-{physical_width}-{physical_height}-{page}')
                dialog.close()

    def test_control_rows_and_serial_reflow(self):
        host = self.own(QWidget())
        layout = QVBoxLayout(host)
        toolbar = PrimaryToolbar(self.backend, host)
        controls = ControlsBar(self.backend, host)
        serial = SerialPanel(self.backend, host)
        layout.addWidget(toolbar)
        layout.addWidget(controls)
        layout.addWidget(serial)
        host.show()
        for width in (1920, 1366, 1372, 1371, 1350, 1349, 1024, 800, 640, 480, 464, 320, 800, 1920):
            host.resize(width, 620)
            toolbar.set_responsive_width(width)
            pump()
            self.assertLessEqual(host.width(), width, 'A layout minimum forced the window wider')
            for field in (controls.board_selector, controls.btn_search_board, controls.port_combo,
                          controls.upload_speed_combo, controls.cb_timestamp, controls.cb_skip_compile):
                self.contained(field, controls)
            if controls.cb_timestamp.isVisible() and controls.cb_skip_compile.isVisible():
                timestamp_right = controls.cb_timestamp.geometry().right()
                checkbox_gap = controls.cb_skip_compile.geometry().left() - timestamp_right - 1
                self.assertLessEqual(controls.cb_timestamp.width(), controls.cb_timestamp.sizeHint().width() + 2,
                                     'Timestamp checkbox must not stretch away from its label')
                self.assertLessEqual(controls.cb_skip_compile.width(), controls.cb_skip_compile.sizeHint().width() + 2,
                                     'Skip Compile checkbox must stay compact')
                self.assertGreaterEqual(checkbox_gap, 0)
                self.assertLessEqual(checkbox_gap, 8, 'Timestamp and Skip Compile should read as one option group')
            if controls.is_compact():
                self.contained(controls.btn_opt_dropdown, controls)
            workspace_actions = [button for button in (
                controls.btn_detach_editor, controls.btn_toggle_editor,
                controls.btn_toggle_monitors, controls.btn_settings,
                controls.btn_ai, controls.btn_opt_dropdown) if button.isVisible()]
            for button in workspace_actions:
                self.contained(button, controls)
            workspace_label = controls._group_labels[3]
            if workspace_label.isVisible():
                self.contained(workspace_label, controls)
                first_action = workspace_actions[0]
                heading_rect = QRect(workspace_label.mapTo(controls, QPoint()), workspace_label.size())
                action_rect = QRect(first_action.mapTo(controls, QPoint()), first_action.size())
                self.assertEqual(heading_rect.left(), action_rect.left(),
                                 'Workspace heading must align with its actions at every width')
                self.assertLess(heading_rect.bottom(), action_rect.top(),
                                'Workspace heading must stay above its actions')
            for button in (toolbar.btn_compile, toolbar.btn_upload, toolbar.btn_actions_dropdown,
                           toolbar.btn_project, toolbar.btn_download):
                if button.isVisible():
                    self.contained(button, toolbar)
            if width < 560:
                self.assertTrue(toolbar.right_container.isVisible())
                self.contained(toolbar.btn_project, toolbar)
                self.contained(toolbar.btn_download, toolbar)
            for field in (serial.btn_reset, serial.btn_pause, serial.cb_autoscroll, serial.cb_auto_clear,
                          serial.cb_ansi_clear, serial.baud_combo, serial.btn_copy, serial.btn_clear):
                self.contained(field, serial._header)
            self.assertLess(controls.upload_speed_combo.width(), 130)
            self.assertLess(serial.baud_combo.width(), 130)
            for combo in (controls.upload_speed_combo, serial.baud_combo):
                text = max(combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count()))
                self.assertGreaterEqual(combo.width(), text + 40, 'Digits must fit beside padding and the arrow')
            if width in (1920, 800, 480):
                capture(host, f'controls-{width}')
        self.assertEqual(controls._row_mode, 1)
        self.assertFalse(serial._header_stacked)
        content_font = serial._output.font()
        controls.set_responsive_height(420)
        host.resize(320, 620)
        pump()
        self.assertLessEqual(controls.height(), 80, 'Short-window chrome must leave room for tool controls')
        for field in (controls.board_selector, controls.btn_search_board, controls.port_combo,
                      controls.upload_speed_combo, controls.cb_timestamp, controls.cb_skip_compile,
                      controls.btn_opt_dropdown):
            self.assertTrue(field.isVisible())
            self.contained(field, controls)
        controls.set_responsive_height(312)
        pump()
        self.assertLessEqual(controls.height(), 42)
        for field in (controls.board_selector, controls.port_combo, controls.upload_speed_combo,
                      controls.btn_opt_dropdown):
            self.assertTrue(field.isVisible())
            self.contained(field, controls)
        controls._toggle_options_menu()
        pump()
        option_labels = [button.text() for button in controls._opt_popup.findChildren(QAbstractButton)]
        self.assertTrue(any('Timestamps' in text for text in option_labels))
        self.assertTrue(any('Skip Compile' in text for text in option_labels))
        controls._opt_popup.close()
        controls.set_responsive_height(620)
        host.resize(1920, 620)
        pump()
        self.assertTrue(all(label.isVisible() for label in controls._group_labels))
        self.assertEqual(serial._output.font(), content_font)

    def test_terminal_add_button_alignment_and_shell_choices(self):
        host = self.own(QWidget())
        layout = QVBoxLayout(host)
        panel, sent = self.terminal(host)
        layout.addWidget(panel)
        host.show()
        pump()
        button = panel._btn_add_tab
        self.assertIsInstance(button, QToolButton)
        self.assertEqual(button.popupMode(), QToolButton.ToolButtonPopupMode.InstantPopup)
        self.assertIs(button.menu(), panel._add_menu)
        pwsh_action = next(action for action in panel._add_menu.actions() if 'pwsh' in action.text().lower())
        cmd_action = next(action for action in panel._add_menu.actions() if 'cmd' in action.text().lower())

        def new_payload():
            calls = [call for call in sent.call_args_list if call.args[0] == 'new']
            self.assertTrue(calls, 'A shell choice must issue a new-session control')
            call = calls[-1]
            self.assertIn(call.args[1], panel._sessions_meta)
            return call.kwargs['extra']

        for mode in ('default', 'light', 'solarized_dark'):
            APP.setStyleSheet(build_stylesheet(mode))
            panel.apply_theme(mode)
            for width in (1920, 800, 480, 320, 800, 1920):
                with self.subTest(theme=mode, width=width):
                    host.resize(width, 240)
                    pump()
                    self.assertLessEqual(host.width(), width)
                    self.assertTrue(panel._tab_bar.isHidden())
                    self.assertEqual(button.mapTo(panel._header, QPoint()).x(),
                                     panel._header_layout.contentsMargins().left(),
                                     'An empty terminal must put + at the header left edge')
                    self.contained(button, panel._header)
                    self.assertIsNone(panel.findChild(QAbstractButton, 'btn-terminal-fullscreen'))
                    for control in panel._header.findChildren(QAbstractButton):
                        if control.isVisible():
                            self.contained(control, panel._header)
                            self.assertNotIn('Full', control.text())
                            self.assertNotIn('Restore', control.text())
                    if width in (1920, 480):
                        capture(host, f'terminal-{mode}-{width}-empty')

                    sent.reset_mock()
                    menu_was_visible = []
                    def dismiss_chooser():
                        menu_was_visible.append(panel._add_menu.isVisible())
                        panel._add_menu.close()
                    QTimer.singleShot(0, dismiss_chooser)
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
                    pump()
                    self.assertEqual(menu_was_visible, [True])
                    self.assertFalse(any(call.args[0] == 'new' for call in sent.call_args_list),
                                     'Opening or dismissing + must not start a shell')
                    self.assertEqual(panel._tab_bar.count(), 0)

                    pwsh_action.trigger()
                    pump()
                    self.assertEqual(new_payload()['kind'], 'pwsh')
                    self.assertEqual(panel._tab_bar.count(), 1)
                    self.assertTrue(panel._tab_bar.isVisible())
                    # QSS can reserve a wide tab-bar size while painting a much
                    # shorter tab. Measure the painted tab, not the outer bar.
                    tab_rect = panel._tab_bar.tabRect(0)
                    tab_right = panel._tab_bar.mapTo(panel._header, tab_rect.topRight()).x()
                    gap = button.mapTo(panel._header, QPoint()).x() - tab_right - 1
                    self.assertGreaterEqual(gap, 0)
                    self.assertLessEqual(gap, panel._header_layout.spacing() + 4,
                                         '+ must follow the actual painted tab without reserved blank width')
                    self.assertLessEqual(abs(button.geometry().center().y() - tab_rect.center().y()
                                             - panel._tab_bar.geometry().y()), 2,
                                         '+ must be vertically aligned with the actual session tab')

                    menu_was_visible = []
                    def choose_cmd():
                        menu_was_visible.append(panel._add_menu.isVisible())
                        cmd_action.trigger()
                        panel._add_menu.close()
                    # The chooser runs a nested event loop. Choose CMD there
                    # with all process/IPC calls still mocked.
                    QTimer.singleShot(0, choose_cmd)
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
                    pump()
                    self.assertEqual(menu_was_visible, [True], '+ must open the shell menu')
                    self.assertEqual(new_payload()['kind'], 'cmd')
                    self.assertEqual(panel._tab_bar.count(), 2)
                    self.contained(panel._tab_bar, panel._header)
                    self.contained(button, panel._header)
                    last_tab_rect = panel._tab_bar.tabRect(panel._tab_bar.count() - 1)
                    last_tab_right = panel._tab_bar.mapTo(panel._header, last_tab_rect.topRight()).x()
                    gap = button.mapTo(panel._header, QPoint()).x() - last_tab_right - 1
                    self.assertGreaterEqual(gap, 0)
                    self.assertLessEqual(gap, panel._header_layout.spacing() + 4,
                                         '+ must remain adjacent to the last actual tab after adding another shell')
                    if width in (1920, 480):
                        capture(host, f'terminal-{mode}-{width}-sessions')

                    while panel._tab_bar.count():
                        panel._on_tab_close_requested(0)
                    pump()
                    self.assertTrue(panel._tab_bar.isHidden())
                    self.assertEqual(button.mapTo(panel._header, QPoint()).x(),
                                     panel._header_layout.contentsMargins().left(),
                                     '+ must return to the left edge after the last session closes')
                    self.assertFalse(panel._btn_clear.isEnabled())
                    self.assertFalse(panel._btn_kill.isEnabled())

    def test_terminal_show_reveal_and_project_reset_preserve_zero_tabs(self):
        panel, sent = self.terminal()
        panel.resize(800, 240)
        panel.show()
        pump()
        self.assertEqual(panel._tab_bar.count(), 0)
        panel._on_tab_revealed()
        panel.reset_for_project(str(ROOT / 'temp/audit/terminal-empty'))
        self.assertEqual(panel._tab_bar.count(), 0)
        self.assertFalse(panel._sessions_meta)
        self.assertIs(panel._stack.currentWidget(), panel._empty_card)
        self.assertTrue(panel._tab_bar.isHidden())
        self.assertFalse(panel._btn_clear.isEnabled())
        self.assertFalse(panel._btn_kill.isEnabled())
        new_calls = [call for call in sent.call_args_list if call.args[0] == 'new']
        self.assertEqual(new_calls, [])

    def test_terminal_many_tabs_scroll_without_pushing_add_button_outside_header(self):
        host = self.own(QWidget())
        layout = QVBoxLayout(host)
        panel, sent = self.terminal(host)
        layout.addWidget(panel)
        host.show()
        pump()
        for index in range(16):
            panel.add_session('pwsh' if index % 2 == 0 else 'cmd')
        for mode in ('default', 'light', 'solarized_dark'):
            APP.setStyleSheet(build_stylesheet(mode))
            panel.apply_theme(mode)
            for width in (1920, 800, 480, 320, 800):
                with self.subTest(theme=mode, width=width):
                    host.resize(width, 140)
                    panel._tab_bar.setCurrentIndex(0)
                    pump()
                    panel._tab_bar.setCurrentIndex(panel._tab_bar.count() - 1)
                    pump()
                    self.assertLessEqual(host.width(), width)
                    self.contained(panel._tab_bar, panel._header)
                    self.contained(panel._btn_add_tab, panel._header)
                    add_left = panel._btn_add_tab.mapTo(panel._header, QPoint()).x()
                    visible_rects = [panel._tab_bar.tabRect(index).intersected(panel._tab_bar.rect())
                                     for index in range(panel._tab_bar.count())]
                    visible_rects = [rect for rect in visible_rects if not rect.isEmpty()]
                    self.assertTrue(visible_rects)
                    painted_right = max(panel._tab_bar.mapTo(panel._header, rect.topRight()).x()
                                        for rect in visible_rects)
                    # Overflow scroll arrows are real chrome after the final
                    # painted tab. Include those visible controls, while still
                    # rejecting any extra reserved blank width after them.
                    scroll_buttons = [button for button in panel._tab_bar.findChildren(QToolButton)
                                      if button.isVisible()]
                    chrome_right = max([painted_right] + [
                        button.mapTo(panel._header, button.rect().topRight()).x()
                        for button in scroll_buttons])
                    gap = add_left - chrome_right - 1
                    self.assertGreaterEqual(gap, 0)
                    self.assertLessEqual(gap, panel._header_layout.spacing() + 4)
                    for button in (panel._btn_clear, panel._btn_kill):
                        self.contained(button, panel._header)
                        self.assertGreater(button.geometry().left(), panel._btn_add_tab.geometry().right())
                    if width == 480:
                        capture(host, f'terminal-{mode}-many-tabs')

    def test_build_console_original_journal_preserves_saved_fonts(self):
        from main.core import config
        from main.core.theme import Theme
        from main.qt.console_panel import ConsolePanelContainer
        from PySide6.QtGui import QFontMetricsF, QTextCursor
        self.addCleanup(Theme.apply_theme, Theme.active_theme)

        cfg = {'timestamp_enabled': False, 'clear_console_on_action': True,
               'clear_serial_on_action': False}
        self.stack.enter_context(patch.object(config, 'load_gui_config', return_value=cfg))
        saved = self.stack.enter_context(patch.object(config, 'save_gui_config', return_value=True))
        theme_mode = self.stack.enter_context(patch.object(config, 'get_theme_mode', return_value='default'))
        self.stack.enter_context(patch.object(config, 'get_hide_build_console_warnings', return_value=False))
        clipboard = Mock()
        self.stack.enter_context(patch.object(QApplication, 'clipboard', return_value=clipboard))
        worker_title = '⚡ Running Parallel Compilation on 6 Logical Processors'
        worker_width = 73
        worker_rows = [
            (' ╔' + '═' * worker_width + '╗', 'header'),
            ('   ' + worker_title.center(worker_width), 'header'),
            (' ╚' + '═' * worker_width + '╝', 'header'),
        ]
        timing_rows = [
            ('╔═════════════════════════════════╗', 'purple_header'),
            ('║    Compilation Time Breakdown   ║', 'purple_header'),
            ('╠═════════════════════════════════╣', 'purple_header'),
            ('║ Code Build & Compilation : 4.2s ║', 'purple_header'),
            ('║ Total Elapsed Time       : 7.1s ║', 'purple_header'),
            ('╚═════════════════════════════════╝', 'purple_header'),
        ]
        compile_rows = [(f'  ⚙ Compiling source{number}.cpp.o...', 'info') for number in range(12)]
        fixture = [
            ('=' * 50, 'header'),
            ('  ⚙  COMPILING (PlatformIO)', 'header'),
            ('=' * 50, 'header'),
            ('  Sketch : isolated responsiveness fixture', 'dim'),
            ('  Board  : ESP32 Dev Module (fixture)', 'dim'),
            ('  Tool   : PlatformIO', 'dim'),
            ('', 'normal'),
            ('  ℹ Incremental build enabled; successful objects are preserved.', 'info'),
            ('  ✔ Entry points OK — setup()/loop() found in: example.ino', 'success'),
            ('', 'normal'),
            *worker_rows,
            ('   >>> System Reserved — 6 Logical Processors <<<', 'dim'),
            ('', 'normal'),
            ('  ℹ Selected-board workspace is isolated from every other board.', 'info'),
            ('    PlatformIO will compile only missing or changed units.', 'dim'),
            ('', 'normal'),
            ('  ⚙ Initializing PlatformIO build engine & dependency tree...', 'purple'),
            ('    SCons is resolving header dependencies in memory...', 'purple_dim'),
            ('    Processing mcu_env (platform: espressif32; board: esp32dev; framework: arduino)', 'purple_dim'),
            ('  ' + '─' * 50, 'purple_dim'),
            *compile_rows,
            ('  ⚠ Warning at sketch.cpp:12:3', 'warning'),
            ('  12 | lookup("building / looking for");', 'warning'),
            ('       ^~~~~~', 'warning'),
            ('  ⚙ Compiling example.ino.cpp.o...', 'info'),
            ('  🔗 Linking...', 'dim'),
            ('  📏 Checking firmware size...', 'dim'),
            ('  RAM: 8.5% (used 28012 bytes from 327680 bytes)', 'success'),
            ('  Flash: 26.3% (used 344969 bytes from 1310720 bytes)', 'success'),
            ('  📦 Binary artifact: firmware.bin (337.2 KB) ready for upload', 'success'),
            ('', 'normal'),
            *timing_rows,
            ('', 'normal'),
            ('  ✔ Compilation successful! (7.1s)', 'success'),
        ]
        original = '\n'.join(text for text, _ in fixture)

        def check_header(container):
            header = container.header
            self.assertFalse(hasattr(header, '_btn_details'), 'The original console has one output view')
            self.assertNotIn('Activity', header._title_lbl.text())
            for control in (header._btn_copy, header._btn_clear,
                            header.cb_autoscroll, header._btn_options,
                            header.cb_auto_clear, header.cb_auto_clear_serial):
                if control.isVisible():
                    self.contained(control, header)
                    self.assertTrue(control.visibleRegion().contains(control.rect().center()),
                                    f'Build header control clipped: {control.text()}')
                    self.assertFalse(control.isWindow())
            self.assertTrue(header._btn_copy.isVisible())
            self.assertTrue(header._btn_clear.isVisible())
            self.assertTrue(header.cb_autoscroll.isVisible())
            compact = header.width() < 850
            self.assertEqual(header._btn_options.isVisible(), compact)
            self.assertEqual(header.cb_auto_clear.isVisible(), not compact)
            self.assertEqual(header.cb_auto_clear_serial.isVisible(), not compact)
            if compact:
                options = header._btn_options.menu().actions()
                self.assertEqual(len(options), 2)
                self.assertEqual(options[0].isChecked(), header.cb_auto_clear.isChecked())
                self.assertEqual(options[1].isChecked(), header.cb_auto_clear_serial.isChecked())

        def check_frame_metrics(console, point_size):
            widths = []
            reference = QFontMetricsF(console.document().defaultFont())
            for text, _ in worker_rows + timing_rows:
                # Search literal event text so indentation and box padding are
                # verified in the real document, rather than recreated here.
                cursor = console.document().find(text)
                self.assertFalse(cursor.isNull(), f'Missing original frame row: {text}')
                cursor.setPosition(cursor.selectionStart() + max(0, text.find('╔') if '╔' in text else 0))
                cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
                font = cursor.charFormat().font().resolve(console.document().defaultFont())
                self.assertEqual(font.pointSize(), point_size)
                metrics = QFontMetricsF(font)
                self.assertAlmostEqual(metrics.horizontalAdvance('M'), reference.horizontalAdvance('M'), places=3,
                                       msg='Headers and box borders must use the saved monospace cell width')
                if text in [line for line, _ in timing_rows]:
                    widths.append(metrics.horizontalAdvance(text))
            self.assertTrue(all(len(text) == len(timing_rows[0][0]) for text, _ in timing_rows))
            self.assertLessEqual(max(widths) - min(widths), .01,
                                 'Every timing frame row must align to the same rendered right edge')

        for point_size in (11, 18):
            with patch.object(config, 'get_monitor_font_size', return_value=point_size):
                container = self.own(ConsolePanelContainer(self.backend))
            container.setObjectName('build-console-fixture')
            console, header = container.console, container.header
            container.resize(1920, 520)
            container.show()
            pump()
            content_font = console.font()
            self.assertEqual(content_font.pointSize(), point_size)
            for mode in ('default', 'light', 'solarized_dark'):
                from main.qt.theme import get_palette
                theme_mode.return_value = mode
                Theme.apply_theme(mode)
                background = get_palette(mode)['BG_DARKEST']
                APP.setStyleSheet(build_stylesheet(mode) +
                                 f'QWidget#build-console-fixture {{ background: {background}; }}')
                container.apply_theme(mode)
                console.clear()
                for text, tag in fixture:
                    console.append_log({'text': text, 'tag': tag, 'newline': True,
                                        'timestamp': '[12:34:56]'})
                while console._queue:
                    console._flush_queue()
                for width in (320, 400, 480, 640, 850, 1100, 1920, 480, 1920):
                    with self.subTest(theme=mode, font=point_size, width=width):
                        container.resize(width, 520)
                        pump()
                        self.assertLessEqual(container.width(), width,
                                             'Build header size hints must not force a wider window')
                        self.assertEqual(console.font(), content_font)
                        self.assertEqual(console.document().defaultFont().pointSize(), point_size)
                        self.assertGreaterEqual(console.viewport().height(), console.fontMetrics().lineSpacing())
                        self.contained(header, container)
                        check_header(container)
                        self.assertEqual(console.toPlainText(), original)
                        self.assertEqual(console.get_content_for_clipboard(False), original)
                        self.assertNotIn('Compiling source units', console.toPlainText())
                        check_frame_metrics(console, point_size)
                        if width in (320, 1100):
                            with patch('main.qt.console_panel.QTimer.singleShot') as later:
                                header._btn_copy.click()
                                pump()
                                self.assertEqual(clipboard.setText.call_args.args[0], original)
                                check_header(container)
                                later.call_args.args[1]()
                        if width in (480, 1920):
                            capture(container, f'build-{mode}-{point_size}pt-{width}-output')
                if RENDER_DIR:
                    container.resize(1280, 680)
                    pump()
                    console.verticalScrollBar().setValue(0)
                    pump()
                    capture(container, f'build-{mode}-{point_size}pt-start')
                    console.verticalScrollBar().setValue(console.verticalScrollBar().maximum())
                    pump()
                    capture(container, f'build-{mode}-{point_size}pt-compilation-timing')
                # Resizing/themes/output are read-only; explicit display
                # choices now persist so the next app run restores them.
                saved.assert_not_called()
                header.cb_autoscroll.setChecked(False)
                self.assertFalse(console._autoscroll)
                saved.assert_called_once()
                self.assertEqual(saved.call_args.kwargs['shared_updates'], {'build_autoscroll': False})
                saved.reset_mock()
                header.cb_autoscroll.setChecked(True)
                self.assertTrue(console._autoscroll)
                saved.assert_called_once()
                self.assertEqual(saved.call_args.kwargs['shared_updates'], {'build_autoscroll': True})
                saved.reset_mock()
                # An unchanged incremental build emits no source object rows.
                # The display must not manufacture compilation activity.
                console.clear()
                incremental = [item for item in fixture if not item[0].startswith('  ⚙ Compiling ')]
                for text, tag in incremental:
                    console.append_log({'text': text, 'tag': tag})
                while console._queue:
                    console._flush_queue()
                expected_incremental = '\n'.join(text for text, _ in incremental)
                self.assertEqual(console.toPlainText(), expected_incremental)
                self.assertEqual(console.get_content_for_clipboard(False), expected_incremental)
                self.assertNotIn('.cpp.o', console.toPlainText())
                self.assertNotIn('Compiling source units', console.toPlainText())
                check_frame_metrics(console, point_size)
            header._btn_clear.click()
            self.assertEqual(console.toPlainText(), '')
            self.assertEqual(console.get_content_for_clipboard(), '')
            self.assertEqual(console.font(), content_font)
            container.hide()
        saved.assert_not_called()

    def test_settings_and_setup_small_workareas(self):
        for width, height in ((1280, 720), (800, 600), (640, 480), (480, 640)):
            area = WorkArea(0, 0, width, height)
            with patch('main.qt.responsive.work_area', return_value=area):
                settings = self.settings()
                settings.show()
                setup, _gui, _namespace = bootstrap_fixture()
                self.own(setup)
                setup.show()
                pump()
                for dialog in (settings, setup):
                    clamp_window(dialog)
                    self.assertTrue(QRect(0, 0, width, height).contains(dialog.frameGeometry()), str(dialog.frameGeometry()))
                for button in (settings.btn_save, settings.btn_cancel, settings.btn_reset):
                    self.contained(button, settings)
                content = settings.scroll.widget()
                self.assertLessEqual(content.width(), settings.scroll.viewport().width())
                for field in (settings.cpu_combo, settings.theme_combo, settings.autosave_spin):
                    self.contained(field, field.parentWidget())
                    self.assertGreaterEqual(field.width(), field.minimumSizeHint().width())
                setup._on_status("Preparing the selected board tools")
                setup._on_log("✔ Private Python runtime ready", "ok")
                setup._on_log("✔ Tool Manager: framework-arduino-avr-attiny@1.5.2 has been installed!", "ok")
                setup._on_log("– Preparing platformio/framework-arduino-avr-megacore @ ~3.1.0", "info")
                setup._on_update_block("package_progress", "  – Unpacking 60%\n")
                setup._on_progress(60)
                pump()
                for field in (setup.timer_lbl, setup.status_lbl, setup.skip_cb, setup.auto_scroll_cb, setup.pct_lbl, setup.prog_bar):
                    self.contained(field, setup)
                self.assertGreater(setup.summary_edit.height(), 40)
                self.contained(setup.details_btn, setup)
                for row in setup.package_rows:
                    if row.isVisible():
                        self.contained(row, setup)
                capture(settings, f'settings-{width}x{height}')
                capture(setup, f'setup-{width}x{height}')
                setup._set_details_expanded(True)
                pump()
                self.assertGreater(setup.log_edit.height(), 40)
                self.contained(setup.log_edit, setup)
                capture(setup, f'setup-details-{width}x{height}')
                settings.hide()
                setup.hide()

    def test_setup_keeps_reading_space_on_high_scale_laptop_workareas(self):
        class Screen(QObject):
            availableGeometryChanged = Signal(QRect)
            logicalDotsPerInchChanged = Signal(float)

            def __init__(self, width, height):
                super().__init__()
                self.area = QRect(0, 0, width, height)

            def geometry(self):
                return QRect(0, 0, self.area.width(), self.area.height() + 48)

            def availableGeometry(self):
                return self.area

        # Physical 1366x768 and 1280x720 at 200%, with a 48 logical-pixel
        # taskbar. The fresh-process scale sweep exercises native DPR separately.
        for width, height in ((683, 336), (640, 312)):
            area = WorkArea(0, 0, width, height)
            screen = Screen(width, height)
            for mode in ('default', 'light', 'solarized_dark'):
                with self.subTest(area=(width, height), theme=mode), \
                     patch('main.qt.responsive.work_area', return_value=area), \
                     patch('main.qt.responsive.active_screen', return_value=screen):
                    setup, gui, namespace = bootstrap_fixture(mode)
                    self.own(setup)
                    setup._on_log('Python dependencies', 'section')
                    setup._on_update_block('pip', '\n'.join(
                        f'fixture-package-{i}    Downloading\n▰▱▱▱ 25%\n' for i in range(14)))
                    setup._on_status('Preparing board and library tools for the offline workspace')
                    gui._start_time = time.time() - 36001
                    setup._tick_clock()
                    setup.show()
                    pump()
                    clamp_window(setup)
                    pump()
                    font = setup.summary_edit.font()
                    for details in (False, True):
                        setup._set_details_expanded(details)
                        pump()
                        self.assertTrue(screen.area.contains(setup.frameGeometry()))
                        view = setup.log_edit if details else setup.summary_edit
                        self.assertGreaterEqual(view.viewport().height(), view.fontMetrics().lineSpacing() * 2)
                        for field in (setup.title_lbl, setup.timer_chip, setup.details_btn, setup.status_lbl,
                                      setup.pct_lbl, setup.prog_bar, setup.skip_cb, setup.auto_scroll_cb, view):
                            self.contained(field, setup)
                        for row in setup.package_rows:
                            if row.isVisible():
                                self.contained(row, setup)
                                self.contained(row, row.parentWidget())
                        for row in [setup.overall_row] + [row for row in setup.package_rows if row.isVisible()]:
                            for label in (row.status_lbl, row.pct_lbl):
                                self.assertGreaterEqual(label.height(), max(label.fontMetrics().height(),
                                                                            label.heightForWidth(label.width())))
                            self.contained(row.progress_bar, row)
                        self.assertEqual(setup.summary_edit.font(), font)
                        capture(setup, f'setup-short-{width}x{height}-{mode}-{details}')
                    namespace['save_bootstrap_config'].assert_not_called()
                    setup.hide()

    def test_picker_and_modify_fit_narrow_workarea(self):
        from main.qt.board_dialog import BoardSearchDialog
        from main.qt.modify_dialog import ModifyFilesDialog
        from main.qt import board_dialog
        with patch('main.qt.responsive.work_area', return_value=WorkArea(0, 0, 480, 640)), \
             patch.object(board_dialog, 'load_recent_boards', return_value=[]):
            picker = self.own(BoardSearchDialog(board_list=['Demo target']))
            modify = self.own(ModifyFilesDialog(self.backend))
            for dialog in (picker, modify):
                dialog.show()
                pump()
                self.assertLessEqual(dialog.frameGeometry().width(), 480)
                for widget in dialog.findChildren(QAbstractButton):
                    if widget.isVisible() and not widget.objectName().startswith('qt_scrollarea'):
                        self.contained(widget, dialog)
                capture(dialog, 'picker-narrow' if dialog is picker else 'modify-narrow')
                dialog.hide()

    def test_popup_edges_and_watcher_quiet_idle(self):
        host = self.own(QWidget())
        toolbar = PrimaryToolbar(self.backend, host)
        QVBoxLayout(host).addWidget(toolbar)
        toolbar.set_responsive_width(480)
        host.setGeometry(0, 360, 480, 100)
        host.show()
        watcher = ScreenWatcher(host)
        pump()
        self.assertFalse(watcher._timer.isActive())
        with patch('main.qt.responsive.work_area', return_value=WorkArea(0, 0, 480, 160)):
            toolbar._toggle_actions_menu()
            popup = toolbar._actions_popup
            pump()
            self.assertTrue(QRect(0, 0, 480, 160).contains(popup.geometry()), str(popup.geometry()))
            self.assertGreater(popup._scroll.verticalScrollBar().maximum(), 0)
            popup.close()

    def test_workarea_and_monitor_changes_coalesce(self):
        class Screen(QObject):
            availableGeometryChanged = Signal(QRect)
            logicalDotsPerInchChanged = Signal(float)
            def __init__(self, rect):
                super().__init__()
                self.rect = rect
            def availableGeometry(self):
                return self.rect
        first = Screen(QRect(0, 0, 1280, 720))
        second = Screen(QRect(-900, 40, 900, 600))
        current = [first]
        widget = self.own(QWidget())
        refreshed = []
        watcher = ScreenWatcher(widget, refreshed.append)
        with patch('main.qt.responsive.active_screen', side_effect=lambda _widget=None: current[0]):
            fit_dialog(widget)
            widget.show()
            pump()
            refreshed.clear()
            first.rect = QRect(0, 40, 640, 440)
            for _ in range(50):
                first.availableGeometryChanged.emit(first.rect)
                first.logicalDotsPerInchChanged.emit(144)
            pump()
            self.assertEqual(refreshed, [first])
            self.assertTrue(first.rect.contains(widget.frameGeometry()))
            current[0] = second
            watcher._schedule()
            pump()
            self.assertIs(watcher._screen, second)
            self.assertTrue(second.rect.contains(widget.frameGeometry()))
            self.assertFalse(watcher._timer.isActive())
            refreshed.clear()
            first.availableGeometryChanged.emit(first.rect)
            pump()
            self.assertEqual(refreshed, [], 'The old screen must be disconnected')


class TkResponsiveChecks(DownloaderChecks):
    def setUp(self):
        import tkinter as tk
        create_root = tk.Tk
        def scaled_root(*args, **kwargs):
            root = create_root(*args, **kwargs)
            root.tk.call('tk', 'scaling', float(os.environ.get('QT_SCALE_FACTOR', '1')) * 96 / 72)
            return root
        with patch.object(tk, 'Tk', side_effect=scaled_root):
            super().setUp()

    def test_header_and_panes_at_native_font_scales(self):
        app = self.app
        from src.modules.tk_glass import ui_scale
        scale = ui_scale(self.root)
        maximum = self.root.winfo_screenwidth() - 48
        wide = min(round(1040 * scale), maximum)
        narrow = min(round(480 * scale), maximum)
        for width in (wide, narrow, wide):
            height = min(round(660 * scale), self.root.winfo_screenheight() - 80)
            self.root.geometry(f'{width}x{height}+20+20')
            self.root.deiconify()
            self.pump()
            self.assertEqual(app.lib_tab._responsive_panes._vertical,
                             app.lib_tab._responsive_panes.pane.winfo_width() < round(660 * scale))
            for button in (app.sources_btn, app.refresh_btn, app.quit_btn):
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
                self.assertLessEqual(button.winfo_y() + button.winfo_height(), button.master.winfo_height())
            self.assertTrue(app.progress.winfo_ismapped())
            self.assertLessEqual(app.progress.winfo_rooty() + app.progress.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())
            app.notebook.select(2)
            self.pump()
            self.assertEqual(app.installed_tab._responsive_panes._vertical,
                             app.installed_tab._responsive_panes.pane.winfo_width() < round(660 * scale))
            detail = app.installed_tab
            for control in (detail.search_entry, detail.lbl_search_status):
                self.assertTrue(control.winfo_ismapped())
                self.assertLessEqual(control.winfo_x() + control.winfo_width(), control.master.winfo_width())
            self.assertGreater(detail.search_entry.winfo_width(), round(70 * scale))
            self.assertIs(detail._details_view._root(), self.root, 'Do not shadow Tk base methods')
            detail.lbl_placeholder.pack_forget()
            detail._details_view.pack(fill='both', expand=True, padx=10, pady=8)
            detail.lbl_name.configure(text='Installed device toolkit')
            detail.lbl_type.configure(text='Type: Library')
            detail.lbl_path.configure(text=str(ROOT / 'temp/audit/fixture-download/device-toolkit'))
            self.pump()
            canvas = detail._details_view.canvas
            canvas.yview_moveto(1)
            self.pump()
            self.assertGreater(canvas.winfo_height(), 40)
            self.assertAlmostEqual(canvas.yview()[1], 1.0, places=2)
            if RENDER_DIR:
                pixmap = APP.primaryScreen().grabWindow(int(self.root.winfo_id()))
                self.assertTrue(pixmap.save(str(RENDER_DIR / f'installed-{width}.png')))
            app.notebook.select(0)
            self.pump()
            if RENDER_DIR:
                pixmap = APP.primaryScreen().grabWindow(int(self.root.winfo_id()))
                self.assertTrue(pixmap.save(str(RENDER_DIR / f'downloader-{width}.png')))

    def test_board_preparation_controls_and_source_status_wrap_on_short_screen(self):
        app = self.app
        from src.modules.tk_glass import ui_scale
        scale = ui_scale(self.root)
        width = min(round(480 * scale), self.root.winfo_screenwidth() - 48)
        height = min(round(510 * scale), self.root.winfo_screenheight() - 80)
        board = dict(name='ESP8266 device core', package='esp8266', architecture='esp8266', maintainer='Community',
                     category='Device support', boards=['NodeMCU 1.0 (ESP-12E Module)', 'NodeMCU 0.9 (ESP-12 Module)'],
                     versions=[dict(version='3.1.2', url='https://fixture.invalid/esp8266.zip', size=12000)])
        app.board_tab.populate({board['name']: board})
        app.board_tab.listbox.selection_set(0)
        app.board_tab._on_select()
        app.notebook.select(1)
        self.root.geometry(f'{width}x{height}+20+20')
        self.root.deiconify()
        self.pump()
        app.board_tab.detail_canvas.yview_moveto(1)
        self.pump()
        # On short panes, controls cannot all be mapped at once. Use Tk canvas
        # coordinates for the embedded controls: native Windows widgets such as
        # ttk.Combobox can report root coordinates in a different DPI space.
        controls = (app.board_tab.version_combo, app.board_tab.download_btn, app.board_tab.prepare_btn)
        for index, control in enumerate(controls):
            canvas = app.board_tab.detail_canvas
            content = app.board_tab._detail_content
            self.assertTrue(canvas.winfo_ismapped(), 'Board details canvas must be visible')
            bounds = canvas.bbox('all')
            self.assertIsNotNone(bounds, 'Board details need a scrollable content region')
            content_height = max(1, bounds[3] - bounds[1])
            viewport_height = max(1, canvas.winfo_height())
            relative_top = 0
            ancestor = control
            while ancestor is not content:
                self.assertIsNotNone(ancestor.master, 'Board control must belong to the details content')
                relative_top += ancestor.winfo_y()
                ancestor = ancestor.master
            window_top = canvas.coords(app.board_tab._content_window)[1]
            control_top = window_top + relative_top
            control_bottom = control_top + control.winfo_height()
            max_offset = max(bounds[1], bounds[3] - viewport_height)
            desired_top = min(max(bounds[1], control_top - max(0, (viewport_height - control.winfo_height()) // 2)),
                              max_offset)
            canvas.yview_moveto((desired_top - bounds[1]) / content_height)
            self.pump()
            visible_top = canvas.canvasy(0)
            visible_bottom = canvas.canvasy(canvas.winfo_height())
            self.assertGreater(control.winfo_width(), 0, f'{control.winfo_class()} must have a laid-out width')
            self.assertGreater(control.winfo_height(), 0, f'{control.winfo_class()} must have a laid-out height')
            self.assertGreaterEqual(control_top, visible_top - 1,
                                    f'{control.winfo_class()} is above the details viewport')
            self.assertLessEqual(control_bottom, visible_bottom + 1,
                                 f'{control.winfo_class()} is below the details viewport')
            if RENDER_DIR:
                pixmap = APP.primaryScreen().grabWindow(int(self.root.winfo_id()))
                self.assertTrue(pixmap.save(str(RENDER_DIR / f'board-control-{index}.png')))
            self.assertLessEqual(control.winfo_rootx() + control.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
        app.sources_status.configure(text='https://vendor.invalid/package/device/index.json: Refresh failed; using saved catalog')
        app.sources_btn.invoke()
        self.pump()
        self.assertGreater(app.sources_status.winfo_height(), 12)
        self.assertLessEqual(app.sources_status.winfo_width(), app.sources_status.master.winfo_width())


def main():
    global RENDER_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render-dir', type=Path)
    parser.add_argument('--only', action='append', default=[],
                        help='Run checks whose method name contains this text (repeatable).')
    args = parser.parse_args()
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    suite = unittest.TestSuite()
    for cls in (QtResponsiveChecks, TkResponsiveChecks):
        for name in cls.__dict__:
            if name.startswith('test_') and (not args.only or any(fragment in name for fragment in args.only)):
                suite.addTest(cls(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f'Qt display scale: {APP.primaryScreen().devicePixelRatio():g}')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
