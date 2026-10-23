#!/usr/bin/env python3
"""Measure warm local package publication on owned temp/ payloads only.

Compares the former copy + source hash + staged hash/sync with current verified
publication. No downloads, installers, live package stores, GUI or hardware.
Results describe this filesystem/cache/host; they are not cold HDD or native
Windows evidence when collected on Linux. Windows retains native CopyFile2.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.modules import bootstrap_package_recovery as recovery


def _owned_output(value):
    output = Path(value).absolute()
    if not output.resolve().is_relative_to((ROOT / "temp").resolve()):
        raise argparse.ArgumentTypeError("Benchmark output must stay inside this checkout's temp/ directory")
    return output


def _payload(source, total_bytes, files):
    source.mkdir()
    chunk = bytes(range(256)) * 256
    quotient, extra = divmod(total_bytes, files)
    for number in range(files):
        directory = source / f"group-{number // 128:04d}"
        directory.mkdir(exist_ok=True)
        size = quotient + (number < extra)
        with (directory / f"payload-{number:06d}.bin").open("xb") as stream:
            while size:
                data = chunk[:min(size, len(chunk))]
                stream.write(data)
                size -= len(data)
    (source / "empty-file").touch()
    (source / "empty-directory").mkdir()


def _baseline(source, core):
    """The old copy/2hash publication, with its same Linux synchronization."""
    from platformio.package.manager import _install as installer
    from src.modules.bootstrap_platformio import archive_paths
    stage_parent = core / ".mcu-package-staging"
    stage_parent.mkdir()
    staging = stage_parent / uuid4().hex
    target = core / "packages/fixture"
    target.parent.mkdir()
    phases = {"source_verification_calls": 1, "stage_verification_calls": 1}
    with archive_paths():
        captured = installer.shutil
        started = time.perf_counter()
        before = time.perf_counter()
        captured.copytree(source, staging, symlinks=True)
        phases["copy_seconds"] = time.perf_counter() - before
    before = time.perf_counter()
    expected = recovery._tree_signature(source)
    phases["source_verification_seconds"] = time.perf_counter() - before
    before = time.perf_counter()
    actual = recovery._tree_signature(staging, sync=True)
    phases["stage_verification_and_sync_seconds"] = time.perf_counter() - before
    if expected != actual:
        raise RuntimeError("Baseline publication failed byte verification")
    recovery._sync_dir(stage_parent)
    recovery._publish_absent(staging, target)
    recovery._sync_dir(stage_parent)
    phases["total_seconds"] = time.perf_counter() - started
    return phases


def _current(source, core):
    from platformio.package.manager import _install as installer
    from src.modules.bootstrap_platformio import archive_paths
    phases = {"copy_seconds": 0.0, "source_inventory_seconds": 0.0,
              "source_verification_seconds": 0.0, "stage_verification_and_sync_seconds": 0.0,
              "source_verification_calls": 0, "stage_verification_calls": 0}
    inventory, signature = recovery._source_inventory, recovery._tree_signature

    def observe_inventory(*args, **kwargs):
        before = time.perf_counter()
        try:
            return inventory(*args, **kwargs)
        finally:
            phases["source_inventory_seconds"] += time.perf_counter() - before

    def observe_signature(folder, **kwargs):
        key = "stage_verification_and_sync_seconds" if kwargs.get("sync") else "source_verification_seconds"
        phases["stage_verification_calls" if kwargs.get("sync") else "source_verification_calls"] += 1
        before = time.perf_counter()
        try:
            return signature(folder, **kwargs)
        finally:
            phases[key] += time.perf_counter() - before

    target = core / "packages/fixture"
    target.parent.mkdir()
    with archive_paths():
        captured = installer.shutil
        class CopyTimer:
            def __getattr__(self, name):
                return getattr(captured, name)
            def copytree(self, *args, **kwargs):
                before = time.perf_counter()
                try:
                    return captured.copytree(*args, **kwargs)
                finally:
                    phases["copy_seconds"] += time.perf_counter() - before

        with patch.object(installer, "shutil", CopyTimer()), \
                patch.object(recovery, "_source_inventory", observe_inventory), \
                patch.object(recovery, "_tree_signature", observe_signature), \
                recovery.atomic_package_writes(core):
            started = time.perf_counter()
            installer.shutil.copytree(source, target, symlinks=True)
            phases["total_seconds"] = time.perf_counter() - started
    return phases


@contextmanager
def _fixture_environment(core, events):
    values = {"PLATFORMIO_CORE_DIR": str(core), "MCU_PACKAGE_EVENTS_ROOT": str(events),
              "PLATFORMIO_PACKAGES_DIR": str(core / "packages"),
              "PLATFORMIO_PLATFORMS_DIR": str(core / "platforms"),
              "PLATFORMIO_GLOBALLIB_DIR": str(core / "lib"),
              "PLATFORMIO_CACHE_DIR": str(core / ".cache")}
    with patch.dict(os.environ, values):
        yield


def benchmark(*, output, size_mib=32, small_files=4096, repeats=3):
    output = _owned_output(output)
    if os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME") or os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME"):
        raise RuntimeError("Run the owned benchmark with the private Bootstrap interpreter outside the workspace process")
    audit = ROOT / "temp/audit/bootstrap-io-performance"
    audit.mkdir(parents=True, exist_ok=True)
    report = {"schema": 1, "host": sys.platform, "architecture": platform.machine(),
              "python": sys.version.split()[0], "filesystem_fixture_parent": str(audit),
              "cache_scope": "warm local fixture; no cache eviction or cold-storage claim",
              "installers": False, "network": False, "hardware": False,
              "current_copy_policy": "large-payload-posix-stream; original copier otherwise",
              "stream_min_bytes": recovery.STREAM_MIN_BYTES,
              "stream_min_average_file_bytes": recovery.STREAM_MIN_AVERAGE,
              "payload_read_model": "Logical main-stream bytes; excludes metadata, filesystem caches and physical I/O",
              "repeats": repeats, "workloads": []}
    with tempfile.TemporaryDirectory(prefix="owned-", dir=audit) as temporary:
        root = Path(temporary)
        for shape, files in (("few_large_files", 4), ("many_small_files", small_files)):
            source = root / shape
            payload_bytes = size_mib * 1024 * 1024
            optimized = (sys.platform != "win32" and payload_bytes >= recovery.STREAM_MIN_BYTES
                         and payload_bytes >= (files + 1) * recovery.STREAM_MIN_AVERAGE)
            _payload(source, payload_bytes, files)
            trials = {"baseline": [], "current": []}
            for repeat in range(repeats):
                # Alternate order to avoid always granting one path first use.
                order = ("baseline", "current") if repeat % 2 == 0 else ("current", "baseline")
                for policy in order:
                    core = root / f"{shape}-{policy}-{repeat}"
                    core.mkdir()
                    with _fixture_environment(core, root / "package-events"):
                        phases = (_baseline if policy == "baseline" else _current)(source, core)
                    if policy == "current" and phases["source_verification_calls"] != (0 if optimized else 1):
                        raise AssertionError("Observed publication path does not match the recorded read-byte model")
                    trials[policy].append(phases)
                    shutil.rmtree(core)  # Outside the timed publication.
            baseline = statistics.median(item["total_seconds"] for item in trials["baseline"])
            current = statistics.median(item["total_seconds"] for item in trials["current"])
            workload = {"shape": shape, "payload_bytes": payload_bytes,
                        "regular_file_count": files + 1, "directory_count": (files + 127) // 128 + 2,
                        "current_copy_policy": "streamed-source-hash" if optimized else "original-copy2-and-two-hashes",
                        "baseline_logical_payload_read_bytes": payload_bytes * 3,
                        "current_logical_payload_read_bytes": payload_bytes * (2 if optimized else 3),
                        "baseline_median_seconds": baseline, "current_median_seconds": current,
                        "baseline_over_current_ratio": baseline / current,
                        "trials": trials}
            report["workloads"].append(workload)
            print(f"{shape}: {files + 1} files, {size_mib} MiB; baseline {baseline:.3f}s, current {current:.3f}s", flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Warm local fixture report: {output}", flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=_owned_output,
                        default=ROOT / "temp/audit/bootstrap-io-performance/result.json")
    parser.add_argument("--size-mib", type=int, default=32, help="Payload size for each workload (1 to 1024 MiB)")
    parser.add_argument("--small-files", type=int, default=4096, help="Many-file workload count (16 to 32768)")
    parser.add_argument("--repeats", type=int, default=3, help="Publication repetitions per policy/workload (2 to 9)")
    args = parser.parse_args(argv)
    if not 1 <= args.size_mib <= 1024 or not 16 <= args.small_files <= 32768 or not 2 <= args.repeats <= 9:
        parser.error("Use size-mib 1..1024, small-files 16..32768 and repeats 2..9")
    benchmark(output=args.output, size_mib=args.size_mib, small_files=args.small_files, repeats=args.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
