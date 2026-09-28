"""
Usuarios del panel, sesiones, permisos por grupo, aislamiento, auditoría,
límites de tasa y pool de conexiones (sección 20 de DWH_README.md).

BD propia (nexus_test_cfg_auth) y backends locales en subproceso. Todas las
contraseñas se generan aquí (desechables); nunca se imprimen.
"""

import concurrent.futures
import hashlib
import importlib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

import psycopg2
import pytest
import requests

import support

DB = "nexus_test_cfg_auth"
T = 15
PK_B = "pk-b-" + secrets.token_urlsafe(16)     # clave panel↔backend (backend b)
PK_IP = "pk-ip-" + secrets.token_urlsafe(16)   # clave panel↔backend (backend ipb)


def pw() -> str:
    return "Pr-" + secrets.token_urlsafe(14)


EMAIL_DOMAIN = "@nexus.test"


def email_for(username: str) -> str:
    """Correo determinista para un nombre de prueba dado (el acceso ahora es por correo)."""
    return username + EMAIL_DOMAIN


# ─────────────────────────────────────────────────────────────────────────────
# Entorno
# ─────────────────────────────────────────────────────────────────────────────
def q(sql, params=()):
    c = support.cfg_conn(DB)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


def enroll(url, token, name):
    r = requests.post(url + "/agent/enroll", headers={"x-group-token": token},
                      json={"name": name, "hostname": "h", "client_version": "test"}, timeout=T)
    assert r.status_code == 201, r.text
    d = r.json()
    return {"x-installation-id": d["installation_id"], "x-installation-secret": d["secret"]}, d["installation_id"]


SEQ = iter(range(1, 10_000_000))


def failed_exec(url, h, tid):
    ex = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    r = requests.post(url + "/agent/executions", headers=h, timeout=T, json={
        "execution_id": ex, "task_id": tid, "started_at": now, "agent_seq": next(SEQ), "client_version": "t"})
    assert r.status_code == 200, r.text
    r = requests.put(url + f"/agent/executions/{ex}", headers=h, timeout=T, json={
        "task_id": tid, "status": "failed", "finished_at": now, "duration_ms": 10, "rows_read": 0,
        "rows_loaded": 0, "agent_seq": next(SEQ), "client_version": "t", "failure_stage": "extract",
        "error_code": "SOURCE_CONNECTION_FAILED", "error_message": "fallo de prueba"})
    assert r.status_code == 200, r.text
    return ex


def mk_mdb(name, groups):
    """Base monitoreada (DWH) con vínculos a `groups` + un cambio estructural pendiente."""
    key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    mid = q("""INSERT INTO monitored_database (kind, engine, identity_key, display_name, group_id, state,
                                               verification_status, view_definitions_enabled)
               VALUES ('dwh', 'postgresql', %s, %s, %s, 'monitoring', 'verified', TRUE) RETURNING id""",
            (key, name, groups[0]))[0][0]
    for g in groups:
        q("INSERT INTO monitored_database_link (monitored_database_id, group_id, company_id) VALUES (%s, %s, 0)",
          (mid, g))
    cid = q("""INSERT INTO structural_change (monitored_database_id, schema_name, object_name, object_type,
                                              change_kind, observed_fingerprint)
               VALUES (%s, 'public', %s, 'table', 'object_added', %s) RETURNING id""",
            (mid, "t_" + uuid.uuid4().hex[:6], "a" * 64))[0][0]
    return mid, cid


class Env(dict):
    pass


def login(url, username, password, *, email=None):
    """Inicia sesión por correo. `username` se traduce a su correo determinista salvo que se
    pase `email` explícito (usuarios inexistentes, mayúsculas, etc.)."""
    em = email if email is not None else email_for(username)
    r = requests.post(url + "/admin/auth/login", json={"email": em, "password": password}, timeout=T)
    assert r.status_code == 200, r.text
    return r.json()


def bearer(tok):
    return {"authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def aenv():
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.recreate_config_db(DB)
    b = support.Backend(DB, extra_ini={
        "inventory": {"expose_view_definitions": "true"},
        "auth": {"ip_max_failures": "1000", "panel_proxy_key": PK_B},
        "notifications": {"allow_http": "true", "block_private_ips": "false"},
    })
    # Backend con límites bajos (misma BD): token estático DESHABILITADO, pool de 5.
    lim = support.Backend(DB, extra_ini={
        "admin": {"allow_static_token": "false"},
        "auth": {"max_failed_attempts": "3", "lockout_base_seconds": "2", "lockout_max_seconds": "60",
                 "ip_max_failures": "1000"},
        "agent": {"enroll_rate_per_minute": "5", "enroll_fail_limit": "3", "auth_fail_limit": "4"},
        "database": {"pool_max": "5", "application_name": "nexus_test_lim"},
    })
    ipb = support.Backend(DB, extra_ini={"auth": {"ip_max_failures": "5", "max_failed_attempts": "100",
                                                  "panel_proxy_key": PK_IP},
                                         "agent": {"enroll_fail_limit": "3"}})
    b.start()
    lim.start()
    ipb.start()
    try:
        ids = support.seed_config(b)
        A, B = ids["group_a"]["id"], ids["group_b"]["id"]
        ha, ia = enroll(b.url, ids["group_a"]["group_token"], "inst-a")
        hb, ib = enroll(b.url, ids["group_b"]["group_token"], "inst-b")
        failed_exec(b.url, ha, ids["t_cli"]["id"])
        failed_exec(b.url, hb, ids["t_b"]["id"])
        inc_a = b.admin("GET", f"/admin/incidents?group_id={A}", expect=200)["items"][0]["id"]
        inc_b = b.admin("GET", f"/admin/incidents?group_id={B}", expect=200)["items"][0]["id"]
        # Eventos legados (client-event con token de empresa)
        for comp, t in ((ids["company_a"], ids["t_cli"]), (ids["company_b"], ids["t_b"])):
            r = requests.post(b.url + "/client-event", headers={"x-token": comp["company_token"]}, timeout=T,
                              json={"config_id": str(t["id"]), "task_name": "x", "event_type": "error",
                                    "detail": "falla legada", "rows_loaded": 0})
            assert r.status_code in (200, 201), r.text
        mdb_a, ch_a = mk_mdb("DWH A", [A])
        mdb_b, ch_b = mk_mdb("DWH B", [B])
        mdb_s, ch_s = mk_mdb("DWH compartido", [A, B])
        chan = {}
        for name, gid in (("canal-a", A), ("canal-b", B), ("canal-global", None)):
            chan[name] = b.admin("POST", "/admin/notification-channels",
                                 {"name": name, "kind": "log", "group_id": gid}, expect=201)["id"]
        users = {}

        def user(name, roles, **kw):
            p = pw()
            u = b.admin("POST", "/admin/users", {"username": name, "email": email_for(name), "password": p,
                                                 "must_change_password": False, "roles": roles, **kw}, expect=201)
            users[name] = {"id": u["id"], "password": p, "email": email_for(name)}
            return u

        user("super", [], is_superadmin=True)
        user("viewer_a", [{"role": "lectura", "group_id": A}])
        user("op_ab", [{"role": "operador", "group_id": A}, {"role": "lectura", "group_id": B}])
        user("attr_a", [{"role": "atribucion_estructura", "group_id": A}])
        user("appr_a", [{"role": "aprobador_inventario", "group_id": A}])
        user("creds_a", [{"role": "admin_credenciales", "group_id": A}])
        user("cfg_a", [{"role": "admin_config", "group_id": A}])
        user("defs_a", [{"role": "definiciones_vistas", "group_id": A}])
        user("auditor_a", [{"role": "auditor", "group_id": A}])
        user("uadmin", [{"role": "admin_usuarios"}])
        sessions = {n: bearer(login(b.url, n, u["password"])["token"]) for n, u in users.items()}
        yield Env(b=b, lim=lim, ipb=ipb, ids=ids, A=A, B=B, ia=ia, ib=ib, ha=ha, hb=hb, inc_a=inc_a, inc_b=inc_b,
                  mdb_a=mdb_a, mdb_b=mdb_b, mdb_s=mdb_s, ch_a=ch_a, ch_b=ch_b, ch_s=ch_s, chan=chan,
                  users=users, s=sessions)
    finally:
        for x in (b, lim, ipb):
            x.stop()
        support.drop_config_db(DB)


def call(env, who, method, path, body=None, expect=None, url=None):
    r = requests.request(method, (url or env["b"].url) + path, json=body, headers=env["s"][who], timeout=T)
    if expect is not None:
        assert r.status_code == expect, f"{who} {method} {path} -> {r.status_code}: {r.text[:300]}"
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Autenticación
# ─────────────────────────────────────────────────────────────────────────────
def test_login_ok_y_fallos_genericos(aenv):
    b = aenv["b"]
    d = login(b.url, "viewer_a", aenv["users"]["viewer_a"]["password"])
    assert d["user"]["username"] == "viewer_a" and d["must_change_password"] is False
    assert d["permissions"] == {"view": [aenv["A"]]}
    assert len(d["token"]) >= 40 and d["idle_timeout_seconds"] == 1800
    # Correo insensible a mayúsculas.
    d2 = login(b.url, "viewer_a", aenv["users"]["viewer_a"]["password"],
              email=email_for("viewer_a").upper())
    assert d2["user"]["username"] == "viewer_a"
    # Contraseña mala y correo inexistente: mismo 401 y mismo mensaje.
    r1 = requests.post(b.url + "/admin/auth/login", json={"email": email_for("viewer_a"), "password": "x" * 14},
                       timeout=T)
    r2 = requests.post(b.url + "/admin/auth/login", json={"email": email_for("no_existe_" + uuid.uuid4().hex[:6]),
                                                          "password": "x" * 14}, timeout=T)
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]
    # El backend YA NO acepta "username" en el cuerpo (solo "email").
    r3 = requests.post(b.url + "/admin/auth/login",
                       json={"username": "viewer_a", "password": aenv["users"]["viewer_a"]["password"]}, timeout=T)
    assert r3.status_code == 422
    # Sin credenciales: 401 en /admin/*.
    assert requests.get(b.url + "/admin/groups", timeout=T).status_code == 401
    assert requests.get(b.url + "/admin/groups", headers=bearer("x" * 43), timeout=T).status_code == 401
    ev = q("SELECT action, details->>'reason' FROM panel_audit_log WHERE action = 'auth.login_failed'")
    assert {r[1] for r in ev} >= {"bad_password", "unknown_email"}


