"""
GUI downloader for direct download links and Google Drive share links.

Setup:
    pip install requests filetype

Run:
    python downloader.py
"""

import socket
import re
import threading
import mimetypes
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

if __name__ == "__main__" and sys.platform.startswith("linux"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.modules.ubuntu_release import enforce_supported_ubuntu
    if not enforce_supported_ubuntu():
        raise SystemExit(1)

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def check_internet_connection(timeout: float = 2.0) -> bool:
    """Robust, fast multi-layer check for active internet connection.

    Uses concurrent probing over port 443 (HTTPS) and HTTP captive-portal probes
    (which respect system/user proxies), with Windows WinINet fallback. Never
    relies on TCP port 53, which is routinely blocked by firewalls and ISPs.
    """
    success = threading.Event()

    def _test_tcp(host: str, port: int) -> None:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            success.set()
        except Exception:
            pass

    def _test_http(url: str) -> None:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 204, 301, 302):
                    success.set()
        except Exception:
            pass

    targets_tcp = [
        ("1.1.1.1", 443),
        ("8.8.8.8", 443),
        ("1.0.0.1", 443),
        ("9.9.9.9", 443),
    ]
    urls = [
        "http://www.msftconnecttest.com/connecttest.txt",
        "http://clients3.google.com/generate_204",
        "https://cloudflare.com",
    ]
    host_targets = [
        ("cloudflare.com", 443),
        ("github.com", 443),
    ]

    threads = []
    for host, port in targets_tcp:
        t = threading.Thread(target=_test_tcp, args=(host, port), daemon=True)
        t.start()
        threads.append(t)

    for url in urls:
        t = threading.Thread(target=_test_http, args=(url,), daemon=True)
        t.start()
        threads.append(t)

    for host, port in host_targets:
        t = threading.Thread(target=_test_tcp, args=(host, port), daemon=True)
        t.start()
        threads.append(t)

    success.wait(timeout=timeout)
    if success.is_set():
        return True

    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes
            flags = ctypes.wintypes.DWORD()
            if ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0):
                return True
        except Exception:
            pass

    return False

try:
    import filetype  # type: ignore
    HAS_FILETYPE = True
except ImportError:
    filetype = None  # type: ignore
    HAS_FILETYPE = False


# ---------- download helpers (same logic as CLI version) ----------

def get_filename_from_response(response, fallback_stem="downloaded_file"):
    content_disposition = response.headers.get("content-disposition")
    if content_disposition:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if match:
            return match.group(1)
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type) if content_type else None
    return f"{fallback_stem}{ext}" if ext else fallback_stem


def detect_extension_from_bytes(filepath: Path):
    if not HAS_FILETYPE or filetype is None:
        return ""
    kind = filetype.guess(str(filepath))
    return f".{kind.extension}" if kind else ""


def extract_gdrive_file_id(url: str):
    """
    Returns the Google Drive file ID from a share/download/open link,
    or None if the URL is not a Google Drive file link.
    """
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"/uc\?.*?id=([a-zA-Z0-9_-]+)",
        r"/open\?id=([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _stream_to_file(response, destination_dir: Path, progress_callback, cancel_event, fallback_stem):
    """
    Streams an already-open requests.Response to disk.
    Returns the final Path, or raises an exception on failure.
    """
    if response.status_code != 200:
        raise RuntimeError(f"Download failed with status {response.status_code}")

    total_size = int(response.headers.get("content-length", 0))
    filename = get_filename_from_response(response, fallback_stem=fallback_stem)
    temp_path = destination_dir / filename

    downloaded = 0
    with open(temp_path, "wb") as f:
        for chunk in response.iter_content(32768):
            if cancel_event.is_set():
                f.close()
                temp_path.unlink(missing_ok=True)
                raise RuntimeError("Download cancelled")
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                progress_callback(downloaded, total_size)

    if not temp_path.suffix:
        ext = detect_extension_from_bytes(temp_path)
        if ext:
            final_path = temp_path.with_suffix(ext)
            temp_path.rename(final_path)
            return final_path
    return temp_path


def _extract_download_form_url(html: str):
    """
    Google Drive confirmation pages contain a <form> whose action URL plus
    hidden inputs (id, export, confirm, uuid) point to the real download.
    Returns the constructed URL, or None if the page has no download form.
    """
    form = re.search(r'<form[^>]+id="download-form"[^>]+action="([^"]+)"', html)
    if not form:
        return None
    fields = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html)
    if not fields:
        return None
    action = form.group(1)
    params = "&".join(f"{name}={quote(value)}" for name, value in fields)
    return f"{action}?{params}"


