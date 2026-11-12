"""Repository-backed application updates for MCU Flasher Bootstrap.

Bootstrap checks the canonical GitHub repository before the GUI starts.  When
the configured branch moves ahead of this copy, it stages a fresh, shallow Git
clone and only then refreshes the application-owned source files in place. If
Git is not available, it uses GitHub's immutable archive for the same verified
commit, so a normal Windows or native Ubuntu installation does not depend on a
separately installed Git client.

An update never moves a copy backwards. A copy records the commit it holds in
``src/dbs/source_revision.json``, written by the release build and by every
update. GitHub must say the branch is newer than that before anything changes,
and a copy that cannot say what it holds is left exactly as it is: replacing it
could destroy newer work, and a missed update costs nothing.

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
import sys
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
# A verified blobless checkout kept between updates, beside the update record, so
# an update downloads only the files that changed. Past this size it starts over.
SOURCE_CACHE_DIRECTORY = "source-cache"
MAX_SOURCE_CACHE_BYTES = 512 * 1024 * 1024
ARCHIVE_TIMEOUT_SECONDS = 15 * 60
# Each startup request gets this long. A launch normally makes two, and a window
# appears only afterwards, so the launcher's whole check is bounded by the budget.
# Running out of it only postpones an update to a later launch.
STARTUP_CHECK_TIMEOUT_SECONDS = 3.0
STARTUP_CHECK_BUDGET_SECONDS = 6.0
# ``urlopen`` applies its timeout to individual socket operations. These bound
# how far one blocked operation can overrun a deadline, and a stall this long is
# treated as failure, so an ordinary pause must fit: one second made slow links
# look offline and ended archive downloads at their first brief stall.
METADATA_IO_SLICE_SECONDS = 2.0
ARCHIVE_IO_SLICE_SECONDS = 30.0
# A scanner can briefly hold a file just written. Only these Windows sharing
# errors retry, with the same elapsed-time discipline as the release seed.
_WINDOWS_SHARING_ERRORS = frozenset((5, 32, 33))
REPLACE_ATTEMPTS = 8
REPLACE_RETRY_SECONDS = 0.25

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
    # This copy's own record. An update writes it directly and never takes it from a checkout.
    "src/dbs/source_revision.json",
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


class _UnknownRecordedCommit(ApplicationUpdateError):
    """GitHub has never seen the commit a copy records, so it cannot compare it."""


# A shallow clone is about 16 MB, which a slow link needs minutes for. Git reports
# its own phases; they are spread over one bar so it never runs back to zero.
_CLONE_PHASES = {
    "receiving objects": (0.0, 85.0, "Downloading"),
    "resolving deltas": (85.0, 93.0, "Unpacking"),
    "updating files": (93.0, 100.0, "Writing files"),
    "checking out files": (93.0, 100.0, "Writing files"),
}
_GIT_PROGRESS = re.compile(
    r"^(?:remote:\s*)?(?P<phase>[A-Za-z][A-Za-z ]*?):\s+(?:(?P<percent>\d{1,3})%\s*)?"
    r"(?:\((?P<done>\d+)/(?P<total>\d+)\))?,?\s*(?P<rest>.*)$"
)
# Progress is reported at most this often, and at least this often while a phase
# is quiet, so the elapsed time keeps moving even while GitHub prepares the pack.
PROGRESS_INTERVAL_SECONDS = 0.25
PROGRESS_HEARTBEAT_SECONDS = 1.0


@dataclass(frozen=True)
class TransferProgress:
    """How far a source download has come, for a progress bar or a log line."""

    phase: str
    # Across the whole download, 0–100; None while nothing can be measured yet.
    percent: float | None
    detail: str = ""
    elapsed: float = 0.0

    def describe(self) -> str:
        parts = [self.phase]
        if self.percent is not None:
            parts.append(f"{self.percent:.0f}%")
        if self.detail:
            parts.append(self.detail)
        minutes, seconds = divmod(int(self.elapsed), 60)
        parts.append(f"{minutes}:{seconds:02d} elapsed")
        return " · ".join(parts)


def progress_bar(percent: float | None, width: int = 24, *, ascii_only: bool = False) -> str:
    """A fixed-width text bar; an unknown amount shows as an empty bar."""
    full, empty = ("#", "-") if ascii_only else ("█", "░")
    filled = 0 if percent is None else max(0, min(width, round(width * float(percent) / 100.0)))
    return f"[{full * filled}{empty * (width - filled)}]"


def parse_git_progress(line: str) -> tuple[str, float | None, str] | None:
    """Read one of git's progress records as (phase, overall percent, detail)."""
    match = _GIT_PROGRESS.match(line.strip())
    if not match:
        return None
    key = match.group("phase").strip().casefold()
    rest = match.group("rest").strip().rstrip(".").strip()
    if rest.casefold().endswith("done"):
        rest = rest[:-4].rstrip(", ").strip()
    detail = rest
    if match.group("done") and match.group("total") and not rest:
        detail = f"{match.group('done')}/{match.group('total')}"
    if key in _CLONE_PHASES:
        if match.group("percent") is None:
            return None
        start, end, label = _CLONE_PHASES[key]
        fraction = min(100, int(match.group("percent"))) / 100.0
        return label, start + (end - start) * fraction, detail
    if line.lstrip().startswith("remote:") and key in {
            "enumerating objects", "counting objects", "compressing objects", "total"}:
        # GitHub is still assembling the pack: nothing has arrived to measure yet.
        return "GitHub is preparing the source", None, ""
    return None


