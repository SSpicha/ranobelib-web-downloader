# Run the Telegram bot locally (PowerShell / Windows).
# Usage:  powershell -ExecutionPolicy Bypass -File bot/run_local.ps1
# This is an alternative to run_local.sh (same kill-first behavior).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $root "..")   # project root

# Kill any running bot/ui.py instance (single-instance enforcement).
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*bot/ui.py*'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
} 2>$null
# (fallback: also kill by CommandLine match)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*bot/ui.py*'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } 2>$null
Start-Sleep -Seconds 1

# Load .env into the current process environment.
$envFile = Join-Path $root "bot\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
        }
    }
}

Write-Host "Starting bot (local)..."
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
& $venvPy (Join-Path $root "bot\ui.py")
