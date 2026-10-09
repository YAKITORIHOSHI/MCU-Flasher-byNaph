[CmdletBinding()]
param(
    [ValidateSet('Bytecode', 'Fresh', 'Runtime', 'Stop')][string]$Mode = 'Bytecode',
    [string]$ProjectRoot,
    [switch]$Apply,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$NoPause
)

# Native Windows maintenance. Dot-source to use the same checked plan in reset.
# No app imports, downloads, recovery cleanup, or inherited PlatformIO paths.
$script:McuProtectedNames = @('.git', '.mcu_flasher_build_cache', '.mcu_ai_edits',
    '.pio', '.vscode', '.clangd', '.opencode', '.venv-linux', '.ubuntu-tools',
    '_python', 'env', 'node_modules', 'offline-extras', '.platformio-mcu-gui')

function New-McuBudget([int]$Seconds) {
    return [pscustomobject]@{ Remaining = 250000; Deadline = [DateTime]::UtcNow.AddSeconds($Seconds) }
}

function Assert-McuBudget($Budget) {
    $Budget.Remaining--
    if ($Budget.Remaining -lt 0 -or [DateTime]::UtcNow -gt $Budget.Deadline) {
        throw 'Maintenance limit reached. Remaining files were retained.'
    }
}

function Get-McuFullPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'An empty maintenance path is invalid.' }
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -eq [IO.Path]::GetPathRoot($full)) { return $full }
    return $full.TrimEnd([IO.Path]::DirectorySeparatorChar)
}

