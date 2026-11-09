[CmdletBinding()]
param([switch]$PrepareOnly, [switch]$Update, [int]$WaitForPid = 0)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONNOUSERSITE = '1'
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$developmentMode = Test-Path -LiteralPath (Join-Path $projectDirectory '.development.json')
$runtimeDirectory = if ($developmentMode) { Join-Path $projectDirectory '.dev-data/launcher' } else { Join-Path $projectDirectory '.runtime' }

function Run-Step([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $([IO.Path]::GetFileName($Program))" }
}
function Fingerprint([string[]]$Files) {
    $values = foreach ($file in ($Files | Sort-Object)) { $file + '=' + (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes(($values -join '|')))).Replace('-', '') }
    finally { $hash.Dispose() }
}
function Matches([string]$Marker, [string]$Value) {
    return (Test-Path -LiteralPath $Marker) -and ((Get-Content -LiteralPath $Marker -Raw).Trim() -eq $Value)
}
function Install-Runtime([string]$Name, $Spec) {
    if (([Uri]$Spec.url).Scheme -ne 'https') { throw 'Runtime downloads require HTTPS.' }
    $folder = Join-Path $runtimeDirectory $Spec.directory
    $exe = Join-Path $folder $Spec.executable
    if ((Matches (Join-Path $folder '.complete') $Spec.sha256) -and (Test-Path -LiteralPath $exe)) { return $exe }
    Write-Host "Preparing $Name $($Spec.version)..."
    $download = Join-Path $runtimeDirectory ($Name + '.zip')
    if (-not (Test-Path -LiteralPath $download) -or (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $Spec.sha256) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $client = New-Object Net.WebClient
        try { $client.DownloadFile($Spec.url, $download) } finally { $client.Dispose() }
    }
    if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $Spec.sha256) { throw "$Name download checksum mismatch. Please retry on a trusted network." }
    if ($Name -eq 'python') { Expand-Archive -LiteralPath $download -DestinationPath $folder -Force }
    else { Expand-Archive -LiteralPath $download -DestinationPath $runtimeDirectory -Force }
    if (-not (Test-Path -LiteralPath $exe)) { throw "$Name extraction incomplete." }
    Set-Content -LiteralPath (Join-Path $folder '.complete') -Value $Spec.sha256 -Encoding ASCII
    return $exe
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    if ($WaitForPid -gt 0) {
        $previous = Get-Process -Id $WaitForPid -ErrorAction SilentlyContinue
        if ($previous -and -not $previous.WaitForExit(60000)) { throw 'The application has not exited. Close it before updating.' }
    }
    New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
    $lock = $null
    try { $lock = [IO.File]::Open((Join-Path $runtimeDirectory 'startup.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw 'Another startup or update is already running.' }
    Push-Location -LiteralPath $projectDirectory
    try {
        if ($developmentMode) {
            if ($Update) { throw 'Use Git directly in the development checkout; update the daily-use copy from the app.' }
            $env:AGENT_RUN_MODE = 'development'
            if (-not $env:AGENT_DATA_DIR) { $env:AGENT_DATA_DIR = Join-Path $projectDirectory '.dev-data' }
            $env:AGENT_PROFILE_DIR = Join-Path $env:AGENT_DATA_DIR 'desktop-profile'
            $env:AGENT_PYTHON = Join-Path $projectDirectory '.venv/Scripts/python.exe'
            if (-not (Test-Path -LiteralPath $env:AGENT_PYTHON)) { throw 'Set up the development .venv first; no additional environment will be downloaded.' }
            Run-Step $env:AGENT_PYTHON @('-c','import fastapi, pydantic, uvicorn, reading_agent')
            $devNode = (Get-Command node.exe -ErrorAction Stop).Source
            $devNpm = Join-Path (Split-Path -Parent (Get-Command npm.cmd -ErrorAction Stop).Source) 'node_modules/npm/bin/npm-cli.js'
            Write-Output 'Building development reading room...'
            Run-Step $devNode @($devNpm,'--prefix',(Join-Path $projectDirectory 'desktop'),'run','prepare:electron')
            Run-Step $devNode @($devNpm,'--prefix',(Join-Path $projectDirectory 'desktop'),'run','build')
            # Electron is a GUI process: hiding its startup window also hides the app, not just a console.
            if (-not $PrepareOnly) { Start-Process -FilePath (Join-Path $projectDirectory 'desktop/node_modules/electron/dist/electron.exe') -ArgumentList ('"{0}"' -f (Join-Path $projectDirectory 'desktop')) -WorkingDirectory $projectDirectory | Out-Null }
            Write-Output 'Preparation complete.'
            exit 0
        }
        $env:AGENT_RUN_MODE = 'daily'
        if (-not $env:AGENT_DATA_DIR) {
            $dailyData = Join-Path $projectDirectory 'data'
            $legacyData = Join-Path $projectDirectory '.local'
            if ((Test-Path -LiteralPath (Join-Path $legacyData 'reading.db')) -and -not (Test-Path -LiteralPath (Join-Path $dailyData 'reading.db'))) { $dailyData = $legacyData }
            $env:AGENT_DATA_DIR = $dailyData
        }
        $env:AGENT_PROFILE_DIR = Join-Path $env:AGENT_DATA_DIR 'desktop-profile'
        $specs = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'launcher-runtimes.json') -Raw | ConvertFrom-Json
        if ($Update) {
            Write-Output 'Checking repository and downloading update...'
            $updateNode = Join-Path (Join-Path $runtimeDirectory $specs.node.directory) $specs.node.executable
            if (-not (Test-Path -LiteralPath $updateNode)) { throw 'Start the application once before updating.' }
            # The same guard is also checked in the app, but must be checked again after exit.
            $env:AGENT_UPDATE_ROOT = $projectDirectory
            Run-Step $updateNode @((Join-Path $PSScriptRoot 'update-repository.cjs'))
            # Restart the newly checked-out bootstrap, rather than running this old script further.
            $lock.Dispose(); $lock = $null
            Start-Process -FilePath (Join-Path $projectDirectory 'Archive Zero.exe') -WorkingDirectory $projectDirectory | Out-Null
            exit 0
        }
        $python = Install-Runtime 'python' $specs.python
        $node = Install-Runtime 'node' $specs.node
        $nodeDirectory = Split-Path -Parent $node
        $env:PATH = $nodeDirectory + ';' + $env:PATH
        $venv = Join-Path $runtimeDirectory ('venv-' + $specs.python.version)
        $envPython = Join-Path $venv 'Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $envPython)) { Write-Output 'Creating local Python environment...'; Run-Step $python @('-I','-m','venv',$venv) }
        $pythonStamp = Fingerprint @((Join-Path $projectDirectory 'requirements-dev.lock'), (Join-Path $projectDirectory 'pyproject.toml'))
        $pythonMarker = Join-Path $runtimeDirectory ('python-dependencies-' + $specs.python.version + '.ready')
        if (-not (Matches $pythonMarker $pythonStamp)) {
            Write-Output 'Installing Python dependencies...'
            Run-Step $envPython @('-m','pip','install','--disable-pip-version-check','-r','requirements-dev.lock')
            Run-Step $envPython @('-m','pip','install','--disable-pip-version-check','--no-deps','-e','.')
            Set-Content -LiteralPath $pythonMarker -Value $pythonStamp -Encoding ASCII
        }
        $npm = Join-Path $nodeDirectory 'node_modules/npm/bin/npm-cli.js'
        $desktop = Join-Path $projectDirectory 'desktop'
        $npmStamp = Fingerprint @((Join-Path $desktop 'package-lock.json'), (Join-Path $desktop 'package.json'))
        $npmMarker = Join-Path $runtimeDirectory 'node-dependencies.ready'
        $electron = Join-Path $desktop 'node_modules/electron/dist/electron.exe'
        if (-not (Matches $npmMarker $npmStamp) -or -not (Test-Path -LiteralPath $electron)) {
            Write-Output 'Installing desktop dependencies (first launch may take several minutes)...'
            Run-Step $node @($npm,'--prefix',$desktop,'ci','--no-audit','--no-fund')
            if (-not (Test-Path -LiteralPath $electron)) {
                Write-Output 'Downloading Electron desktop runtime...'
                Run-Step $node @((Join-Path $desktop 'node_modules/electron/install.js'))
            }
            if (-not (Test-Path -LiteralPath $electron)) { throw 'Electron runtime is missing after installation.' }
            Set-Content -LiteralPath $npmMarker -Value $npmStamp -Encoding ASCII
        }
        $sourceFiles = @(Get-ChildItem (Join-Path $desktop 'renderer'), (Join-Path $desktop 'assets') -File -Recurse | ForEach-Object FullName)
        $sourceFiles += @((Join-Path $desktop 'index.html'), (Join-Path $desktop 'package-lock.json'))
        $buildStamp = Fingerprint $sourceFiles
        $buildMarker = Join-Path $runtimeDirectory 'frontend.ready'
        if (-not (Matches $buildMarker $buildStamp) -or -not (Test-Path -LiteralPath (Join-Path $desktop 'dist/index.html'))) {
            Write-Output 'Building reading room...'
            Run-Step $node @($npm,'--prefix',$desktop,'run','build')
            Set-Content -LiteralPath $buildMarker -Value $buildStamp -Encoding ASCII
        }
        if ($PrepareOnly) { Write-Output 'Preparation complete.'; exit 0 }
        Write-Output 'Opening Archive Zero...'
        $env:AGENT_PYTHON = $envPython
        Start-Process -FilePath $electron -ArgumentList ('"{0}"' -f $desktop) -WorkingDirectory $desktop | Out-Null
    } finally { Pop-Location; if ($lock) { $lock.Dispose() } }
} catch { Write-Output ('ERROR: ' + $_.Exception.Message); exit 1 }
