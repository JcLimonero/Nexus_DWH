"""
Destino (DWH) configurable: esquema, SSL/TLS, destino por empresa y "Probar
conexión" ejecutada por el agente (sección 22 de DWH_README.md).

BD propia (nexus_test_cfg_dest) y backend local en subproceso. Todos los datos
son ficticios; las contraseñas se generan aquí y nunca se imprimen.
"""

import secrets
import uuid

import psycopg2
import pytest
import requests

import support

DB = "nexus_test_cfg_dest"
T = 15
FEAT = {"x-nexus-agent-features": "destination-v2,connection-test"}
CA_PEM = ("-----BEGIN CERTIFICATE-----\n"
          "MIIBszCCAVmgAwIBAgIUQ2VydGlmaWNhZG9EZVBydWViYUNBMAoGCCqGSM49BAMC\n"
          "MBAxDjAMBgNVBAMMBVBydWViYTAeFw0yNjAxMDEwMDAwMDBaFw0zNjAxMDEwMDAw\n"
          "-----END CERTIFICATE-----\n")


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


def enroll(url, header, token, name):
    r = requests.post(url + "/agent/enroll", headers={header: token},
                      json={"name": name, "hostname": "h", "client_version": "5.3.0"}, timeout=T)
    assert r.status_code == 201, r.text
    d = r.json()
    return {"x-installation-id": d["installation_id"], "x-installation-secret": d["secret"]}, d["installation_id"]


def heartbeat(url, h, features=("destination-v2", "connection-test")):
    r = requests.post(url + "/agent/heartbeat", headers=h, timeout=T,
                      json={"client_version": "5.3.0", "features": list(features)})
    assert r.status_code == 200, r.text
    return r.json()


def tasks(url, h, features=True):
    r = requests.get(url + "/agent/tasks", headers={**h, **(FEAT if features else {})}, timeout=T)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def denv():
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.recreate_config_db(DB)
    b = support.Backend(DB)
    b.start()
    try:
        g_pass, c_pass, src_pass = pw(), pw(), pw()
        g = b.admin("POST", "/admin/groups", {
            "name": "Grupo Destino", "warehouse_host": "dwh-grupo.invalid", "warehouse_port": 5432,
            "warehouse_database": "dwh_grupo", "warehouse_username": "usr_grupo", "warehouse_password": g_pass,
            "warehouse_schema": "dwh_g"}, expect=201)
        other = b.admin("POST", "/admin/groups", {
            "name": "Grupo Ajeno", "warehouse_host": "dwh-ajeno.invalid", "warehouse_database": "dwh_ajeno",
            "warehouse_username": "usr_ajeno", "warehouse_password": pw()}, expect=201)
        c_inh = b.admin("POST", "/admin/companies", {
            "group_id": g["id"], "name": "Empresa Hereda", "source_type": "postgresql",
            "source_host": "src-hereda.invalid", "source_port": 5432, "source_database": "dms_h",
            "source_username": "src_user_h", "source_password": src_pass}, expect=201)
        c_own = b.admin("POST", "/admin/companies", {
            "group_id": g["id"], "name": "Empresa Propia", "source_type": "postgresql",
            "source_host": "src-propia.invalid", "source_database": "dms_p", "source_username": "src_user_p",
            "source_password": pw(),
            "warehouse_mode": "custom", "warehouse_host": "dwh-propio.invalid", "warehouse_port": 6543,
            "warehouse_database": "dwh_propio", "warehouse_username": "usr_propio", "warehouse_password": c_pass,
            "warehouse_schema": "ventas", "warehouse_sslmode": "verify-full", "warehouse_sslrootcert": CA_PEM},
            expect=201)
        c_other = b.admin("POST", "/admin/companies", {
            "group_id": other["id"], "name": "Empresa Ajena", "source_type": "postgresql",
            "source_host": "src-ajena.invalid", "source_database": "dms_a", "source_username": "u",
            "source_password": pw()}, expect=201)
        ag = {}
        for comp in (c_inh, c_own, c_other):
            ag[comp["id"]] = b.admin("POST", "/admin/agencies", {"company_id": comp["id"], "name": "Ag " + comp["name"]},
                                     expect=201)
        objs = {}
        for comp in (c_inh, c_own, c_other):
            objs[comp["id"]] = [
                b.admin("POST", "/admin/objects", {"company_id": comp["id"], "name": "Clientes",
                                                   "destination_table": "clientes", "upsert_keys": "id"}, expect=201),
                b.admin("POST", "/admin/objects", {"company_id": comp["id"], "name": "Explicito",
                                                   "destination_table": "otro.explicito", "upsert_keys": "id"},
                        expect=201),
            ]
        tks = {}
        for comp in (c_inh, c_own, c_other):
            tks[comp["id"]] = [b.admin("POST", "/admin/tasks", {
                "agency_id": ag[comp["id"]]["id"], "object_catalog_id": o["id"], "extract_sql": "SELECT 1 AS id",
                "schedule_seconds": 3600}, expect=201)["id"] for o in objs[comp["id"]]]
        users = {}
        for name, roles, extra in (("cfg_d", [{"role": "admin_config", "group_id": g["id"]}], {}),
                                   ("creds_d", [{"role": "admin_credenciales", "group_id": g["id"]},
                                                {"role": "admin_config", "group_id": g["id"]}], {}),
                                   ("viewer_d", [{"role": "lectura", "group_id": g["id"]}], {}),
                                   ("ajeno_d", [{"role": "admin_config", "group_id": other["id"]}], {})):
            p = pw()
            b.admin("POST", "/admin/users", {"username": name, "password": p, "must_change_password": False,
                                             "roles": roles, **extra}, expect=201)
            r = requests.post(b.url + "/admin/auth/login", json={"username": name, "password": p}, timeout=T)
            assert r.status_code == 200, r.text
            users[name] = {"authorization": "Bearer " + r.json()["token"]}
        yield dict(b=b, g=g, other=other, c_inh=c_inh, c_own=c_own, c_other=c_other, tasks=tks, users=users,
                   g_pass=g_pass, c_pass=c_pass, src_pass=src_pass)
    finally:
        b.stop()
        support.drop_config_db(DB)


