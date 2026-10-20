"""Exact owned-fixture differential; AST-extracted reference, no app imports."""
from pathlib import Path
import ast
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "main/core/platformio_board_compat.py"
FUNCTIONS = {"onehorse_recipe_options", "minimain_recipe_options", "_read_recipe_bytes",
             "_read_metadata", "board_compatibility_options", "_arduinoq_options", "compatibility_rule"}
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
nodes = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
        nodes.append(node)
    elif isinstance(node, ast.Assign):
        ast.literal_eval(node.value)
        nodes.append(node)
assert sum(isinstance(node, ast.FunctionDef) for node in nodes) == len(FUNCTIONS)
reference = {"hashlib": hashlib, "json": json, "os": os, "Path": Path, "stat": stat}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), reference)

# These are literal copies of upstream raw bytes, not substituted digest values.
fixture_tree = ast.parse((ROOT / "direct/verify_platformio_board_compat.py").read_text(encoding="utf-8"))
literals = {node.targets[0].id: ast.literal_eval(node.value) for node in fixture_tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"BOARD_BYTES", "MINIMAIN_BYTES"}}
q_class = next(node for node in fixture_tree.body if isinstance(node, ast.ClassDef) and node.name == "UnoQChecks")
q_literals = ast.literal_eval(next(node.value for node in q_class.body if isinstance(node, ast.Assign)
                                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "METADATA"))
HOOK = (ROOT / "src/modules/arduinoq_link_paths.py").read_bytes()
EXE = ROOT / "desktop/target/debug" / ("board_compatibility_probe.exe" if sys.platform == "win32" else "board_compatibility_probe")
SCRATCH = ROOT / "audit/migration/scratch/board-compatibility"
SCRATCH.mkdir(parents=True, exist_ok=True)
counts = {"equivalentComparisons": 0, "positiveDecisions": 0, "emptyDecisions": 0,
          "typedRefusals": 0, "noReadChecks": 0, "structuralRefusals": 0,
          "protocolBoundaryChecks": 0, "platformSkips": 0, "referenceExceptions": 0,
          "strictParserRefusals": 0}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, separators=(",", ":")).encode())


def seed(root, kind):
    core = root / "core"
    is_q = kind == "q"
    board = {"onehorse": "onehorse32dev", "minimain": "minimain_esp32s2", "q": "uno_q"}[kind]
    platform_name = "arduinoq" if is_q else "espressif32"
    install = core / "platforms" / platform_name
    info = dict(platform=platform_name, board=board, framework="arduino",
                pio_manifest=str(install / "boards" / (board + ".json")))
    if is_q:
        for name, raw in q_literals.items():
            path = install / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        framework = {"name": "framework-arduino-zephyr", "version": "0.56.0-leelabskku+unoq"}
        write_json(core / "packages/framework-arduino-zephyr/package.json", framework)
    else:
        manifest = Path(info["pio_manifest"])
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_bytes(literals["BOARD_BYTES" if kind == "onehorse" else "MINIMAIN_BYTES"])
        sdk = "framework-arduinoespressif32-" + ("esp32-libs" if kind == "onehorse" else "esp32s2-libs")
        framework = {"name": "framework-arduinoespressif32", "version": "4.30312.261016"}
        write_json(install / "platform.json", dict(name="espressif32", version="8.0.0",
                   frameworks={"arduino": dict(package=framework["name"], script="builder/frameworks/arduino.py")},
                   packages={name: {"owner": "platformio"} for name in (framework["name"], sdk)}))
        write_json(core / "packages" / framework["name"] / "package.json", framework)
        write_json(core / "packages" / sdk / "package.json", dict(name=sdk, version="3.3.12"))
    hook = root / "application/src/modules/arduinoq_link_paths.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(HOOK)
    return info


def fingerprint(root):
    result = {}
    for path in root.rglob("*"):
        metadata = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(metadata.st_mode) else None
        result[str(path.relative_to(root))] = (metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, digest)
    return result


def invoke(root, raw):
    result = subprocess.run([str(EXE), "--fixture-root", str(root)], input=raw,
                            capture_output=True, timeout=8, check=False)
    assert len(result.stdout) <= 4_000_000 and len(result.stderr) <= 4096
    return result


