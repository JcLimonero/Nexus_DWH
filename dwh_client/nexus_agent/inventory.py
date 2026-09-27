"""
inventory.py — inventario estructural de SOLO LECTURA (sección 19 de DWH_README.md).

El agente inventaría las bases que Nexus le asigna (``POST /agent/inventory/lease``:
una sola instalación por base) y reporta el resultado
(``POST /agent/inventory/snapshots``). Nexus nunca se conecta a la base.

* Sesión de solo lectura (``default_transaction_read_only`` + ``SET SESSION
  CHARACTERISTICS … READ ONLY``), ``statement_timeout`` y ``lock_timeout``
  cortos: solo consultas a ``pg_catalog``; nunca DDL/DML, nunca filas de datos.
* Estructura NORMALIZADA por objeto (base, esquema, nombre, tipo): columnas,
  restricciones e índices indexados por nombre (el orden no importa),
  espacios normalizados fuera de literales, huella sha256 del JSON canónico.
* El SQL de las vistas solo se calcula como hash; el texto viaja únicamente
  si la base tiene ``view_definitions_enabled`` (y nunca se registra en logs).
* Fiabilidad: esquemas sin USAGE → "no verificables" (Nexus no infiere
  eliminaciones ahí); conexión/consulta fallida → snapshot ``unreliable``
  (Nexus conserva la línea base: "No se pudo verificar la estructura").
* Limitación: el muestreo es periódico; un objeto creado y eliminado entre dos
  inventarios no se detecta.
"""

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg2

from . import AGENT_VERSION
from .logsetup import get_logger
from .sanitize import StageError, sanitize_error

log = get_logger()

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
DEFAULT_PORTS = {"postgresql": 5432, "sqlserver": 1433, "mysql": 3306, "pervasive": 1583, "firebird": 3050}
SUPPORTED_ENGINES = ("postgresql",)
CONTYPES = {"p": "primary_key", "u": "unique", "f": "foreign_key", "c": "check", "x": "exclusion"}


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras (mismo algoritmo que dwh_back/inventory_postgres.py)
# ─────────────────────────────────────────────────────────────────────────────
def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def structure_fingerprint(structure: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(structure).encode("utf-8")).hexdigest()


def connection_identity(kind: str, engine: str, host: Any, port: Any, database: Any, dsn: Any = "") -> str:
    engine = (engine or "postgresql").strip().lower()
    dsn = (dsn or "").strip()
    if dsn and engine != "postgresql":
        loc = "dsn=" + dsn.lower()
    else:
        try:
            p = int(port) if port not in (None, "", 0, "0") else DEFAULT_PORTS.get(engine, 0)
        except (TypeError, ValueError):
            p = DEFAULT_PORTS.get(engine, 0)
        loc = f"{str(host or '').strip().lower()}:{p}/{str(database or '').strip()}"
    return hashlib.sha256(f"{kind}|{engine}|{loc}".encode("utf-8")).hexdigest()


def is_system_schema(name: str) -> bool:
    return name in SYSTEM_SCHEMAS or name.startswith("pg_toast") or name.startswith("pg_temp_")


def schema_in_scope(name: str, include: List[str], exclude: List[str]) -> bool:
    if is_system_schema(name):
        return False
    if include and not any(fnmatchcase(name, p) for p in include):
        return False
    if any(fnmatchcase(name, p) for p in exclude):
        return False
    return True