@dataclass(frozen=True)
class SourceCandidate:
    """One immutable commit selected from the configured GitHub branch."""

    commit: str
    branch: str
    repository: str = REPOSITORY
    # When GitHub says this commit was made. Only needed to judge a rewritten branch.
    committed_at: str = ""

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
    """Honor both Skip Updates and the dedicated source-update opt-out."""
    env = os.environ if environ is None else environ
    if str(env.get("MCU_FLASHER_DISABLE_APP_UPDATE", "")).strip().casefold() in {"1", "true", "yes"}:
        return False
    if str(env.get("MCU_FLASH_GUI_SKIP_UPDATES", "")).strip().casefold() in {"1", "true", "yes"}:
        return False
    if not isinstance(config, dict):
        return True
    return config.get("skip_updates") is not True and str(config.get("app_update_policy", "auto")).strip().casefold() in {"", "auto"}


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


def _remaining_network_timeout(deadline: float, operation: str,
                               io_slice: float = METADATA_IO_SLICE_SECONDS) -> float:
    """Bound each socket operation and reject an elapsed overall budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise ApplicationUpdateError(f"{operation} timed out before it could finish.")
    return max(0.001, min(remaining, io_slice))


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


def _commit_date(payload: object) -> str:
    """The committer date from GitHub commit metadata, or nothing if it is not usable."""
    try:
        value = payload["commit"]["committer"]["date"]  # type: ignore[index]
    except (KeyError, TypeError):
        return ""
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value) else ""


def parse_remote_commit(payload: object, branch: str) -> SourceCandidate:
    """Validate GitHub commit metadata before it controls a checkout."""
    branch = _validate_branch(branch)
    if not isinstance(payload, dict):
        raise ApplicationUpdateError("GitHub returned invalid source update metadata.")
    commit = str(payload.get("sha", "")).strip().casefold()
    return SourceCandidate(commit=commit, branch=branch, committed_at=_commit_date(payload))


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


_RECORD_DATE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _recorded_commit(install_root: Path) -> str | None:
    """The commit this copy's own record names, written by a release build or an update."""
    commit = _read_json(install_root / BUNDLED_REVISION_FILE, limit=4096).get("commit")
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def _recorded_date(record: dict[str, Any], *keys: str) -> str:
    """The first fixed-width UTC commit date a record carries, or nothing usable."""
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and _RECORD_DATE.fullmatch(value):
            return value
    return ""


def _known_dates(install_root: Path, state: dict[str, Any]) -> dict[str, str]:
    """When each known commit was made, as far as this copy's own records say.

    GitHub can only compare commits it has. A record whose commit was amended or
    never pushed (one release recorded such a commit) answers 404 forever; its
    date is then the only honest way to tell whether the branch is later work.
    ``timestamp`` is what that hand-made release record called its date.
    """
    dates: dict[str, str] = {}
    record = _read_json(install_root / BUNDLED_REVISION_FILE, limit=4096)
    recorded = _recorded_commit(install_root)
    if recorded:
        dates[recorded] = _recorded_date(record, "committed_at", "timestamp")
    applied = state.get("applied_commit")
    if isinstance(applied, str) and applied not in dates:
        dates[applied] = _recorded_date(state, "applied_committed_at")
    return dates


