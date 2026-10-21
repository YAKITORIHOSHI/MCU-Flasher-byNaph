"""Source-isolated fake-PlatformIO checks; never dispatch a compiler or installer."""

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "desktop/firmware-tools/platformio_compile.py"
OUTPUT = ROOT / "temp/audit/native-compile-inputs"
CONFIG = (b"[platformio]\nsrc_dir = sources\ninclude_dir = sources\n\n"
          b"[env:native_compile]\nplatform = platformio/atmelavr@5.3.0\n"
          b"board = uno\nframework = arduino\n")
CHECKS = []

FAKE_INSTALL = '''
class PackageManagerInstallMixin:
    def install(self, *args, **kwargs):
        raise AssertionError("Upstream install was invoked")
    _install = install
    install_from_uri = install
    unpack = install
    lock = install
    cleanup_expired_downloads = install
'''
FAKE_UPDATE = '''
class PackageManagerUpdateMixin:
    def update(self, *args, **kwargs):
        raise AssertionError("Upstream update was invoked")
    _update = update
'''
FAKE_BASE = '''
from platformio.package.manager._install import PackageManagerInstallMixin
from platformio.package.manager._registry import PackageManagerRegistryMixin
from platformio.package.manager._symlink import PackageManagerSymlinkMixin
from platformio.package.manager._uninstall import PackageManagerUninstallMixin
from platformio.package.manager._update import PackageManagerUpdateMixin
class PackageManagerDownloadMixin:
    pass
class PackageManagerLegacyMixin:
    pass
class BasePackageManager(PackageManagerDownloadMixin, PackageManagerRegistryMixin,
                         PackageManagerSymlinkMixin, PackageManagerInstallMixin,
                         PackageManagerUninstallMixin, PackageManagerUpdateMixin,
                         PackageManagerLegacyMixin):
    prepared = object()
    def ensure_spec(self, spec):
        return spec
    def get_package(self, spec):
        return self.prepared if spec.present else None
'''
FAKE_PLATFORM = '''
from platformio.package.manager.base import BasePackageManager
class PlatformPackageManager(BasePackageManager):
    def install(self, spec, skip_dependencies=False, force=False,
                project_env=None, project_targets=None):
        package = super().install(spec, force=force, skip_dependencies=True)
        raise AssertionError("Platform-specific preparation/cleanup path was reached")
    def uninstall(self, *args, **kwargs):
        raise AssertionError("Platform uninstall was invoked")
'''
FAKE_MAIN = '''
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from platformio.package.exception import PackageException
from platformio.package.manager.base import BasePackageManager
from platformio.package.manager.platform import PlatformPackageManager

class Manager(BasePackageManager):
    pass

manager = Manager()
spec = SimpleNamespace(present=True, uri=None, external=False)
with tempfile.NamedTemporaryFile() as temporary:
    assert Path(temporary.name).parent == Path(os.environ["TMPDIR"]), "Temporary files escaped the owned output directory"
attempts = []
original_open = tempfile._os.open
original_access = tempfile._os.access
def denied_output(path, *args, **kwargs):
    attempts.append(path)
    assert Path(path).parent == Path(os.environ["TMPDIR"])
    raise PermissionError("Owned fixture denies output creation")
tempfile._os.open = denied_output
# Windows tempfile retries PermissionError when access() still says writable;
# model an actual denied directory so the fixture observes one failed attempt.
tempfile._os.access = lambda *args, **kwargs: False
try:
    try:
        tempfile.NamedTemporaryFile()
    except PermissionError:
        pass
    else:
        raise AssertionError("Output denial did not remain a failure")
finally:
    tempfile._os.open = original_open
    tempfile._os.access = original_access
assert len(attempts) == 1, "Temporary-file failure tried a fallback path"
assert manager.install(spec) is manager.prepared
assert manager._install(spec, compatibility=object()) is manager.prepared
refusals = []
def refused(label, call):
    try:
        call()
    except PackageException:
        refusals.append(label)
    else:
        raise AssertionError("Operation was not refused: " + label)

refused("missing", lambda: manager.install(SimpleNamespace(present=False, uri=None, external=False)))
refused("force-keyword", lambda: manager.install(spec, force=True))
refused("force-positional", lambda: manager._install(spec, False, True))
for uri in ("file:///owned", "symlink:///owned", "git+https://invalid/fixture", "https://invalid/fixture"):
    refused(uri, lambda uri=uri: manager.install(SimpleNamespace(present=True, uri=uri, external=True)))
for name in ("update", "_update", "unpack", "install_from_uri", "install_from_registry", "install_symlink",
             "_install_tmp_pkg", "install_dependencies", "install_dependency", "uninstall",
             "uninstall_dependencies", "uninstall_symlink"):
    refused(name, lambda name=name: getattr(manager, name)(spec))
platform_manager = PlatformPackageManager()
assert platform_manager.install(spec, project_env="native_compile", project_targets=[]) is platform_manager.prepared
refused("platform-missing", lambda: platform_manager.install(SimpleNamespace(present=False, uri=None, external=False)))
refused("platform-force", lambda: platform_manager.install(spec, False, True))
refused("platform-external", lambda: platform_manager.install(SimpleNamespace(present=True, uri="file:///owned", external=True)))
refused("platform-uninstall", lambda: platform_manager.uninstall(spec))

try:
    socket.socket()
except RuntimeError as error:
    assert "Network" in str(error)
else:
    raise AssertionError("Python socket creation was not refused")
try:
    __import__("main")
except RuntimeError as error:
    assert "Application" in str(error)
else:
    raise AssertionError("Application import was not refused")
for command in (["pip", "install", "missing"], [sys.executable, "-m", "pip", "install", "missing"],
                ["curl", "https://invalid/fixture"], "compiler --serialized-command"):
    try:
        sys.audit("subprocess.Popen", None, command, None, {})
    except RuntimeError:
        pass
    else:
        raise AssertionError("Preparation/shell audit command was not refused")
assert not any(name == "main" or name.startswith("main.") or name == "src"
               or name.startswith("src.") for name in sys.modules)
assert "site" not in sys.modules and "sitecustomize" not in sys.modules
print(json.dumps(dict(argv=sys.argv, environment=dict(os.environ), path=sys.path,
                     encoding=[sys.stdout.encoding, sys.stderr.encoding],
                     flags=[sys.flags.isolated, sys.flags.no_site, sys.flags.dont_write_bytecode],
                     package_refusals=refusals)))
'''


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def inventory(root):
    return {path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
            for path in root.rglob("*")}


