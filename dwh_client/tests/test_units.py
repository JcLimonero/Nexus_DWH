"""Pruebas unitarias del agente (sin red ni BD)."""

import json
import logging
import os
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

from nexus_agent import agent as agent_mod
from nexus_agent.agent import Agent, FatalAgentError
from nexus_agent.api import ApiRejected, ApiUnavailable, CredentialHolder, NexusApi, validate_api_url
from nexus_agent.credstore import CredentialStore, InstallationCredential
from nexus_agent.etl import (
    TaskResult,
    build_load_sql,
    capture_watermark,
    effective_watermark_for_extract,
    prepare_extract_sql,
    quote_ident,
    quote_table,
)
from nexus_agent.localstate import LocalState
from nexus_agent.sanitize import (
    SECRETS,
    Cancelled,
    ConfigError,
    RedactingFormatter,
    StageError,
    redact_text,
    sanitize_error,
)
from nexus_agent.settings import Settings


# ─────────────────────────────────────────────────────────────────────────────
# sanitize_error
# ─────────────────────────────────────────────────────────────────────────────
def _fake_exc(module, name, msg, **attrs):
    cls = type(name, (Exception,), {"__module__": module})
    exc = cls(msg)
    for k, v in attrs.items():
        setattr(exc, k, v)
    return exc


def test_sanitize_quita_valores_de_fila_y_sql():
    exc = _fake_exc("psycopg2.errors", "UniqueViolation",
                    'duplicate key value violates unique constraint "dups_email_key"\n'
                    "DETAIL:  Key (email)=(juan.perez@correo.com) already exists.\n",
                    pgcode="23505")
    code, msg = sanitize_error(StageError("load", exc))
    assert code == "DWH_CONSTRAINT_VIOLATION"
    assert "juan.perez" not in msg and "UniqueViolation" in msg

    exc = _fake_exc("psycopg2.errors", "UndefinedTable",
                    'relation "t_x" does not exist\nLINE 1: SELECT secreto_col FROM t_x WHERE a = 1\n        ^\n',
                    pgcode="42P01")
    code, msg = sanitize_error(StageError("extract", exc))
    assert code == "SOURCE_SQL_ERROR"
    assert "secreto_col" not in msg and "LINE" not in msg


def test_sanitize_quita_conexion_hosts_usuarios_y_secretos_registrados():
    SECRETS.add("Sup3rS3cret!")
    exc = _fake_exc("psycopg2", "OperationalError",
                    'connection to server at "db.interno.local" (10.20.30.40), port 5432 failed: '
                    'FATAL:  password authentication failed for user "dwh_admin"\n'
                    "dsn: host=db.interno.local user=dwh_admin password=Sup3rS3cret! dbname=dwh")
    code, msg = sanitize_error(StageError("load", exc))
    assert code == "DWH_AUTH_FAILED"
    for s in ("db.interno.local", "10.20.30.40", "dwh_admin", "Sup3rS3cret!", "5432"):
        assert s not in msg, s
    odbc = _fake_exc("pyodbc", "OperationalError", "x")
    odbc.args = ("HYT00", "[HYT00] [Microsoft][ODBC Driver 17] Query timeout expired (0) SERVER=10.0.0.9;UID=sa;PWD=abc123")
    code, msg = sanitize_error(StageError("extract", odbc))
    assert code == "SOURCE_QUERY_TIMEOUT"
    assert "10.0.0.9" not in msg and "abc123" not in msg and "UID=sa" not in msg


def test_sanitize_longitud_y_cancelacion():
    code, msg = sanitize_error(RuntimeError("x" * 5000))
    assert len(msg) <= 500 and code == "UNEXPECTED_ERROR"
    assert sanitize_error(Cancelled("apagado"))[0] == "CANCELLED"
    assert sanitize_error(StageError("config", ConfigError("sin host")))[0] == "CONFIG_ERROR"


def test_redacting_formatter_limpia_logs():
    SECRETS.add("token-super-secreto-123")
    rec = logging.LogRecord("nexus", logging.ERROR, __file__, 1,
                            "falló token-super-secreto-123 con SELECT a, b FROM clientes WHERE x = 1", (), None)
    out = RedactingFormatter("%(message)s").format(rec)
    assert "token-super-secreto-123" not in out and "clientes" not in out and "[SQL omitido]" in out


def test_redact_text_url_con_credenciales():
    s = redact_text("postgresql://user:pw@host:5432/db falló")
    assert "pw" not in s and "user:" not in s


# ─────────────────────────────────────────────────────────────────────────────
# Cola local
# ─────────────────────────────────────────────────────────────────────────────
def _state(tmp_path, **kw):
    kw.setdefault("rng", lambda a, b: b)
    return LocalState(str(tmp_path / "state.db"), **kw)


def test_cola_seq_monotonica_y_persistencia_tras_reinicio(tmp_path):
    st = _state(tmp_path)
    ids = [st.enqueue("agent_event", {"event_type": "warning", "payload": {}})[1] for _ in range(3)]
    assert ids == [1, 2, 3]
    st.close()
    st2 = _state(tmp_path)
    assert st2.depth == 3 and st2.current_seq() == 3
    assert st2.enqueue("agent_event", {"event_type": "warning", "payload": {}})[1] == 4
    assert [i.seq for i in st2.all_items()] == [1, 2, 3, 4]
    assert st2.head().seq == 1


