# Nexus DWH — Guía general

Documento único para entender y operar el **stack DWH de Nexus**:

- `dwh_back/` — servidor de **configuración + monitor** (FastAPI).
- `dwh_client/` — **cliente ETL** que corre en cada sede y carga datos al DWH.
- `dwh_api/` — app de **monitoreo** (consume los endpoints `/monitor/*` del backend).
- `dwh_front/` — **panel web de administración** (Next.js) para dar de alta grupos, empresas, agencias, catálogo y tareas, y ver el monitor (solo variante PostgreSQL; ver sección 16).
- Distribución del agente (Nuitka, servicio de Windows, firma y actualizaciones): sección 21. Destino (DWH) configurable por grupo/empresa, esquema, SSL/TLS y "Probar conexión": sección 22. Despliegue del backend y el panel en contenedores (Coolify, configuración por variables `NEXUS__<SECCION>__<CLAVE>`): sección 23. Resumen de entrega del endurecimiento (fases 1–5): [ENTREGA_ENDURECIMIENTO.md](ENTREGA_ENDURECIMIENTO.md).
- **Encriptación de secretos** con Fernet (opcional, recomendada en producción).

El stack existe en **dos variantes** equivalentes:

| Variante | BD de **configuración** | Archivos principales |
|----------|------------------------|----------------------|
| MySQL    | MySQL / MariaDB        | `dwh_back/main.py`, `dwh_client/client.py` |
| PostgreSQL | PostgreSQL           | `dwh_back/main_postgres.py`, `dwh_client/client_postgres.py` (agente v5 + paquete `dwh_client/nexus_agent/`) |

Elige una según la instalación; los ejemplos de `config.ini` y los scripts SQL están duplicados con sufijo `_postgres` cuando corresponde.

---

## 1. Qué hace el sistema (resumen funcional)

1. **Centraliza** las conexiones de origen (SQL Server/ODBC del DMS) y destino (DWH MySQL) de todas las razones sociales / agencias en **una sola BD de configuración**.
2. Cada **cliente ETL** arranca con un solo dato: un **token** en su `config.ini`.
3. El cliente pide al **backend** `/configs` (o `/group-configs`, `/agency-configs`) y recibe:
   - credenciales de origen y DWH,
   - lista de tareas (`extract_sql`, tabla destino, claves de upsert, programación, etc.).
4. El cliente **ejecuta** las tareas: extrae de origen, crea/ajusta la tabla destino en el DWH y hace el upsert.
5. El cliente **reporta** el resultado (`ok`/`error`) al backend con `/client-event`.
6. La app de **monitor** consulta `/monitor/*` para ver el estado de todos los clientes, los eventos y los errores pendientes de reconocer.

Beneficios principales: un único sitio donde cambiar credenciales/queries, visibilidad de qué cliente ETL funciona y cuál no, y auditoría de las peticiones HTTP al backend.

---

## 2. Arquitectura

```
+-------------------+          HTTPS / HTTP           +-------------------+
|   dwh_client      |  <---------------------------> |     dwh_back       |
| (en cada sede)    |   /configs, /group-configs,    |  FastAPI + uvicorn |
|                   |   /agency-configs,              |                   |
|                   |   /client-event,                |  Lee/escribe BD   |
|                   |   /configs/{id}/last_run        |  de configuración |
+---------+---------+                                +----+----+----------+
          | ODBC (SQL Server)                              |    |
          v                                                |    |
     Origen (DMS) ----extract----> DWH (MySQL/Postgres)     |    |
                                                            |    |
                                              /monitor/*    |    |
                                      +---------------------+    |
                                      |                          |
                                      v                          v
                               +--------------+        Tablas: client_events,
                               |   dwh_api    |        activity_log, (client_group|grupo),
                               |  (monitor)   |        (company|razon_social), agency/…
                               +--------------+
```

- **`dwh_back`** es **servicio de configuración y central de eventos**, no procesa datos; solo conecta a la BD de configuración.
- **`dwh_client`** sí procesa datos: se conecta a **origen** (ODBC/DSN) y a **DWH** con las credenciales que le entrega el backend.
- **`dwh_api`** es opcional: un frontal/monitor para revisar estado de clientes y alertas.

---

## 3. Modos de operación del cliente (tokens)

El cliente ETL tiene **tres modos** según qué token(s) tenga configurados. Prioridad de mayor a menor: **group > agency > company**.

| Modo | Token en `config.ini` | Endpoint que llama | Qué ejecuta |
|------|----------------------|--------------------|-------------|
| **Grupo** | `group_token` | `GET /group-configs` con `x-group-token` | Todas las tareas activas de **todas** las companies del grupo |
| **Agencia** | `agency_token` | `GET /agency-configs` con `x-agency-token` | Solo tareas ligadas a esa agencia |
| **Company** (legado/por defecto) | `token` | `GET /configs` con `x-token` | Todas las tareas de la company (todas sus agencias) |

> **Variante PostgreSQL con agente v5:** estos tokens pasan a ser **tokens de enrolamiento**: el agente los usa una sola vez para darse de alta (`POST /agent/enroll`) y después opera con su propia credencial de instalación. Los endpoints de la tabla siguen activos solo por compatibilidad con agentes v3/v4 (**obsoletos**; ver sección 17). En todo el backend la prioridad es la misma: **grupo > agencia > empresa**.

Deja vacíos los modos que no uses. El flujo por grupo requiere columna `client_group.group_token`; el de agencia requiere `agency.agency_token` (ambas columnas las crea/agrega `dwh_back/schema_postgres.sql`).

> En la variante **MySQL** el `main.py` implementa hoy solo el flujo company (`/configs`). La variante **PostgreSQL** (`main_postgres.py`) implementa **los tres**.

---

## 4. Esquema de la BD de configuración

La BD de configuración tiene dos “nombres” porque hubo un rename de español a inglés en la variante PostgreSQL (ver `dwh_back/english_name_mapping.md`).

Tablas clave (nombres en **inglés** / **legado español**):

| Rol | Inglés | Legado |
|-----|--------|--------|
| Grupo de empresas | `client_group` | `grupo` |
| Razón social / empresa | `company` | `razon_social` |
| Sede / agencia | `agency` | `agencia` |
| Catálogo de objetos a cargar | `object_catalog` | `catalogo_objeto` |
| Tareas (objeto por agencia) | `agency_task` | `agencia_objeto` |
| Auditoría HTTP | `activity_log` | igual |
| Eventos reportados por el cliente | `client_events` | igual |

Campos sensibles (candidatos a cifrado `ENC:`):

- `company.source_host`, `source_database`, `source_username`, `source_password`, `source_dsn`.
- `client_group.warehouse_host`, `warehouse_database`, `warehouse_username`, `warehouse_password`.

Los puertos y flags no se cifran.

---

## 5. Encriptación de secretos

### 5.1. Qué es

Los campos sensibles de la BD de configuración pueden guardarse en **texto plano** o **cifrados** con **Fernet** (AES‑128 en modo CBC + HMAC SHA‑256, de la librería `cryptography`). Un valor cifrado se almacena con el prefijo **`ENC:`**:

```
ENC:gAAAAABl7sa...cadena-fernet...
```

Cuando el backend devuelve esos campos (p. ej. en `/configs`), los **descifra al vuelo** si tiene la clave; si no, el valor **se devuelve tal cual** (útil cuando no hay nada cifrado).

### 5.2. Clave maestra (`config_secret_key`)

La clave es una cadena **Fernet base64 urlsafe de 32 bytes** (se genera con `Fernet.generate_key()`). Se entrega al backend por **una** de estas dos vías, con esta prioridad:

1. `config.ini` del backend:
   ```ini
   [security]
   config_secret_key = TU_FERNET_KEY_AQUI
   ```
2. Variable de entorno **`NEXUS_CONFIG_SECRET_KEY`**.

Si no hay clave y **no hay valores `ENC:`** en la BD → la app funciona normal. Si hay valores `ENC:` y **no** hay clave → `RuntimeError` al leerlos (y fallo del endpoint `/configs`).

### 5.3. Generar una clave

Desde Python:

```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

Guarda esa cadena como `config_secret_key` o como variable de entorno. **No la pierdas**: sin ella no se puede descifrar lo ya cifrado.

### 5.4. Cifrar / descifrar valores

Se usa el script `dwh_back/encrypt_config_secret.py` (o el `.exe` compilado), que lee la misma clave del `config.ini` o de la variable de entorno:

```
# cifrar una cadena concreta
python encrypt_config_secret.py "MiPasswordSQL"

# cifrar en modo interactivo (varios valores seguidos)
python encrypt_config_secret.py

# descifrar
python encrypt_config_secret.py -d "ENC:gAAAAAB..."
python encrypt_config_secret.py --decrypt
```

Salida cifrada siempre con el prefijo `ENC:`. Luego la copias dentro del campo en la tabla de configuración (p. ej. `source_password`).

### 5.5. Qué descifra el backend

En **`dwh_back/main.py`** (MySQL) la función `check_token_status()` envuelve con `decrypt_config_secret(...)` los campos:

- `dsn_odbc`, `origen_ip`, `origen_db`, `origen_user`, `origen_pass`
- `dwh_host`, `dwh_db`, `dwh_user`, `dwh_pass`

En **`dwh_back/main_postgres.py`** (PostgreSQL) hace lo equivalente con los nombres en inglés (`source_*`, `warehouse_*`).

Los **clientes ETL** reciben el valor **ya descifrado** por HTTPS. La seguridad adicional consiste en que, aunque alguien acceda a la BD de configuración, **no verá credenciales en claro**.

### 5.6. Recomendaciones de despliegue

- Usa **HTTPS** en `api_url` fuera de `localhost`.
- Guarda la clave preferentemente en **variable de entorno** del servicio (`NEXUS_CONFIG_SECRET_KEY`) para no dejarla en disco.
- Rotación: si cambias la clave, **descifra antes** con la clave antigua y **vuelve a cifrar** con la nueva.
- El cliente ETL **no** necesita la clave Fernet; solo su token.

---

## 6. Estructura del backend (`dwh_back`)

### 6.1. Archivos principales

- `main.py` — Servidor (**MySQL**). Endpoints `/configs`, `/configs/{id}/last_run`, `/client-event`, `/monitor/*`.
- `main_postgres.py` — Servidor (**PostgreSQL**). Además expone `/agency-configs`, `/group-configs`.
- `admin_postgres.py` — API de administración `/admin/*` (CRUD de la configuración; la monta `main_postgres.py`).
- `agent_postgres.py` — API del agente por instalación `/agent/*`, `/admin/installations|executions|sync-state|legacy-clients` y `/monitor/installations` (sección 17).
- `health_postgres.py` — salud, incidencias y notificaciones: evaluador periódico, ganchos de incidencias del API del agente, `/admin/health/*`, `/admin/incidents*`, `/admin/notification-*` (sección 18).
- `inventory_postgres.py` — inventario estructural y cambios de estructura: `/agent/inventory/*`, `/admin/monitored-databases*`, `/admin/structural-changes*`, `/admin/inventory/summary` (sección 19).
- `redact.py` — saneamiento de textos del backend (errores, detalle de eventos, logs).
- `panel_auth.py` — usuarios del panel: argon2id, sesiones, permisos por grupo, auditoría (sección 20).
- `users_postgres.py` — `/admin/auth/*`, `/admin/users*`, `/admin/roles`, `/admin/sessions*`, `/admin/audit`.
- `manage_users.py` — CLI: primer superadministrador, reinicio de contraseña, desbloqueo, desactivación.
- `db_pool.py` — pool de conexiones acotado. `ratelimit.py` — límites de tasa en memoria.
- `schema_postgres.sql` — esquema **base idempotente** de la BD de configuración PostgreSQL.
- `migrate.py` + `migrations/NNN_*.sql` — migraciones ordenadas (sección 17.8).
- `tests/` — pruebas automatizadas (pytest) del backend.
- `seed_dev_postgres.sql` — datos ficticios **solo para desarrollo local**.
- `encrypt_config_secret.py` — utilidad de cifrado/descifrado Fernet.
- `requirements.txt` / `requirements_postgres.txt` — dependencias Python.
- `config.ini.example`, `config_postgres.ini.example` — plantillas de configuración.
- `*.sql` — scripts de esquema y migraciones (ver `english_name_mapping.md`).
- `*.spec` — plantillas PyInstaller para construir ejecutables `.exe`.
- `run_server.py` — lanzador alternativo del servidor.

### 6.2. `config.ini` del backend (MySQL)

```ini
[database]
host = 127.0.0.1
port = 3306
db = mgd_dwh_config
user = root
password = TU_PASSWORD_AQUI

[monitor]
; Token de autorización para /monitor/*
token = TU_MONITOR_TOKEN

[security]
; Opcional: clave Fernet para descifrar ENC:...
; También puede venir de la variable NEXUS_CONFIG_SECRET_KEY.
config_secret_key = TU_FERNET_KEY_AQUI
```

### 6.3. `config.ini` del backend (PostgreSQL)

```ini
[database]
host = 127.0.0.1
port = 5432
db = mgd_dwh_config
user = postgres
password = TU_PASSWORD_AQUI

[monitor]
token = TU_MONITOR_TOKEN

[security]
; config_secret_key = TU_FERNET_KEY_AQUI

[admin]
; El panel usa usuarios con sesión (sección 20). Token estático de emergencia
; (x-admin-token, también NEXUS_ADMIN_TOKEN): solo con allow_static_token = true.
; token = TU_ADMIN_TOKEN
; allow_static_token = false

[cors]
; Orígenes permitidos (coma). También: NEXUS_CORS_ORIGINS. Vacío (default) = sin CORS.
; El panel dwh_front usa un proxy del lado servidor y no necesita CORS.
; origins = https://otra-app.midominio.com
```

### 6.4. Endpoints (resumen)

Clientes ETL:

- `GET /configs` — header `x-token` (company). Devuelve credenciales + tareas.
- `GET /agency-configs` — header `x-agency-token` (solo PostgreSQL).
- `GET /group-configs` — header `x-group-token` (solo PostgreSQL).
- `PUT /configs/{id}/last_run` — el cliente marca una tarea como ejecutada.
- `POST /client-event` — el cliente reporta `ok`/`error` de una tarea. Desde v4 del backend: `config_id` debe pertenecer al alcance del token (si no **403**), `detail` se sanea y se recorta a 4000 caracteres y los errores 500 no devuelven el detalle interno.
- Los cinco anteriores son **legados** (agentes v3/v4). `[agent] legacy_endpoints = false` los apaga (410).

Agente v5 (solo PostgreSQL; detalle en la sección 17):

- `POST /agent/enroll`, `GET /agent/tasks`, `POST /agent/executions`, `PUT /agent/executions/{id}`, `POST /agent/heartbeat`, `POST /agent/events`, `POST /agent/credentials/rotate`, `GET /agent/whoami`.
- Inventario estructural (sección 19): `POST /agent/inventory/lease`, `POST /agent/inventory/snapshots`.
- Prueba de conexión (sección 22, agente 5.3): `POST /agent/connection-tests/claim`, `POST /agent/connection-tests/{id}/result`.

Monitor (todos requieren header `x-monitor-token`):

- `GET /monitor/events` — historial de eventos (filtros `event_type`, `only_unacknowledged`, `limit`).
- `GET /monitor/clients` — estado agregado por empresa (última conexión, errores pendientes…). Agrupa por `company_id` resuelto (incluye lo reportado con token de grupo/agencia o por instalaciones).
- `GET /monitor/installations` — instalaciones (agentes v5: alcance, estado, `last_seen_at`, versión, cola) + `legacy_clients` (agentes que aún usan tokens; últimos 30 días).
- `GET /monitor/activity` — log HTTP del backend (usualmente solo errores).
- `PUT /monitor/events/{id}/ack` — reconocer una alerta puntual.
- `PUT /monitor/events/ack-all` — reconocer todas las alertas pendientes.

Administración (solo PostgreSQL; **sesión de usuario** `Authorization: Bearer <token>` con permisos por grupo, sección 20; el token estático `x-admin-token` solo si `[admin] allow_static_token = true`; ver `admin_postgres.py`):

- Sesión y usuarios: `POST /admin/auth/login|logout|change-password`, `GET /admin/auth/me`, `/admin/users*`, `/admin/roles`, `/admin/sessions*`, `/admin/audit` (sección 20).
- Equivalentes de `/monitor/*` para el panel (con alcance): `GET /admin/events`, `PUT /admin/events/{id}/ack`, `PUT /admin/events/ack-all`, `GET /admin/clients`, `GET /admin/activity`.

- `GET /admin/whoami`, `GET /admin/stats` — validación del token y conteos para el dashboard.
- Grupos: `GET|POST /admin/groups`, `GET|PUT|DELETE /admin/groups/{id}`, `POST /admin/groups/{id}/enable|disable`, `POST /admin/groups/{id}/regenerate-token`, `DELETE /admin/groups/{id}/token`.
- Empresas: `GET|POST /admin/companies` (`?group_id=`), `GET|PUT|DELETE /admin/companies/{id}`, `POST .../enable|disable`, `POST .../regenerate-token`. Grupos y empresas aceptan `warehouse_schema`, `warehouse_sslmode`, `warehouse_sslrootcert`; empresas además `warehouse_mode` (`inherit|custom`) + `warehouse_*` y `warehouse_clear_password`; salida `effective_warehouse` (sección 22).
- Prueba de conexión (la ejecuta un agente): `POST /admin/connection-tests` (`{target_kind: group_dwh|company_dwh|company_source, group_id|company_id}`), `GET /admin/connection-tests/{id}`, `GET /admin/connection-tests?target_kind=&group_id=&company_id=&limit=` (sección 22.3).
- Agencias: `GET|POST /admin/agencies` (`?group_id=&company_id=`), `GET|PUT|DELETE /admin/agencies/{id}`, `POST .../enable|disable`, `POST .../regenerate-token`, `DELETE .../token`.
- Catálogo: `GET|POST /admin/objects` (`?group_id=&company_id=`), `GET|PUT|DELETE /admin/objects/{id}`, `POST .../enable|disable`.
- Instalaciones: `GET /admin/installations` (`?group_id=&status=`), `GET /admin/installations/{id}`, `POST .../revoke` (`{"reason"}`), `POST .../rotate` (marca rotación; el panel nunca ve el secreto).
- Ejecuciones: `GET /admin/executions` (`?group_id=&company_id=&agency_id=&task_id=&installation_id=&status=&failure_stage=&since=&until=&limit=`), `GET /admin/sync-state`, `GET /admin/legacy-clients`.
- Tareas: `GET|POST /admin/tasks` (`?group_id=&company_id=&agency_id=&object_catalog_id=`), `GET|PUT|DELETE /admin/tasks/{id}`, `POST .../enable|disable`, `POST /admin/tasks/{id}/reset-last-run`.

Reglas de la API admin:

- Los campos sensibles (`source_*`, `warehouse_*`) se guardan **cifrados `ENC:`** si el backend tiene `config_secret_key`; si no, en texto plano. Se puede pegar un valor ya cifrado (`ENC:...`) si el backend puede descifrarlo.
- Las **contraseñas nunca se devuelven** (solo `has_password`). En un `PUT`, contraseña vacía u omitida = se conserva; `clear_password: true` la borra. Host/BD/usuario sí se devuelven descifrados al admin.
- Los tokens (`group_token`, `company_token`, `agency_token`) se generan en el servidor (`secrets.token_urlsafe(32)`) y se pueden regenerar.
- Los errores de validación `422` de `/admin/*` no incluyen el valor recibido (para no registrar contraseñas en `activity_log`).
- Host/BD/usuario de origen y DWH y los tokens de enrolamiento solo se devuelven con `credentials.manage` sobre el grupo (si no: `null` + `secrets_hidden`/`token_hidden`).
- `PUT` es parcial (solo los campos enviados). Errores: `401` sesión inválida/expirada, `403` sin permiso (`permission_required`), `404` no existe **o fuera del alcance de grupos del usuario**, `409` nombre/token duplicado o registro con dependientes (no se borra en cascada: primero hay que borrar/mover los hijos), `422` validación (p. ej. objeto de otra empresa en una tarea).

Salud:

- `GET /health` → `{"status": "ok"}`.

### 6.5. Middleware

El backend registra cada petición en la tabla `activity_log` (método, endpoint, status, duración ms, IP, detalle de error si lo hubo). Esto alimenta `/monitor/activity`.

Desde la migración 001:

- **No** se guarda el token completo: solo un prefijo de 8 caracteres + ids resueltos (`group_id`, `company_id`, `agency_id`, `installation_id`) y `auth_kind` (`company|agency|group|installation|enroll|admin|monitor`). Lo mismo en `client_events`.
- El detalle de error es el cuerpo de la respuesta ≥400 **saneado** y recortado a 1000 caracteres. Las excepciones no controladas guardan solo `Error interno (Tipo)`; la traza (saneada) va al **stderr** del servidor.
- La escritura en BD se hace en un threadpool (`run_in_threadpool`): no bloquea el event loop.
- El token de monitor se compara en tiempo constante (`hmac.compare_digest`).
- Límite de tamaño del cuerpo de las peticiones (413): `[server] max_body_bytes` (defecto 1 MB), `agent_max_body_bytes` (256 KB para `/agent/*`) y `enroll_max_body_bytes` (16 KB para `/agent/enroll`, que no requiere credencial). Se aplica por `Content-Length` y también en cuerpos sin longitud (chunked).
- Los errores 422 de validación **nunca** devuelven el valor recibido (`input`), en ninguna ruta.

---

## 7. Estructura del cliente (`dwh_client`)

### 7.1. `config.ini`

```ini
[nexus]
; Token por company (flujo legado/por defecto)
token = TU_TOKEN_AQUI

; Token de grupo (opcional, prioridad mayor)
group_token =

; Token de agency (opcional, prioridad intermedia; solo PostgreSQL)
agency_token =

; URL base del backend. Usa HTTPS fuera de localhost.
api_url = http://127.0.0.1:8000
```

### 7.2. Qué hace en cada ciclo

> Descripción del cliente **legado** (MySQL `client.py` y agentes PostgreSQL v3/v4). El agente PostgreSQL **v5** (`client_postgres.py`) usa enrolamiento, `GET /agent/tasks`, ejecuciones con `execution_id`, cola local y heartbeat: ver **sección 17**.

1. **Arranque**: lee `config.ini` y escoge modo (group / agency / company).
2. **`fetch_configs`**: pide al backend sus credenciales y su lista de tareas.
3. Para cada tarea activa cuyo `schedule_seconds` haya vencido:
   - **Extrae** de origen con ODBC (DSN o `DRIVER={ODBC Driver 17 for SQL Server}` según `config`).
   - **Crea/ajusta** la tabla destino en el DWH (tipos inferidos; upsert keys → PRIMARY KEY).
   - **Inserta/upserta** las filas.
   - **`POST /client-event`** con `ok` + filas cargadas, o con `error` + traceback.
   - **`PUT /configs/{id}/last_run`** si terminó bien.
4. Entre ciclos duerme `refresh_seconds` (valor del backend).

### 7.3. Logs

Agente v5: `logs/nexus_agent.log` con **rotación diaria a medianoche** (`TimedRotatingFileHandler`; el archivo del día anterior queda como `nexus_agent.log.AAAA-MM-DD`); se conservan `log_retention_days` (defecto 7). Todo pasa por un formateador que sanea (sección 17.7): no se registra SQL ni valores de filas.

### 7.4. Driver ODBC

El cliente detecta el primer driver disponible de esta lista:

```
ODBC Driver 18 for SQL Server
ODBC Driver 17 for SQL Server
ODBC Driver 13 for SQL Server
SQL Server Native Client 11.0
SQL Server
```

Si la fuente usa un **DSN** configurado en Windows, se usa ese directamente.

---

## 8. Monitor (`dwh_api`)

App complementaria que consume `/monitor/*` con el token de monitor:

- `dwh_api/config.ini` con `api_url` y `token`.
- `nexus_monitor.py` expone una UI/consumo; ver el script para detalles.

> Nota: la variante hacia PostgreSQL del monitor se maneja desde el backend; el frontal es agnóstico mientras la URL y el token sean correctos.

---

## 9. Requisitos

### 9.1. Software

- **Python 3.11+** (recomendado).
- **MySQL 8** _o_ **PostgreSQL 14+** (la que uses como BD de configuración).
- **ODBC Driver 17+ para SQL Server** en las máquinas donde corre el cliente.
- **cryptography** (solo si vas a usar secretos `ENC:`; ya está en los `requirements*.txt`).

### 9.2. Dependencias Python

- `dwh_back/requirements.txt` (MySQL):
  - `fastapi`, `uvicorn`, `PyMySQL`, `cryptography`.
- `dwh_back/requirements_postgres.txt` (PostgreSQL):
  - `fastapi`, `uvicorn`, `psycopg2-binary`, `requests`, `cryptography`.
- `dwh_client/requirements.txt` (MySQL):
  - `pyodbc`, `PyMySQL`, `cryptography`, `requests`.
- `dwh_client/requirements_postgres.txt` (PostgreSQL, agente v5):
  - `pyodbc`, `pymysql`, `psycopg2-binary`, `requests`, `fdb` (Firebird sin DSN), `cryptography` (validación de actualizaciones), `pywin32` (solo Windows, servicio). Build: `requirements_build.txt` (Nuitka).
- `dwh_api/requirements.txt`:
  - `requests`.

### 9.3. Red y puertos

- Backend: por defecto escucha en `127.0.0.1:8000`. En producción usa `0.0.0.0` detrás de un **reverse proxy** HTTPS.
- Clientes: necesitan alcanzar el backend y **el origen** (SQL Server del DMS) y el **DWH** de su grupo.

---

## 10. Despliegue paso a paso

### 10.1. Crear la BD de configuración

- **MySQL**: crea la base y ejecuta los scripts `*.sql` correspondientes de `dwh_back/` para crear tablas e inserts iniciales.
- **PostgreSQL**: usa `dwh_back/schema_postgres.sql`. Es **idempotente** (no borra nada): sirve para instalaciones nuevas y para actualizar una BD existente (agrega `group_token`, `agency_token`, `run_on_company_token`, `static_columns` si faltan y amplía a `TEXT` los campos sensibles).
  ```
  psql -h HOST -U postgres -d mgd_dwh_config -f dwh_back/schema_postgres.sql
  ```
  Para desarrollo local puedes cargar además `dwh_back/seed_dev_postgres.sql` (datos ficticios).
  **Después** (y en cada actualización del backend) aplica las migraciones: `python migrate.py` (sección 17.8). Aplica también la línea base, así que en una BD nueva basta con `python migrate.py`.

### 10.2. Preparar el backend

```
cd dwh_back
python -m venv .venv
.\.venv\Scripts\activate        # (Windows) o source .venv/bin/activate
pip install -r requirements.txt         # MySQL
# o
pip install -r requirements_postgres.txt  # PostgreSQL

copy config.ini.example config.ini       # o cp en Linux
# edita host/port/user/password/monitor/security
```

Arranca:

```
# MySQL
python main.py --host 0.0.0.0 --port 8000

# PostgreSQL
python main_postgres.py --host 0.0.0.0 --port 8000
```

Verifica: `http://HOST:8000/health` → `{"status":"ok"}`.

### 10.3. Dar de alta una company/agencia

> En la variante PostgreSQL lo recomendado es hacerlo desde el **panel web** (`dwh_front`, sección 16), que genera los tokens y cifra las credenciales. Los pasos manuales equivalentes son:

1. Inserta `client_group`/`grupo` con `warehouse_host`, `warehouse_database`, etc.
2. Inserta `company`/`razon_social` con `source_host`, credenciales de origen y un `company_token` único.
3. Inserta `agency`/`agencia` por cada sede; opcionalmente con `agency_token`.
4. Define `object_catalog`/`catalogo_objeto`: tabla destino, `create_table_sql`, `upsert_keys`.
5. Asocia objetos a agencias en `agency_task`/`agencia_objeto` con el `extract_sql` concreto.

### 10.4. Cifrar secretos (opcional)

```
cd dwh_back
python encrypt_config_secret.py            # modo interactivo
# pega la password cuando te la pida -> copia el ENC:... a la BD
```

Repite para cada credencial sensible (`source_password`, `warehouse_password`, etc.).

### 10.5. Desplegar el cliente ETL

```
cd dwh_client
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt           # o _postgres

copy config.ini.example config.ini
# pon token, group_token o agency_token, y api_url
```

Ejecuta:

```
python client.py              # MySQL
python client_postgres.py     # PostgreSQL
```

> **Agente PostgreSQL (v5) en producción**: no se instala con Python ni con PyInstaller. Se distribuye compilado con **Nuitka** (`NexusAgent.exe`, sin fuentes) y se instala como **servicio de Windows con cuenta virtual de mínimo privilegio** con `scripts\install_service.ps1`; las actualizaciones se validan (firma del manifiesto, versión, SHA-256, Authenticode) con `scripts\update_agent.ps1`. Ver **sección 21**. Ejecutar con `python client_postgres.py` queda para desarrollo y pruebas. El cliente MySQL legado (`client.py`) sigue con PyInstaller/NSSM.

### 10.6. Desplegar el monitor (opcional)

```
cd dwh_api
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
copy config.ini.example config.ini
python nexus_monitor.py
```

---

## 11. Compilar a ejecutables

**Agente PostgreSQL v5**: se compila con **Nuitka** (`dwh_client/packaging/build_agent.ps1` en Windows o el job `agente-windows` del CI) → `dwh_client/build/dist/NexusAgent/`. Evaluación de herramientas, contenido del paquete, firma y actualizaciones: **sección 21**. **PyInstaller no protege el código** (empaqueta bytecode que se extrae y descompila en minutos): no se usa para el agente v5.

**Legado (PyInstaller)** — solo backend MySQL, utilidades, cliente MySQL `client.py` y monitor. Plantillas existentes en el repositorio: `dwh_client/mgd_client.spec` (compila `client.py`, MySQL), `dwh_back/mgd_server.spec` (`main.py`, MySQL) y `dwh_api/mgd_monitor.spec`. Las demás que se mencionaban antes:

- Backend MySQL: `mgd_server.exe.spec`, `mgd_server.spec`.
- Backend PostgreSQL: `mgd_server_postgres.exe.spec`, `mgd_server_postgres.spec`.
- Utilidad de cifrado: `encrypter.exe.spec`, `mgd_encrypt_config_secret.spec`.
- Cliente MySQL: `mgd_client.exe.spec`, `mgd_client.spec`.
- ~~Cliente PostgreSQL: `mgd_client_postgres.exe.spec`, `mgd_client_postgres.spec`~~ (reemplazado por Nuitka, sección 21).
- Monitor: `dwh_api/mgd_monitor.spec`.

Scripts auxiliares de build:

- `build_postgres.bat`, `build_postgres.ps1`.

Genera los `.exe` y despliega el `config.ini` **junto** al ejecutable (los scripts leen el INI desde la carpeta del `.exe` cuando están congelados).

---

## 12. Mantenimiento y troubleshooting

### 12.1. El cliente no recibe configuraciones

- `GET /configs` con `curl -H "x-token: TOKEN"` → ¿200 o 401/403?
  - `401` → token mal escrito o no existe en la BD.
  - `403` → grupo o company deshabilitado (`enabled = 0`).
- Revisa `activity_log` (endpoint `GET /monitor/activity`) para ver qué falla a nivel HTTP.

### 12.2. Error “Se encontró un secreto cifrado pero falta…”

Hay valores `ENC:` en la BD y el backend no tiene clave. Soluciones:

- Define `config_secret_key` o `NEXUS_CONFIG_SECRET_KEY`.
- Asegúrate de tener `cryptography` instalado.
- Reinicia el backend.

### 12.3. “No se pudo descifrar”

La clave cambió o el valor `ENC:` fue cifrado con **otra** clave. Restaura la clave correcta o vuelve a cifrar el valor con la clave actual.

### 12.4. Contraseña de Postgres perdida

Ver pasos en la sección 5 de este repo (y en cualquier guía de PostgreSQL): `trust` temporal en `pg_hba.conf` + `ALTER USER postgres WITH PASSWORD '...';` + revertir `pg_hba.conf`. Luego actualiza `dwh_back/config.ini` → `[database] password = ...`.

### 12.5. Driver JDBC para DBeaver/DataGrip

Si el IDE falla con “Maven artifact ... cannot be resolved”, descarga el JAR manualmente desde <https://jdbc.postgresql.org/download/> y adjúntalo como **Custom JAR** al driver PostgreSQL del IDE.

### 12.6. No se ven todas las bases en DBeaver

- Usa el nodo **Databases** dentro de la conexión (no los esquemas de `postgres`).
- Asegúrate de que el usuario tiene `CONNECT` a esas bases.
- Si hace falta, crea una conexión por base cambiando el campo `Database` en la pestaña **Main**.

---

## 13. Seguridad (checklist)

- [ ] Backend detrás de **HTTPS** con reverse proxy.
- [ ] `config_secret_key` en **variable de entorno**, no en texto plano.
- [ ] Todas las credenciales de origen/DWH guardadas como `ENC:` en la BD.
- [ ] Tokens (`company_token`, `group_token`, `agency_token`, `monitor_token`) con alta entropía (al menos 32 bytes aleatorios).
- [ ] `pg_hba.conf` / `GRANT` de la BD de configuración limitados a los hosts del backend.
- [ ] Logs del cliente (`dwh_client/logs/`) protegidos. El agente v5 ya no registra SQL ni valores de filas, pero sí nombres de tablas/tareas.
- [ ] Agentes actualizados a v5 y **tokens de enrolamiento borrados** de los `config.ini` tras enrolar; cuando no queden agentes legados, `[agent] legacy_endpoints = false`.
- [ ] Carpeta `agent_data` del agente con ACL solo para la cuenta del servicio y administradores.
- [ ] `api_url` del agente con HTTPS y `mode = production` (sin `allow_insecure_http`).
- [ ] Agente instalado como servicio con `install_service.ps1` (cuenta `NT SERVICE\NexusAgent`, sin privilegios de administrador, `C:\ProgramData\NexusAgent` sin acceso para Usuarios); logins de BD dedicados (origen solo lectura, DWH dueño solo de sus esquemas) (§21.3).
- [ ] Paquetes del agente con firma Authenticode y manifiesto firmado (Ed25519) cuando existan certificado y clave; `[agent] latest_version` del backend al día para ver versiones desactualizadas (§21.5–21.6).
- [ ] Política de Windows Error Reporting / volcados de memoria revisada por el cliente (§21.2).
- [ ] Panel: usuarios nominales con el rol mínimo y alcance por grupo; `[admin] allow_static_token = false`; revisar **Auditoría** periódicamente; `DWH_COOKIE_SECURE=true` detrás de HTTPS.
- [ ] Panel detrás de un proxy inverso (nginx) que **sobrescriba** `X-Real-IP` con `DWH_CLIENT_IP_HEADER=x-real-ip`, `DWH_PANEL_PROXY_KEY` = `[auth] panel_proxy_key` (aleatoria, distinta por entorno) y `DWH_PUBLIC_ORIGIN` con la URL pública; sin esto no hay límite de login por IP (§20.3). En Coolify/Traefik: `DWH_TRUSTED_PROXY_HOPS=1` y verificación con una IP falsa (§23.4).
- [ ] Contenedores (Coolify, §23): secretos (`NEXUS__DATABASE__PASSWORD`, `NEXUS_CONFIG_SECRET_KEY`, `NEXUS__AUTH__PANEL_PROXY_KEY`/`DWH_PANEL_PROXY_KEY`, `NEXUS__MONITOR__TOKEN`) **solo** como variables de Coolify (sin *Build Variable*), nunca en la imagen ni en el repo; `NEXUS__ADMIN__ALLOW_STATIC_TOKEN` sin definir (false); rol de PostgreSQL propio (no `postgres`) y `pg_hba.conf` limitado a la red Docker; 5432 no expuesto a Internet; `trusted_proxies`/`forwarded_allow_ips` limitados a la red de Coolify; backend y panel solo por HTTPS.

---

## 14. Glosario rápido

- **Group token**: token de grupo; un cliente ejecuta tareas de todas las companies del grupo.
- **Agency token**: token de sede; ejecuta solo sus tareas.
- **Company token**: token clásico de razón social.
- **`ENC:`**: prefijo que marca un valor cifrado con Fernet dentro de la BD de configuración.
- **`config_secret_key`**: clave maestra Fernet del backend para descifrar valores `ENC:`.
- **`activity_log`**: tabla con el log HTTP de cada petición al backend.
- **`client_events`**: tabla donde el cliente reporta `ok`/`error` de sus tareas.

---

## 15. Convenciones de rename (BD)

Si vas a trabajar con la variante PostgreSQL y aún ves nombres en español en la BD, consulta `dwh_back/english_name_mapping.md`. Allí está la tabla completa de equivalencias y el orden recomendado para ejecutar `migrate_config_spanish_to_english.sql` sin perder datos.

---

## 16. Panel web (`dwh_front`)

Panel de administración en **Next.js 14 (App Router) + TypeScript + Tailwind**, en español. Solo para la variante **PostgreSQL** (usa `/admin/*` de `main_postgres.py`).

El panel incluye además su propia **documentación de usuario** en `/docs` (menú **Documentación**, visible para cualquier usuario con sesión, sin permiso especial): tabla de contenido con búsqueda, guías «cómo hacer X» con los nombres exactos de botones/campos de cada pantalla y qué permiso requiere cada acción. El diagrama de flujo de esa página también existe como archivo estático en [`docs/diagramas/flujo-sistema.svg`](docs/diagramas/flujo-sistema.svg).

### 16.1. Qué permite

- **Dashboard**: conteos (grupos, empresas, agencias, tareas), **resumen de salud** (instalaciones por conectividad, tareas por estado, incidencias abiertas por severidad) y las incidencias abiertas más relevantes; estado por cliente ETL (`/monitor/clients`) y últimos errores sin reconocer (eventos legados).
- **Salud**, **Incidencias** y **Notificaciones**: ver sección 18.
- **Estructura**: inventario estructural, línea base, cambios pendientes y "Dar por entendido" (sección 19). El menú muestra su propio contador (cambios pendientes; rojo si hay bases que no se pudieron verificar), separado del de incidencias. El menú muestra un contador de incidencias abiertas **sin reconocer** (rojo si hay críticas/errores; se consulta cada 30 s).
- **Grupos / Empresas / Agencias**: alta, edición, baja, habilitar/deshabilitar; tokens con mostrar/copiar/regenerar (y revocar en grupo/agencia); contraseñas de **solo escritura**.
- **Detalle de grupo y de agencia** (`/grupos/[id]`, `/agencias/[id]`): los **extractores** (tareas) por agencia con su salud, alta de extractores desde el contexto y **clonado** a otras agencias (sección 16.5).
- **Catálogo de objetos**: tabla destino, `create_table_sql`, `upsert_keys`, constraint y `static_columns` (editores monoespaciados).
- **Tareas**: por agencia, `extract_sql`, `schedule_seconds` (con atajos), activa, modo empresa (`run_on_company_token`), duración esperada y tolerancia de retraso (opcionales, sección 18), última ejecución y reinicio de `last_run_at`; filtros por grupo/empresa/agencia; acción **Clonar a otras agencias**.
- **Eventos**: `/monitor/events` con filtros, reconocer uno o todos.
- **Actividad**: `/monitor/activity` (log HTTP).
- **Instalaciones**: agentes enrolados (alcance, estado, último contacto con semáforo, versión, cola, fallos 24 h), acciones **Rotar credencial** y **Revocar**; debajo, **clientes legados** que aún usan tokens.
- **Ejecuciones**: historial por intento con filtros (grupo/empresa/agencia, estado, etapa, instalación, tarea, fecha), filas leídas/cargadas/insertadas/actualizadas, duración, código de error, mensaje saneado y avisos.
- Las fechas de las tablas nuevas se guardan en **UTC** y se muestran en `America/Mexico_City` con la zona explícita (`NEXT_PUBLIC_DWH_TIMEZONE` para cambiarla).

### 16.2. Seguridad

- Login en `/login` con **usuario y contraseña** (sección 20). La ruta del servidor Next `POST /api/auth/login` llama a `POST /admin/auth/login` y guarda **solo el token opaco de sesión** en una cookie **httpOnly, SameSite=Strict** (Secure en producción); nunca la contraseña, y el token no llega al JavaScript del navegador. Si la contraseña debe cambiarse, el panel lleva a `/cambiar-contrasena` y el backend rechaza todo lo demás (403 `password_change_required`). Cerrar sesión revoca la sesión en el backend y borra la cookie.
- El navegador **nunca** habla directo con el backend: todo pasa por el proxy `app/api/dwh/[...path]` (solo `/admin/*`; `admin/auth/login|logout` bloqueados), que agrega `Authorization: Bearer <sesión>` y la IP real del navegador (`X-Forwarded-For`).
- **CSRF**: además de SameSite=Strict, toda petición que modifica (login, logout y el proxy) exige la cabecera `x-nexus-csrf: 1`, y se rechaza si `Origin` no es el del panel o `Sec-Fetch-Site` es de otro sitio (403). El origen esperado es `DWH_PUBLIC_ORIGIN` (recomendado en producción, p. ej. `https://panel.midominio.com`); sin él, el `Host` de la petición (nunca `X-Forwarded-Host`, que controla el cliente).
- IP del usuario hacia el backend: `x-nexus-client-ip` + clave `DWH_PANEL_PROXY_KEY` (sección 20.3); el panel no reenvía `X-Forwarded-For`.
- El panel ya **no** usa el token de monitor ni el de administrador (`/monitor/*` queda solo para el monitor legado `dwh_api`; el panel usa `/admin/events|clients|activity`).
- `middleware.ts` redirige a `/login` si no hay cookie; la validez real la decide el backend en cada llamada (401 → vuelve a `/login`).
- La UI oculta o deshabilita lo que el usuario no puede hacer (Reconocer, Dar por entendido, Reclasificar, Aprobar/Reiniciar línea base, Configurar, credenciales/tokens, Usuarios, Auditoría) según `GET /admin/auth/me`; el backend es la fuente de verdad y un 403 se muestra como "Sin permiso".

### 16.3. Variables de entorno (`dwh_front/.env.local`, no se versiona)

| Variable | Descripción |
|----------|-------------|
| `DWH_API_URL` | URL base del backend (p. ej. `http://127.0.0.1:8000`). |
| `DWH_COOKIE_SECURE` | Opcional (`true`/`false`). Por defecto `true` en producción. |
| `DWH_PUBLIC_ORIGIN` | Origen público del panel para la verificación CSRF de `Origin` (recomendado). |
| `DWH_PANEL_PROXY_KEY` | Clave compartida con `[auth] panel_proxy_key` del backend (IP real del usuario). |
| `DWH_CLIENT_IP_HEADER` / `DWH_TRUSTED_PROXY_HOPS` | De dónde sale la IP del navegador (sección 20.3). Sin ellas: desconocida. |

(`DWH_MONITOR_TOKEN` ya no se usa en el panel.)

Plantilla: `dwh_front/.env.example`.

### 16.4. Arranque local (desarrollo)

```
# 1) BD de configuración (Docker, puerto 5546)
docker run -d --name nexus_dwh_pg_dev -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=mgd_dwh_config -p 5546:5432 postgres:16-alpine
docker exec -i nexus_dwh_pg_dev psql -U postgres -d mgd_dwh_config < dwh_back/schema_postgres.sql
docker exec -i nexus_dwh_pg_dev psql -U postgres -d mgd_dwh_config < dwh_back/seed_dev_postgres.sql

# 2) Backend (config.ini con [database] port=5546, [monitor], [security], [admin])
cd dwh_back
python3.12 -m venv .venv && .venv/bin/pip install -r requirements_postgres.txt
.venv/bin/python main_postgres.py --port 8010

# 3) Primer usuario (no hay usuario por defecto)
.venv/bin/python migrate.py
.venv/bin/python manage_users.py create-superadmin --username mi_usuario

# 4) Panel
cd dwh_front
cp .env.example .env.local      # DWH_API_URL=http://127.0.0.1:8010
pnpm install
pnpm dev                        # http://localhost:3000
```

Producción: imagen Docker (`dwh_front/Dockerfile`, `output: "standalone"` → `node server.js`; sección 23) detrás de HTTPS. Sin Docker: `pnpm build` y `node .next/standalone/server.js` (copiando `.next/static` a `.next/standalone/.next/static`); `pnpm start` sigue funcionando con un aviso de Next. Comprobaciones: `pnpm typecheck`, `pnpm lint`, `pnpm build`.

### 16.5. Vista por grupo y por agencia; clonar extractores

En el panel las **tareas** (`agency_task`) se llaman **extractores**: un extractor = agencia + objeto del catálogo (por empresa) + SQL de extracción, programación y umbrales. El **servidor de origen** no es parte del extractor: sale de la **empresa** de la agencia.

**Grupo** (`/grupos/[id]`, desde la lista de Grupos): nombre, habilitado y destino DWH `host:puerto/base` (solo con `credentials.manage` sobre el grupo; si no, "Destino DWH oculto"); tarjetas de resumen (agencias, extractores activos, con error, retrasados, sin ejecutar; las de estado filtran al pulsarlas); empresas → agencias (secciones plegables, "Expandir/Contraer todo") y, por agencia, sus extractores: objeto → tabla destino, programación ("cada 15 min"), estado de salud (mismas etiquetas que Salud), última carga exitosa (con zona explícita), interruptor **Activo** (solo con `config.manage`; si no, insignia) y **Clonar**. Filtros por estado y búsqueda (extractor, tabla, agencia o `#id`) guardados en la URL (`?status=failing&q=…`). Botones **Nuevo extractor** (del grupo: se elige la agencia entre las del grupo; por agencia: preseleccionada). Las empresas sin agencias también se listan (sin filtros), con el aviso y el enlace **Nueva agencia** (abre el alta en Agencias con la empresa elegida, `?nueva=1`). Datos: `GET /admin/groups/{id}`, `/admin/companies?group_id=`, `/admin/agencies?group_id=`, `/admin/health/tasks?group_id=` (4 peticiones, sin N+1); se actualiza cada 30 s.

**Agencia** (`/agencias/[id]`, desde Agencias, Tareas, Salud o el detalle de grupo): migas Grupos › Grupo › Empresa › Agencia; datos de la agencia; resumen; tabla de extractores (estado, última carga exitosa, última ejecución, punto de sincronización, error actual y errores consecutivos con el desglose por instalación, programación, Activo, **Clonar**, **Editar**/Ver en solo lectura con el mismo formulario de Tareas, `components/task-form.tsx`); **Nuevo extractor** con la agencia preseleccionada y **Clonar a otras agencias** (varios extractores a la vez); debajo, incidencias abiertas de la agencia y las últimas 25 ejecuciones (con enlaces a Incidencias/Ejecuciones ya filtradas). Datos: `GET /admin/agencies/{id}`, `/admin/health/tasks?agency_id=`, `/admin/tasks?agency_id=`, `/admin/executions?agency_id=&limit=25`, `/admin/incidents?agency_id=&view=open`.

