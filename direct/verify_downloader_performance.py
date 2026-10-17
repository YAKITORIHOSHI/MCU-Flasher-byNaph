"""Native downloader repaint/resource regressions, without timing assumptions.

Uses only disposable in-memory catalogs and the downloader's isolated Tk fixture.
No downloads, account access, installation, hardware or live settings are used.
Run native GUI verifiers serially: they intentionally exercise actual focus and
pointer bindings. A missing desktop is reported as a unittest skip.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from direct.tk_downloader_fixture import TkDownloaderFixture
from src.modules import tk_glass

SCALE = 1.0


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class GlassResizeSchedulingChecks(unittest.TestCase):
    def test_resize_burst_retires_old_draw_and_close_cancels_only_owned_callback(self):
        # Drive the actual scheduling/cleanup methods with a controlled Tcl
        # callback queue. No sleeps or desktop are needed to prove coalescing.
        for cls in (tk_glass.GlassScene, tk_glass.BrowserHeader,
                    tk_glass.GlassEmptyState, tk_glass.GlassCard):
            with self.subTest(surface=cls.__name__):
                widget = SimpleNamespace(_redraw_id=None, viewport=(100, 100))
                pending, cancelled, rendered = {}, [], []
                serial = 0

                def schedule(_delay, callback):
                    nonlocal serial
                    serial += 1
                    token = f"fixture-after-{serial}"
                    pending[token] = callback
                    return token

                def cancel(token):
                    cancelled.append(token)
                    pending.pop(token)

                def draw():
                    widget._redraw_id = None
                    rendered.append(widget.viewport)

                widget.after = Mock(side_effect=schedule)
                widget.after_cancel = Mock(side_effect=cancel)
                widget._draw = Mock(side_effect=draw)
                widget._layout = Mock()
                widget.winfo_viewable = Mock(return_value=True)
                mapped = SimpleNamespace(widget=widget, type=tk.EventType.Map)
                resized = SimpleNamespace(widget=widget, type=tk.EventType.Configure)
                child = SimpleNamespace(widget=object(), type=tk.EventType.Configure)
                cls._schedule(widget, mapped)
                initial = widget._redraw_id
                cls._schedule(widget)  # Repeated material refresh stays coalesced.
                cls._schedule(widget, child)  # A child layout must not defer the owner.
                self.assertEqual(widget._redraw_id, initial)
                self.assertEqual(widget.after.call_count, 1)
                widget._layout.assert_not_called()

                for count, width in enumerate(range(120, 600, 20), start=1):
                    previous = widget._redraw_id
                    widget.viewport = (width, 300)
                    cls._schedule(widget, resized)
                    self.assertEqual(cancelled[-1], previous)
                    self.assertNotIn(previous, pending)
                    self.assertEqual(tuple(pending), (widget._redraw_id,),
                                     "A resize burst must leave only its latest draw callback")
                    self.assertEqual(widget._layout.call_count, count,
                                     "Every self Configure must apply native geometry immediately")
                widget._draw.assert_not_called()
                pending.pop(widget._redraw_id)()
                self.assertEqual(rendered, [(580, 300)], "Only the settled viewport is painted")
                self.assertIsNone(widget._redraw_id)

                cls._schedule(widget, resized)
                owned = widget._redraw_id
                cls._cancel(widget, SimpleNamespace(widget=object()))
                self.assertEqual(tuple(pending), (owned,), "Child destruction must retain the owner's draw")
                cls._cancel(widget, SimpleNamespace(widget=widget))
                self.assertEqual(cancelled[-1], owned)
                self.assertEqual(pending, {}, "Closing the owner must retire its pending draw")
                self.assertIsNone(widget._redraw_id)
                cancel_count = widget.after_cancel.call_count
                cls._cancel(widget, SimpleNamespace(widget=widget))
                self.assertEqual(widget.after_cancel.call_count, cancel_count,
                                 "Repeated cleanup must not cancel an already retired callback")

                widget.winfo_viewable.return_value = False
                widget._layout.reset_mock()
                cls._schedule(widget, resized)
                widget._layout.assert_not_called()
                self.assertEqual(tuple(pending), (widget._redraw_id,),
                                 "Hidden Configure must retain a deferred geometry/material callback")
                cls._cancel(widget, SimpleNamespace(widget=widget))
                self.assertEqual(pending, {})


class DownloaderPerformanceChecks(TkDownloaderFixture):
    def setUp(self):
        make_root = tk.Tk

        def scaled_root(*args, **kwargs):
            root = make_root(*args, **kwargs)
            root.tk.call("tk", "scaling", SCALE * 96 / 72)
            return root

        with patch.object(tk, "Tk", side_effect=scaled_root):
            super().setUp()

        # Fill the native model directly to keep this suite about repaint work,
        # independent of worker scheduling, disk inventory and incremental load.
        # These names also provide the real browse tab's SVG row-icon metadata.
        names = [f"Fixture package {index:05}" for index in range(10_000)]
        tab = self.app.lib_tab
        tab.all_items = {name: {"name": name} for name in names}
        tab.filtered_names = names
        tab.loaded_count = len(names)
        tab.listbox.insert(tk.END, *names)
        width = min(round(1040 * SCALE), self.root.winfo_screenwidth() - 48)
        height = min(round(660 * SCALE), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{width}x{height}+20+20")
        self.root.deiconify()
        self.settle()

    def pending(self):
        return tuple(self.root.tk.splitlist(self.root.tk.call("after", "info")))

    def settle(self):
        """Wait for actual callbacks to drain, then require a quiet debounce turn."""
        deadline, quiet = time.monotonic() + 10, None
        while time.monotonic() < deadline:
            self.root.update()
            now = time.monotonic()
            if self.pending():
                quiet = None
            else:
                quiet = quiet or now
                if now - quiet >= .12:
                    return
            time.sleep(.01)
        self.fail(f"Downloader did not become idle; callbacks: {self.pending()}")

    def pointer_states(self):
        notebook = self.app.notebook
        for focused in (False, True):
            (notebook if focused else self.app.browse_btn).focus_force()
            self.root.update()
            for _index, left, top, right, bottom in notebook._tab_boxes:
                notebook._strip.event_generate("<Motion>", x=(left + right) // 2,
                                               y=(top + bottom) // 2)
                self.root.update()
            notebook._strip.event_generate("<Leave>", x=0, y=0)
            self.root.update()
        self.settle()

    def materials(self):
        """Record actual retained photos, including temporarily hidden pages."""
        return {str(widget): getattr(widget, "_material_image")
                for widget in descendants(self.root)
                if getattr(widget, "_material_image", None) is not None}

    def test_idle_has_no_repaint_callbacks_or_image_generation(self):
        self.assertEqual(self.pending(), (), "Static glass must stop scheduling when idle")
        notebook, listing = self.app.notebook, self.app.lib_tab.listbox
        with (patch.object(notebook, "refresh_material", wraps=notebook.refresh_material) as tabs,
              patch.object(listing, "_draw_surface", wraps=listing._draw_surface) as rows,
              patch.object(tk_glass, "resource_photo", wraps=tk_glass.resource_photo) as materials,
              patch.object(tk_glass, "glass_photo", wraps=tk_glass.glass_photo) as skins):
            for _ in range(20):
                self.root.update()
                time.sleep(.01)
            self.assertEqual((tabs.call_count, rows.call_count, materials.call_count, skins.call_count),
                             (0, 0, 0, 0), "Idle glass must not repaint or regenerate images")
            self.assertEqual(self.pending(), ())

    def test_resize_updates_native_scene_body_before_deferred_material_draw(self):
        scene = self.app._scene
        before = (scene.winfo_width(), scene.winfo_height())
        material = scene._material_image
        with patch.object(scene, "_draw", wraps=scene._draw) as raster:
            for delta in (20, 40, 60, 80):
                self.root.geometry(f"{max(500, before[0] - delta)}x"
                                   f"{max(400, before[1] - delta // 2)}+20+20")
                # Tcl's idle geometry/display pass intentionally excludes
                # timed callbacks. Check layout without running the raster.
                self.root.update_idletasks()
            actual = (scene.winfo_width(), scene.winfo_height())
            self.assertNotEqual(actual, before, "The native scene must actually resize")
            self.assertEqual((scene.body.winfo_width(), scene.body.winfo_height()), actual,
                             "The live native body must fill the latest scene before glass is baked")
            raster.assert_not_called()
            self.assertIs(scene._material_image, material)
            self.assertIsNotNone(scene._redraw_id, "The final material draw must remain deferred")
        self.settle()

    def test_cached_tab_hover_and_focus_only_repaint_the_strip(self):
        # Cache normal/hover/selected/focus variants before checking reuse.
        self.pointer_states()
        notebook = self.app.notebook
        before_materials = self.materials()
        before_fonts = set(self.root.tk.splitlist(self.root.tk.call("font", "names")))
        with ExitStack() as stack:
            page_refreshes = [stack.enter_context(patch.object(widget, "refresh_material",
                                                               wraps=widget.refresh_material))
                              for widget in descendants(notebook)
                              if hasattr(widget, "refresh_material")]
            materials = stack.enter_context(patch.object(tk_glass, "resource_photo",
                                                          wraps=tk_glass.resource_photo))
            skins = stack.enter_context(patch.object(tk_glass, "glass_photo", wraps=tk_glass.glass_photo))
            for _ in range(3):
                self.pointer_states()
            self.assertEqual(sum(mock.call_count for mock in page_refreshes), 0,
                             "Hover/focus must not refresh any page descendant")
            self.assertEqual(materials.call_count, 0, "Hover/focus must reuse large materials")
            # Moving focus away uses the real Browse button, whose native
            # focus skin may change. Measure the notebook's cached variants.
            tab_skins = [call for call in skins.call_args_list if call.args[0] is notebook]
            self.assertEqual(tab_skins, [], "Warmed tab state skins must be reused")
        self.assertEqual(self.materials(), before_materials)
        self.assertEqual(set(self.root.tk.splitlist(self.root.tk.call("font", "names"))), before_fonts,
                         "Tab pointer/focus input must retain existing native fonts")

    def test_scroll_only_paints_visible_rows_and_reuses_materials_images_and_fonts(self):
        listing = self.app.lib_tab.listbox
        # Warm the real selected/unselected SVG colors before measuring reuse.
        listing.selection_set(0)
        self.settle()
        listing.selection_clear(0)
        self.settle()
        before_materials = self.materials()
        before_images = set(self.root.tk.splitlist(self.root.tk.call("image", "names")))
        before_fonts = set(self.root.tk.splitlist(self.root.tk.call("font", "names")))
        initial_top = listing.nearest(0)
        with (patch.object(listing, "_row_colors", wraps=listing._row_colors) as colors,
              patch.object(tk_glass, "resource_photo", wraps=tk_glass.resource_photo) as materials,
              patch.object(tk_glass, "glass_photo", wraps=tk_glass.glass_photo) as skins):
            for _ in range(12):
                colors.reset_mock()
                listing._surface.event_generate("<MouseWheel>", delta=-120, x=30, y=30)
                self.root.update()
                self.assertGreater(colors.call_count, 0, "Actual wheel input must repaint the viewport")
                self.assertLessEqual(colors.call_count, 128,
                                     "One wheel frame must not visit the complete package catalog")
                for call in colors.call_args_list:
                    self.assertIsNotNone(listing.bbox(call.args[0]), "Only visible native rows need colors")
                text_rows = [item for item in listing._surface.find_all()
                             if listing._surface.type(item) == "text"]
                self.assertLessEqual(len(text_rows), 128)
            self.settle()
            self.assertEqual((materials.call_count, skins.call_count), (0, 0),
                             "Scrolling must reuse the owning material and cached native skins")
        self.assertGreater(listing.nearest(0), initial_top, "Wheel input must actually scroll")
        self.assertEqual(self.materials(), before_materials)
        self.assertEqual(set(self.root.tk.splitlist(self.root.tk.call("image", "names"))), before_images,
                         "Scrolling must reuse icons and materials, without allocating images")
        self.assertEqual(set(self.root.tk.splitlist(self.root.tk.call("font", "names"))), before_fonts,
                         "Scrolling must use existing native fonts")

    def test_warmed_tab_reselect_and_round_trips_reuse_full_materials(self):
        notebook = self.app.notebook
        # Native panes settle their first mapped geometry lazily. Warm every
        # page twice, ending at the same viewport used by the measured cycle.
        for _ in range(2):
            for tab in (*notebook.tabs(), notebook.tabs()[0]):
                notebook.select(tab)
                self.settle()
        before_materials = self.materials()
        with (patch.object(tk_glass, "resource_photo", wraps=tk_glass.resource_photo) as materials,
              patch.object(tk_glass, "glass_photo", wraps=tk_glass.glass_photo) as skins):
            for _ in range(3):
                for tab in (*notebook.tabs(), notebook.tabs()[0]):
                    notebook.select(tab)
                    self.settle()
                    notebook.select(tab)  # Reselecting the active tab is a no-op.
                    self.root.update()
            self.assertEqual(materials.call_count, 0,
                             "Returning to a settled tab must retain its current large material")
            self.assertEqual(skins.call_count, 0, "Round trips must retain cached tab/control skins")
        self.assertEqual(self.materials(), before_materials)
        self.assertEqual(self.pending(), ())

    def test_shared_empty_pane_does_not_rasterize_an_unused_full_background(self):
        with patch.object(tk_glass, "glass_photo", wraps=tk_glass.glass_photo) as skins:
            self.root.geometry(f"{max(500, self.root.winfo_width() - 80)}x"
                               f"{max(400, self.root.winfo_height() - 40)}+20+20")
            self.settle()
        unused = [call for call in skins.call_args_list
                  if isinstance(call.args[0], tk_glass.GlassEmptyState)
                  and getattr(call.args[0].palette, "RESOURCE_GLASS", False)]
        self.assertEqual(unused, [],
                         "Shared empty panes must display the owner without generating a discarded raster")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=float, choices=(1.0, 1.25, 1.5, 1.75, 2.0), default=1.0)
    args = parser.parse_args()
    SCALE = args.scale
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (GlassResizeSchedulingChecks, DownloaderPerformanceChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
