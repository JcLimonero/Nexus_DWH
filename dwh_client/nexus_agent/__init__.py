"""
nexus_agent — núcleo del agente ETL Nexus DWH (variante PostgreSQL).

El punto de entrada es ``dwh_client/client_postgres.py`` (compilado: ``NexusAgent.exe``);
este paquete contiene los módulos testeables:

  sanitize    saneamiento de errores y textos (logs y reportes)
  settings    lectura de config.ini
  logsetup    logging con rotación diaria y redacción
  credstore   credencial de instalación protegida (DPAPI / archivo 0600)
  localstate  SQLite local: cola de reportes, agent_seq, checkpoints, agenda
  api         cliente HTTP de /agent/* (TLS verificado, reintentos)
  etl         extracción / transformación / carga (una transacción por tarea)
  agent       orquestación: scheduler, worker, heartbeat, envío de la cola, inventario
  inventory   inventario estructural de solo lectura (sección 19)
  cli         línea de comandos (consola, --service, --selftest, --verify-update)
  winservice  servicio de Windows (pywin32) con parada ordenada (sección 21)
  selftest    autodiagnóstico sin red ni BD (drivers, TLS, SQLite)
  updates     manifiesto de publicación y validación de actualizaciones (Ed25519 + SHA-256)
  authenticode  estado de la firma Authenticode (WinVerifyTrust, solo Windows)
  release_keys  claves PÚBLICAS de publicación confiables (compiladas en el ejecutable)
"""

AGENT_VERSION = "5.2.0"
