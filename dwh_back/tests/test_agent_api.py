"""Pruebas de la API /agent, /admin/installations, compat legada y migraciones."""

import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
import requests

import support


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def enroll(b, header, token, name="test-inst", expect=201):
    r = requests.post(b.url + "/agent/enroll", headers={header: token},
                      json={"name": name, "hostname": "host-test", "client_version": "test"}, timeout=10)
    assert r.status_code == expect, r.text
    if expect != 201:
        return r.json()
    d = r.json()
    return d["installation_id"], d["secret"]


def H(iid, secret):
    return {"x-installation-id": iid, "x-installation-secret": secret}


def now_iso(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()


def start(b, h, exec_id, task_id, seq, started=None, attempt=1):
    return requests.post(b.url + "/agent/executions", headers=h, timeout=10, json={
        "execution_id": exec_id, "task_id": task_id, "attempt": attempt, "query_version": 1,
        "started_at": started or now_iso(), "agent_seq": seq, "client_version": "test"})


def finish(b, h, exec_id, task_id, seq, status="success", wm=None, **kw):
    body = {"task_id": task_id, "status": status, "finished_at": now_iso(), "duration_ms": 10,
            "rows_read": 5, "rows_loaded": 5, "agent_seq": seq, "client_version": "test", **kw}
    if wm:
        body["checkpoint"] = {"watermark": wm, "kind": "source_clock"}
    if status != "success":
        body.setdefault("failure_stage", "extract")
        body.setdefault("error_code", "SOURCE_CONNECTION_FAILED")
        body.setdefault("error_message", "fallo de prueba")
    return requests.put(b.url + f"/agent/executions/{exec_id}", headers=h, json=body, timeout=10)


def q1(db, sql, params=()):
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ─────────────────────────────────────────────────────────────────────────────
# Migraciones
# ─────────────────────────────────────────────────────────────────────────────
def test_migraciones_idempotentes(env):
    import migrate

    conn = psycopg2.connect(host=support.CFG_PG["host"], port=support.CFG_PG["port"], user="postgres",
                            password=support.CFG_PG["password"], dbname=env["db"])
    try:
        assert migrate.run_migrations(conn, log=lambda *_: None) == []
        assert migrate.pending_migrations(conn) == []
        versions = sorted(migrate.applied_versions(conn))
        assert versions[:5] == ["000", "001", "002", "003", "004"]
    finally:
        conn.close()


def test_migracion_sobre_bd_legada_recorta_tokens_y_rellena_ids():
    """BD antigua (solo esquema base + datos con tokens completos) → migraciones."""
    import migrate

    name = "nexus_test_cfg_legacy"
    support.drop_config_db(name)
    admin = support.cfg_conn("postgres")
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    conn = psycopg2.connect(host=support.CFG_PG["host"], port=support.CFG_PG["port"], user="postgres",
                            password=support.CFG_PG["password"], dbname=name)
    try:
        with conn.cursor() as cur:
            cur.execute(open(migrate.BASELINE_FILE, encoding="utf-8").read())
            cur.execute("INSERT INTO client_group (name, group_token) VALUES ('G', 'grupo-token-completo-123456789')")
            cur.execute("INSERT INTO company (group_id, name, company_token) VALUES (1, 'C', 'company-token-completo-abcdefgh')")
            cur.execute("INSERT INTO agency (company_id, name) VALUES (1, 'A')")
            cur.execute("INSERT INTO object_catalog (company_id, name, destination_table) VALUES (1, 'O', 't')")
            cur.execute("INSERT INTO agency_task (agency_id, object_catalog_id, extract_sql) VALUES (1, 1, 'SELECT 1')")
            cur.execute("INSERT INTO client_events (token, config_id, event_type) VALUES ('company-token-completo-abcdefgh', '1', 'ok')")
            cur.execute("INSERT INTO client_events (token, config_id, event_type) VALUES ('grupo-token-completo-123456789', '1', 'error')")
            cur.execute("INSERT INTO activity_log (token, method, endpoint, status_code) VALUES ('company-token-completo-abcdefgh', 'GET', '/configs', 200)")
        conn.commit()
        applied = migrate.run_migrations(conn, baseline=False, log=lambda *_: None)
        assert applied == ["001", "002", "003", "004"]
        with conn.cursor() as cur:
            cur.execute("SELECT token, company_id, group_id, task_id, auth_kind FROM client_events ORDER BY id")
            rows = cur.fetchall()
            assert rows[0] == ("company-", 1, 1, 1, "company")
            assert rows[1] == ("grupo-to", 1, 1, 1, "group")
            cur.execute("SELECT token, company_id, auth_kind FROM activity_log")
            assert cur.fetchone() == ("company-", 1, "company")
            cur.execute("SELECT query_version, length(query_hash) FROM agency_task")
            assert cur.fetchone() == (1, 64)
        # Segunda pasada: nada nuevo
        assert migrate.run_migrations(conn, log=lambda *_: None) == []
    finally:
        conn.close()
        support.drop_config_db(name)


# ─────────────────────────────────────────────────────────────────────────────
# Enrolamiento y alcance
# ─────────────────────────────────────────────────────────────────────────────
def test_enrolamiento_grupo_y_tareas_autorizadas(env, db):
    b, ids = env["backend"], env["ids"]
    iid, secret = enroll(b, "x-group-token", ids["group_a"]["group_token"])
    r = requests.get(b.url + "/agent/tasks", headers=H(iid, secret), timeout=10)
    assert r.status_code == 200
    data = r.json()
    task_ids = {t["task_id"] for t in data["tasks"]}
    assert task_ids == {ids["t_cli"]["id"]}                     # solo tareas activas del grupo A
    t = data["tasks"][0]
    assert t["query_version"] == 1 and len(t["query_hash"]) == 64
    assert t["source"]["password"] == support.SRC["password"]  # descifrado ENC: en el servidor
    assert data["warehouse"]["host"] == support.DWH["host"]
    assert data["config_max_age_seconds"] == 900
    # Secreto: en BD solo sha256
    row = q1(db, "SELECT credential_hash, scope_type FROM installation WHERE id = %s", (iid,))
    assert row[0] != secret and len(row[0]) == 64 and row[1] == "group"
    # Auditoría de descarga sin SQL
    n = q1(db, "SELECT COUNT(*) FROM task_download_log WHERE installation_id = %s", (iid,))[0]
    assert n == 1
    with db.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'task_download_log'")
        cols = {r[0] for r in cur.fetchall()}
    assert "extract_sql" not in cols and "query_version" in cols


def test_enrolamiento_token_invalido_y_alcance_deshabilitado(env):
    b, ids = env["backend"], env["ids"]
    d = enroll(b, "x-token", "no-existe", expect=401)
    assert d["detail"]["code"] == "invalid_enrollment_token"
    enroll(b, "x-token", "", expect=401)
    b.admin("POST", f"/admin/agencies/{ids['agency_b1']['id']}/disable", expect=200)
    try:
        d = enroll(b, "x-agency-token", ids["agency_b1"]["agency_token"], expect=403)
        assert d["detail"]["code"] == "scope_disabled"
    finally:
        b.admin("POST", f"/admin/agencies/{ids['agency_b1']['id']}/enable", expect=200)


def test_alcance_agencia_y_empresa(env):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-agency-token", ids["agency_a2"]["agency_token"])
    tasks = requests.get(b.url + "/agent/tasks", headers=H(iid, sec), timeout=10).json()["tasks"]
    assert tasks == []  # las tareas de A1-Norte están inactivas
    iid, sec = enroll(b, "x-agency-token", ids["agency_a1"]["agency_token"])
    tasks = requests.get(b.url + "/agent/tasks", headers=H(iid, sec), timeout=10).json()["tasks"]
    assert {t["task_id"] for t in tasks} == {ids["t_cli"]["id"]}
    # Empresa: respeta run_on_company_token
    iid, sec = enroll(b, "x-token", ids["company_a"]["company_token"])
    b.admin("PUT", f"/admin/tasks/{ids['t_cli']['id']}", {"run_on_company_token": False}, expect=200)
    try:
        tasks = requests.get(b.url + "/agent/tasks", headers=H(iid, sec), timeout=10).json()["tasks"]
        assert tasks == []
        r = start(env["backend"], H(iid, sec), str(uuid.uuid4()), ids["t_cli"]["id"], 1)
        assert r.status_code == 403
    finally:
        b.admin("PUT", f"/admin/tasks/{ids['t_cli']['id']}", {"run_on_company_token": True}, expect=200)


def test_variantes_de_autenticacion(env):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"])
    assert requests.get(b.url + "/agent/whoami", headers={"Authorization": f"Bearer {iid}.{sec}"}).status_code == 200
    r = requests.get(b.url + "/agent/whoami", headers=H(iid, sec + "x"))
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
    assert requests.get(b.url + "/agent/whoami").status_code == 401
    assert requests.get(b.url + "/agent/whoami", headers=H("no-uuid", sec)).status_code == 401
    assert requests.get(b.url + "/agent/whoami", headers=H(str(uuid.uuid4()), sec)).status_code == 401


def test_tarea_de_otro_grupo_403_y_aislamiento(env):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"])
    h = H(iid, sec)
    ex = str(uuid.uuid4())
    r = start(b, h, ex, ids["t_b"]["id"], 1)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "task_out_of_scope"
    r = finish(b, h, ex, ids["t_b"]["id"], 2)
    assert r.status_code == 403
    # grupo B no ve nada del grupo A
    iid_b, sec_b = enroll(b, "x-group-token", ids["group_b"]["group_token"])
    data = requests.get(b.url + "/agent/tasks", headers=H(iid_b, sec_b)).json()
    assert {t["task_id"] for t in data["tasks"]} == {ids["t_b"]["id"]}
    assert data["warehouse"]["host"] == "dwh-b.invalid"
    # evento genérico de otra instalación no afecta: execution_id ajeno → 409
    ex2 = str(uuid.uuid4())
    assert start(b, h, ex2, ids["t_cli"]["id"], 3).status_code == 200
    iid2, sec2 = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="otra")
    r = finish(b, H(iid2, sec2), ex2, ids["t_cli"]["id"], 10)
    assert r.status_code == 409