Un id inexistente **o de un grupo fuera del alcance** muestra "Grupo/Agencia no encontrada" (el backend responde 404 en ambos casos). En pantallas angostas las tablas se desplazan dentro de su tarjeta (barra siempre visible); la página no se desplaza a lo ancho.

**Clonar extractores** (muchos extractores son iguales entre agencias; solo cambia el servidor de origen):

| Método y ruta | Uso |
|---|---|
| `POST /admin/tasks/{id}/clone` | Un extractor → varias agencias |
| `POST /admin/agencies/{id}/clone-tasks` | Extractores de una agencia (todos o `task_ids`) → varias agencias |

Cuerpo: `target_agency_ids` (obligatorio), `copy_object_if_missing` (true), `enabled` (**false**: los clones se crean deshabilitados para revisarlos), `on_conflict` (`skip` | `update`), `overwrite_objects` (false), `dry_run` (vista previa sin guardar), y en el masivo `task_ids`. Por cada (extractor, agencia destino):

- **Objeto**: misma empresa → se reutiliza. Otra empresa → se busca por **nombre** en su catálogo: definición idéntica (tabla destino, `create_table_sql`, `upsert_keys`, constraint, `static_columns`) → se reutiliza; distinta → `object_conflict` (no se toca) salvo `on_conflict = update` **y** `overwrite_objects = true` (se sobrescribe la definición: afecta a todas las agencias de esa empresa); inexistente → se copia si `copy_object_if_missing` (aviso `static_columns_review`: las columnas estáticas, p. ej. `dn`, suelen ser propias de cada empresa) o error `object_missing`.
- **Tarea**: si la agencia ya tiene ese objeto → `skipped_exists` (`skip`) o `updated` (`update`: SQL, programación, modo empresa y umbrales; conserva `is_active` y `last_run_at`); si no → `created` con `is_active = enabled`, sin `last_run_at` (primera carga completa); `query_version`/`query_hash` los fija el trigger.
- **Permisos**: `config.manage` en el grupo de origen **y** en el de cada destino. Un destino inexistente o fuera del alcance devuelve `not_found` sin nombre ni grupo; visible sin permiso → `permission_required`. La propia agencia de origen → `same_agency`.
- Cada destino va en su **propio SAVEPOINT**: uno que falla no deshace los demás. Respuesta: `results` (por destino: `status` created/updated/skipped_exists/object_conflict/error, `code`, `task_id`, `object_action` reused/created/updated/conflict/missing, `affected_tasks` = otros extractores de la empresa destino que usan ese objeto cuando hay conflicto o sobrescritura, `target_disabled` + aviso si la agencia/empresa/grupo destino está deshabilitado —el clon no se ejecutará—, `warnings`, `message`) y `summary`.
- Máximo **2000** combinaciones (extractores × agencias) por petición → 422 con mensaje. Los destinos se resuelven en una sola consulta.
- **Vista previa** (`dry_run`): no escribe nada ni consume secuencias; simula en memoria (un objeto que se copiaría a una empresa se reutiliza para sus demás agencias, igual que al ejecutar). Los mensajes van en futuro ("se omitirá").
- **Auditoría**: `tasks.clone` / `agencies.clone_tasks` en `panel_audit_log` con totales, listas acotadas a 100 ids (extractores de origen, agencias y grupos destino, extractores escritos), opciones y resumen (sin SQL). La vista previa no se audita. En general, `details` de la auditoría siempre es JSON válido ≤ 4000 caracteres (si no cabe se recortan las listas con `*_total` y `truncated: true`) y, si el registro fallara, se guarda una fila mínima: nunca se pierde.
- `GET /admin/tasks?light=true` devuelve solo `id`, `agency_id`, `group_id`, `object_catalog_id`, `object_name` (sin SQL); lo usa el diálogo para marcar "Ya lo tiene".

Panel: acción **Clonar** en cada extractor (Tareas, detalle de grupo y de agencia) y **Clonar a otras agencias** en el detalle de agencia (con casillas para elegir extractores). El diálogo lista las agencias destino permitidas agrupadas por grupo/empresa, con búsqueda y la marca "Ya lo tiene"; opciones; **Vista previa** (obligatoria antes de confirmar) y tabla de resultados.

Pruebas: `dwh_back/tests/test_clone_tasks.py` (misma empresa con objeto reutilizado y clon deshabilitado; otra empresa con objeto copiado y aviso de columnas estáticas; objeto idéntico reutilizado; objeto distinto → conflicto, y sobrescritura solo con confirmación; `skip` vs `update` con `query_version`; permiso en origen pero no en destino sin revelar el otro grupo; cruce de grupos con permiso en ambos; vista previa sin cambios ni auditoría; auditoría; clonado masivo con subconjunto) y `test_panel_auth.py::test_vista_grupo_y_agencia_por_alcance` (detalle 404 y listas filtradas vacías fuera del alcance).

---

## 17. Agente v5 (PostgreSQL): identidad, ejecuciones, cola y heartbeat

El cliente oficial de la variante PostgreSQL es `dwh_client/client_postgres.py` (v5). `client_last.py` (v4) se eliminó: sus funciones (tablas `esquema.tabla`, `CREATE SCHEMA`, `static_columns`, lectura por bloques, Firebird) quedaron integradas en `dwh_client/nexus_agent/etl.py`. El v5 **requiere** el backend con las migraciones 001–003 aplicadas; los agentes v3/v4 siguen funcionando contra el backend nuevo (sección 17.9).

### 17.1. Identidad por instalación y enrolamiento

1. En `config.ini` del agente se pone **un** token de enrolamiento (`group_token`, `agency_token` o `token`; prioridad grupo > agencia > empresa).
2. En el primer arranque (o con `--enroll`) el agente llama `POST /agent/enroll` con ese token y datos no sensibles de la máquina (nombre, hostname, SO, versión). El backend crea una fila en `installation` con el **alcance** del token (grupo, empresa o agencia) y devuelve `installation_id` + `secret` **una sola vez**. En BD solo queda `sha256(secret)`; la verificación usa `hmac.compare_digest`.
3. Desde ahí el agente se autentica con `x-installation-id` + `x-installation-secret` (o `Authorization: Bearer <id>.<secret>`). El token de enrolamiento ya **no** es la credencial operativa: **bórrelo de config.ini** tras enrolar (el agente lo recuerda en el log).
4. El backend resuelve las tareas autorizadas **solo** a partir del alcance de la instalación; cualquier `task_id` que mande el agente debe pertenecer a ese alcance (si no, **403** `task_out_of_scope`). Para alcance empresa se respeta `run_on_company_token`.
5. Cada enrolamiento crea una instalación nueva (la misma máquina re-enrolada aparece dos veces; revoque la anterior).

**Dónde guarda la credencial el agente** (`<data_dir>/`, defecto `agent_data/` junto a `config.ini`):

- **Windows**: `agent_credential.dpapi`, cifrado con **DPAPI** (`CryptProtectData` vía ctypes, con entropía propia). `[agent] credential_scope = user` (defecto): solo la cuenta que ejecuta el servicio puede descifrar; si cambia la cuenta del servicio hay que re-enrolar. `machine`: cualquier proceso de esa máquina puede descifrar. **Límites**: un administrador local o cualquier código que corra como la cuenta del servicio puede descifrarla; DPAPI protege contra copiar el archivo a otra máquina/cuenta, respaldos y lecturas casuales, no contra un administrador. Complementar con ACL de la carpeta.
- **Otros SO (desarrollo)**: `agent_credential.json` con permisos **0600** y un aviso en el log (no está cifrado).
- La credencial queda **ligada a `api_url`**: si `config.ini` apunta a otro servidor, el agente se niega a enviar el secreto (hay que re-enrolar con `--enroll`).

**Revocar / rotar** (panel → Instalaciones, o API admin):

- **Revocar** (`POST /admin/installations/{id}/revoke`): la instalación recibe **401** `installation_revoked`, el agente se detiene (código de salida 3) y **no** se re-enrola solo. Para volver: `client_postgres.py --enroll` con un token de enrolamiento vigente (si el token pudo filtrarse, regenérelo antes en el panel).
- **Rotar** (`POST /admin/installations/{id}/rotate`): marca `rotation_required`. En su siguiente contacto (tareas o heartbeat) el agente ve `credential_rotation_required: true`, llama `POST /agent/credentials/rotate` (autenticado con el secreto **vigente**), **guarda primero** el secreto nuevo y luego lo usa. El panel nunca ve secretos.
  - El secreto anterior sigue valiendo hasta que el agente use el nuevo **o** hasta un plazo fijo de `rotation_grace_seconds` (defecto 3600) contado desde la rotación; ese plazo **nunca se extiende**.
  - Si la respuesta de la rotación se pierde, el agente (que sigue con el secreto anterior) vuelve a llamar a rotate y recibe **el mismo** secreto nuevo (re-entrega idempotente: el secreto nuevo se guarda cifrado con una clave derivada del anterior, así que solo quien tiene el anterior puede recuperarlo). Con el secreto anterior **no** se pueden emitir secretos adicionales: quien solo tenga un secreto filtrado no puede encadenar rotaciones ni dejar fuera al agente legítimo, y pierde el acceso al vencer la gracia o cuando el agente use el nuevo. Si hay sospecha de filtración, lo correcto es **revocar** y re-enrolar.
  - Cuando el agente empieza a usar el secreto nuevo, el anterior deja de valer al instante. Si un hilo tenía una petición en vuelo con el anterior y recibe 401, el agente la reintenta una vez con la credencial vigente. Solo `installation_revoked` detiene el agente; otros 401 se registran y se reintentan con backoff (sin iniciar tareas, porque la autorización caduca).

