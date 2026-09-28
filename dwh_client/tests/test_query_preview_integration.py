"""
Integración real de ``nexus_agent.query_preview`` (sección 24): contenedores
locales Docker (origen y DWH PostgreSQL, los mismos de test_destination_integration.py).

Cubre, contra bases reales:
  * ``run_upsert_check``: el SAVEPOINT por fila hace que el error de UNA fila
    (violación de CHECK) no borre lo insertado/actualizado por las demás filas
    de la misma pasada, y que la segunda pasada reporte "actualizada" (no
    "insertada de nuevo") para las filas que sí se guardaron en la primera —
    el bug que corrige esta prueba: antes, el ROLLBACK completo de la
    transacción ante CUALQUIER fila con error deshacía también las filas
    anteriores ya upsertadas de la misma pasada.
  * ``wrap_with_limit`` con CTE, ORDER BY, comentarios y punto y coma final,
    ejecutado de verdad contra el origen.

Se salta si Docker no está disponible (``support.docker_available()``).
"""

from types import SimpleNamespace

import pytest

import support
from nexus_agent.query_preview import execute_limited, run_upsert_check, wrap_with_limit

DWH_SCHEMA = "qp_upsert_check_test"
DWH_TABLE = f"{DWH_SCHEMA}.destino"
SRC_TABLE = "qp_upsert_check_src"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        db_connect_timeout_seconds=5, source_statement_timeout_seconds=10,
        dwh_statement_timeout_seconds=10, dwh_lock_timeout_seconds=5,
    )


def _source_dict() -> dict:
    s = support.SRC
    return {"engine": "postgresql", "host": s["host"], "port": s["port"], "database": s["database"],
            "username": s["username"], "password": s["password"], "dsn": ""}


def _warehouse_dict() -> dict:
    d = support.DWH
    return {"host": d["host"], "port": d["port"], "database": d["database"], "username": d["username"],
            "password": d["password"], "schema": "", "sslmode": "", "sslrootcert": ""}


@pytest.fixture(scope="module")
def qpenv():
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.ensure_container(support.SRC)
    support.ensure_container(support.DWH)

    src_conn = support.pg(support.SRC, support.SRC["database"])
    with src_conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SRC_TABLE}")
        cur.execute(f"CREATE TABLE {SRC_TABLE} (id INT, monto INT, nombre TEXT)")
        # id=2 viola el CHECK (monto > 0) del destino: su fila debe fallar en AMBAS pasadas
        # sin arrastrar a las demás filas de la misma pasada.
        cur.execute(f"INSERT INTO {SRC_TABLE} (id, monto, nombre) VALUES (1, 10, 'uno'), "
                    f"(2, -5, 'dos'), (3, 30, 'tres')")
    src_conn.commit()
    src_conn.close()

    dwh_conn = support.pg(support.DWH, support.DWH["database"])
    with dwh_conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {DWH_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {DWH_SCHEMA}")
        cur.execute(f"CREATE TABLE {DWH_TABLE} (id INT PRIMARY KEY, monto INT CHECK (monto > 0), nombre TEXT)")
    dwh_conn.commit()
    dwh_conn.close()
    yield
    dwh_conn = support.pg(support.DWH, support.DWH["database"])
    with dwh_conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {DWH_SCHEMA} CASCADE")
    dwh_conn.commit()
    dwh_conn.close()
    src_conn = support.pg(support.SRC, support.SRC["database"])
    with src_conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SRC_TABLE}")
    src_conn.commit()
    src_conn.close()


def _row_count(table: str) -> int:
    conn = support.pg(support.DWH, support.DWH["database"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_savepoint_por_fila_no_arrastra_filas_buenas_y_segunda_pasada_actualiza(qpenv):
    cmd = {
        "source": _source_dict(), "warehouse": _warehouse_dict(),
        "extract_sql": f"SELECT id, monto, nombre FROM {SRC_TABLE} ORDER BY id",
        "sample_limit": 20, "destination_table": DWH_TABLE, "key_columns": ["id"],
    }
    result = run_upsert_check(cmd, _settings())
    assert result["status"] == "ok", result
    up = result["upsert"]
    # Fila id=2 (monto=-5) falla el CHECK en las DOS pasadas: un solo error reportado (misma fila).
    assert len(up["column_errors"]) == 1
    # Última pasada (la 2ª): id=1 y id=3 YA estaban (de la 1ª pasada, que sí se conserva gracias al
    # SAVEPOINT) → se ACTUALIZAN, no se insertan de nuevo. Antes del arreglo esto daba inserted=2.
    assert up["inserted"] == 0, up
    assert up["updated"] == 2, up
    assert up["rolled_back"] is True
    # Nunca se confirma nada: la tabla destino queda vacía tras el ROLLBACK final.
    assert _row_count(DWH_TABLE) == 0


def test_wrap_with_limit_con_cte_order_by_comentarios_y_punto_y_coma(qpenv):
    sql = (
        "-- comentario inicial\n"
        f"WITH base AS (SELECT id, monto FROM {SRC_TABLE})\n"
        "SELECT * FROM base ORDER BY id DESC; -- comentario final\n"
    )
    # Un WITH inicial (aunque esté bajo un comentario) no se envuelve como subconsulta: se ejecuta
    # tal cual y el límite lo impone fetchmany (execute_limited se encarga de las dos cosas).
    assert wrap_with_limit(sql, "postgresql", 2) is None
    conn = support.pg(support.SRC, support.SRC["database"])
    try:
        with conn.cursor() as cur:
            executed = execute_limited(cur, sql, "postgresql", 2)
            assert "WITH base AS" in executed
            rows = cur.fetchmany(2)
    finally:
        conn.close()
    assert len(rows) == 2
    assert [r[0] for r in rows] == [3, 2]  # ORDER BY id DESC del query original, sin reescribir
