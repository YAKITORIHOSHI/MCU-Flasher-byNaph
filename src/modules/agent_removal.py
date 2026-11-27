"""Uninstall one coding agent's program for this account; its settings and sign-ins stay.

Only layouts MCU Flasher recognizes are removed: its own downloads (the isolated Hermes
folder, its agents folder) and the files each maker's installer writes for this account.
An install made another way, such as with npm or Homebrew, is described with the command
that removes it and left alone. Nothing is removed while any program runs from it, and
every path is checked again just before it goes. Plan and remove on a worker thread.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import sys

from main.core import ai_agents


class RemovalError(RuntimeError):
    """Why an agent cannot be uninstalled, in words for the person who asked."""


@dataclass(frozen=True)
class RemovalPlan:
    agent_id: str
    name: str
    paths: tuple = ()           # Removed in this order: launchers, then program folders
    kept: tuple = ()            # Its settings and sign-ins, which stay
    tidy: tuple = ()            # Folders removed afterwards only if nothing is left in them
    manual: str = ""            # Instead of paths: how to remove an install made another way
    everything: bool = False    # Its settings and sign-ins are inside ``paths`` and go too
    size: int = 0               # Bytes in ``paths``, as shown before anything is removed


def _windows() -> bool:
    return sys.platform == "win32"


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _inside(path, folder) -> bool:
    """Strictly inside ``folder`` (never the folder itself), compared without resolving links."""
    path, folder = _key(path), _key(folder).rstrip("\\/")
    return path.startswith(folder + os.sep)


def _same(first, second) -> bool:
    return _key(first) == _key(second)


def _local_appdata() -> Path | None:
    value = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(value) if value else None


def _roots() -> list[Path]:
    """Folders an uninstall may remove things inside of; never one of these folders itself."""
    roots = [Path.home(), ai_agents.agents_root()]
    if _windows():
        roots += [Path(value) for value in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", ""))
                  if value.strip()]
    return roots


def _codex_home() -> Path:
    value = os.environ.get("CODEX_HOME", "").strip()
    return Path(value) if value else Path.home() / ".codex"


def _settings(agent_id: str) -> tuple:
    """Where an agent keeps its settings and sign-ins, where that is known; they stay."""
    home = Path.home()
    places = {"claude": (home / ".claude", home / ".claude.json"), "codex": (_codex_home(),)}.get(agent_id, ())
    return tuple(path for path in places if os.path.lexists(path))


def _old_copies(launcher: Path) -> list[Path]:
    """What an agent's self-update leaves beside its program, such as ``agy.1795635612002596960.old``."""
    pattern = re.compile(re.escape(launcher.name) + r"\.\d+\.old")
    try:
        return sorted(path for path in launcher.parent.iterdir() if pattern.fullmatch(path.name))
    except OSError:
        return []


def _maker_layout(spec, launcher: Path):
    """(paths, kept, tidy, everything) the maker's installer wrote for this account, or None."""
    home, windows = Path.home(), _windows()
    target = Path(os.path.realpath(launcher))
    local = _local_appdata()
    if spec.id == "claude":
        # The native installer: versions in ~/.local/share/claude, the command in ~/.local/bin.
        share = home / ".local/share/claude"
        if _same(launcher, home / ".local/bin" / ("claude.exe" if windows else "claude")):
            if launcher.is_symlink() and not _inside(target, share):
                return None
            paths = [launcher, *([share] if share.is_dir() and not share.is_symlink() else [])]
            return paths, _settings("claude"), (), False
        # The older installer kept its own npm folder there; ~/.claude itself is its settings.
        legacy = home / ".claude/local"
        if _inside(launcher, legacy) and legacy.is_dir() and not legacy.is_symlink():
            return [legacy], _settings("claude"), (), False
        return None
    if spec.id == "codex":
        # The standalone installer keeps its releases in <Codex home>/packages/standalone and
        # links the command to the current one.
        packages = next((parent for parent in target.parents
                         if parent.name == "standalone" and parent.parent.name == "packages"), None)
        if packages is not None and not packages.is_symlink():
            paths = [*([launcher] if not _inside(launcher, packages) else []), packages]
            return paths, _settings("codex"), (packages.parent,), False
        if windows and local is not None:
            folder = local / "Programs/OpenAI/Codex"
            if _inside(launcher, folder) and _inside(target, folder):
                return [folder], _settings("codex"), (folder.parent,), False
        return None
    if spec.id == "antigravity":
        if windows:
            folder = local / "agy/bin" if local is not None else None
            if folder is not None and _inside(launcher, folder) and _inside(target, folder):
                return [folder], (), (folder.parent,), False
            return None
        if _same(launcher.parent, home / ".local/bin") and not launcher.is_symlink():
            return [launcher, *_old_copies(launcher)], (), (), False
        return None
    if spec.id == "hermes" and windows and local is not None:
        # Hermes keeps its program, tools, settings and sign-ins together in one home.
        folder = local / "hermes"
        if _inside(launcher, folder) and _inside(target, folder):
            return [folder], (), (), True
    return None


