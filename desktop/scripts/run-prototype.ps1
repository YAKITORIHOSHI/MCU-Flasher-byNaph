$ErrorActionPreference = 'Stop'
$prototypeArguments = [string[]] $args
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
    Invoke-Checked pnpm @('--dir', 'desktop/ui', 'install', '--frozen-lockfile')
    Invoke-Checked pnpm @('--dir', 'desktop/ui', 'build')
    Invoke-Checked cargo @('build', '--manifest-path', 'desktop/Cargo.toml', '--locked', '-p', 'mcu-desktop-prototype')
    Invoke-Checked 'desktop/target/debug/mcu-desktop-prototype.exe' $prototypeArguments
} finally {
    [Environment]::SetEnvironmentVariable('RUSTUP_TOOLCHAIN', $previousToolchain, 'Process')
    [Environment]::SetEnvironmentVariable('CARGO_BUILD_JOBS', $previousJobs, 'Process')
    [Environment]::SetEnvironmentVariable('CARGO_TARGET_DIR', $previousTargetDir, 'Process')
    Pop-Location
}
