"""Repository-backed application updates for MCU Flasher Bootstrap.

Bootstrap checks the canonical GitHub repository before the GUI starts.  When
the configured branch moves, it stages a fresh, shallow Git clone and only
then refreshes the application-owned source files in place. If Git is not
available, it uses GitHub's immutable archive for the same verified commit, so
a normal Windows or native Ubuntu installation does not depend on a separately
installed Git client.

The updater intentionally does not turn an installation into a working copy.
Its checkout, update record and rollback material live under per-user state.
Settings, sketches, project caches, private runtimes, installers and catalog
data remain in the existing installation and are never copied from GitHub.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import uuid
import zipfile


REPOSITORY = "YAKITORIHOSHI/MCU-Flasher-byNaph"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}.git"
DEFAULT_BRANCH = "main"
COMMIT_API_URL = f"https://api.github.com/repos/{REPOSITORY}/commits/{{branch}}"
# Page 2 omits the changed-file list, which keeps this response to a few kilobytes.
COMPARE_API_URL = f"https://api.github.com/repos/{REPOSITORY}/compare/{{base}}...{{head}}?page=2&per_page=1"
# A release ZIP records the commit it was built from, beside its other generated data.
BUNDLED_REVISION_FILE = "src/dbs/source_revision.json"
SOURCE_ARCHIVE_URL = f"https://codeload.github.com/{REPOSITORY}/zip/{{commit}}"

STATE_SCHEMA = 2
MAX_METADATA_BYTES = 512 * 1024
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_FILES = 20_000
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 30_000
MIN_FREE_SPACE_BYTES = 256 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
CLONE_TIMEOUT_SECONDS = 15 * 60
ARCHIVE_TIMEOUT_SECONDS = 15 * 60
# ``urlopen`` applies its timeout to individual socket operations. Keep those
# operations short as well, so a peer that sends a few bytes at a time cannot
# extend Bootstrap's declared elapsed-time budget forever.
NETWORK_IO_SLICE_SECONDS = 1.0

REQUIRED_SOURCE_FILES = frozenset((
    "mcu_flash_gui.py",
    "src/modules/bootstrap.py",
    "src/modules/launcher.py",
    "direct/ubuntu/run.sh",
    "LAUNCHER/README.md",
))
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_INVALID_PATH_CHARACTERS = frozenset('<>:"|?*')
_LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
_WINDOWS_RESERVED_BASENAMES = frozenset((
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
))

_PROTECTED_TOP_LEVEL = frozenset((
    ".git", ".mcu_flasher_build_cache", ".mcu_ai_edits", ".pio", ".vscode",
    ".clangd", ".opencode", "temp", "logs", "env", ".venv", ".venv-linux",
    ".venv-macos", ".ubuntu-tools", ".macos-tools", "index_json", "installers",
))
_PROTECTED_DYNAMIC_FILES = frozenset((
    "src/dbs/bootstrap_config.json",
    "src/dbs/arduino_browser_settings.json",
    "src/dbs/arduino_cli_path.txt",
    "src/dbs/dbs_notif.json",
    "src/gui_config.json",
    ".mcu_gui_config.json",
    ".mcu_cloud_link.json",
    ".owner_config.json",
    ".owner_tickets.json",
    "agents.md",
    # The active Windows launcher may still have a process handle while its
    # Bootstrap child runs. Its source update is deferred to a packaged update.
    "launcher/windows.exe",
))
_PROTECTED_PREFIXES = frozenset((
    "src/_python",
    "src/env",
))


class ApplicationUpdateError(RuntimeError):
    """A repository update could not be checked or installed safely."""


class _GitUnavailable(ApplicationUpdateError):
    """The optional local Git client cannot start; archive fallback is allowed."""


@dataclass(frozen=True)
class SourceCandidate:
    """One immutable commit selected from the configured GitHub branch."""

    commit: str
    branch: str
    repository: str = REPOSITORY

    def __post_init__(self) -> None:
        if self.repository != REPOSITORY or not _COMMIT.fullmatch(self.commit):
            raise ApplicationUpdateError("The GitHub source revision is invalid.")
        _validate_branch(self.branch)

    def to_dict(self) -> dict[str, str]:
        return {"repository": self.repository, "branch": self.branch, "commit": self.commit}


@dataclass(frozen=True)
class UpdateCheck:
    """A bounded non-mutating update result."""

    status: str
    candidate: SourceCandidate | None = None
    detail: str = ""


@dataclass(frozen=True)
class InstalledUpdate:
    """A verified repository source refresh that is ready to restart."""

    candidate: SourceCandidate
    changed_files: int
    preserved_files: int
    strategy: str


@dataclass(frozen=True)
class _Operation:
    relative: PurePosixPath
    prior_exists: bool


def _validate_branch(branch: str) -> str:
    if not isinstance(branch, str):
        raise ApplicationUpdateError("The GitHub update branch is invalid.")
    branch = branch.strip()
    if (not _BRANCH.fullmatch(branch) or ".." in branch
            or branch.startswith("/") or branch.endswith("/")):
        raise ApplicationUpdateError("The GitHub update branch is invalid.")
    return branch


def update_branch(config: object | None = None, *, environ: dict[str, str] | None = None) -> str:
    """Return the explicitly selected source branch, defaulting to main."""
    env = os.environ if environ is None else environ
    configured = str(env.get("MCU_FLASHER_UPDATE_BRANCH", "")).strip()
    if not configured and isinstance(config, dict):
        selected = config.get("app_update_branch", "")
        configured = selected.strip() if isinstance(selected, str) else ""
    return _validate_branch(configured or DEFAULT_BRANCH)


def automatic_updates_enabled(config: object | None = None, *, environ: dict[str, str] | None = None) -> bool:
    """Honor the dedicated source-update opt-out while defaulting to auto."""
    env = os.environ if environ is None else environ
    if str(env.get("MCU_FLASHER_DISABLE_APP_UPDATE", "")).strip().casefold() in {"1", "true", "yes"}:
        return False
    if not isinstance(config, dict):
        return True
    return str(config.get("app_update_policy", "auto")).strip().casefold() in {"", "auto"}


def _installation_root(root: Path | str) -> Path:
    path = Path(root).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ApplicationUpdateError(f"The application folder cannot be inspected: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ApplicationUpdateError("The application folder is redirected or invalid.")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ApplicationUpdateError("The application folder cannot be resolved.") from exc


def update_skip_reason(root: Path | str) -> str | None:
    """Return why this installation cannot safely receive an auto refresh."""
    try:
        install_root = _installation_root(root)
    except ApplicationUpdateError as exc:
        return str(exc)
    if (install_root / ".git").exists():
        return "this is a Git working copy; source control manages its updates"
    if not all((install_root / relative).is_file() for relative in REQUIRED_SOURCE_FILES):
        return "the installation does not have the required MCU Flasher source layout"
    return None


def _user_state_base(*, environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = str(env.get("MCU_FLASHER_STATE_DIR", "")).strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    if os.name == "nt":
        base = str(env.get("LOCALAPPDATA") or env.get("APPDATA") or "").strip()
        return Path(base) / "MCUFlasherByNaph" if base else Path.home() / "AppData" / "Local" / "MCUFlasherByNaph"
    base = str(env.get("XDG_STATE_HOME", "")).strip()
    return (Path(base) if base else Path.home() / ".local" / "state") / "mcu-flasher"


def _installation_key(root: Path) -> str:
    value = str(root).casefold() if os.name == "nt" else str(root)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def update_state_path(root: Path | str, *, state_base: Path | str | None = None) -> Path:
    install_root = _installation_root(root)
    base = Path(state_base) if state_base is not None else _user_state_base()
    return base / "application-updates" / _installation_key(install_root) / "update-state.json"


def _read_json(path: Path, *, limit: int = MAX_STATE_BYTES) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(value, sort_keys=True, indent=2) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ApplicationUpdateError(f"The update record could not be saved: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _api_response_is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname is not None and parsed.hostname.casefold() == "api.github.com"


def _archive_response_is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname is not None and parsed.hostname.casefold() == "codeload.github.com"


def _network_deadline(timeout: float, operation: str) -> float:
    """Return a finite monotonic deadline for one remote operation."""
    try:
        seconds = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ApplicationUpdateError(f"{operation} has an invalid time budget.") from exc
    if not 0.0 < seconds < float("inf"):
        raise ApplicationUpdateError(f"{operation} has an invalid time budget.")
    return time.monotonic() + seconds


def _remaining_network_timeout(deadline: float, operation: str) -> float:
    """Bound each socket operation and reject an elapsed overall budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise ApplicationUpdateError(f"{operation} timed out before it could finish.")
    return max(0.001, min(remaining, NETWORK_IO_SLICE_SECONDS))


