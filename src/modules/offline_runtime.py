"""Board/library preparation policy and bootstrap-only package installation."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_INSTALLER_MODULE = "platformio.package.manager._install"
_enabled = False
_FIREBASE_AUTH_HOSTS = frozenset({
    "identitytoolkit.googleapis.com",
    "securetoken.googleapis.com",
})


class OfflineDependencyError(RuntimeError):
    pass


def network_access_disabled():
    """Compatibility query: package Offline Mode never disables app networking."""
    return False


def bootstrap_instruction(detail="Missing offline dependency"):
    from src.modules.platform_runtime import repair_command
    entry = repair_command()
    return f"{detail}. Prepare it in bootstrap while online: {entry}. The main app does not download packages."


def offline_pio_command(python=None):
    return [str(python or sys.executable), "-B", str(ROOT / "src/modules/offline_platformio.py")]


def _firebase_host(host):
    host = str(host or "").strip("[]").lower().rstrip(".")
    return (host in _FIREBASE_AUTH_HOSTS or host.endswith(".firebasedatabase.app")
            or host.endswith(".firebaseio.com"))


@contextmanager
def firebase_network_access(hostname):
    """Validate a Firebase request destination, independently of package mode."""
    host = str(hostname or "").strip("[]").lower().rstrip(".")
    if not _firebase_host(host):
        raise OfflineDependencyError("Cloud requests are limited to Firebase HTTPS endpoints.")
    yield


def _audit(event, args):
    if event == "subprocess.Popen":
        command = args[1]
        from src.modules.windows_tool_paths import zephyr_cmake_environment
        environment = args[3]
        normalized = zephyr_cmake_environment(command, environment)
        if normalized is not environment:
            for name in ("ZEPHYR_BASE", "ZEPHYR_BOARD_ALIASES"):
                if name in normalized:
                    environment[name] = normalized[name]
        parts = [str(item).lower() for item in command] if isinstance(command, (list, tuple)) else str(command).lower().split()
        # User-owned terminal commands run in their PTY process, outside this
        # app-owned dependency boundary. Never rewrite/replay those commands.
        executables = {Path(item).name.removesuffix(".exe").removesuffix(".cmd") for item in parts}
        if "install" in parts and executables.intersection(("pip", "pip3", "npm", "winget")):
            raise OfflineDependencyError(bootstrap_instruction("Dependency installation is bootstrap-only"))


def activate(offline=True):
    """Keep installation bootstrap-only without blocking application networking.

    The saved mode controls prepared board/library inputs and pip's package
    lookup policy. Cloud, developer access and user connections are independent.
    """
    global _enabled
    if _enabled:
        return
    os.environ["MCU_FLASHER_WORKSPACE_RUNTIME"] = "1"
    os.environ["MCU_FLASHER_APP_ROOT"] = str(ROOT)
    if offline:
        os.environ["MCU_FLASHER_OFFLINE_RUNTIME"] = "1"
        os.environ["PIP_NO_INDEX"] = "1"
    else:
        os.environ.pop("MCU_FLASHER_OFFLINE_RUNTIME", None)
        os.environ.pop("PIP_NO_INDEX", None)
    os.environ["PLATFORMIO_NO_TELEMETRY"] = "1"
    os.environ["PLATFORMIO_DISABLE_UPGRADE_CHECK"] = "1"
    from src.modules.windows_tool_paths import install_espidf_component_relpaths
    from src.modules.mbed_compat import install_mbed_compat
    install_espidf_component_relpaths()
    install_mbed_compat()
    sys.addaudithook(_audit)
    _enabled = True
    # The site hook also runs in nested SCons/framework Python children. Their
    # package managers must retain installed-only behavior in online mode too.
    # Children such as esptool never import PlatformIO, so apply the guard when
    # its installer loads rather than importing all of it into every process.
    if _INSTALLER_MODULE in sys.modules:
        guard_platformio()
    elif not any(isinstance(finder, _InstallerGuardFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _InstallerGuardFinder())


class _GuardedInstallerLoader:
    """Delegate to the real loader, then guard the installer it just executed."""

    def __init__(self, loader):
        self._loader = loader

    def __getattr__(self, name):
        return getattr(self._loader, name)

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        guard_platformio()


class _InstallerGuardFinder:
    """Guard PlatformIO's installer however and whenever it is first imported."""

    def find_spec(self, name, path=None, target=None):
        if name != _INSTALLER_MODULE:
            return None
        from importlib.machinery import PathFinder
        spec = PathFinder.find_spec(name, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _GuardedInstallerLoader(spec.loader)
        return spec


def guard_platformio():
    """Allow installed/local packages, reject registry/VCS installs before I/O."""
    from platformio.package.manager._install import PackageManagerInstallMixin
    from platformio.package.exception import PackageException
    original = PackageManagerInstallMixin._install
    if getattr(original, "_mcu_offline", False) is True:
        return

    def installed_only(manager, spec, *args, **kwargs):
        spec = manager.ensure_spec(spec)
        if kwargs.get("force") or (len(args) > 1 and args[1]):
            raise PackageException(bootstrap_instruction("Forced package replacement is bootstrap-only"))
        package = manager.get_package(spec)
        uri = str(spec.uri or "")
        local = uri.startswith(("file://", "symlink://"))
        if not package and not local:
            raise PackageException(bootstrap_instruction(f"Offline package unavailable: {spec.humanize()}"))
        return original(manager, spec, *args, **kwargs)

    installed_only._mcu_offline = True
    PackageManagerInstallMixin._install = installed_only
