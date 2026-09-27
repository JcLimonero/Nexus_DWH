"""
Inventario estructural con bases PostgreSQL REALES (fase 3).

Backend (subproceso) + contenedor DWH local (nexus_dwh_test_dwh, 5548) +
agente real (InventoryRunner con su API). Se crea una base de pruebas propia
(nexus_inv_it) y un rol de SOLO LECTURA (nexus_inv_ro) que es el que usa el
inventario; el DDL de las pruebas lo ejecuta el superusuario del contenedor.
Nunca se toca una base de cliente real.
"""

import os
import uuid

import pytest

import support
from nexus_agent.agent import Agent
from nexus_agent.inventory import InventoryRunner, collect_postgres, connect_readonly_pg
from nexus_agent.logsetup import setup_logging
from nexus_agent.settings import load_settings

DBNAME = "nexus_test_cfg_inventory_it"
INV_DB = "nexus_inv_it"
RO_USER, RO_PASS = "nexus_inv_ro", "inv-ro-pass-3Hq"
MARKER = "nx_view_literal_" + uuid.uuid4().hex[:6]


def inv_exec(sql, params=None):
    c = support.pg(support.DWH, INV_DB)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
    finally:
        c.close()


def setup_inventory_db():
    admin = support.pg(support.DWH, "postgres")
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{INV_DB}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{INV_DB}"')
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RO_USER,))
        if cur.fetchone():
            cur.execute(f'ALTER ROLE "{RO_USER}" LOGIN PASSWORD %s', (RO_PASS,))
        else:
            cur.execute(f'CREATE ROLE "{RO_USER}" LOGIN NOSUPERUSER PASSWORD %s', (RO_PASS,))
    admin.close()
    inv_exec(f"""
        GRANT CONNECT ON DATABASE "{INV_DB}" TO "{RO_USER}";
        CREATE SCHEMA ventas; CREATE SCHEMA privado;
        GRANT USAGE ON SCHEMA ventas, privado TO "{RO_USER}";
        CREATE TABLE ventas.pedidos (id int PRIMARY KEY, cliente text, total numeric(10,2), estado text,
                                     creado timestamp DEFAULT now());
        CREATE INDEX pedidos_cliente_idx ON ventas.pedidos (cliente);
        CREATE TABLE ventas.temporal (id int);
        CREATE TABLE ventas.detalle (id int, pedido_id int REFERENCES ventas.pedidos(id), cantidad int);
        CREATE VIEW ventas.v_pedidos AS SELECT id, cliente, total FROM ventas.pedidos WHERE estado <> '{MARKER}';
        CREATE VIEW ventas.v_vieja AS SELECT id FROM ventas.temporal;
        CREATE TABLE privado.nomina (id int PRIMARY KEY, sueldo numeric);
        CREATE VIEW privado.v_nomina AS SELECT id FROM privado.nomina;
    """)


@pytest.fixture(scope="module")
def ienv(tmp_path_factory):
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.ensure_container(support.SRC)
    support.ensure_container(support.DWH)
    support.seed_source()
    support.reset_dwh()
    setup_inventory_db()
    support.recreate_config_db(DBNAME)
    b = support.Backend(DBNAME, extra_ini={"health": {"evaluator_interval_seconds": "0"},
                                           "notifications": {"worker_interval_seconds": "0"}})
    b.start()
    base = tmp_path_factory.mktemp("inv_agents")
    try:
        ids = support.seed_config(b)
        g = b.admin("POST", "/admin/groups", {"name": "Grupo Inventario", "warehouse_host": support.DWH["host"],
                                              "warehouse_port": support.DWH["port"], "warehouse_database": INV_DB,
                                              "warehouse_username": RO_USER, "warehouse_password": RO_PASS},
                    expect=201)
        c = b.admin("POST", "/admin/companies", {"group_id": g["id"], "name": "Empresa Inventario",
                                                 "source_type": "postgresql", "source_host": support.SRC["host"],
                                                 "source_port": support.SRC["port"],
                                                 "source_database": support.SRC["database"],
                                                 "source_username": support.SRC["username"],
                                                 "source_password": support.SRC["password"]}, expect=201)
        a1 = b.admin("POST", "/admin/agencies", {"company_id": c["id"], "name": "Inv Centro"}, expect=201)
        a2 = b.admin("POST", "/admin/agencies", {"company_id": c["id"], "name": "Inv Norte"}, expect=201)
        yield {"backend": b, "ids": ids, "base": base, "group": g, "company": c, "a1": a1, "a2": a2, "agents": {}}
    finally:
        b.stop()
        support.drop_config_db(DBNAME)
        admin = support.pg(support.DWH, "postgres")
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{INV_DB}" WITH (FORCE)')
        admin.close()