def _ensure_network_time(deadline: float, operation: str) -> None:
    if time.monotonic() >= deadline:
        raise ApplicationUpdateError(f"{operation} timed out before it could finish.")


def _read_limited_response(response: Any, *, maximum: int, deadline: float, operation: str) -> bytes:
    """Read a small response without letting a trickle defeat its deadline."""
    received = bytearray()
    while len(received) <= maximum:
        _ensure_network_time(deadline, operation)
        chunk = response.read(min(64 * 1024, maximum + 1 - len(received)))
        _ensure_network_time(deadline, operation)
        if not chunk:
            return bytes(received)
        if not isinstance(chunk, bytes):
            raise ApplicationUpdateError(f"{operation} returned an invalid response.")
        received.extend(chunk)
    return bytes(received)


def parse_remote_commit(payload: object, branch: str) -> SourceCandidate:
    """Validate GitHub commit metadata before it controls a checkout."""
    branch = _validate_branch(branch)
    if not isinstance(payload, dict):
        raise ApplicationUpdateError("GitHub returned invalid source update metadata.")
    commit = str(payload.get("sha", "")).strip().casefold()
    return SourceCandidate(commit=commit, branch=branch)


def _read_remote_commit(*, branch: str, timeout: float,
                        opener: Callable[..., Any] = urlopen) -> SourceCandidate:
    operation = "GitHub source metadata"
    deadline = _network_deadline(timeout, operation)
    request = Request(
        COMMIT_API_URL.format(branch=quote(branch, safe="")),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "MCU-Flasher-Bootstrap/11"},
    )
    try:
        with opener(request, timeout=_remaining_network_timeout(deadline, operation)) as response:
            _ensure_network_time(deadline, operation)
            status = int(getattr(response, "status", None) or response.getcode())
            if not _api_response_is_allowed(response.geturl()):
                raise ApplicationUpdateError("GitHub source metadata was redirected to an untrusted location.")
            if status != 200:
                raise ApplicationUpdateError(f"GitHub returned source update status {status}.")
            raw = _read_limited_response(response, maximum=MAX_METADATA_BYTES,
                                         deadline=deadline, operation=operation)
    except HTTPError as exc:
        raise ApplicationUpdateError(f"GitHub could not check for a source update ({exc.code}).") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise ApplicationUpdateError(f"GitHub could not be reached: {exc}") from exc
    if len(raw) > MAX_METADATA_BYTES:
        raise ApplicationUpdateError("GitHub source metadata is too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ApplicationUpdateError("GitHub source metadata is invalid.") from exc
    return parse_remote_commit(payload, branch)


def _bundled_commit(install_root: Path) -> str | None:
    """Return the commit a release ZIP was built from, if this copy records one."""
    commit = _read_json(install_root / BUNDLED_REVISION_FILE, limit=4096).get("commit")
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def _branch_offers_newer_source(bundled: str, candidate: SourceCandidate, *, timeout: float,
                                opener: Callable[..., Any] = urlopen) -> bool:
    """Ask GitHub how the branch relates to this copy's commit.

    A fresh release may be newer than the update branch, so "different" alone must
    never replace it. A rewritten branch has diverged and is still followed.
    """
    operation = "GitHub source comparison"
    deadline = _network_deadline(timeout, operation)
    request = Request(COMPARE_API_URL.format(base=bundled, head=candidate.commit),
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "MCU-Flasher-Bootstrap/11"})
    try:
        with opener(request, timeout=_remaining_network_timeout(deadline, operation)) as response:
            if not _api_response_is_allowed(response.geturl()):
                raise ApplicationUpdateError("GitHub source comparison redirected to an unsafe location.")
            raw = _read_limited_response(response, maximum=MAX_METADATA_BYTES, deadline=deadline, operation=operation)
        status = json.loads(raw.decode("utf-8")).get("status")
    except ApplicationUpdateError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError, UnicodeError, ValueError, AttributeError) as exc:
        raise ApplicationUpdateError(f"GitHub source comparison is unavailable: {exc}") from exc
    if status not in ("ahead", "behind", "identical", "diverged"):
        raise ApplicationUpdateError("GitHub returned an unrecognized source comparison.")
    return status in ("ahead", "diverged")


