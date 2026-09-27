"""
nexus_server_postgres.py  v4 — PostgreSQL
──────────────────────────────────────────
Versión del servidor Nexus que usa PostgreSQL en lugar de MySQL.

Agentes nuevos (identidad por instalación; ver agent_postgres.py):
  POST /agent/enroll             → alta de la instalación con token de enrolamiento
  GET  /agent/tasks              → tareas autorizadas para la instalación
  POST /agent/executions, PUT /agent/executions/{id}, POST /agent/heartbeat,
  POST /agent/events, POST /agent/credentials/rotate

Endpoints LEGADOS de clientes ETL (compatibilidad; [agent] legacy_endpoints):
  GET  /configs                  → todas las tareas de una company (x-token = company_token)
  GET  /agency-configs           → solo tareas de una agency (x-agency-token)
  GET  /group-configs            → tareas de todas las companies del grupo (x-group-token)
  PUT  /configs/{id}/last_run    → marcar tarea ejecutada (mismos headers que el GET correspondiente)
  POST /client-event             → clientes reportan eventos (éxito o error)
  Prioridad de tokens en TODO el backend: grupo > agencia > empresa.

Endpoints monitor (requieren header x-monitor-token):
  GET  /monitor/installations    → instalaciones (agentes nuevos) + clientes legados
  GET  /monitor/events           → historial de eventos
  GET  /monitor/clients          → resumen de todos los clientes
  GET  /monitor/activity         → log HTTP del backend
  PUT  /monitor/events/{id}/ack  → reconocer una alerta
  PUT  /monitor/events/ack-all   → reconocer todas las alertas

Endpoints administración (requieren header x-admin-token; ver admin_postgres.py):
  /admin/*                       → CRUD de grupos, companies, agencias,
                                   catálogo de objetos y tareas (panel dwh_front)
"""

import argparse
import configparser
import hmac
import logging
import os
import sys
import time
import traceback
from datetime import timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - dependencia opcional en dev
    Fernet = None
    InvalidToken = Exception

app = FastAPI(title="Nexus Config API (PostgreSQL)", version="3.0.0")


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

# NEXUS_CONFIG_FILE permite usar otro config.ini (p. ej. pruebas automatizadas).
CONFIG_PATH = os.environ.get("NEXUS_CONFIG_FILE", "").strip() or os.path.join(_APP_DIR, "config.ini")
_ini = configparser.ConfigParser()
_ini.read(CONFIG_PATH)

# Los módulos hermanos (admin_postgres, agent_postgres, redact) se importan por nombre.
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from redact import redact_text, token_prefix  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stderr,
)
server_log = logging.getLogger("nexus.server")

DB_HOST       = _ini.get("database", "host",     fallback="127.0.0.1")
DB_PORT       = _ini.getint("database", "port",  fallback=5432)
DB_NAME       = _ini.get("database", "db",       fallback="nexus_config")
DB_USER       = _ini.get("database", "user",     fallback="postgres")
DB_PASS       = _ini.get("database", "password", fallback="")
MONITOR_TOKEN = _ini.get("monitor",  "token",    fallback="")
CONFIG_SECRET_KEY = _ini.get(
    "security", "config_secret_key",
    fallback=os.environ.get("NEXUS_CONFIG_SECRET_KEY", ""),
).strip()
# Token del panel de administración (/admin/*). Vacío = admin deshabilitado (503).
ADMIN_TOKEN = (
    _ini.get("admin", "token", fallback="").strip()
    or os.environ.get("NEXUS_ADMIN_TOKEN", "").strip()
)
# API del agente por instalación (/agent/*)
AGENT_CONFIG_MAX_AGE = _ini.getint("agent", "config_max_age_seconds", fallback=900)
AGENT_ROTATION_GRACE = _ini.getint("agent", "rotation_grace_seconds", fallback=3600)
AGENT_HEARTBEAT_RETENTION_DAYS = _ini.getint("agent", "heartbeat_retention_days", fallback=7)
AGENT_DOWNLOAD_LOG_RETENTION_DAYS = _ini.getint("agent", "download_log_retention_days", fallback=30)
# Endpoints legados (/configs, /group-configs, /agency-configs, /client-event,
# /configs/{id}/last_run). true = siguen activos (compatibilidad); false = 410.
LEGACY_ENDPOINTS_ENABLED = _ini.getboolean("agent", "legacy_endpoints", fallback=True)
# Aplicar migraciones pendientes al arrancar (por defecto NO: usar migrate.py).
AUTO_MIGRATE = _ini.getboolean("database", "auto_migrate", fallback=False)
# Tolerancia (h) para fechas "futuras" que manda el agente (relojes/husos).
AGENT_FUTURE_TOLERANCE_HOURS = _ini.getint("agent", "future_tolerance_hours", fallback=26)
# Límite de tamaño del cuerpo de las peticiones (bytes). 413 si se supera.
MAX_BODY_BYTES = _ini.getint("server", "max_body_bytes", fallback=1_048_576)
AGENT_MAX_BODY_BYTES = _ini.getint("server", "agent_max_body_bytes", fallback=262_144)
ENROLL_MAX_BODY_BYTES = _ini.getint("server", "enroll_max_body_bytes", fallback=16_384)
# Orígenes permitidos para CORS (separados por coma). Vacío = sin CORS
# (el panel dwh_front usa un proxy del lado servidor y no lo necesita).
CORS_ORIGINS = [
    o.strip()
    for o in (
        _ini.get("cors", "origins", fallback="").strip()
        or os.environ.get("NEXUS_CORS_ORIGINS", "").strip()
    ).split(",")
    if o.strip()
]


# ─────────────────────────────────────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────────────────────────────────────
class SyncTaskConfig(BaseModel):
    id: str
    name: str
    schedule_seconds: int = 3600
    extract_sql: str
    load_table: str
    upsert_keys: List[str] = Field(default_factory=list)
    query_tabla_destino: Optional[str] = None
    constraint_nombre: Optional[str] = None
    query_constraint: Optional[str] = None
    active: bool = True
    run_on_company_token: bool = True
    last_run_at: Optional[str] = None
    updated_at: str = ""
    static_columns: Optional[str] = None


TaskConfig = SyncTaskConfig


