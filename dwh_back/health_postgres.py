"""
health_postgres.py — Salud, incidencias y notificaciones (PostgreSQL)
─────────────────────────────────────────────────────────────────────
Modelo de salud (calculado en el SERVIDOR con la hora de la BD, NOW()):

  * Instalación: último contacto (installation.last_seen_at, cualquier llamada
    autenticada), último latido, última ejecución, última carga exitosa, cola.
    Conectividad: online / offline (sin contacto > disconnect_after_seconds) /
    revoked / scope_disabled. La conectividad NO dice nada del éxito del ETL.
  * Tarea: activa efectiva, en curso (confirmada por el latido), última
    ejecución, última carga exitosa (0 filas también es éxito), punto de
    sincronización confirmado (watermark + tipo), error actual y fallos
    consecutivos, retraso (periodicidad + duración esperada + tolerancia).

Incidencias (tabla incident):

  * Se agrupan por dedup_key = categoría | instalación | tarea: la misma falla
    repetida incrementa occurrences en la incidencia ABIERTA (una sola por
    clave; índice único parcial) y no vuelve a notificar.
  * RECONOCER (acknowledged_*) ≠ RESOLVER (status). Reconocer nunca cierra
    ni convierte un error en éxito.
  * Se resuelven solo con evidencia:
      - disconnected       → latido/contacto recuperado (NO toca las de tareas)
      - task_failed        → carga confirmada (success, incluida 0 filas) de
                             ESA tarea en ESA instalación, más nueva que la
                             última falla (agent_seq): un evento viejo que llega
                             tarde no reabre ni resuelve.
      - task_delayed       → carga confirmada que deja la tarea al día
      - task_running_long  → la ejecución terminó
      - checkpoint_kind_mismatch → un checkpoint aplicado o el reinicio del watermark
      - queue_dead_letter / queue_overflow → sin evidencia automática posible:
                             se cierran MANUALMENTE con motivo obligatorio.
    Además: tarea deshabilitada/borrada o instalación revocada/borrada cierran
    sus incidencias con ese motivo explícito (no es una recuperación).
  * Historial en incident_event.

Evaluador periódico (hilo del backend + POST /admin/health/evaluate): detecta
desconexiones (el cliente desconectado no puede avisar), retrasos y
ejecuciones prolongadas. Un advisory lock de sesión evita que dos réplicas o
dos llamadas evalúen a la vez.

Notificaciones: outbox transaccional (se encola en la misma transacción que la
transición de la incidencia: apertura, resolución y recordatorio opcional),
entrega con reintentos y backoff por un hilo del backend. Emisores: webhook
(firma HMAC-SHA256) y log. Nunca se notifica en cada ciclo.
"""

import hashlib
import ipaddress
import socket
import hmac
import json
import logging
import math
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from redact import redact_text

log = logging.getLogger("nexus.health")

SEVERITIES = ("info", "warning", "error", "critical")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

CATEGORY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "disconnected": {"severity": "critical", "label": "Instalación sin contacto"},
    "task_failed": {"severity": "error", "label": "Falla de tarea"},
    "task_delayed": {"severity": "warning", "label": "Tarea retrasada"},
    "task_running_long": {"severity": "warning", "label": "Ejecución prolongada"},
    "checkpoint_kind_mismatch": {"severity": "warning", "label": "Tipo de reloj del watermark distinto"},
    "queue_dead_letter": {"severity": "warning", "label": "Reportes apartados en la cola local"},
    "queue_overflow": {"severity": "error", "label": "Descartes en la cola local"},
}
CATEGORIES = tuple(CATEGORY_DEFAULTS)
# Categorías sin evidencia automática de recuperación: cierre manual con motivo.
MANUAL_RESOLVABLE = ("queue_dead_letter", "queue_overflow")
TASK_CATEGORIES = ("task_failed", "task_delayed", "task_running_long", "checkpoint_kind_mismatch")
INSTALLATION_CATEGORIES = ("disconnected", "queue_dead_letter", "queue_overflow")

RESOLUTION_LABELS = {
    "heartbeat_recovered": "Latido recuperado",
    "contact_recovered": "Contacto recuperado",
    "load_confirmed": "Carga confirmada",
    "execution_finished": "La ejecución terminó",
    "checkpoint_applied": "Checkpoint aplicado",
    "watermark_reset": "Watermark reiniciado",
    "thresholds_changed": "Umbrales modificados",
    "task_disabled": "Tarea deshabilitada (no es recuperación)",
    "task_deleted": "Tarea eliminada",
    "installation_revoked": "Instalación revocada",
    "installation_deleted": "Instalación eliminada",
    "scope_disabled": "Grupo/empresa/agencia deshabilitado",
    "manual": "Cerrada manualmente",
}

_EVAL_LOCK_KEY = 834_120_777


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class HealthSettings:
    evaluator_interval_seconds: int = 30
    disconnect_after_seconds: int = 300
    heartbeat_expected_seconds: int = 60
    default_expected_duration_seconds: int = 300
    delay_tolerance_factor: float = 0.5
    delay_min_grace_seconds: int = 300
    running_long_factor: float = 3.0
    expected_duration_history: int = 20
    # Tras arrancar el backend no se abren desconexiones durante este tiempo: si
    # el caído era Nexus, los agentes necesitan un latido para volver a verse.
    # -1 = igual a disconnect_after_seconds.
    startup_grace_seconds: int = -1
    # Retención del historial (días; 0 = sin purga). Nunca se borra la última ejecución
    # ni la última carga exitosa de cada tarea, ni las referenciadas por incidencias
    # abiertas o por task_sync_state, ni ejecuciones en curso. Las incidencias no se
    # purgan; de las resueltas hace más de N días se borran los eventos secundarios
    # (recurrencias/notificaciones) y se conservan apertura, reconocimiento y resolución.
    execution_retention_days: int = 180
    incident_event_retention_days: int = 365

    @classmethod
    def from_ini(cls, ini: Any) -> "HealthSettings":
        d = cls()
        sec = "health"
        return cls(
            evaluator_interval_seconds=ini.getint(sec, "evaluator_interval_seconds", fallback=d.evaluator_interval_seconds),
            disconnect_after_seconds=max(10, ini.getint(sec, "disconnect_after_seconds", fallback=d.disconnect_after_seconds)),
            heartbeat_expected_seconds=ini.getint(sec, "heartbeat_expected_seconds", fallback=d.heartbeat_expected_seconds),
            default_expected_duration_seconds=max(1, ini.getint(sec, "default_expected_duration_seconds",
                                                               fallback=d.default_expected_duration_seconds)),
            delay_tolerance_factor=max(0.0, ini.getfloat(sec, "delay_tolerance_factor", fallback=d.delay_tolerance_factor)),
            delay_min_grace_seconds=max(0, ini.getint(sec, "delay_min_grace_seconds", fallback=d.delay_min_grace_seconds)),
            running_long_factor=max(1.0, ini.getfloat(sec, "running_long_factor", fallback=d.running_long_factor)),
            expected_duration_history=min(200, max(3, ini.getint(sec, "expected_duration_history",
                                                                 fallback=d.expected_duration_history))),
            startup_grace_seconds=ini.getint(sec, "startup_grace_seconds", fallback=d.startup_grace_seconds),
            execution_retention_days=max(0, ini.getint(sec, "execution_retention_days",
                                                       fallback=d.execution_retention_days)),
            incident_event_retention_days=max(0, ini.getint(sec, "incident_event_retention_days",
                                                            fallback=d.incident_event_retention_days)),
        )

    @property
    def effective_startup_grace(self) -> int:
        return self.disconnect_after_seconds if self.startup_grace_seconds < 0 else self.startup_grace_seconds


@dataclass
class NotificationSettings:
    worker_interval_seconds: int = 10
    max_attempts: int = 8
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 3600
    allow_http: bool = False
    # Anti-SSRF: rechaza destinos que resuelven a IPs de loopback, privadas, link-local
    # (169.254/16, fe80::/10), CGNAT (100.64/10), multicast, reservadas o no especificadas.
    # Se verifica al guardar y justo antes de cada envío (tras resolver DNS).
    block_private_ips: bool = True
    sending_stale_seconds: int = 300
    retention_days: int = 30

    @classmethod
    def from_ini(cls, ini: Any) -> "NotificationSettings":
        d = cls()
        sec = "notifications"
        return cls(
            worker_interval_seconds=ini.getint(sec, "worker_interval_seconds", fallback=d.worker_interval_seconds),
            max_attempts=max(1, ini.getint(sec, "max_attempts", fallback=d.max_attempts)),
            backoff_base_seconds=max(1, ini.getint(sec, "backoff_base_seconds", fallback=d.backoff_base_seconds)),
            backoff_max_seconds=max(1, ini.getint(sec, "backoff_max_seconds", fallback=d.backoff_max_seconds)),
            allow_http=ini.getboolean(sec, "allow_http", fallback=d.allow_http),
            block_private_ips=ini.getboolean(sec, "block_private_ips", fallback=d.block_private_ips),
            sending_stale_seconds=max(30, ini.getint(sec, "sending_stale_seconds", fallback=d.sending_stale_seconds)),
            retention_days=max(1, ini.getint(sec, "retention_days", fallback=d.retention_days)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────
def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return dt.isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def dedup_key(category: str, installation_id: Optional[str], task_id: Optional[int]) -> str:
    return f"{category}|{installation_id or '-'}|{task_id if task_id is not None else '-'}"


def max_severity(a: Optional[str], b: Optional[str]) -> str:
    a = a or "info"
    b = b or "info"
    return a if SEV_RANK.get(a, 0) >= SEV_RANK.get(b, 0) else b


def _task_label(h: Dict[str, Any]) -> str:
    obj = h.get("object_name") or h.get("destination_table") or f"tarea {h.get('task_id')}"
    ag = h.get("agency_name") or ""
    return f"{obj} · {ag}" if ag else str(obj)


def mask_url(url: str) -> str:
    """esquema://host[:puerto]/… — nunca ruta ni query (pueden llevar tokens)."""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{host}{port}/…"
    except Exception:
        return "(URL no válida)"


def validate_webhook_url(url: str, allow_http: bool, block_private: bool = True) -> str:
    url = (url or "").strip()
    if not url or len(url) > 2000:
        raise ValueError("URL vacía o demasiado larga.")
    p = urlsplit(url)
    if p.scheme not in ("https", "http"):
        raise ValueError("La URL debe ser https://")
    if p.scheme == "http" and not allow_http:
        raise ValueError("Solo se permiten URLs https:// ([notifications] allow_http = false).")
    if not p.hostname:
        raise ValueError("La URL no tiene host.")
    if p.username or p.password:
        raise ValueError("La URL no debe llevar usuario/contraseña.")
    if block_private:
        check_destination(url)
    return url


_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def ip_blocked(value: str) -> bool:
    """True si la IP no es un destino público (loopback, privada, link-local, CGNAT…)."""
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified
                or ip.is_reserved or (ip.version == 4 and ip in _CGNAT)
                or (ip.version == 6 and (ip.is_site_local or ip.sixtofour is not None or ip.teredo is not None)))


def check_destination(url: str) -> None:
    """
    Resuelve el host del webhook y exige que TODAS sus IPs sean públicas.
    ValueError si no resuelve o si alguna IP está bloqueada.
    """
    p = urlsplit(url)
    host = p.hostname or ""
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        raise ValueError("No se pudo resolver el host del webhook.")
    addrs = {str(i[4][0]) for i in infos}
    if not addrs:
        raise ValueError("No se pudo resolver el host del webhook.")
    if any(ip_blocked(a) for a in addrs):
        raise ValueError("Destino no permitido: el host resuelve a una IP privada, local o reservada "
                         "([notifications] block_private_ips).")


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """Firma del webhook: HMAC-SHA256(secreto, "<timestamp>." + cuerpo) en hex."""
    return hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Emisores
# ─────────────────────────────────────────────────────────────────────────────
class SendResult:
    def __init__(self, ok: bool, status_code: Optional[int] = None, error: Optional[str] = None):
        self.ok = ok
        self.status_code = status_code
        self.error = error


class LogSender:
    """Emisor de desarrollo: solo escribe una línea en el log del servidor."""

    def send(self, channel: Dict[str, Any], payload: Dict[str, Any], delivery_id: str) -> SendResult:
        inc = payload.get("incident") or {}
        log.info("[notificación] canal=%s evento=%s entrega=%s incidencia=%s categoría=%s severidad=%s",
                 channel.get("name"), payload.get("event"), delivery_id, inc.get("id"), inc.get("category"),
                 inc.get("severity"))
        return SendResult(True, None, None)


class WebhookSender:
    """
    POST JSON firmado. Cabeceras:
      X-Nexus-Event, X-Nexus-Delivery (idempotencia: el receptor debe ignorar
      repetidos), X-Nexus-Timestamp (epoch s), X-Nexus-Signature:
      "sha256=" + HMAC-SHA256(secreto, timestamp + "." + cuerpo).
    Sin redirecciones; timeout y verificación TLS por canal.
    """

    def send(self, channel: Dict[str, Any], payload: Dict[str, Any], delivery_id: str) -> SendResult:
        url = channel.get("url") or ""
        if not url:
            return SendResult(False, None, "Canal sin URL")
        if channel.get("block_private", True):
            try:
                check_destination(url)   # en cada envío: el DNS pudo cambiar desde que se guardó
            except ValueError:
                return SendResult(False, None, "Destino bloqueado o no resoluble")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", "User-Agent": "NexusDWH-Notifier/1",
                   "X-Nexus-Event": str(payload.get("event", "")), "X-Nexus-Delivery": delivery_id,
                   "X-Nexus-Timestamp": ts}
        secret = channel.get("signing_secret") or ""
        if secret:
            headers["X-Nexus-Signature"] = "sha256=" + sign_payload(secret, ts, body)
        try:
            r = requests.post(url, data=body, headers=headers, timeout=int(channel.get("timeout_seconds") or 10),
                              verify=bool(channel.get("verify_tls", True)), allow_redirects=False)
        except requests.RequestException as exc:
            # Nunca se registra la URL (puede llevar un token) ni el detalle.
            return SendResult(False, None, type(exc).__name__)
        if 200 <= r.status_code < 300:
            return SendResult(True, r.status_code, None)
        return SendResult(False, r.status_code, f"HTTP {r.status_code}")


