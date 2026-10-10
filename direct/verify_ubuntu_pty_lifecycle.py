"""Ubuntu context/input/owned-child fixtures; no live projects or AI requests."""
from pathlib import Path
from contextlib import redirect_stderr
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from main.qt.posix_terminal_panel import PtySession
from main.platforms.ubuntu_pty import PromptObserver, close_pty_tree
from main.platforms.ubuntu_pty_process import NativePtyProcess
from main.platforms.ubuntu_assistant import prepare_context
from main.qt.garbage_collection import install_gui_garbage_collector

APP = QApplication.instance() or QApplication([])
COLLECTOR = install_gui_garbage_collector(APP)


class UbuntuPtyLifecycleChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/ubuntu-parity/pty"
        audit.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("AGENTS.md", ".opencodeignore", ".opencode/skills/sketch-workflow/SKILL.md",
                     ".opencode/skills/mcu-sketch-target/SKILL.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Fixture guidance", encoding="utf-8")

    def wait_for(self, condition, seconds=4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            APP.processEvents()
            if condition():
                return
            time.sleep(.005)
        self.fail("Owned fixture did not finish")

    def test_prompt_persistence_runs_off_the_gui_without_changing_input(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        observed = []
        owner = threading.get_ident()

        def publish(tracker, prompt):
            observed.append((threading.get_ident(), prompt))
            entered.set()
            release.wait(2)
            finished.set()

        with patch("src.modules.ai_prompt_context.PromptInputTracker._publish", publish), \
                patch("src.modules.ai_prompt_context.assistant_process_active", side_effect=AssertionError("Dedicated CLI needs no process scan")):
            observer = PromptObserver(self.root, active=True)
            self.addCleanup(observer.close)
            begin = time.monotonic()
            observer.feed("\x1b[?1;2c\x1b[200~Fix Ω😀漢字\nheader\x1b[201~\r", 1)
            self.assertLess(time.monotonic() - begin, .1)
            self.assertTrue(entered.wait(1))
            self.assertNotEqual(observed[0][0], owner)
            self.assertEqual(observed[0][1], "Fix Ω😀漢字\nheader")
            observer.close()
            observer.feed("must not publish after close\r", 1)
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertEqual(len(observed), 1)

    def test_generic_assistant_detection_is_also_off_the_gui_thread(self):
        seen, finished = [], threading.Event()
        owner = threading.get_ident()
        def detect(pid):
            seen.append((threading.get_ident(), pid))
            return True
        with patch("src.modules.ai_prompt_context.assistant_process_active", side_effect=detect), \
                patch("src.modules.ai_prompt_context.PromptInputTracker._publish", side_effect=lambda *_: finished.set()):
            observer = PromptObserver(self.root)
            self.addCleanup(observer.close)
            observer.feed("Update the sketch\r", 123)
            self.assertTrue(finished.wait(1))
            self.assertEqual(seen[0][1], 123)
            self.assertNotEqual(seen[0][0], owner)

    def backend(self, durable=None, running=False):
        return SimpleNamespace(_hardware_state_lock=threading.Lock(), _hardware_state_running=running,
                               _last_synced_hardware_payload=durable,
                               _sync_project_hardware_state=Mock(return_value=(self.root.name, str(self.root), "latest target")))

    def test_context_waits_for_the_exact_durable_project_state(self):
        api = self.backend(running=True)
        def save():
            time.sleep(.08)
            with api._hardware_state_lock:
                api._last_synced_hardware_payload = api._sync_project_hardware_state.return_value
                api._hardware_state_running = False
        worker = threading.Thread(target=save)
        worker.start()
        with patch("main.core.file_utils.ensure_hidden_read_first_md") as instructions:
            self.assertTrue(prepare_context(api, self.root, timeout=1))
        worker.join()
        instructions.assert_called_once_with(self.root)
        api._sync_project_hardware_state.assert_called_once_with(self.root)

    def test_failed_or_other_project_state_is_not_accepted(self):
        for durable in (None, ("other", str(self.root / "other")), (self.root.name, str(self.root), "old target")):
            with patch("main.core.file_utils.ensure_hidden_read_first_md"), self.assertRaisesRegex(RuntimeError, "could not be saved"):
                prepare_context(self.backend(durable), self.root)

    def test_swallowed_instruction_write_failure_cannot_start_the_assistant(self):
        (self.root / "AGENTS.md").unlink()
        with patch("main.core.file_utils.ensure_hidden_read_first_md"), self.assertRaisesRegex(RuntimeError, "instructions could not be prepared"):
            prepare_context(self.backend(), self.root)

    def test_actual_backend_queue_acknowledges_the_requested_revision_in_fixture_storage(self):
        from main import web_bridge
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api.sketch_dir_path = self.root
        api.current_board, api.current_port = "", ""
        api._hardware_state_lock = threading.Lock()
        api._hardware_state_running = False
        api._hardware_state_pending = api._hardware_state_active_payload = api._last_synced_hardware_payload = None
        api.emit = Mock()
        storage = self.root / "fixture-state"
        storage.mkdir()
        with patch.object(web_bridge, "get_project_build_cache_root", return_value=storage), \
                patch.object(web_bridge, "ensure_hidden_read_first_md"), \
                patch.object(web_bridge, "hide_internal_project_metadata"), \
                patch("main.core.file_utils.ensure_hidden_read_first_md"):
            self.assertTrue(prepare_context(api, self.root, timeout=2))
            value = json.loads((storage / "project_state.json").read_text())
            self.assertEqual(value["project_path"], str(self.root))
            self.assertIsNone(value["hardware"]["board_name"])
            self.assertIsNone(value["hardware"]["port"])
            old = api._last_synced_hardware_payload
            api.current_baud = 57600
            self.assertTrue(prepare_context(api, self.root, timeout=2))
            self.assertNotEqual(api._last_synced_hardware_payload, old)
            self.assertEqual(json.loads((storage / "project_state.json").read_text())["hardware"]["baud_rate"], 57600)

    def test_pending_context_times_out_and_cancelled_context_has_no_mutation(self):
        with patch("main.core.file_utils.ensure_hidden_read_first_md") as instructions:
            self.assertFalse(prepare_context(self.backend(), self.root, cancelled=lambda: True))
            instructions.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "still being saved"):
                prepare_context(self.backend(running=True), self.root, timeout=.03)

    def test_exited_pty_never_looks_up_a_potentially_reused_pid(self):
        process = Mock(isalive=Mock(return_value=False))
        with patch("psutil.Process", side_effect=AssertionError("Reused PID lookup")):
            close_pty_tree(process)
        process.close.assert_called_once_with(force=True)

    def test_threaded_gui_launch_has_no_python_fork_callback_and_owns_a_controlling_tty(self):
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(3))
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(release.set)
        command = (
            "import fcntl,json,os,signal,struct,sys,termios\n"
            "dimensions=lambda: list(struct.unpack('HHHH',fcntl.ioctl(0,termios.TIOCGWINSZ,b'\\0'*8))[:2])\n"
            "ttyfd=os.open('/dev/tty',os.O_RDWR); os.close(ttyfd)\n"
            "signal.signal(signal.SIGWINCH,lambda *_: print('RESIZED:'+json.dumps(dimensions()),flush=True))\n"
            "print('TTY:'+json.dumps([os.isatty(0),os.tcgetpgrp(0)==os.getpgrp(),dimensions()]),flush=True)\n"
            "sys.stdin.readline()\n"
        )
        session = PtySession(str(self.root), [sys.executable, "-I", "-u", "-c", command])
        self.addCleanup(session.close)
        session.resize(37, 111)
        output = []
        session.output.connect(output.append)
        import subprocess
        original = subprocess.Popen
        def launch(*args, **kwargs):
            self.assertNotIn("preexec_fn", kwargs, "Qt must never execute Python after fork")
            self.assertTrue(kwargs["start_new_session"])
            self.assertTrue(kwargs["close_fds"])
            return original(*args, **kwargs)
        with patch("os.fork", side_effect=AssertionError("Python fork called by Qt")), \
                patch("os.forkpty", side_effect=AssertionError("forkpty called by Qt")), \
                patch("main.platforms.ubuntu_pty_process.subprocess.Popen", side_effect=launch):
            self.assertTrue(session.start())
        self.wait_for(lambda: "TTY:" in "".join(output))
        result = json.loads("".join(output).split("TTY:", 1)[1].splitlines()[0])
        self.assertEqual(result, [True, True, [37, 111]])
        session.resize(42, 99)
        self.wait_for(lambda: "RESIZED:[42, 99]" in "".join(output))
        release.set()
        worker.join()

    def test_failed_native_launch_closes_both_owned_pty_descriptors(self):
        descriptors = []
        openpty = os.openpty
        def allocate():
            pair = openpty()
            descriptors.extend(pair)
            return pair
        with patch("main.platforms.ubuntu_pty_process.os.openpty", side_effect=allocate), \
                patch("main.platforms.ubuntu_pty_process.subprocess.Popen", side_effect=OSError("fixture spawn failed")), \
                self.assertRaisesRegex(OSError, "fixture spawn failed"):
            NativePtyProcess.spawn(["fixture-command"], cwd=self.root, env=os.environ.copy(), dimensions=(24, 80))
        self.assertEqual(len(descriptors), 2)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_owned_shutdown_kills_tools_in_separate_process_sessions(self):
        import psutil
        tool = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
        parent = (
            "import json,subprocess,sys,time\n"
            f"a=subprocess.Popen([sys.executable,'-I','-c',{tool!r}])\n"
            f"b=subprocess.Popen([sys.executable,'-I','-c',{tool!r}],start_new_session=True)\n"
            "print('CHILDREN:'+json.dumps([a.pid,b.pid]),flush=True)\n"
            "time.sleep(30)\n"
        )
        session = PtySession(str(self.root), [sys.executable, "-I", "-u", "-c", parent])
        self.addCleanup(session.close)
        output = []
        session.output.connect(output.append)
        self.assertTrue(session.start())
        self.wait_for(lambda: "CHILDREN:" in "".join(output))
        pids = json.loads("".join(output).split("CHILDREN:", 1)[1].splitlines()[0])
        handles = [psutil.Process(pid) for pid in pids]
        self.addCleanup(lambda: [process.kill() for process in handles if process.is_running() and process.status() != psutil.STATUS_ZOMBIE])
        start = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - start, .1, "Shutdown may not wait for tool processes on Qt")
        self.assertIsNone(session.process)
        self.wait_for(lambda: all(not process.is_running() or process.status() == psutil.STATUS_ZOMBIE for process in handles))

    def test_cli_exit_reaps_its_detached_tools_before_the_pty_ends(self):
        import psutil
        tool = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
        parent = (
            "import json,subprocess,sys\n"
            f"a=subprocess.Popen([sys.executable,'-I','-c',{tool!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            f"b=subprocess.Popen([sys.executable,'-I','-c',{tool!r}],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "print('CHILDREN:'+json.dumps([a.pid,b.pid]),flush=True)\n"
            "sys.stdin.readline()\n"
            "raise SystemExit(17)\n"
        )
        session = PtySession(str(self.root), [sys.executable, "-I", "-u", "-c", parent])
        self.addCleanup(session.close)
        output, ended = [], []
        session.output.connect(output.append)
        session.ended.connect(ended.append)
        self.assertTrue(session.start())
        supervisor = session.process
        self.wait_for(lambda: "CHILDREN:" in "".join(output))
        handles = [psutil.Process(pid) for pid in json.loads("".join(output).split("CHILDREN:", 1)[1].splitlines()[0])]
        self.addCleanup(lambda: [process.kill() for process in handles if process.is_running() and process.status() != psutil.STATUS_ZOMBIE])
        session.write("exit now\r")
        self.wait_for(lambda: bool(ended))
        self.assertTrue(all(not process.is_running() for process in handles), "Detached tools must be reaped before a new CLI session starts")
        self.wait_for(lambda: supervisor.exitstatus is not None)
        self.assertEqual(supervisor.exitstatus, 17, "Containment preserves the original CLI exit status")

    def test_containment_failure_does_not_launch_an_unowned_command(self):
        from main.platforms import ubuntu_pty_supervisor as supervisor
        diagnostic = io.StringIO()
        with patch.object(supervisor, "_enable_subreaper", side_effect=OSError("fixture containment unavailable")), \
                patch.object(supervisor.subprocess, "Popen", side_effect=AssertionError("Uncontained command launched")), \
                redirect_stderr(diagnostic):
            self.assertEqual(supervisor.supervise(["fixture-command"]), 125)
        self.assertIn("Bootstrap --repair", diagnostic.getvalue())

    def test_signal_shutdown_has_no_already_reaped_subprocess_warning(self):
        command = "import time; print('SIGNAL_FIXTURE_READY',flush=True); time.sleep(30)"
        session = PtySession(str(self.root), [sys.executable, "-I", "-u", "-c", command])
        self.addCleanup(session.close)
        output, ended = [], []
        session.output.connect(output.append)
        session.ended.connect(ended.append)
        native_spawn = NativePtyProcess.spawn

        def strict_spawn(arguments, **options):
            # -I ignores inherited PYTHONWARNINGS. Pass the warning policy to
            # the actual supervisor interpreter so destructor warnings are visible.
            return native_spawn([arguments[0], "-W", "error", *arguments[1:]], **options)

        with patch.object(NativePtyProcess, "spawn", side_effect=strict_spawn):
            self.assertTrue(session.start())
        supervisor = session.process
        self.wait_for(lambda: "SIGNAL_FIXTURE_READY" in "".join(output))
        supervisor._handle.send_signal(signal.SIGTERM)
        self.wait_for(lambda: bool(ended))
        self.wait_for(lambda: supervisor.exitstatus is not None)
        self.assertEqual(supervisor.exitstatus, 128 + signal.SIGTERM)
        self.assertNotIn("ResourceWarning", "".join(output))
        self.assertNotIn("Exception ignored", "".join(output))

    def test_ctrl_c_reaches_a_native_command_without_terminating_custody_early(self):
        command = "import time; print('COMMAND_READY',flush=True); time.sleep(30)"
        session = PtySession(str(self.root), [sys.executable, "-I", "-u", "-c", command])
        self.addCleanup(session.close)
        output, ended = [], []
        session.output.connect(output.append)
        session.ended.connect(ended.append)
        self.assertTrue(session.start())
        supervisor = session.process
        self.wait_for(lambda: "COMMAND_READY" in "".join(output))
        session.write("\x03")
        self.wait_for(lambda: bool(ended))
        self.wait_for(lambda: supervisor.exitstatus is not None)
        self.assertEqual(supervisor.exitstatus, 130)

    @unittest.skipUnless(shutil.which("bash"), "Native Bash job-control fixture")
    def test_bash_job_control_preserves_the_interactive_shell(self):
        import psutil
        session = PtySession(str(self.root), [shutil.which("bash"), "--noprofile", "--norc", "-i"])
        self.addCleanup(session.close)
        output = []
        session.output.connect(output.append)
        with patch.dict(os.environ, {"HISTFILE": os.devnull, "INPUTRC": os.devnull,
                                     "PS1": "MCU_FIXTURE> ", "PROMPT_COMMAND": ""}):
            self.assertTrue(session.start())
        self.wait_for(lambda: "MCU_FIXTURE>" in "".join(output))
        self.assertNotIn("no job control", "".join(output))
        self.assertNotIn("cannot set terminal process group", "".join(output))
        session.write("sleep 30\r")
        tools = []
        def foreground_started():
            tools[:] = [child for child in psutil.Process(session.process.pid).children(recursive=True)
                        if child.name() == "sleep"]
            return bool(tools)
        self.wait_for(foreground_started)
        self.addCleanup(lambda: [process.kill() for process in tools if process.is_running() and process.status() != psutil.STATUS_ZOMBIE])
        session.write("\x03")
        self.wait_for(lambda: all(not process.is_running() for process in tools))
        self.assertIsNotNone(session.process)
        session.write("printf 'AFTER_CTRL_%s\\n' C\r")
        # Bash/readline may insert bracketed-paste mode escapes at line edges;
        # the echoed command does not itself contain this completed marker.
        self.wait_for(lambda: "AFTER_CTRL_C" in "".join(output))
        self.assertTrue(session.process.isalive(), "Bash must remain available after interrupting its foreground job")
        session.write("sleep 30\r")
        self.wait_for(foreground_started)
        session.write("\x1a")
        self.wait_for(lambda: all(process.status() == psutil.STATUS_STOPPED for process in tools))
        session.write("fg\r")
        self.wait_for(lambda: all(process.status() != psutil.STATUS_STOPPED for process in tools))
        session.write("\x03")
        self.wait_for(lambda: all(not process.is_running() for process in tools))
        session.write("printf 'AFTER_CTRL_%s\\n' Z\r")
        self.wait_for(lambda: "AFTER_CTRL_Z" in "".join(output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
