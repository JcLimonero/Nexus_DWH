"""
updates.py — manifiesto de publicación y validación de actualizaciones del agente.

Paquete de una versión (carpeta ``NexusAgent/`` generada por el build):

  NexusAgent.exe, *.dll, *.pyd …   archivos del programa
  config.example.ini               plantilla SIN valores
  release.json                     manifiesto (versión, archivos, SHA-256, firma Authenticode)
  release.json.sig                 firma Ed25519 del manifiesto (bytes exactos del archivo)

Validación (``verify_release``), en este orden, y cualquier fallo rechaza el
paquete completo:

  1. ``release.json`` legible y con el formato esperado (producto, plataforma).
  2. Firma Ed25519 de ``release.json`` con una clave de confianza COMPILADA en el
     agente instalado (``release_keys.TRUSTED_RELEASE_KEYS``). Sin firma solo se
     acepta con ``allow_unsigned=True`` y SOLO si el agente no tiene claves
     compiladas (modo de transición: verifica integridad, NO autenticidad); en
     cuanto existan claves de publicación, un manifiesto sin firma se rechaza siempre.
  3. Versión estrictamente mayor que la instalada (sin downgrade; la misma
     versión solo con ``allow_same_version``) y ``min_from_version`` respetado.
  4. Sin enlaces simbólicos/junctions; rutas seguras en Windows (sin ':' ni
     nombres de dispositivo, sin puntos/espacios finales, sin duplicados que solo
     difieran en mayúsculas); cada archivo listado existe, con el tamaño y el SHA-256 declarados; rutas
     relativas sin ``..``; y NO hay archivos extra fuera del manifiesto (evita
     que se cuele una DLL en la carpeta del paquete).
  5. Si el manifiesto declara firma Authenticode, en Windows se exige que los
     ejecutables listados tengan firma válida (``WinVerifyTrust``).

Los tests generan claves efímeras en tiempo de prueba: eso NO es una firma de
producción. La clave de producción está pendiente (ver release_keys.py).
"""

import base64
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

MANIFEST_NAME = "release.json"
SIGNATURE_NAME = "release.json.sig"
MANIFEST_FORMAT = "nexus-agent-release/1"
PRODUCT = "NexusAgent"
_VERSION_RE = re.compile(r"^\d{1,5}(\.\d{1,5}){1,3}$")
_EXCLUDED = {MANIFEST_NAME, SIGNATURE_NAME}


class UpdateVerificationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass
class VerificationResult:
    version: str
    key_id: str = ""
    signed_manifest: bool = False
    authenticode: str = "not_declared"
    files: int = 0
    warnings: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────
