"""
nexus_server_postgres.py  v3 — PostgreSQL
──────────────────────────────────────────
Versión del servidor Nexus que usa PostgreSQL en lugar de MySQL.
El token de cada cliente identifica una razón social.
El grupo se resuelve automáticamente vía JOIN en cada petición.

Endpoints clientes ETL:
  GET  /configs                  → todas las tareas de una company (x-token = company_token)
  GET  /agency-configs           → solo tareas de una agency (x-agency-token)
  GET  /group-configs            → tareas de todas las companies del grupo (x-group-token)
  PUT  /configs/{id}/last_run    → marcar tarea ejecutada (mismos headers que el GET correspondiente)

Endpoints monitor (requieren header x-monitor-token):
  POST /client-event             → clientes reportan eventos (éxito o error)
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
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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

_ini = configparser.ConfigParser()
_ini.read(os.path.join(_APP_DIR, "config.ini"))

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
# Resolver identidad completa desde token
# ─────────────────────────────────────────────────────────────────────────────
def resolve_request_identity(
    company_token: str = "",
    group_token: str = "",
    agency_token: str = "",
) -> Tuple[str, str]:
    """
    Devuelve (company_name, group_name) para enriquecer activity_log.
    Usado por el middleware para enriquecer el activity_log.
    """
    if not company_token and not group_token and not agency_token:
        return ("", "")
    try:
        if group_token and group_token_column_exists():
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT '' AS company_name, g.name AS group_name
                    FROM client_group g
                    WHERE g.group_token = %s
                    """,
                    (group_token,),
                )
                row = cur.fetchone()
                return (row[0], row[1]) if row else ("", "")
            finally:
                conn.close()

        if agency_token and agency_token_column_exists():
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT c.name AS company_name, g.name AS group_name
                    FROM agency a
                    JOIN company c ON c.id = a.company_id
                    JOIN client_group g ON g.id = c.group_id
                    WHERE a.agency_token = %s
                    """,
                    (agency_token,),
                )
                row = cur.fetchone()
                return (row[0], row[1]) if row else ("", "")
            finally:
                conn.close()

        if company_token:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT c.name AS company_name, g.name AS group_name
                    FROM company c
                    JOIN client_group g ON g.id = c.group_id
                    WHERE c.company_token = %s
                    """,
                    (company_token,),
                )
                row = cur.fetchone()
                return (row[0], row[1]) if row else ("", "")
            finally:
                conn.close()

        return ("", "")
    except Exception:
        return ("", "")


# ─────────────────────────────────────────────────────────────────────────────
# Middleware – activity_log
# ─────────────────────────────────────────────────────────────────────────────
def _log_activity(
    token: str, razon_social: str, grupo: str,
    method: str, endpoint: str, status_code: int,
    response_ms: int, error_detail: Optional[str], client_ip: str,
) -> None:
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO activity_log
                    (token, company_name, group_name, method, endpoint,
                     status_code, response_ms, error_detail, client_ip)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (token, razon_social, grupo, method, endpoint,
                 status_code, response_ms, error_detail, client_ip),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