def test_cola_rechaza_payload_con_datos_sensibles(tmp_path):
    st = _state(tmp_path)
    for bad in ({"extract_sql": "SELECT 1"}, {"payload": {"password": "x"}}, {"x": [{"token": "t"}]}):
        with pytest.raises(ValueError):
            st.enqueue("agent_event", bad)
    assert st.depth == 0


def test_cola_backoff_exponencial_con_tope_y_jitter(tmp_path):
    now = [1000.0]
    st = _state(tmp_path, backoff_base=2, backoff_max=30, clock=lambda: now[0], rng=lambda a, b: b)
    assert [st.backoff_delay(n) for n in range(1, 7)] == [2, 4, 8, 16, 30, 30]
    st_j = _state(tmp_path, backoff_base=2, backoff_max=30, rng=lambda a, b: a)
    assert st_j.backoff_delay(3) == 4  # jitter: entre la mitad y el tope
    st.enqueue("agent_event", {"event_type": "warning", "payload": {}})
    item = st.head()
    nxt = st.defer(item, "network_error")
    assert nxt == 1002 and st.head().attempts == 1


def test_cola_saturacion_nunca_descarta_fallos_en_silencio(tmp_path):
    st = _state(tmp_path, max_items=4)
    st.enqueue("execution_start", {"x": 1}, execution_id="e1", task_id=1)
    st.enqueue("execution_update", {"status": "success"}, is_success=True, execution_id="e1", task_id=1)
    st.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="e2", task_id=1)
    st.enqueue("agent_event", {"event_type": "agent_started", "payload": {}})
    # 5º: primero se compacta el inicio cuya ejecución ya tiene fin
    st.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="e3", task_id=1)
    types = [(i.type, i.execution_id) for i in st.all_items()]
    assert ("execution_start", "e1") not in types
    # 6º: luego eventos informativos; 7º: luego éxitos
    st.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="e4", task_id=1)
    st.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="e5", task_id=1)
    items = st.all_items()
    assert all(i.critical for i in items) and len(items) == 4
    # Llena de fallos: un éxito nuevo se rechaza (contado), un fallo nuevo se admite hasta 2×
    ev, _ = st.enqueue("execution_update", {"status": "success"}, is_success=True, execution_id="e6", task_id=2,
                       checkpoint=("2030-01-01T00:00:00.000000", "source_clock", 1.0))
    assert ev == "" and st.local_watermark(2)[0] == "2030-01-01T00:00:00.000000"
    for n in range(10):
        st.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id=f"f{n}", task_id=1)
    assert st.depth == 8  # tope duro = 2 × max_items (se descartan los fallos más viejos, contados)
    c = st.overflow_counters()
    assert c["overflow_dropped_start"] == 1 and c["overflow_dropped_info"] == 1
    assert c["overflow_dropped_success"] == 1 and c["overflow_dropped_rejected_new"] == 1
    assert c["overflow_dropped_failure"] >= 1
    rep = st.take_overflow_report(min_interval=0)
    assert rep and rep["overflow_total"] == c["overflow_total"]
    assert st.take_overflow_report(min_interval=0) is None  # ya reportado
    assert st.overflow_total_cached == c["overflow_total"]


def test_cola_retencion_purga_contada(tmp_path):
    now = [10_000_000.0]
    st = _state(tmp_path, retention_days=1, clock=lambda: now[0])
    st.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="old", task_id=1)
    now[0] += 2 * 86400
    st.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="new", task_id=1)
    assert st.purge_expired() == 1
    assert [i.execution_id for i in st.all_items()] == ["new"]
    assert st.overflow_counters()["overflow_dropped_expired"] == 1


def test_checkpoints_pendientes_confirmacion_y_reset(tmp_path):
    st = _state(tmp_path)
    st.enqueue("execution_update", {"status": "success"}, is_success=True, execution_id="a", task_id=7,
               checkpoint=("2030-01-01T00:00:00.000000", "source_clock", 100.0))
    st.enqueue("execution_update", {"status": "success"}, is_success=True, execution_id="b", task_id=7,
               checkpoint=("2030-01-02T00:00:00.000000", "source_clock", 200.0))
    assert st.local_watermark(7)[0] == "2030-01-02T00:00:00.000000"
    assert len(st.unconfirmed_checkpoints()) == 2
    for it in st.all_items():
        st.mark_sent(it)
    assert st.unconfirmed_checkpoints() == []
    assert st.local_watermark(7)[0] == "2030-01-02T00:00:00.000000"  # queda el último confirmado
    assert st.local_watermark(7, reset_after_epoch=300.0) is None      # reinicio posterior en el servidor
    st.prune_checkpoints(7, "2030-01-03T00:00:00.000000")
    assert st.local_watermark(7) is None


# ─────────────────────────────────────────────────────────────────────────────
# Credenciales
# ─────────────────────────────────────────────────────────────────────────────
def test_credencial_archivo_0600(tmp_path):
    cs = CredentialStore(str(tmp_path), force_backend="file")
    cred = InstallationCredential("id-1", "secreto-xyz", "https://nexus.example")
    cs.save(cred)
    assert stat.S_IMODE(os.stat(cs.path).st_mode) == 0o600
    assert cs.load() == cred
    os.chmod(cs.path, 0o644)
    cs.load()
    assert stat.S_IMODE(os.stat(cs.path).st_mode) == 0o600