class CompanyConfigsResponse(BaseModel):
    log_verbose: bool = False
    refresh_seconds: int = 60
    dwh_host: str = ""
    dwh_port: int = 5432
    dwh_db: str = ""
    dwh_user: str = ""
    dwh_pass: str = ""
    origen_tipo: str = "sqlserver"
    origen_ip: str = ""
    origen_port: int = 1433
    origen_db: str = ""
    origen_user: str = ""
    origen_pass: str = ""
    dsn_odbc: str = ""
    configs: List[SyncTaskConfig] = Field(default_factory=list)


ConfigsResponse = CompanyConfigsResponse


class SourceRuntimeConfig(BaseModel):
    source_type: str = "sqlserver"
    source_host: str = ""
    source_port: int = 1433
    source_database: str = ""
    source_username: str = ""
    source_password: str = ""
    source_dsn: str = ""


class GroupSyncTaskConfig(SyncTaskConfig):
    group_id: int
    group_name: str
    company_id: int
    company_name: str
    agency_name: str
    source: SourceRuntimeConfig


class GroupConfigsResponse(BaseModel):
    group_name: str = ""
    log_verbose: bool = False
    refresh_seconds: int = 60
    warehouse_host: str = ""
    warehouse_port: int = 5432
    warehouse_database: str = ""
    warehouse_username: str = ""
    warehouse_password: str = ""
    configs: List[GroupSyncTaskConfig] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Conexión BD
# ─────────────────────────────────────────────────────────────────────────────
def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASS, dbname=DB_NAME,
    )


_secret_cipher: Optional["Fernet"] = None
_group_token_column_exists: Optional[bool] = None
_agency_token_column_exists: Optional[bool] = None


def get_secret_cipher() -> Optional["Fernet"]:
    global _secret_cipher
    if _secret_cipher is not None:
        return _secret_cipher
    if not CONFIG_SECRET_KEY or Fernet is None:
        return None
    _secret_cipher = Fernet(CONFIG_SECRET_KEY.encode("utf-8"))
    return _secret_cipher


def decrypt_config_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if not isinstance(value, str):
        return str(value)
    if not value.startswith("ENC:"):
        return value
    cipher = get_secret_cipher()
    if cipher is None:
        raise RuntimeError(
            "Se encontró un secreto cifrado pero falta cryptography o config_secret_key."
        )
    token = value[4:].strip().encode("utf-8")
    try:
        return cipher.decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("No se pudo descifrar un secreto de configuración.") from exc


def group_token_column_exists() -> bool:
    global _group_token_column_exists
    if _group_token_column_exists is not None:
        return _group_token_column_exists
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'client_group'
              AND column_name = 'group_token'
            LIMIT 1
            """
        )
        _group_token_column_exists = cur.fetchone() is not None
    finally:
        conn.close()
    return _group_token_column_exists


def agency_token_column_exists() -> bool:
    global _agency_token_column_exists
    if _agency_token_column_exists is not None:
        return _agency_token_column_exists
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'agency'
              AND column_name = 'agency_token'
            LIMIT 1
            """
        )
        _agency_token_column_exists = cur.fetchone() is not None
    finally:
        conn.close()
    return _agency_token_column_exists


# ─────────────────────────────────────────────────────────────────────────────
# Resolver identidad (tokens legados) — prioridad única: grupo > agencia > empresa
# ─────────────────────────────────────────────────────────────────────────────
from agent_postgres import (  # noqa: E402
    create_agent_routers,
    resolve_legacy_principal,
    scope_where,
)


def resolve_legacy(company_token: str = "", group_token: str = "", agency_token: str = "") -> Optional[Dict[str, Any]]:
    """Principal de un token legado (o None). Prioridad grupo > agencia > empresa."""
    if not company_token and not group_token and not agency_token:
        return None
    if group_token and not group_token_column_exists():
        group_token = ""
    if agency_token and not agency_token_column_exists():
        agency_token = ""
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return resolve_legacy_principal(
            cur, company_token=company_token, group_token=group_token, agency_token=agency_token
        )
    finally:
        conn.close()


def _active_legacy_token(company_token: str, group_token: str, agency_token: str) -> str:
    return group_token or agency_token or company_token


# ─────────────────────────────────────────────────────────────────────────────
# Middleware – activity_log (sin tokens completos, sin trazas, fuera del loop)
# ─────────────────────────────────────────────────────────────────────────────
_ERROR_DETAIL_MAX = 1000
_ERROR_BODY_CAPTURE = 16_384