def make_agent(env, name, token_kind, token):
    d = env["base"] / name
    d.mkdir(exist_ok=True)
    lines = ["[nexus]", f"api_url = {env['backend'].url}", f"{token_kind} = {token}", "mode = development", "",
             "[agent]", f"data_dir = {d / 'data'}", f"log_dir = {d / 'logs'}", "db_connect_timeout_seconds = 3",
             "task_retry_attempts = 0", "queue_backoff_base_seconds = 0.2", "queue_backoff_max_seconds = 1"]
    (d / "config.ini").write_text("\n".join(lines) + "\n")
    s = load_settings(str(d / "config.ini"))
    setup_logging(s.log_dir, console=False)
    a = Agent(s)
    a.ensure_credential()
    assert a.refresh_config()
    runner = InventoryRunner(s, a.api, a.fresh_config)
    env["agents"][name] = (a, runner)
    return a, runner


def mdb_for(env, key_group):
    items = env["backend"].admin("GET", f"/admin/monitored-databases?group_id={key_group}", expect=200)["items"]
    assert len(items) == 1, items
    return items[0]


def scan(env, runner, mdb_id):
    env["backend"].admin("POST", f"/admin/monitored-databases/{mdb_id}/scan", expect=200)
    out = runner.tick()
    sent = [r for r in out["results"] if r["id"] == mdb_id]
    assert len(sent) == 1 and sent[0]["sent"], out
    return sent[0]


def pending(env, mdb_id):
    return env["backend"].admin(
        "GET", f"/admin/structural-changes?view=pending&monitored_database_id={mdb_id}", expect=200)["items"]


def ack_all(env, mdb_id, attribution="client"):
    b = env["backend"]
    for c in pending(env, mdb_id):
        b.admin("POST", f"/admin/structural-changes/{c['id']}/acknowledge",
                {"attribution": attribution, "expected_version": c["row_version"],
                 "expected_observed_fingerprint": c["observed_fingerprint"]}, expect=200)


