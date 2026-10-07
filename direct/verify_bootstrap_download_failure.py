#!/usr/bin/env python3
"""Retain proof of a real blocked seed transfer and the native failure UI.

Only selected bootstrap definitions execute. The fixture requires its seed, so
the real helper's False enters the production failure callback without running
the normal fallback installers. HTTP outcomes and installer results are never
mocked. All stores, configuration and logs belong to temp/audit.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Optional, Any
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QObject, Qt, Signal, Slot, QTimer, QEvent
from PySide6.QtGui import QColor, QFont, QIcon, QTextCursor, QTextCharFormat
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout,
                              QLabel, QPlainTextEdit, QProgressBar, QSizePolicy, QVBoxLayout)
from main.core.theme import Theme
from src.modules.ui_palette import contrast_ratio


def definitions(root):
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    functions = {"_record_bootstrap_log", "_record_bootstrap_exception",
                 "load_bootstrap_config", "save_bootstrap_config", "safe_unlink",
                 "safe_rmtree", "safe_replace_file", "_ensure_platformio_core_prebuilt",
                 "_normalize_sha256", "_file_sha256", "_download_matches_expectations",
                 "_download_file", "status", "ok", "warn", "fail",
                 "_prepare_bootstrap_qt_path", "_load_bootstrap_qt", "_bootstrap_qt_context"}
    classes = {"_BootstrapRootProxy", "BootstrapGUI"}
    constants = {"_PLATFORMIO_PREBUILT_ZIP_NAME", "_PLATFORMIO_PREBUILT_GITHUB_URL",
                 "_PLATFORMIO_PREBUILT_EXPECTED_SIZE", "_PLATFORMIO_PREBUILT_EXPECTED_SHA256",
                 "_DOWNLOAD_STALL_TIMEOUT_SECONDS", "CYAN", "GREEN", "YELLOW",
                 "RED", "DIM", "BOLD", "RESET"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in classes:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id in constants for target in node.targets):
            nodes.append(node)
    # Run the same failure callback used by the normal setup worker. No setup
    # worker or install routine is included in this isolated namespace.
    nodes.append(next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and node.name == "_fail_and_exit"))
    palette = {"T_" + key: value for key, value in Theme.PALETTES["default"].items()}
    scope = dict(globals(), SCRIPT_DIR=root, __file__=str(source),
                 BOOTSTRAP_CONFIG_FILE=root / "bootstrap_config.json",
                 _BOOTSTRAP_LOG_FILE=root / "failed-download.log",
                 _BOOTSTRAP_LOG_LOCK=threading.Lock(), _T_PALETTE=palette,
                 _BOOTSTRAP_THEME_MODE="default", _resolve_bootstrap_theme=lambda: (palette, "default"),
                 DEFAULT_SKIP_UPDATES=True, BOOTSTRAP_CLOSE_DELAY_S=5.0,
                 HAS_PYSIDE6_BOOTSTRAP=True, _gui=None)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
    return scope


def main():
    if sys.platform != "win32":
        raise SystemExit("The verified prebuilt seed contains Windows packages.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    audit = (ROOT / "temp/audit").resolve()
    audit.mkdir(parents=True, exist_ok=True)
    root = (args.output_dir.resolve() if args.output_dir else
            Path(tempfile.mkdtemp(prefix="download-failure-", dir=audit)))
    if not root.is_relative_to(audit) or root == audit:
        raise SystemExit("Failure proof must remain in its own temp/audit directory.")
    root.mkdir(parents=True, exist_ok=True)
    core = root / "src/.platformio-mcu-gui"
    core.mkdir(parents=True, exist_ok=True)
    os.environ["PLATFORMIO_CORE_DIR"] = str(core)
    os.environ["PLATFORMIO_HOME_DIR"] = str(root / "platformio-home")
    scope = definitions(root)
    partial = root / "src" / (scope["_PLATFORMIO_PREBUILT_ZIP_NAME"] + ".part")
    checkpoint = b"retained checkpoint before the blocked real HTTP request\n"
    partial.write_bytes(checkpoint)
    requests = []

    class BlockedTransfer(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"path": self.path, "range": self.headers.get("Range"), "status": 503})
            payload = b"Transfer blocked by the isolated verification server\n"
            self.send_response(503, "Blocked transfer")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BlockedTransfer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/seed.zip"
    app = QApplication.instance() or QApplication(["Real blocked transfer verification"])
    gui = scope["BootstrapGUI"]()
    scope["_gui"] = scope["gui"] = gui
    result, error = [], []
    # Exercise the real failure callback and log widgets while replacing only
    # its native modal presentation. No desktop/window automation is needed.
    gui.show_error = Mock()

    def run_transfer():
        try:
            gui.log_section("Earlier successful step")
            gui.log_ok("Previous fixture stage completed")
            gui.log_section("Required release seed — blocked transfer verification")
            value = scope["_ensure_platformio_core_prebuilt"](
                gui, script_dir=root,
                plan={"schema": 1, "platforms": ["atmelavr"], "frameworks": ["arduino"], "libraries": []},
                seed_url=url, attempts=1)
            result.append(value)
            if not value:
                scope["_fail_and_exit"]("Required release seed fixture",
                    "The actual loopback server rejected the transfer with HTTP 503.")
        except BaseException as exc:
            error.append(repr(exc))

    started = time.monotonic()
    worker = threading.Thread(target=run_transfer, daemon=True)
    worker.start()
    deadline = started + 15
    try:
        while time.monotonic() < deadline:
            app.processEvents()
            if error:
                raise AssertionError(error)
            if not worker.is_alive() and gui._window.status_lbl.text() == "Setup failed":
                break
            time.sleep(.01)
        else:
            raise AssertionError("Actual blocked transfer/failure callback did not complete")
        assert result == [False], result
        gui.show_error.assert_called_once()
        assert 'HTTP 503' in gui.show_error.call_args.args[1]
        assert requests == [{"path": "/seed.zip", "range": f"bytes={len(checkpoint)}-", "status": 503}], requests
        assert partial.read_bytes() == checkpoint, "The failed attempt changed the retained partial"
        document = gui._window.log_edit.document()
        assert not gui._window.log_edit.textCursor().hasSelection(), "Output created an unwanted selection"
        palette = gui._window._theme_pal
        red = palette["T_RED"]
        assert contrast_ratio(red, palette["T_BG_DARKEST"]) >= 4.5
        failed_start = gui._window._step_start_cursor.position()
        fragments = []
        block = document.firstBlock()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid() and fragment.text().strip():
                    color = fragment.charFormat().foreground().color().name()
                    failed = fragment.position() >= failed_start
                    fragments.append({"text": fragment.text(), "color": color, "failed_step": failed})
                    if failed:
                        assert color == red, (fragment.text(), color)
                it += 1
            block = block.next()
        assert document.find("Previous fixture stage completed").charFormat().foreground().color().name() == palette["T_GREEN"]
        assert contrast_ratio(palette["T_GREEN"], palette["T_BG_DARKEST"]) >= 4.5
        full_log = scope["_BOOTSTRAP_LOG_FILE"].read_text(encoding="utf-8")
        assert "HTTP Error 503: Blocked transfer" in full_log
        assert "Traceback" in full_log and "Setup aborted" in full_log
        capture = root / "failed-download-window.png"
        assert gui._window.grab().save(str(capture))
        (root / "display.txt").write_text(gui._window.log_edit.toPlainText(), encoding="utf-8")
        report = {"url": url, "requests": requests, "seed_result": result[0],
                  "elapsed_seconds": round(time.monotonic() - started, 3),
                  "required_seed_fixture": True, "fallback_installers_run": False,
                  "partial_retained_sha256": hashlib.sha256(checkpoint).hexdigest(),
                  "whole_failed_step_red": True, "error_dialog_requested": True,
                  "native_modal_presentation_mocked": True,
                  "capture": str(capture), "fragments": fragments}
        (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "fragments"}, ensure_ascii=False))
        print(f"Retained real failed-download proof: {root}")
        return 0
    finally:
        gui.close()
        app.processEvents()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