def test_credencial_dpapi_simulado(tmp_path):
    calls = []

    def protect(data, machine):
        calls.append(("p", machine))
        return bytes(b ^ 0x5A for b in data)

    def unprotect(data, machine):
        calls.append(("u", machine))
        return bytes(b ^ 0x5A for b in data)

    cs = CredentialStore(str(tmp_path), scope="machine", protect=protect, unprotect=unprotect, force_backend="dpapi")
    cred = InstallationCredential("id-2", "secreto-dpapi", "https://nexus.example")
    cs.save(cred)
    raw = open(cs.path, "rb").read()
    assert b"secreto-dpapi" not in raw and cs.path.endswith(".dpapi")
    assert cs.load() == cred and calls == [("p", True), ("u", True)]


# ─────────────────────────────────────────────────────────────────────────────
# URL / TLS
# ─────────────────────────────────────────────────────────────────────────────
def test_validacion_url_y_tls(tmp_path):
    ok_local = Settings(api_url="http://127.0.0.1:8000")
    validate_api_url(ok_local)
    with pytest.raises(ConfigError):
        validate_api_url(Settings(api_url="http://nexus.example.com", mode="production", allow_insecure_http=True))
    with pytest.raises(ConfigError):
        validate_api_url(Settings(api_url="http://nexus.example.com", mode="development"))
    validate_api_url(Settings(api_url="http://nexus.example.com", mode="development", allow_insecure_http=True))
    with pytest.raises(ConfigError):
        validate_api_url(Settings(api_url="https://nexus.example.com", ca_bundle=str(tmp_path / "no.pem")))
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    api = NexusApi(Settings(api_url="https://nexus.example.com", ca_bundle=str(ca)), CredentialHolder())
    assert api.session.verify == str(ca)
    api = NexusApi(Settings(api_url="https://nexus.example.com"), CredentialHolder())
    assert api.session.verify is True


# ─────────────────────────────────────────────────────────────────────────────
# Watermark / SQL
# ─────────────────────────────────────────────────────────────────────────────
def test_watermark_solapamiento_solo_con_claves():
    wm = datetime(2030, 1, 1, 12, 0, 0)
    assert effective_watermark_for_extract(wm, 120, True) == wm - timedelta(seconds=120)
    assert effective_watermark_for_extract(wm, 120, False) == wm
    assert effective_watermark_for_extract(None, 120, True) is None
    sql = prepare_extract_sql("SELECT * FROM t WHERE u >= '{last_run}'", "2030-01-01T11:58:00.000000")
    assert sql.endswith("'2030-01-01 11:58:00.000'")
    assert "1900-01-01" in prepare_extract_sql("x >= '{last_run}'", None)


def test_capture_watermark_fallback_si_no_hay_reloj_de_origen():
    class BadConn:
        def cursor(self, *a):
            raise RuntimeError("sin reloj")

        def rollback(self):
            pass

    warnings = []
    s = Settings(watermark_clock="source")
    dt, kind = capture_watermark(BadConn(), "postgresql", s, warnings)
    assert kind == "agent_local" and "SOURCE_CLOCK_UNAVAILABLE" in warnings
    dt, kind = capture_watermark(BadConn(), "postgresql", Settings(watermark_clock="agent_utc"), [])
    assert kind == "agent_utc"


def test_sql_de_carga_y_escape_de_identificadores():
    sql, mode = build_load_sql("dwh.t", ["id", "v"], ["id"])
    assert mode == "upsert" and sql.endswith("RETURNING (xmax = 0)") and '"dwh"."t"' in sql
    assert build_load_sql("t", ["id"], ["id"])[1] == "nothing"
    assert build_load_sql("t", ["id", "v"], [])[1] == "insert"
    assert quote_ident('a"b') == '"a""b"' and quote_table("x") == '"public"."x"'


# ─────────────────────────────────────────────────────────────────────────────
# Agente: orden de envío, agenda, reintentos, credencial ligada a la URL
# ─────────────────────────────────────────────────────────────────────────────
class FakeApi:
    def __init__(self, settings, holder):
        self.calls = []
        self.fail_next = 0
        self.reject = False

    def _call(self, name, payload):
        if self.fail_next:
            self.fail_next -= 1
            raise ApiUnavailable(0, "network_error", "caído")
        if self.reject:
            raise ApiRejected(422, "bad", "rechazado")
        self.calls.append((name, payload.get("agent_seq")))
        return {"status": "ok"}

    def start_execution(self, p):
        return self._call("start", p)

    def update_execution(self, eid, p):
        return self._call("update", p)

    def send_event(self, p):
        return self._call("event", p)

    def close(self):
        pass


def _agent(tmp_path, **kw):
    s = Settings(api_url="http://127.0.0.1:9", data_dir=str(tmp_path), task_retry_attempts=2,
                 task_retry_backoff_seconds=60, **kw)
    now = [1_000_000.0]
    st = LocalState(str(tmp_path / "s.db"), clock=lambda: now[0], rng=lambda a, b: b, backoff_base=2)
    a = Agent(s, state=st, api_factory=FakeApi, clock=lambda: now[0],
              store=CredentialStore(str(tmp_path), force_backend="file"))
    return a, now


