"""
sanitize.py — saneamiento central de errores y textos del agente.

``sanitize_error(exc, stage)`` → ``(error_code, mensaje)``:
  * error_code estable (p. ej. SOURCE_CONNECTION_FAILED, DWH_QUERY_TIMEOUT,
    DWH_CONSTRAINT_VIOLATION, CANCELLED...) para agrupar incidencias;
  * mensaje sin SQL, sin cadenas de conexión, sin hosts/usuarios/contraseñas/
    tokens/DSN, sin valores de filas (psycopg2 ``DETAIL: Key (...)=(...)``) y
    con longitud acotada.

Se usa en logs (``RedactingFormatter``) y en los reportes a Nexus.
Los valores sensibles conocidos (credenciales recibidas de Nexus, secreto de
la instalación, tokens de config.ini) se registran en ``SECRETS`` y se
reemplazan por ``***`` en cualquier texto.
"""

import logging
import socket
import threading
from typing import Iterable, Optional, Tuple

MAX_MESSAGE_LEN = 500


class SecretRegistry:
    """Conjunto de valores sensibles a ocultar (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: Tuple[str, ...] = ()

    def add(self, *values: Optional[str]) -> None:
        with self._lock:
            cur = set(self._values)
            for v in values:
                if v is None:
                    continue
                s = str(v).strip()
                # Valores muy cortos darían falsos positivos por todo el texto.
                if len(s) >= 4:
                    cur.add(s)
            self._values = tuple(sorted(cur, key=len, reverse=True))

    def values(self) -> Tuple[str, ...]:
        return self._values

    def clear(self) -> None:
        with self._lock:
            self._values = ()


SECRETS = SecretRegistry()

from .redact_core import redact_text as _core_redact  # noqa: E402

MAX_INPUT = 16_384


def redact_text(text: Optional[str], extra: Optional[Iterable[str]] = None, *,
                max_len: Optional[int] = MAX_MESSAGE_LEN, strip_sql: bool = True) -> str:
    """
    Quita secretos conocidos (registro SECRETS + ``extra``), SQL (también
    multilínea), literales entre comillas, listas de valores, credenciales,
    IPs y datos de filas. Recorta la entrada ANTES de sanear (anti-ReDoS) y
    usa solo expresiones acotadas (ver redact_core.py).
    """
    if not text:
        return ""
    values = list(SECRETS.values())
    if extra:
        values.extend(str(v) for v in extra if v and len(str(v)) >= 4)
    return _core_redact(text, values, max_len=max_len if max_len is not None else MAX_INPUT,
                        strip_sql=strip_sql, max_input=MAX_INPUT)


# ─────────────────────────────────────────────────────────────────────────────
# Excepciones del agente
# ─────────────────────────────────────────────────────────────────────────────
class AgentError(Exception):
    """Base de errores propios (mensajes ya seguros, sin datos sensibles)."""

    code = "AGENT_ERROR"


class ConfigError(AgentError):
    code = "CONFIG_ERROR"


class Cancelled(AgentError):
    code = "CANCELLED"


class StageError(Exception):
    """Envuelve la excepción original con la etapa en la que ocurrió."""

    def __init__(self, stage: str, original: BaseException, side: str = "") -> None:
        super().__init__(f"{stage}: {type(original).__name__}")
        self.stage = stage
        self.original = original
        self.side = side  # 'SOURCE' | 'DWH' | ''


_STAGE_SIDE = {"extract": "SOURCE", "load": "DWH"}


def _pg_code(exc: BaseException) -> str:
    return str(getattr(exc, "pgcode", "") or "")


def _classify(exc: BaseException, side: str) -> str:
    prefix = f"{side}_" if side else ""
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    text = str(exc).lower()

    if isinstance(exc, Cancelled):
        return "CANCELLED"
    if isinstance(exc, AgentError):
        return exc.code
    if isinstance(exc, MemoryError):
        return "OUT_OF_MEMORY"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return prefix + "TIMEOUT"

    if module.startswith("psycopg2"):
        code = _pg_code(exc)
        if code == "57014":
            return prefix + "QUERY_TIMEOUT"
        if code == "55P03":
            return prefix + "LOCK_TIMEOUT"
        if code.startswith("23"):
            return prefix + "CONSTRAINT_VIOLATION"
        if code.startswith("22"):
            return prefix + "DATA_ERROR"
        if code.startswith("42"):
            return prefix + "SQL_ERROR"
        if code.startswith("28"):
            return prefix + "AUTH_FAILED"
        if code.startswith("3D"):
            return prefix + "DATABASE_NOT_FOUND"
        if code.startswith("08") or (name in ("OperationalError", "InterfaceError") and not code):
            # SSL/TLS: servidor sin SSL con sslmode=require, certificado no verificable, CA inválida…
            if any(k in text for k in ("does not support ssl", "certificate", "root cert", "sslmode",
                                       "ssl error", "ssl negotiation", "sslrootcert")):
                return prefix + "SSL_ERROR"
            if "password authentication failed" in text or "authentication" in text:
                return prefix + "AUTH_FAILED"
            if "timeout" in text:
                return prefix + "CONNECT_TIMEOUT"
            return prefix + "CONNECTION_FAILED"
        return prefix + "DB_ERROR"

    if module.startswith("pymysql"):
        errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else 0
        if errno in (2003, 2006, 2013, 2002):
            return prefix + "CONNECTION_FAILED"
        if errno in (1045, 1044):
            return prefix + "AUTH_FAILED"
        if errno in (3024, 1969):
            return prefix + "QUERY_TIMEOUT"
        if name == "IntegrityError":
            return prefix + "CONSTRAINT_VIOLATION"
        if name == "ProgrammingError":
            return prefix + "SQL_ERROR"
        return prefix + "DB_ERROR"

    if module.startswith("pyodbc"):
        state = str(exc.args[0]) if exc.args else ""
        detail = str(exc.args[1]).lower() if len(exc.args) > 1 else text
        if state in ("IM002", "IM003") or "can't open lib" in detail or "data source name not found" in detail:
            return prefix + "DRIVER_NOT_FOUND"
        if state in ("HYT00", "HYT01"):
            return prefix + "QUERY_TIMEOUT"
        if state.startswith("08") or name in ("OperationalError", "InterfaceError"):
            return prefix + "CONNECTION_FAILED"
        if state.startswith("28"):
            return prefix + "AUTH_FAILED"
        if state.startswith("23") or name == "IntegrityError":
            return prefix + "CONSTRAINT_VIOLATION"
        if state.startswith("42") or name == "ProgrammingError":
            return prefix + "SQL_ERROR"
        return prefix + "DB_ERROR"

    if module.startswith("fdb"):
        return prefix + "DB_ERROR"
    if isinstance(exc, (ValueError, KeyError)) and side == "":
        return "CONFIG_ERROR"
    if isinstance(exc, (ValueError, TypeError)):
        return prefix + "DATA_ERROR"
    return prefix + "UNEXPECTED_ERROR"


def sanitize_error(exc: BaseException, stage: Optional[str] = None,
                   extra_secrets: Optional[Iterable[str]] = None,
                   max_len: int = MAX_MESSAGE_LEN) -> Tuple[str, str]:
    """(error_code, mensaje saneado) para logs y reportes."""
    side = ""
    original = exc
    if isinstance(exc, StageError):
        stage = stage or exc.stage
        side = exc.side or _STAGE_SIDE.get(exc.stage, "")
        original = exc.original
    elif stage:
        side = _STAGE_SIDE.get(stage, "")
    code = _classify(original, side)
    raw = str(original) or ""
    # pyodbc: ('42S02', '[42S02] [Microsoft]... (SQLExecDirectW)') → solo el texto
    if type(original).__module__.startswith("pyodbc") and len(getattr(original, "args", ())) >= 2:
        raw = str(original.args[1])
    msg = redact_text(raw, extra_secrets, max_len=None)
    first = msg.splitlines()[0] if msg else ""
    rest = " | ".join(msg.splitlines()[1:3]) if msg and len(msg.splitlines()) > 1 else ""
    text = f"{type(original).__name__}: {first}" + (f" | {rest}" if rest else "")
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return code, text


class RedactingFormatter(logging.Formatter):
    """Formatter que sanea todo el texto de log (incluidas trazas)."""

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        return redact_text(s, max_len=8000, strip_sql=True)
