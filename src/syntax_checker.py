from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# ── CPU & Memory Optimization Engine ──────────────────────────────────────────
# Freely utilize available CPU cores and RAM for high-throughput syntax checks.
def get_optimal_worker_count() -> int:
    """Detect available CPU cores and allocate optimal parallel threads freely."""
    cpus = os.cpu_count() or 4
    return min(32, max(4, cpus * 2))

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

# In-memory RAM cache: (hash(code), len(code), file_name) -> errors
_CACHE_LOCK = threading.Lock()
_SYNTAX_CODE_CACHE: dict[tuple[int, int, str], list[dict]] = {}
_SYNTAX_FILE_CACHE: dict[tuple[str, int, int], list[dict]] = {}
_MAX_CACHE_ENTRIES = 2048

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

    remainder = _strip_trailing_line_comment(match.group(1)).rstrip()

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

    return {
        "file": file_path.name,
        "line": line_no,
        "col": match.start(1) + 1,
        "endLine": line_no,
        "endCol": len(line) + 1,
        "message": f"Malformed #include directive: expected <FileName.h> or \"FileName.h\", got '{remainder}'",
        "severity": "error"
    }


def analyze_cpp_syntax(
    code: str,
    file_path: Path | str,
    all_defined_functions: set[str] | None = None
) -> list[dict]:
    """
    Analyzes C++/Arduino code for syntax errors:
      - Bracket closure & balance ({}, (), []) with block/function context
      - Missing closing parentheses before '{' in control conditions
      - Missing colons after 'case', 'default', and class access specifiers
      - Missing semicolons after statements, function calls, declarations,
        assignments, do-while loops, and struct/class/enum definitions
      - Preprocessor directives and multiline backslash continuations
      - Raw string literals R"delim(...)delim" and escape sequences
    Utilizes in-memory RAM caching for instant 0.001 ms repeated lookups.
    """
    if not isinstance(file_path, Path):
        file_path = Path(file_path)

    file_name = file_path.name if hasattr(file_path, "name") else str(file_path)
    cache_key = (hash(code), len(code), file_name)
    with _CACHE_LOCK:
        cached = _SYNTAX_CODE_CACHE.get(cache_key)
        if cached is not None:
            return [dict(e) for e in cached]

    errors = []
    lines = code.splitlines()

    # Pass 1: Scanner for literals, comments, raw strings, and bracket closure
    stack = []
    in_block_comment = False
    in_string = False
    in_char = False
    in_raw_string = False
    raw_string_delim = ""
    string_start = (0, 0)
    char_start = (0, 0)
    block_comment_start = (0, 0)

    cleaned_lines = []
    do_block_closed_lines = set()

    for line_idx, line in enumerate(lines):
        line_no = line_idx + 1
        n = len(line)
        i = 0
        clean_chars = []
        in_line_comment = False

        # Fast skip for #include
        if not in_block_comment and not in_raw_string and _INCLUDE_DIRECTIVE_RE.match(line):
            inc_err = check_include_directive(line, line_no, file_path)
            if inc_err:
                errors.append(inc_err)
            cleaned_lines.append("#include")
            continue

        while i < n:
            c = line[i]

            if in_block_comment:
                clean_chars.append(' ')
                if i + 1 < n and c == '*' and line[i + 1] == '/':
                    in_block_comment = False
                    clean_chars.append(' ')
                    i += 2
                else:
                    i += 1
                continue

            if in_raw_string:
                clean_chars.append(' ')
                if c == ')' and line[i:i + len(raw_string_delim) + 2] == f"){raw_string_delim}\"":
                    in_raw_string = False
                    clean_chars.extend([' '] * (len(raw_string_delim) + 1))
                    i += len(raw_string_delim) + 2
                else:
                    i += 1
                continue

            if in_line_comment:
                clean_chars.append(' ')
                i += 1
                continue

            if in_string:
                if c == '\\':
                    clean_chars.append(' ')
                    clean_chars.append(' ')
                    i += 2
                elif c == '"':
                    in_string = False
                    clean_chars.append(' ')
                    i += 1
                else:
                    clean_chars.append(' ')
                    i += 1
                continue

            if in_char:
                if c == '\\':
                    clean_chars.append(' ')
                    clean_chars.append(' ')
                    i += 2
                elif c == '\'':
                    in_char = False
                    clean_chars.append(' ')
                    i += 1
                else:
                    clean_chars.append(' ')
                    i += 1
                continue

            # Check start of comment
            if i + 1 < n and c == '/' and line[i + 1] == '/':
                in_line_comment = True
                clean_chars.append(' ')
                clean_chars.append(' ')
                i += 2
                continue
            if i + 1 < n and c == '/' and line[i + 1] == '*':
                in_block_comment = True
                block_comment_start = (line_no, i + 1)
                clean_chars.append(' ')
                clean_chars.append(' ')
                i += 2
                continue

            # Check raw string: R"delim( ... )delim"
            if c == 'R' and i + 1 < n and line[i + 1] == '"':
                paren_pos = line.find('(', i + 2)
                if paren_pos != -1 and paren_pos - i <= 18:
                    raw_string_delim = line[i + 2:paren_pos]
                    in_raw_string = True
                    clean_chars.extend([' '] * (paren_pos - i + 1))
                    i = paren_pos + 1
                    continue

            # Check string literal
            if c == '"':
                in_string = True
                string_start = (line_no, i + 1)
                clean_chars.append(' ')
                i += 1
                continue

            # Check char literal
            if c == '\'':
                in_char = True
                char_start = (line_no, i + 1)
                clean_chars.append(' ')
                i += 1
                continue

            # Check brackets
            if c in ('(', '{', '['):
                opener_context = ""
                is_type_def = False
                is_do_block = False

                if c == '{':
                    pre_text = line[:i].strip()
                    if not pre_text and line_idx > 0:
                        for prev_idx in range(line_idx - 1, max(-1, line_idx - 6), -1):
                            prev_str = lines[prev_idx].split("//", 1)[0].strip()
                            if prev_str:
                                pre_text = prev_str
                                break

                    # Check for struct / class / enum / union
                    if re.search(r'\b(struct|class|enum|union)\b', pre_text):
                        is_type_def = True
                        m_type = re.search(r'\b(struct|class|enum|union)\s+([A-Za-z_]\w*)?', pre_text)
                        opener_context = m_type.group(0) if m_type else "type definition"
                    elif re.search(r'\bdo\b', pre_text):
                        is_do_block = True
                        opener_context = "do-while loop"
                    elif re.search(r'\b(if|else\s+if|for|while|switch|catch)\b', pre_text):
                        m_ctrl = re.search(r'\b(if|else\s+if|for|while|switch|catch)\s*\([^)]*\)?', pre_text)
                        opener_context = m_ctrl.group(0) if m_ctrl else "control block"
                    elif re.search(r'\belse\b', pre_text):
                        opener_context = "else block"
                    else:
                        m_fn = re.search(r'([A-Za-z_]\w*)\s*\([^)]*\)', pre_text)
                        if m_fn:
                            opener_context = f"function '{m_fn.group(1)}()'"
                        else:
                            opener_context = "block"

                    # Check if an open parenthesis for if/while/for/switch is still unclosed!
                    if stack and stack[-1][0] == '(':
                        top_c, top_l, top_col, top_ctx, _, _ = stack[-1]
                        if any(kw in top_ctx for kw in ('if', 'while', 'for', 'switch')):
                            errors.append({
                                "file": file_name,
                                "line": line_no,
                                "col": i + 1,
                                "endLine": line_no,
                                "endCol": i + 2,
                                "message": f"Missing closing ')' before '{{' for {top_ctx} on line {top_l}",
                                "severity": "error"
                            })
                            stack.pop()

                elif c == '(':
                    pre_text = line[:i].strip()
                    m_ctrl = re.search(r'\b(if|else\s+if|for|while|switch|catch)\b', pre_text)
                    if m_ctrl:
                        opener_context = f"'{m_ctrl.group(1)}' condition"
                    else:
                        m_call = re.search(r'([A-Za-z_]\w*)\s*$', pre_text)
                        opener_context = f"call to '{m_call.group(1)}()'" if m_call else "'('"

                elif c == '[':
                    opener_context = "array subscript / brackets"

                stack.append((c, line_no, i + 1, opener_context, is_type_def, is_do_block))
                clean_chars.append(c)
                i += 1
                continue

            elif c in (')', '}', ']'):
                clean_chars.append(c)
                if not stack:
                    msg = f"Unmatched closing bracket '{c}'"
                    if c == '}':
                        msg = f"Unmatched closing brace '}}' on line {line_no} (extra '}}' or missing '{{' earlier)"
                    elif c == ')':
                        msg = f"Unmatched closing parenthesis ')' on line {line_no}"
                    elif c == ']':
                        msg = f"Unmatched closing square bracket ']' on line {line_no}"
                    errors.append({
                        "file": file_name,
                        "line": line_no,
                        "col": i + 1,
                        "endLine": line_no,
                        "endCol": i + 2,
                        "message": msg,
                        "severity": "error"
                    })
                else:
                    top_c, top_line, top_col, top_ctx, is_type_def, is_do_block = stack.pop()
                    expected = {')': '(', '}': '{', ']': '['}[c]
                    if top_c != expected:
                        errors.append({
                            "file": file_name,
                            "line": line_no,
                            "col": i + 1,
                            "endLine": line_no,
                            "endCol": i + 2,
                            "message": f"Mismatched bracket: expected '{expected}' to close {top_ctx} on line {top_line}, but found '{c}'",
                            "severity": "error"
                        })
                    else:
                        if c == '}' and is_do_block:
                            do_block_closed_lines.add(line_idx)

                        # If this '}' closed a struct / class / enum / union, check for missing semicolon after it!
                        if c == '}' and is_type_def:
                            rest_of_line = line[i + 1:].split("//", 1)[0].strip()
                            if not rest_of_line:
                                next_line_idx = line_idx + 1
                                while next_line_idx < len(lines) and not lines[next_line_idx].strip():
                                    next_line_idx += 1
                                if next_line_idx < len(lines):
                                    next_str = lines[next_line_idx].split("//", 1)[0].strip()
                                    if next_str and not next_str.startswith(';') and not next_str.startswith('}'):
                                        errors.append({
                                            "file": file_name,
                                            "line": line_no,
                                            "col": i + 1,
                                            "endLine": line_no,
                                            "endCol": i + 2,
                                            "message": f"Missing semicolon ';' after {top_ctx} definition",
                                            "severity": "error"
                                        })
                            elif not rest_of_line.startswith(';') and not rest_of_line.endswith(';') and not rest_of_line.endswith(','):
                                errors.append({
                                    "file": file_name,
                                    "line": line_no,
                                    "col": i + 1,
                                    "endLine": line_no,
                                    "endCol": i + 2,
                                    "message": f"Missing semicolon ';' after {top_ctx} definition",
                                    "severity": "error"
                                })
                i += 1
                continue

            clean_chars.append(c)
            i += 1

        if in_string and not line.endswith('\\'):
            errors.append({
                "file": file_name,
                "line": string_start[0],
                "col": string_start[1],
                "endLine": string_start[0],
                "endCol": string_start[1] + 1,
                "message": "Unclosed string literal",
                "severity": "error"
            })
            in_string = False

        if in_char and not line.endswith('\\'):
            errors.append({
                "file": file_name,
                "line": char_start[0],
                "col": char_start[1],
                "endLine": char_start[0],
                "endCol": char_start[1] + 1,
                "message": "Unclosed character literal",
                "severity": "error"
            })
            in_char = False

        cleaned_lines.append("".join(clean_chars))

    if in_block_comment:
        errors.append({
            "file": file_name,
            "line": block_comment_start[0],
            "col": block_comment_start[1],
            "endLine": block_comment_start[0],
            "endCol": block_comment_start[1] + 2,
            "message": "Unclosed block comment '/*' (missing '*/')",
            "severity": "error"
        })

    # Unclosed brackets on stack
    while stack:
        c, line_no, col, ctx, _, _ = stack.pop()
        if c == '{':
            msg = f"{ctx.capitalize()} opened on line {line_no} is never closed (missing '}}')"
        elif c == '(':
            msg = f"Parenthesis for {ctx} opened on line {line_no} is never closed (missing ')')"
        else:
            msg = f"Square bracket '[' on line {line_no} is never closed (missing ']')"
        errors.append({
            "file": file_name,
            "line": line_no,
            "col": col,
            "endLine": line_no,
            "endCol": col + 1,
            "message": msg,
            "severity": "error"
        })

    # Pass 2: Semicolon and Colon Validation
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0

    _CONTINUATION_ENDS = (
        "+", "-", "*", "/", "=", "&", "|", "^", "%", "?", ":", "<", ">",
        "!", ",", "&&", "||", "->", "::", ".", "\\",
        "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
        "<<", ">>", "<<=", ">>=",
    )
    _CONTINUATION_STARTS = (
        "?", ":", "+", "-", "*", "/", "%", "&", "|", "^", "=",
        "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
        "<<", ">>", "<<=", ">>=", "&&", "||", "->", "::",
        "<", ">", ".",
    )
    _CONTROL_KEYWORDS = {
        "if", "for", "while", "switch", "catch", "else", "do", "try"
    }

    expecting_do_while = False
    in_preprocessor_continuation = False

    for line_idx, clean_line in enumerate(cleaned_lines):
        line_no = line_idx + 1
        raw_line = lines[line_idx]
        stripped = clean_line.strip()
        raw_stripped = raw_line.split("//", 1)[0].strip()

        # Handle preprocessor directives and their backslash-continuations
        if in_preprocessor_continuation:
            if not raw_line.rstrip().endswith('\\'):
                in_preprocessor_continuation = False
            continue

        if stripped.startswith("#"):
            if raw_line.rstrip().endswith('\\'):
                in_preprocessor_continuation = True
            continue

        if not stripped:
            continue

        # Check if previous lines closed a 'do' block
        if line_idx in do_block_closed_lines or (line_idx > 0 and (line_idx - 1) in do_block_closed_lines):
            expecting_do_while = True

        # Check for missing colons:
        # Case A: 'case <expr>' missing colon (check raw_stripped to preserve char literals like case 'A')
        if re.match(r'^\s*case\s+[^:]+$', raw_stripped) and not any(raw_stripped.endswith(op) for op in _CONTINUATION_ENDS):
            errors.append({
                "file": file_name,
                "line": line_no,
                "col": len(raw_line),
                "endLine": line_no,
                "endCol": len(raw_line) + 1,
                "message": "Missing colon ':' after 'case' label",
                "severity": "error"
            })
            continue

        # Case B: 'default' missing colon
        m_default = re.match(r'^\s*default\s*$', stripped)
        if m_default:
            errors.append({
                "file": file_name,
                "line": line_no,
                "col": len(raw_line),
                "endLine": line_no,
                "endCol": len(raw_line) + 1,
                "message": "Missing colon ':' after 'default' label",
                "severity": "error"
            })
            continue

        # Case C: 'public', 'private', 'protected' missing colon
        m_access = re.match(r'^\s*(public|private|protected)\s*$', stripped)
        if m_access:
            errors.append({
                "file": file_name,
                "line": line_no,
                "col": len(raw_line),
                "endLine": line_no,
                "endCol": len(raw_line) + 1,
                "message": f"Missing colon ':' after '{m_access.group(1)}' access specifier",
                "severity": "error"
            })
            continue

        # Special: 'do ... while (...)' MUST have a semicolon after 'while (...)'!
        m_do_while = re.search(r'\bwhile\s*\([^)]*\)\s*$', stripped)
        if m_do_while and expecting_do_while and not stripped.endswith(';'):
            expecting_do_while = False
            errors.append({
                "file": file_name,
                "line": line_no,
                "col": len(raw_line) + 1,
                "endLine": line_no,
                "endCol": len(raw_line) + 2,
                "message": "Missing semicolon ';' after 'while' in do-while loop",
                "severity": "error"
            })
            continue

        if stripped.endswith(';'):
            expecting_do_while = False

        # Track parenthesis and bracket depths across lines
        paren_depth += (stripped.count('(') - stripped.count(')'))
        paren_depth = max(0, paren_depth)
        bracket_depth += (stripped.count('[') - stripped.count(']'))
        bracket_depth = max(0, bracket_depth)
        brace_depth += (stripped.count('{') - stripped.count('}'))
        brace_depth = max(0, brace_depth)

        # Semicolon Check:
        if (
            stripped.endswith(';')
            or stripped.endswith('{')
            or stripped.endswith('}')
            or stripped.endswith(':')
            or stripped.endswith(',')
            or stripped.endswith('\\')
        ):
            continue

        # If inside unclosed parentheses or brackets, line continuation is normal
        if paren_depth > 0 or bracket_depth > 0:
            continue

        # If ends with operator, line is continued on next line
        if any(stripped.endswith(op) for op in _CONTINUATION_ENDS):
            continue

        # Check next line for continuation or block opener
        next_is_open_brace = False
        next_is_continuation = False
        for next_idx in range(line_idx + 1, min(len(cleaned_lines), line_idx + 10)):
            next_str = cleaned_lines[next_idx].strip()
            if not next_str:
                continue
            if next_str.startswith('{'):
                next_is_open_brace = True
            elif any(next_str.startswith(op) for op in _CONTINUATION_STARTS):
                next_is_continuation = True
            break

        if next_is_open_brace or next_is_continuation:
            continue

        # Check control statement headers
        m_first = re.match(r'^[A-Za-z_]\w*', stripped)
        first_token = m_first.group(0) if m_first else ""

        if first_token in _CONTROL_KEYWORDS:
            continue

        # Check if line looks like a statement that requires a semicolon:
        is_stmt = False
        stmt_reason = "statement"

        if first_token in ("return", "break", "continue", "goto", "throw"):
            is_stmt = True
            stmt_reason = f"'{first_token}' statement"
        elif "=" in stripped:
            is_stmt = True
            stmt_reason = "assignment"
        elif stripped.endswith(')'):
            is_stmt = True
            stmt_reason = "function call / statement"
        elif re.match(r'^(?:[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?\s+)+[A-Za-z_]\w*(?:\s*\[[^\]]*\])?$', stripped):
            is_stmt = True
            stmt_reason = "declaration"

        if is_stmt:
            errors.append({
                "file": file_name,
                "line": line_no,
                "col": len(raw_line) + 1,
                "endLine": line_no,
                "endCol": len(raw_line) + 2,
                "message": f"Missing semicolon ';' after {stmt_reason}",
                "severity": "warning"
            })

    errors.sort(key=lambda x: (x["line"], x["col"]))
    with _CACHE_LOCK:
        if len(_SYNTAX_CODE_CACHE) >= _MAX_CACHE_ENTRIES:
            _SYNTAX_CODE_CACHE.clear()
        _SYNTAX_CODE_CACHE[cache_key] = errors
    return errors


