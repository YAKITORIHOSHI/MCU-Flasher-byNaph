#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import time
import re
from pathlib import Path


# ── Dynamic Project Root Resolution ──
_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent.parent if _this_file.parent.parent.name == "main" else _this_file.parent.parent
SCRIPT_DIR = _project_root

# ── Global Constants & Telemetry ──
_STARTUP_MONOTONIC = time.perf_counter()


def _startup_event(name: str) -> None:
    """Emit lightweight phase telemetry without adding startup I/O."""
    try:
        elapsed_ms = (time.perf_counter() - _STARTUP_MONOTONIC) * 1000.0
        print(f"[STARTUP] {name} +{elapsed_ms:.1f}ms", flush=True)
    except Exception:
        pass


# ── Serial and Hardware Constants ──
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

# ── Database Storage CRUD Modules ──
try:
    from src.dbs import dbs_create, dbs_read, dbs_update, dbs_delete
except Exception:
    dbs_create = None
    dbs_read = None
    dbs_update = None
    dbs_delete = None

# ── Windows Win32 API Modules ──
try:
    import win32gui
    import win32con
    import win32process
except ImportError:
    win32gui = None
    win32con = None
    win32process = None

UPLOAD_CONNECTION_ATTEMPTS = 10
MCU_FLASH_PATCH_VERSION = "v25-ui-responsive-serial-pump-app-namespace"
PROJECT_BUILD_CACHE_DIR = ".mcu_flasher_build_cache"
PROJECT_BUILD_CACHE_MARKER = ".mcu_flasher_cache_marker"
AI_PROJECT_STORAGE_DIR = ".mcu_ai_edits"
EDITOR_WINDOW_TITLE = "MCU Flasher — Embedded Code Editor (Closing this window will attach back to the MAIN window)"

DEFAULT_SKETCH_DIR = SCRIPT_DIR
DEFAULT_BAUD = 115200
DEFAULT_UPLOAD_SPEED = 460800
ESP8266_MONITOR_BAUD = 74880
ESP32_MONITOR_BAUD = 115200
AVR_MONITOR_BAUD = 9600
VALID_BAUD_RATES = {
    300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800,
    38400, 57600, 74880, 115200, 230400, 460800, 512000, 921600
}


def default_monitor_baud(
    platform: object = "",
    board_id: object = "",
    board_name: object = "",
) -> str:
    """Return the default Serial Monitor baud for a resolved board family.

    ``platform`` is the authoritative value for downloaded/dynamically
    discovered board definitions.  The board/name fields are intentionally
    accepted as additional identity hints for older or third-party records
    whose platform metadata may be an alias rather than the canonical
    PlatformIO platform ID.

    This is the monitor/application baud only; bootloader upload speeds remain
    controlled independently by ``upload_speed``.
    """
    platform_text = str(platform or "").strip().lower()
    identity = " ".join(
        str(value or "").strip().lower()
        for value in (platform, board_id, board_name)
    )

    if (
        platform_text in {"espressif8266", "esp8266"}
        or "espressif8266" in identity
        or "esp8266" in identity
    ):
        return str(ESP8266_MONITOR_BAUD)
    if (
        platform_text in {"espressif32", "esp32"}
        or "espressif32" in identity
        or "esp32" in identity
    ):
        return str(ESP32_MONITOR_BAUD)
    if platform_text in {"atmelavr", "avr"} or "atmelavr" in identity:
        return str(AVR_MONITOR_BAUD)
    return str(DEFAULT_BAUD)


