import os
import shutil
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = PROJECT_ROOT / "src" / "modules" / "bootstrap.py"


class PycachePurgeTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="mcu_flasher_pycache_test_"))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_purge_python_cache_removes_pycache_dirs_and_pyc_files(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("bootstrap_under_test", BOOTSTRAP_PATH)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)

        # Create dummy pycache directory and pyc file
        sub_dir = self.test_dir / "src" / "modules"
        sub_dir.mkdir(parents=True, exist_ok=True)
        
        pycache_dir = sub_dir / "__pycache__"
        pycache_dir.mkdir(exist_ok=True)
        (pycache_dir / "test.cpython-311.pyc").write_bytes(b"dummy bytecode")
        
        pyc_file = sub_dir / "legacy_module.pyc"
        pyc_file.write_bytes(b"dummy pyc")
        
        source_file = sub_dir / "valid_module.py"
        source_file.write_text("print('hello')", encoding="utf-8")

        # Run purge
        bootstrap.purge_python_cache(self.test_dir)

        self.assertFalse(pycache_dir.exists())
        self.assertFalse(pyc_file.exists())
        self.assertTrue(source_file.exists())


if __name__ == "__main__":
    unittest.main()
