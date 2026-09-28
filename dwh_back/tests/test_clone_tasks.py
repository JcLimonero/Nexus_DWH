"""
Clonar extractores (tareas) a otras agencias: POST /admin/tasks/{id}/clone y
POST /admin/agencies/{id}/clone-tasks (sección 16 de DWH_README.md).

BD propia (nexus_test_cfg_clone) y backend local en subproceso. Contraseñas desechables
generadas aquí; nunca se imprimen.
"""

import secrets
import uuid

import pytest
import requests

import support

DB = "nexus_test_cfg_clone"
T = 15


def pw() -> str:
    return "Cl-" + secrets.token_urlsafe(14)


def q(sql, params=()):
    c = support.cfg_conn(DB)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


@pytest.fixture(scope="module")
def cenv():
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.recreate_config_db(DB)
    b = support.Backend(DB)
    b.start()
    try:
        ids = support.seed_config(b)
        A, B = ids["group_a"]["id"], ids["group_b"]["id"]
        # Segunda empresa del grupo A (otro servidor de origen) con una agencia.
        ca2 = b.admin("POST", "/admin/companies", {"group_id": A, "name": "Empresa A2", "source_type": "sqlserver",
                                                   "source_host": "src-a2.invalid"}, expect=201)
        sessions = {}
        for name, roles in {
            "cfg_a": [{"role": "admin_config", "group_id": A}],
            "cfg_ab": [{"role": "admin_config", "group_id": A}, {"role": "admin_config", "group_id": B}],
            "cfg_a_lee_b": [{"role": "admin_config", "group_id": A}, {"role": "lectura", "group_id": B}],
            "lector_a": [{"role": "lectura", "group_id": A}],
        }.items():
            p = pw()
            b.admin("POST", "/admin/users", {"username": name, "email": f"{name}@ejemplo.test", "password": p, "must_change_password": False,
                                             "roles": roles}, expect=201)
            r = requests.post(b.url + "/admin/auth/login", json={"email": f"{name}@ejemplo.test", "password": p}, timeout=T)
            assert r.status_code == 200, r.text
            sessions[name] = {"authorization": "Bearer " + r.json()["token"]}
        yield {"b": b, "ids": ids, "A": A, "B": B, "ca2": ca2, "s": sessions}
    finally:
        b.stop()
        support.drop_config_db(DB)


def call(e, who, method, path, body=None, expect=None):
    r = requests.request(method, e["b"].url + path, json=body, headers=e["s"][who], timeout=T)
    if expect is not None:
        assert r.status_code == expect, f"{who} {method} {path} -> {r.status_code}: {r.text[:400]}"
    return r


def new_agency(e, company_id, prefix="Ag"):
    return e["b"].admin("POST", "/admin/agencies", {"company_id": company_id,
                                                    "name": f"{prefix}-{uuid.uuid4().hex[:6]}"}, expect=201)


def clone(e, who, task_id, targets, expect=200, **opts):
    return call(e, who, "POST", f"/admin/tasks/{task_id}/clone", {"target_agency_ids": targets, **opts},
                expect=expect).json()


def task_row(task_id):
    r = q("""SELECT agency_id, object_catalog_id, extract_sql, schedule_seconds, is_active, run_on_company_token,
                    query_version, query_hash, last_run_at FROM agency_task WHERE id = %s""", (task_id,))
    return r[0] if r else None


def test_misma_empresa_reutiliza_objeto_y_queda_deshabilitada(cenv):
    e = cenv
    ids = e["ids"]
    t_cli, ca = ids["t_cli"], ids["company_a"]
    target = new_agency(e, ca["id"])
    out = clone(e, "cfg_a", t_cli["id"], [target["id"]])
    assert out["summary"]["created"] == 1 and out["summary"]["objects_created"] == 0
    r = out["results"][0]
    assert r["status"] == "created" and r["object_action"] == "reused"
    assert r["object_id"] == t_cli["object_catalog_id"] and r["agency_name"] == target["name"]
    row = task_row(r["task_id"])
    src = task_row(t_cli["id"])
    assert row[0] == target["id"] and row[1] == t_cli["object_catalog_id"]
    assert row[2] == src[2] and row[3] == src[3] and row[5] == src[5]
    assert row[4] is False                                   # deshabilitada por defecto (revisión)
    assert row[6] == 1 and row[7] == src[7] and row[8] is None   # query_version/hash por trigger; sin last_run
    # enabled=True la crea activa
    t2 = new_agency(e, ca["id"])
    r2 = clone(e, "cfg_a", t_cli["id"], [t2["id"]], enabled=True)["results"][0]
    assert r2["status"] == "created" and task_row(r2["task_id"])[4] is True


