"""
GUI downloader for direct download links and Google Drive share links.

Setup:
    pip install requests filetype

Run:
    python downloader.py
"""

import os
import socket
import re
import threading
import mimetypes
from pathlib import Path

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def check_internet_connection(timeout: float = 2.0) -> bool:
    """Fast socket check for active internet connection."""
    test_targets = [
        ("1.1.1.1", 53),
        ("8.8.8.8", 53),
        ("google.com", 80),
    ]
    for host, port in test_targets:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
        except Exception:
            continue
    return False

try:
    # pyrefly: ignore [missing-import]
    import filetype
    HAS_FILETYPE = True
except ImportError:
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
    if not HAS_FILETYPE:
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
    params = "&".join(f"{name}={requests.utils.quote(value)}" for name, value in fields)
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
    def __init__(self):
        super().__init__()
        self.title("Drive Link Downloader")
        self.geometry("520x260")
        self.resizable(False, False)

        self.download_dir = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.status_text = tk.StringVar(value="Paste a download link (direct or Google Drive) and click Download.")
        self.cancel_event = threading.Event()
        self.worker_thread = None

        self._build_widgets()

    def _build_widgets(self):
        pad = {"padx": 12, "pady": 6}

        # URL entry
        tk.Label(self, text="Download link (direct or Google Drive):").pack(anchor="w", **pad)
        self.url_entry = tk.Entry(self, width=70)
        self.url_entry.pack(fill="x", padx=12)

        # Destination folder
        dest_frame = tk.Frame(self)
        dest_frame.pack(fill="x", **pad)
        tk.Label(dest_frame, text="Save to:").pack(side="left")
        tk.Entry(dest_frame, textvariable=self.download_dir, width=45).pack(
            side="left", padx=6, fill="x", expand=True
        )
        tk.Button(dest_frame, text="Browse...", command=self._choose_dir).pack(side="left")

        # Progress bar
        self.progress = ttk.Progressbar(self, orient="horizontal", mode="determinate", length=480)
        self.progress.pack(padx=12, pady=(16, 4))

        # Status label
        tk.Label(self, textvariable=self.status_text, anchor="w", wraplength=480, justify="left").pack(
            fill="x", padx=12
        )

        # Buttons
        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=14)
        self.download_btn = tk.Button(btn_frame, text="Download", width=14, command=self._start_download)
        self.download_btn.pack(side="left", padx=6)
        self.cancel_btn = tk.Button(
            btn_frame, text="Cancel", width=14, command=self._cancel_download, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=6)

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
            messagebox.showwarning(
                "No Internet Connection",
                "Cannot start download because you are currently offline.\n\n"
                "Please check your network connection and try again.",
                parent=self,
            )
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