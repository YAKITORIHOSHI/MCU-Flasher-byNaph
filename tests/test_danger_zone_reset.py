import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESET_SCRIPT = (
    PROJECT_ROOT / "DANGER-ZONE" / "DELETE_EVERYTHING_DO_NOT_RUN.ps1"
)
RESET_CMD = PROJECT_ROOT / "DANGER-ZONE" / "runReset.cmd"


class DangerZoneResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = RESET_SCRIPT.read_text(encoding="utf-8")
        cls.command = RESET_CMD.read_text(encoding="utf-8")

    def test_launcher_selects_full_test_pc_reset(self):
        self.assertIn("-FullReset", self.command)
        self.assertIn("%*", self.command)

    def test_full_reset_requires_exact_confirmation_and_supports_dry_run(self):
        self.assertIn("DELETE MCU INSTALL", self.script)
        self.assertIn("-cne \"DELETE MCU INSTALL\"", self.script)
        self.assertIn("[switch]$DryRun", self.script)
        self.assertIn("DRY RUN: nothing will be stopped", self.script)

    def test_directory_deletion_is_allowlist_gated(self):
        self.assertIn("Test-ApprovedDirectoryTarget $Path", self.script)
        self.assertIn("Safety refusal: directory is not an approved reset target", self.script)
        self.assertIn("Refusing to approve a drive root", self.script)
        self.assertIn("Test-ApprovedFileTarget $Path", self.script)
        self.assertIn("Safety refusal: file is not an approved reset target", self.script)

    def test_system_cleanup_is_scoped_to_bootstrap_components(self):
        required = (
            "Python.Python.3.",
            "opencode-ai",
            "^Arduino CLI",
            "^Node\\.js",
            "Microsoft Edge WebView2 Runtime",
            "Remove-Cp210xDriver",
            ".platformio-mcu-gui",
            "_MCUFlasherByNaph_src",
            "Find-CachedMsi",
            "Remove-StaleUninstallRegistration",
        )
        for token in required:
            self.assertIn(token, self.script)

        # Python folders are removed only as exact Python install children,
        # not by blindly deleting a broad parent such as LocalAppData.
        self.assertIn("^Python3\\d+$", self.script)
        self.assertNotIn('Stop-ProcessesByName @("msedgewebview2", "msedge")', self.script)


if __name__ == "__main__":
    unittest.main()