def _manual(spec, launcher: Path) -> str:
    """How to remove an install MCU Flasher did not make and does not recognize."""
    target = os.path.realpath(launcher).replace("\\", "/")
    found = re.search(r"/node_modules/((?:@[^/]+/)?[^/]+)/", target)
    if found:
        return (f"{spec.name} at {launcher} was installed with npm, so MCU Flasher leaves it alone. "
                f"To uninstall it, run: npm uninstall -g {found.group(1)}")
    found = re.search(r"/(Cellar|Caskroom)/([^/]+)/", target)
    if found:
        cask = " --cask" if found.group(1) == "Caskroom" else ""
        return (f"{spec.name} at {launcher} was installed with Homebrew, so MCU Flasher leaves it alone. "
                f"To uninstall it, run: brew uninstall{cask} {found.group(2)}")
    return (f"MCU Flasher does not know how {spec.name} was installed at {launcher}, so it leaves it alone. "
            f"Remove it the way it was installed.")


def _check_paths(name: str, paths) -> None:
    """Every path is this account's, inside its own folders, and none is one of those folders."""
    roots = _roots()
    uid = getattr(os, "getuid", lambda: None)()
    for path in paths:
        if not any(_inside(path, root) for root in roots) or any(_same(path, root) for root in roots):
            raise RemovalError(f"{path} is outside the folders MCU Flasher may change, so {name} was left alone.")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RemovalError(f"{path} could not be checked: {exc.strerror or exc}") from exc
        if uid is not None and info.st_uid != uid:
            raise RemovalError(f"{path} belongs to another account, so {name} was left alone.")
        if stat.S_ISDIR(info.st_mode) and os.path.ismount(path):
            raise RemovalError(f"{path} is a separate drive, so {name} was left alone.")


def _size(paths) -> int:
    total = 0
    for path in paths:
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            total += info.st_size
            continue
        for folder, _folders, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    total += os.lstat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
    return total


def plan(agent_id: str, executable=None) -> RemovalPlan:
    """What uninstalling this agent removes and keeps; raises RemovalError with words otherwise."""
    spec = ai_agents.agent(agent_id)
    if spec.id != agent_id:
        raise RemovalError("MCU Flasher does not know this assistant.")
    if spec.builtin:
        raise RemovalError(f"{spec.name} comes with MCU Flasher, which needs it, so it is not uninstalled here.")
    found = executable or ai_agents.find_agent(spec.id)
    if not found:
        raise RemovalError(f"{spec.name} is not installed for this account.")
    launcher = Path(os.path.abspath(found))
    place = ai_agents.isolation(spec.id)
    if place is not None:
        if not _inside(launcher, place.root):
            raise RemovalError(f"{spec.name} at {launcher} is not MCU Flasher's own copy, so it was left alone.")
        layout = ([place.root], (), (), True)
    elif _inside(launcher, ai_agents.agent_prefix(spec.id)):
        # MCU Flasher downloaded it into its own folder: that folder is the whole program.
        layout = ([ai_agents.agent_prefix(spec.id)], _settings(spec.id), (), False)
    else:
        layout = _maker_layout(spec, launcher)
    if layout is None:
        return RemovalPlan(spec.id, spec.name, manual=_manual(spec, launcher))
    paths, kept, tidy, everything = layout
    paths = tuple(dict.fromkeys(Path(path) for path in paths))
    _check_paths(spec.name, paths)
    return RemovalPlan(spec.id, spec.name, paths, tuple(kept), tuple(tidy), everything=everything, size=_size(paths))