def test_otra_empresa_copia_objeto_y_luego_omite_o_actualiza(cenv):
    e = cenv
    ids = e["ids"]
    t_cli = ids["t_cli"]
    target = new_agency(e, e["ca2"]["id"])
    r = clone(e, "cfg_a", t_cli["id"], [target["id"]])["results"][0]
    assert r["status"] == "created" and r["object_action"] == "created"
    assert "static_columns_review" in r["warnings"]          # static_columns copiadas: revisar (p. ej. "dn")
    src_obj = q("""SELECT name, destination_table, create_table_sql, upsert_keys, static_columns
                   FROM object_catalog WHERE id = %s""", (t_cli["object_catalog_id"],))[0]
    new_obj = q("""SELECT name, destination_table, create_table_sql, upsert_keys, static_columns, company_id
                   FROM object_catalog WHERE id = %s""", (r["object_id"],))[0]
    assert new_obj[:5] == src_obj and new_obj[5] == e["ca2"]["id"]
    assert task_row(r["task_id"])[1] == r["object_id"]
    # Segunda vez: objeto idéntico → se reutiliza; la tarea ya existe → se omite (por defecto)
    again = clone(e, "cfg_a", t_cli["id"], [target["id"]])["results"][0]
    assert again["status"] == "skipped_exists" and again["object_action"] == "reused"
    assert again["task_id"] == r["task_id"]
    # Otra agencia de la MISMA empresa destino reutiliza el objeto ya copiado
    other = new_agency(e, e["ca2"]["id"])
    r3 = clone(e, "cfg_a", t_cli["id"], [other["id"]])["results"][0]
    assert r3["status"] == "created" and r3["object_action"] == "reused" and r3["object_id"] == r["object_id"]
    # on_conflict=update: cambia el SQL de origen y se propaga; is_active se conserva; query_version sube
    call(e, "cfg_a", "PUT", f"/admin/tasks/{t_cli['id']}", {"schedule_seconds": 900}, expect=200)
    new_sql = task_row(t_cli["id"])[2] + " -- v2"
    call(e, "cfg_a", "PUT", f"/admin/tasks/{t_cli['id']}", {"extract_sql": new_sql}, expect=200)
    up = clone(e, "cfg_a", t_cli["id"], [target["id"]], on_conflict="update")["results"][0]
    assert up["status"] == "updated" and up["task_id"] == r["task_id"]
    row = task_row(r["task_id"])
    assert row[2] == new_sql and row[3] == 900 and row[4] is False and row[6] == 2


def test_objeto_con_otra_definicion_es_conflicto(cenv):
    e = cenv
    ids = e["ids"]
    t_lenta = ids["t_lenta"]
    comp = e["b"].admin("POST", "/admin/companies", {"group_id": e["A"], "name": "Empresa A3"}, expect=201)
    target = new_agency(e, comp["id"])
    # Mismo nombre ("Lenta"), otra tabla destino
    obj = e["b"].admin("POST", "/admin/objects", {"company_id": comp["id"], "name": "Lenta",
                                                  "destination_table": "otra_tabla", "upsert_keys": "id"}, expect=201)
    r = clone(e, "cfg_a", t_lenta["id"], [target["id"]])["results"][0]
    assert r["status"] == "object_conflict" and r["object_action"] == "conflict" and r["task_id"] is None
    assert "destination_table" in r["warnings"]
    # update SIN confirmación explícita tampoco sobrescribe
    r = clone(e, "cfg_a", t_lenta["id"], [target["id"]], on_conflict="update")["results"][0]
    assert r["status"] == "object_conflict"
    assert q("SELECT destination_table FROM object_catalog WHERE id = %s", (obj["id"],))[0][0] == "otra_tabla"
    assert q("SELECT COUNT(*) FROM agency_task WHERE agency_id = %s", (target["id"],))[0][0] == 0
    # Con overwrite_objects: se actualiza la definición y se crea la tarea
    r = clone(e, "cfg_a", t_lenta["id"], [target["id"]], on_conflict="update", overwrite_objects=True)["results"][0]
    assert r["status"] == "created" and r["object_action"] == "updated" and r["object_id"] == obj["id"]
    assert q("SELECT destination_table FROM object_catalog WHERE id = %s", (obj["id"],))[0][0] == "lenta"
    # Sin copiar el objeto faltante → error por destino
    comp4 = e["b"].admin("POST", "/admin/companies", {"group_id": e["A"], "name": "Empresa A4"}, expect=201)
    t4 = new_agency(e, comp4["id"])
    r = clone(e, "cfg_a", t_lenta["id"], [t4["id"]], copy_object_if_missing=False)["results"][0]
    assert r["status"] == "error" and r["code"] == "object_missing"
    assert q("SELECT COUNT(*) FROM object_catalog WHERE company_id = %s", (comp4["id"],))[0][0] == 0