### 17.2. API `/agent/*`

| Método y ruta | Uso |
|---|---|
| `POST /agent/enroll` | Alta con token de enrolamiento (header `x-group-token` / `x-agency-token` / `x-token`). 201 → `installation_id`, `secret`, `scope`. |
| `GET /agent/whoami` | Identidad y alcance. |
| `GET /agent/tasks` | Tareas autorizadas + credenciales de origen (por empresa) y DWH descifradas (general del alcance y, desde 5.3, `warehouse` **efectivo por tarea** con esquema y SSL; tareas retenidas a agentes anteriores en `withheld_tasks`, sección 22.4), `query_version`, `query_hash`, estado de sync (`watermark`, `watermark_kind`, `last_success_at`, `watermark_reset_at`…), `refresh_seconds`, `config_max_age_seconds`, `credential_rotation_required`. Cada entrega se audita en `task_download_log` (instalación, tarea, versión, IP, fecha; **sin SQL**). |
| `POST /agent/executions` | Inicio de ejecución, idempotente por `execution_id` (UUID generado por el agente). |
| `PUT /agent/executions/{id}` | Avance/fin (`running|success|failed|interrupted`), filas, etapa de fallo, código y mensaje saneado, avisos, `checkpoint {watermark, kind}`. Idempotente; ver 17.3. |
| `POST /agent/heartbeat` | Latido (sección 17.6). |
| `POST /agent/events` | Eventos genéricos (`queue_overflow`, `agent_started`, `agent_stopping`, `config_stale`, `credential_rotated`, `warning`, `dead_letter`), deduplicados por `event_id`. |
| `POST /agent/credentials/rotate` | Rotación del secreto (la pide el agente). |
| `POST /agent/connection-tests/claim` | (5.3) Toma una prueba de conexión pendiente de su alcance (sección 22.3). |
| `POST /agent/connection-tests/{id}/result` | (5.3) Resultado saneado de la prueba (solo la instalación que la tomó). |

Errores con cuerpo `{"detail": {"code": "...", "message": "..."}}`: `missing_credentials`, `invalid_credentials`, `installation_revoked` (401); `scope_disabled`, `task_out_of_scope` (403); `execution_conflict` (409). `/agent/*` no expone CORS.

### 17.3. Ejecuciones y estado de sincronización

Son conceptos separados:

- `task_execution`: **una fila por intento**. `execution_id` lo genera el agente; grupo/empresa/agencia/objeto los resuelve el servidor. Guarda versión del cliente y de la query, `attempt`, `started_at`/`finished_at`/`duration_ms` (UTC, `timestamptz`), `status`, `failure_stage` (`config|extract|transform|load|report`), `rows_read`, `rows_loaded`, `rows_inserted`/`rows_updated` (medidos con `RETURNING (xmax = 0)` en el upsert de PostgreSQL), `error_code`, `error_message_sanitized` (≤1000), `warnings`, `checkpoint_confirmed`, `event_time` (reloj del agente) y `received_at` (servidor).
- `task_sync_state` (una fila por tarea): `watermark` confirmado, `last_success_at`, `last_failure_at`, `last_execution_id`, `last_status`, `consecutive_failures`, `current_error_code`, instalación que lo actualizó.

Reglas del servidor:

- Reenvíos del mismo reporte (misma ejecución, `agent_seq` ≤ al ya aplicado) se **ignoran** (`status: ignored`). Un estado terminal **no** se sobrescribe.
- `agent_seq` es una secuencia monotónica **por instalación**, persistida por el agente en su SQLite. El estado de sync solo lo cambia una ejecución **más nueva** que la última aplicada: misma instalación → mayor `agent_seq` de inicio; otra instalación → mayor `started_at`. Un fallo viejo que llega tarde **no pisa** una recuperación más nueva (nunca se usa la hora de llegada).
- El watermark solo **avanza** (`GREATEST`) y solo con `status = success`. Por compatibilidad también se copia a `agency_task.last_run_at`.
- "Reiniciar última ejecución" en el panel pone el watermark en NULL y fija `watermark_reset_at`: se ignoran checkpoints de ejecuciones que empezaron antes (el agente también descarta sus checkpoints locales anteriores).
- Por cada ejecución terminada se escribe además una fila compatible en `client_events` (`source = 'agent'`) para que Eventos, Dashboard y `/monitor/clients` sigan funcionando.
- **Reintentos**: el reenvío de un **reporte** conserva el `execution_id`; un nuevo **intento** de ejecución genera otro `execution_id` con `attempt + 1`. Tras un fallo el agente reintenta `task_retry_attempts` veces (defecto 2) con espera `task_retry_backoff_seconds × 2^(n-1)` (defecto 60 s, 120 s; nunca más que el schedule) y después espera a la siguiente programación.
- `agency_task.query_version` sube (trigger) cada vez que cambia `extract_sql`; `query_hash = sha256(extract_sql)` se calcula en el servidor. El agente registra solo "tarea X: query_version a → b", nunca el SQL.

### 17.4. Watermark y carga atómica

- `{last_run}` en `extract_sql` se reemplaza por el **watermark confirmado** menos `watermark_overlap_seconds` (defecto 120 s). El solapamiento solo se aplica si la tarea tiene claves de upsert (con INSERT plano duplicaría filas). Sin watermark: `1900-01-01 00:00:00`. Agentes que migran desde v3/v4 parten del `last_run_at` legado.
- Watermark nuevo = hora de **inicio de la extracción** leída del **reloj del origen** (`GETDATE()`, `NOW()`, `LOCALTIMESTAMP`, `CURRENT_TIMESTAMP FROM RDB$DATABASE`) antes de ejecutar la consulta, así que no se pierden filas modificadas durante la carga (el v4 usaba el `NOW()` de Nexus al **final**). `[agent] watermark_clock = source|agent_local|agent_utc`. El watermark se guarda como `TIMESTAMP` sin zona: está en el dominio del reloj indicado en `watermark_kind`.
- **No se mezclan relojes**: con `watermark_clock = source`, si en una corrida no se puede leer el reloj del origen, la carga se hace igual pero **no se envía checkpoint** (avisos `SOURCE_CLOCK_UNAVAILABLE` + `CHECKPOINT_SKIPPED_CLOCK_FALLBACK`): la siguiente corrida recarga la ventana. Si el tipo de reloj configurado cambia respecto del watermark guardado, el agente no envía checkpoint (`CHECKPOINT_KIND_MISMATCH`) y el servidor lo rechaza igualmente; para cambiar de reloj, use "Reiniciar última ejecución" (limpia el watermark y su tipo). Si un origen no permite leer su reloj, configure explícitamente `agent_local` (el agente suele estar en la misma zona que el origen).
- El servidor rechaza checkpoints fuera de rango (año < 1900 o más de `[agent] future_tolerance_hours` —defecto 26 h, margen para husos horarios— por delante de su hora UTC; aviso `CHECKPOINT_REJECTED_OUT_OF_RANGE`) y responde 422 a `started_at`/`finished_at` en el futuro.
- **Transición desde v3/v4**: el `last_run_at` legado es la hora de **Nexus** al **final** de la corrida (otro reloj y quizá otro huso). En la primera corrida v5 (`watermark_kind = legacy_last_run`) se aplica un solapamiento mayor, `legacy_watermark_overlap_seconds` (defecto 3600 s; solo con claves de upsert). Si Nexus y el origen están en husos distintos, aumente ese valor o reinicie el watermark (carga completa) antes de migrar la sede.
- **Una transacción por ejecución en el DWH**: los bloques (`fetch_chunk_rows`, defecto 20000) se insertan dentro de la misma transacción y hay un único `COMMIT` al final; ante cualquier error o cancelación, `ROLLBACK` (no hay cargas parciales). El DDL (`create_table_sql`, constraint, columnas nuevas) va antes en transacciones cortas para no retener locks exclusivos.
  - Memoria acotada a un bloque (cursor de servidor en PostgreSQL, `SSCursor` en MySQL, `fetchmany` en ODBC). Los duplicados por clave dentro de un bloque se resuelven igual que antes (gana el último); entre bloques el upsert posterior sobrescribe, con el mismo resultado.
  - Costo: las filas upsertadas quedan bloqueadas (row locks) hasta el COMMIT, los escritores concurrentes sobre las mismas claves esperan (lectores no, MVCC), y la transacción crece con la carga (WAL). Una carga completa grande queda en una sola transacción larga; `dwh_statement_timeout_seconds` aplica por sentencia y `dwh_lock_timeout_seconds` a las esperas de lock.
- **Idempotencia**: con `upsert_keys` la recarga de una ventana es idempotente. **Sin claves** (INSERT plano) un reintento o una recarga **duplica** filas: la ejecución lleva el aviso `NO_UPSERT_KEYS_DUPLICATES_POSSIBLE`. Otros avisos: `DUPLICATE_KEYS_IN_BATCH`, `CONSTRAINT_DDL_FAILED`, `SOURCE_CLOCK_UNAVAILABLE`.
- **Reconciliación DWH ↔ Nexus** (son sistemas distintos): tras el COMMIT en el DWH, el agente guarda el checkpoint como **pendiente** en su SQLite en la misma transacción local que el reporte de fin. Mientras Nexus no lo confirme, la siguiente ejecución usa el mayor entre el watermark de Nexus y el pendiente local del **mismo tipo de reloj** (no recarga lo ya cargado). Al reiniciar, los reportes pendientes se reenvían solos.
- Justo antes del COMMIT el agente marca localmente la ejecución como `committing`. Si el proceso muere: antes de esa marca se reporta `interrupted` / `AGENT_RESTARTED` ("la carga no se confirmó"); con la marca y sin reporte de fin se reporta `AGENT_RESTARTED_COMMIT_UNKNOWN` ("estado de la carga desconocido: pudo confirmarse"). En ambos casos el watermark no avanza y la siguiente corrida recarga la ventana (idempotente con claves; sin claves puede duplicar).
- **Cancelación segura**: al apagar, se espera `shutdown_grace_seconds` (defecto 60) a que termine la tarea en curso; después se marca cancelación, que se revisa **entre bloques** (→ `ROLLBACK`, estado `interrupted`, watermark sin cambios); si la sentencia sigue bloqueada, se cancela en el driver (`psycopg2 cancel`, `pyodbc cursor.cancel`). Si el proceso muere a la fuerza, al arrancar de nuevo las ejecuciones que quedaron abiertas se reportan como `interrupted` con código `AGENT_RESTARTED` (su transacción nunca se confirmó).
- **Timeouts**: conexión (`db_connect_timeout_seconds`) en todos los drivers; `statement_timeout` (PostgreSQL origen y DWH), `lock_timeout` (DWH), `timeout` de consulta en pyodbc, `read_timeout`/`write_timeout` en pymysql. Firebird vía `fdb` no soporta timeouts (use DSN ODBC si es posible). El origen PostgreSQL se abre en modo **solo lectura**.
- Si una tabla destino no existe y la consulta no devolvió filas, ya no se crea con tipos `TEXT` (se espera a tener datos).

### 17.5. Cola local persistente

`<data_dir>/agent_state.db` (SQLite, WAL, `synchronous=FULL`, permisos 0600). Guarda **solo metadatos** de reportes (ids, estados, conteos, códigos y mensajes ya saneados); rechaza por código cualquier payload con claves como `extract_sql`, `password`, `token`, `host`, `rows`…

- Envío en orden estricto de `agent_seq` (un inicio nunca llega después de su fin), con backoff exponencial + jitter (`queue_backoff_base_seconds`, tope `queue_backoff_max_seconds`).
  - **Caídas** (errores de red, timeouts, 502/503/504 —p. ej. del proxy o `config_db_unavailable`—, 408/429, JSON inválido, **403 `scope_disabled`**): se reintenta **indefinidamente** con backoff y **nunca** se descarta ni se aparta nada, dure lo que dure la caída. Con `scope_disabled` además se descarta la config en memoria (no se inician tareas).
  - **Mensaje veneno** (un reporte concreto que Nexus no puede procesar): solo si se cumplen **todas** estas condiciones: el reporte recibió HTTP **500 exacto** `queue_max_server_errors` veces seguidas (defecto 10; cualquier otro error intermedio reinicia el conteo), han pasado al menos `queue_poison_min_seconds` (defecto 1800 s) desde el primer 500, **y** otra llamada a Nexus (heartbeat, tareas u otro reporte) respondió 2xx después de ese primer 500 (Nexus está sano). Entonces:
    - si es **crítico** (fin de ejecución fallida/interrumpida, cualquier fin de ejecución —puede llevar checkpoint— o un evento crítico): **no se descarta**; se **estaciona**: deja de bloquear la cola y se reintenta cada `queue_parked_retry_seconds` (defecto 3600 s) hasta que Nexus lo acepte o hasta que venza la retención de la cola (`queue_retention_days`) o se alcance el recorte por saturación; en ese caso se descarta contándolo (`overflow_*`) y se envía un evento `queue_overflow`, nunca en silencio;
    - si **no** es crítico (inicio de ejecución, evento informativo): va a `dead_letter` local.
  - 4xx definitivos (422 validación, 409 conflicto, 403 `task_out_of_scope`) van a `dead_letter` local (máx. 500 filas, para diagnóstico).
  - **Nunca en silencio**: los apartados y estacionados se cuentan (`dead_letter_total`, `parked_total` y por motivo), los totales viajan en cada heartbeat y se encola un evento crítico `dead_letter` para Nexus con los contadores (a lo sumo uno por minuto; si Nexus está caído se entrega al volver).
  - 401: se reintenta con la credencial vigente; solo `installation_revoked` detiene el agente.
- **Saturación** (`queue_max_items`, defecto 10000): se compactan primero inicios cuya ejecución ya tiene fin encolado, luego eventos informativos, luego éxitos (su checkpoint queda local), luego inicios. Los **fallos nunca se descartan en silencio**: se admiten hasta 2× el máximo y, si aun así no caben, se descartan los más viejos **contándolos**; cada descarte incrementa contadores y se envía un evento `queue_overflow` con los totales (también viajan en el heartbeat).
- **Retención** (`queue_retention_days`, defecto 14): lo más viejo se purga y se cuenta igual.
- Sobrevive reinicios; el backend deduplica por `execution_id`/`event_id` + `agent_seq`, así que reenviar es seguro.
- Si Nexus no responde: se reintenta con backoff y **no se inician tareas nuevas** sin una autorización obtenida hace menos de `config_max_age_seconds` (mínimo entre el del agente y el que anuncia el servidor; defecto 900 s). La configuración (credenciales y SQL) vive **solo en memoria** y se purga al caducar. La tarea que ya estaba corriendo termina y su reporte queda en la cola.
- La agenda (`task_schedule`) también es persistente: un reinicio no dispara todas las tareas (se respeta el último inicio local o el `last_success_at` de Nexus). `run_all_on_start = true` restaura el comportamiento anterior.

### 17.6. Heartbeat

Hilo independiente con su propia sesión HTTP: cada `heartbeat_seconds` (defecto 60) envía `POST /agent/heartbeat` con versión, uptime, tareas/ejecuciones en curso, profundidad de cola, descartes acumulados (`queue_overflow_total`), reportes apartados y estacionados (`dead_letter_total`, `parked_total`), `agent_seq` y antigüedad de la config. Lee contadores atómicos: no comparte locks con el ETL, así que sigue latiendo durante extracciones largas. El backend actualiza `installation.last_seen_at`/`last_heartbeat` y guarda historial en `installation_heartbeat` (retención `heartbeat_retention_days`, defecto 7; `task_download_log` usa `download_log_retention_days`, defecto 30). La ejecución de tareas corre en un hilo *worker*; el scheduler nunca muere por errores transitorios (captura `requests.RequestException`, 5xx, JSON inválido).

### 17.7. Saneamiento

- Motor común: `dwh_back/redact.py`, copiado tal cual en `dwh_client/nexus_agent/redact_core.py` (una prueba verifica que sean idénticos). Contra ReDoS: la entrada se recorta a 16 KB **antes** de sanear, todas las expresiones usan cuantificadores acotados sobre clases negadas y el SQL se detecta con búsqueda lineal; en el backend el saneamiento del middleware corre en el threadpool, nunca en el event loop.
- Enfoque **conservador** (puede ocultar información útil; es a propósito):
  - se ocultan **todos** los literales entre comillas simples (una comilla precedida por una letra —apóstrofo de `Can't`, `doesn't`— no abre literal, así el texto en inglés no se desfigura) y los literales entre comillas dobles, salvo los que siguen a una palabra de identificador (`relation`, `column`, `table`, `constraint`, `index`, `schema`, `type`, `function`…): se conservan nombres de tablas/columnas/constraints y se ocultan valores (`invalid input syntax for type integer: "***"`), usuarios (`Login failed for user '***'`), `Duplicate entry '***'`, `converting the varchar value '***'`, nombres de objeto de SQL Server entre comillas simples, etc.;
  - listas de valores entre paréntesis (`Key (col)=(***)`, `Failing row contains (***)`, `The duplicate key value is (***)` y cualquier paréntesis con comas o `@`);
  - SQL también **multilínea**: desde `select…from`, `insert…into`, `update…set`, `delete…from`, `with…as`, `merge…into`, `create/alter…`, `exec …` hasta el **final** del mensaje;
  - líneas `DETAIL/LINE/HINT/QUERY/CONTEXT/WHERE`, pares `clave=valor` de conexión/DSN, URLs con credenciales, IPs, puertos y los valores sensibles conocidos (credenciales recibidas de Nexus, tokens, secreto de la instalación).
  - además, en cualquier parte del texto: correos electrónicos, secuencias de ≥ 9 dígitos (y formatos de tarjeta `dddd dddd dddd d…` y teléfono `dd dddd dddd`) y valores entre corchetes `[...]`, salvo la cadena de drivers ODBC y los SQLSTATE (`[Microsoft][ODBC Driver 17 for SQL Server][42S02]`). Se conserva el código numérico inicial de MySQL: `(1062, ***)`. Fechas y horas no se tocan.
  - **Límites residuales**: no se detectan valores de negocio "desnudos" (sin comillas, paréntesis ni corchetes y sin formato reconocible), por ejemplo pares `campo=valor` de negocio cuya clave no es de conexión (`rfc=XAXX…`), fragmentos de filas tipo CSV (`A123;Juan;XAXX…`), teléfonos con espacios o guiones (`+52 (55) 1234-5678`), números de menos de 9 dígitos o nombres sueltos. Tampoco una comilla sin cerrar (`abc'VALOR`) ni combinaciones artificiales de apóstrofos y contracciones (`O'Brien='xt't`): es el límite de un enfoque sin parser SQL. Los códigos entre corchetes que no son SQLSTATE (p. ej. `[2002]` de MySQL) también se ocultan. Por eso además no se envían filas, SQL ni credenciales en ningún payload (lista negra de claves en la cola local) y los mensajes se recortan a 500/1000 caracteres.
- Agente: `nexus_agent/sanitize.py` → `sanitize_error(exc)` devuelve `(error_code, mensaje)`. Códigos estables con prefijo de lado (`SOURCE_…`/`DWH_…`): `CONNECTION_FAILED`, `CONNECT_TIMEOUT`, `AUTH_FAILED`, `QUERY_TIMEOUT`, `LOCK_TIMEOUT`, `SQL_ERROR`, `CONSTRAINT_VIOLATION`, `DATA_ERROR`, `DATABASE_NOT_FOUND`, `DRIVER_NOT_FOUND`, `DB_ERROR`, `UNEXPECTED_ERROR`; además `CANCELLED`, `CONFIG_ERROR`, `AGENT_RESTARTED`, `AGENT_RESTARTED_COMMIT_UNKNOWN`, `OUT_OF_MEMORY`. El mensaje pasa por el motor común descrito arriba; longitud ≤ 500. Se usa en los reportes **y** en todos los logs (`RedactingFormatter`, incluidas trazas).
- Backend: `redact.py` aplica lo mismo (defensa en profundidad; el detalle legado se recorta a 16 KB antes y a 4000 caracteres después) al detalle de `/client-event`, a los mensajes de ejecución (además con las credenciales del alcance), a `activity_log.error_detail` y a los logs del servidor.

### 17.8. Migraciones de la BD de configuración

- `schema_postgres.sql` es la **línea base** idempotente. Las tablas y cambios nuevos van en `dwh_back/migrations/NNN_nombre.sql`, que se aplican una sola vez, en orden, cada una en su transacción, y quedan registradas en `schema_migrations` (versión, nombre, checksum, fecha). Un archivo ya aplicado que cambie solo genera un aviso: los cambios van en una migración nueva. Un advisory lock evita migraciones concurrentes.
- Uso: `python migrate.py` (línea base + pendientes), `python migrate.py --status`, `--no-baseline`, `--config RUTA` (o `NEXUS_CONFIG_FILE`).
- El backend avisa en el log al arrancar si hay migraciones pendientes; con `[database] auto_migrate = true` las aplica solo.
- Migraciones actuales:
  - `001_auditoria_sin_tokens`: ids resueltos y `auth_kind` en `activity_log`/`client_events`, relleno a partir de los tokens existentes y **recorte de los tokens guardados a 8 caracteres**; `source`, `task_id`, `installation_id`, `execution_id` en `client_events`.
  - `002_instalaciones`: `installation`, `installation_heartbeat`, `agent_event`, `task_download_log`.
  - `003_ejecuciones_y_sync`: `agency_task.query_version`/`query_hash` + trigger, `task_execution`, `task_sync_state`.
  - `004_rotacion_y_limites`: `installation.pending_secret_enc` (re-entrega idempotente de la rotación).
  - `005_salud_incidencias`: `agency_task.expected_duration_seconds`/`delay_tolerance_seconds`, `task_health_state`, `incident` (+ índice único parcial: una abierta por clave), `incident_event`, `notification_channel`, `notification_outbox` (sección 18). Solo crea tablas/columnas nuevas: rápida y compatible con BD existentes.
  - `006_indices_salud`: índices de expresión/parciales de `task_execution` para el modelo de salud y la retención (sección 18.3.1).
  - `007_inventario_estructural`: `monitored_database` (+ `_link`, `_event`), `inventory_snapshot`, `inventory_object_state`, `inventory_baseline` (+ `_version`), `structural_change` (+ `_event`) y `task_execution.ddl_applied` (sección 19). Solo crea tablas/columnas nuevas.
  - `008_inventario_ajustes`: identidad débil del servidor (`engine_identity_weak`), `allow_engine_duplicate`, estado `out_of_scope` de las alertas y limpieza de `monitored_database_link` al borrar grupo (FK) o empresa (trigger).
  - `009_usuarios_permisos`: `panel_user`, `panel_permission`, `panel_role`, `panel_role_permission` (roles sembrados), `panel_user_role` (rol por alcance de grupo), `panel_session`, `panel_audit_log` y columnas `*_user_id` junto a los actores de texto (`incident`, `incident_event`, `structural_change`, `structural_change_event`, `monitored_database`, `monitored_database_event`, `inventory_baseline_version`, `client_events.acknowledged_by/_at`, `installation.revoked_by`). Solo crea tablas/columnas; no crea usuarios (sección 20).
  - `010_destino_configurable`: esquema destino y SSL/TLS del DWH del grupo (`client_group.warehouse_schema|sslmode|sslrootcert`), destino propio por empresa (`company.warehouse_mode` + campos `warehouse_*`, todas las empresas existentes quedan en `inherit`) y tabla `connection_test` (sección 22). Solo agrega columnas con valores por defecto y una tabla.
- **Ojo con `001` en BD grandes**: hace `UPDATE` masivos sobre `activity_log` y `client_events` (relleno de ids y recorte de tokens) en una sola transacción: puede tardar y generar mucho WAL/bloqueos si `activity_log` es grande. Recomendado: purgar/archivar `activity_log` antiguo antes, ejecutarla en ventana de mantenimiento y con respaldo.
- Compatibles con BD existentes (probado sobre una copia de la BD de desarrollo y sobre una BD "legada" creada en las pruebas).

### 17.9. Compatibilidad con agentes legados (v3/v4) — OBSOLETO

- `/configs`, `/group-configs`, `/agency-configs`, `/client-event` y `/configs/{id}/last_run` siguen funcionando con el mismo contrato. Cambios: prioridad única grupo > agencia > empresa (antes `/configs/{id}/last_run` y `/client-event` resolvían agencia > empresa > grupo); `/client-event` exige que `config_id` sea una tarea del alcance del token (403) y sanea/recorta `detail`.
- Aparecen en el panel (Instalaciones → *Clientes legados*) y en `/monitor/installations → legacy_clients` a partir de `activity_log` (solo peticiones con token **válido**; los intentos con token inválido quedan en Actividad, no se listan como clientes).
- Plan: actualizar cada sede al agente v5, verificar que no queden clientes legados y poner `[agent] legacy_endpoints = false` (responden 410).
- Pendiente para fases siguientes: los tokens de enrolamiento siguen guardados en claro en la BD de configuración (el panel necesita mostrarlos); se buscan por igualdad SQL sobre un índice único. `/agent/enroll` no tiene límite de tasa (solo de tamaño).
- **Riesgo de alcance de grupo**: una instalación enrolada con token de **grupo** recibe las credenciales de origen de **todas** las empresas del grupo y la del DWH. Prefiera tokens de agencia/empresa por sede y reserve el de grupo para servidores centrales controlados.

### 17.10. Variables nuevas

Agente (`[nexus]`): `ca_bundle`, `mode`, `allow_insecure_http`, `installation_name`. (`token`, `group_token`, `agency_token` pasan a ser solo de enrolamiento.)

Agente (`[agent]`): `data_dir`, `log_dir`, `log_retention_days`, `credential_scope`, `heartbeat_seconds`, `tick_seconds`, `config_max_age_seconds`, `http_connect_timeout`, `http_read_timeout`, `api_retry_base_seconds`, `api_retry_max_seconds`, `run_all_on_start`, `task_retry_attempts`, `task_retry_backoff_seconds`, `shutdown_grace_seconds`, `queue_max_items`, `queue_retention_days`, `queue_backoff_base_seconds`, `queue_backoff_max_seconds`, `db_connect_timeout_seconds`, `source_statement_timeout_seconds`, `dwh_statement_timeout_seconds`, `dwh_lock_timeout_seconds`, `fetch_chunk_rows`, `watermark_clock`, `watermark_overlap_seconds`, `legacy_watermark_overlap_seconds`, `queue_max_server_errors`, `queue_poison_min_seconds`, `queue_parked_retry_seconds`, `connection_test_enabled`, `connection_test_poll_seconds` (sección 22). Variable de entorno opcional `NEXUS_AGENT_CONFIG` (ruta del INI). Plantilla comentada: `dwh_client/config_postgres.ini.example`.

Backend: `[database] auto_migrate`; `[agent] config_max_age_seconds`, `rotation_grace_seconds`, `heartbeat_retention_days`, `download_log_retention_days`, `legacy_endpoints`, `future_tolerance_hours`; `[server] max_body_bytes`, `agent_max_body_bytes`, `enroll_max_body_bytes`; variable de entorno `NEXUS_CONFIG_FILE` (ruta alternativa del `config.ini`, usada por las pruebas). Panel: `NEXT_PUBLIC_DWH_TIMEZONE`.

### 17.11. Uso del agente

```
python client_postgres.py                 # servicio: bucle continuo
python client_postgres.py --once          # ejecuta lo vencido, vacía la cola y sale
python client_postgres.py --enroll        # fuerza un enrolamiento nuevo (borra la credencial local)
python client_postgres.py --config RUTA --data-dir RUTA
python client_postgres.py --selftest      # drivers/TLS/SQLite sin red (también NexusAgent.exe --selftest)
```

