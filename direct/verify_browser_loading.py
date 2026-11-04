"""Isolated catalog, scan and Qt responsiveness checks; no live networking."""
from __future__ import annotations
import json
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.qt_browser_fixture import APP, QtBrowserFixture, tasks_idle
from main.qt import package_browser as pb
from src.modules import arduino_lib_req as browser
from src.modules import browser_loading as loading
from PySide6.QtCore import QTimer


def flush_writes():
    writer = loading._WRITER
    if writer:
        writer.join(5)
        assert not writer.is_alive(), 'Cache writer did not finish'


def record(version='1.0.0'):
    return {'Sensor': {'name': 'Sensor', 'versions': [dict(version=version, url='https://example.invalid/sensor.zip',
                                                           checksum='SHA-256:fixture', archiveFileName='sensor.zip')]}}


class EngineStub(browser.BrowserEngine):
    """Package logic without a window: every view callback is an inert recorder."""

    def __init__(self):
        for name in ('_publish_catalogs', '_finish_load', '_set_status', '_set_sources_status', '_post_ui',
                     '_download_done', '_download_error', '_download_cancelled', '_set_progress_determinate',
                     '_update_progress'):
            setattr(self, name, Mock(name=name))
        self._is_online = True


class QtTaskLifecycleChecks(unittest.TestCase):
    """Workers post; only the GUI thread runs callbacks, and idle work has no timer."""

    def setUp(self):
        self.tasks = pb.QtTasks()
        self.addCleanup(self.tasks.close)

    def drain(self, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not tasks_idle(self.tasks):
            APP.processEvents()
        self.assertTrue(tasks_idle(self.tasks), 'Posted callbacks did not drain')

    def test_worker_posts_run_on_the_gui_thread_and_idle_has_no_timer(self):
        calls = []
        worker = threading.Thread(target=lambda: self.tasks.post(lambda: calls.append(threading.get_ident())))
        worker.start()
        worker.join(2)
        self.drain()
        self.assertEqual(calls, [threading.get_ident()])
        self.assertEqual(self.tasks.findChildren(QTimer), [], 'An idle browser must not schedule timers')

    def test_keyed_posts_coalesce_to_the_latest_value(self):
        received = []
        for number in range(10000):
            self.tasks.post(received.append, number, key='progress')
        self.assertEqual(len(self.tasks._latest), 1)
        self.drain()
        self.assertEqual(received, [9999])

    def test_close_from_a_completion_drops_later_work_and_does_not_reschedule(self):
        later = Mock()
        self.tasks.post(self.tasks.close)
        self.tasks.post(later)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not self.tasks.closed:
            APP.processEvents()
        APP.processEvents()
        self.assertTrue(self.tasks.closed)
        later.assert_not_called()
        self.tasks.post(later)
        self.assertFalse(self.tasks._queue)
        self.assertFalse(self.tasks.start(later))
        later.assert_not_called()

    def test_a_burst_of_posts_keeps_the_event_loop_turning(self):
        ran = []
        for number in range(2000):
            self.tasks.post(lambda value=number: (time.sleep(.0005), ran.append(value)))
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(len(ran)))
        self.drain(10)
        self.assertEqual(ran, list(range(2000)))
        self.assertLess(ticks[0], 2000, 'One drain must not monopolize the GUI thread')

    def test_failing_callback_is_reported_without_stopping_the_queue(self):
        later = Mock()
        self.tasks.post(Mock(side_effect=RuntimeError('fixture failure')))
        self.tasks.post(later)
        with patch.object(sys, 'excepthook') as hook:
            self.drain()
        hook.assert_called_once()
        later.assert_called_once()


