#!/usr/bin/env python3
"""Render synthetic Lite previews on an owned X server or isolated native CI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import select
import shutil
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--render-dir", type=Path, default=ROOT / "temp/layout")
    parser.add_argument("--xvfb", default="Xvfb")
    parser.add_argument("--native", action="store_true", help="Isolated Windows Actions runner only")
    args = parser.parse_args()
    if args.native and not (sys.platform == "win32" and os.environ.get("GITHUB_ACTIONS") == "true"
                            and os.environ.get("LITE_VERIFY_NATIVE") == "1"):
        parser.error("Native previews require the isolated Lite Windows Actions job")
    if sys.platform == "win32" and not args.native:
        parser.error("Windows previews require --native on an isolated Actions runner")
    executable = args.executable.resolve(strict=True)
    destination = args.render_dir.resolve()
    if not destination.is_relative_to(ROOT / "temp"):
        parser.error("Keep layout captures under LITE-MODE/temp")
    destination.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    server = None
    server_log = None
    server_reader = None
    try:
        if not args.native:
            xvfb = shutil.which(args.xvfb)
            if not xvfb:
                raise RuntimeError("An owned Xvfb is required; no developer desktop is used")
            read_fd, write_fd = os.pipe()
            server_log = (destination / "xvfb.log").open("wb")
            try:
                server = subprocess.Popen([xvfb, "-displayfd", str(write_fd), "-screen", "0",
                    "4000x2400x24", "-nolisten", "tcp"], pass_fds=(write_fd,),
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                def drain_server():
                    remaining = 65536
                    while chunk := server.stderr.read(8192):
                        if remaining:
                            server_log.write(chunk[:remaining])
                            remaining = max(0, remaining - len(chunk))
                server_reader = threading.Thread(target=drain_server, daemon=True)
                server_reader.start()
                os.close(write_fd)
                write_fd = None
                if not select.select([read_fd], [], [], 10)[0]:
                    raise RuntimeError("Owned Xvfb did not publish its display within 10 seconds")
                display = os.read(read_fd, 64).decode("ascii").strip()
                if not display.isdigit():
                    raise RuntimeError("Owned Xvfb failed to start; inspect xvfb.log")
            finally:
                os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)
            env.update(DISPLAY=":" + display, LIBGL_ALWAYS_SOFTWARE="1", GALLIUM_DRIVER="llvmpipe")
        from PySide6.QtGui import QImage
        cases = [
            ("wide", "1140x800", []),
            ("wide-dark", "1140x800", ["--preview-dark"]),
            ("compact", "700x560", ["--preview-sketch"]),
            ("compact-dark", "700x560", ["--preview-dark"]),
            ("short", "496x300", ["--preview-error"]),
            ("controls", "496x300", ["--preview-controls", "--preview-sketch"]),
            ("smallest", "360x240", ["--preview-controls"]),
            ("smallest-project", "360x240", ["--preview-projects"]),
            ("smallest-board-resources", "360x240", ["--preview-resource-boards"]),
            ("portrait-boards", "480x800", ["--preview-boards"]),
            ("short-resources", "496x300", ["--preview-resources"]),
        ]
        captures = []
        for name, size, flags in cases:
            path = destination / (name + ".png")
            # A previous success must never hide a failed renderer in this run.
            path.unlink(missing_ok=True)
            options = {}
            if sys.platform == "win32":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 1  # SW_SHOWNORMAL (not exported by subprocess).
                options.update(creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup)
            with (destination / (name + ".log")).open("wb") as log:
                subprocess.run([str(executable), "--preview", "--preview-size", size,
                    "--capture", str(path), *flags], cwd=ROOT, env=env,
                    stdout=log, stderr=subprocess.STDOUT, check=True, timeout=45, **options)
            image = QImage(str(path))
            if image.isNull() or image.width() < 300 or image.height() < 180:
                raise AssertionError(f"{name}: native renderer did not save a complete preview")
            colors = {image.pixelColor(x, y).rgba() for x in range(0, image.width(), 7)
                      for y in range(0, image.height(), 7)}
            if len(colors) < 16:
                raise AssertionError(f"{name}: native renderer saved a blank surface")
            captures.append({"name": name, "requested_logical_size": size,
                "pixels": [image.width(), image.height()], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            print("PASS", name, image.width(), image.height(), flush=True)
        (destination / "report.json").write_text(json.dumps({"host": platform.platform(),
            "native_windows": args.native, "owned_xvfb": not args.native,
            "hardware_and_accounts": "not accessed", "captures": captures}, indent=2) + "\n", encoding="utf-8")
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            if server_reader is not None:
                server_reader.join(timeout=2)
                server.stderr.close()
        if server_log is not None:
            server_log.close()


if __name__ == "__main__":
    main()