# ─────────────────────────────────────────────────────────────────────────────
# Ejecuciones: idempotencia, máquina de estados, orden
# ─────────────────────────────────────────────────────────────────────────────
def test_ejecucion_idempotente_y_estado_terminal(env, db):
    b, ids = env["backend"], env["ids"]
    tid = ids["t_cli"]["id"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"])
    h = H(iid, sec)
    ex = str(uuid.uuid4())
    assert start(b, h, ex, tid, 100).json()["duplicate"] is False
    assert start(b, h, ex, tid, 100).json()["duplicate"] is True
    wm = "2025-01-01T10:00:00.000000"
    r = finish(b, h, ex, tid, 101, wm=wm, rows_inserted=3, rows_updated=2)
    assert r.status_code == 200 and r.json()["applied"] is True
    # Reintento del mismo reporte (mismo seq) → ignorado, sin duplicar client_events
    assert finish(b, h, ex, tid, 101, wm=wm).json()["status"] == "ignored"
    # Un fallo posterior para la MISMA ejecución no pisa el estado terminal
    r = finish(b, h, ex, tid, 150, status="failed")
    assert r.json()["reason"] == "terminal_state"
    row = q1(db, "SELECT status, rows_inserted, rows_updated, checkpoint_confirmed FROM task_execution WHERE execution_id = %s", (ex,))
    assert row[0] == "success" and row[1] == 3 and row[2] == 2 and row[3].isoformat() == "2025-01-01T10:00:00"
    assert q1(db, "SELECT COUNT(*) FROM client_events WHERE execution_id = %s", (ex,))[0] == 1
    s = q1(db, "SELECT watermark, last_status, consecutive_failures FROM task_sync_state WHERE task_id = %s", (tid,))
    assert s[0].isoformat() == "2025-01-01T10:00:00" and s[1] == "success" and s[2] == 0
    assert q1(db, "SELECT last_run_at FROM agency_task WHERE id = %s", (tid,))[0].isoformat() == "2025-01-01T10:00:00"
    # /agent/tasks devuelve el watermark confirmado
    t = [t for t in requests.get(b.url + "/agent/tasks", headers=h).json()["tasks"] if t["task_id"] == tid][0]
    assert t["sync"]["watermark"].startswith("2025-01-01T10:00:00")


def test_eventos_fuera_de_orden_no_pisan_recuperacion(env, db):
    b, ids = env["backend"], env["ids"]
    tid = ids["t_cli"]["id"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="orden")
    h = H(iid, sec)
    a, bb = str(uuid.uuid4()), str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)
    assert start(b, h, a, tid, 1000, started=(t0 + timedelta(seconds=1)).isoformat()).status_code == 200
    assert start(b, h, bb, tid, 1002, started=(t0 + timedelta(seconds=5)).isoformat()).status_code == 200
    # Llega primero el éxito de B (más nuevo)...
    assert finish(b, h, bb, tid, 1003, wm="2025-02-02T00:00:00").status_code == 200
    # ...y después el fallo VIEJO de A (seq de inicio menor)
    assert finish(b, h, a, tid, 1004, status="failed").status_code == 200
    s = q1(db, "SELECT last_status, consecutive_failures, last_execution_id, watermark FROM task_sync_state WHERE task_id = %s", (tid,))
    assert s[0] == "success" and s[1] == 0 and str(s[2]) == bb
    assert s[3].isoformat() == "2025-02-02T00:00:00"
    assert q1(db, "SELECT status FROM task_execution WHERE execution_id = %s", (a,))[0] == "failed"
    # Un fallo NUEVO sí cuenta
    c = str(uuid.uuid4())
    start(b, h, c, tid, 1010)
    finish(b, h, c, tid, 1011, status="failed", error_code="DWH_CONNECTION_FAILED")
    s = q1(db, "SELECT last_status, consecutive_failures, current_error_code, watermark FROM task_sync_state WHERE task_id = %s", (tid,))
    assert s[0] == "failed" and s[1] == 1 and s[2] == "DWH_CONNECTION_FAILED"
    assert s[3].isoformat() == "2025-02-02T00:00:00"  # el watermark no retrocede
    # Update con seq viejo para una ejecución ya avanzada → ignorado
    assert finish(b, h, c, tid, 1005, status="success").json()["reason"] == "stale_or_duplicate"


