"""Select prepared Python compatibility providers only for the Mbed adapter."""
from __future__ import annotations

from contextlib import contextmanager
import importlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys

SETUPTOOLS_VERSION = "80.9.0"
FUTURE_VERSION = "1.0.0"
REQUIREMENTS = (f"setuptools=={SETUPTOOLS_VERSION}", f"future=={FUTURE_VERSION}")
# Presence and fingerprints are checked without importing providers at launch.
REQUIRED_FILES = (
    "setuptools/__init__.py", "setuptools/_distutils/spawn.py", "setuptools/_distutils/version.py",
    "future/__init__.py", "past/__init__.py", "past/builtins/misc.py", "distutils-precedence.pth",
    f"setuptools-{SETUPTOOLS_VERSION}.dist-info/METADATA", f"future-{FUTURE_VERSION}.dist-info/METADATA",
)


def _availability_json(path, framework, limit=1048576):
    """Read bounded metadata only from the selected framework package."""
    resolved = path.resolve()
    if os.path.normcase(os.path.commonpath((resolved, framework))) != os.path.normcase(str(framework)):
        raise ValueError("Metadata escapes the selected framework")
    with open(resolved, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Metadata exceeds reviewed bounds")
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Metadata must be an object")
    return value


def unavailable_framework_reason(platform_name, platform_version, board, framework_dir):
    """Identify only the reviewed unsupported STM32-H103 Mbed declaration."""
    if platform_name != "ststm32" or str(platform_version) != "20.0.0":
        return None
    manifest = board if isinstance(board, dict) else getattr(board, "manifest", None)
    ident = board.get("id") if isinstance(board, dict) else getattr(board, "id", None)
    if not isinstance(manifest, dict) or ident != "olimex_f103":
        return None
    build = manifest.get("build", {})
    frameworks = manifest.get("frameworks", [])
    if (not isinstance(build, dict) or build.get("mcu") != "stm32f103rbt6"
            or build.get("core") != "stm32" or build.get("cpu") != "cortex-m3"
            or manifest.get("name") != "Olimex STM32-H103" or manifest.get("vendor") != "Olimex"
            or not isinstance(frameworks, (list, tuple)) or "mbed" not in frameworks or "mbed_variant" in build):
        return None  # An explicit override or changed board identity stays native.
    core, packages = os.environ.get("PLATFORMIO_CORE_DIR"), os.environ.get("PLATFORMIO_PACKAGES_DIR")
    if not isinstance(core, str) or not isinstance(packages, str):
        return None
    try:
        core, packages, framework = Path(core).resolve(), Path(packages).resolve(), Path(framework_dir).resolve()
        if (packages != (core / "packages").resolve() or framework.parent != packages
                or os.path.normcase(os.path.commonpath((packages, core))) != os.path.normcase(str(core))
                or framework.name not in ("framework-mbed", "framework-mbed@6.61700.231105")):
            return None
        metadata = _availability_json(framework / "package.json", framework, 16384)
        if metadata.get("name") != "framework-mbed" or metadata.get("version") != "6.61700.231105":
            return None
        remap = _availability_json(framework / "platformio/variants_remap.json", framework, 65536)
        targets = _availability_json(framework / "targets/targets.json", framework)
        if (not all(isinstance(key, str) and isinstance(value, str) for key, value in remap.items())
                or not all(isinstance(key, str) and isinstance(value, dict) for key, value in targets.items())):
            return None
        if "olimex_f103" in remap or not targets or "OLIMEX_F103" in targets:
            return None  # Preserve newly supplied definitions/remaps and unknown data.
    except (OSError, TypeError, ValueError, UnicodeError):
        return None
    return ("Mbed OS 6.17 has no OLIMEX_F103 target for Olimex STM32-H103; "
            "ststm32 20.0.0 declares Mbed without a matching definition. "
            "Other declared frameworks remain available.")


def prepare_dependencies():
    """Validate installed providers and select them before Mbed prepends its bundle.

    Importing both top-level packages fixes their submodule search paths. Mbed's
    bundled future 0.18.1 then cannot shadow the prepared modern future/past.
    Never install, replace an already loaded package, or fabricate removed APIs.
    """
    try:
        distributions = {}
        for name, expected in (("setuptools", SETUPTOOLS_VERSION), ("future", FUTURE_VERSION)):
            distribution = importlib.metadata.distribution(name)
            if distribution.version != expected:
                raise ImportError(f"{name} {expected} is required; found {distribution.version}")
            distributions[name] = distribution
        setuptools = importlib.import_module("setuptools")
        future = importlib.import_module("future")
        past = importlib.import_module("past")
        for module, distribution in ((setuptools, distributions["setuptools"]),
                                     (future, distributions["future"]), (past, distributions["future"])):
            expected = Path(distribution.locate_file(f"{module.__name__}/__init__.py")).resolve()
            if not getattr(module, "__file__", None) or Path(module.__file__).resolve() != expected:
                raise ImportError(f"{module.__name__} was loaded from an unprepared provider")
        from distutils.spawn import find_executable
        from distutils.version import LooseVersion
        from past.builtins import cmp
        if not callable(find_executable) or not callable(LooseVersion) or cmp(1, 2) != -1:
            raise ImportError("Mbed compatibility APIs are unavailable")
    except (ImportError, OSError, ValueError) as exc:
        entry = "bash direct/ubuntu/run.sh --repair" if sys.platform.startswith("linux") else "direct/windows/run.vbs --repair"
        raise RuntimeError(f"Mbed Python compatibility dependencies are unavailable: {exc}. "
                           f"Prepare them in bootstrap: {entry}") from exc


def _packages_directory():
    packages = os.environ.get("PLATFORMIO_PACKAGES_DIR", "").strip()
    if packages:
        return Path(packages)
    core = os.environ.get("PLATFORMIO_CORE_DIR", "").strip()
    if core:
        return Path(core) / "packages"
    if sys.platform.startswith("linux"):
        from src.modules.platform_runtime import native_platformio_dir
        return native_platformio_dir() / "packages"
    return Path(__file__).resolve().parents[1] / ".platformio-mcu-gui" / "packages"


def _installed_adapter(origin):
    if not isinstance(origin, str):
        return False
    try:
        source = Path(origin).resolve()
        package = source.parent.parent
        return (source.name == "pio_mbed_adapter.py" and source.parent.name == "platformio"
                and re.fullmatch(r"framework-mbed(?:@.+)?", package.name) is not None
                and package.parent == _packages_directory().resolve())
    except (OSError, ValueError):
        return False


class _MbedFinder(importlib.abc.MetaPathFinder):
    _mcu_mbed_compat = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "pio_mbed_adapter":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and _installed_adapter(spec.origin):
            prepare_dependencies()
            return spec
        return None


def install_mbed_compat():
    """Install a lazy, idempotent selector for guarded PlatformIO children."""
    for finder in sys.meta_path:
        if getattr(finder, "_mcu_mbed_compat", False) is True:
            return finder
    finder = _MbedFinder()
    sys.meta_path.insert(0, finder)
    return finder


@contextmanager
def mbed_compat():
    """Scope the selector to bootstrap; preserve an existing runtime selector."""
    existing = any(getattr(finder, "_mcu_mbed_compat", False) is True for finder in sys.meta_path)
    finder = install_mbed_compat()
    try:
        yield
    finally:
        if not existing and finder in sys.meta_path:
            sys.meta_path.remove(finder)
