<#
.SYNOPSIS
  Actualiza el agente Nexus DWH instalado como servicio, validando el paquete y con vuelta atras.

.DESCRIPTION
  Ejecute SIEMPRE la copia INSTALADA de este script
  (C:\Program Files\NexusAgent\scripts\update_agent.ps1), nunca la que trae el paquete nuevo.

  1. Copia el paquete a una carpeta de trabajo NUEVA junto al programa, solo para Administradores y
     SYSTEM (propietario: Administradores). TODAS las comprobaciones se hacen sobre esa copia, nunca
     sobre la carpeta de origen (evita cambios entre la validacion y el uso).
  2. Valida la copia con el binario YA INSTALADO (ancla de confianza: sus claves de publicacion
     compiladas):  NexusAgent.exe --verify-update <copia>
       - firma Ed25519 de release.json con una clave confiable (-AllowUnsignedManifest solo sirve
         mientras el agente instalado no tenga claves; despues se rechaza siempre);
       - version mayor que la instalada (sin downgrade); SHA-256 y tamano de cada archivo, sin
         archivos extra ni enlaces.
  3. Authenticode de la copia:
       - instalada firmada  -> el firmante nuevo debe ser el mismo (o -ExpectedSignerThumbprint);
       - instalada SIN firma y nueva firmada (primer paquete firmado) -> exige -ExpectedSignerThumbprint
         (la huella no se toma del manifiesto);
       - instalada firmada y nueva sin firma -> rechazo salvo -AllowSignatureDowngrade.
  4. Detiene el servicio (parada ordenada) y espera a que el proceso salga; mueve la version actual a
     <InstallDir>.prev y la nueva a <InstallDir> (con reintentos si algo tiene la carpeta abierta).
  5. Arranca y comprueba salud: servicio en ejecucion durante -HealthSeconds y una linea de arranque
     de la version nueva escrita DESPUES de este arranque. Ante cualquier fallo o excepcion
     restaura la version anterior y la arranca (codigo 3). Nunca deja la maquina sin programa
     instalado ni el servicio detenido en silencio: si la restauracion misma falla, lo dice con
     instrucciones (codigo 1).
  Codigos: 0 actualizado, 1 error (sin cambios o restauracion incompleta, ver mensaje),
  3 restaurada la version anterior, 4 paquete rechazado.
  Los datos (config.ini, credencial, cola) no se tocan: son compatibles entre versiones 5.x.

  Instalaciones antiguas (python client_postgres.py o un .exe de PyInstaller, sin servicio de este
  tipo): no se actualizan con este script; siga la migracion de DWH_README.md 21.9.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$PackageDir,
  [string]$InstallDir = (Join-Path $env:ProgramFiles 'NexusAgent'),
  [string]$ServiceName = 'NexusAgent',
  [string]$ExpectedSignerThumbprint = '',
  [int]$StopTimeoutSeconds = 180,
  [int]$HealthSeconds = 45,
  [switch]$AllowUnsignedManifest,
  [switch]$AllowSignatureDowngrade
)
$ErrorActionPreference = 'Stop'
$admins = '*S-1-5-32-544'; $system = '*S-1-5-18'; $users = '*S-1-5-32-545'
$account = "NT SERVICE\$ServiceName"

function Fail([string]$msg, [int]$code = 1) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit $code }

function Get-AgentVersion([string]$Exe) {
  # Captura explícita de stdout: igual en Windows PowerShell 5.1 y pwsh 7, y con un error claro si el
  # .exe no puede ejecutarse (pwsh 7.6 informa "Acceso denegado" como "StandardOutputEncoding is only
  # supported when standard output is redirected").
  $psi = New-Object System.Diagnostics.ProcessStartInfo $Exe, '--version'
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  $out = $p.StandardOutput.ReadToEnd()
  $p.WaitForExit()
  if ($p.ExitCode -ne 0) { throw "$Exe --version terminó con $($p.ExitCode)" }
  return $out.Trim()
}

function Invoke-Icacls([string[]]$IcaclsArgs) {
  & icacls.exe @IcaclsArgs | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "icacls $($IcaclsArgs -join ' ') -> $LASTEXITCODE" }
}

function Test-ProcessGone([string]$Dir, [int]$Seconds = 60) {
  # STOPPED en el SCM no garantiza que el proceso ya haya salido: esperar antes de mover la carpeta.
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    $p = Get-Process -Name NexusAgent -ErrorAction SilentlyContinue |
      Where-Object { $_.Path -and $_.Path.StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase) }
    if (-not $p) { return $true }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

function Stop-Agent([int]$Seconds) {
  $svc = Get-Service -Name $ServiceName
  if ($svc.Status -ne 'Stopped') {
    Stop-Service -Name $ServiceName -NoWait -ErrorAction SilentlyContinue
    try { $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds($Seconds)) } catch { return $false }
  }
  return (Test-ProcessGone $InstallDir 60)
}

