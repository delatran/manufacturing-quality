param([string]$Python = "python", [switch]$RuntimeOnly)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & $Python -c "import sys; assert sys.version_info[:2] == (3, 12), 'Use Python 3.12 for the committed dependency locks.'"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required." }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
    $requirementsLock = if ($RuntimeOnly) { 'requirements.lock' } else { 'requirements-dev.lock' }
    & .\.venv\Scripts\python.exe -m pip install --require-hashes -r $requirementsLock
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
    & .\.venv\Scripts\python.exe -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Python dependency conflicts remain." }
    Push-Location frontend
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
    } finally { Pop-Location }
    Write-Host "Setup complete. Run .\scripts\start.ps1 from this folder."
} finally { Pop-Location }
