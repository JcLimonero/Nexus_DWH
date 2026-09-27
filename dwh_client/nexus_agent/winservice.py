"""
winservice.py — el agente como servicio de Windows (pywin32).

Lo registra ``packaging/windows/install_service.ps1`` con ``sc.exe``:

  binPath = "C:\\Program Files\\NexusAgent\\NexusAgent.exe" --service
            --config "C:\\ProgramData\\NexusAgent\\config.ini"
            --data-dir "C:\\ProgramData\\NexusAgent\\data"
  obj     = NT SERVICE\\NexusAgent   (cuenta virtual, sin privilegios de administrador)

Ciclo de vida:
  * El SCM arranca el proceso; ``run_service`` entrega el control al
    despachador de servicios (``StartServiceCtrlDispatcher``) y el agente corre
    en el hilo del servicio (sin manejadores de señales ni consola: solo log a
    archivo).
  * Parada (Detener / apagado del equipo): se informa STOP_PENDING con un
    ``waitHint`` que cubre ``shutdown_grace_seconds`` + cancelación, y se llama
    ``agent.stop()``: la tarea en curso termina o se revierte (ROLLBACK, sin
    cargas parciales) y la cola queda en disco.
  * Salida del agente:
      0      → SERVICE_STOPPED normal.
      2 / 3  → SERVICE_STOPPED con código específico del servicio (configuración
               o credencial revocada): NO se reinicia en bucle; el operador corrige.
      1      → error inesperado: el proceso termina con código ≠ 0 SIN reportar
               STOPPED, así el SCM lo trata como caída y aplica las acciones de
               recuperación (reinicio con espera) configuradas por el instalador.

Las partes independientes de Windows (cálculo del waitHint, coordinación de la
parada antes/después de crear el agente, traducción de códigos) se prueban en
cualquier sistema (tests/test_packaging.py).
"""

import os
import sys
import threading
from typing import Any, Optional, Tuple

SERVICE_NAME = "NexusAgent"
SERVICE_DISPLAY_NAME = "Nexus DWH Agent"
SERVICE_DESCRIPTION = ("Agente ETL de Nexus DWH: ejecuta las tareas autorizadas por Nexus (extracción de solo "
                       "lectura del DMS y carga al DWH). Conexiones salientes HTTPS a Nexus.")
ERROR_SERVICE_SPECIFIC_ERROR = 1066

# Tiempos que ``Agent.shutdown`` puede tardar además de la gracia (ver agent.py):
# 10 s tras marcar cancelación + 15 s tras cancelar la sentencia + 10 s por hilo + margen.
_SHUTDOWN_EXTRA_SECONDS = 10 + 15 + 10 + 15


def stop_wait_hint_ms(shutdown_grace_seconds: int) -> int:
    """waitHint para SERVICE_STOP_PENDING (ms). El SCM lo usa para no dar el servicio por colgado."""
    return int(max(0, shutdown_grace_seconds) + _SHUTDOWN_EXTRA_SECONDS) * 1000


def exit_disposition(code: int) -> Tuple[str, int, int]:
    """
    Qué hacer al terminar el agente con ``code``:
      ("stopped", win32ExitCode, svcExitCode)  → informar SERVICE_STOPPED
      ("crash", 0, code)                       → salir sin informar (el SCM reinicia)
    """
    if code == 0:
        return "stopped", 0, 0
    if code in (2, 3, 4):
        return "stopped", ERROR_SERVICE_SPECIFIC_ERROR, code
    return "crash", 0, code


class StopCoordinator:
    """
    Coordina la parada pedida por el SCM con el agente, que puede no existir
    todavía (el SCM puede pedir parar mientras se carga la configuración).
    Seguro entre hilos.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agent: Optional[Any] = None
        self.stop_requested = threading.Event()

    def attach(self, agent: Any) -> None:
        with self._lock:
            self._agent = agent
            stop_now = self.stop_requested.is_set()
        if stop_now:
            agent.stop()

    def request_stop(self) -> None:
        with self._lock:
            self.stop_requested.set()
            agent = self._agent
        if agent is not None:
            agent.stop()

    def grace_seconds(self, default: int = 60) -> int:
        with self._lock:
            agent = self._agent
        try:
            return int(agent.settings.shutdown_grace_seconds) if agent is not None else default
        except Exception:  # noqa: BLE001
            return default


def run_service(args: Any) -> int:
    if sys.platform != "win32":
        print("--service solo está disponible en Windows (Administrador de servicios).", file=sys.stderr)
        return 2
    return _run_windows_service(args)  # pragma: no cover - solo Windows


def _run_windows_service(args: Any) -> int:  # pragma: no cover - solo Windows
    import servicemanager
    import win32service
    import win32serviceutil

    from .cli import run_agent

    coordinator = StopCoordinator()
    outcome = {"code": 0}

    class NexusAgentService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def SvcStop(self):  # noqa: N802
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING,
                                     waitHint=stop_wait_hint_ms(coordinator.grace_seconds()))
            coordinator.request_stop()

        def SvcShutdown(self):  # noqa: N802 - apagado del equipo
            self.SvcStop()

        def SvcDoRun(self):  # noqa: N802
            try:
                outcome["code"] = run_agent(args, console=False, install_signals=False,
                                            on_agent=coordinator.attach)
            except BaseException:  # noqa: BLE001
                outcome["code"] = 1

        def SvcRun(self):  # noqa: N802
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            self.SvcDoRun()
            kind, win32_code, svc_code = exit_disposition(outcome["code"])
            if kind == "crash" and not coordinator.stop_requested.is_set():
                import logging

                logging.shutdown()
                os._exit(svc_code or 1)  # el SCM ve una caída → acciones de recuperación
            self.ReportServiceStatus(win32service.SERVICE_STOPPED, win32ExitCode=win32_code,
                                     svcExitCode=svc_code)
            if svc_code:
                # Salir ya: así el código específico (2/3) queda como el último estado informado
                # (pywin32 volvería a informar STOPPED con 0 al retomar el control).
                import logging

                logging.shutdown()
                os._exit(svc_code)

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(NexusAgentService)
    try:
        servicemanager.StartServiceCtrlDispatcher()
    except Exception as exc:  # noqa: BLE001 - p. ej. ERROR_FAILED_SERVICE_CONTROLLER_CONNECT (1063)
        print(f"--service debe iniciarlo el Administrador de servicios ({type(exc).__name__}: {exc}). "
              "Para primer plano ejecute sin --service.", file=sys.stderr)
        return 2
    return outcome["code"]
