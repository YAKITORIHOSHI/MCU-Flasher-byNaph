"""Bracket, block and statement structure of C/C++ code with blanked literals.

Line-shape rules mistake macros, wrapped return types and conditional code for
errors. This pass follows scopes instead and judges only a line end where two
operands meet: no C or C++ statement can contain that without a separator, so valid
library code stays quiet. It uses no GUI toolkit and never runs a compiler.
"""
from __future__ import annotations

from bisect import bisect_right
import re

_STRUCTURE = re.compile(r'[(){}\[\];]')
_TOKEN = re.compile(r'''[A-Za-z_]\w*|"\s*"|'\s*'|\.?\d(?:[eEpP][+-]|[\w.'])*|::|->\*?|\+\+|--|<<=|>>=|<=>'''
                    r'''|[-+*/%&|^=!<>]=|&&|\|\||<<|>>|\.\.\.|\S''')
_DECLARES = re.compile(r'(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*(?:<[^;{}()]*>)?(?:\s*[*&]+\s*|\s+)[A-Za-z_]\w*\s*[;=(\[{,]')
_OBJECT_USE = re.compile(r'\s*(?:\.|->|::|[-+*/%&|^]?=[^=]|\+\+|--)')
_NEXT = re.compile(r'\s*([A-Za-z_]\w*|\+\+|--|["\'~}]|\d)')
_LEADING_LABEL = re.compile(r'\s*(?:case\b(?:[^?:;{}]|::)*?(?<!:):(?!:)|(?:default|public|private|protected|signals|slots)\s*:(?!:)'
                            r'|[A-Za-z_]\w*\s*:(?!:)|else\b(?!\s*if\b)|do\b|try\b)')
_CONTROL_HEAD = re.compile(r'\b(if|for|while|switch|catch)\s*(?:constexpr\s*)?$')
_CALLEE = re.compile(r'([A-Za-z_]\w*)\s*$')
_TYPE_HEAD = re.compile(r'\b(struct|class|union|enum)\b')
_LAMBDA_TAIL = re.compile(r'\]\s*(?:\(.*\)\s*)?(?:(?:mutable|constexpr|noexcept)\s*)*(?:->\s*[\w:<>*&\s,]+)?$', re.S)
_BODY_TAIL = frozenset(("const", "noexcept", "override", "final", "else", "do", "try", "volatile", "mutable", "&", "&&"))

_TYPE_WORDS = frozenset("""void bool boolean char short int long float double unsigned signed auto byte word size_t ssize_t
    int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t wchar_t String""".split())
_SPECIFIERS = frozenset("""const constexpr constinit consteval static extern inline volatile register mutable virtual explicit
    friend typename thread_local struct class union enum typedef using template unsigned signed long short""".split())
_JUMPS = frozenset(("return", "break", "continue", "goto", "throw", "co_return"))
_CONTROL = frozenset(("if", "for", "while", "switch", "catch"))
# Words that continue an expression or declarator instead of starting a statement.
_CONTINUES = frozenset("""and or not xor bitand bitor compl and_eq or_eq xor_eq not_eq const volatile noexcept override final
    throw requires mutable restrict __attribute__ __restrict __asm__ __asm asm alignas try catch while""".split())
# Words that need something after them, so a line may end here.
_EXPECTS = frozenset("""new delete return throw sizeof alignof typeid else do case goto operator typename co_await co_yield
    co_return not compl __extension__ static const constexpr inline extern volatile unsigned signed struct class union enum
    namespace using typedef template friend virtual explicit register mutable default asm __asm__ __asm""".split())
_NOT_CALLS = frozenset("""sizeof defined decltype alignas alignof typeof __typeof__ __attribute__ __declspec asm __asm__ __asm
    volatile noexcept throw static_assert _Static_assert if for while switch catch""".split())
_ASSIGN = frozenset(("=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="))
_MACRO = re.compile(r'(?:[A-Z][A-Z0-9_]*|_[A-Z0-9_]+|__\w+)$')
_MAX_SLICE = 4096


def _word(token):
    return token[0].isalpha() or token[0] == "_"


def _operand(token):
    return _word(token) or token[0] in "\"'" or token[0].isdigit() or token in (")", "]", "++", "--") or (
        token[0] == "." and len(token) > 1 and token[1].isdigit())


class _Scope:
    __slots__ = ("kind", "start", "opener", "resume", "type_name", "is_do")

    def __init__(self, kind, start, opener=-1, resume=False, type_name="", is_do=False):
        self.kind = kind            # file, class, enum, body or init
        self.start = start          # where its open statement begins
        self.opener = opener
        self.resume = resume        # the enclosing statement continues after }
        self.type_name = type_name
        self.is_do = is_do


