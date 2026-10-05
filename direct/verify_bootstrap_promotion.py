"""Exercise the cold-to-original setup window handoff without running installers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'direct'))
sys.path.insert(0, str(ROOT))


def verify_sink_swap():
    """A handoff in a taken batch must deliver each remaining event once."""
    from src.modules.bootstrap_dispatch import BootstrapDispatcher
    dispatcher = BootstrapDispatcher()
    received = []
    def replacement(name, args):
        received.append(('qt', name, args))
    def initial(name, args):
        if name == 'call':
            dispatcher.attach(replacement)
        else:
            received.append(('native', name, args))
    dispatcher.attach(initial)
    def worker():
        dispatcher.sig_log.emit('Before handoff', 'normal')
        dispatcher.sig_call.emit(lambda: None, ())
        dispatcher.sig_log.emit('After handoff', 'normal')
        dispatcher.sig_status.emit('Still the same setup')
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    dispatcher.drain()
    assert received == [('native', 'log', ('Before handoff', 'normal')),
                        ('qt', 'log', ('After handoff', 'normal')),
                        ('qt', 'status', ('Still the same setup',))]
    dispatcher.close()


def verify_qt_origin_guard(folder):
    """Embedded Qt support is virtual; foreign native/package graphs stay blocked."""
    import verify_bootstrap_native as native_checks
    gui, namespace = native_checks.fixture()
    gui.close()
    root = folder / 'qt-origin-guard'
    site = root / 'env' / 'Lib' / 'site-packages'
    for package in ('PySide6', 'shiboken6'):
        (site / package).mkdir(parents=True, exist_ok=True)
        (site / package / '__init__.py').write_text('# Isolated path fixture\n', encoding='utf-8')
    namespace['SCRIPT_DIR'] = root
    prepare = namespace['_prepare_bootstrap_qt_path']
    loader_class = type('EmbeddableZipImporter', (), {'__module__': 'signature_bootstrap'})
    loader = loader_class()

    def module(path, embedded=False, spec_origin=None):
        return SimpleNamespace(__file__=str(path), __loader__=loader if embedded else None,
                               __spec__=SimpleNamespace(origin=spec_origin,
                                                        loader=loader if embedded else None))

    parents = {name: module(site / name / '__init__.py') for name in ('PySide6', 'shiboken6')}
    support = module('shibokensupport/feature.py', embedded=True)
    cases = [
        ('trusted_embedded_support', {**parents, 'PySide6.support.feature': support}, True),
        ('foreign_qt_native', {**parents, 'PySide6.QtWidgets': module(root / 'foreign' / 'QtWidgets.pyd')}, False),
        ('foreign_shiboken_native', {**parents, 'shiboken6.Shiboken': module(root / 'foreign' / 'Shiboken.pyd')}, False),
        ('foreign_qt_package', {**parents, 'PySide6': module(root / 'foreign' / '__init__.py')}, False),
        ('arbitrary_relative_native', {**parents, 'PySide6.QtWidgets': module('QtWidgets.pyd')}, False),
        ('arbitrary_relative_native_inside_env', {**parents, 'PySide6.QtWidgets': module('QtWidgets.pyd')}, False),
        ('arbitrary_relative_python', {**parents, 'PySide6.support.feature': module('foreign/feature.py', embedded=True)}, False),
        ('embedded_without_trusted_parent', {'PySide6.support.feature': support}, False),
        ('embedded_with_wrong_loader', {**parents, 'PySide6.support.feature': module('shibokensupport/feature.py')}, False),
        ('embedded_with_physical_spec', {**parents, 'PySide6.support.feature': module('shibokensupport/feature.py', embedded=True, spec_origin=str(root / 'foreign' / 'feature.py'))}, False),
    ]
    results = []
    for name, modules, accepted in cases:
        with patch.dict(sys.modules, modules), patch.object(sys, 'path', list(sys.path)):
            before = {key: value for key, value in sys.modules.items()
                      if key == 'PySide6' or key.startswith('PySide6.') or key == 'shiboken6' or key.startswith('shiboken6.')}
            previous_cwd = Path.cwd()
            try:
                if name == 'arbitrary_relative_native_inside_env':
                    os.chdir(site)
                try:
                    prepare()
                except ImportError:
                    actual = False
                else:
                    actual = True
            finally:
                os.chdir(previous_cwd)
            assert actual == accepted, (name, actual, accepted)
            assert all(sys.modules.get(key) is value for key, value in before.items()), 'The guard unloaded or replaced a loaded Qt module'
        results.append({'scenario': name, 'accepted': actual, 'passed': True})
    (folder / 'qt-origin-guard.json').write_text(json.dumps(results, indent=2), encoding='utf-8')


def warm_child(mode, folder):
    import verify_bootstrap_native as native_checks
    sys.meta_path[:] = [finder for finder in sys.meta_path if not isinstance(finder, native_checks.NoQt)]
    gui, namespace = native_checks.fixture(mode, has_qt=True, preimport_qt=True)
    result = {'scenario': 'warm', 'theme': mode, 'qt_preimported': True, 'errors': []}
    window = gui._window
    gui.log_ok('Verified runtime opens the original setup design immediately')
    gui.set_status('Runtime verification')
    def inspect():
        try:
            from src.modules.bootstrap_qt import BootstrapDialog
            assert gui._native_window is None
            assert isinstance(window, BootstrapDialog) and window.isVisible()
            assert gui.request_qt_promotion()
            assert gui._window is window
            assert namespace['save_bootstrap_config'].call_count == 0
            assert 'border-radius: 12px' in window.styleSheet()
            assert not window.details_expanded
            assert window.log_stack.currentWidget() is window.summary_edit
            assert 'Verified runtime opens' in window.summary_edit.toPlainText()
            assert window.grab().save(str(folder / f'original-qt-{mode}-warm.png'))
            result['passed'] = True
        except BaseException:
            result['errors'].append(traceback.format_exc())
        finally:
            gui.close()
    gui.root.after(80, inspect)
    gui.mainloop_until_done()
    (folder / f'{mode}-warm.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return 0 if result.get('passed') else 1


def retry_child(mode, folder):
    """A failed display restore must permit a later safe promotion in this process."""
    import verify_bootstrap_native as native_checks
    gui, namespace = native_checks.fixture(mode)
    native, dispatcher = gui._native_window, gui._signals
    native._set_details_expanded(True)
    started = gui._start_time
    for number in range(140):
        gui.log_status(f'Retry retained row {number:03d}')
    native.auto_var.set(False)
    native._auto()
    native.log_edit.yview_moveto(.3)
    gui.pump()
    expected = native.log_edit.get('1.0', 'end-1c')
    expected_top = native.log_edit.get(native.log_edit.index('@0,0'), native.log_edit.index('@0,0 lineend'))
    sys.meta_path[:] = [finder for finder in sys.meta_path if not isinstance(finder, native_checks.NoQt)]
    loader = namespace['_load_bootstrap_qt']
    state = {'rejected': False, 'restore': None}
    result = {'scenario': 'retry', 'theme': mode, 'errors': [], 'callbacks': []}

    def failing_once_loader():
        app, cls = loader()
        if state['restore'] is None:
            state['restore'] = cls.restore_snapshot
            def restore(self, snapshot):
                if not state['rejected']:
                    state['rejected'] = True
                    raise OSError('Deliberate isolated first restore failure')
                return state['restore'](self, snapshot)
            cls.restore_snapshot = restore
        return app, cls
    namespace['_load_bootstrap_qt'] = failing_once_loader

    def inspect():
        try:
            assert result['first_promotion'] is False
            assert result['second_promotion'] is True
            assert gui._signals is dispatcher and gui._start_time == started
            assert gui._native_window is None and native.closed
            window = gui._window
            assert window.isVisible() and not window.auto_scroll_cb.isChecked()
            assert window.details_expanded
            assert window.log_stack.currentWidget() is window.log_edit
            actual = window.log_edit.toPlainText()
            for line in expected.splitlines():
                if line.strip():
                    assert actual.count(line) == expected.count(line), repr(line)
            assert actual.count('Could not open the original setup view') == 1
            assert window.log_edit.firstVisibleBlock().text() == expected_top
            assert result['callbacks'] == list(range(70))
            assert namespace['save_bootstrap_config'].call_count == 0
            assert window.grab().save(str(folder / f'original-qt-{mode}-retry.png'))
            result['passed'] = True
        except BaseException:
            result['errors'].append(traceback.format_exc())
        finally:
            gui.close()

    def worker():
        try:
            for index in range(35):
                gui.root.after(0, lambda index=index: result['callbacks'].append(index))
            result['first_promotion'] = gui.request_qt_promotion()
            result['second_promotion'] = gui.request_qt_promotion()
            for index in range(35, 70):
                gui.root.after(0, lambda index=index: result['callbacks'].append(index))
            gui.root.after(0, inspect)
        except BaseException:
            result['errors'].append(traceback.format_exc())
            gui.close()
    thread = threading.Thread(target=worker)
    thread.start()
    gui.mainloop_until_done()
    thread.join(5)
    result['worker_finished'] = not thread.is_alive()
    assert result['worker_finished']
    (folder / f'{mode}-retry.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return 0 if result.get('passed') else 1


def summary_child(mode, folder):
    """Promote the default clean reading surface without replaying raw events."""
    import verify_bootstrap_native as native_checks
    gui, namespace = native_checks.fixture(mode)
    native, dispatcher = gui._native_window, gui._signals
    started = gui._start_time
    assert not native._details_expanded
    gui.log_section('Preparing Python dependencies')
    for number in range(110):
        gui.log_status(f'Summary row {number:03d} · café e\u0301')
    gui.log_status('pyserial: Collecting pyserial')
    gui.log_status('pyserial: Downloading pyserial-3.5.whl.metadata (1.6 kB)')
    gui.log_status('pyserial: Saved .\\fixture\\wheelhouse\\pyserial.whl')
    gui.log_ok('Downloaded pyserial successfully.')
    gui.log_ok('Downloaded pyserial successfully.')
    gui.log_warn('Custom board framework is unavailable')
    # A technical output line may carry an error before the worker emits a
    # top-level failed result. Its clean failed-step state must also transfer.
    gui.log_dim('ERROR: Required tool package is missing')
    gui.log_dim('pip: No matching distribution found for fixture-tool')
    divider = '  ' + '─' * 104
    gui.update_pip_table_block(
        f'{divider}\n  pyserial                                  ⬇ Downloading...\n'
        f'  ▰▰▱▱  40%\n{divider}\n  PySide6                                   ✖ Install Failed\n'
        f'  ▰▰▰▰  100%\n{divider}\n')
    status = 'Installing Python dependencies (' + ', '.join(f'fixture-package-{i}' for i in range(14)) + ')...'
    gui.set_status(status)
    native.auto_var.set(False)
    native._auto()
    native.summary_edit.yview_moveto(.25)
    selected = native.summary_edit.search('café e\u0301', '1.0')
    end = native.summary_edit.index(selected + ' + 7 chars')
    native.summary_edit.tag_add('sel', selected, end)
    native.summary_edit.mark_set('insert', end)
    gui.pump()
    expected_selection = native.summary_edit.get('sel.first', 'sel.last')
    expected_top = native.summary_edit.get(native.summary_edit.index('@0,0'), native.summary_edit.index('@0,0 lineend'))
    snapshot = native.snapshot()
    assert not snapshot['step_failed']
    assert snapshot['summary']['step_failed']
    assert snapshot['summary']['text'].count('Downloaded pyserial successfully.') == 1
    assert '.whl.metadata' not in snapshot['summary']['text']
    assert '.whl.metadata' in snapshot['text']
    sys.meta_path[:] = [finder for finder in sys.meta_path if not isinstance(finder, native_checks.NoQt)]
    result = {'scenario': 'summary', 'theme': mode, 'errors': [], 'callbacks': []}

    def inspect():
        try:
            window = gui._window
            assert result['promoted'] and window.isVisible() and native.closed
            assert gui._signals is dispatcher and gui._start_time == started
            assert not window.details_expanded
            assert window.log_stack.currentWidget() is window.summary_edit
            assert window.summary_edit.toPlainText() == snapshot['summary']['text']
            assert window.log_edit.toPlainText() == snapshot['text']
            assert window._presentation.snapshot() == snapshot['presentation']
            assert window._summary_failed
            assert window.summary_edit.textCursor().selectedText().replace('\u2029', '\n') == expected_selection
            assert window.summary_edit.firstVisibleBlock().text() == expected_top
            assert not window.auto_scroll_cb.isChecked()
            assert window.package_panel.isVisible()
            displayed = [row for row in window.package_rows if row.isVisible()]
            assert len(displayed) == 2
            assert any('pyserial' in row.status_lbl.text() and row.pct_lbl.text() == '40%' for row in displayed)
            assert any('PySide6' in row.status_lbl.text() and row.pct_lbl.text() == '100%' for row in displayed)
            assert '14 packages' in window.status_lbl.text()
            assert window.status_lbl.toolTip() == status
            for message, color_key in (('Custom board framework is unavailable', 'T_RED'),
                                       ('Required tool package is missing', 'T_RED')):
                cursor = window.summary_edit.document().find(message)
                assert not cursor.isNull()
                assert cursor.charFormat().foreground().color().name().lower() == window._theme_pal[color_key].lower()
            before = window.summary_edit.toPlainText()
            gui.log_ok('Downloaded pyserial successfully.')
            assert window.summary_edit.toPlainText() == before, 'Promotion lost stage-scoped success deduplication'
            gui.log_warn('Custom board framework is unavailable')
            assert window.summary_edit.toPlainText().count('Custom board framework is unavailable') == 2
            assert window.summary_edit.textCursor().selectedText().replace('\u2029', '\n') == expected_selection
            assert window.summary_edit.firstVisibleBlock().text() == expected_top
            gui.log_section('Separate verification stage')
            gui.log_ok('Downloaded pyserial successfully.')
            assert window.summary_edit.toPlainText().count('Downloaded pyserial successfully.') == 2
            assert not window.package_panel.isVisible()
            assert window.grab().save(str(folder / f'original-qt-{mode}-summary.png'))
            window._set_details_expanded(True)
            assert window.log_stack.currentWidget() is window.log_edit
            assert '.whl.metadata' in window.log_edit.toPlainText()
            window._set_details_expanded(False)
            assert window.summary_edit.textCursor().selectedText().replace('\u2029', '\n') == expected_selection
            assert result['callbacks'] == list(range(70))
            namespace['save_bootstrap_config'].assert_not_called()
            namespace['_record_bootstrap_exception'].assert_not_called()
            result.update(passed=True, summary_events=len(snapshot['presentation']['events']),
                          rows=snapshot['presentation']['rows'], first_visible=expected_top,
                          selection=expected_selection, details_expanded=window.details_expanded)
        except BaseException:
            result['errors'].append(traceback.format_exc())
        finally:
            gui.close()

    def worker():
        try:
            for index in range(35):
                gui.root.after(0, lambda index=index: result['callbacks'].append(index))
            result['promoted'] = gui.request_qt_promotion()
            for index in range(35, 70):
                gui.root.after(0, lambda index=index: result['callbacks'].append(index))
            gui.root.after(0, inspect)
        except BaseException:
            result['errors'].append(traceback.format_exc())
            gui.close()
    thread = threading.Thread(target=worker)
    thread.start()
    gui.mainloop_until_done()
    thread.join(5)
    assert not thread.is_alive()
    (folder / f'{mode}-summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return 0 if result.get('passed') else 1


def child(scenario, mode, folder):
    if scenario == 'warm':
        return warm_child(mode, folder)
    if scenario == 'retry':
        return retry_child(mode, folder)
    if scenario == 'summary':
        return summary_child(mode, folder)
    import verify_bootstrap_native as native_checks
    gui, namespace = native_checks.fixture(mode)
    native = gui._native_window
    native._set_details_expanded(True)
    started = time.time() - 394
    gui._start_time = started
    gui._step_index = 5
    gui.set_step_progress(5, 9)
    gui.set_status('Preparing complete offline board/library packs')
    for number in range(140):
        gui.log_status(f'Retained row {number:03d}')
    gui.log_section('Failed earlier attempt')
    gui.log_status('Unicode diagnostic: 🧪 café e\u0301')
    gui.log_fail('Retained failure details')
    gui.log_section('Dependency verification succeeded')
    gui.update_pip_table_block('PACKAGE 120%\nRetained live table\n')
    native.auto_var.set(False)
    native._auto()
    native.log_edit.yview_moveto(.35)
    selected = native.log_edit.search('🧪 café e\u0301', '1.0')
    end = native.log_edit.index(selected + ' + 10 chars')
    native.log_edit.tag_add('sel', selected, end)
    native.log_edit.mark_set('insert', end)
    gui.pump()
    expected_selection = native.log_edit.get('sel.first', 'sel.last')
    expected_top = native.log_edit.get(native.log_edit.index('@0,0'), native.log_edit.index('@0,0 lineend'))
    expected_text = native.log_edit.get('1.0', 'end-1c')
    native_snapshot = native.snapshot()
    dispatcher = gui._signals
    result = {'scenario': scenario, 'theme': mode, 'cold_visible': bool(native.root.winfo_viewable()),
              'initial_qt_app': gui._app is not None, 'expected_top': expected_top,
              'expected_selection': expected_selection, 'errors': [], 'callbacks': []}

    if scenario != 'unavailable':
        # Model verified dependency availability. No pip, package manager or
        # production bootstrap import is called by this UI-only fixture.
        sys.meta_path[:] = [finder for finder in sys.meta_path if not isinstance(finder, native_checks.NoQt)]

    if scenario == 'rollback':
        loader = namespace['_load_bootstrap_qt']
        def failing_loader():
            app, window_class = loader()
            def reject_snapshot(self, snapshot):
                raise OSError('Deliberate restore failure after Qt became available')
            window_class.restore_snapshot = reject_snapshot
            return app, window_class
        namespace['_load_bootstrap_qt'] = failing_loader

    if scenario == 'held':
        bar = native.scrollbar
        positions = [y for y in range(bar.winfo_height()) if bar.identify(bar.winfo_width() // 2, y) == 'thumb']
        point = (bar.winfo_width() // 2, positions[len(positions) // 2])
        bar.event_generate('<ButtonPress-1>', x=point[0], y=point[1])
        assert native.held
        native.root.after(70, lambda: result.update(held_wait_kept_native=not native.closed and gui._app is None))
        native.root.after(180, lambda: bar.event_generate('<ButtonRelease-1>', x=point[0], y=point[1]))

    def inspect():
        try:
            assert gui._signals is dispatcher, 'The worker dispatcher changed'
            assert gui._start_time == started, 'The elapsed start time reset'
            if scenario in ('unavailable', 'rollback'):
                assert not result['promoted']
                assert gui._app is None and not native.closed
                assert native.root.winfo_viewable()
                retained = native.log_edit.get('1.0', 'end-1c')
                result['fallback_text'] = retained
                for line in expected_text.splitlines():
                    if line.strip():
                        assert retained.count(line) == expected_text.count(line), repr(line)
                namespace['_record_bootstrap_exception'].assert_called()
                if scenario == 'rollback':
                    from PySide6.QtWidgets import QApplication
                    assert not any(widget.isVisible() for widget in QApplication.instance().topLevelWidgets())
            else:
                from src.modules.bootstrap_qt import BootstrapDialog
                window = gui._window
                assert result['promoted'] and isinstance(window, BootstrapDialog)
                assert window.isVisible() and native.closed
                assert window.details_expanded
                assert window.log_stack.currentWidget() is window.log_edit
                assert window.log_edit.toPlainText() == expected_text
                assert window.summary_edit.toPlainText() == native_snapshot['summary']['text']
                assert window._presentation.snapshot() == native_snapshot['presentation']
                assert window.log_edit.textCursor().selectedText().replace('\u2029', '\n') == expected_selection
                assert not window.auto_scroll_cb.isChecked()
                result['actual_top'] = window.log_edit.firstVisibleBlock().text()
                result['snapshot_top_offset'] = native_snapshot['top_offset']
                result['qt_scroll_value'] = window.log_edit.verticalScrollBar().value()
                assert window.log_edit.firstVisibleBlock().text() == expected_top, result['actual_top']
                assert 'border-radius: 12px' in window.styleSheet()
                cursor = window.log_edit.document().find('Retained failure details')
                assert cursor.charFormat().foreground().color().name().lower() == window._theme_pal['T_RED'].lower()
                gui.update_pip_table_block('PACKAGE 280%\nReplacement live table\n')
                assert 'PACKAGE 120%' not in window.log_edit.toPlainText()
                assert window.log_edit.toPlainText().count('PACKAGE 280%') == 1
                assert window.log_edit.textCursor().selectedText().replace('\u2029', '\n') == expected_selection
                assert window.log_edit.firstVisibleBlock().text() == expected_top
                result.update(qt_class=type(window).__name__, first_visible=window.log_edit.firstVisibleBlock().text(),
                              selection=window.log_edit.textCursor().selectedText(),
                              elapsed_seconds=time.time() - gui._start_time,
                              qt_geometry=[window.width(), window.height()],
                              auto_scroll=window.auto_scroll_cb.isChecked(),
                              pending_callbacks=list(result['callbacks']))
                if scenario == 'held':
                    assert result.get('held_wait_kept_native')
                # Capture this fixture's QWidget only, never the desktop.
                assert window.grab().save(str(folder / f'original-qt-{mode}-{scenario}.png'))
            assert result['callbacks'] == list(range(70)), 'Queued callbacks were lost or repeated'
            assert namespace['save_bootstrap_config'].call_count == 0
            result['passed'] = True
        except BaseException:
            result['errors'].append(traceback.format_exc())
            result['passed'] = False
        finally:
            gui.close()

    def worker():
        try:
            # This spans more than one drain batch around the sink swap.
            for index in range(35):
                gui.root.after(0, lambda index=index: result['callbacks'].append(index))
            result['promoted'] = gui.request_qt_promotion()
            for index in range(35, 70):
                gui.root.after(0, lambda index=index: result['callbacks'].append(index))
            gui.root.after(0, inspect)
        except BaseException:
            result['errors'].append(traceback.format_exc())
            gui.close()

    thread = threading.Thread(target=worker)
    thread.start()
    gui.mainloop_until_done()
    thread.join(5)
    result['worker_finished'] = not thread.is_alive()
    assert result['worker_finished']
    path = folder / f'{mode}-{scenario}.json'
    path.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'scenario': scenario, 'theme': mode, 'passed': result.get('passed'), 'report': str(path)}))
    return 0 if result.get('passed') else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--child', choices=('success', 'held', 'unavailable', 'rollback', 'warm', 'retry', 'summary'))
    parser.add_argument('--theme', default='default')
    args = parser.parse_args()
    folder = args.report_dir.resolve()
    assert folder.is_relative_to((ROOT / 'temp').resolve())
    folder.mkdir(parents=True, exist_ok=True)
    if args.child:
        return child(args.child, args.theme, folder)
    verify_sink_swap()
    verify_qt_origin_guard(folder)
    results = []
    for scenario, mode in [('success', 'default'), ('success', 'light'), ('success', 'solarized_dark'),
                           ('held', 'default'), ('unavailable', 'default'),
                           ('rollback', 'default'), ('warm', 'default'), ('retry', 'default'),
                           ('summary', 'default')]:
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--report-dir', str(folder),
                   '--child', scenario, '--theme', mode]
        env = {**os.environ, 'QT_QPA_PLATFORM': 'windows' if os.name == 'nt' else 'offscreen',
               'PYTHONDONTWRITEBYTECODE': '1'}
        path = folder / f'{mode}-{scenario}.log'
        with path.open('w', encoding='utf-8') as output:
            completed = subprocess.run(command, cwd=ROOT, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, timeout=35)
        results.append({'scenario': scenario, 'theme': mode, 'exit_code': completed.returncode})
    (folder / 'summary.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2))
    return 0 if all(item['exit_code'] == 0 for item in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
