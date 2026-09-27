# Nexus DWH — Entrega del endurecimiento (fases 1–5)

Documento de cierre. Detalle técnico completo en [DWH_README.md](DWH_README.md) (secciones 16–21). Aquí: qué existía y qué se implementó, archivos, migraciones y compatibilidad, variables nuevas (sin valores), instrucciones de build/instalación/actualización, pruebas con resultados reales, pendientes y límites conocidos.

Ramas / PR (apilados, mezclar en orden): #1 `feat/panel-admin-dwh` → #2 `feat/agente-identidad-ejecuciones` → #3 `feat/salud-incidencias` → #4 `feat/inventario-estructural` → #5 `feat/panel-roles` → fase 5 `feat/empaquetado-agente`.

---

## 1. Qué existía y qué se implementó

| Área | Antes | Ahora |
|---|---|---|
| **Administración** (PR #1) | Alta de grupos/empresas/agencias/tareas directamente en la BD; monitor `dwh_api` con token | API `/admin` + panel web Next.js (`dwh_front`): CRUD de grupos, empresas, agencias, catálogo y tareas, secretos cifrados `ENC:` nunca devueltos, tokens generados en servidor, Eventos, Actividad, Dashboard |
| **Identidad del agente** (fase 1, PR #2) | Tokens de empresa/grupo/agencia compartidos como credencial permanente; `client_last.py` v4 | Enrolamiento con token → **credencial por instalación** (hash en BD, DPAPI en Windows), rotación con gracia, revocación, `GET /agent/tasks` según el alcance de la instalación, 403 a tareas ajenas, config con caducidad y **SQL solo en memoria**, `task_download_log` sin SQL |
| **Ejecuciones** (fase 1) | `client-event` + `last_run_at` (hora de Nexus al final), cargas no atómicas | `task_execution` por intento y `task_sync_state`; carga en **una transacción**; watermark del reloj del origen al inicio, avanza solo tras COMMIT; reconciliación con checkpoint local; eventos fuera de orden por `agent_seq` |
| **Resiliencia** (fase 1) | Reportes perdidos si Nexus caía | **Cola local SQLite** (solo metadatos) con backoff, deduplicación, sin descartes silenciosos; heartbeat independiente; timeouts en todos los drivers; saneamiento común anti-ReDoS de logs y reportes |
| **Salud** (fase 2, PR #3) | Sin detección de caídas ni retrasos | Evaluador de salud (desconexión, fallas, retrasos, ejecuciones prolongadas), **incidencias** con reconocimiento ≠ resolución, **notificaciones** webhook firmadas con outbox, reintentos y anti-SSRF |
| **Estructura** (fase 3, PR #4) | Sin control de cambios de esquema en el DWH | **Inventario estructural** de solo lectura desde el agente, línea base aprobada, cambios detectados con fiabilidad (`complete/partial/unreliable`), "Dar por entendido" con atribución obligatoria, identidad estable de bases y responsable único (lease) |
| **Usuarios y permisos** (fase 4, PR #5) | Un token de administrador compartido | Usuarios con argon2id, sesiones opacas con vencimiento, bloqueo por fuerza bruta, **permisos por grupo** en las 95 rutas `/admin`, aislamiento entre grupos, auditoría, CSRF, token estático deshabilitado por defecto, pool de conexiones, límites de tasa del API del agente |
| **Distribución** (fase 5) | `python client_postgres.py` o PyInstaller (bytecode extraíble), NSSM, sin firma ni validación de actualizaciones | **Nuitka standalone** (`NexusAgent.exe`, sin fuentes, `.pyc` ni docstrings propios; las cadenas constantes siguen legibles) con verificador de paquete; **servicio de Windows** con cuenta virtual `NT SERVICE\NexusAgent`, solo `SeChangeNotifyPrivilege`, ACL mínimas, parada ordenada y recuperación; enrolamiento por el propio servicio con token de un solo uso; `--selftest`; proceso de **firma Authenticode** (sin certificado: falla, no simula); **manifiesto firmado Ed25519** + validación de actualizaciones (firma, sin downgrade, SHA-256, sin archivos extra, Authenticode) con `update_agent.ps1` y vuelta atrás; versión desactualizada en el panel; **CI** (Linux + Windows) |

## 2. Archivos y componentes modificados

- **Backend** (`dwh_back/`): `main_postgres.py`, `admin_postgres.py`, `agent_postgres.py`, `health_postgres.py`, `inventory_postgres.py`, `panel_auth.py`, `users_postgres.py`, `manage_users.py`, `db_pool.py`, `ratelimit.py`, `redact.py`, `migrate.py`, `migrations/001–009`, `schema_postgres.sql`, `seed_dev_postgres.sql`, `config_postgres.ini.example`, `requirements_postgres.txt`, `tests/` (support, conftest, test_agent_api, test_hardening, test_health, test_inventory, test_panel_auth).
- **Agente** (`dwh_client/`): `client_postgres.py` (punto de entrada), `nexus_agent/` (`agent`, `api`, `etl`, `inventory`, `localstate`, `credstore`, `settings`, `sanitize`, `redact_core`, `logsetup`; fase 5: `cli`, `winservice`, `selftest`, `updates`, `authenticode`, `release_keys`), `config_postgres.ini.example`, `requirements_postgres.txt`, `requirements_build.txt`, `requirements_dev.txt`, `packaging/` (`build_agent.py/.ps1/.sh`, `verify_package.py`, `make_manifest.py`, `LEEME.txt`, `windows/install_service.ps1`, `uninstall_service.ps1`, `update_agent.ps1`, `set_enrollment_token.ps1`, `sign_release.ps1`), `tools/gen_release_key.py`, `tools/sign_manifest.py`, `tests/` (test_units, test_integration, test_health_integration, test_inventory_integration, test_packaging). `client_last.py` eliminado (integrado).
- **Panel** (`dwh_front/`): app completa (login, cambio de contraseña, Dashboard, Grupos, Empresas, Agencias, Catálogo, Tareas, Ejecuciones, Instalaciones, Salud, Incidencias, Notificaciones, Estructura, Eventos, Actividad, Usuarios, Auditoría), proxy de servidor `/api/dwh`, `middleware.ts`, `lib/`, `components/`.
- **Raíz**: `DWH_README.md` (§6.4, §7, §9.2, §10.5, §11, §13, §16–§21), `README.md`, `ENTREGA_ENDURECIMIENTO.md`, `.gitignore`, `.github/workflows/ci.yml`.
- Sin cambios: variante MySQL (`main.py`, `client.py`, `mgd_*.spec`) y monitor `dwh_api` (siguen como legado; PyInstaller no es protección).

## 3. Migraciones y compatibilidad con agentes existentes

- `python migrate.py` (línea base `schema_postgres.sql` + pendientes; `--status`; `[database] auto_migrate`). Registradas en `schema_migrations` con checksum y advisory lock.
  - `001_auditoria_sin_tokens` (⚠ `UPDATE` masivo sobre `activity_log`/`client_events`: ventana de mantenimiento y respaldo), `002_instalaciones`, `003_ejecuciones_y_sync`, `004_rotacion_y_limites`, `005_salud_incidencias`, `006_indices_salud`, `007_inventario_estructural`, `008_inventario_ajustes`, `009_usuarios_permisos`.
  - **Fase 5 no agrega migraciones.**
- **Agentes v3/v4** (tokens, endpoints `/configs`, `/group-configs`, `/agency-configs`, `/client-event`, `/configs/{id}/last_run`): siguen funcionando mientras `[agent] legacy_endpoints = true` (defecto); aparecen como *Clientes legados*. Cambios: prioridad única grupo > agencia > empresa y 403 fuera de alcance en `/client-event`. Al migrar, la primera corrida v5 parte de `last_run_at` con `legacy_watermark_overlap_seconds`. Cuando no quede ninguno: `legacy_endpoints = false` (410).
- **Agentes v5 (5.1) → 5.2**: la API `/agent/*` no cambió; ambos conviven. Las instalaciones con fuentes/PyInstaller pasan al servicio compilado re-enrolándose (la credencial DPAPI está ligada a la cuenta anterior) y revocando la instalación vieja; watermark y estado viven en Nexus, así que continúan donde quedaron (DWH_README.md §21.9).
- **Panel**: el primer superadministrador se crea con `python manage_users.py create-superadmin` (no hay usuario por defecto); `DWH_MONITOR_TOKEN` ya no se usa.

## 4. Variables de configuración nuevas (sin valores)

**Backend `config.ini`** (plantilla comentada: `dwh_back/config_postgres.ini.example`):
- `[database]`: `auto_migrate`, `pool_min`, `pool_max`, `pool_timeout_seconds`, `connect_timeout_seconds`, `application_name`.
- `[admin]`: `allow_static_token` (el `token` existente pasa a ser break-glass).
- `[auth]`: `session_absolute_seconds`, `session_idle_seconds`, `max_failed_attempts`, `lockout_base_seconds`, `lockout_max_seconds`, `ip_max_failures`, `ip_window_seconds`, `password_min_length`, `trusted_proxies`, `panel_proxy_key`.
- `[cors]`: `origins`.
- `[agent]`: `config_max_age_seconds`, `rotation_grace_seconds`, `heartbeat_retention_days`, `download_log_retention_days`, `legacy_endpoints`, `future_tolerance_hours`, `enroll_rate_per_minute`, `enroll_fail_limit`, `enroll_fail_window_seconds`, `auth_fail_limit`, `auth_fail_window_seconds`, **`latest_version`** (fase 5).
- `[server]`: `proxy_headers`, `forwarded_allow_ips`, `max_body_bytes`, `agent_max_body_bytes`, `enroll_max_body_bytes`, `inventory_max_body_bytes`, `inventory_max_decompressed_bytes`, `inventory_max_concurrent`, `inventory_max_json_containers`.
- `[health]`: `evaluator_interval_seconds`, `disconnect_after_seconds`, `heartbeat_expected_seconds`, `default_expected_duration_seconds`, `expected_duration_history`, `delay_tolerance_factor`, `delay_min_grace_seconds`, `running_long_factor`, `startup_grace_seconds`, `execution_retention_days`, `incident_event_retention_days`.
- `[notifications]`: `worker_interval_seconds`, `max_attempts`, `backoff_base_seconds`, `backoff_max_seconds`, `allow_http`, `block_private_ips`, `sending_stale_seconds`, `retention_days`.
- `[inventory]`: `enabled`, `dwh_auto_monitor`, `default_interval_seconds`, `lease_ttl_seconds`, `stale_factor`, `snapshot_retention_days`, `max_objects_per_snapshot`, `default_schema_exclude`, `expose_view_definitions`, `evidence_window_days`, `collapse_partitions`.
- Entorno: `NEXUS_CONFIG_FILE`, `NEXUS_PANEL_PROXY_KEY` (alternativa a `[auth] panel_proxy_key`); existentes: `NEXUS_CONFIG_SECRET_KEY`, `NEXUS_ADMIN_TOKEN`, `NEXUS_CORS_ORIGINS`.

**Agente `config.ini`** (plantilla: `dwh_client/config_postgres.ini.example`; en el paquete `config.example.ini`):
- `[nexus]`: `ca_bundle`, `mode`, `allow_insecure_http`, `installation_name` (`token`, `group_token`, `agency_token` pasan a ser solo de enrolamiento).
- `[agent]`: `data_dir`, `log_dir`, `log_retention_days`, `credential_scope`, `heartbeat_seconds`, `tick_seconds`, `config_max_age_seconds`, `http_connect_timeout`, `http_read_timeout`, `api_retry_base_seconds`, `api_retry_max_seconds`, `run_all_on_start`, `task_retry_attempts`, `task_retry_backoff_seconds`, `shutdown_grace_seconds`, `queue_max_items`, `queue_retention_days`, `queue_backoff_base_seconds`, `queue_backoff_max_seconds`, `queue_max_server_errors`, `queue_poison_min_seconds`, `queue_parked_retry_seconds`, `db_connect_timeout_seconds`, `source_statement_timeout_seconds`, `dwh_statement_timeout_seconds`, `dwh_lock_timeout_seconds`, `fetch_chunk_rows`, `inventory_enabled`, `inventory_tick_seconds`, `inventory_statement_timeout_seconds`, `watermark_clock`, `watermark_overlap_seconds`, `legacy_watermark_overlap_seconds`.
- Fase 5: archivo de un solo uso `<data_dir>\enrollment_token.ini`; argumentos `--service`, `--selftest`, `--verify-update`, `--allow-unsigned-manifest`, `--allow-same-version`. Entorno: `NEXUS_AGENT_CONFIG`.

**Panel `dwh_front/.env.local`** (plantilla `.env.example`): `DWH_API_URL`, `DWH_PUBLIC_ORIGIN`, `DWH_PANEL_PROXY_KEY`, `DWH_CLIENT_IP_HEADER`, `DWH_TRUSTED_PROXY_HOPS`, `DWH_COOKIE_SECURE`, `NEXT_PUBLIC_DWH_TIMEZONE`. Eliminada: `DWH_MONITOR_TOKEN`.

**CI** (opcionales): `vars.NEXUS_SIGN_MODE`, `NEXUS_SIGN_TIMESTAMP_URL`, `NEXUS_SIGN_CERT_THUMBPRINT`, `NEXUS_SIGN_DLIB`, `NEXUS_SIGN_METADATA`; secretos `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`. **Pruebas**: `NEXUS_TEST_CFG_HOST/PORT/PASSWORD`, `NEXUS_TEST_SRC_HOST/PORT/CONTAINER`, `NEXUS_TEST_DWH_HOST/PORT/CONTAINER`, `NEXUS_TEST_BACK_PY`, `NEXUS_TEST_CLIENT_PY`, `NEXUS_TEST_AGENT_EXE`.

## 5. Build, instalación y actualización

- **Backend**: `pip install -r dwh_back/requirements_postgres.txt`; `config.ini` desde la plantilla (secretos por variable de entorno); `python migrate.py`; `python manage_users.py create-superadmin`; `python main_postgres.py` detrás de un proxy HTTPS (DWH_README.md §10, §20).
- **Panel**: `pnpm install --frozen-lockfile && pnpm build && pnpm start`, detrás de nginx que sobrescriba `X-Real-IP` (§16, §20.3).
- **Agente — compilar**: en Windows x64 con Python 3.12 y VS Build Tools: `powershell -ExecutionPolicy Bypass -File dwh_client\packaging\build_agent.ps1` → `build\dist\NexusAgent\` + zip SIN FIRMAR; o el artefacto del job `agente-windows`. Firma (cuando haya certificado): `packaging\windows\sign_release.ps1` → `tools\sign_manifest.py` (§21.4–21.6).
- **Agente — instalar**: descomprimir en una carpeta **solo para Administradores** (no `C:\Temp`); como administrador en la carpeta del paquete: `.\scripts\install_service.ps1 -PackageDir . -ApiUrl https://… -TokenType agency -AllowUnsignedManifest` (copia a una carpeta protegida y valida esa copia; servicio creado deshabilitado y habilitado al final; limpieza ante fallos) (§21.8).
- **Agente — actualizar**: ejecutar SIEMPRE la copia **instalada** `"C:\Program Files\NexusAgent\scripts\update_agent.ps1" -PackageDir <paquete nuevo> [-AllowUnsignedManifest]` (nunca la del paquete nuevo): copia a carpeta solo para Administradores, valida esa copia con el binario instalado (firma, versión, hashes, enlaces, Authenticode; el primer paquete firmado exige `-ExpectedSignerThumbprint`), cambia con reintentos, comprueba salud con líneas posteriores al arranque y restaura ante cualquier fallo o excepción (§21.9). Transición desde instalaciones anteriores: §21.9.

## 6. Pruebas ejecutadas y resultados reales (fase 5, 2026-09-27, macOS arm64 + Docker local)

| Suite | Resultado |
|---|---|
| `dwh_back`: `.venv/bin/python -m pytest tests -q` | **114 passed** (113 previas + versión publicada/desactualizada) |
| `dwh_client`: `.venv/bin/python -m pytest tests -q` | **145 passed, 3 skipped** (70 previas + 75 de `test_packaging.py`, incluidas las correcciones de la validación; las 3 omitidas son solo Windows: DPAPI real ×2 y WinVerifyTrust) |
| Pruebas de proceso contra el **binario compilado con Nuitka** (macOS): `NEXUS_TEST_AGENT_EXE=… pytest tests/test_integration.py tests/test_health_integration.py` | **19 passed** (enrolar, cargar, SIGKILL a mitad de carga sin COMMIT, revocación → salida 3, incidencias con el proceso real) |
| Build Nuitka local (macOS, prueba de humo): `packaging/build_agent.py` | OK en ~1 min 30 s; 71 archivos, 54.4 MB (con `no_docstrings`); `verify_package.py`: OK (0 `.py/.pyc`, 0 canarios de código fuente, 0 docstrings propios: `grep` de 3 docstrings = 0 coincidencias, mientras los mensajes de log sí aparecen); `--version`, `--selftest` (todos los drivers OK), `--verify-update` (rechaza sin firma con 4; acepta con `--allow-unsigned-manifest`) |
| E2E `scratchpad/test_admin.py` (backend de desarrollo :8010) | **68 OK, 0 fallas** |
| E2E `scratchpad/test_front_proxy.py` (panel :3000) | **Todas OK** en la primera ejecución (ver nota) |
| Panel en copia limpia sin `.env.local`: `pnpm typecheck && pnpm lint && pnpm build` | OK (sin avisos de ESLint) |
| CI `.github/workflows/ci.yml` | **No ejecutado aún** (se ejecuta al subir la rama). La compilación Windows, el servicio real con cuenta virtual, DPAPI real y WinVerifyTrust solo se validan ahí |
| Scripts PowerShell | **Sin ejecutar localmente** (no hay Windows ni PowerShell en la máquina de desarrollo); los ejercita el job `agente-windows` |

Nota: una segunda ejecución inmediata de `test_front_proxy.py` falla una comprobación de límite por IP porque la anterior dejó al usuario de prueba dentro de la ventana de bloqueo (efecto de repetir la prueba, no del código de la fase 5, que no toca el panel salvo una insignia).

Resultados de fases anteriores (validador independiente APROBADO en cada una): PR #1 e2e 68/68; fase 1 backend 36/36, cliente 52/52; fase 2 backend 61/61, cliente 57/57; fase 3 backend 86/86, cliente 70/70; fase 4 backend 113/113, cliente 70/70, proxy 22/22.

## 7. Requisitos pendientes

- **Certificado de firma de código** (OV/EV en token/HSM o Azure Trusted Signing) y su configuración en el CI o en un equipo de build; hasta entonces todos los paquetes son **SIN FIRMAR**.
- **Clave Ed25519 de publicación** de producción (`tools/gen_release_key.py`), su custodia (fuera del repo/CI, dos personas, respaldo) y su pública en `nexus_agent/release_keys.py`; hasta entonces las actualizaciones requieren `-AllowUnsignedManifest` (integridad sin autenticidad).
- **Primera ejecución del CI** (job Windows) y validación en un **Windows Server real** de sede: servicio con cuenta virtual, DPAPI de usuario con esa cuenta (si falla: `credential_scope = machine`), SID `restricted` (endurecimiento adicional a probar), drivers ODBC.
- **Validación en vivo** de orígenes SQL Server, MySQL, Pervasive y Firebird (hoy probado solo PostgreSQL); cliente Firebird `fbclient.dll` en sedes sin DSN.
- **Roles de BD dedicados**: origen solo lectura; DWH dueño solo de sus esquemas; rol de inventario separado por grupo (requiere columnas cifradas nuevas, API, panel y agente: no implementado).
- **Infraestructura**: proxy inverso HTTPS (nginx) delante de backend y panel con `X-Real-IP` sobrescrito y `DWH_PANEL_PROXY_KEY`; despliegue en stage (esquema, migraciones 001–009, superadmin); destinos reales de notificación (webhook) — solo probados con receptor local.
- Build reproducible con `pip --require-hashes`; instalador MSI (hoy scripts PowerShell); SSO/MFA del panel; límites de tasa distribuidos.

## 8. Límites conocidos de la protección y del monitoreo

- **Memoria y motor**: la compilación no impide extraer de la memoria del agente las consultas, credenciales de origen/DWH ni el secreto de la instalación (un administrador local o `SeDebugPrivilege` puede volcar el proceso), ni capturar las consultas en el motor (Profiler/Extended Events, `pg_stat_statements`, logs del DBMS) o en la red interna sin TLS. Las cadenas constantes del binario son legibles.
- **Administrador de la máquina**: puede leer la credencial DPAPI (en ambos alcances), `config.ini`, la cola local y los logs; DPAPI protege contra copiar archivos a otra máquina/cuenta, no contra un administrador. **Administrador del servidor Nexus**: ve las consultas y (con la clave maestra) las credenciales.
- **Volcados de memoria (WER)**: dependen de la política de Windows del cliente; el instalador no la modifica.
- El agente no oculta su proceso ni interfiere con antivirus, EDR, auditoría o herramientas del cliente (intencional).
- **Saneamiento**: conservador pero sin parser SQL; valores de negocio "desnudos" (sin comillas/paréntesis/formato reconocible) pueden quedar en mensajes de error (§17.7). No se envían filas ni SQL en ningún payload.
- **Salud**: granularidad del evaluador; clientes legados sin incidencias; entrega de notificaciones al menos una vez; DNS rebinding entre verificación y envío del webhook.
- **Inventario**: muestreo periódico (cambios creados y revertidos entre dos inventarios no se detectan), solo PostgreSQL, sin privilegios/dueños/triggers/funciones/secuencias, renombrado = baja + alta, cambios en una sola partición no detectados, sin autoría real (la evidencia no prueba quién cambió).
- **Panel/API**: límites de tasa en memoria por proceso (varias réplicas multiplican el límite); sin proxy de confianza no hay límite de login por IP; `users.manage` es un permiso privilegiado.
- **Actualizaciones**: manuales (sin auto-actualización); sin clave de publicación ni certificado, la autenticidad del paquete depende del canal de distribución. El modo `-AllowUnsignedManifest` desaparece en cuanto el agente tenga claves de publicación compiladas. Las ramas Authenticode de los scripts (firmante fijado, primer paquete firmado, firmado→sin firmar) están revisadas pero no ejecutadas (no hay certificado).
- **Binario**: sin docstrings ni fuentes, pero las cadenas constantes (mensajes, nombres, consultas de catálogo, rutas de la API) se pueden leer con `strings`.