def test_fin_sin_inicio_crea_la_ejecucion(env, db):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="sin-inicio")
    ex = str(uuid.uuid4())
    r = finish(b, H(iid, sec), ex, ids["t_cli"]["id"], 5, status="failed", started_at=now_iso(-5))
    assert r.status_code == 200
    assert q1(db, "SELECT status FROM task_execution WHERE execution_id = %s", (ex,))[0] == "failed"


def test_mensaje_de_error_saneado_en_servidor(env, db):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="saneo")
    ex = str(uuid.uuid4())
    msg = (f"IntegrityError: duplicate key\nDETAIL:  Key (email)=({support.ROW_SECRET}) already exists.\n"
           f"password={support.SRC['password']} host {support.DWH['password']}")
    finish(b, H(iid, sec), ex, ids["t_cli"]["id"], 7, status="failed", error_message=msg)
    stored = q1(db, "SELECT error_message_sanitized FROM task_execution WHERE execution_id = %s", (ex,))[0]
    assert support.ROW_SECRET not in stored and support.SRC["password"] not in stored
    assert support.DWH["password"] not in stored
    detail = q1(db, "SELECT detail FROM client_events WHERE execution_id = %s", (ex,))[0]
    assert support.ROW_SECRET not in detail and support.SRC["password"] not in detail


