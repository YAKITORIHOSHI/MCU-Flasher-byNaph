"""
Arduino Library & Board Browser
================================
Catalog, download and package logic for the PySide6 browser in
main/qt/package_browser.py. It downloads and browses the official Arduino library
index and board package index, so the user can search, inspect, and download any
version of any library or board platform. This module has no window toolkit, so
background code can import it cheaply.

Features
--------
* Tabbed interface: Libraries tab + Boards tab + Installed tab.
* Correct flat-list JSON parsing for both indexes.
* Local file cache so indexes are only fetched once per 24 hours.
* Cache-first loading, bounded disk/network workers and queued view updates.
* Split-pane layout: list on the left, detail panel on the right.
* Version dropdown to pick any release.
* Progress bar for index loading and package archive downloads.
* Clickable website / repository links (opens in default browser).
* Installed tab shows all downloaded items with **available update** indicators.
* User-configurable additional board manager indexes, including multiple
  comma-separated vendor URLs.
* The view shares the Glass, Frosted Light and Solarized Dark palettes with the
  main workspace.
"""

from __future__ import annotations
import json
import hashlib
import os
import re
import sys
import subprocess
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Optional, Any
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

# Self-bootstrap directory resolution
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.environ.get("MCU_PREF_DIR", os.path.dirname(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure project root is in sys.path
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# The Arduino Library & Board Browser is the package downloader; remove offline runtime limits
for _barrier_var in ("MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_WORKSPACE_RUNTIME", "PIP_NO_INDEX"):
    os.environ.pop(_barrier_var, None)

try:
    import requests
except ImportError:
    requests = None

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}




# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIBRARY_INDEX_URL = "https://downloads.arduino.cc/libraries/library_index.json"
BOARD_INDEX_URL = "https://downloads.arduino.cc/packages/package_index.json"

# Index cache lives next to the script so it's portable
INDEX_CACHE_DIR = os.path.join(SCRIPT_DIR, "index_json")
LIBRARY_CACHE_FILE = os.path.join(INDEX_CACHE_DIR, "library_index.json")
BOARD_CACHE_FILE = os.path.join(INDEX_CACHE_DIR, "package_index.json")
CACHE_MAX_AGE_SECONDS = 24 * 60 * 60  # 24 hours


def _url_parts(value) -> list[str]:
    """Return comma-separated board-manager URL candidates from *value*.

    Settings written by older versions may use either a string or a list, so
    both forms are accepted. Empty entries are ignored, which also makes a
    trailing comma harmless.
    """
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,\r\n]+", value) if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _normalize_board_manager_url(value: str) -> str | None:
    """Return a canonical fetch URL for an HTTP(S) board index.

    Vendor lists contain a mixture of ordinary URLs, GitHub ``blob`` links,
    redirects and URLs copied with surrounding quotes. Canonicalizing these
    before validation, caching and fetching keeps one source from producing
    duplicate cache entries and makes GitHub links behave like raw files.
    """
    raw = str(value or "").strip().strip("`'\"").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
    except ValueError:
        return None

    hostname = hostname.casefold()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or
        (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"

    path = parsed.path or "/"
    # GitHub's normal web-view URL returns HTML. Convert the two repository
    # file forms that appear in the community list to raw.githubusercontent.com
    # before attempting JSON parsing.
    github_parts = [part for part in path.split("/") if part]
    if (
        (parsed.hostname or "").casefold() == "github.com"
        and len(github_parts) >= 5
        and github_parts[2].casefold() in {"blob", "raw"}
    ):
        owner, repo, mode, ref = github_parts[:4]
        file_path = "/".join(github_parts[4:])
        if owner and repo and ref and file_path:
            hostname = "raw.githubusercontent.com"
            path = f"/{owner}/{repo}/{ref}/{file_path}"

    return urlunsplit((scheme, hostname, path, parsed.query, ""))


def _is_valid_board_manager_url(value: str) -> bool:
    """Return whether *value* is an absolute HTTP(S) board index URL."""
    return _normalize_board_manager_url(value) is not None


def parse_additional_board_urls(value) -> list[str]:
    """Normalize and de-duplicate user-supplied board index URLs.

    The default Arduino index is deliberately excluded because it is always
    loaded separately. Invalid entries are omitted here and can be reported
    by the settings UI with :func:`invalid_additional_board_urls`.
    """
    result: list[str] = []
    seen: set[str] = set()
    default_norm = _normalize_board_manager_url(BOARD_INDEX_URL)
    default_key = default_norm.casefold() if default_norm else ""
    for candidate in _url_parts(value):
        normalized = _normalize_board_manager_url(candidate)
        if not normalized:
            continue
        key = normalized.casefold()
        if key == default_key or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def invalid_additional_board_urls(value) -> list[str]:
    """Return non-empty URL entries that are not valid HTTP(S) URLs."""
    invalid: list[str] = []
    default_norm = _normalize_board_manager_url(BOARD_INDEX_URL)
    default_key = default_norm.casefold() if default_norm else ""
    for candidate in _url_parts(value):
        if _normalize_board_manager_url(candidate):
            continue
        if candidate.casefold() == default_key:
            continue
        invalid.append(candidate)
    return invalid


def _board_index_cache_file(url: str) -> str:
    """Return a stable cache filename unique to an additional index URL."""
    normalized = _normalize_board_manager_url(url) or str(url).strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return os.path.join(INDEX_CACHE_DIR, f"package_index_{digest}.json")


def _validate_index_payload(data, expected_key: str) -> dict:
    """Validate the small common contract used by Arduino index files.

    The official format is intentionally extensible, so this does not reject
    unknown fields. It only prevents HTML, arrays and unrelated JSON files
    from being cached as apparently successful indexes.
    """
    if not isinstance(data, dict):
        raise ValueError("index root must be a JSON object")
    if not isinstance(data.get(expected_key), list):
        raise ValueError(f"index must contain a '{expected_key}' array")
    if expected_key == "packages":
        for package in data[expected_key]:
            if not isinstance(package, dict) or not isinstance(package.get("name"), str):
                raise ValueError("each board package must declare a name")
            platforms = package.get("platforms", [])
            if not isinstance(platforms, list):
                raise ValueError("board package platforms must be an array")
            for platform in platforms:
                if not isinstance(platform, dict) or not isinstance(platform.get("name"), str):
                    raise ValueError("each board platform must declare a name")
                if not isinstance(platform.get("boards", []), list):
                    raise ValueError("board platform boards must be an array")
                for key in ("toolsDependencies", "discoveryDependencies", "monitorDependencies", "libraryDependencies"):
                    if not isinstance(platform.get(key, []), list):
                        raise ValueError(f"board platform {key} must be an array")
    return data


# Keep only small raw indexes. Large catalogs retain grouped metadata instead.
_INDEX_JSON_RAM_CACHE = OrderedDict()
_INDEX_JSON_RAM_LOCK = threading.Lock()
_INDEX_VALID_SIGNATURES = OrderedDict()


def _remember_valid_index(cache_file, expected_key, st):
    """Called under the raw-cache lock; retain signatures, never large JSON."""
    key = cache_file, expected_key
    _INDEX_VALID_SIGNATURES[key] = st.st_mtime_ns, st.st_size
    _INDEX_VALID_SIGNATURES.move_to_end(key)
    while len(_INDEX_VALID_SIGNATURES) > 64:
        _INDEX_VALID_SIGNATURES.popitem(last=False)


def _has_valid_index_cache(cache_file, expected_key):
    """Avoid parsing a source again merely to decide whether to refresh it."""
    try:
        st = os.stat(cache_file)
    except OSError:
        return False
    key = cache_file, expected_key
    with _INDEX_JSON_RAM_LOCK:
        if _INDEX_VALID_SIGNATURES.get(key) == (st.st_mtime_ns, st.st_size):
            _INDEX_VALID_SIGNATURES.move_to_end(key)
            return True
        _INDEX_VALID_SIGNATURES.pop(key, None)
    return _read_index_cache(cache_file, expected_key) is not None


def _read_index_cache(cache_file: str, expected_key: str) -> dict | None:
    try:
        st = os.stat(cache_file)
        with _INDEX_JSON_RAM_LOCK:
            cached = _INDEX_JSON_RAM_CACHE.get(cache_file)
            if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                _INDEX_JSON_RAM_CACHE.move_to_end(cache_file)
                data = _validate_index_payload(cached[2], expected_key)
                _remember_valid_index(cache_file, expected_key, st)
                return data
            _INDEX_JSON_RAM_CACHE.pop(cache_file, None)

        with open(cache_file, "r", encoding="utf-8-sig") as fh:
            data = _validate_index_payload(json.load(fh), expected_key)
            current = os.stat(cache_file)
            with _INDEX_JSON_RAM_LOCK:
                stable = (st.st_mtime_ns, st.st_size) == (current.st_mtime_ns, current.st_size)
                if stable:
                    _remember_valid_index(cache_file, expected_key, st)
                if stable and st.st_size <= 1_000_000:
                    _INDEX_JSON_RAM_CACHE[cache_file] = (st.st_mtime_ns, st.st_size, data)
                    while (len(_INDEX_JSON_RAM_CACHE) > 4 or
                           sum(entry[1] for entry in _INDEX_JSON_RAM_CACHE.values()) > 2_000_000):
                        _INDEX_JSON_RAM_CACHE.popitem(last=False)
            return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_index_cache(cache_file: str, raw_text: str) -> None:
    """Atomically replace an index cache so interrupted writes cannot poison it."""
    temp_file = f"{cache_file}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(temp_file, "w", encoding="utf-8") as fh:
            fh.write(raw_text)
        os.replace(temp_file, cache_file)
        try:
            st = os.stat(cache_file)
            # Invalidate stale RAM cache entry so next read loads new text
            with _INDEX_JSON_RAM_LOCK:
                _INDEX_JSON_RAM_CACHE.pop(cache_file, None)
                for key in tuple(_INDEX_VALID_SIGNATURES):
                    if key[0] == cache_file:
                        _INDEX_VALID_SIGNATURES.pop(key, None)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(temp_file):
                os.unlink(temp_file)
        except OSError:
            pass

# Settings cache (remembers download folder and board indexes across sessions)
_index_settings = os.path.join(SCRIPT_DIR, "index_json", "arduino_browser_settings.json")
_dbs_settings = os.path.join(SCRIPT_DIR, "src", "dbs", "arduino_browser_settings.json")
_root_settings = os.path.join(SCRIPT_DIR, "arduino_browser_settings.json")
if os.path.exists(_index_settings) or os.path.isdir(os.path.join(SCRIPT_DIR, "index_json")):
    SETTINGS_FILE = _index_settings
elif os.path.exists(_dbs_settings):
    SETTINGS_FILE = _dbs_settings
else:
    SETTINGS_FILE = _root_settings

# Default download location
DEFAULT_DOWNLOAD_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "_MCUFlasherByNaph_src"
)


