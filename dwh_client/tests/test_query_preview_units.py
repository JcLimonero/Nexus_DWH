"""Pruebas puras de nexus_agent/query_preview.py (sin BD ni red)."""

from unittest.mock import MagicMock

import pytest

from nexus_agent.query_preview import (
    MAX_SAMPLE_ROWS,
    _cell,
    _describe_type_code,
    _strip_trailing_noise,
    execute_limited,
    run_command,
    wrap_with_limit,
)


@pytest.mark.parametrize("tipo,expected_prefix", [
    ("firebird", "SELECT FIRST 20 * FROM"),
    ("postgresql", "SELECT * FROM"),
    ("mysql", "SELECT * FROM"),
])
def test_wrap_with_limit_por_motor(tipo, expected_prefix):
    sql = wrap_with_limit("SELECT * FROM clientes", tipo, 20)
    assert sql.startswith(expected_prefix)
    assert "clientes" in sql


@pytest.mark.parametrize("tipo", ["sqlserver", "pervasive"])
def test_wrap_with_limit_sqlserver_pervasive_no_envuelve(tipo):
    # TOP reescrito como subconsulta es frágil (CTE, comentarios, ORDER BY): se ejecuta tal cual
    # y el límite lo impone fetchmany + cierre temprano de la conexión (ver execute_limited).
    assert wrap_with_limit("SELECT * FROM clientes", tipo, 20) is None


def test_wrap_with_limit_topa_al_maximo():
    sql = wrap_with_limit("SELECT * FROM t", "postgresql", 1000)
    assert f"LIMIT {MAX_SAMPLE_ROWS}" in sql


def test_wrap_with_limit_quita_punto_y_coma_final():
    sql = wrap_with_limit("SELECT * FROM t;", "postgresql", 5)
    assert ";" not in sql


@pytest.mark.parametrize("tipo", ["postgresql", "mysql", "firebird", "sqlserver", "pervasive"])
def test_wrap_with_limit_cte_no_se_envuelve_en_ningun_motor(tipo):
    sql = "WITH base AS (SELECT 1 AS x) SELECT * FROM base"
    assert wrap_with_limit(sql, tipo, 10) is None


def test_wrap_with_limit_detecta_cte_bajo_comentario_inicial():
    sql = "-- comentario\nWITH base AS (SELECT 1) SELECT * FROM base"
    assert wrap_with_limit(sql, "postgresql", 10) is None


@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM t;", "SELECT * FROM t"),
    ("SELECT * FROM t -- comentario final", "SELECT * FROM t"),
    ("SELECT * FROM t; -- comentario final", "SELECT * FROM t"),
    ("SELECT * FROM t /* bloque */", "SELECT * FROM t"),
    ("SELECT * FROM t;\n-- otra línea de comentario\n", "SELECT * FROM t"),
    ("SELECT * FROM t", "SELECT * FROM t"),
])
def test_strip_trailing_noise(sql, expected):
    assert _strip_trailing_noise(sql) == expected


def test_execute_limited_ejecuta_envuelto_cuando_es_seguro():
    cur = MagicMock()
    executed = execute_limited(cur, "SELECT * FROM t", "postgresql", 5)
    assert "LIMIT 5" in executed
    cur.execute.assert_called_once_with(executed)


def test_execute_limited_ejecuta_tal_cual_para_sqlserver():
    cur = MagicMock()
    executed = execute_limited(cur, "SELECT * FROM t; -- fin", "sqlserver", 5)
    assert executed == "SELECT * FROM t"
    cur.execute.assert_called_once_with("SELECT * FROM t")


def test_describe_type_code_normaliza_alias():
    assert _describe_type_code("mysql", "STRING") == "varchar"
    assert _describe_type_code("mysql", "LONG") == "bigint"
    assert _describe_type_code("mysql", "algo_desconocido") == "algo_desconocido"


def test_cell_trunca_texto_largo():
    assert _cell(1) == 1
    assert _cell(True) is True
    assert _cell(None) is None
    long = "x" * 600
    assert len(_cell(long)) == 500


def test_run_command_kind_desconocido():
    out = run_command({"kind": "algo_raro"}, object())
    assert out["status"] == "failed"
    assert out["error_code"] == "UNKNOWN_KIND"
