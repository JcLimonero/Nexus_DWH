"""
Integración real: backend (subproceso) + origen PostgreSQL + DWH PostgreSQL
(contenedores Docker locales) + agente en proceso o como subproceso.

Orden de ejecución relevante (los escenarios comparten el entorno del módulo).
"""

import glob
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import support
from nexus_agent.agent import Agent
from nexus_agent.api import ApiForbidden
from nexus_agent.logsetup import setup_logging
from nexus_agent.settings import load_settings

DBNAME = "nexus_test_cfg_client"
CLIENT_PY = os.path.join(support.CLIENT_DIR, ".venv", "bin", "python")


# ─────────────────────────────────────────────────────────────────────────────
# Entorno
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.ensure_container(support.SRC)
    support.ensure_container(support.DWH)
    support.seed_source()
    support.reset_dwh()
    support.recreate_config_db(DBNAME)
    b = support.Backend(DBNAME)
    b.start()
    base = tmp_path_factory.mktemp("agents")
    try:
        ids = support.seed_config(b)
        yield {"backend": b, "ids": ids, "base": base, "agents": []}
    finally:
        for a in []:
            pass
        b.stop()
        support.ensure_container(support.SRC)
        support.ensure_container(support.DWH)
        support.drop_config_db(DBNAME)


def write_ini(env, name, token_kind="group_token", token=None, extra=None):
    d = env["base"] / name
    d.mkdir(exist_ok=True)
    token = token or env["ids"]["group_a"]["group_token"]
    agent = {"data_dir": str(d / "data"), "log_dir": str(d / "logs"), "heartbeat_seconds": "5",
             "tick_seconds": "0.2", "fetch_chunk_rows": "100", "watermark_overlap_seconds": "120",
             "shutdown_grace_seconds": "5", "task_retry_attempts": "0", "db_connect_timeout_seconds": "3",
             "queue_backoff_base_seconds": "0.2", "queue_backoff_max_seconds": "1"}
    agent.update(extra or {})
    lines = ["[nexus]", f"api_url = {env['backend'].url}", f"{token_kind} = {token}", "mode = development", "",
             "[agent]"] + [f"{k} = {v}" for k, v in agent.items()]
    path = d / "config.ini"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def make_agent(env, name, **kw):
    path = write_ini(env, name, **kw)
    s = load_settings(path)
    setup_logging(s.log_dir, console=False)
    a = Agent(s)
    a.ensure_credential()
    env["agents"].append(a)
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


def dwh_q(sql, params=()):
    c = support.pg(support.DWH)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def src_exec(sql, params=()):
    c = support.pg(support.SRC)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
    finally:
        c.close()


def set_active(env, task_key, active):
    b = env["backend"]
    tid = env["ids"][task_key]["id"]
    b.admin("POST", f"/admin/tasks/{tid}/{'enable' if active else 'disable'}", expect=200)
    return tid


# ─────────────────────────────────────────────────────────────────────────────
# Escenarios
# ─────────────────────────────────────────────────────────────────────────────
def test_01_enrolar_ejecutar_y_verificar(env):
    a = make_agent(env, "principal")
    cred_path = a.store.path
    assert os.path.exists(cred_path) and oct(os.stat(cred_path).st_mode)[-3:] == "600"
    tid = env["ids"]["t_cli"]["id"]
    task, wh = task_cfg(a, tid)
    out = a.execute_task(task, wh)
    assert out["status"] == "success", out
    res = out["result"]
    assert (res.rows_read, res.rows_loaded, res.rows_inserted, res.rows_updated) == (500, 500, 500, 0)
    assert a.flush(timeout=20)
    assert dwh_q("SELECT COUNT(*), MIN(dn), MAX(dn) FROM dwh.clientes")[0] == (500, "1000", "1000")
    ex = cfg_q("SELECT status, rows_read, rows_loaded, rows_inserted, rows_updated, checkpoint_kind, "
               "group_id, company_id, agency_id FROM task_execution WHERE execution_id = %s", (out["execution_id"],))[0]
    ids = env["ids"]
    assert ex == ("success", 500, 500, 500, 0, "source_clock", ids["group_a"]["id"], ids["company_a"]["id"],
                  ids["agency_a1"]["id"])
    sync = cfg_q("SELECT watermark, last_status, consecutive_failures FROM task_sync_state WHERE task_id = %s", (tid,))[0]
    assert sync[0] is not None and sync[1] == "success" and sync[2] == 0

    # 2ª corrida: nada nuevo (watermark = inicio de la extracción anterior)
    task, wh = task_cfg(a, tid)
    out2 = a.execute_task(task, wh)
    assert out2["status"] == "success" and out2["result"].rows_read == 0
    # 3ª: 3 filas modificadas en origen → 3 actualizadas (conteo xmax)
    src_exec("UPDATE src_clientes SET nombre = nombre || ' (mod)', updated_at = LOCALTIMESTAMP WHERE id <= 3")
    task, wh = task_cfg(a, tid)
    out3 = a.execute_task(task, wh)
    r3 = out3["result"]
    assert (r3.rows_read, r3.rows_inserted, r3.rows_updated) == (3, 0, 3)
    assert a.flush(timeout=20)
    assert dwh_q("SELECT COUNT(*) FROM dwh.clientes")[0][0] == 500
    assert cfg_q("SELECT COUNT(*) FROM client_events WHERE task_id = %s AND source = 'agent'", (tid,))[0][0] == 3