def age_tests():
    """Las pruebas anteriores dejan de contar para el intervalo mínimo por destino y el tope por usuario."""
    q("UPDATE connection_test SET created_at = created_at - interval '1 hour'")


def call(env, who, method, path, body=None, expect=None):
    r = requests.request(method, env["b"].url + path, json=body, headers=env["users"][who], timeout=T)
    if expect is not None:
        assert r.status_code == expect, f"{who} {method} {path} -> {r.status_code}: {r.text[:300]}"
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Modelo, cifrado y salida del panel
# ─────────────────────────────────────────────────────────────────────────────
def test_destino_propio_cifrado_y_resumen_efectivo(denv):
    b, own, inh = denv["b"], denv["c_own"], denv["c_inh"]
    row = q("SELECT warehouse_mode, warehouse_host, warehouse_password, warehouse_schema, warehouse_sslmode "
            "FROM company WHERE id = %s", (own["id"],))[0]
    assert row[0] == "custom" and row[1].startswith("ENC:") and row[2].startswith("ENC:")
    assert row[3] == "ventas" and row[4] == "verify-full"
    assert own["warehouse_host"] == "dwh-propio.invalid" and own["warehouse_has_password"] is True
    assert "warehouse_password" not in own and denv["c_pass"] not in str(own)
    assert own["effective_warehouse"]["source"] == "company"
    assert own["effective_warehouse"]["schema"] == "ventas"
    assert own["warehouse_has_sslrootcert"] is True
    assert inh["warehouse_mode"] == "inherit"
    assert inh["effective_warehouse"] == {"source": "group", "host": "dwh-grupo.invalid", "port": 5432,
                                          "database": "dwh_grupo", "schema": "dwh_g", "sslmode": "prefer",
                                          "configured": True}
    g = b.admin("GET", f"/admin/groups/{denv['g']['id']}", expect=200)
    assert g["warehouse_schema"] == "dwh_g" and g["warehouse_sslmode"] == "prefer"
    assert g["custom_destination_count"] == 1


def test_validaciones(denv):
    b, gid, cid = denv["b"], denv["g"]["id"], denv["c_own"]["id"]
    for body in ({"warehouse_schema": "Ventas"}, {"warehouse_schema": "1abc"}, {"warehouse_schema": "pg_x"},
                 {"warehouse_schema": "a;drop"}, {"warehouse_sslmode": "sometimes"},
                 {"warehouse_sslrootcert": "no es un certificado"}, {"warehouse_port": 70000}):
        b.admin("PUT", f"/admin/groups/{gid}", body, expect=422)
        b.admin("PUT", f"/admin/companies/{cid}", body, expect=422)
    # Destino propio incompleto
    r = b.admin("POST", "/admin/companies", {"group_id": gid, "name": "Incompleta", "warehouse_mode": "custom",
                                             "warehouse_host": "x.invalid"}, expect=422)
    assert "incompleto" in str(r).lower()
    # Datos de destino sin elegir 'custom'
    b.admin("POST", "/admin/companies", {"group_id": gid, "name": "SinModo", "warehouse_host": "x.invalid"},
            expect=422)
    b.admin("PUT", f"/admin/companies/{denv['c_inh']['id']}", {"warehouse_host": "x.invalid"}, expect=422)
    # Vaciar el host de un destino propio → incompleto
    b.admin("PUT", f"/admin/companies/{cid}", {"warehouse_host": ""}, expect=422)
    assert q("SELECT warehouse_mode FROM company WHERE id = %s", (cid,))[0][0] == "custom"


def test_permisos_destino(denv):
    gid, cid = denv["g"]["id"], denv["c_inh"]["id"]
    # admin_config: puede renombrar, NO cambiar esquema/SSL/destino (credentials.manage).
    call(denv, "cfg_d", "PUT", f"/admin/groups/{gid}", {"warehouse_schema": "otro"}, expect=403)
    call(denv, "cfg_d", "PUT", f"/admin/groups/{gid}", {"warehouse_sslmode": "require"}, expect=403)
    call(denv, "cfg_d", "PUT", f"/admin/companies/{cid}", {"warehouse_mode": "custom"}, expect=403)
    call(denv, "cfg_d", "PUT", f"/admin/companies/{cid}", {"name": "Empresa Hereda"}, expect=200)
    # Sin credentials.manage: se ve el esquema y el modo, no host/base del destino.
    r = call(denv, "viewer_d", "GET", f"/admin/companies/{denv['c_own']['id']}", expect=200).json()
    assert r["warehouse_mode"] == "custom" and r["effective_warehouse"]["schema"] == "ventas"
    assert r["warehouse_host"] is None and r["effective_warehouse"]["host"] is None
    assert r["warehouse_sslrootcert"] is None and r["warehouse_has_sslrootcert"] is True
    # Otro grupo: no existe.
    call(denv, "ajeno_d", "GET", f"/admin/companies/{cid}", expect=404)
    call(denv, "ajeno_d", "PUT", f"/admin/companies/{cid}", {"warehouse_mode": "custom"}, expect=404)
    # admin_credenciales: puede.
    call(denv, "creds_d", "PUT", f"/admin/groups/{gid}", {"warehouse_sslrootcert": CA_PEM}, expect=200)
    call(denv, "creds_d", "PUT", f"/admin/groups/{gid}", {"warehouse_sslrootcert": ""}, expect=200)


