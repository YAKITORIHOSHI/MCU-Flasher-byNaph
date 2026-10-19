# Native Windows counterpart to verify-local.sh. No serial/cloud/live projects.
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$previousToolchain = $env:RUSTUP_TOOLCHAIN
$previousJobs = $env:CARGO_BUILD_JOBS
$previousTargetDir = $env:CARGO_TARGET_DIR
function Invoke-Checked {
    param([string]$Tool, [string[]]$Arguments)
    & $Tool @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Tool failed with exit code $LASTEXITCODE" }
}
Push-Location -LiteralPath $repoRoot
try {
    $env:RUSTUP_TOOLCHAIN = '1.99.1'
    $env:CARGO_TARGET_DIR = Join-Path $repoRoot 'desktop/target'
    if (-not $env:CARGO_BUILD_JOBS) { $env:CARGO_BUILD_JOBS = '4' }
    New-Item -ItemType Directory -Force -Path 'audit/migration/logs', 'audit/migration/scratch' | Out-Null
    Invoke-Checked pnpm @('--dir', 'desktop/ui', 'install', '--frozen-lockfile')
    Invoke-Checked pnpm @('--dir', 'desktop/ui', 'test')
    Invoke-Checked pnpm @('--dir', 'desktop/ui', 'build')
    Invoke-Checked cargo @('fmt', '--manifest-path', 'desktop/Cargo.toml', '--all', '--', '--check')
    Invoke-Checked cargo @('test', '--manifest-path', 'desktop/Cargo.toml', '--locked')
    Invoke-Checked cargo @('clippy', '--manifest-path', 'desktop/Cargo.toml', '--workspace', '--all-targets', '--locked', '--', '-D', 'warnings')
    Invoke-Checked cargo @('build', '--manifest-path', 'desktop/Cargo.toml', '--locked', '-p', 'mcu-host', '--bins')
    Invoke-Checked cargo @('run', '--manifest-path', 'desktop/Cargo.toml', '--locked', '-p', 'mcu-host', '--bin', 'pty_probe')
    foreach ($probe in @('verify-serial-codec.py', 'verify-serial-send.py', 'verify-preferences-merge.py', 'verify-scaffold.py', 'verify-resource-budget.py', 'verify-lock-interop.py', 'verify-target-transport.py', 'verify-ownership.py', 'verify-process-identity.py', 'verify-file-identity.py', 'verify-registry.py')) {
        Invoke-Checked python @('-B', '-W', 'error', "desktop/scripts/$probe")
    }
    Invoke-Checked cargo @('build', '--manifest-path', 'desktop/Cargo.toml', '--locked', '-p', 'mcu-desktop-prototype')
    Invoke-Checked 'desktop/target/debug/mcu-desktop-prototype.exe' @('--verify-prototype')
} finally {
    [Environment]::SetEnvironmentVariable('RUSTUP_TOOLCHAIN', $previousToolchain, 'Process')
    [Environment]::SetEnvironmentVariable('CARGO_BUILD_JOBS', $previousJobs, 'Process')
    [Environment]::SetEnvironmentVariable('CARGO_TARGET_DIR', $previousTargetDir, 'Process')
    Pop-Location
}
