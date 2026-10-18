"""Native competing registrations using disposable JSON and Python's real lock.

All process/device/project labels are controlled fixtures. Neither the Python GUI
configuration module nor live settings, projects, tool stores or serial ports
are opened. This does not qualify production filesystem identity or ownership.
"""
from pathlib import Path
import importlib.util
import json
import os
import platform
import queue
import subprocess
import sys
import tempfile
import threading

import psutil

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop/target/debug" / ("registry_probe.exe" if sys.platform == "win32" else "registry_probe")
spec = importlib.util.spec_from_file_location("registry_lock_reference", ROOT / "main/core/config_store.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def start(fixture, project="a"):
    return subprocess.Popen([str(EXE), str(ROOT), str(fixture), project, "hold"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")


def ready(child):
    messages = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: messages.put(child.stdout.readline(4097)), daemon=True).start()
    line = messages.get(timeout=5)
    assert len(line) <= 4096 and line.endswith("\n"), line
    value = json.loads(line)
    assert value["status"] in ("updated", "unchanged", "busy", "collision"), value
    return value


def release(child):
    out, err = child.communicate(input="release\n", timeout=5)
    assert child.returncode == 0 and not err, (child.returncode, out, err)
    assert json.loads(out) == {"status": "released"}, out


def claim(fixture, project="a"):
    result = subprocess.run([str(EXE), str(ROOT), str(fixture), project, "claim"],
                            text=True, encoding="utf-8", capture_output=True, timeout=5, check=True)
    assert len(result.stdout) <= 4096 and not result.stderr
    return json.loads(result.stdout)


def main():
    checks = []
    children = []
    sandbox = ROOT / "audit/migration/scratch"
    sandbox.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="registry-interop-", dir=sandbox) as directory:
        fixture = Path(directory)
        for name in ("project-a", "project-b"):
            (fixture / name).mkdir()
        target = fixture / "settings.json"
        parent_pid = str(os.getpid())
        seed = dict(shared=dict(theme="glass", unknown=dict(message="café 日本語", count=1)),
                    instances={parent_pid: dict(create_time=psutil.Process().create_time(),
                                                selected_port="FIXTURE-PORT-B", unknown=[1, 2, 3])},
                    futureVersion=dict(preserve=True))
        target.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
        before = target.read_bytes()
        try:
            with reference.config_lock(target):
                assert claim(fixture) == dict(status="busy")
                assert target.read_bytes() == before
            checks.append("python-transaction-lock-blocks-native-registration")

            # Both processes start before either ready frame is consumed. An
            # overlapping transaction may return Busy or see the winner's
            # registration; neither path may publish a second live owner.
            first, second = start(fixture), start(fixture)
            children.extend((first, second))
            results = [(first, ready(first)), (second, ready(second))]
            winners = [child for child, result in results if result["status"] == "updated"]
            assert len(winners) == 1, results
            winner = winners[0]
            loser, result = next((child, result) for child, result in results if child is not winner)
            assert result["status"] in ("busy", "collision"), result
            out, err = loser.communicate(timeout=5)
            assert loser.returncode == 0 and not out and not err
            checks.append("simultaneous-claims-have-one-live-winner")
            config = json.loads(target.read_bytes())
            assert config["shared"] == seed["shared"] and config["futureVersion"] == seed["futureVersion"]
            assert config["instances"][parent_pid] == seed["instances"][parent_pid]
            own = config["instances"][str(winner.pid)]
            assert own["selected_port"] == "FIXTURE-PORT-A" and own["create_time"] > 0
            checks.append("registration-preserves-unrelated-python-records-and-fields")

            before = target.read_bytes()
            assert claim(fixture)["status"] == "collision"
            assert target.read_bytes() == before
            checks.append("live-native-registration-blocks-peer-without-writing")
            assert claim(fixture, "b")["status"] == "collision"
            assert target.read_bytes() == before
            checks.append("native-query-reserves-live-python-port-owner")

            with reference.config_lock(target):
                config = json.loads(target.read_bytes())
                config["shared"]["unknown"]["count"] = 2
                config["instances"][str(winner.pid)]["concurrentPreference"] = "retained"
                # Use the native-written prefix/spelling to avoid conflating
                # Windows canonicalization with this registration proof.
                native_project = config["instances"][str(winner.pid)]["active_sketch_dir"]
                config["instances"][parent_pid]["active_sketch_dir"] = native_project[:-1] + "b"
                target.write_text(json.dumps(config), encoding="utf-8")
            before = target.read_bytes()
            blocked = claim(fixture, "b")
            assert blocked["status"] == "collision" and parent_pid in blocked["owners"]
            assert target.read_bytes() == before
            checks.append("native-query-reserves-live-python-project-owner")
            release(winner)
            config = json.loads(target.read_bytes())
            own = config["instances"][str(winner.pid)]
            assert own["active_sketch_dir"] == own["selected_port"] == ""
            assert own["concurrentPreference"] == "retained" and config["shared"]["unknown"]["count"] == 2
            checks.append("release-rereads-and-preserves-concurrent-python-updates")

            crashed = start(fixture)
            children.append(crashed)
            assert ready(crashed)["status"] == "updated"
            crashed.kill()
            crashed.communicate(timeout=5)
            replacement = start(fixture)
            children.append(replacement)
            assert ready(replacement)["status"] == "updated"
            config = json.loads(target.read_bytes())
            assert str(crashed.pid) in config["instances"]
            assert config["instances"][str(replacement.pid)]["active_sketch_dir"]
            checks.append("confirmed-exited-owner-allows-reclaim-without-pruning-record")
            release(replacement)
            assert not list(fixture.glob(".mcu-fixture-registry-*.tmp"))
            checks.append("successful-and-released-transactions-leave-no-owned-staging")
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
    print(json.dumps(dict(schemaVersion=1, reference="main/core/config.py ownership schema and config_store.config_lock",
                          platform=sys.platform, architecture=platform.machine(), passed=True,
                          checks=checks, checksPassed=len(checks), liveSettingsAccess=False,
                          hardwareAccess=False, productionOwnershipQualified=False)))


if __name__ == "__main__":
    main()
