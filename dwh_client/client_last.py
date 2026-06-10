"""
nexus_client_postgres.py  v4 — PostgreSQL
───────────────────────────────────────────
Cliente ETL que carga datos en PostgreSQL (DWH destino).
Lee su configuración del backend usando token de razón social, grupo o agencia.
Reporta cada ejecución (exitosa o fallida) al backend.
Origen: MySQL, SQL Server, PostgreSQL o Firebird (según origen_tipo en config).
Destino: PostgreSQL (psycopg2).

Cambios v4:
- Soporte para schema en load_table (p. ej. "dwh.carter")
- Soporte para origen Firebird (vía DSN ODBC o conexión directa con fdb)
"""

import configparser
import glob
import logging
import os
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import io

import psycopg2
import psycopg2.extras
import pymysql
import pyodbc
import requests


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))


def load_ini() -> configparser.ConfigParser:
    ini_path = os.path.join(_APP_DIR, "config.ini")
    if not os.path.exists(ini_path):
        print(f"No se encontró config.ini en: {ini_path}")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(ini_path)
    return cfg


_ini = load_ini()
API_BASE_URL = _ini.get("nexus", "api_url", fallback="http://127.0.0.1:8000").strip()
COMPANY_API_TOKEN = _ini.get("nexus", "token", fallback="").strip()
GROUP_API_TOKEN = _ini.get("nexus", "group_token", fallback="").strip()
AGENCY_API_TOKEN = _ini.get("nexus", "agency_token", fallback="").strip()

if not COMPANY_API_TOKEN and not GROUP_API_TOKEN and not AGENCY_API_TOKEN:
    print(
        "Falta al menos uno de: [nexus] token, [nexus] group_token, [nexus] agency_token en config.ini"
    )
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Logging  — archivo diario en ./logs/  (máx 7 archivos)
# ─────────────────────────────────────────────────────────────────────────────
_LOGS_DIR    = os.path.join(_APP_DIR, "logs")
_MAX_LOGS    = 7
os.makedirs(_LOGS_DIR, exist_ok=True)
LOG_PATH     = os.path.join(_LOGS_DIR, f"nexus_{date.today().isoformat()}.log")


def _cleanup_old_logs() -> None:
    files = sorted(glob.glob(os.path.join(_LOGS_DIR, "nexus_*.log")))
    while len(files) > _MAX_LOGS:
        try:
            os.remove(files.pop(0))
        except OSError:
            pass


_cleanup_old_logs()

log = logging.getLogger("nexus")
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
log.addHandler(_fh)
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)
log.addHandler(_ch)


# ─────────────────────────────────────────────────────────────────────────────
# Estado global
# ─────────────────────────────────────────────────────────────────────────────
_verbose: bool = False
_warehouse_config: Dict[str, Any] = {}
_default_source_config: Dict[str, Any] = {}


def is_group_mode() -> bool:
    return bool(GROUP_API_TOKEN)


def is_agency_mode() -> bool:
    """Una sola agencia: prioridad por debajo de group_token."""
    return bool(AGENCY_API_TOKEN) and not is_group_mode()


def _client_mode_label() -> str:
    if is_group_mode():
        return "group"
    if is_agency_mode():
        return "agency"
    return "company"


def _mask_active_client_token() -> str:
    if is_group_mode():
        return _mask_secret(GROUP_API_TOKEN)
    if is_agency_mode():
        return _mask_secret(AGENCY_API_TOKEN)
    return _mask_secret(COMPANY_API_TOKEN)


def _mask_secret(secret: str) -> str:
    if not secret:
        return "(empty)"
    if len(secret) <= 6:
        return "*" * len(secret)
    return f"{secret[:3]}...{secret[-2:]}"


def validate_api_url_security() -> None:
    parsed = urlparse(API_BASE_URL)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return
    if host in {"127.0.0.1", "localhost"}:
        return
    log.warning(
        "API URL sin HTTPS para host no local. Usa TLS en producción: %s",
        API_BASE_URL,
    )


def build_api_headers() -> Dict[str, str]:
    if is_group_mode():
        return {"x-group-token": GROUP_API_TOKEN}
    if is_agency_mode():
        return {"x-agency-token": AGENCY_API_TOKEN}
    return {"x-token": COMPANY_API_TOKEN}


def redact_sensitive_text(text: str, extra_values: Optional[Sequence[str]] = None) -> str:
    redacted = text or ""
    sensitive_values = [
        COMPANY_API_TOKEN,
        GROUP_API_TOKEN,
        AGENCY_API_TOKEN,
        _warehouse_config.get("pass", ""),
        _default_source_config.get("pass", ""),
    ]
    if extra_values:
        sensitive_values.extend(extra_values)
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, "***REDACTED***")
    return redacted


def _empty_runtime_config() -> Dict[str, Any]:
    return {
        "status": "unreachable",
        "log_verbose": False,
        "refresh_seconds": 60,
        "configs": [],
        "dwh": {},
        "source": {},
    }


