"""
destination_postgres.py — destino (DWH) efectivo de una empresa
───────────────────────────────────────────────────────────────
Regla única (sección 22 de DWH_README.md):

  * Por defecto la empresa usa el destino de su GRUPO (``company.warehouse_mode
    = 'inherit'``).
  * Con ``warehouse_mode = 'custom'`` la empresa usa su PROPIO destino completo
    (host, puerto, base, usuario, contraseña, esquema, SSL). No se mezclan
    campos del grupo y de la empresa.

Este módulo concentra esa regla (SQL y Python) para que /agent/tasks, los
endpoints legados, el inventario, la prueba de conexión y el panel usen
exactamente el mismo destino.
"""

import hashlib
import re
from typing import Any, Callable, Dict, Optional

SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
# Modos que un agente anterior a 5.3 NO haría cumplir (se conectaría con 'prefer').
SSL_ENFORCED = ("require", "verify-ca", "verify-full")
DEFAULT_SCHEMA = "public"
DEFAULT_SSLMODE = "prefer"
MAX_CA_PEM = 16384

# Esquema: identificador PostgreSQL simple en minúsculas (el agente trabaja en minúsculas
# y siempre lo cita con comillas dobles).
SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_PEM_RE = re.compile(
    r"^(-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]{16,}-----END CERTIFICATE-----\s*)+$")

WH_FIELDS = ("host", "port", "database", "username", "password", "schema", "sslmode", "sslrootcert")
WH_SECRET_FIELDS = ("host", "database", "username", "password")


def check_schema(v: str) -> str:
    v = (v or "").strip()
    if not SCHEMA_RE.match(v):
        raise ValueError("esquema no válido: minúsculas, números y _ (máx. 63; no puede empezar con número)")
    if v.startswith("pg_") or v == "information_schema":
        raise ValueError("esquema reservado del sistema")
    return v


def check_sslrootcert(v: Optional[str]) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    if len(v) > MAX_CA_PEM:
        raise ValueError(f"certificado demasiado grande (máx. {MAX_CA_PEM} caracteres)")
    v = v.replace("\r\n", "\n") + "\n"
    if not _PEM_RE.match(v):
        raise ValueError("debe ser uno o más certificados PEM (-----BEGIN CERTIFICATE----- … -----END CERTIFICATE-----)")
    return v


def effective_sql(field: str, c: str = "c", g: str = "g") -> str:
    """Expresión SQL del campo efectivo (lista blanca de campos)."""
    if field not in WH_FIELDS:
        raise ValueError(field)
    return f"(CASE WHEN {c}.warehouse_mode = 'custom' THEN {c}.warehouse_{field} ELSE {g}.warehouse_{field} END)"


def effective_columns_sql(c: str = "c", g: str = "g", prefix: str = "eff_wh_") -> str:
    """Columnas SELECT con el destino efectivo (``eff_wh_host``…) y su origen (``eff_wh_source``)."""
    cols = [f"{effective_sql(f, c, g)} AS {prefix}{f}" for f in WH_FIELDS]
    cols.append(f"(CASE WHEN {c}.warehouse_mode = 'custom' THEN 'company' ELSE 'group' END) AS {prefix}source")
    return ", ".join(cols)


def qualified_table_sql(table_expr: str, c: str = "c", g: str = "g") -> str:
    """
    Tabla destino calificada con el esquema efectivo cuando el catálogo no trae
    esquema (``dwh.carter`` se respeta; ``carter`` → ``<esquema>.carter`` salvo
    que el esquema sea ``public``, que se deja igual que antes).
    """
    sch = effective_sql("schema", c, g)
    return (f"(CASE WHEN position('.' in {table_expr}) > 0 OR {sch} = 'public' "
            f"THEN {table_expr} ELSE {sch} || '.' || {table_expr} END)")


def qualify_table(table: str, schema: Optional[str]) -> str:
    table = (table or "").strip()
    schema = (schema or "").strip() or DEFAULT_SCHEMA
    if "." in table or schema == DEFAULT_SCHEMA:
        return table
    return f"{schema}.{table}"


