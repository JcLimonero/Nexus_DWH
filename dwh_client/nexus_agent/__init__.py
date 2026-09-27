"""
nexus_agent — núcleo del agente ETL Nexus DWH (variante PostgreSQL).

El punto de entrada sigue siendo ``dwh_client/client_postgres.py``; este paquete
contiene los módulos testeables:

  sanitize    saneamiento de errores y textos (logs y reportes)
  settings    lectura de config.ini
  logsetup    logging con rotación diaria y redacción
  credstore   credencial de instalación protegida (DPAPI / archivo 0600)
  localstate  SQLite local: cola de reportes, agent_seq, checkpoints, agenda
  api         cliente HTTP de /agent/* (TLS verificado, reintentos)
  etl         extracción / transformación / carga (una transacción por tarea)
  agent       orquestación: scheduler, worker, heartbeat, envío de la cola
"""

AGENT_VERSION = "5.0.0"
