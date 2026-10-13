"""Tk-only palette/resize checks with disposable catalogs and no live writes."""
from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import font
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.tk_downloader_fixture import TkDownloaderFixture
from src.modules.tk_glass import DialogFit, GlassCard, BrowserHeader, NativeResizeFocus, _desktop_work_area, _native_frame, _x11_work_area, ui_scale
from src.modules.ui_metrics import WorkArea
from src.modules.ui_palette import contrast_ratio

SCALE = 1.0
RENDER_DIR = None


class NativeWorkAreaChecks(unittest.TestCase):
    def test_x11_reads_are_bounded_and_release_properties_and_display(self):
        import ctypes
        area = WorkArea(0, 0, 1366, 768)
        buffers = []
        incomplete = False

        def get_property(*args):
            values = [0] if args[2] == 1 else [0, 28, 1366, 700]
            buffer = (ctypes.c_ulong * len(values))(*values)
            buffers.append(buffer)
            args[7]._obj.value = 6  # CARDINAL
            args[8]._obj.value = 32
            args[9]._obj.value = len(values)
            args[10]._obj.value = 4 if incomplete and args[2] == 2 else 0
            args[11]._obj.value = ctypes.cast(buffer, ctypes.c_void_p).value
            return 0

        x11 = SimpleNamespace(XOpenDisplay=Mock(return_value=17), XCloseDisplay=Mock(),
            XDefaultRootWindow=Mock(return_value=19), XInternAtom=Mock(side_effect=lambda _d, name, _existing:
                1 if name == b"_NET_CURRENT_DESKTOP" else 2),
            XGetWindowProperty=Mock(side_effect=get_property), XFree=Mock())
        widget = Mock(winfo_screen=Mock(return_value=":fixture.0"))
        with patch("ctypes.CDLL", return_value=x11):
            self.assertEqual(_x11_work_area(widget, area), WorkArea(0, 28, 1366, 700))
            self.assertEqual([call.args[4] for call in x11.XGetWindowProperty.call_args_list], [1, 64])
            self.assertEqual(x11.XFree.call_count, 2)
            x11.XCloseDisplay.assert_called_once_with(17)
            incomplete = True
            self.assertEqual(_x11_work_area(widget, area), area)
            self.assertEqual(x11.XFree.call_count, 4)
            self.assertEqual(x11.XCloseDisplay.call_count, 2)

    def test_resize_focus_restoration_refuses_another_client_and_cancels_on_close(self):
        root = Mock()
        focused = Mock()
        root.focus_get.return_value = None
        focused.winfo_exists.return_value = True
        focused.winfo_viewable.return_value = True
        keeper = NativeResizeFocus(root)
        keeper._focused = focused
        with patch("src.modules.tk_glass._x11_focus_unset", return_value=False):
            keeper._restore()
        focused.focus_force.assert_not_called()
        with patch("src.modules.tk_glass._x11_focus_unset", return_value=True):
            keeper._restore()
        focused.focus_force.assert_called_once()
        keeper._pending = "fixture-callback"
        keeper._cancel(SimpleNamespace(widget=root))
        root.after_cancel.assert_called_once_with("fixture-callback")
        self.assertIsNone(keeper._pending)

    def test_ubuntu_desktop_panels_and_negative_origins_use_native_units(self):
        area = WorkArea(-1920, 0, 1920, 1080)
        self.assertEqual(_desktop_work_area(area, [2**32 - 1920, 28, 1920, 1012]),
                         WorkArea(-1920, 28, 1920, 1012))
        self.assertEqual(_desktop_work_area(area, [0, 0, 1920, 1080, -1920, 40, 1920, 1000], 1),
                         WorkArea(-1920, 40, 1920, 1000))
        self.assertEqual(_desktop_work_area(area, [-2040, 30, 2200, 1200]),
                         WorkArea(-1920, 30, 1920, 1050))

    def test_unavailable_or_malformed_desktop_hints_preserve_tk_bounds(self):
        area = WorkArea(0, 0, 1366, 768)
        for values, desktop in ((None, None), ([0, 0, 1], 0), ([0, 0, 0, 768], 0),
                                ([0, 0, 1366, -1], 0), ([2000, 0, 100, 100], 0),
                                ([0, 0, 1366, 768], 3), ([0, 0, 1366, 768] * 2, None)):
            with self.subTest(values=values, desktop=desktop):
                self.assertEqual(_desktop_work_area(area, values, desktop), area)
        with patch("ctypes.CDLL", side_effect=OSError("X11 unavailable")):
            self.assertEqual(_x11_work_area(None, area), area)


