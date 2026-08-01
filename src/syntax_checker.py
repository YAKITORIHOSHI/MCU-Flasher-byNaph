import re
from pathlib import Path

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


def check_include_directive(line: str, line_no: int, file_path: Path):
    """
    Validates a single '#include' line for a properly closed <...> or "..." target.
    Returns an error dict if malformed, otherwise None.
    Handles cases like:
        #include <WiFiProv.h      -> missing closing '>'
        #include "Test.h          -> missing closing '"'
        #include WiFi.h           -> missing both delimiters
    """
    match = _INCLUDE_DIRECTIVE_RE.match(line)
    if not match:
        return None

    remainder = _strip_trailing_line_comment(match.group(1)).rstrip()

    if not remainder:
        return {
            "file": file_path.name,
            "line": line_no,
            "col": len(line) + 1,
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
                "message": f"#include directive is missing a closing '>': {remainder}",
                "severity": "error"
            }
        header_name = remainder[1:close_idx]
        if not header_name.strip():
            return {
                "file": file_path.name,
                "line": line_no,
                "col": match.start(1) + 2,
                "message": "Malformed #include directive: empty header name between '<' and '>'",
                "severity": "error"
            }
        trailing = remainder[close_idx + 1:].strip()
        if trailing:
            return {
                "file": file_path.name,
                "line": line_no,
                "col": match.start(1) + close_idx + 2,
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
                "message": f"#include directive is missing a closing '\"': {remainder}",
                "severity": "error"
            }
        header_name = remainder[1:close_idx]
        if not header_name.strip():
            return {
                "file": file_path.name,
                "line": line_no,
                "col": match.start(1) + 2,
                "message": "Malformed #include directive: empty header name between quotes",
                "severity": "error"
            }
        trailing = remainder[close_idx + 1:].strip()
        if trailing:
            return {
                "file": file_path.name,
                "line": line_no,
                "col": match.start(1) + close_idx + 2,
                "message": f'Unexpected characters after #include "{header_name}": \'{trailing}\'',
                "severity": "warning"
            }
        return None

    return {
        "file": file_path.name,
        "line": line_no,
        "col": match.start(1) + 1,
        "message": f"Malformed #include directive: expected <FileName.h> or \"FileName.h\", got '{remainder}'",
        "severity": "error"
    }

