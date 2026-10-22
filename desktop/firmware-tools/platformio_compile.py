"""Development-only, settings-free PlatformIO compile entry for an admitted AVR fixture.

Invoke the native-admitted private interpreter with ``-I -S -B`` and all required
literal arguments. No site module, .pth hook, application adapter or saved
setting is loaded here. Native Rust owns target selection, package/runtime byte
admission, worker resources, immutable lifetime and finite process teardown.

This entry is not connected to a native build caller. Its policy guards are not
an OS sandbox; admitted runtime lifetime, native child networking and Windows
build lock behavior require separate qualification before real compile dispatch.
"""

import sys

# Native log pipes use one encoding even before argument/policy refusals.
for stream in (sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

# Run before any third-party import or argument-dependent path is admitted.
if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
    print("Tool entry requires the admitted private Python with -I -S -B.", file=sys.stderr)
    raise SystemExit(2)

import argparse
import base64
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
import inspect
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import tempfile
import threading


_POPEN_CONTEXT = threading.local()


class ToolBoundaryError(RuntimeError):
    """A supplied native policy or attempted tool operation was refused."""


def _directory(raw):
    if any(part.rstrip(" .").casefold() in {
            ".mcu_flasher_build_cache", ".pio", ".vscode", ".clangd", ".opencode"}
           for part in re.split(r"[/\\]", raw)):
        raise ToolBoundaryError("Protected application paths cannot be used by firmware tools.")
    path = Path(raw)
    if not path.is_absolute() or any(part in (".", "..") for part in re.split(r"[/\\]", raw)):
        raise ToolBoundaryError("Prepared roots must be absolute and have no dot aliases.")
    for part in reversed((path, *path.parents)):
        metadata = part.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & 0x400):
            raise ToolBoundaryError("Prepared roots cannot contain links or reparse aliases.")
    return path.resolve(strict=True)


def _output(parent, name):
    path = parent / name
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _directory(str(path))
    return str(path)


def _config(project, target):
    path = project / "platformio.ini"
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or getattr(metadata, "st_file_attributes", 0) & 0x400
            or metadata.st_size > 1024):
        raise ToolBoundaryError("Compile config must be one bounded regular file.")
    expected = (
        "[platformio]\nsrc_dir = sources\ninclude_dir = sources\n\n"
        "[env:native_compile]\nplatform = %s\nboard = %s\nframework = %s\n" % target
    ).encode("ascii")
    with path.open("rb") as stream:
        actual = stream.read(1025)
    if actual != expected:
        raise ToolBoundaryError("Compile config differs from the supplied native AVR policy.")
    _directory(str(project / "sources"))


def _environment(output, core, packages, platforms):
    # Only OS loader essentials survive. Native admission still owns their
    # provenance; arbitrary inherited PlatformIO/Python/app policy is discarded.
    environment = {name: value for name, value in os.environ.items()
                   if name.upper() in ("SYSTEMROOT", "WINDIR")}
    environment.update({
        "PATH": os.pathsep.join((str(Path(sys.executable).absolute().parent),
                                 str(packages / "toolchain-atmelavr" / "bin"))),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1", "PYTHONSAFEPATH": "1", "PYTHONUTF8": "1",
        "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PLATFORMIO_CORE_DIR": str(core), "PLATFORMIO_PACKAGES_DIR": str(packages),
        "PLATFORMIO_PLATFORMS_DIR": str(platforms),
        "PLATFORMIO_CACHE_DIR": _output(output, "cache-output"),
        "PLATFORMIO_BUILD_CACHE_DIR": _output(output, "build-cache-output"),
        "PLATFORMIO_GLOBALLIB_DIR": _output(output, "lib-output"),
        "PLATFORMIO_BUILD_DIR": _output(output, "build-output"),
        "PLATFORMIO_LIBDEPS_DIR": _output(output, "libdeps-output"),
        "PLATFORMIO_NO_TELEMETRY": "1", "PLATFORMIO_DISABLE_TELEMETRY": "1",
        "PLATFORMIO_DISABLE_UPGRADE_CHECK": "1", "PLATFORMIO_DISABLE_PROMPTS": "1",
        "PLATFORMIO_UNBUFFERED": "1",
    })
    temporary = _output(output, "tmp-output")
    home = _output(output, "home-output")
    environment.update(TMPDIR=temporary, TEMP=temporary, TMP=temporary,
                       HOME=home, USERPROFILE=home,
                       XDG_CONFIG_HOME=home, XDG_CACHE_HOME=home, XDG_DATA_HOME=home)
    os.environ.clear()
    os.environ.update(environment)
    # An unwritable or later-replaced temp directory must fail there rather
    # than triggering tempfile's search through system/user fallback paths.
    tempfile.tempdir = temporary


