#!/usr/bin/env python3
"""Render isolated native UI fixtures in fresh processes at each display scale.

No live setup, accounts, project metadata or hardware is used. Run on a native
desktop (or Xvfb on Ubuntu); a Linux run does not certify Windows rendering.
Failures do not stop later screens from producing their own logs and captures.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SCALES = ("1", "1.25", "1.5", "1.75", "2")


def checks(group, scale, output):
    def render(script, folder, *args):
        return script, ["--render-dir", str(output / folder), *args]

    if group == "edgecases":
        yield render("verify_toolbar_sizing", "toolbar")
        if scale in ("1", "2"):
            yield "verify_shutdown", ["--repeat", "3"]
            yield render("verify_runtime", "workspace", "--no-finalize")
            yield render("verify_responsive", "responsive")
        if scale == "1" and sys.platform == "win32":
            yield "verify_terminal", []
        return

    if group in ("all", "portal"):
        yield render("verify_owner_tickets", "developer")
    if group in ("all", "workspace", "responsive"):
        yield render("verify_toolbar_sizing", "toolbar")
        yield render("verify_responsive", "responsive")
        if scale in ("1", "2"):
            yield "verify_shutdown", ["--repeat", "3"]
            yield render("verify_runtime", "workspace", "--no-finalize")
    if group in ("all", "resources", "responsive"):
        yield render("verify_downloader_appearance", "resources", "--scale", scale)
        yield render("verify_bootstrap_headers", "setup")
        yield render("verify_svg_icons", "icons")
    if group in ("all", "auxiliary") and scale in ("1", "2"):
        for script, folder in (
                ("verify_cloud_sketch_ui", "cloud"),
                ("verify_projects", "projects"),
                ("verify_editor_review_ui", "editor-review"),
                ("verify_custom_board_dialog", "custom-board"),
                ("verify_arduino_board_chooser", "cli-board-chooser"),
                ("verify_package_progress", "package-progress")):
            yield render(script, folder)
        yield "verify_package_coverage", []
    if group == "responsive" and scale in ("1", "1.5", "2"):
        yield render("verify_projects", "projects")
    if group == "responsive" and scale == "1" and sys.platform == "win32":
        yield "verify_terminal", []
    if group == "all" and scale == "1":
        if sys.platform == "win32":
            yield "verify_terminal", []
        yield "verify_theme_readability", []
        yield "verify_panel_readability", []
        yield "verify_build_console", []
        yield "verify_svg_icons", ["--native-tk"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path, default=ROOT / "temp/audit/layout-native")
    parser.add_argument("--group", choices=("all", "portal", "workspace", "resources", "auxiliary", "responsive", "edgecases"), default="all")
    parser.add_argument("--scale", choices=SCALES, action="append")
    args = parser.parse_args()
    output = args.render_dir.resolve()
    if not output.is_relative_to((ROOT / "temp").resolve()):
        parser.error("Evidence must stay inside this checkout's temp directory")
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for scale in args.scale or SCALES:
        scaled = output / ("scale-" + scale)
        scaled.mkdir(parents=True, exist_ok=True)
        for script, options in checks(args.group, scale, scaled):
            variant = "-native-tk" if "--native-tk" in options else ""
            log_path = scaled / (script + variant + ".log")
            env = os.environ.copy()
            env.update(QT_SCALE_FACTOR=scale, PYTHONDONTWRITEBYTECODE="1",
                       PYTHONFAULTHANDLER="1")
            env.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "xcb")
            env.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
            command = [sys.executable, "-B", str(ROOT / "direct" / (script + ".py")), *options]
            print(f"[{scale}] {script}", flush=True)
            started = time.monotonic()
            with log_path.open("w", encoding="utf-8") as log:
                try:
                    completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                               stderr=subprocess.STDOUT, timeout=240)
                    code = completed.returncode
                except subprocess.TimeoutExpired:
                    log.write("\nNative UI fixture exceeded its 240-second deadline.\n")
                    code = 124
            result = dict(check=script, scale=scale, exit_code=code,
                          seconds=round(time.monotonic() - started, 2), log=str(log_path.relative_to(output)))
            results.append(result)
            print(f"  {'PASS' if code == 0 else 'FAIL'} ({result['seconds']}s)", flush=True)
            if code:
                # Logs remain complete on disk; show a bounded diagnostic tail.
                print(log_path.read_text(encoding="utf-8", errors="replace")[-6000:], flush=True)
    report = dict(host=sys.platform, python=sys.version, group=args.group, results=results)
    (output / ("results-" + args.group + ".json")).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    failures = [result for result in results if result["exit_code"]]
    print(f"{len(results) - len(failures)}/{len(results)} native fixture runs passed.", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
