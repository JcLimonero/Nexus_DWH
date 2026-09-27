"""
Integración real de salud e incidencias (fase 2): backend (subproceso) con el
evaluador de salud activo (cada 1 s, desconexión a los 10 s) + origen y DWH
PostgreSQL en Docker local + agente real (en proceso o como subproceso).
Las notificaciones van SOLO a un receptor HTTP local falso.

Escenarios: DMS caído/recuperado, DWH caído/recuperado, pérdida de Nexus con
cola y eventos en orden, agente muerto → desconexión detectada por Nexus y
recuperación que NO cierra errores de tareas, y tarea larga con latidos.
"""

import os
import signal
import subprocess
import threading
import time
import uuid

import pytest

import support
from nexus_agent.agent import Agent
from nexus_agent.logsetup import setup_logging
from nexus_agent.settings import load_settings

DBNAME = "nexus_test_cfg_client_health"
DISCONNECT = 10


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.ensure_container(support.SRC)
    support.ensure_container(support.DWH)
    support.seed_source()
    support.reset_dwh()
    support.recreate_config_db(DBNAME)
    b = support.Backend(DBNAME, extra_ini={
        "health": {"evaluator_interval_seconds": "1", "disconnect_after_seconds": str(DISCONNECT),
                   "heartbeat_expected_seconds": "2"},
        "notifications": {"worker_interval_seconds": "1", "allow_http": "true", "block_private_ips": "false", "backoff_base_seconds": "1",
                          "backoff_max_seconds": "2"},
    })
    b.start()
    rx = support.FakeWebhookReceiver()
    base = tmp_path_factory.mktemp("agents_salud")
    procs = []
    try:
        ids = support.seed_config(b)
        b.admin("POST", "/admin/notification-channels", {"name": "Receptor local", "kind": "webhook",
                                                          "url": rx.url, "min_severity": "info"}, expect=201)
        yield {"backend": b, "ids": ids, "base": base, "rx": rx, "procs": procs}
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        rx.stop()
        b.stop()
        support.ensure_container(support.SRC)
        support.ensure_container(support.DWH)
        support.drop_config_db(DBNAME)


def write_ini(env, name, token_kind="group_token", token=None, extra=None):
    d = env["base"] / name
    d.mkdir(exist_ok=True)
    token = token or env["ids"]["group_a"]["group_token"]
    agent = {"data_dir": str(d / "data"), "log_dir": str(d / "logs"), "heartbeat_seconds": "5",
             "tick_seconds": "0.2", "fetch_chunk_rows": "100", "shutdown_grace_seconds": "5",
             "task_retry_attempts": "0", "db_connect_timeout_seconds": "3",
             "queue_backoff_base_seconds": "0.2", "queue_backoff_max_seconds": "1"}
    agent.update(extra or {})
    lines = ["[nexus]", f"api_url = {env['backend'].url}", f"{token_kind} = {token}", "mode = development",
             f"installation_name = {name}", "", "[agent]"] + [f"{k} = {v}" for k, v in agent.items()]
    path = d / "config.ini"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def make_agent(env, name, **kw):
    s = load_settings(write_ini(env, name, **kw))
    setup_logging(s.log_dir, console=False)
    a = Agent(s)
    a.settings.heartbeat_seconds = 1   # el INI exige >= 5; en proceso se acelera
    a.ensure_credential()
    return a


def task_cfg(agent, task_id):
    agent.refresh_config()
    cfg = agent.fresh_config()
    for t in cfg["tasks"]:
        if t["task_id"] == task_id:
            return t, cfg["warehouse"]
    raise AssertionError(f"tarea {task_id} no autorizada")


