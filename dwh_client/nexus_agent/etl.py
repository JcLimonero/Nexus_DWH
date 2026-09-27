"""
etl.py — extracción, transformación y carga de una tarea (agente v5).

Diferencias con el cliente v3/v4:
  * UNA transacción por ejecución en el DWH: los chunks se insertan dentro de
    la misma transacción y se hace un único COMMIT al final. Si algo falla (o
    se cancela) se hace ROLLBACK: no quedan cargas parciales. El DDL (CREATE
    TABLE / constraint / columnas nuevas) va en transacciones cortas previas,
    para no mantener locks exclusivos de tabla durante toda la carga.
  * Lectura en streaming del origen (fetchmany; cursor de servidor en
    PostgreSQL y SSCursor en MySQL): memoria acotada a ``fetch_chunk_rows``.
  * Watermark = hora de INICIO de la extracción, tomada del reloj del ORIGEN
    (o del agente si no se puede), que solo se confirma tras el COMMIT.
  * Timeouts de conexión y de sentencia en todos los drivers (salvo fdb).
  * Conteo de insertadas vs actualizadas con ``RETURNING (xmax = 0)``.
  * Cancelación segura entre chunks (Cancelled → ROLLBACK).
  * Nunca se registra SQL ni valores de filas en los logs.
"""

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.extras

from .logsetup import get_logger
from .sanitize import SECRETS, Cancelled, ConfigError, StageError

log = get_logger()

try:  # pyodbc necesita un gestor ODBC instalado; se importa bajo demanda.
    import pyodbc  # type: ignore
except Exception:  # pragma: no cover - depende del sistema
    pyodbc = None  # type: ignore

try:
    import pymysql  # type: ignore
    import pymysql.cursors  # type: ignore
except Exception:  # pragma: no cover
    pymysql = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL helpers — schema-aware
# ─────────────────────────────────────────────────────────────────────────────
def quote_ident(name: str) -> str:
    """PostgreSQL usa comillas dobles para identificadores (columnas); se escapan comillas internas."""
    return '"' + str(name).replace('"', '""') + '"'


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
    return f"{quote_ident(schema)}.{quote_ident(tbl)}"


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
    cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
    if cursor.fetchone() is None:
        # Solo si no existe: CREATE SCHEMA IF NOT EXISTS exige CREATE sobre la base aunque el esquema exista.
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)}")

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