def _subprocess_vector(command):
    if (not isinstance(command, (list, tuple)) or not command
            or len(command) > 128 or any(not isinstance(word, str) or "\0" in word for word in command)
            or sum(len(word) for word in command) > 32768):
        raise ToolBoundaryError("Shell command strings are outside the compile boundary.")
    words = [word.lower() for word in command]
    executable = Path(words[0]).name.removesuffix(".exe").removesuffix(".cmd")
    if executable in {"pip", "pip3", "npm", "npx", "winget", "git", "curl", "wget"}:
        raise ToolBoundaryError("Dependency preparation is outside the compile boundary.")
    if "-m" in words and any(word in {"pip", "ensurepip", "venv"} for word in words):
        raise ToolBoundaryError("Python dependency preparation is unavailable.")
    if (command[0] == sys.executable or re.fullmatch(r"(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?", executable)):
        if tuple(command) != getattr(_POPEN_CONTEXT, "python_command", None):
            raise ToolBoundaryError("Only the exact isolated SCons Python handoff is available.")
    return list(command)


def _audit(event, args):
    if event == "import" and args[0].split(".")[0] in {
            "main", "src", "PySide6", "PyQt6", "tkinter", "dedicated_AI"}:
        raise ToolBoundaryError("Application and GUI imports are outside the firmware-tool boundary.")
    if event in ("socket.__new__", "urllib.Request"):
        raise ToolBoundaryError("Network access is unavailable in this compile entry.")
    if event == "subprocess.Popen":
        command = args[1]
        if isinstance(command, str):
            # CPython serializes a Windows vector before this audit event. The
            # guarded Popen constructor below validates the original vector,
            # shell/executable options and retains its exact serialization only
            # for this thread and constructor call. Strings supplied by callers
            # and different serialized commands still refuse.
            admitted = getattr(_POPEN_CONTEXT, "command", None)
            if (sys.platform != "win32" or admitted is None
                    or command != subprocess.list2cmdline(admitted)):
                raise ToolBoundaryError("Shell command strings are outside the compile boundary.")
            command = admitted
        _subprocess_vector(command)


def _guard_subprocess():
    original = subprocess.Popen
    signature = inspect.signature(original.__init__)

    class GuardedPopen(original):
        def __init__(self, args, *positional, **keywords):
            options = signature.bind(self, args, *positional, **keywords).arguments
            command = _subprocess_vector(args)
            if options.get("shell", False) or options.get("executable") is not None:
                raise ToolBoundaryError("Shell and executable overrides are outside the compile boundary.")
            previous = getattr(_POPEN_CONTEXT, "command", None)
            _POPEN_CONTEXT.command = tuple(command)
            try:
                super().__init__(command, *positional, **keywords)
            finally:
                _POPEN_CONTEXT.command = previous

    subprocess.Popen = GuardedPopen


def _encoded(value):
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _scons_arguments(policy, roots, verbose, isatty):
    project, _core, packages, platforms, site, _output = roots
    return [str(packages / "tool-scons/scons.py"), "-Q", "--warn=no-no-parallel-support",
            "--jobs", policy.jobs, "--sconstruct", str(site / "platformio/builder/main.py"),
            "PIOVERBOSE=" + verbose, "ISATTY=" + isatty,
            "PIOENV=" + _encoded("native_compile"),
            "PROJECT_CONFIG=" + _encoded(str(project / "platformio.ini")),
            "PROGRAM_ARGS=W10=",
            "BUILD_SCRIPT=" + _encoded(str(platforms / "atmelavr/builder/main.py"))]


