#!/usr/bin/env python3
"""Build only the matching host's small LAUNCHER executables; never run setup."""
from pathlib import Path
import argparse
import os
import platform
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'LAUNCHER')
    args = parser.parse_args()
    output = args.output.resolve(strict=True)
    machine = platform.machine().lower()
    if platform.system() == 'Windows':
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
        if not compiler.is_file():
            raise SystemExit('The Windows .NET Framework compiler is unavailable; no runtime is installed by this builder.')
        with tempfile.TemporaryDirectory(prefix='launcher-build-', dir=output) as folder:
            target = Path(folder) / 'WINDOWS.exe'
            subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:anycpu', '/optimize+',
                            '/r:System.Windows.Forms.dll', '/r:System.dll',
                            '/win32icon:' + str(ROOT / 'src/assets/mcu_icon.ico'),
                            '/out:' + str(target), str(ROOT / 'src/launcher_folder_windows.cs')], check=True)
            target.replace(output / target.name)
    elif platform.system() == 'Linux' and machine in ('x86_64', 'amd64', 'aarch64', 'arm64'):
        arm = machine in ('aarch64', 'arm64')
        compiler = shutil.which(os.environ.get('CC', 'cc'))
        if compiler is None:
            raise SystemExit('A native C compiler is required to rebuild; this builder installs nothing.')
        names = ('UBUNTU_ARM64', 'FEDORA_ARM64') if arm else ('UBUNTU', 'FEDORA', 'LINUX_MINT')
        with tempfile.TemporaryDirectory(prefix='launcher-build-', dir=output) as folder:
            target = Path(folder) / 'launcher'
            subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Wpedantic', '-Werror',
                            '-Wformat=2', '-Wwrite-strings', '-D_FORTIFY_SOURCE=2',
                            '-fPIE', '-pie', '-Wl,-z,relro,-z,now', '-Wl,--build-id=none',
                            str(ROOT / 'src/launcher_folder_linux.c'), '-o', str(target)], check=True)
            for name in names:
                destination = Path(folder) / name
                shutil.copyfile(target, destination)
                destination.chmod(0o755)
                destination.replace(output / name)
    else:
        raise SystemExit('Build Linux executable entries on Linux AMD64/ARM64, or WINDOWS.exe on Windows.')


if __name__ == '__main__':
    main()
