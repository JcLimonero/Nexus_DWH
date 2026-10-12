"""Pruebas puras de type_mapping.py (sin BD): mapeo de tipos y generación de DDL."""

import pytest

from type_mapping import (
    DdlColumnSpec,
    DdlError,
    SourceColumn,
    build_constraint_name,
    build_create_table_sql,
    build_unique_constraint_sql,
    detect_watermark_column,
    is_safe_identifier,
    is_safe_pg_type,
    map_columns,
    map_source_type,
    normalize_columns,
    quote_ident,
    suggest_indexes,
    to_snake_case,
)


# ── Identificadores ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("Cliente Nombre", "cliente_nombre"),
    ("clienteNombre", "cliente_nombre"),
    ("Fecha-Alta!!", "fecha_alta"),
    ("123col", "c_123col"),
    ("select", "select_"),
    ("", "col"),
    ("___", "col"),
    ("ID", "id"),
])
def test_to_snake_case(raw, expected):
    assert to_snake_case(raw) == expected


def test_is_safe_identifier():
    assert is_safe_identifier("cliente_id")
    assert not is_safe_identifier("Cliente Id")
    assert not is_safe_identifier("select")
    assert not is_safe_identifier("a" * 64)


def test_quote_ident_escapes_quotes():
    assert quote_ident('a"b') == '"a""b"'


def test_normalize_columns_dedupes_after_normalization():
    out = normalize_columns(["Nombre", "nombre", "nombre"])
    names = [c.normalized for c in out]
    assert names == ["nombre", "nombre_1", "nombre_2"]
    assert out[0].renamed is True
    # El segundo "nombre" colisiona con el primero ya normalizado: también se renombra (sufijo).
    assert out[1].renamed is True
    assert out[2].renamed is True


def test_normalize_columns_keeps_safe_alias_as_is():
    out = normalize_columns(["cliente_id"])
    assert out[0].normalized == "cliente_id"
    assert out[0].renamed is False


# ── Mapeo de tipos ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("engine,src,length,precision,scale,expected", [
    ("sqlserver", "varchar", 50, None, None, "varchar(50)"),
    ("sqlserver", "nvarchar", -1, None, None, "text"),
    ("sqlserver", "char", 10, None, None, "char(10)"),
    ("sqlserver", "text", None, None, None, "text"),
    ("sqlserver", "tinyint", None, None, None, "smallint"),
    ("sqlserver", "int", None, None, None, "integer"),
    ("sqlserver", "bigint", None, None, None, "bigint"),
    ("sqlserver", "bit", None, None, None, "boolean"),
    ("sqlserver", "decimal", None, 10, 2, "numeric(10,2)"),
    ("sqlserver", "money", None, None, None, "numeric(19,4)"),
    ("sqlserver", "float", None, None, None, "double precision"),
    ("sqlserver", "real", None, None, None, "real"),
    ("sqlserver", "date", None, None, None, "date"),
    ("sqlserver", "datetime2", None, None, None, "timestamp"),
    ("sqlserver", "datetimeoffset", None, None, None, "timestamptz"),
    ("sqlserver", "time", None, None, None, "time"),
    ("sqlserver", "uniqueidentifier", None, None, None, "uuid"),
    ("sqlserver", "varbinary", None, None, None, "bytea"),
    ("mysql", "varchar", 100, None, None, "varchar(100)"),
    ("mysql", "mediumint", None, None, None, "integer"),
    ("mysql", "longtext", None, None, None, "text"),
    ("mysql", "double", None, None, None, "double precision"),
    ("mysql", "datetime", None, None, None, "timestamp"),
    ("mysql", "json", None, None, None, "jsonb"),
    ("postgresql", "numeric", None, 12, 4, "numeric(12,4)"),
    ("postgresql", "uuid", None, None, None, "uuid"),
    ("firebird", "blob", None, None, None, "bytea"),
])
def test_map_source_type_known(engine, src, length, precision, scale, expected):
    pg_type, warning = map_source_type(engine, src, length=length, precision=precision, scale=scale)
    assert pg_type == expected
    assert warning is None


def test_map_source_type_unknown_falls_back_to_text_with_warning():
    pg_type, warning = map_source_type("sqlserver", "sql_variant")
    assert pg_type == "text"
    assert warning is not None and "text" in warning


def test_map_source_type_decimal_clamps_precision():
    pg_type, _ = map_source_type("sqlserver", "decimal", precision=5000, scale=-3)
    assert pg_type == "numeric(1000,0)"


def test_map_columns_renames_and_maps():
    cols = [SourceColumn(name="Cliente Id", source_type="int"),
            SourceColumn(name="nombre", source_type="varchar", length=50)]
    mapped = map_columns("sqlserver", cols)
    assert mapped[0].name == "cliente_id"
    assert mapped[0].renamed_from == "Cliente Id"
    assert mapped[0].pg_type == "integer"
    assert mapped[1].name == "nombre"
    assert mapped[1].renamed_from is None
    assert mapped[1].pg_type == "varchar(50)"