def normalize_sql_text(text: Optional[str]) -> Optional[str]:
    """
    Normaliza formato SIN cambiar el significado: colapsa espacios/saltos de
    línea fuera de literales ('...') e identificadores entre comillas ("...") y
    quita el ';' final. pg_get_* ya devuelve texto canónico; esto evita
    diferencias irrelevantes de formato entre versiones/clientes.
    """
    if text is None:
        return None
    out: List[str] = []
    quote: Optional[str] = None
    pending_space = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:   # comilla escapada ('' o "")
                    out.append(text[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            if pending_space and out:
                out.append(" ")
            pending_space = False
            quote = ch
            out.append(ch)
        elif ch.isspace():
            pending_space = True
        else:
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(ch)
        i += 1
    s = "".join(out).strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return s


def _index_body(indexdef: str) -> str:
    """'CREATE UNIQUE INDEX x ON s.t USING btree (a)' → 'USING btree (a)' (nombre y tabla ya son la clave)."""
    pos = indexdef.find(" USING ")
    return normalize_sql_text(indexdef[pos + 1:] if pos >= 0 else indexdef) or ""


# ─────────────────────────────────────────────────────────────────────────────
# Conexión de solo lectura
# ─────────────────────────────────────────────────────────────────────────────
def connect_readonly_pg(params: Dict[str, Any], settings: Any) -> Any:
    st = max(1, int(getattr(settings, "inventory_statement_timeout_seconds", 60))) * 1000
    opts = (f"-c statement_timeout={st} -c lock_timeout=5000 -c default_transaction_read_only=on "
            f"-c idle_in_transaction_session_timeout={st * 2}")
    conn = psycopg2.connect(
        host=str(params.get("host") or ""), port=int(params.get("port") or 5432),
        dbname=str(params.get("database") or ""), user=str(params.get("username") or ""),
        password=str(params.get("password") or ""),
        connect_timeout=int(getattr(settings, "db_connect_timeout_seconds", 15)),
        options=opts, application_name="nexus-dwh-inventory",
    )
    conn.set_client_encoding("UTF8")
    # Transacción de solo lectura y vista consistente del catálogo.
    conn.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
    return conn


def engine_identity_pg(cur: Any) -> Tuple[str, str, str]:
    """
    Identidad del servidor+base (no de la configuración). Devuelve
    (principal, fuerza, débil):
      * fuerte: system_identifier del clúster (si el rol puede leerlo) + oid y nombre de la base;
      * débil: dirección/puerto del servidor + oid y nombre de la base (siempre).
    Nexus solo declara "otro servidor" si difiere un componente comparable, así que
    pasar de débil a fuerte (o cambiar la IP interna con el mismo clúster) no es un cambio.
    """
    cur.execute("SELECT d.oid::text, d.datname FROM pg_database d WHERE d.datname = current_database()")
    oid, datname = cur.fetchone()
    cur.execute("SELECT COALESCE(host(inet_server_addr()), 'local') || ':' || current_setting('port')")
    weak = hashlib.sha256(f"addr:{cur.fetchone()[0]}|{oid}|{datname}".encode("utf-8")).hexdigest()
    cur.execute("SAVEPOINT nx_ident")
    try:
        cur.execute("SELECT system_identifier::text FROM pg_control_system()")
        sysid = cur.fetchone()[0]
        cur.execute("RELEASE SAVEPOINT nx_ident")
    except psycopg2.Error:
        cur.execute("ROLLBACK TO SAVEPOINT nx_ident")
        return weak, "weak", weak
    return hashlib.sha256(f"{sysid}|{oid}|{datname}".encode("utf-8")).hexdigest(), "strong", weak


def collect_postgres(conn: Any, include: List[str], exclude: List[str], *, view_definitions: bool = False,
                     max_objects: int = 20000, expected_schemas: Optional[List[str]] = None,
                     collapse_partitions: bool = True) -> Dict[str, Any]:
    """
    Inventario de tablas, vistas, vistas materializadas y tablas foráneas de los
    esquemas del alcance. Solo lee pg_catalog. Devuelve el cuerpo del snapshot
    (sin identificadores de la base monitoreada).
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT current_setting('server_version_num')::int, current_setting('server_version')")
        vnum, vstr = cur.fetchone()
        ident, strength, weak = engine_identity_pg(cur)
        cur.execute("SELECT n.nspname, has_schema_privilege(n.oid, 'USAGE') FROM pg_namespace n ORDER BY 1")
        verified: List[str] = []
        unverifiable: List[Dict[str, str]] = []
        rows_ns = cur.fetchall()
        existing = {r[0] for r in rows_ns}
        for name, usage in rows_ns:
            if not schema_in_scope(name, include, exclude):
                continue
            if usage:
                verified.append(name)
            else:
                # Sin USAGE: no se puede afirmar qué contiene → nunca se infiere eliminación.
                unverifiable.append({"schema_name": name, "reason": "no_usage"})
        # Esquemas que Nexus esperaba (tenían objetos) y ya no existen: verificados como vacíos.
        # pg_namespace es visible para cualquier rol, así que su ausencia es confiable.
        for name in expected_schemas or []:
            if name not in existing and schema_in_scope(name, include, exclude):
                verified.append(name)
        objects: List[Dict[str, Any]] = []
        base = {"engine_identity": ident, "engine_identity_strength": strength, "engine_identity_weak": weak,
                "server_version": str(vstr)[:50], "schemas_verified": verified,
                "schemas_unverifiable": unverifiable}
        live = [s_ for s_ in verified if s_ in existing]
        if not live:
            return dict(base, status="partial" if unverifiable else "complete", objects=objects,
                        reason_code="SCHEMAS_UNVERIFIABLE" if unverifiable else None)

        cur.execute(
            """
            SELECT c.oid, n.nspname, c.relname, c.relkind, c.relpersistence, c.relispartition,
                   (SELECT pn.nspname || '.' || p.relname FROM pg_inherits i
                      JOIN pg_class p ON p.oid = i.inhparent JOIN pg_namespace pn ON pn.oid = p.relnamespace
                     WHERE i.inhrelid = c.oid LIMIT 1) AS parent,
                   (SELECT i.inhparent FROM pg_inherits i WHERE i.inhrelid = c.oid LIMIT 1) AS parent_oid,
                   CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound, c.oid) END AS bound,
                   CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS partkey
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND n.nspname = ANY(%s)
            ORDER BY n.nspname, c.relname
            """,
            (live,),
        )
        rels = cur.fetchall()
        # Tope de memoria (las particiones agrupadas no cuentan como objetos, pero sí se leen).
        if len(rels) > max_objects * (50 if collapse_partitions else 1):
            return dict(base, status="unreliable", reason_code="TOO_MANY_OBJECTS", objects=[])
        oids = [r[0] for r in rels]
        by_oid: Dict[int, Dict[str, Any]] = {}
        parent_of = {r[0]: r[7] for r in rels if r[5] and r[7] is not None}
        for oid, nsp, rel, kind, persistence, is_part, parent, parent_oid, bound, partkey in rels:
            otype = {"r": "table", "p": "table", "v": "view", "m": "matview", "f": "foreign_table"}[kind]
            st: Dict[str, Any] = {"columns": {}}
            if otype in ("table", "matview", "foreign_table"):
                st["constraints"] = {}
                st["indexes"] = {}
            if kind == "p":
                st["partitioned"] = True
                st["partition_key"] = normalize_sql_text(partkey)
            if is_part and parent:
                st["partition_of"] = parent
                st["partition_bound"] = normalize_sql_text(bound)
            if persistence == "u":
                st["persistence"] = "unlogged"
            by_oid[oid] = {"schema_name": nsp, "name": rel, "type": otype, "structure": st}

        gen_col = "a.attgenerated" if vnum >= 120000 else "''::text"
        cur.execute(
            f"""
            SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid), a.attidentity, {gen_col},
                   CASE WHEN a.attcollation <> 0 AND a.attcollation <> t.typcollation THEN co.collname END
            FROM pg_attribute a
            JOIN pg_type t ON t.oid = a.atttypid
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            LEFT JOIN pg_collation co ON co.oid = a.attcollation
            WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
            """,
            (oids,),
        )
        for relid, name, typ, notnull, default, identity, generated, coll in cur.fetchall():
            col: Dict[str, Any] = {"type": typ, "not_null": bool(notnull), "default": None}
            expr = normalize_sql_text(default) if default is not None else None
            if generated == "s":
                col["generated"] = expr
            else:
                col["default"] = expr
            if identity:
                col["identity"] = "always" if identity == "a" else "by_default"
            if coll:
                col["collation"] = coll
            by_oid[relid]["structure"]["columns"][name] = col

        cur.execute(
            """SELECT conrelid, conname, contype, pg_get_constraintdef(oid, true)
               FROM pg_constraint WHERE conrelid = ANY(%s) AND contype IN ('p', 'u', 'f', 'c', 'x')""",
            (oids,),
        )
        for relid, name, contype, definition in cur.fetchall():
            st = by_oid[relid]["structure"]
            st.setdefault("constraints", {})[name] = {"type": CONTYPES.get(contype, contype),
                                                      "definition": normalize_sql_text(definition)}

        # Índices que NO respaldan una restricción (esos ya están en "constraints").
        cur.execute(
            """SELECT i.indrelid, ic.relname, pg_get_indexdef(i.indexrelid), i.indisunique
               FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
               WHERE i.indrelid = ANY(%s)
                 AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid
                                 AND k.conrelid = i.indrelid)""",
            (oids,),
        )
        for relid, name, indexdef, unique in cur.fetchall():
            st = by_oid[relid]["structure"]
            st.setdefault("indexes", {})[name] = {"unique": bool(unique), "definition": _index_body(indexdef)}

        view_oids = [o for o, obj in by_oid.items() if obj["type"] in ("view", "matview")]
        if view_oids:
            cur.execute("SELECT c.oid, pg_get_viewdef(c.oid, true) FROM pg_class c WHERE c.oid = ANY(%s)",
                        (view_oids,))
            for oid, definition in cur.fetchall():
                norm = normalize_sql_text(definition) or ""
                by_oid[oid]["structure"]["definition_hash"] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
                if view_definitions:
                    by_oid[oid]["definition"] = norm

        emitted = list(oids)
        if collapse_partitions:
            # Particiones agrupadas bajo su tabla raíz: una partición nueva o el índice que se
            # propaga a cada partición cambian UN objeto (la raíz), no uno por partición.
            emitted = []
            for o in oids:
                top, seen = o, set()
                while top in parent_of and parent_of[top] in by_oid and top not in seen:
                    seen.add(top)
                    top = parent_of[top]
                if top == o:
                    emitted.append(o)
                else:
                    obj = by_oid[o]
                    by_oid[top]["structure"].setdefault("partitions", {})[f"{obj['schema_name']}.{obj['name']}"] = \
                        obj["structure"].get("partition_bound")
        if len(emitted) > max_objects:
            return dict(base, status="unreliable", reason_code="TOO_MANY_OBJECTS", objects=[])
        objects = [by_oid[o] for o in emitted]
        status = "partial" if unverifiable else "complete"
        return dict(base, status=status, objects=objects,
                    reason_code="SCHEMAS_UNVERIFIABLE" if unverifiable else None)
    finally:
        try:
            conn.rollback()   # nada que confirmar: sesión de solo lectura
        except Exception:
            pass
        cur.close()


# ─────────────────────────────────────────────────────────────────────────────
# Ejecución de un objetivo asignado por Nexus
# ─────────────────────────────────────────────────────────────────────────────
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def target_connection(target: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Credenciales (ya en memoria, de GET /agent/tasks) para el objetivo; nunca se piden aparte."""
    if target.get("kind") == "dwh":
        wh = cfg.get("warehouse") or {}
        if not wh.get("host"):
            return None
        return {"engine": "postgresql", "host": wh.get("host"), "port": wh.get("port") or 5432,
                "database": wh.get("database"), "username": wh.get("username"), "password": wh.get("password"),
                "dsn": ""}
    for t in cfg.get("tasks") or []:
        if int(t.get("company_id") or 0) == int(target.get("company_id") or -1):
            src = t.get("source") or {}
            return {"engine": (src.get("type") or "sqlserver").lower(), "host": src.get("host"),
                    "port": src.get("port"), "database": src.get("database"), "username": src.get("username"),
                    "password": src.get("password"), "dsn": src.get("dsn") or ""}
    return None