def _detect_system_theme() -> str:
    if sys.platform != "win32":
        from main.core.config import _detect_system_theme as detect
        return detect()
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "light" if val == 1 else "default"
    except Exception:
        return "default"


def _resolve_lib_req_theme() -> str:
    # Use the same per-user precedence, legacy migration and OS following as
    # the workspace. Portable standalone copies retain the fallback below.
    try:
        from main.core.config import get_theme_mode
        return get_theme_mode()
    except ImportError:
        pass
    for cfg in (
        os.path.join(os.path.expanduser("~"), ".mcu_gui_config.json"),
        os.path.join(SCRIPT_DIR, "src", "gui_config.json")
    ):
        try:
            if os.path.exists(cfg):
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
                shared = data.get("shared", data)
                if shared.get("theme_follow_system", False):
                    return _detect_system_theme()
                m = str(shared.get("theme_mode", "default")).strip().lower()
                if m in ("default", "light") or m in ("solarized", "solarize", "solarized-dark", "solarized_dark", "solarize_dark"):
                    return m
        except Exception:
            pass
    return "default"







def _find_code_viewer_python(cancel=None) -> Optional[str]:
    """Find a Python executable that has PyQt5 and QScintilla available."""
    if sys.platform == "win32":
        prepared = [Path(SCRIPT_DIR) / "env" / "Scripts" / name
                    for name in ("pythonw.exe", "python.exe")]
        prepared += [Path(SCRIPT_DIR) / "src" / "_python" / name
                     for name in ("pythonw.exe", "python.exe")]
    else:
        prepared = [Path(SCRIPT_DIR) / directory / "bin" / "python"
                    for directory in (".venv-macos" if sys.platform == "darwin" else ".venv-linux", "env")]
    candidates = [Path(sys.executable), *prepared]
    seen = set()
    for cand in candidates:
        if cancel is not None and cancel.is_set():
            return None
        # A venv's python is normally a symlink on Ubuntu. Keep that spelling
        # so Python finds its pyvenv.cfg and the prepared environment packages.
        cand_str = os.path.abspath(cand) if cand.is_file() else ""
        identity = os.path.normcase(cand_str)
        if not cand_str or identity in seen:
            continue
        seen.add(identity)
        try:
            res = subprocess.run(
                [cand_str, "-c", "import PyQt5.QtWidgets, PyQt5.Qsci"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                timeout=5,
            )
            if res.returncode == 0:
                return cand_str
        except Exception:
            continue
    return None








def _load_settings() -> dict:
    """Load settings and repair copied-machine download paths in place."""
    settings = {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            settings = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        settings = {}

    saved_dir = str(settings.get("download_dir", "") or "")
    saved_urls = settings.get("additional_board_urls", [])
    normalized_urls = parse_additional_board_urls(saved_urls)
    settings_changed = saved_urls != normalized_urls
    if settings_changed:
        settings["additional_board_urls"] = normalized_urls
    if not saved_dir or not os.path.isdir(os.path.expandvars(os.path.expanduser(saved_dir))):
        settings["download_dir"] = DEFAULT_DOWNLOAD_DIR
        settings_changed = True
    if settings_changed:
        _save_settings(settings)
    return settings


def _save_settings(settings: dict):
    """Persist atomically and report failure without losing the saved settings."""
    temporary = f"{SETTINGS_FILE}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(SETTINGS_FILE)), exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(temporary, SETTINGS_FILE)
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?"
    r"(?:-([\w.]+))?"
    r"(?:\+([\w.]+))?$"
)


@lru_cache(maxsize=512)
def _version_key(version_str: str):
    """Return a sort key that orders semantic versions correctly."""
    m = _VERSION_RE.match(version_str.strip())
    if not m:
        return (0, 0, 0, 0, ((1, version_str),))

    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    patch = int(m.group(3)) if m.group(3) else 0
    pre = m.group(4)

    is_release = 0 if pre else 1

    pre_key: list = []
    if pre:
        for part in pre.split("."):
            if part.isdigit():
                pre_key.append((0, int(part)))
            else:
                pre_key.append((1, part))

    return (major, minor, patch, is_release, tuple(pre_key))


def _archive_filename(url: str, archive_file_name: str = "") -> str:
    """Choose a safe local filename from package metadata or the download URL."""
    candidate = str(archive_file_name or "").strip()
    if not candidate:
        try:
            candidate = unquote(urlsplit(str(url)).path).rsplit("/", 1)[-1]
        except ValueError:
            candidate = ""
    candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    candidate = re.sub(r'[<>:"/\\|?*]', "_", candidate).strip(" .")
    if not candidate or candidate in {".", ".."}:
        digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:12]
        candidate = f"arduino-package-{digest}.archive"
    return candidate


def _get_folder_name(archive_name: str) -> str:
    """Strip common Arduino package archive extensions to get folder name."""
    for ext in [
        ".tar.bz2", ".tar.gz", ".tar.xz", ".tar.zst", ".tar", ".zip",
        ".tgz", ".tbz2", ".txz", ".tzst",
    ]:
        if str(archive_name).lower().endswith(ext):
            return str(archive_name)[:-len(ext)]
    return os.path.splitext(str(archive_name))[0]


