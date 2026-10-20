#!/usr/bin/env python3
"""Bounded, isolated native-widget smoke controller; Python is a developer tool."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import selectors
import secrets
import signal
import stat
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {
    "native_gtk_controls", "native_combo_callback", "native_button_callback",
    "offline_monaco", "offline_cpp_chunk", "offline_monaco_worker",
    "offline_xterm_unicode", "separate_webviews", "three_editor_only_reparents",
    "same_editor_widget", "native_owner_retained", "dirty_model_retained",
    "undo_redo_retained", "selection_retained", "terminal_widget_retained",
    "typed_ipc_refuses_owner_forgery", "no_python_application_service",
}


class OutputDirectory:
    def __init__(self, root):
        root = root.resolve(strict=True)
        self.path = root / "audit/migration/scratch/native-ui"
        self.fd = None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(root, flags)
        try:
            for part in ("audit", "migration", "scratch", "native-ui"):
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            self.fd = descriptor
            for name in ("latest-output.log", "latest-smoke.json"):
                self._check_leaf(name)
        except BaseException:
            os.close(descriptor)
            self.fd = None
            raise

    def _check_leaf(self, name):
        if self.fd is None:
            raise ValueError("Native-probe output directory is closed.")
        if name not in ("latest-output.log", "latest-smoke.json"):
            raise ValueError("Unknown native-probe output leaf.")
        try:
            metadata = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Native-probe output must be a regular non-symlink leaf.")

    def _temporary(self, name):
        self._check_leaf(name)
        leaf = f".{name}.{secrets.token_hex(12)}.tmp"
        descriptor = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        return leaf, descriptor

    def _replace(self, leaf, name):
        self._check_leaf(name)
        os.replace(leaf, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def _unlink_temporary(self, leaf):
        try:
            os.unlink(leaf, dir_fd=self.fd)
        except FileNotFoundError:
            pass

    def publish(self, name, content):
        leaf, descriptor = self._temporary(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            self._replace(leaf, name)
        finally:
            self._unlink_temporary(leaf)

    @contextmanager
    def capture_log(self):
        leaf, descriptor = self._temporary("latest-output.log")
        try:
            with os.fdopen(descriptor, "wb") as output:
                yield output
        finally:
            try:
                self._replace(leaf, "latest-output.log")
            finally:
                self._unlink_temporary(leaf)

    def read_log(self):
        self._check_leaf("latest-output.log")
        descriptor = os.open("latest-output.log", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Native-probe log is not a regular file.")
            content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Native-probe output log exceeds its 1 MiB read budget.")
        return content.decode(errors="replace")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class OwnedGroup:
    """A private, single-threaded controller capability; reap the leader last.

    wait()/poll() use WNOWAIT, retaining our direct child even after it exits.
    Its unreaped PID pins the captured session/group ID against reuse while we
    clean remaining members. No other code may waitpid() this child. If that
    invariant is lost, cleanup refuses numeric group signaling.
    """

    def __init__(self, *args, **kwargs):
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise RuntimeError("Owned group requires the default SIGCHLD disposition.")
        self.process = subprocess.Popen(*args, start_new_session=True, **kwargs)
        self.pid = self.process.pid
        self.closed = False
        self._assert_owned()

    @property
    def stdout(self):
        return self.process.stdout

    def _assert_owned(self):
        try:
            event = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError as error:
            raise RuntimeError("Owned leader was reaped elsewhere; refusing process-group signals.") from error
        if os.getpgid(self.pid) != self.pid or os.getsid(self.pid) != self.pid:
            raise RuntimeError("Captured child no longer owns the expected native session/group.")
        return event

    def poll(self):
        if self.closed:
            return self.process.returncode
        event = self._assert_owned()
        if event is None:
            return None
        return event.si_status if event.si_code == os.CLD_EXITED else -event.si_status

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            result = self.poll()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            time.sleep(0.02)

    def live_members(self):
        self._assert_owned()
        members = []
        with os.scandir("/proc") as entries:
            for count, entry in enumerate(entries):
                if count > 65536:
                    raise RuntimeError("Native process inventory exceeded its cleanup bound.")
                if not entry.name.isdecimal():
                    continue
                try:
                    with open(Path(entry.path) / "stat", "rb") as source:
                        data = source.read(8193)
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                if len(data) > 8192:
                    raise RuntimeError("Native process metadata exceeded its cleanup bound.")
                fields = data[data.rfind(b")") + 2:].split()
                if len(fields) >= 4 and fields[2] == str(self.pid).encode() and fields[3] == str(self.pid).encode() and fields[0] not in (b"Z", b"X"):
                    members.append(int(entry.name))
        return members


def stop_owned(group):
    if not isinstance(group, OwnedGroup):
        raise TypeError("Cleanup requires a captured OwnedGroup capability.")
    if group.closed:
        return
    # The leader remains our unreaped child for BOTH signal phases. Even if
    # every other member exits between these calls, this numeric ID cannot be
    # recycled into an unrelated process/session/group until the final wait.
    group._assert_owned()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while group.live_members():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        else:
            group.process.wait(timeout=5)
            group.closed = True
            return
    raise RuntimeError("Captured native process group still has live members after SIGKILL.")


def spawn_owned(*args, **kwargs):
    return OwnedGroup(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-display", action="store_true", help="Use the caller's display, including native Wayland; otherwise own an isolated Xvfb.")
    options = parser.parse_args()
    binary = ROOT / "desktop/target/debug/mcu-native-ui-probe"
    assert binary.is_file(), "Native Rust widget probe is not built."
    output_directory = OutputDirectory(ROOT)
    try:
        run_fixture(options, binary, output_directory)
    finally:
        output_directory.close()


def run_fixture(options, binary, output_directory):
    scratch = output_directory.path
    server = None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="controller-", dir=scratch) as location:
        owned = Path(location)
        env = dict(os.environ)
        for key, child in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
            target = owned / child
            target.mkdir()
            env[key] = str(target)
        try:
            if not options.current_display:
                xvfb = ROOT / ".migration-tools/xvfb/usr/bin/Xvfb"
                assert xvfb.is_file(), "Private Xvfb is unavailable; use --current-display for an existing native display."
                with (owned / "xvfb.log").open("wb") as server_log:
                    server = spawn_owned([str(xvfb), "-displayfd", "1", "-screen", "0", "1920x1080x24", "-nolisten", "tcp", "-noreset"], stdout=subprocess.PIPE, stderr=server_log)
                watcher = selectors.DefaultSelector()
                watcher.register(server.stdout, selectors.EVENT_READ)
                try:
                    assert watcher.select(10), "Owned Xvfb did not announce a display."
                    display = server.stdout.readline(32).decode("ascii").strip()
                    assert display.isdecimal() and server.poll() is None, "Owned Xvfb failed startup."
                finally:
                    watcher.close()
                env.update(DISPLAY=":" + display, GDK_BACKEND="x11", LIBGL_ALWAYS_SOFTWARE="1", GALLIUM_DRIVER="llvmpipe", __GLX_VENDOR_LIBRARY_NAME="mesa", WEBKIT_DISABLE_DMABUF_RENDERER="1")
            with output_directory.capture_log() as log:
                task = spawn_owned(["dbus-run-session", "--", str(binary), "--smoke"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    return_code = task.wait(timeout=70)
                finally:
                    stop_owned(task)
            output = output_directory.read_log()
            receipts = [line.removeprefix("MCU_NATIVE_PROBE=") for line in output.splitlines() if line.startswith("MCU_NATIVE_PROBE=")]
            assert len(receipts) == 1, f"Expected one native receipt, got {len(receipts)}. See {scratch / 'latest-output.log'}"
            receipt = json.loads(receipts[0])
            receipt.update(controller="Python developer verification only", elapsedSeconds=round(time.monotonic() - started, 3), displayMode="current-native-display" if options.current_display else "owned-Xvfb-x11-software-rendering")
            output_directory.publish("latest-smoke.json", (json.dumps(receipt, indent=2) + "\n").encode())
            assert return_code == 0 and receipt["passed"], receipt
            assert {"GtkWindow", "GtkComboBoxText", "GtkButton", "GtkLabel", "GtkPaned"} <= set(receipt["widgetTypes"]), receipt["widgetTypes"]
            assert REQUIRED <= receipt["checks"].keys(), sorted(REQUIRED - receipt["checks"].keys())
            assert all(receipt["checks"].values()), receipt
            print(json.dumps(receipt, indent=2))
        finally:
            if server:
                try:
                    stop_owned(server)
                finally:
                    server.stdout.close()


if __name__ == "__main__":
    main()