def run(command, environment, cwd):
    child = subprocess.run(command, cwd=cwd, env=environment, capture_output=True,
                           text=True, encoding="utf-8", errors="strict", timeout=10)
    assert len(child.stdout) + len(child.stderr) <= 65536, "Fixture output exceeded its bound"
    return child


def verify():
    source = ENTRY.read_bytes()
    tree = ast.parse(source, filename=str(ENTRY))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    for node in imports:
        modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
        assert all(module and module.split(".")[0] in {
            "sys", "argparse", "os", "pathlib", "re", "runpy", "stat", "tempfile", "platformio"}
                   for module in modules), modules
    assert isinstance(tree.body[1], ast.Import) and tree.body[1].names[0].name == "sys"
    assert isinstance(tree.body[2], ast.For), "Set log encoding before startup error messages"
    assert isinstance(tree.body[3], ast.If), "Startup flags must be checked before other imports"
    CHECKS.append("static-settings-free-import-closure-and-first-startup-check")
    directory_function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_directory")
    def refuse_path_access(raw):
        raise AssertionError("Protected path was accessed before lexical refusal")
    directory_namespace = dict(Path=refuse_path_access, re=re, ToolBoundaryError=RuntimeError)
    exec(compile(ast.Module(body=[directory_function], type_ignores=[]), str(ENTRY), "exec"), directory_namespace)
    for name in (".mcu_flasher_build_cache", ".PIO", ".VsCode", ".clangd", ".OPENCODE"):
        try:
            directory_namespace["_directory"]("/missing/" + name + "/child")
        except RuntimeError as error:
            assert "Protected" in str(error)
        else:
            raise AssertionError("Protected path was admitted")
    CHECKS.append("protected-components-refused-before-any-path-access")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tool-entry café 日本語 ", dir=OUTPUT) as folder:
        owned = Path(folder).resolve()
        entry = owned / "entry.py"
        entry.write_bytes(source)
        project, prepared, output = (owned / name for name in ("project", "prepared", "output"))
        core, packages, platforms, site = (output / "core", prepared / "core/packages",
                                          prepared / "core/platforms", prepared / "site-packages")
        for path in (project / "sources", core, packages, platforms, site):
            path.mkdir(parents=True, exist_ok=True)
        (project / "platformio.ini").write_bytes(CONFIG)
        (project / "sources/main.cpp").write_bytes(b"void setup(){}\nvoid loop(){}\n")
        imported, pth_marker = owned / "platformio-imported", owned / "pth-executed"
        write(site / "platformio/__init__.py",
              "from pathlib import Path\nPath(" + repr(str(imported)) + ").write_text('fixture')\n")
        write(site / "platformio/__main__.py", FAKE_MAIN)
        write(site / "platformio/package/__init__.py", "")
        write(site / "platformio/package/exception.py", "class PackageException(Exception):\n    pass\n")
        write(site / "platformio/package/manager/__init__.py", "")
        write(site / "platformio/package/manager/_install.py", FAKE_INSTALL)
        write(site / "platformio/package/manager/_update.py", FAKE_UPDATE)
        write(site / "platformio/package/manager/_registry.py",
              "class PackageManagerRegistryMixin:\n    def install_from_registry(self, spec, search_qualifiers=None):\n"
              "        raise AssertionError('Registry helper was not patched at its real MRO owner')\n")
        write(site / "platformio/package/manager/_symlink.py",
              "class PackageManagerSymlinkMixin:\n    def install_symlink(self, spec):\n"
              "        raise AssertionError('Symlink helper was not patched at its real MRO owner')\n"
              "    uninstall_symlink = install_symlink\n")
        write(site / "platformio/package/manager/_uninstall.py",
              "class PackageManagerUninstallMixin:\n    def uninstall(self, spec, skip_dependencies=False):\n"
              "        raise AssertionError('Uninstall was invoked')\n    uninstall_dependencies = uninstall\n")
        write(site / "platformio/package/manager/base.py", FAKE_BASE)
        write(site / "platformio/package/manager/platform.py", FAKE_PLATFORM)
        marker_code = "from pathlib import Path; Path(" + repr(str(pth_marker)) + ").write_text('unexpected')\n"
        write(site / "untrusted-startup.pth", "import pathlib; " + marker_code.removeprefix("from pathlib import Path; ").replace("Path(", "pathlib.Path("))
        write(site / "sitecustomize.py", marker_code)
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("PYTHON", "MCU_FLASHER", "PLATFORMIO"))}
        environment.update(PYTHONPATH=str(site), MCU_FLASHER_WORKSPACE_RUNTIME="1",
                           MCU_FLASHER_APP_ROOT=str(ROOT), PLATFORMIO_CORE_DIR="wrong-core",
                           PLATFORMIO_BUILD_DIR="wrong-output", UNEXPECTED_CALLER_ENV="refuse")
        parameters = {
            "--project-root": str(project), "--core-root": str(core),
            "--packages-root": str(packages), "--platforms-root": str(platforms),
            "--site-packages-root": str(site), "--output-root": str(output), "--jobs": "1",
            "--platform": "platformio/atmelavr@5.3.0", "--board": "uno", "--framework": "arduino",
        }
        base = [sys.executable, "-I", "-S", "-B", str(entry)]

        def arguments(changes=None):
            values = dict(parameters)
            values.update(changes or {})
            return [part for pair in values.items() for part in pair]

        def success(jobs="1"):
            imported.unlink(missing_ok=True)
            before = (inventory(project), inventory(prepared))
            child = run(base + arguments({"--jobs": jobs}), environment, owned)
            assert child.returncode == 0, (child.stdout, child.stderr)
            assert not child.stderr, child.stderr
            assert imported.is_file() and not pth_marker.exists()
            assert (inventory(project), inventory(prepared)) == before, "Tool changed captured input bytes/inventory"
            result = json.loads(child.stdout)
            assert result["argv"] == ["platformio", "run", "--project-dir", str(project), "-j", jobs]
            assert result["flags"] == [1, 1, 1]
            assert [encoding.lower().replace("-", "") for encoding in result["encoding"]] == ["utf8", "utf8"]
            expected = {
                "PLATFORMIO_CORE_DIR": core, "PLATFORMIO_PACKAGES_DIR": packages,
                "PLATFORMIO_PLATFORMS_DIR": platforms,
                "PLATFORMIO_BUILD_DIR": output / "build-output",
                "PLATFORMIO_LIBDEPS_DIR": output / "libdeps-output",
                "TMPDIR": output / "tmp-output",
            }
            for name, path in expected.items():
                assert result["environment"][name] == str(path), (name, result)
            assert not any(name.startswith("MCU_FLASHER") for name in result["environment"])
            assert "PYTHONPATH" not in result["environment"]
            assert "UNEXPECTED_CALLER_ENV" not in result["environment"]
            assert result["environment"]["PIP_NO_INDEX"] == "1"
            assert str(site) == result["path"][-1]
            assert str(ROOT) not in result["path"] and str(owned) not in result["path"]
            assert len(result["package_refusals"]) == 23, result
            CHECKS.append("fixed-compile-argv-explicit-roots-jobs-" + jobs)
            return result

        result = success()
        CHECKS.extend(("prepared-packages-returned-without-upstream-install-or-lock",
                       "real-base-mixin-order-registry-symlink-uninstall-guards",
                       "platform-override-skips-preparation-and-uninstall-side-effects",
                       "defined-utf8-stdout-and-stderr",
                       "default-tempfile-allocation-stays-in-owned-output",
                       "denied-tempfile-allocation-fails-without-fallback",
                       "project-and-prepared-input-inventory-bytes-preserved",
                       "missing-force-positional-local-symlink-vcs-registry-update-refusals",
                       "python-network-and-application-import-refusals",
                       "installer-and-shell-subprocess-audit-refusals-without-process-spawn",
                       "no-pth-sitecustomize-cwd-or-caller-policy-import"))
        success("6")

        def refused(label, changes=None, extra=(), reason=None):
            imported.unlink(missing_ok=True)
            child = run(base + arguments(changes) + list(extra), environment, owned)
            assert child.returncode == 2, (label, child.stdout, child.stderr)
            assert not imported.exists(), (label, "PlatformIO imported before policy refusal")
            assert not pth_marker.exists()
            if reason:
                assert reason in child.stderr, (label, child.stderr)
            CHECKS.append(label)

        (project / "platformio.ini").write_bytes(CONFIG.replace(b"@5.3.0", b"@5.1.0"))
        refused("old-5.1-target-and-matching-config-refused",
                {"--platform": "platformio/atmelavr@5.1.0"}, reason="pinned AVR sample")
        refused("old-5.1-config-with-5.3-target-refused", reason="config differs")
        (project / "platformio.ini").write_bytes(CONFIG)

        for jobs in ("0", "7", "-1", "1.0", "01", ""):
            refused("invalid-jobs-" + repr(jobs), {"--jobs": jobs})
        for key, value in (("--platform", "platformio/atmelavr@5.0.0"),
                           ("--platform", "platformio/espressif32@6.0.0"),
                           ("--board", "megaatmega2560"), ("--framework", "zephyr")):
            refused("unsupported-target-" + value, {key: value})
        refused("relative-root", {"--project-root": "project"})
        refused("dot-parent-root", {"--project-root": str(project) + "/../project"})
        refused("missing-root", {"--site-packages-root": str(owned / "missing")})
        for name in (".mcu_flasher_build_cache", ".PIO", ".VsCode", ".clangd", ".OPENCODE"):
            refused("protected-root-before-io:" + name,
                    {"--site-packages-root": str(owned / name / "missing")},
                    reason="Protected application paths")
        refused("non-directory-root", {"--site-packages-root": str(entry)})
        refused("wrong-writable-core-child", {"--core-root": str(packages)})
        refused("overlapping-root", {"--site-packages-root": str(packages)})
        refused("output-overlaps-prepared-input", {"--output-root": str(prepared), "--core-root": str(prepared / "core")},
                reason="must be independent")
        refused("upload-command-unavailable", extra=("--upload", "yes"))
        refused("duplicate-argument", extra=("--jobs", "1"))
        (project / "platformio.ini").write_bytes(CONFIG + b"extra_scripts = evil.py\n")
        refused("custom-hook-config")
        (project / "platformio.ini").write_bytes(CONFIG.replace(b"sources", b"external"))
        refused("changed-source-config")
        (project / "platformio.ini").write_bytes(CONFIG)
        (output / "build-output").rmdir()
        (output / "build-output").write_bytes(b"not an output directory")
        refused("output-file-alias")
        (output / "build-output").unlink()
        os.link(project / "platformio.ini", owned / "config-hardlink")
        refused("hardlinked-config")
        (owned / "config-hardlink").unlink()
        try:
            (owned / "linked-site").symlink_to(site, target_is_directory=True)
        except OSError:
            CHECKS.append("directory-symlink-refusal-not-executed-on-this-host")
        else:
            refused("directory-symlink-refusal", {"--site-packages-root": str(owned / "linked-site")})

        # Always start the real verifier children safely with -I -S -B. Simulate
        # each missing flag only after startup so the negative -S case cannot
        # execute installed interpreter .pth hooks before our entry is reached.
        for missing in ("isolated", "no_site", "dont_write_bytecode"):
            imported.unlink(missing_ok=True)
            flags = {name: name != missing for name in ("isolated", "no_site", "dont_write_bytecode")}
            launcher = ("import pathlib,sys,types; code=compile(pathlib.Path(sys.argv[1]).read_bytes(), "
                        "sys.argv[1], 'exec'); sys.flags=types.SimpleNamespace(**" + repr(flags)
                        + "); exec(code, {'__name__':'__main__', '__file__':sys.argv[1]})")
            child = run([sys.executable, "-I", "-S", "-B", "-c", launcher, str(entry)], environment, owned)
            assert child.returncode == 2 and "requires" in child.stderr, (missing, child.stderr)
            assert not imported.exists() and not pth_marker.exists()
            CHECKS.append("simulated-missing-flag-before-third-party-import:" + missing)
        assert ENTRY.read_bytes() == source
    report = dict(checks=CHECKS, count=len(CHECKS), real_tools_dispatched=False,
                  scope="source-isolated fake PlatformIO; startup flag negatives simulated safely",
                  limitations=["no full runtime/tool provenance", "no immutable tool lifetime",
                               "no native finite child owner", "nested Python startup unqualified",
                               "Windows real compiler and lock behavior unqualified",
                               "Python audit hooks are not an OS network sandbox"])
    (OUTPUT / "tool-only-entry.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(count=len(CHECKS), real_tools_dispatched=False,
                          report="temp/audit/native-compile-inputs/tool-only-entry.json")))


if __name__ == "__main__":
    verify()