def _known_commits(install_root: Path, state: dict[str, Any]) -> list[str]:
    """Every commit this copy is known to hold.

    Both can be true at once and either can be the newer: a release extracted over an
    updated folder has a newer record than the per-user state left from before, and an
    update applied by an older version has newer state than the release's own record.
    """
    known = []
    recorded = _recorded_commit(install_root)
    if recorded:
        known.append(recorded)
    applied = state.get("applied_commit")
    if (isinstance(applied, str) and _COMMIT.fullmatch(applied) and applied not in known
            and state.get("repository") in (None, REPOSITORY)):
        known.append(applied)
    return known


def _branch_offers_newer_source(installed: str, candidate: SourceCandidate, *, timeout: float,
                                opener: Callable[..., Any] = urlopen) -> bool:
    """Ask GitHub how the branch relates to a commit this copy holds.

    A fresh release may be newer than the update branch, so "different" alone must
    never replace it. A rewritten branch has diverged, so neither contains the other:
    it is followed only when it really is the later work.
    """
    operation = "GitHub source comparison"
    deadline = _network_deadline(timeout, operation)
    request = Request(COMPARE_API_URL.format(base=installed, head=candidate.commit),
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "MCU-Flasher-Bootstrap/11"})
    try:
        with opener(request, timeout=_remaining_network_timeout(deadline, operation)) as response:
            if not _api_response_is_allowed(response.geturl()):
                raise ApplicationUpdateError("GitHub source comparison redirected to an unsafe location.")
            raw = _read_limited_response(response, maximum=MAX_METADATA_BYTES, deadline=deadline, operation=operation)
        comparison = json.loads(raw.decode("utf-8"))
        status = comparison.get("status")
    except ApplicationUpdateError:
        raise
    except HTTPError as exc:
        if exc.code in (404, 422):
            # GitHub answered, and it has no such commit (or no shared history with it).
            raise _UnknownRecordedCommit(f"GitHub does not know the recorded commit {installed[:7]}.") from exc
        raise ApplicationUpdateError(f"GitHub source comparison is unavailable: {exc}") from exc
    except (URLError, OSError, TimeoutError, UnicodeError, ValueError, AttributeError) as exc:
        raise ApplicationUpdateError(f"GitHub source comparison is unavailable: {exc}") from exc
    if status not in ("ahead", "behind", "identical", "diverged"):
        raise ApplicationUpdateError("GitHub returned an unrecognized source comparison.")
    if status != "diverged":
        return status == "ahead"
    installed_at = _commit_date(comparison.get("base_commit"))
    # These dates are fixed-width UTC, so they compare as text. Without both, nothing says this is newer.
    return bool(installed_at and candidate.committed_at and candidate.committed_at > installed_at)


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
    known = _known_commits(install_root, state)
    if not known:
        return UpdateCheck(
            "unidentified", candidate,
            "this copy does not record which version it is, so it is left as it is")
    # Replacing needs the branch to be newer than everything this copy might hold.
    current = candidate.commit in known
    dates = _known_dates(install_root, state)
    try:
        for installed in known:
            if current:
                break
            try:
                current = not _branch_offers_newer_source(installed, candidate, timeout=timeout, opener=opener)
            except _UnknownRecordedCommit:
                # Judge an unknown commit as a rewritten branch is judged: only clearly
                # later work replaces it, and without both dates nothing does.
                recorded_at = dates.get(installed, "")
                if not (recorded_at and candidate.committed_at):
                    raise
                current = not candidate.committed_at > recorded_at
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
    if _recorded_date(state, "applied_committed_at"):
        cached["applied_committed_at"] = state["applied_committed_at"]
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


