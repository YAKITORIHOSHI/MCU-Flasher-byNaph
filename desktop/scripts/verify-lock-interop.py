"""Native Rust/Python OS-lock proof using only explicit migration fixtures."""
from pathlib import Path
import importlib.util
import json
import platform
import queue
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("migration_config_lock_reference", ROOT / "main/core/config_store.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
EXE = ROOT / "desktop/target/debug" / ("lock_probe.exe" if sys.platform == "win32" else "lock_probe")


def probe(fixture, role="try", check=True):
    result = subprocess.run([str(EXE), str(ROOT), str(fixture), role],
                            text=True, encoding="utf-8", capture_output=True, timeout=5, check=check)
    assert len(result.stdout) <= 4096 and len(result.stderr) <= 4096
    return result, json.loads(result.stdout)


def start_hold(fixture):
    child = subprocess.Popen([str(EXE), str(ROOT), str(fixture), "hold"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    messages = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: messages.put(child.stdout.readline(4097)), daemon=True).start()
    try:
        first = messages.get(timeout=5)
        assert len(first) <= 4096
        value = json.loads(first)
        assert value == dict(status="ready", independentBusy=True), value
        return child
    except BaseException:
        child.kill()
        child.communicate(timeout=5)
        raise


def main():
    sandbox = ROOT / "audit/migration/scratch"
    sandbox.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix="lock-interop-", dir=sandbox) as directory:
        fixture = Path(directory)
        target = fixture / "settings.json"
        lock_path = fixture / "settings.json.lock"
        _, value = probe(fixture)
        assert value == dict(status="acquired", independentBusy=True), value
        assert lock_path.read_bytes() == b"0"
        checks.extend(("independent-native-handles-contend", "empty-lock-byte-initialized"))

        lock_path.write_bytes(b"retained fixture marker")
        identity = lock_path.stat().st_ino
        with reference.config_lock(target):
            _, value = probe(fixture)
            assert value == dict(status="busy"), value
        checks.append("python-held-blocks-rust")

        child = start_hold(fixture)
        try:
            try:
                with reference.config_lock(target):
                    raise AssertionError("Python acquired a Rust-held lock")
            except OSError as error:
                assert "Another MCU Flasher window" in str(error), error
            checks.append("rust-held-blocks-python")
            out, err = child.communicate(input="release\n", timeout=5)
            assert child.returncode == 0 and not err, (child.returncode, out, err)
            assert json.loads(out) == dict(status="released"), out
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        with reference.config_lock(target):
            pass
        assert probe(fixture)[1]["status"] == "acquired"
        checks.append("release-allows-both-languages")

        child = start_hold(fixture)
        try:
            child.kill()
            child.communicate(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        with reference.config_lock(target):
            pass
        assert probe(fixture)[1]["status"] == "acquired"
        checks.append("crashed-owner-releases-native-lock")

        assert lock_path.read_bytes() == b"retained fixture marker"
        assert lock_path.stat().st_ino == identity
        assert not target.exists()
        checks.append("release-preserves-lock-identity-and-bytes")

        rejected, value = probe(fixture / "..", check=False)
        assert rejected.returncode != 0 and value["status"] == "error", value
        checks.append("parent-alias-rejected")
    print(json.dumps(dict(reference="main/core/config_store.py:config_lock", platform=sys.platform,
                          architecture=platform.machine(),
                          passed=True, checks=checks, checksPassed=len(checks), liveSettingsAccess=False,
                          productionCoexistenceQualified=False)))


if __name__ == "__main__":
    main()
