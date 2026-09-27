"""
migrate.py — migraciones de la BD de configuración (PostgreSQL)
───────────────────────────────────────────────────────────────
Mecanismo simple y ordenado:

  1. Línea base: ``schema_postgres.sql`` (idempotente). Se aplica en cada
     ejecución; sirve para BD nuevas y para BD antiguas a las que les faltan
     columnas de versiones anteriores.
  2. Migraciones: ``migrations/NNN_nombre.sql`` en orden numérico. Cada una se
     aplica UNA sola vez, en su propia transacción, y queda registrada en la
     tabla ``schema_migrations`` (versión, nombre, checksum sha256, fecha).
     Si un archivo ya aplicado cambió (checksum distinto) solo se avisa: las
     migraciones aplicadas no se reescriben; los cambios van en una nueva.

Se toma un advisory lock para que dos procesos no migren a la vez.

Uso:
    python migrate.py                 # aplica línea base + pendientes
    python migrate.py --status        # lista aplicadas / pendientes
    python migrate.py --no-baseline   # solo migraciones numeradas
    python migrate.py --config /ruta/config.ini

La conexión se toma de [database] del config.ini del backend (o del archivo
indicado en --config / variable NEXUS_CONFIG_FILE) y de las variables
NEXUS__DATABASE__* (el archivo es opcional en contenedores; DWH_README.md §23).
"""

import argparse
import hashlib
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import psycopg2

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import nexus_config as _cfg  # noqa: E402

BASELINE_FILE = os.path.join(APP_DIR, "schema_postgres.sql")
MIGRATIONS_DIR = os.path.join(APP_DIR, "migrations")
_MIGRATION_RE = re.compile(r"^(\d{3,})_([A-Za-z0-9_\-]+)\.sql$")
# Clave arbitraria fija para pg_advisory_lock (evita migraciones concurrentes).
_LOCK_KEY = 834_120_551


def default_config_path() -> str:
    return _cfg.default_config_path()


def load_db_params(config_path: Optional[str] = None) -> Dict[str, Any]:
    # config.ini opcional + variables NEXUS__DATABASE__* (DWH_README.md §23).
    ini, _info = _cfg.load_config(config_path or default_config_path())
    params = _cfg.db_connect_params(ini)
    params["connect_timeout"] = 10
    return params


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def list_migration_files(directory: str = MIGRATIONS_DIR) -> List[Tuple[str, str, str]]:
    """[(version, nombre, ruta)] ordenado por versión numérica."""
    out: List[Tuple[str, str, str]] = []
    if not os.path.isdir(directory):
        return out
    for fname in os.listdir(directory):
        m = _MIGRATION_RE.match(fname)
        if m:
            out.append((m.group(1), m.group(2), os.path.join(directory, fname)))
    out.sort(key=lambda x: int(x[0]))
    versions = [v for v, _, _ in out]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Hay dos migraciones con el mismo número de versión.")
    return out


def _ensure_table(cur: Any) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version       VARCHAR(20)  PRIMARY KEY,
            name          VARCHAR(255) NOT NULL,
            checksum      CHAR(64)     NOT NULL,
            applied_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            execution_ms  INT          NOT NULL DEFAULT 0
        )
        """
    )


def applied_versions(conn: Any) -> Dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {r[0]: r[1] for r in cur.fetchall()}


def pending_migrations(conn: Any, directory: str = MIGRATIONS_DIR) -> List[str]:
    done = applied_versions(conn)
    return [f"{v}_{n}" for v, n, _ in list_migration_files(directory) if v not in done]


def run_migrations(conn: Any, *, baseline: bool = True, directory: str = MIGRATIONS_DIR,
                   baseline_file: str = BASELINE_FILE, log=print) -> List[str]:
    """Aplica línea base + migraciones pendientes. Devuelve las versiones aplicadas."""
    applied: List[str] = []
    if conn.autocommit:
        conn.autocommit = False
    else:
        conn.commit()  # cierra una transacción abierta por el llamador
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    conn.commit()
    try:
        with conn.cursor() as cur:
            _ensure_table(cur)
        conn.commit()

        if baseline and os.path.exists(baseline_file):
            with open(baseline_file, encoding="utf-8") as fh:
                sql = fh.read()
            t0 = time.time()
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum, execution_ms)
                    VALUES ('000', 'baseline_schema_postgres', %s, %s)
                    ON CONFLICT (version) DO UPDATE
                       SET checksum = EXCLUDED.checksum, applied_at = NOW(),
                           execution_ms = EXCLUDED.execution_ms
                    """,
                    (_sha256(sql), int((time.time() - t0) * 1000)),
                )
            conn.commit()
            log("Línea base aplicada (schema_postgres.sql).")

        done = applied_versions(conn)
        for version, name, path in list_migration_files(directory):
            with open(path, encoding="utf-8") as fh:
                sql = fh.read()
            checksum = _sha256(sql)
            if version in done:
                if done[version] != checksum:
                    log(f"AVISO: la migración {version}_{name} ya aplicada cambió (checksum distinto). "
                        "No se vuelve a ejecutar; crea una migración nueva.")
                continue
            t0 = time.time()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, execution_ms) "
                        "VALUES (%s, %s, %s, %s)",
                        (version, name, checksum, int((time.time() - t0) * 1000)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                log(f"ERROR aplicando la migración {version}_{name}; se revirtió.")
                raise
            applied.append(version)
            log(f"Migración aplicada: {version}_{name}")
    finally:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            conn.commit()
        except Exception:
            pass
    return applied


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Migraciones de la BD de configuración Nexus DWH")
    parser.add_argument("--config", help="Ruta al config.ini del backend")
    parser.add_argument("--status", action="store_true", help="Solo mostrar estado")
    parser.add_argument("--no-baseline", action="store_true", help="No aplicar schema_postgres.sql")
    args = parser.parse_args(argv)

    params = load_db_params(args.config)
    conn = psycopg2.connect(**params)
    try:
        if args.status:
            done = applied_versions(conn)
            for version, name, _ in list_migration_files():
                print(f"{version}_{name}: {'aplicada' if version in done else 'PENDIENTE'}")
            if "000" in done:
                print("000 línea base: aplicada")
            return 0
        applied = run_migrations(conn, baseline=not args.no_baseline)
        print(f"Listo. Migraciones nuevas aplicadas: {len(applied)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