def _replace_file(temporary: Path, destination: Path) -> None:
    """Publish one finished file, waiting out a brief Windows sharing violation."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, destination)
            return
        except OSError as exc:
            if (getattr(exc, "winerror", None) not in _WINDOWS_SHARING_ERRORS
                    or attempt + 1 == REPLACE_ATTEMPTS):
                raise
            time.sleep(REPLACE_RETRY_SECONDS)


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
        _replace_file(temporary, destination)
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
                          state_base: Path | str | None = None,
                          cancelled: Callable[[], bool] | None = None) -> InstalledUpdate:
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
        if cancelled is not None and cancelled():
            raise ApplicationUpdateError("Application update skipped before installation.")
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

        # The record lives in the copy, so it stays true if the folder moves to another
        # path or computer, where the per-user state below does not follow.
        record = PurePosixPath(BUNDLED_REVISION_FILE)
        destination = _safe_destination(install_root, record)
        existed = destination.exists() or destination.is_symlink()
        if existed:
            _backup_file(destination, backup_root, record)
        written = {"repository": candidate.repository, "commit": candidate.commit}
        if candidate.committed_at:
            # Lets a later check judge this copy by date if GitHub ever loses the commit.
            written["committed_at"] = candidate.committed_at
        _write_json_atomically(destination, written)
        operations.append(_Operation(record, existed))

        next_state = {
            "schema": STATE_SCHEMA,
            "repository": candidate.repository,
            "branch": candidate.branch,
            "latest_commit": candidate.commit,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "applied_commit": candidate.commit,
            "applied_committed_at": candidate.committed_at,
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


def _git_environment() -> dict[str, str]:
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
    return environment


def _git_executable() -> str:
    git = shutil.which("git")
    if not git:
        raise _GitUnavailable("Git is not available for the preferred source clone.")
    return git


def _run_git(arguments: list[str], *, cwd: Path, timeout: float) -> bytes:
    git = _git_executable()
    environment = _git_environment()
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


def _stop_git(process: subprocess.Popen) -> None:
    """End the clone and the transport helpers it started, then reap it."""
    if process.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                import signal
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class _Cancelled(ApplicationUpdateError):
    """The person or a Skip Updates choice stopped the download; never retried."""


class _NetworkFailure(ApplicationUpdateError):
    """GitHub could not be reached. A full clone over the same link would fail too."""


# What git says when the connection, not the repository, is the problem.
_NETWORK_FAILURES = (
    "could not resolve host", "failed to connect", "couldn't connect", "connection timed out",
    "connection reset", "connection refused", "operation timed out", "network is unreachable",
    "rpc failed", "early eof", "unexpected disconnect", "the remote end hung up", "http/2 stream",
    # Whole phrases: a bare "ssl" also matches folder names such as /home/russlee.
    "ssl certificate", "ssl connect", "ssl_read", "ssl_write", "openssl", "gnutls", "schannel",
    "tls connection",
)
NETWORK_RETRY_DELAY_SECONDS = 2.0


def _git_with_progress(arguments: list[str], *, cwd: Path, deadline: float,
                       progress: Callable[[TransferProgress], None] | None,
                       cancelled: Callable[[], bool] | None, started: float | None = None,
                       span: tuple[float, float] = (0.0, 100.0), phase: str = "Connecting to GitHub",
                       stdin_data: bytes | None = None, operation: str = "clone",
                       final: str | None = "Source downloaded") -> None:
    """Run one git network or checkout command and pass on what it says while it works.

    Git writes its progress to stderr, separated by carriage returns. It is read on
    a thread so the deadline, cancellation and a steady elapsed-time heartbeat keep
    working while GitHub is silent. ``span`` places this command's share on one bar
    shared by every step of an update, so the bar never runs back towards zero.
    """
    import queue
    import threading

    git = _git_executable()
    started = time.monotonic() if started is None else started
    low, high = span
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        options["start_new_session"] = True
    if time.monotonic() >= deadline:
        raise ApplicationUpdateError(f"The GitHub source {operation} ran out of time.")
    try:
        process = subprocess.Popen(
            [git, *arguments], cwd=str(cwd), env=_git_environment(),
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **options,
        )
    except FileNotFoundError as exc:
        raise _GitUnavailable("Git is not available for the preferred source clone.") from exc
    except OSError as exc:
        raise ApplicationUpdateError(f"The GitHub source {operation} could not start: {exc}") from exc

    chunks: "queue.Queue[bytes | None]" = queue.Queue()

    def read_stderr() -> None:
        try:
            while True:
                chunk = process.stderr.read1(4096) if hasattr(process.stderr, "read1") else process.stderr.read(4096)
                if not chunk:
                    break
                chunks.put(chunk)
        except (OSError, ValueError):
            pass
        finally:
            chunks.put(None)

    def write_stdin() -> None:
        # A long object list must not wait on a full pipe while stderr is being read.
        try:
            process.stdin.write(stdin_data)
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    reader = threading.Thread(target=read_stderr, name="MCUSourceUpdateProgress", daemon=True)
    reader.start()
    writer = None
    if stdin_data is not None:
        writer = threading.Thread(target=write_stdin, name="MCUSourceUpdateInput", daemon=True)
        writer.start()
    pending = ""
    messages: list[str] = []
    detail = ""
    initial = phase
    # A later step starts where the earlier ones left the bar.
    percent: float | None = low if low > 0 else None
    last_sent = 0.0
    sent_state: tuple[str, int | None, str] | None = None

    def send(force: bool = False) -> None:
        nonlocal last_sent, sent_state
        if progress is None:
            return
        now = time.monotonic()
        state = (phase, None if percent is None else int(percent), detail)
        due = now - last_sent >= (PROGRESS_INTERVAL_SECONDS if state != sent_state else PROGRESS_HEARTBEAT_SECONDS)
        if not (force or due):
            return
        last_sent, sent_state = now, state
        try:
            progress(TransferProgress(phase, percent, detail, now - started))
        except Exception:
            pass  # A display failure must not stop or fail the download itself.

    def consume(text: str) -> None:
        nonlocal phase, percent, detail
        parsed = parse_git_progress(text)
        if parsed is None:
            if text.strip():
                messages.append(text.strip()[:300])
                del messages[:-20]
            return
        new_phase, new_percent, new_detail = parsed
        if new_percent is None and percent is not None:
            return  # A late remote line must not empty a bar that has started filling.
        if new_phase == "Downloading" and initial != "Connecting to GitHub":
            new_phase = initial  # "Downloading 33 changed files" says more than git's own name.
        phase, detail = new_phase, new_detail
        if new_percent is not None:
            new_percent = low + (high - low) * new_percent / 100.0
        percent = new_percent if new_percent is None or percent is None else max(percent, new_percent)

    send(force=True)
    finished = False
    try:
        while not finished:
            if cancelled is not None and cancelled():
                raise _Cancelled(f"The GitHub source {operation} was cancelled.")
            if time.monotonic() >= deadline:
                raise ApplicationUpdateError(
                    f"The GitHub source {operation} did not finish within {CLONE_TIMEOUT_SECONDS // 60} minutes.")
            try:
                chunk = chunks.get(timeout=0.2)
            except queue.Empty:
                send()
                continue
            if chunk is None:
                finished = True
            else:
                pending += chunk.decode("utf-8", errors="replace")
            *complete, pending = re.split(r"[\r\n]", pending)
            if finished:
                complete.append(pending)
                pending = ""
            for text in complete:
                consume(text)
            send()
        remaining = max(1.0, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise ApplicationUpdateError(f"The GitHub source {operation} could not finish in time.") from exc
    except BaseException:
        _stop_git(process)
        raise
    finally:
        reader.join(timeout=2)
        if writer is not None:
            writer.join(timeout=2)
        try:
            process.stderr.close()
        except OSError:
            pass
    if returncode != 0:
        suffix = messages[-1] if messages else f"exit code {returncode}"
        failure = (_NetworkFailure if any(marker in message.casefold() for message in messages
                                          for marker in _NETWORK_FAILURES) else ApplicationUpdateError)
        raise failure(f"The GitHub source {operation} failed: {suffix}")
    if final:
        phase, percent, detail = final, high, ""
        send(force=True)


def _clone_with_progress(arguments: list[str], *, cwd: Path, timeout: float,
                         progress: Callable[[TransferProgress], None] | None,
                         cancelled: Callable[[], bool] | None) -> None:
    """One complete ``git clone --progress`` on its own bar."""
    _git_with_progress(arguments, cwd=cwd, deadline=time.monotonic() + max(1.0, float(timeout)),
                       progress=progress, cancelled=cancelled)


def _canonical_remote(value: str) -> str:
    return value.strip().removesuffix("/").casefold()


def _remove_tree(path: Path) -> None:
    """Remove a staging or cache folder, including Git's read-only pack files on Windows."""
    def make_writable(function, name, _error):
        try:
            os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
            function(name)
        except OSError:
            pass

    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=make_writable)
        else:
            shutil.rmtree(path, onerror=make_writable)
    except OSError:
        pass


