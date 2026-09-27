# Nexus DWH — Guía general

Documento único para entender y operar el **stack DWH de Nexus**:

- `dwh_back/` — servidor de **configuración + monitor** (FastAPI).
- `dwh_client/` — **cliente ETL** que corre en cada sede y carga datos al DWH.
- `dwh_api/` — app de **monitoreo** (consume los endpoints `/monitor/*` del backend).
- `dwh_front/` — **panel web de administración** (Next.js) para dar de alta grupos, empresas, agencias, catálogo y tareas, y ver el monitor (solo variante PostgreSQL; ver sección 16).
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
- `redact.py` — saneamiento de textos del backend (errores, detalle de eventos, logs).
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
; Token del panel/API de administración /admin/*. También: NEXUS_ADMIN_TOKEN.
; Vacío = /admin/* responde 503.
; token = TU_ADMIN_TOKEN

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

Monitor (todos requieren header `x-monitor-token`):

- `GET /monitor/events` — historial de eventos (filtros `event_type`, `only_unacknowledged`, `limit`).
- `GET /monitor/clients` — estado agregado por empresa (última conexión, errores pendientes…). Agrupa por `company_id` resuelto (incluye lo reportado con token de grupo/agencia o por instalaciones).
- `GET /monitor/installations` — instalaciones (agentes v5: alcance, estado, `last_seen_at`, versión, cola) + `legacy_clients` (agentes que aún usan tokens; últimos 30 días).
- `GET /monitor/activity` — log HTTP del backend (usualmente solo errores).
- `PUT /monitor/events/{id}/ack` — reconocer una alerta puntual.
- `PUT /monitor/events/ack-all` — reconocer todas las alertas pendientes.

Administración (solo PostgreSQL, header `x-admin-token`; ver `admin_postgres.py`):

- `GET /admin/whoami`, `GET /admin/stats` — validación del token y conteos para el dashboard.
- Grupos: `GET|POST /admin/groups`, `GET|PUT|DELETE /admin/groups/{id}`, `POST /admin/groups/{id}/enable|disable`, `POST /admin/groups/{id}/regenerate-token`, `DELETE /admin/groups/{id}/token`.
- Empresas: `GET|POST /admin/companies` (`?group_id=`), `GET|PUT|DELETE /admin/companies/{id}`, `POST .../enable|disable`, `POST .../regenerate-token`.
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
- `PUT` es parcial (solo los campos enviados). Errores: `401` token admin inválido, `503` admin no configurado, `404` no existe, `409` nombre/token duplicado o registro con dependientes (no se borra en cascada: primero hay que borrar/mover los hijos), `422` validación (p. ej. objeto de otra empresa en una tarea).

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
- `dwh_client/requirements_postgres.txt` (PostgreSQL):
  - `pyodbc`, `pymysql`, `psycopg2-binary`, `requests`.
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

> En producción conviene correr el cliente como **servicio de Windows** (usando los `.spec` de PyInstaller para compilar a `.exe` y el Administrador de servicios / NSSM).

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

## 11. Compilar a ejecutables (PyInstaller)

Plantillas incluidas:

- Backend MySQL: `mgd_server.exe.spec`, `mgd_server.spec`.
- Backend PostgreSQL: `mgd_server_postgres.exe.spec`, `mgd_server_postgres.spec`.
- Utilidad de cifrado: `encrypter.exe.spec`, `mgd_encrypt_config_secret.spec`.
- Cliente MySQL: `mgd_client.exe.spec`, `mgd_client.spec`.
- Cliente PostgreSQL: `mgd_client_postgres.exe.spec`, `mgd_client_postgres.spec`.
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

### 16.1. Qué permite

- **Dashboard**: conteos (grupos, empresas, agencias, tareas), estado por cliente ETL (`/monitor/clients`: última conexión, última ejecución, errores pendientes) y últimos errores sin reconocer.
- **Grupos / Empresas / Agencias**: alta, edición, baja, habilitar/deshabilitar; tokens con mostrar/copiar/regenerar (y revocar en grupo/agencia); contraseñas de **solo escritura**.
- **Catálogo de objetos**: tabla destino, `create_table_sql`, `upsert_keys`, constraint y `static_columns` (editores monoespaciados).
- **Tareas**: por agencia, `extract_sql`, `schedule_seconds` (con atajos), activa, modo empresa (`run_on_company_token`), última ejecución y reinicio de `last_run_at`; filtros por grupo/empresa/agencia.
- **Eventos**: `/monitor/events` con filtros, reconocer uno o todos.
- **Actividad**: `/monitor/activity` (log HTTP).
- **Instalaciones**: agentes enrolados (alcance, estado, último contacto con semáforo, versión, cola, fallos 24 h), acciones **Rotar credencial** y **Revocar**; debajo, **clientes legados** que aún usan tokens.
- **Ejecuciones**: historial por intento con filtros (grupo/empresa/agencia, estado, etapa, instalación, tarea, fecha), filas leídas/cargadas/insertadas/actualizadas, duración, código de error, mensaje saneado y avisos.
- Las fechas de las tablas nuevas se guardan en **UTC** y se muestran en `America/Mexico_City` con la zona explícita (`NEXT_PUBLIC_DWH_TIMEZONE` para cambiarla).

### 16.2. Seguridad

- Login en `/login` con el **token de administrador** del backend. Una ruta del servidor Next lo valida (`GET /admin/whoami`) y lo guarda en una cookie **httpOnly, SameSite=Strict** (Secure en producción). Cerrar sesión la borra.
- El navegador **nunca** habla directo con el backend: todo pasa por el proxy `app/api/dwh/[...path]` (solo rutas `/admin/*` y `/monitor/*`), que agrega `x-admin-token` desde la cookie.
- El **token de monitor** vive solo en el servidor del panel (`DWH_MONITOR_TOKEN`); el proxy lo agrega a `/monitor/*` únicamente tras validar la sesión admin.
- `middleware.ts` redirige a `/login` si no hay sesión.

### 16.3. Variables de entorno (`dwh_front/.env.local`, no se versiona)

| Variable | Descripción |
|----------|-------------|
| `DWH_API_URL` | URL base del backend (p. ej. `http://127.0.0.1:8000`). |
| `DWH_MONITOR_TOKEN` | Igual a `[monitor] token` del backend. |
| `DWH_COOKIE_SECURE` | Opcional (`true`/`false`). Por defecto `true` en producción. |

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

# 3) Panel
cd dwh_front
cp .env.example .env.local      # DWH_API_URL=http://127.0.0.1:8010 y DWH_MONITOR_TOKEN
pnpm install
pnpm dev                        # http://localhost:3000
```

Producción: `pnpm build && pnpm start` detrás de HTTPS. Comprobaciones: `pnpm typecheck`, `pnpm lint`, `pnpm build`.

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
| `GET /agent/tasks` | Tareas autorizadas + credenciales de origen (por empresa) y DWH (del grupo) descifradas, `query_version`, `query_hash`, estado de sync (`watermark`, `watermark_kind`, `last_success_at`, `watermark_reset_at`…), `refresh_seconds`, `config_max_age_seconds`, `credential_rotation_required`. Cada entrega se audita en `task_download_log` (instalación, tarea, versión, IP, fecha; **sin SQL**). |
| `POST /agent/executions` | Inicio de ejecución, idempotente por `execution_id` (UUID generado por el agente). |
| `PUT /agent/executions/{id}` | Avance/fin (`running|success|failed|interrupted`), filas, etapa de fallo, código y mensaje saneado, avisos, `checkpoint {watermark, kind}`. Idempotente; ver 17.3. |
| `POST /agent/heartbeat` | Latido (sección 17.6). |
| `POST /agent/events` | Eventos genéricos (`queue_overflow`, `agent_started`, `agent_stopping`, `config_stale`, `credential_rotated`, `warning`, `dead_letter`), deduplicados por `event_id`. |
| `POST /agent/credentials/rotate` | Rotación del secreto (la pide el agente). |

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
  - se ocultan **todos** los literales entre comillas simples y los literales entre comillas dobles, salvo los que siguen a una palabra de identificador (`relation`, `column`, `table`, `constraint`, `index`, `schema`, `type`, `function`…): se conservan nombres de tablas/columnas/constraints y se ocultan valores (`invalid input syntax for type integer: "***"`), usuarios (`Login failed for user '***'`), `Duplicate entry '***'`, `converting the varchar value '***'`, nombres de objeto de SQL Server entre comillas simples, etc.;
  - listas de valores entre paréntesis (`Key (col)=(***)`, `Failing row contains (***)`, `The duplicate key value is (***)` y cualquier paréntesis con comas o `@`);
  - SQL también **multilínea**: desde `select…from`, `insert…into`, `update…set`, `delete…from`, `with…as`, `merge…into`, `create/alter…`, `exec …` hasta el **final** del mensaje;
  - líneas `DETAIL/LINE/HINT/QUERY/CONTEXT/WHERE`, pares `clave=valor` de conexión/DSN, URLs con credenciales, IPs, puertos y los valores sensibles conocidos (credenciales recibidas de Nexus, tokens, secreto de la instalación).
  - además, en cualquier parte del texto: correos electrónicos, secuencias de ≥ 9 dígitos (y formatos de tarjeta `dddd dddd dddd d…` y teléfono `dd dddd dddd`) y valores entre corchetes `[...]`, salvo la cadena de drivers ODBC y los SQLSTATE (`[Microsoft][ODBC Driver 17 for SQL Server][42S02]`). Se conserva el código numérico inicial de MySQL: `(1062, ***)`. Fechas y horas no se tocan.
  - **Límites residuales**: no se detectan valores de negocio "desnudos" (sin comillas, paréntesis ni corchetes y sin formato reconocible), por ejemplo pares `campo=valor` de negocio cuya clave no es de conexión (`rfc=XAXX…`), fragmentos de filas tipo CSV (`A123;Juan;XAXX…`), teléfonos con espacios o guiones (`+52 (55) 1234-5678`), números de menos de 9 dígitos o nombres sueltos. Los códigos entre corchetes que no son SQLSTATE (p. ej. `[2002]` de MySQL) también se ocultan. Por eso además no se envían filas, SQL ni credenciales en ningún payload (lista negra de claves en la cola local) y los mensajes se recortan a 500/1000 caracteres.
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

Agente (`[agent]`): `data_dir`, `log_dir`, `log_retention_days`, `credential_scope`, `heartbeat_seconds`, `tick_seconds`, `config_max_age_seconds`, `http_connect_timeout`, `http_read_timeout`, `api_retry_base_seconds`, `api_retry_max_seconds`, `run_all_on_start`, `task_retry_attempts`, `task_retry_backoff_seconds`, `shutdown_grace_seconds`, `queue_max_items`, `queue_retention_days`, `queue_backoff_base_seconds`, `queue_backoff_max_seconds`, `db_connect_timeout_seconds`, `source_statement_timeout_seconds`, `dwh_statement_timeout_seconds`, `dwh_lock_timeout_seconds`, `fetch_chunk_rows`, `watermark_clock`, `watermark_overlap_seconds`, `legacy_watermark_overlap_seconds`, `queue_max_server_errors`, `queue_poison_min_seconds`, `queue_parked_retry_seconds`. Variable de entorno opcional `NEXUS_AGENT_CONFIG` (ruta del INI). Plantilla comentada: `dwh_client/config_postgres.ini.example`.

Backend: `[database] auto_migrate`; `[agent] config_max_age_seconds`, `rotation_grace_seconds`, `heartbeat_retention_days`, `download_log_retention_days`, `legacy_endpoints`, `future_tolerance_hours`; `[server] max_body_bytes`, `agent_max_body_bytes`, `enroll_max_body_bytes`; variable de entorno `NEXUS_CONFIG_FILE` (ruta alternativa del `config.ini`, usada por las pruebas). Panel: `NEXT_PUBLIC_DWH_TIMEZONE`.

### 17.11. Uso del agente

```
python client_postgres.py                 # servicio: bucle continuo
python client_postgres.py --once          # ejecuta lo vencido, vacía la cola y sale
python client_postgres.py --enroll        # fuerza un enrolamiento nuevo (borra la credencial local)
python client_postgres.py --config RUTA --data-dir RUTA
```

Códigos de salida: 0 ok, 1 error inesperado (el gestor del servicio debe reiniciar), 2 configuración, 3 credencial revocada/inválida.

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