def _balanced_open(tokens, index):
    """Index of the bracket that tokens[index] closes, or -1."""
    depth = 0
    for position in range(index, -1, -1):
        token = tokens[position]
        if token in (")", "]", "}"):
            depth += 1
        elif token in ("(", "[", "{"):
            depth -= 1
            if not depth:
                return position
    return -1


def _lone_type(tokens):
    """Whether these tokens name one type (or none), so its declarator may follow."""
    names, depth = 0, 0
    previous = ""
    for token in tokens:
        if token == "<":
            depth += 1
        elif token == ">":
            depth -= 1
        elif token == ">>":
            depth -= 2
        elif depth <= 0:
            if not (_word(token) or token == "::"):
                return False
            if _word(token) and token not in _SPECIFIERS and previous != "::":
                names += 1
        previous = token
    return names < 2


def _is_cast(tokens):
    """Whether a trailing (...) is a C cast rather than a call or a grouped value."""
    opened = _balanced_open(tokens, len(tokens) - 1)
    if opened < 0:
        return False
    inner = tokens[opened + 1:-1]
    before = tokens[opened - 1] if opened else ""
    if before == ">":
        return False                                    # static_cast<int>(value) is complete.
    typeish = bool(inner) and all(_word(token) or token in ("*", "&", "&&", "::", "<", ">", ",", "[", "]") for token in inner)
    return typeish and (not before or before in _JUMPS or not _operand(before))


def _depth_zero(tokens):
    """The tokens outside every (), [] and {} of this statement."""
    depth, outside = 0, []
    for token in tokens:
        if token in ("(", "[", "{"):
            depth += 1
        elif token in (")", "]", "}"):
            depth = max(0, depth - 1)
        elif not depth:
            outside.append(token)
    return outside