def _parse_checksum(value):
    """Return ``(hashlib algorithm, expected hex digest)`` for Arduino metadata."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    if ":" in raw:
        algorithm, digest = raw.split(":", 1)
    elif "=" in raw:
        algorithm, digest = raw.split("=", 1)
    else:
        digest = raw
        algorithm = {
            32: "md5",
            40: "sha1",
            64: "sha256",
            96: "sha384",
            128: "sha512",
        }.get(len(digest), "")

    algorithm = re.sub(r"[^a-z0-9]", "", algorithm.casefold())
    algorithm = {"sha": "sha1", "sha256": "sha256", "sha384": "sha384", "sha512": "sha512"}.get(algorithm, algorithm)
    digest = digest.strip().casefold()
    if algorithm not in {"md5", "sha1", "sha256", "sha384", "sha512"}:
        raise ValueError(f"unsupported checksum algorithm '{algorithm or 'unknown'}'")
    if not re.fullmatch(r"[0-9a-f]+", digest) or len(digest) != hashlib.new(algorithm).digest_size * 2:
        raise ValueError("invalid checksum value")
    return algorithm, digest


def _verify_download(filepath: str, expected_size=0, checksum="") -> None:
    """Verify the package metadata before an archive is extracted."""
    try:
        declared_size = int(expected_size or 0)
    except (TypeError, ValueError):
        declared_size = 0
    actual_size = os.path.getsize(filepath)
    if declared_size > 0 and actual_size != declared_size:
        raise ValueError(
            f"download size mismatch (expected {declared_size:,} bytes, got {actual_size:,})"
        )

    parsed_checksum = _parse_checksum(checksum)
    if not parsed_checksum:
        return
    algorithm, expected_digest = parsed_checksum
    digest = hashlib.new(algorithm)
    with open(filepath, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_digest = digest.hexdigest().casefold()
    if actual_digest != expected_digest:
        raise ValueError(
            f"{algorithm.upper()} checksum mismatch (expected {expected_digest}, got {actual_digest})"
        )


_PACKAGE_CONNECT_TIMEOUT = 5
_PACKAGE_READ_TIMEOUT = 5


class _DownloadInterrupted(OSError):
    """A stopped network transfer whose owned checkpoint can be retried."""


def _download_package_archive(url, partial_path, metadata, cancel, progress):
    """Stream one explicit attempt; retain source-bound bytes after interruption.

    Only append when the saved source/checksum and the HTTP range agree. A
    changed or Range-ignoring response starts a fresh checkpoint, never a mix
    of two archives. Final size/checksum verification remains with the caller.
    """
    import http.client
    import urllib.error
    import urllib.request

    def check_cancelled():
        if cancel.is_set():
            raise InterruptedError("Package download cancelled; saved partial can be resumed")

    def refused(status):
        if status in (408, 425, 429, 500, 502, 503, 504):
            raise _DownloadInterrupted(
                f"Download interrupted by the server (HTTP {status}). "
                "Saved partial retained; select Download again to resume.")
        raise ValueError(f"Archive download was refused (HTTP {status})")

    check_cancelled()
    try:
        expected_size = max(0, int(metadata.get("size", 0) or 0))
    except (TypeError, ValueError):
        expected_size = 0
    checksum = _parse_checksum(metadata.get("checksum", ""))
    identity = {"url": str(url), "size": expected_size, "checksum": list(checksum) if checksum else None}
    checkpoint_path = partial_path + ".json"
    for path in (partial_path, checkpoint_path):
        if os.path.islink(path) or (os.path.lexists(path) and not os.path.isfile(path)):
            raise ValueError("Download checkpoint is not an ordinary file")

    checkpoint = {}
    try:
        if os.path.getsize(checkpoint_path) <= 8192:
            with open(checkpoint_path, "r", encoding="utf-8") as source:
                value = json.load(source)
            if isinstance(value, dict) and value.get("schema") == 1 and value.get("source") == identity:
                checkpoint = value
    except (OSError, ValueError, TypeError):
        pass
    resume_from = os.path.getsize(partial_path) if os.path.isfile(partial_path) else 0
    etag = str(checkpoint.get("etag") or "")
    modified = str(checkpoint.get("modified") or "")
    validator = etag if etag and not etag.startswith("W/") else modified
    if not checkpoint or not (validator or checksum) or (expected_size and resume_from > expected_size):
        resume_from = 0
    # A completed, immutable checksum-bound checkpoint needs no new request.
    if resume_from and checksum and expected_size and resume_from == expected_size:
        try:
            _verify_download(partial_path, expected_size, metadata.get("checksum", ""))
        except ValueError:
            resume_from = 0
        else:
            check_cancelled()
            progress(resume_from, expected_size)
            return

    headers = dict(DEFAULT_HEADERS, **{"Accept-Encoding": "identity"})
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
        if validator:
            headers["If-Range"] = validator
    response = None
    network_errors = (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, ConnectionError)
    if requests is not None:
        from urllib3.exceptions import HTTPError as StreamError
        network_errors += (requests.exceptions.RequestException, StreamError)
    try:
        check_cancelled()
        if requests is not None:
            response = requests.get(url, stream=True,
                timeout=(_PACKAGE_CONNECT_TIMEOUT, _PACKAGE_READ_TIMEOUT),
                headers=headers, allow_redirects=True)
            status = response.status_code
            read_available = getattr(getattr(response, "raw", None), "read1", None)
            # read1 returns available bytes without waiting to fill a 16 KiB
            # chunk. Even a trickling connection must let Cancel be checked.
            chunks = (iter(lambda: read_available(16384, decode_content=False), b"")
                      if callable(read_available) else response.iter_content(chunk_size=16384))
        else:
            response = urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                              timeout=_PACKAGE_READ_TIMEOUT)
            status = response.getcode()
            read_available = getattr(response, "read1", response.read)
            chunks = iter(lambda: read_available(16384), b"")
        check_cancelled()
        if status not in (200, 206):
            refused(status)
        response_headers = {str(key).casefold(): str(value) for key, value in response.headers.items()}
        if response_headers.get("content-encoding", "identity").casefold() not in ("", "identity"):
            raise ValueError("Archive server returned an encoded response instead of resumable bytes")
        response_etag = response_headers.get("etag", "")
        response_modified = response_headers.get("last-modified", "")
        total = expected_size
        content_length = int(response_headers.get("content-length", "0") or 0)
        if status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response_headers.get("content-range", ""))
            if not resume_from or not match or int(match[1]) != resume_from:
                raise ValueError("Archive server returned an invalid resume range; retry Download")
            start, end, remote_total = map(int, match.groups())
            if end < start or end >= remote_total or (content_length and content_length != end - start + 1):
                raise ValueError("Archive server returned an inconsistent resume range; retry Download")
            if expected_size and remote_total != expected_size:
                raise ValueError("Archive size changed; refresh its index before retrying Download")
            returned_validator = response_etag if etag and not etag.startswith("W/") else response_modified
            if validator and returned_validator != validator:
                raise ValueError("Archive changed during resume; retry Download from the beginning")
            total = total or remote_total
        else:
            resume_from = 0  # If-Range mismatch or a server that ignores Range.
            if expected_size and content_length and content_length != expected_size:
                raise ValueError("Archive response size differs from its index; refresh before retrying Download")
            total = total or content_length
        check_cancelled()
        # Bind ownership before new bytes. Truncate a replaced source first so
        # shutdown between these steps cannot bind its old bytes to a new URL.
        record = {"schema": 1, "source": identity, "etag": response_etag,
                  "modified": response_modified, "total": total}
        serialized = json.dumps(record)
        if len(serialized.encode("utf-8")) > 8192:
            raise ValueError("Archive checkpoint identity exceeds the supported limit")
        downloaded = resume_from
        progress(downloaded, total)
        with open(partial_path, "ab" if resume_from else "wb") as output:
            temporary = checkpoint_path + f".tmp-{os.getpid()}-{threading.get_ident()}"
            try:
                with open(temporary, "w", encoding="utf-8") as destination:
                    destination.write(serialized)
                os.replace(temporary, checkpoint_path)
            finally:
                if os.path.isfile(temporary):
                    os.remove(temporary)
            for chunk in chunks:
                check_cancelled()
                if chunk:
                    output.write(chunk)
                    downloaded += len(chunk)
                    if total and downloaded > total:
                        raise ValueError("Archive server sent more bytes than the declared size")
                    progress(downloaded, total)
        check_cancelled()
        if total and downloaded != total:
            raise _DownloadInterrupted(
                f"Connection interrupted ({downloaded:,} of {total:,} bytes). "
                "Saved partial retained; select Download again to resume.")
        if not downloaded:
            raise ValueError("Archive download produced an empty file")
    except urllib.error.HTTPError as error:
        error.close()
        check_cancelled()
        refused(error.code)
    except network_errors as error:
        check_cancelled()
        raise _DownloadInterrupted(
            "Connection interrupted while downloading. Saved partial retained; "
            "select Download again to resume after the connection returns.") from error
    finally:
        if response is not None:
            try:
                response.close()
            except (OSError, http.client.HTTPException) + network_errors:
                # Closing a broken socket must not replace the interruption
                # result and send its resumable checkpoint into error cleanup.
                pass


def _validate_archive_file(filepath: str) -> None:
    """Reject successful HTTP responses that are actually HTML/error pages."""
    import shutil
    import tarfile
    import zipfile

    if zipfile.is_zipfile(filepath):
        return
    try:
        with tarfile.open(filepath, "r:*"):
            return
    except (OSError, tarfile.TarError):
        pass

    if str(filepath).lower().endswith((".tar.zst", ".tzst")) or _is_zstd_archive(filepath):
        tar_executable = shutil.which("tar")
        if tar_executable:
            try:
                subprocess.run(
                    [tar_executable, "-tf", filepath],
                    check=True, capture_output=True,
                    creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
                )
                return
            except (OSError, subprocess.SubprocessError):
                pass
    raise ValueError("downloaded file is not a readable ZIP or tar archive")


def _is_zstd_archive(filepath: str) -> bool:
    """Detect a Zstandard stream when a vendor omits the archive extension."""
    try:
        with open(filepath, "rb") as fh:
            return fh.read(4) == b"\x28\xb5\x2f\xfd"
    except OSError:
        return False


def heal_library_path_on_current_device(raw_slug: str) -> str:
    """Check if a symlink:// path or directory path exists on the current machine.
    If it points to a non-existent foreign directory (e.g. from another user account or device),
    re-navigate to the current device's download directory (Libs/<folder_name>) or local Arduino libraries.
    """
    if not raw_slug:
        return raw_slug

    is_symlink = raw_slug.startswith("symlink://")
    path_str = raw_slug[len("symlink://"):] if is_symlink else raw_slug
    norm_path = path_str.replace("\\", "/").strip()

    p_obj = os.path.normpath(norm_path)
    if os.path.exists(p_obj):
        return raw_slug  # Valid on this device

    # Path does NOT exist on this device. Extract library directory name
    folder_name = os.path.basename(norm_path.rstrip("/"))
    if not folder_name:
        return raw_slug

    settings = _load_settings()
    download_dir = settings.get("download_dir", DEFAULT_DOWNLOAD_DIR)
    libs_dir = os.path.join(download_dir, "Libs")

    # Candidates to search for the library on current machine
    base_name = re.sub(r'-\d+\.\d+.*$', '', folder_name)
    search_dirs = [
        os.path.join(libs_dir, folder_name),
        os.path.join(libs_dir, base_name),
        os.path.join(os.path.expanduser("~"), "Documents", "Arduino", "libraries", folder_name),
        os.path.join(os.path.expanduser("~"), "Documents", "Arduino", "libraries", base_name),
    ]

    # Search in Libs subdirectories
    if os.path.isdir(libs_dir):
        try:
            for item in os.listdir(libs_dir):
                full_item = os.path.join(libs_dir, item)
                if os.path.isdir(full_item):
                    if item.lower() == folder_name.lower() or item.lower() == base_name.lower():
                        search_dirs.append(full_item)
        except Exception:
            pass

    for candidate in search_dirs:
        if os.path.isdir(candidate):
            healed = os.path.normpath(candidate).replace("\\", "/")
            return f"symlink://{healed}" if is_symlink else healed

    # Fallback to library name if local directory isn't available
    clean_name = base_name if base_name else folder_name
    return clean_name


def _safe_archive_target(extract_dir: str, member_name: str) -> str:
    """Return a safe extraction path or raise for traversal/absolute members."""
    root = os.path.abspath(extract_dir)
    normalized_name = str(member_name).replace("\\", "/")
    if normalized_name.startswith("/") or re.match(r"^[A-Za-z]:", normalized_name):
        raise ValueError(f"archive contains an absolute path: {member_name!r}")
    target = os.path.abspath(os.path.join(root, *[p for p in normalized_name.split("/") if p]))
    if os.path.commonpath((root, target)) != root:
        raise ValueError(f"archive contains a path outside its destination: {member_name!r}")
    return target


def _extract_archive(filepath: str, extract_dir: str, cancel=None):
    """Extract zip/tar package archives safely, including modern tar.zst names."""
    import zipfile
    import tarfile
    import shutil
    import stat
    from pathlib import Path

    def check_cancelled():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Package extraction cancelled")

    def copy(source, dest):
        while True:
            check_cancelled()
            block = source.read(256 * 1024)
            if not block:
                break
            dest.write(block)

    check_cancelled()
    os.makedirs(extract_dir, exist_ok=True)
    lower_path = filepath.lower()
    if lower_path.endswith('.zip') or zipfile.is_zipfile(filepath):
        with zipfile.ZipFile(filepath, 'r') as zip_ref:
            for member in zip_ref.infolist():
                check_cancelled()
                target = _safe_archive_target(extract_dir, member.filename)
                mode = (member.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    continue
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zip_ref.open(member, "r") as source, open(target, "wb") as dest:
                    copy(source, dest)
    else:
        try:
            tar_ref = tarfile.open(filepath, 'r:*')
        except (OSError, tarfile.TarError) as exc:
            tar_ref = None
            if not lower_path.endswith(('.tar.zst', '.tzst')) and not _is_zstd_archive(filepath):
                raise RuntimeError(f"Cannot extract tar archive: {exc}") from exc
            tar_executable = shutil.which("tar")
            if not tar_executable:
                raise RuntimeError(
                    "This tar.zst archive needs a Python zstandard runtime or the system tar utility."
                ) from exc
            try:
                listing = subprocess.run(
                    [tar_executable, "-tf", filepath],
                    check=True, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                for member_name in listing.stdout.splitlines():
                    check_cancelled()
                    _safe_archive_target(extract_dir, member_name.strip())
                child = subprocess.Popen([tar_executable, "-xf", filepath, "-C", extract_dir],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
                try:
                    while True:
                        check_cancelled()
                        try:
                            _output, errors = child.communicate(timeout=.1)
                            if child.returncode:
                                raise RuntimeError(errors.decode("utf-8", errors="replace")[:1000])
                            break
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.communicate()
                check_cancelled()
            except InterruptedError:
                raise
            except (OSError, subprocess.SubprocessError) as tar_exc:
                raise RuntimeError(f"Cannot extract tar.zst archive: {tar_exc}") from tar_exc

        if tar_ref is not None:
            try:
                for member in tar_ref:
                    check_cancelled()
                    target = _safe_archive_target(extract_dir, member.name)
                    # Symlinks and hardlinks can escape the destination on Windows;
                    # package contents remain usable without them.
                    if member.issym() or member.islnk():
                        continue
                    if member.isdir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    if not member.isfile():
                        continue
                    source = tar_ref.extractfile(member)
                    if source is None:
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with source, open(target, "wb") as dest:
                        copy(source, dest)
            finally:
                tar_ref.close()
    check_cancelled()
    # Self-heal / flatten double nesting if present
    try:
        p_dir = Path(extract_dir)
        subdirs = [p for p in p_dir.iterdir() if p.is_dir()]
        files = [p for p in p_dir.iterdir() if p.is_file()]
        if len(subdirs) == 1 and len(files) == 0:
            nested = subdirs[0]
            # Move all contents of nested up to p_dir
            for item in nested.iterdir():
                check_cancelled()
                shutil.move(str(item), str(p_dir))
            nested.rmdir()
    except InterruptedError:
        raise
    except Exception:
        pass


def _promote_directory(staging, destination):
    """Replace a complete payload while preserving the previous one on failure."""
    import shutil
    previous = f"{destination}.previous-{os.getpid()}-{threading.get_ident()}"
    had_previous = os.path.isdir(destination)
    if had_previous:
        os.replace(destination, previous)
    try:
        os.replace(staging, destination)
    except Exception:
        if had_previous:
            os.replace(previous, destination)
        raise
    if had_previous:
        shutil.rmtree(previous, ignore_errors=True)


def _inside_directory(path, directory):
    try:
        root = os.path.normcase(os.path.abspath(directory))
        target = os.path.normcase(os.path.abspath(path))
        return target != root and os.path.commonpath((root, target)) == root
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# Data model — Libraries
# ---------------------------------------------------------------------------

def _group_libraries(raw_list: list[dict]) -> dict[str, dict]:
    """Group the flat release list by library name."""
    by_name: dict[str, list[dict]] = {}
    for entry in raw_list:
        if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not entry['name'].strip():
            continue
        name = entry.get("name", "")
        by_name.setdefault(name, []).append(entry)

    result: dict[str, dict] = {}
    for name, entries in by_name.items():
        entries.sort(key=lambda e: _version_key(str(e.get("version", "0"))),
                     reverse=True)
        latest = entries[0]
        versions = []
        for e in entries:
            versions.append({
                "version": str(e.get("version", "?")),
                "url": str(e.get("url") or ""),
                "size": e.get("size", 0),
                "checksum": e.get("checksum", ""),
                "archiveFileName": e.get("archiveFileName", ""),
            })
        result[name] = {
            "name": name,
            "author": latest.get("author", ""),
            "maintainer": latest.get("maintainer", ""),
            "sentence": latest.get("sentence", ""),
            "paragraph": latest.get("paragraph", ""),
            "website": latest.get("website", ""),
            "repository": latest.get("repository", ""),
            "category": latest.get("category", ""),
            "architectures": latest.get("architectures", []),
            "types": latest.get("types", []),
            "versions": versions,
        }
    return result


# ---------------------------------------------------------------------------
# Data model — Boards
# ---------------------------------------------------------------------------

def _group_boards(packages: list[dict]) -> dict[str, dict]:
    """Group board platforms while preserving package/vendor identity.

    The JSON has packages → platforms (each platform is a version of a
    board core, e.g. "Arduino AVR Boards 1.8.6").  We group all platform
    versions under a single display name and keep every version available
    for download. The package name is part of the internal identity so two
    vendors using the same human-readable platform name cannot overwrite one
    another.
    """
    platform_groups: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        pkg_name = str(pkg.get("name") or "").strip()
        package_identity = (
            pkg_name
            or str(pkg.get("websiteURL") or "").strip()
            or str(pkg.get("maintainer") or "").strip()
            or "unknown-package"
        ).casefold()
        for plat in pkg.get("platforms", []) or []:
            if not isinstance(plat, dict):
                continue
            pname = str(plat.get("name") or pkg_name or "").strip()
            if pname:
                identity = (package_identity, pname.casefold())
                platform_groups.setdefault(identity, []).append((plat, pkg))

    grouped_records = []
    for (_package_key, pname_key), records in platform_groups.items():
        records.sort(
            key=lambda record: _version_key(str(record[0].get("version", "0"))),
            reverse=True,
        )
        latest, latest_pkg = records[0]
        base_name = str(latest.get("name") or latest_pkg.get("name") or pname_key).strip()
        grouped_records.append((base_name, records, latest, latest_pkg))

    name_counts: dict[str, int] = {}
    for base_name, _records, _latest, _latest_pkg in grouped_records:
        key = base_name.casefold()
        name_counts[key] = name_counts.get(key, 0) + 1

    result: dict[str, dict] = {}
    used_names: set[str] = set()
    for base_name, records, latest, latest_pkg in grouped_records:
        package_name = str(latest_pkg.get("name") or "").strip()
        display_name = base_name
        if name_counts.get(base_name.casefold(), 0) > 1:
            suffix = package_name or str(latest_pkg.get("maintainer") or "vendor").strip()
            display_name = f"{base_name} ({suffix})"
        collision_number = 2
        original_display_name = display_name
        while display_name.casefold() in used_names:
            display_name = f"{original_display_name} #{collision_number}"
            collision_number += 1
        used_names.add(display_name.casefold())

        boards_list: list[str] = []
        board_names = set()
        for platform, _pkg in records:
            for board in platform.get("boards", []) or []:
                if not isinstance(board, dict):
                    continue
                board_name = str(board.get("name", "")).strip()
                if board_name and board_name not in board_names:
                    boards_list.append(board_name)
                    board_names.add(board_name)

        versions = []
        seen_versions: set[tuple[str, str]] = set()
        for pv, _pkg in records:
            version = str(pv.get("version", "?"))
            raw_url = str(pv.get("url") or "").strip()
            url = _normalize_board_manager_url(raw_url) or raw_url
            version_key = (version, url)
            if version_key in seen_versions:
                continue
            seen_versions.add(version_key)
            size_val = pv.get("size", 0)
            try:
                size_val = int(size_val)
            except (ValueError, TypeError):
                size_val = 0
            versions.append({
                "version": version,
                "index_url": str(_pkg.get("_index_url") or ""),
                "package": str(_pkg.get("name") or ""),
                "architecture": str(pv.get("architecture") or ""),
                "boards": [str(board.get("name") or "") for board in pv.get("boards", []) if isinstance(board, dict)],
                "url": url,
                "size": size_val,
                "checksum": pv.get("checksum", ""),
                "archiveFileName": pv.get("archiveFileName", ""),
                "deprecated": bool(pv.get("deprecated", False)),
                "toolsDependencies": pv.get("toolsDependencies", []) or [],
                "discoveryDependencies": pv.get("discoveryDependencies", []) or [],
                "monitorDependencies": pv.get("monitorDependencies", []) or [],
                "libraryDependencies": pv.get("libraryDependencies", []) or [],
            })

        result[display_name] = {
            "name": display_name,
            "platform_name": base_name,
            "package": package_name,
            "maintainer": str(latest_pkg.get("maintainer") or ""),
            "website": str(latest_pkg.get("websiteURL") or ""),
            "email": str(latest_pkg.get("email") or ""),
            "architecture": str(latest.get("architecture") or ""),
            "category": str(latest.get("category") or ""),
            "boards": boards_list,
            "help_url": (
                (latest.get("help", {}) or {}).get("online", "")
                if isinstance(latest.get("help", {}), dict)
                else ""
            ),
            "versions": versions,
        }

    return result


def _read_library_catalog(cache_file=LIBRARY_CACHE_FILE, data=None):
    from src.modules.browser_loading import load_catalog
    def build():
        raw = data if data is not None else _read_index_cache(cache_file, 'libraries')
        return _group_libraries(raw['libraries']) if raw is not None else None
    if data is not None and data.get('_browser_cache_written') is False:
        return build()  # Fresh network data remains usable on a read-only cache.
    return load_catalog(cache_file + '.catalog-v1.json', [cache_file], build)


def _read_board_catalog(paths, fresh=None, source_urls=None):
    from src.modules.browser_loading import load_catalog
    def build():
        packages, loaded = [], False
        for path in paths:
            raw = (fresh or {}).get(path)
            if raw is None:
                raw = _read_index_cache(path, 'packages')
            if raw is not None:
                loaded = True
                # Provenance comes from our requested source, never a field
                # supplied by the remote JSON document.
                packages.extend({**package, '_index_url': (source_urls or {}).get(path, '')}
                                for package in raw['packages'])
        return _group_boards(packages) if loaded else None
    if fresh and any(raw.get('_browser_cache_written') is False for raw in fresh.values()):
        return build()
    return load_catalog(os.path.join(INDEX_CACHE_DIR, 'browser_boards_catalog-v1.json'), paths, build)


# ---------------------------------------------------------------------------
# Reusable browsing tab
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Installed Tab Class
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Application class
# ---------------------------------------------------------------------------


def _open_fallback_editor(file_path: str, report=None, reason: Optional[str] = None) -> bool:
    """Open a sketch with the desktop's editor when the code viewer is unavailable."""
    if not file_path or not os.path.exists(file_path):
        return False
    try:
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", file_path])
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", file_path])
        return True
    except Exception:
        pass
    if report and reason:
        report("Code Viewer", f"{reason}\n\nFile: {file_path}")
    return False


