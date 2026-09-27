"""
Salud, incidencias y notificaciones (fase 2).

BD de configuración propia (nexus_test_cfg_health) y backend en subproceso con
umbrales cortos. El evaluador periódico está APAGADO (se dispara con
POST /admin/health/evaluate para que las pruebas sean deterministas); el
"reloj" se controla moviendo hacia atrás last_seen_at / received_at en la BD.
Las notificaciones van SOLO a un receptor HTTP local falso (127.0.0.1).
"""

import hashlib
import hmac
import itertools
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
import requests

import support

DBNAME = "nexus_test_cfg_health"
SEQ = itertools.count(1)


# ─────────────────────────────────────────────────────────────────────────────
# Receptor falso de webhooks (local)
# ─────────────────────────────────────────────────────────────────────────────
Receiver = support.FakeWebhookReceiver


SIGNING_SECRET = "firma-de-prueba-" + uuid.uuid4().hex


@pytest.fixture(scope="module")
def henv():
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.recreate_config_db(DBNAME)
    b = support.Backend(DBNAME, extra_ini={
        "health": {"evaluator_interval_seconds": "0", "disconnect_after_seconds": "300",
                   "heartbeat_expected_seconds": "5", "default_expected_duration_seconds": "30",
                   "delay_tolerance_factor": "0", "delay_min_grace_seconds": "30", "running_long_factor": "2",
                   "startup_grace_seconds": "0"},
        "notifications": {"worker_interval_seconds": "1", "max_attempts": "3", "backoff_base_seconds": "1",
                          "backoff_max_seconds": "2", "allow_http": "true",
                          # el receptor falso está en 127.0.0.1: solo en pruebas se permite
                          "block_private_ips": "false"},
    })
    b.start()
    rx = Receiver()
    try:
        ids = support.seed_config(b)
        ch = b.admin("POST", "/admin/notification-channels", {
            "name": "Webhook de prueba", "kind": "webhook", "url": rx.url, "signing_secret": SIGNING_SECRET,
            "min_severity": "info"}, expect=201)
        yield {"backend": b, "ids": ids, "rx": rx, "channel": ch}
    finally:
        rx.stop()
        b.stop()
        support.drop_config_db(DBNAME)


@pytest.fixture()
def db(henv):
    conn = support.cfg_conn(DBNAME)
    yield conn
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def q(db, sql, params=()):
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def enroll(b, token, name):
    r = requests.post(b.url + "/agent/enroll", headers={"x-group-token": token},
                      json={"name": name, "hostname": "h", "client_version": "test"}, timeout=10)
    assert r.status_code == 201, r.text
    d = r.json()
    return {"x-installation-id": d["installation_id"], "x-installation-secret": d["secret"]}, d["installation_id"]