def compare(root, cases, *, no_reads=False, structural=False, strict_parser=False):
    reference["__file__"] = str(root / "application/main/core/platformio_board_compat.py")
    before = fingerprint(root)
    expected = []
    for case in cases:
        try:
            expected.append(reference["board_compatibility_options"](case, root / "core"))
        except TypeError:
            # The Python native-record wrapper assumes a hashable board field
            # before entering its failure handler. The typed probe explicitly
            # refuses the malformed envelope; do not call this a decision match.
            expected.append(None)
    raw = json.dumps(cases, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    result = invoke(root, raw)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert len(actual) == len(cases)
    for case, want, observed in zip(cases, expected, actual):
        if want is None:
            assert observed["options"] == {} and observed["refusal"] == "InvalidMetadata", (case, observed)
            counts["referenceExceptions"] += 1
        elif strict_parser:
            assert want and observed["options"] == {} and observed["refusal"] == "InvalidMetadata", (case, want, observed)
            counts["strictParserRefusals"] += 1
        elif structural:
            assert observed["options"] == {} and observed["refusal"] is not None, (case, observed)
            counts["structuralRefusals"] += 1
        else:
            rule = reference["compatibility_rule"](want) if want else None
            assert observed["options"] == want and observed["rule"] == rule, (case, want, observed)
            counts["equivalentComparisons"] += 1
            counts["positiveDecisions" if want else "emptyDecisions"] += 1
        assert observed["bytesRead"] <= 5 * 65537
        if observed["refusal"]:
            counts["typedRefusals"] += 1
        if no_reads:
            assert observed["readAttempts"] == 0 and observed["bytesRead"] == 0, observed
            counts["noReadChecks"] += 1
    assert fingerprint(root) == before, "Reader/reference changed fixture bytes or metadata"
    return actual


for kind in ("onehorse", "minimain", "q"):
    with tempfile.TemporaryDirectory(prefix="recipe café 日本語 ", dir=SCRATCH) as folder:
        root = Path(folder)
        info = seed(root, kind)
        exact = compare(root, [info])[0]
        if kind != "q" or sys.platform == "linux":
            assert exact["options"], (kind, exact)
        else:
            assert exact["readAttempts"] == 0
        unknown = [None, [], False, 42, "record", {}, {**info, "backend": "arduino-cli"}]
        for field in ("platform", "board", "framework"):
            for value in (None, False, 42, [], {}, "", "unrelated", info[field].upper(), info[field] + " "):
                unknown.append({**info, field: value})
        compare(root, unknown, no_reads=True)
        compare(root, [{**info, "backend": value, "extra_scripts": "post:untrusted.py",
                        "custom_mcu_arduinoq_hook_sha256": "untrusted"}
                       for value in (None, False, True, {}, [], "platformio")])
        for value in (None, [], {}, "", str(root / "outside.json"), str(root / "core/platforms/other/boards/wrong.json")):
            compare(root, [{**info, "pio_manifest": value}])
        files = [p for p in (root / "core").rglob("*") if p.is_file()]
        for path in files:
            original = path.read_bytes()
            for replacement in (b"[]", b"null", b"{", b"\xff", b" " * 65537):
                path.write_bytes(replacement)
                compare(root, [info])
            path.write_bytes(original)
            if path.name.endswith(".json"):
                path.write_bytes(b"\xef\xbb\xbf" + original)
                compare(root, [info])
                path.write_bytes(original)
                # Python accepts nonstandard numbers, unbounded integers and
                # nesting beyond serde's parser guard in otherwise unused
                # fields. These are explicit development parser refusals.
                mutable_metadata = path.name == "package.json" or (kind != "q" and path.name == "platform.json")
                if mutable_metadata and (kind != "q" or sys.platform == "linux"):
                    for token in (b"NaN", b"1" + b"0" * 400, b"[" * 160 + b"0" + b"]" * 160):
                        path.write_bytes(original.rstrip()[:-1] + b',"unused":' + token + b"}")
                        compare(root, [info], strict_parser=True)
                    path.write_bytes(original)
            if path == Path(info["pio_manifest"]):
                path.write_bytes(original + b" ")
                compare(root, [info])
                path.write_bytes(original)
        if kind == "q" and sys.platform == "linux":
            hook = root / "application/src/modules/arduinoq_link_paths.py"
            hook.write_bytes(HOOK + b"\n# changed fixture hook\n")
            changed = compare(root, [info])[0]
            assert changed["options"]["custom_mcu_arduinoq_hook_sha256"] != exact["options"]["custom_mcu_arduinoq_hook_sha256"]
            assert changed["options"]["extra_scripts"] == exact["options"]["extra_scripts"]
            hook.write_bytes(HOOK)
        if sys.platform == "linux":
            manifest = Path(info["pio_manifest"])
            original = manifest.read_bytes()
            manifest.unlink()
            os.mkfifo(manifest)
            compare(root, [info])
            manifest.unlink()
            manifest.write_bytes(original)
            alias_target = root / "same-bytes.json"
            alias_target.write_bytes(original)
            manifest.unlink()
            manifest.symlink_to(alias_target)
            compare(root, [info])
            manifest.unlink()
            manifest.write_bytes(original)
            # Python accepts an internal parent alias. The fixture reader
            # deliberately refuses every alias; count this separately.
            boards = manifest.parent
            moved = boards.with_name("retained-boards")
            boards.rename(moved)
            boards.symlink_to(moved, target_is_directory=True)
            compare(root, [info], structural=True)
            boards.unlink()
            moved.rename(boards)
        else:
            counts["platformSkips"] += 3
        # Exact envelope boundaries and invalid input never produce success.
        for raw in (b"", b"{", b"{}", b"null", b"[] trailing", b"[NaN]", b"[\xff]",
                    b"[]" + b" " * 999999, json.dumps([None] * 257).encode()):
            result = invoke(root, raw)
            assert result.returncode != 0 and not result.stdout, raw[:40]
            counts["protocolBoundaryChecks"] += 1
        result = invoke(root, b"[]" + b" " * 999998)
        assert result.returncode == 0 and json.loads(result.stdout) == []
        counts["protocolBoundaryChecks"] += 1
        compare(root, [None] * 256, no_reads=True)

if sys.platform == "linux":
    with tempfile.TemporaryDirectory(prefix="control\n", dir=SCRATCH) as folder:
        root = Path(folder)
        info = seed(root, "q")
        assert compare(root, [info])[0]["options"] == {}
else:
    counts["platformSkips"] += 1

# Construction cannot adopt the checkout or its scratch parent as a fixture.
for refused in (ROOT, ROOT / "audit/migration/scratch"):
    result = invoke(refused, b"[]")
    assert result.returncode != 0 and not result.stdout
    counts["protocolBoundaryChecks"] += 1

print(json.dumps(dict(reference=str(SOURCE.relative_to(ROOT)), **counts, passed=True,
                      platform=sys.platform, architecture=platform.machine(),
                      nativeDescriptorSnapshots=True, exactRawFixtureHashes=True,
                      fixtureOnly=True, productionImports=False, hardwareAccess=False,
                      livePackageAccess=False, packageWrites=False, buildAuthority=False,
                      packageRaceQualification=False, pythonServicesRetired=0)))