def test_permisos_por_destino_sin_revelar_otros_grupos(cenv):
    e = cenv
    ids = e["ids"]
    t_cli, ab1, ca = ids["t_cli"], ids["agency_b1"], ids["company_a"]
    ok_target = new_agency(e, ca["id"])
    n_b = q("SELECT COUNT(*) FROM agency_task WHERE agency_id = %s", (ab1["id"],))[0][0]
    # Sin alcance sobre B: "no encontrada", sin nombre ni grupo; los demás destinos siguen
    resp = call(e, "cfg_a", "POST", f"/admin/tasks/{t_cli['id']}/clone",
                {"target_agency_ids": [ab1["id"], 999999, ok_target["id"]]}, expect=200)
    by = {r["agency_id"]: r for r in resp.json()["results"]}
    assert by[ab1["id"]]["code"] == "not_found" and by[ab1["id"]]["agency_name"] is None
    assert by[ab1["id"]]["group_name"] is None and by[999999]["code"] == "not_found"
    assert by[ok_target["id"]]["status"] == "created"
    assert "Agencia B1" not in resp.text and "Grupo B" not in resp.text and "Empresa B1" not in resp.text
    assert resp.json()["target_group_ids"] == [e["A"]]
    # Ve B pero sin config.manage allí → permiso por destino
    r = clone(e, "cfg_a_lee_b", t_cli["id"], [ab1["id"]])["results"][0]
    assert r["status"] == "error" and r["code"] == "permission_required"
    assert q("SELECT COUNT(*) FROM agency_task WHERE agency_id = %s", (ab1["id"],))[0][0] == n_b
    # config.manage en ambos grupos → cruza de grupo (objeto copiado a la empresa de B)
    r = clone(e, "cfg_ab", t_cli["id"], [ab1["id"]])["results"][0]
    assert r["status"] == "created" and r["object_action"] == "created"
    assert q("SELECT company_id FROM object_catalog WHERE id = %s", (r["object_id"],))[0][0] == ids["company_b"]["id"]
    # Origen fuera del alcance → 404; sin config.manage → 403
    assert call(e, "cfg_a", "POST", f"/admin/tasks/{ids['t_b']['id']}/clone",
                {"target_agency_ids": [ok_target["id"]]}).status_code == 404
    assert call(e, "lector_a", "POST", f"/admin/tasks/{t_cli['id']}/clone",
                {"target_agency_ids": [ok_target["id"]]}).status_code == 403
    assert call(e, "cfg_a", "POST", f"/admin/agencies/{ab1['id']}/clone-tasks",
                {"target_agency_ids": [ok_target["id"]]}).status_code == 404