# ─────────────────────────────────────────────────────────────────────────────
def test_inv_01_dos_agentes_mismo_dwh_un_solo_inventario_y_aprobacion(ienv):
    b = ienv["backend"]
    _, r1 = make_agent(ienv, "inv1", "agency_token", ienv["a1"]["agency_token"])
    _, r2 = make_agent(ienv, "inv2", "agency_token", ienv["a2"]["agency_token"])
    o1, o2 = r1.tick(), r2.tick()
    sent = [r for r in o1["results"] + o2["results"] if r.get("sent")]
    assert len(sent) == 1, (o1, o2)                       # solo el responsable inventaría
    m = mdb_for(ienv, ienv["group"]["id"])
    assert m["state"] == "baseline_pending" and m["effective_status"] == "verified"
    assert m["engine_identity_strength"] in ("strong", "weak")
    ienv["mdb"] = m["id"]
    ienv["holder"] = r1 if o1["results"] else r2
    ienv["other"] = r2 if ienv["holder"] is r1 else r1
    c = support.cfg_conn(DBNAME)
    with c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM inventory_snapshot WHERE monitored_database_id = %s", (m["id"],))
        assert cur.fetchone()[0] == 1
    c.close()
    prop = b.admin("GET", f"/admin/monitored-databases/{m['id']}/baseline", expect=200)
    names = {(i["schema_name"], i["name"], i["type"]) for i in prop["items"]}
    assert names == {("ventas", "pedidos", "table"), ("ventas", "temporal", "table"), ("ventas", "detalle", "table"),
                     ("ventas", "v_pedidos", "view"), ("ventas", "v_vieja", "view"), ("privado", "nomina", "table"),
                     ("privado", "v_nomina", "view")}
    ped = [i for i in prop["items"] if i["name"] == "pedidos"][0]["structure"]
    assert ped["columns"]["total"]["type"] == "numeric(10,2)" and "pedidos_pkey" in ped["constraints"]
    assert ped["indexes"]["pedidos_cliente_idx"]["definition"] == "USING btree (cliente)"
    assert b.admin("GET", f"/admin/structural-changes?monitored_database_id={m['id']}", expect=200)["items"] == []
    b.admin("POST", f"/admin/monitored-databases/{m['id']}/baseline/approve",
            {"expected_snapshot_id": prop["snapshot"]["id"]}, expect=200)
    # Sin cambios → sin alertas.
    scan(ienv, ienv["holder"], m["id"])
    assert pending(ienv, m["id"]) == []


def test_inv_02_crear_modificar_y_eliminar_tablas_y_vistas_reales(ienv):
    mdb = ienv["mdb"]
    inv_exec(f"""
        CREATE TABLE ventas.nueva (id int);
        DROP VIEW ventas.v_pedidos;
        ALTER TABLE ventas.pedidos ALTER COLUMN total TYPE numeric(14,2);
        ALTER TABLE ventas.pedidos ALTER COLUMN cliente SET NOT NULL;
        ALTER TABLE ventas.pedidos ALTER COLUMN estado SET DEFAULT 'nuevo';
        ALTER TABLE ventas.pedidos ADD CONSTRAINT pedidos_total_ck CHECK (total >= 0);
        CREATE INDEX pedidos_estado_idx ON ventas.pedidos (estado);
        CREATE VIEW ventas.v_pedidos AS
            SELECT id, cliente, total FROM ventas.pedidos WHERE estado <> '{MARKER}' AND total > 0;
        DROP VIEW ventas.v_vieja;
        DROP TABLE ventas.temporal;
        CREATE MATERIALIZED VIEW ventas.mv_totales AS SELECT cliente, sum(total) AS total FROM ventas.pedidos
            GROUP BY cliente;
    """)
    res = scan(ienv, ienv["holder"], mdb)
    assert res["status"] == "complete"
    by = {c["object_name"]: c for c in pending(ienv, mdb)}
    assert {k: v["change_kind"] for k, v in by.items()} == {
        "nueva": "object_added", "pedidos": "object_modified", "v_pedidos": "object_modified",
        "v_vieja": "object_removed", "temporal": "object_removed", "mv_totales": "object_added"}
    assert set(by["pedidos"]["change_types"]) >= {"column_type_changed", "column_nullability_changed",
                                                  "column_default_changed", "constraint_added", "index_added"}
    assert "view_definition_changed" in by["v_pedidos"]["change_types"]
    assert by["mv_totales"]["object_type"] == "matview"
    det = ienv["backend"].admin("GET", f"/admin/structural-changes/{by['pedidos']['id']}", expect=200)
    tc = [x for x in det["diffs"] if x["kind"] == "column_type_changed"][0]
    assert (tc["item"], tc["before"], tc["after"]) == ("total", "numeric(10,2)", "numeric(14,2)")
    # Dar por entendido con ambas opciones de responsable.
    b = ienv["backend"]
    for name, who in (("nueva", "client"), ("pedidos", "nexus"), ("v_pedidos", "client"), ("v_vieja", "nexus"),
                      ("temporal", "client"), ("mv_totales", "nexus")):
        c = by[name]
        b.admin("POST", f"/admin/structural-changes/{c['id']}/acknowledge",
                {"attribution": who, "expected_version": c["row_version"],
                 "expected_observed_fingerprint": c["observed_fingerprint"], "ticket_ref": f"T-{name}"}, expect=200)
    scan(ienv, ienv["holder"], mdb)
    assert pending(ienv, mdb) == []
    hist = b.admin("GET", f"/admin/structural-changes?view=history&monitored_database_id={mdb}", expect=200)["items"]
    assert {h["attribution"] for h in hist} == {"client", "nexus"} and len(hist) == 6


