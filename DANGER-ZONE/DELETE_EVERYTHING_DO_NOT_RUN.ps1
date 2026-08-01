param([switch]$FullReset)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# The env folder is in the project root (one level up from DANGER-ZONE)
$projectRoot = Split-Path -Parent $scriptDir
$envDir = Join-Path $projectRoot "env"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$script:warningCount = 0

# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------
function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath) { $scriptPath = $MyInvocation.MyCommand.Path }
    Write-Host "Requesting Administrator privileges..." -ForegroundColor Yellow
    try {
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
        if ($FullReset) { $arguments += " -FullReset" }
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
    foreach ($name in $Names) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

function Remove-DirectoryForce([string]$Path) {
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

# ---------------------------------------------------------------------------
# Process execution with MSI-aware retry
# ---------------------------------------------------------------------------
function Invoke-ProcessOnce([string]$FilePath, [string[]]$Arguments) {
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
        return [pscustomobject]@{ Success = ($process.ExitCode -in @(0, 3010)); ExitCode = $process.ExitCode }
    }
    catch {
        return [pscustomobject]@{ Success = $false; ExitCode = -1 }
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

function Invoke-Process([string]$FilePath, [string[]]$Arguments, [switch]$IsMsi) {
    $maxAttempts = 3
    $result = $null
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        if ($IsMsi) { Wait-MsiMutex }
        $result = Invoke-ProcessOnce $FilePath $Arguments
        if ($result.Success) { return $result }
        if ($IsMsi -and $result.ExitCode -eq 1618 -and $attempt -lt $maxAttempts) {
            Write-Info "Windows Installer busy (1618), retrying in $($attempt * 3)s..."
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

function Convert-PSPathToRegPath([string]$PSPath) {
    $path = $PSPath -replace '^Microsoft\.PowerShell\.Core\\Registry::', ''
    $path = $path -replace '^HKEY_LOCAL_MACHINE', 'HKLM'
    $path = $path -replace '^HKEY_CURRENT_USER', 'HKCU'
    $path = $path -replace '^HKEY_CLASSES_ROOT', 'HKCR'
    $path = $path -replace '^HKEY_USERS', 'HKU'
    return $path
}

function Remove-ProductRegistration($Product) {
    try {
        Remove-Item -LiteralPath $Product.PSPath -Recurse -Force -ErrorAction Stop
        Write-Ok "Cleared stale registration for $($Product.DisplayName)."
        return $true
    }
    catch {
        # PowerShell's registry provider sometimes refuses keys that reg.exe
        # (running elevated) can still delete. Worth a second try before
        # giving up.
        $regPath = Convert-PSPathToRegPath $Product.PSPath
        $result = Invoke-ProcessOnce "reg.exe" @("delete", "`"$regPath`"", "/f")
        if ($result.Success) {
            Write-Ok "Cleared stale registration for $($Product.DisplayName) (via reg.exe)."
            return $true
        }
        Write-Bad "Could not clear the registration for $($Product.DisplayName)."
        return $false
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
        }
        elseif ($plain) {
            $result = Invoke-Process "cmd.exe" @('/d', '/s', '/c', "`"$plain`"")
        }
        else {
            Write-Info "$($product.DisplayName) has no uninstaller; applying cleanup fallback."
            if (Remove-ProductRegistration $product) {
                Write-Ok "$($product.DisplayName) marked removed."
            }
            continue
        }

        if ($result.Success) {
            Write-Ok "$($product.DisplayName) removed."
        }
        else {
            # An MSI 1603 or WebView installer 93 often means its files were
            # already gone. Clear that stale record and let the category's
            # file cleanup decide the final outcome instead of noisy failure.
            Write-Info "$($product.DisplayName) uninstaller unavailable (code $($result.ExitCode)); applying cleanup fallback."
            if (Remove-ProductRegistration $product) {
                Write-Ok "$($product.DisplayName) marked removed."
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

    # Edge Update can recreate/hold WebView files immediately after its
    # uninstaller exits. Stop both services and retry the scoped deletion
    # before declaring the reset incomplete.
    Get-Service -Name "edgeupdate", "edgeupdatem" -ErrorAction SilentlyContinue |
    Stop-Service -Force -ErrorAction SilentlyContinue
    Stop-ProcessesByName @("msedgewebview2", "msedge")

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

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Write-Host ""
Write-Step "MCU Flasher Bootstrap Reset (Administrator)"
Write-Host "This removes app dependencies so bootstrap.py can be tested from scratch."

if (-not $FullReset) {
    Write-Host ""
    Write-Host "FULL RESET also uninstalls system-wide Python, Arduino CLI, Microsoft Edge WebView2 Runtime, and the CP210x driver." -ForegroundColor Yellow
    Write-Host "These may be used by other applications."
    $confirmation = Read-Host "Type FULL RESET to continue, or press Enter for env-only reset"
    $FullReset = $confirmation -eq "FULL RESET"
}
Write-Host ""
Initialize-Progress $(if ($FullReset) { 7 } else { 3 })

try {
    Start-Step "Stopping processes that might lock the env folder..."
    Stop-ProcessesByName @("python", "pythonw", "python3", "py", "platformio", "pio")
    Start-Sleep -Milliseconds 500
    Complete-Step "Processes stopped."

    Start-Step "Removing the app virtual environment..."
    $envRemoved = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Remove-DirectoryForce $envDir
        if (-not (Test-Path -LiteralPath $envDir)) {
            $envRemoved = $true
            break
        }
        if ($attempt -lt 3) {
            Write-Info "env folder still locked - retrying in ${attempt}s..."
            Stop-ProcessesByName @("python", "pythonw", "python3", "py", "platformio", "pio")
            Start-Sleep -Seconds $attempt
        }
    }
    if ($envRemoved) {
        Complete-Step "App virtual environment removed."
    } else {
        $script:warningCount++
        Complete-Step "App virtual environment could not be fully removed (some files may still be locked)."
    }

    if ($FullReset) {
        # Scan the registry once and reuse it for every category below
        # instead of re-enumerating three hives per product type.
        $uninstallEntries = Get-AllUninstallEntries

        Start-Step "Removing Python installations..."
        # A running python.exe/pythonw.exe can hold DLLs open and make the
        # uninstaller fail outright (or leave files behind) even when the
        # uninstall command itself is correct.
        Stop-ProcessesByName @("python*", "py")
        Remove-MsiProducts "(?i)^Python" "Python" $uninstallEntries
        Remove-DirectoryForce (Join-Path $env:LOCALAPPDATA "Programs\Python")
        foreach ($pythonRoot in @(
                [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles),
                [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
            )) {
            Get-ChildItem -LiteralPath $pythonRoot -Directory -Filter "Python*" -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-DirectoryForce $_.FullName }
        }
        Complete-Step "Python installations removed or not present."

        Start-Step "Removing Arduino CLI..."
        Stop-ProcessesByName @("arduino-cli")
        Remove-MsiProducts "(?i)^Arduino CLI" "Arduino CLI" $uninstallEntries
        Complete-Step "Arduino CLI removed or not present."

        Start-Step "Removing Microsoft Edge WebView2 Runtime..."
        Stop-ProcessesByName @("msedgewebview2")
        Remove-MsiProducts "(?i)Microsoft Edge WebView2 Runtime" "WebView2 Runtime" $uninstallEntries
        Remove-WebView2Runtime
        Complete-Step "WebView2 Runtime removed or not present."

        Start-Step "Removing CP210x driver packages..."
        Remove-Cp210xDriver
        Complete-Step "CP210x driver removed or not present."
    }

    Start-Step "Cleaning up local files..."
    Remove-Item -LiteralPath (Join-Path $projectRoot "arduino_cli_path.txt") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $projectRoot ".force_rebuild") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $projectRoot "logs\.cp210x_installed") -Force -ErrorAction SilentlyContinue
    Complete-Step "Local cleanup complete."

    if ($FullReset) {
        $remaining = Get-UninstallProducts "(?i)^Python|^Arduino CLI|Microsoft Edge WebView2 Runtime" (Get-AllUninstallEntries)
        if ($remaining) {
            $names = ($remaining | ForEach-Object { $_.DisplayName } | Sort-Object -Unique) -join ", "
            throw "Full reset is incomplete. Still registered: $names"
        }
        $webviewGuid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        $webviewKeys = @(
            "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$webviewGuid",
            "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$webviewGuid",
            "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$webviewGuid"
        )
        if ($webviewKeys | Where-Object { Test-Path -LiteralPath $_ }) {
            throw "Full reset is incomplete. WebView2 is still registered in EdgeUpdate."
        }
        $webviewFolders = @(
            (Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)) "Microsoft\EdgeWebView"),
            (Join-Path $env:LOCALAPPDATA "Microsoft\EdgeWebView")
        )
        if ($webviewFolders | Where-Object { Test-Path -LiteralPath $_ }) {
            throw "Full reset is incomplete. WebView2 runtime files still exist."
        }
    }

    Write-Host ""
    $elapsed = "{0:mm\:ss}" -f $sw.Elapsed
    if ($script:warningCount -gt 0) {
        Write-Host "Completed in $elapsed with $($script:warningCount) warning(s) above - re-check those items manually if they still matter." -ForegroundColor Yellow
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
    Read-Host "Press Enter to close"
}
