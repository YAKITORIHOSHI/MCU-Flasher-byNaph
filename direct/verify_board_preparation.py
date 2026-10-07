#!/usr/bin/env python3
"""Hardware-free exact-board preparation/coverage checks; no live installs."""
from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from main.core import board_catalog
from src.modules import board_preparation as worker
from src.modules import offline_bootstrap

NAMES = {"nodemcu": "NodeMCU 0.9 (ESP-12 Module)", "nodemcuv2": "NodeMCU 1.0 (ESP-12E Module)"}
METADATA = {"package": "esp8266", "architecture": "esp8266", "name": "ESP8266 Boards",
            "boards": list(NAMES.values())}
BASE = {"schema": 1, "platforms": ["atmelavr", "espressif32", "espressif8266"],
        "frameworks": ["arduino"], "libraries": []}


class PreparationChecks(unittest.TestCase):
    def setUp(self):
        # Unknown families now perform bootstrap-only registry discovery. Every
        # in-process fixture remains offline; the real child below has no
        # declaration, so it never queries or installs a platform.
        registry = patch("src.modules.board_index_targets.fetch_preparation_catalog",
                         side_effect=ValueError("Fixture registry discovery disabled"))
        registry.start()
        self.addCleanup(registry.stop)
        scratch = ROOT / "temp/audit/board-preparation"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / "store"
        self.archive = self.root / "downloaded-esp8266"
        self.archive.mkdir()
        declarations = []
        for identifier, name in NAMES.items():
            declarations.extend([f"{identifier}.name={name}", f"{identifier}.build.mcu=esp8266",
                                 f"{identifier}.build.variant=nodemcu",
                                 f"{identifier}.build.board=ESP8266_NODEMCU{'_ESP12E' if identifier == 'nodemcuv2' else ''}"])
        (self.archive / "boards.txt").write_text("\n".join(declarations), encoding="utf-8")
        self.records = board_catalog._parse_downloaded_arduino_board_files(self.archive, force_read=True)
        folder = self.core / "platforms/espressif8266/boards"
        folder.mkdir(parents=True)
        (folder.parent / "platform.json").write_text('{"name":"espressif8266"}', encoding="utf-8")
        for identifier, name in NAMES.items():
            data = {"name": name, "vendor": "NodeMCU", "frameworks": ["arduino", "esp8266-rtos-sdk", "esp8266-nonos-sdk"],
                    "build": {"mcu": "esp8266", "variant": "nodemcu",
                              "extra_flags": "-DARDUINO_ESP8266_NODEMCU" + ("_ESP12E" if identifier == "nodemcuv2" else "")},
                    "upload": {"require_upload_port": True, "speed": 115200}}
            (folder / f"{identifier}.json").write_text(json.dumps(data), encoding="utf-8")
        self.catalog = board_catalog._load_platformio_board_catalog(self.core, force_read=True)
        signatures = {str(Path(row["manifest"]).relative_to(self.core)): hashlib.sha256(Path(row["manifest"]).read_bytes()).hexdigest()
                      for row in self.catalog}
        (self.core / offline_bootstrap.MARKER).write_text(json.dumps({"schema": offline_bootstrap.SCHEMA,
            "host": sys.platform, "prepared_plan": BASE, "board_manifests": signatures,
            "exact_builder_targets": [{"platform": row["platform"], "board": row["id"], "framework": "arduino"}
                                      for row in self.catalog]}), encoding="utf-8")

    def test_default_plan_contains_both_esp_families(self):
        self.assertIn("espressif8266", offline_bootstrap.load_plan()["platforms"])
        self.assertEqual(offline_bootstrap.load_plan()["frameworks"], ["arduino"])

    def test_external_sources_with_at_signs_keep_their_exact_identity(self):
        first = "https://fixture@host.invalid/first.git"
        second = "https://fixture@host.invalid/second.git"
        base = dict(BASE, platforms=[first])
        previous = dict(BASE, platforms=[second])
        plan = worker._merged_plan(base, previous)
        self.assertEqual(plan["platforms"], [first, second])

    def test_real_private_child_reports_unknown_board_without_installing(self):
        from src.modules import package_jobs
        folder = self.root / "unknown-board"
        folder.mkdir()
        (folder / "boards.txt").write_text("# This fixture archive has no board declaration.\n", encoding="utf-8")
        empty_store = self.root / "empty-store"
        empty_store.mkdir()
        events = self.root / "child-events"
        metadata = dict(package="fixture-vendor", architecture="mysterycpu", name="Fixture package",
                        boards=["Mystery Board"])
        # Exercise the real script, interpreter guard and file handoff. Its
        # unknown identity with an empty store cannot select any installer.
        with patch.object(worker.threading, "Thread"):
            process = worker.start_preparation(folder, job_id="real-fixture", package_metadata=metadata,
                                               core=empty_store, event_root=events)
        try:
            self.assertEqual(process.wait(timeout=30), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        snapshot = package_jobs.get_job_snapshot("real-fixture", events)
        self.assertEqual(snapshot["stage"], "unavailable")
        self.assertEqual(snapshot["details"]["ready_count"], 0)
        report = json.loads(Path(snapshot["details"]["coverage_report"]).read_text(encoding="utf-8"))
        self.assertEqual(len(report["boards"]), 1)
        self.assertEqual(report["boards"][0]["name"], "Mystery Board")
        # Publishing an empty source-bound target report is app metadata, not
        # a platform/core installation. No tools or package directories exist.
        self.assertEqual({path.name for path in empty_store.iterdir()}, {".mcu-index-targets.json"})

    def test_both_nodemcu_revisions_resolve_to_independent_exact_ids(self):
        report = worker.preparation_plan(self.records, package_metadata=METADATA, catalog=self.catalog, base_plan=BASE)
        self.assertEqual({row["board"] for row in report["boards"]}, {"nodemcu", "nodemcuv2"})
        for row in report["boards"]:
            self.assertEqual(row["name"], NAMES[row["board"]])
            self.assertEqual(row["platform"], "espressif8266")
            self.assertEqual(row["status"], "preparation_required")
        for record in self.records:
            cached = dict(platform="espressif8266", board="", framework="arduino", pio_resolved=False,
                          arduino_board_id=record["arduino_id"], arduino_variant=record["variant"],
                          arduino_build_board=record["build_board"], mcu=record["mcu"])
            repaired = board_catalog.resolve_board_definition(record["name"], cached, self.catalog)
            self.assertEqual(repaired["board"], record["arduino_id"])
            self.assertTrue(repaired["pio_resolved"])

    def test_missing_esp8266_packages_select_known_platform_without_guessing_ids(self):
        base = dict(BASE, platforms=["atmelavr", "espressif32"])
        report = worker.preparation_plan(self.records, package_metadata=METADATA, catalog=[], base_plan=base)
        self.assertIn("espressif8266", report["plan"]["platforms"])
        self.assertTrue(all(not row["board"] and row["status"] == "preparation_required" for row in report["boards"]))

    def test_unknown_custom_package_never_installs_from_mcu_guess(self):
        metadata = dict(METADATA, package="unreviewed-vendor")
        report = worker.preparation_plan(self.records, package_metadata=metadata, catalog=[], base_plan=BASE)
        self.assertEqual(report["plan"], BASE)
        self.assertTrue(all(row["status"] == "unavailable" for row in report["boards"]))
        self.assertTrue(all(not row["platform"] for row in report["boards"]))

    def test_ambiguous_definitions_remain_unresolved(self):
        record = dict(self.records[0], arduino_id="unknown", name="Generic ESP8266", build_board="")
        copies = [dict(self.catalog[0], id="one", name="Same fixture"),
                  dict(self.catalog[0], id="two", name="Same fixture")]
        report = worker.preparation_plan([record], package_metadata=dict(METADATA, boards=[]), catalog=copies, base_plan=BASE)
        self.assertEqual(report["boards"][0]["board"], "")

    def test_preserves_prior_explicit_framework_and_library_scope(self):
        previous = {"schema": 1, "platforms": ["ststm32"], "frameworks": ["mbed", "arduino"],
                    "libraries": ["owner/custom"]}
        merged = worker._merged_plan(BASE, previous, ["espressif8266"])
        self.assertEqual(merged["frameworks"], ["arduino", "mbed"])
        self.assertIn("ststm32", merged["platforms"])
        self.assertIn("owner/custom", merged["libraries"])
        complete = dict(previous)
        complete.pop("frameworks")
        self.assertNotIn("frameworks", worker._merged_plan(BASE, complete))

    def test_previous_pinned_platform_is_preserved_without_unpinned_install(self):
        previous = dict(BASE, platforms=["platformio/espressif8266@4.2.1"])
        merged = worker._merged_plan(BASE, previous, ["espressif8266"])
        self.assertIn("platformio/espressif8266@4.2.1", merged["platforms"])
        self.assertNotIn("espressif8266", merged["platforms"])
        self.assertIn("atmelavr", merged["platforms"])

    def test_current_explicit_pin_takes_precedence_over_previous_pin(self):
        previous = dict(BASE, platforms=["platformio/espressif8266@4.2.1"])
        current = dict(BASE, platforms=["platformio/espressif8266@4.2.2"])
        merged = worker._merged_plan(current, previous, ["espressif8266"])
        self.assertEqual(merged["platforms"], ["platformio/espressif8266@4.2.2"])

    def test_distinct_library_owners_are_preserved_and_platform_conflicts_rejected(self):
        previous = dict(BASE, libraries=["owner-a/Same@1.0"])
        current = dict(BASE, libraries=["owner-b/Same@2.0"])
        self.assertEqual(worker._merged_plan(current, previous)["libraries"], ["owner-b/Same@2.0", "owner-a/Same@1.0"])
        with self.assertRaisesRegex(ValueError, "Conflicting platform"):
            worker._merged_plan(dict(BASE, platforms=["owner-a/custom@1.0"]),
                                dict(BASE, platforms=["owner-b/custom@2.0"]))

    def test_every_index_name_has_truthful_coverage_even_if_archive_omits_it(self):
        metadata = dict(METADATA, boards=list(NAMES.values()) + ["Omitted board"])
        report = worker.preparation_plan(self.records, package_metadata=metadata, catalog=self.catalog, base_plan=BASE)
        self.assertEqual(len(report["boards"]), 3)
        self.assertEqual(report["boards"][-1]["status"], "unavailable")
        self.assertIn("no declaration", report["boards"][-1]["reason"])

    def test_explicit_framework_exclusion_cannot_be_ready(self):
        report = worker.preparation_plan(self.records, package_metadata=METADATA, catalog=self.catalog,
                                         base_plan=dict(BASE, frameworks=["esp8266-rtos-sdk"]))
        self.assertTrue(all(row["status"] == "unsupported" for row in report["boards"]))

    def test_only_verified_final_catalog_and_certificate_allow_ready(self):
        events = []
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "prepare") as prepare, \
                patch.object(offline_bootstrap, "ready", side_effect=[False, True, True]), \
                patch.object(board_catalog, "_load_platformio_board_catalog", side_effect=[[], self.catalog]):
            result = worker.run_preparation(self.core, self.archive, package_metadata=METADATA,
                                           emit=lambda stage, **details: events.append((stage, details)))
        prepare.assert_called_once()
        self.assertEqual(events[-1][0], "ready")
        self.assertEqual(events[-1][1]["ready_count"], 2)
        self.assertTrue(all(row["status"] == "ready" for row in result["boards"]))

    def test_missing_post_preparation_definition_stays_unavailable(self):
        events = []
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "prepare"), \
                patch.object(offline_bootstrap, "ready", side_effect=[False, True, True]), \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]):
            result = worker.run_preparation(self.core, self.archive, package_metadata=METADATA,
                                           emit=lambda stage, **details: events.append((stage, details)))
        self.assertEqual(events[-1][0], "unavailable")
        self.assertEqual(events[-1][1]["ready_count"], 0)
        self.assertTrue(all(row["status"] == "unavailable" for row in result["boards"]))

    def test_changed_definition_reprepares_even_with_existing_success_marker(self):
        manifest = Path(self.catalog[0]["manifest"])
        manifest.write_bytes(manifest.read_bytes() + b" ")
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "prepare") as prepare, \
                patch.object(offline_bootstrap, "ready", return_value=True), \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=self.catalog):
            result = worker.run_preparation(self.core, self.archive, package_metadata=METADATA, emit=Mock())
        prepare.assert_called_once()
        self.assertEqual(sum(row["status"] == "ready" for row in result["boards"]), 1)
        changed = next(row for row in result["boards"] if row["board"] == self.catalog[0]["id"])
        self.assertEqual(changed["status"], "unavailable")

    def test_preparation_failure_cannot_emit_ready(self):
        events = []
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "prepare", side_effect=RuntimeError("fixture tool missing")), \
                patch.object(offline_bootstrap, "ready", return_value=False), \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "fixture tool missing"):
                worker.run_preparation(self.core, self.archive, package_metadata=METADATA,
                                       emit=lambda stage, **details: events.append((stage, details)))
        self.assertFalse(any(stage == "ready" for stage, _details in events))

    def test_workspace_role_rejected_before_any_package_access(self):
        with patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1"}), \
                patch.object(board_catalog, "_load_platformio_board_catalog") as catalog, \
                patch.object(offline_bootstrap, "prepare") as prepare:
            with self.assertRaisesRegex(RuntimeError, "outside"):
                worker.run_preparation(self.core, self.archive, emit=Mock())
        catalog.assert_not_called()
        prepare.assert_not_called()

    def test_spawn_uses_verified_current_runtime_and_removes_offline_role(self):
        with patch("src.modules.private_python_guard.is_running_private_python", return_value=True), \
                patch.object(worker.subprocess, "Popen") as start, \
                patch.object(worker.threading, "Thread"), \
                patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1", "PIP_NO_INDEX": "1", "PYTHONPATH": "foreign"}):
            worker.start_preparation(self.archive, job_id="fixture-job", package_metadata=METADATA,
                                     core=self.core, event_root=self.root / "events")
        args, kwargs = start.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(args[0][1], "-B")
        self.assertEqual(args[0][args[0].index("--core") + 1], str(self.core))
        self.assertNotIn("MCU_FLASHER_OFFLINE_RUNTIME", kwargs["env"])
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        request = Path(args[0][args[0].index("--package-request") + 1])
        self.assertEqual(json.loads(request.read_text(encoding="utf-8"))["boards"], METADATA["boards"])

    def test_large_index_metadata_uses_short_file_handoff_without_losing_names(self):
        metadata = dict(METADATA, boards=[f"Board {number}: " + "x" * 100 for number in range(1200)])
        with patch("src.modules.private_python_guard.is_running_private_python", return_value=True), \
                patch.object(worker.subprocess, "Popen") as start, patch.object(worker.threading, "Thread"):
            worker.start_preparation(self.archive, job_id="fixture-many", package_metadata=metadata,
                                     core=self.core, event_root=self.root / "events")
        command = start.call_args.args[0]
        self.assertLess(sum(len(argument) for argument in command), 4000)
        request = Path(command[command.index("--package-request") + 1])
        self.assertEqual(len(json.loads(request.read_text(encoding="utf-8"))["boards"]), 1200)

    def test_early_child_exit_becomes_failure_and_terminal_child_event_is_preserved(self):
        from src.modules import package_jobs
        child = Mock()
        child.wait.return_value = 1
        events = self.root / "events"
        package_jobs.publish_event("fixture-exit", "queued", root=events, message="Queued download")
        worker._watch_preparation(child, "fixture-exit", events)
        snapshot = package_jobs.get_job_snapshot("fixture-exit", root=events)
        self.assertEqual(snapshot["stage"], "failed")
        self.assertIn("before reporting", snapshot["message"])
        package_jobs.publish_event("fixture-success", "ready", root=events, message="Prepared exactly")
        worker._watch_preparation(child, "fixture-success", events)
        snapshot = package_jobs.get_job_snapshot("fixture-success", root=events)
        self.assertEqual(snapshot["stage"], "ready")
        self.assertEqual(snapshot["message"], "Prepared exactly")

    def test_watcher_resource_failure_does_not_replay_or_report_child_failed(self):
        from src.modules import package_jobs
        with patch("src.modules.private_python_guard.is_running_private_python", return_value=True), \
                patch.object(worker.subprocess, "Popen") as start, \
                patch.object(worker.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("fixture thread quota")
            process = worker.start_preparation(self.archive, job_id="fixture-watch", package_metadata=METADATA,
                                               core=self.core, event_root=self.root / "events")
        start.assert_called_once()
        self.assertEqual(process.preparation_tracking_error, "fixture thread quota")
        self.assertIsNone(package_jobs.get_job_snapshot("fixture-watch", root=self.root / "events"))

    def test_worker_writes_complete_coverage_instead_of_truncating_event(self):
        from src.modules import package_jobs
        request = package_jobs.write_request("fixture-many", METADATA, root=self.root / "events")
        rows = [{"name": f"Board {number}", "status": "unavailable", "reason": "Fixture"} for number in range(100)]
        def run(core, archive, *, emit, **kwargs):
            emit("unavailable", boards=rows, ready_count=0, unavailable_count=100, message="Fixture coverage")
        with patch("src.modules.private_python_guard.is_running_private_python", return_value=True), \
                patch.object(worker, "run_preparation", side_effect=run):
            status = worker.main(["--core", str(self.core), "--boards", str(self.archive), "--job-id", "fixture-many",
                                  "--package-request", str(request), "--events-root", str(self.root / "events")])
        self.assertEqual(status, 0)
        event = json.loads((self.root / "events/fixture-many.json").read_text(encoding="utf-8"))
        report = json.loads(Path(event["details"]["coverage_report"]).read_text(encoding="utf-8"))
        self.assertEqual(len(report["boards"]), 100)
        self.assertEqual(report["unavailable_count"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
