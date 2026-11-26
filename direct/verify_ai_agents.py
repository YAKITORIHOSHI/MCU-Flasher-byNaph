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
from pathlib import Path, PurePosixPath
import shlex
import stat
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

from main.core import ai_agents, compiled_cache
from main.core.log_colors import contrast_ratio
from main.core.theme import glass_worst
from main.qt.agent_chooser import AgentChooser, BUILTIN_TEXT, DOWNLOADED_TEXT
from main.qt.agent_picker import AgentPicker
from src.modules import agent_setup

APP = QApplication.instance() or QApplication([])
from main.qt.theme import register_fonts
register_fonts()
REAL_AGENTS_ROOT = ai_agents.agents_root


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
        for target, value in (("agents_root", lambda: self.data / "agents"), ("_posix_directories", lambda: [self.home / ".local/bin"])):
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
        self.assertEqual([agent.name for agent in ai_agents.AGENTS], ["Antigravity", "Codex", "Claude", "Hermes", "OpenCode"])
        for agent in ai_agents.AGENTS:
            self.assertNotIn("CLI", agent.name)
            self.assertEqual(agent.builtin, agent.id == "opencode")
        self.assertEqual(ai_agents.DEFAULT_AGENT, "opencode")
        self.assertEqual(ai_agents.agent("no such").id, "opencode")

    def test_download_sources_are_the_makers_own_over_https(self):
        hosts = {"antigravity": "antigravity.google", "codex": "chatgpt.com", "claude": "claude.ai", "hermes": "hermes-agent.nousresearch.com"}
        for platform in ("linux", "darwin", "win32"):
            with patch.object(ai_agents.sys, "platform", platform):
                self.assertIsNone(ai_agents.install_plan("opencode"), "Setup provides the built-in assistant")
                for agent_id, host in hosts.items():
                    plan = ai_agents.install_plan(agent_id)
                    self.assertTrue(plan.url.startswith(f"https://{host}/"), plan)
                    self.assertTrue(plan.url.endswith(".ps1" if platform == "win32" else ".sh"))
                    self.assertIn(host, plan.source)
                    self.assertNotIn("sudo", plan.command)
                self.assertIsNone(ai_agents.install_plan("gemini"), "Gemini is no longer offered")

    def test_only_opencode_takes_the_project_as_an_argument(self):
        self.assertEqual(ai_agents.agent_argv("opencode", "/x/opencode", "/a sketch"), ["/x/opencode", "/a sketch"])
        for agent_id in ("codex", "claude", "hermes"):
            self.assertEqual(ai_agents.agent_argv(agent_id, "/x/tool", "/a sketch"), ["/x/tool"])
        self.assertEqual(ai_agents.agent_argv("antigravity", "/x/agy", "/a sketch"), ["/x/agy", "--dangerously-skip-permissions"])
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
            self.assertIsNone(ai_agents.find_agent("antigravity"))

    def test_another_program_of_the_same_name_is_not_the_agent(self):
        self.program(self.root / "path/agy", "#!/usr/bin/env bash\n# opens the Antigravity editor\n")
        self.assertIsNone(ai_agents.find_agent("antigravity"))
        native = self.home / ".local/bin/agy"
        native.parent.mkdir(parents=True)
        native.write_bytes(Path(sys.executable).resolve().read_bytes()[:4096])
        native.chmod(0o755)
        self.assertEqual(ai_agents.find_agent("antigravity"), str(native), "The native CLI counts; the editor's script does not")
        self.program(self.root / "path/codex")
        link = self.data / "agents/codex/bin/codex"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.root / "path/codex")
        self.assertEqual(ai_agents.find_agent("codex"), str(link), "The app's own download comes first")

    def test_hermes_counts_only_inside_this_app(self):
        place = ai_agents.isolation("hermes")
        self.assertEqual(place.root, self.data / "agents/hermes")
        self.assertEqual((place.data, place.home, place.temp), (place.root / "data", place.root / "home", place.root / "tmp"))
        # Its maker's default install, on PATH and in ~/.local/bin, belongs to another home.
        self.program(self.root / "path/hermes", '#!/bin/sh\nexec "$HOME/.hermes/hermes-agent/run" "$@"\n')
        self.program(self.home / ".local/bin/hermes", '#!/bin/sh\nexec "$HOME/.hermes/hermes-agent/run" "$@"\n')
        self.program(self.home / ".hermes/hermes-agent/.hermes/bin/hermes")
        self.assertIsNone(ai_agents.find_agent("hermes"))
        self.assertEqual(ai_agents.candidates("hermes"), [place.data / "hermes-agent/.hermes/bin/hermes"])
        launcher = self.program(place.data / "hermes-agent/.hermes/bin/hermes")
        self.assertEqual(ai_agents.find_agent("hermes"), str(launcher))
        interpreter = self.program(place.data / "tools/python-3.14/bin/python3")
        launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(str(interpreter))} -I -c 'import sys\nsys.exit(0)'\n", encoding="utf-8")
        self.assertIn("'", launcher.read_text(encoding="utf-8").splitlines()[1].split(" -I")[0], "The fixture path needs quoting")
        self.assertEqual(ai_agents.find_agent("hermes"), str(launcher))
        launcher.write_text("#!/bin/sh\nexec /home/someone/.hermes/tools/python-3.14/bin/python3 -I -c 'import sys'\n", encoding="utf-8")
        self.assertIsNone(ai_agents.find_agent("hermes"), "A launcher still naming the folder's old place counts as missing")
        launcher.write_bytes(b"MZ copied from Windows")
        self.assertIsNone(ai_agents.find_agent("hermes"))

    def test_the_folder_is_in_this_app_per_os_and_cpu_except_on_windows(self):
        with patch.object(ai_agents, "agents_root", REAL_AGENTS_ROOT):
            root = ai_agents.agents_root()
            self.assertEqual(root.parent, ai_agents.APP_ROOT / ".ai-agents")
            self.assertEqual(root.name, compiled_cache.host_namespace())
            with patch.object(ai_agents.sys, "platform", "win32"), \
                    patch.object(ai_agents, "app_data_dir", return_value=self.root / "AppData"):
                self.assertEqual(ai_agents.agents_root(), self.root / "AppData/agents")
                self.assertIsNone(ai_agents.isolation("hermes"), "Windows keeps Hermes where its maker puts it")
                self.assertNotIn("--hermes-home", ai_agents.install_plan("hermes").arguments)
        self.assertIsNone(ai_agents.isolation("claude"))
        self.assertTrue(ai_agents.agent("hermes").isolated)
        self.assertEqual([spec.id for spec in ai_agents.AGENTS if spec.isolated], ["hermes"])

    def test_sessions_get_its_own_home_and_none_of_the_apps_bytecode_folder(self):
        place = ai_agents.isolation("hermes")
        for version in ("v26.13.1",):
            self.program(self.data / f"agents/node/{version}/bin/node")
        base = {"PATH": "/usr/bin", "HOME": str(self.home), "PYTHONPYCACHEPREFIX": str(self.root / "app-cache")}
        changes = ai_agents.agent_environment("hermes", base)
        self.assertEqual(changes["HERMES_HOME"], str(place.data))
        self.assertEqual(changes["HERMES_GATEWAY_LOCK_DIR"], str(place.home / ".local/state/hermes/gateway-locks"))
        self.assertEqual({changes[name] for name in ("TMPDIR", "TMP", "TEMP")}, {str(place.temp)})
        self.assertTrue(place.temp.is_dir())
        self.assertIsNone(changes["PYTHONPYCACHEPREFIX"])
        self.assertNotIn("HOME", changes, "Sessions keep the user's home: borrowed logins and their commands use it")
        self.assertNotIn(str(self.data / "agents/node"), changes.get("PATH", ""), "Hermes brings its own Node.js")
        session = ai_agents.without_removed({**base, **changes})
        self.assertNotIn("PYTHONPYCACHEPREFIX", session)
        self.assertEqual(session["HOME"], str(self.home))
        for agent_id in ("claude", "codex", "antigravity", "opencode"):
            other = ai_agents.agent_environment(agent_id, base)
            self.assertFalse({"HERMES_HOME", "PYTHONPYCACHEPREFIX", "TMPDIR", "HOME"} & set(other), agent_id)

    def test_its_installer_gets_a_stand_in_home_inside_its_folder(self):
        place = ai_agents.isolation("hermes")
        changes = ai_agents.installer_environment("hermes", {"PATH": "/usr/bin"})
        self.assertEqual(changes["HOME"], str(place.home))
        self.assertEqual({name: changes[name] for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")},
                         {"XDG_CONFIG_HOME": str(place.home / ".config"), "XDG_CACHE_HOME": str(place.home / ".cache"),
                          "XDG_DATA_HOME": str(place.home / ".local/share"), "XDG_STATE_HOME": str(place.home / ".local/state")})
        self.assertEqual(changes["HERMES_HOME"], str(place.data))
        self.assertNotIn("HOME", ai_agents.installer_environment("claude", {"PATH": "/usr/bin"}))
        plan = ai_agents.install_plan("hermes")
        self.assertEqual(plan.arguments[-2:], ("--hermes-home", str(place.data)))
        self.assertIn("--skip-browser", plan.arguments)
        self.assertIn("bash -s -- --hermes-home", plan.command)
        self.assertIn(str(place.data), plan.command.replace("'", ""))

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
        (self.data / "agents/codex").mkdir()
        path = ai_agents.agent_environment("codex")["PATH"].split(os.pathsep)
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

    def hermes_installer(self, launcher_text):
        """A stand-in for Nous Research's installer that writes where the real one does."""
        prepared = self.program(self.root / "prepared-launcher", launcher_text)
        record = self.root / "installer.env"
        script = ("#!/bin/bash\n"
                  f"{{ echo \"args: $*\"; env; }} > '{record}'\n"
                  'mkdir -p "$HOME/.local/bin" "$XDG_CACHE_HOME/uv" "$HERMES_HOME/hermes-agent/.hermes/bin"\n'
                  'printf \'#!/bin/sh\\nexec "%s" "$@"\\n\' "$HERMES_HOME/hermes-agent/.hermes/bin/hermes" > "$HOME/.local/bin/hermes"\n'
                  'printf \'\\n# Hermes Agent command\\nexport PATH="$HOME/.local/bin:$PATH"\\n\' >> "$HOME/.bashrc"\n'
                  ': > "$XDG_CACHE_HOME/uv/CACHEDIR.TAG"\n'
                  'mktemp "$TMPDIR/hermes-uv.XXXXXX" > /dev/null\n'
                  f"cp '{prepared}' \"$HERMES_HOME/hermes-agent/.hermes/bin/hermes\"\n"
                  "echo 'Hermes Agent install complete'\n" + "# padding\n" * 10).encode()
        return script, record

    @unittest.skipIf(sys.platform == "win32", "POSIX shells")
    def test_hermes_installs_whole_inside_its_folder_and_never_in_the_home_folder(self):
        place = ai_agents.isolation("hermes")
        calls = self.root / "launcher.calls"
        script, record = self.hermes_installer(f"#!/bin/sh\n{{ echo \"$*\"; echo \"HOME=$HOME\"; }} >> '{calls}'\n")
        lines, before = [], set(Path(tempfile.gettempdir()).glob("mcu-agent-*"))
        with patch.dict(os.environ, {"PATH": os.defpath, "PYTHONPYCACHEPREFIX": str(self.root / "app-cache"),
                                     "TMPDIR": str(self.root / "system-tmp")}):
            (self.root / "system-tmp").mkdir()
            found = agent_setup.install("hermes", on_line=lines.append, fetch=lambda *_a, **_k: script)
        launcher = place.data / "hermes-agent/.hermes/bin/hermes"
        self.assertEqual(found, str(launcher))
        seen = record.read_text(encoding="utf-8")
        self.assertIn(f"args: --non-interactive --skip-browser --skip-computer-use --hermes-home {place.data}\n", seen)
        environment = dict(line.split("=", 1) for line in seen.splitlines()[1:] if "=" in line)
        self.assertEqual({name: environment[name] for name in ("HOME", "HERMES_HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_DATA_HOME")},
                         {"HOME": str(place.home), "HERMES_HOME": str(place.data), "TMPDIR": str(place.temp),
                          "XDG_CACHE_HOME": str(place.home / ".cache"), "XDG_DATA_HOME": str(place.home / ".local/share")})
        self.assertNotIn("PYTHONPYCACHEPREFIX", environment)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [], "Nothing may land in the user's home folder")
        self.assertEqual(list((self.root / "system-tmp").iterdir()), [])
        self.assertTrue((place.home / ".local/bin/hermes").is_file())
        self.assertIn("Hermes Agent command", (place.home / ".bashrc").read_text(encoding="utf-8"))
        self.assertEqual(calls.read_text(encoding="utf-8").splitlines(),
                         ["config set cli.expose_on_path false", f"HOME={place.home}"])
        self.assertIn("Keeping Hermes inside MCU Flasher's folder…", lines)
        self.assertFalse(any(place.temp.glob("mcu-agent-*")), "The installer script is removed")
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("mcu-agent-*")), before)
        for folder in (place.root, place.data, place.home, place.temp):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700, folder)
        with patch.object(agent_setup, "fetch_script", side_effect=AssertionError("Already here")):
            self.assertEqual(agent_setup.install("hermes"), str(launcher))

    @unittest.skipIf(sys.platform == "win32", "POSIX shells")
    def test_hermes_that_cannot_be_kept_off_path_is_reported(self):
        script, _record = self.hermes_installer("#!/bin/sh\necho 'Error: config.yaml is locked'\nexit 4\n")
        with patch.dict(os.environ, {"PATH": os.defpath}):
            with self.assertRaisesRegex(agent_setup.AgentSetupError, r"kept out of ~/.local/bin \(exit 4\)\. Error: config.yaml is locked"):
                agent_setup.install("hermes", fetch=lambda *_a, **_k: script)

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
            self.program(self.data / "agents/codex/bin/codex", "#!/usr/bin/env node\n")
            return 0, []
        with patch.object(agent_setup.sys, "platform", "linux"), patch.object(agent_setup, "system_npm", return_value=None), \
                patch.object(agent_setup, "ensure_private_node",
                             side_effect=lambda **options: real(fetch=fetch, download=download, **options)), \
                patch.object(ai_agents.sys, "platform", "linux"), \
                patch.object(ai_agents, "install_plan", return_value=ai_agents.InstallPlan("npm", "npm", "npm", package="@openai/codex")):
            found = agent_setup.install("codex", run=run)
        self.assertEqual(found, str(self.data / "agents/codex/bin/codex"))
        command, path = ran[0]
        self.assertEqual(command[:2], [str(self.data / f"agents/node/{version}/bin/node"),
                                       str(self.data / f"agents/node/{version}/lib/node_modules/npm/bin/npm-cli.js")])
        self.assertEqual(command[command.index("--prefix") + 1], str(self.data / "agents/codex"))
        self.assertEqual(command[-1], "@openai/codex@latest")
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
        self.assertEqual(shown, {"antigravity": [], "codex": [DOWNLOADED_TEXT], "claude": [DOWNLOADED_TEXT], "hermes": [],
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
            for agent_id in ("antigravity", "hermes"):
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

    def test_the_info_mark_states_who_owns_each_assistant(self):
        from PySide6.QtWidgets import QToolTip
        from main.qt.agent_chooser import disclaimer_text
        widget = self.chooser()
        info = widget.package.info
        self.assertTrue(info.isVisible())
        self.assertEqual(info.accessibleName(), "About these assistants")
        text = disclaimer_text()
        self.assertEqual(info.toolTip(), text, "hovering shows the disclaimer")
        self.assertIn("were not created by, and do not belong to, the developer of MCU Flasher", text)
        self.assertIn("rightful owner", text)
        for spec in ai_agents.AGENTS:
            self.assertIn(f"<b>{spec.name}</b>", text)
        for owner in ("Google", "OpenAI", "Anthropic", "Nous Research", "open-source authors"):
            self.assertIn(owner, text)
        body = widget.package.body_rect()
        self.assertTrue(body.contains(info.geometry().toRectF()), "the mark sits on the board")
        for row in widget.package.rows:
            self.assertFalse(info.geometry().intersects(row.geometry()), row.spec.id)
        info.click()
        self.assertTrue(QToolTip.isVisible(), "clicking shows it too")
        self.assertIn("Disclaimer", QToolTip.text())
        QToolTip.hideText()

    def test_rows_show_no_hover_card(self):
        widget = self.chooser()
        for row in widget.package.rows:
            self.assertEqual(row.toolTip(), "", row.spec.id)
            self.assertIn(row.spec.maker, row.accessibleDescription())

    def test_every_agent_shows_its_own_icon_in_the_theme_ink(self):
        from main.qt import agent_chooser
        for spec in ai_agents.AGENTS:
            data = (ROOT / "src/assets/icons/agents" / f"{spec.id}.svg").read_bytes()
            # Plain artwork: no script and nothing fetched from anywhere.
            self.assertNotIn(b"<script", data.lower(), spec.id)
            self.assertNotRegex(data, rb'(?:href|src)\s*=\s*"(?!#)', spec.id)
            for ink in ("#ff123456", "#ff0969da"):
                image = agent_chooser.agent_icon(spec.id, 20, 2.0, ink)
                self.assertIsNotNone(image, spec.id)
                self.assertEqual(image.width(), 40, spec.id)
                inked = [image.pixelColor(x, y) for x in range(0, 40, 2) for y in range(0, 40, 2)
                         if image.pixelColor(x, y).alpha() > 200]
                self.assertGreater(len(inked), 20, f"{spec.id} draws a visible mark")
                if b"currentColor" in data:
                    want = QColor("#" + ink[3:])
                    # One level of rounding where an edge pixel is premultiplied.
                    self.assertTrue(all(max(abs(color.red() - want.red()), abs(color.green() - want.green()),
                                            abs(color.blue() - want.blue())) <= 3 for color in inked), spec.id)
        self.assertIsNone(agent_chooser.agent_icon("no-such-agent", 20, 1.0, "#ff000000"))

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
        self.assertIn("for this account only" if sys.platform == "win32" else "into MCU Flasher's own folder",
                      widget.detail.text().replace("​", ""))
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
        self.online = True
        network = patch.object(ai_agents, "internet_available", side_effect=lambda *a, **k: self.online)
        network.start()
        self.addCleanup(network.stop)

    def test_offline_the_list_gives_way_to_a_notice_that_brings_it_back(self):
        self.online = False
        self.picker.refresh()
        until(lambda: not self.widget.online, "offline notice")
        self.assertTrue(self.widget._offline_view.isVisibleTo(self.widget))
        self.assertFalse(self.widget._list_view.isVisibleTo(self.widget))
        self.assertTrue(self.picker._recheck.isActive(), "it keeps checking by itself")
        self.online = True
        self.widget.retry_button.click()
        self.assertFalse(self.widget.retry_button.isEnabled())
        until(lambda: self.widget.online, "list returns")
        self.assertTrue(self.widget._list_view.isVisibleTo(self.widget))
        self.assertFalse(self.widget._offline_view.isVisibleTo(self.widget))

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


@unittest.skipIf(sys.platform == "win32", "POSIX PTY")
class SessionEnvironmentChecks(unittest.TestCase):
    def test_none_removes_a_variable_from_the_session(self):
        from main.platforms import ubuntu_pty_process
        from main.qt.posix_terminal_panel import PtySession
        seen = {}

        def spawn(command, *, cwd, env, dimensions):
            seen.update(env)
            raise OSError("the fixture stops before a process starts")
        session = PtySession(str(ROOT), ["/bin/true"])
        self.addCleanup(session.deleteLater)
        session.extra_environment = {"PYTHONPYCACHEPREFIX": None, "HERMES_HOME": "/fixture/data"}
        with patch.dict(os.environ, {"PYTHONPYCACHEPREFIX": "/fixture/app-cache"}), \
                patch.object(ubuntu_pty_process.NativePtyProcess, "spawn", side_effect=spawn):
            self.assertFalse(session.start())
        self.assertEqual(seen["HERMES_HOME"], "/fixture/data")
        self.assertNotIn("PYTHONPYCACHEPREFIX", seen)


class SketchSkillChecks(Fixture):
    NAMES = ("sketch-workflow", "mcu-sketch-target", "serial-monitor-output")

    def generate(self, sketch, installed):
        from main.core import file_utils
        with patch.object(file_utils, "_installed_agent_ids", return_value=frozenset(installed)):
            file_utils.ensure_hidden_read_first_md(sketch)

    def sketch(self):
        sketch = self.root / "Blink Sketch"
        sketch.mkdir()
        (sketch / "Blink Sketch.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        return sketch

    def skills(self, folder):
        base = folder / "skills"
        return sorted(path.parent.name for path in base.glob("*/SKILL.md")) if base.is_dir() else []

    def test_each_installed_agent_gets_the_skills_where_it_reads_them(self):
        sketch = self.sketch()
        self.generate(sketch, {"opencode", "codex", "claude"})
        for folder in (".opencode", ".claude", ".agents"):
            self.assertEqual(self.skills(sketch / folder), sorted(self.NAMES), folder)
        self.assertIn("AGENTS.md", {path.name for path in sketch.iterdir()})
        self.assertFalse((sketch / "CLAUDE.md").exists(), "Claude reads AGENTS.md when a project has no CLAUDE.md")
        serial = (sketch / ".claude/skills/serial-monitor-output/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(serial.startswith("---\nname: serial-monitor-output\ndescription: "))
        for text in ("90-97", "40-47, 100-107", "ansiSet(Print &out, uint8_t fg, uint8_t bg)", "31250", "2000000"):
            self.assertIn(text, serial)
        self.assertNotIn("512000", serial, "the monitor offers only the Arduino IDE's rates")
        workflow = (sketch / ".agents/skills/sketch-workflow/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Build Console", workflow)
        self.assertNotIn("Syntax", workflow)

    def test_only_installed_agents_keep_skills_and_user_files_stay(self):
        sketch = self.sketch()
        self.generate(sketch, {"opencode", "codex", "claude", "antigravity"})
        own = sketch / ".claude/settings.local.json"
        own.write_text("{}", encoding="utf-8")
        mine = sketch / ".agents/skills/sketch-workflow/SKILL.md"
        mine.write_text("---\nname: sketch-workflow\ndescription: My own.\n---\nMine.\n", encoding="utf-8")
        self.generate(sketch, {"opencode"})
        self.assertEqual(self.skills(sketch / ".opencode"), sorted(self.NAMES), "OpenCode is built in")
        self.assertEqual(self.skills(sketch / ".claude"), [], "Claude is gone, so are its skills")
        self.assertTrue(own.is_file(), "Claude's own settings are not this app's to remove")
        self.assertEqual(self.skills(sketch / ".agents"), ["sketch-workflow"], "A user's skill of that name stays")
        self.assertIn("Mine.", mine.read_text(encoding="utf-8"))
        self.generate(sketch, {"opencode", "codex"})
        self.assertIn("Mine.", mine.read_text(encoding="utf-8"), "and is never overwritten")
        self.assertEqual(self.skills(sketch / ".agents"), sorted(self.NAMES))

    @unittest.skipIf(sys.platform == "win32", "Hermes is isolated on Linux and macOS")
    def test_isolated_hermes_gets_them_in_its_own_home(self):
        sketch = self.sketch()
        place = ai_agents.isolation("hermes")
        place.data.mkdir(parents=True)
        self.generate(sketch, {"opencode", "hermes"})
        found = sorted(path.parent.name for path in (place.data / "skills/mcu-flasher").glob("*/SKILL.md"))
        self.assertEqual(found, sorted(self.NAMES))
        self.assertFalse((sketch / ".hermes").exists() or (sketch / ".agents").exists())

    def test_the_application_codebase_is_never_given_sketch_skills(self):
        from main.core import file_utils
        with patch.object(file_utils, "is_application_codebase_dir", return_value=True), \
                patch.object(file_utils, "_install_sketch_skills", side_effect=AssertionError("not in the codebase")):
            file_utils.ensure_hidden_read_first_md(self.sketch())

    def test_installed_agents_are_found_by_files_and_cached(self):
        from main.core import file_utils
        file_utils._installed_agents_cache = (0.0, frozenset())
        self.addCleanup(setattr, file_utils, "_installed_agents_cache", (0.0, frozenset()))
        with patch.object(ai_agents, "find_agent", side_effect=lambda agent_id: "/x" if agent_id == "claude" else None) as find:
            self.assertEqual(file_utils._installed_agent_ids(), {"opencode", "claude"})
            self.assertEqual(file_utils._installed_agent_ids(), {"opencode", "claude"})
        self.assertEqual(find.call_count, 4, "Four downloadable agents, checked once within the cache window")
        file_utils.forget_installed_agents()
        with patch.object(ai_agents, "find_agent", side_effect=lambda agent_id: "/x" if agent_id in ("claude", "codex") else None):
            self.assertEqual(file_utils._installed_agent_ids(), {"opencode", "claude", "codex"},
                             "A finished download is seen at once, not after the cache window")


class ContainmentChecks(unittest.TestCase):
    def test_the_agents_folder_is_never_shipped_replaced_or_cleaned(self):
        from src.modules import app_update, application_sources
        inside = ".ai-agents/ubuntu-x86_64/hermes/data/.env"
        self.assertTrue(app_update._preserve_local_path(PurePosixPath(inside)), "Updates keep it")
        self.assertFalse(application_sources.is_application_source(inside), "Releases and Bootstrap copies leave it out")
        for name, text in (("direct/build_release.py", "'.ai-agents'"), ("cleaner/ubuntu/maintenance.py", '".ai-agents"'),
                           ("cleaner/windows/maintenance.ps1", "'.ai-agents'")):
            self.assertIn(text, (ROOT / name).read_text(encoding="utf-8"), name)
        ignore = ROOT / ".gitignore"
        if ignore.is_file():
            self.assertIn("/.ai-agents/", ignore.read_text(encoding="utf-8").splitlines())


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