def _record_activity(
    audit: Dict[str, Any], company_token: str, group_token: str, agency_token: str,
    method: str, endpoint: str, status_code: int, response_ms: int,
    error_detail: Optional[str], client_ip: str,
) -> None:
    """Se ejecuta en un threadpool (llamadas BD síncronas)."""
    try:
        info: Dict[str, Any] = dict(audit or {})
        legacy_token = _active_legacy_token(company_token, group_token, agency_token)
        if not info.get("auth_kind"):
            if legacy_token:
                kind = "group" if group_token else ("agency" if agency_token else "company")
                info["auth_kind"] = kind
                p = None
                try:
                    p = resolve_legacy(company_token, group_token, agency_token)
                except Exception:
                    p = None
                if p:
                    info.update(group_id=p["group_id"], company_id=p["company_id"], agency_id=p["agency_id"],
                                group_name=p["group_name"], company_name=p["company_name"])
            elif endpoint.startswith("/admin"):
                info["auth_kind"] = "admin"
            elif endpoint.startswith("/monitor"):
                info["auth_kind"] = "monitor"
        prefix = info.get("token_prefix") or token_prefix(legacy_token)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO activity_log
                    (token, company_name, group_name, method, endpoint, status_code, response_ms,
                     error_detail, client_ip, auth_kind, group_id, company_id, agency_id, installation_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (prefix, (info.get("company_name") or "")[:255], (info.get("group_name") or "")[:255],
                 method[:10], endpoint[:255], status_code, response_ms,
                 (redact_text(error_detail, max_len=_ERROR_DETAIL_MAX) if error_detail else None), client_ip[:45], (info.get("auth_kind") or "")[:20],
                 info.get("group_id"), info.get("company_id"), info.get("agency_id"),
                 info.get("installation_id")),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # la auditoría nunca debe romper la petición
        server_log.warning("No se pudo registrar activity_log: %s", type(exc).__name__)


@app.middleware("http")
async def activity_logging_middleware(request: Request, call_next):
    start     = time.time()
    company_token = request.headers.get("x-token", "")
    group_token = request.headers.get("x-group-token", "")
    agency_token = request.headers.get("x-agency-token", "")
    endpoint  = request.url.path
    method    = request.method
    client_ip = request.client.host if request.client else ""

    error_detail: Optional[str] = None
    status_code = 500

    try:
        response    = await call_next(request)
        status_code = response.status_code

        if status_code >= 400:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            # Se sanea en el threadpool (_record_activity), nunca en el event loop.
            error_detail = body[:_ERROR_BODY_CAPTURE].decode("utf-8", errors="replace")
            from starlette.responses import Response as RawResponse
            response = RawResponse(
                content=body, status_code=status_code,
                headers=dict(response.headers), media_type=response.media_type,
            )
        return response

    except Exception as exc:
        status_code  = 500
        # En BD solo el tipo; la traza (saneada) va a stderr del servidor.
        error_detail = f"Error interno ({type(exc).__name__})."
        tb = traceback.format_exc()[-16_000:]
        await run_in_threadpool(
            lambda: server_log.error("Excepción no controlada en %s %s:\n%s", method, endpoint,
                                     redact_text(tb, max_len=8000))
        )
        return JSONResponse(status_code=500, content={"detail": "Error interno."})

    finally:
        elapsed_ms = int((time.time() - start) * 1000)
        # Las validaciones de sesión del panel (/admin/whoami OK) no se registran.
        if not (endpoint == "/admin/whoami" and status_code < 400):
            audit = getattr(request.state, "audit", None) or {}
            await run_in_threadpool(
                _record_activity, audit, company_token, group_token, agency_token,
                method, endpoint, status_code, elapsed_ms, error_detail, client_ip,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────
def _require_monitor_token(token: str) -> None:
    if not MONITOR_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Monitor no configurado: agrega [monitor] token=... en config.ini del servidor.",
        )
    # Comparación en tiempo constante.
    if not token or not hmac.compare_digest(token.encode("utf-8"), MONITOR_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Token de monitor no válido.")


def _require_legacy_enabled() -> None:
    if not LEGACY_ENDPOINTS_ENABLED:
        raise HTTPException(
            status_code=410,
            detail="Endpoint legado deshabilitado: actualice el agente (API /agent con enrolamiento).",
        )

def resolve_company_token(company_token: str) -> Dict[str, Any]:
    """
    Resuelve el token de un cliente ETL por razón social.
    Retorna group + company + configuraciones de conexión.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                c.id              AS company_id,
                c.name            AS company_name,
                c.is_enabled      AS company_enabled,
                c.verbose_logging AS log_verbose,
                c.refresh_seconds,
                c.source_type     AS source_type,
                c.source_dsn      AS source_dsn,
                c.source_host     AS source_host,
                c.source_port     AS source_port,
                c.source_database AS source_database,
                c.source_username AS source_username,
                c.source_password AS source_password,
                g.id              AS group_id,
                g.name            AS group_name,
                g.is_enabled      AS group_enabled,
                g.warehouse_host  AS warehouse_host,
                g.warehouse_port  AS warehouse_port,
                g.warehouse_database AS warehouse_database,
                g.warehouse_username AS warehouse_username,
                g.warehouse_password AS warehouse_password
            FROM company c
            JOIN client_group g ON g.id = c.group_id
            WHERE c.company_token = %s
            """,
            (company_token,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"found": False}

    return {
        "found": True,
        "company_id": row[0],
        "company_name": row[1],
        "company_enabled": bool(row[2]),
        "log_verbose": bool(row[3]),
        "refresh_seconds": row[4],
        "source_type": (row[5] or "sqlserver").lower(),
        "source_dsn": decrypt_config_secret(row[6] or ""),
        "source_host": decrypt_config_secret(row[7] or ""),
        "source_port": row[8],
        "source_database": decrypt_config_secret(row[9] or ""),
        "source_username": decrypt_config_secret(row[10] or ""),
        "source_password": decrypt_config_secret(row[11] or ""),
        "group_id": row[12],
        "group_name": row[13],
        "group_enabled": bool(row[14]),
        "warehouse_host": decrypt_config_secret(row[15] or ""),
        "warehouse_port": row[16],
        "warehouse_database": decrypt_config_secret(row[17] or ""),
        "warehouse_username": decrypt_config_secret(row[18] or ""),
        "warehouse_password": decrypt_config_secret(row[19] or ""),
    }


def resolve_group_token(group_token: str) -> Dict[str, Any]:
    if not group_token_column_exists():
        raise HTTPException(
            status_code=503,
            detail="El flujo de group token requiere la columna client_group.group_token en la BD de configuración.",
        )

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                g.id AS group_id,
                g.name AS group_name,
                g.is_enabled AS group_enabled,
                g.warehouse_host AS warehouse_host,
                g.warehouse_port AS warehouse_port,
                g.warehouse_database AS warehouse_database,
                g.warehouse_username AS warehouse_username,
                g.warehouse_password AS warehouse_password
            FROM client_group g
            WHERE g.group_token = %s
            """,
            (group_token,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"found": False}

    return {
        "found": True,
        "group_id": row[0],
        "group_name": row[1],
        "group_enabled": bool(row[2]),
        "warehouse_host": decrypt_config_secret(row[3] or ""),
        "warehouse_port": row[4],
        "warehouse_database": decrypt_config_secret(row[5] or ""),
        "warehouse_username": decrypt_config_secret(row[6] or ""),
        "warehouse_password": decrypt_config_secret(row[7] or ""),
    }


def resolve_agency_token(agency_token: str) -> Dict[str, Any]:
    if not agency_token_column_exists():
        return {"found": False}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.id AS agency_id,
                a.name AS agency_name,
                a.is_enabled AS agency_enabled,
                c.id AS company_id,
                c.name AS company_name,
                c.is_enabled AS company_enabled,
                c.verbose_logging AS log_verbose,
                c.refresh_seconds,
                c.source_type AS source_type,
                c.source_dsn AS source_dsn,
                c.source_host AS source_host,
                c.source_port AS source_port,
                c.source_database AS source_database,
                c.source_username AS source_username,
                c.source_password AS source_password,
                g.id AS group_id,
                g.name AS group_name,
                g.is_enabled AS group_enabled,
                g.warehouse_host AS warehouse_host,
                g.warehouse_port AS warehouse_port,
                g.warehouse_database AS warehouse_database,
                g.warehouse_username AS warehouse_username,
                g.warehouse_password AS warehouse_password
            FROM agency a
            JOIN company c ON c.id = a.company_id
            JOIN client_group g ON g.id = c.group_id
            WHERE a.agency_token = %s
            """,
            (agency_token,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"found": False}

    return {
        "found": True,
        "agency_id": row[0],
        "agency_name": row[1],
        "agency_enabled": bool(row[2]),
        "company_id": row[3],
        "company_name": row[4],
        "company_enabled": bool(row[5]),
        "log_verbose": bool(row[6]),
        "refresh_seconds": row[7],
        "source_type": (row[8] or "sqlserver").lower(),
        "source_dsn": decrypt_config_secret(row[9] or ""),
        "source_host": decrypt_config_secret(row[10] or ""),
        "source_port": row[11],
        "source_database": decrypt_config_secret(row[12] or ""),
        "source_username": decrypt_config_secret(row[13] or ""),
        "source_password": decrypt_config_secret(row[14] or ""),
        "group_id": row[15],
        "group_name": row[16],
        "group_enabled": bool(row[17]),
        "warehouse_host": decrypt_config_secret(row[18] or ""),
        "warehouse_port": row[19],
        "warehouse_database": decrypt_config_secret(row[20] or ""),
        "warehouse_username": decrypt_config_secret(row[21] or ""),
        "warehouse_password": decrypt_config_secret(row[22] or ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Configs de tareas
# ─────────────────────────────────────────────────────────────────────────────
def fetch_company_task_configs(company_token: str) -> List[SyncTaskConfig]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                at.id AS config_id,
                g.name AS group_name,
                c.name AS company_name,
                a.name AS agency_name,
                oc.destination_table AS destination_table,
                oc.upsert_keys,
                oc.create_table_sql AS create_table_sql,
                oc.constraint_name AS constraint_name,
                oc.create_constraint_sql AS create_constraint_sql,
                at.extract_sql,
                at.schedule_seconds,
                at.is_active,
                at.last_run_at,
                at.updated_at,
                at.run_on_company_token,
                oc.static_columns
            FROM agency_task at
            JOIN agency a ON a.id = at.agency_id
            JOIN object_catalog oc ON oc.id = at.object_catalog_id
            JOIN company c ON c.id = a.company_id
            JOIN client_group g ON g.id = c.group_id
            WHERE c.company_token = %s
              AND at.is_active = TRUE
              AND at.run_on_company_token = TRUE
              AND a.is_enabled = TRUE
              AND oc.is_enabled = TRUE
              AND c.is_enabled = TRUE
              AND g.is_enabled = TRUE
            ORDER BY at.id
            """,
            (company_token,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    task_configs: List[SyncTaskConfig] = []
    for row in rows:
        upsert_keys = [k.strip() for k in (row[5] or "").split(",") if k.strip()]
        task_configs.append(SyncTaskConfig(
            id=str(row[0]),
            name=f"{row[1]} | {row[2]} | {row[3]}",
            load_table=row[4],
            upsert_keys=upsert_keys,
            query_tabla_destino=row[6]  or None,
            constraint_nombre=row[7]    or None,
            query_constraint=row[8]     or None,
            extract_sql=row[9],
            schedule_seconds=row[10],
            active=bool(row[11]),
            last_run_at=row[12].isoformat() if row[12] else None,
            updated_at=row[13].isoformat()  if row[13] else "",
            run_on_company_token=bool(row[14]),
            static_columns=row[15] or None,
        ))
    return task_configs


def fetch_agency_task_configs(agency_token: str) -> List[SyncTaskConfig]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                at.id AS config_id,
                g.name AS group_name,
                c.name AS company_name,
                a.name AS agency_name,
                oc.destination_table AS destination_table,
                oc.upsert_keys,
                oc.create_table_sql AS create_table_sql,
                oc.constraint_name AS constraint_name,
                oc.create_constraint_sql AS create_constraint_sql,
                at.extract_sql,
                at.schedule_seconds,
                at.is_active,
                at.last_run_at,
                at.updated_at,
                at.run_on_company_token,
                oc.static_columns
            FROM agency_task at
            JOIN agency a ON a.id = at.agency_id
            JOIN object_catalog oc ON oc.id = at.object_catalog_id
            JOIN company c ON c.id = a.company_id
            JOIN client_group g ON g.id = c.group_id
            WHERE a.agency_token = %s
              AND at.is_active = TRUE
              AND a.is_enabled = TRUE
              AND oc.is_enabled = TRUE
              AND c.is_enabled = TRUE
              AND g.is_enabled = TRUE
            ORDER BY at.id
            """,
            (agency_token,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    task_configs: List[SyncTaskConfig] = []
    for row in rows:
        upsert_keys = [k.strip() for k in (row[5] or "").split(",") if k.strip()]
        task_configs.append(SyncTaskConfig(
            id=str(row[0]),
            name=f"{row[1]} | {row[2]} | {row[3]}",
            load_table=row[4],
            upsert_keys=upsert_keys,
            query_tabla_destino=row[6]  or None,
            constraint_nombre=row[7]    or None,
            query_constraint=row[8]     or None,
            extract_sql=row[9],
            schedule_seconds=row[10],
            active=bool(row[11]),
            last_run_at=row[12].isoformat() if row[12] else None,
            updated_at=row[13].isoformat()  if row[13] else "",
            run_on_company_token=bool(row[14]),
            static_columns=row[15] or None,
        ))
    return task_configs


def fetch_group_task_configs(group_token: str) -> Tuple[List[GroupSyncTaskConfig], bool, int]:
    if not group_token_column_exists():
        raise HTTPException(
            status_code=503,
            detail="El flujo de group token requiere la columna client_group.group_token en la BD de configuración.",
        )

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                at.id AS config_id,
                g.id AS group_id,
                g.name AS group_name,
                c.id AS company_id,
                c.name AS company_name,
                a.name AS agency_name,
                c.verbose_logging,
                c.refresh_seconds,
                c.source_type AS source_type,
                c.source_dsn AS source_dsn,
                c.source_host AS source_host,
                c.source_port AS source_port,
                c.source_database AS source_database,
                c.source_username AS source_username,
                c.source_password AS source_password,
                oc.destination_table AS destination_table,
                oc.upsert_keys,
                oc.create_table_sql AS create_table_sql,
                oc.constraint_name AS constraint_name,
                oc.create_constraint_sql AS create_constraint_sql,
                at.extract_sql,
                at.schedule_seconds,
                at.is_active,
                at.last_run_at,
                at.updated_at,
                at.run_on_company_token,
                oc.static_columns
            FROM agency_task at
            JOIN agency a ON a.id = at.agency_id
            JOIN object_catalog oc ON oc.id = at.object_catalog_id
            JOIN company c ON c.id = a.company_id
            JOIN client_group g ON g.id = c.group_id
            WHERE g.group_token = %s
              AND at.is_active = TRUE
              AND a.is_enabled = TRUE
              AND oc.is_enabled = TRUE
              AND c.is_enabled = TRUE
              AND g.is_enabled = TRUE
            ORDER BY c.id, at.id
            """,
            (group_token,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    task_configs: List[GroupSyncTaskConfig] = []
    group_log_verbose = False
    refresh_candidates: List[int] = []

    for row in rows:
        group_log_verbose = group_log_verbose or bool(row[6])
        if row[7]:
            refresh_candidates.append(int(row[7]))
        upsert_keys = [k.strip() for k in (row[16] or "").split(",") if k.strip()]
        task_configs.append(
            GroupSyncTaskConfig(
                id=str(row[0]),
                group_id=row[1],
                group_name=row[2],
                company_id=row[3],
                company_name=row[4],
                agency_name=row[5],
                name=f"{row[2]} | {row[4]} | {row[5]}",
                schedule_seconds=row[21],
                extract_sql=row[20],
                load_table=row[15],
                upsert_keys=upsert_keys,
                query_tabla_destino=row[17] or None,
                constraint_nombre=row[18] or None,
                query_constraint=row[19] or None,
                active=bool(row[22]),
                last_run_at=row[23].isoformat() if row[23] else None,
                updated_at=row[24].isoformat() if row[24] else "",
                run_on_company_token=bool(row[25]),
                static_columns=row[26] or None,
                source=SourceRuntimeConfig(
                    source_type=(row[8] or "sqlserver").lower(),
                    source_dsn=decrypt_config_secret(row[9] or ""),
                    source_host=decrypt_config_secret(row[10] or ""),
                    source_port=row[11],
                    source_database=decrypt_config_secret(row[12] or ""),
                    source_username=decrypt_config_secret(row[13] or ""),
                    source_password=decrypt_config_secret(row[14] or ""),
                ),
            )
        )

    refresh_seconds = min(refresh_candidates) if refresh_candidates else 60
    return task_configs, group_log_verbose, refresh_seconds


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints — clientes ETL
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/configs", response_model=CompanyConfigsResponse)
def get_configs(x_token: str = Header(...)) -> CompanyConfigsResponse:
    """[LEGADO] Los agentes nuevos usan GET /agent/tasks con credencial de instalación."""
    _require_legacy_enabled()
    token_state = resolve_company_token(x_token)

    if not token_state["found"]:
        raise HTTPException(status_code=401, detail="Token no válido.")
    if not token_state["group_enabled"]:
        raise HTTPException(status_code=403, detail=f"Grupo '{token_state['group_name']}' deshabilitado.")
    if not token_state["company_enabled"]:
        raise HTTPException(status_code=403, detail=f"Razón social '{token_state['company_name']}' deshabilitada.")

    return CompanyConfigsResponse(
        log_verbose=token_state["log_verbose"],
        refresh_seconds=token_state["refresh_seconds"],
        dwh_host=token_state["warehouse_host"],       dwh_port=token_state["warehouse_port"],
        dwh_db=token_state["warehouse_database"],     dwh_user=token_state["warehouse_username"],
        dwh_pass=token_state["warehouse_password"],
        origen_tipo=token_state["source_type"],
        origen_ip=token_state["source_host"],         origen_port=token_state["source_port"],
        origen_db=token_state["source_database"],     origen_user=token_state["source_username"],
        origen_pass=token_state["source_password"],   dsn_odbc=token_state["source_dsn"],
        configs=fetch_company_task_configs(x_token),
    )


@app.get("/group-configs", response_model=GroupConfigsResponse)
def get_group_configs(x_group_token: str = Header(...)) -> GroupConfigsResponse:
    """[LEGADO] Ver GET /agent/tasks."""
    _require_legacy_enabled()
    group_state = resolve_group_token(x_group_token)
    if not group_state["found"]:
        raise HTTPException(status_code=401, detail="Group token no válido.")
    if not group_state["group_enabled"]:
        raise HTTPException(status_code=403, detail=f"Grupo '{group_state['group_name']}' deshabilitado.")

    task_configs, group_log_verbose, refresh_seconds = fetch_group_task_configs(x_group_token)
    return GroupConfigsResponse(
        group_name=group_state["group_name"],
        log_verbose=group_log_verbose,
        refresh_seconds=refresh_seconds,
        warehouse_host=group_state["warehouse_host"],
        warehouse_port=group_state["warehouse_port"],
        warehouse_database=group_state["warehouse_database"],
        warehouse_username=group_state["warehouse_username"],
        warehouse_password=group_state["warehouse_password"],
        configs=task_configs,
    )


@app.get("/agency-configs", response_model=CompanyConfigsResponse)
def get_agency_configs(
    x_agency_token: str = Header(..., alias="x-agency-token"),
) -> CompanyConfigsResponse:
    """[LEGADO] Ver GET /agent/tasks."""
    _require_legacy_enabled()
    if not agency_token_column_exists():
        raise HTTPException(
            status_code=503,
            detail="El flujo por agencia requiere la columna agency.agency_token en la BD de configuración.",
        )
    token_state = resolve_agency_token(x_agency_token)
    if not token_state["found"]:
        raise HTTPException(status_code=401, detail="Agency token no válido.")
    if not token_state["group_enabled"]:
        raise HTTPException(
            status_code=403,
            detail=f"Grupo '{token_state['group_name']}' deshabilitado.",
        )
    if not token_state["company_enabled"]:
        raise HTTPException(
            status_code=403,
            detail=f"Razón social '{token_state['company_name']}' deshabilitada.",
        )
    if not token_state["agency_enabled"]:
        raise HTTPException(
            status_code=403,
            detail=f"Agencia '{token_state['agency_name']}' deshabilitada.",
        )

    return CompanyConfigsResponse(
        log_verbose=token_state["log_verbose"],
        refresh_seconds=token_state["refresh_seconds"],
        dwh_host=token_state["warehouse_host"],
        dwh_port=token_state["warehouse_port"],
        dwh_db=token_state["warehouse_database"],
        dwh_user=token_state["warehouse_username"],
        dwh_pass=token_state["warehouse_password"],
        origen_tipo=token_state["source_type"],
        origen_ip=token_state["source_host"],
        origen_port=token_state["source_port"],
        origen_db=token_state["source_database"],
        origen_user=token_state["source_username"],
        origen_pass=token_state["source_password"],
        dsn_odbc=token_state["source_dsn"],
        configs=fetch_agency_task_configs(x_agency_token),
    )


def _resolve_task_access(
    config_id: int,
    company_token: Optional[str],
    group_token: Optional[str],
    agency_token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Tarea + principal si config_id pertenece al alcance del token legado.
    Misma prioridad que el resto del backend: grupo > agencia > empresa.
    """
    principal = resolve_legacy(company_token or "", group_token or "", agency_token or "")
    if not principal:
        return None
    where, params = scope_where(
        principal["kind"], principal["group_id"], principal["company_id"], principal["agency_id"]
    )
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            SELECT at.id AS config_id, at.agency_id, a.company_id, c.group_id,
                   c.name AS company_name, g.name AS group_name, a.name AS agency_name
            FROM agency_task at
            JOIN agency a ON a.id = at.agency_id
            JOIN company c ON c.id = a.company_id
            JOIN client_group g ON g.id = c.group_id
            WHERE at.id = %s AND {where}
            """,
            (config_id, *params),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {**dict(row), "principal": principal}


@app.put("/configs/{config_id}/last_run")
def update_last_run(
    config_id: int,
    x_token: Optional[str] = Header(None),
    x_group_token: Optional[str] = Header(None),
    x_agency_token: Optional[str] = Header(None, alias="x-agency-token"),
) -> dict:
    _require_legacy_enabled()
    if not x_token and not x_group_token and not x_agency_token:
        raise HTTPException(
            status_code=401,
            detail="Falta x-token, x-group-token o x-agency-token.",
        )
    task_access = _resolve_task_access(
        config_id, x_token, x_group_token, x_agency_token
    )
    if not task_access:
        raise HTTPException(status_code=404, detail="Tarea no encontrada.")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agency_task SET last_run_at = NOW() WHERE id = %s",
            (config_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "config_id": config_id}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint legado — clientes reportan eventos (éxito O error)
# ─────────────────────────────────────────────────────────────────────────────
_LEGACY_DETAIL_MAX = 4000


def _scope_secret_values(company_id: Optional[int], group_id: int) -> List[str]:
    """Credenciales del alcance, para quitarlas del detalle si vinieran en una traza."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT warehouse_host, warehouse_database, warehouse_username, warehouse_password
               FROM client_group WHERE id = %s""",
            (group_id,),
        )
        vals = list(cur.fetchone() or [])
        if company_id:
            cur.execute(
                """SELECT source_host, source_database, source_username, source_password, source_dsn
                   FROM company WHERE id = %s""",
                (company_id,),
            )
            vals += list(cur.fetchone() or [])
    finally:
        conn.close()
    out: List[str] = []
    for v in vals:
        try:
            dv = decrypt_config_secret(v or "")
        except Exception:
            dv = ""
        if dv:
            out.append(dv)
    return out


@app.post("/client-event")
def report_client_event(
    payload: dict,
    x_token: Optional[str] = Header(None),
    x_group_token: Optional[str] = Header(None),
    x_agency_token: Optional[str] = Header(None, alias="x-agency-token"),
) -> dict:
    """
    [LEGADO] Reporte de resultado de una tarea. Los agentes nuevos usan
    /agent/executions. Reglas:
      * config_id debe ser una tarea del alcance del token (si no, 403).
      * detail se sanea (sin SQL, filas, hosts ni credenciales) y se recorta.
      * No se guarda el token completo (solo prefijo + ids resueltos).

    Payload:
      config_id   : str  — ID de la tarea
      task_name   : str  — (ignorado; el nombre se resuelve en el servidor)
      event_type  : str  — "ok" | "error"
      detail      : str  — resumen si ok, traza si error (opcional)
      rows_loaded : int  — filas cargadas (solo en ok, opcional)
    """
    _require_legacy_enabled()
    if not x_token and not x_group_token and not x_agency_token:
        raise HTTPException(
            status_code=401,
            detail="Falta x-token, x-group-token o x-agency-token.",
        )
    principal = resolve_legacy(x_token or "", x_group_token or "", x_agency_token or "")
    if not principal:
        raise HTTPException(status_code=401, detail="Token no válido.")

    event_type = str(payload.get("event_type", "error"))
    if event_type not in ("ok", "error"):
        raise HTTPException(status_code=422, detail="event_type debe ser 'ok' o 'error'.")
    config_id = str(payload.get("config_id", "")).strip()
    if not config_id.isdigit() or len(config_id) > 9:
        raise HTTPException(status_code=422, detail="config_id no válido.")
    try:
        rows_loaded = max(0, min(int(payload.get("rows_loaded", 0) or 0), 2_147_483_647))
    except (TypeError, ValueError):
        rows_loaded = 0

    task = _resolve_task_access(int(config_id), x_token, x_group_token, x_agency_token)
    if not task:
        raise HTTPException(status_code=403, detail="La tarea no pertenece al alcance del token.")

    raw_detail = str(payload.get("detail", "") or "")[:16_384]  # recorte ANTES de sanear
    detail = redact_text(
        raw_detail,
        secrets=_scope_secret_values(task["company_id"], task["group_id"])
        + [x_token or "", x_group_token or "", x_agency_token or ""],
        max_len=_LEGACY_DETAIL_MAX,
    )

    # Los eventos "ok" se auto-reconocen (no generan alerta pendiente).
    acknowledged = 1 if event_type == "ok" else 0
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO client_events
                    (token, group_name, company_name, config_id, task_name, event_type, detail,
                     rows_loaded, is_acknowledged, source, auth_kind, group_id, company_id, agency_id, task_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'legacy', %s, %s, %s, %s, %s)
                """,
                (
                    token_prefix(_active_legacy_token(x_token or "", x_group_token or "", x_agency_token or "")),
                    task["group_name"],
                    task["company_name"],
                    config_id,
                    f"{task['group_name']} | {task['company_name']} | {task['agency_name']}"[:512],
                    event_type,
                    detail,
                    rows_loaded,
                    acknowledged,
                    principal["kind"],
                    task["group_id"],
                    task["company_id"],
                    task["agency_id"],
                    task["config_id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except psycopg2.Error as exc:
        server_log.error("No se pudo registrar client_event: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Error interno al registrar el evento.")

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints — monitor
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/monitor/events")
def get_events(
    x_monitor_token: str = Header(...),
    event_type: Optional[str] = Query(None, description="'ok' | 'error' | None = todos"),
    only_unacknowledged: bool = Query(False),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """Historial de eventos con grupo + razón social en cada registro."""
    _require_monitor_token(x_monitor_token)

    conditions: List[str] = []
    params: list = []

    if event_type in ("ok", "error"):
        conditions.append("event_type = %s")
        params.append(event_type)
    if only_unacknowledged:
        conditions.append("is_acknowledged = 0")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, created_at, group_name AS grupo, company_name AS razon_social, config_id,
                   task_name, event_type, detail, rows_loaded, is_acknowledged AS acknowledged
            FROM client_events
            {where}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params + [limit],
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    items = [
        {
            "id":           r[0],
            "timestamp":    r[1].isoformat() if r[1] else "",
            "grupo":        r[2],
            "razon_social": r[3],
            "config_id":    r[4],
            "task_name":    r[5],
            "event_type":   r[6],
            "detail":       r[7],
            "rows_loaded":  r[8],
            "acknowledged": bool(r[9]),
        }
        for r in rows
    ]
    return {"total": len(items), "items": items}


@app.get("/monitor/clients")
def get_clients_status(x_monitor_token: str = Header(...)) -> dict:
    """
    Resumen por empresa: grupo, RS, última conexión, ejecuciones totales,
    errores pendientes y errores en la última hora. Agrupa por ids resueltos
    (company_id), así cuenta también lo reportado con token de grupo/agencia o
    por instalaciones. Las instalaciones se ven en /monitor/installations.
    """
    _require_monitor_token(x_monitor_token)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                g.name                                                  AS grupo,
                c.name                                                  AS razon_social,
                c.company_token                                         AS token,
                c.is_enabled                                            AS rs_enabled,
                g.is_enabled                                            AS grupo_enabled,
                al.last_seen, COALESCE(al.requests_total, 0), COALESCE(al.http_errors_total, 0),
                COALESCE(al.http_errors_1h, 0),
                COALESCE(ce.executions_total, 0), COALESCE(ce.exec_errors_total, 0),
                COALESCE(ce.exec_errors_pending, 0), COALESCE(ce.exec_errors_1h, 0),
                ce.last_execution,
                -- Mismas horas como timestamptz (UTC): las columnas legadas son TIMESTAMP
                -- sin zona en la zona de la sesión de la BD.
                al.last_seen::timestamptz, ce.last_execution::timestamptz
            FROM company c
            JOIN client_group g ON g.id = c.group_id
            LEFT JOIN (
                SELECT company_id,
                       MAX(created_at) AS last_seen,
                       COUNT(*) AS requests_total,
                       SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS http_errors_total,
                       SUM(CASE WHEN created_at >= NOW() - INTERVAL '1 hour' AND status_code >= 400
                                THEN 1 ELSE 0 END) AS http_errors_1h
                FROM activity_log
                WHERE company_id IS NOT NULL AND auth_kind NOT IN ('admin', 'monitor')
                GROUP BY company_id
            ) al ON al.company_id = c.id
            LEFT JOIN (
                SELECT company_id,
                       COUNT(*) AS executions_total,
                       SUM(CASE WHEN event_type = 'error' THEN 1 ELSE 0 END) AS exec_errors_total,
                       SUM(CASE WHEN event_type = 'error' AND is_acknowledged = 0 THEN 1 ELSE 0 END) AS exec_errors_pending,
                       SUM(CASE WHEN event_type = 'error' AND created_at >= NOW() - INTERVAL '1 hour'
                                THEN 1 ELSE 0 END) AS exec_errors_1h,
                       MAX(created_at) AS last_execution
                FROM client_events
                WHERE company_id IS NOT NULL
                GROUP BY company_id
            ) ce ON ce.company_id = c.id
            ORDER BY g.name, c.name
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    items = [
        {
            "grupo":               r[0],
            "razon_social":        r[1],
            "token_preview":       (r[2] or "")[:8] + "...",
            "rs_enabled":          bool(r[3]),
            "grupo_enabled":       bool(r[4]),
            "last_seen":           r[5].isoformat()  if r[5]  else None,
            "requests_total":     int(r[6]  or 0),
            "http_errors_total":  int(r[7]  or 0),
            "http_errors_1h":     int(r[8]  or 0),
            "executions_total":   int(r[9]  or 0),
            "exec_errors_total":  int(r[10] or 0),
            "exec_errors_pending": int(r[11] or 0),
            "exec_errors_1h":     int(r[12] or 0),
            "last_execution":     r[13].isoformat() if r[13] else None,
            # Aditivos (compatibles): ISO UTC con zona, para mostrar la hora con zona explícita.
            "last_seen_utc":      r[14].astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if r[14] else None,
            "last_execution_utc": r[15].astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if r[15] else None,
        }
        for r in rows
    ]
    return {"clients": items}


@app.get("/monitor/activity")
def get_activity_log(
    x_monitor_token: str = Header(...),
    only_errors: bool = Query(True),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """Log de actividad HTTP del backend con grupo + RS."""
    _require_monitor_token(x_monitor_token)

    where = "WHERE status_code >= 400" if only_errors else ""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, created_at, group_name AS grupo, company_name AS razon_social, token,
                   method, endpoint, status_code, response_ms,
                   error_detail, client_ip, auth_kind, installation_id
            FROM activity_log
            {where}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    items = [
        {
            "id":           r[0],
            "timestamp":    r[1].isoformat() if r[1] else "",
            "grupo":        r[2],
            "razon_social": r[3],
            "token":        (r[4] or "")[:8] + "...",
            "method":       r[5],
            "endpoint":     r[6],
            "status_code":  r[7],
            "response_ms":  r[8],
            "error_detail": r[9],
            "client_ip":    r[10],
            "auth_kind":    r[11],
            "installation_id": str(r[12]) if r[12] else None,
        }
        for r in rows
    ]
    return {"total": len(items), "items": items}


@app.put("/monitor/events/{event_id}/ack")
def acknowledge_event(event_id: int, x_monitor_token: str = Header(...)) -> dict:
    _require_monitor_token(x_monitor_token)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE client_events SET is_acknowledged = 1 WHERE id = %s",
            (event_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Evento no encontrado.")
    finally:
        conn.close()
    return {"status": "ok", "event_id": event_id}


@app.put("/monitor/events/ack-all")
def acknowledge_all_events(x_monitor_token: str = Header(...)) -> dict:
    _require_monitor_token(x_monitor_token)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE client_events SET is_acknowledged = 1 "
            "WHERE is_acknowledged = 0 AND event_type = 'error'"
        )
        conn.commit()
        affected = cur.rowcount
    finally:
        conn.close()
    return {"status": "ok", "acknowledged": affected}


# ─────────────────────────────────────────────────────────────────────────────
# Administración (/admin/*) y CORS
# ─────────────────────────────────────────────────────────────────────────────
from admin_postgres import create_admin_router  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """
    En /admin/* no se devuelve el valor recibido (`input`/`ctx`) en los errores
    422: podría ser una contraseña y el middleware guarda el cuerpo del error
    en activity_log. El resto de endpoints conserva la respuesta estándar.
    """
    # En NINGUNA ruta se devuelve el valor recibido (podría ser un secreto o un
    # texto enorme que luego se sanea y registra).
    errors = [
        {"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


# Salud, incidencias y notificaciones (sección 18 de DWH_README.md)
from health_postgres import (  # noqa: E402
    HealthSettings, IncidentEngine, NotificationSettings, create_health_router,
)

HEALTH_ENGINE = IncidentEngine(
    get_connection=get_connection,
    settings=HealthSettings.from_ini(_ini),
    notif=NotificationSettings.from_ini(_ini),
    get_secret_cipher=get_secret_cipher,
    decrypt_config_secret=decrypt_config_secret,
)


def _on_watermark_reset(cur: Any, task_id: int) -> None:
    HEALTH_ENGINE.on_watermark_reset(cur, task_id)


app.include_router(
    create_admin_router(
        get_connection=get_connection,
        get_secret_cipher=get_secret_cipher,
        decrypt_config_secret=decrypt_config_secret,
        admin_token=ADMIN_TOKEN,
        group_token_column_exists=group_token_column_exists,
        agency_token_column_exists=agency_token_column_exists,
        on_watermark_reset=_on_watermark_reset,
    )
)
app.include_router(create_health_router(engine=HEALTH_ENGINE, admin_token=ADMIN_TOKEN))

_agent_router, _agent_admin_router, _agent_monitor_router = create_agent_routers(
    get_connection=get_connection,
    decrypt_config_secret=decrypt_config_secret,
    admin_token=ADMIN_TOKEN,
    monitor_token=MONITOR_TOKEN,
    config_max_age_seconds=AGENT_CONFIG_MAX_AGE,
    rotation_grace_seconds=AGENT_ROTATION_GRACE,
    heartbeat_retention_days=AGENT_HEARTBEAT_RETENTION_DAYS,
    download_log_retention_days=AGENT_DOWNLOAD_LOG_RETENTION_DAYS,
    future_tolerance_hours=AGENT_FUTURE_TOLERANCE_HOURS,
    health=HEALTH_ENGINE,
)
app.include_router(_agent_router)
app.include_router(_agent_admin_router)
app.include_router(_agent_monitor_router)


@app.on_event("startup")
def _start_health_threads() -> None:
    # Evaluador de salud y notificador en hilos del proceso (advisory lock en BD:
    # varias réplicas no evalúan a la vez; el outbox se reclama con SKIP LOCKED).
    HEALTH_ENGINE.start()


@app.on_event("shutdown")
def _stop_health_threads() -> None:
    HEALTH_ENGINE.stop()


def check_migrations() -> None:
    """Al arrancar: aplica (auto_migrate) o avisa de migraciones pendientes."""
    try:
        import migrate as _migrate

        conn = get_connection()
        try:
            if AUTO_MIGRATE:
                _migrate.run_migrations(conn, log=server_log.info)
            pending = _migrate.pending_migrations(conn)
        finally:
            conn.close()
        if pending:
            server_log.error(
                "Hay migraciones PENDIENTES en la BD de configuración: %s. "
                "Ejecuta: python migrate.py (o [database] auto_migrate = true).",
                ", ".join(pending),
            )
    except Exception as exc:
        server_log.error("No se pudo verificar migraciones: %s", type(exc).__name__)


class BodySizeLimitMiddleware:
    """
    Límite de tamaño del cuerpo (ASGI puro, externo al resto): 413 si el
    Content-Length lo supera o si el cuerpo recibido en streaming lo supera.
    /agent/enroll (sin autenticar) tiene el límite más bajo.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _limit(path: str) -> int:
        if path == "/agent/enroll":
            return ENROLL_MAX_BODY_BYTES
        if path.startswith("/agent/"):
            return AGENT_MAX_BODY_BYTES
        return MAX_BODY_BYTES

    @staticmethod
    async def _reject(send: Any) -> None:
        body = b'{"detail":{"code":"body_too_large","message":"Cuerpo de la peticion demasiado grande."}}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope.get("path", ""))
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    if int(v) > limit:
                        await self._reject(send)
                        return
                except ValueError:
                    await self._reject(send)
                    return
        state = {"received": 0, "exceeded": False, "started": False}

        async def limited_receive() -> Any:
            msg = await receive()
            if msg.get("type") == "http.request":
                state["received"] += len(msg.get("body", b""))
                if state["received"] > limit:
                    state["exceeded"] = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return msg

        async def guarded_send(msg: Any) -> None:
            if state["exceeded"]:
                if msg.get("type") == "http.response.start" and not state["started"]:
                    state["started"] = True
                    await self._reject(send)
                return  # se descarta la respuesta original
            if msg.get("type") == "http.response.start":
                state["started"] = True
            await send(msg)

        await self.app(scope, limited_receive, guarded_send)


app.add_middleware(BodySizeLimitMiddleware)


# Solo si hay orígenes configurados. Se agrega al final para que quede como
# middleware más externo.
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "x-admin-token", "x-monitor-token"],
        # /agent/* no se expone a navegadores (sin CORS para x-installation-*).
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nexus Config API Server (PostgreSQL)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interfaz de escucha. Usa 0.0.0.0 para aceptar conexiones externas.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"Nexus Server (PostgreSQL) iniciando en {args.host}:{args.port} ...")
    check_migrations()
    uvicorn.run(app, host=args.host, port=args.port, h11_max_incomplete_event_size=65_536)
