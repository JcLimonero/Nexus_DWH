"""
panel_auth.py — Usuarios del panel: autenticación, sesiones, permisos por grupo
───────────────────────────────────────────────────────────────────────────────
* Contraseñas con argon2id (argon2-cffi, parámetros RFC 9106 perfil "low memory":
  m=64 MiB, t=3, p=4). Se re-hashean solas si cambian los parámetros.
* Sesión opaca: token aleatorio de 256 bits (secrets.token_urlsafe(32)); en BD solo
  sha256(token). Vence por tiempo absoluto ([auth] session_absolute_seconds) y por
  inactividad ([auth] session_idle_seconds). Revocable (cierre de sesión, cambio o
  reinicio de contraseña, desactivación del usuario, administrador).
* Permisos por ALCANCE de grupo: panel_user_role(role, group_id NULL = todos).
  Un superadministrador tiene todos los permisos en todos los grupos.
* Aislamiento: un recurso fuera del alcance de "view" del usuario responde 404
  (igual que uno inexistente). Si se ve pero falta el permiso de la acción → 403.
* Token estático x-admin-token: solo si [admin] allow_static_token = true (por
  defecto false). Equivale a superadministrador ("token-admin") y CADA uso queda
  en panel_audit_log.

Las rutas /admin/* declaran su permiso con `Depends(auth.perm("..."))`; la prueba
test_panel_auth.py recorre app.routes y falla si una ruta /admin nueva no lo hace.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import psycopg2
import psycopg2.extras
from fastapi import HTTPException, Request

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
except ImportError:  # pragma: no cover
    PasswordHasher = None  # type: ignore

from ratelimit import BackoffLock, SlidingWindowLimiter, lock_seconds

log = logging.getLogger("nexus.auth")

# ─────────────────────────────────────────────────────────────────────────────
# Catálogo de permisos (igual que migrations/009_usuarios_permisos.sql)
# ─────────────────────────────────────────────────────────────────────────────
PERMISSIONS: Dict[str, str] = {
    "view": "Consultar (sin secretos)",
    "incident.acknowledge": "Reconocer incidencias y eventos legados",
    "incident.close_queue": "Cerrar incidencias de cola local",
    "structure.acknowledge": "Dar por entendido cambios estructurales",
    "structure.reclassify": "Reclasificar cambios estructurales",
    "inventory.approve_baseline": "Aprobar/reiniciar línea base",
    "inventory.configure": "Configurar bases monitoreadas",
    "inventory.view_definitions": "Ver SQL de vistas",
    "credentials.manage": "Administrar credenciales, tokens, instalaciones y canales",
    "config.manage": "Administrar configuración (grupos, empresas, agencias, catálogo, tareas)",
    "audit.view": "Consultar auditoría del panel",
    "users.manage": "Administrar usuarios, roles y sesiones",
}
GLOBAL_ONLY_PERMISSIONS = {"users.manage"}

# Marcadores para rutas que no exigen un permiso concreto.
PUBLIC = "public"                # /admin/auth/login
AUTHENTICATED = "authenticated"  # /admin/auth/me, logout, change-password, whoami

STATIC_ACTOR = "token-admin"

# Lista corta de contraseñas comunes (se compara en minúsculas, sin espacios).
COMMON_PASSWORDS = {
    "123456789012", "1234567890123", "qwertyuiop12", "password1234", "password12345", "contraseña123",
    "contrasena123", "contrasena1234", "administrador", "administrator", "admin1234567", "adminadmin12",
    "qwerty123456", "123456789abc", "abc123456789", "iloveyou1234", "passw0rd1234", "welcome12345",
    "bienvenido123", "bienvenido1234", "letmein12345", "changeme1234", "cambiame1234", "nexus1234567",
    "nexusdwh1234", "dealersolutions", "superadmin12", "000000000000", "111111111111", "123123123123",
    "1q2w3e4r5t6y", "qazwsxedcrfv", "zaq12wsxcde3", "mexico123456", "password!234", "p@ssw0rd1234",
    "p@ssword1234", "trustno11234", "sunshine1234", "football1234", "monkey123456", "dragon123456",
    "master123456", "shadow123456", "12345678910", "abcdefghijkl", "asdfghjkl123", "qwertyqwerty",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(v)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def not_found(what: str = "Registro") -> HTTPException:
    return err(404, "not_found", f"{what} no encontrado.")


def forbidden(permission: str) -> HTTPException:
    return err(403, "permission_required",
               f"Sin permiso: se requiere «{PERMISSIONS.get(permission, permission)}».", permission=permission)


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AuthSettings:
    session_absolute_seconds: int = 12 * 3600
    session_idle_seconds: int = 30 * 60
    max_failed_attempts: int = 5
    lockout_base_seconds: int = 30
    lockout_max_seconds: int = 3600
    ip_max_failures: int = 30
    ip_window_seconds: int = 900
    password_min_length: int = 12
    allow_static_token: bool = False
    static_token: str = ""
    # Proxies de confianza (p. ej. el servidor del panel Next en la misma máquina): solo de
    # ellos (y con panel_proxy_key) se acepta x-nexus-client-ip para conocer la IP real del
    # usuario (límite por IP). Direcciones o redes CIDR (contenedores: 10.0.0.0/8,172.16.0.0/12).
    trusted_proxies: Tuple[str, ...] = ("127.0.0.1", "::1")
    # Secreto compartido con el servidor del panel (DWH_PANEL_PROXY_KEY): solo con él se
    # acepta la IP del usuario que envía el panel (x-nexus-client-ip).
    panel_proxy_key: str = ""

    def is_trusted_proxy(self, ip: str) -> bool:
        """``ip`` coincide con una entrada de ``trusted_proxies`` (dirección exacta o red CIDR)."""
        if not ip:
            return False
        if ip in self.trusted_proxies:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for entry in self.trusted_proxies:
            try:
                net = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if addr.version == net.version and addr in net:
                return True
        return False

    @classmethod
    def from_ini(cls, ini: Any, static_token: str = "") -> "AuthSettings":
        g = lambda k, d: ini.getint("auth", k, fallback=d)  # noqa: E731
        return cls(
            session_absolute_seconds=max(300, g("session_absolute_seconds", 12 * 3600)),
            session_idle_seconds=max(60, g("session_idle_seconds", 30 * 60)),
            max_failed_attempts=max(1, g("max_failed_attempts", 5)),
            lockout_base_seconds=max(1, g("lockout_base_seconds", 30)),
            lockout_max_seconds=max(1, g("lockout_max_seconds", 3600)),
            ip_max_failures=max(1, g("ip_max_failures", 30)),
            ip_window_seconds=max(1, g("ip_window_seconds", 900)),
            password_min_length=max(8, g("password_min_length", 12)),
            allow_static_token=ini.getboolean("admin", "allow_static_token", fallback=False),
            static_token=(static_token or "").strip(),
            trusted_proxies=tuple(p.strip() for p in ini.get("auth", "trusted_proxies",
                                                              fallback="127.0.0.1,::1").split(",") if p.strip()),
            panel_proxy_key=(ini.get("auth", "panel_proxy_key", fallback="").strip()
                             or os.environ.get("NEXUS_PANEL_PROXY_KEY", "").strip()),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Contexto de la petición
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AuthContext:
    user_id: Optional[int]
    username: str
    display_name: str
    is_superadmin: bool
    auth_kind: str                      # session | static_token
    must_change_password: bool = False
    session_id: Optional[int] = None
    session_expires_at: Optional[datetime] = None
    # permiso -> None (todos los grupos) o conjunto de group_id
    grants: Dict[str, Optional[Set[int]]] = field(default_factory=dict)
    # Grupo del recurso afectado (para la auditoría)
    audit_group: Optional[int] = None
    audit_target: Optional[str] = None

    @property
    def actor(self) -> str:
        return self.username

    def has_global(self, perm: str) -> bool:
        return self.is_superadmin or (perm in self.grants and self.grants[perm] is None)

    def has_any(self, perm: str) -> bool:
        if self.is_superadmin:
            return True
        g = self.grants.get(perm, set())
        return g is None or bool(g)

    def can(self, perm: str, group_id: Optional[int]) -> bool:
        if self.has_global(perm):
            return True
        if group_id is None:
            return False
        g = self.grants.get(perm)
        return bool(g) and int(group_id) in g  # type: ignore[operator]

    def groups(self, perm: str = "view") -> Optional[List[int]]:
        """None = todos los grupos; lista (posiblemente vacía) = solo esos."""
        if self.has_global(perm):
            return None
        return sorted(self.grants.get(perm) or set())

    def scope_sql(self, column: str, perm: str = "view") -> Tuple[str, List[Any]]:
        """Fragmento WHERE para limitar al alcance (column = expresión del group_id)."""
        gs = self.groups(perm)
        if gs is None:
            return "TRUE", []
        return f"{column} = ANY(%s)", [gs]

    def check(self, perm: str, group_ids: Any, what: str = "Registro", mode: str = "all") -> None:
        """
        Visibilidad primero (sin "view" sobre NINGUNO de los grupos del recurso → 404,
        igual que si no existiera); luego el permiso de la acción (→ 403).
        group_ids: un id, None (recurso sin grupo: solo alcance global) o lista.
        mode="all": la acción requiere el permiso en TODOS los grupos del recurso.
        """
        gl: List[Optional[int]] = list(group_ids) if isinstance(group_ids, (list, tuple, set)) else [group_ids]
        if not gl:
            gl = [None]
        if not any(self.can("view", g) for g in gl):
            raise not_found(what)
        first = next((g for g in gl if g is not None), None)
        if self.audit_group is None:
            self.audit_group = first
        if perm == "view":
            return
        ok = all(self.can(perm, g) for g in gl) if mode == "all" else any(self.can(perm, g) for g in gl)
        if not ok:
            raise forbidden(perm)

    def permissions_out(self) -> Dict[str, Any]:
        if self.is_superadmin:
            return {p: "all" for p in PERMISSIONS}
        out: Dict[str, Any] = {}
        for p, g in self.grants.items():
            out[p] = "all" if g is None else sorted(g)
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Política de contraseñas
# ─────────────────────────────────────────────────────────────────────────────
def password_problems(password: str, username: str, min_length: int, email: Optional[str] = None) -> List[str]:
    p = password or ""
    problems: List[str] = []
    if len(p) < min_length:
        problems.append(f"debe tener al menos {min_length} caracteres")
    if len(p) > 256:
        problems.append("no puede tener más de 256 caracteres")
    low = p.lower().replace(" ", "")
    u = (username or "").lower()
    if u and (low == u or u in low):
        problems.append("no puede contener el nombre de usuario")
    # Tampoco la parte local del correo (antes de la @): p. ej. "jlimon" en jlimon@dominio.com.
    local = (email or "").split("@", 1)[0].strip().lower()
    if local and local != u and (low == local or local in low):
        problems.append("no puede contener la parte local de su correo")
    if low in COMMON_PASSWORDS or (low and len(set(low)) <= 2):
        problems.append("es demasiado común o predecible")
    return problems


# ─────────────────────────────────────────────────────────────────────────────
# Servicio
# ─────────────────────────────────────────────────────────────────────────────
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
# Validación de correo pragmática (no exhaustiva RFC 5322): local@dominio.tld.
EMAIL_RE = re.compile(r"^[^@\s]{1,200}@[^@\s]{1,190}\.[^@\s]{2,24}$")


class AuthService:
    def __init__(self, *, get_connection: Callable[[], Any], settings: AuthSettings) -> None:
        if PasswordHasher is None:
            raise RuntimeError("Falta la dependencia argon2-cffi (pip install -r requirements_postgres.txt).")
        self.get_connection = get_connection
        self.s = settings
        self.ph = PasswordHasher()
        # Hash de referencia para usuarios inexistentes (tiempo de respuesta similar).
        self._dummy_hash = self.ph.hash(secrets.token_urlsafe(24))
        self.ip_failures = SlidingWindowLimiter(settings.ip_max_failures, settings.ip_window_seconds)
        self.unknown_users = BackoffLock(settings.max_failed_attempts, settings.lockout_base_seconds,
                                         settings.lockout_max_seconds)

    def client_ip(self, request: Request) -> str:
        """
        IP del usuario. X-Forwarded-For NUNCA se usa (lo controla el cliente). Si la petición viene
        de un proxy de confianza (el servidor del panel) con la clave compartida correcta
        (x-nexus-proxy-key = [auth] panel_proxy_key), se usa x-nexus-client-ip ("" = desconocida).
        En cualquier otro caso, la dirección del socket.
        """
        peer = request.client.host if request.client else ""
        key = request.headers.get("x-nexus-proxy-key", "")
        if self.s.is_trusted_proxy(peer) and self.s.panel_proxy_key and key \
                and hmac.compare_digest(key.encode("utf-8"), self.s.panel_proxy_key.encode("utf-8")):
            raw = request.headers.get("x-nexus-client-ip", "").strip()
            try:
                return str(ipaddress.ip_address(raw))[:45]
            except ValueError:
                return ""
        return peer

    def ip_limit_key(self, ip: str) -> str:
        """
        Clave del límite por IP. IP desconocida, compartida (un proxy de confianza, p. ej. el panel
        sin clave) o loopback → "" (sin límite por IP; queda el bloqueo por usuario): nunca se
        bloquea a todos los usuarios por los fallos de otros.
        """
        if not ip or self.s.is_trusted_proxy(ip):
            return ""
        try:
            if ipaddress.ip_address(ip).is_loopback:
                return ""
        except ValueError:
            return ""
        return ip

    # ── BD ──────────────────────────────────────────────────────────────────
    def _conn(self) -> Any:
        try:
            return self.get_connection()
        except psycopg2.OperationalError:
            raise err(503, "config_db_unavailable", "No se pudo conectar a la BD de configuración.")

    # ── Hash ────────────────────────────────────────────────────────────────
    def hash_password(self, password: str) -> str:
        return self.ph.hash(password)

    def verify_password(self, stored: Optional[str], password: str) -> bool:
        try:
            return self.ph.verify(stored or self._dummy_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        except Exception:  # noqa: BLE001
            return False

    # ── Permisos ────────────────────────────────────────────────────────────
    @staticmethod
    def load_grants(cur: Any, user_id: int) -> Dict[str, Optional[Set[int]]]:
        cur.execute(
            """SELECT rp.permission_code AS perm, ur.group_id, p.global_only
               FROM panel_user_role ur
               JOIN panel_role_permission rp ON rp.role_code = ur.role_code
               JOIN panel_permission p ON p.code = rp.permission_code
               WHERE ur.user_id = %s""", (user_id,))
        grants: Dict[str, Optional[Set[int]]] = {}
        for r in cur.fetchall():
            perm, gid, global_only = r["perm"], r["group_id"], r["global_only"]
            if gid is None:
                grants[perm] = None
            elif global_only:
                continue  # users.manage solo vale con alcance global
            elif grants.get(perm, set()) is not None:
                grants.setdefault(perm, set()).add(int(gid))  # type: ignore[union-attr]
        return grants

    # ── Autenticación por petición ──────────────────────────────────────────
    @staticmethod
    def _session_token(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.headers.get("x-session-token", "").strip()

    def authenticate(self, request: Request) -> AuthContext:
        cached = getattr(request.state, "auth", None)
        if isinstance(cached, AuthContext):
            return cached
        token = self._session_token(request)
        static = request.headers.get("x-admin-token", "")
        ctx: Optional[AuthContext] = None
        if token:
            if len(token) > 200:
                raise err(401, "session_invalid", "Sesión no válida o expirada.")
            ctx = self._session_ctx(token)
        elif static:
            if not (self.s.allow_static_token and self.s.static_token):
                raise err(401, "static_token_disabled",
                          "El token estático de administrador está deshabilitado ([admin] allow_static_token).")
            if not hmac.compare_digest(static.encode("utf-8"), self.s.static_token.encode("utf-8")):
                raise err(401, "invalid_token", "Token de administrador no válido.")
            ctx = AuthContext(user_id=None, username=STATIC_ACTOR, display_name="Token de administrador (break-glass)",
                              is_superadmin=True, auth_kind="static_token")
        if ctx is None:
            raise err(401, "session_invalid", "Sesión no válida o expirada.")
        request.state.auth = ctx
        request.state.audit = {**(getattr(request.state, "audit", None) or {}),
                               "auth_kind": "panel" if ctx.auth_kind == "session" else "admin"}
        return ctx

    def _session_ctx(self, token: str) -> AuthContext:
        conn = self._conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """UPDATE panel_session s SET last_seen_at = NOW()
                   FROM panel_user u
                   WHERE s.token_hash = %s AND u.id = s.user_id AND s.revoked_at IS NULL
                     AND s.expires_at > NOW() AND s.last_seen_at > NOW() - make_interval(secs => %s)
                     AND u.is_active
                   RETURNING s.id AS session_id, s.expires_at, u.id, u.username, u.display_name,
                             u.is_superadmin, u.must_change_password""",
                (sha256_hex(token), self.s.session_idle_seconds))
            r = cur.fetchone()
            if not r:
                conn.rollback()
                raise err(401, "session_invalid", "Sesión no válida o expirada.")
            grants = self.load_grants(cur, r["id"])
            conn.commit()
        finally:
            conn.close()
        return AuthContext(user_id=r["id"], username=r["username"], display_name=r["display_name"] or r["username"],
                           is_superadmin=bool(r["is_superadmin"]), auth_kind="session",
                           must_change_password=bool(r["must_change_password"]), session_id=r["session_id"],
                           session_expires_at=r["expires_at"], grants=grants)

    # ── Dependencias FastAPI ────────────────────────────────────────────────
    def perm(self, code: str, *, global_only: bool = False) -> Callable[[Request], AuthContext]:
        if code not in PERMISSIONS:
            raise ValueError(f"Permiso desconocido: {code}")
        need_global = global_only or code in GLOBAL_ONLY_PERMISSIONS

        def dependency(request: Request) -> AuthContext:
            ctx = self.authenticate(request)
            if ctx.must_change_password:
                raise err(403, "password_change_required", "Debe cambiar su contraseña antes de continuar.")
            ok = ctx.has_global(code) if need_global else ctx.has_any(code)
            if not ok:
                raise forbidden(code)
            return ctx

        dependency._nexus_permission = code  # type: ignore[attr-defined]
        dependency._nexus_global = need_global  # type: ignore[attr-defined]
        return dependency

    def authenticated(self, *, allow_password_change: bool = True) -> Callable[[Request], AuthContext]:
        def dependency(request: Request) -> AuthContext:
            ctx = self.authenticate(request)
            if ctx.must_change_password and not allow_password_change:
                raise err(403, "password_change_required", "Debe cambiar su contraseña antes de continuar.")
            return ctx

        dependency._nexus_permission = AUTHENTICATED  # type: ignore[attr-defined]
        return dependency

    @staticmethod
    def public() -> Callable[[], None]:
        def dependency() -> None:
            return None

        dependency._nexus_permission = PUBLIC  # type: ignore[attr-defined]
        return dependency

    # ── Login / logout / contraseña ─────────────────────────────────────────
    def login(self, email: str, password: str, ip: str, user_agent: str) -> Dict[str, Any]:
        em = (email or "").strip().lower()[:255]
        pw = password or ""
        if len(pw) > 1024:
            pw = pw[:1024]  # nunca se hashea algo enorme (DoS); igual fallará
        ipk = self.ip_limit_key(ip)
        wait = self.ip_failures.blocked(ipk)
        if wait:
            self.audit(None, action="auth.login_blocked", ip=ip, status_code=429,
                       details={"reason": "ip_rate_limit"})
            raise err(429, "too_many_attempts", "Demasiados intentos fallidos desde esta dirección. Intente más tarde.",
                      retry_after=int(wait))
        conn = self._conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # Solo por correo (minúsculas); un usuario sin correo (email IS NULL) nunca coincide
            # aquí y por lo tanto no puede iniciar sesión hasta que se le asigne uno.
            cur.execute("""SELECT id, username, display_name, password_hash, is_active, is_superadmin,
                                  must_change_password, failed_attempts, locked_until
                           FROM panel_user WHERE email IS NOT NULL AND lower(email) = %s FOR UPDATE""", (em,))
            u = cur.fetchone()
            now = utcnow()
            locked = None
            if u and u["locked_until"] and u["locked_until"] > now:
                locked = (u["locked_until"] - now).total_seconds()
            elif not u:
                locked = self.unknown_users.locked_for(em)
            if locked:
                conn.rollback()
                # Misma conexión (nunca se piden dos a la vez del pool).
                self.audit(None, action="auth.login_locked", actor_user_id=u["id"] if u else None,
                           actor_name=u["username"] if u else "", ip=ip, status_code=429, cur=cur)
                conn.commit()
                raise err(429, "account_locked", "Demasiados intentos fallidos. Intente más tarde.",
                          retry_after=int(locked) + 1)
            ok = self.verify_password(u["password_hash"] if u else None, pw)
            if not u or not ok or not u["is_active"]:
                self.ip_failures.hit(ipk)
                reason = "unknown_email" if not u else ("inactive" if ok and not u["is_active"] else "bad_password")
                if u:
                    fails = int(u["failed_attempts"]) + 1
                    secs = lock_seconds(fails, self.s.max_failed_attempts, self.s.lockout_base_seconds,
                                        self.s.lockout_max_seconds)
                    cur.execute("""UPDATE panel_user SET failed_attempts = %s, last_failed_at = NOW(),
                                          locked_until = CASE WHEN %s > 0 THEN NOW() + make_interval(secs => %s)
                                                              ELSE locked_until END
                                   WHERE id = %s""", (fails, secs, secs, u["id"]))
                else:
                    self.unknown_users.fail(em)
                self.audit(None, action="auth.login_failed", actor_user_id=u["id"] if u else None,
                           actor_name=u["username"] if u else "", ip=ip, status_code=401,
                           details={"reason": reason}, cur=cur)
                conn.commit()
                raise err(401, "invalid_credentials", "Usuario o contraseña incorrectos.")
            # Éxito
            if self.ph.check_needs_rehash(u["password_hash"]):
                cur.execute("UPDATE panel_user SET password_hash = %s WHERE id = %s",
                            (self.hash_password(pw), u["id"]))
            token = secrets.token_urlsafe(32)
            expires = now + timedelta(seconds=self.s.session_absolute_seconds)
            cur.execute("""UPDATE panel_user SET failed_attempts = 0, locked_until = NULL, last_login_at = NOW()
                           WHERE id = %s""", (u["id"],))
            cur.execute("""INSERT INTO panel_session (token_hash, user_id, expires_at, ip, user_agent)
                           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                        (sha256_hex(token), u["id"], expires, ip[:45], (user_agent or "")[:200]))
            sid = cur.fetchone()["id"]
            grants = self.load_grants(cur, u["id"])
            conn.commit()
        finally:
            conn.close()
        ctx = AuthContext(user_id=u["id"], username=u["username"], display_name=u["display_name"] or u["username"],
                          is_superadmin=bool(u["is_superadmin"]), auth_kind="session",
                          must_change_password=bool(u["must_change_password"]), session_id=sid,
                          session_expires_at=expires, grants=grants)
        self.audit(ctx, action="auth.login", ip=ip, status_code=200, target_type="session", target_id=str(sid))
        return {"token": token, "expires_at": iso(expires), "idle_timeout_seconds": self.s.session_idle_seconds,
                **self.profile(ctx)}

    def logout(self, ctx: AuthContext, ip: str) -> None:
        if ctx.session_id is None:
            return
        self.revoke_sessions(session_id=ctx.session_id, reason="logout")
        self.audit(ctx, action="auth.logout", ip=ip, status_code=200, target_type="session",
                   target_id=str(ctx.session_id))

    def revoke_sessions(self, *, user_id: Optional[int] = None, session_id: Optional[int] = None,
                        except_session: Optional[int] = None, reason: str = "revoked", cur: Any = None) -> int:
        def run(c: Any) -> int:
            conds, params = ["revoked_at IS NULL"], []
            if user_id is not None:
                conds.append("user_id = %s")
                params.append(user_id)
            if session_id is not None:
                conds.append("id = %s")
                params.append(session_id)
            if except_session is not None:
                conds.append("id <> %s")
                params.append(except_session)
            c.execute(f"UPDATE panel_session SET revoked_at = NOW(), revoked_reason = %s WHERE {' AND '.join(conds)}",
                      (reason[:40], *params))
            return c.rowcount

        if cur is not None:
            return run(cur)
        conn = self._conn()
        try:
            n = run(conn.cursor())
            conn.commit()
            return n
        finally:
            conn.close()

    def change_password(self, ctx: AuthContext, current: str, new: str, ip: str) -> None:
        if ctx.user_id is None:
            raise err(409, "not_a_user", "El token estático no tiene contraseña.")
        conn = self._conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT username, email, password_hash FROM panel_user WHERE id = %s FOR UPDATE",
                       (ctx.user_id,))
            u = cur.fetchone()
            if not u or not self.verify_password(u["password_hash"], (current or "")[:1024]):
                conn.rollback()
                self.ip_failures.hit(self.ip_limit_key(ip))
                self.audit(ctx, action="auth.change_password_failed", ip=ip, status_code=401, cur=cur)
                conn.commit()
                raise err(401, "invalid_credentials", "La contraseña actual no es correcta.")
            problems = password_problems(new, u["username"], self.s.password_min_length, email=u["email"])
            if not problems and self.verify_password(u["password_hash"], new):
                problems.append("debe ser distinta de la actual")
            if problems:
                conn.rollback()
                raise err(422, "weak_password", "La nueva contraseña " + "; ".join(problems) + ".", problems=problems)
            cur.execute("""UPDATE panel_user SET password_hash = %s, must_change_password = FALSE,
                                  password_changed_at = NOW(), updated_at = NOW() WHERE id = %s""",
                        (self.hash_password(new), ctx.user_id))
            # Se cierran las demás sesiones del usuario (la actual sigue).
            self.revoke_sessions(user_id=ctx.user_id, except_session=ctx.session_id, reason="password_changed",
                                 cur=cur)
            conn.commit()
        finally:
            conn.close()
        ctx.must_change_password = False
        self.audit(ctx, action="auth.change_password", ip=ip, status_code=200, target_type="user",
                   target_id=str(ctx.user_id))

    def profile(self, ctx: AuthContext) -> Dict[str, Any]:
        return {
            "user": {"id": ctx.user_id, "username": ctx.username, "display_name": ctx.display_name,
                     "is_superadmin": ctx.is_superadmin, "auth_kind": ctx.auth_kind},
            "must_change_password": ctx.must_change_password,
            "permissions": ctx.permissions_out(),
            "session_expires_at": iso(ctx.session_expires_at),
            "idle_timeout_seconds": self.s.session_idle_seconds,
        }

    # ── Auditoría ───────────────────────────────────────────────────────────
    def audit(self, ctx: Optional[AuthContext], *, action: str, ip: str = "", status_code: Optional[int] = None,
              target_type: Optional[str] = None, target_id: Optional[str] = None, group_id: Optional[int] = None,
              details: Optional[Dict[str, Any]] = None, actor_user_id: Optional[int] = None,
              actor_name: Optional[str] = None, cur: Any = None) -> None:
        """Nunca rompe la petición; los detalles no llevan cuerpos ni secretos."""
        if ctx is not None:
            ctx.audit_target = "__done__"  # el middleware no la duplica
        row = (
            ctx.user_id if ctx else actor_user_id,
            ((ctx.username if ctx else actor_name) or "")[:100],
            (ctx.auth_kind if ctx else "anonymous")[:20],
            action[:120], (target_type or None) and target_type[:60], (target_id or None) and str(target_id)[:80],
            group_id if group_id is not None else (ctx.audit_group if ctx else None),
            status_code, audit_details_json(details), (ip or "")[:45],
        )
        sql = """INSERT INTO panel_audit_log (actor_user_id, actor_name, auth_kind, action, target_type, target_id,
                                              group_id, status_code, details, ip)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)"""
        try:
            if cur is not None:
                cur.execute(sql, row)
                return
            conn = self.get_connection()
            try:
                conn.cursor().execute(sql, row)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo registrar panel_audit_log: %s", type(exc).__name__)
            # Nunca se pierde el registro: fila mínima (sin detalles) en una conexión propia.
            try:
                conn = self.get_connection()
                try:
                    conn.cursor().execute(sql, (*row[:8], json.dumps({"details_error": type(exc).__name__}), row[9]))
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc2:  # noqa: BLE001
                log.error("panel_audit_log: tampoco se pudo registrar la fila mínima: %s", type(exc2).__name__)

    def record_request(self, ctx: AuthContext, method: str, route_path: str, path_params: Dict[str, Any],
                       status_code: int, ip: str) -> None:
        """Auditoría automática (middleware): mutaciones /admin/* y todo uso del token estático."""
        parts = [p for p in route_path.split("/") if p]
        target_type = parts[1] if len(parts) > 1 else None
        target_id = None
        for v in path_params.values():
            target_id = str(v)
            break
        self.audit(ctx, action=f"{method} {route_path}"[:120], ip=ip, status_code=status_code,
                   target_type=target_type, target_id=target_id,
                   details={"static_token": True} if ctx.auth_kind == "static_token" else None)


