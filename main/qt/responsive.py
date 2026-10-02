"""Screen-aware sizing in Qt logical pixels; Qt handles physical DPI scaling."""
from PySide6.QtCore import QObject, QEvent, QRect, QTimer, Slot
from PySide6.QtGui import QGuiApplication, QCursor
from src.modules.ui_metrics import WorkArea, fit_rect, preferred_size


def active_screen(widget=None):
    if widget is not None:
        handle = widget.window().windowHandle()
        if handle is not None and handle.screen() is not None:
            return handle.screen()
        if widget.parentWidget() is not None:
            return active_screen(widget.parentWidget())
    return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()


def work_area(widget=None, screen=None):
    screen = screen or active_screen(widget)
    rect = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
    return WorkArea(rect.x(), rect.y(), rect.width(), rect.height())


def fit_dialog(widget, preferred=(760, 580), minimum=(360, 260)):
    area = work_area(widget)
    width, height = preferred_size(area, *preferred)
    widget.setMinimumSize(min(minimum[0], width), min(minimum[1], height))
    widget.resize(width, height)
    widget.move(area.x + (area.width - width) // 2, area.y + (area.height - height) // 2)


def clamp_window(widget, screen=None):
    if widget.isMaximized() or widget.isFullScreen():
        return
    area = work_area(widget, screen)
    client, frame = widget.geometry(), widget.frameGeometry()
    dx, dy = client.x() - frame.x(), client.y() - frame.y()
    fw, fh = max(0, frame.width() - client.width()), max(0, frame.height() - client.height())
    x, y, width, height = fit_rect(frame.x(), frame.y(), frame.width(), frame.height(), area)
    rect = QRect(x + dx, y + dy, max(1, width - fw), max(1, height - fh))
    if rect != client:
        widget.setGeometry(rect)


class ScreenWatcher(QObject):
    """Coalesce monitor/work-area/font changes; no polling or idle timer."""

    def __init__(self, widget, callback=None):
        super().__init__(widget)
        self.widget, self.callback = widget, callback
        self._screen = self._handle = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._refresh)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.FontChange):
            self._schedule()
        return False

    @Slot()
    def _schedule(self, *args):
        self._timer.start()

    @Slot()
    def _refresh(self):
        if not self.widget.isVisible():
            return
        handle = self.widget.windowHandle()
        if handle is not self._handle:
            if self._handle is not None:
                try:
                    self._handle.screenChanged.disconnect(self._schedule)
                except (RuntimeError, TypeError):
                    pass  # A recreated native window can invalidate its old handle.
            self._handle = handle
            if handle:
                handle.screenChanged.connect(self._schedule)
        screen = active_screen(self.widget)
        if screen is not self._screen:
            if self._screen:
                try:
                    for signal in (self._screen.availableGeometryChanged, self._screen.logicalDotsPerInchChanged):
                        signal.disconnect(self._schedule)
                except (RuntimeError, TypeError):
                    pass  # The previous monitor may already have been removed.
            self._screen = screen
            if screen:
                screen.availableGeometryChanged.connect(self._schedule)
                screen.logicalDotsPerInchChanged.connect(self._schedule)
        area = work_area(self.widget, screen)
        # Layouts may impose larger hints; individual forms reflow below.
        self.widget.setMinimumSize(min(self.widget.minimumWidth(), max(1, area.width - 24)),
                                   min(self.widget.minimumHeight(), max(1, area.height - 48)))
        if self.callback:
            self.callback(screen)
        clamp_window(self.widget, screen)
