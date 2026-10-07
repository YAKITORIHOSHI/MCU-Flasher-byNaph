"""Window-owned, focus-preserving progress card for explicitly requested packages."""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, QTimer, Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton
from main.core.theme import Theme
from src.modules.package_jobs import TERMINAL


class PackageProgressCard(QFrame):
    details_requested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("package-progress-card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._jobs = {}
        self._dismissed = set()
        self._current = None
        self._expiry = QTimer(self)
        self._expiry.setSingleShot(True)
        self._expiry.setInterval(10000)
        self._expiry.timeout.connect(self._dismiss)
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(5)
        header = QHBoxLayout()
        self.title = QLabel(self)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setObjectName("package-title")
        header.addWidget(self.title, 1)
        hide = QPushButton("×", self)
        hide.setFixedSize(24, 24)
        hide.setToolTip("Hide progress; preparation continues")
        hide.setAccessibleName("Hide package progress")
        hide.clicked.connect(self._dismiss)
        header.addWidget(hide)
        box.addLayout(header)
        self.message = QLabel(self)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        box.addWidget(self.message)
        row = QHBoxLayout()
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        row.addWidget(self.progress, 1)
        self.phase = QLabel(self)
        self.phase.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.phase)
        details = QPushButton("Details", self)
        details.setToolTip("Open package activity and preparation results")
        details.clicked.connect(self.details_requested.emit)
        row.addWidget(details)
        box.addLayout(row)
        parent.installEventFilter(self)
        self.apply_theme(Theme.active_theme)
        self.hide()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.reposition()
        return super().eventFilter(watched, event)

    def reposition(self):
        parent = self.parentWidget()
        width = max(1, min(460, parent.width() - 24))
        self.setFixedWidth(width)
        height = max(100, self.sizeHint().height())
        self.resize(width, min(height, max(1, parent.height() - 72)))
        self.move(max(0, parent.width() - width - 12), max(0, parent.height() - self.height() - 38))

    def apply_theme(self, mode):
        from main.core.theme import get_palette
        p = get_palette(mode)
        self.setStyleSheet(f"""
            QFrame#package-progress-card {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 {p['BG_LIGHT']}, stop:0.35 {p['BG_DARK']}, stop:1 {p['BG_DARKEST']});
                border: 1px solid {p['BORDER_LIT']}; border-radius: 10px;
            }}
            QLabel {{ background: transparent; color: {p['TEXT']}; border: none; }}
            QLabel#package-title {{ font-weight: 600; }}
            QPushButton {{ background: {p['BG_MID']}; color: {p['TEXT']};
                border: 1px solid {p['BORDER']}; border-radius: 4px; padding: 3px 6px; }}
            QPushButton:hover, QPushButton:focus {{ border-color: {p['CYAN']}; }}
            QProgressBar {{ background: {p['BG_MID']}; border: none; border-radius: 3px; }}
            QProgressBar::chunk {{ background: {p['CYAN']}; border-radius: 3px; }}
        """)

    def update_job(self, payload):
        job = str(payload.get("job_id", ""))
        if not job:
            return
        previous = self._jobs.get(job)
        if previous and int(payload.get("seq", 0)) <= int(previous.get("seq", 0)):
            return
        self._jobs[job] = dict(payload)
        if len(self._jobs) > 48:
            removed = next(iter(self._jobs))
            self._jobs.pop(removed)
            self._dismissed.discard(removed)
        self._render_latest()

    def _render_latest(self):
        available = [(job, data) for job, data in self._jobs.items() if job not in self._dismissed]
        active = [(job, data) for job, data in available if data.get("stage") not in TERMINAL]
        if not available:
            self.hide()
            return
        job, data = max(active or available, key=lambda pair: pair[1].get("created", 0))
        self._current = job
        self._expiry.stop()
        full_title = str(data.get("title", "Board preparation"))
        self.reposition()
        self.title.setText(self.title.fontMetrics().elidedText(full_title, Qt.TextElideMode.ElideRight, self.width() - 66))
        self.title.setToolTip(full_title)
        message = str(data.get("message", ""))
        self.message.setText(message[:260] + ("…" if len(message) > 260 else ""))
        self.message.setToolTip(message)
        pct = data.get("progress")
        self.progress.setVisible(pct is not None)
        if pct is not None:
            self.progress.setValue(max(0, min(100, int(pct))))
        self.phase.setText(str(data.get("stage", "")).capitalize() + (f" · {pct}%" if pct is not None else ""))
        if len(active) > 1:
            self.phase.setText(self.phase.text() + f" · {len(active)} jobs")
        self.reposition()
        self.show()
        self.raise_()
        if not active:
            self._expiry.start()

    def _dismiss(self):
        if self._current:
            self._dismissed.add(self._current)
        self._render_latest()

    def current_job(self):
        return self._jobs.get(self._current, {})
