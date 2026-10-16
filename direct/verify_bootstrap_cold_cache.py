"""Exercise first-run import visibility with unchanged directory timestamps.

Only source-extracted Windows helpers run; downloads/installers are mocked.
All files stay under temp/audit, and no live runtime or package store is changed.
"""
from __future__ import annotations

import ast
import concurrent.futures
import importlib
import importlib.machinery
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Optional
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ColdCacheChecks(unittest.TestCase):
    def test_new_packages_verify_without_reinstall_on_coarse_timestamp_storage(self):
        audit = ROOT / "temp/audit"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            root = Path(directory)
            site = root / "site-packages"
            site.mkdir()
            temporary = root / "wheelhouse-root"
            temporary.mkdir()
            source = ROOT / "src/modules/bootstrap.py"
            tree = ast.parse(source.read_text(encoding="utf-8-sig"))
            names = {"_check_spec", "ensure_pip_packages_parallel"}
            scope = dict(Optional=Optional, BootstrapGUI=object, Path=Path,
                         os=os, sys=sys, shutil=shutil, importlib=importlib,
                         _get_target_site_packages_dirs=lambda: [str(site)],
                         _ensure_pywin32_system32_dlls=Mock(),
                         _is_network_reachable=lambda **kw: True,
                         _sync_private_python_site_packages=Mock(),
                         status=Mock(), ok=Mock(), warn=Mock(), fail=Mock(),
                         _LAST_PIP_ERROR="")
            exec(compile(ast.Module(body=[node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names],
                type_ignores=[]), str(source), "exec"), scope)
            check = lambda: scope["_check_spec"]("cold_fixture")
            scope["PIP_PACKAGES_SPEC"] = [dict(id="fixture", name="Fixture", check=check,
                                              pip_args=["fixture"], critical=True)]

            def install(*args, **kwargs):
                self.assertFalse(check())  # Prime the real PathFinder negative cache.
                before = site.stat()
                (site / "cold_fixture.py").write_text("VALUE = 1\n", encoding="utf-8")
                os.utime(site, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertFalse(check())  # Reproduce the bug before invalidation.
                return True

            scope["_run_pip_install"] = Mock(side_effect=install)

            class NoDownloadExecutor(concurrent.futures.ThreadPoolExecutor):
                def submit(self, function, *args, **kwargs):
                    if function.__name__ == "_download_one":
                        future = concurrent.futures.Future()
                        future.set_result((args[0]["id"], False, root, "Fixture skips network"))
                        return future
                    return super().submit(function, *args, **kwargs)

            with patch.object(concurrent.futures, "ThreadPoolExecutor", NoDownloadExecutor), \
                    patch.object(tempfile, "mkdtemp", return_value=str(temporary)):
                self.assertTrue(scope["ensure_pip_packages_parallel"]())
            scope["_run_pip_install"].assert_called_once()
            self.assertTrue(check())
            # Missing or removed modules must still fail; no cached success.
            (site / "cold_fixture.py").unlink()
            importlib.invalidate_caches()
            self.assertFalse(check())


if __name__ == "__main__":
    unittest.main(verbosity=2)