SENDERS = {"webhook": WebhookSender(), "log": LogSender()}


# ─────────────────────────────────────────────────────────────────────────────
# Motor
# ─────────────────────────────────────────────────────────────────────────────
class IncidentEngine:
    def __init__(self, *, get_connection: Callable[[], Any], settings: HealthSettings,
                 notif: NotificationSettings, get_secret_cipher: Callable[[], Any],
                 decrypt_config_secret: Callable[[Optional[str]], str]) -> None:
        self.get_connection = get_connection
        self.s = settings
        self.n = notif
        self.get_secret_cipher = get_secret_cipher
        self.decrypt = decrypt_config_secret
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._born = time.monotonic()
        self._last_housekeeping = 0.0
        self._hk_lock = threading.Lock()

    # ── BD ──────────────────────────────────────────────────────────────────
    @contextmanager
    def tx(self) -> Iterator[Any]:
        conn = self.get_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def savepoint(self, cur: Any, what: str, stats: Optional[Dict[str, Any]] = None) -> Iterator[None]:
        """
        Aísla un paso: si falla se revierte SOLO ese paso (ROLLBACK TO SAVEPOINT) y
        se registra el tipo de error (nunca el mensaje: podría llevar datos).
        Los ganchos de incidencias nunca deben tumbar el reporte del agente, y
        una instalación/tarea problemática no debe frenar al evaluador.
        """
        name = "sp_health_" + uuid.uuid4().hex[:8]
        cur.execute(f"SAVEPOINT {name}")
        try:
            yield
            cur.execute(f"RELEASE SAVEPOINT {name}")
        except Exception as exc:  # noqa: BLE001
            cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
            log.error("Salud: error aislado en %s: %s", what, type(exc).__name__)
            if stats is not None:
                stats["errors"] = int(stats.get("errors", 0)) + 1

    # ── Primitivas de incidencias ───────────────────────────────────────────
    def _event(self, cur: Any, incident_id: int, event_type: str, *, actor: str = "system",
               message: Optional[str] = None, execution_id: Optional[str] = None,
               data: Optional[Dict[str, Any]] = None) -> None:
        cur.execute(
            """INSERT INTO incident_event (incident_id, event_type, actor, message, execution_id, data)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (incident_id, event_type, actor[:100], (message or None) and message[:1000], execution_id,
             json.dumps(data or {}, default=str)),
        )

    def _open_row(self, cur: Any, category: str, installation_id: Optional[str],
                  task_id: Optional[int]) -> Optional[Dict[str, Any]]:
        cur.execute("SELECT * FROM incident WHERE dedup_key = %s AND status = 'open' FOR UPDATE",
                    (dedup_key(category, installation_id, task_id),))
        return cur.fetchone()

    def open_or_recur(self, cur: Any, *, category: str, installation_id: Optional[str] = None,
                      installation_name: Optional[str] = None, task_id: Optional[int] = None,
                      scope: Optional[Dict[str, Any]] = None, title: str = "", severity: Optional[str] = None,
                      error_code: Optional[str] = None, message: Optional[str] = None,
                      execution_id: Optional[str] = None, evidence_seq: Optional[int] = None,
                      details: Optional[Dict[str, Any]] = None, recur: bool = True) -> Tuple[Dict[str, Any], str]:
        """
        Abre la incidencia de la clave o, si ya hay una abierta, suma una
        ocurrencia (recur=True). Condiciones continuas (desconexión, retraso)
        usan recur=False: no cuentan una ocurrencia por ciclo del evaluador.
        """
        key = dedup_key(category, installation_id, task_id)
        sev = severity or CATEGORY_DEFAULTS[category]["severity"]
        scope = scope or {}
        details = details or {}
        msg = redact_text(message, max_len=1000) if message else None
        for _ in range(3):
            row = self._open_row(cur, category, installation_id, task_id)
            if row:
                if not recur:
                    return row, "unchanged"
                newer = evidence_seq is None or row["last_evidence_seq"] is None or evidence_seq > row["last_evidence_seq"]
                merged = dict(row["details"] or {})
                if error_code:
                    codes = dict(merged.get("error_codes") or {})
                    if error_code in codes or len(codes) < 20:
                        codes[error_code] = int(codes.get(error_code, 0)) + 1
                    merged["error_codes"] = codes
                for k, v in details.items():
                    if k != "error_codes":
                        merged[k] = v
                cur.execute(
                    """UPDATE incident SET occurrences = occurrences + 1, severity = %s, details = %s,
                              last_seen_at = CASE WHEN %s THEN NOW() ELSE last_seen_at END,
                              last_error_code = CASE WHEN %s THEN COALESCE(%s, last_error_code) ELSE last_error_code END,
                              last_message_sanitized = CASE WHEN %s THEN COALESCE(%s, last_message_sanitized)
                                                            ELSE last_message_sanitized END,
                              last_execution_id = CASE WHEN %s THEN COALESCE(%s, last_execution_id) ELSE last_execution_id END,
                              last_evidence_seq = GREATEST(last_evidence_seq, %s),
                              updated_at = NOW()
                       WHERE id = %s RETURNING *""",
                    (max_severity(row["severity"], sev), json.dumps(merged, default=str), newer, newer, error_code,
                     newer, msg, newer, execution_id, evidence_seq, row["id"]),
                )
                upd = cur.fetchone()
                self._event(cur, row["id"], "recurred", message=msg, execution_id=execution_id,
                            data={"error_code": error_code, "out_of_order": not newer})
                return upd, "recurred"
            det = dict(details)
            if error_code:
                det["error_codes"] = {error_code: 1}
            cur.execute(
                """INSERT INTO incident
                       (dedup_key, category, severity, installation_id, installation_name, task_id, group_id,
                        company_id, agency_id, object_catalog_id, title, last_error_code, last_message_sanitized,
                        details, first_execution_id, last_execution_id, last_evidence_seq)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (dedup_key) WHERE status = 'open' DO NOTHING
                   RETURNING *""",
                (key, category, sev, installation_id, (installation_name or None) and installation_name[:255],
                 task_id, scope.get("group_id"), scope.get("company_id"), scope.get("agency_id"),
                 scope.get("object_catalog_id"), title[:300], error_code, msg, json.dumps(det, default=str),
                 execution_id, execution_id, evidence_seq),
            )
            new = cur.fetchone()
            if new:
                self._event(cur, new["id"], "opened", message=msg or title, execution_id=execution_id,
                            data={"error_code": error_code})
                self._enqueue(cur, new, "opened")
                log.info("Incidencia abierta #%s %s (%s)", new["id"], category, key)
                return new, "opened"
        raise RuntimeError("No se pudo abrir ni actualizar la incidencia")

    def resolve(self, cur: Any, incident: Dict[str, Any], reason: str, *, actor: str = "system",
                comment: Optional[str] = None, execution_id: Optional[str] = None,
                evidence_seq: Optional[int] = None) -> Optional[Dict[str, Any]]:
        cur.execute(
            """UPDATE incident SET status = 'resolved', resolved_at = NOW(), resolution_reason = %s,
                      resolution_comment = %s, resolved_by = %s, resolved_execution_id = %s,
                      resolved_evidence_seq = %s,
                      duration_seconds = GREATEST(0, EXTRACT(EPOCH FROM (NOW() - opened_at)))::bigint,
                      updated_at = NOW()
               WHERE id = %s AND status = 'open' RETURNING *""",
            (reason, (comment or None) and comment[:500], actor[:100], execution_id, evidence_seq, incident["id"]),
        )
        row = cur.fetchone()
        if not row:
            return None
        self._event(cur, row["id"], "resolved", actor=actor,
                    message=RESOLUTION_LABELS.get(reason, reason) + (f": {comment}" if comment else ""),
                    execution_id=execution_id, data={"reason": reason})
        self._enqueue(cur, row, "resolved")
        log.info("Incidencia resuelta #%s %s (%s)", row["id"], row["category"], reason)
        return row

    # ── Ganchos del API del agente (misma transacción que el reporte) ───────
    def on_execution_terminal(self, cur: Any, *, installation_id: str, installation_name: str,
                              task: Dict[str, Any], ex: Dict[str, Any], status: str,
                              error_code: Optional[str], message: Optional[str], warnings: List[str],
                              checkpoint_applied: bool) -> None:
        with self.savepoint(cur, "ejecución"):
            self._on_execution_terminal(cur, installation_id=str(installation_id),
                                        installation_name=installation_name, task=task, ex=ex, status=status,
                                        error_code=error_code, message=message, warnings=warnings,
                                        checkpoint_applied=checkpoint_applied)

    def _on_execution_terminal(self, cur: Any, *, installation_id: str, installation_name: str,
                               task: Dict[str, Any], ex: Dict[str, Any], status: str, error_code: Optional[str],
                               message: Optional[str], warnings: List[str], checkpoint_applied: bool) -> None:
        iid = installation_id
        tid = int(task["task_id"])
        exec_id = str(ex["execution_id"])
        seq = ex.get("start_agent_seq") or ex.get("last_agent_seq")
        scope = {k: task.get(k) for k in ("group_id", "company_id", "agency_id", "object_catalog_id")}
        label = f"{task.get('destination_table') or ('tarea ' + str(tid))} · {task.get('agency_name') or ''}"

        # 1) Ejecución prolongada: al terminar la ejecución, se resuelve.
        row = self._open_row(cur, "task_running_long", iid, tid)
        if row and (row["details"] or {}).get("execution_id") == exec_id:
            self.resolve(cur, row, "execution_finished", execution_id=exec_id, evidence_seq=seq)

        # 2) Falla / recuperación de la tarea en ESTA instalación.
        if status in ("failed", "interrupted"):
            if self._newer_execution_exists(cur, iid, tid, seq, "e.status = 'success'"):
                # Falla vieja que llegó tarde: ya hay una carga confirmada más nueva.
                self._note_late(cur, "task_failed", iid, tid, exec_id, "Falla antigua recibida tarde: se ignora "
                                "(ya hay una carga confirmada más reciente).")
            else:
                self.open_or_recur(
                    cur, category="task_failed", installation_id=iid, installation_name=installation_name,
                    task_id=tid, scope=scope, title=f"Falla de tarea: {label}",
                    severity="error" if status == "failed" else "warning", error_code=error_code,
                    message=message, execution_id=exec_id, evidence_seq=seq,
                    details={"last_status": status, "failure_stage": ex.get("failure_stage")},
                )
        elif status == "success":
            row = self._open_row(cur, "task_failed", iid, tid)
            if row:
                if row["last_evidence_seq"] is None or seq is None or seq > row["last_evidence_seq"]:
                    self.resolve(cur, row, "load_confirmed", execution_id=exec_id, evidence_seq=seq)
                else:
                    self._event(cur, row["id"], "late_evidence_ignored", execution_id=exec_id,
                                message="Éxito antiguo recibido tarde: no resuelve una falla más reciente.")
            row = self._open_row(cur, "task_delayed", None, tid)
            if row:
                h = self.task_health(cur, task_id=tid)
                if h and not h[0]["delayed"]:
                    self.resolve(cur, row, "load_confirmed", execution_id=exec_id, evidence_seq=seq)

        # 3) Checkpoint de otro tipo de reloj (aviso de la fase 1 → incidencia visible).
        if "CHECKPOINT_KIND_MISMATCH" in (warnings or []):
            if not self._newer_execution_exists(cur, iid, tid, seq, "e.checkpoint_confirmed IS NOT NULL"):
                self.open_or_recur(
                    cur, category="checkpoint_kind_mismatch", installation_id=iid,
                    installation_name=installation_name, task_id=tid, scope=scope,
                    title=f"Watermark de otro tipo de reloj: {label}", error_code="CHECKPOINT_KIND_MISMATCH",
                    message="El agente usa un tipo de reloj distinto al del watermark guardado: no se avanza "
                            "el punto de sincronización. Use «Reiniciar última ejecución» para cambiar de reloj.",
                    execution_id=exec_id, evidence_seq=seq)
        elif checkpoint_applied:
            row = self._open_row(cur, "checkpoint_kind_mismatch", iid, tid)
            if row and (row["last_evidence_seq"] is None or seq is None or seq > row["last_evidence_seq"]):
                self.resolve(cur, row, "checkpoint_applied", execution_id=exec_id, evidence_seq=seq)

    def _newer_execution_exists(self, cur: Any, iid: str, tid: int, seq: Optional[int], cond: str) -> bool:
        if seq is None:
            return False
        cur.execute(
            f"""SELECT 1 FROM task_execution e
                WHERE e.task_id = %s AND e.installation_id = %s AND {cond}
                  AND COALESCE(e.start_agent_seq, e.last_agent_seq) > %s LIMIT 1""",
            (tid, iid, seq),
        )
        return cur.fetchone() is not None

    def _note_late(self, cur: Any, category: str, iid: str, tid: int, exec_id: str, msg: str) -> None:
        cur.execute("SELECT id FROM incident WHERE dedup_key = %s ORDER BY id DESC LIMIT 1",
                    (dedup_key(category, iid, tid),))
        r = cur.fetchone()
        if r:
            self._event(cur, r["id"], "late_evidence_ignored", execution_id=exec_id, message=msg)

    def on_heartbeat(self, cur: Any, installation_id: str) -> None:
        """Un latido recibido resuelve la desconexión (y SOLO la desconexión)."""
        with self.savepoint(cur, "latido"):
            row = self._open_row(cur, "disconnected", str(installation_id), None)
            if row:
                self.resolve(cur, row, "heartbeat_recovered")

    def on_agent_event(self, cur: Any, *, installation_id: str, installation_name: str,
                       scope: Dict[str, Any], event_type: str, payload: Dict[str, Any]) -> None:
        if event_type not in ("dead_letter", "queue_overflow"):
            return
        with self.savepoint(cur, "evento"):
            category = "queue_dead_letter" if event_type == "dead_letter" else "queue_overflow"
            nums = {k: v for k, v in payload.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            summary = ", ".join(f"{k}={v}" for k, v in list(nums.items())[:8]) or "sin contadores"
            self.open_or_recur(
                cur, category=category, installation_id=str(installation_id), installation_name=installation_name,
                scope=scope, title=f"{CATEGORY_DEFAULTS[category]['label']}: {installation_name}",
                message=f"Evento {event_type} del agente ({summary}). Revise el log local del agente; "
                        "se cierra manualmente con un motivo.",
                details={"counters": nums},
            )

    def on_watermark_reset(self, cur: Any, task_id: int) -> None:
        with self.savepoint(cur, "reinicio watermark"):
            cur.execute("SELECT * FROM incident WHERE status = 'open' AND category = 'checkpoint_kind_mismatch' "
                        "AND task_id = %s FOR UPDATE", (task_id,))
            for row in cur.fetchall():
                self.resolve(cur, row, "watermark_reset", actor="admin")

    # ── Modelo de salud ─────────────────────────────────────────────────────
    def task_health(self, cur: Any, *, task_id: Optional[int] = None, group_id: Optional[int] = None,
                    company_id: Optional[int] = None, agency_id: Optional[int] = None) -> List[Dict[str, Any]]:
        conds, params = [], []
        for col, val in (("t.id", task_id), ("c.group_id", group_id), ("a.company_id", company_id),
                         ("t.agency_id", agency_id)):
            if val is not None:
                conds.append(f"{col} = %s")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        s = self.s
        cur.execute(
            f"""
            SELECT NOW() AS db_now,
                   t.id AS task_id, t.agency_id, a.name AS agency_name, a.company_id, c.name AS company_name,
                   c.group_id, g.name AS group_name, t.object_catalog_id, o.name AS object_name,
                   o.destination_table, t.is_active,
                   (t.is_active AND a.is_enabled AND o.is_enabled AND c.is_enabled AND g.is_enabled) AS effective_active,
                   t.schedule_seconds, t.expected_duration_seconds, t.delay_tolerance_seconds,
                   s.watermark, s.watermark_kind, s.last_status, COALESCE(s.consecutive_failures, 0) AS consecutive_failures,
                   s.current_error_code, s.last_failure_at,
                   hs.active_since,
                   le.execution_id AS le_id, le.status AS le_status, le.started_at AS le_started,
                   le.finished_at AS le_finished, le.error_code AS le_error, le.rows_loaded AS le_rows,
                   le.installation_id AS le_installation,
                   ls.success_at AS last_success_at, ls.rows_loaded AS last_success_rows,
                   ls.execution_id AS last_success_execution_id,
                   p.p90_ms, COALESCE(p.n_hist, 0) AS n_hist,
                   run.execution_id AS run_id, run.installation_id AS run_installation,
                   run.installation_name AS run_installation_name, run.received_at AS run_since,
                   run.confirmed AS run_confirmed, run.inst_online AS run_inst_online,
                   -- Fallas abiertas de la tarea en CUALQUIER instalación (task_sync_state
                   -- es una sola fila por tarea: el éxito de otra instalación no la "cura").
                   COALESCE((SELECT json_agg(json_build_object(
                                 'incident_id', x.id, 'installation_id', x.installation_id,
                                 'installation_name', x.installation_name, 'error_code', x.last_error_code,
                                 'acknowledged', x.acknowledged_at IS NOT NULL, 'last_seen_at', x.last_seen_at,
                                 'occurrences', x.occurrences) ORDER BY x.last_seen_at DESC)
                             FROM incident x WHERE x.status = 'open' AND x.category = 'task_failed'
                               AND x.task_id = t.id), '[]'::json) AS open_failures,
                   (SELECT COUNT(*) FROM installation i
                     WHERE i.status = 'active' AND i.group_id = c.group_id
                       AND (i.scope_type = 'group'
                            OR (i.scope_type = 'company' AND i.company_id = a.company_id AND t.run_on_company_token)
                            OR (i.scope_type = 'agency' AND i.agency_id = t.agency_id))) AS installations_covering
            FROM agency_task t
            JOIN agency a ON a.id = t.agency_id
            JOIN company c ON c.id = a.company_id
            JOIN client_group g ON g.id = c.group_id
            JOIN object_catalog o ON o.id = t.object_catalog_id
            LEFT JOIN task_sync_state s ON s.task_id = t.id
            LEFT JOIN task_health_state hs ON hs.task_id = t.id
            LEFT JOIN LATERAL (
                SELECT e.execution_id, e.status, e.started_at, e.finished_at, e.error_code, e.rows_loaded,
                       e.installation_id
                FROM task_execution e WHERE e.task_id = t.id
                ORDER BY COALESCE(e.started_at, e.received_at) DESC LIMIT 1) le ON TRUE
            LEFT JOIN LATERAL (
                -- Hora de la carga acotada por la hora de recepción del servidor
                -- (un reloj del agente adelantado no puede ocultar un retraso).
                SELECT LEAST(COALESCE(e.finished_at, e.updated_at), e.updated_at) AS success_at,
                       e.rows_loaded, e.execution_id
                FROM task_execution e WHERE e.task_id = t.id AND e.status = 'success'
                ORDER BY LEAST(COALESCE(e.finished_at, e.updated_at), e.updated_at) DESC LIMIT 1) ls ON TRUE
            LEFT JOIN LATERAL (
                SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY x.d) AS p90_ms, COUNT(*) AS n_hist
                FROM (SELECT e.duration_ms AS d FROM task_execution e
                      WHERE e.task_id = t.id AND e.status = 'success' AND e.duration_ms IS NOT NULL
                      ORDER BY e.received_at DESC LIMIT %s) x) p ON TRUE
            LEFT JOIN LATERAL (
                SELECT e.execution_id, e.installation_id, i.name AS installation_name, e.received_at,
                       COALESCE(i.last_heartbeat -> 'running', '[]'::jsonb)
                           @> jsonb_build_array(jsonb_build_object('execution_id', e.execution_id::text)) AS confirmed,
                       (i.status = 'active' AND i.last_seen_at >= NOW() - make_interval(secs => %s)) AS inst_online
                FROM task_execution e JOIN installation i ON i.id = e.installation_id
                WHERE e.task_id = t.id AND e.status = 'running'
                ORDER BY (i.status = 'active' AND i.last_seen_at >= NOW() - make_interval(secs => %s)) DESC,
                         e.received_at DESC LIMIT 1) run ON TRUE
            {where}
            ORDER BY g.name, c.name, a.name, o.name
            """,
            (s.expected_duration_history, s.disconnect_after_seconds, s.disconnect_after_seconds, *params),
        )
        rows = cur.fetchall()
        out = []
        for r in rows:
            out.append(self._task_state(dict(r)))
        return out

    def _task_state(self, r: Dict[str, Any]) -> Dict[str, Any]:
        s = self.s
        now: datetime = r["db_now"]
        sched = int(r["schedule_seconds"] or 3600)
        if r["expected_duration_seconds"]:
            expected, expected_src = int(r["expected_duration_seconds"]), "configured"
        elif r["n_hist"] and int(r["n_hist"]) >= 3 and r["p90_ms"] is not None:
            expected, expected_src = max(1, int(math.ceil(float(r["p90_ms"]) / 1000.0))), "history"
        else:
            expected, expected_src = s.default_expected_duration_seconds, "default"
        if r["delay_tolerance_seconds"] is not None:
            tolerance, tol_src = int(r["delay_tolerance_seconds"]), "configured"
        else:
            tolerance, tol_src = max(s.delay_min_grace_seconds, int(sched * s.delay_tolerance_factor)), "default"
        active_since = r["active_since"] or now
        last_success = r["last_success_at"]
        reference = max(last_success, active_since) if last_success else active_since
        deadline = reference + timedelta(seconds=sched + expected + tolerance)
        next_run = (last_success or active_since) + timedelta(seconds=sched)

        # En curso: SOLO con la instalación en línea, y confirmada por su latido o recién
        # iniciada (el siguiente latido puede tardar hasta heartbeat_expected_seconds).
        # El último latido de una instalación muerta NO mantiene la tarea "en curso".
        running = False
        run_elapsed = None
        if r["run_id"] is not None and r["run_inst_online"]:
            recent = r["run_since"] is not None and (now - r["run_since"]).total_seconds() <= 2 * s.heartbeat_expected_seconds
            running = bool(r["run_confirmed"]) or bool(recent)
            if running and r["run_since"] is not None:
                run_elapsed = int((now - r["run_since"]).total_seconds())
        running_limit = int(max(expected * s.running_long_factor, expected + tolerance))
        running_long = running and run_elapsed is not None and run_elapsed > running_limit

        effective = bool(r["effective_active"])
        open_failures = list(r["open_failures"] or [])
        sync_failing = int(r["consecutive_failures"] or 0) > 0 and r["last_status"] in ("failed", "interrupted")
        failing = sync_failing or bool(open_failures)
        if sync_failing:
            current_error = r["current_error_code"]
        elif open_failures:
            current_error = open_failures[0].get("error_code")
        else:
            current_error = None
        delayed = effective and not running and now > deadline
        never_run = last_success is None and r["le_id"] is None
        if not effective:
            state = "disabled"
        elif running:
            state = "running"
        elif failing:
            state = "failing"
        elif delayed:
            state = "delayed"
        elif never_run:
            state = "never_run"
        else:
            state = "ok"
        return {
            "task_id": r["task_id"], "group_id": r["group_id"], "group_name": r["group_name"],
            "company_id": r["company_id"], "company_name": r["company_name"], "agency_id": r["agency_id"],
            "agency_name": r["agency_name"], "object_catalog_id": r["object_catalog_id"],
            "object_name": r["object_name"], "destination_table": r["destination_table"],
            "is_active": bool(r["is_active"]), "effective_active": effective, "state": state,
            "delayed": delayed, "failing": failing, "running": running, "running_long": running_long,
            "schedule_seconds": sched, "expected_duration_seconds": expected,
            "expected_duration_source": expected_src, "delay_tolerance_seconds": tolerance,
            "delay_tolerance_source": tol_src, "running_long_after_seconds": running_limit,
            "active_since": iso(r["active_since"]),
            "last_execution": None if r["le_id"] is None else {
                "execution_id": str(r["le_id"]), "status": r["le_status"], "started_at": iso(r["le_started"]),
                "finished_at": iso(r["le_finished"]), "error_code": r["le_error"], "rows_loaded": r["le_rows"],
                "installation_id": str(r["le_installation"]) if r["le_installation"] else None},
            "last_success_at": iso(last_success), "last_success_rows": r["last_success_rows"],
            "last_success_execution_id": str(r["last_success_execution_id"]) if r["last_success_execution_id"] else None,
            "watermark": r["watermark"].isoformat() if r["watermark"] else None,
            "watermark_kind": r["watermark_kind"],
            "current_error_code": current_error,
            "failing_installations": [
                {"incident_id": f.get("incident_id"), "installation_id": f.get("installation_id"),
                 "installation_name": f.get("installation_name"), "error_code": f.get("error_code"),
                 "acknowledged": bool(f.get("acknowledged")), "occurrences": f.get("occurrences"),
                 "last_seen_at": f.get("last_seen_at")} for f in open_failures],
            "last_status": r["last_status"], "consecutive_failures": int(r["consecutive_failures"] or 0),
            "last_failure_at": iso(r["last_failure_at"]),
            "running_execution": None if not running else {
                "execution_id": str(r["run_id"]), "installation_id": str(r["run_installation"]),
                "installation_name": r["run_installation_name"], "since": iso(r["run_since"]),
                "elapsed_seconds": run_elapsed, "confirmed_by_heartbeat": bool(r["run_confirmed"])},
            "next_expected_run_at": iso(next_run) if effective else None,
            "delay_deadline_at": iso(deadline) if effective else None,
            "installations_covering": int(r["installations_covering"] or 0),
            "_last_success_dt": last_success,
            "_run_installation": str(r["run_installation"]) if r["run_installation"] else None,
            "_run_id": str(r["run_id"]) if r["run_id"] else None,
        }

    def installation_health(self, cur: Any, *, installation_id: Optional[str] = None,
                            group_id: Optional[int] = None, company_id: Optional[int] = None,
                            agency_id: Optional[int] = None) -> List[Dict[str, Any]]:
        conds, params = [], []
        if installation_id:
            conds.append("i.id = %s")
            params.append(installation_id)
        for col, val in (("i.group_id", group_id), ("i.company_id", company_id), ("i.agency_id", agency_id)):
            if val is not None:
                conds.append(f"{col} = %s")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        cur.execute(
            f"""
            SELECT NOW() AS db_now, i.id, i.name, i.hostname, i.scope_type, i.group_id, g.name AS group_name,
                   i.company_id, c.name AS company_name, i.agency_id, a.name AS agency_name, i.status,
                   i.client_version, i.last_seen_at, i.last_heartbeat, i.created_at, i.revoked_at,
                   (g.is_enabled AND COALESCE(c.is_enabled, TRUE) AND COALESCE(a.is_enabled, TRUE)) AS scope_enabled,
                   (SELECT MAX(h.received_at) FROM installation_heartbeat h WHERE h.installation_id = i.id) AS last_heartbeat_at,
                   (SELECT MAX(COALESCE(e.finished_at, e.started_at, e.received_at)) FROM task_execution e
                     WHERE e.installation_id = i.id) AS last_execution_at,
                   (SELECT MAX(LEAST(COALESCE(e.finished_at, e.updated_at), e.updated_at)) FROM task_execution e
                     WHERE e.installation_id = i.id AND e.status = 'success') AS last_success_at,
                   (SELECT COUNT(*) FROM incident x WHERE x.installation_id = i.id AND x.status = 'open') AS open_incidents
            FROM installation i
            JOIN client_group g ON g.id = i.group_id
            LEFT JOIN company c ON c.id = i.company_id
            LEFT JOIN agency a ON a.id = i.agency_id
            {where}
            ORDER BY g.name, i.name
            """,
            tuple(params),
        )
        out = []
        for r in cur.fetchall():
            now = r["db_now"]
            since = (now - r["last_seen_at"]).total_seconds() if r["last_seen_at"] else None
            if r["status"] != "active":
                conn_state = "revoked"
            elif not r["scope_enabled"]:
                conn_state = "scope_disabled"
            elif since is None:
                conn_state = "never"
            elif since <= self.s.disconnect_after_seconds:
                conn_state = "online"
            else:
                conn_state = "offline"
            hb = r["last_heartbeat"] or {}
            out.append({
                "id": str(r["id"]), "name": r["name"], "hostname": r["hostname"], "scope_type": r["scope_type"],
                "group_id": r["group_id"], "group_name": r["group_name"], "company_id": r["company_id"],
                "company_name": r["company_name"], "agency_id": r["agency_id"], "agency_name": r["agency_name"],
                "status": r["status"], "connectivity": conn_state, "client_version": r["client_version"],
                "last_seen_at": iso(r["last_seen_at"]), "seconds_since_contact": None if since is None else int(since),
                "last_heartbeat_at": iso(r["last_heartbeat_at"]), "last_execution_at": iso(r["last_execution_at"]),
                "last_success_at": iso(r["last_success_at"]), "open_incidents": int(r["open_incidents"] or 0),
                "queue_depth": hb.get("queue_depth"), "dead_letter_total": hb.get("dead_letter_total"),
                "parked_total": hb.get("parked_total"), "queue_overflow_total": hb.get("queue_overflow_total"),
                "running": hb.get("running") or [], "uptime_seconds": hb.get("uptime_seconds"),
                "disconnect_after_seconds": self.s.disconnect_after_seconds,
            })
        return out

    # ── Evaluador periódico ─────────────────────────────────────────────────
    def evaluate(self) -> Dict[str, Any]:
        """Un ciclo. Devuelve {"ran": False} si otro proceso tiene el lock."""
        conn = self.get_connection()
        stats: Dict[str, Any] = {"ran": False, "opened": 0, "resolved": 0, "reminders": 0, "errors": 0,
                                 "failed_phases": []}
        try:
            conn.autocommit = True
            with conn.cursor() as c0:
                c0.execute("SELECT pg_try_advisory_lock(%s)", (_EVAL_LOCK_KEY,))
                got = c0.fetchone()[0]
            if not got:
                return stats
            conn.autocommit = False
            try:
                stats["ran"] = True
                # Cada fase aislada: si una falla (se revierte y se registra), las demás corren.
                for phase, fn in (("instalaciones", self._eval_installations), ("tareas", self._eval_tasks),
                                  ("relleno", self._eval_backfill), ("huérfanas", self._eval_orphans),
                                  ("recordatorios", self._eval_reminders)):
                    try:
                        fn(conn, stats)
                    except psycopg2.OperationalError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        stats["errors"] += 1
                        stats["failed_phases"].append(phase)
                        log.error("Evaluador: la fase %s falló (%s); se continúa con las demás.",
                                  phase, type(exc).__name__)
                self.maybe_housekeeping()
            finally:
                conn.rollback()
                conn.autocommit = True
                with conn.cursor() as c0:
                    c0.execute("SELECT pg_advisory_unlock(%s)", (_EVAL_LOCK_KEY,))
        finally:
            conn.close()
        return stats

    # ── Retención (la llaman el notificador y el evaluador; como máximo 1 vez/hora) ──
    def maybe_housekeeping(self, force: bool = False) -> Optional[Dict[str, int]]:
        now = time.monotonic()
        with self._hk_lock:
            if not force and self._last_housekeeping and now - self._last_housekeeping < 3600:
                return None
            self._last_housekeeping = now
        try:
            return self.housekeeping()
        except psycopg2.OperationalError:
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("Retención: error %s", type(exc).__name__)
            return None

    def housekeeping(self, batch: int = 5000, max_batches: int = 20) -> Dict[str, int]:
        out = {"outbox": 0, "executions": 0, "incident_events": 0}
        with self.tx() as cur:
            cur.execute("""DELETE FROM notification_outbox
                           WHERE status IN ('delivered', 'failed', 'skipped')
                             AND created_at < NOW() - make_interval(days => %s)""", (self.n.retention_days,))
            out["outbox"] = cur.rowcount
        if self.s.execution_retention_days > 0:
            for _ in range(max_batches):   # por lotes: sin transacciones ni bloqueos largos
                with self.tx() as cur:
                    cur.execute(
                        """DELETE FROM task_execution WHERE execution_id IN (
                               SELECT e.execution_id FROM task_execution e
                               WHERE e.received_at < NOW() - make_interval(days => %s)
                                 AND e.status <> 'running'
                                 -- nunca la última ejecución de la tarea
                                 AND EXISTS (SELECT 1 FROM task_execution n WHERE n.task_id = e.task_id
                                               AND COALESCE(n.started_at, n.received_at) > COALESCE(e.started_at, e.received_at))
                                 -- nunca la última carga exitosa de la tarea
                                 AND (e.status <> 'success' OR EXISTS (
                                        SELECT 1 FROM task_execution n WHERE n.task_id = e.task_id AND n.status = 'success'
                                          AND LEAST(COALESCE(n.finished_at, n.updated_at), n.updated_at)
                                              > LEAST(COALESCE(e.finished_at, e.updated_at), e.updated_at)))
                                 AND NOT EXISTS (SELECT 1 FROM task_sync_state s WHERE s.last_execution_id = e.execution_id)
                                 AND NOT EXISTS (SELECT 1 FROM incident x WHERE x.status = 'open'
                                                   AND e.execution_id IN (x.first_execution_id, x.last_execution_id))
                               LIMIT %s)""",
                        (self.s.execution_retention_days, batch))
                    n = cur.rowcount
                out["executions"] += n
                if n < batch:
                    break
        if self.s.incident_event_retention_days > 0:
            with self.tx() as cur:
                cur.execute(
                    """DELETE FROM incident_event ev USING incident x
                       WHERE ev.incident_id = x.id AND x.status = 'resolved'
                         AND x.resolved_at < NOW() - make_interval(days => %s)
                         AND ev.event_type NOT IN ('opened', 'acknowledged', 'resolved')""",
                    (self.s.incident_event_retention_days,))
                out["incident_events"] = cur.rowcount
        if any(out.values()):
            log.info("Retención: %s", out)
        return out

    @staticmethod
    def _count(stats: Dict[str, Any], action: str) -> None:
        if action == "opened":
            stats["opened"] += 1

    def _eval_installations(self, conn: Any, stats: Dict[str, Any]) -> None:
        thr = self.s.disconnect_after_seconds
        in_grace = time.monotonic() - self._born < self.s.effective_startup_grace
        stats["startup_grace"] = in_grace
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Candidatas a desconexión: se bloquean sus filas (un latido concurrente espera
            # y después resuelve) y se re-verifica last_seen_at con la versión más nueva.
            cur.execute(
                """SELECT i.id, i.name, i.group_id, i.company_id, i.agency_id, i.last_seen_at
                   FROM installation i
                   JOIN client_group g ON g.id = i.group_id
                   LEFT JOIN company c ON c.id = i.company_id
                   LEFT JOIN agency a ON a.id = i.agency_id
                   WHERE i.status = 'active' AND g.is_enabled AND COALESCE(c.is_enabled, TRUE)
                     AND COALESCE(a.is_enabled, TRUE)
                     AND (i.last_seen_at IS NULL OR i.last_seen_at < NOW() - make_interval(secs => %s))
                   FOR UPDATE OF i SKIP LOCKED""",
                (thr,),
            )
            # En la gracia de arranque no se abren (sí se resuelven) desconexiones.
            for r in ([] if in_grace else cur.fetchall()):
                with self.savepoint(cur, f"desconexión de la instalación {r['id']}", stats):
                    _, action = self.open_or_recur(
                        cur, category="disconnected", installation_id=str(r["id"]), installation_name=r["name"],
                        scope={"group_id": r["group_id"], "company_id": r["company_id"], "agency_id": r["agency_id"]},
                        title=f"Sin contacto con Nexus: {r['name']}",
                        message=f"Sin latidos ni llamadas desde {iso(r['last_seen_at']) or 'nunca'} "
                                f"(umbral {thr} s). Nexus lo detecta: el agente desconectado no puede avisar.",
                        details={"last_seen_at": iso(r["last_seen_at"]), "threshold_seconds": thr}, recur=False)
                    self._count(stats, action)
            conn.commit()
            # Resolución: contacto recuperado / revocada / alcance deshabilitado.
            cur.execute(
                """SELECT x.*, i.status AS inst_status, i.last_seen_at AS inst_last_seen,
                          (g.is_enabled AND COALESCE(c.is_enabled, TRUE) AND COALESCE(a.is_enabled, TRUE)) AS scope_enabled,
                          (i.last_seen_at >= NOW() - make_interval(secs => %s)) AS fresh
                   FROM incident x
                   JOIN installation i ON i.id = x.installation_id
                   JOIN client_group g ON g.id = i.group_id
                   LEFT JOIN company c ON c.id = i.company_id
                   LEFT JOIN agency a ON a.id = i.agency_id
                   WHERE x.status = 'open' AND x.category = 'disconnected'
                   FOR UPDATE OF x""",
                (thr,),
            )
            for r in cur.fetchall():
                reason = None
                if r["inst_status"] != "active":
                    reason = "installation_revoked"
                elif not r["scope_enabled"]:
                    reason = "scope_disabled"
                elif r["fresh"]:
                    reason = "contact_recovered"
                if reason:
                    with self.savepoint(cur, f"resolución de desconexión {r['id']}", stats):
                        if self.resolve(cur, r, reason):
                            stats["resolved"] += 1
            # Instalación revocada: sus demás incidencias se cierran con ese motivo.
            cur.execute(
                """SELECT x.* FROM incident x JOIN installation i ON i.id = x.installation_id
                   WHERE x.status = 'open' AND i.status <> 'active' FOR UPDATE OF x""")
            for r in cur.fetchall():
                with self.savepoint(cur, f"cierre por revocación {r['id']}", stats):
                    if self.resolve(cur, r, "installation_revoked"):
                        stats["resolved"] += 1
            conn.commit()

    def _eval_tasks(self, conn: Any, stats: Dict[str, Any]) -> None:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Desde cuándo está efectivamente activa cada tarea (gracia al habilitar).
            cur.execute(
                """INSERT INTO task_health_state (task_id, effective_active, active_since, evaluated_at)
                   SELECT t.id, (t.is_active AND a.is_enabled AND o.is_enabled AND c.is_enabled AND g.is_enabled),
                          NOW(), NOW()
                   FROM agency_task t JOIN agency a ON a.id = t.agency_id JOIN company c ON c.id = a.company_id
                   JOIN client_group g ON g.id = c.group_id JOIN object_catalog o ON o.id = t.object_catalog_id
                   ON CONFLICT (task_id) DO UPDATE SET
                       active_since = CASE WHEN EXCLUDED.effective_active AND NOT task_health_state.effective_active
                                           THEN NOW() ELSE task_health_state.active_since END,
                       effective_active = EXCLUDED.effective_active, evaluated_at = NOW()"""
            )
            conn.commit()
            health = self.task_health(cur)
            conn.commit()
            cur.execute("SELECT * FROM incident WHERE status = 'open' AND task_id IS NOT NULL")
            open_by_task: Dict[int, List[Dict[str, Any]]] = {}
            for r in cur.fetchall():
                open_by_task.setdefault(r["task_id"], []).append(r)
            conn.commit()
            for h in health:
                tid = h["task_id"]
                scope = {k: h[k] for k in ("group_id", "company_id", "agency_id", "object_catalog_id")}
                try:
                    if not h["effective_active"]:
                        for inc in open_by_task.get(tid, []):
                            if self.resolve(cur, inc, "task_disabled"):
                                stats["resolved"] += 1
                        conn.commit()
                        continue
                    # Retraso. Si la tarea ya tiene una falla abierta, la falla lo cubre: no se
                    # abre además un retraso (doble alerta). Un retraso ya abierto se conserva.
                    if h["delayed"]:
                        if h["failing_installations"]:
                            conn.commit()
                            action = "suppressed"
                        else:
                            _, action = self.open_or_recur(
                                cur, category="task_delayed", task_id=tid, scope=scope,
                                title=f"Tarea retrasada: {_task_label(h)}",
                                message=(f"Sin carga confirmada desde {h['last_success_at'] or 'nunca'}; se esperaba "
                                         f"antes de {h['delay_deadline_at']} (cada {h['schedule_seconds']} s + duración "
                                         f"esperada {h['expected_duration_seconds']} s + tolerancia "
                                         f"{h['delay_tolerance_seconds']} s)."
                                         + ("" if h["installations_covering"] else
                                            " Ninguna instalación activa cubre esta tarea.")),
                                details={"reference_success_at": h["last_success_at"],
                                         "deadline_at": h["delay_deadline_at"],
                                         "schedule_seconds": h["schedule_seconds"],
                                         "expected_duration_seconds": h["expected_duration_seconds"],
                                         "delay_tolerance_seconds": h["delay_tolerance_seconds"],
                                         "installations_covering": h["installations_covering"]},
                                recur=False)
                        self._count(stats, action)
                    else:
                        for inc in open_by_task.get(tid, []):
                            if inc["category"] != "task_delayed" or h["running"]:
                                continue
                            ref = _parse_iso((inc["details"] or {}).get("reference_success_at"))
                            cur_ok = h["_last_success_dt"]
                            new_load = cur_ok is not None and (ref is None or cur_ok > ref)
                            if self.resolve(cur, inc, "load_confirmed" if new_load else "thresholds_changed",
                                            execution_id=h["last_success_execution_id"] if new_load else None):
                                stats["resolved"] += 1
                    # Ejecución prolongada
                    if h["running_long"] and h["_run_installation"]:
                        re_ = h["running_execution"] or {}
                        _, action = self.open_or_recur(
                            cur, category="task_running_long", installation_id=h["_run_installation"],
                            installation_name=re_.get("installation_name"), task_id=tid, scope=scope,
                            title=f"Ejecución prolongada: {_task_label(h)}",
                            message=(f"La ejecución lleva {re_.get('elapsed_seconds')} s (límite "
                                     f"{h['running_long_after_seconds']} s; duración esperada "
                                     f"{h['expected_duration_seconds']} s)."),
                            execution_id=h["_run_id"], details={"execution_id": h["_run_id"]}, recur=False)
                        self._count(stats, action)
                    for inc in open_by_task.get(tid, []):
                        if inc["category"] == "task_running_long":
                            ex_id = (inc["details"] or {}).get("execution_id")
                            cur.execute("SELECT status FROM task_execution WHERE execution_id = %s", (ex_id,))
                            er = cur.fetchone()
                            if er is None or er["status"] != "running":
                                if self.resolve(cur, inc, "execution_finished", execution_id=ex_id):
                                    stats["resolved"] += 1
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    stats["errors"] += 1
                    log.error("Evaluador: error aislado en la tarea %s: %s", tid, type(exc).__name__)

    def _eval_backfill(self, conn: Any, stats: Dict[str, Any]) -> None:
        """
        Tareas que YA estaban fallando (p. ej. antes de aplicar la migración 005)
        y nunca tuvieron incidencia para su clave: se abre una con la última
        ejecución fallida como evidencia. No reabre claves con historial.
        """
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT s.task_id, s.installation_id, i.name AS installation_name, s.last_status,
                          e.execution_id, COALESCE(e.start_agent_seq, e.last_agent_seq) AS seq, e.error_code,
                          e.error_message_sanitized, e.failure_stage, a.company_id, c.group_id, t.agency_id,
                          t.object_catalog_id, o.destination_table, a.name AS agency_name
                   FROM task_sync_state s
                   JOIN task_execution e ON e.execution_id = s.last_execution_id
                   JOIN installation i ON i.id = s.installation_id AND i.status = 'active'
                   JOIN agency_task t ON t.id = s.task_id
                   JOIN agency a ON a.id = t.agency_id JOIN company c ON c.id = a.company_id
                   JOIN client_group g ON g.id = c.group_id JOIN object_catalog o ON o.id = t.object_catalog_id
                   WHERE s.last_status IN ('failed', 'interrupted') AND s.consecutive_failures > 0
                     AND e.status IN ('failed', 'interrupted')
                     AND t.is_active AND a.is_enabled AND o.is_enabled AND c.is_enabled AND g.is_enabled
                     AND NOT EXISTS (SELECT 1 FROM incident x
                                      WHERE x.dedup_key = 'task_failed|' || s.installation_id::text || '|' || s.task_id::text)""")
            for r in cur.fetchall():
                with self.savepoint(cur, f"relleno de la tarea {r['task_id']}", stats):
                    task = {"task_id": r["task_id"], "group_id": r["group_id"], "company_id": r["company_id"],
                            "agency_id": r["agency_id"], "object_catalog_id": r["object_catalog_id"]}
                    _, action = self.open_or_recur(
                        cur, category="task_failed", installation_id=str(r["installation_id"]),
                        installation_name=r["installation_name"], task_id=r["task_id"], scope=task,
                        title=f"Falla de tarea: {r['destination_table']} · {r['agency_name']}",
                        severity="error" if r["last_status"] == "failed" else "warning", error_code=r["error_code"],
                        message=r["error_message_sanitized"], execution_id=str(r["execution_id"]), evidence_seq=r["seq"],
                        details={"last_status": r["last_status"], "failure_stage": r["failure_stage"], "backfilled": True})
                    self._count(stats, action)
            conn.commit()

    def _eval_orphans(self, conn: Any, stats: Dict[str, Any]) -> None:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT x.* FROM incident x
                   WHERE x.status = 'open' AND x.task_id IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM agency_task t WHERE t.id = x.task_id)
                   FOR UPDATE OF x""")
            for r in cur.fetchall():
                with self.savepoint(cur, f"cierre por tarea eliminada {r['id']}", stats):
                    if self.resolve(cur, r, "task_deleted"):
                        stats["resolved"] += 1
            cur.execute(
                """SELECT x.* FROM incident x
                   WHERE x.status = 'open' AND x.installation_id IS NULL AND x.category <> 'task_delayed'
                   FOR UPDATE OF x""")
            for r in cur.fetchall():
                with self.savepoint(cur, f"cierre por instalación eliminada {r['id']}", stats):
                    if self.resolve(cur, r, "installation_deleted"):
                        stats["resolved"] += 1
            conn.commit()

    def _eval_reminders(self, conn: Any, stats: Dict[str, Any]) -> None:
        """Recordatorio opcional por canal para incidencias abiertas y NO reconocidas."""
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT x.id AS incident_id, ch.id AS channel_id, ch.reminder_interval_minutes AS mins
                   FROM incident x
                   JOIN notification_channel ch ON ch.is_enabled AND ch.reminder_interval_minutes > 0
                   WHERE x.status = 'open' AND x.acknowledged_at IS NULL
                     AND (ch.group_id IS NULL OR ch.group_id = x.group_id)
                     AND (cardinality(ch.categories) = 0 OR x.category = ANY(ch.categories))
                     AND array_position(ARRAY['info','warning','error','critical']::text[], x.severity)
                         >= array_position(ARRAY['info','warning','error','critical']::text[], ch.min_severity)
                     AND x.opened_at < NOW() - make_interval(mins => ch.reminder_interval_minutes)
                     AND NOT EXISTS (SELECT 1 FROM notification_outbox o
                                      WHERE o.channel_id = ch.id AND o.incident_id = x.id
                                        AND o.created_at >= NOW() - make_interval(mins => ch.reminder_interval_minutes))""")
            rows = cur.fetchall()
            for r in rows:
                cur.execute("SELECT * FROM incident WHERE id = %s", (r["incident_id"],))
                inc = cur.fetchone()
                bucket = int(time.time() // (60 * int(r["mins"])))
                n = self._insert_outbox(cur, r["channel_id"], inc, "reminder", f"incident-{inc['id']}-reminder-{bucket}")
                stats["reminders"] += n
            conn.commit()

    # ── Notificaciones ──────────────────────────────────────────────────────
    def _incident_view(self, cur: Any, incident_id: int) -> Dict[str, Any]:
        cur.execute(
            """SELECT x.*, g.name AS group_name, c.name AS company_name, a.name AS agency_name,
                      oc.name AS object_name
               FROM incident x
               LEFT JOIN client_group g ON g.id = x.group_id
               LEFT JOIN company c ON c.id = x.company_id
               LEFT JOIN agency a ON a.id = x.agency_id
               LEFT JOIN object_catalog oc ON oc.id = x.object_catalog_id
               WHERE x.id = %s""",
            (incident_id,),
        )
        return cur.fetchone()

    def build_payload(self, inc: Dict[str, Any], transition: str, delivery_id: str) -> Dict[str, Any]:
        """Payload del webhook: sin SQL, sin credenciales; el mensaje ya viene saneado."""
        return {
            "event": f"incident.{transition}",
            "delivery_id": delivery_id,
            "generated_at": iso(datetime.now(timezone.utc)),
            "incident": {
                "id": inc["id"], "category": inc["category"],
                "category_label": CATEGORY_DEFAULTS.get(inc["category"], {}).get("label"),
                "severity": inc["severity"], "status": inc["status"], "title": inc["title"],
                "installation": {"id": str(inc["installation_id"]) if inc.get("installation_id") else None,
                                 "name": inc.get("installation_name")},
                "scope": {"group": inc.get("group_name"), "company": inc.get("company_name"),
                          "agency": inc.get("agency_name"), "object": inc.get("object_name"),
                          "task_id": inc.get("task_id")},
                "opened_at": iso(inc["opened_at"]), "last_seen_at": iso(inc["last_seen_at"]),
                "occurrences": inc["occurrences"], "last_error_code": inc.get("last_error_code"),
                "message": redact_text(inc.get("last_message_sanitized"), max_len=500)
                if inc.get("last_message_sanitized") else None,
                "resolved_at": iso(inc.get("resolved_at")), "resolution_reason": inc.get("resolution_reason"),
                "duration_seconds": inc.get("duration_seconds"),
                "acknowledged": inc.get("acknowledged_at") is not None,
            },
        }

    def _insert_outbox(self, cur: Any, channel_id: int, inc: Dict[str, Any], transition: str, key: str) -> int:
        view = self._incident_view(cur, inc["id"])
        payload = self.build_payload(view, transition, key)
        cur.execute(
            """INSERT INTO notification_outbox (channel_id, incident_id, transition, idempotency_key, payload)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (channel_id, idempotency_key) DO NOTHING RETURNING id""",
            (channel_id, inc["id"], transition, key, json.dumps(payload, default=str)),
        )
        return 1 if cur.fetchone() else 0

    def _enqueue(self, cur: Any, inc: Dict[str, Any], transition: str) -> None:
        """Encola (misma transacción) una notificación por canal que aplique. Solo transiciones."""
        cur.execute("SELECT * FROM notification_channel WHERE is_enabled")
        for ch in cur.fetchall():
            if SEV_RANK.get(inc["severity"], 0) < SEV_RANK.get(ch["min_severity"], 0):
                continue
            if ch["group_id"] is not None and ch["group_id"] != inc.get("group_id"):
                continue
            if ch["categories"] and inc["category"] not in ch["categories"]:
                continue
            if transition == "opened" and not ch["notify_on_open"]:
                continue
            if transition == "resolved" and not ch["notify_on_resolve"]:
                continue
            self._insert_outbox(cur, ch["id"], inc, transition, f"incident-{inc['id']}-{transition}")

    def _channel_runtime(self, ch: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(ch)
        d["url"] = self.decrypt(ch.get("url_enc") or "") if ch.get("url_enc") else ""
        d["signing_secret"] = self.decrypt(ch.get("signing_secret_enc") or "") if ch.get("signing_secret_enc") else ""
        d["block_private"] = self.n.block_private_ips
        return d

    def backoff_seconds(self, attempts: int) -> int:
        return int(min(self.n.backoff_max_seconds, self.n.backoff_base_seconds * (2 ** max(0, attempts - 1))))

    def deliver_pending(self, limit: int = 20, only_id: Optional[int] = None) -> Dict[str, int]:
        """Reclama pendientes (SKIP LOCKED), envía fuera de la transacción y registra el resultado."""
        stats = {"delivered": 0, "retry": 0, "failed": 0, "skipped": 0}
        with self.tx() as cur:
            cur.execute(
                """UPDATE notification_outbox SET status = 'pending', updated_at = NOW()
                   WHERE status = 'sending' AND updated_at < NOW() - make_interval(secs => %s)""",
                (self.n.sending_stale_seconds,))
            extra = " AND o.id = %s" if only_id else " AND o.next_attempt_at <= NOW()"
            cur.execute(
                f"""SELECT o.id FROM notification_outbox o WHERE o.status = 'pending' {extra}
                    ORDER BY o.id LIMIT %s FOR UPDATE SKIP LOCKED""",
                ((only_id, limit) if only_id else (limit,)),
            )
            ids = [r["id"] for r in cur.fetchall()]
            if ids:
                cur.execute("""UPDATE notification_outbox SET status = 'sending', attempts = attempts + 1,
                                      updated_at = NOW() WHERE id = ANY(%s)""", (ids,))
        for oid in ids:
            try:
                self._deliver_one(oid, stats)
            except psycopg2.OperationalError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Una entrega problemática no aborta el lote (queda 'sending' y se
                # reintenta tras sending_stale_seconds).
                log.error("Notificador: error en la entrega %s: %s", oid, type(exc).__name__)
        self.maybe_housekeeping()
        return stats

    def _deliver_one(self, oid: int, stats: Dict[str, int]) -> str:
        with self.tx() as cur:
            cur.execute("SELECT * FROM notification_outbox WHERE id = %s", (oid,))
            o = cur.fetchone()
            ch = None
            if o is not None:
                cur.execute("SELECT * FROM notification_channel WHERE id = %s", (o["channel_id"],))
                ch = cur.fetchone()
        if o is None:
            # El canal (y con él su outbox, ON DELETE CASCADE) se borró a mitad del lote.
            stats["skipped"] += 1
            return "gone"
        if not ch or (not ch["is_enabled"] and o["transition"] != "test"):
            with self.tx() as cur:
                cur.execute("""UPDATE notification_outbox SET status = 'skipped', last_error = %s, updated_at = NOW()
                               WHERE id = %s""", ("Canal deshabilitado o eliminado", oid))
            stats["skipped"] += 1
            return "skipped"
        try:
            runtime = self._channel_runtime(ch)
            res = SENDERS[ch["kind"]].send(runtime, o["payload"], o["idempotency_key"])
        except Exception as exc:  # noqa: BLE001
            res = SendResult(False, None, type(exc).__name__)
        with self.tx() as cur:
            if res.ok:
                cur.execute("""UPDATE notification_outbox SET status = 'delivered', delivered_at = NOW(),
                                      last_status_code = %s, last_error = NULL, updated_at = NOW() WHERE id = %s""",
                            (res.status_code, oid))
                if o["incident_id"]:
                    cur.execute("""UPDATE incident SET last_notified_at = NOW(), notify_count = notify_count + 1
                                   WHERE id = %s""", (o["incident_id"],))
                    self._event(cur, o["incident_id"], "notified", message=f"Canal «{ch['name']}» ({o['transition']})",
                                data={"channel_id": ch["id"], "transition": o["transition"]})
                stats["delivered"] += 1
            elif o["attempts"] >= self.n.max_attempts or o["transition"] == "test":
                cur.execute("""UPDATE notification_outbox SET status = 'failed', last_status_code = %s,
                                      last_error = %s, updated_at = NOW() WHERE id = %s""",
                            (res.status_code, (res.error or "error")[:300], oid))
                if o["incident_id"]:
                    self._event(cur, o["incident_id"], "notification_failed",
                                message=f"Canal «{ch['name']}»: {res.error} tras {o['attempts']} intento(s)",
                                data={"channel_id": ch["id"], "transition": o["transition"]})
                stats["failed"] += 1
            else:
                cur.execute("""UPDATE notification_outbox SET status = 'pending', last_status_code = %s,
                                      last_error = %s, next_attempt_at = NOW() + make_interval(secs => %s),
                                      updated_at = NOW() WHERE id = %s""",
                            (res.status_code, (res.error or "error")[:300], self.backoff_seconds(o["attempts"]), oid))
                stats["retry"] += 1
        return "done"

    def send_test(self, channel_id: int) -> Dict[str, Any]:
        key = f"test-{uuid.uuid4()}"
        payload = {
            "event": "test", "delivery_id": key, "generated_at": iso(datetime.now(timezone.utc)),
            "incident": {"id": None, "category": "test", "severity": "info", "status": "open",
                         "title": "Prueba de notificación de Nexus DWH (sin incidencia real)"},
        }
        with self.tx() as cur:
            cur.execute("""INSERT INTO notification_outbox (channel_id, incident_id, transition, idempotency_key, payload)
                           VALUES (%s, NULL, 'test', %s, %s) RETURNING id""", (channel_id, key, json.dumps(payload)))
            oid = cur.fetchone()["id"]
        self.deliver_pending(only_id=oid)
        with self.tx() as cur:
            cur.execute("SELECT id, status, attempts, last_error, last_status_code FROM notification_outbox WHERE id = %s",
                        (oid,))
            return dict(cur.fetchone())

    # ── Hilos ───────────────────────────────────────────────────────────────
    def _loop(self, interval: int, fn: Callable[[], Any], name: str) -> None:
        while not self._stop.wait(interval):
            try:
                fn()
            except psycopg2.OperationalError:
                log.warning("%s: BD de configuración no disponible; se reintenta en %s s", name, interval)
            except Exception as exc:  # noqa: BLE001
                log.error("%s: error %s", name, type(exc).__name__)

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        if self.s.evaluator_interval_seconds > 0:
            t = threading.Thread(target=self._loop, name="health-evaluator", daemon=True,
                                 args=(self.s.evaluator_interval_seconds, self.evaluate, "Evaluador de salud"))
            t.start()
            self._threads.append(t)
        if self.n.worker_interval_seconds > 0:
            t = threading.Thread(target=self._loop, name="notifier", daemon=True,
                                 args=(self.n.worker_interval_seconds, self.deliver_pending, "Notificador"))
            t.start()
            self._threads.append(t)
        log.info("Salud: evaluador cada %s s (desconexión > %s s); notificador cada %s s",
                 self.s.evaluator_interval_seconds, self.s.disconnect_after_seconds, self.n.worker_interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(5)
        self._threads = []


# ─────────────────────────────────────────────────────────────────────────────
# API /admin (salud, incidencias, canales de notificación)
# ─────────────────────────────────────────────────────────────────────────────
class AckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: Optional[str] = Field(None, max_length=500)


class ManualResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=500)


class ChannelBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("categories", check_fields=False)
    @classmethod
    def _cats(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        bad = [c for c in v if c not in CATEGORIES]
        if bad:
            raise ValueError("Categorías no válidas: " + ", ".join(bad[:5]))
        return sorted(set(v))


class ChannelCreate(ChannelBase):
    name: str = Field(..., min_length=1, max_length=100)
    kind: str = Field(..., pattern="^(webhook|log)$")
    url: Optional[str] = Field(None, max_length=2000)
    signing_secret: Optional[str] = Field(None, max_length=500)
    is_enabled: bool = True
    min_severity: str = Field("warning", pattern="^(info|warning|error|critical)$")
    group_id: Optional[int] = Field(None, ge=1)
    categories: List[str] = Field(default_factory=list, max_length=20)
    notify_on_open: bool = True
    notify_on_resolve: bool = True
    reminder_interval_minutes: int = Field(0, ge=0, le=10080)
    timeout_seconds: int = Field(10, ge=1, le=60)
    verify_tls: bool = True


class ChannelUpdate(ChannelBase):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    url: Optional[str] = Field(None, max_length=2000)            # None = sin cambio
    signing_secret: Optional[str] = Field(None, max_length=500)  # None = sin cambio, "" = quitar
    is_enabled: Optional[bool] = None
    min_severity: Optional[str] = Field(None, pattern="^(info|warning|error|critical)$")
    group_id: Optional[int] = Field(None, ge=0)                  # 0 = todos los grupos
    categories: Optional[List[str]] = Field(None, max_length=20)
    notify_on_open: Optional[bool] = None
    notify_on_resolve: Optional[bool] = None
    reminder_interval_minutes: Optional[int] = Field(None, ge=0, le=10080)
    timeout_seconds: Optional[int] = Field(None, ge=1, le=60)
    verify_tls: Optional[bool] = None


def create_health_router(*, engine: IncidentEngine, admin_token: str) -> APIRouter:
    configured = (admin_token or "").strip()

    def require_admin(x_admin_token: Optional[str] = Header(None, alias="x-admin-token")) -> None:
        if not configured:
            raise HTTPException(status_code=503, detail="Admin no configurado.")
        provided = (x_admin_token or "").encode("utf-8")
        if not provided or not hmac.compare_digest(provided, configured.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Token de administrador no válido.")

    router = APIRouter(prefix="/admin", tags=["health"], dependencies=[Depends(require_admin)])

    @contextmanager
    def tx() -> Iterator[Any]:
        try:
            with engine.tx() as cur:
                yield cur
        except psycopg2.OperationalError:
            raise HTTPException(status_code=503, detail="No se pudo conectar a la BD de configuración.")

    def clean(h: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in h.items() if not k.startswith("_")}

    # ── Salud ───────────────────────────────────────────────────────────────
    @router.get("/health/settings")
    def health_settings() -> dict:
        return {"health": asdict(engine.s),
                "notifications": {k: v for k, v in asdict(engine.n).items()}}

    @router.post("/health/evaluate")
    def health_evaluate() -> dict:
        try:
            return engine.evaluate()
        except psycopg2.OperationalError:
            raise HTTPException(status_code=503, detail="No se pudo conectar a la BD de configuración.")

    @router.get("/health/tasks")
    def health_tasks(group_id: Optional[int] = Query(None), company_id: Optional[int] = Query(None),
                     agency_id: Optional[int] = Query(None), task_id: Optional[int] = Query(None),
                     state: Optional[str] = Query(None)) -> dict:
        with tx() as cur:
            rows = engine.task_health(cur, task_id=task_id, group_id=group_id, company_id=company_id,
                                      agency_id=agency_id)
            cur.execute("""SELECT task_id, id, category, severity, acknowledged_at IS NOT NULL AS acknowledged
                           FROM incident WHERE status = 'open' AND task_id IS NOT NULL""")
            incs: Dict[int, List[Dict[str, Any]]] = {}
            for r in cur.fetchall():
                incs.setdefault(r["task_id"], []).append({"id": r["id"], "category": r["category"],
                                                          "severity": r["severity"],
                                                          "acknowledged": r["acknowledged"]})
        items = []
        for h in rows:
            if state and h["state"] != state and not (state == "delayed" and h["delayed"]):
                continue
            d = clean(h)
            d["open_incidents"] = incs.get(h["task_id"], [])
            items.append(d)
        return {"items": items, "disconnect_after_seconds": engine.s.disconnect_after_seconds}

    @router.get("/health/installations")
    def health_installations(group_id: Optional[int] = Query(None), company_id: Optional[int] = Query(None),
                             agency_id: Optional[int] = Query(None), connectivity: Optional[str] = Query(None)) -> dict:
        with tx() as cur:
            rows = engine.installation_health(cur, group_id=group_id, company_id=company_id, agency_id=agency_id)
        if connectivity:
            rows = [r for r in rows if r["connectivity"] == connectivity]
        return {"items": rows, "disconnect_after_seconds": engine.s.disconnect_after_seconds}

    @router.get("/health/summary")
    def health_summary(group_id: Optional[int] = Query(None)) -> dict:
        with tx() as cur:
            insts = engine.installation_health(cur, group_id=group_id)
            tasks = engine.task_health(cur, group_id=group_id)
            conds, params = ["status = 'open'"], []
            if group_id is not None:
                conds.append("group_id = %s")
                params.append(group_id)
            cur.execute(f"""SELECT severity, (acknowledged_at IS NOT NULL) AS ack, COUNT(*) AS n FROM incident
                            WHERE {' AND '.join(conds)} GROUP BY 1, 2""", tuple(params))
            inc_rows = cur.fetchall()
            cur.execute(f"""SELECT COUNT(*) AS n FROM incident WHERE status = 'resolved'
                            AND resolved_at >= NOW() - INTERVAL '24 hours'
                            {'AND group_id = %s' if group_id is not None else ''}""", tuple(params))
            resolved_24h = int(cur.fetchone()["n"])
        conn_counts: Dict[str, int] = {}
        for i in insts:
            conn_counts[i["connectivity"]] = conn_counts.get(i["connectivity"], 0) + 1
        task_counts: Dict[str, int] = {}
        for t in tasks:
            task_counts[t["state"]] = task_counts.get(t["state"], 0) + 1
        by_sev = {s: 0 for s in SEVERITIES}
        unack = 0
        for r in inc_rows:
            by_sev[r["severity"]] += int(r["n"])
            if not r["ack"]:
                unack += int(r["n"])
        return {"installations": conn_counts, "tasks": task_counts,
                "incidents": {"open_by_severity": by_sev, "open_total": sum(by_sev.values()),
                              "open_unacknowledged": unack, "resolved_24h": resolved_24h},
                "disconnect_after_seconds": engine.s.disconnect_after_seconds}

    # ── Incidencias ─────────────────────────────────────────────────────────
    INC_SELECT = """
        SELECT x.*, g.name AS group_name, c.name AS company_name, a.name AS agency_name, oc.name AS object_name
        FROM incident x
        LEFT JOIN client_group g ON g.id = x.group_id
        LEFT JOIN company c ON c.id = x.company_id
        LEFT JOIN agency a ON a.id = x.agency_id
        LEFT JOIN object_catalog oc ON oc.id = x.object_catalog_id
    """

    def inc_out(r: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(r)
        for k in ("opened_at", "last_seen_at", "resolved_at", "acknowledged_at", "last_notified_at",
                  "created_at", "updated_at"):
            d[k] = iso(r.get(k))
        for k in ("installation_id", "first_execution_id", "last_execution_id", "resolved_execution_id"):
            d[k] = str(r[k]) if r.get(k) else None
        d.pop("dedup_key", None)
        d["category_label"] = CATEGORY_DEFAULTS.get(r["category"], {}).get("label", r["category"])
        d["resolution_label"] = RESOLUTION_LABELS.get(r.get("resolution_reason") or "", r.get("resolution_reason"))
        d["acknowledged"] = r.get("acknowledged_at") is not None
        d["manual_resolvable"] = r["category"] in MANUAL_RESOLVABLE
        d["duration_so_far_seconds"] = r.get("duration_seconds")
        return d

    @router.get("/incidents")
    def list_incidents(
        view: Optional[str] = Query(None, pattern="^(active|acknowledged|resolved|open)$"),
        status: Optional[str] = Query(None, pattern="^(open|resolved)$"),
        acknowledged: Optional[bool] = Query(None), category: Optional[str] = Query(None),
        severity: Optional[str] = Query(None), group_id: Optional[int] = Query(None),
        company_id: Optional[int] = Query(None), agency_id: Optional[int] = Query(None),
        task_id: Optional[int] = Query(None), installation_id: Optional[uuid.UUID] = Query(None),
        since: Optional[datetime] = Query(None), until: Optional[datetime] = Query(None),
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict:
        conds, params = [], []
        if view == "active":
            conds.append("x.status = 'open' AND x.acknowledged_at IS NULL")
        elif view == "acknowledged":
            conds.append("x.status = 'open' AND x.acknowledged_at IS NOT NULL")
        elif view == "resolved":
            conds.append("x.status = 'resolved'")
        elif view == "open":
            conds.append("x.status = 'open'")
        if status:
            conds.append("x.status = %s")
            params.append(status)
        if acknowledged is not None:
            conds.append("x.acknowledged_at IS " + ("NOT NULL" if acknowledged else "NULL"))
        if category in CATEGORIES:
            conds.append("x.category = %s")
            params.append(category)
        if severity in SEVERITIES:
            conds.append("x.severity = %s")
            params.append(severity)
        for col, val in (("x.group_id", group_id), ("x.company_id", company_id), ("x.agency_id", agency_id),
                         ("x.task_id", task_id)):
            if val is not None:
                conds.append(f"{col} = %s")
                params.append(val)
        if installation_id is not None:
            conds.append("x.installation_id = %s")
            params.append(str(installation_id))
        if since is not None:
            conds.append("x.opened_at >= %s")
            params.append(since)
        if until is not None:
            conds.append("x.opened_at <= %s")
            params.append(until)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with tx() as cur:
            cur.execute(
                INC_SELECT + where + """
                ORDER BY (x.status = 'open') DESC,
                         array_position(ARRAY['critical','error','warning','info']::text[], x.severity),
                         COALESCE(x.resolved_at, x.last_seen_at) DESC
                LIMIT %s""",
                (*params, limit),
            )
            rows = cur.fetchall()
        return {"total": len(rows), "items": [inc_out(r) for r in rows]}

    @router.get("/incidents/badge")
    def incidents_badge() -> dict:
        with tx() as cur:
            cur.execute(
                """SELECT COUNT(*) FILTER (WHERE acknowledged_at IS NULL) AS open_unacknowledged,
                          COUNT(*) AS open_total,
                          COUNT(*) FILTER (WHERE acknowledged_at IS NULL AND severity IN ('critical','error')) AS serious_unacknowledged
                   FROM incident WHERE status = 'open'""")
            r = cur.fetchone()
        return {k: int(v or 0) for k, v in r.items()}

    def get_incident_or_404(cur: Any, incident_id: int) -> Dict[str, Any]:
        cur.execute(INC_SELECT + " WHERE x.id = %s", (incident_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Incidencia no encontrada.")
        return r

    @router.get("/incidents/{incident_id}")
    def get_incident(incident_id: int) -> dict:
        with tx() as cur:
            r = get_incident_or_404(cur, incident_id)
            cur.execute("""SELECT id, event_type, actor, message, execution_id, data, created_at
                           FROM incident_event WHERE incident_id = %s ORDER BY id""", (incident_id,))
            events = [dict(e, created_at=iso(e["created_at"]),
                           execution_id=str(e["execution_id"]) if e["execution_id"] else None)
                      for e in cur.fetchall()]
            cur.execute("""SELECT o.id, o.channel_id, ch.name AS channel_name, o.transition, o.status, o.attempts,
                                  o.last_error, o.last_status_code, o.created_at, o.delivered_at, o.next_attempt_at
                           FROM notification_outbox o LEFT JOIN notification_channel ch ON ch.id = o.channel_id
                           WHERE o.incident_id = %s ORDER BY o.id""", (incident_id,))
            deliveries = [dict(d, created_at=iso(d["created_at"]), delivered_at=iso(d["delivered_at"]),
                               next_attempt_at=iso(d["next_attempt_at"])) for d in cur.fetchall()]
            executions: List[Dict[str, Any]] = []
            if r["task_id"] is not None:
                conds = ["e.task_id = %s", "e.received_at >= %s - INTERVAL '1 hour'"]
                params: List[Any] = [r["task_id"], r["opened_at"]]
                if r["installation_id"] is not None:
                    conds.append("e.installation_id = %s")
                    params.append(str(r["installation_id"]))
                if r["resolved_at"] is not None:
                    conds.append("e.received_at <= %s + INTERVAL '5 minutes'")
                    params.append(r["resolved_at"])
                cur.execute(
                    f"""SELECT e.execution_id, e.installation_id, i.name AS installation_name, e.status,
                               e.failure_stage, e.started_at, e.finished_at, e.duration_ms, e.rows_loaded,
                               e.error_code, e.error_message_sanitized, e.warnings
                        FROM task_execution e LEFT JOIN installation i ON i.id = e.installation_id
                        WHERE {' AND '.join(conds)}
                        ORDER BY COALESCE(e.started_at, e.received_at) DESC LIMIT 30""",
                    tuple(params),
                )
                for e in cur.fetchall():
                    d = dict(e)
                    d["execution_id"] = str(e["execution_id"])
                    d["installation_id"] = str(e["installation_id"]) if e["installation_id"] else None
                    d["started_at"] = iso(e["started_at"])
                    d["finished_at"] = iso(e["finished_at"])
                    executions.append(d)
            health = None
            if r["task_id"] is not None:
                th = engine.task_health(cur, task_id=r["task_id"])
                health = clean(th[0]) if th else None
            elif r["installation_id"] is not None:
                ih = engine.installation_health(cur, installation_id=str(r["installation_id"]))
                health = ih[0] if ih else None
        out = inc_out(r)
        out.update(events=events, deliveries=deliveries, executions=executions, current_health=health)
        return out

    @router.put("/incidents/{incident_id}/ack")
    def ack_incident(incident_id: int, body: Optional[AckBody] = None) -> dict:
        """Reconocer = alguien lo revisó. NUNCA cambia el estado técnico (abierta sigue abierta)."""
        comment = (body.comment if body else None) or None
        with tx() as cur:
            get_incident_or_404(cur, incident_id)
            cur.execute("""UPDATE incident SET acknowledged_at = NOW(), acknowledged_by = %s, ack_comment = %s,
                                  updated_at = NOW() WHERE id = %s""", ("admin", comment, incident_id))
            engine._event(cur, incident_id, "acknowledged", actor="admin", message=comment)
            r = get_incident_or_404(cur, incident_id)
        return inc_out(r)

    @router.put("/incidents/{incident_id}/resolve")
    def resolve_incident(incident_id: int, body: ManualResolveBody) -> dict:
        with tx() as cur:
            cur.execute("SELECT * FROM incident WHERE id = %s FOR UPDATE", (incident_id,))
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Incidencia no encontrada.")
            if r["status"] != "open":
                raise HTTPException(status_code=409, detail="La incidencia ya está resuelta.")
            if r["category"] not in MANUAL_RESOLVABLE:
                raise HTTPException(
                    status_code=409,
                    detail="Esta incidencia se resuelve sola cuando hay evidencia de recuperación "
                           "(latido, carga confirmada, fin de la ejecución…). Puede reconocerla, "
                           "pero no cerrarla manualmente.")
            engine.resolve(cur, r, "manual", actor="admin", comment=body.reason)
            r = get_incident_or_404(cur, incident_id)
        return inc_out(r)

    # ── Canales de notificación ─────────────────────────────────────────────
    def encrypt(value: str) -> str:
        cipher = engine.get_secret_cipher()
        if cipher is None:
            return value
        return "ENC:" + cipher.encrypt(value.encode("utf-8")).decode("utf-8")

    def ch_out(r: Dict[str, Any]) -> Dict[str, Any]:
        d = {k: v for k, v in dict(r).items() if k not in ("url_enc", "signing_secret_enc")}
        d["has_url"] = bool(r.get("url_enc"))
        d["has_secret"] = bool(r.get("signing_secret_enc"))
        d["encrypted"] = all((r.get(k) or "ENC:").startswith("ENC:") for k in ("url_enc", "signing_secret_enc"))
        for k in ("created_at", "updated_at", "last_delivery_at"):
            if k in d:
                d[k] = iso(r.get(k))
        return d

    CH_SELECT = """
        SELECT ch.*, g.name AS group_name,
               (SELECT COUNT(*) FROM notification_outbox o WHERE o.channel_id = ch.id AND o.status IN ('pending','sending')) AS pending,
               (SELECT COUNT(*) FROM notification_outbox o WHERE o.channel_id = ch.id AND o.status = 'failed') AS failed,
               (SELECT MAX(o.delivered_at) FROM notification_outbox o WHERE o.channel_id = ch.id) AS last_delivery_at
        FROM notification_channel ch LEFT JOIN client_group g ON g.id = ch.group_id
    """

    def get_channel_or_404(cur: Any, channel_id: int) -> Dict[str, Any]:
        cur.execute(CH_SELECT + " WHERE ch.id = %s", (channel_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Canal no encontrado.")
        return r

    def check_url(kind: str, url: Optional[str]) -> Optional[str]:
        if kind != "webhook":
            return None
        try:
            return validate_webhook_url(url or "", engine.n.allow_http, engine.n.block_private_ips)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"url: {exc}")

    @router.get("/notification-channels")
    def list_channels() -> dict:
        with tx() as cur:
            cur.execute(CH_SELECT + " ORDER BY ch.name")
            return {"items": [ch_out(r) for r in cur.fetchall()], "allow_http": engine.n.allow_http,
                    "categories": [{"value": c, "label": CATEGORY_DEFAULTS[c]["label"]} for c in CATEGORIES]}

    @router.post("/notification-channels", status_code=201)
    def create_channel(body: ChannelCreate) -> dict:
        url = check_url(body.kind, body.url)
        with tx() as cur:
            try:
                cur.execute(
                    """INSERT INTO notification_channel
                           (name, kind, url_enc, url_display, signing_secret_enc, is_enabled, min_severity, group_id,
                            categories, notify_on_open, notify_on_resolve, reminder_interval_minutes, timeout_seconds,
                            verify_tls)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (body.name.strip(), body.kind, encrypt(url) if url else None, mask_url(url) if url else "",
                     encrypt(body.signing_secret) if body.signing_secret else None, body.is_enabled,
                     body.min_severity, body.group_id, body.categories, body.notify_on_open,
                     body.notify_on_resolve, body.reminder_interval_minutes, body.timeout_seconds, body.verify_tls),
                )
            except psycopg2.errors.UniqueViolation:
                raise HTTPException(status_code=409, detail="Ya existe un canal con ese nombre.")
            except psycopg2.errors.ForeignKeyViolation:
                raise HTTPException(status_code=404, detail="Grupo no encontrado.")
            new_id = cur.fetchone()["id"]
            return ch_out(get_channel_or_404(cur, new_id))

    @router.put("/notification-channels/{channel_id}")
    def update_channel(channel_id: int, body: ChannelUpdate) -> dict:
        data = body.model_dump(exclude_unset=True)
        with tx() as cur:
            cur_row = get_channel_or_404(cur, channel_id)
            sets: Dict[str, Any] = {}
            for k in ("name", "is_enabled", "min_severity", "categories", "notify_on_open", "notify_on_resolve",
                      "reminder_interval_minutes", "timeout_seconds", "verify_tls"):
                if data.get(k) is not None:
                    sets[k] = data[k].strip() if k == "name" else data[k]
            if "group_id" in data and data["group_id"] is not None:
                sets["group_id"] = data["group_id"] or None
            if data.get("url") is not None:
                url = check_url(cur_row["kind"], data["url"])
                if url:
                    sets["url_enc"] = encrypt(url)
                    sets["url_display"] = mask_url(url)
            if "signing_secret" in data and data["signing_secret"] is not None:
                sets["signing_secret_enc"] = encrypt(data["signing_secret"]) if data["signing_secret"] else None
            if sets:
                cols = ", ".join(f"{k} = %s" for k in sets)  # claves de lista blanca
                try:
                    cur.execute(f"UPDATE notification_channel SET {cols}, updated_at = NOW() WHERE id = %s",
                                (*sets.values(), channel_id))
                except psycopg2.errors.UniqueViolation:
                    raise HTTPException(status_code=409, detail="Ya existe un canal con ese nombre.")
                except psycopg2.errors.ForeignKeyViolation:
                    raise HTTPException(status_code=404, detail="Grupo no encontrado.")
            return ch_out(get_channel_or_404(cur, channel_id))

    @router.delete("/notification-channels/{channel_id}")
    def delete_channel(channel_id: int) -> dict:
        with tx() as cur:
            cur.execute("DELETE FROM notification_channel WHERE id = %s", (channel_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Canal no encontrado.")
        return {"status": "ok", "deleted": channel_id}

    @router.post("/notification-channels/{channel_id}/test")
    def test_channel(channel_id: int) -> dict:
        with tx() as cur:
            get_channel_or_404(cur, channel_id)
        res = engine.send_test(channel_id)
        return {"status": res["status"], "attempts": res["attempts"], "error": res["last_error"],
                "http_status": res["last_status_code"]}

    @router.get("/notification-deliveries")
    def list_deliveries(channel_id: Optional[int] = Query(None), incident_id: Optional[int] = Query(None),
                        status: Optional[str] = Query(None, pattern="^(pending|sending|delivered|failed|skipped)$"),
                        limit: int = Query(100, ge=1, le=1000)) -> dict:
        conds, params = [], []
        for col, val in (("o.channel_id", channel_id), ("o.incident_id", incident_id), ("o.status", status)):
            if val is not None:
                conds.append(f"{col} = %s")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with tx() as cur:
            cur.execute(
                f"""SELECT o.id, o.channel_id, ch.name AS channel_name, o.incident_id, x.title AS incident_title,
                           x.category, o.transition, o.status, o.attempts, o.last_error, o.last_status_code,
                           o.created_at, o.delivered_at, o.next_attempt_at, o.idempotency_key
                    FROM notification_outbox o
                    LEFT JOIN notification_channel ch ON ch.id = o.channel_id
                    LEFT JOIN incident x ON x.id = o.incident_id
                    {where} ORDER BY o.id DESC LIMIT %s""",
                (*params, limit),
            )
            rows = cur.fetchall()
        return {"items": [dict(r, created_at=iso(r["created_at"]), delivered_at=iso(r["delivered_at"]),
                               next_attempt_at=iso(r["next_attempt_at"])) for r in rows]}

    return router
