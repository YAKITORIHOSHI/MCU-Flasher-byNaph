from __future__ import annotations

import os
import hashlib
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

try:
    from .syntax_statements import Structure
except ImportError:  # The standalone viewer imports this module by itself.
    from syntax_statements import Structure

# ── CPU & Memory Optimization Engine ──────────────────────────────────────────
# Regex parsing is CPU bound under the GIL. Keep background workers small so
# compilation, serial I/O and the UI retain CPU time on four/six-core systems.
def get_optimal_worker_count() -> int:
    """Use one background parser on constrained hardware, two otherwise."""
    from src.modules.runtime_resources import performance_profile
    return 1 if performance_profile().constrained else 2

_SYNTAX_EXECUTOR = None
_SYNTAX_EXECUTOR_LOCK = threading.Lock()

def get_syntax_executor() -> ThreadPoolExecutor:
    """Shared, reusable multi-core ThreadPoolExecutor for syntax parsing."""
    global _SYNTAX_EXECUTOR
    with _SYNTAX_EXECUTOR_LOCK:
        if _SYNTAX_EXECUTOR is None:
            _SYNTAX_EXECUTOR = ThreadPoolExecutor(
                max_workers=get_optimal_worker_count(),
                thread_name_prefix="SyntaxWorkerPool"
            )
        return _SYNTAX_EXECUTOR

# Content-bound RAM caches; callers always receive their own diagnostic copies.
_CACHE_LOCK = threading.Lock()
_SYNTAX_CODE_CACHE: dict[tuple, list[dict]] = {}
_SYNTAX_FILE_CACHE: dict[tuple, list[dict]] = {}
_MAX_CACHE_ENTRIES = 64
_MAX_CACHE_DIAGNOSTICS = 8192
_MAX_SOURCE_CHARACTERS = 4 * 1024 * 1024
_MAX_BRACKET_DEPTH = 1024
_MAX_STRUCTURAL_TOKENS = 262144


class _Diagnostics(list):
    """Bound diagnostics while scanning, including severely malformed sources."""
    def __init__(self):
        super().__init__()
        self.omitted = 0

    def append(self, diagnostic):
        if len(self) < max(1, _MAX_CACHE_DIAGNOSTICS - 1):
            super().append(diagnostic)
        else:
            self.omitted += 1


def _directive_comment_start(line: str, already_open=False) -> int | None:
    """Find an unfinished block comment outside a directive's quoted literals."""
    i, quote, comment = 0, '', 0 if already_open else None
    while i < len(line):
        if comment is not None:
            end = line.find('*/', i)
            if end < 0:
                return comment
            comment, i = None, end + 2
            continue
        char = line[i]
        if quote:
            if char == '\\':
                i += 2
                continue
            if char == quote:
                quote = ''
        elif char in ('"', "'"):
            quote = char
        elif line.startswith('//', i):
            break
        elif line.startswith('/*', i):
            comment = i
            i += 2
            continue
        i += 1
    return comment


def _remember_diagnostics(cache, key, errors):
    """Bound both revision count and retained diagnostic objects under the lock."""
    if len(errors) > _MAX_CACHE_DIAGNOSTICS:
        return
    cache.pop(key, None)
    cache[key] = [dict(error) for error in errors]
    total = sum(len(value) for value in cache.values())
    while len(cache) > _MAX_CACHE_ENTRIES or total > _MAX_CACHE_DIAGNOSTICS:
        old = cache.pop(next(iter(cache)))
        total -= len(old)

def clear_syntax_cache():
    """Clear all in-memory syntax check caches."""
    with _CACHE_LOCK:
        _SYNTAX_CODE_CACHE.clear()
        _SYNTAX_FILE_CACHE.clear()

def get_syntax_cache_stats() -> dict[str, int]:
    """Return in-memory cache utilization stats."""
    with _CACHE_LOCK:
        return {
            "code_cache_entries": len(_SYNTAX_CODE_CACHE),
            "file_cache_entries": len(_SYNTAX_FILE_CACHE),
            "max_entries": _MAX_CACHE_ENTRIES,
            "optimal_workers": get_optimal_worker_count(),
        }