def test_token_de_sesion_guardado_solo_hasheado(aenv):
    b = aenv["b"]
    tok = login(b.url, "viewer_a", aenv["users"]["viewer_a"]["password"])["token"]
    rows = q("SELECT token_hash FROM panel_session WHERE token_hash = %s",
             (hashlib.sha256(tok.encode()).hexdigest(),))
    assert len(rows) == 1
    dump = q("SELECT string_agg(t::text, '|') FROM panel_session t")[0][0]
    assert tok not in dump
    pw_ = aenv["users"]["viewer_a"]["password"]
    assert pw_ not in q("SELECT string_agg(t::text, '|') FROM panel_user t")[0][0]
    assert q("SELECT password_hash FROM panel_user WHERE username = 'viewer_a'")[0][0].startswith("$argon2id$")
    # Ni el token ni la contraseña quedan en los logs del backend / activity_log / auditoría.
    assert tok not in open(b.log_path).read() and pw_ not in open(b.log_path).read()
    for table in ("activity_log", "panel_audit_log"):
        assert not q(f"SELECT 1 FROM {table} t WHERE t::text LIKE %s OR t::text LIKE %s",
                     (f"%{tok}%", f"%{pw_}%"))


def test_bloqueo_por_usuario_con_backoff_exponencial(aenv):
    lim = aenv["lim"]
    p = pw()
    aenv["b"].admin("POST", "/admin/users", {"username": "lock_me", "email": email_for("lock_me"), "password": p,
                                             "must_change_password": False}, expect=201)

    def attempt(password):
        return requests.post(lim.url + "/admin/auth/login", json={"email": email_for("lock_me"), "password": password},
                             timeout=T)

    assert [attempt("mala-" + "x" * 12).status_code for _ in range(3)] == [401, 401, 401]
    r = attempt(p)  # bloqueada: ni con la contraseña correcta
    assert r.status_code == 429 and r.json()["detail"]["code"] == "account_locked"
    lock1 = q("SELECT EXTRACT(EPOCH FROM locked_until - NOW()) FROM panel_user WHERE username = 'lock_me'")[0][0]
    assert 0 < lock1 <= 2.1
    time.sleep(2.2)
    assert attempt("mala-" + "y" * 12).status_code == 401      # 4.º fallo → 2 × 2^1 = 4 s
    lock2 = q("SELECT EXTRACT(EPOCH FROM locked_until - NOW()) FROM panel_user WHERE username = 'lock_me'")[0][0]
    assert 2.5 < lock2 <= 4.1
    time.sleep(4.2)
    assert attempt(p).status_code == 200
    assert q("SELECT failed_attempts, locked_until FROM panel_user WHERE username = 'lock_me'")[0] == (0, None)
    # Correo inexistente: mismas reglas (el 429 no revela si existe; el bloqueo queda por correo).
    ghost = email_for("ghost_" + uuid.uuid4().hex[:6])
    codes = [requests.post(lim.url + "/admin/auth/login", json={"email": ghost, "password": "z" * 14},
                           timeout=T).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]


def test_limite_de_fallos_por_ip(aenv):
    ipb = aenv["ipb"]

    def panel(ip, key=PK_IP):
        return {"x-nexus-proxy-key": key, "x-nexus-client-ip": ip}

    # IP real enviada por el panel con la clave compartida → límite por IP (5).
    codes = [requests.post(ipb.url + "/admin/auth/login", headers=panel("203.0.113.7"),
                           json={"email": email_for(f"u{i}_" + uuid.uuid4().hex[:4]), "password": "w" * 14},
                           timeout=T).status_code for i in range(6)]
    assert codes == [401] * 5 + [429]
    good = {"email": email_for("viewer_a"), "password": aenv["users"]["viewer_a"]["password"]}
    r = requests.post(ipb.url + "/admin/auth/login", headers=panel("203.0.113.7"), json=good, timeout=T)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_attempts"
    # Solo esa IP: otro usuario desde otra IP entra (no hay bloqueo global).
    assert requests.post(ipb.url + "/admin/auth/login", headers=panel("203.0.113.8"), json=good,
                         timeout=T).status_code == 200
    # X-Forwarded-For del cliente NO cuenta (ni rotándolo evade nada, ni se usa como IP), y una clave
    # incorrecta hace que se ignore x-nexus-client-ip: IP compartida/desconocida → sin límite por IP
    # (solo bloqueo por usuario), así los fallos de otros nunca bloquean a todos.
    codes = [requests.post(ipb.url + "/admin/auth/login",
                           headers={"x-forwarded-for": f"198.51.100.{i}", **panel("203.0.113.7", "mala")},
                           json={"email": email_for(f"spray{i}_" + uuid.uuid4().hex[:4]), "password": "w" * 14},
                           timeout=T).status_code for i in range(40)]
    assert codes == [401] * 40
    assert requests.post(ipb.url + "/admin/auth/login", json=good, timeout=T).status_code == 200
    # El bloqueo por usuario sigue funcionando con IP desconocida (max_failed_attempts=100 aquí: se
    # prueba en test_bloqueo_por_usuario_con_backoff_exponencial).


def test_ip_de_sesion_solo_con_clave_del_panel(aenv):
    b = aenv["b"]
    p = aenv["users"]["viewer_a"]["password"]

    def last_ip():
        return q("SELECT ip FROM panel_session ORDER BY id DESC LIMIT 1")[0][0]

    requests.post(b.url + "/admin/auth/login", json={"email": email_for("viewer_a"), "password": p}, timeout=T,
                  headers={"x-nexus-proxy-key": PK_B, "x-nexus-client-ip": "198.51.100.4"})
    assert last_ip() == "198.51.100.4"
    requests.post(b.url + "/admin/auth/login", json={"email": email_for("viewer_a"), "password": p}, timeout=T,
                  headers={"x-forwarded-for": "6.6.6.6", "x-nexus-client-ip": "7.7.7.7"})
    assert last_ip() == "127.0.0.1"
    requests.post(b.url + "/admin/auth/login", json={"email": email_for("viewer_a"), "password": p}, timeout=T,
                  headers={"x-nexus-proxy-key": PK_B, "x-nexus-client-ip": "no-es-ip"})
    assert last_ip() == ""


