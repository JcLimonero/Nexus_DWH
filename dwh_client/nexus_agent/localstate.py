"""
localstate.py — estado local persistente del agente (SQLite).

Archivo ``<data_dir>/agent_state.db`` con:

  outbox              cola persistente de reportes para Nexus. SOLO metadatos
                      (ids, estados, conteos, códigos y mensajes ya saneados):
                      nunca SQL, secretos ni datos de negocio.
  dead_letter         reportes rechazados de forma definitiva (4xx), para diagnóstico.
  meta                agent_seq (secuencia monotónica por instalación, persistida),
                      contadores de desbordamiento.
  pending_checkpoint  watermark de cargas YA confirmadas en el DWH mientras Nexus
                      no las confirma (reconciliación DWH ↔ Nexus).
  task_schedule       agenda por tarea (un reinicio no dispara todo de golpe).

Política de la cola:
  * Envío estrictamente en orden de agent_seq (un inicio nunca llega después
    de su fin). Reintentos con backoff exponencial + jitter y tope.
  * Saturación (queue_max_items): primero se compactan inicios cuya ejecución
    ya tiene fin encolado, luego eventos informativos, luego éxitos, luego
    inicios. Los FALLOS nunca se descartan en silencio: se admiten hasta 2×
    el máximo y, si aun así no caben, se descartan los más antiguos contando
    cada descarte y encolando un evento ``queue_overflow`` con los totales.
  * Retención (queue_retention_days): lo más viejo se purga y también se cuenta.
"""

import json
import os
import random
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

# Claves que jamás deben viajar en un reporte (defensa en profundidad).
FORBIDDEN_KEYS = {"extract_sql", "sql", "query", "password", "secret", "token", "dsn",
                  "host", "username", "user", "create_table_sql", "create_constraint_sql",
                  "rows", "row", "values", "static_columns"}
MAX_PAYLOAD_BYTES = 16_384
MAX_DEAD_LETTER = 500


@dataclass
class OutboxItem:
    event_id: str
    seq: int
    type: str
    execution_id: Optional[str]
    task_id: Optional[int]
    critical: bool
    payload: Dict[str, Any]
    created_at: float
    attempts: int
    next_attempt_at: float