class AppearanceChecks(TkDownloaderFixture):
    def setUp(self):
        make_root = tk.Tk

        def scaled_root(*args, **kwargs):
            root = make_root(*args, **kwargs)
            root.tk.call("tk", "scaling", SCALE * 96 / 72)
            return root

        with patch.object(tk, "Tk", side_effect=scaled_root):
            super().setUp()

    def pump(self):
        # Native font/geometry propagation needs several coalesced paint turns
        # at large scales. Await actual idleness with a deadline, preserving the
        # assertions that static glass must settle without an animation timer.
        def painting(widget):
            return (getattr(widget, "_redraw_id", None) is not None
                    or any(painting(child) for child in widget.winfo_children()))
        deadline = time.monotonic() + 10
        earliest = time.monotonic() + .25
        quiet_since = None
        while time.monotonic() < deadline:
            self.root.update()
            if painting(self.root):
                quiet_since = None
            else:
                quiet_since = quiet_since or time.monotonic()
                # Header, empty-state and pane reflow all enqueue paint turns.
                # Require quiet longer than their longest 60 ms debounce.
                if time.monotonic() >= earliest and time.monotonic() - quiet_since >= .12:
                    return
            time.sleep(.025)
        def pending(widget):
            own = [f"{type(widget).__name__}:{widget.winfo_geometry()}"] if getattr(widget, "_redraw_id", None) else []
            return own + [item for child in widget.winfo_children() for item in pending(child)]
        self.fail(f"Native glass did not settle within 10 seconds: {pending(self.root)}")

    def show(self, width=1040, height=660):
        scale = ui_scale(self.root)
        width = min(round(width * scale), self.root.winfo_screenwidth() - 48)
        height = min(round(height * scale), self.root.winfo_screenheight() - 80, 688)
        self.root.geometry(f"{width}x{height}+20+20")
        if self.root.state() == "withdrawn":
            self.root.deiconify()
        self.pump()

    def test_themes_and_reflow_preserve_search_selection_version_and_focus(self):
        app = self.app
        versions = [dict(version=value, url="https://fixture.invalid/sensor.zip", size=1200)
                    for value in ("2.1.0", "1.0.0")]
        library = dict(name="Sensor toolkit", author="Fixture", maintainer="", category="Sensors",
                       architectures=["*"], sentence="Read device sensors.",
                       paragraph="Choose a release to prepare its archive.", versions=versions)
        app.lib_tab.populate({library["name"]: library})
        self.show()
        app.lib_tab.listbox.selection_set(0)
        app.lib_tab._on_select()
        app.lib_tab.version_combo.current(1)
        app.lib_tab.search_var.set("Sensor")
        app.lib_tab.search_entry.focus_force()
        self.pump()  # Receive native FocusIn before starting theme/resize preservation.
        self.assertIs(self.root.focus_get(), app.lib_tab.search_entry)
        expected_font = app.lib_tab.listbox.cget("font")
        for mode in ("light", "solarized_dark", "default"):
            with patch.object(self.module, "_resolve_lib_req_theme", return_value=mode):
                app._apply_current_theme()
            for width in (1040, 480, 1040):
                self.show(width)
                self.assertEqual(app.lib_tab.listbox.curselection(), (0,))
                self.assertEqual(app.lib_tab.search_var.get(), "Sensor")
                self.assertEqual(app.lib_tab.version_var.get(), "1.0.0")
                self.assertEqual(app.lib_tab.listbox.cget("font"), expected_font)
                self.assertGreaterEqual(contrast_ratio(app.lib_tab.listbox.cget("selectforeground"),
                                                      app.lib_tab.listbox.cget("selectbackground")), 4.5)
                for button in (app.browse_btn, app.sources_btn, app.refresh_btn, app.quit_btn):
                    image_name = str(button._svg_image)
                    image_width = int(self.root.tk.call("image", "width", image_name))
                    image_height = int(self.root.tk.call("image", "height", image_name))
                    for y in (2, image_height // 2, image_height - 3):
                        rgb = self.root.tk.call(image_name, "get", image_width // 2, y)
                        material = "#" + "".join(f"{int(channel):02x}" for channel in rgb)
                        self.assertGreaterEqual(contrast_ratio(button.cget("foreground"), material), 4.5)
                self.assertIs(self.root.focus_get(), app.lib_tab.search_entry)
                header = next(child for child in self.root.winfo_children() if isinstance(child, BrowserHeader))
                for item in header.find_withtag("subtitle"):
                    ink = header.itemcget(item, "fill")
                    rgb = self.root.tk.call(str(header._surface), "get", header.winfo_width() // 2, 2)
                    material = "#" + "".join(f"{int(channel):02x}" for channel in rgb)
                    self.assertGreaterEqual(contrast_ratio(ink, material), 4.5)
                for control in (app.folder_entry, app.browse_btn, app.sources_btn, app.refresh_btn,
                                app.quit_btn, app.lib_tab.version_combo, app.lib_tab.download_btn):
                    self.assertTrue(control.winfo_ismapped())
                    self.assertLessEqual(control.winfo_x() + control.winfo_width(), control.master.winfo_width() + 1,
                                         f"{control.winfo_class()} must fit its native row")
                detail = app.lib_tab.detail_canvas
                self.assertGreaterEqual(detail.winfo_height(), font.Font(root=self.root,
                    font=app.lib_tab.lbl_name.cget("font")).metrics("linespace"))
                for card in (app.lib_tab._responsive_panes.left, app.lib_tab._responsive_panes.right):
                    images = [item for item in card.find_withtag("glass") if card.type(item) == "image"]
                    self.assertEqual(len(images), 1, "Glass is one antialiased material, not stacked hard bands")
                    image_name = card.itemcget(images[0], "image")
                    center = card.winfo_width() // 2
                    top = self.root.tk.call(image_name, "get", center, 3)
                    bottom = self.root.tk.call(image_name, "get", center, max(3, card.winfo_height() - 4))
                    self.assertNotEqual(top, bottom, "The material must retain reflective depth")
                    self.assertLessEqual(len(card.find_withtag("glass")), 2, "Resize painting must stay bounded")
                    self.assertIsNone(card._redraw_id, "A settled surface must have no paint timer")
                self.assertGreaterEqual(contrast_ratio(self.module.Theme.TEXT_BRIGHT, self.module.Theme.BG_MID), 4.5)
        if RENDER_DIR:
            # Only this optional native capture uses Qt. The browser's glass
            # and SVG rasterizers remain standard-library-only before setup.
            import os
            os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "xcb")
            from PySide6.QtWidgets import QApplication
            capture_app = QApplication.instance() or QApplication([])
            for mode in ("default", "light", "solarized_dark"):
                with patch.object(self.module, "_resolve_lib_req_theme", return_value=mode):
                    app._apply_current_theme()
                self.show()
                capture_app.processEvents()
                image = capture_app.primaryScreen().grabWindow(int(self.root.winfo_id()))
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(str(RENDER_DIR / f"browser-{mode}-selected.png")))
                app.lib_tab.detail_canvas.pack_forget()
                app.lib_tab.detail_scroll.pack_forget()
                app.lib_tab.lbl_placeholder.pack(fill="both", expand=True)
                self.pump()
                self.assertTrue(capture_app.primaryScreen().grabWindow(int(self.root.winfo_id())).save(
                    str(RENDER_DIR / f"browser-{mode}-empty.png")))
                app.lib_tab._on_select()
                self.pump()
        self.show(480)
        app.sources_btn.invoke()
        self.pump()
        for control in (app.board_urls_entry, app.board_urls_apply_btn):
            self.assertTrue(control.winfo_ismapped())
            self.assertLessEqual(control.winfo_x() + control.winfo_width(), control.master.winfo_width() + 1)
        self.assertEqual(self.root.focus_get(), app.lib_tab.search_entry)

    def test_short_glass_panel_retains_a_complete_native_text_line(self):
        panel = tk.Toplevel(self.root)
        self.addCleanup(panel.destroy)
        card = GlassCard(panel, self.module.Theme, padding=9, expand=True, heading="Package details")
        card.pack(fill="both", expand=True)
        entry = tk.Entry(card.body, font=("Consolas", 10))
        entry.insert(0, "Selected release")
        entry.pack(fill="both", expand=True)
        scale = ui_scale(panel)
        for height in (160, 42, 160):
            panel.geometry(f"{round(360 * scale)}x{round(height * scale)}+40+40")
            self.pump()
            self.assertTrue(entry.winfo_ismapped())
            reading_height = font.Font(root=self.root, font=entry.cget("font")).metrics("linespace")
            padding = sum(entry.winfo_pixels(entry.cget(option)) for option in ("borderwidth", "highlightthickness"))
            self.assertGreaterEqual(entry.winfo_height(), reading_height + 2 * padding)
            self.assertEqual(entry.get(), "Selected release")
        panel.destroy()
        self.pump()

    def test_downloader_native_frame_fits_its_monitor_after_a_move(self):
        self.show()
        area = WorkArea(40, 40, 860, 640)
        with patch("src.modules.tk_glass.native_work_area", return_value=area):
            fitted = DialogFit(self.root, preferred=(1040, 740), minimum=(480, 400))
            self.pump()
            fitted._fit()
            self.pump()
            frame, _handle = _native_frame(self.root)
            self.assertGreaterEqual(frame.x, area.x)
            self.assertGreaterEqual(frame.y, area.y)
            self.assertLessEqual(frame.x + frame.width, area.x + area.width)
            self.assertLessEqual(frame.y + frame.height, area.y + area.height)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, choices=(1.0, 1.25, 1.5, 1.75, 2.0), default=1.0)
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()
    SCALE = args.scale
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (NativeWorkAreaChecks, AppearanceChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
