<#
.SYNOPSIS
  Detiene y elimina el servicio del agente Nexus DWH.

.DESCRIPTION
  Por defecto CONSERVA el programa y los datos (credencial y cola de reportes pendientes).
  -RemoveProgram borra la carpeta del programa. -RemoveData borra config.ini, la credencial, la
  cola local (los reportes aún no enviados se pierden) y los logs; pide confirmación.
  Recuerde revocar la instalación en el panel (Instalaciones -> Revocar).
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
  [string]$ServiceName = 'NexusAgent',
  [string]$InstallDir = (Join-Path $env:ProgramFiles 'NexusAgent'),
  [string]$DataRoot = (Join-Path $env:ProgramData 'NexusAgent'),
  [int]$StopTimeoutSeconds = 180,
  [switch]$RemoveProgram,
  [switch]$RemoveData
)
$ErrorActionPreference = 'Stop'
function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Fail 'Ejecute este script como administrador.'
}

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
  if ($svc.Status -ne 'Stopped') {
    Write-Host "Deteniendo $ServiceName (la tarea en curso termina o se revierte; la cola queda en disco)..."
    Stop-Service -Name $ServiceName -NoWait -ErrorAction SilentlyContinue
    $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds($StopTimeoutSeconds))
  }
  & sc.exe delete $ServiceName | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "sc.exe delete devolvió $LASTEXITCODE" }
  Write-Host "Servicio $ServiceName eliminado."
} else {
  Write-Host "El servicio $ServiceName no existe."
}

if ($RemoveProgram -and (Test-Path $InstallDir)) {
  $deadline = (Get-Date).AddSeconds(60)
  while ((Get-Date) -lt $deadline -and (Get-Process -Name NexusAgent -ErrorAction SilentlyContinue |
      Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) })) {
    Start-Sleep -Milliseconds 500
  }
  Remove-Item -Recurse -Force $InstallDir
  Get-ChildItem -Path (Split-Path -Parent $InstallDir) -Directory -Filter ((Split-Path -Leaf $InstallDir) + '.*') -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
  Write-Host "Programa eliminado: $InstallDir"
}
if ($RemoveData -and (Test-Path $DataRoot)) {
  if ($PSCmdlet.ShouldProcess($DataRoot, 'Borrar configuración, credencial, cola local y logs')) {
    Remove-Item -Recurse -Force $DataRoot
    Write-Host "Datos eliminados: $DataRoot"
  }
}
Write-Host 'Revoque la instalación en el panel de Nexus si no se va a reinstalar en esta máquina.'
exit 0
