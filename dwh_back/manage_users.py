"""
manage_users.py — Alta inicial y mantenimiento de usuarios del panel (CLI)
──────────────────────────────────────────────────────────────────────────
El acceso al panel es por CORREO. No se entrega ningún usuario ni contraseña
por defecto. El primer superadministrador se crea así (en el servidor del backend):

    python manage_users.py create-superadmin --email jlimon@nexusqtech.com
        → pide la contraseña dos veces (no se muestra ni se registra), o bien
    NEXUS_NEW_USER_PASSWORD=... python manage_users.py create-superadmin --email jlimon@nexusqtech.com \\
        --password-env NEXUS_NEW_USER_PASSWORD

El usuario (identificador interno/visible) se deriva del correo si no se indica
--username explícitamente.

Otros comandos:
    python manage_users.py list                                          (muestra correo)
    python manage_users.py set-email --username X --email nuevo@dominio  (o --user-id N)
    python manage_users.py reset-password --email X | --username X      (obliga a cambiarla al entrar)
    python manage_users.py unlock --email X | --username X
    python manage_users.py deactivate --email X | --username X           (cierra sus sesiones)
    python manage_users.py revoke-sessions --email X | --username X

Usa el mismo config.ini que el backend (o --config / NEXUS_CONFIG_FILE; opcional con las
variables NEXUS__DATABASE__* del contenedor) y requiere las migraciones 009 y 011
aplicadas (python migrate.py). Cada acción queda en panel_audit_log con actor "cli".
"""

import argparse
import getpass
import json
import os
import sys

import psycopg2
import psycopg2.errors
import psycopg2.extras

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from nexus_config import db_connect_params, default_config_path, load_config  # noqa: E402
from panel_auth import (  # noqa: E402
    derive_username_from_email, password_problems, valid_email, valid_username,
)


def connect(config_path: str):
    # config.ini opcional + variables NEXUS__<SECCION>__<CLAVE> (contenedores, DWH_README.md §23).
    ini, info = load_config(config_path)
    if not info["file_loaded"] and not any(k.startswith("database.") for k in info["applied"]):
        sys.exit(f"No se pudo leer {config_path} ni hay variables NEXUS__DATABASE__*.")
    return psycopg2.connect(connect_timeout=10, **db_connect_params(ini)), ini


def read_password(args: argparse.Namespace, username: str, min_length: int, email: str = "") -> str:
    if args.password_env:
        pw = os.environ.get(args.password_env, "")
        if not pw:
            sys.exit(f"La variable {args.password_env} está vacía.")
    else:
        if not sys.stdin.isatty():
            sys.exit("Sin terminal interactiva: use --password-env NOMBRE_VARIABLE.")
        pw = getpass.getpass("Contraseña nueva: ")
        if getpass.getpass("Repítala: ") != pw:
            sys.exit("Las contraseñas no coinciden.")
    problems = password_problems(pw, username, min_length, email=email)
    if problems:
        sys.exit("Contraseña rechazada: " + "; ".join(problems) + ".")
    return pw


def audit(cur, action: str, target_id, details=None) -> None:
    cur.execute("""INSERT INTO panel_audit_log (actor_name, auth_kind, action, target_type, target_id, status_code, details)
                   VALUES ('cli', 'cli', %s, 'user', %s, 200, %s::jsonb)""",
                (action, str(target_id), json.dumps(details or {})))


def find_user(cur, args: argparse.Namespace):
    """Resuelve el usuario objetivo por --email, --username o --user-id (según lo que traiga `args`)."""
    email = getattr(args, "email", None)
    username = getattr(args, "username", None)
    user_id = getattr(args, "user_id", None)
    if email:
        cur.execute("SELECT id, username, email FROM panel_user WHERE lower(email) = %s", (email.strip().lower(),))
    elif username:
        cur.execute("SELECT id, username, email FROM panel_user WHERE lower(username) = %s",
                    (username.strip().lower(),))
    elif user_id:
        cur.execute("SELECT id, username, email FROM panel_user WHERE id = %s", (user_id,))
    else:
        sys.exit("Indique --email, --username o --user-id para identificar al usuario.")
    u = cur.fetchone()
    if not u:
        sys.exit("Usuario inexistente.")
    return u


