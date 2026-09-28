"""
"Tabla destino desde el query" (sección 24 de DWH_README.md): ciclo de vida del
comando agent_command (query_preview / create_table / upsert_check), permisos,
límites y — sobre todo — que las FILAS de muestra nunca queden en la BD de
Nexus ni en ningún log: solo viven en memoria y se entregan una vez al usuario
que pidió el comando.

Requiere Docker local (BD de configuración propia, backend en subproceso);
se salta si Docker no está disponible (ver support.docker_available()).
"""

import secrets

import psycopg2
import pytest
import requests

import support

DB = "nexus_test_cfg_qprev"
T = 15
FEAT = {"x-nexus-agent-features": "destination-v2,connection-test,query-preview"}


def pw() -> str:
    return "Pr-" + secrets.token_urlsafe(14)


def q(sql, params=()):
    c = support.cfg_conn(DB)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


def enroll(url, token, name="agente"):
    r = requests.post(url + "/agent/enroll", headers={"x-token": token, **FEAT},
                      json={"name": name, "hostname": "h", "client_version": "5.4.0"}, timeout=T)
    assert r.status_code == 201, r.text
    d = r.json()
    return {"x-installation-id": d["installation_id"], "x-installation-secret": d["secret"], **FEAT}


def heartbeat(url, h):
    r = requests.post(url + "/agent/heartbeat", headers=h, timeout=T,
                      json={"client_version": "5.4.0", "features": ["destination-v2", "connection-test",
                                                                    "query-preview"]})
    assert r.status_code == 200, r.text
    return r.json()


def claim(url, h):
    r = requests.post(url + "/agent/commands/claim", headers=h, timeout=T)
    assert r.status_code == 200, r.text
    return r.json()["command"]


def report(url, h, command_id, body):
    r = requests.post(url + f"/agent/commands/{command_id}/result", headers=h, json=body, timeout=T)
    return r


def call(env, who, method, path, body=None, expect=None):
    """Devuelve el JSON ya parseado (como support.Backend.admin); use call_raw si necesita el Response."""
    r = call_raw(env, who, method, path, body, expect)
    return r.json() if r.content else None


def call_raw(env, who, method, path, body=None, expect=None):
    r = requests.request(method, env["b"].url + path, json=body, headers=env["users"][who], timeout=T)
    if expect is not None:
        assert r.status_code == expect, f"{who} {method} {path} -> {r.status_code}: {r.text[:300]}"
    return r


@pytest.fixture(scope="module")
def qenv():
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.recreate_config_db(DB)
    b = support.Backend(DB)
    b.start()
    try:
        g = b.admin("POST", "/admin/groups", {
            "name": "Grupo Query", "warehouse_host": "dwh.invalid", "warehouse_database": "dwh",
            "warehouse_username": "u", "warehouse_password": pw()}, expect=201)
        c = b.admin("POST", "/admin/companies", {
            "group_id": g["id"], "name": "Empresa Query", "source_type": "postgresql",
            "source_host": "src.invalid", "source_port": 5432, "source_database": "dms",
            "source_username": "src_user", "source_password": pw()}, expect=201)
        c_noprev = b.admin("POST", "/admin/companies", {
            "group_id": g["id"], "name": "Empresa Sin Muestra", "source_type": "postgresql",
            "source_host": "src2.invalid", "source_database": "dms2", "source_username": "u2",
            "source_password": pw(), "allow_data_preview": False}, expect=201)
        other = b.admin("POST", "/admin/groups", {
            "name": "Grupo Ajeno Q", "warehouse_host": "dwh2.invalid", "warehouse_database": "d2",
            "warehouse_username": "u2", "warehouse_password": pw()}, expect=201)
        users = {}
        for name, roles in (("cfg_q", [{"role": "admin_config", "group_id": g["id"]}]),
                            ("viewer_q", [{"role": "lectura", "group_id": g["id"]}]),
                            ("ajeno_q", [{"role": "admin_config", "group_id": other["id"]}])):
            p = pw()
            b.admin("POST", "/admin/users", {"username": name, "email": f"{name}@ejemplo.test", "password": p,
                                             "must_change_password": False, "roles": roles}, expect=201)
            r = requests.post(b.url + "/admin/auth/login", json={"email": f"{name}@ejemplo.test", "password": p},
                              timeout=T)
            assert r.status_code == 200, r.text
            users[name] = {"authorization": "Bearer " + r.json()["token"]}
        yield dict(b=b, g=g, c=c, c_noprev=c_noprev, other=other, users=users)
    finally:
        b.stop()
        support.drop_config_db(DB)