def test_inv_03_normalizacion_sin_falsos_positivos(ienv):
    mdb = ienv["mdb"]
    inv_exec(f"""
        -- misma vista con otro formato (espacios, saltos de línea, mayúsculas)
        CREATE OR REPLACE VIEW ventas.v_pedidos AS select   id ,
               cliente,total
          from ventas.pedidos   where estado <> '{MARKER}'   and total>0 ;
        -- mismo índice recreado
        DROP INDEX ventas.pedidos_estado_idx;
        CREATE   INDEX   pedidos_estado_idx ON ventas.pedidos USING btree ( estado );
        -- columna eliminada y vuelta a agregar igual (cambia el orden físico, no la estructura)
        ALTER TABLE ventas.detalle DROP COLUMN cantidad;
        ALTER TABLE ventas.detalle ADD COLUMN cantidad int;
        -- vista materializada refrescada (datos, no estructura)
        REFRESH MATERIALIZED VIEW ventas.mv_totales;
    """)
    scan(ienv, ienv["holder"], mdb)
    assert pending(ienv, mdb) == []


def test_inv_04_perdida_de_permisos_no_produce_eliminaciones(ienv):
    mdb = ienv["mdb"]
    inv_exec(f'REVOKE USAGE ON SCHEMA privado FROM "{RO_USER}"')
    try:
        res = scan(ienv, ienv["holder"], mdb)
        assert res["status"] == "partial" and res["reason_code"] == "SCHEMAS_UNVERIFIABLE"
        assert pending(ienv, mdb) == []
        m = mdb_for(ienv, ienv["group"]["id"])
        assert m["effective_status"] == "partial"
        base = ienv["backend"].admin("GET", f"/admin/monitored-databases/{mdb}/baseline?view=approved",
                                     expect=200)["items"]
        assert {"nomina", "v_nomina"} <= {i["name"] for i in base}
    finally:
        inv_exec(f'GRANT USAGE ON SCHEMA privado TO "{RO_USER}"')
    res = scan(ienv, ienv["holder"], mdb)
    assert res["status"] == "complete" and pending(ienv, mdb) == []


def test_inv_05_conexion_perdida_no_se_pudo_verificar(ienv):
    mdb = ienv["mdb"]
    before = mdb_for(ienv, ienv["group"]["id"])
    inv_exec(f'ALTER ROLE "{RO_USER}" NOLOGIN')
    try:
        res = scan(ienv, ienv["holder"], mdb)
        assert res["status"] == "unreliable" and res["reason_code"].startswith("DWH_")
        m = mdb_for(ienv, ienv["group"]["id"])
        assert m["effective_status"] == "unverifiable"
        assert m["last_verified_at"] == before["last_verified_at"]      # se conserva la referencia anterior
        assert pending(ienv, mdb) == []
        assert m["baseline_objects"] == before["baseline_objects"]
    finally:
        inv_exec(f'ALTER ROLE "{RO_USER}" LOGIN')
    res = scan(ienv, ienv["holder"], mdb)
    assert res["status"] == "complete" and pending(ienv, mdb) == []
    assert mdb_for(ienv, ienv["group"]["id"])["effective_status"] == "verified"


