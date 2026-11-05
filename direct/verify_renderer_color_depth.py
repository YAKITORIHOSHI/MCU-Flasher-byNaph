"""Chromium's low-end mode must not rasterize the application's pages at 4 bits per channel.

That mode reports 512 MB of RAM to Chromium, which selects RGBA4444 tiles for GPU raster:
on a PC it turned the navy editor teal and dithered the tab row's gradient into stripes.
Software rendering ignores the setting, so no `--disable-gpu` fixture can see the damage,
including this one. What can be proved without a GPU is that the opt-out reaches the real
renderer process, which is the process whose compositor settings read it. A person still
has to look at a low-end PC with a real GPU.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from src.modules import runtime_resources as resources

LOW_END_MODE = "--enable-low-end-device-mode"
FULL_COLOR = "--disable-rgba-4444-textures"
PROBE_SECONDS = 90
FLAT_PAGE = "#101b2a"


def low_end_flags(inherited: str = "--disable-gpu") -> str:
    profile = resources.performance_profile(4, 8, physical_cores=4)
    assert profile.low_end_renderer
    with patch.object(resources, "performance_profile", return_value=profile), \
            patch.dict(os.environ, {"QTWEBENGINE_CHROMIUM_FLAGS": inherited}):
        resources.configure_webengine_environment()
        return os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]


def probe() -> int:
    """Load a page and report which switches each real Chromium child was started with."""
    import psutil
    from PySide6.QtCore import QTimer
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication(["color-depth-probe"])
    profile = QWebEngineProfile()  # Off the record: nothing is written to disk.
    page = QWebEnginePage(profile)
    view = QWebEngineView()
    view.setPage(page)
    view.resize(320, 200)
    view.show()

    def report() -> None:
        switches = {}
        for child in psutil.Process().children(recursive=True):
            try:
                arguments = child.cmdline()
            except psutil.Error:
                continue
            kind = next((a.split("=", 1)[1] for a in arguments if a.startswith("--type=")), child.name())
            switches[kind] = sorted(a for a in arguments if a in (LOW_END_MODE, FULL_COLOR))
        print(json.dumps(switches), flush=True)
        app.exit(0)

    view.loadFinished.connect(lambda _ok: QTimer.singleShot(500, report))
    QTimer.singleShot((PROBE_SECONDS - 10) * 1000, lambda: app.exit(3))
    page.setHtml("<!DOCTYPE html><title>probe</title>")
    code = app.exec()
    view.close()
    view.setPage(None)
    del page
    app.processEvents()
    # Chromium children stop with the host; this process proved nothing about shutdown.
    os._exit(code)


def gpu_probe() -> int:
    """Show a flat page without --disable-gpu and report what it painted."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication(["color-depth-gpu"])
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile)
    view = QWebEngineView()
    view.setPage(page)
    view.resize(900, 320)
    view.show()

    def report() -> None:
        image = view.grab().toImage()
        surface = QColor(image.pixel(image.width() // 2, image.height() - 16))
        print(json.dumps({"surface": [surface.red(), surface.green(), surface.blue()],
                          "size": [image.width(), image.height()]}), flush=True)
        app.exit(0)

    view.loadFinished.connect(lambda _ok: QTimer.singleShot(3000, report))
    QTimer.singleShot((PROBE_SECONDS - 10) * 1000, lambda: app.exit(3))
    # A flat page: 4-bit tiles can only hold multiples of 17, so this navy cannot survive them.
    page.setHtml(f"<!DOCTYPE html><title>probe</title><body style='margin:0;background:{FLAT_PAGE}'>")
    code = app.exec()
    os._exit(code)


def compare_gpu() -> int:
    """For CI on a real host: this opens a window, so it is not part of the normal checks.

    Paints the page in low-end mode with and without the opt-out. If this host rasterizes
    with its GPU, the run without the opt-out shows the defect itself. If not, both match
    and the run can say nothing either way, which it reports rather than passing silently.
    """
    painted = {}
    shipped = low_end_flags("")
    for label, flags in (("as shipped", shipped), ("without the opt-out", shipped.replace(FULL_COLOR, ""))):
        result = subprocess.run([sys.executable, "-B", __file__, "--gpu-probe"], cwd=ROOT,
                                env=dict(os.environ, QTWEBENGINE_CHROMIUM_FLAGS=flags),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                                timeout=PROBE_SECONDS)
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        painted[label] = json.loads(lines[-1])["surface"] if lines else None
        print(f"{label}: {flags!r} painted {painted[label]} (exit {result.returncode})")
    expected = [16, 27, 42]  # FLAT_PAGE.
    if painted["as shipped"] in (None, [0, 0, 0]):
        print("No capture from this host, so nothing can be said about its GPU path.")
        return 0
    if painted["as shipped"] != expected:
        print(f"FAILED: the shipped flags painted {painted['as shipped']}, expected {expected}")
        return 1
    if painted["without the opt-out"] == expected:
        print("This host did not rasterize 4-bit tiles either way, so it cannot show the defect.")
    else:
        print("Reproduced on this host: the opt-out is what keeps pages on their real colors.")
    return 0


class ColorDepthChecks(unittest.TestCase):
    def test_low_end_mode_always_keeps_full_color(self):
        flags = low_end_flags().split()
        self.assertIn(LOW_END_MODE, flags)
        self.assertIn(FULL_COLOR, flags)
        self.assertEqual(flags.count(FULL_COLOR), 1)
        repeated = low_end_flags(" ".join(flags)).split()
        self.assertEqual(repeated.count(FULL_COLOR), 1, "repeated setup must not duplicate the switch")

    def test_the_real_renderer_is_started_with_the_opt_out(self):
        environment = dict(os.environ, QTWEBENGINE_CHROMIUM_FLAGS=low_end_flags(),
                           QT_QPA_PLATFORM=os.environ["QT_QPA_PLATFORM"])
        result = subprocess.run([sys.executable, "-B", __file__, "--probe"], cwd=ROOT, env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                errors="replace", timeout=PROBE_SECONDS)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        switches = json.loads(result.stdout.strip().splitlines()[-1])
        if "renderer" not in switches and "zygote" in switches:
            # A container's sandbox puts it in a namespace, leaving only the zygote visible.
            self.skipTest(f"this host hides its renderer from its parent: {switches}")
        self.assertIn("renderer", switches, f"no renderer found: {switches}")
        self.assertEqual(switches["renderer"], sorted((LOW_END_MODE, FULL_COLOR)))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        raise SystemExit(probe())
    if "--gpu-probe" in sys.argv:
        raise SystemExit(gpu_probe())
    if "--gpu" in sys.argv:
        raise SystemExit(compare_gpu())
    unittest.main(verbosity=2)
