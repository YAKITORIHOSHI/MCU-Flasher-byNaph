[CmdletBinding()]
param(
    [switch]$FullReset,
    [switch]$DryRun,
    [switch]$NoPause
)

# PURPOSE: remove MCU Flasher's bootstrap/runtime dependencies from Windows
# so first-run setup can be tested again. This script never runs from the app.
# DANGER: -FullReset removes system components used by other applications.

$ErrorActionPreference = "Stop"
$scriptDir = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))

# Accept either layout:
#   <project>\DANGER-ZONE\this-script.ps1
# or
#   <project>\this-script.ps1
# but never guess beyond those two exact candidates. The marker check prevents
# an accidentally copied reset script from deleting an unrelated directory.
$projectRoot = $null
$rootCandidates = @(
    $scriptDir,
    [IO.Path]::GetFullPath((Split-Path -Parent $scriptDir))
) | Select-Object -Unique
foreach ($candidate in $rootCandidates) {
    if ((Test-Path -LiteralPath (Join-Path $candidate "runThisOnWindows.vbs")) -and
        (Test-Path -LiteralPath (Join-Path $candidate "launcher.py"))) {
        $projectRoot = $candidate
        break
    }
}
if (-not $projectRoot) {
    throw "Safety check failed: reset script is not in the MCU Flasher project root or its DANGER-ZONE folder."
}
$envDir = Join-Path $projectRoot "env"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$script:warningCount = 0
$script:approvedDirectoryTargets = [System.Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)
$script:approvedFileTargets = [System.Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)

# No third-party uninstaller is allowed to block the reset forever.
# 5 minutes is deliberately generous for Python/Node/MSI removal on slow disks.
$script:defaultProcessTimeoutSeconds = 300
$script:pythonBundleSettleSeconds = 120