def analyze_cpp_syntax(code: str, file_path: Path, all_defined_functions: set[str] = None) -> list[dict]:
    """
    Analyzes C++ code for syntax errors (brackets, semicolons, quotes, etc.) 
    and checks if called global functions exist in all_defined_functions or standard APIs.
    """
    errors = []
    if all_defined_functions is None:
        all_defined_functions = set()

    # --- Pass 1: Parse brackets, braces, and literals ---
    stack = []  # Elements: (char, line_no, col_no)
    in_line_comment = False
    in_block_comment = False
    in_string = False
    in_char = False
    string_start = (0, 0)
    char_start = (0, 0)
    block_comment_start = (0, 0)

    lines = code.splitlines()

    for line_idx, line in enumerate(lines):
        line_no = line_idx + 1
        i = 0
        n = len(line)
        in_line_comment = False  # resets at the end of the line

        # '#include' targets (e.g. <WiFi.h> or "Test.h") are preprocessor tokens,
        # not real string/char literals or bracket pairs - they're validated
        # separately by check_include_directive(), so skip the generic scan here
        # to avoid duplicate/misleading "unclosed string" or bracket errors.
        if not in_block_comment and _INCLUDE_DIRECTIVE_RE.match(line):
            continue

        while i < n:
            c = line[i]

            if in_block_comment:
                if i + 1 < n and c == '*' and line[i+1] == '/':
                    in_block_comment = False
                    i += 2
                else:
                    i += 1
                continue

            if in_line_comment:
                break

            if in_string:
                if c == '\\':
                    i += 2  # skip escaped character
                elif c == '"':
                    in_string = False
                    i += 1
                else:
                    i += 1
                continue

            if in_char:
                if c == '\\':
                    i += 2
                elif c == '\'':
                    in_char = False
                    i += 1
                else:
                    i += 1
                continue

            # Check comment starts
            if i + 1 < n and c == '/' and line[i+1] == '/':
                in_line_comment = True
                break
            if i + 1 < n and c == '/' and line[i+1] == '*':
                in_block_comment = True
                block_comment_start = (line_no, i + 1)
                i += 2
                continue

            # Check string start
            if c == '"':
                in_string = True
                string_start = (line_no, i + 1)
                i += 1
                continue

            # Check char start
            if c == '\'':
                in_char = True
                char_start = (line_no, i + 1)
                i += 1
                continue

            # Track bracket balancing
            if c in ('(', '{', '['):
                stack.append((c, line_no, i + 1))
            elif c in (')', '}', ']'):
                if not stack:
                    # For unmatched '}', try to give a better diagnostic:
                    # look backwards for a function/block header missing its '{'
                    msg = f"Unmatched closing bracket '{c}'"
                    if c == '}':
                        # Scan previous lines for a likely block opener context
                        prev_ctx = []
                        for prev in reversed(lines[:line_idx]):
                            prev_s = prev.strip()
                            if not prev_s or prev_s.startswith('//'):
                                continue
                            prev_ctx.append(prev_s[:120])
                            if len(prev_ctx) >= 5:
                                break
                        if prev_ctx:
                            # Check if any of the previous lines looks like a function/control header
                            ctrl_kwds = ("void ", "int ", "float ", "double ", "char ", "string ",
                                         "bool ", "if ", "for ", "while ", "switch ", "else",
                                         "class ", "struct ", "union ", "namespace ")
                            found_header = None
                            for line_text in prev_ctx:
                                low = line_text.lower()
                                if any(low.startswith(k) for k in ctrl_kwds) and '(' in line_text and not low.endswith('{') and not low.endswith(';'):
                                    found_header = line_text
                                    break
                            if found_header:
                                msg = f"Missing '{{' after '{found_header.strip()[:60]}' on line {line_idx}"
                            else:
                                msg = f"Unmatched '}}' (possibly missing '{{' above line {line_idx})"
                    errors.append({
                        "file": file_path.name,
                        "line": line_no,
                        "col": i + 1,
                        "message": msg,
                        "severity": "error"
                    })
                else:
                    top_c, top_line, top_col = stack.pop()
                    expected = {')': '(', '}': '{', ']': '['}[c]
                    if top_c != expected:
                        errors.append({
                            "file": file_path.name,
                            "line": line_no,
                            "col": i + 1,
                            "message": f"Mismatched bracket: expected '{c}' to match '{top_c}' on line {top_line}",
                            "severity": "error"
                        })
            i += 1

        # End of line quotes check (unless continuation character \ exists)
        if in_string and not line.endswith('\\'):
            errors.append({
                "file": file_path.name,
                "line": string_start[0],
                "col": string_start[1],
                "message": "Unclosed string literal",
                "severity": "error"
            })
            in_string = False
        if in_char and not line.endswith('\\'):
            errors.append({
                "file": file_path.name,
                "line": char_start[0],
                "col": char_start[1],
                "message": "Unclosed character literal",
                "severity": "error"
            })
            in_char = False

    if in_block_comment:
        errors.append({
            "file": file_path.name,
            "line": block_comment_start[0],
            "col": block_comment_start[1],
            "message": "Unclosed block comment '/*' (missing '/')",
            "severity": "error"
        })
    while stack:
        c, line_no, col = stack.pop()
        msg = f"Unclosed open bracket '{c}'"
        if c == '{':
            msg = f"Open brace '{{' on line {line_no} is never closed (missing '}}')"
        errors.append({
            "file": file_path.name,
            "line": line_no,
            "col": col,
            "message": msg,
            "severity": "error"
        })

    # --- Pass 2: Line-by-line checks (semicolons, function calls) ---
    in_block_comment = False
    paren_nesting = 0   # tracks ( and [ depth only — not { }
    brace_depth = 0     # tracks { } block depth for context
    for line_idx, line in enumerate(lines):
        line_no = line_idx + 1

        # Strip comments and string contents for accurate line parsing
        clean_line = ""
        in_string_clean = False
        in_char_clean = False
        i = 0
        n = len(line)
        while i < n:
            c = line[i]
            if in_block_comment:
                if i + 1 < n and c == '*' and line[i+1] == '/':
                    in_block_comment = False
                    i += 2
                else:
                    i += 1
                continue
            if i + 1 < n and c == '/' and line[i+1] == '/':
                break  # line comment
            if i + 1 < n and c == '/' and line[i+1] == '*':
                in_block_comment = True
                i += 2
                continue
            if in_string_clean:
                if c == '\\':
                    i += 2
                elif c == '"':
                    in_string_clean = False
                    i += 1
                else:
                    i += 1
                continue
            if in_char_clean:
                if c == '\\':
                    i += 2
                elif c == '\'':
                    in_char_clean = False
                    i += 1
                else:
                    i += 1
                continue
            if c == '"':
                in_string_clean = True
                clean_line += '""'
                i += 1
            elif c == '\'':
                in_char_clean = True
                clean_line += "''"
                i += 1
            else:
                if c in ('(', '['):
                    paren_nesting += 1
                elif c in (')', ']'):
                    paren_nesting = max(0, paren_nesting - 1)
                elif c == '{':
                    brace_depth += 1
                elif c == '}':
                    brace_depth = max(0, brace_depth - 1)
                clean_line += c
                i += 1

        stripped = clean_line.strip()
        if not stripped:
            continue

        # 0. #include directive validation (checked against the raw line, since
        #    clean_line's string/quote stripping logic assumes well-formed quotes)
        if stripped.startswith("#"):
            include_error = check_include_directive(line, line_no, file_path)
            if include_error:
                errors.append(include_error)
            continue

        # 1. Missing Semicolon Check
        _CONTINUATION_ENDS = (
            "+", "-", "*", "/", "=", "&", "|", "^", "%", "?", ":", "<", ">",
            "!", ",", "&&", "||", "->", "::",
            # Comparison / assignment combos that often split across lines
            "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
            "<<", ">>", "<<=", ">>=", "&&=", "||=",
            ".",
        )
        _CONTINUATION_STARTS = (
            "?", ":", "+", "-", "*", "/", "%", "&", "|", "^", "=",
            "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
            "<<", ">>", "<<=", ">>=", "&&", "||", "->", "::",
            "<", ">", ".", ",",
            ")", "]", "}", ");", "],", "},",
        )
        _NON_STMT_ENDS = ("{", "}", ";", ",", "\\", ":")
        # Note: ")" is NOT in _NON_STMT_ENDS because lines like
        #   Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB);
        # need semicolons just like any other statement.  Only
        # function/method/control headers (whose next line is "{")
        # are exempted further below.
        _BLOCK_LIKE_STARTS = ("if", "else", "for", "while", "switch", "case", "default",
                              "class", "struct", "enum", "namespace", "extern", "do",
                              "try", "catch", "public:", "private:", "protected:",
                              "void", "int", "float", "double", "char", "short", "long",
                              "string", "auto", "uint8_t", "uint16_t", "uint32_t",
                              "int8_t", "int16_t", "int32_t", "bool", "unsigned")

        current_line_ends_continuation = (
            stripped.endswith("\\")
            or any(stripped.endswith(op) for op in _CONTINUATION_ENDS)
        )
        current_line_is_non_stmt_end = (
            any(stripped.endswith(e) for e in _NON_STMT_ENDS)
        )
        current_line_is_block_like = (
            any(stripped.startswith(k) for k in _BLOCK_LIKE_STARTS)
            and "(" in stripped
            and ")" in stripped
        )

        if (
            paren_nesting == 0
            and not stripped.startswith("#")
            and not stripped.startswith("//")
            and not stripped.startswith("/*")
            and not current_line_is_non_stmt_end
            and not current_line_ends_continuation
            and not current_line_is_block_like
        ):
            # Check next line for block opener or continuation
            next_is_open_brace = False
            next_is_continuation = False
            for j in range(line_idx + 1, len(lines)):
                ns_raw = lines[j].strip()
                if not ns_raw:
                    continue
                # Skip comment-only lines
                if ns_raw.startswith("//"):
                    continue
                if ns_raw.startswith("/*"):
                    if "*/" in ns_raw:
                        after = ns_raw.split("*/", 1)[1].strip()
                        if not after:
                            continue
                        ns_raw = after
                    else:
                        continue

                if ns_raw.startswith("{"):
                    next_is_open_brace = True
                elif any(ns_raw.startswith(op) for op in _CONTINUATION_STARTS):
                    next_is_continuation = True
                # Also check trailing comma before identifier on next line
                elif (stripped.endswith(",")
                      and re.match(r'^[a-zA-Z_][\w.]*\b', ns_raw)):
                    next_is_continuation = True
                break

            if not next_is_open_brace and not next_is_continuation:
                # Only warn when the line looks like a statement/assignment/call,
                # not a bare label or preprocessor directive
                tokens = stripped.split(None, 1)
                first_token = tokens[0].lower() if tokens else ""
                _STMT_KWDS = ("return", "break", "continue", "goto", "throw",
                              "int", "float", "double", "char", "short", "long",
                              "string", "auto", "uint8_t", "uint16_t", "uint32_t",
                              "int8_t", "int16_t", "int32_t", "bool", "void",
                              "serial", "digitalwrite", "digitalread", "analogwrite",
                              "analogread", "pinmode", "delay")
                looks_like_statement = (
                    "=" in stripped
                    or ("(" in stripped and ")" in stripped)
                    or first_token in _STMT_KWDS
                    or stripped.endswith(")")
                )
                if looks_like_statement:
                    errors.append({
                        "file": file_path.name,
                        "line": line_no,
                        "col": len(line) + 1,
                        "message": "Potential missing semicolon ';'",
                        "severity": "warning"
                    })

    return sorted(errors, key=lambda x: x["line"])

def extract_project_functions(sketch_dir: Path) -> set[str]:
    """Mock project functions extractor (no longer needed, returns empty set)."""
    return set()