class EngineImportChecks(unittest.TestCase):
    def test_engine_loads_without_any_window_toolkit(self):
        probe = """
import importlib.abc
import sys
class NoToolkit(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6', 'PyQt5', 'shiboken6', 'tkinter', '_tkinter'}:
            raise AssertionError('Window toolkit imported by the package engine: ' + fullname)
sys.meta_path.insert(0, NoToolkit())
from src.modules import arduino_lib_req, browser_loading, ubuntu_download_manager
assert not any(name.split('.')[0] in {'PySide6', 'PyQt5', 'shiboken6', 'tkinter', '_tkinter'} for name in sys.modules)
"""
        result = subprocess.run([sys.executable, '-B', '-W', 'error', '-c', probe],
                                cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


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
        browser._INDEX_VALID_SIGNATURES.clear()

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

    def test_catalog_load_does_not_parse_large_board_source_twice(self):
        source = self.folder / 'large-board.json'
        source.write_text(json.dumps(dict(packages=[dict(name='fixture', platforms=[dict(
            name='Fixture core', version='1.0.0', architecture='fixture',
            url='https://fixture.invalid/core.zip', boards=[dict(name='Fixture board')])])],
            padding='x' * 1_100_000)), encoding='utf-8')
        app = EngineStub()
        app._additional_board_urls = []
        app._cache_is_fresh = Mock(return_value=True)
        app._load_index = Mock(side_effect=AssertionError('Fresh source caused a download'))
        with (patch.object(browser, 'BOARD_CACHE_FILE', str(source)),
              patch.object(browser, 'INDEX_CACHE_DIR', str(self.folder)),
              patch.object(browser, '_read_library_catalog', return_value=record()),
              patch.object(browser.json, 'load', wraps=json.load) as parse):
            app._load_both()
        reads = [call for call in parse.call_args_list if call.args[0].name == str(source)]
        self.assertEqual(len(reads), 1, 'A validated catalog source was reparsed just to decide whether to refresh')
        self.assertNotIn(str(source), browser._INDEX_JSON_RAM_CACHE,
                         'Avoiding duplicate parsing must not retain the large raw source')

    def test_validation_summary_rechecks_changed_or_rewritten_sources_and_schema(self):
        path = str(self.source)
        self.assertTrue(browser._has_valid_index_cache(path, 'libraries'))
        self.assertFalse(browser._has_valid_index_cache(path, 'packages'))
        original = self.source.stat()
        browser._write_index_cache(path, ' ' * original.st_size)
        # An application-controlled replacement can have the same size and a
        # coarse filesystem timestamp. Its explicit write must revoke validity.
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertFalse(browser._has_valid_index_cache(path, 'libraries'))
        self.source.write_text('{"libraries":[]}', encoding='utf-8')
        self.touch_source()
        self.assertTrue(browser._has_valid_index_cache(path, 'libraries'))
        self.source.unlink()
        self.assertFalse(browser._has_valid_index_cache(path, 'libraries'))

    def test_unchanged_refresh_reuses_catalogs_before_view_delivery(self):
        libraries = record()
        boards = {'Core': dict(name='Core', versions=[dict(version='1.0', url='https://fixture.invalid/core.zip')])}
        app = EngineStub()
        app.lib_tab, app.board_tab = SimpleNamespace(all_items=libraries), SimpleNamespace(all_items=boards)
        app._additional_board_urls = []
        app._cache_is_fresh = Mock(return_value=True)
        app._load_index = Mock(return_value={})
        # The worker can publish cached data and freshly decoded equal data
        # before the view has handled the first delivery. Neither should reset rows.
        with (patch.object(browser, '_read_library_catalog', side_effect=lambda *args: json.loads(json.dumps(libraries))),
              patch.object(browser, '_read_board_catalog', side_effect=lambda *args, **kwargs: json.loads(json.dumps(boards)))):
            app._load_both(force_refresh=True)
        published = [call.args[1:] for call in app._post_ui.call_args_list
                     if call.args[0] == app._publish_catalogs]
        self.assertEqual(len(published), 3)
        for libs, cores in published:
            if libs is not None:
                self.assertIs(libs, libraries, 'Equal library data must keep rows and selection')
            if cores is not None:
                self.assertIs(cores, boards, 'Equal board data must keep rows and selection')

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
        app = EngineStub()
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
        app = EngineStub()
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


class FolderUpdateChecks(unittest.TestCase):
    """Run the real package worker with fixture HTTP bytes and no window."""

    def setUp(self):
        parent = ROOT / 'temp/audit/downloader'
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        app = EngineStub()
        app.lib_tab, app.board_tab = Mock(), Mock()
        app._download_dir = str(self.folder)
        app._cancel_event = threading.Event()
        app._publish_job = Mock()
        app._handoff_board_preparation = Mock()
        self.app = app

    def test_folder_update_verifies_extracts_and_preserves_old_payload_on_failure(self):
        app = self.app
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('library.properties', 'name=Fixture\nversion=2.0.0\n')
        data = payload.getvalue()
        for failure in ('checksum', 'extraction', None):
            with self.subTest(failure=failure):
                destination = self.folder / str(failure) / 'Libs'
                old_folder = destination / 'core-1'
                old_folder.mkdir(parents=True)
                old_bytes = b'Previous installed payload'
                (old_folder / 'keep.txt').write_bytes(old_bytes)
                old_archive = destination / 'core-1.zip'
                old_archive.write_bytes(b'Previous archive')
                metadata = dict(name='Fixture', size=len(data),
                    checksum='SHA-256:' + ('0' * 64 if failure == 'checksum' else hashlib.sha256(data).hexdigest()))
                response = Mock(status_code=200, raw=None, headers={'content-length': str(len(data))},
                    raise_for_status=Mock(), iter_content=Mock(return_value=iter([data])), close=Mock())
                app._post_ui.reset_mock()
                extractor = (patch.object(browser, '_extract_archive', side_effect=RuntimeError('Fixture extraction failed'))
                    if failure == 'extraction' else patch.object(browser, '_extract_archive', wraps=browser._extract_archive))
                with patch.object(browser.requests, 'get', return_value=response), extractor:
                    app._download_worker(app.lib_tab, 'https://fixture.invalid/core.zip', 'core-2.zip',
                        str(destination), 'folder', (str(old_folder), old_archive.name), metadata, 'installed-fixture')
                self.assertFalse((destination / 'core-2.zip').exists())
                self.assertFalse((destination / 'core-2.zip.part').exists())
                if failure:
                    self.assertEqual((old_folder / 'keep.txt').read_bytes(), old_bytes)
                    self.assertEqual(old_archive.read_bytes(), b'Previous archive')
                    self.assertFalse((destination / 'core-2').exists())
                    self.assertEqual(app._post_ui.call_args.args[0], app._download_error)
                else:
                    self.assertEqual((destination / 'core-2/library.properties').read_text(encoding='utf-8'),
                        'name=Fixture\nversion=2.0.0\n')
                    self.assertFalse(old_folder.exists())
                    self.assertFalse(old_archive.exists())
                    self.assertEqual(app._post_ui.call_args.args,
                        (app._download_done, app.lib_tab, str(destination / 'core-2'), False))
                app._handoff_board_preparation.assert_not_called()


class InstalledUpdateChecks(QtBrowserFixture):
    """Exercise the Installed update path in the real window without a transfer."""

    def setUp(self):
        super().setUp()
        self.app._package_metadata = Mock(return_value={'name': 'Fixture'})
        self.app._prompt_download_option = Mock(side_effect=AssertionError('Installed updates must not prompt for a format'))
        self.app._begin_package_job = Mock(side_effect=lambda _name: setattr(self.app, '_active_package_job', 'installed-fixture'))
        self.set_download_dir(self.folder)

    def _release(self, is_board):
        tab = self.app.board_tab if is_board else self.app.lib_tab
        release = dict(version='2.0.0', url='https://fixture.invalid/core.zip', archiveFileName='core-2.zip')
        tab.all_items = {'Fixture': dict(name='Fixture', versions=[release])}
        return tab, release

    def test_installed_library_and_board_updates_choose_folder_without_prompt(self):
        app = self.app
        for is_board in (False, True):
            with self.subTest(is_board=is_board), patch.object(app._tasks, 'start') as start:
                app._busy = False
                app._package_metadata.reset_mock()
                app._cancel_event.set()
                tab, release = self._release(is_board)
                app._download_update('Fixture', is_board, 'old-folder', 'old.zip')
                self.assertEqual(start.call_args.args, (app._download_worker, tab, release['url'], release['archiveFileName'],
                    str(self.folder / ('Boards' if is_board else 'Libs')), 'folder', ('old-folder', 'old.zip'),
                    {'name': 'Fixture'}, 'installed-fixture'))
                app._package_metadata.assert_called_once_with(tab.all_items['Fixture'], release, is_board=is_board)
                self.assertTrue(app._busy)
                self.assertIs(app._active_download_tab, tab)
                self.assertFalse(app._cancel_event.is_set())
                self.assertEqual(tab.download_btn.text(), 'Cancel')
                self.assertEqual(app.installed_tab.update_btn.text(), 'Cancel')
                self.assertTrue(app.installed_tab.update_btn.isEnabled())
                app._cancel_event.clear()
                app.installed_tab.update_btn.click()
                self.assertTrue(app._cancel_event.is_set(), 'The Installed page must be able to cancel its update')
        app._prompt_download_option.assert_not_called()

    def test_busy_or_invalid_metadata_does_not_dispatch_an_installed_update(self):
        app = self.app
        self._release(True)
        with patch.object(app._tasks, 'start') as start:
            app._busy = True
            app._download_update('Fixture', True)
            app._package_metadata.assert_not_called()
            start.assert_not_called()
            app._busy = False
            app._package_metadata.side_effect = ValueError('Invalid fixture platform')
            app._download_update('Fixture', True)
            start.assert_not_called()
        self.assertEqual(app.status_label.text(), 'Custom platform settings are invalid: Invalid fixture platform')
        self.assertFalse(app._busy)
        app._prompt_download_option.assert_not_called()


class BrowserSleepChecks(QtBrowserFixture):
    """A retained browser sleeps without cancelling user work or reading disk."""

    def guard(self):
        return (patch.object(self.app, '_compute_installed_items_async', side_effect=AssertionError('Wake scanned disk')),
                patch.object(self.app, '_initial_load', side_effect=AssertionError('Wake reloaded catalogs')),
                patch.object(self.app, '_load_both', side_effect=AssertionError('Wake reloaded catalogs')))

    def test_close_always_sleeps_and_never_cancels_user_work(self):
        app = self.app
        app.show()
        self.pump()
        with patch.object(app, '_force_exit') as force:
            app.close()
        self.pump()
        self.assertTrue(app._is_hidden)
        self.assertFalse(app.isVisible())
        force.assert_not_called()
        self.assertFalse(app._cancel_event.is_set())

    def test_sleep_and_wake_preserve_search_selection_and_release(self):
        app, tab = self.app, self.app.lib_tab
        releases = [dict(version=value, url='https://fixture.invalid/sensor.zip') for value in ('2.0', '1.0')]
        tab.populate({'Sensor': dict(name='Sensor', author='Fixture', maintainer='', category='Sensors',
                                     architectures=['*'], sentence='Fixture sensor', paragraph='',
                                     website='', versions=releases)})
        tab.search_entry.setText('Sensor')
        tab.execute_search()
        self.select(tab)
        tab.version_combo.setCurrentIndex(1)
        self.settle()
        widgets = tab.listbox, tab.search_entry, tab.version_combo, app.notebook
        catalog = tab.all_items
        app.show()
        self.pump()
        guards = self.guard()
        with guards[0], guards[1], guards[2]:
            for _ in range(3):
                app.close()
                self.pump()
                self.assertFalse(app.isVisible())
                app._unhide_window()
                self.pump()
                self.assertTrue(app.isVisible())
                self.assertEqual(tab.selected_name(), 'Sensor')
                self.assertEqual(tab.search_entry.text(), 'Sensor')
                self.assertEqual(tab.version_combo.currentText(), '1.0')
                self.assertIs(tab.all_items, catalog)
                self.assertEqual((tab.listbox, tab.search_entry, tab.version_combo, app.notebook), widgets)
        self.assertFalse(app._is_hidden)
        self.assertFalse(hasattr(app, 'quit_btn'), 'The browser has sleep-on-close rather than a Quit action')
        self.assertFalse(app._cancel_event.is_set())

    def test_hidden_catalog_load_keeps_its_worker(self):
        app = self.app
        app.close()
        self.assertTrue(app._is_hidden)
        release = threading.Event()
        self.addCleanup(release.set)
        ran = threading.Event()
        app._start_thread(lambda: (ran.set(), release.wait(2)))
        self.until(ran.is_set)
        self.assertTrue(app._busy)
        self.assertFalse(app.refresh_btn.isEnabled())

    def test_unchanged_inventory_keeps_visible_rows_and_selection(self):
        app = self.app
        app.notebook.setCurrentIndex(2)
        app._installed_items = [dict(name='Sensor', path='fixture/Sensor', type='Library', installed_version='1.0.0',
                                     latest_version='1.0.0', update_available=False)]
        app._installed_map = {'sensor': app._installed_items[0]}
        app.installed_tab.installed_items = app._installed_items
        with (patch.object(app, '_inventory_worker') as worker, patch.object(app.lib_tab, 'refresh_listbox_labels') as labels,
              patch.object(app.board_tab, 'refresh_listbox_labels') as board_labels,
              patch.object(app.installed_tab, 'populate') as populate, patch.object(app, '_update_version_status') as status):
            app._compute_installed_items_async()
            _request, completed = worker.submit.call_args.args
            completed([dict(item) for item in app._installed_items], None)
        labels.assert_not_called()
        board_labels.assert_not_called()
        populate.assert_not_called()
        status.assert_not_called()

    def test_unchanged_inventory_still_populates_newly_opened_installed_tab(self):
        app = self.app
        app.notebook.setCurrentIndex(2)
        original = [dict(name='Sensor', path='fixture/Sensor', type='Library', installed_version='1.0.0',
                         latest_version='1.0.0', update_available=False)]
        app._installed_items = original
        app._installed_map = {'sensor': original[0]}
        app.installed_tab.installed_items = []
        with patch.object(app, '_inventory_worker') as worker, patch.object(app.installed_tab, 'populate') as populate:
            app._compute_installed_items_async()
            _request, completed = worker.submit.call_args.args
            completed([dict(item) for item in original], None)
        self.assertIs(app._installed_items, original)
        populate.assert_called_once_with(original)

    @unittest.skipIf(sys.platform == 'win32', 'Native POSIX socket readiness')
    def test_native_listener_wakes_sleeping_browser_without_polling(self):
        from src.modules.ubuntu_download_manager import DownloadManagerChannel, request
        channel = DownloadManagerChannel(self.folder)
        self.addCleanup(channel.close)
        app = self.app
        app._linux_channel = channel
        app._start_manager_listener()
        app._unhide_window = Mock()
        app._force_exit = Mock(side_effect=channel.close)
        self.assertFalse(app._partial_timer.isActive(), 'A sleeping browser must not poll its channel')
        self.assertTrue(request(self.folder, 'wake'))
        self.until(lambda: app._unhide_window.called)
        app._unhide_window.assert_called_once()
        self.assertFalse(app._partial_timer.isActive())
        app._force_exit = Mock(side_effect=lambda: app._channel_notifier.setEnabled(False))
        self.assertTrue(request(self.folder, 'quit'))
        self.until(lambda: app._force_exit.called)
        app._force_exit.assert_called_once()
        app._linux_channel = None

    def test_windows_trigger_files_wake_and_retire_the_sleeping_window(self):
        app = self.app
        app._unhide_window = Mock()
        app._force_exit = Mock()
        app._check_show_trigger()
        app._unhide_window.assert_not_called()
        (self.folder / '.show_dm_trigger').write_text('show', encoding='utf-8')
        app._check_show_trigger()
        app._unhide_window.assert_called_once()
        self.assertFalse((self.folder / '.show_dm_trigger').exists())
        (self.folder / '.dm_force_exit').write_text('exit', encoding='utf-8')
        app._check_show_trigger()
        app._force_exit.assert_called_once()
        self.assertFalse((self.folder / '.dm_force_exit').exists())

    def test_window_handle_is_published_and_stale_triggers_cleared(self):
        for name in ('.show_dm_trigger', '.dm_force_exit'):
            (self.folder / name).write_text('stale', encoding='utf-8')
        self.app._publish_window_handle()
        self.assertEqual(int((self.folder / '.dm_hwnd').read_text(encoding='utf-8')), int(self.app.winId()))
        self.assertFalse((self.folder / '.show_dm_trigger').exists())
        self.assertFalse((self.folder / '.dm_force_exit').exists())


class BrowserLoadingChecks(QtBrowserFixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(flush_writes)
        self.set_download_dir(self.folder / 'downloads')

    def test_repeated_unchanged_search_keeps_native_rows_and_selection(self):
        tab = self.app.lib_tab
        tab.populate(record())
        self.select(tab)
        with patch.object(tab.model, 'set_rows', wraps=tab.model.set_rows) as rebuild:
            for _ in range(5):
                tab.execute_search()
            rebuild.assert_not_called()
        self.assertEqual(tab.selected_name(), 'Sensor')

    def test_returned_query_failure_allows_an_explicit_retry(self):
        tab = self.app.lib_tab
        tab.populate(record())
        requests = []
        # Exercise worker completion without involving filesystem or threads.
        tab.all_items = {f'Sensor {index}': {} for index in range(1001)}
        tab._search_worker = SimpleNamespace(cancel=lambda: None,
            submit=lambda request, callback: requests.append((request, callback)))
        tab.search_entry.setText('Sensor')
        tab.execute_search()
        requests[-1][1](['Sensor 0'], None)
        tab.search_entry.setText('other')
        tab.execute_search()
        tab.search_entry.setText('Sensor')
        tab.execute_search()
        requests[-1][1](None, 'Worker unavailable')
        previous = len(requests)
        tab.execute_search()
        self.assertEqual(len(requests), previous + 1, 'The failed latest query must remain retryable')
        requests[-1][1](['Sensor 0'], None)
        self.assertEqual(tab.filtered_names, ['Sensor 0'])

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
        set_status = self.app._set_status
        def gui_only_status(text):
            self.assertEqual(threading.get_ident(), main_thread, 'Worker touched a widget')
            set_status(text)
        with patch.object(browser, 'LIBRARY_CACHE_FILE', str(lib)), patch.object(browser, 'BOARD_CACHE_FILE', str(board)), \
                patch.object(browser, 'INDEX_CACHE_DIR', str(self.folder)), patch.object(browser.requests, 'get', side_effect=get), \
                patch.object(self.app, '_set_status', side_effect=gui_only_status):
            self.app._is_online = False
            self.app.lib_tab.search_entry.setText('sensor')
            self.app._start_thread(lambda: self.app._load_both(force_refresh=True))
            self.until(lambda: requested.is_set() and self.app.lib_tab.model.rowCount() == 1)
            self.assertTrue(self.app._busy)
            self.assertEqual(self.app.lib_tab.search_entry.text(), 'sensor')
            release.set()
            self.until(lambda: not self.app._busy and tasks_idle(self.app._tasks))
            self.assertTrue(self.app._is_online)

    def test_fresh_catalog_does_not_probe_network(self):
        lib, board = self.folder / 'libraries.json', self.folder / 'boards.json'
        lib.write_text('{"libraries":[]}', encoding='utf-8')
        board.write_text('{"packages":[]}', encoding='utf-8')
        with patch.object(browser, 'LIBRARY_CACHE_FILE', str(lib)), patch.object(browser, 'BOARD_CACHE_FILE', str(board)), \
                patch.object(browser, 'INDEX_CACHE_DIR', str(self.folder)), patch.object(browser.requests, 'get', side_effect=AssertionError('Unexpected HTTP')):
            self.app._start_thread(self.app._load_both)
            self.until(lambda: not self.app._busy and tasks_idle(self.app._tasks))

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
                    self.assertEqual(errors, [], 'Failure callback ran outside the GUI queue')
                self.until(lambda: errors and tasks_idle(tasks))
                target.assert_not_called()
                self.assertEqual(errors, [(str(failure), ui_thread)])
                done = threading.Event()
                self.assertTrue(tasks.start(done.set))
                self.until(lambda: done.is_set() and tasks_idle(tasks))

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
                release.set()
                threads[0].join(2)
                self.assertFalse(threads[0].is_alive())
                self.settle()
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
                self.until(lambda: received and tasks_idle(tasks))
                self.assertEqual(received, [(None, str(failure), ui_thread)])
                initial.assert_not_called()
                scan.assert_not_called()
                self.assertFalse(worker._running)
                self.assertIsNone(worker._pending)
                self.assertIsNone(worker._cancel)
                worker.submit('retry', completed)
                self.until(lambda: len(received) == 2 and tasks_idle(tasks))
                self.assertEqual(received[-1], ('retry', None, ui_thread))
                scan.assert_called_once()
                self.assertFalse(worker._running)

    def test_catalog_launch_failure_restores_busy_controls_and_status(self):
        app = self.app
        target = Mock()
        with patch.object(loading.threading, 'Thread', side_effect=RuntimeError('Worker budget exhausted')):
            app._start_thread(target)
            self.assertTrue(app._busy)
            self.assertFalse(app.refresh_btn.isEnabled())
        self.until(lambda: not app._busy and tasks_idle(app._tasks))
        target.assert_not_called()
        self.assertTrue(app.refresh_btn.isEnabled())
        self.assertEqual(app.status_label.text(), 'Catalog load failed: Worker budget exhausted')

    def test_latest_scan_bounds_work_and_latest_result_wins(self):
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
        self.until(lambda: 200 in received and tasks_idle(self.app._tasks))
        self.assertEqual(peak[0], 1)
        self.assertLess(len(calls), 10)
        self.assertEqual(received[-1], 200)

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
            self.until(lambda: not app._busy and tasks_idle(app._tasks))
        self.assertEqual(peak[0], 2)

    def test_large_catalog_search_is_latest_only_and_keeps_the_view_responsive(self):
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
        QTimer.singleShot(0, lambda: ticks.append(True))
        for query in ("000", "sensor 123", "sensor 12345"):
            tab.search_entry.setText(query)
            tab.execute_search()
        APP.processEvents()
        self.assertEqual(ticks, [True])
        self.assertEqual(tab.model.rowCount(), 0)
        release.set()
        self.until(lambda: tab.filtered_names == ["Sensor 12345"] and tasks_idle(self.app._tasks))
        self.assertEqual(calls, ["", "sensor 12345"])
        self.assertEqual(tab.model.rows(), ["Sensor 12345"])

    def test_version_disk_scan_stays_off_the_view_and_discards_stale_selection(self):
        app, tab = self.app, self.app.lib_tab
        tab.populate({name: {"name": name, "versions": [dict(version="1.0", size=128, url="https://fixture.invalid/core.zip")]} for name in ("First", "Second")})
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
            return {"scanned": name}
        self.settle()
        with patch.object(app, "_get_installed_info", side_effect=slow_scan), patch.object(app, "_render_version_status") as render:
            tab.listbox.setCurrentIndex(tab.model.index(0))
            self.until(started.is_set)
            tab.listbox.setCurrentIndex(tab.model.index(1))
            release.set()
            self.until(lambda: "Second" in calls and tasks_idle(app._tasks))
            self.pump(.05)
            self.settle()
            self.assertEqual(calls, ["First", "Second"])
            self.assertEqual([call.args[3] for call in render.call_args_list if call.args[3] is not None],
                             [{"scanned": "Second"}], "A stale selection's scan must never render")

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
        with patch.object(browser.requests, "get", return_value=response), patch("src.modules.board_preparation.start_preparation") as prepare:
            app._tasks.start(app._download_worker, tab, "https://fixture.invalid/esp8266.zip", "esp8266.zip", str(destination),
                             "folder", None, metadata, "handoff-fixture")
            self.until(lambda: not app._busy and tasks_idle(app._tasks))
            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.kwargs["job_id"], "handoff-fixture")
            self.assertEqual(prepare.call_args.kwargs["package_metadata"], metadata)
            self.assertEqual(prepare.call_args.kwargs["event_root"], app._package_event_root)
            self.assertTrue((Path(prepare.call_args.args[0]) / "boards.txt").exists())
            self.assertEqual(self.dialogs, [])
        event = json.loads((Path(app._package_event_root) / "handoff-fixture.json").read_text(encoding="utf-8"))
        self.assertEqual([stage["stage"] for stage in event["history"]], ["queued", "downloading", "verifying", "extracting", "queued"])
        self.assertEqual(event["stage"], "queued")
        self.assertIn("background", app.status_label.text())

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
        self.select(tab)
        with patch.object(browser.requests, "get", side_effect=AssertionError("Archive was redownloaded")), \
                patch("src.modules.board_preparation.start_preparation") as prepare:
            app._prepare_existing_board(tab)
            self.until(lambda: not app._busy and tasks_idle(app._tasks))
            prepare.assert_called_once()
            self.assertEqual(self.dialogs, [])
        self.assertEqual(archive.stat().st_size, release["size"])
        self.assertTrue((destination / "esp8266/boards.txt").exists())

    def test_preparation_watcher_failure_warns_without_replaying_spawn(self):
        app, tab = self.app, self.app.board_tab
        app._busy, app._active_download_tab = True, tab
        child = SimpleNamespace(preparation_tracking_error="Worker resources exhausted")
        with patch("src.modules.board_preparation.start_preparation", return_value=child) as prepare:
            app._handoff_board_preparation(str(self.folder / "boards"), {"name": "ESP8266"}, "watcher-fixture")
            app._download_done(tab, str(self.folder / "boards"), True)
            self.settle()
            prepare.assert_called_once()
            self.assertEqual(self.dialogs, [])
        self.assertFalse(app._busy)
        self.assertIn("background tracking unavailable", app.status_label.text())
        self.assertIn("Worker resources exhausted", app.status_label.text())

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
                patch.object(browser.requests, "get", return_value=response):
            app._tasks.start(app._download_worker, tab, "https://fixture.invalid/sensor.zip", "sensor.zip",
                             str(self.folder / "downloads/Libs"), "zip", None, {"size": len(data)}, "unwritable-tracking")
            self.until(lambda: not app._busy and tasks_idle(app._tasks))
            self.assertEqual(self.dialogs, [])
        self.assertEqual((self.folder / "downloads/Libs/sensor.zip").read_bytes(), data)
        self.assertIn("tracking could not be saved", app.status_label.text())
        self.assertTrue(tab.download_btn.isEnabled())

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
            with patch.object(browser.requests, 'get', return_value=response) as get:
                app._tasks.start(app._download_worker, app.lib_tab, 'https://example.invalid/sensor.zip',
                                 'sensor.zip', destination, 'file', None, metadata)
                self.until(lambda: not app._busy and tasks_idle(app._tasks))
                self.assertEqual(self.dialogs, [])
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
                    self.set_download_dir(self.folder / f'{int(updating)}-{int(is_board)}-{stage}')
                    self.dialogs.clear()
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
                    self.select(tab)
                    installed = app.installed_tab
                    installed.populate([dict(name=name, type=kind, path=str(old_path), archive=old_archive.name,
                                             installed_version='1.0.0', latest_version='2.0.0', update_available=True)])
                    self.select(installed)
                    self.until(lambda: installed._loaded_id == installed._select_id)
                    installed.refresh_update_button_state()
                    button = installed.update_btn if updating else tab.download_btn
                    failure = RuntimeError(f'Cannot {stage} download worker')
                    factory = Mock(side_effect=failure) if stage == 'create' else Mock(
                        return_value=SimpleNamespace(start=Mock(side_effect=failure)))
                    response = Mock(status_code=200, raw=None, headers={'content-length': str(len(data))}, raise_for_status=Mock(),
                                    iter_content=Mock(return_value=iter([data])), close=Mock())
                    with patch.object(app, '_prompt_download_option', return_value='zip'), \
                            patch.object(browser.requests, 'get', return_value=response) as get, \
                            patch('src.modules.board_preparation.start_preparation') as prepare, \
                            patch('urllib.request.urlopen', side_effect=AssertionError('Live transfer attempted')):
                        with patch.object(loading.threading, 'Thread', factory):
                            button.click()
                            self.assertTrue(app._busy)
                            self.assertIn('Cancel', tab.download_btn.text())
                            if updating:
                                self.assertIn('Cancel', installed.update_btn.text())
                            get.assert_not_called()
                            self.assertEqual(self.dialogs, [])
                        self.until(lambda: not app._busy and tasks_idle(app._tasks))
                        prefix = 'Unable to start download update:' if updating else 'Unable to start download:'
                        self.assertEqual(self.dialogs, [('critical', (app, 'Download Error', f'{prefix}\n{failure}'))])
                        self.assertEqual(app.status_label.text(), 'Download failed')
                        self.assertIsNone(app._active_download_tab)
                        self.assertIsNone(app._downloading_item_name)
                        self.pump(.05)
                        self.settle()
                        self.assertIn('Download', tab.download_btn.text())
                        self.assertIn('Update', installed.update_btn.text())
                        self.assertTrue(button.isEnabled())
                        self.assertEqual((app.progress.minimum(), app.progress.maximum(), app.progress.value()), (0, 1, 0))
                        self.assertFalse((destination / 'fixture.zip').exists())
                        self.assertEqual((old_path / 'keep.txt').read_bytes(), b'Previous installed payload')
                        self.assertEqual(old_archive.read_bytes(), b'Previous archive')
                        get.assert_not_called()
                        # The next explicit click reaches the real worker, using fixture HTTP bytes only.
                        button.click()
                        self.assertTrue(app._busy)
                        self.until(lambda: not app._busy and tasks_idle(app._tasks))
                        get.assert_called_once()
                        response.close.assert_called_once()
                        if updating:
                            self.assertFalse((destination / 'fixture.zip').exists())
                            self.assertTrue((destination / 'fixture' / ('boards.txt' if is_board else 'library.properties')).exists())
                        else:
                            self.assertEqual((destination / 'fixture.zip').read_bytes(), data)
                        if updating and is_board:
                            prepare.assert_called_once()
                            self.assertEqual(prepare.call_args.kwargs['package_metadata']['version'], '2.0.0')
                            self.assertEqual(app.status_label.text(), 'Board preparation is running in the background')
                        else:
                            prepare.assert_not_called()
                            self.assertEqual(app.status_label.text(), 'Download complete')
                        self.assertEqual(len(self.dialogs), 1)
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
        detail.listbox.setCurrentIndex(detail.model.index(1))
        detail._all_examples = [str(self.folder / 'old/Blink.ino')]
        detail.examples_search.setText('Blink')
        self.assertIn('Scanning', detail.examples_model.stringList()[0])
        detail._loaded_id = detail._select_id
        detail._all_examples = [str(self.folder / 'new/Blink.ino'), str(self.folder / 'new/Read.ino')]
        detail._render_examples()
        self.assertEqual(detail._current_examples, [str(self.folder / 'new/Blink.ino')])
        detail.search_entry.setText('sensor')
        detail.execute_search()
        self.assertEqual(detail.listbox.currentIndex().row(), 1)


if __name__ == '__main__':
    suite = unittest.TestSuite()
    for cls in (QtTaskLifecycleChecks, EngineImportChecks, CacheChecks, FolderUpdateChecks, InstalledUpdateChecks,
                BrowserSleepChecks, BrowserLoadingChecks):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
