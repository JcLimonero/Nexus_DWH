"""
selftest.py — autodiagnóstico inofensivo del ejecutable (``--selftest``).

Comprueba que el binario trae lo necesario para trabajar: intérprete, TLS,
SQLite y los drivers de origen/DWH (psycopg2, pyodbc, pymysql, fdb), además de
``cryptography`` (verificación de actualizaciones) y, en Windows, pywin32
(servicio). No abre conexiones de red ni a bases de datos, no lee config.ini y
no escribe nada. Sirve en el CI (tras compilar) y en soporte ("¿qué drivers ODBC
ve esta máquina?").

Salida: una línea por componente (OK / FALTA / AVISO) y código 0 si todo lo
obligatorio está presente, 1 si falta algo.
"""

import importlib
import platform
import sys
from typing import Any, Callable, Dict, List, Tuple

from . import AGENT_VERSION


def _psycopg2() -> str:
    import psycopg2

    lib = getattr(psycopg2, "__libpq_version__", 0)
    return f"{psycopg2.__version__.split(' ')[0]} (libpq {lib // 10000}.{lib % 10000})"


def _pyodbc() -> str:
    import pyodbc

    try:
        drivers = pyodbc.drivers()
    except Exception:  # noqa: BLE001
        drivers = []
    return f"{pyodbc.version} · drivers ODBC visibles: {', '.join(drivers) if drivers else 'ninguno'}"


def _fdb() -> str:
    import fdb

    # fdb carga fbclient.dll/.so recién al conectar: aquí solo se verifica el módulo.
    return f"{getattr(fdb, '__version__', '?')} (requiere cliente Firebird instalado para conectar sin DSN)"


def _cryptography() -> str:
    import cryptography
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    k = Ed25519PrivateKey.generate()
    k.public_key().verify(k.sign(b"selftest"), b"selftest")
    return f"{cryptography.__version__} (Ed25519 OK)"


def _requests() -> str:
    import certifi
    import requests

    return f"{requests.__version__} · CA bundle: {'OK' if certifi.where() else 'falta'}"


def _ssl() -> str:
    import ssl

    return ssl.OPENSSL_VERSION


def _sqlite() -> str:
    import sqlite3

    c = sqlite3.connect(":memory:")
    try:
        c.execute("PRAGMA journal_mode=WAL")
    finally:
        c.close()
    return sqlite3.sqlite_version


def _simple(module: str) -> Callable[[], str]:
    def f() -> str:
        m = importlib.import_module(module)
        return str(getattr(m, "VERSION_STRING", None) or getattr(m, "__version__", "ok"))
    return f


def _pywin32() -> str:  # pragma: no cover - solo Windows
    import servicemanager  # noqa: F401
    import win32service  # noqa: F401
    import win32serviceutil  # noqa: F401

    return "ok"


def checks() -> List[Tuple[str, Callable[[], str], bool]]:
    items: List[Tuple[str, Callable[[], str], bool]] = [
        ("ssl", _ssl, True),
        ("sqlite3", _sqlite, True),
        ("requests", _requests, True),
        ("psycopg2", _psycopg2, True),
        ("pymysql", _simple("pymysql"), True),
        ("pyodbc", _pyodbc, True),
        ("fdb", _fdb, True),
        ("cryptography", _cryptography, True),
    ]
    if sys.platform == "win32":
        items.append(("pywin32", _pywin32, True))
    return items


def run(out=None) -> int:
    out = out or sys.stdout
    from .release_keys import TRUSTED_RELEASE_KEYS

    compiled = "__compiled__" in globals()
    print(f"Nexus DWH Agent {AGENT_VERSION} · Python {platform.python_version()} · "
          f"{platform.system()} {platform.release()} {platform.machine()} · "
          f"{'compilado (Nuitka)' if compiled else 'intérprete (fuentes)'}", file=out)
    failed = 0
    for name, fn, required in checks():
        try:
            print(f"OK     {name:<13} {fn()}", file=out)
        except Exception as exc:  # noqa: BLE001
            tag = "FALTA " if required else "AVISO "
            failed += 1 if required else 0
            print(f"{tag} {name:<13} {type(exc).__name__}: {str(exc)[:160]}", file=out)
    n = len(TRUSTED_RELEASE_KEYS)
    print(("OK     " if n else "AVISO  ") + f"{'release_keys':<13} {n} clave(s) de publicación confiables"
          + ("" if n else " (pendiente: las actualizaciones exigirán -AllowUnsignedManifest)"), file=out)
    if sys.platform == "win32" and compiled:  # pragma: no cover - solo Windows
        from .authenticode import authenticode_status

        print(f"INFO   {'authenticode':<13} {authenticode_status(sys.executable)}", file=out)
    print("Resultado: " + ("OK" if not failed else f"{failed} componente(s) obligatorio(s) con error"), file=out)
    return 0 if not failed else 1


def summary() -> Dict[str, Any]:
    """Versión en dict (para pruebas)."""
    res: Dict[str, Any] = {}
    for name, fn, _req in checks():
        try:
            res[name] = fn()
        except Exception as exc:  # noqa: BLE001
            res[name] = f"ERROR {type(exc).__name__}"
    return res
