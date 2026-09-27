"""
settings.py — configuración del agente (config.ini).

[nexus]
  api_url              URL base de Nexus. HTTPS obligatorio salvo localhost.
  token / group_token / agency_token
                       Tokens de ENROLAMIENTO (solo se usan si aún no hay
                       credencial de instalación). Prioridad grupo > agencia > empresa.
                       Recomendación: borrarlos del archivo tras enrolar.
  ca_bundle            Ruta a un bundle de CA propio (PEM). Vacío = CAs del sistema/certifi.
  mode                 production (defecto) | development
  allow_insecure_http  true solo en development: permite http:// a hosts no locales.
  installation_name    Nombre para el panel (defecto: nombre de la máquina).

[agent]  (todas opcionales; ver DWH_README.md)
"""

import configparser
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from .sanitize import ConfigError

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Settings:
    api_url: str = "http://127.0.0.1:8000"
    company_token: str = ""
    group_token: str = ""
    agency_token: str = ""
    ca_bundle: str = ""
    mode: str = "production"
    allow_insecure_http: bool = False
    installation_name: str = ""

    data_dir: str = ""
    log_dir: str = ""
    log_retention_days: int = 7
    credential_scope: str = "user"            # DPAPI: user | machine

    heartbeat_seconds: int = 60
    tick_seconds: float = 5.0
    config_max_age_seconds: int = 900
    http_connect_timeout: float = 10.0
    http_read_timeout: float = 30.0
    api_retry_base_seconds: float = 2.0
    api_retry_max_seconds: float = 300.0

    run_all_on_start: bool = False
    task_retry_attempts: int = 2
    task_retry_backoff_seconds: int = 60
    shutdown_grace_seconds: int = 60

    queue_max_items: int = 10000
    queue_retention_days: int = 14
    queue_backoff_base_seconds: float = 2.0
    queue_backoff_max_seconds: float = 300.0
    queue_max_server_errors: int = 10
    queue_poison_min_seconds: int = 1800
    queue_parked_retry_seconds: int = 3600

    db_connect_timeout_seconds: int = 15
    source_statement_timeout_seconds: int = 3600
    dwh_statement_timeout_seconds: int = 3600
    dwh_lock_timeout_seconds: int = 300
    fetch_chunk_rows: int = 20000

    inventory_enabled: bool = True            # inventario estructural (sección 19)
    inventory_tick_seconds: int = 60
    inventory_statement_timeout_seconds: int = 60

    watermark_clock: str = "source"           # source | agent_local | agent_utc
    watermark_overlap_seconds: int = 120
    legacy_watermark_overlap_seconds: int = 3600

    config_path: str = field(default="", repr=False)

    # ── derivadas ──
    @property
    def is_production(self) -> bool:
        return self.mode.lower() != "development"

    def enrollment_token(self) -> tuple:
        """(tipo, token) con prioridad grupo > agencia > empresa, o ('', '')."""
        if self.group_token:
            return "group", self.group_token
        if self.agency_token:
            return "agency", self.agency_token
        if self.company_token:
            return "company", self.company_token
        return "", ""

    @property
    def enrollment_file(self) -> str:
        """Token de enrolamiento de UN solo uso (lo deja el instalador del servicio; ver §21.3)."""
        return os.path.join(self.data_dir, ENROLLMENT_FILE_NAME)


ENROLLMENT_FILE_NAME = "enrollment_token.ini"


def read_enrollment_file(path: str) -> tuple:
    """
    Lee ``<data_dir>/enrollment_token.ini`` (sección [nexus], mismas claves que
    config.ini: group_token / agency_token / token). Devuelve (tipo, token) o
    ('', ''). El agente lo borra en cuanto guarda su credencial de instalación.
    """
    if not path or not os.path.isfile(path):
        return "", ""
    ini = configparser.ConfigParser()
    try:
        ini.read(path, encoding="utf-8-sig")
    except configparser.Error:
        raise ConfigError(f"{ENROLLMENT_FILE_NAME} no tiene formato INI válido.")
    tmp = Settings(group_token=_get(ini, "nexus", "group_token"), agency_token=_get(ini, "nexus", "agency_token"),
                   company_token=_get(ini, "nexus", "token"))
    return tmp.enrollment_token()


def _get(ini: configparser.ConfigParser, section: str, key: str, default: str = "") -> str:
    return ini.get(section, key, fallback=default).strip()


def _int(ini, section, key, default: int, minimum: Optional[int] = None) -> int:
    raw = _get(ini, section, key, "")
    if raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ConfigError(f"[{section}] {key} debe ser un entero.")
    if minimum is not None and val < minimum:
        raise ConfigError(f"[{section}] {key} debe ser >= {minimum}.")
    return val


def _float(ini, section, key, default: float) -> float:
    raw = _get(ini, section, key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"[{section}] {key} debe ser numérico.")


def _bool(ini, section, key, default: bool) -> bool:
    raw = _get(ini, section, key, "").lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "si", "sí", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"[{section}] {key} debe ser true/false.")