def test_envio_en_orden_estricto_con_reintentos(tmp_path):
    a, now = _agent(tmp_path)
    a.state.enqueue("execution_start", {"x": 1}, execution_id="e1", task_id=1)
    a.state.enqueue("execution_update", {"execution_id": "e1", "status": "success"}, execution_id="e1", task_id=1)
    a.api.fail_next = 1
    assert a.send_one(a.api) == "deferred"
    assert a.send_one(a.api) == "wait"        # no se salta al segundo reporte
    now[0] += 3
    assert a.send_one(a.api) == "sent"
    assert a.send_one(a.api) == "sent"
    assert [c[0] for c in a.api.calls] == ["start", "update"]
    assert [c[1] for c in a.api.calls] == [1, 2]
    a.api.reject = True
    a.state.enqueue("agent_event", {"event_type": "warning", "payload": {}})
    assert a.send_one(a.api) == "dead" and a.state.dead_letter_count() == 1 and a.state.depth == 0


def test_agenda_persistente_no_dispara_todo_al_reiniciar(tmp_path):
    a, now = _agent(tmp_path)
    recent = datetime.fromtimestamp(now[0] - 600, timezone.utc).isoformat()
    cfg = {"tasks": [
        {"task_id": 1, "schedule_seconds": 3600, "sync": {"last_success_at": recent}},
        {"task_id": 2, "schedule_seconds": 3600, "sync": {}},
    ]}
    assert [t["task_id"] for t in a.due_tasks(cfg)] == [2]           # 1 corrió hace 10 min
    now[0] += 3000
    assert sorted(t["task_id"] for t in a.due_tasks(cfg)) == [1, 2]
    a.state.set_schedule(2, next_due_at=now[0] + 500, last_started_at=now[0])
    # "reinicio": nueva instancia sobre el mismo estado
    a2 = Agent(a.settings, state=a.state, api_factory=FakeApi, clock=lambda: now[0],
               store=CredentialStore(str(tmp_path), force_backend="file"))
    assert 2 not in [t["task_id"] for t in a2.due_tasks(cfg)]
    cfg["tasks"][1]["schedule_seconds"] = 60                         # cambio de schedule → se recalcula
    assert 2 in [t["task_id"] for t in a2.due_tasks(cfg, now=now[0] + 61)]
    os.makedirs(tmp_path / "x")
    a3, _ = _agent(tmp_path / "x", run_all_on_start=True)   # opción: ejecutar todo al arrancar
    assert sorted(t["task_id"] for t in a3.due_tasks(cfg)) == [1, 2]


def test_reintentos_con_nuevo_execution_id_y_attempt(tmp_path, monkeypatch):
    a, now = _agent(tmp_path)
    calls = []

    def boom(task, wh, prev, settings, ctx, **kw):
        calls.append(prev)
        raise StageError("extract", _fake_exc("psycopg2", "OperationalError", "could not connect host=x"))

    monkeypatch.setattr(agent_mod, "run_task", boom)
    task = {"task_id": 5, "schedule_seconds": 3600, "query_version": 3, "sync": {}}
    r1 = a.execute_task(task, {})
    s = a.state.get_schedule(5)
    assert r1["status"] == "failed" and r1["stage"] == "extract" and r1["error_code"] == "SOURCE_CONNECTION_FAILED"
    assert s["attempt"] == 2 and s["next_due_at"] == now[0] + 60
    r2 = a.execute_task(task, {})
    assert r2["execution_id"] != r1["execution_id"]
    assert a.state.get_schedule(5)["attempt"] == 3 and a.state.get_schedule(5)["next_due_at"] == now[0] + 120
    a.execute_task(task, {})
    s = a.state.get_schedule(5)
    assert s["attempt"] == 1 and s["next_due_at"] == now[0] + 3600   # agotados: esperar la programación
    updates = [i for i in a.state.all_items() if i.type == "execution_update"]
    assert [u.payload["attempt"] for u in updates] == [1, 2, 3]
    assert all(u.critical for u in updates)
    assert all("extract_sql" not in json.dumps(u.payload) for u in updates)


def test_exito_guarda_checkpoint_local_y_lo_usa(tmp_path, monkeypatch):
    a, now = _agent(tmp_path)
    seen = []

    def ok(task, wh, prev, settings, ctx, **kw):
        seen.append(prev)
        return TaskResult(rows_read=3, rows_loaded=3, rows_inserted=3, rows_updated=0,
                          watermark=datetime(2030, 5, 5, 10, 0, 0), watermark_kind="source_clock")

    monkeypatch.setattr(agent_mod, "run_task", ok)
    task = {"task_id": 9, "schedule_seconds": 3600, "sync": {"watermark": "2030-05-01T00:00:00"}}
    a.execute_task(task, {})
    a.execute_task(task, {})
    assert seen[0] == datetime(2030, 5, 1) and seen[1] == datetime(2030, 5, 5, 10)  # usa el checkpoint local
    fin = [i for i in a.state.all_items() if i.type == "execution_update"][0]
    assert fin.payload["checkpoint"] == {"watermark": "2030-05-05T10:00:00.000000", "kind": "source_clock"}


def test_credencial_ligada_a_la_api_url(tmp_path):
    a, _ = _agent(tmp_path)
    a.store.save(InstallationCredential("i", "s", "https://otro-servidor.example"))
    with pytest.raises(FatalAgentError):
        a.ensure_credential()