# Built-in Arduino and C++ functions/macros/types that are globally available
STANDARD_FUNCTIONS = {
    # I/O
    "pinMode", "digitalWrite", "digitalRead", "analogReference", "analogRead",
    "analogWrite", "analogReadResolution", "analogWriteResolution",
    # Advanced I/O
    "tone", "noTone", "shiftOut", "shiftIn", "pulseIn", "pulseInLong",
    # Time
    "millis", "micros", "delay", "delayMicroseconds", "yield",
    # Math / Trigonometry
    "min", "max", "abs", "constrain", "map", "pow", "sqrt", "sin", "cos",
    "tan", "isinf", "isnan", "floor", "ceil", "round",
    # Characters
    "isAlphaNumeric", "isAlpha", "isAscii", "isWhitespace", "isControl",
    "isDigit", "isGraph", "isLowerCase", "isPrint", "isPunct", "isSpace",
    "isUpperCase", "isHexadecimalDigit",
    # Random Numbers
    "randomSeed", "random",
    # Bits and Bytes
    "lowByte", "highByte", "bitRead", "bitWrite", "bitSet", "bitClear", "bit",
    # External Interrupts
    "attachInterrupt", "detachInterrupt", "interrupts", "noInterrupts",
    # ESP Specific / Common
    "analogWriteFreq", "analogWriteRange", "esp_deep_sleep_start",
    # FreeRTOS & ESP-IDF Core
    "xTaskCreate", "xTaskCreatePinnedToCore", "vTaskDelete", "vTaskDelay", "vTaskDelayUntil",
    "xQueueCreate", "xQueueSend", "xQueueReceive", "xQueueSendFromISR", "xQueueReceiveFromISR",
    "xSemaphoreCreateBinary", "xSemaphoreCreateCounting", "xSemaphoreCreateMutex",
    "xSemaphoreTake", "xSemaphoreGive", "xSemaphoreTakeFromISR", "xSemaphoreGiveFromISR",
    "portTICK_PERIOD_MS", "taskYIELD", "esp_restart", "esp_random", "esp_timer_get_time",
    "esp_err_to_name", "nvs_flash_init", "nvs_flash_erase",
    # Types / Casts
    "char", "byte", "int", "long", "float", "double", "word", "short",
    "String", "IPAddress", "boolean",
    # standard C/C++ globals
    "printf", "sprintf", "snprintf", "strlen", "strcmp", "strcpy", "strncpy",
    "memcpy", "memset", "memcmp", "malloc", "calloc", "realloc", "free",
    "atoi", "atol", "atof", "strtol", "strtod", "abs", "labs", "assert",
    # Macros / Special
    "F", "setup", "loop"
}

# C++ control keywords that are followed by a parenthesis but are not function calls
CPP_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "sizeof", "return", "decltype",
    "alignas", "alignof", "noexcept", "static_assert", "thread_local", "operator",
    "throw", "new", "delete"
}

# Matches "#include" (allowing whitespace after '#') and captures everything after it
_INCLUDE_DIRECTIVE_RE = re.compile(r'^\s*#\s*include\s*(.*)$')


def _strip_trailing_line_comment(text: str) -> str:
    """Removes a trailing '// ...' comment that isn't inside a <...> or "..." span."""
    in_angle = False
    in_quote = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_quote:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_quote = False
            i += 1
            continue
        if in_angle:
            if c == '>':
                in_angle = False
            i += 1
            continue
        if c == '"':
            in_quote = True
            i += 1
            continue
        if c == '<':
            in_angle = True
            i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            return text[:i]
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            if end == -1:
                return text[:i]
            text = text[:i] + ' ' * (end + 2 - i) + text[end + 2:]
            i = end + 2
            continue
        i += 1
    return text


