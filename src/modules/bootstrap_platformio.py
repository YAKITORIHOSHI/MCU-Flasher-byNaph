"""Bootstrap-only PlatformIO entry point with safe Windows archive paths."""
from contextlib import contextmanager
from copy import copy
from functools import wraps
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _python_script(args):
    if not isinstance(args, (list, tuple)) or len(args) < 2:
        return None
    return Path(os.fsdecode(args[1])) if isinstance(args[1], (str, bytes, os.PathLike)) else None


@contextmanager
def builder_processes():
    """Keep builder children in bootstrap and expose nested module diagnostics."""
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        raise RuntimeError("Builder preparation belongs to bootstrap")
    import subprocess
    import click
    from src.modules.windows_tool_paths import zephyr_cmake_environment, espidf_component_relpaths
    from src.modules.mbed_compat import mbed_compat
    original_popen, original_echo = subprocess.Popen, click.echo

    class BootstrapPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            if "env" in kwargs:
                kwargs["env"] = zephyr_cmake_environment(args, kwargs["env"])
            script = _python_script(args)
            if script and script.name == "scons.py" and script.parent.name.startswith("tool-scons"):
                args = [args[0], "-B", str(Path(__file__).absolute()), "--script", str(script), *args[2:]]
            elif (script and script.name == "install-deps.py" and script.parent.name == "platformio"
                  and script.parent.parent.name == "scripts"):
                # This upstream subprocess.call otherwise relies on inherited
                # Win32 handles rather than SCons' captured Python streams.
                kwargs.setdefault("stdout", sys.stdout)
                kwargs.setdefault("stderr", sys.stderr)
                print("Preparing Zephyr Git modules (nested installer output follows).", flush=True)
            super().__init__(args, *positional, **kwargs)

    @wraps(original_echo)
    def echo(message=None, *positional, **kwargs):
        # envdump is preparation-only. Its dictionary contains inherited
        # environment values, which must not enter live or persistent logs.
        if ("envdump" in sys.argv and isinstance(message, str) and message.lstrip().startswith("{")
                and ("'ENV':" in message or '"ENV":' in message)):
            message = "Builder environment prepared."
        return original_echo(message, *positional, **kwargs)

    subprocess.Popen, click.echo = BootstrapPopen, echo
    try:
        with espidf_component_relpaths(), mbed_compat():
            yield
    finally:
        subprocess.Popen, click.echo = original_popen, original_echo


def extended_windows_path(path):
    """Keep archive I/O usable after ZIP/TAR security checks resolve junctions."""
    path = os.path.abspath(os.fsdecode(path))
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


@contextmanager
def archive_paths():
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        raise RuntimeError("Package installation belongs to bootstrap, outside the workspace process")
    if sys.platform != "win32":
        yield
        return
    from platformio.package.unpack import FileUnpacker, ZIPArchiver, ExtractArchiveItemError
    from platformio.package.manager import _install as installer

    original = FileUnpacker.unpack
    metadata_methods = {name: ZIPArchiver.__dict__[name] for name in ("preserve_permissions", "preserve_mtime")}
    original_shutil, original_fs = installer.shutil, installer.fs

    def package_path(path):
        # Only extend package-manager storage and owned installation staging.
        # Resolve both sides before containment checks so a junction cannot
        # turn the extended I/O spelling into access outside this core.
        core = os.environ.get("PLATFORMIO_CORE_DIR")
        if not core:
            return False
        try:
            base = os.path.normcase(os.path.realpath(os.path.abspath(core)))
            target = os.path.normcase(os.path.realpath(os.path.abspath(os.fsdecode(path))))
            if os.path.commonpath((base, target)) != base:
                return False
            parts = os.path.relpath(target, base).split(os.sep)
            if len(parts) >= 2 and parts[0] in ("packages", "platforms", "lib"):
                return parts[1] not in ("", os.curdir, os.pardir)
            return (len(parts) >= 3 and parts[:2] == [".cache", "tmp"]
                    and parts[2].startswith("pkg-installing-"))
        except (OSError, TypeError, ValueError):
            return False

    @wraps(original_shutil.copytree)
    def copytree(source, destination, *args, **kwargs):
        if package_path(source) and package_path(destination):
            source, destination = extended_windows_path(source), extended_windows_path(destination)
        return original_shutil.copytree(source, destination, *args, **kwargs)

    @wraps(original_fs.rmtree)
    def rmtree(path, *args, **kwargs):
        if package_path(path):
            path = extended_windows_path(path)
        return original_fs.rmtree(path, *args, **kwargs)

    class InstallModuleProxy:
        def __init__(self, module, **overrides):
            self.module, self.overrides = module, overrides

        def __getattr__(self, name):
            return self.overrides[name] if name in self.overrides else getattr(self.module, name)

    def native_metadata(method):
        @wraps(method)
        def apply(item, destination):
            # ZIP names use '/', which extended Win32 I/O does not translate.
            native_item = copy(item)
            native_item.filename = os.path.normpath(item.filename)
            return method(native_item, extended_windows_path(destination))
        return staticmethod(apply)

    @wraps(original)
    def unpack(self, dest_dir=None, with_progress=True, check_unpacked=True, silent=False):
        destination = extended_windows_path(os.getcwd() if dest_dir is None else dest_dir)
        # Keep PlatformIO's traversal/link checks and extraction. Its final
        # verification needs native separators too, including for TAR members.
        result = original(self, destination, with_progress=with_progress,
                          check_unpacked=False, silent=silent)
        if check_unpacked:
            for item in self._archiver.get_items():
                filename = self._archiver.get_item_filename(item)
                target = os.path.normpath(os.path.join(destination, filename))
                try:
                    if not self._archiver.is_link(item) and not os.path.exists(target):
                        raise ExtractArchiveItemError(filename, destination)
                except NotImplementedError:
                    pass
        return result

    FileUnpacker.unpack = unpack
    # PlatformIO copies extracted packages before committing them. Extended
    # extraction can preserve legitimate upstream trailing-dot names which
    # ordinary Win32 copying and cleanup silently normalize away. Adapt this
    # installer module only, leaving global shutil and runtime untouched.
    installer.shutil = InstallModuleProxy(original_shutil, copytree=copytree)
    installer.fs = InstallModuleProxy(original_fs, rmtree=rmtree)
    try:
        for name in metadata_methods:
            setattr(ZIPArchiver, name, native_metadata(getattr(ZIPArchiver, name)))
        yield
    finally:
        FileUnpacker.unpack = original
        installer.shutil, installer.fs = original_shutil, original_fs
        for name, descriptor in metadata_methods.items():
            setattr(ZIPArchiver, name, descriptor)


def command():
    return [sys.executable, "-B", str(Path(__file__).absolute())]


if __name__ == "__main__":
    import runpy
    from src.modules.platformio_locks import package_locks
    with archive_paths(), builder_processes(), package_locks():
        if len(sys.argv) > 2 and sys.argv[1] == "--script":
            script = sys.argv[2]
            sys.argv = [script, *sys.argv[3:]]
            runpy.run_path(script, run_name="__main__")
        else:
            runpy.run_module("platformio", run_name="__main__")
