#!/usr/bin/env python3
"""Real LLVM and conservative navigation in disposable sources; no Qt or hardware."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules.source_navigation import NavigationResolver, resolve_request


class NavigationChecks(unittest.TestCase):
    def setUp(self):
        base = ROOT / "temp/audit/source-navigation"
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def request(self, text, marker, *, files=(), **options):
        path = self.root / 'sketch.cpp'
        path.write_text(text, encoding='utf-8')
        return dict(path=str(path), project=str(self.root), offset=text.rindex(marker),
                    snapshots=[(str(path), text), *files], **options)

    def test_conservative_lookup_distinguishes_arity_and_keeps_unknown_types_ambiguous(self):
        text = 'void stepRun() {}\nvoid stepRun(int value) {}\nvoid run() { stepRun(3); }\n'
        result = resolve_request(self.request(text, 'stepRun(3)'), semantic=False)
        self.assertEqual([item['line'] for item in result['destinations']], [1])
        text = 'void stepRun(int value) {}\nvoid stepRun(float value) {}\nvoid run() { stepRun(unknown); }'
        result = resolve_request(self.request(text, 'stepRun(unknown)'), semantic=False)
        self.assertEqual(len(result['destinations']), 2)

    def test_defaults_nested_calls_literals_and_variadic_arity(self):
        for call in ('stepRun(1, nested(2, 3))', 'stepRun(1, "x,y")'):
            text = 'void stepRun() {}\nvoid stepRun(int a, const char *b = "") {}\nvoid run() { ' + call + '; }'
            result = resolve_request(self.request(text, call), semantic=False)
            self.assertEqual([item['line'] for item in result['destinations']], [1])
        text = 'void stepRun() {}\nvoid stepRun(int value = 1) {}\nvoid run() { stepRun(); }'
        self.assertEqual(len(resolve_request(self.request(text, 'stepRun();'), semantic=False)['destinations']), 2)

    def test_explicit_header_resolution_returns_locked_source_and_exact_symbol_range(self):
        library = self.root / 'library/src'
        library.mkdir(parents=True)
        header = library / 'motor.h'
        header.write_text('void stepRun();\nvoid stepRun(int value);\n', encoding='utf-8')
        text = '#include <motor.h>\nvoid run() { stepRun(4); }'
        result = resolve_request(self.request(text, 'stepRun(4)', include_roots=[str(library)]), semantic=False)
        self.assertEqual(len(result['destinations']), 1)
        item = result['destinations'][0]
        self.assertEqual(item['path'], str(header))
        self.assertTrue(item['read_only'])
        self.assertEqual(item['content'].encode('utf-8'), header.read_bytes())
        self.assertEqual(item['content'].splitlines()[item['line']][item['column']:item['end_column']], 'stepRun')
        self.assertEqual(header.read_bytes(), item['content'].encode('utf-8'))

    def test_an_include_directive_opens_its_header_from_installed_libraries_before_a_compile(self):
        # What happened: Ctrl+Click on a library showed an empty "Choose a declaration"
        # box, because nothing knew where installed libraries live until a compile.
        store = self.root / 'store'
        header = store / 'ESP32Servo/src/ESP32Servo.h'
        header.parent.mkdir(parents=True)
        header.write_text('#ifndef SERVO_H\n#define SERVO_H\nclass Servo { public: int attach(int pin); };\n#endif\n')
        roots = [str(header.parent)]
        text = '#include <ESP32Servo.h>\nServo servo;\nvoid run() { servo.attach(23); }\n'
        for marker in ('ESP32Servo.h>', '#include'):
            with self.subTest(marker=marker):
                result = resolve_request(self.request(text, marker, library_roots=roots), semantic=False)
                self.assertEqual(result['engine'], 'include')
                self.assertEqual([item['path'] for item in result['destinations']], [str(header)])
                self.assertEqual(result['destinations'][0]['line'], 0)
                self.assertTrue(result['destinations'][0]['read_only'])
                self.assertFalse(result['requires_choice'])
        # The library's declarations resolve through the same explicit include.
        for marker, line in (('Servo servo', 2), ('attach(23)', 2)):
            with self.subTest(symbol=marker):
                result = resolve_request(self.request(text, marker, library_roots=roots), semantic=False)
                self.assertEqual([(item['path'], item['line']) for item in result['destinations']], [(str(header), line)])
        # A header nobody provides explains itself instead of returning an empty list silently.
        missing = resolve_request(self.request('#include <Nowhere.h>\n', 'Nowhere', library_roots=roots), semantic=False)
        self.assertEqual(missing['destinations'], [])
        self.assertIn('Nowhere.h', missing['error'])

    def test_identical_installed_copies_are_one_header_and_different_versions_are_a_choice(self):
        first, second, third = (self.root / name / 'src' for name in ('lib-a', 'lib-b', 'lib-c'))
        for folder, body in ((first, 'v1'), (second, 'v1'), (third, 'v2')):
            folder.mkdir(parents=True)
            (folder / 'Servo.h').write_text('// ' + body + '\n')
        text = '#include <Servo.h>\n'
        result = resolve_request(self.request(text, 'Servo.h', library_roots=[str(first), str(second)]), semantic=False)
        self.assertEqual([item['path'] for item in result['destinations']], [str(first / 'Servo.h')])
        result = resolve_request(self.request(text, 'Servo.h', library_roots=[str(first), str(second), str(third)]),
                                 semantic=False)
        self.assertEqual([item['path'] for item in result['destinations']], [str(first / 'Servo.h'), str(third / 'Servo.h')])
        self.assertTrue(result['requires_choice'])

    def test_the_recorded_build_context_outranks_installed_libraries(self):
        recorded, installed = self.root / 'recorded', self.root / 'installed'
        for folder in (recorded, installed):
            folder.mkdir()
            (folder / 'Servo.h').write_text('// ' + folder.name + '\n')
        workspace = self.root / 'workspace'
        context = workspace / '.pio/build/mcu_env/mcu-navigation.json'
        context.parent.mkdir(parents=True)
        context.write_text('{"schema": 1, "includes": [' + repr(str(recorded)).replace("'", '"') + '], "flags": []}')
        result = resolve_request(self.request('#include <Servo.h>\n', 'Servo.h', workspace=str(workspace),
                                              library_roots=[str(installed)]), semantic=False)
        self.assertEqual([item['path'] for item in result['destinations']], [str(recorded / 'Servo.h')])

    def test_installed_library_roots_list_library_sources_without_traversing_them(self):
        from main.qt.editor_panel import installed_library_roots
        project = self.root / 'Sketch'
        (project / 'lib/Local/src').mkdir(parents=True)
        (project / 'lib/Flat').mkdir(parents=True)
        (project / 'lib/.hidden').mkdir(parents=True)
        with patch('src.modules.package_jobs.package_core_directory', return_value=self.root / 'missing-core'), \
                patch('main.core.board_catalog._get_download_dir', return_value=str(self.root / 'missing-downloads')):
            roots = installed_library_roots(project)
        self.assertEqual(roots, [str(project / 'lib/Flat'), str(project / 'lib/Local/src')])

    def test_comments_and_strings_do_not_navigate(self):
        for text, marker in [('void fake() {}\n// fake();', 'fake();'),
                             ('void fake() {}\nconst char *s="fake";', 'fake"')]:
            self.assertEqual(resolve_request(self.request(text, marker), semantic=False)['destinations'], [])

    def test_conservative_scope_lookup_never_selects_an_unrelated_global_overload(self):
        text = 'void f(int) {}\nnamespace A { void f(int) {} }\nnamespace B { void f(int) {} }\nvoid run() { B::f(1); }'
        result = resolve_request(self.request(text, 'f(1)'), semantic=False)
        self.assertEqual([item['line'] for item in result['destinations']], [2])
        text = 'void f(int) {}\nstruct A { void f(int) {} };\nvoid run() { unknown.f(1); }'
        result = resolve_request(self.request(text, 'f(1)'), semantic=False)
        self.assertEqual([item['line'] for item in result['destinations']], [1])
        self.assertTrue(result['requires_choice'])

    @unittest.skipUnless(importlib.util.find_spec('clang'), 'LLVM wheel is unavailable in this verifier runtime')
    def test_real_selected_context_honors_defines_and_unsupported_targets_stay_conservative(self):
        import json
        text = '#ifdef SELECT_DOUBLE\nvoid f(double) {}\n#else\nvoid f(int) {}\n#endif\nvoid run() { f(1.0); }'
        request = self.request(text, 'f(1.0)')
        context = self.root / '.pio/build/mcu_env/mcu-navigation.json'
        context.parent.mkdir(parents=True)
        context.write_text(json.dumps({'schema':1, 'includes':[], 'flags':['-DSELECT_DOUBLE'], 'compiler':'c++'}))
        result = resolve_request(dict(request, workspace=str(self.root)))
        self.assertEqual(result['engine'], 'LLVM', result)
        self.assertEqual(result['destinations'][0]['line'], 1)
        context.write_text(json.dumps({'schema':1, 'includes':[], 'flags':[], 'compiler':'xtensa-unknown-elf-g++'}))
        result = resolve_request(dict(request, workspace=str(self.root)))
        self.assertEqual(result['engine'], 'declarations')
        self.assertEqual(len(result['destinations']), 2)

    def test_real_child_protocol_and_close_never_write_sources(self):
        text = 'void stepRun() {}\nvoid stepRun(int value) {}\nvoid run() { stepRun(7); }'
        request = self.request(text, 'stepRun(7)')
        resolver = NavigationResolver()
        self.addCleanup(resolver.close)
        result = resolver.resolve(request)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['destinations'][0]['line'], 1)
        self.assertEqual((self.root / 'sketch.cpp').read_text(), text)
        resolver.close()
        self.assertTrue(resolver.resolve(request)['cancelled'])

    def test_parser_deadline_retires_the_owned_process_including_windows_venv_children(self):
        import json
        import psutil
        from src.modules import source_navigation as navigation
        record=self.root/'parser-pid.json'
        worker=self.root/'slow-parser.py'
        worker.write_text('import json,os,time\nfrom pathlib import Path\n'
            f'Path({str(record)!r}).write_text(json.dumps({{"pid":os.getpid()}}))\n'
            'time.sleep(60)\n',encoding='utf-8')
        resolver=NavigationResolver(timeout=2)
        self.addCleanup(resolver.close)
        started=time.monotonic()
        with patch.object(navigation,'__file__',str(worker)):
            result=resolver.resolve({})
        self.assertFalse(result['success'])
        self.assertIn('time limit',result['error'])
        self.assertLess(time.monotonic()-started,8)
        self.assertTrue(record.is_file(),'The owned parser did not reach its controlled slow phase')
        pid=json.loads(record.read_text())['pid']
        deadline=time.monotonic()+2
        while psutil.pid_exists(pid) and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertFalse(psutil.pid_exists(pid),'An owned parser process survived its deadline')

    @unittest.skipUnless(importlib.util.find_spec('clang'), 'LLVM wheel is unavailable in this verifier runtime')
    def test_real_llvm_resolves_types_namespaces_members_and_templates(self):
        cases = [
            ('void f(int) {}\nvoid f(double) {}\nvoid run() { f(1.0); }', 'f(1.0)', 1),
            ('namespace A { void f(int) {} }\nnamespace B { void f(int) {} }\nvoid run() { B::f(1); }', 'f(1)', 1),
            ('struct A { void f() {}\nvoid f(int) {} };\nvoid run() { A value; value.f(1); }', 'f(1)', 1),
            ('template<class T> void f(T) {}\nvoid f(int) {}\nvoid run() { f(1.0); }', 'f(1.0)', 0),
        ]
        for text, marker, line in cases:
            with self.subTest(marker=marker, source=text):
                result = NavigationResolver().resolve(self.request(text, marker))
                self.assertTrue(result['success'], result)
                self.assertEqual(result['engine'], 'LLVM', result)
                self.assertEqual(result['destinations'][0]['line'], line)

    @unittest.skipUnless(importlib.util.find_spec('clang'), 'LLVM wheel is unavailable in this verifier runtime')
    def test_real_llvm_reads_unsaved_header_bytes_and_keeps_unicode_columns(self):
        header = self.root / 'local.h'
        header.write_text('void stepRun(int);', encoding='utf-8')
        unsaved = '// µ prefix\nvoid stepRun(int value) {}\nvoid stepRun(double value) {}'
        text = '#include "local.h"\nvoid run() { /* λ */ stepRun(1.0); }'
        result = NavigationResolver().resolve(self.request(text, 'stepRun(1.0)', files=[(str(header), unsaved)]))
        self.assertEqual(result['engine'], 'LLVM', result)
        self.assertEqual(result['destinations'][0]['line'], 2)
        self.assertEqual(header.read_text(), 'void stepRun(int);')


if __name__ == '__main__':
    unittest.main(verbosity=2)