function Get-NormalizedPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Approve-DirectoryTarget([string]$Path) {
    $normalized = Get-NormalizedPath $Path
    if (-not $normalized) { throw "Refusing to approve an empty directory target." }
    if ($normalized -eq [IO.Path]::GetPathRoot($normalized).TrimEnd('\')) {
        throw "Refusing to approve a drive root: $normalized"
    }
    [void]$script:approvedDirectoryTargets.Add($normalized)
    return $normalized
}

function Test-ApprovedDirectoryTarget([string]$Path) {
    try {
        return $script:approvedDirectoryTargets.Contains((Get-NormalizedPath $Path))
    }
    catch { return $false }
}

function Approve-FileTarget([string]$Path) {
    $normalized = Get-NormalizedPath $Path
    if (-not $normalized -or -not [IO.Path]::GetFileName($normalized)) {
        throw "Refusing to approve an invalid file target: '$Path'."
    }
    [void]$script:approvedFileTargets.Add($normalized)
    return $normalized
}

function Test-ApprovedFileTarget([string]$Path) {
    try {
        return $script:approvedFileTargets.Contains((Get-NormalizedPath $Path))
    }
    catch { return $false }
}

# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------
function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not $DryRun -and -not (Test-Administrator)) {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath) { $scriptPath = $MyInvocation.MyCommand.Path }
    Write-Host "Requesting Administrator privileges..." -ForegroundColor Yellow
    try {
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
        if ($FullReset) { $arguments += " -FullReset" }
        if ($NoPause) { $arguments += " -NoPause" }
        $proc = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -ArgumentList $arguments -ErrorAction Stop
        exit $proc.ExitCode
    }
    catch {
        Write-Warning "Could not auto-elevate: $_"
        Write-Warning "Please right-click PowerShell / Terminal and select 'Run as Administrator', then run this script."
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Console output helpers
# ---------------------------------------------------------------------------
function Write-Step([string]$Text) { Write-Host $Text -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host "  [--]   $Text" -ForegroundColor DarkGray }
function Write-Bad([string]$Text) {
    $script:warningCount++
    Write-Host "  [FAIL] $Text" -ForegroundColor Red
}

function Initialize-Progress([int]$TotalSteps) {
    $script:totalSteps = [Math]::Max(1, $TotalSteps)
    $script:completedSteps = 0
}

function Show-ProgressBar([string]$Status) {
    $percent = [Math]::Round(($script:completedSteps * 100) / $script:totalSteps)
    $filled = [Math]::Floor($percent / 5)
    $bar = ("=" * $filled).PadRight(20, " ")
    Write-Host ("[{0}] {1,3}%  {2}/{3}  {4}" -f $bar, $percent, $script:completedSteps, $script:totalSteps, $Status)
    Write-Progress -Activity "MCU Flasher Bootstrap Reset" -Status $Status -PercentComplete $percent
}

function Start-Step([string]$Status) {
    Show-ProgressBar $Status
}

function Complete-Step([string]$Status) {
    $script:completedSteps = [Math]::Min($script:completedSteps + 1, $script:totalSteps)
    Show-ProgressBar $Status
}

# ---------------------------------------------------------------------------
# Process / file-system helpers
# ---------------------------------------------------------------------------
function Stop-ProcessesByName([string[]]$Names) {
    if ($DryRun) {
        Write-Info ("Would stop: " + ($Names -join ", "))
        return
    }
    foreach ($name in $Names) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

function Stop-AppOwnedProcesses {
    if ($DryRun) {
        Write-Info "Would stop MCU Flasher processes running from the project or app PlatformIO folders."
        return
    }
    $roots = @(
        (Get-NormalizedPath $projectRoot),
        (Get-NormalizedPath (Join-Path $env:LOCALAPPDATA ".platformio-mcu-gui")),
        (Get-NormalizedPath "C:\.platformio-mcu-gui")
    )
    Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
        $process = $_
        try {
            $exe = Get-NormalizedPath $process.Path
            if ($roots | Where-Object { $exe -eq $_ -or $exe.StartsWith($_ + '\', [StringComparison]::OrdinalIgnoreCase) }) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        catch { }
    }
}

function Remove-DirectoryForce([string]$Path) {
    if (-not (Test-ApprovedDirectoryTarget $Path)) {
        Write-Bad "Safety refusal: directory is not an approved reset target: '$Path'."
        return
    }
    if ($DryRun) {
        Write-Info "Would remove directory: $Path"
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        # Fast path: most app-owned folders delete immediately.  Ownership
        # repair is expensive on PlatformIO trees, so use it only on failure.
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        return
    }
    catch {
        & takeown.exe /f $Path /r /d y *> $null
        $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls.exe $Path /grant "$($currentUser):(OI)(CI)F" /t /c *> $null
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $Path) {
            Write-Bad "Could not fully remove directory '$Path'. Some files might be locked by another process."
        }
    }
}

function Remove-FileForce([string]$Path) {
    if (-not (Test-ApprovedFileTarget $Path)) {
        Write-Bad "Safety refusal: file is not an approved reset target: '$Path'."
        return
    }
    if ($DryRun) {
        Write-Info "Would remove file: $Path"
        return
    }
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Process execution with MSI-aware retry
# ---------------------------------------------------------------------------
function Invoke-ProcessOnce(
    [string]$FilePath,
    [string[]]$Arguments,
    [int]$TimeoutSeconds = $script:defaultProcessTimeoutSeconds
) {
    if ($DryRun) {
        Write-Info ("Would run: {0} {1}" -f $FilePath, ($Arguments -join " "))
        return [pscustomobject]@{ Success = $true; ExitCode = 0; TimedOut = $false }
    }
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru -WindowStyle Hidden
        $finished = $process.WaitForExit([Math]::Max(1, $TimeoutSeconds) * 1000)
        if (-not $finished) {
            Write-Info "Process exceeded ${TimeoutSeconds}s; terminating its process tree instead of hanging the reset."
            try { & taskkill.exe /PID $process.Id /T /F *> $null } catch { }
            try { $process.WaitForExit(5000) | Out-Null } catch { }
            return [pscustomobject]@{ Success = $false; ExitCode = 1460; TimedOut = $true }
        }
        $process.Refresh()
        return [pscustomobject]@{ Success = ($process.ExitCode -in @(0, 3010)); ExitCode = $process.ExitCode; TimedOut = $false }
    }
    catch {
        return [pscustomobject]@{ Success = $false; ExitCode = -1; TimedOut = $false }
    }
}

# Windows Installer serializes every install/uninstall behind a single global
# mutex. Firing msiexec calls back-to-back without checking it is what causes
# ERROR_INSTALL_ALREADY_RUNNING (1618) on the second/third product in a batch.
function Wait-MsiMutex([int]$TimeoutSeconds = 60) {
    try {
        $mutex = [System.Threading.Mutex]::OpenExisting("Global\_MSIExecute")
    }
    catch {
        return # not held by anything right now
    }
    try {
        [void]$mutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
    }
    catch { }
    finally {
        try { $mutex.ReleaseMutex() } catch { }
        $mutex.Dispose()
    }
}

function Invoke-Process(
    [string]$FilePath,
    [string[]]$Arguments,
    [switch]$IsMsi,
    [int]$TimeoutSeconds = $script:defaultProcessTimeoutSeconds
) {
    $maxAttempts = 3
    $result = $null
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        if ($IsMsi) { Wait-MsiMutex }
        $result = Invoke-ProcessOnce $FilePath $Arguments $TimeoutSeconds
        if ($result.Success) { return $result }
        if ($result.TimedOut) { return $result }
        if ($IsMsi -and $result.ExitCode -eq 1601) {
            try {
                Start-Service -Name msiserver -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            }
            catch { }
        }
        if ($IsMsi -and $result.ExitCode -in @(1601, 1618) -and $attempt -lt $maxAttempts) {
            Write-Info "Windows Installer not ready (code $($result.ExitCode)), retrying in $($attempt * 3)s..."
            Start-Sleep -Seconds ($attempt * 3)
            continue
        }
        return $result
    }
    return $result
}

# ---------------------------------------------------------------------------
# Uninstall registry enumeration
# ---------------------------------------------------------------------------
function Get-AllUninstallEntries {
    $roots = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    $seen = @{}
    foreach ($root in $roots) {
        Get-ItemProperty -Path $root -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.DisplayName) {
                $key = $_.PSPath
                if (-not $seen.ContainsKey($key)) { $seen[$key] = $_ }
            }
        }
    }
    return @($seen.Values)
}

function Get-UninstallProducts([string]$DisplayNamePattern, $Entries) {
    return @($Entries | Where-Object { $_.DisplayName -match $DisplayNamePattern })
}

function Get-MsiProductCode($Product) {
    $guidPattern = "(\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\})"
    foreach ($candidate in @($Product.PSChildName, $Product.UninstallString, $Product.QuietUninstallString)) {
        if ($candidate -and $candidate -match $guidPattern) { return $Matches[1] }
    }
    return $null
}

function Get-MsiProductState([string]$ProductCode) {
    if (-not $ProductCode) { return $null }
    if (-not ("MsiNative" -as [type])) {
        Add-Type @"
using System.Runtime.InteropServices;
public static class MsiNative {
    [DllImport("msi.dll", CharSet=CharSet.Unicode)]
    public static extern int MsiQueryProductState(string product);
}
"@ -ErrorAction SilentlyContinue
    }
    try { return [MsiNative]::MsiQueryProductState($ProductCode) }
    catch { return $null }
}

function Find-CachedMsi([string]$ProductCode) {
    if (-not $ProductCode) { return $null }
    $cacheRoot = Join-Path $env:ProgramData "Package Cache"
    if (-not (Test-Path -LiteralPath $cacheRoot)) { return $null }
    $escaped = [regex]::Escape($ProductCode.Trim("{}"))
    return Get-ChildItem -LiteralPath $cacheRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $escaped } |
        ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File -Filter "*.msi" -ErrorAction SilentlyContinue } |
        Select-Object -First 1
}