def board_reset_capabilities(
    platform: object = "",
    board_id: object = "",
    board_name: object = "",
    framework: object = "arduino",
) -> dict[str, object]:
    """Describe reset strategies that are safe for a resolved board.

    Soft Reset is an incremental upload of the app's minimal Arduino sketch,
    so it is available to any resolved PlatformIO board using the Arduino
    framework.  Hard Reset is deliberately strategy-based: it is destructive
    and must never fall through to an unrelated MCU's bootloader command.
    Adding a future MCU therefore means adding one explicit strategy here and
    one matching handler, rather than inheriting ESP32/AVR behavior by guess.
    """
    platform_text = str(platform or "").strip().lower()
    identity = " ".join(
        str(value or "").strip().lower()
        for value in (platform, board_id, board_name)
    )
    # Older built-in records omitted ``framework``; the catalog has always
    # treated those records as Arduino-compatible.
    framework_text = str(framework or "arduino").strip().lower()

    if (
        platform_text in {"espressif8266", "esp8266"}
        or "espressif8266" in identity
        or "esp8266" in identity
    ):
        family = "espressif8266"
    elif (
        platform_text in {"espressif32", "esp32"}
        or "espressif32" in identity
        or "esp32" in identity
    ):
        family = "espressif32"
    elif platform_text in {"atmelavr", "avr"} or "atmelavr" in identity:
        family = "atmelavr"
    else:
        family = platform_text

    hard_strategy = {
        "atmelavr": "avr_bootloader",
        "espressif32": "esp32_recovery",
        "espressif8266": "esp8266_erase",
    }.get(family)
    return {
        "family": family,
        "soft_reset": bool(platform_text and board_id and "arduino" in framework_text),
        "hard_reset": hard_strategy is not None,
        "hard_reset_ui": hard_strategy in {"esp32_recovery", "esp8266_erase"},
        "hard_strategy": hard_strategy,
    }

# ─── Standard C / C++ headers ────────────────────────────────────
# Every header shipped with the C standard library, the C++ standard
# library (C++98 → C++23), the C++ <c...> compatibility wrappers, and
# the common POSIX/OS headers.  These are provided by the compiler /
# toolchain, never by an installable Arduino/PlatformIO library, so the
# include-scanners must always treat them as built-in.  (All lowercase
# to match the normalised comparisons used across this file.)
STANDARD_C_CPP_HEADERS = frozenset({
    # C standard library (C89 / C99 / C11 / C17 / C23)
    "assert.h", "complex.h", "ctype.h", "errno.h", "fenv.h", "float.h",
    "inttypes.h", "iso646.h", "limits.h", "locale.h", "math.h",
    "setjmp.h", "signal.h", "stdalign.h", "stdarg.h", "stdatomic.h",
    "stdbit.h", "stdbool.h", "stddef.h", "stdint.h", "stdio.h",
    "stdlib.h", "stdnoreturn.h", "string.h", "tgmath.h", "threads.h",
    "time.h", "uchar.h", "wchar.h", "wctype.h",

    # C++ <c...> compatibility wrappers
    "cassert", "ccomplex", "cctype", "cerrno", "cfenv", "cfloat",
    "cinttypes", "ciso646", "climits", "clocale", "cmath", "csetjmp",
    "csignal", "cstdalign", "cstdarg", "cstdbool", "cstddef", "cstdint",
    "cstdio", "cstdlib", "cstring", "ctime", "ctgmath", "cuchar",
    "cwchar", "cwctype",

    # C++ standard library (C++98 → C++23)
    "algorithm", "any", "array", "atomic", "barrier", "bit", "bitset",
    "charconv", "chrono", "codecvt", "compare", "complex", "concepts",
    "condition_variable", "coroutine", "deque", "exception", "execution",
    "expected", "filesystem", "flat_map", "flat_set", "format",
    "forward_list", "fstream", "functional", "future", "generator",
    "initializer_list", "iomanip", "ios", "iosfwd", "iostream", "istream",
    "iterator", "latch", "limits", "list", "locale", "map", "mdspan",
    "memory", "memory_resource", "mutex", "new", "numbers", "numeric",
    "optional", "ostream", "print", "queue", "random", "ranges", "ratio",
    "regex", "scoped_allocator", "semaphore", "set", "shared_mutex",
    "source_location", "span", "sstream", "stack", "stacktrace",
    "stdexcept", "stop_token", "streambuf", "string", "string_view",
    "syncstream", "system_error", "thread", "tuple", "type_traits",
    "typeindex", "typeinfo", "unordered_map", "unordered_set", "utility",
    "valarray", "variant", "vector", "version",

    # Common POSIX / OS headers used by embedded & desktop code
    "unistd.h", "fcntl.h", "dirent.h", "strings.h", "alloca.h",
    "libgen.h", "endian.h", "byteswap.h",
    "sys/types.h", "sys/stat.h", "sys/time.h", "sys/times.h", "sys/wait.h",
    "sys/ioctl.h", "sys/socket.h", "sys/select.h", "sys/uio.h",
    "sys/mman.h", "sys/param.h", "sys/resource.h", "sys/un.h", "sys/file.h",
    "sys/errno.h", "sys/statvfs.h", "sys/utsname.h", "sys/ipc.h",
    "sys/msg.h", "sys/shm.h", "sys/sem.h", "sys/poll.h", "sys/syscall.h",
    "sys/random.h", "sys/ttydefaults.h",
    "netinet/in.h", "netinet/tcp.h", "arpa/inet.h", "netdb.h", "poll.h",
    "termios.h", "pthread.h", "semaphore.h", "dlfcn.h", "mqueue.h",
})

