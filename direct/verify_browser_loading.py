"""Isolated catalog, scan and Tk responsiveness checks; no live networking."""
from __future__ import annotations
import json
import hashlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import arduino_lib_req as browser
from src.modules import browser_loading as loading
from direct.verify_controls import DownloaderChecks


def flush_writes():
    writer = loading._WRITER
    if writer:
        writer.join(5)
        assert not writer.is_alive(), 'Cache writer did not finish'


def record(version='1.0.0'):
    return {'Sensor': {'name': 'Sensor', 'versions': [dict(version=version, url='https://example.invalid/sensor.zip',
                                                           checksum='SHA-256:fixture', archiveFileName='sensor.zip')]}}


class CacheChecks(unittest.TestCase):
    def setUp(self):
        parent = ROOT / 'temp/audit/downloader'
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.addCleanup(flush_writes)
        self.folder = Path(temporary.name)
        self.source = self.folder / 'source.json'
        self.source.write_text('{"libraries":[]}', encoding='utf-8')
        self.cache = str(self.folder / 'catalog.json')
        loading._CATALOGS.clear()
        browser._INDEX_JSON_RAM_CACHE.clear()

    def touch_source(self):
        stat = self.source.stat()
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    def test_memory_disk_hit_and_source_replacement(self):
        build = Mock(return_value=record())
        first = loading.load_catalog(self.cache, [self.source], build)
        self.assertIs(loading.load_catalog(self.cache, [self.source], build), first)
        build.assert_called_once()
        flush_writes()
        loading._CATALOGS.clear()
        self.assertEqual(loading.load_catalog(self.cache, [self.source], Mock(side_effect=AssertionError('Regrouped'))), first)
        self.touch_source()
        updated = loading.load_catalog(self.cache, [self.source], lambda: record('2.0.0'))
        self.assertEqual(updated['Sensor']['versions'][0]['version'], '2.0.0')

    def test_corrupt_derived_cache_recovers_and_atomic_failure_preserves(self):
        loading.load_catalog(self.cache, [self.source], record)
        flush_writes()
        Path(self.cache).write_text('broken JSON', encoding='utf-8')
        loading._CATALOGS.clear()
        self.assertEqual(loading.load_catalog(self.cache, [self.source], record), record())
        flush_writes()
        before = Path(self.cache).read_bytes()
        self.touch_source()
        with patch.object(loading.os, 'replace', side_effect=OSError('Read-only cache')):
            self.assertEqual(loading.load_catalog(self.cache, [self.source], lambda: record('2.0.0')), record('2.0.0'))
            flush_writes()
        self.assertEqual(Path(self.cache).read_bytes(), before)
        self.assertEqual(list(self.folder.glob('*.tmp-*')), [])

    def test_raw_memory_budget_and_schema_validation(self):
        for index in range(8):
            path = self.folder / f'raw-{index}.json'
            path.write_text('{"libraries":[]}', encoding='utf-8')
            self.assertEqual(browser._read_index_cache(str(path), 'libraries'), {'libraries': []})
        self.assertLessEqual(len(browser._INDEX_JSON_RAM_CACHE), 4)
        self.assertIsNone(browser._read_index_cache(str(path), 'packages'))
        huge = self.folder / 'huge.json'
        huge.write_text(json.dumps(dict(libraries=[], padding='x' * 2_000_000)), encoding='utf-8')
        self.assertIsNotNone(browser._read_index_cache(str(huge), 'libraries'))
        self.assertNotIn(str(huge), browser._INDEX_JSON_RAM_CACHE)

    def test_versions_vendor_identity_and_archive_metadata_preserved(self):
        releases = [dict(name='Sensor', version=version, url='https://example.invalid/tool.zip',
                         checksum='SHA-256:fixture', archiveFileName='tool.zip')
                    for version in ('2.0.0-rc.1', '1.0.0', '2.0.0')]
        grouped = browser._group_libraries([None, *releases])
        self.assertEqual([v['version'] for v in grouped['Sensor']['versions']], ['2.0.0', '2.0.0-rc.1', '1.0.0'])
        self.assertEqual(grouped['Sensor']['versions'][0]['checksum'], 'SHA-256:fixture')
        packages = [dict(name=vendor, platforms=[dict(name='Device core', version='1.0', boards=[dict(name='MCU')],
                                                     url='https://example.invalid/core.zip')]) for vendor in ('one', 'two')]
        self.assertEqual(len(browser._group_boards(packages)), 2)

    def test_custom_index_shape_newlines_and_atomic_settings_failure(self):
        self.assertEqual(browser.parse_additional_board_urls("https://vendor.invalid/a.json\nhttps://vendor.invalid/b.json,https://vendor.invalid/a.json"),
                         ["https://vendor.invalid/a.json", "https://vendor.invalid/b.json"])
        for payload in ({"packages": [None]}, {"packages": [{"name": "vendor", "platforms": {}}]},
                        {"packages": [{"name": "vendor", "platforms": [{"name": "Core", "boards": {}}]}]}):
            with self.assertRaises(ValueError):
                browser._validate_index_payload(payload, "packages")
        settings = self.folder / "settings.json"
        settings.write_bytes(b'{"additional_board_urls":[]}')
        before = settings.read_bytes()
        with patch.object(browser, "SETTINGS_FILE", str(settings)), patch.object(browser.os, "replace", side_effect=OSError("Read-only")):
            self.assertFalse(browser._save_settings({"additional_board_urls": ["https://vendor.invalid/core.json"]}))
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(list(self.folder.glob("settings.json.tmp-*")), [])

    def test_board_provenance_and_user_mapping_follow_selected_release(self):
        source = self.folder / 'vendor.json'
        source.write_text(json.dumps({'packages': [dict(name='future-vendor', _index_url='https://forged.invalid/index.json',
            platforms=[dict(name='Future core', architecture='future', version='2.0',
                            url='https://vendor.invalid/core.zip', boards=[dict(name='Future board')])])]}), encoding='utf-8')
        with patch.object(browser, 'INDEX_CACHE_DIR', str(self.folder)):
            grouped = browser._read_board_catalog([str(source)], source_urls={str(source): 'https://vendor.invalid/index.json'})
        item = grouped['Future core']
        version = item['versions'][0]
        self.assertEqual(version['index_url'], 'https://vendor.invalid/index.json')
        app = object.__new__(browser.ArduinoBrowser)
        value = {'platform': 'vendor/future@2.0', 'board_ids': {'future_chip': 'exact_future'}}
        key = app._board_association_key({**item, **version})
        app._board_platformio_associations = {key: value}
        self.assertEqual(app._package_metadata(item, version, is_board=True)['platformio'], value)
        foreign = dict(version, index_url='https://different.invalid/index.json', platformio=value)
        self.assertNotIn('platformio', app._package_metadata(item, foreign, is_board=True))
        self.assertNotIn('platformio', app._package_metadata(item, version, is_board=False))

    def test_promotion_failure_restores_previous_payload_and_cancel_keeps_archive(self):
        previous, staging = self.folder / "core", self.folder / "core.part"
        previous.mkdir()
        (previous / "keep.txt").write_bytes(b"Previous payload")
        staging.mkdir()
        (staging / "new.txt").write_bytes(b"New payload")
        replace = browser.os.replace
        def reject_new(source, target):
            if str(source) == str(staging):
                raise OSError("Fixture destination unavailable")
            return replace(source, target)
        with patch.object(browser.os, "replace", side_effect=reject_new):
            with self.assertRaises(OSError):
                browser._promote_directory(str(staging), str(previous))
        self.assertEqual((previous / "keep.txt").read_bytes(), b"Previous payload")
        archive = self.folder / "core.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("boards.txt", "nodemcuv2.name=NodeMCU 1.0 (ESP-12E Module)\n")
        cancel = threading.Event()
        cancel.set()
        before = archive.read_bytes()
        with self.assertRaises(InterruptedError):
            browser._extract_archive(str(archive), str(self.folder / "cancelled"), cancel)
        self.assertEqual(archive.read_bytes(), before)
        self.assertFalse((self.folder / "cancelled").exists())

    def test_single_pass_details_and_cancellation(self):
        example = self.folder / 'examples/Blink/Blink.ino'
        example.parent.mkdir(parents=True)
        example.write_text('void setup() {}', encoding='utf-8')
        boards = self.folder / 'boards.txt'
        boards.write_text('chip.name=MCU\nchip.menu.cpu.name=Ignored\n#comment.name=Ignored\n', encoding='utf-8')
        cancel = threading.Event()
        size, examples, _ = loading.scan_package_details(dict(path=str(self.folder), type='Library'), cancel)
        self.assertIn(str(example), examples)
        self.assertGreaterEqual(size, example.stat().st_size)
        self.assertEqual(loading.scan_package_details(dict(path=str(self.folder), type='Board Platform'), cancel)[2], ['MCU'])
        cancel.set()
        self.assertIsNone(loading.scan_package_details(dict(path=str(self.folder), type='Library'), cancel))

    def test_read_only_source_cache_keeps_fresh_downloaded_catalog_usable(self):
        old = {'libraries': [dict(name='Sensor', version='1.0.0', url='https://example.invalid/sensor.zip')]}
        self.source.write_text(json.dumps(old), encoding='utf-8')
        browser._read_library_catalog(str(self.source))
        fresh = {'libraries': [dict(name='Sensor', version='2.0.0', url='https://example.invalid/sensor.zip')]}
        app = browser.ArduinoBrowser.__new__(browser.ArduinoBrowser)
        app._is_online = True
        app._post_ui = Mock()
        response = Mock(content=json.dumps(fresh).encode(), raise_for_status=Mock(), close=Mock())
        before = self.source.read_bytes()
        with patch.object(browser.requests, 'get', return_value=response), patch.object(browser, '_write_index_cache', side_effect=OSError('Read-only')):
            data = app._load_index(browser.LIBRARY_INDEX_URL, str(self.source), True, 'library')
        self.assertEqual(browser._read_library_catalog(str(self.source), data)['Sensor']['versions'][0]['version'], '2.0.0')
        self.assertEqual(self.source.read_bytes(), before)
        fresh_boards = {'packages': [dict(name='vendor', platforms=[dict(name='Core', version='2.0.0', url='https://example.invalid/core.zip')])],
                        '_browser_cache_written': False}
        missing = str(self.folder / 'missing-boards.json')
        self.assertEqual(browser._read_board_catalog([missing], {missing: fresh_boards})['Core']['versions'][0]['version'], '2.0.0')