AUDIT_DETAILS_MAX = 4000


def audit_details_json(details: Optional[Dict[str, Any]], limit: int = AUDIT_DETAILS_MAX) -> str:
    """
    JSON de los detalles de auditoría, SIEMPRE válido y ≤ limit caracteres. Cortar el texto
    produciría JSON inválido (el ::jsonb fallaría y se perdería el registro): si no cabe, se
    recortan las listas y, como último recurso, se guardan solo las claves.
    """
    d = details or {}
    s = json.dumps(d, default=str)
    if len(s) <= limit:
        return s
    for cap in (100, 20, 5, 0):
        shrunk: Dict[str, Any] = {"truncated": True, "original_length": len(s)}
        for k, v in d.items():
            if isinstance(v, (list, tuple)) and len(v) > cap:
                shrunk[k] = list(v)[:cap]
                shrunk[k + "_total"] = len(v)
            elif isinstance(v, str) and len(v) > 500:
                shrunk[k] = v[:500] + "…"
            else:
                shrunk[k] = v
        s2 = json.dumps(shrunk, default=str)
        if len(s2) <= limit:
            return s2
    return json.dumps({"truncated": True, "original_length": len(s), "keys": [str(k)[:60] for k in d][:40]})


# ─────────────────────────────────────────────────────────────────────────────
# Resolución del grupo de un recurso (aislamiento)
# ─────────────────────────────────────────────────────────────────────────────
def _one(cur: Any, sql: str, params: Sequence[Any]) -> Optional[Dict[str, Any]]:
    cur.execute(sql, tuple(params))
    r = cur.fetchone()
    if r is None:
        return None
    return r if isinstance(r, dict) else {"group_id": r[0]}


