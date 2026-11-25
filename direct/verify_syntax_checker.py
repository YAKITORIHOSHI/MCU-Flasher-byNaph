#!/usr/bin/env python3
"""Memory and scratch-file regressions for the offline C/C++ syntax checker.

No Qt workspace, compiler, board metadata, live configuration or package discovery
is started. Disk fixtures live beneath temp/ and disappear after each check.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from src import syntax_checker


class SyntaxCheckerChecks(unittest.TestCase):
    def setUp(self):
        syntax_checker.clear_syntax_cache()
        self.addCleanup(syntax_checker.clear_syntax_cache)

    def analyze(self, code: str, name: str = "fixture.cpp"):
        return syntax_checker.analyze_cpp_syntax(code, name)

    def assertClean(self, code: str, name: str = "fixture.cpp"):
        self.assertEqual(self.analyze(code, name), [], code)

    def assertIssue(self, code: str, fragment: str, name: str = "fixture.cpp"):
        issues = self.analyze(code, name)
        matching = [issue for issue in issues
                    if fragment.lower() in str(issue.get("message", "")).lower()]
        self.assertTrue(matching, (code, issues, fragment))
        return matching

    def scratch_source(self, name="fixture.c"):
        scratch = ROOT / "temp" / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="syntax-checker-", dir=scratch)
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / name

    def test_valid_c11_source(self):
        self.assertClean(
            "#include <stddef.h>\n"
            "typedef struct { int value; } State;\n"
            "enum Mode { MODE_READY, MODE_RUNNING };\n"
            "_Static_assert(sizeof(int) >= 2, \"int required\");\n"
            "_Alignas(8) static unsigned char buffer[16];\n"
            "static State state = {.value = 3};\n"
            "int read_state(void) { return state.value; }\n"
            "int select_value(int x) { return _Generic(x, int: 1, default: 0); }\n"
            "State make_state(void) { return (State){.value = 4}; }\n", "fixture.c")

    def test_valid_c_control_labels_and_do_while(self):
        self.assertClean(
            "int tick(int value) {\n"
            "  do {\n"
            "    value--;\n"
            "  } while (value > 5);\n"
            "  switch (value) {\n"
            "    case 2: break;\n"
            "    default: goto done;\n"
            "  }\n"
            "done:\n"
            "  return value;\n"
            "}\n", "fixture.c")

    def test_cpp_digit_separators_are_not_char_literals(self):
        self.assertClean("unsigned long count = 1'000'000UL;\n"
                         "int bits = 0b1010'0011;\n"
                         "unsigned hex = 0xAB'CD;\n"
                         "double time = 1'234.5'6e+1;\n")

    def test_multiline_strings_comments_and_raw_literals(self):
        self.assertClean(
            "const char* url = \"http://example.test/{[]}\";\n"
            r'const char* escaped = "quote: \" and \\";' "\n"
            "const char* joined =\n"
            "  \"first\"\n"
            "  \" second\";\n"
            "const char* raw = R\"tag({ [ (\n"
            "// not a comment; } ] )\n"
            ")tag\";\n"
            "/* multiline {\n"
            "   ] } ) */\n"
            "void setup() {}\n")

    def test_unterminated_raw_literal_reports_its_opening(self):
        issues = self.assertIssue("\nconst char* text = R\"tag(unfinished\n", "raw")
        self.assertEqual(issues[0]["line"], 2)
        self.assertEqual(issues[0]["col"], 20)

    def test_unterminated_string_char_and_comment(self):
        for code, message in (
            ('const char* text = "unfinished\n', "Unclosed string"),
            ("char letter = 'x;\n", "Unclosed character"),
            ("/* unfinished\nvoid setup() {}\n", "Unclosed block comment"),
        ):
            with self.subTest(message=message):
                self.assertIssue(code, message)

    def test_literal_ended_assignment_needs_semicolon(self):
        self.assertIssue('const char* text = "ready"\n', "semicolon")
        self.assertIssue("char letter = 'A'\n", "semicolon")
        self.assertIssue('const char* text = R"tag(ready)tag"\n', "semicolon")

    def test_type_definition_needs_semicolon_at_eof(self):
        for keyword in ("struct", "class", "union", "enum"):
            with self.subTest(keyword=keyword):
                member = "READY" if keyword == "enum" else "int value;"
                self.assertIssue(f"{keyword} State {{ {member} }}\n", "semicolon")

    def test_macro_replacement_tokens_do_not_unbalance_source(self):
        self.assertClean("#define OPEN {\n"
                         "#define CLOSE }\n"
                         "#define LEFT (\n"
                         "#define ARRAY [\n"
                         "#define CHECK(v) do { \\\n"
                         "  if ((v) > 0) work(); \\\n"
                         "} while (0)\n"
                         "void work() {}\n")

    def test_known_inactive_branch_does_not_create_errors(self):
        self.assertClean("#if 0\n"
                         "void impossible( { \n"
                         "#if 1\n"
                         "unterminated [\n"
                         "#endif\n"
                         "#else\n"
                         "void setup() {}\n"
                         "#endif\n")

    def test_valid_preprocessor_header_macros_and_comments(self):
        self.assertClean("#define HEADER_FILE <stddef.h>\n"
                         "#include HEADER_FILE\n"
                         "#include <stdint.h> /* integer types */\n"
                         "#include \"local.h\" // local declarations\n"
                         "int value = 2;\n", "fixture.c")

    def test_include_line_splicing_and_leading_comments(self):
        self.assertClean("#include \\\n"
                         "  <stddef.h>\n"
                         "#include /* standard integers */ <stdint.h>\n"
                         "int value = 3;\n", "fixture.c")

    def test_lambda_condition_is_not_a_missing_parenthesis(self):
        self.assertClean("void tick() {\n"
                         "  if ([] { return true; }()) {\n"
                         "    tick();\n"
                         "  }\n"
                         "}\n")

    def test_cpp_adjacent_literals_after_declaration(self):
        self.assertClean('const char* label = "first"\n'
                         '  " second"\n'
                         '  " third";\n')

    def test_multiline_c_and_cpp_initializers(self):
        for name in ("fixture.c", "fixture.cpp"):
            with self.subTest(language=name):
                self.assertClean("int values[] = {\n  1,\n  2\n};\n"
                                 "struct State { int value; };\n"
                                 "struct State state = {\n  3\n};\n", name)

    def test_extern_c_and_returning_struct_type(self):
        self.assertClean('extern "C" {\n'
                         '  int external_call(void);\n'
                         '}\n'
                         'struct State { int value; };\n'
                         'struct State make_state(void) {\n'
                         '  return (struct State){.value = 3};\n'
                         '}\n')

    def test_comment_between_type_closing_brace_and_semicolon(self):
        self.assertClean("struct State {\n"
                         "  int value;\n"
                         "} /* closing type */\n"
                         "// a comment before the required punctuation\n"
                         ";\n")

    def test_increment_and_inline_control_statement_need_semicolon(self):
        for statement in ("value++", "--value", "if (ready) value = 2",
                          "while (ready) tick()"):
            with self.subTest(statement=statement):
                self.assertIssue(f"void tick() {{\n  {statement}\n}}\n", "semicolon")

    def test_return_initializer_needs_semicolon(self):
        self.assertIssue("struct State { int value; };\n"
                         "State read() {\n  return State{1}\n}\n", "semicolon")

    def test_malformed_include_is_reported(self):
        for directive in ("#include\n", "#include <stdint.h\n", '#include "local.h\n',
                          "#include <>\n"):
            with self.subTest(directive=directive):
                self.assertTrue(self.analyze(directive, "fixture.c"))

    def test_missing_colons_and_do_while_semicolon(self):
        for source in ("switch (x) {\ncase 1\n break;\n}\n",
                       "switch (x) {\ndefault\n break;\n}\n",
                       "class State {\npublic\n int value;\n};\n"):
            with self.subTest(source=source):
                self.assertIssue(source, "colon")
        self.assertIssue("void work() {\n do {\n  tick();\n } while (ready())\n}\n",
                         "semicolon")

    def test_unicode_positions_and_bracket_ranges_are_source_columns(self):
        source = 'void work() {\n  const char* s = "😀"; ]\n}\n'
        issues = self.assertIssue(source, "bracket")
        target = next(issue for issue in issues if issue["line"] == 2)
        self.assertEqual(target["col"], source.splitlines()[1].index("]") + 1)
        for issue in self.analyze(source):
            line_text = source.splitlines()[issue["line"] - 1]
            self.assertGreaterEqual(issue["col"], 1)
            self.assertLessEqual(issue["col"], len(line_text) + 1)
            self.assertLessEqual(issue["endCol"], len(line_text) + 1)

    def test_code_cache_returns_independent_diagnostics(self):
        source = "void work() {\n"
        first = self.analyze(source)
        first[0]["message"] = "Caller changed this"
        first.append({"message": "Injected by caller"})
        second = self.analyze(source)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0]["message"], "Caller changed this")

    def test_file_cache_checks_actual_bytes_when_stat_signature_matches(self):
        source = self.scratch_source()
        source.write_bytes(b"int value = 3;\n")
        before = source.stat()
        self.assertEqual(syntax_checker.analyze_file_syntax(source), [])
        source.write_bytes(b"int value = 3 \n")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(source.stat().st_size, before.st_size)
        issues = syntax_checker.analyze_file_syntax(source)
        self.assertTrue(issues)
        self.assertIn("semicolon", issues[0]["message"])

    def test_read_failure_cannot_report_clean_source(self):
        source = self.scratch_source()
        source.write_text("int value = 3;\n", encoding="utf-8")
        with patch.object(Path, "open", side_effect=OSError("Fixture cannot read")):
            try:
                issues = syntax_checker.analyze_file_syntax(source)
            except OSError:
                return  # A propagated failure is also honest to the caller.
        self.assertTrue(issues, "An unreadable file must not be reported as clean")

    def test_serial_batch_includes_all_root_file_failures(self):
        source = self.scratch_source()
        source.write_text("void first() {\n", encoding="utf-8")
        other = source.with_name("other.cpp")
        other.write_text("void second() {\n", encoding="utf-8")
        issues = syntax_checker.analyze_files_parallel([other, source], max_workers=1)
        self.assertEqual({Path(issue["file"]).name for issue in issues},
                         {source.name, other.name})

    def test_cache_stays_bounded(self):
        for index in range(100):
            self.analyze(f"void work_{index}() {{\n")
        self.assertLessEqual(len(syntax_checker._SYNTAX_CODE_CACHE),
                             syntax_checker._MAX_CACHE_ENTRIES)
        self.assertLessEqual(sum(map(len, syntax_checker._SYNTAX_CODE_CACHE.values())),
                             syntax_checker._MAX_CACHE_DIAGNOSTICS)

    def test_diagnostic_storm_is_bounded_during_collection(self):
        # A malformed generated file must not allocate one diagnostic dictionary
        # for every character before trimming the published result.
        source = "]" * 60_000
        tracemalloc.start()
        try:
            with patch.object(syntax_checker, "_MAX_CACHE_DIAGNOSTICS", 16):
                issues = self.analyze(source)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLessEqual(len(issues), 16)
        self.assertLess(peak, 8 * 1024 * 1024,
                        "Diagnostics were allocated without a collection bound")

    def test_representative_arduino_controller_and_interrupt_handler(self):
        self.assertClean('#include <Arduino.h>\n'
                         'class Controller {\n'
                         'public:\n'
                         '  explicit Controller(int pin) : pin_(pin) {}\n'
                         '  void start() { pinMode(pin_, OUTPUT); }\n'
                         'private:\n'
                         '  int pin_;\n'
                         '};\n'
                         'struct PinDefinition { int pin; const char* name; };\n'
                         'static const PinDefinition pins[] = {\n'
                         '  { 2, "Status" },\n'
                         '  { 4, "Relay" }\n'
                         '};\n'
                         'Controller controller(2);\n'
                         'void IRAM_ATTR onTick()\n'
                         '{\n'
                         '  digitalWrite(2, HIGH);\n'
                         '}\n'
                         'void setup() { controller.start(); }\n'
                         'void loop() { delay(10); }\n', 'fixture.ino')

    def test_cpp_templates_namespace_and_lambda(self):
        self.assertClean('namespace detail {\n'
                         'template <typename T>\n'
                         'T clamp(T value, T low, T high) {\n'
                         '  return value < low ? low : value > high ? high : value;\n'
                         '}\n'
                         '}\n'
                         'void work() {\n'
                         '  auto callback = [](int value) {\n'
                         '    return value + 1;\n'
                         '  };\n'
                         '  callback(1);\n'
                         '}\n')

    def test_valid_library_shapes_stay_clean(self):
        for source in (
            # Return types, storage and attributes on a line of their own.
            "static esp_err_t\nread_value(int pin) {\n  return pin;\n}\n",
            "void IRAM_ATTR\nonTick() {}\nstatic const\nint limit = 3;\n",
            "void send(const void* data)\n  ATTR_WARN_UNUSED_RESULT ATTR_NON_NULL_PTR_ARG(1);\n",
            "typedef typename Buffer::const_type\n  buffer_type;\n",
            # Macros that already end a statement, or take one as an argument.
            "IMPLEMENT_ISR(INT0_vect, EXTERNAL_INT_0)\nIMPLEMENT_ISR(INT1_vect, EXTERNAL_INT_1)\n",
            "void work() {\n  UNITY_BEGIN()\n  RUN_TEST(test_pass)\n}\n",
            'int check(int* value) {\n  LWIP_ERROR("value", (value != 0), return 0;);\n  return 1;\n}\n',
            'const char* version = "v" VERSION_TEXT(MAJOR) "."\n  VERSION_TEXT(MINOR);\n',
            # Conditional code whose alternatives each balance.
            '#ifdef __cplusplus\nextern "C" {\n#endif\nint call(void);\n#ifdef __cplusplus\n}\n#endif\n',
            "void work(int a, int b) {\n#if FAST\n  if (a) {\n#else\n  if (b) {\n#endif\n    run();\n  }\n}\n",
            "#define PIN 4 /* the pin\n  that isn't used */\nint pin = PIN;\n",
            # Blocks after labels, functions returning named types, casts and templates.
            "int pick(int value) {\n  switch (value) {\n    case Kind::First: {\n      return 1;\n    }\n"
            "    default:\n    {\n      break;\n    }\n  }\n  return 0;\n}\n",
            "enum Mode mode_of(int value) {\n  return (enum Mode)value;\n}\nstruct State* state(void) {\n  return 0;\n}\n",
            "int wide(long value) {\n  return (int)\n    value;\n}\n",
            "void work() {\n  do {\n    step();\n  } while (busy());\n  while (count--)\n    step();\n}\n",
            "Foo::Foo(int a)\n  : a_(a),\n    b_{1}\n{\n  run();\n}\n",
            "void work() {\n  list.forEach([](int value) {\n    use(value);\n  });\n}\n",
        ):
            with self.subTest(source=source):
                self.assertClean(source)

    def test_unfinished_statements_are_errors_just_after_their_last_token(self):
        for source, line, text in (
            ("int led = 4\nvoid setup() {}\n", 1, "int led = 4"),
            ("const char* name = NAME\nconst char* other = OTHER;\n", 1, "const char* name = NAME"),
            ("void setup() {\n  Serial.begin(115200)\n  pinMode(13, OUTPUT);\n}\n", 2, "  Serial.begin(115200)"),
            ("void setup() {\n  int led = HIGH // note\n  run();\n}\n", 2, "  int led = HIGH"),
            ("int read() {\n  return\n}\n", 2, "  return"),
            ("void work() {\n  while (busy()) {\n    break\n  }\n}\n", 3, "    break"),
            ('void work() {\n  char ok[3] = "OK"\n  EEPROM.put(0, ok);\n}\n', 2, '  char ok[3] = "OK"'),
            ("void work() {\n  Serial.println(value)\n}\n", 2, "  Serial.println(value)"),
            ("using namespace std\nint value;\n", 1, "using namespace std"),
            ("#include <Arduino.h>\n#define A 1\nServo servo = Servo()\n\nvoid setup() {}\n", 3, "Servo servo = Servo()"),
        ):
            with self.subTest(source=source):
                issues = self.analyze(source)
                self.assertEqual([(issue["line"], issue["severity"]) for issue in issues], [(line, "error")], issues)
                self.assertIn("semicolon", issues[0]["message"])
                self.assertEqual(issues[0]["col"], len(text) + 1)

    def test_code_in_the_other_branch_is_checked_too(self):
        source = ("#if USB_MODE\n#warning not here\n#else\nvoid setup() {\n  Serial.begin(9600)\n  run();\n"
                  "}\nvoid loop() {\n  if (ready() {\n    run();\n  }\n}\n#endif\n")
        messages = [issue["message"] for issue in self.analyze(source)]
        self.assertTrue(any("semicolon" in message for message in messages), messages)
        self.assertTrue(any("missing" in message.lower() and "')'" in message for message in messages), messages)
        self.assertIssue("#ifdef WIFI\nvoid join() {\n  if (ready()) {\n    run();\n}\n#endif\nvoid setup() {}\n", "never closed")

    def test_an_unclosed_call_is_one_error_on_its_own_line(self):
        for broken in ('  Serial.println("ready";', "  digitalWrite(13, HIGH", "  values[3 = 4;"):
            with self.subTest(broken=broken):
                issues = self.analyze(f"void setup() {{\n  begin();\n{broken}\n  run();\n}}\n\nvoid loop() {{\n  run();\n}}\n")
                self.assertEqual({issue["line"] for issue in issues if issue["severity"] == "error"}, {3}, issues)
                self.assertIn("never closed", issues[0]["message"])
        self.assertClean("void loop() {\n  for (int i = 0; i < 4; i++) run(i);\n  if (int a = read(); a > 2) run(a);\n}\n")

    def test_excessive_bracket_nesting_reports_live_check_limit(self):
        issues = self.analyze("(" * 10_000)
        self.assertTrue(any("nest" in issue["message"].lower()
                            or "complex" in issue["message"].lower()
                            or "limit" in issue["message"].lower() for issue in issues), issues)

    def test_preprocessor_nesting_reports_limit_without_fake_unclosed_errors(self):
        with patch.object(syntax_checker, "_MAX_BRACKET_DEPTH", 3):
            issues = self.analyze("#if 1\n" * 4 + "#endif\n" * 4)
        self.assertTrue(any("Preprocessor nesting" in issue["message"] for issue in issues), issues)
        self.assertFalse(any("missing #endif" in issue["message"] for issue in issues), issues)
        self.assertTrue(any("Compile" in issue["message"] for issue in issues))

    def test_structural_token_complexity_reports_live_check_limit(self):
        with patch.object(syntax_checker, "_MAX_STRUCTURAL_TOKENS", 4):
            issues = self.analyze("() () ()\n")
        self.assertTrue(any("complexity" in issue["message"].lower() for issue in issues), issues)
        self.assertTrue(any("Compile" in issue["message"] for issue in issues))

    def test_source_length_limit_runs_before_hashing_or_line_copies(self):
        class LargeSource(str):
            def encode(self, *args, **kwargs):
                raise AssertionError("Oversized source was encoded before rejection")
            def splitlines(self, *args, **kwargs):
                raise AssertionError("Oversized source was split before rejection")
        with patch.object(syntax_checker, "_MAX_SOURCE_CHARACTERS", 8):
            issues = self.analyze(LargeSource("void setup() {}\n"))
        self.assertEqual(len(issues), 1)
        self.assertIn("size limit", issues[0]["message"])
        self.assertIn("Compile", issues[0]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