def test_reset_watermark_ignora_checkpoints_viejos(env, db):
    b, ids = env["backend"], env["ids"]
    tid = ids["t_cli"]["id"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="reset")
    h = H(iid, sec)
    ex = str(uuid.uuid4())
    start(b, h, ex, tid, 1, started=now_iso(-60))
    b.admin("POST", f"/admin/tasks/{tid}/reset-last-run", expect=200)
    finish(b, h, ex, tid, 2, wm="2025-06-01T00:00:00")
    s = q1(db, "SELECT watermark FROM task_sync_state WHERE task_id = %s", (tid,))
    assert s[0] is None  # ejecución iniciada antes del reinicio: no fija el watermark
    t = [t for t in requests.get(b.url + "/agent/tasks", headers=h).json()["tasks"] if t["task_id"] == tid][0]
    assert t["sync"]["watermark"] is None and t["sync"]["watermark_reset_at"]


def test_query_version_sube_solo_si_cambia_sql(env):
    b, ids = env["backend"], env["ids"]
    tid = ids["t_b"]["id"]
    before = b.admin("GET", f"/admin/tasks/{tid}", expect=200)
    b.admin("PUT", f"/admin/tasks/{tid}", {"schedule_seconds": 1800}, expect=200)
    same = b.admin("GET", f"/admin/tasks/{tid}", expect=200)
    assert same["query_version"] == before["query_version"]
    b.admin("PUT", f"/admin/tasks/{tid}", {"extract_sql": "SELECT 2 AS id"}, expect=200)
    after = b.admin("GET", f"/admin/tasks/{tid}", expect=200)
    assert after["query_version"] == before["query_version"] + 1 and after["query_hash"] != before["query_hash"]


