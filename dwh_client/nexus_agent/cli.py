"""
cli.py — línea de comandos del agente (la usa ``client_postgres.py`` y el
ejecutable compilado ``NexusAgent.exe``).

Un solo ejecutable con subsistema de consola para todos los modos:

  (sin argumentos)          bucle continuo en primer plano (Ctrl+C detiene)
  --once                    ejecuta lo vencido, vacía la cola y sale
  --enroll                  fuerza un enrolamiento nuevo (en servicios: ver §21.3)
  --service                 lo usa el Administrador de servicios de Windows (SCM)
  --selftest                autodiagnóstico sin red ni BD (drivers, TLS, SQLite)
  --verify-update CARPETA   valida un paquete nuevo (firma, versión, hashes, Authenticode)
  --version                 imprime la versión

Códigos de salida: 0 ok, 1 error inesperado, 2 configuración, 3 credencial
revocada/inválida, 4 paquete de actualización rechazado.
"""

import argparse
import signal
import sys
import threading
from typing import Callable, Optional

from . import AGENT_VERSION

EXIT_UPDATE_REJECTED = 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="NexusAgent", description="Agente ETL Nexus DWH (PostgreSQL)")
    p.add_argument("--config", help="Ruta a config.ini (defecto: junto al ejecutable o NEXUS_AGENT_CONFIG)")
    p.add_argument("--data-dir", help="Carpeta de datos del agente (credencial, cola)")
    p.add_argument("--enroll", action="store_true", help="Forzar un enrolamiento nuevo")
    p.add_argument("--once", action="store_true", help="Ejecutar tareas vencidas una vez y salir")
    p.add_argument("--service", action="store_true", help="Modo servicio de Windows (lo usa el SCM)")
    p.add_argument("--selftest", action="store_true", help="Autodiagnóstico (drivers, TLS, SQLite) y salir")
    p.add_argument("--verify-update", metavar="CARPETA", help="Validar un paquete de actualización y salir")
    p.add_argument("--allow-unsigned-manifest", action="store_true",
                   help="Con --verify-update: aceptar release.json sin firma (solo integridad; transición)")
    p.add_argument("--allow-same-version", action="store_true",
                   help="Con --verify-update: aceptar la misma versión (reinstalación)")
    p.add_argument("--version", action="store_true")
    return p


def _windows_account() -> str:  # pragma: no cover - solo Windows
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(257)
        size = ctypes.c_ulong(257)
        if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)):
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    return "?"


def _event_log_error(message: str) -> None:  # pragma: no cover - solo Windows
    """Registro de eventos de Windows (Aplicación), además del log propio: visible sin abrir archivos."""
    if sys.platform != "win32":
        return
    try:
        import servicemanager

        servicemanager.LogErrorMsg(f"Nexus DWH Agent: {message}")
    except Exception:  # noqa: BLE001
        pass


def _early_service_log(args: argparse.Namespace, message: str) -> None:
    """
    Error de configuración ANTES de conocer log_dir (config.ini ilegible o ausente) en modo
    servicio: se escribe en <data_dir>/../logs/nexus_agent.log (estructura del instalador) o junto
    al ejecutable, siempre saneado.
    """
    import datetime as _dt
    import os

    from .sanitize import redact_text
    from .settings import APP_DIR

    base = os.path.dirname(os.path.abspath(args.data_dir)) if args.data_dir else APP_DIR
    try:
        log_dir = os.path.join(base, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "nexus_agent.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} | ERROR    | servicio   | {redact_text(message)}\n")
    except Exception:  # noqa: BLE001
        pass


def run_agent(args: argparse.Namespace, *, console: bool = True, install_signals: bool = True,
              on_agent: Optional[Callable[[object], None]] = None) -> int:
    """Carga configuración, prepara logging y ejecuta el agente. Compartido por consola y servicio."""
    from .agent import EXIT_CONFIG, Agent, FatalAgentError
    from .api import ApiError
    from .logsetup import setup_logging
    from .sanitize import SECRETS, ConfigError, redact_text, sanitize_error
    from .settings import load_settings

    try:
        settings = load_settings(args.config, args.data_dir)
    except ConfigError as exc:
        if console:
            print(f"Error de configuración: {exc}", file=sys.stderr)
        else:
            msg = f"Error de configuración, el servicio se detiene (código 2): {exc}"
            _early_service_log(args, msg)
            _event_log_error(msg)
        return EXIT_CONFIG
    SECRETS.add(settings.company_token, settings.group_token, settings.agency_token)
    log = setup_logging(settings.log_dir, settings.log_retention_days, console=console)
    log.info("=== Nexus DWH Agent (PostgreSQL) v%s%s ===", AGENT_VERSION, " · servicio" if not console else "")
    log.info("Servidor : %s | modo: %s", settings.api_url, settings.mode)
    log.info("Datos    : %s | Logs: %s", settings.data_dir, settings.log_dir)

    try:
        agent = Agent(settings)
    except ConfigError as exc:
        log.error("Configuración no válida: %s", exc)
        if not console:
            _event_log_error(f"Configuración no válida (código 2): {sanitize_error(exc)[1]}")
        return EXIT_CONFIG

    if sys.platform == "win32":  # pragma: no cover - solo Windows
        ok, detail = agent.store.self_check()
        (log.info if ok else log.error)("Cuenta: %s | protección de la credencial: %s%s", _windows_account(), detail,
                                        "" if ok else " (pruebe [agent] credential_scope = machine; ver §21.3)")

    if on_agent is not None:
        on_agent(agent)

    if install_signals and threading.current_thread() is threading.main_thread():
        def _on_signal(signum, _frame):
            log.info("Señal %s recibida: deteniendo el agente...", signum)
            agent.stop()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break / cierre de consola
            signal.signal(signal.SIGBREAK, _on_signal)

    try:
        agent.ensure_credential(force_enroll=args.enroll)
        return agent.run_once() if args.once else agent.run_forever()
    except FatalAgentError as exc:
        log.error("%s", exc)
        if not console and exc.exit_code:
            _event_log_error(f"Detenido (código {exc.exit_code}): {redact_text(str(exc))}")
        return exc.exit_code
    except (ConfigError, ApiError) as exc:
        code, msg = sanitize_error(exc)
        log.error("No se puede continuar: [%s] %s", code, msg)
        if not console:
            _event_log_error(f"No se puede continuar (código 2): [{code}] {msg}")
        return EXIT_CONFIG
    except Exception as exc:  # último recurso: salir con código ≠ 0 para que el servicio reinicie
        code, msg = sanitize_error(exc)
        log.exception("Error inesperado [%s] %s", code, msg)
        return 1


def _utf8_stdio() -> None:
    """El ejecutable corre en modo aislado (sin PYTHONIOENCODING/locale): salida UTF-8 sin excepciones."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - p. ej. sin consola (servicio)
            pass


def main(argv=None) -> int:
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.version:
        print(AGENT_VERSION)
        return 0
    if args.selftest:
        from . import selftest

        return selftest.run()
    if args.verify_update:
        from .updates import cli_verify

        return cli_verify(args.verify_update, AGENT_VERSION, allow_unsigned=args.allow_unsigned_manifest,
                          allow_same_version=args.allow_same_version)
    if args.service:
        from .winservice import run_service

        return run_service(args)
    return run_agent(args)