def now_iso(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()


def new_task(henv, active=True, schedule=60, group="a", **kw):
    b, ids = henv["backend"], henv["ids"]
    n = uuid.uuid4().hex[:8]
    company, agency = (ids["company_a"], ids["agency_a1"]) if group == "a" else (ids["company_b"], ids["agency_b1"])
    o = b.admin("POST", "/admin/objects", {"company_id": company["id"], "name": f"Obj {n}",
                                             "destination_table": f"t_{n}", "upsert_keys": "id"}, expect=201)
    t = b.admin("POST", "/admin/tasks", {"agency_id": agency["id"], "object_catalog_id": o["id"],
                                           "extract_sql": "SELECT 1 AS id", "schedule_seconds": schedule,
                                           "is_active": active, **kw}, expect=201)
    return t["id"]


def run_exec(b, h, tid, status="success", error_code="SOURCE_CONNECTION_FAILED", rows=5, wm=None, kind="source_clock",
             start_seq=None, finish_only=False):
    """Inicio + fin de una ejecución. Devuelve (execution_id, seq de inicio)."""
    ex = str(uuid.uuid4())
    s1 = start_seq or next(SEQ)
    if not finish_only:
        r = requests.post(b.url + "/agent/executions", headers=h, timeout=10, json={
            "execution_id": ex, "task_id": tid, "started_at": now_iso(), "agent_seq": s1, "client_version": "t"})
        assert r.status_code == 200, r.text
    return ex, s1, finish(b, h, ex, tid, status, error_code, rows, wm, kind)


def finish(b, h, ex, tid, status="success", error_code="SOURCE_CONNECTION_FAILED", rows=5, wm=None,
           kind="source_clock", seq=None):
    body = {"task_id": tid, "status": status, "finished_at": now_iso(), "duration_ms": 1200, "rows_read": rows,
            "rows_loaded": rows, "agent_seq": seq or next(SEQ), "client_version": "t"}
    if wm:
        body["checkpoint"] = {"watermark": wm, "kind": kind}
    if status != "success":
        body.update(failure_stage="extract", error_code=error_code,
                    error_message=f"no se pudo conectar password={support.SRC['password']}")
    r = requests.put(b.url + f"/agent/executions/{ex}", headers=h, json=body, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def start_only(b, h, tid, seq=None):
    ex = str(uuid.uuid4())
    s1 = seq or next(SEQ)
    r = requests.post(b.url + "/agent/executions", headers=h, timeout=10, json={
        "execution_id": ex, "task_id": tid, "started_at": now_iso(), "agent_seq": s1, "client_version": "t"})
    assert r.status_code == 200, r.text
    return ex, s1


def incidents(b, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return b.admin("GET", "/admin/incidents?" + qs, expect=200)["items"]


def evaluate(b):
    r = b.admin("POST", "/admin/health/evaluate", expect=200)
    assert r["ran"] is True
    return r


def wait_for(fn, timeout=20.0, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def deliveries(b, incident_id):
    return b.admin("GET", f"/admin/notification-deliveries?incident_id={incident_id}", expect=200)["items"]


# ─────────────────────────────────────────────────────────────────────────────
# Pruebas
# ─────────────────────────────────────────────────────────────────────────────
def test_falla_abre_y_recurrencias_se_agrupan_con_una_notificacion(henv, db):
    b, rx = henv["backend"], henv["rx"]
    h, iid = enroll(b, henv["ids"]["group_a"]["group_token"], "fallas")
    tid = new_task(henv)
    for code in ("SOURCE_CONNECTION_FAILED", "SOURCE_CONNECTION_FAILED", "DWH_CONNECTION_FAILED"):
        run_exec(b, h, tid, status="failed", error_code=code)
    items = incidents(b, task_id=tid, status="open")
    assert len(items) == 1
    inc = items[0]
    assert inc["category"] == "task_failed" and inc["occurrences"] == 3 and inc["severity"] == "error"
    assert inc["installation_id"] == iid and inc["last_error_code"] == "DWH_CONNECTION_FAILED"
    assert inc["details"]["error_codes"] == {"SOURCE_CONNECTION_FAILED": 2, "DWH_CONNECTION_FAILED": 1}
    # El mensaje se sanea (sin la contraseña del origen)
    assert support.SRC["password"] not in (inc["last_message_sanitized"] or "")
    # Una sola notificación (apertura), no una por recurrencia
    wait_for(lambda: [d for d in deliveries(b, inc["id"]) if d["status"] == "delivered"])
    ds = deliveries(b, inc["id"])
    assert [d["transition"] for d in ds] == ["opened"]
    opened = [x for x in rx.bodies("incident.opened") if x["incident"]["id"] == inc["id"]]
    assert len(opened) == 1
    # Firma HMAC verificable por el receptor; sin SQL ni secretos en el cuerpo
    req = [r for r in rx.requests if json.loads(r["body"]).get("incident", {}).get("id") == inc["id"]][0]
    ts, sig = req["headers"]["x-nexus-timestamp"], req["headers"]["x-nexus-signature"]
    expected = hmac.new(SIGNING_SECRET.encode(), ts.encode() + b"." + req["body"], hashlib.sha256).hexdigest()
    assert sig == "sha256=" + expected
    assert req["headers"]["x-nexus-delivery"] == f"incident-{inc['id']}-opened"
    raw = req["body"].decode()
    for secret in (support.SRC["password"], support.DWH["password"], "SELECT 1", SIGNING_SECRET):
        assert secret not in raw
    # Historial
    det = b.admin("GET", f"/admin/incidents/{inc['id']}", expect=200)
    types = [e["event_type"] for e in det["events"]]
    assert [t for t in types if t != "notified"] == ["opened", "recurred", "recurred"] and "notified" in types
    assert len(det["executions"]) == 3


def test_carga_confirmada_resuelve_solo_su_tarea(henv, db):
    b = henv["backend"]
    h, iid = enroll(b, henv["ids"]["group_a"]["group_token"], "recupera")
    t1, t2 = new_task(henv), new_task(henv)
    run_exec(b, h, t1, status="failed")
    run_exec(b, h, t2, status="failed")
    ex_ok, _, _ = run_exec(b, h, t1, status="success", wm="2025-03-01T00:00:00")
    r1 = incidents(b, task_id=t1)[0]
    assert r1["status"] == "resolved" and r1["resolution_reason"] == "load_confirmed"
    assert r1["resolved_execution_id"] == ex_ok and r1["duration_seconds"] is not None
    assert incidents(b, task_id=t2)[0]["status"] == "open"
    # Notificación de resolución, una sola vez
    wait_for(lambda: len([d for d in deliveries(b, r1["id"]) if d["status"] == "delivered"]) == 2)
    assert sorted(d["transition"] for d in deliveries(b, r1["id"])) == ["opened", "resolved"]
    # Una nueva falla abre OTRA incidencia (la resuelta queda en el historial)
    run_exec(b, h, t1, status="failed")
    rows = incidents(b, task_id=t1)
    assert len(rows) == 2 and {r["status"] for r in rows} == {"open", "resolved"}


def test_exito_con_cero_filas_no_es_falla_y_resuelve(henv, db):
    b = henv["backend"]
    h, _ = enroll(b, henv["ids"]["group_a"]["group_token"], "cero")
    tid = new_task(henv)
    run_exec(b, h, tid, status="failed")
    run_exec(b, h, tid, status="success", rows=0)
    inc = incidents(b, task_id=tid)[0]
    assert inc["status"] == "resolved" and inc["resolution_reason"] == "load_confirmed"
    th = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["state"] == "ok" and th["last_success_rows"] == 0 and th["consecutive_failures"] == 0
    assert th["current_error_code"] is None


def test_eventos_fuera_de_orden_no_reabren_ni_resuelven(henv, db):
    b = henv["backend"]
    h, _ = enroll(b, henv["ids"]["group_a"]["group_token"], "orden")
    tid = new_task(henv)
    # (a) Falla VIEJA que llega después de un éxito más nuevo → no abre incidencia
    old_ex, old_seq = start_only(b, h, tid)
    new_ex, _ = start_only(b, h, tid)
    finish(b, h, new_ex, tid, "success")
    finish(b, h, old_ex, tid, "failed")
    assert incidents(b, task_id=tid, status="open") == []
    # (b) Falla NUEVA abierta; un éxito VIEJO que llega tarde no la resuelve
    a_ex, _ = start_only(b, h, tid)        # éxito "viejo"
    c_ex, _ = start_only(b, h, tid)        # falla nueva
    finish(b, h, c_ex, tid, "failed")
    finish(b, h, a_ex, tid, "success")
    inc = incidents(b, task_id=tid, status="open")
    assert len(inc) == 1 and inc[0]["category"] == "task_failed"
    det = b.admin("GET", f"/admin/incidents/{inc[0]['id']}", expect=200)
    assert "late_evidence_ignored" in [e["event_type"] for e in det["events"]]
    # sync_state tampoco se pisa (fase 1)
    assert q(db, "SELECT last_status FROM task_sync_state WHERE task_id = %s", (tid,))[0][0] == "failed"
    # Un éxito realmente nuevo sí resuelve
    run_exec(b, h, tid, "success")
    assert incidents(b, task_id=tid, status="open") == []


def test_desconexion_detectada_por_nexus_y_latido_no_cierra_errores_de_tarea(henv, db):
    b = henv["backend"]
    h, iid = enroll(b, henv["ids"]["group_a"]["group_token"], "desconecta")
    tid = new_task(henv)
    run_exec(b, h, tid, status="failed")
    evaluate(b)
    assert incidents(b, installation_id=iid, category="disconnected") == []
    # Sin contacto > umbral (se mueve el reloj en la BD)
    q2 = support.cfg_conn(DBNAME)
    with q2.cursor() as cur:
        cur.execute("UPDATE installation SET last_seen_at = NOW() - INTERVAL '1 hour' WHERE id = %s", (iid,))
    q2.close()
    evaluate(b)
    evaluate(b)  # segundo ciclo: no duplica ni cuenta ocurrencias por ciclo
    d = incidents(b, installation_id=iid, category="disconnected")
    assert len(d) == 1 and d[0]["status"] == "open" and d[0]["occurrences"] == 1 and d[0]["severity"] == "critical"
    ih = [i for i in b.admin("GET", "/admin/health/installations", expect=200)["items"] if i["id"] == iid][0]
    assert ih["connectivity"] == "offline" and ih["open_incidents"] == 2
    # Latido → se resuelve la desconexión, NO la falla de la tarea
    r = requests.post(b.url + "/agent/heartbeat", headers=h, json={"client_version": "t", "queue_depth": 0})
    assert r.status_code == 200
    d = incidents(b, installation_id=iid, category="disconnected")[0]
    assert d["status"] == "resolved" and d["resolution_reason"] == "heartbeat_recovered"
    tf = incidents(b, task_id=tid, category="task_failed")[0]
    assert tf["status"] == "open"
    evaluate(b)
    assert incidents(b, installation_id=iid, category="disconnected", status="open") == []


def test_reconocer_no_resuelve_ni_convierte_en_exito(henv, db):
    b = henv["backend"]
    h, _ = enroll(b, henv["ids"]["group_a"]["group_token"], "ack")
    tid = new_task(henv)
    run_exec(b, h, tid, status="failed")
    inc = incidents(b, task_id=tid)[0]
    assert inc["acknowledged"] is False
    assert inc["id"] in [i["id"] for i in incidents(b, view="active")]
    r = b.admin("PUT", f"/admin/incidents/{inc['id']}/ack", {"comment": "Revisando con la sede"}, expect=200)
    assert r["acknowledged"] is True and r["status"] == "open" and r["acknowledged_by"] == "admin"
    assert inc["id"] in [i["id"] for i in incidents(b, view="acknowledged")]
    assert inc["id"] not in [i["id"] for i in incidents(b, view="active")]
    # No se puede cerrar a mano una falla de tarea
    rr = requests.put(b.url + f"/admin/incidents/{inc['id']}/resolve", json={"reason": "ya quedó"},
                      headers={"x-admin-token": b.admin_token})
    assert rr.status_code == 409
    th = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["state"] == "failing" and th["current_error_code"] == "SOURCE_CONNECTION_FAILED"
    assert th["open_incidents"][0]["acknowledged"] is True
    # Recurrencias sobre una reconocida: sigue reconocida y abierta
    run_exec(b, h, tid, status="failed")
    inc2 = incidents(b, task_id=tid)[0]
    assert inc2["id"] == inc["id"] and inc2["occurrences"] == 2 and inc2["acknowledged"] is True
    # Badge: solo cuenta abiertas NO reconocidas
    badge = b.admin("GET", "/admin/incidents/badge", expect=200)
    assert badge["open_total"] > badge["open_unacknowledged"]
    # Después la carga confirmada sí resuelve (y la marca de reconocida se conserva en el historial)
    run_exec(b, h, tid, status="success")
    inc3 = incidents(b, task_id=tid)[0]
    assert inc3["status"] == "resolved" and inc3["acknowledged"] is True


def test_retraso_por_periodicidad_y_tarea_deshabilitada(henv, db):
    b = henv["backend"]
    h, _ = enroll(b, henv["ids"]["group_a"]["group_token"], "retraso")
    tid = new_task(henv, schedule=60, expected_duration_seconds=10, delay_tolerance_seconds=5)
    tid_off = new_task(henv, active=False, schedule=60)
    evaluate(b)
    th = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["state"] == "never_run" and th["delayed"] is False
    assert th["expected_duration_seconds"] == 10 and th["expected_duration_source"] == "configured"
    # Gracia vencida (activa "desde hace 1 h") y sin cargas → retrasada
    c = support.cfg_conn(DBNAME)
    with c.cursor() as cur:
        cur.execute("UPDATE task_health_state SET active_since = NOW() - INTERVAL '1 hour' WHERE task_id IN (%s, %s)",
                    (tid, tid_off))
    c.close()
    evaluate(b)
    evaluate(b)
    d = incidents(b, task_id=tid, category="task_delayed")
    assert len(d) == 1 and d[0]["status"] == "open" and d[0]["installation_id"] is None and d[0]["occurrences"] == 1
    # La deshabilitada nunca se marca retrasada
    assert incidents(b, task_id=tid_off) == []
    tho = b.admin("GET", f"/admin/health/tasks?task_id={tid_off}", expect=200)["items"][0]
    assert tho["state"] == "disabled" and tho["delayed"] is False
    # Carga confirmada → se resuelve el retraso
    run_exec(b, h, tid, "success")
    d = incidents(b, task_id=tid, category="task_delayed")[0]
    assert d["status"] == "resolved" and d["resolution_reason"] == "load_confirmed"
    # Otra vez retrasada (la última carga fue "hace 2 h") y se deshabilita → task_disabled
    c = support.cfg_conn(DBNAME)
    with c.cursor() as cur:
        cur.execute("""UPDATE task_execution SET finished_at = NOW() - INTERVAL '2 hours',
                              updated_at = NOW() - INTERVAL '2 hours' WHERE task_id = %s""", (tid,))
    c.close()
    evaluate(b)
    assert incidents(b, task_id=tid, category="task_delayed", status="open")
    b.admin("POST", f"/admin/tasks/{tid}/disable", expect=200)
    evaluate(b)
    d = incidents(b, task_id=tid, category="task_delayed")
    assert all(x["status"] == "resolved" for x in d)
    assert d[0]["resolution_reason"] == "task_disabled"
    # Al rehabilitarla hay gracia nueva (no alerta inmediata)
    b.admin("POST", f"/admin/tasks/{tid}/enable", expect=200)
    evaluate(b)
    assert incidents(b, task_id=tid, category="task_delayed", status="open") == []


def test_ejecucion_en_curso_y_prolongada(henv, db):
    b = henv["backend"]
    h, iid = enroll(b, henv["ids"]["group_a"]["group_token"], "larga")
    tid = new_task(henv, schedule=60, expected_duration_seconds=10, delay_tolerance_seconds=5)
    ex, _ = start_only(b, h, tid)
    requests.post(b.url + "/agent/heartbeat", headers=h, json={"running": [{"task_id": tid, "execution_id": ex}]})
    th = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["state"] == "running" and th["running_execution"]["confirmed_by_heartbeat"] is True
    c = support.cfg_conn(DBNAME)
    with c.cursor() as cur:
        cur.execute("UPDATE task_execution SET received_at = NOW() - INTERVAL '1 hour' WHERE execution_id = %s", (ex,))
        cur.execute("UPDATE task_health_state SET active_since = NOW() - INTERVAL '3 hours' WHERE task_id = %s", (tid,))
    c.close()
    evaluate(b)
    evaluate(b)
    rl = incidents(b, task_id=tid, category="task_running_long")
    assert len(rl) == 1 and rl[0]["status"] == "open" and rl[0]["occurrences"] == 1
    # En curso (confirmada por el latido) no cuenta como retrasada
    assert incidents(b, task_id=tid, category="task_delayed") == []
    finish(b, h, ex, tid, "success")
    rl = incidents(b, task_id=tid, category="task_running_long")[0]
    assert rl["status"] == "resolved" and rl["resolution_reason"] == "execution_finished"


def test_checkpoint_de_otro_reloj_abre_incidencia_y_se_resuelve(henv, db):
    b = henv["backend"]
    h, _ = enroll(b, henv["ids"]["group_a"]["group_token"], "reloj")
    tid = new_task(henv)
    run_exec(b, h, tid, "success", wm="2025-01-01T00:00:00", kind="source_clock")
    run_exec(b, h, tid, "success", wm="2025-01-02T00:00:00", kind="agent_local")
    inc = incidents(b, task_id=tid, category="checkpoint_kind_mismatch")
    assert len(inc) == 1 and inc[0]["status"] == "open"
    # Un checkpoint del reloj correcto lo resuelve
    run_exec(b, h, tid, "success", wm="2025-01-03T00:00:00", kind="source_clock")
    assert incidents(b, task_id=tid, category="checkpoint_kind_mismatch")[0]["resolution_reason"] == "checkpoint_applied"
    # Y el reinicio del watermark también
    run_exec(b, h, tid, "success", wm="2025-01-04T00:00:00", kind="agent_utc")
    assert incidents(b, task_id=tid, category="checkpoint_kind_mismatch", status="open")
    b.admin("POST", f"/admin/tasks/{tid}/reset-last-run", expect=200)
    last = incidents(b, task_id=tid, category="checkpoint_kind_mismatch")
    assert all(x["status"] == "resolved" for x in last)
    assert "watermark_reset" in [x["resolution_reason"] for x in last]


def test_dead_letter_se_cierra_solo_manualmente_con_motivo(henv, db):
    b = henv["backend"]
    h, iid = enroll(b, henv["ids"]["group_a"]["group_token"], "deadletter")
    ev = {"event_id": str(uuid.uuid4()), "event_type": "dead_letter", "agent_seq": next(SEQ),
          "payload": {"dead_letter_total": 3, "parked_total": 1}}
    assert requests.post(b.url + "/agent/events", headers=h, json=ev).status_code == 200
    assert requests.post(b.url + "/agent/events", headers=h, json=ev).json()["duplicate"] is True
    inc = incidents(b, installation_id=iid, category="queue_dead_letter")
    assert len(inc) == 1 and inc[0]["occurrences"] == 1 and inc[0]["manual_resolvable"] is True
    assert inc[0]["details"]["counters"]["dead_letter_total"] == 3
    r = requests.put(b.url + f"/admin/incidents/{inc[0]['id']}/resolve", json={},
                     headers={"x-admin-token": b.admin_token})
    assert r.status_code == 422
    r = b.admin("PUT", f"/admin/incidents/{inc[0]['id']}/resolve", {"reason": "Revisado el log local"}, expect=200)
    assert r["status"] == "resolved" and r["resolution_reason"] == "manual" and r["resolved_by"] == "admin"
    assert r["resolution_comment"] == "Revisado el log local"


def test_revocar_instalacion_cierra_sus_incidencias(henv, db):
    b = henv["backend"]
    h, iid = enroll(b, henv["ids"]["group_a"]["group_token"], "revocar")
    tid = new_task(henv)
    run_exec(b, h, tid, status="failed")
    b.admin("POST", f"/admin/installations/{iid}/revoke", {"reason": "prueba"}, expect=200)
    evaluate(b)
    inc = incidents(b, installation_id=iid)
    assert inc and all(i["status"] == "resolved" and i["resolution_reason"] == "installation_revoked" for i in inc)


def test_advisory_lock_evita_doble_evaluacion(henv, db):
    b = henv["backend"]
    holder = support.cfg_conn(DBNAME)
    with holder.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(834120777)")
    try:
        r = b.admin("POST", "/admin/health/evaluate", expect=200)
        assert r["ran"] is False
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(834120777)")
        holder.close()
    assert b.admin("POST", "/admin/health/evaluate", expect=200)["ran"] is True


def test_notificacion_reintenta_con_backoff_y_falla_definitiva(henv, db):
    """Canal propio (receptor local aparte) filtrado por el grupo B para aislar los códigos de respuesta."""
    b = henv["backend"]
    rx2 = Receiver()
    try:
        ch = b.admin("POST", "/admin/notification-channels", {
            "name": "Reintentos", "kind": "webhook", "url": rx2.url, "min_severity": "error",
            "group_id": henv["ids"]["group_b"]["id"], "categories": ["task_failed"]}, expect=201)
        h, _ = enroll(b, henv["ids"]["group_b"]["group_token"], "reintentos")

        def mine(inc_id, status):
            return [d for d in deliveries(b, inc_id) if d["channel_id"] == ch["id"] and d["status"] == status]

        # 500, 500 y luego 200 → entregada al tercer intento (backoff 1 s, 2 s)
        rx2.codes[:] = [500, 500]
        tid = new_task(henv, group="b")
        run_exec(b, h, tid, status="failed")
        inc = incidents(b, task_id=tid)[0]
        ok = wait_for(lambda: mine(inc["id"], "delivered"), timeout=30)
        assert ok and ok[0]["attempts"] == 3
        assert len(rx2.requests) == 3
        # Todas las entregas del mismo evento llevan la misma clave de idempotencia
        assert {r["headers"]["x-nexus-delivery"] for r in rx2.requests} == {f"incident-{inc['id']}-opened"}
        # Siempre 500 → 'failed' tras max_attempts (3) y evento en el historial
        rx2.codes[:] = [500] * 10
        tid2 = new_task(henv, group="b")
        run_exec(b, h, tid2, status="failed")
        inc2 = incidents(b, task_id=tid2)[0]
        bad = wait_for(lambda: mine(inc2["id"], "failed"), timeout=30)
        assert bad and bad[0]["attempts"] == 3 and bad[0]["last_error"] == "HTTP 500"
        det = b.admin("GET", f"/admin/incidents/{inc2['id']}", expect=200)
        assert "notification_failed" in [e["event_type"] for e in det["events"]]
        # El filtro por categoría: una desconexión del grupo B no va a este canal
        assert all(json.loads(r["body"])["incident"]["category"] == "task_failed" for r in rx2.requests)
        b.admin("DELETE", f"/admin/notification-channels/{ch['id']}", expect=200)
    finally:
        rx2.stop()


def test_canales_secretos_solo_escritura_y_prueba(henv, db):
    b, rx = henv["backend"], henv["rx"]
    lst = b.admin("GET", "/admin/notification-channels", expect=200)
    raw = json.dumps(lst)
    assert rx.url not in raw and SIGNING_SECRET not in raw and "/hook" not in raw
    ch = [c for c in lst["items"] if c["id"] == henv["channel"]["id"]][0]
    assert ch["has_url"] and ch["has_secret"] and ch["encrypted"] and ch["url_display"].endswith("/…")
    row = q(db, "SELECT url_enc, signing_secret_enc FROM notification_channel WHERE id = %s", (ch["id"],))[0]
    assert row[0].startswith("ENC:") and row[1].startswith("ENC:")
    n = len(rx.bodies("test"))
    res = b.admin("POST", f"/admin/notification-channels/{ch['id']}/test", expect=200)
    assert res["status"] == "delivered"
    assert len(rx.bodies("test")) == n + 1
    # URL no https sin allow_http se rechaza; URL con credenciales se rechaza
    r = requests.post(b.url + "/admin/notification-channels", headers={"x-admin-token": b.admin_token},
                      json={"name": "malo", "kind": "webhook", "url": "http://u:p@127.0.0.1/x"})
    assert r.status_code == 422
    r = requests.post(b.url + "/admin/notification-channels", headers={"x-admin-token": b.admin_token},
                      json={"name": "malo2", "kind": "webhook", "url": "ftp://127.0.0.1/x"})
    assert r.status_code == 422
    # Canal "log" (desarrollo) con filtro de categorías
    lg = b.admin("POST", "/admin/notification-channels", {"name": "Log", "kind": "log",
                                                           "categories": ["disconnected"]}, expect=201)
    assert lg["kind"] == "log" and lg["categories"] == ["disconnected"]
    upd = b.admin("PUT", f"/admin/notification-channels/{lg['id']}", {"is_enabled": False}, expect=200)
    assert upd["is_enabled"] is False
    b.admin("DELETE", f"/admin/notification-channels/{lg['id']}", expect=200)


def test_modelo_de_salud_y_resumen(henv, db):
    b = henv["backend"]
    s = b.admin("GET", "/admin/health/summary", expect=200)
    assert set(s) >= {"installations", "tasks", "incidents"}
    assert s["incidents"]["open_total"] >= s["incidents"]["open_unacknowledged"]
    tasks = b.admin("GET", "/admin/health/tasks", expect=200)["items"]
    assert tasks and all(t["state"] in ("ok", "running", "failing", "delayed", "never_run", "disabled") for t in tasks)
    t = tasks[0]
    for k in ("last_execution", "last_success_at", "watermark", "watermark_kind", "current_error_code",
              "consecutive_failures", "next_expected_run_at", "delay_deadline_at", "expected_duration_seconds"):
        assert k in t
    assert not any(k.startswith("_") for k in t)
    raw = json.dumps(tasks)
    assert support.SQL_MARKER not in raw and "extract_sql" not in raw
    settings = b.admin("GET", "/admin/health/settings", expect=200)
    assert settings["health"]["disconnect_after_seconds"] == 300
    assert requests.get(b.url + "/admin/incidents").status_code == 401


def test_tareas_que_ya_fallaban_antes_de_la_migracion_se_rellenan(henv, db):
    """Simula el estado previo a la 005: la tarea falla en task_sync_state y no hay incidencias."""
    b = henv["backend"]
    h, iid = enroll(b, henv["ids"]["group_a"]["group_token"], "relleno")
    tid = new_task(henv)
    run_exec(b, h, tid, status="failed")
    c = support.cfg_conn(DBNAME)
    with c.cursor() as cur:
        cur.execute("DELETE FROM incident WHERE task_id = %s", (tid,))
    c.close()
    evaluate(b)
    inc = incidents(b, task_id=tid)
    assert len(inc) == 1 and inc[0]["status"] == "open" and inc[0]["details"]["backfilled"] is True
    assert inc[0]["installation_id"] == iid
    evaluate(b)
    assert len(incidents(b, task_id=tid)) == 1
    # Con historial para la clave no se vuelve a abrir tras resolverse
    run_exec(b, h, tid, status="success")
    evaluate(b)
    assert [i["status"] for i in incidents(b, task_id=tid)] == ["resolved"]


# ─────────────────────────────────────────────────────────────────────────────
# Correcciones tras la validación de la fase 2
# ─────────────────────────────────────────────────────────────────────────────
def local_engine(**health):
    """Motor en proceso contra la BD de prueba (para pasos internos deterministas)."""
    import health_postgres as hp
    return hp.IncidentEngine(
        get_connection=lambda: psycopg2.connect(host=support.CFG_PG["host"], port=support.CFG_PG["port"],
                                                user=support.CFG_PG["user"], password=support.CFG_PG["password"],
                                                dbname=DBNAME),
        settings=hp.HealthSettings(**health), notif=hp.NotificationSettings(),
        get_secret_cipher=lambda: None, decrypt_config_secret=lambda v: v or "")


def exec_sql(sql, params=()):
    c = support.cfg_conn(DBNAME)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
    finally:
        c.close()


def test_evaluador_aisla_fallos_por_instalacion_y_por_fase(henv, db):
    b = henv["backend"]
    tag = uuid.uuid4().hex[:6]
    _, bad = enroll(b, henv["ids"]["group_a"]["group_token"], f"explota-{tag}")
    _, good = enroll(b, henv["ids"]["group_a"]["group_token"], f"normal-{tag}")
    hf, inst_f = enroll(b, henv["ids"]["group_a"]["group_token"], f"relleno-{tag}")
    tid = new_task(henv, schedule=60, expected_duration_seconds=5, delay_tolerance_seconds=5)
    tid_fail = new_task(henv)
    run_exec(b, hf, tid_fail, status="failed")
    evaluate(b)
    exec_sql("UPDATE installation SET last_seen_at = NOW() - INTERVAL '1 hour' WHERE id IN (%s, %s)", (bad, good))
    exec_sql("UPDATE task_health_state SET active_since = NOW() - INTERVAL '1 hour' WHERE task_id = %s", (tid,))
    exec_sql("DELETE FROM incident WHERE task_id = %s", (tid_fail,))   # para que el relleno tenga trabajo
    # Inyección de fallas: insertar una incidencia de la instalación "explota-*" falla siempre
    exec_sql(f"""CREATE OR REPLACE FUNCTION test_boom_{tag}() RETURNS trigger AS $$
                 BEGIN IF NEW.installation_name LIKE 'explota-%%' THEN RAISE EXCEPTION 'boom'; END IF; RETURN NEW; END
                 $$ LANGUAGE plpgsql""")
    exec_sql(f"CREATE TRIGGER test_boom_{tag} BEFORE INSERT ON incident FOR EACH ROW EXECUTE PROCEDURE test_boom_{tag}()")
    # ...y toda la fase de tareas falla (task_health_state)
    exec_sql(f"""CREATE OR REPLACE FUNCTION test_boom2_{tag}() RETURNS trigger AS $$
                 BEGIN RAISE EXCEPTION 'boom2'; END $$ LANGUAGE plpgsql""")
    exec_sql(f"CREATE TRIGGER test_boom2_{tag} BEFORE INSERT OR UPDATE ON task_health_state "
             f"FOR EACH ROW EXECUTE PROCEDURE test_boom2_{tag}()")
    try:
        r = b.admin("POST", "/admin/health/evaluate", expect=200)
        assert r["ran"] is True and r["errors"] >= 2 and "tareas" in r["failed_phases"]
        # La instalación sana sí quedó desconectada; la problemática no bloqueó a las demás
        assert incidents(b, installation_id=good, category="disconnected", status="open")
        assert incidents(b, installation_id=bad, category="disconnected") == []
        # Las fases posteriores a la que falló corrieron (relleno)
        rel = incidents(b, task_id=tid_fail, status="open")
        assert rel and rel[0]["details"].get("backfilled") is True
    finally:
        exec_sql(f"DROP TRIGGER IF EXISTS test_boom_{tag} ON incident")
        exec_sql(f"DROP TRIGGER IF EXISTS test_boom2_{tag} ON task_health_state")
    # Sin la inyección, el siguiente ciclo completa lo pendiente
    r = b.admin("POST", "/admin/health/evaluate", expect=200)
    assert r["errors"] == 0 and r["failed_phases"] == []
    assert incidents(b, installation_id=bad, category="disconnected", status="open")
    assert incidents(b, task_id=tid, category="task_delayed", status="open")


def test_latido_viejo_de_instalacion_muerta_no_mantiene_tarea_en_curso(henv, db):
    b = henv["backend"]
    h, iid = enroll(b, henv["ids"]["group_a"]["group_token"], "muerta")
    tid = new_task(henv, schedule=60, expected_duration_seconds=10, delay_tolerance_seconds=5)
    ex, _ = start_only(b, h, tid)
    requests.post(b.url + "/agent/heartbeat", headers=h, json={"running": [{"task_id": tid, "execution_id": ex}]})
    assert b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]["state"] == "running"
    evaluate(b)   # crea task_health_state de la tarea
    # El agente murió hace 3 días; su último latido sigue listando la ejecución
    exec_sql("UPDATE installation SET last_seen_at = NOW() - INTERVAL '3 days' WHERE id = %s", (iid,))
    exec_sql("UPDATE task_execution SET received_at = NOW() - INTERVAL '3 days' WHERE execution_id = %s", (ex,))
    exec_sql("UPDATE task_health_state SET active_since = NOW() - INTERVAL '3 days' WHERE task_id = %s", (tid,))
    evaluate(b)
    th = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["running"] is False and th["running_execution"] is None
    assert th["state"] == "delayed" and th["delayed"] is True
    assert incidents(b, task_id=tid, category="task_delayed", status="open")
    assert incidents(b, task_id=tid, category="task_running_long") == []
    assert incidents(b, installation_id=iid, category="disconnected", status="open")


def test_falla_abierta_en_otra_instalacion_mantiene_la_tarea_con_error(henv, db):
    b = henv["backend"]
    ha, ia = enroll(b, henv["ids"]["group_a"]["group_token"], "inst-a")
    hb_, _ = enroll(b, henv["ids"]["group_a"]["group_token"], "inst-b")
    tid = new_task(henv, schedule=60, expected_duration_seconds=5, delay_tolerance_seconds=5)
    run_exec(b, ha, tid, status="failed", error_code="SOURCE_AUTH_FAILED")
    time.sleep(1.1)
    run_exec(b, hb_, tid, status="success")          # otra instalación, más nueva
    assert q(db, "SELECT last_status FROM task_sync_state WHERE task_id = %s", (tid,))[0][0] == "success"
    th = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["state"] == "failing" and th["failing"] is True
    assert th["current_error_code"] == "SOURCE_AUTH_FAILED"
    assert [f["installation_id"] for f in th["failing_installations"]] == [ia]
    # Sin doble alerta: con la falla abierta, el retraso no abre otra incidencia
    evaluate(b)   # crea task_health_state de la tarea
    exec_sql("""UPDATE task_execution SET finished_at = NOW() - INTERVAL '2 hours', updated_at = NOW() - INTERVAL '2 hours'
                WHERE task_id = %s""", (tid,))
    exec_sql("UPDATE task_health_state SET active_since = NOW() - INTERVAL '3 hours' WHERE task_id = %s", (tid,))
    evaluate(b)
    th = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["delayed"] is True
    assert incidents(b, task_id=tid, category="task_delayed") == []
    # Cuando A se recupera, deja de fallar y ya se puede abrir el retraso
    run_exec(b, ha, tid, status="success")
    exec_sql("""UPDATE task_execution SET finished_at = NOW() - INTERVAL '2 hours', updated_at = NOW() - INTERVAL '2 hours'
                WHERE task_id = %s""", (tid,))
    evaluate(b)
    assert b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]["failing_installations"] == []
    assert incidents(b, task_id=tid, category="task_delayed", status="open")


def test_ssrf_destinos_privados_bloqueados():
    import health_postgres as hp
    for url in ("https://127.0.0.1/x", "https://10.0.0.1/x", "https://172.16.5.5/x", "https://192.168.1.1/x",
                "https://169.254.169.254/latest", "https://100.64.0.1/x", "https://[::1]/x", "https://[fe80::1]/x",
                "https://0.0.0.0/x", "https://224.0.0.1/x", "https://localhost/x", "https://[::ffff:127.0.0.1]/x"):
        with pytest.raises(ValueError):
            hp.check_destination(url)
    hp.check_destination("https://93.184.216.34/x")   # IP pública literal (no se envía nada)
    with pytest.raises(ValueError):
        hp.validate_webhook_url("https://10.1.2.3/hook", allow_http=False, block_private=True)
    # En el envío: aunque el canal esté guardado, el destino privado se bloquea sin conectar
    rx = Receiver()
    try:
        res = hp.WebhookSender().send({"url": rx.url, "block_private": True, "timeout_seconds": 2},
                                      {"event": "test"}, "d-1")
        assert res.ok is False and "bloqueado" in res.error and rx.requests == []
    finally:
        rx.stop()


def test_ssrf_api_rechaza_destinos_privados_por_defecto(env):
    """Backend con la configuración por defecto (block_private_ips = true)."""
    b = env["backend"]
    for url in ("https://127.0.0.1/hook", "https://169.254.169.254/latest", "https://localhost:8443/x"):
        r = requests.post(b.url + "/admin/notification-channels", headers={"x-admin-token": b.admin_token},
                          json={"name": "ssrf-" + uuid.uuid4().hex[:6], "kind": "webhook", "url": url,
                                "is_enabled": False}, timeout=10)
        assert r.status_code == 422 and "privada" in r.text, (url, r.text)
    # /monitor/clients: horas con zona (aditivo)
    requests.get(b.url + "/configs", headers={"x-token": env["ids"]["company_a"]["company_token"]}, timeout=10)
    cl = b.monitor("/monitor/clients")["clients"]
    assert cl and all("last_seen_utc" in c for c in cl)
    assert any((c["last_seen_utc"] or "").endswith("Z") for c in cl)


def test_canal_borrado_a_mitad_del_envio_no_aborta_el_lote(henv, db):
    b = henv["backend"]
    eng = local_engine()
    ch = b.admin("POST", "/admin/notification-channels", {"name": "efimero-" + uuid.uuid4().hex[:6], "kind": "log",
                                                           "is_enabled": False}, expect=201)
    oid = q(db, """INSERT INTO notification_outbox (channel_id, incident_id, transition, idempotency_key, payload, status)
                   VALUES (%s, NULL, 'test', %s, '{}'::jsonb, 'sending') RETURNING id""",
            (ch["id"], "k-" + uuid.uuid4().hex))[0][0]
    b.admin("DELETE", f"/admin/notification-channels/{ch['id']}", expect=200)   # borra también su outbox
    stats = {"delivered": 0, "retry": 0, "failed": 0, "skipped": 0}
    assert eng._deliver_one(oid, stats) == "gone" and stats["skipped"] == 1


def test_retencion_outbox_en_el_notificador_y_del_historial(henv, db):
    b = henv["backend"]
    ch = henv["channel"]
    old = q(db, """INSERT INTO notification_outbox (channel_id, incident_id, transition, idempotency_key, payload, status,
                                                    created_at)
                   VALUES (%s, NULL, 'test', %s, '{}'::jsonb, 'delivered', NOW() - INTERVAL '90 days') RETURNING id""",
            (ch["id"], "viejo-" + uuid.uuid4().hex))[0][0]
    # El notificador purga aunque el evaluador esté apagado (evaluator_interval_seconds = 0 aquí)
    local_engine().deliver_pending()
    assert q(db, "SELECT COUNT(*) FROM notification_outbox WHERE id = %s", (old,))[0][0] == 0
    # Historial de ejecuciones: e1 falla (I1), e2 éxito (resuelve I1), e3 éxito, e4 falla (I2 abierta)
    h, _ = enroll(b, henv["ids"]["group_a"]["group_token"], "retencion")
    tid = new_task(henv)
    e1, _, _ = run_exec(b, h, tid, status="failed")
    e2, _, _ = run_exec(b, h, tid, status="success")
    e3, _, _ = run_exec(b, h, tid, status="success")
    e4, _, _ = run_exec(b, h, tid, status="failed")
    i1 = [i for i in incidents(b, task_id=tid) if i["status"] == "resolved"][0]
    exec_sql("UPDATE task_execution SET received_at = NOW() - INTERVAL '100 days' WHERE task_id = %s", (tid,))
    exec_sql("UPDATE incident SET resolved_at = NOW() - INTERVAL '400 days' WHERE id = %s", (i1["id"],))
    exec_sql("INSERT INTO incident_event (incident_id, event_type) VALUES (%s, 'recurred')", (i1["id"],))
    out = local_engine(execution_retention_days=30, incident_event_retention_days=365).maybe_housekeeping(force=True)
    left = {str(r[0]) for r in q(db, "SELECT execution_id FROM task_execution WHERE task_id = %s", (tid,))}
    assert left == {e3, e4}, out           # la última carga exitosa y la última (abierta/sync) se conservan
    ev = [r[0] for r in q(db, "SELECT event_type FROM incident_event WHERE incident_id = %s ORDER BY id", (i1["id"],))]
    assert "recurred" not in ev and "opened" in ev and "resolved" in ev
    th = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["last_execution"]["execution_id"] == e4 and th["last_success_execution_id"] == e3
