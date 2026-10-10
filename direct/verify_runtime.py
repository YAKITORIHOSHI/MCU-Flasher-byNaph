#!/usr/bin/env python3
"""Hardware-free regression checks for targets, recovery and the Qt workspace.

Run using the app runtime. --render-dir additionally captures the real Qt UI
with simulated sketch data; it never opens a serial port or runs a build.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import runpy
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from main.core.target_profile import target_problem, requires_upload_port
from src.modules.recovery import RecoveryBudget
from src.modules import private_python_guard as guard


class RuntimeChecks(unittest.TestCase):
    def test_application_guidance_does_not_escape_to_sketches(self):
        from main.core.file_utils import application_agent_guidance
        self.assertIn("Application development scope", application_agent_guidance(ROOT))
        self.assertEqual(application_agent_guidance(ROOT / "temp/scratch/ordinary-sketch"), "")

    def test_source_syntax(self):
        files = [ROOT / "mcu_flash_gui.py", *ROOT.joinpath("main").rglob("*.py")]
        files += [ROOT / "src/modules" / name for name in ("private_python_guard.py", "recovery.py", "platform_runtime.py", "runtime_resources.py", "launcher.py", "project_terminal.py", "arduino_lib_req.py", "bootstrap.py", "offline_bootstrap.py", "offline_runtime.py", "offline_platformio.py", "dedicated_AI.py", "tk_glass.py", "ui_palette.py", "ui_metrics.py")]
        files += [ROOT / "direct/setup_ubuntu.py", ROOT / "direct/ubuntu/setup.py",
                  ROOT / "src/modules/package_jobs.py", ROOT / "src/modules/board_preparation.py"]
        for path in files:
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")

    def test_cpu_guard_and_unknown_cpu_fail_closed(self):
        from src.modules import runtime_resources as resources
        for cores in (1, 2, 3, None):
            with patch.object(resources.os, "cpu_count", return_value=cores), patch.object(resources, "show_incompatibility_notice") as notice:
                self.assertFalse(resources.enforce_minimum_cpu_requirement())
                notice.assert_called_once()
                self.assertIn("Minimum required: 4", notice.call_args.args[0])
        for cores in (4, 6, 8):
            with patch.object(resources.os, "cpu_count", return_value=cores), patch.object(resources, "physical_cpu_count", return_value=4), patch.object(resources, "show_incompatibility_notice") as notice:
                self.assertTrue(resources.enforce_minimum_cpu_requirement())
                notice.assert_not_called()
        for logical, physical, supported in ((4, 2, False), (6, 3, False), (8, 4, True), (12, 6, True), (4, None, True)):
            with patch.object(resources.os, "cpu_count", return_value=logical), patch.object(resources, "physical_cpu_count", return_value=physical), patch.object(resources, "show_incompatibility_notice"):
                self.assertEqual(resources.enforce_minimum_cpu_requirement(), supported)

    def test_cpu_guard_precedes_runtime_and_gui_initialization(self):
        from src.modules import runtime_resources as resources
        with patch.object(resources.os, "cpu_count", return_value=3), patch.object(resources, "show_incompatibility_notice"), patch.object(guard, "enforce_private_python", side_effect=AssertionError("Private runtime initialized before CPU guard")):
            for entry in ("mcu_flash_gui.py", "main/mcu_flash_gui.py", "src/modules/launcher.py"):
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(ROOT / entry), run_name="__main__")
                self.assertEqual(stopped.exception.code, 1)

    def test_low_end_profiles_and_background_throttling(self):
        from src.modules import runtime_resources as resources
        for cores in (4, 6):
            profile = resources.performance_profile(cores, 8)
            self.assertTrue(profile.constrained)
            self.assertFalse(profile.low_memory)
            self.assertEqual(profile.syntax_interval_ms, 8000)
            self.assertEqual(profile.terminal_scrollback, 2000)
            with patch.object(resources, "performance_profile", return_value=profile), patch.dict(os.environ, {"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-background-timer-throttling --disable-gpu"}):
                resources.configure_webengine_environment()
                self.assertNotIn("--disable-background-timer-throttling", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])
                self.assertIn("--num-raster-threads=1", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])
        self.assertFalse(resources.performance_profile(8, 8).constrained)
        self.assertTrue(resources.performance_profile(8, 8, physical_cores=4).constrained)
        self.assertTrue(resources.performance_profile(12, 8, physical_cores=6).constrained)
        self.assertTrue(resources.performance_profile(12, 4).low_memory)

    def test_compiler_jobs_reserve_ui_and_limit_saved_values(self):
        from main.core import toolchain, storage_resources
        from main import web_bridge
        with patch.object(storage_resources, "storage_worker_limit", return_value=None):
            # Synthetic CPU previews must not inherit the runner's topology
            # when their logical count happens to match the host's count.
            self.assertEqual(toolchain._resource_safe_worker_count("MAX", 4, 8, physical_cpus=4), 2)
            self.assertEqual(toolchain._resource_safe_worker_count("MAX", 6, 8, physical_cpus=6), 4)
            self.assertEqual(toolchain._resource_safe_worker_count("MAX", 6, 0.4, physical_cpus=6), 1)
            self.assertEqual(toolchain._resource_safe_worker_count("MAX", 8, 8, physical_cpus=4), 2)
            self.assertEqual(toolchain._resource_safe_worker_count("MAX", 12, 8, physical_cpus=6), 4)
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        with patch.object(web_bridge, "load_gui_config", return_value={"compiler_jobs": 99}), patch.object(web_bridge, "get_optimal_compiler_jobs", return_value=2):
            self.assertEqual(api._get_jobs(), 2)

    def test_terminal_preserves_protocol_and_bounds_history(self):
        from src.modules import project_terminal as terminal
        data = "\x1b[?1;2c\x1b[3;8R\x1b]11;rgb:ffff/ffff/ffff\x1b\\\x1b[200~line1\rline2\x1b[201~\x03"
        self.assertEqual(terminal.sanitize_terminal_input(data), data)
        self.assertEqual(terminal.sanitize_terminal_input({}), "")
        session = terminal.ShellSession(None, "audit", "cmd")
        for _ in range(2500):
            session.append_history("x" * 4096)
        self.assertLessEqual(session.history_chars, session.history_limit)
        self.assertEqual(len(session.history_text()), session.history_chars)
        session.append_history("y" * (session.history_limit * 2))
        self.assertEqual(session.history_text(), "y" * session.history_limit)

    def test_terminal_honors_no_color_and_cleans_private_python(self):
        from src.modules import project_terminal as terminal
        with patch.dict(os.environ, {"NO_COLOR": "1", "FORCE_COLOR": "1", "PYTHONHOME": "private", "PYTHONPATH": "private"}):
            env = terminal._build_terminal_env(str(ROOT))
        self.assertNotIn("FORCE_COLOR", env)
        self.assertEqual(env["NO_COLOR"], "1")
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_terminal_resize_and_input_target_their_session(self):
        from src.modules import project_terminal as terminal
        class Socket:
            async def send(self, data): pass
            def __aiter__(self): return self.messages()
            async def messages(self):
                yield json.dumps({"type": "resize", "shell": "a", "rows": 45, "cols": 140})
                yield json.dumps({"type": "input", "shell": "a", "data": "\x1b[?1;2c"})
        server = terminal.ProjectTerminalServer(8765, str(ROOT), str(ROOT))
        from unittest.mock import Mock
        for sid in ("a", "b"):
            session = terminal.ShellSession(server, sid, "cmd")
            session.running, session.pty = True, Mock()
            server.sessions[sid] = session
        asyncio.run(server.websocket_handler(Socket()))
        server.sessions["a"].pty.setwinsize.assert_called_once_with(45, 140)
        server.sessions["a"].pty.write.assert_called_once_with("\x1b[?1;2c")
        server.sessions["b"].pty.setwinsize.assert_not_called()

    def test_terminal_close_during_spawn_cannot_orphan_pty(self):
        from src.modules import project_terminal as terminal
        from unittest.mock import Mock
        server = terminal.ProjectTerminalServer(8765, str(ROOT), str(ROOT))
        session = terminal.ShellSession(server, "a", "pwsh")
        session.running, session.generation = True, 1
        server.sessions["a"] = session
        pty = Mock()
        def spawn(*args, **kwargs):
            server._stop_session("a")
            return pty
        with patch.object(terminal, "PtyProcess", SimpleNamespace(spawn=spawn)), patch.object(terminal, "_native_shell_executable", return_value="shell"), patch.object(terminal, "_build_terminal_env", return_value={}):
            server._shell_worker(session, 1)
        self.assertIsNone(session.pty)
        pty.close.assert_called_once_with(force=True)
        pty.read.assert_not_called()

    def test_clear_never_types_into_a_running_cli(self):
        from src.modules import project_terminal as terminal
        from unittest.mock import Mock
        server = terminal.ProjectTerminalServer(8765, str(ROOT), str(ROOT))
        session = terminal.ShellSession(server, "audit", "pwsh")
        session.running, session.pty = True, Mock()
        session.append_history("Earlier output")
        server.sessions["audit"] = session
        self.assertTrue(server.control({"action": "clear", "shell": "audit"})["success"])
        session.pty.write.assert_not_called()
        self.assertEqual(session.history_text(), "")

    def test_terminal_controls_preserve_order_and_coalesce_resizes(self):
        from main.qt import terminal_panel
        from unittest.mock import Mock
        panel = terminal_panel.TerminalPanel()
        panel._port = 8765
        try:
            with patch.object(terminal_panel.threading, "Thread") as worker:
                panel._send_control("new", "audit", {"kind": "pwsh"})
                panel._send_control("select", "audit")
                for _ in range(100):
                    panel._send_control("fit")
                worker.assert_called_once()
            self.assertEqual(len(panel._control_queue), 3)
            delivered = []
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = b'{"success": true}'
            def receive(request, **kwargs):
                delivered.append(json.loads(request.data)["action"])
                return response
            with patch("urllib.request.urlopen", side_effect=receive):
                panel._drain_controls()
            self.assertEqual(delivered, ["new", "select", "fit"])
            self.assertFalse(panel._control_worker_running)
        finally:
            panel._stop_shell()
            panel.deleteLater()

    def test_terminal_output_waits_for_renderer_consumption(self):
        from src.modules import project_terminal as terminal
        class Socket:
            def __init__(self): self.sent = []
            async def send(self, data): self.sent.append(data)
        async def check():
            server = terminal.ProjectTerminalServer(8765, str(ROOT), str(ROOT))
            socket = Socket()
            server.clients.add(socket)
            server._pending_output[socket] = {"a": server._output_limit}
            server._output_events[socket] = asyncio.Event()
            task = asyncio.create_task(server._send(socket, {"type": "output", "shell": "a", "data": "😀"}))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertFalse(socket.sent)
            server._pending_output[socket]["a"] = 0
            server._output_events[socket].set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(server._pending_output[socket]["a"], 2)
            self.assertEqual(len(socket.sent), 1)
        asyncio.run(check())

    def test_unknown_board_is_rejected(self):
        from main.web_bridge import MCUWebBackendAPI
        api = MCUWebBackendAPI.__new__(MCUWebBackendAPI)
        api.current_board = "not-a-real-board-987654321"
        self.assertEqual(api._resolve_board_info(), {})
        self.assertTrue(target_problem(api._resolve_board_info()))

    def test_target_framework_and_injection(self):
        good = {"platform": "raspberrypi", "board": "pico", "framework": "arduino", "pio_resolved": True}
        self.assertFalse(target_problem(good, arduino_sketch=True))
        self.assertTrue(target_problem({**good, "pio_resolved": False}))
        self.assertTrue(target_problem({**good, "board": "pico\nextra = unsafe"}))
        self.assertTrue(target_problem({**good, "framework": "cmsis"}, arduino_sketch=True))
        self.assertFalse(target_problem({**good, "framework": "cmsis"}))

    def test_programmer_does_not_require_com(self):
        self.assertFalse(requires_upload_port({"platform": "ststm32", "upload_protocol": "stlink"}))
        self.assertTrue(requires_upload_port({"platform": "espressif32"}))
        self.assertTrue(requires_upload_port({"platform": "atmelavr"}))

    def test_recovery_is_bounded_and_expires(self):
        clock = [0.0]
        budget = RecoveryBudget(clock=lambda: clock[0])
        self.assertEqual([budget.next_delay() for _ in range(3)], [0.5, 1.5, 4.0])
        self.assertIsNone(budget.next_delay())
        for _ in range(100):
            self.assertIsNone(budget.next_delay())
        self.assertLessEqual(len(budget._failures), 4)
        clock[0] = 61.0
        self.assertEqual(budget.next_delay(), 0.5)

    def test_linux_symlinked_venv_identity(self):
        with patch.object(guard.sys, "platform", "linux"), patch.object(guard.sys, "prefix", str(guard.LINUX_ENV_DIR)), patch.object(guard.sys, "base_prefix", "/usr"), patch.object(guard.sys, "executable", "/usr/bin/python3"):
            self.assertTrue(guard.is_running_private_python())
        with patch.object(guard.sys, "platform", "linux"), patch.object(guard.sys, "prefix", "/usr"), patch.object(guard.sys, "base_prefix", "/usr"):
            self.assertFalse(guard.is_running_private_python())

    def test_host_detection(self):
        from src.modules import platform_runtime
        with patch.object(platform_runtime.platform, "system", return_value="Linux"), patch.object(platform_runtime.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "PRETTY_NAME": "Ubuntu"}, create=True):
            self.assertTrue(platform_runtime.host_info()["ubuntu"])
            self.assertFalse(platform_runtime.host_info()["windows"])

    def test_catalog_refresh_preserves_imported_reference(self):
        from main.core.board_catalog import BoardCatalog
        catalog = BoardCatalog({"old": {"board": "old"}})
        snapshot = catalog.items()
        catalog.replace({"new": {"board": "new"}})
        self.assertEqual(list(catalog), ["new"])
        self.assertEqual(snapshot[0][0], "old")

    def test_registry_schema_and_no_command_mutation(self):
        from main.core import board_catalog, toolchain
        command = [sys.executable, "-m", "platformio"]
        payload = [{"id": "future_board", "name": "Future board", "platform": "futureplatform", "frameworks": ["arduino"], "rom": 1048576}]
        import tempfile
        audit = ROOT / "temp/audit/runtime"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as fixture:
            (Path(fixture) / ".mcu-offline-catalog.json").write_text(json.dumps(payload))
            with patch.object(toolchain, "find_pio_executable", return_value=command), patch.object(board_catalog.subprocess, "run", side_effect=AssertionError("Runtime catalog must stay offline")), patch.object(board_catalog, "_get_safe_platformio_core_dir", return_value=fixture):
                self.assertEqual(board_catalog.load_registry_board_catalog()[0]["id"], "future_board")
        self.assertEqual(command, [sys.executable, "-m", "platformio"])

    def test_installed_manifest_refreshes_copied_cache(self):
        from main.core import board_catalog
        seed = {"Demo": {"board": "demo", "platform": "p", "framework": "arduino", "pio_manifest": "old.exe", "pio_resolved": False}}
        manifest = {"id": "demo", "name": "Demo", "platform": "p", "frameworks": {"arduino"}, "manifest": "/native/demo.json", "upload_protocol": "dfu"}
        with patch.object(board_catalog, "_get_arduino_board_search_roots", return_value=[]), patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[manifest]), patch.object(board_catalog, "_save_board_catalog_cache"):
            refreshed = board_catalog.load_dynamic_boards(seed)
        self.assertTrue(refreshed["Demo"]["pio_resolved"])
        self.assertEqual(refreshed["Demo"]["pio_manifest"], "/native/demo.json")
        self.assertEqual(refreshed["Demo"]["upload_protocol"], "dfu")
        self.assertEqual(seed["Demo"]["pio_manifest"], "old.exe")

    def test_build_preparation_failure_releases_busy_state(self):
        from main import web_bridge
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api.current_board, api._active_process = "demo", None
        events = []
        api.emit = lambda name, payload: events.append((name, payload))
        api._unmap_unc_after_build = lambda: None
        with patch.object(web_bridge, "_refresh_platformio_core_environment", side_effect=OSError("Store unavailable")):
            self.assertFalse(api._compile_worker())
        self.assertFalse(api.is_busy)
        self.assertIsNone(api.active_operation)
        self.assertTrue(any(name == "notification" and payload["type"] == "error" for name, payload in events))

    def test_missing_compiler_releases_operation_before_idle_signal(self):
        from main import web_bridge
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api.current_board, api._active_process = "demo", None
        api.sketch_dir_path = ROOT / "temp/audit/nonexistent-sketch"
        api._resolve_board_info = lambda name=None: {}
        api._effective_cache_root = lambda path: ROOT / "temp/audit/nonexistent-build"
        api._unmap_unc_after_build = lambda: None
        events = []
        api.emit = lambda event, data: events.append((event, data, api.is_busy, api.active_operation))
        with patch.object(web_bridge, "_refresh_platformio_core_environment", return_value=(ROOT / "temp/audit/nonexistent-runtime", False)), \
                patch.object(web_bridge, "find_pio_executable", return_value=None):
            self.assertFalse(api._compile_worker())
        idle = [entry for entry in events if entry[0] == "operation:phase" and entry[1]["phase"] == "idle"]
        self.assertTrue(idle)
        self.assertEqual(idle[-1][2:], (False, None))

    def test_upload_rejection_releases_operation_without_hardware_calls(self):
        from main import web_bridge
        for port, owner in (("", None), ("SIMULATED", "99999")):
            api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
            api.current_port = port
            api.is_busy, api.active_operation, api._current_op_phase = True, "upload", "compiling"
            events = []
            api.emit = lambda event, data: events.append((event, data, api.is_busy, api.active_operation))
            with patch.object(web_bridge, "port_occupied_owner", return_value=owner):
                api._upload_worker()
            idle = [entry for entry in events if entry[0] == "operation:phase" and entry[1]["phase"] == "idle"]
            self.assertTrue(idle)
            self.assertEqual(idle[-1][2:], (False, None))

    def test_native_upload_failure_is_not_replayed(self):
        from main import web_bridge
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api._active_port_label, api.current_port = "", ""
        api.sketch_dir_path = ROOT / "temp/audit/nonexistent-sketch"
        api._active_process = None
        api._stop_requested = False
        api.is_busy = True
        events = []
        api.emit = lambda name, payload: events.append((name, payload))
        api._effective_cache_root = lambda path: path
        api._board_workspace_dir = lambda: api.sketch_dir_path
        api._needs_recompile = lambda: (False, "isolated programmer fixture")
        api._generate_platformio_ini = lambda path: None
        api._unmap_unc_after_build = lambda: None
        api._get_jobs = lambda: 1
        process = SimpleNamespace(stdout=["Simulated programmer error"], wait=lambda: 2, poll=lambda: 2)
        with patch.object(web_bridge, "port_occupied_owner", return_value=None), patch.object(web_bridge, "find_pio_executable", return_value=["PIO_SIMULATED"]), patch.object(web_bridge, "_refresh_platformio_core_environment", return_value=(str(ROOT / "temp/audit/runtime"), False)), patch.object(web_bridge.subprocess, "Popen", return_value=process) as launch:
            api._native_upload_worker(can_skip=True)
        self.assertEqual(launch.call_count, 1)
        self.assertNotIn("--upload-port", launch.call_args.args[0])
        self.assertEqual(launch.call_args.kwargs["env"]["PLATFORMIO_BUILD_JOBS"], "1")
        self.assertEqual(launch.call_args.kwargs["env"]["SCONSFLAGS"], "-j1")
        self.assertFalse(api.is_busy)
        self.assertIsNone(api._active_process)
        self.assertTrue(any(name == "notification" and payload["type"] == "error" for name, payload in events))

    def test_package_installation_cannot_be_interrupted(self):
        from main.web_bridge import MCUWebBackendAPI
        api = MCUWebBackendAPI.__new__(MCUWebBackendAPI)
        api._framework_download_active = api.is_busy = True
        api._stop_requested = False
        events = []
        api.emit = lambda name, payload: events.append((name, payload))
        api.stop_operation()
        self.assertFalse(api._stop_requested)
        self.assertTrue(events)

    def test_ambiguous_board_match_is_rejected(self):
        from main.core import board_catalog
        candidates = [{"id": "a", "platform": "p"}, {"id": "b", "platform": "p"}]
        with patch.object(board_catalog, "_score_arduino_to_pio_board", return_value=(150, ["name"])):
            self.assertIsNone(board_catalog._resolve_arduino_board_record({}, candidates))

    def test_framework_changes_firmware_identity(self):
        from main.web_bridge import MCUWebBackendAPI
        api = MCUWebBackendAPI.__new__(MCUWebBackendAPI)
        api.sketch_dir_path = ROOT / "temp/audit/nonexistent-sketch"
        api.current_board = "demo"
        with patch.object(api, "_resolve_board_info", return_value={"platform": "ststm32", "board": "demo", "framework": "arduino"}):
            arduino = api._hash_sources()
        with patch.object(api, "_resolve_board_info", return_value={"platform": "ststm32", "board": "demo", "framework": "cmsis"}):
            self.assertNotEqual(arduino, api._hash_sources())

    def test_skip_compile_never_reuses_changed_sources(self):
        from main.web_bridge import MCUWebBackendAPI
        api = MCUWebBackendAPI.__new__(MCUWebBackendAPI)
        api.current_board, api.sketch_dir_path = "demo", ROOT / "temp/audit/nonexistent-sketch"
        api.skip_compile = True
        with patch.object(api, "_has_prior_build", return_value=True), patch.object(api, "_resolve_board_info", return_value={"platform": "raspberrypi"}), patch.object(api, "_needs_recompile", return_value=(True, "Source changed")):
            self.assertFalse(api.check_can_skip_compile_for_upload())
        with patch.object(api, "_has_prior_build", return_value=True), patch.object(api, "_resolve_board_info", return_value={"platform": "raspberrypi"}), patch.object(api, "_needs_recompile", return_value=(False, "Match")):
            self.assertTrue(api.check_can_skip_compile_for_upload())

    def test_vector_controls_have_readable_labels(self):
        from main.qt.icons import ActionButton
        button = ActionButton("⚡ Upload")
        button.setObjectName("btn-upload")
        button.setText("⚡ Uploading...")
        self.assertEqual(button.text(), "Uploading...")
        self.assertFalse(button.icon().isNull())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux file lock check")
    def test_linux_reset_lock_excludes_another_process(self):
        import subprocess
        from main.core.config import _try_acquire_reset_cache_lock, _release_reset_cache_lock
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(ROOT / "temp/audit/linux-lock")}):
            handle = _try_acquire_reset_cache_lock()
            self.assertIsNotNone(handle)
            try:
                result = subprocess.run([sys.executable, "-B", "-c", "from main.core.config import _try_acquire_reset_cache_lock; assert _try_acquire_reset_cache_lock() is None"], cwd=ROOT, timeout=10)
                self.assertEqual(result.returncode, 0)
            finally:
                _release_reset_cache_lock(handle)

    def test_disconnected_port_clears_claim_before_catalog_reaches_controls(self):
        from main import web_bridge
        from unittest.mock import Mock
        for persisted in (True, False):
            api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
            api.current_port, api.current_baud = "SIMULATED", 115200
            api.is_busy = False
            api._last_known_ports = [{"device": "SIMULATED"}]
            api._stop_port_monitor = threading.Event()
            api._init_hardware = Mock()
            api._scan_ports = Mock(return_value=[])
            api._stop_serial_monitor = Mock()
            api._sync_project_hardware_state = Mock()
            events = []
            def emit(event, data):
                events.append((event, data, api.current_port))
                if event == "ports:updated":
                    api._stop_port_monitor.set()
            api.emit = emit
            with patch.object(api._stop_port_monitor, "wait", return_value=False), \
                    patch.object(web_bridge, "claim_serial_port", return_value=persisted) as claim:
                api._port_monitor_loop()
            claim.assert_called_once_with("")
            api._stop_serial_monitor.assert_called_once()
            names = [event for event, _, _ in events]
            self.assertLess(names.index("port:selected"), names.index("ports:updated"))
            self.assertEqual(events[-1][2], "")
            if not persisted:
                self.assertTrue(any(data.get("title") == "Port settings unavailable" for _, data, _ in events if isinstance(data, dict)))

    def test_old_serial_reader_cannot_disconnect_new_reader(self):
        from main import web_bridge

        class Port:
            in_waiting = 0
            is_open = False
            def __init__(self):
                self.wake = threading.Event()
            def open(self):
                self.is_open = True
            def read(self, count):
                self.wake.wait(0.01)
                return b""
            def close(self):
                self.is_open = False
                self.wake.set()

        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api._serial_lock = threading.Lock()
        api._serial_generation = 0
        api._serial_conn = api._serial_thread = None
        api.current_port, api.current_baud = "SIMULATED", 115200
        api.is_busy, api.active_operation = False, None
        api._stop_port_monitor = threading.Event()
        api._serial_recovery_budget = RecoveryBudget()
        events = []
        api.emit = lambda event, data: events.append((event, data))
        with patch.object(web_bridge.serial, "Serial", side_effect=[Port(), Port()]):
            api._start_serial_monitor()
            old = api._serial_thread
            api._start_serial_monitor()
            old.join(1)
            self.assertFalse(old.is_alive())
            self.assertTrue(api.serial_running)
            self.assertTrue([p for e, p in events if e == "serial:status"][-1]["connected"])
            api._stop_serial_monitor()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux PTY check")
    def test_native_pty_roundtrip(self):
        from main.qt.posix_terminal_panel import PtySession
        session = PtySession(str(ROOT), ["/bin/sh", "-c", "printf MCU_PTY_OK"])
        output = []
        session.output.connect(output.append)
        self.assertTrue(session.start())
        limit = time.monotonic() + 3
        while session.process and time.monotonic() < limit:
            session._drain()
            time.sleep(0.02)
        session.close()
        self.assertIn("MCU_PTY_OK", "".join(output))


class PreviewBackend:
    """Simulated data only; no file writes, workers, hardware or subprocesses."""
    def __init__(self):
        self.sketch_dir_path = ROOT / "temp/scratch/preview-project"
        self.active_file_path = str(self.sketch_dir_path / "preview.ino")
        self.current_board = self.current_port = ""
        self.current_baud = 115200
        self.upload_speed = "460800"
        self.is_busy = self.serial_running = self.serial_paused = False
        self.active_operation = self._current_op_phase = self._active_reset_kind = None
        self._active_process = self.ai_review_manager = self.ai_watcher = None
        self._operation_worker = None
        self._op_session_id = 0
        self._framework_download_active = False
        self.modified_files = {}
        self.timestamp_enabled = self.skip_compile = self.reset_on_baud_change = False
        self.clear_console_on_action = True
        self.clear_serial_on_action = False

    def get_project_files(self):
        return [
            {"name": "preview.ino", "path": str(self.sketch_dir_path / "preview.ino"), "extension": ".ino", "is_main": True},
            {"name": "preview_config.h", "path": str(self.sketch_dir_path / "preview_config.h"), "extension": ".h", "is_main": False},
        ]

    def get_project_dir(self):
        return str(self.sketch_dir_path)

    def read_file(self, path):
        if str(path).endswith(".h"):
            return {"success": True, "content": "#pragma once\nconstexpr int SENSOR_INTERVAL_MS = 1000;\n"}
        return {"success": True, "content": "// Sensor console — choose your board before compiling.\n#include <Arduino.h>\n\nvoid setup() {\n  Serial.begin(115200);\n}\n\nvoid loop() {\n  Serial.println(\"MCU Flasher ready\");\n  delay(1000);\n}\n"}

    def _resolve_board_info(self, name=None):
        from main.core.board_catalog import SUPPORTED_BOARDS
        return SUPPORTED_BOARDS.get(name or self.current_board, {})

    def get_settings(self):
        return {}

    def get_recent_boards(self):
        return []

    def _scan_ports(self):
        return []

    def check_can_skip_compile(self, *args):
        return False

    def realtime_check_syntax(self, *args):
        return "[]"

    def set_active_file(self, path):
        self.active_file_path = path

    def mark_modified(self, path, dirty=True):
        self.modified_files[path] = dirty

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def qt_smoke(app, render_dir=None):
    from main.qt.garbage_collection import install_gui_garbage_collector
    collector = install_gui_garbage_collector(app)
    from PySide6.QtCore import QTimer, Qt, QRect
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QWidget, QComboBox, QLabel
    from main.core import config, board_catalog, file_utils
    from main.qt.main_window import MCUMainWindow
    from main.qt.board_dialog import BoardSearchDialog
    from main.qt.editor_panel import EditorBridgeAPI
    from main.qt.signals import signals
    from src.modules.ui_metrics import WorkArea

    backend = PreviewBackend()
    from unittest.mock import Mock
    backend.start_services = Mock()
    backend._scan_ports = Mock(side_effect=AssertionError("Port scan on GUI thread"))
    bridge = EditorBridgeAPI(backend)
    bridge.snapshot_buffer(backend.active_file_path, "unsaved text")
    backend.mark_modified(backend.active_file_path)
    bridge.begin_buffer_recovery()
    bridge.mark_modified(backend.active_file_path, False)
    assert bridge.get_recovery_buffers()[backend.active_file_path] == "unsaved text"
    bridge.recovery_complete()
    assert bridge.get_recovery_buffers() == {}
    backend.modified_files.clear()
    fixture = {"Demo target": {"platform": "espressif32", "board": "esp32dev", "framework": "arduino", "frameworks": ["arduino"], "pio_resolved": True}}
    original = dict(board_catalog.SUPPORTED_BOARDS)
    board_catalog.SUPPORTED_BOARDS.replace(fixture)
    # This fixture deliberately exercises wide and compact layouts even when
    # an offscreen CI platform advertises only an 800-pixel virtual monitor.
    with patch("main.qt.responsive.work_area", return_value=WorkArea(0, 0, 1940, 1080)), patch("main.qt.main_window.work_area", return_value=WorkArea(0, 0, 1940, 1080)), patch.object(file_utils, "hide_internal_project_metadata"), patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}), patch.object(config, "_save_raw_config"), patch.object(config, "save_gui_config"), patch.object(config, "get_theme_mode", return_value="default"), patch.object(config, "focus_project_window", return_value=False):
        window = MCUMainWindow(backend)
        backend.start_services.assert_not_called()
        backend._scan_ports.assert_not_called()
        window._primary_toolbar.update_sketch_label(str(backend.sketch_dir_path))
        window.resize(1920, 900)
        window.show()
        assert not window._primary_toolbar.btn_upload.isEnabled()
        assert not window._primary_toolbar.btn_compile.isEnabled()
        backend.current_board = "Demo target"
        signals.board_selected.emit({"board_name": "Demo target"})
        window._primary_toolbar._update_action_button_states()
        assert window._primary_toolbar.btn_compile.isEnabled()
        assert not window._primary_toolbar.btn_upload.isEnabled()
        signals.console_log.emit({"text": "Choose the exact board model. Compilation can run while serial monitoring stays active.", "tag": "info", "newline": True})

        capture_errors = []
        finished = [False]

        def verify_window_ownership(extra=()):
            backend.start_services.assert_called_once()
            visible = [widget for widget in app.topLevelWidgets() if widget.isVisible()]
            unexpected = [widget for widget in visible if widget not in (window, *extra)]
            assert not unexpected, f"Unexpected desktop windows: {[(type(w).__name__, w.objectName()) for w in unexpected]}"
            assert window.findChild(QWidget, "workflow-bar") is None
            assert window._primary_toolbar.window() is window
            assert window._controls_bar.window() is window

        def verify_tool_tabs():
            tabs = window._bottom_tabs.tabBar()
            tabs.setFocus()
            QTest.keyClick(tabs, Qt.Key.Key_Right)
            assert window._bottom_tabs.currentIndex() == 1
            QTest.keyClick(tabs, Qt.Key.Key_Left)
            assert window._bottom_tabs.currentIndex() == 0

        def finish(error=None):
            if finished[0]:
                return
            finished[0] = True
            if error:
                capture_errors.append(error)
            backend.modified_files.clear()
            window.close()
            board_catalog.SUPPORTED_BOARDS.replace(original)
            app.quit()

        def checked(callback):
            def run(*args):
                if finished[0]:
                    return
                try:
                    callback(*args)
                except Exception as exc:
                    finish(exc)
            return run

        page = window._editor_panel._view.page()
        unsaved = "// Unsaved renderer recovery sentinel\nvoid setup() {}\nvoid loop() {}\n"

        @checked
        def verify_restored(value):
            assert value == unsaved, f"Renderer reload lost a dirty buffer: {value!r}"
            assert backend.modified_files.get(backend.active_file_path), "Restored buffer must remain dirty"
            assert not window._editor_panel.bridge._recovering_buffers
            verify_window_ownership()
            collector._last_full -= 61
            collector.collect_pending()
            print("Qt tabs, keyboard navigation, compact/wide ownership, detach/reattach, action gating and dirty renderer recovery: OK")
            finish()

        @checked
        def read_restored():
            page.runJavaScript("window.editorInstance?.getValue()", verify_restored)

        @checked
        def reload_renderer():
            bridge = window._editor_panel.bridge
            assert bridge._buffer_snapshots.get(backend.active_file_path) == unsaved, f"Snapshot mismatch: active={backend.active_file_path!r}, snapshots={bridge._buffer_snapshots!r}"
            # Only explicit detachment may create a second workspace window.
            # Closing it must keep the same WebEngine page and dirty buffer.
            window.detach_editor()
            verify_window_ownership((window._detached_window,))
            assert window._detached_window.centralWidget() is window._editor_panel
            window._detached_window.close()
            assert not window._editor_detached
            assert window._editor_area.currentWidget() is window._editor_panel
            assert window._editor_panel._view.page() is page
            verify_window_ownership()
            window._editor_panel._manual_editor_reload()
            QTimer.singleShot(3000, read_restored)

        @checked
        def edit_loaded(value):
            assert value, "Offline Monaco model did not load"
            QTimer.singleShot(250, reload_renderer)

        @checked
        def capture_light_and_edit():
            verify_window_ownership()
            if render_dir:
                assert window.grab().save(str(render_dir / "glass-light.png"))
            window._on_theme_changed("default")
            profile_check = "true"
            if window._editor_panel._performance.constrained:
                profile_check = "document.documentElement.classList.contains('resource-constrained') && window.editorInstance.getRawOptions().minimap.enabled === false && window.editorInstance.getRawOptions().cursorBlinking === 'solid'"
            page.runJavaScript("Boolean(window.editorInstance?.getModel() && (" + profile_check + ") && (window.editorInstance.setValue(" + json.dumps(unsaved) + "), true))", edit_loaded)

        @checked
        def capture_compact():
            verify_window_ownership()
            verify_tool_tabs()
            if render_dir:
                assert window.grab().save(str(render_dir / "glass-compact.png"))
            toolbar = window._primary_toolbar
            assert toolbar.is_compact()
            toolbar._toggle_actions_menu()
            popup = toolbar._actions_popup
            assert popup.isVisible() and popup.parentWidget() is toolbar
            verify_window_ownership((popup,))
            window.resize(1920, 900)
            assert not popup.isVisible(), "The compact Actions popup must close when the toolbar expands"
            window._on_theme_changed("light")
            QTimer.singleShot(500, capture_light_and_edit)

        @checked
        def capture():
            verify_window_ownership()
            verify_tool_tabs()
            from main.qt.settings_dialog import SettingsDialog
            from PySide6.QtWidgets import QMessageBox
            settings = SettingsDialog(backend, window)
            assert settings.findChild(QWidget, "editor-engine-label").text() == "Offline Monaco"
            assert not hasattr(settings, "editor_combo"), "Settings must not offer an unused editor engine"
            with patch.object(settings, "_save_and_apply") as save, patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                settings.theme_combo.setCurrentText(settings._theme_rev["light"])
                settings._reset_defaults()
                assert settings._theme_map[settings.theme_combo.currentText()] == "default"
                save.assert_called_once()
                settings._last_manual_theme = "unknown-theme"
                settings._on_theme_system_toggled(False)
                assert settings._theme_map[settings.theme_combo.currentText()] == "default"
            settings.close()
            if render_dir:
                render_dir.mkdir(parents=True, exist_ok=True)
                assert window.grab().save(str(render_dir / "glass-dark.png"))
            dialog = BoardSearchDialog(window, board_list=["Demo target"], current_board="Demo target")
            for _ in range(200):
                if not dialog._search_pending:
                    break
                QTest.qWait(10)
            assert not dialog._search_pending, "Board search worker did not finish"
            assert not dialog.findChildren(QComboBox), "Board picker must not contain a framework dropdown"
            assert not any(label.text() == "Framework" for label in dialog.findChildren(QLabel))
            if render_dir:
                dialog.show()
                app.processEvents()
                assert dialog.grab().save(str(render_dir / "glass-board-picker.png"))
            dialog.close()
            page.runJavaScript(r"""(() => {
                const tabs = Array.from(document.querySelectorAll('#tab-bar .tab'));
                if (tabs.length !== 2 || tabs.some(t => t.getAttribute('role') !== 'tab')) return false;
                const loadedModels = window.monaco.editor.getModels();
                // The bundle's initial empty editor owns one placeholder model.
                if (loadedModels.length > tabs.length + 1) return 'Duplicate project models: ' + loadedModels.length;
                if (tabs.some(t => t._filePath?.split(/[\\/]/).pop() !== t.querySelector('span:first-child')?.textContent)) return false;
                if (window.projectPathKey('/home/user/Config.h') === window.projectPathKey('/home/user/config.h')) return false;
                if (window.projectPathKey('C:/Sketch/Config.h') !== window.projectPathKey('c:/sketch/config.h')) return false;
                const bar = document.getElementById('tab-bar');
                const probes = ['/home/probe/Config.h', '/home/probe/config.h'].map((path, i) => {
                    const tab = document.createElement('div');
                    tab.className = 'tab'; tab._filePath = path; tab.id = 'case-probe-' + i;
                    bar.appendChild(tab); return tab;
                });
                window.dedupeProjectTabs();
                const casePreserved = probes.every(tab => tab.parentElement === bar);
                probes.forEach(tab => tab.remove());
                if (!casePreserved) return false;
                const first = tabs[0], last = tabs[1];
                first.focus();
                first.dispatchEvent(new KeyboardEvent('keydown', {key:'End', bubbles:true}));
                if (!last.classList.contains('active') || document.activeElement !== last) return false;
                last.dispatchEvent(new KeyboardEvent('keydown', {key:'Home', bubbles:true}));
                if (!first.classList.contains('active') || document.activeElement !== first) return false;
                // A drag from outside the tab row must not try to insert null.
                let error = false;
                const trap = () => { error = true; };
                window.addEventListener('error', trap);
                first.dispatchEvent(new Event('dragover', {bubbles:true, cancelable:true}));
                window.removeEventListener('error', trap);
                return !error;
            })()""", check_source_tabs)

        @checked
        def check_source_tabs(value):
            assert value is True, f"Source-tab keyboard navigation, accessibility or external drag handling failed: {value!r}"
            # Simulate a small logical work area, such as a scaled laptop screen.
            from PySide6.QtCore import QRect
            with patch("main.qt.main_window.work_area", return_value=WorkArea(0, 0, 1280, 480)):
                window._update_minimum_window_size()
            window.resize(640, 432)
            QTimer.singleShot(250, capture_small)

        @checked
        def capture_small():
            assert window.width() <= 640 and window.height() <= 432, f"Compact workspace exceeded work area: {window.size()}"
            assert window._editor_area.geometry().bottom() < window._bottom_tabs.geometry().top(), "Editor covered the tool tabs"
            window._bottom_tabs.setCurrentWidget(window._serial_panel)
            QTimer.singleShot(180, capture_small_serial)

        def verify_small_tool_navigation():
            tabs = window._bottom_tabs.tabBar()
            assert tabs.visibleRegion().contains(tabs.rect().center()), "Tool navigation clipped"
            local = window._editor_panel.mapFrom(tabs, tabs.rect().center())
            assert not window._editor_panel.rect().contains(local), "Editor covered tool navigation"

        @checked
        def capture_small_serial():
            verify_small_tool_navigation()
            assert window._editor_area.geometry().bottom() < window._bottom_tabs.geometry().top()
            serial = window._serial_panel
            assert serial._header.geometry().bottom() < serial.height(), "Serial header clipped"
            send = serial.findChild(QWidget, "serial-send-bar")
            assert send.geometry().bottom() < serial.height(), "Serial send controls clipped"
            if render_dir:
                assert window.grab().save(str(render_dir / "glass-small-serial.png"))
            window._bottom_tabs.setCurrentWidget(window._console_container)
            QTimer.singleShot(180, capture_small_console)

        @checked
        def capture_small_console():
            verify_small_tool_navigation()
            console = window._console_container
            assert console.header.geometry().bottom() < console.height(), "Build controls clipped"
            assert console.console.geometry().bottom() < console.height(), "Build output clipped"
            verify_window_ownership()
            if render_dir:
                assert window.grab().save(str(render_dir / "glass-small.png"))
            check_small_panel(2)

        def check_small_panel(index):
            # Exercise panel layout without opening a shell or running commands.
            with patch.object(window._terminal_panel, "_on_tab_revealed"), patch.object(window._terminal_panel, "add_session"):
                window._bottom_tabs.setCurrentIndex(index)
            QTimer.singleShot(160, checked(lambda: verify_small_panel(index)))

        def verify_small_panel(index):
            from PySide6.QtWidgets import QAbstractButton
            verify_small_tool_navigation()
            panel = window._bottom_tabs.currentWidget()
            for button in panel.findChildren(QAbstractButton):
                if button.isVisible():
                    assert button.visibleRegion().contains(button.rect().center()), f"Panel {index} control clipped: {button.text()}"
                    position = button.mapTo(panel, button.rect().topLeft())
                    assert panel.rect().contains(QRect(position, button.size())), f"Panel {index} control outside pane: {button.text()}"
            if render_dir:
                assert window.grab().save(str(render_dir / f"glass-small-tool-{index}.png"))
            if index < 5:
                check_small_panel(index + 1)
                return
            window._bottom_tabs.setCurrentIndex(0)
            window._update_minimum_window_size()
            window.resize(900, 700)
            QTimer.singleShot(250, capture_compact)

        QTimer.singleShot(2500, capture)
        QTimer.singleShot(14000, lambda: finish(AssertionError("Qt verification timed out")))
        app.exec()
        if capture_errors:
            raise capture_errors[0]



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--preview-cpus", type=int, choices=(4, 6), help="Simulate the low-end CPU policy for the real Qt preview")
    args = parser.parse_args()
    from PySide6.QtWidgets import QApplication
    app = QApplication(["MCU runtime verification"])
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    if args.preview_cpus:
        from src.modules import runtime_resources
        with patch.object(runtime_resources.os, "cpu_count", return_value=args.preview_cpus):
            runtime_resources.configure_webengine_environment()
            qt_smoke(app, args.render_dir)
        print(f"Real Qt/Monaco constrained profile for {args.preview_cpus} logical CPUs: OK")
    else:
        qt_smoke(app, args.render_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
