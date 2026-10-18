"""Differential snapshot-merge fixtures; no settings files or locks are opened."""
from pathlib import Path
import importlib.util
import itertools
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("migration_config_reference", ROOT / "main/core/config_store.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
values = [None, False, True, 0, 1, 1.0, "glass", [], [True, 1], {"theme":"glass"}, {"theme":"light","font":14}]
cases = [[{"setting":current,"keep":"concurrent"}, {"setting":original}, {"setting":edited}]
         for current, original, edited in itertools.product(values, repeat=3)]
cases += [[{"new":{"other":1},"delete":2},{"delete":1},{"new":{"added":3}}]]
expected = []
for current, original, edited in cases:
    try:
        expected.append(dict(success=True, merged=reference.merge_changes(current, original, edited)))
    except (TypeError, AttributeError):
        expected.append(dict(success=False))
exe = ROOT / "desktop/target/debug" / ("preferences_probe.exe" if sys.platform == "win32" else "preferences_probe")
result = subprocess.run([str(exe)],input=json.dumps(cases),text=True,encoding="utf-8",capture_output=True,check=True)
actual=json.loads(result.stdout)
assert len(actual)==len(expected)
for index,(before,after) in enumerate(zip(expected,actual)):
    assert before==after, f"Preference merge case {index}: {before!r} != {after!r}"
print(json.dumps(dict(reference="main/core/config_store.py::merge_changes",cases=len(cases),passed=True,settingsAccess=False)))
