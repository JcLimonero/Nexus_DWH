"""
api.py — cliente HTTP de la API /agent de Nexus.

* TLS SIEMPRE verificado (``verify=True`` o la ruta de ``ca_bundle``);
  no existe ninguna ruta de código con verify=False.
* ``http://`` solo a localhost/127.0.0.1/::1. Para otro host hace falta
  ``allow_insecure_http = true`` Y ``mode = development``; en production
  se rechaza al arrancar.
* Sin redirecciones automáticas (los headers de credencial no deben viajar a
  otro host).
* Cualquier ``requests.RequestException``, 5xx o JSON inválido se traduce en
  ``ApiUnavailable`` (transitorio: se reintenta). 401 → ``ApiAuthError``,
  403 → ``ApiForbidden``, otros 4xx → ``ApiRejected`` (definitivos).
* Cada hilo (scheduler, heartbeat, envío de cola) usa su propia instancia
  (su propia ``requests.Session``).
"""

import gzip
import json
import os
import platform
import socket
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from . import AGENT_FEATURES, AGENT_VERSION
from .credstore import InstallationCredential
from .sanitize import ConfigError, redact_text
from .settings import Settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class ApiUnavailable(ApiError):
    """Transitorio: red, timeout, 5xx, respuesta inválida."""


class ApiAuthError(ApiError):
    """401: credencial inválida o instalación revocada."""


class ApiForbidden(ApiError):
    """403: fuera de alcance o alcance deshabilitado."""


class ApiRejected(ApiError):
    """Otros 4xx: el reporte no es aceptable (no reintentar)."""


