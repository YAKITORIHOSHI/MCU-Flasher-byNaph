"""Conservative, read-only declaration lookup within already-open C/C++ samples.

This is a source browser, not a compiler index: no include traversal, target
guessing or package writes. Ambiguous declarations are returned to the user.
"""
from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
import re

_NON_CODE = re.compile(
    r'//[^\n]*|/\*.*?\*/|R"(?P<delimiter>[^\s()\\]{0,16})\(.*?\)(?P=delimiter)"'
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.DOTALL)
_IDENTIFIER = re.compile(r'[^\W\d]\w*')
_NOT_TYPES = frozenset('return if else while for switch case break continue goto throw '
                       'delete new sizeof alignof static_assert co_return emit'.split())


def _typed_prefix(prefix):
    # Token checks avoid a nested repetition over arbitrary user source. Type
    # arguments are bounded by the 300-character declaration prefix above.
    plain = re.sub(r'<[^;{}()=]+>', '', prefix)
    return bool(re.fullmatch(r'[\w:\s*&]+', plain) and re.search(r'\w', plain)
                and not re.fullmatch(r'(?:\w+::)+', plain.strip())
                and not _NOT_TYPES.intersection(plain.split()))


def code_only(text):
    return _NON_CODE.sub(lambda match: ''.join('\n' if char == '\n' else ' ' for char in match[0]), text)


def identifier_at(text, offset):
    for match in _IDENTIFIER.finditer(text, max(0, text.rfind('\n', 0, offset))):
        if match.start() > offset:
            break
        if match.start() <= offset < match.end():
            word = match[0]
            return word if code_only(text)[match.start():match.end()] == word else ''
    return ''


@dataclass(frozen=True)
class Definition:
    path: str
    name: str
    line: int
    column: int
    label: str


def find_definitions(snapshots, word, active_path, offset, *, include_declarations=False):
    """Recognize typed functions/variables/types/macros; never jump to a use.

    Local declarations must enclose the reference and precede it. The innermost
    visible local wins. Overloads and multiple global candidates remain choices.
    Unsupported C++ constructs intentionally produce no destination.
    """
    if not _IDENTIFIER.fullmatch(word):
        return []
    candidates = []
    for path, text in snapshots:
        code = code_only(text)
        intervals, parents, stack = {}, {}, []
        for brace in re.finditer(r'[{}]', code):
            if brace[0] == '{':
                parents[brace.start()] = stack[-1] if stack else None
                stack.append(brace.start())
            elif stack:
                intervals[stack.pop()] = brace.start()
        intervals.update((start, len(code)) for start in stack)
        starts = sorted(intervals)
        for match in re.finditer(r'\b' + re.escape(word) + r'\b', code):
            start, end = match.span()
            line_start = code.rfind('\n', 0, start) + 1
            prefix = re.split(r'[;{}]', code[max(0, start-300):start])[-1].strip()
            prefix = re.sub(r'(?m)^\s*#[^\n]*', '', prefix).strip()
            suffix = code[end:end+4096]
            line_prefix = code[line_start:start].strip()
            label = ''
            if re.fullmatch(r'#\s*define', line_prefix):
                label = 'macro'
            elif re.fullmatch(r'(?:enum\s+class|class|struct|enum|union)', prefix):
                label = 'type'
            elif _typed_prefix(prefix):
                if suffix.lstrip().startswith('('):
                    # Parenthesized initializer and a call aren't definitions.
                    opening = end + len(suffix) - len(suffix.lstrip())
                    depth, closing = 0, None
                    for pos in range(opening, min(len(code), opening+16384)):
                        if code[pos] == '(':
                            depth += 1
                        elif code[pos] == ')':
                            depth -= 1
                            if not depth:
                                closing = pos
                                break
                    tail = code[closing+1:closing+1024] if closing is not None else ''
                    ending = re.match(r'\s*(?:(?:const|override|final|noexcept)\s*)*(?:->[^;{}]+)?([;{])', tail)
                    if ending:
                        label = 'function' if ending[1] == '{' else 'function declaration'
                elif re.match(r'\s*[=;\[,{]', suffix):
                    label = 'variable'
            if not label:
                continue
            scope_index = bisect_right(starts, start) - 1
            scope_start = starts[scope_index] if scope_index >= 0 else None
            while scope_start is not None and intervals[scope_start] < start:
                scope_start = parents[scope_start]
            scope = (scope_start, intervals[scope_start]) if scope_start is not None else None
            # Brace-local variables stay in their own file/block. Class/namespace
            # member lookup without a compilation context is deliberately bounded.
            if label == 'variable' and scope:
                if path != active_path or not scope[0] < offset < scope[1] or start > offset:
                    continue
                rank = scope[0] + 1
            else:
                rank = 0
            line = text.count('\n', 0, start)
            candidates.append((rank, Definition(path, word, line, start-line_start, label)))
    if not candidates:
        return []
    best = max(rank for rank, _ in candidates)
    result = [definition for rank, definition in candidates if rank == best]
    if not include_declarations and any(item.label == 'function' for item in result):
        result = [item for item in result if item.label != 'function declaration']
    return result


def split_arguments(text):
    """Split a non-template C++ list without counting commas inside literals."""
    def mask(match):
        value = match[0]
        prefix = ' ' if value.startswith(('//', '/*')) else 'x'
        return prefix + ''.join('\n' if char == '\n' else ' ' for char in value[1:])
    code = _NON_CODE.sub(mask, text)
    if '<' in code or '>' in code:
        return None  # The compiler owns template/comparison disambiguation.
    if not code.strip():
        return []
    parts, start, stack = [], 0, []
    for index, char in enumerate(code):
        if char in '([{':
            stack.append(char)
        elif char in ')]}':
            if not stack or stack.pop() != {')':'(', ']':'[', '}':'{'}[char]:
                return None
        elif char == ',' and not stack:
            parts.append(text[start:index].strip())
            start = index + 1
    return None if stack else parts + [text[start:].strip()]
