"""Isolated package-job delivery and real cross-process lease checks.

All records, locks and fake tool stores live under temp/. No installers,
hardware, workspace configuration or live package stores are opened.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import package_jobs as jobs


def lease_child(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--lease-child', action='store_true')
    parser.add_argument('--events-root', required=True, type=Path)
    parser.add_argument('--core', required=True, type=Path)
    parser.add_argument('--mode', choices=('use', 'prepare'), required=True)
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args(argv)
    try:
        with jobs.package_store_lease(args.core, args.mode, wait=args.wait,
                                      root=args.events_root,
                                      on_wait=lambda: print('queued', flush=True)):
            print('acquired', flush=True)
            sys.stdin.readline()
        print('released', flush=True)
        return 0
    except jobs.PackageStoreBusy:
        print('busy', flush=True)
        return 0


class PackageJobChecks(unittest.TestCase):
    def setUp(self):
        parent = ROOT / 'temp' / 'audit' / 'package-jobs'
        parent.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(fixture.cleanup)
        self.folder = Path(fixture.name)
        self.events = self.folder / 'events'
        self.core = self.folder / 'fake-tool-store'
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for process, _ in self.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def child(self, mode, wait=False):
        command = [sys.executable, '-B', str(Path(__file__).resolve()),
                   '--lease-child', '--events-root', str(self.events),
                   '--core', str(self.core), '--mode', mode]
        if wait:
            command.append('--wait')
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8',
                                   creationflags=0x08000000 if os.name == 'nt' else 0)
        output = queue.Queue()
        def collect():
            for line in process.stdout:
                output.put(line.strip())
        threading.Thread(target=collect, daemon=True).start()
        self.children.append((process, output))
        return process, output

    def expect(self, child, expected):
        process, output = child
        try:
            actual = output.get(timeout=5)
        except queue.Empty:
            self.fail(f'Lease child stalled waiting for {expected!r}; exit={process.poll()}')
        self.assertEqual(actual, expected)

    def release(self, child):
        process, _ = child
        process.stdin.write('\n')
        process.stdin.flush()
        self.expect(child, 'released')
        self.assertEqual(process.wait(timeout=5), 0)

    def test_independent_readers_keep_transitions_and_coalesce_percentages(self):
        readers = [jobs.JobReader(root=self.events, since=0) for _ in range(2)]
        jobs.publish_event('board', 'queued', root=self.events, title='NodeMCU')
        jobs.publish_event('board', 'downloading', root=self.events, progress=1)
        for value in range(2, 101):
            jobs.publish_event('board', 'downloading', root=self.events, progress=value)
        for reader in readers:
            updates = reader.read_updates()
            self.assertEqual(len(updates), 1)
            item, transitions = updates[0]
            self.assertEqual(item['title'], 'NodeMCU')
            self.assertEqual(item['progress'], 100)
            self.assertEqual([row['stage'] for row in transitions], ['queued', 'downloading'])
            self.assertEqual(reader.read_updates(), [])
        jobs.publish_event('board', 'failed', root=self.events, message='Checksum mismatch')
        for reader in readers:
            item, transitions = reader.read_updates()[0]
            self.assertEqual(item['stage'], 'failed')
            self.assertEqual([row['message'] for row in transitions], ['Checksum mismatch'])

    def test_aggregate_unicode_record_bound_keeps_latest_diagnostic(self):
        diagnostic = 'Failed board preparation: ' + '\N{GREEK SMALL LETTER LAMDA}' * 1900
        for index in range(40):
            jobs.publish_event('large', 'preparing' if index % 2 else 'downloading',
                               root=self.events, message='\N{ROCKET}' * 2048,
                               context=['\N{ROCKET}' * 2048] * 32)
        jobs.publish_event('large', 'failed', root=self.events, message=diagnostic,
                           context=['\N{ROCKET}' * 2048] * 32)
        self.assertLessEqual((self.events / 'large.json').stat().st_size, 65536)
        updates = jobs.JobReader(root=self.events, since=0).read_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0]['message'], diagnostic)
        self.assertEqual(updates[0][1][-1]['stage'], 'failed')

    def test_request_and_coverage_files_preserve_every_board(self):
        coverage = {'boards': [{'name': f'Board {value}', 'reason': 'No exact definition'}
                               for value in range(300)]}
        metadata = {'boards': ['Vendor board ' + str(value) + ' x' * 200
                              for value in range(300)]}
        request = jobs.write_request('vendor', metadata, root=self.events)
        report = jobs.write_report('vendor', coverage, root=self.events)
        self.assertEqual(json.loads(request.read_text(encoding='utf-8')), metadata)
        self.assertEqual(json.loads(report.read_text(encoding='utf-8')), coverage)
        self.assertGreater(request.stat().st_size, 32767)
        self.assertEqual(jobs.JobReader(root=self.events, since=0).read_updates(), [])

    def test_failed_atomic_replace_preserves_previous_event(self):
        jobs.publish_event('board', 'downloading', root=self.events, progress=17)
        previous = (self.events / 'board.json').read_bytes()
        with patch.object(jobs.os, 'replace', side_effect=PermissionError('denied fixture')):
            with self.assertRaises(PermissionError):
                jobs.publish_event('board', 'failed', root=self.events, message='Fixture')
        self.assertEqual((self.events / 'board.json').read_bytes(), previous)
        self.assertEqual(list(self.events.glob('*.tmp')), [])

    def test_reader_ignores_corruption_and_detects_interrupted_worker_once(self):
        jobs.publish_event('stopped', 'preparing', root=self.events)
        path = self.events / 'stopped.json'
        item = json.loads(path.read_text(encoding='utf-8'))
        item.update(pid=999999999, updated=time.time() - 30)
        path.write_text(json.dumps(item), encoding='utf-8')
        (self.events / 'corrupt.json').write_text('{', encoding='utf-8')
        reader = jobs.JobReader(root=self.events, since=0)
        with patch.object(jobs, 'process_alive', return_value=False):
            updates = reader.read_updates()
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0][0]['stage'], 'interrupted')
            self.assertEqual(updates[0][1][-1]['stage'], 'interrupted')
            self.assertEqual(reader.read_updates(), [])

    def test_latest_job_not_hidden_by_old_abandoned_records(self):
        self.events.mkdir(parents=True)
        for value in range(140):
            path = self.events / f'abandoned-{value}.json'
            path.write_text('{}', encoding='utf-8')
            os.utime(path, (1, 1))
        jobs.publish_event('latest', 'preparing', root=self.events)
        updates = jobs.JobReader(root=self.events, since=0).read_updates()
        self.assertEqual([item['job_id'] for item, _ in updates], ['latest'])

    def test_completed_retention_preserves_active_jobs(self):
        jobs.write_request('active', {'name': 'Active'}, root=self.events)
        jobs.write_report('active', {'boards': []}, root=self.events)
        jobs.publish_event('active', 'preparing', root=self.events)
        for value in range(70):
            jobs.write_request(f'finished-{value}', {'name': f'Board {value}'}, root=self.events)
            jobs.write_report(f'finished-{value}', {'boards': []}, root=self.events)
            jobs.publish_event(f'finished-{value}', 'ready', root=self.events)
        retained = [json.loads(path.read_text(encoding='utf-8'))
                    for path in self.events.glob('*.json')]
        self.assertEqual(sum(item['stage'] in jobs.TERMINAL for item in retained), 48)
        self.assertIn('active', {item['job_id'] for item in retained})
        retained_ids = {item['job_id'] for item in retained}
        for folder in ('requests', 'reports'):
            self.assertEqual({path.stem for path in (self.events / folder).glob('*.json')},
                             retained_ids)

    def test_cross_process_readers_share_store_and_writer_excludes_them(self):
        with jobs.package_store_lease(self.core, root=self.events):
            reader = self.child('use')
            self.expect(reader, 'acquired')
            writer = self.child('prepare')
            self.expect(writer, 'busy')
            self.assertEqual(writer[0].wait(timeout=5), 0)
            self.release(reader)
        writer = self.child('prepare')
        self.expect(writer, 'acquired')
        with self.assertRaises(jobs.PackageStoreBusy):
            with jobs.package_store_lease(self.core, root=self.events):
                self.fail('Reader entered during exclusive preparation')
        other = self.child('prepare')
        self.expect(other, 'busy')
        self.assertEqual(other[0].wait(timeout=5), 0)
        self.release(writer)

    def test_queued_writer_reserves_store_before_existing_reader_finishes(self):
        with jobs.package_store_lease(self.core, root=self.events):
            writer = self.child('prepare', wait=True)
            self.expect(writer, 'queued')
            with self.assertRaises(jobs.PackageStoreBusy):
                with jobs.package_store_lease(self.core, root=self.events):
                    self.fail('New reader bypassed the queued writer')
        self.expect(writer, 'acquired')
        with self.assertRaises(jobs.PackageStoreBusy):
            with jobs.package_store_lease(self.core, root=self.events):
                self.fail('Reader bypassed the active writer')
        self.release(writer)
        with jobs.package_store_lease(self.core, root=self.events):
            pass

    def test_cancelled_writer_releases_reservation(self):
        cancel, waiting, done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def prepare():
            try:
                with jobs.package_store_lease(self.core, 'prepare', wait=True,
                                              cancel=cancel, on_wait=waiting.set,
                                              root=self.events):
                    errors.append(AssertionError('Cancelled writer acquired store'))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        with jobs.package_store_lease(self.core, root=self.events):
            worker = threading.Thread(target=prepare, daemon=True)
            worker.start()
            self.assertTrue(waiting.wait(3))
            cancel.set()
            self.assertTrue(done.wait(3))
        worker.join(3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], InterruptedError)
        with jobs.package_store_lease(self.core, root=self.events):
            pass

    def test_crashed_writer_does_not_leave_store_permanently_blocked(self):
        writer = self.child('prepare')
        self.expect(writer, 'acquired')
        writer[0].kill()
        writer[0].wait(timeout=5)
        with jobs.package_store_lease(self.core, root=self.events):
            pass

    def test_disk_write_denial_fails_promptly_without_infinite_queue(self):
        cancel, done = threading.Event(), threading.Event()
        errors = []
        def acquire():
            try:
                with jobs.package_store_lease(self.core, 'prepare', wait=True,
                                              cancel=cancel, root=self.events):
                    errors.append(AssertionError('Writer entered despite failed reservation'))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        with patch.object(jobs, '_atomic', side_effect=PermissionError('fixture read-only disk')):
            worker = threading.Thread(target=acquire, daemon=True)
            worker.start()
            returned = done.wait(1)
            cancel.set()
            worker.join(3)
        self.assertTrue(returned, 'Disk failure was silently retried instead of reported')
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], PermissionError)
        self.assertIn('read-only disk', str(errors[0]))
        with jobs.package_store_lease(self.core, root=self.events):
            pass

    def test_corrupt_lease_fails_closed_and_other_tool_store_stays_usable(self):
        lease_dir = jobs._store_directory(self.core, self.events)
        lease_dir.mkdir(parents=True)
        (lease_dir / 'unknown.json').write_text('{', encoding='utf-8')
        for mode in ('use', 'prepare'):
            with self.assertRaises(jobs.PackageStoreBusy):
                with jobs.package_store_lease(self.core, mode, root=self.events):
                    self.fail('Corrupt lease granted access')
        with jobs.package_store_lease(self.folder / 'other-store', root=self.events):
            pass

    def test_explicit_fixture_route_avoids_production_event_directory(self):
        with patch.dict(os.environ, MCU_PACKAGE_EVENTS_ROOT=str(self.events)):
            jobs.publish_event('routed', 'queued')
            with jobs.package_store_lease(self.core):
                self.assertTrue(self.events.is_dir())
        self.assertTrue((self.events / 'routed.json').is_file())

    def test_guard_rejects_before_operation_and_holds_lease_through_body(self):
        check = self
        class Backend:
            _package_event_root = check.events
            called = 0
            released = False
            emitted = []
            def emit(self, event, payload):
                self.emitted.append((event, payload))
            def _release_requested_operation(self):
                self.released = True
            @jobs.guarded_package_operation
            def operation(self):
                self.called += 1
                with check.assertRaises(jobs.PackageStoreBusy):
                    with jobs.package_store_lease(check.core, 'prepare', root=check.events):
                        check.fail('Writer entered while guarded operation was using packages')
                return 'completed'
        backend = Backend()
        with patch.object(jobs, 'package_core_directory', return_value=self.core):
            with jobs.package_store_lease(self.core, 'prepare', root=self.events):
                self.assertFalse(backend.operation())
            self.assertEqual(backend.called, 0)
            self.assertTrue(backend.released)
            self.assertEqual(backend.emitted[-1][1]['title'], 'Board preparation in progress')
            self.assertEqual(backend.operation(), 'completed')
        self.assertEqual(backend.called, 1)

    def test_guard_preserves_operation_error_and_releases_lease(self):
        class Backend:
            _package_event_root = self.events
            @jobs.guarded_package_operation
            def operation(self):
                raise OSError('fixture build failure')
            def emit(self, event, payload):
                raise AssertionError('Operation failure was relabeled as package coordination')
            def _release_requested_operation(self):
                raise AssertionError('Operation error handler was bypassed')
        with patch.object(jobs, 'package_core_directory', return_value=self.core):
            with self.assertRaisesRegex(OSError, 'fixture build failure'):
                Backend().operation()
        with jobs.package_store_lease(self.core, 'prepare', root=self.events):
            pass

    def windows_setup_worker(self, seed_error=None):
        """Execute the real worker while replacing every external setup action."""
        source = ROOT / 'src/modules/bootstrap.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('_bootstrap_tool_store_lease', '_run_setup_in_thread')]
        gui = Mock()
        gui._signals = SimpleNamespace(thread_id=-1)
        gui._package_event_root = self.events
        gui.root.after.side_effect = lambda delay, callback: callback()
        main_script = self.folder / 'mcu_flash_gui.py'
        main_script.write_text('# Isolated launch target\n', encoding='utf-8')
        namespace = dict(Path=Path, os=os, threading=threading,
                         sys=SimpleNamespace(platform='win32', executable=str(self.folder / 'env/Scripts/python.exe')),
                         subprocess=SimpleNamespace(PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT,
                             CREATE_NO_WINDOW=0x08000000, Popen=Mock(return_value=SimpleNamespace(wait=lambda: 0))),
                         time=SimpleNamespace(sleep=lambda delay: None), BootstrapGUI=object,
                         SCRIPT_DIR=self.folder, GUI_SCRIPT=main_script, BOOTSTRAP_CLOSE_DELAY_S=0,
                         _BOOTSTRAP_STARTUP_NOTE='', get_bootstrap_log_file=lambda: self.folder / 'run.log',
                         _record_bootstrap_exception=Mock(), _record_bootstrap_log=Mock(),
                         _prepare_bundled_windows_installers=Mock(return_value=(False, True, {'cp210x': True})),
                         _heal_private_python_runtime=Mock(return_value=True),
                         ensure_python_system_environment=Mock(return_value=True), ensure_pip=Mock(return_value=True),
                         ensure_pip_packages_parallel=Mock(return_value=True), _check_import_pywebview=Mock(return_value=True),
                         ensure_webview2_runtime=Mock(return_value=True),
                         _get_safe_platformio_core_dir=Mock(return_value=str(self.core)),
                         _apply_bootstrap_compiler_budget=Mock(return_value=2),
                         _stream_offline_setup_output=Mock(), _cp210x_driver_status_message=Mock(return_value=(True, 'Fixture')),
                         check_cp210x_driver=Mock(return_value=True), check_opencode_cli=Mock(return_value=True),
                         ensure_opencode_cli=Mock(return_value=True), run_update_checks=Mock(),
                         _write_startup_health_snapshot=Mock(return_value=False),
                         _cleanup_platformio_prebuilt_archive=Mock(return_value=True))
        calls = []
        def guarded_stage(name, error=None):
            def call(*args, **kwargs):
                calls.append(name)
                with self.assertRaises(jobs.PackageStoreBusy):
                    with jobs.package_store_lease(self.core, root=self.events):
                        self.fail(f'{name} mutated packages without exclusive repair lease')
                if error:
                    raise error
                return True
            return call
        def after_repair(*args, **kwargs):
            with jobs.package_store_lease(self.core, root=self.events):
                pass
            calls.append('after-repair')
            return True
        for name in ('_configure_platformio_environment', '_neutralize_conflicting_global_platformio_config',
                     'ensure_platformio', 'ensure_arduino_avr_board', 'ensure_esp32_board_folder'):
            namespace[name] = guarded_stage(name)
        namespace['_ensure_platformio_core_prebuilt'] = guarded_stage('seed', seed_error)
        namespace['_stream_offline_setup_output'] = guarded_stage('offline-packs')
        namespace['ensure_arduino_cli'] = after_repair
        namespace['_spawn_main_gui'] = Mock(side_effect=lambda: (SimpleNamespace(poll=lambda: None), None)
                                             if after_repair() else None)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
        from src.modules import offline_bootstrap
        with patch.object(jobs, 'package_core_directory', return_value=self.core), \
                patch.dict(os.environ, {'PLATFORMIO_CORE_DIR': str(self.core)}), \
                patch.object(offline_bootstrap, 'requested_plan', return_value=({'schema': 1, 'platforms': ['fixture']}, None)), \
                patch.object(offline_bootstrap, 'ready', side_effect=[False, True]), \
                patch.object(offline_bootstrap, 'clean_bootstrap_environment', return_value={}):
            namespace['_run_setup_in_thread'](gui)
        return namespace, gui, calls

    def test_windows_repair_guards_seed_scons_and_offline_packs_before_launch(self):
        namespace, gui, calls = self.windows_setup_worker()
        self.assertIn('seed', calls)
        self.assertIn('ensure_platformio', calls)
        self.assertIn('offline-packs', calls)
        self.assertEqual(calls[-2:], ['after-repair', 'after-repair'])
        namespace['_spawn_main_gui'].assert_called_once()
        gui.show_error.assert_not_called()

    def test_windows_repair_failure_releases_store_and_does_not_launch(self):
        namespace, gui, calls = self.windows_setup_worker(OSError('fixture seed denied'))
        namespace['_spawn_main_gui'].assert_not_called()
        self.assertEqual(calls[-1], 'seed')
        gui.show_error.assert_called_once()
        with jobs.package_store_lease(self.core, root=self.events):
            pass

    def test_windows_repair_refuses_to_wait_on_gui_thread(self):
        source = ROOT / 'src/modules/bootstrap.py'
        node = next(node for node in ast.parse(source.read_text(encoding='utf-8-sig')).body
                    if isinstance(node, ast.FunctionDef) and node.name == '_bootstrap_tool_store_lease')
        scope = {'threading': threading}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), scope)
        gui = SimpleNamespace(_signals=SimpleNamespace(thread_id=threading.get_ident()))
        with self.assertRaisesRegex(RuntimeError, 'not the GUI thread'):
            scope['_bootstrap_tool_store_lease'](gui, self.core)

    def test_ubuntu_repair_holds_native_store_only_during_package_child(self):
        spec = importlib.util.spec_from_file_location('package_setup_fixture', ROOT / 'direct/ubuntu/setup.py')
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        setup.ROOT, setup.ENV_DIR = self.folder, self.folder / '.venv-linux'
        actions = []
        def run(command, **kwargs):
            if any(str(part).endswith('offline_bootstrap.py') for part in command):
                self.assertEqual(command[command.index('--core') + 1], str(self.core))
                with self.assertRaises(jobs.PackageStoreBusy):
                    with jobs.package_store_lease(self.core, root=self.events):
                        self.fail('Ubuntu package child had no exclusive native-store lease')
                actions.append('offline')
            else:
                with jobs.package_store_lease(self.core, root=self.events):
                    pass
                actions.append('runtime-check')
        def launch(command, **kwargs):
            with jobs.package_store_lease(self.core, root=self.events):
                pass
            self.assertEqual(command[-3:], ['--project', '/fixture/Sketch With Spaces', '--new-window'])
            actions.append('launch')
            return 0
        with patch.object(setup, 'sys', SimpleNamespace(platform='linux', version_info=(3, 11), stderr=sys.stderr)), \
                patch.object(setup.os, 'geteuid', return_value=1000, create=True), \
                patch('src.modules.runtime_resources.enforce_minimum_cpu_requirement', return_value=True), \
                patch('src.modules.platform_runtime.native_platformio_dir', return_value=self.core), \
                patch('src.modules.offline_bootstrap.clean_bootstrap_environment', return_value={}), \
                patch.object(setup.venv, 'EnvBuilder') as builder, \
                patch.object(setup.subprocess, 'run', side_effect=run), \
                patch.object(setup.subprocess, 'call', side_effect=launch), \
                patch.dict(os.environ, {'MCU_PACKAGE_EVENTS_ROOT': str(self.events)}):
            self.assertEqual(setup.main(['--launch', '--project', '/fixture/Sketch With Spaces', '--new-window']), 0)
        self.assertEqual(actions, ['runtime-check', 'runtime-check', 'offline', 'launch'])
        builder.return_value.create.assert_called_once_with(setup.ENV_DIR)


if __name__ == '__main__':
    if '--lease-child' in sys.argv:
        raise SystemExit(lease_child(sys.argv[1:]))
    unittest.main(verbosity=2)
