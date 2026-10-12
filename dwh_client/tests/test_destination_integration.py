"""
Integración real del destino configurable (sección 22): backend (subproceso) +
origen y DWH PostgreSQL (contenedores Docker locales) + agente en proceso.

  * empresa que HEREDA el destino del grupo → esquema destino del grupo;
  * empresa con destino PROPIO (otra base del contenedor DWH y otro esquema),
    incluido el DDL del catálogo sin esquema (search_path);
  * sslmode disable/prefer contra el contenedor (sin SSL) y require → falla
    limpia (DWH_SSL_ERROR) tanto en la carga como en la prueba de conexión;
  * "Probar conexión" ejecutada por el agente: correcta, contraseña errónea,
    usuario sin privilegios y origen; sin credenciales en resultados ni logs;
  * inventario: el destino propio es su propia base monitoreada, sin duplicados.
"""

import glob
import os
import secrets

import psycopg2
import pytest
import requests

import support
from nexus_agent import AGENT_VERSION
from nexus_agent.agent import Agent
from nexus_agent.destination import task_warehouse
from nexus_agent.inventory import InventoryRunner
from nexus_agent.logsetup import setup_logging
from nexus_agent.settings import load_settings

DBNAME = "nexus_test_cfg_dest_client"
EMP_DB = "dwh_empresa_test"
RO_ROLE = "nx_test_sin_privilegios"
RO_PASS = "ro-" + secrets.token_urlsafe(12)
RW_ROLE = "nx_test_escritor_esquema"
RW_PASS = "rw-" + secrets.token_urlsafe(12)
T = 15


def dwh_admin(dbname="postgres"):
    return support.pg(support.DWH, dbname)


def dwh_q(dbname, sql, params=()):
    c = dwh_admin(dbname)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


def cfg_q(sql, params=()):
    c = support.cfg_conn(DBNAME)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


@pytest.fixture(scope="module")
def denv(tmp_path_factory):
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.ensure_container(support.SRC)
    support.ensure_container(support.DWH)
    support.seed_source()
    c = dwh_admin()
    with c.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{EMP_DB}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{EMP_DB}"')
        cur.execute(f"DROP ROLE IF EXISTS {RO_ROLE}")
        cur.execute(f"CREATE ROLE {RO_ROLE} LOGIN PASSWORD %s", (RO_PASS,))
        cur.execute(f"DROP ROLE IF EXISTS {RW_ROLE}")
        cur.execute(f"CREATE ROLE {RW_ROLE} LOGIN PASSWORD %s", (RW_PASS,))
    c.close()
    dwh_q(support.DWH["database"], "DROP SCHEMA IF EXISTS grupo_sch CASCADE")
    support.recreate_config_db(DBNAME)
    # Sin límites de frecuencia de la prueba de conexión (se prueban aparte, en dwh_back).
    b = support.Backend(DBNAME, extra_ini={"connection_test": {"min_interval_seconds": "0",
                                                               "max_per_user_per_minute": "1000"}})
    b.start()
    base = tmp_path_factory.mktemp("dest_agents")
    D, S = support.DWH, support.SRC
    try:
        g = b.admin("POST", "/admin/groups", {
            "name": "Grupo Destino Real", "warehouse_host": D["host"], "warehouse_port": D["port"],
            "warehouse_database": D["database"], "warehouse_username": D["username"],
            "warehouse_password": D["password"], "warehouse_schema": "grupo_sch", "warehouse_sslmode": "disable"},
            expect=201)
        src = {"source_type": "postgresql", "source_host": S["host"], "source_port": S["port"],
               "source_database": S["database"], "source_username": S["username"], "source_password": S["password"]}
        c_inh = b.admin("POST", "/admin/companies", {"group_id": g["id"], "name": "Hereda", **src}, expect=201)
        c_own = b.admin("POST", "/admin/companies", {
            "group_id": g["id"], "name": "Propia", **src, "warehouse_mode": "custom",
            "warehouse_host": D["host"], "warehouse_port": D["port"], "warehouse_database": EMP_DB,
            "warehouse_username": D["username"], "warehouse_password": D["password"],
            "warehouse_schema": "emp_sch", "warehouse_sslmode": "prefer"}, expect=201)
        tasks = {}
        for comp in (c_inh, c_own):
            ag = b.admin("POST", "/admin/agencies", {"company_id": comp["id"], "name": "Ag " + comp["name"]},
                         expect=201)
            o1 = b.admin("POST", "/admin/objects", {"company_id": comp["id"], "name": "Clientes",
                                                    "destination_table": "clientes_dest", "upsert_keys": "id"},
                         expect=201)
            o2 = b.admin("POST", "/admin/objects", {
                "company_id": comp["id"], "name": "ConDDL", "destination_table": "ddl_tabla", "upsert_keys": "id",
                "create_table_sql": "CREATE TABLE IF NOT EXISTS ddl_tabla (id INT PRIMARY KEY, nombre TEXT)"},
                expect=201)
            tasks[comp["id"]] = [b.admin("POST", "/admin/tasks", {
                "agency_id": ag["id"], "object_catalog_id": o["id"], "schedule_seconds": 3600,
                "extract_sql": "SELECT id, nombre FROM src_clientes WHERE id <= 10 ORDER BY id"},
                expect=201)["id"] for o in (o1, o2)]
        yield dict(b=b, g=g, c_inh=c_inh, c_own=c_own, tasks=tasks, base=base, agents=[])
    finally:
        b.stop()
        support.drop_config_db(DBNAME)
        c = dwh_admin()
        with c.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{EMP_DB}" WITH (FORCE)')
        c.close()
        dwh_q(support.DWH["database"], "DROP SCHEMA IF EXISTS grupo_sch CASCADE")
        for role in (RO_ROLE, RW_ROLE):
            dwh_q(support.DWH["database"], f"DROP OWNED BY {role}")
            dwh_q("postgres", f"DROP ROLE IF EXISTS {role}")


