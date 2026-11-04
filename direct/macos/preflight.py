#!/usr/bin/env python3
"""Read-only macOS runtime checks; never install packages or open a GUI."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.modules.ubuntu_release import require_supported_ubuntu
from src.modules.ubuntu_architecture import elf_matches_host, host_architecture

ENV_NAME = ".venv-macos"


def _check_qt_runtime_files(qt_root: Path, architecture: str) -> None:
    """Refuse a Qt copied from another CPU before any native library can load."""
    paths = (qt_root / "lib/QtCore.framework/Versions/A/QtCore",
             qt_root / "lib/QtWebEngineCore.framework/Versions/A/QtWebEngineCore",
             qt_root / "plugins/platforms/libqcocoa.dylib")
    for native in paths:
        if not native.is_file():
            raise RuntimeError(f"A Qt runtime file is missing: {native}. Run bash direct/macos/run.sh --repair.")
        if not elf_matches_host(native, architecture):
            raise RuntimeError("The macOS Qt runtime belongs to another CPU. Run bash direct/macos/run.sh --repair on this Mac.")


def runtime_preflight(root=ROOT) -> None:
    """Validate private imports and native Qt without creating windows."""
    require_supported_ubuntu()
    root = Path(root).resolve()
    expected = root / ENV_NAME
    if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != expected:
        raise RuntimeError(f"MCU Flasher must use its own {ENV_NAME} environment. Run bash direct/macos/run.sh --repair.")
    architecture = host_architecture()
    from direct.ubuntu.runtime_architecture import check_runtime_architecture
    check_runtime_architecture(expected, architecture)
    qt_spec = importlib.util.find_spec("PySide6")
    if qt_spec is not None and qt_spec.origin:
        _check_qt_runtime_files(Path(qt_spec.origin).parent / "Qt", architecture)
    sys.path.insert(0, str(root))
    try:
        import PySide6
        import PySide6.QtWebEngineWidgets
        import serial
        import esptool
        import psutil
        import platformio
        import ptyprocess
        from src.modules.mbed_compat import prepare_dependencies
        prepare_dependencies()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"macOS runtime dependency validation failed: {exc}\n"
                           "Run bash direct/macos/run.sh --repair.") from exc
    # Keep Qt5 and Qt6 native graphs in separate processes, as in production.
    result = subprocess.run([sys.executable, "-B", "-c", "import PyQt5.QtWidgets, PyQt5.Qsci"],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("The library sample viewer's PyQt5/QScintilla runtime is unavailable:\n" +
                           result.stderr.strip() + "\nRun bash direct/macos/run.sh --repair.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        runtime_preflight()
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