def running(removal: RemovalPlan) -> list[str]:
    """Programs that run from what would be removed, such as "claude (process 4242)"."""
    try:
        import psutil
    except ImportError:
        return []
    prefixes = {_key(form) for path in removal.paths for form in (path, os.path.realpath(path))}

    def inside(text) -> bool:
        if not text or not os.path.isabs(text):
            return False
        text = _key(text.removesuffix(" (deleted)"))
        return any(text == prefix or text.startswith(prefix.rstrip("\\/") + os.sep) for prefix in prefixes)

    found = []
    for process in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        info = process.info
        if info.get("pid") == os.getpid():
            continue
        if inside(info.get("exe")) or any(inside(arg) for arg in (info.get("cmdline") or [])[:8]):
            found.append(f"{info.get('name') or 'a program'} (process {info.get('pid')})")
    return found


def _windows_retry(function, path, _error) -> None:
    """Windows keeps read-only files from being deleted: clear the flag and try once more."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _open_folders(root) -> None:
    """Caches keep read-only folders, whose files cannot be deleted: open them, inside ``root`` only."""
    def opened(path):
        try:
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_IRWXU != stat.S_IRWXU:
                os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
        except OSError:
            pass
    opened(root)
    for folder, folders, _files in os.walk(root, followlinks=False):
        for name in folders:
            opened(os.path.join(folder, name))      # Before os.walk goes into it


def _remove_one(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if getattr(os.path, "isjunction", lambda _path: False)(path):
        os.rmdir(path)                  # A junction goes; what it points to stays.
    elif stat.S_ISDIR(info.st_mode):
        if _windows():
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_windows_retry)
            else:
                shutil.rmtree(path, onerror=_windows_retry)
        else:
            _open_folders(path)
            shutil.rmtree(path)         # Links inside are removed, never followed.
    else:
        try:
            os.unlink(path)
        except PermissionError:
            if not _windows():
                raise
            _windows_retry(os.unlink, path, None)


def remove(removal: RemovalPlan) -> None:
    """Remove a confirmed plan's paths, after checking them and what runs from them again."""
    if not removal.paths:
        raise RemovalError(removal.manual or f"{removal.name} has nothing MCU Flasher can remove.")
    _check_paths(removal.name, removal.paths)
    busy = running(removal)
    if busy:
        raise RemovalError(f"{removal.name} is running ({', '.join(busy[:3])}). "
                           f"Close it, then uninstall it again.")
    failures = []
    for path in removal.paths:
        try:
            _remove_one(path)
        except OSError as exc:
            failures.append(f"{path}: {exc.strerror or exc}")
    for folder in removal.tidy:
        try:
            if not any(Path(folder).iterdir()) and not any(_same(folder, root) for root in _roots()):
                os.rmdir(folder)
        except OSError:
            pass
    try:
        from main.core.file_utils import forget_installed_agents
        forget_installed_agents()       # Its sketch skills are retired when an assistant next starts.
    except Exception:
        pass
    if failures:
        raise RemovalError(f"{removal.name} was not completely uninstalled. These could not be removed: "
                           + "; ".join(failures[:3]))


def describe(removal: RemovalPlan) -> str:
    """The confirmation's details: what goes, how much, and what stays."""
    home = str(Path.home())

    def shown(path) -> str:
        text = str(path)
        return "~" + text[len(home):] if text == home or _inside(text, home) else text

    size = removal.size
    amount = (f"about {size / 1e9:.1f} GB" if size >= 1e9 else f"about {max(1, round(size / 1e6))} MB"
              if size >= 1e6 else "less than 1 MB")
    lines = [f"MCU Flasher removes {amount}:", *(f"  {shown(path)}" for path in removal.paths), ""]
    if removal.everything:
        lines.append(f"{removal.name}'s settings, keys and sign-ins are inside it and are removed too.")
    elif removal.kept:
        lines.append(f"Your {removal.name} settings and sign-ins stay in "
                     + " and ".join(shown(path) for path in removal.kept)
                     + ", so a later download picks up where you left off.")
    else:
        lines.append(f"Your {removal.name} settings and sign-ins stay where {removal.name} keeps them.")
    return "\n".join(lines)
