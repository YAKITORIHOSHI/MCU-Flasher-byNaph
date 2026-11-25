#!/usr/bin/env python3
"""Verify the AI Assistant's list, detection and downloads with owned fixtures.

No agent, installer, account or network is used: programs are empty fixture files,
installers are small local scripts and Node.js archives are built here.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from main.core import ai_agents
from main.core.log_colors import contrast_ratio
from main.core.theme import glass_worst
from main.qt.agent_chooser import AgentChooser, BUILTIN_TEXT, DOWNLOADED_TEXT
from main.qt.agent_picker import AgentPicker
from src.modules import agent_setup

APP = QApplication.instance() or QApplication([])
from main.qt.theme import register_fonts
register_fonts()


def until(condition, message, seconds=8):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        APP.processEvents()
        time.sleep(.005)


class Fixture(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "temp/audit/ai-agents"
        parent.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "data"
        for target, value in (("app_data_dir", lambda: self.data), ("_posix_directories", lambda: [self.home / ".local/bin"])):
            stub = patch.object(ai_agents, target, value)
            stub.start()
            self.addCleanup(stub.stop)
        environment = patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.root / "path")})
        environment.start()
        self.addCleanup(environment.stop)

    def program(self, path, text="#!/bin/sh\nexit 0\n"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path


class RegistryChecks(unittest.TestCase):
    def test_the_list_is_the_requested_names_with_opencode_built_in(self):
        self.assertEqual([agent.name for agent in ai_agents.AGENTS], ["Gemini", "Codex", "Claude", "Hermes", "OpenCode"])
        for agent in ai_agents.AGENTS:
            self.assertNotIn("CLI", agent.name)
            self.assertEqual(agent.builtin, agent.id == "opencode")
        self.assertEqual(ai_agents.DEFAULT_AGENT, "opencode")
        self.assertEqual(ai_agents.agent("no such").id, "opencode")

    def test_download_sources_are_the_makers_own_over_https(self):
        hosts = {"codex": "chatgpt.com", "claude": "claude.ai", "hermes": "hermes-agent.nousresearch.com"}
        for platform in ("linux", "darwin", "win32"):
            with patch.object(ai_agents.sys, "platform", platform):
                self.assertIsNone(ai_agents.install_plan("opencode"), "Setup provides the built-in assistant")
                for agent_id, host in hosts.items():
                    plan = ai_agents.install_plan(agent_id)
                    self.assertTrue(plan.url.startswith(f"https://{host}/"), plan)
                    self.assertTrue(plan.url.endswith(".ps1" if platform == "win32" else ".sh"))
                    self.assertIn(host, plan.source)
                    self.assertNotIn("sudo", plan.command)
                self.assertEqual(ai_agents.install_plan("gemini").package, "@google/gemini-cli")

    def test_only_opencode_takes_the_project_as_an_argument(self):
        self.assertEqual(ai_agents.agent_argv("opencode", "/x/opencode", "/a sketch"), ["/x/opencode", "/a sketch"])
        for agent_id in ("gemini", "codex", "claude", "hermes"):
            self.assertEqual(ai_agents.agent_argv(agent_id, "/x/tool", "/a sketch"), ["/x/tool"])
        with self.assertRaises(ValueError):
            ai_agents.agent_argv("claude", "", "/x")


@unittest.skipIf(sys.platform == "win32", "POSIX files")
class DetectionChecks(Fixture):
    def test_files_decide_and_nothing_is_run(self):
        self.program(self.home / ".local/bin/claude")
        self.program(self.root / "path/codex")
        with patch("subprocess.Popen", side_effect=AssertionError("Detection must not start anything")), \
                patch("subprocess.run", side_effect=AssertionError("Detection must not start anything")):
            self.assertEqual(ai_agents.find_agent("claude"), str(self.home / ".local/bin/claude"))
            self.assertEqual(ai_agents.find_agent("codex"), str(self.root / "path/codex"))
            self.assertIsNone(ai_agents.find_agent("gemini"))

    def test_another_program_of_the_same_name_is_not_the_agent(self):
        self.program(self.root / "path/hermes", "#!/bin/sh\n# a JavaScript engine\n")
        self.program(self.root / "path/gemini", "#!/usr/bin/env python3\n# a Gemini protocol browser\n")
        self.assertIsNone(ai_agents.find_agent("hermes"))
        self.assertIsNone(ai_agents.find_agent("gemini"))
        self.program(self.home / ".local/bin/hermes", '#!/bin/sh\nexec "$HOME/.hermes/hermes-agent/run" "$@"\n')
        self.assertEqual(ai_agents.find_agent("hermes"), str(self.home / ".local/bin/hermes"))
        package = self.program(self.data / "agents/gemini/lib/node_modules/@google/gemini-cli/dist/index.js", "#!/usr/bin/env node\n")
        link = self.data / "agents/gemini/bin/gemini"
        link.parent.mkdir(parents=True)
        link.symlink_to(package)
        self.assertEqual(ai_agents.find_agent("gemini"), str(link), "The app's own download comes first")

    def test_windows_programs_and_unfinished_files_do_not_count(self):
        self.program(self.home / ".local/bin/claude", "MZ copied from Windows")
        self.assertIsNone(ai_agents.find_agent("claude"))
        path = self.program(self.home / ".local/bin/codex")
        path.chmod(0o644)
        self.assertIsNone(ai_agents.find_agent("codex"))

    def test_private_node_reaches_only_agents_the_app_downloaded(self):
        for version in ("v9.0.0", "v26.13.1", "v10.0.0"):
            self.program(self.data / f"agents/node/{version}/bin/node")
        self.assertEqual(ai_agents.node_directory(), self.data / "agents/node/v26.13.1/bin")
        self.assertNotIn(str(self.data), ai_agents.agent_environment("claude").get("PATH", ""))
        (self.data / "agents/gemini").mkdir()
        path = ai_agents.agent_environment("gemini")["PATH"].split(os.pathsep)
        self.assertEqual(path[0], str(self.data / "agents/node/v26.13.1/bin"))
        self.assertIn(str(self.root / "path"), path)
        self.assertEqual(ai_agents.agent_environment("opencode")["OPENCODE_DISABLE_AUTOUPDATE"], "1")


class WindowsDetectionChecks(Fixture):
    def test_npm_shims_executables_and_placeholders(self):
        npm = self.root / "AppData/npm"
        real = npm / "node_modules/@openai/codex/bin/codex.js"
        self.program(real, "// fixture")
        shim = self.program(npm / "codex.cmd", '@"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
        with patch.object(ai_agents, "_windows_directories", return_value=[npm]), patch.object(ai_agents.sys, "platform", "win32"), \
                patch.object(ai_agents.shutil, "which", return_value=None), patch.object(ai_agents.Path, "home", return_value=self.home):
            self.assertEqual(ai_agents.find_agent("codex"), str(shim))
            real.unlink()
            self.assertIsNone(ai_agents.find_agent("codex"), "A shim without its package is not a download")
            (npm / "codex.cmd").unlink()
            (npm / "codex.exe").write_bytes(b"MZ" + bytes(2048))
            self.assertEqual(ai_agents.find_agent("codex"), str(npm / "codex.exe"))
            (npm / "codex.exe").write_bytes(b"placeholder")
            self.assertIsNone(ai_agents.find_agent("codex"))


class DownloadChecks(Fixture):
    def opener(self, data, url="https://claude.ai/install.sh"):
        stream = io.BytesIO(data)
        response = SimpleNamespace(read=stream.read, geturl=lambda: url, __enter__=None)

        class Context:
            def __enter__(self_inner):
                return response

            def __exit__(self_inner, *_):
                return False
        return lambda *_args, **_options: Context()

    def test_only_complete_scripts_over_https_are_accepted(self):
        script = b"#!/bin/sh\n" + b"# fixture\n" * 20
        self.assertEqual(agent_setup.fetch_script("https://claude.ai/install.sh", opener=self.opener(script)), script)
        for data, url, error in (
            (b"<!doctype html>" + b"x" * 200, "https://claude.ai/install.sh", "page instead"),
            (b"echo nothing to see" + b"x" * 200, "https://claude.ai/install.sh", "not an installer script"),
            (b"#!/bin/sh", "https://claude.ai/install.sh", "unexpected size"),
            (script, "http://claude.ai/install.sh", "redirected away"),
            (b"#!" + b"x" * (agent_setup.MAX_SCRIPT + 1), "https://claude.ai/install.sh", "unexpected size"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(agent_setup.AgentSetupError, error):
                agent_setup.fetch_script("https://claude.ai/install.sh", opener=self.opener(data, url))
        with self.assertRaisesRegex(agent_setup.AgentSetupError, "HTTPS"):
            agent_setup.fetch_script("http://claude.ai/install.sh", opener=self.opener(script))

    def test_progress_lines_are_plain_text(self):
        self.assertEqual(agent_setup.clean_line("\x1b[32m10%\rDownloading 20%\x1b[0m\r\n"), "Downloading 20%")
        self.assertEqual(agent_setup.clean_line("\x1b]0;title\x07done\n"), "done")
        self.assertEqual(agent_setup.clean_line("\x1b[2K\n"), "")

    @unittest.skipIf(sys.platform == "win32", "POSIX shells")
    def test_the_makers_script_runs_for_this_account_and_is_verified_by_its_files(self):
        lines = []
        target = self.home / ".local/bin/claude"
        script = (f"#!/bin/sh\necho Installing fixture\n[ -z \"$PYTHONPATH\" ] || exit 9\nmkdir -p '{target.parent}'\n"
                  f"printf '#!/bin/sh\\n' > '{target}'\nchmod 755 '{target}'\n" + "# padding\n" * 10).encode()
        with patch.dict(os.environ, {"PATH": os.defpath, "PYTHONPATH": "/wrong"}):
            found = agent_setup.install("claude", on_line=lines.append, fetch=lambda *_a, **_k: script)
        self.assertEqual(found, str(target))
        self.assertIn("claude.ai", lines[0])
        self.assertIn("Installing fixture", lines)
        with patch.object(agent_setup, "fetch_script", side_effect=AssertionError("Already here")):
            self.assertEqual(agent_setup.install("claude"), str(target))

    @unittest.skipIf(sys.platform == "win32", "POSIX shells")
    def test_failures_say_why_and_leave_no_script_behind(self):
        before = set(Path(tempfile.gettempdir()).glob("mcu-agent-*"))
        script = b"#!/bin/sh\necho working\necho 'Error: disk is full'\nexit 3\n" + b"# padding\n" * 10
        with patch.dict(os.environ, {"PATH": os.defpath}):
            with self.assertRaisesRegex(agent_setup.AgentSetupError, r"exit 3.*disk is full"):
                agent_setup.install("codex", fetch=lambda *_a, **_k: script)
            with self.assertRaisesRegex(agent_setup.AgentSetupError, "was not found"):
                agent_setup.install("codex", fetch=lambda *_a, **_k: b"#!/bin/sh\necho done\n" + b"# padding\n" * 10)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("mcu-agent-*")), before)
        with self.assertRaisesRegex(agent_setup.AgentSetupError, "repair"):
            agent_setup.install("opencode")

    @unittest.skipIf(sys.platform == "win32", "POSIX process groups")
    def test_cancelling_ends_the_installer_and_everything_it_started(self):
        marker = self.root / "child.pid"
        script = (f"#!/bin/sh\nsleep 600 &\necho $! > '{marker}'\necho started\nwait\n" + "# padding\n" * 10).encode()
        stop, errors = threading.Event(), []

        def work():
            try:
                agent_setup.install("claude", on_line=lambda text: text == "started" and stop.set(),
                                    cancelled=stop.is_set, fetch=lambda *_a, **_k: script)
            except agent_setup.AgentSetupError as exc:
                errors.append(exc)
        with patch.dict(os.environ, {"PATH": os.defpath}):
            worker = threading.Thread(target=work)
            worker.start()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], agent_setup.Cancelled)
        child = int(marker.read_text())
        until(lambda: not Path(f"/proc/{child}").exists() if Path("/proc").is_dir() else True, "the installer's child survived")

    def node_archive(self, name, files):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:xz") as bundle:
            for path, text in files.items():
                info = tarfile.TarInfo(f"{name}/{path}")
                info.size, info.mode = len(text), 0o755
                bundle.addfile(info, io.BytesIO(text))
        return stream.getvalue()

    @unittest.skipIf(sys.platform == "win32", "POSIX archive")
    def test_npm_agents_use_a_verified_private_node_when_the_system_has_none(self):
        version = "v26.13.1"
        with patch.object(agent_setup.sys, "platform", "linux"):
            archive = agent_setup.node_archive_name(version)
        name = archive.removesuffix(".tar.xz")
        payload = self.node_archive(name, {"bin/node": b"#!/bin/sh\nexit 0\n", "lib/node_modules/npm/bin/npm-cli.js": b"// npm"})
        listing = f"{hashlib.sha256(payload).hexdigest()}  {archive}\n"
        urls, ran = [], []

        def fetch(url, **_options):
            urls.append(url)
            return json.dumps([{"version": "v27.0.0", "lts": False}, {"version": version, "lts": "x"}]) if url.endswith("index.json") else listing

        def download(url, target, digest, **_options):
            urls.append(url)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            Path(target).write_bytes(payload)

        def run(command, *, env, cwd, on_line, cancelled):
            ran.append((command, env["PATH"]))
            self.program(self.data / "agents/gemini/bin/gemini", "#!/usr/bin/env node\n// @google/gemini-cli\n")
            return 0, []
        with patch.object(agent_setup.sys, "platform", "linux"), patch.object(agent_setup, "system_npm", return_value=None), \
                patch.object(agent_setup, "ensure_private_node",
                             side_effect=lambda **options: real(fetch=fetch, download=download, **options)), \
                patch.object(ai_agents.sys, "platform", "linux"):
            found = agent_setup.install("gemini", run=run)
        self.assertEqual(found, str(self.data / "agents/gemini/bin/gemini"))
        command, path = ran[0]
        self.assertEqual(command[:2], [str(self.data / f"agents/node/{version}/bin/node"),
                                       str(self.data / f"agents/node/{version}/lib/node_modules/npm/bin/npm-cli.js")])
        self.assertEqual(command[command.index("--prefix") + 1], str(self.data / "agents/gemini"))
        self.assertEqual(command[-1], "@google/gemini-cli@latest")
        self.assertTrue(path.startswith(str(self.data / f"agents/node/{version}/bin")))
        self.assertEqual([item.name for item in (self.data / "agents/node").iterdir()], [version], "No staging folder may remain")
        with self.assertRaisesRegex(agent_setup.AgentSetupError, "no checksum"):
            with patch.object(ai_agents, "node_directory", return_value=None), patch.object(agent_setup.sys, "platform", "linux"):
                real(on_line=lambda _l: None, cancelled=lambda: False, download=download,
                     fetch=lambda url, **_o: fetch(url) if url.endswith("json") else "other\n")

    def test_a_damaged_node_download_is_refused(self):
        target = self.root / "node.tar.xz"
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong")) as opened:
            opened.return_value.headers = {}
            with self.assertRaisesRegex(agent_setup.AgentSetupError, "checksum"):
                agent_setup._download_file("https://nodejs.org/x", target, "0" * 64, on_line=lambda _l: None, cancelled=lambda: False)


real = agent_setup.ensure_private_node


class ChooserChecks(unittest.TestCase):
    def chooser(self, width=420, height=620, found=("codex", "claude", "opencode")):
        widget = AgentChooser()
        widget.resize(width, height)
        widget.show()
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.close)
        widget.set_installed({name: "x" for name in found})
        APP.processEvents()
        return widget

    def test_names_built_in_and_italic_green_downloaded(self):
        widget = self.chooser()
        shown = {row.spec.id: [text for text, _font, _ink in row.suffixes()] for row in widget.package.rows}
        self.assertEqual(shown, {"gemini": [], "codex": [DOWNLOADED_TEXT], "claude": [DOWNLOADED_TEXT], "hermes": [],
                                 "opencode": [BUILTIN_TEXT]}, "Setup provides the built-in agent: only the others can be downloaded")
        self.assertEqual(widget.row("opencode").accessibleName(), "OpenCode (Built-in)")
        self.assertEqual((BUILTIN_TEXT, DOWNLOADED_TEXT), ("(Built-in)", "(Downloaded)"))
        _text, font, ink = widget.row("codex").suffixes()[0]
        self.assertTrue(font.italic())
        for mode in ("default", "light", "solarized_dark"):
            widget.apply_theme(mode)
            green = widget._inks[ink]
            self.assertGreater(green.green(), max(green.red(), green.blue()), (mode, green.name()))
            self.assertGreaterEqual(contrast_ratio(green.name(), glass_worst(mode)["reading"]), 4.5, mode)
        self.assertIn("(Downloaded)", widget.row("claude").accessibleName())

    def test_every_width_keeps_whole_names_and_never_holds_a_pane_open(self):
        for width, height in ((620, 700), (420, 620), (300, 420), (240, 300), (150, 260), (120, 120)):
            widget = self.chooser(width, height)
            self.assertEqual(widget.size().toTuple(), (width, height))
            self.assertEqual(widget.minimumSizeHint().toTuple(), (0, 0))
            self.assertFalse(widget._scroll.horizontalScrollBar().isVisible())
            for row in widget.package.rows:
                compact = row.width() < 150
                available = row.width() - (24 if compact else 62)
                first, second = row.lines(available)
                self.assertEqual(first[0][0], row.spec.name)
                if width >= 240:
                    from PySide6.QtGui import QFontMetricsF
                    self.assertLessEqual(QFontMetricsF(first[0][1]).horizontalAdvance(row.spec.name), available)
                shown = [text for text, _font, _ink in first + second]
                for text, _font, _ink in row.suffixes():
                    self.assertIn(text, shown, (width, row.spec.id))

    def test_the_chosen_row_lights_its_own_leads(self):
        widget = self.chooser()
        for mode in ("default", "light", "solarized_dark"):
            widget.apply_theme(mode)
            for agent_id in ("gemini", "hermes"):
                widget.select(agent_id)
                APP.processEvents()
                image = widget.package.grab().toImage()
                ratio = image.devicePixelRatio()
                body = widget.package.body_rect()
                accent = widget._inks["accent"]
                for row in widget.package.rows:
                    y = row.geometry().center().y() + 1
                    for x in (body.left() - 6, body.right() + 6):
                        lit = image.pixelColor(round(x * ratio), round(y * ratio)).rgb() == accent.rgb()
                        self.assertEqual(lit, row.spec.id == agent_id, (mode, agent_id, row.spec.id))

    def test_start_download_and_cancel_are_only_requests(self):
        widget = self.chooser()
        started, wanted, cancelled = [], [], []
        widget.start_requested.connect(started.append)
        widget.download_requested.connect(wanted.append)
        widget.cancel_requested.connect(lambda: cancelled.append(True))
        widget.select("claude")
        self.assertEqual(widget.primary.text(), "Start Claude")
        widget.primary.click()
        widget.select("hermes")
        self.assertEqual(widget.primary.text(), "Download Hermes")
        self.assertIn("nousresearch", widget.detail.text().replace("​", ""))
        self.assertIn("for this account only", widget.detail.text())
        widget.primary.click()
        self.assertEqual((started, wanted), (["claude"], ["hermes"]))
        widget.show_progress("hermes", "Downloading…")
        self.assertTrue(widget.cancel_button.isVisible())
        self.assertFalse(widget.primary.isVisible() or widget.row("codex").isEnabled())
        widget.cancel_button.click()
        self.assertEqual(cancelled, [True])
        widget.show_message("It failed", error=True)
        self.assertTrue(widget.primary.isVisible())
        self.assertEqual(widget.detail.text(), "It failed")
        widget.select("opencode")
        widget.set_installed({})
        self.assertIn("--repair", widget.detail.text().replace("​", ""))
        self.assertEqual(widget.primary.text(), "Start OpenCode", "Setup, not this list, provides the built-in assistant")

    def test_mouse_and_keyboard_choose_and_start(self):
        widget = self.chooser()
        started = []
        widget.start_requested.connect(started.append)
        row = widget.row("codex")
        QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=QPoint(30, 20))
        self.assertEqual(widget.selected, "codex")
        QTest.keyClick(row, Qt.Key.Key_Down)
        self.assertEqual(widget.selected, "claude")
        QTest.keyClick(widget.row("claude"), Qt.Key.Key_Return)
        QTest.mouseDClick(widget.row("opencode"), Qt.MouseButton.LeftButton, pos=QPoint(30, 20))
        self.assertEqual(started, ["claude", "opencode"])


class PickerChecks(unittest.TestCase):
    def setUp(self):
        self.widget = AgentChooser()
        self.addCleanup(self.widget.deleteLater)
        self.picker = AgentPicker(self.widget)
        self.addCleanup(self.picker.close)
        self.found = {"opencode": "x"}
        stub = patch.object(ai_agents, "installed_agents", side_effect=lambda: dict(self.found))
        stub.start()
        self.addCleanup(stub.stop)

    def test_detection_and_downloads_stay_off_the_gui_thread(self):
        threads = []

        def installed():
            threads.append(threading.current_thread())
            return dict(self.found)
        with patch.object(ai_agents, "installed_agents", side_effect=installed):
            self.picker.refresh()
            until(lambda: self.widget._checked_files, "detection")
        release, done = threading.Event(), []

        def install(agent_id, *, on_line, cancelled):
            threads.append(threading.current_thread())
            on_line("Downloading the fixture…")
            release.wait(5)
            self.found[agent_id] = "x"
            return "x"
        self.picker.downloaded.connect(done.append)
        with patch.object(agent_setup, "install", side_effect=install):
            self.widget.select("claude")
            self.widget.primary.click()
            until(lambda: self.widget.detail.text() == "Downloading the fixture…", "progress")
            self.picker.download("codex")       # One at a time.
            self.assertTrue(self.picker.downloading)
            release.set()
            until(lambda: done == ["claude"], "finished")
        self.assertTrue(self.widget.row("claude").installed)
        self.assertEqual(self.widget.primary.text(), "Start Claude")
        self.assertNotIn(threading.main_thread(), threads)

    def test_failure_names_the_makers_command_and_cancel_does_not(self):
        self.widget._checked_files = True
        with patch.object(agent_setup, "install", side_effect=agent_setup.AgentSetupError("It broke.")):
            self.picker.download("claude")
            until(lambda: not self.picker.downloading, "failed")
        text = self.widget.detail.text().replace("​", "")
        self.assertIn("It broke.", text)
        self.assertIn("claude.ai/install", text)

        def wait(agent_id, *, on_line, cancelled):
            until_thread = time.monotonic() + 5
            while not cancelled() and time.monotonic() < until_thread:
                time.sleep(.01)
            raise agent_setup.Cancelled("The download was cancelled.")
        with patch.object(agent_setup, "install", side_effect=wait):
            self.picker.download("claude")
            self.widget.cancel_requested.emit()
            until(lambda: not self.picker.downloading, "cancelled")
        self.assertEqual(self.widget.detail.text(), "The download was cancelled.")
        self.assertFalse(self.widget.detail.property("failed"))

    def test_closing_the_owner_ends_the_download(self):
        seen = threading.Event()

        def wait(agent_id, *, on_line, cancelled):
            while not cancelled():
                time.sleep(.01)
            seen.set()
            raise agent_setup.Cancelled("The download was cancelled.")
        with patch.object(agent_setup, "install", side_effect=wait):
            self.picker.download("claude")
            self.picker.close()
            self.assertTrue(seen.wait(5))
        APP.processEvents()


class PreferenceChecks(unittest.TestCase):
    def test_the_last_choice_is_remembered_and_unknown_values_are_ignored(self):
        saved = {}
        with patch("main.core.config._load_raw_config", side_effect=lambda: dict(saved)), \
                patch("main.core.config._save_raw_config", side_effect=lambda data: saved.update(data)) as save:
            self.assertEqual(ai_agents.last_agent(), "opencode")
            ai_agents.remember_agent("claude")
            self.assertEqual(ai_agents.last_agent(), "claude")
            ai_agents.remember_agent("claude")
            save.assert_called_once()
            ai_agents.remember_agent("no such agent")
            self.assertEqual(ai_agents.last_agent(), "claude")
            saved["shared"]["ai_assistant_agent"] = "retired"
            self.assertEqual(ai_agents.last_agent(), "opencode")
        with patch("main.core.config._load_raw_config", side_effect=OSError("locked")):
            ai_agents.remember_agent("codex")


if __name__ == "__main__":
    unittest.main(verbosity=2)
