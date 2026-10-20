#!/usr/bin/env python3
"""Probe the shipped Windows EXE's argument transport with a fixed WSH recorder.

Only a copied EXE and recorder scripts run, inside owned temp/ fixtures. This
never invokes Bootstrap, installs dependencies, opens a sketch or uses hardware.
Native Windows is required; a Linux skip is not Windows execution evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
RESULT_VARIABLE = "MCU_LAUNCHER_PROBE_RESULT"
RECORDER = r'''Option Explicit
Dim fs, ws, destination, partial, stream, index
Set fs = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("WScript.Shell")
destination = ws.Environment("PROCESS")("MCU_LAUNCHER_PROBE_RESULT")
If Len(destination) = 0 Then WScript.Quit 71
partial = destination & ".partial"
Set stream = fs.CreateTextFile(partial, True, True)
stream.Write "{""arguments"": ["
For index = 0 To WScript.Arguments.Count - 1
    If index > 0 Then stream.Write ","
    stream.Write JsonString(WScript.Arguments(index))
Next
stream.Write "],""cwd"":" & JsonString(ws.CurrentDirectory)
stream.Write ",""script"":" & JsonString(WScript.ScriptFullName) & "}"
stream.Close
fs.MoveFile partial, destination
WScript.Quit 0

Function JsonString(value)
    Dim result, position, number
    result = """"
    For position = 1 To Len(value)
        number = AscW(Mid(value, position, 1))
        If number < 0 Then number = number + 65536
        result = result & "\u" & Right("0000" & Hex(number), 4)
    Next
    JsonString = result & """"
End Function
'''


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owned_script_processes(psutil, script: Path):
    """Find only recorder children with this fixture's exact script argument."""
    result = {}
    expected = os.path.normcase(str(script))
    for process in psutil.process_iter(("pid", "name", "cmdline", "create_time")):
        try:
            info = process.info
            if (info["name"] or "").lower() not in ("wscript.exe", "cscript.exe"):
                continue
            if any(os.path.normcase(argument) == expected for argument in info["cmdline"] or []):
                result[(info["pid"], info["create_time"])] = process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def capture(executable: Path, script: Path, arguments: list[str], result: Path,
            unrelated_cwd: Path, *, timeout: float = 15.0) -> dict:
    import psutil

    environment = dict(os.environ, **{RESULT_VARIABLE: str(result)})
    # Avoid Python contamination in the harness; the recorder does not run Python.
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        environment.pop(name, None)
    process = subprocess.Popen([str(executable), *arguments], cwd=unrelated_cwd,
                               env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    children = {}
    deadline = time.monotonic() + timeout
    observed = None
    error = None
    cleanup_failures = []
    try:
        while time.monotonic() < deadline:
            children.update(owned_script_processes(psutil, script))
            if result.is_file():
                observed = json.loads(result.read_text(encoding="utf-16"))
                break
            if process.poll() not in (None, 0):
                error = f"Launcher exited with code {process.returncode} before recorder output"
                break
            time.sleep(.05)
        if observed is None and error is None:
            error = "Timed out waiting for the fixed recorder; any owned launcher was stopped"
        try:
            process.wait(timeout=max(.1, min(2.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            error = error or "Launcher did not exit after the recorder completed"
    finally:
        children.update(owned_script_processes(psutil, script))
        # psutil checks retained process identities before signalling. Never
        # terminate a process merely because its PID was previously observed.
        for child in children.values():
            try:
                if child.is_running():
                    child.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                cleanup_failures.append(f"Access denied stopping owned recorder PID {child.pid}")
        if process.poll() is None:
            process.kill()  # The original Popen handle, not a rediscovered PID.
        process.wait(timeout=5)
        _, alive = psutil.wait_procs(list(children.values()), timeout=3)
        cleanup_failures.extend(f"Owned recorder PID {child.pid} survived cleanup" for child in alive)
    return {"expected": arguments, "observed": observed,
            "launcherExitCode": process.returncode, "error": error,
            "cleanupFailures": cleanup_failures,
            "passed": error is None and not cleanup_failures and process.returncode == 0
            and observed is not None
            and observed["arguments"] == arguments
            and os.path.normcase(observed["cwd"]) == os.path.normcase(str(executable.parent))
            and os.path.normcase(observed["script"]) == os.path.normcase(str(script))}


def verify(report_dir: Path) -> dict:
    source = ROOT / "MCU_Flasher.exe"
    original_hash = digest(source)
    report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="launcher-", dir=report_dir) as temporary:
        fixture = Path(temporary)
        # Spaces and Unicode are part of both the executable and recorder paths.
        application = fixture / "relocated app 日本語 café"
        (application / "src/modules").mkdir(parents=True)
        (application / "direct").mkdir()
        unrelated = fixture / "unrelated working directory"
        unrelated.mkdir()
        executable = application / "MCU_Flasher.exe"
        shutil.copyfile(source, executable)
        script = application / "direct/runThisOnWindows.vbs"
        # WSH does not reliably recognize UTF-8 script source; use its native
        # UTF-16 BOM format. Recorder output uses the same explicit encoding.
        script.write_text(RECORDER.replace("\n", "\r\n"), encoding="utf-16", newline="")
        cases = [
            ("relocated_no_arguments", "ordinary", []),
            ("project_path_with_spaces", "ordinary",
             ["--project", str(application / "source folder/main sketch.ino")]),
            ("unicode_project_path", "ordinary",
             ["--project", str(application / "日本語 café/main.ino")]),
            ("trailing_directory_separator", "adversarial",
             ["--project", str(application / "source folder") + "\\"]),
            ("trailing_separator_before_option", "adversarial",
             ["--project", str(application / "source folder") + "\\", "--repair"]),
            ("literal_quote_argument", "adversarial",
             ["--plan", 'literal "quoted" argument']),
        ]
        outcomes = []
        for index, (name, category, arguments) in enumerate(cases):
            result = capture(executable, script, arguments,
                             fixture / f"result-{index}.json", unrelated)
            outcomes.append({"name": name, "category": category, **result})
            print(f"{name}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
        copied_hash = digest(executable)
    preserved = digest(source) == original_hash == copied_hash
    return {"schemaVersion": 1, "nativeHost": sys.platform,
            "scope": "Shipped EXE relocation and literal argument transport to fixed WSH recorder only",
            "bootstrapInstallersHardwareInvoked": False,
            "shippedExecutableSha256": original_hash,
            "originalAndCopiedExecutableBytesMatch": preserved,
            "cases": outcomes,
            "ordinaryPassed": all(case["passed"] for case in outcomes if case["category"] == "ordinary"),
            "passed": preserved and all(case["passed"] for case in outcomes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path,
                        default=ROOT / "temp/audit/windows-launcher")
    options = parser.parse_args()
    if sys.platform != "win32":
        print("SKIP: native Windows is required for the shipped EXE/WSH probe")
        return 0
    report_dir = options.report_dir.resolve()
    owned_temp = (ROOT / "temp").resolve()
    if not report_dir.is_relative_to(owned_temp) or report_dir == owned_temp:
        parser.error("--report-dir must be an owned descendant of checkout temp/")
    report = verify(report_dir)
    report_path = report_dir / "launcher-transport.json"
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, indent=2))
    print(f"Receipt: {report_path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