def _tree_bytes(path: Path) -> int:
    total = 0
    for folder, _directories, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def _verified_checkout(checkout: Path, candidate: SourceCandidate, cwd: Path) -> list[str]:
    """Origin, exact commit and the tracked file list of a finished checkout."""
    origin = _run_git(["-C", str(checkout), "remote", "get-url", "origin"], cwd=cwd, timeout=30).decode("utf-8", errors="strict")
    if _canonical_remote(origin) != _canonical_remote(REPOSITORY_URL):
        raise ApplicationUpdateError("The cloned GitHub source does not have the expected origin.")
    revision = _run_git(["-C", str(checkout), "rev-parse", "HEAD"], cwd=cwd, timeout=30).decode("ascii", errors="strict").strip().casefold()
    if revision != candidate.commit:
        raise ApplicationUpdateError("The cloned GitHub source does not match the verified commit.")
    raw_paths = _run_git(["-C", str(checkout), "ls-files", "-z"], cwd=cwd, timeout=60)
    try:
        return [part.decode("utf-8", errors="strict") for part in raw_paths.split(b"\0") if part]
    except UnicodeError as exc:
        raise ApplicationUpdateError("The cloned GitHub source has an invalid file name.") from exc


def _claim_source_cache(cache: Path, checkout: Path) -> bool:
    """Take the kept checkout for this update; a second launcher simply clones afresh."""
    try:
        if cache.is_symlink() or not (cache / ".git").is_dir():
            _remove_tree(cache)
            return False
        if _tree_bytes(cache / ".git") > MAX_SOURCE_CACHE_BYTES:
            _remove_tree(cache)
            return False
        os.rename(cache, checkout)
        return True
    except OSError:
        return False


