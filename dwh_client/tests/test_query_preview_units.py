"""Pruebas puras de nexus_agent/query_preview.py (sin BD ni red)."""

import pytest

from nexus_agent.query_preview import (
    MAX_SAMPLE_ROWS,
    _cell,
    _describe_type_code,
    run_command,
    wrap_with_limit,
)


@pytest.mark.parametrize("tipo,expected_prefix", [
    ("sqlserver", "SELECT TOP 20 * FROM"),
    ("pervasive", "SELECT TOP 20 * FROM"),
    ("firebird", "SELECT FIRST 20 * FROM"),
    ("postgresql", "SELECT * FROM"),
    ("mysql", "SELECT * FROM"),
])
def test_wrap_with_limit_por_motor(tipo, expected_prefix):
    sql = wrap_with_limit("SELECT * FROM clientes", tipo, 20)
    assert sql.startswith(expected_prefix)
    assert "clientes" in sql


def test_wrap_with_limit_topa_al_maximo():
    sql = wrap_with_limit("SELECT * FROM t", "postgresql", 1000)
    assert f"LIMIT {MAX_SAMPLE_ROWS}" in sql


def test_wrap_with_limit_quita_punto_y_coma_final():
    sql = wrap_with_limit("SELECT * FROM t;", "postgresql", 5)
    assert ";" not in sql


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