def check_include_directive(line: str, line_no: int, file_path: Path | str):
    """
    Validates a single '#include' line for a properly closed <...> or "..." target.
    Returns an error dict with exact line and column ranges if malformed, otherwise None.
    """
    if not isinstance(file_path, Path):
        file_path = Path(file_path)
    match = _INCLUDE_DIRECTIVE_RE.match(line)
    if not match:
        return None

    remainder = _strip_trailing_line_comment(match.group(1)).strip()

    if not remainder:
        return {
            "file": file_path.name,
            "line": line_no,
            "col": len(line) + 1,
            "endLine": line_no,
            "endCol": len(line) + 2,
            "message": "Malformed #include directive: missing header name",
            "severity": "error"
        }

    if remainder.startswith('<'):
        close_idx = remainder.find('>')
        if close_idx == -1:
            return {
                "file": file_path.name,
                "line": line_no,
                "col": len(line) + 1,
                "endLine": line_no,
                "endCol": len(line) + 2,
                "message": f"#include directive is missing a closing '>': {remainder}",
                "severity": "error"
            }
        header_name = remainder[1:close_idx]
        if not header_name.strip():
            return {
                "file": file_path.name,
                "line": line_no,
                "col": match.start(1) + 2,
                "endLine": line_no,
                "endCol": match.start(1) + 3,
                "message": "Malformed #include directive: empty header name between '<' and '>'",
                "severity": "error"
            }
        trailing = remainder[close_idx + 1:].strip()
        if trailing:
            return {
                "file": file_path.name,
                "line": line_no,
                "col": match.start(1) + close_idx + 2,
                "endLine": line_no,
                "endCol": len(line) + 1,
                "message": f"Unexpected characters after #include <{header_name}>: '{trailing}'",
                "severity": "warning"
            }
        return None

    if remainder.startswith('"'):
        close_idx = remainder.find('"', 1)
        if close_idx == -1:
            return {
                "file": file_path.name,
                "line": line_no,
                "col": len(line) + 1,
                "endLine": line_no,
                "endCol": len(line) + 2,
                "message": f'#include directive is missing a closing \'"\': {remainder}',
                "severity": "error"
            }
        header_name = remainder[1:close_idx]
        if not header_name.strip():
            return {
                "file": file_path.name,
                "line": line_no,
                "col": match.start(1) + 2,
                "endLine": line_no,
                "endCol": match.start(1) + 3,
                "message": "Malformed #include directive: empty header name between quotes",
                "severity": "error"
            }
        trailing = remainder[close_idx + 1:].strip()
        if trailing:
            return {
                "file": file_path.name,
                "line": line_no,
                "col": match.start(1) + close_idx + 2,
                "endLine": line_no,
                "endCol": len(line) + 1,
                "message": f'Unexpected characters after #include "{header_name}": \'{trailing}\'',
                "severity": "warning"
            }
        return None

    # Macro-expanded include targets are resolved by the actual preprocessor.
    if re.fullmatch(r'[A-Za-z_]\w*(?:\s*\(.*\))?', remainder):
        return None

    return {
        "file": file_path.name,
        "line": line_no,
        "col": match.start(1) + 1,
        "endLine": line_no,
        "endCol": len(line) + 1,
        "message": f"Malformed #include directive: expected <FileName.h> or \"FileName.h\", got '{remainder}'",
        "severity": "error"
    }


# Readings of conditionals this checker cannot evaluate: the first branch, then every
# macro defined, then none of them.
_READINGS = ("first", "defined", "undefined")
_SCAN = re.compile(r"""//|/\*|["']""")
_STRING_REST = re.compile(r'(?:[^"\\]|\\.)*"')
_CHAR_REST = re.compile(r"(?:[^'\\]|\\.)*'")
_RAW_PREFIX = re.compile(r'(?:^|[^\w])(?:u8|u|U|L)?R$')
_NUMBER_TAIL = re.compile(r"(?:^|[^\w.])(?:0[xX][0-9a-fA-F']+|0[bB][01']+|\.?[0-9][0-9'eEpP.+-]*)$")
_DIRECTIVE = re.compile(r'\s*#\s*(\w+)\b(.*)')
_NEXT_CODE = re.compile(r'\s*(\S)')
_TYPE_START = re.compile(r'(?:void|int|long|short|unsigned|signed|char|float|double|bool|struct|class|enum|union)\b')
_CASE = re.compile(r'\s*case\b')
_DEFAULT = re.compile(r'\s*default\s*$')
_ACCESS = re.compile(r'\s*(public|private|protected)\s*$')
_LABEL_LINE = re.compile(r'\s*(?:case|default|public|private|protected)\b')