def _keep_source_cache(checkout: Path, cache: Path) -> None:
    """Keep a verified checkout so the next update downloads only what changed."""
    try:
        if checkout.is_dir() and not os.path.lexists(cache):
            os.rename(checkout, cache)
    except OSError:
        pass  # Only the next update's speed depends on this.


def _return_source_cache(checkout: Path, cache: Path, reused: bool) -> None:
    """Put a kept checkout back after an interrupted update; its objects are intact.

    Git writes fetched packs atomically, and every use writes the files again, so
    a download cut off half way leaves nothing that the next update could trust.
    A first download that never finished is simply discarded.
    """
    if reused:
        _keep_source_cache(checkout, cache)
    else:
        _remove_tree(checkout)


def _cached_git_source(stage: Path, checkout: Path, hooks: Path, candidate: SourceCandidate, *,
                       reused: bool, deadline: float, started: float,
                       progress: Callable[[TransferProgress], None] | None,
                       cancelled: Callable[[], bool] | None) -> list[str]:
    """Bring a blobless checkout to the verified commit, downloading only missing files.

    The checkout keeps every file's contents from earlier updates. A shallow fetch of
    the new commit brings its folder listings (kilobytes), ``rev-list --missing``
    names the file contents this copy lacks, and one fetch downloads just those.
    The first update downloads every file once, about 16 MB; later ones only what
    changed. Every step is a visible git progress record.
    """
    hook = ["-c", f"core.hooksPath={hooks}"]
    in_checkout = ["-C", str(checkout), *hook]
    common = dict(cwd=stage, deadline=deadline, progress=progress, cancelled=cancelled, started=started)
    if reused:
        origin = _run_git(["-C", str(checkout), "remote", "get-url", "origin"], cwd=stage, timeout=30)
        promisor = _run_git(["-C", str(checkout), "config", "--get", "remote.origin.promisor"], cwd=stage, timeout=30)
        if (_canonical_remote(origin.decode("utf-8", errors="replace")) != _canonical_remote(REPOSITORY_URL)
                or promisor.strip() != b"true"):
            raise ApplicationUpdateError("The kept source checkout is not a GitHub source checkout.")
    else:
        _git_with_progress([
            *hook, "clone", "--progress", "--filter=blob:none", "--depth", "1", "--no-tags",
            "--single-branch", "--no-checkout", "--branch", candidate.branch, REPOSITORY_URL, str(checkout),
        ], span=(0.0, 3.0), final=None, operation="clone", **common)
    # The exact verified commit, never whatever the branch has moved on to since.
    _git_with_progress([
        *in_checkout, "fetch", "--progress", "--filter=blob:none", "--depth", "1", "--no-tags",
        "origin", candidate.commit,
    ], span=(3.0, 6.0), phase="Reading the new revision", final=None, operation="update", **common)
    listed = _run_git([*in_checkout, "rev-list", "--objects", "--missing=print", "--no-object-names",
                       candidate.commit], cwd=stage, timeout=120)
    missing = sorted({line[1:].strip() for line in listed.decode("ascii", errors="strict").splitlines()
                      if line.startswith("?")})
    if any(not _COMMIT.fullmatch(identifier) for identifier in missing):
        raise ApplicationUpdateError("Git listed an invalid object identifier.")
    if missing:
        _git_with_progress([
            *in_checkout, "-c", "fetch.negotiationAlgorithm=noop", "fetch", "--progress", "--no-tags",
            "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none", "--stdin", "origin",
        ], span=(6.0, 92.0), phase=f"Downloading {len(missing)}{' changed' if reused else ''} file{'' if len(missing) == 1 else 's'}", final=None,
            operation="update", stdin_data="".join(f"{identifier}\n" for identifier in missing).encode("ascii"),
            **common)
    # Every file is written again from Git's own objects, so nothing left in a kept
    # checkout can survive into the installation unnoticed.
    for entry in checkout.iterdir():
        if entry.name != ".git":
            _remove_tree(entry)
    _git_with_progress([*in_checkout, "checkout", "--progress", "--force", "--detach", candidate.commit],
                       span=(92.0, 100.0), phase="Writing files", final="Source downloaded",
                       operation="update", **common)
    _run_git([*in_checkout, "clean", "-ffdxq"], cwd=stage, timeout=120)
    return _verified_checkout(checkout, candidate, stage)


