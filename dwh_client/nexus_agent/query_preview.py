"""
query_preview.py — "Tabla destino desde el query" (sección 24): ejecuta los
comandos que Nexus le pide al agente (POST /agent/commands/claim), del mismo
modo que ``destination.py`` ejecuta las pruebas de conexión.

Tres comandos (``kind``):
  * ``query_preview``  — ejecuta el query de origen limitado a una muestra
    (envuelto en un ``SELECT ... LIMIT/TOP/FIRST`` según el motor), lee
    metadatos de columna (nombre, tipo de origen, nulabilidad, longitud/
    precisión/escala) y, si Nexus lo autoriza (``include_rows``), la muestra.
  * ``create_table``   — ejecuta en el DWH el DDL que ya generó el backend
    (``CREATE TABLE IF NOT EXISTS``: idempotente).
  * ``upsert_check``   — vuelve a ejecutar el query de origen (SOLO LECTURA;
    Nexus nunca reenvía filas) y hace upsert de la muestra dos veces dentro
    de una transacción en el DWH que SIEMPRE termina en ROLLBACK.

Nunca se registran SQL ni valores de fila en los logs del agente (mismo
criterio que etl.py); los errores se sanean con ``sanitize_error``.
"""

import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import AGENT_VERSION
from .etl import (
    RunContext,
    build_upsert_sql,
    connect_dwh,
    connect_source,
    prepare_extract_sql,
    prepare_rows_for_postgres,
    resolve_upsert_keys_to_columns,
)
from .sanitize import SECRETS, StageError, sanitize_error

MAX_SAMPLE_ROWS = 20


# ─────────────────────────────────────────────────────────────────────────────
# Límite de filas por motor
# ─────────────────────────────────────────────────────────────────────────────
# Envolver el query del usuario como subconsulta (``SELECT * FROM (<query>) AS q LIMIT n``) es
# frágil ante CTE (``WITH ...``), comentarios finales o un punto y coma final: T-SQL directamente
# no admite un ``WITH`` dentro de una subconsulta, y un comentario/`;` sobrante rompe la subconsulta
# igual en cualquier motor. Por eso ``wrap_with_limit`` primero limpia el ruido final (comentarios y
# `;`) y, cuando envolver no es seguro (SQL Server/Pervasive siempre — mejor ``TOP`` en la sesión que
# reescribir el query —, o un ``WITH`` inicial en cualquier motor), devuelve ``None``: en ese caso se
# ejecuta el query TAL CUAL y el límite lo impone ``cursor.fetchmany(limit)`` con cierre temprano de
# la conexión (no se seguirán leyendo filas del servidor).
_TRAILING_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/\s*$", re.DOTALL)
_TRAILING_LINE_COMMENT_RE = re.compile(r"--[^\n]*$")
_LEADING_BLOCK_COMMENT_RE = re.compile(r"^\s*/\*.*?\*/", re.DOTALL)
_LEADING_LINE_COMMENT_RE = re.compile(r"^\s*--[^\n]*\n")
_CTE_RE = re.compile(r"(?is)^\s*with\b")


def _strip_trailing_noise(sql: str) -> str:
    """Quita, del final, comentarios de bloque/línea y un ``;`` sobrante (repite hasta estabilizar)."""
    s = sql.strip()
    for _ in range(50):  # cota defensiva: nunca un bucle infinito
        new = _TRAILING_BLOCK_COMMENT_RE.sub("", s).rstrip()
        m = _TRAILING_LINE_COMMENT_RE.search(new)
        if m:
            new = new[: m.start()].rstrip()
        if new.endswith(";"):
            new = new[:-1].rstrip()
        if new == s:
            return s
        s = new
    return s


def _strip_leading_noise(sql: str) -> str:
    """Quita, del inicio, comentarios de bloque/línea (para detectar un CTE bajo un comentario)."""
    s = sql
    for _ in range(50):
        new = _LEADING_BLOCK_COMMENT_RE.sub("", s)
        new = _LEADING_LINE_COMMENT_RE.sub("", new)
        new = new.lstrip()
        if new == s:
            return s
        s = new
    return s


def _looks_like_cte(sql: str) -> bool:
    return bool(_CTE_RE.match(_strip_leading_noise(sql)))


