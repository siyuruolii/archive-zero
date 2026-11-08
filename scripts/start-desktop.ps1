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
Push-Location -LiteralPath (Join-Path $projectDirectory 'desktop')
try {
    & npm start
    if ($LASTEXITCODE -ne 0) { throw 'Desktop failed to build or start. Inspect the error above.' }
}
finally { Pop-Location }