def test_config_caduca_y_no_se_inician_tareas(tmp_path, monkeypatch):
    a, _ = _agent(tmp_path, config_max_age_seconds=30)
    a._config = {"tasks": [{"task_id": 1}], "config_max_age_seconds": 900}
    a._config_at = time.monotonic() - 31
    assert a.fresh_config() is None and a._config is None       # se purga de memoria
    assert any(i.payload.get("event_type") == "config_stale" for i in a.state.all_items())


# ─────────────────────────────────────────────────────────────────────────────
# Regresiones de la validación de Fase 1
# ─────────────────────────────────────────────────────────────────────────────
from nexus_agent.api import ApiAuthError, ApiForbidden  # noqa: E402
from nexus_agent.etl import extract_overlap_seconds  # noqa: E402

LEAKS = [
    ('invalid input syntax for type integer: "4111111111111111"', "4111111111111111"),
    ('date/time field value out of range: "2024-13-45 25:61"', "2024-13-45"),
    ("The duplicate key value is (A123, juan@x.com).", "juan@x.com"),
    ("Conversion failed when converting the varchar value 'RFC-XAXX010101' to data type int.", "XAXX010101"),
    ("(1062, \"Duplicate entry 'juan@x.com' for key 'email'\")", "juan@x.com"),
    ("Login failed for user 'ab'.", "'ab'"),
    ("syntax error at end\nSELECT id,\n nombre FROM clientes WHERE rfc='XAXX'", "clientes"),
]


def test_redact_core_sincronizado_con_backend():
    here = os.path.dirname(os.path.abspath(__file__))
    core = open(os.path.join(here, "..", "nexus_agent", "redact_core.py"), encoding="utf-8").read()
    back = open(os.path.join(here, "..", "..", "dwh_back", "redact.py"), encoding="utf-8").read()
    assert core.split("\n", 1)[1] == back


def test_sanitize_fugas_de_drivers():
    for text, secret in LEAKS:
        _, msg = sanitize_error(StageError("load", RuntimeError(text)))
        assert secret not in msg, (text, msg)
        assert secret not in redact_text(text)


def test_sanitize_y_formatter_en_tiempo_acotado():
    fmt = RedactingFormatter("%(message)s")
    for bad in ["Key (a)=(" * 200_000, "(" * 1_000_000, "select " * 300_000, "'" * 1_000_000, "password=" * 200_000]:
        t = time.perf_counter()
        sanitize_error(RuntimeError(bad))
        fmt.format(logging.LogRecord("nexus", logging.ERROR, __file__, 1, bad, (), None))
        assert time.perf_counter() - t < 0.5


def test_api_reintenta_401_con_credencial_rotada(tmp_path):
    holder = CredentialHolder(InstallationCredential("i", "viejo", "http://127.0.0.1:9"))
    api = NexusApi(Settings(api_url="http://127.0.0.1:9"), holder)
    calls = []

    def once(method, path, **kw):
        calls.append(holder.get().secret)
        if len(calls) == 1:
            holder.set(InstallationCredential("i", "nuevo", "http://127.0.0.1:9"))  # otro hilo rotó
            raise ApiAuthError(401, "invalid_credentials", "x")
        return {"ok": True}

    api._request_once = once
    assert api.request("GET", "/agent/whoami") == {"ok": True} and calls == ["viejo", "nuevo"]
    # Revocada: no se reintenta
    calls.clear()
    api._request_once = lambda *a, **k: (_ for _ in ()).throw(ApiAuthError(401, "installation_revoked", "x"))
    with pytest.raises(ApiAuthError):
        api.request("GET", "/agent/whoami")


def test_solo_revocacion_detiene_el_agente(tmp_path):
    a, _ = _agent(tmp_path)
    a._handle_auth_error(ApiAuthError(401, "invalid_credentials", "x"))
    assert a.fatal is None and not a.stop_event.is_set()
    a._handle_auth_error(ApiAuthError(401, "installation_revoked", "x"))
    assert a.fatal.exit_code == 3 and a.stop_event.is_set()


class ScriptedApi(FakeApi):
    def __init__(self, settings, holder):
        super().__init__(settings, holder)
        self.script = []

    def _call(self, name, payload):
        if self.script:
            exc = self.script.pop(0)
            if exc:
                raise exc
        self.calls.append((name, payload.get("agent_seq")))
        return {"status": "ok"}


def _agent2(tmp_path, **kw):
    s = Settings(api_url="http://127.0.0.1:9", data_dir=str(tmp_path), queue_max_server_errors=3, **kw)
    now = [1_000_000.0]
    st = LocalState(str(tmp_path / "s.db"), clock=lambda: now[0], rng=lambda a, b: b, backoff_base=1)
    a = Agent(s, state=st, api_factory=ScriptedApi, clock=lambda: now[0],
              store=CredentialStore(str(tmp_path), force_backend="file"))
    return a, now


def test_scope_disabled_no_descarta_reportes(tmp_path):
    a, now = _agent2(tmp_path)
    a.state.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="e1", task_id=1)
    a.api.script = [ApiForbidden(403, "scope_disabled", "x")] * 3
    for _ in range(3):
        assert a.send_one(a.api) == "deferred"
        now[0] += 100
    assert a.state.dead_letter_count() == 0 and a.state.depth == 1
    assert a.send_one(a.api) == "sent"


