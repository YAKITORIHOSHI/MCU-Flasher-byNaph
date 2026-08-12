"""Suppress console window flashes from subprocess and os.spawnve on Windows."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_installed = False


def _hide_subprocess_kwargs(kwargs: dict) -> dict:
    import subprocess

    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    kwargs.setdefault("creationflags", 0)
    kwargs["creationflags"] |= create_no_window
    return kwargs


def _merge_env(env):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return merged


def install() -> None:
    """Patch subprocess and os.spawnve so child tools run without console windows."""
    global _installed
    if _installed or sys.platform != "win32":
        return

    import subprocess

    orig_popen_cls = subprocess.Popen
    orig_run = subprocess.run
    orig_call = subprocess.call
    orig_check_call = subprocess.check_call
    orig_check_output = subprocess.check_output
    orig_spawnve = os.spawnve
    orig_waitpid = os.waitpid

    # ── Track Popen objects spawned via P_NOWAIT ──────────────────────
    # SCons (used by PlatformIO) converts P_WAIT into P_NOWAIT +
    # os.waitpid.  Our old code created a temporary HiddenPopen,
    # extracted .pid, and discarded the object.  When Python GC'd the
    # Popen, the Windows process handle was closed, so the subsequent
    # os.waitpid failed with errno 10 (ECHILD / "No child processes").
    # Fix: keep every P_NOWAIT Popen alive in a dict keyed by PID,
    # and patch os.waitpid to use Popen.wait() for managed processes.
    import threading as _threading
    _popen_registry: dict[int, subprocess.Popen] = {}
    _registry_lock = _threading.Lock()

    class HiddenPopen(orig_popen_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **_hide_subprocess_kwargs(kwargs))

    def run(*args, **kwargs):
        return orig_run(*args, **_hide_subprocess_kwargs(kwargs))

    def call(*args, **kwargs):
        return orig_call(*args, **_hide_subprocess_kwargs(kwargs))

    def check_call(*args, **kwargs):
        return orig_check_call(*args, **_hide_subprocess_kwargs(kwargs))

    def check_output(*args, **kwargs):
        return orig_check_output(*args, **_hide_subprocess_kwargs(kwargs))

    def _fix_pythonw_in_cmd(cmd_str: str) -> str:
        """Replace pythonw.exe with python.exe in a cmd-line string.

        pythonw.exe discards stdout/stderr on startup, so tools like esptool
        that write progress to stdout fail silently when invoked via pythonw.
        SCons builds the esptool command using sys.executable, which is
        pythonw.exe when the GUI is launched via the VBS launcher.  Swap it
        for python.exe here as a belt-and-suspenders safety net.
        """
        import re as _re
        return _re.sub(
            r'(?i)(pythonw\.exe)',
            lambda m: m.group(0).lower().replace("pythonw.exe", "python.exe"),
            cmd_str,
        )

    def spawnve(mode, path, args, env):
        kwargs = _hide_subprocess_kwargs({})
        merged_env = _merge_env(env)

        # SCons redirects standard descriptors (0, 1, 2) before calling spawnve.
        # Since we use subprocess.Popen, we must explicitly pass the Win32 handles
        # corresponding to those descriptors so SCons can capture stdout/stderr.
        import msvcrt
        si = kwargs.get("startupinfo")
        if si is None:
            si = subprocess.STARTUPINFO()
            kwargs["startupinfo"] = si
        si.dwFlags |= subprocess.STARTF_USESTDHANDLES
        try:
            si.hStdInput = msvcrt.get_osfhandle(0)
        except OSError:
            pass
        try:
            si.hStdOutput = msvcrt.get_osfhandle(1)
        except OSError:
            pass
        try:
            si.hStdError = msvcrt.get_osfhandle(2)
        except OSError:
            pass

        # On Windows, if args is a list, subprocess will use list2cmdline which backslash-escapes quotes.
        # This breaks cmd.exe /C arguments since cmd.exe does not understand backslash-escaped quotes.
        # To fix this, we can format the command line string manually when running cmd.exe.
        is_cmd = False
        if path:
            name = os.path.basename(path).lower()
            if name in ("cmd.exe", "cmd"):
                is_cmd = True
        elif args and args[0]:
            name = os.path.basename(args[0]).lower()
            if name in ("cmd.exe", "cmd"):
                is_cmd = True

        if is_cmd and len(args) >= 3 and args[1].lower() in ("/c", "/r", "/k"):
            # Construct a raw command line string.
            # args[2] is the entire command line that cmd.exe is supposed to run.
            # PlatformIO wraps it in outer double-quotes (""cmd" "args""") for
            # cmd.exe /C, which strips the outermost pair before executing.
            # We do NOT add another layer of quotes; just pass args[2] directly.
            cmd = f'"{path or args[0]}" {args[1]} {_fix_pythonw_in_cmd(args[2])}'
            # This is a single command-line string, so CreateProcess resolves
            # the target purely from that string. Passing executable= here
            # would be redundant, not incorrect -- so leave the exec_kwargs
            # path below alone for this branch (it sets executable=path).
            exec_kwargs = {"executable": path} if path else {}
        else:
            # SCons/os.spawnve gives us the real executable's path separately
            # from argv. Passing BOTH a list `cmd` (whose cmd[0] is argv[0],
            # often just a bare/relative name) AND executable=path to
            # subprocess.run/Popen is what breaks CreateProcess here: Windows
            # needs a single, consistent source of truth for which file to
            # launch. When we have a real path, drive CreateProcess off that
            # path alone -- rewrite cmd[0] to the resolved path and drop the
            # separate executable= kwarg, so there is no chance of the two
            # disagreeing (e.g. relative vs absolute, case, or extension).
            cmd = list(args)
            if path:
                resolved = str(path)
                if cmd:
                    cmd[0] = resolved
                else:
                    cmd = [resolved]
                exec_kwargs = {}
            else:
                exec_kwargs = {}

        try:
            if mode == os.P_WAIT:
                res = orig_run(cmd, env=merged_env, close_fds=False, **exec_kwargs, **kwargs)
                return res.returncode

            if mode == os.P_NOWAIT:
                proc = HiddenPopen(cmd, env=merged_env, close_fds=False, **exec_kwargs, **kwargs)
                with _registry_lock:
                    _popen_registry[proc.pid] = proc
                return proc.pid
        except Exception as e:
            raise

        return orig_spawnve(mode, path, args, env)

    def waitpid(pid, options):
        """Wait for a process, using Popen.wait() for managed processes."""
        with _registry_lock:
            proc = _popen_registry.get(pid)
        if proc is not None:
            import os
            # If WNOHANG is specified, check if the process is still running
            if options & getattr(os, "WNOHANG", 1):
                exit_code = proc.poll()
                if exit_code is None:
                    # Process is still running, return (0, 0)
                    return (0, 0)
                # Process has completed, clean up registry
                with _registry_lock:
                    _popen_registry.pop(pid, None)
                return (pid, exit_code << 8)
            else:
                # Blocking wait, clean up registry
                with _registry_lock:
                    _popen_registry.pop(pid, None)
                exit_code = proc.wait()
                return (pid, exit_code << 8)
        # Fall back to the real os.waitpid for processes we didn't spawn
        return orig_waitpid(pid, options)

    subprocess.Popen = HiddenPopen
    subprocess.run = run
    subprocess.call = call
    subprocess.check_call = check_call
    subprocess.check_output = check_output
    os.spawnve = spawnve
    os.waitpid = waitpid

    _installed = True


def install_venv_site_hook(project_root: Path | None = None) -> None:
    """Auto-load the hide patch for every Python process in the project venv."""
    if sys.platform != "win32":
        return

    root = project_root or Path(__file__).resolve().parent.parent.parent
    venv_site = root / "env" / "Lib" / "site-packages"
    if not venv_site.is_dir():
        return

    hook_py = venv_site / "mcu_flash_gui_subprocess_hook.py"
    hook_pth = venv_site / "mcu_flash_gui_subprocess_hook.pth"

    hook_py.write_text(
        f'''"""Auto-installed hook: hide subprocess console windows on Windows."""
import sys
from pathlib import Path

_root = Path({str(root)!r})
_modules = _root / "src" / "modules"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if str(_modules) not in sys.path:
    sys.path.insert(0, str(_modules))

if sys.platform == "win32":
    try:
        from win_subprocess_hide import install
        install()
    except Exception:
        pass
''',
        encoding="utf-8",
    )
    hook_pth.write_text("import mcu_flash_gui_subprocess_hook\n", encoding="utf-8")