Códigos de salida: 0 ok, 1 error inesperado (el gestor del servicio debe reiniciar), 2 configuración, 3 credencial revocada/inválida, 4 paquete de actualización rechazado (`--verify-update`). En producción el agente corre compilado como servicio de Windows (sección 21); ahí el enrolamiento lo hace el servicio con un token de un solo uso, **no** `--enroll` desde una consola de administrador.

### 17.12. Pruebas automatizadas

Requieren Docker local: `nexus_dwh_pg_dev` (5546, BD de configuración; las pruebas crean y borran sus propias bases `nexus_test_cfg_*`), `nexus_dwh_test_src` (5547, origen PostgreSQL) y `nexus_dwh_test_dwh` (5548, DWH):

```
docker run -d --name nexus_dwh_test_src -e POSTGRES_PASSWORD=src-test-pass-7Qx -e POSTGRES_DB=dms_test -p 5547:5432 postgres:16-alpine
docker run -d --name nexus_dwh_test_dwh -e POSTGRES_PASSWORD=dwh-test-pass-9Kz -e POSTGRES_DB=dwh_test -p 5548:5432 postgres:16-alpine

cd dwh_back   && .venv/bin/pip install -r requirements_dev.txt && .venv/bin/python -m pytest tests -q
cd dwh_client && python3.12 -m venv .venv && .venv/bin/pip install -r requirements_postgres.txt -r requirements_dev.txt
cd dwh_client && .venv/bin/python -m pytest tests -q
```

`dwh_back/tests` (`test_agent_api.py`, `test_hardening.py`): DoS/ReDoS con tiempos acotados y `/health` sin bloqueo, límites de cuerpo (413) y de campos (422 sin eco del valor), fugas de drivers en el saneamiento, watermark fuera de rango o de otro reloj, rotación sin cadena de secuestro, migraciones (idempotencia y BD legada), enrolamiento y alcance, autenticación, aislamiento entre grupos (403), idempotencia y máquina de estados de ejecuciones, eventos fuera de orden, saneamiento en servidor, reinicio de watermark, `query_version`, revocación, rotación con gracia, heartbeat/monitor, compatibilidad legada (403 fuera de alcance, prioridad de tokens, endpoints desactivables), `activity_log` sin tokens. `dwh_client/tests/test_units.py`: saneamiento, cola (secuencia, persistencia, backoff, saturación, retención, checkpoints), credenciales (0600 / DPAPI simulado), URL/TLS, watermark, orden de envío, agenda persistente, reintentos. `dwh_client/tests/test_integration.py`: backend + origen + DWH reales (enrolar → ejecutar → verificar filas/ejecución/sync; origen caído y recuperado; DWH caído y recuperado; Nexus caído con cola que crece y se vacía sin duplicados y sin recargar; config caducada; violación de restricción sin carga parcial; SQL inválido; tabla sin claves; heartbeat durante tarea larga con `pg_sleep`; `SIGKILL` a mitad de carga → sin COMMIT ni avance de watermark y re-ejecución idempotente; credencial revocada → salida 3; y búsqueda de SQL/secretos en logs, BD de Nexus y SQLite local).

---

## 18. Salud, incidencias y notificaciones (PostgreSQL)

Módulo `dwh_back/health_postgres.py` (motor + API) y páginas del panel **Salud**, **Incidencias** y **Notificaciones**. Requiere la migración `005_salud_incidencias`. Reutiliza lo que ya existía: `installation.last_seen_at` (cualquier llamada autenticada del agente), el latido de 60 s (`installation_heartbeat`, `last_heartbeat.running`), `task_execution`, `task_sync_state` y `agent_event`. El Dashboard, Eventos y `/monitor/*` legados siguen igual.

### 18.1. Modelo de salud

Todas las fechas y comparaciones se calculan en el **servidor** con la hora de la BD de configuración (`NOW()`); se guardan en UTC y el panel las muestra en `NEXT_PUBLIC_DWH_TIMEZONE` con la zona explícita. El watermark es la excepción: está en el reloj del origen/agente y se muestra **sin zona** (tal cual).

**Instalación** (`GET /admin/health/installations`):

| Dato | Origen |
|---|---|
| Último contacto | `installation.last_seen_at` (latido o cualquier llamada autenticada) |
| Último latido | `MAX(installation_heartbeat.received_at)` |
| Última ejecución / última carga exitosa | `task_execution` de esa instalación |
| Cola, apartados, descartes, tareas en curso | último latido |
| Conectividad | `online` (contacto ≤ `disconnect_after_seconds`), `offline`, `revoked`, `scope_disabled`, `never` |

**Tarea** (`GET /admin/health/tasks`): activa efectiva (tarea + agencia + objeto + empresa + grupo habilitados), última ejecución (estado, código de error, filas), **última carga exitosa** (incluye cargas de **0 filas**: no son falla), **punto de sincronización confirmado** (`task_sync_state.watermark` + tipo de reloj), **error actual** y **errores consecutivos** (de `task_sync_state`, que ya respeta el orden de los eventos), ejecución **en curso** (confirmada por el latido) y plazos. Estado único para el panel:

| Estado | Regla (en este orden) |
|---|---|
| `disabled` | no está activa efectivamente (nunca genera alerta de retraso) |
| `running` | hay una ejecución `running` de una instalación **en línea**, confirmada por su último latido (o iniciada hace < 2× `heartbeat_expected_seconds`). El último latido de una instalación muerta **no** mantiene la tarea en curso ni suprime el retraso |
| `failing` | la ejecución más reciente (según `agent_seq`/`started_at`) falló o se interrumpió, **o** hay una incidencia `task_failed` abierta para la tarea en **cualquier** instalación (`task_sync_state` es una fila por tarea: el éxito de otra instalación no "cura" la falla). El desglose va en `failing_installations` (instalación, código, ocurrencias, reconocida) y el panel lo muestra bajo *Error actual* |
| `delayed` | `NOW() > referencia + schedule_seconds + duración esperada + tolerancia` |
| `never_run` | sin ejecuciones y aún dentro del plazo |
| `ok` | lo demás |

Además se devuelven los indicadores `delayed`, `failing`, `running`, `running_long` por separado (una tarea puede estar fallando **y** retrasada).

- **Referencia del retraso** = la mayor entre la última carga exitosa y `task_health_state.active_since` (momento en que el evaluador vio la tarea pasar a activa efectiva: al habilitar una tarea, agencia, empresa, grupo u objeto hay un plazo completo de gracia; tras migrar, la gracia empieza en la primera evaluación).
- **Hora de la última carga exitosa** = `LEAST(finished_at del agente, hora de recepción del servidor)`: un reloj del agente adelantado no puede ocultar un retraso, y una carga reportada tarde (Nexus caído) conserva su hora real.
- **Duración esperada**: `agency_task.expected_duration_seconds` si está configurada; si no, el **p90** de las últimas `expected_duration_history` (20) ejecuciones exitosas cuando hay al menos 3; si no, `default_expected_duration_seconds` (300).
- **Tolerancia**: `agency_task.delay_tolerance_seconds` o `max(delay_min_grace_seconds, schedule × delay_tolerance_factor)` (300 s / 0.5).
- **Ejecución prolongada**: en curso más de `max(esperada × running_long_factor, esperada + tolerancia)`. Mientras una tarea está en curso no se marca retrasada.

### 18.2. Incidencias

Tabla `incident` (+ historial `incident_event`). Categorías:

| Categoría | Severidad | Se abre | Se resuelve (solo con evidencia) |
|---|---|---|---|
| `disconnected` | crítica | el **evaluador de Nexus** ve la instalación activa sin contacto > `disconnect_after_seconds` (el agente desconectado no puede avisar) | al recibir un **latido** (`heartbeat_recovered`) o si el evaluador ve contacto reciente (`contact_recovered`). **No toca** las incidencias de tareas |
| `task_failed` | error (interrumpida: advertencia) | fin de ejecución `failed`/`interrupted` | una **carga confirmada** (`success`, también con 0 filas) de **esa tarea en esa instalación**, más nueva que la última falla (`load_confirmed`). No resuelve otras tareas |
| `task_delayed` | advertencia | evaluador: tarea activa, no en curso, pasado el plazo | carga confirmada que deja la tarea al día (`load_confirmed`); si deja de estar retrasada sin carga nueva (se cambiaron umbrales): `thresholds_changed` |
| `task_running_long` | advertencia | evaluador: ejecución en curso más allá del límite | fin de esa ejecución (`execution_finished`) |
| `checkpoint_kind_mismatch` | advertencia | aviso `CHECKPOINT_KIND_MISMATCH` de la fase 1 (el watermark no avanza) | un checkpoint aceptado (`checkpoint_applied`) o **Reiniciar última ejecución** (`watermark_reset`) |
| `queue_dead_letter` / `queue_overflow` | advertencia / error | eventos `dead_letter` / `queue_overflow` del agente | **no hay evidencia automática** (lo apartado/descartado no vuelve): se cierran **manualmente con motivo obligatorio** (`manual`) |

Reglas:

- **Agrupación**: `dedup_key = categoría | instalación | tarea` (`task_delayed` es por tarea, sin instalación). Solo puede haber **una abierta** por clave (índice único parcial; `INSERT … ON CONFLICT DO NOTHING` + reintento, seguro con concurrencia). Una recurrencia suma `occurrences`, actualiza última ocurrencia, último código/mensaje saneado y el contador por código (`details.error_codes`), **sin volver a notificar**. Las condiciones continuas (desconexión, retraso, ejecución prolongada) no suman ocurrencias por ciclo del evaluador. Tras resolverse, una nueva falla abre **otra** incidencia (la anterior queda en el historial).
- Se guarda primera ocurrencia (`opened_at`), última (`last_seen_at`), contador, primera/última ejecución relacionada, `resolved_at`, motivo, ejecución que la resolvió y `duration_seconds`.
- **Reconocida ≠ resuelta**: `PUT /admin/incidents/{id}/ack` (comentario opcional) solo llena `acknowledged_at/by/ack_comment`; la incidencia sigue **abierta** y el estado técnico (salud de la tarea/instalación, error actual) se sigue mostrando. Una recurrencia no borra el reconocimiento. `acknowledged_by` = usuario del panel (+ `acknowledged_by_user_id`); los registros previos a la fase 4 conservan `admin`.
- **Cierre manual** (`PUT /admin/incidents/{id}/resolve`, motivo obligatorio) solo para `queue_dead_letter` y `queue_overflow`; para las demás responde **409**: se resuelven solas con evidencia.
- **Eventos fuera de orden**: el orden de la evidencia es el `agent_seq` de **inicio** de la ejecución (el mismo criterio de `task_sync_state`), nunca la hora de llegada. Una falla vieja que llega después de un éxito más nuevo **no** abre incidencia; un éxito viejo que llega después de una falla más nueva **no** la resuelve (queda en el historial como `late_evidence_ignored`).
- **Cierres administrativos** (motivo explícito, no son recuperación): tarea deshabilitada (o su agencia/empresa/grupo/objeto) → `task_disabled` para todas sus incidencias; tarea borrada → `task_deleted`; instalación revocada → `installation_revoked`; instalación borrada → `installation_deleted`; alcance deshabilitado → `scope_disabled` (desconexión).
- **Sin doble alerta**: mientras la tarea tenga una `task_failed` abierta (en cualquier instalación) no se abre además `task_delayed`: la falla ya lo cubre. Un retraso que ya estaba abierto se conserva hasta que haya carga confirmada.
- **Relleno inicial**: el evaluador abre `task_failed` (con `details.backfilled = true`) para tareas que ya estaban fallando antes de la migración y nunca tuvieron incidencia para su clave.
- Los ganchos de incidencias corren en la **misma transacción** que el reporte del agente, dentro de un `SAVEPOINT`: un error del motor se registra en el log y **nunca** tumba el reporte.

**Evaluador periódico**: hilo del backend cada `evaluator_interval_seconds` (30 s; también `POST /admin/health/evaluate`). Cada instalación, tarea e incidencia se procesa en su propio `SAVEPOINT` y cada fase (instalaciones, tareas, relleno, huérfanas, recordatorios) está aislada: un error se revierte solo para ese elemento/fase, se registra en el log solo con el tipo de error y el resto del ciclo continúa; la respuesta incluye `errors` y `failed_phases`. Usa un **advisory lock de sesión** (`pg_try_advisory_lock`): con varias réplicas o llamadas simultáneas solo una evalúa (las demás responden `ran: false`). Bloquea las filas de `installation` candidatas (`FOR UPDATE SKIP LOCKED`) y re-verifica `last_seen_at`, así que un latido concurrente espera y luego resuelve. Durante `startup_grace_seconds` (por defecto = `disconnect_after_seconds`) tras arrancar el backend **no abre desconexiones**: si el caído era Nexus, los agentes necesitan un latido para volver a verse.

### 18.3. Notificaciones

- **Alertas del panel** siempre activas (menú Incidencias con contador, Dashboard). No había canal externo previo; se agregó una interfaz configurable (`notification_channel`), vacía por defecto: **no se envía nada** hasta que un administrador configure un canal.
- **Solo transiciones**: apertura (`incident.opened`), resolución (`incident.resolved`) y, opcionalmente por canal, un **recordatorio** cada `reminder_interval_minutes` mientras siga abierta y **sin reconocer** (`incident.reminder`). Nunca una por ciclo ni por recurrencia.
- **Outbox transaccional** (`notification_outbox`): la notificación se encola en la misma transacción que la transición. Un hilo del backend (`worker_interval_seconds`) la reclama con `FOR UPDATE SKIP LOCKED`, envía fuera de la transacción y registra el resultado; reintentos con backoff exponencial (`backoff_base_seconds × 2^(n-1)`, tope `backoff_max_seconds`) hasta `max_attempts` → `failed` (+ evento `notification_failed` en la incidencia). Entrega **al menos una vez**: si el proceso muere a mitad de un envío, la fila `sending` se reintenta tras `sending_stale_seconds`; el receptor debe descartar repetidos por `X-Nexus-Delivery` (clave `incident-<id>-<transición>`, única por canal).
- **Filtros por canal**: severidad mínima, grupo (o todos), categorías (vacío = todas), apertura/resolución.
- **Emisores**: `webhook` y `log` (solo escribe en el log del servidor; para desarrollo). La interfaz es un registro (`SENDERS`) para agregar otros (correo, Teams…).
- **Anti-SSRF** (`[notifications] block_private_ips = true` por defecto): al guardar el canal **y** justo antes de cada envío se resuelve el host y se rechaza si alguna IP es de loopback, privada (10/8, 172.16/12, 192.168/16, fc00::/7), link-local (169.254/16 —metadatos de nube—, fe80::/10), CGNAT (100.64/10), multicast, reservada o no especificada (también IPv4 mapeada en IPv6). El envío bloqueado queda como error `Destino bloqueado o no resoluble` (reintentos normales). Solo las pruebas lo desactivan (receptor en 127.0.0.1). Riesgo residual: entre la verificación y la conexión el DNS podría cambiar (DNS rebinding de ventana muy corta); para destinos internos legítimos use un proxy/relé público controlado.
- **Webhook**: `POST` JSON, sin redirecciones, timeout y verificación TLS por canal, solo `https://` salvo `[notifications] allow_http = true`; se rechazan URLs con usuario/contraseña. Cabeceras: `X-Nexus-Event`, `X-Nexus-Delivery`, `X-Nexus-Timestamp` (epoch s) y, si hay secreto, `X-Nexus-Signature: sha256=<hex>` con `HMAC-SHA256(secreto, "<timestamp>." + cuerpo)`. Verificación en el receptor: recalcular el HMAC sobre los bytes recibidos, comparar en tiempo constante y rechazar timestamps viejos (p. ej. > 5 min).
- **Payload** (sin SQL, sin credenciales; mensaje ya saneado):
  ```json
  {"event": "incident.opened", "delivery_id": "incident-42-opened", "generated_at": "…Z",
   "incident": {"id": 42, "category": "task_failed", "category_label": "Falla de tarea", "severity": "error",
                "status": "open", "title": "…", "installation": {"id": "…", "name": "…"},
                "scope": {"group": "…", "company": "…", "agency": "…", "object": "…", "task_id": 7},
                "opened_at": "…Z", "last_seen_at": "…Z", "occurrences": 1, "last_error_code": "SOURCE_CONNECTION_FAILED",
                "message": "…", "resolved_at": null, "resolution_reason": null, "duration_seconds": null,
                "acknowledged": false}}
  ```
- **Secretos**: la URL (puede llevar un token, p. ej. webhooks de Slack/Teams) y el secreto de firma se guardan cifrados (`ENC:` Fernet con `config_secret_key`) y son de **solo escritura**: la API y el panel muestran solo `esquema://host/…`, `has_url`, `has_secret`. Sin `config_secret_key` se guardan en claro y el panel lo advierte. Los errores de entrega guardan solo `HTTP <código>` o el tipo de excepción (nunca la URL ni el cuerpo de la respuesta).
- **Probar canal**: `POST /admin/notification-channels/{id}/test` envía un evento `test` (sin incidencia real) y devuelve el resultado.

### 18.3.1. Retención

La hace el notificador **y** el evaluador (como máximo una vez por hora por proceso; basta con que uno de los dos hilos esté activo):

- `notification_outbox`: entregas terminadas más viejas que `[notifications] retention_days` (30).
- `task_execution`: filas con `received_at` más viejo que `[health] execution_retention_days` (180; 0 = sin purga), por lotes de 5000. **Nunca** se borran: la última ejecución y la última carga exitosa de cada tarea, la referenciada por `task_sync_state`, las referenciadas por incidencias **abiertas** ni las que siguen `running`.
- `incident_event`: de incidencias **resueltas** hace más de `[health] incident_event_retention_days` (365; 0 = sin purga) se borran los eventos secundarios (recurrencias, notificaciones…); se conservan apertura, reconocimiento y resolución. Las incidencias no se purgan.
- La migración `006_indices_salud` agrega índices de expresión/parciales sobre `task_execution` para que las consultas de salud y la retención no recorran la tabla completa.

### 18.4. API

| Método y ruta | Uso |
|---|---|
| `GET /admin/health/summary` | Conteos: instalaciones por conectividad, tareas por estado, incidencias abiertas por severidad, sin reconocer, resueltas 24 h |
| `GET /admin/health/installations` | Filtros `group_id`, `company_id`, `agency_id`, `connectivity` |
| `GET /admin/health/tasks` | Filtros `group_id`, `company_id`, `agency_id`, `task_id`, `state` |
| `GET /admin/health/settings` | Umbrales vigentes |
| `POST /admin/health/evaluate` | Ejecuta un ciclo del evaluador ahora |
| `GET /admin/incidents` | Filtros `view=active|acknowledged|resolved|open`, `status`, `acknowledged`, `category`, `severity`, `group_id`, `company_id`, `agency_id`, `task_id`, `installation_id`, `since`, `until`, `limit` |
| `GET /admin/incidents/badge` | Abiertas sin reconocer / abiertas / graves sin reconocer |
| `GET /admin/incidents/{id}` | Detalle + historial + ejecuciones relacionadas + entregas + estado técnico actual |
| `PUT /admin/incidents/{id}/ack` | Reconocer (`{"comment": "…"}`) |
| `PUT /admin/incidents/{id}/resolve` | Cierre manual con `{"reason": "…"}` (solo cola local) |
| `GET/POST /admin/notification-channels`, `PUT/DELETE /admin/notification-channels/{id}` | Canales (`url` y `signing_secret` solo escritura; en PUT `signing_secret: ""` lo quita y `group_id: 0` = todos los grupos) |
| `POST /admin/notification-channels/{id}/test` | Envío de prueba |
| `GET /admin/notification-deliveries` | Registro de entregas (filtros `channel_id`, `incident_id`, `status`) |

`POST/PUT /admin/tasks` aceptan además `expected_duration_seconds` y `delay_tolerance_seconds` (null = automático).

### 18.5. Panel

- **Salud**: tabla de instalaciones (conectividad, último contacto, última ejecución, última carga exitosa, cola/apartados/descartes, incidencias abiertas) y de tareas (estado, última ejecución, última carga exitosa con filas, punto de sincronización confirmado con su tipo de reloj, error actual, errores consecutivos, plazos y de dónde sale la duración esperada). Filtros por grupo/empresa/agencia, conectividad y estado; se actualiza cada 30 s. Aclara que conectividad ≠ éxito del ETL.
- **Incidencias**: pestañas **Activas** (abiertas sin reconocer), **Reconocidas** (abiertas revisadas) y **Resueltas**; filtros por alcance, categoría y severidad; cada fila muestra por separado el estado técnico (Abierta/Resuelta) y el reconocimiento. Detalle con primera/última ocurrencia, contador, duración, último mensaje saneado, **estado técnico actual**, ejecuciones relacionadas, historial y entregas; acciones **Reconocer** (comentario) y, solo en categorías de cola, **Cerrar con motivo**.
- **Notificaciones**: alta/edición/baja de canales, **Enviar prueba** y registro de entregas.
- **Tareas**: campos opcionales *Duración esperada* y *Tolerancia de retraso*.

### 18.6. Variables nuevas

Backend `[health]`: `evaluator_interval_seconds`, `disconnect_after_seconds`, `heartbeat_expected_seconds`, `default_expected_duration_seconds`, `expected_duration_history`, `delay_tolerance_factor`, `delay_min_grace_seconds`, `running_long_factor`, `startup_grace_seconds`. `[notifications]`: `worker_interval_seconds`, `max_attempts`, `backoff_base_seconds`, `backoff_max_seconds`, `allow_http`, `sending_stale_seconds`, `retention_days` (registro de entregas), `block_private_ips`; `[health]` además `execution_retention_days`, `incident_event_retention_days`. Valores por defecto y explicación en `dwh_back/config_postgres.ini.example`. El agente no cambia (ya envía el latido cada 60 s con las ejecuciones en curso).

### 18.7. Limitaciones conocidas

- La detección tiene la **granularidad del evaluador**: una desconexión se abre entre `disconnect_after_seconds` y `disconnect_after_seconds + evaluator_interval_seconds` después del último contacto (más la gracia de arranque si Nexus se reinició).
- `task_failed` se agrupa **por instalación**: si dos instalaciones ejecutan la misma tarea, el éxito de una no resuelve la falla de la otra (es intencional: la otra sigue rota). El retraso sí es por tarea.
- Deshabilitar una tarea cierra sus incidencias con `task_disabled` (no es recuperación; el historial lo indica).
- Los clientes **legados** (v3/v4, tokens) no generan incidencias: su visibilidad sigue siendo Eventos / Dashboard / Clientes legados.
- Umbrales globales + por tarea; no hay umbrales por instalación ni horarios de mantenimiento/silencio.
- `/monitor/clients` agrega `last_seen_utc` / `last_execution_utc` (ISO UTC, aditivos) para que el Dashboard muestre la zona; las columnas legadas son `TIMESTAMP` sin zona y se interpretan en la zona de la sesión de la BD.
- Entrega de notificaciones **al menos una vez** (el receptor debe deduplicar por `X-Nexus-Delivery`). Sin canales configurados, solo hay alertas en el panel.
- `acknowledged_by`/`resolved_by` = usuario del panel (fase 4); los registros anteriores conservan `admin`.

### 18.8. Pruebas

```
cd dwh_back   && .venv/bin/python -m pytest tests -q          # incluye tests/test_health.py
cd dwh_client && .venv/bin/python -m pytest tests -q          # incluye tests/test_health_integration.py
```

- `dwh_back/tests/test_health.py` (BD propia, evaluador manual, receptor webhook **local**): agrupación de recurrencias con una sola notificación y firma HMAC verificada; recuperación solo de su tarea; éxito con 0 filas; eventos fuera de orden (no reabren ni resuelven); desconexión detectada por Nexus y latido que no cierra errores de tareas; reconocer ≠ resolver (409 al cerrar a mano); retraso por periodicidad, tarea deshabilitada sin alerta y cierre `task_disabled`, gracia al rehabilitar; ejecución en curso y prolongada; `checkpoint_kind_mismatch` (resuelta por checkpoint y por reinicio del watermark); dead-letter con cierre manual con motivo; revocación; advisory lock; reintentos con backoff (500, 500, 200) y fallo definitivo; secretos de canal cifrados y de solo escritura; relleno de fallas previas a la migración; modelo de salud sin SQL; evaluador que aísla fallos por instalación y por fase (fallas inyectadas con triggers); latido viejo de una instalación muerta que no deja la tarea "en curso"; falla abierta en otra instalación que mantiene la tarea con error y sin doble alerta de retraso; anti-SSRF (IPs privadas/metadatos al guardar y al enviar); canal borrado a mitad del envío; retención del outbox desde el notificador y del historial de ejecuciones/eventos sin borrar lo protegido.
- `dwh_client/tests/test_health_integration.py` (backend + origen + DWH reales, evaluador cada 1 s, desconexión a 10 s): DMS caído → incidencia `SOURCE_*` y recuperación que la resuelve (con notificaciones de apertura y resolución); DWH caído/recuperado; **Nexus caído** más que el umbral con la cola guardando falla + éxito → al volver se aplican en orden (abre y resuelve) y **no** se marca desconectado a un agente vivo; **agente muerto** (`SIGKILL`) → Nexus abre `disconnected`, reconocerla no la resuelve, al volver el agente se resuelve la desconexión pero la falla de la tarea sigue abierta; tarea larga (~15 s, > umbral) con latidos → en curso confirmada por latido, sin desconexión.

---

## 19. Inventario estructural y cambios de estructura (PostgreSQL)

Módulos `dwh_back/inventory_postgres.py` (motor + API) y `dwh_client/nexus_agent/inventory.py` (inventario en el agente); página **Estructura** del panel. Requiere la migración `007_inventario_estructural` y agentes **v5.1.0+** (los v5.0 simplemente no inventarían).

Alcance: comparación **estructural** (tablas, vistas, vistas materializadas, tablas foráneas; columnas, tipos, nulabilidad, valores por defecto, llaves, restricciones, índices, definición de vistas, particionamiento). **No** es auditoría de filas ni detecta quién cargó datos.

### 19.1. Quién inventaría y con qué permisos

- El inventario lo hace el **agente local** y lo reporta a Nexus: **sin conexiones entrantes** y Nexus nunca se conecta a la base. Usa las credenciales que el agente ya tiene en memoria (`GET /agent/tasks`); no se entregan credenciales nuevas.
- Sesión de **solo lectura**: `default_transaction_read_only=on` + transacción `READ ONLY` `REPEATABLE READ`, `statement_timeout` (`[agent] inventory_statement_timeout_seconds`, 60 s) y `lock_timeout` de 5 s; solo consultas a `pg_catalog` (nunca DDL/DML, nunca filas). Hilo propio (`[agent] inventory_enabled`, `inventory_tick_seconds`), independiente del ETL y del latido.
- **Privilegios mínimos recomendados** (rol dedicado, p. ej. `nexus_inventario`): `CONNECT` sobre la base y `USAGE` sobre cada esquema a vigilar. No necesita `SELECT` sobre las tablas (los catálogos de PostgreSQL son legibles), ni `pg_read_all_data`, ni superusuario. Para una identidad "fuerte" del servidor lee `pg_control_system()` (permitido a `PUBLIC` en PostgreSQL 16); si no puede, usa dirección+puerto del servidor (identidad "débil").
- Hoy el DWH de cada grupo usa las credenciales del grupo (las mismas del ETL); si se desea separar, configure el DWH con un usuario de solo lectura para un grupo que solo inventaría, o espere a la fase de empaquetado para credenciales por propósito.
- **No** se habilita auditoría del motor, triggers ni event triggers en la base del cliente.

### 19.2. Identidad estable y responsable único

