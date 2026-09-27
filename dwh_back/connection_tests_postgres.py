"""
connection_tests_postgres.py — "Probar conexión" ejecutada por el agente
────────────────────────────────────────────────────────────────────────
Regla de arquitectura: Nexus NUNCA abre conexiones hacia las bases ni los
servidores de los clientes. Toda conexión la inicia el agente (saliente, por
HTTPS hacia Nexus). Por eso la prueba es asíncrona:

  1. El panel pide ``POST /admin/connection-tests`` (permiso config.manage en el
     grupo) sobre la configuración GUARDADA de un destino u origen.
  2. Si ninguna instalación en línea con alcance sobre ese destino/origen (y que
     anuncie la capacidad ``connection-test``) puede tomarla → ``no_agent`` al
     instante. Si hay, queda ``pending``.
  3. El agente la toma con ``POST /agent/connection-tests/claim`` (solo pruebas
     de su alcance: nunca recibe credenciales que no recibiría ya para sus
     tareas), la ejecuta en solo lectura y responde con
     ``POST /agent/connection-tests/{id}/result`` (checks con códigos estables y
     mensajes saneados; Nexus vuelve a sanear con las credenciales del alcance).
  4. ``pending`` sin tomar o ``running`` sin resultado vencen (``expired``).

Alcance de una instalación sobre una prueba (mismo grupo y activa):
  * alcance grupo: cualquier prueba del grupo;
  * alcance empresa/agencia: pruebas de SU empresa (destino efectivo u origen) y
    la del DWH del grupo solo si su empresa lo hereda (ya recibe esas credenciales).
"""

import json
import logging
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from destination_postgres import config_fingerprint, effective_columns_sql, warehouse_from_row
from redact import redact_text

log = logging.getLogger("nexus.connection_tests")

TARGET_KINDS = ("group_dwh", "company_dwh", "company_source")
OPEN = ("pending", "running")
FEATURE = "connection-test"
_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+\-]{0,50}$")


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def http_error(status: int, code: str, message: str, retry_after: Optional[int] = None) -> HTTPException:
    detail: Dict[str, Any] = {"code": code, "message": message}
    headers = None
    if retry_after is not None:
        detail["retry_after"] = int(retry_after)
        headers = {"Retry-After": str(int(retry_after))}
    return HTTPException(status_code=status, detail=detail, headers=headers)


@dataclass
class ConnectionTestSettings:
    enabled: bool = True
    pending_ttl_seconds: int = 120      # sin agente que la tome → expired
    running_ttl_seconds: int = 120      # tomada sin resultado → expired
    online_seconds: int = 180           # instalación "en línea": último contacto dentro de esta ventana
    agent_timeout_seconds: int = 15     # connect/statement timeout que se pide al agente
    retention_days: int = 30
    poll_seconds: int = 10              # sugerencia al agente
    # Límites contra abuso (cada prueba es un inicio de sesión en la base del cliente: riesgo de
    # bloqueo de la cuenta por intentos): una abierta por destino (idempotente), un intervalo mínimo
    # entre pruebas del mismo destino, pendientes por grupo y solicitudes por usuario por minuto.
    min_interval_seconds: int = 30
    max_open_per_group: int = 5
    max_per_user_per_minute: int = 10

    @classmethod
    def from_ini(cls, ini: Any) -> "ConnectionTestSettings":
        s = cls()
        sec = "connection_test"
        g = lambda k, d, lo: max(lo, ini.getint(sec, k, fallback=d))  # noqa: E731
        s.enabled = ini.getboolean(sec, "enabled", fallback=s.enabled)
        s.pending_ttl_seconds = g("pending_ttl_seconds", s.pending_ttl_seconds, 15)
        s.running_ttl_seconds = g("running_ttl_seconds", s.running_ttl_seconds, 15)
        s.online_seconds = g("online_seconds", s.online_seconds, 30)
        s.agent_timeout_seconds = min(120, g("agent_timeout_seconds", s.agent_timeout_seconds, 3))
        s.retention_days = g("retention_days", s.retention_days, 1)
        s.poll_seconds = min(300, g("poll_seconds", s.poll_seconds, 3))
        s.min_interval_seconds = g("min_interval_seconds", s.min_interval_seconds, 0)
        s.max_open_per_group = g("max_open_per_group", s.max_open_per_group, 1)
        s.max_per_user_per_minute = g("max_per_user_per_minute", s.max_per_user_per_minute, 1)
        return s