def _condition_value(expression, reading, defined):
    """Evaluate a plain constant or a single defined() test; anything else follows the reading."""
    expression = _strip_trailing_line_comment(expression).strip()
    while expression.startswith('(') and expression.endswith(')') and expression.count('(') == 1:
        expression = expression[1:-1].strip()
    match = re.fullmatch(r'(!?)\s*(0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]*', expression)
    if match:
        value = int(match[2], 16 if match[2].lower().startswith('0x') else 10) != 0
        return (not value if match[1] else value), True
    match = re.fullmatch(r'(!?)\s*defined\s*(?:\(\s*(\w+)\s*\)|(\w+))', expression)
    if match and reading:
        known = (match[2] or match[3]) in defined or _READINGS[reading] == "defined"
        return (not known if match[1] else known), False
    # Target macros are unavailable to this lightweight checker. Inspect one branch
    # rather than combining mutually exclusive brace structures.
    return _READINGS[reading] != "undefined", False


class _Scanner:
    """One pass over a source: comments and literals blanked, columns unchanged."""

    def __init__(self, lines, file_path, language, reading=0):
        self.cleaned = []
        self.directives = set()
        self.findings = []          # (line, column, message, severity, width)
        self.literals = []          # The same, for literals a conditional could explain
        self.conditional = False    # Some branch could not be evaluated
        self.skipped = 0            # Code lines outside the branches this reading uses
        self.active_lines = 0
        self.limited = False
        self._scan(lines, file_path, language, reading)
        self.active_lines = sum(1 for line in self.cleaned if not line.isspace() and line)

    def _find(self, line_no, column, message, severity="error", width=1):
        if len(self.findings) < _MAX_CACHE_DIAGNOSTICS:
            self.findings.append((line_no, column, message, severity, width))

    def _literal(self, line_no, column, message, width=1):
        if len(self.literals) < _MAX_CACHE_DIAGNOSTICS:
            self.literals.append((line_no, column, message, "error", width))

    def _scan(self, lines, file_path, language, reading):
        stack, active, defined = [], True, set()
        state, opened, raw_end = "", (0, 0), ""     # state: "", comment, raw, string or char
        continued = False                           # A directive's backslash continuation
        cleaned = self.cleaned
        for index, line in enumerate(lines):
            line_no = index + 1
            if continued or not state and "#" in line:
                directive = None if continued else _DIRECTIVE.match(line)
                if continued or directive:
                    self.directives.add(index)
                    if directive and not self._directive(directive, line, line_no, file_path, stack, reading, defined, active):
                        cleaned.append(' ' * len(line))
                        break
                    if directive and directive[1] in ('if', 'ifdef', 'ifndef', 'elif', 'else', 'endif'):
                        active = stack[-1][0] and stack[-1][1] if stack else True
                    cleaned.append(' ' * len(line))
                    was_comment = continued and state == "comment"
                    comment = (_directive_comment_start(line, was_comment)
                               if was_comment or "/*" in line else None)
                    if comment is not None:
                        if not was_comment:
                            opened = (line_no, comment + 1)
                        state = "comment"
                    elif was_comment:
                        state = ""
                    # A comment, unlike a backslash, carries onto the next line by itself.
                    continued = line.rstrip().endswith('\\')
                    continue
            if not active and state != "comment":
                cleaned.append(' ' * len(line))
                self.skipped += bool(line.strip())
                if "/*" in line and _directive_comment_start(line) is not None:
                    state, opened = "comment", (line_no, line.rfind("/*") + 1)
                continue
            if not state and '"' not in line and "'" not in line and '/' not in line:
                cleaned.append(line)                # Nothing to blank: most code lines.
                continue
            pieces, position, length = [], 0, len(line)
            while position < length:
                if state == "comment":
                    end = line.find('*/', position)
                    if end < 0:
                        pieces.append(' ' * (length - position))
                        position = length
                        break
                    pieces.append(' ' * (end + 2 - position))
                    position, state = end + 2, ""
                    continue
                if state == "raw":
                    end = line.find(raw_end, position)
                    if end < 0:
                        pieces.append(' ' * (length - position))
                        position = length
                        break
                    pieces.append(' ' * (end + len(raw_end) - 1 - position) + '"')
                    position, state = end + len(raw_end), ""
                    continue
                if state:
                    quote = '"' if state == "string" else "'"
                    match = (_STRING_REST if quote == '"' else _CHAR_REST).match(line, position)
                    if not match:
                        pieces.append(' ' * (length - position))
                        position = length
                        break
                    pieces.append(' ' * (match.end() - 1 - position) + quote)
                    position, state = match.end(), ""
                    continue
                match = _SCAN.search(line, position)
                if not match:
                    pieces.append(line[position:])
                    break
                begin, token = match.start(), match.group()
                pieces.append(line[position:begin])
                position = begin + len(token)
                if token == '//':
                    pieces.append(' ' * (length - begin))
                    break
                if token == '/*':
                    pieces.append('  ')
                    state, opened = "comment", (line_no, begin + 1)
                    continue
                if token == '"':
                    if begin and line[begin - 1] == 'R' and _RAW_PREFIX.search(line[max(0, begin - 4):begin]):
                        paren = line.find('(', position)
                        if 0 <= paren - position <= 16 and not re.search(r'[\s()\\]', line[position:paren]):
                            raw_end = f'){line[position:paren]}"'
                            state, opened = "raw", (line_no, begin)
                            pieces.append('"' + ' ' * (paren - begin))
                            position = paren + 1
                            continue
                    pieces.append('"')
                    state, opened = "string", (line_no, begin + 1)
                    continue
                if (position < length and line[position].isalnum()
                        and _NUMBER_TAIL.search(line[max(0, begin - 256):begin])):
                    pieces.append(token)            # A numeric digit separator.
                    continue
                pieces.append("'")
                state, opened = "char", (line_no, begin + 1)
            if state in ("string", "char") and not line.endswith('\\'):
                self._literal(*opened, "Unclosed string literal" if state == "string" else "Unclosed character literal")
                state = ""
            cleaned.append("".join(pieces))
        if not self.limited:
            for branch in stack:
                self._find(branch[3], branch[4], "Unclosed preprocessor conditional (missing #endif)")
        if state == "raw":
            self._literal(*opened, "Unclosed raw string literal", 2)
        elif state == "comment":
            self._literal(*opened, "Unclosed block comment '/*' (missing '*/')", 2)

    def _directive(self, match, line, line_no, file_path, stack, reading, defined, active):
        """Follow one directive; False once conditionals nest beyond the live limit."""
        name, argument = match[1], match[2]
        column = line.find('#') + 1
        if name in ('if', 'ifdef', 'ifndef'):
            if len(stack) >= _MAX_BRACKET_DEPTH:
                self._find(line_no, column, "Preprocessor nesting exceeds the live syntax checker limit; "
                           "Compile checks the complete file.", "warning")
                self.limited = True
                return False
            if name == 'if':
                selected, exact = _condition_value(argument, reading, defined)
            else:
                macro = argument.split()[0] if argument.split() else ""
                exact = False
                known = macro in defined or _READINGS[reading] == "defined" and not (
                    name == 'ifndef' and macro.upper().rstrip("_").endswith(("_H", "_HPP", "_INCLUDED")))
                selected = True if not reading else known == (name == 'ifdef')
                if macro == "__ASSEMBLER__":
                    selected, exact = name == 'ifndef', True    # Assembler text, never C++.
            self.conditional = self.conditional or not exact
            stack.append([active, selected, False, line_no, column, selected])
        elif name in ('elif', 'else'):
            if not stack:
                self._find(line_no, column, f"#{name} has no matching #if")
            else:
                branch = stack[-1]
                if branch[2]:
                    self._find(line_no, column, f"#{name} cannot follow #else")
                selected = not branch[5] and (name == 'else' or _condition_value(argument, reading, defined)[0])
                branch[1] = selected
                branch[5] = branch[5] or selected
                branch[2] = branch[2] or name == 'else'
        elif name == 'endif':
            if not stack:
                self._find(line_no, column, "#endif has no matching #if")
            else:
                stack.pop()
        elif active and name == 'define':
            macro = re.match(r'\s*(\w+)', argument)
            if macro:
                defined.add(macro[1])
        elif active and name == 'undef':
            defined.discard(argument.strip())
        elif active and name == 'include' and not line.rstrip().endswith('\\'):
            problem = check_include_directive(line, line_no, file_path)
            if problem:
                self._find(line_no, problem["col"], problem["message"], problem["severity"],
                           max(1, problem["endCol"] - problem["col"]))
        return True


