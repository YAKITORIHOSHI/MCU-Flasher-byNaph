"""Compare pure transport and board-ID helpers without production imports."""
from pathlib import Path
import ast
import itertools
import json
import platform
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
NAMES = ("requires_upload_port", "serial_upload_speed", "upload_configuration", "upload_target_ready")
BOARD_NAME = "valid_platformio_board_id"
source = ROOT / "main/core/target_profile.py"
tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in (*NAMES, BOARD_NAME)]
assert len(functions) == len(NAMES) + 1
assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for function in functions for node in ast.walk(function))
board_patterns = [node for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "_BOARD_IDENTIFIER" for target in node.targets)]
assert len(board_patterns) == 1
strict_patterns = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "_IDENTIFIER" for target in node.targets)]
target_checks = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "target_problem"]
assert len(strict_patterns) == len(target_checks) == 1
constant_tree = ast.parse((ROOT / "main/core/constants.py").read_text(encoding="utf-8"))
constants = [node for node in constant_tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == "MAX_BAUD_RATE" for target in node.targets)]
assert len(constants) == 1
maximum_baud = ast.literal_eval(constants[0].value)
namespace = dict(Mapping=dict, MAX_BAUD_RATE=maximum_baud, re=re)
exec(compile(ast.Module(body=board_patterns + functions, type_ignores=[]), str(source), "exec"), namespace)
exec(compile(ast.Module(body=strict_patterns + target_checks, type_ignores=[]), str(source), "exec"), namespace)

# These ordinary PlatformIO rows cannot enter target_problem's Arduino CLI
# import branches. Keep this Python policy guard distinct from Rust parity:
# the Rust prototype still does not implement full target validation.
strict_base = dict(backend="platformio", platform="ststm8", board="STC89C516RD+", framework="arduino",
                   frameworks=["arduino"], upload_protocol="stlink", pio_resolved=True)
assert namespace["target_problem"](strict_base) == ""
strict_identifier_comparisons = 1
for field in ("platform", "framework", "upload_protocol"):
    for value in (strict_base[field] + "+", "../" + strict_base[field], "--" + strict_base[field],
                  strict_base[field] + "\nother = injected", strict_base[field] + ";option"):
        info = dict(strict_base, **{field: value})
        assert namespace["target_problem"](info), (field, value)
        strict_identifier_comparisons += 1

protocols = ["stlink", "jlink", "cmsis-dap", "dfu", "picotool", "mbed", "uf2", "teensy-cli",
             "teensy-gui", "micronucleus", "wch-link", "nrfjprog", "atmel-ice", "usbasp", "usbtiny",
             "esptool", "arduino", "avrdude", "serial", "future-programmer", "", "STLINK", " dfu ",
             "custom\nsecond_setting = 1"]
platforms = ["espressif32", "espressif8266", "atmelavr", "raspberrypi", "ststm32", "", None,
             "ESPRESSIF32", "espressif32 "]
values = ["", None, False, True, 0, 1, -1, 115200, 921600, 3000000, 9999999999, 10000000000,
          115200.0, " 0921601 ", "\x1c115200\x1f", "١١٥٢٠٠", "１２３", "1e6", "00000000001",
          [], [115200], {}, {"value":115200}]
cases = []
for protocol, target_platform, requested in itertools.product(protocols, platforms, values):
    cases.append([dict(board="dynamic.custom-board_42", platform=target_platform,
                       upload_protocol=protocol, upload_speed=57600, pio_resolved=True), requested, ""])
for protocol, target_platform, speed in itertools.product(protocols, ("atmelavr", "espressif32", "espressif8266"), values):
    cases.append([dict(board="dynamic-board", platform=target_platform, upload_protocol=protocol,
                       upload_speed=speed), "3000000", "COM999"])
for protocol, declared in itertools.product(protocols, (None, False, True, 0, 1, "false", "true", [], {}, [False])):
    cases.append([dict(board="future-programmer-board", platform="espressif32", upload_protocol=protocol,
                       require_upload_port=declared), "3000000", ""])
for backend, protocol, target_platform, requested in itertools.product(
        ("arduino-cli", "platformio", "ARDUINO-CLI", None, False, ["arduino-cli"]),
        protocols, ("atmelavr", "espressif32", "espressif8266"), ("", "115200", "3000000", 115200.0)):
    cases.append([dict(backend=backend, board="dynamic-cli-target", platform=target_platform,
                       upload_protocol=protocol, upload_speed=115200), requested, ""])