# ─────────────────────────────────────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────────────────────────────────────
class TestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_kind: Literal["group_dwh", "company_dwh", "company_source"]
    group_id: Optional[int] = Field(None, ge=1)
    company_id: Optional[int] = Field(None, ge=1)


class CheckItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: str = Field(..., max_length=40, pattern=r"^[A-Z0-9_]{1,40}$")
    ok: Optional[bool] = None
    severity: Literal["error", "warning", "info"] = "info"
    message: str = Field("", max_length=500)


class TestResultBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["ok", "failed"]
    checks: List[CheckItem] = Field(default_factory=list, max_length=20)
    server_version: Optional[str] = Field(None, max_length=120)
    ssl_in_use: Optional[bool] = None
    ssl_version: Optional[str] = Field(None, max_length=20)
    schema_exists: Optional[bool] = None
    duration_ms: Optional[int] = Field(None, ge=0, le=3_600_000)
    error_code: Optional[str] = Field(None, max_length=64)
    error_message: Optional[str] = Field(None, max_length=2000)
    agent_version: str = Field("", max_length=50)


# ─────────────────────────────────────────────────────────────────────────────
# Motor
# ─────────────────────────────────────────────────────────────────────────────
class ConnectionTestEngine:
    def __init__(self, *, get_connection: Callable[[], Any], decrypt_config_secret: Callable[[Optional[str]], str],
                 settings: Optional[ConnectionTestSettings] = None) -> None:
        self.get_connection = get_connection
        self.decrypt = decrypt_config_secret
        self.s = settings or ConnectionTestSettings()

    @contextmanager
    def tx(self) -> Iterator[Any]:
        try:
            conn = self.get_connection()
        except psycopg2.OperationalError:
            raise http_error(503, "config_db_unavailable", "BD de configuración no disponible.")
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Alcance ─────────────────────────────────────────────────────────────
    @staticmethod
    def eligible_sql(t: str = "t", i: str = "i") -> str:
        """Condición SQL: la instalación ``i`` puede ejecutar la prueba ``t`` (sin mirar si está en línea)."""
        return f"""({i}.group_id = {t}.group_id AND {i}.status = 'active' AND (
                    {i}.scope_type = 'group'
                    OR ({t}.target_kind IN ('company_dwh', 'company_source') AND {i}.company_id = {t}.company_id)
                    OR ({t}.target_kind = 'group_dwh' AND {i}.company_id IS NOT NULL AND EXISTS (
                          SELECT 1 FROM company ci WHERE ci.id = {i}.company_id AND ci.warehouse_mode = 'inherit'))))"""

    def online_sql(self, i: str = "i") -> str:
        return (f"({i}.last_seen_at >= NOW() - make_interval(secs => {int(self.s.online_seconds)}) "
                f"AND COALESCE({i}.last_heartbeat -> 'features', '[]'::jsonb) ? '{FEATURE}')")

    def expire(self, cur: Any) -> None:
        cur.execute(
            """UPDATE connection_test SET status = 'expired', finished_at = NOW(),
                      error_code = CASE WHEN status = 'pending' THEN 'NOT_CLAIMED' ELSE 'NO_RESULT' END,
                      message = CASE WHEN status = 'pending'
                                     THEN 'Ningún agente tomó la prueba a tiempo (¿agente detenido o sin red?).'
                                     ELSE 'El agente tomó la prueba pero no devolvió el resultado a tiempo.' END
               WHERE status IN ('pending', 'running') AND expires_at < NOW()""")

    # ── Parámetros de conexión (solo para el agente autorizado) ─────────────
    def target_params(self, cur: Any, target_kind: str, group_id: int,
                      company_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if target_kind == "group_dwh":
            cur.execute("""SELECT warehouse_host AS eff_wh_host, warehouse_port AS eff_wh_port,
                                  warehouse_database AS eff_wh_database, warehouse_username AS eff_wh_username,
                                  warehouse_password AS eff_wh_password, warehouse_schema AS eff_wh_schema,
                                  warehouse_sslmode AS eff_wh_sslmode, warehouse_sslrootcert AS eff_wh_sslrootcert
                           FROM client_group WHERE id = %s""", (group_id,))
            r = cur.fetchone()
            if not r:
                return None
            wh = warehouse_from_row(r, self.decrypt, source="group")
            return dict(wh, kind="dwh", engine="postgresql", dsn="")
        cur.execute(f"""SELECT c.*, {effective_columns_sql()} FROM company c JOIN client_group g ON g.id = c.group_id
                        WHERE c.id = %s AND c.group_id = %s""", (company_id, group_id))
        r = cur.fetchone()
        if not r:
            return None
        if target_kind == "company_dwh":
            wh = warehouse_from_row(r, self.decrypt)
            return dict(wh, kind="dwh", engine="postgresql", dsn="")
        return {
            "kind": "source", "engine": (r["source_type"] or "sqlserver").lower(),
            "host": self.decrypt(r["source_host"] or ""), "port": r["source_port"],
            "database": self.decrypt(r["source_database"] or ""),
            "username": self.decrypt(r["source_username"] or ""),
            "password": self.decrypt(r["source_password"] or ""),
            "dsn": self.decrypt(r["source_dsn"] or ""),
        }

    @staticmethod
    def secrets_of(params: Optional[Dict[str, Any]]) -> List[str]:
        if not params:
            return []
        return [str(params.get(k) or "") for k in ("host", "database", "username", "password", "dsn")
                if params.get(k)]

    # ── Salida ──────────────────────────────────────────────────────────────
    def out(self, cur: Any, r: Dict[str, Any]) -> Dict[str, Any]:
        d = {
            "id": str(r["id"]), "target_kind": r["target_kind"], "group_id": r["group_id"],
            "company_id": r["company_id"], "status": r["status"], "requested_by": r["requested_by"],
            "installation_id": str(r["installation_id"]) if r.get("installation_id") else None,
            "installation_name": r.get("installation_name"),
            "eligible_installations": r.get("eligible_installations") or 0,
            "created_at": iso(r["created_at"]), "claimed_at": iso(r.get("claimed_at")),
            "finished_at": iso(r.get("finished_at")), "expires_at": iso(r.get("expires_at")),
            "result": r.get("result"), "error_code": r.get("error_code"), "message": r.get("message"),
        }
        # ¿La configuración cambió desde que se pidió la prueba?
        params = self.target_params(cur, r["target_kind"], r["group_id"], r["company_id"])
        d["config_changed"] = bool(params and r.get("config_fingerprint")
                                   and config_fingerprint(params) != (r["config_fingerprint"] or "").strip())
        return d

    SELECT = """SELECT t.*, i.name AS installation_name FROM connection_test t
                LEFT JOIN installation i ON i.id = t.installation_id"""

    # ── Panel ───────────────────────────────────────────────────────────────
    def create(self, cur: Any, ctx: Any, body: TestCreate) -> Dict[str, Any]:
        from panel_auth import group_of

        if not self.s.enabled:
            raise http_error(503, "connection_tests_disabled", "La prueba de conexión está deshabilitada.")
        if body.target_kind == "group_dwh":
            if not body.group_id:
                raise http_error(422, "group_required", "Indique group_id.")
            gid = group_of(cur, "group", body.group_id, "Grupo")
            ctx.check("config.manage", gid, "Grupo")
            company_id = None
        else:
            if not body.company_id:
                raise http_error(422, "company_required", "Indique company_id.")
            gid = group_of(cur, "company", body.company_id, "Empresa")
            ctx.check("config.manage", gid, "Empresa")
            company_id = body.company_id
        params = self.target_params(cur, body.target_kind, gid, company_id)
        if not params or not ((params.get("host") or "").strip() or (params.get("dsn") or "").strip()):
            raise http_error(422, "not_configured", "Guarde primero los datos de conexión (host o DSN).")
        self.expire(cur)
        cur.execute("DELETE FROM connection_test WHERE created_at < NOW() - make_interval(days => %s)",
                    (self.s.retention_days,))
        # Serializa las solicitudes del grupo (límites consistentes ante peticiones concurrentes).
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (834_120_600, gid))
        # 1) Idempotente: si ya hay una abierta para el mismo destino/origen, se devuelve esa.
        cur.execute(self.SELECT + """ WHERE t.target_kind = %s AND t.group_id = %s
                                        AND t.company_id IS NOT DISTINCT FROM %s AND t.status IN ('pending', 'running')
                                      ORDER BY t.created_at DESC LIMIT 1""", (body.target_kind, gid, company_id))
        open_row = cur.fetchone()
        if open_row:
            return dict(self.out(cur, open_row), reused=True)
        # 2) Intervalo mínimo entre pruebas del mismo destino (las "sin agente" no cuentan: no se conectó nadie).
        cur.execute("""SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) AS ago FROM connection_test
                       WHERE target_kind = %s AND group_id = %s AND company_id IS NOT DISTINCT FROM %s
                         AND status <> 'no_agent'""", (body.target_kind, gid, company_id))
        ago = cur.fetchone()["ago"]
        if ago is not None and float(ago) < self.s.min_interval_seconds:
            wait = int(self.s.min_interval_seconds - float(ago)) + 1
            raise http_error(429, "too_soon", f"Espere {wait} s antes de volver a probar esta conexión.", wait)
        # 3) Pendientes por grupo y solicitudes por usuario.
        cur.execute("SELECT COUNT(*) AS n FROM connection_test WHERE group_id = %s AND status IN ('pending', 'running')",
                    (gid,))
        if int(cur.fetchone()["n"]) >= self.s.max_open_per_group:
            raise http_error(429, "too_many_open", "Hay demasiadas pruebas en curso en este grupo; espere a que "
                                                   "terminen.", 30)
        cur.execute("""SELECT COUNT(*) AS n FROM connection_test WHERE created_at >= NOW() - interval '60 seconds'
                         AND requested_by = %s AND requested_by_user_id IS NOT DISTINCT FROM %s""",
                    (ctx.actor[:100], ctx.user_id))
        if int(cur.fetchone()["n"]) >= self.s.max_per_user_per_minute:
            raise http_error(429, "rate_limited", "Demasiadas pruebas de conexión en el último minuto.", 60)
        # Instalaciones en línea con alcance y capacidad (la prueba "virtual" t aún no existe).
        cur.execute(
            f"""SELECT COUNT(*) AS n FROM installation i,
                       (SELECT %s::text AS target_kind, %s::int AS group_id, %s::int AS company_id) t
                WHERE {self.eligible_sql()} AND {self.online_sql()}""",
            (body.target_kind, gid, company_id))
        n = int(cur.fetchone()["n"])
        tid = uuid.uuid4()
        status = "pending" if n else "no_agent"
        cur.execute(
            """INSERT INTO connection_test (id, target_kind, group_id, company_id, status, requested_by,
                                            requested_by_user_id, config_fingerprint, eligible_installations,
                                            expires_at, finished_at, error_code, message)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() + make_interval(secs => %s),
                       CASE WHEN %s THEN NOW() END, %s, %s)""",
            (str(tid), body.target_kind, gid, company_id, status, ctx.actor[:100], ctx.user_id,
             config_fingerprint(params), n, self.s.pending_ttl_seconds, status == "no_agent",
             None if n else "NO_AGENT_ONLINE",
             None if n else ("No hay ningún agente en línea (versión 5.3 o superior) con alcance sobre esta "
                             "conexión. La prueba la ejecuta siempre un agente: Nexus no se conecta a sus "
                             "servidores.")))
        cur.execute(self.SELECT + " WHERE t.id = %s", (str(tid),))
        return dict(self.out(cur, cur.fetchone()), reused=False)

    def get(self, cur: Any, ctx: Any, test_id: uuid.UUID) -> Dict[str, Any]:
        self.expire(cur)
        cur.execute(self.SELECT + " WHERE t.id = %s", (str(test_id),))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Prueba no encontrada.")
        ctx.check("view", r["group_id"], "Prueba")
        return self.out(cur, r)

    def latest(self, cur: Any, ctx: Any, target_kind: str, group_id: Optional[int],
               company_id: Optional[int], limit: int) -> List[Dict[str, Any]]:
        self.expire(cur)
        sc, params = ctx.scope_sql("t.group_id")
        conds = [sc, "t.target_kind = %s"]
        params.append(target_kind)
        if group_id is not None:
            conds.append("t.group_id = %s")
            params.append(group_id)
        if company_id is not None:
            conds.append("t.company_id = %s")
            params.append(company_id)
        cur.execute(self.SELECT + " WHERE " + " AND ".join(conds) + " ORDER BY t.created_at DESC LIMIT %s",
                    (*params, limit))
        return [self.out(cur, r) for r in cur.fetchall()]

    # ── Agente ──────────────────────────────────────────────────────────────
    def pending_for(self, cur: Any, ctx: Any) -> int:
        cur.execute(
            f"""SELECT COUNT(*) AS n FROM connection_test t, installation i
                WHERE i.id = %s AND t.status = 'pending' AND t.expires_at >= NOW() AND {self.eligible_sql()}""",
            (str(ctx.id),))
        return int(cur.fetchone()["n"])

    def claim(self, cur: Any, ctx: Any) -> Dict[str, Any]:
        self.expire(cur)
        cur.execute(
            f"""UPDATE connection_test SET status = 'running', installation_id = %s, claimed_at = NOW(),
                       expires_at = NOW() + make_interval(secs => %s)
                WHERE id = (
                    SELECT t.id FROM connection_test t, installation i
                    WHERE i.id = %s AND t.status = 'pending' AND t.expires_at >= NOW() AND {self.eligible_sql()}
                      -- Solo instalaciones que anunciaron la capacidad en su latido (mismas que cuentan como
                      -- "en línea" al pedir la prueba).
                      AND COALESCE(i.last_heartbeat -> 'features', '[]'::jsonb) ? '{FEATURE}'
                    ORDER BY t.created_at LIMIT 1 FOR UPDATE OF t SKIP LOCKED)
                RETURNING *""",
            (str(ctx.id), self.s.running_ttl_seconds + self.s.agent_timeout_seconds, str(ctx.id)))
        t = cur.fetchone()
        if not t:
            return {"test": None, "poll_seconds": self.s.poll_seconds}
        params = self.target_params(cur, t["target_kind"], t["group_id"], t["company_id"])
        if not params:
            cur.execute("""UPDATE connection_test SET status = 'failed', finished_at = NOW(),
                                  error_code = 'TARGET_GONE', message = 'La configuración ya no existe.'
                           WHERE id = %s""", (str(t["id"]),))
            return {"test": None, "poll_seconds": self.s.poll_seconds}
        log.info("Prueba de conexión %s tomada por la instalación %s (%s).", t["id"], ctx.id, t["target_kind"])
        return {"test": {"id": str(t["id"]), "target_kind": t["target_kind"], "kind": params["kind"],
                         "timeout_seconds": self.s.agent_timeout_seconds, "connection": params},
                "poll_seconds": self.s.poll_seconds}

    def result(self, cur: Any, ctx: Any, test_id: uuid.UUID, body: TestResultBody) -> Dict[str, Any]:
        cur.execute("SELECT * FROM connection_test WHERE id = %s FOR UPDATE", (str(test_id),))
        t = cur.fetchone()
        # Misma respuesta si no existe o no es de esta instalación (no se revela su existencia).
        if not t or str(t.get("installation_id") or "") != str(ctx.id):
            raise http_error(404, "not_found", "Prueba no encontrada.")
        if t["status"] == "running" and t["expires_at"] is not None and t["expires_at"] < datetime.now(timezone.utc):
            cur.execute("""UPDATE connection_test SET status = 'expired', finished_at = NOW(), error_code = 'NO_RESULT',
                                  message = 'El agente devolvió el resultado fuera de plazo.' WHERE id = %s""",
                        (str(test_id),))
            # 410 con la transacción confirmada (el vencimiento queda registrado).
            return {"status": "expired", "_http_status": 410}
        if t["status"] != "running":
            raise http_error(409, "not_running", "La prueba ya terminó o venció.")
        secrets = self.secrets_of(self.target_params(cur, t["target_kind"], t["group_id"], t["company_id"]))

        def clean(text: Optional[str], n: int) -> str:
            return redact_text(text or "", secrets=secrets, max_len=n)

        checks = [{"code": c.code, "ok": c.ok, "severity": c.severity, "message": clean(c.message, 300)}
                  for c in body.checks]
        agent_version = clean(body.agent_version, 50)
        if not _VERSION_RE.match(agent_version):
            agent_version = "?"
        # Código de error: solo códigos estables (mayúsculas, dígitos y _). Cualquier otra cosa podría
        # traer un dato sensible de un agente defectuoso u hostil y la ven usuarios con "view".
        code = (body.error_code or "").strip()
        if code and not _CODE_RE.match(code):
            code = "INVALID_CODE"
        if code and secrets and any(sv in code for sv in secrets if len(sv) >= 4):
            code = "INVALID_CODE"
        result = {"checks": checks, "server_version": clean(body.server_version, 120) or None,
                  "ssl_in_use": body.ssl_in_use, "ssl_version": clean(body.ssl_version, 20) or None,
                  "schema_exists": body.schema_exists, "duration_ms": body.duration_ms,
                  "agent_version": agent_version}
        msg = clean(body.error_message, 500) or None
        cur.execute(
            """UPDATE connection_test SET status = %s, finished_at = NOW(), result = %s, error_code = %s, message = %s
               WHERE id = %s""",
            (body.status, json.dumps(result), code or None, msg, str(test_id)))
        return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# Rutas
