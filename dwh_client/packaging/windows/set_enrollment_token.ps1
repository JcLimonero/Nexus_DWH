<#
.SYNOPSIS
  Entrega al servicio un token de enrolamiento de UN solo uso (primer enrolamiento o re-enrolamiento).

.DESCRIPTION
  El enrolamiento debe hacerlo la cuenta del servicio (NT SERVICE\NexusAgent) para que la
  credencial quede protegida con SU DPAPI. Por eso NO se usa  NexusAgent.exe --enroll  desde una
  consola de administrador (la credencial quedaría ligada al administrador y el servicio no podría
  leerla). Este script:
    1. pide el token sin mostrarlo;
    2. con -Reenroll detiene el servicio y borra la credencial local (la instalación anterior debe
       revocarse en el panel);
    3. escribe data\enrollment_token.ini (hereda la ACL de data\: Administradores, SYSTEM y el servicio);
    4. arranca el servicio, que se enrola y borra el archivo (si Nexus rechaza el token, lo renombra
       a enrollment_token.ini.rechazado y se detiene con código 3).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateSet('group', 'agency', 'company')][string]$TokenType,
  [string]$DataRoot = (Join-Path $env:ProgramData 'NexusAgent'),
  [string]$ServiceName = 'NexusAgent',
  [switch]$Reenroll
)
$ErrorActionPreference = 'Stop'
function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Fail 'Ejecute este script como administrador.'
}
$dataDir = Join-Path $DataRoot 'data'
if (-not (Test-Path $dataDir)) { Fail "No existe $dataDir (¿instaló con install_service.ps1?)." }
$cred = Join-Path $dataDir 'agent_credential.dpapi'
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) { Fail "No existe el servicio $ServiceName." }

if (Test-Path $cred) {
  if (-not $Reenroll) { Fail 'Ya hay una credencial de instalación. Use -Reenroll para reemplazarla (y revoque la anterior en el panel).' }
  if ($svc.Status -ne 'Stopped') {
    Stop-Service -Name $ServiceName -NoWait
    $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(180))
  }
  Remove-Item -Force $cred
  Write-Host 'Credencial local anterior eliminada.'
}

$key = @{ group = 'group_token'; agency = 'agency_token'; company = 'token' }[$TokenType]
$secure = Read-Host -AsSecureString "Token de enrolamiento ($TokenType)"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  if (-not $plain) { Fail 'Token vacío.' }
  [System.IO.File]::WriteAllText((Join-Path $dataDir 'enrollment_token.ini'), "[nexus]`r`n$key = $plain`r`n",
    (New-Object System.Text.UTF8Encoding $false))
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr); $plain = $null
}
Remove-Item -Force (Join-Path $dataDir 'enrollment_token.ini.rechazado') -ErrorAction SilentlyContinue

if ((Get-Service -Name $ServiceName).Status -ne 'Running') { Start-Service -Name $ServiceName }
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 3
  if (-not (Test-Path (Join-Path $dataDir 'enrollment_token.ini'))) { break }
}
if (Test-Path (Join-Path $dataDir 'enrollment_token.ini.rechazado')) { Fail 'Nexus rechazó el token (ver logs\nexus_agent.log).' }
if (Test-Path (Join-Path $dataDir 'enrollment_token.ini')) {
  Write-Host 'El servicio aún no consumió el token (¿Nexus inaccesible?). Revise logs\nexus_agent.log; el agente reintenta solo.' -ForegroundColor Yellow
  exit 2
}
Write-Host 'Enrolamiento completado; el token de un solo uso fue borrado por el servicio.'
exit 0
