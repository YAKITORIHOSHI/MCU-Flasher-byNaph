"""Tell a working npm OpenCode on Windows from a damaged one by its files.

opencode-ai ships ``bin/opencode.exe`` as a 479-byte placeholder script that
its postinstall replaces with the real executable from the platform package
(``opencode-windows-x64``, about 200 MB). An install stopped before that, or
one half-removed while OpenCode was running, leaves npm's ``opencode`` shims
launching the placeholder ("opencode-ai's postinstall script was not run") or
nothing at all. Nothing here runs OpenCode to decide: a slow or locked
``--version`` is not damage.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional, Sequence

# @opencode/cli installs an ``opencode`` shim of its own; it is the wrong package here.
PACKAGES = ("@opencode/cli", "opencode-ai")
INSTALL_SPEC = "opencode-ai@latest"
SHIM_NAMES = ("opencode.cmd", "opencode.ps1", "opencode")
SHIM_PATTERN = "opencode*"
# cmd-shim writes "%dp0%\node_modules\...", its sh and ps1 shims "$basedir/node_modules/...".
_SHIM_TARGET = re.compile(r'(?:%dp0%|\$basedir)[\\/]+(node_modules[\\/][^"\r\n]+?)"')


class Damage(NamedTuple):
    prefix: Path  # the npm global directory holding the shims
    path: Path  # the shim or package found without its executable


def is_valid_pe_binary(path: Path | str) -> bool:
    """Return True for a real Windows executable: at least 1 KB, starting with ``MZ``."""
    try:
        candidate = Path(path)
        if not candidate.is_file() or candidate.stat().st_size < 1024:
            return False
        with candidate.open("rb") as stream:
            return stream.read(2) == b"MZ"
    except OSError:
        return False


def shim_target(shim: Path | str) -> Optional[Path]:
    """Return the package file an npm shim launches, or None for any other file."""
    shim = Path(shim)
    try:
        with shim.open("r", encoding="utf-8", errors="replace") as stream:
            text = stream.read(8192)
    except OSError:
        return None
    match = _SHIM_TARGET.search(text)
    if not match:
        return None
    return shim.parent.joinpath(*re.split(r"[\\/]+", match.group(1)))


def target_usable(target: Path, heal: Optional[Callable[[Path], bool]] = None) -> bool:
    """True when ``target`` is a real executable, after ``heal`` reruns a skipped postinstall."""
    if target.suffix.lower() != ".exe":
        return target.is_file()
    if is_valid_pe_binary(target):
        return True
    return bool(heal is not None and heal(target.parent) and is_valid_pe_binary(target))


def shim_usable(shim: Path | str, heal: Optional[Callable[[Path], bool]] = None) -> bool:
    """False only for an npm shim whose package executable is missing or a placeholder."""
    target = shim_target(shim)
    return target is None or target_usable(target, heal)


def find_damaged(directories: Iterable[Path],
                 heal: Optional[Callable[[Path], bool]] = None) -> Optional[Damage]:
    """Return the first npm OpenCode shim or package left without its real executable."""
    for directory in directories:
        directory = Path(directory)
        for name in SHIM_NAMES:
            shim = directory / name
            if shim.is_file() and not shim_usable(shim, heal):
                return Damage(directory, shim)
        for package in PACKAGES:
            package_dir = directory.joinpath("node_modules", *package.split("/"))
            if package_dir.is_dir() and not target_usable(package_dir / "bin" / "opencode.exe", heal):
                return Damage(directory, package_dir)
    return None


def remove_shims(directories: Iterable[Path]) -> list[Path]:
    """Delete leftover ``opencode*`` files in npm global directories; folders stay."""
    removed: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        key = os.path.normcase(os.path.normpath(str(directory)))
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = sorted(Path(directory).glob(SHIM_PATTERN))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() or entry.is_symlink():
                    entry.unlink()
                    removed.append(entry)
            except OSError:
                pass
    return removed


def npm_environment() -> dict[str, str]:
    """The current environment without an ``ignore-scripts`` override: the postinstall must run."""
    return {key: value for key, value in os.environ.items()
            if key.lower() != "npm_config_ignore_scripts"}


def _end_process_tree(process: subprocess.Popen) -> None:
    """End npm and every child it started; killing npm.cmd alone leaves node writing."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass


def run_npm(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    """Run one npm command; the result's ``timed_out`` says whether it was stopped."""
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=npm_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _end_process_tree(process)
        try:
            output, _ = process.communicate(timeout=10)
        except Exception:
            output = ""
        output = (output or "") + f"\n(stopped after {int(timeout)} s)"
    result = subprocess.CompletedProcess(list(command), 1 if timed_out else process.returncode, output or "")
    result.timed_out = timed_out
    return result