def _check_labels(cleaned, directives, language, report):
    """Colons after case, default and access specifiers."""
    for index, line in enumerate(cleaned):
        if not _LABEL_LINE.match(line) or index in directives:
            continue
        stripped = line.strip()
        column = len(line.rstrip()) + 1
        match = _CASE.match(line)
        if match:
            depth = ternary = 0
            found = False
            for position, char in enumerate(stripped[4:], 4):
                if char in '([{':
                    depth += 1
                elif char in ')]}':
                    depth = max(0, depth - 1)
                elif not depth:
                    if char == '?':
                        ternary += 1
                    elif char == ':' and stripped[position:position + 2] != '::' and stripped[position - 1:position + 1] != '::':
                        if ternary:
                            ternary -= 1
                        else:
                            found = True
                            break
            following = next((text.strip() for text in cleaned[index + 1:index + 10] if text.strip()), "")
            if not found and not depth and not stripped.endswith(('\\', '?', '|', '+', '-', '*', '/', '%', '&', '^', '<', '>', '=', ',', '.', '::')) and (
                    not following.startswith((':', '?', '|', '+', '-', '*', '/', '%', '&', '^', '<', '>', '.', '...'))) and len(stripped) > 4:
                report(index + 1, column, "Missing colon ':' after 'case' label")
        elif _DEFAULT.match(line):
            following = next((text.strip() for text in cleaned[index + 1:index + 10] if text.strip()), "")
            if not following.startswith((':', ';', ',', ')')):
                report(index + 1, column, "Missing colon ':' after 'default' label")
        elif language == 'cpp' and (match := _ACCESS.match(line)):
            following = next((text.strip() for text in cleaned[index + 1:index + 10] if text.strip()), "")
            if not following.startswith(':'):
                report(index + 1, column, f"Missing colon ':' after '{match[1]}' access specifier")