for board, resolved, port, protocol in itertools.product(
        (None, False, True, 0, 1, "", "future-board", [], [1], {}, {"id":"board"}),
        (None, False, True, 0, 1, "false", [], {}),
        ("", None, False, True, 0, 1, "COM999", "/dev/fixture", [], [False], {}, {"port":1}),
        ("dfu", "esptool", "future-programmer", "STLINK")):
    cases.append([dict(board=board, pio_resolved=resolved, upload_protocol=protocol), "", port])
for protocol, target_platform in itertools.product(values, platforms):
    cases.append([dict(board="future-board", upload_protocol=protocol, platform=target_platform), "115200", ""])
for info, port in itertools.product((None, False, True, 0, 1, "", [], ["target"]), ("", "COM999")):
    cases.append([info, "115200", port])
board_id_cases = [
    "STC89C516RD+", "board++", "Board._-+42", "0", "1+plus", "A" + "+" * 159,
    "A" + "+" * 160, "", "+board", "-board", "_board", ".board", "../board", "a/board",
    "a\\board", "C:\\board", "board\nplatform = injected", "board\r\n[env:other]", "board\n",
    "board=other", "board;option", "board#comment", "board${section}", "board --option",
    " board", "board ", "böard", "ＳＴＣ", "\x00board", "board\x00", None, False, True, 0, 1,
    [], ["STC89C516RD+"], {}, {"board": "STC89C516RD+"},
]
board_id_cases.extend(value for code in range(128) for value in (chr(code), "A" + chr(code), chr(code) + "A+"))
for board in board_id_cases:
    cases.append([dict(board=board, platform="ststm8", upload_protocol="stlink", pio_resolved=True), "", ""])

raw = json.dumps(cases, ensure_ascii=False, allow_nan=False)
assert len(cases) <= 32768 and len(raw.encode("utf-8")) <= 8_000_000
exe = ROOT / "desktop/target/debug" / ("target_probe.exe" if sys.platform == "win32" else "target_probe")
result = subprocess.run([str(exe)], input=raw, text=True, encoding="utf-8", capture_output=True,
                        check=True, timeout=30)
assert len(result.stdout.encode("utf-8")) <= 32_000_000 and len(result.stderr) <= 4096
actual = json.loads(result.stdout)
assert len(actual) == len(cases)
equivalent = 0
board_id_comparisons = 0
refusals = dict(info=0, upload_protocol=0)
for index, ((info, requested, port), observed) in enumerate(zip(cases, actual)):
    board = info.get("board") if isinstance(info, dict) else None
    expected_board_id = namespace[BOARD_NAME](board)
    assert observed[BOARD_NAME] is expected_board_id, (index, BOARD_NAME, board, expected_board_id, observed)
    board_id_comparisons += 1
    for name in NAMES:
        unsupported = "info" if not isinstance(info, dict) else None
        if name == "upload_configuration" and isinstance(info, dict):
            protocol = info.get("upload_protocol")
            if protocol and not isinstance(protocol, str):
                unsupported = "upload_protocol"
        value = observed[name]
        if unsupported:
            assert value.get("success") is False and value["error"]["code"] == "invalid-target-field", (index, name, value)
            assert value["error"]["field"] == unsupported, (index, name, value)
            refusals[unsupported] += 1
            continue
        args = [info]
        if name in ("serial_upload_speed", "upload_configuration"):
            args.append(requested)
        elif name == "upload_target_ready":
            args.append(port)
        expected = dict(success=True, value=namespace[name](*args))
        assert value == expected, (index, name, cases[index], expected, value)
        equivalent += 1
print(json.dumps(dict(reference="main/core/target_profile.py", functions=NAMES, cases=len(cases),
                      equivalentComparisons=equivalent, structuredRefusalComparisons=sum(refusals.values()),
                      boardIdentityFunction=BOARD_NAME, boardIdentityComparisons=board_id_comparisons,
                      explicitBoardIdentityCases=len(board_id_cases),
                      pythonStrictIdentifierComparisons=strict_identifier_comparisons,
                      validationDifferences=[dict(field=field, comparisons=count,
                          reason="Target must be an object" if field == "info" else
                          "Truthy non-string protocols are refused instead of formatting Python representations into INI")
                          for field, count in refusals.items()],
                      maximumEspBaud=maximum_baud, platform=sys.platform, architecture=platform.machine(),
                      passed=True, hardwareAccess=False, productionImports=False,
                      fullTargetValidationQualified=False)))
