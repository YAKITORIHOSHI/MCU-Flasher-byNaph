#!/usr/bin/env python3
"""Package committed sources, hydrated installers and the private Windows Python.

Uses only the standard library. Never cleans or modifies the working runtime,
settings, sketches or caches. Run with Python 3.10+ from either supported host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.venv-linux', '.venv', 'env', '_python', '.ubuntu-tools',
            '.mcu_flasher_build_cache', '.mcu_ai_edits', '.pio', '.vscode',
            '.clangd', '.opencode', 'temp', 'logs', 'test', 'tests', '__pycache__'}
STATE_NAMES = {'.mcu_gui_config.json', 'gui_config.json', 'dbs_notif.json',
               'arduino_browser_settings.json', 'arduino_cli_path.txt',
               '.owner_config.json', '.owner_tickets.json', '.mcu_cloud_link.json'}
# The current Python application is the shipping product. The Rust prototype
# and its audit evidence remain available in the repository, outside its ZIP.
DEVELOPER_ROOTS = frozenset({'desktop', 'audit'})
REQUIRED_RUNTIME = ('python.exe', 'pythonw.exe', 'python3.dll', 'python314.dll',
                    'vcruntime140.dll', 'vcruntime140_1.dll', 'Lib/os.py',
                    'Lib/encodings/__init__.py', 'Lib/site.py',
                    'Lib/ensurepip/__init__.py', 'Lib/venv/__init__.py',
                    'Lib/tkinter/__init__.py', 'DLLs/_tkinter.pyd',
                    'DLLs/tcl90.dll', 'DLLs/tcl9tk90.dll',
                    'tcl/libtcl9.0.4.zip', 'tcl/libtk9.0.4.zip')


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def plain_path(path):
    attributes = path.lstat()
    if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, 'st_reparse_tag', 0):
        raise RuntimeError(f'Redirected release input refused: {path}')
    return attributes


def include_committed_source(name):
    """Classify archive entries without silently hiding accidental user state."""
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise RuntimeError(f'Unsafe source entry: {path}')
    if path.parts and path.parts[0] in DEVELOPER_ROOTS:
        return False
    if any(part in EXCLUDED for part in path.parts):
        raise RuntimeError(f'Generated or protected source entry: {path}')
    if path.name in STATE_NAMES or path.suffix in ('.pyc', '.pyo'):
        raise RuntimeError(f'Personal state in committed sources: {path}')
    return True


def windows_runtime_files():
    """Inventory the whole private distribution without following redirected paths."""
    runtime = ROOT / 'src/_python'
    for path in (ROOT / 'src', runtime):
        if not stat.S_ISDIR(plain_path(path).st_mode):
            raise RuntimeError(f'Private Windows runtime directory is missing: {path}')
    result = []
    def traversal_error(error):
        raise error

    for folder, directories, names in os.walk(runtime, followlinks=False, onerror=traversal_error):
        for name in sorted(directories):
            plain_path(Path(folder) / name)
        for name in sorted(names):
            path = Path(folder) / name
            attributes = plain_path(path)
            if not stat.S_ISREG(attributes.st_mode):
                raise RuntimeError(f'Nonregular runtime file refused: {path}')
            result.append(path)
    relative = {path.relative_to(runtime).as_posix() for path in result}
    missing = set(REQUIRED_RUNTIME) - relative
    if missing:
        raise RuntimeError(f'Incomplete private Windows runtime: {sorted(missing)}')
    for name in ('python.exe', 'pythonw.exe', 'python3.dll', 'python314.dll'):
        with (runtime / name).open('rb') as stream:
            header = stream.read(64)
            if header[:2] != b'MZ' or len(header) != 64:
                raise RuntimeError(f'Invalid Windows runtime executable: {name}')
            stream.seek(int.from_bytes(header[60:64], 'little'))
            pe = stream.read(6)
            if pe[:4] != b'PE\0\0' or int.from_bytes(pe[4:6], 'little') != 0x8664:
                raise RuntimeError(f'Windows runtime must be x64: {name}')
    return sorted(result)


def build(output, report_dir):
    runtime_files = windows_runtime_files()  # Refuse omission before creating any ZIP.
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()
    prefix = ROOT.name + '/'
    output = output or ROOT / f'MCU-Flasher-{commit[:7]}-fresh.zip'
    if output.exists():
        raise FileExistsError(f'Release output already exists: {output}')
    report_dir.mkdir(parents=True, exist_ok=True)
    entries, installers, normalizations, seen = [], [], [], set()
    omissions = {name: {'committed_files': 0, 'committed_bytes': 0}
                 for name in sorted(DEVELOPER_ROOTS)}

    def info_for(name, mode):
        if name.casefold() in seen:
            raise RuntimeError(f'Windows case collision in release: {name}')
        seen.add(name.casefold())
        info = zipfile.ZipInfo(prefix + name, (2026, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | mode) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        return info

    def add_local(bundle, path, mode, expected=None):
        name = path.relative_to(ROOT).as_posix()
        before = plain_path(path)
        digest = hashlib.sha256()
        size = 0
        with path.open('rb') as source, bundle.open(info_for(name, mode), 'w', force_zip64=True) as dest:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
                dest.write(chunk)
                size += len(chunk)
        after = plain_path(path)
        fingerprint = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if fingerprint(before) != fingerprint(after) or size != before.st_size:
            raise RuntimeError(f'Release input changed during packaging: {name}')
        if expected and (digest.hexdigest(), size) != expected:
            raise RuntimeError(f'Installer does not match its committed LFS receipt: {name}')
        entry = {'path': name, 'sha256': digest.hexdigest(), 'size': size, 'mode': oct(mode)}
        entries.append(entry)
        return entry

    with tempfile.TemporaryDirectory(prefix='release-', dir=report_dir) as staging:
        temporary = Path(staging) / 'release.zip'
        process = subprocess.Popen(['git', 'archive', '--format=tar', commit], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|') as archive, zipfile.ZipFile(
                    temporary, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                for member in archive:
                    path = PurePosixPath(member.name)
                    if not include_committed_source(member.name):
                        if member.isfile():
                            omissions[path.parts[0]]['committed_files'] += 1
                            omissions[path.parts[0]]['committed_bytes'] += member.size
                        continue
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise RuntimeError(f'Nonregular committed file: {path}')
                    data = archive.extractfile(member).read()
                    mode = 0o755 if member.mode & 0o111 else 0o644
                    if data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                        match = re.fullmatch(rb'version https://git-lfs.github.com/spec/v1\n'
                                             rb'oid sha256:([a-f0-9]{64})\nsize ([0-9]+)\n', data)
                        if match is None or path.parts[0] != 'installers':
                            raise RuntimeError(f'Unrecognized LFS asset: {path}')
                        installers.append(add_local(bundle, ROOT / member.name, mode,
                                                    (match[1].decode(), int(match[2]))))
                    else:
                        if path.suffix == '.vbs':
                            data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
                            normalizations.append(member.name)
                        bundle.writestr(info_for(member.name, mode), data)
                        entries.append({'path': member.name, 'sha256': hashlib.sha256(data).hexdigest(),
                                        'size': len(data), 'mode': oct(mode)})
                print(f'Adding {len(runtime_files)} private Windows runtime files...', flush=True)
                for path in runtime_files:
                    add_local(bundle, path, 0o644)
            if process.wait(timeout=30):
                raise RuntimeError(process.stderr.read().decode(errors='replace'))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()

        print('Verifying every archived file and the native launchers...', flush=True)
        with zipfile.ZipFile(temporary) as bundle:
            if len(bundle.infolist()) != len(entries):
                raise RuntimeError('Unexpected release entry count.')
            for entry in entries:
                digest = hashlib.sha256()
                with bundle.open(prefix + entry['path']) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                if digest.hexdigest() != entry['sha256']:
                    raise RuntimeError(f"Archive bytes changed: {entry['path']}")
            for name, cpu in (('MCU_Flasher', 62), ('MCU_Flasher-arm64', 183)):
                info = bundle.getinfo(prefix + name)
                raw = bundle.read(info)
                if (raw[:6] != b'\x7fELF\x02\x01' or int.from_bytes(raw[18:20], 'little') != cpu
                        or (info.external_attr >> 16) & 0o111 != 0o111):
                    raise RuntimeError(f'Invalid native Ubuntu launcher: {name}')
            if not bundle.read(prefix + 'MCU_Flasher.exe').startswith(b'MZ'):
                raise RuntimeError('Missing Windows launcher.')
            if json.loads(bundle.read(prefix + 'src/dbs/bootstrap_config.json')) != {'skip_updates': False}:
                raise RuntimeError('The committed Bootstrap defaults are not fresh.')
        if runtime_files != windows_runtime_files():
            raise RuntimeError('Private Windows runtime inventory changed during packaging.')
        runtime_entries = {item['path']: item for item in entries if item['path'].startswith('src/_python/')}
        for path in runtime_files:
            if digest_file(path) != runtime_entries[path.relative_to(ROOT).as_posix()]['sha256']:
                raise RuntimeError(f'Private Windows runtime changed during verification: {path}')
        temporary.chmod(0o644)
        # Exclusive creation also works across drives and on exFAT. If publication
        # fails, remove only the partial file this attempt actually created.
        identity = None
        try:
            with output.open('xb') as destination, temporary.open('rb') as source:
                identity = os.fstat(destination.fileno())
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            if digest_file(output) != digest_file(temporary):
                raise RuntimeError('Release output changed during publication.')
        except BaseException:
            if identity is not None:
                try:
                    if os.path.samestat(identity, output.lstat()):
                        output.unlink()
                except FileNotFoundError:
                    pass
            raise

    manifest = {'commit': commit, 'archive': str(output), 'bytes': output.stat().st_size,
                'sha256': digest_file(output), 'files': len(entries),
                'windows_private_runtime_files': len(runtime_files),
                'windows_private_runtime_bytes': sum(item['size'] for item in runtime_entries.values()),
                'verified_lfs_installers': installers, 'windows_crlf_entries': normalizations,
                'omitted_developer_roots': omissions,
                'ubuntu_launcher_cpu_and_permissions_verified': True,
                'bootstrap_default': {'skip_updates': False}, 'entries': entries}
    (report_dir / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in manifest.items() if key != 'entries'}, indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New ZIP path; existing files are never overwritten.')
    parser.add_argument('--report-dir', type=Path, default=ROOT / 'temp/audit/release',
                        help='Audit/scratch directory; defaults to temp/audit/release.')
    args = parser.parse_args()
    build(args.output.absolute() if args.output else None, args.report_dir.absolute())


if __name__ == '__main__':
    main()
