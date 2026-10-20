#!/usr/bin/env python3
"""Selected output/root escape regressions, entirely inside owned canaries."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("native_smoke_paths", Path(__file__).with_name("verify-smoke.py"))
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class PathChecks(unittest.TestCase):
    def fixture(self):
        scratch = SMOKE.ROOT / "audit/migration/scratch"
        location = tempfile.TemporaryDirectory(prefix="native-ui-paths-", dir=scratch)
        self.addCleanup(location.cleanup)
        owned = Path(location.name)
        root = owned / "checkout"
        (root / "audit/migration").mkdir(parents=True)
        outside = owned / "outside-checkout"
        outside.mkdir()
        return root, outside

    def test_symlinked_scratch_root_is_refused(self):
        root, outside = self.fixture()
        (root / "audit/migration/scratch").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            SMOKE.OutputDirectory(root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_output_symlink_does_not_change_its_canary(self):
        for name in ("latest-output.log", "latest-smoke.json"):
            with self.subTest(name=name):
                root, outside = self.fixture()
                canary = outside / "must-stay.txt"
                canary.write_bytes(b"KEEP")
                output = SMOKE.OutputDirectory(root)
                self.addCleanup(output.close)
                (output.path / name).symlink_to(canary)
                with self.assertRaises((OSError, ValueError)):
                    output.publish(name, b"REPLACEMENT")
                self.assertEqual(canary.read_bytes(), b"KEEP")
                self.assertTrue((output.path / name).is_symlink())

    def test_atomic_replacement_does_not_write_through_hardlink(self):
        root, outside = self.fixture()
        canary = outside / "hardlink-canary.txt"
        canary.write_bytes(b"KEEP")
        output = SMOKE.OutputDirectory(root)
        self.addCleanup(output.close)
        (output.path / "latest-smoke.json").hardlink_to(canary)
        output.publish("latest-smoke.json", b'{"passed":true}\n')
        self.assertEqual(canary.read_bytes(), b"KEEP")
        self.assertTrue(json.loads((output.path / "latest-smoke.json").read_bytes())["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