# ── DDL ──────────────────────────────────────────────────────────────────────
def test_build_create_table_sql_basic():
    cols = [DdlColumnSpec("id", "integer", nullable=False), DdlColumnSpec("nombre", "text")]
    sql = build_create_table_sql("dwh", "clientes", cols, key_columns=["id"])
    assert 'CREATE TABLE IF NOT EXISTS "dwh"."clientes"' in sql
    assert '"id" integer NOT NULL' in sql
    assert '"nombre" text' in sql
    assert 'PRIMARY KEY ("id")' in sql


def test_build_create_table_sql_no_schema():
    cols = [DdlColumnSpec("id", "integer")]
    sql = build_create_table_sql(None, "clientes", cols)
    assert sql.startswith('CREATE TABLE IF NOT EXISTS "clientes"')
    assert "PRIMARY KEY" not in sql


def test_build_create_table_sql_static_columns_added():
    cols = [DdlColumnSpec("id", "integer")]
    sql = build_create_table_sql(None, "clientes", cols, static_columns={"dn": "01"})
    assert '"dn" text' in sql


def test_build_create_table_sql_rejects_bad_identifier():
    cols = [DdlColumnSpec("id", "integer")]
    with pytest.raises(DdlError):
        build_create_table_sql(None, "clientes; drop table x", cols)


def test_build_create_table_sql_rejects_duplicate_column():
    cols = [DdlColumnSpec("id", "integer"), DdlColumnSpec("id", "text")]
    with pytest.raises(DdlError):
        build_create_table_sql(None, "clientes", cols)


def test_build_create_table_sql_rejects_bad_type():
    cols = [DdlColumnSpec("id", "integer); drop table x --")]
    with pytest.raises(DdlError):
        build_create_table_sql(None, "clientes", cols)


@pytest.mark.parametrize("bad_type", [
    "integer default (select pg_sleep(5))",
    "integer); drop table x --",
    "text); delete from agent_command; --",
    "integer, (select 1)",
    "varchar(50) collate \"C\"",
    "integer[1:2]",
    "integer references otra_tabla(id)",
    "unknown_type",
    "varchar(99999)",
    "numeric(9999,9999)",
    "",
])
def test_is_safe_pg_type_rechaza_tipos_peligrosos_o_desconocidos(bad_type):
    assert not is_safe_pg_type(bad_type)
    with pytest.raises(DdlError):
        build_create_table_sql(None, "clientes", [DdlColumnSpec("id", bad_type)])


@pytest.mark.parametrize("good_type", [
    "text", "smallint", "integer", "bigint", "boolean", "double precision", "real", "date",
    "timestamp", "timestamptz", "time", "uuid", "bytea", "jsonb", "json", "money",
    "varchar(255)", "char(10)", "numeric(18,4)", "numeric(10)", "integer[]", "text[]",
])
def test_is_safe_pg_type_acepta_lista_blanca(good_type):
    assert is_safe_pg_type(good_type)


def test_build_create_table_sql_rejects_key_not_in_columns():
    cols = [DdlColumnSpec("id", "integer")]
    with pytest.raises(DdlError):
        build_create_table_sql(None, "clientes", cols, key_columns=["otra"])


def test_build_constraint_name():
    assert build_constraint_name("clientes", ["id"]) == "clientes_id_key"
    assert build_constraint_name("clientes", ["empresa_id", "codigo"]) == "clientes_empresa_id_codigo_key"


def test_build_unique_constraint_sql():
    sql = build_unique_constraint_sql("dwh", "clientes", "clientes_id_key", ["id"])
    assert sql == 'ALTER TABLE "dwh"."clientes" ADD CONSTRAINT "clientes_id_key" UNIQUE ("id")'


def test_build_unique_constraint_sql_requires_keys():
    with pytest.raises(DdlError):
        build_unique_constraint_sql(None, "clientes", "c_key", [])


def test_suggest_indexes():
    assert suggest_indexes(["id"], "fecha_mod") == ["id", "fecha_mod"]
    assert suggest_indexes(["id"], "id") == ["id"]
    assert suggest_indexes([], None) == []


@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM t WHERE fecha_modificacion >= '{last_run}'", "fecha_modificacion"),
    ("select * from t where t.updated_at > {last_run}", "updated_at"),
    ("SELECT * FROM t WHERE UpdatedAt >= '{last_run}'", "updated_at"),
    ("SELECT * FROM t", None),
])
def test_detect_watermark_column(sql, expected):
    assert detect_watermark_column(sql) == expected