def analyze_cpp_syntax(
    code: str,
    file_path: Path | str,
    all_defined_functions: set[str] | None = None
) -> list[dict]:
    """
    Checks C/C++/Arduino source structure without executing a compiler:
      - Bracket closure & balance ({}, (), []) with block/function context,
        reported where the bracket opened and once per mistake
      - Missing closing parentheses before ';' and before '{' in control conditions
      - Missing colons after 'case', 'default', and class access specifiers
      - Missing semicolons after statements, function calls, declarations,
        assignments, do-while loops, and struct/class/enum definitions, judged from
        scopes and tokens (src/syntax_statements.py) so macros, wrapped return types
        and conditional code stay quiet
      - Preprocessor directives, alternative conditional branches and backslash
        continuations; raw string literals R"delim(...)delim" and escape sequences
    Keeps content-bound, bounded caches and exact codepoint ranges. Target
    preprocessing, symbol/type resolution and library checks belong to Compile.
    """
    if not isinstance(file_path, Path):
        file_path = Path(file_path)

    file_name = file_path.name if hasattr(file_path, "name") else str(file_path)
    language = "c" if file_path.suffix.lower() == ".c" else "cpp"
    if len(code) > _MAX_SOURCE_CHARACTERS:
        return [{"file": file_name, "line": 1, "col": 1, "endLine": 1, "endCol": 1,
                 "message": "Source exceeds the live syntax checker size limit; Compile checks the complete file.",
                 "severity": "warning", "columnEncoding": "codepoint"}]
    cache_key = (hashlib.blake2b(code.encode('utf-8', errors='surrogatepass'), digest_size=16).digest(),
                 len(code), os.path.normcase(os.path.abspath(file_path)), language)
    with _CACHE_LOCK:
        cached = _SYNTAX_CODE_CACHE.get(cache_key)
        if cached is not None:
            return [dict(e) for e in cached]

    errors = _Diagnostics()
    lines = code.splitlines()

    def report(line_no, column, message, severity="error", width=1):
        errors.append({"file": file_name, "line": line_no, "col": column,
                       "endLine": line_no, "endCol": column + width,
                       "message": message, "severity": severity})

    # Which branch of an unknown #if a build uses is the target's choice, so a file is
    # read more than one way and a bracket is reported only where the code is in use.
    readings = []
    for reading in (0, 2, 1):
        scan = _Scanner(lines, file_path, language, reading)
        structure = Structure(scan.cleaned, scan.directives, language,
                              _MAX_STRUCTURAL_TOKENS, _MAX_BRACKET_DEPTH, max(1, _MAX_CACHE_DIAGNOSTICS)).check()
        readings.append((scan, structure))
        broken = any(pair[1].brackets or pair[0].literals for pair in readings)
        if not scan.conditional or not broken and (reading or not scan.skipped):
            break
    failing = [pair for pair in readings if pair[1].brackets or pair[0].literals]
    scan, structure = max(failing or readings, key=lambda pair: pair[0].active_lines)
    if len(failing) == 1 and len(readings) > 1 and scan.active_lines <= 1.25 * max(
            pair[0].active_lines for pair in readings if pair not in failing):
        scan, structure = max((pair for pair in readings if pair not in failing), key=lambda pair: pair[0].active_lines)
    for line_no, column, message, severity, width in scan.literals + scan.findings:
        report(line_no, column, message, severity, width)
    errors.omitted += structure.omitted
    if structure.limit is not None:
        report(*structure.position(structure.limit[0]), structure.limit[1], "warning")
    for offset, message, _pair in structure.brackets:
        report(*structure.position(offset), message)
    if structure.limit is None and not scan.limited:
        _check_labels(scan.cleaned, scan.directives, language, report)
        for offset, context in structure.types:
            following = _NEXT_CODE.match(structure.text, offset + 1)
            rest = following[1] if following else ""
            declaration = _TYPE_START.match(structure.text, following.start(1)) if following else None
            if not rest or rest == "}" or declaration and "\n" in structure.text[offset:following.start(1)]:
                report(*structure.position(offset), f"Missing semicolon ';' after {' '.join(context.split())} definition")
        for other_scan, other in readings:
            if other is structure or other_scan not in [pair[0] for pair in failing] and not other_scan.limited:
                for offset, reason, severity in other.statements:
                    report(*other.position(offset), f"Missing semicolon ';' after {reason}", severity)

    omitted = errors.omitted
    unique = {}
    for error in errors:
        row = min(max(1, error['line']), max(1, len(lines)))
        end_row = min(max(row, error.get('endLine', row)), max(1, len(lines)))
        max_column = len(lines[row - 1]) + 1 if lines else 1
        end_max = len(lines[end_row - 1]) + 1 if lines else 1
        error.update(line=row, endLine=end_row,
                     col=min(max(1, error['col']), max_column),
                     endCol=min(max(1, error.get('endCol', error['col'])), end_max),
                     columnEncoding='codepoint')
        unique[(row, error['col'], error['message'])] = error
    errors = sorted(unique.values(), key=lambda x: (x['line'], x['col']))
    if omitted or len(errors) > _MAX_CACHE_DIAGNOSTICS:
        errors = errors[:_MAX_CACHE_DIAGNOSTICS - 1] + [{
            'file': file_name, 'line': 1, 'col': 1, 'endLine': 1, 'endCol': 1,
            'severity': 'warning', 'columnEncoding': 'codepoint',
            'message': 'Further live syntax diagnostics omitted; Compile checks the complete file.'}]
    with _CACHE_LOCK:
        _remember_diagnostics(_SYNTAX_CODE_CACHE, cache_key, errors)
    return errors


