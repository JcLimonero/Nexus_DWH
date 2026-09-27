"""
credstore.py — credencial de instalación protegida por el sistema operativo.

Windows (producción): DPAPI (CryptProtectData / CryptUnprotectData vía ctypes).
  * credential_scope = user (defecto): solo la cuenta que ejecuta el agente
    (p. ej. la cuenta del servicio) puede descifrar. Si cambia la cuenta del
    servicio hay que re-enrolar.
  * credential_scope = machine: cualquier proceso de ESA máquina puede
    descifrar (útil si el servicio cambia de cuenta; más débil).
  * Límites (ambos modos): un administrador local, o cualquier código que
    corra como la cuenta del servicio, puede descifrar la credencial. DPAPI
    protege contra copiar el archivo a otra máquina/cuenta y contra lectura
    casual del disco o de respaldos, no contra un administrador de la máquina.
    Complementar con ACL de la carpeta de datos (solo cuenta del servicio y
    administradores).

Otros sistemas (desarrollo / pruebas): archivo JSON con permisos 0600 y un
aviso claro en el log: NO está cifrado.

La credencial se liga a la ``api_url``: si config.ini apunta a otro servidor,
el agente se niega a enviar el secreto (evita filtrarlo a un host distinto).
"""

import base64
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from .logsetup import get_logger

ENTROPY = b"NexusDWH-agent-credential-v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04


@dataclass
class InstallationCredential:
    installation_id: str
    secret: str
    api_url: str
    enrolled_at: str = ""
    rotated_at: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# DPAPI (solo Windows)
# ─────────────────────────────────────────────────────────────────────────────
def _dpapi_protect(data: bytes, machine: bool) -> bytes:  # pragma: no cover - solo Windows
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> "DATA_BLOB":
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if machine else 0)
    data_in, entropy, out = blob(data), blob(ENTROPY), DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(data_in), "NexusDWH", ctypes.byref(entropy),
                                    None, None, flags, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(data: bytes, machine: bool) -> bytes:  # pragma: no cover - solo Windows
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> "DATA_BLOB":
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    flags = CRYPTPROTECT_UI_FORBIDDEN
    data_in, entropy, out = blob(data), blob(ENTROPY), DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(data_in), None, ctypes.byref(entropy),
                                      None, None, flags, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cred-", dir=directory)
    try:
        if os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class CredentialStore:
    """Guarda / lee la credencial. Backend DPAPI en Windows, archivo 0600 fuera."""

    def __init__(self, data_dir: str, scope: str = "user", *,
                 protect: Optional[Callable[[bytes, bool], bytes]] = None,
                 unprotect: Optional[Callable[[bytes, bool], bytes]] = None,
                 force_backend: Optional[str] = None) -> None:
        self.data_dir = data_dir
        self.machine = scope == "machine"
        self.backend = force_backend or ("dpapi" if sys.platform == "win32" else "file")
        self._protect = protect or _dpapi_protect
        self._unprotect = unprotect or _dpapi_unprotect
        self._warned = False

    @property
    def path(self) -> str:
        name = "agent_credential.dpapi" if self.backend == "dpapi" else "agent_credential.json"
        return os.path.join(self.data_dir, name)

    def _warn_plain(self) -> None:
        if not self._warned:
            get_logger().warning(
                "Credencial de instalación guardada SIN cifrar (archivo con permisos 0600): "
                "DPAPI solo existe en Windows. Aceptable en desarrollo; en producción use Windows "
                "o proteja la carpeta de datos."
            )
            self._warned = True

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def save(self, cred: InstallationCredential) -> None:
        raw = json.dumps(asdict(cred)).encode("utf-8")
        if self.backend == "dpapi":
            blob = self._protect(raw, self.machine)
            _atomic_write(self.path, base64.b64encode(blob))
        else:
            self._warn_plain()
            _atomic_write(self.path, raw, 0o600)

    def load(self) -> Optional[InstallationCredential]:
        if not self.exists():
            return None
        with open(self.path, "rb") as fh:
            data = fh.read()
        if self.backend == "dpapi":
            raw = self._unprotect(base64.b64decode(data), self.machine)
        else:
            self._warn_plain()
            if os.name != "nt":
                st = os.stat(self.path)
                if st.st_mode & 0o077:
                    get_logger().warning("Permisos demasiado abiertos en la credencial; se corrigen a 0600.")
                    os.chmod(self.path, 0o600)
            raw = data
        d = json.loads(raw.decode("utf-8"))
        return InstallationCredential(**{k: d.get(k, "") for k in InstallationCredential.__dataclass_fields__})

    def self_check(self) -> tuple:
        """
        Comprueba que el backend de protección funciona con la cuenta ACTUAL
        (cifra y descifra un valor aleatorio en memoria; no escribe a disco).
        Útil al arrancar como servicio: con una cuenta virtual (NT SERVICE\\...)
        confirma que DPAPI de usuario está disponible. Devuelve (ok, detalle).
        """
        if self.backend != "dpapi":
            return True, "archivo 0600 (sin cifrar; solo desarrollo)"
        probe = os.urandom(16)
        try:
            ok = self._unprotect(self._protect(probe, self.machine), self.machine) == probe
        except Exception as exc:  # noqa: BLE001
            return False, f"DPAPI ({'machine' if self.machine else 'user'}) falló: {type(exc).__name__}"
        return ok, f"DPAPI ({'machine' if self.machine else 'user'}) {'OK' if ok else 'devolvió datos distintos'}"

    def delete(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
