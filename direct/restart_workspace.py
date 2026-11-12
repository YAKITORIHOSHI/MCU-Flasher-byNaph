"""Wait for a saved workspace to exit, then reopen it: through host Bootstrap repair, or as a plain relaunch."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def restart(parent_pid, project="", reopen=False):
    """reopen=True relaunches normally (a new interface size); otherwise Bootstrap --repair."""
    import psutil
    try:
        parent = psutil.Process(int(parent_pid))
        parent.wait(timeout=60)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        return 1
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "MCU_FLASHER_WORKSPACE_RUNTIME",
                "MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_APP_ROOT", "PIP_NO_INDEX"):
        environment.pop(key, None)
    if sys.platform == "win32":
        system = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/wscript.exe"
        command = [str(system), str(ROOT / "direct/windows/run.vbs")]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW}
    elif reopen and sys.platform == "darwin":
        command = ["bash", str(ROOT / "direct/macos/run.sh")]
        options = {"start_new_session": True}
    else:
        # A relaunch from the desktop shows refusals the way its menu entry does.
        command = ["bash", str(ROOT / "direct/ubuntu/run.sh")] + (["--desktop"] if reopen else [])
        options = {"start_new_session": True}
    if not reopen:
        command.append("--repair")
    if project:
        command.extend(("--project", str(project)))
    subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--project", default="")
    parser.add_argument("--reopen", action="store_true", help="Relaunch normally instead of repairing")
    arguments = parser.parse_args()
    raise SystemExit(restart(arguments.parent, arguments.project, arguments.reopen))