def _scons_handoff(policy, roots):
    from platformio import proc
    original = proc.exec_command
    child = Path(__file__).with_name("scons_compile.py")

    def execute(*args, **keywords):
        if len(args) != 1:
            raise ToolBoundaryError("Firmware tool subprocess requires one literal argument vector.")
        command = args[0]
        if (not isinstance(command, (list, tuple)) or not command
                or any(not isinstance(word, str) or "\0" in word for word in command)):
            raise ToolBoundaryError("Shell command strings are outside the compile boundary.")
        if command[0] != sys.executable:
            return original(*args, **keywords)
        if len(command) != 14:
            raise ToolBoundaryError("Unexpected SCons command shape or target.")
        # Variables may retain upstream insertion order, but their exact names,
        # canonical values and count are fixed. No arbitrary encoded hooks,
        # target, upload, clean, program/test options or duplicate variables.
        options = command[8:]
        if len(set(options)) != len(options):
            raise ToolBoundaryError("Duplicate SCons variable.")
        verbose = next((value[-1:] for value in options if value in ("PIOVERBOSE=0", "PIOVERBOSE=1")), None)
        isatty = next((value[-1:] for value in options if value in ("ISATTY=0", "ISATTY=1")), None)
        if verbose is None or isatty is None:
            raise ToolBoundaryError("Unexpected SCons display options.")
        expected = [sys.executable, *_scons_arguments(policy, roots, verbose, isatty)]
        if list(command[:8]) != expected[:8] or set(options) != set(expected[8:]):
            raise ToolBoundaryError("SCons arguments differ from the exact native compile policy.")
        _regular(child)
        _directory(str(roots[2] / "tool-scons/scons-local-4.11.1"))
        _directory(str(roots[4] / "platformio/builder"))
        forwarded = []
        for name in ("project-root", "core-root", "packages-root", "platforms-root",
                     "site-packages-root", "output-root", "jobs", "platform", "board", "framework"):
            forwarded.extend(("--" + name, getattr(policy, name.replace("-", "_"))))
        forwarded.extend(("--scons-root", str(roots[2] / "tool-scons"),
                          "--builder-root", str(roots[4] / "platformio/builder"),
                          "--verbose", verbose, "--isatty", isatty))
        command = [sys.executable, "-I", "-S", "-B", str(child), *forwarded]
        previous = getattr(_POPEN_CONTEXT, "python_command", None)
        _POPEN_CONTEXT.python_command = tuple(command)
        try:
            return original(command, **keywords)
        finally:
            _POPEN_CONTEXT.python_command = previous

    proc.exec_command = execute


def _regular(path):
    _directory(str(path.parent))
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or getattr(metadata, "st_file_attributes", 0) & 0x400):
        raise ToolBoundaryError("Firmware-tool code must be one regular non-aliased file.")


def _installed_only():
    # Exact private PlatformIO interfaces are part of future runtime admission.
    from platformio.package.exception import PackageException
    from platformio.package.manager._install import PackageManagerInstallMixin
    from platformio.package.manager._registry import PackageManagerRegistryMixin
    from platformio.package.manager._symlink import PackageManagerSymlinkMixin
    from platformio.package.manager._uninstall import PackageManagerUninstallMixin
    from platformio.package.manager._update import PackageManagerUpdateMixin
    from platformio.package.manager.platform import PlatformPackageManager

    def existing(manager, spec, skip_dependencies=False, force=False, compatibility=None):
        if force:
            raise PackageException("Forced package replacement is unavailable.")
        spec = manager.ensure_spec(spec)
        if getattr(spec, "external", False) or getattr(spec, "uri", None):
            raise PackageException("External, local and symlink package sources are unavailable.")
        package = manager.get_package(spec)
        if not package:
            raise PackageException("Required package is not already prepared.")
        # Never call upstream install/lock/cleanup/dependency preparation. Rust
        # must separately certify the complete prepared dependency closure.
        return package

    def refuse(*args, **kwargs):
        raise PackageException("Package installation and updates are unavailable.")

    def existing_platform(manager, spec, skip_dependencies=False, force=False,
                          project_env=None, project_targets=None):
        # The upstream platform override can prepare dependencies or uninstall
        # an incompatible platform even after super().install finds a package.
        return existing(manager, spec, skip_dependencies=skip_dependencies, force=force)

    PackageManagerInstallMixin.install = existing
    PackageManagerInstallMixin._install = existing
    for name in ("unpack", "install_from_uri", "_install_tmp_pkg", "install_dependencies", "install_dependency"):
        setattr(PackageManagerInstallMixin, name, refuse)
    PackageManagerRegistryMixin.install_from_registry = refuse
    PackageManagerSymlinkMixin.install_symlink = refuse
    PackageManagerSymlinkMixin.uninstall_symlink = refuse
    PackageManagerUninstallMixin.uninstall = refuse
    PackageManagerUninstallMixin.uninstall_dependencies = refuse
    PackageManagerUpdateMixin.update = refuse
    PackageManagerUpdateMixin._update = refuse
    PlatformPackageManager.install = existing_platform
    PlatformPackageManager.uninstall = refuse


