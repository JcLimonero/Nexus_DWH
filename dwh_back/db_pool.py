"""
db_pool.py — Pool de conexiones PostgreSQL para el backend
──────────────────────────────────────────────────────────
Antes cada petición abría (y cerraba) su propia conexión. Ahora `get_connection()`
devuelve una conexión PRESTADA de un pool acotado (psycopg2 ThreadedConnectionPool):

  * El contrato no cambia: el código llama `conn.close()` como siempre; el proxy
    devuelve la conexión al pool en lugar de cerrarla (idempotente).
  * Al devolverla se hace ROLLBACK de lo pendiente y `DISCARD ALL` (autocommit):
    ningún estado de sesión (SET, advisory locks de sesión, tablas temporales,
    prepared statements) pasa de un préstamo a otro.
  * Si el pool está agotado se espera hasta `timeout` segundos (semáforo); si no
    hay conexión libre se lanza psycopg2.OperationalError → los handlers ya lo
    traducen a 503 ("BD de configuración no disponible").
  * Conexiones rotas o cerradas se descartan (no vuelven al pool).

Configuración ([database]): pool_min (5: conexiones ociosas que se REUTILIZAN; psycopg2
cierra las que excedan ese número al devolverlas), pool_max (20: tope simultáneo),
pool_timeout_seconds (10), connect_timeout_seconds (5), application_name.
"""

import threading
from typing import Any, Dict, Optional

import psycopg2
from psycopg2 import pool as pg_pool


class PooledConnection:
    """Proxy de una conexión psycopg2: close() la devuelve al pool."""

    __slots__ = ("_conn", "_pool", "_released")

    def __init__(self, conn: Any, owner: "BoundedPool") -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", owner)
        object.__setattr__(self, "_released", False)

    def __getattr__(self, name: str) -> Any:
        if object.__getattribute__(self, "_released"):
            raise psycopg2.InterfaceError("connection already returned to pool")
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # p. ej. conn.autocommit = True (evaluador de salud)
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def __enter__(self) -> "PooledConnection":
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)

    @property
    def closed(self) -> int:
        if object.__getattribute__(self, "_released"):
            return 1
        return object.__getattribute__(self, "_conn").closed

    def close(self) -> None:
        if object.__getattribute__(self, "_released"):
            return
        object.__setattr__(self, "_released", True)
        object.__getattribute__(self, "_pool")._release(object.__getattribute__(self, "_conn"))


class BoundedPool:
    def __init__(self, dsn_kwargs: Dict[str, Any], minconn: int = 1, maxconn: int = 20,
                 timeout: float = 10.0) -> None:
        self.maxconn = max(1, int(maxconn))
        # psycopg2 solo CONSERVA al devolverlas hasta `minconn` conexiones libres (las demás las
        # cierra): minconn = conexiones ociosas que se reutilizan (pool_min).
        self.minconn = min(self.maxconn, max(1, int(minconn)))
        self.timeout = float(timeout)
        self._kwargs = dict(dsn_kwargs)
        self._sem = threading.BoundedSemaphore(self.maxconn)
        self._lock = threading.Lock()
        self._pool: Optional[pg_pool.ThreadedConnectionPool] = None
        self.stats = {"borrowed": 0, "timeouts": 0, "discarded": 0}

    def idle(self) -> int:
        """Conexiones libres guardadas para reutilizar."""
        return len(self._pool._pool) if self._pool is not None else 0

    def _ensure(self) -> pg_pool.ThreadedConnectionPool:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    # Se crea en el primer uso (no al importar: la BD puede no responder aún);
                    # abre `minconn` conexiones que quedan para reutilizarse.
                    self._pool = pg_pool.ThreadedConnectionPool(self.minconn, self.maxconn, **self._kwargs)
        return self._pool

    def getconn(self) -> PooledConnection:
        if not self._sem.acquire(timeout=self.timeout):
            self.stats["timeouts"] += 1
            raise psycopg2.OperationalError("pool_timeout: no hay conexiones libres a la BD de configuración")
        try:
            conn = self._ensure().getconn()
            if conn.closed:
                self._ensure().putconn(conn, close=True)
                conn = self._ensure().getconn()
        except Exception:
            self._sem.release()
            raise
        self.stats["borrowed"] += 1
        return PooledConnection(conn, self)

    def _release(self, conn: Any) -> None:
        discard = bool(conn.closed)
        if not discard:
            try:
                if not conn.autocommit:
                    conn.rollback()
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("DISCARD ALL")
                conn.autocommit = False
            except Exception:  # noqa: BLE001 — conexión rota: se descarta
                discard = True
        try:
            if discard:
                self.stats["discarded"] += 1
            self._ensure().putconn(conn, close=discard)
        except Exception:  # noqa: BLE001
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._sem.release()

    def closeall(self) -> None:
        if self._pool is not None:
            self._pool.closeall()
