"""Regresiones de la validación de Fase 1: DoS/ReDoS, límites, saneamiento, watermark."""

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

from redact import redact_text
from test_agent_api import H, enroll, finish, q1, start

LEAKS = [
    ('invalid input syntax for type integer: "4111111111111111"', "4111111111111111"),
    ('date/time field value out of range: "2024-13-45 25:61"', "2024-13-45"),
    ("The duplicate key value is (A123, juan@x.com).", "juan@x.com"),
    ("Conversion failed when converting the varchar value 'RFC-XAXX010101' to data type int.", "XAXX010101"),
    ("(1062, \"Duplicate entry 'juan@x.com' for key 'email'\")", "juan@x.com"),
    ("Login failed for user 'ab'.", "'ab'"),
    ("syntax error at end\nSELECT id,\n nombre FROM clientes WHERE rfc='XAXX'", "clientes"),
    ("DETAIL:  Key (email)=(juan@x.com) already exists.", "juan@x.com"),
    ("Failing row contains (1, juan@x.com, null).", "juan@x.com"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Saneamiento (función pura)
# ─────────────────────────────────────────────────────────────────────────────
def test_redact_cubre_fugas_reportadas():
    for text, secret in LEAKS:
        out = redact_text(text)
        assert secret not in out, (text, out)
    # Conserva identificadores útiles
    assert '"tabla_x"' in redact_text('relation "tabla_x" does not exist')
    assert '"dups_email_key"' in redact_text('violates unique constraint "dups_email_key"')


def test_redact_tiempo_acotado_en_entradas_patologicas():
    for bad in ["Key (a)=(" * 200_000, "(" * 1_000_000, '"' + "a" * 1_000_000, "select " * 300_000,
                "password=" * 200_000, "'" * 1_000_000, "Failing row contains (" * 100_000,
                "value is (" * 100_000, "a," * 500_000]:
        t = time.perf_counter()
        redact_text(bad)
        assert time.perf_counter() - t < 0.5, bad[:20]


# ─────────────────────────────────────────────────────────────────────────────
# DoS por cuerpo / validación
# ─────────────────────────────────────────────────────────────────────────────
def _health_latency(b, stop):
    worst = 0.0
    while not stop.is_set():
        t = time.perf_counter()
        requests.get(b.url + "/health", timeout=10)
        worst = max(worst, time.perf_counter() - t)
        time.sleep(0.02)
    return worst


def test_enroll_patologico_rapido_y_sin_bloquear(env):
    b = env["backend"]
    payload = {"name": "Key (a)=(" * 20000}
    stop = threading.Event()
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("w", _health_latency(b, stop)))
    th.start()
    try:
        t = time.perf_counter()
        r = requests.post(b.url + "/agent/enroll", json=payload, timeout=30)
        took = time.perf_counter() - t
    finally:
        stop.set()
        th.join()
    assert r.status_code == 413 and took < 1.0
    assert res["w"] < 1.0
    # Sin Content-Length (chunked): también se corta
    def gen():
        for _ in range(50):
            yield b'{"name": "' + b"x" * 1000
    r = requests.post(b.url + "/agent/enroll", data=gen(), headers={"content-type": "application/json"}, timeout=30)
    assert r.status_code == 413


def test_client_event_patologico_no_bloquea(env, db):
    b, ids = env["backend"], env["ids"]
    detail = "Key (a)=(" * 100_000  # ~900 KB < 1 MB
    stop = threading.Event()
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("w", _health_latency(b, stop)))
    th.start()
    try:
        t = time.perf_counter()
        r = requests.post(b.url + "/client-event", headers={"x-token": ids["company_a"]["company_token"]},
                          json={"config_id": str(ids["t_cli"]["id"]), "event_type": "error", "detail": detail},
                          timeout=30)
        took = time.perf_counter() - t
    finally:
        stop.set()
        th.join()
    assert r.status_code == 200 and took < 2.0 and res["w"] < 1.0
    too_big = requests.post(b.url + "/client-event", headers={"x-token": ids["company_a"]["company_token"]},
                            json={"config_id": "1", "event_type": "error", "detail": "x" * 2_000_000}, timeout=30)
    assert too_big.status_code == 413


def test_422_no_devuelve_el_valor_recibido(env):
    b = env["backend"]
    marker = "SECRETO-" + "z" * 300
    r = requests.post(b.url + "/agent/enroll", json={"name": marker}, headers={"x-token": "x"})
    assert r.status_code == 422 and "SECRETO" not in r.text
    r = requests.post(b.url + "/agent/enroll", json={"fingerprint": {"k" * 50: "v"}}, headers={"x-token": "x"})
    assert r.status_code == 422
    r = requests.post(b.url + "/agent/enroll", json={"fingerprint": {"k" * 500_000: "v"}}, headers={"x-token": "x"})
    assert r.status_code == 413


