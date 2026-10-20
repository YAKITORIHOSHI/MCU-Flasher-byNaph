"""Differential fixture for the two pure recipe decisions; no app imports."""
from pathlib import Path
import ast
import copy
import json
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "main/core/platformio_board_compat.py"
NAMES = ("onehorse_recipe_options", "minimain_recipe_options")
CONSTANTS = {"ONEHORSE_RULE", "MINIMAIN_RULE", "_ONEHORSE_BOARD_SHA256",
             "_MINIMAIN_BOARD_SHA256", "_ARDUINO_PACKAGE", "_SDK_PACKAGE", "_S2_SDK_PACKAGE"}
INPUT_LIMIT = 8_000_000
CASE_LIMIT = 32_768
OUTPUT_LIMIT = 32_000_000
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
constants = [node for node in tree.body if isinstance(node, ast.Assign)
             and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
             and node.targets[0].id in CONSTANTS]
assert len(functions) == len(NAMES) and len(constants) == len(CONSTANTS)
assert not any(isinstance(node, (ast.Import, ast.ImportFrom))
               for function in functions for node in ast.walk(function))
for constant in constants:
    ast.literal_eval(constant.value)
namespace = {}
exec(compile(ast.Module(body=constants + functions, type_ignores=[]), str(SOURCE), "exec"), namespace)

ARDUINO = "framework-arduinoespressif32"
S2_FLAGS = ["-DARDUINO_DEPARTMENT_OF_ALCHEMY_MINIMAIN_ESP32S2", "-DARDUINO_RUNNING_CORE=1",
            "-DARDUINO_EVENT_RUNNING_CORE=1", "-DBOARD_HAS_PSRAM", "-DARDUINO_USB_MODE=1",
            "-DARDUINO_USB_CDC_ON_BOOT=1"]
ONEHORSE_EXPECTED = {"board_build.boot": "dio", "board_build.arduino.memory_type": "dio_qspi"}
MINIMAIN_EXPECTED = {"board_build.extra_flags": "-DARDUINO_DEPARTMENT_OF_ALCHEMY_MINIMAIN_ESP32S2 -DARDUINO_RUNNING_CORE=1 -DARDUINO_EVENT_RUNNING_CORE=1 -DBOARD_HAS_PSRAM -DARDUINO_USB_MODE=0 -DARDUINO_USB_CDC_ON_BOOT=1"}


def seed(s2):
    sdk = ARDUINO + ("-esp32s2-libs" if s2 else "-esp32-libs")
    return dict(platformName="espressif32", boardId="minimain_esp32s2" if s2 else "onehorse32dev",
                framework="arduino", boardSha256=namespace["_MINIMAIN_BOARD_SHA256" if s2 else "_ONEHORSE_BOARD_SHA256"],
                board={"build": dict(mcu="esp32s2", variant="department_of_alchemy_minimain_esp32s2", extra_flags=S2_FLAGS[:])
                       if s2 else dict(mcu="esp32", core="esp32", variant="onehorse32dev", flash_mode="dout")},
                platform=dict(name="espressif32", version="8.0.0",
                              frameworks={"arduino": dict(package=ARDUINO, script="builder/frameworks/arduino.py")},
                              packages={ARDUINO: dict(owner="platformio"), sdk: dict(owner="platformio")}),
                frameworkPackage=dict(name=ARDUINO, version="4.30312.261016"),
                sdkPackage=dict(name=sdk, version="3.3.12"))


def expected(case):
    values = []
    for name in NAMES:
        options = namespace[name](case.get("platformName"), case.get("boardId"), case.get("framework"),
                                  board=case.get("board"), platform=case.get("platform"),
                                  framework_package=case.get("frameworkPackage"), sdk_package=case.get("sdkPackage"),
                                  board_sha256=case.get("boardSha256"))
        if options:
            values.append(dict(options=options, rule=namespace["ONEHORSE_RULE" if name == NAMES[0] else "MINIMAIN_RULE"]))
    assert len(values) <= 1
    return values[0] if values else dict(options={}, rule=None)


