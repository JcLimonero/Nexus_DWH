"""
Soporte común de pruebas (backend y agente).

Entorno Docker LOCAL (nunca servidores remotos):
  * nexus_dwh_pg_dev   (5546)  BD de configuración; las pruebas crean su propia
                              base temporal (nexus_test_cfg_*) y la borran.
  * nexus_dwh_test_src (5547)  PostgreSQL que hace de ORIGEN (DMS).
  * nexus_dwh_test_dwh (5548)  PostgreSQL que hace de DWH destino.

El backend se arranca como subproceso (dwh_back/.venv) con un config.ini
temporal (NEXUS_CONFIG_FILE). Todos los datos son ficticios.
"""

import base64
import configparser
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

import psycopg2
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACK_DIR = os.path.join(ROOT, "dwh_back")
CLIENT_DIR = os.path.join(ROOT, "dwh_client")
BACK_PY = os.path.join(BACK_DIR, ".venv", "bin", "python")

CFG_PG = {"host": "127.0.0.1", "port": 5546, "user": "postgres", "password": os.environ.get("NEXUS_TEST_CFG_PASSWORD", "devpass")}
SRC = {"container": "nexus_dwh_test_src", "host": "127.0.0.1", "port": 5547, "database": "dms_test",
       "username": "postgres", "password": "src-test-pass-7Qx"}
DWH = {"container": "nexus_dwh_test_dwh", "host": "127.0.0.1", "port": 5548, "database": "dwh_test",
       "username": "postgres", "password": "dwh-test-pass-9Kz"}

if BACK_DIR not in sys.path:
    sys.path.insert(0, BACK_DIR)


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


def docker_available() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


def wait_pg(params: Dict[str, Any], timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            c = psycopg2.connect(host=params["host"], port=params["port"], user=params["username"],
                                 password=params["password"], dbname=params["database"], connect_timeout=2)
            c.close()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"PostgreSQL no disponible en {params['port']}: {type(last).__name__}")


def ensure_container(p: Dict[str, Any]) -> None:
    state = docker("inspect", "-f", "{{.State.Running}}", p["container"], check=False)
    if state != "true":
        docker("start", p["container"], check=False)
    wait_pg(p)


