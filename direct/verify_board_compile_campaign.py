#!/usr/bin/env python3
"""Offline checks for the opt-in board campaign; all writes use owned scratch."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct import board_compile_campaign as campaign
from direct import board_compile_indexes as indexes
from direct import board_compile_manifest as manifest


class CampaignChecks(unittest.TestCase):
    def setUp(self):
        base = ROOT / "temp/audit/board-campaign-verifier"
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_manifest_accepts_literal_plus_and_refuses_flags_paths_and_ini_injection(self):
        path = self.root / "targets.json"
        for board, valid in (("STC89C516RD+", True), ("board.v2", True), ("-flag", False),
                             ("../board", False), ("board/child", False), ("board\nextra=1", False)):
            with self.subTest(board=board):
                path.write_text(json.dumps({"schema": 1, "targets": [{"platform": "platformio/test@1.0.0",
                                "board": board, "framework": "arduino"}]}))
                if valid:
                    self.assertEqual(campaign.load_manifest(path)[0][0]["board"], board)
                else:
                    with self.assertRaises(ValueError):
                        campaign.load_manifest(path)

    def archive(self, name, *, metadata_link=False):
        path = self.root / name
        with tarfile.open(path, "w") as archive:
            content = b"fixture.name=Fixture Board\nfixture.build.mcu=atmega328p\n"
            info = tarfile.TarInfo("core/boards.txt")
            if metadata_link:
                info.type = tarfile.SYMTYPE
                info.linkname = "../outside/boards.txt"
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            link = tarfile.TarInfo("core/unused-link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            archive.addfile(link)
        return path

    def test_metadata_extraction_ignores_unrelated_links_and_refuses_selected_links(self):
        destination = self.root / "metadata"
        receipt = indexes.extract_metadata(self.archive("safe.tar"), destination)
        self.assertEqual(len(receipt["sources"]), 1)
        self.assertFalse((self.root / "outside").exists())
        with self.assertRaises(ValueError):
            indexes.extract_metadata(self.archive("unsafe.tar", metadata_link=True), self.root / "refused")

    def test_owned_cleanup_cannot_remove_arbitrary_scratch_child(self):
        owned = campaign.owned_root(self.root / "owned", False)
        retained = owned / "retained"
        retained.mkdir()
        (retained / "user.txt").write_text("retain")
        with self.assertRaises(ValueError):
            campaign.remove_owned(retained, owned)
        self.assertEqual((retained / "user.txt").read_text(), "retain")
        campaign.remove_owned(owned / "work", owned)
        self.assertTrue(retained.is_dir())

    @unittest.skipUnless(os.name == "posix", "Native symlink refusal")
    def test_owned_root_refuses_symlink_ancestors(self):
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            campaign.owned_root(alias / "campaign", False)
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "Native owned process-group check")
    def test_real_child_log_is_bounded_and_timeout_is_explicit(self):
        owned = campaign.owned_root(self.root / "owned", False)
        work = owned / "work"
        environment = campaign.child_environment(work, 1)
        options = dict(environment=environment, cwd=work, scratch=owned, reserve=0,
                       cap=16 * 1024 * 1024, log_limit=1024)
        log = owned / "logs/output.gz"
        result = campaign.run_child([sys.executable, "-B", "-c", "print('x' * 10000)"],
                                    log_path=log, timeout=5, **options)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["log_truncated"])
        with gzip.open(log, "rb") as stream:
            self.assertLess(len(stream.read()), 1200)
        result = campaign.run_child([sys.executable, "-B", "-c", "import time; time.sleep(10)"],
                                    log_path=owned / "logs/timeout.gz", timeout=.2, **options)
        self.assertEqual(result["status"], "timed_out")
        self.assertLess(result["elapsed_seconds"], 5)

    def test_artifact_receipts_require_real_bytes(self):
        project = self.root / "project"
        output = project / ".pio/build/campaign"
        output.mkdir(parents=True)
        blob = b"controlled firmware bytes"
        (output / "firmware.bin").write_bytes(blob)
        rows = campaign.firmware_receipts(project)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(rows[0]["bytes"], len(blob))

    @unittest.skipUnless(sys.platform == "linux", "Linux owned-session regression")
    def test_successful_leader_cleans_its_redirected_descendant(self):
        for stubborn in (False, True):
            with self.subTest(stubborn=stubborn):
                owned = campaign.owned_root(self.root / ("stubborn" if stubborn else "ordinary"), False)
                work = owned / "work"
                environment = campaign.child_environment(work, 1)
                child = "import signal,time;" + ("signal.signal(signal.SIGTERM,signal.SIG_IGN);" if stubborn else "") + "print('ready',flush=True);time.sleep(60)"
                leader = "import subprocess,sys; c=subprocess.Popen([sys.executable,'-c',sys.argv[1]],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL); assert c.stdout.readline()==b'ready\\n'; print(c.pid,flush=True)"
                result = campaign.run_child([sys.executable, "-c", leader, child], environment=environment,
                            cwd=work, log_path=owned / "logs/descendant.gz", scratch=owned,
                            timeout=10, reserve=0, cap=16 * 1024 * 1024, log_limit=1024)
                self.assertEqual(result["status"], "succeeded")
                try:
                    descriptor = os.pidfd_open(int(result["tail"].strip()))
                except ProcessLookupError:
                    continue  # Cleanup completed and the OS already reaped the orphan.
                try:
                    self.assertTrue(select.select([descriptor], [], [], 1)[0], "Successful leader left its owned descendant alive")
                finally:
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.close(descriptor)

    @unittest.skipUnless(sys.platform == "linux", "Linux retained child identity")
    def test_externally_reaped_child_refuses_group_signaling(self):
        child = campaign.OwnedLinuxChild([sys.executable, "-c", "pass"])
        child.process.wait(timeout=5)
        with mock.patch.object(os, "killpg", side_effect=AssertionError("Stale group must not be signaled")):
            with self.assertRaisesRegex(RuntimeError, "reaped elsewhere"):
                campaign.terminate(child)

    def test_unqualified_native_hosts_refuse_before_launch(self):
        with mock.patch.object(campaign.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "qualified only on Linux"):
                campaign.run_child([], environment={}, cwd=self.root, log_path=self.root / "unused",
                                   scratch=self.root, timeout=1, reserve=0, cap=1, log_limit=1)

    def test_platform_owner_fallback_requires_one_complete_exact_package(self):
        owner = "fixture-owner"
        item = {"name": "fixture", "type": "platform", "owner": {"username": owner}}
        metadata = {**item, "version": {"name": "1.0.0-rc.4"}, "tier": "community"}

        def pin(search, package):
            replies = [HTTPError("https://fixture", 404, "missing", {}, None), search, package]
            def fetch(request, **kwargs):
                reply = replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return io.BytesIO(json.dumps(reply).encode())
            with mock.patch.object(manifest, "urlopen", side_effect=fetch):
                return manifest.platform_pin("fixture")

        search = {"total": 1, "items": [item]}
        result = pin(search, metadata)
        self.assertEqual(result["platform_spec"], "fixture-owner/fixture@1.0.0-rc.4")
        self.assertTrue(result["experimental"])
        self.assertEqual(result["owner_resolution"]["exact_owner_count"], 1)
        for invalid in ({"total": 2, "items": [item]}, {"total": 2, "items": [item, item]},
                        {"total": 1, "items": [{**item, "owner": {"username": "../escape"}}]},
                        {"total": 1, "items": [{**item, "name": "other"}]}):
            with self.subTest(search=invalid), self.assertRaises(ValueError):
                pin(invalid, metadata)
        with self.assertRaises(ValueError):
            pin(search, {**metadata, "owner": {"username": "other-owner"}})

    def test_cpu_architecture_never_overrides_a_specific_part_conflict(self):
        row = {"mcu": "cortex-m0plus", "properties": {"build.extra_flags": "-D__SAMD21G18A__"}}
        record, evidence, problem = manifest.declaration_record(row)
        self.assertIsNone(problem)
        self.assertEqual(record["mcu"], "samd21g18a")
        self.assertTrue(evidence["cpu_architecture"])
        row["mcu"] = "atmega328p"
        self.assertIsNotNone(manifest.declaration_record(row)[2])

    def test_enriched_match_cannot_compile_replaced_board_definition(self):
        identity = {"board_manifest_sha256": "a" * 64, "platform_manifest_sha256": "b" * 64}
        target = {"provenance": {"expected_" + key: value for key, value in identity.items()}}
        campaign.verify_expected_identity(target, identity)
        for key in identity:
            with self.subTest(key=key), self.assertRaises(ValueError):
                campaign.verify_expected_identity(target, {**identity, key: "c" * 64})

    def test_shared_variant_or_usb_and_cpu_do_not_certify_a_different_board(self):
        for declaration, other, shared in (
            ({"arduino_id": "nicla_voice", "name": "Arduino Nicla Voice", "variant": "NICLA", "build_board": "NICLA"},
             {"id": "nicla_sense_me", "name": "Arduino Nicla Sense ME", "variant": "NICLA", "arduino_defines": ["arduinonicla"]}, {}),
            ({"arduino_id": "stella", "name": "Arduino Stella"},
             {"id": "nano33ble", "name": "Arduino Nano 33 BLE"}, {"hwids": [[0x2341, 0x805a]]}),
        ):
            with self.subTest(source=declaration["arduino_id"]):
                source = {**declaration, **shared, "mcu": "cortex-m4", "properties": {}}
                candidate = {**other, **shared, "mcu": "nrf52840", "frameworks": ["arduino"],
                             "platform": "nordicnrf52", "catalog_source": "installed-manifest", "build_cpu": "cortex-m4",
                             "board_manifest_sha256": "a" * 64,
                             "manifest": "fixture/boards/board.json", "installed_platform_version": "1.0.0"}
                inventory = {"packages": [{"status": "inventoried", "declarations": [source]}]}
                pins = {"nordicnrf52": {"platform_spec": "platformio/nordicnrf52@1.0.0", "version": "1.0.0"}}
                result = manifest.resolve(inventory, [candidate], pins)
                self.assertEqual(result["targets"], [])
                self.assertEqual(result["declarations"][0]["status"], "unresolved_mapping")


if __name__ == "__main__":
    unittest.main(verbosity=2)
