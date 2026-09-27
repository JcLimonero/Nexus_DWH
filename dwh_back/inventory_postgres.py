"""
inventory_postgres.py — Inventario estructural y cambios de estructura
──────────────────────────────────────────────────────────────────────
Sección 19 de DWH_README.md.

El inventario lo hace el AGENTE LOCAL (solo lectura, sin conexiones
entrantes) y lo reporta a Nexus:

  POST /agent/inventory/lease       qué bases puede/debe inventariar la
                                     instalación (una sola instalación por base:
                                     lease con vencimiento)
  POST /agent/inventory/snapshots   resultado (completo / parcial / no confiable)

El backend compara contra la LÍNEA BASE APROBADA (nunca se aprueba sola) y
genera una alerta por objeto con el detalle granular. "Dar por entendido"
incorpora a la línea base SOLO esa diferencia, con atribución manual
obligatoria (cliente / equipo Nexus) y concurrencia optimista.

Reglas de fiabilidad (nunca se infiere una eliminación sin evidencia):
  * snapshot no confiable (conexión caída, identidad del servidor distinta…)
    → no se compara; se conserva la línea base y el panel muestra
    "No se pudo verificar la estructura".
  * esquemas no verificables (sin USAGE, fuera del alcance) → sus objetos
    no se dan por eliminados.

Permisos reservados para la fase de RBAC (hoy: token de administrador; la
exposición del SQL de vistas además requiere [inventory] expose_view_definitions):
  inventory.configure, inventory.approve_baseline, inventory.view_definitions,
  structure.acknowledge, structure.reclassify
"""

import hashlib
import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from typing_extensions import Annotated, Literal

log = logging.getLogger("nexus.inventory")

PERMISSIONS = {
    "inventory.configure": "Configurar bases monitoreadas (alcance, frecuencia, origen opcional)",
    "inventory.approve_baseline": "Aprobar / reiniciar la línea base",
    "inventory.view_definitions": "Ver el SQL de definiciones de vistas",
    "structure.acknowledge": "Dar por entendido un cambio estructural",
    "structure.reclassify": "Reclasificar el responsable de un cambio ya entendido",
}

OBJECT_TYPES = ("table", "view", "matview", "foreign_table")
CHANGE_KINDS = ("object_added", "object_removed", "object_modified")
CHANGE_STATUSES = ("pending", "acknowledged", "superseded", "reverted", "out_of_scope")
ABSENT = "absent"
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
DEFAULT_PORTS = {"postgresql": 5432, "sqlserver": 1433, "mysql": 3306, "pervasive": 1583, "firebird": 3050}
SUPPORTED_ENGINES = ("postgresql",)

CHANGE_TYPE_LABELS = {
    "object_added": "Objeto nuevo",
    "object_removed": "Objeto eliminado",
    "object_modified": "Objeto modificado",
    "column_added": "Columna agregada",
    "column_removed": "Columna eliminada",
    "column_type_changed": "Tipo de columna",
    "column_nullability_changed": "Nulabilidad",
    "column_default_changed": "Valor por defecto",
    "column_attr_changed": "Atributo de columna",
    "constraint_added": "Restricción agregada",
    "constraint_removed": "Restricción eliminada",
    "constraint_changed": "Restricción modificada",
    "index_added": "Índice agregado",
    "index_removed": "Índice eliminado",
    "index_changed": "Índice modificado",
    "view_definition_changed": "Definición de vista",
    "object_attr_changed": "Atributo del objeto",
}

_HOUSEKEEPING_EVERY = 3600.0


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades puras (compartidas en espíritu con nexus_agent/inventory.py)
# ─────────────────────────────────────────────────────────────────────────────
def canonical_json(obj: Any) -> str:
    """JSON canónico: claves ordenadas, sin espacios. Base de las huellas."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def structure_fingerprint(structure: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(structure).encode("utf-8")).hexdigest()


def connection_identity(kind: str, engine: str, host: Any, port: Any, database: Any, dsn: Any = "") -> str:
    """
    Identidad ESTABLE de una base monitoreada a partir de su configuración.
    Mismo algoritmo en el agente (nexus_agent/inventory.py): el agente la
    recalcula con las credenciales que usó y el backend la compara.
    """
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


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def diff_structures(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Diferencias granulares entre dos estructuras normalizadas (columnas por
    nombre, restricciones e índices por nombre: el orden no importa).
    """
    old, new = old or {}, new or {}
    out: List[Dict[str, Any]] = []
    oc, nc = _dict(old.get("columns")), _dict(new.get("columns"))
    for name in sorted(set(nc) - set(oc)):
        out.append({"kind": "column_added", "item": name, "before": None, "after": nc[name]})
    for name in sorted(set(oc) - set(nc)):
        out.append({"kind": "column_removed", "item": name, "before": oc[name], "after": None})
    for name in sorted(set(oc) & set(nc)):
        a, b = _dict(oc[name]), _dict(nc[name])
        if a.get("type") != b.get("type"):
            out.append({"kind": "column_type_changed", "item": name, "before": a.get("type"), "after": b.get("type")})
        if bool(a.get("not_null")) != bool(b.get("not_null")):
            out.append({"kind": "column_nullability_changed", "item": name,
                        "before": "NOT NULL" if a.get("not_null") else "NULL",
                        "after": "NOT NULL" if b.get("not_null") else "NULL"})
        if a.get("default") != b.get("default"):
            out.append({"kind": "column_default_changed", "item": name, "before": a.get("default"),
                        "after": b.get("default")})
        rest_a = {k: v for k, v in a.items() if k not in ("type", "not_null", "default")}
        rest_b = {k: v for k, v in b.items() if k not in ("type", "not_null", "default")}
        if rest_a != rest_b:
            out.append({"kind": "column_attr_changed", "item": name, "before": rest_a, "after": rest_b})
    for section, prefix in (("constraints", "constraint"), ("indexes", "index")):
        o, n = _dict(old.get(section)), _dict(new.get(section))
        for name in sorted(set(n) - set(o)):
            out.append({"kind": f"{prefix}_added", "item": name, "before": None, "after": n[name]})
        for name in sorted(set(o) - set(n)):
            out.append({"kind": f"{prefix}_removed", "item": name, "before": o[name], "after": None})
        for name in sorted(set(o) & set(n)):
            if canonical_json(o[name]) != canonical_json(n[name]):
                out.append({"kind": f"{prefix}_changed", "item": name, "before": o[name], "after": n[name]})
    if old.get("definition_hash") != new.get("definition_hash"):
        # Solo huellas: el SQL de la vista nunca viaja en el detalle general.
        out.append({"kind": "view_definition_changed", "item": None,
                    "before": (old.get("definition_hash") or "")[:12] or None,
                    "after": (new.get("definition_hash") or "")[:12] or None})
    skip = {"columns", "constraints", "indexes", "definition_hash"}
    for k in sorted((set(old) | set(new)) - skip):
        if canonical_json(old.get(k)) != canonical_json(new.get(k)):
            out.append({"kind": "object_attr_changed", "item": k, "before": old.get(k), "after": new.get(k)})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class InventorySettings:
    enabled: bool = True
    dwh_auto_monitor: bool = True
    default_interval_seconds: int = 3600
    lease_ttl_seconds: int = 900
    stale_factor: float = 3.0
    snapshot_retention_days: int = 90
    max_objects_per_snapshot: int = 20000
    expose_view_definitions: bool = False
    default_schema_exclude: Tuple[str, ...] = ()
    evidence_window_days: int = 7
    collapse_partitions: bool = True

    @classmethod
    def from_ini(cls, ini: Any) -> "InventorySettings":
        s = cls()
        sec = "inventory"
        s.enabled = ini.getboolean(sec, "enabled", fallback=s.enabled)
        s.dwh_auto_monitor = ini.getboolean(sec, "dwh_auto_monitor", fallback=s.dwh_auto_monitor)
        s.default_interval_seconds = max(60, ini.getint(sec, "default_interval_seconds", fallback=s.default_interval_seconds))
        s.lease_ttl_seconds = max(30, ini.getint(sec, "lease_ttl_seconds", fallback=s.lease_ttl_seconds))
        s.stale_factor = max(1.0, ini.getfloat(sec, "stale_factor", fallback=s.stale_factor))
        s.snapshot_retention_days = max(0, ini.getint(sec, "snapshot_retention_days", fallback=s.snapshot_retention_days))
        s.max_objects_per_snapshot = max(10, ini.getint(sec, "max_objects_per_snapshot", fallback=s.max_objects_per_snapshot))
        s.expose_view_definitions = ini.getboolean(sec, "expose_view_definitions", fallback=s.expose_view_definitions)
        raw = ini.get(sec, "default_schema_exclude", fallback="")
        s.default_schema_exclude = tuple(p.strip() for p in raw.split(",") if p.strip())
        s.evidence_window_days = max(1, ini.getint(sec, "evidence_window_days", fallback=s.evidence_window_days))
        s.collapse_partitions = ini.getboolean(sec, "collapse_partitions", fallback=s.collapse_partitions)
        return s


def http_error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


# ─────────────────────────────────────────────────────────────────────────────
# Modelos de entrada
# ─────────────────────────────────────────────────────────────────────────────
class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


NameStr = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class LeaseCapabilities(_In):
    dwh: bool = False
    source_company_ids: List[int] = Field(default_factory=list, max_length=500)


class LeaseBody(_In):
    capabilities: LeaseCapabilities = Field(default_factory=LeaseCapabilities)
    client_version: str = Field("", max_length=50)
    release_ids: List[int] = Field(default_factory=list, max_length=500)


_COL_KEYS = ("type", "not_null", "default", "generated", "identity", "collation")
_SCALAR_KEYS = ("definition_hash", "partition_key", "partition_of", "partition_bound", "persistence")


def _txt(v: Any, n: int = 4000) -> Optional[str]:
    return None if v is None else str(v)[:n]


def sanitize_structure(v: Dict[str, Any]) -> Dict[str, Any]:
    """Solo claves conocidas (lista blanca) y tipos acotados; lo demás se descarta."""
    out: Dict[str, Any] = {}
    cols = v.get("columns")
    if isinstance(cols, dict):
        out["columns"] = {str(k)[:128]: {ck: (bool(cv) if ck == "not_null" else _txt(cv))
                                         for ck, cv in c.items() if ck in _COL_KEYS}
                          for k, c in cols.items() if isinstance(c, dict)}
    for sec, keys, flag in (("constraints", ("type", "definition"), None), ("indexes", ("unique", "definition"), "unique")):
        d = v.get(sec)
        if isinstance(d, dict):
            out[sec] = {str(k)[:128]: {ck: (bool(cv) if ck == flag else _txt(cv)) for ck, cv in c.items() if ck in keys}
                        for k, c in d.items() if isinstance(c, dict)}
    for k in _SCALAR_KEYS:
        if v.get(k) is not None:
            out[k] = _txt(v[k])
    if v.get("partitioned"):
        out["partitioned"] = True
    parts = v.get("partitions")
    if isinstance(parts, dict):
        out["partitions"] = {str(k)[:300]: _txt(b) for k, b in parts.items()}
    return out


class InvObject(_In):
    schema_name: NameStr
    name: NameStr
    type: Literal["table", "view", "matview", "foreign_table"]
    structure: Dict[str, Any]
    definition: Optional[str] = Field(None, max_length=500_000)

    @field_validator("structure")
    @classmethod
    def _structure_limits(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        v = sanitize_structure(v)
        if len(canonical_json(v)) > 512_000:
            raise ValueError("estructura de objeto demasiado grande")
        return v


class UnverifiableSchema(_In):
    schema_name: NameStr
    reason: str = Field("unverifiable", max_length=64)


class SnapshotBody(_In):
    snapshot_id: uuid.UUID
    monitored_database_id: int = Field(..., ge=1)
    config_fingerprint: str = Field(..., min_length=64, max_length=64)
    engine_identity: Optional[str] = Field(None, min_length=64, max_length=64)
    engine_identity_strength: Optional[str] = Field(None, max_length=10)
    engine_identity_weak: Optional[str] = Field(None, min_length=64, max_length=64)
    captured_at: datetime
    status: Literal["complete", "partial", "unreliable"]
    reason_code: Optional[str] = Field(None, max_length=64)
    schemas_verified: List[str] = Field(default_factory=list, max_length=10000)
    schemas_unverifiable: List[UnverifiableSchema] = Field(default_factory=list, max_length=10000)
    server_version: str = Field("", max_length=50)
    agent_version: str = Field("", max_length=50)
    objects: List[InvObject] = Field(default_factory=list)


class MonitoredCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["dwh", "source"]
    group_id: Optional[int] = None
    company_id: Optional[int] = None
    enabled: Optional[bool] = None
    scan_interval_seconds: Optional[int] = Field(None, ge=60, le=2592000)
    schema_include: Optional[List[str]] = Field(None, max_length=200)
    schema_exclude: Optional[List[str]] = Field(None, max_length=200)
    view_definitions_enabled: bool = False
    display_name: Optional[str] = Field(None, max_length=255)


class MonitoredUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    scan_interval_seconds: Optional[int] = Field(None, ge=60, le=2592000)
    schema_include: Optional[List[str]] = Field(None, max_length=200)
    schema_exclude: Optional[List[str]] = Field(None, max_length=200)
    view_definitions_enabled: Optional[bool] = None
    display_name: Optional[str] = Field(None, max_length=255)


class ObjectKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_name: NameStr
    name: NameStr
    type: Literal["table", "view", "matview", "foreign_table"]


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_snapshot_id: int
    object_keys: Optional[List[ObjectKey]] = Field(None, max_length=100000)
    comment: Optional[str] = Field(None, max_length=500)


class ResolveDuplicateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["merge", "undo"]
    reason: str = Field(..., min_length=5, max_length=500)


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=5, max_length=500)


class AcknowledgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Obligatoria: sin valor por defecto (atribución MANUAL, no prueba de autoría).
    attribution: Literal["client", "nexus"]
    comment: Optional[str] = Field(None, max_length=1000)
    ticket_ref: Optional[str] = Field(None, max_length=100)
    expected_version: int = Field(..., ge=1)
    expected_observed_fingerprint: str = Field(..., min_length=6, max_length=64)


class ReclassifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attribution: Literal["client", "nexus"]
    reason: str = Field(..., min_length=5, max_length=500)
    ticket_ref: Optional[str] = Field(None, max_length=100)
    expected_version: int = Field(..., ge=1)