def pg_constraint_signature(cursor: Any, table: str) -> List[Tuple[str, str]]:
    """Restricciones actuales de la tabla (nombre, definición): para saber si un DDL cambió algo."""
    cursor.execute("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                   "WHERE conrelid = to_regclass(%s) ORDER BY 1", (quote_table(table),))
    return [(str(r[0]), str(r[1])) for r in cursor.fetchall()]


def ensure_columns_exist(
    cursor: Any, table: str,
    columns: Sequence[str], col_types: Dict[str, str],
) -> List[str]:
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
    added_names: List[str] = []
    for col in columns:
        if col in existing or col.lower() in existing_lower:
            continue
        sql_type = col_types.get(col, "TEXT")
        cursor.execute(
            f"ALTER TABLE {quote_table(table)} "
            f"ADD COLUMN {quote_ident(col)} {sql_type}"
        )
        added += 1
        added_names.append(col)
        log.info("  Auto-columna añadida: %s.%s (%s)", table, col, sql_type)
    if added:
        log.info("  %d columna(s) añadida(s) a '%s'.", added, table)
    return added_names


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
# Marcador {last_run}
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


# ─────────────────────────────────────────────────────────────────────────────
# Contexto y resultado de ejecución
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TaskResult:
    rows_read: int = 0
    rows_loaded: int = 0
    rows_inserted: Optional[int] = None
    rows_updated: Optional[int] = None
    watermark: Optional[datetime] = None      # naive, dominio del reloj indicado en watermark_kind
    watermark_kind: str = ""
    warnings: List[str] = field(default_factory=list)
    # DDL aplicado por el agente (sin SQL): evidencia para el inventario estructural.
    ddl_applied: List[Dict[str, Any]] = field(default_factory=list)


class RunContext:
    """Estado compartido entre el worker y quien puede cancelar (shutdown)."""

    def __init__(self, cancel_event: Optional[threading.Event] = None) -> None:
        self.cancel_event = cancel_event or threading.Event()
        self.stage = "config"
        self._hooks: List[Callable[[], None]] = []
        self._lock = threading.Lock()
        self.on_chunk: Optional[Callable[[str, int], None]] = None  # (etapa, filas) para pruebas/progreso
        self.before_commit: Optional[Callable[[], None]] = None     # marca local "committing"
        # DDL aplicado (evidencia para el inventario); se conserva aunque la ejecución falle después.
        self.ddl_applied: List[Dict[str, Any]] = []

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled("Ejecución cancelada (apagado del agente).")

    def add_cancel_hook(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._hooks.append(fn)

    def clear_hooks(self) -> None:
        with self._lock:
            self._hooks = []

    def fire_cancel_hooks(self) -> None:
        """Interrumpe sentencias en curso (psycopg2.cancel / pyodbc cursor.cancel)."""
        with self._lock:
            hooks = list(self._hooks)
        for fn in hooks:
            try:
                fn()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Conexiones con timeouts
# ─────────────────────────────────────────────────────────────────────────────
def register_task_secrets(task: Dict[str, Any], warehouse: Dict[str, Any]) -> None:
    src = task.get("source") or {}
    if isinstance(task.get("warehouse"), dict) and task["warehouse"]:
        warehouse = task["warehouse"]  # destino efectivo de la tarea (Nexus >= 5.3)
    SECRETS.add(src.get("host"), src.get("username"), src.get("password"), src.get("database"), src.get("dsn"),
                warehouse.get("host"), warehouse.get("username"), warehouse.get("password"),
                warehouse.get("database"))


def _detect_sql_server_driver() -> str:
    available = pyodbc.drivers() if pyodbc else []
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


def _odbc_escape(value: Any) -> str:
    s = str(value)
    if any(ch in s for ch in ";{}=") or s != s.strip():
        return "{" + s.replace("}", "}}") + "}"
    return s


def build_sqlserver_conn_str(src: Dict[str, Any]) -> str:
    dsn = (src.get("dsn") or "").strip()
    if dsn:
        parts = [f"DSN={_odbc_escape(dsn)}"]
        if src.get("username"):
            parts.append(f"UID={_odbc_escape(src['username'])}")
        if src.get("password"):
            parts.append(f"PWD={_odbc_escape(src['password'])}")
        return ";".join(parts)
    if not src.get("host"):
        raise ConfigError("No hay host de origen configurado ni DSN ODBC.")
    server = str(src["host"])
    port = src.get("port") or 1433
    if port and int(port) != 1433:
        server = f"{server},{port}"
    return (
        f"DRIVER={{{_detect_sql_server_driver()}}};SERVER={_odbc_escape(server)};"
        f"DATABASE={_odbc_escape(src.get('database', ''))};"
        f"UID={_odbc_escape(src.get('username', ''))};PWD={_odbc_escape(src.get('password', ''))};"
        f"TrustServerCertificate=yes"
    )


def _require_pyodbc() -> None:
    if pyodbc is None:
        raise ConfigError("pyodbc no está disponible (instale el driver ODBC).")


def connect_source(src: Dict[str, Any], settings: Any, ctx: RunContext) -> Tuple[Any, str]:
    """Conexión al origen con timeouts. Devuelve (conn, tipo)."""
    tipo = (src.get("type") or "sqlserver").lower()
    ct = int(settings.db_connect_timeout_seconds)
    st = int(settings.source_statement_timeout_seconds)
    if tipo == "mysql":
        if pymysql is None:
            raise ConfigError("pymysql no está disponible.")
        if not src.get("host"):
            raise ConfigError("No hay host de origen MySQL configurado.")
        conn = pymysql.connect(
            host=src["host"], port=int(src.get("port") or 3306), user=src.get("username", ""),
            password=src.get("password", ""), database=src.get("database", ""), charset="utf8mb4",
            connect_timeout=ct, read_timeout=(st or None), write_timeout=(st or None),
            cursorclass=pymysql.cursors.SSCursor,
        )
    elif tipo in ("sqlserver", "pervasive"):
        _require_pyodbc()
        conn = pyodbc.connect(build_sqlserver_conn_str(src), timeout=ct)
        conn.timeout = st  # timeout de consulta (segundos, 0 = sin límite)
        if tipo == "pervasive":
            conn.setdecoding(pyodbc.SQL_CHAR, encoding="latin-1")
            conn.setdecoding(pyodbc.SQL_WCHAR, encoding="latin-1")
            conn.setencoding(encoding="latin-1")
    elif tipo == "postgresql":
        if not src.get("host"):
            raise ConfigError("No hay host de origen PostgreSQL configurado.")
        conn = psycopg2.connect(
            host=src["host"], port=int(src.get("port") or 5432), user=src.get("username", ""),
            password=src.get("password", ""), dbname=src.get("database", ""), connect_timeout=ct,
            options=f"-c statement_timeout={st * 1000}", application_name="nexus-dwh-agent",
        )
        conn.set_session(readonly=True)
        ctx.add_cancel_hook(conn.cancel)
    elif tipo == "firebird":
        dsn = (src.get("dsn") or "").strip()
        if dsn:
            _require_pyodbc()
            parts = [f"DSN={_odbc_escape(dsn)}"]
            if src.get("username"):
                parts.append(f"UID={_odbc_escape(src['username'])}")
            if src.get("password"):
                parts.append(f"PWD={_odbc_escape(src['password'])}")
            conn = pyodbc.connect(";".join(parts), autocommit=False, timeout=ct)
            conn.timeout = st
            conn.setdecoding(pyodbc.SQL_CHAR, encoding="latin-1")
            conn.setdecoding(pyodbc.SQL_WCHAR, encoding="latin-1")
            conn.setencoding(encoding="latin-1")
        elif src.get("host"):
            try:
                import fdb  # type: ignore
            except ImportError:
                raise ConfigError("Para conectar a Firebird sin DSN instale: pip install fdb")
            # fdb no soporta timeouts de conexión/sentencia.
            conn = fdb.connect(host=src["host"], port=int(src.get("port") or 3050), database=src.get("database", ""),
                               user=src.get("username", ""), password=src.get("password", ""), charset="WIN1252")
        else:
            raise ConfigError("Firebird requiere DSN o host configurado.")
    else:
        raise ConfigError(f"Tipo de origen no soportado: {tipo}.")
    return conn, tipo


def connect_dwh(warehouse: Dict[str, Any], settings: Any, ctx: RunContext) -> Any:
    from .destination import pg_ssl_kwargs, warehouse_schema

    if not warehouse.get("host"):
        raise ConfigError("No hay host DWH configurado.")
    opts = [f"-c statement_timeout={int(settings.dwh_statement_timeout_seconds) * 1000}"]
    if settings.dwh_lock_timeout_seconds:
        opts.append(f"-c lock_timeout={int(settings.dwh_lock_timeout_seconds) * 1000}")
    conn = psycopg2.connect(
        host=str(warehouse["host"]), port=int(warehouse.get("port") or 5432),
        dbname=str(warehouse.get("database", "")), user=str(warehouse.get("username", "")),
        password=str(warehouse.get("password", "")), connect_timeout=int(settings.db_connect_timeout_seconds),
        options=" ".join(opts), application_name="nexus-dwh-agent", **pg_ssl_kwargs(warehouse, settings),
    )
    conn.set_client_encoding("UTF8")
    ctx.add_cancel_hook(conn.cancel)
    schema = warehouse_schema(warehouse)
    if schema != "public":
        # El DDL del catálogo sin esquema (create_table_sql / constraint) va al esquema destino.
        # SET de sesión + COMMIT inmediato (un ROLLBACK posterior no lo revierte).
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        if cur.fetchone() is None:
            # Solo si no existe (CREATE SCHEMA IF NOT EXISTS exige CREATE sobre la base aunque exista).
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)}")
        cur.execute(f"SET search_path TO {quote_ident(schema)}, public")
        conn.commit()
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Watermark
# ─────────────────────────────────────────────────────────────────────────────
_CLOCK_SQL = {
    "sqlserver": "SELECT GETDATE()",
    "pervasive": "SELECT NOW()",
    "postgresql": "SELECT LOCALTIMESTAMP",
    "mysql": "SELECT NOW(6)",
    "firebird": "SELECT CURRENT_TIMESTAMP FROM RDB$DATABASE",
}


