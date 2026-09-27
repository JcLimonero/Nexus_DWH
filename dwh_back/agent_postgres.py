"""
agent_postgres.py — API del agente ETL por instalación (PostgreSQL)
───────────────────────────────────────────────────────────────────
Identidad por instalación:

  * ``POST /agent/enroll`` — la máquina se da de alta con un token de
    enrolamiento (x-group-token / x-agency-token / x-token; prioridad
    grupo > agencia > empresa) y recibe ``installation_id`` + ``secret``
    (se muestra UNA vez; en BD solo queda sha256(secret)).
  * El resto de ``/agent/*`` se autentica con ``x-installation-id`` +
    ``x-installation-secret`` (o ``Authorization: Bearer <id>.<secret>``).
    El alcance (grupo / empresa / agencia) sale de la instalación: el backend
    NUNCA confía en ids de grupo/empresa/agencia que mande el agente, y
    cualquier task_id que el agente referencie debe pertenecer a su alcance
    (si no, 403).

Endpoints del agente:
  GET  /agent/tasks                   tareas autorizadas (+ query_version, sync)
  POST /agent/executions              inicio de ejecución (idempotente)
  PUT  /agent/executions/{id}         avance / fin (idempotente, con agent_seq)
  POST /agent/heartbeat               latido (last_seen_at)
  POST /agent/events                  eventos genéricos (dedup por event_id)
  POST /agent/credentials/rotate      rotación del secreto (la pide el agente)
  GET  /agent/whoami                  identidad y alcance

Administración (x-admin-token):
  GET  /admin/installations[/{id}], POST /admin/installations/{id}/revoke,
  POST /admin/installations/{id}/rotate, GET /admin/executions,
  GET  /admin/sync-state, GET /admin/legacy-clients

Monitor (x-monitor-token):
  GET  /monitor/installations         instalaciones + clientes legados
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple

import base64

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from typing_extensions import Annotated

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception

from redact import redact_text, token_prefix

log = logging.getLogger("nexus.agent")

STATUSES = ("running", "success", "failed", "interrupted")
TERMINAL = ("success", "failed", "interrupted")
STAGES = ("config", "extract", "transform", "load", "report")
AGENT_EVENT_TYPES = ("queue_overflow", "agent_started", "agent_stopping", "config_stale",
                     "credential_rotated", "warning", "dead_letter")


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────
def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Watermark: TIMESTAMP sin zona (reloj del origen). Si llega con zona, a UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return dt.isoformat()


def _pending_cipher(previous_secret: str) -> "Fernet":
    """Clave derivada del secreto ANTERIOR: solo su poseedor recupera el nuevo."""
    if Fernet is None:
        raise RuntimeError("cryptography no disponible")
    key = hashlib.sha256(b"nexus-rotation-v1|" + previous_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_pending(new_secret_: str, previous_secret: str) -> Optional[str]:
    try:
        return _pending_cipher(previous_secret).encrypt(new_secret_.encode("utf-8")).decode("ascii")
    except Exception:
        return None


def decrypt_pending(token: Optional[str], previous_secret: str) -> Optional[str]:
    if not token:
        return None
    try:
        return _pending_cipher(previous_secret).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        return None


def http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# ─────────────────────────────────────────────────────────────────────────────
# Resolución de tokens legados (enrolamiento y endpoints legados)
# ─────────────────────────────────────────────────────────────────────────────
def resolve_legacy_principal(cur: Any, *, company_token: str = "", group_token: str = "",
                             agency_token: str = "") -> Optional[Dict[str, Any]]:
    """
    Resuelve un token legado con prioridad ÚNICA para todo el backend:
    grupo > agencia > empresa (igual que el cliente).
    Devuelve None si no hay token o no existe.
    """
    if group_token:
        cur.execute(
            """SELECT 'group' AS kind, g.id AS group_id, NULL::int AS company_id, NULL::int AS agency_id,
                      g.name AS group_name, ''::text AS company_name, ''::text AS agency_name,
                      g.is_enabled AS group_enabled, TRUE AS company_enabled, TRUE AS agency_enabled
               FROM client_group g WHERE g.group_token = %s""",
            (group_token,),
        )
        row = cur.fetchone()
        return dict(row, token=group_token) if row else None
    if agency_token:
        cur.execute(
            """SELECT 'agency' AS kind, g.id AS group_id, c.id AS company_id, a.id AS agency_id,
                      g.name AS group_name, c.name AS company_name, a.name AS agency_name,
                      g.is_enabled AS group_enabled, c.is_enabled AS company_enabled,
                      a.is_enabled AS agency_enabled
               FROM agency a JOIN company c ON c.id = a.company_id
               JOIN client_group g ON g.id = c.group_id
               WHERE a.agency_token = %s""",
            (agency_token,),
        )
        row = cur.fetchone()
        return dict(row, token=agency_token) if row else None
    if company_token:
        cur.execute(
            """SELECT 'company' AS kind, g.id AS group_id, c.id AS company_id, NULL::int AS agency_id,
                      g.name AS group_name, c.name AS company_name, ''::text AS agency_name,
                      g.is_enabled AS group_enabled, c.is_enabled AS company_enabled,
                      TRUE AS agency_enabled
               FROM company c JOIN client_group g ON g.id = c.group_id
               WHERE c.company_token = %s""",
            (company_token,),
        )
        row = cur.fetchone()
        return dict(row, token=company_token) if row else None
    return None


def scope_where(kind: str, group_id: int, company_id: Optional[int], agency_id: Optional[int]) -> Tuple[str, Tuple]:
    """
    Filtro SQL de alcance sobre los alias at/a/c/g. Para alcance empresa se
    respeta run_on_company_token (igual que /configs).
    """
    if kind == "agency":
        return "at.agency_id = %s AND a.company_id = %s AND c.group_id = %s", (agency_id, company_id, group_id)
    if kind == "company":
        return "a.company_id = %s AND c.group_id = %s AND at.run_on_company_token = TRUE", (company_id, group_id)
    return "c.group_id = %s", (group_id,)


# ─────────────────────────────────────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────────────────────────────────────
class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


BIGINT_MAX = 2**63 - 1
INT_MAX = 2**31 - 1
Short = Annotated[str, StringConstraints(max_length=200)]


class EnrollBody(_In):
    name: str = Field("", max_length=255)
    hostname: str = Field("", max_length=255)
    os_info: str = Field("", max_length=255)
    client_version: str = Field("", max_length=50)
    fingerprint: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("fingerprint")
    @classmethod
    def _fp_limits(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if len(v) > 20:
            raise ValueError("fingerprint admite como máximo 20 claves")
        out: Dict[str, str] = {}
        for k, val in v.items():
            if len(str(k)) > 40 or len(str(val)) > 200:
                raise ValueError("fingerprint: claves ≤ 40 y valores ≤ 200 caracteres")
            out[str(k)] = str(val)
        return out


class ExecutionStartBody(_In):
    event_id: Optional[uuid.UUID] = None
    execution_id: uuid.UUID
    task_id: int = Field(..., ge=1, le=INT_MAX)
    attempt: int = Field(1, ge=1, le=1000)
    query_version: Optional[int] = Field(None, ge=0, le=INT_MAX)
    started_at: datetime
    agent_seq: int = Field(..., ge=1, le=BIGINT_MAX)
    client_version: str = Field("", max_length=50)
    event_time: Optional[datetime] = None


class Checkpoint(_In):
    watermark: datetime
    kind: Literal["source_clock", "agent_local", "agent_utc"] = "source_clock"


class ExecutionUpdateBody(_In):
    event_id: Optional[uuid.UUID] = None
    task_id: int = Field(..., ge=1, le=INT_MAX)
    status: Literal["running", "success", "failed", "interrupted"]
    attempt: int = Field(1, ge=1, le=1000)
    query_version: Optional[int] = Field(None, ge=0, le=INT_MAX)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    failure_stage: Optional[Literal["config", "extract", "transform", "load", "report"]] = None
    rows_read: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    rows_loaded: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    rows_inserted: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    rows_updated: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    error_code: Optional[str] = Field(None, max_length=64)
    error_message: Optional[str] = Field(None, max_length=4000)
    warnings: List[Short] = Field(default_factory=list, max_length=20)
    checkpoint: Optional[Checkpoint] = None
    agent_seq: int = Field(..., ge=1, le=BIGINT_MAX)
    client_version: str = Field("", max_length=50)
    event_time: Optional[datetime] = None


class RunningItem(_In):
    task_id: Optional[int] = Field(None, ge=0, le=INT_MAX)
    execution_id: Optional[uuid.UUID] = None


class HeartbeatBody(_In):
    client_version: str = Field("", max_length=50)
    uptime_seconds: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    running: List[RunningItem] = Field(default_factory=list, max_length=50)
    queue_depth: Optional[int] = Field(None, ge=0, le=INT_MAX)
    queue_overflow_total: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    dead_letter_total: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    parked_total: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    config_age_seconds: Optional[int] = Field(None, ge=-1, le=BIGINT_MAX)
    agent_seq: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    event_time: Optional[datetime] = None


class AgentEventBody(_In):
    event_id: uuid.UUID
    event_type: str = Field(..., max_length=40)
    agent_seq: Optional[int] = Field(None, ge=0, le=BIGINT_MAX)
    event_time: Optional[datetime] = None
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _payload_limits(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if len(v) > 30:
            raise ValueError("payload admite como máximo 30 claves")
        if len(json.dumps(v, default=str)) > 4096:
            raise ValueError("payload demasiado grande (máx. 4 KB)")
        return v


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(None, max_length=255)


@dataclass
class InstallationCtx:
    id: uuid.UUID
    name: str
    scope_type: str
    group_id: int
    company_id: Optional[int]
    agency_id: Optional[int]
    group_name: str
    company_name: str
    agency_name: str
    rotation_required: bool
    used_previous_secret: bool
    presented_secret: str = ""   # secreto anterior presentado (solo en memoria)
    current_secret: str = ""     # secreto vigente presentado (solo en memoria, para cifrar el pendiente)


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────
def create_agent_routers(
    *,
    get_connection: Callable[[], Any],
    decrypt_config_secret: Callable[[Optional[str]], str],
    admin_token: str,
    monitor_token: str,
    config_max_age_seconds: int = 900,
    rotation_grace_seconds: int = 3600,
    heartbeat_retention_days: int = 7,
    download_log_retention_days: int = 30,
    future_tolerance_hours: int = 26,
    health: Optional[Any] = None,
) -> Tuple[APIRouter, APIRouter, APIRouter]:
    """
    Devuelve (agent_router, admin_router, monitor_router).
    ``health`` (IncidentEngine, opcional): ganchos de incidencias que corren en
    la MISMA transacción que el reporte (dentro de un savepoint: un error del
    gancho nunca tumba el reporte del agente).
    """

    # ── BD ──────────────────────────────────────────────────────────────────
    @contextmanager
    def tx() -> Iterator[Any]:
        try:
            conn = get_connection()
        except psycopg2.OperationalError:
            raise http_error(503, "config_db_unavailable", "BD de configuración no disponible.")
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    _last_cleanup = {"ts": 0.0}

    def maybe_cleanup(cur: Any) -> None:
        """Retención de latidos y auditoría de descargas (a lo sumo 1 vez/hora por proceso)."""
        now = time.time()
        if now - _last_cleanup["ts"] < 3600:
            return
        _last_cleanup["ts"] = now
        cur.execute("DELETE FROM installation_heartbeat WHERE received_at < NOW() - make_interval(days => %s)",
                    (heartbeat_retention_days,))
        cur.execute("DELETE FROM task_download_log WHERE downloaded_at < NOW() - make_interval(days => %s)",
                    (download_log_retention_days,))

    # ── Autenticación de instalación ────────────────────────────────────────
    def authenticate(
        request: Request,
        x_installation_id: Optional[str] = Header(None, alias="x-installation-id"),
        x_installation_secret: Optional[str] = Header(None, alias="x-installation-secret"),
        authorization: Optional[str] = Header(None),
    ) -> InstallationCtx:
        inst_id, secret = x_installation_id, x_installation_secret
        if (not inst_id or not secret) and authorization and authorization.lower().startswith("bearer "):
            raw = authorization[7:].strip()
            if "." in raw:
                inst_id, secret = raw.split(".", 1)
        if not inst_id or not secret:
            raise http_error(401, "missing_credentials", "Faltan credenciales de instalación.")
        try:
            iid = uuid.UUID(inst_id.strip())
        except ValueError:
            raise http_error(401, "invalid_credentials", "Credenciales de instalación no válidas.")
        request.state.audit = {"auth_kind": "installation", "installation_id": str(iid)}
        provided = hash_secret(secret.strip())
        with tx() as cur:
            cur.execute(
                """
                SELECT i.id, i.name, i.scope_type, i.group_id, i.company_id, i.agency_id, i.status,
                       i.credential_hash, i.previous_credential_hash, i.previous_valid_until,
                       i.rotation_required,
                       g.name AS group_name, g.is_enabled AS group_enabled,
                       COALESCE(c.name, '') AS company_name, COALESCE(c.is_enabled, TRUE) AS company_enabled,
                       COALESCE(a.name, '') AS agency_name, COALESCE(a.is_enabled, TRUE) AS agency_enabled
                FROM installation i
                JOIN client_group g ON g.id = i.group_id
                LEFT JOIN company c ON c.id = i.company_id
                LEFT JOIN agency a  ON a.id = i.agency_id
                WHERE i.id = %s
                """,
                (str(iid),),
            )
            row = cur.fetchone()
            # Comparación en tiempo constante incluso si no existe la fila.
            current = (row or {}).get("credential_hash") or ("0" * 64)
            ok = hmac.compare_digest(provided, current)
            used_prev = False
            if row and not ok and row.get("previous_credential_hash") and row.get("previous_valid_until") \
                    and row["previous_valid_until"] > utcnow():
                used_prev = hmac.compare_digest(provided, row["previous_credential_hash"])
            if not row or not (ok or used_prev):
                raise http_error(401, "invalid_credentials", "Credenciales de instalación no válidas.")
            request.state.audit.update(
                group_id=row["group_id"], company_id=row["company_id"], agency_id=row["agency_id"],
                group_name=row["group_name"], company_name=row["company_name"],
            )
            if row["status"] != "active":
                raise http_error(401, "installation_revoked", "La instalación fue revocada. Debe re-enrolarse.")
            if not row["group_enabled"] or not row["company_enabled"] or not row["agency_enabled"]:
                raise http_error(403, "scope_disabled", "El grupo, empresa o agencia de la instalación está deshabilitado.")
            ip = request.client.host if request.client else ""
            if ok and row.get("previous_credential_hash"):
                # El agente ya usa el secreto nuevo: se invalida el anterior y el pendiente.
                cur.execute(
                    "UPDATE installation SET previous_credential_hash = NULL, previous_valid_until = NULL, "
                    "pending_secret_enc = NULL, last_seen_at = NOW(), last_ip = %s WHERE id = %s",
                    (ip, str(iid)),
                )
            else:
                cur.execute("UPDATE installation SET last_seen_at = NOW(), last_ip = %s WHERE id = %s", (ip, str(iid)))
        return InstallationCtx(
            id=iid, name=row["name"], scope_type=row["scope_type"], group_id=row["group_id"],
            company_id=row["company_id"], agency_id=row["agency_id"], group_name=row["group_name"],
            company_name=row["company_name"], agency_name=row["agency_name"],
            rotation_required=bool(row["rotation_required"]) or used_prev,
            used_previous_secret=used_prev,
            presented_secret=secret.strip() if used_prev else "",
            current_secret=secret.strip() if ok else "",
        )

    def task_in_scope(cur: Any, ctx: InstallationCtx, task_id: int) -> Dict[str, Any]:
        where, params = scope_where(ctx.scope_type, ctx.group_id, ctx.company_id, ctx.agency_id)
        cur.execute(
            f"""
            SELECT at.id AS task_id, at.agency_id, a.company_id, c.group_id, at.object_catalog_id,
                   g.name AS group_name, c.name AS company_name, a.name AS agency_name,
                   oc.destination_table
            FROM agency_task at
            JOIN agency a ON a.id = at.agency_id
            JOIN company c ON c.id = a.company_id
            JOIN client_group g ON g.id = c.group_id
            JOIN object_catalog oc ON oc.id = at.object_catalog_id
            WHERE at.id = %s AND {where}
            """,
            (task_id, *params),
        )
        row = cur.fetchone()
        if not row:
            raise http_error(403, "task_out_of_scope", "La tarea no pertenece al alcance de la instalación.")
        return row

    def scope_secrets(cur: Any, task: Dict[str, Any]) -> List[str]:
        """Valores sensibles del alcance de una tarea (para sanear mensajes)."""
        cur.execute(
            """SELECT c.source_host, c.source_database, c.source_username, c.source_password, c.source_dsn,
                      g.warehouse_host, g.warehouse_database, g.warehouse_username, g.warehouse_password
               FROM company c JOIN client_group g ON g.id = c.group_id WHERE c.id = %s""",
            (task["company_id"],),
        )
        row = cur.fetchone() or {}
        out: List[str] = []
        for v in row.values():
            try:
                val = decrypt_config_secret(v or "")
            except Exception:
                val = ""
            if val:
                out.append(val)
        return out

    agent = APIRouter(prefix="/agent", tags=["agent"])

    # ── Enrolamiento ────────────────────────────────────────────────────────
    @agent.post("/enroll", status_code=201)
    def enroll(
        body: EnrollBody,
        request: Request,
        x_token: Optional[str] = Header(None),
        x_group_token: Optional[str] = Header(None),
        x_agency_token: Optional[str] = Header(None, alias="x-agency-token"),
    ) -> dict:
        if not (x_token or x_group_token or x_agency_token):
            raise http_error(401, "missing_enrollment_token", "Falta el token de enrolamiento.")
        with tx() as cur:
            p = resolve_legacy_principal(cur, company_token=x_token or "", group_token=x_group_token or "",
                                         agency_token=x_agency_token or "")
            request.state.audit = {"auth_kind": "enroll"}
            if not p:
                raise http_error(401, "invalid_enrollment_token", "Token de enrolamiento no válido.")
            request.state.audit.update(group_id=p["group_id"], company_id=p["company_id"],
                                       agency_id=p["agency_id"], group_name=p["group_name"],
                                       company_name=p["company_name"], token_prefix=token_prefix(p["token"]))
            if not (p["group_enabled"] and p["company_enabled"] and p["agency_enabled"]):
                raise http_error(403, "scope_disabled", "El grupo, empresa o agencia está deshabilitado.")
            iid = uuid.uuid4()
            secret = new_secret()
            fp = {k: str(v)[:200] for k, v in list((body.fingerprint or {}).items())[:20]}
            name = body.name.strip() or body.hostname.strip() or f"instalacion-{str(iid)[:8]}"
            cur.execute(
                """
                INSERT INTO installation
                    (id, name, hostname, os_info, fingerprint, scope_type, group_id, company_id, agency_id,
                     credential_hash, credential_rotated_at, enrolled_via, enrollment_token_prefix,
                     client_version, last_seen_at, last_ip)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, NOW(), %s)
                """,
                (str(iid), name[:255], body.hostname[:255], body.os_info[:255], json.dumps(fp),
                 p["kind"], p["group_id"], p["company_id"], p["agency_id"], hash_secret(secret),
                 p["kind"], token_prefix(p["token"]), body.client_version[:50],
                 request.client.host if request.client else ""),
            )
            request.state.audit["installation_id"] = str(iid)
        log.info("Instalación enrolada %s (alcance %s)", iid, p["kind"])
        return {
            "installation_id": str(iid),
            "secret": secret,
            "scope": {"type": p["kind"], "group": p["group_name"], "company": p["company_name"],
                      "agency": p["agency_name"]},
            "message": "Guarde el secreto de forma segura: no se volverá a mostrar.",
        }

    @agent.get("/whoami")
    def whoami(ctx: InstallationCtx = Depends(authenticate)) -> dict:
        return {
            "installation_id": str(ctx.id), "name": ctx.name,
            "scope": {"type": ctx.scope_type, "group": ctx.group_name, "company": ctx.company_name,
                      "agency": ctx.agency_name},
            "credential_rotation_required": ctx.rotation_required,
        }

    # ── Tareas ──────────────────────────────────────────────────────────────
    @agent.get("/tasks")
    def get_tasks(request: Request, ctx: InstallationCtx = Depends(authenticate)) -> dict:
        where, params = scope_where(ctx.scope_type, ctx.group_id, ctx.company_id, ctx.agency_id)
        with tx() as cur:
            cur.execute(
                """SELECT warehouse_host, warehouse_port, warehouse_database, warehouse_username, warehouse_password
                   FROM client_group WHERE id = %s""",
                (ctx.group_id,),
            )
            g = cur.fetchone()
            cur.execute(
                f"""
                SELECT at.id AS task_id, at.agency_id, a.company_id, c.group_id, at.object_catalog_id,
                       g.name AS group_name, c.name AS company_name, a.name AS agency_name,
                       c.verbose_logging, c.refresh_seconds,
                       c.source_type, c.source_dsn, c.source_host, c.source_port, c.source_database,
                       c.source_username, c.source_password,
                       oc.destination_table, oc.upsert_keys, oc.create_table_sql, oc.constraint_name,
                       oc.create_constraint_sql, oc.static_columns,
                       at.extract_sql, at.schedule_seconds, at.query_version, at.query_hash,
                       at.run_on_company_token, at.last_run_at,
                       s.watermark, s.watermark_kind, s.last_success_at, s.last_execution_id,
                       s.consecutive_failures, s.last_status, s.watermark_reset_at
                FROM agency_task at
                JOIN agency a ON a.id = at.agency_id
                JOIN object_catalog oc ON oc.id = at.object_catalog_id
                JOIN company c ON c.id = a.company_id
                JOIN client_group g ON g.id = c.group_id
                LEFT JOIN task_sync_state s ON s.task_id = at.id
                WHERE {where}
                  AND at.is_active AND a.is_enabled AND oc.is_enabled AND c.is_enabled AND g.is_enabled
                ORDER BY c.id, at.id
                """,
                params,
            )
            rows = cur.fetchall()
            ip = request.client.host if request.client else ""
            if rows:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO task_download_log (installation_id, task_id, query_version, client_ip) VALUES %s",
                    [(str(ctx.id), r["task_id"], r["query_version"], ip) for r in rows],
                )
        tasks = []
        refresh = []
        verbose = False
        for r in rows:
            verbose = verbose or bool(r["verbose_logging"])
            if r["refresh_seconds"]:
                refresh.append(int(r["refresh_seconds"]))
            # Compatibilidad: si aún no hay sync_state, se parte del last_run_at legado.
            wm, wm_kind = r["watermark"], r["watermark_kind"]
            if wm is None and r["last_run_at"] is not None:
                wm, wm_kind = r["last_run_at"], "legacy_last_run"
            tasks.append({
                "task_id": r["task_id"],
                "name": f"{r['group_name']} | {r['company_name']} | {r['agency_name']}",
                "group_id": r["group_id"], "company_id": r["company_id"], "agency_id": r["agency_id"],
                "object_catalog_id": r["object_catalog_id"],
                "load_table": r["destination_table"],
                "upsert_keys": [k.strip() for k in (r["upsert_keys"] or "").split(",") if k.strip()],
                "create_table_sql": r["create_table_sql"] or None,
                "constraint_name": r["constraint_name"] or None,
                "create_constraint_sql": r["create_constraint_sql"] or None,
                "static_columns": r["static_columns"] or None,
                "extract_sql": r["extract_sql"],
                "schedule_seconds": r["schedule_seconds"],
                "query_version": r["query_version"],
                "query_hash": (r["query_hash"] or "").strip(),
                "source": {
                    "type": (r["source_type"] or "sqlserver").lower(),
                    "host": decrypt_config_secret(r["source_host"] or ""),
                    "port": r["source_port"],
                    "database": decrypt_config_secret(r["source_database"] or ""),
                    "username": decrypt_config_secret(r["source_username"] or ""),
                    "password": decrypt_config_secret(r["source_password"] or ""),
                    "dsn": decrypt_config_secret(r["source_dsn"] or ""),
                },
                "sync": {
                    "watermark": wm.isoformat() if wm else None,
                    "watermark_kind": wm_kind,
                    "last_success_at": iso(r["last_success_at"]),
                    "last_execution_id": str(r["last_execution_id"]) if r["last_execution_id"] else None,
                    "consecutive_failures": r["consecutive_failures"] or 0,
                    "last_status": r["last_status"],
                    "watermark_reset_at": iso(r["watermark_reset_at"]),
                },
            })
        return {
            "installation_id": str(ctx.id),
            "scope": {"type": ctx.scope_type, "group": ctx.group_name, "company": ctx.company_name,
                      "agency": ctx.agency_name},
            "issued_at": iso(utcnow()),
            "config_max_age_seconds": config_max_age_seconds,
            "refresh_seconds": min(refresh) if refresh else 60,
            "log_verbose": verbose,
            "credential_rotation_required": ctx.rotation_required,
            "warehouse": {
                "host": decrypt_config_secret((g or {}).get("warehouse_host") or ""),
                "port": (g or {}).get("warehouse_port") or 5432,
                "database": decrypt_config_secret((g or {}).get("warehouse_database") or ""),
                "username": decrypt_config_secret((g or {}).get("warehouse_username") or ""),
                "password": decrypt_config_secret((g or {}).get("warehouse_password") or ""),
            },
            "tasks": tasks,
        }

    # ── Ejecuciones ─────────────────────────────────────────────────────────
    def bump_seq(cur: Any, ctx: InstallationCtx, seq: Optional[int]) -> None:
        if seq:
            cur.execute("UPDATE installation SET last_agent_seq = GREATEST(last_agent_seq, %s) WHERE id = %s",
                        (seq, str(ctx.id)))

    @agent.post("/executions")
    def start_execution(body: ExecutionStartBody, ctx: InstallationCtx = Depends(authenticate)) -> dict:
        with tx() as cur:
            t = task_in_scope(cur, ctx, body.task_id)
            if to_utc(body.started_at) > utcnow() + timedelta(hours=future_tolerance_hours):
                raise http_error(422, "invalid_time", "started_at está en el futuro.")
            cur.execute(
                """
                INSERT INTO task_execution
                    (execution_id, installation_id, task_id, group_id, company_id, agency_id, object_catalog_id,
                     client_version, query_version, attempt, status, started_at, event_time,
                     start_agent_seq, last_agent_seq)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running', %s, %s, %s, %s)
                ON CONFLICT (execution_id) DO NOTHING
                RETURNING execution_id
                """,
                (str(body.execution_id), str(ctx.id), t["task_id"], t["group_id"], t["company_id"],
                 t["agency_id"], t["object_catalog_id"], body.client_version, body.query_version,
                 body.attempt, to_utc(body.started_at), to_utc(body.event_time), body.agent_seq, body.agent_seq),
            )
            created = cur.fetchone() is not None
            if not created:
                cur.execute("SELECT installation_id, task_id FROM task_execution WHERE execution_id = %s",
                            (str(body.execution_id),))
                ex = cur.fetchone()
                if str(ex["installation_id"]) != str(ctx.id) or ex["task_id"] != body.task_id:
                    raise http_error(409, "execution_conflict", "execution_id ya usado por otra instalación o tarea.")
            bump_seq(cur, ctx, body.agent_seq)
        return {"status": "ok", "execution_id": str(body.execution_id), "duplicate": not created}

    def apply_sync_state(cur: Any, ctx: InstallationCtx, ex: Dict[str, Any], body: ExecutionUpdateBody,
                         error_code: Optional[str], cp: Optional[Checkpoint]) -> bool:
        """Actualiza task_sync_state respetando el orden (agent_seq / started_at), no el reloj de llegada."""
        task_id = ex["task_id"]
        cur.execute("INSERT INTO task_sync_state (task_id) VALUES (%s) ON CONFLICT DO NOTHING", (task_id,))
        cur.execute("SELECT * FROM task_sync_state WHERE task_id = %s FOR UPDATE", (task_id,))
        s = cur.fetchone()
        ex_seq = ex.get("start_agent_seq") or ex.get("last_agent_seq") or body.agent_seq
        ex_started = ex.get("started_at")
        if s["last_event_started_at"] is None and s["last_event_seq"] is None:
            newer = True
        elif s["installation_id"] is not None and str(s["installation_id"]) == str(ctx.id) and s["last_event_seq"] is not None:
            newer = ex_seq > s["last_event_seq"]
        else:
            newer = ex_started is not None and (s["last_event_started_at"] is None or ex_started > s["last_event_started_at"])

        changed = False
        # El watermark solo avanza (GREATEST) y solo con datos ya confirmados en el DWH.
        if body.status == "success" and cp is not None:
            reset_at = s["watermark_reset_at"]
            if reset_at is None or (ex_started is not None and ex_started >= reset_at):
                wm = naive(cp.watermark)
                if s["watermark"] is None or wm > s["watermark"]:
                    cur.execute(
                        "UPDATE task_sync_state SET watermark = %s, watermark_kind = %s, updated_at = NOW() WHERE task_id = %s",
                        (wm, cp.kind, task_id),
                    )
                    # Compatibilidad con agentes legados y el panel: last_run_at = watermark.
                    cur.execute("UPDATE agency_task SET last_run_at = %s WHERE id = %s", (wm, task_id))
                    changed = True
        if newer:
            if body.status == "success":
                cur.execute(
                    """UPDATE task_sync_state SET last_success_at = %s, last_execution_id = %s, last_status = 'success',
                              consecutive_failures = 0, current_error_code = NULL, installation_id = %s,
                              last_event_seq = %s, last_event_started_at = %s, updated_at = NOW()
                       WHERE task_id = %s""",
                    (ex.get("finished_at") or utcnow(), str(ex["execution_id"]), str(ctx.id), ex_seq, ex_started, task_id),
                )
            else:
                cur.execute(
                    """UPDATE task_sync_state SET last_failure_at = %s, last_execution_id = %s, last_status = %s,
                              consecutive_failures = consecutive_failures + 1, current_error_code = %s,
                              installation_id = %s, last_event_seq = %s, last_event_started_at = %s, updated_at = NOW()
                       WHERE task_id = %s""",
                    (ex.get("finished_at") or utcnow(), str(ex["execution_id"]), body.status, error_code,
                     str(ctx.id), ex_seq, ex_started, task_id),
                )
            changed = True
        return changed

    @agent.put("/executions/{execution_id}")
    def update_execution(execution_id: uuid.UUID, body: ExecutionUpdateBody,
                         ctx: InstallationCtx = Depends(authenticate)) -> dict:
        with tx() as cur:
            t = task_in_scope(cur, ctx, body.task_id)
            bump_seq(cur, ctx, body.agent_seq)
            cur.execute("SELECT * FROM task_execution WHERE execution_id = %s FOR UPDATE", (str(execution_id),))
            ex = cur.fetchone()
            if ex is None:
                # El inicio se perdió (p. ej. compactado en la cola): se crea con lo que llegue.
                cur.execute(
                    """
                    INSERT INTO task_execution
                        (execution_id, installation_id, task_id, group_id, company_id, agency_id, object_catalog_id,
                         client_version, query_version, attempt, status, started_at, last_agent_seq)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running', %s, 0)
                    RETURNING *
                    """,
                    (str(execution_id), str(ctx.id), t["task_id"], t["group_id"], t["company_id"], t["agency_id"],
                     t["object_catalog_id"], body.client_version, body.query_version, body.attempt,
                     to_utc(body.started_at)),
                )
                ex = cur.fetchone()
            if str(ex["installation_id"]) != str(ctx.id) or ex["task_id"] != body.task_id:
                raise http_error(409, "execution_conflict", "execution_id pertenece a otra instalación o tarea.")
            if body.agent_seq <= (ex["last_agent_seq"] or 0):
                return {"status": "ignored", "reason": "stale_or_duplicate", "execution_id": str(execution_id)}
            if ex["status"] in TERMINAL:
                # Un estado terminal no se sobrescribe (ni por reintentos ni por eventos viejos).
                cur.execute("UPDATE task_execution SET last_agent_seq = %s WHERE execution_id = %s",
                            (body.agent_seq, str(execution_id)))
                return {"status": "ignored", "reason": "terminal_state", "execution_id": str(execution_id)}

            # Relojes del agente absurdos → 422 (el agente lo aparta de su cola con aviso).
            limit = utcnow() + timedelta(hours=future_tolerance_hours)
            for label, val in (("started_at", body.started_at), ("finished_at", body.finished_at)):
                if val is not None and to_utc(val) > limit:
                    raise http_error(422, "invalid_time", f"{label} está en el futuro.")
            extra_warnings: List[str] = []
            cp = body.checkpoint if body.status == "success" else None
            if cp is not None:
                wm = naive(cp.watermark)
                # El watermark está en hora local del origen (sin zona): tolerancia amplia por husos.
                if wm > limit.replace(tzinfo=None) or wm.year < 1900:
                    extra_warnings.append("CHECKPOINT_REJECTED_OUT_OF_RANGE")
                    cp = None
                else:
                    cur.execute("SELECT watermark_kind FROM task_sync_state WHERE task_id = %s", (t["task_id"],))
                    srow = cur.fetchone()
                    stored_kind = (srow or {}).get("watermark_kind")
                    if stored_kind and stored_kind != "legacy_last_run" and stored_kind != cp.kind:
                        # No se mezclan relojes: un watermark de otro reloj podría saltarse filas.
                        extra_warnings.append("CHECKPOINT_KIND_MISMATCH")
                        cp = None
            secrets_ = scope_secrets(cur, t) if body.error_message else []
            err_msg = redact_text(body.error_message, secrets_, max_len=1000) if body.error_message else None
            err_code = (body.error_code or ("UNKNOWN" if body.status in ("failed", "interrupted") else None))
            warnings = [redact_text(w, secrets_, max_len=200) for w in body.warnings][:20] + extra_warnings
            cur.execute(
                """
                UPDATE task_execution SET
                    status = %s, failure_stage = %s, finished_at = %s, duration_ms = %s,
                    rows_read = %s, rows_loaded = %s, rows_inserted = %s, rows_updated = %s,
                    error_code = %s, error_message_sanitized = %s, warnings = %s,
                    checkpoint_confirmed = %s, checkpoint_kind = %s,
                    event_time = COALESCE(%s, event_time),
                    started_at = COALESCE(started_at, %s),
                    query_version = COALESCE(query_version, %s),
                    client_version = CASE WHEN client_version = '' THEN %s ELSE client_version END,
                    last_agent_seq = %s, updated_at = NOW()
                WHERE execution_id = %s
                RETURNING *
                """,
                (body.status, body.failure_stage if body.status != "success" else None,
                 to_utc(body.finished_at), body.duration_ms, body.rows_read, body.rows_loaded,
                 body.rows_inserted, body.rows_updated, err_code, err_msg, json.dumps(warnings),
                 naive(cp.watermark) if cp else None, cp.kind[:16] if cp else None,
                 to_utc(body.event_time), to_utc(body.started_at), body.query_version, body.client_version,
                 body.agent_seq, str(execution_id)),
            )
            ex = cur.fetchone()
            sync_changed = False
            if body.status in TERMINAL:
                sync_changed = apply_sync_state(cur, ctx, ex, body, err_code, cp)
                if health is not None:
                    health.on_execution_terminal(
                        cur, installation_id=str(ctx.id), installation_name=ctx.name, task=t, ex=ex,
                        status=body.status, error_code=err_code, message=err_msg, warnings=warnings,
                        checkpoint_applied=cp is not None)
                # Evento compatible para el monitor/panel actuales.
                if body.status == "success":
                    detail = f"{body.rows_loaded or 0} filas cargadas en '{t['destination_table']}'"
                    if warnings:
                        detail += " | avisos: " + ", ".join(warnings)
                    ev_type, ack = "ok", 1
                else:
                    detail = f"[{err_code}] etapa {body.failure_stage or '?'}: {err_msg or ''}"[:2000]
                    ev_type, ack = "error", 0
                cur.execute(
                    """
                    INSERT INTO client_events
                        (token, group_name, company_name, config_id, task_name, event_type, detail, rows_loaded,
                         is_acknowledged, source, auth_kind, group_id, company_id, agency_id, task_id,
                         installation_id, execution_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'agent', 'installation', %s, %s, %s, %s, %s, %s)
                    """,
                    (str(ctx.id)[:8], t["group_name"], t["company_name"], str(t["task_id"]),
                     f"{t['group_name']} | {t['company_name']} | {t['agency_name']}", ev_type, detail,
                     min(int(body.rows_loaded or 0), 2_147_483_647), ack, t["group_id"], t["company_id"],
                     t["agency_id"], t["task_id"], str(ctx.id), str(execution_id)),
                )
        return {"status": "ok", "applied": True, "execution_id": str(execution_id),
                "sync_state_updated": sync_changed}

    # ── Latido y eventos ────────────────────────────────────────────────────
    @agent.post("/heartbeat")
    def heartbeat(body: HeartbeatBody, ctx: InstallationCtx = Depends(authenticate)) -> dict:
        running = [{"task_id": r.task_id, "execution_id": str(r.execution_id) if r.execution_id else None}
                   for r in body.running]
        summary = {
            "uptime_seconds": body.uptime_seconds, "queue_depth": body.queue_depth,
            "queue_overflow_total": body.queue_overflow_total, "dead_letter_total": body.dead_letter_total,
            "parked_total": body.parked_total,
            "running": running,
            "config_age_seconds": body.config_age_seconds, "agent_seq": body.agent_seq,
            "event_time": iso(to_utc(body.event_time)), "received_at": iso(utcnow()),
        }
        with tx() as cur:
            cur.execute(
                """UPDATE installation SET last_heartbeat = %s,
                          client_version = CASE WHEN %s <> '' THEN %s ELSE client_version END,
                          last_agent_seq = GREATEST(last_agent_seq, COALESCE(%s, 0))
                   WHERE id = %s""",
                (json.dumps(summary), body.client_version, body.client_version, body.agent_seq, str(ctx.id)),
            )
            cur.execute(
                """INSERT INTO installation_heartbeat
                       (installation_id, event_time, agent_seq, client_version, uptime_seconds, queue_depth, running)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (str(ctx.id), to_utc(body.event_time), body.agent_seq, body.client_version,
                 body.uptime_seconds, body.queue_depth, json.dumps(running)),
            )
            if health is not None:
                # El latido resuelve la desconexión (solo esa categoría).
                health.on_heartbeat(cur, str(ctx.id))
            maybe_cleanup(cur)
        return {"status": "ok", "server_time": iso(utcnow()),
                "credential_rotation_required": ctx.rotation_required}

    @agent.post("/events")
    def agent_event(body: AgentEventBody, ctx: InstallationCtx = Depends(authenticate)) -> dict:
        if body.event_type not in AGENT_EVENT_TYPES:
            raise http_error(422, "unknown_event_type", "Tipo de evento no admitido.")
        payload: Dict[str, Any] = {}
        for k, v in list(body.payload.items())[:30]:
            if isinstance(v, (int, float, bool)) or v is None:
                payload[str(k)[:40]] = v
            else:
                payload[str(k)[:40]] = redact_text(str(v)[:1000], max_len=300)
        with tx() as cur:
            cur.execute(
                """INSERT INTO agent_event (event_id, installation_id, event_type, agent_seq, event_time, payload)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
                (str(body.event_id), str(ctx.id), body.event_type, body.agent_seq, to_utc(body.event_time),
                 json.dumps(payload)),
            )
            created = cur.fetchone() is not None
            bump_seq(cur, ctx, body.agent_seq)
            if created and health is not None:
                health.on_agent_event(
                    cur, installation_id=str(ctx.id), installation_name=ctx.name,
                    scope={"group_id": ctx.group_id, "company_id": ctx.company_id, "agency_id": ctx.agency_id},
                    event_type=body.event_type, payload=payload)
        return {"status": "ok", "duplicate": not created}

    # ── Rotación de credencial ──────────────────────────────────────────────
    @agent.post("/credentials/rotate")
    def rotate_credentials(ctx: InstallationCtx = Depends(authenticate)) -> dict:
        """
        Con el secreto VIGENTE: emite uno nuevo; el vigente pasa a "anterior" con
        una gracia fija (rotation_grace_seconds, nunca se extiende).
        Con el secreto ANTERIOR (respuesta perdida): devuelve el MISMO secreto
        nuevo ya emitido (idempotente, recuperable solo con el anterior). No se
        emiten secretos adicionales: no hay cadena de secuestro.
        """
        with tx() as cur:
            cur.execute("SELECT credential_hash, pending_secret_enc, previous_valid_until FROM installation "
                        "WHERE id = %s FOR UPDATE", (str(ctx.id),))
            row = cur.fetchone()
            if ctx.used_previous_secret:
                pending = decrypt_pending(row.get("pending_secret_enc"), ctx.presented_secret)
                if not pending or not hmac.compare_digest(hash_secret(pending), row["credential_hash"]):
                    raise http_error(409, "rotation_unavailable",
                                     "No hay un secreto pendiente recuperable; use la credencial vigente o re-enrole.")
                remaining = int(max(0, (row["previous_valid_until"] - utcnow()).total_seconds()))
                log.info("Re-entrega del secreto pendiente a la instalación %s", ctx.id)
                return {"installation_id": str(ctx.id), "secret": pending, "previous_valid_seconds": remaining,
                        "redelivered": True}
            secret = new_secret()
            # El secreto con el que se autenticó es el vigente: se necesita en claro para cifrar
            # el pendiente; se obtiene del header ya validado (no se guarda en claro).
            cur.execute(
                """UPDATE installation SET previous_credential_hash = credential_hash,
                          previous_valid_until = NOW() + make_interval(secs => %s),
                          credential_hash = %s, pending_secret_enc = %s,
                          credential_rotated_at = NOW(), rotation_required = FALSE
                   WHERE id = %s""",
                (rotation_grace_seconds, hash_secret(secret), encrypt_pending(secret, ctx.current_secret),
                 str(ctx.id)),
            )
        log.info("Credencial rotada para la instalación %s", ctx.id)
        return {"installation_id": str(ctx.id), "secret": secret,
                "previous_valid_seconds": rotation_grace_seconds, "redelivered": False}

    # ═════════════════════════════════════════════════════════════════════════
    # Administración
    # ═════════════════════════════════════════════════════════════════════════
    configured_admin = (admin_token or "").strip()

    def require_admin(x_admin_token: Optional[str] = Header(None, alias="x-admin-token")) -> None:
        if not configured_admin:
            raise HTTPException(status_code=503, detail="Admin no configurado.")
        provided = (x_admin_token or "").encode("utf-8")
        if not provided or not hmac.compare_digest(provided, configured_admin.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Token de administrador no válido.")

    admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

    INSTALLATION_SELECT = """
        SELECT i.id, i.name, i.hostname, i.os_info, i.scope_type, i.group_id, g.name AS group_name,
               i.company_id, c.name AS company_name, i.agency_id, a.name AS agency_name,
               i.status, i.enrolled_via, i.enrollment_token_prefix, i.client_version,
               i.last_seen_at, i.last_ip, i.last_heartbeat, i.credential_rotated_at, i.rotation_required,
               (i.previous_credential_hash IS NOT NULL AND i.previous_valid_until > NOW()) AS rotation_in_grace,
               i.revoked_at, i.revoked_reason, i.created_at, i.updated_at, i.last_agent_seq,
               (SELECT COUNT(*) FROM task_execution e WHERE e.installation_id = i.id
                  AND e.status = 'failed' AND e.received_at >= NOW() - INTERVAL '24 hours') AS failures_24h,
               (SELECT MAX(e.finished_at) FROM task_execution e WHERE e.installation_id = i.id) AS last_execution_at
        FROM installation i
        JOIN client_group g ON g.id = i.group_id
        LEFT JOIN company c ON c.id = i.company_id
        LEFT JOIN agency a  ON a.id = i.agency_id
    """

    def inst_out(r: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(r)
        out["id"] = str(r["id"])
        out["legacy"] = False
        for k in ("last_seen_at", "credential_rotated_at", "revoked_at", "created_at", "updated_at", "last_execution_at"):
            out[k] = iso(r.get(k))
        return out

    def list_installations(group_id: Optional[int], status: Optional[str]) -> List[Dict[str, Any]]:
        conds, params = [], []
        if group_id is not None:
            conds.append("i.group_id = %s")
            params.append(group_id)
        if status in ("active", "revoked"):
            conds.append("i.status = %s")
            params.append(status)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with tx() as cur:
            cur.execute(INSTALLATION_SELECT + where + " ORDER BY g.name, i.name", tuple(params))
            return [inst_out(r) for r in cur.fetchall()]

    def legacy_clients() -> List[Dict[str, Any]]:
        """Clientes que aún usan tokens legados (según activity_log de los últimos 30 días)."""
        with tx() as cur:
            cur.execute(
                """
                SELECT al.auth_kind, al.group_id, g.name AS group_name, al.company_id, c.name AS company_name,
                       al.agency_id, a.name AS agency_name, al.token AS token_prefix,
                       MAX(al.created_at)::timestamptz AS last_seen_at, COUNT(*) AS requests_30d,
                       SUM(CASE WHEN al.status_code >= 400 THEN 1 ELSE 0 END) AS http_errors_30d,
                       (ARRAY_AGG(al.client_ip ORDER BY al.created_at DESC))[1] AS last_ip
                FROM activity_log al
                LEFT JOIN client_group g ON g.id = al.group_id
                LEFT JOIN company c ON c.id = al.company_id
                LEFT JOIN agency a ON a.id = al.agency_id
                WHERE al.auth_kind IN ('group', 'company', 'agency')
                  AND al.group_id IS NOT NULL  -- intentos con token inválido no son "clientes"
                  AND al.created_at >= NOW() - INTERVAL '30 days'
                  AND (al.endpoint IN ('/configs', '/group-configs', '/agency-configs', '/client-event')
                       OR al.endpoint LIKE '/configs/%/last_run')
                GROUP BY al.auth_kind, al.group_id, g.name, al.company_id, c.name, al.agency_id, a.name, al.token
                ORDER BY MAX(al.created_at) DESC
                """
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["legacy"] = True
            d["last_seen_at"] = iso(r["last_seen_at"])
            d["requests_30d"] = int(r["requests_30d"] or 0)
            d["http_errors_30d"] = int(r["http_errors_30d"] or 0)
            out.append(d)
        return out

    @admin.get("/installations")
    def admin_list_installations(group_id: Optional[int] = Query(None), status: Optional[str] = Query(None)) -> dict:
        return {"items": list_installations(group_id, status)}

    @admin.get("/installations/{installation_id}")
    def admin_get_installation(installation_id: uuid.UUID) -> dict:
        with tx() as cur:
            cur.execute(INSTALLATION_SELECT + " WHERE i.id = %s", (str(installation_id),))
            r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Instalación no encontrada.")
        return inst_out(r)

    @admin.post("/installations/{installation_id}/revoke")
    def admin_revoke(installation_id: uuid.UUID, body: Optional[RevokeBody] = None) -> dict:
        with tx() as cur:
            cur.execute(
                """UPDATE installation SET status = 'revoked', revoked_at = NOW(), revoked_reason = %s,
                          previous_credential_hash = NULL, previous_valid_until = NULL
                   WHERE id = %s""",
                ((body.reason if body else None), str(installation_id)),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Instalación no encontrada.")
        return admin_get_installation(installation_id)

    @admin.post("/installations/{installation_id}/rotate")
    def admin_rotate(installation_id: uuid.UUID) -> dict:
        """
        Marca la instalación para rotar su secreto: en su próximo contacto el
        agente recibe credential_rotation_required=true y llama a
        POST /agent/credentials/rotate. El panel nunca ve el secreto.
        """
        with tx() as cur:
            cur.execute("UPDATE installation SET rotation_required = TRUE WHERE id = %s AND status = 'active'",
                        (str(installation_id),))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Instalación no encontrada o revocada.")
        return admin_get_installation(installation_id)

    @admin.get("/executions")
    def admin_executions(
        group_id: Optional[int] = Query(None), company_id: Optional[int] = Query(None),
        agency_id: Optional[int] = Query(None), task_id: Optional[int] = Query(None),
        installation_id: Optional[uuid.UUID] = Query(None), status: Optional[str] = Query(None),
        failure_stage: Optional[str] = Query(None), since: Optional[datetime] = Query(None),
        until: Optional[datetime] = Query(None), limit: int = Query(200, ge=1, le=2000),
    ) -> dict:
        conds, params = [], []
        for col, val in (("e.group_id", group_id), ("e.company_id", company_id), ("e.agency_id", agency_id),
                         ("e.task_id", task_id)):
            if val is not None:
                conds.append(f"{col} = %s")
                params.append(val)
        if installation_id is not None:
            conds.append("e.installation_id = %s")
            params.append(str(installation_id))
        if status in STATUSES:
            conds.append("e.status = %s")
            params.append(status)
        if failure_stage in STAGES:
            conds.append("e.failure_stage = %s")
            params.append(failure_stage)
        if since is not None:
            conds.append("COALESCE(e.started_at, e.received_at) >= %s")
            params.append(to_utc(since))
        if until is not None:
            conds.append("COALESCE(e.started_at, e.received_at) <= %s")
            params.append(to_utc(until))
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with tx() as cur:
            cur.execute(
                f"""
                SELECT e.execution_id, e.installation_id, i.name AS installation_name, e.task_id,
                       e.group_id, g.name AS group_name, e.company_id, c.name AS company_name,
                       e.agency_id, a.name AS agency_name, oc.name AS object_name, oc.destination_table,
                       e.client_version, e.query_version, e.attempt, e.status, e.failure_stage,
                       e.started_at, e.finished_at, e.duration_ms, e.rows_read, e.rows_loaded,
                       e.rows_inserted, e.rows_updated, e.error_code, e.error_message_sanitized, e.warnings,
                       e.checkpoint_confirmed, e.checkpoint_kind, e.event_time, e.received_at
                FROM task_execution e
                LEFT JOIN installation i ON i.id = e.installation_id
                LEFT JOIN client_group g ON g.id = e.group_id
                LEFT JOIN company c ON c.id = e.company_id
                LEFT JOIN agency a ON a.id = e.agency_id
                LEFT JOIN object_catalog oc ON oc.id = e.object_catalog_id
                {where}
                ORDER BY COALESCE(e.started_at, e.received_at) DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            rows = cur.fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["execution_id"] = str(r["execution_id"])
            d["installation_id"] = str(r["installation_id"])
            for k in ("started_at", "finished_at", "event_time", "received_at"):
                d[k] = iso(r[k])
            d["checkpoint_confirmed"] = r["checkpoint_confirmed"].isoformat() if r["checkpoint_confirmed"] else None
            items.append(d)
        return {"total": len(items), "items": items}

    @admin.get("/sync-state")
    def admin_sync_state(group_id: Optional[int] = Query(None), task_id: Optional[int] = Query(None)) -> dict:
        conds, params = [], []
        if group_id is not None:
            conds.append("c.group_id = %s")
            params.append(group_id)
        if task_id is not None:
            conds.append("s.task_id = %s")
            params.append(task_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with tx() as cur:
            cur.execute(
                f"""
                SELECT s.task_id, g.name AS group_name, c.name AS company_name, a.name AS agency_name,
                       oc.name AS object_name, s.watermark, s.watermark_kind, s.last_success_at,
                       s.last_failure_at, s.last_execution_id, s.last_status, s.consecutive_failures,
                       s.current_error_code, s.installation_id, s.updated_at
                FROM task_sync_state s
                JOIN agency_task at ON at.id = s.task_id
                JOIN agency a ON a.id = at.agency_id
                JOIN company c ON c.id = a.company_id
                JOIN client_group g ON g.id = c.group_id
                JOIN object_catalog oc ON oc.id = at.object_catalog_id
                {where}
                ORDER BY g.name, c.name, a.name, oc.name
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["watermark"] = r["watermark"].isoformat() if r["watermark"] else None
            for k in ("last_success_at", "last_failure_at", "updated_at"):
                d[k] = iso(r[k])
            d["last_execution_id"] = str(r["last_execution_id"]) if r["last_execution_id"] else None
            d["installation_id"] = str(r["installation_id"]) if r["installation_id"] else None
            items.append(d)
        return {"items": items}

    @admin.get("/legacy-clients")
    def admin_legacy_clients() -> dict:
        return {"items": legacy_clients()}

    # ═════════════════════════════════════════════════════════════════════════
    # Monitor
    # ═════════════════════════════════════════════════════════════════════════
    def require_monitor(x_monitor_token: Optional[str] = Header(None, alias="x-monitor-token")) -> None:
        if not monitor_token:
            raise HTTPException(status_code=503, detail="Monitor no configurado.")
        provided = (x_monitor_token or "").encode("utf-8")
        if not provided or not hmac.compare_digest(provided, monitor_token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Token de monitor no válido.")

    monitor = APIRouter(prefix="/monitor", tags=["monitor"], dependencies=[Depends(require_monitor)])

    @monitor.get("/installations")
    def monitor_installations() -> dict:
        items = list_installations(None, None)
        for it in items:
            # Sin datos de red internos en el monitor.
            it.pop("last_ip", None)
        return {"installations": items, "legacy_clients": legacy_clients()}

    return agent, admin, monitor
