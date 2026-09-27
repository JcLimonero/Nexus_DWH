"""
agent.py — orquestación del agente ETL.

Hilos:
  * principal (scheduler): refresca la autorización/config con GET /agent/tasks,
    decide qué tarea toca y la entrega al worker. Nunca muere por errores
    transitorios (red, 5xx, JSON inválido): reintenta con backoff.
  * worker: ejecuta UNA tarea a la vez (etl.run_task) con timeouts de BD.
  * heartbeat: POST /agent/heartbeat cada ``heartbeat_seconds`` con su propia
    sesión HTTP; no comparte locks con el ETL (lee contadores atómicos).
  * sender: vacía la cola local (SQLite) en orden de agent_seq, con backoff.
  * inventory: inventario estructural de solo lectura de las bases que Nexus le
    asigne (lease), con su propia sesión HTTP y frecuencia independiente del ETL.
  * connection-tests: toma y ejecuta las pruebas "Probar conexión" pedidas desde el
    panel (sección 22), con su propia sesión HTTP; nunca bloquea al ETL.

Autorización con caducidad: la config (credenciales + SQL) vive SOLO en
memoria y vale ``config_max_age_seconds``. Si Nexus no responde durante más
tiempo, no se INICIAN tareas nuevas (la que está en curso termina y su reporte
queda en la cola).

Cancelación segura: al apagar se espera ``shutdown_grace_seconds`` a que la
tarea en curso termine; después se marca cancelación (se revisa entre chunks
→ ROLLBACK, estado ``interrupted``) y, si sigue bloqueada en una sentencia, se
cancela la sentencia en el driver (psycopg2 ``cancel`` / pyodbc ``cancel``).
"""

import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import AGENT_FEATURES, AGENT_VERSION
from .api import (
    ApiAuthError,
    ApiError,
    ApiForbidden,
    ApiRejected,
    ApiUnavailable,
    CredentialHolder,
    NexusApi,
    installation_info,
)
from .credstore import CredentialStore, InstallationCredential
from .destination import (
    connection_key, prune_ca_files, register_warehouse_secrets, run_connection_test, task_warehouse,
)
from .inventory import InventoryRunner
from .etl import RunContext, TaskResult, parse_watermark, register_task_secrets, run_task, watermark_iso
from .localstate import LocalState, OutboxItem
from .logsetup import get_logger
from .sanitize import SECRETS, Cancelled, ConfigError, StageError, sanitize_error
from .settings import Settings, read_enrollment_file