def make_agent(env, name):
    d = env["base"] / name
    d.mkdir(exist_ok=True)
    lines = ["[nexus]", f"api_url = {env['b'].url}", f"group_token = {env['g']['group_token']}",
             "mode = development", "", "[agent]", f"data_dir = {d / 'data'}", f"log_dir = {d / 'logs'}",
             "heartbeat_seconds = 5", "tick_seconds = 0.2", "task_retry_attempts = 0",
             "db_connect_timeout_seconds = 3", "run_all_on_start = true",
             "connection_test_min_spacing_seconds = 0", "connection_test_max_per_minute = 100"]
    (d / "config.ini").write_text("\n".join(lines) + "\n")
    s = load_settings(str(d / "config.ini"))
    setup_logging(s.log_dir, console=False)
    a = Agent(s)
    a.ensure_credential()
    env["agents"].append(a)
    return a


def admin_test(env, body):
    return env["b"].admin("POST", "/admin/connection-tests", body, expect=201)


def run_test(env, a, body):
    """Pide la prueba desde el "panel" y la ejecuta el agente real (hilo de pruebas)."""
    assert a.send_heartbeat(a.api)
    t = admin_test(env, body)
    assert t["status"] == "pending", t
    done = a.run_one_connection_test(a.api)
    assert done and done["id"] == t["id"]
    return env["b"].admin("GET", f"/admin/connection-tests/{t['id']}", expect=200)


def test_01_carga_en_destino_del_grupo_y_propio_con_esquemas(denv):
    a = make_agent(denv, "grupo")
    assert a.run_once() == 0
    dwh = support.DWH["database"]
    # Hereda: esquema del grupo en la base del grupo.
    assert dwh_q(dwh, "SELECT COUNT(*) FROM grupo_sch.clientes_dest")[0][0] == 10
    assert dwh_q(dwh, "SELECT COUNT(*) FROM grupo_sch.ddl_tabla")[0][0] == 10
    # Propio: otra base y otro esquema; el DDL del catálogo sin esquema quedó en emp_sch (search_path).
    assert dwh_q(EMP_DB, "SELECT COUNT(*) FROM emp_sch.clientes_dest")[0][0] == 10
    assert dwh_q(EMP_DB, "SELECT COUNT(*) FROM emp_sch.ddl_tabla")[0][0] == 10
    assert dwh_q(EMP_DB, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' "
                         "AND table_name IN ('ddl_tabla', 'clientes_dest')")[0][0] == 0
    assert dwh_q(dwh, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' "
                      "AND table_name IN ('ddl_tabla', 'clientes_dest')")[0][0] == 0
    ok = cfg_q("SELECT COUNT(*) FROM task_execution WHERE status = 'success'")[0][0]
    assert ok == 4
    # Evidencia DDL con el objeto calificado.
    ddl = " ".join(r[0] for r in cfg_q("SELECT ddl_applied::text FROM task_execution"))
    assert "emp_sch.ddl_tabla" in ddl and "grupo_sch.clientes_dest" in ddl