def _matching_applied_commit(state: dict[str, Any], candidate: SourceCandidate) -> bool:
    return (
        state.get("schema") == STATE_SCHEMA
        and state.get("repository") == candidate.repository
        and state.get("branch") == candidate.branch
        and state.get("applied_commit") == candidate.commit
    )


def check_for_update(root: Path | str, *, config: object | None = None, timeout: float = 3.0,
                     state_base: Path | str | None = None,
                     opener: Callable[..., Any] = urlopen) -> UpdateCheck:
    """Check the canonical GitHub branch with a short, bounded startup request."""
    reason = update_skip_reason(root)
    if reason:
        return UpdateCheck("skipped", detail=reason)
    try:
        branch = update_branch(config)
        candidate = _read_remote_commit(branch=branch, timeout=timeout, opener=opener)
    except ApplicationUpdateError as exc:
        return UpdateCheck("unavailable", detail=str(exc))

    install_root = _installation_root(root)
    state_file = update_state_path(install_root, state_base=state_base)
    state = _read_json(state_file)
    current = _matching_applied_commit(state, candidate)
    bundled = _bundled_commit(install_root) if not state.get("applied_commit") else None
    if bundled and not current:
        # No update has replaced this release yet, so its own commit is what is installed.
        try:
            current = bundled == candidate.commit or not _branch_offers_newer_source(
                bundled, candidate, timeout=timeout, opener=opener)
        except ApplicationUpdateError as exc:
            return UpdateCheck("unavailable", detail=str(exc))
    cached = {
        "schema": STATE_SCHEMA,
        "repository": candidate.repository,
        "branch": candidate.branch,
        "latest_commit": candidate.commit,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "applied_commit": state.get("applied_commit") if isinstance(state.get("applied_commit"), str) else "",
        "applied_at": state.get("applied_at") if isinstance(state.get("applied_at"), str) else "",
        "managed_files": state.get("managed_files") if isinstance(state.get("managed_files"), list) else [],
    }
    # A normal current check is deliberately read-only.  Slow HDD/removable
    # installations should not pay a metadata write at every Bootstrap start.
    if any(state.get(key) != value for key, value in cached.items() if key != "checked_at"):
        try:
            _write_json_atomically(state_file, cached)
        except ApplicationUpdateError:
            # A read-only per-user state directory must not prevent the app from launching.
            pass
    return UpdateCheck("current" if current else "available", candidate)


