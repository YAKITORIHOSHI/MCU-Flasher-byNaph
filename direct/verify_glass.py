"""The glass is real and readable: rendered pixels of real panes over the real scene.

Every surface shows a defocused photograph through it, so contrast is measured at the worst
point of each surface rather than assumed from palette values. The scene stays in the theme's
own colors and must look like a photograph out of focus, never like a smooth gradient.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QMainWindow, QMenuBar, QPlainTextEdit, QPushButton,
                               QStatusBar, QTabWidget, QToolBar, QVBoxLayout, QWidget)

from main.core.log_colors import contrast_ratio, glass_inks, themed_log_colors
from main.core.theme import Theme, glass_worst
from main.qt import glass
from main.qt.setup_components import GlassCard
from main.qt.theme import build_stylesheet, css_color, get_palette, glass_tokens, install_no_focus_lines

APP = QApplication.instance() or QApplication(["verify-glass"])
install_no_focus_lines(APP)
MODES = ("default", "light", "solarized_dark")
# Short and normal windows put the bars at different heights over the scene.
SIZES = ((1350, 694), (667, 326), (2560, 1440))


def at(image: QImage, x: float, y: float) -> QColor:
    """The pixel at logical coordinates, in a capture whose pixels are device pixels."""
    ratio = image.devicePixelRatio()
    return QColor(image.pixel(min(image.width() - 1, round(x * ratio)), min(image.height() - 1, round(y * ratio))))


def luminance(color: QColor) -> float:
    def linear(value):
        value /= 255
        return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
    return .2126 * linear(color.red()) + .7152 * linear(color.green()) + .0722 * linear(color.blue())


def contrast(first: float, second: float) -> float:
    high, low = max(first, second), min(first, second)
    return (high + .05) / (low + .05)


class Host(QMainWindow):
    """The workspace's bars with nothing written on them, so every pixel is pure surface."""

    def __init__(self, mode):
        super().__init__()
        Theme.apply_theme(mode)
        APP.setStyleSheet(build_stylesheet(mode))
        self.menu = QMenuBar(self)
        self.setMenuBar(self.menu)
        self.primary = QToolBar(self)
        self.primary.setObjectName("primary-toolbar")
        self.addToolBar(self.primary)
        self.addToolBarBreak()
        self.controls = QToolBar(self)
        self.controls.setObjectName("controls-toolbar")
        self.addToolBar(self.controls)
        filler = QWidget()
        filler.setFixedHeight(48)
        self.controls.addWidget(filler)
        self.setStatusBar(QStatusBar(self))
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("workspace-tabs")
        self.tabs.tabBar().setDrawBase(False)
        for _ in range(3):
            self.tabs.addTab(QWidget(), "")
        layout.addStretch(1)
        layout.addWidget(self.tabs, 1)
        page = self.tabs.widget(0)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.header = QWidget()
        self.header.setObjectName("console-header")
        self.header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.header.setFixedHeight(36)
        page_layout.addWidget(self.header)
        self.reading = QPlainTextEdit()
        self.reading.setObjectName("build-console")
        page_layout.addWidget(self.reading, 1)
        self.buttons = {}
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(4, 4, 4, 4)
        for name, enabled in (("btn-clean", True), ("btn-save", True), ("btn-stop", True), ("btn-compile", False)):
            button = QPushButton("")
            button.setObjectName(name)
            button.setFixedSize(60, 26)
            button.setEnabled(enabled)
            row_layout.addWidget(button)
            self.buttons[name] = button
        self.primary.addWidget(row)

    def paintEvent(self, event):
        radius = glass.pane_radius(self.height())
        glass.paint_scene(self, ((glass.bar_pane(self.primary, self), radius),
                                 (glass.bar_pane(self.controls, self), radius),
                                 (glass.pane_for(self.tabs, self), radius)))


