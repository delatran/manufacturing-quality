$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        throw "Run .\scripts\setup.ps1 first to create the Python environment."
    }
    if (-not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
        throw "Run npm ci and npm run build in frontend first, or run .\scripts\setup.ps1."
    }
    Write-Host "Starting the analyst workspace at http://127.0.0.1:8000. Press Ctrl+C to stop."
    & .\.venv\Scripts\python.exe -m analyst
    if ($LASTEXITCODE -ne 0) { throw "Application stopped with exit code $LASTEXITCODE." }
} finally { Pop-Location }