- `monitored_database.identity_key = sha256(tipo | motor | host:puerto/base)` normalizados (host en minúsculas, puerto por defecto del motor; DSN para orígenes ODBC). El backend la calcula de la configuración y el agente la recalcula con la conexión que usó: si no coinciden → **409 `identity_mismatch`**.
- Un DWH compartido por varias agencias (o por varios grupos con la misma dirección) es **una sola** base monitoreada; `monitored_database_link` registra qué grupos/empresas la referencian.
- Identidad del **servidor** que reporta el agente, con dos componentes: **fuerte** = `sha256(system_identifier | oid | nombre de la base)` (si el rol puede leer `pg_control_system()`) y **débil** = `sha256(dirección:puerto del servidor | oid | nombre)` (siempre). Solo se compara un componente **comparable** (fuerte con fuerte; si alguno no tiene fuerte, débil con débil) y se completan los que falten: pasar de débil a fuerte (o perder el permiso y volver a débil) o que cambie la IP interna con el mismo clúster **no** es un cambio de servidor.
- Misma base física con **otra configuración** (otro nombre de host, otro grupo). **Solo cuenta la identidad FUERTE**: la débil colisiona trivialmente entre clientes distintos (p. ej. `127.0.0.1:5432`, oid 16384, base `dwh`), así que una coincidencia solo débil **nunca** marca duplicado ni fusiona; solo deja un aviso no bloqueante `possible_duplicate` en el historial de la base.
  - si la identidad fuerte coincide y la configuración original **sigue vigente**, o pertenece a **otro grupo** (u otra empresa, en orígenes) → la nueva queda **`duplicate_of_id`**: se inventaría y alerta una sola vez. **Nunca se mueven línea base ni alertas entre grupos**;
  - si la identidad fuerte coincide, la original **ya no está vigente** y es del **mismo grupo** (p. ej. se cambió el `warehouse_host` del grupo a un alias de la misma base) → el registro original **adopta la nueva identidad** y conserva su línea base, alertas e historial (evento `identity_rebound`); el registro nuevo se fusiona y desaparece. El monitoreo nunca queda apagado.
  - Acción de administrador **Resolver duplicado** (`POST /admin/monitored-databases/{id}/resolve-duplicate {action: merge|undo, reason}`): *fusionar* solo si ambas son del mismo grupo/empresa (si no, 409 `cross_group_merge`), con identidad fuerte coincidente (si no, 409 `weak_identity`) y la original ya no vigente (si no, 409 `original_still_current`); o *deshacer* una detección errónea (no se vuelve a marcar sola; evento `duplicate_undone`). Así, un DWH compartido por grupos distintos queda como duplicado; para inventariarlo por separado use *deshacer*.
- Si un componente comparable **difiere** para una base ya monitoreada (otro servidor en la misma dirección) → el snapshot se trata como **no confiable** (`ENGINE_IDENTITY_CHANGED`) hasta que un administrador **reinicie la línea base**.
- **Lease**: `POST /agent/inventory/lease` asigna cada base a **una** instalación (`lease_ttl_seconds`, defecto 900 s, renovable en cada ciclo). Si el responsable deja de renovar (agente caído), otra instalación con alcance la toma. Un snapshot de una instalación sin lease → **409 `lease_not_held`**; de otra fuera de alcance → **403**. "Liberar responsable" en el panel lo reasigna.

### 19.3. Alcance, exclusiones y origen opcional

- Se inventarían **todos los esquemas de usuario autorizados** (no solo los objetos del catálogo Nexus), para detectar tablas/vistas extra. Siempre se excluyen `pg_catalog`, `information_schema`, `pg_toast*`, `pg_temp_*`. Por base: patrones de inclusión/exclusión (`fnmatch`, p. ej. `ventas_*`) y `[inventory] default_schema_exclude` global. El backend vuelve a aplicar el filtro.
- **DWH**: se registra solo cuando un agente lo alcanza (`dwh_auto_monitor`). Desde la sección 22, el **destino propio** de una empresa es su propia base monitoreada (misma identidad estable = mismo registro; ver 22.5).
- **Origen (DMS)**: **opcional y explícito**: se registra por empresa en el panel (`POST /admin/monitored-databases {"kind":"source","company_id":…}`) y queda **deshabilitado** salvo que se habilite. Hoy el agente inventaría orígenes PostgreSQL; otros motores se registran pero reportan "No se pudo verificar la estructura" (`ENGINE_UNSUPPORTED`).
- Frecuencia configurable por base (`scan_interval_seconds`, defecto `default_interval_seconds` = 3600), independiente del ETL; "Inventariar ahora" pide uno inmediato.

### 19.4. Normalización y huellas

- Por objeto (**base, esquema, nombre, tipo**) una estructura normalizada: columnas **por nombre** (`format_type`, `NOT NULL`, `DEFAULT` con `pg_get_expr`, identidad, generada, collation no por defecto), restricciones por nombre (`pg_get_constraintdef`), índices que no respaldan restricciones (`USING …` de `pg_get_indexdef`), clave/límites de partición, `unlogged`, y para vistas/vistas materializadas **solo la huella** (`sha256`) de `pg_get_viewdef`.
- El orden físico de columnas/restricciones no importa y se colapsan espacios fuera de literales: recrear una vista con otro formato, recrear un índice igual, borrar y volver a agregar una columna igual o refrescar una vista materializada **no** generan alertas.
- Huella del objeto = `sha256` del JSON canónico (claves ordenadas); huella del snapshot = de todas las huellas.

### 19.5. Estados de fiabilidad ("No se pudo verificar la estructura")

| Snapshot | Cuándo | Efecto |
|---|---|---|
| `complete` | todos los esquemas del alcance verificados | se compara |
| `partial` | hay esquemas **sin USAGE** (`schemas_unverifiable`) | se compara, pero **nunca** se infiere eliminación en esos esquemas |
| `unreliable` | conexión/autenticación/consulta fallida (`DWH_CONNECTION_FAILED`…), servidor distinto, motor no soportado, demasiados objetos | **no** se compara; se conserva la línea base; el panel muestra **"No se pudo verificar la estructura"** con la última verificación exitosa |

También se muestra "No se pudo verificar" si no hay intento en `intervalo × stale_factor + lease_ttl` (agente caído).

Reglas conservadoras de eliminación:

- Una **eliminación** solo se registra para esquemas que **este** snapshot listó en `schemas_verified` (y que siguen en el alcance). Un esquema omitido, no verificable o fuera de alcance nunca produce eliminaciones.
- Para que el borrado de un esquema completo sí se detecte, el lease envía `expected_schemas` (esquemas que ya tenían objetos): si alguno ya no existe en `pg_namespace` (visible para cualquier rol), el agente lo reporta como verificado y vacío.
- Un snapshot **sin objetos** cuando antes había objetos en el alcance se trata como **no confiable** (`EMPTY_SNAPSHOT_SUSPICIOUS`): no se borra toda la referencia por un inventario vacío. Si de verdad se eliminó todo, reinicie la línea base.
- Un snapshot con `captured_at` **anterior** al último aceptado de esa base se ignora (`status: ignored`, `stale_snapshot`): datos viejos nunca revierten alertas. Cuenta como intento (`last_attempt_at`), así que la base no queda "vencida" en cada ciclo. `captured_at` más adelantado que `[agent] future_tolerance_hours` → **422** `invalid_time` (un reloj adelantado no congela el monitoreo). El reenvío del mismo `snapshot_id` responde `duplicate`; ese `snapshot_id` usado para otra base → **409** `snapshot_id_conflict`.
- Un DWH que de verdad quedó **vacío** seguirá como `EMPTY_SNAPSHOT_SUSPICIOUS` ("No se pudo verificar") hasta que se **reinicie la línea base**.
- Snapshot de una base inexistente o fuera del alcance de la instalación: **404** en ambos casos (no se revela su existencia).

### 19.6. Línea base

- El primer inventario queda como **propuesta** (`baseline_pending`); **nunca se aprueba sola** ni genera alertas. No se asume que los objetos los creó Nexus: la coincidencia con tablas destino del catálogo se muestra solo como evidencia ("Catálogo Nexus").
- Aprobar (`POST …/baseline/approve {expected_snapshot_id, object_keys?}`) exige que la propuesta revisada siga siendo la última (si llegó otra → 409 `snapshot_changed`). Se puede aprobar un subconjunto: lo no aprobado queda como cambios pendientes ("objeto nuevo").
- En monitoreo no se puede re-aprobar en bloque. **Reiniciar línea base** (motivo obligatorio) cierra las pendientes como reemplazadas (quedan en el historial), borra la referencia (queda en `inventory_baseline_version`, acción `reset`) y pide un inventario nuevo que vuelve a fijar la identidad del servidor.
- Historial de cada cambio de la referencia: `inventory_baseline_version` (versión global, acción `approved|acknowledged|removed|reset`, huella, estructura, actor, fecha).

### 19.7. Cambios estructurales

Una alerta **por objeto** (`object_added`, `object_removed`, `object_modified`) con detalle granular (`column_added/removed/type_changed/nullability_changed/default_changed/attr_changed`, `constraint_added/removed/changed`, `index_added/removed/changed`, `view_definition_changed`, `object_attr_changed`), grupo y base afectados (empresa en orígenes), esquema, objeto y tipo, **primera detección** y **última observación** (+ contador), anterior vs. actual (estructura de la línea base y observada), estado y responsable.

- Una sola **pendiente** por objeto (índice único parcial). Si el mismo estado se observa de nuevo solo se actualiza la última observación. Si el objeto **vuelve a cambiar** mientras está pendiente, la alerta vista **no se modifica en silencio**: pasa a `superseded` y se crea otra (enlazadas). Si vuelve por sí solo a la línea base → `reverted`.
- Estados: `pending`, `acknowledged`, `superseded`, `reverted`, `out_of_scope` (el esquema se excluyó del alcance: la pendiente se cierra al guardar la configuración o en el siguiente inventario; el historial queda). Las alertas estructurales son **independientes** de las incidencias de carga: dar por entendido **no** resuelve incidencias.

### 19.8. "Dar por entendido"

`POST /admin/structural-changes/{id}/acknowledge {attribution: "client"|"nexus", comment?, ticket_ref?, expected_version, expected_observed_fingerprint}`

- Responsable **obligatorio**: "Modificó cliente" o "Modificó equipo Nexus"; comentario y ticket opcionales; usuario del panel (`ack_by` + `ack_by_user_id`) y fecha automáticos.
- Efectos: sale de pendientes; se conserva el historial y el detalle; se incorpora a la línea base **solo esa diferencia** (alta/actualización/baja de ese objeto); **no** acepta otras pendientes; si el objeto cambia después se genera una alerta nueva.
- **Concurrencia optimista**: se bloquea la base y la alerta (mismo orden que la recepción de snapshots) y se verifica `row_version`, la huella mostrada y el último estado observado. Si el objeto cambió otra vez → **409** (`not_pending` con `superseded_by_id`, `stale_version` u `object_changed_again`) y la versión nueva queda pendiente.
- **Reclasificar** (`POST …/reclassify {attribution, reason (obligatorio), ticket_ref?, expected_version}`): solo alertas entendidas; conserva en el historial los valores anteriores, el actor y la fecha; no pisa el reconocimiento original.
- La atribución es **manual** y se guarda separada de la **evidencia técnica** (`evidence`): ejecuciones del agente Nexus que aplicaron DDL sobre el objeto (`task_execution.ddl_applied`: `create_table`, `add_column` con columnas, `constraint_ddl`; sin SQL) y coincidencias con el catálogo. La evidencia se adjunta aunque el reporte de la ejecución llegue después de la detección, pero **nunca** asigna autoría. La autoría real y la hora exacta requieren auditoría del motor, que no se habilita.

### 19.9. Definiciones de vistas (sensibles)

- Por defecto solo viaja y se guarda la **huella**. Con "Guardar SQL de vistas" en la base (`view_definitions_enabled`) el agente envía el texto por TLS y el backend lo guarda **cifrado** (`ENC:` Fernet con `config_secret_key`; sin clave no se guarda). Nunca se registra en logs ni aparece en el detalle general.
- Al **deshabilitar** "Guardar SQL de vistas" se borra el SQL cifrado ya guardado de esa base (estado observado, línea base y alertas; evento `view_definitions_purged`); quedan las huellas.
- Verlo: `GET /admin/structural-changes/{id}/definitions`: **doble llave**, el interruptor global `[inventory] expose_view_definitions = true` **y** el permiso `inventory.view_definitions` sobre los grupos de la base; cada consulta queda en el historial del cambio (con el usuario).

### 19.10. API

| Método y ruta | Uso |
|---|---|
| `POST /agent/inventory/lease` | `{capabilities: {dwh, source_company_ids}, release_ids?}` → objetivos asignados (`granted`, `due`, alcance, frecuencia) |
| `POST /agent/inventory/snapshots` | Resultado del inventario (idempotente por `snapshot_id`). Protecciones en la capa ASGI, **antes** de leer el cuerpo: se verifica la credencial de la instalación (401 sin leerlo ni descomprimirlo), a lo sumo `[server] inventory_max_concurrent` (2) a la vez (429 al resto; el agente reintenta con backoff). Después: límite comprimido `inventory_max_body_bytes` (8 MB), **descomprimido** `inventory_max_decompressed_bytes` (16 MB) en el threadpool (413), un solo miembro gzip completo (concatenados/truncados → 400) y tope de contenedores JSON `inventory_max_json_containers` (defecto 30 × `max_objects_per_snapshot`) **antes** de parsear (413 `too_many_json_containers`). Solo este endpoint acepta gzip (otros → 415). En el resto de `/agent/*` (salvo `enroll`), un cuerpo > 16 KB o chunked también exige credencial válida antes de leerse |
| `GET /admin/inventory/summary`, `GET /admin/structural-changes/badge` | Conteos (panel/dashboard) |
| `GET/POST /admin/monitored-databases`, `GET/PUT /admin/monitored-databases/{id}` | Bases monitoreadas y su configuración (cambios auditados en `monitored_database_event`) |
| `POST /admin/monitored-databases/{id}/scan` · `/release-lease` | Inventario inmediato · liberar responsable |
| `POST /admin/monitored-databases/{id}/resolve-duplicate` | `{action: merge|undo, reason}` (sección 19.2) |
| `GET /admin/monitored-databases/{id}/baseline?view=auto|approved|proposal` · `/baseline/history` | Línea base / propuesta · historial |
| `POST /admin/monitored-databases/{id}/baseline/approve` · `/baseline/reset` | Aprobar · reiniciar (motivo) |
| `GET /admin/structural-changes` | Filtros `view=pending|history`, `status`, `group_id`, `company_id`, `agency_id`, `monitored_database_id`, `schema`, `object`, `change_type`, `attribution` (`client|nexus|none`), `since`, `until`, `limit` |
| `GET /admin/structural-changes/{id}` · `/definitions` | Detalle (anterior vs actual, evidencia, historial, otras alertas del objeto) · SQL (permiso) |
| `POST /admin/structural-changes/{id}/acknowledge` · `/reclassify` | Dar por entendido · reclasificar |

Permisos (sección 20): `inventory.configure`, `inventory.approve_baseline`, `inventory.view_definitions`, `structure.acknowledge`, `structure.reclassify`. Las respuestas incluyen `allowed_actions` (lo que el usuario puede hacer sobre esa base/cambio, evaluado sobre **todos** sus grupos).

### 19.11. Panel

**Estructura** con pestañas *Cambios pendientes*, *Historial* (entendidos con responsable, reemplazados, revertidos, reclasificaciones) y *Bases monitoreadas* (estado de verificación, línea base, última verificación, responsable del inventario, configuración, propuesta con aprobación total o parcial, reinicio, inventarios y eventos). El detalle muestra anterior vs. actual, la evidencia en un recuadro "Evidencia técnica (no prueba autoría)" y el formulario "Dar por entendido" con responsable obligatorio. Horas en `NEXT_PUBLIC_DWH_TIMEZONE` con zona explícita. El Dashboard tiene la tarjeta *Cambios estructurales*.

### 19.12. Variables nuevas

Backend `[inventory]`: `enabled`, `dwh_auto_monitor`, `default_interval_seconds`, `lease_ttl_seconds`, `stale_factor`, `snapshot_retention_days`, `max_objects_per_snapshot`, `default_schema_exclude`, `expose_view_definitions`, `evidence_window_days`, `collapse_partitions`; `[server] inventory_max_body_bytes`, `inventory_max_decompressed_bytes`, `inventory_max_concurrent`, `inventory_max_json_containers`. `captured_at` usa `[agent] future_tolerance_hours`. Agente `[agent]`: `inventory_enabled`, `inventory_tick_seconds`, `inventory_statement_timeout_seconds`. Ver `dwh_back/config_postgres.ini.example` y `dwh_client/config_postgres.ini.example`.

### 19.13. Limitaciones conocidas

- **Muestreo periódico**: un objeto creado y eliminado entre dos inventarios (o un cambio revertido entre dos) **no se detecta**. La granularidad es la frecuencia de la base.
- No se sabe **quién** ni **cuándo exactamente** se hizo un cambio: solo "entre el inventario anterior y este". La atribución es manual; la evidencia Nexus es indicio, no prueba.
- Solo PostgreSQL (DWH y orígenes PostgreSQL). SQL Server/MySQL/Firebird: registrables pero "No se pudo verificar" (`ENGINE_UNSUPPORTED`).
- No se comparan privilegios (GRANT), dueños, triggers, funciones, secuencias ni comentarios.
- Las columnas se comparan por nombre: renombrar una columna aparece como eliminada + agregada.
- **Particiones** (`[inventory] collapse_partitions = true`, defecto): se agrupan bajo su tabla raíz (`partitions`: nombre → límites). Una partición nueva o un índice creado en la raíz (que PostgreSQL propaga a cada partición) generan **una** alerta en la raíz, no una por partición. Contrapartida: cambios hechos solo en una partición (un índice o restricción local) no se detectan. Las **sub-particiones** se aplanan bajo la raíz. **Adjuntar** (`ATTACH PARTITION`) una tabla que antes era independiente aparece como `object_removed` de esa tabla + raíz modificada (y `DETACH` como objeto nuevo). Con `false` cada partición es un objeto propio.
- **DWH compartido por varios grupos con distinto host**: se inventaría una sola vez (duplicado); los vínculos muestran todos los grupos. Si un grupo cambia de host a otra base, el registro original queda con la otra configuración vigente o se fusiona según 19.2.
- **Tamaño**: ~0,4 KB por objeto con pocas columnas (≈100 bytes por columna) sin comprimir; gzip lo reduce ~6×. Con los límites por defecto (8 MB comprimido / **16 MB descomprimido** / `max_objects_per_snapshot` 20000) caben del orden de 8000 tablas de ~20 columnas (suba `inventory_max_decompressed_bytes` si hace falta, considerando memoria × `inventory_max_concurrent`); si se supera, el agente reporta `PAYLOAD_TOO_LARGE` o `TOO_MANY_OBJECTS` ("No se pudo verificar"). Si falla el envío, el agente espera con backoff exponencial por base (tope 1 h) en lugar de re-inventariar cada ciclo.
- El **responsable** del inventario aparece como "vencido" si su agente no está corriendo (no renovó el lease); otra instalación con alcance lo toma en su siguiente ciclo.
- Evidencia técnica: `add_column` solo se asocia a alertas con esas columnas agregadas, `create_table` solo a objetos nuevos y `constraint_ddl` solo a restricciones agregadas/modificadas; el agente marca `constraint_ddl` únicamente si el DDL del catálogo cambió las restricciones (re-ejecutarlo sin cambios no es evidencia). La marca no dice **cuál** restricción cambió: se asocia a cualquier restricción agregada/modificada de esa tabla.
- La estructura recibida se filtra con lista blanca de claves (columnas, restricciones, índices, huella de definición, partición); lo desconocido se descarta.
- `ack_by`/`reclassified_by`/`baseline_approved_by` = usuario del panel (fase 4); los registros anteriores conservan `admin`. El nombre por defecto de un DWH (`DWH <grupo> (…)`) se genera con el grupo que lo registró y puede verse desde otro grupo vinculado (el campo `group_name` sí se oculta).

### 19.14. Pruebas

```
cd dwh_back   && .venv/bin/python -m pytest tests/test_inventory.py -q               # lógica (inventarios construidos)
cd dwh_client && .venv/bin/python -m pytest tests/test_inventory_integration.py -q   # PostgreSQL real + agente
```

- `test_inventory.py`: propuesta sin alertas y aprobación (409 con snapshot viejo, aprobación parcial); alta/modificación/eliminación de tablas y vistas con detalle granular y filtros; "Dar por entendido" con ambas opciones, historial, incorporación de solo esa diferencia, otra pendiente intacta y alerta nueva al volver a cambiar; cambio concurrente (409 y la versión nueva sigue pendiente) y carrera real ack/snapshot con invariantes; pérdida de permisos, conexión o servidor sin eliminaciones falsas y reversión; reclasificación con motivo; lease único, relevo y 403/409; misma base física con otro host sin duplicados; el ack no resuelve incidencias de carga; SQL de vistas cifrado y protegido; origen opcional y motor no soportado; evidencia sin atribución (antes y después de la detección); reinicio de línea base. Regresiones: cambio de host a la misma base (conserva línea base/historial), fusionar/deshacer duplicado, eliminaciones solo en esquemas verificados y snapshot vacío sospechoso, snapshot viejo ignorado, gzip con límite anti zip-bomb (413/400/415), identidad débil→fuerte compatible, colisión de identidad débil entre clientes sin duplicar ni fusionar (y nunca fusión entre grupos), bomba JSON/gzip sin credencial (401 sin leer el cuerpo, RSS < 150 MB, 429 por concurrencia, tope de contenedores), `captured_at` futuro → 422, `snapshot_id` ajeno → 409, lista blanca de estructura y evidencia específica, `out_of_scope`, borrado del SQL de vistas, contador = resumen, limpieza de vínculos.
- `test_inventory_integration.py` (base `nexus_inv_it` y rol de solo lectura en el contenedor DWH): dos agentes del mismo DWH → un solo inventario; crear/modificar/eliminar tablas, vistas y vista materializada reales; normalización sin falsos positivos; `REVOKE USAGE` → parcial sin eliminaciones; `NOLOGIN` → no verificable; sesión de solo lectura (incluso como superusuario); relevo del lease; `ensure_columns_exist` del ETL → evidencia con `execution_id` y sin atribución; sin SQL de vistas ni secretos en logs/BD. Además: particiones agrupadas (una alerta en la raíz), DDL de restricción sin cambios no es evidencia, backoff del runner y `PAYLOAD_TOO_LARGE` ante 413.

---

## 20. Usuarios, roles, permisos por grupo y auditoría (PostgreSQL)

Módulos `dwh_back/panel_auth.py`, `users_postgres.py`, `manage_users.py`, `db_pool.py`, `ratelimit.py`; migración `009_usuarios_permisos`; páginas **Usuarios** y **Auditoría** del panel. Sustituye el acceso con un único token de administrador.

### 20.1. Usuarios y contraseñas

- `panel_user`: usuario (minúsculas, único sin distinguir mayúsculas), nombre visible, correo opcional, activo, superadministrador, `must_change_password`, intentos fallidos, bloqueo, último acceso, auditoría de alta.
- **Hash argon2id** (`argon2-cffi`, parámetros RFC 9106 perfil *low memory*: 64 MiB, t=3, p=4; ≈ 40 ms). Elegido frente a `hashlib.scrypt` por ser el estándar recomendado (OWASP) y re-hashear solo si cambian los parámetros. Nunca se guarda ni registra la contraseña.
- Política: mínimo `[auth] password_min_length` (12), máximo 256, no puede contener el usuario, no puede ser una contraseña común/predecible (lista corta + caracteres repetidos), distinta de la actual. Sin reglas de composición (NIST 800-63B).
- **No hay usuario ni contraseña por defecto.** Primer superadministrador (en el servidor del backend, con el mismo `config.ini`):

  ```
  python migrate.py
  python manage_users.py create-superadmin --username jlimon          # pide la contraseña 2 veces
  NEXUS_NEW_USER_PASSWORD=... python manage_users.py create-superadmin --username jlimon --password-env NEXUS_NEW_USER_PASSWORD
  python manage_users.py list | reset-password --username X | unlock --username X | deactivate --username X | revoke-sessions --username X
  ```

  Por defecto obliga a cambiar la contraseña en el primer inicio (`--no-force-change` lo evita). Cada acción queda en `panel_audit_log` con actor `cli`.

### 20.2. Sesiones

- `POST /admin/auth/login {username, password}` → token **opaco** de 256 bits (`secrets.token_urlsafe(32)`) + perfil + permisos efectivos. En BD solo `sha256(token)` (`panel_session.token_hash`).
- Vencimiento **absoluto** `[auth] session_absolute_seconds` (12 h) y por **inactividad** `session_idle_seconds` (30 min; cada petición renueva `last_seen_at`). Se revoca al cerrar sesión, al cambiar/reiniciar la contraseña (las demás sesiones), al desactivar el usuario o desde **Usuarios → Sesiones activas**.
- `GET /admin/auth/me` (perfil, permisos por grupo y grupos visibles), `POST /admin/auth/logout`, `POST /admin/auth/change-password {current_password, new_password}`.
- Con `must_change_password` todo lo demás responde **403** `password_change_required` (solo `me`, `logout` y `change-password`).
- Credenciales: `Authorization: Bearer <token>` (o `x-session-token`).

### 20.3. Protección contra fuerza bruta

- **Por usuario (BD)**: tras `max_failed_attempts` (5) fallos, bloqueo de `lockout_base_seconds × 2^(fallos − 5)` (30 s, 60 s, 120 s…, tope `lockout_max_seconds` = 1 h). Durante el bloqueo ni la contraseña correcta entra (429 `account_locked`). Un inicio correcto reinicia el contador; **Usuarios → Desbloquear** o `manage_users.py unlock`.
- **Usuarios inexistentes**: mismo bloqueo en memoria (el 429 no revela si el usuario existe) y la contraseña se verifica contra un hash argon2id de referencia (tiempo de respuesta similar; misma respuesta 401 `invalid_credentials`).
- **Por IP**: `ip_max_failures` (30) fallos (de cualquier usuario: cubre el "rociado" de contraseñas) en `ip_window_seconds` (15 min) → 429, **solo si la IP del usuario es conocida y no compartida**. Con IP desconocida, loopback o la de un proxy de confianza (el panel sin clave), **no** hay límite por IP y queda solo el bloqueo por usuario: los fallos de un atacante nunca bloquean a todos los usuarios.
- **Cómo se conoce la IP** (despliegue): `X-Forwarded-For` **nunca** se usa en el backend (uvicorn arranca con `proxy_headers = false`; `[server] proxy_headers`/`forwarded_allow_ips` solo si hay un proxy inverso de confianza delante del backend). El servidor del panel envía `x-nexus-client-ip` + `x-nexus-proxy-key`; el backend lo acepta solo si la petición viene de `[auth] trusted_proxies` (direcciones o redes CIDR) **y** la clave coincide con `[auth] panel_proxy_key` (= `DWH_PANEL_PROXY_KEY` del panel; también `NEXUS_PANEL_PROXY_KEY`). El panel obtiene la IP del navegador solo de fuentes de confianza: `DWH_CLIENT_IP_HEADER` (p. ej. `x-real-ip` que su nginx **sobrescribe** con `$remote_addr`) o `DWH_TRUSTED_PROXY_HOPS = N` (N proxies que **agregan** a `X-Forwarded-For`: se toma el N-ésimo desde el final), nunca el primer valor de `X-Forwarded-For`. Sin configurar (defecto) la IP es desconocida: Next como servidor propio no expone la IP del socket cuando el cliente ya manda `X-Forwarded-For`. Recomendado en producción: nginx con HTTPS delante del panel, `proxy_set_header X-Real-IP $remote_addr;`, `DWH_CLIENT_IP_HEADER=x-real-ip` y la clave compartida.
- Límites en memoria: por proceso (con varias réplicas, el límite es por réplica); el bloqueo por usuario es en BD (compartido).

### 20.4. Permisos, roles y alcance por grupo

Permisos (`panel_permission`):

