<#
.SYNOPSIS
  Compila el agente para Windows (x64) con Nuitka y deja build\dist\NexusAgent + un .zip SIN FIRMAR.

.DESCRIPTION
  Requisitos del equipo de build: Windows 10/11 o Server x64, Python 3.12 x64 (python.org),
  Visual Studio 2022 Build Tools con "Desktop development with C++" (MSVC 14.3+).
  Crea un venv de build aislado (build\venv-build), instala requirements_build.txt, compila,
  verifica el paquete (verify_package.py), ejecuta NexusAgent.exe --version/--selftest y comprime.
  La firma Authenticode (windows\sign_release.ps1) y la del manifiesto (tools\sign_manifest.py)
  son pasos posteriores.
.EXAMPLE
  cd dwh_client; powershell -ExecutionPolicy Bypass -File packaging\build_agent.ps1
#>
[CmdletBinding()]
param(
  [string]$Python = 'py',
  [string[]]$PythonArgs = @('-3.12'),
  [int]$Jobs = 0,
  [switch]$AllowDownloads
)
$ErrorActionPreference = 'Stop'
function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

$clientDir = Split-Path -Parent $PSScriptRoot
Set-Location $clientDir
$venv = Join-Path $clientDir 'build\venv-build'
$env:PYTHONDONTWRITEBYTECODE = '1'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
  & $Python @PythonArgs -m venv $venv
  if ($LASTEXITCODE -ne 0) { Fail 'No se pudo crear el venv de build.' }
}
$py = Join-Path $venv 'Scripts\python.exe'
& $py -m pip install --disable-pip-version-check -q -r requirements_build.txt
if ($LASTEXITCODE -ne 0) { Fail 'pip install falló.' }

$buildArgs = @('packaging\build_agent.py')
if ($Jobs -gt 0) { $buildArgs += "--jobs=$Jobs" }
if ($AllowDownloads) { $buildArgs += '--allow-downloads' }
& $py @buildArgs
if ($LASTEXITCODE -ne 0) { Fail "build_agent.py devolvió $LASTEXITCODE" }

$dist = Join-Path $clientDir 'build\dist\NexusAgent'
$exe = Join-Path $dist 'NexusAgent.exe'
$version = (& $exe --version).Trim()
if ($LASTEXITCODE -ne 0) { Fail 'NexusAgent.exe --version falló.' }
& $exe --selftest
if ($LASTEXITCODE -ne 0) { Fail 'NexusAgent.exe --selftest falló.' }

$zip = Join-Path $clientDir "build\NexusAgent-$version-windows-x64-SIN-FIRMAR.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $dist -DestinationPath $zip
Write-Host "Listo: $dist"
Write-Host "Zip (SIN FIRMAR): $zip"
exit 0