# ─────────────────────────────────────────────────────────────────────────────
# Revocación, rotación, heartbeat, eventos
# ─────────────────────────────────────────────────────────────────────────────
def test_revocacion(env):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="revocar")
    assert requests.get(b.url + "/agent/tasks", headers=H(iid, sec)).status_code == 200
    out = b.admin("POST", f"/admin/installations/{iid}/revoke", {"reason": "prueba"}, expect=200)
    assert out["status"] == "revoked"
    r = requests.get(b.url + "/agent/tasks", headers=H(iid, sec))
    assert r.status_code == 401 and r.json()["detail"]["code"] == "installation_revoked"
    b.admin("POST", f"/admin/installations/{iid}/rotate", expect=404)


def test_rotacion_con_gracia(env, db):
    b, ids = env["backend"], env["ids"]
    iid, old = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="rotar")
    b.admin("POST", f"/admin/installations/{iid}/rotate", expect=200)
    hb = requests.post(b.url + "/agent/heartbeat", headers=H(iid, old), json={"client_version": "t"}).json()
    assert hb["credential_rotation_required"] is True
    r1 = requests.post(b.url + "/agent/credentials/rotate", headers=H(iid, old)).json()
    new = r1["secret"]
    assert new != old and r1["redelivered"] is False
    deadline1 = q1(db, "SELECT previous_valid_until FROM installation WHERE id = %s", (iid,))[0]
    # La respuesta "se perdió": con el secreto viejo se RE-ENTREGA el mismo nuevo (sin emitir otro)
    hb = requests.post(b.url + "/agent/heartbeat", headers=H(iid, old), json={}).json()
    assert hb["credential_rotation_required"] is True
    for _ in range(3):
        r2 = requests.post(b.url + "/agent/credentials/rotate", headers=H(iid, old)).json()
        assert r2["secret"] == new and r2["redelivered"] is True
    # La gracia NO se extiende
    assert q1(db, "SELECT previous_valid_until FROM installation WHERE id = %s", (iid,))[0] == deadline1
    assert requests.get(b.url + "/agent/whoami", headers=H(iid, new)).status_code == 200
    # Usar el nuevo invalida el anterior y el pendiente
    assert requests.get(b.url + "/agent/whoami", headers=H(iid, old)).status_code == 401
    assert q1(db, "SELECT pending_secret_enc IS NULL FROM installation WHERE id = %s", (iid,))[0] is True
    info = b.admin("GET", f"/admin/installations/{iid}", expect=200)
    assert info["rotation_required"] is False and info["credential_rotated_at"]


def test_rotacion_sin_cadena_de_secuestro(env, db):
    """Quien solo tiene el secreto viejo no puede emitir secretos nuevos ni alargar la gracia."""
    b, ids = env["backend"], env["ids"]
    iid, s1 = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="secuestro")
    s2 = requests.post(b.url + "/agent/credentials/rotate", headers=H(iid, s1)).json()["secret"]
    # el "atacante" con s1 solo obtiene s2 (el mismo), nunca un s3
    got = {requests.post(b.url + "/agent/credentials/rotate", headers=H(iid, s1)).json()["secret"] for _ in range(3)}
    assert got == {s2}
    assert requests.get(b.url + "/agent/whoami", headers=H(iid, s2)).status_code == 200   # el legítimo sigue
    # Vencida la gracia, s1 deja de valer
    db.cursor().execute("UPDATE installation SET previous_credential_hash = encode(sha256(convert_to(%s,'UTF8')),'hex'), "
                        "previous_valid_until = NOW() - interval '1 second' WHERE id = %s", (s1, iid))
    assert requests.get(b.url + "/agent/whoami", headers=H(iid, s1)).status_code == 401
    # Rotación normal con el vigente sí emite uno nuevo
    s3 = requests.post(b.url + "/agent/credentials/rotate", headers=H(iid, s2)).json()["secret"]
    assert s3 not in (s1, s2)


