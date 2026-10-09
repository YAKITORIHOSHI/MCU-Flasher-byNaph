[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$DryRun,
    [switch]$NoPause
)

# This is a Windows system repair utility, separate from MCU/Ubuntu resources.
# With no -Apply it lists the offered actions only: no probes, writes or UAC.
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This diagnostic is for Windows only. Use native Ubuntu diagnostics on Ubuntu."
}
if ($Apply -and $DryRun) { throw "Choose -Apply or -DryRun, not both." }
if (-not $Apply) {
    Write-Host "Windows Crash Diagnostic and Repair - PREVIEW" -ForegroundColor Cyan
    Write-Host "A confirmed -Apply run inspects crash events, disks, memory, drivers, thermal/power state, update health and reliability history."
    Write-Host "It also runs SFC/DISM, repairs detected update/network/WMI issues, adjusts selected system settings, and clears selected Windows caches."
    Write-Host "These repairs affect Windows and other applications. Reports go to temp\audit\crash-diagnostic in this project."
    Write-Host "No diagnostic, repair, report write or elevation was performed. Review this script before using -Apply."
    if (-not $NoPause) { [void](Read-Host "Press Enter to close") }
    exit 0
}

$ErrorActionPreference = "Stop"
$scriptDir = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $scriptDir))
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "mcu_flash_gui.py") -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $projectRoot "direct\windows\run.vbs") -PathType Leaf)) {
    throw "Run this diagnostic from the MCU Flasher DANGER-ZONE folder."
}
. (Join-Path $projectRoot "cleaner\windows\maintenance.ps1") -ProjectRoot $projectRoot -Mode Runtime -Apply:$Apply -DryRun:$DryRun -Force -NoPause:$NoPause
Assert-McuWindowsIdle -ProjectRoot $projectRoot
Write-Host "This run applies Windows system repairs and may require a restart." -ForegroundColor Yellow
if ((Read-Host "Type REPAIR WINDOWS to continue") -cne "REPAIR WINDOWS") {
    Write-Host "Cancelled. Nothing was changed." -ForegroundColor Yellow
    exit 2
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Requesting Administrator privileges. The elevated process will confirm again." -ForegroundColor Yellow
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Apply"
    if ($NoPause) { $arguments += " -NoPause" }
    try {
        $elevated = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -ArgumentList $arguments -ErrorAction Stop
        exit $elevated.ExitCode
    }
    catch { Write-Error "Could not start the elevated diagnostic: $_"; exit 1 }
}

# Do not stop installer/update services in the middle of somebody else's setup.
$installerMutex = $null
try { $installerMutex = [Threading.Mutex]::OpenExisting("Global\_MSIExecute") }
catch [Threading.WaitHandleCannotBeOpenedException] { }
if ($installerMutex) {
    $installerAcquired = $false
    try {
        try { $installerAcquired = $installerMutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $installerAcquired = $true }
        if (-not $installerAcquired) {
            throw "Windows Installer is busy. Finish the current install or uninstall before repairing Windows."
        }
    }
    finally {
        if ($installerAcquired) { $installerMutex.ReleaseMutex() }
        $installerMutex.Dispose()
    }
}

function Assert-DiagnosticNoLinks([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing a diagnostic path through a link or junction: $current"
            }
        }
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
}

<#
╔══════════════════════════════════════════════════════════════════════════════╗
║              PC CRASH DIAGNOSTIC & REPAIR TOOL v2.0                        ║
║              Created: 2026-09-24 | Emergency Fix Script                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  This script investigates WHY your PC keeps crashing and applies           ║
║  automated fixes. It checks:                                               ║
║                                                                            ║
║  [1]  BSOD / Blue Screen minidump analysis                                ║
║  [2]  Windows system file integrity (SFC + DISM)                          ║
║  [3]  Disk health (SMART, chkdsk, filesystem errors)                      ║
║  [4]  Memory diagnostics (RAM issues)                                     ║
║  [5]  Driver crash analysis (faulty drivers)                              ║
║  [6]  Critical Event Log errors (System, Application, Hardware)           ║
║  [7]  Windows Update health & pending repairs                             ║
║  [8]  Overheating / thermal throttling detection                          ║
║  [9]  Startup program overload analysis                                   ║
║  [10] Power settings & sleep/hibernate issues                             ║
║  [11] Disk space & pagefile health                                        ║
║  [12] Pending Windows reboots & stuck updates                             ║
║  [13] Reliability Monitor history summary                                 ║
║  [14] GPU driver crash detection                                          ║
║  [15] Antivirus conflict detection                                        ║
║                                                                            ║
║  ⚠️  Requires -Apply, typed confirmation and administrator access.        ║
║  💾  Reports: project temp/audit/crash-diagnostic.                          ║
╚══════════════════════════════════════════════════════════════════════════════╝
#>