def add_identify_args(parser: argparse.ArgumentParser, *, allow_user_id: bool = False) -> None:
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--email", default="", help="Correo del usuario (identifica la cuenta)")
    g.add_argument("--username", default="", help="Usuario (identifica la cuenta)")
    if allow_user_id:
        g.add_argument("--user-id", type=int, default=None, help="Id del usuario (identifica la cuenta)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Usuarios del panel Nexus DWH (acceso por correo)")
    ap.add_argument("--config", default=default_config_path())
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-superadmin", help="Crea un superadministrador")
    c.add_argument("--email", required=True, help="Correo con el que iniciará sesión")
    c.add_argument("--username", default="", help="Usuario (opcional; se deriva del correo si no se indica)")
    c.add_argument("--display-name", default="")
    c.add_argument("--password-env", default="", help="Leer la contraseña de esta variable de entorno")
    c.add_argument("--no-force-change", action="store_true",
                   help="No obligar a cambiar la contraseña en el primer inicio de sesión")
    r = sub.add_parser("reset-password", help="Reinicia la contraseña de un usuario existente")
    add_identify_args(r)
    r.add_argument("--password-env", default="")
    for name, help_ in (("unlock", "Quita el bloqueo por intentos fallidos"),
                       ("deactivate", "Desactiva la cuenta y cierra sus sesiones"),
                       ("revoke-sessions", "Cierra todas las sesiones activas")):
        add_identify_args(sub.add_parser(name, help=help_))
    se = sub.add_parser("set-email", help="Cambia el correo (de acceso) de un usuario existente")
    add_identify_args(se, allow_user_id=True)
    se.add_argument("--new-email", dest="new_email", required=True, help="Nuevo correo")
    sub.add_parser("list", help="Lista los usuarios (incluye correo)")
    args = ap.parse_args()

    from argon2 import PasswordHasher
    ph = PasswordHasher()
    conn, ini = connect(args.config)
    min_length = max(8, ini.getint("auth", "password_min_length", fallback=12))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if args.cmd == "list":
            cur.execute("""SELECT id, username, email, display_name, is_active, is_superadmin, must_change_password,
                                  locked_until, last_login_at FROM panel_user ORDER BY username""")
            for u in cur.fetchall():
                print(f"{u['id']:>4}  {u['username']:<24} {u['email'] or '(sin correo)':<32} "
                      f"activo={u['is_active']!s:<5} superadmin={u['is_superadmin']!s:<5} "
                      f"cambiar_contraseña={u['must_change_password']!s:<5} "
                      f"último_acceso={u['last_login_at'] or '-'}")
            return
        if args.cmd == "create-superadmin":
            email = args.email.strip().lower()
            if not valid_email(email):
                sys.exit("Correo no válido.")
            cur.execute("SELECT 1 FROM panel_user WHERE lower(email) = %s", (email,))
            if cur.fetchone():
                sys.exit("Ya existe un usuario con ese correo.")
            uname = (args.username or "").strip().lower() or derive_username_from_email(email)
            if not valid_username(uname):
                sys.exit("Usuario no válido (3-64: minúsculas, números, punto, guion, guion bajo).")
            cur.execute("SELECT 1 FROM panel_user WHERE lower(username) = %s", (uname,))
            if cur.fetchone():
                sys.exit(f"Ese usuario ('{uname}') ya existe; indique --username explícito.")
            pw = read_password(args, uname, min_length, email=email)
            cur.execute("""INSERT INTO panel_user (username, display_name, email, password_hash, is_superadmin,
                                                   must_change_password, created_by)
                           VALUES (%s, %s, %s, %s, TRUE, %s, 'cli') RETURNING id""",
                        (uname, args.display_name or uname, email, ph.hash(pw), not args.no_force_change))
            uid = cur.fetchone()["id"]
            audit(cur, "cli.create_superadmin", uid, {"username": uname, "email": email})
            print(f"Superadministrador '{uname}' <{email}> creado (id {uid}).")
        elif args.cmd == "set-email":
            u = find_user(cur, args)
            new_email = args.new_email.strip().lower()
            if not valid_email(new_email):
                sys.exit("Correo no válido.")
            cur.execute("SELECT 1 FROM panel_user WHERE lower(email) = %s AND id <> %s", (new_email, u["id"]))
            if cur.fetchone():
                sys.exit("Ya existe otro usuario con ese correo.")
            cur.execute("UPDATE panel_user SET email = %s, updated_at = NOW() WHERE id = %s", (new_email, u["id"]))
            audit(cur, "cli.set_email", u["id"], {"username": u["username"], "email": new_email})
            print(f"Hecho: correo de '{u['username']}' actualizado a {new_email}.")
        else:
            u = find_user(cur, args)
            uid, uname, email = u["id"], u["username"], u["email"]
            if args.cmd == "reset-password":
                pw = read_password(args, uname, min_length, email=email)
                cur.execute("""UPDATE panel_user SET password_hash = %s, must_change_password = TRUE,
                                      failed_attempts = 0, locked_until = NULL, password_changed_at = NOW()
                               WHERE id = %s""", (ph.hash(pw), uid))
            elif args.cmd == "unlock":
                cur.execute("UPDATE panel_user SET failed_attempts = 0, locked_until = NULL WHERE id = %s", (uid,))
            elif args.cmd == "deactivate":
                cur.execute("UPDATE panel_user SET is_active = FALSE WHERE id = %s", (uid,))
            if args.cmd in ("reset-password", "deactivate", "revoke-sessions"):
                cur.execute("""UPDATE panel_session SET revoked_at = NOW(), revoked_reason = 'cli'
                               WHERE user_id = %s AND revoked_at IS NULL""", (uid,))
            audit(cur, "cli." + args.cmd.replace("-", "_"), uid)
            print(f"Hecho: {args.cmd} {uname}.")
        conn.commit()
    except psycopg2.errors.UndefinedTable:
        sys.exit("Falta la migración 009 (ejecute: python migrate.py).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
