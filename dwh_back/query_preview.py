"""
query_preview.py — "Tabla destino desde el query" (comandos ejecutados por el agente).

Generaliza el patrón request/claim/result de "Probar conexión"
(connection_tests_postgres.py, sección 22) a tres comandos que el panel puede
pedirle a un agente en línea sobre el origen/DWH de una empresa:

  * ``query_preview``  — ejecuta el query de extracción (limitado a una
    muestra) y devuelve columnas/tipos de origen + hasta 20 filas de ejemplo.
  * ``create_table``   — ejecuta en el DWH efectivo de la empresa el
    ``CREATE TABLE IF NOT EXISTS`` que generó el backend (idempotente;
    ``type_mapping.build_create_table_sql``).
  * ``upsert_check``   — vuelve a traer la muestra del origen (el agente la
    re-obtiene: Nexus nunca reenvía filas) y hace upsert dos veces dentro de
    una transacción que termina siempre en ROLLBACK.

Regla de arquitectura (igual que connection_tests_postgres.py): Nexus NUNCA
abre conexiones hacia las bases de los clientes. Regla de privacidad: las
FILAS de muestra nunca se guardan en la BD de Nexus, en activity_log ni en
panel_audit_log — solo viven en un almacén EN MEMORIA con vencimiento corto
(``PreviewRowStore``) y se entregan una única vez al usuario que pidió el
comando (mismo alcance/permiso). En la BD de Nexus (tabla ``agent_command``)
solo se guardan METADATOS del resultado (columnas, tipos, conteos, avisos,
duración) en ``result_meta``.
"""

import json
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

import hashlib

from destination_postgres import effective_columns_sql, qualify_table, warehouse_from_row
from redact import redact_text
from type_mapping import (
    DdlColumnSpec,
    DdlError,
    build_constraint_name,
    build_create_table_sql,
    build_unique_constraint_sql,
    is_safe_identifier,
    map_columns,
    SourceColumn,
)

log = logging.getLogger("nexus.query_preview")

KINDS = ("query_preview", "create_table", "upsert_check")
FEATURE = "query-preview"
MAX_SAMPLE_ROWS = 20
MAX_CELL_CHARS = 500
MAX_ROW_PAYLOAD_CHARS = 200_000  # tope defensivo del payload completo de filas


def http_error(status: int, code: str, message: str, retry_after: Optional[int] = None) -> HTTPException:
    detail: Dict[str, Any] = {"code": code, "message": message}
    headers = None
    if retry_after is not None:
        detail["retry_after"] = int(retry_after)
        headers = {"Retry-After": str(int(retry_after))}
    return HTTPException(status_code=status, detail=detail, headers=headers)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class QueryPreviewSettings:
    enabled: bool = True
    pending_ttl_seconds: int = 120
    running_ttl_seconds: int = 60
    online_seconds: int = 180
    agent_timeout_seconds: int = 30
    retention_days: int = 7
    poll_seconds: int = 10
    min_interval_seconds: int = 15
    max_open_per_group: int = 5
    max_per_user_per_minute: int = 10
    rows_ttl_seconds: int = 600  # cuánto vive la muestra en memoria (10 min)

    @classmethod
    def from_ini(cls, ini: Any) -> "QueryPreviewSettings":
        s = cls()
        sec = "query_preview"
        g = lambda k, d, lo: max(lo, ini.getint(sec, k, fallback=d))  # noqa: E731
        s.enabled = ini.getboolean(sec, "enabled", fallback=s.enabled)
        s.pending_ttl_seconds = g("pending_ttl_seconds", s.pending_ttl_seconds, 15)
        s.running_ttl_seconds = g("running_ttl_seconds", s.running_ttl_seconds, 15)
        s.online_seconds = g("online_seconds", s.online_seconds, 30)
        s.agent_timeout_seconds = min(300, g("agent_timeout_seconds", s.agent_timeout_seconds, 5))
        s.retention_days = g("retention_days", s.retention_days, 1)
        s.poll_seconds = min(300, g("poll_seconds", s.poll_seconds, 3))
        s.min_interval_seconds = g("min_interval_seconds", s.min_interval_seconds, 0)
        s.max_open_per_group = g("max_open_per_group", s.max_open_per_group, 1)
        s.max_per_user_per_minute = g("max_per_user_per_minute", s.max_per_user_per_minute, 1)
        s.rows_ttl_seconds = g("rows_ttl_seconds", s.rows_ttl_seconds, 30)
        return s


