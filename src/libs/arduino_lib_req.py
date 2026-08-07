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
* All network I/O runs in background threads — the GUI never freezes.
* Split-pane layout: list on the left, detail panel on the right.
* Version dropdown to pick any release.
* Progress bar for index loading and zip downloads.
* Clickable website / repository links (opens in default browser).
* Installed tab shows all downloaded items with **available update** indicators.
"""

import json
import os
import re
import sys
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import webbrowser

# Self-bootstrap using env if running outside of it
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.environ.get("MCU_PREF_DIR", os.path.dirname(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VENV_DIR = os.path.join(SCRIPT_DIR, "env")
if os.path.isdir(VENV_DIR) and not getattr(sys, 'frozen', False):
    current_exe = os.path.normpath(sys.executable).lower()
    venv_exe = os.path.normpath(os.path.join(VENV_DIR, "Scripts", "python.exe")).lower()
    venv_exew = os.path.normpath(os.path.join(VENV_DIR, "Scripts", "pythonw.exe")).lower()
    if current_exe != venv_exe and current_exe != venv_exew:
        exe_to_use = venv_exew if current_exe.endswith("pythonw.exe") else venv_exe
        subprocess.Popen([exe_to_use] + sys.argv)
        sys.exit(0)

import requests

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

# Settings cache (remembers download folder across sessions)
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "arduino_browser_settings.json")

# Default download location
DEFAULT_DOWNLOAD_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "_MCUFlasherByNaph_src"
)


class Theme:
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


def make_flat_button(parent, text, command, bg, bg_hover, font=("Montserrat", 9, "bold")) -> tk.Button:
    btn = tk.Button(
        parent, text=text, command=command,
        font=font, fg=Theme.TEXT_BRIGHT, bg=bg,
        activebackground=bg_hover, activeforeground=Theme.TEXT_BRIGHT,
        disabledforeground=Theme.TEXT_DIM,
        relief=tk.FLAT, borderwidth=0, padx=14, pady=4, cursor="hand2"
    )
    btn.bind("<Enter>", lambda e: btn.configure(bg=bg_hover) if btn["state"] != "disabled" else None)
    btn.bind("<Leave>", lambda e: btn.configure(bg=bg) if btn["state"] != "disabled" else None)
    return btn


class CircularLoadingOverlay(tk.Frame):
    """Circular arc loading spinner overlay matching the AI Assistant loading animation."""

    def __init__(self, parent, bg_color=Theme.BG_DARKEST, spinner_color=Theme.CYAN,
                 title="Loading Arduino Indexes...", subtitle="Downloading & scanning packages in background thread..."):
        super().__init__(parent, bg=bg_color)
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
            self._after_id = self.after(30, self._draw_spinner)
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


def _open_code_viewer(file_path, all_paths=None):
    viewer_script = os.path.join(SCRIPT_DIR, "src", "qscintilla_viewer.py")
    if not os.path.exists(viewer_script):
        # Fallback to notepad/editor if the viewer script is missing
        try:
            if sys.platform == "win32":
                import ctypes
                SW_SHOWNORMAL = 1
                ctypes.windll.shell32.ShellExecuteW(None, "open", "notepad.exe", f'"{file_path}"', None, SW_SHOWNORMAL)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-e", file_path])
            else:
                subprocess.Popen(["xdg-open", file_path])
        except Exception as e:
            messagebox.showerror("Error", f"Viewer script not found and fallback failed:\n{e}")
        return

    try:
        # Launch standalone PyQt5 viewer with focus file and all library example files
        cmd = [sys.executable, viewer_script, file_path]
        if all_paths:
            cmd.extend(all_paths)
        subprocess.Popen(cmd)
    except Exception as e:
        messagebox.showerror("Error", f"Failed to launch QScintilla viewer:\n{e}")


def _load_settings() -> dict:
    """Load persisted settings from disk. Returns defaults on any error."""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save_settings(settings: dict):
    """Persist settings to disk. Non-fatal on failure."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
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


def _version_key(version_str: str):
    """Return a sort key that orders semantic versions correctly."""
    m = _VERSION_RE.match(version_str.strip())
    if not m:
        return (0, 0, 0, 0, version_str)

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

    return (major, minor, patch, is_release, pre_key)


def _get_folder_name(archive_name: str) -> str:
    """Strip archive extension to get folder name."""
    for ext in ['.tar.bz2', '.tar.gz', '.tar.xz', '.zip', '.tgz', '.tbz2']:
        if archive_name.lower().endswith(ext):
            return archive_name[:-len(ext)]
    return os.path.splitext(archive_name)[0]


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


