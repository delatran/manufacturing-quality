param([switch]$Fix)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Invoke-Check {
    param([string]$Label, [string]$Executable, [string[]]$Arguments)
    Write-Host $Label
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)." }
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Run .\scripts\setup.ps1 before checking the project."
}

Push-Location $projectRoot
try {
    if ($Fix) {
        Invoke-Check -Label 'Format Python' -Executable $pythonExe -Arguments @('-m', 'ruff', 'format')
        Invoke-Check -Label 'Fix Python lint' -Executable $pythonExe -Arguments @('-m', 'ruff', 'check', '--fix')
        Push-Location frontend
        try {
            Invoke-Check -Label 'Format frontend' -Executable 'npm.cmd' -Arguments @('run', 'format')
            Invoke-Check -Label 'Fix frontend lint' -Executable 'npm.cmd' -Arguments @('run', 'lint', '--', '--fix')
        } finally { Pop-Location }
    }
    Invoke-Check -Label 'Check Python dependencies' -Executable $pythonExe -Arguments @('-m', 'pip', 'check')
    Invoke-Check -Label 'Check Python format' -Executable $pythonExe -Arguments @('-m', 'ruff', 'format', '--check')
    Invoke-Check -Label 'Lint Python' -Executable $pythonExe -Arguments @('-m', 'ruff', 'check')
    Invoke-Check -Label 'Typecheck Python' -Executable $pythonExe -Arguments @('-m', 'mypy')
    Invoke-Check -Label 'Test Python' -Executable $pythonExe -Arguments @('-m', 'pytest', 'tests', '-q')
    Invoke-Check -Label 'Check generated contracts' -Executable $pythonExe -Arguments @('scripts/generate_contracts.py', '--check')
    Invoke-Check -Label 'Check generated submission' -Executable $pythonExe -Arguments @('-m', 'scripts.generate_submission', '--check')
    Push-Location frontend
    try {
        Invoke-Check -Label 'Check frontend' -Executable 'npm.cmd' -Arguments @('run', 'check')
    } finally { Pop-Location }
    Write-Host 'All local checks passed.'
} finally { Pop-Location }
