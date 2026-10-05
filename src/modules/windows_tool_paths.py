"""Keep Windows tool-specific path identities consistent across junctions."""
from __future__ import annotations

import ntpath
import os
import re
import sys
from pathlib import Path
from contextlib import contextmanager
from functools import wraps


def python_install_path(path):
    """Keep pip's target prefix short without changing the actual interpreter."""
    path = Path(path)
    if sys.platform != "win32":
        return path
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
            short = Path(buffer.value)
            if short.samefile(path):
                return short
    except (OSError, AttributeError):
        pass
    # Python and pip support extended paths even when NTFS 8.3 names are off.
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return Path(value)
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


def pip_environment(environment=None):
    """Give wheel builders a short physical temp directory on Windows.

    Setuptools expands core junctions. Pip's TAR extraction also combines native
    and forward slashes, which is invalid in extended-path namespaces. Use the
    app's ordinary short temp folder for pip, retaining compiler temp routing.
    """
    result = dict(os.environ if environment is None else environment)
    if sys.platform != "win32":
        return result
    directory = result.get("MCU_FLASHER_PIP_TEMP_DIR")
    if directory:
        for name in ("TEMP", "TMP", "TMPDIR"):
            result[name] = directory
    return result


def install_espidf_component_relpaths():
    """Keep IDF component objects inside their distinct SCons build folders."""
    if sys.platform != "win32" or getattr(os.path.relpath, "_mcu_espidf_component_relpath", False):
        return
    original = os.path.relpath

    @wraps(original)
    def relpath(path, start=None):
        # ESP-IDF resolves its components base, but passes the junction source
        # spelling to relpath. Those different roots otherwise generate enough
        # '..' segments to escape both app and bootloader object directories.
        if sys.platform == "win32" and start is not None:
            source, base = os.fspath(path), os.fspath(start)
            if isinstance(source, str) and isinstance(base, str):
                normalized = ntpath.normpath(base)
                package = ntpath.basename(ntpath.dirname(normalized))
                if (ntpath.isabs(source) and ntpath.isabs(base)
                        and ntpath.basename(normalized).lower() == "components"
                        and re.fullmatch(r"framework-espidf(?:@.+)?", package, re.IGNORECASE)):
                    canonical_base = os.path.realpath(base)
                    canonical_source = os.path.realpath(source)
                    try:
                        contained = ntpath.normcase(ntpath.commonpath((canonical_base, canonical_source))) == ntpath.normcase(canonical_base)
                    except ValueError:
                        contained = False  # Different drives retain native behavior.
                    if contained:
                        return original(canonical_source, canonical_base)
        return original(path, start)

    relpath._mcu_espidf_component_relpath = True
    os.path.relpath = relpath


@contextmanager
def espidf_component_relpaths():
    """Scope the IDF path adapter to a bootstrap builder invocation."""
    original = os.path.relpath
    install_espidf_component_relpaths()
    installed = os.path.relpath
    try:
        yield
    finally:
        if os.path.relpath is installed:
            os.path.relpath = original


def zephyr_cmake_environment(command, environment):
    """Give Zephyr CMake the same canonical base it uses for source files."""
    from src.modules.zephyr_compat import cmake_environment
    environment = cmake_environment(command, environment)
    if sys.platform != "win32" or not command or not environment:
        return environment
    if isinstance(command, (list, tuple)):
        executable = os.fsdecode(command[0])
    elif isinstance(command, str):
        # Windows' subprocess audit event receives the serialized command line.
        # Inspect its program only; never rewrite or replay the command.
        match = re.match(r'^\s*(?:"([^"]+)"|([^\s"]+))', command)
        if not match:
            return environment
        executable = match[1] or match[2]
    else:
        return environment
    executable = ntpath.basename(executable).lower()
    base = environment.get("ZEPHYR_BASE")
    if executable not in ("cmake", "cmake.exe") or not isinstance(base, str):
        return environment
    if not ntpath.basename(base.rstrip("\\/")).startswith("framework-zephyr"):
        return environment
    # CMake resolves junctions for some CMAKE_CURRENT_LIST_FILE values. Using
    # the alias as ZEPHYR_BASE makes its relative library names escape the
    # framework and include the installation folder's spaces. Keep package,
    # project and compiler arguments short; only this CMake base is canonical.
    canonical = os.path.realpath(base).replace("\\", "/")
    if canonical == base:
        return environment
    result = dict(environment)
    result["ZEPHYR_BASE"] = canonical
    return result
