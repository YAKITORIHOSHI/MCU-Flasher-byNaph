"""Compare pure budget decisions with Python; no host probes/settings/env writes."""
from pathlib import Path
import ast
import itertools
import json
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
source = ROOT / "main/core/build_resources.py"
tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
names = {"_system_reserved_cpu_count", "_resource_safe_worker_count", "get_optimal_compiler_jobs"}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
assert len(functions) == len(names)
# Execute the original function bodies without the module's import-time host
# discovery and environment mutation. Imported providers return fixture metrics.
runtime = types.ModuleType("src.modules.runtime_resources")
runtime.logical_cpu_count = lambda: metrics["logicalCpus"]
runtime.physical_cpu_count = lambda: metrics["physicalCpus"]
storage = types.ModuleType("main.core.storage_resources")
storage.storage_worker_limit = lambda *args, **kwargs: metrics["storageLimit"]
sys.modules[runtime.__name__] = runtime
sys.modules[storage.__name__] = storage
reference = {"_available_memory_gb": lambda: metrics["availableGib"]}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), reference)

cases = [dict(mode=mode, logicalCpus=logical, physicalCpus=physical,
              availableGib=memory, storageLimit=storage_limit)
         for mode, logical, physical, memory, storage_limit in itertools.product(
             ["LOW", "medium", "HIGH", "ULTRA", "MAX", "MAXIMUM", "unknown"],
             [1, 2, 4, 6, 8, 12, 32, 64],
             [None, 4, 6, 8],
             [None, 0.25, 0.5, 0.99, 1.0, 1.99, 2.0, 3.99, 4.0, 16.0],
             [None, 1, 2, 4])]
expected = []
for metrics in cases:
    expected.append(dict(
        safe=reference["_resource_safe_worker_count"](
            metrics["mode"], total_cpus=metrics["logicalCpus"],
            physical_cpus=metrics["physicalCpus"], available_gb=metrics["availableGib"]),
        optimal=reference["get_optimal_compiler_jobs"](metrics["mode"])))
exe = ROOT / "desktop/target/debug" / ("resource_probe.exe" if sys.platform == "win32" else "resource_probe")
result = subprocess.run([str(exe)], input=json.dumps(cases), text=True,
                        encoding="utf-8", capture_output=True, check=True)
actual = json.loads(result.stdout)
assert len(actual) == len(expected)
for index, (before, after) in enumerate(zip(expected, actual)):
    assert before == after, f"Budget case {index}: {cases[index]!r}: {before!r} != {after!r}"
print(json.dumps(dict(reference="main/core/build_resources.py", cases=len(cases),
                      passed=True, hostProbes=False, settingsAccess=False, environmentWrites=False)))