def _check_payload(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                raise ValueError(f"Clave prohibida en payload de reporte: {path}{k}")
            _check_payload(v, f"{path}{k}.")
    elif isinstance(obj, list):
        for v in obj:
            _check_payload(v, path)


class LocalState:
    def __init__(self, path: str, *, max_items: int = 10000, retention_days: int = 14,
                 backoff_base: float = 2.0, backoff_max: float = 300.0,
                 parked_retry_seconds: float = 3600.0,
                 clock: Callable[[], float] = time.time,
                 rng: Callable[[float, float], float] = random.uniform) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.max_items = max_items
        self.hard_max = max_items * 2
        self.retention_seconds = retention_days * 86400
        self.backoff_base = backoff_base
        self._parked_retry_seconds = float(parked_retry_seconds)
        self.backoff_max = backoff_max
        self.clock = clock
        self.rng = rng
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._init_schema()
        self._migrate_schema()
        self._depth = self._count()
        self.dead_letter_total_cached = int(self._meta_get("dead_letter_total", "0") or 0)
        self.parked_total_cached = int(self._meta_get("parked_total", "0") or 0)
        # Copias sin bloqueo para el heartbeat (lecturas atómicas de int).
        self.overflow_total_cached = int(self._meta_get("overflow_total", "0") or 0)
        self.last_seq_cached = int(self._meta_get("agent_seq", "0") or 0)
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    # ── esquema ─────────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id        TEXT PRIMARY KEY,
                    seq             INTEGER NOT NULL UNIQUE,
                    type            TEXT NOT NULL,
                    execution_id    TEXT,
                    task_id         INTEGER,
                    critical        INTEGER NOT NULL DEFAULT 0,
                    is_success      INTEGER NOT NULL DEFAULT 0,
                    payload         TEXT NOT NULL,
                    created_at      REAL NOT NULL,
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_exec ON outbox(execution_id);
                CREATE TABLE IF NOT EXISTS dead_letter (
                    event_id   TEXT PRIMARY KEY,
                    seq        INTEGER,
                    type       TEXT,
                    payload    TEXT,
                    created_at REAL,
                    failed_at  REAL,
                    status     INTEGER,
                    reason     TEXT
                );
                CREATE TABLE IF NOT EXISTS pending_checkpoint (
                    execution_id TEXT PRIMARY KEY,
                    task_id      INTEGER NOT NULL,
                    watermark    TEXT NOT NULL,
                    kind         TEXT NOT NULL,
                    started_at   REAL NOT NULL,
                    created_at   REAL NOT NULL,
                    confirmed    INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_pc_task ON pending_checkpoint(task_id);
                CREATE TABLE IF NOT EXISTS running_execution (
                    execution_id  TEXT PRIMARY KEY,
                    task_id       INTEGER NOT NULL,
                    payload       TEXT NOT NULL,
                    created_at    REAL NOT NULL,
                    phase         TEXT NOT NULL DEFAULT 'running'
                );
                CREATE TABLE IF NOT EXISTS task_schedule (
                    task_id          INTEGER PRIMARY KEY,
                    next_due_at      REAL NOT NULL,
                    attempt          INTEGER NOT NULL DEFAULT 1,
                    schedule_seconds INTEGER,
                    last_started_at  REAL,
                    last_finished_at REAL,
                    last_status      TEXT
                );
                """
            )

    def _migrate_schema(self) -> None:
        """Columnas agregadas después de la primera versión del archivo local."""
        with self._lock:
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(outbox)").fetchall()}
            if "server_errors" not in cols:
                self._db.execute("ALTER TABLE outbox ADD COLUMN server_errors INTEGER NOT NULL DEFAULT 0")
            if "first_server_error_at" not in cols:
                self._db.execute("ALTER TABLE outbox ADD COLUMN first_server_error_at REAL")
            if "parked" not in cols:
                self._db.execute("ALTER TABLE outbox ADD COLUMN parked INTEGER NOT NULL DEFAULT 0")
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(running_execution)").fetchall()}
            if "phase" not in cols:
                self._db.execute("ALTER TABLE running_execution ADD COLUMN phase TEXT NOT NULL DEFAULT 'running'")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── meta / secuencia ────────────────────────────────────────────────────
    def _meta_get(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _meta_set(self, key: str, value: Any) -> None:
        self._db.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def _meta_incr(self, key: str, n: int = 1) -> int:
        v = int(self._meta_get(key, "0") or 0) + n
        self._meta_set(key, v)
        return v

    def next_seq(self) -> int:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                v = self._meta_incr("agent_seq")
                self._db.execute("COMMIT")
                self.last_seq_cached = v
                return v
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def current_seq(self) -> int:
        with self._lock:
            return int(self._meta_get("agent_seq", "0") or 0)

    @property
    def depth(self) -> int:
        """Profundidad de la cola (lectura sin bloqueo, para el heartbeat)."""
        return self._depth

    def overflow_total(self) -> int:
        with self._lock:
            return int(self._meta_get("overflow_total", "0") or 0)

    def overflow_counters(self) -> Dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM meta WHERE key LIKE 'overflow_%'").fetchall()
            return {r["key"]: int(r["value"]) for r in rows}

    def _count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    # ── encolado ────────────────────────────────────────────────────────────
    def enqueue(self, event_type: str, payload: Dict[str, Any], *, critical: bool = False,
                is_success: bool = False, execution_id: Optional[str] = None,
                task_id: Optional[int] = None,
                checkpoint: Optional[Tuple[str, str, float]] = None,
                running: Optional[str] = None) -> Tuple[str, int]:
        """
        Encola un reporte. Asigna event_id y agent_seq (en la misma transacción).
        ``checkpoint`` = (watermark_iso, kind, started_at_epoch): se guarda como
        pendiente de confirmar en la MISMA transacción que el reporte de fin.
        ``running`` = "start" registra la ejecución como en curso (para detectar
        tras un reinicio las que murieron a medias); "end" la da por terminada.
        """
        _check_payload(payload)
        with self._lock:
            now = self.clock()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                accepted = self._make_room(critical)
                seq = self._meta_incr("agent_seq")
                event_id = str(uuid.uuid4())
                body = dict(payload, event_id=event_id, agent_seq=seq)
                raw = json.dumps(body, separators=(",", ":"), default=str)
                if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                    raise ValueError("Payload de reporte demasiado grande.")
                if accepted:
                    self._db.execute(
                        "INSERT INTO outbox (event_id, seq, type, execution_id, task_id, critical, is_success, "
                        "payload, created_at, attempts, next_attempt_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                        (event_id, seq, event_type, execution_id, task_id, int(critical), int(is_success),
                         raw, now, now),
                    )
                else:
                    # Cola llena solo de fallos: el reporte nuevo (no crítico) se descarta
                    # contándolo; el checkpoint local se guarda igual (los datos ya están en el DWH).
                    self._meta_incr("overflow_dropped_rejected_new")
                    self._meta_incr("overflow_total")
                    self._meta_set("overflow_pending_report", 1)
                    event_id = ""
                if running == "start" and execution_id and task_id is not None:
                    self._db.execute(
                        "INSERT OR REPLACE INTO running_execution (execution_id, task_id, payload, created_at) "
                        "VALUES (?,?,?,?)", (execution_id, task_id, json.dumps(payload, default=str), now))
                elif running == "end" and execution_id:
                    self._db.execute("DELETE FROM running_execution WHERE execution_id = ?", (execution_id,))
                if checkpoint is not None and execution_id and task_id is not None:
                    wm, kind, started = checkpoint
                    self._db.execute(
                        "INSERT OR REPLACE INTO pending_checkpoint (execution_id, task_id, watermark, kind, "
                        "started_at, created_at, confirmed) VALUES (?,?,?,?,?,?,0)",
                        (execution_id, task_id, wm, kind, started, now),
                    )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._depth = self._count()
            self.last_seq_cached = seq
            self.overflow_total_cached = int(self._meta_get("overflow_total", "0") or 0)
            return event_id, seq

    def _drop(self, rows: List[sqlite3.Row], category: str) -> int:
        for r in rows:
            self._db.execute("DELETE FROM outbox WHERE event_id = ?", (r["event_id"],))
        if rows:
            self._meta_incr(f"overflow_dropped_{category}", len(rows))
            self._meta_incr("overflow_total", len(rows))
            self._meta_set("overflow_pending_report", 1)
        return len(rows)

    def _make_room(self, critical: bool) -> bool:
        """Libera espacio según la política. Devuelve False si el nuevo reporte no cabe."""
        count = self._count()
        if count < self.max_items:
            return True
        need = count - self.max_items + 1
        # 1) inicios cuya ejecución ya tiene fin encolado (el fin crea la ejecución si falta)
        rows = self._db.execute(
            "SELECT o.event_id FROM outbox o WHERE o.type = 'execution_start' AND EXISTS ("
            " SELECT 1 FROM outbox u WHERE u.type = 'execution_update' AND u.execution_id = o.execution_id)"
            " ORDER BY o.seq LIMIT ?", (need,)).fetchall()
        need -= self._drop(rows, "start")
        if need <= 0:
            return True
        # 2) eventos informativos no críticos
        rows = self._db.execute(
            "SELECT event_id FROM outbox WHERE type = 'agent_event' AND critical = 0 ORDER BY seq LIMIT ?",
            (need,)).fetchall()
        need -= self._drop(rows, "info")
        if need <= 0:
            return True
        # 3) éxitos (el checkpoint queda en pending_checkpoint local)
        rows = self._db.execute(
            "SELECT event_id FROM outbox WHERE type = 'execution_update' AND is_success = 1 ORDER BY seq LIMIT ?",
            (need,)).fetchall()
        need -= self._drop(rows, "success")
        if need <= 0:
            return True
        # 4) inicios restantes
        rows = self._db.execute(
            "SELECT event_id FROM outbox WHERE type = 'execution_start' ORDER BY seq LIMIT ?", (need,)).fetchall()
        need -= self._drop(rows, "start")
        if need <= 0:
            return True
        # 5) solo quedan críticos (fallos / overflow).
        if not critical:
            return False
        count = self._count()
        if count < self.hard_max:
            return True  # se admiten fallos hasta 2× el máximo
        excess = count - self.hard_max + 1
        rows = self._db.execute("SELECT event_id FROM outbox ORDER BY seq LIMIT ?", (excess,)).fetchall()
        self._drop(rows, "failure")
        return True

    def take_dead_letter_report(self, min_interval: float = 60.0) -> Optional[Dict[str, int]]:
        """Contadores de reportes apartados (dead-letter) aún no avisados a Nexus."""
        with self._lock:
            if self._meta_get("dead_letter_pending_report", "0") != "1":
                return None
            last = float(self._meta_get("dead_letter_last_report_at", "0") or 0)
            now = self.clock()
            if now - last < min_interval:
                return None
            self._meta_set("dead_letter_pending_report", 0)
            self._meta_set("dead_letter_last_report_at", now)
            rows = self._db.execute("SELECT key, value FROM meta WHERE key IN ('dead_letter_total', 'parked_total') "
                                    "OR key LIKE 'dead_letter_reason_%'").fetchall()
            return {r["key"]: int(float(r["value"])) for r in rows}

    def take_overflow_report(self, min_interval: float = 60.0) -> Optional[Dict[str, int]]:
        """Si hubo descartes no reportados (y pasó min_interval), devuelve los contadores."""
        with self._lock:
            if self._meta_get("overflow_pending_report", "0") != "1":
                return None
            last = float(self._meta_get("overflow_last_report_at", "0") or 0)
            now = self.clock()
            if now - last < min_interval:
                return None
            self._meta_set("overflow_pending_report", 0)
            self._meta_set("overflow_last_report_at", now)
            rows = self._db.execute("SELECT key, value FROM meta WHERE key LIKE 'overflow_%' "
                                    "AND key NOT IN ('overflow_pending_report', 'overflow_last_report_at')").fetchall()
            return {r["key"]: int(float(r["value"])) for r in rows}

    def purge_expired(self) -> int:
        """Retención: purga reportes más viejos que queue_retention_days (contados)."""
        with self._lock:
            limit = self.clock() - self.retention_seconds
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute("SELECT event_id FROM outbox WHERE created_at < ?", (limit,)).fetchall()
                n = self._drop(rows, "expired")
                self._db.execute("DELETE FROM pending_checkpoint WHERE created_at < ? AND confirmed = 1", (limit,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._depth = self._count()
            return n

    # ── consumo ─────────────────────────────────────────────────────────────
    def _row_to_item(self, r: sqlite3.Row) -> OutboxItem:
        return OutboxItem(
            event_id=r["event_id"], seq=r["seq"], type=r["type"], execution_id=r["execution_id"],
            task_id=r["task_id"], critical=bool(r["critical"]), payload=json.loads(r["payload"]),
            created_at=r["created_at"], attempts=r["attempts"], next_attempt_at=r["next_attempt_at"],
        )

    def head(self) -> Optional[OutboxItem]:
        """
        Siguiente reporte a intentar:
          1. un reporte "estacionado" (sospechoso de veneno) cuyo reintento ya venció;
          2. si no, el primero NO estacionado en orden de agent_seq (listo o no);
          3. si no hay, el primer estacionado (para esperar su reintento).
        Los estacionados no bloquean al resto de la cola.
        """
        with self._lock:
            r = self._db.execute("SELECT * FROM outbox WHERE parked = 1 AND next_attempt_at <= ? "
                                 "ORDER BY seq LIMIT 1", (self.clock(),)).fetchone()
            if r is None:
                r = self._db.execute("SELECT * FROM outbox WHERE parked = 0 ORDER BY seq LIMIT 1").fetchone()
            if r is None:
                r = self._db.execute("SELECT * FROM outbox ORDER BY next_attempt_at LIMIT 1").fetchone()
            return self._row_to_item(r) if r else None

    def all_items(self) -> List[OutboxItem]:
        with self._lock:
            return [self._row_to_item(r) for r in self._db.execute("SELECT * FROM outbox ORDER BY seq").fetchall()]

    def mark_sent(self, item: OutboxItem) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM outbox WHERE event_id = ?", (item.event_id,))
                if item.type == "execution_update" and item.execution_id:
                    self._confirm_checkpoint(item.execution_id)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._depth = self._count()

    def _confirm_checkpoint(self, execution_id: str) -> None:
        row = self._db.execute("SELECT * FROM pending_checkpoint WHERE execution_id = ?", (execution_id,)).fetchone()
        if not row:
            return
        self._db.execute("UPDATE pending_checkpoint SET confirmed = 1 WHERE execution_id = ?", (execution_id,))
        # Se conserva solo el último confirmado por tarea (más los no confirmados).
        self._db.execute(
            "DELETE FROM pending_checkpoint WHERE task_id = ? AND confirmed = 1 AND execution_id <> ? "
            "AND watermark <= ?", (row["task_id"], execution_id, row["watermark"]))

    def backoff_delay(self, attempts: int) -> float:
        ceiling = min(self.backoff_max, self.backoff_base * (2 ** max(0, attempts - 1)))
        return self.rng(ceiling / 2.0, ceiling)

    def defer(self, item: OutboxItem, error: str, server_error: bool = False) -> float:
        """
        Aplaza con backoff. ``server_error`` = HTTP 500 exacto: se cuenta junto con
        la hora del primero. Cualquier otro error (502/503/504, red, timeout...)
        REINICIA el conteo: es una caída, no un reporte envenenado.
        """
        with self._lock:
            attempts = item.attempts + 1
            nxt = self.clock() + self.backoff_delay(attempts)
            parked = self._db.execute("SELECT parked FROM outbox WHERE event_id = ?", (item.event_id,)).fetchone()
            if parked is not None and parked[0]:
                # Un estacionado conserva su intervalo largo, no el backoff normal.
                nxt = self.clock() + self._parked_retry_seconds
            if server_error:
                self._db.execute("UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ?, "
                                 "server_errors = server_errors + 1, "
                                 "first_server_error_at = COALESCE(first_server_error_at, ?) WHERE event_id = ?",
                                 (attempts, nxt, (error or "")[:200], self.clock(), item.event_id))
            else:
                self._db.execute("UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ?, "
                                 "server_errors = 0, first_server_error_at = NULL WHERE event_id = ?",
                                 (attempts, nxt, (error or "")[:200], item.event_id))
            return nxt

    def server_error_info(self, item: OutboxItem) -> Tuple[int, Optional[float], bool]:
        """(500 consecutivos, hora del primero, estacionado)."""
        with self._lock:
            r = self._db.execute("SELECT server_errors, first_server_error_at, parked FROM outbox WHERE event_id = ?",
                                 (item.event_id,)).fetchone()
            return (int(r[0]), r[1], bool(r[2])) if r else (0, None, False)

    def park(self, item: OutboxItem, retry_seconds: float, reason: str) -> None:
        """
        Estaciona un reporte CRÍTICO sospechoso de veneno: no se descarta, deja de
        bloquear la cola y se reintenta cada ``retry_seconds``.
        """
        with self._lock:
            self._parked_retry_seconds = float(retry_seconds)
            already = self._db.execute("SELECT parked FROM outbox WHERE event_id = ?", (item.event_id,)).fetchone()
            self._db.execute("UPDATE outbox SET parked = 1, next_attempt_at = ?, last_error = ? WHERE event_id = ?",
                             (self.clock() + retry_seconds, (reason or "")[:200], item.event_id))
            if already is not None and not already[0]:
                self._meta_incr("parked_total")
                self._meta_set("dead_letter_pending_report", 1)
                self.parked_total_cached = int(self._meta_get("parked_total", "0") or 0)

    def parked_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM outbox WHERE parked = 1").fetchone()[0])

    def dead_letter(self, item: OutboxItem, status: int, reason: str) -> None:
        with self._lock:
            now = self.clock()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO dead_letter (event_id, seq, type, payload, created_at, failed_at, status, reason) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (item.event_id, item.seq, item.type, json.dumps(item.payload), item.created_at, now, status,
                     (reason or "")[:300]),
                )
                self._db.execute("DELETE FROM outbox WHERE event_id = ?", (item.event_id,))
                self._db.execute(
                    "DELETE FROM dead_letter WHERE event_id IN (SELECT event_id FROM dead_letter "
                    "ORDER BY failed_at DESC LIMIT -1 OFFSET ?)", (MAX_DEAD_LETTER,))
                total = self._meta_incr("dead_letter_total")
                self._meta_incr(f"dead_letter_reason_{(reason or 'unknown')[:40]}")
                if not (item.type == "agent_event" and item.payload.get("event_type") == "dead_letter"):
                    self._meta_set("dead_letter_pending_report", 1)
                self._db.execute("COMMIT")
                self.dead_letter_total_cached = total
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._depth = self._count()

    def dead_letter_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0])

    # ── checkpoints ─────────────────────────────────────────────────────────
    def local_watermark(self, task_id: int, reset_after_epoch: Optional[float] = None) -> Optional[Tuple[str, str]]:
        """Mayor watermark local (pendiente o confirmado) de la tarea."""
        with self._lock:
            if reset_after_epoch is not None:
                row = self._db.execute(
                    "SELECT watermark, kind FROM pending_checkpoint WHERE task_id = ? AND started_at >= ? "
                    "ORDER BY watermark DESC LIMIT 1", (task_id, reset_after_epoch)).fetchone()
            else:
                row = self._db.execute(
                    "SELECT watermark, kind FROM pending_checkpoint WHERE task_id = ? "
                    "ORDER BY watermark DESC LIMIT 1", (task_id,)).fetchone()
            return (row["watermark"], row["kind"]) if row else None

    def unconfirmed_checkpoints(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM pending_checkpoint WHERE confirmed = 0 ORDER BY created_at").fetchall()]

    def prune_checkpoints(self, task_id: int, server_watermark_iso: Optional[str]) -> None:
        """Borra checkpoints confirmados ya cubiertos por el watermark del servidor."""
        if not server_watermark_iso:
            return
        with self._lock:
            self._db.execute("DELETE FROM pending_checkpoint WHERE task_id = ? AND confirmed = 1 AND watermark <= ?",
                             (task_id, server_watermark_iso))

    def mark_committing(self, execution_id: str) -> None:
        """Marca persistente justo ANTES del COMMIT en el DWH."""
        with self._lock:
            self._db.execute("UPDATE running_execution SET phase = 'committing' WHERE execution_id = ?",
                             (execution_id,))

    def orphan_executions(self) -> List[Dict[str, Any]]:
        """Ejecuciones iniciadas que no llegaron a reportar fin (proceso muerto)."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM running_execution ORDER BY created_at").fetchall()
            return [{"execution_id": r["execution_id"], "task_id": r["task_id"], "phase": r["phase"],
                     "payload": json.loads(r["payload"])} for r in rows]

    # ── agenda ──────────────────────────────────────────────────────────────
    def get_schedule(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._db.execute("SELECT * FROM task_schedule WHERE task_id = ?", (task_id,)).fetchone()
            return dict(r) if r else None

    def set_schedule(self, task_id: int, **fields: Any) -> None:
        allowed = {"next_due_at", "attempt", "schedule_seconds", "last_started_at", "last_finished_at", "last_status"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        with self._lock:
            cur = self.get_schedule(task_id)
            if cur is None:
                base = {"next_due_at": self.clock(), "attempt": 1}
                base.update(fields)
                cols = ", ".join(["task_id", *base.keys()])
                ph = ", ".join(["?"] * (len(base) + 1))
                self._db.execute(f"INSERT INTO task_schedule ({cols}) VALUES ({ph})", (task_id, *base.values()))
            elif fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self._db.execute(f"UPDATE task_schedule SET {sets} WHERE task_id = ?", (*fields.values(), task_id))