| Permiso | Qué permite |
|---|---|
| `view` | Consultar todo lo del alcance (salud, cargas, incidencias, ejecuciones, estructura, configuración **sin secretos**) |
| `incident.acknowledge` | Reconocer incidencias y eventos legados (uno o todos) |
| `incident.close_queue` | Cerrar a mano (con motivo) incidencias de cola local |
| `structure.acknowledge` | "Dar por entendido" (atribuir responsable) |
| `structure.reclassify` | Reclasificar un cambio ya entendido |
| `inventory.approve_baseline` | Aprobar / reiniciar la línea base |
| `inventory.configure` | Alta de origen monitoreado, alcance/frecuencia/SQL de vistas, inventariar ahora, liberar responsable, resolver duplicado |
| `inventory.view_definitions` | Ver el SQL de vistas (además del interruptor `[inventory] expose_view_definitions`) |
| `credentials.manage` | Ver y cambiar host/base/usuario/contraseña de origen y DWH, ver/regenerar/revocar tokens de enrolamiento, rotar/revocar instalaciones, canales de notificación (URL/secreto) |
| `config.manage` | Grupos (alta/baja solo con alcance global), empresas, agencias, catálogo, tareas (incl. umbrales de salud y reinicio de watermark); `POST /admin/health/evaluate` requiere alcance global |
| `audit.view` | Consultar `panel_audit_log` (por alcance) |
| `users.manage` | Usuarios, roles y sesiones — **solo alcance global** |

Roles sembrados (`panel_role`): `lectura` (view) · `operador` (view + incident.acknowledge + incident.close_queue) · `atribucion_estructura` (view + structure.acknowledge + structure.reclassify) · `aprobador_inventario` (view + inventory.approve_baseline + inventory.configure) · `definiciones_vistas` (view + inventory.view_definitions) · `admin_credenciales` (view + credentials.manage) · `admin_config` (view + config.manage) · `auditor` (view + audit.view) · `admin_usuarios` (view + users.manage + audit.view; solo global). El **superadministrador** tiene todo en todos los grupos.

**Alcance**: cada asignación `panel_user_role` es un rol en *todos los grupos* (`group_id` NULL) o en *un grupo*. Ej.: operador en el grupo A y lectura en el B. `users.manage` asignado a un grupo se rechaza (422). **Solo un superadministrador** crea, edita, desactiva, desbloquea, reinicia la contraseña, cambia los roles o cierra las sesiones de **otro superadministrador** (403 `superadmin_required`). Nadie se desactiva ni se quita el superadministrador a sí mismo; siempre queda al menos uno activo. Un administrador de usuarios que no es superadministrador **no puede cambiar sus propios roles** (403 `self_roles`, auditado como `users.set_roles_self_denied`): debe hacerlo otro administrador. Aun así `users.manage` puede dar roles a otras cuentas: trátelo como privilegiado.

### 20.5. Aislamiento entre grupos (backend)

- Toda ruta `/admin/*` declara su permiso con `Depends(auth.perm(...))` (o `public`/`authenticated` para la sesión). `tests/test_panel_auth.py` recorre `app.routes` y **falla si una ruta `/admin` nueva no declara permiso**.
- **Listas**: se filtran por los grupos donde el usuario tiene `view` (grupo → empresa → agencia → tarea/objeto; instalación, ejecución, estado de sincronización, incidencia, evento legado, actividad HTTP y auditoría por su `group_id`; canales por su grupo; bases monitoreadas por su grupo **o** un grupo vinculado). Registros sin grupo (canal "todos los grupos", actividad anónima, inicios de sesión) solo con alcance global.
- **Detalle y acciones**: se resuelve el grupo del recurso; si el usuario no lo ve → **404** (igual que inexistente); si lo ve pero le falta el permiso → **403** `permission_required` (con `permission`).
- **Recursos compartidos**: un DWH compartido por varios grupos se ve si alguno está en el alcance, pero modificarlo (y dar por entendido / reclasificar sus cambios, aprobar su línea base) exige el permiso en **todos** sus grupos (el efecto es compartido). El grupo "dueño" fuera del alcance no se nombra (`owner_hidden`). Las respuestas de inventario traen `allowed_actions`.
- **Agregados** (dashboard, `/admin/stats`, `/admin/health/summary`, contadores del menú, `/admin/inventory/summary`, `/admin/structural-changes/badge`, `/admin/incidents/badge`) se calculan solo sobre los grupos permitidos. "Reconocer todos" (eventos) solo toca los grupos donde se puede reconocer.
- Secretos: sin `credentials.manage` sobre el grupo, host/base/usuario llegan `null` (`secrets_hidden`) y los tokens `null` (`token_hidden`), también los **prefijos** de token de `/admin/clients` (`token_preview`) y `/admin/activity` (`token`); las contraseñas nunca se devuelven.
- Nombres de otros grupos: el nombre por defecto de un DWH es neutro (`DWH <base> · <huella>`); si el grupo dueño está fuera del alcance, el nombre (también el heredado de versiones previas) se reemplaza por `Base monitoreada #id (compartida)`, y la instalación responsable del inventario o de un snapshot de otro grupo aparece como "(instalación de otro grupo)".
- Filtro `?group_id=` de un grupo fuera del alcance (enlace compartido): el panel avisa "Grupo fuera de su alcance" y quita el filtro.
- Un DWH compartido por varias agencias sigue siendo **un** inventario (sin cambios de la fase 3).

### 20.6. Token estático (break-glass) y `/monitor/*`

- `x-admin-token` (`[admin] token` / `NEXUS_ADMIN_TOKEN`) **solo** se acepta con `[admin] allow_static_token = true` (defecto **false**; si no, 401 `static_token_disabled`). Equivale a superadministrador con actor `token-admin`; el backend lo avisa al arrancar y **cada uso (también lecturas)** queda en `panel_audit_log` (`auth_kind = static_token`). Úselo solo para emergencias o pruebas automatizadas; en producción déjelo en false.
- `/monitor/*` sigue con su propio `x-monitor-token` para el monitor legado (`dwh_api`), sin cambios de contrato. El panel ya no lo usa: `/admin/events`, `/admin/events/{id}/ack`, `/admin/events/ack-all`, `/admin/clients`, `/admin/activity` son los equivalentes con sesión, permisos y alcance.

### 20.7. Auditoría

`panel_audit_log`: fecha, actor (id + nombre), tipo de autenticación, acción, recurso, grupo, código HTTP, detalles saneados (nunca cuerpos, contraseñas ni tokens) e IP. Se registra: inicio/cierre de sesión, intentos fallidos/bloqueados, cambios de contraseña, administración de usuarios/roles/sesiones, **toda mutación `/admin/*`** (acción = método + ruta, incluidas las rechazadas 403/404) y todo uso del token estático. `GET /admin/audit` (`audit.view`, filtros grupo/actor/acción/resultado/fechas). Los actores reales quedan además en `acknowledged_by(_user_id)`, `resolved_by(_user_id)`, `ack_by(_user_id)`, `reclassified_by(_user_id)`, `baseline_approved_by(_user_id)`, eventos de historial (`actor_user_id`), `client_events.acknowledged_by` e `installation.revoked_by`.

### 20.8. Panel

- **Filtros comunes** (componente `FilterBar`, guardados en la URL para compartir/recargar): grupo, empresa, agencia, base monitoreada, tarea, estado y rango de fechas, según aplique: Salud (grupo/empresa/agencia/tarea/estado de tarea; conectividad aparte; sin fechas: es estado actual), Ejecuciones (todos + etapa/instalación), Incidencias (todos + categoría/severidad; pestañas por estado), Estructura (pendientes e historial: grupo/empresa/agencia/base/fechas + tipo/responsable/estado/esquema/objeto; bases: grupo/empresa/agencia), Eventos (grupo/empresa/agencia/tarea/tipo/fechas), Actividad (grupo/empresa/agencia/estado HTTP/fechas + tipo de cliente), Instalaciones (grupo/empresa/agencia/estado), Empresas/Agencias/Catálogo/Tareas (jerarquía), Auditoría (grupo/resultado/fechas + actor/acción). Las opciones solo incluyen grupos del alcance.
- Acciones visibles solo con permiso (los formularios se abren en solo lectura para ver SQL/DDL sin poder guardar).
- **Usuarios** (`users.manage`): alta con contraseña temporal generada, datos, activar/desactivar, superadministrador (solo superadmin), roles por alcance, reinicio de contraseña (obliga a cambiarla), desbloqueo, sesiones activas con cierre, tabla de roles/permisos.
- **Auditoría** (`audit.view`).
- El menú muestra el usuario y su alcance; los contadores solo cuentan sus grupos.

### 20.9. Pool de conexiones

`get_connection()` presta conexiones de un pool acotado (`db_pool.BoundedPool` sobre `ThreadedConnectionPool`): `[database] pool_min` (5: conexiones ociosas que se **conservan y reutilizan**; psycopg2 cierra al devolverlas las que excedan `minconn`, por eso no puede ser 0), `pool_max` (20: tope simultáneo; las que pasan de `pool_min` se abren bajo demanda y se cierran al devolverse), `pool_timeout_seconds` (10; agotado → 503), `connect_timeout_seconds` (5), `application_name` (`nexus_dwh_back`). Verificable en `pg_stat_activity`: los mismos PID atienden las peticiones y la última consulta de una conexión libre es `DISCARD ALL`. `conn.close()` devuelve la conexión tras `ROLLBACK` + `DISCARD ALL` (ningún estado de sesión ni advisory lock pasa de un préstamo a otro); las rotas se descartan. La pre-autenticación de `/agent/*` y la auditoría usan el mismo pool. Dimensione `pool_max` ≤ `max_connections` de PostgreSQL entre todas las réplicas.

### 20.10. Límites de tasa del API del agente

`POST /agent/enroll`: `[agent] enroll_rate_per_minute` (20) intentos por IP y minuto; `enroll_fail_limit` (10) fallos por IP **y** por prefijo (8 caracteres) del token en `enroll_fail_window_seconds` (900) → 429 + `Retry-After` antes de tocar la BD. Credenciales de instalación inválidas: `auth_fail_limit` (30) por IP en `auth_fail_window_seconds` (300) → 429 (también en la pre-autenticación de cuerpos grandes y del inventario). En memoria, por proceso; detrás de un proxy inverso configure uvicorn con `--proxy-headers`/`--forwarded-allow-ips` para ver la IP real.

### 20.11. Variables nuevas

Backend: `[admin] allow_static_token`; `[auth] session_absolute_seconds`, `session_idle_seconds`, `max_failed_attempts`, `lockout_base_seconds`, `lockout_max_seconds`, `ip_max_failures`, `ip_window_seconds`, `password_min_length`, `trusted_proxies`, `panel_proxy_key` (o `NEXUS_PANEL_PROXY_KEY`); `[server] proxy_headers`, `forwarded_allow_ips`; `[database] pool_min`, `pool_max`, `pool_timeout_seconds`, `connect_timeout_seconds`, `application_name`; `[agent] enroll_rate_per_minute`, `enroll_fail_limit`, `enroll_fail_window_seconds`, `auth_fail_limit`, `auth_fail_window_seconds`. Dependencia nueva: `argon2-cffi`. Panel: se elimina `DWH_MONITOR_TOKEN`; nuevas `DWH_PUBLIC_ORIGIN`, `DWH_PANEL_PROXY_KEY`, `DWH_CLIENT_IP_HEADER`, `DWH_TRUSTED_PROXY_HOPS`. Plantilla: `dwh_back/config_postgres.ini.example`.

### 20.12. Pruebas

```
cd dwh_back && .venv/bin/python -m pytest tests/test_panel_auth.py -q
```

`test_panel_auth.py` (BD propia y tres backends: normal, con límites bajos y pool de 5, y con límite por IP): inicio de sesión correcto/fallido con respuesta genérica; token de sesión solo como hash y contraseña en argon2id (ni en logs, `activity_log` ni auditoría); bloqueo por usuario con backoff exponencial (2 s → 4 s) y usuario inexistente con las mismas reglas; límite por IP; tiempo similar usuario existente/inexistente; vencimiento por inactividad y absoluto; logout revoca; política de contraseñas y cambio (cierra las demás sesiones); `must_change_password` bloquea el resto; token estático deshabilitado por defecto y auditado cuando se habilita; CLI de superadministrador; **todas las rutas `/admin` declaran permiso** (introspección de `app.routes`); matriz de permisos (acción representativa de cada permiso, 200/403/409); secretos y tokens solo con `credentials.manage`; aislamiento A/B en ~20 listas, agregados y contadores, 404 en detalles y mutaciones fuera del alcance (incluido DWH compartido que exige el permiso en todos sus grupos) sin cambios en B; auditoría por alcance; administración de usuarios (roles globales, superadmin, desactivar/reiniciar/sesiones); límites de enrolamiento y de credenciales inválidas; **100 peticiones concurrentes con pool de 5** sin errores ni más de 5 conexiones; DWH compartido que no nombra al grupo dueño fuera del alcance. Regresiones (validación): límite por IP solo con la IP enviada por el panel con clave, `X-Forwarded-For` rotado o clave incorrecta ignorados y sin bloqueo global; IP de sesión solo con clave; administrador de usuarios no superadmin no toca superadmins (6 acciones) ni sus propios roles; contador de sesiones sin las inactivas; prefijos de token ocultos sin credenciales; nombre de DWH neutro y nombres de otros grupos/instalaciones ocultos; el pool **reutiliza** las mismas conexiones (PID estables) y deja `DISCARD ALL`.

Panel (servidor Next): script de prueba de CSRF y proxy (cabecera obligatoria, Origin/Sec-Fetch-Site ajenos → 403, cookie httpOnly/SameSite=Strict, token nunca en el cuerpo, `/monitor/*` y `admin/auth/login` no reenviados, logout revoca en el backend).

### 20.13. Pendiente / fuera de alcance

SSO y MFA (TOTP) — futuros; límites de tasa distribuidos (hoy en memoria por proceso); los nombres por defecto de bases monitoreadas incluyen el grupo que las registró.

---

## 21. Distribución del agente: compilación, servicio de Windows, firma y actualizaciones

Fase 5. Resumen de entrega de todas las fases: [ENTREGA_ENDURECIMIENTO.md](ENTREGA_ENDURECIMIENTO.md).

### 21.1. Herramienta de compilación: evaluación y decisión

| Opción | Qué hace con el código | Protección real | Drivers (psycopg2, pyodbc, pymysql, fdb, cryptography) | Decisión |
|---|---|---|---|---|
| **PyInstaller** (specs actuales `mgd_*.spec`) | Empaqueta el **bytecode** `.pyc` en un archivo dentro del `.exe` | Casi nula: `pyinstxtractor` + un descompilador recuperan el código en minutos | Sí | Solo para los clientes **legados** (MySQL `client.py`); no es protección |
| **Nuitka** (standalone) | Traduce el Python a **C** y lo compila a código máquina; no quedan `.pyc` propios | Media: obliga a ingeniería inversa de código nativo; las cadenas constantes (mensajes, consultas de catálogo) siguen siendo legibles | Sí (probado: el binario compilado pasa `--selftest` y las pruebas de proceso) | **Elegida** |
| Nuitka comercial | Además cifra constantes y trazas, anti-depuración | Algo mayor | Sí | Opcional, requiere licencia; no incluido |
| PyArmor | Ofusca bytecode con runtime propio | Media; licencia comercial por uso; runtime detectable/rompible | Sí, con PyInstaller | No requerido (capa opcional si se licencia) |
| Cython (manual) | Compila módulos a `.pyd` | Parecida a Nuitka | Sí, pero hay que empaquetar igual | Más trabajo de mantenimiento sin ventaja |

**Decisión**: Nuitka `--mode=standalone` (carpeta con `NexusAgent.exe` + DLL/PYD de terceros), **no** `onefile`: onefile se autoextrae en `%TEMP%` en cada arranque (deja archivos, arranca más lento, más falsos positivos de antivirus y no se puede validar/firmar archivo por archivo). Opciones relevantes (`packaging/build_agent.py`): `--python-flag=no_site,isolated,safe_path` (el ejecutable ignora `PYTHONPATH`/`PYTHONHOME`, `site-packages` y el directorio actual), `--python-flag=no_docstrings` (el binario no lleva docstrings; `verify_package.py` lo comprueba), `--nofollow-import-to` pruebas/pytest/pip, `--noinclude-{pytest,setuptools,unittest}-mode=nofollow`, `--remove-output`, `--report` (inventario de lo compilado), metadatos de versión de Windows (compañía, producto, versión = `AGENT_VERSION`) y MSVC. Un **único ejecutable de consola** sirve para todos los modos: `--service` (lo usa el SCM; en la sesión 0 no hay ventana), `--selftest`, `--version`, `--verify-update`, `--once`, `--enroll`.

### 21.2. Qué lleva (y qué no) el paquete

`build/dist/NexusAgent/`: `NexusAgent.exe`, runtime de Python y extensiones/DLL de terceros, `certifi/cacert.pem`, `config.example.ini` (plantilla **sin valores**), `LEEME.txt`, `scripts\` (instalar, desinstalar, actualizar, token de enrolamiento) y `release.json` (+ `release.json.sig` cuando se firma).

`packaging/verify_package.py` (lo ejecuta el build y el CI) **falla** si encuentra: `.py/.pyc/.pyo` o `__pycache__` (los propios siempre; los de terceros también salvo `--allow-third-party-py`), texto de nuestro **código fuente** incrustado en binarios (líneas "canario" que solo existen en las fuentes), `.sql`, carpetas/archivos de pruebas, `config.ini` u otro `.ini`, una plantilla con valores en claves sensibles (`token`, `password`, `secret`…), datos locales del agente (`agent_data`, `*.dpapi`, `agent_credential*`, `agent_state.db*`, `enrollment_token*`), logs, volcados (`*.dmp`), `.env`, llaves privadas PEM, `.pfx/.p12/.key`, o archivos que no coincidan con `release.json`. También falla si encuentra **docstrings** propios (el build compila sin ellos). Las demás **cadenas constantes** (mensajes de log y error, nombres de tablas/campos, consultas al catálogo de PostgreSQL del inventario, URLs de la API) **siguen siendo legibles** con `strings`: es inherente a cualquier compilador. Resultado real del build de prueba en macOS: 71 archivos, 0 `.py/.pyc`, 0 canarios de código ni de docstrings.

**SQL y catálogo**: el paquete no contiene SQL de negocio ni el catálogo de consultas; el agente descarga **solo** las tareas autorizadas para su alcance y las mantiene **en memoria** (fase 1: la config caduca a los `config_max_age_seconds`, nunca se escribe a disco, la cola SQLite rechaza claves como `extract_sql`). El empaquetado no agrega cachés: no hay fuentes que generen `__pycache__` (y `client_postgres.py` fija `sys.dont_write_bytecode`), la carpeta del programa es de solo lectura para el servicio y Nuitka standalone no extrae nada a `%TEMP%`.

**Volcados de memoria (Windows Error Reporting)**: si el proceso fallara, WER puede generar un volcado que contenga memoria del proceso (SQL o credenciales recibidas de Nexus) y, según la política de la máquina, enviarlo a Microsoft o guardarlo en `%ProgramData%\Microsoft\Windows\WER`. El agente **no** habilita volcados propios, no usa `faulthandler` a archivo y el instalador **no modifica** WER (es una política del cliente). Recomendación para el administrador de la sede: revisar `HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting` (consentimiento y `LocalDumps`) según su política; si se habilitan `LocalDumps` para `NexusAgent.exe`, proteger la carpeta de volcados como información sensible.

### 21.3. Servicio de Windows con mínimo privilegio

`scripts\install_service.ps1` (PowerShell como administrador):

| Elemento | Configuración |
|---|---|
| Programa | `C:\Program Files\NexusAgent` — Administradores/SYSTEM control total; servicio y Usuarios **solo lectura/ejecución** (el servicio no puede modificar su binario ni sus DLL) |
| Datos | `C:\ProgramData\NexusAgent\config.ini` (servicio: solo lectura), `data\` (credencial DPAPI, cola SQLite, token de un solo uso) y `logs\` (servicio: modificación). **Sin acceso para Usuarios**; herencia cortada |
| Cuenta | **Cuenta virtual `NT SERVICE\NexusAgent`**: no es administrador, no tiene contraseña que gestionar, SID propio para las ACL; en red sale como la cuenta de equipo |
| Privilegios | `sc privs` = solo `SeChangeNotifyPrivilege` (se quitan `SeImpersonatePrivilege`, `SeCreateGlobalPrivilege`, etc.). Sin `SeDebug`, sin derechos de administrador |
| SID del servicio | `unrestricted` (el tipo `restricted`, más estricto, queda como endurecimiento a validar en Windows real) |
| Inicio | Automático retrasado |
| Recuperación | Reinicio a 1, 5 y 15 min (contador a 24 h) **solo ante caídas** (`failureflag 0`) |
| Red | Solo conexiones **salientes** (HTTPS a Nexus, origen y DWH). No se crean reglas de firewall ni puertos de entrada |
| No hace | No toca Defender, firewall, registro de eventos, auditoría ni WER; no oculta el proceso (aparece como `NexusAgent.exe` / servicio "Nexus DWH Agent") |

- **binPath**: `"C:\Program Files\NexusAgent\NexusAgent.exe" --service --config "C:\ProgramData\NexusAgent\config.ini" --data-dir "C:\ProgramData\NexusAgent\data"`.
- **Parada** (Detener o apagado del equipo): el servicio informa `STOP_PENDING` con un `waitHint` = `shutdown_grace_seconds` + 50 s y llama `agent.stop()`: la tarea en curso termina o hace `ROLLBACK` (sin cargas parciales) y la cola queda en disco. En un apagado del equipo Windows concede pocos segundos: la carga en curso se revierte y se repite en el siguiente arranque (idempotente con claves de upsert, §17.4).
- **Códigos de salida** del servicio (`sc query NexusAgent` → `SERVICE_EXIT_CODE`): 0 parada normal; **2** configuración (p. ej. sin credencial ni token) y **3** credencial revocada o token rechazado: el servicio queda **detenido** (no se reinicia en bucle; el operador corrige). Un error inesperado (1) termina el proceso sin informar `STOPPED` para que el SCM aplique la recuperación.
- **Enrolamiento**: debe hacerlo la **cuenta del servicio** para que DPAPI quede ligado a ella. Por eso **no** se usa `NexusAgent.exe --enroll` desde una consola de administrador (la credencial quedaría cifrada para el administrador). Flujo: `install_service.ps1 -TokenType agency|company|group` (o después `scripts\set_enrollment_token.ps1`) pide el token **sin mostrarlo** y lo escribe en `data\enrollment_token.ini` (ACL de `data\`); al arrancar, el servicio se enrola, guarda su credencial y **borra** ese archivo (si Nexus rechaza el token, lo sobrescribe y borra —el token rechazado **no** queda en claro—, deja una marca `enrollment_token.ini.rechazado` con solo fecha y código, registra que hay que re-enrolar y se detiene con código 3; si al arrancar ya hay credencial y queda un token sin consumir, lo borra). Re-enrolar: `set_enrollment_token.ps1 -Reenroll` (detiene, borra la credencial local, entrega un token nuevo y arranca; revoque la instalación anterior en el panel). El archivo se borra con `unlink` (en SSD no se garantiza el borrado físico): el token de enrolamiento debe tratarse como de corta vida y regenerarse si hay duda.
- **DPAPI y la cuenta**: con `credential_scope = user` (defecto) solo `NT SERVICE\NexusAgent` descifra la credencial. Al arrancar, el servicio registra `Cuenta: … | protección de la credencial: DPAPI (user) OK` (autoprueba en memoria). Los errores que detienen el servicio (configuración, credencial revocada) van al log del agente **y** al Registro de eventos de Windows (Aplicación); si `config.ini` ni siquiera se puede leer, el motivo se escribe en `<DataRoot>\logs\nexus_agent.log`; si fallara con la cuenta virtual, use `credential_scope = machine` (cualquier proceso **de esa máquina** puede descifrar; se compensa con la ACL de `data\`). En ambos casos un **administrador local** puede obtener la credencial (§21.7).
- **Privilegios en las bases** (fuera del agente, a configurar por el DBA):
  - **Origen (DMS)**: login dedicado de **solo lectura** con `SELECT` únicamente sobre los objetos de las consultas (SQL Server: usuario en `db_datareader` o `GRANT SELECT` por objeto; sin `db_owner`, sin `sysadmin`). El origen PostgreSQL ya se abre en modo solo lectura.
  - **DWH**: login dedicado **dueño solo del/los esquema(s) destino** (`CREATE`, `INSERT`, `UPDATE`, `SELECT`, `ALTER` de sus tablas; `CREATE` en la base solo si el agente debe crear esquemas nuevos). Sin superusuario ni `CREATEROLE`.
  - **Inventario**: hoy usa las credenciales DWH del grupo en sesión de solo lectura (§19.1). Un rol de inventario separado (`CONNECT` + `USAGE`) por grupo queda **pendiente** (requiere columnas cifradas nuevas en `client_group`, API, panel y agente).

### 21.4. Compilar

Equipo de build Windows x64: Python 3.12 x64 (python.org) y Visual Studio 2022 Build Tools ("Desktop development with C++").

```powershell
cd dwh_client
powershell -ExecutionPolicy Bypass -File packaging\build_agent.ps1        # venv aislado + Nuitka + verificación
# → build\dist\NexusAgent\  y  build\NexusAgent-<versión>-windows-x64-SIN-FIRMAR.zip
```

Dependencias fijadas: `requirements_postgres.txt` (ejecución: pyodbc, pymysql, psycopg2-binary, requests, **fdb**, **cryptography**, **pywin32** solo Windows) y `requirements_build.txt` (Nuitka 4.2.2). `packaging/build_agent.sh` hace lo mismo en macOS/Linux **solo como prueba de humo** de la configuración de Nuitka (no se distribuye). El CI compila en `windows-latest` (§21.10). Requisitos externos de la máquina destino que **no** van en el paquete: driver ODBC del origen (SQL Server 17/18, Pervasive, Firebird ODBC) y, para Firebird sin DSN, el cliente Firebird (`fbclient.dll`).

### 21.5. Firma de código (Authenticode) — pendiente de certificado

- `packaging/windows/sign_release.ps1` firma `NexusAgent.exe` y `scripts\*.ps1` con **signtool**, SHA-256 y **sello de tiempo RFC 3161** obligatorio (`-TimestampUrl`), verifica (`signtool verify /pa` y `Get-AuthenticodeSignature`) y regenera `release.json` declarando firmante (sujeto y huella). **No crea certificados ni simula firmas**: sin certificado, falla.
- Desde junio de 2023 las claves de firma de código deben estar en **hardware** (token/HSM) o en un servicio de firma: modos `-Mode CertStore -CertThumbprint …` (certificado del almacén con clave en token/HSM, típico en un runner propio) y `-Mode TrustedSigning -DlibPath … -MetadataPath …` (Azure Trusted Signing). `-IncludeThirdPartyBinaries` firma además DLL/PYD de terceros sin firma (para políticas WDAC/AppLocker).
- CI: la firma es un job separado (`firma-windows`) que **nunca corre en pull requests**, usa el entorno protegido `firma-codigo` (configure en GitHub revisores/ramas permitidas) y los valores le llegan por variables de entorno (no se interpolan en el script). Solo corre si el repositorio define `vars.NEXUS_SIGN_MODE` (+ `NEXUS_SIGN_TIMESTAMP_URL`, `NEXUS_SIGN_CERT_THUMBPRINT` o `NEXUS_SIGN_DLIB`/`NEXUS_SIGN_METADATA`, y los secretos `AZURE_CLIENT_ID/TENANT_ID/CLIENT_SECRET` para Trusted Signing). Sin eso el artefacto se llama `NexusAgent-<versión>-windows-x64-SIN-FIRMAR` y `release.json` declara `"authenticode": {"signed": false, "note": "SIN FIRMAR"}`.
- **Hoy no hay certificado**: todos los paquetes son **SIN FIRMAR**. Consecuencias: SmartScreen/directivas pueden bloquear el `.exe` y los `.ps1` (ejecutar con `-ExecutionPolicy Bypass`), y la autenticidad solo la da la firma Ed25519 del manifiesto (§21.6), también pendiente de clave.

### 21.6. Manifiesto de publicación y validación de actualizaciones

- `release.json`: formato `nexus-agent-release/1`, producto, versión, plataforma (`windows-x64`), `min_from_version` opcional, datos del build (fecha, commit, Python, Nuitka), estado Authenticode y **SHA-256 + tamaño de cada archivo**.
- `release.json.sig`: firma **Ed25519** de los bytes exactos de `release.json` con la clave **de publicación** de Nexus. Las claves **públicas** confiables se compilan dentro del agente (`nexus_agent/release_keys.py`): el paquete nuevo lo valida el binario **ya instalado**, así que el ancla de confianza es la versión anterior y no el paquete que se quiere instalar.
- Validación (`NexusAgent.exe --verify-update CARPETA`, `nexus_agent/updates.py`): firma con clave confiable (`--allow-unsigned-manifest` es un modo de transición que solo existe mientras el agente **no** tenga claves compiladas: verifica integridad, **no** autenticidad; en cuanto `release_keys.py` tenga una clave, un manifiesto sin firma se rechaza siempre, sin opción de saltarlo) → formato y plataforma → versión **mayor** que la instalada (sin downgrade; misma versión solo con `--allow-same-version`) y `min_from_version` → **sin enlaces simbólicos ni junctions** (archivos, carpetas o la raíz) → rutas seguras en Windows (sin `..`, sin `:` de flujos alternativos NTFS, sin nombres de dispositivo `CON/PRN/AUX/NUL/COM1-9/LPT1-9` con o sin extensión, sin punto o espacio final, sin duplicados que solo difieran en mayúsculas) → entradas bien formadas (tamaño entero, SHA-256 de 64 hex; un manifiesto hostil da rechazo controlado, sin traza) → cada archivo existe con su tamaño y SHA-256, **ningún archivo extra** (evita DLL plantadas) → si declara Authenticode, firma válida (`WinVerifyTrust`) de los ejecutables. Código de salida 4 = rechazado.
- Pipeline de publicación: `build_agent.ps1` → `sign_release.ps1` (Authenticode; regenera `release.json`) → `tools/sign_manifest.py --package … --key …` en la máquina que custodia la clave (pide la frase de paso sin eco; avisa si la clave no está en `release_keys.py`) → comprimir y distribuir.
- **Clave de publicación** (`tools/gen_release_key.py --out <fuera del repo>`): Ed25519, PEM PKCS#8 **cifrado** con frase de paso; imprime la línea para `release_keys.py`. Custodia recomendada: fuera del repositorio y del CI público (HSM/bóveda o medio fuera de línea con respaldo, dos personas); rotación = agregar la nueva en una versión, publicar con ella y retirar la vieja en la siguiente. `.gitignore` excluye `*.ed25519`, `*.pfx`, `*.p12`. **Pendiente**: generar la clave de producción y agregar su pública a `release_keys.py`; hasta entonces `release_keys.py` está vacío y toda actualización exige `-AllowUnsignedManifest`.
- El agente **no se auto-actualiza** (no descarga ni ejecuta nada por su cuenta). Actualiza el operador con `update_agent.ps1` (§21.9). El panel marca **Desactualizada** a las instalaciones que reportan una versión menor que `[agent] latest_version` del backend (solo informativo).

### 21.7. Límites de la protección (léase)

- La compilación con Nuitka **dificulta** leer y modificar el programa; **no** es cifrado ni DRM. Las cadenas constantes (mensajes, consultas al catálogo de PostgreSQL del inventario, nombres de campos) son legibles en el binario, y las trazas incluyen nombres de módulo y líneas.
- **No impide** extraer de la **memoria** del proceso las consultas SQL, las credenciales de origen/DWH ni el secreto de la instalación que el agente recibe de Nexus para trabajar: cualquiera con privilegios de administrador (o `SeDebugPrivilege`) en la máquina puede volcar la memoria, adjuntar un depurador o leer la credencial DPAPI.
- **No impide** capturar las consultas en el **motor** de base de datos (SQL Server Profiler/Extended Events, `pg_stat_statements`, `log_statement`, auditoría del DBMS) ni en la red interna hacia el origen/DWH si esas conexiones no usan TLS.
- El agente **no** oculta su proceso, **no** desactiva ni interfiere con antivirus, EDR, auditoría o herramientas del cliente, y no pide privilegios de administrador. Es intencional.
- Lo que sí aporta: sin fuentes ni catálogo completo en disco, SQL solo en memoria y solo lo autorizado, credencial por instalación revocable, ACL y cuenta de mínimo privilegio, integridad/autenticidad de actualizaciones (cuando existan la clave y el certificado) y trazabilidad en Nexus (quién descargó qué tarea y cuándo, `task_download_log`).

### 21.8. Instalación nueva (paso a paso)

1. En el panel: crear la agencia/empresa y copiar su token de enrolamiento (prefiera agencia/empresa sobre grupo, §17.9).
2. Copiar el paquete (zip) a la máquina, verificar su origen (SHA-256 publicado junto al zip o firma Authenticode cuando exista) y descomprimirlo en una carpeta **solo para Administradores** (no `C:\Temp` ni el Escritorio), p. ej.:
   ```powershell
   mkdir C:\NexusAgentPkg
   icacls C:\NexusAgentPkg /inheritance:r /grant:r *S-1-5-32-544:(OI)(CI)F *S-1-5-18:(OI)(CI)F
   ```
   Los scripts, además, **copian primero** el paquete a su propia carpeta protegida y validan solo esa copia (evita que alguien cambie archivos entre la validación y el uso).
3. PowerShell **como administrador**, en la carpeta del paquete:
   ```powershell
   NexusAgent.exe --selftest
   powershell -ExecutionPolicy Bypass -File .\scripts\install_service.ps1 -PackageDir . `
       -ApiUrl https://nexus.midominio.com -TokenType agency  [-AllowUnsignedManifest]
   ```
   El instalador crea `C:\Program Files\NexusAgent` nueva (propietario Administradores, solo Administradores/SYSTEM), copia y valida ahí; crea el servicio **deshabilitado**, lo configura (cuenta, SID, privilegios, recuperación, ACL, token) y solo al final lo pasa a automático retrasado; ante cualquier fallo elimina el servicio y la carpeta del programa. Si `C:\ProgramData\NexusAgent` ya existía, el instalador **aborta** cuando algún elemento no pertenece a Administradores/SYSTEM o hay enlaces/junctions (una carpeta precreada por un usuario sin privilegios podría traer un `config.ini` con otro `api_url`); en una reinstalación se aceptan además, solo dentro de `data\` y `logs\`, los archivos cuyo propietario es el SID del propio servicio (`NT SERVICE\NexusAgent`: credencial, cola, logs), y se restablecen los permisos conservando la credencial y la cola. El token de enrolamiento se escribe con `CreateNew` y se borra si la instalación falla. Con un paquete firmado, `-ExpectedSignerThumbprint` fija el firmante (la huella nunca se toma del manifiesto). `-AllowUnsignedManifest` es necesario mientras no exista la clave de publicación. Opcional: `-CredentialScope machine`, `-InstallDir`, `-DataRoot`, `-ExpectedSignerThumbprint`, `-NoStart`.
4. Revisar `C:\ProgramData\NexusAgent\logs\nexus_agent.log` (enrolamiento, `DPAPI (user) OK`) y el panel (Instalaciones → aparece la máquina; Salud → latido). Ajustar `C:\ProgramData\NexusAgent\config.ini` si hace falta (`[agent] …`, §17.10) y reiniciar el servicio.
5. Desinstalar: `scripts\uninstall_service.ps1` (conserva datos) o con `-RemoveProgram -RemoveData`; revocar la instalación en el panel.

### 21.9. Actualización y transición desde instalaciones existentes

**Agente ya instalado con este servicio** (5.2.0 en adelante):

```powershell
# Descomprimir el paquete nuevo en una carpeta solo para Administradores (§21.8) y, como administrador,
# ejecutar SIEMPRE el script de la versión INSTALADA (nunca el que trae el paquete nuevo):
powershell -ExecutionPolicy Bypass -File "C:\Program Files\NexusAgent\scripts\update_agent.ps1" `
    -PackageDir C:\NexusAgentPkg\NexusAgent-5.3.0  [-AllowUnsignedManifest] [-ExpectedSignerThumbprint <huella>]
```

