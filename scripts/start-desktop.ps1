[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory '.venv\Scripts\python.exe'))) {
    throw 'Python environment missing. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e ".[dev]"'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory 'desktop\node_modules'))) {
    throw 'Desktop dependencies missing. Run: npm --prefix desktop ci'
}
$env:AGENT_RUN_MODE = 'development'
if (-not $env:AGENT_DATA_DIR) { $env:AGENT_DATA_DIR = Join-Path $projectDirectory '.dev-data' }
$env:AGENT_PROFILE_DIR = Join-Path $env:AGENT_DATA_DIR 'desktop-profile'
$env:AGENT_PYTHON = Join-Path $projectDirectory '.venv/Scripts/python.exe'
Push-Location -LiteralPath (Join-Path $projectDirectory 'desktop')
try {
    & npm start
    if ($LASTEXITCODE -ne 0) { throw 'Desktop failed to build or start. Inspect the error above.' }
}
finally { Pop-Location }