function Test-McuWithin([string]$Path, [string]$Root) {
    $pathValue = Get-McuFullPath $Path
    $rootValue = Get-McuFullPath $Root
    $prefix = $rootValue.TrimEnd([IO.Path]::DirectorySeparatorChar) + '\'
    return $pathValue.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Test-McuReparse($Item) {
    return [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Assert-McuProject([string]$Root) {
    $rootValue = Get-McuFullPath $Root
    if ($rootValue -eq [IO.Path]::GetPathRoot($rootValue)) { throw 'A drive root cannot be an application maintenance root.' }
    if (-not (Test-Path -LiteralPath (Join-Path $rootValue 'mcu_flash_gui.py') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $rootValue 'direct/windows/run.vbs') -PathType Leaf)) {
        throw 'This is not an MCU Flasher application directory.'
    }
    if (Test-McuReparse (Get-Item -LiteralPath $rootValue -Force -ErrorAction Stop)) {
        throw 'Use the real application directory, rather than a linked root.'
    }
    return $rootValue
}

function Assert-McuParents([string]$Path, [string]$Root) {
    if (-not (Test-McuWithin $Path $Root)) { throw "Target leaves its maintenance root: $Path" }
    $parent = [IO.Path]::GetDirectoryName((Get-McuFullPath $Path))
    while ($parent) {
        if (Test-Path -LiteralPath $parent) {
            if (Test-McuReparse (Get-Item -LiteralPath $parent -Force -ErrorAction Stop)) {
                throw "Linked maintenance parent was retained: $parent"
            }
        }
        if ((Get-McuFullPath $parent) -eq (Get-McuFullPath $Root)) { return }
        $parent = [IO.Path]::GetDirectoryName($parent)
    }
    throw 'The maintenance root could not be verified.'
}

function Get-McuAliasBases([string]$ProjectRoot) {
    $rootValue = Get-McuFullPath $ProjectRoot
    $bases = @([IO.Path]::GetPathRoot($rootValue))
    $local = [Environment]::GetFolderPath('LocalApplicationData')
    if ($local) { $bases += $local }
    return @($bases | Select-Object -Unique)
}

function Get-McuWindowsAliases([string]$ProjectRoot) {
    $rootValue = Get-McuFullPath $ProjectRoot
    foreach ($base in (Get-McuAliasBases $rootValue)) {
        foreach ($relative in @('.platformio-mcu-gui', '.mcuflasher-app/.platformio-mcu-gui', '.mcuflasher-app/.mcuflasher-libs')) {
            $path = Join-Path $base $relative
            $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            if (-not $item -or -not (Test-McuReparse $item)) { continue }
            $target = @($item.Target)
            if ($target.Count -ne 1 -or -not $target[0]) { continue }
            $destination = [string]$target[0]
            if ($destination.StartsWith('\??\')) { $destination = $destination.Substring(4) }
            if (-not [IO.Path]::IsPathRooted($destination)) {
                $destination = Join-Path $item.Parent.FullName $destination
            }
            $destination = Get-McuFullPath $destination
            $allowed = @('src/.platformio-mcu-gui', 'src/_board-frameworks/.platformio', 'src/modules', 'src/libs') |
                ForEach-Object { Get-McuFullPath (Join-Path $rootValue $_) }
            if ($allowed -contains $destination) {
                [pscustomobject]@{ Path = $item.FullName; Root = $base; Kind = 'Alias'; Target = $destination }
            }
        }
    }
}

function Test-McuOwnedCommand([string]$Command, [string]$ProjectRoot) {
    $arguments = @([regex]::Matches($Command, '"[^"]*"|[^\s]+') | ForEach-Object { $_.Value.Trim('"') })
    if ($arguments.Count -lt 2) { return $false }
    $position = 1
    while ($position -lt $arguments.Count -and $arguments[$position] -in @('-B', '-u', '-I', '-S', '-s', '-E', '-O', '-OO', '-q')) { $position++ }
    if ($position -ge $arguments.Count) { return $false }
    # Require the entry script itself, rather than a root in an unrelated argument.
    $entries = @('mcu_flash_gui.py', 'mcu_flash_web.py', 'launcher.py', 'main/mcu_flash_gui.py',
        'direct/launcher.py', 'direct/windows/launcher.py', 'direct/windows/launch.py',
        'src/modules/bootstrap.py', 'src/modules/offline_bootstrap.py', 'src/modules/board_preparation.py') |
        ForEach-Object { Get-McuFullPath (Join-Path $ProjectRoot $_) }
    if (-not [IO.Path]::IsPathRooted($arguments[$position])) { return $false }
    return $entries -contains (Get-McuFullPath $arguments[$position])
}

function Get-McuOwnedProcesses([string]$ProjectRoot) {
    $rootValue = Get-McuFullPath $ProjectRoot
    $prefixes = @($rootValue) + @(Get-McuWindowsAliases $rootValue | ForEach-Object Path)
    $rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    if ($rows.Count -gt 16384) { throw 'Process inventory exceeds the maintenance limit.' }
    $owned = @{}
    foreach ($row in $rows) {
        if ($row.ProcessId -eq $PID) { continue }
        $exe = [string]$row.ExecutablePath
        $command = [string]$row.CommandLine
        $matches = $false
        foreach ($prefix in $prefixes) {
            if ($exe -and (Test-McuWithin $exe $prefix)) { $matches = $true; break }
        }
        if (-not $matches) { $matches = Test-McuOwnedCommand $command $rootValue }
        if ($matches) { $owned[[int]$row.ProcessId] = $row }
    }
    # Include subprocesses of captured owners even when their executable is global.
    do {
        $changed = $false
        foreach ($row in $rows) {
            if ($row.ProcessId -ne $PID -and -not $owned.ContainsKey([int]$row.ProcessId) -and
                $owned.ContainsKey([int]$row.ParentProcessId)) {
                $owned[[int]$row.ProcessId] = $row
                $changed = $true
            }
        }
    } while ($changed)
    return @($owned.Values)
}

function Assert-McuWindowsIdle([string]$ProjectRoot) {
    if (@(Get-McuOwnedProcesses $ProjectRoot).Count) {
        throw 'Close MCU Flasher, Bootstrap, terminals and package operations before maintenance. No process was stopped.'
    }
}

function Get-McuBytecodePlan([string]$ProjectRoot) {
    $rootValue = Get-McuFullPath $ProjectRoot
    $budget = New-McuBudget 30
    $roots = @('main', 'src/modules', 'src/dbs', 'direct', 'cleaner', 'DANGER-ZONE') |
        ForEach-Object { Join-Path $rootValue $_ }
    $roots += $rootValue
    foreach ($sourceRoot in $roots) {
        if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) { continue }
        if ($sourceRoot -ne $rootValue) { Assert-McuParents $sourceRoot $rootValue }
        $stack = [Collections.Generic.Stack[string]]::new()
        $stack.Push($sourceRoot)
        while ($stack.Count) {
            $directory = $stack.Pop()
            $item = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
            if (Test-McuReparse $item) { continue }
            foreach ($child in Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) {
                Assert-McuBudget $budget
                if (Test-McuReparse $child) { continue }
                if ($child.PSIsContainer) {
                    if ($script:McuProtectedNames -contains $child.Name) { continue }
                    if ($directory -eq $rootValue -and $child.Name -ne '__pycache__') { continue }
                    $stack.Push($child.FullName)
                } elseif ($child.Extension -in @('.pyc', '.pyo')) {
                    [pscustomobject]@{ Path = $child.FullName; Root = $rootValue; Kind = 'Bytecode' }
                }
            }
            if ($item.Name -eq '__pycache__') {
                [pscustomobject]@{ Path = $item.FullName; Root = $rootValue; Kind = 'EmptyCache' }
            }
        }
    }
}

function Get-McuWindowsPlan([string]$ProjectRoot, [string]$Mode) {
    $rootValue = Assert-McuProject $ProjectRoot
    if ($Mode -eq 'Stop') { return }
    if ($Mode -ne 'Bytecode') { Get-McuWindowsAliases $rootValue }
    Get-McuBytecodePlan $rootValue
    if ($Mode -eq 'Bytecode') { return }
    foreach ($relative in @('env', 'src/env', 'src/.platformio-mcu-gui', 'src/_board-frameworks/.platformio', 'src/offline-extras/win32')) {
        $path = Join-Path $rootValue $relative
        if (-not (Test-Path -LiteralPath $path)) { continue }
        Assert-McuParents $path $rootValue
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (-not $item.PSIsContainer -or (Test-McuReparse $item)) { throw "Linked or invalid resource root was retained: $path" }
        if ($relative -in @('env', 'src/env') -and
            (-not (Test-Path -LiteralPath (Join-Path $path 'pyvenv.cfg') -PathType Leaf) -or
             -not (Test-Path -LiteralPath (Join-Path $path 'Scripts/python.exe') -PathType Leaf))) {
            throw "Unrecognized virtual environment was retained: $path"
        }
        if ($relative -eq 'src/offline-extras/win32') {
            $ownerFile = Get-Item -LiteralPath (Join-Path $path '.mcu-offline-extras.json') -Force -ErrorAction Stop
            if ($ownerFile.Length -gt 8192 -or (Test-McuReparse $ownerFile)) { throw 'Offline extras ownership is invalid.' }
            $owner = Get-Content -LiteralPath $ownerFile.FullName -Raw | ConvertFrom-Json
            if ($owner.schema -ne 1 -or $owner.host -ne 'win32' -or
                $owner.installation -ne $rootValue.ToLowerInvariant() -or
                $owner.purpose -ne 'MCU Flasher offline preparation extras') { throw 'Offline extras belong to another installation or host.' }
            foreach ($child in Get-ChildItem -LiteralPath $path -Force) {
                if ($child.Name -notin @('.mcu-offline-extras.json', 'readiness.json', 'archives')) { throw 'Unknown offline extras were retained.' }
            }
        }
        [pscustomobject]@{ Path = $path; Root = $rootValue; Kind = 'Tree' }
    }
    if ($Mode -eq 'Runtime') {
        $path = Join-Path $rootValue 'src/_python'
        if (Test-Path -LiteralPath $path) {
            Assert-McuParents $path $rootValue
            if ((Test-McuReparse (Get-Item -LiteralPath $path -Force)) -or
                -not (Test-Path -LiteralPath (Join-Path $path 'python.exe') -PathType Leaf)) { throw 'Unrecognized private Python directory was retained.' }
            [pscustomobject]@{ Path = $path; Root = $rootValue; Kind = 'Tree' }
        }
    }
}

function Remove-McuTree([string]$Path) {
    # The caller already checked the absolute deletion root. Never traverse links.
    Assert-McuBudget $script:McuDeletionBudget
    if ([DateTime]::UtcNow -ge $script:McuNextProcessCheck) {
        Assert-McuWindowsIdle $script:McuDeletionRoot
        $script:McuNextProcessCheck = [DateTime]::UtcNow.AddSeconds(1)
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (Test-McuReparse $item) {
        if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName, $false) }
        else { Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop }
        return
    }
    if ($item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop) { Remove-McuTree $child.FullName }
        [IO.Directory]::Delete($Path, $false)
    } else {
        Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
}

function Invoke-McuWindowsMaintenance {
    param([string]$ProjectRoot, [ValidateSet('Bytecode', 'Fresh', 'Runtime', 'Stop')][string]$Mode,
          [switch]$Apply, [switch]$DryRun, [switch]$Force)
    $rootValue = Assert-McuProject $ProjectRoot
    if ($Apply -and -not $DryRun -and [Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Use the native Ubuntu maintenance utilities on Linux.' }
    $plan = @(Get-McuWindowsPlan $rootValue $Mode)
    $processPlan = @()
    Write-Host "Windows maintenance: $Mode ($rootValue)"
    foreach ($entry in $plan) { Write-Host ("  {0}: {1}" -f $entry.Kind, $entry.Path) }
    if ($Mode -eq 'Stop') {
        $processPlan = @(Get-McuOwnedProcesses $rootValue)
        foreach ($row in $processPlan) { Write-Host "  Stop captured MCU Flasher process: $($row.ProcessId) $($row.Name)" }
    }
    if (-not $Apply -or $DryRun) { Write-Host 'Preview only. Use -Apply to perform this plan.'; return }
    if ($Mode -ne 'Stop') { Assert-McuWindowsIdle $rootValue }
    if (-not $Force) {
        $phrase = if ($Mode -eq 'Stop') { 'STOP MCU FLASHER' } elseif ($Mode -eq 'Runtime') { 'RESET MCU WINDOWS' } else { 'CLEAN MCU WINDOWS' }
        if ($Mode -eq 'Stop') { Write-Host 'An active write may be interrupted. Queued commands will not be replayed.' }
        if ((Read-Host "Type $phrase to continue") -cne $phrase) { throw 'Cancelled. Nothing was changed.' }
    }
    if ($Mode -eq 'Stop') {
        foreach ($row in ($processPlan | Sort-Object CreationDate -Descending)) {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($row.ProcessId)" -ErrorAction Stop
            if ($current -and $current.CreationDate -eq $row.CreationDate) { Stop-Process -Id $row.ProcessId -Force -ErrorAction Stop }
        }
        Assert-McuWindowsIdle $rootValue
        return
    }
    Assert-McuWindowsIdle $rootValue
    # Recheck venv markers, extras ownership and the entire reviewed target set.
    $freshPlan = @(Get-McuWindowsPlan $rootValue $Mode)
    $reviewed = @($plan | ForEach-Object { "$($_.Kind)|$($_.Path)|$($_.Target)" } | Sort-Object)
    $current = @($freshPlan | ForEach-Object { "$($_.Kind)|$($_.Path)|$($_.Target)" } | Sort-Object)
    if ($reviewed.Count -ne $current.Count -or ($reviewed -join "`n") -cne ($current -join "`n")) {
        throw 'The maintenance plan changed after preview. Nothing was removed; review it again.'
    }
    # Validate every target before performing even the first removal.
    foreach ($entry in $plan) {
        Assert-McuParents $entry.Path $entry.Root
        $item = Get-Item -LiteralPath $entry.Path -Force -ErrorAction Stop
        if ($entry.Kind -eq 'Alias') {
            if (-not (Test-McuReparse $item) -or
                -not (@(Get-McuWindowsAliases $rootValue | Where-Object Path -eq $entry.Path).Count)) { throw 'An alias changed after preview; nothing was removed.' }
        } elseif (Test-McuReparse $item) { throw 'A cleanup target became linked; nothing was removed.' }
    }
    $script:McuDeletionBudget = New-McuBudget 120
    $script:McuDeletionRoot = $rootValue
    $script:McuNextProcessCheck = [DateTime]::UtcNow.AddSeconds(1)
    foreach ($entry in $plan) {
        Assert-McuBudget $script:McuDeletionBudget
        if ([DateTime]::UtcNow -ge $script:McuNextProcessCheck) {
            Assert-McuWindowsIdle $rootValue
            $script:McuNextProcessCheck = [DateTime]::UtcNow.AddSeconds(1)
        }
        Assert-McuParents $entry.Path $entry.Root
        if ($entry.Kind -eq 'Alias') {
            if (-not (@(Get-McuWindowsAliases $rootValue | Where-Object Path -eq $entry.Path).Count)) { throw 'Alias ownership changed during cleanup.' }
            [IO.Directory]::Delete($entry.Path, $false)
        }
        elseif ($entry.Kind -eq 'Tree') { Remove-McuTree $entry.Path }
        elseif ($entry.Kind -eq 'EmptyCache') {
            if (-not @(Get-ChildItem -LiteralPath $entry.Path -Force -ErrorAction Stop).Count) { [IO.Directory]::Delete($entry.Path, $false) }
        }
        else { Remove-Item -LiteralPath $entry.Path -Force -ErrorAction Stop }
    }
    Write-Host 'Maintenance completed. Sketches, settings, logs, recovery and Ubuntu resources were preserved.'
    if ($Mode -ne 'Bytecode') { Write-Host 'Launch direct/windows/run.vbs while online to prepare Windows resources again.' }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        if (-not $ProjectRoot) { $ProjectRoot = Join-Path $PSScriptRoot '../..' }
        Invoke-McuWindowsMaintenance -ProjectRoot $ProjectRoot -Mode $Mode -Apply:$Apply -DryRun:$DryRun -Force:$Force
    } catch { Write-Error $_ -ErrorAction Continue; exit 1 }
    finally { if (-not $NoPause) { [void](Read-Host 'Press Enter to close') } }
}