def judge(kind, tokens, following, language="cpp", declares=False, object_use=False):
    """(reason, severity) when `following`, on a later line, cannot continue `tokens`.

    `declares` means the next line reads as a new declaration of its own, and
    `object_use` that `following` names an object rather than a macro.
    """
    first, last = tokens[0], tokens[-1]
    if len(tokens) == 1 and first in _JUMPS and kind == "body":
        return (f"'{first}' statement", "error") if first in ("break", "continue") or following == "}" else None
    if not _operand(last) or last in _EXPECTS or last in _CONTINUES:
        return None
    if following in _CONTINUES and not (following in ("try", "while") or following in ("const", "volatile")
                                        and (kind == "body" or last != ")" or any(token in _ASSIGN for token in tokens))):
        return None
    if tokens[:2] == ["using", "namespace"] and len(tokens) > 2:
        return ("using directive", "error") if _word(following) else None
    if first == "using" and "=" in tokens:
        return ("type alias", "error") if declares or following in _TYPE_WORDS or following in _SPECIFIERS else None
    if first in ("template", "using", "friend", "namespace", "asm", "__asm__", "__asm", "#") or "operator" in tokens:
        return None
    if following in ("\"", "'") and (last[0] in "\"'" or _word(last) or last == ")") or (
            last[0] in "\"'" and _MACRO.match(following) and not object_use):
        return None                                     # Adjacent literals and string macros join.
    starts_code = following in _CONTROL or following in _JUMPS or following in ("else", "do", "case", "default", "try")
    ends_block = following == "}"
    outside = _depth_zero(tokens)
    assigned = any(token in _ASSIGN for token in outside)
    macro_end = _word(last) and bool(_MACRO.match(last))
    if kind == "body":
        if first in _JUMPS:
            if last == ")" and (_is_cast(tokens) or tokens[_balanced_open(tokens, len(tokens) - 1) - 1] == "new"):
                return None
            return f"'{first}' statement", "error"
        if _lone_type(tokens):
            return None
        if first == "typedef":
            return "type definition", "error"
        if assigned:
            if last == ")" and _is_cast(tokens):
                return None
            if macro_end and following in ("\"", "'"):
                return None
            return "assignment", "error"
        if last in ("++", "--") or first in ("++", "--"):
            return "increment / decrement", "error"
        if last == ")":
            opened = _balanced_open(tokens, len(tokens) - 1)
            name = tokens[opened - 1] if opened > 0 else ""
            before = tokens[:max(0, opened - 1)]
            if before and not _operand(before[-1]) and before[-1] not in (".", "->", "::", "*", "&", "<", ">") and (
                    not name or _word(name) and name not in _NOT_CALLS):
                return "statement", "error" if ends_block or starts_code else "warning"
            if not name or not _word(name) or name in _NOT_CALLS or _MACRO.match(name):
                return None
            if before and before[-1] in (".", "->", "::"):
                return "function call / statement", "error"
            if not before and name.endswith("_t"):
                return None
            if not before:
                # A lowercase name can still be a macro that expands to a statement.
                return "function call / statement", "error" if ends_block and name[0].islower() else "warning"
            if all(_word(token) or token in ("::", "*", "&", "<", ">", ",") for token in before):
                if any(_MACRO.match(token) for token in before if _word(token)):
                    return None
                return "declaration", "error" if ends_block or starts_code or following in _TYPE_WORDS else "warning"
            return "statement", "warning"
        if last == "]":
            names = [token for token in outside if _word(token)]
            if first in _TYPE_WORDS and len(names) > 1:
                return "declaration", "error"
            return ("statement", "warning") if ends_block and len(tokens) > 3 else None
        words = [token for token in tokens if token not in _SPECIFIERS]
        if all(_word(token) or token == "::" for token in words):
            names = [token for index, token in enumerate(words)
                     if _word(token) and (not index or words[index - 1] != "::")]
            if len(names) < 2 or last in _TYPE_WORDS or any(_MACRO.match(name) for name in names):
                return None
            return "declaration", "error" if first in _TYPE_WORDS or starts_code or ends_block else "warning"
        if macro_end:
            return None
        if first in _TYPE_WORDS and _word(last) and last not in _TYPE_WORDS:
            return "declaration", "error"
        if starts_code or any(token in ("<<", ">>", "==", "!=", "+", "-", "/", "%", "&&", "||", ".", "->") for token in outside):
            return "statement", "error" if ends_block or starts_code else "warning"
        return None
    if kind in ("file", "class"):
        declares = declares or following in _TYPE_WORDS or following in _SPECIFIERS
        if not declares and not (ends_block and kind == "class"):
            names = [token for token in tokens if _word(token) and token not in _SPECIFIERS]
            if (_word(following) and len(names) == 2 and names[0] in _TYPE_WORDS and names[1] == last
                    and last not in _TYPE_WORDS and all(_word(token) for token in tokens)
                    and last[0].islower() and not _MACRO.match(following)):
                return "declaration", "warning"
            return None
        if _lone_type(tokens):
            return None
        if first == "typedef":
            return ("type definition", "error") if not macro_end and last not in _TYPE_WORDS else None
        if assigned:
            return ("assignment", "error") if first != "extern" or len(outside) > 2 else None
        names = [token for token in outside if _word(token) and token not in _SPECIFIERS]
        if any(_MACRO.match(name) for name in names):
            return None
        if last == ")" and language != "c":
            opened = _balanced_open(tokens, len(tokens) - 1)
            before = tokens[:opened]
            if (len(before) >= 2 and _word(before[-1]) and before[-1] not in _NOT_CALLS
                    and all(_word(token) or token in ("*", "&", "::", "<", ">") for token in before)
                    and before[-2] not in ("::",) and len([t for t in before if _word(t) and t not in _SPECIFIERS]) >= 2):
                return "declaration", "error" if before[0] in _TYPE_WORDS else "warning"
            return None
        if not all(_word(token) for token in tokens):
            if last == "]" and first in _TYPE_WORDS and len(outside) > 1:
                return "declaration", "error"
            return None
        if len(names) >= 2 and names[-1] not in _TYPE_WORDS and first not in ("extern", "inline") and (
                names[0] in _TYPE_WORDS or declares or ends_block):
            return "declaration", "error" if names[0] in _TYPE_WORDS else "warning"
    return None