def _company_token(env, company):
    row = q("SELECT company_token FROM company WHERE id = %s", (company["id"],))
    return row[0][0]


# ─────────────────────────────────────────────────────────────────────────────
def test_no_agent_sin_instalacion_en_linea(qenv):
    out = call(qenv, "cfg_q", "POST", "/admin/query-commands",
              {"kind": "query_preview", "company_id": qenv["c"]["id"], "extract_sql": "SELECT 1 AS id"},
              expect=201)
    assert out["status"] == "no_agent"
    assert out["error_code"] == "NO_AGENT_ONLINE"


def test_ciclo_completo_query_preview_filas_una_sola_vez(qenv):
    b = qenv["b"]
    token = _company_token(qenv, qenv["c"])
    h = enroll(b.url, token)
    heartbeat(b.url, h)

    out = call(qenv, "cfg_q", "POST", "/admin/query-commands",
              {"kind": "query_preview", "company_id": qenv["c"]["id"],
               "extract_sql": "SELECT id, nombre FROM clientes"}, expect=201)
    assert out["status"] == "pending"
    cid = out["id"]

    cmd = claim(b.url, h)
    assert cmd["kind"] == "query_preview"
    assert cmd["extract_sql"] == "SELECT id, nombre FROM clientes"
    assert cmd["include_rows"] is True

    r = report(b.url, h, cid, {
        "status": "ok",
        "columns": [{"name": "id", "source_type": "int", "nullable": False},
                    {"name": "Nombre Cliente", "source_type": "varchar", "length": 50, "nullable": True}],
        "row_count": 2, "rows": [[1, "Ana"], [2, "Luis"]], "duration_ms": 42, "agent_version": "5.4.0",
    })
    assert r.status_code == 200, r.text

    got = call(qenv, "cfg_q", "GET", f"/admin/query-commands/{cid}", expect=200)
    assert got["status"] == "ok"
    assert got["has_sample_rows"] is True
    cols = got["result"]["columns"]
    assert cols[0]["suggested_pg_type"] == "integer"
    assert cols[1]["suggested_name"] == "nombre_cliente"
    assert cols[1]["renamed_from"] == "Nombre Cliente"

    # Las filas solo llegan si se piden explícitamente (include_rows=true) y una sola vez.
    with_rows = call(qenv, "cfg_q", "GET", f"/admin/query-commands/{cid}?include_rows=true", expect=200)
    assert with_rows["rows"] == [[1, "Ana"], [2, "Luis"]]
    again = call(qenv, "cfg_q", "GET", f"/admin/query-commands/{cid}?include_rows=true", expect=200)
    assert "rows" not in again or not again["rows"]
    assert again["has_sample_rows"] is False

    # Nunca en BD ni en activity_log.
    (raw_req,) = q("SELECT request::text, result_meta::text FROM agent_command WHERE id = %s", (cid,))
    assert "Ana" not in raw_req[0] and "Ana" not in (raw_req[1] or "")
    logs = q("SELECT error_detail FROM activity_log WHERE error_detail IS NOT NULL")
    assert all("Ana" not in (row[0] or "") for row in logs)


def test_otro_usuario_no_puede_leer_las_filas(qenv):
    b = qenv["b"]
    token = _company_token(qenv, qenv["c"])
    h = enroll(b.url, token, name="agente2")
    heartbeat(b.url, h)
    out = call(qenv, "cfg_q", "POST", "/admin/query-commands",
              {"kind": "query_preview", "company_id": qenv["c"]["id"], "extract_sql": "SELECT 1 AS id"},
              expect=201)
    cid = out["id"]
    cmd = claim(b.url, h)
    report(b.url, h, cid, {"status": "ok", "columns": [{"name": "id", "source_type": "int"}],
                           "row_count": 1, "rows": [[1]], "agent_version": "5.4.0"})
    # "viewer_q" tiene lectura sobre el mismo grupo pero no pidió el comando: sin filas.
    got = call(qenv, "viewer_q", "GET", f"/admin/query-commands/{cid}?include_rows=true", expect=200)
    assert not got.get("rows")