def test_volver_a_heredar_descarta_el_destino_propio(denv):
    b, gid = denv["b"], denv["g"]["id"]
    c = b.admin("POST", "/admin/companies", {
        "group_id": gid, "name": "Temporal", "warehouse_mode": "custom", "warehouse_host": "tmp.invalid",
        "warehouse_database": "tmp", "warehouse_username": "tmp_user", "warehouse_password": pw()}, expect=201)
    assert c["effective_warehouse"]["source"] == "company"
    c = b.admin("PUT", f"/admin/companies/{c['id']}", {"warehouse_mode": "inherit"}, expect=200)
    assert c["warehouse_mode"] == "inherit" and c["effective_warehouse"]["source"] == "group"
    row = q("SELECT warehouse_host, warehouse_password, warehouse_schema FROM company WHERE id = %s", (c["id"],))[0]
    assert row == ("", "", "public")
    b.admin("DELETE", f"/admin/companies/{c['id']}", expect=200)


# ─────────────────────────────────────────────────────────────────────────────
# /agent/tasks
# ─────────────────────────────────────────────────────────────────────────────
def test_tareas_con_destino_efectivo_por_tarea(denv):
    b = denv["b"]
    h, _ = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-grupo")
    d = tasks(b.url, h)
    assert d["warehouse"]["host"] == "dwh-grupo.invalid" and d["warehouse"]["schema"] == "dwh_g"
    by = {t["task_id"]: t for t in d["tasks"]}
    t_inh, t_inh_expl = denv["tasks"][denv["c_inh"]["id"]]
    t_own, t_own_expl = denv["tasks"][denv["c_own"]["id"]]
    assert by[t_inh]["warehouse"]["host"] == "dwh-grupo.invalid"
    assert by[t_inh]["warehouse"]["password"] == denv["g_pass"]
    assert by[t_inh]["load_table"] == "dwh_g.clientes"
    assert by[t_inh_expl]["load_table"] == "otro.explicito"
    w = by[t_own]["warehouse"]
    assert (w["host"], w["port"], w["database"], w["schema"], w["sslmode"], w["source"]) == \
        ("dwh-propio.invalid", 6543, "dwh_propio", "ventas", "verify-full", "company")
    assert w["password"] == denv["c_pass"] and "BEGIN CERTIFICATE" in w["sslrootcert"]
    assert by[t_own]["load_table"] == "ventas.clientes"
    assert d["withheld_tasks"] == []
    # Otro grupo nunca aparece.
    assert not set(denv["tasks"][denv["c_other"]["id"]]) & set(by)


def test_agente_anterior_no_recibe_tareas_que_no_puede_cumplir(denv):
    b = denv["b"]
    h, _ = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-vieja")
    d = tasks(b.url, h, features=False)
    ids = {t["task_id"] for t in d["tasks"]}
    own = set(denv["tasks"][denv["c_own"]["id"]])
    assert not ids & own
    assert {w["task_id"] for w in d["withheld_tasks"]} == own
    assert {w["reason"] for w in d["withheld_tasks"]} == {"destination_per_company"}
    assert set(denv["tasks"][denv["c_inh"]["id"]]) <= ids
    # Descargas auditadas: solo lo entregado.
    iid = h["x-installation-id"]
    got = {r[0] for r in q("SELECT task_id FROM task_download_log WHERE installation_id = %s", (iid,))}
    assert not got & own
    # Instalación de EMPRESA con destino propio: su destino es el principal → sin retención por destino,
    # pero verify-full no lo cumpliría un agente anterior.
    hc, _ = enroll(b.url, "x-token", denv["c_own"]["company_token"], "inst-emp-vieja")
    d = tasks(b.url, hc, features=False)
    assert d["warehouse"]["host"] == "dwh-propio.invalid"
    assert {w["reason"] for w in d["withheld_tasks"]} == {"ssl_enforced"}
    b.admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_sslmode": "prefer"}, expect=200)
    try:
        d = tasks(b.url, hc, features=False)
        assert d["withheld_tasks"] == [] and {t["task_id"] for t in d["tasks"]} == own
        assert all(t["load_table"].startswith(("ventas.", "otro.")) for t in d["tasks"])
    finally:
        b.admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_sslmode": "verify-full"}, expect=200)