def wrap_with_limit(sql: str, tipo: str, limit: int) -> Optional[str]:
    """
    Devuelve el SQL envuelto con el límite de filas, o ``None`` si debe ejecutarse tal cual (el
    límite se aplica entonces con ``cursor.fetchmany`` y cierre temprano de la conexión).
    """
    inner = _strip_trailing_noise(sql)
    limit = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    if tipo in ("sqlserver", "pervasive"):
        return None  # TOP en una subconsulta reescrita es frágil (CTE, comentarios); mejor fetchmany.
    if _looks_like_cte(inner):
        return None  # un WITH inicial no siempre puede anidarse dentro de "FROM (...) AS q".
    if tipo == "firebird":
        return f"SELECT FIRST {limit} * FROM ({inner}) AS nexus_preview_q"
    return f"SELECT * FROM ({inner}) AS nexus_preview_q LIMIT {limit}"


def execute_limited(cursor: Any, sql: str, tipo: str, limit: int) -> str:
    """Ejecuta ``sql`` respetando el límite de filas; devuelve el SQL realmente ejecutado (para
    describir columnas). Si no se pudo envolver, ejecuta el original (limpio) y confía en
    ``fetchmany`` + cierre temprano de la conexión (ver módulo)."""
    to_run = wrap_with_limit(sql, tipo, limit) or _strip_trailing_noise(sql)
    cursor.execute(to_run)
    return to_run


# ─────────────────────────────────────────────────────────────────────────────
# Metadatos de columna por motor
# ─────────────────────────────────────────────────────────────────────────────
def _from_cursor_description(cursor: Any, tipo: str) -> List[Dict[str, Any]]:
    """Último recurso (y única vía en MySQL/Firebird/PostgreSQL vía psycopg2): cursor.description."""
    out: List[Dict[str, Any]] = []
    for col in cursor.description or []:
        name = col[0]
        type_code = col[1]
        display_size = col[2]
        internal_size = col[3]
        precision = col[4] if len(col) > 4 else None
        scale = col[5] if len(col) > 5 else None
        null_ok = col[6] if len(col) > 6 else True
        src_type = _describe_type_code(tipo, type_code)
        length = None
        if src_type in ("varchar", "nvarchar", "char", "nchar"):
            length = internal_size or display_size
        out.append({"name": str(name), "source_type": src_type, "nullable": bool(null_ok) if null_ok is not None
                    else True, "length": length, "precision": precision, "scale": scale})
    return out


def _describe_type_code(tipo: str, type_code: Any) -> str:
    """PEP-249 type_code puede ser un objeto DBAPI genérico (STRING, NUMBER...) o específico del driver."""
    name = getattr(type_code, "__name__", None) or str(type_code)
    name = name.lower()
    # pymysql/pyodbc/fdb devuelven nombres razonablemente descriptivos; se normalizan alias comunes.
    aliases = {
        "str": "varchar", "string": "varchar", "text": "text", "int": "integer", "long": "bigint",
        "number": "numeric", "decimal": "decimal", "float": "float", "double": "double",
        "datetime": "datetime", "date": "date", "time": "time", "bool": "boolean", "binary": "varbinary",
        "blob": "blob", "none": "text",
    }
    return aliases.get(name, name)


def sqlserver_columns(cursor: Any, wrapped_sql: str) -> List[Dict[str, Any]]:
    """``sp_describe_first_result_set`` (metadatos exactos); si falla, cursor.description."""
    try:
        cursor.execute("EXEC sp_describe_first_result_set @tsql = ?", wrapped_sql)
        out = []
        for row in cursor.fetchall():
            d = {row.cursor_description[i][0]: v for i, v in enumerate(row)} if hasattr(row, "cursor_description") \
                else dict(zip([c[0] for c in cursor.description], row))
            out.append({"name": d.get("name"), "source_type": str(d.get("system_type_name") or "").split("(")[0],
                       "nullable": bool(d.get("is_nullable", True)), "length": d.get("max_length"),
                       "precision": d.get("precision"), "scale": d.get("scale")})
        if out:
            return out
    except Exception:  # noqa: BLE001 — sin permiso / vista con SP: se cae al fallback
        pass
    return []


def pg_columns(cursor: Any) -> List[Dict[str, Any]]:
    """PostgreSQL: cursor.description trae el OID de tipo; se resuelve con pg_type."""
    out = []
    oids = {c[1] for c in cursor.description or []}
    names: Dict[int, str] = {}
    if oids:
        cursor.execute("SELECT oid, typname FROM pg_type WHERE oid = ANY(%s)", (list(oids),))
        names = {int(r[0]): r[1] for r in cursor.fetchall()}
    for col in cursor.description or []:
        typname = names.get(col[1], "text")
        out.append({"name": col[0], "source_type": typname, "nullable": True,
                   "length": col[3] if col[3] and col[3] > 0 else None,
                   "precision": col[4] if col[4] and col[4] > 0 else None,
                   "scale": col[5] if col[5] and col[5] >= 0 else None})
    return out


