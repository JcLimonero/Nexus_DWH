"""
Inventario estructural y "Dar por entendido" (fase 3) — lógica del backend.

BD de configuración propia (nexus_test_cfg_inventory) y backend en
subproceso. Los inventarios se construyen a mano (misma forma que produce el
agente) para controlar cada caso; cada prueba usa un DWH ficticio propio
(host *.invalid distinto → identidad distinta), así que no comparten estado.
Las pruebas con bases PostgreSQL REALES (agente + contenedor DWH) están en
dwh_client/tests/test_inventory_integration.py.
"""

import hashlib
import itertools
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

import support
from inventory_postgres import connection_identity, structure_fingerprint

DBNAME = "nexus_test_cfg_inventory"
SEQ = itertools.count(1)
N = itertools.count(1)


@pytest.fixture(scope="module")
def ienv():
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.recreate_config_db(DBNAME)
    b = support.Backend(DBNAME, extra_ini={
        "health": {"evaluator_interval_seconds": "0", "startup_grace_seconds": "0"},
        "notifications": {"worker_interval_seconds": "0"},
        "inventory": {"lease_ttl_seconds": "120", "default_interval_seconds": "3600"},
        "server": {"inventory_max_decompressed_bytes": "2000000"},
    })
    b.start()
    try:
        ids = support.seed_config(b)
        yield {"backend": b, "ids": ids}
    finally:
        b.stop()
        support.drop_config_db(DBNAME)


@pytest.fixture()
def db(ienv):
    conn = support.cfg_conn(DBNAME)
    yield conn
    conn.close()


def q(db, sql, params=()):
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def enroll(b, header, token, name):
    r = requests.post(b.url + "/agent/enroll", headers={header: token},
                      json={"name": name, "hostname": "h", "client_version": "test"}, timeout=10)
    assert r.status_code == 201, r.text
    d = r.json()
    return {"x-installation-id": d["installation_id"], "x-installation-secret": d["secret"]}


def new_dwh(ienv, host=None, db_name="dwh_inv", agencies=1):
    """Grupo con un DWH ficticio propio + empresa + agencias + instalaciones enroladas por agencia."""
    b = ienv["backend"]
    n = next(N)
    host = host or f"dwh-{n}-{uuid.uuid4().hex[:6]}.invalid"
    g = b.admin("POST", "/admin/groups", {"name": f"Grupo Inv {n} {uuid.uuid4().hex[:4]}", "warehouse_host": host,
                                          "warehouse_port": 5432, "warehouse_database": db_name,
                                          "warehouse_username": "inv_ro", "warehouse_password": "pw-inv-secreta"},
                expect=201)
    c = b.admin("POST", "/admin/companies", {"group_id": g["id"], "name": f"Empresa Inv {n}",
                                             "source_type": "postgresql", "source_host": f"src-{n}.invalid",
                                             "source_port": 5432, "source_database": "dms", "source_username": "u",
                                             "source_password": "p-secreta"}, expect=201)
    ags, hs = [], []
    for i in range(agencies):
        a = b.admin("POST", "/admin/agencies", {"company_id": c["id"], "name": f"Agencia Inv {n}-{i}"}, expect=201)
        ags.append(a)
        hs.append(enroll(b, "x-agency-token", a["agency_token"], f"inst-{n}-{i}"))
    key = connection_identity("dwh", "postgresql", host, 5432, db_name)
    return {"group": g, "company": c, "agencies": ags, "headers": hs, "h": hs[0], "key": key,
            "engine": hashlib.sha256(f"engine-{host}".encode()).hexdigest()}


def lease(b, h, dwh=True, companies=(), release=()):
    r = requests.post(b.url + "/agent/inventory/lease", headers=h, timeout=10,
                      json={"capabilities": {"dwh": dwh, "source_company_ids": list(companies)},
                            "client_version": "test", "release_ids": list(release)})
    assert r.status_code == 200, r.text
    return r.json()


def dwh_target(b, d, h=None):
    targets = [t for t in lease(b, h or d["h"])["targets"] if t["kind"] == "dwh"]
    assert len(targets) == 1, targets
    d["mdb"] = targets[0]["monitored_database_id"]
    return targets[0]


def col(t, not_null=False, default=None):
    return {"type": t, "not_null": not_null, "default": default}


def table(cols, constraints=None, indexes=None):
    return {"columns": cols, "constraints": constraints or {}, "indexes": indexes or {}}


def view(cols, definition):
    return {"columns": cols, "definition_hash": hashlib.sha256(definition.encode()).hexdigest()}


def obj(schema, name, typ, structure, definition=None):
    o = {"schema_name": schema, "name": name, "type": typ, "structure": structure}
    if definition is not None:
        o["definition"] = definition
    return o


def base_objects():
    return [
        obj("public", "clientes", "table", table(
            {"id": col("integer", True), "nombre": col("text"), "saldo": col("numeric(10,2)", default="0")},
            {"clientes_pkey": {"type": "primary_key", "definition": "PRIMARY KEY (id)"}},
            {"clientes_nombre_idx": {"unique": False, "definition": "USING btree (nombre)"}})),
        obj("public", "ventas", "table", table({"id": col("integer", True), "total": col("numeric")})),
        obj("public", "v_clientes", "view", view({"id": col("integer"), "nombre": col("text")},
                                                  "SELECT id, nombre FROM clientes")),
        obj("public", "v_vieja", "view", view({"id": col("integer")}, "SELECT id FROM ventas")),
        obj("secreto", "nomina", "table", table({"id": col("integer", True)})),
    ]


def snapshot(b, d, objects, status="complete", verified=("public", "secreto"), unverifiable=(), reason=None,
             h=None, engine=None, key=None, expect=200, captured=None, strength="strong", weak=None, raw=False):
    body = {"snapshot_id": str(uuid.uuid4()), "monitored_database_id": d["mdb"],
            "config_fingerprint": key or d["key"], "engine_identity": engine or d["engine"],
            "engine_identity_strength": strength, "engine_identity_weak": weak,
            "captured_at": (captured or datetime.now(timezone.utc)).isoformat(),
            "status": status, "reason_code": reason, "schemas_verified": list(verified),
            "schemas_unverifiable": [{"schema_name": s, "reason": "no_usage"} for s in unverifiable],
            "server_version": "16", "agent_version": "test", "objects": objects}
    if raw:
        return body
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=h or d["h"], json=body, timeout=15)
    assert r.status_code == expect, r.text
    return r.json()


def approve(b, d, keys=None, expect=200):
    m = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}", expect=200)
    body = {"expected_snapshot_id": m["last_verified_snapshot_id"], "comment": "aprobada en prueba"}
    if keys is not None:
        body["object_keys"] = keys
    return b.admin("POST", f"/admin/monitored-databases/{d['mdb']}/baseline/approve", body, expect=expect)


def monitored(b, d, **base):
    """DWH con línea base aprobada a partir de base_objects()."""
    dwh_target(b, d)
    snapshot(b, d, base_objects())
    approve(b, d)
    return d


def changes(b, d, view="pending", **params):
    qs = "&".join([f"monitored_database_id={d['mdb']}", f"view={view}"] + [f"{k}={v}" for k, v in params.items()])
    return b.admin("GET", "/admin/structural-changes?" + qs, expect=200)["items"]


def change_for(b, d, name, view="pending"):
    items = [c for c in changes(b, d, view) if c["object_name"] == name]
    assert len(items) == 1, items
    return b.admin("GET", f"/admin/structural-changes/{items[0]['id']}", expect=200)


def ack(b, c, attribution="client", expect=200, **kw):
    body = {"attribution": attribution, "expected_version": c["row_version"],
            "expected_observed_fingerprint": c["observed_fingerprint"], **kw}
    r = requests.post(b.url + f"/admin/structural-changes/{c['id']}/acknowledge", json=body,
                      headers={"x-admin-token": b.admin_token}, timeout=10)
    assert r.status_code == expect, r.text
    return r.json()


def baseline_items(b, d, view="approved"):
    r = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}/baseline?view={view}", expect=200)
    return {(i["schema_name"], i["name"], i["type"]): i for i in r["items"]}


def with_objects(**changes_):
    """base_objects() con reemplazos: nombre → objeto (None = eliminarlo)."""
    out = {o["name"]: o for o in base_objects()}
    for k, v in changes_.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return list(out.values())