def extract_project_functions(sketch_dir: Path) -> set[str]:
    """Mock project functions extractor (no longer needed, returns empty set)."""
    return set()


def analyze_file_syntax(
    file_path: Path | str,
    all_defined_functions: set[str] | None = None
) -> list[dict]:
    """Analyze current source bytes; matching file timestamps cannot hide edits."""
    fp = Path(file_path) if not isinstance(file_path, Path) else file_path
    try:
        code, digest = read_source_snapshot(fp)
    except (OSError, ValueError) as exc:
        return [{"file": fp.name, "line": 1, "col": 1, "endLine": 1, "endCol": 1,
                 "message": f"Cannot read source: {str(exc)[:300]}", "severity": "error",
                 "columnEncoding": "codepoint"}]
    file_key = (os.path.normcase(os.path.abspath(fp)), digest)

    with _CACHE_LOCK:
        cached = _SYNTAX_FILE_CACHE.get(file_key)
        if cached is not None:
            return [dict(e) for e in cached]

    errors = analyze_cpp_syntax(code, fp, all_defined_functions)

    with _CACHE_LOCK:
        _remember_diagnostics(_SYNTAX_FILE_CACHE, file_key, errors)

    return errors


def read_source_snapshot(file_path: Path | str) -> tuple[str, bytes]:
    """Read bounded source bytes once in a worker, retaining their fingerprint."""
    with Path(file_path).open('rb') as stream:
        raw = stream.read(_MAX_SOURCE_CHARACTERS * 4 + 1)
    code = raw.decode('utf-8', errors='replace')
    return code, hashlib.blake2b(raw, digest_size=16).digest()