def pg(p: Dict[str, Any], dbname: Optional[str] = None):
    conn = psycopg2.connect(host=p["host"], port=p["port"], user=p.get("username", p.get("user")),
                            password=p["password"], dbname=dbname or p.get("database", "postgres"))
    conn.autocommit = True
    return conn


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def recreate_config_db(dbname: str) -> None:
    admin = psycopg2.connect(host=CFG_PG["host"], port=CFG_PG["port"], user=CFG_PG["user"],
                             password=CFG_PG["password"], dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{dbname}"')
    admin.close()
    import migrate  # dwh_back/migrate.py

    conn = psycopg2.connect(host=CFG_PG["host"], port=CFG_PG["port"], user=CFG_PG["user"],
                            password=CFG_PG["password"], dbname=dbname)
    try:
        migrate.run_migrations(conn, log=lambda *_: None)
    finally:
        conn.close()


def drop_config_db(dbname: str) -> None:
    admin = psycopg2.connect(host=CFG_PG["host"], port=CFG_PG["port"], user=CFG_PG["user"],
                             password=CFG_PG["password"], dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    admin.close()


def cfg_conn(dbname: str):
    conn = psycopg2.connect(host=CFG_PG["host"], port=CFG_PG["port"], user=CFG_PG["user"],
                            password=CFG_PG["password"], dbname=dbname)
    conn.autocommit = True
    return conn


class Backend:
    """Backend FastAPI en subproceso con config.ini temporal."""

    def __init__(self, dbname: str, port: Optional[int] = None, extra_ini: Optional[Dict[str, Dict[str, str]]] = None):
        self.dbname = dbname
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.admin_token = "test-admin-" + secrets.token_urlsafe(24)
        self.monitor_token = "test-monitor-" + secrets.token_urlsafe(24)
        self.fernet_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        self.tmpdir = tempfile.mkdtemp(prefix="nexus_back_")
        self.ini_path = os.path.join(self.tmpdir, "config.ini")
        self.log_path = os.path.join(self.tmpdir, "backend.log")
        ini = configparser.ConfigParser()
        ini["database"] = {"host": CFG_PG["host"], "port": str(CFG_PG["port"]), "db": dbname,
                           "user": CFG_PG["user"], "password": CFG_PG["password"]}
        ini["monitor"] = {"token": self.monitor_token}
        ini["admin"] = {"token": self.admin_token}
        ini["security"] = {"config_secret_key": self.fernet_key}
        ini["agent"] = {"config_max_age_seconds": "900", "rotation_grace_seconds": "3600"}
        for sec, vals in (extra_ini or {}).items():
            ini[sec] = {**(ini[sec] if ini.has_section(sec) else {}), **vals}
        with open(self.ini_path, "w") as fh:
            ini.write(fh)
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        env = dict(os.environ, NEXUS_CONFIG_FILE=self.ini_path)
        self._log = open(self.log_path, "a")
        self.proc = subprocess.Popen(
            [BACK_PY, "main_postgres.py", "--port", str(self.port)], cwd=BACK_DIR, env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if requests.get(self.url + "/health", timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError("El backend terminó al arrancar; ver " + self.log_path)
            time.sleep(0.3)
        raise RuntimeError("El backend no respondió /health")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        try:
            self._log.close()
        except Exception:  # noqa: BLE001
            pass

    def restart(self) -> None:
        self.stop()
        self.start()

    # ── helpers HTTP ──
    def admin(self, method: str, path: str, body: Optional[dict] = None, expect: Optional[int] = None) -> Any:
        r = requests.request(method, self.url + path, json=body, headers={"x-admin-token": self.admin_token}, timeout=15)
        if expect is not None:
            assert r.status_code == expect, f"{method} {path} -> {r.status_code}: {r.text[:300]}"
        return r.json() if r.content else None

    def monitor(self, path: str) -> Any:
        r = requests.get(self.url + path, headers={"x-monitor-token": self.monitor_token}, timeout=15)
        assert r.status_code == 200, r.text[:300]
        return r.json()


SQL_MARKER = "nx_sql_marker_7f3a"          # aparece SOLO dentro de extract_sql
ROW_SECRET = "valor.fila.secreto@marker"   # valor de fila que no debe salir en logs/reportes


def seed_source() -> None:
    """Tablas de origen: clientes (con updated_at), tabla lenta y tabla con email duplicado."""
    c = pg(SRC)
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS src_clientes, src_dups")
        cur.execute("""CREATE TABLE src_clientes (
                           id INT PRIMARY KEY, nombre TEXT, updated_at TIMESTAMP NOT NULL)""")
        cur.execute("""INSERT INTO src_clientes
                       SELECT g, 'cliente ' || g, LOCALTIMESTAMP - interval '1 day'
                       FROM generate_series(1, 500) g""")
        cur.execute("CREATE TABLE src_dups (id INT, email TEXT)")
        cur.execute("INSERT INTO src_dups VALUES (1, %s), (2, %s)", (ROW_SECRET, ROW_SECRET))
    c.close()


def reset_dwh() -> None:
    c = pg(DWH)
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS dwh CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.clientes, public.lenta, public.dups, public.sin_llave CASCADE")
    c.close()


def seed_config(b: Backend) -> Dict[str, Any]:
    """Alta vía API admin (los secretos quedan cifrados ENC:)."""
    ga = b.admin("POST", "/admin/groups", {
        "name": "Grupo A", "warehouse_host": DWH["host"], "warehouse_port": DWH["port"],
        "warehouse_database": DWH["database"], "warehouse_username": DWH["username"],
        "warehouse_password": DWH["password"]}, expect=201)
    gb = b.admin("POST", "/admin/groups", {
        "name": "Grupo B", "warehouse_host": "dwh-b.invalid", "warehouse_port": 5432,
        "warehouse_database": "dwh_b", "warehouse_username": "user_b", "warehouse_password": "pass-b-secreta"}, expect=201)
    ca = b.admin("POST", "/admin/companies", {
        "group_id": ga["id"], "name": "Empresa A1", "source_type": "postgresql", "source_host": SRC["host"],
        "source_port": SRC["port"], "source_database": SRC["database"], "source_username": SRC["username"],
        "source_password": SRC["password"], "refresh_seconds": 30}, expect=201)
    cb = b.admin("POST", "/admin/companies", {
        "group_id": gb["id"], "name": "Empresa B1", "source_type": "postgresql", "source_host": "src-b.invalid",
        "source_port": 5432, "source_database": "dms_b", "source_username": "src_user_b",
        "source_password": "src-pass-b-secreta"}, expect=201)
    aa1 = b.admin("POST", "/admin/agencies", {"company_id": ca["id"], "name": "Agencia A1-Centro"}, expect=201)
    aa2 = b.admin("POST", "/admin/agencies", {"company_id": ca["id"], "name": "Agencia A1-Norte"}, expect=201)
    ab1 = b.admin("POST", "/admin/agencies", {"company_id": cb["id"], "name": "Agencia B1"}, expect=201)

    def obj(company, name, table, keys, **kw):
        return b.admin("POST", "/admin/objects", {"company_id": company["id"], "name": name,
                                                   "destination_table": table, "upsert_keys": keys, **kw}, expect=201)

    o_cli = obj(ca, "Clientes", "dwh.clientes", "id", static_columns='{"dn": 1000}')
    o_lenta = obj(ca, "Lenta", "lenta", "id")
    o_dups = obj(ca, "Dups", "dups", "id",
                 create_table_sql="CREATE TABLE IF NOT EXISTS dups (id INT PRIMARY KEY, email TEXT UNIQUE)")
    o_bad = obj(ca, "Mala", "mala", "id")
    o_sinllave = obj(ca, "SinLlave", "sin_llave", None)
    o_b = obj(cb, "ClientesB", "clientes_b", "id")

    def task(agency, o, sql, active=True, schedule=3600):
        return b.admin("POST", "/admin/tasks", {"agency_id": agency["id"], "object_catalog_id": o["id"],
                                                 "extract_sql": sql, "schedule_seconds": schedule,
                                                 "is_active": active}, expect=201)

    t_cli = task(aa1, o_cli, f"SELECT id, nombre, updated_at /* {SQL_MARKER} */ FROM src_clientes "
                              "WHERE updated_at >= '{last_run}' ORDER BY id")
    t_lenta = task(aa1, o_lenta, f"SELECT g AS id, pg_sleep(0.02) IS NULL AS x /* {SQL_MARKER} */ "
                                 "FROM generate_series(1, 400) g", active=False)
    t_dups = task(aa1, o_dups, f"SELECT id, email /* {SQL_MARKER} */ FROM src_dups ORDER BY id", active=False)
    t_bad = task(aa2, o_bad, f"SELECT id FROM tabla_inexistente_xyz /* {SQL_MARKER} */", active=False)
    t_sin = task(aa2, o_sinllave, f"SELECT id, nombre /* {SQL_MARKER} */ FROM src_clientes WHERE id <= 5", active=False)
    t_b = task(ab1, o_b, f"SELECT 1 AS id /* {SQL_MARKER} */")
    return {
        "group_a": ga, "group_b": gb, "company_a": ca, "company_b": cb,
        "agency_a1": aa1, "agency_a2": aa2, "agency_b1": ab1,
        "t_cli": t_cli, "t_lenta": t_lenta, "t_dups": t_dups, "t_bad": t_bad, "t_sin": t_sin, "t_b": t_b,
    }


class FakeWebhookReceiver:
    """Receptor HTTP LOCAL (127.0.0.1) que registra los webhooks. Nunca destinos reales."""

    def __init__(self):
        self.requests = []
        self.codes = []          # códigos a devolver en orden; después 200
        self.lock = threading.Lock()
        rec = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with rec.lock:
                    rec.requests.append({"headers": {k.lower(): v for k, v in self.headers.items()},
                                         "body": body, "path": self.path})
                    code = rec.codes.pop(0) if rec.codes else 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bodies(self, event=None):
        with self.lock:
            out = [json.loads(r["body"]) for r in self.requests]
        return [b for b in out if event is None or b.get("event") == event]

    def stop(self):
        self.server.shutdown()