def _policy(arguments, child=False):
    names = ("project-root", "core-root", "packages-root", "platforms-root",
             "site-packages-root", "output-root", "jobs", "platform", "board", "framework")
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    additional = ("scons-root", "builder-root", "verbose", "isatty") if child else ()
    fixed_names = names + additional
    for name in fixed_names:
        parser.add_argument("--" + name, required=True)
    if len(arguments) != 2 * len(fixed_names) or set(arguments[::2]) != {"--" + name for name in fixed_names}:
        parser.error("Supply each fixed tool argument once as a separate name/value pair.")
    policy = parser.parse_args(arguments)
    try:
        if policy.jobs not in {str(value) for value in range(1, 7)}:
            raise ToolBoundaryError("Native compiler jobs must be one literal integer from 1 to 6.")
        target = (policy.platform, policy.board, policy.framework)
        if target != ("platformio/atmelavr@5.3.0", "uno", "arduino"):
            raise ToolBoundaryError("Only the explicitly supplied pinned AVR sample is admitted.")
        project, core, packages, platforms, site, output = (
            _directory(getattr(policy, name.replace("-", "_"))) for name in names[:6])
        if core != output / "core":
            raise ToolBoundaryError("Writable core must be the precreated output-root/core child.")
        independent = (project, packages, platforms, site, output)
        if any(left == right or left in right.parents or right in left.parents
               for index, left in enumerate(independent) for right in independent[index + 1:]):
            raise ToolBoundaryError("Project, prepared package/platform/site and output roots must be independent.")
        _config(project, target)
        if child:
            if policy.verbose not in ("0", "1") or policy.isatty not in ("0", "1"):
                raise ToolBoundaryError("SCons display flags must be literal zero or one.")
            if (_directory(policy.scons_root) != packages / "tool-scons"
                    or _directory(policy.builder_root) != site / "platformio/builder"):
                raise ToolBoundaryError("SCons and builder roots differ from the admitted prepared roots.")
            _directory(str(packages / "tool-scons/scons-local-4.11.1"))
            for path in (site / "platformio/builder/main.py", platforms / "atmelavr/builder/main.py"):
                _regular(path)
        _environment(output, core, packages, platforms)
    except (ToolBoundaryError, OSError, ValueError) as error:
        parser.error(str(error))

    return policy, (project, core, packages, platforms, site, output)


def _start(roots):
    # -I -S leaves only interpreter-owned standard paths. Appending, rather
    # than site.addsitedir(), deliberately does not execute admitted .pth files.
    sys.path.append(str(roots[4]))
    sys.addaudithook(_audit)
    _guard_subprocess()
    _installed_only()


def main(argv=None):
    policy, roots = _policy(list(sys.argv[1:] if argv is None else argv))
    _start(roots)
    _scons_handoff(policy, roots)
    project = roots[0]
    sys.argv = ["platformio", "run", "--project-dir", str(project), "-j", policy.jobs]
    runpy.run_module("platformio", run_name="__main__")
    return 0


def scons_main(argv):
    policy, roots = _policy(list(argv), child=True)
    os.chdir(roots[0])
    _start(roots)
    # Do not run upstream scons.py: its startup probes/prepends other package
    # locations. Only this exact prepared engine root may supply SCons.
    sys.path.append(str(roots[2] / "tool-scons/scons-local-4.11.1"))
    sys.argv = _scons_arguments(policy, roots, policy.verbose, policy.isatty)
    engine = roots[2] / "tool-scons/scons-local-4.11.1"
    spec = PathFinder.find_spec("SCons", [str(engine)])
    expected = engine / "SCons/__init__.py"
    if (spec is None or spec.origin != str(expected) or spec.loader is None
            or list(spec.submodule_search_locations or ()) != [str(expected.parent)]
            or any(name == "SCons" or name.startswith("SCons.") for name in sys.modules)):
        raise ToolBoundaryError("SCons must originate in the exact admitted engine package.")
    _regular(expected)
    package = module_from_spec(spec)
    sys.modules["SCons"] = package
    spec.loader.exec_module(package)
    from SCons import Script
    Script.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