def _classify(header, kind, depth):
    """What a { means here, whether its statement continues after }, and any type name."""
    if depth:
        # A lambda inside a call's arguments; anything else there is a value list.
        return ("body" if _LAMBDA_TAIL.search(header) else "init"), True, ""
    text = header.strip()
    if not text:
        return ("body" if kind == "body" else "file" if kind == "file" else "init"), False, ""
    tokens = _TOKEN.findall(text[-_MAX_SLICE:])
    last = tokens[-1]
    outside = _depth_zero(tokens)
    if "namespace" in tokens[:2] or tokens[0] == "extern" and len(tokens) == 2 and last[0] == '"':
        return "file", False, ""
    assigned = any(token in _ASSIGN for token in outside) and "operator" not in tokens
    if assigned or last in ("return", ",") or tokens[0] in ("return", "throw"):
        return ("body" if _LAMBDA_TAIL.search(text) and last != "]" or last == "]" and _opens_lambda(tokens)
                else "init"), True, ""
    tail = len(tokens) - 1
    while tail > 0 and tokens[tail] in _BODY_TAIL and tokens[tail] not in ("else", "do", "try"):
        tail -= 1
    opened = _balanced_open(tokens, tail) if tokens[tail] == ")" else -1
    owner = tokens[opened - 1] if opened > 0 else ""
    if opened < 0 or owner in ("__attribute__", "alignas", "__declspec", "__aligned", "__packed"):
        # `struct State read() {` is a function; a type definition has no call before {.
        match = None
        for found in _TYPE_HEAD.finditer(text):
            if text[:found.start()].count("<") <= text[:found.start()].count(">"):
                match = found
                break
        if match:
            name = re.match(r'(?:struct|class|union|enum)\s*(?:class\s+|struct\s+)?([A-Za-z_]\w*)?', text[match.start():])
            return ("enum" if match[1] == "enum" else "class"), True, name[0].strip()
    if last == ")" or last in _BODY_TAIL:
        return "body", _LAMBDA_TAIL.search(text) is not None and _opens_lambda(tokens), ""
    if last == "]":
        return ("body", True, "") if _opens_lambda(tokens) else ("init", True, "")
    if "->" in outside and ")" in tokens:
        return "body", False, ""                        # Trailing return type.
    if ")" in tokens and ":" in outside:
        return "init", True, ""                         # A brace-initialized member before the body.
    if last in ("}",):
        return "body", False, ""
    if kind == "body" and len(tokens) == 1:
        return "body", False, ""
    return "init", True, ""


def _opens_lambda(tokens):
    """Whether the statement's last bracket group is a lambda introducer."""
    index = len(tokens) - 1
    while index >= 0 and tokens[index] in ("mutable", "constexpr", "noexcept"):
        index -= 1
    if index >= 0 and tokens[index] == ")":
        index = _balanced_open(tokens, index) - 1
    if index < 0 or tokens[index] != "]":
        return False
    opened = _balanced_open(tokens, index)
    return opened >= 0 and (not opened or not _operand(tokens[opened - 1]) or tokens[opened - 1] in _JUMPS)


def opener_context(text, offset):
    """A readable name for the bracket at `offset`, worked out only for a message."""
    char = text[offset]
    if char == "[":
        return "array subscript / brackets"
    before = text[max(0, offset - 512):offset].rstrip()
    if char == "(":
        control = _CONTROL_HEAD.search(before)
        if control:
            return f"'{control[1]}' condition"
        name = re.search(r'([A-Za-z_]\w*)$', before)
        return f"call to '{name[1]}()'" if name else "'('"
    tail = re.split(r'[;{}]', before)[-1].strip()
    match = re.search(r'\b(struct|class|enum|union)\s*([A-Za-z_]\w*)?[^=()]*$', tail)
    if match:
        return match[0].split(":")[0].strip() if match[2] else match[1]
    if re.search(r'\bdo$', tail):
        return "do-while loop"
    control = re.search(r'\b(else\s+if|if|for|while|switch|catch)\s*\(.*\)$', tail, re.S)
    if control:
        return f"'{control[1]}' block"
    if re.search(r'\belse$', tail):
        return "else block"
    if "=" in _depth_zero(_TOKEN.findall(tail)):
        return "initializer"
    name = re.search(r'([A-Za-z_]\w*)\s*\([^()]*(?:\([^()]*\)[^()]*)*\)[\w\s]*$', tail)
    return f"function '{name[1]}()'" if name else "block"