def _prepare_git_source(stage: Path, candidate: SourceCandidate,
                        report: Callable[[str], None],
                        progress: Callable[[TransferProgress], None] | None = None,
                        cancelled: Callable[[], bool] | None = None,
                        cache: Path | None = None) -> tuple[Path, list[str]]:
    hooks = stage / "empty-hooks"
    hooks.mkdir()
    started = time.monotonic()
    deadline = started + CLONE_TIMEOUT_SECONDS
    if cache is not None:
        checkout = stage / "repository"
        reused = _claim_source_cache(cache, checkout)
        report("Downloading only what changed in the verified GitHub source revision…" if reused else
               "Downloading the verified GitHub source revision (about 16 MB, once)…")
        attempt = 0
        while True:
            attempt += 1
            try:
                return checkout, _cached_git_source(stage, checkout, hooks, candidate, reused=reused,
                                                    deadline=deadline, started=started,
                                                    progress=progress, cancelled=cancelled)
            except (_GitUnavailable, _Cancelled):
                _return_source_cache(checkout, cache, reused)
                raise
            except _NetworkFailure:
                # A dropped connection is not a reason to download everything again over
                # the same link: try the small download once more, then leave the update
                # for the next launch and keep what was already downloaded.
                # A first download whose clone finished goes on from what it has.
                reused = (checkout / ".git").is_dir()
                if attempt >= 2 or time.monotonic() + NETWORK_RETRY_DELAY_SECONDS >= deadline:
                    _return_source_cache(checkout, cache, reused)
                    raise
                report("GitHub could not be reached; trying again…")
                if not reused:
                    _remove_tree(checkout)
                pause = time.monotonic() + NETWORK_RETRY_DELAY_SECONDS
                while time.monotonic() < pause:
                    if cancelled is not None and cancelled():
                        _return_source_cache(checkout, cache, reused)
                        raise _Cancelled("The GitHub source update was cancelled.")
                    time.sleep(0.1)
            except ApplicationUpdateError as exc:
                if cancelled is not None and cancelled():
                    _return_source_cache(checkout, cache, reused)
                    raise _Cancelled("The GitHub source update was cancelled.") from exc
                if time.monotonic() >= deadline:
                    raise
                # A server without partial clone, an old Git or a damaged kept checkout:
                # the plain shallow clone below is the long-proven path.
                report(f"Incremental source download unavailable ({exc}); cloning the full revision…")
                _remove_tree(checkout)
                break
    report("Cloning the verified GitHub source revision (about 16 MB)…")
    checkout = stage / "repository-full"
    _git_with_progress([
        "-c", f"core.hooksPath={hooks}", "clone", "--progress", "--depth", "1", "--no-tags",
        "--single-branch", "--branch", candidate.branch, REPOSITORY_URL, str(checkout),
    ], cwd=stage, deadline=deadline, progress=progress, cancelled=cancelled, started=started)
    return checkout, _verified_checkout(checkout, candidate, stage)


