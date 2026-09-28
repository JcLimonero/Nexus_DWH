"""
type_mapping.py — mapeo de tipos origen → PostgreSQL y generación de DDL.

Único lugar donde se decide el tipo destino sugerido a partir del tipo que
reporta el motor origen (SQL Server/Pervasive, MySQL, PostgreSQL, Firebird) y
donde se arma el ``CREATE TABLE`` / la restricción de upsert para el catálogo
(``object_catalog.create_table_sql`` / ``create_constraint_sql``). Puras
funciones de texto: no abren conexiones ni dependen de FastAPI, por eso se
prueban sin BD (``dwh_back/tests/test_type_mapping.py``).

El usuario puede editar cada tipo sugerido en el panel antes de guardar; este
módulo solo entrega el punto de partida y valida/normaliza lo que llega.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Identificadores
# ─────────────────────────────────────────────────────────────────────────────
_IDENT_OK_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_SNAKE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_IDENT_RE = re.compile(r"[^a-zA-Z0-9_]+")
PG_RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric", "both", "case", "cast",
    "check", "collate", "column", "constraint", "create", "current_date", "current_time", "current_timestamp",
    "current_user", "default", "deferrable", "desc", "distinct", "do", "else", "end", "except", "false", "for",
    "foreign", "from", "grant", "group", "having", "in", "initially", "intersect", "into", "leading", "limit",
    "localtime", "localtimestamp", "new", "not", "null", "of", "off", "offset", "old", "on", "only", "or",
    "order", "placing", "primary", "references", "select", "session_user", "some", "symmetric", "table", "then",
    "to", "trailing", "true", "union", "unique", "user", "using", "when", "where", "table", "index",
}


def to_snake_case(name: str) -> str:
    """Normaliza un identificador (columna/alias del query) a snake_case seguro para Postgres."""
    name = (name or "").strip()
    if not name:
        return "col"
    name = _SNAKE_BOUNDARY_RE.sub("_", name)
    name = _NON_IDENT_RE.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("_").lower()
    if not name:
        name = "col"
    if name[0].isdigit():
        name = f"c_{name}"
    if name in PG_RESERVED:
        name = f"{name}_"
    return name[:63]


def is_safe_identifier(name: str) -> bool:
    return bool(_IDENT_OK_RE.match(name or "")) and (name or "") not in PG_RESERVED and len(name) <= 63


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


@dataclass
class RenamedColumn:
    original: str
    normalized: str
    renamed: bool


def normalize_columns(names: Sequence[str]) -> List[RenamedColumn]:
    """Alias del query que ya son seguros se dejan igual; el resto se normaliza y se avisa (rename)."""
    out: List[RenamedColumn] = []
    seen: Dict[str, int] = {}
    for raw in names:
        raw = str(raw or "")
        base = raw if is_safe_identifier(raw) else to_snake_case(raw)
        candidate = base
        n = seen.get(base, 0)
        while candidate in seen:
            n += 1
            candidate = f"{base}_{n}"
        seen[base] = n
        seen[candidate] = seen.get(candidate, 0)
        out.append(RenamedColumn(original=raw, normalized=candidate, renamed=candidate != raw))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Mapeo de tipos por motor origen → PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SourceColumn:
    name: str
    source_type: str                 # texto tal cual lo reporta el origen (varchar, int, numeric...)
    nullable: bool = True
    length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None


@dataclass
class MappedColumn:
    name: str                        # ya normalizado (snake_case si aplicaba)
    source_type: str
    pg_type: str
    nullable: bool = True
    warning: Optional[str] = None    # p. ej. "tipo desconocido: se usó text"
    renamed_from: Optional[str] = None


# (patrón, motores a los que aplica; None = todos) — se evalúa en orden, primer match gana.
_RULES: List[Tuple[str, Optional[Tuple[str, ...]]]] = [
    (r"^(nvarchar|varchar)\s*\(\s*max\s*\)$", None),
    (r"^n?varchar$", None),
    (r"^n?char$", None),
    (r"^(n?text|ntext|longtext|mediumtext|tinytext|clob)$", None),
    (r"^(tinyint|smallint|int2)$", None),
    (r"^(int|integer|int4|mediumint)$", None),
    (r"^(bigint|int8)$", None),
    (r"^(bit|bool|boolean)$", None),
    (r"^(decimal|numeric)$", None),
    (r"^money|smallmoney$", None),
    (r"^(float|double|double precision|real)$", None),
    (r"^date$", None),
    (r"^(datetime|datetime2|smalldatetime|timestamp)$", ("sqlserver", "pervasive", "mysql")),
    (r"^timestamp$", ("postgresql",)),  # postgres cursor.description ya distingue timestamptz aparte
    (r"^(timestamptz|timestamp with time zone|datetimeoffset)$", None),
    (r"^time$", None),
    (r"^(uniqueidentifier|uuid)$", None),
    (r"^(binary|varbinary|blob|image|bytea|longblob|mediumblob)$", None),
    (r"^(json|jsonb)$", None),
]


def _num(v: Optional[int], default: int) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def map_source_type(engine: str, source_type: str, *, length: Optional[int] = None,
                    precision: Optional[int] = None, scale: Optional[int] = None) -> Tuple[str, Optional[str]]:
    """Devuelve ``(tipo_postgres, aviso)``. ``aviso`` no es None cuando el tipo es desconocido (→ text)."""
    engine = (engine or "").strip().lower()
    t = (source_type or "").strip().lower()
    t = re.sub(r"\s*\(.*\)$", "", t).strip()  # "varchar(50)" -> "varchar" (length llega aparte)

    if t in ("varchar", "nvarchar", "character varying"):
        if length is not None and int(length) < 0:  # nvarchar(max) / varchar(max) → -1 en algunos drivers
            return "text", None
        n = _num(length, 255)
        return (f"varchar({n})" if n > 0 else "text"), None
    if t in ("char", "nchar", "character"):
        n = _num(length, 1)
        return f"char({max(1, n)})", None
    if t in ("text", "ntext", "longtext", "mediumtext", "tinytext", "clob", "string"):
        return "text", None
    if t in ("tinyint", "int2", "smallint"):
        return "smallint", None
    if t in ("int", "integer", "int4", "mediumint"):
        return "integer", None
    if t in ("bigint", "int8"):
        return "bigint", None
    if t in ("bit",) and engine in ("sqlserver", "pervasive", ""):
        return "boolean", None
    if t in ("bool", "boolean"):
        return "boolean", None
    if t in ("decimal", "numeric"):
        p, s = _num(precision, 18), _num(scale, 0)
        p = max(1, min(1000, p))
        s = max(0, min(p, s))
        return f"numeric({p},{s})", None
    if t in ("money", "smallmoney"):
        return "numeric(19,4)", None
    if t in ("float", "double", "double precision", "float8"):
        return "double precision", None
    if t in ("real", "float4"):
        return "real", None
    if t == "date":
        return "date", None
    if t in ("datetime", "datetime2", "smalldatetime", "timestamp without time zone"):
        return "timestamp", None
    if t == "timestamp":
        # PostgreSQL distingue timestamp/timestamptz por el OID (se resuelve antes de llegar aquí);
        # MySQL/SQL Server reportan "timestamp" a secas para datetime sin zona.
        return "timestamp", None
    if t in ("timestamptz", "timestamp with time zone", "datetimeoffset"):
        return "timestamptz", None
    if t == "time":
        return "time", None
    if t in ("uniqueidentifier", "uuid"):
        return "uuid", None
    if t in ("binary", "varbinary", "blob", "image", "bytea", "longblob", "mediumblob", "tinyblob"):
        return "bytea", None
    if t in ("json",):
        return "jsonb", None
    if t in ("jsonb",):
        return "jsonb", None
    if t in ("xml",):
        return "text", None
    if t in ("year",):
        return "smallint", None
    return "text", f"tipo de origen desconocido «{source_type}»: se usó text (puede editarlo)."


def map_columns(engine: str, columns: Sequence[SourceColumn]) -> List[MappedColumn]:
    normalized = normalize_columns([c.name for c in columns])
    out: List[MappedColumn] = []
    for col, ren in zip(columns, normalized):
        pg_type, warning = map_source_type(engine, col.source_type, length=col.length,
                                           precision=col.precision, scale=col.scale)
        out.append(MappedColumn(name=ren.normalized, source_type=col.source_type, pg_type=pg_type,
                                nullable=col.nullable, warning=warning,
                                renamed_from=col.name if ren.renamed else None))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# DDL
# ─────────────────────────────────────────────────────────────────────────────
class DdlError(ValueError):
    pass


@dataclass
class DdlColumnSpec:
    name: str
    pg_type: str
    nullable: bool = True


# Lista blanca ESTRICTA de tipos PostgreSQL admitidos en el DDL generado (nunca una expresión
# arbitraria): los que produce map_source_type() más los que el usuario puede escribir a mano en el
# panel. Nada fuera de este patrón se acepta (evita inyección de DDL vía el "tipo destino editable",
# p. ej. "integer default (select pg_sleep(5))" o "integer); drop table x --").
_SIZED_TYPE_RE = re.compile(r"^(?:varchar|char|character varying|character)\((?:[1-9][0-9]{0,3})\)$")
_NUMERIC_TYPE_RE = re.compile(r"^(?:numeric|decimal)\((?:[1-9][0-9]{0,3})(?:\s*,\s*[0-9]{1,3})?\)$")
_PLAIN_TYPES = {
    "text", "smallint", "integer", "bigint", "boolean", "double precision", "real", "date",
    "timestamp", "timestamptz", "timestamp without time zone", "timestamp with time zone",
    "time", "time without time zone", "uuid", "bytea", "jsonb", "json", "money",
}


def is_safe_pg_type(pg_type: str) -> bool:
    """True solo si ``pg_type`` es exactamente uno de la lista blanca (opcionalmente como arreglo ``[]``)."""
    t = (pg_type or "").strip().lower()
    if t.endswith("[]"):
        t = t[:-2].strip()
    return bool(t) and (t in _PLAIN_TYPES or _SIZED_TYPE_RE.match(t) or _NUMERIC_TYPE_RE.match(t))


def _validate_columns(columns: Sequence[DdlColumnSpec]) -> None:
    if not columns:
        raise DdlError("La tabla necesita al menos una columna.")
    seen = set()
    for c in columns:
        if not is_safe_identifier(c.name):
            raise DdlError(f"Nombre de columna no válido: «{c.name}».")
        if c.name in seen:
            raise DdlError(f"Columna duplicada: «{c.name}».")
        seen.add(c.name)
        if not is_safe_pg_type(c.pg_type):
            raise DdlError(f"Tipo de columna no válido para «{c.name}»: «{c.pg_type}».")


def build_create_table_sql(schema: Optional[str], table: str, columns: Sequence[DdlColumnSpec],
                           key_columns: Sequence[str] = (), static_columns: Optional[Dict[str, Any]] = None
                           ) -> str:
    """``CREATE TABLE IF NOT EXISTS <esquema.tabla> (...)`` con PK opcional sobre ``key_columns``."""
    if not is_safe_identifier(table):
        raise DdlError(f"Nombre de tabla no válido: «{table}».")
    if schema and not is_safe_identifier(schema):
        raise DdlError(f"Nombre de esquema no válido: «{schema}».")
    _validate_columns(columns)
    col_names = {c.name for c in columns}
    static_columns = static_columns or {}
    all_cols = list(columns)
    for k in static_columns:
        sk = to_snake_case(str(k))
        if sk not in col_names:
            all_cols.append(DdlColumnSpec(name=sk, pg_type="text", nullable=True))
            col_names.add(sk)
    for k in key_columns:
        if k not in col_names:
            raise DdlError(f"La llave «{k}» no está entre las columnas de la tabla.")
    qualified = f"{quote_ident(schema)}.{quote_ident(table)}" if schema else quote_ident(table)
    lines = [f"  {quote_ident(c.name)} {c.pg_type}" + ("" if c.nullable else " NOT NULL") for c in all_cols]
    if key_columns:
        lines.append(f"  PRIMARY KEY ({', '.join(quote_ident(k) for k in key_columns)})")
    return f"CREATE TABLE IF NOT EXISTS {qualified} (\n" + ",\n".join(lines) + "\n)"


def build_constraint_name(table: str, key_columns: Sequence[str]) -> str:
    base = to_snake_case(table)
    suffix = "_".join(to_snake_case(k) for k in key_columns) or "pk"
    name = f"{base}_{suffix}_key"[:63]
    return name


def build_unique_constraint_sql(schema: Optional[str], table: str, constraint_name: str,
                                key_columns: Sequence[str]) -> str:
    if not key_columns:
        raise DdlError("Se necesita al menos una columna llave para la restricción de upsert.")
    if not is_safe_identifier(constraint_name):
        raise DdlError(f"Nombre de restricción no válido: «{constraint_name}».")
    qualified = f"{quote_ident(schema)}.{quote_ident(table)}" if schema else quote_ident(table)
    cols = ", ".join(quote_ident(k) for k in key_columns)
    return (f"ALTER TABLE {qualified} ADD CONSTRAINT {quote_ident(constraint_name)} UNIQUE ({cols})")


def suggest_indexes(key_columns: Sequence[str], watermark_column: Optional[str]) -> List[str]:
    """Sugerencia de índices: la(s) llave(s) (si no hay PK) y la columna comparada con {last_run}, si se detecta."""
    out: List[str] = []
    if key_columns:
        out.append(",".join(key_columns))
    if watermark_column and watermark_column not in key_columns:
        out.append(watermark_column)
    return out


_LAST_RUN_COL_RE = re.compile(
    r"([a-zA-Z_][a-zA-Z0-9_\.]*)\s*(?:>=?|<=?)\s*(?:'\{last_run\}'|\{last_run\}|:last_run)", re.IGNORECASE)


def detect_watermark_column(extract_sql: str) -> Optional[str]:
    """Heurística: columna comparada contra ``{last_run}`` en el query (para sugerir índice)."""
    m = _LAST_RUN_COL_RE.search(extract_sql or "")
    if not m:
        return None
    col = m.group(1).split(".")[-1]
    return to_snake_case(col) if not is_safe_identifier(col) else col