class BrowserLoadingChecks(DownloaderChecks):
    def setUp(self):
        super().setUp()
        parent = ROOT / 'temp/audit/downloader'
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.addCleanup(flush_writes)
        self.folder = Path(temporary.name)
        self.app._download_dir = str(self.folder / 'downloads')
        self.app._cancel_event = threading.Event()
        self.app._compute_installed_items = MethodType(browser.ArduinoBrowser._compute_installed_items, self.app)
        self.app._compute_installed_items_async = MethodType(browser.ArduinoBrowser._compute_installed_items_async, self.app)

    def until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for the isolated worker')

    def test_cached_catalog_visible_while_network_waits_and_reconnects(self):
        lib = self.folder / 'libraries.json'
        board = self.folder / 'boards.json'
        data = {'libraries': [dict(name='Sensor', version='1.0.0', url='https://example.invalid/sensor.zip')]}
        lib.write_text(json.dumps(data), encoding='utf-8')
        board.write_text('{"packages":[]}', encoding='utf-8')
        requested, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def get(url, **kwargs):
            requested.set()
            self.assertTrue(release.wait(2))
            value = data if url == browser.LIBRARY_INDEX_URL else {'packages': []}
            return Mock(content=json.dumps(value).encode(), raise_for_status=Mock(), close=Mock())
        main_thread = threading.get_ident()
        after = self.root.after
        def safe_after(*args, **kwargs):
            self.assertEqual(threading.get_ident(), main_thread, 'Worker called Tcl')
            return after(*args, **kwargs)
        with patch.object(browser, 'LIBRARY_CACHE_FILE', str(lib)), patch.object(browser, 'BOARD_CACHE_FILE', str(board)), \
                patch.object(browser, 'INDEX_CACHE_DIR', str(self.folder)), patch.object(browser.requests, 'get', side_effect=get), \
                patch.object(self.root, 'after', side_effect=safe_after):
            self.app._is_online = False
            self.app.lib_tab.search_var.set('sensor')
            self.app._start_thread(lambda: self.app._load_both(force_refresh=True))
            self.until(lambda: requested.is_set() and self.app.lib_tab.listbox.size() == 1)
            self.assertTrue(self.app._busy)
            self.assertEqual(self.app.lib_tab.search_var.get(), 'sensor')
            release.set()
            self.until(lambda: not self.app._busy and self.app._tasks._timer is None)
            self.assertTrue(self.app._is_online)

    def test_fresh_catalog_does_not_probe_network(self):
        lib, board = self.folder / 'libraries.json', self.folder / 'boards.json'
        lib.write_text('{"libraries":[]}', encoding='utf-8')
        board.write_text('{"packages":[]}', encoding='utf-8')
        with patch.object(browser, 'LIBRARY_CACHE_FILE', str(lib)), patch.object(browser, 'BOARD_CACHE_FILE', str(board)), \
                patch.object(browser, 'INDEX_CACHE_DIR', str(self.folder)), patch.object(browser.requests, 'get', side_effect=AssertionError('Unexpected HTTP')):
            self.app._start_thread(self.app._load_both)
            self.until(lambda: not self.app._busy and self.app._tasks._timer is None)

    def test_task_launch_failures_queue_errors_balance_slots_and_allow_retry(self):
        tasks = self.app._tasks
        ui_thread = threading.get_ident()
        for stage in ('create', 'start'):
            with self.subTest(stage=stage):
                target, errors = Mock(), []
                failed = lambda error: errors.append((error, threading.get_ident()))
                failure = RuntimeError(f'Cannot {stage} fixture worker')
                factory = Mock(side_effect=failure) if stage == 'create' else Mock(
                    return_value=SimpleNamespace(start=Mock(side_effect=failure)))
                with patch.object(loading.threading, 'Thread', factory):
                    self.assertFalse(tasks.start(target, failed=failed))
                    self.assertEqual(tasks._active, 0)
                    self.assertEqual(errors, [], 'Failure callback ran outside the Tk queue')
                self.until(lambda: errors and tasks._timer is None)
                target.assert_not_called()
                self.assertEqual(errors, [(str(failure), ui_thread)])
                done = threading.Event()
                self.assertTrue(tasks.start(done.set))
                self.until(lambda: done.is_set() and tasks._timer is None)
                self.assertEqual(tasks._active, 0)

    def test_task_launch_rollback_cannot_race_a_worker(self):
        tasks = self.app._tasks
        real_thread = threading.Thread
        for entered_before_error in (False, True):
            with self.subTest(entered_before_error=entered_before_error):
                release, entered = threading.Event(), threading.Event()
                calls, errors, threads = [], [], []
                self.addCleanup(release.set)

                def target():
                    calls.append('entered')
                    entered.set()
                    release.wait(2)

                def factory(**kwargs):
                    def run():
                        if not entered_before_error:
                            release.wait(2)
                        kwargs['target']()

                    def start():
                        thread = real_thread(target=run, daemon=True)
                        threads.append(thread)
                        thread.start()
                        if entered_before_error:
                            self.assertTrue(entered.wait(2))
                        raise RuntimeError('Failure after native startup')
                    return SimpleNamespace(start=start)

                with patch.object(loading.threading, 'Thread', side_effect=factory):
                    self.assertEqual(tasks.start(target, failed=errors.append), entered_before_error)
                self.assertEqual(tasks._active, int(entered_before_error))
                release.set()
                threads[0].join(2)
                self.assertFalse(threads[0].is_alive())
                self.until(lambda: tasks._timer is None)
                self.assertEqual(tasks._active, 0)
                self.assertEqual(calls, ['entered'] if entered_before_error else [])
                self.assertEqual(errors, [] if entered_before_error else ['Failure after native startup'])

    def test_latest_scan_launch_failure_reports_latest_without_replay_and_retries(self):
        tasks = self.app._tasks
        for stage in ('create', 'start'):
            with self.subTest(stage=stage):
                scan = Mock(side_effect=lambda value, cancel: value)
                worker = loading.LatestScan(tasks, scan)
                initial, received = Mock(), []
                ui_thread = threading.get_ident()
                completed = lambda value, error: received.append((value, error, threading.get_ident()))
                failure = RuntimeError(f'Cannot {stage} detail worker')
                factory = Mock(side_effect=failure) if stage == 'create' else Mock(
                    return_value=SimpleNamespace(start=Mock(side_effect=failure)))
                with patch.object(loading.threading, 'Thread', factory):
                    worker.submit('initial', initial)
                    worker.submit('latest', completed)
                    factory.assert_called_once()
                    self.assertEqual(received, [])
                self.until(lambda: received and tasks._timer is None)
                self.assertEqual(received, [(None, str(failure), ui_thread)])
                initial.assert_not_called()
                scan.assert_not_called()
                self.assertFalse(worker._running)
                self.assertIsNone(worker._pending)
                self.assertIsNone(worker._cancel)
                worker.submit('retry', completed)
                self.until(lambda: len(received) == 2 and tasks._timer is None)
                self.assertEqual(received[-1], ('retry', None, ui_thread))
                scan.assert_called_once()
                self.assertEqual(tasks._active, 0)
                self.assertFalse(worker._running)

    def test_catalog_launch_failure_restores_busy_controls_and_status(self):
        app = self.app
        target = Mock()
        with patch.object(loading.threading, 'Thread', side_effect=RuntimeError('Worker budget exhausted')):
            app._start_thread(target)
            self.assertTrue(app._busy)
            self.assertEqual(str(app.refresh_btn.cget('state')), 'disabled')
        self.until(lambda: not app._busy and app._tasks._timer is None)
        target.assert_not_called()
        self.assertEqual(str(app.refresh_btn.cget('state')), 'normal')
        self.assertEqual(app.status_var.get(), 'Catalog load failed: Worker budget exhausted')
        self.assertEqual(app._tasks._active, 0)

    def test_latest_scan_bounds_work_and_idle_timer_stops(self):
        active, peak, calls, received = [0], [0], [], []
        started = threading.Event()
        def scan(value, cancel):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            calls.append(value)
            started.set()
            if value == 0:
                cancel.wait(1)
            active[0] -= 1
            return value
        worker = loading.LatestScan(self.app._tasks, scan)
        worker.submit(0, lambda value, error: received.append(value))
        self.until(started.is_set)
        for number in range(1, 201):
            worker.submit(number, lambda value, error: received.append(value))
        self.until(lambda: 200 in received and self.app._tasks._timer is None)
        self.assertEqual(peak[0], 1)
        self.assertLess(len(calls), 10)
        self.assertEqual(received[-1], 200)
        for number in range(10000):
            self.app._tasks.post(lambda value: None, number, key='progress')
        self.assertEqual(len(self.app._tasks._latest), 1)
        self.app._tasks.begin()
        self.app._tasks.end()
        self.until(lambda: self.app._tasks._timer is None)

    def test_inventory_invalidates_and_keeps_board_only_updates(self):
        dest = Path(self.app._download_dir) / 'Boards'
        dest.mkdir(parents=True)
        file = dest / 'core.zip'
        file.touch()
        self.app.board_tab.all_items = {'Core': {'versions': [dict(version='2.0.0', url='https://example.invalid/new.zip'),
                                                               dict(version='1.0.0', url='https://example.invalid/core.zip')]}}
        first = self.app._compute_installed_items()
        self.assertEqual(first[0]['type'], 'Board Platform')
        self.assertTrue(first[0]['update_available'])
        self.assertIs(self.app._compute_installed_items(), first)
        file.unlink()
        self.assertEqual(self.app._compute_installed_items(), [])

    def test_low_end_network_worker_budget(self):
        lib = self.folder / 'libraries.json'
        lib.write_text('{"libraries":[]}', encoding='utf-8')
        app = self.app
        app._additional_board_urls = [f'https://example.invalid/vendor-{n}.json' for n in range(5)]
        active, peak = [0], [0]
        lock = threading.Lock()
        def get(*args, **kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(.025)
            with lock:
                active[0] -= 1
            return Mock(content=b'{"packages":[]}', raise_for_status=Mock(), close=Mock())
        with patch.object(browser, 'LIBRARY_CACHE_FILE', str(lib)), patch.object(browser, 'BOARD_CACHE_FILE', str(self.folder / 'boards.json')), \
                patch.object(browser, 'INDEX_CACHE_DIR', str(self.folder)), patch.object(browser.requests, 'get', side_effect=get), \
                patch('src.modules.runtime_resources.performance_profile', return_value=SimpleNamespace(constrained=True)):
            app._start_thread(app._load_both)
            self.until(lambda: not app._busy and app._tasks._timer is None)
        self.assertEqual(peak[0], 2)

    def test_large_catalog_search_is_latest_only_and_keeps_tk_responsive(self):
        tab = self.app.lib_tab
        original_scan = tab._search_worker.scan
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        ui_thread = threading.get_ident()
        calls = []
        def scan(request, cancel):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            calls.append(request[1])
            if len(calls) == 1:
                started.set()
                release.wait(2)
            return original_scan(request, cancel)
        tab._search_worker.scan = scan
        tab.populate({f"Sensor {index:05}": {"name": f"Sensor {index:05}", "versions": []} for index in range(25000)})
        self.until(started.is_set)
        ticks = []
        self.root.after(0, lambda: ticks.append(True))
        for query in ("000", "sensor 123", "sensor 12345"):
            tab.search_var.set(query)
            tab._execute_search()
        self.root.update()
        self.assertEqual(ticks, [True])
        self.assertEqual(tab.listbox.size(), 0)
        release.set()
        self.until(lambda: tab.filtered_names == ["Sensor 12345"] and self.app._tasks._timer is None)
        self.assertEqual(calls, ["", "sensor 12345"])
        self.assertEqual(tab.listbox.get(0), "Sensor 12345")

    def test_version_disk_scan_stays_off_tk_and_discards_stale_selection(self):
        app, tab = self.app, self.app.lib_tab
        tab.populate({name: {"name": name, "versions": [dict(version="1.0", size=128, url="https://fixture.invalid/core.zip")]} for name in ("First", "Second")})
        app._update_version_status = MethodType(browser.ArduinoBrowser._update_version_status, app)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        ui_thread = threading.get_ident()
        calls = []
        def slow_scan(_tab, name, item, **kwargs):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            calls.append(name)
            if name == "First":
                started.set()
                release.wait(2)
            return None
        tab.version_var.set("1.0")
        with patch.object(app, "_get_installed_info", side_effect=slow_scan), patch.object(app, "_render_version_status") as render:
            tab.listbox.selection_set(0)
            app._update_version_status(tab)
            self.until(started.is_set)
            tab.listbox.selection_clear(0, "end")
            tab.listbox.selection_set(1)
            app._update_version_status(tab)
            release.set()
            self.until(lambda: app._tasks._timer is None)
            self.assertEqual(calls, ["First", "Second"])
            render.assert_called_once()
            self.assertEqual(render.call_args.args[1], "Second")

    def test_board_download_handoff_reuses_job_and_selected_version_metadata(self):
        app, tab = self.app, self.app.board_tab
        app._active_download_tab = tab
        app._downloading_item_name = "ESP8266"
        app._cancel_event = threading.Event()
        app._busy = True
        data_stream = io.BytesIO()
        with zipfile.ZipFile(data_stream, "w") as archive:
            archive.writestr("esp8266/boards.txt", "nodemcuv2.name=NodeMCU 1.0 (ESP-12E Module)\n")
        data = data_stream.getvalue()
        metadata = dict(name="ESP8266", package="esp8266", architecture="esp8266", boards=["NodeMCU 1.0 (ESP-12E Module)"],
                        size=len(data), checksum="SHA-256:" + hashlib.sha256(data).hexdigest())
        response = Mock(status_code=200, raw=None, headers={"content-length": str(len(data))}, raise_for_status=Mock(),
                        iter_content=Mock(return_value=iter([data])), close=Mock())
        destination = self.folder / "download/Boards"
        app._active_package_job = "handoff-fixture"
        with patch.object(browser.requests, "get", return_value=response), patch("src.modules.board_preparation.start_preparation") as prepare, \
                patch.object(browser.messagebox, "showinfo") as success, patch.object(browser.messagebox, "showerror") as failure:
            app._tasks.start(app._download_worker, tab, "https://fixture.invalid/esp8266.zip", "esp8266.zip", str(destination),
                             "folder", None, metadata, "handoff-fixture")
            self.until(lambda: not app._busy and app._tasks._timer is None)
            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.kwargs["job_id"], "handoff-fixture")
            self.assertEqual(prepare.call_args.kwargs["package_metadata"], metadata)
            self.assertEqual(prepare.call_args.kwargs["event_root"], app._package_event_root)
            self.assertTrue((Path(prepare.call_args.args[0]) / "boards.txt").exists())
            success.assert_not_called()
            failure.assert_not_called()
        event = json.loads((Path(app._package_event_root) / "handoff-fixture.json").read_text(encoding="utf-8"))
        self.assertEqual([stage["stage"] for stage in event["history"]], ["queued", "downloading", "verifying", "extracting", "queued"])
        self.assertEqual(event["stage"], "queued")
        self.assertIn("background", app.status_var.get())

    def test_prepare_saved_archive_is_explicit_and_does_not_redownload(self):
        app, tab = self.app, self.app.board_tab
        destination = Path(app._download_dir) / "Boards"
        destination.mkdir(parents=True)
        archive = destination / "esp8266.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("boards.txt", "nodemcuv2.name=NodeMCU 1.0 (ESP-12E Module)\n")
        release = dict(version="3.1", url="https://fixture.invalid/esp8266.zip", archiveFileName=archive.name,
                       size=archive.stat().st_size, checksum="SHA-256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
                       package="esp8266", architecture="esp8266", boards=["NodeMCU 1.0 (ESP-12E Module)"])
        tab.populate({"ESP8266": dict(name="ESP8266", package="esp8266", architecture="esp8266", versions=[release])})
        tab.listbox.selection_set(0)
        tab.version_var.set("3.1")
        with patch.object(browser.requests, "get", side_effect=AssertionError("Archive was redownloaded")), \
                patch("src.modules.board_preparation.start_preparation") as prepare, patch.object(browser.messagebox, "showerror") as failure:
            app._prepare_existing_board(tab)
            self.until(lambda: not app._busy and app._tasks._timer is None)
            prepare.assert_called_once()
            failure.assert_not_called()
        self.assertEqual(archive.stat().st_size, release["size"])
        self.assertTrue((destination / "esp8266/boards.txt").exists())

    def test_preparation_watcher_failure_warns_without_replaying_spawn(self):
        app, tab = self.app, self.app.board_tab
        app._busy, app._active_download_tab = True, tab
        child = SimpleNamespace(preparation_tracking_error="Worker resources exhausted")
        with patch("src.modules.board_preparation.start_preparation", return_value=child) as prepare, \
                patch.object(browser.messagebox, "showerror") as error:
            app._handoff_board_preparation(str(self.folder / "boards"), {"name": "ESP8266"}, "watcher-fixture")
            app._download_done(tab, str(self.folder / "boards"), True)
            self.until(lambda: app._tasks._timer is None)
            prepare.assert_called_once()
            error.assert_not_called()
        self.assertFalse(app._busy)
        self.assertIn("background tracking unavailable", app.status_var.get())
        self.assertIn("Worker resources exhausted", app.status_var.get())

    def test_tracking_write_failure_never_strands_download_controls(self):
        app, tab = self.app, self.app.lib_tab
        app._busy, app._active_download_tab = True, tab
        app._downloading_item_name = "Sensor"
        app._active_package_job = "unwritable-tracking"
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as stream:
            stream.writestr("library.properties", "name=Sensor\n")
        data = payload.getvalue()
        response = Mock(status_code=200, raw=None, headers={"content-length": str(len(data))}, raise_for_status=Mock(),
                        iter_content=Mock(return_value=iter([data])), close=Mock())
        with patch("src.modules.package_jobs.publish_event", side_effect=OSError("Tracking journal read-only")), \
                patch.object(browser.requests, "get", return_value=response), patch.object(browser.messagebox, "showerror") as error:
            app._tasks.start(app._download_worker, tab, "https://fixture.invalid/sensor.zip", "sensor.zip",
                             str(self.folder / "downloads/Libs"), "zip", None, {"size": len(data)}, "unwritable-tracking")
            self.until(lambda: not app._busy and app._tasks._timer is None)
            error.assert_not_called()
        self.assertEqual((self.folder / "downloads/Libs/sensor.zip").read_bytes(), data)
        self.assertIn("tracking could not be saved", app.status_var.get())
        self.assertEqual(str(tab.download_btn.cget("state")), "normal")

    def test_download_checksum_progress_and_cancel_cleanup(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('library.properties', 'name=Sensor\nversion=1.0.0\n')
        data = payload.getvalue()
        metadata = dict(size=len(data), checksum='SHA-256:' + hashlib.sha256(data).hexdigest())
        app = self.app
        app._active_download_tab = app.lib_tab
        app._downloading_item_name = 'Sensor'
        app._cancel_event = threading.Event()
        destination = str(self.folder / 'downloads/Libs')
        for cancel in (False, True):
            app._busy = True
            app._active_download_tab = app.lib_tab
            app._cancel_event.set() if cancel else app._cancel_event.clear()
            response = Mock(status_code=200, raw=None, headers={'content-length': str(len(data))}, raise_for_status=Mock(),
                            iter_content=Mock(return_value=iter([data])), close=Mock())
            with patch.object(browser.requests, 'get', return_value=response) as get, patch.object(browser.messagebox, 'showinfo'), \
                    patch.object(browser.messagebox, 'showerror') as failed:
                app._tasks.start(app._download_worker, app.lib_tab, 'https://example.invalid/sensor.zip',
                                 'sensor.zip', destination, 'file', None, metadata)
                self.until(lambda: not app._busy and app._tasks._timer is None)
                failed.assert_not_called()
                if cancel:
                    get.assert_not_called()
                    response.close.assert_not_called()
                else:
                    response.close.assert_called_once()
                self.assertFalse((Path(destination) / 'sensor.zip.part').exists())
        self.assertEqual((Path(destination) / 'sensor.zip').read_bytes(), data)

    def _assert_download_launch_recovers(self, updating):
        app = self.app
        app._cancel_event = threading.Event()
        app._active_download_tab = app._downloading_item_name = None
        for is_board in (False, True):
            for stage in ('create', 'start'):
                with self.subTest(updating=updating, is_board=is_board, stage=stage):
                    kind = 'Board Platform' if is_board else 'Library'
                    name = f'Fixture {kind} {stage}'
                    app._download_dir = str(self.folder / f'{int(updating)}-{int(is_board)}-{stage}')
                    destination = Path(app._download_dir) / ('Boards' if is_board else 'Libs')
                    destination.mkdir(parents=True)
                    old_path, old_archive = destination / 'old-payload', destination / 'old.zip'
                    old_path.mkdir()
                    (old_path / 'keep.txt').write_bytes(b'Previous installed payload')
                    old_archive.write_bytes(b'Previous archive')
                    payload = io.BytesIO()
                    with zipfile.ZipFile(payload, 'w') as archive:
                        archive.writestr('boards.txt' if is_board else 'library.properties', 'fixture=2.0.0\n')
                    data = payload.getvalue()
                    release = dict(version='2.0.0', url='https://example.invalid/fixture.zip',
                                   archiveFileName='fixture.zip', size=len(data),
                                   checksum='SHA-256:' + hashlib.sha256(data).hexdigest())
                    tab = app.board_tab if is_board else app.lib_tab
                    tab.populate({name: dict(name=name, versions=[release])})
                    tab.listbox.selection_set(0)
                    tab.version_var.set(release['version'])
                    tab.download_btn.config(state='normal')
                    installed = app.installed_tab
                    installed.populate([dict(name=name, type=kind, path=str(old_path), archive=old_archive.name,
                                             installed_version='1.0.0', latest_version='2.0.0', update_available=True)])
                    installed.listbox.selection_set(0)
                    installed.refresh_update_button_state()
                    button = installed.update_btn if updating else tab.download_btn
                    failure = RuntimeError(f'Cannot {stage} download worker')
                    factory = Mock(side_effect=failure) if stage == 'create' else Mock(
                        return_value=SimpleNamespace(start=Mock(side_effect=failure)))
                    response = Mock(status_code=200, raw=None, headers={'content-length': str(len(data))}, raise_for_status=Mock(),
                                    iter_content=Mock(return_value=iter([data])), close=Mock())
                    with patch.object(app, '_prompt_download_option', return_value='zip'), \
                            patch.object(browser.requests, 'get', return_value=response) as get, \
                            patch('urllib.request.urlopen', side_effect=AssertionError('Live transfer attempted')), \
                            patch.object(browser.messagebox, 'showerror') as show_error, \
                            patch.object(browser.messagebox, 'showinfo') as show_info:
                        with patch.object(loading.threading, 'Thread', factory):
                            button.invoke()
                            self.assertTrue(app._busy)
                            self.assertIn('Cancel', tab.download_btn.cget('text'))
                            if updating:
                                self.assertIn('Cancel', installed.update_btn.cget('text'))
                            get.assert_not_called()
                            show_error.assert_not_called()
                        self.until(lambda: not app._busy and app._tasks._timer is None)
                        prefix = 'Unable to start download update:' if updating else 'Unable to start download:'
                        show_error.assert_called_once_with('Download Error', f'{prefix}\n{failure}', parent=self.root)
                        self.assertEqual(app.status_var.get(), 'Download failed')
                        self.assertIsNone(app._active_download_tab)
                        self.assertIsNone(app._downloading_item_name)
                        self.assertIn('Download', tab.download_btn.cget('text'))
                        self.assertIn('Update', installed.update_btn.cget('text'))
                        self.assertEqual(str(button.cget('state')), 'normal')
                        self.assertEqual(str(app.progress.cget('mode')), 'determinate')
                        self.assertEqual(float(app.progress.cget('value')), 0)
                        self.assertEqual(app._tasks._active, 0)
                        self.assertFalse((destination / 'fixture.zip').exists())
                        self.assertEqual((old_path / 'keep.txt').read_bytes(), b'Previous installed payload')
                        self.assertEqual(old_archive.read_bytes(), b'Previous archive')
                        get.assert_not_called()
                        show_info.assert_not_called()
                        # The next explicit click reaches the real worker, using fixture HTTP bytes only.
                        button.invoke()
                        self.assertTrue(app._busy)
                        self.until(lambda: not app._busy and app._tasks._timer is None)
                        get.assert_called_once()
                        response.close.assert_called_once()
                        self.assertEqual((destination / 'fixture.zip').read_bytes(), data)
                        self.assertEqual(app.status_var.get(), 'Download complete')
                        self.assertEqual(app._tasks._active, 0)
                        show_error.assert_called_once()
                        show_info.assert_not_called()
                        self.assertFalse((destination / 'fixture.zip.part').exists())
                        if updating:
                            self.assertFalse(old_path.exists())
                            self.assertFalse(old_archive.exists())
                        else:
                            self.assertTrue(old_path.exists())
                            self.assertTrue(old_archive.exists())

    def test_download_button_launch_failure_restores_controls_and_explicit_retry(self):
        self._assert_download_launch_recovers(updating=False)

    def test_update_button_launch_failure_preserves_old_payload_and_explicit_retry(self):
        self._assert_download_launch_recovers(updating=True)

    def test_detail_filter_does_not_revive_old_scan_and_preserves_path_selection(self):
        detail = self.app.installed_tab
        detail._detail_worker = Mock()
        items = [dict(type='Library', name='Sensor', path=str(self.folder / str(index)),
                      installed_version='1.0', latest_version='1.0', update_available=False)
                 for index in range(2)]
        detail.populate(items)
        detail.listbox.selection_set(1)
        detail._on_select()
        detail._all_examples = [str(self.folder / 'old/Blink.ino')]
        detail.examples_search_var.set('Blink')
        self.assertIn('Scanning', detail.examples_listbox.get(0))
        detail._loading_complete_req_id = detail._select_req_id
        detail._all_examples = [str(self.folder / 'new/Blink.ino'), str(self.folder / 'new/Read.ino')]
        detail._filter_and_render_list(item_override=items[1])
        self.assertEqual(detail._current_examples, [str(self.folder / 'new/Blink.ino')])
        detail.search_var.set('sensor')
        detail._execute_search()
        self.assertEqual(detail.listbox.curselection(), (1,))


if __name__ == '__main__':
    suite = unittest.TestSuite()
    for cls in (CacheChecks, BrowserLoadingChecks):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