def test_ajeno_no_ve_el_comando(qenv):
    b = qenv["b"]
    token = _company_token(qenv, qenv["c"])
    h = enroll(b.url, token, name="agente3")
    heartbeat(b.url, h)
    out = call(qenv, "cfg_q", "POST", "/admin/query-commands",
              {"kind": "query_preview", "company_id": qenv["c"]["id"], "extract_sql": "SELECT 1"}, expect=201)
    call(qenv, "ajeno_q", "GET", f"/admin/query-commands/{out['id']}", expect=404)


def test_allow_data_preview_false_bloquea_filas_aunque_el_agente_las_mande(qenv):
    b = qenv["b"]
    token = _company_token(qenv, qenv["c_noprev"])
    h = enroll(b.url, token, name="agente_sin_muestra")
    heartbeat(b.url, h)
    out = call(qenv, "cfg_q", "POST", "/admin/query-commands",
              {"kind": "query_preview", "company_id": qenv["c_noprev"]["id"], "extract_sql": "SELECT 1 AS id"},
              expect=201)
    cid = out["id"]
    cmd = claim(b.url, h)
    assert cmd["include_rows"] is False
    # Un agente defectuoso u hostil que igual mande filas: el backend las descarta.
    report(b.url, h, cid, {"status": "ok", "columns": [{"name": "id", "source_type": "int"}],
                           "row_count": 1, "rows": [[1]], "agent_version": "5.4.0"})
    got = call(qenv, "cfg_q", "GET", f"/admin/query-commands/{cid}?include_rows=true", expect=200)
    assert got["has_sample_rows"] is False
    assert not got.get("rows")
    (has_pending,) = q("SELECT has_pending_rows FROM agent_command WHERE id = %s", (cid,))
    assert has_pending[0] is False


def test_solo_config_manage_puede_pedir_comandos(qenv):
    call(qenv, "viewer_q", "POST", "/admin/query-commands",
        {"kind": "query_preview", "company_id": qenv["c"]["id"], "extract_sql": "SELECT 1"}, expect=403)


def test_create_table_genera_ddl_y_lo_ejecuta_el_agente(qenv):
    b = qenv["b"]
    token = _company_token(qenv, qenv["c"])
    h = enroll(b.url, token, name="agente_ddl")
    heartbeat(b.url, h)
    out = call(qenv, "cfg_q", "POST", "/admin/query-commands", {
        "kind": "create_table", "company_id": qenv["c"]["id"], "destination_table": "clientes_nuevo",
        "columns": [{"name": "id", "pg_type": "integer", "nullable": False},
                   {"name": "nombre", "pg_type": "text", "nullable": True}],
        "key_columns": ["id"],
    }, expect=201)
    cid = out["id"]
    cmd = claim(b.url, h)
    assert cmd["kind"] == "create_table"
    assert "CREATE TABLE IF NOT EXISTS" in cmd["ddl"]
    assert '"id" integer NOT NULL' in cmd["ddl"]
    assert cmd["warehouse"]["host"] == "dwh.invalid"
    r = report(b.url, h, cid, {"status": "ok", "duration_ms": 10, "agent_version": "5.4.0"})
    assert r.status_code == 200
    got = call(qenv, "cfg_q", "GET", f"/admin/query-commands/{cid}", expect=200)
    assert got["status"] == "ok"


def test_create_table_rechaza_nombre_no_valido(qenv):
    call(qenv, "cfg_q", "POST", "/admin/query-commands", {
        "kind": "create_table", "company_id": qenv["c"]["id"], "destination_table": "clientes; drop table x",
        "columns": [{"name": "id", "pg_type": "integer"}],
    }, expect=422)


def test_limite_de_intervalo_minimo(qenv):
    b = qenv["b"]
    token = _company_token(qenv, qenv["c"])
    h = enroll(b.url, token, name="agente_rl")
    heartbeat(b.url, h)
    # Aísla la prueba del tope "abiertas por grupo": las pruebas anteriores pueden haber dejado
    # comandos pendientes sin reclamar (p. ej. test_ajeno_no_ve_el_comando). Aquí solo se quiere
    # probar el intervalo mínimo entre dos solicitudes IDÉNTICAS, no el tope de abiertas.
    q("DELETE FROM agent_command WHERE group_id = %s", (qenv["g"]["id"],))
    body = {"kind": "query_preview", "company_id": qenv["c"]["id"], "extract_sql": "SELECT 1 AS x"}
    call(qenv, "cfg_q", "POST", "/admin/query-commands", body, expect=201)
    r = call(qenv, "cfg_q", "POST", "/admin/query-commands", body, expect=429)
    assert r["detail"]["code"] == "too_soon"