def capabilities(cfg: Dict[str, Any]) -> Dict[str, Any]:
    wh = cfg.get("warehouse") or {}
    companies = sorted({int(t["company_id"]) for t in cfg.get("tasks") or [] if t.get("company_id")})
    return {"dwh": bool(wh.get("host")), "source_company_ids": companies}


def build_snapshot(target: Dict[str, Any], conn_params: Dict[str, Any], settings: Any,
                   connect: Callable[[Dict[str, Any], Any], Any] = connect_readonly_pg) -> Dict[str, Any]:
    """Inventaría el objetivo y arma el cuerpo de POST /agent/inventory/snapshots (nunca lanza)."""
    kind = target["kind"]
    side = "DWH" if kind == "dwh" else "SOURCE"
    body: Dict[str, Any] = {
        "snapshot_id": str(uuid.uuid4()), "monitored_database_id": int(target["monitored_database_id"]),
        "config_fingerprint": connection_identity(kind, conn_params["engine"], conn_params.get("host"),
                                                  conn_params.get("port"), conn_params.get("database"),
                                                  conn_params.get("dsn")),
        "captured_at": utc_iso(), "agent_version": AGENT_VERSION,
        "status": "unreliable", "reason_code": None, "objects": [],
        "schemas_verified": [], "schemas_unverifiable": [],
    }
    if conn_params["engine"] not in SUPPORTED_ENGINES:
        body["reason_code"] = "ENGINE_UNSUPPORTED"
        return body
    conn = None
    try:
        conn = connect(conn_params, settings)
        data = collect_postgres(conn, list(target.get("schema_include") or []),
                                list(target.get("schema_exclude") or []),
                                view_definitions=bool(target.get("view_definitions_enabled")),
                                max_objects=int(target.get("max_objects") or 20000),
                                expected_schemas=list(target.get("expected_schemas") or []),
                                collapse_partitions=bool(target.get("collapse_partitions", True)))
        body.update(data)
    except Exception as exc:  # noqa: BLE001 — cualquier falla = no se pudo verificar
        code, _msg = sanitize_error(StageError("inventory", exc, side=side))
        body.update(status="unreliable", reason_code=code[:64], objects=[])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    body["captured_at"] = utc_iso()
    return body