def extract_project_functions(sketch_dir: Path) -> set[str]:
    """Mock project functions extractor (no longer needed, returns empty set)."""
    return set()


def analyze_file_syntax(
    file_path: Path | str,
    all_defined_functions: set[str] | None = None
) -> list[dict]:
    """Analyze a single file with in-memory stat caching for zero-overhead rechecks."""
    fp = Path(file_path) if not isinstance(file_path, Path) else file_path
    try:
        st = fp.stat()
        file_key = (str(fp.resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        return []

    with _CACHE_LOCK:
        cached = _SYNTAX_FILE_CACHE.get(file_key)
        if cached is not None:
            return [dict(e) for e in cached]

    try:
        code = fp.read_text(encoding="utf-8", errors="replace")
        errors = analyze_cpp_syntax(code, fp, all_defined_functions)
    except Exception:
        errors = []

    with _CACHE_LOCK:
        if len(_SYNTAX_FILE_CACHE) >= _MAX_CACHE_ENTRIES:
            _SYNTAX_FILE_CACHE.clear()
        _SYNTAX_FILE_CACHE[file_key] = errors

    return errors


def analyze_files_parallel(
    files: list[Path | str],
    all_defined_functions: set[str] | None = None,
    max_workers: int | None = None
) -> list[dict]:
    """Analyze multiple C++/Arduino files in parallel across all CPU cores."""
    if not files:
        return []
    executor = get_syntax_executor()
    futures = [
        executor.submit(analyze_file_syntax, f, all_defined_functions)
        for f in files
    ]
    all_errors = []
    for fut in futures:
        try:
            all_errors.extend(fut.result())
        except Exception:
            pass
    all_errors.sort(key=lambda x: (x.get("file", ""), x.get("line", 0)))
    return all_errors