def test_02_ssl_require_contra_servidor_sin_ssl_falla_limpio(denv):
    b, a = denv["b"], denv["agents"][0]
    b.admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_sslmode": "require"}, expect=200)
    try:
        a.refresh_config()
        cfg = a.fresh_config()
        task = next(t for t in cfg["tasks"] if t["task_id"] == denv["tasks"][denv["c_own"]["id"]][0])
        assert task["warehouse"]["sslmode"] == "require"
        out = a.execute_task(task, task_warehouse(task, cfg))
        assert out["status"] == "failed" and out["error_code"] == "DWH_SSL_ERROR", out
        # Prueba de conexión con el mismo destino.
        r = run_test(denv, a, {"target_kind": "company_dwh", "company_id": denv["c_own"]["id"]})
        assert r["status"] == "failed" and r["error_code"] == "DWH_SSL_ERROR"
        assert r["result"]["checks"][0]["code"] == "CONNECT" and r["result"]["checks"][0]["ok"] is False
    finally:
        b.admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_sslmode": "prefer"}, expect=200)


def test_03_prueba_de_conexion_correcta(denv):
    a = denv["agents"][0]
    r = run_test(denv, a, {"target_kind": "group_dwh", "group_id": denv["g"]["id"]})
    assert r["status"] == "ok", r
    res = r["result"]
    codes = {c["code"]: c for c in res["checks"]}
    assert codes["CONNECT"]["ok"] is True
    assert res["server_version"].startswith("PostgreSQL 16")
    assert res["ssl_in_use"] is False and codes["SSL"]["severity"] == "warning"   # sslmode=disable
    assert res["schema_exists"] is True and codes["CREATE_TABLE"]["ok"] is True
    assert res["agent_version"] == AGENT_VERSION and r["installation_name"]
    # Destino propio (prefer, esquema existente) y un esquema que aún no existe.
    r = run_test(denv, a, {"target_kind": "company_dwh", "company_id": denv["c_own"]["id"]})
    assert r["status"] == "ok" and r["result"]["schema_exists"] is True
    denv["b"].admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_schema": "nuevo_sch"}, expect=200)
    try:
        r = run_test(denv, a, {"target_kind": "company_dwh", "company_id": denv["c_own"]["id"]})
        codes = {c["code"]: c for c in r["result"]["checks"]}
        assert r["status"] == "ok" and r["result"]["schema_exists"] is False and codes["CREATE_SCHEMA"]["ok"]
        # Solo lectura: la prueba no creó el esquema.
        assert not dwh_q(EMP_DB, "SELECT 1 FROM pg_namespace WHERE nspname = 'nuevo_sch'")
    finally:
        denv["b"].admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_schema": "emp_sch"},
                        expect=200)
    # Origen de la empresa.
    r = run_test(denv, a, {"target_kind": "company_source", "company_id": denv["c_inh"]["id"]})
    assert r["status"] == "ok" and {c["code"] for c in r["result"]["checks"]} == {"CONNECT", "QUERY"}


def test_04_prueba_con_contrasena_erronea_y_sin_privilegios(denv):
    b, a, gid = denv["b"], denv["agents"][0], denv["g"]["id"]
    bad = "mala-" + secrets.token_urlsafe(10)
    b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_password": bad}, expect=200)
    try:
        r = run_test(denv, a, {"target_kind": "group_dwh", "group_id": gid})
        assert r["status"] == "failed" and r["error_code"] == "DWH_AUTH_FAILED", r
        assert bad not in str(r) and support.DWH["host"] not in str(r["message"] or "")
        b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_username": RO_ROLE, "warehouse_password": RO_PASS},
                expect=200)
        r = run_test(denv, a, {"target_kind": "group_dwh", "group_id": gid})
        codes = {c["code"]: c for c in r["result"]["checks"]}
        assert r["status"] == "failed" and r["error_code"] == "DWH_INSUFFICIENT_PRIVILEGE", r
        assert codes["CONNECT"]["ok"] is True and codes["SCHEMA_USAGE"]["ok"] is False
        assert RO_PASS not in str(r)
    finally:
        b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_username": support.DWH["username"],
                                                "warehouse_password": support.DWH["password"]}, expect=200)