log = get_logger()

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_AUTH = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class FatalAgentError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class Agent:
    def __init__(self, settings: Settings, *, store: Optional[CredentialStore] = None,
                 state: Optional[LocalState] = None,
                 api_factory: Callable[[Settings, CredentialHolder], NexusApi] = NexusApi,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        os.makedirs(settings.data_dir, exist_ok=True)
        self.store = store or CredentialStore(settings.data_dir, settings.credential_scope)
        self.state = state or LocalState(
            os.path.join(settings.data_dir, "agent_state.db"), max_items=settings.queue_max_items,
            retention_days=settings.queue_retention_days, backoff_base=settings.queue_backoff_base_seconds,
            backoff_max=settings.queue_backoff_max_seconds,
            parked_retry_seconds=settings.queue_parked_retry_seconds, clock=clock,
        )
        self.holder = CredentialHolder()
        self.api_factory = api_factory
        self.api = api_factory(settings, self.holder)
        self.clock = clock

        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.fatal: Optional[FatalAgentError] = None
        self._started_mono = time.monotonic()

        self._config: Optional[Dict[str, Any]] = None
        self._config_at: Optional[float] = None
        self._config_lock = threading.Lock()
        self._stale_reported = False
        self._rotation_requested = threading.Event()

        self._work_q: "queue.Queue[Any]" = queue.Queue()
        self._busy = threading.Event()
        self._running: Dict[int, str] = {}
        self._running_lock = threading.Lock()
        self._current_ctx: Optional[RunContext] = None
        self._sender_wakeup = threading.Event()
        self._sender_paused_until = 0.0
        self._threads: List[threading.Thread] = []
        self._last_purge = 0.0
        self._sender_stop_deadline = float("inf")
        self._last_auth_warn = 0.0
        self._last_api_ok = 0.0
        self._tests_wakeup = threading.Event()
        self._test_times: List[float] = []            # inicios de pruebas (monotónico), último minuto
        self._test_last_by_target: Dict[str, float] = {}
        self.last_connection_test: Optional[Dict[str, Any]] = None

    # ═════════════════════════════════════════════════════════════════════════
    # Credencial
    # ═════════════════════════════════════════════════════════════════════════
    def ensure_credential(self, force_enroll: bool = False) -> InstallationCredential:
        if force_enroll:
            self.store.delete()
        cred = self.store.load()
        if cred is not None:
            if cred.api_url.rstrip("/") != self.settings.api_url.rstrip("/"):
                raise FatalAgentError(EXIT_CONFIG, "La credencial guardada es de otra api_url. "
                                                   "Re-enrole con --enroll si el cambio es intencional.")
            SECRETS.add(cred.secret)
            self.holder.set(cred)
            log.info("Instalación %s (credencial local cargada).", cred.installation_id)
            if os.path.exists(self.settings.enrollment_file):
                # Quedó de un enrolamiento anterior (p. ej. no se pudo borrar): ya no se necesita.
                self._discard_enrollment_file()
                log.warning("Había un token de enrolamiento de un solo uso sin consumir con credencial ya "
                            "existente: se borró (no se usa).")
            return cred

        kind, token = self.settings.enrollment_token()
        from_file = False
        if not token:
            # Token de un solo uso dejado por el instalador del servicio en la carpeta de datos
            # (así el enrolamiento lo hace la cuenta del servicio y DPAPI queda ligado a ella).
            kind, token = read_enrollment_file(self.settings.enrollment_file)
            from_file = bool(token)
        if not token:
            raise FatalAgentError(EXIT_CONFIG, "No hay credencial de instalación ni token de enrolamiento "
                                               "([nexus] group_token / agency_token / token o "
                                               "<data_dir>/enrollment_token.ini).")
        SECRETS.add(token)
        attempt = 0
        while True:
            try:
                data = self.api.enroll(kind, token, installation_info(self.settings))
                break
            except ApiUnavailable as exc:
                attempt += 1
                delay = min(self.settings.api_retry_max_seconds,
                            self.settings.api_retry_base_seconds * (2 ** min(attempt, 10)))
                log.warning("Nexus no disponible para enrolar (%s). Reintento en %.0fs.", exc.code, delay)
                if self.stop_event.wait(delay):
                    raise FatalAgentError(EXIT_OK, "Detenido antes de enrolar.")
            except (ApiAuthError, ApiForbidden, ApiRejected) as exc:
                if from_file:
                    # No se conserva el token rechazado en claro: se borra y queda solo una marca sin
                    # secretos (fecha y código) para el operador y set_enrollment_token.ps1.
                    self._discard_enrollment_file()
                    try:
                        with open(self.settings.enrollment_file + ".rechazado", "w", encoding="utf-8") as fh:
                            fh.write(f"{iso_utc(utcnow())} {exc.code}\n")
                    except OSError:
                        pass
                    log.error("Nexus rechazó el token de enrolamiento de un solo uso (%s); se borró. "
                              "Genere un token nuevo en el panel y re-enrole (set_enrollment_token.ps1).", exc.code)
                raise FatalAgentError(EXIT_AUTH, f"Enrolamiento rechazado ({exc.code}): {exc.message}")
        cred = InstallationCredential(
            installation_id=str(data["installation_id"]), secret=str(data["secret"]),
            api_url=self.settings.api_url.rstrip("/"), enrolled_at=iso_utc(utcnow()),
        )
        SECRETS.add(cred.secret)
        self.store.save(cred)
        self.holder.set(cred)
        scope_type = (data.get("scope") or {}).get("type", "?")
        if from_file:
            self._discard_enrollment_file()
            log.warning("Instalación enrolada: %s (alcance %s). Token de un solo uso consumido y borrado de la "
                        "carpeta de datos.", cred.installation_id, scope_type)
        else:
            log.warning(
                "Instalación enrolada: %s (alcance %s). Recomendación: elimine token/group_token/agency_token "
                "de config.ini; ya no se usan para operar.", cred.installation_id, scope_type,
            )
        return cred

    def _discard_enrollment_file(self) -> None:
        """Sobrescribe y borra el token de un solo uso (mejor esfuerzo; en SSD no hay borrado físico)."""
        path = self.settings.enrollment_file
        for attempt in range(3):
            try:
                if not os.path.exists(path):
                    return
                size = os.path.getsize(path)
                with open(path, "r+b") as fh:
                    fh.write(b"\0" * size)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.unlink(path)
                return
            except OSError as exc:
                if attempt == 2:
                    log.error("No se pudo borrar %s (%s): bórrelo a mano.", os.path.basename(path),
                              type(exc).__name__)
                else:
                    time.sleep(0.5)

    def rotate_credential(self) -> bool:
        try:
            data = self.api.rotate_credentials()
        except ApiError as exc:
            log.warning("No se pudo rotar la credencial (%s); se reintentará.", exc.code)
            return False
        old = self.holder.get()
        cred = InstallationCredential(
            installation_id=str(data["installation_id"]), secret=str(data["secret"]),
            api_url=self.settings.api_url.rstrip("/"), enrolled_at=old.enrolled_at if old else "",
            rotated_at=iso_utc(utcnow()),
        )
        SECRETS.add(cred.secret)
        # Primero se persiste; si el proceso muere aquí, el secreto anterior sigue
        # valiendo durante la ventana de gracia del servidor.
        self.store.save(cred)
        self.holder.set(cred)
        self._rotation_requested.clear()
        log.info("Credencial de instalación rotada.")
        self.enqueue_agent_event("credential_rotated", {}, critical=False)
        return True

    def _handle_auth_error(self, exc: ApiAuthError) -> None:
        """
        Solo ``installation_revoked`` detiene el agente. Otro 401 (p. ej. carrera con
        una rotación; la API ya reintentó con la credencial vigente) se registra y se
        sigue reintentando con backoff sin iniciar tareas nuevas (la config caduca).
        """
        if exc.code == "installation_revoked":
            log.error("La instalación fue revocada (%s). El agente se detiene. Para volver a operar re-enrole con: "
                      "client_postgres.py --enroll (requiere token de enrolamiento vigente).", exc.code)
            if self.fatal is None:
                self.fatal = FatalAgentError(EXIT_AUTH, "La instalación fue revocada")
            self.stop_event.set()
            return
        now = time.monotonic()
        if now - self._last_auth_warn > 60:
            self._last_auth_warn = now
            log.error("Nexus rechazó la credencial de instalación (%s); se reintenta. Si persiste, "
                      "re-enrole con --enroll.", exc.code)

    # ═════════════════════════════════════════════════════════════════════════
    # Configuración / autorización
    # ═════════════════════════════════════════════════════════════════════════
    def refresh_config(self) -> bool:
        try:
            data = self.api.get_tasks()
        except ApiUnavailable as exc:
            log.warning("No se pudo obtener la configuración de Nexus (%s).", exc.code)
            return False
        except ApiAuthError as exc:
            self._handle_auth_error(exc)
            return False
        except ApiForbidden as exc:
            log.warning("Nexus rechazó la configuración (%s): %s", exc.code, exc.message)
            self._drop_config()
            return False
        except ApiRejected as exc:
            log.warning("Respuesta inesperada de Nexus (%s).", exc.code)
            return False

        self.mark_api_ok()
        warehouse = data.get("warehouse") or {}
        register_warehouse_secrets(warehouse)
        for t in data.get("tasks") or []:
            register_task_secrets(t, warehouse)
            sync = t.get("sync") or {}
            self.state.prune_checkpoints(int(t["task_id"]), sync.get("watermark"))
        with self._config_lock:
            old = self._config
            self._config = data
            self._config_at = time.monotonic()
        self._stale_reported = False
        self._log_changes(old, data)
        withheld = data.get("withheld_tasks") or []
        if withheld:
            log.warning("Nexus retuvo %d tarea(s) para este agente: %s.", len(withheld),
                        ", ".join(sorted({str(w.get("reason")) for w in withheld})))
        try:
            prune_ca_files(self.settings, data)
        except Exception as exc:  # noqa: BLE001
            log.debug("Certificados: no se pudo limpiar (%s).", type(exc).__name__)
        if data.get("credential_rotation_required"):
            self.rotate_credential()
        return True

    def _drop_config(self) -> None:
        with self._config_lock:
            self._config = None
            self._config_at = None

    def _log_changes(self, old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> None:
        """Solo ids, versiones y agenda: jamás el SQL."""
        new_map = {int(t["task_id"]): t for t in new.get("tasks") or []}
        if old is None:
            log.info("Configuración recibida: %d tarea(s) autorizada(s).", len(new_map))
            return
        old_map = {int(t["task_id"]): t for t in old.get("tasks") or []}
        for tid in sorted(set(new_map) - set(old_map)):
            log.info("  [+] Nueva tarea %s (query v%s).", tid, new_map[tid].get("query_version"))
        for tid in sorted(set(old_map) - set(new_map)):
            log.info("  [-] Tarea %s ya no está autorizada.", tid)
        for tid in sorted(set(old_map) & set(new_map)):
            a, b = old_map[tid], new_map[tid]
            if a.get("query_version") != b.get("query_version"):
                log.info("  [~] Tarea %s: query_version %s → %s.", tid, a.get("query_version"), b.get("query_version"))
            if a.get("schedule_seconds") != b.get("schedule_seconds"):
                log.info("  [~] Tarea %s: schedule %ss → %ss.", tid, a.get("schedule_seconds"), b.get("schedule_seconds"))

    def config_age(self) -> Optional[float]:
        at = self._config_at
        return None if at is None else time.monotonic() - at

    def fresh_config(self) -> Optional[Dict[str, Any]]:
        """Config vigente o None si caducó (entonces se purga de memoria)."""
        with self._config_lock:
            cfg, at = self._config, self._config_at
        if cfg is None or at is None:
            return None
        max_age = min(self.settings.config_max_age_seconds,
                      int(cfg.get("config_max_age_seconds") or self.settings.config_max_age_seconds))
        if time.monotonic() - at > max_age:
            self._drop_config()
            if not self._stale_reported:
                self._stale_reported = True
                log.error("La autorización de Nexus caducó (%ss sin respuesta válida): no se inician tareas "
                          "nuevas hasta recuperar la conexión.", max_age)
                self.enqueue_agent_event("config_stale", {"max_age_seconds": max_age}, critical=False)
            return None
        return cfg

    # ═════════════════════════════════════════════════════════════════════════
    # Agenda
    # ═════════════════════════════════════════════════════════════════════════
    def due_tasks(self, cfg: Dict[str, Any], now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = self.clock() if now is None else now
        due = []
        for t in cfg.get("tasks") or []:
            tid = int(t["task_id"])
            schedule = int(t.get("schedule_seconds") or 3600)
            sched = self.state.get_schedule(tid)
            if sched is None:
                last_ok = _epoch((t.get("sync") or {}).get("last_success_at"))
                if self.settings.run_all_on_start or last_ok is None:
                    nxt = now
                else:
                    # Tras un reinicio no se dispara todo: se respeta el último éxito conocido.
                    nxt = max(now, last_ok + schedule)
                self.state.set_schedule(tid, next_due_at=nxt, schedule_seconds=schedule, attempt=1)
                sched = self.state.get_schedule(tid)
            elif sched.get("schedule_seconds") != schedule:
                base = sched.get("last_started_at") or now
                self.state.set_schedule(tid, schedule_seconds=schedule, next_due_at=base + schedule)
                sched = self.state.get_schedule(tid)
            if sched["next_due_at"] <= now:
                due.append((sched["next_due_at"], t))
        due.sort(key=lambda x: x[0])
        return [t for _, t in due]

    def previous_watermark(self, task: Dict[str, Any]):
        sync = task.get("sync") or {}
        server = parse_watermark(sync.get("watermark"))
        local = self.state.local_watermark(int(task["task_id"]), _epoch(sync.get("watermark_reset_at")))
        server_kind = sync.get("watermark_kind")
        if local and server_kind and server_kind != "legacy_last_run" and local[1] != server_kind:
            local = None  # no se mezclan relojes
        local_dt = parse_watermark(local[0]) if local else None
        if server is None:
            return local_dt
        if local_dt is None:
            return server
        return max(server, local_dt)

    # ═════════════════════════════════════════════════════════════════════════
    # Ejecución
    # ═════════════════════════════════════════════════════════════════════════
    def execute_task(self, task: Dict[str, Any], warehouse: Dict[str, Any]) -> Dict[str, Any]:
        tid = int(task["task_id"])
        sched = self.state.get_schedule(tid) or {}
        attempt = int(sched.get("attempt") or 1)
        schedule = int(task.get("schedule_seconds") or 3600)
        execution_id = str(uuid.uuid4())
        started = utcnow()
        start_epoch = self.clock()
        t0 = time.monotonic()
        self.state.set_schedule(tid, last_started_at=start_epoch)
        common = {"task_id": tid, "attempt": attempt, "query_version": task.get("query_version"),
                  "started_at": iso_utc(started), "client_version": AGENT_VERSION}
        self.state.enqueue("execution_start", dict(common, execution_id=execution_id, event_time=iso_utc(started)),
                           execution_id=execution_id, task_id=tid, running="start")
        self._sender_wakeup.set()
        with self._running_lock:
            self._running[tid] = execution_id
        log.info("Tarea %s: inicio ejecución %s (intento %d, query v%s).", tid, execution_id[:8], attempt,
                 task.get("query_version"))

        ctx = RunContext(self.cancel_event)
        ctx.before_commit = lambda: self.state.mark_committing(execution_id)
        self._current_ctx = ctx
        res: Optional[TaskResult] = None
        status, stage, code, message = "success", None, None, None
        try:
            prev = self.previous_watermark(task)
            prev_kind = (task.get("sync") or {}).get("watermark_kind")
            res = run_task(task, warehouse, prev, self.settings, ctx, previous_kind=prev_kind)
        except Cancelled:
            status, stage, code, message = "interrupted", ctx.stage, "CANCELLED", "Ejecución cancelada por apagado."
        except StageError as exc:
            if isinstance(exc.original, Cancelled):
                status, stage, code, message = "interrupted", exc.stage, "CANCELLED", "Ejecución cancelada."
            else:
                status, stage = "failed", exc.stage
                code, message = sanitize_error(exc)
        except Exception as exc:  # defensa: nada debe tumbar el worker
            status, stage = "failed", ctx.stage
            code, message = sanitize_error(exc, stage=ctx.stage)
        finally:
            self._current_ctx = None
            with self._running_lock:
                self._running.pop(tid, None)

        finished = utcnow()
        duration_ms = int((time.monotonic() - t0) * 1000)
        if status == "success" and res is not None and res.watermark is not None:
            stored_kind = (task.get("sync") or {}).get("watermark_kind")
            if "SOURCE_CLOCK_UNAVAILABLE" in res.warnings:
                # Reloj del origen no disponible: NO se avanza el watermark con otro reloj
                # (mezclar relojes puede saltarse filas). Se recargará la ventana.
                res.warnings.append("CHECKPOINT_SKIPPED_CLOCK_FALLBACK")
                res.watermark = None
            elif stored_kind and stored_kind != "legacy_last_run" and stored_kind != res.watermark_kind:
                res.warnings.append("CHECKPOINT_KIND_MISMATCH")
                res.watermark = None
        payload: Dict[str, Any] = dict(
            common, status=status, finished_at=iso_utc(finished), duration_ms=duration_ms,
            failure_stage=stage if status != "success" else None,
            rows_read=res.rows_read if res else None, rows_loaded=res.rows_loaded if res else None,
            rows_inserted=res.rows_inserted if res else None, rows_updated=res.rows_updated if res else None,
            error_code=code, error_message=message, warnings=list(res.warnings) if res else [],
            ddl_applied=list(ctx.ddl_applied)[:50],
            event_time=iso_utc(finished),
        )
        checkpoint = None
        if status == "success" and res is not None and res.watermark is not None:
            wm = watermark_iso(res.watermark)
            payload["checkpoint"] = {"watermark": wm, "kind": res.watermark_kind}
            checkpoint = (wm, res.watermark_kind, start_epoch)
        self.state.enqueue("execution_update", dict(payload, execution_id=execution_id),
                           critical=(status != "success"), is_success=(status == "success"),
                           execution_id=execution_id, task_id=tid, checkpoint=checkpoint, running="end")
        self._sender_wakeup.set()

        # Agenda / reintentos (un reintento = nueva ejecución con attempt+1)
        now = self.clock()
        if status == "success":
            self.state.set_schedule(tid, attempt=1, next_due_at=start_epoch + schedule,
                                    last_finished_at=now, last_status=status)
            log.info("Tarea %s: OK — %s leídas, %s cargadas (%s ins / %s act) en %.1fs.%s", tid,
                     res.rows_read, res.rows_loaded, res.rows_inserted, res.rows_updated, duration_ms / 1000,
                     (" Avisos: " + ", ".join(res.warnings)) if res.warnings else "")
        elif status == "interrupted":
            self.state.set_schedule(tid, next_due_at=now, last_finished_at=now, last_status=status)
            log.warning("Tarea %s: interrumpida en etapa %s (ROLLBACK, watermark sin cambios).", tid, stage)
        else:
            if attempt <= self.settings.task_retry_attempts:
                delay = self.settings.task_retry_backoff_seconds * (2 ** (attempt - 1))
                nxt = now + min(delay, schedule)
                self.state.set_schedule(tid, attempt=attempt + 1, next_due_at=nxt, last_finished_at=now,
                                        last_status=status)
                retry_msg = f"reintento {attempt + 1} en {int(nxt - now)}s"
            else:
                self.state.set_schedule(tid, attempt=1, next_due_at=start_epoch + schedule, last_finished_at=now,
                                        last_status=status)
                retry_msg = "sin más reintentos hasta la próxima programación"
            log.error("Tarea %s: FALLÓ en etapa %s [%s] %s (%s).", tid, stage, code, message, retry_msg)
        return {"execution_id": execution_id, "status": status, "stage": stage, "error_code": code,
                "result": res}

    def recover_orphans(self) -> int:
        """
        Tras un reinicio: las ejecuciones que quedaron a medias (proceso muerto
        durante la carga) se reportan como ``interrupted``. Su transacción en el
        DWH nunca se confirmó (sin COMMIT → PostgreSQL la revierte) y el watermark
        no avanzó, así que la siguiente ejecución vuelve a cargar esa ventana.
        """
        orphans = self.state.orphan_executions()
        for o in orphans:
            p = o["payload"]
            payload = {k: p.get(k) for k in ("task_id", "attempt", "query_version", "started_at", "client_version")}
            if o.get("phase") == "committing":
                code = "AGENT_RESTARTED_COMMIT_UNKNOWN"
                msg = ("El agente se reinició durante el COMMIT: estado de la carga desconocido (pudo "
                       "confirmarse). El watermark no avanzó; la siguiente ejecución recarga la ventana.")
            else:
                code = "AGENT_RESTARTED"
                msg = "El agente se reinició durante la ejecución, antes del COMMIT: la carga no se confirmó."
            payload.update(status="interrupted", finished_at=iso_utc(utcnow()), failure_stage=None,
                           error_code=code, error_message=msg,
                           warnings=[], event_time=iso_utc(utcnow()), execution_id=o["execution_id"])
            self.state.enqueue("execution_update", payload, critical=True, execution_id=o["execution_id"],
                               task_id=o["task_id"], running="end")
            log.warning("Tarea %s: la ejecución %s quedó a medias en el arranque anterior; se reporta como "
                        "interrumpida (sin COMMIT en el DWH).", o["task_id"], o["execution_id"][:8])
        if orphans:
            self._sender_wakeup.set()
        return len(orphans)

    # ═════════════════════════════════════════════════════════════════════════
    # Cola de reportes
    # ═════════════════════════════════════════════════════════════════════════
    def enqueue_agent_event(self, event_type: str, payload: Dict[str, Any], critical: bool = False) -> None:
        try:
            self.state.enqueue("agent_event", {"event_type": event_type, "event_time": iso_utc(utcnow()),
                                               "payload": payload}, critical=critical)
            self._sender_wakeup.set()
        except Exception as exc:
            log.warning("No se pudo encolar evento %s: %s", event_type, type(exc).__name__)

    def _deliver(self, api: NexusApi, item: OutboxItem) -> None:
        p = item.payload
        if item.type == "execution_start":
            api.start_execution(p)
        elif item.type == "execution_update":
            api.update_execution(p.get("execution_id") or item.execution_id, p)
        elif item.type == "agent_event":
            api.send_event(p)
        else:
            raise ApiRejected(0, "unknown_type", f"Tipo de reporte desconocido: {item.type}")

    def send_one(self, api: NexusApi) -> str:
        """Intenta enviar el primer reporte. Devuelve: empty|wait|sent|deferred|dead|auth."""
        item = self.state.head()
        if item is None:
            return "empty"
        if item.next_attempt_at > self.clock():
            return "wait"
        try:
            self._deliver(api, item)
        except ApiUnavailable as exc:
            # Solo un HTTP 500 EXACTO cuenta para "veneno". 502/503/504 (proxy o backend
            # caídos, BD de config no disponible), red y timeouts son una CAÍDA: se
            # reintenta siempre con backoff y nunca se descarta nada.
            is_500 = exc.status == 500
            nxt = self.state.defer(item, exc.code, server_error=is_500)
            if is_500 and self._is_poison(item):
                return self._handle_poison(item, exc)
            log.debug("Reporte %s aplazado (%s) hasta +%.0fs.", item.seq, exc.code, nxt - self.clock())
            return "deferred"
        except ApiAuthError as exc:
            self.state.defer(item, exc.code)
            self._handle_auth_error(exc)
            return "auth"
        except ApiForbidden as exc:
            if exc.code == "scope_disabled":
                # Alcance deshabilitado temporalmente: NO se descartan reportes (incluidos fallos).
                self.state.defer(item, exc.code)
                self._drop_config()
                return "deferred"
            log.warning("Reporte %s (%s) rechazado definitivamente por Nexus (%s); va a dead-letter "
                        "(se avisará a Nexus).", item.seq, item.type, exc.code)
            self.state.dead_letter(item, exc.status, exc.code)
            return "dead"
        except ApiRejected as exc:
            log.warning("Reporte %s (%s) rechazado definitivamente por Nexus (%s); va a dead-letter "
                        "(se avisará a Nexus).", item.seq, item.type, exc.code)
            self.state.dead_letter(item, exc.status, exc.code)
            return "dead"
        except ConfigError as exc:
            self.state.defer(item, "config")
            log.error("Error de configuración al enviar reportes: %s", exc)
            return "deferred"
        except Exception as exc:
            self.state.defer(item, type(exc).__name__)
            code, msg = sanitize_error(exc)
            log.warning("Error inesperado al enviar reporte %s: [%s] %s", item.seq, code, msg)
            return "deferred"
        self.state.mark_sent(item)
        self.mark_api_ok()
        return "sent"

    def mark_api_ok(self) -> None:
        """Nexus respondió 2xx a alguna llamada (heartbeat, tareas o reportes)."""
        self._last_api_ok = self.clock()

    def _is_poison(self, item: OutboxItem) -> bool:
        """
        Veneno = Nexus está sano pero rechaza ESTE reporte con 500:
          * ≥ queue_max_server_errors respuestas 500 seguidas para el reporte,
          * ≥ queue_poison_min_seconds desde la primera, y
          * otra llamada a Nexus tuvo éxito DESPUÉS de esa primera falla.
        """
        count, first_at, parked = self.state.server_error_info(item)
        if parked or first_at is None:
            return False
        return (count >= self.settings.queue_max_server_errors
                and self.clock() - first_at >= self.settings.queue_poison_min_seconds
                and self._last_api_ok > first_at)

    def _handle_poison(self, item: OutboxItem, exc: ApiError) -> str:
        if item.critical or item.type == "execution_update":
            # Críticos (fallos, interrupciones) y fines con checkpoint: NUNCA se descartan.
            self.state.park(item, self.settings.queue_parked_retry_seconds, "poison_500")
            log.error("Reporte %s (%s) rechazado con 500 repetidos mientras Nexus responde a otras "
                      "llamadas: se estaciona y se reintenta cada %ss (no bloquea la cola; se avisa a Nexus).",
                      item.seq, item.type, self.settings.queue_parked_retry_seconds)
            return "parked"
        log.error("Reporte %s (%s) rechazado con 500 repetidos mientras Nexus responde a otras llamadas: "
                  "se aparta a dead-letter (se avisa a Nexus).", item.seq, item.type)
        self.state.dead_letter(item, exc.status, "server_error_repeated")
        return "dead"

    def flush(self, timeout: float = 30.0, api: Optional[NexusApi] = None) -> bool:
        """Envía la cola completa (uso: --once y pruebas). True si quedó vacía."""
        api = api or self.api
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.send_one(api)
            if r == "empty":
                return True
            if r in ("auth",):
                return False
            if r in ("wait", "deferred"):
                head = self.state.head()
                if head is None:
                    return True
                delay = max(0.05, min(head.next_attempt_at - self.clock(), deadline - time.monotonic()))
                if delay <= 0 or time.monotonic() + delay > deadline:
                    return False
                time.sleep(min(delay, 1.0))
        return self.state.head() is None

    # ═════════════════════════════════════════════════════════════════════════
    # Hilos
    # ═════════════════════════════════════════════════════════════════════════
    def _sender_loop(self) -> None:
        api = self.api_factory(self.settings, self.holder)
        try:
            while True:
                if self.stop_event.is_set() and time.monotonic() > self._sender_stop_deadline:
                    break
                try:
                    r = self.send_one(api)
                except Exception as exc:  # nunca debe morir
                    log.warning("Envío de reportes: error inesperado %s.", type(exc).__name__)
                    r = "deferred"
                if r in ("sent", "dead", "parked"):
                    continue
                if r == "auth":
                    if self.stop_event.wait(5):
                        continue
                wait = 1.0
                if r in ("wait", "deferred"):
                    head = self.state.head()
                    if head is not None:
                        wait = max(0.05, min(1.0, head.next_attempt_at - self.clock()))
                self._sender_wakeup.wait(wait)
                self._sender_wakeup.clear()
        finally:
            api.close()

    def heartbeat_payload(self) -> Dict[str, Any]:
        with self._running_lock:
            running = [{"task_id": t, "execution_id": e} for t, e in self._running.items()]
        age = self.config_age()
        return {
            "client_version": AGENT_VERSION,
            "uptime_seconds": int(time.monotonic() - self._started_mono),
            "running": running,
            "queue_depth": self.state.depth,
            "queue_overflow_total": self.state.overflow_total_cached,
            "dead_letter_total": self.state.dead_letter_total_cached,
            "parked_total": self.state.parked_total_cached,
            "agent_seq": self.state.last_seq_cached,
            "config_age_seconds": int(age) if age is not None else None,
            "event_time": iso_utc(utcnow()),
            "features": [f for f in AGENT_FEATURES
                         if f != "connection-test" or self.settings.connection_test_enabled],
        }

    def send_heartbeat(self, api: NexusApi) -> bool:
        try:
            resp = api.heartbeat(self.heartbeat_payload())
        except ApiAuthError as exc:
            self._handle_auth_error(exc)
            return False
        except ApiError as exc:
            log.debug("Heartbeat no enviado (%s).", exc.code)
            return False
        except Exception as exc:
            log.debug("Heartbeat: error %s.", type(exc).__name__)
            return False
        self.mark_api_ok()
        if resp.get("credential_rotation_required"):
            self._rotation_requested.set()
        if resp.get("connection_tests_pending"):
            self._tests_wakeup.set()
        return True

    def _heartbeat_loop(self) -> None:
        api = self.api_factory(self.settings, self.holder)  # sesión HTTP propia
        failures = 0
        try:
            while not self.stop_event.is_set():
                ok = self.send_heartbeat(api)
                failures = 0 if ok else failures + 1
                if failures == 3:
                    log.warning("Heartbeat: Nexus no responde (se sigue intentando).")
                self.stop_event.wait(self.settings.heartbeat_seconds)
        finally:
            api.close()

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set() or not self._work_q.empty():
            try:
                job = self._work_q.get(timeout=0.5)
            except queue.Empty:
                continue
            task, warehouse = job
            try:
                self.execute_task(task, warehouse)
            except Exception as exc:  # execute_task ya captura; defensa extra
                code, msg = sanitize_error(exc)
                log.error("Worker: error inesperado [%s] %s", code, msg)
            finally:
                self._busy.clear()

    def _inventory_loop(self) -> None:
        api = self.api_factory(self.settings, self.holder)  # sesión HTTP propia
        try:
            InventoryRunner(self.settings, api, self.fresh_config, stop_event=self.stop_event,
                            on_api_ok=self.mark_api_ok).loop()
        finally:
            api.close()

    def _tests_budget_wait(self) -> float:
        """Segundos a esperar antes de tomar otra prueba (tope por minuto), 0 si puede."""
        now = time.monotonic()
        self._test_times = [t for t in self._test_times if now - t < 60]
        if len(self._test_times) < self.settings.connection_test_max_per_minute:
            return 0.0
        return max(0.5, 60 - (now - self._test_times[0]))

    def run_one_connection_test(self, api: NexusApi) -> Optional[Dict[str, Any]]:
        """Toma una prueba pendiente (si hay), la ejecuta y reporta. Devuelve un resumen o None."""
        if self._tests_budget_wait() > 0:
            return None  # tope por minuto: ni siquiera se toma (otra instalación puede hacerlo)
        resp = api.claim_connection_test()
        self.mark_api_ok()
        test = resp.get("test")
        if not test:
            return None
        tid = str(test.get("id"))
        log.info("Prueba de conexión %s (%s): en curso.", tid[:8], test.get("target_kind"))
        # Separación mínima entre pruebas a la MISMA conexión (evita bloquear la cuenta en la base del
        # cliente por intentos repetidos); la espera cabe en el plazo del servidor.
        target = connection_key(test)
        last = self._test_last_by_target.get(target)
        spacing = float(self.settings.connection_test_min_spacing_seconds)
        if last is not None and time.monotonic() - last < spacing:
            if self.stop_event.wait(spacing - (time.monotonic() - last)):
                return None
        self._test_times.append(time.monotonic())
        self._test_last_by_target[target] = time.monotonic()
        result = run_connection_test(test, self.settings)
        try:
            api.report_connection_test(tid, result)
        except ApiError as exc:
            log.warning("Prueba de conexión %s: no se pudo reportar el resultado (%s).", tid[:8], exc.code)
        log.info("Prueba de conexión %s: %s%s.", tid[:8], result.get("status"),
                 f" (código {result['error_code']})" if result.get("error_code") else "")
        self.last_connection_test = {"id": tid, "status": result.get("status"),
                                     "error_code": result.get("error_code")}
        return self.last_connection_test

    def _connection_test_loop(self) -> None:
        api = self.api_factory(self.settings, self.holder)  # sesión HTTP propia
        failures = 0
        try:
            while not self.stop_event.is_set():
                delay = float(self.settings.connection_test_poll_seconds)
                try:
                    # Varias pendientes seguidas se toman sin esperar.
                    while not self.stop_event.is_set() and self.run_one_connection_test(api):
                        pass
                    budget = self._tests_budget_wait()
                    if budget:
                        delay = max(delay, budget)
                    failures = 0
                except ApiAuthError as exc:
                    self._handle_auth_error(exc)
                    failures += 1
                except ApiError as exc:
                    failures += 1
                    if exc.status == 404:
                        # Nexus anterior sin la ruta: se consulta muy de vez en cuando.
                        delay = 600.0
                    log.debug("Prueba de conexión: consulta no disponible (%s).", exc.code)
                except Exception as exc:  # noqa: BLE001 — el hilo nunca muere
                    failures += 1
                    code, msg = sanitize_error(exc)
                    log.warning("Prueba de conexión: error inesperado [%s] %s", code, msg)
                if failures:
                    delay = max(delay, min(300.0, delay * (2 ** min(failures, 5))))
                self._tests_wakeup.wait(delay)
                self._tests_wakeup.clear()
        finally:
            api.close()

    def start_threads(self) -> None:
        self._sender_stop_deadline = float("inf")
        threads = [("heartbeat", self._heartbeat_loop), ("sender", self._sender_loop),
                   ("worker", self._worker_loop)]
        if self.settings.inventory_enabled:
            threads.append(("inventory", self._inventory_loop))
        if self.settings.connection_test_enabled:
            threads.append(("connection-tests", self._connection_test_loop))
        for name, target in threads:
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    # ═════════════════════════════════════════════════════════════════════════
    # Bucle principal
    # ═════════════════════════════════════════════════════════════════════════
    def _periodic_housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_purge > 3600:
            self._last_purge = now
            try:
                n = self.state.purge_expired()
                if n:
                    log.warning("Cola: %d reporte(s) superaron la retención y se purgaron (contados).", n)
            except Exception as exc:
                log.warning("Cola: no se pudo purgar (%s).", type(exc).__name__)
        dl = self.state.take_dead_letter_report()
        if dl:
            log.error("Reportes apartados (dead-letter) no aceptados por Nexus: %s.", dl)
            self.enqueue_agent_event("dead_letter", dl, critical=True)
        rep = self.state.take_overflow_report()
        if rep:
            log.error("Cola de reportes saturada: descartes acumulados %s.", rep)
            self.enqueue_agent_event("queue_overflow", rep, critical=True)

    def run_forever(self) -> int:
        self.recover_orphans()
        pending = self.state.unconfirmed_checkpoints()
        if pending:
            log.warning("%d carga(s) confirmadas en el DWH aún sin confirmación de Nexus. Sus reportes pendientes "
                        "en la cola se reenvían; si alguno se descartó por saturación, el checkpoint local se "
                        "sigue usando hasta que Nexus confirme uno igual o posterior.", len(pending))
        self.start_threads()
        self.enqueue_agent_event("agent_started", {"version": AGENT_VERSION}, critical=False)
        next_refresh = 0.0
        failures = 0
        try:
            while not self.stop_event.is_set():
                try:
                    now = time.monotonic()
                    if self._rotation_requested.is_set():
                        self.rotate_credential()
                    if now >= next_refresh:
                        if self.refresh_config():
                            failures = 0
                            cfg = self._config or {}
                            next_refresh = now + max(5, int(cfg.get("refresh_seconds") or 60))
                        else:
                            failures += 1
                            next_refresh = now + min(self.settings.api_retry_max_seconds,
                                                     self.settings.api_retry_base_seconds * (2 ** min(failures, 10)))
                    if self.stop_event.is_set():
                        break
                    cfg = self.fresh_config()
                    if cfg is not None and not self._busy.is_set():
                        due = self.due_tasks(cfg)
                        if due:
                            self._busy.set()
                            self._work_q.put((due[0], task_warehouse(due[0], cfg)))
                    self._periodic_housekeeping()
                except Exception as exc:  # el scheduler nunca muere por errores transitorios
                    code, msg = sanitize_error(exc)
                    log.error("Scheduler: error inesperado [%s] %s", code, msg)
                self.stop_event.wait(self.settings.tick_seconds)
        finally:
            self.shutdown()
        return self.fatal.exit_code if self.fatal else EXIT_OK

    def run_once(self) -> int:
        """Refresca config, ejecuta las tareas vencidas en serie y vacía la cola."""
        self.recover_orphans()
        if not self.refresh_config():
            return self.fatal.exit_code if self.fatal else 1
        cfg = self.fresh_config() or {}
        for task in self.due_tasks(cfg):
            if self.stop_event.is_set():
                break
            self.execute_task(task, task_warehouse(task, cfg))
        self.flush(timeout=60)
        return self.fatal.exit_code if self.fatal else EXIT_OK

    def stop(self) -> None:
        self.stop_event.set()

    def shutdown(self) -> None:
        self.stop_event.set()
        self._tests_wakeup.set()
        grace = self.settings.shutdown_grace_seconds
        if self._busy.is_set():
            log.warning("Apagando: se espera hasta %ss a que termine la tarea en curso.", grace)
            deadline = time.monotonic() + grace
            while self._busy.is_set() and time.monotonic() < deadline:
                time.sleep(0.2)
            if self._busy.is_set():
                log.warning("Apagando: se cancela la tarea en curso (ROLLBACK entre chunks).")
                self.cancel_event.set()
                deadline = time.monotonic() + 10
                while self._busy.is_set() and time.monotonic() < deadline:
                    time.sleep(0.2)
                ctx = self._current_ctx
                if self._busy.is_set() and ctx is not None:
                    ctx.fire_cancel_hooks()  # interrumpe la sentencia en curso
                    deadline = time.monotonic() + 15
                    while self._busy.is_set() and time.monotonic() < deadline:
                        time.sleep(0.2)
        self.enqueue_agent_event("agent_stopping", {}, critical=False)
        # Último intento corto de vaciar la cola (lo pendiente queda en disco).
        self._sender_stop_deadline = time.monotonic() + 5
        self._sender_wakeup.set()
        for th in self._threads:
            th.join(timeout=10)
        try:
            self.api.close()
        except Exception:
            pass
        log.info("Agente detenido. Reportes pendientes en cola: %d.", self.state.depth)