# ─────────────────────────────────────────────────────────────────────────────
def register_agent_connection_test_routes(agent: APIRouter, authenticate: Callable[..., Any],
                                          engine: ConnectionTestEngine) -> None:
    @agent.post("/connection-tests/claim")
    def claim(ctx: Any = Depends(authenticate)) -> dict:
        with engine.tx() as cur:
            return engine.claim(cur, ctx)

    @agent.post("/connection-tests/{test_id}/result")
    def result(test_id: uuid.UUID, body: TestResultBody, ctx: Any = Depends(authenticate)) -> Any:
        with engine.tx() as cur:
            out = engine.result(cur, ctx, test_id, body)
        if out.pop("_http_status", None) == 410:
            return JSONResponse(status_code=410, content={"detail": {"code": "expired",
                                                                     "message": "La prueba venció."}})
        return out


def create_connection_test_admin_router(*, engine: ConnectionTestEngine, auth: Any) -> APIRouter:
    VIEW = auth.perm("view")
    CONFIG = auth.perm("config.manage")
    router = APIRouter(prefix="/admin", tags=["connection-tests"])

    @router.post("/connection-tests", status_code=201)
    def create_test(body: TestCreate, response: Response, ctx: Any = Depends(CONFIG)) -> dict:
        with engine.tx() as cur:
            out = engine.create(cur, ctx, body)
            if out.get("reused"):
                response.status_code = 200   # ya había una abierta para ese destino: se devuelve esa
                return out
            auth.audit(ctx, action="connection_test.request", status_code=201, target_type="connection_test",
                       target_id=out["id"], group_id=out["group_id"],
                       details={"target_kind": out["target_kind"], "company_id": out["company_id"],
                                "status": out["status"], "eligible_installations": out["eligible_installations"]},
                       cur=cur)
        return out

    @router.get("/connection-tests/{test_id}")
    def get_test(test_id: uuid.UUID, ctx: Any = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            return engine.get(cur, ctx, test_id)

    @router.get("/connection-tests")
    def list_tests(target_kind: Literal["group_dwh", "company_dwh", "company_source"] = Query(...),
                   group_id: Optional[int] = Query(None), company_id: Optional[int] = Query(None),
                   limit: int = Query(5, ge=1, le=50), ctx: Any = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            return {"items": engine.latest(cur, ctx, target_kind, group_id, company_id, limit)}

    return router