class RuleApi(FakeApi):
    """API simulada: ``rule(name, payload)`` devuelve una excepción o None (éxito)."""

    def __init__(self, settings, holder):
        super().__init__(settings, holder)
        self.rule = lambda name, p: None

    def _call(self, name, payload):
        exc = self.rule(name, payload)
        if exc:
            raise exc
        self.calls.append((name, payload.get("agent_seq")))
        return {"status": "ok"}


def _agent3(tmp_path):
    s = Settings(api_url="http://127.0.0.1:9", data_dir=str(tmp_path), queue_max_server_errors=10,
                 queue_poison_min_seconds=1800, queue_parked_retry_seconds=3600)
    now = [1_000_000.0]
    st = LocalState(str(tmp_path / "s.db"), clock=lambda: now[0], rng=lambda a, b: b, backoff_base=2,
                    backoff_max=300)
    a = Agent(s, state=st, api_factory=RuleApi, clock=lambda: now[0],
              store=CredentialStore(str(tmp_path), force_backend="file"))
    return a, now


def _fill(a):
    a.state.enqueue("execution_start", {"x": 1}, execution_id="e1", task_id=1)
    a.state.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="e1", task_id=1)
    a.state.enqueue("execution_update", {"status": "success"}, is_success=True, execution_id="e2", task_id=1,
                    checkpoint=("2030-01-01T00:00:00.000000", "source_clock", 1.0))
    a.state.enqueue("agent_event", {"event_type": "agent_started", "payload": {}})


def test_caida_larga_no_descarta_nada_y_se_entrega_todo(tmp_path):
    a, now = _agent3(tmp_path)
    _fill(a)
    errors = [ApiUnavailable(502, "server_error", "x"), ApiUnavailable(503, "server_error", "x"),
              ApiUnavailable(504, "server_error", "x"), ApiUnavailable(0, "network_error", "x"),
              ApiUnavailable(500, "server_error", "x")]  # 500 durante la caída: tampoco es veneno
    n = [0]

    def outage(name, p):
        n[0] += 1
        return errors[n[0] % len(errors)]

    a.api.rule = outage
    end = now[0] + 10 * 3600                      # 10 horas de caída
    while now[0] < end:
        a.send_one(a.api)
        now[0] += 120
    assert n[0] > 100
    assert a.state.dead_letter_count() == 0 and a.state.parked_count() == 0 and a.state.depth == 4
    a.api.rule = lambda name, p: None             # Nexus vuelve
    now[0] += 1000
    assert a.flush(timeout=5)
    assert [c[1] for c in a.api.calls] == [1, 2, 3, 4]   # todo, en orden y sin duplicados
    assert a.state.dead_letter_count() == 0


def test_500_intercalado_con_503_reinicia_el_conteo(tmp_path):
    a, now = _agent3(tmp_path)
    a.state.enqueue("agent_event", {"event_type": "warning", "payload": {}})
    seq = iter([ApiUnavailable(500, "server_error", "x")] * 9 + [ApiUnavailable(503, "server_error", "x")]
               + [ApiUnavailable(500, "server_error", "x")] * 9)
    a.api.rule = lambda name, p: next(seq)
    for _ in range(19):
        a.mark_api_ok()                           # Nexus sano para otras llamadas
        now[0] += 400
        assert a.send_one(a.api) in ("deferred", "wait")
    assert a.state.dead_letter_count() == 0


def test_veneno_real_con_nexus_sano(tmp_path):
    a, now = _agent3(tmp_path)
    _fill(a)
    # El fallo crítico (seq 2) y el evento informativo (seq 4) fallan SIEMPRE con 500; lo demás pasa.
    a.api.rule = lambda name, p: (ApiUnavailable(500, "server_error", "x")
                                  if p.get("agent_seq") in (2, 4) else None)
    results = []
    for _ in range(80):
        a.mark_api_ok()                           # heartbeats OK: Nexus sano
        results.append(a.send_one(a.api))
        now[0] += 300
    assert "parked" in results and "dead" in results
    items = {i.seq: i for i in a.state.all_items()}
    assert 2 in items and a.state.parked_count() == 1         # crítico: estacionado, NO descartado
    assert a.state.dead_letter_count() == 1                   # no crítico: dead-letter
    sent = [c[1] for c in a.api.calls]
    assert 1 in sent and 3 in sent                            # el resto de la cola fluyó
    assert a.heartbeat_payload()["parked_total"] == 1 and a.heartbeat_payload()["dead_letter_total"] == 1
    a._periodic_housekeeping()
    ev = [i for i in a.state.all_items() if i.type == "agent_event" and i.payload.get("event_type") == "dead_letter"]
    assert ev and ev[0].critical and ev[0].payload["payload"]["parked_total"] == 1
    # El estacionado se reintenta cada hora y se entrega cuando Nexus lo acepta
    a.api.rule = lambda name, p: None
    parked = items[2]
    assert a.state.head().seq != 2 or parked.next_attempt_at <= now[0]
    now[0] += 3601
    assert a.flush(timeout=5)
    assert 2 in [c[1] for c in a.api.calls] and a.state.depth == 0


def test_422_va_a_dead_letter_con_aviso(tmp_path):
    a, now = _agent2(tmp_path)
    a.state.enqueue("execution_update", {"status": "failed"}, critical=True, execution_id="e1", task_id=1)
    a.api.script = [ApiRejected(422, "http_422", "x")]
    assert a.send_one(a.api) == "dead"
    assert a.state.take_dead_letter_report(min_interval=0)["dead_letter_total"] == 1