def test_endpoints_legados_reciben_el_destino_efectivo(denv):
    b = denv["b"]
    r = requests.get(b.url + "/configs", headers={"x-token": denv["c_own"]["company_token"]}, timeout=T)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dwh_host"] == "dwh-propio.invalid" and d["dwh_db"] == "dwh_propio" and d["dwh_pass"] == denv["c_pass"]
    assert sorted(c["load_table"] for c in d["configs"]) == ["otro.explicito", "ventas.clientes"]
    r = requests.get(b.url + "/configs", headers={"x-token": denv["c_inh"]["company_token"]}, timeout=T)
    assert r.json()["dwh_host"] == "dwh-grupo.invalid"
    assert sorted(c["load_table"] for c in r.json()["configs"]) == ["dwh_g.clientes", "otro.explicito"]
    r = requests.get(b.url + "/group-configs", headers={"x-group-token": denv["g"]["group_token"]}, timeout=T)
    assert r.status_code == 200, r.text
    ids = {int(c["id"]) for c in r.json()["configs"]}
    assert not ids & set(denv["tasks"][denv["c_own"]["id"]])        # destino propio: no va por group-configs
    assert set(denv["tasks"][denv["c_inh"]["id"]]) <= ids


# ─────────────────────────────────────────────────────────────────────────────
# Prueba de conexión
# ─────────────────────────────────────────────────────────────────────────────
def test_prueba_sin_agente_en_linea(denv):
    # Todas las instalaciones de las pruebas anteriores no anunciaron "connection-test".
    q("UPDATE installation SET last_heartbeat = NULL")
    r = call(denv, "cfg_d", "POST", "/admin/connection-tests",
             {"target_kind": "group_dwh", "group_id": denv["g"]["id"]}, expect=201).json()
    assert r["status"] == "no_agent" and r["error_code"] == "NO_AGENT_ONLINE" and r["finished_at"]
    assert "Nexus no se conecta" in r["message"]


def test_prueba_permisos_y_alcance_del_panel(denv):
    gid = denv["g"]["id"]
    call(denv, "viewer_d", "POST", "/admin/connection-tests", {"target_kind": "group_dwh", "group_id": gid},
         expect=403)
    call(denv, "ajeno_d", "POST", "/admin/connection-tests", {"target_kind": "group_dwh", "group_id": gid},
         expect=404)
    call(denv, "ajeno_d", "POST", "/admin/connection-tests",
         {"target_kind": "company_dwh", "company_id": denv["c_inh"]["id"]}, expect=404)
    call(denv, "cfg_d", "POST", "/admin/connection-tests", {"target_kind": "company_dwh"}, expect=422)
    t = call(denv, "cfg_d", "POST", "/admin/connection-tests", {"target_kind": "group_dwh", "group_id": gid},
             expect=201).json()
    call(denv, "viewer_d", "GET", f"/admin/connection-tests/{t['id']}", expect=200)
    call(denv, "ajeno_d", "GET", f"/admin/connection-tests/{t['id']}", expect=404)
    items = call(denv, "ajeno_d", "GET", "/admin/connection-tests?target_kind=group_dwh", expect=200).json()["items"]
    assert all(i["group_id"] != gid for i in items)


def test_prueba_ciclo_completo_con_saneamiento_y_auditoria(denv):
    b, gid = denv["b"], denv["g"]["id"]
    h, iid = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-pruebas")
    hx, _ = enroll(b.url, "x-group-token", denv["other"]["group_token"], "inst-ajena")
    heartbeat(b.url, h)
    heartbeat(b.url, hx)
    t = call(denv, "cfg_d", "POST", "/admin/connection-tests",
             {"target_kind": "company_dwh", "company_id": denv["c_own"]["id"]}, expect=201).json()
    assert t["status"] == "pending" and t["eligible_installations"] >= 1
    assert heartbeat(b.url, h)["connection_tests_pending"] >= 1
    # Instalación de otro grupo: nada que tomar.
    r = requests.post(b.url + "/agent/connection-tests/claim", headers=hx, timeout=T)
    assert r.status_code == 200 and r.json()["test"] is None
    r = requests.post(b.url + "/agent/connection-tests/claim", headers=h, timeout=T).json()
    test = r["test"]
    # Puede haber pruebas previas pendientes del mismo grupo: se toman en orden.
    while test and test["id"] != t["id"]:
        requests.post(b.url + f"/agent/connection-tests/{test['id']}/result", headers=h, timeout=T,
                      json={"status": "failed", "checks": []})
        test = requests.post(b.url + "/agent/connection-tests/claim", headers=h, timeout=T).json()["test"]
    assert test and test["kind"] == "dwh"
    conn = test["connection"]
    assert (conn["host"], conn["port"], conn["schema"], conn["sslmode"]) == \
        ("dwh-propio.invalid", 6543, "ventas", "verify-full")
    assert conn["password"] == denv["c_pass"]
    running = call(denv, "cfg_d", "GET", f"/admin/connection-tests/{t['id']}", expect=200).json()
    assert running["status"] == "running" and running["installation_name"] == "inst-pruebas"
    # Otra instalación no puede reportar; el mensaje se sanea con las credenciales del alcance.
    body = {"status": "failed", "error_code": "DWH_AUTH_FAILED",
            "error_message": f"password authentication failed for user usr_propio host=dwh-propio.invalid "
                             f"pwd={denv['c_pass']}",
            "checks": [{"code": "CONNECT", "ok": False, "severity": "error",
                        "message": f"falló con {denv['c_pass']} en dwh-propio.invalid"}],
            "ssl_in_use": False, "duration_ms": 12, "agent_version": "5.3.0"}
    r = requests.post(b.url + f"/agent/connection-tests/{t['id']}/result", headers=hx, json=body, timeout=T)
    assert r.status_code == 404
    r = requests.post(b.url + f"/agent/connection-tests/{t['id']}/result", headers=h, json=body, timeout=T)
    assert r.status_code == 200, r.text
    r = requests.post(b.url + f"/agent/connection-tests/{t['id']}/result", headers=h, json=body, timeout=T)
    assert r.status_code == 409
    done = call(denv, "viewer_d", "GET", f"/admin/connection-tests/{t['id']}", expect=200).json()
    assert done["status"] == "failed" and done["error_code"] == "DWH_AUTH_FAILED"
    text = str(done)
    assert denv["c_pass"] not in text and "dwh-propio.invalid" not in text and "usr_propio" not in text
    assert done["result"]["checks"][0]["code"] == "CONNECT"
    assert done["config_changed"] is False
    # Auditoría de la solicitud.
    rows = q("SELECT action, target_id, group_id, details::text FROM panel_audit_log "
             "WHERE action = 'connection_test.request' AND target_id = %s", (t["id"],))
    assert len(rows) == 1 and rows[0][2] == gid and "company_dwh" in rows[0][3]
    # Si la configuración cambia después, el panel lo indica.
    b.admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_schema": "ventas2"}, expect=200)
    try:
        assert call(denv, "cfg_d", "GET", f"/admin/connection-tests/{t['id']}").json()["config_changed"] is True
    finally:
        b.admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_schema": "ventas"}, expect=200)
    ok = requests.post(b.url + "/agent/connection-tests/claim", headers=h, timeout=T).json()
    assert ok["test"] is None and ok["poll_seconds"] >= 3
    # Una instalación que no anunció "connection-test" en su latido no puede tomar pruebas.
    hn, _ = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-sin-capacidad")
    heartbeat(b.url, hn, features=("destination-v2",))
    age_tests()
    t3 = call(denv, "cfg_d", "POST", "/admin/connection-tests",
              {"target_kind": "company_source", "company_id": denv["c_inh"]["id"]}, expect=201).json()
    assert requests.post(b.url + "/agent/connection-tests/claim", headers=hn, timeout=T).json()["test"] is None
    assert any(x["id"] == t3["id"] for x in _claim_all(b, h))


