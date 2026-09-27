"""
admin_postgres.py — API de administración (PostgreSQL)
──────────────────────────────────────────────────────
CRUD de la BD de configuración para el panel web (dwh_front):
grupos, companies (razones sociales), agencias, catálogo de objetos y tareas.

Se monta desde main_postgres.py con:

    app.include_router(create_admin_router(...))

Autenticación: header ``x-admin-token`` comparado (tiempo constante) contra
``[admin] token`` de config.ini o la variable ``NEXUS_ADMIN_TOKEN``.
Si no hay token configurado, todos los endpoints /admin/* responden 503.

Reglas de seguridad:
  * Campos sensibles (source_host/database/username/password/dsn y
    warehouse_host/database/username/password) se guardan cifrados con Fernet
    (prefijo ENC:) si el backend tiene config_secret_key; si no, en texto plano.
  * Las contraseñas NUNCA se devuelven: solo ``has_password``.
    Contraseña vacía u omitida en una actualización = se conserva la actual.
  * Los tokens se generan en el servidor con secrets.token_urlsafe(32).
  * Solo SQL parametrizado; los nombres de columnas salen de listas blancas.
"""

import hmac
import re
import secrets
from contextlib import contextmanager
from typing import Annotated, Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple

import psycopg2
import psycopg2.errors
import psycopg2.extras
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

SOURCE_TYPES = ("sqlserver", "mysql", "postgresql", "pervasive", "firebird")
SourceType = Literal["sqlserver", "mysql", "postgresql", "pervasive", "firebird"]

# Campos sensibles por tabla (se cifran al escribir)
GROUP_SECRET_FIELDS = ("warehouse_host", "warehouse_database", "warehouse_username", "warehouse_password")
COMPANY_SECRET_FIELDS = ("source_host", "source_database", "source_username", "source_password", "source_dsn")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

# Mensajes amigables para violaciones de constraints conocidas
_CONSTRAINT_MESSAGES = {
    "client_group_name_key": "Ya existe un grupo con ese nombre.",
    "company_group_id_name_key": "Ya existe una empresa con ese nombre en el grupo.",
    "company_company_token_key": "El token de empresa ya está en uso.",
    "agency_company_id_name_key": "Ya existe una agencia con ese nombre en la empresa.",
    "object_catalog_company_id_name_key": "Ya existe un objeto con ese nombre en la empresa.",
    "agency_task_agency_id_object_catalog_id_key": "Esa agencia ya tiene una tarea para ese objeto.",
    "uq_client_group_group_token": "El token de grupo ya está en uso.",
    "client_group_group_token_key": "El token de grupo ya está en uso.",
    "uq_agency_agency_token": "El token de agencia ya está en uso.",
    "agency_agency_token_key": "El token de agencia ya está en uso.",
}


