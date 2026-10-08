"""Collect Python cycles between Qt events, never from an arbitrary worker.

Reference-counted cleanup remains immediate. Automatic cyclic collection can
otherwise finalize Qt wrappers inside a virtual eventFilter call or a worker's
allocation. The workspace owns this collector for the lifetime of QApplication.
"""
import gc
import time

from PySide6.QtCore import QObject, QThread, QTimer, Slot
from PySide6.QtWidgets import QApplication


class GuiGarbageCollector(QObject):
    def __init__(self, app):
        if QThread.currentThread() != app.thread():
            raise RuntimeError("Install cyclic collection on the Qt application thread")
        super().__init__(app)
        self._app = app
        self._previously_enabled = gc.isenabled()
        self._stopped = False
        self._collecting = False
        self._last_full = time.monotonic()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.collect_pending)
        app.aboutToQuit.connect(self.stop)
        self._timer.start()

    @Slot()
    def collect_pending(self):
        if (self._stopped or self._collecting
                or QThread.currentThread() != self._app.thread()):
            return
        # Do not finalize objects from the suspended outer event while a modal
        # dialog runs its nested event loop.
        if self._app.thread().loopLevel() > 1 or QApplication.activeModalWidget() is not None:
            return
        full = time.monotonic() - self._last_full >= 60
        if not full and gc.get_count()[0] < gc.get_threshold()[0]:
            return
        self._collecting = True
        try:
            gc.collect(2 if full else 0)
            if full:
                self._last_full = time.monotonic()
        finally:
            self._collecting = False

    @Slot()
    def stop(self):
        """Retire the timer; restore GC only after QApplication has returned."""
        self._timer.stop()
        self._stopped = True

    def restore(self):
        self.stop()
        if self._previously_enabled:
            gc.enable()


def install_gui_garbage_collector(app):
    collector = getattr(app, "_mcu_cycle_collector", None)
    if collector is None:
        collector = GuiGarbageCollector(app)
        app._mcu_cycle_collector = collector
    return collector
