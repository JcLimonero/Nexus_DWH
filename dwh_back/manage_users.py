"""
manage_users.py — Alta inicial y mantenimiento de usuarios del panel (CLI)
──────────────────────────────────────────────────────────────────────────
No se entrega ningún usuario ni contraseña por defecto. El primer
superadministrador se crea así (en el servidor del backend):

    python manage_users.py create-superadmin --username jlimon
        → pide la contraseña dos veces (no se muestra ni se registra), o bien
    NEXUS_NEW_USER_PASSWORD=... python manage_users.py create-superadmin --username jlimon --password-env NEXUS_NEW_USER_PASSWORD

Otros comandos:
    python manage_users.py list
    python manage_users.py reset-password --username X      (obliga a cambiarla al entrar)
    python manage_users.py unlock --username X
    python manage_users.py deactivate --username X           (cierra sus sesiones)
    python manage_users.py revoke-sessions --username X

Usa el mismo config.ini que el backend (o --config / NEXUS_CONFIG_FILE; opcional con las
variables NEXUS__DATABASE__* del contenedor) y requiere
la migración 009 aplicada (python migrate.py). Cada acción queda en panel_audit_log
con actor "cli".
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
from panel_auth import password_problems, valid_username  # noqa: E402


def connect(config_path: str):
    # config.ini opcional + variables NEXUS__<SECCION>__<CLAVE> (contenedores, DWH_README.md §23).
    ini, info = load_config(config_path)
    if not info["file_loaded"] and not any(k.startswith("database.") for k in info["applied"]):
        sys.exit(f"No se pudo leer {config_path} ni hay variables NEXUS__DATABASE__*.")
    return psycopg2.connect(connect_timeout=10, **db_connect_params(ini)), ini


def read_password(args: argparse.Namespace, username: str, min_length: int) -> str:
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
    problems = password_problems(pw, username, min_length)
    if problems:
        sys.exit("Contraseña rechazada: " + "; ".join(problems) + ".")
    return pw


def audit(cur, action: str, target_id, details=None) -> None:
    cur.execute("""INSERT INTO panel_audit_log (actor_name, auth_kind, action, target_type, target_id, status_code, details)
                   VALUES ('cli', 'cli', %s, 'user', %s, 200, %s::jsonb)""",
                (action, str(target_id), json.dumps(details or {})))


def main() -> None:
    ap = argparse.ArgumentParser(description="Usuarios del panel Nexus DWH")
    ap.add_argument("--config", default=default_config_path())
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-superadmin", help="Crea un superadministrador")
    c.add_argument("--username", required=True)
    c.add_argument("--display-name", default="")
    c.add_argument("--email", default="")
    c.add_argument("--password-env", default="", help="Leer la contraseña de esta variable de entorno")
    c.add_argument("--no-force-change", action="store_true",
                   help="No obligar a cambiar la contraseña en el primer inicio de sesión")
    r = sub.add_parser("reset-password")
    r.add_argument("--username", required=True)
    r.add_argument("--password-env", default="")
    for name in ("unlock", "deactivate", "revoke-sessions"):
        sub.add_parser(name).add_argument("--username", required=True)
    sub.add_parser("list")
    args = ap.parse_args()

    from argon2 import PasswordHasher
    ph = PasswordHasher()
    conn, ini = connect(args.config)
    min_length = max(8, ini.getint("auth", "password_min_length", fallback=12))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if args.cmd == "list":
            cur.execute("""SELECT id, username, display_name, is_active, is_superadmin, must_change_password,
                                  locked_until, last_login_at FROM panel_user ORDER BY username""")
            for u in cur.fetchall():
                print(f"{u['id']:>4}  {u['username']:<24} activo={u['is_active']!s:<5} "
                      f"superadmin={u['is_superadmin']!s:<5} cambiar_contraseña={u['must_change_password']!s:<5} "
                      f"último_acceso={u['last_login_at'] or '-'}")
            return
        uname = args.username.strip().lower()
        if args.cmd == "create-superadmin":
            if not valid_username(uname):
                sys.exit("Usuario no válido (3-64: minúsculas, números, punto, guion, guion bajo).")
            cur.execute("SELECT 1 FROM panel_user WHERE lower(username) = %s", (uname,))
            if cur.fetchone():
                sys.exit("Ese usuario ya existe.")
            pw = read_password(args, uname, min_length)
            cur.execute("""INSERT INTO panel_user (username, display_name, email, password_hash, is_superadmin,
                                                   must_change_password, created_by)
                           VALUES (%s, %s, %s, %s, TRUE, %s, 'cli') RETURNING id""",
                        (uname, args.display_name or uname, args.email or None, ph.hash(pw),
                         not args.no_force_change))
            uid = cur.fetchone()["id"]
            audit(cur, "cli.create_superadmin", uid, {"username": uname})
            print(f"Superadministrador '{uname}' creado (id {uid}).")
        else:
            cur.execute("SELECT id FROM panel_user WHERE lower(username) = %s", (uname,))
            u = cur.fetchone()
            if not u:
                sys.exit("Usuario inexistente.")
            uid = u["id"]
            if args.cmd == "reset-password":
                pw = read_password(args, uname, min_length)
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
