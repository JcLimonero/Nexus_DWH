"""
nexus_config.py — carga de la configuración del backend (config.ini + variables de entorno)
──────────────────────────────────────────────────────────────────────────────────────────
Configuración 12-factor para contenedores (Coolify/Docker, ver DWH_README.md §23):

* Se lee ``config.ini`` si existe (``NEXUS_CONFIG_FILE`` o ``<carpeta del backend>/config.ini``).
  **No es obligatorio**: sin archivo se usan los valores por defecto del código.
* Después, cada variable de entorno ``NEXUS__<SECCION>__<CLAVE>`` define o **sobrescribe** el valor
  ``[seccion] clave`` (sección y clave en minúsculas). Ejemplos::

      NEXUS__DATABASE__HOST=host.docker.internal
      NEXUS__DATABASE__PASSWORD=...            (secreto)
      NEXUS__AUTH__TRUSTED_PROXIES=10.0.0.0/8,172.16.0.0/12
      NEXUS__CONNECTION_TEST__ENABLED=true

  El separador es el doble guion bajo; una clave puede llevar guiones bajos simples
  (``POOL_MAX``, ``CONNECTION_TEST``). Una variable **vacía se ignora** (así una variable
  declarada sin valor en el orquestador no borra el valor del archivo ni rompe un entero).
* Las variables específicas que ya existían siguen funcionando igual (``NEXUS_CONFIG_SECRET_KEY``,
  ``NEXUS_ADMIN_TOKEN``, ``NEXUS_CORS_ORIGINS``, ``NEXUS_PANEL_PROXY_KEY``): se usan cuando la
  clave correspondiente no tiene valor (ni en el archivo ni en ``NEXUS__...``).

Nunca se registran valores: ``load_config`` devuelve solo los NOMBRES ``seccion.clave`` aplicados.
"""

import configparser
import os
import re
import sys
from typing import Dict, List, Mapping, Optional, Tuple

ENV_PREFIX = "NEXUS__"
_PART_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_]*[A-Za-z0-9])?$")

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def default_config_path(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("NEXUS_CONFIG_FILE") or "").strip() or os.path.join(APP_DIR, "config.ini")


def parse_env_overrides(environ: Optional[Mapping[str, str]] = None) -> Tuple[Dict[Tuple[str, str], str], List[str]]:
    """
    ``{(seccion, clave): valor}`` a partir de ``NEXUS__SECCION__CLAVE`` y la lista de nombres de
    variables ignoradas por mal formadas (sin valores).
    """
    env = os.environ if environ is None else environ
    out: Dict[Tuple[str, str], str] = {}
    invalid: List[str] = []
    for name in sorted(env):
        if not name.upper().startswith(ENV_PREFIX):
            continue
        value = env[name]
        if value is None or value.strip() == "":
            continue
        parts = name[len(ENV_PREFIX):].split("__")
        if len(parts) != 2 or not all(_PART_RE.match(p) for p in parts):
            invalid.append(name)
            continue
        out[(parts[0].lower(), parts[1].lower())] = value.strip()
    return out, invalid


def apply_env_overrides(ini: configparser.ConfigParser,
                        environ: Optional[Mapping[str, str]] = None) -> Tuple[List[str], List[str]]:
    """Aplica ``NEXUS__*`` sobre ``ini``. Devuelve (claves aplicadas ``seccion.clave``, variables inválidas)."""
    overrides, invalid = parse_env_overrides(environ)
    applied: List[str] = []
    for (section, key), value in overrides.items():
        if not ini.has_section(section):
            ini.add_section(section)
        # Con la interpolación por defecto de ConfigParser, '%' es especial: se escapa para que
        # una contraseña con '%' llegue literal.
        ini.set(section, key, value.replace("%", "%%"))
        applied.append(f"{section}.{key}")
    return applied, invalid


def load_config(config_path: Optional[str] = None,
                environ: Optional[Mapping[str, str]] = None) -> Tuple[configparser.ConfigParser, Dict[str, object]]:
    """
    Lee el archivo (si existe) y aplica las variables ``NEXUS__*``. Devuelve el ``ConfigParser`` y
    un resumen SIN valores: ``{"path", "file_loaded", "applied", "invalid"}``.
    """
    path = config_path or default_config_path(environ)
    ini = configparser.ConfigParser()
    loaded = bool(ini.read(path, encoding="utf-8-sig")) if path else False
    applied, invalid = apply_env_overrides(ini, environ)
    return ini, {"path": path, "file_loaded": loaded, "applied": applied, "invalid": invalid}


def db_connect_params(ini: configparser.ConfigParser) -> Dict[str, object]:
    """Parámetros de psycopg2 de ``[database]`` (compartidos por el backend, migrate.py y manage_users.py)."""
    params: Dict[str, object] = {
        "host": ini.get("database", "host", fallback="127.0.0.1"),
        "port": ini.getint("database", "port", fallback=5432),
        "dbname": ini.get("database", "db", fallback="nexus_config"),
        "user": ini.get("database", "user", fallback="postgres"),
        "password": ini.get("database", "password", fallback=""),
    }
    # TLS hacia PostgreSQL (disable | allow | prefer | require | verify-ca | verify-full). Vacío =
    # comportamiento por defecto de libpq (prefer).
    sslmode = ini.get("database", "sslmode", fallback="").strip()
    if sslmode:
        params["sslmode"] = sslmode
    sslrootcert = ini.get("database", "sslrootcert", fallback="").strip()
    if sslrootcert:
        params["sslrootcert"] = sslrootcert
    return params
