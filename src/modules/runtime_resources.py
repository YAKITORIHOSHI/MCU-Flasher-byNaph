"""Shared hardware policy; startup checks need only the standard library."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

MINIMUM_CPU_CORES = 4
MINIMUM_LOGICAL_CORES = MINIMUM_CPU_CORES


def logical_cpu_count() -> int | None:
    try:
        count = os.cpu_count()
        return int(count) if count and count > 0 else None
    except (OSError, TypeError, ValueError):
        return None


def physical_cpu_count() -> int | None:
    try:
        import psutil
        count = psutil.cpu_count(logical=False)
        return int(count) if count and count > 0 else None
    except (ImportError, OSError, TypeError, ValueError, AttributeError):
        return None


def compatibility_problem(cores: int | None, physical_cores: int | None = None) -> str:
    if cores is not None and cores >= MINIMUM_LOGICAL_CORES and (physical_cores is None or physical_cores >= MINIMUM_CPU_CORES):
        return ""
    detected = str(cores) if cores is not None else "Unavailable (CPU count could not be determined)"
    return (
        "MCU Flasher by Naph cannot start on this computer.\n\n"
        f"Detected physical CPU cores: {physical_cores if physical_cores is not None else 'Unavailable; using logical thread count'}\n"
        f"Detected logical CPU cores/threads: {detected}\n"
        "Minimum required: 4 CPU cores (logical threads when physical detection is unavailable)\n\n"
        "Use a computer with at least four CPU cores. "
        "If CPU detection failed, check the operating system's hardware information and restart."
    )


def show_incompatibility_notice(message: str) -> None:
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "MCU Flasher — Incompatible Hardware", 0x10)
        except (AttributeError, OSError):
            pass
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        # Ubuntu setup includes python3-tk; no editor or WebEngine is loaded.
        try:
            import tkinter as tk
            from tkinter import messagebox
        except ImportError:
            return
        try:
            root = tk.Tk()
            root.withdraw()
            try:
                messagebox.showerror("MCU Flasher — Incompatible Hardware", message, parent=root)
            finally:
                root.destroy()
        except (RuntimeError, tk.TclError):
            # Headless/desktop dependency failures still get the stderr notice.
            pass


def enforce_minimum_cpu_requirement() -> bool:
    message = compatibility_problem(logical_cpu_count(), physical_cpu_count())
    if message:
        show_incompatibility_notice(message)
        return False
    return True


@dataclass(frozen=True)
class PerformanceProfile:
    constrained: bool
    low_memory: bool
    syntax_interval_ms: int
    terminal_interval_ms: int
    terminal_scrollback: int
    terminal_history_chars: int


def performance_profile(cores: int | None = None, total_memory_gb: float | None = None,
                        physical_cores: int | None = None) -> PerformanceProfile:
    if cores is None:
        cores = logical_cpu_count()
        physical_cores = physical_cpu_count() if physical_cores is None else physical_cores
    if total_memory_gb is None:
        try:
            import psutil
            total_memory_gb = psutil.virtual_memory().total / (1024 ** 3)
        except (ImportError, OSError, AttributeError):
            pass
    low_memory = total_memory_gb is not None and total_memory_gb < 5.5
    constrained = cores is None or cores <= 6 or (physical_cores is not None and physical_cores <= 6) or low_memory
    return PerformanceProfile(constrained, low_memory, 8000 if constrained else 4000,
                              40 if constrained else 25, 2000 if constrained else 5000,
                              128_000 if constrained else 256_000)


def configure_webengine_environment() -> None:
    profile = performance_profile()
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    # Hidden views may throttle on small PCs. Keep GPU acceleration and its
    # watchdog; disabling either can increase CPU load or hide driver failures.
    no_throttle = ["--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                   "--disable-renderer-backgrounding", "--disable-features=CalculateNativeWinOcclusion"]
    if profile.constrained:
        flags = [flag for flag in flags if flag not in no_throttle]
        additions = ["--enable-low-end-device-mode", "--num-raster-threads=1"]
    else:
        additions = no_throttle
    for flag in additions:
        if flag not in flags:
            flags.append(flag)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