def test_tiempo_similar_usuario_inexistente(aenv):
    b = aenv["b"]

    def t(email):
        t0 = time.perf_counter()
        requests.post(b.url + "/admin/auth/login", json={"email": email, "password": "q" * 14}, timeout=T)
        return time.perf_counter() - t0

    p = pw()
    b.admin("POST", "/admin/users", {"username": "timing_u", "email": email_for("timing_u"), "password": p,
                                     "must_change_password": False}, expect=201)
    known = sorted(t(email_for("timing_u")) for _ in range(4))[1:3]
    unknown = sorted(t(email_for("nadie_" + uuid.uuid4().hex[:6])) for _ in range(4))[1:3]
    ratio = (sum(unknown) / 2) / (sum(known) / 2)
    # Ambos verifican un hash argon2id (≈ decenas de ms): sin atajo para usuarios inexistentes.
    assert 0.5 < ratio < 2.0, (known, unknown)
    assert min(unknown) > 0.01


def test_sesion_vence_por_inactividad_y_absoluto(aenv):
    b = aenv["b"]
    p = aenv["users"]["viewer_a"]["password"]
    t1 = login(b.url, "viewer_a", p)["token"]
    assert requests.get(b.url + "/admin/auth/me", headers=bearer(t1), timeout=T).status_code == 200
    q("UPDATE panel_session SET last_seen_at = NOW() - INTERVAL '31 minutes' WHERE token_hash = %s",
      (hashlib.sha256(t1.encode()).hexdigest(),))
    assert requests.get(b.url + "/admin/auth/me", headers=bearer(t1), timeout=T).status_code == 401
    t2 = login(b.url, "viewer_a", p)["token"]
    q("UPDATE panel_session SET expires_at = NOW() - INTERVAL '1 second' WHERE token_hash = %s",
      (hashlib.sha256(t2.encode()).hexdigest(),))
    r = requests.get(b.url + "/admin/groups", headers=bearer(t2), timeout=T)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "session_invalid"


def test_logout_revoca_la_sesion(aenv):
    b = aenv["b"]
    tok = login(b.url, "viewer_a", aenv["users"]["viewer_a"]["password"])["token"]
    assert requests.post(b.url + "/admin/auth/logout", headers=bearer(tok), timeout=T).status_code == 200
    assert requests.get(b.url + "/admin/auth/me", headers=bearer(tok), timeout=T).status_code == 401
    assert q("SELECT revoked_reason FROM panel_session WHERE token_hash = %s",
             (hashlib.sha256(tok.encode()).hexdigest(),))[0][0] == "logout"


def test_politica_y_cambio_de_contrasena(aenv):
    b = aenv["b"]
    p = pw()
    b.admin("POST", "/admin/users", {"username": "cambia", "email": email_for("cambia"), "password": p,
                                     "must_change_password": False}, expect=201)
    t1 = login(b.url, "cambia", p)["token"]
    t2 = login(b.url, "cambia", p)["token"]

    def change(cur, new):
        return requests.post(b.url + "/admin/auth/change-password", headers=bearer(t1), timeout=T,
                             json={"current_password": cur, "new_password": new})

    assert change(p, "corta1").status_code == 422
    assert change(p, "xxcambiaxx-123456").status_code == 422               # contiene el usuario
    assert change(p, "password1234").status_code == 422                     # común
    assert change(p, "aaaaaaaaaaaaaaaa").status_code == 422                 # predecible
    assert change(p, p).status_code == 422                                  # igual a la actual
    assert change("no-es-la-actual-123", pw()).status_code == 401
    new = pw()
    assert change(p, new).status_code == 200
    # La sesión actual sigue; las demás se cierran.
    assert requests.get(b.url + "/admin/auth/me", headers=bearer(t1), timeout=T).status_code == 200
    assert requests.get(b.url + "/admin/auth/me", headers=bearer(t2), timeout=T).status_code == 401
    assert login(b.url, "cambia", new)["user"]["username"] == "cambia"
    # Crear usuario con contraseña débil también se rechaza.
    r = requests.post(b.url + "/admin/users", headers={"x-admin-token": b.admin_token}, timeout=T,
                      json={"username": "debil", "email": email_for("debil"), "password": "123456789012"})
    assert r.status_code == 422


def test_must_change_password_bloquea_lo_demas(aenv):
    b = aenv["b"]
    p = pw()
    b.admin("POST", "/admin/users", {"username": "nuevo", "email": email_for("nuevo"), "password": p,
                                     "roles": [{"role": "lectura", "group_id": aenv["A"]}]}, expect=201)
    d = login(b.url, "nuevo", p)
    assert d["must_change_password"] is True
    h = bearer(d["token"])
    r = requests.get(b.url + "/admin/groups", headers=h, timeout=T)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "password_change_required"
    assert requests.get(b.url + "/admin/auth/me", headers=h, timeout=T).status_code == 200
    r = requests.post(b.url + "/admin/auth/change-password", headers=h, timeout=T,
                      json={"current_password": p, "new_password": pw()})
    assert r.status_code == 200
    assert requests.get(b.url + "/admin/groups", headers=h, timeout=T).status_code == 200