def test_02_tarea_de_otro_grupo_es_rechazada(env):
    a = env["agents"][0]
    payload = {"execution_id": "5f0c7d3e-6b5b-4c55-9d7c-1a2b3c4d5e6f", "task_id": env["ids"]["t_b"]["id"],
               "attempt": 1, "started_at": "2030-01-01T00:00:00Z", "agent_seq": 99999}
    with pytest.raises(ApiForbidden) as e:
        a.api.start_execution(payload)
    assert e.value.code == "task_out_of_scope"
    tasks = {t["task_id"] for t in a.fresh_config()["tasks"]}
    assert env["ids"]["t_b"]["id"] not in tasks


def test_03_origen_caido_y_recuperado(env):
    a = env["agents"][0]
    tid = env["ids"]["t_cli"]["id"]
    wm_before = cfg_q("SELECT watermark FROM task_sync_state WHERE task_id = %s", (tid,))[0][0]
    task, wh = task_cfg(a, tid)
    support.docker("stop", "-t", "2", support.SRC["container"])
    try:
        out = a.execute_task(task, wh)
    finally:
        support.docker("start", support.SRC["container"])
        support.wait_pg(support.SRC)
    assert out["status"] == "failed" and out["stage"] == "extract"
    assert out["error_code"].startswith("SOURCE_"), out
    assert a.flush(timeout=20)
    assert cfg_q("SELECT watermark FROM task_sync_state WHERE task_id = %s", (tid,))[0][0] == wm_before
    task, wh = task_cfg(a, tid)
    assert a.execute_task(task, wh)["status"] == "success"


def test_04_dwh_caido_y_recuperado(env):
    a = env["agents"][0]
    tid = env["ids"]["t_cli"]["id"]
    assert a.flush(timeout=20)
    wm_before = cfg_q("SELECT watermark FROM task_sync_state WHERE task_id = %s", (tid,))[0][0]
    src_exec("UPDATE src_clientes SET updated_at = LOCALTIMESTAMP WHERE id BETWEEN 10 AND 14")
    task, wh = task_cfg(a, tid)
    support.docker("stop", "-t", "2", support.DWH["container"])
    try:
        out = a.execute_task(task, wh)
    finally:
        support.docker("start", support.DWH["container"])
        support.wait_pg(support.DWH)
    assert out["status"] == "failed" and out["stage"] == "load" and out["error_code"].startswith("DWH_"), out
    assert a.flush(timeout=20)
    assert cfg_q("SELECT watermark FROM task_sync_state WHERE task_id = %s", (tid,))[0][0] == wm_before
    task, wh = task_cfg(a, tid)
    out = a.execute_task(task, wh)
    assert out["status"] == "success" and out["result"].rows_read >= 5  # la ventana fallida se recarga
    assert a.flush(timeout=20)
    assert cfg_q("SELECT consecutive_failures, last_status FROM task_sync_state WHERE task_id = %s", (tid,))[0] == (0, "success")