class Surfaces(unittest.TestCase):
    def tearDown(self):
        Theme.apply_theme("default")
        APP.setStyleSheet("")

    def rendered(self, mode, size):
        host = Host(mode)
        self.addCleanup(host.deleteLater)
        host.resize(*size)
        host.show()
        for _ in range(4):
            APP.processEvents()
        return host, host.grab().toImage()

    def composite_at(self, host, x, y):
        return at(host._glass_scene._composite.toImage(), x, y)

    def worst(self, host, image, widget, ink, inset=8):
        """Lowest contrast between this ink and any part of the widget's surface."""
        top_left = widget.mapTo(host, QPoint())
        # Clear of the sheet's rim and rounded corners, which are not where text sits.
        rect = QRect(top_left, widget.size()).adjusted(inset + 8, min(inset, widget.height() // 3),
                                                       -(inset + 8), -min(inset, widget.height() // 3))
        target = luminance(QColor(ink))
        # Buttons on the bar are measured as buttons; their rims are not the bar's surface.
        children = [QRect(child.mapTo(host, QPoint()), child.size()).adjusted(-2, -2, 2, 2)
                    for child in widget.findChildren(QPushButton)]
        lowest = 99.0
        for y in range(rect.top(), rect.bottom() + 1, max(1, rect.height() // 6)):
            for x in range(rect.left(), rect.right() + 1, max(1, rect.width() // 24)):
                if not any(child.contains(x, y) for child in children):
                    lowest = min(lowest, contrast(target, luminance(at(image, x, y))))
        return lowest

    def test_text_stays_readable_on_every_surface_at_its_worst_point(self):
        for mode in MODES:
            palette = get_palette(mode)
            for size in SIZES:
                host, image = self.rendered(mode, size)
                surfaces = (("primary", host.primary, ("TEXT", "TEXT_BRIGHT")),
                            ("controls", host.controls, ("TEXT", "TEXT_BRIGHT")),
                            ("header", host.header, ("TEXT", "TEXT_BRIGHT")),
                            ("menu", host.menu, ("TEXT", "TEXT_DIM", "TEXT_BRIGHT")),
                            ("status", host.statusBar(), ("TEXT", "TEXT_DIM", "TEXT_BRIGHT")),
                            ("reading", host.reading.viewport(), ("TEXT", "TEXT_DIM", "TEXT_BRIGHT")))
                for name, widget, inks in surfaces:
                    for ink_name in inks:
                        with self.subTest(mode=mode, size=size, surface=name, ink=ink_name):
                            self.assertGreaterEqual(self.worst(host, image, widget, palette[ink_name]), 4.5)
                bar = host.tabs.tabBar()
                chip_ink = glass_tokens(mode)["chip_ink"]
                for index, ink in ((0, chip_ink), (1, palette["TEXT"])):
                    rect = bar.tabRect(index)
                    probe = QRect(bar.mapTo(host, rect.topLeft()), rect.size()).adjusted(8, 8, -8, -8)
                    target = luminance(QColor(ink))
                    lowest = min(contrast(target, luminance(at(image, x, y)))
                                 for y in range(probe.top(), probe.bottom(), 3) for x in range(probe.left(), probe.right(), 3))
                    with self.subTest(mode=mode, size=size, surface=f"tab {index}", ink=ink):
                        self.assertGreaterEqual(lowest, 4.5)
                host.close()

    def test_every_semantic_ink_clears_its_surface_at_its_hardest_point(self):
        for mode in MODES:
            worst = glass_worst(mode)
            for surface, colors in (("reading", themed_log_colors(mode)), ("sheet", glass_inks(mode))):
                for name, color in colors.items():
                    with self.subTest(mode=mode, surface=surface, ink=name):
                        self.assertGreaterEqual(contrast_ratio(color, worst[surface]), 4.5)

    def test_action_text_stays_readable_in_every_state(self):
        for mode in MODES:
            palette = get_palette(mode)
            disabled_ink = glass_tokens(mode)["disabled_ink"]
            host, image = self.rendered(mode, SIZES[0])
            for name, ink, floor in (("btn-clean", palette["TEXT_BRIGHT"], 4.5), ("btn-save", palette["TEXT_BRIGHT"], 4.5),
                                     ("btn-stop", palette["TEXT_BRIGHT"], 4.5), ("btn-compile", disabled_ink, 3.0)):
                with self.subTest(mode=mode, button=name):
                    self.assertGreaterEqual(self.worst(host, image, host.buttons[name], ink), floor)
            button = host.buttons["btn-clean"]
            button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
            button.update()
            APP.processEvents()
            hovered = host.grab().toImage()
            with self.subTest(mode=mode, state="hover"):
                self.assertGreaterEqual(self.worst(host, hovered, button, palette["TEXT_BRIGHT"]), 4.5)
            button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
            host.close()

    def test_a_pane_is_a_distinct_sheet_with_a_lit_rim_and_a_shadow(self):
        for mode in MODES:
            host, image = self.rendered(mode, SIZES[0])
            top = host.controls.geometry().top() + 1  # Where this bar's sheet begins.
            x = host.width() // 2
            column = [at(image, x, y).lightness() for y in range(top - 6, top + 10)]
            with self.subTest(mode=mode):
                self.assertGreaterEqual(max(column) - min(column), 6, "A pane must read as its own sheet")
            host.close()

    def test_the_scene_shows_through_a_reading_surface(self):
        # The defect this replaces: an opaque block covering most of the window, with
        # nothing behind it to see. Any flat fill makes the whole well one color.
        for mode in MODES:
            host, image = self.rendered(mode, SIZES[2])
            viewport = host.reading.viewport()
            origin = viewport.mapTo(host, QPoint())
            levels = [at(image, origin.x() + x, origin.y() + viewport.height() // 2).lightness()
                      for x in range(8, viewport.width() - 8, 16)]
            with self.subTest(mode=mode):
                self.assertGreaterEqual(max(levels) - min(levels), 6, "the scene must be visible through the well")
            host.close()

    def test_a_fill_is_never_applied_twice(self):
        # The frame and its viewport both take a stylesheet background, which would double
        # the smoke and leave a square block inside the rounded frame.
        for mode in MODES:
            host, image = self.rendered(mode, SIZES[0])
            viewport = host.reading.viewport()
            origin = viewport.mapTo(host, QPoint())
            red, green, blue, alpha = (float(part) for part in glass_tokens(mode)["reading"][5:-1].split(","))
            alpha /= 255
            for dx, dy in ((30, 20), (viewport.width() // 2, viewport.height() // 2)):
                x, y = origin.x() + dx, origin.y() + dy
                under, seen = self.composite_at(host, x, y), at(image, x, y)
                with self.subTest(mode=mode, point=(dx, dy)):
                    self.assertAlmostEqual(seen.red(), under.red() * (1 - alpha) + red * alpha, delta=3)
                    self.assertAlmostEqual(seen.green(), under.green() * (1 - alpha) + green * alpha, delta=3)
                    self.assertAlmostEqual(seen.blue(), under.blue() * (1 - alpha) + blue * alpha, delta=3)
            host.close()


class Scene(unittest.TestCase):
    def tearDown(self):
        Theme.apply_theme("default")

    def pixels(self, image):
        for y in range(0, image.height(), 3):
            for x in range(0, image.width(), 3):
                yield QColor(image.pixel(x, y))

    def test_every_theme_has_its_own_scene_on_disk(self):
        for mode in MODES:
            for frosted in (False, True):
                with self.subTest(mode=mode, frosted=frosted):
                    self.assertFalse(glass._source(mode, frosted).size() == QSize(16, 10), "fell back to a flat fill")
                    self.assertAlmostEqual(glass._source(mode, frosted).width() / glass._source(mode, frosted).height(),
                                           glass._SCENE_ASPECT)

    def test_a_missing_scene_leaves_the_theme_instead_of_failing(self):
        glass._source.cache_clear()
        self.addCleanup(glass._source.cache_clear)
        with patch.object(glass, "_ASSETS", ROOT / "temp" / "no-such-folder"):
            image = glass._source("default", False)
        self.assertEqual(QColor(image.pixel(0, 0)).name(), get_palette("default")["BG_DARK"])
        glass._source.cache_clear()

    def test_the_scene_stays_in_the_theme_with_no_color_wash(self):
        for mode in MODES:
            base = QColor(get_palette(mode)["BG_DARK"]).hslHue()
            total = counted = 0
            for color in self.pixels(glass._source(mode, True)):
                if color.hslSaturation() < 16:
                    continue  # Nearly gray: its hue is meaningless.
                drift = abs(color.hslHue() - base)
                total += min(drift, 360 - drift) <= 40
                counted += 1
            # A few lights may carry the accent. The picture as a whole may not.
            with self.subTest(mode=mode):
                self.assertGreaterEqual(total / max(1, counted), .9)

    def test_the_scene_is_a_picture_and_not_a_gradient(self):
        # A gradient has one rise or fall across the frame. A photograph of real things has many.
        for mode in MODES:
            image = glass._source(mode, False)
            for y in (image.height() // 4, image.height() // 2, image.height() * 3 // 4):
                levels = [QColor(image.pixel(x, y)).lightness() for x in range(0, image.width(), 8)]
                turns = sum(1 for before, here, after in zip(levels, levels[1:], levels[2:])
                            if (here - before) * (after - here) < 0)
                with self.subTest(mode=mode, row=y):
                    self.assertGreaterEqual(turns, 2)
                    self.assertGreaterEqual(max(levels) - min(levels), 16)

    def test_the_frosted_copy_is_softer_than_what_it_copies(self):
        for mode in MODES:
            sharp = glass._render_layer(QSize(640, 400), 1.0, mode).toImage()
            soft = glass._render_layer(QSize(640, 400), 1.0, mode, frosted=True).toImage()
            def roughness(image):
                return sum(abs(QColor(image.pixel(x, y)).lightness() - QColor(image.pixel(x + 4, y)).lightness())
                           for y in range(0, 400, 7) for x in range(0, 636, 4))
            with self.subTest(mode=mode):
                self.assertLess(roughness(soft), roughness(sharp))

    def test_the_hardest_surface_values_are_the_real_ones(self):
        # main/core/theme.py names these so inks can be chosen without loading any images.
        def linear(value):
            value /= 255
            return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
        def level(rgb):
            return .2126 * linear(rgb[0]) + .7152 * linear(rgb[1]) + .0722 * linear(rgb[2])
        def over(top, alpha, bottom):
            return tuple(top[index] * alpha + bottom[index] * (1 - alpha) for index in range(3))
        def token(value):
            red, green, blue, alpha = (float(part) for part in value[5:-1].split(","))
            return (red, green, blue), alpha / 255
        for mode in MODES:
            light = mode == "light"
            frost = None
            image = glass._source(mode, True)
            for y in range(image.height()):
                for x in range(image.width()):
                    pixel = image.pixel(x, y)
                    rgb = ((pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255)
                    if frost is None or (level(rgb) < level(frost) if light else level(rgb) > level(frost)):
                        frost = rgb
            white = (255, 255, 255)
            sheet = over(white, 92 / 255, frost) if light else over(
                white, glass.SHEET_TINT_TOP / 255, over(white, glass.SHEET_REFLECTION / 255, frost))
            tokens = glass_tokens(mode)
            reading = over(*token(tokens["reading"]), sheet)
            well = over(*token(tokens["well"]), sheet)
            for name, value in (("sheet", sheet), ("reading", reading), ("well", well)):
                recorded = QColor(glass_worst(mode)[name])
                with self.subTest(mode=mode, surface=name):
                    self.assertLessEqual(abs(recorded.red() - value[0]), 1.5)
                    self.assertLessEqual(abs(recorded.green() - value[1]), 1.5)
                    self.assertLessEqual(abs(recorded.blue() - value[2]), 1.5)

    def test_a_fractional_scale_leaves_no_edge_uncovered(self):
        for ratio in (1.25, 1.5, 1.75, 1.3125):
            for width, height in ((1351, 695), (667, 326), (1093, 576)):
                pixmap = glass._render_scene(QSize(width, height), ratio, "default")
                with self.subTest(ratio=ratio, size=(width, height)):
                    self.assertGreaterEqual(pixmap.width(), width * ratio)
                    self.assertGreaterEqual(pixmap.height(), height * ratio)

    def test_the_scene_fills_any_window_without_stretching(self):
        for width, height in ((1350, 694), (640, 400), (500, 1200), (3440, 1440), (320, 200)):
            scene_width, scene_height, left, top = glass.scene_fit(width, height)
            with self.subTest(size=(width, height)):
                self.assertAlmostEqual(scene_width / scene_height, glass._SCENE_ASPECT)
                self.assertLessEqual(left, 0)
                self.assertLessEqual(top, 0)
                self.assertGreaterEqual(scene_width + left + 1e-6, width)
                self.assertGreaterEqual(scene_height + top + 1e-6, height)

    def test_a_page_paints_the_scene_exactly_where_its_window_does(self):
        window = QWidget()
        self.addCleanup(window.deleteLater)
        window.resize(1000, 700)
        view = QWidget(window)
        view.setGeometry(30, 120, 400, 300)
        width, height, left, top = glass.scene_fit(1000, 700)
        backdrop = glass.page_backdrop_for(view)
        self.assertEqual((backdrop["width"], backdrop["height"]), (round(width, 2), round(height, 2)))
        self.assertEqual((backdrop["x"], backdrop["y"]), (round(left - 30, 2), round(top - 120, 2)))
        self.assertEqual(backdrop["radius"], glass.pane_radius(700))

    def test_a_page_hears_when_anything_above_it_moves(self):
        def settle():
            # A native window may report one geometry change in more than one step.
            for _ in range(6):
                APP.processEvents()
                QTest.qWait(10)

        window = QWidget()
        self.addCleanup(window.deleteLater)
        window.resize(1000, 700)
        window.show()
        holder = QWidget(window)
        holder.show()
        holder.setGeometry(0, 0, 900, 600)
        view = QWidget(holder)
        view.setGeometry(10, 10, 300, 200)
        view.show()
        settle()
        sent = []
        backdrop = glass.PageBackdrop(view, sent.append)
        self.assertEqual(sent, [glass.page_backdrop_for(view)])
        holder.move(0, 50)
        settle()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1], glass.page_backdrop_for(view))
        window.resize(1200, 800)
        settle()
        self.assertGreater(len(sent), 2)
        self.assertEqual(sent[-1], glass.page_backdrop_for(view))
        told = len(sent)
        holder.move(holder.pos())  # Nothing changed: no needless page call.
        settle()
        self.assertEqual(len(sent), told)
        backdrop.forget()  # A new page has been told nothing.
        self.assertEqual(len(sent), told + 1)

    def test_the_editor_page_is_the_same_glass(self):
        html = (ROOT / "src/editor/index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r"#app-container \{[^}]*var\(--frost\)[^}]*border-radius: var\(--scene-radius\);[^}]*overflow: hidden;")
        self.assertIn("var(--scene) var(--scene-x) var(--scene-y)", html)
        self.assertEqual(html.count("'editor.background': '#00000000'"), 3, "Monaco must not paint over the glass")
        self.assertEqual(html.count("'editorGutter.background': '#00000000'"), 3)
        self.assertIn(".monaco-editor { outline: none !important; }", html)
        for mode, selector in (("default", ":root.theme-default"), ("light", ":root.theme-light"),
                               ("solarized_dark", ":root.theme-solarized-dark")):
            block = html[html.index(selector):]
            block = block[:block.index("}")]
            tokens = glass_tokens(mode)
            with self.subTest(mode=mode):
                self.assertIn(f"url('../assets/glass/{mode}.png')", block)
                self.assertIn(f"url('../assets/glass/{mode}-frosted.png')", block)
                reading = css_color(tokens["reading"]).rsplit(",", 1)
                self.assertRegex(block, rf"--reading: {re.escape(reading[0])}, 0?\.\d+\);")
                self.assertIn(f"--chip: {tokens['chip']};", block)
                self.assertIn(f"--chip-ink: {tokens['chip_ink']};", block)

    def test_terminal_pages_are_the_same_glass(self):
        for name in ("terminal.html", "opencode.html"):
            html = (ROOT / "src/editor" / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn('src="glass_page.js"', html)
                self.assertIn("allowTransparency: true", html)
                self.assertIn("var(--frost) var(--scene-x) var(--scene-y)", html)

    def test_grain_breaks_bands_without_showing_as_texture(self):
        widget = QWidget()
        self.addCleanup(widget.deleteLater)
        widget.resize(600, 400)
        for mode in MODES:
            Theme.apply_theme(mode)
            canvas = QImage(600, 400, QImage.Format.Format_RGB32)
            painter = QPainter(canvas)
            try:
                glass.GlassScene(widget).paint(painter)
            finally:
                painter.end()
            jumps = [abs(QColor(canvas.pixel(x, 200)).green() - QColor(canvas.pixel(x + 1, 200)).green()) for x in range(599)]
            with self.subTest(mode=mode):
                self.assertLessEqual(max(jumps), 12)
                self.assertGreater(sum(jump > 0 for jump in jumps), 150, "a smooth picture with no grain will band")

    def test_the_scene_is_painted_at_device_pixels(self):
        pixmap = glass._render_scene(QSize(100, 50), 1.5, "default")
        self.assertEqual(pixmap.size(), QSize(150, 75))
        self.assertEqual(pixmap.devicePixelRatio(), 1.5)

    def test_a_veiled_window_is_one_readable_sheet(self):
        for mode in MODES:
            palette = get_palette(mode)
            image = glass._render_scene(QSize(800, 500), 1.0, mode, veiled=True).toImage()
            for name in ("TEXT", "TEXT_DIM"):
                target = luminance(QColor(palette[name]))
                lowest = min(contrast(target, luminance(color)) for color in self.pixels(image))
                with self.subTest(mode=mode, ink=name):
                    self.assertGreaterEqual(lowest, 4.5)

    def test_a_resize_burst_reuses_the_scene_and_a_new_theme_does_not_wait(self):
        widget = QWidget()
        self.addCleanup(widget.deleteLater)
        widget.resize(300, 200)
        scene = glass.GlassScene(widget)
        canvas = QImage(800, 800, QImage.Format.Format_RGB32)
        painter = QPainter(canvas)
        try:
            with patch.object(glass, "_render_scene", wraps=glass._render_scene) as render:
                scene.paint(painter)
                self.assertEqual(render.call_count, 1)
                for width in (310, 320, 330, 340):
                    widget.resize(width, 200)
                    scene.paint(painter)
                self.assertEqual(render.call_count, 1)
                self.assertTrue(scene._settle.isActive())
                Theme.apply_theme("light")
                scene.paint(painter)
                self.assertEqual(render.call_count, 2)
                Theme.apply_theme("default")
                scene._rebuild()
                scene.paint(painter)
                self.assertEqual(render.call_count, 3)
        finally:
            painter.end()

    def test_a_settled_window_repaints_with_one_image(self):
        widget = QWidget()
        self.addCleanup(widget.deleteLater)
        widget.resize(300, 200)
        scene = glass.GlassScene(widget)
        canvas = QImage(800, 800, QImage.Format.Format_RGB32)
        painter = QPainter(canvas)
        panes = ((QRect(10, 10, 200, 100), 12),)
        try:
            scene.paint(painter, panes)
            first = scene._composite
            with patch.object(glass, "_render_layer", side_effect=AssertionError("no rendering here")):
                scene.paint(painter, panes)
            self.assertIs(scene._composite, first)
        finally:
            painter.end()

    def test_a_sheet_is_the_frosted_scene_with_its_own_rim(self):
        widget = QWidget()
        self.addCleanup(widget.deleteLater)
        widget.resize(640, 400)
        for mode in MODES:
            Theme.apply_theme(mode)
            scene = glass.GlassScene(widget)
            canvas = QImage(640, 400, QImage.Format.Format_RGB32)
            painter = QPainter(canvas)
            try:
                scene.paint(painter, ((QRect(100, 100, 400, 200), 16),))
            finally:
                painter.end()
            inside = QColor(canvas.pixel(300, 200))
            frost = QColor(glass._render_layer(QSize(640, 400), 1.0, mode, frosted=True).toImage().pixel(300, 200))
            with self.subTest(mode=mode):
                self.assertLess(abs(inside.lightness() - frost.lightness()), 80, "the scene must show through the tint")
                self.assertGreaterEqual(inside.lightness(), frost.lightness() - 3, "the tint lifts, never darkens")


class Cards(unittest.TestCase):
    def tearDown(self):
        Theme.apply_theme("default")

    def surface(self, mode, size=(420, 220), ratio=1.0, radius=12):
        Theme.apply_theme(mode)
        card = GlassCard(None, get_palette(mode), radius=radius)
        self.addCleanup(card.deleteLater)
        card.resize(*size)
        return card._make_surface(ratio).toImage()

    def over(self, surface, backdrop):
        canvas = QImage(surface.size(), QImage.Format.Format_RGB32)
        canvas.setDevicePixelRatio(surface.devicePixelRatio())
        canvas.fill(QColor(backdrop))
        painter = QPainter(canvas)
        painter.drawImage(0, 0, surface)
        painter.end()
        return canvas

    def test_what_is_behind_a_card_shows_through_it(self):
        for mode in MODES:
            surface = self.surface(mode)
            first = QColor(self.over(surface, "#ff0000").pixel(210, 110))
            second = QColor(self.over(surface, "#0000ff").pixel(210, 110))
            with self.subTest(mode=mode):
                self.assertGreater(first.red() - second.red(), 40)
                self.assertGreater(second.blue() - first.blue(), 40)

    def test_nothing_inside_a_card_has_a_hard_edge(self):
        for mode in MODES:
            image = self.over(self.surface(mode), "#404040")
            # Clear of the rim and its rounded corners: only what fills the pane.
            for y in range(4, image.height() - 4, 2):
                jumps = [abs(QColor(image.pixel(x, y)).lightness() - QColor(image.pixel(x + 1, y)).lightness())
                         for x in range(16, image.width() - 17, 2)]
                with self.subTest(mode=mode, row=y):
                    self.assertLessEqual(max(jumps), 4)

    def test_the_rim_is_lit_from_above_and_one_device_pixel_wide(self):
        for mode in MODES:
            for ratio in (1.0, 1.25, 1.5, 2.0):
                image = self.over(self.surface(mode, ratio=ratio), "#404040")
                x = image.width() // 3
                column = [QColor(image.pixel(x, y)).lightness() for y in range(image.height() // 2)]
                # The rim is the first sharp step down the card: it stands out from both neighbors.
                rim = max(range(1, len(column) // 2), key=lambda y: column[y] - max(column[y - 1], column[y + 1]))
                with self.subTest(mode=mode, ratio=ratio):
                    self.assertGreaterEqual(column[rim] - column[rim - 1], 3, "lit against what is above it")
                    self.assertGreaterEqual(column[rim] - column[rim + 2], 3, "and not spread down the pane")
                    bottom = max(range(image.height() - 1, image.height() * 3 // 4, -1),
                                 key=lambda y: QColor(image.pixel(x, y)).lightness())
                    self.assertGreater(column[rim], QColor(image.pixel(x, bottom)).lightness(), "brighter at the top")


class Lines(unittest.TestCase):
    """The sources of stray lines. Rendered captures are checked by verify_monitor_matrix.py."""

    PANELS = sorted([*(ROOT / "main/qt").glob("*.py"), ROOT / "src/modules/bootstrap_qt.py"])

    def calls(self):
        for path in self.PANELS:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    yield path, node

    def test_no_stylesheet_without_a_selector_draws_a_one_sided_border(self):
        # It also applies to every child: each QLabel inside then draws its own partial line.
        for path, node in self.calls():
            if node.func.attr != "setStyleSheet" or not node.args:
                continue
            argument = node.args[0]
            parts = [argument.value] if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else (
                [value.value if isinstance(value, ast.Constant) else "" for value in argument.values]
                if isinstance(argument, ast.JoinedStr) else [])
            text = "".join(str(part) for part in parts)
            if text and "{" not in text:
                with self.subTest(file=path.name, line=node.lineno):
                    self.assertIsNone(re.search(r"border-(top|bottom|left|right)\s*:\s*(?!none)", text), text[:90])

    def test_document_mode_never_leaves_a_native_tab_bar_base_line(self):
        for path in self.PANELS:
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"^(\s*)self\.(\w+)\.setDocumentMode\(True\)", source, re.M):
                with self.subTest(file=path.name, tabs=match.group(2)):
                    self.assertIn(f"self.{match.group(2)}.tabBar().setDrawBase(False)", source)

    def test_frame_lines_are_styled_rather_than_drawn_by_the_native_style(self):
        stylesheet = build_stylesheet("default") + (ROOT / "main/qt/package_browser.py").read_text(encoding="utf-8")
        for path in self.PANELS:
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"^\s*(\w+)\.setFrameShape\(QFrame\.Shape\.(?:HLine|VLine)\)", source, re.M):
                name = match.group(1)
                found = re.search(rf"{name}\.setObjectName\(\"([\w-]+)\"\)", source[max(0, match.start() - 160):match.end() + 160])
                with self.subTest(file=path.name, line=source.count(chr(10), 0, match.start()) + 1):
                    self.assertIsNotNone(found, "a line with no objectName has no theme rule")
                    self.assertRegex(stylesheet, rf"QFrame#{found.group(1)} \{{\{{? ?background")

    def test_a_label_keeps_its_ampersand(self):
        source = (ROOT / "main/qt/settings_dialog.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'QGroupBox\("[^"]*[^&]&[^&][^"]*"\)', source))


if __name__ == "__main__":
    unittest.main(verbosity=1)