function Remove-StaleUninstallRegistration($Product, [string]$Reason) {
    if (-not $Product -or -not $Product.PSPath) { return }
    if ($DryRun) {
        Write-Info "Would remove stale registration for $($Product.DisplayName): $Reason"
        return
    }
    try {
        Remove-Item -LiteralPath $Product.PSPath -Recurse -Force -ErrorAction Stop
        Write-Ok "Removed stale registration for $($Product.DisplayName)."
    }
    catch {
        Write-Bad "Could not remove stale registration for $($Product.DisplayName): $_"
    }
}

# ---------------------------------------------------------------------------
# Uninstall a set of matching products
# ---------------------------------------------------------------------------
function Remove-MsiProducts([string]$DisplayNamePattern, [string]$Label, $Entries) {
    $products = Get-UninstallProducts $DisplayNamePattern $Entries
    if (-not $products) {
        Write-Info "${Label}: not installed."
        return
    }

    $guidPattern = "(\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\})"

    foreach ($product in $products) {
        Write-Host "  Removing $($product.DisplayName)..."
        $quiet = $product.QuietUninstallString
        $plain = $product.UninstallString
        $result = $null

        $productCode = Get-MsiProductCode $product

        if ($quiet) {
            # Vendor-provided silent uninstall command. This is always the
            # most reliable option because it already encodes the right
            # installer technology (MSI, Burn bundle, NSIS, etc.) instead of
            # us guessing. Previously the script pulled a GUID out of this
            # string and force-fed it to msiexec, which breaks for anything
            # that isn't a plain MSI product (e.g. python.org's bundle
            # installer, WebView2) and was the actual cause of the failures.
            $result = Invoke-Process "cmd.exe" @('/d', '/s', '/c', "`"$quiet`"") -IsMsi:($quiet -match "(?i)msiexec")
        }
        elseif ($plain -match "(?i)msiexec" -and $plain -match $guidPattern) {
            $guid = $Matches[1]
            $result = Invoke-Process "msiexec.exe" @("/x", $guid, "/quiet", "/norestart") -IsMsi
            if (-not $result.Success -and $result.ExitCode -in @(1603, 1612)) {
                $cachedMsi = Find-CachedMsi $guid
                if ($cachedMsi) {
                    Write-Info "Retrying $($product.DisplayName) through cached MSI package."
                    $result = Invoke-Process "msiexec.exe" @("/x", $cachedMsi.FullName, "/quiet", "/norestart") -IsMsi
                }
            }
        }
        elseif ($plain) {
            $result = Invoke-Process "cmd.exe" @('/d', '/s', '/c', "`"$plain`"")
        }
        else {
            Write-Bad "$($product.DisplayName) has no registered uninstaller; registration was preserved for manual repair."
            continue
        }

        if ($result.Success) {
            Write-Ok "$($product.DisplayName) removed."
        }
        elseif ($result.ExitCode -eq 1605) {
            Remove-StaleUninstallRegistration $product "Windows Installer says the product is already absent."
        }
        else {
            $msiState = Get-MsiProductState $productCode
            if ($productCode -and $msiState -ne 5) {
                Remove-StaleUninstallRegistration $product "Windows Installer no longer reports the product as installed."
                continue
            }
            # Do not erase the uninstall record after a failed uninstall. A
            # stale-looking registration is still the safest recovery handle
            # for Windows Installer or the vendor repair tool.
            if ($result.TimedOut) {
                Write-Bad "$($product.DisplayName) uninstall exceeded the safety timeout; its registration was preserved."
            }
            else {
                Write-Bad "$($product.DisplayName) uninstall failed (code $($result.ExitCode)); its registration was preserved."
            }
        }
    }
}

function Remove-Cp210xDriver {
    $driverText = (& pnputil.exe /enum-drivers 2>&1 | Out-String)
    $blocks = $driverText -split "(\r?\n){2,}"
    $publishedNames = @()
    foreach ($block in $blocks) {
        if ($block -match "(?i)silabser\.inf|silicon laboratories") {
            $publishedNames += [regex]::Matches($block, "(?i)oem\d+\.inf") | ForEach-Object { $_.Value }
        }
    }
    $publishedNames = $publishedNames | Select-Object -Unique
    if (-not $publishedNames) {
        Write-Info "CP210x driver: not installed."
        return
    }
    foreach ($name in $publishedNames) {
        Write-Host "  Removing CP210x driver package $name..."
        if ($DryRun) {
            Write-Info "Would run pnputil /delete-driver $name /uninstall /force"
            continue
        }
        & pnputil.exe /delete-driver $name /uninstall /force *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "CP210x driver package $name removed."
        }
        else {
            Write-Bad "Could not remove CP210x driver package $name (exit $LASTEXITCODE)."
        }
    }
}