def test_inv_06_sesion_de_solo_lectura(ienv):
    s = ienv["agents"]["inv1"][0].settings
    # Incluso con credenciales de superusuario la sesión del inventario no puede escribir.
    conn = connect_readonly_pg({"host": support.DWH["host"], "port": support.DWH["port"], "database": INV_DB,
                                "username": support.DWH["username"], "password": support.DWH["password"]}, s)
    try:
        cur = conn.cursor()
        with pytest.raises(Exception) as ei:
            cur.execute("CREATE TABLE ventas.no_debe_existir (id int)")
        assert getattr(ei.value, "pgcode", "") == "25006"       # read_only_sql_transaction
        conn.rollback()
        data = collect_postgres(conn, ["ventas"], [], view_definitions=False)
        assert {o["schema_name"] for o in data["objects"]} == {"ventas"}
        assert all("definition" not in o for o in data["objects"])
    finally:
        conn.close()
    c = support.pg(support.DWH, INV_DB)
    with c.cursor() as cur:
        cur.execute("SELECT to_regclass('ventas.no_debe_existir')")
        assert cur.fetchone()[0] is None
    c.close()


def test_inv_07_el_otro_agente_toma_el_relevo_si_el_responsable_cae(ienv):
    mdb = ienv["mdb"]
    c = support.cfg_conn(DBNAME)
    with c.cursor() as cur:
        cur.execute("UPDATE monitored_database SET lease_until = NOW() - INTERVAL '1 second' WHERE id = %s", (mdb,))
    c.close()
    scan(ienv, ienv["other"], mdb)
    assert pending(ienv, mdb) == []                  # misma línea base, sin alertas duplicadas
    out = ienv["holder"].tick()
    assert [r for r in out["results"] if r["id"] == mdb] == []
    ienv["holder"], ienv["other"] = ienv["other"], ienv["holder"]


def test_inv_08_evidencia_de_ddl_nexus_sin_atribucion(ienv):
    b, ids = ienv["backend"], ienv["ids"]
    a, runner = make_agent(ienv, "etl_a", "group_token", ids["group_a"]["group_token"])
    tid = ids["t_cli"]["id"]
    cfg = a.fresh_config()
    task = [t for t in cfg["tasks"] if t["task_id"] == tid][0]
    assert a.execute_task(task, cfg["warehouse"])["status"] == "success"
    assert a.flush(30)
    runner.tick()
    m = [x for x in b.admin("GET", f"/admin/monitored-databases?group_id={ids['group_a']['id']}", expect=200)["items"]
         if x["kind"] == "dwh"][0]
    prop = b.admin("GET", f"/admin/monitored-databases/{m['id']}/baseline", expect=200)
    cli = [i for i in prop["items"] if i["name"] == "clientes"][0]
    assert cli["nexus_catalog_match"] is True                     # evidencia, no atribución
    b.admin("POST", f"/admin/monitored-databases/{m['id']}/baseline/approve",
            {"expected_snapshot_id": prop["snapshot"]["id"]}, expect=200)
    # El catálogo trae una columna nueva: el agente Nexus la agrega (ensure_columns_exist).
    b.admin("PUT", f"/admin/tasks/{tid}", {
        "extract_sql": "SELECT id, nombre, updated_at, 'web'::text AS canal FROM src_clientes "
                       "WHERE updated_at >= '{last_run}' ORDER BY id"}, expect=200)
    assert a.refresh_config()
    src = support.pg(support.SRC)
    with src.cursor() as cur:   # filas nuevas en la ventana del watermark
        cur.execute("UPDATE src_clientes SET updated_at = LOCALTIMESTAMP WHERE id <= 3")
    src.close()
    cfg = a.fresh_config()
    task = [t for t in cfg["tasks"] if t["task_id"] == tid][0]
    out = a.execute_task(task, cfg["warehouse"])
    assert out["status"] == "success"
    assert a.flush(30)
    b.admin("POST", f"/admin/monitored-databases/{m['id']}/scan", expect=200)
    runner.tick()
    ch = [c for c in b.admin("GET", f"/admin/structural-changes?view=pending&monitored_database_id={m['id']}",
                             expect=200)["items"] if c["object_name"] == "clientes"]
    assert len(ch) == 1 and "column_added" in ch[0]["change_types"]
    det = b.admin("GET", f"/admin/structural-changes/{ch[0]['id']}", expect=200)
    ev = [e for e in det["evidence"] if e["type"] == "nexus_execution"]
    assert ev and ev[0]["execution_id"] == out["execution_id"] and ev[0]["columns"] == ["canal"]
    assert det["attribution"] is None and det["status"] == "pending"