def group_of(cur: Any, kind: str, row_id: Any, what: str = "Registro") -> Optional[int]:
    """group_id del recurso; 404 si no existe."""
    sql = {
        "group": "SELECT id AS group_id FROM client_group WHERE id = %s",
        "company": "SELECT group_id FROM company WHERE id = %s",
        "agency": "SELECT c.group_id FROM agency a JOIN company c ON c.id = a.company_id WHERE a.id = %s",
        "object": "SELECT c.group_id FROM object_catalog o JOIN company c ON c.id = o.company_id WHERE o.id = %s",
        "task": """SELECT c.group_id FROM agency_task t JOIN agency a ON a.id = t.agency_id
                   JOIN company c ON c.id = a.company_id WHERE t.id = %s""",
        "installation": "SELECT group_id FROM installation WHERE id = %s",
        "incident": "SELECT group_id FROM incident WHERE id = %s",
        "channel": "SELECT group_id FROM notification_channel WHERE id = %s",
        "event": "SELECT group_id FROM client_events WHERE id = %s",
    }[kind]
    r = _one(cur, sql, (str(row_id) if kind == "installation" else row_id,))
    if r is None:
        raise not_found(what)
    return r["group_id"]


def groups_of_mdb(cur: Any, mdb_id: int, what: str = "Base monitoreada") -> List[Optional[int]]:
    """Grupos de una base monitoreada: su grupo + los grupos vinculados (DWH compartido)."""
    cur.execute("""SELECT md.group_id,
                          ARRAY(SELECT DISTINCT l.group_id FROM monitored_database_link l
                                 WHERE l.monitored_database_id = md.id) AS linked
                   FROM monitored_database md WHERE md.id = %s""", (mdb_id,))
    r = cur.fetchone()
    if r is None:
        raise not_found(what)
    gid, linked = (r["group_id"], r["linked"]) if isinstance(r, dict) else (r[0], r[1])
    out: List[Optional[int]] = []
    for g in [gid, *(linked or [])]:
        if g is not None and g not in out:
            out.append(int(g))
    return out or [None]