def _launch_code_viewer(file_path, all_paths=None, report=None, *, python_exe=None,
                        theme_mode=None, font_size=None, include_roots=()):
    viewer_script = os.path.join(SCRIPT_DIR, "src", "qscintilla_viewer.py")
    if not os.path.exists(viewer_script):
        _open_fallback_editor(file_path, report, f"Viewer script not found:\n{viewer_script}")
        return

    py_exe = python_exe or _find_code_viewer_python() or sys.executable
    try:
        cmd = [str(py_exe), viewer_script, file_path]
        if all_paths:
            cmd.extend(all_paths)
        environment = os.environ.copy()
        if include_roots:
            environment["MCU_FLASHER_VIEWER_INCLUDE_ROOTS"] = json.dumps(list(include_roots))
        else:
            environment.pop("MCU_FLASHER_VIEWER_INCLUDE_ROOTS", None)
        if theme_mode is not None:
            environment["MCU_FLASHER_VIEWER_THEME"] = str(theme_mode)
        if font_size is not None:
            environment["MCU_FLASHER_VIEWER_FONT_SIZE"] = str(font_size)
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(cmd, creationflags=creationflags, env=environment)
        if sys.platform != "win32":
            from src.modules.ubuntu_download_manager import remember_viewer
            remember_viewer(process)
    except Exception as e:
        _open_fallback_editor(file_path, report, f"Failed to launch QScintilla viewer:\n{e}")