Pasos del script: copia el paquete a `…\NexusAgent.new-<fecha>` creada nueva **solo para Administradores/SYSTEM** (propietario Administradores) y hace **todas** las comprobaciones sobre esa copia: `--verify-update` **con el binario instalado** → Authenticode (instalada firmada: mismo firmante o `-ExpectedSignerThumbprint`; instalada sin firma y nueva firmada —**primer paquete firmado**—: exige `-ExpectedSignerThumbprint`, la huella no se toma del manifiesto; instalada firmada y nueva sin firma: rechazo salvo `-AllowSignatureDowngrade`) → permisos definitivos → detiene el servicio y espera a que el proceso salga (si no para, no cambia nada y lo vuelve a arrancar) → `NexusAgent` → `NexusAgent.prev`, nuevo → `NexusAgent` con **reintentos con espera** si algo tiene la carpeta abierta (mensaje claro: consola/Explorador dentro de la carpeta) → arranca y comprueba salud (servicio en ejecución `-HealthSeconds` y una línea de arranque de la versión nueva **escrita después de este arranque**). **Cualquier** fallo o excepción (incluido un `Start-Service` que falla o un cambio de nombre que no se puede hacer) lleva a la **restauración**: detiene (y si hace falta termina) el proceso nuevo, mueve la versión fallida a `NexusAgent.failed-<fecha>`, devuelve la anterior a su lugar, arranca el servicio y comprueba que corre la versión anterior (código 3). Si la restauración misma no queda completa, lo dice en rojo con el estado y los pasos manuales (código 1): nunca deja el programa ausente ni el servicio detenido en silencio. Códigos: 0 ok, 1 error, 3 restaurado, 4 paquete rechazado. Los datos (`config.ini`, credencial, cola, agenda) no se tocan y son compatibles dentro de 5.x.

**Desde instalaciones anteriores** (compatibilidad):

| Hoy corre… | Transición |
|---|---|
| Agente v5 con fuentes (`python client_postgres.py`) o `.exe` de PyInstaller, con `agent_data` propio | 1) Detener el proceso/servicio anterior (NSSM, tarea programada…). 2) `install_service.ps1` (datos nuevos en `C:\ProgramData\NexusAgent`). 3) La credencial DPAPI anterior está ligada a **otra cuenta**: enrolar de nuevo con `-TokenType` y **revocar** la instalación vieja en el panel. El watermark y el estado de sincronización viven en Nexus (`task_sync_state`), así que la nueva instalación continúa donde quedó; la agenda local se reconstruye con `last_success_at`. 4) Si la cola anterior tenía reportes pendientes, dejar que el agente viejo la vacíe antes (`--once`) o aceptar que esos reportes se pierdan (la carga ya confirmada en el DWH no se repite porque el watermark confirmado es el de Nexus; las no confirmadas se recargan: idempotente con claves de upsert; sin claves puede duplicar, §17.4). |
| Agentes v3/v4 (tokens, endpoints legados) | Igual que arriba; siguen funcionando mientras `[agent] legacy_endpoints = true`. La primera corrida v5 usa el `last_run_at` legado con `legacy_watermark_overlap_seconds` (§17.4). Cuando no quede ninguno (panel → Clientes legados), `legacy_endpoints = false`. |
| Cliente MySQL (`client.py`, `mgd_client.spec`) | Fuera de alcance: sigue con PyInstaller (sin protección) y sus endpoints. |

El backend acepta agentes 5.1 y 5.2 a la vez (la API `/agent/*` no cambió en esta fase); no hay migración de BD nueva en la fase 5.

### 21.10. CI (`.github/workflows/ci.yml`)

- Permisos `contents: read`, acciones fijadas a versión mayor, sin secretos en los logs; se ejecuta en PR, push a `main` y manual.
- **backend-agente** (ubuntu): levanta los tres PostgreSQL con los mismos nombres/puertos del entorno local (§17.12), crea `.venv` y corre `dwh_back/tests` y `dwh_client/tests`. El soporte de pruebas acepta `NEXUS_TEST_CFG_HOST/PORT/PASSWORD`, `NEXUS_TEST_SRC_HOST/PORT/CONTAINER`, `NEXUS_TEST_DWH_HOST/PORT/CONTAINER`, `NEXUS_TEST_BACK_PY`, `NEXUS_TEST_CLIENT_PY` y `NEXUS_TEST_AGENT_EXE` (ejecuta las pruebas de proceso contra el **binario compilado**).
- **panel** (ubuntu): `pnpm install --frozen-lockfile`, `typecheck`, `lint`, `build`.
- **agente-windows** (windows-latest): pruebas de empaquetado (incluye **DPAPI real** en ambos alcances y **WinVerifyTrust**), compilación Nuitka + `verify_package.py`, `--version/--selftest/--verify-update`, y prueba del **servicio real**: instalación con cuenta virtual (comprueba cuenta, privilegios, recuperación, código 2 sin token, `DPAPI (user) OK` con `NT SERVICE\NexusAgent`, ACL), token de un solo uso con Nexus inaccesible (servicio en ejecución reintentando, token ausente del log), `config.ini` inválido → detenido con 2 y motivo en el log (y Registro de eventos, aviso si no aparece), sin acceso de Usuarios a `data\`, actualizaciones con la copia **instalada** de `update_agent.ps1`: paquete cuya versión no arranca como la declarada → **vuelta atrás** (3); paquete cuyo ejecutable no arranca como servicio (`Start-Service` lanza excepción) → **vuelta atrás** (3) y carpeta `failed-*`; carpeta del programa bloqueada por un proceso con el directorio actual dentro → sin pérdida del programa y servicio de nuevo en ejecución; paquete alterado → rechazado (4); tras cada caso, versión original en ejecución y sin carpetas de trabajo; desinstalación. Publica el paquete **SIN FIRMAR** y el informe de Nuitka. **firma-windows**: job aparte (nunca en PR, entorno protegido `firma-codigo`) que descarga ese artefacto, firma, regenera el manifiesto, verifica y publica `…-firmado`.
- **Estado**: definido y revisado, pero **no ejecutado todavía** (se ejecutará al subir la rama). La compilación Windows, el servicio y DPAPI con cuenta virtual solo se validan ahí.

### 21.11. Variables nuevas

- Agente (`NexusAgent.exe` / `client_postgres.py`): argumentos `--service`, `--selftest`, `--verify-update CARPETA`, `--allow-unsigned-manifest`, `--allow-same-version`. Archivo opcional `<data_dir>\enrollment_token.ini` (`[nexus] group_token|agency_token|token`, un solo uso). Sin claves nuevas en `config.ini`.
- Backend: `[agent] latest_version` (informativo, vacío = sin comparación). La API de instalaciones devuelve además `latest_version` y `version_status` (`current|outdated|unknown`).
- Instalador: parámetros de los scripts (`-ApiUrl`, `-TokenType`, `-CredentialScope`, `-InstallDir`, `-DataRoot`, `-AllowUnsignedManifest`, `-ExpectedSignerThumbprint`, `-HealthSeconds`…).
- CI (opcionales, del repositorio): `vars.NEXUS_SIGN_MODE`, `NEXUS_SIGN_TIMESTAMP_URL`, `NEXUS_SIGN_CERT_THUMBPRINT`, `NEXUS_SIGN_DLIB`, `NEXUS_SIGN_METADATA`; secretos `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`.
- Pruebas: `NEXUS_TEST_*` (§21.10).

### 21.12. Pruebas

```
cd dwh_client && .venv/bin/python -m pytest tests/test_packaging.py -q
cd dwh_client && NEXUS_TEST_AGENT_EXE=build/dist/NexusAgent/NexusAgent .venv/bin/python -m pytest tests/test_integration.py tests/test_health_integration.py -q
```

`test_packaging.py`: manifiesto firmado válido (claves **efímeras** generadas en la prueba, no una firma de producción), archivo alterado (tamaño y hash), manifiesto modificado tras firmar, clave desconocida, sin claves confiables, downgrade/misma versión/`min_from_version`, sin firma solo en modo explícito (y aun así con integridad), DLL no declarada, rutas con `..`, Authenticode declarado (válido/inválido/sin firma/no comprobable), plataforma, `--verify-update` por CLI (códigos 0/4), `make_manifest.py` tras firmar; `verify_package.py` (paquete limpio, manifiesto, 14 casos prohibidos, plantilla con valores, terceros tolerados solo en modo explícito, plantilla del repo sin valores); servicio (waitHint, códigos de salida, parada antes/después de crear el agente, `--service` fuera de Windows, `run_agent` en hilo no principal sin señales ni consola); token de un solo uso (prioridad, BOM, se borra al enrolar, se aparta si se rechaza); autoprueba de la credencial; `--version`/`--selftest`; con claves confiables no hay modo sin firma (API y CLI); rutas prohibidas en Windows (18 casos: `:`/ADS, dispositivos reservados, punto/espacio final, unidad, UNC, `..`, vacías, control) y permitidas parecidas (`console.dll`, `com10.dll`); duplicados por mayúsculas; 8 entradas y 5 manifiestos hostiles con rechazo controlado (código 4, sin traza); enlaces simbólicos (archivo, carpeta hacia fuera, raíz); docstrings detectados por `verify_package.py`; token rechazado sin quedar en claro y token sobrante borrado con credencial existente; error de configuración en modo servicio escrito en `<data_dir>/../logs`; en Windows: DPAPI real (`user` y `machine`) y `WinVerifyTrust`.

### 21.13. Pendiente

No probado por falta de certificado: las ramas de Authenticode de los scripts (firmante fijado, primer paquete firmado, rechazo de firmado→sin firmar) solo están revisadas, no ejecutadas. Certificado de firma de código (token/HSM o Azure Trusted Signing) y su configuración en el CI; clave Ed25519 de publicación de producción y su custodia; primera ejecución del job `agente-windows` (compilación, servicio con cuenta virtual, DPAPI) y prueba en un Windows Server real de la sede (drivers ODBC SQL Server/Pervasive/Firebird y `fbclient.dll`); SID de servicio `restricted`; credencial de inventario separada por grupo; `pip --require-hashes` para el build (hoy versiones fijadas sin hashes); instalador MSI (hoy scripts PowerShell).

---

## 22. Destino (DWH) configurable: esquema, SSL/TLS, destino por empresa y "Probar conexión"

Módulos `dwh_back/destination_postgres.py` (regla del destino efectivo), `dwh_back/connection_tests_postgres.py` (prueba de conexión), `dwh_client/nexus_agent/destination.py` (agente) y `dwh_front/components/destination.tsx` (panel). Requiere la migración `010_destino_configurable` y, para las funciones nuevas, el agente **5.3.0**.

### 22.1. Destino efectivo (regla única)

- El **grupo** define el destino predeterminado: host, puerto, base, usuario, contraseña (como antes) + **esquema destino** (`warehouse_schema`, defecto `public`) + **SSL/TLS** (`warehouse_sslmode`, defecto `prefer`; `warehouse_sslrootcert` = PEM de la CA, opcional).
- Cada **empresa** usa por defecto el destino de su grupo (`company.warehouse_mode = 'inherit'`, valor de todas las empresas existentes tras la migración). Con `warehouse_mode = 'custom'` usa un **destino propio completo** (mismos campos; host/base/usuario/contraseña cifrados `ENC:` igual que el resto). No se mezclan campos: o todo del grupo o todo de la empresa. Volver a `inherit` **borra** el destino propio guardado (no quedan credenciales sin uso).
- La misma regla (SQL `CASE WHEN c.warehouse_mode = 'custom' …`) se usa en `/agent/tasks`, en los endpoints legados, en el inventario, en la prueba de conexión y en el panel.
- **Esquema**: una tabla del catálogo **sin** esquema (`clientes`) se carga en `<esquema efectivo>.clientes`; un esquema explícito (`dwh.carter`) se respeta. El backend ya entrega `load_table` calificado (los agentes anteriores entienden `esquema.tabla`) y el agente 5.3 además fija `search_path = <esquema>, public` en la sesión del DWH, así el DDL del catálogo sin esquema (`create_table_sql`, constraint) queda en ese esquema. Si el esquema no existe, el agente lo crea (solo si no existe: `CREATE SCHEMA` exige privilegio `CREATE` sobre la base). Validación: `^[a-z_][a-z0-9_]{0,62}$`, sin `pg_*` ni `information_schema`; siempre se cita con comillas dobles.
- **SSL/TLS** (`sslmode` de libpq): `disable`, `allow`, `prefer` (defecto, comportamiento previo: cifra si el servidor lo ofrece, sin verificar), `require` (falla si no hay SSL; con CA indicada libpq verifica como `verify-ca`), `verify-ca` (**CA obligatoria**: el backend rechaza guardar `verify-ca` sin el PEM), **`verify-full`** (CA + nombre del host; **recomendado** cuando el tráfico sale de la red local; la CA es **opcional**: sin ella el agente usa el almacén de CAs del sistema, `sslrootcert=system`, libpq ≥ 16). El PEM de la CA no es secreto: el agente lo escribe en `<data_dir>/certs/<sha256>.pem` (0600 en POSIX; nombre temporal único + reemplazo atómico) y borra los que ya no usa ninguna configuración vigente ni una prueba en curso. Un error de SSL se reporta con el código estable `DWH_SSL_ERROR` (servidor sin SSL, certificado no verificable, CA inválida).

### 22.2. Permisos (sección 20)

- Destino del grupo (host/puerto/base/usuario/contraseña, **esquema, SSL, CA**) y destino de la empresa (**incluido el cambio de modo** heredar ↔ propio): `credentials.manage` sobre el grupo. Nombre, habilitado, etc.: `config.manage`.
- Sin `credentials.manage` el panel ve el **modo**, el **esquema** y el `sslmode` (no son secretos) y un resumen del destino efectivo sin host/base (`effective_warehouse.host = null`); el PEM de la CA solo como `has_sslrootcert`. La contraseña nunca se devuelve (`has_password` / `warehouse_has_password`).
- **Probar conexión**: `config.manage` sobre el grupo (ver el resultado: `view`). Fuera del alcance → 404.

### 22.3. Probar conexión (la ejecuta el agente, nunca Nexus)

Regla de arquitectura: Nexus **no** abre conexiones hacia las bases ni servidores de los clientes; toda conexión la inicia el agente (saliente, HTTPS). Por eso la prueba es asíncrona:

1. Panel → `POST /admin/connection-tests {target_kind, group_id | company_id}` con `target_kind` = `group_dwh` (DWH del grupo), `company_dwh` (destino **efectivo** de la empresa) o `company_source` (origen/DMS de la empresa). Usa la configuración **guardada** (el panel deshabilita el botón con cambios sin guardar). Auditado como `connection_test.request`.
2. Si no hay ninguna instalación **en línea** (último contacto < `[connection_test] online_seconds`, defecto 180 s) con alcance y que anuncie la capacidad `connection-test` en su heartbeat (agente 5.3+) → `no_agent` inmediato con el motivo. Si hay → `pending`.
3. El agente consulta `POST /agent/connection-tests/claim` cada `[agent] connection_test_poll_seconds` (defecto 10 s; el heartbeat además devuelve `connection_tests_pending` y lo despierta) en un **hilo propio** que nunca bloquea al ETL. Solo recibe pruebas de **su alcance** y con credenciales que ya recibiría para sus tareas: alcance grupo → cualquier prueba del grupo; alcance empresa/agencia → pruebas de su empresa y la del DWH del grupo solo si su empresa lo hereda.
4. El agente responde `POST /agent/connection-tests/{id}/result` (solo la instalación que la tomó; otra → 404; repetido → 409; **fuera de plazo → 410** y la prueba queda `expired`). Nexus vuelve a sanear los mensajes con las credenciales del alcance; `error_code` solo se acepta con formato `^[A-Z0-9_]{1,64}$` y sin contener un secreto conocido (si no → `INVALID_CODE`) y `agent_version` solo como versión (`^[0-9A-Za-z.+-]{0,50}$`, si no `?`).
5. **Límites** (cada prueba es un inicio de sesión en la base del cliente: se evita bloquear su cuenta por intentos repetidos): si ya hay una prueba **abierta** para el mismo destino/origen se devuelve esa (200, `reused: true`; idempotente); entre pruebas del mismo destino debe pasar `min_interval_seconds` (30; si no, **429** `too_soon` con `Retry-After`; las `no_agent` no cuentan); como máximo `max_open_per_group` (5) abiertas por grupo (429 `too_many_open`) y `max_per_user_per_minute` (10) solicitudes por usuario (429 `rate_limited`). El agente además separa `connection_test_min_spacing_seconds` (30) las pruebas a la **misma** conexión y no toma más de `connection_test_max_per_minute` (6).
6. `pending` sin tomar (`pending_ttl_seconds`, 120 s) → `expired`/`NOT_CLAIMED`; tomada sin resultado (`running_ttl_seconds` + `agent_timeout_seconds`) → `expired`/`NO_RESULT`. Retención `retention_days` (30). `GET /admin/connection-tests/{id}` y `GET /admin/connection-tests?target_kind=…&group_id=…&company_id=…&limit=…` (el panel muestra la última). `config_changed = true` si la configuración cambió después de la prueba (huella sin contraseña).

Qué comprueba el agente (**solo lectura**: sesión `default_transaction_read_only`, `statement_timeout`/`connect_timeout` = `agent_timeout_seconds`, `lock_timeout` 3 s; no crea nada):

- DWH: `CONNECT` (conexión + autenticación con el `sslmode`/CA configurados), versión del servidor, **SSL en uso** (`pg_stat_ssl` de su propia sesión; aviso si va sin cifrar), `SCHEMA_EXISTS`, `SCHEMA_USAGE` y `CREATE_TABLE` (`has_schema_privilege`) o, si el esquema no existe, `CREATE_SCHEMA` (`has_database_privilege(…, 'CREATE')`). Resultado `ok` si conecta y puede usar (o crear) el esquema; si no, `failed` con `DWH_INSUFFICIENT_PRIVILEGE`.
- Origen: `CONNECT` con los drivers del ETL (PostgreSQL, MySQL, SQL Server/Pervasive por ODBC, Firebird) y `QUERY` (`SELECT 1`, Firebird `FROM RDB$DATABASE`). No se inventaría ni se leen datos.
- Códigos de error estables del agente (`DWH_AUTH_FAILED`, `DWH_SSL_ERROR`, `DWH_CONNECTION_FAILED`, `DWH_DATABASE_NOT_FOUND`, `SOURCE_…`, …) y mensajes saneados (§17.7): nunca host, usuario, contraseña ni DSN.

### 22.4. Compatibilidad

- **Agentes 5.3** envían `x-nexus-agent-features: destination-v2,connection-test` en cada petición y `features` en el heartbeat. `GET /agent/tasks` entrega además `warehouse` **por tarea** (destino efectivo con `schema`, `sslmode`, `sslrootcert`, `source`) y `withheld_tasks`.
- **Agentes anteriores (5.0–5.2)** solo conocen el `warehouse` general (alcance grupo → DWH del grupo; alcance empresa/agencia → destino efectivo de su empresa). Nexus **retiene** (no entrega; queda en `withheld_tasks` de la respuesta, en el log del backend y en **Instalaciones** del panel: "Tareas retenidas: N — actualice el agente a 5.3") las tareas que un agente anterior no ejecutaría correctamente:
  - `destination_per_company`: la **conexión** efectiva de la tarea difiere de ese `warehouse` (se compara host, puerto, base, **usuario**, contraseña, `sslmode` y CA; misma base con otro usuario también cuenta);
  - `ssl_enforced`: `sslmode` `require`/`verify-ca`/`verify-full` (un agente anterior se conectaría con `prefer`);
  - `schema_ddl`: esquema destino ≠ `public`, tabla del catálogo sin esquema y `create_table_sql`/`create_constraint_sql` que no mencionan el esquema destino (sin el `search_path` del 5.3 ese DDL crearía objetos en `public`). Heurística conservadora: si el DDL ya califica con `<esquema>.` no se retiene.
  - Lo que sí reciben: la tabla destino ya calificada (`esquema.tabla`), así que las cargas sin DDL del catálogo quedan en el esquema correcto. Actualice a 5.3 antes de configurar destinos propios, SSL obligatorio o esquemas con DDL de catálogo.
- **Agente 5.3 contra un Nexus anterior**: sin `warehouse` por tarea usa el general (comportamiento previo); la prueba de conexión responde 404 y el hilo consulta muy de vez en cuando (600 s).
- **Legado v3/v4** (`/configs`, `/agency-configs`): reciben el destino **efectivo** en los mismos campos (`dwh_*`) y la tabla calificada con el esquema. **No se les puede retener nada** (su contrato no lo contempla), así que con estos clientes: el SSL obligatorio **no** se aplica (se conectan con el comportamiento por defecto de su driver) y el DDL del catálogo sin esquema (`query_tabla_destino`, `query_constraint`) se ejecuta en `public` aunque el esquema destino sea otro. Migre esas sedes a 5.3 antes de usar esas opciones (o califique el DDL del catálogo con el esquema). `/group-configs` (un único DWH por respuesta) **excluye** las tareas de empresas con destino propio.

### 22.4.1. Cuándo aplican los cambios y reinicio de la carga

- Un cambio de destino (grupo o empresa) se guarda al instante en Nexus; cada agente lo toma en su **siguiente refresco** de configuración (`refresh_seconds` de la empresa, defecto 60 s). La tarea que **ya está corriendo** termina (o falla) en el destino **anterior**: su transacción se confirma o revierte allí. Si Nexus no está disponible, el agente sigue con la configuración que tenía en memoria hasta `config_max_age_seconds` (defecto 900 s); pasado ese plazo no inicia tareas nuevas (§17.5), así que nunca carga indefinidamente en un destino viejo.
- **Reinicio de la carga**: cuando cambia la **ubicación física** del destino efectivo de una empresa (host, puerto, base o esquema; no usuario, contraseña ni SSL) —por editar el destino del grupo (afecta a las empresas que lo heredan), el destino propio, el modo heredar ↔ propio o mover la empresa de grupo— Nexus reinicia **automáticamente** el watermark y `last_run_at` de los extractores de esas empresas (igual que "Reiniciar última ejecución": la próxima ejecución hace carga completa en las tablas nuevas; los checkpoints de ejecuciones iniciadas antes se ignoran). El `PUT` acepta `reset_sync: false` para no reiniciar. La respuesta trae `destination_changed_companies` y `sync_reset_tasks`, y se audita `destination.change` (empresas cuyo destino cambió, nombres de los campos enviados —nunca valores—, extractores reiniciados).
- El panel lo avisa **antes de guardar** ("Cambia el destino: al guardar se reiniciará la carga de N extractor(es)") con el interruptor *Reiniciar la carga (recomendado)*, activado por defecto.
- El reinicio es **conservador**: abarca todos los extractores de las empresas afectadas, incluso los de tablas con esquema explícito en el catálogo (p. ej. `dwh.carter`, que no cambian de ubicación si solo cambia el esquema) y los casos en que el host solo cambia de nombre (IP ↔ DNS del mismo servidor). La recarga completa es inocua con claves de upsert, pero en tablas **sin claves** (inserción simple) duplica filas: en esos casos desactive el interruptor.

### 22.5. Inventario (sección 19) con destinos por empresa

- Un destino propio es **su propia** base monitoreada (`kind = dwh`) por identidad estable (`host:puerto/base`): se registra automáticamente (`dwh_auto_monitor`) cuando un agente con credenciales de ese destino pide el lease. Dos empresas con el mismo destino propio (o un destino propio igual al del grupo) comparten **un solo** registro, sin duplicados.
- El agente 5.3 declara en el lease `dwh_identities` (las identidades de todos los DWH para los que tiene credenciales: el general y los de sus tareas) y elige la conexión por identidad (nunca otra conexión). Un agente anterior solo recibe su DWH principal.
- El DWH del grupo solo se ofrece si alguna empresa del alcance lo usa (o el alcance es de grupo). Un destino propio se inventaría cuando la empresa tiene al menos una tarea autorizada (es de ahí de donde el agente obtiene las credenciales).
- "Configuración vigente" de una base DWH considera también los destinos propios de las empresas del grupo.

### 22.6. Panel

- **Grupos** → editar: sección *Destino: Data Warehouse* con esquema destino, `sslmode` (con explicación de cada modo), certificado de la CA (PEM) y **Probar conexión** (estados: esperando a un agente → el agente está probando → resultado con cada verificación; *sin agente en línea*; *sin respuesta*). La lista muestra esquema, ssl y cuántas empresas tienen destino propio.
- **Empresas** → editar: *Probar conexión al origen*; sección *Destino (DWH)* con el interruptor **Usar el destino del grupo** (activado por defecto; muestra el destino heredado). Al desactivarlo aparecen los campos del destino propio. *Probar conexión al destino* prueba el destino efectivo. La lista tiene la columna *Destino (DWH)* con la insignia *Destino del grupo* / *Destino propio*, esquema y ssl.
- **Detalle de grupo** (`/grupos/[id]`): esquema/ssl del grupo y, por empresa, la insignia de destino y su esquema.

### 22.7. Variables nuevas

- Backend `[connection_test]`: `enabled` (true), `pending_ttl_seconds` (120), `running_ttl_seconds` (120), `online_seconds` (180), `agent_timeout_seconds` (15), `retention_days` (30), `poll_seconds` (10, sugerencia al agente), `min_interval_seconds` (30), `max_open_per_group` (5), `max_per_user_per_minute` (10).
- Agente `[agent]`: `connection_test_enabled` (true; `false` = el agente no anuncia la capacidad ni consulta pruebas), `connection_test_poll_seconds` (10), `connection_test_min_spacing_seconds` (30), `connection_test_max_per_minute` (6).
- API: `PUT /admin/groups/{id}` y `PUT /admin/companies/{id}` aceptan `reset_sync` (true); salida `inherited_task_count` (grupo) y `task_count` (empresa). `installation.withheld_tasks`/`withheld_at` en `/admin/installations`.
- BD: `installation.withheld_tasks|withheld_at`; `client_group.warehouse_schema|warehouse_sslmode|warehouse_sslrootcert`; `company.warehouse_mode|warehouse_host|warehouse_port|warehouse_database|warehouse_username|warehouse_password|warehouse_schema|warehouse_sslmode|warehouse_sslrootcert`; tabla `connection_test`.

### 22.8. Pruebas

- `dwh_back/tests/test_destination.py` (19; las 6 últimas son regresiones de la validación: resultado hostil con secreto en `error_code`/`agent_version`, límites —60 solicitudes = 1 prueba, 429 por intervalo, por grupo y por usuario—, resultado fuera de plazo 410, retención `schema_ddl` y misma base con otro usuario, reinicio de la carga al cambiar el destino —y no al cambiar solo la contraseña— con auditoría y `reset_sync: false`, `verify-ca` sin CA 422): cifrado y resumen efectivo; validaciones (esquema, sslmode, PEM, puerto, destino propio incompleto, datos sin modo `custom`); permisos (`config.manage` vs `credentials.manage`, lectura sin host, otro grupo 404); volver a heredar borra el destino propio; `/agent/tasks` con destino por tarea y tabla calificada; retención para agentes anteriores (`destination_per_company`, `ssl_enforced`) sin registrar la descarga; endpoints legados con destino efectivo y `/group-configs` sin empresas con destino propio; prueba `no_agent`, permisos y alcance del panel, ciclo completo (claim por alcance, otra instalación 404, repetido 409, saneamiento, auditoría, `config_changed`), alcance de instalaciones de empresa, vencimiento; inventario con destino propio (agente anterior vs 5.3, sin duplicados, destino compartido).
- `dwh_client/tests/test_destination_integration.py` (7, contenedores locales; incluye un usuario con `USAGE`+`CREATE` en el esquema destino pero **sin** `CREATE` en la base que crea tablas nuevas —antes fallaba con "permission denied for database"—): carga real en el destino del grupo (esquema del grupo) y en un destino propio (otra base del contenedor DWH y otro esquema, DDL del catálogo vía `search_path`); `require` contra el contenedor sin SSL → `DWH_SSL_ERROR` limpio en la carga y en la prueba; prueba correcta (versión, SSL no usado con aviso, privilegios; esquema inexistente sin crearlo), contraseña errónea (`DWH_AUTH_FAILED`) y usuario sin privilegios (`DWH_INSUFFICIENT_PRIVILEGE`), origen; inventario con dos bases DWH sin duplicados; sin credenciales en logs ni resultados.
- `dwh_client/tests/test_units.py`: pruebas espaciadas y con tope por minuto en el agente, escritura concurrente atómica de la CA y CA en uso no borrada, esquema/tabla efectiva, `sslmode`/CA (archivo 0600, limpieza, `system`), clasificación `DWH_SSL_ERROR`, elección del DWH por identidad en el inventario, capacidades del heartbeat y cabecera.

### 22.9. Pendiente / fuera de alcance

- Probar valores **sin guardar** del formulario (hoy se guarda y luego se prueba; enviar credenciales no guardadas al agente exigiría almacenarlas temporalmente).
- Certificado de **cliente** (`sslcert`/`sslkey`) para autenticación mutua TLS con el DWH.
- SSL/TLS para los **orígenes** (SQL Server usa `TrustServerCertificate=yes` como antes).
- Separar credenciales de inventario por propósito (sigue §19.1).
- Canal residual en `error_code` de la prueba de conexión: un agente comprometido podría enviar un dato transformado (mayúsculas, `_`) que pase el formato; el agente ya posee esas credenciales, así que el riesgo es bajo (cerrarlo exigiría una lista cerrada de códigos).

---

## 23. Despliegue en Coolify (contenedores)

Backend (`dwh_back`) y panel (`dwh_front`) como **dos aplicaciones** de Coolify construidas desde el repositorio público `JcLimonero/Nexus_DWH` (rama `main`). PostgreSQL 16 corre en el **host** del VPS (BD `NexusDWH`); los agentes de las sedes hablan con el backend por HTTPS y el panel habla con el backend por la **red interna** de Docker.

```
Navegador ──HTTPS──► Traefik (Coolify) ──► panel :3000 ──http interno──► backend :8000 ──► PostgreSQL (host.docker.internal:5432, NexusDWH)
Agentes   ──HTTPS──► Traefik (Coolify) ──────────────────────────────────► backend :8000
```

**Regla de secretos:** ningún secreto va en la imagen ni en el repositorio. Todo se define como **variable de entorno en Coolify** (las marcadas SECRETO las escribe una persona; en Coolify desmarque *Build Variable* para ellas). Las imágenes no copian `config.ini`, `.env*` ni pruebas (`.dockerignore`).

### 23.1. Configuración del backend por variables (`NEXUS__<SECCION>__<CLAVE>`)

`nexus_config.py` (lo usan `main_postgres.py`, `migrate.py` y `manage_users.py`):

- `config.ini` es **opcional** (`NEXUS_CONFIG_FILE` o `dwh_back/config.ini`). Sin archivo se usan los valores por defecto del código.
- Cada variable `NEXUS__<SECCION>__<CLAVE>` **define o sobrescribe** `[seccion] clave` de `config_postgres.ini.example` (sección y clave sin distinguir mayúsculas; separador: doble guion bajo; la clave puede llevar guiones bajos simples). Ejemplos: `NEXUS__DATABASE__PASSWORD`, `NEXUS__DATABASE__POOL_MAX`, `NEXUS__AUTH__TRUSTED_PROXIES`, `NEXUS__CONNECTION_TEST__ENABLED`.
- Una variable **vacía se ignora** (no borra el valor del archivo ni rompe un entero). Nombres mal formados se ignoran con un aviso.
- Al arrancar se registran **solo los nombres** (`database.password, auth.panel_proxy_key, …`), nunca los valores.
- Siguen funcionando las variables específicas anteriores: `NEXUS_CONFIG_SECRET_KEY` (clave Fernet), `NEXUS_PANEL_PROXY_KEY`, `NEXUS_ADMIN_TOKEN`, `NEXUS_CORS_ORIGINS`, `NEXUS_CONFIG_FILE` (se usan cuando la clave no tiene valor en el archivo ni en `NEXUS__…`).
- Nuevo en `[database]`: `sslmode` (`disable|allow|prefer|require|verify-ca|verify-full`; vacío = defecto de libpq) y `sslrootcert`, también para `migrate.py` y `manage_users.py`.
- `[auth] trusted_proxies` y `[server] forwarded_allow_ips` admiten **redes CIDR** (las IP de los contenedores cambian en cada despliegue).

### 23.2. Aplicaciones en Coolify

**Opción recomendada (VPS stage): una sola aplicación Docker Compose** con el `docker-compose.yml` de la raíz: servicios `backend` (8000) y `frontend` (3000) en la misma red interna (el panel usa `DWH_API_URL=http://backend:8000`), `host.docker.internal` ya mapeado (`extra_hosts`) y migraciones al arrancar (`NEXUS_RUN_MIGRATIONS=true`). En Coolify: *+ New → Docker Compose*, repo `JcLimonero/Nexus_DWH`, rama `main`, ubicación `/docker-compose.yml`; dominios por servicio (`frontend` → panel, `backend` → API de agentes). Variables de la aplicación: `DB_USER`, `DB_PASSWORD` (secreto), `DB_NAME` (defecto `NexusDWH`), `NEXUS_CONFIG_SECRET_KEY` (secreto), `NEXUS_PANEL_PROXY_KEY` (secreto, compartida por ambos servicios), `DWH_PUBLIC_ORIGIN` (URL HTTPS del panel); opcionales `PASSWORD_MIN_LENGTH` (mínimo de contraseñas del panel, defecto 12, piso 8), `NEXUS_TRUSTED_PROXIES` / `NEXUS_FORWARDED_ALLOW_IPS` (defecto `10.0.0.0/16`, redes Docker locales del VPS; acótelas a la subred de la aplicación si es posible) y `NEXUS_AGENT_LATEST_VERSION`.

