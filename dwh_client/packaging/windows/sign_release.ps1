<#
.SYNOPSIS
  Firma con Authenticode el ejecutable y los scripts del paquete y regenera release.json.

.DESCRIPTION
  NO crea certificados ni simula firmas: si no hay certificado/servicio de firma disponible, falla.
  Desde junio de 2023 las claves de firma de código (OV/EV) deben residir en hardware (token/HSM)
  o en un servicio de firma en la nube; por eso se soportan dos modos:

    -Mode CertStore        certificado instalado en el almacén de Windows (clave en token USB/HSM
                           con su KSP/CSP), seleccionado por huella (-CertThumbprint).
    -Mode TrustedSigning   Azure Trusted Signing (Artifact Signing) vía signtool /dlib con el
                           "Trusted Signing Client Tools" (-DlibPath) y un metadata.json (-MetadataPath).

  Siempre con sello de tiempo RFC 3161 (-TimestampUrl, obligatorio): la firma sigue siendo válida
  después de que caduque el certificado.

  Firma: NexusAgent.exe y scripts\*.ps1 (el paquete lo produce Nexus). -IncludeThirdPartyBinaries
  firma además las DLL/PYD de terceros que no traigan firma (útil con políticas WDAC/AppLocker que
  exigen firma en todo lo que se carga; implica avalarlas con el certificado de Nexus).

  Después ejecuta packaging\make_manifest.py --authenticode-signed (los SHA-256 cambian al firmar)
  y elimina release.json.sig previo. La firma Ed25519 del manifiesto es un paso posterior
  (tools\sign_manifest.py en la máquina que custodia la clave de publicación).
.EXAMPLE
  .\sign_release.ps1 -PackageDir ..\..\build\dist\NexusAgent -Mode CertStore `
      -CertThumbprint 0123ABCD... -TimestampUrl http://timestamp.digicert.com
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$PackageDir,
  [Parameter(Mandatory = $true)][ValidateSet('CertStore', 'TrustedSigning')][string]$Mode,
  [Parameter(Mandatory = $true)][string]$TimestampUrl,
  [string]$CertThumbprint = '',
  [string]$DlibPath = '',
  [string]$MetadataPath = '',
  [string]$SignToolPath = '',
  [string]$Python = 'python',
  [switch]$IncludeThirdPartyBinaries
)
$ErrorActionPreference = 'Stop'
function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

$PackageDir = (Resolve-Path $PackageDir).Path
if (-not $SignToolPath) {
  $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($cmd) { $SignToolPath = $cmd.Source }
  else {
    $SignToolPath = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
  }
}
if (-not $SignToolPath -or -not (Test-Path $SignToolPath)) { Fail 'No se encontró signtool.exe (Windows SDK).' }

$signArgs = @('sign', '/v', '/fd', 'SHA256', '/tr', $TimestampUrl, '/td', 'SHA256')
switch ($Mode) {
  'CertStore' {
    if (-not $CertThumbprint) { Fail 'Falta -CertThumbprint.' }
    $cert = Get-ChildItem -Path Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert -ErrorAction SilentlyContinue |
      Where-Object { $_.Thumbprint -eq $CertThumbprint.ToUpper() } | Select-Object -First 1
    if (-not $cert) { Fail "No hay un certificado de firma de código con huella $CertThumbprint en el almacén." }
    if ($cert.NotAfter -lt (Get-Date)) { Fail "El certificado venció el $($cert.NotAfter)." }
    $signArgs += @('/sha1', $cert.Thumbprint)
    $subject = $cert.Subject; $thumb = $cert.Thumbprint
  }
  'TrustedSigning' {
    if (-not (Test-Path $DlibPath)) { Fail 'Falta -DlibPath (Azure.CodeSigning.Dlib.dll).' }
    if (-not (Test-Path $MetadataPath)) { Fail 'Falta -MetadataPath (metadata.json de Trusted Signing).' }
    $signArgs += @('/dlib', $DlibPath, '/dmdf', $MetadataPath)
    $subject = ''; $thumb = ''
  }
}

$targets = @((Join-Path $PackageDir 'NexusAgent.exe'))
$targets += Get-ChildItem -Path (Join-Path $PackageDir 'scripts') -Filter *.ps1 -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
if ($IncludeThirdPartyBinaries) {
  $targets += Get-ChildItem -Path $PackageDir -Recurse -Include *.dll, *.pyd |
    Where-Object { (Get-AuthenticodeSignature $_.FullName).Status -eq 'NotSigned' } | ForEach-Object { $_.FullName }
}

& $SignToolPath @signArgs @targets
if ($LASTEXITCODE -ne 0) { Fail "signtool sign devolvió $LASTEXITCODE" }

foreach ($t in $targets) {
  & $SignToolPath verify /pa /q $t
  if ($LASTEXITCODE -ne 0) { Fail "signtool verify falló para $t" }
  $sig = Get-AuthenticodeSignature -FilePath $t
  if ($sig.Status -ne 'Valid') { Fail "Firma no válida en $t : $($sig.Status)" }
  if (-not $thumb) { $thumb = $sig.SignerCertificate.Thumbprint; $subject = $sig.SignerCertificate.Subject }
}
Write-Host "Firmados $($targets.Count) archivo(s) por: $subject ($thumb)"

$makeManifest = Join-Path (Split-Path -Parent $PSScriptRoot) 'make_manifest.py'
& $Python $makeManifest --package $PackageDir --authenticode-signed --signer-subject $subject --signer-thumbprint $thumb
if ($LASTEXITCODE -ne 0) { Fail 'make_manifest.py falló.' }
Write-Host 'Siguiente paso: firmar release.json con tools\sign_manifest.py (clave Ed25519 de publicación, fuera de línea).'
exit 0
