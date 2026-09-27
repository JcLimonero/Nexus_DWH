"""
client_postgres.py  v5 — Agente ETL Nexus DWH (PostgreSQL)
──────────────────────────────────────────────────────────
Cliente ETL OFICIAL de la variante PostgreSQL (reemplaza a client_last.py v4,
cuyas funciones — tablas con esquema "dwh.tabla", CREATE SCHEMA, static_columns,
lectura por bloques, Firebird — quedaron integradas en nexus_agent/etl.py).

Origen: SQL Server / Pervasive (ODBC), MySQL, PostgreSQL o Firebird.
Destino: PostgreSQL (DWH del grupo).

Novedades v5 (ver DWH_README.md):
  * Identidad por instalación: se enrola UNA vez con el token de config.ini
    (grupo / agencia / empresa) y opera con su propia credencial, guardada con
    DPAPI en Windows. Nexus decide qué tareas puede ejecutar.
  * Ejecuciones con execution_id, carga atómica por tarea (una transacción),
    watermark = inicio de extracción confirmado solo tras el COMMIT.
  * Cola local persistente de reportes (SQLite) con reintentos y backoff.
  * Heartbeat independiente, timeouts en todos los drivers, logs saneados con
    rotación diaria.

Uso:
  python client_postgres.py                 # servicio (bucle continuo)
  python client_postgres.py --once          # ejecuta lo vencido, vacía la cola y sale
  python client_postgres.py --enroll        # fuerza un enrolamiento nuevo
  python client_postgres.py --config RUTA   # otro config.ini
Códigos de salida: 0 ok, 1 error inesperado, 2 configuración, 3 credencial revocada/inválida.
"""

import argparse
import signal
import sys

from nexus_agent import AGENT_VERSION
from nexus_agent.agent import EXIT_CONFIG, Agent, FatalAgentError
from nexus_agent.api import ApiError
from nexus_agent.logsetup import setup_logging
from nexus_agent.sanitize import SECRETS, ConfigError, sanitize_error
from nexus_agent.settings import load_settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agente ETL Nexus DWH (PostgreSQL)")
    parser.add_argument("--config", help="Ruta a config.ini (defecto: junto al ejecutable)")
    parser.add_argument("--data-dir", help="Carpeta de datos del agente (credencial, cola)")
    parser.add_argument("--enroll", action="store_true", help="Forzar un enrolamiento nuevo")
    parser.add_argument("--once", action="store_true", help="Ejecutar tareas vencidas una vez y salir")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(AGENT_VERSION)
        return 0

    try:
        settings = load_settings(args.config, args.data_dir)
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    SECRETS.add(settings.company_token, settings.group_token, settings.agency_token)
    log = setup_logging(settings.log_dir, settings.log_retention_days)
    log.info("=== Nexus DWH Agent (PostgreSQL) v%s ===", AGENT_VERSION)
    log.info("Servidor : %s | modo: %s", settings.api_url, settings.mode)
    log.info("Datos    : %s | Logs: %s", settings.data_dir, settings.log_dir)

    try:
        agent = Agent(settings)
    except ConfigError as exc:
        log.error("Configuración no válida: %s", exc)
        return EXIT_CONFIG

    def _on_signal(signum, _frame):
        log.info("Señal %s recibida: deteniendo el agente...", signum)
        agent.stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break / parada de consola
        signal.signal(signal.SIGBREAK, _on_signal)

    try:
        agent.ensure_credential(force_enroll=args.enroll)
        return agent.run_once() if args.once else agent.run_forever()
    except FatalAgentError as exc:
        log.error("%s", exc)
        return exc.exit_code
    except (ConfigError, ApiError) as exc:
        code, msg = sanitize_error(exc)
        log.error("No se puede continuar: [%s] %s", code, msg)
        return EXIT_CONFIG
    except Exception as exc:  # último recurso: salir con código ≠ 0 para que el servicio reinicie
        code, msg = sanitize_error(exc)
        log.exception("Error inesperado [%s] %s", code, msg)
        return 1


if __name__ == "__main__":
    sys.exit(main())
