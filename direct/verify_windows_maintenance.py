"""Exercise maintenance only against disposable fixtures; never run a live reset."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which('powershell.exe')


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


class ShellMaintenanceChecks(unittest.TestCase):
    def test_native_shell_wrappers_parse_without_running(self):
        if sys.platform == 'win32':
            git = shutil.which('git')
            candidate = Path(git).parent.parent / 'bin/bash.exe' if git else None
            bash = str(candidate) if candidate and candidate.is_file() else None
        else:
            bash = shutil.which('bash')
        if not bash:
            self.skipTest('Bash unavailable for syntax-only verification')
        for relative in ('cleaner/clean_fresh.sh', 'cleaner/clean_pycache.sh', 'DANGER-ZONE/reset_ubuntu.sh'):
            path = ROOT / relative
            self.assertNotIn(b'\r', path.read_bytes())
            result = subprocess.run([bash, '-n', str(path)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))


@unittest.skipUnless(sys.platform == 'win32' and SHELL, 'Native Windows PowerShell fixtures')
class WindowsMaintenanceChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'temp' / 'audit' / 'maintenance'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="app '! ", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write('mcu_flash_gui.py', '# fixture')
        self.write('direct/windows/run.vbs', "' fixture")

    def write(self, relative, content='sentinel'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def run_ps(self, body, *, mock_idle=True):
        script = self.root / 'probe.ps1'
        imports = f". {literal(ROOT / 'cleaner/windows/maintenance.ps1')} -ProjectRoot {literal(self.root)} -NoPause\n"
        if mock_idle:
            imports += 'function Get-McuOwnedProcesses([string]$ProjectRoot) { return @() }\n'
        script.write_text("$ErrorActionPreference = 'Stop'\n" + imports + body + '\n', encoding='utf-8-sig')
        result = subprocess.run([SHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_powershell_sources_parse_without_invoking_them(self):
        paths = list((ROOT / 'cleaner').rglob('*.ps1')) + list((ROOT / 'DANGER-ZONE').glob('*.ps1'))
        array = ', '.join(literal(path) for path in paths)
        self.run_ps(f"foreach ($path in @({array})) {{ $tokens=$null; $errors=$null; [void][Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors); if ($errors.Count) {{ throw ($errors | Out-String) }} }}")

    def test_danger_previews_never_probe_repair_elevate_or_write_settings(self):
        destination = self.root / 'DANGER-ZONE'
        destination.mkdir()
        for name in ('DELETE_EVERYTHING_DO_NOT_RUN.ps1', 'PC_Crash_Diagnostic_and_Fix.ps1'):
            shutil.copyfile(ROOT / 'DANGER-ZONE' / name, destination / name)
        helper = self.root / 'cleaner/windows/maintenance.ps1'
        helper.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'cleaner/windows/maintenance.ps1', helper)
        settings = self.write('src/dbs/bootstrap_config.json', '{"skip_updates":true}')
        before = settings.read_bytes()
        for name, arguments in (('DELETE_EVERYTHING_DO_NOT_RUN.ps1', ['-FullReset']),
                                ('PC_Crash_Diagnostic_and_Fix.ps1', [])):
            script = destination / name
            prefix = "function Start-Process { throw 'Preview must not elevate or start helpers' }; function Get-CimInstance { throw 'Preview must not probe processes' }; function Get-ItemProperty { throw 'Preview must not probe registry' }; function Set-Content { throw 'Preview must not write settings' }; function Add-Content { throw 'Preview must not write reports' }; function New-Item { throw 'Preview must not create files' };\n"
            script.write_text(prefix + script.read_text(encoding='utf-8-sig'), encoding='utf-8-sig')
            # Param must remain the first statement; inject mocks just after it.
            text = script.read_text(encoding='utf-8-sig')
            text = text[len(prefix):]
            end = text.index('\n)') + len('\n)')
            script.write_text(text[:end] + '\n' + prefix + text[end:], encoding='utf-8-sig')
            result = subprocess.run([SHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                     str(script), '-NoPause', *arguments], capture_output=True, text=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(settings.read_bytes(), before)
        self.assertFalse((self.root / 'temp').exists())

    def test_preview_is_read_only_even_with_force_and_settings_present(self):
        settings = self.write('src/dbs/bootstrap_config.json', '{"skip_updates":true,"offline_enabled":true}')
        bytecode = self.write('main/__pycache__/stale.pyc')
        before = {path: path.read_bytes() for path in (settings, bytecode)}
        self.run_ps('Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Fresh -Force -DryRun')
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_bytecode_cleanup_prunes_protected_resources_and_unknown_cache_files(self):
        removed = [self.write('main/__pycache__/stale.pyc'), self.write('direct/__pycache__/stale.pyo'),
                   self.write('root.pyc')]
        preserved = [self.write(relative) for relative in (
            '.mcu_flasher_build_cache/__pycache__/keep.pyc', 'main/.mcu_ai_edits/keep.pyc',
            '.pio/keep.pyc', 'src/_python/keep.pyc', '.venv-linux/keep.pyc', 'src/offline-extras/linux/keep.pyc',
            'main/__pycache__/user-note.txt', 'sketch.ino', 'logs/user-note.txt')]
        self.run_ps('Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Bytecode -Apply -Force')
        self.assertTrue(all(not path.exists() for path in removed))
        self.assertTrue(all(path.exists() for path in preserved))
        self.assertTrue((self.root / 'main/__pycache__').is_dir())
        self.assertFalse((self.root / 'direct/__pycache__').exists())

    def test_fresh_preserves_private_python_ubuntu_and_all_settings(self):
        self.write('env/pyvenv.cfg')
        self.write('env/Scripts/python.exe')
        self.write('src/.platformio-mcu-gui/packages/tool/package.json')
        preserved = [self.write(relative) for relative in (
            'src/_python/python.exe', '.venv-linux/bin/python', '.ubuntu-tools/arduino-cli/arduino-cli',
            'src/offline-extras/linux/readiness.json', 'src/gui_config.json', 'src/dbs/bootstrap_config.json',
            '.mcu_flasher_build_cache/.mcu_ai_edits/session/edit.txt', 'logs/session_backup.json')]
        self.run_ps('Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Fresh -Apply -Force')
        self.assertFalse((self.root / 'env').exists())
        self.assertFalse((self.root / 'src/.platformio-mcu-gui').exists())
        self.assertTrue(all(path.exists() for path in preserved))

    def test_runtime_removes_only_private_windows_runtime(self):
        self.write('src/_python/python.exe')
        protected = self.write('.venv-linux/bin/python')
        self.run_ps('Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Runtime -Apply -Force')
        self.assertFalse((self.root / 'src/_python').exists())
        self.assertTrue(protected.exists())

    def test_invalid_environment_refuses_entire_plan_before_deleting_bytecode(self):
        retained = self.write('env/user-sketch.ino')
        bytecode = self.write('main/stale.pyc')
        self.run_ps("$refused=$false; try { Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Fresh -Apply -Force } catch { $refused=$true }; if (-not $refused) { throw 'Invalid environment was accepted' }")
        self.assertTrue(retained.exists())
        self.assertTrue(bytecode.exists())

    def test_owned_process_blocks_cleanup_without_stopping_it(self):
        retained = self.write('main/stale.pyc')
        self.run_ps("function Get-McuOwnedProcesses([string]$ProjectRoot) { return @([pscustomobject]@{ ProcessId=777; Name='fixture' }) }; function Stop-Process { throw 'Must not stop a live process' }; $refused=$false; try { Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Bytecode -Apply -Force } catch { $refused=$true }; if (-not $refused) { throw 'Live process was accepted' }")
        self.assertTrue(retained.exists())

    def test_ownership_changed_during_confirmation_refuses_before_deletion(self):
        self.write('env/pyvenv.cfg')
        executable = self.write('env/Scripts/python.exe')
        bytecode = self.write('main/stale.pyc')
        self.run_ps("function Read-Host { Remove-Item -LiteralPath (Join-Path $ProjectRoot 'env/pyvenv.cfg'); return 'CLEAN MCU WINDOWS' }; $refused=$false; try { Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Fresh -Apply } catch { $refused=$true }; if (-not $refused) { throw 'Changed ownership was accepted' }")
        self.assertTrue(executable.exists())
        self.assertTrue(bytecode.exists())

    def test_scan_budget_failure_does_not_apply_a_partial_plan(self):
        retained = self.write('main/stale.pyc')
        self.run_ps("function New-McuBudget([int]$Seconds) { return [pscustomobject]@{ Remaining=0; Deadline=[DateTime]::UtcNow.AddSeconds(30) } }; $refused=$false; try { Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Bytecode -Apply -Force } catch { $refused=$true }; if (-not $refused) { throw 'Exhausted inventory budget accepted' }")
        self.assertTrue(retained.exists())

    def test_junction_root_and_nested_links_never_delete_external_content(self):
        external = self.write('external/user-sketch.ino')
        self.write('env/pyvenv.cfg')
        self.write('env/Scripts/python.exe')
        self.run_ps("[void](New-Item -ItemType Junction -Path (Join-Path $ProjectRoot 'env/linked') -Target (Join-Path $ProjectRoot 'external')); Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Fresh -Apply -Force")
        self.assertTrue(external.exists())
        self.assertFalse((self.root / 'env').exists())
        self.run_ps("[void](New-Item -ItemType Junction -Path (Join-Path $ProjectRoot 'env') -Target (Join-Path $ProjectRoot 'external')); $refused=$false; try { Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode Fresh -Apply -Force } catch { $refused=$true }; if (-not $refused) { throw 'Linked root accepted' }; [IO.Directory]::Delete((Join-Path $ProjectRoot 'env'),$false)")
        self.assertTrue(external.exists())

    def test_aliases_are_authenticated_and_only_unlinked(self):
        package = self.write('src/.platformio-mcu-gui/packages/keep.json')
        external = self.write('external/keep.json')
        self.run_ps("$script:fixtureAliasBase=Join-Path $ProjectRoot 'aliases'; [void](New-Item -ItemType Directory -Path $script:fixtureAliasBase); function Get-McuAliasBases([string]$ProjectRoot) { return @($script:fixtureAliasBase) }; [void](New-Item -ItemType Junction -Path (Join-Path $script:fixtureAliasBase '.platformio-mcu-gui') -Target (Join-Path $ProjectRoot 'external')); if (@(Get-McuWindowsAliases $ProjectRoot).Count) { throw 'Foreign alias was accepted' }; [IO.Directory]::Delete((Join-Path $script:fixtureAliasBase '.platformio-mcu-gui'),$false); [void](New-Item -ItemType Junction -Path (Join-Path $script:fixtureAliasBase '.platformio-mcu-gui') -Target (Join-Path $ProjectRoot 'src/.platformio-mcu-gui')); if (@(Get-McuWindowsAliases $ProjectRoot).Count -ne 1) { throw 'Owned alias was missed' }; $entry=@(Get-McuWindowsAliases $ProjectRoot)[0]; Assert-McuParents $entry.Path $entry.Root; [IO.Directory]::Delete($entry.Path,$false)")
        self.assertTrue(package.exists())
        self.assertTrue(external.exists())

    def test_drive_root_alias_boundary_is_normalized_without_changing_drive(self):
        self.run_ps("$drive=[IO.Path]::GetPathRoot($ProjectRoot); $path=Join-Path $drive ('mcu-nonexistent-fixture-' + [guid]::NewGuid().ToString() + '/entry'); if (-not (Test-McuWithin $path $drive)) { throw 'Drive containment failed' }; Assert-McuParents $path $drive; if ((Get-McuFullPath $drive) -ne $drive) { throw 'Drive root became drive-relative' }")

    def test_stop_scope_requires_owned_entry_and_excludes_unrelated_root_arguments(self):
        self.run_ps("$other='C:\\Other\\launcher.py'; $unrelated='python.exe \"' + $other + '\" --project \"' + $ProjectRoot + '\"'; if (Test-McuOwnedCommand $unrelated $ProjectRoot) { throw 'Unrelated script was classified as owned' }; $own='python.exe -B \"' + (Join-Path $ProjectRoot 'main/mcu_flash_gui.py') + '\"'; if (-not (Test-McuOwnedCommand $own $ProjectRoot)) { throw 'Owned entry was missed' }; $argument='python.exe C:\\Other\\script.py --target \"' + (Join-Path $ProjectRoot 'main/mcu_flash_gui.py') + '\"'; if (Test-McuOwnedCommand $argument $ProjectRoot) { throw 'An unrelated argument was classified as the entry' }")

    def test_driver_matching_requires_exact_supported_inf_and_unambiguous_oem_name(self):
        driver = literal(ROOT / 'cleaner/remove_mcu_drivers.ps1')
        self.run_ps(f". {driver} -NoPause; $text='Published: oem12.inf`nOriginal: ch341ser.inf`n`nPublished: oem13.inf`nOriginal: usbser.inf`nProvider: Microsoft`n`nPublished: oem14.inf`nOriginal: unrelated-ch34.inf`nProvider: WCH'; $text=$text.Replace('`n', [Environment]::NewLine); $matches=@(Get-McuDriverPackages $text); if ($matches.Count -ne 1 -or $matches[0].Published -ne 'oem12.inf') {{ throw 'Driver package ownership was not exact' }}")

    def test_driver_apply_uses_revalidated_snapshot_and_does_not_force_in_use(self):
        driver = literal(ROOT / 'cleaner/remove_mcu_drivers.ps1')
        self.run_ps(f". {driver} -NoPause; function Test-McuDriverAdministrator {{ return $true }}; $script:deletes=@(); function Invoke-McuDriverTool([string[]]$Arguments) {{ if ($Arguments[0] -eq '/enum-drivers') {{ return \"Published: oem12.inf`nOriginal: ch341ser.inf\" }}; $script:deletes += ($Arguments -join ' '); return 'fixture success' }}; Invoke-McuDriverCleanup -Apply -Force -NoPause; if ($script:deletes.Count -ne 1 -or $script:deletes[0] -ne '/delete-driver oem12.inf /uninstall') {{ throw 'Forced or repeated driver deletion' }}")

    def test_changed_driver_snapshot_never_elevates_or_removes_any_package(self):
        driver = literal(ROOT / 'cleaner/remove_mcu_drivers.ps1')
        self.run_ps(f". {driver} -NoPause; function Start-Process {{ throw 'Must not elevate' }}; function Invoke-McuDriverTool([string[]]$Arguments) {{ if ($Arguments[0] -ne '/enum-drivers') {{ throw 'Must not delete' }}; return \"Published: oem13.inf`nOriginal: ch341ser.inf\" }}; $refused=$false; try {{ Invoke-McuDriverCleanup -Apply -Force -ExpectedPackages 'oem12.inf:ch341ser.inf' }} catch {{ $refused=$true }}; if (-not $refused) {{ throw 'Changed driver snapshot accepted' }}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
