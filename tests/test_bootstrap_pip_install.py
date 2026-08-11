import importlib.util
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = PROJECT_ROOT / "src" / "libs" / "bootstrap.py"


def load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("bootstrap_pip_under_test", BOOTSTRAP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BootstrapPipInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_bootstrap_module()

    def test_missing_packages_are_installed_in_one_cached_batch(self):
        b = self.bootstrap
        installed = {
            "pyserial": False,
            "psutil": False,
            "certifi": False,
        }

        def mark_installed(args, **kwargs):
            for pkg in ("pyserial", "psutil", "certifi"):
                if pkg in args:
                    installed[pkg] = True
            return True

        specs = [
            {
                "id": "pyserial",
                "name": "pyserial",
                "check": lambda: installed["pyserial"],
                "pip_args": ["pyserial"],
                "critical": True,
            },
            {
                "id": "psutil",
                "name": "psutil",
                "check": lambda: installed["psutil"],
                "pip_args": ["psutil"],
                "critical": True,
            },
            {
                "id": "certifi",
                "name": "certifi",
                "check": lambda: installed["certifi"],
                "pip_args": ["certifi", "pyserial"],
                "critical": True,
            },
        ]

        with (
            mock.patch.object(b, "PIP_PACKAGES_SPEC", specs),
            mock.patch.object(b, "_run_pip_install", side_effect=mark_installed) as run_pip,
            mock.patch.object(b.sys, "argv", ["bootstrap.py"]),
        ):
            self.assertTrue(b.ensure_pip_packages_parallel(None))

        run_pip.assert_called_once()
        args, kwargs = run_pip.call_args
        self.assertEqual(
            args[0],
            ["--no-warn-script-location", "pyserial", "psutil", "certifi"],
        )
        self.assertTrue(kwargs["use_cache"])
        self.assertTrue(kwargs["only_binary"])
        self.assertGreaterEqual(kwargs["timeout"], 600)


if __name__ == "__main__":
    unittest.main()
