"""Shared, read-only C/C++ navigation for Monaco and the Qt5 sample viewer.

All disk reads and LLVM parsing run in a disposable, deadline-bound process.
Requests carry immutable buffers and the actual selected build context. This
module has no Qt imports and never starts a compiler, installer or language server.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from bisect import bisect_right
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.modules.sample_symbols import code_only, find_definitions, identifier_at, split_arguments

MAX_FILE = 4 * 1024**2
MAX_INPUT = 16 * 1024**2
MAX_HEADERS = 256
CONTEXT_NAME = "mcu-navigation.json"


@dataclass(frozen=True)
class Destination:
    path: str
    name: str
    line: int
    column: int
    label: str
    signature: str = ""
    end_line: int = -1
    end_column: int = -1


def _text(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE or b"\x00" in raw:
        raise ValueError("This source exceeds the preview limit or contains binary data.")
    return raw.decode("utf-8", errors="replace")


def _context(request):
    includes = [str(Path(value).absolute()) for value in request.get("include_roots", [])
                if isinstance(value, str) and Path(value).is_absolute()]
    flags, configured = [], False
    workspace = request.get("workspace")
    if workspace:
        path = Path(workspace) / ".pio/build/mcu_env" / CONTEXT_NAME
        try:
            value = json.loads(_text(path))
            if value.get("schema") == 1:
                includes += value.get("includes", [])
                flags += value.get("flags", [])
                flags += _compiler_target(value.get("compiler", ""))
                configured = True
        except (OSError, ValueError, TypeError):
            pass
    # An explicit compilation database can supply newer context without writing
    # or traversing generated directories. Never execute its compiler command.
    for base in (request.get("project"), workspace):
        if not base:
            continue
        try:
            entries = json.loads(_text(Path(base) / "compile_commands.json"))
            for entry in entries if isinstance(entries, list) else ():
                file = Path(entry.get("file", ""))
                directory = Path(entry.get("directory", base))
                file = file if file.is_absolute() else directory / file
                if str(file.absolute()) != request["path"]:
                    continue
                args = entry.get("arguments")
                if not isinstance(args, list):
                    # Command strings have shell/Windows-specific quoting;
                    # guessing that quoting would give the wrong include paths.
                    continue
                flags = _safe_flags(args[1:], directory)
                if not any(arg.startswith('--target=') or arg == '-target' for arg in flags):
                    flags += _compiler_target(args[0])
                configured = True
                break
        except (OSError, ValueError, TypeError, KeyError):
            pass
    includes = list(dict.fromkeys(value for value in includes
                                  if isinstance(value, str) and Path(value).is_absolute()))[:256]
    flags = _safe_flags(flags, Path(request.get("project") or Path(request["path"]).parent))
    for value in includes:
        flags += ["-I", value]
    return includes, flags, configured


def _compiler_target(compiler):
    # GCC's executable prefix is its declared target triple, not a board-name
    # guess. Unsupported LLVM targets fail parsing and retain the candidate UI.
    name = str(compiler).replace('\\', '/').rsplit('/', 1)[-1]
    match = re.fullmatch(r'([\w+.-]+)-(?:g\+\+|gcc)(?:\.exe)?', name)
    return ['--target=' + match[1]] if match else []


def _safe_flags(arguments, directory):
    """Keep real parsing options, excluding output, plugins and response files."""
    result, index = [], 0
    while index < min(len(arguments), 2048):
        arg = str(arguments[index])
        if arg in ("-I", "-isystem", "-iquote", "-include", "-isysroot", "--sysroot", "-D", "-U", "-x", "-target"):
            if index + 1 < len(arguments):
                value = str(arguments[index + 1])
                if arg not in ("-D", "-U", "-x", "-target"):
                    value = str(Path(value) if Path(value).is_absolute() else directory / value)
                result += [arg, value]
                index += 1
        elif arg.startswith(("-D", "-U", "-std=", "--target=", "-mcpu=", "-march=", "-mabi=", "-mfpu=", "-mfloat-abi=")):
            result.append(arg)
        elif arg in ("-fno-exceptions", "-fexceptions", "-fno-rtti", "-frtti", "-fshort-enums",
                     "-fshort-wchar", "-fsigned-char", "-funsigned-char", "-nostdinc", "-nostdinc++",
                     "-m32", "-m64"):
            result.append(arg)
        elif arg.startswith("-I") and len(arg) > 2:
            path = Path(arg[2:])
            result += ["-I", str(path if path.is_absolute() else directory / path)]
        index += 1
    return result


def _include_snapshots(snapshots, includes):
    files = dict(snapshots)
    queue = [(path, text, 0) for path, text in snapshots]
    total = sum(len(text) for text in files.values())
    while queue and len(files) < MAX_HEADERS and total < MAX_INPUT:
        path, text, depth = queue.pop(0)
        if depth >= 12:
            continue
        for match in re.finditer(r'^\s*#\s*include\s*([<"])([^>"\r\n]+)[>"]', text, re.M):
            name = match[2]
            if Path(name).is_absolute() or ".." in Path(name).parts:
                continue
            roots = ([str(Path(path).parent)] if match[1] == '"' else []) + includes
            for root in roots:
                candidate = Path(root) / name
                key = str(candidate.absolute())
                if key in files:
                    break
                try:
                    content = _text(candidate)
                except (OSError, ValueError):
                    continue
                if total + len(content) > MAX_INPUT:
                    break
                files[key] = content
                queue.append((key, content, depth + 1))
                total += len(content)
                break
    return tuple(files.items())


def _parenthesized(text, start):
    code = code_only(text)
    opening = start
    while opening < len(code) and code[opening].isspace():
        opening += 1
    if opening >= len(code) or code[opening] != '(':
        return None
    depth = 0
    for index in range(opening, min(len(code), opening + 16384)):
        depth += (code[index] == '(') - (code[index] == ')')
        if depth == 0:
            return text[opening + 1:index]
    return None


def _fallback(snapshots, path, offset, word):
    source = dict(snapshots)[path]
    start = offset
    while start > 0 and re.match(r'\w', source[start - 1]):
        start -= 1
    call = _parenthesized(source, start + len(word))
    arguments = split_arguments(call) if call is not None else None
    definitions = find_definitions(snapshots, word, path, offset, include_declarations=True)
    scopes = {}
    for name, text in snapshots:
        code = code_only(text)
        stack, markers = [], [(-1, (), False)]
        for brace in re.finditer(r'[{}]', code):
            if brace[0] == '{':
                prefix = code[max(0, brace.start() - 512):brace.start()]
                match = re.search(r'\b(namespace|class|struct)\s+([A-Za-z_]\w*(?:::\w+)*)[^;{}]*$', prefix)
                named = bool(match and (match[1] == 'namespace' or not re.search(r'[()]', prefix[match.end(2):])))
                stack.append((match[2], match[1]) if named else ('', ''))
            elif stack:
                stack.pop()
            markers.append((brace.start(), tuple(part for name, _ in stack for part in name.split('::') if part),
                            any(kind in ('class', 'struct') for _, kind in stack)))
        scopes[name] = ([item[0] for item in markers], markers, code)

    def scope_at(name, position):
        starts, markers, _ = scopes[name]
        return markers[bisect_right(starts, position) - 1][1:]

    before = scopes[path][2][:start].rstrip()
    qualifier = re.search(r'((?:[A-Za-z_]\w*::)+)$', before)
    qualified = qualifier[1][:-2] if qualifier else None
    member = before.endswith(('.', '->'))
    caller_scope, _ = scope_at(path, offset)
    unknown_using = bool(re.search(r'\busing\s+(?:namespace\b|[\w:]+::' + re.escape(word) + r'\b)', scopes[path][2]))
    result = []
    for item in definitions:
        text = dict(snapshots)[item.path]
        position = sum(len(line) for line in text.splitlines(keepends=True)[:item.line]) + item.column
        scope, in_class = scope_at(item.path, position)
        prefix = scopes[item.path][2][max(0, position - 256):position].rstrip()
        declared = re.search(r'((?:[A-Za-z_]\w*::)+)$', prefix)
        scope = (*scope, *(declared[1][:-2].split('::') if declared else ()))
        if item.label != 'macro':
            if qualified is not None and '::'.join(scope) != qualified:
                continue
            if member and not in_class and not declared:
                continue
            if qualified is None and not member and not unknown_using:
                if tuple(scope) not in [caller_scope[:count] for count in range(len(caller_scope) + 1)]:
                    continue
        parameters = _parenthesized(text, position + len(word)) if item.label.startswith('function') else None
        parts = split_arguments(parameters) if parameters is not None else None
        if arguments is not None and parts is not None:
            if parts == ['void']:
                parts = []
            minimum = sum('=' not in code_only(part) and '...' not in part for part in parts)
            variadic = bool(parts and '...' in parts[-1])
            if len(arguments) < minimum or (not variadic and len(arguments) > len(parts)):
                continue
        label = '::'.join((*scope, word))
        signature = f"{label}({parameters.strip()})" if parameters is not None else item.label
        result.append(Destination(item.path, word, item.line, item.column, item.label, signature,
                                  item.line, item.column + len(word)))
    # A declaration and a body with the identical signature describe one target.
    bodies = {item.signature for item in result if item.label == 'function'}
    return ([item for item in result if item.label != 'function declaration' or item.signature not in bodies],
            member or unknown_using)


def _semantic(snapshots, path, offset, flags, configured):
    try:
        from clang.cindex import Cursor, Diagnostic, Index, TranslationUnit
    except (ImportError, OSError):
        return []
    source = dict(snapshots)[path]
    # Without real preprocessor context an inactive branch could look valid.
    if not configured and any(re.search(r'^\s*#\s*(if|ifdef|ifndef|elif)\b', text, re.M)
                              for _, text in snapshots):
        return []
    language = 'c' if Path(path).suffix.lower() == '.c' else 'c++'
    options = ["-x", language]
    if language == 'c++' and not any(arg.startswith('-std=') for arg in flags):
        options.append('-std=gnu++17')
    unit = Index.create().parse(path, args=options + flags, unsaved_files=list(snapshots),
        options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
    # Never trust a recovered AST to select an overload after missing headers,
    # unknown target flags, unresolved types or an ambiguous call.
    if any(diagnostic.severity >= Diagnostic.Error for diagnostic in unit.diagnostics):
        return []
    prefix = source[:offset]
    line = prefix.count('\n') + 1
    column = len(prefix.rsplit('\n', 1)[-1].encode('utf-8')) + 1
    cursor = Cursor.from_location(unit, unit.get_location(path, (line, column)))
    target = cursor.get_definition() or cursor.referenced
    if target is None or not target.location.file or not target.spelling:
        return []
    location = target.location
    destination = str(Path(location.file.name).absolute())
    text = dict(snapshots).get(destination)
    if text is None:
        text = _text(destination)
    row = text.splitlines()[location.line - 1]
    col = len(row.encode('utf-8')[:location.column - 1].decode('utf-8', errors='ignore'))
    if row[col:col + len(target.spelling)] != target.spelling:
        return []
    return [Destination(destination, target.spelling, location.line - 1, col,
                        "definition" if target.is_definition() else "declaration", target.displayname,
                        location.line - 1, col + len(target.spelling))]


def resolve_request(request, *, semantic=True):
    path = str(Path(request["path"]).absolute())
    snapshots = tuple((str(Path(name).absolute()), text) for name, text in request["snapshots"]
                      if isinstance(name, str) and isinstance(text, str) and len(text) <= MAX_FILE)
    if sum(len(text) for _, text in snapshots) > MAX_INPUT or len(snapshots) > MAX_HEADERS:
        raise ValueError("The navigation snapshot exceeds its supported size.")
    source = dict(snapshots).get(path)
    if source is None:
        raise ValueError("The current source snapshot is unavailable.")
    offset = max(0, min(len(source), int(request["offset"])))
    word = identifier_at(source, offset)
    if not word:
        return {"success": True, "destinations": [], "word": ""}
    includes, flags, configured = _context(dict(request, path=path))
    # Quoted project includes are explicit sources; add only the current root.
    includes = list(dict.fromkeys([str(Path(path).parent), *includes]))
    snapshots = _include_snapshots(snapshots, includes)
    destinations, engine, requires_choice = [], "declarations", False
    if semantic:
        try:
            destinations = _semantic(snapshots, path, offset, flags, configured)
            if destinations:
                engine = "LLVM"
        except Exception:
            pass
    if not destinations:
        destinations, requires_choice = _fallback(snapshots, path, offset, word)
        requires_choice = requires_choice or any(
            re.search(r'^\s*#\s*(if|ifdef|ifndef|elif)\b', text, re.M) for _, text in snapshots)
    files = dict(snapshots)
    result = []
    for item in destinations[:64]:
        value = asdict(item)
        content = files.get(item.path)
        if content is None:
            content = _text(item.path)
        value["content"] = content
        value["read_only"] = str(Path(item.path).parent) != str(Path(request.get("project") or path).absolute())
        result.append(value)
    return {"success": True, "word": word, "destinations": result, "engine": engine,
            "requires_choice": requires_choice}


class NavigationResolver:
    def __init__(self, *, python_executable=None, timeout=8.0):
        self.python = str(python_executable or sys.executable)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._process = None
        self._closed = False

    def resolve(self, request):
        data = json.dumps(request, ensure_ascii=True)
        if len(data) > MAX_INPUT * 6:
            return {"success": False, "error": "The source snapshot is too large."}
        job, process = None, None
        with self._lock:
            if self._closed:
                return {"success": False, "cancelled": True}
            try:
                flags = 0
                if sys.platform == 'win32':
                    from src.modules.bootstrap_pip_monitor import _WindowsJob
                    job = _WindowsJob()
                    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_SUSPENDED
                process = subprocess.Popen([self.python, "-I", "-B", str(Path(__file__).absolute()), "--worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
                self._process = process
                if job is not None:
                    job.assign_and_resume(process)
            except OSError:
                if job is not None:
                    job.close()
                if process is not None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                self._process = None
                return {"success": False, "error": "Source navigation could not start its owned parser."}
        try:
            output, _errors = process.communicate(data.encode('utf-8'), timeout=self.timeout)
            if process.returncode or len(output) > MAX_INPUT * 6:
                return {"success": False, "error": "Source navigation could not parse this target."}
            return json.loads(output)
        except subprocess.TimeoutExpired:
            # Windows venv launchers can create a second Python process. The
            # job was assigned before resume, so closing it cancels only this
            # parser and its descendants, including inherited output pipes.
            if job is not None:
                job.close()
            else:
                process.kill()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # The worker still has a finite cleanup budget.
            return {"success": False, "error": "Source navigation reached its time limit. Try a smaller source selection."}
        except (OSError, ValueError):
            return {"success": False, "error": "Source navigation is unavailable for this request."}
        finally:
            if job is not None:
                job.close()
            with self._lock:
                if self._process is process:
                    self._process = None

    def close(self):
        with self._lock:
            self._closed = True
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT * 6 + 1)
        if len(raw) > MAX_INPUT * 6:
            raise ValueError("The navigation request is too large.")
        answer = resolve_request(json.loads(raw))
    except Exception as error:
        answer = {"success": False, "error": str(error)[:512]}
    print(json.dumps(answer, ensure_ascii=True))