def test_inv_09_sin_sql_de_vistas_ni_secretos_en_logs_ni_en_nexus(ienv):
    b = ienv["backend"]
    texts = [open(b.log_path, encoding="utf-8", errors="replace").read()]
    for a, _ in ienv["agents"].values():
        for f in os.listdir(a.settings.log_dir):
            texts.append(open(os.path.join(a.settings.log_dir, f), encoding="utf-8", errors="replace").read())
    blob = "\n".join(texts)
    assert MARKER not in blob and RO_PASS not in blob
    c = support.cfg_conn(DBNAME)
    with c.cursor() as cur:
        for sql in ("SELECT structure::text FROM inventory_object_state",
                    "SELECT COALESCE(diffs::text,'') || COALESCE(previous_structure::text,'') || "
                    "COALESCE(current_structure::text,'') FROM structural_change",
                    "SELECT COALESCE(definition_enc,'') FROM inventory_object_state",
                    "SELECT COALESCE(error_detail,'') FROM activity_log"):
            cur.execute(sql)
            assert all(MARKER not in (r[0] or "") for r in cur.fetchall()), sql
    c.close()


def test_inv_10_particiones_agrupadas_bajo_la_tabla_raiz(ienv):
    mdb = ienv["mdb"]
    inv_exec("""CREATE TABLE ventas.movs (id int, fecha date NOT NULL) PARTITION BY RANGE (fecha);
                CREATE TABLE ventas.movs_2024 PARTITION OF ventas.movs FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
                CREATE TABLE ventas.movs_2025 PARTITION OF ventas.movs FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');""")
    scan(ienv, ienv["holder"], mdb)
    p = pending(ienv, mdb)
    assert [(c["object_name"], c["change_kind"]) for c in p] == [("movs", "object_added")]   # sin una alerta por partición
    ack_all(ienv, mdb)
    # Partición nueva + índice en la raíz (que PostgreSQL propaga a cada partición) → UNA alerta en la raíz.
    inv_exec("""CREATE TABLE ventas.movs_2026 PARTITION OF ventas.movs FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
                CREATE INDEX movs_fecha_idx ON ventas.movs (fecha);""")
    scan(ienv, ienv["holder"], mdb)
    p = pending(ienv, mdb)
    assert [c["object_name"] for c in p] == ["movs"], p
    assert {"index_added", "object_attr_changed"} <= set(p[0]["change_types"])
    det = ienv["backend"].admin("GET", f"/admin/structural-changes/{p[0]['id']}", expect=200)
    parts = det["current_structure"]["partitions"]
    assert set(parts) == {"ventas.movs_2024", "ventas.movs_2025", "ventas.movs_2026"}
    ack_all(ienv, mdb, "nexus")