def test_heartbeat_y_monitor(env, db):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-agency-token", ids["agency_a1"]["agency_token"], name="latido")
    ex = str(uuid.uuid4())
    r = requests.post(b.url + "/agent/heartbeat", headers=H(iid, sec), json={
        "client_version": "5.0.0", "uptime_seconds": 12, "queue_depth": 3, "agent_seq": 9,
        "running": [{"task_id": ids["t_cli"]["id"], "execution_id": ex}]})
    assert r.status_code == 200
    row = q1(db, "SELECT client_version, last_seen_at IS NOT NULL, last_heartbeat->>'queue_depth' FROM installation WHERE id = %s", (iid,))
    assert row == ("5.0.0", True, "3")
    assert q1(db, "SELECT COUNT(*) FROM installation_heartbeat WHERE installation_id = %s", (iid,))[0] == 1
    mon = b.monitor("/monitor/installations")
    inst = [i for i in mon["installations"] if i["id"] == iid][0]
    assert inst["scope_type"] == "agency" and inst["agency_name"] == "Agencia A1-Centro" and inst["legacy"] is False
    assert "last_ip" not in inst


def test_eventos_de_agente_dedup(env):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="eventos")
    ev = {"event_id": str(uuid.uuid4()), "event_type": "queue_overflow", "agent_seq": 3,
          "payload": {"overflow_total": 5, "nota": f"password={support.SRC['password']}"}}
    r1 = requests.post(b.url + "/agent/events", headers=H(iid, sec), json=ev).json()
    r2 = requests.post(b.url + "/agent/events", headers=H(iid, sec), json=ev).json()
    assert r1["duplicate"] is False and r2["duplicate"] is True
    ev["event_type"] = "otro"
    ev["event_id"] = str(uuid.uuid4())
    assert requests.post(b.url + "/agent/events", headers=H(iid, sec), json=ev).status_code == 422


def test_admin_ejecuciones_filtros(env):
    b = env["backend"]
    data = b.admin("GET", "/admin/executions?status=failed&limit=50", expect=200)
    assert data["items"] and all(i["status"] == "failed" for i in data["items"])
    assert all("error_message_sanitized" in i for i in data["items"])
    assert b.admin("GET", "/admin/sync-state", expect=200)["items"]
    r = requests.get(b.url + "/admin/executions")
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Compatibilidad legada
# ─────────────────────────────────────────────────────────────────────────────
def test_client_event_legado_valida_alcance_y_sanea(env, db):
    b, ids = env["backend"], env["ids"]
    tok = ids["company_a"]["company_token"]
    r = requests.post(b.url + "/client-event", headers={"x-token": tok},
                      json={"config_id": str(ids["t_b"]["id"]), "event_type": "error", "detail": "x"})
    assert r.status_code == 403
    r = requests.post(b.url + "/client-event", headers={"x-token": tok},
                      json={"config_id": "abc", "event_type": "error"})
    assert r.status_code == 422
    big = ("Traceback...\nDETAIL:  Key (email)=(" + support.ROW_SECRET + ") already exists.\n"
           "conn: host=10.1.2.3 password=" + support.SRC["password"] + " " + tok + "\n" + "x" * 100_000)
    r = requests.post(b.url + "/client-event", headers={"x-token": tok},
                      json={"config_id": str(ids["t_cli"]["id"]), "event_type": "error", "detail": big, "rows_loaded": "nope"})
    assert r.status_code == 200
    row = q1(db, "SELECT detail, token, company_id, task_id, source FROM client_events WHERE source = 'legacy' ORDER BY id DESC LIMIT 1")
    detail = row[0]
    assert len(detail) <= 4000
    for secret in (support.ROW_SECRET, support.SRC["password"], tok, "10.1.2.3"):
        assert secret not in detail
    assert row[1] == tok[:8] and row[2] == ids["company_a"]["id"] and row[3] == ids["t_cli"]["id"] and row[4] == "legacy"