function Rename-WithRetry([string]$Path, [string]$NewLeaf) {
  # Una consola, el Explorador o un antivirus con la carpeta abierta hacen fallar el cambio de nombre.
  $delay = 1
  for ($i = 1; $i -le 6; $i++) {
    try { Rename-Item -Path $Path -NewName $NewLeaf -ErrorAction Stop; return }
    catch {
      if ($i -eq 6) {
        throw "No se pudo renombrar '$Path' a '$NewLeaf' tras $i intentos: $($_.Exception.Message). " +
              '¿Hay una consola, el Explorador u otro programa abierto dentro de esa carpeta?'
      }
      Start-Sleep -Seconds $delay; $delay = [Math]::Min($delay * 2, 16)
    }
  }
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Fail 'Ejecute este script como administrador.'
}
$installedExe = Join-Path $InstallDir 'NexusAgent.exe'
if (-not (Test-Path $installedExe)) { Fail "No hay agente instalado en $InstallDir (use install_service.ps1)." }
if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { Fail "No existe el servicio $ServiceName." }
if (-not (Test-Path $PackageDir -PathType Container)) { Fail "No existe la carpeta del paquete $PackageDir." }
$PackageDir = (Resolve-Path $PackageDir).Path
$currentVersion = Get-AgentVersion $installedExe

# -- 1. Copia de trabajo solo para Administradores ---------------------------
$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$staging = "$InstallDir.new-$stamp"
$prev = "$InstallDir.prev"
$failed = "$InstallDir.failed-$stamp"
if (Test-Path $staging) { Fail "Ya existe $staging." }
function Remove-Staging { if (Test-Path $staging) { Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue } }
try {
  New-Item -ItemType Directory -Path $staging | Out-Null
  Invoke-Icacls @($staging, '/setowner', $admins)
  Invoke-Icacls @($staging, '/inheritance:r', '/grant:r', "${admins}:(OI)(CI)F", "${system}:(OI)(CI)F")
  Get-ChildItem -LiteralPath $PackageDir -Force | Copy-Item -Destination $staging -Recurse -Force
} catch { Remove-Staging; Fail "No se pudo preparar la copia de trabajo: $($_.Exception.Message)" }

# -- 2-3. Validacion SOLO sobre la copia --------------------------------------
function Reject([string]$msg) { Remove-Staging; Fail $msg 4 }
$verifyArgs = @('--verify-update', $staging)
if ($AllowUnsignedManifest) { $verifyArgs += '--allow-unsigned-manifest' }
& $installedExe @verifyArgs
if ($LASTEXITCODE -ne 0) { Reject 'El agente instalado rechazó el paquete (ver motivo arriba).' }
try { $manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $staging 'release.json') | ConvertFrom-Json }
catch { Reject 'release.json ilegible.' }
Write-Host "Instalada: $currentVersion  ->  Paquete: $($manifest.version)"

$currentSig = Get-AuthenticodeSignature -FilePath $installedExe
$installedSigned = ($currentSig.Status -eq 'Valid')
if ($manifest.authenticode.signed) {
  if ($ExpectedSignerThumbprint) { $thumb = $ExpectedSignerThumbprint.ToUpper() }
  elseif ($installedSigned) { $thumb = $currentSig.SignerCertificate.Thumbprint }
  else {
    Reject ('Primer paquete firmado sobre una instalación sin firma: indique -ExpectedSignerThumbprint con la ' +
            'huella del certificado de Nexus obtenida por un canal independiente (no se toma del manifiesto).')
  }
  foreach ($exe in $manifest.executables) {
    $sig = Get-AuthenticodeSignature -FilePath (Join-Path $staging $exe)
    if ($sig.Status -ne 'Valid') { Reject "Firma Authenticode de $exe no válida: $($sig.Status)" }
    if ($sig.SignerCertificate.Thumbprint -ne $thumb) {
      Reject "Firmante de $exe = $($sig.SignerCertificate.Subject) ($($sig.SignerCertificate.Thumbprint)); se esperaba $thumb."
    }
  }
} elseif ($installedSigned -and -not $AllowSignatureDowngrade) {
  Reject 'La versión instalada está firmada y el paquete nuevo NO: se rechaza (use -AllowSignatureDowngrade solo si es intencional).'
} else {
  Write-Host 'AVISO: paquete SIN firma Authenticode.' -ForegroundColor Yellow
}

# Permisos definitivos (la cuenta del servicio solo lee/ejecuta su programa).
try {
  # Solo en la raíz, con herencia; el contenido la hereda (/reset). Aplicar /inheritance:r +
  # /grant:r con /T a cada archivo terminaba en "Acceso denegado" al ejecutar NexusAgent.exe.
  Invoke-Icacls @($staging, '/inheritance:r', '/grant:r', "${admins}:(OI)(CI)F", "${system}:(OI)(CI)F",
    "${account}:(OI)(CI)RX", "${users}:(OI)(CI)RX", '/Q')
  Invoke-Icacls @((Join-Path $staging '*'), '/reset', '/T', '/C', '/Q')
} catch { Remove-Staging; Fail "No se pudieron fijar los permisos: $($_.Exception.Message)" }