def test_correo_duplicado_409_y_formato_invalido_422(aenv):
    b = aenv["b"]
    dup_email = email_for("correo_dup_" + uuid.uuid4().hex[:6])
    b.admin("POST", "/admin/users", {"username": "dupu1", "email": dup_email, "password": pw(),
                                     "must_change_password": False}, expect=201)
    # Mismo correo (sin distinguir mayúsculas) al crear otro usuario → 409.
    r = requests.post(b.url + "/admin/users", headers={"x-admin-token": b.admin_token}, timeout=T,
                      json={"username": "dupu2", "email": dup_email.upper(), "password": pw()})
    assert r.status_code == 409
    # Formato de correo inválido → 422.
    r = requests.post(b.url + "/admin/users", headers={"x-admin-token": b.admin_token}, timeout=T,
                      json={"username": "malcorreo", "email": "no-es-un-correo", "password": pw()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_email"
    # Editar el correo de OTRO usuario al mismo correo ya usado → también 409.
    other = b.admin("POST", "/admin/users", {"username": "otrou", "email": email_for("otrou_" + uuid.uuid4().hex[:6]),
                                             "password": pw(), "must_change_password": False}, expect=201)
    r2 = requests.put(b.url + f"/admin/users/{other['id']}", headers={"x-admin-token": b.admin_token}, timeout=T,
                      json={"email": dup_email})
    assert r2.status_code == 409
    # El usuario que ya tenía ese correo no se vio afectado.
    assert q("SELECT COUNT(*) FROM panel_user WHERE lower(email) = %s", (dup_email.lower(),))[0][0] == 1


def test_usuario_sin_correo_no_puede_iniciar_sesion_hasta_que_se_le_asigne(aenv):
    """Un usuario preexistente sin correo (caso típico tras la migración 011, antes de que un
    administrador lo complete) no puede iniciar sesión con ninguna combinación de correo/contraseña;
    en cuanto se le asigna uno (PUT /admin/users/{id}), sí puede."""
    b = aenv["b"]
    from argon2 import PasswordHasher
    ph = PasswordHasher()
    raw_pw = pw()
    uname = "sin_correo_" + uuid.uuid4().hex[:6]
    uid = q("""INSERT INTO panel_user (username, email, password_hash, must_change_password, created_by)
               VALUES (%s, NULL, %s, FALSE, 'test') RETURNING id""", (uname, ph.hash(raw_pw)))[0][0]
    assert q("SELECT email FROM panel_user WHERE id = %s", (uid,))[0][0] is None
    guess_email = email_for(uname)
    r = requests.post(b.url + "/admin/auth/login", json={"email": guess_email, "password": raw_pw}, timeout=T)
    assert r.status_code == 401
    b.admin("PUT", f"/admin/users/{uid}", {"email": guess_email}, expect=200)
    d = login(b.url, uname, raw_pw, email=guess_email)
    assert d["user"]["username"] == uname


def test_token_estatico_deshabilitado_por_defecto_y_auditado(aenv):
    from panel_auth import AuthSettings
    import configparser
    assert AuthSettings.from_ini(configparser.ConfigParser(), "x" * 40).allow_static_token is False
    lim, b = aenv["lim"], aenv["b"]
    r = requests.get(lim.url + "/admin/groups", headers={"x-admin-token": lim.admin_token}, timeout=T)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "static_token_disabled"
    # Habilitado (solo pruebas/emergencia): cada uso, también las lecturas, queda auditado.
    marker = uuid.uuid4().hex[:8]
    requests.get(b.url + f"/admin/groups?x={marker}", headers={"x-admin-token": b.admin_token}, timeout=T)
    rows = q("""SELECT actor_name, auth_kind FROM panel_audit_log WHERE action = 'GET /admin/groups'
                AND auth_kind = 'static_token'""")
    assert rows and rows[0] == ("token-admin", "static_token")
    r = requests.get(b.url + "/admin/groups", headers={"x-admin-token": "otro-" + "x" * 40}, timeout=T)
    assert r.status_code == 401


def test_cli_crea_superadmin(aenv):
    b = aenv["b"]
    p = pw()
    raiz_email = email_for("raiz_cli_" + uuid.uuid4().hex[:6])
    env = dict(os.environ, NEXUS_CONFIG_FILE=b.ini_path, NX_TEST_PW=p)
    out = subprocess.run([support.BACK_PY, "manage_users.py", "create-superadmin", "--email", raiz_email,
                          "--password-env", "NX_TEST_PW"], cwd=support.BACK_DIR, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert p not in out.stdout + out.stderr
    assert raiz_email in out.stdout
    d = login(b.url, "raiz", p, email=raiz_email)
    assert d["user"]["is_superadmin"] is True and d["must_change_password"] is True
    # Correo duplicado y contraseña débil → error (sin tocar la BD).
    assert subprocess.run([support.BACK_PY, "manage_users.py", "create-superadmin", "--email", raiz_email,
                           "--password-env", "NX_TEST_PW"], cwd=support.BACK_DIR, env=env,
                          capture_output=True, text=True, timeout=60).returncode != 0
    env["NX_TEST_PW"] = "123456789012"
    assert subprocess.run([support.BACK_PY, "manage_users.py", "create-superadmin",
                           "--email", email_for("raiz2_" + uuid.uuid4().hex[:6]),
                           "--password-env", "NX_TEST_PW"], cwd=support.BACK_DIR, env=env,
                          capture_output=True, text=True, timeout=60).returncode != 0


def test_cli_username_se_deriva_del_correo_y_set_email(aenv):
    """El usuario (identificador interno) se deriva de la parte local del correo si no se indica
    --username; `set-email` cambia el correo de acceso de una cuenta existente."""
    b = aenv["b"]
    env = dict(os.environ, NEXUS_CONFIG_FILE=b.ini_path)
    local = "cli.derivado." + uuid.uuid4().hex[:6]
    email1 = local + EMAIL_DOMAIN
    p = pw()
    env["NX_TEST_PW"] = p
    out = subprocess.run([support.BACK_PY, "manage_users.py", "create-superadmin", "--email", email1,
                          "--password-env", "NX_TEST_PW"], cwd=support.BACK_DIR, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    row = q("SELECT username FROM panel_user WHERE lower(email) = %s", (email1.lower(),))
    assert row and row[0][0]
    derived_username = row[0][0]
    assert derived_username == local

    email2 = "cli.nuevo." + uuid.uuid4().hex[:6] + EMAIL_DOMAIN
    out2 = subprocess.run([support.BACK_PY, "manage_users.py", "set-email", "--username", derived_username,
                          "--new-email", email2], cwd=support.BACK_DIR, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out2.returncode == 0, out2.stderr
    assert q("SELECT email FROM panel_user WHERE username = %s", (derived_username,))[0][0] == email2
    # El correo anterior ya no sirve para iniciar sesión; el nuevo sí.
    r_old = requests.post(b.url + "/admin/auth/login", json={"email": email1, "password": p}, timeout=T)
    assert r_old.status_code == 401
    d = login(b.url, derived_username, p, email=email2)
    assert d["user"]["username"] == derived_username
    # `list` incluye el correo.
    out3 = subprocess.run([support.BACK_PY, "manage_users.py", "list"], cwd=support.BACK_DIR, env=env,
                          capture_output=True, text=True, timeout=60)
    assert out3.returncode == 0 and email2 in out3.stdout


# ─────────────────────────────────────────────────────────────────────────────
# Todas las rutas /admin declaran permiso
# ─────────────────────────────────────────────────────────────────────────────
def test_todas_las_rutas_admin_declaran_permiso(aenv):
    os.environ["NEXUS_CONFIG_FILE"] = aenv["b"].ini_path
    sys.path.insert(0, support.BACK_DIR)
    main = importlib.import_module("main_postgres")
    from fastapi.routing import APIRoute
    from panel_auth import AUTHENTICATED, PERMISSIONS, PUBLIC, dependency_permissions
    admin_routes = [r for r in main.app.routes if isinstance(r, APIRoute) and r.path.startswith("/admin")]
    assert len(admin_routes) > 90
    missing, public = [], []
    for r in admin_routes:
        perms = dependency_permissions(r.dependant)
        if not perms:
            missing.append(f"{sorted(r.methods)} {r.path}")
        for p in perms:
            assert p in PERMISSIONS or p in (PUBLIC, AUTHENTICATED), (r.path, p)
            if p == PUBLIC:
                public.append(r.path)
        # Las mutaciones nunca quedan solo con "authenticated" (salvo la propia sesión).
        if r.methods & {"POST", "PUT", "DELETE", "PATCH"} and perms == [AUTHENTICATED]:
            assert r.path in ("/admin/auth/logout", "/admin/auth/change-password"), r.path
    assert not missing, "Rutas /admin sin permiso declarado: " + ", ".join(missing)
    assert public == ["/admin/auth/login"]
    main.DB_POOL.closeall()


# ─────────────────────────────────────────────────────────────────────────────
# Matriz de permisos (acción representativa por permiso)
# ─────────────────────────────────────────────────────────────────────────────
def test_matriz_de_permisos(aenv):
    e = aenv
    A, ids = e["A"], e["ids"]
    ack_body = {"attribution": "client", "expected_version": 999, "expected_observed_fingerprint": "a" * 64}
    appr_body = {"expected_snapshot_id": 1}
    matrix = [
        # (usuario, método, ruta, cuerpo, esperado)
        ("viewer_a", "GET", "/admin/incidents", None, 200),
        ("viewer_a", "PUT", f"/admin/incidents/{e['inc_a']}/ack", {}, 403),
        ("op_ab", "PUT", f"/admin/incidents/{e['inc_a']}/ack", {"comment": "visto"}, 200),
        ("op_ab", "PUT", f"/admin/incidents/{e['inc_b']}/ack", {}, 403),            # B solo lectura
        ("op_ab", "PUT", f"/admin/incidents/{e['inc_a']}/resolve", {"reason": "no aplica"}, 409),  # pasa permiso
        ("viewer_a", "PUT", f"/admin/incidents/{e['inc_a']}/resolve", {"reason": "no aplica"}, 403),
        ("viewer_a", "POST", f"/admin/structural-changes/{e['ch_a']}/acknowledge", ack_body, 403),
        ("attr_a", "POST", f"/admin/structural-changes/{e['ch_a']}/acknowledge", ack_body, 409),  # stale → pasó
        ("attr_a", "POST", f"/admin/structural-changes/{e['ch_s']}/acknowledge", ack_body, 403),  # compartida A+B
        ("attr_a", "POST", f"/admin/structural-changes/{e['ch_a']}/reclassify",
         {"attribution": "nexus", "reason": "motivo largo", "expected_version": 1}, 409),
        ("op_ab", "POST", f"/admin/structural-changes/{e['ch_a']}/reclassify",
         {"attribution": "nexus", "reason": "motivo largo", "expected_version": 1}, 403),
        ("appr_a", "POST", f"/admin/monitored-databases/{e['mdb_a']}/baseline/approve", appr_body, 409),
        ("attr_a", "POST", f"/admin/monitored-databases/{e['mdb_a']}/baseline/approve", appr_body, 403),
        ("appr_a", "PUT", f"/admin/monitored-databases/{e['mdb_a']}", {"scan_interval_seconds": 7200}, 200),
        ("viewer_a", "PUT", f"/admin/monitored-databases/{e['mdb_a']}", {"scan_interval_seconds": 7200}, 403),
        ("defs_a", "GET", f"/admin/structural-changes/{e['ch_a']}/definitions", None, 200),
        ("viewer_a", "GET", f"/admin/structural-changes/{e['ch_a']}/definitions", None, 403),
        ("creds_a", "POST", f"/admin/groups/{A}/regenerate-token", None, 200),
        ("creds_a", "PUT", f"/admin/groups/{A}", {"warehouse_host": "dwh-a.invalid"}, 200),
        ("creds_a", "PUT", f"/admin/groups/{A}", {"name": "Grupo A2"}, 403),              # config, no credenciales
        ("creds_a", "POST", f"/admin/installations/{e['ia']}/rotate", None, 200),
        ("cfg_a", "POST", f"/admin/groups/{A}/regenerate-token", None, 403),
        ("cfg_a", "PUT", f"/admin/groups/{A}", {"warehouse_password": "nueva-secreta-xyz"}, 403),
        ("cfg_a", "PUT", f"/admin/tasks/{ids['t_cli']['id']}", {"schedule_seconds": 1800}, 200),
        ("cfg_a", "POST", "/admin/groups", {"name": "Nuevo G"}, 403),                     # solo alcance global
        ("viewer_a", "PUT", f"/admin/tasks/{ids['t_cli']['id']}", {"schedule_seconds": 1800}, 403),
        ("cfg_a", "POST", f"/admin/installations/{e['ia']}/revoke", {"reason": "x"}, 403),
        ("creds_a", "PUT", f"/admin/notification-channels/{e['chan']['canal-a']}", {"min_severity": "error"}, 200),
        ("creds_a", "POST", "/admin/notification-channels", {"name": "g-" + uuid.uuid4().hex[:5], "kind": "log"}, 403),
        ("cfg_a", "PUT", f"/admin/notification-channels/{e['chan']['canal-a']}", {"min_severity": "error"}, 403),
        ("viewer_a", "POST", "/admin/health/evaluate", None, 403),
        ("cfg_a", "POST", "/admin/health/evaluate", None, 403),                          # config.manage GLOBAL
        ("viewer_a", "GET", "/admin/users", None, 403),
        ("uadmin", "GET", "/admin/users", None, 200),
        ("viewer_a", "GET", "/admin/audit", None, 403),
        ("auditor_a", "GET", "/admin/audit", None, 200),
        ("op_ab", "PUT", "/admin/events/ack-all", None, 200),
        ("viewer_a", "PUT", "/admin/events/ack-all", None, 403),
        ("super", "POST", "/admin/health/evaluate", None, 200),
    ]
    bad = []
    for who, method, path, body, expected in matrix:
        r = call(e, who, method, path, body)
        if r.status_code != expected:
            bad.append(f"{who} {method} {path} -> {r.status_code} (esperado {expected}): {r.text[:160]}")
    assert not bad, "\n".join(bad)
    # 403 con código y permiso explícito
    r = call(e, "viewer_a", "PUT", f"/admin/incidents/{e['inc_a']}/ack", {})
    assert r.json()["detail"]["code"] == "permission_required"
    assert r.json()["detail"]["permission"] == "incident.acknowledge"
    # Actor real en el reconocimiento
    inc = call(e, "op_ab", "GET", f"/admin/incidents/{e['inc_a']}", expect=200).json()
    assert inc["acknowledged_by"] == "op_ab"
    assert q("SELECT acknowledged_by_user_id FROM incident WHERE id = %s", (e["inc_a"],))[0][0] == \
        e["users"]["op_ab"]["id"]
    # ack-all del operador solo tocó el grupo A
    assert q("SELECT is_acknowledged FROM client_events WHERE group_id = %s AND event_type = 'error'",
             (e["B"],))[0][0] == 0
    assert q("SELECT acknowledged_by FROM client_events WHERE group_id = %s AND event_type = 'error'",
             (e["A"],))[0][0] == "op_ab"


def test_secretos_y_tokens_solo_con_credentials_manage(aenv):
    e = aenv
    A = e["A"]
    g_view = call(e, "viewer_a", "GET", f"/admin/groups/{A}", expect=200).json()
    assert g_view["warehouse_host"] is None and g_view["group_token"] is None and g_view["secrets_hidden"]
    assert g_view["has_token"] is True
    g_cred = call(e, "creds_a", "GET", f"/admin/groups/{A}", expect=200).json()
    assert g_cred["warehouse_host"] and g_cred["group_token"] and not g_cred["secrets_hidden"]
    c_view = call(e, "cfg_a", "GET", f"/admin/companies/{e['ids']['company_a']['id']}", expect=200).json()
    assert c_view["source_host"] is None and c_view["company_token"] is None
    for a in call(e, "viewer_a", "GET", "/admin/agencies", expect=200).json()["items"]:
        assert a["agency_token"] is None
    assert support.DWH["password"] not in str(g_cred) and "warehouse_password" not in g_cred


# ─────────────────────────────────────────────────────────────────────────────
# Aislamiento entre grupos
# ─────────────────────────────────────────────────────────────────────────────
def test_aislamiento_listas_y_agregados(aenv):
    e = aenv
    A, B = e["A"], e["B"]
    lists = {
        "/admin/groups": "items", "/admin/companies": "items", "/admin/agencies": "items",
        "/admin/objects": "items", "/admin/tasks": "items", "/admin/installations": "items",
        "/admin/executions": "items", "/admin/sync-state": "items", "/admin/incidents": "items",
        "/admin/notification-channels": "items", "/admin/monitored-databases": "items",
        "/admin/structural-changes": "items", "/admin/events": "items", "/admin/clients": "clients",
        "/admin/activity?only_errors=false": "items", "/admin/health/tasks": "items",
        "/admin/health/installations": "items", "/admin/legacy-clients": "items",
        "/admin/notification-deliveries": "items",
    }
    super_counts = {}
    for path, key in lists.items():
        allr = call(e, "super", "GET", path, expect=200).json()[key]
        mine = call(e, "viewer_a", "GET", path, expect=200).json()[key]
        super_counts[path] = len(allr)
        text = str(mine)
        assert "Grupo B" not in text and "Empresa B1" not in text and "canal-b" not in text, path
        for it in mine:
            gid = it.get("group_id", it.get("id") if path == "/admin/groups" else A)
            assert gid in (A, None) or path in ("/admin/monitored-databases",), (path, it)
            if path == "/admin/monitored-databases":
                assert A in [lk["group_id"] for lk in it["links"]] and B not in [lk["group_id"] for lk in it["links"]]
        if path in ("/admin/groups", "/admin/companies", "/admin/incidents", "/admin/installations",
                    "/admin/structural-changes", "/admin/notification-channels", "/admin/events"):
            assert len(mine) < len(allr), path
    assert "canal-global" not in str(call(e, "viewer_a", "GET", "/admin/notification-channels").json())
    # El operador A+B ve ambos.
    names = {g["name"] for g in call(e, "op_ab", "GET", "/admin/groups", expect=200).json()["items"]}
    assert {"Grupo B"} <= names
    # Agregados
    st = call(e, "viewer_a", "GET", "/admin/stats", expect=200).json()
    assert st["groups"] == 1 and st["companies"] == 1
    assert call(e, "super", "GET", "/admin/stats").json()["groups"] >= 2
    hs = call(e, "viewer_a", "GET", "/admin/health/summary", expect=200).json()
    n_a = len(call(e, "super", "GET", f"/admin/incidents?view=open&group_id={A}").json()["items"])
    assert hs["incidents"]["open_total"] == n_a
    assert sum(hs["installations"].values()) == 1
    badge = call(e, "viewer_a", "GET", "/admin/incidents/badge", expect=200).json()
    assert badge["open_total"] == n_a
    sb = call(e, "viewer_a", "GET", "/admin/structural-changes/badge", expect=200).json()
    assert sb["pending_changes"] == 2        # la de A + la del DWH compartido
    assert call(e, "super", "GET", "/admin/structural-changes/badge").json()["pending_changes"] == 3
    inv = call(e, "viewer_a", "GET", "/admin/inventory/summary", expect=200).json()
    assert inv["databases"] == 2


def test_aislamiento_detalle_y_mutaciones_404(aenv):
    e = aenv
    ids, B = e["ids"], e["B"]
    tb = ids["t_b"]["id"]
    detail_404 = [
        f"/admin/groups/{B}", f"/admin/companies/{ids['company_b']['id']}", f"/admin/agencies/{ids['agency_b1']['id']}",
        f"/admin/tasks/{tb}", f"/admin/installations/{e['ib']}", f"/admin/incidents/{e['inc_b']}",
        f"/admin/monitored-databases/{e['mdb_b']}", f"/admin/monitored-databases/{e['mdb_b']}/baseline",
        f"/admin/monitored-databases/{e['mdb_b']}/baseline/history", f"/admin/structural-changes/{e['ch_b']}",
    ]
    objs_b = call(e, "super", "GET", f"/admin/objects?group_id={B}").json()["items"]
    detail_404.append(f"/admin/objects/{objs_b[0]['id']}")
    for path in detail_404:
        r = call(e, "viewer_a", "GET", path)
        assert r.status_code == 404, (path, r.status_code)
        # Mismo resultado que un id inexistente
    assert call(e, "viewer_a", "GET", "/admin/tasks/999999").status_code == 404
    assert call(e, "defs_a", "GET", f"/admin/structural-changes/{e['ch_b']}/definitions").status_code == 404
    mut_404 = [
        ("cfg_a", "PUT", f"/admin/groups/{B}", {"name": "hack"}),
        ("cfg_a", "POST", f"/admin/tasks/{tb}/disable", None),
        ("cfg_a", "PUT", f"/admin/tasks/{tb}", {"schedule_seconds": 60}),
        ("cfg_a", "DELETE", f"/admin/tasks/{tb}", None),
        ("cfg_a", "POST", f"/admin/objects/{objs_b[0]['id']}/disable", None),
        ("cfg_a", "POST", "/admin/agencies", {"company_id": ids["company_b"]["id"], "name": "intrusa"}),
        ("cfg_a", "POST", "/admin/tasks", {"agency_id": ids["agency_b1"]["id"], "object_catalog_id": objs_b[0]["id"],
                                           "extract_sql": "SELECT 1"}),
        ("creds_a", "POST", f"/admin/groups/{B}/regenerate-token", None),
        ("creds_a", "POST", f"/admin/installations/{e['ib']}/revoke", {"reason": "x"}),
        ("creds_a", "PUT", f"/admin/notification-channels/{e['chan']['canal-b']}", {"min_severity": "error"}),
        ("creds_a", "DELETE", f"/admin/notification-channels/{e['chan']['canal-global']}", None),
        ("attr_a", "POST", f"/admin/structural-changes/{e['ch_b']}/acknowledge",
         {"attribution": "client", "expected_version": 1, "expected_observed_fingerprint": "a" * 64}),
        ("appr_a", "POST", f"/admin/monitored-databases/{e['mdb_b']}/baseline/reset", {"reason": "intento B"}),
        ("appr_a", "POST", f"/admin/monitored-databases/{e['mdb_b']}/scan", None),
        ("appr_a", "POST", "/admin/monitored-databases", {"kind": "dwh", "group_id": B}),
        ("op_ab", "PUT", "/admin/events/999999/ack", None),
    ]
    for who, method, path, body in mut_404:
        r = call(e, who, method, path, body)
        assert r.status_code == 404, (who, method, path, r.status_code, r.text[:200])
    # Nada cambió en B
    assert call(e, "super", "GET", f"/admin/tasks/{tb}").json()["is_active"] is True
    assert call(e, "super", "GET", f"/admin/installations/{e['ib']}").json()["status"] == "active"
    assert call(e, "super", "GET", f"/admin/groups/{B}").json()["name"] == "Grupo B"


def test_vista_grupo_y_agencia_por_alcance(aenv):
    """Vistas de detalle del panel (/grupos/[id], /agencias/[id]): usan los filtros group_id/agency_id
    de las listas existentes; fuera del alcance → 404 en el detalle y listas vacías (nunca datos de B)."""
    e = aenv
    ids, A, B = e["ids"], e["A"], e["B"]
    aa1, ab1 = ids["agency_a1"]["id"], ids["agency_b1"]["id"]
    # Dentro del alcance: detalle + extractores de la agencia y del grupo.
    ag = call(e, "viewer_a", "GET", f"/admin/agencies/{aa1}", expect=200).json()
    assert ag["group_id"] == A and ag["company_id"] == ids["company_a"]["id"] and ag["agency_token"] is None
    ht = call(e, "viewer_a", "GET", f"/admin/health/tasks?agency_id={aa1}", expect=200).json()["items"]
    assert ht and {t["agency_id"] for t in ht} == {aa1}
    assert {t["task_id"] for t in ht} == {t["id"] for t in call(e, "viewer_a", "GET", f"/admin/tasks?agency_id={aa1}",
                                                                   expect=200).json()["items"]}
    hg = call(e, "viewer_a", "GET", f"/admin/health/tasks?group_id={A}", expect=200).json()["items"]
    assert {t["group_id"] for t in hg} == {A} and len(hg) > len(ht)
    assert all(x["agency_id"] == aa1 for x in
               call(e, "viewer_a", "GET", f"/admin/executions?agency_id={aa1}", expect=200).json()["items"])
    # Fuera del alcance: detalle 404 (igual que inexistente) y listas filtradas vacías.
    for path in (f"/admin/groups/{B}", f"/admin/agencies/{ab1}", "/admin/groups/999999", "/admin/agencies/999999"):
        assert call(e, "viewer_a", "GET", path).status_code == 404, path
    for path in (f"/admin/health/tasks?group_id={B}", f"/admin/health/tasks?agency_id={ab1}",
                 f"/admin/tasks?group_id={B}", f"/admin/tasks?agency_id={ab1}", f"/admin/agencies?group_id={B}",
                 f"/admin/executions?agency_id={ab1}", f"/admin/incidents?agency_id={ab1}&view=open"):
        assert call(e, "viewer_a", "GET", path, expect=200).json()["items"] == [], path
    # El superadministrador sí ve B con los mismos filtros (el filtro funciona, no es solo el alcance).
    assert call(e, "super", "GET", f"/admin/health/tasks?agency_id={ab1}", expect=200).json()["items"]
    assert call(e, "super", "GET", f"/admin/incidents?agency_id={ab1}&view=open", expect=200).json()["items"]
    # El lector no puede activar/desactivar extractores (403), config.manage de A sí; en B → 404.
    # (t_dups ya está inactiva: se activa y se regresa, sin cerrar incidencias de otras pruebas.)
    t_dups = ids["t_dups"]["id"]
    assert call(e, "viewer_a", "POST", f"/admin/tasks/{t_dups}/enable").status_code == 403
    assert call(e, "cfg_a", "POST", f"/admin/tasks/{t_dups}/enable", expect=200).json()["is_active"] is True
    assert call(e, "cfg_a", "POST", f"/admin/tasks/{t_dups}/disable", expect=200).json()["is_active"] is False
    assert call(e, "cfg_a", "POST", f"/admin/tasks/{ids['t_b']['id']}/disable").status_code == 404


def test_auditoria_por_alcance(aenv):
    e = aenv
    # op_ab reconoció en A (test de matriz) y cfg_a intentó en B (404): la auditoría tiene grupo.
    call(e, "cfg_a", "PUT", f"/admin/tasks/{e['ids']['t_cli']['id']}", {"schedule_seconds": 1700}, expect=200)
    items = call(e, "auditor_a", "GET", "/admin/audit?limit=2000", expect=200).json()["items"]
    assert items and all(i["group_id"] == e["A"] for i in items)
    assert any(i["action"] == "PUT /admin/tasks/{task_id}" and i["actor_name"] == "cfg_a" for i in items)
    alls = call(e, "super", "GET", "/admin/audit?limit=2000", expect=200).json()["items"]
    assert any(i["group_id"] is None for i in alls) and len(alls) > len(items)
    # Los intentos fallidos (404/403) también quedan registrados con su código.
    assert any(i["status_code"] in (403, 404) for i in alls if i["actor_name"] in ("cfg_a", "creds_a"))


def test_admin_de_usuarios(aenv):
    e = aenv
    b = e["b"]
    # users.manage no se asigna por grupo
    r = call(e, "uadmin", "POST", "/admin/users", {"username": "x_" + uuid.uuid4().hex[:5],
                                                   "email": email_for("x_" + uuid.uuid4().hex[:5]), "password": pw(),
                                                   "roles": [{"role": "admin_usuarios", "group_id": e["A"]}]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "global_only_role"
    # uadmin (no superadmin) no crea superadmins
    r = call(e, "uadmin", "POST", "/admin/users", {"username": "y_" + uuid.uuid4().hex[:5],
                                                   "email": email_for("y_" + uuid.uuid4().hex[:5]), "password": pw(),
                                                   "is_superadmin": True})
    assert r.status_code == 403
    # Desactivar cierra sus sesiones; no puede desactivarse a sí mismo.
    p = pw()
    u = call(e, "uadmin", "POST", "/admin/users", {"username": "temporal", "email": email_for("temporal"),
                                                   "password": p, "must_change_password": False,
                                                   "roles": [{"role": "lectura", "group_id": e["B"]}]},
             expect=201).json()
    tok = login(b.url, "temporal", p)["token"]
    assert requests.get(b.url + "/admin/groups", headers=bearer(tok), timeout=T).status_code == 200
    call(e, "uadmin", "PUT", f"/admin/users/{u['id']}", {"is_active": False}, expect=200)
    assert requests.get(b.url + "/admin/groups", headers=bearer(tok), timeout=T).status_code == 401
    r = requests.post(b.url + "/admin/auth/login", json={"email": email_for("temporal"), "password": p}, timeout=T)
    assert r.status_code == 401
    assert call(e, "uadmin", "PUT", f"/admin/users/{e['users']['uadmin']['id']}", {"is_active": False}).status_code == 409
    # Reinicio de contraseña → must_change_password y sesiones cerradas
    call(e, "uadmin", "PUT", f"/admin/users/{u['id']}", {"is_active": True}, expect=200)
    p2 = pw()
    d = call(e, "uadmin", "POST", f"/admin/users/{u['id']}/reset-password", {"password": p2}, expect=200).json()
    assert d["must_change_password"] is True
    assert login(b.url, "temporal", p2)["must_change_password"] is True
    # Roles por alcance y revocación de sesión por el administrador
    call(e, "uadmin", "PUT", f"/admin/users/{u['id']}/roles",
         {"roles": [{"role": "operador", "group_id": e["A"]}, {"role": "lectura", "group_id": None}]}, expect=200)
    sess = call(e, "uadmin", "GET", f"/admin/sessions?user_id={u['id']}", expect=200).json()["items"]
    assert sess
    call(e, "uadmin", "POST", f"/admin/sessions/{sess[0]['id']}/revoke", expect=200)
    roles = call(e, "uadmin", "GET", "/admin/roles", expect=200).json()
    assert {"lectura", "operador", "admin_credenciales"} <= {r["code"] for r in roles["roles"]}


# ─────────────────────────────────────────────────────────────────────────────
# Límites del API del agente
# ─────────────────────────────────────────────────────────────────────────────
def test_limite_de_enrolamiento_por_ip_y_fallos(aenv):
    e = aenv
    lim, ipb = e["lim"], e["ipb"]
    tok = call(e, "super", "GET", f"/admin/groups/{e['A']}").json()["group_token"]  # (se regeneró en la matriz)
    codes = []
    h = None
    for i in range(6):
        r = requests.post(lim.url + "/agent/enroll", headers={"x-group-token": tok},
                          json={"name": f"rl{i}", "hostname": "h", "client_version": "t"}, timeout=T)
        codes.append(r.status_code)
        if r.status_code == 201 and h is None:
            d = r.json()
            h = {"x-installation-id": d["installation_id"], "x-installation-secret": d["secret"]}
    assert codes == [201] * 5 + [429]
    assert "retry-after" in {k.lower() for k in r.headers}
    # Fallidos por IP (backend ipb, enroll_fail_limit = 3): después ni un token válido pasa.
    bad = [requests.post(ipb.url + "/agent/enroll", headers={"x-group-token": "falso-" + uuid.uuid4().hex},
                         json={"name": "x", "hostname": "h", "client_version": "t"}, timeout=T).status_code
           for _ in range(3)]
    assert bad == [401, 401, 401]
    r = requests.post(ipb.url + "/agent/enroll", headers={"x-group-token": tok},
                      json={"name": "x", "hostname": "h", "client_version": "t"}, timeout=T)
    assert r.status_code == 429
    # Ráfaga de credenciales de instalación inválidas (auth_fail_limit = 4 en lim).
    for _ in range(4):
        r = requests.get(lim.url + "/agent/whoami", headers={"x-installation-id": str(uuid.uuid4()),
                                                             "x-installation-secret": "malo"}, timeout=T)
        assert r.status_code == 401
    r = requests.get(lim.url + "/agent/whoami", headers=h, timeout=T)
    assert r.status_code == 429
    # En otro proceso (b) la misma instalación sigue funcionando: el límite es por IP y proceso.
    assert requests.get(e["b"].url + "/agent/whoami", headers=h, timeout=T).status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Pool de conexiones
# ─────────────────────────────────────────────────────────────────────────────
def test_pool_100_peticiones_concurrentes_sin_agotar_conexiones(aenv):
    e = aenv
    lim = e["lim"]
    tok = login(lim.url, "op_ab", e["users"]["op_ab"]["password"])["token"]
    h = bearer(tok)
    peak = {"n": 0}
    stop = {"v": False}

    def watch():
        c = psycopg2.connect(host=support.CFG_PG["host"], port=support.CFG_PG["port"], user="postgres",
                             password=support.CFG_PG["password"], dbname="postgres")
        c.autocommit = True
        with c.cursor() as cur:
            while not stop["v"]:
                cur.execute("SELECT COUNT(*) FROM pg_stat_activity WHERE application_name = 'nexus_test_lim'")
                peak["n"] = max(peak["n"], cur.fetchone()[0])
                time.sleep(0.02)
        c.close()

    paths = ["/admin/groups", "/admin/incidents", "/admin/health/summary", "/admin/tasks", "/admin/stats"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=101) as ex:
        w = ex.submit(watch)
        futs = [ex.submit(requests.get, lim.url + paths[i % len(paths)], headers=h, timeout=60) for i in range(100)]
        codes = [f.result().status_code for f in futs]
        stop["v"] = True
        w.result()
    assert codes.count(200) == 100, codes
    assert 1 <= peak["n"] <= 5, peak
    assert requests.get(lim.url + "/health", timeout=T).status_code == 200


def test_dwh_compartido_no_revela_el_grupo_dueno(aenv):
    e = aenv
    b = e["b"]
    p = pw()
    b.admin("POST", "/admin/users", {"username": "viewer_b", "email": email_for("viewer_b"), "password": p,
                                     "must_change_password": False,
                                     "roles": [{"role": "lectura", "group_id": e["B"]}]}, expect=201)
    h = bearer(login(b.url, "viewer_b", p)["token"])
    items = requests.get(b.url + "/admin/monitored-databases", headers=h, timeout=T).json()["items"]
    shared = [m for m in items if m["id"] == e["mdb_s"]][0]
    # El dueño (grupo A) está fuera del alcance: no se nombra; los vínculos visibles son solo los de B.
    assert shared["group_id"] is None and shared["group_name"] is None and shared["owner_hidden"] is True
    assert [lk["group_id"] for lk in shared["links"]] == [e["B"]]
    assert shared["allowed_actions"]["acknowledge"] is False
    ch = requests.get(b.url + f"/admin/structural-changes/{e['ch_s']}", headers=h, timeout=T).json()
    assert ch["group_name"] is None and "Grupo A" not in str(ch)


def test_admin_de_usuarios_no_toca_superadmins_ni_sus_propios_roles(aenv):
    e = aenv
    b = e["b"]
    sup = e["users"]["super"]["id"]
    sup_tok = login(b.url, "super", e["users"]["super"]["password"])["token"]
    sid = q("SELECT id FROM panel_session WHERE token_hash = %s", (hashlib.sha256(sup_tok.encode()).hexdigest(),))[0][0]
    for method, path, body in (("PUT", f"/admin/users/{sup}", {"is_active": False}),
                               ("PUT", f"/admin/users/{sup}", {"display_name": "hackeado"}),
                               ("POST", f"/admin/users/{sup}/unlock", None),
                               ("PUT", f"/admin/users/{sup}/roles", {"roles": [{"role": "lectura"}]}),
                               ("POST", f"/admin/users/{sup}/reset-password", {"password": pw()}),
                               ("POST", f"/admin/sessions/{sid}/revoke", None)):
        r = call(e, "uadmin", method, path, body)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "superadmin_required", (path, r.text)
    assert requests.get(b.url + "/admin/auth/me", headers=bearer(sup_tok), timeout=T).status_code == 200
    assert q("SELECT is_active, display_name FROM panel_user WHERE id = %s", (sup,))[0][0] is True
    # Sus propios roles: no (evita auto-escalada) y queda auditado.
    r = call(e, "uadmin", "PUT", f"/admin/users/{e['users']['uadmin']['id']}/roles",
             {"roles": [{"role": "admin_usuarios"}, {"role": "admin_credenciales"}]})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "self_roles"
    assert q("SELECT COUNT(*) FROM panel_audit_log WHERE action = 'users.set_roles_self_denied'")[0][0] >= 1
    # Un superadmin sí puede.
    call(e, "super", "PUT", f"/admin/users/{e['users']['uadmin']['id']}/roles",
         {"roles": [{"role": "admin_usuarios"}]}, expect=200)


def test_contador_de_sesiones_excluye_inactivas(aenv):
    e = aenv
    uid = e["users"]["auditor_a"]["id"]
    login(e["b"].url, "auditor_a", e["users"]["auditor_a"]["password"])
    count = lambda: [u for u in call(e, "super", "GET", "/admin/users").json()["items"] if u["id"] == uid][0]["active_sessions"]  # noqa: E731
    n = count()
    assert n >= 1
    q("UPDATE panel_session SET last_seen_at = NOW() - INTERVAL '2 hours' WHERE user_id = %s", (uid,))
    assert count() == 0


def test_prefijos_de_token_solo_con_credenciales(aenv):
    e = aenv
    cl = call(e, "viewer_a", "GET", "/admin/clients", expect=200).json()["clients"]
    assert cl and all(c["token_preview"] is None for c in cl)
    cl = call(e, "creds_a", "GET", "/admin/clients", expect=200).json()["clients"]
    assert cl and all(c["token_preview"] for c in cl)
    act = call(e, "viewer_a", "GET", "/admin/activity?only_errors=false", expect=200).json()["items"]
    assert act and all(a["token"] is None for a in act)


def test_nombre_neutro_y_nombres_de_otros_grupos_ocultos(aenv):
    e = aenv
    m = call(e, "super", "POST", "/admin/monitored-databases", {"kind": "dwh", "group_id": e["A"]}, expect=201).json()
    assert "Grupo A" not in m["display_name"] and m["display_name"].startswith("DWH ")
    # Vista desde B de la base compartida (dueño A): sin nombre heredado ni instalación de A.
    q("UPDATE monitored_database SET lease_installation_id = %s, lease_until = NOW() + INTERVAL '1 hour' "
      "WHERE id = %s", (e["ia"], e["mdb_s"]))
    p = pw()
    e["b"].admin("POST", "/admin/users", {"username": "viewer_b2", "email": email_for("viewer_b2"), "password": p,
                                          "must_change_password": False,
                                          "roles": [{"role": "lectura", "group_id": e["B"]}]}, expect=201)
    h = bearer(login(e["b"].url, "viewer_b2", p)["token"])
    d = requests.get(e["b"].url + f"/admin/monitored-databases/{e['mdb_s']}", headers=h, timeout=T).json()
    assert d["display_name"].startswith("Base monitoreada #") and d["owner_hidden"] is True
    assert d["lease_installation_name"] == "(instalación de otro grupo)"
    assert "inst-a" not in str(d)


def test_pool_reutiliza_conexiones_y_limpia_estado(aenv):
    e = aenv
    lim = e["lim"]
    h = bearer(login(lim.url, "op_ab", e["users"]["op_ab"]["password"])["token"])
    for _ in range(5):
        requests.get(lim.url + "/admin/groups", headers=h, timeout=T)

    def pids():
        return {r[0] for r in q("""SELECT pid FROM pg_stat_activity WHERE application_name = 'nexus_test_lim'""")}

    before = pids()
    for _ in range(40):
        assert requests.get(lim.url + "/admin/groups", headers=h, timeout=T).status_code == 200
    after = pids()
    assert before and after <= before | set(), (before, after)   # mismas conexiones: se reutilizan
    assert len(after) <= 5
    idle = q("""SELECT query FROM pg_stat_activity WHERE application_name = 'nexus_test_lim' AND state = 'idle'""")
    assert idle and all(r[0] == "DISCARD ALL" for r in idle), idle


# ─────────────────────────────────────────────────────────────────────────────
# Migración 011 — acceso por correo (normalización, aborto en duplicados)
# ─────────────────────────────────────────────────────────────────────────────
def test_migracion_011_normaliza_correos_y_aborta_con_duplicados():
    """
    BD propia y aislada (no usa el fixture `aenv`): se aplican la línea base + migraciones
    001..010, se insertan usuarios ficticios (con correos duplicados sin distinguir mayúsculas,
    con espacios, y uno SIN correo) directamente en panel_user, y luego se aplica SOLO la
    migración 011 dos veces: la primera debe ABORTAR (duplicados) sin tocar nada; tras corregir
    el duplicado, la segunda debe aplicarse y crear el índice único.
    """
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    import migrate

    dbname = "nexus_test_cfg_migr011"
    admin = psycopg2.connect(host=support.CFG_PG["host"], port=support.CFG_PG["port"], user=support.CFG_PG["user"],
                             password=support.CFG_PG["password"], dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{dbname}"')
    admin.close()

    tmp = tempfile.mkdtemp(prefix="nx_migr011_")
    conn = None
    try:
        # Copia de las migraciones EXCEPTO la 011 (se aplica aparte, a mano, más abajo).
        for fname in os.listdir(migrate.MIGRATIONS_DIR):
            if not fname.startswith("011_"):
                shutil.copy(os.path.join(migrate.MIGRATIONS_DIR, fname), os.path.join(tmp, fname))

        conn = psycopg2.connect(host=support.CFG_PG["host"], port=support.CFG_PG["port"],
                                user=support.CFG_PG["user"], password=support.CFG_PG["password"], dbname=dbname)
        migrate.run_migrations(conn, directory=tmp, log=lambda *_: None)

        with conn.cursor() as cur:
            cur.execute("INSERT INTO panel_user (username, email, password_hash) VALUES (%s, %s, %s)",
                       ("migr_dup1", "Dup@Ejemplo.com", "x"))
            cur.execute("INSERT INTO panel_user (username, email, password_hash) VALUES (%s, %s, %s)",
                       ("migr_dup2", "  dup@ejemplo.com  ", "x"))
            cur.execute("INSERT INTO panel_user (username, email, password_hash) VALUES (%s, %s, %s)",
                       ("migr_sin_correo", None, "x"))
        conn.commit()

        # 1.ª vez: aborta (duplicados sin distinguir mayúsculas) — nada se pierde ni se modifica.
        with pytest.raises(Exception):
            migrate.run_migrations(conn, baseline=False, directory=migrate.MIGRATIONS_DIR, log=lambda *_: None)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('ux_panel_user_email')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT email FROM panel_user WHERE username = 'migr_dup1'")
            assert cur.fetchone()[0] == "Dup@Ejemplo.com"  # sin normalizar: la migración abortó y revirtió todo

        # Se corrige el duplicado (a mano, como pediría el mensaje de la migración) y se reintenta.
        with conn.cursor() as cur:
            cur.execute("UPDATE panel_user SET email = 'dup2@ejemplo.com' WHERE username = 'migr_dup2'")
        conn.commit()
        applied = migrate.run_migrations(conn, baseline=False, directory=migrate.MIGRATIONS_DIR, log=lambda *_: None)
        assert "011" in applied

        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('ux_panel_user_email')")
            assert cur.fetchone()[0] is not None
            cur.execute("SELECT email FROM panel_user WHERE username = 'migr_dup1'")
            assert cur.fetchone()[0] == "dup@ejemplo.com"        # normalizado a minúsculas/trim
            cur.execute("SELECT email FROM panel_user WHERE username = 'migr_sin_correo'")
            assert cur.fetchone()[0] is None                     # nunca se inventa un correo
            # El índice único rechaza un nuevo duplicado.
            with pytest.raises(psycopg2.errors.UniqueViolation):
                with conn.cursor() as cur2:
                    cur2.execute("INSERT INTO panel_user (username, email, password_hash) "
                                "VALUES ('migr_dup3', 'DUP@EJEMPLO.COM', 'x')")
            conn.rollback()
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
        support.drop_config_db(dbname)
