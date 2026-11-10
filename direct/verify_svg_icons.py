"""Isolated SVG visibility, interaction and source checks for Windows and Ubuntu."""
from __future__ import annotations
import argparse
import ast
from contextlib import ExitStack
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('PYTHONDONTWRITEBYTECODE','1')
PARSER = argparse.ArgumentParser(description=__doc__)
PARSER.add_argument('--render-dir',type=Path)
ARGS, EXTRA = PARSER.parse_known_args()
from src.modules.svg_icons import NAMES, ASSET_ROOT, svg_source
from src.modules.ui_palette import contrast_ratio
from main.core.theme import Theme

EMOJI = re.compile('[\U0001f000-\U0001faff\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d\u20e3]')
MODES = ('default','light','solarized_dark')


class SourceChecks(unittest.TestCase):
    def test_production_sources_have_no_literal_emoji(self):
        for base in ('main','src/modules'):
            for path in (ROOT/base).rglob('*.py'):
                text=path.read_text(encoding='utf-8-sig')
                self.assertFalse(EMOJI.search(text),str(path.relative_to(ROOT)))
                ast.parse(text,filename=str(path))
        self.assertFalse(EMOJI.search((ROOT/'src/editor/index.html').read_text(encoding='utf-8')))

    def test_control_svg_inks_match_checked_hover_disabled_and_field_surfaces(self):
        from src.modules.svg_icons import control_assets
        for mode in MODES:
            palette = Theme.PALETTES[mode]
            controls = control_assets(mode)
            for state,surface in (("checked","CYAN_DIM"),("hover","CYAN"),("disabled","BG_MID"),("chevron","BG_DARK"),("setup-hover","BORDER_LIT")):
                source = Path(controls[state]).read_text(encoding='utf-8')
                ink = ET.fromstring(source)[0].get("stroke")
                self.assertGreaterEqual(contrast_ratio(ink,palette[surface]),3 if state=="disabled" else 4.5,(mode,state))

    def test_compatibility_severity_is_independent_of_icon_glyphs(self):
        from main.core.board_compat import is_compat_warning, plain_compat_reason, SOFT_WARNING_PREFIX
        caution = SOFT_WARNING_PREFIX + "GPIO reserved on this board"
        self.assertTrue(is_compat_warning(caution))
        self.assertEqual(plain_compat_reason(caution), "GPIO reserved on this board")
        old = chr(0x26a0) + " GPIO reserved on this board"
        self.assertTrue(is_compat_warning(old))
        self.assertEqual(plain_compat_reason(old), "GPIO reserved on this board")
        self.assertFalse(is_compat_warning("Selected board is incompatible"))
        self.assertFalse(is_compat_warning(""))

    def test_svg_family_has_local_bounded_geometry_and_rejects_unknown_names(self):
        self.assertGreaterEqual(len(NAMES),64)
        for name in NAMES:
            source=svg_source(name,'#112233')
            node=ET.fromstring(source)
            self.assertEqual(node.get('viewBox'),'0 0 24 24')
            self.assertIn('stroke="#112233"',source)
            self.assertNotIn('href=',source)
            self.assertNotIn('<text',source)
            self.assertNotIn('currentColor',source)
        with self.assertRaises(ValueError):svg_source('../unknown','#ffffff')
        with self.assertRaises(ValueError):svg_source('brand','url(http://example.invalid)')


os.environ.setdefault('QT_QPA_PLATFORM','windows' if sys.platform=='win32' else 'offscreen')
from PySide6.QtCore import QCoreApplication,QEvent,Qt,QPoint
from PySide6.QtGui import QColor,QIcon,QImage,QPainter,QPalette,QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QHBoxLayout,QLabel,QVBoxLayout,QWidget,QTabBar,QStyle,QStyleOption,QMainWindow,QDockWidget,QAbstractButton
from main.qt.icons import ActionButton,IconLabel,icon,_ink
from main.qt.theme import build_stylesheet,register_fonts
from main.qt.garbage_collection import install_gui_garbage_collector
APP=QApplication.instance() or QApplication([])
COLLECTOR=install_gui_garbage_collector(APP)
register_fonts()