def describe_columns(cursor: Any, tipo: str, wrapped_sql: str) -> List[Dict[str, Any]]:
    if tipo in ("sqlserver", "pervasive"):
        cols = sqlserver_columns(cursor, wrapped_sql)
        if cols:
            return cols
    if tipo == "postgresql":
        return pg_columns(cursor)
    return _from_cursor_description(cursor, tipo)


# ─────────────────────────────────────────────────────────────────────────────
# query_preview
# ─────────────────────────────────────────────────────────────────────────────
def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:500]


def run_query_preview(cmd: Dict[str, Any], settings: Any) -> Dict[str, Any]:
    src = cmd.get("source") or {}
    SECRETS.add(src.get("host"), src.get("username"), src.get("password"), src.get("database"), src.get("dsn"))
    limit = int(cmd.get("sample_limit") or MAX_SAMPLE_ROWS)
    include_rows = bool(cmd.get("include_rows", True))
    t0 = time.monotonic()
    conn = None
    try:
        sql = prepare_extract_sql(str(cmd.get("extract_sql") or ""), None)
        conn, tipo = connect_source(src, settings, RunContext())
        cur = conn.cursor()
        executed_sql = execute_limited(cur, sql, tipo, limit)
        columns = describe_columns(cur, tipo, executed_sql)
        rows_raw = cur.fetchmany(limit)
        row_count = len(rows_raw)
        out: Dict[str, Any] = {"status": "ok", "columns": columns, "row_count": row_count,
                               "agent_version": AGENT_VERSION}
        if include_rows:
            out["rows"] = [[_cell(v) for v in row] for row in rows_raw]
        return out
    except Exception as exc:  # noqa: BLE001
        code, msg = sanitize_error(StageError("extract", exc, side="SOURCE"))
        return {"status": "failed", "error_code": code, "error_message": msg, "agent_version": AGENT_VERSION}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        _ = time.monotonic() - t0  # duración informativa (no crítica para el resultado)


# ─────────────────────────────────────────────────────────────────────────────
# create_table
# ─────────────────────────────────────────────────────────────────────────────
def run_create_table(cmd: Dict[str, Any], settings: Any) -> Dict[str, Any]:
    wh = cmd.get("warehouse") or {}
    SECRETS.add(wh.get("host"), wh.get("username"), wh.get("password"), wh.get("database"))
    t0 = time.monotonic()
    conn = None
    try:
        ddl = str(cmd.get("ddl") or "").strip()
        if not ddl:
            return {"status": "failed", "error_code": "MISSING_DDL", "error_message": "Sin DDL para ejecutar.",
                    "agent_version": AGENT_VERSION}
        conn = connect_dwh(wh, settings, RunContext())
        cur = conn.cursor()
        cur.execute(ddl)  # CREATE TABLE IF NOT EXISTS: idempotente.
        conn.commit()
        return {"status": "ok", "duration_ms": int((time.monotonic() - t0) * 1000), "agent_version": AGENT_VERSION}
    except Exception as exc:  # noqa: BLE001
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        code, msg = sanitize_error(StageError("load", exc, side="DWH"))
        return {"status": "failed", "error_code": code, "error_message": msg, "agent_version": AGENT_VERSION}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ─────────────────────────────────────────────────────────────────────────────