# ─────────────────────────────────────────────────────────────────────────────
# Almacén EN MEMORIA de la muestra de filas (nunca en BD, nunca en logs)
# ─────────────────────────────────────────────────────────────────────────────
class PreviewRowStore:
    """
    Filas de muestra por comando: vencen solas (TTL corto) y se borran en la
    PRIMERA lectura del usuario que pidió el comando. Nunca se serializan a
    disco ni se pasan a ningún logger; viven solo en este diccionario del
    proceso del backend (si hay varios workers, cada uno tiene su copia: el
    resultado se lee del worker que recibió el POST del agente o no se
    encuentra — el panel reintenta contra el mismo comando hasta encontrarlo
    o hasta que expire, igual que con cualquier resultado).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: Dict[str, tuple] = {}  # command_id -> (rows, user_id, expires_at_monotonic)

    def put(self, command_id: str, rows: List[List[Any]], user_id: Optional[int], ttl_seconds: int) -> None:
        with self._lock:
            self._rows[command_id] = (rows, user_id, time.monotonic() + ttl_seconds)

    def take(self, command_id: str, user_id: Optional[int]) -> Optional[List[List[Any]]]:
        """Devuelve las filas UNA sola vez para el usuario que las pidió; None si no hay o ya se leyeron."""
        with self._lock:
            entry = self._rows.get(command_id)
            if not entry:
                return None
            rows, owner, expires = entry
            if time.monotonic() > expires or owner != user_id:
                self._rows.pop(command_id, None)
                return None
            self._rows.pop(command_id, None)  # una sola lectura
            return rows

    def has(self, command_id: str) -> bool:
        with self._lock:
            entry = self._rows.get(command_id)
            return bool(entry and time.monotonic() <= entry[2])

    def discard(self, command_id: str) -> None:
        with self._lock:
            self._rows.pop(command_id, None)

    def sweep(self) -> int:
        now = time.monotonic()
        with self._lock:
            expired = [k for k, v in self._rows.items() if now > v[2]]
            for k in expired:
                self._rows.pop(k, None)
            return len(expired)


# ─────────────────────────────────────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────────────────────────────────────
class ColumnChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(..., max_length=63)
    pg_type: str = Field(..., max_length=40)
    nullable: bool = True


class CommandCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["query_preview", "create_table", "upsert_check"]
    company_id: int = Field(..., ge=1)
    agency_id: Optional[int] = Field(None, ge=1)
    object_catalog_id: Optional[int] = Field(None, ge=1)
    # query_preview / upsert_check: el agente ejecuta este query (re-fetch; nunca se le reenvían filas).
    extract_sql: Optional[str] = Field(None, max_length=20_000)
    sample_limit: int = Field(20, ge=1, le=MAX_SAMPLE_ROWS)
    # create_table / upsert_check: definición destino.
    destination_table: Optional[str] = Field(None, max_length=128)
    columns: List[ColumnChoice] = Field(default_factory=list, max_length=200)
    key_columns: List[str] = Field(default_factory=list, max_length=20)
    static_columns: Dict[str, Any] = Field(default_factory=dict)


class SourceColumnMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(..., max_length=200)
    source_type: str = Field("", max_length=100)
    nullable: bool = True
    length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None


class UpsertCheckReport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    inserted: int = Field(0, ge=0)
    updated: int = Field(0, ge=0)
    duplicate_keys_in_sample: int = Field(0, ge=0)
    null_keys_in_sample: int = Field(0, ge=0)
    column_errors: Dict[str, str] = Field(default_factory=dict)
    rolled_back: bool = True


class CommandResultBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["ok", "failed"]
    columns: List[SourceColumnMeta] = Field(default_factory=list, max_length=300)
    row_count: Optional[int] = Field(None, ge=0)
    # Filas de muestra: NUNCA se valida su contenido (evita que un 422 eco de Pydantic las
    # revele en el detalle del error, que sí puede quedar en logs de errores >=400). Se
    # acotan por código, nunca por una constraint que rechace el valor.
    rows: Optional[List[List[Any]]] = None
    upsert: Optional[UpsertCheckReport] = None
    duration_ms: Optional[int] = Field(None, ge=0, le=3_600_000)
    error_code: Optional[str] = Field(None, max_length=64)
    error_message: Optional[str] = Field(None, max_length=2000)
    agent_version: str = Field("", max_length=50)


def _command_fingerprint(kind: str, company_id: int, extract_sql: str, destination_table: str) -> str:
    """Huella del comando (para detectar repeticiones idénticas y espaciarlas)."""
    raw = "|".join([kind, str(company_id), extract_sql or "", destination_table or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+\-]{0,50}$")


def _cap_rows(rows: Optional[List[List[Any]]]) -> List[List[Any]]:
    """Acota filas/celdas por código (nunca por validación de Pydantic: ver CommandResultBody)."""
    if not rows:
        return []
    out: List[List[Any]] = []
    total = 0
    for row in rows[:MAX_SAMPLE_ROWS]:
        if not isinstance(row, list):
            continue
        clean_row = []
        for cell in row[:300]:
            s = cell if isinstance(cell, (int, float, bool)) or cell is None else str(cell)
            if isinstance(s, str) and len(s) > MAX_CELL_CHARS:
                s = s[:MAX_CELL_CHARS] + "…"
            clean_row.append(s)
        total += sum(len(str(c)) for c in clean_row)
        if total > MAX_ROW_PAYLOAD_CHARS:
            break
        out.append(clean_row)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Motor
# ─────────────────────────────────────────────────────────────────────────────
class AgentCommandEngine:
    def __init__(self, *, get_connection: Callable[[], Any], decrypt_config_secret: Callable[[Optional[str]], str],
                 settings: Optional[QueryPreviewSettings] = None, row_store: Optional[PreviewRowStore] = None
                 ) -> None:
        self.get_connection = get_connection
        self.decrypt = decrypt_config_secret
        self.s = settings or QueryPreviewSettings()
        self.rows = row_store or PreviewRowStore()

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
        return (f"({i}.group_id = {t}.group_id AND {i}.status = 'active' "
                f"AND ({i}.scope_type = 'group' OR {i}.company_id = {t}.company_id))")

    def online_sql(self, i: str = "i") -> str:
        return (f"({i}.last_seen_at >= NOW() - make_interval(secs => {int(self.s.online_seconds)}) "
                f"AND COALESCE({i}.last_heartbeat -> 'features', '[]'::jsonb) ? '{FEATURE}')")

    def expire(self, cur: Any) -> None:
        cur.execute(
            """UPDATE agent_command SET status = 'expired', finished_at = NOW(),
                      error_code = CASE WHEN status = 'pending' THEN 'NOT_CLAIMED' ELSE 'NO_RESULT' END,
                      message = CASE WHEN status = 'pending'
                                     THEN 'Ningún agente tomó el comando a tiempo.'
                                     ELSE 'El agente tomó el comando pero no devolvió el resultado a tiempo.' END
               WHERE status IN ('pending', 'running') AND expires_at < NOW()""")

    # ── Destino/origen efectivos (para el agente) ───────────────────────────
    def _company_row(self, cur: Any, company_id: int) -> Dict[str, Any]:
        cur.execute(f"""SELECT c.*, {effective_columns_sql()} FROM company c JOIN client_group g ON g.id = c.group_id
                        WHERE c.id = %s""", (company_id,))
        r = cur.fetchone()
        if not r:
            raise http_error(404, "not_found", "Empresa no encontrada.")
        return r

    def _source_params(self, r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "engine": (r["source_type"] or "sqlserver").lower(),
            "host": self.decrypt(r["source_host"] or ""), "port": r["source_port"],
            "database": self.decrypt(r["source_database"] or ""),
            "username": self.decrypt(r["source_username"] or ""),
            "password": self.decrypt(r["source_password"] or ""),
            "dsn": self.decrypt(r["source_dsn"] or ""),
        }

    def _warehouse(self, r: Dict[str, Any]) -> Dict[str, Any]:
        return warehouse_from_row(r, self.decrypt)

    @staticmethod
    def secrets_of(*param_dicts: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        for p in param_dicts:
            out.extend(str(p.get(k) or "") for k in ("host", "database", "username", "password", "dsn")
                       if p.get(k))
        return out

    SELECT = """SELECT t.*, i.name AS installation_name FROM agent_command t
                LEFT JOIN installation i ON i.id = t.installation_id"""

    def _with_suggested_types(self, cur: Any, r: Dict[str, Any], result_meta: Dict[str, Any]) -> Dict[str, Any]:
        """Agrega, por columna, el tipo Postgres SUGERIDO (mapeo único del backend; el panel lo deja editar)."""
        cols = result_meta.get("columns") or []
        if not cols:
            return result_meta
        try:
            company = self._company_row(cur, r["company_id"])
            engine = (company.get("source_type") or "sqlserver").lower()
        except HTTPException:
            engine = "sqlserver"
        source_cols = [SourceColumn(name=c.get("name") or "", source_type=c.get("source_type") or "",
                                    nullable=c.get("nullable", True), length=c.get("length"),
                                    precision=c.get("precision"), scale=c.get("scale")) for c in cols]
        mapped = map_columns(engine, source_cols)
        out_cols = []
        for original, m in zip(cols, mapped):
            out_cols.append({**original, "suggested_name": m.name, "renamed_from": m.renamed_from,
                             "suggested_pg_type": m.pg_type, "type_warning": m.warning})
        return {**result_meta, "columns": out_cols}

    def out(self, r: Dict[str, Any], *, cur: Any = None) -> Dict[str, Any]:
        result_meta = r.get("result_meta")
        if cur is not None and result_meta and r["kind"] == "query_preview":
            result_meta = self._with_suggested_types(cur, r, result_meta)
        d = {
            "id": str(r["id"]), "kind": r["kind"], "group_id": r["group_id"], "company_id": r["company_id"],
            "agency_id": r["agency_id"], "object_catalog_id": r["object_catalog_id"], "status": r["status"],
            "requested_by": r["requested_by"],
            "installation_id": str(r["installation_id"]) if r.get("installation_id") else None,
            "installation_name": r.get("installation_name"),
            "eligible_installations": r.get("eligible_installations") or 0,
            "created_at": iso(r["created_at"]), "claimed_at": iso(r.get("claimed_at")),
            "finished_at": iso(r.get("finished_at")), "expires_at": iso(r.get("expires_at")),
            "result": result_meta, "error_code": r.get("error_code"), "message": r.get("message"),
            "has_sample_rows": bool(r.get("has_pending_rows")) and self.rows.has(str(r["id"])),
        }
        return d

    # ── Panel ───────────────────────────────────────────────────────────────
    def create(self, cur: Any, ctx: Any, body: CommandCreate) -> Dict[str, Any]:
        from panel_auth import group_of

        if not self.s.enabled:
            raise http_error(503, "query_preview_disabled", "La ejecución de queries desde el panel está "
                                                            "deshabilitada.")
        gid = group_of(cur, "company", body.company_id, "Empresa")
        ctx.check("config.manage", gid, "Empresa")
        company = self._company_row(cur, body.company_id)
        allow_preview = bool(company.get("allow_data_preview", True))
        if body.kind == "query_preview" and not (body.extract_sql or "").strip():
            raise http_error(422, "extract_sql_required", "Indique el query a ejecutar.")
        if body.kind == "upsert_check" and not (body.extract_sql or "").strip():
            raise http_error(422, "extract_sql_required", "Indique el query a ejecutar.")
        request: Dict[str, Any] = {"sample_limit": min(body.sample_limit, MAX_SAMPLE_ROWS),
                                   "include_rows": allow_preview}
        if body.extract_sql:
            request["extract_sql"] = body.extract_sql
        if body.kind in ("create_table", "upsert_check"):
            table = (body.destination_table or "").strip().lower()
            if not table:
                raise http_error(422, "destination_table_required", "Indique la tabla destino.")
            base_table = table.split(".")[-1]
            if not is_safe_identifier(base_table):
                raise http_error(422, "invalid_table_name", f"Nombre de tabla no válido: «{base_table}».")
            schema = None if "." in table else (company.get("eff_wh_schema") or "public")
            if schema == "public":
                schema = None
            try:
                specs = [DdlColumnSpec(c.name, c.pg_type, c.nullable) for c in body.columns]
                if body.kind == "create_table":
                    ddl = build_create_table_sql(schema, base_table, specs, body.key_columns, body.static_columns)
                    request["ddl"] = ddl
                request["destination_table"] = qualify_table(base_table, schema)
                request["key_columns"] = list(body.key_columns)
                if body.key_columns:
                    request["constraint_name"] = build_constraint_name(base_table, body.key_columns)
            except DdlError as exc:
                raise http_error(422, "invalid_definition", str(exc)) from exc
        self.expire(cur)
        cur.execute("DELETE FROM agent_command WHERE created_at < NOW() - make_interval(days => %s) "
                    "AND status NOT IN ('pending', 'running')", (self.s.retention_days,))
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (834_120_601, gid))
        fp = _command_fingerprint(body.kind, body.company_id, body.extract_sql or "",
                                  request.get("destination_table") or "")
        cur.execute("""SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) AS ago FROM agent_command
                       WHERE company_id = %s AND kind = %s AND config_fingerprint = %s
                         AND status <> 'no_agent'""", (body.company_id, body.kind, fp))
        ago = cur.fetchone()["ago"]
        if ago is not None and float(ago) < self.s.min_interval_seconds:
            wait = int(self.s.min_interval_seconds - float(ago)) + 1
            raise http_error(429, "too_soon", f"Espere {wait} s antes de repetir este comando.", wait)
        cur.execute("SELECT COUNT(*) AS n FROM agent_command WHERE group_id = %s AND status IN ('pending','running')",
                    (gid,))
        if int(cur.fetchone()["n"]) >= self.s.max_open_per_group:
            raise http_error(429, "too_many_open", "Hay demasiados comandos en curso en este grupo; espere a que "
                                                   "terminen.", 30)
        cur.execute("""SELECT COUNT(*) AS n FROM agent_command WHERE created_at >= NOW() - interval '60 seconds'
                         AND requested_by_user_id IS NOT DISTINCT FROM %s""", (ctx.user_id,))
        if int(cur.fetchone()["n"]) >= self.s.max_per_user_per_minute:
            raise http_error(429, "rate_limited", "Demasiados comandos en el último minuto.", 60)
        cur.execute(
            f"""SELECT COUNT(*) AS n FROM installation i,
                       (SELECT %s::int AS group_id, %s::int AS company_id) t
                WHERE {self.eligible_sql()} AND {self.online_sql()}""",
            (gid, body.company_id))
        n = int(cur.fetchone()["n"])
        cid = uuid.uuid4()
        status = "pending" if n else "no_agent"
        cur.execute(
            """INSERT INTO agent_command (id, kind, group_id, company_id, agency_id, object_catalog_id, status,
                                          requested_by, requested_by_user_id, request, config_fingerprint,
                                          eligible_installations, expires_at, finished_at, error_code, message)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW() + make_interval(secs => %s),
                       CASE WHEN %s THEN NOW() END, %s, %s)""",
            (str(cid), body.kind, gid, body.company_id, body.agency_id, body.object_catalog_id, status,
             ctx.actor[:100], ctx.user_id, json.dumps(request), fp, n, self.s.pending_ttl_seconds,
             status == "no_agent", None if n else "NO_AGENT_ONLINE",
             None if n else ("No hay ningún agente en línea (versión 5.4 o superior) con alcance sobre esta "
                             "empresa.")))
        cur.execute(self.SELECT + " WHERE t.id = %s", (str(cid),))
        return self.out(cur.fetchone(), cur=cur)

    def get(self, cur: Any, ctx: Any, command_id: uuid.UUID) -> Dict[str, Any]:
        self.expire(cur)
        cur.execute(self.SELECT + " WHERE t.id = %s", (str(command_id),))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Comando no encontrado.")
        ctx.check("view", r["group_id"], "Comando")
        return self.out(r, cur=cur)

    def take_rows(self, ctx: Any, command_id: uuid.UUID) -> Optional[List[List[Any]]]:
        """Entrega la muestra UNA vez, solo al usuario que pidió el comando (no requiere BD)."""
        return self.rows.take(str(command_id), ctx.user_id)

    def list(self, cur: Any, ctx: Any, company_id: Optional[int], kind: Optional[str], limit: int
             ) -> List[Dict[str, Any]]:
        self.expire(cur)
        sc, params = ctx.scope_sql("t.group_id")
        conds = [sc]
        if company_id is not None:
            conds.append("t.company_id = %s")
            params.append(company_id)
        if kind is not None:
            conds.append("t.kind = %s")
            params.append(kind)
        cur.execute(self.SELECT + " WHERE " + " AND ".join(conds) + " ORDER BY t.created_at DESC LIMIT %s",
                    (*params, limit))
        return [self.out(r, cur=cur) for r in cur.fetchall()]

    # ── Agente ──────────────────────────────────────────────────────────────
    def pending_for(self, cur: Any, ctx: Any) -> int:
        cur.execute(
            f"""SELECT COUNT(*) AS n FROM agent_command t, installation i
                WHERE i.id = %s AND t.status = 'pending' AND t.expires_at >= NOW() AND {self.eligible_sql()}""",
            (str(ctx.id),))
        return int(cur.fetchone()["n"])

    def claim(self, cur: Any, ctx: Any) -> Dict[str, Any]:
        self.expire(cur)
        cur.execute(
            f"""UPDATE agent_command SET status = 'running', installation_id = %s, claimed_at = NOW(),
                       expires_at = NOW() + make_interval(secs => %s)
                WHERE id = (
                    SELECT t.id FROM agent_command t, installation i
                    WHERE i.id = %s AND t.status = 'pending' AND t.expires_at >= NOW() AND {self.eligible_sql()}
                      AND COALESCE(i.last_heartbeat -> 'features', '[]'::jsonb) ? '{FEATURE}'
                    ORDER BY t.created_at LIMIT 1 FOR UPDATE OF t SKIP LOCKED)
                RETURNING *""",
            (str(ctx.id), self.s.running_ttl_seconds + self.s.agent_timeout_seconds, str(ctx.id)))
        t = cur.fetchone()
        if not t:
            return {"command": None, "poll_seconds": self.s.poll_seconds}
        company = self._company_row(cur, t["company_id"])
        request = t["request"] or {}
        payload: Dict[str, Any] = {"id": str(t["id"]), "kind": t["kind"],
                                   "timeout_seconds": self.s.agent_timeout_seconds}
        if t["kind"] in ("query_preview", "upsert_check"):
            payload["source"] = self._source_params(company)
            payload["extract_sql"] = request.get("extract_sql", "")
            payload["sample_limit"] = request.get("sample_limit", MAX_SAMPLE_ROWS)
            payload["include_rows"] = bool(request.get("include_rows", True))
        if t["kind"] in ("create_table", "upsert_check"):
            payload["warehouse"] = self._warehouse(company)
            payload["destination_table"] = request.get("destination_table")
            payload["key_columns"] = request.get("key_columns") or []
            payload["constraint_name"] = request.get("constraint_name")
        if t["kind"] == "create_table":
            payload["ddl"] = request.get("ddl")
        log.info("Comando %s (%s) tomado por la instalación %s.", t["id"], t["kind"], ctx.id)
        return {"command": payload, "poll_seconds": self.s.poll_seconds}

    def result(self, cur: Any, ctx: Any, command_id: uuid.UUID, body: CommandResultBody) -> Dict[str, Any]:
        cur.execute("SELECT * FROM agent_command WHERE id = %s FOR UPDATE", (str(command_id),))
        t = cur.fetchone()
        if not t or str(t.get("installation_id") or "") != str(ctx.id):
            raise http_error(404, "not_found", "Comando no encontrado.")
        if t["status"] == "running" and t["expires_at"] is not None and t["expires_at"] < datetime.now(timezone.utc):
            cur.execute("""UPDATE agent_command SET status = 'expired', finished_at = NOW(), error_code = 'NO_RESULT',
                                  message = 'El agente devolvió el resultado fuera de plazo.' WHERE id = %s""",
                        (str(command_id),))
            return {"status": "expired", "_http_status": 410}
        if t["status"] != "running":
            raise http_error(409, "not_running", "El comando ya terminó o venció.")
        company = self._company_row(cur, t["company_id"])
        allow_preview = bool(company.get("allow_data_preview", True))
        request = t["request"] or {}
        secrets = self.secrets_of(self._source_params(company), self._warehouse(company))

        def clean(text: Optional[str], n: int) -> str:
            return redact_text(text or "", secrets=secrets, max_len=n)

        agent_version = clean(body.agent_version, 50)
        if not _VERSION_RE.match(agent_version):
            agent_version = "?"
        code = (body.error_code or "").strip()
        if code and not _CODE_RE.match(code):
            code = "INVALID_CODE"
        if code and secrets and any(sv in code for sv in secrets if len(sv) >= 4):
            code = "INVALID_CODE"
        columns_meta = [{"name": c.name[:200], "source_type": clean(c.source_type, 100), "nullable": c.nullable,
                         "length": c.length, "precision": c.precision, "scale": c.scale}
                        for c in body.columns[:300]]
        result_meta: Dict[str, Any] = {"columns": columns_meta, "row_count": body.row_count,
                                       "duration_ms": body.duration_ms, "agent_version": agent_version}
        if body.upsert is not None:
            result_meta["upsert"] = {**body.upsert.model_dump(),
                                     "column_errors": {k[:200]: clean(v, 300)
                                                      for k, v in list(body.upsert.column_errors.items())[:200]}}
        has_rows = False
        # Filas: NUNCA a BD/logs. Solo si el comando las pidió, la empresa lo permite y el
        # estado es ok; si no, se descartan en silencio (defensa en profundidad).
        if body.status == "ok" and allow_preview and request.get("include_rows", True) and body.rows:
            rows = _cap_rows(body.rows)
            if rows:
                self.rows.put(str(command_id), rows, t.get("requested_by_user_id"), self.s.rows_ttl_seconds)
                has_rows = True
        msg = clean(body.error_message, 500) or None
        cur.execute(
            """UPDATE agent_command SET status = %s, finished_at = NOW(), result_meta = %s, error_code = %s,
                      message = %s, has_pending_rows = %s,
                      request = request - 'extract_sql' - 'ddl'
               WHERE id = %s""",
            (body.status, json.dumps(result_meta), code or None, msg, has_rows, str(command_id)))
        return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# Rutas
# ─────────────────────────────────────────────────────────────────────────────
def register_agent_command_routes(agent: APIRouter, authenticate: Callable[..., Any],
                                  engine: AgentCommandEngine) -> None:
    @agent.post("/commands/claim")
    def claim(ctx: Any = Depends(authenticate)) -> dict:
        with engine.tx() as cur:
            return engine.claim(cur, ctx)

    @agent.post("/commands/{command_id}/result")
    def result(command_id: uuid.UUID, body: CommandResultBody, ctx: Any = Depends(authenticate)) -> Any:
        with engine.tx() as cur:
            out = engine.result(cur, ctx, command_id, body)
        if out.pop("_http_status", None) == 410:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=410, content={"detail": {"code": "expired",
                                                                     "message": "El comando venció."}})
        return out


def create_query_preview_admin_router(*, engine: AgentCommandEngine, auth: Any) -> APIRouter:
    VIEW = auth.perm("view")
    CONFIG = auth.perm("config.manage")
    router = APIRouter(prefix="/admin", tags=["query-preview"])

    @router.post("/query-commands", status_code=201)
    def create_command(body: CommandCreate, response: Response, ctx: Any = Depends(CONFIG)) -> dict:
        with engine.tx() as cur:
            out = engine.create(cur, ctx, body)
            auth.audit(ctx, action=f"query_command.{body.kind}", status_code=201, target_type="agent_command",
                       target_id=out["id"], group_id=out["group_id"],
                       details={"kind": body.kind, "company_id": out["company_id"], "status": out["status"],
                                "eligible_installations": out["eligible_installations"]},
                       cur=cur)
        return out

    @router.get("/query-commands/{command_id}")
    def get_command(command_id: uuid.UUID, include_rows: bool = Query(False),
                    ctx: Any = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            out = engine.get(cur, ctx, command_id)
        if include_rows and out["status"] == "ok":
            rows = engine.take_rows(ctx, command_id)
            if rows is not None:
                out["rows"] = rows
        return out

    @router.get("/query-commands")
    def list_commands(company_id: Optional[int] = Query(None),
                      kind: Optional[Literal["query_preview", "create_table", "upsert_check"]] = Query(None),
                      limit: int = Query(5, ge=1, le=50), ctx: Any = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            return {"items": engine.list(cur, ctx, company_id, kind, limit)}

    return router