def _extract_archive(filepath: str, extract_dir: str):
    """Extract a zip or tar archive into extract_dir."""
    import zipfile
    import tarfile
    import shutil
    from pathlib import Path

    if filepath.lower().endswith('.zip'):
        with zipfile.ZipFile(filepath, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
    elif filepath.lower().endswith(('.tar.gz', '.tgz')):
        with tarfile.open(filepath, 'r:gz') as tar_ref:
            tar_ref.extractall(extract_dir)
    elif filepath.lower().endswith(('.tar.bz2', '.tbz2')):
        with tarfile.open(filepath, 'r:bz2') as tar_ref:
            tar_ref.extractall(extract_dir)
    elif filepath.lower().endswith(('.tar.xz', '.txz')):
        with tarfile.open(filepath, 'r:xz') as tar_ref:
            tar_ref.extractall(extract_dir)

    # Self-heal / flatten double nesting if present
    try:
        p_dir = Path(extract_dir)
        subdirs = [p for p in p_dir.iterdir() if p.is_dir()]
        files = [p for p in p_dir.iterdir() if p.is_file()]
        if len(subdirs) == 1 and len(files) == 0:
            nested = subdirs[0]
            # Move all contents of nested up to p_dir
            for item in nested.iterdir():
                shutil.move(str(item), str(p_dir))
            nested.rmdir()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Data model — Libraries
# ---------------------------------------------------------------------------

def _group_libraries(raw_list: list[dict]) -> dict[str, dict]:
    """Group the flat release list by library name."""
    by_name: dict[str, list[dict]] = {}
    for entry in raw_list:
        name = entry.get("name", "")
        by_name.setdefault(name, []).append(entry)

    result: dict[str, dict] = {}
    for name, entries in by_name.items():
        entries.sort(key=lambda e: _version_key(e.get("version", "0")),
                     reverse=True)
        latest = entries[0]
        versions = []
        for e in entries:
            versions.append({
                "version": e.get("version", "?"),
                "url": e.get("url", ""),
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
    """Group the board package index by platform name.

    The JSON has packages → platforms (each platform is a version of a
    board core, e.g. "Arduino AVR Boards 1.8.6").  We group all platform
    versions under a single display name and keep every version available
    for download.
    """
    result: dict[str, dict] = {}

    for pkg in packages:
        pkg_name = pkg.get("name", "")
        pkg_maintainer = pkg.get("maintainer", "")
        pkg_website = pkg.get("websiteURL", "")
        pkg_email = pkg.get("email", "")

        platforms = pkg.get("platforms", [])
        # Group platforms by their display name (e.g. "Arduino AVR Boards")
        by_platform_name: dict[str, list[dict]] = {}
        for plat in platforms:
            pname = plat.get("name", pkg_name)
            by_platform_name.setdefault(pname, []).append(plat)

        for pname, plat_versions in by_platform_name.items():
            # Sort versions newest-first
            plat_versions.sort(
                key=lambda p: _version_key(p.get("version", "0")),
                reverse=True
            )
            latest = plat_versions[0]

            # Collect board names from the latest version
            boards_list = [b.get("name", "") for b in latest.get("boards", [])]

            versions = []
            for pv in plat_versions:
                size_val = pv.get("size", 0)
                try:
                    size_val = int(size_val)
                except (ValueError, TypeError):
                    size_val = 0
                versions.append({
                    "version": pv.get("version", "?"),
                    "url": pv.get("url", ""),
                    "size": size_val,
                    "checksum": pv.get("checksum", ""),
                    "archiveFileName": pv.get("archiveFileName", ""),
                })

            result[pname] = {
                "name": pname,
                "package": pkg_name,
                "maintainer": pkg_maintainer,
                "website": pkg_website,
                "email": pkg_email,
                "architecture": latest.get("architecture", ""),
                "category": latest.get("category", ""),
                "boards": boards_list,
                "help_url": latest.get("help", {}).get("online", ""),
                "versions": versions,
            }

    return result


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

        self._wrapping_labels: list[ttk.Label] = []

        self._build(parent)

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
        self.search_entry.pack(side="left", padx=(8, 10))
        self.search_entry.bind("<KeyRelease>", self._on_search)

        self.lbl_search_status = tk.Label(top, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST)
        self.lbl_search_status.pack(side="left", padx=(10, 0))

        # Paned window: list | detail
        pane = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=Theme.BORDER, sashwidth=2, sashrelief=tk.FLAT, bd=0)
        pane.pack(fill="both", expand=True, pady=(4, 4))

        # --- Left: item list ---
        left = tk.Frame(pane, bg=Theme.BG_DARKEST)
        pane.add(left, minsize=200)

        self.listbox = tk.Listbox(
            left, font=("Consolas", 10),
            bg=Theme.BG_MID, fg=Theme.TEXT_BRIGHT,
            selectbackground=Theme.BORDER_LIT, selectforeground=Theme.TEXT_BRIGHT,
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
        self.detail_frame = tk.Frame(pane, bg=Theme.BG_DARKEST)
        pane.add(self.detail_frame, minsize=350)

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
            self.detail_canvas.after_idle(_sync_detail_scrollbar)

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
        self.all_items = items
        self.sorted_names = sorted(items.keys(), key=str.lower)
        self.name_tuples = [(n, n.lower()) for n in self.sorted_names]
        self.filtered_names = list(self.sorted_names)
        if hasattr(self, "detail_canvas"):
            self.detail_canvas.pack_forget()
        if hasattr(self, "detail_scroll"):
            self.detail_scroll.pack_forget()
            self._scroll_state["visible"] = False
        self.lbl_placeholder.pack(fill="both", expand=True, padx=10, pady=10)
        self._populate_listbox()

    def _populate_listbox(self):
        if getattr(self, "_load_more_after_id", None) is not None:
            self.app.root.after_cancel(self._load_more_after_id)
            self._load_more_after_id = None
        self._loading_more = False

        self.listbox.delete(0, tk.END)
        total = len(self.filtered_names)
        self.loaded_count = min(250, total)
        if total > 250:
            self.lbl_search_status.config(text=f"Showing top {self.loaded_count} of {total} matches")
            self.listbox.insert(tk.END, *self.filtered_names[:self.loaded_count])
        else:
            if total == 0:
                self.lbl_search_status.config(text="No matches found")
            else:
                self.lbl_search_status.config(text=f"Found {total} matches")
            if self.filtered_names:
                self.listbox.insert(tk.END, *self.filtered_names[:self.loaded_count])

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
                items_to_add = self.filtered_names[self.loaded_count:next_count]
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
        self._search_after_id = None
        text = self.search_var.get().strip().lower()
        if not text:
            self.filtered_names = list(self.sorted_names)
        else:
            starts_with = []
            contains = []
            for n, n_lower in self.name_tuples:
                if n_lower.startswith(text):
                    starts_with.append(n)
                elif text in n_lower:
                    contains.append(n)
            self.filtered_names = starts_with + contains
        self._populate_listbox()

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
        self.detail_canvas.after_idle(self._sync_detail_scrollbar)


# ---------------------------------------------------------------------------
# Installed Tab Class
# ---------------------------------------------------------------------------

class InstalledTab:
    """A tab that shows all locally installed libraries / board platforms
    and indicates available updates."""

    def __init__(self, parent: ttk.Frame, app: "ArduinoBrowser"):
        self.app = app
        self.installed_items: list[dict] = []   # list of installed info dicts
        self.filtered_items: list[dict] = []
        self._search_after_id = None
        self._wrapping_labels = []
        self._all_examples = []
        self._all_boards = []

        self._build(parent)

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
        self.search_entry.pack(side="left", padx=(8, 10))
        self.search_entry.bind("<KeyRelease>", self._on_search)

        self.lbl_search_status = tk.Label(top, text="Found 0 installed item(s)", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST)
        self.lbl_search_status.pack(side="left", padx=(10, 0))

        # Paned window: list | detail
        pane = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=Theme.BORDER, sashwidth=2, sashrelief=tk.FLAT, bd=0)
        pane.pack(fill="both", expand=True, pady=(4, 4))

        # --- Left: item list ---
        left = tk.Frame(pane, bg=Theme.BG_DARKEST)
        pane.add(left, minsize=200)

        self.listbox = tk.Listbox(
            left, font=("Consolas", 10),
            bg=Theme.BG_MID, fg=Theme.TEXT_BRIGHT,
            selectbackground=Theme.BORDER_LIT, selectforeground=Theme.TEXT_BRIGHT,
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
        self.detail_frame = tk.Frame(pane, bg=Theme.BG_DARKEST)
        pane.add(self.detail_frame, minsize=350)

        # Placeholder
        self.lbl_placeholder = tk.Label(
            self.detail_frame, text="No Local Item Selected",
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
            self.detail_canvas.after_idle(_sync_detail_scrollbar)

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

        # Labels inside detail content
        self.lbl_name = tk.Label(self._detail_content, text="", font=("Montserrat", 14, "bold"), fg=Theme.CYAN, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_name.pack(anchor="w", fill="x", pady=(0, 4))
        self._wrapping_labels.append(self.lbl_name)

        self.lbl_type = tk.Label(self._detail_content, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_type.pack(anchor="w", fill="x")
        self._wrapping_labels.append(self.lbl_type)

        # Version info frame
        ver_frame = tk.Frame(self._detail_content, bg=Theme.BG_DARKEST)
        ver_frame.pack(anchor="w", fill="x", pady=4)

        self.lbl_installed_ver = tk.Label(ver_frame, text="", font=("Montserrat", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_installed_ver.pack(anchor="w")
        self.lbl_latest_ver = tk.Label(ver_frame, text="", font=("Montserrat", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_latest_ver.pack(anchor="w")
        self.lbl_update_status = tk.Label(ver_frame, text="", font=("Montserrat", 9, "bold"), bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_update_status.pack(anchor="w", pady=(4,0))

        self.lbl_size = tk.Label(self._detail_content, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_size.pack(anchor="w", fill="x")
        self._wrapping_labels.append(self.lbl_size)

        sep = tk.Frame(self._detail_content, bg=Theme.BORDER, height=1)
        sep.pack(fill="x", pady=8)

        self.lbl_path_header = tk.Label(self._detail_content, text="Location on Disk:", font=("Montserrat", 9, "bold"), fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST, anchor="w")
        self.lbl_path_header.pack(anchor="w", fill="x")

        self.lbl_path = tk.Label(self._detail_content, text="", font=("Consolas", 9), fg=Theme.TEXT, bg=Theme.BG_DARKEST, anchor="w", justify="left")
        self.lbl_path.pack(anchor="w", fill="x", pady=(2, 6))
        self._wrapping_labels.append(self.lbl_path)

        sep2 = tk.Frame(self._detail_content, bg=Theme.BORDER, height=1)
        sep2.pack(fill="x", pady=8)

        # Action Buttons frame
        btn_frame = tk.Frame(self._detail_content, bg=Theme.BG_DARKEST)
        btn_frame.pack(anchor="w", fill="x", pady=4)

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

        # --- Sample Codes (Examples) section ---
        sep3 = tk.Frame(self._detail_content, bg=Theme.BORDER, height=1)
        sep3.pack(fill="x", pady=8)

        self.lbl_examples_header = tk.Label(
            self._detail_content, text="Sample Codes (Examples):",
            font=("Montserrat", 9, "bold"), fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST, anchor="w"
        )
        self.lbl_examples_header.pack(anchor="w", fill="x")

        self.lbl_examples_hint = tk.Label(
            self._detail_content, text="Double-click a sketch to view its code",
            font=("Montserrat", 8), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w"
        )
        self.lbl_examples_hint.pack(anchor="w", fill="x", pady=(0, 4))

        # Filter entry for examples / boards
        examples_search_wrap = tk.Frame(self._detail_content, bg=Theme.BG_DARKEST)
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

        examples_wrap = tk.Frame(self._detail_content, bg=Theme.BG_DARKEST)
        examples_wrap.pack(anchor="w", fill="both", expand=True)

        self.examples_listbox = tk.Listbox(
            examples_wrap, font=("Consolas", 9), height=8,
            bg=Theme.BG_MID, fg=Theme.TEXT_BRIGHT,
            selectbackground=Theme.BORDER_LIT, selectforeground=Theme.TEXT_BRIGHT,
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
            self.app.root.after(80, lambda: self._start_loading_animation(req_id, next_idx))
        except Exception:
            pass

    def _load_details_async_worker(self, item: dict, req_id: int):
        path = item.get("path", "")
        item_type = item.get("type", "Library")

        # Perform heavy disk I/O scanning on background thread
        size_bytes = self._get_dir_size(path)

        if item_type == "Library":
            examples = self._find_examples(path)
            boards = []
        else:
            examples = []
            boards = self._find_boards(path)

        # Post results back to main thread safely
        def _on_complete():
            if getattr(self, "_select_req_id", 0) != req_id:
                return

            self._loading_complete_req_id = req_id

            if size_bytes > 1024 * 1024:
                self.lbl_size.config(text=f"Size on Disk: {size_bytes / (1024 * 1024):.1f} MB")
            else:
                self.lbl_size.config(text=f"Size on Disk: {size_bytes / 1024:.0f} KB")

            if item_type == "Library":
                self._all_examples = examples
            else:
                self._all_boards = boards

            self._filter_and_render_list(item_override=item)

            if hasattr(self, "detail_canvas"):
                self.detail_canvas.after_idle(self._sync_detail_scrollbar)

        try:
            self.app.root.after(0, _on_complete)
        except Exception:
            pass

    def _on_examples_search_change(self, *args):
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
            if not getattr(self, "_all_examples", None):
                self.examples_listbox.insert(tk.END, "(no sample sketches found)")
                self.examples_listbox.config(state=tk.DISABLED)
                return

            for path in self._all_examples:
                sketch_folder = os.path.basename(os.path.dirname(path))
                display_name = f"{sketch_folder} / {os.path.basename(path)}"
                if not query or query in display_name.lower():
                    self.examples_listbox.insert(tk.END, display_name)
                    self._current_examples.append(path)

            if not self._current_examples:
                self.examples_listbox.insert(tk.END, "(no matching sample sketches found)")
                self.examples_listbox.config(state=tk.DISABLED)
        else:
            self._current_examples = []
            if not getattr(self, "_all_boards", None):
                self.examples_listbox.insert(tk.END, "(no board definitions found)")
                self.examples_listbox.config(state=tk.DISABLED)
                return

            matched_count = 0
            for board in self._all_boards:
                if not query or query in board.lower():
                    self.examples_listbox.insert(tk.END, f"  •  {board}")
                    matched_count += 1

            if matched_count == 0:
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
        _open_code_viewer(path, self._current_examples)

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
        self.installed_items = installed_items
        self._execute_search()

    def _on_search(self, event=None):
        if self._search_after_id is not None:
            self.app.root.after_cancel(self._search_after_id)
        self._search_after_id = self.app.root.after(150, self._execute_search)

    def _execute_search(self):
        self._search_after_id = None
        query = self.search_var.get().lower().strip()
        selected_name = None
        sel = self.listbox.curselection()
        if sel and sel[0] < len(self.filtered_items):
            selected_name = self.filtered_items[sel[0]]["name"]

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

        self.lbl_search_status.config(text=f"Found {len(self.filtered_items)} installed item(s)")

        self.listbox.delete(0, tk.END)
        for item in self.filtered_items:
            up_prefix = "⬆ " if item.get("update_available") else ""
            self.listbox.insert(tk.END, f"{up_prefix}{item['name']} ({item['installed_version']})")

        if selected_name:
            for i, item in enumerate(self.filtered_items):
                if item["name"] == selected_name:
                    self.listbox.selection_set(i)
                    self.listbox.see(i)
                    self._on_select()
                    return

        # Hide detail if nothing
        if hasattr(self, "detail_canvas"):
            self.detail_canvas.pack_forget()
        self.lbl_placeholder.pack(fill="both", expand=True, padx=10, pady=10)
        self._clear_examples()

    def _on_select(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.filtered_items):
            return
        item = self.filtered_items[idx]

        if hasattr(self, "examples_search_var"):
            self.examples_search_var.set("")

        self.lbl_placeholder.pack_forget()
        if hasattr(self, "detail_canvas") and not self.detail_canvas.winfo_ismapped():
            self.detail_canvas.pack(side=tk.LEFT, fill="both", expand=True)

        self.lbl_name.config(text=item["name"])
        self.lbl_type.config(text=f"Type: {item['type']}")

        self.lbl_installed_ver.config(text=f"Installed version: {item['installed_version']}")
        self.lbl_latest_ver.config(text=f"Latest version:    {item['latest_version']}")

        if item.get("update_available"):
            self.lbl_update_status.config(text="⬆ Update available", fg=Theme.YELLOW)
            self.update_btn.config(state="normal")
        else:
            self.lbl_update_status.config(text="✓ Up‑to‑date", fg=Theme.GREEN)
            self.update_btn.config(state="disabled")

        self.lbl_path.config(text=item["path"])

        if item["type"] == "Library":
            self.lbl_examples_header.config(text="Sample Codes (Examples):")
            self.lbl_examples_hint.config(text="Double-click a sketch to view its code")
        else:
            self.lbl_examples_header.config(text="Available Boards:")
            self.lbl_examples_hint.config(text="Supported microcontrollers inside this downloaded platform package")

        # Increment select request ID for async thread cancellation/validation
        self._select_req_id = getattr(self, "_select_req_id", 0) + 1
        req_id = self._select_req_id

        # Show immediate loading state with animated spinner
        self.lbl_size.config(text="Size on Disk: Calculating ⠋...")
        self.examples_listbox.config(state=tk.NORMAL)
        self.examples_listbox.delete(0, tk.END)
        self.examples_listbox.insert(tk.END, "  ⏳ Scanning disk content in background...")
        self.examples_listbox.config(state=tk.DISABLED)

        # Start loading animation spinner
        self._start_loading_animation(req_id)

        # Launch background thread worker for disk I/O scanning!
        threading.Thread(
            target=self._load_details_async_worker,
            args=(item, req_id),
            daemon=True
        ).start()

        if hasattr(self, "detail_canvas"):
            self.detail_canvas.yview_moveto(0)
            self.detail_canvas.after_idle(self._sync_detail_scrollbar)

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
            self.app._compute_installed_items()  # refresh
            self.populate(self.app._installed_items)

    def _update_item(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        item = self.filtered_items[sel[0]]
        if not item.get("update_available"):
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
            self.app._compute_installed_items()
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

    def __init__(self):
        # DPI awareness on Windows
        if sys.platform == "win32":
            try:
                from ctypes import windll
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        self.root = tk.Tk()
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

        self.root.geometry("960x620")
        self.root.minsize(720, 480)
        try:
            self.root.state('zoomed')
        except tk.TclError:
            pass

        self._busy = False
        self._active_download_tab = None
        self._downloading_item_name = None
        self._cancel_event = threading.Event()
        self._keep_alive = True
        self._is_hidden = False

        # Load persisted download directory
        settings = _load_settings()
        saved_dir = settings.get("download_dir", "")
        if saved_dir and os.path.isdir(saved_dir):
            self._download_dir = saved_dir
        else:
            self._download_dir = DEFAULT_DOWNLOAD_DIR
        os.makedirs(self._download_dir, exist_ok=True)
        os.makedirs(INDEX_CACHE_DIR, exist_ok=True)

        # Intercept window close event to hide window instead of destroying process (Sleep Mode)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Save window HWND for instant Win32 unhide
        try:
            hwnd_file = os.path.join(INDEX_CACHE_DIR, ".dm_hwnd")
            with open(hwnd_file, "w", encoding="utf-8") as f:
                f.write(str(self.root.winfo_id()))
        except Exception:
            pass

        # Installed items cache (computed from disk + indexes)
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
        """Poll for wake-up trigger file sent by main MCU Flasher GUI."""
        trigger_file = os.path.join(INDEX_CACHE_DIR, ".show_dm_trigger")
        if os.path.exists(trigger_file):
            try:
                os.remove(trigger_file)
            except Exception:
                pass
            self._unhide_window()
        self.root.after(250, self._check_show_trigger)

    def _unhide_window(self):
        """Instantly restore window from memory without reloading JSON indexes."""
        self.root.deiconify()
        try:
            self.root.state('zoomed')
        except tk.TclError:
            pass
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
        threading.Thread(target=self._wake_sync, daemon=True).start()

    def _wake_sync(self):
        self._compute_installed_items()
        try:
            self.root.after(0, lambda: self.installed_tab.populate(self._installed_items) if hasattr(self, "installed_tab") else None)
        except Exception:
            pass

    def _force_exit(self):
        """Permanently close process and purge memory."""
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

    def _build_ui(self):
        self.root.configure(bg=Theme.BG_DARKEST)

        style = ttk.Style()
        style.theme_use("clam")

        # Configure frames and general layouts
        style.configure("TFrame", background=Theme.BG_DARKEST)
        style.configure("TLabel", background=Theme.BG_DARKEST, foreground=Theme.TEXT)
        style.configure("TNotebook", background=Theme.BG_DARKEST, borderwidth=0)
        style.configure("TNotebook.Tab", background=Theme.BG_MID, foreground=Theme.TEXT_DIM, borderwidth=0, padding=(12, 4))
        style.map("TNotebook.Tab", background=[("selected", Theme.BG_LIGHT)], foreground=[("selected", Theme.TEXT_BRIGHT)])

        style.configure("TCombobox",
                         fieldbackground=Theme.BG_LIGHT,
                         background=Theme.BG_HOVER,
                         foreground=Theme.TEXT_BRIGHT,
                         selectbackground=Theme.CYAN_DIM,
                         selectforeground=Theme.TEXT_BRIGHT,
                         bordercolor=Theme.BORDER,
                         arrowcolor=Theme.TEXT_DIM)
        style.map("TCombobox",
                   fieldbackground=[("readonly", Theme.BG_LIGHT)],
                   selectbackground=[("readonly", Theme.CYAN_DIM)],
                   selectforeground=[("readonly", Theme.TEXT_BRIGHT)])

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

        # Top bar with title + refresh
        top_bar = tk.Frame(self.root, bg=Theme.BG_DARK, pady=6, padx=10)
        top_bar.pack(fill="x")
        tk.Frame(self.root, bg=Theme.BORDER, height=1).pack(fill="x")

        tk.Label(top_bar, text="Arduino Library & Board Browser",
                 font=("Montserrat", 12, "bold"), fg=Theme.CYAN, bg=Theme.BG_DARK).pack(side="left")

        self.quit_btn = make_flat_button(
            top_bar, "✖ Quit & Purge", self._force_exit,
            Theme.BTN_STOP, Theme.BTN_STOP_H
        )
        self.quit_btn.config(fg=Theme.TEXT_BRIGHT)
        self.quit_btn.pack(side="right", padx=(6, 0))

        self.refresh_btn = make_flat_button(
            top_bar, "⟳ Refresh All", self._refresh_all,
            Theme.BTN_MONITOR, Theme.BTN_MONITOR_H
        )
        self.refresh_btn.pack(side="right")

        # Download folder bar
        folder_bar = tk.Frame(self.root, bg=Theme.BG_MID, pady=8, padx=10)
        folder_bar.pack(fill="x")
        tk.Frame(self.root, bg=Theme.BORDER, height=1).pack(fill="x")

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
            folder_bar, "📂 Browse…", self._choose_download_dir,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H
        )
        self.browse_btn.pack(side="right")

        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # --- Libraries tab ---
        lib_frame = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(lib_frame, text="  📚 Libraries  ")
        self.lib_tab = BrowseTab(
            lib_frame, self,
            detail_builder=self._build_library_detail,
            on_select_handler=self._on_library_select,
        )

        # --- Boards tab ---
        board_frame = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(board_frame, text="  🔌 Boards  ")
        self.board_tab = BrowseTab(
            board_frame, self,
            detail_builder=self._build_board_detail,
            on_select_handler=self._on_board_select,
        )

        # --- Installed tab ---
        installed_frame = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(installed_frame, text="  💾 Installed  ")
        self.installed_tab = InstalledTab(installed_frame, self)

        # Bind tab selection to recompute installed items when entering the tab
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Bottom bar: progress + status
        bottom = tk.Frame(self.root, bg=Theme.BG_DARK, pady=6, padx=10)
        bottom.pack(fill="x")
        tk.Frame(self.root, bg=Theme.BORDER, height=1).pack(side="bottom", fill="x")

        self.progress = ttk.Progressbar(bottom, style="Horizontal.TProgressbar", length=200)
        self.progress.pack(side="left")

        self.status_var = tk.StringVar(value="Starting…")
        tk.Label(bottom, textvariable=self.status_var, font=("Montserrat", 9),
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARK).pack(side="left", padx=10)

    def _on_tab_changed(self, event=None):
        try:
            selected_tab = self.notebook.index(self.notebook.select())
            if selected_tab == 2:  # Installed tab
                self._compute_installed_items_async()
        except Exception:
            pass

    def _compute_installed_items_async(self):
        self._set_status("Scanning installed libraries & cores ⠋...")
        try:
            self.progress.start(10)
        except Exception:
            pass

        def _worker():
            self._compute_installed_items()

            def _ui_done():
                try:
                    self.progress.stop()
                except Exception:
                    pass
                self._set_status("Ready")
                if hasattr(self, "installed_tab"):
                    self.installed_tab.populate(self._installed_items)

            try:
                self.root.after(0, _ui_done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

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
        _save_settings(settings)
        self._set_status(f"Download folder set to {new_path}")
        # Refresh everything that depends on the download path
        self._compute_installed_items()
        self.installed_tab.populate(self._installed_items)
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)

    def _cancel_download(self):
        self._cancel_event.set()

    # ------------------------------------------------------------------
    # Library detail panel
    # ------------------------------------------------------------------

    def _build_library_detail(self, tab: BrowseTab):
        dc = tab._detail_content
        dc.configure(bg=Theme.BG_DARKEST)

        tab.lbl_name = tk.Label(dc, text="", font=("Montserrat", 14, "bold"), fg=Theme.CYAN, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_name.pack(anchor="w", fill="x", pady=(0, 4))
        tab._wrapping_labels.append(tab.lbl_name)

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

        tab.lbl_available = tk.Label(ver_frame, text="", font=("Montserrat", 9, "bold"), fg=Theme.GREEN, bg=Theme.BG_DARKEST)
        tab.lbl_available.pack(side="left", padx=4)

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
        tab.lbl_name.pack(anchor="w", fill="x", pady=(0, 4))
        tab._wrapping_labels.append(tab.lbl_name)

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

        tab.lbl_available = tk.Label(ver_frame, text="", font=("Montserrat", 9, "bold"), fg=Theme.GREEN, bg=Theme.BG_DARKEST)
        tab.lbl_available.pack(side="left", padx=4)

        tab.lbl_size = tk.Label(dc, text="", font=("Montserrat", 9), fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor="w")
        tab.lbl_size.pack(anchor="w", pady=2)

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

    # ------------------------------------------------------------------
    # Shared detail helpers
    # ------------------------------------------------------------------

    def _update_version_status(self, tab: BrowseTab):
        sel = tab.listbox.curselection()
        if not sel:
            return
        name = tab.filtered_names[sel[0]]
        item = tab.all_items[name]
        ver = tab.version_var.get()

        target_version = None
        for v in item["versions"]:
            if v["version"] == ver:
                target_version = v
                break

        if not target_version:
            return

        size_val = target_version["size"]
        try:
            size_val = int(size_val)
        except (ValueError, TypeError):
            size_val = 0
        size_kb = size_val / 1024
        if size_kb > 1024:
            tab.lbl_size.config(text=f"Size: {size_kb / 1024:.1f} MB")
        else:
            tab.lbl_size.config(text=f"Size: {size_kb:.0f} KB")

        url = target_version["url"]
        archive = target_version["archiveFileName"] or url.split("/")[-1]
        subfolder = "Libs" if tab == self.lib_tab else "Boards"
        dest_dir = os.path.join(self._download_dir, subfolder)
        
        filepath = os.path.join(dest_dir, archive)
        folder_path = os.path.join(dest_dir, _get_folder_name(archive))
        
        is_available = os.path.isfile(filepath) or os.path.isdir(folder_path)

        if self._busy and tab == self._active_download_tab and item["name"] == self._downloading_item_name:
            tab.lbl_available.config(text="")
            tab.download_btn.config(text="✕ Cancel", command=self._cancel_download, state="normal")
        else:
            if is_available:
                tab.lbl_available.config(text="Already Available")
                tab.download_btn.config(text="⬇ Download", command=lambda t=tab: self._download(t), state="disabled")
            else:
                tab.lbl_available.config(text="")
                if self._busy:
                    tab.download_btn.config(text="⬇ Download", command=lambda t=tab: self._download(t), state="disabled")
                else:
                    tab.download_btn.config(text="⬇ Download", command=lambda t=tab: self._download(t), state="normal")

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

    def _compute_installed_items(self):
        """Scan download folders against loaded indexes to determine what is
        installed and whether an update is available."""
        items = []

        # Helper: for a given index item (lib or board) and type, check
        # if any of its versions exist on disk.
        def check_index_item(name: str, index_entry: dict, subfolder: str, type_label: str):
            latest_version = index_entry["versions"][0]["version"]  # sorted newest first
            # Walk versions from newest to oldest, first match = installed version
            for ver_entry in index_entry["versions"]:
                archive = ver_entry["archiveFileName"] or ver_entry["url"].split("/")[-1]
                dest_dir = os.path.join(self._download_dir, subfolder)
                filepath = os.path.join(dest_dir, archive)
                folder_path = os.path.join(dest_dir, _get_folder_name(archive))
                if os.path.isdir(folder_path) or os.path.isfile(filepath):
                    installed_version = ver_entry["version"]
                    # Prefer folder if it exists
                    path = folder_path if os.path.isdir(folder_path) else filepath
                    update_available = _version_key(latest_version) > _version_key(installed_version)
                    items.append({
                        "type": type_label,
                        "name": name,
                        "installed_version": installed_version,
                        "latest_version": latest_version,
                        "update_available": update_available,
                        "path": path,
                        "archive": archive,
                    })
                    break  # only report the newest installed version

        # Libraries
        if self.lib_tab.all_items:
            for name, entry in self.lib_tab.all_items.items():
                check_index_item(name, entry, "Libs", "Library")

        # Boards
        if self.board_tab.all_items:
            for name, entry in self.board_tab.all_items.items():
                check_index_item(name, entry, "Boards", "Board Platform")

        # Sort alphabetically
        items.sort(key=lambda x: x["name"].lower())
        self._installed_items = items

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _initial_load(self):
        """Called once on startup. Loads both indexes with circular loading overlay."""
        self._set_status("Loading indexes…")
        if not hasattr(self, "_loading_overlay") or not self._loading_overlay or not self._loading_overlay.winfo_exists():
            try:
                self._loading_overlay = CircularLoadingOverlay(
                    self.notebook,
                    title="Loading Arduino Library & Board Indexes...",
                    subtitle="Downloading & parsing package definitions on background thread..."
                )
                self._loading_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            except Exception:
                pass
        self._start_thread(self._load_both)

    def _refresh_all(self):
        if self._busy:
            return
        self._set_status("Refreshing all indexes…")
        if not hasattr(self, "_loading_overlay") or not self._loading_overlay or not self._loading_overlay.winfo_exists():
            try:
                self._loading_overlay = CircularLoadingOverlay(
                    self.notebook,
                    title="Refreshing All Indexes...",
                    subtitle="Downloading newest library & board definitions..."
                )
                self._loading_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            except Exception:
                pass
        self._start_thread(lambda: self._load_both(force_refresh=True))

    def _cache_is_fresh(self, cache_file: str) -> bool:
        if not os.path.isfile(cache_file):
            return False
        age = time.time() - os.path.getmtime(cache_file)
        return age < CACHE_MAX_AGE_SECONDS

    def _start_thread(self, target):
        self._busy = True
        self.refresh_btn.config(state="disabled")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        t = threading.Thread(target=target, daemon=True)
        t.start()

    def _load_both(self, force_refresh=False):
        """Load both library and board indexes (runs in worker thread)."""
        libs = {}
        boards = {}

        # --- Libraries ---
        lib_data = self._load_index(
            LIBRARY_INDEX_URL, LIBRARY_CACHE_FILE, force_refresh, "library"
        )
        if lib_data is not None:
            libs = _group_libraries(lib_data.get("libraries", []))
            self.root.after(0, self.lib_tab.populate, libs)

        # --- Boards ---
        board_data = self._load_index(
            BOARD_INDEX_URL, BOARD_CACHE_FILE, force_refresh, "board"
        )
        if board_data is not None:
            boards = _group_boards(board_data.get("packages", []))
            self.root.after(0, self.board_tab.populate, boards)

        # Final status and installed recompute
        lib_count = len(libs) if lib_data else 0
        board_count = len(boards) if board_data else 0
        self.root.after(0, self._finish_load,
                        f"{lib_count} libraries, {board_count} board platforms loaded")

    def _load_index(self, url: str, cache_file: str,
                    force_refresh: bool, label: str) -> dict | None:
        if not force_refresh and self._cache_is_fresh(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass

        def _update_overlay():
            self._set_status(f"Downloading {label} index…")
            if hasattr(self, "_loading_overlay") and self._loading_overlay and self._loading_overlay.winfo_exists():
                self._loading_overlay.update_message(
                    title=f"Downloading {label.title()} Index...",
                    subtitle="Downloading index from Arduino servers on background thread..."
                )

        self.root.after(0, _update_overlay)
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            self.root.after(0, self._set_status,
                           f"Failed to download {label} index")
            return None

        try:
            data = resp.json()
        except ValueError:
            self.root.after(0, self._set_status,
                           f"Invalid JSON from {label} index")
            return None

        try:
            os.makedirs(INDEX_CACHE_DIR, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                fh.write(resp.text)
        except OSError:
            pass

        return data

    def _finish_load(self, msg: str):
        if hasattr(self, "_loading_overlay") and self._loading_overlay and self._loading_overlay.winfo_exists():
            try:
                self._loading_overlay.stop_and_destroy()
            except Exception:
                pass
            self._loading_overlay = None

        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._set_status(msg)
        self._busy = False
        self.refresh_btn.config(state="normal")
        # Update installed items and refresh installed tab if it's visible
        self._compute_installed_items()
        try:
            if self.notebook.index(self.notebook.select()) == 2:
                self.installed_tab.populate(self._installed_items)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _prompt_download_option(self, archive_name) -> str:
        dialog = tk.Toplevel(self.root)
        dialog.title("Download Options")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=Theme.BG_DARKEST)

        result = tk.StringVar(value="")

        lbl = tk.Label(
            dialog,
            text=f"Choose format for:\n{archive_name}",
            font=("Montserrat", 10), justify="center", anchor="center", wraplength=700,
            fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST
        )
        lbl.pack(pady=15)

        btn_frame = tk.Frame(dialog, bg=Theme.BG_DARKEST)
        btn_frame.pack(fill="x", padx=20)

        def select_option(opt):
            result.set(opt)
            dialog.destroy()

        btn_zip = make_flat_button(btn_frame, "ZIP Only (Default)", lambda: select_option("zip"), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H)
        btn_zip.pack(side="left", padx=8, expand=True, fill="x")

        btn_folder = make_flat_button(btn_frame, "Folder Only", lambda: select_option("folder"), Theme.BTN_MONITOR, Theme.BTN_MONITOR_H)
        btn_folder.pack(side="left", padx=8, expand=True, fill="x")

        btn_both = make_flat_button(btn_frame, "Both (ZIP & Folder)", lambda: select_option("both"), Theme.BTN_COMPILE, Theme.BTN_COMPILE_H)
        btn_both.pack(side="left", padx=8, expand=True, fill="x")

        cancel_frame = tk.Frame(dialog, bg=Theme.BG_DARKEST)
        cancel_frame.pack(fill="x", pady=10)
        btn_cancel = make_flat_button(cancel_frame, "Cancel", dialog.destroy, Theme.BTN_STOP, Theme.BTN_STOP_H)
        btn_cancel.pack(pady=5)

        dialog.update_idletasks()
        req_w = max(780, dialog.winfo_reqwidth() + 40)
        req_h = dialog.winfo_reqheight() + 10
        x = self.root.winfo_x() + (self.root.winfo_width() - req_w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - req_h) // 2
        dialog.geometry(f"{req_w}x{req_h}+{x}+{y}")

        btn_zip.focus_set()

        self.root.wait_window(dialog)
        return result.get()

    def _download(self, tab: BrowseTab):
        sel = tab.listbox.curselection()
        if not sel or self._busy:
            return
        name = tab.filtered_names[sel[0]]
        item = tab.all_items[name]
        ver = tab.version_var.get()

        url = ""
        archive = ""
        for v in item["versions"]:
            if v["version"] == ver:
                url = v["url"]
                archive = v["archiveFileName"] or url.split("/")[-1]
                break

        if not url:
            messagebox.showerror("Error", f"No download URL found for version '{ver or '(none)'}'.")
            return

        subfolder = "Libs" if tab == self.lib_tab else "Boards"
        dest_dir = os.path.join(self._download_dir, subfolder)
        os.makedirs(dest_dir, exist_ok=True)

        download_option = self._prompt_download_option(archive)
        if not download_option:
            return

        self._busy = True
        self._active_download_tab = tab
        self._downloading_item_name = name
        self._cancel_event.clear()
        tab.download_btn.config(text="✕ Cancel", command=self._cancel_download, state="normal")
        self._set_status(f"Downloading {archive}…")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)

        t = threading.Thread(target=self._download_worker,
                             args=(tab, url, archive, dest_dir, download_option), daemon=True)
        t.start()

    def _download_update(self, name: str, is_board: bool, old_path: str = "", old_archive: str = ""):
        if self._busy:
            return

        tab = self.board_tab if is_board else self.lib_tab
        item = tab.all_items.get(name)
        if not item:
            messagebox.showerror("Error", f"Item '{name}' not found in index.")
            return

        ver = item["versions"][0]["version"]
        url = ""
        archive = ""
        for v in item["versions"]:
            if v["version"] == ver:
                url = v["url"]
                archive = v["archiveFileName"] or url.split("/")[-1]
                break

        if not url:
            messagebox.showerror("Error", f"No download URL found for version '{ver}'.")
            return

        subfolder = "Boards" if is_board else "Libs"
        dest_dir = os.path.join(self._download_dir, subfolder)
        os.makedirs(dest_dir, exist_ok=True)

        # Delete the old version before downloading the update
        if old_path and os.path.exists(old_path):
            try:
                import shutil
                if os.path.isdir(old_path):
                    shutil.rmtree(old_path)
                else:
                    os.remove(old_path)
            except Exception:
                pass
        if old_archive:
            old_archive_path = os.path.join(dest_dir, old_archive)
            if os.path.isfile(old_archive_path):
                try:
                    os.remove(old_archive_path)
                except Exception:
                    pass

        download_option = self._prompt_download_option(archive)
        if not download_option:
            return

        self._busy = True
        self._active_download_tab = tab
        self._downloading_item_name = name
        self._cancel_event.clear()

        tab.download_btn.config(text="✕ Cancel", command=self._cancel_download, state="normal")
        self._set_status(f"Downloading {archive}…")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)

        t = threading.Thread(target=self._download_worker,
                             args=(tab, url, archive, dest_dir, download_option), daemon=True)
        t.start()

    def _download_worker(self, tab: BrowseTab, url: str, archive: str, dest_dir: str, download_option: str):
        try:
            resp = requests.get(url, stream=True, timeout=120)
            resp.raise_for_status()

            os.makedirs(dest_dir, exist_ok=True)
            filepath = os.path.join(dest_dir, archive)

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            if total:
                self.root.after(0, self._set_progress_determinate, total)

            with open(filepath, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    if self._cancel_event.is_set():
                        fh.close()
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        self.root.after(0, self._download_cancelled, tab)
                        return

                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        self.root.after(0, self._update_progress,
                                        downloaded, total)

            if download_option in ("folder", "both"):
                self.root.after(0, self._set_status, "Extracting files…")
                folder_path = os.path.join(dest_dir, _get_folder_name(archive))
                os.makedirs(folder_path, exist_ok=True)
                _extract_archive(filepath, folder_path)

            if download_option == "folder":
                try:
                    os.remove(filepath)
                except OSError:
                    pass

            self.root.after(0, self._download_done, tab, filepath if download_option != "folder" else folder_path)

        except requests.exceptions.RequestException as e:
            if self._cancel_event.is_set():
                self.root.after(0, self._download_cancelled, tab)
            else:
                self.root.after(0, self._download_error, tab,
                               f"Download failed:\n{e}")
        except OSError as e:
            self.root.after(0, self._download_error, tab,
                           f"File write error:\n{e}")
        except Exception as e:
            import traceback
            self.root.after(0, self._download_error, tab,
                           f"Unexpected error:\n{e}\n\n{traceback.format_exc()}")

    def _set_progress_determinate(self, total):
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=total, value=0)

    def _update_progress(self, downloaded, total):
        self.progress.config(value=downloaded)
        pct = int(downloaded / total * 100) if total else 0
        self._set_status(f"Downloading… {pct}%")

    def _download_done(self, tab: BrowseTab, filepath):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._set_status("Download complete")
        self._busy = False
        tab.download_btn.config(text="⬇ Download", command=lambda t=tab: self._download(t), state="normal")
        self._active_download_tab = None
        self._downloading_item_name = None
        
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)
        # Refresh installed list
        self._compute_installed_items()
        try:
            if self.notebook.index(self.notebook.select()) == 2:
                self.installed_tab.populate(self._installed_items)
        except Exception:
            pass
        
        messagebox.showinfo("Done", f"Saved:\n{filepath}")

    def _download_cancelled(self, tab: BrowseTab):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._set_status("Download cancelled")
        self._busy = False
        tab.download_btn.config(text="⬇ Download", command=lambda t=tab: self._download(t), state="normal")
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
        self._active_download_tab = None
        self._downloading_item_name = None
        
        self._update_version_status(self.lib_tab)
        self._update_version_status(self.board_tab)
        messagebox.showerror("Download Error", msg)

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
    app = ArduinoBrowser()
    app.run()