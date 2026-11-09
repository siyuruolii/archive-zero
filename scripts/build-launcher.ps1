[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework C# compiler not found.' }
& $compiler /nologo /target:winexe /platform:x64 /optimize+ /utf8output /codepage:65001 ('/reference:' + (Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/WPF/PresentationFramework.dll')) ('/reference:' + (Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/WPF/PresentationCore.dll')) ('/reference:' + (Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/WPF/WindowsBase.dll')) /reference:System.Xaml.dll ('/resource:' + (Join-Path $PSScriptRoot 'launcher/Launcher.xaml') + ',Launcher.xaml') ('/win32icon:' + (Join-Path $projectDirectory 'desktop/assets/brand/archive-zero.ico')) ('/out:' + (Join-Path $projectDirectory 'Archive Zero.exe')) (Join-Path $PSScriptRoot 'launcher/Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed.' }