def test_vista_previa_no_guarda_nada_y_auditoria(cenv):
    e = cenv
    ids = e["ids"]
    t_cli = ids["t_cli"]
    target = new_agency(e, ids["company_a"]["id"])
    far = new_agency(e, e["b"].admin("POST", "/admin/companies", {"group_id": e["A"], "name": "Empresa A5"},
                                     expect=201)["id"])
    n_tasks = q("SELECT COUNT(*) FROM agency_task")[0][0]
    n_objs = q("SELECT COUNT(*) FROM object_catalog")[0][0]
    n_audit = q("SELECT COUNT(*) FROM panel_audit_log WHERE action = 'tasks.clone'")[0][0]
    out = clone(e, "cfg_a", t_cli["id"], [target["id"], far["id"]], dry_run=True)
    assert out["dry_run"] is True and out["summary"]["created"] == 2 and out["summary"]["objects_created"] == 1
    assert all(r["task_id"] is None for r in out["results"])
    assert q("SELECT COUNT(*) FROM agency_task")[0][0] == n_tasks
    assert q("SELECT COUNT(*) FROM object_catalog")[0][0] == n_objs
    assert q("SELECT COUNT(*) FROM panel_audit_log WHERE action = 'tasks.clone'")[0][0] == n_audit
    # Ejecución real → una entrada de auditoría con origen, destinos y resumen
    clone(e, "cfg_a", t_cli["id"], [target["id"], far["id"]])
    rows = q("""SELECT actor_name, target_type, target_id, group_id, details FROM panel_audit_log
                WHERE action = 'tasks.clone' ORDER BY id DESC LIMIT 1""")
    actor, ttype, tid, gid, details = rows[0]
    assert actor == "cfg_a" and ttype == "task" and tid == str(t_cli["id"]) and gid == e["A"]
    assert details["source_task_ids"] == [t_cli["id"]]
    assert details["target_agency_ids"] == [target["id"], far["id"]]
    assert details["summary"]["created"] == 2 and details["options"]["enabled"] is False
    assert "extract_sql" not in str(details)


def test_clonar_extractores_de_una_agencia(cenv):
    e = cenv
    ids = e["ids"]
    aa2 = ids["agency_a2"]                          # tareas: t_bad (Mala), t_sin (SinLlave)
    target = new_agency(e, e["ca2"]["id"])
    # Subconjunto
    out = call(e, "cfg_a", "POST", f"/admin/agencies/{aa2['id']}/clone-tasks",
               {"target_agency_ids": [target["id"]], "task_ids": [ids["t_sin"]["id"]]}, expect=200).json()
    assert out["source_task_ids"] == [ids["t_sin"]["id"]] and len(out["results"]) == 1
    assert out["results"][0]["status"] == "created" and out["results"][0]["object_name"] == "SinLlave"
    # Todas (la ya clonada se omite)
    out = call(e, "cfg_a", "POST", f"/admin/agencies/{aa2['id']}/clone-tasks",
               {"target_agency_ids": [target["id"]]}, expect=200).json()
    st = {r["object_name"]: r["status"] for r in out["results"]}
    assert st == {"Mala": "created", "SinLlave": "skipped_exists"}
    assert out["summary"]["created"] == 1 and out["summary"]["skipped_exists"] == 1
    # Tarea de otra agencia en task_ids → 422; la propia agencia como destino → error por destino
    assert call(e, "cfg_a", "POST", f"/admin/agencies/{aa2['id']}/clone-tasks",
                {"target_agency_ids": [target["id"]], "task_ids": [ids["t_cli"]["id"]]}).status_code == 422
    out = call(e, "cfg_a", "POST", f"/admin/agencies/{aa2['id']}/clone-tasks",
               {"target_agency_ids": [aa2["id"]]}, expect=200).json()
    assert {r["code"] for r in out["results"]} == {"same_agency"}
    assert q("SELECT COUNT(*) FROM panel_audit_log WHERE action = 'agencies.clone_tasks'")[0][0] >= 2


# ─────────────────────────────────────────────────────────────────────────────
# Regresiones (validación)
# ─────────────────────────────────────────────────────────────────────────────
def test_auditoria_no_se_pierde_con_muchos_destinos(cenv):
    """499 ids inexistentes + 1 válido: antes el JSON truncado a 4000 era inválido y se perdía el registro."""
    e = cenv
    t_cli = e["ids"]["t_cli"]
    ok = new_agency(e, e["ids"]["company_a"]["id"])
    n0 = q("SELECT COUNT(*) FROM panel_audit_log WHERE action = 'tasks.clone'")[0][0]
    out = clone(e, "cfg_a", t_cli["id"], [ok["id"]] + list(range(900001, 900500)))
    assert out["summary"]["created"] == 1 and out["summary"]["error"] == 499
    assert q("SELECT COUNT(*) FROM panel_audit_log WHERE action = 'tasks.clone'")[0][0] == n0 + 1
    d = q("SELECT details FROM panel_audit_log WHERE action = 'tasks.clone' ORDER BY id DESC LIMIT 1")[0][0]
    assert d["target_agency_total"] == 500 and len(d["target_agency_ids"]) == 100
    assert d["summary"]["created"] == 1 and d["task_ids_written_total"] == 1