# upsert_check — inserta dos veces dentro de una transacción y hace ROLLBACK
# ─────────────────────────────────────────────────────────────────────────────
def run_upsert_check(cmd: Dict[str, Any], settings: Any) -> Dict[str, Any]:
    src = cmd.get("source") or {}
    wh = cmd.get("warehouse") or {}
    SECRETS.add(src.get("host"), src.get("username"), src.get("password"), src.get("database"), src.get("dsn"))
    SECRETS.add(wh.get("host"), wh.get("username"), wh.get("password"), wh.get("database"))
    limit = int(cmd.get("sample_limit") or MAX_SAMPLE_ROWS)
    table = str(cmd.get("destination_table") or "")
    key_columns = list(cmd.get("key_columns") or [])
    t0 = time.monotonic()
    src_conn = None
    dwh_conn = None
    try:
        sql = prepare_extract_sql(str(cmd.get("extract_sql") or ""), None)
        src_conn, tipo = connect_source(src, settings, RunContext())
        scur = src_conn.cursor()
        execute_limited(scur, sql, tipo, limit)
        columns = [c[0] for c in scur.description or []]
        rows_raw = scur.fetchmany(limit)
        src_conn.close()
        src_conn = None
        if not rows_raw:
            return {"status": "ok", "row_count": 0, "columns": [], "agent_version": AGENT_VERSION,
                    "upsert": {"inserted": 0, "updated": 0, "duplicate_keys_in_sample": 0,
                              "null_keys_in_sample": 0, "column_errors": {}, "rolled_back": True}}
        valid_keys = resolve_upsert_keys_to_columns(columns, key_columns)
        dup_keys = 0
        null_keys = 0
        if valid_keys:
            idxs = [columns.index(k) for k in valid_keys]
            seen = set()
            for row in rows_raw:
                key = tuple(row[i] for i in idxs)
                if any(v is None for v in key):
                    null_keys += 1
                elif key in seen:
                    dup_keys += 1
                else:
                    seen.add(key)
        rows = prepare_rows_for_postgres(table, columns, rows_raw)
        sql_upsert = build_upsert_sql(table, columns, key_columns)
        # RETURNING (xmax = 0) para distinguir insertadas de actualizadas (mismo criterio que etl.py).
        sql_upsert_returning = sql_upsert + " RETURNING (xmax = 0) AS inserted"
        column_errors: Dict[str, str] = {}
        dwh_conn = connect_dwh(wh, settings, RunContext())
        dcur = dwh_conn.cursor()
        inserted = updated = 0
        try:
            for pass_no in (1, 2):
                inserted = updated = 0
                for i, row in enumerate(rows):
                    # SAVEPOINT por fila: un error de una fila (tipo, NOT NULL, overflow...) se
                    # descarta con ROLLBACK TO SAVEPOINT y NO se pierde lo ya insertado/actualizado
                    # por las filas anteriores de esta misma pasada (antes un ROLLBACK de toda la
                    # transacción las borraba también). El ROLLBACK final de la transacción completa
                    # sigue garantizado: esto nunca confirma nada en el DWH.
                    dcur.execute("SAVEPOINT nexus_upsert_check_row")
                    try:
                        dcur.execute(sql_upsert_returning, row)
                        r = dcur.fetchone()
                        dcur.execute("RELEASE SAVEPOINT nexus_upsert_check_row")
                        if r and r[0]:
                            inserted += 1
                        else:
                            updated += 1
                    except Exception as exc:  # noqa: BLE001 — error de tipos/constraint por fila
                        code, msg = sanitize_error(StageError("load", exc, side="DWH"))
                        column_errors[f"fila_{i + 1}"] = f"{code}: {msg}"
                        dcur.execute("ROLLBACK TO SAVEPOINT nexus_upsert_check_row")
                        dcur.execute("RELEASE SAVEPOINT nexus_upsert_check_row")
            # Segunda pasada esperada: 0 insertadas, N actualizadas (mismas llaves).
        finally:
            dwh_conn.rollback()  # NUNCA se confirma: es solo una validación (deshace TODO, incluidas
                                 # las filas que sí se insertaron/actualizaron bien en cada pasada).
        return {"status": "ok", "columns": [], "row_count": len(rows_raw),
                "duration_ms": int((time.monotonic() - t0) * 1000), "agent_version": AGENT_VERSION,
                "upsert": {"inserted": inserted, "updated": updated, "duplicate_keys_in_sample": dup_keys,
                          "null_keys_in_sample": null_keys, "column_errors": column_errors, "rolled_back": True}}
    except Exception as exc:  # noqa: BLE001
        code, msg = sanitize_error(exc)
        return {"status": "failed", "error_code": code, "error_message": msg, "agent_version": AGENT_VERSION}
    finally:
        for c in (src_conn, dwh_conn):
            if c is not None:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass


def run_command(cmd: Dict[str, Any], settings: Any) -> Dict[str, Any]:
    """Despacha por ``kind`` (nunca lanza: siempre devuelve un cuerpo de resultado)."""
    kind = cmd.get("kind")
    try:
        if kind == "query_preview":
            return run_query_preview(cmd, settings)
        if kind == "create_table":
            return run_create_table(cmd, settings)
        if kind == "upsert_check":
            return run_upsert_check(cmd, settings)
        return {"status": "failed", "error_code": "UNKNOWN_KIND", "error_message": f"Comando no soportado: {kind}.",
                "agent_version": AGENT_VERSION}
    except Exception as exc:  # noqa: BLE001 — defensa: el resultado siempre se reporta
        code, msg = sanitize_error(exc)
        return {"status": "failed", "error_code": code, "error_message": msg, "agent_version": AGENT_VERSION}