**Alternativa: dos aplicaciones Dockerfile**:

| | Backend | Panel |
|---|---|---|
| Origen | GitHub público `JcLimonero/Nexus_DWH`, rama `main` | igual |
| Build pack | **Dockerfile** | **Dockerfile** |
| Base directory | `/dwh_back` | `/dwh_front` |
| Dockerfile | `/Dockerfile` (dentro de la base) | `/Dockerfile` |
| Puerto expuesto (*Ports Exposes*) | `8000` | `3000` |
| Dominio (ejemplo) | `https://dwh-api.midominio.com` (público: lo usan los agentes) | `https://dwh-panel.midominio.com` |
| Health check | `GET /health` → 200 (`HEALTHCHECK` de la imagen) | `GET /login` → 200 (`HEALTHCHECK` de la imagen) |
| Usuario del proceso | `nexus` (uid 10001), sin privilegios | `nexus` (uid 10001) |

- Imágenes: backend `python:3.12-slim` con solo `requirements_postgres.txt`, `CMD python main_postgres.py --host 0.0.0.0 --port 8000`; panel multi-etapa `node:22-alpine` + pnpm (corepack) con `output: "standalone"` (`node server.js`, `PORT=3000`, `HOSTNAME=0.0.0.0`).
- Health checks: basta el `HEALTHCHECK` de cada Dockerfile; si se activa el de Coolify, use las mismas rutas y puertos (`/health`:8000, `/login`:3000). El `/health` del backend no consulta la BD.
- **Red interna panel → backend**: ambas aplicaciones quedan en la red Docker de Coolify (`coolify` por defecto). Dé al backend un nombre estable en esa red (en Coolify: *Network Aliases* / alias de red del backend, p. ej. `nexus-dwh-back`; según la versión, también sirve el nombre de contenedor con *Consistent Container Names*) y use `DWH_API_URL=http://nexus-dwh-back:8000`. Compruebe desde la terminal del panel: `wget -qO- http://nexus-dwh-back:8000/health`. (Usar la URL pública del backend también funciona, pero la petición sale y vuelve por Traefik y el backend ve la IP pública del VPS: habría que agregar esa IP a `trusted_proxies`; se prefiere la red interna.)
- **PostgreSQL en el host**: `NEXUS__DATABASE__HOST=host.docker.internal`. Compruebe desde la terminal del backend `getent hosts host.docker.internal`; si no resuelve (Linux), agregue en *Custom Docker Options* del backend `--add-host=host.docker.internal:host-gateway`. En el host: `listen_addresses` debe incluir la IP del puente Docker (o `*` con firewall que **no** exponga 5432 a Internet) y `pg_hba.conf` debe permitir la red de Docker (p. ej. `host NexusDWH nexus_app 10.0.0.0/8 scram-sha-256` y/o `172.16.0.0/12`). Use un rol propio (p. ej. `nexus_app`) **dueño** de `NexusDWH` (las migraciones crean tablas), no `postgres`.

### 23.3. Variables del backend

| Variable | Uso | Ejemplo (no secreto) | |
|---|---|---|---|
| `NEXUS__DATABASE__HOST` | Host de PostgreSQL | `host.docker.internal` | |
| `NEXUS__DATABASE__PORT` | Puerto | `5432` | |
| `NEXUS__DATABASE__DB` | Base de configuración | `NexusDWH` | |
| `NEXUS__DATABASE__USER` | Rol de la app | `nexus_app` | |
| `NEXUS__DATABASE__PASSWORD` | Contraseña del rol | — | **SECRETO** |
| `NEXUS__DATABASE__SSLMODE` | TLS a PostgreSQL (opcional; en el mismo host basta vacío/`prefer`) | `prefer` | |
| `NEXUS__DATABASE__POOL_MIN` / `POOL_MAX` | Pool (§20.9) | `5` / `20` | |
| `NEXUS__DATABASE__AUTO_MIGRATE` | Migrar al arrancar dentro del proceso (alternativa a `NEXUS_RUN_MIGRATIONS`) | `false` | |
| `NEXUS_CONFIG_SECRET_KEY` | Clave Fernet para descifrar los `ENC:` de la BD (§5) | — | **SECRETO** |
| `NEXUS__AUTH__PANEL_PROXY_KEY` (o `NEXUS_PANEL_PROXY_KEY`) | Clave compartida con el panel (= `DWH_PANEL_PROXY_KEY`) | — | **SECRETO** |
| `NEXUS__AUTH__TRUSTED_PROXIES` | Desde dónde se acepta `x-nexus-client-ip` + clave (red Docker del panel). Ideal: la subred de la red `coolify` (`docker network inspect coolify`, p. ej. `10.0.1.0/24`) | `10.0.0.0/8,172.16.0.0/12` | |
| `NEXUS__SERVER__PROXY_HEADERS` | Usar `X-Forwarded-For` de Traefik para la IP real de los **agentes** (límites de enrolamiento/credenciales por IP, §20.10). Sin esto todos los agentes comparten la IP de Traefik | `true` | |
| `NEXUS__SERVER__FORWARDED_ALLOW_IPS` | Solo de estas IP/redes se acepta ese `X-Forwarded-For` (la red de Traefik; uvicorn toma la primera IP no confiable desde la derecha, así que un valor falso del cliente no sirve) | `10.0.0.0/8,172.16.0.0/12` | |
| `NEXUS__ADMIN__ALLOW_STATIC_TOKEN` | Token estático break-glass (§20.6). **Defecto `false`**; no lo defina | `false` | |
| `NEXUS__MONITOR__TOKEN` | Solo si se usa el monitor legado `dwh_api` (`/monitor/*`) | — | SECRETO, opcional |
| `NEXUS__AGENT__LATEST_VERSION` | Última versión publicada del agente (§21.6) | `5.3.0` | |
| `NEXUS__AGENT__LEGACY_ENDPOINTS` | `false` cuando no queden agentes v3/v4 | `true` | |
| `NEXUS__CORS__ORIGINS` (o `NEXUS_CORS_ORIGINS`) | Normalmente vacío (el panel no necesita CORS) | — | |
| `NEXUS__HEALTH__*`, `NEXUS__NOTIFICATIONS__*`, `NEXUS__INVENTORY__*`, `NEXUS__CONNECTION_TEST__*`, `NEXUS__SERVER__MAX_BODY_BYTES`, … | Cualquier otra clave de `config_postgres.ini.example` | `NEXUS__NOTIFICATIONS__ALLOW_HTTP=false` | |
| `NEXUS_RUN_MIGRATIONS` | `true` = el contenedor ejecuta `python migrate.py` antes de arrancar (si falla, no arranca) | `false` | |

Con proxy headers activos, las peticiones internas del panel (que no envían `X-Forwarded-For`) conservan la IP del contenedor del panel, así que `trusted_proxies` + clave siguen funcionando.

### 23.4. Variables del panel

| Variable | Uso | Ejemplo (no secreto) | |
|---|---|---|---|
| `DWH_API_URL` | Backend por la red interna | `http://nexus-dwh-back:8000` | |
| `DWH_PANEL_PROXY_KEY` | = `NEXUS__AUTH__PANEL_PROXY_KEY` del backend | — | **SECRETO** |
| `DWH_PUBLIC_ORIGIN` | Origen público para la verificación CSRF | `https://dwh-panel.midominio.com` | |
| `DWH_COOKIE_SECURE` | Cookie `Secure` (HTTPS) | `true` | |
| `DWH_TRUSTED_PROXY_HOPS` | IP del navegador: último valor de `X-Forwarded-For` que agrega Traefik (**recomendado**, ver abajo) | `1` | |
| `DWH_CLIENT_IP_HEADER` | Alternativa: `x-real-ip` (no defina ambas; si está, tiene prioridad) | — | |
| `NEXT_PUBLIC_DWH_TIMEZONE` | Opcional, **de construcción** (*Build Variable*), pública; vacío = `America/Mexico_City` | — | |

Las variables `DWH_*` son solo de servidor y se leen **en tiempo de ejecución** (no se incrustan en el build; verificado en `.next/server`): cambiarlas solo requiere reiniciar, no reconstruir. El panel ya no usa `DWH_MONITOR_TOKEN`.

**IP del navegador detrás de Traefik** (límite de login por IP, §20.3; verificado con Traefik v3 local): Traefik, con su configuración por defecto, **borra** los `X-Forwarded-For`/`X-Real-Ip` que manda el cliente y los reescribe con la IP del socket, así que tanto `DWH_CLIENT_IP_HEADER=x-real-ip` como `DWH_TRUSTED_PROXY_HOPS=1` dan la IP real. Pero si el Traefik del servidor tiene `forwardedHeaders.insecure=true` (o `trustedIPs` amplios), **conserva** el `X-Real-Ip` del cliente (falsificable) mientras que a `X-Forwarded-For` solo **agrega** la IP real al final: `DWH_TRUSTED_PROXY_HOPS=1` sigue siendo correcto en ambos casos, por eso es la opción recomendada. Si hay otro proxy delante de Traefik (p. ej. Cloudflare en modo proxy), súmelo: `DWH_TRUSTED_PROXY_HOPS=2`. Comprobación tras desplegar: intente iniciar sesión con un usuario inexistente enviando `curl -H 'X-Real-Ip: 1.2.3.4' -H 'X-Forwarded-For: 1.2.3.4' -H 'x-nexus-csrf: 1' -H 'Origin: https://dwh-panel.midominio.com' -H 'content-type: application/json' -d '{"username":"prueba_ip","password":"xxxxxxxxxxxxxx"}' https://dwh-panel.midominio.com/api/auth/login` y confirme en **Auditoría** que la IP del intento es la suya y no `1.2.3.4`.

### 23.5. Generar los secretos (en su equipo, nunca en el repo)

```
# Clave Fernet (NEXUS_CONFIG_SECRET_KEY). Si ya hay valores ENC: en la BD, use la clave EXISTENTE.
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Clave compartida panel↔backend (la misma en NEXUS__AUTH__PANEL_PROXY_KEY y DWH_PANEL_PROXY_KEY)
openssl rand -base64 48 | tr -d '\n=+/' ; echo
# Contraseña del rol de PostgreSQL
openssl rand -base64 32 | tr -d '\n=+/' ; echo
# (Opcional) token del monitor legado
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Guárdelos en el gestor de secretos del equipo y péguelos en Coolify (*Environment Variables*, sin *Build Variable*). Distintos por entorno (stage/producción).

### 23.6. Migraciones

- **Recomendado** (control explícito): tras el primer despliegue y en cada versión que traiga migraciones, abra la **Terminal** del backend en Coolify y ejecute `python migrate.py` (o `python migrate.py --status` para ver pendientes). El backend avisa en el log si hay migraciones pendientes.
- **Automático**: `NEXUS_RUN_MIGRATIONS=true` en el backend → el contenedor ejecuta `migrate.py` antes de arrancar (advisory lock: seguro con varias réplicas; si falla, el contenedor no arranca y Coolify conserva la versión anterior). Alternativa dentro del proceso: `NEXUS__DATABASE__AUTO_MIGRATE=true`.
- BD nueva: `migrate.py` aplica la línea base (`schema_postgres.sql`) y todas las migraciones.

### 23.7. Primer superadministrador

En la **Terminal** del backend (Coolify → aplicación → *Terminal*; o `docker exec -it <contenedor> sh`):

```
python migrate.py
python manage_users.py create-superadmin --username jlimon      # pide la contraseña dos veces
python manage_users.py list
```

Usa las mismas variables `NEXUS__DATABASE__*` del contenedor (no hace falta `config.ini`). Si la terminal no es interactiva: `NEXUS_NEW_USER_PASSWORD=... python manage_users.py create-superadmin --username X --password-env NEXUS_NEW_USER_PASSWORD` (evite dejar la contraseña en el historial). Por defecto obliga a cambiar la contraseña en el primer inicio.

### 23.8. Actualizaciones y agentes

- Active *Auto Deploy* (webhook de GitHub) en ambas aplicaciones: cada push a `main` reconstruye y redespliega. Coolify espera a que el contenedor nuevo esté *healthy* antes de retirar el anterior.
- Tras un despliegue con migraciones nuevas: `python migrate.py` (o `NEXUS_RUN_MIGRATIONS=true`).
- Cambiar solo variables: *Restart* (no hace falta reconstruir), salvo `NEXT_PUBLIC_DWH_TIMEZONE` (requiere *Redeploy*).
- Agentes de las sedes: `[server] api_url = https://dwh-api.midominio.com` (la URL HTTPS pública del backend) con `mode = production` (§13, §17). El panel no se expone a los agentes.
- Comprobaciones: `curl https://dwh-api.midominio.com/health` → `{"status":"ok"}`; `https://dwh-panel.midominio.com/login` carga; en el log del backend aparece `Configuración desde variables de entorno: …` (solo nombres).

### 23.9. Prueba local de las imágenes

```
docker build -t nexus-dwh-back dwh_back && docker build -t nexus-dwh-front dwh_front
docker network create nexus-net
docker run -d --name back --network nexus-net -p 127.0.0.1:18000:8000 \
  -e NEXUS__DATABASE__HOST=host.docker.internal -e NEXUS__DATABASE__PORT=5546 \
  -e NEXUS__DATABASE__DB=mgd_dwh_config -e NEXUS__DATABASE__USER=postgres -e NEXUS__DATABASE__PASSWORD=devpass \
  -e NEXUS__AUTH__TRUSTED_PROXIES=10.0.0.0/8,172.16.0.0/12 -e NEXUS__AUTH__PANEL_PROXY_KEY=clave-local nexus-dwh-back
docker run -d --name front --network nexus-net -p 127.0.0.1:13000:3000 -e DWH_API_URL=http://back:8000 \
  -e DWH_PANEL_PROXY_KEY=clave-local -e DWH_COOKIE_SECURE=false -e DWH_PUBLIC_ORIGIN=http://127.0.0.1:13000 nexus-dwh-front
```

(Solo desarrollo: `devpass` es la contraseña del contenedor local de §16.4.) Pruebas: `dwh_back/tests/test_env_config.py` (variables `NEXUS__*`, vacías/mal formadas, sin valores en el resumen, `allow_static_token` falso por defecto, `trusted_proxies` CIDR, `migrate.py`/`manage_users.py` solo con variables).