def test_05_nexus_caido_cola_crece_y_se_vacia_sin_duplicados(env):
    a = env["agents"][0]
    b = env["backend"]
    tid = env["ids"]["t_cli"]["id"]
    task, wh = task_cfg(a, tid)
    src_exec("UPDATE src_clientes SET updated_at = LOCALTIMESTAMP WHERE id BETWEEN 20 AND 21")
    n_exec_before = cfg_q("SELECT COUNT(*) FROM task_execution WHERE task_id = %s", (tid,))[0][0]
    a.settings.watermark_overlap_seconds = 0   # conteos exactos (el solapamiento se prueba aparte)
    b.stop()
    try:
        out1 = a.execute_task(task, wh)       # la config sigue vigente en memoria
        assert out1["status"] == "success" and out1["result"].rows_read == 2
        assert a.flush(timeout=2) is False
        assert a.state.depth >= 2 and len(a.state.unconfirmed_checkpoints()) == 1
        # Carga confirmada en DWH pero NO en Nexus: la siguiente corrida usa el checkpoint local (no recarga)
        out2 = a.execute_task(task, wh)
        assert out2["status"] == "success" and out2["result"].rows_read == 0
        assert a.state.depth >= 4
        assert a.refresh_config() is False    # sin autorización nueva...
    finally:
        b.start()
        a.settings.watermark_overlap_seconds = 120
    for it in a.state.all_items():            # sin esperar el backoff acumulado
        a.state._db.execute("UPDATE outbox SET next_attempt_at = 0 WHERE event_id = ?", (it.event_id,))
    assert a.flush(timeout=30)
    assert a.state.depth == 0 and a.state.unconfirmed_checkpoints() == []
    for ex in (out1["execution_id"], out2["execution_id"]):
        assert cfg_q("SELECT COUNT(*) FROM task_execution WHERE execution_id = %s", (ex,))[0][0] == 1
        assert cfg_q("SELECT COUNT(*) FROM client_events WHERE execution_id = %s", (ex,))[0][0] == 1
    assert cfg_q("SELECT COUNT(*) FROM task_execution WHERE task_id = %s", (tid,))[0][0] == n_exec_before + 2
    # reenvío duplicado del mismo reporte: el servidor lo ignora
    assert dwh_q("SELECT COUNT(*) FROM dwh.clientes")[0][0] == 500


def test_06_config_caducada_no_inicia_tareas(env):
    a = env["agents"][0]
    a.refresh_config()
    a._config_at -= 10_000
    assert a.fresh_config() is None and a._config is None
    assert a.due_tasks({"tasks": []}) == []
    assert a.refresh_config() and a.fresh_config() is not None


def test_07_violacion_de_restriccion_saneada_y_sin_carga_parcial(env):
    a = env["agents"][0]
    tid = set_active(env, "t_dups", True)
    try:
        task, wh = task_cfg(a, tid)
        out = a.execute_task(task, wh)
    finally:
        set_active(env, "t_dups", False)
    assert out["status"] == "failed" and out["stage"] == "load"
    assert out["error_code"] == "DWH_CONSTRAINT_VIOLATION", out
    assert dwh_q("SELECT COUNT(*) FROM dups")[0][0] == 0      # ROLLBACK: ni la primera fila
    assert a.flush(timeout=20)
    msg = cfg_q("SELECT error_message_sanitized FROM task_execution WHERE execution_id = %s", (out["execution_id"],))[0][0]
    assert support.ROW_SECRET not in msg and "IntegrityError" not in msg or "UniqueViolation" in msg
    assert support.ROW_SECRET not in msg


def test_08_sql_invalido_saneado(env):
    b = env["backend"]
    a = make_agent(env, "norte", token_kind="agency_token", token=env["ids"]["agency_a2"]["agency_token"])
    tid = set_active(env, "t_bad", True)
    try:
        task, wh = task_cfg(a, tid)
        out = a.execute_task(task, wh)
    finally:
        set_active(env, "t_bad", False)
    assert out["status"] == "failed" and out["stage"] == "extract" and out["error_code"] == "SOURCE_SQL_ERROR"
    assert a.flush(timeout=20)
    msg = cfg_q("SELECT error_message_sanitized FROM task_execution WHERE execution_id = %s", (out["execution_id"],))[0][0]
    assert support.SQL_MARKER not in msg and "tabla_inexistente_xyz" in msg