# ─────────────────────────────────────────────────────────────────────────────
# Pruebas
# ─────────────────────────────────────────────────────────────────────────────
def test_primer_inventario_es_propuesta_y_requiere_aprobacion(ienv, db):
    b = ienv["backend"]
    d = new_dwh(ienv)
    t = dwh_target(b, d)
    assert t["granted"] and t["due"]
    r = snapshot(b, d, base_objects())
    assert r["state"] == "baseline_pending"
    # Un segundo inventario con un objeto extra tampoco genera alertas (aún no hay referencia aprobada).
    snapshot(b, d, base_objects() + [obj("public", "extra", "table", table({"id": col("integer")}))])
    assert changes(b, d) == []
    m = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}", expect=200)
    assert m["state"] == "baseline_pending" and m["effective_status"] == "verified"
    prop = baseline_items(b, d, view="proposal")
    assert len(prop) == 6 and baseline_items(b, d) == {}
    # Aprobar con un snapshot viejo → 409 (llegó otro inventario desde que se revisó).
    first = q(db, "SELECT MIN(id) FROM inventory_snapshot WHERE monitored_database_id = %s", (d["mdb"],))[0][0]
    r = requests.post(b.url + f"/admin/monitored-databases/{d['mdb']}/baseline/approve",
                      headers={"x-admin-token": b.admin_token}, json={"expected_snapshot_id": first}, timeout=10)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "snapshot_changed"
    # Aprobación parcial: lo NO aprobado ("extra") queda como alerta de objeto nuevo.
    keys = [{"schema_name": s, "name": n, "type": t} for (s, n, t) in prop if n != "extra"]
    res = approve(b, d, keys=keys)
    assert res["approved_objects"] == 5 and res["pending_changes"] == 1
    pend = changes(b, d)
    assert [(c["object_name"], c["change_kind"]) for c in pend] == [("extra", "object_added")]
    assert pend[0]["attribution"] is None
    # No se puede volver a "aprobar" en bloque en modo monitoreo (aceptaría todo lo pendiente).
    approve(b, d, expect=409)


