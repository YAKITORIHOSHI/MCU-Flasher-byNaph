"""Own one native PTY command and its orphaned tools for its entire lifetime.

Run with the private Python as ``python -I -B <this file> -- <command>``.
The launcher supplies a new session and PTY stdio. This standalone process
acquires its controlling terminal before starting the command.
Linux subreaper custody also covers tools that create separate sessions.
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import signal
import subprocess
import sys
import termios
import time


def _enable_subreaper() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("native PTY containment requires Linux")
    library = ctypes.CDLL(None, use_errno=True)
    library.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                              ctypes.c_ulong, ctypes.c_ulong]
    library.prctl.restype = ctypes.c_int
    # linux/prctl.h: PR_SET_CHILD_SUBREAPER. Establish custody before fork.
    if library.prctl(36, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _restore_child_signals() -> None:
    # Ignored dispositions survive exec; restore these before the command.
    for name in ("SIGINT", "SIGQUIT", "SIGTERM", "SIGHUP", "SIGTSTP",
                 "SIGTTIN", "SIGTTOU", "SIGCHLD"):
        signal.signal(getattr(signal, name), signal.SIG_DFL)


def _acquire_terminal() -> None:
    # Popen's native session setup avoids running Python after a fork inside
    # the threaded Qt process. Only this fresh interpreter attaches the tty.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(0, os.getpgrp())


def _reap_adopted() -> None:
    # Only this supervisor's children can be waited on. Bound one drain so a
    # command creating many descendants cannot monopolize shutdown forever.
    for _ in range(1024):
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if pid == 0:
            return


def _close_descendants(owner, psutil) -> bool:
    """Terminate checked descendants, including newly adopted orphans."""
    deadline = time.monotonic() + 1.5
    while True:
        try:
            children = owner.children(recursive=True)
        except psutil.Error:
            return False
        if not children:
            _reap_adopted()
            return True
        for child in reversed(children):
            try:
                child.terminate()  # psutil checks the captured creation identity.
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(children, timeout=min(.25, max(0, deadline - time.monotonic())))
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=min(.15, max(0, deadline - time.monotonic())))
        _reap_adopted()
        if time.monotonic() >= deadline:
            try:
                return not owner.children(recursive=True)
            except psutil.Error:
                return False


def supervise(command: list[str], *, acquire_terminal: bool = False) -> int:
    try:
        import psutil
        _enable_subreaper()
        if acquire_terminal:
            _acquire_terminal()
        owner = psutil.Process(os.getpid())
    except Exception as exc:
        print(f"Terminal child containment could not start: {exc}. "
              "Run Ubuntu Bootstrap --repair and retry.", file=sys.stderr, flush=True)
        return 125

    requested_signal = [0]

    def stop(signum, _frame):
        requested_signal[0] = signum

    # Keyboard signals already reach the command through the foreground PTY
    # group. The supervisor must keep custody while the command handles them.
    for name in ("SIGINT", "SIGQUIT", "SIGTSTP", "SIGTTIN", "SIGTTOU"):
        signal.signal(getattr(signal, name), signal.SIG_IGN)
    for name in ("SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, name), stop)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    status = 125
    try:
        # This standalone supervisor has no threads. Reset only child signal
        # dispositions; do not introduce pipes, sessions or foreground groups.
        child = subprocess.Popen(command, preexec_fn=_restore_child_signals)
        while not requested_signal[0]:
            try:
                result = child.wait(timeout=.05)
            except subprocess.TimeoutExpired:
                continue
            status = result if result >= 0 else 128 - result
            break
        if requested_signal[0]:
            status = 128 + requested_signal[0]
    except Exception as exc:
        print(f"Terminal command could not start: {exc}", file=sys.stderr, flush=True)
    finally:
        if not _close_descendants(owner, psutil):
            print("Terminal tools did not finish within the shutdown deadline.",
                  file=sys.stderr, flush=True)
            status = 125
    return status


def main() -> int:
    arguments = sys.argv[1:]
    if len(arguments) < 2 or arguments[0] != "--":
        print("Expected a native command after --.", file=sys.stderr)
        return 125
    return supervise(arguments[1:], acquire_terminal=True)


if __name__ == "__main__":
    raise SystemExit(main())