def _clean_patterns(values: Optional[List[str]]) -> Optional[List[str]]:
    if values is None:
        return None
    out = []
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        if len(v) > 128:
            raise http_error(422, "invalid_pattern", "Patrón de esquema demasiado largo (máx. 128).")
        out.append(v)
    return sorted(set(out))


# ─────────────────────────────────────────────────────────────────────────────
# Motor
# ─────────────────────────────────────────────────────────────────────────────
class InventoryEngine:
    def __init__(self, *, get_connection: Callable[[], Any], settings: InventorySettings,
                 get_secret_cipher: Callable[[], Any], decrypt_config_secret: Callable[[Optional[str]], str],
                 future_tolerance_hours: int = 26) -> None:
        self.future_tolerance_hours = future_tolerance_hours
        self.get_connection = get_connection
        self.s = settings
        self.get_secret_cipher = get_secret_cipher
        self.decrypt = decrypt_config_secret
        self._last_housekeeping = 0.0

    # ── BD ──────────────────────────────────────────────────────────────────
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

    @contextmanager
    def savepoint(self, cur: Any, what: str) -> Iterator[None]:
        name = "sp_inv_" + uuid.uuid4().hex[:8]
        cur.execute(f"SAVEPOINT {name}")
        try:
            yield
            cur.execute(f"RELEASE SAVEPOINT {name}")
        except Exception as exc:  # noqa: BLE001
            cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
            log.error("Inventario: error aislado en %s: %s", what, type(exc).__name__)

    # ── Cifrado de definiciones de vistas ───────────────────────────────────
    def encrypt_definition(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        cipher = self.get_secret_cipher()
        if cipher is None:
            # Sin clave no se guarda el SQL (solo su hash): nunca en claro.
            return None
        return "ENC:" + cipher.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt_definition(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        try:
            return self.decrypt(value)
        except Exception:  # noqa: BLE001
            return None

    # ── Historial ───────────────────────────────────────────────────────────
    @staticmethod
    def _mdb_event(cur: Any, mdb_id: int, event_type: str, *, actor: str = "system", message: Optional[str] = None,
                   data: Optional[Dict[str, Any]] = None, actor_user_id: Optional[int] = None) -> None:
        cur.execute(
            """INSERT INTO monitored_database_event (monitored_database_id, event_type, actor, message, data,
                                                    actor_user_id)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (mdb_id, event_type, actor[:100], (message or None) and message[:1000], json.dumps(data or {}, default=str),
             actor_user_id),
        )

    @staticmethod
    def _change_event(cur: Any, change_id: int, event_type: str, *, actor: str = "system",
                      message: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
                      actor_user_id: Optional[int] = None) -> None:
        cur.execute(
            """INSERT INTO structural_change_event (change_id, event_type, actor, message, data, actor_user_id)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (change_id, event_type, actor[:100], (message or None) and message[:1000], json.dumps(data or {}, default=str),
             actor_user_id),
        )

    # ── Candidatos (bases que una instalación puede alcanzar) ───────────────
    def _company_ids_in_scope(self, cur: Any, ctx: Any) -> List[int]:
        if ctx.scope_type == "group":
            cur.execute("SELECT id FROM company WHERE group_id = %s", (ctx.group_id,))
            return [r["id"] for r in cur.fetchall()]
        return [ctx.company_id] if ctx.company_id else []

    def source_identity(self, company_row: Dict[str, Any]) -> str:
        return connection_identity(
            "source", company_row.get("source_type") or "sqlserver",
            self.decrypt(company_row.get("source_host") or ""), company_row.get("source_port"),
            self.decrypt(company_row.get("source_database") or ""), self.decrypt(company_row.get("source_dsn") or ""),
        )

    def dwh_identity(self, group_row: Dict[str, Any]) -> Optional[str]:
        host = self.decrypt(group_row.get("warehouse_host") or "")
        if not host.strip():
            return None
        return connection_identity("dwh", "postgresql", host, group_row.get("warehouse_port") or 5432,
                                   self.decrypt(group_row.get("warehouse_database") or ""))

    def candidates_for(self, cur: Any, ctx: Any) -> List[Dict[str, Any]]:
        """Bases (DWH del grupo y orígenes HABILITADOS explícitamente) al alcance de la instalación."""
        out: List[Dict[str, Any]] = []
        cur.execute("""SELECT id, name, warehouse_host, warehouse_port, warehouse_database
                       FROM client_group WHERE id = %s""", (ctx.group_id,))
        g = cur.fetchone()
        if g:
            key = self.dwh_identity(g)
            if key:
                dbname = self.decrypt(g.get("warehouse_database") or "")
                out.append({"kind": "dwh", "engine": "postgresql", "identity_key": key, "group_id": g["id"],
                            "company_id": None, "display_name": f"DWH {dbname} · {key[:6]}"[:255]})
        companies = self._company_ids_in_scope(cur, ctx)
        if companies:
            cur.execute(
                """SELECT c.id, c.name, c.group_id, c.source_type, c.source_host, c.source_port,
                          c.source_database, c.source_dsn
                   FROM company c
                   WHERE c.id = ANY(%s)
                     AND EXISTS (SELECT 1 FROM monitored_database m WHERE m.kind = 'source' AND m.company_id = c.id)""",
                (companies,),
            )
            for c in cur.fetchall():
                out.append({"kind": "source", "engine": (c["source_type"] or "sqlserver").lower(),
                            "identity_key": self.source_identity(c), "group_id": c["group_id"],
                            "company_id": c["id"], "display_name": f"Origen {c['name']}"[:255]})
        return out

    # ── Lease ───────────────────────────────────────────────────────────────
    def lease(self, cur: Any, ctx: Any, body: LeaseBody) -> Dict[str, Any]:
        if not self.s.enabled:
            return {"enabled": False, "targets": [], "lease_ttl_seconds": self.s.lease_ttl_seconds}
        caps = body.capabilities
        cands = self.candidates_for(cur, ctx)
        targets: List[Dict[str, Any]] = []
        kept: List[int] = []
        for c in cands:
            if c["kind"] == "dwh" and not caps.dwh:
                continue
            if c["kind"] == "source" and c["company_id"] not in set(caps.source_company_ids):
                continue
            cur.execute("SELECT * FROM monitored_database WHERE identity_key = %s FOR UPDATE", (c["identity_key"],))
            m = cur.fetchone()
            if m is None:
                if c["kind"] != "dwh" or not self.s.dwh_auto_monitor:
                    continue
                cur.execute(
                    """INSERT INTO monitored_database (kind, engine, identity_key, display_name, group_id,
                                                       scan_interval_seconds, schema_exclude, created_by)
                       VALUES ('dwh', %s, %s, %s, %s, %s, %s, 'system')
                       ON CONFLICT (identity_key) DO NOTHING RETURNING id""",
                    (c["engine"], c["identity_key"], c["display_name"], c["group_id"],
                     self.s.default_interval_seconds, list(self.s.default_schema_exclude)),
                )
                new = cur.fetchone()
                if new:
                    self._mdb_event(cur, new["id"], "created", message="Alta automática del DWH del grupo",
                                    data={"group_id": c["group_id"]})
                cur.execute("SELECT * FROM monitored_database WHERE identity_key = %s FOR UPDATE", (c["identity_key"],))
                m = cur.fetchone()
            cur.execute(
                """INSERT INTO monitored_database_link (monitored_database_id, group_id, company_id)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (monitored_database_id, group_id, company_id) DO UPDATE SET last_seen_at = NOW()""",
                (m["id"], c["group_id"], c["company_id"] or 0),
            )
            if m["id"] in body.release_ids and m["lease_installation_id"] is not None \
                    and str(m["lease_installation_id"]) == str(ctx.id):
                cur.execute("UPDATE monitored_database SET lease_installation_id = NULL, lease_until = NULL "
                            "WHERE id = %s", (m["id"],))
                self._mdb_event(cur, m["id"], "lease_released", data={"installation_id": str(ctx.id)})
                continue
            if not m["enabled"] or m["duplicate_of_id"] is not None:
                if m["lease_installation_id"] is not None and str(m["lease_installation_id"]) == str(ctx.id):
                    cur.execute("UPDATE monitored_database SET lease_installation_id = NULL, lease_until = NULL "
                                "WHERE id = %s", (m["id"],))
                continue
            kept.append(m["id"])
            cur.execute(
                """UPDATE monitored_database SET
                       lease_installation_id = %s,
                       lease_acquired_at = CASE WHEN lease_installation_id IS DISTINCT FROM %s::uuid
                                                THEN NOW() ELSE lease_acquired_at END,
                       lease_until = NOW() + make_interval(secs => %s), updated_at = NOW()
                   WHERE id = %s
                     AND (lease_installation_id IS NULL OR lease_installation_id = %s::uuid OR lease_until < NOW())
                   RETURNING (lease_acquired_at = NOW()) AS acquired, lease_until,
                             (last_attempt_at IS NULL
                              OR last_attempt_at <= NOW() - make_interval(secs => scan_interval_seconds)
                              OR (scan_requested_at IS NOT NULL AND scan_requested_at > last_attempt_at)) AS due""",
                (str(ctx.id), str(ctx.id), self.s.lease_ttl_seconds, m["id"], str(ctx.id)),
            )
            got = cur.fetchone()
            granted = got is not None
            if granted and got["acquired"]:
                self._mdb_event(cur, m["id"], "lease_acquired",
                                message=f"Inventario asignado a la instalación {ctx.name}",
                                data={"installation_id": str(ctx.id),
                                      "previous": str(m["lease_installation_id"]) if m["lease_installation_id"] else None})
            excl = sorted(set(m["schema_exclude"] or []) | set(self.s.default_schema_exclude))
            # Esquemas que ya tenían objetos: el agente los verifica explícitamente aunque ya no existan
            # (así una eliminación de esquema se registra solo si el inventario lo verificó).
            cur.execute("""SELECT schema_name FROM inventory_baseline WHERE monitored_database_id = %s
                           UNION SELECT schema_name FROM inventory_object_state
                           WHERE monitored_database_id = %s AND present""", (m["id"], m["id"]))
            expected = sorted({r["schema_name"] for r in cur.fetchall()})
            targets.append({
                "monitored_database_id": m["id"], "kind": m["kind"], "engine": m["engine"],
                "company_id": m["company_id"] if m["kind"] == "source" else None,
                "identity_key": m["identity_key"], "display_name": m["display_name"],
                "granted": granted, "due": bool(granted and got["due"]),
                "lease_until": iso(got["lease_until"]) if granted else None,
                "scan_interval_seconds": m["scan_interval_seconds"],
                "schema_include": list(m["schema_include"] or []), "schema_exclude": excl,
                "view_definitions_enabled": bool(m["view_definitions_enabled"]),
                "max_objects": self.s.max_objects_per_snapshot,
                "expected_schemas": expected,
                "collapse_partitions": self.s.collapse_partitions,
            })
        # Leases que la instalación ya no debe tener (alcance o capacidades cambiaron).
        cur.execute(
            """UPDATE monitored_database SET lease_installation_id = NULL, lease_until = NULL
               WHERE lease_installation_id = %s AND NOT (id = ANY(%s)) RETURNING id""",
            (str(ctx.id), kept or [0]),
        )
        for r in cur.fetchall():
            self._mdb_event(cur, r["id"], "lease_released", data={"installation_id": str(ctx.id), "reason": "scope"})
        return {"enabled": True, "targets": targets, "lease_ttl_seconds": self.s.lease_ttl_seconds,
                "server_time": iso(datetime.now(timezone.utc))}

    # ── Identidad del servidor (fuerte / débil) ─────────────────────────────
    @staticmethod
    def _engine_pair(strength: Optional[str], primary: Optional[str], weak: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        primary = (primary or "").strip() or None
        weak = (weak or "").strip() or None
        strong = primary if (primary and strength == "strong") else None
        return strong, weak or (primary if strength == "weak" else None)

    @staticmethod
    def engines_same(a: Tuple[Optional[str], Optional[str]], b: Tuple[Optional[str], Optional[str]]) -> Optional[bool]:
        """True/False si hay un componente COMPARABLE (fuerte vs fuerte; si no, débil vs débil); None si no."""
        if a[0] and b[0]:
            return a[0] == b[0]
        if a[1] and b[1]:
            return a[1] == b[1]
        return None

    def _store_engine(self, cur: Any, mdb_id: int, stored: Tuple[Optional[str], Optional[str]],
                      new: Tuple[Optional[str], Optional[str]]) -> None:
        strong = new[0] or stored[0]
        weak = new[1] or stored[1]
        cur.execute(
            """UPDATE monitored_database SET engine_identity = %s, engine_identity_strength = %s,
                      engine_identity_weak = %s WHERE id = %s""",
            (strong or weak, "strong" if strong else "weak", weak, mdb_id))

    def _find_same_engine(self, cur: Any, m: Dict[str, Any], new: Tuple[Optional[str], Optional[str]]) -> Optional[Dict[str, Any]]:
        """
        Otra base con la MISMA identidad FUERTE (system_identifier + oid + nombre). La débil
        (dirección:puerto + oid + nombre) colisiona trivialmente entre clientes distintos
        (p. ej. 127.0.0.1:5432 / oid 16384 / "dwh"), así que nunca basta para duplicar/fusionar.
        """
        if not new[0]:
            return None
        cur.execute(
            """SELECT * FROM monitored_database
               WHERE kind = %s AND id <> %s AND duplicate_of_id IS NULL
                 AND engine_identity = %s AND engine_identity_strength = 'strong'
               ORDER BY id LIMIT 1""",
            (m["kind"], m["id"], new[0]))
        return cur.fetchone()

    def _weak_suggestion(self, cur: Any, m: Dict[str, Any], new: Tuple[Optional[str], Optional[str]]) -> None:
        """Coincidencia SOLO de identidad débil: aviso no bloqueante para el administrador."""
        if not new[1]:
            return
        cur.execute(
            """SELECT id FROM monitored_database
               WHERE kind = %s AND id <> %s AND duplicate_of_id IS NULL
                 AND (engine_identity_weak = %s OR (engine_identity = %s AND engine_identity_strength = 'weak'))
               ORDER BY id LIMIT 5""", (m["kind"], m["id"], new[1], new[1]))
        ids = [r["id"] for r in cur.fetchall()]
        if ids:
            self._mdb_event(cur, m["id"], "possible_duplicate",
                            message="Posible duplicado (solo coincide la identidad débil: misma dirección interna, "
                                    f"oid y nombre que la(s) base(s) #{', #'.join(map(str, ids))}). No se fusiona ni "
                                    "se marca como duplicada; revíselo si corresponde.",
                            data={"candidates": ids})

    def same_owner(self, cur: Any, dst: Dict[str, Any], src: Dict[str, Any]) -> bool:
        """¿El registro original pertenece al mismo grupo (DWH) / empresa (origen) que el nuevo?"""
        if dst["kind"] != src["kind"]:
            return False
        if src["kind"] == "source":
            return bool(src.get("company_id")) and dst.get("company_id") == src.get("company_id")
        if not src.get("group_id"):
            return False
        if dst.get("group_id") == src["group_id"]:
            return True
        cur.execute("SELECT 1 FROM monitored_database_link WHERE monitored_database_id = %s AND group_id = %s",
                    (dst["id"], src["group_id"]))
        return cur.fetchone() is not None

    def identity_is_current(self, cur: Any, mdb: Dict[str, Any]) -> bool:
        """¿Alguna configuración vigente (grupo / empresa) apunta todavía a esta identidad?"""
        key = (mdb["identity_key"] or "").strip()
        if mdb["kind"] == "dwh":
            cur.execute("SELECT warehouse_host, warehouse_port, warehouse_database FROM client_group")
            return any(self.dwh_identity(g) == key for g in cur.fetchall())
        if not mdb.get("company_id"):
            return False
        cur.execute("SELECT * FROM company WHERE id = %s", (mdb["company_id"],))
        c = cur.fetchone()
        return bool(c) and self.source_identity(c) == key

    @staticmethod
    def _has_history(cur: Any, mdb_id: int) -> bool:
        cur.execute("""SELECT EXISTS (SELECT 1 FROM inventory_baseline WHERE monitored_database_id = %s)
                           OR EXISTS (SELECT 1 FROM structural_change WHERE monitored_database_id = %s) AS h""",
                    (mdb_id, mdb_id))
        return bool(cur.fetchone()["h"])

    def merge_into(self, cur: Any, src: Dict[str, Any], dst_id: int, *, actor: str, reason: str) -> Dict[str, Any]:
        """
        La base ``dst`` (con su línea base, alertas e historial) adopta la identidad de
        configuración de ``src`` (registro nuevo de la MISMA base física, p. ej. tras cambiar
        el nombre del host). ``src`` se elimina; sus snapshots, eventos y vínculos pasan a ``dst``.
        """
        cur.execute("SELECT COUNT(*) AS n FROM inventory_baseline WHERE monitored_database_id = %s", (src["id"],))
        nb = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM structural_change WHERE monitored_database_id = %s", (src["id"],))
        nc = cur.fetchone()["n"]
        if nb or nc:
            raise http_error(409, "merge_conflict",
                             "El registro nuevo ya tiene línea base o alertas propias; no se fusiona automáticamente.")
        cur.execute("SELECT * FROM monitored_database WHERE id = %s FOR UPDATE", (dst_id,))
        dst = cur.fetchone()
        old_key = (dst["identity_key"] or "").strip()
        cur.execute("UPDATE inventory_snapshot SET monitored_database_id = %s WHERE monitored_database_id = %s",
                    (dst_id, src["id"]))
        cur.execute("UPDATE monitored_database_event SET monitored_database_id = %s WHERE monitored_database_id = %s",
                    (dst_id, src["id"]))
        # Los vínculos del registro original ya no son vigentes (su configuración cambió).
        cur.execute("DELETE FROM monitored_database_link WHERE monitored_database_id = %s", (dst_id,))
        cur.execute("UPDATE monitored_database_link SET monitored_database_id = %s WHERE monitored_database_id = %s",
                    (dst_id, src["id"]))
        cur.execute("UPDATE monitored_database SET duplicate_of_id = NULL WHERE duplicate_of_id = %s", (src["id"],))
        cur.execute("DELETE FROM monitored_database WHERE id = %s", (src["id"],))
        cur.execute(
            """UPDATE monitored_database SET identity_key = %s, group_id = %s, company_id = %s, display_name = %s,
                      lease_installation_id = %s, lease_until = %s, lease_acquired_at = %s,
                      duplicate_of_id = NULL, updated_at = NOW()
               WHERE id = %s RETURNING *""",
            (src["identity_key"], src["group_id"], src["company_id"], src["display_name"],
             src["lease_installation_id"], src["lease_until"], src["lease_acquired_at"], dst_id))
        new = cur.fetchone()
        self._mdb_event(cur, dst_id, "identity_rebound", actor=actor,
                        message=f"La configuración cambió pero es la misma base física: se conserva la línea base "
                                f"y el historial (fusionado el registro #{src['id']}). {reason}".strip(),
                        data={"previous_identity": old_key[:12], "new_identity": (src["identity_key"] or "")[:12],
                              "merged_from": src["id"], "reason": reason})
        return new

    # ── Recepción de snapshots ──────────────────────────────────────────────
    def ingest_snapshot(self, cur: Any, ctx: Any, body: SnapshotBody) -> Dict[str, Any]:
        if len(body.objects) > self.s.max_objects_per_snapshot:
            raise http_error(413, "too_many_objects", "El inventario supera max_objects_per_snapshot.")
        cur.execute("SELECT * FROM monitored_database WHERE id = %s FOR UPDATE", (body.monitored_database_id,))
        m = cur.fetchone()
        # Misma respuesta si no existe o si está fuera del alcance (no se revela su existencia).
        cands = {c["identity_key"] for c in self.candidates_for(cur, ctx)}
        if m is None or m["identity_key"].strip() not in cands:
            raise http_error(404, "unknown_database", "Base monitoreada inexistente o fuera del alcance.")
        if m["lease_installation_id"] is None or str(m["lease_installation_id"]) != str(ctx.id):
            raise http_error(409, "lease_not_held", "Otra instalación es responsable del inventario de esta base.")
        cur.execute("SELECT id, status, processing FROM inventory_snapshot WHERE snapshot_uuid = %s "
                    "AND monitored_database_id = %s", (str(body.snapshot_id), m["id"]))
        dup = cur.fetchone()
        if dup:
            return {"status": "duplicate", "snapshot_id": dup["id"], "result": dup["processing"]}
        cur.execute("SELECT 1 FROM inventory_snapshot WHERE snapshot_uuid = %s", (str(body.snapshot_id),))
        if cur.fetchone():
            raise http_error(409, "snapshot_id_conflict", "Ese snapshot_id ya se usó para otra base.")
        if not m["enabled"] or m["duplicate_of_id"] is not None:
            raise http_error(409, "monitoring_disabled", "El monitoreo de esta base está deshabilitado.")
        if body.config_fingerprint != m["identity_key"].strip():
            raise http_error(409, "identity_mismatch",
                             "La conexión usada no corresponde a la identidad de la base monitoreada.")
        # Reloj del agente adelantado: no se acepta (congelaría el monitoreo al rechazar lo posterior).
        captured = body.captured_at if body.captured_at.tzinfo else body.captured_at.replace(tzinfo=timezone.utc)
        if captured > datetime.now(timezone.utc) + timedelta(hours=self.future_tolerance_hours):
            raise http_error(422, "invalid_time", "captured_at está en el futuro.")
        # Un inventario más viejo que el último aceptado nunca pisa datos más nuevos.
        cur.execute("""SELECT MAX(captured_at) AS last FROM inventory_snapshot WHERE monitored_database_id = %s
                         AND captured_at <= NOW() + make_interval(hours => %s)""",
                    (m["id"], self.future_tolerance_hours))
        last = cur.fetchone()["last"]
        if last is not None and captured < last:
            # Cuenta como intento (no se vuelve "vencida" en cada ciclo del agente).
            cur.execute("UPDATE monitored_database SET last_attempt_at = NOW(), scan_requested_at = NULL "
                        "WHERE id = %s", (m["id"],))
            return {"status": "ignored", "reason": "stale_snapshot", "latest_captured_at": iso(last)}

        status, reason = body.status, body.reason_code
        merged_from = None
        new_eng = self._engine_pair(body.engine_identity_strength, body.engine_identity, body.engine_identity_weak)
        if status != "unreliable" and m["kind"] == "source" and m["engine"] not in SUPPORTED_ENGINES:
            status, reason = "unreliable", "ENGINE_UNSUPPORTED"
        if status != "unreliable":
            stored = self._engine_pair(m["engine_identity_strength"], m["engine_identity"], m["engine_identity_weak"])
            if not new_eng[0] and not new_eng[1]:
                status, reason = "unreliable", "ENGINE_IDENTITY_UNAVAILABLE"
            elif not stored[0] and not stored[1]:
                other = None if m["allow_engine_duplicate"] else self._find_same_engine(cur, m, new_eng)
                if other is None and not m["allow_engine_duplicate"]:
                    self._weak_suggestion(cur, m, new_eng)
                if other is not None:
                    if self.identity_is_current(cur, other) or self._has_history(cur, m["id"]) \
                            or not self.same_owner(cur, other, m):
                        # La misma base física sigue monitoreada con otra configuración vigente
                        # (p. ej. otro grupo con otro nombre de host): no se duplica nada.
                        # (Nunca se mueve línea base ni alertas entre grupos: sin fusión automática.)
                        cur.execute(
                            """UPDATE monitored_database SET duplicate_of_id = %s, lease_installation_id = NULL,
                                      lease_until = NULL, updated_at = NOW() WHERE id = %s""", (other["id"], m["id"]))
                        self._store_engine(cur, m["id"], (None, None), new_eng)
                        self._mdb_event(cur, m["id"], "duplicate_detected",
                                        message=f"Es la misma base que la #{other['id']}: no se inventaría dos veces.",
                                        data={"duplicate_of_id": other["id"]})
                        return {"status": "rejected", "code": "duplicate_database", "duplicate_of_id": other["id"]}
                    # La configuración original ya no apunta a esa base: el registro original
                    # (línea base + historial) adopta la nueva identidad; no se pierde el monitoreo.
                    merged_from = m["id"]
                    m = self.merge_into(cur, m, other["id"], actor="system", reason="Cambio de configuración.")
                    stored = self._engine_pair(m["engine_identity_strength"], m["engine_identity"],
                                               m["engine_identity_weak"])
                self._store_engine(cur, m["id"], stored, new_eng)
            elif self.engines_same(stored, new_eng) is False:
                # Otro servidor responde en la misma dirección: no se compara (no son "eliminaciones").
                status, reason = "unreliable", "ENGINE_IDENTITY_CHANGED"
                if m["last_reason_code"] != "ENGINE_IDENTITY_CHANGED":
                    self._mdb_event(cur, m["id"], "identity_changed",
                                    message="El servidor que respondió no es el mismo de la línea base; "
                                            "no se compara (reinicie la línea base si el cambio es intencional).")
            else:
                # Compatible (p. ej. se pasó de identidad débil a fuerte): se completan los componentes.
                self._store_engine(cur, m["id"], stored, new_eng)

        include = list(m["schema_include"] or [])
        exclude = sorted(set(m["schema_exclude"] or []) | set(self.s.default_schema_exclude))
        objects = [o for o in body.objects if schema_in_scope(o.schema_name, include, exclude)] \
            if status != "unreliable" else []
        unverifiable = {u.schema_name for u in body.schemas_unverifiable}
        verified = sorted({s for s in body.schemas_verified if schema_in_scope(s, include, exclude)} - unverifiable)
        verified_set = set(verified)
        if status != "unreliable" and not objects:
            # Inventario vacío cuando ya había objetos: conservador, no se infieren eliminaciones masivas.
            cur.execute("""SELECT schema_name FROM inventory_object_state WHERE monitored_database_id = %s AND present
                           UNION SELECT schema_name FROM inventory_baseline WHERE monitored_database_id = %s""",
                        (m["id"], m["id"]))
            if any(schema_in_scope(r["schema_name"], include, exclude) for r in cur.fetchall()):
                status, reason = "unreliable", "EMPTY_SNAPSHOT_SUSPICIOUS"

        def removal_ok(schema: str) -> bool:
            # Solo se infiere eliminación en esquemas que ESTE inventario verificó explícitamente.
            return schema in verified_set and schema_in_scope(schema, include, exclude)

        snap_fp = hashlib.sha256("\n".join(sorted(
            f"{o.schema_name}.{o.name}:{o.type}:{structure_fingerprint(o.structure)}" for o in objects
        )).encode()).hexdigest() if status != "unreliable" else None
        cur.execute(
            """INSERT INTO inventory_snapshot
                   (snapshot_uuid, monitored_database_id, installation_id, captured_at, status, reason_code,
                    object_count, snapshot_fingerprint, schemas_verified, schemas_unverifiable, agent_version,
                    server_version, engine_identity, engine_identity_weak)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (str(body.snapshot_id), m["id"], str(ctx.id), body.captured_at, status, reason,
             len(objects) if status != "unreliable" else 0, snap_fp,
             json.dumps(verified), json.dumps([u.model_dump() for u in body.schemas_unverifiable]),
             body.agent_version, body.server_version, new_eng[0], new_eng[1]),
        )
        snap_id = cur.fetchone()["id"]
        extra = {"monitored_database_id": m["id"]}
        if merged_from:
            extra["merged_from"] = merged_from

        if status == "unreliable":
            if m["verification_status"] != "unverifiable":
                self._mdb_event(cur, m["id"], "verification_failed",
                                message="No se pudo verificar la estructura; se conserva la referencia anterior.",
                                data={"reason_code": reason})
            cur.execute(
                """UPDATE monitored_database SET verification_status = 'unverifiable', last_attempt_at = NOW(),
                          last_snapshot_id = %s, last_reason_code = %s, updated_at = NOW()
                   WHERE id = %s""", (snap_id, reason, m["id"]))
            result = {"status": "ok", "verification": "unverifiable", "reason_code": reason}
            cur.execute("UPDATE inventory_snapshot SET processing = %s WHERE id = %s", (json.dumps(result), snap_id))
            self.maybe_housekeeping(cur)
            return dict(result, snapshot_id=snap_id, **extra)

        # ── Estado observado ────────────────────────────────────────────────
        observed: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        store_defs = bool(m["view_definitions_enabled"])
        rows = []
        for o in objects:
            fp = structure_fingerprint(o.structure)
            dh = o.structure.get("definition_hash")
            denc = self.encrypt_definition(o.definition) if (store_defs and o.definition) else None
            key = (o.schema_name, o.name, o.type)
            observed[key] = {"fingerprint": fp, "structure": o.structure, "definition_hash": dh,
                             "definition_enc": denc}
            rows.append((m["id"], o.schema_name, o.name, o.type, fp, json.dumps(o.structure, default=str),
                         dh, denc, snap_id))
        if rows:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO inventory_object_state
                       (monitored_database_id, schema_name, object_name, object_type, fingerprint, structure,
                        definition_hash, definition_enc, last_snapshot_id)
                   VALUES %s
                   ON CONFLICT (monitored_database_id, schema_name, object_name, object_type) DO UPDATE SET
                       fingerprint = EXCLUDED.fingerprint, structure = EXCLUDED.structure,
                       definition_hash = EXCLUDED.definition_hash,
                       definition_enc = COALESCE(EXCLUDED.definition_enc,
                                                 CASE WHEN inventory_object_state.definition_hash
                                                           IS NOT DISTINCT FROM EXCLUDED.definition_hash
                                                      THEN inventory_object_state.definition_enc END),
                       present = TRUE, missing_since = NULL, last_seen_at = NOW(),
                       last_snapshot_id = EXCLUDED.last_snapshot_id""",
                rows, page_size=1000,
            )
        # Ausentes: solo en esquemas que este inventario verificó.
        cur.execute(
            """SELECT schema_name, object_name, object_type FROM inventory_object_state
               WHERE monitored_database_id = %s AND present""", (m["id"],))
        missing = [r for r in cur.fetchall()
                   if (r["schema_name"], r["object_name"], r["object_type"]) not in observed
                   and removal_ok(r["schema_name"])]
        if missing:
            psycopg2.extras.execute_values(
                cur,
                "UPDATE inventory_object_state s SET present = FALSE, missing_since = NOW(), "
                f"last_snapshot_id = {int(snap_id)} FROM (VALUES %s) AS v(sn, obn, ot) "
                f"WHERE s.monitored_database_id = {int(m['id'])} AND s.schema_name = v.sn "
                "AND s.object_name = v.obn AND s.object_type = v.ot",
                [(r["schema_name"], r["object_name"], r["object_type"]) for r in missing],
            )

        result: Dict[str, Any] = {"status": "ok", "verification": "verified" if status == "complete" else "partial",
                                  "objects": len(objects), "missing": len(missing)}
        if m["state"] == "awaiting_first_snapshot":
            cur.execute("UPDATE monitored_database SET state = 'baseline_pending' WHERE id = %s", (m["id"],))
            self._mdb_event(cur, m["id"], "baseline_proposed",
                            message="Primer inventario recibido: pendiente de aprobación como línea base.",
                            data={"snapshot_id": snap_id, "objects": len(objects)})
            result["state"] = "baseline_pending"
        elif m["state"] == "monitoring":
            result.update(self._diff_and_alert(cur, m, snap_id, observed, removal_ok, include, exclude))
        cur.execute(
            """UPDATE monitored_database SET verification_status = %s, last_attempt_at = NOW(),
                      last_verified_at = NOW(), last_verified_snapshot_id = %s, last_snapshot_id = %s,
                      last_reason_code = %s, updated_at = NOW()
               WHERE id = %s""",
            ("verified" if status == "complete" else "partial", snap_id, snap_id, reason, m["id"]))
        if m["verification_status"] == "unverifiable":
            self._mdb_event(cur, m["id"], "verification_recovered", message="Estructura verificada de nuevo.")
        cur.execute("UPDATE inventory_snapshot SET processing = %s WHERE id = %s", (json.dumps(result), snap_id))
        self.maybe_housekeeping(cur)
        return dict(result, snapshot_id=snap_id, **extra)

    def close_out_of_scope(self, cur: Any, m: Dict[str, Any], actor: str = "system") -> int:
        """Alertas pendientes de esquemas que quedaron fuera del alcance → out_of_scope (historial intacto)."""
        include = list(m["schema_include"] or [])
        exclude = sorted(set(m["schema_exclude"] or []) | set(self.s.default_schema_exclude))
        cur.execute("SELECT id, schema_name FROM structural_change WHERE monitored_database_id = %s "
                    "AND status = 'pending' FOR UPDATE", (m["id"],))
        n = 0
        for r in cur.fetchall():
            if schema_in_scope(r["schema_name"], include, exclude):
                continue
            cur.execute("""UPDATE structural_change SET status = 'out_of_scope', status_changed_at = NOW(),
                                  row_version = row_version + 1, updated_at = NOW() WHERE id = %s""", (r["id"],))
            self._change_event(cur, r["id"], "out_of_scope", actor=actor,
                               message="El esquema quedó fuera del alcance del monitoreo; la alerta se cierra.")
            n += 1
        return n

    # ── Comparación contra la línea base ────────────────────────────────────
    def _observed_from_state(self, cur: Any, mdb_id: int) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        cur.execute(
            """SELECT schema_name, object_name, object_type, fingerprint, structure, definition_hash, definition_enc
               FROM inventory_object_state WHERE monitored_database_id = %s AND present""", (mdb_id,))
        return {(r["schema_name"], r["object_name"], r["object_type"]): {
            "fingerprint": r["fingerprint"].strip(), "structure": r["structure"],
            "definition_hash": (r["definition_hash"] or "").strip() or None, "definition_enc": r["definition_enc"]}
            for r in cur.fetchall()}

    def _diff_and_alert(self, cur: Any, m: Dict[str, Any], snap_id: Optional[int],
                        observed: Dict[Tuple[str, str, str], Dict[str, Any]], removal_ok: Callable[[str], bool],
                        include: List[str], exclude: List[str]) -> Dict[str, int]:
        mdb_id = m["id"]
        cur.execute("SELECT * FROM inventory_baseline WHERE monitored_database_id = %s", (mdb_id,))
        baseline = {(r["schema_name"], r["object_name"], r["object_type"]): r for r in cur.fetchall()}
        cur.execute("SELECT * FROM structural_change WHERE monitored_database_id = %s AND status = 'pending' "
                    "FOR UPDATE", (mdb_id,))
        pending = {(r["schema_name"], r["object_name"], r["object_type"]): r for r in cur.fetchall()}
        cur.execute("SELECT baseline_version FROM monitored_database WHERE id = %s", (mdb_id,))
        bver = cur.fetchone()["baseline_version"]
        stats = {"detected": 0, "superseded": 0, "reverted": 0, "observed_again": 0, "skipped_unverifiable": 0,
                 "out_of_scope": 0}
        since = self._evidence_since(cur, mdb_id, snap_id)
        for key in sorted(set(observed) | set(baseline) | set(pending)):
            schema = key[0]
            o, b, p = observed.get(key), baseline.get(key), pending.get(key)
            if not schema_in_scope(schema, include, exclude):
                # Fuera del alcance actual: no se compara; su alerta pendiente se cierra como out_of_scope.
                if p is not None:
                    cur.execute("""UPDATE structural_change SET status = 'out_of_scope', status_changed_at = NOW(),
                                          row_version = row_version + 1, updated_at = NOW() WHERE id = %s""",
                                (p["id"],))
                    self._change_event(cur, p["id"], "out_of_scope",
                                       message="El esquema quedó fuera del alcance del monitoreo; la alerta se cierra.")
                    stats["out_of_scope"] += 1
                continue
            if o is None and not removal_ok(schema):
                # Esquema no verificado en este inventario: NO se infiere eliminación ni se toca la alerta.
                stats["skipped_unverifiable"] += 1
                continue
            target = o["fingerprint"] if o else ABSENT
            base = b["fingerprint"].strip() if b else ABSENT
            if target == base:
                if p is not None:
                    cur.execute(
                        """UPDATE structural_change SET status = 'reverted', status_changed_at = NOW(),
                                  row_version = row_version + 1, updated_at = NOW(), last_snapshot_id = %s
                           WHERE id = %s""", (snap_id, p["id"]))
                    self._change_event(cur, p["id"], "reverted",
                                       message="El objeto volvió a coincidir con la línea base aprobada.")
                    stats["reverted"] += 1
                continue
            if p is not None and p["observed_fingerprint"].strip() == target \
                    and (p["baseline_fingerprint"] or "").strip() == ("" if b is None else base):
                cur.execute(
                    """UPDATE structural_change SET last_observed_at = NOW(), observation_count = observation_count + 1,
                              last_snapshot_id = %s, updated_at = NOW() WHERE id = %s""", (snap_id, p["id"]))
                stats["observed_again"] += 1
                continue
            if p is not None:
                # El objeto volvió a cambiar: la alerta que el usuario vio NO se modifica en silencio.
                cur.execute(
                    """UPDATE structural_change SET status = 'superseded', status_changed_at = NOW(),
                              row_version = row_version + 1, updated_at = NOW() WHERE id = %s""", (p["id"],))
                stats["superseded"] += 1
            new_id = self._insert_change(cur, m, key, b, o, snap_id, bver, since, supersedes=p)
            if p is not None:
                cur.execute("UPDATE structural_change SET superseded_by_id = %s WHERE id = %s", (new_id, p["id"]))
                self._change_event(cur, p["id"], "superseded",
                                   message=f"El objeto volvió a cambiar; reemplazada por la alerta #{new_id}.",
                                   data={"superseded_by_id": new_id})
            stats["detected"] += 1
        return stats

    def _insert_change(self, cur: Any, m: Dict[str, Any], key: Tuple[str, str, str], b: Optional[Dict[str, Any]],
                       o: Optional[Dict[str, Any]], snap_id: Optional[int], bver: int, since: datetime,
                       supersedes: Optional[Dict[str, Any]]) -> int:
        if b is None:
            kind = "object_added"
            diffs: List[Dict[str, Any]] = [{"kind": "object_added", "item": None, "before": None, "after": None}]
        elif o is None:
            kind = "object_removed"
            diffs = [{"kind": "object_removed", "item": None, "before": None, "after": None}]
        else:
            kind = "object_modified"
            diffs = diff_structures(b["structure"], o["structure"])
            if not diffs:
                diffs = [{"kind": "object_attr_changed", "item": "fingerprint", "before": None, "after": None}]
        types = sorted({d["kind"] for d in diffs} | {kind})
        evidence = self._evidence(cur, m, key, kind, diffs, since)
        cur.execute(
            """INSERT INTO structural_change
                   (monitored_database_id, schema_name, object_name, object_type, change_kind, change_types, diffs,
                    baseline_fingerprint, observed_fingerprint, baseline_version, previous_structure, current_structure,
                    previous_definition_hash, current_definition_hash, previous_definition_enc, current_definition_enc,
                    first_snapshot_id, last_snapshot_id, supersedes_id, evidence)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (m["id"], key[0], key[1], key[2], kind, types, json.dumps(diffs, default=str),
             b["fingerprint"].strip() if b else None, o["fingerprint"] if o else ABSENT, bver,
             json.dumps(b["structure"], default=str) if b else None,
             json.dumps(o["structure"], default=str) if o else None,
             ((b.get("definition_hash") or "").strip() or None) if b else None,
             (o.get("definition_hash") or None) if o else None,
             b.get("definition_enc") if b else None, o.get("definition_enc") if o else None,
             snap_id, snap_id, supersedes["id"] if supersedes else None, json.dumps(evidence, default=str)),
        )
        cid = cur.fetchone()["id"]
        self._change_event(cur, cid, "detected", message=CHANGE_TYPE_LABELS.get(kind, kind),
                           data={"change_types": types, "snapshot_id": snap_id,
                                 "supersedes_id": supersedes["id"] if supersedes else None,
                                 "evidence_items": len(evidence)})
        return cid

    # ── Evidencia técnica (no es atribución) ────────────────────────────────
    def _evidence_since(self, cur: Any, mdb_id: int, snap_id: Optional[int]) -> datetime:
        cur.execute(
            """SELECT COALESCE(
                   (SELECT received_at FROM inventory_snapshot
                     WHERE monitored_database_id = %s AND status <> 'unreliable' AND id <> COALESCE(%s, 0)
                     ORDER BY id DESC LIMIT 1),
                   NOW() - make_interval(days => %s)) - INTERVAL '1 hour' AS since""",
            (mdb_id, snap_id, self.s.evidence_window_days))
        return cur.fetchone()["since"]

    @staticmethod
    def _linked_groups(cur: Any, mdb: Dict[str, Any]) -> List[int]:
        cur.execute("SELECT DISTINCT group_id FROM monitored_database_link WHERE monitored_database_id = %s",
                    (mdb["id"],))
        ids = {r["group_id"] for r in cur.fetchall()}
        if mdb.get("group_id"):
            ids.add(mdb["group_id"])
        return sorted(ids)

    @staticmethod
    def _ddl_relevant(action: str, columns: List[str], kind: str, diffs: List[Dict[str, Any]]) -> bool:
        """La evidencia debe coincidir con el TIPO de cambio (no basta con que sea el mismo objeto)."""
        added_cols = {d["item"] for d in diffs if d["kind"] == "column_added"}
        if action == "create_table":
            return kind == "object_added"
        if action == "add_column":
            return kind == "object_modified" and bool(added_cols & set(columns or []))
        if action == "constraint_ddl":
            return kind == "object_modified" and any(d["kind"] in ("constraint_added", "constraint_changed")
                                                     for d in diffs)
        return False

    def _evidence(self, cur: Any, m: Dict[str, Any], key: Tuple[str, str, str], kind: str,
                  diffs: List[Dict[str, Any]], since: datetime) -> List[Dict[str, Any]]:
        if key[2] != "table" or kind == "object_removed":
            return []
        out: List[Dict[str, Any]] = []
        groups = self._linked_groups(cur, m)
        if not groups:
            return out
        full = f"{key[0]}.{key[1]}".lower()
        cur.execute(
            """SELECT oc.id, oc.name, oc.company_id, oc.destination_table
               FROM object_catalog oc JOIN company c ON c.id = oc.company_id
               WHERE c.group_id = ANY(%s)""", (groups,))
        for r in cur.fetchall():
            dt = (r["destination_table"] or "").strip().lower()
            if (dt if "." in dt else "public." + dt) == full:
                out.append({"type": "nexus_catalog", "object_catalog_id": r["id"], "object_name": r["name"],
                            "company_id": r["company_id"],
                            "note": "El objeto coincide con una tabla destino del catálogo Nexus."})
        cur.execute(
            """SELECT execution_id, task_id, installation_id, finished_at, ddl_applied
               FROM task_execution
               WHERE ddl_applied <> '[]'::jsonb AND group_id = ANY(%s) AND finished_at >= %s
                 AND ddl_applied @> %s::jsonb
               ORDER BY finished_at DESC LIMIT 20""",
            (groups, since, json.dumps([{"object": full}])))
        for r in cur.fetchall():
            for item in r["ddl_applied"] or []:
                if str(item.get("object", "")).lower() != full:
                    continue
                if self._ddl_relevant(item.get("action", ""), item.get("columns") or [], kind, diffs):
                    out.append(self._exec_evidence(r, item, diffs))
        return out

    @staticmethod
    def _exec_evidence(r: Dict[str, Any], item: Dict[str, Any], diffs: List[Dict[str, Any]]) -> Dict[str, Any]:
        added = {d["item"] for d in diffs if d["kind"] == "column_added"}
        return {"type": "nexus_execution", "execution_id": str(r["execution_id"]), "task_id": r["task_id"],
                "installation_id": str(r["installation_id"]) if r["installation_id"] else None,
                "finished_at": iso(r["finished_at"]), "action": item.get("action"),
                "columns": [c for c in (item.get("columns") or []) if c in added][:50],
                "note": "Una ejecución del agente Nexus aplicó DDL sobre este objeto "
                        "(evidencia técnica; no prueba autoría)."}

    def on_execution_ddl(self, cur: Any, ex: Dict[str, Any], ddl_applied: List[Dict[str, Any]]) -> None:
        """Reporte de ejecución que llega DESPUÉS de la detección: adjunta evidencia a alertas pendientes."""
        if not ddl_applied or ex.get("group_id") is None:
            return
        with self.savepoint(cur, "evidencia de ejecución"):
            for item in ddl_applied:
                full = str(item.get("object", "")).lower()
                if "." not in full:
                    continue
                schema, name = full.split(".", 1)
                cur.execute(
                    """SELECT sc.* FROM structural_change sc
                       JOIN monitored_database m ON m.id = sc.monitored_database_id
                       WHERE sc.status = 'pending' AND sc.object_type = 'table'
                         AND lower(sc.schema_name) = %s AND lower(sc.object_name) = %s
                         AND (m.group_id = %s OR EXISTS (SELECT 1 FROM monitored_database_link l
                              WHERE l.monitored_database_id = m.id AND l.group_id = %s))
                       FOR UPDATE OF sc""",
                    (schema, name, ex["group_id"], ex["group_id"]))
                for sc in cur.fetchall():
                    if not self._ddl_relevant(item.get("action", ""), item.get("columns") or [], sc["change_kind"],
                                              sc["diffs"] or []):
                        continue
                    ev = list(sc["evidence"] or [])
                    if any(e.get("execution_id") == str(ex["execution_id"]) and e.get("action") == item.get("action")
                           for e in ev):
                        continue
                    ev.append(self._exec_evidence(ex, item, sc["diffs"] or []))
                    cur.execute("UPDATE structural_change SET evidence = %s, updated_at = NOW() WHERE id = %s",
                                (json.dumps(ev, default=str), sc["id"]))
                    self._change_event(cur, sc["id"], "evidence_attached",
                                       message="Se adjuntó evidencia de una ejecución Nexus (no es atribución).",
                                       data={"execution_id": str(ex["execution_id"]), "action": item.get("action")})

    # ── Línea base ──────────────────────────────────────────────────────────
    def approve_baseline(self, mdb_id: int, body: ApproveBody, actor: str = "admin",
                         actor_user_id: Optional[int] = None) -> Dict[str, Any]:
        with self.tx() as cur:
            cur.execute("SELECT * FROM monitored_database WHERE id = %s FOR UPDATE", (mdb_id,))
            m = cur.fetchone()
            if m is None:
                raise http_error(404, "not_found", "Base monitoreada inexistente.")
            if m["state"] != "baseline_pending":
                raise http_error(409, "not_pending_approval",
                                 "La base no tiene una propuesta de línea base pendiente de aprobación.")
            if m["last_verified_snapshot_id"] != body.expected_snapshot_id:
                raise http_error(409, "snapshot_changed",
                                 "Llegó un inventario más reciente: revise la propuesta actualizada antes de aprobar.",
                                 latest_snapshot_id=m["last_verified_snapshot_id"])
            observed = self._observed_from_state(cur, mdb_id)
            include = list(m["schema_include"] or [])
            exclude = sorted(set(m["schema_exclude"] or []) | set(self.s.default_schema_exclude))
            observed = {k: v for k, v in observed.items() if schema_in_scope(k[0], include, exclude)}
            if body.object_keys is not None:
                wanted = {(k.schema_name, k.name, k.type) for k in body.object_keys}
                unknown = wanted - set(observed)
                if unknown:
                    raise http_error(422, "unknown_objects", f"{len(unknown)} objeto(s) no están en la propuesta.")
            else:
                wanted = set(observed)
            version = m["baseline_version"] + 1
            cur.execute("DELETE FROM inventory_baseline WHERE monitored_database_id = %s", (mdb_id,))
            rows = [(mdb_id, k[0], k[1], k[2], v["fingerprint"], json.dumps(v["structure"], default=str),
                     v["definition_hash"], v["definition_enc"], actor) for k, v in observed.items() if k in wanted]
            if rows:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO inventory_baseline (monitored_database_id, schema_name, object_name, object_type,
                           fingerprint, structure, definition_hash, definition_enc, approved_by, origin)
                       VALUES %s""",
                    rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, 'initial_approval')", page_size=1000)
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO inventory_baseline_version (monitored_database_id, baseline_version, schema_name,
                           object_name, object_type, action, fingerprint, structure, definition_hash, actor, comment)
                       VALUES %s""",
                    [(mdb_id, version, r[1], r[2], r[3], "approved", r[4], r[5], r[6], actor, body.comment)
                     for r in rows], page_size=1000)
            cur.execute(
                """UPDATE monitored_database SET state = 'monitoring', baseline_version = %s,
                          baseline_approved_at = NOW(), baseline_approved_by = %s,
                          baseline_approved_by_user_id = %s, updated_at = NOW()
                   WHERE id = %s""", (version, actor, actor_user_id, mdb_id))
            cur.execute("""UPDATE inventory_baseline_version SET actor_user_id = %s
                           WHERE monitored_database_id = %s AND baseline_version = %s""",
                        (actor_user_id, mdb_id, version))
            self._mdb_event(cur, mdb_id, "baseline_approved", actor=actor, message=body.comment,
                            data={"snapshot_id": body.expected_snapshot_id, "approved_objects": len(rows),
                                  "not_approved": len(observed) - len(rows), "baseline_version": version},
                            actor_user_id=actor_user_id)
            # Lo NO aprobado queda como alerta (objeto nuevo) para revisarlo con "Dar por entendido".
            cur.execute("SELECT schemas_verified FROM inventory_snapshot WHERE id = %s",
                        (body.expected_snapshot_id,))
            srow = cur.fetchone() or {}
            ver = set(srow.get("schemas_verified") or [])
            cur.execute("SELECT * FROM monitored_database WHERE id = %s", (mdb_id,))
            m2 = cur.fetchone()
            stats = self._diff_and_alert(cur, m2, body.expected_snapshot_id, observed, lambda sc: sc in ver,
                                         include, exclude)
        return {"status": "ok", "baseline_version": version, "approved_objects": len(rows),
                "pending_changes": stats["detected"]}

    def reset_baseline(self, mdb_id: int, reason: str, actor: str = "admin",
                       actor_user_id: Optional[int] = None) -> Dict[str, Any]:
        with self.tx() as cur:
            cur.execute("SELECT * FROM monitored_database WHERE id = %s FOR UPDATE", (mdb_id,))
            m = cur.fetchone()
            if m is None:
                raise http_error(404, "not_found", "Base monitoreada inexistente.")
            version = m["baseline_version"] + 1
            cur.execute(
                """UPDATE structural_change SET status = 'superseded', status_changed_at = NOW(),
                          row_version = row_version + 1, updated_at = NOW()
                   WHERE monitored_database_id = %s AND status = 'pending' RETURNING id""", (mdb_id,))
            closed = [r["id"] for r in cur.fetchall()]
            for cid in closed:
                self._change_event(cur, cid, "baseline_reset", actor=actor,
                                   message=f"Línea base reiniciada: {reason}", actor_user_id=actor_user_id)
            cur.execute(
                """INSERT INTO inventory_baseline_version (monitored_database_id, baseline_version, schema_name,
                       object_name, object_type, action, fingerprint, structure, definition_hash, actor, comment)
                   SELECT monitored_database_id, %s, schema_name, object_name, object_type, 'reset', fingerprint,
                          structure, definition_hash, %s, %s
                   FROM inventory_baseline WHERE monitored_database_id = %s""",
                (version, actor, reason[:500], mdb_id))
            cur.execute("""UPDATE inventory_baseline_version SET actor_user_id = %s
                           WHERE monitored_database_id = %s AND baseline_version = %s""",
                        (actor_user_id, mdb_id, version))
            cur.execute("DELETE FROM inventory_baseline WHERE monitored_database_id = %s", (mdb_id,))
            # Se espera un inventario NUEVO (la identidad del servidor se vuelve a fijar con él).
            cur.execute(
                """UPDATE monitored_database SET state = 'awaiting_first_snapshot', baseline_version = %s,
                          engine_identity = NULL, engine_identity_strength = NULL, baseline_approved_at = NULL,
                          baseline_approved_by = NULL, baseline_approved_by_user_id = NULL,
                          scan_requested_at = NOW(), updated_at = NOW()
                   WHERE id = %s""", (version, mdb_id))
            self._mdb_event(cur, mdb_id, "baseline_reset", actor=actor, message=reason,
                            data={"pending_closed": len(closed), "baseline_version": version},
                            actor_user_id=actor_user_id)
        return {"status": "ok", "pending_closed": len(closed), "state": "awaiting_first_snapshot"}

    # ── Dar por entendido / reclasificar ────────────────────────────────────
    def acknowledge(self, change_id: int, body: AcknowledgeBody, actor: str = "admin",
                    actor_user_id: Optional[int] = None) -> Dict[str, Any]:
        with self.tx() as cur:
            cur.execute("SELECT monitored_database_id FROM structural_change WHERE id = %s", (change_id,))
            r = cur.fetchone()
            if r is None:
                raise http_error(404, "not_found", "Cambio estructural inexistente.")
            # Mismo orden de bloqueo que la recepción de snapshots (base → alerta): sin deadlocks.
            cur.execute("SELECT * FROM monitored_database WHERE id = %s FOR UPDATE", (r["monitored_database_id"],))
            m = cur.fetchone()
            cur.execute("SELECT * FROM structural_change WHERE id = %s FOR UPDATE", (change_id,))
            c = cur.fetchone()
            if c["status"] != "pending":
                raise http_error(409, "not_pending",
                                 "El cambio ya no está pendiente"
                                 + (f": el objeto volvió a cambiar (alerta #{c['superseded_by_id']})."
                                    if c["superseded_by_id"] else "."),
                                 current_status=c["status"], superseded_by_id=c["superseded_by_id"])
            if c["row_version"] != body.expected_version \
                    or c["observed_fingerprint"].strip() != body.expected_observed_fingerprint.strip():
                raise http_error(409, "stale_version",
                                 "La alerta cambió desde que se mostró; vuelva a revisarla.",
                                 current_version=c["row_version"])
            key = (c["schema_name"], c["object_name"], c["object_type"])
            cur.execute(
                """SELECT fingerprint, present FROM inventory_object_state
                   WHERE monitored_database_id = %s AND schema_name = %s AND object_name = %s AND object_type = %s""",
                (m["id"], *key))
            st = cur.fetchone()
            current = st["fingerprint"].strip() if st and st["present"] else ABSENT
            if current != c["observed_fingerprint"].strip():
                raise http_error(409, "object_changed_again",
                                 "El objeto volvió a cambiar después de lo mostrado; no se acepta la versión nueva "
                                 "en silencio. Revise la alerta más reciente.")
            version = m["baseline_version"] + 1
            if c["change_kind"] == "object_removed":
                cur.execute(
                    """DELETE FROM inventory_baseline WHERE monitored_database_id = %s AND schema_name = %s
                       AND object_name = %s AND object_type = %s""", (m["id"], *key))
                action, fp, structure, dh = "removed", None, None, None
            else:
                cur.execute(
                    """INSERT INTO inventory_baseline (monitored_database_id, schema_name, object_name, object_type,
                           fingerprint, structure, definition_hash, definition_enc, version, origin, change_id,
                           approved_at, approved_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 'acknowledged_change', %s, NOW(), %s)
                       ON CONFLICT (monitored_database_id, schema_name, object_name, object_type) DO UPDATE SET
                           fingerprint = EXCLUDED.fingerprint, structure = EXCLUDED.structure,
                           definition_hash = EXCLUDED.definition_hash, definition_enc = EXCLUDED.definition_enc,
                           version = inventory_baseline.version + 1, origin = 'acknowledged_change',
                           change_id = EXCLUDED.change_id, approved_at = NOW(), approved_by = EXCLUDED.approved_by""",
                    (m["id"], *key, c["observed_fingerprint"].strip(), json.dumps(c["current_structure"], default=str),
                     c["current_definition_hash"], c["current_definition_enc"], change_id, actor))
                action, fp, structure, dh = ("acknowledged", c["observed_fingerprint"].strip(),
                                             json.dumps(c["current_structure"], default=str), c["current_definition_hash"])
            cur.execute(
                """INSERT INTO inventory_baseline_version (monitored_database_id, baseline_version, schema_name,
                       object_name, object_type, action, fingerprint, structure, definition_hash, change_id, actor,
                       comment)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (m["id"], version, *key, action, fp, structure, dh, change_id, actor, body.comment))
            cur.execute("""UPDATE inventory_baseline_version SET actor_user_id = %s
                           WHERE monitored_database_id = %s AND baseline_version = %s""",
                        (actor_user_id, m["id"], version))
            cur.execute("UPDATE monitored_database SET baseline_version = %s, updated_at = NOW() WHERE id = %s",
                        (version, m["id"]))
            cur.execute(
                """UPDATE structural_change SET status = 'acknowledged', status_changed_at = NOW(), attribution = %s,
                          ack_by = %s, ack_by_user_id = %s, ack_at = NOW(), ack_comment = %s, ticket_ref = %s,
                          row_version = row_version + 1, updated_at = NOW()
                   WHERE id = %s RETURNING row_version""",
                (body.attribution, actor, actor_user_id, (body.comment or "").strip() or None,
                 (body.ticket_ref or "").strip() or None, change_id))
            new_version = cur.fetchone()["row_version"]
            self._change_event(cur, change_id, "acknowledged", actor=actor,
                               message=("Modificó cliente" if body.attribution == "client" else "Modificó equipo Nexus")
                               + (f": {body.comment.strip()}" if body.comment and body.comment.strip() else ""),
                               data={"attribution": body.attribution, "ticket_ref": body.ticket_ref,
                                     "observed_fingerprint": c["observed_fingerprint"].strip(),
                                     "baseline_version": version}, actor_user_id=actor_user_id)
        return {"status": "ok", "change_id": change_id, "row_version": new_version, "baseline_version": version}

    def reclassify(self, change_id: int, body: ReclassifyBody, actor: str = "admin",
                   actor_user_id: Optional[int] = None) -> Dict[str, Any]:
        with self.tx() as cur:
            cur.execute("SELECT * FROM structural_change WHERE id = %s FOR UPDATE", (change_id,))
            c = cur.fetchone()
            if c is None:
                raise http_error(404, "not_found", "Cambio estructural inexistente.")
            if c["status"] != "acknowledged":
                raise http_error(409, "not_acknowledged", "Solo se reclasifican cambios ya dados por entendidos.")
            if c["row_version"] != body.expected_version:
                raise http_error(409, "stale_version", "El cambio se modificó desde que se mostró; recargue.",
                                 current_version=c["row_version"])
            new_ticket = (body.ticket_ref or "").strip() or c["ticket_ref"]
            if body.attribution == c["attribution"] and new_ticket == c["ticket_ref"]:
                raise http_error(422, "no_change", "La reclasificación no cambia el responsable ni el ticket.")
            previous = {"attribution": c["attribution"], "ticket_ref": c["ticket_ref"], "ack_comment": c["ack_comment"],
                        "ack_by": c["ack_by"], "ack_at": iso(c["ack_at"]),
                        "reclassified_by": c["reclassified_by"], "reclassified_at": iso(c["reclassified_at"])}
            cur.execute(
                """UPDATE structural_change SET attribution = %s, ticket_ref = %s, reclassified_at = NOW(),
                          reclassified_by = %s, reclassified_by_user_id = %s,
                          row_version = row_version + 1, updated_at = NOW()
                   WHERE id = %s RETURNING row_version""",
                (body.attribution, new_ticket, actor, actor_user_id, change_id))
            new_version = cur.fetchone()["row_version"]
            self._change_event(cur, change_id, "reclassified", actor=actor, message=body.reason.strip(),
                               data={"previous": previous,
                                     "new": {"attribution": body.attribution, "ticket_ref": new_ticket},
                                     "reason": body.reason.strip()}, actor_user_id=actor_user_id)
        return {"status": "ok", "change_id": change_id, "row_version": new_version}

    # ── Retención ───────────────────────────────────────────────────────────
    def maybe_housekeeping(self, cur: Any) -> None:
        now = time.time()
        if now - self._last_housekeeping < _HOUSEKEEPING_EVERY or self.s.snapshot_retention_days <= 0:
            return
        self._last_housekeeping = now
        with self.savepoint(cur, "retención de snapshots"):
            cur.execute(
                """DELETE FROM inventory_snapshot s
                   WHERE s.received_at < NOW() - make_interval(days => %s)
                     AND NOT EXISTS (SELECT 1 FROM monitored_database m
                                     WHERE m.last_snapshot_id = s.id OR m.last_verified_snapshot_id = s.id)""",
                (self.s.snapshot_retention_days,))

    # ── Vistas para el panel ────────────────────────────────────────────────
    def stale_seconds_sql(self) -> str:
        return (f"(md.scan_interval_seconds * {float(self.s.stale_factor)} + {int(self.s.lease_ttl_seconds)})")


# ─────────────────────────────────────────────────────────────────────────────
# Rutas del agente (se registran dentro del router /agent con su autenticación)
# ─────────────────────────────────────────────────────────────────────────────
def register_agent_inventory_routes(agent: APIRouter, authenticate: Callable[..., Any],
                                    engine: InventoryEngine) -> None:
    @agent.post("/inventory/lease")
    def inventory_lease(body: LeaseBody, ctx: Any = Depends(authenticate)) -> dict:
        with engine.tx() as cur:
            return engine.lease(cur, ctx, body)

    @agent.post("/inventory/snapshots")
    def inventory_snapshot(body: SnapshotBody, ctx: Any = Depends(authenticate)) -> dict:
        with engine.tx() as cur:
            return engine.ingest_snapshot(cur, ctx, body)


# ─────────────────────────────────────────────────────────────────────────────
# Administración
# ─────────────────────────────────────────────────────────────────────────────
def create_inventory_admin_router(*, engine: InventoryEngine, auth: Any) -> APIRouter:
    """
    Permisos: view (consultas, por alcance), inventory.configure, inventory.approve_baseline,
    structure.acknowledge, structure.reclassify, inventory.view_definitions. Una base monitoreada
    pertenece a su grupo y a los grupos vinculados (DWH compartido): se VE si alguno está en el
    alcance y se MODIFICA solo con el permiso en TODOS ellos (el efecto es compartido).
    """
    from panel_auth import AuthContext, group_of, groups_of_change, groups_of_mdb, mdb_scope_sql

    VIEW = auth.perm("view")
    CONFIGURE = auth.perm("inventory.configure")
    APPROVE = auth.perm("inventory.approve_baseline")
    ACK = auth.perm("structure.acknowledge")
    RECLASS = auth.perm("structure.reclassify")
    DEFS = auth.perm("inventory.view_definitions")

    router = APIRouter(prefix="/admin", tags=["inventory"])

    def check_mdb(cur: Any, ctx: AuthContext, mdb_id: int, perm: str = "view") -> List[Optional[int]]:
        gs = groups_of_mdb(cur, mdb_id)
        ctx.check(perm, gs, "Base monitoreada")
        return gs

    MDB_SELECT = f"""
        SELECT md.*, g.name AS group_name, c.name AS company_name,
               i.name AS lease_installation_name, i.group_id AS lease_installation_group_id,
               (md.lease_until IS NOT NULL AND md.lease_until > NOW()) AS lease_active,
               (md.last_attempt_at IS NOT NULL AND
                md.last_attempt_at < NOW() - make_interval(secs => {engine.stale_seconds_sql()})) AS stale,
               (SELECT COUNT(*) FROM structural_change sc
                 WHERE sc.monitored_database_id = md.id AND sc.status = 'pending') AS pending_changes,
               (SELECT COUNT(*) FROM inventory_baseline b WHERE b.monitored_database_id = md.id) AS baseline_objects,
               (SELECT COUNT(*) FROM inventory_object_state s
                 WHERE s.monitored_database_id = md.id AND s.present) AS observed_objects,
               (SELECT COALESCE(json_agg(json_build_object('group_id', l.group_id, 'group_name', lg.name,
                                                           'company_id', NULLIF(l.company_id, 0),
                                                           'company_name', lc.name) ORDER BY l.group_id, l.company_id),
                                '[]'::json)
                  FROM monitored_database_link l
                  LEFT JOIN client_group lg ON lg.id = l.group_id
                  LEFT JOIN company lc ON lc.id = NULLIF(l.company_id, 0)
                 WHERE l.monitored_database_id = md.id) AS links
        FROM monitored_database md
        LEFT JOIN client_group g ON g.id = md.group_id
        LEFT JOIN company c ON c.id = md.company_id
        LEFT JOIN installation i ON i.id = md.lease_installation_id
    """

    def effective_status(r: Dict[str, Any]) -> str:
        if r["duplicate_of_id"]:
            return "duplicate"
        if not r["enabled"]:
            return "disabled"
        if r["verification_status"] == "never":
            return "never"
        if r["verification_status"] == "unverifiable" or r.get("stale"):
            return "unverifiable"
        return r["verification_status"]

    def hide_owner(d: Dict[str, Any], ctx: AuthContext) -> None:
        """DWH compartido visible por un vínculo: el grupo "dueño" fuera del alcance no se nombra."""
        if d.get("group_id") is not None and not ctx.can("view", d["group_id"]):
            d["group_id"], d["group_name"], d["company_name"] = None, None, None
            d["owner_hidden"] = True
            # Nombres heredados que podrían incluir el grupo dueño (versiones previas a la fase 4).
            for k in ("display_name", "database_name"):
                if k in d:
                    d[k] = f"Base monitoreada #{d.get('monitored_database_id') or d.get('id')} (compartida)"
        # Instalación responsable de otro grupo: no se nombra.
        if "lease_installation_group_id" in d:
            lg = d.pop("lease_installation_group_id")
            if d.get("lease_installation_name") and not ctx.can("view", lg):
                d["lease_installation_name"] = "(instalación de otro grupo)"

    def allowed_actions(ctx: AuthContext, groups: List[Optional[int]]) -> Dict[str, bool]:
        """Qué puede hacer el usuario sobre el recurso (todas las acciones exigen TODOS sus grupos)."""
        gl = groups or [None]
        return {k: all(ctx.can(p, g) for g in gl) for k, p in (
            ("configure", "inventory.configure"), ("approve_baseline", "inventory.approve_baseline"),
            ("acknowledge", "structure.acknowledge"), ("reclassify", "structure.reclassify"),
            ("view_definitions", "inventory.view_definitions"))}

    def mdb_out(r: Dict[str, Any], ctx: Optional[AuthContext] = None) -> Dict[str, Any]:
        d = dict(r)
        if ctx is None:
            d.pop("lease_installation_group_id", None)
        if ctx is not None:
            groups = [g for g in [r.get("group_id")] + [lk.get("group_id") for lk in (r.get("links") or [])]
                      if g is not None]
            d["allowed_actions"] = allowed_actions(ctx, list(dict.fromkeys(groups)))
            # Vínculos con grupos fuera del alcance: no se revelan sus nombres.
            d["links"] = [lk for lk in (d.get("links") or []) if ctx.can("view", lk.get("group_id"))]
            hide_owner(d, ctx)
        d["identity_key"] = (d.get("identity_key") or "").strip()[:12]
        d["engine_identity"] = ((d.get("engine_identity") or "").strip()[:12]) or None
        d["engine_identity_weak"] = ((d.get("engine_identity_weak") or "").strip()[:12]) or None
        d["lease_installation_id"] = str(d["lease_installation_id"]) if d.get("lease_installation_id") else None
        d["effective_status"] = effective_status(r)
        for k in ("lease_acquired_at", "lease_until", "scan_requested_at", "last_attempt_at", "last_verified_at",
                  "baseline_approved_at", "created_at", "updated_at"):
            d[k] = iso(d.get(k))
        d["schema_include"] = list(d.get("schema_include") or [])
        d["schema_exclude"] = list(d.get("schema_exclude") or [])
        d["default_schema_exclude"] = list(engine.s.default_schema_exclude)
        return d

    def scope_filter(alias: str, group_id: Optional[int], company_id: Optional[int],
                     agency_id: Optional[int], cur: Any, ctx: AuthContext) -> Tuple[List[str], List[Any]]:
        """Filtro por grupo/empresa/agencia sobre monitored_database (alias) + alcance del usuario."""
        sql0, p0 = mdb_scope_sql(ctx, alias)
        where: List[str] = [sql0]
        params: List[Any] = list(p0)
        if agency_id:
            cur.execute("SELECT a.company_id, c.group_id FROM agency a JOIN company c ON c.id = a.company_id "
                        "WHERE a.id = %s", (agency_id,))
            a = cur.fetchone()
            if not a:
                return ["FALSE"], []  # noqa: E501
            company_id = company_id or a["company_id"]
            group_id = group_id or a["group_id"]
        if company_id:
            cur.execute("SELECT group_id FROM company WHERE id = %s", (company_id,))
            c = cur.fetchone()
            if not c:
                return ["FALSE"], []
            # Origen de esa empresa o DWH de su grupo (los datos de la empresa viven ahí).
            where.append(f"""(({alias}.kind = 'source' AND {alias}.company_id = %s) OR
                              ({alias}.kind = 'dwh' AND ({alias}.group_id = %s OR EXISTS (
                                 SELECT 1 FROM monitored_database_link l WHERE l.monitored_database_id = {alias}.id
                                   AND l.group_id = %s))))""")
            params += [company_id, c["group_id"], c["group_id"]]
        elif group_id:
            where.append(f"""({alias}.group_id = %s OR EXISTS (SELECT 1 FROM monitored_database_link l
                              WHERE l.monitored_database_id = {alias}.id AND l.group_id = %s))""")
            params += [group_id, group_id]
        return where, params

    # ── Resumen / contador ──────────────────────────────────────────────────
    @router.get("/inventory/summary")
    def inventory_summary(group_id: Optional[int] = Query(None), ctx: AuthContext = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            where, params = scope_filter("md", group_id, None, None, cur, ctx)
            cur.execute(MDB_SELECT + (" WHERE " + " AND ".join(where) if where else ""), params)
            rows = [mdb_out(r, ctx) for r in cur.fetchall()]
        by_status: Dict[str, int] = {}
        for r in rows:
            by_status[r["effective_status"]] = by_status.get(r["effective_status"], 0) + 1
        return {
            "databases": len(rows), "by_status": by_status,
            "pending_changes": sum(int(r["pending_changes"]) for r in rows
                                   if not r["duplicate_of_id"] and r["enabled"]),
            "awaiting_baseline": sum(1 for r in rows if r["state"] == "baseline_pending"
                                     and not r["duplicate_of_id"] and r["enabled"]),
            "unverifiable": by_status.get("unverifiable", 0),
            "permissions": PERMISSIONS,
        }

    @router.get("/structural-changes/badge")
    def changes_badge(ctx: AuthContext = Depends(VIEW)) -> dict:
        scope, sp = mdb_scope_sql(ctx, "md")
        with engine.tx() as cur:
            # Mismos criterios que /admin/inventory/summary (sin bases deshabilitadas ni duplicadas).
            cur.execute(f"""SELECT COUNT(*) AS n FROM structural_change sc
                            JOIN monitored_database md ON md.id = sc.monitored_database_id
                            WHERE sc.status = 'pending' AND md.enabled AND md.duplicate_of_id IS NULL AND {scope}""",
                        sp)
            n = cur.fetchone()["n"]
            cur.execute(f"""SELECT COUNT(*) AS n FROM monitored_database md
                            WHERE md.enabled AND md.duplicate_of_id IS NULL AND {scope}
                              AND (md.verification_status = 'unverifiable' OR (md.last_attempt_at IS NOT NULL AND
                                   md.last_attempt_at < NOW() - make_interval(secs => {engine.stale_seconds_sql()})))""",
                        sp)
            u = cur.fetchone()["n"]
            cur.execute(f"SELECT COUNT(*) AS n FROM monitored_database md WHERE md.state = 'baseline_pending' "
                        f"AND md.duplicate_of_id IS NULL AND md.enabled AND {scope}", sp)
            b = cur.fetchone()["n"]
        return {"pending_changes": n, "unverifiable_databases": u, "awaiting_baseline": b}

    # ── Bases monitoreadas ──────────────────────────────────────────────────
    @router.get("/monitored-databases")
    def list_mdb(group_id: Optional[int] = Query(None), company_id: Optional[int] = Query(None),
                 agency_id: Optional[int] = Query(None), kind: Optional[str] = Query(None),
                 ctx: AuthContext = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            where, params = scope_filter("md", group_id, company_id, agency_id, cur, ctx)
            if kind in ("dwh", "source"):
                where.append("md.kind = %s")
                params.append(kind)
            cur.execute(MDB_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY md.id", params)
            return {"items": [mdb_out(r, ctx) for r in cur.fetchall()]}

    def get_mdb_or_404(cur: Any, mdb_id: int) -> Dict[str, Any]:
        cur.execute(MDB_SELECT + " WHERE md.id = %s", (mdb_id,))
        r = cur.fetchone()
        if not r:
            raise http_error(404, "not_found", "Base monitoreada inexistente.")
        return r

    @router.get("/monitored-databases/{mdb_id}")
    def get_mdb(mdb_id: int, ctx: AuthContext = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id)
            raw = get_mdb_or_404(cur, mdb_id)
            m = mdb_out(raw, ctx)
            cur.execute("""SELECT s.id, s.installation_id, i.name AS installation_name, i.group_id AS _inst_group,
                                  s.captured_at, s.received_at,
                                  s.status, s.reason_code, s.object_count, s.schemas_verified, s.schemas_unverifiable,
                                  s.agent_version, s.server_version, s.processing
                           FROM inventory_snapshot s LEFT JOIN installation i ON i.id = s.installation_id
                           WHERE s.monitored_database_id = %s ORDER BY s.id DESC LIMIT 20""", (mdb_id,))
            snaps = []
            for s in cur.fetchall():
                s = dict(s)
                if not ctx.can("view", s.pop("_inst_group", None)) and s.get("installation_name"):
                    s["installation_name"], s["installation_id"] = "(instalación de otro grupo)", None
                s["installation_id"] = str(s["installation_id"]) if s["installation_id"] else None
                s["captured_at"], s["received_at"] = iso(s["captured_at"]), iso(s["received_at"])
                snaps.append(s)
            cur.execute("""SELECT id, event_type, actor, message, data, created_at FROM monitored_database_event
                           WHERE monitored_database_id = %s ORDER BY id DESC LIMIT 50""", (mdb_id,))
            events = [dict(e, created_at=iso(e["created_at"])) for e in cur.fetchall()]
            # ¿La configuración vigente sigue apuntando a esta base?
            current = False
            if raw["kind"] == "dwh" and raw["group_id"]:
                cur.execute("SELECT warehouse_host, warehouse_port, warehouse_database FROM client_group WHERE id = %s",
                            (raw["group_id"],))
                g = cur.fetchone()
                current = bool(g) and engine.dwh_identity(g) == get_mdb_or_404(cur, mdb_id)["identity_key"].strip()
            elif raw["kind"] == "source" and raw["company_id"]:
                cur.execute("SELECT * FROM company WHERE id = %s", (raw["company_id"],))
                c = cur.fetchone()
                current = bool(c) and engine.source_identity(c) == get_mdb_or_404(cur, mdb_id)["identity_key"].strip()
        m.update(snapshots=snaps, events=events, config_current=current)
        return m

    @router.post("/monitored-databases", status_code=201)
    def create_mdb(body: MonitoredCreate, ctx: AuthContext = Depends(CONFIGURE)) -> dict:
        with engine.tx() as cur:
            if body.kind == "dwh":
                if not body.group_id:
                    raise http_error(422, "group_required", "Indique group_id para el DWH.")
                gid_chk = group_of(cur, "group", body.group_id, "Grupo")
                ctx.check("inventory.configure", gid_chk, "Grupo")
                cur.execute("SELECT id, name, warehouse_host, warehouse_port, warehouse_database FROM client_group "
                            "WHERE id = %s", (body.group_id,))
                g = cur.fetchone()
                if not g:
                    raise http_error(404, "not_found", "Grupo inexistente.")
                key = engine.dwh_identity(g)
                if not key:
                    raise http_error(422, "no_warehouse", "El grupo no tiene DWH configurado.")
                engine_name, group_id, company_id = "postgresql", g["id"], None
                # Nombre neutro (sin el grupo): un DWH puede quedar compartido por varios grupos.
                name = body.display_name or f"DWH {engine.decrypt(g['warehouse_database'] or '')} · {key[:6]}"
                enabled = True if body.enabled is None else body.enabled
            else:
                if not body.company_id:
                    raise http_error(422, "company_required", "Indique company_id para monitorear su origen (DMS).")
                gid_chk = group_of(cur, "company", body.company_id, "Empresa")
                ctx.check("inventory.configure", gid_chk, "Empresa")
                cur.execute("SELECT * FROM company WHERE id = %s", (body.company_id,))
                c = cur.fetchone()
                if not c:
                    raise http_error(404, "not_found", "Empresa inexistente.")
                key = engine.source_identity(c)
                engine_name = (c["source_type"] or "sqlserver").lower()
                group_id, company_id = c["group_id"], c["id"]
                name = body.display_name or f"Origen {c['name']}"
                # El monitoreo del origen es OPCIONAL: deshabilitado salvo que se pida explícitamente.
                enabled = bool(body.enabled)
            cur.execute("SELECT id FROM monitored_database WHERE identity_key = %s", (key,))
            ex = cur.fetchone()
            if ex:
                raise http_error(409, "already_monitored", "Esa base ya está registrada.", id=ex["id"])
            cur.execute(
                """INSERT INTO monitored_database (kind, engine, identity_key, display_name, group_id, company_id,
                       enabled, scan_interval_seconds, schema_include, schema_exclude, view_definitions_enabled,
                       created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (body.kind, engine_name, key, name[:255], group_id, company_id, enabled,
                 body.scan_interval_seconds or engine.s.default_interval_seconds,
                 _clean_patterns(body.schema_include) or [], _clean_patterns(body.schema_exclude) or [],
                 body.view_definitions_enabled, ctx.actor))
            new_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO monitored_database_link (monitored_database_id, group_id, company_id) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (new_id, group_id, company_id or 0))
            engine._mdb_event(cur, new_id, "created", actor=ctx.actor, actor_user_id=ctx.user_id,
                              data={"kind": body.kind, "enabled": enabled, "engine": engine_name})
            out = mdb_out(get_mdb_or_404(cur, new_id), ctx)
        if body.kind == "source" and engine_name not in SUPPORTED_ENGINES:
            out["warning"] = (f"El agente aún no inventaría orígenes {engine_name}: se reportará "
                              "'No se pudo verificar la estructura' (ENGINE_UNSUPPORTED).")
        return out

    @router.put("/monitored-databases/{mdb_id}")
    def update_mdb(mdb_id: int, body: MonitoredUpdate, ctx: AuthContext = Depends(CONFIGURE)) -> dict:
        changes = body.model_dump(exclude_unset=True)
        ACTOR = ctx.actor
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id, "inventory.configure")
            cur.execute("SELECT * FROM monitored_database WHERE id = %s FOR UPDATE", (mdb_id,))
            m = cur.fetchone()
            if not m:
                raise http_error(404, "not_found", "Base monitoreada inexistente.")
            sets, params, audit = [], [], {}
            for k, v in changes.items():
                if k in ("schema_include", "schema_exclude"):
                    v = _clean_patterns(v) or []
                if v is None:
                    continue
                before = list(m[k]) if isinstance(m[k], list) else m[k]
                if before != v:
                    audit[k] = {"before": before, "after": v}
                sets.append(f"{k} = %s")
                params.append(v)
            if sets:
                cur.execute(f"UPDATE monitored_database SET {', '.join(sets)}, updated_at = NOW() WHERE id = %s",
                            (*params, mdb_id))
            if audit:
                engine._mdb_event(cur, mdb_id, "config_changed", actor=ACTOR, data=audit, actor_user_id=ctx.user_id)
            if "view_definitions_enabled" in audit and not changes.get("view_definitions_enabled"):
                # Al deshabilitarlo se borra el SQL cifrado ya guardado (quedan solo las huellas).
                n = 0
                for table, cols in (("inventory_object_state", ("definition_enc",)),
                                    ("inventory_baseline", ("definition_enc",)),
                                    ("structural_change", ("previous_definition_enc", "current_definition_enc"))):
                    cond = " OR ".join(f"{c} IS NOT NULL" for c in cols)
                    cur.execute(f"UPDATE {table} SET {', '.join(f'{c} = NULL' for c in cols)} "
                                f"WHERE monitored_database_id = %s AND ({cond})", (mdb_id,))
                    n += cur.rowcount
                engine._mdb_event(cur, mdb_id, "view_definitions_purged", actor=ACTOR,
                                  message="Se borró el SQL cifrado de vistas guardado.", data={"rows": n},
                                  actor_user_id=ctx.user_id)
            if "schema_include" in audit or "schema_exclude" in audit:
                cur.execute("SELECT * FROM monitored_database WHERE id = %s", (mdb_id,))
                engine.close_out_of_scope(cur, cur.fetchone(), actor=ACTOR)
            return mdb_out(get_mdb_or_404(cur, mdb_id), ctx)

    @router.post("/monitored-databases/{mdb_id}/scan")
    def request_scan(mdb_id: int, ctx: AuthContext = Depends(CONFIGURE)) -> dict:
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id, "inventory.configure")
            cur.execute("UPDATE monitored_database SET scan_requested_at = NOW() WHERE id = %s RETURNING id", (mdb_id,))
            if not cur.fetchone():
                raise http_error(404, "not_found", "Base monitoreada inexistente.")
            engine._mdb_event(cur, mdb_id, "scan_requested", actor=ctx.actor, actor_user_id=ctx.user_id)
        return {"status": "ok"}

    @router.post("/monitored-databases/{mdb_id}/resolve-duplicate")
    def resolve_duplicate(mdb_id: int, body: ResolveDuplicateBody, ctx: AuthContext = Depends(CONFIGURE)) -> dict:
        """
        merge: el registro ORIGINAL (línea base + historial) adopta la configuración de este
               duplicado (p. ej. se cambió el nombre del host del DWH) y el duplicado desaparece.
        undo:  la detección fue errónea: se quita la marca y no se vuelve a marcar sola.
        """
        ACTOR = ctx.actor
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id, "inventory.configure")
            cur.execute("SELECT * FROM monitored_database WHERE id = %s FOR UPDATE", (mdb_id,))
            m = cur.fetchone()
            if not m:
                raise http_error(404, "not_found", "Base monitoreada inexistente.")
            if m["duplicate_of_id"]:
                # La fusión/deshacer afecta también a la original: permiso sobre sus grupos.
                check_mdb(cur, ctx, m["duplicate_of_id"], "inventory.configure")
            if not m["duplicate_of_id"]:
                raise http_error(409, "not_duplicate", "La base no está marcada como duplicada.")
            if body.action == "undo":
                cur.execute("""UPDATE monitored_database SET duplicate_of_id = NULL, allow_engine_duplicate = TRUE,
                                      engine_identity = NULL, engine_identity_strength = NULL,
                                      engine_identity_weak = NULL, updated_at = NOW() WHERE id = %s""", (mdb_id,))
                engine._mdb_event(cur, mdb_id, "duplicate_undone", actor=ACTOR, message=body.reason.strip(),
                                  data={"previous_duplicate_of_id": m["duplicate_of_id"]})
                return {"status": "ok", "id": mdb_id}
            cur.execute("SELECT * FROM monitored_database WHERE id = %s", (m["duplicate_of_id"],))
            orig = cur.fetchone()
            if not orig or not engine.same_owner(cur, orig, m):
                # Nunca se mueve línea base ni alertas entre grupos (o empresas) distintos.
                raise http_error(409, "cross_group_merge",
                                 "La base original pertenece a otro grupo/empresa: no se fusiona. "
                                 "Si no son la misma base, use 'deshacer duplicado'.")
            a_ = engine._engine_pair(orig["engine_identity_strength"], orig["engine_identity"],
                                     orig["engine_identity_weak"])
            b_ = engine._engine_pair(m["engine_identity_strength"], m["engine_identity"], m["engine_identity_weak"])
            if not (a_[0] and b_[0] and a_[0] == b_[0]):
                raise http_error(409, "weak_identity",
                                 "Solo se fusiona con identidad FUERTE coincidente (system_identifier).")
            if orig and engine.identity_is_current(cur, orig):
                raise http_error(409, "original_still_current",
                                 "La configuración original sigue vigente: son dos configuraciones de la misma base "
                                 "y se inventaría una sola vez. Use 'deshacer' solo si no es la misma base.")
            new = engine.merge_into(cur, m, m["duplicate_of_id"], actor=ACTOR, reason=body.reason.strip())
            return {"status": "ok", "id": new["id"], "merged_from": mdb_id}

    @router.post("/monitored-databases/{mdb_id}/release-lease")
    def release_lease(mdb_id: int, ctx: AuthContext = Depends(CONFIGURE)) -> dict:
        ACTOR = ctx.actor
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id, "inventory.configure")
            cur.execute("""UPDATE monitored_database SET lease_installation_id = NULL, lease_until = NULL
                           WHERE id = %s RETURNING id""", (mdb_id,))
            if not cur.fetchone():
                raise http_error(404, "not_found", "Base monitoreada inexistente.")
            engine._mdb_event(cur, mdb_id, "lease_released", actor=ACTOR, data={"reason": "admin"})
        return {"status": "ok"}

    @router.get("/monitored-databases/{mdb_id}/baseline")
    def get_baseline(mdb_id: int, view: str = Query("auto"), schema: Optional[str] = Query(None),
                     search: Optional[str] = Query(None, max_length=128), limit: int = Query(1000, ge=1, le=20000),
                     ctx: AuthContext = Depends(VIEW)) -> dict:
        """
        view=approved → línea base aprobada; view=proposal → último inventario observado (propuesta);
        auto → propuesta si está pendiente de aprobación, si no la aprobada.
        """
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id)
            m = get_mdb_or_404(cur, mdb_id)
            if view == "auto":
                view = "proposal" if m["state"] != "monitoring" else "approved"
            table = "inventory_baseline" if view == "approved" else "inventory_object_state"
            where = ["monitored_database_id = %s"]
            params: List[Any] = [mdb_id]
            if table == "inventory_object_state":
                where.append("present")
            if schema:
                where.append("schema_name = %s")
                params.append(schema)
            if search:
                where.append("object_name ILIKE %s")
                params.append(f"%{search}%")
            cur.execute(f"""SELECT schema_name, object_name, object_type, fingerprint, structure, definition_hash
                            FROM {table} WHERE {' AND '.join(where)}
                            ORDER BY schema_name, object_name, object_type LIMIT %s""", (*params, limit))
            rows = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {' AND '.join(where)}", params)
            total = cur.fetchone()["n"]
            # Coincidencias con el catálogo Nexus (EVIDENCIA, no atribución).
            catalog: Set[str] = set()
            groups = engine._linked_groups(cur, m)
            if groups:
                cur.execute("""SELECT oc.destination_table FROM object_catalog oc JOIN company c ON c.id = oc.company_id
                               WHERE c.group_id = ANY(%s)""", (groups,))
                for r in cur.fetchall():
                    dt = (r["destination_table"] or "").strip().lower()
                    if dt:
                        catalog.add(dt if "." in dt else "public." + dt)
            snap = None
            if m["last_verified_snapshot_id"]:
                cur.execute("""SELECT id, status, received_at, captured_at, schemas_verified, schemas_unverifiable,
                                      object_count FROM inventory_snapshot WHERE id = %s""",
                            (m["last_verified_snapshot_id"],))
                snap = cur.fetchone()
                if snap:
                    snap = dict(snap, received_at=iso(snap["received_at"]), captured_at=iso(snap["captured_at"]))
        items = []
        for r in rows:
            st = r["structure"] or {}
            items.append({
                "schema_name": r["schema_name"], "name": r["object_name"], "type": r["object_type"],
                "fingerprint": r["fingerprint"].strip(), "columns": len(st.get("columns") or {}),
                "constraints": len(st.get("constraints") or {}), "indexes": len(st.get("indexes") or {}),
                "has_definition_hash": bool(r["definition_hash"]), "structure": st,
                "nexus_catalog_match": f"{r['schema_name']}.{r['object_name']}".lower() in catalog,
            })
        return {"view": view, "state": m["state"], "total": total, "items": items, "snapshot": snap,
                "baseline_version": m["baseline_version"]}

    @router.post("/monitored-databases/{mdb_id}/baseline/approve")
    def approve(mdb_id: int, body: ApproveBody, ctx: AuthContext = Depends(APPROVE)) -> dict:
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id, "inventory.approve_baseline")
        return engine.approve_baseline(mdb_id, body, actor=ctx.actor, actor_user_id=ctx.user_id)

    @router.post("/monitored-databases/{mdb_id}/baseline/reset")
    def reset(mdb_id: int, body: ReasonBody, ctx: AuthContext = Depends(APPROVE)) -> dict:
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id, "inventory.approve_baseline")
        return engine.reset_baseline(mdb_id, body.reason.strip(), actor=ctx.actor, actor_user_id=ctx.user_id)

    @router.get("/monitored-databases/{mdb_id}/baseline/history")
    def baseline_history(mdb_id: int, limit: int = Query(200, ge=1, le=2000), ctx: AuthContext = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            check_mdb(cur, ctx, mdb_id)
            cur.execute("""SELECT id, baseline_version, schema_name, object_name, object_type, action, fingerprint,
                                  change_id, actor, comment, created_at
                           FROM inventory_baseline_version WHERE monitored_database_id = %s
                           ORDER BY id DESC LIMIT %s""", (mdb_id, limit))
            return {"items": [dict(r, created_at=iso(r["created_at"]),
                                   fingerprint=(r["fingerprint"] or "").strip() or None) for r in cur.fetchall()]}

    # ── Cambios estructurales ───────────────────────────────────────────────
    CHANGE_SELECT = """
        SELECT sc.id, sc.monitored_database_id, sc.schema_name, sc.object_name, sc.object_type, sc.change_kind,
               sc.change_types, sc.baseline_fingerprint, sc.observed_fingerprint, sc.first_detected_at,
               sc.last_observed_at, sc.observation_count, sc.status, sc.status_changed_at, sc.supersedes_id,
               sc.superseded_by_id, sc.attribution, sc.ack_by, sc.ack_at, sc.ack_comment, sc.ticket_ref,
               sc.reclassified_at, sc.reclassified_by, sc.row_version, sc.evidence, sc.baseline_version,
               md.kind AS database_kind, md.display_name AS database_name, md.group_id, g.name AS group_name,
               md.company_id, c.name AS company_name,
               ARRAY(SELECT DISTINCT l.group_id FROM monitored_database_link l
                      WHERE l.monitored_database_id = md.id) AS _linked_groups
        FROM structural_change sc
        JOIN monitored_database md ON md.id = sc.monitored_database_id
        LEFT JOIN client_group g ON g.id = md.group_id
        LEFT JOIN company c ON c.id = md.company_id
    """

    def change_out(r: Dict[str, Any], ctx: Optional[AuthContext] = None) -> Dict[str, Any]:
        d = dict(r)
        linked = d.pop("_linked_groups", None) or []
        if ctx is not None:
            groups = list(dict.fromkeys([g for g in [r.get("group_id"), *linked] if g is not None]))
            d["allowed_actions"] = allowed_actions(ctx, groups)
            hide_owner(d, ctx)
        for k in ("first_detected_at", "last_observed_at", "status_changed_at", "ack_at", "reclassified_at"):
            d[k] = iso(d.get(k))
        d["baseline_fingerprint"] = (d.get("baseline_fingerprint") or "").strip() or None
        d["observed_fingerprint"] = (d.get("observed_fingerprint") or "").strip()
        d["change_types"] = list(d.get("change_types") or [])
        d["evidence_count"] = len(d.get("evidence") or [])
        return d

    @router.get("/structural-changes")
    def list_changes(
        view: Optional[str] = Query(None), status: Optional[str] = Query(None),
        group_id: Optional[int] = Query(None), company_id: Optional[int] = Query(None),
        agency_id: Optional[int] = Query(None), monitored_database_id: Optional[int] = Query(None),
        schema: Optional[str] = Query(None, max_length=128), object: Optional[str] = Query(None, max_length=128),
        change_type: Optional[str] = Query(None, max_length=40), attribution: Optional[str] = Query(None),
        since: Optional[datetime] = Query(None), until: Optional[datetime] = Query(None),
        limit: int = Query(300, ge=1, le=2000), ctx: AuthContext = Depends(VIEW),
    ) -> dict:
        with engine.tx() as cur:
            where, params = scope_filter("md", group_id, company_id, agency_id, cur, ctx)
            if view == "pending":
                where.append("sc.status = 'pending'")
            elif view == "history":
                where.append("sc.status <> 'pending'")
            if status in CHANGE_STATUSES:
                where.append("sc.status = %s")
                params.append(status)
            if monitored_database_id:
                where.append("sc.monitored_database_id = %s")
                params.append(monitored_database_id)
            if schema:
                where.append("sc.schema_name = %s")
                params.append(schema)
            if object:
                where.append("sc.object_name ILIKE %s")
                params.append(f"%{object}%")
            if change_type:
                where.append("(sc.change_kind = %s OR %s = ANY(sc.change_types))")
                params += [change_type, change_type]
            if attribution in ("client", "nexus"):
                where.append("sc.attribution = %s")
                params.append(attribution)
            elif attribution == "none":
                where.append("sc.attribution IS NULL")
            if since:
                where.append("sc.first_detected_at >= %s")
                params.append(since)
            if until:
                where.append("sc.first_detected_at <= %s")
                params.append(until)
            sql = CHANGE_SELECT + (" WHERE " + " AND ".join(where) if where else "") + \
                " ORDER BY sc.first_detected_at DESC, sc.id DESC LIMIT %s"
            cur.execute(sql, (*params, limit))
            return {"items": [change_out(r, ctx) for r in cur.fetchall()], "labels": CHANGE_TYPE_LABELS}

    @router.get("/structural-changes/{change_id}")
    def get_change(change_id: int, ctx: AuthContext = Depends(VIEW)) -> dict:
        with engine.tx() as cur:
            _, change_groups = groups_of_change(cur, change_id)
            ctx.check("view", change_groups, "Cambio estructural")
            cur.execute(CHANGE_SELECT + " WHERE sc.id = %s", (change_id,))
            r = cur.fetchone()
            if not r:
                raise http_error(404, "not_found", "Cambio estructural inexistente.")
            d = change_out(r, ctx)
            cur.execute("""SELECT diffs, previous_structure, current_structure, previous_definition_hash,
                                  current_definition_hash, (previous_definition_enc IS NOT NULL) AS has_prev_def,
                                  (current_definition_enc IS NOT NULL) AS has_cur_def
                           FROM structural_change WHERE id = %s""", (change_id,))
            x = cur.fetchone()
            d.update(diffs=x["diffs"], previous_structure=x["previous_structure"],
                     current_structure=x["current_structure"],
                     previous_definition_hash=(x["previous_definition_hash"] or "").strip() or None,
                     current_definition_hash=(x["current_definition_hash"] or "").strip() or None,
                     definitions_stored=bool(x["has_prev_def"] or x["has_cur_def"]),
                     definitions_viewable=engine.s.expose_view_definitions
                     and all(ctx.can("inventory.view_definitions", g) for g in change_groups))
            cur.execute("""SELECT id, event_type, actor, message, data, created_at FROM structural_change_event
                           WHERE change_id = %s ORDER BY id""", (change_id,))
            d["events"] = [dict(e, created_at=iso(e["created_at"])) for e in cur.fetchall()]
            # Historial del mismo objeto (otras alertas).
            cur.execute("""SELECT id, change_kind, status, first_detected_at, attribution, ack_at
                           FROM structural_change WHERE monitored_database_id = %s AND schema_name = %s
                             AND object_name = %s AND object_type = %s AND id <> %s ORDER BY id DESC LIMIT 20""",
                        (r["monitored_database_id"], r["schema_name"], r["object_name"], r["object_type"], change_id))
            d["object_history"] = [dict(h, first_detected_at=iso(h["first_detected_at"]), ack_at=iso(h["ack_at"]))
                                   for h in cur.fetchall()]
            cur.execute("SELECT state, verification_status, last_verified_at FROM monitored_database WHERE id = %s",
                        (r["monitored_database_id"],))
            s = cur.fetchone()
            d["database_state"] = {"state": s["state"], "verification_status": s["verification_status"],
                                   "last_verified_at": iso(s["last_verified_at"])}
        d["labels"] = CHANGE_TYPE_LABELS
        return d

    @router.get("/structural-changes/{change_id}/definitions")
    def get_definitions(change_id: int, ctx: AuthContext = Depends(DEFS)) -> JSONResponse:
        # Doble llave: el interruptor global [inventory] expose_view_definitions (por defecto false)
        # Y el permiso inventory.view_definitions sobre los grupos de la base.
        if not engine.s.expose_view_definitions:
            raise http_error(403, "permission_required",
                             "Ver el SQL de vistas requiere el permiso inventory.view_definitions "
                             "([inventory] expose_view_definitions = true).",
                             permission="inventory.view_definitions")
        with engine.tx() as cur:
            _, change_groups = groups_of_change(cur, change_id)
            ctx.check("inventory.view_definitions", change_groups, "Cambio estructural")
            cur.execute("""SELECT previous_definition_enc, current_definition_enc FROM structural_change
                           WHERE id = %s""", (change_id,))
            r = cur.fetchone()
            if not r:
                raise http_error(404, "not_found", "Cambio estructural inexistente.")
            engine._change_event(cur, change_id, "definitions_viewed", actor=ctx.actor, actor_user_id=ctx.user_id,
                                 message="Se consultó el SQL de la definición (acceso sensible).")
        return JSONResponse(
            {"previous": engine.decrypt_definition(r["previous_definition_enc"]),
             "current": engine.decrypt_definition(r["current_definition_enc"])},
            headers={"Cache-Control": "no-store"})

    @router.post("/structural-changes/{change_id}/acknowledge")
    def acknowledge(change_id: int, body: AcknowledgeBody, ctx: AuthContext = Depends(ACK)) -> dict:
        with engine.tx() as cur:
            _, gs = groups_of_change(cur, change_id)
            ctx.check("structure.acknowledge", gs, "Cambio estructural")
        return engine.acknowledge(change_id, body, actor=ctx.actor, actor_user_id=ctx.user_id)

    @router.post("/structural-changes/{change_id}/reclassify")
    def reclassify(change_id: int, body: ReclassifyBody, ctx: AuthContext = Depends(RECLASS)) -> dict:
        with engine.tx() as cur:
            _, gs = groups_of_change(cur, change_id)
            ctx.check("structure.reclassify", gs, "Cambio estructural")
        return engine.reclassify(change_id, body, actor=ctx.actor, actor_user_id=ctx.user_id)

    return router