def test_prueba_alcance_de_instalaciones_de_empresa(denv):
    b, gid = denv["b"], denv["g"]["id"]
    q("UPDATE connection_test SET status = 'expired' WHERE status IN ('pending', 'running')")
    age_tests()
    q("UPDATE installation SET last_heartbeat = NULL")
    h_inh, _ = enroll(b.url, "x-token", denv["c_inh"]["company_token"], "inst-emp-hereda")
    h_own, _ = enroll(b.url, "x-token", denv["c_own"]["company_token"], "inst-emp-propia")
    heartbeat(b.url, h_inh)
    heartbeat(b.url, h_own)

    def claim(h):
        return requests.post(b.url + "/agent/connection-tests/claim", headers=h, timeout=T).json()["test"]

    # DWH del grupo: solo la empresa que lo HEREDA puede probarlo.
    t = call(denv, "cfg_d", "POST", "/admin/connection-tests", {"target_kind": "group_dwh", "group_id": gid},
             expect=201).json()
    assert t["eligible_installations"] == 1
    assert claim(h_own) is None
    got = claim(h_inh)
    assert got["id"] == t["id"] and got["connection"]["host"] == "dwh-grupo.invalid"
    # Origen de la empresa propia: solo su instalación.
    t2 = call(denv, "cfg_d", "POST", "/admin/connection-tests",
              {"target_kind": "company_source", "company_id": denv["c_own"]["id"]}, expect=201).json()
    assert claim(h_inh) is None
    got = claim(h_own)
    assert got["id"] == t2["id"] and got["kind"] == "source" and got["connection"]["engine"] == "postgresql"


def test_prueba_vence(denv):
    b, gid = denv["b"], denv["g"]["id"]
    h, _ = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-vence")
    heartbeat(b.url, h)
    q("UPDATE connection_test SET status = 'expired' WHERE status IN ('pending', 'running')")
    age_tests()
    t = call(denv, "cfg_d", "POST", "/admin/connection-tests", {"target_kind": "group_dwh", "group_id": gid},
             expect=201).json()
    assert t["status"] == "pending"
    q("UPDATE connection_test SET expires_at = NOW() - interval '1 second' WHERE id = %s", (t["id"],))
    r = call(denv, "cfg_d", "GET", f"/admin/connection-tests/{t['id']}", expect=200).json()
    assert r["status"] == "expired" and r["error_code"] == "NOT_CLAIMED"


# ─────────────────────────────────────────────────────────────────────────────
# Inventario: destino propio = su propia base monitoreada (identidad estable)
# ─────────────────────────────────────────────────────────────────────────────
def _lease(b, h, caps):
    r = requests.post(b.url + "/agent/inventory/lease", headers=h, timeout=T,
                      json={"capabilities": caps, "client_version": "5.3.0"})
    assert r.status_code == 200, r.text
    return r.json()["targets"]