def load_settings(config_path: Optional[str] = None, data_dir: Optional[str] = None) -> Settings:
    path = config_path or os.environ.get("NEXUS_AGENT_CONFIG", "").strip() or os.path.join(APP_DIR, "config.ini")
    if not os.path.exists(path):
        raise ConfigError(f"No se encontró config.ini en: {path}")
    ini = configparser.ConfigParser()
    try:
        ini.read(path, encoding="utf-8-sig")
    except configparser.Error as exc:
        # Secciones/opciones duplicadas, encabezado faltante, etc.: es un error de
        # configuración (código 2), no una caída que dispare reinicios.
        raise ConfigError(f"config.ini no tiene formato INI válido ({type(exc).__name__}).")
    except UnicodeDecodeError:
        raise ConfigError("config.ini no está en UTF-8.")
    base_dir = os.path.dirname(os.path.abspath(path))

    s = Settings(config_path=path)
    s.api_url = _get(ini, "nexus", "api_url", s.api_url).rstrip("/")
    s.company_token = _get(ini, "nexus", "token")
    s.group_token = _get(ini, "nexus", "group_token")
    s.agency_token = _get(ini, "nexus", "agency_token")
    s.ca_bundle = _get(ini, "nexus", "ca_bundle")
    s.mode = (_get(ini, "nexus", "mode", "production") or "production").lower()
    if s.mode not in ("production", "development"):
        raise ConfigError("[nexus] mode debe ser production o development.")
    s.allow_insecure_http = _bool(ini, "nexus", "allow_insecure_http", False)
    s.installation_name = _get(ini, "nexus", "installation_name")

    s.data_dir = data_dir or _get(ini, "agent", "data_dir") or os.path.join(base_dir, "agent_data")
    s.log_dir = _get(ini, "agent", "log_dir") or os.path.join(base_dir, "logs")
    s.log_retention_days = _int(ini, "agent", "log_retention_days", 7, 1)
    s.credential_scope = (_get(ini, "agent", "credential_scope", "user") or "user").lower()
    if s.credential_scope not in ("user", "machine"):
        raise ConfigError("[agent] credential_scope debe ser user o machine.")

    s.heartbeat_seconds = _int(ini, "agent", "heartbeat_seconds", 60, 5)
    s.tick_seconds = _float(ini, "agent", "tick_seconds", 5.0)
    s.config_max_age_seconds = _int(ini, "agent", "config_max_age_seconds", 900, 30)
    s.http_connect_timeout = _float(ini, "agent", "http_connect_timeout", 10.0)
    s.http_read_timeout = _float(ini, "agent", "http_read_timeout", 30.0)
    s.api_retry_base_seconds = _float(ini, "agent", "api_retry_base_seconds", 2.0)
    s.api_retry_max_seconds = _float(ini, "agent", "api_retry_max_seconds", 300.0)

    s.run_all_on_start = _bool(ini, "agent", "run_all_on_start", False)
    s.task_retry_attempts = _int(ini, "agent", "task_retry_attempts", 2, 0)
    s.task_retry_backoff_seconds = _int(ini, "agent", "task_retry_backoff_seconds", 60, 1)
    s.shutdown_grace_seconds = _int(ini, "agent", "shutdown_grace_seconds", 60, 0)

    s.queue_max_items = _int(ini, "agent", "queue_max_items", 10000, 10)
    s.queue_retention_days = _int(ini, "agent", "queue_retention_days", 14, 1)
    s.queue_backoff_base_seconds = _float(ini, "agent", "queue_backoff_base_seconds", 2.0)
    s.queue_backoff_max_seconds = _float(ini, "agent", "queue_backoff_max_seconds", 300.0)
    s.queue_max_server_errors = _int(ini, "agent", "queue_max_server_errors", 10, 1)
    s.queue_poison_min_seconds = _int(ini, "agent", "queue_poison_min_seconds", 1800, 0)
    s.queue_parked_retry_seconds = _int(ini, "agent", "queue_parked_retry_seconds", 3600, 10)

    s.db_connect_timeout_seconds = _int(ini, "agent", "db_connect_timeout_seconds", 15, 1)
    s.source_statement_timeout_seconds = _int(ini, "agent", "source_statement_timeout_seconds", 3600, 0)
    s.dwh_statement_timeout_seconds = _int(ini, "agent", "dwh_statement_timeout_seconds", 3600, 0)
    s.dwh_lock_timeout_seconds = _int(ini, "agent", "dwh_lock_timeout_seconds", 300, 0)
    s.fetch_chunk_rows = _int(ini, "agent", "fetch_chunk_rows", 20000, 100)

    s.inventory_enabled = _bool(ini, "agent", "inventory_enabled", True)
    s.inventory_tick_seconds = _int(ini, "agent", "inventory_tick_seconds", 60, 5)
    s.inventory_statement_timeout_seconds = _int(ini, "agent", "inventory_statement_timeout_seconds", 60, 1)

    s.watermark_clock = (_get(ini, "agent", "watermark_clock", "source") or "source").lower()
    if s.watermark_clock not in ("source", "agent_local", "agent_utc"):
        raise ConfigError("[agent] watermark_clock debe ser source, agent_local o agent_utc.")
    s.watermark_overlap_seconds = _int(ini, "agent", "watermark_overlap_seconds", 120, 0)
    s.legacy_watermark_overlap_seconds = _int(ini, "agent", "legacy_watermark_overlap_seconds", 3600, 0)
    return s