KNOWN_WARNINGS = []


# ═══════════════════════════════════════════════════════════════
# ANSI ESCAPE HANDLING
# ═══════════════════════════════════════════════════════════════
# Matches CSI sequences (ESC [ ... letter), e.g. \033[2J, \033[H, \033[1;31m,
# plus the bare cursor-home form \033[H with no params. Covers clear-screen,
# cursor movement, and SGR (color) codes — anything a basic terminal emitter
# like Simulation.ino's draw() would send.
ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Sequences that conventionally mean "clear the screen / reset view".
# \033[2J  = erase entire screen
# \033[3J  = erase scrollback (some terminals)
# \033[H   = cursor to home (0,0)
# Sketches commonly send \033[2J\033[H back-to-back (as Simulation.ino does)
# to clear and then home the cursor in one shot. This matches a *run* of one
# or more such codes glued together as a single unit, so a glued pair only
# triggers one clear instead of two.
ANSI_CLEAR_RE = re.compile(r"(?:\x1b\[(?:2J|3J|H)|\[2J\[H\]|\[2J\[H)+", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    """Remove ANSI/CSI escape sequences from a string, leaving plain text."""
    clean = ANSI_CLEAR_RE.sub("", text)
    return ANSI_CSI_RE.sub("", clean)

_VALID_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


__all__ = [
    "AI_PROJECT_STORAGE_DIR",
    "ANSI_CLEAR_RE",
    "ANSI_CSI_RE",
    "AVR_MONITOR_BAUD",
    "board_reset_capabilities",
    "DEFAULT_BAUD",
    "DEFAULT_SKETCH_DIR",
    "DEFAULT_UPLOAD_SPEED",
    "EDITOR_WINDOW_TITLE",
    "ESP32_MONITOR_BAUD",
    "ESP8266_MONITOR_BAUD",
    "KNOWN_WARNINGS",
    "MCU_FLASH_PATCH_VERSION",
    "PROJECT_BUILD_CACHE_DIR",
    "PROJECT_BUILD_CACHE_MARKER",
    "SCRIPT_DIR",
    "STANDARD_C_CPP_HEADERS",
    "UPLOAD_CONNECTION_ATTEMPTS",
    "VALID_BAUD_RATES",
    "default_monitor_baud",
    "_STARTUP_MONOTONIC",
    "_VALID_NAME_RE",
    "_startup_event",
    "dbs_create",
    "dbs_read",
    "dbs_update",
    "dbs_delete",
    "serial",
    "strip_ansi",
    "win32con",
    "win32gui",
    "win32process",
]