def download_gdrive_link(url: str, destination_dir: Path, progress_callback, cancel_event):
    """
    Downloads a file from a Google Drive share link.
    Handles virus-scan confirmation pages (including files too large to scan).
    Returns the final Path, or raises an exception on failure.
    """
    file_id = extract_gdrive_file_id(url)
    if not file_id:
        raise RuntimeError("Could not extract a file ID from the Google Drive link")

    session = requests.Session()
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    response = session.get(download_url, stream=True, timeout=15)

    for _ in range(3):
        if not response.headers.get("content-type", "").lower().startswith("text/html"):
            break
        page = response.text
        response.close()
        form_url = _extract_download_form_url(page)
        if form_url:
            download_url = form_url
        else:
            confirm = re.search(r'name="confirm" value="([0-9A-Za-z_-]+)"', page)
            if confirm:
                download_url = f"https://drive.google.com/uc?export=download&confirm={confirm.group(1)}&id={file_id}"
            else:
                raise RuntimeError(
                    "Google Drive returned a page instead of the file "
                    "(the link may be private or unavailable)"
                )
        response = session.get(download_url, stream=True, timeout=15)

    if response.headers.get("content-type", "").lower().startswith("text/html"):
        raise RuntimeError("Google Drive returned an HTML page instead of the file")

    return _stream_to_file(response, destination_dir, progress_callback, cancel_event, "downloaded_file")


def download_direct_link(url: str, destination_dir: Path, progress_callback, cancel_event):
    """
    Downloads from a direct URL, or a Google Drive link if detected.
    progress_callback(downloaded_bytes, total_bytes) is called after every chunk.
    cancel_event is a threading.Event; download stops early if it's set.
    Returns the final Path, or raises an exception on failure.
    """
    if "drive.google.com" in url:
        return download_gdrive_link(url, destination_dir, progress_callback, cancel_event)

    response = requests.get(url, stream=True, timeout=15)
    return _stream_to_file(response, destination_dir, progress_callback, cancel_event, "downloaded_file")


# ---------- GUI ----------