def _download_source_archive(candidate: SourceCandidate, archive: Path, *, report: Callable[[str], None],
                             opener: Callable[..., Any] = urlopen,
                             progress: Callable[[TransferProgress], None] | None = None,
                             cancelled: Callable[[], bool] | None = None) -> None:
    report("Git is unavailable; downloading the same verified GitHub source revision…")
    operation = "GitHub source download"
    deadline = _network_deadline(ARCHIVE_TIMEOUT_SECONDS, operation)
    started = time.monotonic()
    last_sent = 0.0

    def send(total: int | None, force: bool = False) -> None:
        nonlocal last_sent
        now = time.monotonic()
        if progress is None or not (force or now - last_sent >= PROGRESS_INTERVAL_SECONDS):
            return
        last_sent = now
        megabytes = received / (1024 * 1024)
        rate = megabytes / max(0.001, now - started)
        # codeload streams without a length, so the amount received is what moves.
        percent = min(100.0, received * 100.0 / total) if total else None
        try:
            progress(TransferProgress("Downloading the source archive", percent,
                                      f"{megabytes:.2f} MiB | {rate:.2f} MiB/s", now - started))
        except Exception:
            pass
    request = Request(
        SOURCE_ARCHIVE_URL.format(commit=candidate.commit),
        headers={"Accept": "application/zip", "User-Agent": "MCU-Flasher-Bootstrap/11"},
    )
    received = 0
    try:
        with opener(request, timeout=_remaining_network_timeout(deadline, operation, ARCHIVE_IO_SLICE_SECONDS)) as response:
            _ensure_network_time(deadline, operation)
            status = int(getattr(response, "status", None) or response.getcode())
            if status != 200 or not _archive_response_is_allowed(response.geturl()):
                raise ApplicationUpdateError("GitHub source download was redirected to an untrusted location.")
            length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
            if length and (not str(length).isdigit() or int(length) > MAX_ARCHIVE_BYTES):
                raise ApplicationUpdateError("The GitHub source archive is too large.")
            total = int(length) if length else None
            send(total, force=True)
            with archive.open("xb") as stream:
                while True:
                    if cancelled is not None and cancelled():
                        raise ApplicationUpdateError("The GitHub source download was cancelled.")
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
                    send(total)
            send(total, force=True)
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
                # GitHub lists the wrapper folder itself first; only a file there is invalid.
                if len(full.parts) < 2 and not info.is_dir():
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
                        opener: Callable[..., Any] = urlopen,
                        cancelled: Callable[[], bool] | None = None,
                        progress: Callable[[TransferProgress], None] | None = None) -> InstalledUpdate:
    """Stage a clone (or immutable archive fallback), verify it and refresh the app."""
    reason = update_skip_reason(root)
    if reason:
        raise ApplicationUpdateError(f"Automatic application update skipped: {reason}.")
    if cancelled is not None and cancelled():
        raise ApplicationUpdateError("Application update skipped before staging.")
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

    cache = state_file.parent / SOURCE_CACHE_DIRECTORY
    verified_checkout: Path | None = None
    try:
        try:
            source_root, names = _prepare_git_source(stage, candidate, report, progress, cancelled, cache)
            strategy = "git-clone"
            if source_root.name == "repository":
                verified_checkout = source_root
        except _GitUnavailable:
            report("Clone unavailable; using GitHub's immutable source archive instead…")
            archive = stage / "source.zip"
            _download_source_archive(candidate, archive, report=report, opener=opener,
                                     progress=progress, cancelled=cancelled)
            source_root, names = _extract_source_archive(archive, stage)
            strategy = "github-source-archive"
        return install_staged_source(install_root, source_root, names, candidate, strategy=strategy,
                                     report=report, state_base=state_base, cancelled=cancelled)
    finally:
        # A checkout verified at the exact commit is kept even if installing failed
        # (a locked file, say), so the retry needs no second download. Every use
        # writes its files again from Git's objects.
        if verified_checkout is not None:
            _keep_source_cache(verified_checkout, cache)
        _remove_tree(stage)
