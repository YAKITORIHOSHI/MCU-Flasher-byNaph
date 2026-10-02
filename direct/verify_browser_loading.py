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
            response = Mock(headers={'content-length': str(len(data))}, raise_for_status=Mock(),
                            iter_content=Mock(return_value=iter([data])), close=Mock())
            with patch.object(browser.requests, 'get', return_value=response), patch.object(browser.messagebox, 'showinfo'), \
                    patch.object(browser.messagebox, 'showerror') as failed:
                app._tasks.start(app._download_worker, app.lib_tab, 'https://example.invalid/sensor.zip',
                                 'sensor.zip', destination, 'file', None, metadata)
                self.until(lambda: not app._busy and app._tasks._timer is None)
                failed.assert_not_called()
                response.close.assert_called_once()
                self.assertFalse((Path(destination) / 'sensor.zip.part').exists())
        self.assertEqual((Path(destination) / 'sensor.zip').read_bytes(), data)

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
