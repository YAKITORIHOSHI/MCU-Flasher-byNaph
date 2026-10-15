"""Keep standalone Tk font fallback compatible with native Linux Xft."""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import sys


@lru_cache(maxsize=1)
def prepare_tk_font_runtime() -> bool:
    """Use monochrome fallback on Xft releases without color glyph support.

    Call before creating the standalone Tk root. Older Xft can send invalid
    XRender glyph requests for color fonts; old Tk then invokes a retired font
    error callback through its expired stack pointer. Keep Unicode text and
    configured font choices, and exclude only unsupported color fallback.
    """
    if not sys.platform.startswith('linux'):
        return False

    import ctypes

    try:
        xft = ctypes.CDLL('libXft.so.2')
        xft.XftGetVersion.argtypes = []
        xft.XftGetVersion.restype = ctypes.c_int
        version = xft.XftGetVersion()
    except (OSError, AttributeError):
        return False  # Tk may use a different native font backend.
    if version >= 20305:
        return False

    # Supplement the process's existing Fontconfig configuration, including
    # user font preferences. Nothing is written to its files or environment,
    # and separately launched Qt viewers retain their own font configuration.
    fontconfig = ctypes.CDLL('libfontconfig.so.1')
    fontconfig.FcConfigGetCurrent.argtypes = []
    fontconfig.FcConfigGetCurrent.restype = ctypes.c_void_p
    fontconfig.FcConfigParseAndLoad.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    fontconfig.FcConfigParseAndLoad.restype = ctypes.c_int
    fontconfig.FcConfigBuildFonts.argtypes = [ctypes.c_void_p]
    fontconfig.FcConfigBuildFonts.restype = ctypes.c_int
    config = fontconfig.FcConfigGetCurrent()
    supplement = Path(__file__).resolve().parents[1] / 'assets' / 'tk_fontconfig_legacy.conf'
    if (not config or not fontconfig.FcConfigParseAndLoad(config, os.fsencode(supplement), 0)
            or not fontconfig.FcConfigBuildFonts(config)):
        raise RuntimeError('Unable to initialize compatible Tk font fallback')
    return True