def test_prioridad_de_tokens_consistente(env):
    """Grupo > agencia > empresa en /client-event y /configs/{id}/last_run."""
    b, ids = env["backend"], env["ids"]
    hdrs = {"x-token": ids["company_b"]["company_token"], "x-group-token": ids["group_a"]["group_token"]}
    ok = requests.post(b.url + "/client-event", headers=hdrs,
                       json={"config_id": str(ids["t_cli"]["id"]), "event_type": "ok", "rows_loaded": 1})
    assert ok.status_code == 200  # manda el token de grupo A
    no = requests.post(b.url + "/client-event", headers=hdrs,
                       json={"config_id": str(ids["t_b"]["id"]), "event_type": "ok"})
    assert no.status_code == 403
    assert requests.put(b.url + f"/configs/{ids['t_b']['id']}/last_run", headers=hdrs).status_code == 404
    assert requests.put(b.url + f"/configs/{ids['t_cli']['id']}/last_run", headers=hdrs).status_code == 200
    # agencia > empresa
    hdrs2 = {"x-token": ids["company_b"]["company_token"], "x-agency-token": ids["agency_a1"]["agency_token"]}
    assert requests.post(b.url + "/client-event", headers=hdrs2,
                         json={"config_id": str(ids["t_cli"]["id"]), "event_type": "ok"}).status_code == 200


def test_endpoints_legados_siguen_funcionando(env):
    b, ids = env["backend"], env["ids"]
    r = requests.get(b.url + "/configs", headers={"x-token": ids["company_a"]["company_token"]})
    assert r.status_code == 200 and r.json()["configs"]
    r = requests.get(b.url + "/group-configs", headers={"x-group-token": ids["group_a"]["group_token"]})
    assert r.status_code == 200
    r = requests.get(b.url + "/agency-configs", headers={"x-agency-token": ids["agency_a1"]["agency_token"]})
    assert r.status_code == 200
    assert requests.get(b.url + "/configs", headers={"x-token": "malo"}).status_code == 401


def test_activity_log_sin_tokens_completos_y_legados_visibles(env, db):
    b, ids = env["backend"], env["ids"]
    tokens = [ids["company_a"]["company_token"], ids["group_a"]["group_token"], ids["agency_a1"]["agency_token"],
              ids["company_b"]["company_token"], b.admin_token, b.monitor_token]
    with db.cursor() as cur:
        cur.execute("SELECT MAX(length(token)) FROM activity_log")
        assert cur.fetchone()[0] <= 8
        cur.execute("SELECT MAX(length(token)) FROM client_events")
        assert cur.fetchone()[0] <= 8
        cur.execute("SELECT string_agg(COALESCE(error_detail,'') || token, ' ') FROM activity_log")
        blob = cur.fetchone()[0] or ""
    for t in tokens:
        assert t not in blob
    legacy = b.monitor("/monitor/installations")["legacy_clients"]
    kinds = {(c["auth_kind"], c["company_name"]) for c in legacy}
    assert ("company", "Empresa A1") in kinds and any(c["legacy"] for c in legacy)
    clients = b.monitor("/monitor/clients")["clients"]
    a1 = [c for c in clients if c["razon_social"] == "Empresa A1"][0]
    assert a1["executions_total"] >= 1 and a1["last_seen"]


def test_monitor_token(env):
    b = env["backend"]
    assert requests.get(b.url + "/monitor/clients", headers={"x-monitor-token": "malo"}).status_code == 401
    assert requests.get(b.url + "/monitor/installations", headers={"x-monitor-token": "malo"}).status_code == 401
    assert requests.get(b.url + "/monitor/clients", headers={"x-monitor-token": b.monitor_token}).status_code == 200


def test_endpoints_legados_deshabilitables(env):
    b2 = support.Backend(env["db"], extra_ini={
        "agent": {"legacy_endpoints": "false"},
        "security": {"config_secret_key": env["backend"].fernet_key},  # misma clave: puede descifrar
    })
    b2.start()
    try:
        ids = env["ids"]
        r = requests.get(b2.url + "/configs", headers={"x-token": ids["company_a"]["company_token"]})
        assert r.status_code == 410
        r = requests.post(b2.url + "/client-event", headers={"x-token": ids["company_a"]["company_token"]},
                          json={"config_id": "1", "event_type": "ok"})
        assert r.status_code == 410
        # /agent sigue operando
        iid, sec = enroll(b2, "x-group-token", ids["group_a"]["group_token"], name="sin-legado")
        assert requests.get(b2.url + "/agent/tasks", headers=H(iid, sec)).status_code == 200
    finally:
        b2.stop()


def test_backend_log_sin_secretos(env):
    b = env["backend"]
    log = open(b.log_path, encoding="utf-8", errors="replace").read()
    for s in (support.SRC["password"], support.DWH["password"], support.ROW_SECRET, b.admin_token):
        assert s not in log
