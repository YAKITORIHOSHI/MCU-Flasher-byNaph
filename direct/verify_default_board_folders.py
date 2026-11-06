#!/usr/bin/env python3
"""Linux and macOS setup fills Resources with the default cores and survives a failure."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.modules import default_board_folders as folders


class DefaultBoardFolderChecks(unittest.TestCase):
    def test_both_native_setups_prepare_the_folders_after_the_toolchains(self):
        for name in ("ubuntu", "macos"):
            source = (ROOT / "direct" / name / "setup.py").read_text(encoding="utf-8")
            self.assertLess(source.index("finish_bootstrap(core)\n"), source.index("prepare_default_board_folders(python"))

    def test_failed_download_warns_without_stopping_setup(self):
        for result in (subprocess.CompletedProcess([], 1), OSError("no runtime")):
            with patch.object(subprocess, "run", side_effect=[result]), patch("builtins.print") as shown:
                self.assertFalse(folders.prepare_default_board_folders("python"))
            self.assertIn("Compile and Upload are not affected", shown.call_args.args[0])

    def test_one_failed_core_does_not_skip_the_other(self):
        from src.modules import bootstrap
        with patch.object(bootstrap, "ensure_arduino_avr_board", side_effect=RuntimeError("offline")), \
                patch.object(bootstrap, "ensure_esp32_board_folder", return_value=True) as esp32, \
                patch("sys.stderr"):
            self.assertEqual(folders.main(), 1)
        esp32.assert_called_once()


if __name__ == "__main__":
    unittest.main()