def test_limites_en_ejecuciones_y_eventos(env):
    b, ids = env["backend"], env["ids"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="limites")
    h = H(iid, sec)
    ex = str(uuid.uuid4())
    tid = ids["t_cli"]["id"]
    assert start(b, h, ex, tid, 1).status_code == 200
    assert finish(b, h, ex, tid, 2, rows_read=2**70).status_code == 422          # no 500
    assert finish(b, h, ex, tid, 2, warnings=["w" * 300]).status_code == 422
    assert finish(b, h, ex, tid, 2, warnings=["w"] * 21).status_code == 422
    r = requests.post(b.url + "/agent/events", headers=h, json={
        "event_id": str(uuid.uuid4()), "event_type": "warning", "payload": {"x": "y" * 5000}})
    assert r.status_code == 422
    r = requests.post(b.url + "/agent/heartbeat", headers=h, json={"queue_depth": 2**40})
    assert r.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# Watermark: rango y tipo de reloj
# ─────────────────────────────────────────────────────────────────────────────
def test_watermark_futuro_y_tipo_distinto_no_se_aplican(env, db):
    b, ids = env["backend"], env["ids"]
    tid = ids["t_cli"]["id"]
    iid, sec = enroll(b, "x-group-token", ids["group_a"]["group_token"], name="wm")
    h = H(iid, sec)
    db.cursor().execute("INSERT INTO task_sync_state (task_id, watermark, watermark_kind) VALUES (%s, '2025-03-01', 'source_clock') "
                        "ON CONFLICT (task_id) DO UPDATE SET watermark = '2025-03-01', watermark_kind = 'source_clock', "
                        "watermark_reset_at = NULL", (tid,))
    ex = str(uuid.uuid4())
    start(b, h, ex, tid, 10)
    r = finish(b, h, ex, tid, 11, wm="2999-01-01T00:00:00")
    assert r.status_code == 200
    row = q1(db, "SELECT warnings, checkpoint_confirmed FROM task_execution WHERE execution_id = %s", (ex,))
    assert "CHECKPOINT_REJECTED_OUT_OF_RANGE" in row[0] and row[1] is None
    assert q1(db, "SELECT watermark::text FROM task_sync_state WHERE task_id = %s", (tid,))[0].startswith("2025-03-01")
    # reloj distinto (agent_local) sobre watermark de reloj del origen → no se mezcla
    ex2 = str(uuid.uuid4())
    start(b, h, ex2, tid, 12)
    body = {"task_id": tid, "status": "success", "agent_seq": 13,
            "checkpoint": {"watermark": "2025-04-01T00:00:00", "kind": "agent_local"}}
    requests.put(b.url + f"/agent/executions/{ex2}", headers=h, json=body)
    row = q1(db, "SELECT warnings FROM task_execution WHERE execution_id = %s", (ex2,))
    assert "CHECKPOINT_KIND_MISMATCH" in row[0]
    assert q1(db, "SELECT watermark::text, watermark_kind FROM task_sync_state WHERE task_id = %s", (tid,)) == \
        ("2025-03-01 00:00:00", "source_clock")
    # Fechas del agente absurdas → 422
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert start(b, h, str(uuid.uuid4()), tid, 14, started=future).status_code == 422


def test_client_event_legado_sanea_fugas(env, db):
    b, ids = env["backend"], env["ids"]
    for text, secret in LEAKS:
        r = requests.post(b.url + "/client-event", headers={"x-token": ids["company_a"]["company_token"]},
                          json={"config_id": str(ids["t_cli"]["id"]), "event_type": "error", "detail": text})
        assert r.status_code == 200
        stored = q1(db, "SELECT detail FROM client_events WHERE source = 'legacy' ORDER BY id DESC LIMIT 1")[0]
        assert secret not in stored, (text, stored)


def test_clientes_legados_excluyen_tokens_invalidos(env):
    b = env["backend"]
    requests.get(b.url + "/configs", headers={"x-token": "token-invalido-" + "q" * 20})
    legacy = b.monitor("/monitor/installations")["legacy_clients"]
    assert legacy and all(c["group_id"] is not None for c in legacy)


def test_redact_correos_digitos_corchetes_y_codigos_utiles():
    out = redact_text("contacto juan.perez@correo.com.mx tarjeta 4111 1111 1111 1111 tel 5512345678 "
                      "cuenta 123456789012 valor [secreto interno]")
    for s in ("juan.perez", "4111", "5512345678", "123456789012", "secreto interno"):
        assert s not in out, out
    # Se conserva lo útil: código MySQL, cadena ODBC/SQLSTATE, fechas, conteos
    assert redact_text("(1062, \"Duplicate entry 'x' for key 'k'\")").startswith("(1062, ***)")
    odbc = redact_text("[Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Invalid object name 'x'. (208)")
    assert odbc.startswith("[Microsoft][ODBC Driver 17 for SQL Server][SQL Server]")
    assert "[42S02]" in redact_text("[42S02] error")
    assert "2026-09-27 02:56:32" in redact_text("Tarea 12: 500 filas en 2026-09-27 02:56:32")
    for bad in ["1" * 1_000_000, "a@" * 500_000, "[" * 1_000_000, "1111 " * 200_000, "x@y." * 200_000]:
        t = time.perf_counter()
        redact_text(bad)
        assert time.perf_counter() - t < 0.5
