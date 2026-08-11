import importlib.util
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = PROJECT_ROOT / "src" / "libs" / "bootstrap.py"


def load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("bootstrap_msi_elevation", BOOTSTRAP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BootstrapMsiElevationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_bootstrap_module()

    def test_log_open_error_retries_same_msi_without_logging(self):
        b = self.bootstrap
        args = ["/i", r"C:\installer\arduino-cli.msi", "/quiet", "/norestart"]
        log_path = Path(r"C:\Temp\mcu_flasher_msi_logs\arduino-cli-test.log")

        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "_new_msi_log_path", return_value=log_path),
            mock.patch.object(b, "_shell_execute_elevated_wait", side_effect=[1622, 0]) as launch,
        ):
            code, detail = b._run_msiexec(args)

        self.assertEqual(code, 0)
        self.assertIn("retried once without diagnostic logging", detail)
        self.assertEqual(
            launch.call_args_list,
            [
                mock.call("msiexec", [*args, "/L*V", str(log_path)]),
                mock.call("msiexec", args),
            ],
        )

    def test_already_elevated_setup_does_not_start_another_helper(self):
        b = self.bootstrap
        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "_is_process_elevated", return_value=True),
            mock.patch.object(b, "_shell_execute_elevated_wait") as elevate,
            mock.patch.object(b, "status"),
        ):
            self.assertTrue(b._request_first_run_privileged_setup())

        elevate.assert_not_called()

    def test_windows_bundle_uses_only_supported_windows_installers(self):
        b = self.bootstrap
        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "ensure_webview2_runtime", return_value=True) as webview,
            mock.patch.object(b, "ensure_arduino_cli", return_value=True) as arduino,
            mock.patch.object(b, "ensure_cp210x", return_value=True) as cp210x,
            mock.patch.object(b, "_install_arduino_cli_machine_only") as machine_only,
        ):
            results = b._ensure_bundled_windows_installers()

        self.assertEqual(
            results,
            {"webview2": True, "arduino_cli": True, "cp210x": True},
        )
        webview.assert_called_once_with()
        arduino.assert_called_once_with()
        cp210x.assert_called_once_with()
        machine_only.assert_not_called()

    def test_privileged_helper_uses_the_full_machine_installer_bundle(self):
        b = self.bootstrap
        bundle_results = {"webview2": True, "arduino_cli": True, "cp210x": True}
        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "_is_process_elevated", return_value=True),
            mock.patch.object(
                b,
                "_ensure_bundled_windows_installers",
                return_value=bundle_results,
            ) as install_bundle,
            mock.patch.object(b, "_find_usable_npm_cmd", return_value="npm.cmd"),
        ):
            self.assertTrue(b._run_privileged_first_run_tasks())

        install_bundle.assert_called_once_with(machine_only_arduino=True)

    def test_already_elevated_bootstrap_runs_the_windows_bundle_directly(self):
        b = self.bootstrap
        direct_results = {"webview2": True, "arduino_cli": True, "cp210x": True}
        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "_is_process_elevated", return_value=True),
            mock.patch.object(b, "_request_first_run_privileged_setup", return_value=True) as request,
            mock.patch.object(
                b,
                "_ensure_bundled_windows_installers",
                return_value=direct_results,
            ) as install_bundle,
        ):
            elevated, setup_ok, results = b._prepare_bundled_windows_installers()

        self.assertTrue(elevated)
        self.assertTrue(setup_ok)
        self.assertEqual(results, direct_results)
        request.assert_called_once_with(None)
        install_bundle.assert_called_once_with()

    def test_cp210x_status_reports_installed_or_staged_driver(self):
        ok, message = self.bootstrap._cp210x_driver_status_message(
            True,
            False,
            False,
            True,
        )

        self.assertTrue(ok)
        self.assertIn("installed or staged", message)


if __name__ == "__main__":
    unittest.main()
