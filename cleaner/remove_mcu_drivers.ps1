[CmdletBinding()]
param([switch]$Apply, [switch]$DryRun, [switch]$Force,
      [switch]$ForceInUse, [switch]$NoPause, [string]$ExpectedPackages)

# Windows driver-store maintenance is separate from Ubuntu native USB access.
# Preview is read-only; in-use package removal requires an additional opt-in.
$script:McuDriverProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
function Get-McuDriverPackages([string]$Text) {
    foreach ($block in ($Text -split '(?:\r?\n){2,}')) {
        $names = @([regex]::Matches($block, '(?i)\b[a-z0-9_.-]+\.inf\b') |
            ForEach-Object { $_.Value.ToLowerInvariant() } | Select-Object -Unique)
        $published = @($names | Where-Object { $_ -match '^oem\d+\.inf$' })
        $original = @($names | Where-Object { $_ -match '^(silabser|slabvcp|ch341ser|ch343ser|wchusbserial|wchsser|arduino|genuino|linino)\.inf$' })
        if ($published.Count -eq 1 -and $original.Count -eq 1) {
            [pscustomobject]@{ Published = $published[0]; Original = $original[0] }
        }
    }
}

function Invoke-McuDriverTool([string[]]$Arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path ([Environment]::GetFolderPath('Windows')) 'System32/pnputil.exe'
    $info.Arguments = $Arguments -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    try {
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill()
            [void]$process.WaitForExit(3000)
            throw 'Driver-store command exceeded its time limit.'
        }
        $text = $output.GetAwaiter().GetResult() + $errors.GetAwaiter().GetResult()
        if ($text.Length -gt 4194304) { throw 'Driver-store output exceeded its supported limit.' }
        if ($process.ExitCode -ne 0) { throw "Driver-store command failed ($($process.ExitCode)): $text" }
        return $text
    } finally { $process.Dispose() }
}

function Invoke-McuDriverCleanup {
    param([switch]$Apply, [switch]$DryRun, [switch]$Force, [switch]$ForceInUse,
          [switch]$NoPause, [string]$ExpectedPackages)
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'This utility is for Windows drivers only.' }
    $packages = @(Get-McuDriverPackages (Invoke-McuDriverTool @('/enum-drivers')) | Sort-Object Published -Unique)
    Write-Host 'Windows MCU serial driver packages:'
    foreach ($package in $packages) { Write-Host "  $($package.Published): $($package.Original)" }
    $snapshot = ($packages | ForEach-Object { "$($_.Published):$($_.Original)" }) -join ';'
    if ($ExpectedPackages -and $snapshot -cne $ExpectedPackages) { throw 'Driver packages changed after confirmation. Nothing was removed.' }
    if (-not $packages.Count) { Write-Host 'No supported third-party driver packages were found.'; return }
    if (-not $Apply -or $DryRun) { Write-Host 'Preview only. Use -Apply to remove the listed drivers.'; return }
    if (-not (Get-Command Assert-McuWindowsIdle -ErrorAction SilentlyContinue)) {
        . (Join-Path $script:McuDriverProjectRoot 'cleaner/windows/maintenance.ps1') -ProjectRoot $script:McuDriverProjectRoot -NoPause
    }
    Assert-McuWindowsIdle -ProjectRoot $script:McuDriverProjectRoot
    Write-Host 'Removal affects every Windows application using these driver packages.'
    if (-not $Force -and (Read-Host 'Type REMOVE MCU DRIVERS to continue') -cne 'REMOVE MCU DRIVERS') {
        throw 'Cancelled. No drivers were removed.'
    }
    if (-not (Test-McuDriverAdministrator)) {
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Apply -Force -NoPause'
        $arguments += ' -ExpectedPackages "' + $snapshot + '"'
        if ($ForceInUse) { $arguments += ' -ForceInUse' }
        $child = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList $arguments -ErrorAction Stop
        if ($child.ExitCode -ne 0) { throw 'Driver removal did not complete. In-use drivers are retained unless -ForceInUse was selected.' }
        return
    }
    foreach ($package in $packages) {
        Assert-McuWindowsIdle -ProjectRoot $script:McuDriverProjectRoot
        if ($package.Published -notmatch '^oem\d+\.inf$') { throw 'Invalid captured driver package.' }
        $current = @(Get-McuDriverPackages (Invoke-McuDriverTool @('/enum-drivers')) |
            Where-Object { $_.Published -eq $package.Published -and $_.Original -eq $package.Original })
        if ($current.Count -ne 1) { throw 'A captured driver changed. Remaining packages were retained.' }
        $arguments = @('/delete-driver', $package.Published, '/uninstall')
        if ($ForceInUse) { $arguments += '/force' }
        Write-Host (Invoke-McuDriverTool $arguments)
    }
}

function Test-McuDriverAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-McuDriverCleanup -Apply:$Apply -DryRun:$DryRun -Force:$Force -ForceInUse:$ForceInUse -NoPause:$NoPause -ExpectedPackages $ExpectedPackages }
    catch { Write-Error $_ -ErrorAction Continue; exit 1 }
    finally { if (-not $NoPause) { [void](Read-Host 'Press Enter to close') } }
}