function Remove-WebView2Runtime {
    $clientGuid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    $clientKeys = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$clientGuid",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$clientGuid",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$clientGuid"
    )
    $programFilesX86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    $runtimeFolders = @(
        (Join-Path $programFilesX86 "Microsoft\EdgeWebView"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\EdgeWebView")
    )
    foreach ($folder in $runtimeFolders) { Approve-DirectoryTarget $folder *> $null }
    $uninstallEntries = Get-UninstallProducts "(?i)Microsoft Edge WebView2 Runtime" (Get-AllUninstallEntries)

    if ($DryRun) {
        foreach ($entry in $uninstallEntries) {
            if ($entry.UninstallString) { Write-Info "Would run WebView2 uninstall command: $($entry.UninstallString)" }
        }
        foreach ($folder in $runtimeFolders) { Write-Info "Would remove WebView2 folder: $folder" }
        foreach ($key in $clientKeys) { Write-Info "Would remove WebView2 registration: $key" }
        return
    }

    foreach ($entry in $uninstallEntries) {
        if (-not $entry.UninstallString) { continue }
        Write-Host "  Removing $($entry.DisplayName)..."
        $result = Invoke-Process "cmd.exe" @('/d', '/s', '/c', "`"$($entry.UninstallString)`"")
        if ($result.Success) {
            Write-Ok "$($entry.DisplayName) uninstaller completed."
        }
        elseif ($result.ExitCode -eq 93) {
            Write-Info "$($entry.DisplayName) uninstaller reported no removable runtime (code 93); continuing with scoped cleanup."
        }
        else {
            Write-Info "$($entry.DisplayName) uninstaller returned code $($result.ExitCode); continuing with scoped cleanup."
        }
    }

    # Edge Update can recreate/hold WebView files immediately after its
    # uninstaller exits. Stop both services and retry the scoped deletion
    # before declaring the reset incomplete.
    Get-Service -Name "edgeupdate", "edgeupdatem" -ErrorAction SilentlyContinue |
    Stop-Service -Force -ErrorAction SilentlyContinue
    Stop-ProcessesByName @("msedgewebview2")

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        foreach ($folder in $runtimeFolders) {
            Remove-DirectoryForce $folder
        }
        foreach ($key in $clientKeys) {
            Remove-Item -LiteralPath $key -Recurse -Force -ErrorAction SilentlyContinue
        }

        $stillRegistered = $clientKeys | Where-Object { Test-Path -LiteralPath $_ }
        $stillOnDisk = $runtimeFolders | Where-Object { Test-Path -LiteralPath $_ }
        if (-not $stillRegistered -and -not $stillOnDisk) {
            $webViewEntries = Get-UninstallProducts "(?i)Microsoft Edge WebView2 Runtime" (Get-AllUninstallEntries)
            foreach ($entry in $webViewEntries) {
                Remove-StaleUninstallRegistration $entry "WebView2 files and EdgeUpdate client keys are gone."
            }
            Write-Ok "WebView2 Runtime: removed or not present."
            return
        }
        if ($attempt -lt 3) {
            Write-Info "WebView2 files are still releasing; retrying cleanup..."
            Start-Sleep -Seconds $attempt
        }
    }

    Write-Bad "WebView2 Runtime still has files or an EdgeUpdate registration after retries."
}

function Test-Cp210xInstalledByBootstrap {
    $sentinel = Join-Path $projectRoot "logs\.cp210x_installed"
    if (-not (Test-Path -LiteralPath $sentinel)) { return $false }
    try {
        $text = Get-Content -LiteralPath $sentinel -Raw -ErrorAction Stop
        return $text -match ("(?im)^machine:\s*" + [regex]::Escape($env:COMPUTERNAME) + "\s*$")
    }
    catch { return $false }
}

function Remove-OpenCodeCli {
    $npmCandidates = @(
        (Get-Command npm.cmd -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
        (Join-Path $env:APPDATA "npm\npm.cmd"),
        "C:\Program Files\nodejs\npm.cmd",
        "C:\Program Files (x86)\nodejs\npm.cmd"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

    if ($npmCandidates) {
        $npm = $npmCandidates | Select-Object -First 1
        $result = Invoke-Process $npm @("uninstall", "-g", "opencode-ai")
        if ($result.Success) {
            Write-Ok "OpenCode CLI npm package removed or not present."
        }
        else {
            Write-Bad "OpenCode CLI uninstall failed (code $($result.ExitCode))."
        }
    }
    else {
        Write-Info "OpenCode CLI: npm is not installed."
    }

    # npm normally removes these itself. Clean only the exact package/shim
    # names in case a previous uninstall was interrupted.
    $npmRoot = Join-Path $env:APPDATA "npm"
    $packageDir = Join-Path $npmRoot "node_modules\opencode-ai"
    Approve-DirectoryTarget $packageDir *> $null
    Remove-DirectoryForce $packageDir
    foreach ($shim in @("opencode", "opencode.cmd", "opencode.ps1")) {
        $shimPath = Join-Path $npmRoot $shim
        Approve-FileTarget $shimPath *> $null
        Remove-FileForce $shimPath
    }
}

function Wait-ForPythonInstallerSettle([int]$TimeoutSeconds = $script:pythonBundleSettleSeconds) {
    if ($DryRun) { return }
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $TimeoutSeconds))
    do {
        # Python's bundle coordinates several component MSIs. Wait for the MSI
        # mutex, then also make sure the bundle/runtime process itself has gone.
        # Do NOT treat an idle Windows Installer service process as "busy";
        # msiexec.exe may remain resident for minutes after work is complete.
        Wait-MsiMutex 10
        $busy = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Id -ne $PID -and $_.ProcessName -match '(?i)^(python(?:-|$)|pythonw$|python3$|py$)'
        })
        if (-not $busy) {
            Start-Sleep -Milliseconds 750
            return
        }
        Start-Sleep -Milliseconds 750
    } while ([DateTime]::UtcNow -lt $deadline)
    Write-Info "Python bundle is still settling after ${TimeoutSeconds}s; continuing with bounded residual cleanup."
}

function Get-PythonComponentRank($Product) {
    $name = $Product.DisplayName
    if ($name -match '(?i)Add to Path') { return 10 }
    if ($name -match '(?i)pip Bootstrap') { return 20 }
    if ($name -match '(?i)Documentation') { return 30 }
    if ($name -match '(?i)Development Libraries') { return 40 }
    if ($name -match '(?i)Tcl/Tk') { return 50 }
    if ($name -match '(?i)Standard Library') { return 60 }
    if ($name -match '(?i)Executables') { return 70 }
    if ($name -match '(?i)Core Interpreter') { return 80 }
    if ($name -match '(?i)Launcher') { return 90 }
    return 100
}

function Get-PythonInstallDirectories {
    $parents = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        ([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)),
        ([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86))
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

    $dirs = @()
    foreach ($parent in $parents) {
        $dirs += Get-ChildItem -LiteralPath $parent -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '(?i)^Python3\d+$|^Python 3\.\d+$|^Launcher$' } |
            Select-Object -ExpandProperty FullName
    }
    return @($dirs | Sort-Object -Unique)
}

function Remove-PathEntriesForTargets([string[]]$Targets) {
    $normalizedTargets = @()
    foreach ($target in $Targets) {
        if (-not $target) { continue }
        $normalizedTargets += Get-NormalizedPath $target
        $normalizedTargets += Get-NormalizedPath (Join-Path $target "Scripts")
    }
    $normalizedTargets = @($normalizedTargets | Where-Object { $_ } | Sort-Object -Unique)
    if (-not $normalizedTargets) { return }

    foreach ($scope in @("User", "Machine")) {
        $current = [Environment]::GetEnvironmentVariable("Path", $scope)
        if (-not $current) { continue }
        $parts = @($current -split ";" | Where-Object { $_ -and $_.Trim() })
        $kept = @()
        $removed = @()
        foreach ($part in $parts) {
            $normalizedPart = Get-NormalizedPath ($part.Trim())
            if ($normalizedTargets -contains $normalizedPart) { $removed += $part }
            else { $kept += $part }
        }
        if (-not $removed) { continue }
        if ($DryRun) {
            Write-Info "Would remove Python PATH entries from ${scope}: $($removed -join '; ')"
            continue
        }
        [Environment]::SetEnvironmentVariable("Path", ($kept -join ";"), $scope)
        Write-Ok "Removed Python PATH entries from $scope scope."
    }
}

function Remove-PythonInstallDirectories {
    $dirs = Get-PythonInstallDirectories
    foreach ($dir in $dirs) {
        Approve-DirectoryTarget $dir *> $null
        Remove-DirectoryForce $dir
    }

    $localPythonParent = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if ((Test-Path -LiteralPath $localPythonParent) -and
        -not (Get-ChildItem -LiteralPath $localPythonParent -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        Approve-DirectoryTarget $localPythonParent *> $null
        Remove-DirectoryForce $localPythonParent
    }
}

function Remove-PythonBootstrapPackages {
    # FullReset intentionally restores the machine to a "no Python 3 from this
    # clean first-run bootstrap baseline" state. Prefer the registered python.org bundle
    # uninstaller first. The bundle owns its component MSIs and knows the correct
    # order; manually racing those component MSIs is what caused hangs such as:
    #   Removing Python 3.14.x Core Interpreter (64-bit)...
    $entries = Get-AllUninstallEntries
    $knownPythonDirs = Get-PythonInstallDirectories
    $bundlePattern = '(?i)^Python 3\.\d+(?:\.\d+)? \((?:32|64)-bit\)$'
    $componentPattern = '(?i)^Python (?:3\.|Launcher)'
    $mainBundles = @($entries | Where-Object { $_.DisplayName -match $bundlePattern })
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue

    foreach ($bundle in $mainBundles) {
        Write-Info "Using the registered Python bundle uninstaller for $($bundle.DisplayName)."
        $exact = '^' + [regex]::Escape([string]$bundle.DisplayName) + '$'
        Remove-MsiProducts $exact "Python bundle" @($bundle)
        Wait-ForPythonInstallerSettle

        # If the bundle is still registered, winget is a bounded fallback. We do
        # not run winget first because some winget/provider paths return before
        # the bundle's final component MSI has completely settled.
        $stillThere = @(Get-AllUninstallEntries | Where-Object { $_.DisplayName -eq $bundle.DisplayName })
        if ($stillThere -and $winget -and $bundle.DisplayName -match '(?i)^Python 3\.(\d+)') {
            $minor = $Matches[1]
            $packageId = "Python.Python.3.$minor"
            Write-Info "$($bundle.DisplayName) is still registered; trying winget fallback $packageId."
            $result = Invoke-Process $winget.Source @(
                "uninstall", "--id", $packageId, "--exact", "--silent",
                "--disable-interactivity", "--accept-source-agreements"
            ) -TimeoutSeconds 300
            if ($result.Success) {
                Write-Ok "$packageId removal completed through winget fallback."
            }
            elseif ($result.TimedOut) {
                Write-Bad "$packageId winget uninstall timed out."
            }
            else {
                Write-Info "$packageId winget uninstall returned code $($result.ExitCode); residual cleanup will continue."
            }
            Wait-ForPythonInstallerSettle
        }
    }

    if ($DryRun) { return }

    # Remove ONLY residual Python component entries left after the bundle has
    # finished. This path should normally be empty. Each call is timeout-bounded,
    # so a damaged Core Interpreter MSI can no longer freeze the entire reset.
    $remaining = Get-AllUninstallEntries
    $componentEntries = @($remaining | Where-Object {
        $_.DisplayName -match $componentPattern -and
        $_.DisplayName -notmatch $bundlePattern
    } | Sort-Object @{ Expression = { Get-PythonComponentRank $_ } }, DisplayName)

    if ($componentEntries) {
        Write-Info "Cleaning $($componentEntries.Count) residual Python component registration(s) after bundle uninstall."
        foreach ($component in $componentEntries) {
            $exact = '^' + [regex]::Escape([string]$component.DisplayName) + '$'
            Remove-MsiProducts $exact "Python residual component" @($component)
        }
        Wait-ForPythonInstallerSettle 45
    }

    $pythonDirs = @(
        $knownPythonDirs +
        (Get-PythonInstallDirectories) +
        $envDir
    ) | Where-Object { $_ } | Sort-Object -Unique
    Remove-PathEntriesForTargets $pythonDirs

    # Old bootstrap builds wrote PYTHON_HOME. Current builds do not, but a reset
    # must remove a stale project/Python pointer from earlier runs as well.
    foreach ($scope in @("User", "Machine")) {
        $pythonHome = [Environment]::GetEnvironmentVariable("PYTHON_HOME", $scope)
        if (-not $pythonHome) { continue }
        $normalizedHome = Get-NormalizedPath $pythonHome
        $matchesRemovedTarget = $false
        foreach ($target in $pythonDirs) {
            $normalizedTarget = Get-NormalizedPath $target
            if ($normalizedTarget -and (
                $normalizedHome -eq $normalizedTarget -or
                $normalizedHome.StartsWith($normalizedTarget + '\', [StringComparison]::OrdinalIgnoreCase)
            )) {
                $matchesRemovedTarget = $true
                break
            }
        }
        if ($matchesRemovedTarget) {
            [Environment]::SetEnvironmentVariable("PYTHON_HOME", $null, $scope)
            Write-Ok "Removed stale PYTHON_HOME from $scope scope."
        }
    }

    Remove-PythonInstallDirectories

    # Finally remove stale bundle registrations only when no Python files or
    # component registrations remain. Never destroy the last recovery handle
    # while Windows still thinks an actual MSI component is installed.
    $remaining = Get-AllUninstallEntries
    $remainingBundles = @($remaining | Where-Object { $_.DisplayName -match $bundlePattern })
    $remainingComponents = @($remaining | Where-Object {
        $_.DisplayName -match $componentPattern -and
        $_.DisplayName -notmatch $bundlePattern
    })
    if (-not (Get-PythonInstallDirectories) -and -not $remainingComponents) {
        foreach ($bundle in $remainingBundles) {
            Remove-StaleUninstallRegistration $bundle "Python files and component registrations are gone."
        }
    }
}

function Remove-DefenderExclusions {
    $removeCommand = Get-Command Remove-MpPreference -ErrorAction SilentlyContinue
    if (-not $removeCommand) {
        Write-Info "Windows Defender exclusion command is unavailable."
        return
    }
    $localAppData = $env:LOCALAPPDATA
    $paths = @(
        (Join-Path $localAppData ".platformio-mcu-gui"),
        (Join-Path $localAppData ".pio-mcu"),
        "C:\.platformio-mcu-gui",
        (Join-Path $projectRoot "src\_board-frameworks\.platformio"),
        (Join-Path $env:USERPROFILE ".platformio"),
        $projectRoot
    ) | Select-Object -Unique
    foreach ($path in $paths) {
        if ($DryRun) {
            Write-Info "Would remove Defender exclusion: $path"
            continue
        }
        try {
            Remove-MpPreference -ExclusionPath $path -ErrorAction Stop
        }
        catch {
            # Not being present is the normal case when the opt-in bootstrap
            # flag was never enabled; don't turn it into a reset failure.
        }
    }
    Write-Ok "MCU Flasher Defender exclusions removed or not present."
}

function Initialize-ApprovedResetTargets {
    $targets = @(
        $envDir,
        (Join-Path $projectRoot ".pio"),
        (Join-Path $projectRoot ".pio_cache"),
        (Join-Path $projectRoot ".cache"),
        (Join-Path $projectRoot "__pycache__"),
        (Join-Path $projectRoot "logs"),
        (Join-Path $projectRoot "src\_board-frameworks\.platformio"),
        (Join-Path $projectRoot "soft_reset_project\boards"),
        (Join-Path $projectRoot "soft_reset_project\.pio"),
        (Join-Path $projectRoot "soft_reset_project_uno\boards"),
        (Join-Path $projectRoot "soft_reset_project_uno\.pio"),
        (Join-Path $env:LOCALAPPDATA ".platformio-mcu-gui"),
        (Join-Path $env:LOCALAPPDATA ".pio-mcu"),
        "C:\.platformio-mcu-gui",
        (Join-Path $env:TEMP ".platformio-mcu-gui"),
        (Join-Path $env:TEMP "mcu_flash_gui_cache"),
        (Join-Path $env:USERPROFILE ".mcu_flash_gui"),
        (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "_MCUFlasherByNaph_src")
    )
    if ($FullReset) {
        $targets += @(
            (Join-Path $env:LOCALAPPDATA "Arduino15"),
            (Join-Path $env:LOCALAPPDATA "pip\Cache"),
            (Join-Path $env:LOCALAPPDATA "npm-cache"),
            (Join-Path $env:APPDATA "npm-cache")
        )
    }
    foreach ($target in $targets) { Approve-DirectoryTarget $target *> $null }

    foreach ($file in @(
        (Join-Path $projectRoot "arduino_cli_path.txt"),
        (Join-Path $projectRoot ".force_rebuild"),
        (Join-Path $projectRoot "src\gui_config.json"),
        (Join-Path $env:USERPROFILE ".mcu_gui_config.json"),
        (Join-Path $env:TEMP "arduino_cli_msi_install.log"),
        (Join-Path $env:TEMP "py_exec_path.txt"),
        (Join-Path $env:TEMP "winget_py_search.txt")
    )) { Approve-FileTarget $file *> $null }

    Get-ChildItem -LiteralPath $projectRoot -Directory -Filter "env.incompatible-*" -ErrorAction SilentlyContinue |
        ForEach-Object { Approve-DirectoryTarget $_.FullName *> $null }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Write-Host ""
Write-Step "MCU Flasher Bootstrap Reset"
Write-Host "This removes MCU Flasher runtime data and, with -FullReset, the dependencies/caches used to reproduce a true first-run bootstrap."
Initialize-ApprovedResetTargets

if ($DryRun) {
    Write-Host "DRY RUN: nothing will be stopped, uninstalled, deleted, or changed." -ForegroundColor Yellow
}
elseif ($FullReset) {
    Write-Host ""
    Write-Host "DANGER - FULL WINDOWS DEPENDENCY RESET" -ForegroundColor Red
    Write-Host "This will uninstall detected Python 3, Python Launcher, Arduino CLI, Node.js," -ForegroundColor Yellow
    Write-Host "OpenCode CLI, and Microsoft Edge WebView2 Runtime. It also removes MCU Flasher" -ForegroundColor Yellow
    Write-Host "PlatformIO/toolchain data, the Arduino15 package store, reset caches, and app state." -ForegroundColor Yellow
    Write-Host "pip/npm download caches used by bootstrap timing runs are cleared too." -ForegroundColor Yellow
    Write-Host "Detected CP210x/Silicon Labs driver packages are removed too." -ForegroundColor Yellow
    Write-Host "User sketch folders are not reset or deleted." -ForegroundColor Green
    $confirmation = Read-Host "Type DELETE MCU INSTALL to continue"
    if ($confirmation -cne "DELETE MCU INSTALL") {
        Write-Host "Cancelled. Nothing was changed." -ForegroundColor Yellow
        exit 2
    }
}
else {
    Write-Host ""
    Write-Host "Project reset removes the private env, app-specific PlatformIO/toolchains, caches, and state." -ForegroundColor Yellow
    Write-Host "It does not uninstall shared Windows applications." -ForegroundColor Yellow
    $confirmation = Read-Host "Type RESET MCU PROJECT to continue"
    if ($confirmation -cne "RESET MCU PROJECT") {
        Write-Host "Cancelled. Nothing was changed." -ForegroundColor Yellow
        exit 2
    }
}
Write-Host ""
Initialize-Progress $(if ($FullReset) { 14 } else { 5 })

try {
    Start-Step "Stopping MCU Flasher-owned processes..."
    Stop-AppOwnedProcesses
    if (-not $DryRun) { Start-Sleep -Milliseconds 300 }
    Complete-Step "MCU Flasher-owned processes stopped."

    Start-Step "Removing project-local runtime folders..."
    $projectRuntimeTargets = @(
        $envDir,
        (Join-Path $projectRoot ".pio"),
        (Join-Path $projectRoot ".cache"),
        (Join-Path $projectRoot "__pycache__"),
        (Join-Path $projectRoot "logs")
    )
    $projectRuntimeTargets += Get-ChildItem -LiteralPath $projectRoot -Directory -Filter "env.incompatible-*" -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
    foreach ($target in $projectRuntimeTargets) { Remove-DirectoryForce $target }
    Complete-Step "Project-local runtime folders removed."

    Start-Step "Removing app-specific PlatformIO and board toolchains..."
    foreach ($target in @(
        (Join-Path $projectRoot ".pio_cache"),
        (Join-Path $projectRoot "src\_board-frameworks\.platformio"),
        (Join-Path $env:LOCALAPPDATA ".pio-mcu"),
        (Join-Path $env:LOCALAPPDATA ".platformio-mcu-gui"),
        "C:\.platformio-mcu-gui",
        (Join-Path $env:TEMP ".platformio-mcu-gui")
    )) { Remove-DirectoryForce $target }
    Complete-Step "App-specific PlatformIO and toolchains removed."

    Start-Step "Removing reset caches, app state, and default board downloads..."
    foreach ($target in @(
        (Join-Path $projectRoot "soft_reset_project\boards"),
        (Join-Path $projectRoot "soft_reset_project\.pio"),
        (Join-Path $projectRoot "soft_reset_project_uno\boards"),
        (Join-Path $projectRoot "soft_reset_project_uno\.pio"),
        (Join-Path $env:TEMP "mcu_flash_gui_cache"),
        (Join-Path $env:USERPROFILE ".mcu_flash_gui"),
        (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "_MCUFlasherByNaph_src")
    )) { Remove-DirectoryForce $target }
    Complete-Step "Reset caches, app state, and default board downloads removed."

    Start-Step "Removing generated bootstrap pointer and temporary files..."
    foreach ($file in @(
        (Join-Path $projectRoot "arduino_cli_path.txt"),
        (Join-Path $projectRoot ".force_rebuild"),
        (Join-Path $projectRoot "src\gui_config.json"),
        (Join-Path $env:USERPROFILE ".mcu_gui_config.json"),
        (Join-Path $env:TEMP "arduino_cli_msi_install.log"),
        (Join-Path $env:TEMP "py_exec_path.txt"),
        (Join-Path $env:TEMP "winget_py_search.txt")
    )) { Remove-FileForce $file }
    Complete-Step "Generated bootstrap and temporary files removed."

    if ($FullReset) {
        $uninstallEntries = Get-AllUninstallEntries

        Start-Step "Removing OpenCode CLI..."
        Stop-ProcessesByName @("opencode")
        Remove-OpenCodeCli
        Complete-Step "OpenCode CLI removed or not present."

        Start-Step "Removing Arduino CLI..."
        Stop-ProcessesByName @("arduino-cli")
        $uninstallEntries = Get-AllUninstallEntries
        Remove-MsiProducts "(?i)^Arduino CLI" "Arduino CLI" $uninstallEntries
        Remove-DirectoryForce (Join-Path $env:LOCALAPPDATA "Arduino15")
        Complete-Step "Arduino CLI and Arduino15 package store removed or not present."

        Start-Step "Removing Python used by the full bootstrap environment..."
        Stop-ProcessesByName @("python", "pythonw", "python3", "py")
        Remove-PythonBootstrapPackages
        Complete-Step "Python bootstrap environment removed or not present."

        Start-Step "Removing Node.js installed for OpenCode..."
        Stop-ProcessesByName @("npm", "npx", "node")
        $uninstallEntries = Get-AllUninstallEntries
        Remove-MsiProducts "(?i)^Node\.js" "Node.js" $uninstallEntries
        Complete-Step "Node.js removed or not present."

        Start-Step "Clearing bootstrap package-manager caches..."
        foreach ($target in @(
            (Join-Path $env:LOCALAPPDATA "pip\Cache"),
            (Join-Path $env:LOCALAPPDATA "npm-cache"),
            (Join-Path $env:APPDATA "npm-cache")
        )) { Remove-DirectoryForce $target }
        Complete-Step "pip/npm caches removed or not present."

        Start-Step "Removing Microsoft Edge WebView2 Runtime..."
        Stop-ProcessesByName @("msedgewebview2")
        Remove-WebView2Runtime
        Complete-Step "WebView2 Runtime removed or not present."

        Start-Step "Removing CP210x driver packages..."
        Remove-Cp210xDriver
        Complete-Step "CP210x driver removed or not present."

        Start-Step "Removing opt-in Windows Defender exclusions..."
        Remove-DefenderExclusions
        Complete-Step "MCU Flasher Defender exclusions removed or not present."

        Start-Step "Verifying the full Windows dependency reset..."
        if (-not $DryRun) {
            $remaining = Get-UninstallProducts "(?i)^Python (?:3\.|Launcher)|^Arduino CLI|^Node\.js|Microsoft Edge WebView2 Runtime" (Get-AllUninstallEntries)
            if ($remaining) {
                $names = ($remaining | ForEach-Object { $_.DisplayName } | Sort-Object -Unique) -join ", "
                Write-Bad "Still registered after reset: $names"
            }
            $leftovers = $script:approvedDirectoryTargets | Where-Object { Test-Path -LiteralPath $_ }
            foreach ($leftover in $leftovers) { Write-Bad "Approved reset target still exists: $leftover" }
        }
        Complete-Step "Full Windows dependency reset verified."
    }

    Write-Host ""
    $elapsed = "{0:mm\:ss}" -f $sw.Elapsed
    if ($DryRun) {
        Write-Host "Dry run complete in $elapsed. No changes were made." -ForegroundColor Green
        exit 0
    }
    if ($script:warningCount -gt 0) {
        Write-Host "Reset incomplete after ${elapsed}: $($script:warningCount) item(s) need attention." -ForegroundColor Red
        exit 1
    }
    if ($FullReset) {
        Write-Host "Full reset complete in $elapsed. Run runThisOnWindows.vbs to reinstall everything." -ForegroundColor Green
    }
    else {
        Write-Host "Env reset complete in $elapsed. Run bootstrap.py to reinstall app packages." -ForegroundColor Green
    }
    exit 0
}
catch {
    Show-ProgressBar "Reset failed."
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Re-run the script - remaining items often clear once locking processes are closed and Windows Installer finishes its previous job." -ForegroundColor DarkYellow
    exit 1
}
finally {
    Write-Progress -Activity "MCU Flasher Bootstrap Reset" -Completed
    if (-not $NoPause) { Read-Host "Press Enter to close" }
}
