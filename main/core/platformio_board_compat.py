"""Narrow compatibility decisions for verified installed PlatformIO recipes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

ONEHORSE_RULE = "onehorse32dev-espressif32-8-arduino-3.3.12-dout"
_ONEHORSE_BOARD_SHA256 = "483741724c41624f1be320ef09ec3396fc0f34d7d0bc633b092e257cd621990b"
_ARDUINO_PACKAGE = "framework-arduinoespressif32"
_SDK_PACKAGE = "framework-arduinoespressif32-esp32-libs"
_S2_SDK_PACKAGE = "framework-arduinoespressif32-esp32s2-libs"


def onehorse_recipe_options(platform_name, board_id, framework, *, board, platform,
                           framework_package, sdk_package, board_sha256):
    """Repair only the reviewed recipe; returned settings never select hardware.

    PlatformIO 8's unchanged Onehorse manifest omits boot and SDK memory type.
    Espressif's 3.3.12 boards.txt keeps DOUT but explicitly boots DIO. The
    corresponding SDK is dio_qspi. New board bytes, versions or explicit native
    overrides stay with their own builder; this is not a family-wide fallback.
    """
    if (platform_name, board_id, framework) != ("espressif32", "onehorse32dev", "arduino"):
        return {}
    if not all(isinstance(value, dict) for value in (board, platform, framework_package, sdk_package)):
        return {}
    if (board_sha256 != _ONEHORSE_BOARD_SHA256 or platform.get("name") != "espressif32"
            or platform.get("version") != "8.0.0"
            or framework_package.get("name") != _ARDUINO_PACKAGE
            or framework_package.get("version") != "4.30312.261016"
            or sdk_package.get("name") != _SDK_PACKAGE or sdk_package.get("version") != "3.3.12"):
        return {}
    frameworks = platform.get("frameworks")
    dispatch = frameworks.get("arduino") if isinstance(frameworks, dict) else None
    packages = platform.get("packages")
    if (not isinstance(dispatch, dict) or dispatch.get("package") != _ARDUINO_PACKAGE
            or dispatch.get("script") != "builder/frameworks/arduino.py" or not isinstance(packages, dict)
            or any(not isinstance(packages.get(name), dict) or packages[name].get("owner") != "platformio"
                   for name in (_ARDUINO_PACKAGE, _SDK_PACKAGE))):
        return {}
    build = board.get("build")
    arduino = build.get("arduino", {}) if isinstance(build, dict) else None
    if (not isinstance(build, dict) or not isinstance(arduino, dict)
            or build.get("mcu") != "esp32" or build.get("core") != "esp32"
            or build.get("variant") != "onehorse32dev" or build.get("flash_mode") != "dout"
            or "boot" in build or "memory_type" in build or "memory_type" in arduino):
        return {}
    return {"board_build.boot": "dio", "board_build.arduino.memory_type": "dio_qspi"}


def _read_metadata(path, boundary):
    """Read one regular, bounded selected metadata file without writing a store."""
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode) or not path.resolve(strict=True).is_relative_to(boundary):
        raise ValueError("Compatibility metadata must be a regular file inside the selected core")
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("Compatibility metadata exceeds 64 KiB")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Compatibility metadata must be a JSON object")
    return raw, value


def board_compatibility_options(info, core_dir):
    """Compile-worker-only adapter for native catalog data and a selected core.

    This reader does no discovery, installation or mutation. It ignores any
    renderer-supplied option map. Call it from compile/upload workers, never a
    GUI callback. Unknown or redirected metadata leaves the native recipe alone.
    """
    if isinstance(info, dict) and info.get("backend") != "arduino-cli" and (info.get("platform"), info.get("board"), info.get("framework")) == ("arduinoq", "uno_q", "arduino"):
        return _arduinoq_options(info, core_dir)
    if (not isinstance(info, dict) or info.get("backend") == "arduino-cli"
            or (info.get("platform"), info.get("framework")) != ("espressif32", "arduino") or info.get("board") not in {"onehorse32dev", "minimain_esp32s2"}):
        return {}
    try:
        core = Path(core_dir).resolve(strict=True)
        platforms = (core / "platforms").resolve(strict=True)
        packages = (core / "packages").resolve(strict=True)
        if platforms.parent != core or packages.parent != core:
            return {}
        manifest = Path(info.get("pio_manifest") or "")
        if (manifest.name != info["board"] + ".json" or manifest.parent.name != "boards"
                or manifest.parent.parent.parent.resolve(strict=True) != platforms):
            return {}
        raw, board = _read_metadata(manifest, platforms)
        _, platform = _read_metadata(manifest.parent.parent / "platform.json", platforms)
        _, framework = _read_metadata(packages / _ARDUINO_PACKAGE / "package.json", packages)
        sdk_name = _SDK_PACKAGE if info["board"] == "onehorse32dev" else _S2_SDK_PACKAGE
        _, sdk = _read_metadata(packages / sdk_name / "package.json", packages)
        decide = onehorse_recipe_options if info["board"] == "onehorse32dev" else minimain_recipe_options
        return decide("espressif32", info["board"], "arduino", board=board,
                                       platform=platform, framework_package=framework, sdk_package=sdk,
                                       board_sha256=hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError, TypeError, UnicodeError):
        return {}


MINIMAIN_RULE = "minimain-esp32s2-espressif32-8-arduino-3.3.12-usb"
_MINIMAIN_BOARD_SHA256 = "e9a066074564a920d0e9df6761ad366b5ae32755a67beb32e1ad5f307ceafe9c"


def minimain_recipe_options(platform_name, board_id, framework, *, board, platform,
                            framework_package, sdk_package, board_sha256):
    """Select S2 native USB CDC for only the exact reviewed upstream recipe.

    This S2 board's PlatformIO 8 flags incorrectly select hardware USB/JTAG CDC,
    which the S2 does not implement. Preserve every other native board flag and
    CDC-on-boot; replace only USB mode 1 with TinyUSB mode 0. No source, package,
    variant, pin, upload protocol or board identity changes.
    """
    if (platform_name, board_id, framework) != ("espressif32", "minimain_esp32s2", "arduino"):
        return {}
    if not all(isinstance(value, dict) for value in (board, platform, framework_package, sdk_package)):
        return {}
    if (board_sha256 != _MINIMAIN_BOARD_SHA256 or platform.get("name") != "espressif32"
            or platform.get("version") != "8.0.0"
            or framework_package.get("name") != _ARDUINO_PACKAGE
            or framework_package.get("version") != "4.30312.261016"
            or sdk_package.get("name") != _S2_SDK_PACKAGE or sdk_package.get("version") != "3.3.12"):
        return {}
    frameworks, packages = platform.get("frameworks"), platform.get("packages")
    dispatch = frameworks.get("arduino") if isinstance(frameworks, dict) else None
    if (not isinstance(dispatch, dict) or dispatch.get("package") != _ARDUINO_PACKAGE
            or dispatch.get("script") != "builder/frameworks/arduino.py" or not isinstance(packages, dict)
            or any(not isinstance(packages.get(name), dict) or packages[name].get("owner") != "platformio"
                   for name in (_ARDUINO_PACKAGE, _S2_SDK_PACKAGE))):
        return {}
    build = board.get("build")
    if (not isinstance(build, dict) or build.get("mcu") != "esp32s2"
            or build.get("variant") != "department_of_alchemy_minimain_esp32s2"):
        return {}
    flags = build.get("extra_flags")
    expected = ["-DARDUINO_DEPARTMENT_OF_ALCHEMY_MINIMAIN_ESP32S2", "-DARDUINO_RUNNING_CORE=1",
                "-DARDUINO_EVENT_RUNNING_CORE=1", "-DBOARD_HAS_PSRAM", "-DARDUINO_USB_MODE=1",
                "-DARDUINO_USB_CDC_ON_BOOT=1"]
    if flags != expected:
        return {}
    return {"board_build.extra_flags": " ".join("-DARDUINO_USB_MODE=0" if flag == "-DARDUINO_USB_MODE=1" else flag for flag in flags)}


def compatibility_rule(options):
    return ARDUINOQ_RULE if "extra_scripts" in options else MINIMAIN_RULE if "board_build.extra_flags" in options else ONEHORSE_RULE


ARDUINOQ_RULE = "arduinoq-rc4-zephyr-0.56-linker-paths"
_UNOQ_PLATFORM_SHA256 = "2ae4b31c96c795424f3b38579530b62aa0251004d1f2b28ed10f1ddcc7e7c8c0"
_UNOQ_BOARD_SHA256 = "8cbeb02c478524a13cb79267212ae4a768fc4afdf5ae9b7ad5e1084ed5943832"
_UNOQ_LINKER_SHA256 = "d5547a1d22fa78a5c84a44ec2d53e1dade6d29d37f56c999299b1de789437ee1"


def _arduinoq_options(info, core_dir):
    import sys
    if sys.platform != "linux":
        return {}
    try:
        core = Path(core_dir).resolve(strict=True)
        platforms, packages = core / "platforms", core / "packages"
        if platforms.resolve(strict=True).parent != core or packages.resolve(strict=True).parent != core:
            return {}
        manifest = Path(info.get("pio_manifest") or "")
        if (manifest.name != "uno_q.json" or manifest.parent.name != "boards"
                or manifest.parent.parent.parent.resolve(strict=True) != platforms.resolve(strict=True)):
            return {}
        board_raw, _ = _read_metadata(manifest, platforms.resolve())
        platform_raw, _ = _read_metadata(manifest.parent.parent / "platform.json", platforms.resolve())
        linker = manifest.parent.parent / "builder/artifacts/zephyr-llext.py"
        if linker.is_symlink() or not linker.resolve(strict=True).is_relative_to(platforms.resolve()) or linker.stat().st_size > 65536:
            return {}
        if (hashlib.sha256(board_raw).hexdigest() != _UNOQ_BOARD_SHA256
                or hashlib.sha256(platform_raw).hexdigest() != _UNOQ_PLATFORM_SHA256
                or hashlib.sha256(linker.read_bytes()).hexdigest() != _UNOQ_LINKER_SHA256):
            return {}
        _, framework = _read_metadata(packages / "framework-arduino-zephyr/package.json", packages.resolve(strict=True))
        if (framework.get("name"), framework.get("version")) != ("framework-arduino-zephyr", "0.56.0-leelabskku+unoq"):
            return {}
        hook = Path(__file__).resolve().parents[2] / "src/modules/arduinoq_link_paths.py"
        with hook.open("rb") as source:
            raw = source.read(65537)
        if hook.is_symlink() or len(raw) > 65536 or any(ord(char) < 32 for char in str(hook)):
            return {}
        return {"extra_scripts": "post:" + hook.as_posix(),
                "custom_mcu_arduinoq_hook_sha256": hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, TypeError, UnicodeError):
        return {}
