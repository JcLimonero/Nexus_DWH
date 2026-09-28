-- ============================================================================
-- 012 — Tabla destino "desde el query": comandos del agente y muestra de datos
-- ============================================================================
-- Requerimiento: crear un extractor a partir del query de origen (el agente lo
-- ejecuta, trae hasta 20 filas de muestra, el usuario elige columnas llave,
-- el backend genera el CREATE TABLE con los tipos detectados y el agente
-- valida el upsert). Nexus NUNCA se conecta a las bases de los clientes: toda
-- ejecución (query de muestra, DDL, validación de upsert) la hace el agente,
-- con el mismo patrón request/claim/result de "Probar conexión" (connection_test,
-- migración 010), generalizado a tres tipos de comando (kind):
--   * query_preview  — ejecuta el query (limitado a 20 filas) y devuelve
--                      columnas/tipos de origen + la muestra;
--   * create_table   — ejecuta en el DWH el CREATE TABLE IF NOT EXISTS que
--                      generó el backend (idempotente);
--   * upsert_check   — re-obtiene la muestra del origen y hace upsert dos
--                      veces dentro de una transacción que termina en ROLLBACK.
--
-- Los datos de negocio (filas de muestra) NUNCA se guardan aquí ni en ninguna
-- otra tabla: el resultado que postea el agente solo persiste sus METADATOS
-- (columnas, tipos, conteos, errores, duración) en la columna result_meta;
-- las filas viven solo en memoria del proceso del backend (TTL corto) y se
-- entregan una sola vez al usuario que pidió el comando (dwh_back/query_preview.py).
-- La columna request SÍ puede contener el texto del query mientras el comando
-- está abierto (el agente lo necesita para ejecutarlo, igual que ya ocurre con
-- agency_task.extract_sql en BD): se purga al expirar/completarse (retención).
--
-- Solo agrega columnas/tabla: rápida y compatible con BD existentes.
-- ============================================================================

ALTER TABLE company ADD COLUMN IF NOT EXISTS allow_data_preview BOOLEAN NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS agent_command (
    id                    UUID         PRIMARY KEY,
    kind                  VARCHAR(20)  NOT NULL
                          CHECK (kind IN ('query_preview', 'create_table', 'upsert_check')),
    group_id              INT          NOT NULL REFERENCES client_group(id) ON UPDATE CASCADE ON DELETE CASCADE,
    company_id            INT          NOT NULL REFERENCES company(id) ON UPDATE CASCADE ON DELETE CASCADE,
    agency_id             INT          REFERENCES agency(id) ON UPDATE CASCADE ON DELETE CASCADE,
    object_catalog_id     INT          REFERENCES object_catalog(id) ON UPDATE CASCADE ON DELETE CASCADE,
    status                VARCHAR(12)  NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'running', 'ok', 'failed', 'expired', 'no_agent')),
    requested_by          VARCHAR(100) NOT NULL DEFAULT '',
    requested_by_user_id  INT          REFERENCES panel_user(id) ON DELETE SET NULL,
    installation_id       UUID         REFERENCES installation(id) ON DELETE SET NULL,
    -- Datos necesarios para que el agente ejecute el comando (query en borrador, DDL generado,
    -- columnas/llaves elegidas). Puede incluir SQL: se purga al terminar/expirar (ver retención).
    request               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- Solo METADATOS del resultado (columnas, tipos, conteos, avisos, duración). Nunca filas.
    result_meta           JSONB,
    has_pending_rows      BOOLEAN      NOT NULL DEFAULT false,
    config_fingerprint    CHAR(64),
    eligible_installations INT         NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    claimed_at            TIMESTAMPTZ,
    finished_at           TIMESTAMPTZ,
    expires_at            TIMESTAMPTZ  NOT NULL,
    error_code            VARCHAR(64),
    message               TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_command_open
    ON agent_command (group_id, created_at) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_agent_command_company_created
    ON agent_command (company_id, created_at DESC);
