"""SCons hook loaded by every application PlatformIO build (pre and post).

Pre: reuse identical objects across sketches and after Clean. SCons keys each
object on its sources, headers and full command line, so a hit is byte-safe.
Post: record the libraries this build actually used. Reuse checks then hash
those trees instead of every library the user has ever downloaded.

Upload with "nobuild" never runs the framework script, and that script is what
lists the extra flash images (an ESP32's bootloader, partition table and
boot_app0). Without them only the application was written, so a board kept the
partition table and OTA selection of whatever it ran before. The post pass of a
build records the list; the pre pass of a nobuild upload restores it before the
platform script assembles the uploader command.

Neither step may fail a build: without them the build is only slower.
"""
import json
import os

Import("env")  # noqa: F821  (provided by SCons)

from SCons.Script import COMMAND_LINE_TARGETS  # noqa: E402

_RECORD = "mcu-used-libraries.json"
_FLASH_IMAGES = "mcu-flash-images.json"
# Set by the application only for serial uploads whose restart it performs
# itself through the Serial Monitor, so the boot output is captured.
_AFTER_RESET = "MCU_FLASHER_ESPTOOL_AFTER"


def _write_if_changed(path, content):
    try:
        with open(path, encoding="utf-8") as previous:
            if previous.read() == content:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        output.write(content)
    os.replace(temporary, path)


def _record_flash_images():
    """Remember the exact extra images this build's framework script declared."""
    if "nobuild" in COMMAND_LINE_TARGETS:
        return
    images = []
    for item in env.get("FLASH_EXTRA_IMAGES", []) or []:  # noqa: F821
        try:
            offset, path = item[0], item[1]
        except (IndexError, TypeError):
            return  # An unfamiliar shape: leave uploads to PlatformIO's own rules.
        images.append([str(env.subst(str(offset))), os.path.abspath(env.subst(str(path)))])  # noqa: F821
    path = os.path.join(env.subst("$BUILD_DIR"), _FLASH_IMAGES)  # noqa: F821
    _write_if_changed(path, json.dumps({"schema": 1, "images": images}, indent=0))


def _restore_flash_images():
    """Give a nobuild upload the image list its successful build declared."""
    if "nobuild" not in COMMAND_LINE_TARGETS or env.get("FLASH_EXTRA_IMAGES"):  # noqa: F821
        return
    path = os.path.join(env.subst("$BUILD_DIR"), _FLASH_IMAGES)  # noqa: F821
    try:
        with open(path, encoding="utf-8") as stream:
            record = json.loads(stream.read(65536))
    except (OSError, ValueError):
        return
    images = record.get("images") if isinstance(record, dict) and record.get("schema") == 1 else None
    if not isinstance(images, list) or len(images) > 16:
        return
    restored = []
    for item in images:
        if (not isinstance(item, list) or len(item) != 2
                or not all(isinstance(value, str) and value for value in item)):
            return
        offset, image = item
        if not os.path.isfile(image):
            # A missing image means this build is not what the record described.
            # Fail the upload visibly rather than writing only part of the set.
            raise FileNotFoundError(f"Recorded flash image is missing: {image}. Compile again.")
        restored.append((offset, image))
    if restored:
        env.Replace(FLASH_EXTRA_IMAGES=restored)  # noqa: F821


def _apply_after_reset():
    """Leave the chip in the bootloader when the application restarts it itself."""
    wanted = os.environ.get(_AFTER_RESET, "")
    if wanted != "no-reset" or "upload" not in COMMAND_LINE_TARGETS:
        return
    flags = list(env.get("UPLOADERFLAGS", []) or [])  # noqa: F821
    for index, flag in enumerate(flags[:-1]):
        if str(flag) == "--after":
            current = str(flags[index + 1])
            # esptool 4 spells it no_reset, esptool 5 no-reset; follow the platform.
            flags[index + 1] = "no_reset" if "_" in current else "no-reset"
            env.Replace(UPLOADERFLAGS=flags)  # noqa: F821
            return


def _record_navigation_context():
    includes = [env.Dir(env.subst(str(path))).get_abspath() for path in env.get("CPPPATH", [])]  # noqa: F821
    for builder in env.GetLibBuilders():  # noqa: F821
        if builder.is_dependent:
            includes.extend(builder.env.Dir(builder.env.subst(str(path))).get_abspath()
                            for path in builder.env.get("CPPPATH", []))
    defines = env.get("CPPDEFINES", [])  # noqa: F821
    flags = []
    for define in defines:
        if isinstance(define, (tuple, list)):
            flags.append("-D" + str(define[0]) + ("=" + str(define[1]) if len(define) > 1 else ""))
        else:
            flags.append("-D" + str(define))
    flags += [str(flag) for key in ("CCFLAGS", "CXXFLAGS") for flag in env.get(key, [])]  # noqa: F821
    value = {"schema": 1, "includes": list(dict.fromkeys(os.path.abspath(path) for path in includes)),
             "flags": flags, "compiler": env.subst("$CXX")}  # noqa: F821
    path = os.path.join(env.subst("$BUILD_DIR"), "mcu-navigation.json")  # noqa: F821
    content = json.dumps(value, sort_keys=True, indent=0)
    try:
        with open(path, encoding="utf-8") as stream:
            if stream.read() == content:
                return
    except OSError:
        pass
    temporary = path + f".{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.replace(temporary, path)


def _enable_object_cache():
    directory = os.environ.get("MCU_FLASHER_OBJECT_CACHE")
    if directory and not env.get("MCU_FLASHER_OBJECT_CACHE_READY"):  # noqa: F821
        os.makedirs(directory, exist_ok=True)
        env.CacheDir(directory)  # noqa: F821
        env["MCU_FLASHER_OBJECT_CACHE_READY"] = True  # noqa: F821


def _record_used_libraries():
    # "nobuild" skips the library finder, so it has nothing new to report.
    if "nobuild" in COMMAND_LINE_TARGETS:
        return
    used = sorted({os.path.realpath(builder.path) for builder in env.GetLibBuilders()  # noqa: F821
                   if builder.is_dependent})
    path = os.path.join(env.subst("$BUILD_DIR"), _RECORD)  # noqa: F821
    content = json.dumps({"schema": 1, "libraries": used}, indent=0)
    try:
        with open(path, encoding="utf-8") as previous:
            if previous.read() == content:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        output.write(content)
    os.replace(temporary, path)


if env.get("MCU_FLASHER_HOOK_PRE_DONE"):  # noqa: F821
    for _step in (_record_used_libraries, _record_navigation_context, _record_flash_images):
        try:
            _step()
        except Exception:  # pylint: disable=broad-except
            pass
    # An upload that cannot honour the requested restart ownership must not run
    # with a different one, so this step is not silenced.
    _apply_after_reset()
else:
    env["MCU_FLASHER_HOOK_PRE_DONE"] = True  # noqa: F821
    try:
        _enable_object_cache()
    except Exception:  # pylint: disable=broad-except
        pass
    # A recorded image that disappeared fails the upload instead of writing a
    # partial set; every other condition leaves PlatformIO's behavior unchanged.
    _restore_flash_images()
