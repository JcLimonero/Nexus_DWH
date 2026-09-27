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
| PostgreSQL | PostgreSQL           | `dwh_back/main_postgres.py`, `dwh_client/client_postgres.py` |

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
- `schema_postgres.sql` — esquema **idempotente** de la BD de configuración PostgreSQL.
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
- `POST /client-event` — el cliente reporta `ok`/`error` de una tarea.

Monitor (todos requieren header `x-monitor-token`):

- `GET /monitor/events` — historial de eventos (filtros `event_type`, `only_unacknowledged`, `limit`).
- `GET /monitor/clients` — estado agregado por cliente (última conexión, errores pendientes…).
- `GET /monitor/activity` — log HTTP del backend (usualmente solo errores).
- `PUT /monitor/events/{id}/ack` — reconocer una alerta puntual.
- `PUT /monitor/events/ack-all` — reconocer todas las alertas pendientes.

Administración (solo PostgreSQL, header `x-admin-token`; ver `admin_postgres.py`):

- `GET /admin/whoami`, `GET /admin/stats` — validación del token y conteos para el dashboard.
- Grupos: `GET|POST /admin/groups`, `GET|PUT|DELETE /admin/groups/{id}`, `POST /admin/groups/{id}/enable|disable`, `POST /admin/groups/{id}/regenerate-token`, `DELETE /admin/groups/{id}/token`.
- Empresas: `GET|POST /admin/companies` (`?group_id=`), `GET|PUT|DELETE /admin/companies/{id}`, `POST .../enable|disable`, `POST .../regenerate-token`.
- Agencias: `GET|POST /admin/agencies` (`?group_id=&company_id=`), `GET|PUT|DELETE /admin/agencies/{id}`, `POST .../enable|disable`, `POST .../regenerate-token`, `DELETE .../token`.
- Catálogo: `GET|POST /admin/objects` (`?group_id=&company_id=`), `GET|PUT|DELETE /admin/objects/{id}`, `POST .../enable|disable`.
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

El backend registra cada petición en la tabla `activity_log` (token, método, endpoint, status, duración ms, IP, detalle de error si lo hubo). Esto alimenta `/monitor/activity`.

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

Se escriben en `dwh_client/logs/nexus_YYYY-MM-DD.log`. Se conservan los **últimos 7** archivos.

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
- [ ] Logs del cliente (`dwh_client/logs/`) protegidos — no suelen llevar contraseñas, pero sí SQL potencialmente sensible.

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
