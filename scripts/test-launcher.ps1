[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$PreparedRoot)
$ErrorActionPreference = 'Stop'
$PreparedRoot = [IO.Path]::GetFullPath($PreparedRoot)
$exe = Join-Path $PreparedRoot 'Archive Zero.exe'
if (-not (Test-Path -LiteralPath (Join-Path $PreparedRoot '.runtime/frontend.ready'))) { throw 'Prepare an isolated checkout first.' }
$oldPath = $env:PATH
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $exe -ArgumentList '--prepare-only' -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(30000) -or $process.ExitCode -ne 0) { throw 'Warm EXE preparation failed.' }
    $log = Get-Content -LiteralPath (Join-Path $PreparedRoot '.runtime/launcher.log') -Raw
    if ($log -match 'Installing|Downloading|Building') { throw 'Unchanged checkout unnecessarily rebuilt dependencies.' }
    Write-Output ('Warm EXE startup reused dependencies: passed ({0:N1}s)' -f $timer.Elapsed.TotalSeconds)

    $fixture = Join-Path $PreparedRoot ('.runtime/self-update-test-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path (Join-Path $fixture 'scripts') -Force | Out-Null
    Copy-Item -LiteralPath $exe -Destination (Join-Path $fixture 'Archive Zero.exe')
    Set-Content -LiteralPath (Join-Path $fixture 'pyproject.toml') -Value '# isolated launcher fixture' -Encoding ASCII
    # This isolated script proves the tracked EXE is not locked by the update worker.
    @'
$root = Split-Path -Parent $PSScriptRoot
$handle = [IO.File]::Open((Join-Path $root 'Archive Zero.exe'), 'Open', 'ReadWrite', 'None')
$handle.Dispose()
Set-Content -LiteralPath (Join-Path $root '.runtime/unlocked.txt') -Value 'ok'
'@ | Set-Content -LiteralPath (Join-Path $fixture 'scripts/bootstrap-desktop.ps1') -Encoding UTF8
    $updater = Start-Process -FilePath (Join-Path $fixture 'Archive Zero.exe') -ArgumentList '--update' -PassThru -WindowStyle Hidden
    if (-not $updater.WaitForExit(30000)) { throw 'Update launcher did not hand off.' }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not (Test-Path -LiteralPath (Join-Path $fixture '.runtime/unlocked.txt'))) {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Update worker did not release the tracked EXE.' }
        Start-Sleep -Milliseconds 200
    }
    Write-Output 'Windows updater handoff leaves tracked EXE replaceable: passed.'
} finally { $env:PATH = $oldPath }