def _to_naive(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        # Hora local del origen expresada con zona: se conserva la hora de pared del origen.
        dt = dt.replace(tzinfo=None)
    return dt


def read_source_clock(conn: Any, tipo: str) -> Optional[datetime]:
    sql = _CLOCK_SQL.get(tipo)
    if not sql:
        return None
    if tipo == "mysql" and pymysql is not None:
        cur = conn.cursor(pymysql.cursors.Cursor)
    else:
        cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return _to_naive(row[0]) if row else None


def capture_watermark(conn: Any, tipo: str, settings: Any, warnings: List[str]) -> Tuple[datetime, str]:
    """Hora de INICIO de la extracción (antes de ejecutar la consulta)."""
    mode = settings.watermark_clock
    if mode == "source":
        try:
            dt = read_source_clock(conn, tipo)
            if dt is not None:
                return dt, "source_clock"
        except Exception as exc:
            log.warning("  No se pudo leer el reloj del origen (%s); se usa el reloj local del agente.",
                        type(exc).__name__)
            if tipo == "postgresql":
                try:
                    conn.rollback()
                except Exception:
                    pass
        warnings.append("SOURCE_CLOCK_UNAVAILABLE")
        mode = "agent_local"
    if mode == "agent_utc":
        return datetime.now(timezone.utc).replace(tzinfo=None), "agent_utc"
    return datetime.now(), "agent_local"


def parse_watermark(value: Optional[str]) -> Optional[datetime]:
    return _to_naive(value) if value else None


def watermark_iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def extract_overlap_seconds(settings: Any, previous_kind: Optional[str]) -> int:
    """
    Solapamiento de seguridad. En el primer paso desde v3/v4 (watermark_kind =
    legacy_last_run) last_run_at era el reloj de NEXUS al FINAL de la corrida
    (otro reloj/huso): se usa un solapamiento mayor.
    """
    overlap = int(settings.watermark_overlap_seconds)
    if previous_kind == "legacy_last_run":
        overlap = max(overlap, int(getattr(settings, "legacy_watermark_overlap_seconds", 3600)))
    return overlap


def effective_watermark_for_extract(watermark: Optional[datetime], overlap_seconds: int,
                                    has_upsert_keys: bool) -> Optional[datetime]:
    """
    Aplica el solapamiento de seguridad solo cuando hay claves de upsert (con
    INSERT plano el solapamiento duplicaría filas en cada corrida).
    """
    if watermark is None:
        return None
    if has_upsert_keys and overlap_seconds > 0:
        return watermark - timedelta(seconds=overlap_seconds)
    return watermark


# ─────────────────────────────────────────────────────────────────────────────
# SQL de carga con conteo insert/update
# ─────────────────────────────────────────────────────────────────────────────
def build_load_sql(table: str, columns: Sequence[str], upsert_keys: Sequence[str]) -> Tuple[str, str]:
    """(sql para execute_values, modo) con RETURNING para contar insertadas/actualizadas."""
    base = build_upsert_sql_values_template(table, columns, upsert_keys)
    valid = resolve_upsert_keys_to_columns(columns, upsert_keys)
    if not valid:
        return base + " RETURNING TRUE", "insert"
    if base.endswith("DO NOTHING"):
        return base + " RETURNING TRUE", "nothing"
    return base + " RETURNING (xmax = 0)", "upsert"


def _parse_static_columns(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        log.warning("  static_columns no es JSON válido; se ignora.")
        return {}


def _open_stream_cursor(conn: Any, tipo: str) -> Any:
    if tipo == "postgresql":
        cur = conn.cursor(name="nexus_extract")  # cursor de servidor: no trae todo a memoria
        cur.itersize = 2000
        return cur
    return conn.cursor()


# ─────────────────────────────────────────────────────────────────────────────
# Ejecutar tarea
# ─────────────────────────────────────────────────────────────────────────────
def run_task(task: Dict[str, Any], warehouse: Dict[str, Any], previous_watermark: Optional[datetime],
             settings: Any, ctx: RunContext, previous_kind: Optional[str] = None) -> TaskResult:
    """
    Ejecuta la tarea completa. Lanza StageError(etapa, exc) o Cancelled.
    No registra SQL ni datos. Devuelve TaskResult con el watermark a confirmar.
    """
    res = TaskResult()
    res.ddl_applied = ctx.ddl_applied
    task_id = task["task_id"]
    try:
        ctx.stage = "config"
        from .destination import effective_load_table
        if isinstance(task.get("warehouse"), dict) and task["warehouse"]:
            # Destino efectivo de la tarea (Nexus >= 5.3) sobre el general: nunca se carga en otro DWH.
            warehouse = task["warehouse"]
        load_table = effective_load_table(str(task["load_table"]), warehouse or {})
        src = task.get("source") or {}
        if not warehouse or not warehouse.get("host"):
            raise ConfigError("Conexión al DWH no configurada.")
        upsert_keys_cfg = list(task.get("upsert_keys") or [])
        _, load_tbl_name = split_schema_table(load_table)
        is_customers = load_tbl_name.lower().strip() == "customers"
        static_columns = _parse_static_columns(task.get("static_columns"))
        if static_columns:
            nulls = [k for k, v in static_columns.items() if v is None]
            if nulls:
                log.warning("  Tarea %s: static_columns con valores null en %d columna(s).", task_id, len(nulls))
        chunk_rows = int(settings.fetch_chunk_rows)

        # ── Extracción (streaming) ──────────────────────────────────────────
        ctx.stage = "extract"
        sconn, tipo = connect_source(src, settings, ctx)
        try:
            wm, wm_kind = capture_watermark(sconn, tipo, settings, res.warnings)
            res.watermark, res.watermark_kind = wm, wm_kind
            has_keys = bool(upsert_keys_cfg) or is_customers
            overlap = extract_overlap_seconds(settings, previous_kind)
            eff = effective_watermark_for_extract(previous_watermark, overlap, has_keys)
            extract_sql = prepare_extract_sql(task["extract_sql"], watermark_iso(eff) if eff else None)
            ctx.check_cancel()
            scur = _open_stream_cursor(sconn, tipo)
            if tipo in ("sqlserver", "pervasive", "firebird") and pyodbc is not None:
                ctx.add_cancel_hook(scur.cancel)
            scur.execute(extract_sql)
            del extract_sql
            chunk = scur.fetchmany(chunk_rows)
            description = scur.description or []
            columns_raw = [d[0].decode("latin-1") if isinstance(d[0], bytes) else str(d[0]) for d in description]
            res.rows_read += len(chunk)
            if ctx.on_chunk:
                ctx.on_chunk("extract", len(chunk))
            if not columns_raw:
                log.warning("  Tarea %s: la consulta no devolvió columnas; se omite la carga.", task_id)
                return res

            # ── Plan de transformación (a partir del primer chunk) ──────────
            ctx.stage = "transform"
            canon = canonicalize_dwh_column_names(load_table, columns_raw)
            if any(a != b for a, b in zip(columns_raw, canon)):
                log.info("  Tarea %s: %d columna(s) alineadas a nombres del DWH.", task_id,
                         sum(1 for a, b in zip(columns_raw, canon) if a != b))
            extra_cols = list(static_columns.keys())
            extra_vals = tuple(static_columns.values())
            if extra_cols:
                log.info("  Tarea %s: %d columna(s) estáticas inyectadas.", task_id, len(extra_cols))
            first_call = {"v": True}

            def transform(rows_in: Sequence[Sequence[Any]]) -> Tuple[List[str], List[tuple], List[str]]:
                cols, rows = merge_row_columns_if_duplicate_names(
                    canon, rows_in, task_id=str(task_id) if first_call["v"] else "")
                first_call["v"] = False
                rows = prepare_rows_for_postgres(load_table, cols, rows)
                if extra_cols:
                    cols = list(cols) + extra_cols
                    rows = [r + extra_vals for r in rows]
                keys = resolve_upsert_keys_to_columns(cols, upsert_keys_cfg)
                if not keys and is_customers:
                    keys = resolve_upsert_keys_to_columns(cols, ["idAgency", "ndClientDMS"])
                cols, rows, keys = maybe_adjust_customers_load(load_table, cols, rows, keys)
                n_raw = len(rows)
                rows = dedupe_rows_for_upsert(cols, rows, keys)
                if n_raw > len(rows) and "DUPLICATE_KEYS_IN_BATCH" not in res.warnings:
                    res.warnings.append("DUPLICATE_KEYS_IN_BATCH")
                return list(cols), rows, list(keys)

            columns, first_rows, upsert_keys = transform(chunk)

            # ── DDL en transacciones cortas ─────────────────────────────────
            ctx.stage = "load"
            dconn = connect_dwh(warehouse, settings, ctx)
            try:
                cur = dconn.cursor()
                schema_, tbl_ = split_schema_table(load_table)
                ddl_object = f"{schema_}.{tbl_}".lower()
                existed_before = pg_table_exists(cur, load_table)
                if task.get("create_table_sql"):
                    cur.execute(task["create_table_sql"])
                    dconn.commit()
                    if not existed_before and pg_table_exists(cur, load_table):
                        res.ddl_applied.append({"object": ddl_object, "action": "create_table", "columns": []})
                        existed_before = True
                if task.get("create_constraint_sql"):
                    try:
                        before = pg_constraint_signature(cur, load_table)
                        cur.execute(task["create_constraint_sql"])
                        dconn.commit()
                        # Solo es evidencia si el DDL cambió algo (el catálogo lo re-ejecuta en cada corrida).
                        if pg_constraint_signature(cur, load_table) != before:
                            res.ddl_applied.append({"object": ddl_object, "action": "constraint_ddl", "columns": []})
                    except psycopg2.Error as exc:
                        dconn.rollback()
                        log.warning("  Tarea %s: el constraint del catálogo no se aplicó (%s).", task_id,
                                    type(exc).__name__)
                        res.warnings.append("CONSTRAINT_DDL_FAILED")
                if first_rows:
                    col_types = infer_column_types(columns, first_rows)
                    if not pg_table_exists(cur, load_table):
                        create_table_if_missing(cur, load_table, columns, col_types, upsert_keys)
                        log.info("  Tarea %s: tabla destino creada (no existía; revise el DDL del catálogo).", task_id)
                        res.ddl_applied.append({"object": ddl_object, "action": "create_table", "columns": []})
                    added_cols = ensure_columns_exist(cur, load_table, columns, col_types)
                    dconn.commit()
                    if added_cols:
                        res.ddl_applied.append({"object": ddl_object, "action": "add_column",
                                                "columns": [str(c)[:200] for c in added_cols][:200]})

                # ── Carga: UNA transacción para toda la ejecución ───────────
                load_sql, mode = build_load_sql(load_table, columns, upsert_keys)
                inserted = updated = 0
                rows = first_rows
                while True:
                    ctx.check_cancel()
                    if rows:
                        ctx.stage = "load"
                        out = psycopg2.extras.execute_values(cur, load_sql, rows, page_size=5000, fetch=True)
                        flags = [bool(r[0]) for r in out]
                        if mode == "upsert":
                            inserted += sum(1 for f in flags if f)
                            updated += sum(1 for f in flags if not f)
                        else:
                            inserted += len(flags)
                        res.rows_loaded += len(rows)
                        if ctx.on_chunk:
                            ctx.on_chunk("load", len(rows))
                        log.info("  Tarea %s: %d filas en transacción (sin confirmar).", task_id, res.rows_loaded)
                    ctx.check_cancel()
                    ctx.stage = "extract"
                    chunk = scur.fetchmany(chunk_rows)
                    if not chunk:
                        break
                    res.rows_read += len(chunk)
                    if ctx.on_chunk:
                        ctx.on_chunk("extract", len(chunk))
                    ctx.stage = "transform"
                    _, rows, _ = transform(chunk)
                ctx.stage = "load"
                ctx.check_cancel()
                if ctx.before_commit:
                    ctx.before_commit()
                dconn.commit()
                res.rows_inserted, res.rows_updated = inserted, updated
                if not upsert_keys and res.rows_loaded:
                    # INSERT plano: un reintento de esta ventana duplicaría filas.
                    res.warnings.append("NO_UPSERT_KEYS_DUPLICATES_POSSIBLE")
            except BaseException:
                try:
                    dconn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    dconn.close()
                except Exception:
                    pass
        finally:
            try:
                sconn.close()
            except Exception:
                pass
    except (Cancelled, StageError):
        raise
    except ConfigError as exc:
        raise StageError("config" if ctx.stage == "config" else ctx.stage, exc)
    except Exception as exc:
        raise StageError(ctx.stage, exc)
    finally:
        ctx.clear_hooks()
    return res
