"""
Arduino Library & Board Browser
================================
A desktop GUI (tkinter / ttk) that downloads and browses the official
Arduino library index and board package index, letting the user search,
inspect, and download any version of any library or board platform.

Features
--------
* Tabbed interface: Libraries tab + Boards tab + Installed tab.
* Correct flat-list JSON parsing for both indexes.
* Local file cache so indexes are only fetched once per 24 hours.
* Cache-first loading, bounded disk/network workers and queued Tk updates.
* Split-pane layout: list on the left, detail panel on the right.
* Version dropdown to pick any release.
* Progress bar for index loading and package archive downloads.
* Clickable website / repository links (opens in default browser).
* Installed tab shows all downloaded items with **available update** indicators.
* User-configurable additional board manager indexes, including multiple
  comma-separated vendor URLs.
* Static glass cards and keyboard-accessible tabs share Glass, Frosted Light
  and Solarized Dark palettes with the main workspace.
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
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
import webbrowser
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


def _read_index_cache(cache_file: str, expected_key: str) -> dict | None:
    try:
        st = os.stat(cache_file)
        with _INDEX_JSON_RAM_LOCK:
            cached = _INDEX_JSON_RAM_CACHE.get(cache_file)
            if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                _INDEX_JSON_RAM_CACHE.move_to_end(cache_file)
                return _validate_index_payload(cached[2], expected_key)
            _INDEX_JSON_RAM_CACHE.pop(cache_file, None)

        with open(cache_file, "r", encoding="utf-8-sig") as fh:
            data = _validate_index_payload(json.load(fh), expected_key)
            with _INDEX_JSON_RAM_LOCK:
                if st.st_size <= 1_000_000:
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



class Theme:
    PALETTES = {
        "default": {
            "BG_DARKEST": "#0a0e14",
            "BG_DARK": "#10151c",
            "BG_MID": "#161d27",
            "BG_LIGHT": "#1c2532",
            "BG_HOVER": "#243040",
            "BORDER": "#2a3545",
            "BORDER_LIT": "#3d5068",
            "TEXT": "#c8d2dc",
            "TEXT_DIM": "#6b7d94",
            "TEXT_BRIGHT": "#e8edf3",
            "CYAN": "#39c5bb",
            "CYAN_DIM": "#1f7872",
            "GREEN": "#5ccc6e",
            "GREEN_DIM": "#2d6636",
            "YELLOW": "#e8b83a",
            "YELLOW_DIM": "#7a6020",
            "RED": "#f05050",
            "RED_DIM": "#7a2828",
            "MAGENTA": "#c678dd",
            "BLUE": "#61afef",
            "ORANGE": "#d19a66",
            "BTN_COMPILE": "#2d7d46",
            "BTN_COMPILE_H": "#38a058",
            "BTN_UPLOAD": "#8244a0",
            "BTN_UPLOAD_H": "#a05cc0",
            "BTN_FULL": "#2077b0",
            "BTN_FULL_H": "#2899dd",
            "BTN_MONITOR": "#1a7a70",
            "BTN_MONITOR_H": "#22a090",
            "BTN_STOP": "#a03030",
            "BTN_STOP_H": "#cc4444",
            "BTN_CLEAR": "#3a4555",
            "BTN_CLEAR_H": "#4a5a70",
        },
        "light": {
            "BG_DARKEST": "#f4f6f9",
            "BG_DARK": "#e9ecef",
            "BG_MID": "#ffffff",
            "BG_LIGHT": "#dee2e6",
            "BG_HOVER": "#d0d7de",
            "BORDER": "#c5ccd6",
            "BORDER_LIT": "#0969da",
            "TEXT": "#24292f",
            "TEXT_DIM": "#57606a",
            "TEXT_BRIGHT": "#1a1f24",
            "CYAN": "#0969da",
            "CYAN_DIM": "#0550ae",
            "GREEN": "#1a7f37",
            "GREEN_DIM": "#116329",
            "YELLOW": "#9a6700",
            "YELLOW_DIM": "#7d4e00",
            "RED": "#cf222e",
            "RED_DIM": "#a40e26",
            "MAGENTA": "#8250df",
            "BLUE": "#0969da",
            "ORANGE": "#bc4c00",
            "BTN_COMPILE": "#2da44e",
            "BTN_COMPILE_H": "#2c974b",
            "BTN_UPLOAD": "#8250df",
            "BTN_UPLOAD_H": "#753fe0",
            "BTN_FULL": "#0969da",
            "BTN_FULL_H": "#0858b8",
            "BTN_MONITOR": "#0e8a7e",
            "BTN_MONITOR_H": "#0b7066",
            "BTN_STOP": "#cf222e",
            "BTN_STOP_H": "#b61c27",
            "BTN_CLEAR": "#e1e4e8",
            "BTN_CLEAR_H": "#d0d7de",
        },
        "solarized_dark": {
            "BG_DARKEST": "#001b22",
            "BG_DARK": "#002b36",
            "BG_MID": "#073642",
            "BG_LIGHT": "#0d4a59",
            "BG_HOVER": "#115d70",
            "BORDER": "#166b80",
            "BORDER_LIT": "#2aa198",
            "TEXT": "#ffffff",
            "TEXT_DIM": "#d0e4e8",
            "TEXT_BRIGHT": "#ffffff",
            "CYAN": "#2aa198",
            "CYAN_DIM": "#208078",
            "GREEN": "#859900",
            "GREEN_DIM": "#586600",
            "YELLOW": "#b58900",
            "YELLOW_DIM": "#7a5d00",
            "RED": "#dc322f",
            "RED_DIM": "#93201e",
            "MAGENTA": "#d33682",
            "BLUE": "#268bd2",
            "ORANGE": "#cb4b16",
            "BTN_COMPILE": "#2da44e",
            "BTN_COMPILE_H": "#38b95e",
            "BTN_UPLOAD": "#268bd2",
            "BTN_UPLOAD_H": "#3a9de0",
            "BTN_FULL": "#268bd2",
            "BTN_FULL_H": "#3a9de0",
            "BTN_MONITOR": "#2aa198",
            "BTN_MONITOR_H": "#38b8ae",
            "BTN_STOP": "#dc322f",
            "BTN_STOP_H": "#e84a47",
            "BTN_CLEAR": "#0a4554",
            "BTN_CLEAR_H": "#115d70",
        }
    }

    BG_DARKEST  = "#0a0e14"
    BG_DARK     = "#10151c"
    BG_MID      = "#161d27"
    BG_LIGHT    = "#1c2532"
    BG_HOVER    = "#243040"
    BORDER      = "#2a3545"
    BORDER_LIT  = "#3d5068"

    TEXT        = "#c8d2dc"
    TEXT_DIM    = "#6b7d94"
    TEXT_BRIGHT = "#e8edf3"

    CYAN        = "#39c5bb"
    CYAN_DIM    = "#1f7872"
    GREEN       = "#5ccc6e"
    GREEN_DIM   = "#2d6636"
    YELLOW      = "#e8b83a"
    YELLOW_DIM  = "#7a6020"
    RED         = "#f05050"
    RED_DIM     = "#7a2828"
    MAGENTA     = "#c678dd"
    BLUE        = "#61afef"
    ORANGE      = "#d19a66"

    BTN_COMPILE   = "#2d7d46"
    BTN_COMPILE_H = "#38a058"
    BTN_UPLOAD    = "#8244a0"
    BTN_UPLOAD_H  = "#a05cc0"
    BTN_FULL      = "#2077b0"
    BTN_FULL_H    = "#2899dd"
    BTN_MONITOR   = "#1a7a70"
    BTN_MONITOR_H = "#22a090"
    BTN_STOP      = "#a03030"
    BTN_STOP_H    = "#cc4444"
    BTN_CLEAR     = "#3a4555"
    BTN_CLEAR_H   = "#4a5a70"

    active_theme = "default"

    @classmethod
    def apply_theme(cls, mode: str = "default") -> str:
        mode_key = (mode or "default").lower().strip()
        if mode_key in ("solarized", "solarize", "solarized-dark", "solarized_dark", "solarize_dark"):
            mode_key = "solarized_dark"
        elif mode_key not in cls.PALETTES:
            mode_key = "default"
        palette = cls.PALETTES[mode_key]
        for key, val in palette.items():
            setattr(cls, key, val)
        cls.active_theme = mode_key
        return mode_key


try:
    from main.core.theme import Theme as _SharedTheme
    Theme.PALETTES = _SharedTheme.PALETTES
    Theme.apply_theme("default")
except ImportError:
    pass


def _detect_system_theme() -> str:
    if sys.platform.startswith("linux"):
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
                if m in Theme.PALETTES or m in ("solarized", "solarize", "solarized-dark", "solarized_dark", "solarize_dark"):
                    return m
        except Exception:
            pass
    return "default"

Theme.apply_theme(_resolve_lib_req_theme())


def make_flat_button(parent, text, command, bg, bg_hover, font=("Montserrat", 9, "bold")) -> tk.Button:
    from src.modules.tk_glass import readable_foreground
    btn = tk.Button(
        parent, text=text, command=command,
        font=font, fg=readable_foreground(bg), bg=bg,
        activebackground=bg_hover, activeforeground=readable_foreground(bg_hover),
        disabledforeground=Theme.TEXT_DIM,
        relief=tk.FLAT, borderwidth=0, padx=12, pady=6, cursor="hand2",
        highlightthickness=1, highlightbackground=Theme.BORDER, highlightcolor=Theme.CYAN
    )
    btn._normal_bg, btn._hover_bg = bg, bg_hover
    btn._bg_token = next((key for key in ("BTN_CLEAR", "BTN_MONITOR", "BTN_COMPILE", "BTN_STOP", "BTN_UPLOAD", "BTN_FULL", "BTN_DIM")
                          if getattr(Theme, key) == bg), None)
    btn.bind("<Enter>", lambda e, b=btn: b.configure(bg=b._hover_bg, fg=readable_foreground(b._hover_bg), cursor="hand2") if str(b["state"]) != "disabled" else b.configure(cursor="arrow"))
    btn.bind("<Leave>", lambda e, b=btn: b.configure(bg=b._normal_bg, fg=readable_foreground(b._normal_bg)))
    return btn


class CircularLoadingOverlay(tk.Frame):
    """Circular arc loading spinner overlay matching the AI Assistant loading animation."""

    def __init__(self, parent, bg_color=None, spinner_color=None,
                 title="Loading Arduino Indexes...", subtitle="Downloading & scanning packages in background thread..."):
        bg_color = bg_color or Theme.BG_DARKEST
        spinner_color = spinner_color or Theme.CYAN
        super().__init__(parent, bg=bg_color)
        from src.modules.runtime_resources import performance_profile
        self._frame_interval = 180 if performance_profile().constrained else 100
        self.bg_color = bg_color
        self.spinner_color = spinner_color
        self.angle = 0
        self.is_animating = True
        self._after_id = None

        self.center_frame = tk.Frame(self, bg=bg_color)
        self.center_frame.place(relx=0.5, rely=0.5, anchor="center")

        self.size = 56
        self.canvas = tk.Canvas(
            self.center_frame,
            width=self.size,
            height=self.size,
            bg=bg_color,
            highlightthickness=0
        )
        self.canvas.pack(pady=(0, 12))

        self.title_label = tk.Label(
            self.center_frame,
            text=title,
            font=("Montserrat", 11, "bold"),
            fg=Theme.TEXT_BRIGHT,
            bg=bg_color
        )
        self.title_label.pack(pady=(0, 4))

        self.sub_label = tk.Label(
            self.center_frame,
            text=subtitle,
            font=("Montserrat", 9),
            fg=Theme.TEXT_DIM,
            bg=bg_color
        )
        self.sub_label.pack()

        self._draw_spinner()

    def _draw_spinner(self):
        if not self.is_animating:
            return
        try:
            self.canvas.delete("all")
            pad = 6
            r = self.size - pad
            self.canvas.create_oval(
                pad, pad, r, r,
                outline=Theme.BORDER,
                width=4
            )
            self.canvas.create_arc(
                pad, pad, r, r,
                start=self.angle,
                extent=100,
                outline=self.spinner_color,
                style="arc",
                width=4
            )
            self.angle = (self.angle + 12) % 360
            self._after_id = self.after(self._frame_interval, self._draw_spinner)
        except Exception:
            pass

    def update_message(self, title=None, subtitle=None):
        try:
            if title is not None and self.title_label.winfo_exists():
                self.title_label.configure(text=str(title))
            if subtitle is not None and self.sub_label.winfo_exists():
                self.sub_label.configure(text=str(subtitle))
        except Exception:
            pass

    def stop_and_destroy(self):
        self.is_animating = False
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass


def _find_code_viewer_python(cancel=None) -> Optional[str]:
    """Find a Python executable that has PyQt5 and QScintilla available."""
    if sys.platform == "win32":
        prepared = [Path(SCRIPT_DIR) / "env" / "Scripts" / name
                    for name in ("pythonw.exe", "python.exe")]
        prepared += [Path(SCRIPT_DIR) / "src" / "_python" / name
                     for name in ("pythonw.exe", "python.exe")]
    else:
        prepared = [Path(SCRIPT_DIR) / directory / "bin" / "python"
                    for directory in (".venv-linux", "env")]
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


def _open_fallback_editor(file_path: str, parent=None, reason: Optional[str] = None) -> bool:
    """Open sketch file in Notepad as graceful fallback without triggering external Arduino IDE."""
    if not file_path or not os.path.exists(file_path):
        return False
    try:
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", file_path])
            return True
        else:
            subprocess.Popen(["xdg-open", file_path])
            return True
    except Exception:
        pass
    if parent and reason:
        try:
            messagebox.showerror("Code Viewer", f"{reason}\n\nFile: {file_path}", parent=parent)
        except Exception:
            pass
    return False


def _launch_code_viewer(file_path, all_paths=None, parent=None, *, python_exe=None,
                        theme_mode=None, font_size=None):
    viewer_script = os.path.join(SCRIPT_DIR, "src", "qscintilla_viewer.py")
    if not os.path.exists(viewer_script):
        _open_fallback_editor(file_path, parent=parent, reason=f"Viewer script not found:\n{viewer_script}")
        return

    py_exe = python_exe or _find_code_viewer_python() or sys.executable
    try:
        cmd = [str(py_exe), viewer_script, file_path]
        if all_paths:
            cmd.extend(all_paths)
        environment = os.environ.copy()
        if theme_mode is not None:
            environment["MCU_FLASHER_VIEWER_THEME"] = str(theme_mode)
        if font_size is not None:
            environment["MCU_FLASHER_VIEWER_FONT_SIZE"] = str(font_size)
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(cmd, creationflags=creationflags, env=environment)
        if sys.platform.startswith("linux"):
            from src.modules.ubuntu_download_manager import remember_viewer
            remember_viewer(process)
    except Exception as e:
        _open_fallback_editor(file_path, parent=parent, reason=f"Failed to launch QScintilla viewer:\n{e}")


def _open_code_viewer(file_path, all_paths=None, parent=None, *, is_current=None):
    """Probe the prepared viewer off Tk, then open the latest valid selection."""
    if parent is None:
        return _open_fallback_editor(file_path)
    from src.modules.browser_loading import TkTasks, LatestScan
    tasks = getattr(parent, "_mcu_code_viewer_tasks", None)
    if tasks is None or tasks.closed:
        tasks = parent._mcu_code_viewer_tasks = TkTasks(parent)

        def probe(_request, cancel):
            from main.core.config import get_monitor_font_size
            interpreter = _find_code_viewer_python(cancel)
            return interpreter, get_monitor_font_size()

        parent._mcu_code_viewer_scan = LatestScan(tasks, probe)
    paths = tuple(all_paths or (file_path,))
    request_id = getattr(parent, "_mcu_code_viewer_request", 0) + 1
    parent._mcu_code_viewer_request = request_id

    def completed(result, error):
        if (tasks.closed or request_id != parent._mcu_code_viewer_request
                or (is_current is not None and not is_current())
                or parent.state() == "withdrawn"):
            return
        if not os.path.isfile(file_path):
            messagebox.showerror("Code Viewer", "Sample file no longer exists on disk.", parent=parent)
            return
        interpreter, font_size = result if result is not None else (None, 12)
        if error:
            messagebox.showerror("Code Viewer", f"Unable to prepare the sample viewer:\n{error}", parent=parent)
            return
        if interpreter is None:
            messagebox.showinfo("Bootstrap required",
                                "Prepare the code viewer dependencies in Bootstrap, then reopen this sample.",
                                parent=parent)
            return
        _launch_code_viewer(file_path, paths, parent=parent, python_exe=interpreter,
                            theme_mode=Theme.active_theme, font_size=font_size)

    parent._mcu_code_viewer_scan.submit((file_path, paths), completed)


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

class BrowseTab:
    """A reusable tab with: search bar, list pane, detail pane, download."""

    def __init__(self, parent: ttk.Frame, app: "ArduinoBrowser",
                 detail_builder, on_select_handler):
        self.app = app
        self._on_select_handler = on_select_handler
        self._detail_builder = detail_builder

        self.all_items: dict[str, dict] = {}
        self.sorted_names: list[str] = []
        self.name_tuples: list[tuple[str, str]] = []
        self.filtered_names: list[str] = []
        self._search_after_id = None
        self.loaded_count = 0
        self._loading_more = False
        self._load_more_after_id = None
        self._label_after_id = None
        self._search_revision = 0
        from src.modules.browser_loading import LatestScan, search_catalog
        self._search_worker = LatestScan(app._tasks, search_catalog)
        self._version_revision = 0
        self._version_worker = LatestScan(app._tasks, lambda request, cancel: None if cancel.is_set()
            else app._get_installed_info(*request[:3], download_dir=request[3], installed_items=request[4], cancel=cancel))

        self._wrapping_labels: list[Any] = []
        self.lbl_name: Any = None
        self.lbl_package: Any = None
        self.lbl_author: Any = None
        self.lbl_maintainer: Any = None
        self.lbl_category: Any = None
        self.lbl_arch: Any = None
        self.lbl_boards_header: Any = None
        self.lbl_boards: Any = None
        self.lbl_sentence: Any = None
        self.lbl_paragraph: Any = None
        self.lbl_size: Any = None
        self.lbl_available: Any = None
        self.lbl_status_badge: Any = None
        self.link_website: Any = None
        self.link_help: Any = None
        self.link_repo: Any = None
        self.version_combo: Any = None
        self.version_var: Any = None
        self.download_btn: Any = None
        self._current_website: Any = None
        self._current_help: Any = None
        self._current_repo: Any = None
        self._current_item: Any = None

        self._build(parent)

        def cancel_callbacks(event):
            if event.widget is parent:
                self._search_revision += 1
                self._search_worker.cancel()
                self._version_revision += 1
                self._version_worker.cancel()
                for name in ('_search_after_id', '_load_more_after_id', '_label_after_id'):
                    timer = getattr(self, name, None)
                    if timer is not None:
                        self.app.root.after_cancel(timer)
                        setattr(self, name, None)
        parent.bind('<Destroy>', cancel_callbacks, add='+')

    def _build(self, parent: ttk.Frame):
        # Search bar
        top = tk.Frame(parent, bg=Theme.BG_DARKEST, pady=4)
        top.pack(fill="x")

        tk.Label(top, text="Search:", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            top, textvariable=self.search_var,
            width=40, font=("Montserrat", 10),
            bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN,
            highlightbackground=Theme.BORDER
        )
        self.lbl_search_status = tk.Label(top, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST)
        self.lbl_search_status.pack(side="right", padx=(10, 0))
        self.search_entry.pack(side="left", padx=(8, 10), fill="x", expand=True)
        self.search_entry.bind("<KeyRelease>", self._on_search)
        self.search_entry.bind("<Return>", lambda _event: self._execute_search())

        # Paned window: list | detail
        pane = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=Theme.BORDER, sashwidth=2, sashrelief=tk.FLAT, bd=0)
        pane.pack(fill="both", expand=True, pady=(4, 4))

        # --- Left: item list ---
        from src.modules.tk_glass import GlassCard
        left_card = GlassCard(pane, Theme, padding=7, expand=True)
        pane.add(left_card, minsize=200)
        left = left_card.body

        self.listbox = tk.Listbox(
            left, font=("Consolas", 10),
            bg=Theme.BG_MID, fg=Theme.TEXT_BRIGHT,
            selectbackground=Theme.CYAN_DIM, selectforeground="#ffffff",
            highlightcolor=Theme.CYAN, highlightbackground=Theme.BORDER,
            borderwidth=1, relief=tk.FLAT,
            activestyle="none",
            exportselection=False
        )
        self.list_scroll = ttk.Scrollbar(left, orient="vertical", style="Vertical.TScrollbar",
                                         command=self.listbox.yview)
        self.listbox.config(yscrollcommand=self._on_scroll)
        self.list_scroll.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # --- Right: detail panel ---
        detail_card = GlassCard(pane, Theme, padding=9, expand=True)
        pane.add(detail_card, minsize=350)
        self.detail_frame = detail_card.body
        self.detail_frame.configure(bg=Theme.BG_DARKEST)
        from src.modules.tk_glass import ResponsivePanes
        self._responsive_panes = ResponsivePanes(pane, left_card, detail_card)

        # Placeholder
        self.lbl_placeholder = tk.Label(
            self.detail_frame, text="No Item Selected",
            font=("Montserrat", 13), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="center"
        )
        self.lbl_placeholder.pack(fill="both", expand=True, padx=10, pady=10)

        # Scrollable Canvas container for detail panel (Scrollable IF AND ONLY IF content overflows)
        self.detail_canvas = tk.Canvas(self.detail_frame, bg=Theme.BG_DARKEST, highlightthickness=0, borderwidth=0)
        self.detail_scroll = ttk.Scrollbar(self.detail_frame, orient="vertical", style="Vertical.TScrollbar",
                                           command=self.detail_canvas.yview)
        self.detail_canvas.configure(yscrollcommand=self.detail_scroll.set)

        self._scroll_state = {"visible": False}

        def _sync_detail_scrollbar():
            if not self.detail_canvas.winfo_exists() or not self.detail_canvas.winfo_ismapped():
                if self._scroll_state["visible"]:
                    self.detail_scroll.pack_forget()
                    self._scroll_state["visible"] = False
                return
            bbox = self.detail_canvas.bbox("all")
            content_height = (bbox[3] - bbox[1]) if bbox else 0
            view_height = self.detail_canvas.winfo_height()
            needs_scroll = content_height > view_height + 2
            if needs_scroll and not self._scroll_state["visible"]:
                self.detail_scroll.pack(side=tk.RIGHT, fill=tk.Y)
                self._scroll_state["visible"] = True
            elif not needs_scroll and self._scroll_state["visible"]:
                self.detail_scroll.pack_forget()
                self.detail_canvas.yview_moveto(0)
                self._scroll_state["visible"] = False

        self._sync_detail_scrollbar = _sync_detail_scrollbar

        def _on_canvas_configure(event):
            self.detail_canvas.itemconfigure(self._content_window, width=event.width)
            self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all"))
            _sync_detail_scrollbar()

            pad = 30
            for lbl in self._wrapping_labels:
                lbl.configure(wraplength=max(event.width - pad, 100))

        def _on_content_configure(event=None):
            self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all"))
            _sync_detail_scrollbar()

        def _on_mousewheel(event):
            if self._scroll_state["visible"]:
                if event.delta:
                    self.detail_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                elif event.num == 4:
                    self.detail_canvas.yview_scroll(-1, "units")
                elif event.num == 5:
                    self.detail_canvas.yview_scroll(1, "units")

        self.detail_canvas.bind("<Configure>", _on_canvas_configure)
        self.detail_canvas.bind("<MouseWheel>", _on_mousewheel)
        self.detail_canvas.bind("<Button-4>", _on_mousewheel)
        self.detail_canvas.bind("<Button-5>", _on_mousewheel)

        # Detail content
        self._detail_content = tk.Frame(self.detail_canvas, bg=Theme.BG_DARKEST)
        self._content_window = self.detail_canvas.create_window((0, 0), window=self._detail_content, anchor="nw")
        self._detail_content.bind("<Configure>", _on_content_configure)
        self._detail_content.bind("<MouseWheel>", _on_mousewheel)
        self._detail_content.bind("<Button-4>", _on_mousewheel)
        self._detail_content.bind("<Button-5>", _on_mousewheel)

        # Let the detail builder populate the content frame
        self._detail_builder(self)

    def populate(self, items: dict[str, dict]):
        if self.all_items is items:
            return
        self.all_items = items
        self.sorted_names = []
        self.name_tuples = []
        self.filtered_names = []
        if hasattr(self, "detail_canvas"):
            self.detail_canvas.pack_forget()
        if hasattr(self, "detail_scroll"):
            self.detail_scroll.pack_forget()
            self._scroll_state["visible"] = False
        self.lbl_placeholder.pack(fill="both", expand=True, padx=10, pady=10)
        self._execute_search()

    def _display_item_name(self, name: str) -> str:
        inst_map = getattr(self.app, "_installed_map", None)
        if inst_map and name.lower() in inst_map:
            inst = inst_map[name.lower()]
            return f"⬆ {name}" if inst.get("update_available") else f"{'↓' if self is self.app.board_tab else '✔'} {name}"
        return name

    def refresh_listbox_labels(self):
        """Relabel in bounded batches without resetting scroll or selection."""
        if not hasattr(self, "listbox") or not self.listbox.winfo_exists():
            return
        revision = self._search_revision
        def relabel(start=0):
            self._label_after_id = None
            if revision != self._search_revision or not self.listbox.winfo_exists():
                return
            end = min(start + 250, self.loaded_count)
            existing = self.listbox.get(start, end - 1) if end > start else ()
            for offset, old in enumerate(existing):
                index = start + offset
                label = self._display_item_name(self.filtered_names[index])
                if label != old:
                    selected = index in self.listbox.curselection()
                    self.listbox.delete(index)
                    self.listbox.insert(index, label)
                    if selected:
                        self.listbox.selection_set(index)
            if end < self.loaded_count:
                self._label_after_id = self.app.root.after(16, lambda: relabel(end))
        if self._label_after_id is not None:
            self.app.root.after_cancel(self._label_after_id)
            self._label_after_id = None
        relabel()

    def _populate_listbox(self):
        _load_more_after_id = getattr(self, "_load_more_after_id", None)
        if _load_more_after_id is not None:
            self.app.root.after_cancel(str(_load_more_after_id))
            self._load_more_after_id = None
        self._loading_more = False

        self.listbox.delete(0, tk.END)
        total = len(self.filtered_names)
        self.loaded_count = min(250, total)
        labels = [self._display_item_name(n) for n in self.filtered_names[:self.loaded_count]]
        if total > 250:
            self.lbl_search_status.config(text=f"Showing top {self.loaded_count} of {total} matches")
            if labels:
                self.listbox.insert(tk.END, *labels)
        else:
            if total == 0:
                if not getattr(self.app, "_is_online", True) and not self.all_items:
                    self.lbl_search_status.config(text="⚠ Offline Mode: Connect to internet to browse catalog")
                else:
                    self.lbl_search_status.config(text="No matches found")
            else:
                self.lbl_search_status.config(text=f"Found {total} matches")
            if labels:
                self.listbox.insert(tk.END, *labels)

    def _on_scroll(self, first, last):
        self.list_scroll.set(first, last)
        if float(last) >= 0.99 and self.loaded_count < len(self.filtered_names):
            if not getattr(self, "_loading_more", False) and getattr(self, "_load_more_after_id", None) is None:
                self.lbl_search_status.config(text="Loading more...")
                self._load_more_after_id = self.app.root.after(400, self._load_more_items)

    def _load_more_items(self):
        self._load_more_after_id = None
        self._loading_more = True
        try:
            total = len(self.filtered_names)
            next_count = min(self.loaded_count + 250, total)
            if next_count > self.loaded_count:
                items_to_add = [self._display_item_name(n) for n in self.filtered_names[self.loaded_count:next_count]]
                self.listbox.insert(tk.END, *items_to_add)
                self.loaded_count = next_count
                self.lbl_search_status.config(text=f"Showing top {self.loaded_count} of {total} matches")
        finally:
            self._loading_more = False

    def _on_search(self, event=None):
        if self._search_after_id is not None:
            self.app.root.after_cancel(self._search_after_id)
        self._search_after_id = self.app.root.after(150, self._execute_search)

    def _execute_search(self):
        if self._search_after_id is not None:
            self.app.root.after_cancel(self._search_after_id)
        self._search_after_id = None
        self._search_revision += 1
        revision, items = self._search_revision, self.all_items
        query = self.search_var.get().strip().casefold()
        def completed(names, error):
            if revision != self._search_revision or items is not self.all_items:
                return
            if error:
                self.lbl_search_status.config(text=f"Search unavailable: {error}")
                return
            if names is not None:
                self.filtered_names = names
                self._populate_listbox()
        if len(items) <= 1000:
            from src.modules.browser_loading import search_catalog
            self._search_worker.cancel()
            completed(search_catalog((items, query), threading.Event()), None)
        else:
            self.lbl_search_status.config(text="Searching…")
            self._search_worker.submit((items, query), completed)

    def _on_select(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.filtered_names):
            return
        name = self.filtered_names[idx]
        item = self.all_items[name]

        # Show detail canvas, hide placeholder
        self.lbl_placeholder.pack_forget()
        if not self.detail_canvas.winfo_ismapped():
            self.detail_canvas.pack(side=tk.LEFT, fill="both", expand=True)

        self._on_select_handler(self, item)
        self.detail_canvas.yview_moveto(0)
        self._sync_detail_scrollbar()


# ---------------------------------------------------------------------------
# Installed Tab Class
# ---------------------------------------------------------------------------

class InstalledTab:
    """A tab that shows all locally installed libraries / board platforms
    and indicates available updates."""

    @staticmethod
    def _item_icon(item: dict) -> str:
        """Return the Installed-tab icon for a library or board platform."""
        return "📖" if str(item.get("type", "")).casefold() == "library" else "🔌"

    def _display_item_name(self, item: dict) -> str:
        """Decorate a displayed name without changing its searchable value."""
        return f"{self._item_icon(item)} {item['name']}"

    def __init__(self, parent: ttk.Frame, app: "ArduinoBrowser"):
        self.app = app
        self.installed_items: list[dict] = []   # list of installed info dicts
        self.filtered_items: list[dict] = []
        self._search_after_id = None
        self._wrapping_labels = []
        self._all_examples = []
        self._all_boards = []
        self._select_req_id: int = 0
        self._loading_complete_req_id: int = 0
        self._animation_after_id = None
        from src.modules.browser_loading import LatestScan, scan_package_details
        self._detail_worker = LatestScan(app._tasks, scan_package_details)

        self._build(parent)
        def cancel_callbacks(event):
            if event.widget is parent:
                self._select_req_id += 1
                self._detail_worker.cancel()
                self._stop_loading_animation()
                if self._search_after_id is not None:
                    self.app.root.after_cancel(self._search_after_id)
                    self._search_after_id = None
        parent.bind('<Destroy>', cancel_callbacks, add='+')

    def _build(self, parent: ttk.Frame):
        # Search bar
        top = tk.Frame(parent, bg=Theme.BG_DARKEST, pady=4)
        top.pack(fill="x")

        tk.Label(top, text="Search:", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(side="left")
        # Reserve the count before the expanding entry so it stays readable.
        self.lbl_search_status = tk.Label(top, text="0 installed", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST)
        self.lbl_search_status.pack(side="right", padx=(8, 0))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            top, textvariable=self.search_var,
            width=40, font=("Montserrat", 10),
            bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN,
            highlightbackground=Theme.BORDER
        )
        self.search_entry.pack(side="left", padx=(8, 10), fill="x", expand=True)
        self.search_entry.bind("<KeyRelease>", self._on_search)

        # Paned window: list | detail
        pane = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=Theme.BORDER, sashwidth=2, sashrelief=tk.FLAT, bd=0)
        pane.pack(fill="both", expand=True, pady=(4, 4))

        # --- Left: item list ---
        from src.modules.tk_glass import GlassCard
        left_card = GlassCard(pane, Theme, padding=7, expand=True)
        pane.add(left_card, minsize=200)
        left = left_card.body

        self.listbox = tk.Listbox(
            left, font=("Consolas", 10),
            bg=Theme.BG_MID, fg=Theme.TEXT_BRIGHT,
            selectbackground=Theme.CYAN_DIM, selectforeground="#ffffff",
            highlightcolor=Theme.CYAN, highlightbackground=Theme.BORDER,
            borderwidth=1, relief=tk.FLAT,
            activestyle="none",
            exportselection=False
        )
        self.list_scroll = ttk.Scrollbar(left, orient="vertical", style="Vertical.TScrollbar",
                                         command=self.listbox.yview)
        self.listbox.config(yscrollcommand=self.list_scroll.set)
        self.list_scroll.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # --- Right: detail panel ---
        detail_card = GlassCard(pane, Theme, padding=9, expand=True)
        pane.add(detail_card, minsize=350)
        self.detail_frame = detail_card.body
        self.detail_frame.configure(bg=Theme.BG_DARKEST)

        # Placeholder
        self.lbl_placeholder = tk.Label(
            self.detail_frame, text="No Local Item Selected",
            font=("Montserrat", 13), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="center"
        )
        self.lbl_placeholder.pack(fill="both", expand=True, padx=10, pady=10)
        from src.modules.tk_glass import ResponsivePanes
        self._responsive_panes = ResponsivePanes(pane, left_card, detail_card)

        # Metadata and example controls remain reachable on short detail panes.
        from src.modules.tk_glass import ScrollBody
        self._details_view = ScrollBody(self.detail_frame, Theme.BG_DARKEST)
        self._detail_content = self._details_view.body

        def _on_detail_configure(event):
            pad = 30
            for lbl in self._wrapping_labels:
                try:
                    lbl.configure(wraplength=max(event.width - pad, 100))
                except Exception:
                    pass

        self.detail_frame.bind("<Configure>", _on_detail_configure)

        # Top Metadata Section (fixed natural height)
        self._top_info = tk.Frame(self._detail_content, bg=Theme.BG_DARKEST)
        self._top_info.pack(fill="x", expand=False)

        # Labels inside detail content
        self.lbl_name = tk.Label(self._top_info, text="", font=("Montserrat", 14, "bold"), fg=Theme.CYAN, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_name.pack(anchor="w", fill="x", pady=(0, 4))
        self._wrapping_labels.append(self.lbl_name)

        self.lbl_type = tk.Label(self._top_info, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_type.pack(anchor="w", fill="x")
        self._wrapping_labels.append(self.lbl_type)

        # Version info frame
        ver_frame = tk.Frame(self._top_info, bg=Theme.BG_DARKEST)
        ver_frame.pack(anchor="w", fill="x", pady=4)

        self.lbl_installed_ver = tk.Label(ver_frame, text="", font=("Montserrat", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_installed_ver.pack(anchor="w")
        self.lbl_latest_ver = tk.Label(ver_frame, text="", font=("Montserrat", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_latest_ver.pack(anchor="w")
        self.lbl_update_status = tk.Label(ver_frame, text="", font=("Montserrat", 9, "bold"), bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_update_status.pack(anchor="w", pady=(4, 0))

        self.lbl_size = tk.Label(self._top_info, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_size.pack(anchor="w", fill="x")
        self._wrapping_labels.append(self.lbl_size)

        sep = tk.Frame(self._top_info, bg=Theme.BORDER, height=1)
        sep.pack(fill="x", pady=5)

        self.lbl_path_header = tk.Label(self._top_info, text="Location on Disk:", font=("Montserrat", 9, "bold"), fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_path_header.pack(anchor="w", fill="x")

        self.lbl_path = tk.Label(self._top_info, text="", font=("Consolas", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST, anchor="w", justify="left")
        self.lbl_path.pack(anchor="w", fill="x", pady=(1, 4))
        self._wrapping_labels.append(self.lbl_path)

        sep2 = tk.Frame(self._top_info, bg=Theme.BORDER, height=1)
        sep2.pack(fill="x", pady=5)

        # Action Buttons frame
        btn_frame = tk.Frame(self._top_info, bg=Theme.BG_DARKEST)
        btn_frame.pack(anchor="w", fill="x", pady=2)

        self.open_btn = make_flat_button(
            btn_frame, "📂 Open Folder", self._open_folder,
            Theme.BTN_MONITOR, Theme.BTN_MONITOR_H
        )
        self.open_btn.pack(side="left", padx=(0, 10))

        self.update_btn = make_flat_button(
            btn_frame, "⬇ Update", self._update_item,
            Theme.BTN_COMPILE, Theme.BTN_COMPILE_H
        )
        self.update_btn.pack(side="left", padx=(0, 10))

        self.delete_btn = make_flat_button(
            btn_frame, "❌ Delete", self._delete_item,
            Theme.BTN_STOP, Theme.BTN_STOP_H
        )
        self.delete_btn.pack(side="left")
        self._set_update_btn_state(False)

        # --- Sample Codes (Examples) section --- (expands vertically to fill remaining space)
        self._examples_section = tk.Frame(self._detail_content, bg=Theme.BG_DARKEST)
        self._examples_section.pack(fill="both", expand=True, pady=(2, 0))

        sep3 = tk.Frame(self._examples_section, bg=Theme.BORDER, height=1)
        sep3.pack(fill="x", pady=5)

        self.lbl_examples_header = tk.Label(
            self._examples_section, text="Sample Codes (Examples):",
            font=("Montserrat", 9, "bold"), fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST, anchor="w"
        )
        self.lbl_examples_header.pack(anchor="w", fill="x")

        self.lbl_examples_hint = tk.Label(
            self._examples_section, text="Double-click a sketch to view its code",
            font=("Montserrat", 8), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w"
        )
        self.lbl_examples_hint.pack(anchor="w", fill="x", pady=(0, 4))

        # Filter entry for examples / boards
        examples_search_wrap = tk.Frame(self._examples_section, bg=Theme.BG_DARKEST)
        examples_search_wrap.pack(anchor="w", fill="x", pady=(0, 4))

        tk.Label(
            examples_search_wrap, text="🔍 Filter:",
            font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
        ).pack(side="left", padx=(0, 4))

        self.examples_search_var = tk.StringVar()
        self.examples_search_var.trace_add("write", self._on_examples_search_change)

        self.examples_search_entry = tk.Entry(
            examples_search_wrap, textvariable=self.examples_search_var,
            font=("Montserrat", 9), bg=Theme.BG_MID, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN,
            highlightbackground=Theme.BORDER
        )
        self.examples_search_entry.pack(side="left", fill="x", expand=True)

        examples_wrap = tk.Frame(self._examples_section, bg=Theme.BG_DARKEST)
        examples_wrap.pack(anchor="w", fill="both", expand=True, pady=(4, 0))

        self.examples_listbox = tk.Listbox(
            examples_wrap, font=("Consolas", 9),
            bg=Theme.BG_MID, fg=Theme.TEXT_BRIGHT,
            selectbackground=Theme.CYAN_DIM, selectforeground="#ffffff",
            highlightcolor=Theme.CYAN, highlightbackground=Theme.BORDER,
            borderwidth=1, relief=tk.FLAT, activestyle="none", exportselection=False
        )
        self.examples_scroll = ttk.Scrollbar(
            examples_wrap, orient="vertical", style="Vertical.TScrollbar",
            command=self.examples_listbox.yview
        )
        self.examples_listbox.config(yscrollcommand=self.examples_scroll.set)
        self.examples_scroll.pack(side="right", fill="y")
        self.examples_listbox.pack(side="left", fill="both", expand=True)
        self.examples_listbox.bind("<Double-Button-1>", self._open_example)

        self._current_examples = []  # list of full paths, parallel to listbox rows

    def _find_examples(self, base_path: str):
        """Recursively find .ino/.pde sample sketches under any 'examples'
        folder inside base_path."""
        found = []
        try:
            for root, dirs, files in os.walk(base_path):
                rel = os.path.relpath(root, base_path)
                rel_parts = [] if rel == "." else rel.lower().split(os.sep)
                if "examples" in rel_parts:
                    for f in files:
                        if f.lower().endswith((".ino", ".pde")):
                            found.append(os.path.join(root, f))
        except Exception:
            pass
        found.sort(key=lambda p: os.path.basename(p).lower())
        return found

    def _find_boards(self, base_path: str) -> list[str]:
        boards = []
        try:
            from pathlib import Path
            for p in Path(base_path).glob("**/boards.txt"):
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                    for line in content.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if ".name=" in line:
                            parts = line.split(".name=", 1)
                            if len(parts) == 2:
                                board_id = parts[0].strip()
                                if "." in board_id:
                                    continue
                                display_name = parts[1].strip()
                                if display_name and display_name not in boards:
                                    boards.append(display_name)
                except Exception:
                    pass
        except Exception:
            pass
        boards.sort(key=str.lower)
        return boards

    def _clear_examples(self, message: str = "(no sample sketches found)"):
        self._current_examples = []
        self.examples_listbox.config(state=tk.NORMAL)
        self.examples_listbox.delete(0, tk.END)
        self.examples_listbox.insert(tk.END, message)
        self.examples_listbox.config(state=tk.DISABLED)

    _SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def _start_loading_animation(self, req_id: int, frame_idx: int = 0):
        self._animation_after_id = None
        if getattr(self, "_select_req_id", 0) != req_id:
            return
        if getattr(self, "_loading_complete_req_id", None) == req_id:
            return

        frame = self._SPINNER_FRAMES[frame_idx % len(self._SPINNER_FRAMES)]
        try:
            current_text = self.lbl_size["text"]
            if "Calculating" in current_text:
                self.lbl_size.config(text=f"Size on Disk: Calculating {frame}...")
        except Exception:
            pass

        next_idx = (frame_idx + 1) % len(self._SPINNER_FRAMES)
        try:
            self._animation_after_id = self.app.root.after(180, lambda: self._start_loading_animation(req_id, next_idx))
        except Exception:
            pass

    def _load_details_async_worker(self, item: dict, req_id: int):
        """Submit one latest scan rather than creating a thread per selection."""
        def completed(result, error):
            if self._select_req_id != req_id:
                return
            self._loading_complete_req_id = req_id
            self._stop_loading_animation()
            if error or result is None:
                self.lbl_size.config(text='Size on Disk: unavailable')
                self._clear_examples(f'(scan unavailable: {error or "cancelled"})')
                return
            size, self._all_examples, self._all_boards = result
            label = f'{size / (1024 * 1024):.1f} MB' if size > 1024 * 1024 else f'{size / 1024:.0f} KB'
            self.lbl_size.config(text=f'Size on Disk: {label}')
            self._filter_and_render_list(item_override=item)
        self._detail_worker.submit(item, completed)

    def _on_examples_search_change(self, *args):
        if self._loading_complete_req_id != self._select_req_id:
            return
        self._filter_and_render_list()

    def _filter_and_render_list(self, item_override=None):
        query = self.examples_search_var.get().lower().strip()
        self.examples_listbox.config(state=tk.NORMAL)
        self.examples_listbox.delete(0, tk.END)

        item = item_override
        if not item:
            sel = self.listbox.curselection()
            if sel and sel[0] < len(self.filtered_items):
                item = self.filtered_items[sel[0]]

        item_type = item["type"] if item else ("Library" if getattr(self, "_all_examples", None) else "Board Platform")

        if item_type == "Library":
            self._current_examples = []
            labels = []
            if not getattr(self, "_all_examples", None):
                self.examples_listbox.insert(tk.END, "(no sample sketches found)")
                self.examples_listbox.config(state=tk.DISABLED)
                return

            for path in self._all_examples:
                sketch_folder = os.path.basename(os.path.dirname(path))
                display_name = f"{sketch_folder} / {os.path.basename(path)}"
                if not query or query in display_name.lower():
                    labels.append(display_name)
                    self._current_examples.append(path)

            if labels:
                self.examples_listbox.insert(tk.END, *labels)

            if not self._current_examples:
                self.examples_listbox.insert(tk.END, "(no matching sample sketches found)")
                self.examples_listbox.config(state=tk.DISABLED)
        else:
            self._current_examples = []
            if not getattr(self, "_all_boards", None):
                self.examples_listbox.insert(tk.END, "(no board definitions found)")
                self.examples_listbox.config(state=tk.DISABLED)
                return

            labels = [f"  •  {board}" for board in self._all_boards if not query or query in board.lower()]
            if labels:
                self.examples_listbox.insert(tk.END, *labels)
            else:
                self.examples_listbox.insert(tk.END, "(no matching board definitions found)")
                self.examples_listbox.config(state=tk.DISABLED)

    def _populate_boards(self, base_path: str):
        self._all_boards = self._find_boards(base_path)
        self._filter_and_render_list()

    def _populate_examples(self, base_path: str):
        self._all_examples = self._find_examples(base_path)
        self._filter_and_render_list()

    def _open_example(self, event=None):
        sel = self.examples_listbox.curselection()
        if not sel or not self._current_examples:
            return
        idx = sel[0]
        if idx >= len(self._current_examples):
            return
        path = self._current_examples[idx]
        if not os.path.exists(path):
            messagebox.showerror("Error", "Sample file no longer exists on disk.")
            return
        paths = tuple(self._current_examples)
        def is_current():
            selection = self.examples_listbox.curselection()
            return (tuple(self._current_examples) == paths and bool(selection)
                    and selection[0] < len(paths) and paths[selection[0]] == path)
        _open_code_viewer(path, paths, parent=self.app.root, is_current=is_current)

    def _get_dir_size(self, path: str) -> int:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        try:
            for root, dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.exists(fp):
                        total += os.path.getsize(fp)
        except Exception:
            pass
        return total

    def populate(self, installed_items: list[dict]):
        """Replace the internal installed items list and refresh UI."""
        if self.installed_items is installed_items:
            return
        self.installed_items = installed_items
        self._execute_search()

    def _on_search(self, event=None):
        if self._search_after_id is not None:
            self.app.root.after_cancel(self._search_after_id)
        self._search_after_id = self.app.root.after(150, self._execute_search)

    def _execute_search(self):
        self._search_after_id = None
        self._select_req_id += 1
        self._detail_worker.cancel()
        self._stop_loading_animation()
        query = self.search_var.get().lower().strip()
        selected_path = None
        sel = self.listbox.curselection()
        if sel and sel[0] < len(self.filtered_items):
            selected_path = self.filtered_items[sel[0]]["path"]

        if not query:
            self.filtered_items = list(self.installed_items)
        else:
            starts = []
            contains = []
            for item in self.installed_items:
                name_lower = item["name"].lower()
                if name_lower.startswith(query):
                    starts.append(item)
                elif query in name_lower or query in item.get("type", "").lower():
                    contains.append(item)
            self.filtered_items = starts + contains

        self.lbl_search_status.config(text=f"{len(self.filtered_items)} installed")

        self.listbox.delete(0, tk.END)
        labels = [f"{'⬆ ' if item.get('update_available') else ''}{self._display_item_name(item)} ({item['installed_version']})"
                  for item in self.filtered_items]
        if labels:
            self.listbox.insert(tk.END, *labels)

        if selected_path:
            for i, item in enumerate(self.filtered_items):
                if item["path"] == selected_path:
                    self.listbox.selection_set(i)
                    self.listbox.see(i)
                    self._on_select()
                    return

        # Hide detail if nothing
        if hasattr(self, "_details_view"):
            self._details_view.pack_forget()
        self.lbl_placeholder.pack(fill="both", expand=True, padx=10, pady=10)
        self._clear_examples()
        self._set_update_btn_state(False)

    def _set_update_btn_state(self, enabled: bool):
        """Make the update button unclickable and visually disabled when no update is available."""
        if not hasattr(self, "update_btn"):
            return
        if enabled:
            self.update_btn.config(
                state="normal",
                cursor="hand2",
                bg=Theme.BTN_COMPILE,
                activebackground=Theme.BTN_COMPILE_H,
                fg=Theme.TEXT_BRIGHT,
                text="⬇ Update",
                command=self._update_item,
            )
        else:
            self.update_btn.config(
                state="disabled",
                cursor="arrow",
                bg=Theme.BG_MID,
                activebackground=Theme.BG_MID,
                fg=Theme.TEXT_DIM,
                text="⬇ Update",
                command=self._update_item,
            )

    def refresh_update_button_state(self):
        """Refresh the Update button state according to the currently selected item."""
        sel = self.listbox.curselection()
        if not sel or sel[0] >= len(self.filtered_items):
            self._set_update_btn_state(False)
            return
        item = self.filtered_items[sel[0]]
        self._set_update_btn_state(bool(item.get("update_available")))

    def _on_select(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            self._set_update_btn_state(False)
            return
        idx = sel[0]
        if idx >= len(self.filtered_items):
            self._set_update_btn_state(False)
            return
        item = self.filtered_items[idx]
        self._stop_loading_animation()
        # Invalidate earlier samples before changing traced filter variables.
        self._select_req_id += 1
        req_id = self._select_req_id

        if hasattr(self, "examples_search_var"):
            self.examples_search_var.set("")

        self.lbl_placeholder.pack_forget()
        if not self._details_view.winfo_ismapped():
            self._details_view.pack(fill="both", expand=True, padx=10, pady=8)

        self.lbl_name.config(text=self._display_item_name(item))
        self.lbl_type.config(text=f"Type: {item['type']}")

        self.lbl_installed_ver.config(text=f"Installed version: {item['installed_version']}")
        self.lbl_latest_ver.config(text=f"Latest version:    {item['latest_version']}")

        if item.get("update_available"):
            self.lbl_update_status.config(text="⬆ Update available", fg=Theme.YELLOW)
            self._set_update_btn_state(True)
        elif item.get("latest_version") in ("—", "— (Offline)", item.get("installed_version")):
            self.lbl_update_status.config(text="✓ Installed", fg=Theme.GREEN)
            self._set_update_btn_state(False)
        else:
            self.lbl_update_status.config(text="✓ Up‑to‑date", fg=Theme.GREEN)
            self._set_update_btn_state(False)

        self.lbl_path.config(text=item["path"])

        if item["type"] == "Library":
            self.lbl_examples_header.config(text="Sample Codes (Examples):")
            self.lbl_examples_hint.config(text="Double-click a sketch to view its code")
        else:
            self.lbl_examples_header.config(text="Available Boards:")
            self.lbl_examples_hint.config(text="Supported microcontrollers inside this downloaded platform package")

        # Show immediate loading state with animated spinner
        self.lbl_size.config(text="Size on Disk: Calculating ⠋...")
        self.examples_listbox.config(state=tk.NORMAL)
        self.examples_listbox.delete(0, tk.END)
        self.examples_listbox.insert(tk.END, "  ⏳ Scanning disk content in background...")
        self.examples_listbox.config(state=tk.DISABLED)

        # Start loading animation spinner
        self._start_loading_animation(req_id)

        # Reuse one cancellable worker for the latest detail selection.
        self._load_details_async_worker(item, req_id)

    def _stop_loading_animation(self):
        if self._animation_after_id is not None:
            self.app.root.after_cancel(self._animation_after_id)
            self._animation_after_id = None

    def _open_folder(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        item = self.filtered_items[sel[0]]
        path = item["path"]
        if os.path.exists(path):
            if sys.platform == "win32":
                os.startfile(path)
            else:
                webbrowser.open("file://" + path)
        else:
            messagebox.showerror("Error", "Folder does not exist or has been deleted outside this browser.")
            self.app._compute_installed_items_async()  # refresh
            self.populate(self.app._installed_items)

    def _update_item(self):
        sel = self.listbox.curselection()
        if not sel:
            self._set_update_btn_state(False)
            return
        item = self.filtered_items[sel[0]]
        if not item.get("update_available"):
            self._set_update_btn_state(False)
            return
        is_board = item["type"] == "Board Platform"
        old_path = item.get("path", "")
        old_archive = item.get("archive", "")
        self.app._download_update(item["name"], is_board, old_path, old_archive)

    def _delete_item(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        item = self.filtered_items[sel[0]]
        path = item["path"]
        name = item["name"]
        item_type = item["type"]

        confirm = messagebox.askyesno(
            "Confirm Delete",
            f"Are you sure you want to permanently delete the {item_type.lower()} '{name}' "
            f"(version {item['installed_version']})?\n\nPath: {path}",
            icon="warning",
            parent=self.app.root
        )
        if not confirm:
            return

        import shutil
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.isfile(path):
                os.remove(path)

            messagebox.showinfo("Deleted", f"Successfully deleted '{name}'.")
            # Recompute and refresh everything
            self.app._compute_installed_items_async()
            self.populate(self.app._installed_items)
            self.app._update_version_status(self.app.lib_tab)
            self.app._update_version_status(self.app.board_tab)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to delete item:\n{e}")


# ---------------------------------------------------------------------------
# Application class
# ---------------------------------------------------------------------------

class ArduinoBrowser:
    """Main application window with Libraries, Boards, and Installed tabs."""

    def __init__(self, *, linux_channel=None):
        # DPI awareness on Windows
        if sys.platform == "win32":
            try:
                from ctypes import windll
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        self.root = tk.Tk()
        self._linux_channel = linux_channel
        self.root.title("Arduino Library & Board Browser")

        # Set AppUserModelID so Windows taskbar groups it with the main MCU Flasher window
        if sys.platform == "win32":
            try:
                import ctypes
                myappid = 'Naph.MCUFlasher.GUI.V6'
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
            except Exception:
                pass
            try:
                icon_path = os.path.join(SCRIPT_DIR, "src", "assets", "mcu_icon.ico")
                if not os.path.exists(icon_path):
                    icon_path = os.path.join(SCRIPT_DIR, "src", "mcu_icon.ico")
                if os.path.exists(icon_path):
                    self.root.iconbitmap(default=icon_path)
                    self.root.iconbitmap(icon_path)
            except Exception:
                pass

        # Tk uses native screen units; its fonts independently follow point scaling.
        work_w, work_h = 1280, 720
        start_x, start_y = 0, 0
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class _WorkAreaRect(ctypes.Structure):
                    _fields_ = [
                        ('left', wintypes.LONG),
                        ('top', wintypes.LONG),
                        ('right', wintypes.LONG),
                        ('bottom', wintypes.LONG)
                    ]

                rect = _WorkAreaRect()
                SPI_GETWORKAREA = 0x0030
                if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                    work_w = max(1, rect.right - rect.left)
                    work_h = max(1, rect.bottom - rect.top)
                    start_x = rect.left
                    start_y = rect.top
                else:
                    work_w = max(1, self.root.winfo_screenwidth())
                    work_h = max(1, self.root.winfo_screenheight())
            except Exception:
                work_w = max(1, self.root.winfo_screenwidth())
                work_h = max(1, self.root.winfo_screenheight())
        else:
            work_w = max(1, self.root.winfo_screenwidth())
            work_h = max(1, self.root.winfo_screenheight())

        from src.modules.tk_glass import ui_scale
        scale = ui_scale(self.root)
        target_w = min(round(1040 * scale), int(work_w * .88))
        target_h = min(round(740 * scale), int(work_h * .88))
        # Ensure target does not exceed usable work area
        target_w = min(target_w, max(1, work_w - 24))
        target_h = min(target_h, max(1, work_h - 48))

        pos_x = start_x + (work_w - target_w) // 2
        pos_y = start_y + (work_h - target_h) // 2

        self.root.geometry(f"{target_w}x{target_h}+{pos_x}+{pos_y}")

        # Lists and details stack vertically below the horizontal content budget.
        min_w = min(target_w, round(480 * scale))
        min_h = min(target_h, round(400 * scale))
        self.root.minsize(min_w, min_h)

        self._busy = False
        self._active_download_tab = None
        self._downloading_item_name = None
        self._cancel_event = threading.Event()
        self._keep_alive = True
        self._is_hidden = False

        # Load persisted download directory
        settings = _load_settings()
        saved_dir = settings.get("download_dir", "")
        self._additional_board_urls = parse_additional_board_urls(
            settings.get("additional_board_urls", [])
        )
        associations = settings.get("board_platformio_associations", {})
        self._board_platformio_associations = associations if isinstance(associations, dict) else {}
        if saved_dir and os.path.isdir(saved_dir):
            self._download_dir = saved_dir
        else:
            self._download_dir = DEFAULT_DOWNLOAD_DIR
        os.makedirs(self._download_dir, exist_ok=True)
        os.makedirs(INDEX_CACHE_DIR, exist_ok=True)

        # Intercept window close event to hide window instead of destroying process (Sleep Mode)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Save window HWND for instant Win32 unhide
        if sys.platform == "win32":
            try:
                hwnd_file = os.path.join(INDEX_CACHE_DIR, ".dm_hwnd")
                with open(hwnd_file, "w", encoding="utf-8") as f:
                    f.write(str(self.root.winfo_id()))
            except Exception:
                pass

        # Clean up any stale trigger files left over from prior abnormal shutdowns
        for stale_name in ((".dm_force_exit", ".show_dm_trigger") if sys.platform == "win32" else ()):
            stale_file = os.path.join(INDEX_CACHE_DIR, stale_name)
            if os.path.exists(stale_file):
                try:
                    os.remove(stale_file)
                except Exception:
                    pass

        # Installed items cache (computed from disk + indexes)
        self._is_online: bool = True
        self._installed_items: list[dict] = []

        self._build_ui()
        self.root.after(100, self._initial_load)
        self.root.after(250, self._check_show_trigger)

    def _on_window_close(self):
        """Sleep Mode: Hide window on close to keep loaded indexes in RAM."""
        if getattr(self, "_keep_alive", True):
            self.root.withdraw()
            self._is_hidden = True
        else:
            self._force_exit()

    def _check_show_trigger(self):
        """Poll for wake-up or force-exit trigger file sent by main MCU Flasher GUI."""
        if sys.platform.startswith("linux"):
            channel = getattr(self, "_linux_channel", None)
            for command in channel.poll() if channel is not None else ():
                if command == "quit":
                    self._force_exit()
                    return
                self._unhide_window()
            self.root.after(150, self._check_show_trigger)
            return
        # If the main app is shutting down, it writes a force-exit trigger so
        # this sleeping Download Manager process doesn't remain as an orphan.
        force_exit_file = os.path.join(INDEX_CACHE_DIR, ".dm_force_exit")
        if os.path.exists(force_exit_file):
            try:
                os.remove(force_exit_file)
            except Exception:
                pass
            self._force_exit()
            return

        trigger_file = os.path.join(INDEX_CACHE_DIR, ".show_dm_trigger")
        if os.path.exists(trigger_file):
            try:
                os.remove(trigger_file)
            except Exception:
                pass
            self._unhide_window()
        self.root.after(1000 if self._is_hidden else 500, self._check_show_trigger)

    def _unhide_window(self):
        """Instantly restore window from memory without reloading JSON indexes."""
        self._apply_current_theme()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._is_hidden = False
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = self.root.winfo_id()
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE / SW_SHOW
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        self._wake_sync()

    def _apply_current_theme(self):
        """Recolor a sleeping downloader without losing search or download state."""
        from src.modules.tk_glass import readable_foreground
        mode = _resolve_lib_req_theme()
        old = Theme.PALETTES[getattr(self, "_rendered_theme", Theme.active_theme)]
        Theme.apply_theme(mode)
        new = Theme.PALETTES[Theme.active_theme]
        self._rendered_theme = Theme.active_theme
        if old is new:
            return
        colors = {}
        for key, color in old.items():
            if key in new:
                colors.setdefault(color, new[key])

        def recolor(widget):
            for option in ("background", "foreground", "activebackground", "activeforeground",
                           "disabledforeground", "insertbackground", "highlightbackground",
                           "highlightcolor", "selectbackground", "selectforeground"):
                try:
                    value = str(widget.cget(option))
                    if value in colors:
                        widget.configure(**{option: colors[value]})
                except tk.TclError:
                    pass  # ttk delegates these colors to named styles below.
            if hasattr(widget, "_normal_bg"):
                token = getattr(widget, "_bg_token", None)
                widget._normal_bg = new.get(token, colors.get(widget._normal_bg, widget._normal_bg))
                widget._hover_bg = new.get(token + "_H" if token else "", colors.get(widget._hover_bg, widget._hover_bg))
                widget.configure(bg=widget._normal_bg, fg=readable_foreground(widget._normal_bg),
                                 activebackground=widget._hover_bg, activeforeground=readable_foreground(widget._hover_bg))
            if hasattr(widget, "_schedule"):
                widget._schedule()
            for child in widget.winfo_children():
                recolor(child)

        recolor(self.root)
        self._configure_styles()

    def _wake_sync(self):
        self._compute_installed_items_async()

    def _force_exit(self):
        """Permanently close process and purge memory."""
        if sys.platform.startswith("linux"):
            self._cancel_event.set()
            channel = getattr(self, "_linux_channel", None)
            if channel is not None:
                channel.close()
                self._linux_channel = None
            from src.modules.ubuntu_download_manager import close_viewers
            close_viewers()
        try:
            self.root.withdraw()
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                hwnd_file = os.path.join(INDEX_CACHE_DIR, ".dm_hwnd")
                if os.path.exists(hwnd_file):
                    os.remove(hwnd_file)
            except Exception:
                pass
        self.root.destroy()
        sys.exit(0)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _configure_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Configure frames and general layouts
        style.configure("TFrame", background=Theme.BG_DARKEST)
        style.configure("TLabel", background=Theme.BG_DARKEST, foreground=Theme.TEXT)
        style.configure("TNotebook", background=Theme.BG_DARKEST, borderwidth=0, tabmargins=(0, 0, 0, 8),
                        lightcolor=Theme.BORDER, darkcolor=Theme.BORDER, bordercolor=Theme.BORDER)
        style.configure("TNotebook.Tab", font=("Montserrat", 10, "bold"), background=Theme.BG_MID,
                        foreground=Theme.TEXT_DIM, borderwidth=1, bordercolor=Theme.BORDER,
                        lightcolor=Theme.BORDER, darkcolor=Theme.BORDER, padding=(16, 7))
        style.map("TNotebook.Tab", background=[("selected", Theme.BG_LIGHT), ("active", Theme.BG_HOVER)],
                  foreground=[("selected", Theme.TEXT_BRIGHT), ("active", Theme.TEXT_BRIGHT)],
                  bordercolor=[("selected", Theme.CYAN)], padding=[("selected", (16, 7)), ("!selected", (16, 7))])

        style.configure("TCombobox",
                         fieldbackground=Theme.BG_LIGHT,
                         background=Theme.BG_HOVER,
                         foreground=Theme.TEXT_BRIGHT,
                         selectbackground=Theme.CYAN_DIM,
                         selectforeground="#ffffff",
                         bordercolor=Theme.BORDER,
                         arrowcolor=Theme.TEXT_DIM)
        style.map("TCombobox",
                   fieldbackground=[("readonly", Theme.BG_LIGHT)],
                   selectbackground=[("readonly", Theme.CYAN_DIM)],
                   selectforeground=[("readonly", "#ffffff")])

        self.root.option_add("*TCombobox*Listbox.background", Theme.BG_LIGHT)
        self.root.option_add("*TCombobox*Listbox.foreground", Theme.TEXT_BRIGHT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", Theme.BG_HOVER)
        self.root.option_add("*TCombobox*Listbox.selectForeground", Theme.CYAN)
        self.root.option_add("*TCombobox*Listbox.relief", "flat")
        self.root.option_add("*TCombobox*Listbox.borderWidth", "1")
        self.root.option_add("*TCombobox*Listbox.highlightBackground", Theme.BORDER)

        self.root.option_add("*Listbox.background", Theme.BG_LIGHT)
        self.root.option_add("*Listbox.foreground", Theme.TEXT_BRIGHT)
        self.root.option_add("*Listbox.selectBackground", Theme.BG_HOVER)
        self.root.option_add("*Listbox.selectForeground", Theme.CYAN)

        style.configure("Vertical.TScrollbar",
                        background=Theme.BG_MID,
                        troughcolor=Theme.BG_DARKEST,
                        bordercolor=Theme.BG_DARKEST,
                        arrowcolor=Theme.TEXT_DIM,
                        lightcolor=Theme.BG_MID,
                        darkcolor=Theme.BG_MID)
        style.map("Vertical.TScrollbar",
                  background=[("active", Theme.BORDER_LIT)])

        style.configure("Horizontal.TProgressbar",
                        troughcolor=Theme.BG_MID,
                        background=Theme.CYAN,
                        bordercolor=Theme.BORDER,
                        lightcolor=Theme.CYAN,
                        darkcolor=Theme.CYAN)


    def _build_ui(self):
        from src.modules.browser_loading import TkTasks, LatestScan
        if not hasattr(self, '_tasks'):
            self._tasks = TkTasks(self.root)
            self._inventory_worker = LatestScan(self._tasks, self._scan_inventory_request)
            self._inventory_request_id = 0
            self._catalog_revision = 0
        self._rendered_theme = Theme.active_theme
        from src.modules.tk_glass import GlassCard
        from src.modules.runtime_resources import performance_profile
        self._progress_interval = 120 if performance_profile().constrained else 80
        self.root.configure(bg=Theme.BG_DARKEST)

        self._configure_styles()

        # Static glass header; content stays opaque for legibility.
        header = GlassCard(self.root, Theme)
        header.pack(fill="x", padx=12, pady=(12, 8))
        top_bar = header.body
        title_col = tk.Frame(top_bar, bg=Theme.BG_MID)
        title_col.grid(row=0, column=0, sticky="ew")
        top_bar.grid_columnconfigure(0, weight=1)
        actions = tk.Frame(top_bar, bg=Theme.BG_MID)
        actions.grid(row=0, column=1, sticky="e")
        tk.Label(title_col, text="Libraries & boards", anchor="w",
                 font=("Montserrat", 14, "bold"), fg=Theme.TEXT_BRIGHT, bg=Theme.BG_MID).pack(fill="x")
        tk.Label(title_col, text="MCU Flasher by Naph · Arduino packages", anchor="w",
                 font=("Montserrat", 8), fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(fill="x", pady=(3, 0))

        self.quit_btn = make_flat_button(
            actions, "Quit", self._force_exit,
            Theme.BTN_STOP, Theme.BTN_STOP_H
        )
        self.quit_btn.pack(side="right", padx=(6, 0))

        self.refresh_btn = make_flat_button(
            actions, "Refresh indexes", self._refresh_all,
            Theme.BTN_MONITOR, Theme.BTN_MONITOR_H
        )
        self.refresh_btn.pack(side="right")
        self.sources_btn = make_flat_button(actions, "Board indexes", self._toggle_sources,
                                            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H)
        self.sources_btn.pack(side="right", padx=(6, 6))
        self._header_stacked = None

        def reflow_header(event):
            if event.widget is not top_bar:
                return
            stacked = event.width < title_col.winfo_reqwidth() + actions.winfo_reqwidth() + 16
            if stacked != self._header_stacked:
                actions.grid_configure(row=1 if stacked else 0, column=0 if stacked else 1,
                                       columnspan=2 if stacked else 1, sticky="e", pady=(6, 0) if stacked else 0)
                self._header_stacked = stacked
                header._schedule()
        top_bar.bind("<Configure>", reflow_header, add="+")

        # Download folder bar
        folder_card = GlassCard(self.root, Theme, padding=10)
        folder_card.pack(fill="x", padx=12, pady=(0, 8))
        folder_bar = folder_card.body

        tk.Label(folder_bar, text="Download folder:",
                 font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(side="left")

        self.folder_var = tk.StringVar(value=self._download_dir)
        self.folder_entry = tk.Entry(
            folder_bar, textvariable=self.folder_var,
            font=("Consolas", 10), bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN,
            highlightbackground=Theme.BORDER
        )
        self.folder_entry.pack(side="left", padx=(8, 8), fill="x", expand=True)
        self.folder_entry.bind("<Return>", lambda e: self._apply_folder_entry())
        self.folder_entry.bind("<FocusOut>", lambda e: self._apply_folder_entry())

        self.browse_btn = make_flat_button(
            folder_bar, "Browse…", self._choose_download_dir,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H
        )
        self.browse_btn.pack(side="right")

        # Additional board manager indexes. The default Arduino index is
        # always loaded; this field lets users add vendor indexes such as the
        # ESP8266 package index without editing Arduino-CLI files manually.
        self._sources_card = GlassCard(self.root, Theme, padding=10)
        self._sources_open = False
        board_url_bar = tk.Frame(self._sources_card.body, bg=Theme.BG_MID)
        board_url_bar.pack(fill="x")

        tk.Label(
            board_url_bar, text="Vendor URLs:",
            font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_MID,
        ).pack(side="left")

        self.board_urls_var = tk.StringVar(value=", ".join(self._additional_board_urls))
        self.board_urls_entry = tk.Entry(
            board_url_bar, textvariable=self.board_urls_var,
            font=("Consolas", 10), bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN,
            highlightbackground=Theme.BORDER,
        )
        self.board_urls_entry.pack(side="left", padx=(8, 8), fill="x", expand=True)
        self.board_urls_entry.bind("<Return>", lambda e: self._apply_board_urls())

        self.board_urls_apply_btn = make_flat_button(
            board_url_bar, "Apply & refresh", self._apply_board_urls,
            Theme.BTN_MONITOR, Theme.BTN_MONITOR_H,
        )
        self.board_urls_apply_btn.pack(side="right")

        sources_hint = tk.Label(
            self._sources_card.body,
            text="Add vendor index URLs separated by commas or new lines. Each source is checked independently.",
            font=("Montserrat", 8), fg=Theme.TEXT_DIM, bg=Theme.BG_MID,
            anchor="w", justify="left", pady=4,
        )
        sources_hint.pack(fill="x")
        self.sources_status = tk.Label(self._sources_card.body, text="", font=("Montserrat", 8),
                                      fg=Theme.TEXT_DIM, bg=Theme.BG_MID, justify="left", anchor="w")
        self.sources_status.pack(fill="x", pady=(2, 4))
        def wrap_sources(event):
            for label in (sources_hint, self.sources_status):
                label.configure(wraplength=max(100, event.width - 8))
        self._sources_card.body.bind("<Configure>", wrap_sources, add="+")

        third_party_boards_url = (
            "https://github.com/arduino/Arduino/wiki/"
            "Unofficial-list-of-3rd-party-boards-support-urls"
        )
        third_party_boards_link = tk.Label(
            self._sources_card.body,
            text="Find third-party board index URLs ↗",
            font=("Montserrat", 8, "underline"),
            fg=Theme.BLUE, bg=Theme.BG_MID, cursor="hand2",
            anchor="w", padx=10,
        )
        third_party_boards_link.pack(fill="x")
        third_party_boards_link.bind(
            "<Button-1>",
            lambda _event: webbrowser.open(third_party_boards_url),
        )
        third_party_boards_link.bind(
            "<Enter>",
            lambda _event: third_party_boards_link.configure(fg=Theme.CYAN),
        )
        third_party_boards_link.bind(
            "<Leave>",
            lambda _event: third_party_boards_link.configure(fg=Theme.BLUE),
        )

        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(4, 8))
        self.notebook.enable_traversal()

        # --- Libraries tab ---
        lib_frame = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(lib_frame, text="Libraries", underline=0)
        self.lib_tab = BrowseTab(
            lib_frame, self,
            detail_builder=self._build_library_detail,
            on_select_handler=self._on_library_select,
        )

        # --- Boards tab ---
        board_frame = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(board_frame, text="Boards", underline=0)
        self.board_tab = BrowseTab(
            board_frame, self,
            detail_builder=self._build_board_detail,
            on_select_handler=self._on_board_select,
        )

        # --- Installed tab ---
        installed_frame = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(installed_frame, text="Installed", underline=0)
        self.installed_tab = InstalledTab(installed_frame, self)

        # Bind tab selection to recompute installed items when entering the tab
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Bottom bar: progress + status
        bottom_card = GlassCard(self.root, Theme, padding=10)
        bottom_card.pack(side="bottom", fill="x", padx=12, pady=(0, 12), before=self.notebook)
        bottom = bottom_card.body

        self.progress = ttk.Progressbar(bottom, style="Horizontal.TProgressbar", length=140)
        self.progress.pack(side="left")

        self.status_var = tk.StringVar(value="Starting…")
        status = tk.Label(bottom, textvariable=self.status_var, font=("Montserrat", 9),
                          fg=Theme.TEXT_DIM, bg=Theme.BG_MID, justify="left", anchor="w")
        status.pack(side="left", padx=10, fill="x", expand=True)
        bottom.bind("<Configure>", lambda event: status.configure(
            wraplength=max(100, event.width - self.progress.winfo_reqwidth() - 24)), add="+")

    def _toggle_sources(self):
        """Keep advanced vendor indexes available without occupying list space."""
        self._sources_open = not self._sources_open
        if self._sources_open:
            self._sources_card.pack(fill="x", padx=12, pady=(0, 8), before=self.notebook)
        else:
            self._sources_card.pack_forget()
        self.sources_btn.configure(text="Hide indexes" if self._sources_open else "Board indexes")

    def _on_tab_changed(self, event=None):
        try:
            selected_tab = self.notebook.index(self.notebook.select())
            if selected_tab == 2:  # Installed tab
                self._compute_installed_items_async()
        except Exception:
            pass

    def _compute_installed_items_async(self):
        """Coalesce scans; publish only results for the current folder/catalog."""
        self._inventory_request_id += 1
        request_id = self._inventory_request_id
        revision = self._catalog_revision
        folder = self._download_dir
        request = (folder, self.lib_tab.all_items, self.board_tab.all_items, self._is_online)
        def completed(items, error):
            if request_id != self._inventory_request_id or revision != self._catalog_revision or folder != self._download_dir:
                return
            if error:
                self._set_status(f'Installed packages could not be scanned: {error}')
                return
            if items is None:
                return
            self._installed_items = items
            self._installed_map = {item["name"].lower(): item for item in items}
            if hasattr(self, "lib_tab") and hasattr(self.lib_tab, "refresh_listbox_labels"):
                self.lib_tab.refresh_listbox_labels()
            if hasattr(self, "board_tab") and hasattr(self.board_tab, "refresh_listbox_labels"):
                self.board_tab.refresh_listbox_labels()
            self._update_version_status(self.lib_tab)
            self._update_version_status(self.board_tab)
            if self.notebook.index(self.notebook.select()) == 2:
                self.installed_tab.populate(items)
            if not self._busy and self.notebook.index(self.notebook.select()) == 2:
                self._set_status(f'{len(items)} installed package(s)')
        self._inventory_worker.submit(request, completed)

    def _scan_inventory_request(self, request, cancel):
        folder, libraries, boards, online = request
        return self._compute_installed_items(download_dir=folder, libraries=libraries,
                                             boards=boards, online=online, cancel=cancel)

    def _post_ui(self, callback, *args):
        key = 'download-progress' if getattr(callback, '__name__', '') == '_update_progress' else None
        self._tasks.post(callback, *args, key=key)

    def _publish_catalogs(self, libraries, boards):
        changed = False
        for tab, items in ((self.lib_tab, libraries), (self.board_tab, boards)):
            if items is not None and tab.all_items is not items:
                tab.populate(items)
                changed = True
        if changed:
            self._catalog_revision += 1

    def _choose_download_dir(self):
        chosen = filedialog.askdirectory(
            title="Choose download folder",
            initialdir=self._download_dir
                       if os.path.isdir(self._download_dir)
                       else os.path.expanduser("~")
        )
        if not chosen:
            return
        self.folder_var.set(chosen)
        self._apply_folder_entry()

    def _apply_folder_entry(self):
        if self._busy:
            self.folder_var.set(self._download_dir)
            self._set_status("Wait for the current download before changing folders")
            return
        new_path = self.folder_var.get().strip()
        if not new_path:
            self.folder_var.set(self._download_dir)
            return
        new_path = os.path.normpath(new_path)
        if os.path.basename(new_path) != "_MCUFlasherByNaph_src":
            new_path = os.path.join(new_path, "_MCUFlasherByNaph_src")
        try:
            os.makedirs(new_path, exist_ok=True)
        except OSError:
            self.folder_var.set(self._download_dir)
            self._set_status("Invalid folder path — reverted")
            return
        self._download_dir = new_path
        self.folder_var.set(new_path)
        settings = _load_settings()
        settings["download_dir"] = new_path
        if not _save_settings(settings):
            self._set_status("Download folder changed for this session; settings could not be saved")
            self._compute_installed_items_async()
            return
        self._set_status(f"Download folder set to {new_path}")
        # Refresh everything that depends on the download path
        self._compute_installed_items_async()
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)

    def _apply_board_urls(self):
        """Persist additional board indexes and refresh the browser catalog."""
        if self._busy:
            return

        raw_urls = self.board_urls_var.get()
        invalid_urls = invalid_additional_board_urls(raw_urls)
        if invalid_urls:
            preview = "\n".join(f"• {url}" for url in invalid_urls[:8])
            if len(invalid_urls) > 8:
                preview += f"\n• …and {len(invalid_urls) - 8} more"
            messagebox.showerror(
                "Invalid Board Manager URL",
                "Each additional board manager URL must be a complete HTTP or HTTPS URL.\n\n"
                f"Invalid entries:\n{preview}",
                parent=self.root,
            )
            return

        urls = parse_additional_board_urls(raw_urls)
        settings = _load_settings()
        settings["additional_board_urls"] = urls
        if not _save_settings(settings):
            self._set_status("Board indexes could not be saved; check that settings storage is writable")
            return
        self._additional_board_urls = urls
        self.board_urls_var.set(", ".join(urls))
        self._set_status("Board manager URLs saved — refreshing indexes…")
        self._refresh_all()

    def _cancel_download(self):
        self._cancel_event.set()
        self._set_status("Cancelling download…")

    # ------------------------------------------------------------------
    # Library detail panel
    # ------------------------------------------------------------------

    def _build_library_detail(self, tab: BrowseTab):
        dc = tab._detail_content
        dc.configure(bg=Theme.BG_DARKEST)

        tab.lbl_name = tk.Label(dc, text="", font=("Montserrat", 14, "bold"), fg=Theme.CYAN, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_name.pack(anchor="w", fill="x", pady=(0, 2))
        tab._wrapping_labels.append(tab.lbl_name)

        tab.lbl_status_badge = tk.Label(dc, text="", font=("Montserrat", 9, "bold"), fg=Theme.GREEN, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_status_badge.pack(anchor="w", fill="x", pady=(0, 4))
        tab._wrapping_labels.append(tab.lbl_status_badge)

        tab.lbl_author = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_author.pack(anchor="w", fill="x")
        tab._wrapping_labels.append(tab.lbl_author)

        tab.lbl_category = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_category.pack(anchor="w", fill="x")
        tab._wrapping_labels.append(tab.lbl_category)

        tab.lbl_arch = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_arch.pack(anchor="w", fill="x")
        tab._wrapping_labels.append(tab.lbl_arch)

        sep = tk.Frame(dc, bg=Theme.BORDER, height=1)
        sep.pack(fill="x", pady=8)

        tab.lbl_sentence = tk.Label(dc, text="", font=("Montserrat", 10), fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST, anchor="w", justify="left")
        tab.lbl_sentence.pack(anchor="w", fill="x")
        tab._wrapping_labels.append(tab.lbl_sentence)

        tab.lbl_paragraph = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST, anchor="w", justify="left")
        tab.lbl_paragraph.pack(anchor="w", fill="x", pady=(2, 6))
        tab._wrapping_labels.append(tab.lbl_paragraph)

        link_frame = tk.Frame(dc, bg=Theme.BG_DARKEST)
        link_frame.pack(anchor="w", pady=2)

        tab.link_website = tk.Label(link_frame, text="", font=("Montserrat", 9, "underline"), fg=Theme.BLUE, bg=Theme.BG_DARKEST, cursor="hand2")
        tab.link_website.pack(anchor="w")
        tab.link_website.bind("<Button-1>",
                              lambda e: self._open_link(tab, "website"))

        tab.link_repo = tk.Label(link_frame, text="", font=("Montserrat", 9, "underline"), fg=Theme.BLUE, bg=Theme.BG_DARKEST, cursor="hand2")
        tab.link_repo.pack(anchor="w")
        tab.link_repo.bind("<Button-1>",
                           lambda e: self._open_link(tab, "repo"))

        sep2 = tk.Frame(dc, bg=Theme.BORDER, height=1)
        sep2.pack(fill="x", pady=8)

        ver_frame = tk.Frame(dc, bg=Theme.BG_DARKEST)
        ver_frame.pack(anchor="w", fill="x", pady=4)

        tk.Label(ver_frame, text="Version:", font=("Montserrat", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST).pack(side="left")
        tab.version_var = tk.StringVar()
        tab.version_combo = ttk.Combobox(ver_frame,
                                         textvariable=tab.version_var,
                                         state="readonly", width=20)
        tab.version_combo.pack(side="left", padx=4)

        tab.download_btn = make_flat_button(ver_frame, "⬇ Download",
                                            lambda: self._download(tab), Theme.BTN_COMPILE, Theme.BTN_COMPILE_H)
        tab.download_btn.pack(side="left", padx=8)

        tab.lbl_available = tk.Label(dc, text="", font=("Montserrat", 9, "bold"), fg=Theme.GREEN,
                                     bg=Theme.BG_DARKEST, anchor="w", justify="left")
        tab.lbl_available.pack(fill="x", pady=2)
        tab._wrapping_labels.append(tab.lbl_available)

        tab.lbl_size = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_size.pack(anchor="w", pady=2)

    def _on_library_select(self, tab: BrowseTab, lib: dict):
        tab.lbl_name.config(text=lib["name"])
        tab.lbl_author.config(
            text=f"Author: {lib['author']}"
                 + (f"  •  Maintainer: {lib['maintainer']}"
                    if lib["maintainer"] else "")
        )
        tab.lbl_category.config(text=f"Category: {lib['category']}")
        archs = ", ".join(lib["architectures"]) if lib["architectures"] else "*"
        tab.lbl_arch.config(text=f"Architectures: {archs}")

        paragraph = re.sub(r"<[^>]+>", " ", lib.get("paragraph", ""))
        tab.lbl_sentence.config(text=lib["sentence"])
        tab.lbl_paragraph.config(text=paragraph)

        tab._current_website = lib.get("website", "")
        tab._current_repo = lib.get("repository", "")
        tab.link_website.config(
            text=f"🌐 {tab._current_website}" if tab._current_website else ""
        )
        tab.link_repo.config(
            text=f"📦 {tab._current_repo}" if tab._current_repo else ""
        )

        ver_labels = [v["version"] for v in lib["versions"]]
        tab.version_combo.config(values=ver_labels)
        if ver_labels:
            tab.version_combo.current(0)
            tab.version_var.set(ver_labels[0])
        self._update_version_status(tab)
        tab.version_combo.bind("<<ComboboxSelected>>",
                               lambda e: self._update_version_status(tab))

    # ------------------------------------------------------------------
    # Board detail panel
    # ------------------------------------------------------------------

    def _build_board_detail(self, tab: BrowseTab):
        dc = tab._detail_content
        dc.configure(bg=Theme.BG_DARKEST)

        tab.lbl_name = tk.Label(dc, text="", font=("Montserrat", 14, "bold"), fg=Theme.CYAN, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_name.pack(anchor="w", fill="x", pady=(0, 2))
        tab._wrapping_labels.append(tab.lbl_name)

        tab.lbl_status_badge = tk.Label(dc, text="", font=("Montserrat", 9, "bold"), fg=Theme.GREEN, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_status_badge.pack(anchor="w", fill="x", pady=(0, 4))
        tab._wrapping_labels.append(tab.lbl_status_badge)

        tab.lbl_package = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_package.pack(anchor="w", fill="x")
        tab._wrapping_labels.append(tab.lbl_package)

        tab.lbl_maintainer = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_maintainer.pack(anchor="w", fill="x")
        tab._wrapping_labels.append(tab.lbl_maintainer)

        tab.lbl_arch = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_arch.pack(anchor="w", fill="x")
        tab._wrapping_labels.append(tab.lbl_arch)

        tab.lbl_category = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_category.pack(anchor="w", fill="x")
        tab._wrapping_labels.append(tab.lbl_category)

        sep = tk.Frame(dc, bg=Theme.BORDER, height=1)
        sep.pack(fill="x", pady=8)

        tab.lbl_boards_header = tk.Label(dc, text="Supported Boards:", font=("Montserrat", 10, "bold"), fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_boards_header.pack(anchor="w", fill="x")

        tab.lbl_boards = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST, anchor="w", justify="left")
        tab.lbl_boards.pack(anchor="w", fill="x", pady=(2, 6))
        tab._wrapping_labels.append(tab.lbl_boards)

        link_frame = tk.Frame(dc, bg=Theme.BG_DARKEST)
        link_frame.pack(anchor="w", pady=2)

        tab.link_website = tk.Label(link_frame, text="", font=("Montserrat", 9, "underline"), fg=Theme.BLUE, bg=Theme.BG_DARKEST, cursor="hand2")
        tab.link_website.pack(anchor="w")
        tab.link_website.bind("<Button-1>",
                              lambda e: self._open_link(tab, "website"))

        tab.link_help = tk.Label(link_frame, text="", font=("Montserrat", 9, "underline"), fg=Theme.BLUE, bg=Theme.BG_DARKEST, cursor="hand2")
        tab.link_help.pack(anchor="w")
        tab.link_help.bind("<Button-1>",
                           lambda e: self._open_link(tab, "help"))

        sep2 = tk.Frame(dc, bg=Theme.BORDER, height=1)
        sep2.pack(fill="x", pady=8)

        ver_frame = tk.Frame(dc, bg=Theme.BG_DARKEST)
        ver_frame.pack(anchor="w", fill="x", pady=4)

        tk.Label(ver_frame, text="Version:", font=("Montserrat", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST).pack(side="left")
        tab.version_var = tk.StringVar()
        tab.version_combo = ttk.Combobox(ver_frame,
                                         textvariable=tab.version_var,
                                         state="readonly", width=20)
        tab.version_combo.pack(side="left", padx=4)

        tab.download_btn = make_flat_button(ver_frame, "⬇ Download",
                                            lambda: self._download(tab), Theme.BTN_COMPILE, Theme.BTN_COMPILE_H)
        tab.download_btn.pack(side="left", padx=8)

        tab.lbl_available = tk.Label(dc, text="", font=("Montserrat", 9, "bold"), fg=Theme.GREEN,
                                     bg=Theme.BG_DARKEST, anchor="w", justify="left")
        tab.lbl_available.pack(fill="x", pady=2)
        tab._wrapping_labels.append(tab.lbl_available)

        tab.lbl_size = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_size.pack(anchor="w", pady=2)

        prepare_row = tk.Frame(dc, bg=Theme.BG_DARKEST)
        prepare_row.pack(fill="x", pady=(8, 2))
        tab.prepare_btn = make_flat_button(prepare_row, "Prepare board support",
            lambda: self._prepare_existing_board(tab), Theme.BTN_MONITOR, Theme.BTN_MONITOR_H)
        tab.prepare_btn.pack(anchor="w")
        tab.prepare_btn.configure(state="disabled")
        tab.mapping_btn = make_flat_button(prepare_row, "Custom platform…",
            lambda: self._edit_board_platform(tab), Theme.BTN_MONITOR, Theme.BTN_MONITOR_H)
        tab.mapping_btn.pack(anchor="w", pady=(5, 0))
        tab.arduino_cli_btn = make_flat_button(prepare_row, "Choose Arduino CLI boards…",
            lambda: self._edit_arduino_cli_boards(tab), Theme.BTN_MONITOR, Theme.BTN_MONITOR_H)
        tab.arduino_cli_btn.pack(anchor="w", pady=(5, 0))
        hint = tk.Label(dc, text="PlatformIO is used first. Arduino CLI is prepared only for boards you choose when no exact PlatformIO target is available.",
                        font=("Montserrat", 8), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w", justify="left")
        hint.pack(fill="x", pady=(2, 4))
        tab._wrapping_labels.append(hint)

    def _on_board_select(self, tab: BrowseTab, board: dict):
        tab.lbl_name.config(text=board["name"])
        tab.lbl_package.config(text=f"Package: {board['package']}")
        tab.lbl_maintainer.config(text=f"Maintainer: {board['maintainer']}")
        tab.lbl_arch.config(text=f"Architecture: {board['architecture']}")
        tab.lbl_category.config(text=f"Category: {board['category']}")

        boards_text = ", ".join(board["boards"]) if board["boards"] else "—"
        tab.lbl_boards.config(text=boards_text)

        tab._current_website = board.get("website", "")
        tab._current_help = board.get("help_url", "")
        tab.link_website.config(
            text=f"🌐 {tab._current_website}" if tab._current_website else ""
        )
        tab.link_help.config(
            text=f"📖 {tab._current_help}" if tab._current_help else ""
        )

        ver_labels = [v["version"] for v in board["versions"]]
        tab.version_combo.config(values=ver_labels)
        if ver_labels:
            tab.version_combo.current(0)
            tab.version_var.set(ver_labels[0])
        self._update_version_status(tab)
        tab.version_combo.bind("<<ComboboxSelected>>",
                               lambda e: self._update_version_status(tab))

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

    def _edit_arduino_cli_boards(self, tab):
        if self._busy:
            return
        selection = tab.listbox.curselection()
        if not selection or selection[0] >= len(tab.filtered_names):
            return
        item = tab.all_items.get(tab.filtered_names[selection[0]])
        if not item:
            return
        version = next((entry for entry in item.get('versions', [])
                        if entry.get('version') == tab.version_var.get()), None)
        if version is None:
            return
        metadata = {**item, **version}
        archive = _archive_filename(metadata.get('url', ''), metadata.get('archiveFileName', ''))
        folder = os.path.join(self._download_dir, 'Boards', _get_folder_name(archive))
        from src.modules.arduino_board_chooser import open_board_chooser
        open_board_chooser(self, tab, metadata, folder, theme=Theme, button=make_flat_button,
                           load_settings=_load_settings, save_settings=_save_settings)

    def _edit_board_platform(self, tab):
        if self._busy:
            return
        selection = tab.listbox.curselection()
        if not selection or selection[0] >= len(tab.filtered_names):
            return
        item = tab.all_items[tab.filtered_names[selection[0]]]
        version = next((entry for entry in item['versions'] if entry['version'] == tab.version_var.get()), None)
        if version is None:
            return
        from src.modules.board_index_targets import normalize_platformio_configuration
        from src.modules.tk_glass import DialogFit, GlassCard, ScrollForm, ui_scale
        metadata = {**item, **version}
        key = self._board_association_key(metadata)
        try:
            configuration = normalize_platformio_configuration(self._board_platformio_associations.get(key))
        except ValueError:
            configuration = {}
        dialog = tk.Toplevel(self.root)
        dialog.title('Custom board platform')
        dialog.transient(self.root)
        dialog.configure(bg=Theme.BG_DARKEST)
        scale = ui_scale(dialog)
        pad = max(6, round(10 * scale))
        gap = max(4, round(7 * scale))
        header = GlassCard(dialog, Theme, padding=10)
        header.pack(fill='x', padx=pad, pady=(pad, gap))
        heading = tk.Label(header.body, text=item['name'], font=('Montserrat', 12, 'bold'), fg=Theme.TEXT_BRIGHT,
                           bg=Theme.BG_MID, anchor='w', justify='left')
        heading.pack(fill='x')
        header.body.bind('<Configure>', lambda event: heading.configure(wraplength=max(1, event.width)), add='+')
        footer = GlassCard(dialog, Theme, padding=10)
        # Reserve actions before the expanding form consumes the short screen.
        footer.pack(side='bottom', fill='x', padx=pad, pady=(gap, pad))
        status = tk.Label(footer.body, text='', fg=Theme.TEXT_DIM, bg=Theme.BG_MID, font=('Montserrat', 8),
                          anchor='w', justify='left')
        buttons = tk.Frame(footer.body, bg=Theme.BG_MID)
        buttons.pack(fill='x')
        def status_message(message):
            status.configure(text=message)
            status.pack(fill='x', before=buttons)
            buttons.pack_configure(pady=(gap, 0))
        footer.body.bind('<Configure>', lambda event: status.configure(wraplength=max(1, event.width)), add='+')
        card = GlassCard(dialog, Theme, padding=10, expand=True)
        card.pack(fill='both', expand=True, padx=pad)
        scroll = ScrollForm(card.body, Theme.BG_MID)
        scroll.pack(fill='both', expand=True)
        body = scroll.body
        hint = tk.Label(body, text="Use your vendor's PlatformIO platform for custom boards. Clear both fields to restore automatic matching.",
                        font=('Montserrat', 9), fg=Theme.TEXT, bg=Theme.BG_MID, justify='left', anchor='w')
        hint.pack(fill='x', pady=(0, gap))
        tk.Label(body, text='Platform specification', fg=Theme.TEXT_BRIGHT, bg=Theme.BG_MID,
                 font=('Montserrat', 9, 'bold'), anchor='w').pack(fill='x')
        platform = tk.StringVar(value=configuration.get('platform', ''))
        entry = tk.Entry(body, textvariable=platform, bg=Theme.BG_DARKEST, fg=Theme.TEXT,
                         insertbackground=Theme.CYAN, font=('Montserrat', 10), relief='flat',
                         highlightbackground=Theme.BORDER, highlightcolor=Theme.CYAN, highlightthickness=1)
        entry.pack(fill='x', pady=(4, 4), ipady=round(3 * scale))
        platform_help = tk.Label(body, text='owner/platform@version or an HTTPS platform source', font=('Montserrat', 8),
                                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID, anchor='w', justify='left')
        platform_help.pack(fill='x')
        tk.Label(body, text='Optional exact board IDs', font=('Montserrat', 9, 'bold'),
                 fg=Theme.TEXT_BRIGHT, bg=Theme.BG_MID, anchor='w').pack(fill='x', pady=(12, 0))
        mappings_help = tk.Label(body, text='One mapping per line: Arduino declaration ID = PlatformIO board ID',
                                 font=('Montserrat', 8), fg=Theme.TEXT_DIM, bg=Theme.BG_MID, anchor='w', justify='left')
        mappings_help.pack(fill='x', pady=(2, 4))
        editor = tk.Frame(body, bg=Theme.BG_MID)
        editor.pack(fill='x')
        editor.rowconfigure(0, weight=1)
        editor.columnconfigure(0, weight=1)
        mappings = tk.Text(editor, height=6, width=1, bg=Theme.BG_DARKEST, fg=Theme.TEXT,
                           insertbackground=Theme.CYAN, font=('Consolas', 10), wrap='none',
                           padx=5, pady=5, highlightbackground=Theme.BORDER, highlightcolor=Theme.CYAN,
                           highlightthickness=1, relief='flat')
        mappings.insert('1.0', '\n'.join(f'{left} = {right}' for left, right in configuration.get('board_ids', {}).items()))
        mappings.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(editor, orient='vertical', command=mappings.yview)
        ttk.Style(dialog).configure('CustomPlatform.Horizontal.TScrollbar',
                                    background=Theme.BG_MID, troughcolor=Theme.BG_DARKEST,
                                    bordercolor=Theme.BG_DARKEST, arrowcolor=Theme.TEXT_DIM,
                                    lightcolor=Theme.BG_MID, darkcolor=Theme.BG_MID,
                                    arrowsize=max(12, round(12 * scale)))
        ttk.Style(dialog).map('CustomPlatform.Horizontal.TScrollbar',
                              background=[('disabled', Theme.BG_DARK), ('active', Theme.BG_HOVER)],
                              troughcolor=[('disabled', Theme.BG_DARKEST)],
                              arrowcolor=[('disabled', Theme.TEXT_DIM)])
        horizontal = ttk.Scrollbar(editor, orient='horizontal', command=mappings.xview,
                                   style='CustomPlatform.Horizontal.TScrollbar')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        mappings.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        def save():
            if self._busy:
                status_message('Wait for the current download before changing its platform.')
                return
            try:
                parsed = {}
                for line in mappings.get('1.0', 'end').splitlines():
                    if not line.strip():
                        continue
                    left, separator, right = line.partition('=')
                    if not separator or left.strip() in parsed:
                        raise ValueError('Use one unique Arduino ID = PlatformIO ID mapping per line.')
                    parsed[left.strip()] = right.strip()
                value = normalize_platformio_configuration({'platform': platform.get(), 'board_ids': parsed})
            except ValueError as error:
                status_message(str(error))
                return
            save_button.configure(state='disabled')
            status_message('Saving board support settings…')
            def persist():
                settings = _load_settings()
                existing = settings.get('board_platformio_associations')
                stored = dict(existing) if isinstance(existing, dict) else {}
                if value:
                    stored[key] = value
                else:
                    stored.pop(key, None)
                settings['board_platformio_associations'] = stored
                saved = _save_settings(settings)
                def finish():
                    if saved:
                        self._board_platformio_associations = stored
                        self._set_status('Board platform saved. Download or prepare support to verify the exact targets.')
                    if not dialog.winfo_exists():
                        return
                    if saved:
                        dialog.destroy()
                    else:
                        save_button.configure(state='normal')
                        status_message('Settings could not be saved. Check that settings storage is writable.')
                self._post_ui(finish)
            def failed(error):
                if dialog.winfo_exists():
                    save_button.configure(state='normal')
                    status_message(f'Settings could not be saved: {error}')
            self._tasks.start(persist, failed=failed)
        make_flat_button(buttons, 'Cancel', dialog.destroy, Theme.BTN_MONITOR, Theme.BTN_MONITOR_H).pack(side='right')
        save_button = make_flat_button(buttons, 'Save', save, Theme.BTN_COMPILE, Theme.BTN_COMPILE_H)
        save_button.pack(side='right', padx=(0, 8))
        body.bind('<Configure>', lambda event: [label.configure(wraplength=max(1, event.width - 4))
                                               for label in (hint, platform_help, mappings_help)], add='+')
        dialog.bind('<Escape>', lambda _event: dialog.destroy())
        dialog._platform_form = scroll
        dialog._platform_entry = entry
        dialog._platform_mappings = mappings
        dialog._platform_status = status
        dialog._platform_save = save_button
        dialog._dialog_fit = DialogFit(dialog, self.root, preferred=(620, 475), minimum=(320, 240))
        dialog.grab_set()
        entry.focus_set()
        scroll.ensure_visible(entry)

    # ------------------------------------------------------------------
    # Shared detail helpers
    # ------------------------------------------------------------------

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

    def _update_version_status(self, tab: BrowseTab):
        sel = tab.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(tab.filtered_names):
            return
        name = tab.filtered_names[idx]
        item = tab.all_items.get(name)
        if not item:
            return
        ver = tab.version_var.get()

        target_version = None
        for v in item.get("versions", []):
            if v["version"] == ver:
                target_version = v
                break

        if not target_version:
            return

        size_val = target_version.get("size", 0)
        try:
            size_val = int(size_val)
        except (ValueError, TypeError):
            size_val = 0
        size_kb = size_val / 1024
        if size_kb > 1024:
            tab.lbl_size.config(text=f"Size: {size_kb / 1024:.1f} MB")
        else:
            tab.lbl_size.config(text=f"Size: {size_kb:.0f} KB")

        tab._version_revision += 1
        revision = tab._version_revision
        folder = self._download_dir
        if hasattr(tab, "prepare_btn"):
            tab.prepare_btn.configure(state="disabled")
        if self._busy and tab == self._active_download_tab and item.get("name") == self._downloading_item_name:
            self._render_version_status(tab, name, item, ver, None)
            return
        def completed(installed_info, error):
            if (revision != tab._version_revision or folder != self._download_dir
                    or ver != tab.version_var.get()):
                return
            selection = tab.listbox.curselection()
            if not selection or selection[0] >= len(tab.filtered_names) or tab.filtered_names[selection[0]] != name:
                return
            if error:
                tab.lbl_available.configure(text=f"Local package scan failed: {error}", fg=Theme.RED)
                return
            self._render_version_status(tab, name, item, ver, installed_info)
        tab._version_worker.submit((tab, name, item, folder, tuple(getattr(self, "_installed_items", []))), completed)

    def _render_version_status(self, tab, name, item, ver, installed_info):
        if hasattr(tab, "prepare_btn"):
            available = bool(installed_info and ver in installed_info["installed_versions"])
            tab.prepare_btn.configure(state="normal" if available and not self._busy else "disabled")

        if self._busy and tab == self._active_download_tab and item.get("name") == self._downloading_item_name:
            tab.lbl_available.config(text="")
            if hasattr(tab, "lbl_status_badge"):
                tab.lbl_status_badge.config(text="")
            tab.download_btn.config(text="✕ Cancel", command=self._cancel_download, state="normal")
            return

        if installed_info is not None:
            inst_ver = installed_info["installed_version"]
            best_info = installed_info["best_info"]
            all_inst_vers = installed_info["installed_versions"]

            if ver in all_inst_vers:
                curr_info = all_inst_vers[ver]
                badge_text = f"Downloaded files (v{ver})" if tab is self.board_tab else f"✔ Already Available (v{ver})"
                tab.lbl_available.config(text=badge_text, fg=Theme.GREEN)
                if hasattr(tab, "lbl_status_badge"):
                    tab.lbl_status_badge.config(text=badge_text, fg=Theme.GREEN)

                if curr_info["has_folder"]:
                    tab.download_btn.config(
                        text="⬇ Download (ZIP)",
                        command=lambda t=tab: self._download(t, already_available=True),
                        state="normal",
                        bg=Theme.BTN_CLEAR,
                        activebackground=Theme.BTN_CLEAR_H,
                        cursor="hand2",
                    )
                else:
                    tab.download_btn.config(
                        text="⬇ Download",
                        command=lambda t=tab: self._download(t, already_available=False),
                        state="normal",
                        bg=Theme.BTN_COMPILE,
                        activebackground=Theme.BTN_COMPILE_H,
                        cursor="hand2",
                    )

            elif _version_key(ver) > _version_key(inst_ver):
                badge_text = f"⬆ Update Available (Installed: v{inst_ver})"
                tab.lbl_available.config(text=badge_text, fg=Theme.YELLOW)
                if hasattr(tab, "lbl_status_badge"):
                    tab.lbl_status_badge.config(text=badge_text, fg=Theme.YELLOW)

                cleanup_target = (
                    best_info.get("folder_path") or best_info.get("archive_path"),
                    best_info.get("archive") or os.path.basename(best_info.get("archive_path", "")),
                )
                tab.download_btn.config(
                    text=f"⬆ Update to v{ver}",
                    command=lambda t=tab, cl=cleanup_target: self._download(t, cleanup_old=cl, already_available=False),
                    state="normal",
                    bg=Theme.BTN_COMPILE,
                    activebackground=Theme.BTN_COMPILE_H,
                    cursor="hand2",
                )

            else:
                badge_text = f"Installed: v{inst_ver} (Older v{ver} selected)"
                tab.lbl_available.config(text=badge_text, fg=Theme.TEXT_DIM)
                if hasattr(tab, "lbl_status_badge"):
                    tab.lbl_status_badge.config(text=badge_text, fg=Theme.TEXT_DIM)

                tab.download_btn.config(
                    text=f"⬇ Download v{ver}",
                    command=lambda t=tab: self._download(t, already_available=False),
                    state="normal",
                    bg=Theme.BTN_COMPILE,
                    activebackground=Theme.BTN_COMPILE_H,
                    cursor="hand2",
                )
        else:
            tab.lbl_available.config(text="")
            if hasattr(tab, "lbl_status_badge"):
                tab.lbl_status_badge.config(text="")

            tab.download_btn.config(
                text="⬇ Download",
                command=lambda t=tab: self._download(t, already_available=False),
                state="normal",
                bg=Theme.BTN_COMPILE,
                activebackground=Theme.BTN_COMPILE_H,
                cursor="hand2",
            )

    def _open_link(self, tab: BrowseTab, link_type: str):
        if link_type == "website":
            url = getattr(tab, "_current_website", "")
        elif link_type == "repo":
            url = getattr(tab, "_current_repo", "")
        elif link_type == "help":
            url = getattr(tab, "_current_help", "")
        else:
            url = ""
        if url:
            webbrowser.open(url)

    # ------------------------------------------------------------------
    # Installed items computation (update detection)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _initial_load(self):
        """Load local catalogs first, then fetch missing or stale sources."""
        if self._busy:
            return
        self._set_status("Initializing package manager…")
        self._start_thread(self._load_both)

    def _refresh_all(self):
        if self._busy:
            return
        self._set_status('Refreshing catalog indexes…')
        self._start_thread(lambda: self._load_both(force_refresh=True))

    def _cache_is_fresh(self, cache_file: str) -> bool:
        if not os.path.isfile(cache_file):
            return False
        age = time.time() - os.path.getmtime(cache_file)
        return age < CACHE_MAX_AGE_SECONDS

    def _start_thread(self, target):
        if self._busy:
            return
        self._busy = True
        self.refresh_btn.config(state='disabled')
        self.progress.config(mode='indeterminate')
        self.progress.start(self._progress_interval)
        self._tasks.start(target, failed=lambda error: self._finish_load(f'Catalog load failed: {error}'))

    def _load_both(self, force_refresh=False):
        """Publish cached catalogs before HTTP requests; bound refresh concurrency."""
        from concurrent.futures import ThreadPoolExecutor
        from src.modules.runtime_resources import performance_profile
        urls = [BOARD_INDEX_URL, *self._additional_board_urls]
        paths = [BOARD_CACHE_FILE if index == 0 else _board_index_cache_file(url)
                 for index, url in enumerate(urls)]
        libs = _read_library_catalog(LIBRARY_CACHE_FILE)
        source_urls = dict(zip(paths, urls))
        boards = _read_board_catalog(paths, source_urls=source_urls)
        self._post_ui(self._publish_catalogs, libs, boards)
        needed = [(index, url, cache) for index, (url, cache) in enumerate(zip(urls, paths))
                  if force_refresh or not self._cache_is_fresh(cache) or _read_index_cache(cache, 'packages') is None]
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
                self._post_ui(self._publish_catalogs, libs, None)
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
        self._post_ui(self._publish_catalogs, libs, boards)
        message = f'{len(libs or {})} libraries, {len(boards or {})} board platforms loaded'
        if not self._is_online:
            message += ' — offline/unavailable; using local caches'
        if failed:
            message += f'; {len(failed)} additional index(es) unavailable'
        if self._catalog_memory_only:
            message += '; cache storage unavailable — downloaded data kept in memory'
        if hasattr(self, "sources_status"):
            lines = [f"{source}: {outcome}" for source, outcome in self._source_results.items()
                     if source != LIBRARY_INDEX_URL]
            self._post_ui(lambda: self.sources_status.configure(text="\n".join(lines)))
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

        def _update_overlay():
            self._set_status(f"Downloading {label} index…")
            if hasattr(self, "_loading_overlay") and self._loading_overlay and self._loading_overlay.winfo_exists():
                self._loading_overlay.update_message(
                    title=f"Downloading {label.title()} Index...",
                    subtitle="Downloading index from Arduino servers on background thread..."
                )

        self._post_ui(_update_overlay)
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

    def _finish_load(self, msg: str):
        if getattr(self, '_loading_overlay', None):
            self._loading_overlay.stop_and_destroy()
            self._loading_overlay = None
        self.progress.stop()
        self.progress.config(mode='determinate', value=0)
        self._set_status(msg)
        self._busy = False
        self.refresh_btn.config(state='normal')
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)
        self._compute_installed_items_async()

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

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

    def _download_start_error(self, tab, message):
        try:
            self._tasks.start(lambda: self._publish_job("failed", message=message))
        finally:
            self._download_error(tab, message)

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

    def _prepare_existing_board(self, tab):
        if self._busy:
            return
        selection = tab.listbox.curselection()
        if not selection or selection[0] >= len(tab.filtered_names):
            return
        name = tab.filtered_names[selection[0]]
        item = tab.all_items.get(name)
        if not item:
            return
        version = next((entry for entry in item.get("versions", []) if entry["version"] == tab.version_var.get()), None)
        if not version:
            return
        try:
            metadata = self._package_metadata(item, version, is_board=True)
        except ValueError as error:
            self._set_status(f'Custom platform settings are invalid: {error}')
            return
        self._begin_package_job(name)
        job_id = self._active_package_job
        self._busy, self._active_download_tab = True, tab
        self._downloading_item_name = name
        self._cancel_event.clear()
        tab.prepare_btn.configure(state="disabled")
        tab.download_btn.configure(text="✕ Cancel", command=self._cancel_download, state="normal")
        self._set_status("Checking downloaded board package…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(self._progress_interval)
        destination = os.path.join(self._download_dir, "Boards")
        self._tasks.start(self._prepare_existing_worker, tab, destination, metadata, job_id,
            failed=lambda error: self._download_start_error(tab, f"Unable to start preparation:\n{error}"))

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

    def _prompt_download_option(self, archive_name, already_available: bool = False) -> str:
        dialog = tk.Toplevel(self.root)
        dialog.title("Download Options")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=Theme.BG_DARKEST)

        result = tk.StringVar(value="")

        if already_available:
            title_text = f"Choose format for:\n{archive_name}\n\n(Extracted files already exist locally — only Archive / ZIP download is available)"
        else:
            title_text = f"Choose format for:\n{archive_name}"

        lbl = tk.Label(
            dialog,
            text=title_text,
            font=("Montserrat", 10), justify="center", anchor="center", wraplength=700,
            fg=Theme.YELLOW if already_available else Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST
        )
        lbl.pack(pady=15)

        btn_frame = tk.Frame(dialog, bg=Theme.BG_DARKEST)
        btn_frame.pack(fill="x", padx=20)

        def select_option(opt):
            result.set(opt)
            dialog.destroy()

        if already_available:
            btn_folder = make_flat_button(
                btn_frame, "̶📁̶ ̶E̶x̶t̶r̶a̶c̶t̶e̶d̶ (Already Available)",
                lambda: None, Theme.BG_MID, Theme.BG_MID
            )
            btn_folder.config(state="disabled", cursor="arrow")
            btn_folder.pack(side="left", padx=8, expand=True, fill="x")

            btn_both = make_flat_button(
                btn_frame, "̶📦̶ ̶B̶o̶t̶h̶ (Already Available)",
                lambda: None, Theme.BG_MID, Theme.BG_MID
            )
            btn_both.config(state="disabled", cursor="arrow")
            btn_both.pack(side="left", padx=8, expand=True, fill="x")

            btn_zip = make_flat_button(
                btn_frame, "🗜 Archive Only (ZIP)",
                lambda: select_option("zip"), Theme.BTN_COMPILE, Theme.BTN_COMPILE_H
            )
            btn_zip.pack(side="left", padx=8, expand=True, fill="x")
            btn_zip.focus_set()
            dialog.bind("<Return>", lambda e: select_option("zip"))
        else:
            btn_folder = make_flat_button(
                btn_frame, "📁 Folder / Extracted (Default)",
                lambda: select_option("folder"), Theme.BTN_COMPILE, Theme.BTN_COMPILE_H
            )
            btn_folder.pack(side="left", padx=8, expand=True, fill="x")

            btn_both = make_flat_button(
                btn_frame, "📦 Both (ZIP & Folder)",
                lambda: select_option("both"), Theme.BTN_MONITOR, Theme.BTN_MONITOR_H
            )
            btn_both.pack(side="left", padx=8, expand=True, fill="x")

            btn_zip = make_flat_button(
                btn_frame, "🗜 Archive Only",
                lambda: select_option("zip"), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H
            )
            btn_zip.pack(side="left", padx=8, expand=True, fill="x")
            btn_folder.focus_set()
            dialog.bind("<Return>", lambda e: select_option("folder"))

        cancel_frame = tk.Frame(dialog, bg=Theme.BG_DARKEST)
        cancel_frame.pack(fill="x", pady=10)
        btn_cancel = make_flat_button(cancel_frame, "Cancel", dialog.destroy, Theme.BTN_STOP, Theme.BTN_STOP_H)
        btn_cancel.pack(pady=5)

        dialog.bind("<Escape>", lambda e: dialog.destroy())

        dialog.update_idletasks()
        req_w = max(780, dialog.winfo_reqwidth() + 40)
        req_h = dialog.winfo_reqheight() + 10
        x = self.root.winfo_x() + (self.root.winfo_width() - req_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - req_h) // 2
        dialog.geometry(f"{req_w}x{req_h}+{x}+{y}")

        self.root.wait_window(dialog)
        return result.get()

    def _download(self, tab: BrowseTab, cleanup_old: tuple | None = None, already_available: bool = False):
        sel = tab.listbox.curselection()
        if not sel or self._busy:
            return
        idx = sel[0]
        if idx >= len(tab.filtered_names):
            return
        name = tab.filtered_names[idx]
        item = tab.all_items[name]
        ver = tab.version_var.get()

        url = ""
        target_version = None
        for v in item["versions"]:
            if v["version"] == ver:
                url = v["url"]
                target_version = v
                break

        if not url or target_version is None:
            messagebox.showerror("Error", f"No download URL found for version '{ver or '(none)'}'.")
            return
        try:
            metadata = self._package_metadata(item, target_version, is_board=(tab == self.board_tab))
        except ValueError as error:
            self._set_status(f'Custom platform settings are invalid: {error}')
            return
        archive = _archive_filename(url, target_version.get("archiveFileName", ""))

        subfolder = "Libs" if tab == self.lib_tab else "Boards"
        dest_dir = os.path.join(self._download_dir, subfolder)
        download_option = self._prompt_download_option(archive, already_available=already_available)
        if not download_option:
            return

        self._busy = True
        self._active_download_tab = tab
        self._downloading_item_name = name
        self._cancel_event.clear()
        self._begin_package_job(name)
        tab.download_btn.config(text="✕ Cancel", command=self._cancel_download, state="normal")
        status_action = "Updating" if cleanup_old else "Downloading"
        self._set_status(f"{status_action} {archive}…")
        self.progress.config(mode="indeterminate")
        self.progress.start(self._progress_interval)

        self._tasks.start(self._download_worker, tab, url, archive, dest_dir,
                          download_option, cleanup_old, metadata, self._active_package_job,
                          failed=lambda error: self._download_start_error(tab, f"Unable to start download:\n{error}"))

    def _download_update(self, name: str, is_board: bool, old_path: str = "", old_archive: str = ""):
        if self._busy:
            return
        tab = self.board_tab if is_board else self.lib_tab
        item = tab.all_items.get(name)
        if not item:
            messagebox.showerror("Error", f"Item '{name}' not found in index.", parent=self.root)
            return

        ver = item["versions"][0]["version"]
        url = ""
        target_version = None
        for v in item["versions"]:
            if v["version"] == ver:
                url = v["url"]
                target_version = v
                break

        if not url or target_version is None:
            messagebox.showerror("Error", f"No download URL found for version '{ver}'.", parent=self.root)
            return
        try:
            metadata = self._package_metadata(item, target_version, is_board=is_board)
        except ValueError as error:
            self._set_status(f'Custom platform settings are invalid: {error}')
            return
        archive = _archive_filename(url, target_version.get("archiveFileName", ""))

        subfolder = "Boards" if is_board else "Libs"
        dest_dir = os.path.join(self._download_dir, subfolder)
        download_option = self._prompt_download_option(archive)
        if not download_option:
            return

        self._busy = True
        self._active_download_tab = tab
        self._downloading_item_name = name
        self._cancel_event.clear()
        self._begin_package_job(name)

        tab.download_btn.config(text="✕ Cancel", command=self._cancel_download, state="normal")
        if hasattr(self, "installed_tab") and hasattr(self.installed_tab, "update_btn"):
            self.installed_tab.update_btn.config(text="✕ Cancel", command=self._cancel_download, state="normal")

        self._set_status(f"Downloading update {archive}…")
        self.progress.config(mode="indeterminate")
        self.progress.start(self._progress_interval)

        self._tasks.start(self._download_worker, tab, url, archive, dest_dir,
                          download_option, (old_path, old_archive), metadata, self._active_package_job,
                          failed=lambda error: self._download_start_error(tab, f"Unable to start download update:\n{error}"))

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

    def _set_progress_determinate(self, total):
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=total, value=0)

    def _update_progress(self, downloaded, total):
        if not self._busy or self._active_download_tab is None:
            return
        self.progress.config(value=downloaded)
        pct = int(downloaded / total * 100) if total else 0
        self._set_status(f"Downloading… {pct}%")

    def _download_done(self, tab: BrowseTab, filepath, preparing=False):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._set_status("Board preparation is running in the background" if preparing else "Download complete")
        if preparing and getattr(self, "_preparation_tracking_error", None):
            self._set_status(f"{self.status_var.get()}; background tracking unavailable: {self._preparation_tracking_error}")
        if getattr(self, "_package_tracking_error", None):
            self._set_status(f"{self.status_var.get()}; package tracking could not be saved")
        self._busy = False
        tab.download_btn.config(text="⬇ Download", command=lambda t=tab: self._download(t), state="normal")
        if hasattr(self, "installed_tab") and hasattr(self.installed_tab, "refresh_update_button_state"):
            self.installed_tab.refresh_update_button_state()
        elif hasattr(self, "installed_tab") and hasattr(self.installed_tab, "update_btn"):
            self.installed_tab.update_btn.config(text="⬇ Update", command=self.installed_tab._update_item, state="disabled")
        self._active_download_tab = None
        self._downloading_item_name = None
        
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)
        # Refresh installed list
        self._compute_installed_items_async()
        try:
            if self.notebook.index(self.notebook.select()) == 2:
                self.installed_tab.populate(self._installed_items)
            elif hasattr(self, "installed_tab") and hasattr(self.installed_tab, "refresh_update_button_state"):
                self.installed_tab.refresh_update_button_state()
        except Exception:
            pass
        
        # Completion stays in the status row and workspace Notifications tab.

    def _download_cancelled(self, tab: BrowseTab):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._set_status("Download cancelled")
        self._busy = False
        tab.download_btn.config(text="⬇ Download", command=lambda t=tab: self._download(t), state="normal")
        if hasattr(self, "installed_tab") and hasattr(self.installed_tab, "refresh_update_button_state"):
            self.installed_tab.refresh_update_button_state()
        elif hasattr(self, "installed_tab") and hasattr(self.installed_tab, "update_btn"):
            self.installed_tab.update_btn.config(text="⬇ Update", command=self.installed_tab._update_item, state="disabled")
        self._active_download_tab = None
        self._downloading_item_name = None
        
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)

    def _download_error(self, tab: BrowseTab, msg):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._set_status("Download failed")
        self._busy = False
        tab.download_btn.config(text="⬇ Download", command=lambda t=tab: self._download(t), state="normal")
        if hasattr(self, "installed_tab") and hasattr(self.installed_tab, "refresh_update_button_state"):
            self.installed_tab.refresh_update_button_state()
        elif hasattr(self, "installed_tab") and hasattr(self.installed_tab, "update_btn"):
            self.installed_tab.update_btn.config(text="⬇ Update", command=self.installed_tab._update_item, state="disabled")
        self._active_download_tab = None
        self._downloading_item_name = None
        
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)
        messagebox.showerror("Download Error", msg, parent=self.root)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str):
        self.status_var.set(text)

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.modules.runtime_resources import enforce_minimum_cpu_requirement
    if not enforce_minimum_cpu_requirement():
        raise SystemExit(2)
    if sys.platform.startswith("linux"):
        from src.modules.ubuntu_download_manager import AlreadyRunning, DownloadManagerChannel, close_viewers, request
        try:
            channel = DownloadManagerChannel(SCRIPT_DIR)
        except AlreadyRunning:
            raise SystemExit(0 if request(SCRIPT_DIR, "wake") else 1)
        try:
            app = ArduinoBrowser(linux_channel=channel)
            app.run()
        finally:
            channel.close()
            close_viewers()
    else:
        app = ArduinoBrowser()
        app.run()