# ─────────────────────────────────────────────────────────────────────────────
# Modelos de entrada
# ─────────────────────────────────────────────────────────────────────────────
def _strip_name(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("no puede estar vacío")
    return v


def _norm_upsert_keys(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    keys = [k.strip() for k in v.replace("\n", ",").split(",") if k.strip()]
    return ",".join(keys) or None


def _check_ident(v: str) -> str:
    v = v.strip()
    if not _IDENT_RE.match(v):
        raise ValueError("debe ser un identificador SQL válido (letras, números, _ ; opcional esquema.tabla)")
    return v


def _empty_to_none(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    return v if v.strip() else None


def _not_blank(v: str) -> str:
    if not v.strip():
        raise ValueError("no puede estar vacío")
    return v


NameStr = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(_strip_name)]
IdentStr = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(_check_ident)]
SqlStr = Annotated[str, Field(min_length=1), AfterValidator(_not_blank)]
OptText = Annotated[Optional[str], AfterValidator(_empty_to_none)]
OptName = Annotated[Optional[str], Field(max_length=255), AfterValidator(_empty_to_none)]
UpsertKeys = Annotated[Optional[str], AfterValidator(_norm_upsert_keys)]
Port = Annotated[int, Field(ge=1, le=65535)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupCreate(_Base):
    name: NameStr
    warehouse_host: str = Field("", max_length=255)
    warehouse_port: Port = 5432
    warehouse_database: str = Field("", max_length=255)
    warehouse_username: str = Field("", max_length=255)
    warehouse_password: Optional[str] = Field(None, max_length=512)
    is_enabled: bool = True


class GroupUpdate(_Base):
    name: Optional[NameStr] = None
    warehouse_host: Optional[str] = Field(None, max_length=255)
    warehouse_port: Optional[Port] = None
    warehouse_database: Optional[str] = Field(None, max_length=255)
    warehouse_username: Optional[str] = Field(None, max_length=255)
    warehouse_password: Optional[str] = Field(None, max_length=512)
    clear_password: bool = False
    is_enabled: Optional[bool] = None


class CompanyCreate(_Base):
    group_id: int = Field(..., ge=1)
    name: NameStr
    source_type: SourceType = "sqlserver"
    source_host: str = Field("", max_length=255)
    source_port: Port = 1433
    source_database: str = Field("", max_length=512)
    source_username: str = Field("", max_length=255)
    source_password: Optional[str] = Field(None, max_length=512)
    source_dsn: str = Field("", max_length=255)
    verbose_logging: bool = False
    refresh_seconds: int = Field(60, ge=5, le=86400)
    is_enabled: bool = True


class CompanyUpdate(_Base):
    group_id: Optional[int] = Field(None, ge=1)
    name: Optional[NameStr] = None
    source_type: Optional[SourceType] = None
    source_host: Optional[str] = Field(None, max_length=255)
    source_port: Optional[Port] = None
    source_database: Optional[str] = Field(None, max_length=512)
    source_username: Optional[str] = Field(None, max_length=255)
    source_password: Optional[str] = Field(None, max_length=512)
    source_dsn: Optional[str] = Field(None, max_length=255)
    clear_password: bool = False
    verbose_logging: Optional[bool] = None
    refresh_seconds: Optional[int] = Field(None, ge=5, le=86400)
    is_enabled: Optional[bool] = None


class AgencyCreate(_Base):
    company_id: int = Field(..., ge=1)
    name: NameStr
    is_enabled: bool = True
    generate_token: bool = True


class AgencyUpdate(_Base):
    company_id: Optional[int] = Field(None, ge=1)
    name: Optional[NameStr] = None
    is_enabled: Optional[bool] = None


class ObjectCreate(_Base):
    company_id: int = Field(..., ge=1)
    name: NameStr
    description: OptText = None
    destination_table: IdentStr
    create_table_sql: OptText = None
    upsert_keys: UpsertKeys = None
    constraint_name: OptName = None
    create_constraint_sql: OptText = None
    static_columns: OptText = None
    is_enabled: bool = True


class ObjectUpdate(_Base):
    company_id: Optional[int] = Field(None, ge=1)
    name: Optional[NameStr] = None
    description: OptText = None
    destination_table: Optional[IdentStr] = None
    create_table_sql: OptText = None
    upsert_keys: UpsertKeys = None
    constraint_name: OptName = None
    create_constraint_sql: OptText = None
    static_columns: OptText = None
    is_enabled: Optional[bool] = None


class TaskCreate(_Base):
    agency_id: int = Field(..., ge=1)
    object_catalog_id: int = Field(..., ge=1)
    extract_sql: SqlStr
    schedule_seconds: int = Field(3600, ge=10, le=31_536_000)
    is_active: bool = True
    run_on_company_token: bool = True
    # Salud (opcionales; NULL = valores de [health] / historial de ejecuciones)
    expected_duration_seconds: Optional[int] = Field(None, ge=1, le=31_536_000)
    delay_tolerance_seconds: Optional[int] = Field(None, ge=0, le=31_536_000)


class TaskUpdate(_Base):
    agency_id: Optional[int] = Field(None, ge=1)
    object_catalog_id: Optional[int] = Field(None, ge=1)
    extract_sql: Optional[SqlStr] = None
    schedule_seconds: Optional[int] = Field(None, ge=10, le=31_536_000)
    is_active: Optional[bool] = None
    run_on_company_token: Optional[bool] = None
    # null explícito = volver al valor automático
    expected_duration_seconds: Optional[int] = Field(None, ge=1, le=31_536_000)
    delay_tolerance_seconds: Optional[int] = Field(None, ge=0, le=31_536_000)


# Campos de tarea que admiten volver a NULL con un null explícito en el PUT.
TASK_NULLABLE_FIELDS = ("expected_duration_seconds", "delay_tolerance_seconds")


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────
def create_admin_router(
    *,
    get_connection: Callable[[], Any],
    get_secret_cipher: Callable[[], Any],
    decrypt_config_secret: Callable[[Optional[str]], str],
    admin_token: str,
    group_token_column_exists: Callable[[], bool],
    agency_token_column_exists: Callable[[], bool],
    on_watermark_reset: Optional[Callable[[Any, int], None]] = None,
) -> APIRouter:
    """Construye el router /admin usando los helpers de main_postgres."""

    configured_token = (admin_token or "").strip()

    # ── Auth ────────────────────────────────────────────────────────────────
    def require_admin(x_admin_token: Optional[str] = Header(None, alias="x-admin-token")) -> None:
        if not configured_token:
            raise HTTPException(
                status_code=503,
                detail="Admin no configurado: agrega [admin] token=... en config.ini o NEXUS_ADMIN_TOKEN.",
            )
        provided = (x_admin_token or "").encode("utf-8")
        if not provided or not hmac.compare_digest(provided, configured_token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Token de administrador no válido.")

    router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

    # ── BD ──────────────────────────────────────────────────────────────────
    @contextmanager
    def tx() -> Iterator[Any]:
        """Transacción con cursor dict; traduce errores de integridad a HTTP."""
        try:
            conn = get_connection()
        except psycopg2.OperationalError:
            raise HTTPException(status_code=503, detail="No se pudo conectar a la BD de configuración.")
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            yield cur
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except psycopg2.errors.UniqueViolation as exc:
            conn.rollback()
            name = getattr(exc.diag, "constraint_name", "") or ""
            raise HTTPException(
                status_code=409,
                detail=_CONSTRAINT_MESSAGES.get(name, "Ya existe un registro con esos datos."),
            )
        except psycopg2.errors.ForeignKeyViolation:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="Referencia inválida: el registro relacionado no existe o tiene dependientes.",
            )
        except (psycopg2.errors.NotNullViolation, psycopg2.errors.CheckViolation,
                psycopg2.errors.StringDataRightTruncation) as exc:
            conn.rollback()
            col = getattr(exc.diag, "column_name", "") or getattr(exc.diag, "constraint_name", "") or ""
            raise HTTPException(status_code=422, detail=f"Dato no válido{(' en ' + col) if col else ''}.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fetch_all(sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        with tx() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def fetch_one(sql: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        with tx() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def exists(cur: Any, table: str, row_id: int) -> bool:
        # `table` siempre viene de literales internos, nunca del usuario.
        cur.execute(f"SELECT 1 FROM {table} WHERE id = %s", (row_id,))
        return cur.fetchone() is not None

    def count(cur: Any, sql: str, params: Tuple) -> int:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(list(row.values())[0]) if row else 0

    def new_token() -> str:
        return secrets.token_urlsafe(32)

    # ── Cifrado ─────────────────────────────────────────────────────────────
    def encrypt_value(value: Optional[str], field: str) -> str:
        if value is None or value == "":
            return ""
        if value.startswith("ENC:"):
            # Permite pegar un valor ya cifrado con encrypt_config_secret.py,
            # siempre que el backend pueda descifrarlo.
            try:
                decrypt_config_secret(value)
            except Exception:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field}: el valor ENC: no se puede descifrar con la clave del servidor.",
                )
            return value
        cipher = get_secret_cipher()
        if cipher is None:
            return value
        return "ENC:" + cipher.encrypt(value.encode("utf-8")).decode("utf-8")

    def reveal(row: Dict[str, Any], fields: Tuple[str, ...], password_field: str) -> Dict[str, Any]:
        """Descifra campos sensibles no-password y oculta la contraseña."""
        encrypted: List[str] = []
        errors: List[str] = []
        for f in fields:
            raw = row.get(f)
            if isinstance(raw, str) and raw.startswith("ENC:"):
                encrypted.append(f)
            if f == password_field:
                row["has_password"] = bool(raw)
                row.pop(f, None)
                continue
            try:
                row[f] = decrypt_config_secret(raw or "")
            except Exception:
                row[f] = None
                errors.append(f)
        row["encrypted_fields"] = encrypted
        row["decrypt_errors"] = errors
        return row

    # ── Update genérico ─────────────────────────────────────────────────────
    def apply_update(cur: Any, table: str, row_id: int, values: Dict[str, Any]) -> None:
        if not values:
            return
        # Las claves provienen de modelos Pydantic (lista blanca), no del usuario.
        cols = ", ".join(f"{k} = %s" for k in values)
        cur.execute(f"UPDATE {table} SET {cols} WHERE id = %s", (*values.values(), row_id))

    def not_found(what: str) -> HTTPException:
        return HTTPException(status_code=404, detail=f"{what} no encontrado.")

    # ── Info ────────────────────────────────────────────────────────────────
    @router.get("/whoami")
    def whoami() -> dict:
        return {
            "status": "ok",
            "role": "admin",
            "encryption_enabled": get_secret_cipher() is not None,
            "group_token_supported": group_token_column_exists(),
            "agency_token_supported": agency_token_column_exists(),
            "source_types": list(SOURCE_TYPES),
        }

    @router.get("/stats")
    def stats() -> dict:
        row = fetch_one(
            """
            SELECT
              (SELECT COUNT(*) FROM client_group)                       AS groups,
              (SELECT COUNT(*) FROM client_group WHERE is_enabled)      AS groups_enabled,
              (SELECT COUNT(*) FROM company)                            AS companies,
              (SELECT COUNT(*) FROM company WHERE is_enabled)           AS companies_enabled,
              (SELECT COUNT(*) FROM agency)                             AS agencies,
              (SELECT COUNT(*) FROM agency WHERE is_enabled)            AS agencies_enabled,
              (SELECT COUNT(*) FROM object_catalog)                     AS objects,
              (SELECT COUNT(*) FROM agency_task)                        AS tasks,
              (SELECT COUNT(*) FROM agency_task WHERE is_active)        AS tasks_active,
              (SELECT COUNT(*) FROM client_events
                 WHERE event_type = 'error' AND is_acknowledged = 0)    AS pending_errors,
              (SELECT COUNT(*) FROM client_events
                 WHERE created_at >= NOW() - INTERVAL '24 hours')       AS events_24h,
              (SELECT COUNT(*) FROM client_events
                 WHERE event_type = 'error'
                   AND created_at >= NOW() - INTERVAL '24 hours')       AS errors_24h
            """
        )
        return {k: int(v or 0) for k, v in (row or {}).items()}

    # ════════════════════════════════════════════════════════════════════════
    # GRUPOS
    # ════════════════════════════════════════════════════════════════════════
    def group_select() -> str:
        token_col = "g.group_token" if group_token_column_exists() else "NULL::varchar"
        return f"""
            SELECT g.id, g.name, {token_col} AS group_token,
                   g.warehouse_host, g.warehouse_port, g.warehouse_database,
                   g.warehouse_username, g.warehouse_password,
                   g.is_enabled, g.created_at, g.updated_at,
                   (SELECT COUNT(*) FROM company c WHERE c.group_id = g.id) AS company_count
            FROM client_group g
        """

    def group_out(row: Dict[str, Any]) -> Dict[str, Any]:
        return reveal(row, GROUP_SECRET_FIELDS, "warehouse_password")

    def get_group_or_404(group_id: int) -> Dict[str, Any]:
        row = fetch_one(group_select() + " WHERE g.id = %s", (group_id,))
        if not row:
            raise not_found("Grupo")
        return group_out(row)

    @router.get("/groups")
    def list_groups() -> dict:
        rows = fetch_all(group_select() + " ORDER BY g.name")
        return {"items": [group_out(r) for r in rows]}

    @router.get("/groups/{group_id}")
    def get_group(group_id: int) -> dict:
        return get_group_or_404(group_id)

    @router.post("/groups", status_code=201)
    def create_group(body: GroupCreate) -> dict:
        values: Dict[str, Any] = {
            "name": body.name,
            "warehouse_host": encrypt_value(body.warehouse_host, "warehouse_host"),
            "warehouse_port": body.warehouse_port,
            "warehouse_database": encrypt_value(body.warehouse_database, "warehouse_database"),
            "warehouse_username": encrypt_value(body.warehouse_username, "warehouse_username"),
            "warehouse_password": encrypt_value(body.warehouse_password, "warehouse_password"),
            "is_enabled": body.is_enabled,
        }
        if group_token_column_exists():
            values["group_token"] = new_token()
        cols = ", ".join(values)
        ph = ", ".join(["%s"] * len(values))
        with tx() as cur:
            cur.execute(f"INSERT INTO client_group ({cols}) VALUES ({ph}) RETURNING id", tuple(values.values()))
            new_id = cur.fetchone()["id"]
        return get_group_or_404(new_id)

    @router.put("/groups/{group_id}")
    def update_group(group_id: int, body: GroupUpdate) -> dict:
        data = body.model_dump(exclude_unset=True)
        clear_pw = data.pop("clear_password", False)
        values: Dict[str, Any] = {}
        for k, v in data.items():
            if v is None:
                continue
            if k == "warehouse_password":
                if v == "":
                    continue
                values[k] = encrypt_value(v, k)
            elif k in GROUP_SECRET_FIELDS:
                values[k] = encrypt_value(v, k)
            else:
                values[k] = v
        if clear_pw and "warehouse_password" not in values:
            values["warehouse_password"] = ""
        with tx() as cur:
            if not exists(cur, "client_group", group_id):
                raise not_found("Grupo")
            apply_update(cur, "client_group", group_id, values)
        return get_group_or_404(group_id)

    @router.delete("/groups/{group_id}")
    def delete_group(group_id: int) -> dict:
        with tx() as cur:
            if not exists(cur, "client_group", group_id):
                raise not_found("Grupo")
            n = count(cur, "SELECT COUNT(*) AS n FROM company WHERE group_id = %s", (group_id,))
            if n:
                raise HTTPException(
                    status_code=409,
                    detail=f"No se puede eliminar: el grupo tiene {n} empresa(s). Elimínalas o muévelas antes.",
                )
            cur.execute("DELETE FROM client_group WHERE id = %s", (group_id,))
        return {"status": "ok", "deleted": group_id}

    @router.post("/groups/{group_id}/enable")
    def enable_group(group_id: int) -> dict:
        return _set_flag("client_group", group_id, "is_enabled", True, "Grupo", get_group_or_404)

    @router.post("/groups/{group_id}/disable")
    def disable_group(group_id: int) -> dict:
        return _set_flag("client_group", group_id, "is_enabled", False, "Grupo", get_group_or_404)

    @router.post("/groups/{group_id}/regenerate-token")
    def regenerate_group_token(group_id: int) -> dict:
        if not group_token_column_exists():
            raise HTTPException(status_code=503, detail="La BD no tiene la columna client_group.group_token.")
        return _set_token("client_group", "group_token", group_id, new_token(), "Grupo", get_group_or_404)

    @router.delete("/groups/{group_id}/token")
    def revoke_group_token(group_id: int) -> dict:
        if not group_token_column_exists():
            raise HTTPException(status_code=503, detail="La BD no tiene la columna client_group.group_token.")
        return _set_token("client_group", "group_token", group_id, None, "Grupo", get_group_or_404)

    # ════════════════════════════════════════════════════════════════════════
    # EMPRESAS (company)
    # ════════════════════════════════════════════════════════════════════════
    COMPANY_SELECT = """
        SELECT c.id, c.group_id, g.name AS group_name, c.name, c.company_token,
               c.source_type, c.source_host, c.source_port, c.source_database,
               c.source_username, c.source_password, c.source_dsn,
               c.verbose_logging, c.refresh_seconds, c.is_enabled,
               g.is_enabled AS group_enabled,
               c.created_at, c.updated_at,
               (SELECT COUNT(*) FROM agency a WHERE a.company_id = c.id)         AS agency_count,
               (SELECT COUNT(*) FROM object_catalog o WHERE o.company_id = c.id) AS object_count
        FROM company c
        JOIN client_group g ON g.id = c.group_id
    """

    def company_out(row: Dict[str, Any]) -> Dict[str, Any]:
        return reveal(row, COMPANY_SECRET_FIELDS, "source_password")

    def get_company_or_404(company_id: int) -> Dict[str, Any]:
        row = fetch_one(COMPANY_SELECT + " WHERE c.id = %s", (company_id,))
        if not row:
            raise not_found("Empresa")
        return company_out(row)

    @router.get("/companies")
    def list_companies(group_id: Optional[int] = Query(None)) -> dict:
        if group_id is not None:
            rows = fetch_all(COMPANY_SELECT + " WHERE c.group_id = %s ORDER BY g.name, c.name", (group_id,))
        else:
            rows = fetch_all(COMPANY_SELECT + " ORDER BY g.name, c.name")
        return {"items": [company_out(r) for r in rows]}

    @router.get("/companies/{company_id}")
    def get_company(company_id: int) -> dict:
        return get_company_or_404(company_id)

    @router.post("/companies", status_code=201)
    def create_company(body: CompanyCreate) -> dict:
        values: Dict[str, Any] = {
            "group_id": body.group_id,
            "name": body.name,
            "company_token": new_token(),
            "source_type": body.source_type,
            "source_port": body.source_port,
            "verbose_logging": body.verbose_logging,
            "refresh_seconds": body.refresh_seconds,
            "is_enabled": body.is_enabled,
        }
        for f in COMPANY_SECRET_FIELDS:
            values[f] = encrypt_value(getattr(body, f), f)
        cols = ", ".join(values)
        ph = ", ".join(["%s"] * len(values))
        with tx() as cur:
            if not exists(cur, "client_group", body.group_id):
                raise HTTPException(status_code=404, detail="Grupo no encontrado.")
            cur.execute(f"INSERT INTO company ({cols}) VALUES ({ph}) RETURNING id", tuple(values.values()))
            new_id = cur.fetchone()["id"]
        return get_company_or_404(new_id)

    @router.put("/companies/{company_id}")
    def update_company(company_id: int, body: CompanyUpdate) -> dict:
        data = body.model_dump(exclude_unset=True)
        clear_pw = data.pop("clear_password", False)
        values: Dict[str, Any] = {}
        for k, v in data.items():
            if v is None:
                continue
            if k == "source_password":
                if v == "":
                    continue
                values[k] = encrypt_value(v, k)
            elif k in COMPANY_SECRET_FIELDS:
                values[k] = encrypt_value(v, k)
            else:
                values[k] = v
        if clear_pw and "source_password" not in values:
            values["source_password"] = ""
        with tx() as cur:
            if not exists(cur, "company", company_id):
                raise not_found("Empresa")
            if "group_id" in values and not exists(cur, "client_group", values["group_id"]):
                raise HTTPException(status_code=404, detail="Grupo no encontrado.")
            apply_update(cur, "company", company_id, values)
        return get_company_or_404(company_id)

    @router.delete("/companies/{company_id}")
    def delete_company(company_id: int) -> dict:
        with tx() as cur:
            if not exists(cur, "company", company_id):
                raise not_found("Empresa")
            n_ag = count(cur, "SELECT COUNT(*) AS n FROM agency WHERE company_id = %s", (company_id,))
            n_obj = count(cur, "SELECT COUNT(*) AS n FROM object_catalog WHERE company_id = %s", (company_id,))
            if n_ag or n_obj:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"No se puede eliminar: la empresa tiene {n_ag} agencia(s) y "
                        f"{n_obj} objeto(s) de catálogo."
                    ),
                )
            cur.execute("DELETE FROM company WHERE id = %s", (company_id,))
        return {"status": "ok", "deleted": company_id}

    @router.post("/companies/{company_id}/enable")
    def enable_company(company_id: int) -> dict:
        return _set_flag("company", company_id, "is_enabled", True, "Empresa", get_company_or_404)

    @router.post("/companies/{company_id}/disable")
    def disable_company(company_id: int) -> dict:
        return _set_flag("company", company_id, "is_enabled", False, "Empresa", get_company_or_404)

    @router.post("/companies/{company_id}/regenerate-token")
    def regenerate_company_token(company_id: int) -> dict:
        return _set_token("company", "company_token", company_id, new_token(), "Empresa", get_company_or_404)

    # ════════════════════════════════════════════════════════════════════════
    # AGENCIAS
    # ════════════════════════════════════════════════════════════════════════
    def agency_select() -> str:
        token_col = "a.agency_token" if agency_token_column_exists() else "NULL::varchar"
        return f"""
            SELECT a.id, a.company_id, c.name AS company_name,
                   c.group_id, g.name AS group_name,
                   a.name, {token_col} AS agency_token, a.is_enabled,
                   a.created_at, a.updated_at,
                   (SELECT COUNT(*) FROM agency_task t WHERE t.agency_id = a.id) AS task_count
            FROM agency a
            JOIN company c ON c.id = a.company_id
            JOIN client_group g ON g.id = c.group_id
        """

    def get_agency_or_404(agency_id: int) -> Dict[str, Any]:
        row = fetch_one(agency_select() + " WHERE a.id = %s", (agency_id,))
        if not row:
            raise not_found("Agencia")
        return row

    @router.get("/agencies")
    def list_agencies(
        company_id: Optional[int] = Query(None),
        group_id: Optional[int] = Query(None),
    ) -> dict:
        conds: List[str] = []
        params: List[Any] = []
        if company_id is not None:
            conds.append("a.company_id = %s")
            params.append(company_id)
        if group_id is not None:
            conds.append("c.group_id = %s")
            params.append(group_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = fetch_all(agency_select() + where + " ORDER BY g.name, c.name, a.name", tuple(params))
        return {"items": rows}

    @router.get("/agencies/{agency_id}")
    def get_agency(agency_id: int) -> dict:
        return get_agency_or_404(agency_id)

    @router.post("/agencies", status_code=201)
    def create_agency(body: AgencyCreate) -> dict:
        values: Dict[str, Any] = {
            "company_id": body.company_id,
            "name": body.name,
            "is_enabled": body.is_enabled,
        }
        if body.generate_token and agency_token_column_exists():
            values["agency_token"] = new_token()
        cols = ", ".join(values)
        ph = ", ".join(["%s"] * len(values))
        with tx() as cur:
            if not exists(cur, "company", body.company_id):
                raise HTTPException(status_code=404, detail="Empresa no encontrada.")
            cur.execute(f"INSERT INTO agency ({cols}) VALUES ({ph}) RETURNING id", tuple(values.values()))
            new_id = cur.fetchone()["id"]
        return get_agency_or_404(new_id)

    @router.put("/agencies/{agency_id}")
    def update_agency(agency_id: int, body: AgencyUpdate) -> dict:
        values = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        with tx() as cur:
            cur.execute("SELECT company_id FROM agency WHERE id = %s", (agency_id,))
            current = cur.fetchone()
            if not current:
                raise not_found("Agencia")
            if "company_id" in values and values["company_id"] != current["company_id"]:
                if not exists(cur, "company", values["company_id"]):
                    raise HTTPException(status_code=404, detail="Empresa no encontrada.")
                n = count(cur, "SELECT COUNT(*) AS n FROM agency_task WHERE agency_id = %s", (agency_id,))
                if n:
                    raise HTTPException(
                        status_code=409,
                        detail=f"No se puede cambiar de empresa: la agencia tiene {n} tarea(s) ligadas al catálogo de su empresa actual.",
                    )
            apply_update(cur, "agency", agency_id, values)
        return get_agency_or_404(agency_id)

    @router.delete("/agencies/{agency_id}")
    def delete_agency(agency_id: int) -> dict:
        with tx() as cur:
            if not exists(cur, "agency", agency_id):
                raise not_found("Agencia")
            n = count(cur, "SELECT COUNT(*) AS n FROM agency_task WHERE agency_id = %s", (agency_id,))
            if n:
                raise HTTPException(
                    status_code=409,
                    detail=f"No se puede eliminar: la agencia tiene {n} tarea(s). Elimínalas antes.",
                )
            cur.execute("DELETE FROM agency WHERE id = %s", (agency_id,))
        return {"status": "ok", "deleted": agency_id}

    @router.post("/agencies/{agency_id}/enable")
    def enable_agency(agency_id: int) -> dict:
        return _set_flag("agency", agency_id, "is_enabled", True, "Agencia", get_agency_or_404)

    @router.post("/agencies/{agency_id}/disable")
    def disable_agency(agency_id: int) -> dict:
        return _set_flag("agency", agency_id, "is_enabled", False, "Agencia", get_agency_or_404)

    @router.post("/agencies/{agency_id}/regenerate-token")
    def regenerate_agency_token(agency_id: int) -> dict:
        if not agency_token_column_exists():
            raise HTTPException(status_code=503, detail="La BD no tiene la columna agency.agency_token.")
        return _set_token("agency", "agency_token", agency_id, new_token(), "Agencia", get_agency_or_404)

    @router.delete("/agencies/{agency_id}/token")
    def revoke_agency_token(agency_id: int) -> dict:
        if not agency_token_column_exists():
            raise HTTPException(status_code=503, detail="La BD no tiene la columna agency.agency_token.")
        return _set_token("agency", "agency_token", agency_id, None, "Agencia", get_agency_or_404)

    # ════════════════════════════════════════════════════════════════════════
    # CATÁLOGO DE OBJETOS
    # ════════════════════════════════════════════════════════════════════════
    OBJECT_SELECT = """
        SELECT o.id, o.company_id, c.name AS company_name,
               c.group_id, g.name AS group_name,
               o.name, o.description, o.destination_table, o.create_table_sql,
               o.upsert_keys, o.constraint_name, o.create_constraint_sql,
               o.static_columns, o.is_enabled, o.created_at, o.updated_at,
               (SELECT COUNT(*) FROM agency_task t WHERE t.object_catalog_id = o.id) AS task_count
        FROM object_catalog o
        JOIN company c ON c.id = o.company_id
        JOIN client_group g ON g.id = c.group_id
    """

    def get_object_or_404(object_id: int) -> Dict[str, Any]:
        row = fetch_one(OBJECT_SELECT + " WHERE o.id = %s", (object_id,))
        if not row:
            raise not_found("Objeto")
        return row

    @router.get("/objects")
    def list_objects(
        company_id: Optional[int] = Query(None),
        group_id: Optional[int] = Query(None),
    ) -> dict:
        conds: List[str] = []
        params: List[Any] = []
        if company_id is not None:
            conds.append("o.company_id = %s")
            params.append(company_id)
        if group_id is not None:
            conds.append("c.group_id = %s")
            params.append(group_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = fetch_all(OBJECT_SELECT + where + " ORDER BY g.name, c.name, o.name", tuple(params))
        return {"items": rows}

    @router.get("/objects/{object_id}")
    def get_object(object_id: int) -> dict:
        return get_object_or_404(object_id)

    @router.post("/objects", status_code=201)
    def create_object(body: ObjectCreate) -> dict:
        values = body.model_dump()
        cols = ", ".join(values)
        ph = ", ".join(["%s"] * len(values))
        with tx() as cur:
            if not exists(cur, "company", body.company_id):
                raise HTTPException(status_code=404, detail="Empresa no encontrada.")
            cur.execute(f"INSERT INTO object_catalog ({cols}) VALUES ({ph}) RETURNING id", tuple(values.values()))
            new_id = cur.fetchone()["id"]
        return get_object_or_404(new_id)

    @router.put("/objects/{object_id}")
    def update_object(object_id: int, body: ObjectUpdate) -> dict:
        nullable = {"description", "create_table_sql", "upsert_keys", "constraint_name",
                    "create_constraint_sql", "static_columns"}
        values = {
            k: v for k, v in body.model_dump(exclude_unset=True).items()
            if v is not None or k in nullable
        }
        with tx() as cur:
            cur.execute("SELECT company_id FROM object_catalog WHERE id = %s", (object_id,))
            current = cur.fetchone()
            if not current:
                raise not_found("Objeto")
            if "company_id" in values and values["company_id"] != current["company_id"]:
                if not exists(cur, "company", values["company_id"]):
                    raise HTTPException(status_code=404, detail="Empresa no encontrada.")
                n = count(cur, "SELECT COUNT(*) AS n FROM agency_task WHERE object_catalog_id = %s", (object_id,))
                if n:
                    raise HTTPException(
                        status_code=409,
                        detail=f"No se puede cambiar de empresa: el objeto tiene {n} tarea(s).",
                    )
            apply_update(cur, "object_catalog", object_id, values)
        return get_object_or_404(object_id)

    @router.delete("/objects/{object_id}")
    def delete_object(object_id: int) -> dict:
        with tx() as cur:
            if not exists(cur, "object_catalog", object_id):
                raise not_found("Objeto")
            n = count(cur, "SELECT COUNT(*) AS n FROM agency_task WHERE object_catalog_id = %s", (object_id,))
            if n:
                raise HTTPException(
                    status_code=409,
                    detail=f"No se puede eliminar: el objeto está asignado a {n} tarea(s).",
                )
            cur.execute("DELETE FROM object_catalog WHERE id = %s", (object_id,))
        return {"status": "ok", "deleted": object_id}

    @router.post("/objects/{object_id}/enable")
    def enable_object(object_id: int) -> dict:
        return _set_flag("object_catalog", object_id, "is_enabled", True, "Objeto", get_object_or_404)

    @router.post("/objects/{object_id}/disable")
    def disable_object(object_id: int) -> dict:
        return _set_flag("object_catalog", object_id, "is_enabled", False, "Objeto", get_object_or_404)

    # ════════════════════════════════════════════════════════════════════════
    # TAREAS (agency_task)
    # ════════════════════════════════════════════════════════════════════════
    TASK_SELECT = """
        SELECT t.id, t.agency_id, a.name AS agency_name,
               a.company_id, c.name AS company_name,
               c.group_id, g.name AS group_name,
               t.object_catalog_id, o.name AS object_name, o.destination_table,
               t.extract_sql, t.schedule_seconds, t.is_active, t.run_on_company_token,
               t.last_run_at, t.created_at, t.updated_at, t.query_version, t.query_hash,
               t.expected_duration_seconds, t.delay_tolerance_seconds,
               (t.is_active AND a.is_enabled AND o.is_enabled
                 AND c.is_enabled AND g.is_enabled) AS effective_active
        FROM agency_task t
        JOIN agency a ON a.id = t.agency_id
        JOIN object_catalog o ON o.id = t.object_catalog_id
        JOIN company c ON c.id = a.company_id
        JOIN client_group g ON g.id = c.group_id
    """

    def get_task_or_404(task_id: int) -> Dict[str, Any]:
        row = fetch_one(TASK_SELECT + " WHERE t.id = %s", (task_id,))
        if not row:
            raise not_found("Tarea")
        return row

    def check_task_refs(cur: Any, agency_id: int, object_id: int) -> None:
        cur.execute("SELECT company_id FROM agency WHERE id = %s", (agency_id,))
        ag = cur.fetchone()
        if not ag:
            raise HTTPException(status_code=404, detail="Agencia no encontrada.")
        cur.execute("SELECT company_id FROM object_catalog WHERE id = %s", (object_id,))
        ob = cur.fetchone()
        if not ob:
            raise HTTPException(status_code=404, detail="Objeto no encontrado.")
        if ag["company_id"] != ob["company_id"]:
            raise HTTPException(
                status_code=422,
                detail="El objeto del catálogo pertenece a otra empresa distinta a la de la agencia.",
            )

    @router.get("/tasks")
    def list_tasks(
        group_id: Optional[int] = Query(None),
        company_id: Optional[int] = Query(None),
        agency_id: Optional[int] = Query(None),
        object_catalog_id: Optional[int] = Query(None),
    ) -> dict:
        conds: List[str] = []
        params: List[Any] = []
        for col, val in (("c.group_id", group_id), ("a.company_id", company_id),
                         ("t.agency_id", agency_id), ("t.object_catalog_id", object_catalog_id)):
            if val is not None:
                conds.append(f"{col} = %s")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = fetch_all(TASK_SELECT + where + " ORDER BY g.name, c.name, a.name, o.name", tuple(params))
        return {"items": rows}

    @router.get("/tasks/{task_id}")
    def get_task(task_id: int) -> dict:
        return get_task_or_404(task_id)

    @router.post("/tasks", status_code=201)
    def create_task(body: TaskCreate) -> dict:
        values = body.model_dump()
        cols = ", ".join(values)
        ph = ", ".join(["%s"] * len(values))
        with tx() as cur:
            check_task_refs(cur, body.agency_id, body.object_catalog_id)
            cur.execute(f"INSERT INTO agency_task ({cols}) VALUES ({ph}) RETURNING id", tuple(values.values()))
            new_id = cur.fetchone()["id"]
        return get_task_or_404(new_id)

    @router.put("/tasks/{task_id}")
    def update_task(task_id: int, body: TaskUpdate) -> dict:
        values = {k: v for k, v in body.model_dump(exclude_unset=True).items()
                  if v is not None or k in TASK_NULLABLE_FIELDS}
        with tx() as cur:
            cur.execute("SELECT agency_id, object_catalog_id FROM agency_task WHERE id = %s", (task_id,))
            current = cur.fetchone()
            if not current:
                raise not_found("Tarea")
            if "agency_id" in values or "object_catalog_id" in values:
                check_task_refs(
                    cur,
                    values.get("agency_id", current["agency_id"]),
                    values.get("object_catalog_id", current["object_catalog_id"]),
                )
            apply_update(cur, "agency_task", task_id, values)
        return get_task_or_404(task_id)

    @router.delete("/tasks/{task_id}")
    def delete_task(task_id: int) -> dict:
        with tx() as cur:
            cur.execute("DELETE FROM agency_task WHERE id = %s", (task_id,))
            if cur.rowcount == 0:
                raise not_found("Tarea")
        return {"status": "ok", "deleted": task_id}

    @router.post("/tasks/{task_id}/enable")
    def enable_task(task_id: int) -> dict:
        return _set_flag("agency_task", task_id, "is_active", True, "Tarea", get_task_or_404)

    @router.post("/tasks/{task_id}/disable")
    def disable_task(task_id: int) -> dict:
        return _set_flag("agency_task", task_id, "is_active", False, "Tarea", get_task_or_404)

    @router.post("/tasks/{task_id}/reset-last-run")
    def reset_task_last_run(task_id: int) -> dict:
        """
        Pone last_run_at en NULL y reinicia el watermark de task_sync_state: la
        próxima ejecución hará carga completa ('{last_run}' = 1900-01-01).
        watermark_reset_at evita que un checkpoint de una ejecución que empezó
        antes del reinicio vuelva a fijar el watermark.
        """
        with tx() as cur:
            cur.execute("UPDATE agency_task SET last_run_at = NULL WHERE id = %s", (task_id,))
            if cur.rowcount == 0:
                raise not_found("Tarea")
            cur.execute(
                """
                INSERT INTO task_sync_state (task_id, watermark, watermark_kind, watermark_reset_at)
                VALUES (%s, NULL, NULL, NOW())
                ON CONFLICT (task_id) DO UPDATE
                   SET watermark = NULL, watermark_kind = NULL, watermark_reset_at = NOW(), updated_at = NOW()
                """,
                (task_id,),
            )
            if on_watermark_reset is not None:
                # Cierra incidencias de "tipo de reloj distinto" de la tarea (motivo watermark_reset).
                on_watermark_reset(cur, task_id)
        return get_task_or_404(task_id)

    # ── Helpers compartidos (definidos al final; usan closures de arriba) ────
    def _set_flag(table: str, row_id: int, column: str, value: bool, what: str,
                  getter: Callable[[int], Dict[str, Any]]) -> Dict[str, Any]:
        with tx() as cur:
            cur.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", (value, row_id))
            if cur.rowcount == 0:
                raise not_found(what)
        return getter(row_id)

    def _set_token(table: str, column: str, row_id: int, token: Optional[str], what: str,
                   getter: Callable[[int], Dict[str, Any]]) -> Dict[str, Any]:
        with tx() as cur:
            cur.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", (token, row_id))
            if cur.rowcount == 0:
                raise not_found(what)
        return getter(row_id)

    return router