# ─── Configuration ───────────────────────────────────────────────────────────
$ErrorActionPreference = "Continue"
$script:WindowsDirectory = [Environment]::GetFolderPath("Windows")
$script:WindowsSystemDrive = [IO.Path]::GetPathRoot($script:WindowsDirectory).TrimEnd('\')
$script:WindowsLocalAppData = [Environment]::GetFolderPath("LocalApplicationData")
$reportDirectory = Join-Path $projectRoot "temp\audit\crash-diagnostic"
Assert-DiagnosticNoLinks $reportDirectory
[void][IO.Directory]::CreateDirectory($reportDirectory)
Assert-DiagnosticNoLinks $reportDirectory
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$reportPath = Join-Path $reportDirectory "CrashReport_${timestamp}_$PID.txt"
$fixLogPath = Join-Path $reportDirectory "FixesApplied_${timestamp}_$PID.txt"
Assert-DiagnosticNoLinks $reportPath
Assert-DiagnosticNoLinks $fixLogPath

# ─── Helper Functions ────────────────────────────────────────────────────────

function Write-Banner {
    param([string]$Title)
    $line = "═" * 76
    Write-Host ""
    Write-Host "╔$line╗" -ForegroundColor Cyan
    Write-Host "║  $($Title.PadRight(74))║" -ForegroundColor Cyan
    Write-Host "╚$line╝" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Section {
    param([string]$Title)
    $line = "─" * 74
    $text = "`n┌$line┐`n│  $($Title.PadRight(72))│`n└$line┘"
    Write-Host $text -ForegroundColor Yellow
    Add-Content -Path $reportPath -Value $text
}

function Write-Finding {
    param(
        [string]$Status, # OK, WARN, FAIL, INFO, FIX
        [string]$Message
    )
    $color = switch ($Status) {
        "OK"   { "Green" }
        "WARN" { "Yellow" }
        "FAIL" { "Red" }
        "INFO" { "Cyan" }
        "FIX"  { "Magenta" }
        default { "White" }
    }
    $icon = switch ($Status) {
        "OK"   { "✅" }
        "WARN" { "⚠️ " }
        "FAIL" { "❌" }
        "INFO" { "ℹ️ " }
        "FIX"  { "🔧" }
        default { "  " }
    }
    $line = "  $icon [$Status] $Message"
    Write-Host $line -ForegroundColor $color
    Add-Content -Path $reportPath -Value $line
}

function Write-Detail {
    param([string]$Message)
    $line = "          $Message"
    Write-Host $line -ForegroundColor Gray
    Add-Content -Path $reportPath -Value $line
}

function Write-FixLog {
    param([string]$FixDescription)
    Add-Content -Path $fixLogPath -Value "[$(Get-Date -Format 'HH:mm:ss')] $FixDescription"
}

# ─── Start Report ────────────────────────────────────────────────────────────

Write-Banner "PC CRASH DIAGNOSTIC & REPAIR TOOL v2.0"
Write-Host "  Report will be saved to: $reportPath" -ForegroundColor DarkGray
Write-Host "  Started at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray
Write-Host ""

Set-Content -Path $reportPath -Value @"
╔══════════════════════════════════════════════════════════════════════════════╗
║                    PC CRASH DIAGNOSTIC REPORT                              ║
║                    Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')                           ║
║                    Computer:  $($env:COMPUTERNAME)                                        ║
║                    User:      $($env:USERNAME)                                            ║
╚══════════════════════════════════════════════════════════════════════════════╝
"@

Set-Content -Path $fixLogPath -Value "═══ FIXES APPLIED LOG ═══`nStarted: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`n"

$issuesFound = 0
$fixesApplied = 0

# ═════════════════════════════════════════════════════════════════════════════
# [1] SYSTEM OVERVIEW
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[1/15] SYSTEM OVERVIEW"

try {
    $os = Get-CimInstance Win32_OperatingSystem
    $cs = Get-CimInstance Win32_ComputerSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $bios = Get-CimInstance Win32_BIOS

    Write-Finding "INFO" "OS: $($os.Caption) Build $($os.BuildNumber)"
    Write-Finding "INFO" "CPU: $($cpu.Name)"
    Write-Finding "INFO" "RAM: $([math]::Round($cs.TotalPhysicalMemory / 1GB, 1)) GB"
    Write-Finding "INFO" "Last Boot: $($os.LastBootUpTime)"

    # Check uptime — long uptime can cause instability
    $uptime = (Get-Date) - $os.LastBootUpTime
    if ($uptime.TotalDays -gt 14) {
        Write-Finding "WARN" "System uptime is $([math]::Round($uptime.TotalDays, 1)) days — consider rebooting"
        $issuesFound++
    } else {
        Write-Finding "OK" "System uptime: $([math]::Round($uptime.TotalDays, 1)) days"
    }
} catch {
    Write-Finding "FAIL" "Could not retrieve system info: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [2] BSOD / BLUE SCREEN MINIDUMP ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[2/15] BSOD / BLUE SCREEN ANALYSIS"

$minidumpDir = "$script:WindowsDirectory\Minidump"
$memoryDmp = "$script:WindowsDirectory\MEMORY.DMP"

if (Test-Path $minidumpDir) {
    $dumps = Get-ChildItem -Path $minidumpDir -Filter "*.dmp" -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime -Descending |
             Select-Object -First 10

    if ($dumps.Count -gt 0) {
        Write-Finding "FAIL" "Found $($dumps.Count) BSOD minidump file(s) — your PC HAS been blue-screening!"
        $issuesFound++
        foreach ($d in $dumps) {
            Write-Detail "💀 $($d.Name) — $($d.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')) ($([math]::Round($d.Length/1KB, 1)) KB)"
        }
        Write-Finding "INFO" "Most recent BSOD: $($dumps[0].LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"

        # Check frequency
        $recentDumps = $dumps | Where-Object { $_.LastWriteTime -gt (Get-Date).AddDays(-7) }
        if ($recentDumps.Count -ge 3) {
            Write-Finding "FAIL" "$($recentDumps.Count) BSODs in the last 7 days — CRITICAL frequency!"
            $issuesFound++
        }
    } else {
        Write-Finding "OK" "Minidump folder exists but no dump files found"
    }
} else {
    Write-Finding "INFO" "No minidump folder found at $minidumpDir"
}

if (Test-Path $memoryDmp) {
    $dmpInfo = Get-Item $memoryDmp
    Write-Finding "WARN" "Full memory dump exists: $($dmpInfo.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')) ($([math]::Round($dmpInfo.Length/1GB, 2)) GB)"
    $issuesFound++
} else {
    Write-Finding "OK" "No full memory dump file found"
}

# Parse BugCheck from Event Log
Write-Finding "INFO" "Scanning Event Log for BugCheck (BSOD) entries..."
try {
    $bugChecks = Get-WinEvent -FilterHashtable @{
        LogName   = 'System'
        Id        = 1001
        ProviderName = 'Microsoft-Windows-WER-SystemErrorReporting'
    } -MaxEvents 10 -ErrorAction SilentlyContinue

    if ($bugChecks -and $bugChecks.Count -gt 0) {
        Write-Finding "FAIL" "Found $($bugChecks.Count) BugCheck (BSOD) event(s) in System log!"
        $issuesFound++
        foreach ($bc in $bugChecks | Select-Object -First 5) {
            Write-Detail "BSOD Event: $($bc.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'))"
            # Try to extract bugcheck code from message
            if ($bc.Message -match '(0x[0-9A-Fa-f]+)') {
                Write-Detail "  BugCheck Code: $($Matches[1])"
            }
            if ($bc.Message -match 'caused by[:\s]+(\S+)') {
                Write-Detail "  Caused by: $($Matches[1])"
            }
        }
    } else {
        Write-Finding "OK" "No BugCheck events found in System event log"
    }
} catch {
    Write-Finding "INFO" "Could not query BugCheck events: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [3] CRITICAL EVENT LOG ERRORS
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[3/15] CRITICAL EVENT LOG ERRORS (Last 48 Hours)"

$cutoff = (Get-Date).AddHours(-48)

# System log critical/error events
try {
    $sysErrors = Get-WinEvent -FilterHashtable @{
        LogName   = 'System'
        Level     = @(1, 2)  # Critical, Error
        StartTime = $cutoff
    } -MaxEvents 30 -ErrorAction SilentlyContinue

    if ($sysErrors -and $sysErrors.Count -gt 0) {
        Write-Finding "FAIL" "$($sysErrors.Count)+ critical/error events in System log (last 48h)"
        $issuesFound++

        # Group by source
        $grouped = $sysErrors | Group-Object ProviderName | Sort-Object Count -Descending | Select-Object -First 8
        foreach ($g in $grouped) {
            Write-Detail "$($g.Count)x from [$($g.Name)]"
            $sample = $g.Group | Select-Object -First 1
            $msgPreview = ($sample.Message -split "`n")[0]
            if ($msgPreview.Length -gt 80) { $msgPreview = $msgPreview.Substring(0, 80) + "..." }
            Write-Detail "  Latest: $msgPreview"
        }

        # Specifically look for disk, driver, and power errors
        $diskErrors = $sysErrors | Where-Object { $_.ProviderName -match "disk|ntfs|storage|volmgr" }
        if ($diskErrors.Count -gt 0) {
            Write-Finding "FAIL" "$($diskErrors.Count) DISK-related errors — possible failing drive!"
            $issuesFound++
        }

        $driverErrors = $sysErrors | Where-Object { $_.ProviderName -match "driver|kernel|wdf" }
        if ($driverErrors.Count -gt 0) {
            Write-Finding "WARN" "$($driverErrors.Count) DRIVER-related errors detected"
            $issuesFound++
        }

        $powerErrors = $sysErrors | Where-Object { $_.ProviderName -match "power|kernel-power" }
        if ($powerErrors.Count -gt 0) {
            Write-Finding "FAIL" "$($powerErrors.Count) POWER errors — unexpected shutdowns/restarts!"
            $issuesFound++
        }
    } else {
        Write-Finding "OK" "No critical/error events in System log (last 48h)"
    }
} catch {
    Write-Finding "INFO" "Could not query System event log: $_"
}

# Application log errors
try {
    $appErrors = Get-WinEvent -FilterHashtable @{
        LogName   = 'Application'
        Level     = @(1, 2)
        StartTime = $cutoff
    } -MaxEvents 20 -ErrorAction SilentlyContinue

    if ($appErrors -and $appErrors.Count -gt 0) {
        Write-Finding "WARN" "$($appErrors.Count)+ application errors in last 48h"
        $grouped = $appErrors | Group-Object ProviderName | Sort-Object Count -Descending | Select-Object -First 5
        foreach ($g in $grouped) {
            Write-Detail "$($g.Count)x from [$($g.Name)]"
        }
    } else {
        Write-Finding "OK" "No critical application errors (last 48h)"
    }
} catch {
    Write-Finding "INFO" "Could not query Application event log: $_"
}

# Unexpected shutdowns (Event ID 6008)
try {
    $unexpectedShutdowns = Get-WinEvent -FilterHashtable @{
        LogName = 'System'
        Id      = 6008
    } -MaxEvents 10 -ErrorAction SilentlyContinue

    if ($unexpectedShutdowns -and $unexpectedShutdowns.Count -gt 0) {
        Write-Finding "FAIL" "$($unexpectedShutdowns.Count) UNEXPECTED SHUTDOWN(S) recorded!"
        $issuesFound++
        foreach ($us in $unexpectedShutdowns | Select-Object -First 5) {
            Write-Detail "Unexpected shutdown at: $($us.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'))"
        }
    } else {
        Write-Finding "OK" "No unexpected shutdown events found"
    }
} catch {
    Write-Finding "INFO" "Could not query shutdown events: $_"
}

# Kernel-Power 41 (the classic "PC just turned off" event)
try {
    $kp41 = Get-WinEvent -FilterHashtable @{
        LogName      = 'System'
        Id           = 41
        ProviderName = 'Microsoft-Windows-Kernel-Power'
    } -MaxEvents 10 -ErrorAction SilentlyContinue

    if ($kp41 -and $kp41.Count -gt 0) {
        Write-Finding "FAIL" "$($kp41.Count) Kernel-Power 41 event(s) — system rebooted without clean shutdown!"
        Write-Detail "This is the #1 indicator of crashes, power loss, or hardware failure."
        $issuesFound++
        foreach ($ev in $kp41 | Select-Object -First 5) {
            Write-Detail "Event at: $($ev.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'))"
        }
    } else {
        Write-Finding "OK" "No Kernel-Power 41 events found"
    }
} catch {
    Write-Finding "INFO" "Could not query Kernel-Power events: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [4] WINDOWS SYSTEM FILE INTEGRITY (SFC + DISM)
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[4/15] WINDOWS SYSTEM FILE INTEGRITY"

# Check if there are pending SFC results from a previous run
$sfcLog = "$script:WindowsDirectory\Logs\CBS\CBS.log"
if (Test-Path $sfcLog) {
    $recentCorruption = Select-String -Path $sfcLog -Pattern "Cannot repair member file|Hashes for file member" -ErrorAction SilentlyContinue |
                        Select-Object -Last 5
    if ($recentCorruption -and $recentCorruption.Count -gt 0) {
        Write-Finding "WARN" "Previous SFC scan found corruption evidence in CBS.log"
        $issuesFound++
    }
}

Write-Finding "FIX" "Running DISM RestoreHealth (this may take 5-15 minutes)..."
Write-Host "     Please wait — repairing Windows component store..." -ForegroundColor DarkYellow
try {
    $dismResult = & DISM.exe /Online /Cleanup-Image /RestoreHealth /NoRestart 2>&1
    $dismExitCode = $LASTEXITCODE
    $dismOutput = $dismResult -join "`n"

    if ($dismExitCode -eq 0) {
        if ($dismOutput -match "The component store is repairable") {
            Write-Finding "FIX" "DISM found and REPAIRED component store corruption!"
            $fixesApplied++
            Write-FixLog "DISM RestoreHealth repaired component store corruption"
        } elseif ($dismOutput -match "No component store corruption detected") {
            Write-Finding "OK" "DISM: No component store corruption detected"
        } else {
            Write-Finding "OK" "DISM RestoreHealth completed successfully"
        }
    } else {
        Write-Finding "WARN" "DISM RestoreHealth exited with code $dismExitCode"
        $issuesFound++
        # Try online repair source
        Write-Finding "FIX" "Attempting DISM with online repair source..."
        & DISM.exe /Online /Cleanup-Image /RestoreHealth /Source:WU /NoRestart 2>&1 | Out-Null
    }
} catch {
    Write-Finding "FAIL" "DISM RestoreHealth failed: $_"
    $issuesFound++
}

Write-Finding "FIX" "Running SFC /scannow (this may take 5-10 minutes)..."
Write-Host "     Please wait — scanning and repairing system files..." -ForegroundColor DarkYellow
try {
    $sfcResult = & sfc.exe /scannow 2>&1
    $sfcOutput = $sfcResult -join "`n"

    if ($sfcOutput -match "did not find any integrity violations") {
        Write-Finding "OK" "SFC: No integrity violations found"
    } elseif ($sfcOutput -match "successfully repaired") {
        Write-Finding "FIX" "SFC found and REPAIRED corrupted system files!"
        $fixesApplied++
        Write-FixLog "SFC /scannow repaired corrupted system files"
    } elseif ($sfcOutput -match "found corrupt files but was unable to fix") {
        Write-Finding "FAIL" "SFC found corruption it CANNOT fix — may need repair install"
        $issuesFound++
    } else {
        Write-Finding "INFO" "SFC completed — check CBS.log for details"
    }
} catch {
    Write-Finding "FAIL" "SFC scan failed: $_"
    $issuesFound++
}

# ═════════════════════════════════════════════════════════════════════════════
# [5] DISK HEALTH
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[5/15] DISK HEALTH"

# Get physical disks
try {
    $physDisks = Get-PhysicalDisk -ErrorAction SilentlyContinue
    if ($physDisks) {
        foreach ($disk in $physDisks) {
            $status = $disk.HealthStatus
            $mediaType = $disk.MediaType
            $size = [math]::Round($disk.Size / 1GB, 1)

            if ($status -eq "Healthy") {
                Write-Finding "OK" "Disk $($disk.DeviceId): $($disk.FriendlyName) ($mediaType, ${size}GB) — $status"
            } elseif ($status -eq "Warning") {
                Write-Finding "WARN" "Disk $($disk.DeviceId): $($disk.FriendlyName) — WARNING status! Possible failing drive!"
                $issuesFound++
            } else {
                Write-Finding "FAIL" "Disk $($disk.DeviceId): $($disk.FriendlyName) — $status — DRIVE MAY BE FAILING!"
                $issuesFound++
            }

            # Check SMART via storage reliability counter
            try {
                $reliability = Get-StorageReliabilityCounter -PhysicalDisk $disk -ErrorAction SilentlyContinue
                if ($reliability) {
                    if ($reliability.ReadErrorsTotal -gt 0) {
                        Write-Finding "WARN" "  Read errors detected: $($reliability.ReadErrorsTotal)"
                        $issuesFound++
                    }
                    if ($reliability.WriteErrorsTotal -gt 0) {
                        Write-Finding "WARN" "  Write errors detected: $($reliability.WriteErrorsTotal)"
                        $issuesFound++
                    }
                    if ($reliability.Temperature -and $reliability.Temperature -gt 55) {
                        Write-Finding "WARN" "  Disk temperature: $($reliability.Temperature)°C — HOT!"
                        $issuesFound++
                    } elseif ($reliability.Temperature) {
                        Write-Finding "OK" "  Disk temperature: $($reliability.Temperature)°C"
                    }
                    if ($reliability.Wear -and $reliability.Wear -gt 80) {
                        Write-Finding "FAIL" "  SSD Wear level: $($reliability.Wear)% — drive nearing end of life!"
                        $issuesFound++
                    } elseif ($reliability.Wear) {
                        Write-Finding "OK" "  SSD Wear level: $($reliability.Wear)%"
                    }
                    $powerOnHrs = $reliability.PowerOnHours
                    if ($powerOnHrs) {
                        Write-Finding "INFO" "  Power-on hours: $powerOnHrs ($([math]::Round($powerOnHrs/8760, 1)) years)"
                    }
                }
            } catch {
                Write-Detail "  Could not read SMART/reliability data"
            }
        }
    }
} catch {
    Write-Finding "INFO" "Could not query physical disks: $_"
}

# Check volume free space
Write-Finding "INFO" "Checking drive free space..."
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    $freeGB = [math]::Round($_.FreeSpace / 1GB, 1)
    $totalGB = [math]::Round($_.Size / 1GB, 1)
    $usedPct = if ($_.Size -gt 0) { [math]::Round((($_.Size - $_.FreeSpace) / $_.Size) * 100, 1) } else { 0 }

    if ($freeGB -lt 5) {
        Write-Finding "FAIL" "Drive $($_.DeviceID) — CRITICALLY LOW: ${freeGB}GB free of ${totalGB}GB ($usedPct% used)"
        $issuesFound++
    } elseif ($freeGB -lt 20) {
        Write-Finding "WARN" "Drive $($_.DeviceID) — Low space: ${freeGB}GB free of ${totalGB}GB ($usedPct% used)"
        $issuesFound++
    } else {
        Write-Finding "OK" "Drive $($_.DeviceID) — ${freeGB}GB free of ${totalGB}GB ($usedPct% used)"
    }
}

# Schedule chkdsk on system drive if errors were found
Write-Finding "FIX" "Running filesystem check on system volume..."
try {
    $volResult = & fsutil dirty query $script:WindowsSystemDrive 2>&1
    if ($volResult -match "dirty") {
        Write-Finding "FAIL" "System volume $script:WindowsSystemDrive is marked DIRTY — filesystem errors present!"
        Write-Finding "FIX" "Scheduling chkdsk on next reboot..."
        & chkdsk.exe $script:WindowsSystemDrive /F /R /X 2>&1 | Out-Null
        $fixesApplied++
        Write-FixLog "Scheduled chkdsk /F /R on $script:WindowsSystemDrive for next reboot"
        $issuesFound++
    } else {
        Write-Finding "OK" "System volume $script:WindowsSystemDrive is clean"
    }
} catch {
    Write-Finding "INFO" "Could not check filesystem dirty bit: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [6] MEMORY (RAM) DIAGNOSTICS
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[6/15] MEMORY (RAM) DIAGNOSTICS"

# Check available memory
try {
    $os = Get-CimInstance Win32_OperatingSystem
    $freeMemMB = [math]::Round($os.FreePhysicalMemory / 1024, 0)
    $totalMemMB = [math]::Round($os.TotalVisibleMemorySize / 1024, 0)
    $usedPct = [math]::Round((1 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize)) * 100, 1)

    if ($freeMemMB -lt 500) {
        Write-Finding "FAIL" "CRITICALLY low free RAM: ${freeMemMB}MB free of ${totalMemMB}MB ($usedPct% used)"
        $issuesFound++
    } elseif ($freeMemMB -lt 1024) {
        Write-Finding "WARN" "Low free RAM: ${freeMemMB}MB free of ${totalMemMB}MB ($usedPct% used)"
        $issuesFound++
    } else {
        Write-Finding "OK" "RAM: ${freeMemMB}MB free of ${totalMemMB}MB ($usedPct% used)"
    }
} catch {
    Write-Finding "INFO" "Could not query memory status: $_"
}

# Check for previous Windows Memory Diagnostic results
try {
    $memDiag = Get-WinEvent -FilterHashtable @{
        LogName      = 'System'
        ProviderName = 'Microsoft-Windows-MemoryDiagnostics-Results'
    } -MaxEvents 5 -ErrorAction SilentlyContinue

    if ($memDiag -and $memDiag.Count -gt 0) {
        foreach ($m in $memDiag) {
            if ($m.Message -match "no errors") {
                Write-Finding "OK" "Memory Diagnostic ($($m.TimeCreated.ToString('yyyy-MM-dd'))): No errors found"
            } else {
                Write-Finding "FAIL" "Memory Diagnostic ($($m.TimeCreated.ToString('yyyy-MM-dd'))): ERRORS FOUND — faulty RAM!"
                Write-Detail $m.Message
                $issuesFound++
            }
        }
    } else {
        Write-Finding "INFO" "No previous Memory Diagnostic results found"
        Write-Finding "INFO" "Recommendation: Run 'mdsched.exe' to schedule a memory test on next reboot"
    }
} catch {
    Write-Finding "INFO" "Could not query memory diagnostic events"
}

# Check pagefile configuration
try {
    $pagefiles = Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue
    if ($pagefiles) {
        foreach ($pf in $pagefiles) {
            $usedPct = if ($pf.AllocatedBaseSize -gt 0) { [math]::Round(($pf.CurrentUsage / $pf.AllocatedBaseSize) * 100, 1) } else { 0 }
            if ($usedPct -gt 80) {
                Write-Finding "WARN" "Pagefile $($pf.Name): $usedPct% used ($($pf.CurrentUsage)MB / $($pf.AllocatedBaseSize)MB)"
                $issuesFound++
            } else {
                Write-Finding "OK" "Pagefile $($pf.Name): $usedPct% used ($($pf.CurrentUsage)MB / $($pf.AllocatedBaseSize)MB)"
            }
        }
    } else {
        Write-Finding "WARN" "No pagefile detected — system may crash under memory pressure!"
        $issuesFound++
    }
} catch {
    Write-Finding "INFO" "Could not query pagefile: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [7] DRIVER CRASH ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[7/15] DRIVER CRASH ANALYSIS"

# Find recently problematic drivers
try {
    $driverErrors = Get-WinEvent -FilterHashtable @{
        LogName   = 'System'
        Level     = @(1, 2, 3)
        StartTime = (Get-Date).AddDays(-7)
    } -MaxEvents 100 -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match "driver|\.sys" }

    if ($driverErrors -and $driverErrors.Count -gt 0) {
        Write-Finding "WARN" "$($driverErrors.Count) driver-related error/warning events in last 7 days"
        $issuesFound++

        # Extract .sys file names mentioned
        $sysFiles = @()
        foreach ($de in $driverErrors) {
            if ($de.Message -match '\\([a-zA-Z0-9_]+\.sys)') {
                $sysFiles += $Matches[1]
            }
        }
        $sysFiles = $sysFiles | Select-Object -Unique
        if ($sysFiles.Count -gt 0) {
            Write-Finding "WARN" "Problematic driver files mentioned:"
            foreach ($sf in $sysFiles) {
                Write-Detail "  ⚡ $sf"
            }
        }
    } else {
        Write-Finding "OK" "No driver-related errors in last 7 days"
    }
} catch {
    Write-Finding "INFO" "Could not analyze driver events: $_"
}

# Check for unsigned/problematic drivers
Write-Finding "INFO" "Checking for unsigned or problematic drivers..."
try {
    $unsignedDrivers = Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
                       Where-Object { $_.IsSigned -eq $false -and $_.DriverName } |
                       Select-Object DeviceName, DriverName, DriverVersion -First 10

    if ($unsignedDrivers -and $unsignedDrivers.Count -gt 0) {
        Write-Finding "WARN" "$($unsignedDrivers.Count) unsigned driver(s) detected:"
        $issuesFound++
        foreach ($ud in $unsignedDrivers) {
            Write-Detail "  $($ud.DeviceName) — $($ud.DriverName) v$($ud.DriverVersion)"
        }
    } else {
        Write-Finding "OK" "All loaded drivers are signed"
    }
} catch {
    Write-Finding "INFO" "Could not check driver signatures: $_"
}

# Check for devices with errors
try {
    $errorDevices = Get-PnpDevice -Status Error -ErrorAction SilentlyContinue
    if ($errorDevices -and $errorDevices.Count -gt 0) {
        Write-Finding "FAIL" "$($errorDevices.Count) device(s) in ERROR state!"
        $issuesFound++
        foreach ($ed in $errorDevices) {
            Write-Detail "  ❌ $($ed.FriendlyName) [$($ed.Class)] — Status: $($ed.Status)"
        }
    } else {
        Write-Finding "OK" "No devices in error state"
    }

    $problemDevices = Get-PnpDevice -Status Degraded -ErrorAction SilentlyContinue
    if ($problemDevices -and $problemDevices.Count -gt 0) {
        Write-Finding "WARN" "$($problemDevices.Count) device(s) in DEGRADED state"
        $issuesFound++
        foreach ($pd in $problemDevices) {
            Write-Detail "  ⚠️  $($pd.FriendlyName) [$($pd.Class)]"
        }
    }
} catch {
    Write-Finding "INFO" "Could not enumerate PnP devices: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [8] GPU / DISPLAY DRIVER CRASHES
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[8/15] GPU / DISPLAY DRIVER CRASHES"

# Check for TDR (Timeout Detection and Recovery) events
try {
    $tdrEvents = Get-WinEvent -FilterHashtable @{
        LogName   = 'System'
        Id        = @(4101, 4097)
        ProviderName = 'Display'
    } -MaxEvents 10 -ErrorAction SilentlyContinue

    if ($tdrEvents -and $tdrEvents.Count -gt 0) {
        Write-Finding "FAIL" "$($tdrEvents.Count) GPU TDR (display driver crash/recovery) event(s)!"
        $issuesFound++
        foreach ($t in $tdrEvents | Select-Object -First 3) {
            Write-Detail "GPU crash at: $($t.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'))"
            $msgPreview = ($t.Message -split "`n")[0]
            if ($msgPreview.Length -gt 80) { $msgPreview = $msgPreview.Substring(0,80) + "..." }
            Write-Detail "  $msgPreview"
        }
    } else {
        Write-Finding "OK" "No GPU TDR (display driver crash) events found"
    }
} catch {
    Write-Finding "INFO" "Could not query GPU events: $_"
}

# Get GPU info
try {
    $gpus = Get-CimInstance Win32_VideoController
    foreach ($gpu in $gpus) {
        Write-Finding "INFO" "GPU: $($gpu.Name) — Driver: $($gpu.DriverVersion) ($($gpu.DriverDate.ToString('yyyy-MM-dd')))"
        if ($gpu.Status -ne "OK") {
            Write-Finding "WARN" "GPU status: $($gpu.Status)"
            $issuesFound++
        }
    }
} catch {
    Write-Finding "INFO" "Could not query GPU info: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [9] THERMAL / OVERHEATING DETECTION
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[9/15] THERMAL / OVERHEATING DETECTION"

try {
    # Try WMI thermal zones
    $thermalZones = Get-CimInstance -Namespace "root/WMI" -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue
    if ($thermalZones) {
        foreach ($tz in $thermalZones) {
            $tempC = [math]::Round(($tz.CurrentTemperature / 10) - 273.15, 1)
            if ($tempC -gt 90) {
                Write-Finding "FAIL" "CPU Temperature: ${tempC}°C — CRITICALLY HOT! Thermal throttling/shutdown likely!"
                $issuesFound++
            } elseif ($tempC -gt 75) {
                Write-Finding "WARN" "CPU Temperature: ${tempC}°C — Running hot"
                $issuesFound++
            } elseif ($tempC -gt 0) {
                Write-Finding "OK" "CPU Temperature: ${tempC}°C"
            }
        }
    } else {
        Write-Finding "INFO" "WMI thermal zone data not available (common on some systems)"
    }
} catch {
    Write-Finding "INFO" "Could not read thermal data: $_"
}

# Check for thermal throttling events
try {
    $throttleEvents = Get-WinEvent -FilterHashtable @{
        LogName   = 'System'
        Id        = @(1, 37)
        ProviderName = 'Microsoft-Windows-Kernel-Processor-Power'
    } -MaxEvents 10 -ErrorAction SilentlyContinue

    if ($throttleEvents -and $throttleEvents.Count -gt 0) {
        Write-Finding "WARN" "$($throttleEvents.Count) CPU throttling event(s) — possible overheating!"
        $issuesFound++
        foreach ($te in $throttleEvents | Select-Object -First 3) {
            Write-Detail "Throttle event: $($te.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'))"
        }
    } else {
        Write-Finding "OK" "No CPU thermal throttling events found"
    }
} catch {
    Write-Finding "INFO" "Could not query throttle events: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [10] WINDOWS UPDATE HEALTH
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[10/15] WINDOWS UPDATE HEALTH"

# Check for pending updates
try {
    $wuSession = New-Object -ComObject Microsoft.Update.Session -ErrorAction SilentlyContinue
    if ($wuSession) {
        $wuSearcher = $wuSession.CreateUpdateSearcher()
        Write-Finding "INFO" "Checking for pending Windows Updates..."
        $pendingUpdates = $wuSearcher.Search("IsInstalled=0 and Type='Software'").Updates

        if ($pendingUpdates.Count -gt 0) {
            Write-Finding "WARN" "$($pendingUpdates.Count) pending Windows Update(s)"
            $issuesFound++
            foreach ($u in $pendingUpdates | Select-Object -First 5) {
                Write-Detail "  📦 $($u.Title)"
            }
            # Check for critical/security updates
            $criticalUpdates = @($pendingUpdates | Where-Object {
                $_.MsrcSeverity -eq "Critical" -or $_.MsrcSeverity -eq "Important"
            })
            if ($criticalUpdates.Count -gt 0) {
                Write-Finding "FAIL" "$($criticalUpdates.Count) CRITICAL/IMPORTANT security update(s) missing!"
                $issuesFound++
            }
        } else {
            Write-Finding "OK" "Windows Update: No pending updates"
        }
    }
} catch {
    Write-Finding "INFO" "Could not check Windows Update status: $_"
}

# Check for pending reboot
$pendingReboot = $false
$rebootReasons = @()

if (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending") {
    $pendingReboot = $true
    $rebootReasons += "Component Based Servicing"
}
if (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired") {
    $pendingReboot = $true
    $rebootReasons += "Windows Update"
}
if (Test-Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager") {
    $pfro = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager" -Name PendingFileRenameOperations -ErrorAction SilentlyContinue
    if ($pfro.PendingFileRenameOperations) {
        $pendingReboot = $true
        $rebootReasons += "Pending File Rename Operations"
    }
}

if ($pendingReboot) {
    Write-Finding "WARN" "System has PENDING REBOOT required! Reasons: $($rebootReasons -join ', ')"
    Write-Finding "INFO" "⚡ A pending reboot can cause instability and crashes. Reboot ASAP!"
    $issuesFound++
} else {
    Write-Finding "OK" "No pending reboot required"
}

# Fix Windows Update components
Write-Finding "FIX" "Resetting Windows Update components..."
try {
    # Stop WU services
    Stop-Service -Name wuauserv -Force -ErrorAction SilentlyContinue
    Stop-Service -Name cryptSvc -Force -ErrorAction SilentlyContinue
    Stop-Service -Name bits -Force -ErrorAction SilentlyContinue
    Stop-Service -Name msiserver -Force -ErrorAction SilentlyContinue

    # Preserve existing update backup data; deleting it is a separate decision.
    $sdBackup = "$script:WindowsDirectory\SoftwareDistribution.bak"
    if (Test-Path -LiteralPath $sdBackup) {
        Write-Finding "INFO" "Existing Windows Update backup preserved: $sdBackup"
    }

    # Re-register DLLs
    $dlls = @(
        "atl.dll", "urlmon.dll", "mshtml.dll", "shdocvw.dll", "browseui.dll",
        "jscript.dll", "vbscript.dll", "scrrun.dll", "msxml.dll", "msxml3.dll",
        "msxml6.dll", "actxprxy.dll", "softpub.dll", "wintrust.dll", "dssenh.dll",
        "rsaenh.dll", "gpkcsp.dll", "sccbase.dll", "slbcsp.dll", "cryptdlg.dll",
        "oleaut32.dll", "ole32.dll", "shell32.dll", "initpki.dll", "wuapi.dll",
        "wuaueng.dll", "wuaueng1.dll", "wucltui.dll", "wups.dll", "wups2.dll",
        "wuweb.dll", "qmgr.dll", "qmgrprxy.dll", "wucltux.dll", "muweb.dll",
        "wuwebv.dll"
    )
    foreach ($dll in $dlls) {
        & regsvr32.exe /s $dll 2>&1 | Out-Null
    }

    # Reset Winsock and proxy
    & netsh winsock reset 2>&1 | Out-Null
    & netsh winhttp reset proxy 2>&1 | Out-Null

    # Restart services
    Start-Service -Name wuauserv -ErrorAction SilentlyContinue
    Start-Service -Name cryptSvc -ErrorAction SilentlyContinue
    Start-Service -Name bits -ErrorAction SilentlyContinue
    Start-Service -Name msiserver -ErrorAction SilentlyContinue

    Write-Finding "FIX" "Windows Update components reset successfully"
    $fixesApplied++
    Write-FixLog "Reset Windows Update components (services, DLL registrations, Winsock)"
} catch {
    Write-Finding "WARN" "Could not fully reset WU components: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [11] POWER SETTINGS & SLEEP ISSUES
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[11/15] POWER SETTINGS & SLEEP/HIBERNATE ISSUES"

try {
    $powerPlan = Get-CimInstance -Namespace "root\cimv2\power" -ClassName Win32_PowerPlan -ErrorAction SilentlyContinue |
                 Where-Object { $_.IsActive }
    if ($powerPlan) {
        Write-Finding "INFO" "Active power plan: $($powerPlan.ElementName)"
    }
} catch {
    Write-Finding "INFO" "Could not query power plan"
}

# Check if fast startup is enabled (can cause issues)
try {
    $fastBoot = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" -Name HiberbootEnabled -ErrorAction SilentlyContinue
    if ($fastBoot -and $fastBoot.HiberbootEnabled -eq 1) {
        Write-Finding "WARN" "Fast Startup is ENABLED — this can cause crash/boot issues"
        Write-Finding "FIX" "Disabling Fast Startup for stability..."
        Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" -Name HiberbootEnabled -Value 0 -ErrorAction SilentlyContinue
        $fixesApplied++
        Write-FixLog "Disabled Fast Startup (HiberbootEnabled = 0)"
        $issuesFound++
    } else {
        Write-Finding "OK" "Fast Startup is disabled"
    }
} catch {
    Write-Finding "INFO" "Could not check Fast Startup: $_"
}

# Check sleep/wake failures
try {
    $sleepErrors = Get-WinEvent -FilterHashtable @{
        LogName   = 'System'
        Id        = @(42, 107)
        ProviderName = 'Microsoft-Windows-Kernel-Power'
    } -MaxEvents 10 -ErrorAction SilentlyContinue

    if ($sleepErrors -and $sleepErrors.Count -gt 0) {
        Write-Finding "WARN" "$($sleepErrors.Count) sleep/wake failure event(s) found"
        $issuesFound++
    } else {
        Write-Finding "OK" "No sleep/wake failure events"
    }
} catch {
    Write-Finding "INFO" "Could not query sleep events: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [12] STARTUP PROGRAM OVERLOAD
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[12/15] STARTUP PROGRAM ANALYSIS"

try {
    $startupItems = Get-CimInstance Win32_StartupCommand -ErrorAction SilentlyContinue
    if ($startupItems) {
        Write-Finding "INFO" "$($startupItems.Count) startup programs registered"
        if ($startupItems.Count -gt 15) {
            Write-Finding "WARN" "High number of startup programs ($($startupItems.Count)) — may slow boot and increase crash risk"
            $issuesFound++
        }
        foreach ($si in $startupItems | Sort-Object Name) {
            Write-Detail "  🚀 $($si.Name) — $($si.Location)"
        }
    }
} catch {
    Write-Finding "INFO" "Could not enumerate startup programs: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [13] ANTIVIRUS & SECURITY SOFTWARE CONFLICTS
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[13/15] ANTIVIRUS & SECURITY SOFTWARE"

try {
    $avProducts = Get-CimInstance -Namespace "root/SecurityCenter2" -ClassName AntiVirusProduct -ErrorAction SilentlyContinue
    if ($avProducts) {
        $avCount = ($avProducts | Measure-Object).Count
        if ($avCount -gt 1) {
            Write-Finding "WARN" "Multiple antivirus products detected ($avCount) — can cause conflicts and crashes!"
            $issuesFound++
        }
        foreach ($av in $avProducts) {
            Write-Finding "INFO" "AV: $($av.displayName)"
        }
    } else {
        Write-Finding "WARN" "Could not detect antivirus products"
    }
} catch {
    Write-Finding "INFO" "Could not query Security Center: $_"
}

# Check Windows Defender status
try {
    $defenderStatus = Get-MpComputerStatus -ErrorAction SilentlyContinue
    if ($defenderStatus) {
        Write-Finding "INFO" "Windows Defender — Real-time: $($defenderStatus.RealTimeProtectionEnabled), Tamper: $($defenderStatus.IsTamperProtected)"
        if (-not $defenderStatus.AntivirusSignatureLastUpdated -or
            $defenderStatus.AntivirusSignatureLastUpdated -lt (Get-Date).AddDays(-7)) {
            Write-Finding "WARN" "Windows Defender definitions are outdated (last: $($defenderStatus.AntivirusSignatureLastUpdated))"
            $issuesFound++
        }
    }
} catch {
    Write-Finding "INFO" "Could not query Defender status"
}

# ═════════════════════════════════════════════════════════════════════════════
# [14] RELIABILITY HISTORY
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[14/15] RELIABILITY HISTORY (Last 14 Days)"

try {
    $reliabilityRecords = Get-CimInstance -ClassName Win32_ReliabilityRecords -ErrorAction SilentlyContinue |
                          Where-Object { $_.TimeGenerated -gt (Get-Date).AddDays(-14) } |
                          Where-Object { $_.EventIdentifier -ne 0 } |
                          Sort-Object TimeGenerated -Descending |
                          Select-Object -First 20

    if ($reliabilityRecords -and $reliabilityRecords.Count -gt 0) {
        Write-Finding "INFO" "$($reliabilityRecords.Count) reliability event(s) in last 14 days"

        $crashes = @($reliabilityRecords | Where-Object { $_.SourceName -match "Application Error|Application Hang|Windows|LiveKernelEvent" })
        if ($crashes.Count -gt 0) {
            Write-Finding "WARN" "$($crashes.Count) application crash/hang event(s)"
            $issuesFound++
            foreach ($c in $crashes | Select-Object -First 8) {
                $msgPreview = if ($c.Message.Length -gt 70) { $c.Message.Substring(0, 70) + "..." } else { $c.Message }
                Write-Detail "$($c.TimeGenerated.ToString('MM-dd HH:mm')) [$($c.SourceName)] $msgPreview"
            }
        }
    } else {
        Write-Finding "OK" "No recent reliability issues"
    }
} catch {
    Write-Finding "INFO" "Could not query reliability records: $_"
}

# ═════════════════════════════════════════════════════════════════════════════
# [15] ADDITIONAL AUTOMATED FIXES
# ═════════════════════════════════════════════════════════════════════════════
Write-Section "[15/15] APPLYING ADDITIONAL REPAIRS"

# Fix 1: Shared temporary folders may contain unsaved or recovery material.
Write-Finding "INFO" "Shared temporary files preserved. Review Windows Settings > System > Storage for optional cleanup."

# Fix 2: Reset network stack (common cause of random hangs)
Write-Finding "FIX" "Resetting network stack..."
try {
    & ipconfig /flushdns 2>&1 | Out-Null
    & netsh int ip reset 2>&1 | Out-Null
    Write-Finding "FIX" "DNS cache flushed and IP stack reset"
    $fixesApplied++
    Write-FixLog "Flushed DNS cache and reset IP stack"
} catch {
    Write-Finding "INFO" "Network reset partially completed: $_"
}

# Fix 3: Clear font cache (corrupted font cache causes Explorer crashes)
Write-Finding "FIX" "Clearing font cache..."
try {
    Stop-Service -Name FontCache -Force -ErrorAction SilentlyContinue
    Stop-Service -Name FontCache3.0.0.0 -Force -ErrorAction SilentlyContinue
    $fontCachePath = "$script:WindowsDirectory\ServiceProfiles\LocalService\AppData\Local\FontCache"
    Assert-DiagnosticNoLinks $fontCachePath
    if (Test-Path -LiteralPath $fontCachePath) {
        Get-ChildItem -LiteralPath $fontCachePath -File -ErrorAction Stop | ForEach-Object {
            Assert-DiagnosticNoLinks $_.FullName
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
        }
    }
    Start-Service -Name FontCache -ErrorAction SilentlyContinue
    Write-Finding "FIX" "Font cache cleared and service restarted"
    $fixesApplied++
    Write-FixLog "Cleared font cache"
} catch {
    Write-Finding "INFO" "Font cache cleanup: $_"
}

# Fix 4: Reset icon cache (corrupted icon cache causes Explorer issues)
Write-Finding "FIX" "Clearing icon cache..."
try {
    $iconCachePath = "$script:WindowsLocalAppData\Microsoft\Windows\Explorer"
    Assert-DiagnosticNoLinks $iconCachePath
    if (Test-Path -LiteralPath $iconCachePath) {
        Get-ChildItem -LiteralPath $iconCachePath -File -Filter "iconcache*" -ErrorAction Stop | ForEach-Object {
            Assert-DiagnosticNoLinks $_.FullName
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
        }
        Get-ChildItem -LiteralPath $iconCachePath -File -Filter "thumbcache*" -ErrorAction Stop | ForEach-Object {
            Assert-DiagnosticNoLinks $_.FullName
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
        }
    }
    Write-Finding "FIX" "Icon and thumbnail caches cleared"
    $fixesApplied++
    Write-FixLog "Cleared icon and thumbnail caches"
} catch {
    Write-Finding "INFO" "Icon cache cleanup: $_"
}

# Fix 5: Repair WMI repository (corrupted WMI causes system instability)
Write-Finding "FIX" "Verifying WMI repository integrity..."
try {
    $wmiResult = & winmgmt /verifyrepository 2>&1
    if ($wmiResult -match "not consistent" -or $wmiResult -match "inconsistent") {
        Write-Finding "WARN" "WMI repository is INCONSISTENT — repairing..."
        & winmgmt /salvagerepository 2>&1 | Out-Null
        Write-Finding "FIX" "WMI repository salvage attempted"
        $fixesApplied++
        Write-FixLog "Salvaged inconsistent WMI repository"
        $issuesFound++
    } else {
        Write-Finding "OK" "WMI repository is consistent"
    }
} catch {
    Write-Finding "INFO" "Could not verify WMI: $_"
}

# Fix 6: Check and repair Windows image
Write-Finding "FIX" "Running DISM component cleanup..."
try {
    # Keep Windows update rollback available; do not apply /ResetBase.
    & DISM.exe /Online /Cleanup-Image /StartComponentCleanup /NoRestart 2>&1 | Out-Null
    Write-Finding "FIX" "DISM component store cleaned up"
    $fixesApplied++
    Write-FixLog "DISM component store cleanup completed"
} catch {
    Write-Finding "INFO" "DISM cleanup: $_"
}

# Fix 7: Increase TDR timeout (prevents GPU driver crash resets)
Write-Finding "FIX" "Adjusting GPU TDR timeout to prevent display driver crashes..."
try {
    $tdrKey = "HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
    $currentTdr = Get-ItemProperty -Path $tdrKey -Name TdrDelay -ErrorAction SilentlyContinue
    if (-not $currentTdr -or $currentTdr.TdrDelay -lt 8) {
        Set-ItemProperty -Path $tdrKey -Name TdrDelay -Value 10 -Type DWord -ErrorAction SilentlyContinue
        Write-Finding "FIX" "GPU TDR timeout increased to 10 seconds (default is 2)"
        $fixesApplied++
        Write-FixLog "Increased TDR timeout to 10 seconds"
    } else {
        Write-Finding "OK" "GPU TDR timeout already set to $($currentTdr.TdrDelay) seconds"
    }
} catch {
    Write-Finding "INFO" "Could not adjust TDR: $_"
}

# Fix 8: Disable memory compression if RAM is adequate (can cause crashes on some systems)
try {
    $memInfo = Get-CimInstance Win32_OperatingSystem
    $totalRAMGB = [math]::Round($memInfo.TotalVisibleMemorySize / 1MB, 0)
    if ($totalRAMGB -ge 8) {
        $memComp = Get-MMAgent -ErrorAction SilentlyContinue
        if ($memComp -and $memComp.MemoryCompression) {
            Write-Finding "INFO" "Memory compression is active (RAM: ${totalRAMGB}GB)"
            Write-Detail "If experiencing crashes, try: Disable-MMAgent -MemoryCompression"
        }
    }
} catch {
    # Not critical, skip silently
}

# ═════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host ""
$summaryLine = "═" * 76
Write-Host "╔$summaryLine╗" -ForegroundColor $(if ($issuesFound -gt 5) { "Red" } elseif ($issuesFound -gt 0) { "Yellow" } else { "Green" })
Write-Host "║                         DIAGNOSTIC SUMMARY                                ║" -ForegroundColor White
Write-Host "╠$summaryLine╣" -ForegroundColor DarkGray

$summaryText = @"

══════════════════════════════════════════════════════════════════════════════
                           DIAGNOSTIC SUMMARY
══════════════════════════════════════════════════════════════════════════════
"@
Add-Content -Path $reportPath -Value $summaryText

if ($issuesFound -eq 0) {
    Write-Host "║  ✅ NO ISSUES FOUND — Your system appears healthy!                      ║" -ForegroundColor Green
    Add-Content -Path $reportPath -Value "  ✅ NO ISSUES FOUND — System appears healthy!"
} else {
    $msg = "  ⚠️  $issuesFound ISSUE(S) FOUND"
    Write-Host "║$($msg.PadRight(76))║" -ForegroundColor Yellow
    Add-Content -Path $reportPath -Value $msg
}

$fixMsg = "  🔧 $fixesApplied FIX(ES) APPLIED AUTOMATICALLY"
Write-Host "║$($fixMsg.PadRight(76))║" -ForegroundColor Cyan
Add-Content -Path $reportPath -Value $fixMsg

Write-Host "╠$summaryLine╣" -ForegroundColor DarkGray
Write-Host "║  RECOMMENDED NEXT STEPS:                                                  ║" -ForegroundColor White

$steps = @()

if ($issuesFound -gt 0) {
    $steps += "║  1. REBOOT your PC now to apply pending fixes                             ║"
    $steps += "║  2. Run Windows Memory Diagnostic: Win+R → mdsched.exe → Restart Now     ║"
    $steps += "║  3. Update all drivers via Device Manager or manufacturer's website       ║"
    $steps += "║  4. Check for Windows Updates: Settings → Update & Security               ║"
    $steps += "║  5. If crashes continue, check the detailed report file                   ║"
} else {
    $steps += "║  1. Reboot to apply any pending component repairs                         ║"
    $steps += "║  2. Keep Windows and drivers updated                                      ║"
    $steps += "║  3. Monitor system stability over the next few days                       ║"
}

$nextStepsText = @"

  RECOMMENDED NEXT STEPS:
"@
Add-Content -Path $reportPath -Value $nextStepsText

foreach ($s in $steps) {
    Write-Host $s -ForegroundColor White
    Add-Content -Path $reportPath -Value ($s -replace '║', '' ).Trim()
}

Write-Host "╠$summaryLine╣" -ForegroundColor DarkGray
$reportMsg = "║  📄 Full report: $($reportPath.PadRight($(76 - 18)))║"
if ($reportMsg.Length -gt 78) {
    $reportMsg = "║  📄 Full report saved to temp/audit/crash-diagnostic                       ║"
}
Write-Host $reportMsg -ForegroundColor DarkGray
$fixLogMsg = "║  📄 Fixes log:   $($fixLogPath.PadRight($(76 - 18)))║"
if ($fixLogMsg.Length -gt 78) {
    $fixLogMsg = "║  📄 Fixes log saved to temp/audit/crash-diagnostic                          ║"
}
Write-Host $fixLogMsg -ForegroundColor DarkGray
Write-Host "╚$summaryLine╝" -ForegroundColor DarkGray

Add-Content -Path $reportPath -Value "`n  Full report: $reportPath"
Add-Content -Path $reportPath -Value "  Fixes log: $fixLogPath"
Add-Content -Path $reportPath -Value "`n═══ END OF REPORT ═══"

Write-Host ""
Write-Host "  ⚡ A REBOOT is strongly recommended to apply all fixes!" -ForegroundColor Yellow
Write-Host ""

# Offer to run memory diagnostic
$memChoice = if ($NoPause) { 'n' } else { Read-Host "  Would you like to schedule a Windows Memory Diagnostic on next reboot? (y/n)" }
if ($memChoice -eq 'y' -or $memChoice -eq 'Y') {
    Write-Host "  Scheduling memory diagnostic..." -ForegroundColor Cyan
    & mdsched.exe /rundiag
    Write-Finding "FIX" "Scheduled Windows Memory Diagnostic for next reboot"
    Write-FixLog "Scheduled Windows Memory Diagnostic"
}

Write-Host ""
$rebootChoice = if ($NoPause) { 'n' } else { Read-Host "  Would you like to reboot now to apply all fixes? (y/n)" }
if ($rebootChoice -eq 'y' -or $rebootChoice -eq 'Y') {
    Write-Host "  Rebooting in 15 seconds... Press Ctrl+C to cancel." -ForegroundColor Red
    & shutdown /r /t 15 /c "PC Crash Diagnostic - Applying fixes reboot"
}

Write-Host ""
Write-Host "  Script complete. Stay safe! 🛡️" -ForegroundColor Green
Write-Host ""
