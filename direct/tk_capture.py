"""Bounded native Tk captures for disposable visual-verification fixtures.

X11 captures read the Tk drawable directly: Qt can return a non-null, entirely
black image for an Xwayland window. No Qt application or window is created here.
Windows fixtures may supply their existing QApplication for the Qt fallback.
Call on the Tk GUI thread after the fixture's ordinary layout/paint pump.
"""
from __future__ import annotations

import ctypes
from pathlib import Path
import struct
import sys
import zlib


_MAX_DIMENSION = 8192
_MAX_PIXELS = 16_000_000
_MAX_NATIVE_BYTES = 64_000_000


def _size(width: int, height: int) -> None:
    if not (0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION
            and width * height <= _MAX_PIXELS):
        raise ValueError(f"Tk capture dimensions exceed the native image budget: {width}x{height}")


def _png(width: int, height: int, rgb: bytes | bytearray) -> bytes:
    """Encode RGB pixels without introducing a fixture image dependency."""
    _size(width, height)
    if len(rgb) != width * height * 3:
        raise ValueError("Tk capture returned an incomplete RGB image")
    first = rgb[:3]
    if all(rgb[offset:offset + 3] == first for offset in range(3, len(rgb), 3)):
        raise ValueError("Tk capture is a uniform image; native painting was not captured")
    stride = width * 3
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        rows.extend(rgb[y * stride:(y + 1) * stride])

    def chunk(kind: bytes, value: bytes) -> bytes:
        return (struct.pack("!I", len(value)) + kind + value
                + struct.pack("!I", zlib.crc32(kind + value) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 3)) + chunk(b"IEND", b""))


class _XImage(ctypes.Structure):
    # Only the documented XImage prefix is read. XDestroyImage owns its data
    # and releases the complete native structure, including its function table.
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
                ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
                ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
                ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
                ("blue_mask", ctypes.c_ulong)]


def _x11_png(widget, width: int, height: int) -> bytes:
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                             ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                             ctypes.c_ulong, ctypes.c_int]
    x11.XGetImage.restype = ctypes.POINTER(_XImage)
    x11.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
    x11.XDestroyImage.restype = ctypes.c_int
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    display = x11.XOpenDisplay(str(widget.winfo_screen()).encode("utf-8"))
    if not display:
        raise RuntimeError("Tk capture could not open the window's X11 display")
    image = None
    try:
        image = x11.XGetImage(display, int(widget.winfo_id()), 0, 0, width,
                              height, ctypes.c_ulong(-1).value, 2)  # ZPixmap
        if not image:
            raise RuntimeError("Tk capture could not read its X11 drawable")
        native = image.contents
        if native.width != width or native.height != height:
            raise ValueError("Tk capture returned unexpected X11 dimensions")
        if (native.format != 2 or native.xoffset != 0 or not native.data
                or native.byte_order not in (0, 1) or native.bits_per_pixel not in (16, 24, 32)):
            raise ValueError("Tk capture returned an unsupported X11 pixel format")
        pixel_bytes = native.bits_per_pixel // 8
        count = native.bytes_per_line * height
        if native.bytes_per_line < width * pixel_bytes or not 0 < count <= _MAX_NATIVE_BYTES:
            raise ValueError("Tk capture exceeds the native pixel-data budget")
        masks = (native.red_mask, native.green_mask, native.blue_mask)
        shifts = [(mask & -mask).bit_length() - 1 for mask in masks]
        if (any(mask <= 0 or mask >= 1 << native.bits_per_pixel for mask in masks)
                or masks[0] & masks[1] or masks[0] & masks[2] or masks[1] & masks[2]):
            raise ValueError("Tk capture returned invalid X11 color masks")
        maxima = [mask >> shift for mask, shift in zip(masks, shifts)]
        if any(value & (value + 1) for value in maxima):
            raise ValueError("Tk capture returned unsupported X11 color masks")
        raw = ctypes.string_at(native.data, count)
        rgb = bytearray()
        byte_order = "little" if native.byte_order == 0 else "big"
        for y in range(height):
            for column in range(width):
                offset = y * native.bytes_per_line + column * pixel_bytes
                value = int.from_bytes(raw[offset:offset + pixel_bytes], byte_order)
                rgb.extend((((value & mask) >> shift) * 255 + maximum // 2) // maximum
                           for mask, shift, maximum in zip(masks, shifts, maxima))
        return _png(width, height, rgb)
    finally:
        try:
            if image:
                x11.XDestroyImage(image)
        finally:
            x11.XCloseDisplay(display)


def _qt_png(widget, width: int, height: int, capture_app) -> bytes:
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    app = capture_app or QApplication.instance()
    if app is None:
        raise RuntimeError("Native Tk capture requires the fixture's existing QApplication on this platform")
    app.processEvents()
    screen = app.primaryScreen()
    if screen is None:
        raise RuntimeError("Native Tk capture has no Qt screen")
    image = screen.grabWindow(int(widget.winfo_id())).toImage()
    if image.isNull():
        raise ValueError("Tk capture returned an empty Qt image")
    width, height = image.width(), image.height()
    _size(width, height)
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    raw, stride = bytes(image.constBits()), image.bytesPerLine()
    rgb = b"".join(raw[y * stride:y * stride + width * 3] for y in range(height))
    return _png(width, height, rgb)


def save_tk_capture(widget, path: str | Path, *, capture_app=None) -> Path:
    """Write one validated native window PNG without changing its focus/state."""
    widget.update_idletasks()
    if not widget.winfo_viewable():
        raise ValueError("Tk capture requires a mapped window")
    width, height = widget.winfo_width(), widget.winfo_height()
    _size(width, height)
    png = (_x11_png(widget, width, height) if sys.platform.startswith("linux")
           else _qt_png(widget, width, height, capture_app))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    return target