def test_detalles_de_auditoria_siempre_json_valido():
    import json
    import panel_auth
    s = panel_auth.audit_details_json({"ids": list(range(20000)), "texto": "x" * 50000, "n": 1})
    d = json.loads(s)
    assert len(s) <= panel_auth.AUDIT_DETAILS_MAX and d["truncated"] is True and d["ids_total"] == 20000
    s = panel_auth.audit_details_json({f"k{i}": "y" * 400 for i in range(200)})
    assert json.loads(s)["truncated"] is True and len(s) <= panel_auth.AUDIT_DETAILS_MAX


def test_tope_de_combinaciones_422(cenv):
    e = cenv
    r = call(e, "cfg_a", "POST", f"/admin/tasks/{e['ids']['t_cli']['id']}/clone",
             {"target_agency_ids": list(range(1, 2002))})
    assert r.status_code == 422, r.text[:300]


def test_vista_previa_no_consume_secuencias(cenv):
    e = cenv
    comp = e["b"].admin("POST", "/admin/companies", {"group_id": e["A"], "name": "Empresa A6"}, expect=201)
    t1, t2 = new_agency(e, comp["id"]), new_agency(e, comp["id"])
    seqs = "SELECT (SELECT last_value FROM agency_task_id_seq), (SELECT last_value FROM object_catalog_id_seq)"
    before = q(seqs)[0]
    out = clone(e, "cfg_a", e["ids"]["t_cli"]["id"], [t1["id"], t2["id"]], dry_run=True)
    assert q(seqs)[0] == before
    # El objeto se "copia" una sola vez para la empresa; el segundo destino lo reutiliza (igual que al ejecutar)
    acts = [r["object_action"] for r in out["results"]]
    assert acts == ["created", "reused"] and out["summary"]["objects_created"] == 1
    real = clone(e, "cfg_a", e["ids"]["t_cli"]["id"], [t1["id"], t2["id"]])
    assert [r["object_action"] for r in real["results"]] == acts
    assert [r["status"] for r in real["results"]] == [r["status"] for r in out["results"]]
    msg = clone(e, "cfg_a", e["ids"]["t_cli"]["id"], [t1["id"]], dry_run=True)["results"][0]["message"]
    assert "se omitirá" in msg


def test_conflicto_informa_extractores_afectados(cenv):
    e = cenv
    comp = e["b"].admin("POST", "/admin/companies", {"group_id": e["A"], "name": "Empresa A7"}, expect=201)
    other, target = new_agency(e, comp["id"]), new_agency(e, comp["id"])
    obj = e["b"].admin("POST", "/admin/objects", {"company_id": comp["id"], "name": "Clientes",
                                                  "destination_table": "otra", "upsert_keys": "id"}, expect=201)
    e["b"].admin("POST", "/admin/tasks", {"agency_id": other["id"], "object_catalog_id": obj["id"],
                                          "extract_sql": "SELECT 1"}, expect=201)
    r = clone(e, "cfg_a", e["ids"]["t_cli"]["id"], [target["id"]])["results"][0]
    assert r["status"] == "object_conflict" and r["affected_tasks"] == 1
    r = clone(e, "cfg_a", e["ids"]["t_cli"]["id"], [target["id"]], on_conflict="update", overwrite_objects=True,
              dry_run=True)["results"][0]
    assert r["object_action"] == "updated" and r["affected_tasks"] == 1
    assert q("SELECT destination_table FROM object_catalog WHERE id = %s", (obj["id"],))[0][0] == "otra"


def test_aviso_si_el_destino_esta_deshabilitado(cenv):
    e = cenv
    target = new_agency(e, e["ids"]["company_a"]["id"])
    e["b"].admin("POST", f"/admin/agencies/{target['id']}/disable", expect=200)
    r = clone(e, "cfg_a", e["ids"]["t_lenta"]["id"], [target["id"]])["results"][0]
    assert r["status"] == "created" and r["target_disabled"] is True and "target_disabled" in r["warnings"]


def test_lista_ligera_de_tareas_sin_sql(cenv):
    e = cenv
    items = call(e, "lector_a", "GET", "/admin/tasks?light=true", expect=200).json()["items"]
    assert items and set(items[0]) == {"id", "agency_id", "group_id", "object_catalog_id", "object_name"}
    assert "extract_sql" not in str(items) and {i["group_id"] for i in items} == {e["A"]}
