#!/usr/bin/env python3
"""Run hardware-free verifiers in parallel and name what actually regressed.

With --baseline, each failure is rerun against a clean export of that Git ref,
so a failure that already exists there is not reported as new. Logs go to
temp/audit/verifier-runs/. See .agents/skills/mcu-verify/SKILL.md.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "temp/audit/verifier-runs"
_FAILURE = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.M)
# Verifiers that need arguments or a host this runner cannot provide.
_ARGUMENTS = {"verify_bootstrap_promotion": ["--report-dir", str(LOGS / "bootstrap-promotion")]}
_WINDOWS_ONLY = {"verify_bootstrap_download_failure", "verify_terminal"}


def environment() -> dict:
    # A sweep must never reach the developer's desktop: refusal and notice fixtures
    # would otherwise open real dialogs for release data that is deliberately wrong.
    inherited = {name: value for name, value in os.environ.items()
                 if name not in ("DISPLAY", "WAYLAND_DISPLAY")}
    return dict(inherited, QT_QPA_PLATFORM="offscreen", QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu",
                PYTHONDONTWRITEBYTECODE="1")


def run_one(name: str, root: Path, timeout: int, log: Path) -> dict:
    started = time.monotonic()
    command = [sys.executable, "-B", str(root / "direct" / f"{name}.py"), *_ARGUMENTS.get(name, [])]
    try:
        result = subprocess.run(command, cwd=root, env=environment(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", timeout=timeout)
        output, code = result.stdout, result.returncode
    except subprocess.TimeoutExpired as exc:
        output, code = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""), 124
    log.write_text(output, encoding="utf-8", errors="replace")
    failures = set(_FAILURE.findall(output))
    # PySide6 can crash during interpreter shutdown after every test has passed.
    teardown_only = code < 0 or code == 139
    passed = code == 0 or (teardown_only and not failures and re.search(r"^OK\b", output, re.M) is not None)
    return {"name": name, "code": code, "passed": passed, "failures": failures,
            "seconds": round(time.monotonic() - started)}


def export_baseline(reference: str, directory: Path) -> None:
    paths = [path for path in ("direct", "main", "src", "LAUNCHER", ".agents", ".github", "mcu_flash_gui.py",
                               "README.md", "RELEASE_NOTES.md")
             if subprocess.run(["git", "cat-file", "-e", f"{reference}:{path}"], cwd=ROOT, capture_output=True).returncode == 0]
    archive = subprocess.Popen(["git", "archive", reference, "--", *paths], cwd=ROOT, stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(directory)], stdin=archive.stdout, check=True)
    archive.stdout.close()
    if archive.wait():
        raise RuntimeError(f"Could not export {reference}")
    if (ROOT / "AGENTS.md").is_file():
        (directory / "AGENTS.md").write_bytes((ROOT / "AGENTS.md").read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="Run verifiers whose file name contains any of these")
    parser.add_argument("--jobs", type=int, default=max(2, min(6, (os.cpu_count() or 4) // 2)))
    parser.add_argument("--timeout", type=int, default=400)
    parser.add_argument("--baseline", help="Git ref used to classify failures as new or pre-existing")
    arguments = parser.parse_args()
    LOGS.mkdir(parents=True, exist_ok=True)
    names = sorted(path.stem for path in (ROOT / "direct").glob("verify_*.py"))
    if sys.platform != "win32":
        names = [name for name in names if name not in _WINDOWS_ONLY]
    if arguments.names:
        names = [name for name in names if any(part in name for part in arguments.names)]
    with ThreadPoolExecutor(arguments.jobs) as pool:
        results = list(pool.map(lambda name: run_one(name, ROOT, arguments.timeout, LOGS / f"{name}.log"), names))
    failed = [result for result in results if not result["passed"]]
    print(f"{len(results) - len(failed)}/{len(results)} verifiers passed")
    known = {}
    if failed and arguments.baseline:
        with tempfile.TemporaryDirectory(prefix="baseline-", dir=LOGS) as temporary:
            export_baseline(arguments.baseline, Path(temporary))
            with ThreadPoolExecutor(arguments.jobs) as pool:
                outcomes = pool.map(lambda result: run_one(
                    result["name"], Path(temporary), arguments.timeout, LOGS / f"{result['name']}.baseline.log"), failed)
                known = {result["name"]: result for result in outcomes}
    regressions = 0
    for result in failed:
        before = known.get(result["name"])
        new = result["failures"] - before["failures"] if before else result["failures"]
        if before and before["passed"] and not new:
            new = {f"exit code {result['code']}"}
        status = "NEW" if (new or not before) else "pre-existing"
        regressions += status == "NEW"
        detail = ", ".join(sorted(new or result["failures"]))[:300] or f"exit code {result['code']}"
        print(f"  {status:12} {result['name']} ({result['seconds']}s): {detail}")
    print(f"Logs: {LOGS.relative_to(ROOT)}")
    return 1 if regressions or (failed and not arguments.baseline) else 0


if __name__ == "__main__":
    raise SystemExit(main())