def paths(value, prefix=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield prefix + (key,)
            yield from paths(child, prefix + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield prefix + (index,)
            yield from paths(child, prefix + (index,))


def changed(base, path, value=None, remove=False):
    result = copy.deepcopy(base)
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    if remove:
        del parent[path[-1]]
    else:
        parent[path[-1]] = copy.deepcopy(value)
    return result


cases = []
labels = []


def add(label, case, want=None):
    if want is not None:
        assert expected(case)["options"] == want, (label, expected(case), want)
    cases.append(case)
    labels.append(label)


for s2 in (False, True):
    base = seed(s2)
    board = base["boardId"]
    literal = MINIMAIN_EXPECTED if s2 else ONEHORSE_EXPECTED
    add(board + ":exact", base, literal)
    # Every required field, nested container and ordered list element is
    # independently missing or replaced with a wrong JSON type/value.
    for path in paths(base):
        add(f"{board}:missing:{path}", changed(base, path, remove=True), {})
        for value in (None, False, True, 0, 1, -1, 1.5, "", "changed", [], {}, [None], {"value": "changed"}):
            add(f"{board}:changed:{path}:{value!r}", changed(base, path, value), {})
    for path in ((), ("board",), ("board", "build"), ("platform",),
                 ("frameworkPackage",), ("sdkPackage",)):
        extended = copy.deepcopy(base)
        parent = extended
        for key in path:
            parent = parent[key]
        parent["future_unrelated_metadata"] = [None, {"retained": True}]
        add(f"{board}:unrelated:{path}", extended, literal)
    add(board + ":wrong-sdk-generation", changed(base, ("sdkPackage", "name"),
        ARDUINO + ("-esp32-libs" if s2 else "-esp32s2-libs")), {})
    for path, value in ((("platform", "version"), "8.0.1"),
                        (("frameworkPackage", "version"), "4.30313.0"),
                        (("sdkPackage", "version"), "3.3.13"),
                        (("boardSha256",), "0" * 64),
                        (("boardSha256",), base["boardSha256"].upper()),
                        (("platform", "frameworks", "arduino", "script"), "builder/other.py")):
        add(f"{board}:future-native:{path}", changed(base, path, value), {})
    for field in ("platformName", "boardId", "framework"):
        for value in (base[field].upper(), base[field] + " ", base[field] + "\nother=value"):
            add(f"{board}:selection:{field}:{value!r}", changed(base, (field,), value), {})
    if s2:
        for index in range(len(S2_FLAGS) - 1):
            flags = S2_FLAGS[:]
            flags[index], flags[index + 1] = flags[index + 1], flags[index]
            add(f"{board}:flag-order:{index}", changed(base, ("board", "build", "extra_flags"), flags), {})
        for flags in (S2_FLAGS + ["-DNEW_FLAG=1"], " ".join(S2_FLAGS), S2_FLAGS[:4] + ["-DARDUINO_USB_MODE=0"] + S2_FLAGS[5:]):
            add(f"{board}:native-flags:{flags!r}", changed(base, ("board", "build", "extra_flags"), flags), {})
    else:
        for arduino in ({}, {"future_option": "retained"}):
            add(f"{board}:missing-memory-type:{arduino}", changed(base, ("board", "build", "arduino"), arduino), literal)
        for arduino in (None, False, True, 0, "", [], [None]):
            add(f"{board}:malformed-arduino:{arduino!r}", changed(base, ("board", "build", "arduino"), arduino), {})
        for path in (("board", "build", "boot"), ("board", "build", "memory_type"),
                     ("board", "build", "arduino", "memory_type")):
            for value in (None, False, 0, "", "dio", "dio_qspi", {}, []):
                extended = copy.deepcopy(base)
                extended["board"]["build"]["arduino"] = {}
                add(f"{board}:explicit-override:{path}:{value!r}", changed(extended, path, value), {})

for value in (None, False, True, 0, 1, "", [], [seed(False)]):
    cases.append(value)
    labels.append(f"malformed-case:{value!r}")
for target_platform, board in (("arduinoq", "uno_q"), ("espressif32", "future-board")):
    case = seed(False)
    case.update(platformName=target_platform, boardId=board)
    add(f"unported-selection:{target_platform}:{board}", case, {})

exe = ROOT / "desktop/target/debug" / ("board_recipe_probe.exe" if sys.platform == "win32" else "board_recipe_probe")


def run(raw):
    result = subprocess.run([str(exe)], input=raw, capture_output=True, timeout=30, check=False)
    assert len(result.stdout) <= OUTPUT_LIMIT and len(result.stderr) <= 4096
    return result


raw = json.dumps(cases, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
assert len(cases) <= CASE_LIMIT and len(raw) <= INPUT_LIMIT
result = run(raw)
assert result.returncode == 0, result.stderr
actual = json.loads(result.stdout)
assert len(actual) == len(cases), ("result count", len(actual), len(cases))
positives = refusals = 0
for label, case, observed in zip(labels, cases, actual):
    want = expected(case) if isinstance(case, dict) else dict(options={}, rule=None)
    assert observed == want, (label, want, observed)
    if want["options"]:
        positives += 1
    else:
        refusals += 1

# The probe accepts its exact byte/case boundaries and rejects oversized or
# malformed envelopes before producing a successful decision stream.
boundary = b"[null]" + b" " * (INPUT_LIMIT - len(b"[null]"))
result = run(boundary)
assert result.returncode == 0 and json.loads(result.stdout) == [dict(options={}, rule=None)]
result = run(b"[null]" + b" " * (INPUT_LIMIT + 1 - len(b"[null]")))
assert result.returncode != 0 and not result.stdout
result = run(json.dumps([None] * CASE_LIMIT, separators=(",", ":")).encode())
assert result.returncode == 0 and len(json.loads(result.stdout)) == CASE_LIMIT
result = run(json.dumps([None] * (CASE_LIMIT + 1), separators=(",", ":")).encode())
assert result.returncode != 0 and not result.stdout
for malformed in (b"", b"{", b"{}", b"null", b"[] trailing", b"[NaN]", b"[\xff]"):
    result = run(malformed)
    assert result.returncode != 0 and not result.stdout, malformed

print(json.dumps(dict(reference=str(SOURCE.relative_to(ROOT)), functions=NAMES, cases=len(cases),
                      positiveDecisions=positives, emptyDecisions=refusals, equivalentComparisons=len(cases),
                      protocolBoundaryChecks=11, inputBytes=len(raw), maximumInputBytes=INPUT_LIMIT,
                      maximumCases=CASE_LIMIT, maximumOutputBytes=OUTPUT_LIMIT, passed=True,
                      hardwareAccess=False, productionImports=False, filesystemReaderQualified=False,
                      arduinoQSelectionQualified=False, platform=sys.platform, architecture=platform.machine())))