def test_inventario_registra_el_destino_propio_sin_duplicados(denv):
    import destination_postgres as dp

    b, gid = denv["b"], denv["g"]["id"]
    g_key = dp.dwh_identity({"host": "dwh-grupo.invalid", "port": 5432, "database": "dwh_grupo"})
    own_key = dp.dwh_identity({"host": "dwh-propio.invalid", "port": 6543, "database": "dwh_propio"})
    h, _ = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-inv")
    # Agente anterior (sin dwh_identities): solo su DWH principal (el del grupo).
    keys = {t["identity_key"] for t in _lease(b, h, {"dwh": True})}
    assert keys == {g_key}
    # Agente 5.3: ambos, cada uno con su registro.
    for _ in range(2):
        keys = {t["identity_key"] for t in _lease(b, h, {"dwh": True, "dwh_identities": [g_key, own_key]})}
        assert keys == {g_key, own_key}
    assert q("SELECT COUNT(*) FROM monitored_database WHERE identity_key IN (%s, %s)", (g_key, own_key))[0][0] == 2
    # Otra empresa con el MISMO destino propio → misma base, sin duplicado.
    c2 = b.admin("POST", "/admin/companies", {
        "group_id": gid, "name": "Empresa Propia 2", "warehouse_mode": "custom",
        "warehouse_host": "DWH-PROPIO.invalid", "warehouse_port": 6543, "warehouse_database": "dwh_propio",
        "warehouse_username": "otro_usuario", "warehouse_password": pw()}, expect=201)
    try:
        _lease(b, h, {"dwh": True, "dwh_identities": [g_key, own_key]})
        assert q("SELECT COUNT(*) FROM monitored_database WHERE identity_key = %s", (own_key,))[0][0] == 1
        ev = q("""SELECT e.message FROM monitored_database_event e JOIN monitored_database m
                  ON m.id = e.monitored_database_id WHERE m.identity_key = %s AND e.event_type = 'created'""",
               (own_key,))
        assert ev and "destino propio" in ev[0][0]
        # Instalación de la empresa con destino propio: su principal es el propio (no el del grupo).
        hc, _ = enroll(b.url, "x-token", denv["c_own"]["company_token"], "inst-inv-emp")
        assert {t["identity_key"] for t in _lease(b, hc, {"dwh": True})} == {own_key}
        # Un agente sin credenciales de ese destino no lo recibe.
        assert {t["identity_key"] for t in _lease(b, h, {"dwh": True, "dwh_identities": [g_key]})} == {g_key}
    finally:
        b.admin("DELETE", f"/admin/companies/{c2['id']}", expect=200)


# ─────────────────────────────────────────────────────────────────────────────
# Regresiones de la validación (observaciones)
# ─────────────────────────────────────────────────────────────────────────────
def _claim_all(b, h):
    out = []
    while True:
        t = requests.post(b.url + "/agent/connection-tests/claim", headers=h, timeout=T).json()["test"]
        if not t:
            return out
        out.append(t)


def test_resultado_hostil_no_filtra_por_codigo_ni_version(denv):
    b = denv["b"]
    q("UPDATE connection_test SET status = 'expired' WHERE status IN ('pending', 'running')")
    age_tests()
    h, _ = enroll(b.url, "x-token", denv["c_own"]["company_token"], "inst-hostil")
    heartbeat(b.url, h)
    t = call(denv, "cfg_d", "POST", "/admin/connection-tests",
             {"target_kind": "company_dwh", "company_id": denv["c_own"]["id"]}, expect=201).json()
    got = [x for x in _claim_all(b, h) if x["id"] == t["id"]]
    assert got
    pw_ = denv["c_pass"]
    body = {"status": "failed", "error_code": pw_[:60], "agent_version": pw_[:40], "server_version": pw_,
            "error_message": f"conn postgresql://usr_propio:{pw_}@dwh-propio.invalid/x",
            "checks": [{"code": "CONNECT", "ok": False, "severity": "error", "message": f"pw {pw_}"}]}
    r = requests.post(b.url + f"/agent/connection-tests/{t['id']}/result", headers=h, json=body, timeout=T)
    assert r.status_code == 200, r.text
    view = call(denv, "viewer_d", "GET", f"/admin/connection-tests/{t['id']}", expect=200).json()
    blob = str(view)
    assert pw_ not in blob and pw_[:40] not in blob and pw_[:12] not in blob
    assert view["error_code"] == "INVALID_CODE" and view["result"]["agent_version"] == "?"
    # Un código válido pero que contiene un secreto conocido también se reemplaza.
    age_tests()
    t2 = call(denv, "cfg_d", "POST", "/admin/connection-tests",
              {"target_kind": "company_dwh", "company_id": denv["c_own"]["id"]}, expect=201).json()
    _claim_all(b, h)
    r = requests.post(b.url + f"/agent/connection-tests/{t2['id']}/result", headers=h, timeout=T,
                      json={"status": "failed", "error_code": "DWH_USR_PROPIO", "agent_version": "5.3.0"})
    assert r.status_code == 200
    v2 = call(denv, "cfg_d", "GET", f"/admin/connection-tests/{t2['id']}").json()
    assert v2["error_code"] == "DWH_USR_PROPIO" and v2["result"]["agent_version"] == "5.3.0"