def test_09_sin_claves_upsert_avisa_duplicados(env):
    a = env["agents"][-1]
    tid = set_active(env, "t_sin", True)
    try:
        task, wh = task_cfg(a, tid)
        out = a.execute_task(task, wh)
        out2 = a.execute_task(task, wh)
    finally:
        set_active(env, "t_sin", False)
    assert "NO_UPSERT_KEYS_DUPLICATES_POSSIBLE" in out["result"].warnings
    assert dwh_q("SELECT COUNT(*) FROM sin_llave")[0][0] == 10   # documentado: sin claves, reintentar duplica
    assert a.flush(timeout=20)
    w = cfg_q("SELECT warnings FROM task_execution WHERE execution_id = %s", (out2["execution_id"],))[0][0]
    assert "NO_UPSERT_KEYS_DUPLICATES_POSSIBLE" in w


def test_10_heartbeat_sigue_durante_tarea_larga(env):
    a = make_agent(env, "latido", extra={"heartbeat_seconds": "5"})
    a.settings.heartbeat_seconds = 1
    iid = a.holder.get().installation_id
    tid = set_active(env, "t_lenta", True)
    for t in (env["ids"]["t_cli"]["id"],):
        a.state.set_schedule(t, next_due_at=time.time() + 99999)   # solo la tarea lenta
    th = threading.Thread(target=a.run_forever, daemon=True)
    th.start()
    try:
        deadline = time.time() + 40
        exec_id = None
        while time.time() < deadline:
            rows = cfg_q("SELECT execution_id, status FROM task_execution WHERE task_id = %s AND installation_id = %s",
                         (tid, iid))
            if rows and rows[0][1] == "success":
                exec_id = str(rows[0][0])
                break
            time.sleep(0.5)
        assert exec_id, "la tarea lenta no terminó"
        hb = cfg_q("SELECT running FROM installation_heartbeat WHERE installation_id = %s ORDER BY id", (iid,))
        during = [r for r in hb if any(x.get("execution_id") == exec_id for x in r[0])]
        assert len(during) >= 3, f"latidos durante la tarea: {len(during)} de {len(hb)}"
    finally:
        a.stop()
        th.join(30)
        set_active(env, "t_lenta", False)
    assert dwh_q("SELECT COUNT(*) FROM lenta")[0][0] == 400


def _run_cli(ini, *args, wait=True):
    cmd = [CLIENT_PY, os.path.join(support.CLIENT_DIR, "client_postgres.py"), "--config", ini, *args]
    if wait:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_11_reinicio_a_mitad_de_carga(env):
    support.docker  # noqa
    dwh_q_drop = support.pg(support.DWH)
    with dwh_q_drop.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS lenta")
    dwh_q_drop.close()
    tid = set_active(env, "t_lenta", True)
    ini = write_ini(env, "proceso", token_kind="agency_token", token=env["ids"]["agency_a1"]["agency_token"],
                    extra={"fetch_chunk_rows": "100", "run_all_on_start": "true"})
    logdir = os.path.join(os.path.dirname(ini), "logs")
    try:
        wm_before = cfg_q("SELECT watermark FROM task_sync_state WHERE task_id = %s", (tid,))
        # El primer --once enrola y ejecuta; lo matamos (SIGKILL) a mitad de la carga
        p = _run_cli(ini, "--once", wait=False)
        deadline = time.time() + 60
        seen = False
        while time.time() < deadline and not seen:
            for f in glob.glob(os.path.join(logdir, "nexus_agent.log*")):
                if "Tarea %d: 200 filas en transacci" % tid in open(f, encoding="utf-8").read():
                    seen = True
            time.sleep(0.2)
        assert seen, "no se alcanzó la carga"
        p.send_signal(signal.SIGKILL)
        p.wait(10)
        assert dwh_q("SELECT COUNT(*) FROM lenta")[0][0] == 0            # nada confirmado
        assert cfg_q("SELECT watermark FROM task_sync_state WHERE task_id = %s", (tid,)) == wm_before
        # Re-ejecución: detecta la ejecución huérfana, la reporta interrumpida y recarga completo
        r = _run_cli(ini, "--once")
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        assert dwh_q("SELECT COUNT(*) FROM lenta")[0][0] == 400
        st = cfg_q("SELECT status, error_code FROM task_execution WHERE task_id = %s ORDER BY received_at", (tid,))
        statuses = [s for s, _ in st]
        assert ("interrupted", "AGENT_RESTARTED") in st and statuses.count("success") >= 1
        # Idempotente: otra corrida no duplica
        data = os.path.join(os.path.dirname(ini), "data", "agent_state.db")
        c = sqlite3.connect(data)
        c.execute("UPDATE task_schedule SET next_due_at = 0")
        c.commit()
        c.close()
        r = _run_cli(ini, "--once")
        assert r.returncode == 0
        assert dwh_q("SELECT COUNT(*) FROM lenta")[0][0] == 400
    finally:
        set_active(env, "t_lenta", False)