def validate_api_url(settings: Settings) -> None:
    parsed = urlparse(settings.api_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ConfigError("[nexus] api_url no es una URL http(s) válida.")
    if parsed.scheme == "https":
        if settings.ca_bundle and not os.path.exists(settings.ca_bundle):
            raise ConfigError("[nexus] ca_bundle apunta a un archivo inexistente.")
        return
    if host in LOCAL_HOSTS:
        return
    if settings.is_production:
        raise ConfigError("api_url usa http:// hacia un host no local: en mode=production se exige HTTPS.")
    if not settings.allow_insecure_http:
        raise ConfigError("api_url usa http:// hacia un host no local. Use HTTPS "
                          "(o allow_insecure_http=true solo con mode=development).")


class CredentialHolder:
    """Credencial compartida entre hilos (se reemplaza completa al rotar)."""

    def __init__(self, cred: Optional[InstallationCredential] = None) -> None:
        self._lock = threading.Lock()
        self._cred = cred

    def get(self) -> Optional[InstallationCredential]:
        with self._lock:
            return self._cred

    def set(self, cred: Optional[InstallationCredential]) -> None:
        with self._lock:
            self._cred = cred


def installation_info(settings: Settings) -> Dict[str, Any]:
    hostname = socket.gethostname()
    return {
        "name": settings.installation_name or hostname,
        "hostname": hostname,
        "os_info": f"{platform.system()} {platform.release()}",
        "client_version": AGENT_VERSION,
        "fingerprint": {
            "machine": platform.machine(),
            "python": platform.python_version(),
            "platform": platform.platform(terse=True),
        },
    }


class NexusApi:
    def __init__(self, settings: Settings, holder: Optional[CredentialHolder] = None) -> None:
        validate_api_url(settings)
        self.settings = settings
        self.holder = holder or CredentialHolder()
        self.base = settings.api_url.rstrip("/")
        self.session = requests.Session()
        # Nunca False: True (CAs del sistema/certifi) o bundle propio.
        self.session.verify = settings.ca_bundle or True
        self.session.headers["User-Agent"] = f"nexus-dwh-agent/{AGENT_VERSION}"
        # Capacidades (sección 22): sin "destination-v2" Nexus retiene las tareas que este agente
        # no podría ejecutar bien (destino por empresa, SSL obligatorio).
        self.session.headers["x-nexus-agent-features"] = ",".join(AGENT_FEATURES)
        self.timeout = (settings.http_connect_timeout, settings.http_read_timeout)

    def close(self) -> None:
        self.session.close()

    def _auth_headers(self) -> Dict[str, str]:
        cred = self.holder.get()
        if cred is None:
            raise ApiAuthError(401, "no_credential", "Sin credencial de instalación.")
        if cred.api_url.rstrip("/") != self.base:
            raise ConfigError("La credencial guardada pertenece a otra api_url; re-enrole la instalación.")
        return {"x-installation-id": cred.installation_id, "x-installation-secret": cred.secret}

    def request(self, method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None, auth: bool = True, compress: bool = False) -> Dict[str, Any]:
        used = self.holder.get() if auth else None
        try:
            return self._request_once(method, path, json_body=json_body, headers=headers, auth=auth,
                                      compress=compress)
        except ApiAuthError as exc:
            # Otro hilo pudo rotar el secreto mientras esta petición iba en vuelo:
            # se reintenta UNA vez con la credencial vigente antes de darla por mala.
            if auth and exc.code != "installation_revoked" and self.holder.get() is not used:
                return self._request_once(method, path, json_body=json_body, headers=headers, auth=auth,
                                          compress=compress)
            raise

    def _request_once(self, method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None,
                      headers: Optional[Dict[str, str]] = None, auth: bool = True,
                      compress: bool = False) -> Dict[str, Any]:
        hdrs = dict(headers or {})
        if auth:
            hdrs.update(self._auth_headers())
        url = f"{self.base}{path}"
        kwargs: Dict[str, Any] = {"json": json_body}
        if compress and json_body is not None:
            # Cuerpo grande (inventario): gzip; Nexus lo descomprime con límite (anti zip-bomb).
            kwargs = {"data": gzip.compress(json.dumps(json_body, separators=(",", ":"),
                                                       ensure_ascii=False).encode("utf-8"), 6)}
            hdrs.update({"Content-Type": "application/json", "Content-Encoding": "gzip"})
        try:
            r = self.session.request(method, url, headers=hdrs, timeout=self.timeout,
                                     allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            raise ApiUnavailable(0, "network_error", redact_text(f"{type(exc).__name__}: {exc}", max_len=200))
        status = r.status_code
        data: Any = None
        try:
            data = r.json() if r.content else {}
        except ValueError:
            if status < 400:
                raise ApiUnavailable(status, "invalid_json", "Respuesta no JSON del servidor.")
        if 300 <= status < 400:
            raise ApiUnavailable(status, "redirect", "El servidor respondió con redirección (no se sigue).")
        if status >= 500 or status in (408, 429):
            raise ApiUnavailable(status, "server_error", f"HTTP {status}")
        if status >= 400:
            code, message = f"http_{status}", f"HTTP {status}"
            detail = data.get("detail") if isinstance(data, dict) else None
            if isinstance(detail, dict):
                code = str(detail.get("code") or code)
                message = str(detail.get("message") or message)
            elif isinstance(detail, str):
                message = detail
            message = redact_text(message, max_len=300)
            if status == 401:
                raise ApiAuthError(status, code, message)
            if status == 403:
                raise ApiForbidden(status, code, message)
            raise ApiRejected(status, code, message)
        if not isinstance(data, dict):
            raise ApiUnavailable(status, "invalid_json", "Respuesta JSON inesperada.")
        return data

    # ── Endpoints ───────────────────────────────────────────────────────────
    def enroll(self, token_kind: str, token: str, info: Dict[str, Any]) -> Dict[str, Any]:
        header = {"group": "x-group-token", "agency": "x-agency-token", "company": "x-token"}[token_kind]
        return self.request("POST", "/agent/enroll", json_body=info, headers={header: token}, auth=False)

    def get_tasks(self) -> Dict[str, Any]:
        return self.request("GET", "/agent/tasks")

    def start_execution(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/agent/executions", json_body=payload)

    def update_execution(self, execution_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("PUT", f"/agent/executions/{execution_id}", json_body=payload)

    def heartbeat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/agent/heartbeat", json_body=payload)

    def send_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/agent/events", json_body=payload)

    def rotate_credentials(self) -> Dict[str, Any]:
        return self.request("POST", "/agent/credentials/rotate")

    # Inventario estructural (sección 19)
    def inventory_lease(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/agent/inventory/lease", json_body=payload)

    def inventory_snapshot(self, payload: Dict[str, Any], compress: bool = True) -> Dict[str, Any]:
        return self.request("POST", "/agent/inventory/snapshots", json_body=payload, compress=compress)

    # Prueba de conexión pedida desde el panel (sección 22)
    def claim_connection_test(self) -> Dict[str, Any]:
        return self.request("POST", "/agent/connection-tests/claim")

    def report_connection_test(self, test_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", f"/agent/connection-tests/{test_id}/result", json_body=payload)
