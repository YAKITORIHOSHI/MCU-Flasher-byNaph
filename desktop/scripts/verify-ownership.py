"""Original ownership decisions vs pure Rust; no settings, projects or devices.

Only the named function body is compiled. psutil's direct-query outcomes are
injected, so the Python application's module/startup/persistence never runs.
"""
from pathlib import Path
import ast
import itertools
import json
import re
import subprocess
import sys
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
tree = ast.parse((ROOT / "main/core/config.py").read_text(encoding="utf-8"))
nodes = [node for node in tree.body
         if isinstance(node, ast.FunctionDef) and node.name == "_instance_is_alive"]
assert len(nodes) == 1
namespace = {}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "config.py:_instance_is_alive", "exec"), namespace)
reference = namespace["_instance_is_alive"]
PID = "2345"


class NoSuchProcess(Exception):
    pass


class AccessDenied(Exception):
    pass


def expected_liveness(instance, evidence):
    kind = evidence["kind"]
    if kind == "inventoryUnavailable":
        return reference(PID, instance, None)
    if kind == "present":
        return reference(PID, instance, {PID: evidence.get("createTime")})
    direct = evidence["direct"]

    def process(pid):
        assert pid == int(PID)
        if direct["kind"] == "notRunning":
            raise NoSuchProcess()
        if direct["kind"] == "accessDenied":
            raise AccessDenied()
        return types.SimpleNamespace(create_time=lambda: direct.get("createTime"))

    fake = types.SimpleNamespace(Process=process, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied)
    with patch.dict(sys.modules, psutil=fake):
        return reference(PID, instance, {})


def normalize_port(port):
    if not port or port.startswith("─"):
        return None
    match = re.match(r"(COM\d+|/dev/\S+)", port)
    return (match.group(1) if match else port.split()[0]).upper()


def main():
    timestamps = [None, 0, 1, 98.0, 98.0001, 100.0, 101.9999, 102.0, 5000.5]
    evidence = [dict(kind="inventoryUnavailable")]
    evidence += [dict(kind="present", createTime=value) for value in timestamps]
    evidence += [dict(kind="missing", direct=dict(kind=kind)) for kind in ("notRunning", "accessDenied")]
    evidence += [dict(kind="missing", direct=dict(kind="present", createTime=value)) for value in timestamps]
    instances = [{}, {"create_time": None}]
    instances += [{"create_time": value} for value in timestamps if value is not None]
    cases = []
    expected = []
    for instance, process in itertools.product(instances, evidence):
        cases.append(dict(operation="liveness", instance=instance, evidence=process))
        expected.append(dict(success=True, reservesResource=expected_liveness(instance, process)))
    liveness_cases = len(cases)
    ports = ["", "─ Select port ─", "COM1", "COM10 description", "COM5: USB serial",
             "COM9x", "com9 USB serial", "/dev/ttyUSB0", "/dev/ttyACM0 USB Serial",
             "/dev/serial/by-id/usb-Café_日本語", "custom-device description", "  COM9 description",
             "\t/dev/ttyUSB0\tlabel", "COM0009", "COM0", "COM1\u00a0serial", "COM2\r\nlabel"]
    for port in ports:
        cases.append(dict(operation="port", port=port))
        expected.append(dict(success=True, value=normalize_port(port)))
    # Deliberate structural refusals: malformed records and ambiguous tokens
    # must not turn into evidence that an owned resource is available.
    refusals = [dict(operation="liveness", instance=value,
                     evidence=dict(kind="present", createTime=100.0))
                for value in (None, [], {"create_time": "100"}, {"create_time": True})]
    refusals += [dict(operation="port", port=port) for port in ("   ", "COM١ USB", "COM² USB")]
    cases.extend(refusals)
    expected.extend(dict(success=False) for _ in refusals)
    exe = ROOT / "desktop/target/debug" / ("ownership_probe.exe" if sys.platform == "win32" else "ownership_probe")
    result = subprocess.run([str(exe)], input=json.dumps(cases), text=True, encoding="utf-8",
                            capture_output=True, check=True, timeout=10)
    assert len(result.stdout) < 2_000_000 and len(result.stderr) < 4096
    actual = json.loads(result.stdout)
    assert len(actual) == len(expected)
    for index, (before, after) in enumerate(zip(expected, actual)):
        assert before == after, f"Ownership case {index}: {cases[index]!r}: {before!r} != {after!r}"
    print(json.dumps(dict(reference="main/core/config.py::_instance_is_alive and port_occupied_owner token policy",
                          cases=len(cases), livenessCases=liveness_cases, portCases=len(ports),
                          structuredRefusals=len(refusals), passed=True,
                          settingsAccess=False, processDiscovery=False, deviceAccess=False,
                          productionRegistrationQualified=False)))


if __name__ == "__main__":
    main()
