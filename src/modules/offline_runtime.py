"""Workspace network policy and bootstrap-only package installation."""
from __future__ import annotations

import ipaddress
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_enabled = False
_network_blocked = False
_firebase_network = threading.local()
_FIREBASE_AUTH_HOSTS = frozenset({
    "identitytoolkit.googleapis.com",
    "securetoken.googleapis.com",
})


class OfflineDependencyError(RuntimeError):
    pass


def network_access_disabled():
    """Report the current process policy, including an inherited child guard.

    Saved preferences may already describe the next launch. An installed audit
    hook keeps its original mode until the workspace is restarted.
    """
    return _network_blocked if _enabled else os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") == "1"


def offline_network_instruction():
    return "Network access is disabled in Offline Mode. Turn it off in Settings and restart MCU Flasher."


def bootstrap_instruction(detail="Missing offline dependency"):
    entry = "bash direct/ubuntu/run.sh --repair" if sys.platform.startswith("linux") else "direct/windows/run.vbs --repair"
    return f"{detail}. Prepare it in bootstrap while online: {entry}. The main app does not download packages."


def offline_pio_command(python=None):
    return [str(python or sys.executable), "-B", str(ROOT / "src/modules/offline_platformio.py")]


def _local_address(address):
    if isinstance(address, (str, bytes)):
        return True  # Unix-domain sockets / Windows named pipes
    if not isinstance(address, tuple) or not address:
        return False
    host = str(address[0]).strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _firebase_host(host):
    host = str(host or "").strip("[]").lower().rstrip(".")
    return (host in _FIREBASE_AUTH_HOSTS or host.endswith(".firebasedatabase.app")
            or host.endswith(".firebaseio.com"))


@contextmanager
def firebase_network_access(hostname):
    """Allow one validated Firebase HTTPS host through an Offline Mode audit hook."""
    host = str(hostname or "").strip("[]").lower().rstrip(".")
    if not _firebase_host(host):
        raise OfflineDependencyError("Cloud requests are limited to Firebase HTTPS endpoints.")
    previous = getattr(_firebase_network, "host", None)
    _firebase_network.host = host
    try:
        yield
    finally:
        if previous is None:
            try:
                del _firebase_network.host
            except AttributeError:
                pass
        else:
            _firebase_network.host = previous


def _firebase_connect_allowed(address, expected_host):
    if not expected_host or not isinstance(address, tuple) or len(address) < 2:
        return False
    try:
        if int(address[1]) != 443:
            return False
    except (TypeError, ValueError):
        return False
    target = str(address[0]).strip("[]").lower().rstrip(".")
    try:
        parsed = ipaddress.ip_address(target)
    except ValueError:
        return target == expected_host
    return parsed.is_global


def _audit(event, args):
    if _network_blocked and event == "socket.getaddrinfo":
        host = str(args[0]).strip("[]").lower().rstrip(".")
        allowed_host = getattr(_firebase_network, "host", None)
        if host not in ("localhost", "127.0.0.1", "::1", "none") and host != allowed_host:
            raise OfflineDependencyError(offline_network_instruction())
    if _network_blocked and event in ("socket.connect", "socket.sendto"):
        address = args[-1]
        if _local_address(address):
            pass
        elif (event == "socket.connect"
              and _firebase_connect_allowed(address, getattr(_firebase_network, "host", None))):
            pass
        else:
            raise OfflineDependencyError(offline_network_instruction())
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
    """Keep installation bootstrap-only; networking follows the saved mode.

    Audit hooks cannot be removed. A mode change therefore requires a new
    process, rather than trying to turn an active offline hook off in place.
    """
    global _enabled, _network_blocked
    if _enabled:
        return
    _network_blocked = bool(offline)
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
    guard_platformio()


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