# -- 4. Parada ----------------------------------------------------------------
Write-Host "Deteniendo $ServiceName..."
if (-not (Stop-Agent $StopTimeoutSeconds)) {
  Remove-Staging
  try { Start-Service -Name $ServiceName -ErrorAction Stop } catch { }
  Fail "El servicio o su proceso no terminó en el plazo; no se cambió nada (estado actual: $((Get-Service $ServiceName).Status))."
}

# -- 5. Cambio, arranque y salud (cualquier excepción -> restauración) -------
function Test-Health([string]$Version, [datetime]$Since) {
  Start-Service -Name $ServiceName -ErrorAction Stop
  $deadline = (Get-Date).AddSeconds($HealthSeconds)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    if ((Get-Service -Name $ServiceName).Status -ne 'Running') { return $false }
  }
  $logDir = Join-Path $env:ProgramData 'NexusAgent\logs'
  $bin = (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'").PathName
  if ($bin -match '--config "([^"]+)"') {
    $line = Get-Content -Encoding UTF8 $Matches[1] | Where-Object { $_ -match '^\s*log_dir\s*=' } | Select-Object -First 1
    if ($line) { $logDir = ($line -split '=', 2)[1].Trim() }
  }
  $log = Join-Path $logDir 'nexus_agent.log'
  if (-not (Test-Path $log)) { Write-Host "AVISO: no existe $log." -ForegroundColor Yellow; return $false }
  # Solo cuentan líneas escritas DESPUÉS de este arranque (hora local, como las escribe el agente).
  $marker = "Nexus DWH Agent (PostgreSQL) v$Version"
  $ok = $false
  foreach ($l in (Get-Content -Encoding UTF8 -Tail 400 $log)) {
    if ($l -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \|' -and $l.Contains($marker)) {
      $t = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
      if ($t -ge $Since.AddSeconds(-2)) { $ok = $true }
    }
  }
  if (-not $ok) { Write-Host "AVISO: el log no muestra un arranque de v$Version posterior a $Since." -ForegroundColor Yellow }
  return ($ok -and (Get-Service -Name $ServiceName).Status -eq 'Running')
}

$movedOld = $false; $swapped = $false; $healthy = $false
try {
  if (Test-Path $prev) { Remove-Item -Recurse -Force $prev }
  Rename-WithRetry $InstallDir (Split-Path -Leaf $prev); $movedOld = $true
  Rename-WithRetry $staging (Split-Path -Leaf $InstallDir); $swapped = $true
  $since = Get-Date
  $healthy = Test-Health $manifest.version $since
} catch {
  Write-Host "ERROR durante el cambio o el arranque: $($_.Exception.Message)" -ForegroundColor Red
}

if ($healthy) {
  Write-Host "Actualizado a $($manifest.version). La versión anterior queda en $prev (se reemplaza en la próxima actualización)."
  exit 0
}

# -- Restauración --------------------------------------------------------------
Write-Host 'La versión nueva no quedó sana: se restaura la anterior.' -ForegroundColor Red
$problems = @()
if (-not (Stop-Agent $StopTimeoutSeconds)) {
  # Proceso nuevo colgado: se fuerza su fin para poder restaurar (solo el de la carpeta del programa).
  Get-Process -Name NexusAgent -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force -ErrorAction SilentlyContinue
  if (-not (Test-ProcessGone $InstallDir 30)) { $problems += 'el proceso nuevo no terminó' }
}
if ($swapped) {
  try { Rename-WithRetry $InstallDir (Split-Path -Leaf $failed) } catch { $problems += $_.Exception.Message }
}
if ($movedOld -and -not (Test-Path $InstallDir)) {
  try { Rename-WithRetry $prev (Split-Path -Leaf $InstallDir) } catch { $problems += $_.Exception.Message }
}
if (-not $swapped) { Remove-Staging }
try { Start-Service -Name $ServiceName -ErrorAction Stop } catch { $problems += "Start-Service: $($_.Exception.Message)" }
Start-Sleep -Seconds 5
$status = (Get-Service -Name $ServiceName).Status
$restoredVersion = if (Test-Path $installedExe) { try { Get-AgentVersion $installedExe } catch { "(error: $($_.Exception.Message))" } } else { '(sin programa)' }
if ($problems.Count -gt 0 -or $status -ne 'Running' -or $restoredVersion -ne $currentVersion) {
  Write-Host "ATENCIÓN: la restauración NO quedó completa. Servicio: $status. Programa en ${InstallDir}: $restoredVersion." -ForegroundColor Red
  foreach ($p in $problems) { Write-Host "  - $p" -ForegroundColor Red }
  Write-Host "  Versión anterior: $prev (si existe). Versión fallida: $failed (si existe)." -ForegroundColor Red
  Write-Host "  Acción manual: detener el servicio, dejar la versión $currentVersion en $InstallDir y  Start-Service $ServiceName." -ForegroundColor Red
  exit 1
}
if ($swapped) { Write-Host "Restaurada la versión $currentVersion (servicio en ejecución). La versión fallida quedó en $failed para diagnóstico." }
else { Write-Host "No se llegó a cambiar la versión; $currentVersion sigue instalada y en ejecución." }
exit 3