@app.middleware("http")
async def activity_logging_middleware(request: Request, call_next):
    start     = time.time()
    company_token = request.headers.get("x-token", "")
    group_token = request.headers.get("x-group-token", "")
    agency_token = request.headers.get("x-agency-token", "")
    request_token = group_token or agency_token or company_token
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
            error_detail = body.decode("utf-8", errors="replace")
            from starlette.responses import Response as RawResponse
            response = RawResponse(
                content=body, status_code=status_code,
                headers=dict(response.headers), media_type=response.media_type,
            )
        return response

    except Exception:
        status_code  = 500
        error_detail = traceback.format_exc()
        return JSONResponse(status_code=500, content={"detail": "Error interno."})

    finally:
        elapsed_ms = int((time.time() - start) * 1000)
        # Las validaciones de sesión del panel (/admin/whoami OK) no se registran.
        if not (endpoint == "/admin/whoami" and status_code < 400):
            company_name, group_name = resolve_request_identity(
                company_token, group_token, agency_token
            )
            _log_activity(
                request_token, company_name, group_name, method, endpoint,
                status_code, elapsed_ms, error_detail, client_ip,
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
    if token != MONITOR_TOKEN:
        raise HTTPException(status_code=401, detail="Token de monitor no válido.")


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
    conn = get_connection()
    try:
        cur = conn.cursor()
        if agency_token and agency_token_column_exists():
            cur.execute(
                """
                SELECT at.id, c.name AS company_name, g.name AS group_name
                FROM agency_task at
                JOIN agency a ON a.id = at.agency_id
                JOIN company c ON c.id = a.company_id
                JOIN client_group g ON g.id = c.group_id
                WHERE at.id = %s AND a.agency_token = %s
                """,
                (config_id, agency_token),
            )
        elif company_token:
            cur.execute(
                """
                SELECT at.id, c.name AS company_name, g.name AS group_name
                FROM agency_task at
                JOIN agency a ON a.id = at.agency_id
                JOIN company c ON c.id = a.company_id
                JOIN client_group g ON g.id = c.group_id
                WHERE at.id = %s AND c.company_token = %s
                """,
                (config_id, company_token),
            )
        elif group_token and group_token_column_exists():
            cur.execute(
                """
                SELECT at.id, c.name AS company_name, g.name AS group_name
                FROM agency_task at
                JOIN agency a ON a.id = at.agency_id
                JOIN company c ON c.id = a.company_id
                JOIN client_group g ON g.id = c.group_id
                WHERE at.id = %s AND g.group_token = %s
                """,
                (config_id, group_token),
            )
        else:
            return None
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None
    return {"config_id": row[0], "company_name": row[1], "group_name": row[2]}


@app.put("/configs/{config_id}/last_run")
def update_last_run(
    config_id: int,
    x_token: Optional[str] = Header(None),
    x_group_token: Optional[str] = Header(None),
    x_agency_token: Optional[str] = Header(None, alias="x-agency-token"),
) -> dict:
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
# Endpoint — clientes reportan eventos (éxito O error)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/client-event")
def report_client_event(
    payload: dict,
    x_token: Optional[str] = Header(None),
    x_group_token: Optional[str] = Header(None),
    x_agency_token: Optional[str] = Header(None, alias="x-agency-token"),
) -> dict:
    """
    Cada vez que un cliente termina de ejecutar una tarea (bien o mal),
    reporta aquí. El backend resuelve grupo + RS desde el token y los guarda.

    Payload:
      config_id   : str  — ID de la tarea
      task_name   : str  — nombre descriptivo (grupo | RS | agencia)
      event_type  : str  — "ok" | "error"
      detail      : str  — resumen si ok, traceback si error (opcional)
      rows_loaded : int  — filas cargadas (solo en ok, opcional)
    """
    if not x_token and not x_group_token and not x_agency_token:
        raise HTTPException(
            status_code=401,
            detail="Falta x-token, x-group-token o x-agency-token.",
        )

    event_type  = str(payload.get("event_type", "error"))
    config_id   = str(payload.get("config_id", ""))
    task_name   = str(payload.get("task_name", ""))
    detail      = str(payload.get("detail", ""))
    rows_loaded = int(payload.get("rows_loaded", 0))

    # Los eventos "ok" se auto-reconocen (no generan alerta pendiente).
    # Los eventos "error" quedan acknowledged=0 hasta que el monitor los revise.
    acknowledged = 1 if event_type == "ok" else 0

    task_access = None
    if config_id.isdigit():
        task_access = _resolve_task_access(
            int(config_id), x_token, x_group_token, x_agency_token
        )

    if x_group_token:
        group_state = resolve_group_token(x_group_token)
        if not group_state["found"]:
            raise HTTPException(status_code=401, detail="Group token no válido.")
        group_name = task_access["group_name"] if task_access else group_state["group_name"]
        company_name = task_access["company_name"] if task_access else ""
        request_token = x_group_token
    elif x_agency_token:
        if not agency_token_column_exists():
            raise HTTPException(status_code=503, detail="Columna agency_token no disponible.")
        ag_state = resolve_agency_token(x_agency_token)
        if not ag_state["found"]:
            raise HTTPException(status_code=401, detail="Agency token no válido.")
        group_name = ag_state["group_name"]
        company_name = ag_state["company_name"]
        request_token = x_agency_token
    else:
        token_state = resolve_company_token(x_token or "")
        if not token_state["found"]:
            raise HTTPException(status_code=401, detail="Token no válido.")
        group_name = token_state["group_name"]
        company_name = token_state["company_name"]
        request_token = x_token or ""

    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO client_events
                    (token, group_name, company_name, config_id,
                     task_name, event_type, detail, rows_loaded, is_acknowledged)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    request_token,
                    group_name,
                    company_name,
                    config_id,
                    task_name,
                    event_type,
                    detail,
                    rows_loaded,
                    acknowledged,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

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
    Resumen por cliente: grupo, RS, última conexión,
    ejecuciones totales, errores pendientes y errores en la última hora.
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
                MAX(al.created_at)                                      AS last_seen,
                COUNT(al.id)                                            AS requests_total,
                COALESCE(SUM(CASE WHEN al.status_code >= 400 THEN 1 ELSE 0 END), 0) AS http_errors_total,
                COALESCE(SUM(CASE WHEN al.created_at >= NOW() - INTERVAL '1 hour'
                    AND al.status_code >= 400 THEN 1 ELSE 0 END), 0)    AS http_errors_1h,
                (SELECT COUNT(*)
                 FROM client_events ce
                 WHERE ce.token = c.company_token)                     AS executions_total,
                (SELECT COUNT(*)
                 FROM client_events ce
                 WHERE ce.token = c.company_token
                   AND ce.event_type = 'error')                         AS exec_errors_total,
                (SELECT COUNT(*)
                 FROM client_events ce
                 WHERE ce.token = c.company_token
                   AND ce.event_type = 'error'
                   AND ce.is_acknowledged = 0)                          AS exec_errors_pending,
                (SELECT COUNT(*)
                 FROM client_events ce
                 WHERE ce.token = c.company_token
                   AND ce.event_type = 'error'
                   AND ce.created_at >= NOW() - INTERVAL '1 hour')      AS exec_errors_1h,
                (SELECT MAX(ce.created_at)
                 FROM client_events ce
                 WHERE ce.token = c.company_token)                      AS last_execution
            FROM company c
            JOIN client_group g ON g.id = c.group_id
            LEFT JOIN activity_log al ON al.token = c.company_token
            GROUP BY c.id, c.name, c.company_token, c.is_enabled,
                     g.id, g.name, g.is_enabled
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
                   error_detail, client_ip
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
from fastapi.exception_handlers import request_validation_exception_handler  # noqa: E402


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """
    En /admin/* no se devuelve el valor recibido (`input`/`ctx`) en los errores
    422: podría ser una contraseña y el middleware guarda el cuerpo del error
    en activity_log. El resto de endpoints conserva la respuesta estándar.
    """
    if not request.url.path.startswith("/admin"):
        return await request_validation_exception_handler(request, exc)
    errors = [
        {"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


app.include_router(
    create_admin_router(
        get_connection=get_connection,
        get_secret_cipher=get_secret_cipher,
        decrypt_config_secret=decrypt_config_secret,
        admin_token=ADMIN_TOKEN,
        group_token_column_exists=group_token_column_exists,
        agency_token_column_exists=agency_token_column_exists,
    )
)

# Solo si hay orígenes configurados. Se agrega al final para que quede como
# middleware más externo.
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "x-admin-token", "x-monitor-token"],
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
    uvicorn.run(app, host=args.host, port=args.port)
