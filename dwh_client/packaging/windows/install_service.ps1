<#
.SYNOPSIS
  Instala el agente Nexus DWH como servicio de Windows con minimo privilegio.

.DESCRIPTION
  * Copia PRIMERO el paquete a la carpeta del programa, creada nueva y solo para Administradores y
    SYSTEM (propietario: Administradores), y valida ESA copia (manifiesto, SHA-256, sin archivos
    extra ni enlaces, Authenticode si se declara, --selftest). Nada se valida sobre la carpeta de
    origen, que otro usuario podria modificar entre la validacion y el uso.
  * Programa en  -InstallDir  (defecto C:\Program Files\NexusAgent): Administradores/SYSTEM control
    total; la cuenta del servicio y Usuarios solo lectura/ejecucion (no puede modificar su binario).
  * Datos en  -DataRoot  (defecto C:\ProgramData\NexusAgent): config.ini (solo lectura para el
    servicio), data\ (credencial DPAPI, cola SQLite) y logs\ (modificacion para el servicio).
    SIN acceso para Usuarios. Si la carpeta ya existia (p. ej. creada por otro usuario), se toma su
    propiedad para Administradores y se restablecen sus permisos (queda en la salida).
  * Servicio "NexusAgent" con la cuenta VIRTUAL  NT SERVICE\NexusAgent: se crea DESHABILITADO,
    se configura (cuenta, SID, privilegios reducidos a SeChangeNotifyPrivilege, recuperacion) y solo
    al final pasa a inicio automatico retrasado. Ante cualquier fallo se elimina el servicio y la
    carpeta del programa recien creada.
  * No abre puertos de entrada, no toca el firewall, Defender, el registro de eventos ni la
    auditoria del sistema, y no cambia Windows Error Reporting.
  * Enrolamiento: con -TokenType pide el token de enrolamiento SIN mostrarlo y lo deja en
    data\enrollment_token.ini; el propio servicio se enrola (asi DPAPI queda ligado a su cuenta)
    y borra ese archivo.
  Recomendacion: descomprima el paquete en una carpeta solo para Administradores (no C:\Temp).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\install_service.ps1 -PackageDir . `
      -ApiUrl https://nexus.midominio.com -TokenType agency
#>
[CmdletBinding()]
param(
  [string]$PackageDir = (Split-Path -Parent $PSScriptRoot),
  [string]$InstallDir = (Join-Path $env:ProgramFiles 'NexusAgent'),
  [string]$DataRoot = (Join-Path $env:ProgramData 'NexusAgent'),
  [string]$ServiceName = 'NexusAgent',
  [string]$ApiUrl = '',
  [ValidateSet('', 'group', 'agency', 'company')][string]$TokenType = '',
  [ValidateSet('user', 'machine')][string]$CredentialScope = 'user',
  [string]$ExpectedSignerThumbprint = '',
  [switch]$AllowUnsignedManifest,
  [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$admins = '*S-1-5-32-544'; $system = '*S-1-5-18'; $users = '*S-1-5-32-545'
$account = "NT SERVICE\$ServiceName"

function Fail([string]$msg, [int]$code = 1) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit $code }
function Write-Utf8NoBom([string]$Path, [string]$Text) {
  [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
}
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
  & $Exe @Arguments
  if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') -> código $LASTEXITCODE" }
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Fail 'Ejecute este script en una consola de PowerShell como administrador.'
}
if (-not (Test-Path (Join-Path $PackageDir 'NexusAgent.exe'))) { Fail "No se encontró NexusAgent.exe en $PackageDir" }
$PackageDir = (Resolve-Path $PackageDir).Path
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
  Fail "El servicio $ServiceName ya existe. Para cambiar de versión use update_agent.ps1."
}
if (Test-Path $InstallDir) {
  Fail "Ya existe $InstallDir (¿restos de una instalación anterior?). Bórrela o use update_agent.ps1."
}

# -- 1. Copia a la carpeta del programa (solo Administradores) y validación de LA COPIA --
$installCreated = $false
function Remove-Program { if ($installCreated -and (Test-Path $InstallDir)) { Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue } }
try {
  New-Item -ItemType Directory -Path $InstallDir | Out-Null
  $installCreated = $true
  Invoke-Native 'icacls.exe' @($InstallDir, '/setowner', $admins, '/Q')
  Invoke-Native 'icacls.exe' @($InstallDir, '/inheritance:r', '/grant:r', "${admins}:(OI)(CI)F", "${system}:(OI)(CI)F", '/Q')
  Get-ChildItem -LiteralPath $PackageDir -Force | Copy-Item -Destination $InstallDir -Recurse -Force
} catch { Remove-Program; Fail "No se pudo copiar el programa: $($_.Exception.Message)" }

$exePath = Join-Path $InstallDir 'NexusAgent.exe'
function Reject([string]$msg) { Remove-Program; Fail $msg 4 }
try { $manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $InstallDir 'release.json') | ConvertFrom-Json }
catch { Reject 'release.json ilegible.' }
Write-Host "Paquete: NexusAgent $($manifest.version) ($($manifest.platform))"
if ($manifest.authenticode.signed) {
  foreach ($exe in $manifest.executables) {
    $sig = Get-AuthenticodeSignature -FilePath (Join-Path $InstallDir $exe)
    if ($sig.Status -ne 'Valid') { Reject "Firma Authenticode de $exe no válida: $($sig.Status)" }
    if ($ExpectedSignerThumbprint) {
      if ($sig.SignerCertificate.Thumbprint -ne $ExpectedSignerThumbprint.ToUpper()) {
        Reject "El firmante de $exe ($($sig.SignerCertificate.Thumbprint)) no es el esperado ($ExpectedSignerThumbprint)."
      }
    } else {
      # La huella NO se toma del manifiesto (lo controla quien arma el paquete).
      Write-Host "AVISO: firmante $($sig.SignerCertificate.Subject) ($($sig.SignerCertificate.Thumbprint)) no fijado; compruebe la huella por un canal independiente o use -ExpectedSignerThumbprint." -ForegroundColor Yellow
    }
  }
} else {
  Write-Host 'AVISO: paquete SIN firma Authenticode (certificado de firma de código pendiente).' -ForegroundColor Yellow
}
# Primera instalación: no hay binario instalado que haga de ancla de confianza; el propio paquete
# comprueba su manifiesto (integridad). La autenticidad la dan Authenticode y la firma Ed25519 si el
# binario ya trae claves de publicación (con claves, un manifiesto sin firma se rechaza siempre).
$verifyArgs = @('--verify-update', $InstallDir, '--allow-same-version')
if ($AllowUnsignedManifest) { $verifyArgs += '--allow-unsigned-manifest' }
& $exePath @verifyArgs
if ($LASTEXITCODE -ne 0) { Reject 'El paquete no pasó la validación (ver motivo arriba).' }
& $exePath --selftest
if ($LASTEXITCODE -ne 0) { Reject 'NexusAgent.exe --selftest falló.' }

# -- 2. Datos y configuración --------------------------------------------------
$dataDir = Join-Path $DataRoot 'data'
$logDir = Join-Path $DataRoot 'logs'
$configPath = Join-Path $DataRoot 'config.ini'
$serviceCreated = $false
try {
  if (Test-Path -LiteralPath $DataRoot) {
    # Una carpeta precreada por un usuario sin privilegios podría traer un config.ini con otro
    # api_url, archivos con handles abiertos o junctions: se ABORTA en lugar de "arreglarla".
    $okOwners = @('S-1-5-32-544', 'S-1-5-18')
    # Los archivos que crea el propio servicio (credencial, cola, logs) pertenecen a su SID de
    # servicio (S-1-5-80-…, determinista aunque el servicio no exista): se aceptan SOLO dentro de
    # data\ y logs\, para poder reinstalar conservando la credencial y la cola.
    $serviceSid = $null
    $showsid = & sc.exe showsid $ServiceName 2>$null | Out-String
    if ($showsid -match '(S-1-5-80(?:-\d+){5})') { $serviceSid = $Matches[1] }
    $svcOwnedRoots = @((Join-Path $DataRoot 'data'), (Join-Path $DataRoot 'logs'))
    $items = @(Get-Item -LiteralPath $DataRoot -Force) + @(Get-ChildItem -LiteralPath $DataRoot -Recurse -Force -ErrorAction Stop)
    foreach ($it in $items) {
      if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "La carpeta de datos contiene un enlace/junction ($($it.FullName)). Revísela y elimínela antes de instalar."
      }
      $ownerSid = (Get-Acl -LiteralPath $it.FullName).GetOwner([Security.Principal.SecurityIdentifier]).Value
      $inSvcRoot = $false
      foreach ($r in $svcOwnedRoots) {
        if ($it.FullName.StartsWith($r + '\', [StringComparison]::OrdinalIgnoreCase)) { $inSvcRoot = $true }
      }
      if ($serviceSid -and $inSvcRoot -and $ownerSid -eq $serviceSid) { continue }
      if ($okOwners -notcontains $ownerSid) {
        throw "La carpeta de datos existe y $($it.FullName) pertenece a $ownerSid (no Administradores/SYSTEM). Revísela y elimínela antes de instalar."
      }
    }
    Write-Host "La carpeta de datos $DataRoot ya existía (propietario Administradores/SYSTEM): se restablecen sus permisos."
    Invoke-Native 'icacls.exe' @($DataRoot, '/reset', '/T', '/C', '/Q')
  }
  foreach ($d in @($DataRoot, $dataDir, $logDir)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
  # Primero cerrar la carpeta de datos (solo Administradores/SYSTEM) y después escribir en ella.
  Invoke-Native 'icacls.exe' @($DataRoot, '/setowner', $admins, '/T', '/C', '/Q')
  # La ACL se fija SOLO en la raíz y el contenido la hereda (/reset). Aplicar /inheritance:r +
  # /grant:r con /T a cada archivo terminaba en "Acceso denegado" al ejecutar NexusAgent.exe.
  Invoke-Native 'icacls.exe' @($DataRoot, '/inheritance:r', '/grant:r', "${admins}:(OI)(CI)F", "${system}:(OI)(CI)F", '/Q')
  Invoke-Native 'icacls.exe' @((Join-Path $DataRoot '*'), '/reset', '/T', '/C', '/Q')
  if (-not (Test-Path $configPath)) {
    if (-not $ApiUrl) { throw 'Indique -ApiUrl (no existe config.ini todavía).' }
    $tpl = Get-Content -Raw -Encoding UTF8 (Join-Path $InstallDir 'config.example.ini')
    $tpl = $tpl -replace '(?m)^api_url\s*=.*$', ("api_url = " + $ApiUrl.Replace('$', '$$'))
    $tpl = $tpl -replace '(?m)^;\s*data_dir\s*=.*$', "data_dir = $dataDir"
    $tpl = $tpl -replace '(?m)^;\s*log_dir\s*=.*$', "log_dir = $logDir"
    $tpl = $tpl -replace '(?m)^;\s*credential_scope\s*=.*$', "credential_scope = $CredentialScope"
    Write-Utf8NoBom $configPath $tpl
    Write-Host "Creado $configPath"
  } else {
    Write-Host "Se conserva el config.ini existente: $configPath"
  }

  # -- 3. Servicio: creado DESHABILITADO, configurado y habilitado al final --
  $binPath = "`"$exePath`" --service --config `"$configPath`" --data-dir `"$dataDir`""
  New-Service -Name $ServiceName -BinaryPathName $binPath -DisplayName 'Nexus DWH Agent' `
    -Description 'Agente ETL de Nexus DWH: tareas autorizadas por Nexus (lectura del DMS, carga al DWH). Solo conexiones salientes.' `
    -StartupType Disabled | Out-Null
  $serviceCreated = $true
  Invoke-Native 'sc.exe' @('config', $ServiceName, 'obj=', $account)
  Invoke-Native 'sc.exe' @('sidtype', $ServiceName, 'unrestricted')
  Invoke-Native 'sc.exe' @('privs', $ServiceName, 'SeChangeNotifyPrivilege')
  Invoke-Native 'sc.exe' @('failure', $ServiceName, 'reset=', '86400', 'actions=', 'restart/60000/restart/300000/restart/900000')
  Invoke-Native 'sc.exe' @('failureflag', $ServiceName, '0')
  $svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
  if ($svc.StartName -ne $account) { throw "No se pudo asignar la cuenta $account (quedó '$($svc.StartName)')." }

  # -- 4. Permisos de la cuenta del servicio (SID de grupos: no depende del idioma) --
  # Solo en las raíces, con herencia (OI)(CI); el contenido hereda (/reset): ver el paso 2.
  Invoke-Native 'icacls.exe' @($InstallDir, '/inheritance:r', '/grant:r', "${admins}:(OI)(CI)F", "${system}:(OI)(CI)F",
    "${account}:(OI)(CI)RX", "${users}:(OI)(CI)RX", '/Q')
  Invoke-Native 'icacls.exe' @((Join-Path $InstallDir '*'), '/reset', '/T', '/C', '/Q')
  Invoke-Native 'icacls.exe' @($DataRoot, '/grant', "${account}:(OI)(CI)RX", '/Q')
  foreach ($d in @($dataDir, $logDir)) {
    Invoke-Native 'icacls.exe' @($d, '/grant', "${account}:(OI)(CI)M", '/Q')
  }

  # -- 5. Token de enrolamiento de un solo uso --
  if ($TokenType) {
    $key = @{ group = 'group_token'; agency = 'agency_token'; company = 'token' }[$TokenType]
    $secure = Read-Host -AsSecureString "Token de enrolamiento ($TokenType)"
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
      $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
      if (-not $plain) { throw 'Token vacío.' }
      $tokenPath = Join-Path $dataDir 'enrollment_token.ini'
      if (Test-Path -LiteralPath $tokenPath) { Remove-Item -LiteralPath $tokenPath -Force }
      # CreateNew: nunca reutiliza un archivo (ni un handle) preexistente.
      $fs = New-Object System.IO.FileStream($tokenPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
      try {
        $bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes("[nexus]`r`n$key = $plain`r`n")
        $fs.Write($bytes, 0, $bytes.Length)
      } finally { $fs.Dispose() }
    } finally {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr); $plain = $null
    }
    Write-Host 'Token guardado en data\enrollment_token.ini (solo Administradores/SYSTEM/servicio). El servicio lo borra al enrolar.'
  }

  # -- 6. Recién ahora, inicio automático retrasado --
  Invoke-Native 'sc.exe' @('config', $ServiceName, 'start=', 'delayed-auto')
} catch {
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  if ($serviceCreated) { & sc.exe delete $ServiceName | Out-Null; Write-Host "Se eliminó el servicio $ServiceName a medio configurar." }
  $leftover = Join-Path $dataDir 'enrollment_token.ini'
  if (Test-Path -LiteralPath $leftover) { Remove-Item -LiteralPath $leftover -Force -ErrorAction SilentlyContinue; Write-Host 'Se eliminó el token de enrolamiento escrito.' }
  Remove-Program
  exit 1
}

Write-Host ''
Write-Host "Servicio $ServiceName instalado con la cuenta $account."
Write-Host 'Nota: Windows Error Reporting puede guardar volcados de memoria de procesos que fallan; ver DWH_README.md 21.2.'

# -- 7. Arranque y comprobación -----------------------------------------------
if (-not $NoStart) {
  try { Start-Service -Name $ServiceName -ErrorAction Stop } catch { Write-Host "Start-Service: $($_.Exception.Message)" -ForegroundColor Yellow }
  Start-Sleep -Seconds 10
  $s = Get-Service -Name $ServiceName
  Write-Host "Estado: $($s.Status)"
  $log = Join-Path $logDir 'nexus_agent.log'
  if (Test-Path $log) { Get-Content -Tail 15 -Encoding UTF8 $log }
  if ($s.Status -ne 'Running') {
    Write-Host 'El servicio no quedó en ejecución: revise el log anterior, el Visor de eventos (Aplicación) y  sc.exe query NexusAgent  (SERVICE_EXIT_CODE: 2 = configuración, 3 = credencial revocada/rechazada).' -ForegroundColor Yellow
    exit 2
  }
}
exit 0
