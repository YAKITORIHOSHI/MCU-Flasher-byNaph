"""Compare explicit Rust self/owned-child identity with psutil; no PID inventory."""
from pathlib import Path
import json
import math
import os
import platform
import queue
import subprocess
import sys
import threading

import psutil

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop/target/debug" / ("process_probe.exe" if sys.platform == "win32" else "process_probe")


def main():
    result = subprocess.run([str(EXE)], text=True, encoding="utf-8", capture_output=True,
                            check=True, timeout=10)
    assert len(result.stdout) <= 16384 and not result.stderr, (result.stdout, result.stderr)
    native = json.loads(result.stdout)
    assert native["passed"] and all(native["checks"].values()), native
    checks = list(native["checks"])
    child = subprocess.Popen([str(EXE), "--hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8")
    messages = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: messages.put(child.stdout.readline(4097)), daemon=True).start()
    try:
        line = messages.get(timeout=3)
        assert len(line) <= 4096 and line.endswith("\n"), line
        ready = json.loads(line)
        evidence = ready["identity"]
        assert ready["schemaVersion"] == 1 and ready["native"] and evidence["state"] == "running", ready
        identity = evidence["identity"]
        assert identity["pid"] == child.pid, identity
        reference = psutil.Process(child.pid).create_time()
        actual = identity["createTime"]
        assert math.isfinite(actual) and actual > 0
        # Linux psutil uses start ticks / SC_CLK_TCK + btime. Windows uses
        # FILETIME, with sub-millisecond floating point conversion differences.
        tolerance = 0.001 if sys.platform == "win32" else 2 / os.sysconf("SC_CLK_TCK")
        assert abs(reference - actual) <= tolerance, (reference, actual, tolerance)
        checks.extend(("explicit-pid-matches-psutil", "creation-epoch-matches-psutil"))
        out, err = child.communicate(input="release\n", timeout=3)
        assert child.returncode == 0 and not out and not err, (child.returncode, out, err)
        checks.append("fixed-hold-release-exits-cleanly")
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)
    print(json.dumps(dict(
        schemaVersion=1, reference="main/core/config.py:_own_create_time; psutil.Process.create_time",
        platform=sys.platform, architecture=platform.machine(), passed=True,
        checks=checks, checksPassed=len(checks), psutilVersion=psutil.__version__,
        createTimeDifferenceSeconds=abs(reference - actual), toleranceSeconds=tolerance,
        inventoryScans=False, liveSettingsAccess=False, hardwareAccess=False,
        productionOwnershipQualified=False)))


if __name__ == "__main__":
    main()