def parse_version(value: str) -> Tuple[int, ...]:
    value = (value or "").strip()
    if not _VERSION_RE.match(value):
        raise UpdateVerificationError("bad_version", f"Versión no válida: {value[:40]!r}")
    parts = tuple(int(p) for p in value.split("."))
    return parts + (0,) * (4 - len(parts))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def key_id_for(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _load_public(b64: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = base64.b64decode(b64)
    if len(raw) != 32:
        raise UpdateVerificationError("bad_key", "Clave pública Ed25519 inválida (deben ser 32 bytes).")
    return Ed25519PublicKey.from_public_bytes(raw)


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                   *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_safe_relpath(rel: str) -> bool:
    """
    Ruta relativa segura en Windows y POSIX: sin raíz ni unidad, sin '.'/'..', sin ':' (flujos
    alternativos NTFS), sin caracteres de control, sin componentes terminados en punto o espacio
    (Windows los recorta: "a.dll." == "a.dll") y sin nombres de dispositivo reservados
    (CON, PRN, AUX, NUL, COM1-9, LPT1-9, con o sin extensión).
    """
    if not isinstance(rel, str) or not rel or len(rel) > 400:
        return False
    if rel.startswith(("/", "\\")) or ":" in rel or any(ord(c) < 32 for c in rel):
        return False
    for part in re.split(r"[\\/]", rel):
        if part in ("", ".", "..") or part[-1] in (".", " "):
            return False
        if part.split(".")[0].strip().upper() in _RESERVED_NAMES:
            return False
    return True


def _is_reparse(path: str) -> bool:
    """Enlace simbólico, junction u otro punto de reanálisis (sin seguirlo)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def check_no_links(package_dir: str) -> None:
    """Rechaza enlaces simbólicos/junctions en el paquete (archivos y carpetas), incluida la raíz."""
    if _is_reparse(package_dir):
        raise UpdateVerificationError("link_in_package", "La carpeta del paquete es un enlace/junction.")
    for root, dirs, files in os.walk(package_dir, followlinks=False):
        for name in dirs + files:
            full = os.path.join(root, name)
            if _is_reparse(full):
                rel = os.path.relpath(full, package_dir).replace(os.sep, "/")
                raise UpdateVerificationError("link_in_package", f"Enlace simbólico/junction no permitido: {rel}")


def list_package_files(package_dir: str) -> List[str]:
    """Rutas relativas (con '/') de todos los archivos del paquete, sin el manifiesto ni su firma."""
    out = []
    for root, _dirs, files in os.walk(package_dir, followlinks=False):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), package_dir).replace(os.sep, "/")
            if rel not in _EXCLUDED:
                out.append(rel)
    return sorted(out)


# ─────────────────────────────────────────────────────────────────────────────
# Construcción y firma (lado de publicación)
# ─────────────────────────────────────────────────────────────────────────────
def build_manifest(package_dir: str, version: str, *, platform: str, build: Optional[Dict[str, Any]] = None,
                   authenticode: Optional[Dict[str, Any]] = None, min_from_version: str = "",
                   executables: Iterable[str] = ("NexusAgent.exe",)) -> Dict[str, Any]:
    parse_version(version)
    if min_from_version:
        parse_version(min_from_version)
    files = []
    for rel in list_package_files(package_dir):
        full = os.path.join(package_dir, *rel.split("/"))
        files.append({"path": rel, "size": os.path.getsize(full), "sha256": sha256_file(full)})
    if not files:
        raise UpdateVerificationError("empty_package", "La carpeta del paquete está vacía.")
    return {
        "format": MANIFEST_FORMAT,
        "product": PRODUCT,
        "version": version,
        "platform": platform,
        "min_from_version": min_from_version,
        "build": build or {},
        "authenticode": authenticode or {"signed": False, "note": "SIN FIRMAR"},
        "executables": [e for e in executables if any(f["path"] == e for f in files)],
        "files": files,
    }


def manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sign_manifest(data: bytes, private_key: Any) -> Dict[str, str]:
    """Firma Ed25519 de los bytes EXACTOS de release.json (``private_key``: Ed25519PrivateKey)."""
    from cryptography.hazmat.primitives import serialization

    pub_raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"algorithm": "ed25519", "key_id": key_id_for(pub_raw),
            "signature": base64.b64encode(private_key.sign(data)).decode("ascii")}


# ─────────────────────────────────────────────────────────────────────────────
# Verificación (lado del agente instalado)
# ─────────────────────────────────────────────────────────────────────────────
def _read_json(path: str, code: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise UpdateVerificationError(code, f"{os.path.basename(path)} demasiado grande.")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("no es un objeto")
        return data
    except UpdateVerificationError:
        raise
    except FileNotFoundError:
        raise UpdateVerificationError(code, f"Falta {os.path.basename(path)} en el paquete.")
    except Exception as exc:  # noqa: BLE001
        raise UpdateVerificationError(code, f"{os.path.basename(path)} ilegible: {type(exc).__name__}")


def verify_manifest_signature(data: bytes, sig: Dict[str, Any], trusted_keys: Dict[str, str]) -> str:
    from cryptography.exceptions import InvalidSignature

    if sig.get("algorithm") != "ed25519":
        raise UpdateVerificationError("bad_signature", "Algoritmo de firma no soportado.")
    key_id = str(sig.get("key_id") or "")
    if key_id not in trusted_keys:
        raise UpdateVerificationError("unknown_key", f"La firma usa una clave no confiable ({key_id[:16] or '?'}).")
    pub = _load_public(trusted_keys[key_id])
    try:
        pub.verify(base64.b64decode(str(sig.get("signature") or "")), data)
    except (InvalidSignature, ValueError):
        raise UpdateVerificationError("bad_signature", "La firma de release.json no es válida.")
    return key_id


def verify_release(package_dir: str, *, installed_version: str, trusted_keys: Optional[Dict[str, str]] = None,
                   allow_unsigned: bool = False, allow_same_version: bool = False,
                   expected_platform: Optional[str] = None,
                   authenticode_check: Optional[Callable[[str], str]] = None) -> VerificationResult:
    if trusted_keys is None:
        from .release_keys import TRUSTED_RELEASE_KEYS

        trusted_keys = TRUSTED_RELEASE_KEYS
    manifest_path = os.path.join(package_dir, MANIFEST_NAME)
    sig_path = os.path.join(package_dir, SIGNATURE_NAME)
    try:
        with open(manifest_path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        raise UpdateVerificationError("no_manifest", "Falta release.json en el paquete.")
    warnings: List[str] = []

    # 1-2. Firma del manifiesto (antes de interpretar nada más).
    key_id = ""
    if os.path.exists(sig_path):
        key_id = verify_manifest_signature(data, _read_json(sig_path, "bad_signature"), trusted_keys)
    elif trusted_keys:
        # Con claves de publicación compiladas, el modo sin firma deja de existir (no hay bypass).
        raise UpdateVerificationError("unsigned_manifest", "El paquete no trae release.json.sig y este agente "
                                                           "exige manifiestos firmados.")
    elif allow_unsigned:
        warnings.append("MANIFIESTO SIN FIRMA: se verifica integridad (SHA-256) pero NO autenticidad del paquete.")
    else:
        raise UpdateVerificationError("unsigned_manifest", "El paquete no trae release.json.sig (firma Ed25519).")

    try:
        manifest = json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise UpdateVerificationError("bad_manifest", "release.json no es JSON válido.")
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT or manifest.get("product") != PRODUCT:
        raise UpdateVerificationError("bad_manifest", "release.json no corresponde a un paquete del agente Nexus.")
    if expected_platform and manifest.get("platform") != expected_platform:
        raise UpdateVerificationError("wrong_platform", f"Plataforma del paquete: {manifest.get('platform')!r}.")

    # 3. Versión (sin downgrade).
    new_v = str(manifest.get("version") or "")
    new_t, cur_t = parse_version(new_v), parse_version(installed_version)
    if new_t < cur_t:
        raise UpdateVerificationError("downgrade", f"El paquete ({new_v}) es anterior a la versión instalada "
                                                   f"({installed_version}).")
    if new_t == cur_t and not allow_same_version:
        raise UpdateVerificationError("same_version", f"La versión {new_v} ya está instalada.")
    min_from = str(manifest.get("min_from_version") or "")
    if min_from and cur_t < parse_version(min_from):
        raise UpdateVerificationError("min_from_version", f"Esta versión exige actualizar primero a {min_from}.")

    # 4. Archivos: sin enlaces, existencia, tamaño, hash y ningún archivo extra.
    check_no_links(package_dir)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise UpdateVerificationError("bad_manifest", "release.json no lista archivos.")
    declared = set()
    declared_ci = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise UpdateVerificationError("bad_manifest", "Entrada de archivo mal formada en el manifiesto.")
        rel = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if not _is_safe_relpath(rel):
            raise UpdateVerificationError("unsafe_path", f"Ruta no permitida en el manifiesto: {str(rel)[:80]!r}")
        if (not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            raise UpdateVerificationError("bad_manifest", f"Tamaño o SHA-256 mal formado para {rel}.")
        rel = rel.replace("\\", "/")
        if rel.lower() in declared_ci:  # NTFS no distingue mayúsculas
            raise UpdateVerificationError("bad_manifest", f"Archivo repetido en el manifiesto: {rel}")
        declared.add(rel)
        declared_ci.add(rel.lower())
        full = os.path.join(package_dir, *rel.split("/"))
        if not os.path.isfile(full):
            raise UpdateVerificationError("missing_file", f"Falta el archivo {rel}.")
        if os.path.getsize(full) != size:
            raise UpdateVerificationError("size_mismatch", f"Tamaño distinto en {rel}.")
        if sha256_file(full).lower() != digest.lower():
            raise UpdateVerificationError("hash_mismatch", f"SHA-256 distinto en {rel}: el archivo fue alterado.")
    extra = sorted(f for f in list_package_files(package_dir) if f.lower() not in declared_ci)
    if extra:
        raise UpdateVerificationError("unexpected_files", "Archivos no declarados en el manifiesto: "
                                                          + ", ".join(extra[:10]))

    # 5. Authenticode.
    authn = manifest.get("authenticode") or {}
    status = "not_declared"
    if authn.get("signed"):
        check = authenticode_check
        if check is None:
            from .authenticode import authenticode_status as check
        results = []
        if not isinstance(manifest.get("executables"), list) or not manifest.get("executables"):
            raise UpdateVerificationError("bad_manifest", "Firma Authenticode declarada sin ejecutables listados.")
        for exe in manifest.get("executables") or []:
            if not isinstance(exe, str) or exe not in declared:
                raise UpdateVerificationError("bad_manifest", f"Ejecutable {exe} no listado en files.")
            st = check(os.path.join(package_dir, *exe.split("/")))
            if st == "unsupported":
                results.append("unsupported")
                continue
            if st != "valid":
                raise UpdateVerificationError("authenticode_invalid",
                                              f"{exe}: firma Authenticode {st} (se esperaba válida).")
            results.append("valid")
        status = "valid" if results and all(r == "valid" for r in results) else "not_checked"
        if status == "not_checked":
            warnings.append("Firma Authenticode declarada pero no comprobable en este sistema (solo Windows).")
    else:
        warnings.append("Ejecutables SIN FIRMA Authenticode (certificado de firma de código pendiente).")

    return VerificationResult(version=new_v, key_id=key_id, signed_manifest=bool(key_id), authenticode=status,
                              files=len(declared), warnings=warnings)


def cli_verify(package_dir: str, installed_version: str, *, allow_unsigned: bool = False,
               allow_same_version: bool = False, out=None) -> int:
    """``NexusAgent.exe --verify-update CARPETA``: 0 válido, 4 rechazado."""
    out = out or sys.stdout
    try:
        res = verify_release(package_dir, installed_version=installed_version, allow_unsigned=allow_unsigned,
                             allow_same_version=allow_same_version,
                             expected_platform="windows-x64" if sys.platform == "win32" else None)
    except UpdateVerificationError as exc:
        print(f"RECHAZADO [{exc.code}] {exc.message}", file=out)
        return 4
    except Exception as exc:  # noqa: BLE001 - manifiesto hostil/mal formado: rechazo controlado, sin traza
        print(f"RECHAZADO [bad_manifest] Paquete no válido ({type(exc).__name__}).", file=out)
        return 4
    print(f"OK versión {res.version} · archivos {res.files} · manifiesto "
          f"{'firmado (' + res.key_id + ')' if res.signed_manifest else 'SIN FIRMA'} · authenticode {res.authenticode}",
          file=out)
    for w in res.warnings:
        print(f"AVISO {w}", file=out)
    return 0
