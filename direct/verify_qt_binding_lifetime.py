"""Bounded, application-free Qt binding probes for mutable CPython None.

Run each operation in a fresh process, so a native binding reference error can
be identified without involving application services, persistence or hardware.
Python 3.12+ keeps None immortal; those runs still exercise native lifecycles,
but cannot establish that a binding preserves its reference count on 3.10/3.11.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
from pathlib import Path


CASES = (
    "scalar-setters", "document-setters", "pixmap-painting", "widget-painting",
    "virtual-with-super", "virtual-without-super", "text-edit-style",
    "queued-slot", "deferred-delete",
)


def run_child(case: str, iterations: int) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
    import PySide6
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, QTimer, Qt
    from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QTextDocument, QTextOption
    from PySide6.QtWidgets import QApplication, QTextEdit, QWidget

    app = QApplication([])
    # Collection stays on this GUI thread throughout the native event probes.
    gc.disable()
    widgets = []
    objects = []
    count = [0]
    rect = QRect(0, 0, 140, 80)
    font_a, font_b = QFont("sans-serif", 10), QFont("sans-serif", 11)
    font_b.setItalic(True)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)

    def paint(target, index):
        painter = QPainter(target)
        gradient = QLinearGradient(0, 0, 80, 140)
        color = QColor("#63b7f4")
        color.setAlpha(42 if index % 2 else 22)
        gradient.setColorAt(0, color)
        color.setAlpha(0)
        gradient.setColorAt(1, color)
        painter.fillRect(rect, gradient)
        painter.end()

    class Painted(QWidget):
        def paintEvent(self, event):
            count[0] += 1
            paint(self, count[0])

    class WithSuper(QWidget):
        def changeEvent(self, event):
            super().changeEvent(event)
            count[0] += 1

    class WithoutSuper(QWidget):
        def changeEvent(self, event):
            count[0] += 1

    if case == "scalar-setters":
        color = QColor("#63b7f4")
        gradient = QLinearGradient(0, 0, 80, 140)
        def operation(index):
            color.setAlpha(index % 255)
            gradient.setColorAt(.5, color)
            font_a.setItalic(bool(index % 2))
    elif case == "document-setters":
        document = QTextDocument()
        objects.append(document)
        def operation(index):
            document.setDefaultFont(font_a if index % 2 else font_b)
            document.setDefaultTextOption(option)
            document.setPlainText("literal <tag> " + "A" * 100)
            document.setTextWidth(140 + index % 2)
            document.size()
    elif case == "pixmap-painting":
        pixmap = QPixmap(140, 80)
        def operation(index):
            paint(pixmap, index)
    elif case == "widget-painting":
        widget = Painted()
        widgets.append(widget)
        widget.resize(140, 80)
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        widget.show()
        app.processEvents()
        def operation(index):
            widget.grab()
    elif case in ("virtual-with-super", "virtual-without-super"):
        widget = (WithSuper if case == "virtual-with-super" else WithoutSuper)()
        widgets.append(widget)
        def operation(index):
            widget.setFont(font_a if index % 2 else font_b)
            app.sendEvent(widget, QEvent(QEvent.Type.StyleChange))
    elif case == "text-edit-style":
        widget = QTextEdit()
        widgets.append(widget)
        widget.setReadOnly(True)
        widget.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        widget.resize(140, 80)
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        widget.show()
        app.processEvents()
        def operation(index):
            widget.setPlainText("literal <tag> " + "A" * 100)
            widget.setStyleSheet("font-size: 13px;" if index % 2 else "font-size: 14px;")
            widget.grab()
    elif case == "queued-slot":
        timer = QTimer()
        objects.append(timer)
        timer.setSingleShot(True)
        def receive():
            count[0] += 1
        timer.timeout.connect(receive)
        def operation(index):
            timer.start(0)
            app.processEvents()
    else:
        owner = QObject()
        objects.append(owner)
        def operation(index):
            child = QObject(owner)
            child.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    for index in range(20):
        operation(index)
    app.processEvents()
    gc.collect()
    baseline = sys.getrefcount(None)
    previous = baseline
    negative_batches = 0
    failed = False
    samples = []
    metadata = {"case": case, "python": sys.version.split()[0], "platform": sys.platform,
                "pyside": PySide6.__version__, "shiboken": shiboken6.__version__,
                "qpa": app.platformName(), "mutable_none": sys.version_info < (3, 12),
                "baseline": baseline}
    print(json.dumps(metadata), flush=True)
    for index in range(iterations):
        operation(index)
        if (index + 1) % 25 == 0 or index + 1 == iterations:
            current = sys.getrefcount(None)
            samples.append((index + 1, current))
            negative_batches = negative_batches + 1 if current < previous else 0
            if index < 50 or (index + 1) % 100 == 0:
                print(json.dumps({"case": case, "completed": index + 1, "none_refs": current}), flush=True)
            # Stop before a reference error can exhaust the process singleton.
            if metadata["mutable_none"] and negative_batches >= 2 and baseline - current >= 64:
                failed = True
                break
            previous = current
    for widget in widgets:
        widget.close()
        widget.deleteLater()
    for obj in objects:
        obj.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()
    print(json.dumps({**metadata, "completed": index + 1, "samples": samples,
                      "callbacks": count[0], "final": sys.getrefcount(None),
                      "sustained_none_ref_loss": failed}), flush=True)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=CASES)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if not 100 <= args.iterations <= 3000:
        parser.error("--iterations must be between 100 and 3000")
    if args.child:
        return run_child(args.child, args.iterations)
    failed = []
    for case in args.case or CASES:
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                                 "--child", case, "--iterations", str(args.iterations)],
                                timeout=45, check=False)
        if result.returncode:
            failed.append({"case": case, "exit_code": result.returncode})
    print(json.dumps({"qt_binding_probe_failures": failed}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