def test_crear_modificar_y_eliminar_tablas_y_vistas_con_detalle_granular(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    cli = table({"id": col("bigint", True), "nombre": col("text", True), "saldo": col("numeric(10,2)", default="1"),
                 "rfc": col("text")},
                {"clientes_pkey": {"type": "primary_key", "definition": "PRIMARY KEY (id)"},
                 "clientes_rfc_key": {"type": "unique", "definition": "UNIQUE (rfc)"}},
                {"clientes_nombre_idx": {"unique": False, "definition": "USING btree (lower(nombre))"}})
    r = snapshot(b, d, with_objects(
        clientes=obj("public", "clientes", "table", cli),
        v_clientes=obj("public", "v_clientes", "view", view({"id": col("integer"), "nombre": col("text")},
                                                             "SELECT id, nombre FROM clientes WHERE id > 0")),
        v_vieja=None, ventas=None,
        nueva=obj("public", "nueva", "table", table({"id": col("integer")})),
        mv_resumen=obj("public", "mv_resumen", "matview", {**view({"n": col("bigint")}, "SELECT count(*)"),
                                                           "constraints": {}, "indexes": {}}),
    ))
    assert r["detected"] == 6, r
    by = {c["object_name"]: c for c in changes(b, d)}
    assert {k: v["change_kind"] for k, v in by.items()} == {
        "clientes": "object_modified", "v_clientes": "object_modified", "v_vieja": "object_removed",
        "ventas": "object_removed", "nueva": "object_added", "mv_resumen": "object_added"}
    assert set(by["clientes"]["change_types"]) >= {
        "column_added", "column_type_changed", "column_nullability_changed", "column_default_changed",
        "constraint_added", "index_changed"}
    assert "view_definition_changed" in by["v_clientes"]["change_types"]
    det = b.admin("GET", f"/admin/structural-changes/{by['clientes']['id']}", expect=200)
    kinds = {(x["kind"], x["item"]) for x in det["diffs"]}
    assert ("column_type_changed", "id") in kinds and ("column_added", "rfc") in kinds
    assert det["previous_structure"]["columns"]["id"]["type"] == "integer"
    assert det["current_structure"]["columns"]["id"]["type"] == "bigint"
    assert det["group_name"] == d["group"]["name"] and det["database_kind"] == "dwh"
    assert det["first_detected_at"] and det["last_observed_at"]
    # Filtros del listado.
    assert [c["object_name"] for c in changes(b, d, change_type="column_type_changed")] == ["clientes"]
    assert {c["object_name"] for c in changes(b, d, change_type="object_removed")} == {"v_vieja", "ventas"}
    items = b.admin("GET", f"/admin/structural-changes?view=pending&group_id={d['group']['id']}", expect=200)["items"]
    assert len(items) == 6
    items = b.admin("GET", f"/admin/structural-changes?view=pending&agency_id={d['agencies'][0]['id']}",
                    expect=200)["items"]
    assert len(items) == 6
    # Mismo estado observado de nuevo: no se duplican alertas (solo "última observación").
    before = {c["id"]: c["observation_count"] for c in changes(b, d)}
    r2 = snapshot(b, d, with_objects(
        clientes=obj("public", "clientes", "table", cli),
        v_clientes=obj("public", "v_clientes", "view", view({"id": col("integer"), "nombre": col("text")},
                                                             "SELECT id, nombre FROM clientes WHERE id > 0")),
        v_vieja=None, ventas=None,
        nueva=obj("public", "nueva", "table", table({"id": col("integer")})),
        mv_resumen=obj("public", "mv_resumen", "matview", {**view({"n": col("bigint")}, "SELECT count(*)"),
                                                           "constraints": {}, "indexes": {}}),
    ))
    assert r2["detected"] == 0 and r2["observed_again"] == 6
    after = {c["id"]: c["observation_count"] for c in changes(b, d)}
    assert set(after) == set(before) and all(after[k] == before[k] + 1 for k in after)


def test_dar_por_entendido_cliente_y_nexus_solo_esa_diferencia_e_historial(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    cli2 = table({"id": col("integer", True), "nombre": col("text"), "saldo": col("numeric(10,2)", default="0"),
                  "email": col("text")},
                 {"clientes_pkey": {"type": "primary_key", "definition": "PRIMARY KEY (id)"}},
                 {"clientes_nombre_idx": {"unique": False, "definition": "USING btree (nombre)"}})
    ventas2 = table({"id": col("integer", True), "total": col("numeric(12,2)")})
    objs = with_objects(clientes=obj("public", "clientes", "table", cli2),
                        ventas=obj("public", "ventas", "table", ventas2), v_vieja=None)
    snapshot(b, d, objs)
    c_cli, c_ven, c_vv = change_for(b, d, "clientes"), change_for(b, d, "ventas"), change_for(b, d, "v_vieja")
    base0 = baseline_items(b, d)

    # Atribución obligatoria.
    r = requests.post(b.url + f"/admin/structural-changes/{c_cli['id']}/acknowledge",
                      headers={"x-admin-token": b.admin_token}, timeout=10,
                      json={"expected_version": c_cli["row_version"],
                            "expected_observed_fingerprint": c_cli["observed_fingerprint"]})
    assert r.status_code == 422
    r = requests.post(b.url + f"/admin/structural-changes/{c_cli['id']}/acknowledge",
                      headers={"x-admin-token": b.admin_token}, timeout=10,
                      json={"attribution": "otro", "expected_version": c_cli["row_version"],
                            "expected_observed_fingerprint": c_cli["observed_fingerprint"]})
    assert r.status_code == 422

    ack(b, c_cli, "client", comment="El cliente agregó email para CRM", ticket_ref="TCK-101")
    ack(b, c_vv, "nexus", comment="Vista retirada por el equipo Nexus")
    # 1) sale de pendientes; 4) NO acepta otros pendientes (ventas sigue pendiente y su referencia intacta)
    assert {c["object_name"] for c in changes(b, d)} == {"ventas"}
    base1 = baseline_items(b, d)
    assert base1[("public", "clientes", "table")]["fingerprint"] == structure_fingerprint(cli2)
    assert ("public", "v_vieja", "view") not in base1
    assert base1[("public", "ventas", "table")]["fingerprint"] == base0[("public", "ventas", "table")]["fingerprint"]
    # 2) historial y detalle se conservan
    det = b.admin("GET", f"/admin/structural-changes/{c_cli['id']}", expect=200)
    assert det["status"] == "acknowledged" and det["attribution"] == "client" and det["ack_by"] == "admin"
    assert det["ack_at"] and det["ticket_ref"] == "TCK-101" and det["ack_comment"].startswith("El cliente")
    assert [e["event_type"] for e in det["events"]] == ["detected", "acknowledged"]
    assert det["previous_structure"] and det["current_structure"] and det["diffs"]
    hist = changes(b, d, view="history")
    assert {(c["object_name"], c["attribution"]) for c in hist} == {("clientes", "client"), ("v_vieja", "nexus")}
    assert [c["object_name"] for c in changes(b, d, view="history", attribution="nexus")] == ["v_vieja"]
    hv = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}/baseline/history", expect=200)["items"]
    assert {(h["object_name"], h["action"]) for h in hv[:2]} == {("clientes", "acknowledged"), ("v_vieja", "removed")}

    # Mismo estado otra vez: sin alertas nuevas para lo entendido.
    r = snapshot(b, d, objs)
    assert r["detected"] == 0
    # 5) si el objeto vuelve a cambiar → alerta NUEVA (la entendida queda en el historial).
    cli3 = dict(cli2, columns=dict(cli2["columns"], telefono=col("text")))
    snapshot(b, d, with_objects(clientes=obj("public", "clientes", "table", cli3),
                                ventas=obj("public", "ventas", "table", ventas2), v_vieja=None))
    nuevo = change_for(b, d, "clientes")
    assert nuevo["id"] != c_cli["id"] and nuevo["status"] == "pending"
    assert {(x["kind"], x["item"]) for x in nuevo["diffs"]} == {("column_added", "telefono")}
    assert nuevo["baseline_fingerprint"] == structure_fingerprint(cli2)
    assert b.admin("GET", f"/admin/structural-changes/{c_cli['id']}", expect=200)["status"] == "acknowledged"


def test_cambio_concurrente_durante_el_ack_no_se_acepta_en_silencio(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    v2 = table({"id": col("integer", True), "total": col("numeric"), "iva": col("numeric")})
    snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", v2)))
    shown = change_for(b, d, "ventas")          # lo que el usuario ve en pantalla
    # Mientras decide, el objeto vuelve a cambiar (llega otro inventario).
    v3 = table({"id": col("integer", True), "total": col("numeric"), "iva": col("numeric"), "ieps": col("numeric")})
    snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", v3)))
    r = ack(b, shown, "client", expect=409)
    assert r["detail"]["code"] == "not_pending" and r["detail"]["superseded_by_id"]
    old = b.admin("GET", f"/admin/structural-changes/{shown['id']}", expect=200)
    assert old["status"] == "superseded" and old["superseded_by_id"] == r["detail"]["superseded_by_id"]
    new = change_for(b, d, "ventas")
    assert new["id"] == old["superseded_by_id"] and new["supersedes_id"] == shown["id"]
    assert new["observed_fingerprint"] == structure_fingerprint(v3)
    # La referencia NO absorbió ninguna de las dos versiones.
    base = baseline_items(b, d)[("public", "ventas", "table")]
    assert base["fingerprint"] == structure_fingerprint(base_objects()[1]["structure"])
    # Versión/huella esperada incorrecta → 409 sin cambios.
    bad = dict(new, row_version=new["row_version"] + 5)
    assert ack(b, bad, "nexus", expect=409)["detail"]["code"] == "stale_version"
    bad = dict(new, observed_fingerprint=structure_fingerprint(v2))
    assert ack(b, bad, "nexus", expect=409)["detail"]["code"] == "stale_version"
    assert change_for(b, d, "ventas")["status"] == "pending"
    # Con la versión vigente sí se acepta.
    ack(b, new, "nexus")
    assert baseline_items(b, d)[("public", "ventas", "table")]["fingerprint"] == structure_fingerprint(v3)


def test_ack_y_snapshot_simultaneos_mantienen_invariantes(ienv, db):
    """Carrera real: ack e inventario a la vez. Nunca queda aceptada una versión que nadie vio."""
    b = ienv["backend"]
    for i in range(6):
        d = monitored(b, new_dwh(ienv))
        v2 = table({"id": col("integer", True), "total": col("numeric"), "c2": col("text")})
        v3 = table({"id": col("integer", True), "total": col("numeric"), "c2": col("text"), "c3": col("text")})
        snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", v2)))
        shown = change_for(b, d, "ventas")
        out = {}

        def do_ack():
            out["ack"] = requests.post(
                b.url + f"/admin/structural-changes/{shown['id']}/acknowledge",
                headers={"x-admin-token": b.admin_token}, timeout=15,
                json={"attribution": "client", "expected_version": shown["row_version"],
                      "expected_observed_fingerprint": shown["observed_fingerprint"]}).status_code

        def do_snap():
            out["snap"] = snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", v3)))

        ts = [threading.Thread(target=do_ack), threading.Thread(target=do_snap)]
        if i % 2:
            ts.reverse()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        base = baseline_items(b, d)[("public", "ventas", "table")]["fingerprint"]
        pend = changes(b, d)
        assert base != structure_fingerprint(v3), "v3 se aceptó sin que nadie la viera"
        assert len(pend) == 1 and pend[0]["observed_fingerprint"] == structure_fingerprint(v3)
        if out["ack"] == 200:
            assert base == structure_fingerprint(v2) and pend[0]["baseline_fingerprint"] == structure_fingerprint(v2)
        else:
            assert out["ack"] == 409 and base == structure_fingerprint(base_objects()[1]["structure"])


def test_perdida_de_permisos_o_conexion_no_genera_eliminaciones(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    # Se revoca USAGE sobre "secreto": el agente lo reporta como no verificable (sin sus objetos).
    objs = [o for o in base_objects() if o["schema_name"] != "secreto"]
    r = snapshot(b, d, objs, status="partial", verified=("public",), unverifiable=("secreto",),
                 reason="SCHEMAS_UNVERIFIABLE")
    assert r["verification"] == "partial" and r["detected"] == 0, r
    assert changes(b, d) == []
    m = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}", expect=200)
    assert m["effective_status"] == "partial"
    assert ("secreto", "nomina", "table") in baseline_items(b, d)
    # Conexión perdida: snapshot no confiable → sin comparación, referencia intacta.
    last_ok = m["last_verified_at"]
    r = snapshot(b, d, [], status="unreliable", verified=(), reason="DWH_CONNECTION_FAILED")
    assert r["verification"] == "unverifiable"
    m = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}", expect=200)
    assert m["effective_status"] == "unverifiable" and m["last_reason_code"] == "DWH_CONNECTION_FAILED"
    assert m["last_verified_at"] == last_ok
    assert changes(b, d) == [] and len(baseline_items(b, d)) == 5
    summary = b.admin("GET", f"/admin/inventory/summary?group_id={d['group']['id']}", expect=200)
    assert summary["unverifiable"] == 1
    # Servidor distinto detrás de la misma dirección: tampoco se compara.
    r = snapshot(b, d, [], engine="f" * 64)
    assert r["verification"] == "unverifiable" and r["reason_code"] == "ENGINE_IDENTITY_CHANGED"
    assert changes(b, d) == []
    # Se recupera: inventario completo igual a la referencia → sin alertas.
    r = snapshot(b, d, base_objects())
    assert r["verification"] == "verified" and r["detected"] == 0
    # Un esquema omitido en schemas_verified NO produce eliminaciones…
    r = snapshot(b, d, objs, verified=("public",))
    assert r["detected"] == 0 and changes(b, d) == []
    # …pero uno que el agente verificó explícitamente vacío/inexistente sí.
    r = snapshot(b, d, objs, verified=("public", "secreto"))
    assert [c["object_name"] for c in changes(b, d)] == ["nomina"]
    # Si vuelve a aparecer igual que la referencia, la alerta se marca como revertida.
    snapshot(b, d, base_objects())
    assert changes(b, d) == []
    rev = changes(b, d, view="history")
    assert rev[0]["status"] == "reverted"


def test_reclasificar_requiere_motivo_y_conserva_valores_previos(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    snapshot(b, d, base_objects() + [obj("public", "extra", "table", table({"id": col("integer")}))])
    c = change_for(b, d, "extra")
    r = requests.post(b.url + f"/admin/structural-changes/{c['id']}/reclassify",
                      headers={"x-admin-token": b.admin_token}, timeout=10,
                      json={"attribution": "nexus", "reason": "motivo largo", "expected_version": c["row_version"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_acknowledged"
    ack(b, c, "client", comment="lo pidió el cliente", ticket_ref="T-1")
    c = b.admin("GET", f"/admin/structural-changes/{c['id']}", expect=200)
    for body in ({"attribution": "nexus", "expected_version": c["row_version"]},
                 {"attribution": "nexus", "reason": "no", "expected_version": c["row_version"]}):
        r = requests.post(b.url + f"/admin/structural-changes/{c['id']}/reclassify",
                          headers={"x-admin-token": b.admin_token}, json=body, timeout=10)
        assert r.status_code == 422
    b.admin("POST", f"/admin/structural-changes/{c['id']}/reclassify",
            {"attribution": "nexus", "reason": "Fue una migración del equipo Nexus (ticket interno)",
             "ticket_ref": "NX-77", "expected_version": c["row_version"]}, expect=200)
    det = b.admin("GET", f"/admin/structural-changes/{c['id']}", expect=200)
    assert det["attribution"] == "nexus" and det["ticket_ref"] == "NX-77"
    assert det["reclassified_by"] == "admin" and det["reclassified_at"]
    assert det["ack_by"] == "admin" and det["ack_comment"] == "lo pidió el cliente"   # el ack original no se pisa
    ev = [e for e in det["events"] if e["event_type"] == "reclassified"][0]
    assert ev["data"]["previous"]["attribution"] == "client" and ev["data"]["previous"]["ticket_ref"] == "T-1"
    assert ev["data"]["new"]["attribution"] == "nexus" and ev["actor"] == "admin"
    assert ev["data"]["reason"].startswith("Fue una migración")
    # Versión vieja → 409.
    r = requests.post(b.url + f"/admin/structural-changes/{c['id']}/reclassify",
                      headers={"x-admin-token": b.admin_token}, timeout=10,
                      json={"attribution": "client", "reason": "otra vez al cliente", "expected_version": c["row_version"]})
    assert r.status_code == 409


def test_lease_una_instalacion_por_base_y_sin_inventarios_duplicados(ienv, db):
    b = ienv["backend"]
    d = new_dwh(ienv, agencies=2)   # dos agencias (dos instalaciones) comparten el DWH del grupo
    t1 = dwh_target(b, d, d["headers"][0])
    t2 = [t for t in lease(b, d["headers"][1])["targets"] if t["kind"] == "dwh"][0]
    assert t1["granted"] and not t2["granted"]
    assert t1["monitored_database_id"] == t2["monitored_database_id"]
    assert q(db, "SELECT COUNT(*) FROM monitored_database WHERE identity_key = %s", (d["key"],))[0][0] == 1
    # La instalación sin lease no puede reportar.
    r = snapshot(b, d, base_objects(), h=d["headers"][1], expect=409)
    assert r["detail"]["code"] == "lease_not_held"
    snapshot(b, d, base_objects())
    assert q(db, "SELECT COUNT(*) FROM inventory_snapshot WHERE monitored_database_id = %s", (d["mdb"],))[0][0] == 1
    # Vence el lease (agente caído) → la otra instalación lo toma; la primera ya no puede reportar.
    q(db, "UPDATE monitored_database SET lease_until = NOW() - INTERVAL '1 second' WHERE id = %s RETURNING id",
      (d["mdb"],))
    t2 = [t for t in lease(b, d["headers"][1])["targets"] if t["kind"] == "dwh"][0]
    assert t2["granted"]
    assert [t for t in lease(b, d["headers"][0])["targets"] if t["kind"] == "dwh"][0]["granted"] is False
    snapshot(b, d, base_objects(), h=d["headers"][0], expect=409)
    snapshot(b, d, base_objects(), h=d["headers"][1])
    ev = [e["event_type"] for e in b.admin("GET", f"/admin/monitored-databases/{d['mdb']}", expect=200)["events"]]
    assert ev.count("lease_acquired") == 2
    # Liberación voluntaria (release_ids) y por el administrador.
    lease(b, d["headers"][1], release=[d["mdb"]])
    assert q(db, "SELECT lease_installation_id FROM monitored_database WHERE id = %s", (d["mdb"],))[0][0] is None
    # Una instalación de OTRO grupo no alcanza esta base.
    other = new_dwh(ienv)
    assert d["mdb"] not in [t["monitored_database_id"] for t in lease(b, other["h"])["targets"]]
    r = snapshot(b, dict(d), base_objects(), h=other["h"], expect=404)
    assert r["detail"]["code"] == "unknown_database"          # igual que una base inexistente
    r = snapshot(b, dict(d, mdb=999999), base_objects(), h=other["h"], expect=404)
    assert r["detail"]["code"] == "unknown_database"
    # Huella de conexión distinta a la identidad → 409.
    dwh_target(b, d, d["headers"][1])
    r = snapshot(b, d, base_objects(), h=d["headers"][1], key="0" * 64, expect=409)
    assert r["detail"]["code"] == "identity_mismatch"


def test_misma_base_fisica_con_otro_nombre_de_host_no_se_duplica(ienv, db):
    b = ienv["backend"]
    a = new_dwh(ienv, host="db-compartida.invalid", db_name="dwh_comp")
    dwh_target(b, a)
    snapshot(b, a, base_objects())
    # Otro grupo apunta a la MISMA base física con otro nombre de host (misma identidad del motor).
    c = new_dwh(ienv, host="DB-COMPARTIDA.invalid", db_name="dwh_comp")
    assert c["key"] == a["key"]            # el host se normaliza (mayúsculas/espacios)
    dwh_target(b, c)
    assert c["mdb"] == a["mdb"] and q(db, "SELECT COUNT(*) FROM monitored_database WHERE identity_key = %s",
                                      (a["key"],))[0][0] == 1
    e = new_dwh(ienv, host="10.9.9.9.invalid", db_name="dwh_comp")
    dwh_target(b, e)
    assert e["mdb"] != a["mdb"]
    r = snapshot(b, e, base_objects(), engine=a["engine"])
    assert r["status"] == "rejected" and r["code"] == "duplicate_database" and r["duplicate_of_id"] == a["mdb"]
    assert q(db, "SELECT COUNT(*) FROM inventory_object_state WHERE monitored_database_id = %s", (e["mdb"],))[0][0] == 0
    m = b.admin("GET", f"/admin/monitored-databases/{e['mdb']}", expect=200)
    assert m["effective_status"] == "duplicate" and m["duplicate_of_id"] == a["mdb"]
    assert [t for t in lease(b, e["h"])["targets"] if t["kind"] == "dwh"] == []
    # La base original sigue con su único inventario; la vinculación registra ambos grupos.
    links = b.admin("GET", f"/admin/monitored-databases/{a['mdb']}", expect=200)["links"]
    assert {l["group_id"] for l in links} == {a["group"]["id"], c["group"]["id"]}


def test_ack_estructural_no_resuelve_incidencias_de_carga(ienv, db):
    b, ids = ienv["backend"], ienv["ids"]
    h = enroll(b, "x-group-token", ids["group_a"]["group_token"], "inst-carga")
    tid = ids["t_cli"]["id"]
    ex = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    requests.post(b.url + "/agent/executions", headers=h, timeout=10, json={
        "execution_id": ex, "task_id": tid, "started_at": now.isoformat(), "agent_seq": next(SEQ)}).raise_for_status()
    requests.put(b.url + f"/agent/executions/{ex}", headers=h, timeout=10, json={
        "task_id": tid, "status": "failed", "failure_stage": "load", "error_code": "DWH_SQL_ERROR",
        "error_message": "columna inexistente", "finished_at": (now + timedelta(seconds=1)).isoformat(),
        "agent_seq": next(SEQ)}).raise_for_status()
    inc = [i for i in b.admin("GET", "/admin/incidents?view=open", expect=200)["items"]
           if i["task_id"] == tid and i["category"] == "task_failed"]
    assert len(inc) == 1
    d = monitored(b, new_dwh(ienv))
    snapshot(b, d, base_objects() + [obj("public", "extra", "table", table({"id": col("integer")}))])
    ack(b, change_for(b, d, "extra"), "nexus")
    still = b.admin("GET", f"/admin/incidents/{inc[0]['id']}", expect=200)
    assert still["status"] == "open"


def test_definiciones_de_vistas_protegidas_y_cifradas(ienv, db):
    b = ienv["backend"]
    d = new_dwh(ienv)
    dwh_target(b, d)
    marker = "nx_view_sql_marker_" + uuid.uuid4().hex[:6]
    sql1, sql2 = f"SELECT id FROM ventas /* {marker} */", f"SELECT id, total FROM ventas /* {marker} */"
    v1 = obj("public", "v_sens", "view", view({"id": col("integer")}, sql1), definition=sql1)
    # Sin view_definitions_enabled el texto no se guarda (solo el hash).
    snapshot(b, d, [v1])
    assert q(db, "SELECT definition_enc FROM inventory_object_state WHERE monitored_database_id = %s",
             (d["mdb"],))[0][0] is None
    b.admin("PUT", f"/admin/monitored-databases/{d['mdb']}", {"view_definitions_enabled": True}, expect=200)
    snapshot(b, d, [v1])
    approve(b, d)
    v2 = obj("public", "v_sens", "view", view({"id": col("integer"), "total": col("numeric")}, sql2), definition=sql2)
    snapshot(b, d, [v2])
    c = change_for(b, d, "v_sens")
    stored = q(db, "SELECT previous_definition_enc, current_definition_enc FROM structural_change WHERE id = %s",
               (c["id"],))[0]
    assert all(s and s.startswith("ENC:") and marker not in s for s in stored)
    assert marker not in str(c) and c["definitions_stored"] is True and c["definitions_viewable"] is False
    r = requests.get(b.url + f"/admin/structural-changes/{c['id']}/definitions",
                     headers={"x-admin-token": b.admin_token}, timeout=10)
    assert r.status_code == 403 and r.json()["detail"]["permission"] == "inventory.view_definitions"
    assert marker not in open(b.log_path).read()
    assert not q(db, "SELECT 1 FROM activity_log WHERE error_detail LIKE %s", (f"%{marker}%",))


def test_monitoreo_del_origen_es_opcional_y_explicito(ienv, db):
    b = ienv["backend"]
    d = new_dwh(ienv)
    comp = d["company"]["id"]
    # Sin alta explícita no hay objetivo de origen aunque el agente tenga credenciales.
    assert [t for t in lease(b, d["h"], companies=[comp])["targets"] if t["kind"] == "source"] == []
    m = b.admin("POST", "/admin/monitored-databases", {"kind": "source", "company_id": comp}, expect=201)
    assert m["enabled"] is False and m["kind"] == "source"
    r = requests.post(b.url + "/admin/monitored-databases", headers={"x-admin-token": b.admin_token}, timeout=10,
                      json={"kind": "source", "company_id": comp})
    assert r.status_code == 409
    assert [t for t in lease(b, d["h"], companies=[comp])["targets"] if t["kind"] == "source"] == []
    b.admin("PUT", f"/admin/monitored-databases/{m['id']}",
            {"enabled": True, "schema_include": ["ventas_*"], "schema_exclude": ["ventas_tmp"]}, expect=200)
    src = [t for t in lease(b, d["h"], companies=[comp])["targets"] if t["kind"] == "source"]
    assert len(src) == 1 and src[0]["granted"] and src[0]["schema_include"] == ["ventas_*"]
    assert "ventas_tmp" in src[0]["schema_exclude"]
    # Sin capacidad declarada (el agente no tiene credenciales de esa empresa) no se asigna.
    assert [t for t in lease(b, d["h"], companies=[])["targets"] if t["kind"] == "source"] == []
    ev = b.admin("GET", f"/admin/monitored-databases/{m['id']}", expect=200)["events"]
    assert any(e["event_type"] == "config_changed" and "enabled" in e["data"] for e in ev)
    # Origen SQL Server: se puede registrar, pero se reporta "no se pudo verificar" (motor no soportado).
    sq = b.admin("POST", "/admin/companies", {"group_id": d["group"]["id"], "name": "Empresa SQLServer",
                                              "source_type": "sqlserver", "source_host": "mssql.invalid",
                                              "source_port": 1433, "source_database": "dms", "source_username": "u",
                                              "source_password": "p"}, expect=201)
    ms = b.admin("POST", "/admin/monitored-databases", {"kind": "source", "company_id": sq["id"], "enabled": True},
                 expect=201)
    assert "ENGINE_UNSUPPORTED" in ms["warning"]
    # La instalación por agencia de otra empresa no alcanza ese origen; una de grupo sí.
    assert [t for t in lease(b, d["h"], companies=[sq["id"]])["targets"] if t["kind"] == "source"] == []
    hg = enroll(b, "x-group-token", d["group"]["group_token"], "inst-grupo")
    t = [t for t in lease(b, hg, companies=[sq["id"]])["targets"] if t["kind"] == "source"][0]
    assert t["granted"]
    key = connection_identity("source", "sqlserver", "mssql.invalid", 1433, "dms")
    r = snapshot(b, dict(d, mdb=t["monitored_database_id"]), base_objects(), key=key, h=hg)
    assert r["verification"] == "unverifiable" and r["reason_code"] == "ENGINE_UNSUPPORTED"


def test_evidencia_nexus_no_atribuye_autoria(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    o = b.admin("POST", "/admin/objects", {"company_id": d["company"]["id"], "name": "Ventas catálogo",
                                           "destination_table": "ventas", "upsert_keys": "id"}, expect=201)
    t = b.admin("POST", "/admin/tasks", {"agency_id": d["agencies"][0]["id"], "object_catalog_id": o["id"],
                                         "extract_sql": "SELECT 1 AS id", "schedule_seconds": 3600}, expect=201)

    def execution(ddl):
        ex = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        requests.post(b.url + "/agent/executions", headers=d["h"], timeout=10, json={
            "execution_id": ex, "task_id": t["id"], "started_at": now.isoformat(),
            "agent_seq": next(SEQ)}).raise_for_status()
        r = requests.put(b.url + f"/agent/executions/{ex}", headers=d["h"], timeout=10, json={
            "task_id": t["id"], "status": "success", "finished_at": (now + timedelta(seconds=1)).isoformat(),
            "rows_read": 1, "rows_loaded": 1, "agent_seq": next(SEQ), "ddl_applied": ddl})
        assert r.status_code == 200, r.text
        return ex

    # 1) El agente Nexus agregó la columna en una carga y luego el inventario la detecta.
    ex1 = execution([{"object": "public.ventas", "action": "add_column", "columns": ["iva"]}])
    assert q(db, "SELECT ddl_applied FROM task_execution WHERE execution_id = %s", (ex1,))[0][0][0]["action"] == \
        "add_column"
    v2 = table({"id": col("integer", True), "total": col("numeric"), "iva": col("text")})
    snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", v2)))
    c = change_for(b, d, "ventas")
    evs = [e for e in c["evidence"] if e["type"] == "nexus_execution"]
    assert [e["execution_id"] for e in evs] == [ex1] and evs[0]["columns"] == ["iva"]
    assert c["attribution"] is None and c["status"] == "pending"      # evidencia ≠ atribución
    # 2) El reporte llega DESPUÉS de la detección: se adjunta a la alerta pendiente.
    v3 = dict(v2, columns=dict(v2["columns"], ieps=col("text")))
    snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", v3)))
    c2 = change_for(b, d, "ventas")
    ex2 = execution([{"object": "public.ventas", "action": "add_column", "columns": ["ieps"]},
                     {"object": "public.otra", "action": "add_column", "columns": ["x"]}])
    c2 = b.admin("GET", f"/admin/structural-changes/{c2['id']}", expect=200)
    assert ex2 in [e.get("execution_id") for e in c2["evidence"]]
    assert "evidence_attached" in [e["event_type"] for e in c2["events"]] and c2["attribution"] is None
    # Una ejecución con DDL no relacionado (otra columna) no se adjunta como evidencia.
    execution([{"object": "public.ventas", "action": "add_column", "columns": ["no_existe"]}])
    c2b = b.admin("GET", f"/admin/structural-changes/{c2['id']}", expect=200)
    assert len([e for e in c2b["evidence"] if e["type"] == "nexus_execution"]) == len(
        [e for e in c2["evidence"] if e["type"] == "nexus_execution"])
    # La coincidencia con el catálogo Nexus también es solo evidencia.
    b.admin("POST", "/admin/objects", {"company_id": d["company"]["id"], "name": "Clientes NX",
                                       "destination_table": "public.clientes_nx"}, expect=201)
    snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", v3)) +
             [obj("public", "clientes_nx", "table", table({"id": col("integer")}))])
    cn = change_for(b, d, "clientes_nx")
    assert [e["type"] for e in cn["evidence"]] == ["nexus_catalog"] and cn["attribution"] is None
    prop = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}/baseline?view=proposal", expect=200)["items"]
    assert {i["name"] for i in prop if i["nexus_catalog_match"]} == {"ventas", "clientes_nx"}
    ack(b, c2b, "client")      # el humano decide (aunque haya evidencia Nexus)
    assert b.admin("GET", f"/admin/structural-changes/{c2b['id']}", expect=200)["attribution"] == "client"


def test_reiniciar_linea_base_y_solicitar_inventario(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    snapshot(b, d, with_objects(ventas=None))
    assert len(changes(b, d)) == 1
    r = requests.post(b.url + f"/admin/monitored-databases/{d['mdb']}/baseline/reset",
                      headers={"x-admin-token": b.admin_token}, json={"reason": "no"}, timeout=10)
    assert r.status_code == 422
    b.admin("POST", f"/admin/monitored-databases/{d['mdb']}/baseline/reset",
            {"reason": "Se migró el DWH a un servidor nuevo"}, expect=200)
    assert changes(b, d) == [] and baseline_items(b, d) == {}
    hist = changes(b, d, view="history")
    assert hist[0]["status"] == "superseded"
    m = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}", expect=200)
    assert m["state"] == "awaiting_first_snapshot" and m["engine_identity"] is None
    t = dwh_target(b, d)
    assert t["due"]                       # se pidió un inventario nuevo
    b.admin("POST", f"/admin/monitored-databases/{d['mdb']}/scan", expect=200)
    r = snapshot(b, d, base_objects(), engine="a" * 64)   # la identidad del servidor se vuelve a fijar
    assert r["state"] == "baseline_pending"
    hv = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}/baseline/history", expect=200)["items"]
    assert any(h["action"] == "reset" for h in hv)


# ─────────────────────────────────────────────────────────────────────────────
# Regresiones de la validación de la fase 3
# ─────────────────────────────────────────────────────────────────────────────
def test_cambiar_el_host_de_la_misma_base_conserva_monitoreo_linea_base_e_historial(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    snapshot(b, d, base_objects() + [obj("public", "extra", "table", table({"id": col("integer")}))])
    pend_before = {c["object_name"] for c in changes(b, d)}
    old_id = d["mdb"]
    # El grupo pasa a apuntar a la MISMA base física con otro nombre de host.
    new_host = "alias-" + uuid.uuid4().hex[:6] + ".invalid"
    b.admin("PUT", f"/admin/groups/{d['group']['id']}", {"warehouse_host": new_host}, expect=200)
    d2 = dict(d, key=connection_identity("dwh", "postgresql", new_host, 5432, "dwh_inv"))
    t = dwh_target(b, d2)
    assert t["granted"] and t["monitored_database_id"] != old_id      # registro nuevo por la identidad nueva
    r = snapshot(b, d2, base_objects() + [obj("public", "extra", "table", table({"id": col("integer")}))])
    assert r["monitored_database_id"] == old_id and r["merged_from"] == d2["mdb"], r
    # Un solo registro: el original con la identidad nueva, su línea base y sus alertas.
    rows = q(db, "SELECT id, identity_key, duplicate_of_id FROM monitored_database WHERE group_id = %s",
             (d["group"]["id"],))
    assert rows == [(old_id, d2["key"], None)]
    assert len(baseline_items(b, d)) == 5
    assert {c["object_name"] for c in changes(b, d)} == pend_before
    # El agente vuelve a recibir la base original y puede seguir reportando.
    t = dwh_target(b, dict(d2))
    assert t["monitored_database_id"] == old_id and t["granted"]
    r = snapshot(b, dict(d2, mdb=old_id), base_objects())
    assert r["verification"] == "verified" and r["reverted"] == 1
    ev = b.admin("GET", f"/admin/monitored-databases/{old_id}", expect=200)["events"]
    assert any(e["event_type"] == "identity_rebound" for e in ev)


def test_resolver_duplicado_fusionar_y_deshacer(ienv, db):
    b = ienv["backend"]
    host_a = "orig-" + uuid.uuid4().hex[:6] + ".invalid"
    a = new_dwh(ienv, host=host_a, db_name="dwh_dup")
    dwh_target(b, a)
    snapshot(b, a, base_objects())
    approve(b, a)
    # Otro grupo con otro host a la MISMA base física (identidad fuerte igual) → duplicado.
    c = new_dwh(ienv, host="otro-" + uuid.uuid4().hex[:6] + ".invalid", db_name="dwh_dup")
    dwh_target(b, c)
    assert snapshot(b, c, base_objects(), engine=a["engine"])["code"] == "duplicate_database"
    # Fusionar entre grupos distintos NUNCA se permite (aunque el original deje de estar vigente).
    b.admin("PUT", f"/admin/groups/{a['group']['id']}", {"warehouse_host": "nuevo-" + uuid.uuid4().hex[:6] + ".invalid"},
            expect=200)
    r = requests.post(b.url + f"/admin/monitored-databases/{c['mdb']}/resolve-duplicate",
                      headers={"x-admin-token": b.admin_token}, json={"action": "merge", "reason": "prueba"}, timeout=10)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cross_group_merge"
    assert len(baseline_items(b, a)) == 5            # la línea base de A no se movió

    # Fusión manual dentro del MISMO grupo: G2 comparte la base de G (misma identidad de configuración).
    host_g = "g-" + uuid.uuid4().hex[:6] + ".invalid"
    g = new_dwh(ienv, host=host_g, db_name="dwh_m")
    dwh_target(b, g)
    snapshot(b, g, base_objects())
    approve(b, g)
    g2 = new_dwh(ienv, host=host_g, db_name="dwh_m")
    assert [t for t in lease(b, g2["h"])["targets"] if t["kind"] == "dwh"][0]["monitored_database_id"] == g["mdb"]
    # G cambia a un alias de la misma base mientras G2 sigue apuntando al original → duplicado (vigente).
    alias = "alias-" + uuid.uuid4().hex[:6] + ".invalid"
    b.admin("PUT", f"/admin/groups/{g['group']['id']}", {"warehouse_host": alias}, expect=200)
    gn = dict(g, key=connection_identity("dwh", "postgresql", alias, 5432, "dwh_m"))
    dwh_target(b, gn)
    assert snapshot(b, gn, base_objects())["code"] == "duplicate_database"
    r = requests.post(b.url + f"/admin/monitored-databases/{gn['mdb']}/resolve-duplicate",
                      headers={"x-admin-token": b.admin_token}, json={"action": "merge", "reason": "prueba"}, timeout=10)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "original_still_current"
    # G2 migra a otra base: el original deja de estar vigente → ahora sí se fusiona (mismo grupo G).
    b.admin("PUT", f"/admin/groups/{g2['group']['id']}", {"warehouse_host": "g2-" + uuid.uuid4().hex[:6] + ".invalid"},
            expect=200)
    r = b.admin("POST", f"/admin/monitored-databases/{gn['mdb']}/resolve-duplicate",
                {"action": "merge", "reason": "G migró a un alias"}, expect=200)
    assert r["id"] == g["mdb"]
    assert q(db, "SELECT identity_key FROM monitored_database WHERE id = %s", (g["mdb"],))[0][0] == gn["key"]
    assert len(baseline_items(b, g)) == 5
    ev = b.admin("GET", f"/admin/monitored-databases/{g['mdb']}", expect=200)["events"]
    assert any(e["event_type"] == "identity_rebound" and e["actor"] == "admin" for e in ev)
    # Deshacer: una detección errónea deja de marcarse sola.
    e = new_dwh(ienv, db_name="dwh_dup")
    dwh_target(b, e)
    strong_x = hashlib.sha256(b"x-" + e["key"].encode()).hexdigest()
    other = new_dwh(ienv, db_name="dwh_dup")
    dwh_target(b, other)
    snapshot(b, other, base_objects(), engine=strong_x)
    assert snapshot(b, e, base_objects(), engine=strong_x)["code"] == "duplicate_database"
    b.admin("POST", f"/admin/monitored-databases/{e['mdb']}/resolve-duplicate",
            {"action": "undo", "reason": "Son bases distintas (clon restaurado)"}, expect=200)
    dwh_target(b, e)
    r = snapshot(b, e, base_objects(), engine=strong_x)
    assert r["status"] == "ok" and r["state"] == "baseline_pending"
    ev = b.admin("GET", f"/admin/monitored-databases/{e['mdb']}", expect=200)["events"]
    assert any(x["event_type"] == "duplicate_undone" for x in ev)


def test_colision_de_identidad_debil_entre_clientes_no_duplica_ni_fusiona(ienv, db):
    """Repro del validador: la identidad débil colisiona trivialmente entre clientes distintos."""
    b = ienv["backend"]
    W = hashlib.sha256(b"addr:127.0.0.1:5432|16384|dwh").hexdigest()
    a = new_dwh(ienv)
    dwh_target(b, a)
    snapshot(b, a, base_objects(), engine=W, strength="weak", weak=W)
    approve(b, a)
    v2 = table({"id": col("integer", True), "total": col("numeric"), "iva": col("text")})
    snapshot(b, a, with_objects(ventas=obj("public", "ventas", "table", v2)), engine=W, strength="weak", weak=W)
    alerts_a = {c["id"] for c in changes(b, a)}
    # Cliente B: otro servidor (fuerte propia), misma débil → NO es duplicado; solo aviso.
    bb = new_dwh(ienv)
    dwh_target(b, bb)
    r = snapshot(b, bb, [obj("public", "otra", "table", table({"x": col("int")}))],
                 engine=hashlib.sha256(b"otro-sysid").hexdigest(), strength="strong", weak=W, verified=("public",))
    assert r["status"] == "ok" and r["state"] == "baseline_pending", r
    m = b.admin("GET", f"/admin/monitored-databases/{bb['mdb']}", expect=200)
    assert m["duplicate_of_id"] is None and any(e["event_type"] == "possible_duplicate" for e in m["events"])
    # A cambia de servidor y un tercer cliente C reporta la misma débil: NO hereda nada de A.
    b.admin("PUT", f"/admin/groups/{a['group']['id']}", {"warehouse_host": "nuevo-" + uuid.uuid4().hex[:5] + ".invalid"},
            expect=200)
    c = new_dwh(ienv)
    dwh_target(b, c)
    r = snapshot(b, c, [obj("public", "de_c", "table", table({"x": col("int")}))],
                 engine=W, strength="weak", weak=W, verified=("public",))
    assert r["status"] == "ok" and "merged_from" not in r and r["monitored_database_id"] == c["mdb"], r
    ma = b.admin("GET", f"/admin/monitored-databases/{a['mdb']}", expect=200)
    assert ma["group_id"] == a["group"]["id"] and ma["duplicate_of_id"] is None
    assert {x["id"] for x in changes(b, a)} == alerts_a
    assert all(x["group_id"] == a["group"]["id"] for x in changes(b, a))
    assert baseline_items(b, c, view="proposal") and baseline_items(b, c) == {}
    # Fuerte igual pero de OTRO grupo cuyo original ya no está vigente → duplicado, nunca fusión automática.
    x = monitored(b, new_dwh(ienv))
    y = new_dwh(ienv)
    dwh_target(b, y)
    b.admin("PUT", f"/admin/groups/{x['group']['id']}", {"warehouse_host": "fuera-" + uuid.uuid4().hex[:5] + ".invalid"},
            expect=200)
    r = snapshot(b, y, base_objects(), engine=x["engine"])
    assert r.get("code") == "duplicate_database" and "merged_from" not in r
    assert len(baseline_items(b, x)) == 5


def test_eliminaciones_solo_en_esquemas_verificados_y_snapshot_vacio_sospechoso(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    t = dwh_target(b, d)
    assert t["expected_schemas"] == ["public", "secreto"]
    # "complete" pero sin listar "secreto" como verificado → no se infieren eliminaciones ahí.
    objs = [o for o in base_objects() if o["schema_name"] != "secreto"]
    r = snapshot(b, d, objs, verified=("public",))
    assert r["verification"] == "verified" and r["detected"] == 0 and r["missing"] == 0
    # Inventario vacío cuando había objetos → no confiable (no se borra todo).
    r = snapshot(b, d, [], verified=("public", "secreto"))
    assert r["verification"] == "unverifiable" and r["reason_code"] == "EMPTY_SNAPSHOT_SUSPICIOUS"
    assert changes(b, d) == [] and len(baseline_items(b, d)) == 5
    m = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}", expect=200)
    assert m["effective_status"] == "unverifiable"


def test_snapshot_mas_viejo_que_el_ultimo_se_ignora(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    snapshot(b, d, with_objects(ventas=None))
    pend = changes(b, d)
    assert len(pend) == 1
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    r = snapshot(b, d, base_objects(), captured=old)
    assert r["status"] == "ignored" and r["reason"] == "stale_snapshot"
    assert [c["id"] for c in changes(b, d)] == [pend[0]["id"]]          # no se revirtió con datos viejos
    # El mismo snapshot reenviado (idempotencia) sigue respondiendo "duplicate".
    body = snapshot(b, d, with_objects(ventas=None), raw=True)
    r1 = requests.post(b.url + "/agent/inventory/snapshots", headers=d["h"], json=body, timeout=10).json()
    r2 = requests.post(b.url + "/agent/inventory/snapshots", headers=d["h"], json=body, timeout=10).json()
    assert r1["status"] == "ok" and r2["status"] == "duplicate"


def test_snapshot_comprimido_con_limites_anti_zip_bomb(ienv, db):
    import gzip
    import json as _json
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    hdr = dict(d["h"], **{"Content-Type": "application/json", "Content-Encoding": "gzip"})
    body = snapshot(b, d, base_objects(), raw=True)
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=hdr,
                      data=gzip.compress(_json.dumps(body).encode()), timeout=10)
    assert r.status_code == 200 and r.json()["verification"] == "verified", r.text
    # Descomprimido > inventory_max_decompressed_bytes (2 MB en esta prueba) → 413 sin procesar.
    bomb = gzip.compress(b'{"x":"' + b"0" * 3_000_000 + b'"}')
    assert len(bomb) < 100_000
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=hdr, data=bomb, timeout=10)
    assert r.status_code == 413
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=hdr, data=b"no es gzip", timeout=10)
    assert r.status_code == 400
    r = requests.post(b.url + "/agent/inventory/snapshots", timeout=10, data=b"{}",
                      headers=dict(d["h"], **{"Content-Type": "application/json", "Content-Encoding": "br"}))
    assert r.status_code == 415
    r = requests.post(b.url + "/agent/heartbeat", timeout=10, data=gzip.compress(b"{}"),
                      headers=dict(d["h"], **{"Content-Type": "application/json", "Content-Encoding": "gzip"}))
    assert r.status_code == 415                      # solo el endpoint de inventario admite gzip


def test_identidad_debil_a_fuerte_es_compatible(ienv, db):
    b = ienv["backend"]
    d = new_dwh(ienv)
    dwh_target(b, d)
    weak = hashlib.sha256(b"weak-" + d["key"].encode()).hexdigest()
    strong = hashlib.sha256(b"strong-" + d["key"].encode()).hexdigest()
    snapshot(b, d, base_objects(), engine=weak, strength="weak", weak=weak)
    approve(b, d)
    # El rol ahora puede leer system_identifier: identidad fuerte + la misma débil → compatible.
    r = snapshot(b, d, base_objects(), engine=strong, strength="strong", weak=weak)
    assert r["verification"] == "verified", r
    row = q(db, "SELECT engine_identity, engine_identity_strength, engine_identity_weak FROM monitored_database "
                "WHERE id = %s", (d["mdb"],))[0]
    assert (row[0], row[1], row[2]) == (strong, "strong", weak)
    # Cambió la IP interna pero es el mismo clúster (fuerte igual) → compatible.
    r = snapshot(b, d, base_objects(), engine=strong, strength="strong", weak="c" * 64)
    assert r["verification"] == "verified"
    # Vuelve a débil (se perdió el permiso) con la débil vigente → compatible.
    r = snapshot(b, d, base_objects(), engine="c" * 64, strength="weak", weak="c" * 64)
    assert r["verification"] == "verified"
    # Fuerte distinta → otro servidor.
    r = snapshot(b, d, base_objects(), engine="e" * 64, strength="strong", weak="c" * 64)
    assert r["reason_code"] == "ENGINE_IDENTITY_CHANGED"


def test_estructura_con_lista_blanca_y_evidencia_especifica(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    st = table({"id": col("integer", True), "total": col("numeric"), "iva": col("numeric")})
    st["columns"]["iva"]["secreto"] = "no-debe-guardarse"
    st["malicioso"] = {"x": "y"}
    snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", st)))
    c = change_for(b, d, "ventas")
    assert "malicioso" not in c["current_structure"] and "secreto" not in c["current_structure"]["columns"]["iva"]
    o = b.admin("POST", "/admin/objects", {"company_id": d["company"]["id"], "name": "Ventas", "destination_table":
                                           "ventas"}, expect=201)
    t = b.admin("POST", "/admin/tasks", {"agency_id": d["agencies"][0]["id"], "object_catalog_id": o["id"],
                                         "extract_sql": "SELECT 1 AS id", "schedule_seconds": 3600}, expect=201)
    ex = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    requests.post(b.url + "/agent/executions", headers=d["h"], timeout=10, json={
        "execution_id": ex, "task_id": t["id"], "started_at": now.isoformat(), "agent_seq": next(SEQ)}).raise_for_status()
    requests.put(b.url + f"/agent/executions/{ex}", headers=d["h"], timeout=10, json={
        "task_id": t["id"], "status": "success", "finished_at": (now + timedelta(seconds=1)).isoformat(),
        "agent_seq": next(SEQ), "ddl_applied": [
            {"object": "public.ventas", "action": "constraint_ddl"},           # no hay cambio de restricción
            {"object": "public.ventas", "action": "create_table"},             # no es objeto nuevo
            {"object": "public.ventas", "action": "add_column", "columns": ["iva", "otra"]}]}).raise_for_status()
    c = b.admin("GET", f"/admin/structural-changes/{c['id']}", expect=200)
    ev = [e for e in c["evidence"] if e["type"] == "nexus_execution"]
    assert [(e["action"], e["columns"]) for e in ev] == [("add_column", ["iva"])]


def test_ajustes_de_alcance_definiciones_contadores_y_vinculos(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    snapshot(b, d, base_objects() + [obj("secreto", "nuevo", "table", table({"id": col("integer")}))])
    c = change_for(b, d, "nuevo")
    # Excluir el esquema cierra su alerta pendiente como fuera de alcance (historial intacto).
    b.admin("PUT", f"/admin/monitored-databases/{d['mdb']}", {"schema_exclude": ["secreto"]}, expect=200)
    c = b.admin("GET", f"/admin/structural-changes/{c['id']}", expect=200)
    assert c["status"] == "out_of_scope" and "out_of_scope" in [e["event_type"] for e in c["events"]]
    assert changes(b, d) == []
    # Deshabilitar el SQL de vistas borra lo guardado cifrado.
    b.admin("PUT", f"/admin/monitored-databases/{d['mdb']}", {"view_definitions_enabled": True}, expect=200)
    sql = "SELECT id FROM ventas WHERE id > 5"
    snapshot(b, d, with_objects(v_vieja=obj("public", "v_vieja", "view", view({"id": col("integer")}, sql),
                                            definition=sql)))
    assert q(db, "SELECT COUNT(*) FROM structural_change WHERE monitored_database_id = %s "
                 "AND current_definition_enc IS NOT NULL", (d["mdb"],))[0][0] == 1
    b.admin("PUT", f"/admin/monitored-databases/{d['mdb']}", {"view_definitions_enabled": False}, expect=200)
    for tbl, cols in (("inventory_object_state", "definition_enc"), ("inventory_baseline", "definition_enc"),
                      ("structural_change", "COALESCE(previous_definition_enc, current_definition_enc)")):
        assert q(db, f"SELECT COUNT(*) FROM {tbl} WHERE monitored_database_id = %s AND {cols} IS NOT NULL",
                 (d["mdb"],))[0][0] == 0
    ev = b.admin("GET", f"/admin/monitored-databases/{d['mdb']}", expect=200)["events"]
    assert any(e["event_type"] == "view_definitions_purged" for e in ev)
    # El contador del menú y el resumen usan los mismos criterios (sin bases deshabilitadas).
    badge = b.admin("GET", "/admin/structural-changes/badge", expect=200)
    summ = b.admin("GET", "/admin/inventory/summary", expect=200)
    assert badge["pending_changes"] == summ["pending_changes"]
    b.admin("PUT", f"/admin/monitored-databases/{d['mdb']}", {"enabled": False}, expect=200)
    badge2 = b.admin("GET", "/admin/structural-changes/badge", expect=200)
    summ2 = b.admin("GET", "/admin/inventory/summary", expect=200)
    assert badge2["pending_changes"] == summ2["pending_changes"] == badge["pending_changes"] - 1
    # Borrar un grupo limpia sus vínculos.
    g = b.admin("POST", "/admin/groups", {"name": "Grupo efímero " + uuid.uuid4().hex[:4]}, expect=201)
    q(db, "INSERT INTO monitored_database_link (monitored_database_id, group_id, company_id) VALUES (%s, %s, 0) "
          "RETURNING 1", (d["mdb"], g["id"]))
    b.admin("DELETE", f"/admin/groups/{g['id']}", expect=200)
    assert not q(db, "SELECT 1 FROM monitored_database_link WHERE group_id = %s", (g["id"],))


def _rss_mb(b):
    import os as _os
    return int(_os.popen(f"ps -o rss= -p {b.proc.pid}").read().strip()) // 1024


def test_bomba_json_gzip_sin_credencial_no_se_descomprime_y_memoria_acotada(ienv, db):
    import gzip
    import json as _json
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    bomb = gzip.compress(b'{"x":[' + b"{}," * 20_000_000 + b'{}]}', 9)      # ~60 MB descomprimido
    gz_hdr = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
    base_rss = _rss_mb(b)
    t0 = time.time()
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=gz_hdr, data=bomb, timeout=60)
    assert r.status_code == 401 and time.time() - t0 < 5                # sin credencial: ni se lee ni se descomprime
    bad = dict(gz_hdr, **{"x-installation-id": str(uuid.uuid4()), "x-installation-secret": "x" * 40})
    assert requests.post(b.url + "/agent/inventory/snapshots", headers=bad, data=bomb, timeout=60).status_code == 401
    # Con credencial: límite descomprimido → 413, y memoria acotada.
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=dict(d["h"], **gz_hdr), data=bomb, timeout=60)
    assert r.status_code == 413
    # JSON "bomba" dentro del límite de bytes (sin comprimir): tope de contenedores → 413 antes de parsear.
    raw = b'{"x":[' + b"{}," * 1_000_000 + b'{}]}'
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=dict(d["h"], **{"Content-Type": "application/json"}),
                      data=raw, timeout=60)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_many_json_containers"
    # Concurrencia: 4 a la vez → acotado (429 para las que exceden) y el backend sigue respondiendo.
    import threading as _th
    codes = []
    ths = [_th.Thread(target=lambda: codes.append(requests.post(
        b.url + "/agent/inventory/snapshots", headers=dict(d["h"], **gz_hdr), data=bomb, timeout=60).status_code))
        for _ in range(4)]
    [t.start() for t in ths]
    assert requests.get(b.url + "/health", timeout=10).status_code == 200
    [t.join() for t in ths]
    assert set(codes) <= {413, 429}
    for _ in range(4):
        requests.post(b.url + "/agent/inventory/snapshots", headers=gz_hdr, data=bomb, timeout=60)
    assert _rss_mb(b) - base_rss < 150, (base_rss, _rss_mb(b))
    # Cuerpos grandes en otros /agent/* sin credencial: 401 antes de leerlos.
    r = requests.post(b.url + "/agent/heartbeat", headers={"Content-Type": "application/json"},
                      data=b'{"x":"' + b"a" * 100_000 + b'"}', timeout=10)
    assert r.status_code == 401
    # Miembros gzip concatenados → 400; el snapshot legítimo sigue funcionando.
    body = snapshot(b, d, base_objects(), raw=True)
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=dict(d["h"], **gz_hdr), timeout=10,
                      data=gzip.compress(_json.dumps(body).encode()) + gzip.compress(b"{}"))
    assert r.status_code == 400
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=dict(d["h"], **gz_hdr), timeout=10,
                      data=gzip.compress(_json.dumps(body).encode()))
    assert r.status_code == 200 and r.json()["verification"] == "verified"


