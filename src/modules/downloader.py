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

if __name__ == "__main__" and sys.platform != "win32":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.modules.ubuntu_release import enforce_supported_ubuntu
    if not enforce_supported_ubuntu():
        raise SystemExit(1)

import requests


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

def create_window(*, theme_mode=None):
    """Build the window on demand; importing this module must not load Qt."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                                   QMessageBox, QProgressBar, QVBoxLayout)
    from main.core.config import get_theme_mode
    from main.core.theme import Theme
    from main.qt.glass import GlassWorkspace
    from main.qt.icons import ActionButton
    from main.qt.responsive import ScreenWatcher, fit_dialog
    from main.qt.theme import build_stylesheet, install_no_focus_lines, register_fonts

    class Reports(QObject):
        """Queued delivery from the transfer worker to the GUI thread."""
        progress = Signal(int, int)
        success = Signal(str)
        failure = Signal(str)

    class DownloaderWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("MCU Flasher — Download files")
            self.cancel_event = threading.Event()
            self.worker_thread = None
            self.reports = Reports(self)
            self.reports.progress.connect(self._update_progress, Qt.ConnectionType.QueuedConnection)
            self.reports.success.connect(self._on_success, Qt.ConnectionType.QueuedConnection)
            self.reports.failure.connect(self._on_error, Qt.ConnectionType.QueuedConnection)
            self._build_widgets()
            fit_dialog(self, preferred=(660, 420), minimum=(420, 320))
            self._screen_watcher = ScreenWatcher(self)

        def _build_widgets(self):
            central = GlassWorkspace()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.setContentsMargins(12, 12, 12, 12)
            root.setSpacing(10)
            title = QLabel("Download files")
            title.setObjectName("download-title")
            root.addWidget(title)
            subtitle = QLabel("Direct links and Google Drive")
            subtitle.setObjectName("download-subtitle")
            root.addWidget(subtitle)
            card = QFrame()
            card.setObjectName("download-card")
            body = QVBoxLayout(card)
            body.setContentsMargins(14, 14, 14, 14)
            body.setSpacing(8)
            body.addWidget(QLabel("Download link"))
            self.url_entry = QLineEdit()
            body.addWidget(self.url_entry)
            body.addWidget(QLabel("Save to"))
            folder = QHBoxLayout()
            self.folder_entry = QLineEdit(str(Path.home() / "Downloads"))
            folder.addWidget(self.folder_entry, 1)
            self.browse_btn = ActionButton("Browse…")
            self.browse_btn.clicked.connect(self._choose_dir)
            folder.addWidget(self.browse_btn)
            body.addLayout(folder)
            self.progress = QProgressBar()
            self.progress.setTextVisible(False)
            self.progress.setRange(0, 100)
            self.progress.setFixedHeight(6)
            body.addWidget(self.progress)
            self.status_label = QLabel("Paste a direct link or Google Drive share link to download a file.")
            self.status_label.setWordWrap(True)
            self.status_label.setObjectName("download-status")
            body.addWidget(self.status_label)
            body.addStretch(1)
            actions = QHBoxLayout()
            actions.addStretch(1)
            self.cancel_btn = ActionButton("Cancel")
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.clicked.connect(self._cancel_download)
            actions.addWidget(self.cancel_btn)
            self.download_btn = ActionButton("Download")
            self.download_btn.setProperty("role", "primary")
            self.download_btn.clicked.connect(self._start_download)
            actions.addWidget(self.download_btn)
            body.addLayout(actions)
            root.addWidget(card, 1)

        def _choose_dir(self):
            chosen = QFileDialog.getExistingDirectory(self, "Choose download folder", self.folder_entry.text())
            if chosen:
                self.folder_entry.setText(chosen)

        def _start_download(self):
            url = self.url_entry.text().strip()
            if not url:
                QMessageBox.warning(self, "Missing link", "Please paste a download link first.")
                return
            self.download_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)
            self.cancel_event.clear()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.status_label.setText("Starting download...")
            self.worker_thread = threading.Thread(target=self._run_download, args=(url, Path(self.folder_entry.text())),
                                                  daemon=True)
            self.worker_thread.start()

        def _run_download(self, url, dest_dir):
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                final_path = download_direct_link(url, dest_dir, self.reports.progress.emit, self.cancel_event)
                self.reports.success.emit(str(final_path))
            except Exception as error:  # pylint: disable=broad-except
                self.reports.failure.emit(str(error))

        def _update_progress(self, downloaded, total):
            megabytes = downloaded / (1024 * 1024)
            if total > 0:
                percent = downloaded / total * 100
                self.progress.setValue(round(percent))
                self.status_label.setText(f"Downloading... {megabytes:.2f} MB / {total / (1024 * 1024):.2f} MB ({percent:.1f}%)")
            else:
                self.progress.setRange(0, 0)
                self.status_label.setText(f"Downloading... {megabytes:.2f} MB (size unknown)")

        def _finish(self, status):
            self.status_label.setText(status)
            self.download_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)

        def _on_success(self, final_path):
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self._finish(f"Done. Saved to:\n{final_path}")

        def _on_error(self, message):
            self.progress.setRange(0, 100)
            self._finish(f"Error: {message}")

        def _cancel_download(self):
            self.cancel_event.set()
            self.status_label.setText("Cancelling...")
            self.cancel_btn.setEnabled(False)

    app = QApplication.instance() or QApplication(sys.argv)
    register_fonts()
    install_no_focus_lines(app)
    mode = theme_mode or get_theme_mode()
    mode = mode if mode in Theme.PALETTES else "default"
    Theme.apply_theme(mode)
    palette = Theme.PALETTES[Theme.active_theme]
    app.setStyleSheet(build_stylesheet(Theme.active_theme) + f"""
        QFrame#download-card {{ background: {palette["BG_MID"]}; border: 1px solid {palette["BORDER"]}; border-radius: 10px; }}
        QLabel#download-title {{ color: {palette["TEXT_BRIGHT"]}; font-size: 17px; font-weight: 700; }}
        QLabel#download-subtitle, QLabel#download-status {{ color: {palette["TEXT_DIM"]}; }}
    """)
    return app, DownloaderWindow()


if __name__ == "__main__":
    application, window = create_window()
    window.show()
    raise SystemExit(application.exec())