def groups_of_change(cur: Any, change_id: int) -> Tuple[int, List[Optional[int]]]:
    cur.execute("SELECT monitored_database_id FROM structural_change WHERE id = %s", (change_id,))
    r = cur.fetchone()
    if r is None:
        raise not_found("Cambio estructural")
    mdb_id = r["monitored_database_id"] if isinstance(r, dict) else r[0]
    return mdb_id, groups_of_mdb(cur, mdb_id, "Cambio estructural")


def mdb_scope_sql(ctx: AuthContext, alias: str = "md", perm: str = "view") -> Tuple[str, List[Any]]:
    """Base monitoreada visible si su grupo o algún grupo vinculado está en el alcance."""
    gs = ctx.groups(perm)
    if gs is None:
        return "TRUE", []
    return (f"""({alias}.group_id = ANY(%s) OR EXISTS (SELECT 1 FROM monitored_database_link l__
                   WHERE l__.monitored_database_id = {alias}.id AND l__.group_id = ANY(%s)))""", [gs, gs])


def valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match(username or ""))


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


def derive_username_from_email(email: str) -> str:
    """Parte local del correo (antes de la @), saneada a las reglas de USERNAME_RE (3-64: minúsculas,
    números, punto, guion o guion bajo, empezando por letra/número)."""
    local = (email or "").split("@", 1)[0].strip().lower()
    local = re.sub(r"[^a-z0-9._-]", "", local)
    local = local.lstrip("._-")
    if not local:
        local = "usuario"
    while len(local) < 3:
        local += "0"
    return local[:64]


def dependency_permissions(dependant: Any) -> List[str]:
    """Permisos declarados (recursivo) en un Dependant de FastAPI — usado por las pruebas."""
    out: List[str] = []
    stack = [dependant]
    while stack:
        d = stack.pop()
        call = getattr(d, "call", None)
        p = getattr(call, "_nexus_permission", None)
        if p:
            out.append(p)
        stack.extend(getattr(d, "dependencies", []) or [])
    return out


def iter_ids(values: Iterable[Any]) -> List[int]:
    return [int(v) for v in values if v is not None]