def cfg_q(sql, params=()):
    c = support.cfg_conn(DBNAME)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def incidents(env, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return env["backend"].admin("GET", "/admin/incidents?" + qs, expect=200)["items"]


def wait_for(fn, timeout=30.0, step=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def notified(env, incident_id, event):
    return [x for x in env["rx"].bodies(event) if (x.get("incident") or {}).get("id") == incident_id]


def iid_of(agent):
    return agent.holder.get().installation_id


def stop_start(container):
    support.docker("stop", "-t", "2", container["container"])


def up(container):
    support.docker("start", container["container"])
    support.wait_pg(container)


# ─────────────────────────────────────────────────────────────────────────────
# Escenarios
# ─────────────────────────────────────────────────────────────────────────────
def test_01_dms_caido_abre_incidencia_y_recuperacion_la_resuelve(env):
    a = make_agent(env, "salud-dms")
    iid = iid_of(a)
    tid = env["ids"]["t_cli"]["id"]
    task, wh = task_cfg(a, tid)
    assert a.execute_task(task, wh)["status"] == "success"
    assert a.flush(timeout=20)
    stop_start(support.SRC)
    try:
        out = a.execute_task(task, wh)
    finally:
        up(support.SRC)
    assert out["status"] == "failed" and out["error_code"].startswith("SOURCE_")
    assert a.flush(timeout=20)
    inc = incidents(env, installation_id=iid, task_id=tid, category="task_failed", status="open")
    assert len(inc) == 1 and inc[0]["last_error_code"].startswith("SOURCE_")
    th = env["backend"].admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
    assert th["state"] == "failing" and th["consecutive_failures"] == 1
    # La instalación sigue EN LÍNEA: conectividad ≠ éxito del ETL
    ih = [i for i in env["backend"].admin("GET", "/admin/health/installations", expect=200)["items"] if i["id"] == iid][0]
    assert ih["connectivity"] == "online"
    task, wh = task_cfg(a, tid)
    assert a.execute_task(task, wh)["status"] == "success"
    assert a.flush(timeout=20)
    r = incidents(env, installation_id=iid, task_id=tid, category="task_failed")[0]
    assert r["id"] == inc[0]["id"] and r["status"] == "resolved" and r["resolution_reason"] == "load_confirmed"
    assert wait_for(lambda: notified(env, r["id"], "incident.opened") and notified(env, r["id"], "incident.resolved"))
    assert len(notified(env, r["id"], "incident.opened")) == 1


def test_02_dwh_caido_abre_incidencia_y_recuperacion_la_resuelve(env):
    a = make_agent(env, "salud-dwh")
    iid = iid_of(a)
    tid = env["ids"]["t_cli"]["id"]
    task, wh = task_cfg(a, tid)
    stop_start(support.DWH)
    try:
        out = a.execute_task(task, wh)
    finally:
        up(support.DWH)
    assert out["status"] == "failed" and out["error_code"].startswith("DWH_")
    assert a.flush(timeout=20)
    inc = incidents(env, installation_id=iid, task_id=tid, category="task_failed", status="open")
    assert len(inc) == 1 and inc[0]["last_error_code"].startswith("DWH_")
    task, wh = task_cfg(a, tid)
    assert a.execute_task(task, wh)["status"] == "success"
    assert a.flush(timeout=20)
    r = incidents(env, installation_id=iid, task_id=tid, category="task_failed")[0]
    assert r["status"] == "resolved" and r["resolution_reason"] == "load_confirmed"


def test_03_perdida_de_nexus_cola_en_orden_y_sin_desconexion_falsa(env):
    b = env["backend"]
    a = make_agent(env, "salud-nexus")
    iid = iid_of(a)
    tid = env["ids"]["t_cli"]["id"]
    task, wh = task_cfg(a, tid)
    hb = threading.Thread(target=a._heartbeat_loop, daemon=True)
    hb.start()
    try:
        assert wait_for(lambda: cfg_q("SELECT COUNT(*) FROM installation_heartbeat WHERE installation_id = %s",
                                      (iid,))[0][0] > 0)
        b.stop()
        t_down = time.time()
        stop_start(support.SRC)
        try:
            out1 = a.execute_task(task, wh)        # falla (queda en la cola)
        finally:
            up(support.SRC)
        out2 = a.execute_task(task, wh)            # éxito posterior (queda en la cola)
        assert out1["status"] == "failed" and out2["status"] == "success"
        assert a.flush(timeout=1) is False and a.state.depth >= 4
        # Nexus caído MÁS que el umbral de desconexión
        time.sleep(max(0.0, DISCONNECT + 3 - (time.time() - t_down)))
        b.start()
        for it in a.state.all_items():
            a.state._db.execute("UPDATE outbox SET next_attempt_at = 0 WHERE event_id = ?", (it.event_id,))
        assert a.flush(timeout=30)
        # La falla y la recuperación llegan EN ORDEN: la incidencia se abre y se resuelve
        inc = incidents(env, installation_id=iid, task_id=tid, category="task_failed")
        assert len(inc) == 1 and inc[0]["status"] == "resolved" and inc[0]["resolution_reason"] == "load_confirmed"
        assert inc[0]["resolved_execution_id"] == out2["execution_id"]
        # Tras reiniciar Nexus no se marca desconectado a un agente vivo (gracia + latido)
        time.sleep(DISCONNECT + 3)
        assert incidents(env, installation_id=iid, category="disconnected") == []
        ih = [i for i in b.admin("GET", "/admin/health/installations", expect=200)["items"] if i["id"] == iid][0]
        assert ih["connectivity"] == "online"
    finally:
        a.stop_event.set()
        hb.join(10)


def test_04_agente_muerto_nexus_detecta_desconexion_y_latido_no_cierra_errores(env):
    b = env["backend"]
    tid_bad = env["ids"]["t_bad"]["id"]
    b.admin("POST", f"/admin/tasks/{tid_bad}/enable", expect=200)
    ini = write_ini(env, "salud-proceso", token_kind="agency_token", token=env["ids"]["agency_a2"]["agency_token"],
                    extra={"run_all_on_start": "true"})
    cmd = [*support.agent_cmd(), "--config", ini]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env["procs"].append(p)
    try:
        row = wait_for(lambda: cfg_q("SELECT id FROM installation WHERE name = 'salud-proceso'"))
        iid = str(row[0][0])
        tf = wait_for(lambda: incidents(env, installation_id=iid, task_id=tid_bad, category="task_failed", status="open"))
        assert tf, "la tarea con SQL inválido no abrió incidencia"
        # Se mata el agente: no puede avisar de su propia desconexión; Nexus la detecta
        p.send_signal(signal.SIGKILL)
        p.wait(10)
        t_kill = time.time()
        d = wait_for(lambda: incidents(env, installation_id=iid, category="disconnected", status="open"),
                     timeout=DISCONNECT + 20)
        assert d, "Nexus no detectó la desconexión"
        assert time.time() - t_kill >= DISCONNECT - 2
        assert d[0]["severity"] == "critical"
        # Reconocer no la resuelve
        b.admin("PUT", f"/admin/incidents/{d[0]['id']}/ack", {"comment": "Llamando a la sede"}, expect=200)
        time.sleep(2)
        d2 = incidents(env, installation_id=iid, category="disconnected")[0]
        assert d2["status"] == "open" and d2["acknowledged"] is True
        # El agente vuelve: la desconexión se resuelve, el error de la tarea NO
        p2 = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env["procs"].append(p2)
        res = wait_for(lambda: [x for x in incidents(env, installation_id=iid, category="disconnected")
                                if x["status"] == "resolved"], timeout=30)
        assert res and res[0]["resolution_reason"] in ("heartbeat_recovered", "contact_recovered")
        still = incidents(env, installation_id=iid, task_id=tid_bad, category="task_failed")
        assert len(still) == 1 and still[0]["status"] == "open" and still[0]["id"] == tf[0]["id"]
        th = b.admin("GET", f"/admin/health/tasks?task_id={tid_bad}", expect=200)["items"][0]
        assert th["state"] == "failing" and th["current_error_code"]
        p2.send_signal(signal.SIGTERM)
        p2.wait(30)
    finally:
        b.admin("POST", f"/admin/tasks/{tid_bad}/disable", expect=200)
    # Tarea deshabilitada: sus incidencias se cierran con ese motivo explícito (no es recuperación)
    closed = wait_for(lambda: [x for x in incidents(env, task_id=tid_bad, category="task_failed")
                               if x["status"] == "resolved"], timeout=15)
    assert closed and closed[0]["resolution_reason"] == "task_disabled"


def test_05_tarea_larga_con_latidos_sin_desconexion(env):
    b, ids = env["backend"], env["ids"]
    n = uuid.uuid4().hex[:6]
    o = b.admin("POST", "/admin/objects", {"company_id": ids["company_a"]["id"], "name": f"Larga {n}",
                                             "destination_table": f"larga_{n}", "upsert_keys": "id"}, expect=201)
    # ~15 s de extracción (> umbral de desconexión de 10 s)
    t = b.admin("POST", "/admin/tasks", {
        "agency_id": ids["agency_a1"]["id"], "object_catalog_id": o["id"], "schedule_seconds": 3600,
        "extract_sql": "SELECT g AS id, pg_sleep(0.05) IS NULL AS x FROM generate_series(1, 300) g",
        "expected_duration_seconds": 120, "is_active": True}, expect=201)
    tid = t["id"]
    a = make_agent(env, "salud-larga")
    iid = iid_of(a)
    a.state.set_schedule(ids["t_cli"]["id"], next_due_at=time.time() + 99999)   # solo la tarea larga
    th = threading.Thread(target=a.run_forever, daemon=True)
    th.start()
    seen_running = False
    try:
        deadline = time.time() + 60
        done = False
        while time.time() < deadline:
            h = b.admin("GET", f"/admin/health/tasks?task_id={tid}", expect=200)["items"][0]
            if h["state"] == "running" and h["running_execution"]["confirmed_by_heartbeat"]:
                seen_running = True
            st = cfg_q("SELECT status FROM task_execution WHERE task_id = %s AND installation_id = %s", (tid, iid))
            if st and st[0][0] == "success":
                done = True
                break
            time.sleep(0.5)
        assert done, "la tarea larga no terminó"
        assert seen_running, "el panel nunca vio la tarea en curso confirmada por latido"
        started, finished = cfg_q("SELECT received_at, updated_at FROM task_execution WHERE task_id = %s", (tid,))[0]
        assert (finished - started).total_seconds() > DISCONNECT
        n_hb = cfg_q("""SELECT COUNT(*) FROM installation_heartbeat
                        WHERE installation_id = %s AND received_at BETWEEN %s AND %s""", (iid, started, finished))[0][0]
        assert n_hb >= 5
        assert incidents(env, installation_id=iid, category="disconnected") == []
        assert incidents(env, task_id=tid, category="task_running_long") == []
    finally:
        a.stop()
        th.join(30)
        b.admin("POST", f"/admin/tasks/{tid}/disable", expect=200)