def test_inv_11_ddl_de_restriccion_sin_cambios_no_es_evidencia(ienv):
    b, ids = ienv["backend"], ienv["ids"]
    a, _ = ienv["agents"]["etl_a"]
    tid = ids["t_cli"]["id"]
    obj_id = [o for o in b.admin("GET", "/admin/objects", expect=200)["items"]
              if o["destination_table"] == "dwh.clientes"][0]["id"]
    b.admin("PUT", f"/admin/objects/{obj_id}", {
        "constraint_name": "ck_nx_id",
        "create_constraint_sql": "ALTER TABLE dwh.clientes DROP CONSTRAINT IF EXISTS ck_nx_id; "
                                 "ALTER TABLE dwh.clientes ADD CONSTRAINT ck_nx_id CHECK (id IS NOT NULL)"}, expect=200)
    ex_ids = []
    for _ in range(2):
        assert a.refresh_config()
        cfg = a.fresh_config()
        task = [t for t in cfg["tasks"] if t["task_id"] == tid][0]
        out = a.execute_task(task, cfg["warehouse"])
        assert out["status"] == "success", out
        assert "CONSTRAINT_DDL_FAILED" not in out["result"].warnings, out["result"].warnings
        ex_ids.append(out["execution_id"])
    assert a.flush(30)
    c = support.cfg_conn(DBNAME)
    with c.cursor() as cur:
        cur.execute("SELECT execution_id::text, ddl_applied FROM task_execution WHERE execution_id::text = ANY(%s)",
                    (ex_ids,))
        got = {r[0]: [d["action"] for d in r[1]] for r in cur.fetchall()}
    c.close()
    assert "constraint_ddl" in got[ex_ids[0]]          # la primera vez sí creó la restricción
    assert "constraint_ddl" not in got[ex_ids[1]]      # re-ejecutarla sin cambios no es evidencia


# ── Runner: backoff por base y 413 (sin BD) ─────────────────────────────────
class _FakeApi:
    def __init__(self, errors):
        self.errors = list(errors)
        self.sent = []

    def inventory_lease(self, payload):
        return {"targets": [{"monitored_database_id": 7, "kind": "dwh", "granted": True, "due": True,
                             "schema_include": [], "schema_exclude": [], "max_objects": 10}]}

    def inventory_snapshot(self, body, compress=True):
        self.sent.append(dict(body))
        if self.errors:
            raise self.errors.pop(0)
        return {"status": "ok"}


def _runner(errors, clock):
    from nexus_agent.settings import Settings
    api = _FakeApi(errors)

    def boom(params, settings):
        raise OSError("sin red")

    cfg = {"warehouse": {"host": "127.0.0.1", "port": 5432, "database": "x", "username": "u", "password": "p"},
           "tasks": []}
    r = InventoryRunner(Settings(inventory_tick_seconds=60), api, lambda: cfg, connect=boom, clock=clock)
    return r, api


def test_inv_runner_backoff_si_el_envio_falla():
    from nexus_agent.api import ApiUnavailable
    now = [1000.0]
    r, api = _runner([ApiUnavailable(503, "server_error", "x"), ApiUnavailable(503, "server_error", "x")],
                     lambda: now[0])
    out = r.tick()
    assert out["results"][0]["sent"] is False and out["results"][0]["retry_in"] == 60
    assert r.tick()["results"][0]["code"] == "backoff" and len(api.sent) == 1   # no re-inventaría cada ciclo
    now[0] += 61
    out = r.tick()
    assert out["results"][0]["retry_in"] == 120 and len(api.sent) == 2        # exponencial
    now[0] += 121
    assert r.tick()["results"][0]["sent"] is True and len(api.sent) == 3
    assert r._backoff == {}


def test_inv_runner_413_envia_estado_pequeno():
    from nexus_agent.api import ApiRejected
    r, api = _runner([ApiRejected(413, "body_too_large", "grande")], lambda: 0.0)
    out = r.tick()
    assert out["results"][0]["sent"] is True
    assert api.sent[-1]["status"] == "unreliable" and api.sent[-1]["reason_code"] == "PAYLOAD_TOO_LARGE"
    assert api.sent[-1]["objects"] == []