class Structure:
    """One reading of a source: its bracket errors and unfinished statements."""

    def __init__(self, cleaned, directive_lines=(), language="cpp", max_tokens=262144, max_depth=1024,
                 max_findings=8192):
        self.cleaned = cleaned
        self.text = "\n".join(cleaned)
        self.language = language
        self.max_tokens = max_tokens
        self.max_depth = max_depth
        self.max_findings = max_findings
        self.omitted = 0
        self.directives = sorted(directive_lines)
        self.offsets = [0]
        for line in cleaned:
            self.offsets.append(self.offsets[-1] + len(line) + 1)
        self.brackets = []          # (offset, message, pair offset)
        self.statements = []        # (offset after the token, reason, severity)
        self.types = []             # (offset of }, type text)
        self.limit = None           # (offset, message)

    def position(self, offset):
        """1-based line and column of a text offset."""
        line = min(bisect_right(self.offsets, offset) - 1, max(0, len(self.cleaned) - 1))
        return line + 1, offset - self.offsets[line] + 1

    def _add(self, findings, *finding):
        if len(findings) < self.max_findings:
            findings.append(finding)
        else:
            self.omitted += 1

    def _candidates(self):
        """Offsets just past a line's last code character where a statement may be unfinished."""
        offset = 0
        for line in self.cleaned:
            end = len(line.rstrip())
            if end:
                char = line[end - 1]
                if char.isalnum() or char in "_)]\"'}" or line[max(0, end - 2):end] in ("++", "--"):
                    yield offset + end
            offset += len(line) + 1

    def _crosses_directive(self, start, end):
        first, last = bisect_right(self.offsets, start) - 1, bisect_right(self.offsets, end) - 1
        index = bisect_right(self.directives, first)
        return index < len(self.directives) and self.directives[index] <= last

    def check(self):
        text, scopes = self.text, [_Scope("file", 0)]
        stack = []                  # (char, offset)
        depths = [0]                # ( and [ inside each scope
        candidates = self._candidates()
        candidate = next(candidates, None)
        while_after_do = None
        pending_do = -1
        count = 0
        for match in _STRUCTURE.finditer(text):
            offset, char = match.start(), match.group()
            while candidate is not None and candidate <= offset:
                if not depths[-1] and candidate != offset or text[candidate - 1] in ")]}" and candidate <= offset:
                    if not depths[-1]:
                        self._judge(scopes[-1], candidate)
                candidate = next(candidates, None)
            if char == ";":
                if depths[-1]:
                    allowed = self._separators(stack)
                    if allowed:
                        continue
                    self._unclosed(stack, depths)
                scopes[-1].start = offset + 1
                continue
            count += 1
            if count > self.max_tokens:
                self.limit = (offset, "Source complexity exceeds the live syntax checker limit; Compile checks the complete file.")
                return self
            if char in "([":
                if len(stack) >= self.max_depth:
                    self.limit = (offset, "Nesting exceeds the live syntax checker limit; Compile checks the complete file.")
                    return self
                if char == "(" and pending_do >= 0 and not depths[-1]:
                    if text[pending_do:offset].strip() == "while":
                        while_after_do = len(stack)
                    pending_do = -1
                stack.append((char, offset))
                depths[-1] += 1
            elif char == "{":
                if len(stack) >= self.max_depth:
                    self.limit = (offset, "Nesting exceeds the live syntax checker limit; Compile checks the complete file.")
                    return self
                scope = scopes[-1]
                header = text[max(self._after_labels(scope.start, offset, True), offset - _MAX_SLICE):offset]
                if stack and stack[-1][0] == "(" and not _LAMBDA_TAIL.search(header):
                    opened = stack[-1][1]
                    control = _CONTROL_HEAD.search(text[max(0, opened - 64):opened])
                    if control:
                        line, _ = self.position(opened)
                        self._add(self.brackets, offset, f"Missing closing ')' before '{{' for '{control[1]}' condition on line {line}",
                                  opened)
                        stack.pop()
                        depths[-1] = max(0, depths[-1] - 1)
                kind, resume, type_name = _classify(header, scope.kind, depths[-1])
                is_do = kind == "body" and re.search(r'\bdo\s*$', header) is not None
                stack.append((char, offset))
                scopes.append(_Scope(kind, offset + 1, offset, resume, type_name, is_do))
                depths.append(0)
            else:
                expected = {")": "(", "]": "[", "}": "{"}[char]
                if char == "}" and depths[-1] and len(scopes) > 1:
                    self._unclosed(stack, depths)           # Every ( or [ still open in this block.
                if not stack or char != "}" and stack[-1][0] == "{":
                    line, _ = self.position(offset)
                    self._add(self.brackets, offset, {
                        "}": f"Unmatched closing brace '}}' on line {line} (extra '}}' or missing '{{' earlier)",
                        ")": f"Unmatched closing parenthesis ')' on line {line}",
                        "]": f"Unmatched closing square bracket ']' on line {line}"}[char], -1)
                    continue
                opened_char, opened = stack.pop()
                if opened_char != expected:
                    line, _ = self.position(opened)
                    wanted = {"(": ")", "[": "]", "{": "}"}[opened_char]
                    self._add(self.brackets, offset, f"Mismatched bracket: expected '{wanted}' to close "
                              f"{opener_context(text, opened)} on line {line}, but found '{char}'", opened)
                if opened_char == "{":
                    closed = scopes.pop()
                    depths.pop()
                    scope = scopes[-1]
                    if closed.is_do:
                        pending_do = offset + 1
                    if closed.kind in ("class", "enum") and closed.type_name:
                        self._add(self.types, offset, closed.type_name)
                        scope.start = offset + 1
                    elif not closed.resume:
                        scope.start = offset + 1
                else:
                    depths[-1] = max(0, depths[-1] - 1)
                    if char == ")" and not depths[-1]:
                        if while_after_do == len(stack):
                            while_after_do = None
                            rest = re.compile(r'\s*').match(text, offset + 1).end()
                            if text[rest:rest + 1] != ";" and not self._crosses_directive(offset, min(rest, len(text) - 1)):
                                self._add(self.statements, offset + 1, "'while' in do-while loop", "error")
                            scopes[-1].start = offset + 1
                        elif _CONTROL_HEAD.search(text[max(0, opened - 64):opened]):
                            scopes[-1].start = offset + 1       # The controlled statement begins here.
        while candidate is not None:
            if not depths[-1]:
                self._judge(scopes[-1], candidate)
            candidate = next(candidates, None)
        for char, offset in reversed(stack):
            line, _ = self.position(offset)
            context = opener_context(text, offset)
            self._add(self.brackets, offset, {
                "{": f"{context[:1].upper() + context[1:]} opened on line {line} is never closed (missing '}}')",
                "(": f"Parenthesis for {context} opened on line {line} is never closed (missing ')')",
                "[": f"Square bracket '[' on line {line} is never closed (missing ']')"}[char], -1)
        return self

    def _separators(self, stack):
        """Whether a ; may appear here: for and init statements, or a macro's statement argument."""
        for index in range(len(stack) - 1, -1, -1):
            char, offset = stack[index]
            if char == "{":
                return False
            before = self.text[max(0, offset - 64):offset]
            name = _CALLEE.search(before)
            if name and (_MACRO.match(name[1]) or index == len(stack) - 1 and _CONTROL_HEAD.search(before)):
                return True
        return False

    def _unclosed(self, stack, depths):
        """Report and drop the parentheses and brackets still open in this block."""
        while stack and stack[-1][0] != "{":
            char, offset = stack.pop()
            line, _ = self.position(offset)
            context = opener_context(self.text, offset)
            self._add(self.brackets, offset, f"Parenthesis for {context} opened on line {line} is never closed (missing ')')"
                      if char == "(" else f"Square bracket '[' on line {line} is never closed (missing ']')", -1)
        depths[-1] = 0

    def _after_labels(self, start, end, blocks=False):
        """Labels and one-word openers introduce a statement; they do not end it."""
        while True:
            label = _LEADING_LABEL.match(self.text, start, end)
            if not label or label.end() <= start or blocks and label[0].strip() in ("else", "do", "try"):
                return start
            start = label.end()

    def _judge(self, scope, end):
        if scope.kind in ("init", "enum"):
            return
        text = self.text
        following = _NEXT.match(text, end)
        at_end = following is None and not text[end:].strip()
        if following is None and not at_end:
            return
        start = scope.start
        if end <= start or end - start > _MAX_SLICE * 4:
            return
        start = self._after_labels(start, end)
        start += len(text[start:end]) - len(text[start:end].lstrip())
        tokens = _TOKEN.findall(text[start:end])
        if not tokens:
            return
        if tokens[-1] == "}":
            # A value list or lambda ends like an operand; a finished block never reaches here.
            opened = _balanced_open(tokens, len(tokens) - 1)
            if opened <= 0:
                return
            tokens = tokens[:opened] + ["]"]
        if at_end:
            if len(tokens) > 1 and scope.kind == "file" and any(token in _ASSIGN for token in _depth_zero(tokens)) and (
                    _operand(tokens[-1]) and "operator" not in tokens):
                self._add(self.statements, end, "assignment", "error")
            return
        begin = following.start(1)
        if self._crosses_directive(start, end):
            return
        word = following.group(1)
        found = judge(scope.kind, tokens, word, self.language,
                      not _MACRO.match(word) and _DECLARES.match(text, begin) is not None,
                      _OBJECT_USE.match(text, following.end(1)) is not None)
        if found is not None and (found[1] == "error" or not self._crosses_directive(end, begin)):
            self._add(self.statements, end, *found)
            scope.start = end
