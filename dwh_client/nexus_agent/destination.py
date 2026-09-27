"""
destination.py — destino (DWH) efectivo y "Probar conexión" (sección 22).

* ``task_warehouse(task, cfg)``: destino de la tarea. Nexus >= 5.3 manda un
  ``warehouse`` por tarea (destino propio de la empresa o el del grupo); con un
  Nexus anterior se usa el ``warehouse`` general.
* ``effective_load_table``: tabla sin esquema → ``<esquema destino>.tabla``;
  un esquema explícito del catálogo (``dwh.carter``) se respeta.
* ``pg_ssl_kwargs``: ``sslmode`` / ``sslrootcert`` para psycopg2. El PEM de la
  CA se escribe en ``<data_dir>/certs/<sha256>.pem`` (no es secreto; permisos
  0600 en POSIX) porque libpq necesita una ruta. ``verify-full`` sin CA propia
  usa el almacén de CAs del sistema (``sslrootcert=system``, libpq >= 16).
* ``run_connection_test``: prueba de SOLO LECTURA pedida desde el panel. No
  crea objetos: comprueba conexión, versión, SSL en uso, existencia del esquema
  y privilegios (``has_schema_privilege`` / ``has_database_privilege``). Nunca
  registra ni devuelve credenciales (mensajes saneados con sanitize_error).
"""

import hashlib
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import psycopg2

from . import AGENT_VERSION
from .sanitize import SECRETS, StageError, sanitize_error

SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_SCHEMA = "public"