class QtSvgChecks(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        from main.core import config
        for name,value in (('_load_raw_config',{'shared':{},'instances':{}}),('_save_raw_config',True),('save_gui_config',True)):
            self.stack.enter_context(patch.object(config,name,return_value=value))
        self.addCleanup(APP.setStyleSheet,APP.styleSheet())
        self.addCleanup(Theme.apply_theme,Theme.active_theme)

    def own(self,widget):
        self.addCleanup(widget.deleteLater)
        return widget

    def test_action_states_use_visible_svg_in_every_theme(self):
        for mode in MODES:
            Theme.apply_theme(mode);APP.setStyleSheet(build_stylesheet(mode))
            parent=self.own(QWidget());row=QHBoxLayout(parent)
            button=ActionButton('Copy',parent);row.addWidget(button)
            button.setStyleSheet('QPushButton { background: #ffe266; color: #141414; }')
            button.show();parent.show();APP.processEvents()
            self.assertFalse(button.icon().isNull())
            self.assertEqual(button.text(),'Copy')
            for enabled in (True,False,True):
                button.setEnabled(enabled);APP.processEvents()
                group=QPalette.ColorGroup.Active if enabled else QPalette.ColorGroup.Disabled
                ink=_ink(button,QPalette.ColorRole.ButtonText,QPalette.ColorRole.Button,group)
                self.assertGreaterEqual(contrast_ratio(ink,button.palette().color(group,QPalette.ColorRole.Button).name()),3)
            button.set_icon('check');button.setText('')
            self.assertEqual(button._icon_name,'check')
            self.assertTrue(button.accessibleName())
            button.set_icon('copy');button.setText('Copy')
            self.assertEqual(button._icon_name,'copy')

    def test_compact_controls_preserve_explicit_icons_and_actions(self):
        from direct.verify_runtime import PreviewBackend
        from main.qt.serial_panel import SerialPanel
        from main.qt.terminal_panel import TerminalPanel
        from main.qt.ai_panel import AIPanel
        backend=PreviewBackend()
        serial=self.own(SerialPanel(backend=backend))
        for width in (1200,850,500):
            serial.set_responsive_width(width)
            for paused in (False,True):
                serial._on_pause_toggle(paused)
                self.assertEqual(serial.btn_pause._icon_name,'resume' if paused else 'pause')
                self.assertFalse(serial.btn_pause.icon().isNull())
        terminal=self.own(TerminalPanel(backend=backend))
        terminal.set_responsive_width(400)
        self.assertFalse(terminal._btn_kill.icon().isNull())
        self.assertFalse(terminal._btn_clear.icon().isNull())
        self.assertFalse(terminal._btn_add_tab.icon().isNull())
        self.assertTrue(terminal._btn_add_tab.accessibleName())
        assistant=self.own(AIPanel(backend=backend))
        self.assertFalse(assistant._loader_card._mark.pixmap().isNull())

    def test_status_icons_do_not_enter_clipboard_or_change_unicode_input(self):
        from main.qt.console_panel import ConsolePanel
        panel=self.own(ConsolePanel())
        text='Literal user text: '+chr(0x1f600)+' / '+chr(0x6f22)
        panel.append_log({'text':text,'tag':'warning','newline':True})
        panel._flush_queue()
        self.assertEqual(panel.toPlainText(),text)
        self.assertEqual(panel.get_content_for_clipboard(False),text)
        self.assertEqual(panel._gutter_icon(panel.document().firstBlock())[0],'warning')
        for mode in MODES:
            panel.apply_theme(mode)
            self.assertEqual(panel.toPlainText(),text)

    def test_terminal_tab_and_floating_dock_close_glyphs_are_visible_and_clickable_in_each_theme(self):
        from main.qt.theme import install_no_focus_lines
        from main.qt.icons import tab_icon_inks, ClosableTerminalTabs
        install_no_focus_lines(APP)
        for mode in MODES:
            Theme.apply_theme(mode); APP.setStyleSheet(build_stylesheet(mode))
            host=self.own(QWidget()); layout=QVBoxLayout(host)
            tabs=ClosableTerminalTabs(host); tabs.setTabsClosable(True); tabs.setExpanding(False)
            tabs.addTab('Bash'); tabs.addTab('CMD'); layout.addWidget(tabs)
            closed=[]; tabs.tabCloseRequested.connect(closed.append)
            host.resize(400,100); host.show(); APP.processEvents()
            for index in (0,1):
                button=tabs.tabButton(index,QTabBar.ButtonPosition.RightSide) or tabs.tabButton(index,QTabBar.ButtonPosition.LeftSide)
                self.assertIsNotNone(button)
                self.assertTrue(tabs.tabRect(index).contains(button.geometry()), (mode,index))
                glyph=QImage(button.size(),QImage.Format.Format_ARGB32); glyph.fill(Qt.GlobalColor.transparent)
                button.render(glyph,QPoint(),QRegion(),QWidget.RenderFlag.DrawChildren)
                ink=QColor(tab_icon_inks()[index==tabs.currentIndex()])
                self.assertTrue(any(glyph.pixelColor(x,y).alpha()>128 and
                                    max(abs(glyph.pixelColor(x,y).red()-ink.red()),abs(glyph.pixelColor(x,y).green()-ink.green()),abs(glyph.pixelColor(x,y).blue()-ink.blue()))<8
                                    for x in range(glyph.width()) for y in range(glyph.height())), (mode,index))
            QTest.mouseClick(button,Qt.MouseButton.LeftButton)
            self.assertEqual(closed,[1])
            main=self.own(QMainWindow()); dock=QDockWidget('Notifications',main)
            dock.setWidget(QLabel('Owned notification fixture')); main.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea,dock)
            main.show(); dock.setFloating(True); APP.processEvents()
            close=dock.findChild(QAbstractButton,'qt_dockwidget_closebutton')
            self.assertIsNotNone(close)
            glyph=APP.style().standardIcon(QStyle.StandardPixmap.SP_DockWidgetCloseButton,None,dock).pixmap(16,16).toImage()
            self.assertTrue(any(glyph.pixelColor(x,y).alpha()>128 for x in range(glyph.width()) for y in range(glyph.height())),mode)
            QTest.mouseClick(close,Qt.MouseButton.LeftButton); APP.processEvents()
            self.assertFalse(dock.isVisible())
            main.hide(); host.hide()

    def test_render_all_icons_and_themed_gallery(self):
        names=sorted(NAMES)
        height=112+((len(names)+7)//8)*78
        gallery=QImage(1200,height*4,QImage.Format.Format_ARGB32)
        painter=QPainter(gallery)
        modes=(*MODES,'developer')
        for row,mode in enumerate(modes):
            palette=Theme.PALETTES[mode] if mode!='developer' else {'BG_MID':'#fffdf5','TEXT':'#141414','CYAN':'#1b5a86'}
            top=row*height
            painter.fillRect(0,top,1200,height,QColor(palette['BG_MID']))
            painter.setPen(QColor(palette['TEXT']))
            font=painter.font();font.setPointSize(18);font.setBold(True);painter.setFont(font)
            painter.drawText(30,top+40,{'default':'Glass Dark','light':'Frosted Light','solarized_dark':'Solarized Dark','developer':'Developer Access'}[mode])
            font.setPointSize(9);font.setBold(False);painter.setFont(font)
            for index,name in enumerate(names):
                x=30+(index%8)*146;y=top+70+(index//8)*78
                rendered=icon(name,palette['TEXT'],size=32)
                self.assertFalse(rendered.isNull(),name)
                pixmap=rendered.pixmap(32,32)
                self.assertFalse(pixmap.isNull())
                image=pixmap.toImage()
                self.assertTrue(any(image.pixelColor(px,py).alpha() for py in range(image.height()) for px in range(image.width())),name)
                rendered.paint(painter,x,y,32,32)
                painter.drawText(x,y+52,name.replace('_',' '))
        painter.end()
        if ARGS.render_dir:
            ARGS.render_dir.mkdir(parents=True,exist_ok=True)
            self.assertTrue(gallery.save(str(ARGS.render_dir/'svg-family-all-themes.png')))


if __name__=='__main__':
    unittest.main(argv=[sys.argv[0],*EXTRA],verbosity=2)
