# MGD DWH

Sistema de Data Warehouse para Nexus. Incluye:

- **dwh_back**: API de configuración (FastAPI). Sirve configuraciones a los clientes.
- **dwh_client**: Cliente ETL que extrae de SQL Server y carga en MySQL DWH.
- **dwh_api**: Monitor API y CLI para monitorear ejecuciones y errores.
- **dwh_front**: Panel web (Next.js) para administrar la configuración (grupos, empresas, agencias, catálogo, tareas) y ver el monitor. Requiere la variante PostgreSQL del backend.

Esquema de la BD de configuración (PostgreSQL): `dwh_back/schema_postgres.sql`. Guía completa: [DWH_README.md](DWH_README.md) (el panel web está en la sección 16).

Agente PostgreSQL v5 en producción: compilado con Nuitka (`NexusAgent.exe`, sin fuentes), instalado como servicio de Windows de mínimo privilegio y con actualizaciones validadas — ver [DWH_README.md §21](DWH_README.md). Resumen de entrega del endurecimiento (fases 1–5), variables, migraciones, pruebas, pendientes y límites: [ENTREGA_ENDURECIMIENTO.md](ENTREGA_ENDURECIMIENTO.md).

## Configuración

Copia `config.ini.example` como `config.ini` en cada carpeta y ajusta las credenciales.

## Requisitos

- Python 3.10+
- MySQL
- SQL Server (para el cliente)
- ODBC Driver for SQL Server