class BrowserEngine:
    """Catalog, inventory and download work, independent of the window toolkit.

    Workers report through ``_post_ui``; only the owning view touches widgets.
    """

    @staticmethod
    def _board_association_key(metadata):
        return json.dumps([metadata.get("index_url", ""), metadata.get("package", ""),
                           metadata.get("architecture", "")], ensure_ascii=False, separators=(',', ':'))

    def _package_metadata(self, item, version, *, is_board):
        metadata = {**item, **version}
        # Index-authored platform associations cannot override the user's
        # choice. Exact aliases are verified after preparation by the worker.
        metadata.pop("platformio", None)
        if is_board:
            from src.modules.board_index_targets import normalize_platformio_configuration
            value = getattr(self, '_board_platformio_associations', {}).get(self._board_association_key(metadata))
            configuration = normalize_platformio_configuration(value)
            if configuration:
                metadata['platformio'] = configuration
        return metadata

    def _get_installed_info(self, tab: BrowseTab, name: str, item: dict, *, download_dir=None, installed_items=None, cancel=None) -> dict | None:
        """Find local installation metadata for the given item (board or library)."""
        is_board = (tab == self.board_tab)
        subfolder = "Boards" if is_board else "Libs"
        dest_dir = os.path.join(self._download_dir if download_dir is None else download_dir, subfolder)
        if not os.path.isdir(dest_dir):
            return None

        versions = item.get("versions", [])
        if not versions:
            return None

        installed_versions = {}
        for ver_entry in versions:
            if cancel is not None and cancel.is_set():
                return None
            v_str = ver_entry.get("version", "")
            if not v_str:
                continue
            v_url = ver_entry.get("url", "")
            archive = _archive_filename(v_url, ver_entry.get("archiveFileName", ""))
            folder_name = _get_folder_name(archive)

            f_path = os.path.join(dest_dir, folder_name)
            a_path = os.path.join(dest_dir, archive)
            has_folder = os.path.isdir(f_path)
            has_archive = os.path.isfile(a_path)

            if has_folder or has_archive:
                installed_versions[v_str] = {
                    "version": v_str,
                    "folder_path": f_path if has_folder else "",
                    "archive_path": a_path if has_archive else "",
                    "has_folder": has_folder,
                    "has_archive": has_archive,
                    "archive": archive,
                    "folder_name": folder_name,
                }

        # Also fallback to check item name in self._installed_items if direct match wasn't in versions list
        installed_items = getattr(self, "_installed_items", []) if installed_items is None else installed_items
        if not installed_versions and installed_items:
            expected_type = "Board Platform" if is_board else "Library"
            for inst in installed_items:
                if cancel is not None and cancel.is_set():
                    return None
                if inst.get("type") == expected_type and inst.get("name", "").lower() == name.lower():
                    inst_v = inst.get("installed_version", "")
                    p = inst.get("path", "")
                    has_folder = os.path.isdir(p)
                    has_archive = os.path.isfile(p)
                    installed_versions[inst_v] = {
                        "version": inst_v,
                        "folder_path": p if has_folder else "",
                        "archive_path": p if has_archive else "",
                        "has_folder": has_folder,
                        "has_archive": has_archive,
                        "archive": inst.get("archive", ""),
                        "folder_name": os.path.basename(p),
                    }
                    break

        if not installed_versions:
            return None

        best_ver = max(installed_versions.keys(), key=_version_key)
        best_info = installed_versions[best_ver]
        latest_catalog_ver = versions[0]["version"] if versions else best_ver
        update_available = (_version_key(latest_catalog_ver) > _version_key(best_ver)) if self._is_online else False

        return {
            "installed_version": best_ver,
            "latest_version": latest_catalog_ver,
            "update_available": update_available,
            "installed_versions": installed_versions,
            "best_info": best_info,
        }

    def _compute_installed_items(self, *, download_dir=None, libraries=None, boards=None, online=None, cancel=None):
        """Scan local entries in the worker; reuse unchanged inventories briefly."""
        download_dir = self._download_dir if download_dir is None else download_dir
        libraries = self.lib_tab.all_items if libraries is None else libraries
        boards = self.board_tab.all_items if boards is None else boards
        online = self._is_online if online is None else online
        cancel = cancel or threading.Event()
        items = []
        seen_paths = set()

        # Fast disk inventory: read local Libs and Boards directories directly
        libs_dir = os.path.join(download_dir, "Libs")
        boards_dir = os.path.join(download_dir, "Boards")

        local_libs = set()
        if os.path.isdir(libs_dir):
            try:
                local_libs = set(os.listdir(libs_dir))
            except Exception:
                local_libs = set()

        local_boards = set()
        if os.path.isdir(boards_dir):
            try:
                local_boards = set(os.listdir(boards_dir))
            except Exception:
                local_boards = set()

        stamps = []
        for directory, entries in ((libs_dir, local_libs), (boards_dir, local_boards)):
            for name in sorted(entries):
                if cancel.is_set():
                    return None
                try:
                    stat = os.stat(os.path.join(directory, name))
                    stamps.append((directory, name, stat.st_mtime_ns, stat.st_size))
                except OSError:
                    continue
        signature = (download_dir, id(libraries), id(boards), online, tuple(stamps))
        memo = getattr(self, '_inventory_memo', None)
        if memo and memo[0] == signature and time.monotonic() - memo[1] < 5:
            return memo[2]

        matched_lib_folders = set()
        matched_lib_archives = set()
        matched_board_folders = set()
        matched_board_archives = set()

        # Helper: check an index item against the pre-scanned directory contents
        def check_index_item(name: str, index_entry: dict, local_set: set, subfolder_name: str, type_label: str):
            dest_dir = os.path.join(download_dir, subfolder_name)
            if not index_entry.get("versions"):
                return
            latest_version = index_entry["versions"][0]["version"]  # sorted newest first

            for ver_entry in index_entry["versions"]:
                if cancel.is_set():
                    return
                archive = _archive_filename(ver_entry["url"], ver_entry.get("archiveFileName", ""))
                folder_name = _get_folder_name(archive)

                # Instant memory set lookup instead of tens of thousands of disk I/O calls
                has_folder = folder_name in local_set
                has_archive = archive in local_set

                if has_folder or has_archive:
                    installed_version = ver_entry["version"]
                    path = os.path.join(dest_dir, folder_name) if has_folder else os.path.join(dest_dir, archive)
                    update_available = (_version_key(latest_version) > _version_key(installed_version)) if online else False
                    norm_p = os.path.normcase(os.path.normpath(path))
                    if norm_p not in seen_paths:
                        seen_paths.add(norm_p)
                        items.append({
                            "type": type_label,
                            "name": name,
                            "installed_version": installed_version,
                            "latest_version": latest_version if online else installed_version,
                            "update_available": update_available,
                            "path": path,
                            "archive": archive,
                        })
                    if type_label == "Library":
                        if has_folder:
                            matched_lib_folders.add(folder_name)
                        if has_archive:
                            matched_lib_archives.add(archive)
                    else:
                        if has_folder:
                            matched_board_folders.add(folder_name)
                        if has_archive:
                            matched_board_archives.add(archive)
                    break  # only report the newest installed version

        # Libraries from catalog index
        if libraries and local_libs:
            for name, entry in libraries.items():
                if cancel.is_set():
                    return None
                check_index_item(name, entry, local_libs, "Libs", "Library")

        # Boards from catalog index
        if boards and local_boards:
            for name, entry in boards.items():
                if cancel.is_set():
                    return None
                check_index_item(name, entry, local_boards, "Boards", "Board Platform")

        # --- Direct Filesystem Discovery: Unmatched / Offline Local Libraries ---
        if os.path.isdir(libs_dir):
            for entry in sorted(local_libs, key=str.lower):
                if cancel.is_set():
                    return None
                if entry in matched_lib_folders or entry in matched_lib_archives:
                    continue
                if entry.startswith(".") or entry.endswith((".part", ".tmp")) or ".part-" in entry or ".previous-" in entry:
                    continue
                entry_path = os.path.join(libs_dir, entry)
                lib_name = entry
                lib_ver = ""

                if os.path.isdir(entry_path):
                    prop_file = os.path.join(entry_path, "library.properties")
                    if os.path.isfile(prop_file):
                        try:
                            with open(prop_file, "r", encoding="utf-8", errors="replace") as pf:
                                for line in pf:
                                    line = line.strip()
                                    if line.startswith("name="):
                                        val = line.split("=", 1)[1].strip()
                                        if val:
                                            lib_name = val
                                    elif line.startswith("version="):
                                        lib_ver = line.split("=", 1)[1].strip()
                        except Exception:
                            pass
                    if not lib_ver:
                        m = re.search(r'[-_v](\d+\.\d+(?:\.\d+)?(?:[-_+][\w\.]+)?)$', entry)
                        if m:
                            lib_ver = m.group(1)
                            if lib_name == entry:
                                lib_name = entry[:m.start()].rstrip("-_v")
                    if not lib_ver:
                        lib_ver = "Installed"
                    archive_name = f"{entry}.zip"
                elif os.path.isfile(entry_path):
                    m = re.search(r'[-_v](\d+\.\d+(?:\.\d+)?(?:[-_+][\w\.]+)?)(?:\.zip|\.tar|\.tgz)', entry, re.IGNORECASE)
                    if m:
                        lib_ver = m.group(1)
                        lib_name = entry[:m.start()].rstrip("-_v")
                    else:
                        lib_name = _get_folder_name(entry)
                        lib_ver = "Installed"
                    archive_name = entry
                else:
                    continue

                norm_p = os.path.normcase(os.path.normpath(entry_path))
                if norm_p not in seen_paths:
                    seen_paths.add(norm_p)
                    items.append({
                        "type": "Library",
                        "name": lib_name,
                        "installed_version": lib_ver,
                        "latest_version": lib_ver if not online else "—",
                        "update_available": False,
                        "path": entry_path,
                        "archive": archive_name,
                    })

        # --- Direct Filesystem Discovery: Unmatched / Offline Local Boards ---
        if os.path.isdir(boards_dir):
            for entry in sorted(local_boards, key=str.lower):
                if cancel.is_set():
                    return None
                if entry in matched_board_folders or entry in matched_board_archives:
                    continue
                if entry.startswith(".") or entry.endswith((".part", ".tmp")) or ".part-" in entry or ".previous-" in entry:
                    continue
                entry_path = os.path.join(boards_dir, entry)
                board_name = entry
                board_ver = ""

                if os.path.isdir(entry_path):
                    for root, dirs, files in os.walk(entry_path):
                        if cancel.is_set():
                            return None
                        if "platform.txt" in files:
                            try:
                                with open(os.path.join(root, "platform.txt"), "r", encoding="utf-8", errors="replace") as pf:
                                    for line in pf:
                                        line = line.strip()
                                        if line.startswith("name="):
                                            val = line.split("=", 1)[1].strip()
                                            if val:
                                                board_name = val
                                        elif line.startswith("version="):
                                            board_ver = line.split("=", 1)[1].strip()
                                break
                            except Exception:
                                pass
                    if not board_ver:
                        m = re.search(r'[-_v](\d+\.\d+(?:\.\d+)?(?:[-_+][\w\.]+)?)$', entry)
                        if m:
                            board_ver = m.group(1)
                            if board_name == entry:
                                board_name = entry[:m.start()].rstrip("-_v")
                    if not board_ver:
                        board_ver = "Installed"
                    archive_name = f"{entry}.tar.bz2"
                elif os.path.isfile(entry_path):
                    m = re.search(r'[-_v](\d+\.\d+(?:\.\d+)?(?:[-_+][\w\.]+)?)(?:\.tar|\.zip|\.tgz)', entry, re.IGNORECASE)
                    if m:
                        board_ver = m.group(1)
                        board_name = entry[:m.start()].rstrip("-_v")
                    else:
                        board_name = _get_folder_name(entry)
                        board_ver = "Installed"
                    archive_name = entry
                else:
                    continue

                norm_p = os.path.normcase(os.path.normpath(entry_path))
                if norm_p not in seen_paths:
                    seen_paths.add(norm_p)
                    items.append({
                        "type": "Board Platform",
                        "name": board_name,
                        "installed_version": board_ver,
                        "latest_version": board_ver if not online else "—",
                        "update_available": False,
                        "path": entry_path,
                        "archive": archive_name,
                    })

        # Sort alphabetically
        items.sort(key=lambda x: x["name"].lower())
        if cancel.is_set():
            return None
        self._inventory_memo = signature, time.monotonic(), items
        return items

    def _scan_inventory_request(self, request, cancel):
        folder, libraries, boards, online = request
        return self._compute_installed_items(download_dir=folder, libraries=libraries,
                                             boards=boards, online=online, cancel=cancel)

    def _cache_is_fresh(self, cache_file: str) -> bool:
        if not os.path.isfile(cache_file):
            return False
        age = time.time() - os.path.getmtime(cache_file)
        return age < CACHE_MAX_AGE_SECONDS

    def _load_both(self, force_refresh=False):
        """Publish cached catalogs before HTTP requests; bound refresh concurrency."""
        # From its own module: Python 3.15's concurrent.futures exposes this as a lazy import.
        from concurrent.futures.thread import ThreadPoolExecutor
        from src.modules.runtime_resources import performance_profile
        from src.modules.browser_loading import CatalogPublisher
        publications = CatalogPublisher(getattr(getattr(self, 'lib_tab', None), 'all_items', {}),
                                        getattr(getattr(self, 'board_tab', None), 'all_items', {}))

        def publish(libraries, boards):
            # Content comparison belongs to this worker, never a view callback.
            self._post_ui(self._publish_catalogs, *publications.prepare(libraries, boards))

        urls = [BOARD_INDEX_URL, *self._additional_board_urls]
        paths = [BOARD_CACHE_FILE if index == 0 else _board_index_cache_file(url)
                 for index, url in enumerate(urls)]
        libs = _read_library_catalog(LIBRARY_CACHE_FILE)
        source_urls = dict(zip(paths, urls))
        boards = _read_board_catalog(paths, source_urls=source_urls)
        publish(libs, boards)
        needed = [(index, url, cache) for index, (url, cache) in enumerate(zip(urls, paths))
                  if force_refresh or not self._cache_is_fresh(cache) or not _has_valid_index_cache(cache, 'packages')]
        need_libraries = force_refresh or libs is None or not self._cache_is_fresh(LIBRARY_CACHE_FILE)
        if not needed and not need_libraries:
            self._post_ui(self._finish_load, f'{len(libs or {})} libraries, {len(boards or {})} board platforms loaded from cache')
            return
        self._index_fetch_succeeded = threading.Event()
        self._source_results = {}
        self._source_results_lock = threading.Lock()
        self._catalog_memory_only = False
        # A failed prior refresh must not prevent a later explicit reconnect.
        self._is_online = True
        # Cached lists remain interactive while stale sources refresh in the worker.
        if need_libraries:
            data = self._load_index(LIBRARY_INDEX_URL, LIBRARY_CACHE_FILE, True, 'library')
            if data is not None:
                libs = _read_library_catalog(LIBRARY_CACHE_FILE, data)
                publish(libs, None)
            del data
        fresh, failed = {}, []
        def fetch(index, url, cache):
            return index, url, cache, self._load_index(url, cache, True, 'board' if index == 0 else f'additional board {index}')
        workers = 2 if performance_profile().constrained else 4
        with ThreadPoolExecutor(max_workers=min(workers, max(1, len(needed))), thread_name_prefix='BoardIndex') as pool:
            futures = [pool.submit(fetch, *entry) for entry in needed]
            for future in futures:
                index, url, cache, data = future.result()
                if data is not None:
                    fresh[cache] = data
                elif index:
                    failed.append(url)
        if needed:
            boards = _read_board_catalog(paths, fresh, source_urls=source_urls)
        self._is_online = self._index_fetch_succeeded.is_set()
        publish(libs, boards)
        message = f'{len(libs or {})} libraries, {len(boards or {})} board platforms loaded'
        if not self._is_online:
            message += ' — offline/unavailable; using local caches'
        if failed:
            message += f'; {len(failed)} additional index(es) unavailable'
        if self._catalog_memory_only:
            message += '; cache storage unavailable — downloaded data kept in memory'
        lines = [f"{source}: {outcome}" for source, outcome in self._source_results.items()
                 if source != LIBRARY_INDEX_URL]
        self._post_ui(self._set_sources_status, "\n".join(lines))
        self._post_ui(self._finish_load, message)

    def _load_index(self, url: str, cache_file: str,
                    force_refresh: bool, label: str) -> dict | None:
        expected_key = "libraries" if label.casefold() == "library" else "packages"
        if not force_refresh and self._cache_is_fresh(cache_file):
            cached = _read_index_cache(cache_file, expected_key)
            if cached is not None:
                return cached
            else:
                try:
                    os.unlink(cache_file)
                    with _INDEX_JSON_RAM_LOCK:
                        _INDEX_JSON_RAM_CACHE.pop(cache_file, None)
                except OSError:
                    pass

        # Offline safety guard: If offline flag is active or no connection, never make network calls!
        if not getattr(self, "_is_online", True):
            stale = _read_index_cache(cache_file, expected_key)
            return stale

        self._post_ui(self._set_status, f"Downloading {label} index…")
        normalized_url = _normalize_board_manager_url(url) or str(url).strip()
        data = None
        raw_text = None
        errors: list[str] = []
        def source_result(outcome):
            if hasattr(self, "_source_results_lock"):
                with self._source_results_lock:
                    self._source_results[normalized_url] = outcome

        def _parse_response(raw_bytes: bytes):
            text = raw_bytes.decode("utf-8-sig")
            return _validate_index_payload(json.loads(text), expected_key), text

        if requests is not None:
            resp = None
            try:
                resp = requests.get(normalized_url, timeout=(5, 12), headers=DEFAULT_HEADERS)
                resp.raise_for_status()
                response_bytes = getattr(resp, "content", b"")
                if not response_bytes:
                    response_bytes = str(getattr(resp, "text", "")).encode("utf-8")
                data, raw_text = _parse_response(response_bytes)
            except Exception as exc:
                errors.append(str(exc))
                data = None
            finally:
                if resp is not None:
                    resp.close()

        if data is None:
            try:
                import urllib.request
                req = urllib.request.Request(normalized_url, headers=DEFAULT_HEADERS)
                with urllib.request.urlopen(req, timeout=12) as uresp:
                    data, raw_text = _parse_response(uresp.read())
            except Exception as e:
                errors.append(str(e))

        if data is not None and raw_text is not None:
            source_result("Available")
            if hasattr(self, '_index_fetch_succeeded'):
                self._index_fetch_succeeded.set()
            try:
                _write_index_cache(cache_file, raw_text)
            except OSError:
                data['_browser_cache_written'] = False
                self._catalog_memory_only = True
            return data

        # A failed refresh must not throw away the last known-good index.
        stale = _read_index_cache(cache_file, expected_key)
        if stale is not None:
            source_result("Refresh failed; using saved catalog")
            self._post_ui(self._set_status, f"Offline/unavailable: loaded cached {label} index")
            return stale

        detail = next((error for error in errors if error), "invalid index response")
        source_result(f"Unavailable — {detail[:200]}")
        self._post_ui(self._set_status, f"Failed to download {label} index: {detail}")
        return None

    def _publish_job(self, stage, *, job_id=None, title=None, message=None, progress=None, **details):
        job_id = job_id or getattr(self, "_active_package_job", None)
        if not job_id:
            return
        try:
            from src.modules.package_jobs import publish_event
            publish_event(job_id, stage, root=getattr(self, "_package_event_root", None), title=title,
                          message=message, progress=progress, **details)
        except Exception as error:
            # Tracking is optional; a full/read-only journal must not strand
            # transfer controls or recursively attempt another journal write.
            if not getattr(self, "_package_tracking_error", None):
                self._package_tracking_error = str(error)
                self._post_ui(self._set_status, f"Package tracking unavailable: {error}")

    def _begin_package_job(self, name):
        import uuid
        self._active_package_job = uuid.uuid4().hex
        self._package_tracking_error = None
        self._preparation_tracking_error = None

    def _handoff_board_preparation(self, folder, metadata, job_id):
        from src.modules.board_preparation import start_preparation
        self._publish_job("queued", job_id=job_id, message="Preparing downloaded board support")
        child = start_preparation(folder, job_id=job_id, package_metadata=metadata,
                                  event_root=getattr(self, "_package_event_root", None))
        tracking_error = getattr(child, "preparation_tracking_error", None)
        if isinstance(tracking_error, str) and tracking_error:
            # The child already owns the installation. Keep its job running
            # and report only the missing exit watcher; never replay the spawn.
            self._preparation_tracking_error = tracking_error

    def _prepare_existing_worker(self, tab, destination, metadata, job_id):
        import shutil
        archive = _archive_filename(metadata.get("url", ""), metadata.get("archiveFileName", ""))
        archive_path = os.path.join(destination, archive)
        folder = os.path.join(destination, _get_folder_name(archive))
        staging = f"{folder}.part-{os.getpid()}-{threading.get_ident()}"
        try:
            self._publish_job("queued", job_id=job_id, title=metadata.get("name"), message="Checking downloaded board files")
            if not os.path.isdir(folder):
                if not os.path.isfile(archive_path):
                    raise FileNotFoundError("Download this board version before preparing support")
                self._publish_job("verifying", job_id=job_id, message="Verifying saved board archive")
                _verify_download(archive_path, metadata.get("size", 0), metadata.get("checksum", ""))
                _validate_archive_file(archive_path)
                self._publish_job("extracting", job_id=job_id, message="Extracting saved board archive")
                _extract_archive(archive_path, staging, self._cancel_event)
                if self._cancel_event.is_set():
                    raise InterruptedError("Preparation cancelled")
                _promote_directory(staging, folder)
            elif self._cancel_event.is_set():
                raise InterruptedError("Preparation cancelled")
            self._handoff_board_preparation(folder, metadata, job_id)
            self._post_ui(self._download_done, tab, folder, True)
        except InterruptedError:
            self._publish_job("cancelled", job_id=job_id, message="Board preparation cancelled")
            self._post_ui(self._download_cancelled, tab)
        except Exception as error:
            self._publish_job("failed", job_id=job_id, message=str(error))
            self._post_ui(self._download_error, tab, f"Board preparation failed:\n{error}")
        finally:
            if os.path.isdir(staging) and _inside_directory(staging, destination):
                shutil.rmtree(staging, ignore_errors=True)

    def _download_worker(
        self,
        tab: BrowseTab,
        url: str,
        archive: str,
        dest_dir: str,
        download_option: str,
        cleanup_old: tuple | None = None,
        package_metadata: dict | None = None,
        job_id: str | None = None,
    ):
        import shutil
        filepath = os.path.join(dest_dir, archive)
        folder_path = os.path.join(dest_dir, _get_folder_name(archive))
        partial_path = f"{filepath}.part"
        checkpoint_path = partial_path + ".json"
        extraction_path = f"{folder_path}.part-{os.getpid()}-{threading.get_ident()}"
        retain_partial = False
        try:
            os.makedirs(dest_dir, exist_ok=True)
            self._publish_job("queued", job_id=job_id, title=(package_metadata or {}).get("name"), message="Starting package download")
            self._publish_job("downloading", job_id=job_id, message="Downloading package archive")
            last_progress = time.monotonic()

            determinate = False

            def report_progress(downloaded, total):
                nonlocal determinate, last_progress
                if total and not determinate:
                    determinate = True
                    self._post_ui(self._set_progress_determinate, total)
                if total:
                    self._post_ui(self._update_progress, downloaded, total)
                if time.monotonic() - last_progress >= .25:
                    self._publish_job("downloading", job_id=job_id, message="Downloading package archive",
                                      progress=min(99, int(downloaded / total * 100)) if total else None)
                    last_progress = time.monotonic()

            _download_package_archive(url, partial_path, package_metadata or {}, self._cancel_event, report_progress)

            if self._cancel_event.is_set():
                raise InterruptedError("Package download cancelled")
            self._publish_job("verifying", job_id=job_id, message="Verifying package size and checksum")
            _verify_download(
                partial_path,
                (package_metadata or {}).get("size", 0),
                (package_metadata or {}).get("checksum", ""),
            )
            _validate_archive_file(partial_path)
            if download_option in ("folder", "both"):
                self._publish_job("extracting", job_id=job_id, message="Extracting verified package")
                self._post_ui(self._set_status, "Extracting files…")
                if os.path.isdir(extraction_path):
                    shutil.rmtree(extraction_path, ignore_errors=True)
                _extract_archive(partial_path, extraction_path, self._cancel_event)
                # Replace an older folder only after the new archive has been
                # downloaded, verified and fully extracted.
                if self._cancel_event.is_set():
                    raise InterruptedError("Package extraction cancelled")
                _promote_directory(extraction_path, folder_path)
            if download_option not in ("folder", "both") and self._cancel_event.is_set():
                raise InterruptedError("Package download cancelled")
            os.replace(partial_path, filepath)

            if download_option == "folder":
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except OSError:
                    pass

            # Safe cleanup of old version only after new version is confirmed extracted/saved
            if cleanup_old:
                old_p, old_arc = cleanup_old
                if old_p and _inside_directory(old_p, dest_dir) and os.path.exists(old_p):
                    try:
                        norm_old = os.path.normpath(old_p).lower()
                        norm_new_folder = os.path.normpath(folder_path).lower()
                        norm_new_file = os.path.normpath(filepath).lower()
                        if norm_old != norm_new_folder and norm_old != norm_new_file:
                            if os.path.isdir(old_p):
                                shutil.rmtree(old_p)
                            elif os.path.isfile(old_p):
                                os.remove(old_p)
                    except Exception:
                        pass
                if old_arc:
                    old_arc_path = os.path.join(dest_dir, old_arc)
                    if _inside_directory(old_arc_path, dest_dir) and os.path.isfile(old_arc_path) and os.path.normpath(old_arc_path).lower() != os.path.normpath(filepath).lower():
                        try:
                            os.remove(old_arc_path)
                        except Exception:
                            pass

            preparing = tab == self.board_tab and download_option in ("folder", "both") and bool(job_id)
            if preparing:
                self._handoff_board_preparation(folder_path, package_metadata or {}, job_id)
            else:
                self._publish_job("unavailable" if tab == self.board_tab else "ready", job_id=job_id,
                    message="Archive saved; select Prepare board support to verify targets" if tab == self.board_tab else "Library download complete",
                    progress=100)
            self._post_ui(self._download_done, tab, filepath if download_option != "folder" else folder_path, preparing)

        except (InterruptedError, _DownloadInterrupted) as e:
            retain_partial = True
            if self._cancel_event.is_set():
                self._publish_job("cancelled", job_id=job_id, message=str(e))
                self._post_ui(self._download_cancelled, tab)
            else:
                self._publish_job("failed", job_id=job_id, message=str(e))
                self._post_ui(self._download_error, tab, str(e))
        except OSError as e:
            if self._cancel_event.is_set():
                self._publish_job("cancelled", job_id=job_id, message="Package download cancelled")
                self._post_ui(self._download_cancelled, tab)
            else:
                self._publish_job("failed", job_id=job_id, message=str(e))
                self._post_ui(self._download_error, tab,
                               f"File/download error:\n{e}")
        except Exception as e:
            if self._cancel_event.is_set():
                self._publish_job("cancelled", job_id=job_id, message="Package download cancelled")
                self._post_ui(self._download_cancelled, tab)
            else:
                self._publish_job("failed", job_id=job_id, message=str(e))
                self._post_ui(self._download_error, tab,
                               f"Download failed:\n{e}")
        finally:
            leftovers = (extraction_path,) if retain_partial else (partial_path, checkpoint_path, extraction_path)
            for leftover in leftovers:
                try:
                    if os.path.isfile(leftover):
                        os.remove(leftover)
                    elif os.path.isdir(leftover):
                        shutil.rmtree(leftover, ignore_errors=True)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.modules.ubuntu_release import enforce_supported_ubuntu
    if not enforce_supported_ubuntu():
        raise SystemExit(1)
    from src.modules.runtime_resources import enforce_minimum_cpu_requirement
    if not enforce_minimum_cpu_requirement():
        raise SystemExit(2)
    from main.qt.package_browser import main
    raise SystemExit(main())
