"""
Nexus DWH — agente ETL v5 (variante PostgreSQL).

Punto de entrada (fuentes y ejecutable compilado ``NexusAgent.exe``). La lógica
vive en ``nexus_agent/`` (ver ``nexus_agent/cli.py``):

  * Identidad por instalación: enrolamiento con token (grupo/agencia/empresa) →
    credencial propia protegida con DPAPI; los tokens ya no son la credencial
    operativa.
  * GET /agent/tasks: el backend decide qué tareas ejecutar.
  * Ejecuciones con execution_id, carga atómica por tarea (una transacción),
    watermark = inicio de extracción confirmado solo tras el COMMIT.
  * Cola local persistente de reportes (SQLite) con reintentos y backoff.
  * Heartbeat independiente, timeouts en todos los drivers, logs saneados con
    rotación diaria.
  * Servicio de Windows con cuenta de mínimo privilegio, autodiagnóstico y
    validación de actualizaciones (DWH_README.md §21).

Uso:
  python client_postgres.py                 # bucle continuo en primer plano
  python client_postgres.py --once          # ejecuta lo vencido, vacía la cola y sale
  python client_postgres.py --enroll        # fuerza un enrolamiento nuevo
  python client_postgres.py --config RUTA   # otro config.ini
  NexusAgent.exe --service ...              # lo usa el Administrador de servicios
  NexusAgent.exe --selftest                 # drivers / TLS / SQLite, sin red
  NexusAgent.exe --verify-update CARPETA    # valida un paquete nuevo
Códigos de salida: 0 ok, 1 error inesperado, 2 configuración, 3 credencial
revocada/inválida, 4 paquete de actualización rechazado.
"""

import sys

# Nunca escribir bytecode (__pycache__) junto al programa: en el ejecutable
# compilado no hay fuentes .py propias, y en modo fuentes evita dejar cachés.
sys.dont_write_bytecode = True

from nexus_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