def test_limites_de_solicitudes_de_prueba(denv):
    b, gid = denv["b"], denv["g"]["id"]
    q("UPDATE connection_test SET status = 'expired' WHERE status IN ('pending', 'running')")
    age_tests()
    h, _ = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-limites")
    heartbeat(b.url, h)
    # 1) Idempotente: 60 solicitudes al mismo destino → una sola prueba abierta.
    codes, ids = [], set()
    for _ in range(60):
        r = call(denv, "cfg_d", "POST", "/admin/connection-tests", {"target_kind": "group_dwh", "group_id": gid})
        codes.append(r.status_code)
        ids.add(r.json().get("id"))
    assert codes[0] == 201 and set(codes[1:]) == {200} and len(ids) == 1
    assert q("SELECT COUNT(*) FROM connection_test WHERE status = 'pending' AND group_id = %s", (gid,))[0][0] == 1
    # 2) Intervalo mínimo por destino: recién terminada → 429 con Retry-After.
    tid = ids.pop()
    q("UPDATE connection_test SET status = 'failed', finished_at = NOW() WHERE id = %s", (tid,))
    r = call(denv, "cfg_d", "POST", "/admin/connection-tests", {"target_kind": "group_dwh", "group_id": gid})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "too_soon" and int(r.headers["Retry-After"]) > 0
    # 3) Máximo de pruebas abiertas por grupo (5).
    extra = []
    for i in range(3):
        extra.append(b.admin("POST", "/admin/companies", {"group_id": gid, "name": f"Lim {i}",
                                                          "source_type": "postgresql", "source_host": "s.invalid"},
                             expect=201))
    try:
        age_tests()
        targets = [{"target_kind": "group_dwh", "group_id": gid}] + \
            [{"target_kind": "company_source", "company_id": c["id"]} for c in extra] + \
            [{"target_kind": "company_source", "company_id": denv["c_inh"]["id"]},
             {"target_kind": "company_source", "company_id": denv["c_own"]["id"]}]
        got = [call(denv, "cfg_d", "POST", "/admin/connection-tests", t_).status_code for t_ in targets]
        assert got[:5] == [201] * 5 and got[5] == 429, got
        # 4) Tope por usuario por minuto (10).
        q("UPDATE connection_test SET status = 'expired' WHERE status IN ('pending', 'running')")
        age_tests()
        uid = q("SELECT id FROM panel_user WHERE username = 'cfg_d'")[0][0]
        for _ in range(10):
            q("""INSERT INTO connection_test (id, target_kind, group_id, status, requested_by, requested_by_user_id,
                                              expires_at) VALUES (%s, 'group_dwh', %s, 'no_agent', 'cfg_d', %s, NOW())""",
              (str(uuid.uuid4()), gid, uid))
        r = call(denv, "cfg_d", "POST", "/admin/connection-tests",
                 {"target_kind": "company_dwh", "company_id": denv["c_inh"]["id"]})
        assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"
    finally:
        q("UPDATE connection_test SET status = 'expired' WHERE status IN ('pending', 'running')")
        age_tests()
        for c in extra:
            b.admin("DELETE", f"/admin/companies/{c['id']}", expect=200)


def test_resultado_fuera_de_plazo_410(denv):
    b, gid = denv["b"], denv["g"]["id"]
    q("UPDATE connection_test SET status = 'expired' WHERE status IN ('pending', 'running')")
    age_tests()
    h, _ = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-tarde")
    heartbeat(b.url, h)
    t = call(denv, "cfg_d", "POST", "/admin/connection-tests", {"target_kind": "group_dwh", "group_id": gid},
             expect=201).json()
    assert any(x["id"] == t["id"] for x in _claim_all(b, h))
    q("UPDATE connection_test SET expires_at = NOW() - interval '1 minute' WHERE id = %s", (t["id"],))
    r = requests.post(b.url + f"/agent/connection-tests/{t['id']}/result", headers=h, json={"status": "ok"},
                      timeout=T)
    assert r.status_code == 410
    assert q("SELECT status, error_code FROM connection_test WHERE id = %s", (t["id"],))[0] == ("expired", "NO_RESULT")


def test_agente_anterior_mismo_host_otro_usuario_y_ddl_sin_esquema(denv):
    b, gid = denv["b"], denv["g"]["id"]
    same = b.admin("POST", "/admin/companies", {
        "group_id": gid, "name": "Mismo Host", "warehouse_mode": "custom", "warehouse_host": "dwh-grupo.invalid",
        "warehouse_database": "dwh_grupo", "warehouse_username": "otro_usuario", "warehouse_password": pw()},
        expect=201)
    ag = b.admin("POST", "/admin/agencies", {"company_id": same["id"], "name": "Ag mismo"}, expect=201)
    o = b.admin("POST", "/admin/objects", {"company_id": same["id"], "name": "X", "destination_table": "x",
                                           "upsert_keys": "id"}, expect=201)
    t_same = b.admin("POST", "/admin/tasks", {"agency_id": ag["id"], "object_catalog_id": o["id"],
                                              "extract_sql": "SELECT 1 AS id"}, expect=201)["id"]
    ag_inh = b.admin("GET", f"/admin/agencies?company_id={denv['c_inh']['id']}", expect=200)["items"][0]
    o_ddl = b.admin("POST", "/admin/objects", {"company_id": denv["c_inh"]["id"], "name": "ConDDL",
                                               "destination_table": "con_ddl", "upsert_keys": "id",
                                               "create_table_sql": "CREATE TABLE IF NOT EXISTS con_ddl (id INT)"},
                    expect=201)
    o_ok = b.admin("POST", "/admin/objects", {"company_id": denv["c_inh"]["id"], "name": "ConDDLCalif",
                                              "destination_table": "con_ddl2", "upsert_keys": "id",
                                              "create_table_sql": "CREATE TABLE IF NOT EXISTS dwh_g.con_ddl2 (id INT)"},
                   expect=201)
    t_ddl = b.admin("POST", "/admin/tasks", {"agency_id": ag_inh["id"], "object_catalog_id": o_ddl["id"],
                                             "extract_sql": "SELECT 1 AS id"}, expect=201)["id"]
    t_ok = b.admin("POST", "/admin/tasks", {"agency_id": ag_inh["id"], "object_catalog_id": o_ok["id"],
                                            "extract_sql": "SELECT 1 AS id"}, expect=201)["id"]
    try:
        h, iid = enroll(b.url, "x-group-token", denv["g"]["group_token"], "inst-vieja-2")
        d = tasks(b.url, h, features=False)
        reasons = {w["task_id"]: w["reason"] for w in d["withheld_tasks"]}
        assert reasons[t_same] == "destination_per_company"
        assert reasons[t_ddl] == "schema_ddl"
        assert t_ok not in reasons and t_ok in {t["task_id"] for t in d["tasks"]}
        # El 5.3 las recibe todas, con el usuario correcto.
        d2 = tasks(b.url, h)
        by = {t["task_id"]: t for t in d2["tasks"]}
        assert by[t_same]["warehouse"]["username"] == "otro_usuario" and d2["withheld_tasks"] == []
        # Visible en Instalaciones: el último GET (5.3) ya no retiene nada; se vuelve a pedir como agente viejo.
        tasks(b.url, h, features=False)
        inst = b.admin("GET", f"/admin/installations/{iid}", expect=200)
        assert {w["reason"] for w in inst["withheld_tasks"]} >= {"destination_per_company", "schema_ddl"}
        assert inst["withheld_at"]
    finally:
        for tid in (t_same, t_ddl, t_ok):
            b.admin("DELETE", f"/admin/tasks/{tid}", expect=200)
        for oid in (o["id"], o_ddl["id"], o_ok["id"]):
            b.admin("DELETE", f"/admin/objects/{oid}", expect=200)
        b.admin("DELETE", f"/admin/agencies/{ag['id']}", expect=200)
        b.admin("DELETE", f"/admin/companies/{same['id']}", expect=200)