def analyze_files_parallel(
    files: list[Path | str],
    all_defined_functions: set[str] | None = None,
    max_workers: int | None = None,
    source_contents: dict[str, str] | None = None
) -> list[dict]:
    """Analyze root sources using the small shared background parser pool."""
    if not files:
        return []
    def analyze(source):
        content = (source_contents or {}).get(str(source))
        return (analyze_cpp_syntax(content, source, all_defined_functions)
                if content is not None else analyze_file_syntax(source, all_defined_functions))
    if max_workers == 1:
        results = [diag for f in files for diag in analyze(f)]
        return sorted(results, key=lambda x: (x.get("file", ""), x.get("line", 0)))
    executor = get_syntax_executor()
    all_errors = []
    for start in range(0, len(files), 16):
        batch = files[start:start + 16]
        futures = [executor.submit(analyze, f) for f in batch]
        for source, fut in zip(batch, futures):
            try:
                all_errors.extend(fut.result())
            except Exception as exc:
                all_errors.append({"file": Path(source).name, "line": 1, "col": 1,
                    "endLine": 1, "endCol": 1, "severity": "error", "columnEncoding": "codepoint",
                    "message": f"Syntax check failed: {str(exc)[:300]}"})
    all_errors.sort(key=lambda x: (x.get("file", ""), x.get("line", 0)))
    return all_errors
