-- ============================================================================
-- 003 — Versionado de queries, ejecuciones y estado de sincronización
-- ============================================================================
-- agency_task.query_version: entero que sube (trigger) cada vez que cambia
--   extract_sql. query_hash = sha256(extract_sql) calculado en el servidor.
-- task_execution: un registro por INTENTO de ejecución (execution_id lo genera
--   el agente). Todas las fechas en UTC (timestamptz).
-- task_sync_state: estado confirmado por tarea (watermark, último éxito,
--   fallos consecutivos). Es un concepto distinto a las ejecuciones.
--   watermark es TIMESTAMP SIN zona: está en el dominio del reloj del ORIGEN
--   (o del agente si no se pudo leer el reloj del origen, ver watermark_kind).
-- ============================================================================

ALTER TABLE agency_task ADD COLUMN IF NOT EXISTS query_version INT NOT NULL DEFAULT 1;
ALTER TABLE agency_task ADD COLUMN IF NOT EXISTS query_hash    CHAR(64);

-- Relleno inicial del hash sin tocar updated_at.
ALTER TABLE agency_task DISABLE TRIGGER agency_task_updated_at;
UPDATE agency_task
   SET query_hash = encode(sha256(convert_to(extract_sql, 'UTF8')), 'hex')
 WHERE query_hash IS NULL;
ALTER TABLE agency_task ENABLE TRIGGER agency_task_updated_at;

CREATE OR REPLACE FUNCTION agency_task_query_version()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.query_version := 1;
    ELSIF NEW.extract_sql IS DISTINCT FROM OLD.extract_sql THEN
        NEW.query_version := OLD.query_version + 1;
    ELSE
        -- La versión solo la mueve el trigger (no se puede fijar a mano).
        NEW.query_version := OLD.query_version;
    END IF;
    NEW.query_hash := encode(sha256(convert_to(NEW.extract_sql, 'UTF8')), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agency_task_query_version ON agency_task;
CREATE TRIGGER agency_task_query_version
    BEFORE INSERT OR UPDATE ON agency_task
    FOR EACH ROW EXECUTE PROCEDURE agency_task_query_version();


CREATE TABLE IF NOT EXISTS task_execution (
    execution_id              UUID         PRIMARY KEY,
    installation_id           UUID         NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
    task_id                   INT          NOT NULL,
    group_id                  INT,
    company_id                INT,
    agency_id                 INT,
    object_catalog_id         INT,
    client_version            VARCHAR(50)  NOT NULL DEFAULT '',
    query_version             INT,
    attempt                   INT          NOT NULL DEFAULT 1,
    status                    VARCHAR(12)  NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running', 'success', 'failed', 'interrupted')),
    failure_stage             VARCHAR(12)  CHECK (failure_stage IS NULL OR failure_stage IN
                              ('config', 'extract', 'transform', 'load', 'report')),
    started_at                TIMESTAMPTZ,
    finished_at               TIMESTAMPTZ,
    duration_ms               BIGINT,
    rows_read                 BIGINT,
    rows_loaded               BIGINT,
    rows_inserted             BIGINT,
    rows_updated              BIGINT,
    error_code                VARCHAR(64),
    error_message_sanitized   VARCHAR(1000),
    warnings                  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    checkpoint_confirmed      TIMESTAMP,
    checkpoint_kind           VARCHAR(16),
    event_time                TIMESTAMPTZ,
    received_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    start_agent_seq           BIGINT,
    last_agent_seq            BIGINT       NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_texec_task_started ON task_execution(task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_texec_inst_started ON task_execution(installation_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_texec_status       ON task_execution(status);
CREATE INDEX IF NOT EXISTS idx_texec_group        ON task_execution(group_id);
CREATE INDEX IF NOT EXISTS idx_texec_received     ON task_execution(received_at DESC);


CREATE TABLE IF NOT EXISTS task_sync_state (
    task_id                   INT          PRIMARY KEY REFERENCES agency_task(id) ON DELETE CASCADE,
    watermark                 TIMESTAMP,
    watermark_kind            VARCHAR(16),
    last_success_at           TIMESTAMPTZ,
    last_failure_at           TIMESTAMPTZ,
    last_execution_id         UUID,
    last_status               VARCHAR(12),
    consecutive_failures      INT          NOT NULL DEFAULT 0,
    current_error_code        VARCHAR(64),
    installation_id           UUID,
    last_event_seq            BIGINT,
    last_event_started_at     TIMESTAMPTZ,
    -- Reinicio manual del watermark desde el panel: se ignoran checkpoints de
    -- ejecuciones que empezaron antes de esta fecha.
    watermark_reset_at        TIMESTAMPTZ,
    updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