def test_reloj_futuro_snapshot_id_ajeno_y_viejo_cuenta_como_intento(ienv, db):
    b = ienv["backend"]
    d = monitored(b, new_dwh(ienv))
    now = datetime.now(timezone.utc)
    r = snapshot(b, d, base_objects(), captured=now + timedelta(days=3650), expect=422)
    assert r["detail"]["code"] == "invalid_time"
    v2 = table({"id": col("integer", True), "total": col("numeric"), "iva": col("text")})
    r = snapshot(b, d, with_objects(ventas=obj("public", "ventas", "table", v2)))
    assert r["status"] == "ok" and r["detected"] == 1                   # el monitoreo no se congeló
    # Viejo → ignorado, pero cuenta como intento: la base no queda "vencida" en cada ciclo.
    b.admin("POST", f"/admin/monitored-databases/{d['mdb']}/scan", expect=200)
    r = snapshot(b, d, base_objects(), captured=now - timedelta(hours=2))
    assert r["status"] == "ignored"
    assert dwh_target(b, d)["due"] is False
    # snapshot_id ya usado en otra base → 409 (antes: 500).
    body = snapshot(b, d, base_objects(), raw=True)
    requests.post(b.url + "/agent/inventory/snapshots", headers=d["h"], json=body, timeout=10).raise_for_status()
    o = new_dwh(ienv)
    dwh_target(b, o)
    body2 = snapshot(b, o, base_objects(), raw=True)
    body2["snapshot_id"] = body["snapshot_id"]
    r = requests.post(b.url + "/agent/inventory/snapshots", headers=o["h"], json=body2, timeout=10)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "snapshot_id_conflict"