def test_cambio_de_destino_reinicia_la_carga(denv):
    b, gid = denv["b"], denv["g"]["id"]
    inh_tasks = denv["tasks"][denv["c_inh"]["id"]]
    own_tasks = denv["tasks"][denv["c_own"]["id"]]
    all_t = inh_tasks + own_tasks

    def set_wm():
        for tid in all_t:
            q("""INSERT INTO task_sync_state (task_id, watermark, watermark_kind) VALUES (%s, '2026-01-01', 'source_clock')
                 ON CONFLICT (task_id) DO UPDATE SET watermark = '2026-01-01', watermark_kind = 'source_clock',
                 watermark_reset_at = NULL""", (tid,))

    def wm(tid):
        return q("SELECT watermark FROM task_sync_state WHERE task_id = %s", (tid,))[0][0]

    set_wm()
    # Solo la contraseña: no cambia la tabla física → no se reinicia nada.
    r = b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_password": denv["g_pass"]}, expect=200)
    assert r["sync_reset_tasks"] == 0 and all(wm(t) for t in all_t)
    # Esquema del grupo: se reinician SOLO las empresas que lo heredan.
    r = b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_schema": "dwh_g2"}, expect=200)
    try:
        assert r["sync_reset_tasks"] == len(inh_tasks)
        assert r["destination_changed_companies"] == [denv["c_inh"]["id"]]
        assert all(wm(t) is None for t in inh_tasks) and all(wm(t) for t in own_tasks)
        aud = q("""SELECT details::text FROM panel_audit_log WHERE action = 'destination.change'
                   ORDER BY id DESC LIMIT 1""")[0][0]
        assert '"tasks_reset": 2' in aud and "warehouse_schema" in aud
    finally:
        b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_schema": "dwh_g", "reset_sync": False}, expect=200)
    # Con reset_sync = false se registra el cambio pero no se reinicia.
    set_wm()
    r = b.admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_database": "dwh_propio_b",
                                                                   "reset_sync": False}, expect=200)
    assert r["sync_reset_tasks"] == 0 and r["destination_changed_companies"] == [denv["c_own"]["id"]]
    assert all(wm(t) for t in own_tasks)
    r = b.admin("PUT", f"/admin/companies/{denv['c_own']['id']}", {"warehouse_database": "dwh_propio"}, expect=200)
    assert r["sync_reset_tasks"] == len(own_tasks) and all(wm(t) is None for t in own_tasks)
    assert all(wm(t) for t in inh_tasks)
    # Conteo para el aviso del panel.
    g = b.admin("GET", f"/admin/groups/{gid}", expect=200)
    assert g["inherited_task_count"] >= len(inh_tasks)
    assert b.admin("GET", f"/admin/companies/{denv['c_own']['id']}")["task_count"] == len(own_tasks)


def test_verify_ca_exige_ca_y_config_sin_credenciales_puede_guardar(denv):
    b, gid = denv["b"], denv["g"]["id"]
    b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_sslmode": "verify-ca"}, expect=422)
    b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_sslmode": "verify-ca", "warehouse_sslrootcert": CA_PEM},
            expect=200)
    b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_sslrootcert": ""}, expect=422)   # sigue en verify-ca
    b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_sslmode": "verify-full", "warehouse_sslrootcert": ""},
            expect=200)
    b.admin("PUT", f"/admin/groups/{gid}", {"warehouse_sslmode": "prefer"}, expect=200)
    # Usuario solo con config.manage que manda el formulario completo con "inherit" (la empresa ya hereda).
    call(denv, "cfg_d", "PUT", f"/admin/companies/{denv['c_inh']['id']}",
         {"name": "Empresa Hereda", "warehouse_mode": "inherit"}, expect=200)