def _normalize_source_config(data: Dict[str, Any]) -> Dict[str, Any]:
    tipo = (data.get("origen_tipo") or data.get("source_type") or "sqlserver").lower()
    default_port = 3050 if tipo == "firebird" else 1433
    return {
        "tipo": tipo,
        "ip": data.get("origen_ip") or data.get("source_host") or "",
        "port": data.get("origen_port") or data.get("source_port") or default_port,
        "db": data.get("origen_db") or data.get("source_db") or data.get("source_database") or "",
        "user": data.get("origen_user") or data.get("source_user") or data.get("source_username") or "",
        "pass": data.get("origen_pass") or data.get("source_pass") or data.get("source_password") or "",
        "dsn_odbc": data.get("dsn_odbc") or data.get("source_dsn") or "",
    }


def _normalize_warehouse_config(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "host": data.get("dwh_host") or data.get("warehouse_host") or "",
        "port": data.get("dwh_port") or data.get("warehouse_port") or 5432,
        "db": data.get("dwh_db") or data.get("warehouse_db") or data.get("warehouse_database") or "",
        "user": data.get("dwh_user") or data.get("warehouse_user") or data.get("warehouse_username") or "",
        "pass": data.get("dwh_pass") or data.get("warehouse_pass") or data.get("warehouse_password") or "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# API — obtener configuraciones
# ─────────────────────────────────────────────────────────────────────────────
def fetch_runtime_config() -> Dict[str, Any]:
    if is_group_mode():
        endpoint = "/group-configs"
    elif is_agency_mode():
        endpoint = "/agency-configs"
    else:
        endpoint = "/configs"
    url = f"{API_BASE_URL}{endpoint}"
    headers = build_api_headers()
    empty = _empty_runtime_config()

    try:
        r = requests.get(url, headers=headers, timeout=10)
    except requests.ConnectionError:
        return empty

    if r.status_code == 401:
        return {**empty, "status": "invalid"}
    if r.status_code == 403:
        log.warning("Servicio deshabilitado: %s", r.json().get("detail", ""))
        return {**empty, "status": "disabled"}

    r.raise_for_status()
    data = r.json()
    runtime = {
        "status": "ok",
        "log_verbose": data.get("log_verbose", False),
        "refresh_seconds": data.get("refresh_seconds", 60),
        "configs": data.get("configs", []),
        "dwh": _normalize_warehouse_config(data),
        "source": _normalize_source_config(data),
    }
    if is_group_mode():
        for task in runtime["configs"]:
            if "source" in task and isinstance(task["source"], dict):
                task["source"] = _normalize_source_config(task["source"])
    return runtime


# ─────────────────────────────────────────────────────────────────────────────
# API — reportar evento al backend (éxito o error)
# ─────────────────────────────────────────────────────────────────────────────
def send_client_event(
    config_id: str,
    task_name: str,
    event_type: str,          # "ok" | "error"
    detail: str = "",
    rows_loaded: int = 0,
) -> None:
    url     = f"{API_BASE_URL}/client-event"
    headers = build_api_headers()
    payload = {
        "config_id":   config_id,
        "task_name":   task_name,
        "event_type":  event_type,
        "detail":      redact_sensitive_text(detail),
        "rows_loaded": rows_loaded,
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code != 200:
            log.warning("No se pudo reportar evento al backend (status %d).", r.status_code)
    except requests.ConnectionError:
        log.warning("No se pudo reportar evento al backend (sin conexión).")


# ─────────────────────────────────────────────────────────────────────────────
# API — notificar last_run_at
# ─────────────────────────────────────────────────────────────────────────────
def mark_task_last_run(config_id: str) -> None:
    url     = f"{API_BASE_URL}/configs/{config_id}/last_run"
    headers = build_api_headers()
    try:
        r = requests.put(url, headers=headers, timeout=10)
        if r.status_code != 200:
            log.warning("No se pudo actualizar last_run_at para tarea %s.", config_id)
    except requests.ConnectionError:
        log.warning("No se pudo notificar last_run_at (sin conexión).")


# ─────────────────────────────────────────────────────────────────────────────
# Conexiones — Origen (MySQL, SQL Server, PostgreSQL o Firebird)
# ─────────────────────────────────────────────────────────────────────────────
def _detect_sql_server_driver() -> str:
    available = pyodbc.drivers()
    for name in [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    ]:
        if name in available:
            return name
    return "ODBC Driver 17 for SQL Server"


def _build_sqlserver_conn_str(source_config: Dict[str, Any]) -> str:
    dsn = source_config.get("dsn_odbc", "").strip()
    if dsn:
        parts = [f"DSN={dsn}"]
        if source_config.get("user"):
            parts.append(f"UID={source_config['user']}")
        if source_config.get("pass"):
            parts.append(f"PWD={source_config['pass']}")
        return ";".join(parts)

    if not source_config.get("ip"):
        raise ValueError("No hay IP de origen configurada ni DSN ODBC.")

    driver = _detect_sql_server_driver()
    server = source_config["ip"]
    port   = source_config.get("port", 1433)
    if port and port != 1433:
        server = f"{server},{port}"

    return (
        f"DRIVER={{{driver}}};SERVER={server};"
        f"DATABASE={source_config['db']};"
        f"UID={source_config['user']};PWD={source_config['pass']};"
        f"TrustServerCertificate=yes"
    )


@contextmanager
def _source_connection(source_config: Optional[Dict[str, Any]] = None):
    cfg = source_config or _default_source_config
    tipo = cfg.get("tipo", "sqlserver") or "sqlserver"

    if tipo == "mysql":
        if not cfg.get("ip"):
            raise ValueError("No hay host de origen MySQL configurado.")
        conn = pymysql.connect(
            host=cfg["ip"],
            port=int(cfg.get("port", 3306)),
            user=cfg["user"],
            password=cfg["pass"],
            database=cfg["db"],
            charset="utf8mb4",
        )

    elif tipo == "sqlserver":
        conn = pyodbc.connect(_build_sqlserver_conn_str(cfg))

    elif tipo == "pervasive":
        conn = pyodbc.connect(_build_sqlserver_conn_str(cfg))
        conn.setdecoding(pyodbc.SQL_CHAR,  encoding="latin-1")
        conn.setdecoding(pyodbc.SQL_WCHAR, encoding="latin-1")
        conn.setencoding(encoding="latin-1")

    elif tipo == "postgresql":
        if not cfg.get("ip"):
            raise ValueError("No hay host de origen PostgreSQL configurado.")
        conn = psycopg2.connect(
            host=cfg["ip"],
            port=int(cfg.get("port", 5432)),
            user=cfg["user"],
            password=cfg["pass"],
            dbname=cfg["db"],
        )

    elif tipo == "firebird":
        dsn = cfg.get("dsn_odbc", "").strip()
        if dsn:
            # Vía DSN ODBC (p. ej. ConexionMMW32prodAL configurado en Windows)
            parts = [f"DSN={dsn}"]
            if cfg.get("user"):
                parts.append(f"UID={cfg['user']}")
            if cfg.get("pass"):
                parts.append(f"PWD={cfg['pass']}")
            conn = pyodbc.connect(";".join(parts), autocommit=False)
            # Firebird suele devolver strings en latin-1
            conn.setdecoding(pyodbc.SQL_CHAR,  encoding="latin-1")
            conn.setdecoding(pyodbc.SQL_WCHAR, encoding="latin-1")
            conn.setencoding(encoding="latin-1")
        elif cfg.get("ip"):
            # Vía fdb nativo (sin DSN): requiere `pip install fdb`
            try:
                import fdb  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "Para conectar a Firebird sin DSN instala: pip install fdb"
                )
            port = int(cfg.get("port", 3050))
            conn = fdb.connect(
                host=cfg["ip"],
                port=port,
                database=cfg["db"],
                user=cfg["user"],
                password=cfg["pass"],
                charset="WIN1252",
            )
        else:
            raise ValueError(
                "Firebird requiere 'dsn_odbc' o 'source_host' configurado."
            )

    else:
        raise ValueError(
            f"origen_tipo no soportado: {tipo}. "
            f"Use: mysql, sqlserver, postgresql, pervasive, firebird"
        )

    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _dwh_connection():
    if not _warehouse_config.get("host"):
        raise ValueError("No hay host DWH configurado.")

    host = str(_warehouse_config["host"])
    port = int(_warehouse_config.get("port", 5432))
    user = str(_warehouse_config["user"])
    password = str(_warehouse_config["pass"])
    dbname = str(_warehouse_config["db"])

    dsn = (
        f"host={host} port={port} dbname={dbname} "
        f"user={user} password={password}"
    )

    try:
        conn = psycopg2.connect(dsn)
        conn.set_client_encoding("UTF8")
        log.debug("Conexión DWH establecida (client_encoding=UTF8)")
    except Exception as e:
        raise RuntimeError(f"No se pudo conectar a {host}:{port} db={dbname} user={user}: {e}") from e

    try:
        yield conn
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL helpers — schema-aware
# ─────────────────────────────────────────────────────────────────────────────
def quote_ident(name: str) -> str:
    """PostgreSQL usa comillas dobles para identificadores (columnas)."""
    return f'"{name}"'


def split_schema_table(table: str) -> Tuple[str, str]:
    """
    Separa 'dwh.carter' → ('dwh', 'carter').
    Sin punto → ('public', tabla).
    """
    if "." in table:
        schema, tbl = table.split(".", 1)
        return schema.strip(), tbl.strip()
    return "public", table.strip()


def quote_table(table: str) -> str:
    """
    Devuelve identificador PostgreSQL con schema:
      'dwh.carter'  → '"dwh"."carter"'
      'customers'   → '"public"."customers"'
    """
    schema, tbl = split_schema_table(table)
    return f'"{schema}"."{tbl}"'


def _column_suggests_pg_temporal(column: str) -> bool:
    c = column.lower()
    if c == "timestamp":
        return True
    if "timestamp_hex" in c:
        return False
    if "date" in c or "timestamp" in c or c.endswith("_at"):
        return True
    return False


def sanitize_value_for_postgres(column: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value
    if not _column_suggests_pg_temporal(column):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s or s.startswith("0000-00-00"):
            return None
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            try:
                date.fromisoformat(s[:10])
            except ValueError:
                return None
        return value
    return value


_CUSTOMERS_VARCHAR_LIMITS: Dict[str, int] = {}


def truncate_string_for_dwh_table(table: str, column: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return value
    if not isinstance(value, str):
        return value
    # Comparar solo el nombre de tabla sin schema
    _, tbl = split_schema_table(table)
    t = tbl.lower().strip()
    if t != "customers":
        return value
    key = column.lower().strip().strip('"').strip("'")
    max_len = _CUSTOMERS_VARCHAR_LIMITS.get(key)
    if max_len is None or len(value) <= max_len:
        return value
    return value[:max_len]


def prepare_cell_for_postgres(load_table: str, column: str, value: Any) -> Any:
    v = sanitize_value_for_postgres(column, value)
    v = truncate_string_for_dwh_table(load_table, column, v)
    if isinstance(v, str):
        v = v.replace("\x00", "")
    return v


def prepare_rows_for_postgres(
    load_table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> List[tuple]:
    return [
        tuple(prepare_cell_for_postgres(load_table, c, v) for c, v in zip(columns, row))
        for row in rows
    ]


def resolve_upsert_keys_to_columns(
    columns: Sequence[str], upsert_keys: Sequence[str]
) -> List[str]:
    if not columns or not upsert_keys:
        return []
    by_lower = {c.lower(): c for c in columns}
    out: List[str] = []
    seen: set = set()
    for k in upsert_keys:
        if k is None:
            continue
        s = str(k).strip()
        if not s:
            continue
        cand = s if s in columns else by_lower.get(s.lower(), "")
        if not cand or cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
    return out


def maybe_adjust_customers_load(
    load_table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    upsert_keys: Sequence[str],
) -> tuple:
    _, tbl = split_schema_table(load_table)
    if tbl.lower().strip() != "customers":
        return list(columns), [tuple(r) for r in rows], list(upsert_keys)

    cols = list(columns)
    by_lower = {c.lower(): c for c in cols}
    if "id" not in by_lower:
        return cols, [tuple(r) for r in rows], list(upsert_keys)

    uk_low = {k.lower() for k in upsert_keys}
    if "idagency" not in uk_low or "ndclientdms" not in uk_low:
        return cols, [tuple(r) for r in rows], list(upsert_keys)

    id_name = by_lower["id"]
    idx = cols.index(id_name)
    new_cols = [c for i, c in enumerate(cols) if i != idx]
    new_rows = [tuple(r[i] for i in range(len(r)) if i != idx) for r in rows]
    new_upsert = [k for k in upsert_keys if k.lower() != "id"]
    return new_cols, new_rows, new_upsert


def dedupe_rows_for_upsert(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    upsert_keys: Sequence[str],
) -> List[tuple]:
    valid_keys = resolve_upsert_keys_to_columns(columns, upsert_keys)
    if not valid_keys or not rows:
        return [tuple(r) for r in rows]
    col_index = {name: i for i, name in enumerate(columns)}
    idxs = [col_index[k] for k in valid_keys]
    merged: Dict[tuple, tuple] = {}
    for row in rows:
        key = tuple(row[i] for i in idxs)
        merged[key] = tuple(row)
    return list(merged.values())


def infer_column_types(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> Dict[str, str]:
    samples: List[Any] = [None] * len(columns)
    for row in rows:
        for i, val in enumerate(row):
            if samples[i] is None and val is not None:
                samples[i] = val
        if all(v is not None for v in samples):
            break

    type_map: Dict[str, str] = {}
    for i, col in enumerate(columns):
        val = samples[i]
        if isinstance(val, bool):         sql_type = "BOOLEAN"
        elif isinstance(val, datetime):   sql_type = "TIMESTAMP"
        elif isinstance(val, date):       sql_type = "DATE"
        elif isinstance(val, Decimal):    sql_type = "DECIMAL(18,4)"
        else:                             sql_type = "TEXT"
        type_map[col] = sql_type
    return type_map


def create_table_if_missing(
    cursor: Any, table: str,
    columns: Sequence[str], column_types: Dict[str, str],
    upsert_keys: Sequence[str],
) -> None:
    """
    Crea la tabla si no existe, respetando el schema (p. ej. dwh.carter).
    Si el origen no trae columna 'id', se añade id BIGSERIAL PRIMARY KEY.
    """
    # Asegurar que el schema exista
    schema, _ = split_schema_table(table)
    cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    has_source_id = any((c or "").lower() == "id" for c in columns)
    col_defs: List[str] = []
    if not has_source_id:
        col_defs.append("id BIGSERIAL PRIMARY KEY")

    for col in columns:
        sql_type = column_types[col]
        col_defs.append(f"{quote_ident(col)} {sql_type}")

    valid_keys = resolve_upsert_keys_to_columns(columns, upsert_keys)
    if not has_source_id:
        if valid_keys:
            uniq = ", ".join(quote_ident(k) for k in valid_keys)
            col_defs.append(f"UNIQUE ({uniq})")
    elif valid_keys:
        pk = ", ".join(quote_ident(k) for k in valid_keys)
        col_defs.append(f"PRIMARY KEY ({pk})")

    cursor.execute(
        f"CREATE TABLE IF NOT EXISTS {quote_table(table)} "
        f"({', '.join(col_defs)})"
    )


def pg_table_exists(cursor: Any, table: str) -> bool:
    """True si existe la tabla en el schema indicado (o public si no se especifica)."""
    schema, tbl = split_schema_table(table)
    cursor.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (schema.lower(), tbl.lower()),
    )
    return cursor.fetchone() is not None


def ensure_columns_exist(
    cursor: Any, table: str,
    columns: Sequence[str], col_types: Dict[str, str],
) -> None:
    """
    Verifica que la tabla PostgreSQL tenga todas las columnas necesarias.
    Si faltan, las agrega con ALTER TABLE ADD COLUMN.
    """
    schema, tbl = split_schema_table(table)
    cursor.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema.lower(), tbl.lower()),
    )
    existing = {row[0] for row in cursor.fetchall()}
    existing_lower = {n.lower() for n in existing}
    added = 0
    for col in columns:
        if col in existing or col.lower() in existing_lower:
            continue
        sql_type = col_types.get(col, "TEXT")
        cursor.execute(
            f"ALTER TABLE {quote_table(table)} "
            f"ADD COLUMN {quote_ident(col)} {sql_type}"
        )
        added += 1
        log.info("  Auto-columna añadida: %s.%s (%s)", table, col, sql_type)
    if added:
        log.info("  %d columna(s) añadida(s) a '%s'.", added, table)


def build_upsert_sql_values_template(
    table: str, columns: Sequence[str], upsert_keys: Sequence[str]
) -> str:
    """SQL para execute_values: INSERT ... VALUES %s ON CONFLICT ..."""
    cols_sql   = ", ".join(quote_ident(c) for c in columns)
    valid_keys = resolve_upsert_keys_to_columns(columns, upsert_keys)

    if not valid_keys:
        return f"INSERT INTO {quote_table(table)} ({cols_sql}) VALUES %s"

    vk_set = set(valid_keys)
    update_cols = [c for c in columns if c not in vk_set]
    conflict_cols = ", ".join(quote_ident(k) for k in valid_keys)
    if not update_cols:
        return (
            f"INSERT INTO {quote_table(table)} ({cols_sql}) VALUES %s "
            f"ON CONFLICT ({conflict_cols}) DO NOTHING"
        )
    set_clause = ", ".join(
        f"{quote_ident(c)} = EXCLUDED.{quote_ident(c)}" for c in update_cols
    )
    return (
        f"INSERT INTO {quote_table(table)} ({cols_sql}) VALUES %s "
        f"ON CONFLICT ({conflict_cols}) DO UPDATE SET {set_clause}"
    )


def build_upsert_sql(
    table: str, columns: Sequence[str], upsert_keys: Sequence[str]
) -> str:
    cols_sql     = ", ".join(quote_ident(c) for c in columns)
    placeholders = ", ".join("%s" for _ in columns)
    valid_keys   = resolve_upsert_keys_to_columns(columns, upsert_keys)

    if not valid_keys:
        return f"INSERT INTO {quote_table(table)} ({cols_sql}) VALUES ({placeholders})"

    vk_set = set(valid_keys)
    update_cols = [c for c in columns if c not in vk_set]
    conflict_cols = ", ".join(quote_ident(k) for k in valid_keys)
    if not update_cols:
        return (
            f"INSERT INTO {quote_table(table)} ({cols_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_cols}) DO NOTHING"
        )
    set_clause = ", ".join(
        f"{quote_ident(c)} = EXCLUDED.{quote_ident(c)}" for c in update_cols
    )
    return (
        f"INSERT INTO {quote_table(table)} ({cols_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_cols}) DO UPDATE SET {set_clause}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Nombres de columnas: unificar orígenes distintos (SP/vistas) sin tocar cada BD
# ─────────────────────────────────────────────────────────────────────────────
_DWH_COLUMN_SYNONYMS: Dict[str, Dict[str, str]] = {
    "services": {
        "servicer_to_performe": "service_to_perform",
        "servicer_to_performm": "service_to_perform",
        "servicer_to_perform": "service_to_perform",
        "service_to_performe": "service_to_perform",
        "km": "kms",
    },
    "invoices": {
        "client_bussines_name": "client_bussines_name",
        "client_busines_name": "client_bussines_name",
        "client_business_name": "client_bussines_name",
        "client_businness_name": "client_bussines_name",
    },
}


def canonicalize_dwh_column_names(load_table: str, columns: Sequence[str]) -> List[str]:
    # Usar solo el nombre de tabla sin schema para buscar sinónimos
    _, tbl = split_schema_table(load_table)
    t = tbl.lower().strip()
    syn = _DWH_COLUMN_SYNONYMS.get(t)
    if not syn:
        return list(columns)
    lower_to_canon = {k.lower(): v for k, v in syn.items()}
    return [lower_to_canon.get((c or "").lower(), c) for c in columns]


def merge_row_columns_if_duplicate_names(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    task_id: str = "",
) -> Tuple[List[str], List[tuple]]:
    if not columns:
        return [], [tuple(r) for r in rows]

    groups: Dict[str, List[int]] = {}
    order: List[str] = []
    for i, c in enumerate(columns):
        name = c if c is not None else ""
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(i)

    if all(len(ix) == 1 for ix in groups.values()):
        return list(columns), [tuple(r) for r in rows]

    if task_id:
        log.warning(
            "  Tarea %s: columnas homónimas tras normalizar; se fusionan por fila.",
            task_id,
        )

    new_rows: List[tuple] = []
    for row in rows:
        tup = tuple(row)
        vals: List[Any] = []
        for name in order:
            indices = groups[name]
            chosen: Any = None
            for j in reversed(indices):
                if j < len(tup) and tup[j] is not None:
                    chosen = tup[j]
                    break
            if chosen is None and indices:
                j0 = indices[0]
                chosen = tup[j0] if j0 < len(tup) else None
            vals.append(chosen)
        new_rows.append(tuple(vals))
    return order, new_rows


# ─────────────────────────────────────────────────────────────────────────────
# Ejecutar tarea
# ─────────────────────────────────────────────────────────────────────────────
def normalize_last_run_for_tsql(last_run_at: Optional[str]) -> str:
    default = "1900-01-01 00:00:00"
    if not last_run_at:
        return default
    s = str(last_run_at).strip()
    if not s:
        return default
    iso = s.replace("Z", "+00:00")
    if len(iso) >= 19 and iso[10] == " " and "T" not in iso:
        iso = iso[:10] + "T" + iso[11:]
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return s
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def prepare_extract_sql(sql: str, last_run_at: Optional[str]) -> str:
    if "{last_run}" not in sql:
        return sql
    raw = normalize_last_run_for_tsql(last_run_at)
    escaped = raw.replace("'", "''")
    return sql.replace("{last_run}", escaped)


def run_task(task: Dict[str, Any]) -> int:
    """
    Ejecuta la tarea ETL completa.
    Devuelve el número de filas cargadas.
    """
    task_id          = task["id"]
    task_name        = task["name"]
    load_table       = task["load_table"].lower()
    upsert_keys_raw  = task.get("upsert_keys") or []
    query_tabla      = task.get("query_tabla_destino")
    query_constraint = task.get("query_constraint")
    last_run_at      = task.get("last_run_at")

    task_source_config = task.get("source") or _default_source_config
    if not _warehouse_config or not task_source_config:
        raise RuntimeError("Conexiones no configuradas.")

    if _verbose:
        log.info("--- Tarea %s: %s ---", task_id, task_name)

    # 1) DDL tabla destino
    if query_tabla:
        with _dwh_connection() as conn:
            cur = conn.cursor()
            cur.execute(query_tabla)
            conn.commit()
            if _verbose:
                log.info("  Tabla '%s' verificada.", load_table)

    # 2) Constraint
    if query_constraint:
        try:
            with _dwh_connection() as conn:
                cur = conn.cursor()
                cur.execute(query_constraint)
                conn.commit()
        except Exception as exc:
            log.warning("  Constraint falló (puede que ya exista): %s", exc)

    # 3) Extraer desde origen
    extract_sql = prepare_extract_sql(task["extract_sql"], last_run_at)
    with _source_connection(task_source_config) as conn:
        cur = conn.cursor()
        cur.execute(extract_sql)
        #rows    = cur.fetchall()
        rows = []
        while True:
            chunk = cur.fetchmany(50000)
            if not chunk:
                break
            rows.extend(chunk)
            log.info("  Leídas %d filas...", len(rows))
        columns_raw = [
            d[0].decode("latin-1") if isinstance(d[0], bytes) else str(d[0])
            for d in (cur.description or [])
        ]

    columns = canonicalize_dwh_column_names(load_table, columns_raw)
    if any(a != b for a, b in zip(columns_raw, columns)):
        diff = {a: b for a, b in zip(columns_raw, columns) if a != b}
        log.info("  Tarea %s: columnas alineadas al DWH: %s", task_id, diff)

    rows_list: List[tuple]
    columns, rows_list = merge_row_columns_if_duplicate_names(
        columns, rows, task_id=str(task_id)
    )

    if not columns:
        log.warning("  Tarea %s: sin columnas, se omite.", task_id)
        return 0

    orig_ncol = len(columns)
    rows_tuples = prepare_rows_for_postgres(load_table, columns, rows_list)

    # Inyectar columnas estáticas (p. ej. {"dn": 1000}) definidas por tarea/agencia
    import json as _json
    static_columns = task.get("static_columns") or {}
    if isinstance(static_columns, str):
        try:
            static_columns = _json.loads(static_columns)
        except Exception:
            static_columns = {}
    if static_columns:
        nulls = [k for k, v in static_columns.items() if v is None]
        if nulls:
            log.warning(
                "  Tarea %s: static_columns tiene valores null en: %s",
                task_id, ", ".join(nulls),
            )
        extra_cols = list(static_columns.keys())
        extra_vals = tuple(static_columns.values())
        columns = list(columns) + extra_cols
        rows_tuples = [row + extra_vals for row in rows_tuples]
        log.info("  Tarea %s: columnas estáticas inyectadas: %s", task_id, static_columns)

    upsert_keys = resolve_upsert_keys_to_columns(columns, upsert_keys_raw)

    _, load_tbl_name = split_schema_table(load_table)
    if not upsert_keys and load_tbl_name.lower().strip() == "customers":
        upsert_keys = resolve_upsert_keys_to_columns(
            columns, ["idAgency", "ndClientDMS"]
        )
        if upsert_keys:
            log.info(
                "  Tarea %s: upsert_keys vacío en catálogo; usando %s.",
                task_id,
                ", ".join(upsert_keys),
            )

    columns, rows_tuples, upsert_keys = maybe_adjust_customers_load(
        load_table, columns, rows_tuples, upsert_keys
    )
    if load_tbl_name.lower().strip() == "customers" and len(columns) < orig_ncol:
        log.info(
            "  Tarea %s: customers — se omite columna id del origen (IDENTITY en destino).",
            task_id,
        )

    n_raw = len(rows_tuples)
    rows_tuples = dedupe_rows_for_upsert(columns, rows_tuples, upsert_keys)
    n_dedup = len(rows_tuples)
    if n_raw > n_dedup:
        log.warning(
            "  Tarea %s: %d filas duplicadas por clave upsert en el mismo lote "
            "(se usa la última por clave): %d → %d.",
            task_id, n_raw - n_dedup, n_raw, n_dedup,
        )
    if _verbose:
        log.info("  Filas a cargar: %d", n_dedup)

    # 4) Crear tabla destino si no existe (con schema correcto)
    col_types = infer_column_types(columns, rows_tuples)
    with _dwh_connection() as conn:
        cur = conn.cursor()
        if not pg_table_exists(cur, load_table):
            create_table_if_missing(cur, load_table, columns, col_types, upsert_keys)
            if _verbose:
                log.info(
                    "  Tabla '%s' creada en el DWH (no existía; revisa DDL del catálogo).",
                    load_table,
                )
        ensure_columns_exist(cur, load_table, columns, col_types)
        conn.commit()

    # 5) Upsert con execute_values
    CHUNK_SIZE = 20000
    PAGE_SIZE  = 5000
    upsert_sql = build_upsert_sql_values_template(load_table, columns, upsert_keys)
    with _dwh_connection() as conn:
        cur = conn.cursor()
        if rows_tuples:
            total = len(rows_tuples)
            log.info("  Cargando %d filas (execute_values)...", total)
            for i in range(0, total, CHUNK_SIZE):
                chunk = rows_tuples[i : i + CHUNK_SIZE]
                psycopg2.extras.execute_values(cur, upsert_sql, chunk, page_size=PAGE_SIZE)
                conn.commit()
                loaded = min(i + len(chunk), total)
                log.info("  Cargadas %d / %d filas (%.0f%%)", loaded, total, 100.0 * loaded / total)
            log.info("  Filas cargadas: %d", total)
        else:
            if _verbose:
                log.info("  Sin filas nuevas.")

    return len(rows_tuples)


# ─────────────────────────────────────────────────────────────────────────────
# Detección de cambios entre configuraciones
# ─────────────────────────────────────────────────────────────────────────────
_COMPARE_FIELDS = [
    "schedule_seconds", "extract_sql", "load_table", "upsert_keys",
    "query_tabla_destino", "constraint_nombre", "query_constraint", "active",
]


def detect_changes(old: List[Dict], new: List[Dict]) -> None:
    old_map = {str(t["id"]): t for t in old}
    new_map = {str(t["id"]): t for t in new}
    old_ids, new_ids = set(old_map), set(new_map)

    for tid in sorted(new_ids - old_ids):
        log.info("  [+] Nueva tarea %s: %s", tid, new_map[tid].get("name", ""))
    for tid in sorted(old_ids - new_ids):
        log.info("  [-] Tarea removida %s: %s", tid, old_map[tid].get("name", ""))
    for tid in sorted(old_ids & new_ids):
        cambios = [
            f"{f}: {old_map[tid].get(f)!r} → {new_map[tid].get(f)!r}"
            for f in _COMPARE_FIELDS
            if old_map[tid].get(f) != new_map[tid].get(f)
        ]
        if cambios:
            log.info("  [~] Tarea %s modificada:", tid)
            for c in cambios:
                log.info("      %s", c)


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────
_last_run_ts: Dict[str, float] = {}
TICK_SECONDS  = 5
RETRY_SECONDS = 30


def run_scheduler() -> None:
    global _verbose, _warehouse_config, _default_source_config

    configs: List[Dict[str, Any]] = []
    last_config_refresh: float = 0
    refresh_seconds: int = 60

    log.info("Scheduler iniciado (PostgreSQL).")

    while True:
        now = time.time()

        # ── Refrescar configs ──
        if now - last_config_refresh >= refresh_seconds:
            result = fetch_runtime_config()
            status = result["status"]

            if status == "unreachable":
                log.error("Backend no disponible. Reintentando en %ds...", RETRY_SECONDS)
                configs = []
                last_config_refresh = 0
                time.sleep(RETRY_SECONDS)
                continue

            if status == "invalid":
                log.error("Token no válido. Verifica config.ini. Deteniendo.")
                return

            if status == "disabled":
                log.warning("Servicio deshabilitado. Reintentando en %ds...", RETRY_SECONDS)
                configs = []
                last_config_refresh = 0
                time.sleep(RETRY_SECONDS)
                continue

            _verbose = result["log_verbose"]
            _warehouse_config = result["dwh"]
            _default_source_config = result["source"]
            nuevas = result["configs"]

            new_refresh = result["refresh_seconds"]
            if new_refresh != refresh_seconds:
                log.info("refresh_seconds: %ds → %ds", refresh_seconds, new_refresh)
                refresh_seconds = new_refresh

            if configs and nuevas:
                detect_changes(configs, nuevas)

            old_map = {str(t["id"]): t for t in configs}
            for t in nuevas:
                tid = str(t["id"])
                if tid in old_map:
                    if old_map[tid].get("schedule_seconds") != t.get("schedule_seconds"):
                        _last_run_ts.pop(tid, None)
                        log.info("Tarea %s: schedule reiniciado.", tid)

            configs = nuevas
            last_config_refresh = now
            log.info("Configs: %d tareas activas (refresh: %ds).", len(configs), refresh_seconds)

        # ── Ejecutar tareas ──
        executed = errors = 0

        for task in configs:
            task_id   = str(task["id"])
            task_name = task.get("name", task_id)
            schedule  = task.get("schedule_seconds", 3600)

            if not task.get("active", True):
                continue
            if now - _last_run_ts.get(task_id, 0) < schedule:
                continue

            try:
                rows_loaded = run_task(task)
                _last_run_ts[task_id] = time.time()
                executed += 1

                mark_task_last_run(task_id)

                send_client_event(
                    config_id=task_id,
                    task_name=task_name,
                    event_type="ok",
                    detail=f"{rows_loaded} filas cargadas en '{task.get('load_table', '')}'",
                    rows_loaded=rows_loaded,
                )

            except Exception as exc:
                _last_run_ts[task_id] = time.time()
                errors += 1
                task_source_config = task.get("source") or _default_source_config
                error_msg = redact_sensitive_text(
                    traceback.format_exc(),
                    extra_values=[
                        str(task_source_config.get("pass", "")),
                        str(task_source_config.get("user", "")),
                    ],
                )
                log.error("Error en tarea %s: %s\n%s", task_id, exc, error_msg)

                send_client_event(
                    config_id=task_id,
                    task_name=task_name,
                    event_type="error",
                    detail=error_msg,
                )

        if not _verbose and (executed > 0 or errors > 0):
            log.info("Ciclo: %d ejecutadas, %d errores.", executed, errors)

        time.sleep(TICK_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    validate_api_url_security()
    log.info("=== Nexus DWH Client (PostgreSQL) v4 ===")
    log.info("Server   : %s", API_BASE_URL)
    log.info("Mode     : %s", _client_mode_label())
    log.info("Token    : %s", _mask_active_client_token())
    log.info("Logs     : %s", _LOGS_DIR)
    try:
        run_scheduler()
    except KeyboardInterrupt:
        log.info("Cliente detenido por el usuario.")


if __name__ == "__main__":
    main()