def _safe_relative(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or len(name) > 1024 or "\\" in name or name.startswith("/"):
        raise ApplicationUpdateError("The staged source contains an unsafe path.")
    path = PurePosixPath(name)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ApplicationUpdateError("The staged source contains an unsafe path.")
    for part in path.parts:
        if ("\x00" in part or any(character in _INVALID_PATH_CHARACTERS for character in part)
                or part.endswith((".", " "))):
            raise ApplicationUpdateError("The staged source contains an unsupported path.")
        # Windows treats these device aliases as special even when an extension
        # is present. Reject them on every host so an archive staged on Linux
        # cannot later become an unsafe or unusable Windows source update.
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_BASENAMES:
            raise ApplicationUpdateError("The staged source contains an unsupported path.")
    return path


def _preserve_local_path(relative: PurePosixPath) -> bool:
    value = relative.as_posix().casefold()
    top = relative.parts[0].casefold()
    if top in _PROTECTED_TOP_LEVEL or value in _PROTECTED_DYNAMIC_FILES:
        return True
    return any(value == prefix or value.startswith(prefix + "/") for prefix in _PROTECTED_PREFIXES)


def _safe_file_below(root: Path, relative: PurePosixPath) -> Path:
    try:
        info = root.lstat()
    except OSError as exc:
        raise ApplicationUpdateError("The staged source root is unavailable.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ApplicationUpdateError("The staged source root is redirected or invalid.")
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ApplicationUpdateError("The staged source is incomplete.") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ApplicationUpdateError("The staged source contains a redirected file.")
        if index + 1 == len(relative.parts):
            if not stat.S_ISREG(info.st_mode):
                raise ApplicationUpdateError("The staged source contains a non-regular file.")
        elif not stat.S_ISDIR(info.st_mode):
            raise ApplicationUpdateError("The staged source has an invalid directory layout.")
    return current


def _safe_destination(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
                info = current.lstat()
            except OSError as exc:
                raise ApplicationUpdateError("The application update directory could not be created.") from exc
        except OSError as exc:
            raise ApplicationUpdateError("The application update destination could not be inspected.") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ApplicationUpdateError("The application update destination is redirected or invalid.")
    return current / relative.parts[-1]


def _is_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(len(_LFS_PREFIX)) == _LFS_PREFIX
    except OSError as exc:
        raise ApplicationUpdateError("A staged source file could not be inspected.") from exc


def _paths_from_names(source_root: Path, names: Iterable[str]) -> tuple[list[PurePosixPath], int]:
    all_paths: set[str] = set()
    selected: list[PurePosixPath] = []
    seen: set[str] = set()
    preserved = 0
    total = 0
    for name in names:
        relative = _safe_relative(name)
        normalized = relative.as_posix()
        key = normalized.casefold()
        if key in seen:
            raise ApplicationUpdateError("The staged source contains colliding file names.")
        seen.add(key)
        all_paths.add(normalized)
        if _preserve_local_path(relative):
            preserved += 1
            continue
        source = _safe_file_below(source_root, relative)
        if _is_lfs_pointer(source):
            raise ApplicationUpdateError("The staged source has an unresolved Git LFS file.")
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise ApplicationUpdateError("A staged source file could not be inspected.") from exc
        if size < 0 or size > MAX_SOURCE_BYTES:
            raise ApplicationUpdateError("The staged source contains an oversized file.")
        total += size
        if total > MAX_SOURCE_BYTES or len(selected) >= MAX_SOURCE_FILES:
            raise ApplicationUpdateError("The staged source exceeds the safe update limit.")
        selected.append(relative)
    if REQUIRED_SOURCE_FILES - all_paths:
        raise ApplicationUpdateError("The staged source is not a complete MCU Flasher application.")
    return sorted(selected, key=lambda item: item.as_posix()), preserved


def _files_equal(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as left, second.open("rb") as right:
            while True:
                left_chunk = left.read(DOWNLOAD_CHUNK_BYTES)
                right_chunk = right.read(DOWNLOAD_CHUNK_BYTES)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(DOWNLOAD_CHUNK_BYTES), b""):
                digest.update(block)
    except OSError as exc:
        raise ApplicationUpdateError("An application update file could not be verified.") from exc
    return digest.hexdigest()


def _copy_file_atomically(source: Path, destination: Path, mode: int | None = None) -> None:
    temporary = destination.parent / f".{destination.name}.mcu-update-{uuid.uuid4().hex}.tmp"
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=DOWNLOAD_CHUNK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
        if mode is not None:
            try:
                os.chmod(temporary, mode)
            except OSError:
                pass
        os.replace(temporary, destination)
    except OSError as exc:
        raise ApplicationUpdateError(f"An application source file could not be installed: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _backup_file(source: Path, backup_root: Path, relative: PurePosixPath) -> None:
    destination = _safe_destination(backup_root, relative)
    try:
        mode = stat.S_IMODE(source.stat().st_mode)
    except OSError as exc:
        raise ApplicationUpdateError("The existing application file could not be backed up.") from exc
    _copy_file_atomically(source, destination, mode)


def _rollback(install_root: Path, backup_root: Path, operations: list[_Operation]) -> list[str]:
    """Restore changed files and retain concise evidence if recovery is incomplete."""
    failures: list[str] = []
    for operation in reversed(operations):
        try:
            destination = _safe_destination(install_root, operation.relative)
            if operation.prior_exists:
                backup = _safe_file_below(backup_root, operation.relative)
                _copy_file_atomically(backup, destination, stat.S_IMODE(backup.stat().st_mode))
            elif destination.exists() or destination.is_symlink():
                info = destination.lstat()
                if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    destination.unlink()
        except Exception as exc:
            # Keep rolling back the other files, but never hide a partial
            # recovery from Bootstrap or the person who must repair it.
            failures.append(f"{operation.relative.as_posix()} ({type(exc).__name__})")
    return failures


def _managed_paths(state: dict[str, Any]) -> set[PurePosixPath]:
    paths: set[PurePosixPath] = set()
    value = state.get("managed_files")
    if not isinstance(value, list) or len(value) > MAX_SOURCE_FILES:
        return paths
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            relative = _safe_relative(item)
        except ApplicationUpdateError:
            continue
        if not _preserve_local_path(relative):
            paths.add(relative)
    return paths


def install_staged_source(root: Path | str, source_root: Path | str, source_names: Iterable[str],
                          candidate: SourceCandidate, *, strategy: str,
                          report: Callable[[str], None] | None = None,
                          state_base: Path | str | None = None) -> InstalledUpdate:
    """Apply a pre-validated staged source tree; public for isolated verification."""
    reason = update_skip_reason(root)
    if reason:
        raise ApplicationUpdateError(f"Automatic application update skipped: {reason}.")
    install_root = _installation_root(root)
    staged_root = Path(source_root)
    selected, preserved = _paths_from_names(staged_root, source_names)
    report = report or (lambda _message: None)
    state_file = update_state_path(install_root, state_base=state_base)
    previous_state = _read_json(state_file)
    previous_managed = _managed_paths(previous_state)
    stale = sorted(previous_managed - set(selected), key=lambda item: item.as_posix())
    try:
        stage_parent = state_file.parent / "staging"
        stage_parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(stage_parent).free < MIN_FREE_SPACE_BYTES:
            raise ApplicationUpdateError("There is not enough free disk space to stage the source update.")
        backup_root = Path(tempfile.mkdtemp(prefix="backup-", dir=stage_parent))
    except OSError as exc:
        if isinstance(exc, ApplicationUpdateError):
            raise
        raise ApplicationUpdateError(f"The source update staging folder could not be created: {exc}") from exc

    operations: list[_Operation] = []
    changed = 0
    try:
        report("Verifying and refreshing application source files…")
        for relative in selected:
            source = _safe_file_below(staged_root, relative)
            destination = _safe_destination(install_root, relative)
            existed = destination.exists() or destination.is_symlink()
            if existed:
                info = destination.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ApplicationUpdateError("An existing application source file is redirected or invalid.")
                if _files_equal(source, destination):
                    continue
                _backup_file(destination, backup_root, relative)
            _copy_file_atomically(source, destination, stat.S_IMODE(source.stat().st_mode))
            operations.append(_Operation(relative, existed))
            changed += 1

        for relative in stale:
            destination = _safe_destination(install_root, relative)
            if not (destination.exists() or destination.is_symlink()):
                continue
            info = destination.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ApplicationUpdateError("A stale application source file is redirected or invalid.")
            _backup_file(destination, backup_root, relative)
            destination.unlink()
            operations.append(_Operation(relative, True))
            changed += 1

        for relative in (PurePosixPath(name) for name in REQUIRED_SOURCE_FILES):
            source = _safe_file_below(staged_root, relative)
            destination = _safe_file_below(install_root, relative)
            if _digest(source) != _digest(destination):
                raise ApplicationUpdateError("A critical application source file did not verify after installation.")

        next_state = {
            "schema": STATE_SCHEMA,
            "repository": candidate.repository,
            "branch": candidate.branch,
            "latest_commit": candidate.commit,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "applied_commit": candidate.commit,
            "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "strategy": strategy,
            "managed_files": [relative.as_posix() for relative in selected],
        }
        _write_json_atomically(state_file, next_state)
        report("GitHub source update verified. Restarting MCU Flasher…")
        return InstalledUpdate(candidate, changed, preserved, strategy)
    except BaseException as failure:
        rollback_failures = _rollback(install_root, backup_root, operations)
        if rollback_failures and isinstance(failure, Exception):
            detail = ", ".join(rollback_failures[:5])
            if len(rollback_failures) > 5:
                detail += ", …"
            raise ApplicationUpdateError(
                f"{failure} Automatic recovery was incomplete ({detail}); manual recovery may be needed."
            ) from failure
        raise
    finally:
        try:
            shutil.rmtree(backup_root)
        except OSError:
            pass


def _run_git(arguments: list[str], *, cwd: Path, timeout: float) -> bytes:
    git = shutil.which("git")
    if not git:
        raise _GitUnavailable("Git is not available for the preferred source clone.")
    environment = os.environ.copy()
    environment.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "https",
        # The updater preserves existing installer payloads. Avoid hydrating
        # their Git LFS binaries only to skip them during the source refresh.
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C",
    })
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [git, *arguments], cwd=str(cwd), env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=max(1.0, float(timeout)), check=False,
            creationflags=flags,
        )
    except FileNotFoundError as exc:
        raise _GitUnavailable("Git is not available for the preferred source clone.") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApplicationUpdateError(f"The GitHub source clone could not finish: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = detail[-1][:300] if detail else f"exit code {completed.returncode}"
        raise ApplicationUpdateError(f"The GitHub source clone failed: {suffix}")
    return completed.stdout


def _canonical_remote(value: str) -> str:
    return value.strip().removesuffix("/").casefold()


def _prepare_git_source(stage: Path, candidate: SourceCandidate,
                        report: Callable[[str], None]) -> tuple[Path, list[str]]:
    report("Cloning the verified GitHub source revision…")
    hooks = stage / "empty-hooks"
    hooks.mkdir()
    checkout = stage / "repository"
    _run_git([
        "-c", f"core.hooksPath={hooks}", "clone", "--depth", "1", "--no-tags", "--single-branch",
        "--branch", candidate.branch, REPOSITORY_URL, str(checkout),
    ], cwd=stage, timeout=CLONE_TIMEOUT_SECONDS)
    origin = _run_git(["-C", str(checkout), "remote", "get-url", "origin"], cwd=stage, timeout=30).decode("utf-8", errors="strict")
    if _canonical_remote(origin) != _canonical_remote(REPOSITORY_URL):
        raise ApplicationUpdateError("The cloned GitHub source does not have the expected origin.")
    revision = _run_git(["-C", str(checkout), "rev-parse", "HEAD"], cwd=stage, timeout=30).decode("ascii", errors="strict").strip().casefold()
    if revision != candidate.commit:
        raise ApplicationUpdateError("The cloned GitHub source does not match the verified commit.")
    raw_paths = _run_git(["-C", str(checkout), "ls-files", "-z"], cwd=stage, timeout=60)
    try:
        names = [part.decode("utf-8", errors="strict") for part in raw_paths.split(b"\0") if part]
    except UnicodeError as exc:
        raise ApplicationUpdateError("The cloned GitHub source has an invalid file name.") from exc
    return checkout, names


def _download_source_archive(candidate: SourceCandidate, archive: Path, *, report: Callable[[str], None],
                             opener: Callable[..., Any] = urlopen) -> None:
    report("Git is unavailable; downloading the same verified GitHub source revision…")
    operation = "GitHub source download"
    deadline = _network_deadline(ARCHIVE_TIMEOUT_SECONDS, operation)
    request = Request(
        SOURCE_ARCHIVE_URL.format(commit=candidate.commit),
        headers={"Accept": "application/zip", "User-Agent": "MCU-Flasher-Bootstrap/11"},
    )
    received = 0
    try:
        with opener(request, timeout=_remaining_network_timeout(deadline, operation)) as response:
            _ensure_network_time(deadline, operation)
            status = int(getattr(response, "status", None) or response.getcode())
            if status != 200 or not _archive_response_is_allowed(response.geturl()):
                raise ApplicationUpdateError("GitHub source download was redirected to an untrusted location.")
            length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
            if length and (not str(length).isdigit() or int(length) > MAX_ARCHIVE_BYTES):
                raise ApplicationUpdateError("The GitHub source archive is too large.")
            with archive.open("xb") as stream:
                while True:
                    _ensure_network_time(deadline, operation)
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    _ensure_network_time(deadline, operation)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ApplicationUpdateError("GitHub source download returned an invalid response.")
                    received += len(chunk)
                    if received > MAX_ARCHIVE_BYTES:
                        raise ApplicationUpdateError("The GitHub source archive is too large.")
                    stream.write(chunk)
    except HTTPError as exc:
        raise ApplicationUpdateError(f"GitHub could not download the source update ({exc.code}).") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise ApplicationUpdateError(f"GitHub source download failed: {exc}") from exc
    if not received:
        raise ApplicationUpdateError("GitHub returned an empty source archive.")


def _extract_source_archive(archive: Path, destination: Path) -> tuple[Path, list[str]]:
    """Extract one GitHub checkout wrapper, rejecting links and path escapes."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ApplicationUpdateError("The GitHub source archive contains too many files.")
            wrapper: str | None = None
            files: list[tuple[PurePosixPath, zipfile.ZipInfo]] = []
            seen: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename.rstrip("/") if info.is_dir() else info.filename
                if not name:
                    continue
                full = _safe_relative(name)
                if len(full.parts) < 2:
                    raise ApplicationUpdateError("The GitHub source archive has an invalid layout.")
                if wrapper is None:
                    wrapper = full.parts[0]
                elif full.parts[0] != wrapper:
                    raise ApplicationUpdateError("The GitHub source archive has multiple roots.")
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise ApplicationUpdateError("The GitHub source archive contains a non-regular file.")
                relative = PurePosixPath(*full.parts[1:])
                key = relative.as_posix().casefold()
                if key in seen:
                    raise ApplicationUpdateError("The GitHub source archive contains colliding file names.")
                seen.add(key)
                if info.file_size < 0 or info.file_size > MAX_ARCHIVE_EXTRACTED_BYTES:
                    raise ApplicationUpdateError("The GitHub source archive contains an oversized file.")
                total += info.file_size
                if total > MAX_ARCHIVE_EXTRACTED_BYTES:
                    raise ApplicationUpdateError("The GitHub source archive expands beyond the safe limit.")
                if info.file_size > 8 * 1024 * 1024 and info.compress_size * 500 < info.file_size:
                    raise ApplicationUpdateError("The GitHub source archive has an unsafe compression ratio.")
                files.append((relative, info))
            if wrapper is None:
                raise ApplicationUpdateError("The GitHub source archive is empty.")
            source_root = destination / "source"
            source_root.mkdir()
            for relative, info in files:
                output = _safe_destination(source_root, relative)
                with bundle.open(info, "r") as reader, output.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=DOWNLOAD_CHUNK_BYTES)
                mode = info.external_attr >> 16
                if mode:
                    try:
                        os.chmod(output, stat.S_IMODE(mode))
                    except OSError:
                        pass
    except (OSError, zipfile.BadZipFile) as exc:
        raise ApplicationUpdateError(f"The GitHub source archive could not be extracted safely: {exc}") from exc
    return source_root, [relative.as_posix() for relative, _info in files]


def apply_source_update(root: Path | str, candidate: SourceCandidate, *,
                        report: Callable[[str], None] | None = None,
                        state_base: Path | str | None = None,
                        opener: Callable[..., Any] = urlopen) -> InstalledUpdate:
    """Stage a clone (or immutable archive fallback), verify it and refresh the app."""
    reason = update_skip_reason(root)
    if reason:
        raise ApplicationUpdateError(f"Automatic application update skipped: {reason}.")
    install_root = _installation_root(root)
    report = report or (lambda _message: None)
    state_file = update_state_path(install_root, state_base=state_base)
    try:
        stage_parent = state_file.parent / "staging"
        stage_parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(stage_parent).free < MIN_FREE_SPACE_BYTES:
            raise ApplicationUpdateError("There is not enough free disk space to stage the source update.")
        stage = Path(tempfile.mkdtemp(prefix="source-", dir=stage_parent))
    except OSError as exc:
        if isinstance(exc, ApplicationUpdateError):
            raise
        raise ApplicationUpdateError(f"The source update staging folder could not be created: {exc}") from exc

    try:
        try:
            source_root, names = _prepare_git_source(stage, candidate, report)
            strategy = "git-clone"
        except _GitUnavailable:
            report("Clone unavailable; using GitHub's immutable source archive instead…")
            archive = stage / "source.zip"
            _download_source_archive(candidate, archive, report=report, opener=opener)
            source_root, names = _extract_source_archive(archive, stage)
            strategy = "github-source-archive"
        return install_staged_source(install_root, source_root, names, candidate, strategy=strategy,
                                     report=report, state_base=state_base)
    finally:
        try:
            shutil.rmtree(stage)
        except OSError:
            pass
