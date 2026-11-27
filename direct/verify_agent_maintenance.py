#!/usr/bin/env python3
"""Assistant upkeep that never runs an agent: Hermes's unusable gateway leftovers, and
uninstalling an agent's program while its settings and sign-ins stay.

Every check works on fixture folders under the system temporary folder, with a stand-in
home folder; no real agent, gateway, sign-in or installation is touched.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main.core import ai_agents  # noqa: E402

POSIX = sys.platform != "win32"


def _bind(path: Path) -> socket.socket:
    """A listening socket at any path length, bound through the folder's descriptor."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    folder = os.open(path.parent, os.O_PATH | os.O_DIRECTORY)
    try:
        listener.bind(f"/proc/self/fd/{folder}/{path.name}")
    finally:
        os.close(folder)
    listener.listen(1)
    return listener


@unittest.skipUnless(sys.platform.startswith("linux"), "Hermes is isolated, and these sockets bound, on Linux")
class HermesGatewayLeftovers(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="mcu-hermes-"))
        self.addCleanup(self._remove, self.base)

    @staticmethod
    def _remove(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def place(self, length: int) -> ai_agents.Isolation:
        """An isolation whose data folder makes gateway.sock exactly ``length`` bytes long."""
        stem = self.base / "app"
        filler = max(1, length - len(os.fsencode(str(stem / "data" / "gateway.sock"))))
        root = Path(str(stem) + "x" * filler)
        place = ai_agents.Isolation(root=root, data=root / "data", home=root / "home", temp=root / "tmp")
        place.data.mkdir(parents=True)
        os.chmod(place.data, 0o700)
        self.assertEqual(len(os.fsencode(str(place.data / "gateway.sock"))), length)
        return place

    def clear(self, place):
        with patch.object(ai_agents, "isolation", return_value=place):
            return ai_agents.clear_unreachable_control("hermes")

    def test_a_dead_socket_too_long_to_reach_is_removed(self):
        # The reported failure: ~/.hermes moved into a 118-byte folder path.
        place = self.place(118)
        _bind(place.data / "gateway.sock").close()          # Its gateway is gone; the file stays.
        self.assertEqual(self.clear(place), [place.data / "gateway.sock"])
        self.assertFalse(os.path.lexists(place.data / "gateway.sock"))

    def test_a_live_socket_is_never_removed(self):
        place = self.place(118)
        listener = _bind(place.data / "gateway.sock")
        self.addCleanup(listener.close)
        self.assertEqual(self.clear(place), [])
        self.assertTrue(os.path.lexists(place.data / "gateway.sock"))

    def test_a_reachable_socket_is_left_to_hermes(self):
        place = self.place(100)
        _bind(place.data / "gateway.sock").close()
        self.assertEqual(self.clear(place), [])
        self.assertTrue(os.path.lexists(place.data / "gateway.sock"))

    def test_a_regular_file_of_that_name_is_not_ours_to_remove(self):
        place = self.place(118)
        (place.data / "gateway.sock").write_text("not a socket", encoding="utf-8")
        self.assertEqual(self.clear(place), [])
        self.assertTrue((place.data / "gateway.sock").is_file())

    def test_a_pointer_to_nothing_is_removed_and_one_to_a_socket_stays(self):
        place = self.place(118)
        pointer = place.data / "gateway.sock.path"
        pointer.write_text(str(self.base / "hermes-gw-0123456789abcdef/control.sock"), encoding="utf-8")
        self.assertEqual(self.clear(place), [pointer])
        live_folder = self.base / "hermes-gw-live"
        live_folder.mkdir()
        listener = _bind(live_folder / "control.sock")
        self.addCleanup(listener.close)
        pointer.write_text(str(live_folder / "control.sock"), encoding="utf-8")
        self.assertEqual(self.clear(place), [])
        self.assertTrue(pointer.is_file())
        pointer.write_bytes(b"\xff\xfe not a path")
        self.assertEqual(self.clear(place), [pointer])

    def test_only_the_isolated_hermes_is_touched(self):
        place = self.place(118)
        _bind(place.data / "gateway.sock").close()
        with patch.object(ai_agents, "isolation", return_value=place):
            self.assertEqual(ai_agents.clear_unreachable_control("claude"), [])
        with patch.object(ai_agents, "isolation", return_value=None):
            self.assertEqual(ai_agents.clear_unreachable_control("hermes"), [])
        self.assertTrue(os.path.lexists(place.data / "gateway.sock"))

    def test_every_session_start_clears_them(self):
        place = self.place(118)
        _bind(place.data / "gateway.sock").close()
        with patch.object(ai_agents, "isolation", return_value=place), \
                patch.object(ai_agents, "node_directory", return_value=None):
            environment = ai_agents.agent_environment("hermes", {"PATH": "/usr/bin"})
        self.assertEqual(environment["HERMES_HOME"], str(place.data))
        self.assertFalse(os.path.lexists(place.data / "gateway.sock"))



@unittest.skipUnless(POSIX, "The makers' Linux and macOS layouts, in a stand-in home folder")
class AgentRemoval(unittest.TestCase):
    """What Uninstall removes, keeps and refuses, on the layouts each maker's installer writes."""

    def setUp(self):
        import shutil
        from src.modules import agent_removal
        self.removal = agent_removal
        self.base = Path(tempfile.mkdtemp(prefix="mcu-removal-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.addCleanup(self._open_all)
        self.home = self.base / "home"
        self.agents = self.base / "app/.ai-agents/host"
        for folder in (self.home / ".local/bin", self.agents):
            folder.mkdir(parents=True)
        for patcher in (patch.object(Path, "home", return_value=self.home),
                        patch.dict(os.environ, {"HOME": str(self.home)}),
                        patch.object(ai_agents, "agents_root", return_value=self.agents),
                        patch.object(ai_agents, "isolation", return_value=None),
                        patch("main.core.file_utils.forget_installed_agents")):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("CODEX_HOME", None)
        # Something of the user's beside every launcher, which must stay.
        self.write(self.home / ".local/bin/esptool", "#!/bin/sh\n")

    def _open_all(self):
        for folder, _folders, _files in os.walk(self.base):
            os.chmod(folder, 0o700)

    @staticmethod
    def write(path: Path, text="program", mode=0o755) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def link(self, path: Path, target: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        return path

    def plan(self, agent_id, launcher):
        with patch.object(ai_agents, "find_agent", return_value=str(launcher)):
            return self.removal.plan(agent_id)

    def assert_gone(self, *paths):
        for path in paths:
            self.assertFalse(os.path.lexists(path), path)

    def assert_kept(self, *paths):
        for path in paths:
            self.assertTrue(os.path.lexists(path), path)

    def claude(self):
        version = self.write(self.home / ".local/share/claude/versions/2.1.0", "claude")
        launcher = self.link(self.home / ".local/bin/claude", version)
        settings = self.write(self.home / ".claude/settings.json", "{}", 0o600)
        config = self.write(self.home / ".claude.json", "{}", 0o600)
        return launcher, settings, config

    def test_claude_loses_its_program_and_keeps_its_settings(self):
        launcher, settings, config = self.claude()
        plan = self.plan("claude", launcher)
        self.assertEqual(plan.paths, (launcher, self.home / ".local/share/claude"))
        self.assertEqual(plan.kept, (self.home / ".claude", self.home / ".claude.json"))
        self.assertFalse(plan.everything)
        self.assertGreater(plan.size, 0)
        text = self.removal.describe(plan)
        self.assertIn("~/.local/bin/claude", text)
        self.assertIn("~/.local/share/claude", text)
        self.assertIn("stay in ~/.claude and ~/.claude.json", text)
        self.removal.remove(plan)
        self.assert_gone(launcher, self.home / ".local/share/claude")
        self.assert_kept(settings, config, self.home / ".local/bin/esptool", self.home / ".local/share")

    def test_codex_loses_its_releases_and_keeps_its_home(self):
        release = self.write(self.home / ".codex/packages/standalone/releases/1.0/bin/codex")
        self.link(self.home / ".codex/packages/standalone/current", self.home / ".codex/packages/standalone/releases/1.0")
        launcher = self.link(self.home / ".local/bin/codex", self.home / ".codex/packages/standalone/current/bin/codex")
        auth = self.write(self.home / ".codex/auth.json", "{}", 0o600)
        daemon = self.write(self.home / ".codex/packages/app-server-daemon/daemon")
        plan = self.plan("codex", launcher)
        self.assertEqual(plan.paths, (launcher, self.home / ".codex/packages/standalone"))
        self.assertEqual((plan.kept, plan.tidy), ((self.home / ".codex",), (self.home / ".codex/packages",)))
        self.removal.remove(plan)
        self.assert_gone(launcher, release, self.home / ".codex/packages/standalone")
        self.assert_kept(auth, daemon, self.home / ".codex/packages")
        # Without anything else there, the emptied packages folder goes too; the home stays.
        daemon.unlink()
        daemon.parent.rmdir()
        self.write(self.home / ".codex/packages/standalone/releases/2.0/bin/codex")
        launcher = self.link(self.home / ".local/bin/codex", self.home / ".codex/packages/standalone/releases/2.0/bin/codex")
        self.removal.remove(self.plan("codex", launcher))
        self.assert_gone(self.home / ".codex/packages")
        self.assert_kept(auth)

    def test_antigravity_loses_its_program_and_the_copies_its_updates_left(self):
        launcher = self.write(self.home / ".local/bin/agy")
        old = self.write(self.home / ".local/bin/agy.1795635612002596960.old")
        unrelated = [self.write(self.home / ".local/bin" / name) for name in ("agy.old", "agy.12.old.bak", "agy-tools")]
        plan = self.plan("antigravity", launcher)
        self.assertEqual(plan.paths, (launcher, old))
        self.assertIn("stay where Antigravity keeps them", self.removal.describe(plan))
        self.removal.remove(plan)
        self.assert_gone(launcher, old)
        self.assert_kept(*unrelated)

    def test_hermes_loses_its_whole_folder_but_nothing_its_links_point_to(self):
        root = self.agents / "hermes"
        place = ai_agents.Isolation(root=root, data=root / "data", home=root / "home", temp=root / "tmp")
        launcher = self.write(place.data / "hermes-agent/.hermes/bin/hermes")
        outside = self.write(self.home / "notes.txt", "mine")
        self.link(place.home / ".local/bin/notes", outside)
        locked = self.write(place.data / "cache/uv/locked/file")
        os.chmod(locked.parent, 0o555)          # Caches keep folders like this.
        with patch.object(ai_agents, "isolation", return_value=place):
            plan = self.plan("hermes", launcher)
            self.assertEqual((plan.paths, plan.everything), ((root,), True))
            self.assertIn("are removed too", self.removal.describe(plan))
            self.removal.remove(plan)
        self.assert_gone(root)
        self.assert_kept(outside, self.agents)
        self.assertEqual(outside.read_text(encoding="utf-8"), "mine")

    def test_an_agent_in_the_apps_own_folder_goes_whole(self):
        launcher = self.write(self.agents / "codex/bin/codex")
        plan = self.plan("codex", launcher)
        self.assertEqual(plan.paths, (self.agents / "codex",))
        self.removal.remove(plan)
        self.assert_gone(self.agents / "codex")
        self.assert_kept(self.agents)

    def test_installs_made_another_way_are_described_and_left_alone(self):
        script = self.write(self.home / ".npm-global/lib/node_modules/@openai/codex/bin/codex.js")
        npm = self.link(self.home / ".npm-global/bin/codex", script)
        plan = self.plan("codex", npm)
        self.assertEqual(plan.paths, ())
        self.assertIn("npm uninstall -g @openai/codex", plan.manual)
        brew = self.write(self.base / "opt/homebrew/Cellar/codex/1.0/bin/codex")
        plan = self.plan("codex", self.link(self.base / "opt/homebrew/bin/codex", brew))
        self.assertIn("brew uninstall codex", plan.manual)
        cask = self.write(self.base / "opt/homebrew/Caskroom/claude-code/2.0/claude")
        plan = self.plan("claude", self.link(self.base / "opt/homebrew/bin/claude", cask))
        self.assertIn("brew uninstall --cask claude-code", plan.manual)
        system = self.write(self.base / "usr/bin/claude")
        plan = self.plan("claude", system)
        self.assertIn("does not know how Claude was installed", plan.manual)
        # A launcher in the usual place that leads somewhere else is not the installer's either.
        elsewhere = self.write(self.base / "elsewhere/claude")
        plan = self.plan("claude", self.link(self.home / ".local/bin/claude", elsewhere))
        self.assertEqual(plan.paths, ())
        for path in (script, npm, brew, cask, system, elsewhere):
            self.assert_kept(path)
        with self.assertRaises(self.removal.RemovalError):
            self.removal.remove(plan)

    def test_the_built_in_unknown_and_missing_agents_are_refused(self):
        for agent_id, words in (("opencode", "comes with MCU Flasher"), ("nobody", "does not know this assistant")):
            with self.assertRaises(self.removal.RemovalError) as raised:
                self.removal.plan(agent_id)
            self.assertIn(words, str(raised.exception))
        with patch.object(ai_agents, "find_agent", return_value=None), self.assertRaises(self.removal.RemovalError) as raised:
            self.removal.plan("claude")
        self.assertIn("not installed", str(raised.exception))

    def test_only_this_accounts_folders_below_its_own_roots_are_removed(self):
        outside = self.write(self.base / "outside/program")
        for paths in ((self.home,), (self.agents,), (outside,), (self.home / "../outside/program",)):
            plan = self.removal.RemovalPlan("claude", "Claude", tuple(Path(path) for path in paths))
            with self.assertRaises(self.removal.RemovalError, msg=paths):
                self.removal.remove(plan)
        self.assert_kept(outside, self.home, self.agents)
        launcher, _settings, _config = self.claude()
        plan = self.plan("claude", launcher)
        with patch.object(os, "getuid", return_value=os.getuid() + 1), self.assertRaises(self.removal.RemovalError) as raised:
            self.removal.remove(plan)
        self.assertIn("another account", str(raised.exception))
        self.assert_kept(launcher, self.home / ".local/share/claude")

    def test_nothing_is_removed_while_it_runs(self):
        import shutil
        import subprocess
        import time
        launcher, settings, _config = self.claude()
        share = self.home / ".local/share/claude"
        # A program inside it, found by its executable, and the launcher's script, found by
        # its command line. Both wait without starting anything else.
        program = share / "tools/sleep"
        program.parent.mkdir(parents=True)
        shutil.copy2(shutil.which("sleep"), program)
        version = self.write(share / "versions/2.1.0", "#!/bin/sh\nread line\n")
        plan = self.plan("claude", launcher)
        for command in ([str(program), "30"], [str(launcher)]):
            child = subprocess.Popen(command, stdin=subprocess.PIPE)
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
            deadline = time.monotonic() + 5
            while not self.removal.running(plan) and time.monotonic() < deadline:
                time.sleep(0.02)
            names = self.removal.running(plan)
            self.assertTrue(any(name.endswith(f"(process {child.pid})") for name in names), (command, names))
            with self.assertRaises(self.removal.RemovalError) as raised:
                self.removal.remove(plan)
            self.assertIn("is running", str(raised.exception))
            self.assert_kept(launcher, program, version, settings)
            child.kill()
            child.wait()
        self.assertEqual(self.removal.running(plan), [])
        self.removal.remove(plan)
        self.assert_gone(launcher, share)
        self.assert_kept(settings)

    def test_windows_layouts_are_recognized(self):
        local = self.base / "Local"
        with patch.object(self.removal, "_windows", return_value=True), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(local), "APPDATA": str(self.base / "Roaming")}):
            claude = self.write(self.home / ".local/bin/claude.exe")
            self.write(self.home / ".local/share/claude/versions/2.1.0.exe")
            self.assertEqual(self.plan("claude", claude).paths, (claude, self.home / ".local/share/claude"))
            codex = self.write(local / "Programs/OpenAI/Codex/bin/codex.exe")
            plan = self.plan("codex", codex)
            self.assertEqual((plan.paths, plan.tidy), ((local / "Programs/OpenAI/Codex",), (local / "Programs/OpenAI",)))
            agy = self.write(local / "agy/bin/agy.exe")
            plan = self.plan("antigravity", agy)
            self.assertEqual((plan.paths, plan.tidy), ((local / "agy/bin",), (local / "agy",)))
            hermes = self.write(local / "hermes/bin/hermes.exe")
            plan = self.plan("hermes", hermes)
            self.assertEqual((plan.paths, plan.everything), ((local / "hermes",), True))
            self.removal.remove(plan)
            self.assert_gone(local / "hermes")
            self.assert_kept(local)
            plan = self.removal.RemovalPlan("hermes", "Hermes", (local,))
            with self.assertRaises(self.removal.RemovalError):
                self.removal.remove(plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