def task_warehouse(task: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    wh = task.get("warehouse")
    if isinstance(wh, dict) and wh:
        return wh
    return cfg.get("warehouse") or {}


def all_warehouses(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    if cfg.get("warehouse"):
        out.append(cfg["warehouse"])
    for t in cfg.get("tasks") or []:
        if isinstance(t.get("warehouse"), dict) and t["warehouse"]:
            out.append(t["warehouse"])
    return out


def warehouse_schema(wh: Dict[str, Any]) -> str:
    s = str(wh.get("schema") or "").strip().lower()
    return s or DEFAULT_SCHEMA


def effective_load_table(load_table: str, wh: Dict[str, Any]) -> str:
    table = str(load_table or "").strip().lower()
    if "." in table:
        return table
    schema = warehouse_schema(wh)
    return table if schema == DEFAULT_SCHEMA else f"{schema}.{table}"


# CAs en uso por pruebas en curso (no se borran aunque la config ya no las mencione).
_IN_USE: Dict[str, int] = {}
_IN_USE_LOCK = threading.Lock()


def _ca_name(pem: str) -> str:
    return hashlib.sha256(((pem or "").strip() + "\n").encode("utf-8")).hexdigest() + ".pem"


def connection_key(test: Dict[str, Any]) -> str:
    """Clave (sin secretos en claro) de la conexión que usa una prueba: para espaciar pruebas repetidas."""
    c = test.get("connection") or {}
    raw = "|".join(str(c.get(k) or "") for k in ("kind", "engine", "host", "port", "database", "username", "dsn"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _certs_dir(settings: Any) -> str:
    return os.path.join(getattr(settings, "data_dir", "") or ".", "certs")


def ca_file(settings: Any, pem: str) -> str:
    """Ruta del PEM de la CA (se escribe una sola vez por contenido)."""
    pem = (pem or "").strip() + "\n"
    digest = hashlib.sha256(pem.encode("utf-8")).hexdigest()
    d = _certs_dir(settings)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{digest}.pem")
    if not os.path.exists(path):
        # Nombre temporal único + reemplazo atómico: dos hilos (ETL, inventario, prueba) no se pisan.
        tmp = os.path.join(d, f".{digest}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
                fh.write(pem)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return path


def prune_ca_files(settings: Any, cfg: Optional[Dict[str, Any]]) -> int:
    """Borra certificados que ya no usa ninguna configuración vigente."""
    d = _certs_dir(settings)
    if not os.path.isdir(d):
        return 0
    keep = set()
    for wh in all_warehouses(cfg or {}):
        pem = (wh.get("sslrootcert") or "").strip()
        if pem:
            keep.add(_ca_name(pem))
    with _IN_USE_LOCK:
        keep |= {k for k, v in _IN_USE.items() if v > 0}
    n = 0
    for name in os.listdir(d):
        if name.endswith(".pem") and name not in keep:
            try:
                os.remove(os.path.join(d, name))
                n += 1
            except OSError:
                pass
    return n


def pg_ssl_kwargs(wh: Dict[str, Any], settings: Any) -> Dict[str, Any]:
    """Opciones SSL/TLS para psycopg2.connect (vacío = comportamiento previo de libpq: prefer)."""
    mode = str(wh.get("sslmode") or "").strip().lower()
    if not mode:
        return {}
    if mode not in SSL_MODES:
        mode = "require"  # valor desconocido: nunca se degrada a sin cifrado
    kw: Dict[str, Any] = {"sslmode": mode}
    pem = (wh.get("sslrootcert") or "").strip()
    if pem and mode in ("verify-ca", "verify-full", "require"):
        kw["sslrootcert"] = ca_file(settings, pem)
    elif mode == "verify-full" and psycopg2.__libpq_version__ >= 160000:
        # Sin CA propia: almacén de CAs del sistema (libpq lo admite solo con verify-full).
        kw["sslrootcert"] = "system"
    return kw


def register_warehouse_secrets(wh: Dict[str, Any]) -> None:
    SECRETS.add(wh.get("host"), wh.get("username"), wh.get("password"), wh.get("database"))


# ─────────────────────────────────────────────────────────────────────────────
# Prueba de conexión (solo lectura)
# ─────────────────────────────────────────────────────────────────────────────
def _check(code: str, ok: Optional[bool], message: str, severity: Optional[str] = None) -> Dict[str, Any]:
    if severity is None:
        severity = "info" if ok else ("error" if ok is False else "warning")
    return {"code": code, "ok": ok, "severity": severity, "message": message[:300]}


def _test_dwh(conn_params: Dict[str, Any], settings: Any, timeout: int) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    out: Dict[str, Any] = {"status": "failed", "checks": checks}
    schema = warehouse_schema(conn_params)
    t0 = time.monotonic()
    conn = None
    try:
        try:
            conn = psycopg2.connect(
                host=str(conn_params.get("host") or ""), port=int(conn_params.get("port") or 5432),
                dbname=str(conn_params.get("database") or ""), user=str(conn_params.get("username") or ""),
                password=str(conn_params.get("password") or ""), connect_timeout=max(2, int(timeout)),
                options=(f"-c statement_timeout={int(timeout) * 1000} -c default_transaction_read_only=on "
                         f"-c lock_timeout=3000"),
                application_name="nexus-dwh-connection-test", **pg_ssl_kwargs(conn_params, settings),
            )
        except Exception as exc:  # noqa: BLE001
            code, msg = sanitize_error(StageError("load", exc, side="DWH"))
            checks.append(_check("CONNECT", False, f"No se pudo conectar: {msg}"))
            out.update(error_code=code, error_message=msg)
            return out
        conn.set_session(readonly=True, autocommit=False)
        checks.append(_check("CONNECT", True, "Conexión y autenticación correctas."))
        cur = conn.cursor()
        cur.execute("SELECT current_setting('server_version'), current_database(), current_user")
        version, _db, _user = cur.fetchone()
        out["server_version"] = f"PostgreSQL {version}"[:120]
        # SSL en uso (pg_stat_ssl existe desde 9.5).
        ssl_in_use, ssl_version = None, None
        try:
            cur.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            r = cur.fetchone()
            if r:
                ssl_in_use, ssl_version = bool(r[0]), (r[1] or None)
        except psycopg2.Error:
            conn.rollback()
        out["ssl_in_use"], out["ssl_version"] = ssl_in_use, ssl_version
        mode = str(conn_params.get("sslmode") or "prefer")
        if ssl_in_use:
            checks.append(_check("SSL", True, f"Conexión cifrada ({ssl_version or 'TLS'}, sslmode={mode})."))
        elif mode == "disable":
            checks.append(_check("SSL", None, "Conexión SIN cifrar (sslmode=disable).", "warning"))
        else:
            checks.append(_check("SSL", None, f"Conexión SIN cifrar: el servidor no ofrece SSL (sslmode={mode}). "
                                              "Use require o verify-full si el tráfico sale de la red local.",
                                 "warning"))
        # Esquema y privilegios (sin crear nada).
        cur.execute("SELECT oid FROM pg_namespace WHERE nspname = %s", (schema,))
        exists = cur.fetchone() is not None
        out["schema_exists"] = exists
        ok = True
        if exists:
            cur.execute("SELECT has_schema_privilege(%s, 'USAGE'), has_schema_privilege(%s, 'CREATE')",
                        (schema, schema))
            usage, create = cur.fetchone()
            checks.append(_check("SCHEMA_EXISTS", True, f"El esquema «{schema}» existe."))
            checks.append(_check("SCHEMA_USAGE", bool(usage), "Permiso USAGE sobre el esquema." if usage
                                 else "Sin permiso USAGE sobre el esquema: no se podrá escribir en sus tablas."))
            checks.append(_check("CREATE_TABLE", bool(create), "Puede crear tablas en el esquema." if create
                                 else "Sin permiso CREATE en el esquema: no podrá crear tablas nuevas "
                                      "(solo cargar en tablas existentes con permisos).",
                                 None if create else "warning"))
            ok = bool(usage)
        else:
            cur.execute("SELECT has_database_privilege(current_database(), 'CREATE')")
            can_schema = bool(cur.fetchone()[0])
            checks.append(_check("SCHEMA_EXISTS", None, f"El esquema «{schema}» no existe; el agente lo crea en la "
                                                        "primera carga.", "warning"))
            checks.append(_check("CREATE_SCHEMA", can_schema, "Puede crear el esquema." if can_schema
                                 else "Sin permiso CREATE en la base: no podrá crear el esquema."))
            ok = can_schema
        conn.rollback()
        out["status"] = "ok" if ok else "failed"
        if not ok:
            out["error_code"] = "DWH_INSUFFICIENT_PRIVILEGE"
            out["error_message"] = "Conecta, pero el usuario no tiene los permisos necesarios en el esquema destino."
        return out
    except Exception as exc:  # noqa: BLE001
        code, msg = sanitize_error(StageError("load", exc, side="DWH"))
        checks.append(_check("QUERY", False, f"Error al verificar: {msg}"))
        out.update(status="failed", error_code=code, error_message=msg)
        return out
    finally:
        out["duration_ms"] = int((time.monotonic() - t0) * 1000)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


_PING_SQL = {"firebird": "SELECT 1 FROM RDB$DATABASE"}
_ENGINE_LABELS = {"sqlserver": "SQL Server", "mysql": "MySQL", "postgresql": "PostgreSQL", "firebird": "Firebird",
                  "pervasive": "Pervasive"}


def _test_source(conn_params: Dict[str, Any], settings: Any, timeout: int) -> Dict[str, Any]:
    from .etl import RunContext, connect_source  # import local: evita ciclos

    checks: List[Dict[str, Any]] = []
    out: Dict[str, Any] = {"status": "failed", "checks": checks}
    t0 = time.monotonic()
    src = {"type": conn_params.get("engine"), "host": conn_params.get("host"), "port": conn_params.get("port"),
           "database": conn_params.get("database"), "username": conn_params.get("username"),
           "password": conn_params.get("password"), "dsn": conn_params.get("dsn")}

    class _S:  # timeouts propios de la prueba (más cortos que los del ETL)
        db_connect_timeout_seconds = max(2, int(timeout))
        source_statement_timeout_seconds = max(2, int(timeout))

    conn = None
    try:
        try:
            conn, tipo = connect_source(src, _S, RunContext())
        except Exception as exc:  # noqa: BLE001
            code, msg = sanitize_error(StageError("extract", exc, side="SOURCE"))
            checks.append(_check("CONNECT", False, f"No se pudo conectar: {msg}"))
            out.update(error_code=code, error_message=msg)
            return out
        checks.append(_check("CONNECT", True, "Conexión y autenticación correctas."))
        cur = conn.cursor()
        cur.execute(_PING_SQL.get(tipo, "SELECT 1"))
        cur.fetchall()
        checks.append(_check("QUERY", True, "Consulta de prueba (SELECT 1) correcta."))
        out["server_version"] = _ENGINE_LABELS.get(tipo, str(tipo))
        out["status"] = "ok"
        return out
    except Exception as exc:  # noqa: BLE001
        code, msg = sanitize_error(StageError("extract", exc, side="SOURCE"))
        checks.append(_check("QUERY", False, f"Error al consultar: {msg}"))
        out.update(status="failed", error_code=code, error_message=msg)
        return out
    finally:
        out["duration_ms"] = int((time.monotonic() - t0) * 1000)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def run_connection_test(test: Dict[str, Any], settings: Any) -> Dict[str, Any]:
    """Ejecuta la prueba y devuelve el cuerpo de POST /agent/connection-tests/{id}/result (nunca lanza)."""
    params = test.get("connection") or {}
    SECRETS.add(params.get("host"), params.get("username"), params.get("password"), params.get("database"),
                params.get("dsn"))
    timeout = int(test.get("timeout_seconds") or 15)
    pem = (params.get("sslrootcert") or "").strip()
    key = _ca_name(pem) if pem else None
    if key:
        with _IN_USE_LOCK:
            _IN_USE[key] = _IN_USE.get(key, 0) + 1
    try:
        if test.get("kind") == "dwh":
            res = _test_dwh(params, settings, timeout)
        else:
            res = _test_source(params, settings, timeout)
    except Exception as exc:  # noqa: BLE001 — defensa
        code, msg = sanitize_error(exc)
        res = {"status": "failed", "checks": [_check("UNEXPECTED", False, msg)], "error_code": code,
               "error_message": msg}
    finally:
        if key:
            with _IN_USE_LOCK:
                _IN_USE[key] = max(0, _IN_USE.get(key, 1) - 1)
    res["agent_version"] = AGENT_VERSION
    return res
