"""
users_postgres.py — API de sesión y de administración de usuarios del panel
───────────────────────────────────────────────────────────────────────────
  POST /admin/auth/login             {email, password} (público; límite por IP y bloqueo por correo)
  POST /admin/auth/logout           (sesión)
  GET  /admin/auth/me               (sesión; también con must_change_password)
  POST /admin/auth/change-password  (sesión; política de contraseñas)

  users.manage (solo alcance global):
  GET/POST /admin/users, GET/PUT /admin/users/{id}, POST /admin/users/{id}/reset-password,
  PUT /admin/users/{id}/roles, POST /admin/users/{id}/unlock,
  GET /admin/sessions, POST /admin/sessions/{id}/revoke, GET /admin/roles

  audit.view (por alcance de grupo):
  GET /admin/audit
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.errors
import psycopg2.extras
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from panel_auth import (
    AuthContext, AuthService, derive_username_from_email, err, iso, not_found, password_problems,
    valid_email, valid_username,
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Base):
    email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=1024)


class ChangePasswordBody(_Base):
    current_password: str = Field(..., min_length=1, max_length=1024)
    new_password: str = Field(..., min_length=1, max_length=256)


class RoleAssignment(_Base):
    role: str = Field(..., min_length=1, max_length=64)
    group_id: Optional[int] = Field(None, ge=1)   # None = todos los grupos


class UserCreate(_Base):
    email: str = Field(..., min_length=3, max_length=255)
    username: Optional[str] = Field(None, max_length=64)
    display_name: str = Field("", max_length=120)
    password: str = Field(..., min_length=1, max_length=256)
    is_superadmin: bool = False
    must_change_password: bool = True
    roles: List[RoleAssignment] = Field(default_factory=list, max_length=200)


class UserUpdate(_Base):
    display_name: Optional[str] = Field(None, max_length=120)
    email: Optional[str] = Field(None, min_length=3, max_length=255)
    is_active: Optional[bool] = None
    is_superadmin: Optional[bool] = None


class ResetPasswordBody(_Base):
    password: str = Field(..., min_length=1, max_length=256)


class RolesBody(_Base):
    roles: List[RoleAssignment] = Field(default_factory=list, max_length=200)


def create_users_router(*, auth: AuthService) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["auth"])
    client_ip = auth.client_ip

    def tx_conn() -> Any:
        try:
            return auth.get_connection()
        except psycopg2.OperationalError:
            raise err(503, "config_db_unavailable", "No se pudo conectar a la BD de configuración.")

    class _Tx:
        def __enter__(self) -> Any:
            self.conn = tx_conn()
            self.cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            return self.cur

        def __exit__(self, et: Any, ev: Any, tb: Any) -> bool:
            try:
                if et is None:
                    self.conn.commit()
                else:
                    self.conn.rollback()
                    if isinstance(ev, psycopg2.errors.UniqueViolation):
                        raise err(409, "duplicate", "Ya existe un usuario con ese correo o nombre de usuario.")
                    if isinstance(ev, psycopg2.errors.ForeignKeyViolation):
                        raise err(404, "not_found", "Rol o grupo inexistente.")
            finally:
                self.conn.close()
            return False

    # ── Sesión ──────────────────────────────────────────────────────────────
    @router.post("/auth/login", dependencies=[Depends(auth.public())])
    def login(body: LoginBody, request: Request) -> dict:
        return auth.login(body.email, body.password, client_ip(request),
                          request.headers.get("user-agent", ""))

    @router.post("/auth/logout")
    def logout(request: Request, ctx: AuthContext = Depends(auth.authenticated())) -> dict:
        auth.logout(ctx, client_ip(request))
        return {"status": "ok"}

    @router.get("/auth/me")
    def me(ctx: AuthContext = Depends(auth.authenticated())) -> dict:
        out = auth.profile(ctx)
        out["password_min_length"] = auth.s.password_min_length
        # Grupos visibles (para selectores) sin exponer nada más.
        with _Tx() as cur:
            sql, params = ctx.scope_sql("g.id")
            cur.execute(f"SELECT g.id, g.name FROM client_group g WHERE {sql} ORDER BY g.name", params)
            out["groups"] = [dict(r) for r in cur.fetchall()]
        return out

    @router.post("/auth/change-password")
    def change_password(body: ChangePasswordBody, request: Request,
                        ctx: AuthContext = Depends(auth.authenticated())) -> dict:
        auth.change_password(ctx, body.current_password, body.new_password, client_ip(request))
        return {"status": "ok"}

    # ── Usuarios ────────────────────────────────────────────────────────────
    manage = auth.perm("users.manage")

    USER_SELECT = """
        SELECT u.id, u.username, u.display_name, u.email, u.is_active, u.is_superadmin, u.must_change_password,
               u.failed_attempts, u.locked_until, u.last_login_at, u.password_changed_at, u.created_at,
               u.created_by, u.updated_at,
               COALESCE((SELECT json_agg(json_build_object('role', ur.role_code, 'role_name', r.name,
                                                           'group_id', ur.group_id, 'group_name', g.name)
                                         ORDER BY ur.role_code, g.name)
                         FROM panel_user_role ur JOIN panel_role r ON r.code = ur.role_code
                         LEFT JOIN client_group g ON g.id = ur.group_id
                         WHERE ur.user_id = u.id), '[]'::json) AS roles,
               (SELECT COUNT(*) FROM panel_session s WHERE s.user_id = u.id AND s.revoked_at IS NULL
                   AND s.expires_at > NOW()
                   AND s.last_seen_at > NOW() - make_interval(secs => {idle})) AS active_sessions
        FROM panel_user u
    """

    USER_SELECT = USER_SELECT.replace("{idle}", str(int(auth.s.session_idle_seconds)))

    def protect_superadmin(ctx: AuthContext, target_is_superadmin: bool) -> None:
        """Un administrador de usuarios que no es superadministrador no toca a un superadministrador."""
        if target_is_superadmin and not ctx.is_superadmin:
            raise err(403, "superadmin_required", "Solo un superadministrador puede modificar a otro superadministrador.")

    def user_out(r: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(r)
        for k in ("locked_until", "last_login_at", "password_changed_at", "created_at", "updated_at"):
            d[k] = iso(d.get(k))
        d["locked"] = bool(r.get("locked_until") and r["locked_until"] > datetime.now(r["locked_until"].tzinfo))
        return d

    def get_user(cur: Any, user_id: int) -> Dict[str, Any]:
        cur.execute(USER_SELECT + " WHERE u.id = %s", (user_id,))
        r = cur.fetchone()
        if not r:
            raise not_found("Usuario")
        return user_out(r)

    def check_password(pw: str, username: str, email: Optional[str] = None) -> None:
        problems = password_problems(pw, username, auth.s.password_min_length, email=email)
        if problems:
            raise err(422, "weak_password", "La contraseña " + "; ".join(problems) + ".", problems=problems)

    def unique_username(cur: Any, base: str) -> str:
        """`base` saneado a las reglas de username; si ya existe, se sufija -2, -3, ... hasta caber en 64."""
        candidate = base
        n = 2
        while True:
            cur.execute("SELECT 1 FROM panel_user WHERE lower(username) = %s", (candidate,))
            if not cur.fetchone():
                return candidate
            suffix = f"-{n}"
            candidate = base[: 64 - len(suffix)] + suffix
            n += 1

    def set_roles(cur: Any, user_id: int, roles: List[RoleAssignment], actor: str) -> None:
        cur.execute("SELECT code FROM panel_role")
        known = {r["code"] for r in cur.fetchall()}
        cur.execute("""SELECT DISTINCT rp.role_code FROM panel_role_permission rp
                       JOIN panel_permission p ON p.code = rp.permission_code WHERE p.global_only""")
        global_roles = {r["role_code"] for r in cur.fetchall()}
        cur.execute("DELETE FROM panel_user_role WHERE user_id = %s", (user_id,))
        seen = set()
        for a in roles:
            if a.role not in known:
                raise err(422, "unknown_role", f"Rol desconocido: {a.role}.")
            if a.role in global_roles and a.group_id is not None:
                raise err(422, "global_only_role", f"El rol {a.role} solo puede asignarse con alcance global.")
            key = (a.role, a.group_id)
            if key in seen:
                continue
            seen.add(key)
            cur.execute("INSERT INTO panel_user_role (user_id, role_code, group_id, created_by) VALUES (%s, %s, %s, %s)",
                        (user_id, a.role, a.group_id, actor[:100]))

    def active_superadmins(cur: Any, excluding: Optional[int] = None) -> int:
        cur.execute("SELECT COUNT(*) AS n FROM panel_user WHERE is_superadmin AND is_active AND id <> %s",
                    (excluding or 0,))
        return int(cur.fetchone()["n"])

    @router.get("/users")
    def list_users(ctx: AuthContext = Depends(manage)) -> dict:
        with _Tx() as cur:
            cur.execute(USER_SELECT + " ORDER BY u.username")
            return {"items": [user_out(r) for r in cur.fetchall()]}

    @router.get("/users/{user_id}")
    def read_user(user_id: int, ctx: AuthContext = Depends(manage)) -> dict:
        with _Tx() as cur:
            u = get_user(cur, user_id)
            cur.execute("""SELECT id, created_at, last_seen_at, expires_at, ip, user_agent, revoked_at, revoked_reason
                           FROM panel_session WHERE user_id = %s ORDER BY id DESC LIMIT 50""", (user_id,))
            u["sessions"] = [dict(s, created_at=iso(s["created_at"]), last_seen_at=iso(s["last_seen_at"]),
                                  expires_at=iso(s["expires_at"]), revoked_at=iso(s["revoked_at"]))
                             for s in cur.fetchall()]
        return u

    @router.post("/users", status_code=201)
    def create_user(body: UserCreate, request: Request, ctx: AuthContext = Depends(manage)) -> dict:
        email = body.email.strip().lower()
        if not valid_email(email):
            raise err(422, "invalid_email", "Correo no válido.")
        if body.is_superadmin and not ctx.is_superadmin:
            raise err(403, "superadmin_required", "Solo un superadministrador puede crear otro superadministrador.")
        base_uname = (body.username or "").strip().lower() or derive_username_from_email(email)
        if not valid_username(base_uname):
            raise err(422, "invalid_username",
                      "Usuario: 3-64 caracteres en minúsculas, números, punto, guion o guion bajo.")
        check_password(body.password, base_uname, email=email)
        with _Tx() as cur:
            cur.execute("SELECT 1 FROM panel_user WHERE lower(email) = %s", (email,))
            if cur.fetchone():
                raise err(409, "duplicate", "Ya existe un usuario con ese correo.")
            uname = unique_username(cur, base_uname)
            cur.execute("""INSERT INTO panel_user (username, display_name, email, password_hash, is_superadmin,
                                                   must_change_password, created_by, created_by_user_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (uname, body.display_name.strip() or uname, email,
                         auth.hash_password(body.password), body.is_superadmin, body.must_change_password,
                         ctx.username, ctx.user_id))
            uid = cur.fetchone()["id"]
            set_roles(cur, uid, body.roles, ctx.username)
            auth.audit(ctx, action="users.create", ip=client_ip(request), status_code=201, target_type="user",
                       target_id=str(uid), details={"username": uname, "email": email,
                                                    "superadmin": body.is_superadmin,
                                                    "roles": [a.model_dump() for a in body.roles]}, cur=cur)
            return get_user(cur, uid)

    @router.put("/users/{user_id}")
    def update_user(user_id: int, body: UserUpdate, request: Request, ctx: AuthContext = Depends(manage)) -> dict:
        data = body.model_dump(exclude_unset=True)
        with _Tx() as cur:
            cur.execute("SELECT * FROM panel_user WHERE id = %s FOR UPDATE", (user_id,))
            u = cur.fetchone()
            if not u:
                raise not_found("Usuario")
            protect_superadmin(ctx, bool(u["is_superadmin"]))
            if "is_superadmin" in data and data["is_superadmin"] != u["is_superadmin"] and not ctx.is_superadmin:
                raise err(403, "superadmin_required", "Solo un superadministrador puede cambiar ese atributo.")
            if user_id == ctx.user_id and (data.get("is_active") is False or data.get("is_superadmin") is False):
                raise err(409, "self_lockout", "No puede desactivarse ni quitarse el superadministrador a sí mismo.")
            if u["is_superadmin"] and u["is_active"] and (data.get("is_active") is False
                                                         or data.get("is_superadmin") is False):
                if active_superadmins(cur, excluding=user_id) == 0:
                    raise err(409, "last_superadmin", "Debe quedar al menos un superadministrador activo.")
            sets: Dict[str, Any] = {}
            for k in ("display_name", "email", "is_active", "is_superadmin"):
                if k in data and data[k] is not None:
                    sets[k] = data[k].strip() if isinstance(data[k], str) else data[k]
            if "email" in sets:
                em = sets["email"].lower()
                if not valid_email(em):
                    raise err(422, "invalid_email", "Correo no válido.")
                cur.execute("SELECT 1 FROM panel_user WHERE lower(email) = %s AND id <> %s", (em, user_id))
                if cur.fetchone():
                    raise err(409, "duplicate", "Ya existe un usuario con ese correo.")
                sets["email"] = em
            if sets:
                cols = ", ".join(f"{k} = %s" for k in sets)  # claves de lista blanca
                cur.execute(f"UPDATE panel_user SET {cols}, updated_at = NOW() WHERE id = %s", (*sets.values(), user_id))
            if sets.get("is_active") is False:
                auth.revoke_sessions(user_id=user_id, reason="user_disabled", cur=cur)
            auth.audit(ctx, action="users.update", ip=client_ip(request), status_code=200, target_type="user",
                       target_id=str(user_id), details={"changed": sorted(sets)}, cur=cur)
            return get_user(cur, user_id)

    @router.post("/users/{user_id}/reset-password")
    def reset_password(user_id: int, body: ResetPasswordBody, request: Request,
                       ctx: AuthContext = Depends(manage)) -> dict:
        with _Tx() as cur:
            cur.execute("SELECT username, email, is_superadmin FROM panel_user WHERE id = %s FOR UPDATE", (user_id,))
            u = cur.fetchone()
            if not u:
                raise not_found("Usuario")
            if u["is_superadmin"] and not ctx.is_superadmin:
                raise err(403, "superadmin_required", "Solo un superadministrador puede reiniciar esa contraseña.")
            check_password(body.password, u["username"], email=u["email"])
            cur.execute("""UPDATE panel_user SET password_hash = %s, must_change_password = TRUE,
                                  failed_attempts = 0, locked_until = NULL, password_changed_at = NOW(),
                                  updated_at = NOW() WHERE id = %s""", (auth.hash_password(body.password), user_id))
            n = auth.revoke_sessions(user_id=user_id, reason="password_reset", cur=cur)
            auth.audit(ctx, action="users.reset_password", ip=client_ip(request), status_code=200,
                       target_type="user", target_id=str(user_id), details={"sessions_revoked": n}, cur=cur)
            return get_user(cur, user_id)

    @router.post("/users/{user_id}/unlock")
    def unlock_user(user_id: int, request: Request, ctx: AuthContext = Depends(manage)) -> dict:
        with _Tx() as cur:
            cur.execute("SELECT is_superadmin FROM panel_user WHERE id = %s FOR UPDATE", (user_id,))
            u = cur.fetchone()
            if not u:
                raise not_found("Usuario")
            protect_superadmin(ctx, bool(u["is_superadmin"]))
            cur.execute("UPDATE panel_user SET failed_attempts = 0, locked_until = NULL WHERE id = %s", (user_id,))
            auth.audit(ctx, action="users.unlock", ip=client_ip(request), status_code=200, target_type="user",
                       target_id=str(user_id), cur=cur)
            return get_user(cur, user_id)

    @router.put("/users/{user_id}/roles")
    def put_roles(user_id: int, body: RolesBody, request: Request, ctx: AuthContext = Depends(manage)) -> dict:
        with _Tx() as cur:
            cur.execute("SELECT id, is_superadmin FROM panel_user WHERE id = %s FOR UPDATE", (user_id,))
            u = cur.fetchone()
            if not u:
                raise not_found("Usuario")
            protect_superadmin(ctx, bool(u["is_superadmin"]))
            if user_id == ctx.user_id and not ctx.is_superadmin:
                # Evita la auto-escalada: los roles propios los cambia OTRO administrador.
                auth.audit(ctx, action="users.set_roles_self_denied", ip=client_ip(request), status_code=403,
                           target_type="user", target_id=str(user_id), cur=cur)
                cur.connection.commit()
                raise err(403, "self_roles", "No puede modificar sus propios roles; pídalo a otro administrador.")
            set_roles(cur, user_id, body.roles, ctx.username)
            auth.audit(ctx, action="users.set_roles", ip=client_ip(request), status_code=200, target_type="user",
                       target_id=str(user_id), details={"roles": [a.model_dump() for a in body.roles]}, cur=cur)
            return get_user(cur, user_id)

    @router.get("/roles")
    def list_roles(ctx: AuthContext = Depends(manage)) -> dict:
        with _Tx() as cur:
            cur.execute("""SELECT r.code, r.name, r.description,
                                  ARRAY(SELECT rp.permission_code FROM panel_role_permission rp
                                         WHERE rp.role_code = r.code ORDER BY 1) AS permissions,
                                  EXISTS (SELECT 1 FROM panel_role_permission rp JOIN panel_permission p
                                            ON p.code = rp.permission_code
                                           WHERE rp.role_code = r.code AND p.global_only) AS global_only
                           FROM panel_role r ORDER BY r.code""")
            roles = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT code, description, global_only FROM panel_permission ORDER BY code")
            perms = [dict(r) for r in cur.fetchall()]
        return {"roles": roles, "permissions": perms}

    @router.get("/sessions")
    def list_sessions(user_id: Optional[int] = Query(None), active: bool = Query(True),
                      ctx: AuthContext = Depends(manage)) -> dict:
        conds, params = [], []
        if user_id is not None:
            conds.append("s.user_id = %s")
            params.append(user_id)
        if active:
            conds.append("s.revoked_at IS NULL AND s.expires_at > NOW() "
                         "AND s.last_seen_at > NOW() - make_interval(secs => %s)")
            params.append(auth.s.session_idle_seconds)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with _Tx() as cur:
            cur.execute(f"""SELECT s.id, s.user_id, u.username, s.created_at, s.last_seen_at, s.expires_at, s.ip,
                                   s.user_agent, s.revoked_at, s.revoked_reason, (s.id = %s) AS current,
                                   u.is_superadmin
                            FROM panel_session s JOIN panel_user u ON u.id = s.user_id {where}
                            ORDER BY s.last_seen_at DESC LIMIT 500""", (ctx.session_id or 0, *params))
            rows = cur.fetchall()
        return {"items": [dict(r, created_at=iso(r["created_at"]), last_seen_at=iso(r["last_seen_at"]),
                               expires_at=iso(r["expires_at"]), revoked_at=iso(r["revoked_at"])) for r in rows]}

    @router.post("/sessions/{session_id}/revoke")
    def revoke_session(session_id: int, request: Request, ctx: AuthContext = Depends(manage)) -> dict:
        with _Tx() as cur:
            cur.execute("""SELECT u.is_superadmin FROM panel_session s JOIN panel_user u ON u.id = s.user_id
                           WHERE s.id = %s""", (session_id,))
            t = cur.fetchone()
            if not t:
                raise not_found("Sesión")
            if session_id != ctx.session_id:
                protect_superadmin(ctx, bool(t["is_superadmin"]))
            n = auth.revoke_sessions(session_id=session_id, reason="admin", cur=cur)
            auth.audit(ctx, action="sessions.revoke", ip=client_ip(request), status_code=200, target_type="session",
                       target_id=str(session_id), cur=cur)
        return {"status": "ok", "revoked": n}

    # ── Auditoría ───────────────────────────────────────────────────────────
    @router.get("/audit")
    def list_audit(
        group_id: Optional[int] = Query(None), actor: Optional[str] = Query(None, max_length=100),
        action: Optional[str] = Query(None, max_length=120), status: Optional[str] = Query(None, pattern="^(ok|error)$"),
        since: Optional[datetime] = Query(None), until: Optional[datetime] = Query(None),
        limit: int = Query(300, ge=1, le=2000), ctx: AuthContext = Depends(auth.perm("audit.view")),
    ) -> dict:
        sql, params = ctx.scope_sql("a.group_id", "audit.view")
        conds = [sql]
        if group_id is not None:
            conds.append("a.group_id = %s")
            params.append(group_id)
        if actor:
            conds.append("a.actor_name ILIKE %s")
            params.append(f"%{actor}%")
        if action:
            conds.append("a.action ILIKE %s")
            params.append(f"%{action}%")
        if status == "ok":
            conds.append("COALESCE(a.status_code, 200) < 400")
        elif status == "error":
            conds.append("a.status_code >= 400")
        if since:
            conds.append("a.at >= %s")
            params.append(since)
        if until:
            conds.append("a.at <= %s")
            params.append(until)
        with _Tx() as cur:
            cur.execute(f"""SELECT a.id, a.at, a.actor_user_id, a.actor_name, a.auth_kind, a.action, a.target_type,
                                   a.target_id, a.group_id, g.name AS group_name, a.status_code, a.details, a.ip
                            FROM panel_audit_log a LEFT JOIN client_group g ON g.id = a.group_id
                            WHERE {' AND '.join(conds)} ORDER BY a.id DESC LIMIT %s""", (*params, limit))
            rows = cur.fetchall()
        return {"items": [dict(r, at=iso(r["at"])) for r in rows]}

    return router
