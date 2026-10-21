"""Native downloader repaint/resource regressions, without timing assumptions.

Uses only disposable in-memory catalogs and the downloader's isolated Tk fixture.
No downloads, account access, installation, hardware or live settings are used.
Run native GUI verifiers serially: they intentionally exercise actual focus and
pointer bindings. A missing desktop is reported as a unittest skip.
"""
from __future__ import annotations

import argparse
import gc
from contextlib import ExitStack
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
import weakref
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


class RasterWorkerChecks(unittest.TestCase):
    def take(self, worker):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = worker.take()
            if result is not None:
                return result
            time.sleep(.002)
        self.fail("The bounded encoder did not complete")

    def test_worker_keeps_latest_per_surface_and_one_completed_result(self):
        from src.modules.tk_raster import RasterQueue
        entered, release, seen, cancelled = threading.Event(), threading.Event(), [], []

        def encode(value, cancel):
            seen.append(value)
            if value == b"old":
                entered.set()
                release.wait(3)
                cancelled.append(cancel.is_set())
            return value

        worker = RasterQueue(encode)
        self.addCleanup(worker.close)
        try:
            worker.request(1, 1, b"old")
            self.assertTrue(entered.wait(2))
            worker.request(1, 2, b"superseded")
            worker.request(1, 3, b"latest")
            worker.request(2, 4, b"other surface")
            release.set()
            deadline = time.monotonic() + 2
            while len(seen) < 2 and time.monotonic() < deadline:
                time.sleep(.002)
            time.sleep(.02)
            self.assertEqual(seen, [b"old", b"latest"],
                             "One completed PNG must backpressure the next encode")
            self.assertEqual(self.take(worker), (1, 3, b"latest", None))
            self.assertEqual(self.take(worker), (2, 4, b"other surface", None))
            self.assertEqual(cancelled, [True], "A newer request must cancel stale CPU work")
        finally:
            release.set()

    def test_worker_bounds_requests_and_completed_bytes_and_recovers_start_failure(self):
        from src.modules.tk_raster import RasterQueue
        entered, release = threading.Event(), threading.Event()

        def encode(value, _cancel):
            entered.set()
            release.wait(3)
            return value

        worker = RasterQueue(encode)
        self.addCleanup(worker.close)
        try:
            worker.request(0, 0, b"zero")
            self.assertTrue(entered.wait(2))
            for token in range(1, worker.MAX_SURFACES):
                worker.request(token, token, b"bounded")
            with self.assertRaisesRegex(RuntimeError, "Too many"):
                worker.request(worker.MAX_SURFACES, 17, b"refused")
            worker.close()
        finally:
            release.set()
        deadline = time.monotonic() + 2
        while worker.busy() and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertFalse(worker.busy(), "Closing must retire the encoder without a UI callback")
        self.assertIsNone(worker.take(), "Closing must discard pending and in-flight results")

        worker = RasterQueue(lambda value, _cancel: value)
        self.addCleanup(worker.close)
        for replacement in (Mock(side_effect=RuntimeError("no thread")),
                            Mock(return_value=SimpleNamespace(start=Mock(side_effect=RuntimeError("no thread"))))):
            with patch("src.modules.tk_raster.threading.Thread", replacement):
                with self.assertRaisesRegex(RuntimeError, "no thread"):
                    worker.request(1, 1, b"first")
            self.assertFalse(worker.busy())
        worker.MAX_RESULT_BYTES = 2
        worker.request(1, 2, b"oversized")
        token, generation, data, error = self.take(worker)
        self.assertEqual((token, generation, data), (1, 2, None))
        self.assertIn("PNG budget", error)
        worker.request(1, 3, b"ok")
        self.assertEqual(self.take(worker), (1, 3, b"ok", None))

    def test_cooperative_renderer_preserves_exact_png_bytes(self):
        palette = SimpleNamespace(BG_LIGHT="#1c2532", BG_MID="#161d27", BG_DARK="#10151c",
                                  BG_DARKEST="#0a0e14", BORDER="#2a3545", BORDER_LIT="#3d5068")
        stops = tuple(getattr(palette, key) for key in ("BG_LIGHT", "BG_MID", "BG_DARK", "BG_DARKEST"))
        args = (360, 240, stops, palette.BORDER, palette.BORDER_LIT, palette.BG_DARKEST, 12, False)
        expected = tk_glass.resource_glass_png.__wrapped__(*args)
        self.assertEqual(tk_glass._encode_resource(args), expected,
                         "Scheduling must not change even one encoded material byte")

    def test_delivery_releases_png_when_current_surface_no_longer_needs_it(self):
        from src.modules.tk_raster import ResourceRaster

        class Surface:
            _redraw_id = None

            def winfo_exists(self):
                return True

            winfo_viewable = winfo_exists

            def _draw(self):
                # Returning to a cached size, or a now-short viewport, can
                # legitimately leave a delivered PNG unconsumed by _draw.
                if getattr(self, "fail", False):
                    raise ValueError("fixture native image failure")

        surface = Surface()
        delivery = ResourceRaster.__new__(ResourceRaster)
        delivery._targets = {1: (weakref.ref(surface), 2, "stale viewport")}
        delivery._accept((1, 2, b"completed bytes", None))
        self.assertIsNone(surface._resource_ready,
                          "A no-op draw must not retain another large completed PNG")
        surface.fail = True
        delivery._targets = {1: (weakref.ref(surface), 3, "current viewport")}
        with self.assertRaisesRegex(ValueError, "native image failure"):
            delivery._accept((1, 3, b"completed bytes", None))
        self.assertIsNone(surface._resource_ready,
                          "Failed native image creation must release its completed bytes")

    def test_each_surface_cache_hit_cancels_unneeded_in_flight_work(self):
        from src.modules.tk_raster import ResourceRaster

        class CachedKey:
            def __eq__(self, other):
                return True

            def __ne__(self, other):
                return False

        class Cancelled(Exception):
            pass

        palette = SimpleNamespace(BG_LIGHT="#1c2532", BG_MID="#161d27", BG_DARK="#10151c",
                                  BG_DARKEST="#0a0e14", BORDER="#2a3545", BORDER_LIT="#3d5068",
                                  WINDOW_BG="#0a0e14", CYAN="#00aabb", RESOURCE_GLASS=True)
        for cls, layout in ((tk_glass.GlassScene, (100, 100)),
                            (tk_glass.GlassCard, (100, 100, 0)),
                            (tk_glass.BrowserHeader, (100, 100, 4, 16, 18, 20, 18, False, 3))):
            with self.subTest(surface=cls.__name__):
                queue = Mock()
                renderer = ResourceRaster.__new__(ResourceRaster)
                renderer._queue, renderer._targets = queue, {}
                root = SimpleNamespace(_resource_raster=renderer)
                widget = SimpleNamespace(_root=lambda: root, _layout=lambda: layout, palette=palette,
                                         winfo_viewable=lambda: True, winfo_fpixels=lambda _value: 96,
                                         _surface_key=CachedKey(), cget=lambda _value: palette.WINDOW_BG,
                                         framed=True, _resource_request="old request",
                                         _resource_ready=("stale", b"large completed PNG"))
                renderer._targets[id(widget)] = "pending"

                def discard(target):
                    ResourceRaster.discard(renderer, target)
                    # Stop before native painting; this fixture needs no Tcl.
                    raise Cancelled()

                renderer.discard = discard
                with patch.object(tk_glass, "_material_underlay"):
                    with self.assertRaises(Cancelled):
                        cls._draw(widget)
                queue.discard.assert_called_once_with(id(widget))
                self.assertIsNone(widget._resource_request)
                self.assertIsNone(widget._resource_ready)
                self.assertNotIn(id(widget), renderer._targets)


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

    def test_large_material_encoding_does_not_block_tk_or_replace_previous_image(self):
        scene, entered, release = self.app._scene, threading.Event(), threading.Event()
        original = tk_glass.resource_glass_png.__wrapped__
        threads = []

        def delayed(*args, **kwargs):
            threads.append(threading.get_ident())
            entered.set()
            if threading.current_thread() is not threading.main_thread():
                release.wait(3)
            return original(*args, **kwargs)

        previous = scene._material_image
        with patch.object(tk_glass.resource_glass_png, "__wrapped__", delayed):
            try:
                self.root.geometry(f"{self.root.winfo_width() - 40}x"
                                   f"{self.root.winfo_height() - 20}+20+20")
                self.root.update_idletasks()
                scene._draw()
                self.assertTrue(entered.wait(2), "The large raster must start")
                self.assertNotIn(threading.get_ident(), threads,
                                 "PNG generation must not execute on the Tk thread")
                self.assertIs(scene._material_image, previous,
                              "Keep the previous image while the worker paints")
                marker = []
                self.root.after(0, lambda: marker.append(True))
                self.root.update()
                self.assertEqual(marker, [True], "Tk must deliver input while encoding is blocked")
            finally:
                release.set()
            self.settle()
        self.assertIsNot(scene._material_image, previous)
        self.assertEqual((scene._material_image.width(), scene._material_image.height()),
                         (scene.winfo_width(), scene.winfo_height()))
        self.assertEqual(self.pending(), (), "The worker delivery timer must stop when idle")

    def test_button_refresh_reuses_native_font_and_observes_named_font_changes(self):
        from tkinter import font as tkfont
        button = self.app.refresh_btn
        named = tkfont.Font(root=self.root, family="Montserrat", size=9, weight="bold")
        button.configure(font=named)
        self.settle()
        width = button.winfo_reqwidth()
        with patch.object(tk_glass.tkfont, "Font", wraps=tkfont.Font) as clones:
            for _ in range(10):
                button._refresh_svg()
            self.assertEqual(clones.call_count, 0,
                             "Unchanged input must not clone/delete a Tcl font")
            named.configure(size=15)
            button._refresh_svg()
            self.assertGreater(button.winfo_reqwidth(), width,
                               "A named font change must still resize its native skin")

    def test_hidden_and_superseded_materials_are_discarded_then_map_renders_current_theme(self):
        scene, entered, release = self.app._scene, threading.Event(), threading.Event()
        original = tk_glass._encode_resource

        def delayed(args, cancel=None):
            entered.set()
            release.wait(3)
            return original(args, cancel)

        previous, palette = scene._material_image, scene.palette
        with patch.object(tk_glass, "_encode_resource", delayed):
            # Existing controllers retain a CPU-only module entry; replace it
            # here to gate an actual native request rather than starting a UI.
            renderer = self.root._resource_raster
            with patch.object(renderer._queue, "encode", delayed):
                try:
                    self.root.geometry(f"{self.root.winfo_width() - 80}x"
                                       f"{self.root.winfo_height() - 40}+20+20")
                    self.root.update_idletasks()
                    scene._draw()
                    self.assertTrue(entered.wait(2))
                    current = SimpleNamespace(**{key: getattr(palette, key) for key in
                                              ("BG_LIGHT", "BG_MID", "BG_DARK", "BG_DARKEST",
                                               "BORDER", "BORDER_LIT", "WINDOW_BG")})
                    current.BG_LIGHT = "#243140"
                    scene.palette = current
                    scene._draw()
                    self.root.withdraw()
                    self.root.update()
                finally:
                    release.set()
                self.settle()
                self.assertIs(scene._material_image, previous, "A hidden surface must retain its old image")
                self.assertEqual(self.pending(), (), "Hidden results must not leave a polling timer")
            self.root.deiconify()
            self.settle()
        self.assertIsNot(scene._material_image, previous)
        self.assertEqual(scene._surface_key[2], current.BG_LIGHT,
                         "Reopening must accept only the current palette")
        self.assertEqual((scene._material_image.width(), scene._material_image.height()),
                         (scene.winfo_width(), scene.winfo_height()))
        scene.palette = palette

    def test_material_worker_start_failure_reports_once_and_next_draw_can_retry(self):
        scene = self.app._scene
        previous = scene._material_image
        self.root.geometry(f"{self.root.winfo_width() - 40}x"
                           f"{self.root.winfo_height() - 20}+20+20")
        self.root.update_idletasks()
        with (patch("src.modules.tk_raster.threading.Thread", side_effect=RuntimeError("fixture start")),
              patch.object(self.root, "report_callback_exception") as reported):
            scene._draw()
            self.assertEqual(reported.call_count, 1)
            self.assertIs(scene._material_image, previous)
            self.assertIsNone(scene._resource_request)
        scene._draw()
        self.settle()
        self.assertIsNot(scene._material_image, previous)

    def test_destroying_surface_cancels_active_work_without_retaining_native_widgets(self):
        entered, release = threading.Event(), threading.Event()
        original = tk_glass._encode_resource

        def delayed(args, cancel=None):
            entered.set()
            release.wait(3)
            return original(args, cancel)

        dialog = tk.Toplevel(self.root)
        dialog.geometry("600x420+40+40")
        scene = tk_glass.GlassScene(dialog, self.module.Theme)
        scene.pack(fill="both", expand=True)
        self.root.update_idletasks()
        reference = weakref.ref(scene)
        with patch.object(self.root._resource_raster._queue, "encode", delayed):
            try:
                scene._draw()
                self.assertTrue(entered.wait(2))
                dialog.destroy()
                scene = dialog = None
                gc.collect()
                self.assertIsNone(reference(), "The encoder must not retain a destroyed Tcl widget")
            finally:
                release.set()
            self.settle()
        self.assertEqual(self.pending(), ())

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
                               for cls in (GlassResizeSchedulingChecks, RasterWorkerChecks, DownloaderPerformanceChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
