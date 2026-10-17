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
from direct.tk_capture import save_tk_capture
from src.modules.tk_glass import DialogFit, GlassCard, BrowserHeader, NativeResizeFocus, _desktop_work_area, _native_frame, _x11_work_area, ui_scale
from src.modules.tk_material import GlassBody, GlassLabel, _owner
from src.modules.ui_metrics import WorkArea
from src.modules.ui_palette import contrast_ratio

SCALE = 1.0
RENDER_DIR = None
CAPTURE_APP = None


def capture(widget, filename):
    global CAPTURE_APP
    if sys.platform == "win32" and CAPTURE_APP is None:
        from PySide6.QtWidgets import QApplication
        CAPTURE_APP = QApplication.instance() or QApplication([])
    return save_tk_capture(widget, RENDER_DIR / filename, capture_app=CAPTURE_APP)


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
        root._redraw_id = None
        root.winfo_children.return_value = []
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
        focused.focus_force.reset_mock()
        focused.focus_set.reset_mock()
        root.focus_get.side_effect = [None, focused]
        keeper._restore()
        focused.focus_set.assert_called_once()
        focused.focus_force.assert_not_called()
        root.focus_get.side_effect = None
        late_material = Mock()
        late_material._redraw_id = "after-material"
        late_material.winfo_children.return_value = []
        root.winfo_children.return_value = [late_material]
        root.after.return_value = "after-focus-retry"
        with patch("src.modules.tk_glass._x11_focus_unset", return_value=True):
            keeper._restore()
            focused.focus_force.assert_not_called()
            root.after.assert_called_once_with(60, keeper._restore)
            self.assertEqual(keeper._pending, "after-focus-retry")
            late_material._redraw_id = None
            keeper._restore()
            focused.focus_force.assert_called_once()
        keeper._remember(SimpleNamespace(widget=focused))
        self.assertIs(keeper._focused, focused, "A stale FocusIn must not forget the last actual control")
        root.after.reset_mock()
        keeper._lost_focus(SimpleNamespace(widget=focused))
        root.after.assert_called_once_with(60, keeper._restore)
        self.assertEqual(keeper._pending, "after-focus-retry")
        keeper._pending = "fixture-callback"
        keeper._cancel(SimpleNamespace(widget=root))
        root.after_cancel.assert_called_once_with("fixture-callback")
        self.assertIsNone(keeper._pending)
        # ttk's native popdown can own focus without a Python widget wrapper.
        # Keep that focus occupied and avoid scheduling or forcing a repair.
        root.focus_get.side_effect = KeyError("native-popdown")
        root.after.reset_mock()
        focused.focus_force.reset_mock()
        keeper._remember(SimpleNamespace(widget=focused))
        keeper._lost_focus(SimpleNamespace(widget=focused))
        keeper._restore()
        root.after.assert_not_called()
        focused.focus_force.assert_not_called()

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

    def test_native_reading_font_and_keyboard_focus_ring(self):
        self.show()
        if sys.platform.startswith("linux"):
            self.assertEqual(font.Font(root=self.root, font=self.app.lib_tab.listbox.cget("font")).actual("family"),
                             "Montserrat", "Bundled UI fonts must reach Tk, not only Qt")
        button = self.app.refresh_btn
        button.focus_force()
        self.pump()
        self.assertEqual(button.winfo_pixels(button.cget("highlightthickness")), 0)
        self.assertEqual(button._glass_key[4], button.cget("highlightcolor"))
        self.app.lib_tab.search_entry.focus_force()
        self.pump()
        well = self.app.lib_tab._search_well
        self.assertEqual(well._surface_key[4], self.module.Theme.CYAN)

    def test_clear_control_corners_follow_the_owner_through_theme_and_resize(self):
        app = self.app
        literal = "Sketch \u03a9 $literal"
        app.lib_tab.search_var.set(literal)

        def assert_button_corners(button):
            owner = _owner(button)
            self.assertIsNotNone(owner, "A glass action needs an owning material")
            image, source = button._svg_image, owner._material_image
            x = button.winfo_rootx() - owner.winfo_rootx()
            y = button.winfo_rooty() - owner.winfo_rooty()
            for px, py in ((0, 0), (image.width()-1, 0),
                           (0, image.height()-1), (image.width()-1, image.height()-1)):
                self.assertEqual(image.get(px, py), source.get(x+px, y+py),
                                 "Rounded action corners must reveal the actual owner, without a flat matte")

        for mode in ("default", "light", "solarized_dark"):
            with patch.object(self.module, "_resolve_lib_req_theme", return_value=mode):
                app._apply_current_theme()
            for width in (1040, 480):
                self.show(width)
                for button in (app.sources_btn, app.refresh_btn, app.quit_btn, app.browse_btn):
                    assert_button_corners(button)
                for well in (app._folder_well, app.lib_tab._search_well):
                    owner = _owner(well)
                    self.assertIs(well._underlay_image, owner._material_image)
                    underlay, = well.find_withtag("material-underlay")
                    self.assertEqual(well.coords(underlay), [
                        owner.winfo_rootx()-well.winfo_rootx(), owner.winfo_rooty()-well.winfo_rooty()])
                    image = well._surface
                    for x, y in ((0, 0), (image.width()-1, 0),
                                 (0, image.height()-1), (image.width()-1, image.height()-1)):
                        self.assertTrue(image.transparency_get(x, y),
                                        "Native field corners must leave the shared material visible")
                    self.assertEqual(well.entry.cget("background"), self.module.Theme.FIELD_BG,
                                     "The native editing interior keeps its readable inset fill")
                self.assertEqual(app.lib_tab.search_entry.get(), literal)

        # State changes alter the action skin, while its outside corners still
        # belong to the same scene. No button command is invoked by this check.
        button = app.browse_btn
        button.event_generate("<Enter>")
        self.pump()
        assert_button_corners(button)
        button.configure(state="disabled")
        self.pump()
        assert_button_corners(button)
        button.configure(state="normal")
        button.event_generate("<Leave>")
        self.pump()
        assert_button_corners(button)

    def test_clear_list_preserves_native_pointer_keyboard_and_scroll_callbacks(self):
        tab = self.app.lib_tab
        tab._on_select_handler = Mock()
        tab.populate({f"Fixture package {index:03}": {"name": f"Fixture package {index:03}"}
                      for index in range(200)})
        self.show()
        listing = tab.listbox
        owner = _owner(listing._surface)
        self.assertIs(listing._surface_photo, owner._material_image)
        self.assertIs(listing._rail_photo, owner._material_image)
        self.assertEqual((listing._surface.winfo_x(), listing._surface.winfo_y()), (0, 0),
                         "The shared material must cover the native inactive border")
        for canvas in (listing._surface, listing._rail):
            material, = [item for item in canvas.find_all()
                         if canvas.type(item) == "image" and canvas.itemcget(item, "image") == str(owner._material_image)]
            self.assertEqual(canvas.coords(material), [
                owner.winfo_rootx()-canvas.winfo_rootx(), owner.winfo_rooty()-canvas.winfo_rooty()])

        changes, scrolls = [], []
        listing.bind("<<ListboxSelect>>", lambda _event: changes.append(listing.curselection()), add="+")
        def scrolled(first, last):
            scrolls.append((float(first), float(last)))
            tab._on_scroll(first, last)
        listing.configure(yscrollcommand=scrolled)
        _x, y, _width, height = listing.bbox(2)
        y += height // 2 - listing._surface.winfo_y()
        listing._surface.event_generate("<ButtonPress-1>", x=8, y=y, time=1000)
        listing._surface.event_generate("<ButtonRelease-1>", x=8, y=y, time=1001)
        self.pump()
        self.assertEqual(listing.curselection(), (2,))
        listing.focus_force()
        self.root.update()
        listing.event_generate("<KeyPress-Down>")
        self.pump()
        self.assertEqual(listing.curselection(), (3,))
        self.assertIn((2,), changes)
        self.assertIn((3,), changes)
        self.assertGreaterEqual(tab._on_select_handler.call_count, 2)
        rectangles = listing._surface.find_withtag("selection")
        self.assertEqual(len(rectangles), 1, "Only the chosen row gets a solid selection surface")
        self.assertEqual(listing._surface.itemcget(rectangles[0], "fill"), listing.cget("selectbackground"))
        focus_ring, = listing._surface.find_withtag("focus-ring")
        self.assertEqual(listing._surface.itemcget(focus_ring, "outline"), listing.cget("highlightcolor"))
        double_calls = []
        listing.bind("<Double-Button-1>", lambda _event: double_calls.append(listing.curselection()), add="+")
        _x, double_y, _width, line = listing.bbox(3)
        for timestamp in (3000, 3100):
            listing._surface.event_generate("<ButtonPress-1>", x=8, y=double_y+line//2, time=timestamp)
            listing._surface.event_generate("<ButtonRelease-1>", x=8, y=double_y+line//2, time=timestamp+1)
        self.pump()
        self.assertEqual(double_calls, [(3,)], "A double click invokes the native action once")
        before = listing.yview()[0]
        if sys.platform.startswith("linux"):
            listing._surface.event_generate("<Button-5>", x=8, y=10, time=2000)
        else:
            listing._surface.event_generate("<MouseWheel>", x=8, y=10, delta=-120)
        self.pump()
        self.assertGreater(listing.yview()[0], before)
        self.assertTrue(scrolls, "The native scroll callback must reach the original scrollbar")
        self.assertEqual(tab.list_scroll.get(), listing.yview())
        listing.yview_moveto(1)
        self.pump()
        self.assertGreater(listing.nearest(0), 0)
        self.assertLessEqual(sum(listing._surface.type(item) == "text" for item in listing._surface.find_all()), 128)
        self.assertIs(listing._surface_photo, owner._material_image)
        if listing.bind_class("Listbox", "<Button-2>"):
            listing.insert(tk.END, "Long package name " * 40)
            listing.xview_moveto(0)
            self.pump()
            listing._surface.event_generate("<ButtonPress-2>", x=150, y=10, time=5000)
            listing._surface.event_generate("<Motion>", x=20, y=10, state=512, time=5100)
            listing._surface.event_generate("<ButtonRelease-2>", x=20, y=10, time=5200)
            self.pump()
            self.assertGreater(listing.xview()[0], 0, "Native middle-button panning remains available")

    def test_clear_notebook_retains_native_selection_disabled_tabs_and_traversal(self):
        notebook = self.app.notebook
        self.assertFalse(notebook._style_ready,
                         "A hidden notebook must not tile its 1px placeholder across the page")
        self.show()
        self.assertTrue(notebook._style_ready)
        owner = _owner(notebook)
        self.assertIs(notebook._material_photo, owner._material_image)
        material = notebook._strip.find_all()[0]
        self.assertEqual(notebook._strip.itemcget(material, "image"), str(owner._material_image))
        x = notebook.winfo_rootx()-owner.winfo_rootx()
        y = notebook.winfo_rooty()-owner.winfo_rooty()
        self.assertEqual(notebook._strip.coords(material), [-x, -y])
        self.assertEqual(notebook._client_image.get(0, 0),
                         owner._material_image.get(x, y+notebook._strip.winfo_height()))
        def assert_tab_rims(selected, hovered=None):
            self.assertEqual(len(notebook._skin_photos), len(notebook._tab_boxes),
                             "Unselected tabs need their own visible rounded border")
            for image, (index, *_box) in zip(notebook._skin_photos, notebook._tab_boxes):
                rim = (self.module.Theme.CYAN if index == selected else
                       self.module.Theme.BORDER_LIT if index == hovered else self.module.Theme.BORDER)
                expected = tuple(part//257 for part in notebook.winfo_rgb(rim))
                self.assertEqual(image.get(image.width()//2, 0), expected)
                self.assertTrue(image.transparency_get(0, 0), "Tab corners reveal the shared scene")
        assert_tab_rims(0)
        _index, left, top, right, bottom = next(box for box in notebook._tab_boxes if box[0] == 1)
        notebook._strip.event_generate("<Motion>", x=(left+right)//2, y=(top+bottom)//2)
        self.pump()
        assert_tab_rims(0, 1)
        changes = []
        notebook.bind("<<NotebookTabChanged>>", lambda _event: changes.append(notebook.index(notebook.select())), add="+")

        def click_tab(index, timestamp):
            _index, left, top, right, bottom = next(box for box in notebook._tab_boxes if box[0] == index)
            px, py = (left+right)//2, (top+bottom)//2
            notebook._strip.event_generate("<ButtonPress-1>", x=px, y=py, time=timestamp)
            notebook._strip.event_generate("<ButtonRelease-1>", x=px, y=py, time=timestamp+1)
            self.pump()

        click_tab(1, 3000)
        self.assertEqual(notebook.index(notebook.select()), 1)
        self.assertIn(1, changes)
        notebook._strip.event_generate("<Leave>")
        self.pump()
        assert_tab_rims(1)
        notebook.tab(2, state="disabled")
        self.pump()
        click_tab(2, 4000)
        self.assertEqual(notebook.index(notebook.select()), 1, "An overlaid disabled tab must remain inactive")
        notebook.focus_force()
        self.root.update()
        notebook.event_generate("<Control-Tab>")
        self.pump()
        self.assertEqual(notebook.index(notebook.select()), 0,
                         "Native keyboard traversal must skip the disabled Installed tab")
        self.assertIs(self.root.focus_get(), notebook)

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
                header = app._header
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
                owner = app.lib_tab._responsive_panes.right
                for surface in (owner.body, app.lib_tab.detail_canvas, app.lib_tab._detail_content,
                                app.lib_tab.lbl_name, app.lib_tab.lbl_paragraph):
                    self.assertIsInstance(surface, (GlassBody, GlassLabel))
                    self.assertIs(surface._material_photo, owner._material_image,
                                  "Labels and native containers must continue the same glass after themes")
                    self.assertEqual(surface.coords(surface._material_item), [
                        surface.canvasx(0) + owner.winfo_rootx() - surface.winfo_rootx(),
                        surface.canvasy(0) + owner.winfo_rooty() - surface.winfo_rooty()])
                self.assertGreaterEqual(contrast_ratio(self.module.Theme.TEXT_BRIGHT, self.module.Theme.BG_MID), 4.5)
        if RENDER_DIR:
            # Read X11 Tk drawables directly; a non-null Qt grab can be black
            # under Xwayland. Windows retains its existing Qt capture backend.
            for mode in ("default", "light", "solarized_dark"):
                with patch.object(self.module, "_resolve_lib_req_theme", return_value=mode):
                    app._apply_current_theme()
                self.show()
                capture(self.root, f"browser-{mode}-selected.png")
                app.lib_tab.detail_canvas.pack_forget()
                app.lib_tab.detail_scroll.pack_forget()
                app.lib_tab.lbl_placeholder.pack(fill="both", expand=True)
                self.pump()
                capture(self.root, f"browser-{mode}-empty.png")
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
        # Production card parents use the theme's hex surface, rather than
        # the Windows Toplevel default's symbolic SystemButtonFace color.
        panel.configure(bg=self.module.Theme.WINDOW_BG)
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

    def test_short_glass_panel_retains_complete_svg_button_height(self):
        panel = tk.Toplevel(self.root)
        self.addCleanup(panel.destroy)
        # Match the themed parent used by the actual downloader cards.
        panel.configure(bg=self.module.Theme.WINDOW_BG)
        card = GlassCard(panel, self.module.Theme, padding=9, expand=True, heading="Package details")
        card.pack(fill="both", expand=True)
        action = self.module.make_flat_button(card.body, "Prepare board support", lambda: None,
                                              self.module.Theme.BTN_MONITOR, self.module.Theme.BTN_MONITOR_H)
        action.pack(anchor="w")
        panel.update_idletasks()
        required = action.winfo_reqheight()
        for height in (required + round(12 * ui_scale(panel)), required + 2, required + 80):
            panel.geometry(f"{round(360 * ui_scale(panel))}x{height}+40+40")
            self.pump()
            self.assertGreaterEqual(card.body.winfo_height(), required)
            self.assertGreaterEqual(action.winfo_height(), required)
            self.assertGreaterEqual(action.winfo_rooty(), card.body.winfo_rooty())
            self.assertLessEqual(action.winfo_rooty() + action.winfo_height(),
                                 card.body.winfo_rooty() + card.body.winfo_height())


class NativeMaterialChecks(unittest.TestCase):
    def test_shared_material_variable_scroll_bindings_and_close_cleanup(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk desktop unavailable: {error}")
        self.addCleanup(root.destroy)
        root.tk.call("tk", "scaling", SCALE * 96 / 72)
        root.geometry("420x280+20+20")
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args)
        owner = tk.Frame(root, bg="#182a38")
        owner.pack(fill="both", expand=True)
        owner._material_image = tk.PhotoImage(master=root, width=420, height=280)
        owner._material_image.put("#284858", to=(0, 0, 420, 280))
        owner._material_bounds = ("#284858", "#284858")
        value = tk.StringVar(master=root, value="Original metadata")
        label = GlassLabel(owner, textvariable=value, bg="#445566", fg="#ffffff", anchor="w")
        label.pack(fill="x", padx=13, pady=9)
        canvas = GlassBody(owner, bg="#445566", height=120)
        canvas.pack(fill="both", expand=True, padx=13)
        content = GlassBody(canvas, bg="#445566")
        tk.Entry(content).pack(fill="x")
        tk.Frame(content, width=1, height=900).pack()
        window = canvas.create_window(0, 0, window=content, anchor="nw")

        # These are replacement bindings, as used by the production details
        # view. They must not replace the material's owned geometry hooks.
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox(window)))
        root.update()

        def assert_material(widget):
            self.assertIs(widget._material_photo, owner._material_image)
            self.assertEqual(widget.itemcget(widget._material_item, "image"), str(owner._material_image))
            self.assertEqual(widget.coords(widget._material_item), [
                widget.canvasx(0) + owner.winfo_rootx() - widget.winfo_rootx(),
                widget.canvasy(0) + owner.winfo_rooty() - widget.winfo_rooty()])

        for widget in (label, canvas, content):
            assert_material(widget)
        self.assertEqual(label.cget("background"), "#445566", "Explicit bg must override inherited fallback")
        self.assertGreaterEqual(content.winfo_reqheight(), 900)
        region = tuple(map(float, canvas.cget("scrollregion").split()))
        self.assertEqual(region, tuple(map(float, canvas.bbox(window))))
        self.assertNotEqual(region, tuple(map(float, canvas.bbox("all"))),
                            "The shared background extends outside content and must not enlarge its scrollregion")

        literal = "Updated metadata Ω $literal — no callback argument loss"
        value.set(literal)
        root.update()
        self.assertEqual(label.itemcget(label._text_item, "text"), literal)
        self.assertEqual(label["text"], literal, "Label option access must not fall through to native Canvas options")
        canvas.yview_moveto(.5)
        root.update()
        self.assertGreater(canvas.yview()[0], 0)
        for widget in (label, canvas, content):
            assert_material(widget)
        self.assertEqual(tuple(map(float, canvas.cget("scrollregion").split())),
                         tuple(map(float, canvas.bbox(window))))

        owned_commands = [command for widget in (label, canvas, content)
                          for command in widget._material_bindings.values()]
        owned_commands.append(label._variable_trace[1])
        owner.destroy()
        root.update()
        value.set("After close")
        root.update()
        self.assertEqual(errors, [])
        self.assertEqual(value.trace_info(), [])
        for widget in (label, canvas, content):
            self.assertIsNone(widget._redraw_id)
        for command in owned_commands:
            self.assertFalse(root.tk.call("info", "commands", command), "Owned Tcl callbacks must be released")


class FileDownloaderChecks(unittest.TestCase):
    def test_themes_compact_scrolling_and_literal_fields_without_network(self):
        from src.modules import downloader
        from src.modules.tk_glass import ui_scale
        initialize = tk.Tk.__init__
        def scaled_initialize(window, *args, **kwargs):
            initialize(window, *args, **kwargs)
            window.withdraw()
            window.tk.call("tk", "scaling", SCALE * 96 / 72)
        for mode in ("default", "light", "solarized_dark"):
            with self.subTest(theme=mode), patch.object(tk.Tk, "__init__", scaled_initialize), \
                    patch.object(downloader, "check_internet_connection", side_effect=AssertionError("Network probe")), \
                    patch.object(downloader.DownloaderApp, "_run_download", side_effect=AssertionError("Download started")):
                window = downloader.DownloaderApp(theme_mode=mode)
                errors = []
                window.report_callback_exception = lambda *args: errors.append(args)
                try:
                    value = "https://fixture.invalid/Sketch%20Ω?literal=$value"
                    window.url_entry.insert(0, value)
                    window.download_dir.set(str(ROOT / "temp/audit/download-design/Save Ω $literal"))
                    folder = window.download_dir.get()
                    for width, height in ((720, 460), (420, 320), (720, 460)):
                        scale = ui_scale(window)
                        window.geometry(f"{min(round(width * scale), window.winfo_screenwidth() - 40)}x"
                                        f"{min(round(height * scale), window.winfo_screenheight() - 40)}+10+10")
                        window.deiconify()
                        deadline = time.monotonic() + .5
                        while time.monotonic() < deadline:
                            window.update()
                            time.sleep(.02)
                        window.download_btn.focus_force()
                        window.update()
                        self.assertEqual(window.url_entry.get(), value)
                        self.assertEqual(window.download_dir.get(), folder)
                        self.assertTrue(window.download_btn.winfo_ismapped())
                        canvas = window._form.canvas
                        self.assertGreaterEqual(window.download_btn.winfo_rooty(), canvas.winfo_rooty())
                        self.assertLessEqual(window.download_btn.winfo_rooty() + window.download_btn.winfo_height(),
                                             canvas.winfo_rooty() + canvas.winfo_height() + 1)
                        for control in (window.url_entry, window.folder_entry, window.browse_btn, window.download_btn):
                            self.assertLessEqual(control.winfo_x() + control.winfo_width(), control.master.winfo_width() + 1)
                        if RENDER_DIR:
                            capture(window, f"file-downloader-{mode}-{width}.png")
                    self.assertEqual(errors, [])
                finally:
                    window.destroy()


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
                               for cls in (NativeWorkAreaChecks, NativeMaterialChecks, AppearanceChecks, FileDownloaderChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