def test_sin_reloj_de_origen_no_avanza_watermark(tmp_path, monkeypatch):
    a, _ = _agent(tmp_path)

    def ok(task, wh, prev, settings, ctx, previous_kind=None):
        return TaskResult(rows_read=1, rows_loaded=1, watermark=datetime(2030, 1, 1), watermark_kind="agent_local",
                          warnings=["SOURCE_CLOCK_UNAVAILABLE"])

    monkeypatch.setattr(agent_mod, "run_task", ok)
    a.execute_task({"task_id": 3, "schedule_seconds": 60, "sync": {}}, {})
    fin = [i for i in a.state.all_items() if i.type == "execution_update"][0]
    assert "checkpoint" not in fin.payload and "CHECKPOINT_SKIPPED_CLOCK_FALLBACK" in fin.payload["warnings"]
    assert a.state.local_watermark(3) is None


def test_no_se_mezclan_relojes(tmp_path, monkeypatch):
    a, _ = _agent(tmp_path, watermark_clock="agent_utc")

    def ok(task, wh, prev, settings, ctx, previous_kind=None):
        return TaskResult(rows_read=1, rows_loaded=1, watermark=datetime(2030, 1, 1), watermark_kind="agent_utc")

    monkeypatch.setattr(agent_mod, "run_task", ok)
    task = {"task_id": 4, "schedule_seconds": 60,
            "sync": {"watermark": "2029-01-01T00:00:00", "watermark_kind": "source_clock"}}
    a.execute_task(task, {})
    fin = [i for i in a.state.all_items() if i.type == "execution_update"][0]
    assert "checkpoint" not in fin.payload and "CHECKPOINT_KIND_MISMATCH" in fin.payload["warnings"]
    # un checkpoint local de otro reloj se ignora al calcular el watermark previo
    a.state.enqueue("execution_update", {"status": "success"}, execution_id="x", task_id=4,
                    checkpoint=("2031-01-01T00:00:00.000000", "agent_utc", 1.0))
    assert a.previous_watermark(task) == datetime(2029, 1, 1)


def test_solapamiento_mayor_en_transicion_desde_legado():
    s = Settings(watermark_overlap_seconds=120, legacy_watermark_overlap_seconds=3600)
    assert extract_overlap_seconds(s, "legacy_last_run") == 3600
    assert extract_overlap_seconds(s, "source_clock") == 120


def test_reinicio_durante_commit_se_reporta_como_desconocido(tmp_path):
    a, _ = _agent(tmp_path)
    a.state.enqueue("execution_start", {"task_id": 1, "started_at": "2030-01-01T00:00:00Z"},
                    execution_id="e-commit", task_id=1, running="start")
    a.state.mark_committing("e-commit")
    a.state.enqueue("execution_start", {"task_id": 2, "started_at": "2030-01-01T00:00:00Z"},
                    execution_id="e-run", task_id=2, running="start")
    assert a.recover_orphans() == 2
    ups = {i.execution_id: i.payload for i in a.state.all_items() if i.type == "execution_update"}
    assert ups["e-commit"]["error_code"] == "AGENT_RESTARTED_COMMIT_UNKNOWN"
    assert "desconocido" in ups["e-commit"]["error_message"]
    assert ups["e-run"]["error_code"] == "AGENT_RESTARTED"
    assert a.state.orphan_executions() == []


def test_sanitize_conserva_codigo_mysql_y_oculta_correos():
    exc = _fake_exc("pymysql.err", "IntegrityError", "x")
    exc.args = (1062, "Duplicate entry 'juan@x.com' for key 'email'")
    code, msg = sanitize_error(StageError("load", exc))
    assert code == "DWH_CONSTRAINT_VIOLATION" and "juan@x.com" not in msg and "1062" in msg
    assert "4111111111111111" not in redact_text("tarjeta 4111111111111111 en fila")


# ─────────────────────────────────────────────────────────────────────────────
# Destino configurable (sección 22)
# ─────────────────────────────────────────────────────────────────────────────
def test_destino_efectivo_esquema_y_tabla():
    from nexus_agent.destination import effective_load_table, task_warehouse

    assert effective_load_table("Clientes", {"schema": "ventas"}) == "ventas.clientes"
    assert effective_load_table("dwh.carter", {"schema": "ventas"}) == "dwh.carter"
    assert effective_load_table("clientes", {"schema": "public"}) == "clientes"
    assert effective_load_table("clientes", {}) == "clientes"   # Nexus anterior: sin esquema
    cfg = {"warehouse": {"host": "g"}, "tasks": []}
    assert task_warehouse({"warehouse": {"host": "propio"}}, cfg) == {"host": "propio"}
    assert task_warehouse({}, cfg) == {"host": "g"}