def warehouse_from_row(row: Dict[str, Any], decrypt: Callable[[Optional[str]], str],
                       prefix: str = "eff_wh_", source: Optional[str] = None) -> Dict[str, Any]:
    """Diccionario de conexión (descifrado) para el agente a partir de columnas ``<prefix><campo>``."""
    def raw(f: str) -> Any:
        return row.get(prefix + f)

    return {
        "host": decrypt(raw("host") or ""),
        "port": int(raw("port") or 5432),
        "database": decrypt(raw("database") or ""),
        "username": decrypt(raw("username") or ""),
        "password": decrypt(raw("password") or ""),
        "schema": (raw("schema") or DEFAULT_SCHEMA),
        "sslmode": (raw("sslmode") or DEFAULT_SSLMODE),
        "sslrootcert": raw("sslrootcert") or "",
        "source": source or row.get(prefix + "source") or "group",
    }


def dwh_identity(wh: Dict[str, Any]) -> Optional[str]:
    """Identidad estable del DWH (mismo algoritmo que inventory_postgres.connection_identity)."""
    host = str(wh.get("host") or "").strip()
    if not host:
        return None
    try:
        port = int(wh.get("port") or 5432)
    except (TypeError, ValueError):
        port = 5432
    loc = f"{host.lower()}:{port}/{str(wh.get('database') or '').strip()}"
    return hashlib.sha256(f"dwh|postgresql|{loc}".encode("utf-8")).hexdigest()


def config_fingerprint(params: Dict[str, Any]) -> str:
    """Huella de la configuración probada SIN la contraseña (para avisar si cambió)."""
    parts = [str(params.get(k) or "") for k in ("kind", "engine", "host", "port", "database", "username",
                                                 "schema", "sslmode", "dsn")]
    parts.append(hashlib.sha256(str(params.get("sslrootcert") or "").encode("utf-8")).hexdigest())
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def connection_signature(wh: Dict[str, Any]) -> Optional[str]:
    """
    Firma de la CONEXIÓN completa (host, puerto, base, usuario, contraseña, sslmode y CA): dos destinos
    con el mismo host/base pero otro usuario u otras opciones SSL son conexiones distintas.
    """
    if not str(wh.get("host") or "").strip():
        return None
    parts = [str(wh.get("host") or "").strip().lower(), str(wh.get("port") or 5432),
             str(wh.get("database") or "").strip(), str(wh.get("username") or ""),
             hashlib.sha256(str(wh.get("password") or "").encode("utf-8")).hexdigest(),
             str(wh.get("sslmode") or DEFAULT_SSLMODE),
             hashlib.sha256(str(wh.get("sslrootcert") or "").strip().encode("utf-8")).hexdigest()]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def ddl_needs_search_path(schema: str, destination_table: str, *ddl: Optional[str]) -> bool:
    """
    ¿El DDL del catálogo (create_table_sql / create_constraint_sql) quedaría en otro esquema sin el
    search_path del agente 5.3? Sí cuando el esquema destino no es public, la tabla del catálogo no trae
    esquema y hay DDL que no menciona el esquema destino (heurística conservadora).
    """
    schema = (schema or DEFAULT_SCHEMA).strip().lower()
    if schema == DEFAULT_SCHEMA or "." in (destination_table or ""):
        return False
    texts = [str(t).lower() for t in ddl if t and str(t).strip()]
    if not texts:
        return False
    return not all((f"{schema}." in t or f'"{schema}".' in t) for t in texts)


def needs_destination_v2(wh: Dict[str, Any], top_level_signature: Optional[str],
                         destination_table: str = "", create_table_sql: Optional[str] = None,
                         create_constraint_sql: Optional[str] = None) -> Optional[str]:
    """
    Motivo por el que un agente SIN soporte de destino configurable (< 5.3) no puede ejecutar la
    tarea sin riesgo, o None si puede. El esquema de la tabla se resuelve calificándola (los agentes
    anteriores entienden "esquema.tabla"), pero el DDL del catálogo sin esquema no.
    """
    if connection_signature(wh) != top_level_signature:
        return "destination_per_company"
    if (wh.get("sslmode") or DEFAULT_SSLMODE) in SSL_ENFORCED:
        return "ssl_enforced"
    if ddl_needs_search_path(wh.get("schema") or DEFAULT_SCHEMA, destination_table, create_table_sql,
                             create_constraint_sql):
        return "schema_ddl"
    return None