class DownloaderApp(tk.Tk):
    def __init__(self, *, theme_mode=None):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from src.modules.tk_font_runtime import register_tk_fonts
        from main.core.theme import Theme
        from types import SimpleNamespace
        from src.modules.ui_palette import frosted_palette
        register_tk_fonts()
        if theme_mode is None:
            from main.core.config import get_theme_mode
            theme_mode = get_theme_mode()
        mode = theme_mode if theme_mode in Theme.PALETTES else "default"
        self.palette = SimpleNamespace(**frosted_palette(Theme.PALETTES[mode]), active_theme=mode)
        super().__init__()
        self.title("MCU Flasher — Download files")
        from src.modules.tk_glass import DialogFit
        self._window_fit = DialogFit(self, preferred=(660, 420), minimum=(420, 320))
        self.configure(bg=self.palette.WINDOW_BG)
        self.download_dir = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.status_text = tk.StringVar(value="Paste a direct link or Google Drive share link to download a file.")
        self.cancel_event = threading.Event()
        self.worker_thread = None
        self._build_widgets()

    def _build_widgets(self):
        from src.modules.tk_glass import BrowserHeader, GlassButton, GlassCard, FieldRow, ScrollForm, EntryWell
        from src.modules.ui_palette import readable_foreground
        p = self.palette
        self._header = BrowserHeader(self, p, title="Download files", subtitle="Direct links and Google Drive")
        self._header.pack(fill="x", padx=12, pady=(12, 10))
        self._form = ScrollForm(self, p.WINDOW_BG, fill_viewport=True)
        self._form.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._card = GlassCard(self._form.body, p, padding=14)
        self._card.pack(fill="both", expand=True)
        self._card.bind("<<GlassRequestChanged>>", lambda _event: self._form.request_fit(), add="+")
        body = self._card.body

        def label(parent, text):
            return tk.Label(parent, text=text, font=("Montserrat", 10), bg=p.BG_MID, fg=p.TEXT,
                            anchor="w", bd=0)

        def entry(parent, **options):
            return EntryWell(parent, p, font=("Montserrat", 10), **options)

        def button(parent, text, callback, *, primary=False, state="normal"):
            base, hover = (p.BTN_MONITOR, p.BTN_MONITOR_H) if primary else (p.BTN_CLEAR, p.BTN_CLEAR_H)
            return GlassButton(parent, text=text, command=callback, state=state,
                               font=("Montserrat", 9, "bold"), bg=base, activebackground=hover,
                               fg=readable_foreground(base), activeforeground=readable_foreground(hover),
                               disabledforeground=p.TEXT_DIM, relief="flat", bd=0,
                               highlightthickness=1, highlightbackground=p.BG_MID, highlightcolor=p.CYAN)

        label(body, "Download link").pack(fill="x", pady=(0, 7))
        self._url_well = entry(body, width=1)
        self.url_entry = self._url_well.entry
        self._url_well.pack(fill="x", ipady=5, pady=(0, 14))
        folder = tk.Frame(body, bg=p.BG_MID)
        folder.pack(fill="x", pady=(0, 16))
        self._folder_well = entry(folder, textvariable=self.download_dir, width=1)
        self.folder_entry = self._folder_well.entry
        self.browse_btn = button(folder, "Browse…", self._choose_dir)
        self._folder_row = FieldRow(folder, label(folder, "Save to"), self._folder_well, self.browse_btn)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar", background=p.BG_MID, troughcolor=p.WINDOW_BG,
                        bordercolor=p.WINDOW_BG, arrowcolor=p.TEXT_DIM,
                        lightcolor=p.BG_MID, darkcolor=p.BG_MID)
        style.map("Vertical.TScrollbar", background=[("active", p.BG_HOVER)])
        style.configure("FileDownload.Horizontal.TProgressbar", background=p.CYAN,
                        troughcolor=p.BG_DARK, borderwidth=0, lightcolor=p.CYAN, darkcolor=p.CYAN)
        self.progress = ttk.Progressbar(body, orient="horizontal", mode="determinate",
                                       style="FileDownload.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(0, 10))
        self.status_label = label(body, "")
        self.status_label.configure(textvariable=self.status_text, justify="left", fg=p.TEXT_DIM)
        self.status_label.pack(fill="x", pady=(0, 16))
        body.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(80, event.width - 4)), add="+")
        actions = tk.Frame(body, bg=p.BG_MID)
        actions.pack(fill="x")
        self.download_btn = button(actions, "Download", self._start_download, primary=True)
        self.download_btn.pack(side="right")
        self.cancel_btn = button(actions, "Cancel", self._cancel_download, state="disabled")
        self.cancel_btn.pack(side="right", padx=(0, 8))

    def _choose_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.download_dir.get())
        if chosen:
            self.download_dir.set(chosen)

    def _start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Missing link", "Please paste a download link first.")
            return

        if not check_internet_connection():
            proceed = messagebox.askyesno(
                "Internet Connection Warning",
                "Could not verify an active internet connection.\n\n"
                "Downloads typically require an active internet connection.\n\n"
                "Would you like to attempt the download anyway?",
                parent=self,
            )
            if not proceed:
                return

        dest_dir = Path(self.download_dir.get())
        dest_dir.mkdir(parents=True, exist_ok=True)

        self.cancel_event.clear()
        self.progress["value"] = 0
        self.status_text.set("Starting download...")
        self.download_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")

        self.worker_thread = threading.Thread(
            target=self._run_download, args=(url, dest_dir), daemon=True
        )
        self.worker_thread.start()

    def _run_download(self, url, dest_dir):
        def on_progress(downloaded, total):
            self.after(0, self._update_progress, downloaded, total)

        try:
            final_path = download_direct_link(url, dest_dir, on_progress, self.cancel_event)
            self.after(0, self._on_success, final_path)
        except Exception as e:
            self.after(0, self._on_error, str(e))

    def _update_progress(self, downloaded, total):
        if total > 0:
            percent = (downloaded / total) * 100
            self.progress["value"] = percent
            self.status_text.set(f"Downloading... {downloaded / (1024*1024):.2f} MB / {total / (1024*1024):.2f} MB ({percent:.1f}%)")
        else:
            self.progress["mode"] = "indeterminate"
            self.status_text.set(f"Downloading... {downloaded / (1024*1024):.2f} MB (size unknown)")

    def _on_success(self, final_path):
        self.progress["value"] = 100
        self.status_text.set(f"Done. Saved to:\n{final_path}")
        self.download_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    def _on_error(self, message):
        self.status_text.set(f"Error: {message}")
        self.download_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    def _cancel_download(self):
        self.cancel_event.set()
        self.status_text.set("Cancelling...")
        self.cancel_btn.config(state="disabled")


if __name__ == "__main__":
    app = DownloaderApp()
    app.mainloop()