def test_opciones_ssl_y_certificado(tmp_path):
    import psycopg2

    from nexus_agent.destination import pg_ssl_kwargs, prune_ca_files

    s = Settings(data_dir=str(tmp_path))
    assert pg_ssl_kwargs({}, s) == {}                       # compat: comportamiento previo de libpq
    assert pg_ssl_kwargs({"sslmode": "disable"}, s) == {"sslmode": "disable"}
    assert pg_ssl_kwargs({"sslmode": "loquesea"}, s) == {"sslmode": "require"}   # nunca se degrada
    pem = "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----"
    kw = pg_ssl_kwargs({"sslmode": "verify-full", "sslrootcert": pem}, s)
    assert kw["sslmode"] == "verify-full" and os.path.isfile(kw["sslrootcert"])
    assert open(kw["sslrootcert"]).read().startswith("-----BEGIN CERTIFICATE-----")
    assert oct(os.stat(kw["sslrootcert"]).st_mode)[-3:] == "600"
    assert pg_ssl_kwargs({"sslmode": "verify-full", "sslrootcert": pem}, s) == kw   # mismo archivo
    if psycopg2.__libpq_version__ >= 160000:
        assert pg_ssl_kwargs({"sslmode": "verify-full"}, s)["sslrootcert"] == "system"
    assert "sslrootcert" not in pg_ssl_kwargs({"sslmode": "verify-ca"}, s)
    # Limpieza: se conserva el que usa la config vigente.
    assert prune_ca_files(s, {"warehouse": {"sslrootcert": pem}, "tasks": []}) == 0
    assert prune_ca_files(s, {"warehouse": {}, "tasks": []}) == 1
    assert not os.path.exists(kw["sslrootcert"])


def test_error_ssl_clasificado():
    import psycopg2

    exc = psycopg2.OperationalError('connection to server at "10.0.0.5", port 5432 failed: server does not '
                                    'support SSL, but SSL was required')
    code, msg = sanitize_error(StageError("load", exc))
    assert code == "DWH_SSL_ERROR" and "10.0.0.5" not in msg
    code, _ = sanitize_error(StageError("load", psycopg2.OperationalError("could not connect: timeout expired")))
    assert code == "DWH_CONNECT_TIMEOUT"


def test_inventario_elige_el_dwh_por_identidad():
    from nexus_agent.inventory import capabilities, dwh_identity_of, target_connection

    g = {"host": "dwh-g", "port": 5432, "database": "a", "username": "u", "password": "p", "sslmode": "disable"}
    own = {"host": "dwh-p", "port": 6543, "database": "b", "username": "u2", "password": "p2", "sslmode": "require"}
    cfg = {"warehouse": g, "tasks": [{"task_id": 1, "company_id": 7, "warehouse": own, "source": {}}]}
    caps = capabilities(cfg)
    assert sorted(caps["dwh_identities"]) == sorted([dwh_identity_of(g), dwh_identity_of(own)])
    t = target_connection({"kind": "dwh", "identity_key": dwh_identity_of(own)}, cfg)
    assert (t["host"], t["port"], t["sslmode"]) == ("dwh-p", 6543, "require")
    assert target_connection({"kind": "dwh", "identity_key": "f" * 64}, cfg) is None   # nunca otra conexión


def test_heartbeat_anuncia_capacidades(tmp_path):
    s = Settings(data_dir=str(tmp_path), api_url="http://127.0.0.1:1", mode="development",
                 allow_insecure_http=True)
    a = Agent(s)
    assert a.heartbeat_payload()["features"] == ["destination-v2", "connection-test"]
    s.connection_test_enabled = False
    assert a.heartbeat_payload()["features"] == ["destination-v2"]
    assert a.api.session.headers["x-nexus-agent-features"] == "destination-v2,connection-test"


def test_pruebas_de_conexion_espaciadas_y_con_tope(tmp_path, monkeypatch):
    from nexus_agent import agent as am

    s = Settings(data_dir=str(tmp_path), api_url="http://127.0.0.1:1", mode="development",
                 allow_insecure_http=True, connection_test_min_spacing_seconds=1, connection_test_max_per_minute=3)
    a = Agent(s)
    ran = []
    monkeypatch.setattr(am, "run_connection_test", lambda test, settings: ran.append(time.monotonic()) or
                        {"status": "ok", "checks": []})

    class FakeApi:
        def claim_connection_test(self):
            return {"test": {"id": "t", "target_kind": "group_dwh", "kind": "dwh",
                             "connection": {"host": "h", "port": 5432, "database": "d", "username": "u"}}}

        def report_connection_test(self, tid, payload):
            return {}

    api = FakeApi()
    for _ in range(3):
        assert a.run_one_connection_test(api)
    # Misma conexión: separadas al menos 1 s.
    assert ran[1] - ran[0] >= 0.95 and ran[2] - ran[1] >= 0.95
    # Tope por minuto alcanzado: no se toma otra.
    assert a.run_one_connection_test(api) is None and a._tests_budget_wait() > 0


def test_ca_escritura_atomica_y_en_uso_no_se_borra(tmp_path):
    import threading as th

    from nexus_agent import destination as dm

    s = Settings(data_dir=str(tmp_path))
    pem = "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----"
    paths = []
    ts = [th.Thread(target=lambda: paths.append(dm.ca_file(s, pem))) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(paths)) == 1 and os.path.isfile(paths[0])
    assert not [f for f in os.listdir(os.path.dirname(paths[0])) if f.endswith(".tmp")]
    with dm._IN_USE_LOCK:
        dm._IN_USE[dm._ca_name(pem)] = 1
    try:
        assert dm.prune_ca_files(s, {"warehouse": {}, "tasks": []}) == 0 and os.path.isfile(paths[0])
    finally:
        with dm._IN_USE_LOCK:
            dm._IN_USE.pop(dm._ca_name(pem), None)
    assert dm.prune_ca_files(s, {"warehouse": {}, "tasks": []}) == 1