class InventoryRunner:
    """
    Hilo propio del agente (independiente del ETL): cada ``inventory_tick_seconds``
    renueva el lease y, para cada base asignada y vencida, inventaría y reporta.
    Si el envío falla, espera con backoff exponencial (por base, con tope) antes de
    volver a inventariar esa base, en lugar de repetir el inventario en cada ciclo.
    """

    BACKOFF_MAX_SECONDS = 3600.0

    def __init__(self, settings: Any, api: Any, get_config: Callable[[], Optional[Dict[str, Any]]],
                 stop_event: Optional[threading.Event] = None, on_api_ok: Optional[Callable[[], None]] = None,
                 connect: Callable[[Dict[str, Any], Any], Any] = connect_readonly_pg,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.api = api
        self.get_config = get_config
        self.stop_event = stop_event or threading.Event()
        self.on_api_ok = on_api_ok
        self.connect = connect
        self.clock = clock
        self.last_results: List[Dict[str, Any]] = []
        self._backoff: Dict[int, Tuple[int, float]] = {}   # base → (fallos seguidos, no antes de)

    def backoff_delay(self, failures: int) -> float:
        base = max(5.0, float(getattr(self.settings, "inventory_tick_seconds", 60)))
        return min(self.BACKOFF_MAX_SECONDS, base * (2 ** max(0, failures - 1)))

    def _send(self, body: Dict[str, Any]) -> Dict[str, Any]:
        from .api import ApiRejected

        try:
            return self.api.inventory_snapshot(body)
        except ApiRejected as exc:
            if exc.status != 413:
                raise
            # Demasiado grande incluso comprimido: se informa un estado pequeño y explícito
            # (Nexus mostrará "No se pudo verificar la estructura").
            body.update(status="unreliable", reason_code="PAYLOAD_TOO_LARGE", objects=[],
                        schemas_verified=[], schemas_unverifiable=[])
            return self.api.inventory_snapshot(body)

    def tick(self) -> Dict[str, Any]:
        from .api import ApiError  # import local: evita ciclos

        cfg = self.get_config()
        if cfg is None:
            return {"skipped": "no_config"}
        try:
            lease = self.api.inventory_lease({"capabilities": capabilities(cfg), "client_version": AGENT_VERSION})
        except ApiError as exc:
            log.debug("Inventario: lease no disponible (%s).", exc.code)
            return {"skipped": "lease_failed", "code": exc.code}
        if self.on_api_ok:
            self.on_api_ok()
        results = []
        now = self.clock()
        for target in lease.get("targets") or []:
            if self.stop_event.is_set():
                break
            if not (target.get("granted") and target.get("due")):
                continue
            mid = int(target["monitored_database_id"])
            failures, not_before = self._backoff.get(mid, (0, 0.0))
            if now < not_before:
                results.append({"id": mid, "sent": False, "code": "backoff"})
                continue
            params = target_connection(target, cfg)
            if params is None:
                continue
            t0 = time.monotonic()
            body = build_snapshot(target, params, self.settings, self.connect)
            try:
                resp = self._send(body)
            except ApiError as exc:
                failures += 1
                delay = self.backoff_delay(failures)
                self._backoff[mid] = (failures, self.clock() + delay)
                log.warning("Inventario de la base #%s no enviado (%s); reintento en %.0fs.", mid, exc.code, delay)
                results.append({"id": mid, "sent": False, "code": exc.code, "retry_in": delay})
                continue
            self._backoff.pop(mid, None)
            log.info("Inventario de la base #%s: %s, %d objeto(s) en %.1fs%s.", mid,
                     body["status"], len(body.get("objects") or []), time.monotonic() - t0,
                     f" [{body['reason_code']}]" if body.get("reason_code") else "")
            results.append({"id": mid, "sent": True, "status": body["status"],
                            "reason_code": body.get("reason_code"), "response": resp})
        self.last_results = results
        return {"targets": len(lease.get("targets") or []), "results": results}

    def loop(self) -> None:
        interval = max(5.0, float(getattr(self.settings, "inventory_tick_seconds", 60)))
        # Pequeña espera inicial: primero se obtiene la configuración/autorización.
        if self.stop_event.wait(min(interval, 10.0)):
            return
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — el hilo nunca muere
                code, msg = sanitize_error(exc)
                log.warning("Inventario: error inesperado [%s] %s", code, msg)
            self.stop_event.wait(interval)
