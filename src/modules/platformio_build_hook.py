"""SCons hook loaded by every application PlatformIO build (pre and post).

Pre: reuse identical objects across sketches and after Clean. SCons keys each
object on its sources, headers and full command line, so a hit is byte-safe.
Post: record the libraries this build actually used. Reuse checks then hash
those trees instead of every library the user has ever downloaded.

Neither step may fail a build: without them the build is only slower.
"""
import json
import os

Import("env")  # noqa: F821  (provided by SCons)

from SCons.Script import COMMAND_LINE_TARGETS  # noqa: E402

_RECORD = "mcu-used-libraries.json"


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


try:
    if env.get("MCU_FLASHER_HOOK_PRE_DONE"):  # noqa: F821
        _record_used_libraries()
        _record_navigation_context()
    else:
        env["MCU_FLASHER_HOOK_PRE_DONE"] = True  # noqa: F821
        _enable_object_cache()
except Exception:  # pylint: disable=broad-except
    pass