def test_05_inventario_destino_propio_sin_duplicados(denv):
    a = denv["agents"][0]
    a.refresh_config()
    runner = InventoryRunner(a.settings, a.api, a.fresh_config)
    first = runner.tick()
    assert first.get("targets") == 2, first
    sent = [r for r in first["results"] if r.get("sent")]
    assert len(sent) == 2 and all(r["status"] == "complete" for r in sent), first
    assert runner.tick().get("targets") == 2
    rows = cfg_q("SELECT identity_key, display_name, verification_status FROM monitored_database "
                 "WHERE kind = 'dwh' ORDER BY id")
    assert len(rows) == 2
    names = " ".join(r[1] for r in rows)
    assert support.DWH["database"] in names and EMP_DB in names


def test_06_esquema_existente_sin_create_en_la_base(denv):
    """Usuario con USAGE+CREATE en el esquema destino pero SIN CREATE en la base: carga y crea tablas."""
    b, a, gid = denv["b"], denv["agents"][0], denv["g"]["id"]
    dwh = support.DWH["database"]
    dwh_q(dwh, f"GRANT USAGE, CREATE ON SCHEMA grupo_sch TO {RW_ROLE}")
    assert dwh_q(dwh, "SELECT has_database_privilege(%s, current_database(), 'CREATE')", (RW_ROLE,))[0][0] is False
    comp = denv["c_inh"]
    ag = b.admin("GET", f"/admin/agencies?company_id={comp['id']}", expect=200)["items"][0]
    o = b.admin("POST", "/admin/objects", {"company_id": comp["id"], "name": "Nueva", "destination_table": "tabla_nueva_rol",
                                           "upsert_keys": "id"}, expect=201)
    tid = b.admin("POST", "/admin/tasks", {"agency_id": ag["id"], "object_catalog_id": o["id"], "schedule_seconds": 3600,
                                           "extract_sql": "SELECT id, nombre FROM src_clientes WHERE id <= 3"},
                  expect=201)["id"]
    b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_username": RW_ROLE, "warehouse_password": RW_PASS,
                                            "reset_sync": False}, expect=200)
    try:
        a.refresh_config()
        cfg = a.fresh_config()
        task = next(t for t in cfg["tasks"] if t["task_id"] == tid)
        out = a.execute_task(task, task_warehouse(task, cfg))
        assert out["status"] == "success", out
        assert dwh_q(dwh, "SELECT COUNT(*) FROM grupo_sch.tabla_nueva_rol")[0][0] == 3
    finally:
        b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_username": support.DWH["username"],
                                                "warehouse_password": support.DWH["password"], "reset_sync": False},
                expect=200)
        b.admin("DELETE", f"/admin/tasks/{tid}", expect=200)
        b.admin("DELETE", f"/admin/objects/{o['id']}", expect=200)


def test_99_sin_credenciales_en_logs_ni_resultados(denv):
    secrets_ = [support.DWH["password"], support.SRC["password"], RO_PASS, RW_PASS, denv["g"]["group_token"]]
    for a in denv["agents"]:
        cred = a.holder.get()
        if cred:
            secrets_.append(cred.secret)
    files = glob.glob(os.path.join(str(denv["base"]), "*", "logs", "nexus_agent.log*")) + [denv["b"].log_path]
    assert files
    for f in files:
        text = open(f, encoding="utf-8", errors="replace").read()
        for s in secrets_:
            assert s not in text, f"{s[:4]}… en {os.path.basename(f)}"
    blob = " ".join(str(r) for r in cfg_q("SELECT result::text, message FROM connection_test"))
    for s in secrets_:
        assert s not in blob
    # El certificado/archivos del agente: no hay credenciales en data_dir fuera de la credencial.
    assert cfg_q("SELECT COUNT(*) FROM connection_test")[0][0] == 7