def test_12_credencial_revocada(env):
    a = make_agent(env, "revocada")
    iid = a.holder.get().installation_id
    env["backend"].admin("POST", f"/admin/installations/{iid}/revoke", {"reason": "prueba"}, expect=200)
    assert a.refresh_config() is False
    assert a.fatal is not None and a.fatal.exit_code == 3 and a.stop_event.is_set()
    ini = write_ini(env, "revocada")
    r = _run_cli(ini, "--once")
    assert r.returncode == 3


def test_13_monitor_ve_instalaciones(env):
    mon = env["backend"].monitor("/monitor/installations")
    names = {i["name"] for i in mon["installations"]}
    assert len(mon["installations"]) >= 4
    assert any(i["scope_type"] == "agency" for i in mon["installations"])
    assert any(i["status"] == "revoked" for i in mon["installations"])


def test_99_sin_sql_ni_secretos_en_logs_bd_y_cola(env):
    ids = env["ids"]
    secrets_ = [support.SQL_MARKER, support.ROW_SECRET, support.SRC["password"], support.DWH["password"],
                ids["group_a"]["group_token"], ids["agency_a1"]["agency_token"], ids["agency_a2"]["agency_token"],
                "pass-b-secreta", "src-pass-b-secreta"]
    for a in env["agents"]:
        cred = a.holder.get()
        if cred:
            secrets_.append(cred.secret)
    for f in glob.glob(os.path.join(str(env["base"]), "*", "data", "agent_credential.json")):
        secrets_.append(json.load(open(f))["secret"])
    # 1) logs de agentes y backend
    files = glob.glob(os.path.join(str(env["base"]), "*", "logs", "nexus_agent.log*")) + [env["backend"].log_path]
    assert len(files) >= 3
    for f in files:
        text = open(f, encoding="utf-8", errors="replace").read()
        for s in secrets_:
            assert s not in text, f"{s[:6]}… encontrado en {os.path.basename(f)}"
    # 2) tablas de reportes / auditoría en Nexus
    blob = cfg_q("""
        SELECT COALESCE(string_agg(COALESCE(detail,'') || token, ' '), '') FROM client_events
        UNION ALL SELECT COALESCE(string_agg(COALESCE(error_message_sanitized,'') || warnings::text, ' '), '') FROM task_execution
        UNION ALL SELECT COALESCE(string_agg(COALESCE(error_detail,'') || token, ' '), '') FROM activity_log
        UNION ALL SELECT COALESCE(string_agg(payload::text, ' '), '') FROM agent_event
        UNION ALL SELECT COALESCE(string_agg(fingerprint::text || COALESCE(last_heartbeat::text,''), ' '), '') FROM installation
    """)
    text = " ".join(r[0] for r in blob)
    for s in secrets_:
        assert s not in text, f"{s[:6]}… encontrado en BD de Nexus"
    # 3) SQLite local (cola / dead-letter / agenda) de cada agente
    for dbf in glob.glob(os.path.join(str(env["base"]), "*", "data", "agent_state.db")):
        c = sqlite3.connect(dbf)
        dump = "\n".join(c.iterdump())
        c.close()
        for s in secrets_:
            assert s not in dump, f"{s[:6]}… encontrado en {dbf}"
